using System.Text;
using UnityEngine;
using UnityEngine.UI;

public class TaskStatusUI : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("UI")]
    [SerializeField]
    private Text processStatusText;

    private readonly string[] transportPhases =
    {
        "MOVING_TO_PICKUP",
        "DOCKING_PICKUP",
        "LIFTING_UP",
        "LEAVING_PICKUP",
        "MOVING_TO_DROPOFF",
        "DOCKING_DROPOFF",
        "LIFTING_DOWN",
        "LEAVING_DROPOFF",
        "COMPLETED"
    };

    private readonly string[] transportPhaseLabels =
    {
        "Pickup 위치로 이동",
        "Pickup ArUco 도킹",
        "리프트 상승",
        "Pickup에서 후진",
        "Dropoff 위치로 이동",
        "Dropoff ArUco 도킹",
        "리프트 하강",
        "Dropoff에서 후진",
        "운반 완료"
    };

    private void Awake()
    {
        ShowIdle();
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            Debug.LogError(
                "[TaskUI] FactoryStateManager가 " +
                "연결되지 않았습니다.",
                this);

            return;
        }

        factoryStateManager.SnapshotApplied +=
            HandleSnapshotApplied;

        factoryStateManager.TransportStatusUpdated +=
            HandleTransportStatusUpdated;

        factoryStateManager.ProductionStatusUpdated +=
            HandleProductionStatusUpdated;

        RefreshTaskScreen();
    }

    private void OnDisable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.SnapshotApplied -=
            HandleSnapshotApplied;

        factoryStateManager.TransportStatusUpdated -=
            HandleTransportStatusUpdated;

        factoryStateManager.ProductionStatusUpdated -=
            HandleProductionStatusUpdated;
    }

    private void HandleSnapshotApplied(
        ProductionSnapshotData snapshot)
    {
        RefreshTaskScreen();
    }

    private void HandleTransportStatusUpdated(
        TransportStatusData transport)
    {
        RefreshTaskScreen();
    }

    private void HandleProductionStatusUpdated(
        ProductionJobData job)
    {
        RefreshTaskScreen();
    }

    private void RefreshTaskScreen()
    {
        if (factoryStateManager == null ||
            processStatusText == null)
        {
            return;
        }

        // 진행 중인 운반 Action이 있으면 우선 표시합니다.
        if (factoryStateManager.TryGetActiveTransport(
                out TransportStatusData transport))
        {
            ShowTransportStatus(transport);
            return;
        }

        // 운반 중이 아니면 현재 Production Job을 표시합니다.
        if (factoryStateManager.TryGetActiveProductionJob(
                out ProductionJobData job))
        {
            ShowProductionStatus(job);
            return;
        }

        ShowIdle();
    }

    private void ShowTransportStatus(
        TransportStatusData transport)
    {
        if (transport.task_type == "RETURN_HOME")
        {
            ShowReturnHomeStatus(transport);
            return;
        }

        StringBuilder text = new StringBuilder();

        text.AppendLine(
            "<b><size=27>현재 작업: 재료 운반</size></b>");

        text.AppendLine();

        text.AppendLine(
            $"작업 ID          " +
            $"{DisplayValue(transport.job_id)}");

        text.AppendLine(
            $"운반 ID          " +
            $"{DisplayValue(transport.delivery_id)}");

        text.AppendLine(
            $"담당 로봇        " +
            $"{DisplayRobotName(transport.robot_id)}");

        text.AppendLine(
            $"현재 단계        " +
            $"<b>{FormatTransportPhase(transport.phase)}</b>");

        text.AppendLine(
            $"진행률            " +
            $"<color=#2878D2><b>" +
            $"{Mathf.Clamp01(transport.progress) * 100f:F0}%" +
            "</b></color>");

        if (!string.IsNullOrWhiteSpace(transport.detail))
        {
            text.AppendLine(
                $"상세 내용        {transport.detail}");
        }

        text.AppendLine();
        text.AppendLine("<b>운반 진행 단계</b>");

        int currentPhaseIndex =
            GetTransportPhaseIndex(transport.phase);

        for (int i = 0;
             i < transportPhaseLabels.Length;
             i++)
        {
            string phaseState;

            if (currentPhaseIndex < 0)
            {
                phaseState = "[대기]";
            }
            else if (i < currentPhaseIndex)
            {
                phaseState =
                    "<color=#2AAA5D>[완료]</color>";
            }
            else if (i == currentPhaseIndex)
            {
                phaseState =
                    "<color=#2878D2><b>[진행]</b></color>";
            }
            else
            {
                phaseState = "[대기]";
            }

            text.AppendLine(
                $"{phaseState}  {transportPhaseLabels[i]}");
        }

        AppendTransportResult(text, transport);

        processStatusText.text = text.ToString();
    }

    private void ShowReturnHomeStatus(
        TransportStatusData transport)
    {
        StringBuilder text = new StringBuilder();

        text.AppendLine(
            "<b><size=27>현재 작업: 포크리프트 복귀</size></b>");

        text.AppendLine();

        text.AppendLine(
            $"요청 ID          {transport.req_id}");

        text.AppendLine(
            $"담당 로봇        " +
            $"{DisplayRobotName(transport.robot_id)}");

        text.AppendLine(
            $"현재 단계        {FormatReturnHomePhase(transport.phase)}");

        text.AppendLine(
            $"진행률            " +
            $"<color=#2878D2><b>" +
            $"{Mathf.Clamp01(transport.progress) * 100f:F0}%" +
            "</b></color>");

        if (!string.IsNullOrWhiteSpace(transport.detail))
        {
            text.AppendLine(
                $"상세 내용        {transport.detail}");
        }

        AppendTransportResult(text, transport);

        processStatusText.text = text.ToString();
    }

    private void ShowProductionStatus(
        ProductionJobData job)
    {
        string processName = GetProcessName(job);

        StringBuilder text = new StringBuilder();

        text.AppendLine(
            $"<b><size=27>현재 작업: {processName}</size></b>");

        text.AppendLine();

        text.AppendLine(
            $"작업 ID          {DisplayValue(job.job_id)}");

        text.AppendLine(
            $"제품             {DisplayValue(job.product)}");

        text.AppendLine(
            $"현재 단계        " +
            $"<b>{DisplayStep(job.current_operation, job.current_step_display_name)}</b>");

        text.AppendLine(
            $"단계 상태        " +
            $"{FormatStepStatus(EffectiveStepStatus(job))}");

        text.AppendLine(
            $"다음 단계        " +
            $"{DisplayStep(job.next_operation, job.next_step_display_name)}");

        string controlState = FormatControlState(job.control_state);
        if (!string.IsNullOrWhiteSpace(controlState))
        {
            text.AppendLine(
                $"제어 상태        <color=#EB9B2D><b>{controlState}</b></color>");
        }

        text.AppendLine(
            $"진행률            " +
            $"<color=#2878D2><b>" +
            $"{Mathf.Clamp01(job.progress) * 100f:F0}%" +
            "</b></color>");

        if (job.ready)
        {
            text.AppendLine(
                "작업 준비 상태   " +
                "<color=#2AAA5D>준비 완료</color>");
        }
        else
        {
            text.AppendLine(
                "작업 준비 상태   " +
                "<color=#EB9B2D>대기</color>");

            text.AppendLine(
                $"대기 사유        " +
                $"{DisplayValue(job.readiness_reason)}");
        }

        if (!string.IsNullOrWhiteSpace(job.error_code))
        {
            text.AppendLine(
                $"오류             " +
                $"<color=#D74141>{job.error_code}</color>");
        }

        AppendProductionGuide(text, processName, job);

        processStatusText.text = text.ToString();
    }

    private void AppendProductionGuide(
        StringBuilder text,
        string processName,
        ProductionJobData job)
    {
        text.AppendLine();

        if (processName == "집 조립")
        {
            text.AppendLine("<b>집 조립 진행 정보</b>");
            text.AppendLine(
                $"[현재] {DisplayValue(job.current_operation)}");
            text.AppendLine(
                $"[다음] {DisplayValue(job.next_operation)}");
        }
        else if (processName == "완성 주택 배치")
        {
            text.AppendLine("<b>완성 주택 배치 정보</b>");
            text.AppendLine(
                $"[현재] {DisplayValue(job.current_operation)}");
            text.AppendLine(
                $"[다음] {DisplayValue(job.next_operation)}");
        }
        else if (processName == "재료 운반")
        {
            text.AppendLine("<b>재료 공급 정보</b>");
            text.AppendLine(
                $"[현재] {DisplayValue(job.current_operation)}");
            text.AppendLine(
                $"[다음] {DisplayValue(job.next_operation)}");
        }
    }

    private void AppendTransportResult(
        StringBuilder text,
        TransportStatusData transport)
    {
        if (string.IsNullOrWhiteSpace(transport.result))
        {
            return;
        }

        text.AppendLine();

        switch (transport.result)
        {
            case "SUCCEEDED":

                text.AppendLine(
                    "결과             " +
                    "<color=#2AAA5D><b>완료</b></color>");
                break;

            case "FAILED":

                text.AppendLine(
                    "결과             " +
                    "<color=#D74141><b>실패</b></color>");
                break;

            case "CANCELED":

                text.AppendLine(
                    "결과             " +
                    "<color=#919BA5><b>취소</b></color>");
                break;

            default:

                text.AppendLine(
                    $"결과             {transport.result}");
                break;
        }

        if (!string.IsNullOrWhiteSpace(transport.error_code))
        {
            text.AppendLine(
                $"오류 코드        " +
                $"<color=#D74141>" +
                $"{transport.error_code}</color>");
        }
    }

    private int GetTransportPhaseIndex(string phase)
    {
        for (int i = 0; i < transportPhases.Length; i++)
        {
            if (transportPhases[i] == phase)
            {
                return i;
            }
        }

        return -1;
    }

    private string FormatTransportPhase(string phase)
    {
        int index = GetTransportPhaseIndex(phase);

        return index >= 0
            ? transportPhaseLabels[index]
            : DisplayValue(phase);
    }

    private string FormatReturnHomePhase(string phase)
    {
        switch (phase)
        {
            case "RETURNING_HOME":
                return "HOME 위치로 복귀 중";
            case "PARKING_LIFT":
                return "리프트 주차 중";
            case "COMPLETED":
                return "HOME 복귀 완료";
            default:
                return DisplayValue(phase);
        }
    }

    private string GetProcessName(ProductionJobData job)
    {
        if (job.job_status == "COMPLETED")
        {
            return "완료";
        }

        string operation =
            job.current_operation ?? string.Empty;

        if (operation == "MOVE_COMPLETED_HOUSE" ||
            operation.Contains("HOUSE_PLACEMENT") ||
            operation.Contains("PLACE_HOUSE") ||
            operation.Contains("COMPLETED_HOUSE"))
        {
            return "FR5 완성 주택 운반";
        }

        if (FactoryOperationCatalog.TryGetStep(operation, out int step))
        {
            return FactoryOperationCatalog.StepNames[step];
        }

        // 모르는 Operation은 서버 문자열 그대로 표시
        return DisplayValue(operation);
    }

    private string FormatStepStatus(string status)
    {
        switch (status)
        {
            case "RUNNING":
            case "IN_PROGRESS":
                return "진행 중";

            case "WAITING":
            case "PENDING":
                return "대기";

            case "COMPLETED":
                return "완료";

            case "PRE_ROOF_READY":
                return "지붕 설치 전 검사";

            case "ROOF_READY":
                return "지붕 설치 준비";

            case "FAILED":
                return "실패";

            default:
                return DisplayValue(status);
        }
    }

    private static string EffectiveStepStatus(ProductionJobData job)
    {
        if (!string.IsNullOrWhiteSpace(job.current_step_status))
        {
            return job.current_step_status;
        }

        return !string.IsNullOrWhiteSpace(job.next_step_id)
            ? "PENDING"
            : job.job_status;
    }

    private static string DisplayStep(
        string operation,
        string serverDisplayName)
    {
        if (FactoryOperationCatalog.TryGetStep(operation, out int step))
        {
            return FactoryOperationCatalog.StepNames[step]
                .Replace("\n", " / ");
        }

        if (!string.IsNullOrWhiteSpace(serverDisplayName))
        {
            return serverDisplayName;
        }

        return string.IsNullOrWhiteSpace(operation) ? "-" : operation;
    }

    private static string FormatControlState(string controlState)
    {
        switch ((controlState ?? string.Empty).Trim().ToUpperInvariant())
        {
            case "PAUSE_REQUESTED":
                return "일시정지 요청 중";
            case "PAUSED":
                return "일시정지";
            case "RESUME_REQUESTED":
                return "재개 요청 중";
            default:
                return null;
        }
    }

    private string DisplayRobotName(string robotId)
    {
        switch (robotId)
        {
            case "forklift_01":
            case "turtlebot_01":
                return "포크리프트 01";

            case "fr5":
                return "FR5";

            case "zkbot1":
                return "ZKKeep 01";

            case "zkbot2":
                return "ZKKeep 02";


            default:
                return DisplayValue(robotId);
        }
    }

    private void ShowIdle()
    {
        if (processStatusText == null)
        {
            return;
        }

        processStatusText.text =
            "<b><size=27>현재 작업</size></b>\n\n" +
            "현재 공정        대기\n" +
            "현재 단계        -\n" +
            "진행률           0%\n" +
            "상태             진행 중인 작업 없음";
    }

    private string DisplayValue(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? "-"
            : value;
    }
}
