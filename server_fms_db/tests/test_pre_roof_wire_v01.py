"""Regression coverage for PRE_ROOF Vision wire contract v0.2.

The filename is retained so existing test selection remains valid; v0.1 bundled
messages are intentionally unsupported by the runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.pre_roof_udp_transport import PreRoofUdpDatagramProtocol, PreRoofUdpRuntime, PreRoofUdpRuntimeConfig
from shared.models import Base
from shared.models.factory import (
    ProductionInspection, ProductionInspectionResultCode, ProductionInspectionStatus,
    ProductionInspectionViewRequest, Product, RoofOptionCode,
)
from shared.schemas.pre_roof_vision import (
    PreRoofInspectionResultV01, PreRoofViewInspectionAckV02,
    PreRoofViewInspectionRequestV02, PreRoofViewInspectionResultV02, PreRoofViewMessageType,
)
from shared.services.production_completion_service import PreRoofVisionApplyDisposition, ProductionCompletionService
from shared.realtime.production_inspection_events import set_production_inspection_change_callback
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import HOUSE_B_STAGES, add_active_recipe, add_gated_roof_stages

VIEWS = ("TOP", "LEFT", "RIGHT", "FRONT", "BEHIND")


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(Product(product_code="HOUSE_B", product_name="B"))
    db.flush()
    add_gated_roof_stages(db, add_active_recipe(db, "HOUSE_B", stages=HOUSE_B_STAGES))
    db.commit()
    try:
        yield db
    finally:
        db.close(); Base.metadata.drop_all(engine); engine.dispose()


def _job_at_pre_roof(session: Session):
    job = ProductionOrchestrationService(session).create_job(
        product_code="HOUSE_B", job_code=f"PRE-V02-{uuid.uuid4().hex}", roof_option_code=RoofOptionCode.ROOF_02
    )
    orch = ProductionOrchestrationService(session); orch.start_job(job.job_id)
    while (step := orch.get_next_step(job.job_id)) is not None:
        orch.start_step(step.job_step_id); orch.complete_step(step.job_step_id)
    session.expire_all(); return session.get(type(job), job.job_id)


def _request(session: Session, inspection: ProductionInspection):
    return ProductionCompletionService(session).prepare_pre_roof_view_wire_request(inspection_id=inspection.inspection_id)


def _result(request, *, result="PASS", view_name=None, production_valid=False):
    return PreRoofViewInspectionResultV02(
        inspection_request_id=request.inspection_request_id,
        inspection_cycle=request.inspection_cycle, job_id=request.job_id, job_code=request.job_code,
        view_name=view_name or request.view_name, result=result,
        runtime_version="V7", runtime_port=8775, production_valid=production_valid,
        timestamp=datetime.now(timezone.utc),
    )


def test_first_eligibility_creates_top_v02_request(session: Session) -> None:
    job = _job_at_pre_roof(session)
    inspection = ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
    _, request = _request(session, inspection)
    payload = request.model_dump(mode="json")
    assert payload["ver"] == "0.2"
    assert payload["message_type"] == "pre_roof_view_inspection_request"
    assert payload["view_name"] == "TOP"


def test_accepted_ack_is_correlated_by_request_cycle_and_view(session: Session) -> None:
    job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
    inspection = service.start_pre_roof_inspection(production_job_id=job.job_id); request_id, request = _request(session, inspection)
    assert service.apply_pre_roof_view_ack(inspection_request_id=str(request.inspection_request_id), inspection_cycle=request.inspection_cycle, view_name="TOP", accepted=True, duplicate=False, reason_code=None)
    row = session.get(ProductionInspectionViewRequest, request_id)
    assert row.status == "ACKED" and row.acked_at is not None
    assert not service.apply_pre_roof_view_ack(inspection_request_id=str(request.inspection_request_id), inspection_cycle=request.inspection_cycle, view_name="LEFT", accepted=True, duplicate=False, reason_code=None)
    assert row.status == "ACKED"


def test_server_advances_one_view_and_reinspects_failed_view(session: Session) -> None:
    job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
    inspection = service.start_pre_roof_inspection(production_job_id=job.job_id)
    _, top = _request(session, inspection)
    assert service.apply_pre_roof_view_result(vision_result=_result(top), result_digest="a" * 64) is PreRoofVisionApplyDisposition.APPLIED
    _, left = _request(session, inspection)
    assert left.view_name.value == "LEFT"
    assert service.apply_pre_roof_view_result(vision_result=_result(left, result="FAIL"), result_digest="b" * 64) is PreRoofVisionApplyDisposition.APPLIED
    rows = list(session.scalars(select(ProductionInspectionViewRequest).where(ProductionInspectionViewRequest.inspection_id == inspection.inspection_id).order_by(ProductionInspectionViewRequest.view_request_id)))
    assert [row.view_name for row in rows] == ["TOP", "LEFT", "LEFT"]
    assert rows[-1].status == "REQUESTED" and rows[-1].inspection_request_id != str(left.inspection_request_id)
    _, left_retry = _request(session, inspection)
    assert left_retry.view_name.value == "LEFT" and left_retry.inspection_request_id != left.inspection_request_id
    assert service.apply_pre_roof_view_result(vision_result=_result(left_retry), result_digest="c" * 64) is PreRoofVisionApplyDisposition.APPLIED
    _, right = _request(session, inspection)
    assert right.view_name.value == "RIGHT"


def test_all_five_pass_release_roof_even_when_production_valid_false(session: Session) -> None:
    job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
    inspection = service.start_pre_roof_inspection(production_job_id=job.job_id)
    for index, expected in enumerate(VIEWS):
        _, request = _request(session, inspection)
        assert request.view_name.value == expected
        assert service.apply_pre_roof_view_result(vision_result=_result(request, production_valid=False), result_digest=f"{index:x}" * 64) is PreRoofVisionApplyDisposition.APPLIED
    session.expire_all(); stored = session.get(ProductionInspection, inspection.inspection_id)
    assert stored.status is ProductionInspectionStatus.COMPLETED
    assert stored.result is ProductionInspectionResultCode.PASS
    assert stored.production_valid is False
    assert ProductionCompletionService.is_pre_roof_gate_open(stored)
    assert session.get(type(job), job.job_id).status.value == "ROOF_READY"


def test_duplicate_out_of_order_and_late_result_do_not_progress_or_overwrite(session: Session) -> None:
    job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
    inspection = service.start_pre_roof_inspection(production_job_id=job.job_id)
    _, top = _request(session, inspection)
    assert service.apply_pre_roof_view_result(vision_result=_result(top), result_digest="d" * 64) is PreRoofVisionApplyDisposition.APPLIED
    assert service.apply_pre_roof_view_result(vision_result=_result(top), result_digest="d" * 64) is PreRoofVisionApplyDisposition.DUPLICATE
    _, left = _request(session, inspection)
    rogue = ProductionInspectionViewRequest(inspection_id=inspection.inspection_id, view_name="RIGHT", status="REQUESTED")
    session.add(rogue); session.commit()
    right = PreRoofViewInspectionResultV02(inspection_request_id=rogue.inspection_request_id, inspection_cycle=inspection.inspection_cycle, job_id=job.job_id, job_code=job.job_code, view_name="RIGHT", result="PASS", timestamp=datetime.now(timezone.utc))
    assert service.apply_pre_roof_view_result(vision_result=right, result_digest="e" * 64) is PreRoofVisionApplyDisposition.STALE
    assert service.apply_pre_roof_view_result(vision_result=_result(left, result="FAIL"), result_digest="f" * 64) is PreRoofVisionApplyDisposition.APPLIED
    _, retry = _request(session, inspection)
    assert service.apply_pre_roof_view_result(vision_result=_result(left, result="PASS"), result_digest="g" * 64) is PreRoofVisionApplyDisposition.CONFLICT
    assert retry.inspection_request_id != left.inspection_request_id
    assert session.scalar(select(ProductionInspectionViewRequest.status).where(ProductionInspectionViewRequest.inspection_request_id == str(retry.inspection_request_id))) == "REQUESTED"



def test_udp_result_callback_materializes_and_dispatches_left_with_autoflush_disabled(tmp_path) -> None:
    """Exercise the real runtime callback with the FMS session setting."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pre_roof_runtime.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        with factory() as db:
            db.add(Product(product_code="HOUSE_B", product_name="B")); db.flush()
            add_gated_roof_stages(db, add_active_recipe(db, "HOUSE_B", stages=HOUSE_B_STAGES)); db.commit()
            job = _job_at_pre_roof(db)
            inspection = ProductionCompletionService(db).start_pre_roof_inspection(production_job_id=job.job_id)
            inspection_id, job_id = inspection.inspection_id, job.job_id

        runtime = PreRoofUdpRuntime(
            session_factory=factory,
            config=PreRoofUdpRuntimeConfig("127.0.0.1", 20061, "127.0.0.1", 0, 1, 1),
        )
        top_request_id, top_payload = runtime._prepare_send(inspection_id, False)
        top = PreRoofViewInspectionRequestV02.model_validate_json(top_payload)
        assert top.view_name.value == "TOP"
        observed: list[int] = []
        set_production_inspection_change_callback(observed.append)
        try:
            outcome, _ = runtime._apply_result_sync(_result(top))
        finally:
            set_production_inspection_change_callback(None)
        assert outcome is PreRoofVisionApplyDisposition.APPLIED
        # The committed runtime result emits the existing inspection identity
        # callback consumed by the Redis publisher and Unity DB rereader.
        assert observed == [inspection_id]

        with factory() as db:
            rows = list(db.scalars(select(ProductionInspectionViewRequest).where(
                ProductionInspectionViewRequest.inspection_id == inspection_id
            ).order_by(ProductionInspectionViewRequest.view_request_id)))
            assert [(row.view_name, row.status, row.result) for row in rows] == [
                ("TOP", "COMPLETED", ProductionInspectionResultCode.PASS),
                ("LEFT", "REQUESTED", None),
            ]
            assert runtime._next_unsent_inspection_id() == inspection_id

        left_request_id, left_payload = runtime._prepare_send(inspection_id, False)
        left = PreRoofViewInspectionRequestV02.model_validate_json(left_payload)
        assert left.view_name.value == "LEFT" and left.inspection_request_id != top.inspection_request_id
        accepted, acked_id = runtime._apply_ack_sync(PreRoofViewInspectionAckV02(
            inspection_request_id=left.inspection_request_id, inspection_cycle=left.inspection_cycle,
            view_name="LEFT", accepted=True, duplicate=False, timestamp=datetime.now(timezone.utc),
        ))
        assert accepted and acked_id == left_request_id
        with factory() as db:
            left_row = db.get(ProductionInspectionViewRequest, left_request_id)
            assert left_row is not None and left_row.status == "ACKED" and left_row.acked_at is not None
    finally:
        Base.metadata.drop_all(engine); engine.dispose()


def test_v01_bundled_message_is_not_routed_as_v02(session: Session) -> None:
    runtime = PreRoofUdpRuntime(session_factory=lambda: session, config=PreRoofUdpRuntimeConfig("127.0.0.1", 20061, "127.0.0.1", 0, 1, 0))
    protocol = PreRoofUdpDatagramProtocol(runtime)
    # It is valid v0.1 JSON but has the obsolete bundled discriminator.
    payload = {"ver":"0.1", "message_type":"pre_roof_inspection_result", "inspection_request_id":str(uuid.uuid4()), "inspection_cycle":1, "job_id":1, "inspection_type":"PRE_ROOF", "status":"COMPLETED", "overall_result":"PASS", "vision_production_valid":False, "views":[], "timestamp":"2026-09-01T00:00:00Z", "runtime_profile":"PRE_ROOF_5VIEW", "runtime_versions":{}}
    protocol.datagram_received(__import__("json").dumps(payload).encode(), ("127.0.0.1", 20061))
    assert session.scalar(select(ProductionInspectionViewRequest.view_request_id)) is None
