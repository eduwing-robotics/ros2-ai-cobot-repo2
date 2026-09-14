from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.inventory import get_db
from shared.models import Base
from shared.models.factory import (
    IncomingQATransaction,
    IncomingQATransactionStatus,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    MaterialInspection,
    MaterialInspectionFailureType,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
)
from shared.schemas.vision import IncomingQARequestItemV02, IncomingQARequestV02
from shared.services.incoming_qa_transaction_service import IncomingQATransactionService
from shared.vision_recipe_mapping import IncomingQAInspectionMode


@pytest.fixture(name="db_session")
def fixture_db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="client")
def fixture_client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _seed_job(session: Session, *, code: str) -> tuple[ProductionJob, dict[str, JobMaterialDeliveryItem]]:
    if session.get(Product, "QA_MONITOR") is None:
        session.add(Product(product_code="QA_MONITOR", product_name="QA monitor", is_active=True))
        session.flush()
    job = ProductionJob(job_code=code, product_code="QA_MONITOR", status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id, batch_order=1, delivery_code=f"{code}-DEL", display_name=code, status="PENDING"
    )
    session.add(delivery)
    session.flush()
    part_specs = (
        ("BASE", "base_house_b"),
        ("B01", "wall_ext_back_window"),
        ("B02", "wall_ext_door"),
    )
    for suffix, vision_class in part_specs:
        part_code = f"{code}-{suffix}"
        session.add(Part(part_code=part_code, part_name=part_code, category=PartCategory.STRUCTURE, vision_class=vision_class, unit="EA"))
    session.flush()
    items = {
        suffix: JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, part_code=f"{code}-{suffix}", quantity=1)
        for suffix, _vision_class in part_specs
    }
    session.add_all(items.values())
    session.flush()
    return job, items


def _request(*, request_id: str, mode: IncomingQAInspectionMode, cycle: int, items: list[tuple[str, JobMaterialDeliveryItem, str]]) -> IncomingQARequestV02:
    return IncomingQARequestV02(
        inspection_request_id=request_id,
        inspection_mode=mode,
        inspection_cycle=cycle,
        items=[
            IncomingQARequestItemV02(
                slot_id=slot,
                delivery_item_id=item.delivery_item_id,
                expected_part_code=item.part_code,
                expected_class_name=vision_class,
                expected_quantity=item.quantity,
            )
            for slot, item, vision_class in items
        ],
    )


def _create_transaction(session: Session, *, job: ProductionJob, request: IncomingQARequestV02) -> IncomingQATransaction:
    return IncomingQATransactionService().create_or_get(
        session, production_job_id=job.job_id, request=request
    ).transaction


def _inspection(session: Session, transaction: IncomingQATransaction, item: JobMaterialDeliveryItem) -> MaterialInspection:
    inspection = session.scalar(select(MaterialInspection).where(
        MaterialInspection.incoming_qa_transaction_id == transaction.transaction_id,
        MaterialInspection.delivery_item_id == item.delivery_item_id,
    ))
    assert inspection is not None
    return inspection


def test_v02_monitor_returns_empty_transactions_for_existing_job(client: TestClient, db_session: Session) -> None:
    job, _items = _seed_job(db_session, code="QA-MONITOR-EMPTY")
    db_session.commit()

    response = client.get(f"/production/jobs/{job.job_id}/incoming-qa/v02")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job.job_id,
        "gate": {"status": "HOLD", "total_expected_items": 3, "released_items": 0},
        "incoming_qa_test_hold": False,
        "can_enable_incoming_qa_test_hold": True,
        "can_disable_incoming_qa_test_hold": False,
        "can_advance_house_b": False,
        "can_release_incoming_qa_test_hold": False,
        "transactions": [],
    }


