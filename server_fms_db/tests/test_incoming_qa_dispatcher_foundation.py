from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fms_server.incoming_material_qa_dispatch_coordinator import (
    IncomingMaterialQADispatchAction,
    IncomingMaterialQADispatchCoordinator,
)
from fms_server.incoming_material_qa_http_client import (
    IncomingMaterialQAAcknowledgement,
    IncomingMaterialQAHttpTransportError,
)
from fms_server.incoming_material_qa_runtime import IncomingMaterialQARuntime
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    Base,
    JobMaterialDelivery,
    Inventory,
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
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.schemas.vision import IncomingMaterialQAResult, QAWireDetection


class RecordingClient:
    def __init__(self) -> None:
        self.requests = []

    def send_request(self, request):
        self.requests.append(request)
        return IncomingMaterialQAAcknowledgement(status_code=202)


class FailThenAckClient(RecordingClient):
    def __init__(self) -> None:
        super().__init__()
        self._fail_once = True

    def send_request(self, request):
        self.requests.append(request)
        if self._fail_once:
            self._fail_once = False
            raise IncomingMaterialQAHttpTransportError("temporary Vision network failure")
        return IncomingMaterialQAAcknowledgement(status_code=202)


@pytest.fixture(name="session")
def session_fixture() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _item(
    session: Session,
    *,
    mode: SupplyMode | None = SupplyMode.TRANSPORTED,
    group: str | None = "QA_DISPATCH_GROUP",
    destination: str | None = None,
    suffix: str = "A",
) -> JobMaterialDeliveryItem:
    product = Product(product_code=f"QA_DISPATCH_PRODUCT_{suffix}", product_name="QA dispatcher")
    part = Part(
        part_code=f"QA_DISPATCH_PART_{suffix}",
        part_name="QA dispatcher part",
        category=PartCategory.STRUCTURE,
        unit="EA",
        vision_class=f"qa_dispatch_class_{suffix}",
    )
    session.add_all((product, part))
    session.flush()
    session.add(Inventory(part_code=part.part_code, quantity=10))
    job = ProductionJob(job_code=f"QA_DISPATCH_JOB_{suffix}", product_code=product.product_code, status=JobStatus.REQUESTED)
    session.add(job)
    session.flush()
    step = JobStep(
        job_id=job.job_id,
        step_order=1,
        operation_code="INSTALL_TEST",
        display_name="Incoming QA dispatcher test",
        part_code=part.part_code,
        quantity=1,
        supply_mode=mode,
        supply_group_code=group,
        supply_destination_code=destination,
        status=StepStatus.PENDING,
    )
    delivery = JobMaterialDelivery(
        production_job_id=job.job_id,
        batch_order=1,
        delivery_code=f"QA_DISPATCH_DEL_{suffix}",
        display_name="Incoming QA dispatcher delivery",
        status=MaterialDeliveryStatus.PENDING,
        supply_mode=mode,
        supply_group_code=group,
        supply_destination_code=destination,
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
    return item


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
        inspection_request_id=f"qa-dispatch-terminal-{item.delivery_item_id}",
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


def _coordinator(session: Session, client: RecordingClient | None = None) -> IncomingMaterialQADispatchCoordinator:
    runtime = IncomingMaterialQARuntime(client=client) if client is not None else None
    return IncomingMaterialQADispatchCoordinator(session, runtime=runtime)


def test_prepare_creates_one_cycle_and_reuses_requested_without_network(session: Session) -> None:
    item = _item(session)
    client = RecordingClient()
    coordinator = _coordinator(session, client)

    first = coordinator.prepare_inspection(delivery_item_id=item.delivery_item_id)
    second = coordinator.prepare_inspection(delivery_item_id=item.delivery_item_id)

    assert first.action is IncomingMaterialQADispatchAction.CREATE_NEW
    assert second.action is IncomingMaterialQADispatchAction.REUSE_EXISTING
    assert second.should_send is True
    assert (first.inspection_request_id, first.inspection_cycle) == (second.inspection_request_id, second.inspection_cycle)
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert client.requests == []


def test_dispatch_sends_only_after_persisted_prepare_and_running_is_not_resent(session: Session) -> None:
    item = _item(session, suffix="SEND")
    client = RecordingClient()
    coordinator = _coordinator(session, client)

    prepared = coordinator.prepare_inspection(delivery_item_id=item.delivery_item_id)
    assert prepared.action is IncomingMaterialQADispatchAction.CREATE_NEW
    assert client.requests == []
    result = coordinator.dispatch_item(delivery_item_id=item.delivery_item_id)
    repeated = coordinator.dispatch_item(delivery_item_id=item.delivery_item_id)

    inspection = MaterialInspectionService().get_latest_inspection(session, item.delivery_item_id)
    assert result.decision.action is IncomingMaterialQADispatchAction.REUSE_EXISTING
    assert result.acknowledgement is not None and result.acknowledgement.status_code == 202
    assert repeated.decision.action is IncomingMaterialQADispatchAction.REUSE_EXISTING
    assert repeated.acknowledgement is None
    assert inspection is not None and inspection.status is MaterialInspectionStatus.RUNNING
    assert len(client.requests) == 1
    assert client.requests[0].inspection_request_id == prepared.inspection_request_id


@pytest.mark.parametrize(
    ("status", "result", "production_valid", "action"),
    [
        (MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.PASS, True, IncomingMaterialQADispatchAction.SKIP_RELEASED),
        (MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.FAIL, False, IncomingMaterialQADispatchAction.HOLD_TERMINAL_FAILURE),
        (MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.NOT_EVALUATED, False, IncomingMaterialQADispatchAction.HOLD_TERMINAL_FAILURE),
        (MaterialInspectionStatus.ERROR, None, None, IncomingMaterialQADispatchAction.HOLD_TERMINAL_FAILURE),
    ],
)
def test_terminal_latest_cycle_never_auto_allocates_or_sends(
    session: Session,
    status: MaterialInspectionStatus,
    result: MaterialInspectionResult | None,
    production_valid: bool | None,
    action: IncomingMaterialQADispatchAction,
) -> None:
    item = _item(session, suffix=f"TERM_{status}_{result}")
    original = _terminal(session, item, status=status, result=result, production_valid=production_valid)
    client = RecordingClient()
    decision = _coordinator(session, client).dispatch_item(delivery_item_id=item.delivery_item_id).decision

    assert decision.action is action
    assert decision.inspection_request_id == original.inspection_request_id
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert client.requests == []


@pytest.mark.parametrize(
    ("mode", "group", "expected"),
    [
        (SupplyMode.MANUAL, "QA_MANUAL", IncomingMaterialQADispatchAction.CREATE_NEW),
        (None, None, IncomingMaterialQADispatchAction.NOT_APPLICABLE),
        (None, "QA_PARTIAL", IncomingMaterialQADispatchAction.POLICY_INVALID),
    ],
)
def test_policy_filter_preserves_legacy_exclusion_and_fails_closed_partial(
    session: Session,
    mode: SupplyMode | None,
    group: str | None,
    expected: IncomingMaterialQADispatchAction,
) -> None:
    item = _item(session, mode=mode, group=group, suffix=f"POLICY_{mode}_{group}")
    decision = _coordinator(session).prepare_inspection(delivery_item_id=item.delivery_item_id)

    assert decision.action is expected
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == (
        1 if expected is IncomingMaterialQADispatchAction.CREATE_NEW else 0
    )


def test_communication_retry_reuses_same_request_and_cycle_without_reinspection(session: Session) -> None:
    item = _item(session, suffix="RETRY")
    client = FailThenAckClient()
    coordinator = _coordinator(session, client)

    with pytest.raises(IncomingMaterialQAHttpTransportError):
        coordinator.dispatch_item(delivery_item_id=item.delivery_item_id)
    failed_send = MaterialInspectionService().get_latest_inspection(session, item.delivery_item_id)
    assert failed_send is not None
    assert failed_send.status is MaterialInspectionStatus.REQUESTED
    assert failed_send.failure_reason is not None
    first_request_id, first_cycle = failed_send.inspection_request_id, failed_send.inspection_cycle

    retried = coordinator.dispatch_item(delivery_item_id=item.delivery_item_id)
    inspection = MaterialInspectionService().get_latest_inspection(session, item.delivery_item_id)
    assert retried.decision.action is IncomingMaterialQADispatchAction.REUSE_EXISTING
    assert inspection is not None
    assert (inspection.inspection_request_id, inspection.inspection_cycle) == (first_request_id, first_cycle)
    assert inspection.status is MaterialInspectionStatus.RUNNING
    assert inspection.failure_reason is None
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert [request.inspection_request_id for request in client.requests] == [first_request_id, first_request_id]


def test_job_materialization_does_not_auto_create_or_send_qa(session: Session) -> None:
    product = Product(product_code="QA_NO_AUTO_PRODUCT", product_name="No auto QA")
    part = Part(part_code="QA_NO_AUTO_PART", part_name="No auto QA part", category=PartCategory.STRUCTURE, unit="EA", vision_class="qa_no_auto_class")
    session.add_all((product, part))
    session.flush()
    session.add(Inventory(part_code=part.part_code, quantity=10))
    recipe = AssemblyRecipe(product_code=product.product_code, version=1, is_active=True)
    session.add(recipe)
    session.flush()
    session.add(AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=1, operation_code="INSTALL_TEST", display_name="No auto QA", part_code=part.part_code, quantity=1, supply_mode=SupplyMode.TRANSPORTED, supply_group_code="QA_NO_AUTO_GROUP", is_terminal=True))
    session.commit()

    ProductionOrchestrationService(session).create_job(product_code=product.product_code, job_code="QA-NO-AUTO-JOB")

    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 0



