import asyncio
import pytest
from api_server.services.command_interpreter import CommandInterpreter, extract_json, try_parse_deterministic_create
from api_server.services.command_interpreter import LLMResponseFormatError
from shared.enums.ai import Intent
from shared.models.factory import RoofOptionCode
from api_server.services.voice_timing import VoiceTiming, activate_voice_timing
class FakeLLM:
 def __init__(self, text): self.text=text
 async def chat(self, _): return self.text
def response(intent="CREATE_PRODUCTION_REQUEST", name="A형 초소형 주택", quantity=1, confirmation=True, clarification=False, message=None):
 import json
 return json.dumps({"intent":intent,"product_name":name,"product_code":None,"quantity":quantity,"target_job_id":None,"requires_confirmation":confirmation,"clarification_needed":clarification,"clarification_message":message})

def test_extract_wrapped_json(): assert extract_json('설명 ```json {"a":1} ``` 끝') == {'a':1}
def test_bad_json():
 with pytest.raises(LLMResponseFormatError): extract_json('{bad}')
def test_catalog_overwrites_code():
 _, c, _ = asyncio.run(CommandInterpreter(FakeLLM(response())).interpret('A형 초소형 주택 한 채 생산해줘'))
 assert c.product_code == 'HOUSE_A' and c.requires_confirmation
def test_ambiguous_product_needs_question():
 _, c, _ = asyncio.run(CommandInterpreter(FakeLLM(response(name=None, confirmation=False, clarification=True, message='무슨 모델인가요?'))).interpret('주택 만들어줘'))
 assert c.clarification_needed and c.product_code is None

def test_ambiguous_house_is_deterministic_without_llm():
 class NeverCalled:
  async def chat(self, _): raise AssertionError("LLM must not be called")
 _, command, _ = asyncio.run(CommandInterpreter(NeverCalled()).interpret("주택 만들어줘."))
 assert command.clarification_needed and command.product_code is None

def test_unknown_empty_optional_fields_are_normalized():
 class Fake:
  async def chat(self, _): return '{"intent":"UNKNOWN","product_name":"","quantity":"","target_job_id":"","clarification_needed":false,"clarification_message":""}'
 _, command, _ = asyncio.run(CommandInterpreter(Fake()).interpret("오늘 점심 뭐야?"))
 assert command.intent == Intent.UNKNOWN and command.clarification_needed

def test_unknown_message_is_server_fixed_not_model_text():
 class Fake:
  async def chat(self, _): return '{"intent":"UNKNOWN","clarification_needed":true,"clarification_message":"오늘 점심으로 무엇을 원하세요?"}'
 _, command, _ = asyncio.run(CommandInterpreter(Fake()).interpret("오늘 점심 뭐야?"))
 assert command.clarification_message == "지원하는 생산 시스템 명령으로 다시 말씀해 주세요."

def test_inventory_query_is_deterministic_without_llm():
 class NeverCalled:
  async def chat(self, _): raise AssertionError("LLM must not be called")
 _, command, _ = asyncio.run(CommandInterpreter(NeverCalled()).interpret("A 주택 자재 재고 조회해줘"))
 assert command.intent == Intent.QUERY_INVENTORY
 assert command.inventory_scope == "CATEGORY" and command.category_name == "주택 자재"


class CountingLLM:
 def __init__(self, text=None):
  self.text = text or '{"intent":"UNKNOWN","clarification_needed":true,"clarification_message":"fallback"}'
  self.calls = 0
 async def chat(self, _):
  self.calls += 1
  return self.text


@pytest.mark.parametrize("text", [
 "지금 생산 어디까지 됐어?",
 "현재 무슨 작업 중이야?",
 "지금 무슨 작업 중이야?",
 "현재 작업 뭐야?",
 "생산 진행 상황 알려줘",
 "생산 어디까지 진행됐어?",
 "현재 생산 단계 알려줘",
 "진행 중인 작업 알려줘",
 "진행 중인 공정 알려줘",
 "생산 끝났어?",
 "현재 생산 상태 알려줘",
])
def test_explicit_job_status_query_is_deterministic_without_ollama(text):
 llm = CountingLLM()
 timing = VoiceTiming(endpoint="/ai/interpret")
 with activate_voice_timing(timing):
  _, command, raw = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert llm.calls == 0 and raw == ""
 assert command.intent is Intent.QUERY_JOB_STATUS
 assert command.clarification_needed is False
 assert "deterministic_routing_ms" in timing.stages_ms
 assert "ollama_llm_ms" not in timing.stages_ms


