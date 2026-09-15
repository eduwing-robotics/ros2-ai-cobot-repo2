from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.ai import get_conversation_service, get_interpreter
from api_server.routers.inventory import get_db, get_inventory_service
from api_server.services.command_interpreter import CommandInterpreter
from api_server.services.inventory_voice_query import InventoryVoiceQueryService
from api_server.services.production_conversation_service import ProductionConversationService
from api_server.services.response_message_builder import ResponseMessageBuilder
from shared.enums.ai import Intent
from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    Inventory,
    Part,
    PartCategory,
    Product,
)
from shared.schemas.ai import StructuredCommand
from shared.services.inventory_service import InventoryService


class FakeInterpreter:
    def __init__(self, command: StructuredCommand) -> None:
        self.command = command

    async def interpret(self, text: str):
        return text, self.command, ""


class UnexpectedLLM:
    async def chat(self, _text: str):  # pragma: no cover - deterministic route must not call it
        raise AssertionError("inventory deterministic parser unexpectedly called LLM")


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        _seed_multi_product_master(db)
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_multi_product_master(session: Session) -> None:
    products = (
        ("MODEL_ALPHA", "A형 주택"),
        ("MODEL_BRAVO", "B형 주택"),
        ("MODEL_CHARLIE", "C형 주택"),
    )
    for code, name in products:
        session.add(Product(product_code=code, product_name=name))
    session.flush()

    parts = (
        ("A_BASE", "밑판", 8, 0),
        ("A_OUTER", "A형 외벽", 4, 0),
        ("A_INNER_EMPTY", "내벽", None, None),
        ("B_BASE", "밑판", 10, 2),
        ("B_INNER", "내벽", 9, 0),
        ("B_OUTER_REAR", "후면 창문 결합 외벽", 10, 0),
        ("B_OUTER_DOOR", "출입문 결합 외벽", 10, 0),
        ("B_OUTER_LEFT", "좌측 창문 결합 외벽", 10, 0),
        ("B_OUTER_RIGHT", "우측 외벽", 10, 0),
        ("B_ROOF", "경사지붕", 10, 0),
        ("SHARED_PANEL", "공용 패널", 6, 1),
    )
    for code, name, quantity, reserved in parts:
        session.add(Part(part_code=code, part_name=name, category=PartCategory.STRUCTURE, unit="EA"))
    session.flush()
    for code, _name, quantity, reserved in parts:
        if quantity is not None:
            session.add(Inventory(part_code=code, quantity=quantity, reserved_quantity=reserved))

    stages = {
        "MODEL_ALPHA": (("INSTALL_BASE", "A_BASE"), ("INSTALL_OUTER_WALL", "A_OUTER"), ("INSTALL_INNER_WALL", "A_INNER_EMPTY"), ("INSTALL_SHARED", "SHARED_PANEL")),
        "MODEL_BRAVO": (("INSTALL_BASE", "B_BASE"), ("INSTALL_INNER_WALL", "B_INNER"), ("INSTALL_REAR_OUTER_WALL", "B_OUTER_REAR"), ("INSTALL_DOOR_OUTER_WALL", "B_OUTER_DOOR"), ("INSTALL_LEFT_OUTER_WALL", "B_OUTER_LEFT"), ("INSTALL_RIGHT_OUTER_WALL", "B_OUTER_RIGHT"), ("INSTALL_ROOF", "B_ROOF", "ROOF_02")),
        "MODEL_CHARLIE": (("INSTALL_SHARED", "SHARED_PANEL"),),
    }
    for product_code, recipe_stages in stages.items():
        recipe = AssemblyRecipe(product_code=product_code, version=1, is_active=True)
        session.add(recipe)
        session.flush()
        session.add_all(
            AssemblyRecipeStage(
                recipe_id=recipe.recipe_id,
                stage_order=index,
                operation_code=operation,
                display_name=operation,
                part_code=part_code,
                quantity=1,
                is_terminal=index == len(recipe_stages),
                option_code=option_code,
            )
            for index, stage_data in enumerate(recipe_stages, start=1)
            for operation, part_code, option_code in [(stage_data[0], stage_data[1], stage_data[2] if len(stage_data) > 2 else None)]
        )
    session.commit()


def _query(session: Session, *, item_name: str | None = None, product_code: str | None = None) -> list:
    command = StructuredCommand(
        intent=Intent.QUERY_INVENTORY,
        inventory_scope="ITEM" if item_name else "ALL",
        item_name=item_name,
        product_code=product_code,
    )
    return InventoryVoiceQueryService(InventoryService(session)).fetch(command) or []


