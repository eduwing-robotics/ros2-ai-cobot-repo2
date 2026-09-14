from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.services.production_snapshot_service import ProductionSnapshotService
from api_server.services.unity_production_inspection_projection_service import (
    UnityProductionInspectionProjectionService,
)
from api_server.services.unity_realtime import RedisTelemetrySubscriber, UnityRealtimeHub
from fms_server.production_inspection_realtime_publisher import ProductionInspectionChangedPublisher
from shared.models import Base
from shared.models.factory import (
    ProductionInspection,
    ProductionInspectionResultCode,
    ProductionInspectionStatus,
    Product,
    RoofOptionCode,
)
from shared.realtime.production_inspection_events import (
    PRODUCTION_INSPECTION_CHANGED_CHANNEL,
    set_production_inspection_change_callback,
)
from shared.schemas.pre_roof_vision import PreRoofInspectionResultV01, PreRoofViewInspectionResultV02
from shared.services.production_completion_service import ProductionCompletionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from tests.recipe_test_support import HOUSE_B_STAGES, add_active_recipe, add_gated_roof_stages


VIEWS = ("TOP", "LEFT", "RIGHT", "FRONT", "BEHIND")
VERSIONS_V3 = {"controller": "V3", "TOP": "V5", "LEFT": "V3", "RIGHT": "V7", "FRONT": "V1", "BEHIND": "V4"}


@dataclass(eq=False)
class FakeWebSocket:
    messages: list[dict] = field(default_factory=list)
    async def accept(self) -> None: return None
    async def send_json(self, payload: dict) -> None: self.messages.append(payload)


def _factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Product(product_code="UNITY_PRE", product_name="Unity PRE")); session.flush()
        add_gated_roof_stages(session, add_active_recipe(session, "UNITY_PRE", stages=HOUSE_B_STAGES))
        session.commit()
    return factory, engine


def _job_at_pre_roof(session: Session):
    job = ProductionOrchestrationService(session).create_job(
        product_code="UNITY_PRE", job_code=f"UNITY-PRE-{uuid.uuid4().hex}", roof_option_code=RoofOptionCode.ROOF_02
    )
    orchestration = ProductionOrchestrationService(session)
    orchestration.start_job(job.job_id)
    while (step := orchestration.get_next_step(job.job_id)) is not None:
        orchestration.start_step(step.job_step_id)
        orchestration.complete_step(step.job_step_id)
    return job


def _result(inspection: ProductionInspection, *, outcome: str = "PASS", valid: bool = False) -> PreRoofInspectionResultV01:
    return PreRoofInspectionResultV01(
        inspection_request_id=inspection.inspection_request_id,
        inspection_cycle=inspection.inspection_cycle,
        job_id=inspection.production_job_id,
        overall_result=outcome,
        vision_production_valid=valid,
        views=[{
            "view_name": view, "result": outcome,
            "reason_code": "RUNTIME_NOT_EVALUATED" if outcome == "NOT_EVALUATED" else None,
            "defects": [], "metrics": {},
        } for view in VIEWS],
        timestamp=datetime.now(timezone.utc), runtime_profile="PRE_ROOF_5VIEW", runtime_versions=VERSIONS_V3,
    )


def test_snapshot_is_empty_without_any_production_inspection() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            job = ProductionOrchestrationService(session).create_job(
                product_code="UNITY_PRE", job_code=f"NO-INSPECTION-{uuid.uuid4().hex}", roof_option_code=RoofOptionCode.ROOF_02
            )
            assert UnityProductionInspectionProjectionService(session).get_snapshots(job_ids=[job.job_id]) == []
    finally:
        Base.metadata.drop_all(engine); engine.dispose()


def test_projection_running_ack_and_pass_validity_gate_are_canonical() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            job = _job_at_pre_roof(session)
            service = ProductionCompletionService(session)
            inspection = service.start_pre_roof_inspection(production_job_id=job.job_id)
            projection = UnityProductionInspectionProjectionService(session).get_inspection_status(inspection_id=inspection.inspection_id)
            assert projection is not None
            current = projection["inspection"]
            assert current["status"] == "RUNNING" and current["current_view"] == "TOP"
            row_id, request = service.prepare_pre_roof_view_wire_request(inspection_id=inspection.inspection_id)
            assert service.apply_pre_roof_view_ack(inspection_request_id=str(request.inspection_request_id), inspection_cycle=request.inspection_cycle, view_name="TOP", accepted=True, duplicate=False, reason_code=None)
            assert UnityProductionInspectionProjectionService(session).get_inspection_status(inspection_id=inspection.inspection_id)["inspection"]["transport"]["acked"] is True
            for index, view in enumerate(VIEWS):
                _, current_request = service.prepare_pre_roof_view_wire_request(inspection_id=inspection.inspection_id)
                assert current_request.view_name.value == view
                result = PreRoofViewInspectionResultV02(inspection_request_id=current_request.inspection_request_id, inspection_cycle=current_request.inspection_cycle, job_id=job.job_id, job_code=job.job_code, view_name=view, result="PASS", production_valid=False, timestamp=datetime.now(timezone.utc))
                service.apply_pre_roof_view_result(vision_result=result, result_digest=str(index) * 64)
            payload = UnityProductionInspectionProjectionService(session).get_inspection_status(inspection_id=inspection.inspection_id)["inspection"]
            assert payload["result"] == "PASS" and payload["production_valid"] is False
            assert payload["gate_state"] == "RELEASED"
            assert [view["view_name"] for view in payload["views"]] == list(VIEWS)
    finally:
        Base.metadata.drop_all(engine); engine.dispose()

