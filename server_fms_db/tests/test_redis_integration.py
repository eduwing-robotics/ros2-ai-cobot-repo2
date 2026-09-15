"""Opt-in integration coverage against a local Redis server."""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

if os.getenv("RUN_REDIS_INTEGRATION") != "1":
    pytest.skip("Set RUN_REDIS_INTEGRATION=1 to run Redis integration tests.", allow_module_level=True)

from shared.realtime.redis_client import create_realtime_redis_client


@pytest.mark.redis_integration
def test_redis_ping_set_get_publish_subscribe_and_close() -> None:
    async def run() -> None:
        client = create_realtime_redis_client()
        key = f"smart_factory:test:redis:{uuid.uuid4().hex}"
        channel = f"smart_factory.test.events.{uuid.uuid4().hex}"
        pubsub = client.raw_client.pubsub()
        try:
            assert await client.ping() is True
            await client.set_json(key, {"source": "redis-integration", "count": 1})
            assert await client.get_json(key) == {"source": "redis-integration", "count": 1}

            await pubsub.subscribe(channel)
            await pubsub.get_message(timeout=1.0)
            assert await client.publish_json(channel, {"event": "hello"}) >= 1
            for _ in range(20):
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                if message is not None:
                    assert json.loads(message["data"]) == {"event": "hello"}
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Redis Pub/Sub message was not received.")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await client.delete(key)
            await client.close()

    asyncio.run(run())
