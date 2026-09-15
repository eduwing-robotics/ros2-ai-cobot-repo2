from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fms_server.forklift_action_adapter import (
    FakeForkliftActionTransport,
    ForkliftActionAdapter,
    ForkliftActionStatus,
    ForkliftExecutionFeedback,
    ForkliftExecutionResult,
)
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.forklift_runtime_state import (
    ForkliftRuntimeEvent,
    ForkliftRuntimeState,
    ForkliftRuntimeStatePublisher,
    ForkliftRuntimeStateStore,
)
from shared.config import Settings
from shared.realtime.transport_events import TRANSPORT_EVENT_CHANNEL
from shared.models import Base
from shared.models.factory import Product
from shared.services.execution_attempt_service import ExecutionAttemptService


class MemoryRedis:
    def __init__(self, *, fail_set: bool = False, fail_delete: bool = False, fail_publish: bool = False) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.fail_set = fail_set
        self.fail_delete = fail_delete
        self.fail_publish = fail_publish
        self.closed = False

    async def set_json(self, key: str, payload: dict[str, Any]) -> None:
        if self.fail_set:
            raise RuntimeError("SET unavailable")
        self.values[key] = dict(payload)

    async def get_json(self, key: str) -> object | None:
        return self.values.get(key)

    async def delete(self, *keys: str) -> int:
        if self.fail_delete:
            raise RuntimeError("DEL unavailable")
        return sum(1 for key in keys if self.values.pop(key, None) is not None)

    async def publish_json(self, channel: str, payload: dict[str, Any]) -> None:
        if self.fail_publish:
            raise RuntimeError("PUBLISH unavailable")
        self.published.append((channel, dict(payload)))

    async def close(self) -> None:
        self.closed = True


def _state(
    *, req_id: str = "REQ-1", robot_id: str = "configured_turtle", task_type: str = "EXECUTE_TRANSPORT",
    job_id: int | None = 11, delivery_id: int | None = 22, phase: str = "MOVING_TO_PICKUP", progress: float = 0.1,
) -> ForkliftRuntimeState:
    return ForkliftRuntimeState(
        req_id=req_id,
        job_id=job_id,
        delivery_id=delivery_id,
        robot_id=robot_id,
        task_type=task_type,
        phase=phase,
        progress=progress,
        detail="actual feedback",
    )


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="HOUSE_A", product_name="A"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _coordinator(
    session: Session,
    *,
    feedback: tuple[ForkliftExecutionFeedback, ...] = (),
    result: ForkliftExecutionResult | None = None,
    runtime_state_sink=None,
    runtime_state_cleanup_sink=None,
    runtime_event_sink=None,
    settings: Settings | None = None,
) -> ForkliftExecutionCoordinator:
    transport = FakeForkliftActionTransport(
        execute_transport_feedback=feedback,
        return_home_feedback=feedback,
        execute_transport_result=result,
        return_home_result=result,
    )
    return ForkliftExecutionCoordinator(
        session,
        adapter=ForkliftActionAdapter(transport),
        execution_attempt_service=ExecutionAttemptService(session),
        runtime_state_sink=runtime_state_sink,
        runtime_state_cleanup_sink=runtime_state_cleanup_sink,
        runtime_event_sink=runtime_event_sink,
        settings=settings,
    )


def _execute(coordinator: ForkliftExecutionCoordinator, *, req_id: str = "REQ-EXECUTE") -> ForkliftExecutionResult:
    return coordinator.execute_transport(
        job_id=11,
        delivery_id=22,
        pickup_code="RACK1",
        dropoff_code="DROP",
        req_id=req_id,
    )


def test_latest_is_absent_before_any_feedback() -> None:
    async def run() -> None:
        store = ForkliftRuntimeStateStore(MemoryRedis())
        assert await store.get_latest("configured_turtle") is None

    asyncio.run(run())


def test_store_overwrites_with_exact_second_feedback() -> None:
    async def run() -> None:
        redis = MemoryRedis()
        store = ForkliftRuntimeStateStore(redis)
        first = _state(phase="MOVING_TO_PICKUP", progress=0.1)
        second = _state(phase="MOVING_TO_DROPOFF", progress=0.62)
        await store.write(first)
        await store.write(second)

        assert await store.get_latest(second.robot_id) == second
        assert redis.values[store.key_for(second.robot_id)] == second.to_payload()

    asyncio.run(run())


def test_coordinator_binds_execute_feedback_context_and_registry_robot_id(session: Session) -> None:
    observed: list[ForkliftRuntimeState] = []
    configured = Settings(turtlebot_robot_id="configured_turtle")
    feedback = ForkliftExecutionFeedback("LIFTING_UP", 0.3, "lifting")
    coordinator = _coordinator(
        session,
        feedback=(feedback,),
        runtime_state_sink=observed.append,
        settings=configured,
    )

    _execute(coordinator)

    assert observed == [
        ForkliftRuntimeState(
            req_id="REQ-EXECUTE", job_id=11, delivery_id=22,
            robot_id="configured_turtle", task_type="EXECUTE_TRANSPORT",
            phase="LIFTING_UP", progress=0.3, detail="lifting",
        )
    ]


