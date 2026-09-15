"""Process-local, non-authoritative Voice Runtime diagnostics for the monitor."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from shared.schemas.ai import (
    VoiceRuntimeDiagnosticEventRequest,
    VoiceRuntimeMonitorResponse,
    VoiceRuntimeTurnResponse,
)

_TERMINAL_STATES = frozenset({"COMPLETED", "ERROR"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_display(error: str | None) -> str | None:
    """Expose only an operator-safe diagnostic summary to Unity."""
    if not error:
        return None
    if error.startswith(("STT_", "VAD_")):
        return "음성을 처리하지 못했습니다."
    if error.startswith("API_"):
        return "서버 통신 오류가 발생했습니다."
    return "음성 처리 중 오류가 발생했습니다."


@dataclass
class _VoiceTurn:
    turn_id: str
    started_at: datetime
    ended_at: datetime | None = None
    state: str = "LISTENING"
    session_id: str | None = None
    wake_detected: bool = False
    speech_detected: bool = False
    transcript: str | None = None
    intent: str | None = None
    parsed_command_summary: str | None = None
    confirmation_state: str | None = None
    response_text: str | None = None
    error: str | None = None
    latencies_ms: dict[str, float] = field(default_factory=dict)

    def response(self) -> VoiceRuntimeTurnResponse:
        return VoiceRuntimeTurnResponse(
            turn_id=self.turn_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            state=self.state,
            session_id=self.session_id,
            wake_detected=self.wake_detected,
            speech_detected=self.speech_detected,
            transcript=self.transcript,
            intent=self.intent,
            parsed_command_summary=self.parsed_command_summary,
            confirmation_state=self.confirmation_state,
            response_text=self.response_text,
            error=self.error,
            latencies_ms=dict(self.latencies_ms),
        )


class VoiceRuntimeMonitor:
    """Bounded in-memory observer. It owns no audio, DB, or business state."""

    def __init__(self, *, max_recent_turns: int = 50) -> None:
        self._max_recent_turns = max_recent_turns
        self._lock = Lock()
        self._runtime_status = "IDLE"
        self._mic_active = False
        self._current: _VoiceTurn | None = None
        self._recent: deque[_VoiceTurn] = deque(maxlen=max_recent_turns)

    def record(self, event: VoiceRuntimeDiagnosticEventRequest) -> None:
        """Merge one best-effort diagnostic event; never execute voice work."""

        with self._lock:
            if event.runtime_status is not None:
                self._runtime_status = event.runtime_status
            if event.mic_active is not None:
                self._mic_active = event.mic_active
            if event.turn_id is None:
                return
            turn = self._current
            if turn is None or turn.turn_id != event.turn_id:
                turn = _VoiceTurn(turn_id=event.turn_id, started_at=_now(), state=event.state)
                self._current = turn
            self._merge(turn, event)
            if event.state in _TERMINAL_STATES:
                turn.ended_at = _now()
                self._recent.appendleft(turn)
                self._current = None
                self._runtime_status = "READY"

    @staticmethod
    def _merge(turn: _VoiceTurn, event: VoiceRuntimeDiagnosticEventRequest) -> None:
        turn.state = event.state
        for name in (
            "session_id", "transcript", "intent", "parsed_command_summary",
            "confirmation_state", "response_text", "error",
        ):
            value = getattr(event, name)
            if value is not None:
                setattr(turn, name, value)
        if event.wake_detected is not None:
            turn.wake_detected = event.wake_detected
        if event.speech_detected is not None:
            turn.speech_detected = event.speech_detected
        if event.latencies_ms:
            turn.latencies_ms.update(event.latencies_ms)

    def snapshot(self) -> VoiceRuntimeMonitorResponse:
        with self._lock:
            return VoiceRuntimeMonitorResponse(
                runtime_status=self._runtime_status,
                mic_active=self._mic_active,
                current_turn=self._current.response() if self._current is not None else None,
                recent_turns=[turn.response() for turn in self._recent],
            )

    def unity_snapshot(self, *, recent_limit: int = 5) -> dict[str, Any]:
        """Return the small, display-safe Voice projection for Unity reconnects."""
        with self._lock:
            def project(turn: _VoiceTurn) -> dict[str, Any]:
                return {
                    "turn_id": turn.turn_id,
                    "state": turn.state,
                    "timestamp": turn.started_at.isoformat(),
                    "transcript": turn.transcript,
                    "response_text": turn.response_text,
                    "intent": turn.intent,
                    "runtime_status": self._runtime_status,
                    "error_display": _error_display(turn.error),
                }

            return {
                "state": self._current.state if self._current is not None else "IDLE",
                "runtime_status": self._runtime_status,
                "current_turn": project(self._current) if self._current is not None else None,
                "recent_turns": [project(turn) for turn in list(self._recent)[:recent_limit]],
            }

    def reset_for_test(self) -> None:
        with self._lock:
            self._runtime_status = "IDLE"
            self._mic_active = False
            self._current = None
            self._recent.clear()


_voice_runtime_monitor = VoiceRuntimeMonitor()


def get_voice_runtime_monitor() -> VoiceRuntimeMonitor:
    return _voice_runtime_monitor


def voice_runtime_realtime_event(event: VoiceRuntimeDiagnosticEventRequest) -> dict[str, Any]:
    """Normalize one monitor event to the additive Unity websocket payload."""
    return {
        "turn_id": event.turn_id,
        "state": event.state,
        "runtime_status": event.runtime_status,
        "transcript": event.transcript,
        "response_text": event.response_text,
        "intent": event.intent,
        "error_display": _error_display(event.error),
    }
