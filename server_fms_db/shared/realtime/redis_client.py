"""Small async Redis foundation for realtime cache and Pub/Sub use cases.

Redis is auxiliary infrastructure only. Durable production state remains in
PostgreSQL; this module never models or persists Jobs, Steps, or deliveries.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from shared.config import get_settings

logger = logging.getLogger(__name__)


class RedisDependencyUnavailableError(RuntimeError):
    """redis-py is missing from the process environment."""


class RedisUnavailableError(RuntimeError):
    """Redis could not complete an auxiliary realtime operation."""


class RedisJsonSerializationError(ValueError):
    """A realtime payload was not valid JSON data."""


class RealtimeRedisClient:
    """Reusable async client with bounded, non-fatal health checks.

    Construction never connects. A process that does not need realtime features
    can therefore start normally while Redis is unavailable. Callers can treat
    ``ping() is False`` as degraded realtime capability without changing any
    PostgreSQL-backed production operation.
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        socket_connect_timeout_seconds: float = 1.0,
        socket_timeout_seconds: float = 1.0,
        client: Any | None = None,
    ) -> None:
        self.redis_url = redis_url or get_settings().redis_url
        self._closed = False
        if client is not None:
            self._client = client
            self._redis_error = Exception
            return

        try:
            from redis.asyncio import Redis
            from redis.exceptions import RedisError
        except ImportError as exc:  # pragma: no cover - before dependency installation only
            raise RedisDependencyUnavailableError(
                "redis-py is unavailable; install requirements.txt before using realtime Redis."
            ) from exc

        self._redis_error = RedisError
        self._client = Redis.from_url(
            self.redis_url,
            decode_responses=True,
            socket_connect_timeout=socket_connect_timeout_seconds,
            socket_timeout=socket_timeout_seconds,
        )

    @property
    def raw_client(self) -> Any:
        """redis-py's async client, exposed for future Pub/Sub subscribers."""

        return self._client

    async def ping(self) -> bool:
        """Return availability without allowing a cache outage to crash a process."""

        if self._closed:
            return False
        try:
            return bool(await self._client.ping())
        except (self._redis_error, OSError) as exc:
            logger.warning("Redis unavailable at %s: %s", self.redis_url, exc)
            return False

    async def set_json(self, key: str, payload: Mapping[str, Any] | list[Any]) -> None:
        """Store JSON for a caller-owned realtime cache key."""

        await self._execute("SET", self._client.set(key, self._encode_json(payload)))

    async def get_json(self, key: str) -> Any | None:
        """Read and decode a JSON cache value; missing keys remain ``None``."""

        value = await self._execute("GET", self._client.get(key))
        if value is None:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise RedisJsonSerializationError(f"Redis key {key!r} did not contain valid JSON.") from exc

    async def publish_json(self, channel: str, payload: Mapping[str, Any] | list[Any]) -> int:
        """Publish one JSON message and return Redis' subscriber count."""

        return int(await self._execute("PUBLISH", self._client.publish(channel, self._encode_json(payload))))

    async def delete(self, *keys: str) -> int:
        """Delete caller-owned keys, useful for bounded test cleanup."""

        if not keys:
            return 0
        return int(await self._execute("DEL", self._client.delete(*keys)))

    async def close(self) -> None:
        """Close the async connection pool; safe to call repeatedly."""

        if self._closed:
            return
        self._closed = True
        try:
            await self._client.aclose()
        except (self._redis_error, OSError) as exc:
            logger.debug("Redis close failed at %s: %s", self.redis_url, exc)

    @staticmethod
    def _encode_json(payload: Mapping[str, Any] | list[Any]) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise RedisJsonSerializationError("Realtime payload must be JSON serializable.") from exc

    async def _execute(self, operation: str, awaitable: Any) -> Any:
        if self._closed:
            raise RedisUnavailableError("Redis client is closed.")
        try:
            return await awaitable
        except (self._redis_error, OSError) as exc:
            raise RedisUnavailableError(
                f"Redis {operation} failed at {self.redis_url}: {exc}"
            ) from exc


def create_realtime_redis_client(*, redis_url: str | None = None) -> RealtimeRedisClient:
    """Create a client from ``REDIS_URL`` without opening a connection yet."""

    return RealtimeRedisClient(redis_url=redis_url)
