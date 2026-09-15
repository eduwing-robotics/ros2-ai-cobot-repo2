from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from api_server.services.production_conversation_service import (
    ProductionConversationService,
    extract_roof_followup,
    parse_confirmation_answer,
)
from shared.enums.ai import Intent
from shared.models import Base
from shared.models.factory import (
    PendingProductionRequest,
    PendingProductionState,
    Product,
    ProductionJob,
    RoofOptionCode,
)
from shared.schemas.ai import StructuredCommand
from shared.services.pending_production_request_service import PendingProductionRequestService


class FakeInterpreter:
    def __init__(self, command: StructuredCommand) -> None:
        self.command = command
        self.calls = 0

    async def interpret(self, text: str):
        self.calls += 1
        return text.strip(), self.command, "{}"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    db.add_all([Product(product_code="HOUSE_A", product_name="A형 주택"), Product(product_code="HOUSE_B", product_name="B형 주택")])
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc)


def create_command(*, product_code: str = "HOUSE_A", quantity: int = 1, roof: RoofOptionCode | None = None) -> StructuredCommand:
    return StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택" if product_code == "HOUSE_A" else "B형 주택",
        product_code=product_code,
        quantity=quantity,
        roof_option_code=roof,
        requires_confirmation=True,
    )


def conversation(session: Session, now: datetime, interpreter: FakeInterpreter, *, ttl_seconds: int = 300) -> ProductionConversationService:
    return ProductionConversationService(
        pending_service=PendingProductionRequestService(session, ttl_seconds=ttl_seconds, clock=lambda: now),
        interpreter=interpreter,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1번", RoofOptionCode.ROOF_01),
        (" 2번. ", RoofOptionCode.ROOF_02),
        ("평지붕", RoofOptionCode.ROOF_01),
        ("경사지붕", RoofOptionCode.ROOF_02),
        ("1번 지붕", RoofOptionCode.ROOF_01),
        ("3번", None),
    ],
)
def test_roof_followup_parser_is_context_specific(text: str, expected: RoofOptionCode | None) -> None:
    assert extract_roof_followup(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("네", True), ("예", True), ("응", True), ("맞아", True), ("맞아요", True), ("맞습니다", True), ("아니", False), ("아니요", False), ("아니야", False), ("아닙니다", False), ("2번", None), ("글쎄", None)],
)
def test_confirmation_parser_uses_exact_aliases(text: str, expected: bool | None) -> None:
    assert parse_confirmation_answer(text) is expected


def test_full_roof_selection_confirmation_flow_never_creates_job(session: Session, now: datetime) -> None:
    interpreter = FakeInterpreter(create_command())
    svc = conversation(session, now, interpreter)
    jobs_before = session.scalar(select(func.count()).select_from(ProductionJob))

    first = asyncio.run(svc.handle_text(session_id="flow-a", text="A형 한 채 만들어줘"))
    assert first.pending is not None and first.pending.state is PendingProductionState.WAITING_ROOF_OPTION
    assert first.message == "지붕을 선택해주세요. 1번 평지붕, 2번 경사지붕입니다."
    assert interpreter.calls == 1

    second = asyncio.run(svc.handle_text(session_id="flow-a", text="2번"))
    assert second.pending is not None
    assert second.pending.state is PendingProductionState.AWAITING_CONFIRMATION
    assert second.pending.roof_option_code is RoofOptionCode.ROOF_02
    assert second.message == "A형 주택 한 채를 2번 경사지붕으로 제작하는 것이 맞습니까?"
    assert interpreter.calls == 1

    third = asyncio.run(svc.handle_text(session_id="flow-a", text="네"))
    assert third.pending is not None and third.pending.state is PendingProductionState.CONFIRMED
    assert third.pending.confirmed_at == now
    assert third.message == "생산 요청이 확인되었습니다."
    assert interpreter.calls == 1
    assert session.scalar(select(func.count()).select_from(ProductionJob)) == jobs_before == 0


def test_roof_included_command_goes_directly_to_confirmation(session: Session, now: datetime) -> None:
    interpreter = FakeInterpreter(create_command(product_code="HOUSE_B", quantity=2, roof=RoofOptionCode.ROOF_01))
    svc = conversation(session, now, interpreter)
    first = asyncio.run(svc.handle_text(session_id="flow-b", text="B형 두 채 평지붕으로 만들어줘"))
    assert first.pending is not None and first.pending.state is PendingProductionState.AWAITING_CONFIRMATION
    assert first.message == "B형 주택 두 채를 1번 평지붕으로 제작하는 것이 맞습니까?"

    confirmed = asyncio.run(svc.handle_text(session_id="flow-b", text="네"))
    assert confirmed.pending is not None and confirmed.pending.state is PendingProductionState.CONFIRMED
    assert interpreter.calls == 1


