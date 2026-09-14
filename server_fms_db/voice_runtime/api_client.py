import logging
from dataclasses import dataclass
from typing import Optional

import httpx


logger = logging.getLogger(__name__)


class VoiceConversationValidationError(RuntimeError):
    """Recoverable server-side audio validation (HTTP 422)."""


class VoiceConversationServerError(RuntimeError):
    """Server processing failure distinct from transport connectivity."""


class VoiceConversationConnectionError(RuntimeError):
    """Unable to connect to the authoritative Voice API."""


@dataclass
class ConversationResponse:
    session_id: str
    conversation_state: Optional[str]
    message: str
    clarification_needed: bool
    intent: Optional[str] = None
    production_job_ids: tuple[int, ...] = ()


class VoiceAPIClient:
    """HTTP client for the authoritative Voice and TTS API routes.

    The microphone runtime deliberately sends the original WAV only once to
    ``/ai/voice-conversation``. That route owns STT and every business
    decision; the client merely turns its returned ``message`` into speech.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(base_url=base_url)
        self._runtime_turn_id: str | None = None

    def set_runtime_turn_id(self, turn_id: str | None) -> None:
        """Attach best-effort diagnostics to existing API calls without changing their body."""
        self._runtime_turn_id = turn_id

    async def record_runtime_event(self, event: dict[str, object]) -> None:
        """Diagnostic-only ingress. Failures intentionally remain caller-contained."""
        response = await self.client.post("/ai/debug/voice-runtime/events", json=event, timeout=1.0)
        response.raise_for_status()

    def _runtime_headers(self) -> dict[str, str] | None:
        if self._runtime_turn_id is None:
            return None
        return {"X-Voice-Runtime-Turn-ID": self._runtime_turn_id}

    async def send_voice_conversation(self, session_id: str, audio_bytes: bytes) -> ConversationResponse:
        files = {"audio": ("speech.wav", audio_bytes, "audio/wav")}
        # Do not reconstruct the conversation from a separate STT request. The
        # server's multipart route performs real STT and returns the sole
        # authoritative business message for this turn.
        try:
            response = await self.client.post(
                "/ai/voice-conversation",
                data={"session_id": session_id}, files=files,
                headers=self._runtime_headers(), timeout=30.0,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise VoiceConversationConnectionError("Voice API connection failed.") from exc
        if response.status_code == 422:
            raise VoiceConversationValidationError(response.text)
        if response.status_code >= 500:
            raise VoiceConversationServerError(f"Voice API HTTP {response.status_code}.")
        response.raise_for_status()
        payload = response.json()
        command = payload.get("command") or {}
        message = str(payload.get("message") or "")
        if not message:
            raise ValueError("Voice API returned an empty authoritative response message.")

        return ConversationResponse(
            session_id=payload.get("session_id"),
            conversation_state=payload.get("conversation_state"),
            message=message,
            clarification_needed=command.get("clarification_needed", False)
            if isinstance(command, dict)
            else False,
            intent=command.get("intent") if isinstance(command, dict) else None,
            production_job_ids=tuple(payload.get("production_job_ids") or ()),
        )

    async def get_execution_attempt_error(self, attempt_id: int) -> dict[str, object] | None:
        if not isinstance(attempt_id, int) or attempt_id < 1:
            return None
        try:
            response = await self.client.get(f"/production/execution-attempts/{attempt_id}/error", timeout=5.0)
            if response.status_code >= 400:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as exc:
            logger.warning("Execution-attempt error read failed attempt_id=%s: %s", attempt_id, exc)
            return None

    async def list_production_jobs(self) -> list[dict[str, object]]:
        try:
            response = await self.client.get("/production/jobs", timeout=5.0)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []
        except Exception as exc:
            logger.warning("Production job list read failed: %s", exc)
            return []

    async def get_production_announcement_snapshot(self, job_id: int) -> dict[str, object] | None:
        """Read existing durable monitoring routes after an identity trigger."""
        if not isinstance(job_id, int) or job_id < 1:
            return None
        try:
            responses = await __import__("asyncio").gather(
                self.client.get(f"/production/jobs/{job_id}", timeout=5.0),
                self.client.get(f"/production/jobs/{job_id}/material-deliveries", timeout=5.0),
                self.client.get(f"/production/jobs/{job_id}/incoming-qa/v02", timeout=5.0),
                self.client.get(f"/production/jobs/{job_id}/inspection", timeout=5.0),
                self.client.get(f"/production/jobs/{job_id}/events", timeout=5.0),
            )
        except Exception as exc:
            logger.warning("Production announcement read failed job_id=%s: %s", job_id, exc)
            return None
        job, deliveries, qa, inspection, events = responses
        if job.status_code >= 400:
            return None
        if any(response.status_code >= 500 for response in responses[1:]):
            return None
        return {
            "job": job.json(),
            "steps": (job.json() or {}).get("steps", []),
            "deliveries": deliveries.json() if deliveries.status_code < 400 else [],
            "incoming_qa": qa.json() if qa.status_code < 400 else {},
            "pre_roof_inspection": inspection.json() if inspection.status_code < 400 else {},
            "events": events.json() if events.status_code < 400 else [],
        }

    async def get_tts_audio(self, text: str) -> bytes:
        logger.info("Voice TTS request started text_length=%s", len(text))
        response = await self.client.post("/ai/tts", json={"text": text}, headers=self._runtime_headers(), timeout=10.0)
        response.raise_for_status()
        return response.content

    async def close(self):
        if self._owns_client:
            await self.client.aclose()
