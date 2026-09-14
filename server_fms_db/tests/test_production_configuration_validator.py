from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
import pytest

from shared.models import Base
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    EventType,
    Inventory,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStep,
    Part,
    PartCategory,
    Product,
    ProductionEvent,
    ProductionJob,
)
from shared.services.production_configuration_validator import (
    ProductionConfigurationInvalidError,
)
from shared.services.production_inventory_preflight_service import (
    ProductionInventoryPreflightService,
)
from shared.services.production_orchestration_service import ProductionOrchestrationService


def _session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.info["test_engine"] = engine
    return session


def _close(session: Session) -> None:
    engine = session.info["test_engine"]
    session.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _recipe(session: Session, *, part_code: str | None = None, quantity: int | None = None) -> Product:
    product = Product(product_code="CONFIG_TEST", product_name="Configuration Test")
    session.add(product)
    if part_code is not None:
        session.add(
            Part(
                part_code=part_code,
                part_name="Configuration material",
                category=PartCategory.STRUCTURE,
                unit="EA",
            )
        )
    session.flush()
    recipe = AssemblyRecipe(product_code=product.product_code, version=1, is_active=True)
    session.add(recipe)
    session.flush()
    session.add(
        AssemblyRecipeStage(
            recipe_id=recipe.recipe_id,
            stage_order=1,
            operation_code="INSTALL_TEST",
            display_name="Install test",
            part_code=part_code,
            quantity=quantity,
        )
    )
    session.commit()
    return product


def test_zero_material_requirements_are_configuration_invalid_not_shortage() -> None:
    session = _session()
    try:
        product = _recipe(session)
        with pytest.raises(ProductionConfigurationInvalidError) as error:
            ProductionInventoryPreflightService(session).validate(
                product_code=product.product_code, quantity=1
            )
        assert [issue.code for issue in error.value.issues] == ["ZERO_MATERIAL_REQUIREMENTS"]
        assert not hasattr(error.value, "shortages")
    finally:
        _close(session)


def test_invalid_configuration_blocks_direct_job_creation_without_rows() -> None:
    session = _session()
    try:
        product = _recipe(session)
        with pytest.raises(ProductionConfigurationInvalidError):
            ProductionOrchestrationService(session).create_job(
                product_code=product.product_code, job_code="CONFIG-EMPTY-001"
            )
        for model in (
            ProductionJob,
            JobStep,
            ProductionEvent,
            JobMaterialDelivery,
            JobMaterialDeliveryItem,
            JobMaterialFeedExecution,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    finally:
        _close(session)


def test_valid_configuration_preserves_normal_preflight_success_and_shortage() -> None:
    session = _session()
    try:
        product = _recipe(session, part_code="CONFIG_PART", quantity=2)
        session.add(Inventory(part_code="CONFIG_PART", quantity=2))
        session.commit()
        success = ProductionInventoryPreflightService(session).validate(
            product_code=product.product_code, quantity=1
        )
        assert success.can_produce is True
        assert success.shortages == []

        session.get(Inventory, "CONFIG_PART").quantity = 1
        session.commit()
        shortage = ProductionInventoryPreflightService(session).validate(
            product_code=product.product_code, quantity=1
        )
        assert shortage.can_produce is False
        assert shortage.shortages[0].part_code == "CONFIG_PART"
    finally:
        _close(session)


def test_partial_material_binding_is_configuration_invalid() -> None:
    session = _session()
    try:
        product = _recipe(session, part_code="CONFIG_PART", quantity=None)
        with pytest.raises(ProductionConfigurationInvalidError) as error:
            ProductionInventoryPreflightService(session).validate(
                product_code=product.product_code, quantity=1
            )
        assert [issue.code for issue in error.value.issues] == ["MISSING_MATERIAL_QUANTITY", "ZERO_MATERIAL_REQUIREMENTS"]
    finally:
        _close(session)
