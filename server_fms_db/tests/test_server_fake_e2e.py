import pytest
from datetime import datetime, timezone
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from shared.models.factory import (
    Base, Product, ProductionJob, JobStep, StepStatus, JobStatus,
    JobMaterialDelivery, JobMaterialDeliveryItem, MaterialDeliveryStatus,
    JobMaterialFeedExecution, MaterialFeedStatus,
    ExecutionAttempt, ExecutionAttemptStatus,
    Part, PartCategory
)
from shared.schemas.vision import IncomingMaterialQAResult
from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.material_delivery_service import MaterialDeliveryService
from shared.services.material_feed_execution_service import MaterialFeedExecutionService
from shared.services.material_inspection_service import MaterialInspectionService
from shared.services.step_readiness_service import StepReadinessService
from shared.services.execution_attempt_service import ExecutionAttemptService
from tests.recipe_test_support import seed_and_reserve_inventory_for_job

from fms_server.worker import FmsWorker
from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator

from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.forklift_action_adapter import ForkliftActionAdapter, ForkliftExecutionResult, ForkliftActionStatus

from fms_server.fake_cell_action_transport import FakeCellActionTransport, FakeCellActionExchange
from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.forklift_action_adapter import FakeForkliftActionTransport

@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)

@pytest.fixture
def session(session_factory):
    with session_factory() as session:
        yield session

def make_parts(session):
    parts = [
        Part(part_code="W1", part_name="Wall 1", category=PartCategory.STRUCTURE, vision_class="wall_ext_left", unit="EA"),
        Part(part_code="W2", part_name="Wall 2", category=PartCategory.STRUCTURE, vision_class="wall_ext_left", unit="EA"),
    ]
    session.add_all(parts)
    session.commit()

def create_worker(session_factory, cell_exchanges=None, forklift_transport_override=None):
    if cell_exchanges is None:
        cell_exchanges = [FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(100)]

    cell_transport = FakeCellActionTransport(cell_exchanges)
    if forklift_transport_override:
        forklift_transport = forklift_transport_override
    else:
        forklift_transport = FakeForkliftActionTransport()

    cell_adapter = RobotCellActionAdapter(cell_transport)
    forklift_adapter = ForkliftActionAdapter(forklift_transport)

    def fms_coord_factory(session):
        return FmsExecutionCoordinator(
            session,
            orchestration_service=ProductionOrchestrationService(session),
            step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
            robot_cell_adapter=cell_adapter,
            execution_attempt_service=ExecutionAttemptService(session)
        )

    def forklift_coord_factory(session):
        return ForkliftExecutionCoordinator(
            session,
            adapter=forklift_adapter,
            execution_attempt_service=ExecutionAttemptService(session)
        )

    def feed_coord_factory(session):
        return MaterialFeedExecutionCoordinator(
            session,
            robot_cell_adapter=cell_adapter
        )

    worker = FmsWorker(
        session_factory=session_factory,
        fms_execution_coordinator_factory=fms_coord_factory,
        forklift_execution_coordinator_factory=forklift_coord_factory,
        material_feed_execution_coordinator_factory=feed_coord_factory
    )
    return worker, cell_transport, forklift_transport

def pass_qa(session, delivery_item_id):
    qa_service = MaterialInspectionService()
    req = qa_service.request_inspection(session, delivery_item_id)
    session.commit()
    qa_service.mark_running(session, req.inspection_request_id)
    session.commit()
    qa_service.apply_inspection_result(session, IncomingMaterialQAResult(
        inspection_request_id=req.inspection_request_id,
        delivery_item_id=req.delivery_item_id,
        inspection_cycle=req.inspection_cycle,
        result="PASS",
        expected_part_code=req.expected_part_code,
        expected_class_name=req.expected_class_name,
        expected_quantity=req.expected_quantity,
        detected_quantity=req.expected_quantity,
        detections=[],
        frame_width=640,
        frame_height=480,
        camera_source="D435",
        frame_seq=1,
        timestamp=datetime.now(timezone.utc),
        model_scope="m",
        model_version="1",
        production_valid=True
    ))
    session.commit()
    item = session.get(JobMaterialDeliveryItem, delivery_item_id)
    assert item is not None
    delivery = session.get(JobMaterialDelivery, item.job_delivery_id)
    assert delivery is not None
    job = session.get(ProductionJob, delivery.production_job_id)
    assert job is not None
    seed_and_reserve_inventory_for_job(session, job=job)
    session.commit()

