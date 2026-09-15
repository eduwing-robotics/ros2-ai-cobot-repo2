from __future__ import annotations

import json
import re

from pydantic import ValidationError
from api_server.services.voice_timing import voice_timing_stage

from api_server.services.llm_service import OllamaService
from api_server.services.temporary_product_catalog import PRODUCTS, find_product
from shared.enums.ai import Intent
from shared.models.factory import RoofOptionCode
from shared.schemas.ai import CHANGING_INTENTS, StructuredCommand

UNSAFE_CONTROL_PATTERN = re.compile(r"\b(fr5|zkbot|turtlebot|moveit|plc|ros)\b|관절|레지스터|\b[md]\d+", re.IGNORECASE)
ROOF_OPTION_ALIAS_FRAGMENT = (
    r"(?:평\s*지붕|평평한\s*지붕|판판한\s*지붕|1\s*번\s*지붕|"
    r"지붕\s*0?1(?!\d)(?:\s*번)?|경사\s*지붕|2\s*번\s*지붕|"
    r"지붕\s*0?2(?!\d)(?:\s*번)?)"
)
ROOF_OPTION_ALIAS_PATTERNS = {
    RoofOptionCode.ROOF_01: re.compile(
        r"(?:평\s*지붕|평평한\s*지붕|판판한\s*지붕|1\s*번\s*지붕|"
        r"지붕\s*0?1(?!\d)(?:\s*번)?)"
    ),
    RoofOptionCode.ROOF_02: re.compile(
        r"(?:경사\s*지붕|경사형\s*지붕|2\s*번\s*지붕|지붕\s*0?2(?!\d)(?:\s*번)?)"
    ),
}
ROOF_OPTION_NUMERIC_CORRECTION_PATTERN = re.compile(
    r"(?:1\s*번\s*말고\s*2\s*번\s*지붕|2\s*번\s*말고\s*1\s*번\s*지붕)"
)
PRODUCTION_QUANTITY_WITH_UNIT_PATTERN = re.compile(
    rf"(?P<quantity>[1-9]\d*|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|하나|둘|셋|넷)"
    rf"\s*(?:개|채)(?:를|을)?(?:\s*{ROOF_OPTION_ALIAS_FRAGMENT}\s*(?:으로|로)?)?"
    rf"\s*(?:생산|만들|제작|조립|시작)"
)
BARE_ONE_PRODUCTION_QUANTITY_PATTERN = re.compile(r"하나\s*(?:를|을)?\s*(?:생산|만들|제작|조립|시작)")
INVENTORY_QUERY_PATTERN = re.compile(r"재고|몇 개 남|얼마나 남|몇 개 있")
INVENTORY_ALL_SCOPE_PATTERN = re.compile(r"(?:전체|모든|전부)")
INVENTORY_ITEM_FILLER_PATTERN = re.compile(
    r"(?:현재\s*|재고|조회|확인해줘|알려줘|보여줘|몇\s*개\s*남았어\?*|"
    r"얼마나\s*남았어\?*|몇\s*개\s*있어\?*)"
)
# Complete, operator-facing production-status questions only.  Mentions of a
# production step without a question cue remain on the LLM path.
JOB_STATUS_QUERY_PATTERNS = (
    re.compile(r"(?:현재|지금)\s*(?:생산\s*)?어디까지\s*(?:진행\s*)?(?:됐어|되었어|야|인지|알려줘)?[?？!！.]*$"),
    re.compile(r"(?:현재|지금)\s*(?:무슨\s*)?(?:작업|공정)\s*(?:중(?:이야|이냐|인지)?|뭐야|뭔지|알려줘|확인해줘)[?？!！.]*$"),
    re.compile(r"(?:현재|지금)\s*작업\s*(?:뭐야|뭔지|알려줘|확인해줘)[?？!！.]*$"),
    re.compile(r"(?:현재\s*)?생산\s*(?:진행\s*상황|어디까지\s*진행|단계|상태)\s*(?:알려줘|알려\s*줘|확인해줘|확인|됐어|되었어|야|인지)?[?？!！.]*$"),
    re.compile(r"진행\s*중인\s*(?:작업|공정)\s*(?:알려줘|알려\s*줘|확인해줘|확인|뭐야|뭔지)?[?？!！.]*$"),
    re.compile(r"생산\s*끝났어[?？!！.]*$"),
)


