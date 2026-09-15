from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.ai import get_conversation_service
from api_server.services.production_conversation_service import ProductionConversationService
from shared.enums.ai import Intent
from shared.models import Base
from shared.models.factory import (
    ExecutionAttempt,
    ExecutionAttemptControlState,
    ExecutionAttemptStatus,
    ExecutorType,
    JobStatus,
    Product,
    ProductionJob,
    ProductionJobControlState,
)
from shared.schemas.ai import StructuredCommand
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_control_service import ProductionControlService


class MappingInterpreter:
    def __init__(self, commands: dict[str, StructuredCommand]) -> None:
        self.commands = commands

    async def interpret(self, text: str):
        return text, self.commands[text], "{}"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(Product(product_code="VOICE_CONTROL", product_name="Voice control product"))
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _job(session: Session, *, status: JobStatus = JobStatus.RUNNING) -> ProductionJob:
    job = ProductionJob(
        product_code="VOICE_CONTROL", job_code=f"VOICE-CONTROL-{session.query(ProductionJob).count()}", status=status,
    )
    session.add(job)
    session.commit()
    return job


def _robot_attempt(session: Session, job: ProductionJob, *, control_state=ExecutionAttemptControlState.ACTIVE) -> ExecutionAttempt:
    attempt = ExecutionAttempt(
        req_id=f"voice-cell-{job.job_id}", executor_type=ExecutorType.ROBOT_CELL,
        command_type="EXECUTE_TASK", job_id=job.job_id, attempt_no=1,
        status=ExecutionAttemptStatus.ACCEPTED, control_state=control_state,
        request_payload_json="{}",
    )
    session.add(attempt)
    session.commit()
    return attempt


def _service(session: Session, commands: dict[str, StructuredCommand]) -> ProductionConversationService:
    return ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=MappingInterpreter(commands),
        control_service=ProductionControlService(session),
    )


def _pause(*, target: int | None = None) -> StructuredCommand:
    return StructuredCommand(
        intent=Intent.PAUSE_JOB, target_job_id=str(target) if target is not None else None,
        requires_confirmation=True,
    )


def _resume(*, target: int | None = None) -> StructuredCommand:
    return StructuredCommand(
        intent=Intent.RESUME_JOB, target_job_id=str(target) if target is not None else None,
        requires_confirmation=True,
    )


def test_voice_pause_explicit_target_persists_request_not_physical_settlement(session: Session) -> None:
    job = _job(session); attempt = _robot_attempt(session, job)
    result = asyncio.run(_service(session, {"pause": _pause(target=job.job_id)}).handle_text(session_id="s", text="pause"))
    assert result.message == "생산 일시정지를 요청했습니다."
    assert job.status is JobStatus.RUNNING
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
    assert job.control_immediate_requested is True
    assert attempt.status is ExecutionAttemptStatus.ACCEPTED
    assert attempt.control_state is ExecutionAttemptControlState.PAUSE_REQUESTED


def test_voice_pause_implicit_unique_target_and_ambiguous_target_is_fail_closed(session: Session) -> None:
    job = _job(session)
    service = _service(session, {"pause": _pause()})
    first = asyncio.run(service.handle_text(session_id="s", text="pause"))
    assert first.message == "생산 일시정지를 요청했습니다."
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED

    _job(session)
    ambiguous = asyncio.run(service.handle_text(session_id="s", text="pause"))
    assert "여러 생산 작업" in ambiguous.message


def test_voice_pause_idempotency_terminal_and_forklift_unsupported(session: Session) -> None:
    job = _job(session); service = _service(session, {"pause": _pause(target=job.job_id)})
    assert asyncio.run(service.handle_text(session_id="s", text="pause")).message == "생산 일시정지를 요청했습니다."
    assert "이미 요청" in asyncio.run(service.handle_text(session_id="s", text="pause")).message

    forklift = _job(session)
    session.add(ExecutionAttempt(
        req_id=f"voice-forklift-{forklift.job_id}", executor_type=ExecutorType.FORKLIFT,
        command_type="EXECUTE_TRANSPORT", job_id=forklift.job_id, attempt_no=1,
        status=ExecutionAttemptStatus.ACCEPTED, request_payload_json="{}",
    )); session.commit()
    unsupported = asyncio.run(_service(session, {"pause-forklift": _pause(target=forklift.job_id)}).handle_text(session_id="s", text="pause-forklift"))
    assert "물류 이동" in unsupported.message
    assert forklift.status is JobStatus.RUNNING and forklift.control_state is ProductionJobControlState.ACTIVE

    terminal = _job(session, status=JobStatus.COMPLETED)
    blocked = asyncio.run(_service(session, {"pause-terminal": _pause(target=terminal.job_id)}).handle_text(session_id="s", text="pause-terminal"))
    assert "일시정지할 수 있는 상태" in blocked.message


def test_voice_resume_uses_same_attempt_and_only_requests_resume(session: Session) -> None:
    job = _job(session, status=JobStatus.PRE_ROOF_READY)
    job.control_state = ProductionJobControlState.PAUSED
    session.commit()
    attempt = _robot_attempt(session, job, control_state=ExecutionAttemptControlState.HELD)
    result = asyncio.run(_service(session, {"resume": _resume(target=job.job_id)}).handle_text(session_id="s", text="resume"))
    assert result.message == "생산 재개를 요청했습니다."
    assert job.status is JobStatus.PRE_ROOF_READY
    assert job.control_state is ProductionJobControlState.RESUME_REQUESTED
    assert attempt.status is ExecutionAttemptStatus.ACCEPTED
    assert attempt.control_state is ExecutionAttemptControlState.RESUME_REQUESTED
    duplicate = asyncio.run(_service(session, {"resume": _resume(target=job.job_id)}).handle_text(session_id="s", text="resume"))
    assert "이미 요청" in duplicate.message


def test_voice_resume_already_active_and_invalid_state_are_safe(session: Session) -> None:
    active = _job(session)
    message = asyncio.run(_service(session, {"resume": _resume(target=active.job_id)}).handle_text(session_id="s", text="resume")).message
    assert message == "생산은 이미 재개되어 있습니다."
    requested = _job(session, status=JobStatus.COMPLETED)
    blocked = asyncio.run(_service(session, {"resume-bad": _resume(target=requested.job_id)}).handle_text(session_id="s", text="resume-bad"))
    assert "재개할 수 있는 일시정지 상태" in blocked.message


def test_pause_does_not_consume_active_create_draft(session: Session) -> None:
    pending_service = PendingProductionRequestService(session)
    draft = pending_service.create_pending(session_id="draft", product_code=None, quantity=1, roof_option_code=None)
    job = _job(session)
    service = _service(session, {"생산 일시정지해줘": _pause(target=job.job_id)})
    result = asyncio.run(service.handle_text(session_id="draft", text="생산 일시정지해줘"))
    assert result.pending is not None and result.pending.request_id == draft.request_id
    assert result.pending.state.value == "COLLECTING_DETAILS"
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED


def test_conversation_route_binds_pause_to_durable_request_only(session: Session) -> None:
    job = _job(session)
    service = _service(session, {"pause": _pause(target=job.job_id)})
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        with TestClient(app) as client:
            response = client.post("/ai/conversation", json={"session_id": "route", "text": "pause"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["message"] == "생산 일시정지를 요청했습니다."
    assert job.status is JobStatus.RUNNING
    assert job.control_state is ProductionJobControlState.PAUSE_REQUESTED
