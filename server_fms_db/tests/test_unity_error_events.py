from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.services.production_snapshot_service import ProductionSnapshotService
from api_server.services.unity_realtime import RedisTelemetrySubscriber, UnityRealtimeHub
from shared.models import Base
from shared.models.factory import ExecutionAttempt, ExecutionAttemptStatus, ExecutorType
from shared.realtime.error_events import set_execution_attempt_error_callback
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.unity_error_projection_service import UnityErrorProjectionService


@dataclass(eq=False)
class FakeWebSocket:
    messages: list[dict] = field(default_factory=list)

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def _snapshot() -> dict:
    return {"jobs": [], "robots": [], "transports": [], "active_errors": []}


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield db
    finally:
        set_execution_attempt_error_callback(None)
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _attempt(*, status: ExecutionAttemptStatus, error_code: str | None = None, detail: str | None = None) -> ExecutionAttempt:
    return ExecutionAttempt(
        req_id=f"attempt-{status.value}-{error_code or 'none'}-{detail or 'none'}",
        executor_type=ExecutorType.ROBOT_CELL,
        command_type="INSTALL_WALL",
        attempt_no=1,
        status=status,
        request_payload_json="{}",
        error_code=error_code,
        detail=detail,
    )


def test_unknown_projection_is_active_and_does_not_fabricate_error_code(session: Session) -> None:
    unknown = _attempt(status=ExecutionAttemptStatus.UNKNOWN, detail="RESULT_TIMEOUT")
    failed = _attempt(status=ExecutionAttemptStatus.FAILED, error_code="E503", detail="Cell failed")
    session.add_all((unknown, failed))
    session.commit()

    projection = UnityErrorProjectionService(session)
    unknown_event = projection.get_error_event(attempt_id=unknown.attempt_id)
    assert unknown_event == {
        "source": "ROBOT_CELL", "job_id": None, "step_id": None, "delivery_id": None,
        "severity": None, "error_code": None, "detail": "RESULT_TIMEOUT", "recoverable": None,
    }
    assert projection.get_active_errors() == [unknown_event]
    assert projection.get_error_event(attempt_id=failed.attempt_id)["error_code"] == "E503"


def test_snapshot_reconstructs_only_unresolved_unknown_errors(session: Session) -> None:
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    snapshot_service = ProductionSnapshotService(factory)
    assert snapshot_service.get_snapshot()["active_errors"] == []

    unknown = _attempt(status=ExecutionAttemptStatus.UNKNOWN, detail="undetermined")
    failed = _attempt(status=ExecutionAttemptStatus.FAILED, detail="historical failure")
    session.add_all((unknown, failed))
    session.commit()

    first = snapshot_service.get_snapshot()["active_errors"]
    second = snapshot_service.get_snapshot()["active_errors"]
    assert first == second == [UnityErrorProjectionService(session).get_error_event(attempt_id=unknown.attempt_id)]


def test_unknown_transition_notifies_once_only_after_commit(session: Session) -> None:
    service = ExecutionAttemptService(session)
    attempt = _attempt(status=ExecutionAttemptStatus.ACCEPTED)
    session.add(attempt)
    session.commit()
    observed: list[int] = []
    set_execution_attempt_error_callback(observed.append)

    service.mark_unknown(attempt.req_id, detail="RESULT_TIMEOUT")
    assert observed == []
    session.commit()
    assert observed == [attempt.attempt_id]

    service.mark_unknown(attempt.req_id, detail="RESULT_TIMEOUT")
    session.commit()
    assert observed == [attempt.attempt_id]


def test_failed_transition_notifies_once_after_commit(session: Session) -> None:
    service = ExecutionAttemptService(session)
    attempt = _attempt(status=ExecutionAttemptStatus.ACCEPTED)
    session.add(attempt)
    session.commit()
    observed: list[int] = []
    set_execution_attempt_error_callback(observed.append)

    service.apply_result(attempt.req_id, ExecutionAttemptStatus.FAILED, error_code="E503", detail="Cell failed")
    assert observed == []
    session.commit()
    assert observed == [attempt.attempt_id]


def test_error_event_uses_envelope_sequence_and_fans_out_to_each_client() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(_snapshot)
        first, second = FakeWebSocket(), FakeWebSocket()
        await hub.connect(first)
        await hub.connect(second)
        error = {
            "source": "ROBOT_CELL", "job_id": 7, "step_id": 11, "delivery_id": None,
            "severity": None, "error_code": None, "detail": "RESULT_TIMEOUT", "recoverable": None,
        }
        await hub.publish_error_event(error)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        for socket in (first, second):
            assert [item["type"] for item in socket.messages] == ["production_snapshot", "error_event"]
            assert [item["sequence"] for item in socket.messages] == [1, 2]
            assert socket.messages[-1]["schema_version"] == "1.0"
            assert socket.messages[-1]["data"] == error

    asyncio.run(run())


def test_error_subscriber_rereads_authoritative_attempt_state() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(_snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        seen: list[int] = []
        subscriber = RedisTelemetrySubscriber(
            hub,
            error_event_reader=lambda attempt_id: seen.append(attempt_id) or {
                "source": "FORKLIFT", "job_id": 3, "step_id": None, "delivery_id": 9,
                "severity": None, "error_code": None, "detail": "undetermined", "recoverable": None,
            },
        )
        await subscriber._handle_error_event({"attempt_id": "14", "error_code": "FAKE"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == [14]
        assert socket.messages[-1]["type"] == "error_event"
        assert socket.messages[-1]["data"]["error_code"] is None

    asyncio.run(run())
