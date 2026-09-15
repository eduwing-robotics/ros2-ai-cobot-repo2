from __future__ import annotations

import asyncio
import json
import logging

import uvicorn.config

from fastapi import Request
from api_server.routers import ai as ai_router
from api_server.services.command_interpreter import CommandInterpreter
from api_server.services.voice_timing import VoiceTiming, activate_voice_timing, logger as timing_logger, voice_timing_stage
from api_server.services.production_conversation_service import ProductionConversationResult
from shared.config import Settings
from shared.enums.ai import Intent
from shared.schemas.ai import StructuredCommand, TranscriptionResponse


def test_voice_timing_t8_is_off_by_default_and_has_no_active_stage() -> None:
    assert Settings().voice_timing_debug is False
    timing = VoiceTiming(endpoint="/ai/voice-conversation")
    with activate_voice_timing(None):
        with voice_timing_stage("ignored_ms"):
            pass
    assert timing.stages_ms == {}


def test_voice_timing_t9_records_real_measured_stages_only_when_enabled(caplog) -> None:
    timing = VoiceTiming(endpoint="/ai/voice-command", correlation_id="VOICEBENCH_T001")
    with activate_voice_timing(timing):
        with voice_timing_stage("stt_total_ms"):
            pass
        with voice_timing_stage("response_build_ms"):
            pass
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        timing.record(intent="QUERY_INVENTORY")
    records = [record for record in caplog.records if record.name == "uvicorn.error" and "VOICE_TIMING" in record.getMessage()]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage().removeprefix("VOICE_TIMING "))
    assert payload["event"] == "VOICE_TIMING"
    assert payload["endpoint"] == "/ai/voice-command"
    assert payload["correlation_id"] == "VOICEBENCH_T001"
    assert set(payload["stages_ms"]) == {"stt_total_ms", "response_build_ms"}


def test_voice_timing_t10_deterministic_unsafe_followup_has_no_ollama_stage() -> None:
    class UnexpectedLLM:
        async def chat(self, _text: str) -> str:
            raise AssertionError("deterministic unsupported route must not call Ollama")

    timing = VoiceTiming(endpoint="/ai/voice-conversation")
    with activate_voice_timing(timing):
        _, command, _ = asyncio.run(CommandInterpreter(UnexpectedLLM()).interpret("PLC 리셋해줘"))
    assert command.intent is Intent.UNKNOWN
    assert "interpreter_ms" in timing.stages_ms
    assert "deterministic_routing_ms" in timing.stages_ms
    assert "ollama_llm_ms" not in timing.stages_ms



def test_voice_timing_t9_route_logs_stages_without_changing_response(monkeypatch, caplog) -> None:
    async def fake_transcribe(_stt, _audio):
        return TranscriptionResponse(text="PLC 리셋해줘", processing_time_ms=1.0)

    class Service:
        async def handle_text(self, *, session_id: str, text: str):
            assert text == "PLC 리셋해줘"
            return ProductionConversationResult(
                session_id=session_id,
                message="지원하지 않는 명령입니다.",
                pending=None,
                command=StructuredCommand(
                    intent=Intent.UNKNOWN,
                    clarification_needed=True,
                    clarification_message="지원하지 않는 명령입니다.",
                ),
            )

    monkeypatch.setattr(ai_router, "do_transcribe", fake_transcribe)
    monkeypatch.setattr(ai_router, "get_settings", lambda: Settings(voice_timing_debug=True))
    request = Request({
        "type": "http", "method": "POST",
        "headers": [(b"x-voice-e2e-turn", b"VOICEBENCH_T001")],
    })
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = asyncio.run(ai_router.voice_conversation(
            session_id="VOICEBENCH_T001",
            audio=object(),
            stt=object(),
            service=Service(),
            http_request=request,
        ))
    assert response.session_id == "VOICEBENCH_T001"
    assert response.command is not None and response.command.intent is Intent.UNKNOWN
    assert response.conversation_state is None
    records = [record for record in caplog.records if record.name == "uvicorn.error" and "VOICE_TIMING" in record.getMessage()]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage().removeprefix("VOICE_TIMING "))
    assert payload["event"] == "VOICE_TIMING"
    assert payload["endpoint"] == "/ai/voice-conversation"
    assert payload["correlation_id"] == "VOICEBENCH_T001"
    assert 'stt_total_ms' in payload["stages_ms"]
    assert 'conversation_total_ms' in payload["stages_ms"]
    assert 'response_build_ms' in payload["stages_ms"]


def test_voice_timing_uses_uvicorn_info_logger_without_root_level_change() -> None:
    assert uvicorn.config.LOGGING_CONFIG["loggers"]["uvicorn"]["level"] == "INFO"
    assert timing_logger.name == "uvicorn.error"
    assert uvicorn.config.LOGGING_CONFIG["loggers"]["uvicorn"]["handlers"] == ["default"]
    assert logging.getLogger().getEffectiveLevel() >= logging.WARNING


def test_voice_timing_disabled_route_emits_no_record(monkeypatch, caplog) -> None:
    async def fake_transcribe(_stt, _audio):
        return TranscriptionResponse(text="PLC 리셋해줘", processing_time_ms=1.0)

    class Service:
        async def handle_text(self, *, session_id: str, text: str):
            return ProductionConversationResult(
                session_id=session_id,
                message="지원하지 않는 명령입니다.",
                pending=None,
                command=StructuredCommand(
                    intent=Intent.UNKNOWN,
                    clarification_needed=True,
                    clarification_message="지원하지 않는 명령입니다.",
                ),
            )

    monkeypatch.setattr(ai_router, "do_transcribe", fake_transcribe)
    monkeypatch.setattr(ai_router, "get_settings", lambda: Settings(voice_timing_debug=False))
    request = Request({"type": "http", "method": "POST", "headers": []})
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = asyncio.run(ai_router.voice_conversation(
            session_id="VOICEBENCH_DISABLED",
            audio=object(),
            stt=object(),
            service=Service(),
            http_request=request,
        ))
    assert response.command is not None and response.command.intent is Intent.UNKNOWN
    assert not [record for record in caplog.records if record.name == "uvicorn.error" and "VOICE_TIMING" in record.getMessage()]
