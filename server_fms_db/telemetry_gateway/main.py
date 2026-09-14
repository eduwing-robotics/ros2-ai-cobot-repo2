"""Internal-debug telemetry gateway: ROS normalization to Redis realtime state."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from shared.config import Settings, get_settings
from shared.realtime.redis_client import create_realtime_redis_client
from telemetry_gateway.ros_subscriber import RosTelemetrySubscriber, RosTelemetryUnavailableError
from telemetry_gateway.telemetry import LatestValueTelemetryHandoff, RedisTelemetrySink

VERSION = "0.1.0"
logger = logging.getLogger(__name__)


class ConnectionManager:
    """Keep track of internal-debug telemetry clients."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)


@dataclass
class TelemetryGatewayRuntime:
    """Own Redis handoff and optional ROS executor for one Gateway process."""

    settings: Settings
    handoff: LatestValueTelemetryHandoff | None = None
    redis_sink: RedisTelemetrySink | None = None
    redis_client: object | None = None
    ros_subscriber: RosTelemetrySubscriber | None = None
    ros_start_error: str | None = None

    async def start(self) -> None:
        redis_client = create_realtime_redis_client(redis_url=self.settings.redis_url)
        self.redis_client = redis_client
        self.redis_sink = RedisTelemetrySink(redis_client)
        self.handoff = LatestValueTelemetryHandoff(self.redis_sink)
        self.handoff.start()

        # Normal test/API imports do not attach a ROS participant to Domain 73.
        # Deployments opt in only after sourcing the intended ROS environment.
        if not self.settings.telemetry_ros_enabled:
            logger.info("Telemetry ROS subscriptions disabled (TELEMETRY_ROS_ENABLED=false).")
            return
        subscriber = RosTelemetrySubscriber(
            loop=asyncio.get_running_loop(),
            offer_update=self.handoff.offer,
        )
        try:
            subscriber.start()
        except RosTelemetryUnavailableError as exc:
            self.ros_start_error = str(exc)
            logger.warning("Telemetry ROS subscriptions unavailable: %s", exc)
            return
        except Exception as exc:  # keep gateway/Redis diagnostics alive if ROS startup fails
            self.ros_start_error = str(exc)
            logger.warning("Telemetry ROS subscriptions failed to start: %s", exc)
            subscriber.stop()
            return
        self.ros_subscriber = subscriber

    async def stop(self) -> None:
        if self.ros_subscriber is not None:
            self.ros_subscriber.stop()
            self.ros_subscriber = None
        if self.handoff is not None:
            await self.handoff.stop()
            self.handoff = None
        if self.redis_client is not None:
            await self.redis_client.close()  # type: ignore[union-attr]
            self.redis_client = None

    @property
    def ros_running(self) -> bool:
        return self.ros_subscriber is not None and self.ros_subscriber.running

    @property
    def redis_state(self) -> str:
        if self.redis_sink is None or self.redis_sink.redis_available is None:
            return "unknown"
        return "connected" if self.redis_sink.redis_available else "unavailable"


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = get_settings()
    runtime = TelemetryGatewayRuntime(settings)
    application.state.telemetry_runtime = runtime
    logger.info(
        "Telemetry Gateway starting (env=%s, port=%s)",
        settings.app_env,
        settings.telemetry_port,
    )
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()
        logger.info("Telemetry Gateway stopped")


app = FastAPI(title="Tiny House Telemetry Gateway", version=VERSION, lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    runtime: TelemetryGatewayRuntime | None = getattr(app.state, "telemetry_runtime", None)
    return {
        "status": "ok",
        "service": "telemetry-gateway",
        "version": VERSION,
        "redis": runtime.redis_state if runtime is not None else "unknown",
        "ros": "running" if runtime is not None and runtime.ros_running else "disabled",
    }


@app.websocket("/ws/telemetry")
async def telemetry_socket(websocket: WebSocket) -> None:
    """Internal debug route only; Unity's final route is API Server ``/ws/unity``."""

    await manager.connect(websocket)
    logger.info("Telemetry client connected (%s active)", len(manager._connections))
    runtime: TelemetryGatewayRuntime | None = getattr(app.state, "telemetry_runtime", None)
    try:
        robots = runtime.handoff.latest_payloads() if runtime is not None and runtime.handoff is not None else []
        await websocket.send_json(
            {
                "type": "TELEMETRY_SNAPSHOT",
                "robots": robots,
                "message": "INTERNAL DEBUG ONLY; Redis-to-API/Unity streaming is not enabled.",
            }
        )
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.info("Telemetry client disconnected")
    finally:
        manager.disconnect(websocket)
