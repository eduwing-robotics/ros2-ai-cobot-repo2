from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.inventory import get_db
from shared.models import Base
from shared.models.factory import JobMaterialDeliveryItem, MaterialInspection, MaterialInspectionStatus, Part
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.material_inspection_service import (
    MaterialInspectionResultConflictError,
    MaterialInspectionService,
)


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _request(session: Session) -> tuple[int, dict[str, object]]:
    part = Part(part_code="QA_CALLBACK_PART", part_name="QA callback", category="STRUCTURE", unit="EA", vision_class="wall_ext_right")
    item = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code=part.part_code, quantity=3)
    session.add_all([part, item])
    session.commit()
    request = MaterialInspectionService().request_inspection(session, item.delivery_item_id)
    session.commit()
    payload: dict[str, object] = {
        "ver": "0.1", "inspection_request_id": request.inspection_request_id,
        "delivery_item_id": item.delivery_item_id, "inspection_cycle": request.inspection_cycle,
        "status": "COMPLETED", "result": "PASS", "failure_type": None,
        "expected_part_code": request.expected_part_code, "expected_class_name": request.expected_class_name,
        "expected_quantity": request.expected_quantity, "detected_quantity": 3, "detections": [],
        "frame_width": 1920, "frame_height": 1080, "camera_source": "GLOBAL_CAMERA", "frame_seq": 10,
        "timestamp": datetime.now(timezone.utc).isoformat(), "model_scope": "VISION", "model_version": "v0.1",
        "production_valid": True,
    }
    return item.delivery_item_id, payload


def _apply_legacy_fixture(session: Session, payload: dict[str, object]) -> MaterialInspection:
    inspection = MaterialInspectionService().apply_inspection_result(
        session, IncomingMaterialQAResult.model_validate(payload)
    )
    session.commit()
    return inspection


def test_legacy_result_http_route_is_not_registered_and_cannot_release(client: TestClient, db_session: Session) -> None:
    item_id, payload = _request(db_session)

    response = client.post("/api/v1/incoming-qa/results", json=payload)

    assert response.status_code == 404
    inspection = db_session.scalar(select(MaterialInspection))
    assert inspection is not None and inspection.status is MaterialInspectionStatus.REQUESTED
    assert MaterialInspectionService().is_delivery_item_released(db_session, item_id) is False


def test_internal_legacy_fixture_service_remains_idempotent_without_public_http(client: TestClient, db_session: Session) -> None:
    item_id, payload = _request(db_session)

    first = _apply_legacy_fixture(db_session, payload)
    second = _apply_legacy_fixture(db_session, payload)

    assert first.inspection_id == second.inspection_id
    assert MaterialInspectionService().is_delivery_item_released(db_session, item_id) is True
    assert client.post("/api/v1/incoming-qa/results", json=payload).status_code == 404


@pytest.mark.parametrize(
    ("status", "result", "production_valid"),
    [
        ("COMPLETED", "FAIL", True),
        ("COMPLETED", "NOT_EVALUATED", True),
        ("ERROR", None, False),
    ],
)
def test_internal_failures_and_nonterminal_outcomes_remain_hold(
    db_session: Session, status: str, result: str | None, production_valid: bool,
) -> None:
    item_id, payload = _request(db_session)
    payload.update({"status": status, "result": result, "production_valid": production_valid})
    if result == "FAIL":
        payload["failure_type"] = "DEFECT"

    _apply_legacy_fixture(db_session, payload)

    assert MaterialInspectionService().is_delivery_item_released(db_session, item_id) is False



def test_completed_pass_releases_even_when_runtime_authorization_is_false(
    db_session: Session,
) -> None:
    item_id, payload = _request(db_session)
    payload.update({"status": "COMPLETED", "result": "PASS", "production_valid": False})

    inspection = _apply_legacy_fixture(db_session, payload)

    assert inspection.production_valid is False
    assert MaterialInspectionService().is_delivery_item_released(db_session, item_id) is True

def test_internal_conflicting_terminal_fixture_cannot_overwrite_history(db_session: Session) -> None:
    _, payload = _request(db_session)
    _apply_legacy_fixture(db_session, payload)
    conflict = dict(payload, result="FAIL", failure_type="DEFECT")

    with pytest.raises(MaterialInspectionResultConflictError):
        MaterialInspectionService().apply_inspection_result(
            db_session, IncomingMaterialQAResult.model_validate(conflict)
        )

    inspection = db_session.scalar(select(MaterialInspection))
    assert inspection is not None and str(inspection.result) == "PASS"
