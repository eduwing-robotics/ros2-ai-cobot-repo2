from __future__ import annotations

from fastapi.testclient import TestClient

from api_server.main import app
from api_server.services.voice_runtime_monitor import (
    VoiceRuntimeMonitor, get_voice_runtime_monitor, voice_runtime_realtime_event,
)
from shared.schemas.ai import VoiceRuntimeDiagnosticEventRequest


def _event(turn_id: str | None, state: str, **values: object) -> VoiceRuntimeDiagnosticEventRequest:
    return VoiceRuntimeDiagnosticEventRequest(turn_id=turn_id, state=state, **values)


def test_voice_monitor_empty_projection_has_no_active_turn() -> None:
    monitor = VoiceRuntimeMonitor()

    snapshot = monitor.snapshot()

    assert snapshot.runtime_status == "IDLE"
    assert snapshot.mic_active is False
    assert snapshot.current_turn is None
    assert snapshot.recent_turns == []


def test_voice_monitor_records_turn_lifecycle_and_safe_fields() -> None:
    monitor = VoiceRuntimeMonitor()
    monitor.record(_event("voice-101", "LISTENING", runtime_status="READY", mic_active=True))
    monitor.record(_event("voice-101", "WAKE_DETECTED", wake_detected=True))
    monitor.record(_event("voice-101", "SPEECH_DETECTED", speech_detected=True))
    monitor.record(_event("voice-101", "INTERPRETING", transcript="B형 주택 한 채 만들어줘", latencies_ms={"stt": 420.0}))
    monitor.record(_event(
        "voice-101", "WAITING_CONFIRMATION", intent="CREATE_PRODUCTION_REQUEST",
        parsed_command_summary="intent=CREATE_PRODUCTION_REQUEST, product_code=HOUSE_B, quantity=1",
        confirmation_state="AWAITING_CONFIRMATION", response_text="생산을 시작할까요?",
    ))

    current = monitor.snapshot().current_turn

    assert current is not None
    assert current.state == "WAITING_CONFIRMATION"
    assert current.wake_detected is True and current.speech_detected is True
    assert current.transcript == "B형 주택 한 채 만들어줘"
    assert current.intent == "CREATE_PRODUCTION_REQUEST"
    assert current.confirmation_state == "AWAITING_CONFIRMATION"
    assert current.latencies_ms == {"stt": 420.0}

    monitor.record(_event("voice-101", "COMPLETED", runtime_status="READY", mic_active=True))
    snapshot = monitor.snapshot()
    assert snapshot.current_turn is None
    assert snapshot.recent_turns[0].state == "COMPLETED"


def test_voice_monitor_records_error_and_enforces_ring_buffer_limit() -> None:
    monitor = VoiceRuntimeMonitor(max_recent_turns=2)
    for number in range(3):
        monitor.record(_event(f"voice-{number}", "LISTENING"))
        monitor.record(_event(f"voice-{number}", "ERROR", error="STT_EMPTY"))

    snapshot = monitor.snapshot()

    assert [turn.turn_id for turn in snapshot.recent_turns] == ["voice-2", "voice-1"]
    assert all(turn.error == "STT_EMPTY" for turn in snapshot.recent_turns)


def test_voice_monitor_unity_projection_limits_history_and_hides_raw_error():
    monitor = VoiceRuntimeMonitor(max_recent_turns=10)
    monitor.record(_event("voice-unity", "INTERPRETING", runtime_status="PROCESSING", transcript="현재 무슨 작업 중이야?"))
    monitor.record(_event("voice-unity", "ERROR", error="API_INTERNAL: traceback must not escape"))
    projection = monitor.unity_snapshot()
    assert projection["state"] == "IDLE"
    assert projection["recent_turns"][0]["transcript"] == "현재 무슨 작업 중이야?"
    assert projection["recent_turns"][0]["error_display"] == "서버 통신 오류가 발생했습니다."
    assert "error" not in projection["recent_turns"][0]


def test_voice_realtime_event_is_display_safe_and_preserves_transcript_response():
    event = _event(
        "voice-event", "RESPONDING", runtime_status="RESPONDING",
        transcript="현재 무슨 작업 중이야?", intent="QUERY_JOB_STATUS",
        response_text="현재 A형 초소형 하우스를 생산 중입니다.",
    )
    assert voice_runtime_realtime_event(event) == {
        "turn_id": "voice-event", "state": "RESPONDING",
        "runtime_status": "RESPONDING", "transcript": "현재 무슨 작업 중이야?",
        "response_text": "현재 A형 초소형 하우스를 생산 중입니다.",
        "intent": "QUERY_JOB_STATUS", "error_display": None,
    }


def test_voice_runtime_debug_endpoint_is_read_only() -> None:
    monitor = get_voice_runtime_monitor()
    monitor.reset_for_test()
    monitor.record(_event("voice-api", "LISTENING", runtime_status="READY", mic_active=True))
    before = monitor.snapshot().model_dump(mode="json")
    with TestClient(app) as client:
        response = client.get("/ai/debug/voice-runtime")
    after = monitor.snapshot().model_dump(mode="json")
    monitor.reset_for_test()

    assert response.status_code == 200
    assert response.json() == before
    assert after == before


def test_voice_runtime_event_ingress_fans_out_to_unity_without_changing_response():
    class RecordingHub:
        def __init__(self):
            self.events = []

        async def publish_voice_event(self, event):
            self.events.append(event)

    monitor = get_voice_runtime_monitor()
    monitor.reset_for_test()
    try:
        with TestClient(app) as client:
            hub = RecordingHub()
            client.app.state.unity_hub = hub
            response = client.post("/ai/debug/voice-runtime/events", json={
                "turn_id": "voice-ws", "state": "LISTENING", "runtime_status": "READY",
            })
            assert response.status_code == 202
            assert hub.events == [{
                "turn_id": "voice-ws", "state": "LISTENING", "runtime_status": "READY",
                "transcript": None, "response_text": None, "intent": None, "error_display": None,
            }]
    finally:
        monitor.reset_for_test()


def test_voice_runtime_event_publish_failure_is_isolated():
    class FailingHub:
        async def publish_voice_event(self, _event):
            raise RuntimeError("slow Unity is unavailable")

    monitor = get_voice_runtime_monitor()
    monitor.reset_for_test()
    try:
        with TestClient(app) as client:
            client.app.state.unity_hub = FailingHub()
            response = client.post("/ai/debug/voice-runtime/events", json={
                "turn_id": "voice-isolated", "state": "LISTENING", "runtime_status": "READY",
            })
            assert response.status_code == 202
            assert monitor.snapshot().current_turn is not None
    finally:
        monitor.reset_for_test()


def test_voice_runtime_event_ingress_updates_only_diagnostic_memory() -> None:
    monitor = get_voice_runtime_monitor()
    monitor.reset_for_test()
    with TestClient(app) as client:
        response = client.post("/ai/debug/voice-runtime/events", json={
            "turn_id": "voice-ingress", "state": "WAKE_DETECTED", "runtime_status": "READY",
            "mic_active": True, "wake_detected": True,
        })
        snapshot = client.get("/ai/debug/voice-runtime")
    monitor.reset_for_test()

    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert snapshot.status_code == 200
    assert snapshot.json()["current_turn"]["turn_id"] == "voice-ingress"
    assert snapshot.json()["current_turn"]["wake_detected"] is True
