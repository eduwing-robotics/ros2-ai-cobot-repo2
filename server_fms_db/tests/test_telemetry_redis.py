from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from shared.realtime.redis_client import RedisUnavailableError
from telemetry_gateway.telemetry import (
    FR5_MIN_OUTPUT_INTERVAL_SECONDS,
    LatestValueTelemetryHandoff,
    MOBILE_ROBOT_POSE_CHANNEL,
    RedisTelemetrySink,
    TelemetryUpdate,
    normalize_fr5_cell_status,
    normalize_joint_state,
    normalize_mobile_robot_pose,
    normalize_zk_ready,
    normalize_zk_status,
    ros_stamp_to_rfc3339,
)


@dataclass
class MutableClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


class FakeSink:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.updates: list[TelemetryUpdate] = []

    async def publish(self, update: TelemetryUpdate) -> None:
        if self.failures:
            self.failures -= 1
            raise RedisUnavailableError("test redis down")
        self.updates.append(update)


def joint_message(*, velocity: list[float] | None = None, effort: list[float] | None = None, sec: int = 0, nanosec: int = 0):
    return SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nanosec)),
        name=["a1_joint", "a2_joint"],
        position=[1.0, 2.0],
        velocity=[] if velocity is None else velocity,
        effort=[] if effort is None else effort,
    )



def mobile_pose_message(*, frame_id: str = "map", sec: int = 1_700_000_000, nanosec: int = 123_000_000, x: float = 1.0, y: float = 2.0, z: float = 0.0, qx: float = 0.0, qy: float = 0.0, qz: float = 0.0, qw: float = 1.0):
    return SimpleNamespace(
        header=SimpleNamespace(frame_id=frame_id, stamp=SimpleNamespace(sec=sec, nanosec=nanosec)),
        pose=SimpleNamespace(
            position=SimpleNamespace(x=x, y=y, z=z),
            orientation=SimpleNamespace(x=qx, y=qy, z=qz, w=qw),
        ),
    )


