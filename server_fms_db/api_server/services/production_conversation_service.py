"""State-aware production-request conversation orchestration.

The service is intentionally transport-agnostic: HTTP and a future voice adapter may
call the same method.  It keeps CommandInterpreter stateless and never creates a
ProductionJob.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService
from shared.services.production_configuration_validator import ProductionConfigurationInvalidError
from shared.services.production_status_query_service import ProductionStatusQueryService
from api_server.services.inventory_voice_query import InventoryVoiceQueryService

from api_server.services.temporary_product_catalog import find_product
from api_server.services.command_interpreter import (
    CommandInterpreter,
    extract_explicit_roof_options,
    normalize_text,
)
from api_server.services.response_message_builder import ResponseMessageBuilder
from api_server.services.voice_timing import voice_timing_stage
from shared.enums.ai import Intent
from shared.models.factory import (
    PendingProductionRequest,
    PendingProductionState,
    RoofOptionCode,
    ProductionJob,
)
from shared.schemas.ai import StructuredCommand
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_request_materialization_service import (
    ProductionInventoryShortageError,
    ProductionRequestMaterializationService,
)
from shared.services.production_control_service import (
    ProductionControlAmbiguousTargetError,
    ProductionControlConflictError,
    ProductionControlNoActiveTargetError,
    ProductionControlOutcome,
    ProductionControlService,
    ProductionControlTargetError,
)

_BARE_ROOF_SELECTION = re.compile(r"([12])\s*번[.!?]?")
_TRAILING_PUNCTUATION = re.compile(r"[.!?]+$")
_YES_ALIASES = frozenset({"네", "예", "응", "맞아", "맞아요", "맞습니다", "yes", "y"})
_NO_ALIASES = frozenset({"아니", "아니요", "아니야", "아닙니다"})
_CONTROL_INTENT_HINT = re.compile(r"(?:일시\s*정지|멈춰|중지|재개|다시\s*(?:시작|진행)|\bpause\b|\bresume\b)", re.IGNORECASE)


@dataclass(frozen=True)
class ProductionConversationResult:
    session_id: str
    message: str
    pending: PendingProductionRequest | None
    command: StructuredCommand | None
    production_jobs: tuple[ProductionJob, ...] = ()


def extract_roof_followup(text: str) -> RoofOptionCode | None:
    """Interpret an explicit roof alias, or bare 1번/2번 only in waiting context."""

    normalized = normalize_text(text)
    options = extract_explicit_roof_options(normalized)
    if len(options) == 1:
        return next(iter(options))
    if len(options) > 1:
        return None
    selection = _BARE_ROOF_SELECTION.fullmatch(normalized)
    if selection is None:
        return None
    return RoofOptionCode.ROOF_01 if selection.group(1) == "1" else RoofOptionCode.ROOF_02


def parse_confirmation_answer(text: str) -> bool | None:
    """Return True/False only for exact, deterministic confirmation aliases."""

    normalized = _TRAILING_PUNCTUATION.sub("", normalize_text(text)).casefold()
    if normalized in _YES_ALIASES:
        return True
    if normalized in _NO_ALIASES:
        return False
    return None


class ProductionConversationService:
    """Coordinate active PendingProductionRequest state around stateless interpretation."""

    def __init__(
        self,
        *,
        pending_service: PendingProductionRequestService,
        interpreter: CommandInterpreter,
        message_builder: ResponseMessageBuilder | None = None,
        materialization_service: ProductionRequestMaterializationService | None = None,
        preflight_service: ProductionInventoryPreflightService | None = None,
        status_query_service: ProductionStatusQueryService | None = None,
        inventory_query_service: InventoryVoiceQueryService | None = None,
        control_service: ProductionControlService | None = None,
    ) -> None:
        self._pending_service = pending_service
        self._interpreter = interpreter
        self._status_query = status_query_service
        self._inventory_query = inventory_query_service
        self._control = control_service
        self._messages = message_builder or ResponseMessageBuilder()
        self._materialization_service = materialization_service
        self._preflight = preflight_service

    async def handle_text(self, *, session_id: str, text: str) -> ProductionConversationResult:
        with voice_timing_stage("pending_lookup_db_ms"):
            active = self._pending_service.get_active_by_session(session_id)
        # An explicit control phrase must not be consumed as a CREATE draft
        # follow-up. Its canonical intent still comes from CommandInterpreter.
        if active is not None and _CONTROL_INTENT_HINT.search(text) is None:
            with voice_timing_stage("active_conversation_ms"):
                return self._handle_active_pending(session_id=session_id, text=text, pending=active)

        with voice_timing_stage("command_interpretation_ms"):
            _, command, _ = await self._interpreter.interpret(text)
        if command.intent in {Intent.PAUSE_JOB, Intent.RESUME_JOB} and self._control is not None:
            return self._handle_control_command(command=command, session_id=session_id, pending=active)
        if active is not None:
            with voice_timing_stage("active_conversation_ms"):
                return self._handle_active_pending(session_id=session_id, text=text, pending=active)
        if self._can_create_pending(command):
            with voice_timing_stage("pending_write_db_ms"):
                pending = self._pending_service.create_pending(
                    session_id=session_id,
                    product_code=command.product_code,
                    quantity=command.quantity or 1,
                    roof_option_code=command.roof_option_code,
                )
            if pending.state is not PendingProductionState.WAITING_ROOF_OPTION and self._preflight is not None:
                try:
                    with voice_timing_stage("preflight_ms"):
                        preflight = self._preflight.validate(
                            product_code=pending.product_code,
                            quantity=pending.quantity,
                            roof_option_code=pending.roof_option_code,
                        )
                except ProductionConfigurationInvalidError:
                    with voice_timing_stage("pending_write_db_ms"):
                        pending = self._pending_service.reject_pending(request_id=pending.request_id)
                    return ProductionConversationResult(
                        session_id=session_id,
                        message=self._messages.build_configuration_invalid_message(pending.product_code),
                        pending=pending,
                        command=command,
                    )
                if not preflight.can_produce:
                    with voice_timing_stage("pending_write_db_ms"):
                        pending = self._pending_service.reject_pending(request_id=pending.request_id)
                    return ProductionConversationResult(
                        session_id=session_id,
                        message=self._messages.build_shortage_message(pending.product_code, preflight.shortages),
                        pending=pending,
                        command=command,
                    )

            return ProductionConversationResult(
                session_id=session_id,
                message=(
                    "생산할 초소형 주택 모델을 말씀해 주세요."
                    if pending.state is PendingProductionState.COLLECTING_DETAILS
                    else self._messages.build_roof_selection_question()
                    if pending.state is PendingProductionState.WAITING_ROOF_OPTION
                    else self._messages.build_pending_confirmation(pending)
                ),
                pending=pending,
                command=command,
            )

        if command.intent == Intent.QUERY_INVENTORY:
            # Category intent is not backed by a filtered inventory query yet;
            # never narrate an unfiltered ALL result as category-specific truth.
            if command.inventory_scope == "CATEGORY":
                message = self._messages.build_inventory_narration(command, None)
            elif self._inventory_query is not None:
                with voice_timing_stage("inventory_query_db_ms"):
                    inventory_result = self._inventory_query.fetch(command)
                message = self._messages.build_inventory_narration(command, inventory_result)
            else:
                message = self._messages.build_interpretation(command)
        elif command.intent == Intent.QUERY_JOB_STATUS and self._status_query is not None:
            with voice_timing_stage("status_query_db_ms"):
                status_result = self._status_query.get_active_job_status()
            message = self._messages.build_job_status_message(status_result)
        else:
            message = self._messages.build_interpretation(command)

        return ProductionConversationResult(
            session_id=session_id,
            message=message,
            pending=None,
            command=command,
        )

    def _handle_control_command(
        self,
        *,
        command: StructuredCommand,
        session_id: str,
        pending: PendingProductionRequest | None,
    ) -> ProductionConversationResult:
        try:
            job_id = self._control.resolve_target_job_id(target_job_id=command.target_job_id)
            if command.intent is Intent.PAUSE_JOB:
                # Voice is the only current command source that requests the
                # Robot Cell v0.3 immediate stop preference. Persist it with
                # the durable PAUSE request; the API never calls ROS directly.
                result = self._control.request_pause(job_id=job_id, immediate=True)
            else:
                result = self._control.request_resume(job_id=job_id)
        except ProductionControlNoActiveTargetError:
            message = "현재 제어할 생산 작업이 없습니다."
        except ProductionControlAmbiguousTargetError:
            message = "여러 생산 작업이 진행 중입니다. 대상 작업 번호를 지정해 주세요."
        except ProductionControlTargetError:
            message = "지정한 생산 작업을 찾을 수 없습니다."
        except ProductionControlConflictError:
            message = (
                "이 생산 작업은 일시정지할 수 있는 상태가 아닙니다."
                if command.intent is Intent.PAUSE_JOB
                else "이 생산 작업은 재개할 수 있는 일시정지 상태가 아닙니다."
            )
        else:
            message = self._control_message(command.intent, result.outcome)
        return ProductionConversationResult(
            session_id=session_id,
            message=message,
            # Control is independent of a same-session CREATE draft.
            pending=pending,
            command=command,
        )


    def _control_message(self, intent: Intent, outcome: ProductionControlOutcome) -> str:
        if intent is Intent.PAUSE_JOB:
            return {
                ProductionControlOutcome.PAUSE_REQUESTED: "생산 일시정지를 요청했습니다.",
                ProductionControlOutcome.ALREADY_PAUSE_REQUESTED: "생산 일시정지가 이미 요청되어 있습니다.",
                ProductionControlOutcome.ALREADY_PAUSED: "생산은 이미 일시정지 상태입니다.",
                ProductionControlOutcome.ACTIVE_FORKLIFT_PHYSICAL_PAUSE_NOT_SUPPORTED: "현재 물류 이동 중인 작업은 물리적 일시정지를 아직 지원하지 않습니다.",
            }.get(outcome, "생산 일시정지 요청을 처리할 수 없습니다.")
        return {
            ProductionControlOutcome.RESUME_REQUESTED: "생산 재개를 요청했습니다.",
            ProductionControlOutcome.ALREADY_RESUME_REQUESTED: "생산 재개가 이미 요청되어 있습니다.",
            ProductionControlOutcome.ALREADY_ACTIVE: "생산은 이미 재개되어 있습니다.",
        }.get(outcome, "생산 재개 요청을 처리할 수 없습니다.")

    def _handle_active_pending(
        self,
        *,
        session_id: str,
        text: str,
        pending: PendingProductionRequest,
    ) -> ProductionConversationResult:
        if pending.state is PendingProductionState.COLLECTING_DETAILS:
            product = find_product(normalize_text(text))
            if product is None:
                return ProductionConversationResult(
                    session_id=session_id,
                    message="생산할 초소형 주택 모델을 말씀해 주세요.",
                    pending=pending,
                    command=None,
                )
            with voice_timing_stage("pending_write_db_ms"):
                pending = self._pending_service.set_product(
                    request_id=pending.request_id, product_code=product.code
                )
            return ProductionConversationResult(
                session_id=session_id,
                message=(self._messages.build_roof_selection_question()
                    if pending.state is PendingProductionState.WAITING_ROOF_OPTION
                    else self._messages.build_pending_confirmation(pending)),
                pending=pending,
                command=None,
            )

        if pending.state is PendingProductionState.WAITING_ROOF_OPTION:
            roof_option = extract_roof_followup(text)
            if roof_option is None:
                return ProductionConversationResult(
                    session_id=session_id,
                    message=self._messages.build_roof_selection_question(),
                    pending=pending,
                    command=None,
                )
            with voice_timing_stage("pending_write_db_ms"):
                pending = self._pending_service.set_roof_option(
                    request_id=pending.request_id,
                    roof_option_code=roof_option,
                )
            if self._preflight is not None:
                try:
                    with voice_timing_stage("preflight_ms"):
                        preflight = self._preflight.validate(
                            product_code=pending.product_code,
                            quantity=pending.quantity,
                            roof_option_code=pending.roof_option_code,
                        )
                except ProductionConfigurationInvalidError:
                    with voice_timing_stage("pending_write_db_ms"):
                        pending = self._pending_service.reject_pending(request_id=pending.request_id)
                    return ProductionConversationResult(
                        session_id=session_id,
                        message=self._messages.build_configuration_invalid_message(pending.product_code),
                        pending=pending,
                        command=None,
                    )
                if not preflight.can_produce:
                    with voice_timing_stage("pending_write_db_ms"):
                        pending = self._pending_service.reject_pending(request_id=pending.request_id)
                    return ProductionConversationResult(
                        session_id=session_id,
                        message=self._messages.build_shortage_message(pending.product_code, preflight.shortages),
                        pending=pending,
                        command=None,
                    )
            return ProductionConversationResult(
                session_id=session_id,
                message=self._messages.build_pending_confirmation(pending),
                pending=pending,
                command=None,
            )

        if pending.state is PendingProductionState.AWAITING_CONFIRMATION:
            answer = parse_confirmation_answer(text)
            if answer is True:
                if self._materialization_service is None:
                    with voice_timing_stage("pending_write_db_ms"):
                        pending = self._pending_service.confirm_pending(request_id=pending.request_id)
                    jobs: tuple[ProductionJob, ...] = ()
                else:
                    try:
                        with voice_timing_stage("materialization_db_ms"):
                            materialized = self._materialization_service.confirm_and_create_jobs(
                                pending_request_id=pending.request_id
                            )
                    except ProductionInventoryShortageError as error:
                        # Materialization rolls back on its authoritative recheck.
                        # Keep the confirmation-ready request so the caller can retry
                        # after inventory changes; never report a false confirmation.
                        pending = self._pending_service.get_by_id(pending.request_id)
                        return ProductionConversationResult(
                            session_id=session_id,
                            message=self._messages.build_shortage_message(
                                pending.product_code, error.shortages
                            ),
                            pending=pending,
                            command=None,
                        )
                    except ProductionConfigurationInvalidError:
                        pending = self._pending_service.get_by_id(pending.request_id)
                        return ProductionConversationResult(
                            session_id=session_id,
                            message=self._messages.build_configuration_invalid_message(pending.product_code),
                            pending=pending,
                            command=None,
                        )
                    pending = materialized.pending
                    jobs = tuple(materialized.jobs)
                return ProductionConversationResult(
                    session_id=session_id,
                    message=self._messages.build_pending_confirmed(job_count=len(jobs)),
                    pending=pending,
                    command=None,
                    production_jobs=jobs,
                )
            if answer is False:
                with voice_timing_stage("pending_write_db_ms"):
                    pending = self._pending_service.reject_pending(request_id=pending.request_id)
                return ProductionConversationResult(
                    session_id=session_id,
                    message=self._messages.build_pending_rejected(),
                    pending=pending,
                    command=None,
                )
            return ProductionConversationResult(
                session_id=session_id,
                message=self._messages.build_pending_confirmation(pending),
                pending=pending,
                command=None,
            )

        # get_active_by_session returns only active states. Keep this defensive branch
        # so a future service change never invokes the LLM for a terminal request.
        return ProductionConversationResult(
            session_id=session_id,
            message=self._messages.build_interpretation(
                StructuredCommand(
                    intent=Intent.UNKNOWN,
                    clarification_needed=True,
                    clarification_message="지원하는 생산 시스템 명령으로 다시 말씀해 주세요.",
                )
            ),
            pending=pending,
            command=None,
        )

    @staticmethod
    def _can_create_pending(command: StructuredCommand) -> bool:
        # CREATE quantity is canonically one when omitted by the existing
        # interpreter contract. A missing product becomes a durable draft.
        return command.intent is Intent.CREATE_PRODUCTION_REQUEST and (
            command.product_code is not None or command.clarification_needed
        )
