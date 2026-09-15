"""Voice-runtime Redis observer for durable production announcement readbacks."""
from __future__ import annotations
import asyncio, json, logging
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from typing import Any
from shared.realtime.production_events import PRODUCTION_CHANGED_CHANNEL
from shared.realtime.transport_events import TRANSPORT_EVENT_CHANNEL
from shared.realtime.incoming_qa_events import INCOMING_QA_CHANGED_CHANNEL
from shared.realtime.production_inspection_events import PRODUCTION_INSPECTION_CHANGED_CHANNEL
from shared.realtime.error_events import ERROR_EVENT_CHANNEL
from shared.realtime.redis_client import create_realtime_redis_client, RedisUnavailableError
from .production_announcements import ProductionAnnouncementDetector
logger=logging.getLogger(__name__)
class ProductionEventAnnouncementSubscriber:
    def __init__(self, *, snapshot_reader: Callable[[int], Awaitable[dict[str,Any]|None]], queue, job_ids_reader: Callable[[], Awaitable[list[dict[str,Any]]]] | None = None, fault_reader: Callable[[int], Awaitable[dict[str,Any]|None]] | None = None, client_factory=create_realtime_redis_client, channel_prefix: str=""):
        self._read=snapshot_reader; self._queue=queue; self._job_ids_reader=job_ids_reader; self._fault_reader=fault_reader; self._client_factory=client_factory; self._prefix=channel_prefix; self._detector=ProductionAnnouncementDetector(); self._stop=asyncio.Event(); self._task=None
        # Captured before the subscriber starts. Jobs requested after this
        # point must remain eligible for their first genuine transition.
        self._started_at = datetime.now(UTC)
        self._bootstrapped = False
    def start(self):
        if self._task is None: self._task=asyncio.create_task(self._run(),name="voice-production-announcement-subscriber")
    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task=None
    async def _bootstrap_existing_jobs(self) -> None:
        """Baseline only jobs that existed when this runtime started.

        Subscription is already active while this runs, so a new Job's event is
        queued and later observed normally. A Job created after ``_started_at``
        is deliberately not baselined even if it appears in the list during the
        small startup race.
        """
        if self._job_ids_reader is None:
            self._bootstrapped = True
            return
        try:
            jobs = await self._job_ids_reader()
        except Exception:
            logger.exception("Production announcement bootstrap list failed")
            self._bootstrapped = True
            return
        count = 0
        for job in jobs:
            if not isinstance(job, dict):
                continue
            job_id = job.get("job_id")
            if not isinstance(job_id, int) or job_id < 1:
                continue
            if not self._existed_at_runtime_start(job):
                continue
            try:
                snapshot = await self._read(job_id)
            except Exception:
                logger.exception("Production announcement bootstrap read failed job_id=%s", job_id)
                continue
            if snapshot:
                self._detector.baseline(snapshot)
                count += 1
        self._bootstrapped = True
        logger.info("Production announcement baseline established jobs=%s", count)

    def _existed_at_runtime_start(self, job: dict[str, Any]) -> bool:
        raw = job.get("requested_at")
        if not isinstance(raw, str):
            # A listed Job with no usable timestamp is safest treated as
            # pre-existing; this avoids replaying historical milestones.
            return True
        try:
            requested_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if requested_at.tzinfo is None:
                requested_at = requested_at.replace(tzinfo=UTC)
            return requested_at <= self._started_at
        except ValueError:
            return True

    async def observe_job(self, job_id:int) -> None:
        try: snapshot=await self._read(job_id)
        except Exception: logger.exception("Production announcement snapshot read failed job_id=%s",job_id); return
        if snapshot:
            for announcement in self._detector.observe(snapshot): self._queue.enqueue(announcement.text,announcement.priority)
    async def observe_identity(self, *, entity: str, entity_id: int) -> None:
        """Resolve QA/inspection identity triggers without inventing an API event."""
        if self._job_ids_reader is None or entity_id < 1:
            return
        for job in await self._job_ids_reader():
            job_id = job.get("job_id") if isinstance(job, dict) else None
            if not isinstance(job_id, int):
                continue
            snapshot = await self._read(job_id)
            if not snapshot:
                continue
            if entity == "qa" and any(tx.get("transaction_id") == entity_id for tx in (snapshot.get("incoming_qa") or {}).get("transactions", []) if isinstance(tx, dict)):
                for announcement in self._detector.observe(snapshot): self._queue.enqueue(announcement.text, announcement.priority)
                return
            inspection = snapshot.get("pre_roof_inspection") or {}
            if entity == "inspection" and inspection.get("inspection_id") == entity_id:
                for announcement in self._detector.observe(snapshot): self._queue.enqueue(announcement.text, announcement.priority)
                return
    async def observe_fault(self, job_id:int, fault_key:object) -> None:
        for announcement in self._detector.observe_cell_fault(job_id=job_id,fault_key=fault_key): self._queue.enqueue(announcement.text,announcement.priority)
    async def _run(self):
        client=None; pubsub=None
        try:
            client=self._client_factory()
            if not await client.ping(): raise RedisUnavailableError("Redis PING failed")
            pubsub=client.raw_client.pubsub()
            await pubsub.subscribe(*[self._prefix+name for name in (PRODUCTION_CHANGED_CHANNEL, TRANSPORT_EVENT_CHANNEL, ERROR_EVENT_CHANNEL, INCOMING_QA_CHANGED_CHANNEL, PRODUCTION_INSPECTION_CHANGED_CHANNEL)])
            # Subscribe before baseline so a Job created during bootstrap is
            # queued and processed as a genuinely new Job afterwards.
            await self._bootstrap_existing_jobs()
            logger.info("Production announcement Redis subscriber ready")
            while not self._stop.is_set():
                message=await pubsub.get_message(ignore_subscribe_messages=True,timeout=1.0)
                if not message: continue
                try:
                    raw_data = message.get("data")
                    if isinstance(raw_data, bytes):
                        raw_data = raw_data.decode("utf-8")
                    payload=json.loads(raw_data)
                    raw_channel = message.get("channel", "")
                    if isinstance(raw_channel, bytes):
                        raw_channel = raw_channel.decode("utf-8")
                    if not isinstance(raw_channel, str):
                        continue
                    channel=raw_channel.removeprefix(self._prefix)
                except Exception:
                    logger.debug("Ignoring malformed production announcement Pub/Sub message", exc_info=True)
                    continue
                if not isinstance(payload,dict): continue
                raw_job_id=payload.get("job_id")
                if isinstance(raw_job_id,int) and raw_job_id>0:
                    await self.observe_job(raw_job_id)
                elif channel == INCOMING_QA_CHANGED_CHANNEL:
                    raw_id = payload.get("transaction_id")
                    if isinstance(raw_id, int): await self.observe_identity(entity="qa", entity_id=raw_id)
                elif channel == PRODUCTION_INSPECTION_CHANGED_CHANNEL:
                    raw_id = payload.get("inspection_id")
                    if isinstance(raw_id, int): await self.observe_identity(entity="inspection", entity_id=raw_id)
                elif channel == ERROR_EVENT_CHANNEL and self._fault_reader is not None:
                    raw_id = payload.get("attempt_id")
                    if isinstance(raw_id, int):
                        fault = await self._fault_reader(raw_id)
                        if isinstance(fault, dict) and fault.get("source") == "ROBOT_CELL" and isinstance(fault.get("job_id"), int):
                            await self.observe_fault(fault["job_id"], raw_id)
        except asyncio.CancelledError: raise
        except Exception as exc: logger.warning("Production announcement Redis observer stopped: %s",exc)
        finally:
            if pubsub is not None:
                try: await pubsub.aclose()
                except Exception: pass
            if client is not None: await client.close()
