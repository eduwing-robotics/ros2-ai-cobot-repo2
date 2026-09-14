import pytest
from sqlalchemy.orm import Session

from shared.models.factory import (
    MaterialInspectionStatus,
    MaterialInspectionResult,
)
from shared.services.material_inspection_service import (
    MaterialInspectionService,
    MaterialInspectionError,
)
from fms_server.incoming_material_qa_client import (
    IncomingMaterialQAClientError,
    IncomingMaterialQAClientTimeoutError,
)
from fms_server.fake_incoming_material_qa_client import FakeIncomingMaterialQAClient

from sqlalchemy import create_engine
from shared.models.base import Base

@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

def _setup_delivery_item(session: Session) -> int:
    from shared.models.factory import JobMaterialDeliveryItem, Part, ProductionJob, JobStep, JobStatus, StepStatus

    # Needs a part with vision_class
    part = Part(part_code="TEST_PART_QA", part_name="Test QA Part", category="STRUCTURE", unit="EA", vision_class="test_qa_cls")
    if not session.get(Part, "TEST_PART_QA"):
        session.add(part)

    job = ProductionJob(product_code="TEST", job_code="J-QA", status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()

    step = JobStep(job_id=job.job_id, operation_code="ROOF_INSTALL", step_order=1, status=StepStatus.PENDING, display_name="Test Step")
    session.add(step)
    session.flush()

    item = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=step.job_step_id, quantity=5, part_code="TEST_PART_QA")
    session.add(item)
    session.flush()
    return item.delivery_item_id


def test_fake_qa_pass_lifecycle(session: Session):
    service = MaterialInspectionService()
    client = FakeIncomingMaterialQAClient()

    item_id = _setup_delivery_item(session)

    # 1. REQUESTED
    req = service.request_inspection(session, item_id)

    # 2. RUNNING
    service.mark_running(session, req.inspection_request_id)

    # 3. Client Call
    client.next_outcome = "PASS"
    result = client.request_inspection(req)

    # 4. COMPLETED
    inspection = service.apply_inspection_result(session, result)

    assert inspection.status == MaterialInspectionStatus.COMPLETED
    assert inspection.result == MaterialInspectionResult.PASS
    assert inspection.failure_type is None

    # 5. RELEASE check
    assert service.is_delivery_item_released(session, item_id) is True


def test_fake_qa_fail_lifecycle(session: Session):
    service = MaterialInspectionService()
    client = FakeIncomingMaterialQAClient()

    item_id = _setup_delivery_item(session)
    req = service.request_inspection(session, item_id)
    service.mark_running(session, req.inspection_request_id)

    client.next_outcome = "FAIL"
    client.next_failure_type = "DEFECT"
    result = client.request_inspection(req)

    inspection = service.apply_inspection_result(session, result)

    assert inspection.status == MaterialInspectionStatus.COMPLETED
    assert inspection.result == MaterialInspectionResult.FAIL
    assert inspection.failure_type == "DEFECT"

    # HOLD check
    assert service.is_delivery_item_released(session, item_id) is False


def test_fake_qa_not_evaluated(session: Session):
    service = MaterialInspectionService()
    client = FakeIncomingMaterialQAClient()

    item_id = _setup_delivery_item(session)
    req = service.request_inspection(session, item_id)
    service.mark_running(session, req.inspection_request_id)

    client.next_outcome = "NOT_EVALUATED"
    result = client.request_inspection(req)

    inspection = service.apply_inspection_result(session, result)
    assert inspection.status == MaterialInspectionStatus.COMPLETED
    assert inspection.result == MaterialInspectionResult.NOT_EVALUATED
    assert service.is_delivery_item_released(session, item_id) is False


def test_fake_qa_error_timeout_lifecycle(session: Session):
    service = MaterialInspectionService()
    client = FakeIncomingMaterialQAClient()

    item_id = _setup_delivery_item(session)
    req = service.request_inspection(session, item_id)
    service.mark_running(session, req.inspection_request_id)

    client.next_outcome = "TIMEOUT"
    with pytest.raises(IncomingMaterialQAClientTimeoutError):
        client.request_inspection(req)

    inspection = service.mark_error(session, req.inspection_request_id, "Timeout simulated")

    assert inspection.status == MaterialInspectionStatus.ERROR
    assert inspection.result is None
    assert inspection.failure_reason == "Timeout simulated"
    assert service.is_delivery_item_released(session, item_id) is False


def test_fake_qa_correlation_mismatch(session: Session):
    service = MaterialInspectionService()
    client = FakeIncomingMaterialQAClient()

    item_id = _setup_delivery_item(session)
    req = service.request_inspection(session, item_id)

    result = client.request_inspection(req)
    # Tamper with result
    result.inspection_cycle += 1

    with pytest.raises(MaterialInspectionError, match="Inspection cycle mismatch"):
        service.apply_inspection_result(session, result)


def test_fake_qa_idempotency(session: Session):
    service = MaterialInspectionService()
    client = FakeIncomingMaterialQAClient()

    item_id = _setup_delivery_item(session)
    req = service.request_inspection(session, item_id)
    result = client.request_inspection(req)

    inspection1 = service.apply_inspection_result(session, result)
    inspection2 = service.apply_inspection_result(session, result)

    assert inspection1 is inspection2

    # Conflict test
    result2 = result.model_copy()
    result2.result = "FAIL"
    with pytest.raises(MaterialInspectionError, match="Conflicting terminal result already applied"):
        service.apply_inspection_result(session, result2)


def test_fake_qa_reinspection_cycle(session: Session):
    service = MaterialInspectionService()
    client = FakeIncomingMaterialQAClient()

    item_id = _setup_delivery_item(session)

    req1 = service.request_inspection(session, item_id)
    assert req1.inspection_cycle == 1

    req2 = service.request_inspection(session, item_id)
    assert req2.inspection_cycle == 2
