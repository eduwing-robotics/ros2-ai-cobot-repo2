"""Best-effort Redis identity publisher for committed ProductionInspection changes."""

from __future__ import annotations

import asyncio
import logging

from shared.realtime.production_inspection_events import (
    PRODUCTION_INSPECTION_CHANGED_CHANNEL,
    production_inspection_changed_payload,
)
from shared.realtime.redis_client import RealtimeRedisClient, create_realtime_redis_client

logger = logging.getLogger(__name__)


class ProductionInspectionChangedPublisher:
    """Redis is secondary observability; a failed publish never rolls back DB."""

    def __init__(self, *, client_factory=create_realtime_redis_client, queue_size: int = 128) -> None:
        self._client_factory = client_factory
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="production-inspection-changed-publisher")

    def notify_after_commit(self, inspection_id: int) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, inspection_id)

    def _offer(self, inspection_id: int) -> None:
        if not isinstance(inspection_id, int) or inspection_id < 1:
            logger.warning("Ignoring ProductionInspection change without a valid inspection_id.")
            return
        try:
            self._queue.put_nowait(inspection_id)
        except asyncio.QueueFull:
            logger.warning("Dropping best-effort ProductionInspection notification: queue full.")

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
                inspection_id = await self._queue.get()
                try:
                    await client.publish_json(
                        PRODUCTION_INSPECTION_CHANGED_CHANNEL,
                        production_inspection_changed_payload(inspection_id=inspection_id),
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not publish committed ProductionInspection change inspection_id=%s: %s",
                        inspection_id, exc,
                    )
        finally:
            if client is not None:
                await client.close()
