"""Shared durable guard for physical TurtleBot movements."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.factory import ExecutionAttempt, ExecutionAttemptStatus, ExecutorType
from shared.services.drop_resource_service import (
    EMPTY_RETURN_COMMAND_TYPE,
    MATERIAL_TRANSPORT_COMMAND_TYPE,
)

RETURN_HOME_COMMAND_TYPE = "RETURN_HOME"
ACTIVE_FORKLIFT_MOVEMENT_STATUSES = (
    ExecutionAttemptStatus.CREATED,
    ExecutionAttemptStatus.DISPATCHING,
    ExecutionAttemptStatus.ACCEPTED,
    ExecutionAttemptStatus.UNKNOWN,
)
FORKLIFT_PHYSICAL_COMMAND_TYPES = (
    MATERIAL_TRANSPORT_COMMAND_TYPE,
    EMPTY_RETURN_COMMAND_TYPE,
    RETURN_HOME_COMMAND_TYPE,
)


def has_active_forklift_movement(session: Session, *, for_update: bool = False) -> bool:
    """Return whether a durable physical TurtleBot movement is unresolved."""

    statement = select(ExecutionAttempt.attempt_id).where(
        ExecutionAttempt.executor_type == ExecutorType.FORKLIFT,
        ExecutionAttempt.command_type.in_(FORKLIFT_PHYSICAL_COMMAND_TYPES),
        ExecutionAttempt.status.in_(ACTIVE_FORKLIFT_MOVEMENT_STATUSES),
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement.limit(1)) is not None
