"""Strict read-only benchmark/demo readiness preflight."""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from shared.config import Settings, get_settings
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    ExecutionAttempt,
    ExecutionAttemptStatus,
    IncomingQATransaction,
    IncomingQATransactionStatus,
    Inventory,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionStatus,
    ProductionJob,
)
from shared.services.drop_resource_service import DropResourceService, DropResourceState

BENCHMARK_DATABASE = "smart_factory_benchmark"
TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED})
ACTIVE_ATTEMPT_STATUSES = frozenset({
    ExecutionAttemptStatus.CREATED,
    ExecutionAttemptStatus.DISPATCHING,
    ExecutionAttemptStatus.ACCEPTED,
    ExecutionAttemptStatus.UNKNOWN,
})
ACTIVE_QA_STATUSES = frozenset({IncomingQATransactionStatus.SENT, IncomingQATransactionStatus.ACKED})
EXPECTED_HOUSE_B_GROUPS = ("BASE", "OUTER_WALLS", "OUTER_WALLS", "OUTER_WALLS", "OUTER_WALLS", "INNER_WALL", "ROOF")


@dataclass(frozen=True)
class Check:
    passed: bool
    details: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightReport:
    checks: dict[str, Check]
    warnings: list[str]

    @property
    def demo_ready(self) -> bool:
        return all(check.passed for check in self.checks.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "demo_ready": self.demo_ready,
            "checks": {name: asdict(check) for name, check in self.checks.items()},
            "warnings": self.warnings,
        }


def source_head() -> str:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    return ScriptDirectory.from_config(config).get_current_head()


def database_identity_check(*, url: URL | None, actual_database: str | None = None) -> Check:
    if url is None or not url.drivername.startswith("postgresql") or url.database != BENCHMARK_DATABASE:
        return Check(False, ["POSTGRES_TEST_DATABASE_URL must target smart_factory_benchmark."], {
            "parsed_database": url.database if url else None,
        })
    if actual_database is not None and actual_database != BENCHMARK_DATABASE:
        return Check(False, ["current_database() is not smart_factory_benchmark."], {
            "host": url.host, "port": url.port, "parsed_database": url.database,
            "actual_database": actual_database,
        })
    return Check(True, [f"{url.host}:{url.port} / {url.database}"], {
        "host": url.host, "port": url.port, "database": url.database,
    })


