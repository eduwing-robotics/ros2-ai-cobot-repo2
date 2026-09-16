using System;
using System.Globalization;
using UnityEngine;
using UnityEngine.UI;

public class JobRowUI : MonoBehaviour
{
    [SerializeField]
    private Text jobIdText;

    [SerializeField]
    private Text productText;

    [SerializeField]
    private Text statusText;

    [SerializeField]
    private Text operationText;

    [SerializeField]
    private Text requestedAtText;

    private readonly Color normalColor =
        IndustrialConsoleTheme.TextColor;

    private readonly Color runningColor =
        IndustrialConsoleTheme.Cyan;

    private readonly Color completedColor =
        new Color32(42, 170, 93, 255);

    private readonly Color pausedColor =
        new Color32(235, 155, 45, 255);

    private readonly Color errorColor =
        new Color32(215, 65, 65, 255);

    private void Awake()
    {
        ConfigureColumnLayout();
    }

    public void ConfigureColumnLayout()
    {
        HorizontalLayoutGroup rowLayout =
            GetComponent<HorizontalLayoutGroup>();

        if (rowLayout == null)
        {
            return;
        }

        // Match the Header layout: five equal columns with no accumulating
        // inter-column spacing. The row itself is stretched by JobListUI.
        rowLayout.padding = new RectOffset(0, 0, 2, 2);
        rowLayout.spacing = 0f;
        rowLayout.childControlWidth = true;
        rowLayout.childForceExpandWidth = true;

        Text[] columns =
        {
            jobIdText,
            productText,
            statusText,
            operationText,
            requestedAtText
        };

        foreach (Text column in columns)
        {
            if (column == null)
            {
                continue;
            }

            LayoutElement layout = column.GetComponent<LayoutElement>();

            if (layout == null)
            {
                layout = column.gameObject.AddComponent<LayoutElement>();
            }

            layout.minWidth = 0f;
            layout.preferredWidth = 60f;
            layout.flexibleWidth = 1f;
            layout.layoutPriority = 2;
            column.alignment = TextAnchor.MiddleLeft;
            column.raycastTarget = false;
        }
    }

    public void SetData(ProductionJobData job)
    {
        if (job == null)
        {
            return;
        }

        jobIdText.text = job.job_id == "LOCAL-PROCESS-TEST" ? "TEST" :
            job.numeric_job_id > 0
                ? job.numeric_job_id.ToString()
                : DisplayValue(job.job_id);

        productText.text =
            FormatProductCode(
                !string.IsNullOrWhiteSpace(job.product_code)
                    ? job.product_code
                    : job.product);

        string serverStatus =
            !string.IsNullOrWhiteSpace(job.status)
                ? job.status
                : job.job_status;

        string displayStatus =
            IsPausedControlState(job.control_state)
                ? job.control_state
                : serverStatus;

        statusText.text =
            FormatStatus(displayStatus);

        statusText.color =
            GetStatusColor(
                displayStatus,
                job.error_code);

        operationText.text =
            FormatOperation(job.current_operation);

        requestedAtText.text =
            FormatServerTime(job.requested_at);
    }

    private string FormatStatus(string status)
    {
        switch (status)
        {
            case "RUNNING":
            case "IN_PROGRESS":
                return "진행 중";

            case "PAUSED":
                return "일시정지";

            case "PAUSE_REQUESTED":
                return "정지 요청";

            case "RESUME_REQUESTED":
                return "재개 요청";

            case "PRE_ROOF_READY":
                return "검사 준비";

            case "ROOF_READY":
                return "지붕 준비";

            case "COMPLETED":
                return "완료";

            case "CANCELED":
                return "취소";

            case "FAILED":
                return "실패";

            case "PENDING":
            case "QUEUED":
            case "CREATED":
            case "REQUESTED":
                return "대기";

            default:
                return DisplayValue(status);
        }
    }

    private string FormatOperation(string operation)
    {
        string displayName = FactoryOperationCatalog.GetDisplayName(operation);
        return displayName == "공정 정보 대기" ? "-" : displayName;
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

        // 실행 중인 PC의 로컬 시간으로 변환
        return time
            .ToLocalTime()
            .ToString("HH:mm:ss");
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

        return productCode.Length <= 14
            ? productCode
            : productCode.Substring(0, 12) + "…";
    }

    private Color GetStatusColor(
        string status,
        string errorCode)
    {
        if (!string.IsNullOrWhiteSpace(errorCode) ||
            status == "FAILED")
        {
            return errorColor;
        }

        switch (status)
        {
            case "RUNNING":
            case "IN_PROGRESS":
            case "PRE_ROOF_READY":
            case "ROOF_READY":
                return runningColor;

            case "COMPLETED":
                return completedColor;

            case "PAUSED":
            case "PAUSE_REQUESTED":
            case "RESUME_REQUESTED":
                return pausedColor;

            default:
                return normalColor;
        }
    }

    private static bool IsPausedControlState(string controlState)
    {
        string state = (controlState ?? string.Empty)
            .Trim()
            .ToUpperInvariant();

        return state == "PAUSED" ||
               state == "PAUSE_REQUESTED" ||
               state == "RESUME_REQUESTED";
    }

    private string DisplayValue(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? "-"
            : value;
    }
}
