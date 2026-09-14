from __future__ import annotations

import asyncio

import pytest

from voice_runtime.production_announcements import (
    AnnouncementPriority,
    ProductionAnnouncementDetector,
)
from voice_runtime.production_event_subscriber import ProductionEventAnnouncementSubscriber


def _snapshot(
    *,
    job_id: int = 17,
    code: str | None = "OUTER_WALL_INSTALL",
    name: str | None = "FR5 외벽 설치",
    status: str = "RUNNING",
    control: str = "ACTIVE",
    events=None,
    qa=None,
    inspection=None,
):
    return {
        "job": {
            "job_id": job_id,
            "status": status,
            "control_state": control,
            "process_stage_code": code,
            "process_stage_display_name": name,
        },
        "events": events or [],
        "incoming_qa": qa or {},
        "pre_roof_inspection": inspection or {},
        # Deliberately contradictory legacy detail: normal stage output must
        # not inspect it after canonical projection is available.
        "steps": [{"operation_code": "INSTALL_ROOF", "status": "RUNNING"}],
        "deliveries": [{"supply_group_code": "OUTER_WALLS", "status": "COMPLETED"}],
    }


def _texts(items):
    return [item.text for item in items]


@pytest.mark.parametrize(
    ("code", "name", "expected"),
    [
        ("COMMAND_RECEIVED", "작업 명령 전달", "작업 명령이 전달되었습니다."),
        ("INCOMING_QA", "수입검사", "수입검사를 시작합니다."),
        ("BASE_INSTALL", "Zekeep 베이스 설치", "Zekeep 베이스 설치 공정을 시작합니다."),
        ("OUTER_WALL_DELIVERY", "외벽 팔레트 운반", "외벽 팔레트 운반 공정을 시작합니다."),
        ("OUTER_WALL_INSTALL", "FR5 외벽 설치", "FR5 외벽 설치 공정을 시작합니다."),
        ("OUTER_RETURN_INNER_DELIVERY", "외벽 빈 팔레트 회수 / 내벽 팔레트 운반", "외벽 빈 팔레트 회수 / 내벽 팔레트 운반 공정을 시작합니다."),
        ("INNER_WALL_INSTALL", "FR5 내벽 설치", "FR5 내벽 설치 공정을 시작합니다."),
        ("INNER_WALL_RETURN", "내벽 빈 팔레트 회수", "내벽 빈 팔레트 회수 공정을 시작합니다."),
        ("PRE_ROOF_INSPECTION", "조립 결과 검사", "조립 결과 검사를 시작합니다."),
        ("ROOF_INSTALL", "Zekeep 지붕 설치", "Zekeep 지붕 설치 공정을 시작합니다."),
        ("HOUSE_OUTBOUND", "FR5 완성 주택 운반", "FR5 완성 주택 운반 공정을 시작합니다."),
        ("COMPLETED", "작업 완료", "생산 작업이 완료되었습니다."),
    ],
)
def test_each_canonical_process_stage_is_announced_once(code: str, name: str, expected: str) -> None:
    detector = ProductionAnnouncementDetector()
    snapshot = _snapshot(code=code, name=name, status="COMPLETED" if code == "COMPLETED" else "RUNNING")
    assert _texts(detector.observe(snapshot)) == [expected]
    assert detector.observe(snapshot) == []


def test_multiple_detail_events_inside_outer_stage_speak_once() -> None:
    detector = ProductionAnnouncementDetector()
    snapshots = [_snapshot(code="OUTER_WALL_INSTALL", name="FR5 외벽 설치") for _ in range(4)]
    assert _texts(detector.observe(snapshots[0])) == ["FR5 외벽 설치 공정을 시작합니다."]
    assert all(detector.observe(snapshot) == [] for snapshot in snapshots[1:])


def test_return_and_inner_delivery_detail_changes_share_one_stage_speech() -> None:
    detector = ProductionAnnouncementDetector()
    for index in range(4):
        snapshot = _snapshot(code="OUTER_RETURN_INNER_DELIVERY", name="외벽 빈 팔레트 회수 / 내벽 팔레트 운반")
        if index == 0:
            assert _texts(detector.observe(snapshot)) == ["외벽 빈 팔레트 회수 / 내벽 팔레트 운반 공정을 시작합니다."]
        else:
            assert detector.observe(snapshot) == []