def test_rejection_is_not_cancel_job(session: Session, now: datetime) -> None:
    interpreter = FakeInterpreter(create_command(roof=RoofOptionCode.ROOF_02))
    svc = conversation(session, now, interpreter)
    asyncio.run(svc.handle_text(session_id="reject", text="A형 한 채 2번 지붕으로 만들어줘"))
    result = asyncio.run(svc.handle_text(session_id="reject", text="아니"))
    assert result.pending is not None and result.pending.state is PendingProductionState.REJECTED
    assert result.pending.rejected_at == now
    assert result.pending.confirmed_at is None
    assert result.message == "생산 요청을 진행하지 않겠습니다."
    assert result.command is None


def test_invalid_state_specific_inputs_keep_active_state_without_llm(session: Session, now: datetime) -> None:
    interpreter = FakeInterpreter(create_command())
    svc = conversation(session, now, interpreter)
    waiting = asyncio.run(svc.handle_text(session_id="invalid", text="A형 한 채 만들어줘"))
    assert waiting.pending is not None
    invalid_roof = asyncio.run(svc.handle_text(session_id="invalid", text="3번"))
    assert invalid_roof.pending is not None and invalid_roof.pending.state is PendingProductionState.WAITING_ROOF_OPTION
    assert invalid_roof.message == "지붕을 선택해주세요. 1번 평지붕, 2번 경사지붕입니다."

    asyncio.run(svc.handle_text(session_id="invalid", text="1번"))
    invalid_confirmation = asyncio.run(svc.handle_text(session_id="invalid", text="2번"))
    assert invalid_confirmation.pending is not None
    assert invalid_confirmation.pending.state is PendingProductionState.AWAITING_CONFIRMATION
    assert "제작하는 것이 맞습니까?" in invalid_confirmation.message
    assert interpreter.calls == 1


def test_no_pending_bare_number_uses_stateless_interpreter_without_pending(session: Session, now: datetime) -> None:
    unknown = StructuredCommand(intent=Intent.UNKNOWN, clarification_needed=True, clarification_message="지원하는 생산 시스템 명령으로 다시 말씀해 주세요.")
    interpreter = FakeInterpreter(unknown)
    result = asyncio.run(conversation(session, now, interpreter).handle_text(session_id="none", text="2번"))
    assert result.pending is None
    assert result.command is unknown
    assert interpreter.calls == 1
    assert session.scalar(select(PendingProductionRequest.request_id)) is None


def test_sessions_are_isolated_and_terminal_request_allows_new_request(session: Session, now: datetime) -> None:
    a = FakeInterpreter(create_command())
    b = FakeInterpreter(create_command(product_code="HOUSE_B", roof=RoofOptionCode.ROOF_02))
    svc_a = conversation(session, now, a)
    svc_b = conversation(session, now, b)
    asyncio.run(svc_a.handle_text(session_id="session-a", text="A형 한 채 만들어줘"))
    asyncio.run(svc_b.handle_text(session_id="session-b", text="B형 한 채 경사지붕으로 만들어줘"))
    asyncio.run(svc_a.handle_text(session_id="session-a", text="2번"))

    pending_a = PendingProductionRequestService(session, clock=lambda: now).get_active_by_session("session-a")
    pending_b = PendingProductionRequestService(session, clock=lambda: now).get_active_by_session("session-b")
    assert pending_a is not None and pending_a.roof_option_code is RoofOptionCode.ROOF_02
    assert pending_b is not None and pending_b.roof_option_code is RoofOptionCode.ROOF_02

    asyncio.run(svc_a.handle_text(session_id="session-a", text="아니"))
    next_request = asyncio.run(svc_a.handle_text(session_id="session-a", text="A형 한 채 만들어줘"))
    assert next_request.pending is not None and next_request.pending.state is PendingProductionState.WAITING_ROOF_OPTION


def test_expired_pending_does_not_consume_bare_roof_followup(session: Session, now: datetime) -> None:
    current = {"value": now}
    interpreter = FakeInterpreter(create_command())
    svc = ProductionConversationService(
        pending_service=PendingProductionRequestService(session, ttl_seconds=1, clock=lambda: current["value"]),
        interpreter=interpreter,
    )
    first = asyncio.run(svc.handle_text(session_id="expired", text="A형 한 채 만들어줘"))
    assert first.pending is not None
    current["value"] = now + timedelta(seconds=2)
    interpreter.command = StructuredCommand(intent=Intent.UNKNOWN, clarification_needed=True, clarification_message="지원하는 생산 시스템 명령으로 다시 말씀해 주세요.")
    second = asyncio.run(svc.handle_text(session_id="expired", text="2번"))
    assert PendingProductionRequestService(session, clock=lambda: current["value"]).get_by_id(first.pending.request_id).state is PendingProductionState.EXPIRED
    assert second.pending is None
    assert interpreter.calls == 2
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightResult

