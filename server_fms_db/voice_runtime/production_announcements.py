"""Canonical PROCESS-stage announcement detection for the speaker runtime."""
from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class AnnouncementPriority(IntEnum):
    NORMAL = 1
    HIGH = 2


@dataclass(frozen=True, slots=True)
class ProductionAnnouncement:
    key: tuple[object, ...]
    text: str
    priority: AnnouncementPriority = AnnouncementPriority.NORMAL


class ProductionAnnouncementDetector:
    """Speak only server-derived PROCESS stage entry, plus independent faults.

    Redis events are wake-up identities only.  The supplied readback must carry
    the canonical process projection from the API; this class never derives a
    production phase from steps, deliveries, QA, or PRE_ROOF state.
    """

    _STAGE_TEXT_OVERRIDES = {
        "COMMAND_RECEIVED": "작업 명령이 전달되었습니다.",
        "INCOMING_QA": "수입검사를 시작합니다.",
        "PRE_ROOF_INSPECTION": "조립 결과 검사를 시작합니다.",
        "COMPLETED": "생산 작업이 완료되었습니다.",
    }
    _FAULT_TEXT = {
        "QA_FAILED": "생산 전 자재 검사에 실패했습니다. 재검사가 필요합니다.",
        "PRE_ROOF_FAILED": "지붕 설치 전 검사에 실패했습니다.",
        "CELL_FAULT": "로봇 셀 오류가 발생했습니다. 작업자 확인이 필요합니다.",
        "PAUSED": "정지되었습니다.",
        "RESUMED": "작업을 재개합니다.",
    }

    def __init__(self) -> None:
        self._spoken: set[tuple[object, ...]] = set()

    def baseline(self, snapshot: dict[str, Any]) -> None:
        """Record an already-visible stage at runtime bootstrap without speech."""
        self.observe(snapshot, emit=False)

    def observe(self, snapshot: dict[str, Any], *, emit: bool = True) -> list[ProductionAnnouncement]:
        job = snapshot.get("job") or {}
        job_id = job.get("job_id")
        if not isinstance(job_id, int) or job_id < 1:
            return []
        found: list[ProductionAnnouncement] = []

        def add(key: tuple[object, ...], text: str, priority: AnnouncementPriority = AnnouncementPriority.NORMAL) -> None:
            if key not in self._spoken:
                self._spoken.add(key)
                if emit:
                    found.append(ProductionAnnouncement(key, text, priority))

        # PROCESS is the sole normal-production authority.  A null stage for
        # FAILED/CANCELED is deliberately silent rather than replaying a prior stage.
        stage_code = job.get("process_stage_code")
        stage_name = job.get("process_stage_display_name")
        if isinstance(stage_code, str) and stage_code:
            text = self._STAGE_TEXT_OVERRIDES.get(stage_code)
            if text is None and isinstance(stage_name, str) and stage_name:
                text = f"{stage_name} 공정을 시작합니다."
            if text is not None:
                add((job_id, "PROCESS_STAGE", stage_code), text)

        # These are abnormal/control alerts, not phase interpretation.  They
        # remain independent of PROCESS so real faults are still announced.
        control = job.get("control_state")
        if control == "PAUSED":
            add((job_id, "PAUSED"), self._FAULT_TEXT["PAUSED"])
        if control == "ACTIVE" and any(
            isinstance(event, dict) and event.get("event_type") == "JOB_RESUMED"
            for event in (snapshot.get("events") or [])
        ):
            add((job_id, "RESUMED"), self._FAULT_TEXT["RESUMED"])
        qa = snapshot.get("incoming_qa") or {}
        for tx in qa.get("transactions", []) if isinstance(qa, dict) else []:
            if isinstance(tx, dict) and tx.get("status") == "COMPLETED" and tx.get("overall_result") == "FAIL":
                add((job_id, "QA_FAILED", tx.get("mode"), tx.get("cycle")), self._FAULT_TEXT["QA_FAILED"], AnnouncementPriority.HIGH)
        inspection = snapshot.get("pre_roof_inspection") or {}
        if isinstance(inspection, dict) and inspection.get("status") == "COMPLETED" and inspection.get("result") == "FAIL":
            add((job_id, "PRE_ROOF_FAILED", inspection.get("inspection_cycle")), self._FAULT_TEXT["PRE_ROOF_FAILED"], AnnouncementPriority.HIGH)
        return found

    def observe_cell_fault(self, *, job_id: int, fault_key: object) -> list[ProductionAnnouncement]:
        if job_id < 1:
            return []
        key = (job_id, "CELL_FAULT", fault_key)
        if key in self._spoken:
            return []
        self._spoken.add(key)
        return [ProductionAnnouncement(key, self._FAULT_TEXT["CELL_FAULT"], AnnouncementPriority.HIGH)]
