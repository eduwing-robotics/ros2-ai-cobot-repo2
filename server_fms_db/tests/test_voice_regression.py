import pytest
import asyncio
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import select

from shared.models.factory import PendingProductionState, RoofOptionCode, ProductionJob, Inventory, Part
from shared.schemas.ai import Intent, StructuredCommand
from api_server.services.command_interpreter import CommandInterpreter
from api_server.services.production_conversation_service import ProductionConversationService
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService

class MockLLM:
    def __init__(self, json_responses):
        self.json_responses = json_responses
        self.call_count = 0
    async def chat(self, prompt: str) -> str:
        resp = self.json_responses.get(prompt, '{"intent": "UNKNOWN"}')
        if isinstance(resp, list):
            r = resp[self.call_count % len(resp)]
            self.call_count += 1
            return r
        return resp

# We will just write a few helper functions to run tests
