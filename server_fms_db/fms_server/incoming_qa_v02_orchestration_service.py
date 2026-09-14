"""Durable job-level orchestration for Incoming QA v0.2 transactions.

This layer plans persisted v0.2 transactions and derives the next eligible send
from durable state.  It deliberately does not parse UDP, apply Vision results,
change production readiness, or infer any cycle from transaction sequence.
"""

from __future__ import annotations

import uuid
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.realtime.incoming_qa_events import get_incoming_qa_change_callback
from shared.realtime.production_events import get_production_change_callback
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    StepStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    ProductionJob,
)
from shared.schemas.vision import IncomingQARequestItemV02, IncomingQARequestV02
from shared.services.incoming_qa_orchestration_service import IncomingQAOrchestrationService
from shared.services.incoming_qa_transaction_service import (
    IncomingQATransactionCorrelationError,
    IncomingQATransactionService,
)
from shared.vision_recipe_mapping import (
    IncomingQAInspectionMode,
    VisionRecipeMappingError,
    initial_mode_slots_for_product,
    resolve_product_mode_slot,
)


logger = logging.getLogger(__name__)


class IncomingQAV02OrchestrationError(RuntimeError):
    """Persisted expected-material data cannot safely form a Vision request."""


class IncomingQAV02ActiveInspectionError(IncomingQAV02OrchestrationError):
    """Selected items already have a queued or physically active v0.2 request."""


@dataclass(frozen=True, slots=True)
class IncomingQAV02PlanResult:
    """Durable planning/reconciliation result; caller owns any UDP send."""

    created_transaction_ids: tuple[int, ...] = ()
    send_transaction_id: int | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedItem:
    delivery_item_id: int
    part_code: str
    vision_class: str
    quantity: int
    mode: IncomingQAInspectionMode
    slot_id: str


@dataclass(frozen=True, slots=True)
class IncomingQATestHoldState:
    enabled: bool
    can_enable: bool
    can_disable: bool
    can_advance_house_b: bool
    can_release: bool


_ACTIVE_TRANSACTION_STATUSES = (
    IncomingQATransactionStatus.SENT,
    IncomingQATransactionStatus.ACKED,
)
_TERMINAL_BLOCKING_STATUSES = (
    IncomingQATransactionStatus.REJECTED,
    IncomingQATransactionStatus.ERROR,
)
_TERMINAL_JOB_STATUSES = (
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELED,
)


