import asyncio

import pytest

from shared.realtime.redis_client import RedisJsonSerializationError, RealtimeRedisClient


def test_json_encoding_is_compact_and_round_trippable() -> None:
    encoded = RealtimeRedisClient._encode_json({"robot_id": "zk2", "ready": True})
    assert encoded == '{"robot_id":"zk2","ready":true}'


def test_json_encoding_rejects_non_serializable_values() -> None:
    with pytest.raises(RedisJsonSerializationError):
        RealtimeRedisClient._encode_json({"unsupported": object()})


def test_unavailable_redis_ping_is_nonfatal() -> None:
    async def run() -> None:
        client = RealtimeRedisClient(
            redis_url="redis://127.0.0.1:63991/0",
            socket_connect_timeout_seconds=0.05,
            socket_timeout_seconds=0.05,
        )
        try:
            assert await client.ping() is False
        finally:
            await client.close()

    asyncio.run(run())
