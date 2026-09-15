"""Project internal Forklift runtime events into the contracted Unity shape."""

from __future__ import annotations

from fms_server.forklift_runtime_state import ForkliftRuntimeEvent
from shared.schemas.production import TransportStatusData


class UnityTransportStatusProjectionService:
    """Single owner of internal transport event -> Unity data mapping."""

    @staticmethod
    def project(event: ForkliftRuntimeEvent) -> dict[str, object]:
        return TransportStatusData(
            req_id=event.state.req_id,
            job_id=event.state.job_id,
            delivery_id=event.state.delivery_id,
            robot_id=event.state.robot_id,
            task_type=event.state.task_type,
            phase=event.state.phase,
            progress=event.state.progress,
            result=event.result,
            error_code=event.error_code,
            detail=event.detail,
        ).model_dump()
