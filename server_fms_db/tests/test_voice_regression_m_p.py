import pytest
import asyncio
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import select

from shared.models import Base
from shared.models.factory import PendingProductionState, RoofOptionCode, ProductionJob, Inventory, Part, PendingProductionRequest, Product
from shared.schemas.ai import Intent, StructuredCommand
from api_server.services.command_interpreter import CommandInterpreter
from api_server.services.production_conversation_service import ProductionConversationService
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService, PreflightShortage, ProductionInventoryPreflightResult

@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add_all([
        Product(product_code="HOUSE_A", product_name="A형 주택"),
        Product(product_code="HOUSE_B", product_name="B형 주택"),
    ])
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


class FakeInterpreter(CommandInterpreter):
    def __init__(self):
        pass
    async def interpret(self, text: str):
        if "A형" in text or "A타입" in text:
            if "경사" in text or "2번" in text: roof = RoofOptionCode.ROOF_02
            elif "평" in text or "1번" in text: roof = RoofOptionCode.ROOF_01
            else: roof = None
            return text, StructuredCommand(intent=Intent.CREATE_PRODUCTION_REQUEST, product_name="A형 주택", product_code="HOUSE_A", quantity=1, roof_option_code=roof, requires_confirmation=True), "{}"
        if text in ["응", "네", "맞아", "확인", "진행해", "그렇게 해줘"]:
            return text, StructuredCommand(intent=Intent.UNKNOWN), "{}" # Actually confirmation parsing isn't LLM!
        if text in ["아니", "취소해", "안 할래", "하지 마"]:
            return text, StructuredCommand(intent=Intent.UNKNOWN), "{}" # Reject
        return text, StructuredCommand(intent=Intent.UNKNOWN), "{}"

class FakePreflightPass:
    def validate(self, **kwargs):
        return ProductionInventoryPreflightResult(can_produce=True, product_code=kwargs.get('product_code'), requested_quantity=kwargs.get('quantity'), shortages=[])

class FakePreflightFail:
    def validate(self, **kwargs):
        return ProductionInventoryPreflightResult(can_produce=False, product_code=kwargs.get('product_code'), requested_quantity=kwargs.get('quantity'),
            shortages=[PreflightShortage("PART_A", "외벽", 2, 0, 2), PreflightShortage("PART_B", "평지붕", 1, 0, 1)])

def test_m001(session: Session, now: datetime):
    # User: "A형 주택 하나 만들어줘" -> Roof -> "평지붕" -> Confirm -> "네" -> Job
    interpreter = FakeInterpreter()
    pend_svc = PendingProductionRequestService(session, clock=lambda: now)
    svc = ProductionConversationService(pending_service=pend_svc, interpreter=interpreter, preflight_service=FakePreflightPass())

    r1 = asyncio.run(svc.handle_text(session_id="M001", text="A형 주택 하나 만들어줘"))
    assert r1.pending.state == PendingProductionState.WAITING_ROOF_OPTION

    r2 = asyncio.run(svc.handle_text(session_id="M001", text="평지붕"))
    assert r2.pending.state == PendingProductionState.AWAITING_CONFIRMATION

    r3 = asyncio.run(svc.handle_text(session_id="M001", text="네"))
    # Confirm creates pending materialization? Wait, _handle_active_pending uses _materialization_service
    # Let's just check the string and the pending state.
    assert r3.pending.state == PendingProductionState.CONFIRMED

def test_m004(session: Session, now: datetime):
    interpreter = FakeInterpreter()
    pend_svc = PendingProductionRequestService(session, clock=lambda: now)
    svc = ProductionConversationService(pending_service=pend_svc, interpreter=interpreter, preflight_service=FakePreflightPass())

    asyncio.run(svc.handle_text(session_id="M004", text="A형 하나 만들어줘"))
    asyncio.run(svc.handle_text(session_id="M004", text="1번"))
    r3 = asyncio.run(svc.handle_text(session_id="M004", text="아니"))
    assert r3.pending.state == PendingProductionState.REJECTED

def test_m005(session: Session, now: datetime):
    interpreter = FakeInterpreter()
    pend_svc = PendingProductionRequestService(session, clock=lambda: now)
    svc = ProductionConversationService(pending_service=pend_svc, interpreter=interpreter, preflight_service=FakePreflightFail())

    r1 = asyncio.run(svc.handle_text(session_id="M005", text="A형 평지붕 하나 만들어줘"))
    # Preflight #1 will reject
    assert r1.pending.state == PendingProductionState.REJECTED
    assert "부족" in r1.message