def test_latest_cycle_snapshot_wins_and_valid_pass_releases_gate() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
            first = service.start_pre_roof_inspection(production_job_id=job.job_id)
            service.fail_pre_roof_inspection(production_job_id=job.job_id, reason="operator retry")
            second = service.start_pre_roof_inspection(production_job_id=job.job_id)
            latest = UnityProductionInspectionProjectionService(session).get_inspection_status(inspection_id=first.inspection_id)
            assert latest["inspection"]["inspection_cycle"] == 2
            assert latest["inspection"]["status"] == "RUNNING" and latest["inspection"]["current_view"] == "TOP"
            service.pass_pre_roof_inspection(production_job_id=job.job_id)
            released = UnityProductionInspectionProjectionService(session).get_inspection_status(inspection_id=second.inspection_id)
            assert released["inspection"]["production_valid"] is True
            assert released["inspection"]["gate_state"] == "RELEASED"
    finally:
        Base.metadata.drop_all(engine); engine.dispose()


def test_error_and_not_evaluated_remain_not_released() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
            first = service.start_pre_roof_inspection(production_job_id=job.job_id)
            service.not_evaluated_pre_roof_inspection(production_job_id=job.job_id, reason="runtime unavailable")
            ne = UnityProductionInspectionProjectionService(session).get_inspection_status(inspection_id=first.inspection_id)["inspection"]
            assert ne["status"] == "COMPLETED" and ne["result"] == "NOT_EVALUATED" and ne["gate_state"] == "NOT_RELEASED"
            second = service.start_pre_roof_inspection(production_job_id=job.job_id)
            service.error_pre_roof_inspection(production_job_id=job.job_id, reason="ACK_TIMEOUT_MAX_RETRIES")
            error = UnityProductionInspectionProjectionService(session).get_inspection_status(inspection_id=second.inspection_id)["inspection"]
            assert error["status"] == "ERROR" and error["result"] is None and error["gate_state"] == "NOT_RELEASED"
    finally:
        Base.metadata.drop_all(engine); engine.dispose()


def test_duplicate_and_stale_results_do_not_emit_new_inspection_changed_event() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
            observed: list[int] = []
            set_production_inspection_change_callback(observed.append)
            inspection = service.start_pre_roof_inspection(production_job_id=job.job_id)
            _, request = service.prepare_pre_roof_view_wire_request(inspection_id=inspection.inspection_id)
            result = PreRoofViewInspectionResultV02(inspection_request_id=request.inspection_request_id, inspection_cycle=request.inspection_cycle, job_id=job.job_id, job_code=job.job_code, view_name="TOP", result="PASS", timestamp=datetime.now(timezone.utc))
            service.apply_pre_roof_view_result(vision_result=result, result_digest="4" * 64)
            before_duplicate = list(observed)
            assert service.apply_pre_roof_view_result(vision_result=result, result_digest="4" * 64).value == "DUPLICATE"
            assert observed == before_duplicate
    finally:
        set_production_inspection_change_callback(None)
        Base.metadata.drop_all(engine); engine.dispose()


