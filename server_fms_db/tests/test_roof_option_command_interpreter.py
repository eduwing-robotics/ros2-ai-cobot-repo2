from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from api_server.services.command_interpreter import (
    CommandInterpreter,
    extract_explicit_roof_option,
    extract_explicit_roof_options,
)
from shared.enums.ai import Intent
from shared.models.factory import RoofOptionCode


class FakeLLM:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = json.dumps(payload)

    async def chat(self, _text: str) -> str:
        return self.payload


def production_payload(*, roof_option_code: str | None = None, quantity: int = 1) -> dict[str, object]:
    return {
        "intent": "CREATE_PRODUCTION_REQUEST",
        "product_name": "A형 초소형 주택",
        "product_code": "MODEL_VALUE_MUST_NOT_BE_TRUSTED",
        "quantity": quantity,
        "target_job_id": None,
        "inventory_scope": None,
        "item_name": None,
        "category_name": None,
        "roof_option_code": roof_option_code,
        "requires_confirmation": True,
        "clarification_needed": False,
        "clarification_message": None,
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A형 한 채 평지붕으로 만들어줘", RoofOptionCode.ROOF_01),
        ("A형 한 채 평 지붕으로 만들어줘", RoofOptionCode.ROOF_01),
        ("A형 한 채 평평한 지붕으로 만들어줘", RoofOptionCode.ROOF_01),
        ("A형 한 채 판판한 지붕으로 만들어줘", RoofOptionCode.ROOF_01),
        ("A형 한 채 1번 지붕으로 만들어줘", RoofOptionCode.ROOF_01),
        ("A형 한 채 지붕 1번으로 만들어줘", RoofOptionCode.ROOF_01),
        ("A형 한 채 지붕01으로 만들어줘", RoofOptionCode.ROOF_01),
        ("B형 한 채 경사지붕으로 만들어줘", RoofOptionCode.ROOF_02),
        ("B형 한 채 경사 지붕으로 만들어줘", RoofOptionCode.ROOF_02),
        ("B형 한 채 2번 지붕으로 만들어줘", RoofOptionCode.ROOF_02),
        ("B형 한 채 지붕 2번으로 만들어줘", RoofOptionCode.ROOF_02),
        ("B형 한 채 지붕02으로 만들어줘", RoofOptionCode.ROOF_02),
    ],
)
def test_extract_explicit_roof_option_aliases(text: str, expected: RoofOptionCode) -> None:
    assert extract_explicit_roof_option(text) is expected


@pytest.mark.parametrize("text", ["1번", "2번", "S3 공정 상태 알려줘", "2번 공정 상태 알려줘", "FR5 상태 확인해줘"])
def test_roof_extraction_does_not_misread_bare_or_unrelated_numbers(text: str) -> None:
    assert extract_explicit_roof_option(text) is None


def test_explicit_roof_alias_overrides_wrong_llm_value() -> None:
    _, command, _ = asyncio.run(
        CommandInterpreter(FakeLLM(production_payload(roof_option_code="ROOF_02"))).interpret(
            "A형 한 채 평지붕으로 만들어줘"
        )
    )

    assert command.intent is Intent.CREATE_PRODUCTION_REQUEST
    assert command.product_code == "HOUSE_A"
    assert command.quantity == 1
    assert command.roof_option_code is RoofOptionCode.ROOF_01


def test_numeric_roof_alias_does_not_replace_explicit_production_quantity() -> None:
    _, command, _ = asyncio.run(
        CommandInterpreter(FakeLLM(production_payload(quantity=10))).interpret(
            "A형 두 채 1번 지붕으로 만들어줘"
        )
    )

    assert command.product_code == "HOUSE_A"
    assert command.quantity == 2
    assert command.roof_option_code is RoofOptionCode.ROOF_01


def test_missing_roof_stays_nullable_for_existing_production_request() -> None:
    _, command, _ = asyncio.run(
        CommandInterpreter(FakeLLM(production_payload())).interpret("A형 한 채 만들어줘")
    )

    assert command.product_code == "HOUSE_A"
    assert command.quantity == 1
    assert command.roof_option_code is None
    assert command.clarification_needed is False


@pytest.mark.parametrize(
    "text",
    [
        "A형 한 채 1번 지붕이랑 2번 지붕 중 하나로 만들어줘",
        "A형 한 채 1번 말고 2번 지붕으로 만들어줘",
        "A형 한 채 평지붕 말고 경사지붕으로 만들어줘",
    ],
)
def test_conflicting_roof_aliases_require_clarification_without_selecting_one(text: str) -> None:
    assert extract_explicit_roof_options(text) == frozenset(
        {RoofOptionCode.ROOF_01, RoofOptionCode.ROOF_02}
    )

    _, command, _ = asyncio.run(CommandInterpreter(FakeLLM(production_payload())).interpret(text))

    assert command.roof_option_code is None
    assert command.clarification_needed is True
    assert command.requires_confirmation is False
    assert command.clarification_message


@pytest.mark.parametrize(
    "text,payload",
    [
        ("S3 공정 상태 알려줘", {"intent": "QUERY_JOB_STATUS", "roof_option_code": "ROOF_01"}),
        ("2번 공정 상태 알려줘", {"intent": "QUERY_JOB_STATUS", "roof_option_code": "ROOF_02"}),
    ],
)
def test_non_production_intents_do_not_keep_roof_option(text: str, payload: dict[str, object]) -> None:
    _, command, _ = asyncio.run(CommandInterpreter(FakeLLM(payload)).interpret(text))

    assert command.intent is Intent.QUERY_JOB_STATUS
    assert command.roof_option_code is None


def test_unsafe_fr5_command_has_no_roof_option() -> None:
    class NeverCalled:
        async def chat(self, _text: str) -> str:
            raise AssertionError("unsafe pre-filter must stop before LLM")

    _, command, _ = asyncio.run(CommandInterpreter(NeverCalled()).interpret("FR5 상태 확인해줘"))
    assert command.intent is Intent.UNKNOWN
    assert command.roof_option_code is None


@pytest.mark.parametrize("value", ["ROOF_03", "ROOF_A", "FLAT", "SLOPED"])
def test_invalid_canonical_roof_code_is_rejected(value: str) -> None:
    payload = production_payload(roof_option_code=value)
    with pytest.raises(ValidationError):
        CommandInterpreter._validate_llm_payload(payload)