KOREAN_PRODUCTION_QUANTITIES = {
    "한": 1,
    "하나": 1,
    "두": 2,
    "둘": 2,
    "세": 3,
    "셋": 3,
    "네": 4,
    "넷": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}

# This parser is deliberately narrower than the LLM path.  It only accepts a
# complete, explicit CREATE shape; every unclear form remains an Ollama task.
CREATE_ACTION_PATTERN = re.compile(r"(?:생산|만들|제작|조립|시작)")
STRICT_CREATE_QUANTITY_PATTERN = re.compile(
    r"(?P<quantity>[1-9]\d*|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|하나|둘|셋|넷)"
    r"\s*(?:개|채)(?:를|을)?"
)
# Keep STT corruption such as "두체" out of the default-one path.  A
# genuinely omitted quantity remains valid; a malformed stated quantity does not.
STRICT_CREATE_MALFORMED_QUANTITY_PATTERN = re.compile(
    r"(?:[1-9]\d*|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|하나|둘|셋|넷)\s*체"
)
STRICT_CREATE_BARE_ONE_PATTERN = re.compile(
    rf"하나\s*(?:를|을)?(?:\s*{ROOF_OPTION_ALIAS_FRAGMENT}\s*(?:으로|로)?)?"
    r"\s*(?:생산|만들|제작|조립|시작)"
)
STRICT_CREATE_FALLBACK_PATTERN = re.compile(
    r"(?:아까|전에|저번|그걸로|같은\s*걸로|다르게|비슷한\s*걸로|"
    r"중에|재고\s*(?:되는|많은)|가능한\s*걸로|적당한\s*걸로|아니고|말고|제외)"
)
# These patterns run only after a complete CREATE verb was found.  In
# particular, the bare A/B forms are not a global text replacement rule.
STRICT_CREATE_PRODUCT_PATTERNS = {
    "HOUSE_A": (
        re.compile(r"(?<![a-z0-9_])house\s*_?\s*a(?![a-z0-9_])", re.IGNORECASE),
        re.compile(r"(?<![a-z0-9_])a\s*(?:형|타입)(?:\s*주택)?(?![a-z0-9_])", re.IGNORECASE),
        re.compile(r"에이\s*(?:형|타입)(?:\s*주택)?"),
        re.compile(r"(?<![a-z0-9_])a(?![a-z0-9_])", re.IGNORECASE),
    ),
    "HOUSE_B": (
        re.compile(r"(?<![a-z0-9_])house\s*_?\s*b(?![a-z0-9_])", re.IGNORECASE),
        re.compile(r"(?<![a-z0-9_])b\s*(?:형|타입)(?:\s*주택)?(?![a-z0-9_])", re.IGNORECASE),
        re.compile(r"비\s*(?:형|타입)(?:\s*주택)?"),
        re.compile(r"(?<![a-z0-9_])b(?![a-z0-9_])", re.IGNORECASE),
    ),
}


class LLMResponseFormatError(RuntimeError): pass


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_explicit_job_status_query(text: str) -> bool:
    """Return True only for complete, unambiguous production-status questions."""
    return any(pattern.fullmatch(text) for pattern in JOB_STATUS_QUERY_PATTERNS)


def extract_explicit_production_quantity(text: str) -> int | None:
    """Return an explicitly stated production count, never arbitrary identifiers."""
    match = PRODUCTION_QUANTITY_WITH_UNIT_PATTERN.search(text)
    if match:
        value = match.group("quantity")
        return int(value) if value.isdigit() else KOREAN_PRODUCTION_QUANTITIES[value]
    if BARE_ONE_PRODUCTION_QUANTITY_PATTERN.search(text):
        return 1
    return None


def extract_explicit_roof_options(text: str) -> frozenset[RoofOptionCode]:
    """Return explicit roof options; numeric corrections intentionally signal conflict."""
    options = {
        code for code, pattern in ROOF_OPTION_ALIAS_PATTERNS.items() if pattern.search(text)
    }
    if ROOF_OPTION_NUMERIC_CORRECTION_PATTERN.search(text):
        options.update({RoofOptionCode.ROOF_01, RoofOptionCode.ROOF_02})
    return frozenset(options)


def extract_explicit_roof_option(text: str) -> RoofOptionCode | None:
    """Return one explicit roof option; conflicts intentionally resolve to None."""
    options = extract_explicit_roof_options(text)
    return next(iter(options)) if len(options) == 1 else None


