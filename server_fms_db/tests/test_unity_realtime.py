from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field

from api_server.services.unity_realtime import (
    JOINT_STATE_CHANNEL,
    MOBILE_ROBOT_POSE_CHANNEL,
    STATUS_CHANNEL,
    UnityRealtimeHub,
)


@dataclass(eq=False)
class FakeWebSocket:
    accepted: bool = False
    messages: list[dict] = field(default_factory=list)

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def snapshot() -> dict:
    return {"jobs": [], "robots": [], "transports": [], "incoming_qa": [], "production_inspections": [], "active_errors": []}


def joint(robot_id: str, position: float) -> dict:
    return {
        "robot_id": robot_id,
        "joint_names": ["joint_1"],
        "positions": [position],
        "velocities": [],
        "efforts": [],
        "source_timestamp": "2026-08-14T00:00:00.000Z",
        "received_at": "2026-08-14T00:00:01.000Z",
    }



def mobile_pose(*, x: float = 1.25) -> dict:
    return {
        "robot_id": "forklift_01",
        "frame_id": "map",
        "timestamp": "2026-09-07T01:02:03.456Z",
        "position": {"x": x, "y": -0.5, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        "received_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def test_mobile_robot_pose_uses_existing_unity_endpoint_envelope_and_reconnect_cache() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        await hub.ingest(MOBILE_ROBOT_POSE_CHANNEL, mobile_pose())
        first = FakeWebSocket()
        await hub.connect(first)
        assert [message["type"] for message in first.messages] == [
            "production_snapshot", "mobile_robot_pose", "robot_status"
        ]
        pose_envelope = first.messages[1]
        assert pose_envelope["schema_version"] == "1.0"
        assert pose_envelope["data"] == {
            "robot_id": "forklift_01",
            "frame_id": "map",
            "timestamp": "2026-09-07T01:02:03.456Z",
            "position": {"x": 1.25, "y": -0.5, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        }
        status = first.messages[-1]["data"]
        assert status["robot_id"] == "forklift_01"
        assert status["connected"] is True
        assert status["connection_state"] == "ONLINE"
        assert status["activity_state"] == "IDLE"
        assert status["status"] == "IDLE"
        assert status["pose_age_s"] is not None and status["pose_age_s"] <= 1.0
        second = FakeWebSocket()
        await hub.connect(second)
        assert [message["type"] for message in second.messages] == [
            "production_snapshot", "mobile_robot_pose", "robot_status"
        ]
        assert second.messages[1]["data"] == pose_envelope["data"]
        assert second.messages[-1]["data"]["activity_state"] == "IDLE"

    asyncio.run(run())


def test_mobile_robot_status_tracks_fms_transport_activity_without_changing_pose_stream() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        await hub.ingest(MOBILE_ROBOT_POSE_CHANNEL, mobile_pose())
        await asyncio.sleep(0)
        await hub.publish_transport_status({
            "req_id": "transport-1", "job_id": 1, "delivery_id": 2,
            "robot_id": "forklift_01", "task_type": "EXECUTE_TRANSPORT",
            "phase": "MOVING_TO_PICKUP", "progress": 0.25,
            "result": None, "error_code": None, "detail": "moving",
        })
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        working = [message["data"] for message in socket.messages if message["type"] == "robot_status"][-1]
        assert working["connection_state"] == "ONLINE"
        assert working["activity_state"] == "WORKING"
        assert working["status"] == "WORKING"
        assert working["transport_task_type"] == "EXECUTE_TRANSPORT"
        assert working["transport_phase"] == "MOVING_TO_PICKUP"

        await hub.publish_transport_status({
            "req_id": "transport-1", "job_id": 1, "delivery_id": 2,
            "robot_id": "forklift_01", "task_type": "EXECUTE_TRANSPORT",
            "phase": "MOVING_TO_DROPOFF", "progress": 1.0,
            "result": "SUCCEEDED", "error_code": None, "detail": "done",
        })
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        idle = [message["data"] for message in socket.messages if message["type"] == "robot_status"][-1]
        assert idle["activity_state"] == "IDLE"
        assert idle["status"] == "IDLE"
        assert "transport_phase" not in idle

    asyncio.run(run())


def test_mobile_robot_status_marks_stale_pose_connection_lost() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        stale = mobile_pose()
        stale["received_at"] = (datetime.now(UTC) - timedelta(seconds=1.1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        await hub.ingest(MOBILE_ROBOT_POSE_CHANNEL, stale)
        socket = FakeWebSocket()
        await hub.connect(socket)
        status = socket.messages[-1]
        assert status["type"] == "robot_status"
        assert status["data"]["connected"] is False
        assert status["data"]["connection_state"] == "CONNECTION_LOST"
        assert status["data"]["status"] == "CONNECTION_LOST"
        assert status["data"]["activity_state"] == "IDLE"

    asyncio.run(run())


def test_voice_event_uses_existing_unity_queue_and_snapshot_reconnect_projection() -> None:
    async def run() -> None:
        voice = {
            "state": "IDLE", "runtime_status": "READY", "current_turn": None,
            "recent_turns": [{"turn_id": "old", "state": "COMPLETED", "timestamp": "now", "transcript": "재고 알려줘", "response_text": "재고입니다.", "intent": "QUERY_INVENTORY", "runtime_status": "READY", "error_display": None}],
        }
        hub = UnityRealtimeHub(snapshot, voice_snapshot=lambda: voice)
        first = FakeWebSocket()
        await hub.connect(first)
        assert first.messages[0]["data"]["voice_runtime"] == voice
        await hub.publish_voice_event({
            "turn_id": "voice-1", "state": "INTERPRETING", "runtime_status": "PROCESSING",
            "transcript": "현재 무슨 작업 중이야?", "response_text": None,
            "intent": None, "error_display": None,
        })
        await asyncio.sleep(0)
        assert first.messages[-1]["type"] == "voice_runtime_event"
        assert first.messages[-1]["data"]["transcript"] == "현재 무슨 작업 중이야?"
        second = FakeWebSocket()
        await hub.connect(second)
        assert second.messages[0]["data"]["voice_runtime"] == voice

    asyncio.run(run())


def test_invalid_mobile_robot_pose_is_not_fanned_out() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        invalid = mobile_pose()
        invalid["orientation"]["w"] = float("nan")
        await hub.ingest(MOBILE_ROBOT_POSE_CHANNEL, invalid)
        await asyncio.sleep(0)
        assert [message["type"] for message in socket.messages] == ["production_snapshot"]

    asyncio.run(run())

def test_unity_envelope_snapshot_first_and_connection_wide_sequence() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        # Existing mirror becomes initial telemetry after snapshot.
        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 1.0))
        socket = FakeWebSocket()
        await hub.connect(socket)
        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot1", "connected": True, "busy": False})
        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot1", "ready": True, "received_at": "now"})
        await asyncio.sleep(0)

        assert socket.accepted is True
        assert [message["type"] for message in socket.messages] == [
            "production_snapshot", "robot_joint_state", "robot_status"
        ]
        assert [message["sequence"] for message in socket.messages] == [1, 2, 3]
        assert all(message["schema_version"] == "1.0" for message in socket.messages)
        assert socket.messages[1]["data"]["source_timestamp"] == "2026-08-14T00:00:00.000Z"
        assert socket.messages[-1]["data"] == {
            "robot_id": "zkbot1", "connected": True, "ready": True, "busy": False
        }

    asyncio.run(run())


def test_unity_connections_have_independent_sequences_and_missing_data_is_not_fabricated() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        first = FakeWebSocket()
        second = FakeWebSocket()
        await hub.connect(first)
        await hub.connect(second)

        assert [message["sequence"] for message in first.messages] == [1]
        assert [message["sequence"] for message in second.messages] == [1]
        assert [message["type"] for message in first.messages] == ["production_snapshot"]
        assert [message["type"] for message in second.messages] == ["production_snapshot"]
        assert first.messages[0]["data"] == second.messages[0]["data"] == {"jobs": [], "robots": [], "transports": [], "incoming_qa": [], "production_inspections": [], "active_errors": []}
        await hub.ingest(JOINT_STATE_CHANNEL, joint("zkbot2", 2.0))
        await asyncio.sleep(0)
        assert [message["sequence"] for message in first.messages] == [1, 2]
        assert [message["sequence"] for message in second.messages] == [1, 2]

    asyncio.run(run())


@dataclass(eq=False)
class SnapshotGateWebSocket(FakeWebSocket):
    snapshot_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_snapshot: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_json(self, payload: dict) -> None:
        if payload["type"] == "production_snapshot":
            self.snapshot_started.set()
            await self.release_snapshot.wait()
        self.messages.append(payload)


def test_syncing_connection_coalesces_realtime_until_snapshot_has_been_sent() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        socket = SnapshotGateWebSocket()
        connection_task = asyncio.create_task(hub.connect(socket))
        await socket.snapshot_started.wait()
        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 5.0))
        socket.release_snapshot.set()
        await connection_task

        assert socket.messages[0]["type"] == "production_snapshot"
        assert socket.messages[0]["sequence"] == 1
        assert socket.messages[-1]["type"] == "robot_joint_state"
        assert socket.messages[-1]["sequence"] == 2

    asyncio.run(run())


@dataclass(eq=False)
class SlowWebSocket(FakeWebSocket):
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_json(self, payload: dict) -> None:
        if payload["type"] != "production_snapshot":
            await self.release.wait()
        self.messages.append(payload)


def test_slow_unity_client_coalesces_telemetry_without_blocking_fast_client() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        slow = SlowWebSocket()
        fast = FakeWebSocket()
        await hub.connect(slow)
        await hub.connect(fast)

        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 1.0))
        await asyncio.sleep(0)
        assert [message["sequence"] for message in fast.messages] == [1, 2]
        assert [message["sequence"] for message in slow.messages] == [1]

        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 2.0))
        slow.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert slow.messages[-1]["data"]["positions"] == [2.0]
        assert 2 <= len(slow.messages) <= 3
        assert slow.messages[0]["sequence"] == 1

    asyncio.run(run())