@pytest.mark.parametrize(
    ("terminal_result", "production_valid"),
    [
        (MaterialInspectionResult.PASS, True),
        (MaterialInspectionResult.FAIL, False),
        (MaterialInspectionResult.NOT_EVALUATED, False),
    ],
)
def test_global_single_flight_blocks_another_item_until_terminal_callback(
    session: Session,
    terminal_result: MaterialInspectionResult,
    production_valid: bool,
) -> None:
    first_item = _item(session, suffix="GLOBAL_FIRST")
    second_item = _item(session, suffix="GLOBAL_SECOND")
    client = RecordingClient()
    coordinator = _coordinator(session, client)

    first = coordinator.dispatch_item(delivery_item_id=first_item.delivery_item_id)
    blocked = coordinator.dispatch_item(delivery_item_id=second_item.delivery_item_id)

    assert first.decision.action is IncomingMaterialQADispatchAction.CREATE_NEW
    assert blocked.decision.action is IncomingMaterialQADispatchAction.GLOBAL_BUSY
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 1
    assert len(client.requests) == 1

    inspection = MaterialInspectionService().get_latest_inspection(session, first_item.delivery_item_id)
    assert inspection is not None
    MaterialInspectionService().apply_inspection_result(session, IncomingMaterialQAResult(
        inspection_request_id=inspection.inspection_request_id,
        delivery_item_id=inspection.delivery_item_id,
        inspection_cycle=inspection.inspection_cycle,
        status="COMPLETED",
        result=terminal_result.value,
        failure_type=None,
        expected_part_code=inspection.expected_part_code,
        expected_class_name=inspection.expected_class_name,
        expected_quantity=inspection.expected_quantity,
        detected_quantity=inspection.expected_quantity if production_valid else 0,
        detections=(
            [QAWireDetection(
                class_name=inspection.expected_class_name,
                class_id=1,
                confidence=0.99,
                bbox_xyxy=[1.0, 1.0, 2.0, 2.0],
            )]
            if production_valid else []
        ),
        frame_width=640,
        frame_height=480,
        camera_source="GLOBAL_CAMERA",
        frame_seq=1,
        timestamp=datetime.now(timezone.utc),
        model_scope="test",
        model_version="test",
        production_valid=production_valid,
    ))
    session.commit()

    next_item = coordinator.dispatch_item(delivery_item_id=second_item.delivery_item_id)
    assert next_item.decision.action is IncomingMaterialQADispatchAction.CREATE_NEW
    assert session.scalar(select(func.count()).select_from(MaterialInspection)) == 2
    assert len(client.requests) == 2


