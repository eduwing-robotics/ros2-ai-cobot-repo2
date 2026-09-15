from __future__ import annotations

import asyncio

from fastapi import FastAPI

import api_server.main as api_main
from shared.realtime.production_events import get_production_change_callback


def test_api_lifespan_owns_process_local_production_publisher(monkeypatch) -> None:
    created = []

    class FakeProductionPublisher:
        def __init__(self) -> None:
            self.notifications: list[tuple[int, str | None]] = []
            self.started = False
            self.stopped = False
            created.append(self)

        async def start(self) -> None:
            self.started = True

        def notify_after_commit(self, job_id: int, reason: str | None = None) -> None:
            self.notifications.append((job_id, reason))

        async def stop(self) -> None:
            self.stopped = True

    class FakeSubscriber:
        def __init__(self, *_args, **_kwargs) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    class FakeLlm:
        async def close(self) -> None:
            return None

    monkeypatch.setattr(api_main, "ProductionChangedPublisher", FakeProductionPublisher)
    monkeypatch.setattr(api_main, "RedisTelemetrySubscriber", FakeSubscriber)
    monkeypatch.setattr(api_main, "get_llm_service", lambda: FakeLlm())

    application = FastAPI()
    application.dependency_overrides[api_main.get_db] = lambda: None
    application.state.unity_snapshot_provider = lambda: {
        "jobs": [], "robots": [], "transports": [], "incoming_qa": [],
        "production_inspections": [], "active_errors": [],
    }
    application.state.unity_production_status_reader = lambda _job_id: None
    application.state.unity_error_event_reader = lambda _attempt_id: None

    async def run() -> None:
        async with api_main.lifespan(application):
            callback = get_production_change_callback()
            assert callback is not None
            callback(42, "job_created")
            assert created[0].started is True
            assert created[0].notifications == [(42, "job_created")]
        assert created[0].stopped is True
        assert get_production_change_callback() is None

    asyncio.run(run())