class IncomingQAV02OrchestrationService:
    """Plan first/reinspection work and reconcile a job's durable transaction queue."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._transactions = IncomingQATransactionService()
        self._global_qa = IncomingQAOrchestrationService(session)

    def start_initial_inspection(self, *, job_id: int) -> IncomingQAV02PlanResult:
        """Create exactly the first configured mode transaction for a fresh job.

        For HOUSE_B the central mapping defines BASE_AB/C09 first.  The next
        HOUSE_B mode is intentionally created later by :meth:`reconcile_job`
        after the persisted BASE terminal result is known.
        """

        try:
            self._global_qa.acquire_global_transaction_guard()
            job, by_mode = self._locked_job_and_expected_items(job_id)
            existing = self._transactions_for_job(job_id)
            if existing:
                self._session.commit()
                return self._reconcile_after_commit_hint(existing)
            if self._has_active_transaction() or self._has_legacy_active_inspection():
                self._session.commit()
                return IncomingQAV02PlanResult(blocked_reason="ACTIVE_INCOMING_QA_TRANSACTION")

            first_mode, _ = initial_mode_slots_for_product(job.product_code)[0]
            first_items = by_mode[first_mode]
            self._require_no_inspection_history(first_items)
            created = self._create_group(
                job_id=job.job_id,
                mode=first_mode,
                inspection_cycle=1,
                items=first_items,
            )
            self._session.commit()
            self._notify_after_commit(created.transaction.transaction_id)
            self._notify_production_after_commit(job.job_id, "incoming_qa_transaction_created")
            return IncomingQAV02PlanResult(
                created_transaction_ids=(created.transaction.transaction_id,),
                send_transaction_id=created.transaction.transaction_id,
            )
        except Exception:
            self._session.rollback()
            raise

    def request_reinspection(
        self,
        *,
        job_id: int,
        delivery_item_ids: Iterable[int],
    ) -> IncomingQAV02PlanResult:
        """Persist deterministic same-mode/same-next-cycle groups for selected items.

        Operator selection may include PASS items.  A history-free selected item
        has next cycle 1; the caller controls selection, while this service only
        preserves per-item cycle truth and Vision's single-cycle request shape.
        """

        selected_ids = tuple(sorted(set(delivery_item_ids)))
        if not selected_ids:
            raise IncomingQAV02OrchestrationError("At least one delivery_item_id is required.")
        try:
            self._global_qa.acquire_global_transaction_guard()
            job, _ = self._locked_job_and_expected_items(job_id)
            resolved = self._locked_selected_items(job, selected_ids)
            active_item_ids = self._active_selected_item_ids(selected_ids)
            if active_item_ids:
                raise IncomingQAV02ActiveInspectionError(
                    "Selected items already have queued/active Incoming QA transactions: "
                    + ", ".join(str(item_id) for item_id in sorted(active_item_ids))
                )
            if self._has_active_transaction() or self._has_legacy_active_inspection():
                self._session.commit()
                return IncomingQAV02PlanResult(blocked_reason="ACTIVE_INCOMING_QA_TRANSACTION")

            grouped: dict[tuple[IncomingQAInspectionMode, int], list[_ResolvedItem]] = {}
            for item in resolved:
                next_cycle = self._next_cycle(item.delivery_item_id)
                grouped.setdefault((item.mode, next_cycle), []).append(item)
            created_ids: list[int] = []
            for (mode, cycle), group in sorted(
                grouped.items(),
                key=lambda entry: self._group_sort_key(job.product_code, entry[0], entry[1]),
            ):
                created = self._create_group(
                    job_id=job.job_id,
                    mode=mode,
                    inspection_cycle=cycle,
                    items=group,
                )
                created_ids.append(created.transaction.transaction_id)
            self._session.commit()
            self._notify_many_after_commit(created_ids)
            if created_ids:
                self._notify_production_after_commit(job.job_id, "incoming_qa_transaction_created")
            return IncomingQAV02PlanResult(
                created_transaction_ids=tuple(created_ids),
                send_transaction_id=created_ids[0] if created_ids else None,
            )
        except Exception:
            self._session.rollback()
            raise

    def request_mode_reinspection(
        self,
        *,
        job_id: int,
        inspection_mode: IncomingQAInspectionMode,
    ) -> IncomingQAV02PlanResult:
        """Create one full next cycle for a completed persisted QA mode.

        This is an operator planning boundary only: it persists a REQUESTED
        transaction and leaves UDP transmission to the normal FMS reconciler.
        """
        try:
            self._global_qa.acquire_global_transaction_guard()
            job, by_mode = self._locked_job_and_expected_items(job_id)
            self._require_mode_reinspection_job_safe(job)
            if inspection_mode not in by_mode:
                raise IncomingQAV02OrchestrationError(
                    f"Incoming QA mode {inspection_mode.value} is not configured for job product {job.product_code}."
                )
            transactions = self._transactions_for_job(job.job_id)
            mode_transactions = [
                transaction
                for transaction in transactions
                if transaction.inspection_mode == inspection_mode.value
            ]
            if not mode_transactions:
                raise IncomingQAV02OrchestrationError(
                    f"Incoming QA mode {inspection_mode.value} has not completed an initial cycle."
                )
            latest = max(mode_transactions, key=lambda transaction: transaction.inspection_cycle)
            if latest.status in (
                IncomingQATransactionStatus.REQUESTED,
            ) + _ACTIVE_TRANSACTION_STATUSES:
                raise IncomingQAV02ActiveInspectionError(
                    f"Incoming QA mode {inspection_mode.value} already has an active cycle {latest.inspection_cycle}."
                )
            if latest.status is not IncomingQATransactionStatus.COMPLETED:
                raise IncomingQAV02OrchestrationError(
                    f"Incoming QA mode {inspection_mode.value} must have a completed latest cycle before reinspection."
                )
            if self._has_pending_or_active_transaction() or self._has_legacy_active_inspection():
                raise IncomingQAV02ActiveInspectionError(
                    "Another pending or live Incoming QA transaction currently owns the shared Vision resource."
                )
            created = self._create_group(
                job_id=job.job_id,
                mode=inspection_mode,
                inspection_cycle=latest.inspection_cycle + 1,
                items=by_mode[inspection_mode],
            )
            self._session.commit()
            self._notify_after_commit(created.transaction.transaction_id)
            self._notify_production_after_commit(job.job_id, "incoming_qa_transaction_created")
            return IncomingQAV02PlanResult(
                created_transaction_ids=(created.transaction.transaction_id,),
                send_transaction_id=created.transaction.transaction_id,
            )
        except Exception:
            self._session.rollback()
            raise

    def complete_current_mode_for_test_override(self, *, job_id: int) -> IncomingQATransaction:
        """Durably mark exactly one eligible v0.2 QA mode PASS without Vision I/O."""
        try:
            self._global_qa.acquire_global_transaction_guard()
            job, by_mode = self._locked_job_and_expected_items(job_id)
            self._require_mode_reinspection_job_safe(job)
            if self._has_pending_or_active_transaction() or self._has_legacy_active_inspection():
                raise IncomingQAV02ActiveInspectionError(
                    "Incoming QA Test Override is blocked while a real or queued inspection transaction exists."
                )
            mode_order = [mode for mode, _slots in initial_mode_slots_for_product(job.product_code)]
            transactions = self._transactions_for_job(job.job_id)
            selected_mode: IncomingQAInspectionMode | None = None
            cycle = 1
            for mode in mode_order:
                item_ids = {item.delivery_item_id for item in by_mode[mode]}
                latest = self._latest_transaction_for_exact_items(transactions, mode, item_ids)
                if latest is None:
                    selected_mode = mode
                    break
                if latest.status is not IncomingQATransactionStatus.COMPLETED:
                    raise IncomingQAV02ActiveInspectionError(
                        "Incoming QA Test Override is blocked while the current inspection transaction is nonterminal."
                    )
                if latest.overall_result is not MaterialInspectionResult.PASS:
                    selected_mode = mode
                    cycle = latest.inspection_cycle + 1
                    break
            if selected_mode is None:
                raise IncomingQAV02OrchestrationError("All required Incoming QA modes already passed.")
            if cycle == 1:
                self._require_no_inspection_history(by_mode[selected_mode])
            created = self._create_group(
                job_id=job.job_id, mode=selected_mode, inspection_cycle=cycle, items=by_mode[selected_mode]
            )
            transaction = created.transaction
            now = datetime.now(timezone.utc)
            transaction.status = IncomingQATransactionStatus.COMPLETED
            transaction.overall_result = MaterialInspectionResult.PASS
            # This test seam is not a Vision Runtime authorization assertion.
            transaction.production_valid = False
            transaction.completed_at = now
            inspections = list(self._session.scalars(
                select(MaterialInspection).where(
                    MaterialInspection.incoming_qa_transaction_id == transaction.transaction_id
                )
            ))
            for inspection in inspections:
                inspection.status = MaterialInspectionStatus.COMPLETED
                inspection.result = MaterialInspectionResult.PASS
                inspection.production_valid = True
                inspection.completed_at = now
            self._session.commit()
            self._notify_after_commit(transaction.transaction_id)
            self._notify_production_after_commit(job.job_id, "incoming_qa_test_override_pass")
            return transaction
        except Exception:
            self._session.rollback()
            raise

    def set_test_hold(self, *, job_id: int, enabled: bool) -> bool:
        """Persist a pre-production QA test hold without changing QA evidence."""
        try:
            job = self._locked_job(job_id)
            self._require_mode_reinspection_job_safe(job)
            job.incoming_qa_test_hold = enabled
            self._session.commit()
            self._notify_production_after_commit(job.job_id, "incoming_qa_test_hold_changed")
            return job.incoming_qa_test_hold
        except Exception:
            self._session.rollback()
            raise

    def advance_house_b_for_test_hold(self, *, job_id: int) -> IncomingQAV02PlanResult:
        """Create HOUSE_B cycle 1 from a held, successful BASE_AB boundary.

        This is deliberately only durable planning. The normal FMS reconciler
        remains the sole UDP sender.
        """
        try:
            self._global_qa.acquire_global_transaction_guard()
            job, by_mode = self._locked_job_and_expected_items(job_id)
            self._require_mode_reinspection_job_safe(job)
            if not job.incoming_qa_test_hold:
                raise IncomingQAV02OrchestrationError(
                    "HOUSE_B test advance requires incoming_qa_test_hold to be enabled."
                )
            mode_order = initial_mode_slots_for_product(job.product_code)
            if len(mode_order) < 2:
                raise IncomingQAV02OrchestrationError("This product has no HOUSE_B follow-up Incoming QA mode.")
            first_mode, followup_mode = mode_order[0][0], mode_order[1][0]
            transactions = self._transactions_for_job(job.job_id)
            first_ids = {item.delivery_item_id for item in by_mode[first_mode]}
            followup_ids = {item.delivery_item_id for item in by_mode[followup_mode]}
            latest_first = self._latest_transaction_for_exact_items(
                transactions, first_mode, first_ids
            )
            if (
                latest_first is None
                or latest_first.status is not IncomingQATransactionStatus.COMPLETED
                or latest_first.overall_result is not MaterialInspectionResult.PASS
            ):
                raise IncomingQAV02OrchestrationError("HOUSE_B test advance requires a latest completed BASE_AB PASS.")
            if self._has_pending_or_active_transaction() or self._has_legacy_active_inspection():
                raise IncomingQAV02ActiveInspectionError(
                    "Another pending or live Incoming QA transaction currently owns the shared Vision resource."
                )
            if self._has_initial_transaction(transactions, followup_mode, followup_ids):
                raise IncomingQAV02ActiveInspectionError("HOUSE_B initial inspection already exists for this job.")
            self._require_no_inspection_history(by_mode[followup_mode])
            created = self._create_group(
                job_id=job.job_id,
                mode=followup_mode,
                inspection_cycle=1,
                items=by_mode[followup_mode],
            )
            self._session.commit()
            self._notify_after_commit(created.transaction.transaction_id)
            self._notify_production_after_commit(job.job_id, "incoming_qa_house_b_advanced")
            return IncomingQAV02PlanResult(
                created_transaction_ids=(created.transaction.transaction_id,),
                send_transaction_id=created.transaction.transaction_id,
            )
        except Exception:
            self._session.rollback()
            raise

    def test_hold_state(self, *, job_id: int) -> IncomingQATestHoldState:
        """Read-only backend authority for Monitoring GUI QA-hold controls."""
        job = self._session.get(ProductionJob, job_id)
        if job is None:
            raise IncomingQAV02OrchestrationError(f"Production job not found: job_id={job_id}.")
        safe = self._is_mode_reinspection_job_safe(job)
        enabled = bool(job.incoming_qa_test_hold)
        can_advance = False
        can_release = False
        if safe and enabled:
            try:
                mode_order = initial_mode_slots_for_product(job.product_code)
                if len(mode_order) >= 2 and not self._has_pending_or_active_transaction() and not self._has_legacy_active_inspection():
                    first_mode, followup_mode = mode_order[0][0], mode_order[1][0]
                    transactions = self._transactions_for_job_read_only(job.job_id)
                    first_ids = self._mode_item_ids(job.job_id, first_mode)
                    followup_ids = self._mode_item_ids(job.job_id, followup_mode)
                    latest_first = self._latest_transaction_for_exact_items(transactions, first_mode, first_ids)
                    can_advance = (
                        latest_first is not None
                        and latest_first.status is IncomingQATransactionStatus.COMPLETED
                        and latest_first.overall_result is MaterialInspectionResult.PASS
                        and not self._has_initial_transaction(transactions, followup_mode, followup_ids)
                    )
                    can_release = IncomingQAOrchestrationService(self._session).preproduction_readiness(
                        job_id=job.job_id
                    ).qa_passed
            except VisionRecipeMappingError:
                pass
        return IncomingQATestHoldState(
            enabled=enabled,
            can_enable=safe and not enabled,
            can_disable=safe and enabled,
            can_advance_house_b=can_advance,
            can_release=can_release,
        )

    def can_request_mode_reinspection(
        self,
        *,
        job_id: int,
        inspection_mode: IncomingQAInspectionMode,
    ) -> bool:
        """Read-only UI hint; ``request_mode_reinspection`` remains authority."""
        job = self._session.get(ProductionJob, job_id)
        if job is None or not self._is_mode_reinspection_job_safe(job):
            return False
        try:
            configured_modes = {mode for mode, _slots in initial_mode_slots_for_product(job.product_code)}
        except VisionRecipeMappingError:
            return False
        if inspection_mode not in configured_modes:
            return False
        if self._has_pending_or_active_transaction() or self._has_legacy_active_inspection():
            return False
        latest = self._session.scalar(
            select(IncomingQATransaction)
            .where(
                IncomingQATransaction.production_job_id == job_id,
                IncomingQATransaction.inspection_mode == inspection_mode.value,
            )
            .order_by(
                IncomingQATransaction.inspection_cycle.desc(),
                IncomingQATransaction.transaction_id.desc(),
            )
            .limit(1)
        )
        return latest is not None and latest.status is IncomingQATransactionStatus.COMPLETED

    def _require_mode_reinspection_job_safe(self, job: ProductionJob) -> None:
        if not self._is_mode_reinspection_job_safe(job):
            raise IncomingQAV02OrchestrationError(
                "Incoming QA mode reinspection is not allowed after downstream assembly has started or for a terminal production job."
            )

    def _is_mode_reinspection_job_safe(self, job: ProductionJob) -> bool:
        if job.status in _TERMINAL_JOB_STATUSES:
            return False
        return self._session.scalar(
            select(JobStep.job_step_id)
            .where(
                JobStep.job_id == job.job_id,
                JobStep.status != StepStatus.PENDING,
            )
            .limit(1)
        ) is None

    def reconcile_job(self, *, job_id: int) -> IncomingQAV02PlanResult:
        """Derive one next safe send or missing initial progression from DB state."""

        try:
            self._global_qa.acquire_global_transaction_guard()
            job, by_mode = self._locked_job_and_expected_items(job_id)
            transactions = self._transactions_for_job(job_id)
            if not transactions:
                self._session.commit()
                return IncomingQAV02PlanResult(blocked_reason="INITIAL_INSPECTION_NOT_STARTED")
            if self._has_active_transaction() or self._has_legacy_active_inspection():
                self._session.commit()
                return IncomingQAV02PlanResult(blocked_reason="ACTIVE_INCOMING_QA_TRANSACTION")

            # A prior transport/protocol failure must be operated on explicitly;
            # never skip it and send a later queued transaction.
            requested = [tx for tx in transactions if tx.status is IncomingQATransactionStatus.REQUESTED]
            if requested:
                first = min(requested, key=lambda tx: tx.transaction_id)
                if any(
                    tx.transaction_id < first.transaction_id and tx.status in _TERMINAL_BLOCKING_STATUSES
                    for tx in transactions
                ):
                    self._session.commit()
                    return IncomingQAV02PlanResult(blocked_reason="PRIOR_TRANSACTION_ERROR")
                self._session.commit()
                return IncomingQAV02PlanResult(send_transaction_id=first.transaction_id)

            result = self._reconcile_initial_progression(job, by_mode, transactions)
            self._session.commit()
            self._notify_many_after_commit(result.created_transaction_ids)
            if result.created_transaction_ids:
                self._notify_production_after_commit(job.job_id, "incoming_qa_transaction_created")
            return result
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _notify_after_commit(transaction_id: int) -> None:
        callback = get_incoming_qa_change_callback()
        if callback is None:
            return
        try:
            callback(transaction_id)
        except Exception:  # Redis is secondary to a committed orchestration plan
            logger.exception("Incoming QA post-commit notification failed transaction=%s", transaction_id)

    @classmethod
    def _notify_many_after_commit(cls, transaction_ids: Iterable[int]) -> None:
        for transaction_id in transaction_ids:
            cls._notify_after_commit(transaction_id)

    @staticmethod
    def _notify_production_after_commit(job_id: int, reason: str) -> None:
        callback = get_production_change_callback()
        if callback is None:
            return
        try:
            callback(job_id, reason)
        except Exception:  # best-effort notification cannot undo a committed hold change
            logger.exception("Incoming QA production post-commit notification failed job=%s", job_id)

    def _reconcile_initial_progression(
        self,
        job: ProductionJob,
        by_mode: dict[IncomingQAInspectionMode, tuple[_ResolvedItem, ...]],
        transactions: list[IncomingQATransaction],
    ) -> IncomingQAV02PlanResult:
        mode_order = initial_mode_slots_for_product(job.product_code)
        if len(mode_order) < 2:
            return IncomingQAV02PlanResult(blocked_reason="NO_FOLLOWUP_INITIAL_MODE")
        first_mode = mode_order[0][0]
        followup_mode = mode_order[1][0]
        first_item_ids = {item.delivery_item_id for item in by_mode[first_mode]}
        followup_item_ids = {item.delivery_item_id for item in by_mode[followup_mode]}

        if self._has_initial_transaction(transactions, followup_mode, followup_item_ids):
            return IncomingQAV02PlanResult(blocked_reason="INITIAL_INSPECTION_COMPLETE")

        latest_first = self._latest_transaction_for_exact_items(
            transactions, first_mode, first_item_ids
        )
        if latest_first is None or latest_first.status is not IncomingQATransactionStatus.COMPLETED:
            if any(tx.status in _TERMINAL_BLOCKING_STATUSES and tx.inspection_mode == first_mode.value for tx in transactions):
                return IncomingQAV02PlanResult(blocked_reason="INITIAL_TRANSACTION_ERROR")
            return IncomingQAV02PlanResult(blocked_reason="WAITING_INITIAL_TERMINAL_RESULT")
        if latest_first.overall_result is not MaterialInspectionResult.PASS:
            return IncomingQAV02PlanResult(blocked_reason="BASE_PASS_REQUIRED")
        if job.incoming_qa_test_hold:
            return IncomingQAV02PlanResult(blocked_reason="INCOMING_QA_TEST_HOLD")
        # Vision runtime authorization is retained as metadata. Incoming QA
        # progression is governed by completed PASS/FAIL inspection evidence.
        self._require_no_inspection_history(by_mode[followup_mode])
        created = self._create_group(
            job_id=job.job_id,
            mode=followup_mode,
            inspection_cycle=1,
            items=by_mode[followup_mode],
        )
        return IncomingQAV02PlanResult(
            created_transaction_ids=(created.transaction.transaction_id,),
            send_transaction_id=created.transaction.transaction_id,
        )

    def _locked_job(self, job_id: int) -> ProductionJob:
        job = self._session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update()
        )
        if job is None:
            raise IncomingQAV02OrchestrationError(f"Production job not found: job_id={job_id}.")
        return job

    def _transactions_for_job_read_only(self, job_id: int) -> list[IncomingQATransaction]:
        return list(self._session.scalars(
            select(IncomingQATransaction)
            .where(IncomingQATransaction.production_job_id == job_id)
            .order_by(IncomingQATransaction.transaction_id)
        ))

    def _mode_item_ids(self, job_id: int, mode: IncomingQAInspectionMode) -> set[int]:
        rows = self._session.execute(
            select(JobMaterialDeliveryItem.delivery_item_id, Part.vision_class)
            .join(JobMaterialDelivery, JobMaterialDelivery.job_delivery_id == JobMaterialDeliveryItem.job_delivery_id)
            .join(Part, Part.part_code == JobMaterialDeliveryItem.part_code)
            .where(JobMaterialDelivery.production_job_id == job_id)
        )
        job = self._session.get(ProductionJob, job_id)
        assert job is not None
        result: set[int] = set()
        for item_id, vision_class in rows:
            if not vision_class:
                continue
            resolved_mode, _slot = resolve_product_mode_slot(
                product_code=job.product_code, vision_class=vision_class
            )
            if resolved_mode is mode:
                result.add(item_id)
        return result

    def _locked_job_and_expected_items(
        self, job_id: int
    ) -> tuple[ProductionJob, dict[IncomingQAInspectionMode, tuple[_ResolvedItem, ...]]]:
        job = self._session.scalar(
            select(ProductionJob).where(ProductionJob.job_id == job_id).with_for_update()
        )
        if job is None:
            raise IncomingQAV02OrchestrationError(f"Production job not found: job_id={job_id}.")
        try:
            expected_recipe = initial_mode_slots_for_product(job.product_code)
        except VisionRecipeMappingError as exc:
            raise IncomingQAV02OrchestrationError(str(exc)) from exc
        rows = list(self._session.execute(self._expected_items_query(job_id)))
        if not rows:
            raise IncomingQAV02OrchestrationError("Incoming QA expected material set is empty.")

        expected_slots = {
            (mode, slot_id)
            for mode, slots in expected_recipe
            for slot_id in slots
        }
        resolved: dict[tuple[IncomingQAInspectionMode, str], _ResolvedItem] = {}
        for item, part in rows:
            if not part.vision_class:
                raise IncomingQAV02OrchestrationError(
                    f"Delivery item {item.delivery_item_id} has no persisted Part vision_class."
                )
            try:
                mode, slot_id = resolve_product_mode_slot(
                    product_code=job.product_code,
                    vision_class=part.vision_class,
                )
            except VisionRecipeMappingError as exc:
                raise IncomingQAV02OrchestrationError(
                    f"Delivery item {item.delivery_item_id}: {exc}"
                ) from exc
            key = (mode, slot_id)
            if key not in expected_slots:
                raise IncomingQAV02OrchestrationError(
                    f"Delivery item {item.delivery_item_id} resolves to unexpected Vision slot {mode.value}/{slot_id}."
                )
            if key in resolved:
                raise IncomingQAV02OrchestrationError(
                    f"Duplicate persisted expected material for Vision slot {mode.value}/{slot_id}."
                )
            resolved[key] = _ResolvedItem(
                delivery_item_id=item.delivery_item_id,
                part_code=item.part_code,
                vision_class=part.vision_class,
                quantity=item.quantity,
                mode=mode,
                slot_id=slot_id,
            )
        if set(resolved) != expected_slots:
            missing = sorted(f"{mode.value}/{slot}" for mode, slot in expected_slots - set(resolved))
            extra = sorted(f"{mode.value}/{slot}" for mode, slot in set(resolved) - expected_slots)
            raise IncomingQAV02OrchestrationError(
                "Persisted expected material does not match Vision recipe; "
                f"missing={missing} extra={extra}."
            )
        return job, {
            mode: tuple(resolved[(mode, slot_id)] for slot_id in slots)
            for mode, slots in expected_recipe
        }

    @staticmethod
    def _expected_items_query(job_id: int):
        """Read master Part data while locking only mutable expected items."""
        return (
            select(JobMaterialDeliveryItem, Part)
            .join(
                JobMaterialDelivery,
                JobMaterialDelivery.job_delivery_id == JobMaterialDeliveryItem.job_delivery_id,
            )
            .join(Part, Part.part_code == JobMaterialDeliveryItem.part_code)
            .where(JobMaterialDelivery.production_job_id == job_id)
            .order_by(JobMaterialDeliveryItem.delivery_item_id)
            .with_for_update(of=JobMaterialDeliveryItem)
        )

    def _locked_selected_items(
        self, job: ProductionJob, selected_ids: tuple[int, ...]
    ) -> tuple[_ResolvedItem, ...]:
        _ignored_job, by_mode = self._locked_job_and_expected_items(job.job_id)
        all_items = {
            item.delivery_item_id: item
            for items in by_mode.values()
            for item in items
        }
        unknown = sorted(set(selected_ids) - set(all_items))
        if unknown:
            raise IncomingQAV02OrchestrationError(
                "Selected delivery items are not expected items for this job: "
                + ", ".join(str(item_id) for item_id in unknown)
            )
        return tuple(all_items[item_id] for item_id in selected_ids)

    def _transactions_for_job(self, job_id: int) -> list[IncomingQATransaction]:
        return list(self._session.scalars(
            select(IncomingQATransaction)
            .where(IncomingQATransaction.production_job_id == job_id)
            .order_by(IncomingQATransaction.transaction_id)
            .with_for_update()
        ))

    def _has_active_transaction(self) -> bool:
        return self._session.scalar(
            select(IncomingQATransaction.transaction_id)
            .join(ProductionJob, ProductionJob.job_id == IncomingQATransaction.production_job_id)
            .where(
                IncomingQATransaction.status.in_(_ACTIVE_TRANSACTION_STATUSES),
                ProductionJob.status.not_in((JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED)),
            )
            .limit(1)
        ) is not None

    def _has_pending_or_active_transaction(self) -> bool:
        """Operator whole-mode reinspection never queues beside another live plan."""
        return self._session.scalar(
            select(IncomingQATransaction.transaction_id)
            .join(ProductionJob, ProductionJob.job_id == IncomingQATransaction.production_job_id)
            .where(
                IncomingQATransaction.status.in_(
                    (IncomingQATransactionStatus.REQUESTED,) + _ACTIVE_TRANSACTION_STATUSES
                ),
                ProductionJob.status.not_in(_TERMINAL_JOB_STATUSES),
            )
            .limit(1)
        ) is not None

    def _has_legacy_active_inspection(self) -> bool:
        """Never plan a UDP transaction beside a live legacy HTTP request."""
        return self._session.scalar(
            select(MaterialInspection.inspection_id)
            .join(
                JobMaterialDeliveryItem,
                JobMaterialDeliveryItem.delivery_item_id == MaterialInspection.delivery_item_id,
            )
            .join(
                JobMaterialDelivery,
                JobMaterialDelivery.job_delivery_id == JobMaterialDeliveryItem.job_delivery_id,
            )
            .join(ProductionJob, ProductionJob.job_id == JobMaterialDelivery.production_job_id)
            .where(
                MaterialInspection.incoming_qa_transaction_id.is_(None),
                MaterialInspection.status.in_(
                    (MaterialInspectionStatus.REQUESTED, MaterialInspectionStatus.RUNNING)
                ),
                ProductionJob.status.not_in((JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED)),
            )
            .limit(1)
        ) is not None

    def _active_selected_item_ids(self, selected_ids: tuple[int, ...]) -> set[int]:
        return set(self._session.scalars(
            select(MaterialInspection.delivery_item_id)
            .join(IncomingQATransaction, IncomingQATransaction.transaction_id == MaterialInspection.incoming_qa_transaction_id)
            .where(
                MaterialInspection.delivery_item_id.in_(selected_ids),
                IncomingQATransaction.status.in_(
                    (IncomingQATransactionStatus.REQUESTED,) + _ACTIVE_TRANSACTION_STATUSES
                ),
            )
        ))

    def _next_cycle(self, delivery_item_id: int) -> int:
        latest = self._session.scalar(
            select(func.max(MaterialInspection.inspection_cycle)).where(
                MaterialInspection.delivery_item_id == delivery_item_id
            )
        )
        return (latest or 0) + 1

    def _require_no_inspection_history(self, items: Iterable[_ResolvedItem]) -> None:
        for item in items:
            if self._next_cycle(item.delivery_item_id) != 1:
                raise IncomingQAV02OrchestrationError(
                    f"Initial v0.2 inspection requires no prior history for delivery_item_id={item.delivery_item_id}."
                )

    def _create_group(
        self,
        *,
        job_id: int,
        mode: IncomingQAInspectionMode,
        inspection_cycle: int,
        items: Iterable[_ResolvedItem],
    ):
        ordered = tuple(sorted(items, key=lambda item: item.slot_id))
        request = IncomingQARequestV02(
            inspection_request_id=str(uuid.uuid4()),
            inspection_cycle=inspection_cycle,
            inspection_mode=mode,
            items=[
                IncomingQARequestItemV02(
                    slot_id=item.slot_id,
                    delivery_item_id=item.delivery_item_id,
                    expected_part_code=item.part_code,
                    expected_class_name=item.vision_class,
                    expected_quantity=item.quantity,
                )
                for item in ordered
            ],
        )
        try:
            return self._transactions.create_or_get(
                self._session, production_job_id=job_id, request=request
            )
        except IncomingQATransactionCorrelationError as exc:
            raise IncomingQAV02OrchestrationError(str(exc)) from exc

    @staticmethod
    def _has_initial_transaction(
        transactions: Iterable[IncomingQATransaction],
        mode: IncomingQAInspectionMode,
        item_ids: set[int],
    ) -> bool:
        for transaction in transactions:
            if transaction.inspection_mode != mode.value or transaction.inspection_cycle != 1:
                continue
            current_ids = {inspection.delivery_item_id for inspection in transaction.inspections}
            if current_ids == item_ids:
                return True
        return False

    @staticmethod
    def _latest_transaction_for_exact_items(
        transactions: Iterable[IncomingQATransaction],
        mode: IncomingQAInspectionMode,
        item_ids: set[int],
    ) -> IncomingQATransaction | None:
        matches = [
            transaction
            for transaction in transactions
            if transaction.inspection_mode == mode.value
            and {inspection.delivery_item_id for inspection in transaction.inspections} == item_ids
        ]
        return max(matches, key=lambda transaction: (transaction.inspection_cycle, transaction.transaction_id), default=None)

    @staticmethod
    def _latest_completed_transaction_for_exact_items(
        transactions: Iterable[IncomingQATransaction],
        mode: IncomingQAInspectionMode,
        item_ids: set[int],
    ) -> IncomingQATransaction | None:
        matches = [
            transaction
            for transaction in transactions
            if transaction.status is IncomingQATransactionStatus.COMPLETED
            and transaction.inspection_mode == mode.value
            and {inspection.delivery_item_id for inspection in transaction.inspections} == item_ids
        ]
        return max(matches, key=lambda transaction: transaction.transaction_id, default=None)

    @staticmethod
    def _group_sort_key(
        product_code: str,
        mode_cycle: tuple[IncomingQAInspectionMode, int],
        items: list[_ResolvedItem],
    ) -> tuple[int, int, tuple[str, ...]]:
        mode, cycle = mode_cycle
        order = [configured_mode for configured_mode, _ in initial_mode_slots_for_product(product_code)]
        return (order.index(mode), cycle, tuple(sorted(item.slot_id for item in items)))

    @staticmethod
    def _reconcile_after_commit_hint(
        transactions: list[IncomingQATransaction],
    ) -> IncomingQAV02PlanResult:
        requested = [tx for tx in transactions if tx.status is IncomingQATransactionStatus.REQUESTED]
        return IncomingQAV02PlanResult(
            send_transaction_id=min(requested, key=lambda tx: tx.transaction_id).transaction_id if requested else None,
            blocked_reason=None if requested else "INITIAL_INSPECTION_ALREADY_EXISTS",
        )


class IncomingQAV02FmsReconciler:
    """Small session-factory boundary for the async FMS UDP loop."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def reconcile_once(self) -> int | None:
        with self._session_factory() as session:
            job_ids = list(session.scalars(self._candidate_job_ids_query()))
            for job_id in job_ids:
                outcome = IncomingQAV02OrchestrationService(session).reconcile_job(job_id=job_id)
                if outcome.send_transaction_id is not None:
                    return outcome.send_transaction_id
        return None

    @staticmethod
    def _candidate_job_ids_query():
        """Select only Jobs whose QA history can still require reconciliation."""
        return (
            select(IncomingQATransaction.production_job_id)
            .join(
                ProductionJob,
                ProductionJob.job_id == IncomingQATransaction.production_job_id,
            )
            .where(ProductionJob.status.not_in(_TERMINAL_JOB_STATUSES))
            .distinct()
            .order_by(IncomingQATransaction.production_job_id)
        )
