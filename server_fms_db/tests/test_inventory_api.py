from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.inventory import get_db
from shared.models import Base
from shared.models.factory import Part, PartCategory
from shared.services.inventory_service import InventoryService


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


@pytest.fixture(name="client")
def fixture_client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


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


def test_get_all_inventory(client: TestClient, db_session: Session) -> None:
    part1 = create_test_part(db_session, "PART_1")
    part2 = create_test_part(db_session, "PART_2")

    service = InventoryService(db_session)
    service.stock_in(part_code=part1.part_code, quantity=10, reason="test")
    service.stock_in(part_code=part2.part_code, quantity=5, reason="test")

    response = client.get("/inventory")
    assert response.status_code == 200
    data = response.json()

    assert len(data) == 2
    assert data[0]["part_code"] == "PART_1"
    assert data[0]["quantity"] == 10
    assert "updated_at" in data[0]

    assert data[1]["part_code"] == "PART_2"
    assert data[1]["quantity"] == 5


def test_get_inventory_by_part_code(client: TestClient, db_session: Session) -> None:
    part = create_test_part(db_session, "ROOF")
    service = InventoryService(db_session)
    service.stock_in(part_code=part.part_code, quantity=42, reason="init")

    response = client.get("/inventory/ROOF")
    assert response.status_code == 200
    data = response.json()

    assert data["part_code"] == "ROOF"
    assert data["quantity"] == 42
    assert data["updated_at"] is not None


def test_get_inventory_missing_part(client: TestClient) -> None:
    response = client.get("/inventory/NONEXISTENT")
    assert response.status_code == 404
    assert "Part not found" in response.json()["detail"]


def test_get_inventory_no_stock_yet(client: TestClient, db_session: Session) -> None:
    create_test_part(db_session, "WALL")

    response = client.get("/inventory/WALL")
    assert response.status_code == 200
    data = response.json()

    assert data["part_code"] == "WALL"
    assert data["quantity"] == 0
    assert data["updated_at"] is None
