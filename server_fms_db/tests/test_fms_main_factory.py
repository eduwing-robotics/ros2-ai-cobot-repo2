from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fms_server.main as fms_main
from fms_server.forklift_action_adapter import ForkliftExecutionFeedback
from fms_server.forklift_execution_coordinator import ForkliftExecutionCoordinator
from fms_server.material_feed_execution_coordinator import MaterialFeedExecutionCoordinator
from shared.models import Base


def test_worker_loop_factory_constructs_material_feed_coordinator(monkeypatch) -> None:
    """Exercise the same nested factory used by FMS startup without ticking work."""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    created: list[MaterialFeedExecutionCoordinator] = []

    class CapturingWorker:
        def __init__(
            self,
            *,
            session_factory,
            fms_execution_coordinator_factory,
            forklift_execution_coordinator_factory,
            material_feed_execution_coordinator_factory,
            pause_resume_coordinator_factory=None,
        ) -> None:
            with session_factory() as session:
                created.append(material_feed_execution_coordinator_factory(session))

        def tick(self) -> bool:
            return False

    monkeypatch.setattr(fms_main, "FmsWorker", CapturingWorker)
    monkeypatch.setattr(fms_main, "get_session_factory", lambda: factory)
    stop_event = asyncio.Event()
    stop_event.set()

    asyncio.run(
        fms_main.worker_loop(
            stop_event,
            settings=SimpleNamespace(cell_transport="fake"),
        )
    )

    assert len(created) == 1
    assert isinstance(created[0], MaterialFeedExecutionCoordinator)


def test_worker_loop_factory_wires_forklift_runtime_callbacks(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    created: list[ForkliftExecutionCoordinator] = []

    class RecordingRuntimePublisher:
        def __init__(self) -> None:
            self.states = []
            self.events = []
            self.cleanup = []

        def notify_state(self, state) -> None:
            self.states.append(state)

        def notify_event(self, event) -> None:
            self.events.append(event)

        def notify_terminal(self, *, robot_id: str, req_id: str) -> None:
            self.cleanup.append((robot_id, req_id))

    class CapturingWorker:
        def __init__(
            self, *, session_factory, fms_execution_coordinator_factory,
            forklift_execution_coordinator_factory, material_feed_execution_coordinator_factory,
            pause_resume_coordinator_factory=None,
        ) -> None:
            with session_factory() as session:
                created.append(forklift_execution_coordinator_factory(session))

        def tick(self) -> bool:
            return False

    publisher = RecordingRuntimePublisher()
    monkeypatch.setattr(fms_main, "FmsWorker", CapturingWorker)
    monkeypatch.setattr(fms_main, "get_session_factory", lambda: factory)
    stop_event = asyncio.Event()
    stop_event.set()

    asyncio.run(
        fms_main.worker_loop(
            stop_event, settings=SimpleNamespace(cell_transport="fake"),
            forklift_runtime_publisher=publisher,
        )
    )

    callback = created[0]._bind_feedback(
        req_id="REQ-STARTUP", job_id=1, delivery_id=2, task_type="EXECUTE_TRANSPORT"
    )
    callback(ForkliftExecutionFeedback("MOVING_TO_PICKUP", 0.1, "feedback"))
    created[0]._clear_runtime_state("REQ-STARTUP")

    assert publisher.states[0].req_id == "REQ-STARTUP"
    assert created[0]._runtime_event_sink == publisher.notify_event
    assert publisher.cleanup[0][1] == "REQ-STARTUP"
