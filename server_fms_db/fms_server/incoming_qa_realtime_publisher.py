"""FMS/API-owned best-effort publisher for committed Incoming QA changes."""

from __future__ import annotations

import asyncio
import logging

from shared.realtime.incoming_qa_events import (
    INCOMING_QA_CHANGED_CHANNEL,
    incoming_qa_changed_payload,
)
from shared.realtime.redis_client import RealtimeRedisClient, create_realtime_redis_client

logger = logging.getLogger(__name__)


class IncomingQAChangedPublisher:
    """Queue post-commit identity triggers without coupling DB state to Redis."""

    def __init__(self, *, client_factory=create_realtime_redis_client, queue_size: int = 128) -> None:
        self._client_factory = client_factory
        self._queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="incoming-qa-changed-publisher")

    def notify_after_commit(self, transaction_id: int) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, transaction_id)

    def _offer(self, transaction_id: int) -> None:
        if not isinstance(transaction_id, int) or transaction_id < 1:
            logger.warning("Ignoring Incoming QA change without a valid transaction_id.")
            return
        try:
            self._queue.put_nowait(transaction_id)
        except asyncio.QueueFull:
            logger.warning("Dropping best-effort Incoming QA change notification: queue full.")

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
                transaction_id = await self._queue.get()
                try:
                    await client.publish_json(
                        INCOMING_QA_CHANGED_CHANNEL,
                        incoming_qa_changed_payload(transaction_id=transaction_id),
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not publish committed Incoming QA change transaction_id=%s: %s",
                        transaction_id,
                        exc,
                    )
        finally:
            if client is not None:
                await client.close()
