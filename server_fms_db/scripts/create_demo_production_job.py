"""Create a benchmark-only ProductionJob for Unity status projection checks.

This tool deliberately creates only a durable ``REQUESTED`` job. It does not
start an FMS worker, dispatch a step, manufacture Incoming-QA evidence, or
contact Robot Cell, TurtleBot, Vision, or any other external system.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import create_engine, delete, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from api_server.services.production_snapshot_service import ProductionSnapshotService
from shared.config import get_settings
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    ExecutionAttempt,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStep,
    MaterialInspection,
    ProductionEvent,
    ProductionInspection,
    ProductionInspectionResult,
    ProductionJob,
    RoofOptionCode,
)
from shared.services.production_orchestration_service import ProductionOrchestrationService

EXPECTED_DATABASE = "smart_factory_benchmark"
DEMO_JOB_PREFIX = "DEMO_"


class DemoDatabaseSafetyError(RuntimeError):
    """Raised before a demo write could target a non-benchmark database."""


class UnsupportedDemoRoofOptionError(ValueError):
    """Raised when a product has explicit options but not the requested one."""


def benchmark_session_factory() -> sessionmaker[Session]:
    """Use only POSTGRES_TEST_DATABASE_URL after an authoritative DB-name check."""
    settings = get_settings()
    benchmark_url = settings.postgres_test_database_url.strip()
    production_url = settings.database_url.strip()
    if not benchmark_url:
        raise DemoDatabaseSafetyError(
            "POSTGRES_TEST_DATABASE_URL is required; demo jobs never fall back to DATABASE_URL."
        )
    if production_url and benchmark_url == production_url:
        raise DemoDatabaseSafetyError(
            "POSTGRES_TEST_DATABASE_URL must be distinct from DATABASE_URL."
        )

    engine = create_engine(benchmark_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            database = connection.scalar(text("SELECT current_database()"))
        if database != EXPECTED_DATABASE:
            raise DemoDatabaseSafetyError(
                f"Refusing demo writes: connected database is {database!r}, "
                f"expected {EXPECTED_DATABASE!r}."
            )
    except Exception:
        engine.dispose()
        raise
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _supported_explicit_options(session: Session, *, product_code: str) -> set[str]:
    """Return option codes explicitly configured by the active recipe."""
    recipe = session.scalar(
        select(AssemblyRecipe).where(
            AssemblyRecipe.product_code == product_code,
            AssemblyRecipe.is_active.is_(True),
        )
    )
    if recipe is None:
        return set()
    return {
        option_code
        for option_code in session.scalars(
            select(AssemblyRecipeStage.option_code).where(
                AssemblyRecipeStage.recipe_id == recipe.recipe_id,
                AssemblyRecipeStage.option_code.is_not(None),
            )
        )
        if option_code is not None
    }


def _validate_demo_roof_option(
    session: Session,
    *,
    product_code: str,
    roof_option_code: RoofOptionCode | None,
) -> None:
    """Give an actionable option error without relaxing core validation."""
    explicit_options = _supported_explicit_options(session, product_code=product_code)
    if roof_option_code is not None and explicit_options and roof_option_code.value not in explicit_options:
        supported = ", ".join(sorted(explicit_options))
        raise UnsupportedDemoRoofOptionError(
            f"{product_code} does not support {roof_option_code.value}; "
            f"configured option(s): {supported}."
        )


def cleanup_demo_jobs(session: Session) -> int:
    """Delete only script-owned demo jobs in current FK dependency order."""
    job_ids = list(session.scalars(
        select(ProductionJob.job_id).where(ProductionJob.job_code.like(f"{DEMO_JOB_PREFIX}%"))
    ))
    if not job_ids:
        print("No demo jobs found.")
        return 0

    delivery_ids = list(session.scalars(select(JobMaterialDelivery.job_delivery_id).where(
        JobMaterialDelivery.production_job_id.in_(job_ids)
    )))
    step_ids = list(session.scalars(select(JobStep.job_step_id).where(JobStep.job_id.in_(job_ids))))
    delivery_item_ids = list(session.scalars(select(JobMaterialDeliveryItem.delivery_item_id).where(
        JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
    ))) if delivery_ids else []
    inspection_ids = list(session.scalars(select(ProductionInspection.inspection_id).where(
        ProductionInspection.production_job_id.in_(job_ids)
    )))

    print(f"Found {len(job_ids)} demo job(s). Cleaning up benchmark records...")
    attempt_scope = [ExecutionAttempt.job_id.in_(job_ids)]
    if step_ids:
        attempt_scope.append(ExecutionAttempt.job_step_id.in_(step_ids))
    if delivery_ids:
        attempt_scope.append(ExecutionAttempt.job_delivery_id.in_(delivery_ids))
    session.execute(delete(ExecutionAttempt).where(or_(*attempt_scope)))
    if delivery_item_ids:
        session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id.in_(delivery_item_ids)))
    if delivery_ids:
        session.execute(delete(JobMaterialFeedExecution).where(
            JobMaterialFeedExecution.job_delivery_id.in_(delivery_ids)
        ))
        session.execute(delete(JobMaterialDeliveryItem).where(
            JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids)
        ))
    if inspection_ids:
        session.execute(delete(ProductionInspectionResult).where(
            ProductionInspectionResult.inspection_id.in_(inspection_ids)
        ))
    session.execute(delete(ProductionInspection).where(ProductionInspection.production_job_id.in_(job_ids)))
    session.execute(delete(ProductionEvent).where(ProductionEvent.job_id.in_(job_ids)))
    if step_ids:
        session.execute(delete(JobStep).where(JobStep.job_step_id.in_(step_ids)))
    if delivery_ids:
        session.execute(delete(JobMaterialDelivery).where(
            JobMaterialDelivery.job_delivery_id.in_(delivery_ids)
        ))
    session.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids)))
    session.commit()
    print("Benchmark demo cleanup complete.")
    return len(job_ids)


def _default_roof_option(product_code: str) -> RoofOptionCode | None:
    """HOUSE_B's active official master currently supports ROOF_02."""
    return RoofOptionCode.ROOF_02 if product_code == "HOUSE_B" else None


