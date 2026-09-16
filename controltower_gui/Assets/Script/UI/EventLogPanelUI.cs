using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;
using System.Text;

public class EventLogPanelUI : MonoBehaviour
{
    public event Action LogsChanged;

    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    private enum LogFilter
    {
        All,
        Info,
        Warning,
        Error
    }

    private class EventEntry
    {
        public DateTime timestamp;
        public EventLogLevel level;
        public string message;
    }

    [Header("List")]
    [SerializeField]
    private Transform content;

    [SerializeField]
    private EventLogRowUI rowPrefab;

    [SerializeField]
    private Text emptyStateText;

    [SerializeField]
    private int maxEntries = 100;

    [Header("Filter Buttons")]
    [SerializeField]
    private Button allButton;

    [SerializeField]
    private Button infoButton;

    [SerializeField]
    private Button warningButton;

    [SerializeField]
    private Button errorButton;

    [Header("Mock")]
    [SerializeField]
    private bool loadMockEventsOnStart = false;

    private readonly List<EventEntry> entries =
        new List<EventEntry>();

    private readonly List<EventLogRowUI> visibleRows =
        new List<EventLogRowUI>();

    // 서버는 같은 상태를 반복해서 보낼 수 있으므로, 화면 로그에는
    // 실제 상태 변화와 진행률 이정표만 남깁니다.
    private readonly Dictionary<string, string> productionSignatures =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

    private readonly Dictionary<string, string> robotSignatures =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

    private readonly Dictionary<string, string> transportSignatures =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

    private readonly HashSet<string> incomingQaSignatures =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    private readonly HashSet<string> incomingGateReleasedJobs =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    private readonly HashSet<string> inspectionSignatures =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    private readonly HashSet<string> errorSignatures =
        new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    private LogFilter currentFilter = LogFilter.All;

    private readonly Color selectedColor =
        IndustrialConsoleTheme.Accent;

    private readonly Color normalColor =
        IndustrialConsoleTheme.Raised;

    private readonly Color selectedTextColor = IndustrialConsoleTheme.Background;

    private readonly Color normalTextColor =
        IndustrialConsoleTheme.TextColor;

    private void Awake()
    {
        allButton.onClick.AddListener(
            () => SetFilter(LogFilter.All));

        infoButton.onClick.AddListener(
            () => SetFilter(LogFilter.Info));

        warningButton.onClick.AddListener(
            () => SetFilter(LogFilter.Warning));

        errorButton.onClick.AddListener(
            () => SetFilter(LogFilter.Error));

        UpdateFilterButtons();
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.ErrorEventReceived += HandleErrorEvent;
        factoryStateManager.SnapshotApplied += HandleSnapshotApplied;
        factoryStateManager.ProductionStatusUpdated += HandleProductionStatus;
        factoryStateManager.RobotStatusUpdated += HandleRobotStatus;
        factoryStateManager.TransportStatusUpdated += HandleTransportStatus;
        factoryStateManager.IncomingQaStatusUpdated += HandleIncomingQaStatus;
        factoryStateManager.ProductionInspectionStatusUpdated +=
            HandleProductionInspectionStatus;

    }