def _valid_host(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        # Hostnames are legitimate deployment configuration, but whitespace and
        # URL-shaped inputs are not host values.
        return all(char.isalnum() or char in ".-" for char in candidate)


def _valid_port(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def config_check(settings: Settings | Any) -> tuple[Check, list[str]]:
    fields = (
        ("VISION_INCOMING_QA_UDP_HOST", "vision_incoming_qa_udp_host", _valid_host),
        ("VISION_INCOMING_QA_UDP_PORT", "vision_incoming_qa_udp_port", _valid_port),
        ("FMS_INCOMING_QA_RESULT_UDP_HOST", "fms_incoming_qa_result_udp_host", _valid_host),
        ("FMS_INCOMING_QA_RESULT_UDP_PORT", "fms_incoming_qa_result_udp_port", _valid_port),
        ("INCOMING_QA_UDP_ACK_TIMEOUT_SECONDS", "incoming_qa_udp_ack_timeout_seconds", lambda v: isinstance(v, (int, float)) and v > 0),
        ("INCOMING_QA_UDP_MAX_RETRIES", "incoming_qa_udp_max_retries", lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0),
        ("VISION_PRE_ROOF_UDP_HOST", "vision_pre_roof_udp_host", _valid_host),
        ("VISION_PRE_ROOF_UDP_PORT", "vision_pre_roof_udp_port", _valid_port),
        ("FMS_PRE_ROOF_RESULT_UDP_HOST", "fms_pre_roof_result_udp_host", _valid_host),
        ("FMS_PRE_ROOF_RESULT_UDP_PORT", "fms_pre_roof_result_udp_port", _valid_port),
    )
    invalid = [label for label, attr, validator in fields if not validator(getattr(settings, attr, None))]
    mode = getattr(settings, "cell_transport", None)
    prefetch = getattr(settings, "material_prefetch_mode", None)
    if not isinstance(mode, str) or not mode.strip():
        invalid.append("CELL_TRANSPORT")
    if prefetch is None or not str(getattr(prefetch, "value", prefetch)).strip():
        invalid.append("MATERIAL_PREFETCH_MODE")
    warnings: list[str] = []
    if getattr(settings, "test_override_enabled", False):
        warnings.append("WARNING: TEST_OVERRIDE_ENABLED=true")
    warnings.append(f"CELL_TRANSPORT={mode}")
    warnings.append(f"MATERIAL_PREFETCH_MODE={getattr(prefetch, 'value', prefetch)}")
    if invalid:
        return Check(False, [f"missing or malformed: {', '.join(invalid)}"], {"invalid": invalid}), warnings
    return Check(True, ["critical demo integration settings are syntactically valid"], {
        "cell_transport": mode,
        "test_override_enabled": bool(getattr(settings, "test_override_enabled", False)),
        "material_prefetch_mode": str(getattr(prefetch, "value", prefetch)),
    }), warnings


def _check_jobs(session: Session) -> Check:
    jobs = list(session.scalars(select(ProductionJob).where(ProductionJob.status.not_in(TERMINAL_JOB_STATUSES))))
    if not jobs:
        return Check(True, ["no non-terminal production jobs"])
    details = [f"job_id={job.job_id} status={job.status.value} control_state={job.control_state.value}" for job in jobs]
    return Check(False, details, {"job_ids": [job.job_id for job in jobs]})


def _check_incoming_qa(session: Session) -> Check:
    transactions = list(session.execute(
        select(IncomingQATransaction, ProductionJob)
        .join(ProductionJob, ProductionJob.job_id == IncomingQATransaction.production_job_id)
        .where(
            IncomingQATransaction.status.in_(ACTIVE_QA_STATUSES),
            ProductionJob.status.not_in(TERMINAL_JOB_STATUSES),
        )
    ).all())
    legacy = list(session.execute(
        select(MaterialInspection, ProductionJob)
        .join(JobMaterialDeliveryItem, JobMaterialDeliveryItem.delivery_item_id == MaterialInspection.delivery_item_id)
        .join(JobMaterialDelivery, JobMaterialDelivery.job_delivery_id == JobMaterialDeliveryItem.job_delivery_id)
        .join(ProductionJob, ProductionJob.job_id == JobMaterialDelivery.production_job_id)
        .where(
            MaterialInspection.incoming_qa_transaction_id.is_(None),
            MaterialInspection.status.in_((MaterialInspectionStatus.REQUESTED, MaterialInspectionStatus.RUNNING)),
            ProductionJob.status.not_in(TERMINAL_JOB_STATUSES),
        )
    ).all())
    if not transactions and not legacy:
        return Check(True, ["no globally blocking Incoming QA transaction or legacy inspection"])
    details = [
        f"transaction_id={tx.transaction_id} job_id={job.job_id} status={tx.status.value} request_id={tx.inspection_request_id}"
        for tx, job in transactions
    ] + [
        f"legacy_inspection_id={inspection.inspection_id} job_id={job.job_id} status={inspection.status.value}"
        for inspection, job in legacy
    ]
    return Check(False, details)


def _check_drop(session: Session) -> Check:
    snapshot = DropResourceService(session).get_drop_state()
    if snapshot.state is DropResourceState.FREE and snapshot.owner_delivery_id is None:
        return Check(True, ["FREE"])
    return Check(False, [f"state={snapshot.state.value}", f"owner_delivery_id={snapshot.owner_delivery_id}"], {
        "state": snapshot.state.value,
        "owner_delivery_id": snapshot.owner_delivery_id,
        "owner_attempt_id": snapshot.owner_attempt_id,
    })


def _check_attempts(session: Session) -> Check:
    attempts = list(session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))))
    if not attempts:
        return Check(True, ["no active execution attempts"])
    return Check(False, [
        f"attempt_id={attempt.attempt_id} executor={attempt.executor_type.value} command={attempt.command_type} status={attempt.status.value} job_id={attempt.job_id}"
        for attempt in attempts
    ])


def _active_house_b_recipe(session: Session) -> tuple[AssemblyRecipe | None, list[AssemblyRecipeStage]]:
    recipes = list(session.scalars(select(AssemblyRecipe).where(
        AssemblyRecipe.product_code == "HOUSE_B", AssemblyRecipe.is_active.is_(True)
    )))
    if len(recipes) != 1:
        return None, []
    recipe = recipes[0]
    stages = list(session.scalars(select(AssemblyRecipeStage).where(
        AssemblyRecipeStage.recipe_id == recipe.recipe_id
    ).order_by(AssemblyRecipeStage.stage_order)))
    return recipe, stages


def recipe_check(session: Session) -> tuple[Check, list[AssemblyRecipeStage]]:
    recipe, stages = _active_house_b_recipe(session)
    if recipe is None:
        return Check(False, ["HOUSE_B must have exactly one active recipe."]), []
    groups = tuple(stage.supply_group_code for stage in stages)
    if groups != EXPECTED_HOUSE_B_GROUPS:
        return Check(False, [f"recipe_id={recipe.recipe_id} version={recipe.version}", f"groups={list(groups)}"]), stages
    return Check(True, [f"HOUSE_B recipe_id={recipe.recipe_id} version={recipe.version}", "BASE → OUTER×4 → INNER → ROOF"]), stages


