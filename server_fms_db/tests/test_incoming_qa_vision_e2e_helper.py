from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from shared.config import Settings
from shared.models import Base
from shared.models.factory import (
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStep,
    MaterialInspection,
    Part,
    Product,
    ProductionJob,
)
from fms_server.incoming_material_qa_http_client import IncomingMaterialQAAcknowledgement, IncomingMaterialQAHttpClient, IncomingMaterialQAHttpConflictError
from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from shared.services.material_inspection_service import MaterialInspectionService


_spec = importlib.util.spec_from_file_location(
    "incoming_qa_e2e_helper", Path(__file__).parents[1] / "scripts" / "run_incoming_qa_vision_e2e.py"
)
assert _spec is not None and _spec.loader is not None
helper = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = helper
_spec.loader.exec_module(helper)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_helper_creates_owned_fixture_and_cleans_only_after_terminal_callback(session: Session) -> None:
    fixture = helper._create_disposable_fixture(session)
    assert fixture.product_code.startswith("INCOMING_QA_E2E_")
    assert fixture.part_code.startswith("INCOMING_QA_E2E_")
    request = MaterialInspectionService().request_inspection(session, fixture.delivery_item_id)
    session.commit()

    with pytest.raises(SystemExit, match="not reached a terminal"):
        helper._cleanup_fixture(session, inspection_request_id=request.inspection_request_id)

    MaterialInspectionService().mark_error(session, request.inspection_request_id, "test terminal")
    session.commit()
    cleaned = helper._cleanup_fixture(session, inspection_request_id=request.inspection_request_id)
    assert cleaned == fixture
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 0
    assert session.scalar(select(func.count()).select_from(JobMaterialDeliveryItem)) == 0
    assert session.scalar(select(func.count()).select_from(JobMaterialDelivery)) == 0
    assert session.scalar(select(func.count()).select_from(JobStep)) == 0
    assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0
    assert session.scalar(select(func.count()).select_from(Part)) == 0
    assert session.scalar(select(func.count()).select_from(Product)) == 0


class _RecordingRuntime:
    def __init__(self, acknowledgement_status: int) -> None:
        self.acknowledgement_status = acknowledgement_status
        self.request_ids: list[str] = []
        self.payload: dict[str, object] | None = None

    def send_existing(self, session: Session, *, inspection_request_id: str) -> IncomingMaterialQAAcknowledgement:
        self.request_ids.append(inspection_request_id)
        inspection = session.scalar(
            select(MaterialInspection).where(MaterialInspection.inspection_request_id == inspection_request_id)
        )
        assert inspection is not None
        self.payload = MaterialInspectionService.request_from_inspection(inspection).model_dump(mode="json")
        return IncomingMaterialQAAcknowledgement(
            status_code=self.acknowledgement_status,
            idempotent=self.acknowledgement_status == 200,
        )


def _running_fixture_request(session: Session):
    fixture = helper._create_disposable_fixture(session)
    request = MaterialInspectionService().request_inspection(session, fixture.delivery_item_id)
    MaterialInspectionService().mark_running(session, request.inspection_request_id)
    session.commit()
    inspection = session.scalar(
        select(MaterialInspection).where(MaterialInspection.inspection_request_id == request.inspection_request_id)
    )
    assert inspection is not None
    return fixture, request, inspection


@pytest.mark.parametrize("ack_status", [202, 200])
def test_resend_reuses_exact_persisted_snapshot_without_creating_rows(session: Session, ack_status: int) -> None:
    fixture, request, inspection = _running_fixture_request(session)
    before_snapshot = MaterialInspectionService.request_from_inspection(inspection).model_dump(mode="json")
    runtime = _RecordingRuntime(ack_status)

    acknowledgement, returned, returned_fixture, outbound = helper._resend_existing(
        session, inspection_request_id=request.inspection_request_id, runtime=runtime
    )

    assert acknowledgement.status_code == ack_status
    assert runtime.request_ids == [request.inspection_request_id]
    assert runtime.payload == before_snapshot == outbound
    assert returned.inspection_request_id == request.inspection_request_id
    assert returned.inspection_cycle == 1
    assert returned_fixture == fixture
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert MaterialInspectionService.request_from_inspection(returned).model_dump(mode="json") == before_snapshot


def test_resend_surfaces_409_without_new_rows_or_snapshot_mutation(session: Session) -> None:
    _, request, inspection = _running_fixture_request(session)
    before_snapshot = MaterialInspectionService.request_from_inspection(inspection).model_dump(mode="json")

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, request=http_request)

    client = IncomingMaterialQAHttpClient(
        settings=Settings(
            vision_incoming_qa_base_url="http://vision.test",
            vision_incoming_qa_request_path="/api/v1/incoming-qa/requests",
            vision_incoming_qa_max_attempts=1,
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(IncomingMaterialQAHttpConflictError):
        helper._resend_existing(
            session,
            inspection_request_id=request.inspection_request_id,
            runtime=IncomingMaterialQARuntime(client=client),
        )

    row = session.scalar(select(MaterialInspection).where(MaterialInspection.inspection_request_id == request.inspection_request_id))
    assert row is not None and row.status == "ERROR"
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert MaterialInspectionService.request_from_inspection(row).model_dump(mode="json") == before_snapshot


def test_resend_rejects_terminal_or_non_owned_fixture_before_send(session: Session) -> None:
    _, request, _ = _running_fixture_request(session)
    MaterialInspectionService().mark_error(session, request.inspection_request_id, "terminal")
    session.commit()
    with pytest.raises(SystemExit, match="terminal"):
        helper._resend_existing(session, inspection_request_id=request.inspection_request_id, runtime=_RecordingRuntime(202))

    fixture, second_request, _ = _running_fixture_request(session)
    delivery = session.get(JobMaterialDelivery, fixture.delivery_id)
    part = session.get(Part, fixture.part_code)
    job = session.get(ProductionJob, fixture.job_id)
    assert delivery is not None and part is not None and job is not None
    delivery.delivery_code = "NOT_OWNED"
    job.job_code = "NOT_OWNED_JOB"
    session.commit()
    with pytest.raises(SystemExit, match="does not own"):
        helper._resend_existing(session, inspection_request_id=second_request.inspection_request_id, runtime=_RecordingRuntime(202))


def test_test_database_guard_rejects_production_target(monkeypatch: pytest.MonkeyPatch) -> None:
    unsafe = Settings(
        database_url="postgresql+psycopg://user:secret@localhost/smart_factory_benchmark",
        postgres_test_database_url="postgresql+psycopg://user:secret@localhost/smart_factory_benchmark",
        cell_transport="fake",
    )
    monkeypatch.setattr(helper, "get_settings", lambda: unsafe)
    with pytest.raises(SystemExit, match="differ from DATABASE_URL"):
        helper._test_url()
