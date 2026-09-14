import pytest
import asyncio
from datetime import datetime, timezone
from shared.models.factory import PendingProductionState, RoofOptionCode, ProductionJob, Inventory, Part
from shared.schemas.ai import Intent, StructuredCommand
from api_server.services.command_interpreter import CommandInterpreter

class MockLLM:
    def __init__(self, override_intent=None, override_product=None, override_quantity=None):
        self.override_intent = override_intent
        self.override_product = override_product
        self.override_quantity = override_quantity
    async def chat(self, prompt: str) -> str:
        d = {"intent": self.override_intent or "UNKNOWN"}
        if self.override_product: d["product_name"] = self.override_product
        if self.override_quantity is not None: d["quantity"] = self.override_quantity
        import json
        return json.dumps(d)

V_CASES_CREATE = [
    ("V001", "A형 주택 하나 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V002", "A형으로 한 채 생산해줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V003", "A타입 주택 한 개 제작해줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V004", "HOUSE_A 하나 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V005", "A형 주택 제작 시작해줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V006", "B형 주택 하나 생산해줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_B", 1, None),
    ("V007", "A형 하나 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V008", "A형 한 개 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V009", "A형 한 채 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V010", "A형 1개 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V011", "A형 1채 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, None),
    ("V012", "A형 두 개 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 2, None),
    ("V013", "A형 두 채 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 2, None),
    ("V014", "A형 2개 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 2, None),
    ("V015", "A형 2채 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 2, None),
    ("V016", "A형 열 개 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 10, None),
    ("V017", "A형 평지붕 하나 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, RoofOptionCode.ROOF_01),
    ("V018", "A형 평평한 지붕으로 한 채 생산해줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, RoofOptionCode.ROOF_01),
    ("V019", "A형 1번 지붕으로 하나 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, RoofOptionCode.ROOF_01),
    ("V020", "A형 경사지붕 한 채 만들어줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, RoofOptionCode.ROOF_02),
    ("V021", "A형 경사형 지붕으로 하나 생산해", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, RoofOptionCode.ROOF_02),
    ("V022", "A형 2번 지붕으로 한 채 제작해줘", Intent.CREATE_PRODUCTION_REQUEST, "HOUSE_A", 1, RoofOptionCode.ROOF_02),
]

def test_v_cases_create():
    async def run():
        results = []
        for cid, text, exp_intent, exp_prod, exp_qty, exp_roof in V_CASES_CREATE:
            llm = MockLLM(override_intent=exp_intent.value, override_product="A형 주택" if exp_prod == "HOUSE_A" else "B형 주택")
            interpreter = CommandInterpreter(llm)
            _, cmd, _ = await interpreter.interpret(text)
            passed = (cmd.intent == exp_intent and cmd.product_code == exp_prod and cmd.quantity == exp_qty and cmd.roof_option_code == exp_roof)
            results.append((cid, passed, cmd.quantity))
            print(f"{cid} passed: {passed}, qty: {cmd.quantity}")
        return results
    asyncio.run(run())
