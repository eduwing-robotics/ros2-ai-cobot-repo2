import asyncio

from fastapi.testclient import TestClient

from api_server.main import app
import api_server.routers.ai as ai_router
from api_server.services.response_message_builder import ResponseMessageBuilder
from api_server.services.tts.base import TTSProvider
from api_server.services.tts.service import TTSService
from shared.config import Settings
from shared.enums.ai import Intent


class FakeProvider(TTSProvider):
    async def synthesize(self, _text): return b"fake-mp3"
    async def health(self): return True


def test_tts_service_and_text_validation():
    settings = Settings(tts_voice="test", tts_max_text_length=5)
    service = TTSService(settings, FakeProvider())
    assert asyncio.run(service.synthesize("안녕")).audio == b"fake-mp3"
    try: asyncio.run(service.synthesize(""))
    except ValueError: pass
    else: assert False


def test_response_message_builder_templates():
    builder = ResponseMessageBuilder()
    assert "A형" in builder.build(Intent.CREATE_PRODUCTION_REQUEST, {"product_name":"A형 초소형 주택", "quantity":1})
    assert "지붕은 5개" in builder.build(Intent.QUERY_INVENTORY, {"item_name":"지붕", "quantity":5})
    assert builder.build(Intent.UNKNOWN, {}) == "현재 지원하지 않는 요청입니다."
    class Command: intent=Intent.CREATE_PRODUCTION_REQUEST; clarification_needed=False; product_name='A형 초소형 주택'; quantity=1
    assert '요청을 확인했습니다' in builder.build_interpretation(Command())


def test_tts_api_mock(monkeypatch):
    service = TTSService(Settings(tts_voice="test"), FakeProvider())
    monkeypatch.setattr(ai_router, "get_tts_service", lambda: service)
    with TestClient(app) as client:
        response = client.post("/ai/tts", json={"text":"테스트입니다."})
        blank = client.post("/ai/tts", json={"text":" "})
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg" and response.content == b"fake-mp3"
    assert blank.status_code == 422
