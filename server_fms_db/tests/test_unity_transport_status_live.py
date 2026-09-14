from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from api_server.services.unity_realtime import RedisTelemetrySubscriber, UnityRealtimeHub
from fms_server.forklift_runtime_state import ForkliftRuntimeEvent, ForkliftRuntimeState


@dataclass(eq=False)
class FakeWebSocket:
    messages: list[dict] = field(default_factory=list)

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def snapshot() -> dict:
    # The live-only ticket deliberately leaves reconnect transport state empty.
    return {"jobs": [], "robots": [], "transports": [], "active_errors": []}


def runtime_state(
    *,
    req_id: str = "REQ-TRANSPORT",
    task_type: str = "EXECUTE_TRANSPORT",
    phase: str = "MOVING_TO_PICKUP",
    progress: float = 0.1,
) -> ForkliftRuntimeState:
    return ForkliftRuntimeState(
        req_id=req_id,
        job_id=41 if task_type == "EXECUTE_TRANSPORT" else None,
        delivery_id=73 if task_type == "EXECUTE_TRANSPORT" else None,
        robot_id="TB-01",
        task_type=task_type,
        phase=phase,
        progress=progress,
        detail="actual feedback detail",
    )


async def emit(hub: UnityRealtimeHub, event: ForkliftRuntimeEvent) -> None:
    subscriber = RedisTelemetrySubscriber(hub)
    await subscriber._handle_transport_event(event.to_payload())
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def test_execute_transport_feedback_projects_exact_contract_to_all_live_clients() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        first = FakeWebSocket()
        second = FakeWebSocket()
        await hub.connect(first)
        await hub.connect(second)

        await emit(hub, ForkliftRuntimeEvent(runtime_state(), None, None, "actual feedback detail"))

        for socket in (first, second):
            assert [message["type"] for message in socket.messages] == [
                "production_snapshot", "transport_status"
            ]
            assert [message["sequence"] for message in socket.messages] == [1, 2]
            message = socket.messages[-1]
            assert message["schema_version"] == "1.0"
            assert message["data"] == {
                "req_id": "REQ-TRANSPORT",
                "job_id": 41,
                "delivery_id": 73,
                "robot_id": "TB-01",
                "task_type": "EXECUTE_TRANSPORT",
                "phase": "MOVING_TO_PICKUP",
                "progress": 0.1,
                "result": None,
                "error_code": None,
                "detail": "actual feedback detail",
            }

    asyncio.run(run())


def test_second_actual_feedback_is_a_second_live_message_with_updated_values() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        await emit(hub, ForkliftRuntimeEvent(runtime_state(phase="MOVING_TO_PICKUP", progress=0.1), None, None, "first"))
        await emit(hub, ForkliftRuntimeEvent(runtime_state(phase="MOVING_TO_DROPOFF", progress=0.62), None, None, "second"))

        live = socket.messages[1:]
        assert [message["sequence"] for message in socket.messages] == [1, 2, 3]
        assert [(message["data"]["phase"], message["data"]["progress"], message["data"]["detail"]) for message in live] == [
            ("MOVING_TO_PICKUP", 0.1, "first"),
            ("MOVING_TO_DROPOFF", 0.62, "second"),
        ]

    asyncio.run(run())


def test_return_home_feedback_uses_null_job_and_delivery_ids() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        await emit(
            hub,
            ForkliftRuntimeEvent(
                runtime_state(task_type="RETURN_HOME", phase="RETURNING_HOME", progress=0.5),
                None,
                None,
                "returning from actual feedback",
            ),
        )

        data = socket.messages[-1]["data"]
        assert data["task_type"] == "RETURN_HOME"
        assert data["job_id"] is None
        assert data["delivery_id"] is None
        assert data["phase"] == "RETURNING_HOME"
        assert data["progress"] == 0.5
        assert data["result"] is None
        assert data["error_code"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("result", "error_code", "detail"),
    [
        ("SUCCEEDED", "", "transport completed"),
        ("FAILED", "DOCK_FAILED", "dock failed"),
        ("CANCELED", "", "transport canceled"),
    ],
)
def test_terminal_result_uses_last_actual_feedback_without_fabricating_runtime_values(
    result: str, error_code: str, detail: str
) -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        state = runtime_state(phase="LIFTING_DOWN", progress=0.93)
        await emit(hub, ForkliftRuntimeEvent(state, result, error_code, detail))

        data = socket.messages[-1]["data"]
        assert data["phase"] == "LIFTING_DOWN"
        assert data["progress"] == 0.93
        assert data["result"] == result
        assert data["error_code"] == error_code
        assert data["detail"] == detail

    asyncio.run(run())


def test_return_home_terminal_result_preserves_null_delivery_context() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        state = runtime_state(task_type="RETURN_HOME", phase="RETURNING_HOME", progress=0.8)
        await emit(hub, ForkliftRuntimeEvent(state, "FAILED", "HOME_FAILED", "return home failed"))

        data = socket.messages[-1]["data"]
        assert data["job_id"] is None
        assert data["delivery_id"] is None
        assert data["task_type"] == "RETURN_HOME"
        assert data["result"] == "FAILED"
        assert data["error_code"] == "HOME_FAILED"
        assert data["detail"] == "return home failed"

    asyncio.run(run())


def test_live_event_does_not_change_production_snapshot_transports() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        first = FakeWebSocket()
        await hub.connect(first)
        await emit(hub, ForkliftRuntimeEvent(runtime_state(), None, None, "feedback"))
        second = FakeWebSocket()
        await hub.connect(second)

        assert first.messages[0]["data"]["transports"] == []
        assert [message["type"] for message in second.messages] == ["production_snapshot"]
        assert second.messages[0]["sequence"] == 1
        assert second.messages[0]["data"]["transports"] == []

    asyncio.run(run())


def test_invalid_internal_transport_event_is_ignored_without_crashing_hub() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        subscriber = RedisTelemetrySubscriber(hub)
        await subscriber._handle_transport_event({"req_id": "missing-required-fields"})
        await asyncio.sleep(0)
        assert [message["type"] for message in socket.messages] == ["production_snapshot"]

    asyncio.run(run())


def test_uncontracted_unknown_terminal_result_is_not_exposed_as_transport_status() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        subscriber = RedisTelemetrySubscriber(hub)
        payload = ForkliftRuntimeEvent(runtime_state(), None, None, "feedback").to_payload()
        payload["result"] = "UNKNOWN"

        await subscriber._handle_transport_event(payload)
        await asyncio.sleep(0)

        assert [message["type"] for message in socket.messages] == ["production_snapshot"]

    asyncio.run(run())