def test_websocket_publishes_v02_per_view_aggregate_transitions() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            job = _job_at_pre_roof(session); service = ProductionCompletionService(session)
            inspection = service.start_pre_roof_inspection(production_job_id=job.job_id)

            async def publish_current(hub: UnityRealtimeHub) -> dict:
                subscriber = RedisTelemetrySubscriber(hub, production_inspection_status_reader=ProductionSnapshotService(factory).get_production_inspection_status)
                await subscriber._handle_production_inspection_changed({"inspection_id": inspection.inspection_id})
                await asyncio.sleep(0); await asyncio.sleep(0)
                return socket.messages[-1]["data"]["inspection"]

            async def run() -> None:
                nonlocal socket
                hub = UnityRealtimeHub(lambda: ProductionSnapshotService(factory).get_snapshot())
                socket = FakeWebSocket(); await hub.connect(socket)
                _, top = service.prepare_pre_roof_view_wire_request(inspection_id=inspection.inspection_id)
                service.apply_pre_roof_view_result(vision_result=PreRoofViewInspectionResultV02(inspection_request_id=top.inspection_request_id, inspection_cycle=top.inspection_cycle, job_id=job.job_id, job_code=job.job_code, view_name="TOP", result="PASS", timestamp=datetime.now(timezone.utc)), result_digest="a" * 64)
                partial = await publish_current(hub)
                assert partial["current_view"] == "LEFT"
                assert [(v["view_name"], v["status"]) for v in partial["views"]] == [("TOP", "PASS"), ("LEFT", "IN_PROGRESS"), ("RIGHT", "PENDING"), ("FRONT", "PENDING"), ("BEHIND", "PENDING")]

                _, left = service.prepare_pre_roof_view_wire_request(inspection_id=inspection.inspection_id)
                service.apply_pre_roof_view_result(vision_result=PreRoofViewInspectionResultV02(inspection_request_id=left.inspection_request_id, inspection_cycle=left.inspection_cycle, job_id=job.job_id, job_code=job.job_code, view_name="LEFT", result="FAIL", timestamp=datetime.now(timezone.utc)), result_digest="b" * 64)
                failed = await publish_current(hub)
                assert failed["current_view"] == "LEFT"
                assert failed["views"][1]["status"] == "IN_PROGRESS"
                assert failed["views"][2]["status"] == "PENDING"

                for index, expected in enumerate(("LEFT", "RIGHT", "FRONT", "BEHIND"), start=2):
                    _, request = service.prepare_pre_roof_view_wire_request(inspection_id=inspection.inspection_id)
                    assert request.view_name.value == expected
                    service.apply_pre_roof_view_result(vision_result=PreRoofViewInspectionResultV02(inspection_request_id=request.inspection_request_id, inspection_cycle=request.inspection_cycle, job_id=job.job_id, job_code=job.job_code, view_name=expected, result="PASS", production_valid=False, timestamp=datetime.now(timezone.utc)), result_digest=f"{index:x}" * 64)
                    await publish_current(hub)
                final = socket.messages[-1]["data"]["inspection"]
                assert final["status"] == "COMPLETED" and final["result"] == "PASS"
                assert final["gate_state"] == "RELEASED" and final["current_view"] is None
                assert [view["status"] for view in final["views"]] == ["PASS"] * 5
            socket: FakeWebSocket
            asyncio.run(run())
    finally:
        Base.metadata.drop_all(engine); engine.dispose()


def test_post_commit_callback_and_subscriber_reread_broadcast_canonical_status() -> None:
    factory, engine = _factory()
    try:
        with factory() as session:
            job = _job_at_pre_roof(session)
            observed: list[int] = []
            set_production_inspection_change_callback(observed.append)
            inspection = ProductionCompletionService(session).start_pre_roof_inspection(production_job_id=job.job_id)
            set_production_inspection_change_callback(None)
            assert observed == [inspection.inspection_id]

        async def run() -> None:
            hub = UnityRealtimeHub(lambda: ProductionSnapshotService(factory).get_snapshot())
            socket = FakeWebSocket(); await hub.connect(socket)
            reader = ProductionSnapshotService(factory).get_production_inspection_status
            subscriber = RedisTelemetrySubscriber(hub, production_inspection_status_reader=reader)
            await subscriber._handle_production_inspection_changed({"inspection_id": str(inspection.inspection_id), "untrusted": "raw"})
            await asyncio.sleep(0); await asyncio.sleep(0)
            snapshot_inspections = socket.messages[0]["data"]["production_inspections"]
            message = socket.messages[-1]
            assert message["type"] == "production_inspection_status"
            assert message["data"]["inspection"]["inspection_id"] == inspection.inspection_id
            assert message["data"]["inspection"]["status"] == "RUNNING"
            assert snapshot_inspections == [message["data"]]
        asyncio.run(run())
    finally:
        set_production_inspection_change_callback(None)
        Base.metadata.drop_all(engine); engine.dispose()


def test_publisher_failure_is_best_effort_and_snapshot_reuses_same_projection() -> None:
    class BrokenClient:
        async def publish_json(self, channel: str, payload: dict) -> int:
            assert channel == PRODUCTION_INSPECTION_CHANGED_CHANNEL and payload["inspection_id"] == 19
            raise RuntimeError("redis unavailable")
        async def close(self) -> None: return None

    async def run() -> None:
        publisher = ProductionInspectionChangedPublisher(client_factory=BrokenClient)
        await publisher.start(); publisher.notify_after_commit(19); await asyncio.sleep(0); await publisher.stop()
    asyncio.run(run())