def _extract_strict_create_product_codes(text: str) -> tuple[str, ...]:
    return tuple(
        product.code
        for product in PRODUCTS
        if any(pattern.search(text) for pattern in STRICT_CREATE_PRODUCT_PATTERNS[product.code])
    )


def _extract_strict_create_quantities(text: str) -> tuple[int, ...]:
    values = [
        int(value) if value.isdigit() else KOREAN_PRODUCTION_QUANTITIES[value]
        for value in (match.group("quantity") for match in STRICT_CREATE_QUANTITY_PATTERN.finditer(text))
    ]
    if STRICT_CREATE_BARE_ONE_PATTERN.search(text):
        values.append(1)
    return tuple(values)


def try_parse_deterministic_create(text: str) -> StructuredCommand | None:
    """Return only a fully explicit CREATE command, otherwise leave interpretation to Ollama."""
    if (
        not CREATE_ACTION_PATTERN.search(text)
        or STRICT_CREATE_FALLBACK_PATTERN.search(text)
        or STRICT_CREATE_MALFORMED_QUANTITY_PATTERN.search(text)
    ):
        return None
    product_codes = _extract_strict_create_product_codes(text)
    quantities = _extract_strict_create_quantities(text)
    roof_options = extract_explicit_roof_options(text)
    if len(product_codes) != 1 or len(quantities) > 1 or len(roof_options) > 1:
        return None
    product = next(product for product in PRODUCTS if product.code == product_codes[0])
    return StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name=product.canonical_name,
        product_code=product.code,
        # CREATE requests without an explicit count retain the established one-house default.
        quantity=quantities[0] if quantities else 1,
        roof_option_code=next(iter(roof_options)) if roof_options else None,
        requires_confirmation=True,
    )


def extract_json(raw: str) -> dict:
    """Extract the first complete JSON object without confusing braces in strings."""
    cleaned = re.sub(r"```(?:json)?|```", "", raw, flags=re.IGNORECASE).strip()
    start = cleaned.find("{")
    if start < 0:
        raise LLMResponseFormatError("LLM이 JSON 객체를 반환하지 않았습니다.")
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as error:
        raise LLMResponseFormatError("LLM JSON 형식이 올바르지 않습니다.") from error
    if not isinstance(value, dict):
        raise LLMResponseFormatError("LLM JSON 최상위 값은 객체여야 합니다.")
    return value


