from pathlib import Path

from fastapi.testclient import TestClient
from api_server.main import app

def test_production_monitor_page_is_served():
    with TestClient(app) as client:
        page = client.get("/production/monitor")

    assert page.status_code == 200
    assert "FMS Production Monitor" in page.text
    assert "job-detail-container" in page.text
    assert "execution-snapshot-container" in page.text
    assert "material-deliveries-container" in page.text
    assert "incoming-qa-v02-container" in page.text
    assert "/production/jobs/${selectedJobId}/incoming-qa/v02" in page.text
    assert "fetchIncomingQAV02" in page.text
    assert "renderIncomingQAV02" in page.text
    assert "Incoming QA v0.2" in page.text
    assert "incoming-qa-v02-action" in page.text
    assert "incoming-qa-v02-feedback" in page.text
    assert "incomingQAV02InitialStartControl" in page.text
    assert "incomingQATestHoldControls" in page.text
    assert "Vision QA 테스트 Hold" in page.text
    assert "can_enable_incoming_qa_test_hold" in page.text
    assert "can_advance_house_b" in page.text
    assert "can_release_incoming_qa_test_hold" in page.text
    assert "/incoming-qa/v02/test-hold" in page.text
    assert "/incoming-qa/v02/advance-house-b" in page.text
    assert "HOUSE_B 검사 진행" in page.text
    assert "생산 진행" in page.text
    assert "입고검사 자재 준비 완료" in page.text
    assert "Vision 검사 위치에 이 Job의 검사 대상 자재를 모두 배치" in page.text
    assert "confirmIncomingQAV02InitialStart" in page.text
    assert "window.confirm" in page.text
    assert "/incoming-qa/v02/start" in page.text
    assert "incomingQAV02StartInFlight" in page.text
    assert "transactions.length > 0" in page.text
    assert "terminalStatuses" in page.text
    assert "입고검사 요청이 생성되었습니다. Transaction 상태를 확인하세요." in page.text
    assert "result.created_transaction_ids.length === 0" in page.text
    assert "입고검사 요청이 생성되지 않았습니다." in page.text
    assert "await fetchIncomingQAV02()" in page.text
    assert "voice-runtime-container" in page.text
    assert "/ai/debug/voice-runtime" in page.text
    assert "fetchVoiceRuntime" in page.text
    assert "Voice Command" in page.text
    assert "/production/jobs/${selectedJobId}/execution-snapshot" in page.text
    assert "/production/jobs/${selectedJobId}/material-deliveries" in page.text
    assert "fetchExecutionSnapshot" in page.text
    assert "fetchMaterialDeliveries" in page.text
    assert "fetchReadOnlyWithTimeout" in page.text
    assert "method: 'GET'" in page.text
    assert "clearTimeout(timeoutId)" in page.text
    assert "qaStateLabel" in page.text
    assert "transportStatusLabel" in page.text
    assert "QA policy undecided" in page.text
    assert "Request/send issue:" in page.text
    for state_label in [
        "Not requested", "Requested", "Running", "Released",
        "Failed", "Error", "Not evaluated", "Not released for production",
    ]:
        assert state_label in page.text
    for transport_label in [
        "Transport pending", "Transport in progress", "Transport completed", "Transport failed",
    ]:
        assert transport_label in page.text
    assert "Waiting for preparation" in page.text
    assert "Prepared" in page.text
    assert "is_delivered" not in page.text
    assert "renderMaterialIncomingQAGate" in page.text
    assert "Incoming QA Gate:" in page.text
    assert "Released:" in page.text
    assert "Inspection complete:" in page.text
    assert "deliveryItemsAreReleased" in page.text
    assert "item.qa_released === true" in page.text
    assert "입고검사 통과 후 사용할 수 있습니다." in page.text
    assert "shouldShowPhysicalReadyCommand" in page.text
    assert "delivery.supply_mode === 'TRANSPORTED'" in page.text
    assert "delivery.status === 'PENDING'" in page.text
    assert "delivery.physical_ready === false" in page.text
    assert "const qaReleased = deliveryItemsAreReleased(delivery);" in page.text
    assert "팔레트 준비 완료" in page.text
    assert "createPhysicalReadyRequestId" in page.text
    assert "crypto.randomUUID" in page.text
    assert "postJsonWithTimeout" in page.text
    assert "/physical-ready" in page.text
    assert "{ request_id: requestId }" in page.text
    assert "/material-deliveries/${deliveryId}/physical-ready" in page.text
    assert "physicalReadyCommandsInFlight" in page.text
    assert "setPhysicalReadyButtonInFlight" in page.text
    assert "refreshMaterialDeliveriesAfterCommand" in page.text
    assert "shouldShowManualPrestageCommand" in page.text
    assert "delivery.supply_mode === 'MANUAL'" in page.text
    assert "delivery.manual_prestage_ready === false" in page.text
    assert "수동 셋업 완료" in page.text
    assert "confirmManualPrestage" in page.text
    assert "manualPrestageCommandsInFlight" in page.text
    assert "setManualPrestageButtonInFlight" in page.text
    assert "/manual-prestage-ready" in page.text
    assert "Robot Cell 작업 위치에 실제 배치" in page.text
    assert "manual_prestage_ready_at" in page.text
    assert "manual_prestage_ready" in page.text
    assert "shouldShowOperatorExecutionReady" in page.text
    assert "OPERATOR_EXECUTION_READY_REQUIRED" in page.text
    assert "/steps/${stepId}/execution-ready" in page.text
    assert "TurtleBot이 작업영역에서 이탈" in page.text
    assert "operator_execution_ready_at" in page.text
    assert "외벽 연속 조립 시작" in page.text
    assert "can_start_outer_wall_batch" in page.text
    assert "/outer-walls/operator-ready" in page.text
    assert "각 외벽은 기존 공정 순서대로 하나씩 실행됩니다" in page.text
    assert "empty-pallet-return" in page.text
    assert "빈 팔레트 확인 및 반환" in page.text
    assert "shouldShowEmptyReturnCommand" in page.text
    assert "delivery.can_empty_pallet_return === true" in page.text
    assert "completedStepIds" not in page.text
    assert "emptyReturnCommandsInFlight" in page.text
    assert "confirmed_empty: true" in page.text
    assert "실제 팔레트가 비어 있는 것을 확인" in page.text
    assert "terminalDropCleanupButtonHtml" in page.text
    assert "can_terminal_drop_cleanup" in page.text
    assert "TEST MODE · 빈 팔레트 반환 시뮬레이션" in page.text
    assert "/terminal-drop-cleanup" in page.text
    assert "shouldShowIncomingQAStartCommand" not in page.text
    assert "incomingQAStartButtonHtml" not in page.text
    assert "startIncomingQA" not in page.text
    assert "/items/${deliveryItemId}/incoming-qa/start" not in page.text
    assert "/items/${deliveryItemId}/incoming-qa/reinspect" not in page.text
    assert "shouldShowIncomingQAReinspectionCommand" in page.text
    assert "incomingQAReinspectionButtonHtml" in page.text
    assert "reinspectIncomingQAV02" in page.text
    assert "재검사 요청" in page.text
    assert "/incoming-qa/v02/reinspect" in page.text
    assert "delivery_item_ids: [deliveryItemId]" in page.text
    assert "item.qa_state === 'FAILED'" in page.text
    assert "item.qa_state === 'NOT_EVALUATED'" in page.text
    assert "item.qa_state === 'ERROR'" in page.text
    assert "transaction.error_reason || transaction.ack_reason_code" in page.text
    assert "production-request-input" in page.text
    assert "수동 생산 요청" in page.text
    assert "생산 요청 보내기" in page.text
    assert "POST /ai/conversation" not in page.text
    assert "'/ai/conversation'" in page.text
    assert "submitProductionConversation" in page.text
    assert "getProductionConversationSessionId" in page.text
    assert "session_id: getProductionConversationSessionId()" in page.text
    assert "text: normalizedText" in page.text
    assert "생산 확정" in page.text
    assert "submitProductionConversation('네')" in page.text
    assert "submitProductionConversation('아니')" in page.text
    assert "production_job_ids" in page.text
    assert "await refreshData()" in page.text
    assert "productionConversationInFlight" in page.text
    assert "HOUSE_B Job 생성" not in page.text
    assert "검사 전체 시작" not in page.text
    assert "TurtleBot을 즉시 출발시키지 않으며" in page.text
    assert "Robot Cell Dispatch" in page.text
    assert "last_step_execution_event" in page.text
    assert "preRoofControlsHtml" in page.text
    assert "runPreRoofCommand" in page.text
    assert "/pre-roof/${action}" in page.text
    assert "품질검사 시작" in page.text
    assert "재검사 시작" in page.text
    assert "현장 조치 후 재검사가 필요합니다." in page.text
    assert "Production Ready" in page.text
    assert "Robot Cell Dispatch" in page.text
    assert "snapshot.next_step" in page.text
    assert "ROBOT_CELL_CONTRACT_BLOCKED" not in page.text
    assert "if (value === null || value === undefined || value === '') return '-';" in page.text
    assert "Execution state inconsistency" in page.text
    assert "setInterval(refreshData, 3000)" in page.text
    assert "setInterval(fetchMaterialDeliveries" not in page.text
    assert "execute_step" not in page.text
    assert "dispatch_step" not in page.text
    # The existing Test Override UI enables completion from a backend Robot
    # blocker; the FMS-off service regression makes a ready first step return it.
    assert "/test/jobs/${selectedJobId}/current-blocker" in page.text
    assert "ROBOT_CELL_STEP_EXECUTION" in page.text
    assert "/start-current-blocker" in page.text
    assert "/advance-current-blocker" in page.text
    assert "testOverrideAdvance.disabled = !synthetic.has(testOverrideBlocker.blocker_type)" in page.text