@pytest.mark.parametrize("result", [MaterialInspectionResult.FAIL, MaterialInspectionResult.NOT_EVALUATED])
def test_explicit_reinspection_creates_new_identity_only_for_reinspectable_terminal(
    session: Session,
    result: MaterialInspectionResult,
) -> None:
    item = _item(session, suffix=f"REINSPECT_{result.value}")
    original = _terminal(
        session,
        item,
        status=MaterialInspectionStatus.COMPLETED,
        result=result,
        production_valid=False,
    )
    client = RecordingClient()

    result_dispatch = _coordinator(session, client).dispatch_reinspection_item(
        delivery_item_id=item.delivery_item_id
    )
    latest = MaterialInspectionService().get_latest_inspection(session, item.delivery_item_id)

    assert result_dispatch.decision.action is IncomingMaterialQADispatchAction.CREATE_REINSPECTION
    assert latest is not None
    assert latest.inspection_cycle == original.inspection_cycle + 1
    assert latest.inspection_request_id != original.inspection_request_id
    assert latest.status is MaterialInspectionStatus.RUNNING
    assert len(client.requests) == 1
    assert client.requests[0].expected_part_code == original.expected_part_code
    assert client.requests[0].expected_class_name == original.expected_class_name
    assert client.requests[0].expected_quantity == original.expected_quantity


def test_explicit_reinspection_does_not_override_pass_or_error(session: Session) -> None:
    for suffix, status, result, valid in (
        ("REINSPECT_PASS", MaterialInspectionStatus.COMPLETED, MaterialInspectionResult.PASS, True),
        ("REINSPECT_ERROR", MaterialInspectionStatus.ERROR, None, None),
    ):
        item = _item(session, suffix=suffix)
        original = _terminal(session, item, status=status, result=result, production_valid=valid)
        decision = _coordinator(session).prepare_reinspection(
            delivery_item_id=item.delivery_item_id
        )
        assert decision.action in {
            IncomingMaterialQADispatchAction.SKIP_RELEASED,
            IncomingMaterialQADispatchAction.HOLD_TERMINAL_FAILURE,
        }
        assert decision.inspection_request_id == original.inspection_request_id
        assert session.scalar(
            select(func.count()).select_from(MaterialInspection).where(
                MaterialInspection.delivery_item_id == item.delivery_item_id
            )
        ) == 1