def test_return_home_context_has_no_delivery_or_job(session: Session) -> None:
    observed: list[ForkliftRuntimeState] = []
    feedback = ForkliftExecutionFeedback("RETURNING_HOME", 0.5, "return")
    coordinator = _coordinator(
        session,
        feedback=(feedback,),
        runtime_state_sink=observed.append,
        settings=Settings(turtlebot_robot_id="configured_turtle"),
    )

    coordinator.return_home(req_id="REQ-HOME")

    assert observed[0].req_id == "REQ-HOME"
    assert observed[0].job_id is None
    assert observed[0].delivery_id is None
    assert observed[0].task_type == "RETURN_HOME"
    assert observed[0].phase == "RETURNING_HOME"


def test_terminal_event_uses_last_actual_feedback_and_authoritative_result(session: Session) -> None:
    events: list[ForkliftRuntimeEvent] = []
    coordinator = _coordinator(
        session,
        feedback=(ForkliftExecutionFeedback("LIFTING_DOWN", 0.93, "feedback detail"),),
        result=ForkliftExecutionResult(ForkliftActionStatus.FAILED, "DOCK_FAILED", "actual result detail"),
        runtime_event_sink=events.append,
        settings=Settings(turtlebot_robot_id="configured_turtle"),
    )

    assert _execute(coordinator, req_id="REQ-TERMINAL").status is ForkliftActionStatus.FAILED
    assert events == [
        ForkliftRuntimeEvent(
            state=ForkliftRuntimeState(
                req_id="REQ-TERMINAL", job_id=11, delivery_id=22,
                robot_id="configured_turtle", task_type="EXECUTE_TRANSPORT",
                phase="LIFTING_DOWN", progress=0.93, detail="feedback detail",
            ),
            result="FAILED", error_code="DOCK_FAILED", detail="actual result detail",
        )
    ]


def test_terminal_result_without_actual_feedback_emits_no_fabricated_event(session: Session) -> None:
    events: list[ForkliftRuntimeEvent] = []
    coordinator = _coordinator(session, runtime_event_sink=events.append)

    assert _execute(coordinator, req_id="REQ-NO-FEEDBACK").status is ForkliftActionStatus.SUCCEEDED
    assert events == []


def test_terminal_event_is_emitted_once_even_if_result_path_is_revisited(session: Session) -> None:
    events: list[ForkliftRuntimeEvent] = []
    result = ForkliftExecutionResult(ForkliftActionStatus.SUCCEEDED, "", "done")
    coordinator = _coordinator(
        session, feedback=(ForkliftExecutionFeedback("LIFTING_DOWN", 0.9, "feedback"),),
        result=result, runtime_event_sink=events.append,
    )

    _execute(coordinator, req_id="REQ-ONCE")
    coordinator._emit_terminal_runtime_event("REQ-ONCE", result)

    assert len(events) == 1


def test_terminal_event_sink_failure_does_not_change_business_result(session: Session) -> None:
    def fail_publish(event: ForkliftRuntimeEvent) -> None:
        raise RuntimeError("PUBLISH unavailable")

    coordinator = _coordinator(
        session, feedback=(ForkliftExecutionFeedback("MOVING_TO_PICKUP", 0.1, "feedback"),),
        runtime_event_sink=fail_publish,
    )

    assert _execute(coordinator, req_id="REQ-PUBLISH-FAIL").status is ForkliftActionStatus.SUCCEEDED


def test_newer_request_terminal_event_never_uses_old_request_feedback(session: Session) -> None:
    events: list[ForkliftRuntimeEvent] = []
    coordinator = _coordinator(session, runtime_event_sink=events.append)
    old = coordinator._bind_feedback(req_id="OLD", job_id=1, delivery_id=2, task_type="EXECUTE_TRANSPORT")
    new = coordinator._bind_feedback(req_id="NEW", job_id=3, delivery_id=4, task_type="EXECUTE_TRANSPORT")
    old(ForkliftExecutionFeedback("MOVING_TO_PICKUP", 0.1, "old"))
    new(ForkliftExecutionFeedback("LIFTING_DOWN", 0.9, "new"))

    coordinator._emit_terminal_runtime_event("NEW", ForkliftExecutionResult(ForkliftActionStatus.SUCCEEDED, "", "done"))

    assert events[0].state.req_id == "NEW"
    assert events[0].state.phase == "LIFTING_DOWN"