def test_pre_roof_per_view_updates_do_not_repeat_process_announcement() -> None:
    detector = ProductionAnnouncementDetector()
    for index, view in enumerate(("TOP", "LEFT", "RIGHT", "FRONT", "BEHIND")):
        snapshot = _snapshot(code="PRE_ROOF_INSPECTION", name="조립 결과 검사", inspection={"current_view": view})
        if index == 0:
            assert _texts(detector.observe(snapshot)) == ["조립 결과 검사를 시작합니다."]
        else:
            assert detector.observe(snapshot) == []


def test_stage_change_generates_new_announcement_with_server_display_name() -> None:
    detector = ProductionAnnouncementDetector()
    assert _texts(detector.observe(_snapshot(code="BASE_INSTALL", name="Zekeep 베이스 설치"))) == ["Zekeep 베이스 설치 공정을 시작합니다."]
    assert _texts(detector.observe(_snapshot(code="OUTER_WALL_DELIVERY", name="외벽 팔레트 운반"))) == ["외벽 팔레트 운반 공정을 시작합니다."]


def test_null_failed_or_canceled_stage_never_replays_normal_process() -> None:
    detector = ProductionAnnouncementDetector()
    assert detector.observe(_snapshot(code=None, name=None, status="FAILED")) == []
    assert detector.observe(_snapshot(code=None, name=None, status="CANCELED")) == []


def test_fault_and_control_alerts_remain_independent_of_process_stage() -> None:
    detector = ProductionAnnouncementDetector()
    failed = _snapshot(code=None, name=None, qa={"transactions": [{"status": "COMPLETED", "overall_result": "FAIL", "mode": "BASE_AB", "cycle": 1}]})
    alerts = detector.observe(failed)
    assert _texts(alerts) == ["생산 전 자재 검사에 실패했습니다. 재검사가 필요합니다."]
    assert alerts[0].priority is AnnouncementPriority.HIGH
    assert _texts(detector.observe(_snapshot(code=None, name=None, control="PAUSED"))) == ["정지되었습니다."]
    assert _texts(detector.observe(_snapshot(code=None, name=None, events=[{"event_type": "JOB_RESUMED"}]))) == ["작업을 재개합니다."]
    assert _texts(detector.observe_cell_fault(job_id=17, fault_key=100)) == ["로봇 셀 오류가 발생했습니다. 작업자 확인이 필요합니다."]


def test_restart_baseline_suppresses_current_stage_but_new_job_still_announces_stage_one() -> None:
    detector = ProductionAnnouncementDetector()
    current = _snapshot(job_id=17, code="OUTER_WALL_INSTALL", name="FR5 외벽 설치")
    detector.baseline(current)
    assert detector.observe(current) == []
    assert _texts(detector.observe(_snapshot(job_id=18, code="COMMAND_RECEIVED", name="작업 명령 전달"))) == ["작업 명령이 전달되었습니다."]


async def _subscriber_case() -> None:
    received = []

    class Queue:
        def enqueue(self, text, priority):
            received.append((text, priority))

    snapshots = {17: _snapshot(code="INNER_WALL_RETURN", name="내벽 빈 팔레트 회수")}

    async def reader(job_id):
        return snapshots.get(job_id)

    subscriber = ProductionEventAnnouncementSubscriber(snapshot_reader=reader, queue=Queue())
    await subscriber.observe_job(17)
    await subscriber.observe_job(17)
    assert [text for text, _priority in received] == ["내벽 빈 팔레트 회수 공정을 시작합니다."]


def test_subscriber_repeated_redis_identity_triggers_dedupe_by_process_stage() -> None:
    asyncio.run(_subscriber_case())


async def _subscriber_restart_case() -> None:
    received = []

    class Queue:
        def enqueue(self, text, priority):
            received.append(text)

    snapshots = {
        17: _snapshot(job_id=17, code="ROOF_INSTALL", name="Zekeep 지붕 설치"),
        21: _snapshot(job_id=21, code="COMMAND_RECEIVED", name="작업 명령 전달"),
    }

    async def reader(job_id):
        return snapshots.get(job_id)

    async def jobs():
        return [
            {"job_id": 17, "requested_at": "2020-01-01T00:00:00Z"},
            {"job_id": 21, "requested_at": "2999-01-01T00:00:00Z"},
        ]

    subscriber = ProductionEventAnnouncementSubscriber(snapshot_reader=reader, queue=Queue(), job_ids_reader=jobs)
    await subscriber._bootstrap_existing_jobs()
    await subscriber.observe_job(17)
    await subscriber.observe_job(21)
    assert received == ["작업 명령이 전달되었습니다."]


def test_subscriber_restart_baselines_existing_stage_and_announces_new_job() -> None:
    asyncio.run(_subscriber_restart_case())