def test_c1_enough_inventory_proceeds_to_confirm(session: Session, now: datetime) -> None:
    class FakePreflight:
        def validate(self, **kwargs):
            return ProductionInventoryPreflightResult(can_produce=True, product_code="HOUSE_A", requested_quantity=1, shortages=[])

    interpreter = FakeInterpreter(create_command(roof=RoofOptionCode.ROOF_01))
    svc = ProductionConversationService(
        pending_service=PendingProductionRequestService(session, ttl_seconds=300, clock=lambda: now),
        interpreter=interpreter,
        preflight_service=FakePreflight()
    )
    result = asyncio.run(svc.handle_text(session_id="c1", text="A형 한 채 1번 지붕으로 만들어줘"))
    assert result.pending is not None
    assert result.pending.state is PendingProductionState.AWAITING_CONFIRMATION


def test_c2_c3_shortage_rejects_immediately(session: Session, now: datetime) -> None:
    from shared.services.production_inventory_preflight_service import PreflightShortage, ProductionInventoryPreflightResult

    class FakePreflight:
        def validate(self, **kwargs):
            return ProductionInventoryPreflightResult(
                can_produce=False,
                product_code="HOUSE_A",
                requested_quantity=1,
                shortages=[
                    PreflightShortage(part_code="PART_A", part_name="외벽", required_quantity=2, available_quantity=0, shortage_quantity=2)
                ]
            )

    interpreter = FakeInterpreter(create_command(roof=RoofOptionCode.ROOF_01))
    svc = ProductionConversationService(
        pending_service=PendingProductionRequestService(session, ttl_seconds=300, clock=lambda: now),
        interpreter=interpreter,
        preflight_service=FakePreflight()
    )
    result = asyncio.run(svc.handle_text(session_id="c2", text="A형 한 채 1번 지붕으로 만들어줘"))

    assert result.pending is not None
    assert result.pending.state is PendingProductionState.REJECTED
    assert "외벽 2개" in result.message
    assert "부족하여 생산할 수 없습니다" in result.message


def test_missing_product_create_is_durable_and_followup_preserves_quantity(session: Session, now: datetime) -> None:
    incomplete = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST, quantity=1, clarification_needed=True,
        clarification_message="생산할 초소형 주택 모델을 말씀해 주세요.",
    )
    interpreter = FakeInterpreter(incomplete)
    svc = conversation(session, now, interpreter)

    first = asyncio.run(svc.handle_text(session_id="collect-a", text="집 하나 만들어줘"))
    assert first.pending is not None
    assert first.pending.state is PendingProductionState.COLLECTING_DETAILS
    assert first.pending.product_code is None and first.pending.quantity == 1
    request_id = first.pending.request_id
    assert first.message == "생산할 초소형 주택 모델을 말씀해 주세요."

    # Reconstructed service proves the same persisted draft is continuation authority.
    restarted = conversation(session, now, FakeInterpreter(StructuredCommand(intent=Intent.UNKNOWN, clarification_needed=True, clarification_message="unused")))
    product = asyncio.run(restarted.handle_text(session_id="collect-a", text="B형"))
    assert product.pending is not None and product.pending.request_id == request_id
    assert product.pending.product_code == "HOUSE_B" and product.pending.quantity == 1
    assert product.pending.state is PendingProductionState.WAITING_ROOF_OPTION
    assert "지붕을 선택" in product.message

    roof = asyncio.run(restarted.handle_text(session_id="collect-a", text="경사지붕"))
    assert roof.pending is not None and roof.pending.state is PendingProductionState.AWAITING_CONFIRMATION
    assert roof.pending.quantity == 1
    confirmed = asyncio.run(restarted.handle_text(session_id="collect-a", text="네"))
    assert confirmed.pending is not None and confirmed.pending.state is PendingProductionState.CONFIRMED
    assert interpreter.calls == 1


def test_collecting_details_is_session_isolated_and_invalid_product_keeps_draft(session: Session, now: datetime) -> None:
    incomplete = StructuredCommand(intent=Intent.CREATE_PRODUCTION_REQUEST, quantity=1, clarification_needed=True, clarification_message="생산할 초소형 주택 모델을 말씀해 주세요.")
    svc_a = conversation(session, now, FakeInterpreter(incomplete))
    first = asyncio.run(svc_a.handle_text(session_id="collect-a", text="집 하나 만들어줘"))
    svc_b = conversation(session, now, FakeInterpreter(StructuredCommand(intent=Intent.UNKNOWN, clarification_needed=True, clarification_message="지원하는 생산 시스템 명령으로 다시 말씀해 주세요.")))
    other = asyncio.run(svc_b.handle_text(session_id="collect-b", text="B형"))
    assert other.pending is None
    invalid = asyncio.run(svc_a.handle_text(session_id="collect-a", text="없는 모델"))
    assert invalid.pending is not None and invalid.pending.request_id == first.pending.request_id
    assert invalid.pending.state is PendingProductionState.COLLECTING_DETAILS