def test_initial_sync_sends_each_cached_robot_once_after_snapshot() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 1.0))
        await hub.ingest(JOINT_STATE_CHANNEL, joint("zkbot1", 2.0))
        await hub.ingest(JOINT_STATE_CHANNEL, joint("zkbot2", 3.0))
        socket = FakeWebSocket()

        await hub.connect(socket)

        assert [message["type"] for message in socket.messages] == [
            "production_snapshot", "robot_joint_state", "robot_joint_state", "robot_joint_state"
        ]
        assert [message["data"].get("robot_id") for message in socket.messages[1:]] == [
            "fr5", "zkbot1", "zkbot2"
        ]
        assert [message["sequence"] for message in socket.messages] == [1, 2, 3, 4]

    asyncio.run(run())


def test_live_joint_update_sends_only_the_changed_robot() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        for robot_id, position in (("fr5", 1.0), ("zkbot1", 2.0), ("zkbot2", 3.0)):
            await hub.ingest(JOINT_STATE_CHANNEL, joint(robot_id, position))
        socket = FakeWebSocket()
        await hub.connect(socket)
        initial_count = len(socket.messages)

        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 4.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        live = socket.messages[initial_count:]
        assert [(message["type"], message["data"]["robot_id"]) for message in live] == [
            ("robot_joint_state", "fr5")
        ]

    asyncio.run(run())


