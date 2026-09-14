from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.ai import get_interpreter, get_stt_service
from api_server.routers.inventory import get_db, get_inventory_service
from shared.enums.ai import Intent
from shared.models import Base
from shared.models.factory import Part, PartCategory
from shared.schemas.ai import StructuredCommand, TranscriptionResponse
from shared.services.inventory_service import InventoryService


class FakeInterpreter:
    def __init__(self, command: StructuredCommand) -> None:
        self.command = command

    async def interpret(self, text: str):
        return text.strip(), self.command, "{}"


class FakeSTT:
    def __init__(self, result: TranscriptionResponse) -> None:
        self.result = result

    async def transcribe_upload(self, _audio):
        return self.result


def create_test_part(session: Session, code: str) -> Part:
    part = Part(
        vision_class="wall_ext_back",
        part_code=code,
        part_name=f"{code} name",
        category=PartCategory.STRUCTURE,
        unit="EA",
    )
    session.add(part)
    session.commit()
    return part


@pytest.fixture(name="db_session")
def fixture_db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="inventory_service")
def fixture_inventory_service(db_session: Session) -> InventoryService:
    return InventoryService(db_session)


def get_client(db_session: Session, interpreter, stt=None) -> TestClient:
    app.dependency_overrides[get_interpreter] = lambda: interpreter
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(db_session)
    if stt:
        app.dependency_overrides[get_stt_service] = lambda: stt

    return TestClient(app)


def test_interpret_query_inventory_item(db_session: Session) -> None:
    part = create_test_part(db_session, "ROOF")
    InventoryService(db_session).stock_in(part_code=part.part_code, quantity=7, reason="test")

    cmd = StructuredCommand(
        intent=Intent.QUERY_INVENTORY,
        inventory_scope="ITEM",
        item_name="ROOF"
    )

    client = get_client(db_session, FakeInterpreter(cmd))
    response = client.post("/ai/interpret", json={"text": "ROOF 재고 몇 개 남았어?"})

    assert response.status_code == 200
    data = response.json()
    assert data["command"]["intent"] == "QUERY_INVENTORY"

    inv_result = data.get("inventory_result")
    assert inv_result is not None
    assert len(inv_result) == 1
    assert inv_result[0]["part_code"] == "ROOF"
    assert inv_result[0]["quantity"] == 7
    assert inv_result[0]["updated_at"] is not None


def test_interpret_query_inventory_no_stock_yet(db_session: Session) -> None:
    # Part exists, but no stock_in
    create_test_part(db_session, "FLOOR")

    cmd = StructuredCommand(
        intent=Intent.QUERY_INVENTORY,
        inventory_scope="ITEM",
        item_name="FLOOR"
    )

    client = get_client(db_session, FakeInterpreter(cmd))
    response = client.post("/ai/interpret", json={"text": "FLOOR 재고 몇 개 남았어?"})

    assert response.status_code == 200
    data = response.json()
    inv_result = data["inventory_result"]
    assert len(inv_result) == 1
    assert inv_result[0]["part_code"] == "FLOOR"
    assert inv_result[0]["quantity"] == 0
    assert inv_result[0]["updated_at"] is None


def test_interpret_query_inventory_unknown_part(db_session: Session) -> None:
    # Part does not exist
    cmd = StructuredCommand(
        intent=Intent.QUERY_INVENTORY,
        inventory_scope="ITEM",
        item_name="UNKNOWN_PART"
    )

    client = get_client(db_session, FakeInterpreter(cmd))
    response = client.post("/ai/interpret", json={"text": "UNKNOWN_PART 재고 몇 개 남았어?"})

    assert response.status_code == 200
    data = response.json()
    assert data["inventory_result"] == []  # empty list as per our fallback


def test_voice_command_query_inventory_all_returns_all_items(db_session: Session) -> None:
    first = create_test_part(db_session, "WALL")
    second = create_test_part(db_session, "ROOF")
    service = InventoryService(db_session)
    service.stock_in(part_code=first.part_code, quantity=15, reason="test")
    service.stock_in(part_code=second.part_code, quantity=7, reason="test")
    cmd = StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ALL")
    stt = FakeSTT(TranscriptionResponse(text="전체 자재 재고 확인해줘", language="ko", processing_time_ms=0))

    client = get_client(db_session, FakeInterpreter(cmd), stt)
    response = client.post("/ai/voice-command", files={"audio": ("a.wav", b"x", "audio/wav")})

    assert response.status_code == 200
    data = response.json()
    assert data["command"]["inventory_scope"] == "ALL"
    assert data["command"]["item_name"] is None
    assert {item["part_code"] for item in data["inventory_result"]} == {"WALL", "ROOF"}


def test_voice_command_query_inventory_item(db_session: Session) -> None:
    part = create_test_part(db_session, "WALL")
    InventoryService(db_session).stock_in(part_code=part.part_code, quantity=15, reason="test")

    cmd = StructuredCommand(
        intent=Intent.QUERY_INVENTORY,
        inventory_scope="ITEM",
        item_name="WALL"
    )
    stt = FakeSTT(TranscriptionResponse(text="WALL 재고 몇 개야?", language="ko", processing_time_ms=0))

    client = get_client(db_session, FakeInterpreter(cmd), stt)
    response = client.post("/ai/voice-command", files={"audio": ("a.wav", b"x", "audio/wav")})

    assert response.status_code == 200
    data = response.json()
    inv_result = data["inventory_result"]
    assert len(inv_result) == 1
    assert inv_result[0]["part_code"] == "WALL"
    assert inv_result[0]["quantity"] == 15


def test_other_intent_does_not_fetch_inventory(db_session: Session) -> None:
    cmd = StructuredCommand(
        intent=Intent.QUERY_JOB_STATUS,
        target_job_id="123"
    )

    client = get_client(db_session, FakeInterpreter(cmd))
    response = client.post("/ai/interpret", json={"text": "진행 상태 어때?"})

    assert response.status_code == 200
    data = response.json()
    assert data["command"]["intent"] == "QUERY_JOB_STATUS"
    assert data.get("inventory_result") is None