def test_mobile_robot_pose_normalization_preserves_map_meters_quaternion_and_ros_timestamp() -> None:
    payload = normalize_mobile_robot_pose(
        mobile_pose_message(),
        robot_id="forklift_01",
        received_at="2026-09-07T01:02:03.456Z",
    )
    assert payload == {
        "robot_id": "forklift_01",
        "frame_id": "map",
        "timestamp": "2023-11-14T22:13:20.123Z",
        "position": {"x": 1.0, "y": 2.0, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "received_at": "2026-09-07T01:02:03.456Z",
    }


@pytest.mark.parametrize("message", [
    mobile_pose_message(frame_id=""),
    mobile_pose_message(x=float("nan")),
    mobile_pose_message(qw=float("inf")),
    mobile_pose_message(qx=0.0, qy=0.0, qz=0.0, qw=0.0),
    mobile_pose_message(sec=0, nanosec=0),
])
def test_mobile_robot_pose_rejects_invalid_contract_samples(message) -> None:
    assert normalize_mobile_robot_pose(message, robot_id="forklift_01") is None

def test_joint_state_normalization_preserves_order_empty_arrays_and_timestamp() -> None:
    payload = normalize_joint_state(
        joint_message(sec=1_700_000_000, nanosec=123_000_000),
        robot_id="zkbot1",
        received_at="2026-08-13T01:02:03.456Z",
    )

    assert payload == {
        "robot_id": "zkbot1",
        "joint_names": ["a1_joint", "a2_joint"],
        "positions": [1.0, 2.0],
        "velocities": [],
        "efforts": [],
        "source_timestamp": "2023-11-14T22:13:20.123Z",
        "received_at": "2026-08-13T01:02:03.456Z",
    }
    assert ros_stamp_to_rfc3339(SimpleNamespace(sec=0, nanosec=0)) is None


def test_status_and_ready_normalization_keep_connected_and_ready_separate() -> None:
    status = normalize_zk_status(
        '{"robot":"wrong-name","online":true,"ready":false,"busy":false}',
        robot_id="zkbot1",
        received_at="2026-08-13T01:02:03.456Z",
    )
    ready = normalize_zk_ready(True, robot_id="zkbot1", received_at="2026-08-13T01:02:04.456Z")

    assert status["robot_id"] == "zkbot1"
    assert status["connected"] is True
    assert status["ready"] is False
    assert ready == {"robot_id": "zkbot1", "ready": True, "received_at": "2026-08-13T01:02:04.456Z"}


def test_invalid_zk_status_json_is_diagnostic_not_a_crash() -> None:
    payload = normalize_zk_status("not-json", robot_id="zkbot2", received_at="2026-08-13T01:02:03.456Z")
    assert payload == {
        "robot_id": "zkbot2",
        "received_at": "2026-08-13T01:02:03.456Z",
        "valid_json": False,
        "raw": "not-json",
    }



def cell_status_message(*, fr5: dict[str, object] | None = None, ver: str = "0.3") -> str:
    return json.dumps({
        "ver": ver,
        "seq": 1,
        "ts": "2026-09-10T01:02:03.456Z",
        "cell_state": "IDLE",
        "active_task": None,
        "hold": None,
        "robots": {"fr5": fr5 or {}},
        "conveyor": {},
        "error": None,
    })


@pytest.mark.parametrize(("fr5", "expected"), [
    ({"connected": True, "grip": 40, "grip_real": 39, "grip_real_age_s": 0.2},
     {"robot_id": "fr5", "connected": True, "grip": 40, "grip_real": 39, "grip_real_age_s": 0.2,
      "received_at": "2026-09-10T02:03:04.567Z"}),
    ({"grip": 40, "grip_real": None, "grip_real_age_s": 31.2},
     {"robot_id": "fr5", "grip": 40, "grip_real": None, "grip_real_age_s": 31.2,
      "received_at": "2026-09-10T02:03:04.567Z"}),
    ({"grip": None, "grip_real": None, "grip_real_age_s": None},
     {"robot_id": "fr5", "grip": None, "grip_real": None, "grip_real_age_s": None,
      "received_at": "2026-09-10T02:03:04.567Z"}),
    ({"grip": 0, "grip_real": 100, "grip_real_age_s": 0},
     {"robot_id": "fr5", "grip": 0, "grip_real": 100, "grip_real_age_s": 0.0,
      "received_at": "2026-09-10T02:03:04.567Z"}),
])
def test_fr5_cell_status_normalization_preserves_gripper_contract(
    fr5: dict[str, object], expected: dict[str, object]
) -> None:
    assert normalize_fr5_cell_status(
        cell_status_message(fr5=fr5), received_at="2026-09-10T02:03:04.567Z"
    ) == expected


def test_fr5_cell_status_is_backward_compatible_and_invalid_optional_values_are_safe() -> None:
    assert normalize_fr5_cell_status(
        cell_status_message(ver="0.2"), received_at="now"
    ) == {
        "robot_id": "fr5", "grip": None, "grip_real": None, "grip_real_age_s": None, "received_at": "now"
    }
    payload = normalize_fr5_cell_status(
        cell_status_message(fr5={"grip": 101, "grip_real": True, "grip_real_age_s": -1}), received_at="now"
    )
    assert payload == {
        "robot_id": "fr5", "grip": None, "grip_real": None, "grip_real_age_s": None, "received_at": "now"
    }


def test_mobile_pose_uses_dedicated_latest_redis_key_and_pubsub_channel() -> None:
    sink = RedisTelemetrySink(object())
    update = TelemetryUpdate("mobile_robot_pose", "forklift_01", {"robot_id": "forklift_01"})
    assert sink.key_for(update) == "telemetry:robot:forklift_01:mobile_robot_pose"
    assert sink.channel_for(update) == MOBILE_ROBOT_POSE_CHANNEL


def test_ros_subscriber_module_is_import_safe_without_starting_ros() -> None:
    # Importing the optional ROS boundary must not require rclpy/generated messages.
    from telemetry_gateway.ros_subscriber import RosTelemetrySubscriber

    submitted = []

    class ImmediateLoop:
        def call_soon_threadsafe(self, callback, *args):
            callback(*args)

    subscriber = RosTelemetrySubscriber(loop=ImmediateLoop(), offer_update=submitted.append)
    subscriber._mobile_pose_callback("forklift_01")(mobile_pose_message())
    subscriber._cell_status_callback()(SimpleNamespace(data=cell_status_message(
        fr5={"grip": 40, "grip_real": None, "grip_real_age_s": 31.2}
    )))
    assert [update.kind for update in submitted] == ["mobile_robot_pose", "status"]
    assert submitted[0].payload["robot_id"] == "forklift_01"
    assert submitted[1].payload == {
        "robot_id": "fr5", "grip": 40, "grip_real": None, "grip_real_age_s": 31.2,
        "received_at": submitted[1].payload["received_at"],
    }

def test_telemetry_ros_disabled_does_not_construct_any_ros_subscriber(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import Settings
    from telemetry_gateway import main as telemetry_main

    class FakeRedisClient:
        async def close(self) -> None:
            pass

    monkeypatch.setattr(telemetry_main, "create_realtime_redis_client", lambda **_kwargs: FakeRedisClient())
    monkeypatch.setattr(
        telemetry_main,
        "RosTelemetrySubscriber",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("ROS must stay disabled")),
    )

    async def run() -> None:
        runtime = telemetry_main.TelemetryGatewayRuntime(
            Settings(cell_transport="fake", telemetry_ros_enabled=False)
        )
        await runtime.start()
        try:
            assert runtime.ros_running is False
        finally:
            await runtime.stop()

    asyncio.run(run())


def test_fr5_throttle_is_latest_value_wins_without_upsampling_zk() -> None:
    async def run() -> None:
        clock = MutableClock()
        sink = FakeSink()
        handoff = LatestValueTelemetryHandoff(sink, monotonic=clock)

        handoff.offer(TelemetryUpdate("joint_state", "fr5", {"positions": [1]}))
        assert await handoff.flush_due() == 1
        clock.value += FR5_MIN_OUTPUT_INTERVAL_SECONDS / 2
        handoff.offer(TelemetryUpdate("joint_state", "fr5", {"positions": [2]}))
        handoff.offer(TelemetryUpdate("joint_state", "fr5", {"positions": [3]}))
        assert await handoff.flush_due() == 0
        clock.value += FR5_MIN_OUTPUT_INTERVAL_SECONDS / 2
        assert await handoff.flush_due() == 1

        handoff.offer(TelemetryUpdate("joint_state", "zkbot1", {"positions": [10]}))
        assert await handoff.flush_due() == 1
        clock.value += 10
        assert await handoff.flush_due() == 0

        assert [update.payload["positions"] for update in sink.updates] == [[1], [3], [10]]

    asyncio.run(run())


def test_redis_failure_keeps_latest_pending_then_recovers() -> None:
    async def run() -> None:
        clock = MutableClock()
        sink = FakeSink(failures=1)
        handoff = LatestValueTelemetryHandoff(sink, monotonic=clock, redis_retry_seconds=1.0)
        handoff.offer(TelemetryUpdate("status", "zkbot1", {"connected": True}))

        assert await handoff.flush_due() == 0
        assert handoff.next_wait_seconds() == pytest.approx(1.0)
        clock.value = 1.0
        assert await handoff.flush_due() == 1
        assert sink.updates[0].payload == {"connected": True}

    asyncio.run(run())