def create_status_only_demo_job(
    session: Session,
    *,
    product_code: str,
    job_code: str,
    roof_option_code: RoofOptionCode | None,
) -> ProductionJob:
    """Create the official durable job seed and intentionally stop there."""
    _validate_demo_roof_option(
        session,
        product_code=product_code,
        roof_option_code=roof_option_code,
    )
    return ProductionOrchestrationService(session).create_job(
        product_code=product_code,
        job_code=job_code,
        roof_option_code=roof_option_code,
    )


def print_job_status(job: ProductionJob, *, session: Session) -> None:
    """Print immutable JobStep snapshots, not legacy ProcessStep relations."""
    session.refresh(job)
    steps = list(session.scalars(select(JobStep).where(
        JobStep.job_id == job.job_id
    ).order_by(JobStep.step_order, JobStep.job_step_id)))
    print(f"job_id={job.job_id}")
    print(f"job_code={job.job_code}")
    print(f"product_code={job.product_code}")
    print(f"status={job.status.value}")
    print(f"roof_option_code={job.roof_option_code.value if job.roof_option_code else 'NONE'}")
    print("steps:")
    for step in steps:
        print(
            f"  order={step.step_order} operation={step.operation_code} "
            f"status={step.status.value} terminal={step.is_terminal} "
            f"source_recipe_stage_id={step.source_recipe_stage_id}"
        )


def _print_unity_projection(factory: sessionmaker[Session], *, job_id: int) -> None:
    """Read the production_status projection without a WebSocket or network call."""
    projection = ProductionSnapshotService(factory).get_job_status(job_id)
    if projection is None:
        raise RuntimeError(f"Production snapshot did not return newly created job_id={job_id}.")
    print(
        "unity_production_status="
        f"job_id={projection['job_id']} status={projection['status']} "
        f"current_stage_code={projection['current_stage_code']}"
    )


def _new_job_code(product_code: str) -> str:
    return f"{DEMO_JOB_PREFIX}{product_code}_STATUS_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or clean benchmark-only status-only Unity demo ProductionJobs."
    )
    parser.add_argument("--product", default="HOUSE_B", help="Product code, e.g. HOUSE_A or HOUSE_B.")
    parser.add_argument(
        "--roof-option",
        choices=[option.value for option in RoofOptionCode],
        help="Explicit recipe option. HOUSE_B defaults to its supported ROOF_02.",
    )
    parser.add_argument(
        "--scenario",
        choices=["created"],
        default="created",
        help="Only safe status-only creation is supported; no production step is dispatched.",
    )
    parser.add_argument("--job-code", help=f"Optional unique code; use {DEMO_JOB_PREFIX} prefix for --cleanup support.")
    parser.add_argument("--cleanup", action="store_true", help=f"Delete only {DEMO_JOB_PREFIX}* jobs from benchmark.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    product_code = args.product.strip()
    if not product_code:
        print("--product must be non-empty.", file=sys.stderr)
        return 2
    factory: sessionmaker[Session] | None = None
    try:
        factory = benchmark_session_factory()
        with factory() as session:
            if args.cleanup:
                cleanup_demo_jobs(session)
                return 0
            roof_option_code = (
                RoofOptionCode(args.roof_option)
                if args.roof_option is not None
                else _default_roof_option(product_code)
            )
            job_code = args.job_code.strip() if args.job_code else _new_job_code(product_code)
            if not job_code:
                print("--job-code must be non-empty.", file=sys.stderr)
                return 2
            job = create_status_only_demo_job(
                session,
                product_code=product_code,
                job_code=job_code,
                roof_option_code=roof_option_code,
            )
            print_job_status(job, session=session)
        _print_unity_projection(factory, job_id=job.job_id)
        return 0
    except (DemoDatabaseSafetyError, UnsupportedDemoRoofOptionError, RuntimeError) as exc:
        print(f"Demo job not created: {exc}", file=sys.stderr)
        return 1
    finally:
        if factory is not None:
            factory.kw["bind"].dispose()


if __name__ == "__main__":
    raise SystemExit(main())