def test_all_query_projects_every_active_product_context(session: Session) -> None:
    rows = _query(session)
    contexts = {context.product_code for row in rows for context in row.products}
    assert contexts == {"MODEL_ALPHA", "MODEL_BRAVO", "MODEL_CHARLIE"}


def test_duplicate_human_name_returns_all_active_recipe_candidates(session: Session) -> None:
    rows = _query(session, item_name="밑판")
    assert [(row.part_code, row.available_quantity) for row in rows] == [("A_BASE", 8), ("B_BASE", 8)]
    assert [row.products[0].product_code for row in rows] == ["MODEL_ALPHA", "MODEL_BRAVO"]


def test_product_scoped_item_uses_master_context_not_part_code_parsing(session: Session) -> None:
    rows = _query(session, item_name="밑판", product_code="MODEL_BRAVO")
    assert [row.part_code for row in rows] == ["B_BASE"]
    assert rows[0].products[0].product_name == "B형 주택"

    # The deterministic parser leaves the compact product term in item_name;
    # the Voice resolver derives its scope from Product master name data.
    command = asyncio.run(CommandInterpreter(UnexpectedLLM()).interpret("B형 밑판 재고 알려줘"))[1]
    assert command.intent is Intent.QUERY_INVENTORY and command.item_name == "B형 밑판"
    assert [row.part_code for row in InventoryVoiceQueryService(InventoryService(session)).fetch(command)] == ["B_BASE"]


def test_outer_wall_and_roof_family_terms_return_every_match(session: Session) -> None:
    outer = _query(session, item_name="외벽")
    assert {row.part_code for row in outer} == {
        "A_OUTER", "B_OUTER_REAR", "B_OUTER_DOOR", "B_OUTER_LEFT", "B_OUTER_RIGHT"
    }
    bravo_outer = _query(session, item_name="외벽", product_code="MODEL_BRAVO")
    assert {row.part_code for row in bravo_outer} == {
        "B_OUTER_REAR", "B_OUTER_DOOR", "B_OUTER_LEFT", "B_OUTER_RIGHT"
    }
    assert [row.part_code for row in _query(session, item_name="지붕")] == ["B_ROOF"]


def test_exact_code_unknown_and_known_zero_stock_semantics(session: Session) -> None:
    assert [row.part_code for row in _query(session, item_name="B_BASE")] == ["B_BASE"]
    assert _query(session, item_name="없는 자재") == []
    empty = _query(session, item_name="내벽", product_code="MODEL_ALPHA")
    assert len(empty) == 1 and empty[0].part_code == "A_INNER_EMPTY"
    assert (empty[0].quantity, empty[0].reserved_quantity, empty[0].available_quantity) == (0, 0, 0)


def test_shared_part_is_one_physical_row_with_multiple_product_contexts(session: Session) -> None:
    rows = _query(session, item_name="공용 패널")
    assert len(rows) == 1
    row = rows[0]
    assert (row.quantity, row.reserved_quantity, row.available_quantity) == (6, 1, 5)
    assert {context.product_code for context in row.products} == {"MODEL_ALPHA", "MODEL_CHARLIE"}


def test_narration_uses_available_and_reservation_not_physical_only(session: Session) -> None:
    builder = ResponseMessageBuilder()
    reserved = _query(session, item_name="밑판", product_code="MODEL_BRAVO")
    message = builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="밑판", product_code="MODEL_BRAVO"),
        reserved,
    )
    assert "총 10개 중 2개가 예약" in message and "현재 8개 사용 가능" in message
    no_reservation = _query(session, item_name="밑판", product_code="MODEL_ALPHA")
    assert "현재 8개 사용 가능" in builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="밑판"), no_reservation
    )
    zero = _query(session, item_name="내벽", product_code="MODEL_ALPHA")
    assert "사용 가능한 재고가 없습니다" in builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="내벽"), zero
    )

