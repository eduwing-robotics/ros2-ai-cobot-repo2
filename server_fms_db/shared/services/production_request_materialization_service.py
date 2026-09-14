"""Atomic conversion of an approved pending request into one Job per house."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeVar
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import (
    PendingProductionRequest,
    PendingProductionState,
    ProductionJob,
)
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService
from shared.services.assembly_recipe_service import AssemblyRecipeService
from shared.services.pending_production_request_service import (
    InvalidPendingProductionRequestStateTransitionError,
    PendingProductionRequestNotFoundError,
)
from shared.services.product_lookup_service import ProductLookupService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from api_server.services.voice_timing import voice_timing_stage
from shared.realtime.production_events import (
    ProductionChangeCallback,
    get_production_change_callback,
)


class ProductionRequestMaterializationError(RuntimeError):
    """Base error for converting a PendingProductionRequest into production jobs."""



class ProductionInventoryShortageError(ProductionRequestMaterializationError):
    def __init__(self, message: str, shortages: list) -> None:
        super().__init__(message)
        self.shortages = shortages
class PendingMaterializationInvariantError(ProductionRequestMaterializationError):
    """Raised when a pending request or its existing linked jobs is inconsistent."""


class PendingMaterializationStateError(ProductionRequestMaterializationError):
    """Raised when a pending state cannot legally be materialized."""


@dataclass(frozen=True)
class ProductionMaterializationResult:
    pending: PendingProductionRequest
    jobs: list[ProductionJob]
    created: bool


_Result = TypeVar("_Result")

logger = logging.getLogger(__name__)


class ProductionRequestMaterializationService:
    """Materialize exactly one Job per requested house in one DB transaction."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
        job_code_generator: Callable[[str, int, int], str] | None = None,
        preflight_service: ProductionInventoryPreflightService | None = None,
        post_commit_callback: ProductionChangeCallback | None = None,
    ) -> None:
        self._session = session
        self._orchestration = ProductionOrchestrationService(session)
        self._products = ProductLookupService(session)
        self._recipes = AssemblyRecipeService(session)
        self._clock = clock or self._utcnow
        self._job_code_generator = job_code_generator
        self._preflight = preflight_service or ProductionInventoryPreflightService(session)
        self._post_commit_callback = post_commit_callback or get_production_change_callback()

    def confirm_and_create_jobs(
        self, *, pending_request_id: int
    ) -> ProductionMaterializationResult:
        """Confirm a ready request and atomically create all of its independent Jobs.

        A Phase 4 CONFIRMED request with no linked jobs is intentionally accepted for
        backwards compatibility. Fully linked requests return their existing jobs;
        partial links are surfaced as an integrity error and never auto-repaired.
        """

        self._validate_positive_id(pending_request_id, field_name="pending_request_id")

        def operation() -> tuple[ProductionMaterializationResult | None, bool]:
            pending = self._get_pending_for_update(pending_request_id)
            now = self._now()
            if self._expire_active_if_needed(pending, now=now):
                self._session.flush()
                return None, True

            jobs = self._linked_jobs(pending.request_id)
            self._validate_existing_links(pending, jobs)
            if jobs:
                if pending.state is not PendingProductionState.CONFIRMED:
                    raise PendingMaterializationStateError(
                        "Linked production jobs require a CONFIRMED pending request."
                    )
                return ProductionMaterializationResult(pending=pending, jobs=jobs, created=False), False

            if pending.state not in {
                PendingProductionState.AWAITING_CONFIRMATION,
                PendingProductionState.CONFIRMED,
            }:
                raise PendingMaterializationStateError(
                    f"Pending request state {pending.state.value} cannot be materialized."
                )
            if pending.product_code is None:
                raise PendingMaterializationInvariantError(
                    "A product is required before production jobs can be created."
                )
            if pending.roof_option_code is None:
                raise PendingMaterializationInvariantError(
                    "A roof option is required before production jobs can be created."
                )
            if pending.quantity < 1:
                raise PendingMaterializationInvariantError("Pending request quantity must be positive.")

            product = self._products.get_by_code(pending.product_code)
            recipe = self._recipes.get_active_recipe_for_product(
                pending.product_code, for_update=True
            )

            with voice_timing_stage("confirm_preflight_ms"):
                preflight_result = self._preflight.validate(
                    product_code=pending.product_code,
                    quantity=pending.quantity,
                    roof_option_code=pending.roof_option_code,
                )
            if not preflight_result.can_produce:
                raise ProductionInventoryShortageError(
                    "Production inventory preflight check failed during job creation.",
                    preflight_result.shortages,
                )

            created_jobs: list[ProductionJob] = []
            for item_index in range(1, pending.quantity + 1):
                created_jobs.append(
                    self._create_job(
                        product_code=product.product_code,
                        pending=pending,
                        item_index=item_index,
                        assembly_recipe_id=recipe.recipe_id,
                    )
                )

            pending.state = PendingProductionState.CONFIRMED
            pending.confirmed_at = pending.confirmed_at or now
            pending.rejected_at = None
            pending.updated_at = now
            self._session.flush()
            return ProductionMaterializationResult(
                pending=pending, jobs=created_jobs, created=True
            ), False

        result, expired = self._run_write_transaction(operation)
        if expired:
            raise PendingMaterializationStateError("Expired pending requests cannot be materialized.")
        assert result is not None
        self._notify_created_jobs_after_commit(result)
        return result

    def _notify_created_jobs_after_commit(self, result: ProductionMaterializationResult) -> None:
        """Publish identity-only refresh triggers after the authoritative commit.

        Redis publication is intentionally best effort: a committed Job must
        remain successful even when a process-local realtime publisher fails.
        """
        if not result.created or self._post_commit_callback is None:
            return
        for job_id in sorted({job.job_id for job in result.jobs}):
            try:
                self._post_commit_callback(job_id, "job_created")
            except Exception as exc:
                logger.warning("Could not publish committed Job creation job_id=%s: %s", job_id, exc)

    def _create_job(
        self,
        *,
        product_code: str,
        pending: PendingProductionRequest,
        item_index: int,
        assembly_recipe_id: int,
    ) -> ProductionJob:
        return self._orchestration._create_job_in_transaction(
            product_code=product_code,
            job_code=f"PENDING-{pending.request_id}-{item_index}",
            roof_option_code=pending.roof_option_code,
            source_pending_request_id=pending.request_id,
            source_item_index=item_index,
            assembly_recipe_id=assembly_recipe_id,
        )

    def _get_pending_for_update(self, request_id: int) -> PendingProductionRequest:
        pending = self._session.scalar(
            select(PendingProductionRequest)
            .where(PendingProductionRequest.request_id == request_id)
            .with_for_update()
        )
        if pending is None:
            raise PendingProductionRequestNotFoundError(
                f"Pending production request not found: request_id={request_id}."
            )
        return pending

    def _linked_jobs(self, request_id: int) -> list[ProductionJob]:
        return list(
            self._session.scalars(
                select(ProductionJob)
                .where(ProductionJob.source_pending_request_id == request_id)
                .order_by(ProductionJob.source_item_index)
            )
        )

    @staticmethod
    def _validate_existing_links(
        pending: PendingProductionRequest, jobs: list[ProductionJob]
    ) -> None:
        if not jobs:
            return
        expected_indexes = list(range(1, pending.quantity + 1))
        actual_indexes = [job.source_item_index for job in jobs]
        if actual_indexes != expected_indexes:
            raise PendingMaterializationInvariantError(
                "Existing linked production jobs are incomplete or have invalid source indexes."
            )
        if any(
            job.roof_option_code is not pending.roof_option_code
            for job in jobs
        ):
            raise PendingMaterializationInvariantError(
                "Existing linked production jobs do not match the pending roof option."
            )

    @staticmethod
    def _expire_active_if_needed(pending: PendingProductionRequest, *, now: datetime) -> bool:
        expires_at = pending.expires_at
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if pending.state in {
            PendingProductionState.COLLECTING_DETAILS,
            PendingProductionState.WAITING_ROOF_OPTION,
            PendingProductionState.AWAITING_CONFIRMATION,
        } and expires_at <= now:
            pending.state = PendingProductionState.EXPIRED
            return True
        return False

    def _run_write_transaction(self, operation: Callable[[], _Result]) -> _Result:
        try:
            result = operation()
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise PendingMaterializationInvariantError("clock must return a timezone-aware datetime.")
        return value

    @staticmethod
    def _validate_positive_id(value: int, *, field_name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PendingMaterializationInvariantError(f"{field_name} must be a positive integer.")

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)