class CommandInterpreter:
    def __init__(self, llm: OllamaService) -> None: self.llm = llm

    @staticmethod
    def _validate_llm_payload(payload: dict) -> StructuredCommand:
        # Empty strings from a model are absent optional values, never quantities or IDs.
        for key in ("product_name", "product_code", "roof_option_code", "quantity", "target_job_id", "inventory_scope", "item_name", "category_name"):
            if payload.get(key) == "":
                payload[key] = None
        intent = Intent(payload.get("intent", Intent.UNKNOWN))
        if intent != Intent.CREATE_PRODUCTION_REQUEST and payload.get("quantity") == 0:
            payload["quantity"] = None
        payload["requires_confirmation"] = intent in CHANGING_INTENTS
        if intent in {Intent.QUERY_JOB_STATUS, Intent.QUERY_INVENTORY}:
            payload["requires_confirmation"] = False
        if intent == Intent.UNKNOWN:
            payload["clarification_needed"] = True
            payload["clarification_message"] = "지원하는 생산 시스템 명령으로 다시 말씀해 주세요."
        if intent == Intent.CREATE_PRODUCTION_REQUEST and not payload.get("product_name"):
            payload["clarification_needed"] = True
            payload["clarification_message"] = payload.get("clarification_message") or "생산할 초소형 주택 모델을 말씀해 주세요."
        return StructuredCommand.model_validate(payload)

    async def interpret(self, original_text: str) -> tuple[str, StructuredCommand, str]:
        with voice_timing_stage("interpreter_ms"):
            normalized = normalize_text(original_text)
            with voice_timing_stage("deterministic_routing_ms"):
                if UNSAFE_CONTROL_PATTERN.search(normalized):
                    return normalized, StructuredCommand(intent=Intent.UNKNOWN, clarification_needed=True,
                        clarification_message="로봇 저수준 제어 명령은 이 API에서 지원하지 않습니다."), ""
                strict_create = try_parse_deterministic_create(normalized)
                if strict_create is not None:
                    return normalized, strict_create, ""
                # Classification is fast, but the conversation layer remains responsible
                # for the authoritative production-status DB query.
                if is_explicit_job_status_query(normalized):
                    return normalized, StructuredCommand(
                        intent=Intent.QUERY_JOB_STATUS,
                        clarification_needed=False,
                    ), ""
                # Inventory lookup is deterministic: do not let a model confuse it with job status.
                # A CREATE sentence mentioning inventory eligibility is not a lookup.
                if not CREATE_ACTION_PATTERN.search(normalized) and INVENTORY_QUERY_PATTERN.search(normalized):
                    category = next((name for name in ("화장실", "주방", "주택 자재") if name in normalized), None)
                    # Whole-set terms are meaningful only after this request was already
                    # identified as an inventory query. They must never become an item name.
                    explicit_all_scope = category is None and bool(INVENTORY_ALL_SCOPE_PATTERN.search(normalized))
                    item = (
                        None
                        if category or explicit_all_scope
                        else INVENTORY_ITEM_FILLER_PATTERN.sub("", normalized).strip() or None
                    )
                    return normalized, StructuredCommand(intent=Intent.QUERY_INVENTORY,
                        inventory_scope="CATEGORY" if category else ("ALL" if explicit_all_scope or item is None else "ITEM"),
                        item_name=item, category_name=category, clarification_needed=False), ""
                # Product model selection is a deterministic server rule, not an LLM guess.
                if "주택" in normalized and re.search(r"생산|만들|조립", normalized) and find_product(normalized) is None:
                    return normalized, StructuredCommand(intent=Intent.CREATE_PRODUCTION_REQUEST, quantity=1,
                        clarification_needed=True, clarification_message="생산할 초소형 주택 모델을 말씀해 주세요."), ""
            with voice_timing_stage("ollama_llm_ms"):
                raw = await self.llm.chat(normalized)
            try:
                command = self._validate_llm_payload(extract_json(raw))
            except (ValueError, ValidationError, LLMResponseFormatError):
                with voice_timing_stage("ollama_repair_ms"):
                    repair = await self.llm.chat(f"다음 응답을 지정 JSON만으로 수정해: {raw[:1200]}")
                try: command = self._validate_llm_payload(extract_json(repair))
                except (ValidationError, LLMResponseFormatError) as error: raise LLMResponseFormatError("LLM 응답을 명령 형식으로 해석할 수 없습니다.") from error
                raw = repair
            return normalized, self._apply_server_rules(command, normalized), raw

    def _apply_server_rules(self, command: StructuredCommand, text: str) -> StructuredCommand:
        values = command.model_dump()
        values["requires_confirmation"] = command.intent in CHANGING_INTENTS
        # A roof choice is meaningful only for a confirmed production-request shape.
        values["roof_option_code"] = None
        if command.intent == Intent.CREATE_PRODUCTION_REQUEST:
            product = find_product(command.product_name, text)
            if product is None:
                values.update(product_name=None, product_code=None, clarification_needed=True,
                              clarification_message="생산할 초소형 주택 모델을 말씀해 주세요.", requires_confirmation=False)
            else:
                explicit_quantity = extract_explicit_production_quantity(text)
                roof_options = extract_explicit_roof_options(text)
                if len(roof_options) > 1:
                    values.update(
                        product_name=product.canonical_name,
                        product_code=product.code,
                        quantity=explicit_quantity if explicit_quantity is not None else command.quantity or 1,
                        roof_option_code=None,
                        clarification_needed=True,
                        clarification_message="지붕 옵션이 서로 다르게 지정되었습니다. 하나의 지붕 옵션을 말씀해 주세요.",
                        requires_confirmation=False,
                    )
                else:
                    values.update(product_name=product.canonical_name, product_code=product.code,
                                  quantity=explicit_quantity if explicit_quantity is not None else command.quantity or 1,
                                  roof_option_code=extract_explicit_roof_option(text),
                                  clarification_needed=False, clarification_message=None, requires_confirmation=True)
        elif command.intent == Intent.UNKNOWN:
            values.update(clarification_needed=True, clarification_message="지원하는 생산 시스템 명령으로 다시 말씀해 주세요.", requires_confirmation=False)
        elif command.intent in {Intent.QUERY_JOB_STATUS, Intent.QUERY_INVENTORY}:
            values["requires_confirmation"] = False
        return StructuredCommand.model_validate(values)
