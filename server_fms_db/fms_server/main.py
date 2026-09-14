"""Minimal asynchronous worker entrypoint for the future FMS Server."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from fms_server.vision_status import VisionStatusHandler, VisionStatusUdpReceiver
from fms_server.incoming_material_qa_udp_transport import (
    IncomingQAUdpRuntime,
    IncomingQAUdpRuntimeConfig,
)
from fms_server.incoming_qa_v02_orchestration_service import IncomingQAV02FmsReconciler
from fms_server.pre_roof_udp_transport import PreRoofUdpRuntime, PreRoofUdpRuntimeConfig
from fms_server.cell_status import CellStatusStore, RosCellStatusSubscriber
from fms_server.production_realtime_publisher import ProductionChangedPublisher
from fms_server.incoming_qa_realtime_publisher import IncomingQAChangedPublisher
from fms_server.production_inspection_realtime_publisher import ProductionInspectionChangedPublisher
from fms_server.execution_attempt_error_publisher import ExecutionAttemptErrorPublisher
from fms_server.forklift_runtime_state import ForkliftRuntimeStatePublisher
from shared.realtime.production_events import set_production_change_callback
from shared.realtime.incoming_qa_events import set_incoming_qa_change_callback
from shared.realtime.production_inspection_events import set_production_inspection_change_callback
from shared.realtime.error_events import set_execution_attempt_error_callback
from shared.config import get_settings
from shared.database import get_session_factory
from fms_server.worker import FmsWorker
from fms_server.pause_resume_coordinator import PauseResumeCoordinator
from fms_server.robot_cell_control import FakeRobotCellControlPort, Ros2RobotCellControlPort

from fms_server.execution_coordinator import FmsExecutionCoordinator
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator

from fms_server.fake_cell_action_transport import FakeCellActionTransport, FakeCellActionExchange
from fms_server.cell_action_transport import CellTaskExecutionResult
from fms_server.robot_cell_action_adapter import RobotCellActionAdapter
from fms_server.forklift_action_adapter import ForkliftActionAdapter
from fms_server.forklift_action_adapter import FakeForkliftActionTransport

from shared.services.production_orchestration_service import ProductionOrchestrationService
from shared.services.step_readiness_service import StepReadinessService
from shared.services.execution_attempt_service import ExecutionAttemptService
from shared.services.material_delivery_service import MaterialDeliveryService

logger = logging.getLogger(__name__)


def reconcile_pause_resume_once(session_factory, coordinator_factory) -> bool:
    """Run one durable control reconciliation with a thread-owned Session."""
    with session_factory() as session:
        return coordinator_factory(session).reconcile_once()

def create_action_transports(settings):
    """Compose fake or actual Action transports without starting worker/ROS loops."""

    ros_action_runtime = None
    if settings.cell_transport == "fake":
        logger.info("Using FAKE Robot Cell and TurtleBot transports")
        cell_transport = FakeCellActionTransport([FakeCellActionExchange(CellTaskExecutionResult.success()) for _ in range(1000)])
        forklift_transport = FakeForkliftActionTransport()
    elif settings.cell_transport == "ros2":
        logger.info("Using shared ROS2 Robot Cell and TurtleBot Action transports")
        from fms_server.ros2_cell_action_transport import Ros2CellActionTransport
        from fms_server.ros2_fms_action_runtime import Ros2FmsActionRuntime
        from fms_server.ros2_forklift_action_transport import Ros2ForkliftActionTransport

        ros_action_runtime = Ros2FmsActionRuntime()
        try:
            cell_transport = Ros2CellActionTransport(
                action_name=settings.cell_execute_task_action_name,
                server_wait_timeout_seconds=settings.cell_action_server_wait_timeout_seconds,
                result_timeout_seconds=settings.cell_action_result_timeout_seconds,
                runtime=ros_action_runtime,
            )
            forklift_transport = Ros2ForkliftActionTransport(
                execute_action_name=settings.forklift_execute_transport_action_name,
                return_home_action_name=settings.forklift_return_home_action_name,
                server_wait_timeout_seconds=settings.cell_action_server_wait_timeout_seconds,
                result_timeout_seconds=settings.cell_action_result_timeout_seconds,
                runtime=ros_action_runtime,
            )
        except Exception:
            ros_action_runtime.close()
            raise
    else:
        raise ValueError(f"Invalid CELL_TRANSPORT configuration: {settings.cell_transport}")
    return cell_transport, forklift_transport, ros_action_runtime


async def worker_loop(
    stop_event: asyncio.Event,
    vision_status_handler: VisionStatusHandler | None = None,
    settings=None,
    forklift_runtime_publisher: ForkliftRuntimeStatePublisher | None = None,
    cell_status_store: CellStatusStore | None = None,
) -> None:
    """Run the future production worker without busy-waiting."""

    if settings is None:
        settings = get_settings()

    cell_transport, forklift_transport, ros_action_runtime = create_action_transports(settings)
    # A sessionmaker is thread-safe to share; Sessions are not.  The pause/resume
    # loop creates its Session only inside its asyncio.to_thread worker.
    session_factory = get_session_factory()

    if settings.cell_transport == "ros2":
        logger.info(
            "Robot Cell ActionServer diagnostics: action=%s available=%s",
            settings.cell_execute_task_action_name,
            cell_transport.wait_for_server(timeout_seconds=settings.cell_action_server_wait_timeout_seconds),
        )

    cell_adapter = RobotCellActionAdapter(cell_transport)
    forklift_adapter = ForkliftActionAdapter(forklift_transport)
    if settings.cell_transport == "ros2":
        control_port = Ros2RobotCellControlPort()
    else:
        control_port = FakeRobotCellControlPort()

    def fms_execution_coordinator_factory(session):
        return FmsExecutionCoordinator(
            session,
            orchestration_service=ProductionOrchestrationService(session),
            step_readiness_service=StepReadinessService(MaterialDeliveryService(session)),
            robot_cell_adapter=cell_adapter,
            execution_attempt_service=ExecutionAttemptService(session),
            cell_status_store=cell_status_store,
        )

    def forklift_execution_coordinator_factory(session):
        return ForkliftExecutionCoordinator(
            session,
            adapter=forklift_adapter,
            execution_attempt_service=ExecutionAttemptService(session),
            runtime_state_sink=(forklift_runtime_publisher.notify_state if forklift_runtime_publisher else None),
            runtime_event_sink=(forklift_runtime_publisher.notify_event if forklift_runtime_publisher else None),
            runtime_state_cleanup_sink=(
                (lambda robot_id, req_id: forklift_runtime_publisher.notify_terminal(robot_id=robot_id, req_id=req_id))
                if forklift_runtime_publisher else None
            ),
        )

    def material_feed_execution_coordinator_factory(session):
        return MaterialFeedExecutionCoordinator(
            session,
            robot_cell_adapter=cell_adapter,
            cell_status_store=cell_status_store,
        )

    def pause_resume_coordinator_factory(session):
        return PauseResumeCoordinator(
            session, control_port=control_port, cell_status_store=cell_status_store
        )

    async def pause_resume_loop() -> None:
        """Keep control reconciliation alive while a worker tick awaits ExecuteTask."""
        while not stop_event.is_set():
            try:
                changed = await asyncio.to_thread(
                    reconcile_pause_resume_once,
                    session_factory,
                    pause_resume_coordinator_factory,
                )
                await asyncio.sleep(0.01 if changed else 0.25)
            except Exception:
                logger.exception("FMS pause/resume reconciliation error")
                await asyncio.sleep(1.0)

    worker = FmsWorker(
        session_factory=session_factory,
        fms_execution_coordinator_factory=fms_execution_coordinator_factory,
        forklift_execution_coordinator_factory=forklift_execution_coordinator_factory,
        material_feed_execution_coordinator_factory=material_feed_execution_coordinator_factory,
        pause_resume_coordinator_factory=pause_resume_coordinator_factory,
    )
    # ReturnHome keeps its existing durable eligibility authority. Both fake and
    # ROS2 transports now provide the separate ReturnHome Action contract.
    configure_auto_return = getattr(worker, "set_auto_empty_pallet_return_enabled", None)
    if callable(configure_auto_return):
        configure_auto_return(settings.cell_transport in {"fake", "ros2"})
    configure_prefetch = getattr(worker, "set_material_prefetch_mode", None)
    if callable(configure_prefetch):
        configure_prefetch(getattr(settings, "material_prefetch_mode", "disabled"))

    logger.info("Starting FMS worker loop with %s transport", settings.cell_transport)
    pause_resume_task = asyncio.create_task(pause_resume_loop(), name="fms-pause-resume-reconciliation")
    try:
        while not stop_event.is_set():
            try:
                dispatched = await asyncio.to_thread(worker.tick)
                if not dispatched:
                    await asyncio.sleep(1.0)
                else:
                    # Yield to loop if dispatched, don't sleep
                    await asyncio.sleep(0.01)
            except Exception:
                logger.exception("FMS worker loop error")
                await asyncio.sleep(5.0)
    finally:
        pause_resume_task.cancel()
        await asyncio.gather(pause_resume_task, return_exceptions=True)
        for transport in (control_port, forklift_transport, cell_transport):
            close = getattr(transport, "close", None)
            if callable(close):
                close()
        if ros_action_runtime is not None:
            ros_action_runtime.close()

async def incoming_qa_v02_reconciliation_loop(
    stop_event: asyncio.Event,
    runtime: IncomingQAUdpRuntime,
) -> None:
    """Send at most one persisted v0.2 transaction per durable reconciliation.

    The loop owns no product policy.  It sees only planned transactions and
    lets the service derive BASE→follow-up progression from terminal DB facts.
    """

    reconciler = IncomingQAV02FmsReconciler(get_session_factory())
    while not stop_event.is_set():
        try:
            transaction_id = await asyncio.to_thread(reconciler.reconcile_once)
            if transaction_id is not None:
                await runtime.send_transaction(transaction_id)
                await asyncio.sleep(0)
                continue
        except Exception:
            logger.exception("Incoming QA v0.2 reconciliation loop error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def pre_roof_reconciliation_loop(
    stop_event: asyncio.Event,
    runtime: PreRoofUdpRuntime,
) -> None:
    """Dispatch only durable PRE_ROOF RUNNING inspections lacking an initial send."""
    while not stop_event.is_set():
        try:
            inspection_id = await runtime.reconcile_once()
            if inspection_id is not None:
                await runtime.send_inspection(inspection_id)
                await asyncio.sleep(0)
                continue
        except Exception:
            logger.exception("PRE_ROOF reconciliation loop error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def run_worker() -> None:
    settings = get_settings()
    stop_event = asyncio.Event()
    vision_status_handler = VisionStatusHandler()
    vision_status_receiver = VisionStatusUdpReceiver(
        host=settings.vision_status_udp_host,
        port=settings.vision_status_udp_port,
        handler=vision_status_handler,
    )
    incoming_qa_udp_config = IncomingQAUdpRuntimeConfig.from_settings(settings)
    pre_roof_udp_config = PreRoofUdpRuntimeConfig.from_settings(settings)
    pre_roof_udp_runtime = (
        None if pre_roof_udp_config is None else PreRoofUdpRuntime(
            session_factory=get_session_factory(), config=pre_roof_udp_config
        )
    )
    incoming_qa_udp_runtime = (
        None
        if incoming_qa_udp_config is None
        else IncomingQAUdpRuntime(
            session_factory=get_session_factory(),
            config=incoming_qa_udp_config,
        )
    )
    production_publisher = ProductionChangedPublisher()
    incoming_qa_publisher = IncomingQAChangedPublisher()
    production_inspection_publisher = ProductionInspectionChangedPublisher()
    error_publisher = ExecutionAttemptErrorPublisher()
    await production_publisher.start()
    await incoming_qa_publisher.start()
    await production_inspection_publisher.start()
    await error_publisher.start()
    forklift_runtime_publisher = ForkliftRuntimeStatePublisher()
    await forklift_runtime_publisher.start()
    set_production_change_callback(production_publisher.notify_after_commit)
    set_incoming_qa_change_callback(incoming_qa_publisher.notify_after_commit)
    set_production_inspection_change_callback(production_inspection_publisher.notify_after_commit)
    set_execution_attempt_error_callback(error_publisher.notify_after_commit)
    cell_status_store: CellStatusStore | None = None
    cell_status_subscriber: RosCellStatusSubscriber | None = None
    if settings.cell_transport == "ros2":
        logger.info("Robot Cell ROS diagnostics: domain=%s rmw=%s cyclonedds_uri=%s", os.getenv("ROS_DOMAIN_ID", "<unset>"), os.getenv("RMW_IMPLEMENTATION", "<unset>"), os.getenv("CYCLONEDDS_URI", "<unset>"))
        cell_status_store = CellStatusStore()
        cell_status_subscriber = RosCellStatusSubscriber(cell_status_store)

    def request_shutdown() -> None:
        logger.info("FMS shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, request_shutdown)
        except NotImplementedError:
            signal.signal(signal_name, lambda *_: request_shutdown())

    logger.info("FMS Server started (env=%s, ROS_DOMAIN_ID=%s)", settings.app_env, settings.ros_domain_id)
    incoming_qa_reconciliation_task: asyncio.Task[None] | None = None
    pre_roof_reconciliation_task: asyncio.Task[None] | None = None
    try:
        if cell_status_subscriber is not None:
            cell_status_subscriber.start()
            logger.info("Robot Cell status diagnostics: received=false available=false state=<none>")
        await vision_status_receiver.start()
        if incoming_qa_udp_runtime is not None:
            await incoming_qa_udp_runtime.start()
            incoming_qa_reconciliation_task = asyncio.create_task(
                incoming_qa_v02_reconciliation_loop(stop_event, incoming_qa_udp_runtime)
            )
        if pre_roof_udp_runtime is not None:
            await pre_roof_udp_runtime.start()
            pre_roof_reconciliation_task = asyncio.create_task(
                pre_roof_reconciliation_loop(stop_event, pre_roof_udp_runtime)
            )
        await worker_loop(
            stop_event, vision_status_handler, settings=settings,
            forklift_runtime_publisher=forklift_runtime_publisher,
            cell_status_store=cell_status_store,
        )
    finally:
        stop_event.set()
        if incoming_qa_reconciliation_task is not None:
            incoming_qa_reconciliation_task.cancel()
            await asyncio.gather(incoming_qa_reconciliation_task, return_exceptions=True)
        if pre_roof_reconciliation_task is not None:
            pre_roof_reconciliation_task.cancel()
            await asyncio.gather(pre_roof_reconciliation_task, return_exceptions=True)
        if cell_status_subscriber is not None:
            cell_status_subscriber.close()
        if pre_roof_udp_runtime is not None:
            await pre_roof_udp_runtime.close()
        if incoming_qa_udp_runtime is not None:
            await incoming_qa_udp_runtime.close()
        vision_status_receiver.close()
        set_production_change_callback(None)
        set_incoming_qa_change_callback(None)
        set_production_inspection_change_callback(None)
        set_execution_attempt_error_callback(None)
        await error_publisher.stop()
        await incoming_qa_publisher.stop()
        await production_inspection_publisher.stop()
        await forklift_runtime_publisher.stop()
        await production_publisher.stop()
        logger.info("FMS Server stopped")


def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
