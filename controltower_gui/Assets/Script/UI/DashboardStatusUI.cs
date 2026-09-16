using System;
using System.Globalization;
using System.Text;
using UnityEngine;
using UnityEngine.UI;

public class DashboardStatusUI : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("Summary Cards")]
    [SerializeField]
    private Text completedJobsText;

    [SerializeField]
    private Text currentProductText;

    [SerializeField]
    private Text currentProcessText;

    [Header("Detail Cards")]
    [SerializeField]
    private Text currentJobText;

    [SerializeField]
    private Text robotSummaryText;

    [Header("Recent Events")]
    [SerializeField] private EventLogPanelUI eventLogPanelUI;
    [SerializeField] private Text recentEventText;

    private ProductionJobData completedDisplayJob;
    private float completedDisplayUntil;
    private readonly System.Collections.Generic.HashSet<string> shownCompletions =
        new System.Collections.Generic.HashSet<string>();

    private void Update()
    {
        if (completedDisplayJob != null && Time.unscaledTime >= completedDisplayUntil)
        {
            completedDisplayJob = null;
            RefreshProductionInformation();
        }
    }

    private void RememberCompletion(ProductionJobData job)
    {
        if (job == null || FactoryOperationCatalog.JobStatus(job) != "COMPLETED") return;
        string key = job.job_id ?? job.numeric_job_id.ToString();
        if (!shownCompletions.Add(key)) return;
        completedDisplayJob = job;
        completedDisplayUntil = Time.unscaledTime + 5f;
    }

    private void Awake()
    {
        ConfigureDetailLayout(currentJobText, 13);
        ConfigureDetailLayout(robotSummaryText, 14);
        ConfigureDetailLayout(recentEventText, 13);
        ShowDefaultState();
    }

    private static void ConfigureDetailLayout(Text text, int fontSize)
    {
        if (text == null)
        {
            return;
        }

        text.alignment = TextAnchor.UpperLeft;
        text.fontSize = fontSize;
        text.resizeTextForBestFit = false;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        text.lineSpacing = 1.05f;

        RectTransform rect = text.rectTransform;
        rect.offsetMin = new Vector2(10f, 8f);
        rect.offsetMax = new Vector2(-10f, -34f);
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            Debug.LogError(
                "[Dashboard] FactoryStateManager가 " +
                "연결되지 않았습니다.",
                this);

            return;
        }

        if (eventLogPanelUI != null)
            eventLogPanelUI.LogsChanged += RefreshRecentEvents;

        factoryStateManager.SnapshotApplied +=
            HandleSnapshotApplied;

        factoryStateManager.ProductionStatusUpdated +=
            HandleProductionStatusUpdated;

        factoryStateManager.RobotStatusUpdated +=
            HandleRobotStatusUpdated;

        RefreshDashboard();
    }

    private void OnDisable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        if (eventLogPanelUI != null)
            eventLogPanelUI.LogsChanged -= RefreshRecentEvents;

        factoryStateManager.SnapshotApplied -=
            HandleSnapshotApplied;

        factoryStateManager.ProductionStatusUpdated -=
            HandleProductionStatusUpdated;

        factoryStateManager.RobotStatusUpdated -=
            HandleRobotStatusUpdated;
    }

    private void RefreshRecentEvents()
    {
        if (recentEventText == null)
            return;

        if (eventLogPanelUI == null)
        {
            recentEventText.text = "이벤트 로그가 연결되지 않았습니다.";
            return;
        }

        recentEventText.text =
            eventLogPanelUI.GetRecentSummary(5);
    }

    private void HandleSnapshotApplied(
        ProductionSnapshotData snapshot)
    {
        RefreshDashboard();
    }

    private void HandleProductionStatusUpdated(
        ProductionJobData job)
    {
        RememberCompletion(job);
        RefreshDashboard();
    }

    private void HandleRobotStatusUpdated(
        RobotStatusData status)
    {
        RefreshRobotSummary();
    }

    private void RefreshDashboard()
    {
        RefreshCompletedCount();
        RefreshProductionInformation();
        RefreshRobotSummary();
        RefreshRecentEvents();
    }

    private void RefreshCompletedCount()
    {
        int completedToday = 0;

        foreach (ProductionJobData job in
                 factoryStateManager.ProductionJobs.Values)
        {
            if (job == null ||
                job.job_status != "COMPLETED")
            {
                continue;
            }

            if (WasCompletedToday(job.completed_at))
            {
                completedToday++;
            }
        }

        if (completedJobsText != null)
        {
            completedJobsText.text =
                "오늘 완료한 생산 업무\n" +
                $"<b><size=24>{completedToday}건</size></b>";
        }
    }

    private bool WasCompletedToday(string timestamp)
    {
        if (string.IsNullOrWhiteSpace(timestamp))
        {
            return false;
        }

        bool parsed =
            DateTimeOffset.TryParse(
                timestamp,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal,
                out DateTimeOffset completedTime);

        if (!parsed)
        {
            return false;
        }

        DateTime localCompletedDate =
            completedTime.ToLocalTime().Date;

        return localCompletedDate == DateTime.Now.Date;
    }

    private void RefreshProductionInformation()
    {
        if (!factoryStateManager.TryGetActiveProductionJob(
                out ProductionJobData job))
        {
            if (completedDisplayJob != null && Time.unscaledTime < completedDisplayUntil)
                job = completedDisplayJob;
            else
            {
                ShowNoActiveProduction();
                return;
            }
        }

        string product =
            FormatProductCode(
                !string.IsNullOrWhiteSpace(job.product_code)
                    ? job.product_code
                    : job.product);

        string process = FactoryOperationCatalog.JobStatus(job) == "COMPLETED"
            ? "작업 완료" : FactoryOperationCatalog.GetProcessDisplayName(job);

        string controlState =
            FormatControlState(job.control_state);

        if (!string.IsNullOrWhiteSpace(controlState))
        {
            process += $" · {controlState}";
        }

        if (currentProductText != null)
        {
            currentProductText.text =
                "현재 생산 제품\n" +
                $"<b><size=22>{product}</size></b>";
        }

        if (currentProcessText != null)
        {
            currentProcessText.text =
                "현재 생산 공정\n" +
                $"<b><size=22>{process}</size></b>";
        }

        UpdateCurrentJobText(job, product, process);
    }

    private void UpdateCurrentJobText(
    ProductionJobData job,
    string product,
    string process)
    {
        if (currentJobText == null)
        {
            return;
        }

        string displayJobId =
            job.numeric_job_id > 0
                ? job.numeric_job_id.ToString()
                : DisplayValue(job.job_id);

        string status =
            !string.IsNullOrWhiteSpace(job.status)
                ? job.status
                : job.job_status;

        StringBuilder text = new StringBuilder();

        text.AppendLine($"<b>#{displayJobId} · {product}</b>");

        text.AppendLine(
            $"상태  {FormatJobStatus(status)}");

        text.AppendLine(
            $"공정  {process}");

        text.AppendLine(
            $"시작  " +
            $"{FormatServerTime(job.started_at)}");

        currentJobText.text = text.ToString();
    }

    private void RefreshRobotSummary()
    {
        if (robotSummaryText == null ||
            factoryStateManager == null)
        {
            return;
        }

        int working = 0;
        int connected = 0;
        int error = 0;

        foreach (string robotId in new[] { "forklift_01", "fr5", "zkbot2" })
        {
            if (!factoryStateManager.TryGetRobotStatus(robotId, out RobotStatusData robot) || robot == null)
            {
                continue;
            }

            if (robot.connected) connected++;
            if (robot.connected && robot.busy) working++;

            if (!string.IsNullOrWhiteSpace(
                    robot.error_code) ||
                robot.state == "ERROR")
            {
                error++;
            }
        }

        robotSummaryText.text =
            "전체 3대\n" +
            $"연결 <color=#2AAA5D>{connected}대</color>\n" +
            $"작업 <color=#2878D2>{working}대</color>\n" +
            $"오류 <color=#D74141>{error}대</color>";
    }

    private void ShowNoActiveProduction()
    {
        if (currentProductText != null)
        {
            currentProductText.text =
                "현재 생산 제품\n" +
                "<b><size=24>-</size></b>";
        }

        if (currentProcessText != null)
        {
            currentProcessText.text =
                "현재 생산 공정\n" +
                "<b><size=22>대기</size></b>";
        }

        if (currentJobText != null)
        {
            currentJobText.text =
                "진행 중인 생산 업무가 없습니다.";
        }
    }

    private void ShowDefaultState()
    {
        if (completedJobsText != null)
        {
            completedJobsText.text =
                "오늘 완료한 생산 업무\n" +
                "<b><size=24>0건</size></b>";
        }

        ShowNoActiveProduction();

        if (robotSummaryText != null)
        {
            robotSummaryText.text =
                "전체 3대\n연결 <color=#2AAA5D>0대</color>\n작업 <color=#2878D2>0대</color>\n오류 0대";
        }
    }

    private string FormatProcess(string operation)
    {
        return FactoryOperationCatalog.GetDisplayName(operation);
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
    private string FormatJobStatus(string status)
    {
        switch (status)
        {
            case "RUNNING":
            case "IN_PROGRESS":
                return "<color=#2878D2>진행 중</color>";

            case "PAUSED":
                return "<color=#EB9B2D>일시정지</color>";

            case "PRE_ROOF_READY":
                return "<color=#2878D2>지붕 전 검사</color>";

            case "ROOF_READY":
                return "<color=#2878D2>지붕 준비</color>";

            case "COMPLETED":
                return "<color=#2AAA5D>완료</color>";

            case "FAILED":
                return "<color=#D74141>실패</color>";

            case "CANCELED":
                return "취소";

            default:
                return DisplayValue(status);
        }
    }

    private string FormatServerTime(string timestamp)
    {
        if (string.IsNullOrWhiteSpace(timestamp))
        {
            return "-";
        }

        bool parsed =
            DateTimeOffset.TryParse(
                timestamp,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal,
                out DateTimeOffset time);

        if (!parsed)
        {
            return timestamp;
        }

        return time
            .ToLocalTime()
            .ToString("MM-dd HH:mm:ss");
    }

    private string DisplayValue(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? "-"
            : value;
    }

    private string FormatProductCode(string productCode)
    {
        if (string.IsNullOrWhiteSpace(productCode))
        {
            return "-";
        }

        string upper = productCode.ToUpperInvariant();

        if (upper.Contains("HOUSE_A") || upper.Contains("PRODUCT_A"))
        {
            return "HOUSE_A";
        }

        if (upper.Contains("HOUSE_B") || upper.Contains("PRODUCT_B"))
        {
            return "HOUSE_B";
        }

        return productCode.Length <= 18
            ? productCode
            : productCode.Substring(0, 16) + "…";
    }
}
