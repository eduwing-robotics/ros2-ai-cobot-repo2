from __future__ import annotations

from collections.abc import Generator, Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.inventory import get_db
from api_server.routers.production import get_incoming_material_qa_dispatch_coordinator
from fms_server.incoming_material_qa_dispatch_coordinator import IncomingMaterialQADispatchCoordinator
from fms_server.incoming_material_qa_http_client import (
    IncomingMaterialQAAcknowledgement,
    IncomingMaterialQAHttpTransportError,
)
from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobStatus,
    JobStep,
    MaterialDeliveryStatus,
    MaterialInspection,
    MaterialInspectionResult,
    MaterialInspectionStatus,
    Part,
    PartCategory,
    Product,
    ProductionJob,
    StepStatus,
    SupplyMode,
)


class RecordingVisionClient:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.requests = []
        self._fail_first = fail_first

    def send_request(self, request):
        self.requests.append(request)
        if self._fail_first:
            self._fail_first = False
            raise IncomingMaterialQAHttpTransportError("fake Vision send failure")
        return IncomingMaterialQAAcknowledgement(status_code=202)


@pytest.fixture(name="session_factory")
def session_factory_fixture() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture(name="db_session")
def db_session_fixture(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture(name="vision")
def vision_fixture() -> RecordingVisionClient:
    return RecordingVisionClient()


@pytest.fixture(name="client")
def client_fixture(db_session: Session, vision: RecordingVisionClient) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    runtime = IncomingMaterialQARuntime(client=vision)
    coordinator = IncomingMaterialQADispatchCoordinator(db_session, runtime=runtime)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_incoming_material_qa_dispatch_coordinator] = lambda: coordinator
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _context(
    session: Session,
    *,
    suffix: str,
    mode: SupplyMode | None = SupplyMode.TRANSPORTED,
    group: str | None = "OPERATOR_QA_GROUP",
) -> tuple[ProductionJob, JobMaterialDelivery, JobMaterialDeliveryItem]:
    product = Product(product_code=f"OP_QA_PRODUCT_{suffix}", product_name="Operator QA product")
    part = Part(
        part_code=f"OP_QA_PART_{suffix}",
        part_name="Operator QA part",
        category=PartCategory.STRUCTURE,
        unit="EA",
        vision_class=f"operator_qa_class_{suffix}",
    )
    session.add_all((product, part))
    session.flush()
    job = ProductionJob(job_code=f"OP-QA-JOB-{suffix}", product_code=product.product_code, status=JobStatus.RUNNING)
    session.add(job)
    session.flush()
    step = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="INSTALL_TEST",
        display_name="Operator Incoming QA",
        part_code=part.part_code,
        quantity=1,
        supply_mode=mode,
        supply_group_code=group,
        status=StepStatus.PENDING,
    )
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code=f"OP-QA-DEL-{suffix}",
        display_name="Operator QA delivery",
        status=MaterialDeliveryStatus.PENDING,
        supply_mode=mode,
        supply_group_code=group,
    )
    session.add_all((step, delivery))
    session.flush()
    item = JobMaterialDeliveryItem(
        job_delivery_id=delivery.job_delivery_id,
        job_step_id=step.job_step_id,
        part_code=part.part_code,
        quantity=1,
    )
    session.add(item)
    session.commit()
    return job, delivery, item


def _url(job: ProductionJob, delivery: JobMaterialDelivery, item: JobMaterialDeliveryItem) -> str:
    return (
        f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}"
        f"/items/{item.delivery_item_id}/incoming-qa/start"
    )

def _reinspect_url(job: ProductionJob, delivery: JobMaterialDelivery, item: JobMaterialDeliveryItem) -> str:
    return (
        f"/production/jobs/{job.job_id}/material-deliveries/{delivery.job_delivery_id}"
        f"/items/{item.delivery_item_id}/incoming-qa/reinspect"
    )


