"""Minimal FastAPI entrypoint for user and GUI requests."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api_server.routers.ai import router as ai_router
from api_server.routers.inventory import get_db, router as inventory_router
from api_server.routers.production import router as production_router
from api_server.routers.ui import router as ui_router
from api_server.routers.test_override import router as test_override_router
from api_server.services.llm_service import get_llm_service
from api_server.services.production_snapshot_service import ProductionSnapshotService
from api_server.services.unity_realtime import RedisTelemetrySubscriber, UnityRealtimeHub
from api_server.services.voice_runtime_monitor import get_voice_runtime_monitor
from fms_server.incoming_qa_realtime_publisher import IncomingQAChangedPublisher
from fms_server.production_realtime_publisher import ProductionChangedPublisher
from fms_server.production_inspection_realtime_publisher import ProductionInspectionChangedPublisher
from shared.config import get_settings
from shared.database import get_session_factory
from shared.realtime.incoming_qa_events import set_incoming_qa_change_callback
from shared.realtime.production_events import set_production_change_callback
from shared.realtime.production_inspection_events import set_production_inspection_change_callback

VERSION = "0.1.0"
logger = logging.getLogger(__name__)


def _empty_production_snapshot() -> dict[str, list[object]]:
    return {"jobs": [], "robots": [], "transports": [], "incoming_qa": [], "production_inspections": [], "active_errors": []}


def _default_production_snapshot() -> dict[str, Any]:
    """Read authoritative Job data lazily; startup does not require PostgreSQL."""

    if not get_settings().database_url.strip():
        return _empty_production_snapshot()
    return ProductionSnapshotService(get_session_factory()).get_snapshot()



def _default_production_status(job_id: int) -> dict[str, Any] | None:
    """Read one durable Job for a production.changed notification."""

    if not get_settings().database_url.strip():
        return None
    return ProductionSnapshotService(get_session_factory()).get_job_status(job_id)


def _default_incoming_qa_status(transaction_id: int) -> dict[str, Any] | None:
    """Read one durable Incoming QA transaction for a change notification."""

    if not get_settings().database_url.strip():
        return None
    return ProductionSnapshotService(get_session_factory()).get_incoming_qa_status(transaction_id)


def _default_production_inspection_status(inspection_id: int) -> dict[str, Any] | None:
    if not get_settings().database_url.strip():
        return None
    return ProductionSnapshotService(get_session_factory()).get_production_inspection_status(inspection_id)


def _default_error_event(attempt_id: int) -> dict[str, Any] | None:
    if not get_settings().database_url.strip():
        return None
    return ProductionSnapshotService(get_session_factory()).get_error_event(attempt_id)

@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    logger.info("API Server starting (env=%s, port=%s)", settings.app_env, settings.api_port)
    snapshot_provider = getattr(application.state, "unity_snapshot_provider", _default_production_snapshot)
    status_reader = getattr(application.state, "unity_production_status_reader", _default_production_status)
    error_reader = getattr(application.state, "unity_error_event_reader", _default_error_event)
    if hasattr(application.state, "unity_incoming_qa_status_reader"):
        incoming_qa_reader = application.state.unity_incoming_qa_status_reader
    elif get_db in application.dependency_overrides:
        # Isolated API tests supply request sessions through this override. Do
        # not let an optional diagnostic subscriber open a separate configured
        # PostgreSQL connection; tests that exercise this stream inject a reader.
        incoming_qa_reader = None
    else:
        incoming_qa_reader = _default_incoming_qa_status
    if hasattr(application.state, "unity_production_inspection_status_reader"):
        production_inspection_reader = application.state.unity_production_inspection_status_reader
    elif get_db in application.dependency_overrides:
        production_inspection_reader = None
    else:
        production_inspection_reader = _default_production_inspection_status
    unity_hub = UnityRealtimeHub(
        snapshot_provider,
        voice_snapshot=lambda: get_voice_runtime_monitor().unity_snapshot(),
    )
    production_publisher = ProductionChangedPublisher()
    incoming_qa_publisher = IncomingQAChangedPublisher() if incoming_qa_reader is not None else None
    production_inspection_publisher = (
        ProductionInspectionChangedPublisher() if production_inspection_reader is not None else None
    )
    unity_subscriber = RedisTelemetrySubscriber(
        unity_hub,
        channel_prefix=settings.unity_redis_channel_prefix,
        production_status_reader=status_reader,
        error_event_reader=error_reader,
        incoming_qa_status_reader=incoming_qa_reader,
        production_inspection_status_reader=production_inspection_reader,
    )
    application.state.unity_hub = unity_hub
    application.state.unity_subscriber = unity_subscriber
    application.state.production_changed_publisher = production_publisher
    await production_publisher.start()
    set_production_change_callback(production_publisher.notify_after_commit)
    if incoming_qa_publisher is not None:
        await incoming_qa_publisher.start()
        set_incoming_qa_change_callback(incoming_qa_publisher.notify_after_commit)
    if production_inspection_publisher is not None:
        await production_inspection_publisher.start()
        set_production_inspection_change_callback(production_inspection_publisher.notify_after_commit)
    unity_subscriber.start()
    try:
        yield
    finally:
        set_production_change_callback(None)
        if incoming_qa_publisher is not None:
            set_incoming_qa_change_callback(None)
        if production_inspection_publisher is not None:
            set_production_inspection_change_callback(None)
        await unity_subscriber.stop()
        await production_publisher.stop()
        if incoming_qa_publisher is not None:
            await incoming_qa_publisher.stop()
        if production_inspection_publisher is not None:
            await production_inspection_publisher.stop()
        await get_llm_service().close()
        logger.info("API Server stopped")


app = FastAPI(title="Tiny House Production API", version=VERSION, lifespan=lifespan)

# Development-only permissive CORS. Restrict allowed origins before deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="api_server/static"), name="static")
app.include_router(ai_router)
app.include_router(inventory_router)
app.include_router(production_router)
app.include_router(ui_router)
app.include_router(test_override_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-server", "version": VERSION}


@app.websocket("/ws/unity")
async def unity_socket(websocket: WebSocket) -> None:
    """Unity's sole realtime endpoint; Redis and ROS remain server-internal."""

    hub: UnityRealtimeHub = app.state.unity_hub
    connection = await hub.connect(websocket)
    try:
        while True:
            # Unity messages are reserved for a future explicit client protocol.
            # Keeping the receive loop lets disconnects be detected promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.disconnect(connection)