def inventory_check(session: Session, stages: list[AssemblyRecipeStage]) -> Check:
    requirements: dict[str, int] = {}
    for stage in stages:
        if stage.part_code:
            requirements[stage.part_code] = requirements.get(stage.part_code, 0) + (stage.quantity or 0)
    if not requirements:
        return Check(False, ["active HOUSE_B recipe has no material requirements"])
    rows: list[dict[str, Any]] = []
    for part_code, required in sorted(requirements.items()):
        inventory = session.get(Inventory, part_code)
        quantity = inventory.quantity if inventory else 0
        reserved = inventory.reserved_quantity if inventory else 0
        available = quantity - reserved
        rows.append({"part_code": part_code, "required": required, "available": available, "reserved": reserved, "sufficient": available >= required})
    insufficient = [row for row in rows if not row["sufficient"]]
    if insufficient:
        return Check(False, [f"insufficient: {row['part_code']} required={row['required']} available={row['available']}" for row in insufficient], {"parts": rows})
    return Check(True, ["sufficient for 1 HOUSE_B"], {"parts": rows})


def _collect_verified_session(session: Session, *, head: str, settings: Settings | Any) -> tuple[dict[str, Check], list[str]]:
    checks: dict[str, Check] = {}
    current = session.scalar(text("SELECT version_num FROM alembic_version"))
    checks["migration"] = Check(current == head, [f"db={current}", f"source={head}"], {"database_revision": current, "source_head": head})
    checks["active_job"] = _check_jobs(session)
    checks["incoming_qa"] = _check_incoming_qa(session)
    checks["drop"] = _check_drop(session)
    checks["attempts"] = _check_attempts(session)
    checks["recipe"], stages = recipe_check(session)
    checks["inventory"] = inventory_check(session, stages) if checks["recipe"].passed else Check(False, ["not evaluated because recipe validation failed"])
    configuration, warnings = config_check(settings)
    checks["config"] = configuration
    return checks, warnings


def run_preflight(
    settings: Settings | Any | None = None,
    *,
    head_resolver: Callable[[], str] = source_head,
) -> PreflightReport:
    settings = settings or get_settings()
    try:
        raw_url = str(getattr(settings, "postgres_test_database_url", "")).strip()
        url = make_url(raw_url) if raw_url else None
    except Exception:
        url = None
    database = database_identity_check(url=url)
    checks: dict[str, Check] = {"database": database}
    if not database.passed or url is None:
        config, warnings = config_check(settings)
        checks["config"] = config
        return PreflightReport(checks=checks, warnings=warnings)

    engine = create_engine(url, pool_pre_ping=False)
    try:
        with engine.connect() as connection:
            actual = connection.scalar(text("SELECT current_database()"))
            database = database_identity_check(url=url, actual_database=actual)
            checks["database"] = database
            if not database.passed:
                config, warnings = config_check(settings)
                checks["config"] = config
                return PreflightReport(checks=checks, warnings=warnings)
            # The identity query starts a transaction with psycopg. End it before
            # opening the only transaction used for all preflight SELECTs.
            connection.rollback()
            connection.exec_driver_sql("BEGIN READ ONLY")
            with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                collected, warnings = _collect_verified_session(session, head=head_resolver(), settings=settings)
                checks.update(collected)
            connection.exec_driver_sql("ROLLBACK")
            return PreflightReport(checks=checks, warnings=warnings)
    finally:
        engine.dispose()


def _print_human(report: PreflightReport, *, verbose: bool) -> None:
    labels = (
        ("database", "DATABASE"), ("migration", "MIGRATION"), ("active_job", "ACTIVE JOB"),
        ("incoming_qa", "INCOMING QA"), ("drop", "DROP"), ("attempts", "ATTEMPTS"),
        ("recipe", "RECIPE"), ("inventory", "INVENTORY"), ("config", "CONFIG"),
    )
    for key, label in labels:
        check = report.checks.get(key)
        if check is None:
            print(f"{label:<20} FAIL")
            print("  not evaluated")
            continue
        print(f"{label:<20} {'PASS' if check.passed else 'FAIL'}")
        for detail in check.details:
            print(f"  {detail}")
        if verbose and key == "inventory":
            for row in check.data.get("parts", []):
                print(f"  {row['part_code']}: required={row['required']} available={row['available']} reserved={row['reserved']} sufficient={row['sufficient']}")
    for warning in report.warnings:
        print(warning)
    print("--------------------------------")
    print(f"DEMO READY          {'YES' if report.demo_ready else 'NO'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_preflight()
    except Exception as exc:
        report = PreflightReport(checks={"database": Check(False, [f"preflight error: {exc}"])}, warnings=[])
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, default=str))
    else:
        _print_human(report, verbose=args.verbose)
    return 0 if report.demo_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
