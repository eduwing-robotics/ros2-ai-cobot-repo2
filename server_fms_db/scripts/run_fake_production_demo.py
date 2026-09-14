"""Run a recipe-backed ProductionJob through the FMS using FakeCellActionTransport.

This development/demo tool deliberately creates a real recipe snapshot and then uses
FmsExecutionCoordinator for every step. It never selects ROS2 transport, writes ORM
statuses directly, or establishes a Robot Cell payload schema.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from time import sleep
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

# Make this executable as ``.venv/bin/python scripts/run_fake_production_demo.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fms_server.cell_action_transport import CellTaskCommand, CellTaskRawResult
from fms_server.execution_coordinator import CoordinatorOutcome, FmsExecutionCoordinator
from fms_server.fake_cell_action_transport import FakeCellActionExchange, FakeCellActionTransport
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter, RobotCellPartClassMapper
from shared.database import get_session_factory
from shared.models.factory import (
    MaterialDeliveryStatus,
    AssemblyRecipe,
    JobStep,
    ProductionInspection,
    ProductionJob,
    StepStatus,
)
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.product_lookup_service import ProductLookupError, ProductLookupService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService

# Demo-only valid ExecuteTask payloads. These synthetic slots/part values are
# explicitly not Production BOM or Robot Cell master data.
def build_synthetic_demo_parts(*, operation_code: str, step_order: int) -> str:
    """Return one synthetic valid array item for Fake transport demonstrations only."""

    class_name = RobotCellPartClassMapper.map_operation_code(operation_code)
    item = {"slot": f"DEMO_SLOT_{step_order:02d}", "class": class_name}
    return json.dumps([item], separators=(",", ":"))


class FakeProductionDemoError(RuntimeError):
    """Raised when a demo cannot safely continue through the real FMS path."""


@dataclass(frozen=True, slots=True)
class FakeProductionDemoResult:
    job_id: int
    product_code: str
    assembly_recipe_id: int
    assembly_recipe_version: int
    recipe_stage_count: int
    action_commands: tuple[CellTaskCommand, ...]
    final_job_status: str
    inspection_status: str | None
    stopped_outcome: CoordinatorOutcome | None = None


def non_negative_delay(value: str) -> float:
    """Argparse converter for the Fake transport observation delay."""

    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("delay must be a number of seconds.") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("delay must be greater than or equal to zero.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a recipe-backed HOUSE_A/HOUSE_B demo through FakeCellActionTransport only."
    )
    parser.add_argument(
        "--product",
        choices=("HOUSE_A", "HOUSE_B"),
        default="HOUSE_A",
        help="Product code with an active Assembly Recipe (default: HOUSE_A).",
    )
    parser.add_argument(
        "--delay",
        type=non_negative_delay,
        default=2.0,
        metavar="SECONDS",
        help="Demo observation delay after Goal acceptance and after completion before the next dispatch (default: 2).",
    )
    parser.add_argument(
        "--fallback-4-slots",
        action="store_true",
        help="Only dispatch 4 outer wall slots (fallback mode for 8/18 demo).",
    )
    return parser


def run_fake_production_demo(
    session: Session,
    *,
    product_code: str,
    delay_seconds: float,
    auto_satisfy_material: bool = True,
) -> FakeProductionDemoResult:
    """Create and run one Recipe Job through Coordinator + Fake transport only.

    This core function accepts any existing product code for isolated integration
    tests; the user-facing CLI deliberately restricts choices to HOUSE_A/HOUSE_B.
    """

    if delay_seconds < 0:
        raise ValueError("delay_seconds must be greater than or equal to zero.")
    lookup = ProductLookupService(session)
    try:
        product = lookup.get_by_code(product_code)
    except ProductLookupError as exc:
        raise FakeProductionDemoError(f"Product {product_code!r} was not found.") from exc

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job_code = f"FAKE_DEMO_{product.product_code}_{timestamp}_{uuid.uuid4().hex[:8].upper()}"
    orchestration = ProductionOrchestrationService(session)
    job = orchestration.create_job(product_code=product.product_code, job_code=job_code)
    job = orchestration.start_job(job.job_id)
    recipe = session.get(AssemblyRecipe, job.assembly_recipe_id)
    if recipe is None:
        raise FakeProductionDemoError(f"Demo Job {job.job_id} has no Assembly Recipe snapshot.")
    stage_count = session.scalar(
        select(func.count())
        .select_from(JobStep)
        .where(JobStep.job_id == job.job_id, JobStep.source_recipe_stage_id.is_not(None))
    )
    if not stage_count:
        raise FakeProductionDemoError(f"Demo Job {job.job_id} has no recipe-backed JobSteps.")

    exchanges = [
        FakeCellActionExchange(
            CellTaskRawResult(status="SUCCEEDED", detail="Fake demo execution succeeded.", completed_json="[]"),
            execution_delay_seconds=delay_seconds,
        )
        for _ in range(stage_count * 2)
    ]
    transport = FakeCellActionTransport(exchanges)
    coordinator = FmsExecutionCoordinator(
        session,
        orchestration_service=orchestration,
        step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
        robot_cell_adapter=RobotCellActionAdapter(transport),
        execution_attempt_service=ExecutionAttemptService(session),
    )

    print(f"Job ID: {job.job_id}")
    print(f"Product: {product.product_code}")
    print(f"Assembly Recipe: {recipe.recipe_id} / v{recipe.version}")
    print(f"Recipe stage count: {stage_count}")
    print(f"Demo delay: {delay_seconds:g}s after Goal acceptance and between completed steps.")

    fallback_operations = {
        "INSTALL_REAR_OUTER_WALL",
        "INSTALL_DOOR_OUTER_WALL",
        "INSTALL_LEFT_OUTER_WALL",
        "INSTALL_RIGHT_OUTER_WALL",
    }

    stopped_outcome: CoordinatorOutcome | None = None
    try:
        steps_to_run = []
        for ordinal in range(1, stage_count + 1):
            s = session.scalar(select(JobStep).where(JobStep.job_id == job.job_id, JobStep.step_order == ordinal))
            steps_to_run.append(s)

        for ordinal, step in enumerate(steps_to_run, start=1):
            if step is None:
                raise FakeProductionDemoError("Recipe execution ended before all recipe stages were dispatched.")

            # If fallback is enabled, skip steps that are not in the fallback list
            if getattr(sys.modules["__main__"], "fallback_4_slots", False) and step.operation_code not in fallback_operations:
                print(f"[{ordinal}/{stage_count}] {step.operation_code} (step_id={step.job_step_id}) - SKIPPED FOR FALLBACK")
                continue

            req_id = f"demo-job-{job.job_id}-step-{step.job_step_id}"
            print(f"[{ordinal}/{stage_count}] {step.operation_code} (step_id={step.job_step_id})")

            if auto_satisfy_material:
                # Auto-satisfy any deliveries required for this step
                from shared.services.material_feed_execution_service import MaterialFeedExecutionService
                from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator

                delivery_svc = MaterialDeliveryService(session)
                feed_svc = MaterialFeedExecutionService(session)
                feed_transport = FakeCellActionTransport([
                    FakeCellActionExchange(
                        CellTaskRawResult(status="SUCCEEDED", detail="Fake demo feed succeeded.", completed_json="[]"),
                        execution_delay_seconds=0,
                    )
                    for _ in range(stage_count)
                ])
                feed_coordinator = MaterialFeedExecutionCoordinator(
                    session, robot_cell_adapter=RobotCellActionAdapter(feed_transport)
                )

                for delivery in delivery_svc.get_required_deliveries_for_step(step.job_step_id):
                    if delivery.status is not MaterialDeliveryStatus.COMPLETED:
                        if delivery.status is MaterialDeliveryStatus.PENDING:
                            delivery_svc.start_delivery(delivery.job_delivery_id)
                        delivery_svc.complete_delivery(delivery.job_delivery_id)

                        feed = feed_svc.get_for_delivery(delivery.job_delivery_id)
                        if feed:
                            feed_result = feed_coordinator.execute_feed(
                                feed_execution_id=feed.feed_execution_id,
                                req_id=f"demo-feed-{feed.feed_execution_id}",
                                parts_json='[{"slot":"SYNTHETIC","class":"base"}]'
                            )
                            print(f"  Feed execution outcome: {feed_result.outcome}")
                            if feed_result.detail:
                                print(f"  Feed error: {feed_result.detail}")
                        else:
                            print("  Policy Delivery has no MATERIAL_FEED execution.")

            session.flush()

            # Print readiness BEFORE dispatch to see if we satisfied it
            pre_ready = coordinator._step_readiness.evaluate(job_id=job.job_id, job_step_id=step.job_step_id)
            print(f"  Pre-dispatch readiness: {pre_ready.ready} (Reason: {pre_ready.reason})")

            print("  Dispatching Fake Robot Cell Action...")
            result = coordinator.execute_step(
                job_id=job.job_id,
                job_step_id=step.job_step_id,
                req_id=req_id,
                parts_json=build_synthetic_demo_parts(operation_code=step.operation_code or "", step_order=step.step_order),
            )
            session.expire_all()
            stored_step = session.get(JobStep, step.job_step_id)
            stored_job = session.get(ProductionJob, job.job_id)
            if result.started:
                print("  Goal accepted -> RUNNING")
            print(f"  Outcome: {result.outcome}")
            print(f"  JobStep: {stored_step.status if stored_step is not None else 'MISSING'}")
            print(f"  Job: {stored_job.status if stored_job is not None else 'MISSING'}")
            if result.outcome is not CoordinatorOutcome.COMPLETED:
                stopped_outcome = result.outcome
                print("  Demo stopped; no automatic retry or state rollback is performed.")
                break
            print("  SUCCESS -> COMPLETED")
            if delay_seconds and ordinal < stage_count:
                # All lifecycle writes have committed through Coordinator. End this
                # read transaction before the presentation-only inter-step delay.
                session.rollback()
                print(f"  Waiting {delay_seconds:g}s before the next dispatch...")
                sleep(delay_seconds)
    except KeyboardInterrupt:
        session.expire_all()
        stored_job = session.get(ProductionJob, job.job_id)
        print("\nDemo interrupted. No automatic rollback was performed.")
        print(f"Job {job.job_id} currently {stored_job.status if stored_job is not None else 'MISSING'}.")
        raise

    session.expire_all()
    stored_job = session.get(ProductionJob, job.job_id)
    inspection = session.scalar(
        select(ProductionInspection).where(ProductionInspection.production_job_id == job.job_id)
    )
    if stored_job is None:
        raise FakeProductionDemoError(f"Demo Job {job.job_id} disappeared during execution.")
    return FakeProductionDemoResult(
        job_id=job.job_id,
        product_code=product.product_code,
        assembly_recipe_id=recipe.recipe_id,
        assembly_recipe_version=recipe.version,
        recipe_stage_count=stage_count,
        action_commands=tuple(transport.commands),
        final_job_status=stored_job.status.value,
        inspection_status=inspection.status.value if inspection is not None else None,
        stopped_outcome=stopped_outcome,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setattr(sys.modules["__main__"], "fallback_4_slots", args.fallback_4_slots)
    print("=== Fake Production Demo ===")
    print("Transport: FakeCellActionTransport")
    print("Real robot commands: DISABLED")
    print("Demo parts_json: synthetic valid JSON array (not Production BOM or Robot Cell master data)")

    session_factory = get_session_factory()
    try:
        with session_factory() as session:
            result = run_fake_production_demo(
                session,
                product_code=args.product,
                delay_seconds=args.delay,
            )
    except KeyboardInterrupt:
        return 130
    except FakeProductionDemoError as exc:
        print(f"Demo error: {exc}", file=sys.stderr)
        return 1

    print("\n=== Fake Production Demo Result ===")
    print(f"Job status: {result.final_job_status}")
    print(f"PRE_ROOF inspection: {result.inspection_status or '-'}")
    print(f"Actions sent: {len(result.action_commands)}")
    if result.stopped_outcome is not None:
        print(f"Stopped outcome: {result.stopped_outcome}")
        return 1
    print("Assembly demo complete.")
    print("Open: http://localhost:8000/production/monitor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