def _add_ready_legacy_delivery(session, job, step):
    delivery = JobMaterialDelivery(production_job_id=job.job_id, delivery_code=f"{job.job_code}-READY", display_name="Ready legacy", status=MaterialDeliveryStatus.COMPLETED, batch_order=1)
    session.add(delivery)
    session.flush()
    item = JobMaterialDeliveryItem(job_delivery_id=delivery.job_delivery_id, job_step_id=step.job_step_id, part_code=step.part_code, quantity=1)
    session.add_all((item, JobMaterialFeedExecution(job_delivery_id=delivery.job_delivery_id, status=MaterialFeedStatus.COMPLETED)))
    session.commit()
    pass_qa(session, item.delivery_item_id)

def test_SE1_delivery_qa_hold(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.PENDING, batch_order=1)
    delivery_item = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    session.add_all([p, job, s1, delivery, delivery_item])
    session.commit()
    worker, cell_t, fork_t = create_worker(session_factory)
    assert not worker.tick()
    assert len(fork_t.execute_transport_requests) == 0
    session.refresh(delivery)
    assert delivery.status == MaterialDeliveryStatus.PENDING
    assert not worker.tick()
    assert len(cell_t.commands) == 0

def test_SE2_qa_pass_feed_cell(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="Step", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.COMPLETED, batch_order=1)
    delivery_item = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    feed = JobMaterialFeedExecution(job_delivery_id=1, status=MaterialFeedStatus.PENDING)
    session.add_all([p, job, s1, delivery, delivery_item, feed])
    session.commit()
    worker, cell_t, fork_t = create_worker(session_factory)
    assert not worker.tick()
    pass_qa(session, delivery_item.delivery_item_id)
    assert worker.tick()
    session.refresh(feed)
    assert feed.status == MaterialFeedStatus.COMPLETED
    assert worker.tick()
    assert len(cell_t.commands) == 2
    session.refresh(s1)
    assert s1.status == StepStatus.COMPLETED

def test_SE3_sequential_steps(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=False)
    s2 = JobStep(job_id=1, step_order=2, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S2", vision_class="wall_ext_left", slot_code="SLOT2", pick_zone="CONVEYOR_PICK", part_code="W2", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.COMPLETED, batch_order=1)
    d1 = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    d2 = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=2, part_code="W2", quantity=1)
    feed = JobMaterialFeedExecution(job_delivery_id=1, status=MaterialFeedStatus.COMPLETED)
    session.add_all([p, job, s1, s2, delivery, d1, d2, feed])
    session.commit()
    for d in [d1, d2]: pass_qa(session, d.delivery_item_id)
    worker, cell_t, fork_t = create_worker(session_factory)
    assert worker.tick()
    assert len(cell_t.commands) == 1
    assert json.loads(cell_t.commands[0].parts_json)[0]["part_code"] == "W1"
    assert worker.tick()
    assert len(cell_t.commands) == 2
    assert json.loads(cell_t.commands[1].parts_json)[0]["part_code"] == "W2"

def test_SE4_delivery_failed(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.PENDING, batch_order=1)
    d1 = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    session.add_all([p, job, s1, delivery, d1])
    session.commit()
    pass_qa(session, d1.delivery_item_id)
    class FailedForkliftTransport(FakeForkliftActionTransport):
        def send_execute_transport(self, *args, **kwargs):
            return ForkliftExecutionResult(status=ForkliftActionStatus.FAILED, error_code="E1", detail="err")
    worker, cell_t, fork_t = create_worker(session_factory, forklift_transport_override=FailedForkliftTransport())
    assert worker.tick()
    session.refresh(delivery)
    assert delivery.status == MaterialDeliveryStatus.FAILED
    assert not worker.tick()

