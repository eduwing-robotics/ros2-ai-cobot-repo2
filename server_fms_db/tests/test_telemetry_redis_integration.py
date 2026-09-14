"""Opt-in Redis verification of the telemetry latest-state and Pub/Sub boundary."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

if os.getenv("RUN_REDIS_INTEGRATION") != "1":
    pytest.skip("Set RUN_REDIS_INTEGRATION=1 to run telemetry Redis integration tests.", allow_module_level=True)

from shared.realtime.redis_client import create_realtime_redis_client
from telemetry_gateway.telemetry import LatestValueTelemetryHandoff, RedisTelemetrySink, TelemetryUpdate


@pytest.mark.redis_integration
def test_normalized_telemetry_reaches_latest_key_and_pubsub() -> None:
    async def run() -> None:
        suffix = uuid.uuid4().hex
        key_namespace = f"smart_factory:test:telemetry:{suffix}"
        channel_prefix = f"smart_factory.test.{suffix}."
        client = create_realtime_redis_client()
        sink = RedisTelemetrySink(client, key_namespace=key_namespace, channel_prefix=channel_prefix)
        handoff = LatestValueTelemetryHandoff(sink)
        payload = {
            "robot_id": "zkbot1",
            "joint_names": ["a1_joint", "a2_joint", "a3_joint"],
            "positions": [0.1, 0.2, 0.3],
            "velocities": [],
            "efforts": [],
            "source_timestamp": None,
            "received_at": "2026-08-13T00:00:00.000Z",
        }
        update = TelemetryUpdate("joint_state", "zkbot1", payload)
        key = sink.key_for(update)
        channel = sink.channel_for(update)
        pubsub = client.raw_client.pubsub()
        try:
            assert await client.ping() is True
            await pubsub.subscribe(channel)
            await pubsub.get_message(timeout=1.0)
            handoff.offer(update)
            assert await handoff.flush_due() == 1
            assert await client.get_json(key) == payload
            for _ in range(20):
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                if message is not None:
                    assert json.loads(message["data"]) == payload
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Telemetry Pub/Sub payload was not received.")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await client.delete(key)
            await client.close()

    asyncio.run(run())
