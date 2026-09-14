"""FMS-owned, non-blocking Redis publisher for committed production changes."""

from __future__ import annotations

import asyncio
import logging

from shared.realtime.production_events import PRODUCTION_CHANGED_CHANNEL, production_changed_payload
from shared.realtime.redis_client import RealtimeRedisClient, create_realtime_redis_client

logger = logging.getLogger(__name__)


class ProductionChangedPublisher:
    """Best-effort queue; a Redis outage never changes a committed DB result."""

    def __init__(self, *, client_factory=create_realtime_redis_client, queue_size: int = 128) -> None:
        self._client_factory = client_factory
        self._queue: asyncio.Queue[tuple[int, str | None]] = asyncio.Queue(maxsize=queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fms-production-changed-publisher")

    def notify_after_commit(self, job_id: int, reason: str | None = None) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, job_id, reason)

    def _offer(self, job_id: int, reason: str | None) -> None:
        try:
            self._queue.put_nowait((job_id, reason))
        except asyncio.QueueFull:
            logger.warning("Dropping best-effort production.changed notification: queue full.")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._loop = None

    async def _run(self) -> None:
        client: RealtimeRedisClient | None = None
        try:
            client = self._client_factory()
            while True:
                job_id, reason = await self._queue.get()
                try:
                    await client.publish_json(
                        PRODUCTION_CHANGED_CHANNEL,
                        production_changed_payload(job_id=job_id, reason=reason),
                    )
                except Exception as exc:
                    logger.warning("Could not publish committed production change job_id=%s: %s", job_id, exc)
        finally:
            if client is not None:
                await client.close()
