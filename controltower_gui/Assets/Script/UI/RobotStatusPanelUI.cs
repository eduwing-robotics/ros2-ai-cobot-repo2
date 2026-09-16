using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.UI;

public class RobotStatusPanelUI : MonoBehaviour
{
    [Serializable]
    public class RobotTextBinding
    {
        public string robotId;
        public string displayName;
        public Text statusText;
    }

    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("Robot Texts")]
    [SerializeField]
    private RobotTextBinding[] robotBindings;

    private readonly Dictionary<string, RobotTextBinding>
        bindingByRobotId =
            new Dictionary<string, RobotTextBinding>();

    private void Awake()
    {
        BuildBindingDictionary();
        ShowAllNoData();
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            Debug.LogError(
                "[RobotPanel] FactoryStateManager가 " +
                "연결되지 않았습니다.",
                this);

            return;
        }

        factoryStateManager.SnapshotApplied +=
            HandleSnapshotApplied;

        factoryStateManager.RobotStatusUpdated +=
            HandleRobotStatusUpdated;

        RefreshAll();
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

    private void BuildBindingDictionary()
    {
        bindingByRobotId.Clear();

        if (robotBindings == null)
        {
            return;
        }

        foreach (RobotTextBinding binding in robotBindings)
        {
            if (binding == null ||
                string.IsNullOrWhiteSpace(binding.robotId))
            {
                continue;
            }

            binding.robotId = FactoryStateManager.CanonicalizeRobotId(binding.robotId);
            if (binding.robotId == "forklift_01" || binding.robotId == "turtlebot_01")
                binding.displayName = "forklift";
            bindingByRobotId[binding.robotId] = binding;
        }
    }

    private void HandleSnapshotApplied(
        ProductionSnapshotData snapshot)
    {
        RefreshAll();
    }

    private void HandleRobotStatusUpdated(
        RobotStatusData status)
    {
        if (status == null)
        {
            return;
        }

        if (!bindingByRobotId.TryGetValue(
                FactoryStateManager.CanonicalizeRobotId(status.robot_id),
                out RobotTextBinding binding))
        {
            // UI에 등록되지 않은 로봇은 무시합니다.
            return;
        }

        UpdateRobotText(binding, status);
    }

    private void RefreshAll()
    {
        if (factoryStateManager == null ||
            robotBindings == null)
        {
            return;
        }

        foreach (RobotTextBinding binding in robotBindings)
        {
            if (binding == null)
            {
                continue;
            }

            if (factoryStateManager.TryGetRobotStatus(
                    binding.robotId,
                    out RobotStatusData status))
            {
                UpdateRobotText(binding, status);
            }
            else
            {
                ShowNoData(binding);
            }
        }
    }

    private void UpdateRobotText(
    RobotTextBinding binding,
    RobotStatusData status)
    {
        if (binding.statusText == null)
        {
            return;
        }

        bool hasError =
            !string.IsNullOrWhiteSpace(status.error_code);

        string stateColor =
            GetStateColor(status.state, hasError);

        string connectionText =
            status.connected
                ? "<color=#2AAA5D>연결됨</color>"
                : "<color=#D74141>연결 끊김</color>";

        string readyText =
            status.ready
                ? "<color=#2AAA5D>준비 완료</color>"
                : "<color=#EB9B2D>준비 안 됨</color>";

        StringBuilder text = new StringBuilder();

        text.AppendLine(
            $"<b><size=26>{binding.displayName}</size></b>");

        text.AppendLine();

        text.AppendLine(
            $"상태          " +
            $"<color={stateColor}>" +
            $"{FormatState(status.state)}</color>");

        text.AppendLine(
            $"연결 상태     {connectionText}");

        text.AppendLine(
            $"작업 준비     {readyText}");

        text.AppendLine(
            $"현재 작업     " +
            $"{DisplayValue(status.current_operation)}");

        if (!string.IsNullOrWhiteSpace(status.job_id))
        {
            text.AppendLine(
                $"작업 ID       {status.job_id}");
        }

        if (!string.IsNullOrWhiteSpace(status.lift_state))
        {
            text.AppendLine(
                $"리프트        {status.lift_state}");
        }

        if (hasError)
        {
            text.AppendLine(
                $"오류          " +
                $"<color=#D74141>{status.error_code}</color>");
        }
        else
        {
            text.AppendLine(
                "오류          " +
                "<color=#2AAA5D>정상</color>");
        }

        binding.statusText.text = text.ToString();
    }

    private void ShowAllNoData()
    {
        if (robotBindings == null)
        {
            return;
        }

        foreach (RobotTextBinding binding in robotBindings)
        {
            ShowNoData(binding);
        }
    }

    private void ShowNoData(RobotTextBinding binding)
    {
        if (binding == null ||
            binding.statusText == null)
        {
            return;
        }

        binding.statusText.text =
            $"<b><size=26>{binding.displayName}</size></b>\n\n" +
            "상태          데이터 없음\n" +
            "연결 상태     -\n" +
            "작업 준비     -\n" +
            "현재 작업     -\n" +
            "오류          -";
    }

    private string FormatState(string state)
    {
        switch (state)
        {
            case "IDLE":
                return "대기";

            case "WORKING":
                return "작업 중";

            case "NOT_READY":
                return "준비되지 않음";

            case "DISCONNECTED":
                return "연결 끊김";

            case "MOVING":
                return "이동 중";

            case "WARNING":
                return "경고";

            case "ERROR":
                return "오류";

            default:
                return DisplayValue(state);
        }
    }

    private string GetStateColor(
    string state,
    bool hasError)
    {
        if (hasError ||
            state == "ERROR" ||
            state == "DISCONNECTED")
        {
            return "#D74141";
        }

        switch (state)
        {
            case "IDLE":
                return "#2AAA5D";
            case "NOT_READY":
                return "#EB9B2D";

            case "WORKING":
            case "MOVING":
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