@pytest.mark.parametrize("status", list(ForkliftActionStatus))
def test_terminal_result_requests_req_id_safe_cleanup(session: Session, status: ForkliftActionStatus) -> None:
    cleanup: list[tuple[str, str]] = []
    coordinator = _coordinator(
        session,
        feedback=(ForkliftExecutionFeedback("MOVING_TO_PICKUP", 1.0, "feedback only"),),
        result=ForkliftExecutionResult(status, "", "result"),
        runtime_state_cleanup_sink=lambda robot_id, req_id: cleanup.append((robot_id, req_id)),
        settings=Settings(turtlebot_robot_id="configured_turtle"),
    )

    result = _execute(coordinator, req_id=f"REQ-{status.value}")

    assert result.status is status
    assert cleanup == [("configured_turtle", f"REQ-{status.value}")]


def test_req_id_safe_cleanup_does_not_delete_newer_state() -> None:
    async def run() -> None:
        redis = MemoryRedis()
        store = ForkliftRuntimeStateStore(redis)
        old = _state(req_id="OLD")
        new = _state(req_id="NEW", phase="MOVING_TO_DROPOFF", progress=0.6)
        await store.write(old)
        await store.write(new)

        assert await store.clear_if_matches(robot_id=new.robot_id, req_id=old.req_id) is False
        assert await store.get_latest(new.robot_id) == new

    asyncio.run(run())


def test_progress_one_feedback_does_not_change_authoritative_result(session: Session) -> None:
    coordinator = _coordinator(
        session,
        feedback=(ForkliftExecutionFeedback("LIFTING_DOWN", 1.0, "feedback"),),
        result=ForkliftExecutionResult(ForkliftActionStatus.FAILED, "E-RESULT", "failed result"),
    )

    result = _execute(coordinator)

    assert result.status is ForkliftActionStatus.FAILED
    assert result.error_code == "E-RESULT"


def test_runtime_state_sink_failure_does_not_change_business_result(session: Session) -> None:
    def fail_write(state: ForkliftRuntimeState) -> None:
        raise RuntimeError("Redis SET unavailable")

    coordinator = _coordinator(
        session,
        feedback=(ForkliftExecutionFeedback("MOVING_TO_PICKUP", 0.1, "feedback"),),
        runtime_state_sink=fail_write,
    )

    assert _execute(coordinator).status is ForkliftActionStatus.SUCCEEDED


def test_runtime_cleanup_failure_does_not_change_business_result(session: Session) -> None:
    def fail_cleanup(robot_id: str, req_id: str) -> None:
        raise RuntimeError("Redis DEL unavailable")

    coordinator = _coordinator(session, runtime_state_cleanup_sink=fail_cleanup)

    assert _execute(coordinator).status is ForkliftActionStatus.SUCCEEDED


def test_async_publisher_writes_and_cleans_latest_state() -> None:
    async def eventually(predicate) -> None:
        for _ in range(50):
            if predicate():
                return
            await asyncio.sleep(0.002)
        raise AssertionError("publisher did not process queued runtime state")

    async def run() -> None:
        redis = MemoryRedis()
        publisher = ForkliftRuntimeStatePublisher(client_factory=lambda: redis)
        state = _state(robot_id="configured_turtle")
        await publisher.start()
        try:
            publisher.notify_state(state)
            key = ForkliftRuntimeStateStore.key_for(state.robot_id)
            await eventually(lambda: key in redis.values)
            await eventually(lambda: len(redis.published) == 1)
            assert redis.published == [(
                TRANSPORT_EVENT_CHANNEL,
                {**state.to_payload(), "result": None, "error_code": None},
            )]
            terminal = ForkliftRuntimeEvent(state, "SUCCEEDED", "", "actual result")
            publisher.notify_event(terminal)
            await eventually(lambda: len(redis.published) == 2)
            assert redis.published[-1] == (TRANSPORT_EVENT_CHANNEL, terminal.to_payload())
            publisher.notify_terminal(robot_id=state.robot_id, req_id=state.req_id)
            await eventually(lambda: key not in redis.values)
        finally:
            await publisher.stop()

        assert redis.closed is True

    asyncio.run(run())


@pytest.mark.parametrize(("fail_set", "fail_delete"), [(True, False), (False, True)])
def test_async_publisher_redis_failure_is_nonfatal(fail_set: bool, fail_delete: bool) -> None:
    async def run() -> None:
        redis = MemoryRedis(fail_set=fail_set, fail_delete=fail_delete)
        publisher = ForkliftRuntimeStatePublisher(client_factory=lambda: redis)
        state = _state(robot_id="configured_turtle")
        key = ForkliftRuntimeStateStore.key_for(state.robot_id)
        if fail_delete:
            redis.values[key] = state.to_payload()
        await publisher.start()
        try:
            if fail_set:
                publisher.notify_state(state)
            else:
                publisher.notify_terminal(robot_id=state.robot_id, req_id=state.req_id)
            await asyncio.sleep(0.02)
        finally:
            await publisher.stop()
        assert redis.closed is True

    asyncio.run(run())