def test_live_joint_updates_coalesce_per_robot_without_replaying_cached_zk() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        for robot_id, position in (("fr5", 1.0), ("zkbot1", 2.0), ("zkbot2", 3.0)):
            await hub.ingest(JOINT_STATE_CHANNEL, joint(robot_id, position))
        socket = FakeWebSocket()
        await hub.connect(socket)
        initial_count = len(socket.messages)

        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 4.0))
        await hub.ingest(JOINT_STATE_CHANNEL, joint("fr5", 5.0))
        await hub.ingest(JOINT_STATE_CHANNEL, joint("zkbot1", 6.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        live = socket.messages[initial_count:]
        assert [(message["data"]["robot_id"], message["data"]["positions"]) for message in live] == [
            ("fr5", [5.0]), ("zkbot1", [6.0])
        ]

    asyncio.run(run())


def test_fr5_gripper_status_is_additive_and_hydrates_on_unity_reconnect() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        status = {
            "robot_id": "fr5", "connected": True,
            "grip": 40, "grip_real": None, "grip_real_age_s": 31.2,
            "unexpected": "not a Unity contract field",
        }
        hub.hydrate(joint_states=[], statuses=[status], readies=[])
        socket = FakeWebSocket()
        await hub.connect(socket)

        assert socket.messages[0]["data"]["robots"] == [{
            "robot_id": "fr5", "connected": True,
            "grip": 40, "grip_real": None, "grip_real_age_s": 31.2,
        }]
        assert socket.messages[1]["type"] == "robot_status"
        assert socket.messages[1]["data"] == socket.messages[0]["data"]["robots"][0]

        await hub.ingest(STATUS_CHANNEL, {
            "robot_id": "fr5", "grip": None, "grip_real": None, "grip_real_age_s": None,
        })
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert socket.messages[-1]["data"] == {
            "robot_id": "fr5", "grip": None, "grip_real": None, "grip_real_age_s": None,
        }

    asyncio.run(run())


def test_live_status_update_sends_only_the_changed_robot() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(snapshot)
        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot1", "connected": True, "busy": False})
        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot1", "ready": True, "received_at": "now"})
        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot2", "connected": True, "busy": False})
        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot2", "ready": True, "received_at": "now"})
        socket = FakeWebSocket()
        await hub.connect(socket)
        initial_count = len(socket.messages)

        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot1", "connected": False, "busy": True})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first_live = socket.messages[initial_count:]
        assert [(message["type"], message["data"]["robot_id"]) for message in first_live] == [
            ("robot_status", "zkbot1")
        ]
        assert first_live[0]["data"] == {"robot_id": "zkbot1", "connected": False, "ready": True, "busy": True}

        initial_count = len(socket.messages)
        await hub.ingest(STATUS_CHANNEL, {"robot_id": "zkbot1", "ready": False, "received_at": "later"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        second_live = socket.messages[initial_count:]
        assert [(message["type"], message["data"]["robot_id"]) for message in second_live] == [
            ("robot_status", "zkbot1")
        ]
        assert second_live[0]["data"]["ready"] is False

    asyncio.run(run())



def test_unity_current_stage_code_is_preserved_in_snapshot_and_production_status() -> None:
    async def run() -> None:
        job = {
            "job_id": 42,
            "job_code": "HOUSE-B-UNITY-STAGE",
            "status": "PRE_ROOF_READY",
            "current_stage_code": "PRE_ROOF_INSPECTION",
        }
        hub = UnityRealtimeHub(lambda: {
            "jobs": [job], "robots": [], "transports": [], "active_errors": []
        })
        socket = FakeWebSocket()
        await hub.connect(socket)
        await hub.publish_production_status(dict(job))
        await asyncio.sleep(0)

        snapshot_message, status_message = socket.messages
        assert snapshot_message["type"] == "production_snapshot"
        assert snapshot_message["data"]["jobs"][0]["current_stage_code"] == "PRE_ROOF_INSPECTION"
        assert status_message["type"] == "production_status"
        assert status_message["data"]["current_stage_code"] == "PRE_ROOF_INSPECTION"
        assert all(message["schema_version"] == "1.0" for message in socket.messages)

    asyncio.run(run())