def test_v02_monitor_projects_pending_completed_and_deterministic_transactions(client: TestClient, db_session: Session) -> None:
    job, items = _seed_job(db_session, code="QA-MONITOR-ONE")
    other_job, other_items = _seed_job(db_session, code="QA-MONITOR-OTHER")
    base = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-BASE-1", mode=IncomingQAInspectionMode.BASE_AB, cycle=1,
        items=[("C09", items["BASE"], "base_house_b")],
    ))
    house = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-HOUSE-1", mode=IncomingQAInspectionMode.HOUSE_B, cycle=1,
        items=[("B01", items["B01"], "wall_ext_back_window"), ("B02", items["B02"], "wall_ext_door")],
    ))
    reinspect = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-B02-2", mode=IncomingQAInspectionMode.HOUSE_B, cycle=2,
        items=[("B02", items["B02"], "wall_ext_door")],
    ))
    other = _create_transaction(db_session, job=other_job, request=_request(
        request_id="REQ-OTHER-1", mode=IncomingQAInspectionMode.BASE_AB, cycle=1,
        items=[("C09", other_items["BASE"], "base_house_b")],
    ))
    now = datetime.now(timezone.utc)
    base.status = IncomingQATransactionStatus.COMPLETED
    base.overall_result = MaterialInspectionResult.PASS
    base.production_valid = True
    base.completed_at = now
    base_item = _inspection(db_session, base, items["BASE"])
    base_item.status = MaterialInspectionStatus.COMPLETED
    base_item.result = MaterialInspectionResult.PASS
    base_item.production_valid = True
    base_item.predicted_class_name = "base_house_b"
    base_item.material_confidence = 0.98
    base_item.completed_at = now
    house.status = IncomingQATransactionStatus.ACKED
    house.ack_accepted = True
    house.acked_at = now
    reinspect.status = IncomingQATransactionStatus.COMPLETED
    reinspect.overall_result = MaterialInspectionResult.FAIL
    reinspect.production_valid = False
    reinspect.completed_at = now
    b02 = _inspection(db_session, reinspect, items["B02"])
    b02.status = MaterialInspectionStatus.COMPLETED
    b02.result = MaterialInspectionResult.FAIL
    b02.production_valid = False
    b02.predicted_class_name = "wall_ext_back_window"
    b02.material_confidence = 0.31
    b02.failure_type = MaterialInspectionFailureType.WRONG_CLASS
    b02.result_detail_json = json.dumps({"defects": ["COLOR_NG", "CRACK_DAMAGE"], "quality_scores": {"surface": 0.2}})
    b02.completed_at = now
    other.status = IncomingQATransactionStatus.ERROR
    other.retry_count = 2
    other.error_reason = "ACK_TIMEOUT_MAX_RETRIES"
    db_session.commit()

    response = client.get(f"/production/jobs/{job.job_id}/incoming-qa/v02")

    assert response.status_code == 200
    payload = response.json()
    assert [transaction["inspection_request_id"] for transaction in payload["transactions"]] == [
        "REQ-BASE-1", "REQ-HOUSE-1", "REQ-B02-2"
    ]
    assert payload["gate"] == {"status": "HOLD", "total_expected_items": 3, "released_items": 1}
    completed_pass, acked, completed_fail = payload["transactions"]
    assert completed_pass["status"] == "COMPLETED"
    assert completed_pass["items"][0]["predicted_class_name"] == "base_house_b"
    assert acked["status"] == "ACKED"
    assert [item["status"] for item in acked["items"]] == ["REQUESTED", "REQUESTED"]
    assert [item["slot_id"] for item in acked["items"]] == ["B01", "B02"]
    assert completed_fail["inspection_cycle"] == 2
    fail_item = completed_fail["items"][0]
    assert fail_item["failure_type"] == "WRONG_CLASS"
    assert fail_item["predicted_class_name"] == "wall_ext_back_window"
    assert fail_item["defects"] == ["COLOR_NG", "CRACK_DAMAGE"]
    assert fail_item["quality_scores"] == {"surface": 0.2}
    assert "REQ-OTHER-1" not in response.text


def test_v02_monitor_projects_rejected_and_error_reasons(client: TestClient, db_session: Session) -> None:
    job, items = _seed_job(db_session, code="QA-MONITOR-ERROR")
    rejected = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-REJECTED-1", mode=IncomingQAInspectionMode.BASE_AB, cycle=1,
        items=[("C09", items["BASE"], "base_house_b")],
    ))
    rejected.status = IncomingQATransactionStatus.REJECTED
    rejected.ack_accepted = False
    rejected.ack_reason_code = "CONTRACT_CONFLICT"
    errored = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-ERROR-2", mode=IncomingQAInspectionMode.BASE_AB, cycle=2,
        items=[("C09", items["BASE"], "base_house_b")],
    ))
    errored.status = IncomingQATransactionStatus.ERROR
    errored.retry_count = 3
    errored.error_reason = "ACK_TIMEOUT_MAX_RETRIES"
    db_session.commit()

    response = client.get(f"/production/jobs/{job.job_id}/incoming-qa/v02")

    assert response.status_code == 200
    rejected_json, error_json = response.json()["transactions"]
    assert (rejected_json["status"], rejected_json["ack_reason_code"]) == ("REJECTED", "CONTRACT_CONFLICT")
    assert (error_json["status"], error_json["retry_count"], error_json["error_reason"]) == (
        "ERROR", 3, "ACK_TIMEOUT_MAX_RETRIES"
    )


