from __future__ import annotations

import httpx

from shared.config import Settings, get_settings


class LLMConfigurationError(RuntimeError): pass
class LLMConnectionError(RuntimeError): pass
class LLMTimeoutError(RuntimeError): pass


SYSTEM_PROMPT = """한국어 초소형 주택 생산 명령을 JSON 객체 하나로 분류한다. JSON 외 텍스트는 금지한다.
intent는 CREATE_PRODUCTION_REQUEST, PAUSE_JOB, RESUME_JOB, CANCEL_JOB, QUERY_JOB_STATUS, UNKNOWN 중 하나다.
반드시 intent, product_name, quantity, target_job_id, inventory_scope, item_name, category_name, roof_option_code, clarification_needed, clarification_message를 포함한다. 재고 수량은 추측하지 않는다.
roof_option_code는 지붕이 명시되었을 때만 ROOF_01 또는 ROOF_02이며, 없으면 null이다. 제품이 불명확하면 추측하지 말고 clarification_needed=true로 한다. 로봇 관절·좌표·PLC·ROS 저수준 제어와 일반 지식 질문은 UNKNOWN이다.
product_code와 requires_confirmation은 넣지 않는다."""


class OllamaService:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.ollama_timeout_seconds)
        return self._client

    async def chat(self, user_prompt: str) -> str:
        if not self.settings.ollama_model.strip():
            raise LLMConfigurationError("OLLAMA_MODEL 환경변수가 설정되지 않았습니다.")
        try:
            response = await (await self._get_client()).post(
                f"{self.settings.ollama_base_url.rstrip('/')}/api/chat",
                json={"model": self.settings.ollama_model, "stream": False, "format": "json", "options": {"temperature": 0, "num_predict": 160}, "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}
                ]},
            )
            response.raise_for_status()
            return str(response.json()["message"]["content"])
        except httpx.TimeoutException as error:
            raise LLMTimeoutError("Ollama 응답 시간이 초과되었습니다.") from error
        except (httpx.HTTPError, KeyError, ValueError) as error:
            raise LLMConnectionError("Ollama에 연결하거나 응답을 읽지 못했습니다.") from error

    async def reachable(self) -> bool:
        try:
            response = await (await self._get_client()).get(f"{self.settings.ollama_base_url.rstrip('/')}/api/tags", timeout=2.0)
            return response.is_success
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()


_service: OllamaService | None = None
def get_llm_service() -> OllamaService:
    global _service
    if _service is None: _service = OllamaService()
    return _service
