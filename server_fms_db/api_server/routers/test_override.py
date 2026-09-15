"""Explicitly guarded test-only production blocker commands."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from api_server.routers.inventory import get_db
from shared.services.test_override_service import (
    CurrentBlockerType,
    TestOverrideDeniedError,
    TestOverrideService,
    TestOverrideStaleError,
)


router = APIRouter(prefix="/test", tags=["test-override"])


class AdvanceCurrentBlockerRequest(BaseModel):
    expected_blocker_type: CurrentBlockerType
    expected_entity_id: int = Field(gt=0)

    model_config = ConfigDict(extra="forbid")


class CurrentBlockerResponse(BaseModel):
    blocker_type: CurrentBlockerType
    entity_id: int
    detail: str | None = None


class TerminalDropCleanupResponse(BaseModel):
    job_delivery_id: int
    attempt_id: int | None
    already_recovered: bool
    status: str = "SUCCESS"


class AdvanceCurrentBlockerResponse(BaseModel):
    advanced_blocker_type: CurrentBlockerType
    entity_id: int
    next_blocker_type: CurrentBlockerType | None
    next_entity_id: int | None
    status: str = "SUCCESS"


class HouseOutboundCompletionResponse(BaseModel):
    job_id: int
    status: str
    already_completed: bool

def _service(db: Session) -> TestOverrideService:
    return TestOverrideService(db)


@router.get("/jobs/{job_id}/current-blocker", response_model=CurrentBlockerResponse | None)
def current_blocker(job_id: int, db: Annotated[Session, Depends(get_db)]):
    try:
        service = _service(db)
        service.require_capability()
        blocker = service.current_blocker(job_id=job_id)
    except TestOverrideDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return None if blocker is None else CurrentBlockerResponse(
        blocker_type=blocker.blocker_type, entity_id=blocker.entity_id, detail=blocker.detail
    )


@router.post(
    "/jobs/{job_id}/material-deliveries/{job_delivery_id}/terminal-drop-cleanup",
    response_model=TerminalDropCleanupResponse,
)
def terminal_drop_cleanup_for_test_override(
    job_id: int,
    job_delivery_id: int,
    db: Annotated[Session, Depends(get_db)],
) -> TerminalDropCleanupResponse:
    try:
        result = _service(db).recover_terminal_drop_for_test_override(
            job_id=job_id, job_delivery_id=job_delivery_id
        )
    except TestOverrideDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except TestOverrideStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return TerminalDropCleanupResponse(
        job_delivery_id=result.job_delivery_id,
        attempt_id=result.attempt_id,
        already_recovered=result.already_recovered,
    )


@router.post("/jobs/{job_id}/house-outbound/complete", response_model=HouseOutboundCompletionResponse)
def complete_house_outbound_for_test_override(
    job_id: int, db: Annotated[Session, Depends(get_db)]
) -> HouseOutboundCompletionResponse:
    try:
        service = _service(db)
        service.require_capability()
        before = service._job(job_id, lock=False)
        already_completed = before.status.value == "COMPLETED"
        job = service.complete_house_outbound_for_test_override(job_id=job_id)
    except TestOverrideDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except TestOverrideStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return HouseOutboundCompletionResponse(
        job_id=job.job_id, status=job.status.value, already_completed=already_completed
    )

@router.post("/jobs/{job_id}/start-current-blocker", response_model=AdvanceCurrentBlockerResponse)
def start_current_blocker(
    job_id: int,
    command: AdvanceCurrentBlockerRequest,
    db: Annotated[Session, Depends(get_db)],
) -> AdvanceCurrentBlockerResponse:
    try:
        result = _service(db).start_current_robot_cell_blocker(
            job_id=job_id,
            expected_blocker_type=command.expected_blocker_type,
            expected_entity_id=command.expected_entity_id,
        )
    except TestOverrideDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except TestOverrideStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AdvanceCurrentBlockerResponse(
        advanced_blocker_type=result.advanced.blocker_type,
        entity_id=result.advanced.entity_id,
        next_blocker_type=None if result.next_blocker is None else result.next_blocker.blocker_type,
        next_entity_id=None if result.next_blocker is None else result.next_blocker.entity_id,
    )


@router.post("/jobs/{job_id}/advance-current-blocker", response_model=AdvanceCurrentBlockerResponse)
def advance_current_blocker(
    job_id: int,
    command: AdvanceCurrentBlockerRequest,
    db: Annotated[Session, Depends(get_db)],
) -> AdvanceCurrentBlockerResponse:
    try:
        result = _service(db).advance(
            job_id=job_id,
            expected_blocker_type=command.expected_blocker_type,
            expected_entity_id=command.expected_entity_id,
        )
    except TestOverrideDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except TestOverrideStaleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AdvanceCurrentBlockerResponse(
        advanced_blocker_type=result.advanced.blocker_type,
        entity_id=result.advanced.entity_id,
        next_blocker_type=None if result.next_blocker is None else result.next_blocker.blocker_type,
        next_entity_id=None if result.next_blocker is None else result.next_blocker.entity_id,
    )