def _terminal(
    session: Session,
    item: JobMaterialDeliveryItem,
    *,
    status: MaterialInspectionStatus,
    result: MaterialInspectionResult | None,
    production_valid: bool | None,
) -> MaterialInspection:
    part = session.get(Part, item.part_code)
    assert part is not None
    inspection = MaterialInspection(
        inspection_request_id=f"operator-qa-terminal-{item.delivery_item_id}",
        delivery_item_id=item.delivery_item_id,
        inspection_cycle=1,
        status=status,
        result=result,
        expected_part_code=item.part_code,
        expected_class_name=part.vision_class,
        expected_quantity=item.quantity,
        production_valid=production_valid,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(inspection)
    session.commit()
    return inspection


def test_operator_start_creates_one_cycle_and_sends_one_fake_vision_request(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    job, delivery, item = _context(db_session, suffix="NEW")

    response = client.post(_url(job, delivery, item))

    assert response.status_code == 200
    assert response.json() == {
        "action": "CREATE_NEW",
        "delivery_item_id": item.delivery_item_id,
        "inspection_request_id": vision.requests[0].inspection_request_id,
        "inspection_cycle": 1,
        "vision_request_sent": True,
        "acknowledgement_status_code": 202,
    }
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert db_session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
    assert len(vision.requests) == 1


def test_requested_send_failure_reuses_same_cycle_on_later_operator_retry(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    vision._fail_first = True
    job, delivery, item = _context(db_session, suffix="RETRY")

    first = client.post(_url(job, delivery, item))
    assert first.status_code == 503
    persisted = db_session.scalar(select(MaterialInspection).where(MaterialInspection.delivery_item_id == item.delivery_item_id))
    assert persisted is not None and persisted.status is MaterialInspectionStatus.REQUESTED
    first_identity = (persisted.inspection_request_id, persisted.inspection_cycle)

    second = client.post(_url(job, delivery, item))
    assert second.status_code == 200
    assert second.json()["action"] == "REUSE_EXISTING"
    assert (second.json()["inspection_request_id"], second.json()["inspection_cycle"]) == first_identity
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert [request.inspection_request_id for request in vision.requests] == [first_identity[0], first_identity[0]]


def test_running_item_is_reused_without_new_send_or_cycle(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    job, delivery, item = _context(db_session, suffix="RUNNING")
    assert client.post(_url(job, delivery, item)).status_code == 200

    repeated = client.post(_url(job, delivery, item))

    assert repeated.status_code == 200
    assert repeated.json()["action"] == "REUSE_EXISTING"
    assert repeated.json()["vision_request_sent"] is False
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert len(vision.requests) == 1


@pytest.mark.parametrize(
    ("status", "result", "production_valid", "expected_action"),
    [
        (MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.PASS, True, "SKIP_RELEASED"),
        (MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.FAIL, False, "HOLD_TERMINAL_FAILURE"),
        (MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.NOT_EVALUATED, False, "HOLD_TERMINAL_FAILURE"),
        (MaterialInspectionStatus.ERROR, None, None, "HOLD_TERMINAL_FAILURE"),
    ],
)
def test_released_and_terminal_items_never_allocate_or_send_again(
    client: TestClient,
    db_session: Session,
    vision: RecordingVisionClient,
    status: MaterialInspectionStatus,
    result: MaterialInspectionResult | None,
    production_valid: bool | None,
    expected_action: str,
) -> None:
    job, delivery, item = _context(db_session, suffix=f"TERM_{status}_{result}")
    original = _terminal(db_session, item, status=status, result=result, production_valid=production_valid)

    response = client.post(_url(job, delivery, item))

    assert response.status_code == 200
    assert response.json()["action"] == expected_action
    assert response.json()["inspection_request_id"] == original.inspection_request_id
    assert response.json()["vision_request_sent"] is False
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert vision.requests == []


@pytest.mark.parametrize(
    ("mode", "group"),
    [(None, None), (None, "OP_QA_PARTIAL")],
)
def test_legacy_and_partial_policy_are_rejected_without_inspection(
    client: TestClient,
    db_session: Session,
    vision: RecordingVisionClient,
    mode: SupplyMode | None,
    group: str | None,
) -> None:
    job, delivery, item = _context(db_session, suffix=f"POLICY_{mode}_{group}", mode=mode, group=group)

    response = client.post(_url(job, delivery, item))

    assert response.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 0
    assert vision.requests == []


def test_manual_policy_creates_one_cycle_and_sends_one_fake_vision_request(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    job, delivery, item = _context(
        db_session,
        suffix="MANUAL",
        mode=SupplyMode.MANUAL,
        group="OP_QA_MANUAL",
    )

    response = client.post(_url(job, delivery, item))

    assert response.status_code == 200
    assert response.json()["action"] == "CREATE_NEW"
    assert response.json()["delivery_item_id"] == item.delivery_item_id
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert len(vision.requests) == 1


def test_ownership_is_fail_closed_and_one_call_targets_only_one_item(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    job, delivery, item = _context(db_session, suffix="OWNER")
    _, _, other_item = _context(db_session, suffix="OTHER")

    assert client.post(_url(job, delivery, other_item)).status_code == 404
    assert client.post(f"/production/jobs/999999/material-deliveries/{delivery.job_delivery_id}/items/{item.delivery_item_id}/incoming-qa/start").status_code == 404

    response = client.post(_url(job, delivery, item))
    assert response.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert vision.requests[0].delivery_item_id == item.delivery_item_id



def test_operator_start_is_globally_single_flight_across_distinct_items(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    first_job, first_delivery, first_item = _context(db_session, suffix="GLOBAL_FIRST")
    second_job, second_delivery, second_item = _context(db_session, suffix="GLOBAL_SECOND")

    assert client.post(_url(first_job, first_delivery, first_item)).status_code == 200
    blocked = client.post(_url(second_job, second_delivery, second_item))

    assert blocked.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert len(vision.requests) == 1


def test_operator_reinspection_creates_new_cycle_after_fail_and_preserves_original(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    job, delivery, item = _context(db_session, suffix="REINSPECT_FAIL")
    original = _terminal(
        db_session,
        item,
        status=MaterialInspectionStatus.COMPLETED,
        result=MaterialInspectionResult.FAIL,
        production_valid=False,
    )

    response = client.post(_reinspect_url(job, delivery, item))

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "CREATE_REINSPECTION"
    assert body["inspection_cycle"] == 2
    assert body["inspection_request_id"] != original.inspection_request_id
    assert body["vision_request_sent"] is True
    rows = list(db_session.scalars(select(MaterialInspection).where(
        MaterialInspection.delivery_item_id == item.delivery_item_id
    ).order_by(MaterialInspection.inspection_cycle)))
    assert [(row.inspection_cycle, row.inspection_request_id) for row in rows] == [
        (1, original.inspection_request_id),
        (2, body["inspection_request_id"]),
    ]
    assert len(vision.requests) == 1


@pytest.mark.parametrize("result", [MaterialInspectionResult.NOT_EVALUATED, MaterialInspectionResult.FAIL])
def test_operator_reinspection_accepts_each_terminal_hold_result(
    client: TestClient,
    db_session: Session,
    vision: RecordingVisionClient,
    result: MaterialInspectionResult,
) -> None:
    job, delivery, item = _context(db_session, suffix=f"REINSPECT_{result.value}")
    _terminal(
        db_session,
        item,
        status=MaterialInspectionStatus.COMPLETED,
        result=result,
        production_valid=False,
    )

    response = client.post(_reinspect_url(job, delivery, item))

    assert response.status_code == 200
    assert response.json()["action"] == "CREATE_REINSPECTION"
    assert len(vision.requests) == 1


def test_operator_reinspection_does_not_reinspect_pass_or_error(
    client: TestClient, db_session: Session, vision: RecordingVisionClient,
) -> None:
    for suffix, status, result, valid in (
        ("REINSPECT_PASS", MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.PASS, True),
        ("REINSPECT_ERROR", MaterialInspectionStatus.ERROR, None, None),
    ):
        job, delivery, item = _context(db_session, suffix=suffix)
        original = _terminal(db_session, item, status=status, result=result, production_valid=valid)
        response = client.post(_reinspect_url(job, delivery, item))
        assert response.status_code == 200
        assert response.json()["action"] in {"SKIP_RELEASED", "HOLD_TERMINAL_FAILURE"}
        assert response.json()["inspection_request_id"] == original.inspection_request_id
    assert vision.requests == []
