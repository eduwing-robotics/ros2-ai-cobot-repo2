"""FMS-owned best-effort publisher for committed execution-attempt errors."""

from __future__ import annotations

import asyncio
import logging

from shared.realtime.error_events import ERROR_EVENT_CHANNEL, error_event_payload
from shared.realtime.redis_client import RealtimeRedisClient, create_realtime_redis_client

logger = logging.getLogger(__name__)


class ExecutionAttemptErrorPublisher:
    def __init__(self, *, client_factory=create_realtime_redis_client, queue_size: int = 128) -> None:
        self._client_factory = client_factory
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fms-execution-attempt-error-publisher")

    def notify_after_commit(self, attempt_id: int) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, attempt_id)

    def _offer(self, attempt_id: int) -> None:
        try:
            self._queue.put_nowait(attempt_id)
        except asyncio.QueueFull:
            logger.warning("Dropping best-effort error.event notification: queue full.")

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
                attempt_id = await self._queue.get()
                try:
                    await client.publish_json(ERROR_EVENT_CHANNEL, error_event_payload(attempt_id=attempt_id))
                except Exception as exc:
                    logger.warning("Could not publish committed error.event attempt_id=%s: %s", attempt_id, exc)
        finally:
            if client is not None:
                await client.close()