def test_option_material_narration_omits_product_prefix_but_keeps_structured_context(session: Session) -> None:
    builder = ResponseMessageBuilder()
    rows = _query(session, item_name="지붕")
    assert [row.part_code for row in rows] == ["B_ROOF"]
    assert rows[0].products[0].product_name == "B형 주택"
    assert rows[0].model_dump()["products"][0]["product_name"] == "B형 주택"
    message = builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="지붕"), rows
    )
    assert message == "경사지붕은 현재 10개 사용 가능합니다."
    assert "B형 주택" not in message

    scoped = _query(session, item_name="지붕", product_code="MODEL_BRAVO")
    assert [row.part_code for row in scoped] == ["B_ROOF"]
    scoped_message = builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="B형 지붕", product_code="MODEL_BRAVO"), scoped
    )
    assert "B형 주택" not in scoped_message
    assert "경사지붕" in scoped_message

    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "MODEL_BRAVO"))
    assert recipe is not None
    session.add(Part(part_code="B_OPTION", part_name="차양 지붕", category=PartCategory.STRUCTURE, unit="EA"))
    session.flush()
    session.add(Inventory(part_code="B_OPTION", quantity=4, reserved_quantity=0))
    session.add(AssemblyRecipeStage(
        recipe_id=recipe.recipe_id, stage_order=8, operation_code="INSTALL_OPTION", display_name="차양 선택", part_code="B_OPTION",
        quantity=1, option_code="AWNING_OPTION", is_terminal=True,
    ))
    session.commit()
    scoped_rows = _query(session, item_name="지붕", product_code="MODEL_BRAVO")
    grouped_message = builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="B형 지붕", product_code="MODEL_BRAVO"), scoped_rows
    )
    assert grouped_message.startswith("지붕 재고는")
    assert "B형" not in grouped_message


def test_arbitrary_option_stage_is_generic_without_product_prefix(session: Session) -> None:
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "MODEL_CHARLIE"))
    assert recipe is not None
    session.add(Part(part_code="C_OPTION", part_name="태양광 패널", category=PartCategory.STRUCTURE, unit="EA"))
    session.flush()
    session.add(Inventory(part_code="C_OPTION", quantity=7, reserved_quantity=0))
    session.add(AssemblyRecipeStage(
        recipe_id=recipe.recipe_id, stage_order=2, operation_code="INSTALL_OPTION", display_name="태양광 선택", part_code="C_OPTION",
        quantity=1, option_code="SOLAR_OPTION", is_terminal=True,
    ))
    session.commit()
    rows = _query(session, item_name="태양광")
    assert [row.part_code for row in rows] == ["C_OPTION"]
    assert rows[0].is_option_material is True
    message = ResponseMessageBuilder().build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="태양광"), rows
    )
    assert message == "태양광 패널은 현재 7개 사용 가능합니다."
    assert "C형 주택" not in message



def test_narration_groups_multiple_parts_for_one_product_once(session: Session) -> None:
    builder = ResponseMessageBuilder()
    rows = _query(session, item_name="외벽", product_code="MODEL_BRAVO")
    message = builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="외벽", product_code="MODEL_BRAVO"),
        rows,
    )
    assert message.count("B형 주택") == 1
    assert "B형 주택 외벽 재고는" in message
    for name in ("후면 창문 결합 외벽", "출입문 결합 외벽", "좌측 창문 결합 외벽", "우측 외벽"):
        assert name in message


def test_narration_groups_each_product_and_preserves_reserved_and_zero_details(session: Session) -> None:
    builder = ResponseMessageBuilder()
    rear = session.get(Inventory, "B_OUTER_REAR")
    right = session.get(Inventory, "B_OUTER_RIGHT")
    assert rear is not None and right is not None
    rear.reserved_quantity = 2
    right.reserved_quantity = 10
    session.commit()

    rows = _query(session, item_name="외벽")
    structured_before = [row.model_dump() for row in rows]
    message = builder.build_inventory_narration(
        StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="외벽"), rows
    )
    assert message.count("A형 주택") == 1
    assert message.count("B형 주택") == 1
    assert "총 10개 중 2개가 예약되어 현재 8개" in message
    assert "우측 외벽은 현재 사용 가능한 재고가 없습니다" in message
    assert [row.model_dump() for row in rows] == structured_before

def test_interpret_and_conversation_use_same_master_scoped_projection(session: Session) -> None:
    command = StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope="ITEM", item_name="밑판")
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(session)
    app.dependency_overrides[get_interpreter] = lambda: FakeInterpreter(command)
    try:
        with TestClient(app) as client:
            interpret = client.post("/ai/interpret", json={"text": "밑판 재고 알려줘"})
            assert interpret.status_code == 200, interpret.text
            rows = interpret.json()["inventory_result"]
            assert {row["part_code"] for row in rows} == {"A_BASE", "B_BASE"}
            assert {row["products"][0]["product_name"] for row in rows} == {"A형 주택", "B형 주택"}

            conversation = client.post("/ai/conversation", json={"session_id": "multi-product", "text": "밑판 재고 알려줘"})
            assert conversation.status_code == 200, conversation.text
            message = conversation.json()["message"]
            assert "A형 주택" in message and "B형 주택" in message
            assert "8개 사용 가능" in message
    finally:
        app.dependency_overrides.clear()
