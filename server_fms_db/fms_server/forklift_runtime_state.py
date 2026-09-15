"""Transient Redis latest-state support for observed Forklift feedback."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from shared.realtime.transport_events import TRANSPORT_EVENT_CHANNEL
from shared.realtime.redis_client import (
    RedisJsonSerializationError,
    RealtimeRedisClient,
    create_realtime_redis_client,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ForkliftRuntimeState:
    """Context-bound feedback, not a durable execution result or Unity DTO."""

    req_id: str
    job_id: int | None
    delivery_id: int | None
    robot_id: str
    task_type: str
    phase: str
    progress: float
    detail: str

    def to_payload(self) -> dict[str, str | int | float | None]:
        return {
            "req_id": self.req_id,
            "job_id": self.job_id,
            "delivery_id": self.delivery_id,
            "robot_id": self.robot_id,
            "task_type": self.task_type,
            "phase": self.phase,
            "progress": self.progress,
            "detail": self.detail,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ForkliftRuntimeState | None:
        if not isinstance(payload, dict):
            return None
        try:
            req_id = payload["req_id"]
            robot_id = payload["robot_id"]
            task_type = payload["task_type"]
            phase = payload["phase"]
            detail = payload["detail"]
            progress = payload["progress"]
            job_id = payload["job_id"]
            delivery_id = payload["delivery_id"]
        except KeyError:
            return None
        if not all(isinstance(value, str) for value in (req_id, robot_id, task_type, phase, detail)):
            return None
        if isinstance(progress, bool) or not isinstance(progress, (int, float)):
            return None
        if any(value is not None and (isinstance(value, bool) or not isinstance(value, int)) for value in (job_id, delivery_id)):
            return None
        return cls(
            req_id=req_id,
            job_id=job_id,
            delivery_id=delivery_id,
            robot_id=robot_id,
            task_type=task_type,
            phase=phase,
            progress=float(progress),
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class ForkliftRuntimeEvent:
    """Internal realtime event with optional authoritative Action Result."""

    state: ForkliftRuntimeState
    result: str | None
    error_code: str | None
    detail: str

    def to_payload(self) -> dict[str, str | int | float | None]:
        payload = self.state.to_payload()
        payload.update({
            "result": self.result,
            "error_code": self.error_code,
            "detail": self.detail,
        })
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> ForkliftRuntimeEvent | None:
        state = ForkliftRuntimeState.from_payload(payload)
        if state is None or not isinstance(payload, dict):
            return None
        result = payload.get("result")
        error_code = payload.get("error_code")
        detail = payload.get("detail")
        if state.task_type not in {"EXECUTE_TRANSPORT", "RETURN_HOME"}:
            return None
        if result is not None and not isinstance(result, str):
            return None
        if result is not None and result not in {"SUCCEEDED", "FAILED", "CANCELED"}:
            return None
        if error_code is not None and not isinstance(error_code, str):
            return None
        if not isinstance(detail, str):
            return None
        return cls(state=state, result=result, error_code=error_code, detail=detail)


class ForkliftRuntimeStateStore:
    """One transient latest-feedback key per transport robot."""

    def __init__(self, client: RealtimeRedisClient) -> None:
        self._client = client

    @staticmethod
    def key_for(robot_id: str) -> str:
        return f"transport:robot:{robot_id}:latest"

    async def write(self, state: ForkliftRuntimeState) -> None:
        await self._client.set_json(self.key_for(state.robot_id), state.to_payload())

    async def get_latest(self, robot_id: str) -> ForkliftRuntimeState | None:
        try:
            payload = await self._client.get_json(self.key_for(robot_id))
        except RedisJsonSerializationError as exc:
            logger.warning("Ignoring malformed Forklift runtime cache for robot_id=%s: %s", robot_id, exc)
            return None
        state = ForkliftRuntimeState.from_payload(payload)
        if payload is not None and state is None:
            logger.warning("Ignoring invalid Forklift runtime cache shape for robot_id=%s", robot_id)
        return state

    async def clear_if_matches(self, *, robot_id: str, req_id: str) -> bool:
        """Remove only the matching operation's latest state.

        The current shared Redis helper has no compare-and-delete primitive, so
        this performs the safest available read/compare/delete sequence.  A
        newer state with a different request ID is never deliberately deleted.
        """

        state = await self.get_latest(robot_id)
        if state is None or state.req_id != req_id:
            return False
        await self._client.delete(self.key_for(robot_id))
        return True


@dataclass(frozen=True, slots=True)
class _RuntimeCleanup:
    robot_id: str
    req_id: str


class ForkliftRuntimeStatePublisher:
    """FMS-owned bridge from synchronous feedback callbacks to async Redis."""

    def __init__(self, *, client_factory: Callable[[], RealtimeRedisClient] = create_realtime_redis_client, queue_size: int = 128) -> None:
        self._client_factory = client_factory
        self._queue: asyncio.Queue[ForkliftRuntimeState | ForkliftRuntimeEvent | _RuntimeCleanup] = asyncio.Queue(maxsize=queue_size)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fms-forklift-runtime-state-publisher")

    def notify_state(self, state: ForkliftRuntimeState) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, state)

    def notify_event(self, event: ForkliftRuntimeEvent) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, event)

    def notify_terminal(self, *, robot_id: str, req_id: str) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._offer, _RuntimeCleanup(robot_id=robot_id, req_id=req_id))

    def _offer(self, item: ForkliftRuntimeState | ForkliftRuntimeEvent | _RuntimeCleanup) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("Dropping best-effort Forklift runtime Redis update: queue full.")

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
            store = ForkliftRuntimeStateStore(client)
            while True:
                item = await self._queue.get()
                try:
                    if isinstance(item, ForkliftRuntimeState):
                        try:
                            await store.write(item)
                        except Exception as exc:
                            logger.warning("Could not store Forklift runtime Redis state: %s", exc)
                        try:
                            await client.publish_json(
                                TRANSPORT_EVENT_CHANNEL,
                                ForkliftRuntimeEvent(item, None, None, item.detail).to_payload(),
                            )
                        except Exception as exc:
                            logger.warning("Could not publish Forklift runtime feedback event: %s", exc)
                    elif isinstance(item, ForkliftRuntimeEvent):
                        try:
                            await client.publish_json(TRANSPORT_EVENT_CHANNEL, item.to_payload())
                        except Exception as exc:
                            logger.warning("Could not publish Forklift terminal runtime event: %s", exc)
                    else:
                        try:
                            await store.clear_if_matches(robot_id=item.robot_id, req_id=item.req_id)
                        except Exception as exc:
                            logger.warning("Could not clear Forklift runtime Redis state: %s", exc)
                except Exception as exc:
                    logger.warning("Could not update Forklift runtime Redis state: %s", exc)
        finally:
            if client is not None:
                await client.close()
