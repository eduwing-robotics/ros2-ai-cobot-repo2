#!/usr/bin/env python3
"""Human-only guarded helper for one Incoming QA HTTP integration exchange.

Every request creates a uniquely prefixed disposable fixture in
smart_factory_benchmark. It never selects an arbitrary pre-existing delivery
item. Cleanup is allowed only by the resulting inspection_request_id and only
after the transaction is terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from fms_server.incoming_material_qa_http_client import IncomingMaterialQAAcknowledgement
from shared.config import get_settings
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialInspection,
    MaterialInspectionStatus,
    MaterialDeliveryStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
)
from shared.services.material_inspection_service import MaterialInspectionService

_EXPECTED_TEST_DB = "smart_factory_benchmark"
_FIXTURE_PREFIX = "INCOMING_QA_E2E_"


@dataclass(frozen=True)
class FixtureRef:
    product_code: str
    part_code: str
    job_id: int
    job_step_id: int
    delivery_id: int
    delivery_item_id: int


def _test_url() -> str:
    settings = get_settings()
    url = settings.postgres_test_database_url.strip()
    if not url or url == settings.database_url.strip():
        raise SystemExit("Refusing: POSTGRES_TEST_DATABASE_URL must be configured and differ from DATABASE_URL.")
    if urlparse(url).path.rsplit("/", 1)[-1] != _EXPECTED_TEST_DB:
        raise SystemExit(f"Refusing: target database must be {_EXPECTED_TEST_DB!r}.")
    if settings.cell_transport != "fake":
        raise SystemExit("Refusing: CELL_TRANSPORT must be fake for this guarded helper.")
    return url


def _create_disposable_fixture(session: Session) -> FixtureRef:
    suffix = uuid.uuid4().hex[:14].upper()
    product_code = f"{_FIXTURE_PREFIX}PRODUCT_{suffix}"
    part_code = f"{_FIXTURE_PREFIX}PART_{suffix}"
    job_code = f"{_FIXTURE_PREFIX}JOB_{suffix}"
    delivery_code = f"{_FIXTURE_PREFIX}DELIVERY_{suffix}"

    product = Product(product_code=product_code, product_name="Incoming QA disposable E2E fixture")
    part = Part(
        part_code=part_code,
        part_name="Incoming QA disposable wall part",
        category=PartCategory.STRUCTURE,
        unit="EA",
        vision_class="wall_ext_back",
    )
    job = ProductionJob(job_code=job_code, product_code=product_code, status=JobStatus.REQUESTED)
    session.add_all((product, part, job))
    session.flush()
    step = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="INCOMING_QA_E2E",
        display_name="Incoming QA disposable fixture",
        status=StepStatus.PENDING,
    )
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code=delivery_code,
        display_name="Incoming QA disposable fixture",
        status=MaterialDeliveryStatus.PENDING,
    )
    session.add_all((step, delivery))
    session.flush()
    item = JobMaterialDeliveryItem(
        job_delivery_id=delivery.job_delivery_id,
        job_step_id=step.job_step_id,
        part_code=part.part_code,
        quantity=1,
    )
    session.add(item)
    session.commit()
    return FixtureRef(
        product_code=product.product_code,
        part_code=part.part_code,
        job_id=job.job_id,
        job_step_id=step.job_step_id,
        delivery_id=delivery.job_delivery_id,
        delivery_item_id=item.delivery_item_id,
    )


def _fixture_from_inspection(session: Session, inspection: MaterialInspection) -> FixtureRef:
    item = session.get(JobMaterialDeliveryItem, inspection.delivery_item_id)
    if item is None:
        raise SystemExit("Refusing cleanup: inspection delivery item is missing.")
    delivery = session.get(JobMaterialDelivery, item.job_delivery_id)
    step = session.get(JobStep, item.job_step_id)
    if delivery is None or step is None:
        raise SystemExit("Refusing cleanup: fixture delivery or step is missing.")
    job = session.get(ProductionJob, delivery.production_job_id)
    part = session.get(Part, item.part_code)
    if job is None or part is None:
        raise SystemExit("Refusing cleanup: fixture job or part is missing.")
    if not (
        job.job_code.startswith(_FIXTURE_PREFIX)
        and delivery.delivery_code.startswith(_FIXTURE_PREFIX)
        and part.part_code.startswith(_FIXTURE_PREFIX)
    ):
        raise SystemExit("Refusing cleanup: request does not own an Incoming QA disposable fixture.")
    return FixtureRef(
        product_code=job.product_code,
        part_code=part.part_code,
        job_id=job.job_id,
        job_step_id=step.job_step_id,
        delivery_id=delivery.job_delivery_id,
        delivery_item_id=item.delivery_item_id,
    )


def _resend_existing(
    session: Session,
    *,
    inspection_request_id: str,
    runtime: IncomingMaterialQARuntime | None = None,
) -> tuple[IncomingMaterialQAAcknowledgement, MaterialInspection, FixtureRef, dict[str, object]]:
    """Retransmit one owned non-terminal transaction without allocating anything."""
    inspection = session.scalar(
        select(MaterialInspection).where(
            MaterialInspection.inspection_request_id == inspection_request_id
        )
    )
    if inspection is None:
        raise SystemExit("Refusing resend: inspection request not found.")
    fixture = _fixture_from_inspection(session, inspection)
    if inspection.status in {MaterialInspectionStatus.COMPLETED, MaterialInspectionStatus.ERROR}:
        raise SystemExit("Refusing resend: terminal inspection transactions cannot be retransmitted.")

    # Capture exactly the persisted snapshot before the network call so the CLI
    # output can be compared directly with Vision's original request capture.
    outbound_payload = MaterialInspectionService.request_from_inspection(inspection).model_dump(mode="json")
    acknowledgement = (runtime or IncomingMaterialQARuntime()).send_existing(
        session, inspection_request_id=inspection_request_id
    )
    session.refresh(inspection)
    return acknowledgement, inspection, fixture, outbound_payload


def _cleanup_fixture(session: Session, *, inspection_request_id: str) -> FixtureRef:
    inspection = session.scalar(
        select(MaterialInspection).where(
            MaterialInspection.inspection_request_id == inspection_request_id
        )
    )
    if inspection is None:
        raise SystemExit("Refusing cleanup: inspection request not found.")
    if inspection.status not in {MaterialInspectionStatus.COMPLETED, MaterialInspectionStatus.ERROR}:
        raise SystemExit("Refusing cleanup: callback has not reached a terminal state.")
    fixture = _fixture_from_inspection(session, inspection)

    session.execute(delete(MaterialInspection).where(MaterialInspection.delivery_item_id == fixture.delivery_item_id))
    session.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.delivery_item_id == fixture.delivery_item_id))
    session.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id == fixture.delivery_id))
    session.execute(delete(JobStep).where(JobStep.job_step_id == fixture.job_step_id))
    session.execute(delete(ProductionJob).where(ProductionJob.job_id == fixture.job_id))
    session.execute(delete(Part).where(Part.part_code == fixture.part_code))
    session.execute(delete(Product).where(Product.product_code == fixture.product_code))
    session.commit()
    return fixture


def _row_payload(row: MaterialInspection) -> dict[str, object]:
    return {
        "inspection_request_id": row.inspection_request_id,
        "delivery_item_id": row.delivery_item_id,
        "inspection_cycle": row.inspection_cycle,
        "status": getattr(row.status, "value", row.status),
        "result": getattr(row.result, "value", row.result),
        "expected_part_code": row.expected_part_code,
        "expected_class_name": row.expected_class_name,
        "expected_quantity": row.expected_quantity,
        "production_valid": row.production_valid,
        "release_allowed": MaterialInspectionService.is_release_allowed(row),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("request", "resend", "status", "cleanup"))
    parser.add_argument("--confirm-test-db", required=True)
    parser.add_argument("--inspection-request-id")
    args = parser.parse_args()
    if args.confirm_test_db != _EXPECTED_TEST_DB:
        raise SystemExit(f"Refusing: pass --confirm-test-db {_EXPECTED_TEST_DB} exactly.")
    if args.command in {"resend", "status", "cleanup"} and not args.inspection_request_id:
        raise SystemExit(f"{args.command} requires --inspection-request-id.")

    engine = create_engine(_test_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            if connection.execute(text("select current_database()")).scalar_one() != _EXPECTED_TEST_DB:
                raise SystemExit("Refusing: connected database is not smart_factory_benchmark.")
        with Session(engine) as session:
            if args.command == "request":
                fixture = _create_disposable_fixture(session)
                try:
                    acknowledgement = IncomingMaterialQARuntime().create_and_send(
                        session, delivery_item_id=fixture.delivery_item_id
                    )
                except Exception:
                    # The transaction is intentionally retained in ERROR/HOLD so
                    # the operator can inspect it and run cleanup by request ID.
                    row = session.scalar(
                        select(MaterialInspection)
                        .where(MaterialInspection.delivery_item_id == fixture.delivery_item_id)
                        .order_by(MaterialInspection.inspection_cycle.desc())
                    )
                    if row is not None:
                        print(json.dumps({"fixture": asdict(fixture), **_row_payload(row)}, ensure_ascii=False))
                    raise
                row = session.scalar(
                    select(MaterialInspection)
                    .where(MaterialInspection.delivery_item_id == fixture.delivery_item_id)
                    .order_by(MaterialInspection.inspection_cycle.desc())
                )
                assert row is not None
                print(json.dumps({"ack_status": acknowledgement.status_code, "fixture": asdict(fixture), **_row_payload(row)}, ensure_ascii=False))
            elif args.command == "resend":
                acknowledgement, row, fixture, outbound_payload = _resend_existing(
                    session, inspection_request_id=args.inspection_request_id
                )
                print(json.dumps({
                    "ack_status": acknowledgement.status_code,
                    "new_fixture_created": False,
                    "new_inspection_created": False,
                    "outbound_payload": outbound_payload,
                    "fixture": asdict(fixture),
                    **_row_payload(row),
                }, ensure_ascii=False))
            elif args.command == "status":
                row = session.scalar(
                    select(MaterialInspection).where(
                        MaterialInspection.inspection_request_id == args.inspection_request_id
                    )
                )
                if row is None:
                    raise SystemExit("Inspection request not found in smart_factory_benchmark.")
                print(json.dumps({**_row_payload(row), "fixture": asdict(_fixture_from_inspection(session, row))}, ensure_ascii=False))
            else:
                fixture = _cleanup_fixture(session, inspection_request_id=args.inspection_request_id)
                print(json.dumps({"cleaned": True, "fixture": asdict(fixture)}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