    private void OnDisable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.ErrorEventReceived -= HandleErrorEvent;
        factoryStateManager.SnapshotApplied -= HandleSnapshotApplied;
        factoryStateManager.ProductionStatusUpdated -= HandleProductionStatus;
        factoryStateManager.RobotStatusUpdated -= HandleRobotStatus;
        factoryStateManager.TransportStatusUpdated -= HandleTransportStatus;
        factoryStateManager.IncomingQaStatusUpdated -= HandleIncomingQaStatus;
        factoryStateManager.ProductionInspectionStatusUpdated -=
            HandleProductionInspectionStatus;
    }

    private void HandleSnapshotApplied(ProductionSnapshotData snapshot)
    {
        if (snapshot == null)
        {
            return;
        }

        SeedSnapshotState(snapshot);

        int jobCount = snapshot.jobs?.Length ?? 0;
        int robotCount = snapshot.robots?.Length ?? 0;

        AddInfo(
            $"서버 상태 동기화 완료 · 작업 {jobCount}건 · " +
            $"로봇 {robotCount}대");


        if (snapshot.active_errors == null)
        {
            return;
        }

        foreach (ErrorEventData error in snapshot.active_errors)
        {
            HandleErrorEvent(error);
        }
    }

    private void HandleErrorEvent(ErrorEventData error)
    {
        if (error == null)
        {
            return;
        }

        string code = string.IsNullOrWhiteSpace(error.error_code)
            ? "ERROR"
            : error.error_code;

        string detail = string.IsNullOrWhiteSpace(error.detail)
            ? "상세 내용 없음"
            : error.detail;

        string signature =
            $"{error.job_id}|{error.step_id}|{error.delivery_id}|" +
            $"{code}|{detail}";

        if (!errorSignatures.Add(signature))
        {
            return;
        }

        AddError($"[{code}] {detail}");
    }

    private void HandleProductionStatus(ProductionJobData job)
    {
        if (job == null)
        {
            return;
        }

        string jobId = GetJobId(job);
        string status = FirstText(job.status, job.job_status, "상태 미정");
        string step = FirstText(
            job.process_stage_display_name,
            job.current_step_display_name,
            job.current_operation,
            job.current_stage_code,
            "공정 대기");
        string signature =
            $"{status}|{job.control_state}|{job.current_stage_code}|" +
            $"{job.current_operation}|{job.current_step_status}|" +
            $"{job.current_step_order}|{job.process_stage_code}|{job.process_stage_order}";

        if (!StoreChanged(productionSignatures, jobId, signature))
        {
            return;
        }

        string message = $"작업 {jobId} · {step} · {status}";
        string normalized = status.ToUpperInvariant();

        if (normalized.Contains("ERROR") || normalized.Contains("FAIL"))
        {
            AddError(message);
        }
        else if (normalized.Contains("HOLD") ||
                 normalized.Contains("PAUSE") ||
                 normalized.Contains("BLOCK"))
        {
            AddWarning(message);
        }
        else
        {
            AddInfo(message);
        }
    }

    private void HandleRobotStatus(RobotStatusData robot)
    {
        if (robot == null || string.IsNullOrWhiteSpace(robot.robot_id))
        {
            return;
        }

        string key = robot.robot_id.Trim();
        string signature =
            $"{robot.connected}|{robot.ready}|{robot.busy}|{robot.state}|" +
            $"{robot.current_operation}|{robot.lift_state}|{robot.error_code}";

        if (!StoreChanged(robotSignatures, key, signature))
        {
            return;
        }

        if (!string.IsNullOrWhiteSpace(robot.error_code))
        {
            AddError($"{key} 오류 · {robot.error_code}");
            return;
        }

        if (!robot.connected)
        {
            AddWarning($"{key} 연결 끊김");
            return;
        }

        string operation = FirstText(
            robot.current_operation,
            robot.lift_state,
            robot.state,
            robot.ready ? "준비 완료" : "연결됨");

        AddInfo($"{key} · {Humanize(operation)}");
    }

    private void HandleTransportStatus(TransportStatusData transport)
    {
        if (transport == null)
        {
            return;
        }

        string key = FirstText(
            transport.req_id,
            transport.delivery_id,
            $"{transport.job_id}:{transport.robot_id}");
        int progressPercent = Mathf.Clamp(
            Mathf.RoundToInt(transport.progress * 100f),
            0,
            100);
        int progressMilestone = progressPercent >= 100
            ? 100
            : (progressPercent / 25) * 25;
        string signature =
            $"{transport.phase}|{transport.result}|{transport.error_code}|" +
            $"{progressMilestone}";

        if (!StoreChanged(transportSignatures, key, signature))
        {
            return;
        }

        string robot = FirstText(transport.robot_id, "운반 로봇");
        string phase = Humanize(FirstText(transport.phase, "운반 상태 갱신"));
        string message = $"{robot} 운반 · {phase} · {progressPercent}%";
        string result = FirstText(transport.result).ToUpperInvariant();

        if (!string.IsNullOrWhiteSpace(transport.error_code) ||
            result.Contains("ERROR") || result.Contains("FAIL"))
        {
            string detail = FirstText(
                transport.detail,
                transport.error_code,
                "원인 미상");
            AddError($"{message} · {detail}");
        }
        else
        {
            AddInfo(message);
        }
    }

    private void HandleIncomingQaStatus(IncomingQaStatusData data)
    {
        if (data == null)
        {
            return;
        }

        if (data.transactions != null && data.transactions.Length > 0)
        {
            foreach (IncomingQaTransactionData transaction in data.transactions)
            {
                LogIncomingTransaction(data, transaction);
            }
        }
        else
        {
            LogIncomingTransaction(data, data.transaction);
        }

        if (string.Equals(
                data.job_gate_state,
                "RELEASED",
                StringComparison.OrdinalIgnoreCase))
        {
            string jobKey = data.job_id.ToString();

            if (incomingGateReleasedJobs.Add(jobKey))
            {
                AddInfo(
                    "[수입검사] 전체 통과 · Gate 해제 · " +
                    $"Job {jobKey}");
            }
        }
    }

    private void LogIncomingTransaction(
        IncomingQaStatusData data,
        IncomingQaTransactionData transaction)
    {
        if (transaction == null)
        {
            return;
        }

        string status = FirstText(transaction.status).ToUpperInvariant();
        string result = FirstText(transaction.overall_result).ToUpperInvariant();
        string key =
            $"{data.job_id}|{transaction.transaction_id}|" +
            $"{transaction.request_id}|{transaction.cycle}|{status}|{result}";

        if (!incomingQaSignatures.Add(key))
        {
            return;
        }

        string mode = CompactIncomingMode(transaction.mode);

        if (status == "REQUESTED")
        {
            AddInfo($"[수입검사] {mode} 시작 · Job {data.job_id}");
            return;
        }

        // SENT/ACKED는 통신 중간 상태라 운영 로그를 불필요하게 늘리지 않습니다.
        if (status == "SENT" || status == "ACKED")
        {
            return;
        }

        if (status == "ERROR" || status == "REJECTED")
        {
            AddError(
                $"[수입검사] {mode} 실패 · " +
                FirstText(transaction.error_reason, status));
            return;
        }

        if (status != "COMPLETED")
        {
            return;
        }

        if (result == "PASS")
        {
            AddInfo($"[수입검사] {mode} 통과 · Job {data.job_id}");
        }
        else if (result == "FAIL")
        {
            AddWarning(
                $"[수입검사] {mode} 불량 · " +
                BuildIncomingFailureSummary(data.items));
        }
        else
        {
            AddWarning(
                $"[수입검사] {mode} · " +
                Humanize(result));
        }
    }

    private void HandleProductionInspectionStatus(
        ProductionInspectionStatusData data)
    {
        ProductionInspectionData inspection = data?.inspection;

        if (inspection == null)
        {
            return;
        }

        string status = FirstText(inspection.status).ToUpperInvariant();
        string result = FirstText(inspection.result).ToUpperInvariant();
        string key =
            $"{data.job_id}|{inspection.inspection_id}|" +
            $"{inspection.inspection_request_id}|{inspection.inspection_cycle}|" +
            $"{status}|{result}|{inspection.current_view}|" +
            $"{inspection.gate_state}|{BuildInspectionViewSignature(inspection.views)}";

        if (!inspectionSignatures.Add(key))
        {
            return;
        }

        string type = string.Equals(
            data.inspection_type,
            "PRE_ROOF",
            StringComparison.OrdinalIgnoreCase)
            ? "조립 결과 검사"
            : Humanize(FirstText(data.inspection_type, "품질검사"));

        if (status == "PENDING" || status == "RUNNING")
        {
            string currentView = FirstText(inspection.current_view);
            AddInfo(string.IsNullOrWhiteSpace(currentView)
                ? $"[{type}] 요청 대기 · Job {data.job_id}"
                : $"[{type}] {currentView} 진행 중 · Job {data.job_id}");
            return;
        }

        if (status == "ERROR")
        {
            AddError(
                $"[{type}] 통신/검사 오류 · " +
                FirstText(inspection.transport?.wire_error_code, "원인 미상"));
            return;
        }

        if (status != "COMPLETED")
        {
            return;
        }

        if (result == "FAIL")
        {
            AddWarning(
                $"[{type}] 불량 · " +
                BuildInspectionFailureSummary(inspection.views));
            return;
        }

        if (result == "PASS")
        {
            bool released = string.Equals(
                inspection.gate_state,
                "RELEASED",
                StringComparison.OrdinalIgnoreCase);

            if (released)
            {
                AddInfo($"[{type}] 통과 · Gate 해제 · Job {data.job_id}");
            }
            else
            {
                AddWarning(
                    $"[{type}] PASS · " +
                    "서버 Gate 대기");
            }
        }
        else
        {
            AddWarning(
                $"[{type}] {Humanize(result)} · Job {data.job_id}");
        }
    }

    private void SeedSnapshotState(ProductionSnapshotData snapshot)
    {
        productionSignatures.Clear();
        robotSignatures.Clear();
        transportSignatures.Clear();
        incomingQaSignatures.Clear();
        incomingGateReleasedJobs.Clear();
        inspectionSignatures.Clear();

        if (snapshot.jobs != null)
        {
            foreach (ProductionJobData job in snapshot.jobs)
            {
                if (job == null)
                {
                    continue;
                }

                productionSignatures[GetJobId(job)] =
                    $"{FirstText(job.status, job.job_status, "상태 미정")}|" +
                    $"{job.control_state}|{job.current_stage_code}|" +
                    $"{job.current_operation}|{job.current_step_status}|" +
                    $"{job.current_step_order}|{job.process_stage_code}|{job.process_stage_order}";
            }
        }

        if (snapshot.robots != null)
        {
            foreach (RobotStatusData robot in snapshot.robots)
            {
                if (robot == null || string.IsNullOrWhiteSpace(robot.robot_id))
                {
                    continue;
                }

                robotSignatures[robot.robot_id.Trim()] =
                    $"{robot.connected}|{robot.ready}|{robot.busy}|{robot.state}|" +
                    $"{robot.current_operation}|{robot.lift_state}|{robot.error_code}";
            }
        }

        if (snapshot.transports != null)
        {
            foreach (TransportStatusData transport in snapshot.transports)
            {
                SeedTransport(transport);
            }
        }

        if (snapshot.incoming_qa != null)
        {
            foreach (IncomingQaStatusData incoming in snapshot.incoming_qa)
            {
                SeedIncomingQa(incoming);
            }
        }

        if (snapshot.production_inspections != null)
        {
            foreach (ProductionInspectionStatusData inspection in
                     snapshot.production_inspections)
            {
                SeedInspection(inspection);
            }
        }
    }

    private void SeedTransport(TransportStatusData transport)
    {
        if (transport == null)
        {
            return;
        }

        string key = FirstText(
            transport.req_id,
            transport.delivery_id,
            $"{transport.job_id}:{transport.robot_id}");
        int percent = Mathf.Clamp(
            Mathf.RoundToInt(transport.progress * 100f),
            0,
            100);
        int milestone = percent >= 100 ? 100 : (percent / 25) * 25;

        transportSignatures[key] =
            $"{transport.phase}|{transport.result}|{transport.error_code}|" +
            $"{milestone}";
    }

    private void SeedIncomingQa(IncomingQaStatusData data)
    {
        if (data == null)
        {
            return;
        }

        if (string.Equals(
                data.job_gate_state,
                "RELEASED",
                StringComparison.OrdinalIgnoreCase))
        {
            incomingGateReleasedJobs.Add(data.job_id.ToString());
        }

        IncomingQaTransactionData[] transactions = data.transactions;

        if (transactions != null && transactions.Length > 0)
        {
            foreach (IncomingQaTransactionData transaction in transactions)
            {
                SeedIncomingTransaction(data.job_id, transaction);
            }
        }
        else
        {
            SeedIncomingTransaction(data.job_id, data.transaction);
        }
    }

    private void SeedIncomingTransaction(
        long jobId,
        IncomingQaTransactionData transaction)
    {
        if (transaction == null)
        {
            return;
        }

        incomingQaSignatures.Add(
            $"{jobId}|{transaction.transaction_id}|{transaction.request_id}|" +
            $"{transaction.cycle}|{FirstText(transaction.status).ToUpperInvariant()}|" +
            $"{FirstText(transaction.overall_result).ToUpperInvariant()}");
    }

    private void SeedInspection(ProductionInspectionStatusData data)
    {
        ProductionInspectionData inspection = data?.inspection;

        if (inspection == null)
        {
            return;
        }

        inspectionSignatures.Add(
            $"{data.job_id}|{inspection.inspection_id}|" +
            $"{inspection.inspection_request_id}|{inspection.inspection_cycle}|" +
            $"{FirstText(inspection.status).ToUpperInvariant()}|" +
            $"{FirstText(inspection.result).ToUpperInvariant()}|" +
            $"{inspection.gate_state}");
    }

    private static bool StoreChanged(
        IDictionary<string, string> cache,
        string key,
        string signature)
    {
        if (cache.TryGetValue(key, out string previous) &&
            string.Equals(previous, signature, StringComparison.Ordinal))
        {
            return false;
        }

        cache[key] = signature;
        return true;
    }

    private static string GetJobId(ProductionJobData job)
    {
        return FirstText(
            job.job_id,
            job.numeric_job_id > 0 ? job.numeric_job_id.ToString() : null,
            job.job_code,
            "-");
    }

    private static string FirstText(params string[] values)
    {
        if (values != null)
        {
            foreach (string value in values)
            {
                if (!string.IsNullOrWhiteSpace(value))
                {
                    return value.Trim();
                }
            }
        }

        return string.Empty;
    }

    private static string Humanize(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "상태 미정";
        }

        switch (value.Trim().ToUpperInvariant())
        {
            case "BASE_AB": return "베이스";
            case "HOUSE_A": return "HOUSE_A 자재";
            case "HOUSE_B": return "HOUSE_B 자재";
            case "DELIVERING_PICKUP": return "Pickup 이동 중";
            case "DELIVERING_DROPOFF": return "Dropoff 이동 중";
            case "PICKUP": return "Pickup 작업";
            case "DROPOFF": return "Dropoff 작업";
            case "LIFTING": return "리프트 상승 중";
            case "LOWERING": return "리프트 하강 중";
            case "NOT_EVALUATED": return "판정 대기";
            default: return value.Replace('_', ' ');
        }
    }

    private static string CompactIncomingMode(string value)
    {
        switch (FirstText(value).ToUpperInvariant())
        {
            case "BASE_AB": return "베이스";
            case "HOUSE_A": return "HOUSE_A";
            case "HOUSE_B": return "HOUSE_B";
            default: return FirstText(value, "자재").Replace('_', ' ');
        }
    }

    private static string BuildIncomingFailureSummary(
        IncomingQaItemData[] items)
    {
        if (items == null || items.Length == 0)
        {
            return "불량 상세 없음";
        }

        List<string> failures = new List<string>();

        foreach (IncomingQaItemData item in items)
        {
            if (item == null ||
                !string.Equals(item.result, "FAIL", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            string part = FirstText(item.expected_part_code, "자재");
            string slot = FirstText(item.slot_id, "-");
            string reason = IncomingQaFailureText.Format(item);
            failures.Add($"{slot} {part} ({reason})");
        }

        return failures.Count == 0
            ? "불량 상세 없음"
            : string.Join(", ", failures);
    }

    private static string BuildInspectionFailureSummary(
        ProductionInspectionViewData[] views)
    {
        if (views == null || views.Length == 0)
        {
            return "불량 상세 없음";
        }

        List<string> failures = new List<string>();

        foreach (ProductionInspectionViewData view in views)
        {
            if (view == null ||
                !string.Equals(view.result, "FAIL", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            failures.Add(
                $"{FirstText(view.view_name, "VIEW")}: " +
                FirstText(view.reason_code, "DEFECT"));
        }

        return failures.Count == 0
            ? "불량 상세 없음"
            : string.Join(", ", failures);
    }

    private static string BuildInspectionViewSignature(
        ProductionInspectionViewData[] views)
    {
        if (views == null || views.Length == 0) return string.Empty;

        List<string> states = new List<string>();
        foreach (ProductionInspectionViewData view in views)
        {
            if (view == null) continue;
            states.Add(
                $"{view.view_name}:{FirstText(view.status, view.result)}");
        }
        return string.Join(",", states);
    }

    private void Start()
    {
        if (loadMockEventsOnStart)
        {
            LoadMockEvents();
        }
        else
        {
            RebuildRows();
        }
    }

    public void AddInfo(string message)
    {
        AddEntry(EventLogLevel.Info, message);
    }

    public void AddWarning(string message)
    {
        AddEntry(EventLogLevel.Warning, message);
    }

    public void AddError(string message)
    {
        AddEntry(EventLogLevel.Error, message);
    }

    public void AddEntry(
        EventLogLevel level,
        string message)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            return;
        }

        entries.Add(
            new EventEntry
            {
                timestamp = DateTime.Now,
                level = level,
                message = message
            });

        entries.Sort((a, b) => a.timestamp.CompareTo(b.timestamp));
        while (entries.Count > maxEntries)
        {
            entries.RemoveAt(0);
        }

        RebuildRows();
        LogsChanged?.Invoke();
    }

    private void SetFilter(LogFilter filter)
    {
        currentFilter = filter;

        UpdateFilterButtons();
        RebuildRows();
    }

    private readonly Dictionary<EventEntry, EventLogRowUI> rowsByEntry =
        new Dictionary<EventEntry, EventLogRowUI>();

    private void RebuildRows()
    {
        var removed = new List<EventEntry>();
        foreach (var pair in rowsByEntry)
            if (!entries.Contains(pair.Key))
            {
                if (pair.Value != null)
                {
                    pair.Value.gameObject.SetActive(false);
                    Destroy(pair.Value.gameObject);
                }
                removed.Add(pair.Key);
            }
        foreach (EventEntry entry in removed) rowsByEntry.Remove(entry);
        visibleRows.Clear();
        int index = 0;
        for (int i = entries.Count - 1; i >= 0; i--)
        {
            EventEntry entry = entries[i];
            bool visible = PassesFilter(entry.level);
            if (!rowsByEntry.TryGetValue(entry, out EventLogRowUI row) || row == null)
            {
                if (!visible) continue;
                row = Instantiate(rowPrefab, content);
                rowsByEntry[entry] = row;
                // Bind the timestamp once, when this record's row is created.
                row.SetData(entry.timestamp.ToString("HH:mm:ss"), entry.level, entry.message, index % 2 == 1);
            }
            row.gameObject.SetActive(visible);
            if (!visible) continue;
            row.transform.SetSiblingIndex(index++);
            visibleRows.Add(row);
        }
        if (emptyStateText != null) emptyStateText.gameObject.SetActive(index == 0);
    }

    private bool PassesFilter(EventLogLevel level)
    {
        switch (currentFilter)
        {
            case LogFilter.Info:
                return level == EventLogLevel.Info;

            case LogFilter.Warning:
                return level == EventLogLevel.Warning;

            case LogFilter.Error:
                return level == EventLogLevel.Error;

            default:
                return true;
        }
    }

    private void ClearVisibleRows()
    {
        foreach (EventLogRowUI row in rowsByEntry.Values)
            if (row != null) Destroy(row.gameObject);
        rowsByEntry.Clear();
        visibleRows.Clear();
    }

    private void UpdateFilterButtons()
    {
        SetButtonVisual(
            allButton,
            currentFilter == LogFilter.All);

        SetButtonVisual(
            infoButton,
            currentFilter == LogFilter.Info);

        SetButtonVisual(
            warningButton,
            currentFilter == LogFilter.Warning);

        SetButtonVisual(
            errorButton,
            currentFilter == LogFilter.Error);
    }

    private void SetButtonVisual(
        Button button,
        bool selected)
    {
        if (button == null)
        {
            return;
        }

        button.transition = Selectable.Transition.None;
        button.image.color =
            selected ? selectedColor : normalColor;

        Text buttonText =
            button.GetComponentInChildren<Text>();

        if (buttonText != null)
        {
            buttonText.color =
                selected
                    ? selectedTextColor
                    : normalTextColor;
        }
    }

    private void LoadMockEvents()
    {
        entries.Clear();

        AddMockEntry(
            7,
            EventLogLevel.Info,
            "HOUSE_A 생산 작업이 생성되었습니다.");

        AddMockEntry(
            6,
            EventLogLevel.Info,
            "1차 자재 품질검사를 시작했습니다.");

        AddMockEntry(
            5,
            EventLogLevel.Info,
            "자재 품질검사 결과: PASS");

        AddMockEntry(
            4,
            EventLogLevel.Info,
            "포크리프트가 자재 운반을 시작했습니다.");

        AddMockEntry(
            3,
            EventLogLevel.Warning,
            "ZKKeep 2가 준비되지 않은 상태입니다.");

        AddMockEntry(
            2,
            EventLogLevel.Info,
            "포크리프트가 Pickup 위치에 도착했습니다.");

        AddMockEntry(
            1,
            EventLogLevel.Error,
            "FR5 부품 파지 실패가 발생했습니다.");

        RebuildRows();
        LogsChanged?.Invoke();
    }

    private void AddMockEntry(
        int secondsAgo,
        EventLogLevel level,
        string message)
    {
        entries.Add(
            new EventEntry
            {
                timestamp =
                    DateTime.Now.AddSeconds(-secondsAgo),

                level = level,
                message = message
            });
    }

    public string GetRecentSummary(int count = 5)
    {
        if (entries.Count == 0)
            return "표시할 이벤트가 없습니다.";

        StringBuilder builder = new StringBuilder();
        int addedCount = 0;

        // 최신 이벤트부터 표시
        for (int i = entries.Count - 1;
             i >= 0 && addedCount < count;
             i--)
        {
            EventEntry entry = entries[i];

            builder.Append(entry.timestamp.ToString("HH:mm:ss"));
            builder.Append("  ");
            builder.Append(GetColoredLevel(entry.level));
            builder.Append("  ");
            builder.Append(entry.message);

            addedCount++;

            if (i > 0 && addedCount < count)
                builder.AppendLine();
        }

        return builder.ToString();
    }

    private string GetColoredLevel(EventLogLevel level)
    {
        switch (level)
        {
            case EventLogLevel.Warning:
                return "<color=#EB9B2D>[WARNING]</color>";

            case EventLogLevel.Error:
                return "<color=#D74141>[ERROR]</color>";

            default:
                return "<color=#2DC7D5>[INFO]</color>";
        }
    }
}
