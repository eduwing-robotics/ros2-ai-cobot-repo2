from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models import Base
from shared.models.factory import Product
from shared.services.product_lookup_service import (
    InvalidProductCodeError,
    ProductLookupService,
    ProductNotFoundError,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, connection_record) -> None:  # noqa: ARG001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def service(session: Session) -> ProductLookupService:
    return ProductLookupService(session)


def create_product(session: Session, code: str) -> Product:
    product = Product(
        product_code=code,
        product_name=f"{code} name",
        description="A test product",
        is_active=True,
    )
    session.add(product)
    session.commit()
    return product


def test_get_by_code_success_house_a(session: Session, service: ProductLookupService) -> None:
    # Setup
    create_product(session, "HOUSE_A")

    # Execute
    product = service.get_by_code("HOUSE_A")

    # Assert
    assert product is not None
    assert product.product_code == "HOUSE_A"
    assert product.product_code is not None


def test_get_by_code_success_house_b(session: Session, service: ProductLookupService) -> None:
    # Setup
    create_product(session, "HOUSE_B")

    # Execute
    product = service.get_by_code("HOUSE_B")

    # Assert
    assert product is not None
    assert product.product_code == "HOUSE_B"
    assert product.product_code is not None


def test_get_by_code_unknown_product(service: ProductLookupService) -> None:
    with pytest.raises(ProductNotFoundError, match="Product not found for code"):
        service.get_by_code("UNKNOWN_PRODUCT")


def test_get_by_code_empty_string(service: ProductLookupService) -> None:
    with pytest.raises(InvalidProductCodeError, match="Invalid product code"):
        service.get_by_code("")


def test_get_by_code_whitespace_only(service: ProductLookupService) -> None:
    with pytest.raises(InvalidProductCodeError, match="Invalid product code"):
        service.get_by_code("   \t  ")


def test_get_by_code_none(service: ProductLookupService) -> None:
    with pytest.raises(InvalidProductCodeError, match="Invalid product code"):
        service.get_by_code(None)
