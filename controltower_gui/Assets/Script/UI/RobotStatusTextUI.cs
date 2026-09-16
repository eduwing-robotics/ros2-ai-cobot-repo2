using System.Text;
using UnityEngine;
using UnityEngine.UI;

public class RobotStatusTextUI : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("Robot")]
    [SerializeField]
    private string robotId = "forklift_01";

    [SerializeField]
    private string displayName = "forklift";

    [Header("UI")]
    [SerializeField]
    private Text statusText;

    private void Awake()
    {
        robotId = FactoryStateManager.CanonicalizeRobotId(robotId);
        if (robotId == "forklift_01" || robotId == "turtlebot_01")
            displayName = "forklift";
        ShowNoData();
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            Debug.LogError(
                $"[{robotId} UI] FactoryStateManager가 연결되지 않았습니다.",
                this);

            return;
        }

        factoryStateManager.SnapshotApplied +=
            HandleSnapshotApplied;

        factoryStateManager.RobotStatusUpdated +=
            HandleRobotStatusUpdated;

        RefreshCurrentStatus();
    }

    private void OnDisable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.SnapshotApplied -=
            HandleSnapshotApplied;

        factoryStateManager.RobotStatusUpdated -=
            HandleRobotStatusUpdated;
    }

    private void HandleSnapshotApplied(
        ProductionSnapshotData snapshot)
    {
        RefreshCurrentStatus();
    }

    private void HandleRobotStatusUpdated(
        RobotStatusData status)
    {
        if (status == null ||
            FactoryStateManager.CanonicalizeRobotId(status.robot_id) != robotId)
        {
            return;
        }

        UpdateText(status);
    }

    private void RefreshCurrentStatus()
    {
        if (factoryStateManager.TryGetRobotStatus(
                robotId,
                out RobotStatusData status))
        {
            UpdateText(status);
        }
        else
        {
            ShowNoData();
        }
    }

    private void UpdateText(RobotStatusData status)
    {
        if (statusText == null)
        {
            return;
        }

        bool hasError =
            !string.IsNullOrWhiteSpace(status.error_code);

        string stateColor =
            GetStateColor(status.state, hasError);

        StringBuilder builder = new StringBuilder();

        builder.AppendLine(
            $"<b><size=26>{displayName}</size></b>");

        builder.AppendLine();

        builder.AppendLine(
            $"상태          " +
            $"<color={stateColor}>" +
            $"{FormatState(status.state)}</color>");

        builder.AppendLine(
            $"작업 ID       {DisplayValue(status.job_id)}");

        builder.AppendLine(
            $"현재 작업     " +
            $"{DisplayValue(status.current_operation)}");

        builder.AppendLine(
            $"리프트        " +
            $"{DisplayValue(status.lift_state)}");

        if (hasError)
        {
            builder.AppendLine(
                $"오류          " +
                $"<color=#D74141>{status.error_code}</color>");
        }
        else
        {
            builder.AppendLine(
                "오류          " +
                "<color=#2AAA5D>정상</color>");
        }

        statusText.text = builder.ToString();
    }

    private void ShowNoData()
    {
        if (statusText == null)
        {
            return;
        }

        statusText.text =
            $"<b><size=26>{displayName}</size></b>\n\n" +
            "상태          데이터 없음\n" +
            "작업 ID       -\n" +
            "현재 작업     -\n" +
            "리프트        -\n" +
            "오류          -";
    }

    private string FormatState(string state)
    {
        switch (state)
        {
            case "IDLE":
                return "IDLE · 대기";

            case "MOVING":
                return "MOVING · 이동 중";

            case "WORKING":
                return "WORKING · 작업 중";

            case "WARNING":
                return "WARNING · 경고";

            case "ERROR":
                return "ERROR · 오류";

            default:
                return DisplayValue(state);
        }
    }

    private string GetStateColor(
        string state,
        bool hasError)
    {
        if (hasError || state == "ERROR")
        {
            return "#D74141";
        }

        switch (state)
        {
            case "IDLE":
                return "#2AAA5D";

            case "MOVING":
            case "WORKING":
                return "#2878D2";

            case "WARNING":
                return "#EB9B2D";

            default:
                return "#919BA5";
        }
    }

    private string DisplayValue(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? "-"
            : value;
    }
}
