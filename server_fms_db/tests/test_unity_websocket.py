from __future__ import annotations

from fastapi.testclient import TestClient

from api_server.main import app


def test_unity_websocket_sends_postgres_snapshot_shape_before_any_redis_telemetry() -> None:
    app.state.unity_snapshot_provider = lambda: {
        "jobs": [], "robots": [], "transports": [], "active_errors": []
    }
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/unity") as websocket:
                first = websocket.receive_json()
        assert first["schema_version"] == "1.0"
        assert first["type"] == "production_snapshot"
        assert first["sequence"] == 1
        assert set(first) == {"schema_version", "type", "timestamp", "sequence", "data"}
        assert first["data"] == {
            "jobs": [], "robots": [], "transports": [], "incoming_qa": [],
            "production_inspections": [], "active_errors": [],
            "voice_runtime": {"state": "IDLE", "runtime_status": "IDLE", "current_turn": None, "recent_turns": []},
        }
    finally:
        app.state.__dict__.pop("unity_snapshot_provider", None)
