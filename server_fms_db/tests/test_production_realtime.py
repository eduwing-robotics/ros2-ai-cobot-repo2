from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

from api_server.services.unity_realtime import RedisTelemetrySubscriber, UnityRealtimeHub
from fms_server.production_realtime_publisher import ProductionChangedPublisher
from shared.realtime.production_events import PRODUCTION_CHANGED_CHANNEL
from shared.services.production_orchestration_service import ProductionOrchestrationService


@dataclass(eq=False)
class FakeWebSocket:
    messages: list[dict] = field(default_factory=list)

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def _snapshot() -> dict:
    return {"jobs": [], "robots": [], "transports": [], "active_errors": []}


def test_production_status_is_after_snapshot_and_uses_connection_sequence() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(_snapshot)
        first, second = FakeWebSocket(), FakeWebSocket()
        await hub.connect(first)
        await hub.connect(second)
        await hub.publish_production_status({"job_id": 19, "status": "RUNNING"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert [item["type"] for item in first.messages] == ["production_snapshot", "production_status"]
        assert [item["sequence"] for item in first.messages] == [1, 2]
        assert [item["sequence"] for item in second.messages] == [1, 2]

    asyncio.run(run())


def test_subscriber_rereads_authoritative_status_instead_of_redis_payload() -> None:
    async def run() -> None:
        hub = UnityRealtimeHub(_snapshot)
        socket = FakeWebSocket()
        await hub.connect(socket)
        seen: list[int] = []

        def read_job(job_id: int) -> dict:
            seen.append(job_id)
            return {"job_id": job_id, "status": "COMPLETED", "job_code": "DB-19"}

        subscriber = RedisTelemetrySubscriber(hub, production_status_reader=read_job)
        await subscriber._handle_production_changed({"job_id": "19", "status": "FAKE"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == [19]
        assert socket.messages[-1]["type"] == "production_status"
        assert socket.messages[-1]["data"] == {"job_id": 19, "status": "COMPLETED", "job_code": "DB-19"}

    asyncio.run(run())


def test_orchestration_callback_runs_only_after_commit() -> None:
    class Session:
        committed = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            return None

    session = Session()
    observed: list[tuple[int, str, bool]] = []
    service = ProductionOrchestrationService(
        session, post_commit_callback=lambda job_id, reason: observed.append((job_id, reason, session.committed))
    )
    service._run_write_transaction(lambda: SimpleNamespace(job_id=7), reason="step_completed")
    assert observed == [(7, "step_completed", True)]


def test_orchestration_does_not_publish_when_commit_fails() -> None:
    class Session:
        def commit(self) -> None:
            raise RuntimeError("db unavailable")

        def rollback(self) -> None:
            return None

    observed: list[int] = []
    service = ProductionOrchestrationService(Session(), post_commit_callback=lambda job_id, reason: observed.append(job_id))
    try:
        service._run_write_transaction(lambda: SimpleNamespace(job_id=7), reason="step_completed")
    except RuntimeError:
        pass
    else:  # pragma: no cover - assertion guard
        raise AssertionError("commit failure must be visible")
    assert observed == []


def test_fms_publisher_keeps_running_when_redis_publish_fails() -> None:
    class BrokenClient:
        async def publish_json(self, channel: str, payload: dict) -> int:
            assert channel == PRODUCTION_CHANGED_CHANNEL
            raise RuntimeError("redis unavailable")

        async def close(self) -> None:
            return None

    async def run() -> None:
        publisher = ProductionChangedPublisher(client_factory=BrokenClient)
        await publisher.start()
        publisher.notify_after_commit(5, "step_completed")
        await asyncio.sleep(0)
        await publisher.stop()

    asyncio.run(run())