def test_incoming_qa_mode_reinspection_controls_use_backend_eligibility_and_mode_route():
    page = (Path(__file__).resolve().parents[1] / "api_server/static/production_monitor.html").read_text()
    assert "transaction.can_reinspect_mode" in page
    assert "incomingQAModeReinspectionButtonHtml" in page
    assert "confirmIncomingQAModeReinspection" in page
    assert "BASE_AB 재검사" in page
    assert "HOUSE_B 재검사" in page
    assert "기존 검사 결과는 이력으로 유지됩니다" in page
    assert "/incoming-qa/v02/${inspectionMode}/reinspect" in page


def test_pre_roof_monitor_controls_follow_wire_authority():
    page = (Path(__file__).resolve().parents[1] / "api_server/static/production_monitor.html").read_text()
    assert "inspection.status === 'COMPLETED' && inspection.result !== 'PASS'" in page
    assert "Vision View 검사 진행 중입니다." in page
    assert "function preRoofViewsHtml(inspection)" in page
    assert "Current View" in page and "inspection.gate_state" in page
    assert "runPreRoofCommand(${escapeHtml(snapshot.job_id)}, 'pass')" not in page
    assert "runPreRoofCommand(${escapeHtml(snapshot.job_id)}, 'fail')" not in page


def test_monitor_selected_job_cancel_and_terminal_mutation_lock_are_present():
    page = (Path(__file__).resolve().parents[1] / "api_server/static/production_monitor.html").read_text()
    assert 'id="selected-job-cancel-button"' in page
    assert "작업 취소" in page
    assert "confirmSelectedJobCancel" in page
    assert "/production/jobs/" in page and "/cancel" in page
    assert "생산 작업을 취소하시겠습니까?" in page
    assert "terminalJobStatuses" in page
    assert "isCurrentJobTerminal" in page
    assert "!isCurrentJobTerminal()" in page
    assert "종료된 Job은 읽기 전용입니다." in page


def test_incoming_qa_monitor_labels_runtime_authorization_separately_from_item_pass() -> None:
    page = (Path(__file__).resolve().parents[1] / "api_server/static/production_monitor.html").read_text()
    assert "Vision Runtime Authorization" in page
    assert "Inspection PASS" in page
    assert "<th>Production Valid</th>" not in page


def test_house_outbound_override_visibility_uses_canonical_process_stage_only():
    page = (Path(__file__).resolve().parents[1] / "api_server/static/production_monitor.html").read_text()
    start = page.index("function isHouseOutboundCheckpoint()")
    end = page.index("async function fetchTestOverrideBlocker", start)
    condition = page[start:end]
    assert "job.process_stage_code === 'HOUSE_OUTBOUND'" in condition
    assert "!isTerminalJob(job)" in condition
    assert "job.status === 'RUNNING'" not in condition
    assert "Array.isArray(job.steps)" not in condition
    assert 'id="test-override-house-outbound"' in page
    assert "/test/jobs/${currentJobId}/house-outbound/complete" in page