@pytest.mark.parametrize("text", [
 "현재 작업 취소해줘",
 "A형 한 채 생산해줘",
 "생산 상태 변경해줘",
])
def test_job_status_fastpath_does_not_consume_non_query_commands(text):
 llm = CountingLLM()
 _, command, _ = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert command.intent is not Intent.QUERY_JOB_STATUS

@pytest.mark.parametrize("text", [
 ('전체 재고 알려줘'),
 ('전체 자재 재고 확인해줘'),
 ('모든 자재 재고 보여줘'),
 ('현재 모든 자재 재고 보여줘'),
 ('전부 재고 확인해줘'),
])
def test_explicit_whole_inventory_query_is_all_without_ollama(text):
 llm = CountingLLM()
 _, command, raw = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert llm.calls == 0 and raw == ''
 assert command.intent is Intent.QUERY_INVENTORY
 assert command.inventory_scope == 'ALL'
 assert command.item_name is None and command.category_name is None


@pytest.mark.parametrize(("text", "expected_item"), [
 ('외벽 재고 알려줘', '외벽'),
 ('ROOF_01 재고 확인해줘', 'ROOF_01'),
])
def test_specific_inventory_item_is_item_without_ollama(text, expected_item):
 llm = CountingLLM()
 _, command, raw = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert llm.calls == 0 and raw == ''
 assert command.intent is Intent.QUERY_INVENTORY
 assert command.inventory_scope == 'ITEM' and command.item_name == expected_item


@pytest.mark.parametrize('text', [
 '전체 작업 취소해줘',
 '모든 생산 작업 보여줘',
 '전체 공정 상태 알려줘',
])
def test_whole_set_words_without_inventory_context_do_not_become_inventory_all(text):
 llm = CountingLLM()
 _, command, _ = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert not (command.intent is Intent.QUERY_INVENTORY and command.inventory_scope == 'ALL')


def test_inventory_without_explicit_whole_scope_remains_item_and_is_not_noisy_all():
 llm = CountingLLM()
 _, command, raw = asyncio.run(CommandInterpreter(llm).interpret('자재 하나 재고 알려줘'))
 assert llm.calls == 0 and raw == ''
 assert command.intent is Intent.QUERY_INVENTORY
 assert command.inventory_scope == 'ITEM' and command.item_name == '자재 하나'


def test_ambiguous_generic_material_inventory_is_not_promoted_to_all():
 llm = CountingLLM()
 _, command, raw = asyncio.run(CommandInterpreter(llm).interpret('자재 재고 알려줘'))
 assert llm.calls == 0 and raw == ''
 assert command.intent is Intent.QUERY_INVENTORY
 assert command.inventory_scope == 'ITEM' and command.item_name == '자재'


@pytest.mark.parametrize(("text", "product_code", "quantity", "roof"), [
 ('A형 주택 한 채 평지붕으로 만들어줘', 'HOUSE_A', 1, RoofOptionCode.ROOF_01),
 ('A타입 주택 하나 평지붕으로 만들어줘', 'HOUSE_A', 1, RoofOptionCode.ROOF_01),
 ('B형 두 채 경사지붕으로 생산해줘', 'HOUSE_B', 2, RoofOptionCode.ROOF_02),
 ('A형 주택 한 채 만들어줘', 'HOUSE_A', 1, None),
 ('평지붕 A형 하나 만들어줘', 'HOUSE_A', 1, RoofOptionCode.ROOF_01),
 ('A타입으로 두 채 만들어줘', 'HOUSE_A', 2, None),
])
def test_strict_create_fastpath_returns_exact_command_without_ollama(text, product_code, quantity, roof):
 llm = CountingLLM()
 timing = VoiceTiming(endpoint='/ai/voice-conversation')
 with activate_voice_timing(timing):
  _, command, raw = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert llm.calls == 0 and raw == ''
 assert command.intent is Intent.CREATE_PRODUCTION_REQUEST
 assert command.product_code == product_code and command.quantity == quantity and command.roof_option_code is roof
 assert command.requires_confirmation and not command.clarification_needed
 assert 'deterministic_routing_ms' in timing.stages_ms and 'interpreter_ms' in timing.stages_ms
 assert 'ollama_llm_ms' not in timing.stages_ms and 'ollama_repair_ms' not in timing.stages_ms