def test_v02_monitor_returns_404_for_unknown_job(client: TestClient) -> None:
    assert client.get("/production/jobs/999999/incoming-qa/v02").status_code == 404


def test_v02_monitor_requested_transaction_uses_immutable_item_snapshot(client: TestClient, db_session: Session) -> None:
    job, items = _seed_job(db_session, code="QA-MONITOR-REQUESTED")
    transaction = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-REQUESTED-1", mode=IncomingQAInspectionMode.HOUSE_B, cycle=1,
        items=[("B01", items["B01"], "wall_ext_back_window"), ("B02", items["B02"], "wall_ext_door")],
    ))
    db_session.commit()

    response = client.get(f"/production/jobs/{job.job_id}/incoming-qa/v02")

    assert response.status_code == 200
    payload = response.json()["transactions"]
    assert len(payload) == 1
    assert payload[0]["transaction_id"] == transaction.transaction_id
    assert payload[0]["status"] == "REQUESTED"
    assert [(item["slot_id"], item["status"], item["result"]) for item in payload[0]["items"]] == [
        ("B01", "REQUESTED", None), ("B02", "REQUESTED", None)
    ]


def test_v02_monitor_reuses_latest_effective_gate_for_release(client: TestClient, db_session: Session) -> None:
    job, items = _seed_job(db_session, code="QA-MONITOR-RELEASE")
    base = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-RELEASE-BASE", mode=IncomingQAInspectionMode.BASE_AB, cycle=1,
        items=[("C09", items["BASE"], "base_house_b")],
    ))
    house = _create_transaction(db_session, job=job, request=_request(
        request_id="REQ-RELEASE-HOUSE", mode=IncomingQAInspectionMode.HOUSE_B, cycle=1,
        items=[("B01", items["B01"], "wall_ext_back_window"), ("B02", items["B02"], "wall_ext_door")],
    ))
    for transaction, item_values in ((base, [items["BASE"]]), (house, [items["B01"], items["B02"]])):
        transaction.status = IncomingQATransactionStatus.COMPLETED
        transaction.overall_result = MaterialInspectionResult.PASS
        transaction.production_valid = True
        for item in item_values:
            inspection = _inspection(db_session, transaction, item)
            inspection.status = MaterialInspectionStatus.COMPLETED
            inspection.result = MaterialInspectionResult.PASS
            inspection.production_valid = True
    db_session.commit()

    response = client.get(f"/production/jobs/{job.job_id}/incoming-qa/v02")

    assert response.status_code == 200
    assert response.json()["gate"] == {"status": "RELEASE", "total_expected_items": 3, "released_items": 3}


def test_v02_test_hold_api_persists_and_monitor_exposes_backend_authority(
    client: TestClient, db_session: Session
) -> None:
    job, _items = _seed_job(db_session, code="QA-MONITOR-HOLD")
    db_session.commit()

    enabled = client.post(
        f"/production/jobs/{job.job_id}/incoming-qa/v02/test-hold", json={"enabled": True}
    )
    assert enabled.status_code == 200
    assert enabled.json() == {"job_id": job.job_id, "incoming_qa_test_hold": True}
    payload = client.get(f"/production/jobs/{job.job_id}/incoming-qa/v02").json()
    assert payload["incoming_qa_test_hold"] is True
    assert payload["can_enable_incoming_qa_test_hold"] is False
    assert payload["can_disable_incoming_qa_test_hold"] is True

    disabled = client.post(
        f"/production/jobs/{job.job_id}/incoming-qa/v02/test-hold", json={"enabled": False}
    )
    assert disabled.status_code == 200
    assert disabled.json()["incoming_qa_test_hold"] is False
