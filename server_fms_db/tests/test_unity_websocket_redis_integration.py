from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from api_server.main import app
from shared.config import get_settings
from shared.realtime.redis_client import create_realtime_redis_client
from telemetry_gateway.telemetry import JOINT_STATE_CHANNEL

if os.getenv("RUN_REDIS_INTEGRATION") != "1":
    pytest.skip("Set RUN_REDIS_INTEGRATION=1 to run Unity WebSocket Redis integration tests.", allow_module_level=True)


@pytest.mark.redis_integration
def test_redis_pubsub_reaches_unity_websocket_after_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    suffix = uuid.uuid4().hex
    prefix = f"smart_factory.test.{suffix}."
    monkeypatch.setenv("UNITY_REDIS_CHANNEL_PREFIX", prefix)
    get_settings.cache_clear()
    app.state.unity_snapshot_provider = lambda: {
        "jobs": [], "robots": [], "transports": [], "active_errors": []
    }
    try:
        with TestClient(app) as test_client:
            deadline = time.monotonic() + 2.0
            while app.state.unity_subscriber.available is not True and time.monotonic() < deadline:
                time.sleep(0.01)
            assert app.state.unity_subscriber.available is True
            with test_client.websocket_connect("/ws/unity") as websocket:
                snapshot = websocket.receive_json()
                assert snapshot["type"] == "production_snapshot"
                assert snapshot["sequence"] == 1
                assert snapshot["data"] == {
                    "jobs": [], "robots": [], "transports": [], "active_errors": []
                }

                payload = {
                    "robot_id": "test_fr5",
                    "joint_names": ["joint_1"],
                    "positions": [1.25],
                    "velocities": [],
                    "efforts": [],
                    "source_timestamp": "2026-08-14T00:00:00.000Z",
                    "received_at": "2026-08-14T00:00:01.000Z",
                }
                async def publish() -> None:
                    redis_client = create_realtime_redis_client()
                    try:
                        await redis_client.publish_json(f"{prefix}{JOINT_STATE_CHANNEL}", payload)
                    finally:
                        await redis_client.close()

                asyncio.run(publish())
                joint_state = websocket.receive_json()
                assert joint_state["type"] == "robot_joint_state"
                assert joint_state["sequence"] == 2
                assert joint_state["data"] == {
                    "robot_id": "test_fr5",
                    "joint_names": ["joint_1"],
                    "positions": [1.25],
                    "velocities": [],
                    "efforts": [],
                    "source_timestamp": "2026-08-14T00:00:00.000Z",
                }
    finally:
        app.state.__dict__.pop("unity_snapshot_provider", None)
        get_settings.cache_clear()
