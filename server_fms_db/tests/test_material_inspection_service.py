from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from shared.models.factory import (
    Base,
    MaterialInspection,
    MaterialInspectionStatus,
    MaterialInspectionResult,
    JobMaterialDeliveryItem,
    Part,
)
from shared.schemas.vision import IncomingMaterialQAResult, QAWireDetection
from shared.services.material_inspection_service import (
    MaterialInspectionService,
    MaterialInspectionError,
)

@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine

@pytest.fixture
def db_session(engine) -> Iterator[Session]:
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()

@pytest.fixture
def service():
    return MaterialInspectionService()

def test_request_inspection_and_apply_result(db_session: Session, service: MaterialInspectionService):
    part = Part(part_code="PART-1", part_name="Test Part", category="STRUCTURE", vision_class="wall_ext_back", unit="EA")
    db_session.add(part)

    # Fake delivery item
    item = JobMaterialDeliveryItem(
        job_delivery_id=1,
        job_step_id=1,
        part_code="PART-1",
        quantity=2,
        is_delivered=False
    )
    db_session.add(item)
    db_session.commit()

    # Create cycle
    req = service.request_inspection(db_session, item.delivery_item_id)
    assert req.inspection_cycle == 1
    assert req.expected_class_name == "wall_ext_back"
    assert req.expected_quantity == 2

    # Verify hold
    assert service.is_delivery_item_released(db_session, item.delivery_item_id) is False

    # Apply PASS
    res = IncomingMaterialQAResult(
        ver="0.1",
        inspection_request_id=req.inspection_request_id,
        delivery_item_id=item.delivery_item_id,
        inspection_cycle=1,
        result="PASS",
        expected_part_code="PART-1",
        expected_class_name="wall_ext_back",
        expected_quantity=2,
        detected_quantity=2,
        detections=[],
        frame_width=640,
        frame_height=480,
        camera_source="GLOBAL_CAMERA",
        frame_seq=1,
        timestamp=datetime.now(timezone.utc),
        model_scope="V1",
        model_version="1",
        production_valid=True
    )

    inspection = service.apply_inspection_result(db_session, res)
    assert inspection.status == "COMPLETED"
    assert inspection.result == "PASS"

    # Verify release
    assert service.is_delivery_item_released(db_session, item.delivery_item_id) is True

    # Re-inspection creates cycle 2
    req2 = service.request_inspection(db_session, item.delivery_item_id)
    assert req2.inspection_cycle == 2

    # Old PASS + Latest REQUESTED = HOLD
    assert service.is_delivery_item_released(db_session, item.delivery_item_id) is False

    # Apply FAIL
    res.inspection_request_id = req2.inspection_request_id
    res.inspection_cycle = 2
    res.result = "FAIL"
    res.failure_type = "WRONG_CLASS"

    service.apply_inspection_result(db_session, res)
    assert service.is_delivery_item_released(db_session, item.delivery_item_id) is False
