from fastapi.testclient import TestClient

from telemetry_gateway.main import app


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "telemetry-gateway"


def test_telemetry_websocket_snapshot_and_ping() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/telemetry") as websocket:
            snapshot = websocket.receive_json()
            websocket.send_text("ping")
            pong = websocket.receive_text()

    assert snapshot["type"] == "TELEMETRY_SNAPSHOT"
    assert snapshot["robots"] == []
    assert pong == "pong"
