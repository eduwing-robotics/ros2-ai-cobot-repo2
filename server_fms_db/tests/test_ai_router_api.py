"""HTTP-boundary tests; every AI dependency is replaced with a fake."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api_server.main import app
from api_server.routers.ai import get_interpreter
from api_server.routers.inventory import get_db
import api_server.routers.ai as ai_router
from api_server.services.command_interpreter import CommandInterpreter
from api_server.services.llm_service import LLMConnectionError, LLMTimeoutError
from api_server.services.stt_service import STTInferenceError, STTModelLoadError
from shared.enums.ai import Intent
from shared.schemas.ai import StructuredCommand, TranscriptionResponse


class FakeInterpreter:
    def __init__(self, command: StructuredCommand | Exception) -> None:
        self.command = command
        self.calls = 0

    async def interpret(self, text: str):
        self.calls += 1
        if isinstance(self.command, Exception):
            raise self.command
        return text.strip(), self.command, "{}"


class NeverCalledLLM:
    async def chat(self, _text: str):
        raise AssertionError("deterministic status query must not call Ollama")


class FakeSTT:
    def __init__(self, result: TranscriptionResponse | Exception) -> None:
        self.result = result
        self.loaded = False

    async def transcribe_upload(self, _audio):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def command(intent: Intent, **overrides) -> StructuredCommand:
    values = dict(intent=intent, requires_confirmation=intent in {Intent.CREATE_PRODUCTION_REQUEST, Intent.PAUSE_JOB, Intent.RESUME_JOB, Intent.CANCEL_JOB})
    values.update(overrides)
    return StructuredCommand(**values)


def client_with(interpreter=None, stt=None):
    app.dependency_overrides[get_db] = lambda: None
    if interpreter is not None:
        app.dependency_overrides[get_interpreter] = lambda: interpreter
    if stt is not None:
        app.dependency_overrides[ai_router.get_stt_service] = lambda stt=stt: stt
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_health_does_not_load_stt(monkeypatch):
    router = ai_router
    fake = FakeSTT(TranscriptionResponse(text="", processing_time_ms=0))
    monkeypatch.setattr(router, "get_stt_service", lambda: fake)
    monkeypatch.setattr(router, "get_llm_service", lambda: type("L", (), {"reachable": lambda _: __import__("asyncio").sleep(0, result=True)})())
    with TestClient(app) as client:
        response = client.get("/ai/health")
    assert response.status_code == 200 and response.json()["stt_loaded"] is False


def test_interpret_production_and_low_level_block():
    fake = FakeInterpreter(command(Intent.CREATE_PRODUCTION_REQUEST, product_name="A형 초소형 주택", product_code="TINY_HOUSE_A", quantity=1))
    with client_with(fake) as client:
        result = client.post("/ai/interpret", json={"text": "A형 초소형 주택 한 채 생산해줘."})
    assert result.status_code == 200 and result.json()["command"]["product_code"] == "TINY_HOUSE_A"
    assert "요청을 확인했습니다" in result.json()["suggested_response_text"]


def test_interpret_explicit_status_query_uses_deterministic_fastpath():
    interpreter = CommandInterpreter(NeverCalledLLM())
    with client_with(interpreter) as client:
        result = client.post("/ai/interpret", json={"text": "현재 무슨 작업 중이야?"})
    assert result.status_code == 200
    payload = result.json()["command"]
    assert payload["intent"] == Intent.QUERY_JOB_STATUS.value
    assert payload["clarification_needed"] is False


def test_interpret_timeout_and_connection_errors():
    with client_with(FakeInterpreter(LLMTimeoutError())) as client:
        assert client.post("/ai/interpret", json={"text": "A형"}).status_code == 504
    with client_with(FakeInterpreter(LLMConnectionError())) as client:
        assert client.post("/ai/interpret", json={"text": "A형"}).status_code == 503


def test_interpret_invalid_text_is_422():
    with client_with() as client:
        assert client.post("/ai/interpret", json={"text": "  "}).status_code == 422


def test_transcribe_success():
    ok = FakeSTT(TranscriptionResponse(text="A형 초소형 주택", language="ko", processing_time_ms=0))
    with client_with(stt=ok) as client:
        response = client.post("/ai/transcribe", files={"audio": ("a.wav", b"x", "audio/wav")})
    assert response.status_code == 200


def test_voice_command_uses_stt_then_interpreter():
    stt = FakeSTT(TranscriptionResponse(text="현재 생산이 어디까지 진행됐어?", language="ko", processing_time_ms=0))
    interpreter = FakeInterpreter(command(Intent.QUERY_JOB_STATUS))
    with client_with(interpreter, stt) as client:
        response = client.post("/ai/voice-command", files={"audio": ("a.wav", b"x", "audio/wav")})
    assert response.status_code == 200
    assert response.json()["command"]["intent"] == "QUERY_JOB_STATUS"
    assert interpreter.calls == 1
