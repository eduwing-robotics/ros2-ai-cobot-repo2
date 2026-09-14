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
        import json
        return json.dumps(d)

V_CASES_QUERY = [
    ("V041", "재고 알려줘", Intent.QUERY_INVENTORY, "ALL", None),
    ("V042", "현재 자재 재고 보여줘", Intent.QUERY_INVENTORY, "ALL", None),
    ("V043", "지붕 재고 몇 개 있어?", Intent.QUERY_INVENTORY, "ALL", None),
    ("V044", "외벽 몇 개 남았어?", Intent.QUERY_INVENTORY, "ITEM", "외벽"),
    ("V045", "화장실 자재 재고 알려줘", Intent.QUERY_INVENTORY, "CATEGORY", None),
]

def test_v_cases_query():
    async def run():
        results = []
        for cid, text, exp_intent, exp_scope, exp_item in V_CASES_QUERY:
            llm = MockLLM(override_intent=exp_intent.value)
            interpreter = CommandInterpreter(llm)
            _, cmd, _ = await interpreter.interpret(text)
            passed = (cmd.intent == exp_intent)
            if exp_scope: passed = passed and (cmd.inventory_scope == exp_scope)
            if exp_item: passed = passed and (cmd.item_name == exp_item)
            results.append((cid, passed, cmd.inventory_scope, cmd.item_name))
            print(f"{cid} passed: {passed}, scope: {cmd.inventory_scope}, item: {cmd.item_name}")
        return results
    asyncio.run(run())