@pytest.mark.parametrize(("text", "product_code"), [
 ('A형으로 생산해줘', 'HOUSE_A'), ('A 타입으로 생산해줘', 'HOUSE_A'),
 ('A타입 생산 시작해줘', 'HOUSE_A'), ('HOUSE A 생산해줘', 'HOUSE_A'),
 ('HOUSE_A로 만들어줘', 'HOUSE_A'), ('house a 생산해줘', 'HOUSE_A'), ('A 생산해줘', 'HOUSE_A'),
 ('에이형 생산해줘', 'HOUSE_A'), ('에이 타입 생산해줘', 'HOUSE_A'),
 ('B형으로 생산해줘', 'HOUSE_B'), ('B 타입으로 생산해줘', 'HOUSE_B'),
 ('B타입 생산 시작해줘', 'HOUSE_B'), ('HOUSE B 생산해줘', 'HOUSE_B'),
 ('HOUSE_B로 만들어줘', 'HOUSE_B'), ('house b 생산해줘', 'HOUSE_B'), ('B 생산해줘', 'HOUSE_B'),
 ('비형 생산해줘', 'HOUSE_B'), ('비 타입 생산해줘', 'HOUSE_B'),
])
def test_house_type_aliases_are_deterministic_only_for_create_context(text, product_code):
 llm = CountingLLM()
 _, command, raw = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert llm.calls == 0 and raw == ''
 assert command.intent is Intent.CREATE_PRODUCTION_REQUEST
 assert command.product_code == product_code and command.quantity == 1


def test_bare_house_letters_are_not_globally_normalized_outside_create_context():
 assert try_parse_deterministic_create('A 재고 확인해줘') is None
 assert try_parse_deterministic_create('B 상태를 확인해줘') is None
 llm = CountingLLM()
 _, command, _ = asyncio.run(CommandInterpreter(llm).interpret('A 주택 자재 재고 조회해줘'))
 assert command.intent is Intent.QUERY_INVENTORY
 assert llm.calls == 0


@pytest.mark.parametrize('text', [
 'A형이랑 B형 중에 재고 되는 걸로 두 채 만들어줘',
 '전에 만들었던 거랑 지붕만 다르게 해줘',
 '아까 말한 걸로 하나 더 만들어줘',
 'A형 아니고 B형으로 만들어줘',
 'A형 두체 평지붕으로 생산해줘',
 '적당한 걸로 하나 만들어줘',
 'A형 두 채랑 B형 한 채 만들어줘',
])
def test_ambiguous_create_uses_existing_ollama_fallback(text):
 llm = CountingLLM()
 _, command, raw = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert llm.calls == 1 and raw
 assert command.intent is Intent.UNKNOWN

@pytest.mark.parametrize('text', [
 'A형 B형 한 채 만들어줘',
 'A형 한 채 두 채 만들어줘',
 'A형 평지붕 경사지붕으로 만들어줘',
 'A형 하나 만들어줘, 아니 두 개',
])
def test_conflicting_create_values_never_take_fastpath(text):
 llm = CountingLLM()
 _, command, raw = asyncio.run(CommandInterpreter(llm).interpret(text))
 assert llm.calls == 1 and raw
 assert command.intent is Intent.UNKNOWN

def test_create_with_inventory_eligibility_falls_back_instead_of_querying_inventory():
 llm = CountingLLM()
 timing = VoiceTiming(endpoint='/ai/voice-conversation')
 with activate_voice_timing(timing):
  _, command, _ = asyncio.run(CommandInterpreter(llm).interpret('A형이랑 B형 중에 재고 되는 걸로 두 채 만들어줘'))
 assert llm.calls == 1
 assert command.intent is Intent.UNKNOWN
 assert 'ollama_llm_ms' in timing.stages_ms
 assert 'ollama_repair_ms' not in timing.stages_ms


def test_stt_like_critical_tokens_are_not_globally_normalized_into_commands():
 llm = CountingLLM()
 _, command, _ = asyncio.run(CommandInterpreter(llm).interpret('자제 제거 확인해줘'))
 assert llm.calls == 1
 assert command.intent is Intent.UNKNOWN
 assert try_parse_deterministic_create('A형 두체 평지붕으로 생산해줘') is None

def test_greeting_is_not_a_global_rejection_alias():
 from api_server.services.production_conversation_service import parse_confirmation_answer
 assert parse_confirmation_answer('안녕') is None
 assert parse_confirmation_answer('아니요') is False