def test_SE5_qa_fail(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.COMPLETED, batch_order=1)
    d1 = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    session.add_all([p, job, s1, delivery, d1])
    session.commit()
    qa_service = MaterialInspectionService()
    req = qa_service.request_inspection(session, d1.delivery_item_id)
    session.commit()
    qa_service.mark_running(session, req.inspection_request_id)
    session.commit()
    qa_service.apply_inspection_result(session, IncomingMaterialQAResult(
        inspection_request_id=req.inspection_request_id,
        delivery_item_id=req.delivery_item_id,
        inspection_cycle=req.inspection_cycle,
        result="FAIL",
        expected_part_code=req.expected_part_code,
        expected_class_name=req.expected_class_name,
        expected_quantity=req.expected_quantity,
        detected_quantity=req.expected_quantity,
        detections=[],
        frame_width=640, frame_height=480, camera_source="D435", frame_seq=1,
        timestamp=datetime.now(timezone.utc), model_scope="m", model_version="1",
        production_valid=False,
        failure_type="DEFECT"
    ))
    session.commit()
    worker, cell_t, fork_t = create_worker(session_factory)
    assert not worker.tick()

def test_SE6_feed_failed(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.COMPLETED, batch_order=1)
    d1 = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    feed = JobMaterialFeedExecution(job_delivery_id=1, status=MaterialFeedStatus.PENDING)
    session.add_all([p, job, s1, delivery, d1, feed])
    session.commit()
    pass_qa(session, d1.delivery_item_id)
    exchanges = [FakeCellActionExchange(CellTaskExecutionResult.cell_failed(error_code="E1", detail="err"))]
    worker, cell_t, fork_t = create_worker(session_factory, cell_exchanges=exchanges)
    assert worker.tick()
    session.refresh(feed)
    assert feed.status == MaterialFeedStatus.FAILED
    assert not worker.tick()

def test_SE7_cell_failed(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    session.add_all([p, job, s1])
    session.commit()
    _add_ready_legacy_delivery(session, job, s1)
    exchanges = [FakeCellActionExchange(CellTaskExecutionResult.cell_failed(error_code="E1", detail="err"))]
    worker, cell_t, fork_t = create_worker(session_factory, cell_exchanges=exchanges)
    assert worker.tick()
    session.refresh(s1)
    session.refresh(job)
    assert s1.status == StepStatus.FAILED
    assert job.status == JobStatus.FAILED
    assert not worker.tick()

def test_SE8_cell_unknown(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    session.add_all([p, job, s1])
    session.commit()
    _add_ready_legacy_delivery(session, job, s1)
    exchanges = [FakeCellActionExchange(CellTaskExecutionResult.transport_error(detail="timeout"))]
    worker, cell_t, fork_t = create_worker(session_factory, cell_exchanges=exchanges)
    assert worker.tick()
    assert not worker.tick()

def test_SE9_pre_roof_hold(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING, assembly_recipe_id=1)
    s1 = JobStep(source_recipe_stage_id=1, job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.COMPLETED, batch_order=1)
    d1 = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    feed = JobMaterialFeedExecution(job_delivery_id=1, status=MaterialFeedStatus.COMPLETED)
    session.add_all([p, job, s1, delivery, d1, feed])
    session.commit()
    pass_qa(session, d1.delivery_item_id)
    worker, cell_t, fork_t = create_worker(session_factory)
    import logging; logging.basicConfig(level=logging.INFO)
    assert worker.tick() # Cell task finishes successfully
    session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert not worker.tick() # Held at PRE_ROOF_READY!

def test_SE10_retick_safety(session_factory, session):
    make_parts(session)
    p = Product(product_code="HOUSE_A", product_name="A")
    job = ProductionJob(product_code="HOUSE_A", job_code="J1", status=JobStatus.RUNNING)
    s1 = JobStep(job_id=1, step_order=1, operation_code="INSTALL_LEFT_OUTER_WALL", status=StepStatus.PENDING, display_name="S1", vision_class="wall_ext_left", slot_code="SLOT1", pick_zone="CONVEYOR_PICK", part_code="W1", is_terminal=True)
    delivery = JobMaterialDelivery(production_job_id=1, delivery_code="DEL1", display_name="Del1", status=MaterialDeliveryStatus.COMPLETED, batch_order=1)
    d1 = JobMaterialDeliveryItem(job_delivery_id=1, job_step_id=1, part_code="W1", quantity=1)
    feed = JobMaterialFeedExecution(job_delivery_id=1, status=MaterialFeedStatus.COMPLETED)
    session.add_all([p, job, s1, delivery, d1, feed])
    session.commit()
    pass_qa(session, d1.delivery_item_id)
    worker, cell_t, fork_t = create_worker(session_factory)
    assert worker.tick()
    assert not worker.tick()
    assert len(fork_t.execute_transport_requests) == 0
    assert len(cell_t.commands) == 1
