from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import json

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.incoming_material_qa_http_client import (
    IncomingMaterialQAHttpClient,
    IncomingMaterialQAHttpConflictError,
)
from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from shared.config import Settings
from shared.models import Base
from shared.models.factory import (
    JobMaterialDeliveryItem,
    MaterialInspection,
    MaterialInspectionStatus,
    Part,
)
from shared.services.material_inspection_service import MaterialInspectionService


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _item(session: Session) -> JobMaterialDeliveryItem:
    part = Part(part_code="QA_HTTP_PART", part_name="QA HTTP", category="STRUCTURE", unit="EA", vision_class="wall_ext_left")
    item = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code=part.part_code, quantity=2)
    session.add_all([part, item])
    session.commit()
    return item


def _settings(**overrides: object) -> Settings:
    return Settings(
        vision_incoming_qa_base_url="http://vision.test:8010",
        vision_incoming_qa_request_path="/api/v1/incoming-qa/requests",
        vision_incoming_qa_timeout_seconds=0.1,
        vision_incoming_qa_max_attempts=2,
        vision_incoming_qa_retry_delay_seconds=0,
        **overrides,
    )


def test_request_persisted_before_202_then_marked_running(session: Session) -> None:
    item = _item(session)
    observed: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(request.content))
        assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
        inspection = session.scalar(select(MaterialInspection))
        assert inspection is not None and inspection.status == MaterialInspectionStatus.REQUESTED
        return httpx.Response(202, request=request)

    runtime = IncomingMaterialQARuntime(client=IncomingMaterialQAHttpClient(settings=_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))))
    acknowledgement = runtime.create_and_send(session, delivery_item_id=item.delivery_item_id)

    inspection = session.scalar(select(MaterialInspection))
    assert acknowledgement.status_code == 202
    assert inspection is not None and inspection.status == MaterialInspectionStatus.RUNNING
    assert observed == [{"ver": "0.1", "inspection_request_id": inspection.inspection_request_id, "delivery_item_id": item.delivery_item_id, "inspection_cycle": 1, "expected_part_code": "QA_HTTP_PART", "expected_class_name": "wall_ext_left", "expected_quantity": 2}]
    assert MaterialInspectionService().is_release_allowed(inspection) is False


def test_network_retry_reuses_exact_persisted_snapshot(session: Session) -> None:
    item = _item(session)
    payloads: list[bytes] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payloads.append(request.content)
        if calls == 1:
            raise httpx.ConnectTimeout("simulated")
        return httpx.Response(202, request=request)

    runtime = IncomingMaterialQARuntime(client=IncomingMaterialQAHttpClient(settings=_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None))
    runtime.create_and_send(session, delivery_item_id=item.delivery_item_id)
    assert calls == 2
    assert payloads[0] == payloads[1]
    inspection = session.scalar(select(MaterialInspection))
    assert inspection is not None and inspection.inspection_cycle == 1 and inspection.status == MaterialInspectionStatus.RUNNING


def test_duplicate_send_reuses_existing_transaction_without_new_cycle(session: Session) -> None:
    item = _item(session)
    payloads: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(request.content)
        return httpx.Response(202, request=request)

    runtime = IncomingMaterialQARuntime(client=IncomingMaterialQAHttpClient(settings=_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))))
    first = runtime.create_transaction(session, delivery_item_id=item.delivery_item_id)
    runtime.send_existing(session, inspection_request_id=first.inspection_request_id)
    runtime.send_existing(session, inspection_request_id=first.inspection_request_id)
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert payloads[0] == payloads[1]
    inspection = session.scalar(select(MaterialInspection))
    assert inspection is not None and inspection.inspection_request_id == first.inspection_request_id and inspection.inspection_cycle == 1


def test_send_uses_snapshot_not_mutated_catalog(session: Session) -> None:
    item = _item(session)
    service = MaterialInspectionService()
    request = service.request_inspection(session, item.delivery_item_id)
    session.commit()
    part = session.get(Part, "QA_HTTP_PART")
    assert part is not None
    part.vision_class = "changed_class"
    item.quantity = 99
    session.commit()
    observed: list[dict[str, object]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(http_request.content))
        return httpx.Response(202, request=http_request)

    runtime = IncomingMaterialQARuntime(client=IncomingMaterialQAHttpClient(settings=_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))))
    runtime.send_existing(session, inspection_request_id=request.inspection_request_id)
    assert observed[0]["expected_class_name"] == "wall_ext_left"
    assert observed[0]["expected_quantity"] == 2


def test_vision_409_marks_hold_without_allocating_new_cycle(session: Session) -> None:
    item = _item(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, request=request)

    runtime = IncomingMaterialQARuntime(client=IncomingMaterialQAHttpClient(settings=_settings(), client=httpx.Client(transport=httpx.MockTransport(handler))))
    with pytest.raises(IncomingMaterialQAHttpConflictError):
        runtime.create_and_send(session, delivery_item_id=item.delivery_item_id)
    inspection = session.scalar(select(MaterialInspection))
    assert inspection is not None and inspection.status == MaterialInspectionStatus.ERROR
    assert inspection.inspection_cycle == 1
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert MaterialInspectionService().is_release_allowed(inspection) is False


def test_release_policy_requires_completed_pass_not_runtime_authorization(session: Session) -> None:
    item = _item(session)
    service = MaterialInspectionService()
    request = service.request_inspection(session, item.delivery_item_id)
    inspection = session.scalar(select(MaterialInspection).where(MaterialInspection.inspection_request_id == request.inspection_request_id))
    assert inspection is not None
    assert service.is_release_allowed(None) is False
    assert service.is_release_allowed(inspection) is False  # REQUESTED
    service.mark_running(session, request.inspection_request_id)
    assert service.is_release_allowed(inspection) is False  # RUNNING
    inspection.status = MaterialInspectionStatus.COMPLETED
    inspection.result = "PASS"
    inspection.production_valid = False
    assert service.is_release_allowed(inspection) is True
    inspection.production_valid = True
    assert service.is_release_allowed(inspection) is True
    inspection.result = "FAIL"
    assert service.is_release_allowed(inspection) is False
    inspection.result = "NOT_EVALUATED"
    assert service.is_release_allowed(inspection) is False
    inspection.status = MaterialInspectionStatus.ERROR
    inspection.result = None
    assert service.is_release_allowed(inspection) is False
