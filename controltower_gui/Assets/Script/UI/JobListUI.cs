using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;

public class JobListUI : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("UI")]
    [SerializeField]
    private Transform content;

    [SerializeField]
    private JobRowUI jobRowPrefab;

    [SerializeField]
    private Text emptyStateText;

    [SerializeField]
    [Min(1)]
    private int maxVisibleJobs = 5;

    private readonly Dictionary<string, JobRowUI>
        rowsByJobId =
            new Dictionary<string, JobRowUI>();

    private void OnEnable()
    {
        ConfigureTableLayout();
        ConfigureEmptyStateLayout();
        if (factoryStateManager == null)
        {
            Debug.LogError(
                "[JobList] FactoryStateManager가 " +
                "연결되지 않았습니다.",
                this);

            return;
        }

        factoryStateManager.SnapshotApplied +=
            HandleSnapshotApplied;

        factoryStateManager.ProductionStatusUpdated +=
            HandleProductionStatusUpdated;

        RebuildList();
    }

    public void ConfigureTableLayout()
    {
        if (content == null)
        {
            return;
        }

        // The header is inset 10 px from JobListP. JobScrollView is inset only
        // 5 px, so its content needs another 5 px on each side to share the
        // exact same five-column coordinate system as the header.
        if (content is RectTransform contentRect)
        {
            float currentHeight = contentRect.sizeDelta.y;
            contentRect.anchorMin = new Vector2(0f, 1f);
            contentRect.anchorMax = new Vector2(1f, 1f);
            contentRect.pivot = new Vector2(0.5f, 1f);
            contentRect.anchoredPosition = new Vector2(0f, 0f);
            contentRect.sizeDelta = new Vector2(-10f, currentHeight);
        }

        VerticalLayoutGroup rowsLayout =
            content.GetComponent<VerticalLayoutGroup>();

        if (rowsLayout != null)
        {
            rowsLayout.childControlWidth = true;
            rowsLayout.childForceExpandWidth = true;
        }
    }

    public void ConfigureEmptyStateLayout()
    {
        if (emptyStateText == null || content == null || content.parent == null) return;
        // Placeholder is an overlay, not a narrow cell in the row layout.
        emptyStateText.transform.SetParent(content.parent, false);
        var rect = emptyStateText.rectTransform;
        rect.anchorMin = Vector2.zero;
        rect.anchorMax = Vector2.one;
        rect.offsetMin = new Vector2(12, 12);
        rect.offsetMax = new Vector2(-12, -12);
        emptyStateText.alignment = TextAnchor.MiddleCenter;
        emptyStateText.fontSize = 13;
        emptyStateText.horizontalOverflow = HorizontalWrapMode.Wrap;
        emptyStateText.verticalOverflow = VerticalWrapMode.Truncate;
        emptyStateText.raycastTarget = false;
        var layout = emptyStateText.GetComponent<LayoutElement>();
        if (layout == null) layout = emptyStateText.gameObject.AddComponent<LayoutElement>();
        layout.ignoreLayout = true;
    }

    private void OnDisable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.SnapshotApplied -=
            HandleSnapshotApplied;

        factoryStateManager.ProductionStatusUpdated -=
            HandleProductionStatusUpdated;
    }

    private void HandleSnapshotApplied(
        ProductionSnapshotData snapshot)
    {
        RebuildList();
    }

    private void HandleProductionStatusUpdated(
        ProductionJobData job)
    {
        RebuildList();
    }

    private void RebuildList()
    {
        ClearRows();

        if (factoryStateManager == null)
        {
            return;
        }

        List<ProductionJobData> jobs =
            new List<ProductionJobData>(
                factoryStateManager.ProductionJobs.Values);

        // 최근 생성된 숫자 Job ID가 위에 표시됩니다.
        jobs.Sort(
            (first, second) =>
                second.numeric_job_id.CompareTo(
                    first.numeric_job_id));

        int visibleCount = 0;

        foreach (ProductionJobData job in jobs)
        {
            if (!IsVisibleJob(job))
            {
                continue;
            }

            UpdateOrCreateRow(job);

            visibleCount++;

            if (visibleCount >= maxVisibleJobs)
            {
                break;
            }
        }

        UpdateEmptyState();
    }

    private void UpdateOrCreateRow(
        ProductionJobData job)
    {
        if (job == null ||
            string.IsNullOrWhiteSpace(job.job_id))
        {
            return;
        }

        if (!rowsByJobId.TryGetValue(
                job.job_id,
                out JobRowUI row))
        {
            row = Instantiate(
                jobRowPrefab,
                content);

            row.gameObject.name =
                $"JobRow_{job.job_id}";

            rowsByJobId[job.job_id] = row;
        }

        row.SetData(job);
        UpdateEmptyState();
    }

    private void ClearRows()
    {
        foreach (JobRowUI row in rowsByJobId.Values)
        {
            if (row != null)
            {
                Destroy(row.gameObject);
            }
        }

        rowsByJobId.Clear();
        UpdateEmptyState();
    }

    private bool IsVisibleJob(ProductionJobData job)
    {
        if (job == null)
        {
            return false;
        }

        string status = !string.IsNullOrWhiteSpace(job.status)
            ? job.status
            : job.job_status;

        switch (status)
        {
            case "RUNNING":
            case "IN_PROGRESS":
            case "PAUSED":
            case "REQUESTED":
            case "PENDING":
            case "QUEUED":
            case "CREATED":
                return true;

            default:
                return false;
        }
    }

    private void UpdateEmptyState()
    {
        if (emptyStateText != null)
        {
            emptyStateText.gameObject.SetActive(rowsByJobId.Count == 0);
        }
    }
}
