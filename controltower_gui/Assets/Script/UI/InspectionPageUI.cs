using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using UnityEngine;
using UnityEngine.UI;

public class InspectionPageUI : MonoBehaviour
{
    private static readonly string[] PreRoofViewOrder =
        { "TOP", "LEFT", "RIGHT", "FRONT", "BEHIND" };

    public enum FeedState
    {
        Waiting,
        Online,
        Inspecting,
        Passed,
        Failed,
        Hold,
        GateWaiting,
        NotEvaluated,
        Offline
    }
    public enum InspectionTarget { None, Incoming, Assembly }

    [Header("Incoming inspection camera")]
    [SerializeField] private FactoryStateManager factoryStateManager;
    [SerializeField] private RawImage incomingInspectionImage;
    [SerializeField] private Text incomingStatusText;
    [SerializeField] private Text incomingPlaceholderText;
    [SerializeField] private Text incomingDetailText;
    [SerializeField] private Texture incomingSuccessReferenceTexture;

    [Header("Assembly inspection camera")]
    [SerializeField] private RawImage assemblyInspectionImage;
    [SerializeField] private Text assemblyStatusText;
    [SerializeField] private Text assemblyPlaceholderText;
    [SerializeField] private Text assemblyDetailText;

    public InspectionTarget ActiveTarget { get; private set; }
    public FeedState AssemblyFeedState { get; private set; } = FeedState.Waiting;
    public string CurrentAssemblyView { get; private set; } = string.Empty;
    public int IncomingPassedInspectionCount => incomingPassMessages.Count;
    public event Action<InspectionTarget> CameraStartRequested;
    public event Action<InspectionTarget> CameraStopRequested;

    public bool ShouldReceiveFrames(InspectionTarget target)
    {
        if (target == InspectionTarget.Incoming)
        {
            return !incomingSucceeded;
        }

        if (target == InspectionTarget.Assembly)
        {
            return !assemblySucceeded;
        }

        return false;
    }

    private bool incomingSucceeded;
    private bool assemblySucceeded;
    private Texture2D incomingSuccessSnapshot;
    private Texture2D assemblySuccessSnapshot;
    private string currentIncomingTransactionKey;
    private string currentAssemblyInspectionKey;
    private string blockedIncomingMode;
    private string blockedIncomingFailureDetail;
    private double incomingCaptureAt = -1;
    private long currentIncomingJobId = long.MinValue;
    private readonly Dictionary<string, string> incomingPassMessages =
        new Dictionary<string, string>();
    private readonly List<string> incomingPassOrder =
        new List<string>();
    private readonly Dictionary<string, IncomingQaStatusData>
        latestIncomingStatusByMode =
            new Dictionary<string, IncomingQaStatusData>();

    private void Awake()
    {
        incomingInspectionImage = CreateAspectFitSurface(
            incomingInspectionImage,
            "IncomingVideoAspectFit");
        assemblyInspectionImage = CreateAspectFitSurface(
            assemblyInspectionImage,
            "AssemblyVideoAspectFit");

        ResetIncomingInspection();
        ResetAssemblyInspection();
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            factoryStateManager = FindAnyObjectByType<FactoryStateManager>();
        }

        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.IncomingQaStatusUpdated +=
            HandleIncomingQaStatusUpdated;
        factoryStateManager.ProductionInspectionStatusUpdated +=
            HandleProductionInspectionStatusUpdated;

        factoryStateManager.ProductionStatusUpdated += HandleCurrentJobChanged;
        factoryStateManager.SnapshotApplied += HandleInspectionSnapshot;
        RefreshCurrentInspectionJob(true);
    }

    private void HandleCurrentJobChanged(ProductionJobData job)
    {
        RefreshCurrentInspectionJob(false);
    }

    private void HandleInspectionSnapshot(ProductionSnapshotData snapshot)
    {
        RefreshCurrentInspectionJob(true);
    }

    // Production state, never QA arrival order, owns the displayed job.
    private void RefreshCurrentInspectionJob(bool force)
    {
        long jobId = long.MinValue;
        if (factoryStateManager != null &&
            factoryStateManager.TryGetActiveProductionJob(out ProductionJobData job))
        {
            jobId = job.numeric_job_id;
            if (jobId <= 0 && !long.TryParse(job.job_id, out jobId))
                jobId = long.MinValue;
        }
        bool jobChanged = jobId != currentIncomingJobId;

        if (!force && !jobChanged) return;

        currentIncomingJobId = jobId;
        currentIncomingTransactionKey = null;
        currentAssemblyInspectionKey = null;

        // 페이지 재진입 또는 같은 Job의 Snapshot 갱신만으로 앞선
        // Transaction의 통과 기록을 지우지 않는다. 검사 화면은 실제
        // current_job_id가 바뀔 때만 새 작업 기준으로 초기화한다.
        if (jobChanged)
        {
            ActiveTarget = InspectionTarget.None;
            ResetIncomingInspection();
            ResetAssemblyInspection();
        }

        if (jobId == long.MinValue) return;

        // FactoryStateManager retains histories per job/transaction, including
        // late results. Replay only this job when opening/switching the page.
        var statuses = new List<IncomingQaStatusData>();
        foreach (var status in factoryStateManager.IncomingQaStatuses.Values)
            if (status != null && status.job_id == jobId) statuses.Add(status);
        statuses.Sort((a, b) => {
            int cycle = (a.transaction?.cycle ?? 0).CompareTo(b.transaction?.cycle ?? 0);
            return cycle != 0 ? cycle :
                (a.transaction?.transaction_id ?? 0).CompareTo(b.transaction?.transaction_id ?? 0);
        });
        foreach (var status in statuses) HandleIncomingQaStatusUpdated(status);

        ProductionInspectionStatusData latest = null;
        foreach (var status in factoryStateManager.ProductionInspectionStatuses.Values)
            if (status != null && status.job_id == jobId &&
                Normalize(status.inspection_type) == "PRE_ROOF" &&
                IsNewerProductionInspectionStatus(status, latest)) latest = status;
        if (latest != null) HandleProductionInspectionStatusUpdated(latest);
    }

    private void OnDisable()
    {
        if (factoryStateManager != null)
        {
            factoryStateManager.ProductionStatusUpdated -= HandleCurrentJobChanged;
            factoryStateManager.SnapshotApplied -= HandleInspectionSnapshot;
            factoryStateManager.IncomingQaStatusUpdated -=
                HandleIncomingQaStatusUpdated;
            factoryStateManager.ProductionInspectionStatusUpdated -=
                HandleProductionInspectionStatusUpdated;
        }
    }

    public void BeginIncomingInspection()
    {
        incomingSucceeded = false;
        incomingCaptureAt = -1;
        ActiveTarget = InspectionTarget.Incoming;
        ReleaseSnapshot(ref incomingSuccessSnapshot);
        ApplyTexture(
            incomingInspectionImage,
            incomingPlaceholderText,
            null,
            FeedState.Inspecting);
        SetStatus(incomingStatusText, FeedState.Inspecting);
        RenderIncomingHistory("현재 수입검사 진행 중");
        CameraStartRequested?.Invoke(InspectionTarget.Incoming);
    }

    public void BeginAssemblyInspection()
    {
        // 조립검사 중에는 왼쪽 영상 칸에 지정된 수입검사 이미지를
        // 표시하고, 상태와 판정 문구는 기존 UI 텍스트로 렌더링합니다.
        ShowIncomingSuccessReference();
        assemblySucceeded = false;
        ActiveTarget = InspectionTarget.Assembly;
        ResetAssemblyInspection();
        AssemblyFeedState = FeedState.Inspecting;
        SetStatus(assemblyStatusText, FeedState.Inspecting);
        CameraStartRequested?.Invoke(InspectionTarget.Assembly);
    }

    public void EndInspection()
    {
        ActiveTarget = InspectionTarget.None;
    }

    public void SetIncomingInspectionFeed(Texture texture, FeedState state, string detail = null)
    {
        if (incomingSucceeded) return;
        ActiveTarget = InspectionTarget.Incoming;
        ApplyFeed(incomingInspectionImage, incomingStatusText,
            incomingPlaceholderText, incomingDetailText,
            texture, state, detail);
    }

    public void SetAssemblyInspectionFeed(Texture texture, FeedState state, string detail = null)
    {
        if (assemblySucceeded) return;
        ActiveTarget = InspectionTarget.Assembly;
        AssemblyFeedState = state;
        ApplyFeed(assemblyInspectionImage, assemblyStatusText,
            assemblyPlaceholderText, assemblyDetailText,
            texture, state, detail);
    }

    public void SetIncomingFrame(Texture texture)
    {
        if (incomingSucceeded) return;
        ActiveTarget = InspectionTarget.Incoming;
        ApplyTexture(incomingInspectionImage, incomingPlaceholderText,
            texture, FeedState.Inspecting);
    }

    public void SetAssemblyFrame(Texture texture)
    {
        if (assemblySucceeded) return;
        ActiveTarget = InspectionTarget.Assembly;
        ApplyTexture(assemblyInspectionImage, assemblyPlaceholderText,
            texture, FeedState.Inspecting);
    }

    public void SetIncomingDefect(string description)
    {
        SetDefectText(incomingDetailText, description, false, true);
    }

    public void SetAssemblyDefect(string description)
    {
        SetDefectText(assemblyDetailText, description, false, true);
    }

    public void CompleteIncomingInspection(bool passed, string defectDescription = null)
    {
        SetStatus(
            incomingStatusText,
            passed ? FeedState.Passed : FeedState.Failed);
        RenderIncomingHistory(
            passed ? "전체 수입검사 통과" : defectDescription,
            !passed);

        if (passed)
        {
            // 중복 PASS 메시지는 캡처 시각을 뒤로 미루지 않습니다.
            if (!incomingSucceeded && incomingCaptureAt < 0)
                incomingCaptureAt = Time.realtimeSinceStartupAsDouble + 3.0;
        }
        else
        {
            incomingCaptureAt = -1;
            incomingSucceeded = false;
            ReleaseSnapshot(ref incomingSuccessSnapshot);
            CameraStartRequested?.Invoke(InspectionTarget.Incoming);
        }
    }

    // 비활성 검사 페이지에서도 Managers의 영상 수신기가 호출합니다.
    // timeScale과 무관하게 PASS 후 3초 동안 최신 프레임을 받고 캡처합니다.
    public void TickIncomingSuccessCapture()
    {
        if (factoryStateManager != null && !isActiveAndEnabled)
            RefreshCurrentInspectionJob(false);
        if (incomingCaptureAt < 0 ||
            Time.realtimeSinceStartupAsDouble < incomingCaptureAt)
            return;
        incomingCaptureAt = -1;
        FreezeSuccessfulFrame(incomingInspectionImage, ref incomingSuccessSnapshot);
        incomingSucceeded = true;
        if (ActiveTarget == InspectionTarget.Incoming)
            ActiveTarget = InspectionTarget.None;
        CameraStopRequested?.Invoke(InspectionTarget.Incoming);
    }

    private void Update()
    {
        TickIncomingSuccessCapture();
    }

    public void CompleteAssemblyInspection(bool passed, string defectDescription = null)
    {
        AssemblyFeedState = passed ? FeedState.Passed : FeedState.Failed;
        ApplyResult(assemblyStatusText, assemblyDetailText,
            passed, defectDescription);

        if (passed)
        {
            assemblySucceeded = true;
            FreezeSuccessfulFrame(
                assemblyInspectionImage,
                ref assemblySuccessSnapshot);
            ActiveTarget = InspectionTarget.None;
            CameraStopRequested?.Invoke(InspectionTarget.Assembly);
        }
    }

    // PRE_ROOF HOLD는 검사만 멈춘 상태입니다. 영상 수신은 계속 유지합니다.
    public void HoldAssemblyInspection(string detail = null)
    {
        assemblySucceeded = false;
        ReleaseSnapshot(ref assemblySuccessSnapshot);
        ActiveTarget = InspectionTarget.Assembly;
        AssemblyFeedState = FeedState.Hold;
        SetStatus(assemblyStatusText, FeedState.Hold);
        SetDefectText(
            assemblyDetailText,
            string.IsNullOrWhiteSpace(detail) ? "자재 교체 대기" : detail,
            false,
            false);
        CameraStartRequested?.Invoke(InspectionTarget.Assembly);
    }

    // 영상으로 판정을 추측하지 않고 서버의 NOT_EVALUATED 결과를 그대로 표시합니다.
    public void SetAssemblyInspectionNotEvaluated(string detail = null)
    {
        assemblySucceeded = false;
        ReleaseSnapshot(ref assemblySuccessSnapshot);
        ActiveTarget = InspectionTarget.Assembly;
        AssemblyFeedState = FeedState.NotEvaluated;
        SetStatus(assemblyStatusText, FeedState.NotEvaluated);
        SetDefectText(
            assemblyDetailText,
            string.IsNullOrWhiteSpace(detail)
                ? "검사 불가 또는 재검사 필요"
                : detail,
            false,
            false);
        CameraStartRequested?.Invoke(InspectionTarget.Assembly);
    }

    public void ResetIncomingInspection()
    {
        incomingSucceeded = false;
        ReleaseSnapshot(ref incomingSuccessSnapshot);
        incomingPassMessages.Clear();
        incomingPassOrder.Clear();
        latestIncomingStatusByMode.Clear();
        blockedIncomingMode = null;
        blockedIncomingFailureDetail = null;
        incomingCaptureAt = -1;
        ApplyFeed(incomingInspectionImage, incomingStatusText,
            incomingPlaceholderText, incomingDetailText,
            null, FeedState.Waiting, "판별 대기");
    }

    private void ShowIncomingSuccessReference()
    {
        if (incomingSuccessReferenceTexture == null) return;

        incomingCaptureAt = -1;
        incomingSucceeded = true;
        ReleaseSnapshot(ref incomingSuccessSnapshot);
        ApplyTexture(
            incomingInspectionImage,
            incomingPlaceholderText,
            incomingSuccessReferenceTexture,
            FeedState.Passed);
        SetStatus(incomingStatusText, FeedState.Passed);

        if (incomingDetailText != null)
        {
            incomingDetailText.text =
                "베이스 검사 통과\n" +
                "HOUSE_B 자재 검사 통과\n" +
                "전체 수입검사 통과";
            incomingDetailText.color = new Color32(42, 170, 93, 255);
        }

        CameraStopRequested?.Invoke(InspectionTarget.Incoming);
    }

    private void HandleIncomingQaStatusUpdated(IncomingQaStatusData data)
    {
        IncomingQaTransactionData transaction = data?.transaction;

        if (transaction == null)
        {
            return;
        }

        if (currentIncomingJobId != data.job_id) return;

        string transactionKey = data.job_id + ":" +
            (transaction.transaction_id > 0
                ? transaction.transaction_id.ToString()
                : transaction.request_id);
        string modeKey = Normalize(transaction.mode);
        string state = Normalize(transaction.status);
        string result = Normalize(transaction.overall_result);

        // 재검사 중 늦게 도착한 과거 Transaction이 최신 판정을
        // 덮어쓰지 않도록 모드별 최신 Cycle/Transaction만 적용합니다.
        // 검사 순서와 Gate 전환의 최종 권위는 Main Server입니다.
        if (!ShouldApplyIncomingQaStatus(modeKey, data))
        {
            Debug.LogWarning(
                $"[IncomingQA] 과거 상태 무시: job={data.job_id}, " +
                $"mode={transaction.mode}, cycle={transaction.cycle}, " +
                $"transaction={transaction.transaction_id}, state={state}",
                this);
            return;
        }

        bool isNewTransaction =
            currentIncomingTransactionKey != transactionKey;

        if (isNewTransaction)
        {
            currentIncomingTransactionKey = transactionKey;

            // 서버가 다른 검사 모드로 진행시켰다면 이전 모드의 화면 잠금을
            // 해제합니다. Unity가 서버의 검사 순서를 재판정하지 않습니다.
            if (!string.IsNullOrEmpty(blockedIncomingMode) &&
                modeKey != blockedIncomingMode)
            {
                blockedIncomingMode = null;
                blockedIncomingFailureDetail = null;
            }
        }

        if (state == "REQUESTED" || state == "SENT" || state == "ACKED")
        {
            // 중간 상태는 결과로 표시하지 않습니다. 새 검사가 시작될 때만
            // 이전 성공 화면의 고정을 풀고 UDP 영상을 다시 받습니다.
            if (isNewTransaction)
            {
                BeginIncomingInspection();
                RenderIncomingHistory(
                    !string.IsNullOrEmpty(blockedIncomingMode)
                        ? $"현재 {DisplayMode(transaction.mode)} 재검사 진행 중"
                        : $"현재 {DisplayMode(transaction.mode)} 검사 진행 중");
            }

            return;
        }

        if (state == "ERROR" || state == "REJECTED")
        {
            incomingCaptureAt = -1;
            incomingSucceeded = false;
            ReleaseSnapshot(ref incomingSuccessSnapshot);
            CameraStartRequested?.Invoke(InspectionTarget.Incoming);
            blockedIncomingMode = modeKey;
            blockedIncomingFailureDetail =
                string.IsNullOrWhiteSpace(transaction.error_reason)
                    ? "검사 처리 실패"
                    : transaction.error_reason;
            SetStatus(incomingStatusText, FeedState.Failed);
            RenderIncomingHistory(
                blockedIncomingFailureDetail,
                true);
            return;
        }

        if (state != "COMPLETED")
        {
            return;
        }

        Debug.Log(
            $"[IncomingQA UI APPLY] job={data.job_id}, " +
            $"mode={transaction.mode}, cycle={transaction.cycle}, " +
            $"transaction={transaction.transaction_id}, result={result}, " +
            $"items={BuildIncomingItemSummary(data.items)}",
            this);

        bool hasFailedItem = HasFailedItem(data.items);

        if (result == "PASS" && !hasFailedItem)
        {
            if (modeKey == blockedIncomingMode)
            {
                blockedIncomingMode = null;
                blockedIncomingFailureDetail = null;
            }

            RecordIncomingPass(transaction);

            // COMPLETED는 이 Transaction의 종료일 뿐입니다.
            // BASE_AB와 HOUSE_B를 포함한 전체 Gate가 풀린 뒤에만
            // 최종 성공 처리하고 영상을 고정합니다.
            if (IsIncomingGateReleased(data))
            {
                CompleteIncomingInspection(true);
            }
            else
            {
                SetStatus(incomingStatusText, FeedState.Inspecting);
                RenderIncomingHistory("다음 수입검사 대기");
            }

            return;
        }

        if (result == "FAIL" || hasFailedItem)
        {
            blockedIncomingMode = modeKey;
            blockedIncomingFailureDetail = BuildFailureDetail(data);
            CompleteIncomingInspection(
                false,
                blockedIncomingFailureDetail);
            return;
        }

        // COMPLETED이지만 NOT_EVALUATED인 경우 성공/불량으로 오인하지 않습니다.
        incomingCaptureAt = -1;
        SetStatus(incomingStatusText, FeedState.NotEvaluated);
        RenderIncomingHistory("검사 완료 · 판정 결과 없음");
    }

    private void RecordIncomingPass(IncomingQaTransactionData transaction)
    {
        string modeKey = Normalize(transaction?.mode);

        if (string.IsNullOrEmpty(modeKey))
        {
            modeKey = "TRANSACTION_" +
                (transaction?.transaction_id ?? 0);
        }

        string message = $"{DisplayMode(transaction?.mode)} 검사 통과";

        if (!incomingPassMessages.ContainsKey(modeKey))
        {
            incomingPassOrder.Add(modeKey);
        }

        incomingPassMessages[modeKey] = message;
    }

    private void RenderIncomingHistory(
        string trailingMessage,
        bool failed = false)
    {
        if (incomingDetailText == null)
        {
            return;
        }

        List<string> lines = new List<string>();

        for (int i = 0; i < incomingPassOrder.Count; i++)
        {
            string key = incomingPassOrder[i];

            if (incomingPassMessages.TryGetValue(key, out string message))
            {
                lines.Add(
                    "<color=#2AAA5D><b>" +
                    $"{message}</b></color>");
            }
        }

        if (!string.IsNullOrWhiteSpace(trailingMessage))
        {
            bool allPassed = trailingMessage == "전체 수입검사 통과";
            bool inspecting = trailingMessage.Contains("진행 중");

            if (failed)
            {
                lines.Add(
                    "<color=#D74141><b>" +
                    $"{trailingMessage}</b></color>");
            }
            else if (allPassed)
            {
                lines.Add(
                    $"<color=#2AAA5D><b>{trailingMessage}</b></color>");
            }
            else if (inspecting)
            {
                lines.Add(
                    $"<color=#2878D2><b>{trailingMessage}</b></color>");
            }
            else
            {
                lines.Add(trailingMessage);
            }
        }

        incomingDetailText.text = lines.Count == 0
            ? "판별 대기"
            : string.Join("\n", lines);

        incomingDetailText.supportRichText = true;
    }


    private static bool IsIncomingGateReleased(
        IncomingQaStatusData currentStatus)
    {
        // Job-wide Gate의 유일한 최종 권위는 Main Server입니다.
        // 개별 transaction.production_valid 또는 고정 슬롯 목록으로
        // Unity가 전체 통과 여부를 재계산하지 않습니다.
        return Normalize(currentStatus?.job_gate_state) == "RELEASED";
    }

    private void HandleProductionInspectionStatusUpdated(
        ProductionInspectionStatusData data)
    {
        ProductionInspectionData inspection = data?.inspection;

        if (inspection == null || data.job_id != currentIncomingJobId ||
            Normalize(data.inspection_type) != "PRE_ROOF")
        {
            return;
        }

        string inspectionKey = data.job_id + ":" +
            (inspection.inspection_id > 0
                ? inspection.inspection_id.ToString()
                : inspection.inspection_request_id) + ":" +
            inspection.inspection_cycle;
        bool isNewInspection =
            currentAssemblyInspectionKey != inspectionKey;

        if (isNewInspection)
        {
            currentAssemblyInspectionKey = inspectionKey;
            BeginAssemblyInspection();
        }

        string status = Normalize(inspection.status);

        if (status == "PENDING")
        {
            SetAssemblyLiveState(
                FeedState.Waiting,
                inspection,
                "PRE_ROOF 검사 요청 대기");
            return;
        }

        if (status == "RUNNING")
        {
            SetAssemblyLiveState(
                FeedState.Inspecting,
                inspection);
            return;
        }

        if (status == "ERROR")
        {
            string wireError = inspection.transport?.wire_error_code;
            SetAssemblyLiveState(
                FeedState.Failed,
                inspection,
                string.IsNullOrWhiteSpace(wireError)
                    ? "PRE_ROOF 검사 처리 오류"
                    : wireError);
            return;
        }

        if (status != "COMPLETED")
        {
            return;
        }

        string result = Normalize(inspection.result);

        if (result == "FAIL")
        {
            CompleteAssemblyInspection(
                false,
                BuildProductionInspectionDetail(inspection));
            CurrentAssemblyView = Normalize(inspection.current_view);
            RenderAssemblyViewStates(
                inspection,
                BuildProductionInspectionDetail(inspection),
                true);
            return;
        }

        if (result == "NOT_EVALUATED" || string.IsNullOrEmpty(result))
        {
            SetAssemblyInspectionNotEvaluated(
                BuildProductionInspectionDetail(inspection));
            CurrentAssemblyView = Normalize(inspection.current_view);
            RenderAssemblyViewStates(
                inspection,
                BuildProductionInspectionDetail(inspection));
            return;
        }

        if (result != "PASS")
        {
            SetAssemblyInspectionNotEvaluated(
                "알 수 없는 검사 결과: " + inspection.result);
            CurrentAssemblyView = Normalize(inspection.current_view);
            RenderAssemblyViewStates(
                inspection,
                "알 수 없는 검사 결과: " + inspection.result);
            return;
        }

        // PASS만으로 지붕 공정을 허용하거나 영상을 고정하지 않습니다.
        // Server의 Job-wide gate_state가 유일한 최종 권위입니다.
        if (Normalize(inspection.gate_state) == "RELEASED")
        {
            CompleteAssemblyInspection(true);
            CurrentAssemblyView = string.Empty;
            RenderAssemblyViewStates(
                inspection,
                "전체 5개 View 통과 · Roof Gate 해제");
            return;
        }

        SetAssemblyLiveState(
            FeedState.GateWaiting,
            inspection,
            "5개 View 검사 PASS · 생산 Gate 미해제");
    }

    private void SetAssemblyLiveState(
        FeedState state,
        ProductionInspectionData inspection,
        string footer = null)
    {
        assemblySucceeded = false;
        ReleaseSnapshot(ref assemblySuccessSnapshot);
        ActiveTarget = InspectionTarget.Assembly;
        AssemblyFeedState = state;
        SetStatus(assemblyStatusText, state);
        CurrentAssemblyView = Normalize(inspection?.current_view);

        if (state == FeedState.Inspecting &&
            !string.IsNullOrEmpty(CurrentAssemblyView) &&
            assemblyStatusText != null)
        {
            assemblyStatusText.text = "검사 중 · " + CurrentAssemblyView;
        }

        RenderAssemblyViewStates(
            inspection,
            footer,
            state == FeedState.Failed);
        CameraStartRequested?.Invoke(InspectionTarget.Assembly);
    }

    private void RenderAssemblyViewStates(
        ProductionInspectionData inspection,
        string footer = null,
        bool footerIsFailure = false)
    {
        if (assemblyDetailText == null) return;

        var viewsByName = new Dictionary<string, ProductionInspectionViewData>();
        if (inspection?.views != null)
        {
            foreach (ProductionInspectionViewData view in inspection.views)
            {
                string name = Normalize(view?.view_name);
                if (!string.IsNullOrEmpty(name)) viewsByName[name] = view;
            }
        }

        string currentView = Normalize(inspection?.current_view);
        List<string> lines = new List<string>();

        foreach (string viewName in PreRoofViewOrder)
        {
            viewsByName.TryGetValue(viewName, out ProductionInspectionViewData view);
            string viewState = Normalize(view?.status);
            if (string.IsNullOrEmpty(viewState))
                viewState = Normalize(view?.result);
            if (string.IsNullOrEmpty(viewState))
                viewState = "PENDING";

            bool isCurrent = viewName == currentView;
            string color = GetProductionViewStateColor(viewState, isCurrent);
            string displayState = GetProductionViewStateText(
                viewState,
                isCurrent);
            lines.Add(
                $"<color={color}><b>{viewName} · {displayState}</b></color>");
        }

        if (!string.IsNullOrWhiteSpace(footer))
        {
            string footerColor = footerIsFailure ? "#D74141" : "#F4F6F9";
            lines.Add($"<color={footerColor}>{footer}</color>");
        }

        assemblyDetailText.supportRichText = true;
        assemblyDetailText.text = string.Join("\n", lines);
        assemblyDetailText.color = Color.white;
    }

    private static string GetProductionViewStateText(
        string state,
        bool isCurrent)
    {
        if (isCurrent || state == "IN_PROGRESS" || state == "RUNNING")
            return "현재";
        if (state == "PASS") return "통과";
        if (state == "PENDING") return "검사대기";
        if (state == "FAIL" || state == "ERROR") return "불량";
        return string.IsNullOrEmpty(state) ? "검사대기" : state;
    }

    private static string GetProductionViewStateColor(
        string state,
        bool isCurrent)
    {
        if (state == "PASS") return "#2AAA5D";
        if (state == "FAIL" || state == "ERROR") return "#D74141";
        if (state == "IN_PROGRESS" || state == "RUNNING" || isCurrent)
            return "#2DC7D5";
        if (state == "PENDING") return "#919BA5";
        return "#E19123";
    }

    private static string BuildProductionInspectionDetail(
        ProductionInspectionData inspection)
    {
        List<string> details = new List<string>();

        if (inspection?.views != null)
        {
            foreach (ProductionInspectionViewData view in inspection.views)
            {
                if (view == null || Normalize(view.result) == "PASS")
                {
                    continue;
                }

                string viewName = string.IsNullOrWhiteSpace(view.view_name)
                    ? "VIEW"
                    : view.view_name;
                string reason = string.IsNullOrWhiteSpace(view.reason_code)
                    ? Normalize(view.result)
                    : view.reason_code;

                details.Add($"[{viewName}] {reason}");
            }
        }

        if (details.Count > 0)
        {
            return string.Join("\n", details);
        }

        return Normalize(inspection?.result) == "FAIL"
            ? "불량 상세 정보 없음"
            : "검사 완료 · 판정 결과 없음";
    }

    private static bool IsNewerProductionInspectionStatus(
        ProductionInspectionStatusData candidate,
        ProductionInspectionStatusData current)
    {
        if (candidate?.inspection == null)
        {
            return false;
        }

        if (current?.inspection == null)
        {
            return true;
        }

        if (candidate.job_id != current.job_id)
        {
            return candidate.job_id > current.job_id;
        }

        if (candidate.inspection.inspection_cycle !=
            current.inspection.inspection_cycle)
        {
            return candidate.inspection.inspection_cycle >
                   current.inspection.inspection_cycle;
        }

        return candidate.inspection.inspection_id >=
               current.inspection.inspection_id;
    }

    private static string DisplayMode(string mode)
    {
        switch (Normalize(mode))
        {
            case "BASE_AB": return "베이스";
            case "HOUSE_B": return "HOUSE_B 자재";
            case "HOUSE_A": return "HOUSE_A 자재";
            default: return string.IsNullOrWhiteSpace(mode) ? "현재 자재" : mode;
        }
    }

    private static bool HasFailedItem(IncomingQaItemData[] items)
    {
        if (items == null)
        {
            return false;
        }

        foreach (IncomingQaItemData item in items)
        {
            if (item != null && Normalize(item.result) == "FAIL")
            {
                return true;
            }
        }

        return false;
    }

    private static string BuildFailureDetail(IncomingQaStatusData data)
    {
        List<string> failures = new List<string>();

        if (data?.items != null)
        {
            foreach (IncomingQaItemData item in data.items)
            {
                if (item == null || Normalize(item.result) != "FAIL")
                {
                    continue;
                }

                string location = string.IsNullOrWhiteSpace(item.slot_id)
                    ? "슬롯 미상"
                    : item.slot_id;
                string part = string.IsNullOrWhiteSpace(item.expected_part_code)
                    ? "자재 미상"
                    : item.expected_part_code;
                string cause = IncomingQaFailureText.Format(item);

                failures.Add($"[{location}] {part} · {cause}");
            }
        }

        if (failures.Count > 0)
        {
            return string.Join("\n", failures);
        }

        return string.IsNullOrWhiteSpace(data?.transaction?.error_reason)
            ? "불량 상세 정보 없음"
            : data.transaction.error_reason;
    }

    private static bool IsNewerIncomingQaStatus(
        IncomingQaStatusData candidate,
        IncomingQaStatusData current)
    {
        if (candidate?.transaction == null)
        {
            return false;
        }

        if (current?.transaction == null)
        {
            return true;
        }

        if (candidate.transaction.cycle != current.transaction.cycle)
        {
            return candidate.transaction.cycle > current.transaction.cycle;
        }

        return candidate.transaction.transaction_id >
               current.transaction.transaction_id;
    }

    private bool ShouldApplyIncomingQaStatus(
        string modeKey,
        IncomingQaStatusData candidate)
    {
        if (candidate?.transaction == null)
        {
            return false;
        }

        if (!latestIncomingStatusByMode.TryGetValue(
                modeKey,
                out IncomingQaStatusData current) ||
            current?.transaction == null)
        {
            latestIncomingStatusByMode[modeKey] = candidate;
            return true;
        }

        IncomingQaTransactionData incoming = candidate.transaction;
        IncomingQaTransactionData applied = current.transaction;
        bool sameTransaction =
            incoming.transaction_id > 0 &&
            incoming.transaction_id == applied.transaction_id;

        if (!sameTransaction &&
            !string.IsNullOrWhiteSpace(incoming.request_id) &&
            incoming.request_id == applied.request_id)
        {
            sameTransaction = true;
        }

        if (sameTransaction)
        {
            if (IncomingQaStateRank(incoming.status) <
                IncomingQaStateRank(applied.status))
            {
                return false;
            }

            latestIncomingStatusByMode[modeKey] = candidate;
            return true;
        }

        if (!IsNewerIncomingQaStatus(candidate, current))
        {
            return false;
        }

        latestIncomingStatusByMode[modeKey] = candidate;
        return true;
    }

    private static int IncomingQaStateRank(string state)
    {
        switch (Normalize(state))
        {
            case "REQUESTED": return 1;
            case "SENT": return 2;
            case "ACKED": return 3;
            case "COMPLETED":
            case "ERROR":
            case "REJECTED": return 4;
            default: return 0;
        }
    }

    private static string BuildIncomingItemSummary(
        IncomingQaItemData[] items)
    {
        if (items == null || items.Length == 0)
        {
            return "none";
        }

        List<string> summaries = new List<string>();

        foreach (IncomingQaItemData item in items)
        {
            if (item == null)
            {
                continue;
            }

            string defects = item.defects == null || item.defects.Length == 0
                ? "-"
                : string.Join(",", item.defects);
            summaries.Add(
                $"{item.slot_id}:{Normalize(item.result)}:" +
                $"{Normalize(item.failure_type)}:{defects}");
        }

        return summaries.Count == 0
            ? "none"
            : string.Join(" | ", summaries);
    }

    private static string Normalize(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? string.Empty
            : value.Trim().ToUpperInvariant();
    }

    public void ResetAssemblyInspection()
    {
        assemblySucceeded = false;
        ReleaseSnapshot(ref assemblySuccessSnapshot);
        AssemblyFeedState = FeedState.Waiting;
        ApplyFeed(assemblyInspectionImage, assemblyStatusText,
            assemblyPlaceholderText, assemblyDetailText,
            null, FeedState.Waiting, "판별 대기");
    }

    private void OnDestroy()
    {
        ReleaseSnapshot(ref incomingSuccessSnapshot);
        ReleaseSnapshot(ref assemblySuccessSnapshot);
    }

    private static void ApplyResult(Text statusText, Text defectText,
        bool passed, string defectDescription)
    {
        SetStatus(statusText, passed ? FeedState.Passed : FeedState.Failed);
        SetDefectText(defectText, defectDescription, passed, !passed);
    }

    private static void ApplyFeed(RawImage image, Text statusText,
        Text placeholderText, Text detailText, Texture texture,
        FeedState state, string detail)
    {
        ApplyTexture(image, placeholderText, texture, state);
        SetStatus(statusText, state);

        if (detailText != null)
        {
            detailText.text = string.IsNullOrWhiteSpace(detail) ? "-" : detail;
            detailText.color = IndustrialConsoleTheme.TextColor;
        }
    }

    private static void ApplyTexture(RawImage image, Text placeholderText,
        Texture texture, FeedState state)
    {
        if (image != null)
        {
            AspectRatioFitter fitter =
                image.GetComponent<AspectRatioFitter>();

            if (fitter != null && texture != null &&
                texture.width > 0 && texture.height > 0)
            {
                fitter.aspectRatio =
                    (float)texture.width / texture.height;
            }

            if (texture != null && image.texture == null)
                Debug.Log($"[VisionDiag:UI] firstTexture image={image.name} " +
                    $"active={image.isActiveAndEnabled} size={texture.width}x{texture.height} " +
                    $"rect={image.rectTransform.rect.size}", image);
            image.texture = texture;
            image.color = texture == null
                ? IndustrialConsoleTheme.Background
                : Color.white;
        }

        if (placeholderText != null)
        {
            placeholderText.gameObject.SetActive(texture == null);
            placeholderText.text = state == FeedState.Offline
                ? "영상 신호 없음"
                : "검사 영상 대기 중";
        }
    }

    private static RawImage CreateAspectFitSurface(
        RawImage viewport,
        string surfaceName)
    {
        if (viewport == null)
        {
            return null;
        }

        Transform existing = viewport.transform.Find(surfaceName);

        if (existing != null &&
            existing.TryGetComponent(out RawImage existingImage))
        {
            return existingImage;
        }

        // 기존 RawImage는 영상 영역의 어두운 배경으로 남겨 두고,
        // 그 안에 16:9 화면을 FitInParent로 배치합니다.
        // 이렇게 하면 Vision Overlay의 가장자리를 자르지 않습니다.
        viewport.texture = null;
        viewport.color = IndustrialConsoleTheme.Background;

        GameObject surfaceObject = new GameObject(
            surfaceName,
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(RawImage),
            typeof(AspectRatioFitter));

        RectTransform surfaceTransform =
            surfaceObject.GetComponent<RectTransform>();
        surfaceTransform.SetParent(viewport.rectTransform, false);
        surfaceTransform.anchorMin = new Vector2(0.5f, 0.5f);
        surfaceTransform.anchorMax = new Vector2(0.5f, 0.5f);
        surfaceTransform.pivot = new Vector2(0.5f, 0.5f);
        surfaceTransform.anchoredPosition = Vector2.zero;
        surfaceTransform.localScale = Vector3.one;
        surfaceTransform.SetAsFirstSibling();

        AspectRatioFitter fitter =
            surfaceObject.GetComponent<AspectRatioFitter>();
        fitter.aspectMode = AspectRatioFitter.AspectMode.FitInParent;
        fitter.aspectRatio = 16f / 9f;

        RawImage surface = surfaceObject.GetComponent<RawImage>();
        surface.raycastTarget = false;
        surface.color = IndustrialConsoleTheme.Background;
        return surface;
    }

    private static void SetDefectText(
        Text target,
        string description,
        bool passed,
        bool failed)
    {
        if (target == null) return;

        if (passed)
        {
            target.text = "검출된 불량 없음";
            target.color = new Color32(42, 170, 93, 255);
            return;
        }

        target.text = string.IsNullOrWhiteSpace(description)
            ? "불량 상세 정보 대기"
            : description;
        target.color = failed
            ? new Color32(215, 65, 65, 255)
            : IndustrialConsoleTheme.TextColor;
    }

    private static void SetStatus(Text target, FeedState state)
    {
        if (target == null) return;
        target.text = GetStateText(state);
        target.color = GetStateColor(state);
    }

    private static void FreezeSuccessfulFrame(
        RawImage image,
        ref Texture2D snapshot)
    {
        ReleaseSnapshot(ref snapshot);

        if (image == null || image.texture == null)
        {
            return;
        }

        snapshot = CaptureTexture(image.texture);

        if (snapshot != null)
        {
            image.texture = snapshot;
            image.color = Color.white;
        }
    }

    private static Texture2D CaptureTexture(Texture source)
    {
        int width = Mathf.Max(source.width, 1);
        int height = Mathf.Max(source.height, 1);
        RenderTexture temporary = RenderTexture.GetTemporary(
            width, height, 0, RenderTextureFormat.ARGB32);
        RenderTexture previous = RenderTexture.active;

        try
        {
            Graphics.Blit(source, temporary);
            RenderTexture.active = temporary;
            Texture2D captured = new Texture2D(
                width, height, TextureFormat.RGBA32, false);
            captured.name = source.name + "_InspectionSuccess";
            captured.ReadPixels(new Rect(0f, 0f, width, height), 0, 0);
            captured.Apply(false, false);
            return captured;
        }
        finally
        {
            RenderTexture.active = previous;
            RenderTexture.ReleaseTemporary(temporary);
        }
    }

    private static void ReleaseSnapshot(ref Texture2D snapshot)
    {
        if (snapshot == null) return;
        UnityEngine.Object.Destroy(snapshot);
        snapshot = null;
    }

    private static string GetStateText(FeedState state)
    {
        switch (state)
        {
            case FeedState.Online: return "카메라 연결됨";
            case FeedState.Inspecting: return "검사 중";
            case FeedState.Passed: return "검사 성공";
            case FeedState.Failed: return "검사 불합격";
            case FeedState.Hold: return "교체 대기";
            case FeedState.GateWaiting: return "Gate 대기";
            case FeedState.NotEvaluated: return "검사 불가";
            case FeedState.Offline: return "카메라 연결 끊김";
            default: return "대기";
        }
    }

    private static Color GetStateColor(FeedState state)
    {
        switch (state)
        {
            case FeedState.Online:
            case FeedState.Passed:
                return new Color32(42, 170, 93, 255);
            case FeedState.Inspecting:
                return IndustrialConsoleTheme.Cyan;
            case FeedState.Hold:
            case FeedState.GateWaiting:
                return new Color32(225, 145, 35, 255);
            case FeedState.Failed:
            case FeedState.Offline:
                return new Color32(215, 65, 65, 255);
            case FeedState.NotEvaluated:
                return new Color32(145, 105, 35, 255);
            default:
                return new Color32(145, 155, 165, 255);
        }
    }
}

public class VisionUdpVideoReceiverBase : MonoBehaviour
{
    private const int ProtocolHeaderSize = 32;
    private const int MaxPayloadSize = 1200;

    [Header("HMV1 UDP Stream")]
    [SerializeField] private int listenPort = 21010;
    [SerializeField] private int expectedStreamId = 1;
    [SerializeField] private int incompleteFrameTimeoutMs = 200;
    [SerializeField] private int maximumFrameBytes = 4 * 1024 * 1024;

    [Header("UI")]
    [SerializeField] private InspectionPageUI inspectionPage;
    [SerializeField] private InspectionPageUI.InspectionTarget target =
        InspectionPageUI.InspectionTarget.Incoming;
    [SerializeField] private RawImage dashboardMirrorImage;
    [SerializeField] private Text dashboardMirrorPlaceholder;

    [Header("Debug")]
    [SerializeField] private bool logRejectedPackets;

    private readonly object completedFrameLock = new object();
    private byte[] pendingJpeg;
    private ulong pendingTimestampMs;
    private uint pendingFrameId;

    private UdpClient udpClient;
    private Thread receiveThread;
    private volatile bool running;
    private volatile bool acceptFrames = true;
    private Texture2D videoTexture;

    [SerializeField] private bool logVideoDiagnostics = true;
    private int diagReceived, diagPaused, diagHeaderRejected, diagCompleted;
    private int diagExpired, diagSizeRejected, diagReceiveErrors;
    private int diagDecoded, diagDecodeFailed, diagNoTarget, diagUiCalls;
    private string diagLastHeader = "none";
    private double diagNextReport;

    private void ReportVideoDiagnostics()
    {
        if (!logVideoDiagnostics || Time.realtimeSinceStartupAsDouble < diagNextReport)
            return;
        diagNextReport = Time.realtimeSinceStartupAsDouble + 5.0;
        Debug.Log($"[VisionDiag:{listenPort}] running={running} accept={acceptFrames} " +
            $"rx={Volatile.Read(ref diagReceived)} paused={Volatile.Read(ref diagPaused)} " +
            $"headerRejected={Volatile.Read(ref diagHeaderRejected)} " +
            $"complete={Volatile.Read(ref diagCompleted)} expired={Volatile.Read(ref diagExpired)} " +
            $"sizeRejected={Volatile.Read(ref diagSizeRejected)} errors={Volatile.Read(ref diagReceiveErrors)} " +
            $"decoded={diagDecoded} decodeFailed={diagDecodeFailed} noTarget={diagNoTarget} uiCalls={diagUiCalls} " +
            $"page={(inspectionPage != null ? inspectionPage.name : "null")} " +
            $"pageAccept={(inspectionPage != null && inspectionPage.ShouldReceiveFrames(target))} " +
            $"mirror={(dashboardMirrorImage != null ? dashboardMirrorImage.name : "null")} " +
            $"mirrorActive={(dashboardMirrorImage != null && dashboardMirrorImage.isActiveAndEnabled)} " +
            $"header={diagLastHeader}", this);
    }

    private sealed class IncompleteFrame
    {
        public readonly int FrameSize;
        public readonly byte[][] Chunks;
        public readonly bool[] Received;
        public readonly DateTime CreatedUtc = DateTime.UtcNow;
        public int ReceivedCount;

        public IncompleteFrame(int frameSize, int chunkCount)
        {
            FrameSize = frameSize;
            Chunks = new byte[chunkCount][];
            Received = new bool[chunkCount];
        }
    }

    protected void OnEnable()
    {
        if (inspectionPage != null)
        {
            inspectionPage.CameraStartRequested += HandleCameraStart;
            inspectionPage.CameraStopRequested += HandleCameraStop;
            acceptFrames = inspectionPage.ShouldReceiveFrames(target);
        }

        StartReceiver();
    }

    protected void OnDisable()
    {
        if (inspectionPage != null)
        {
            inspectionPage.CameraStartRequested -= HandleCameraStart;
            inspectionPage.CameraStopRequested -= HandleCameraStop;
        }

        StopReceiver();
    }

    protected void OnDestroy()
    {
        StopReceiver();

        if (videoTexture != null)
        {
            Destroy(videoTexture);
            videoTexture = null;
        }
    }

    protected void Update()
    {
        ReportVideoDiagnostics();
        if (inspectionPage != null &&
            target == InspectionPageUI.InspectionTarget.Incoming)
        {
            inspectionPage?.TickIncomingSuccessCapture();
        }

        byte[] jpeg;

        lock (completedFrameLock)
        {
            jpeg = pendingJpeg;
            pendingJpeg = null;
        }

        if (jpeg == null || !acceptFrames ||
            (inspectionPage == null && dashboardMirrorImage == null))
        {
            if (jpeg != null && inspectionPage == null && dashboardMirrorImage == null)
                diagNoTarget++;
            return;
        }

        if (videoTexture == null)
        {
            videoTexture = new Texture2D(2, 2, TextureFormat.RGB24, false)
            {
                name = $"VisionStream{expectedStreamId}"
            };
        }

        if (!videoTexture.LoadImage(jpeg, false))
        {
            diagDecodeFailed++;
            if (logRejectedPackets)
            {
                Debug.LogWarning(
                    $"[VisionVideo:{listenPort}] JPEG Decode 실패", this);
            }

            return;
        }

        if (inspectionPage != null &&
            target == InspectionPageUI.InspectionTarget.Incoming)
        {
            inspectionPage.SetIncomingFrame(videoTexture);
        }
        else if (inspectionPage != null)
        {
            inspectionPage.SetAssemblyFrame(videoTexture);
        }

        diagDecoded++;
        diagUiCalls++;
        UpdateDashboardMirror(videoTexture);
    }

    private void UpdateDashboardMirror(Texture texture)
    {
        if (dashboardMirrorImage == null || texture == null)
        {
            return;
        }

        dashboardMirrorImage.texture = texture;
        dashboardMirrorImage.color = Color.white;

        AspectRatioFitter fitter =
            dashboardMirrorImage.GetComponent<AspectRatioFitter>();
        if (fitter != null && texture.width > 0 && texture.height > 0)
        {
            fitter.aspectRatio = (float)texture.width / texture.height;
        }

        if (dashboardMirrorPlaceholder != null)
        {
            dashboardMirrorPlaceholder.gameObject.SetActive(false);
        }
    }

    private void StartReceiver()
    {
        if (running)
        {
            return;
        }

        try
        {
            udpClient = new UdpClient(
                new IPEndPoint(IPAddress.Any, listenPort));
            udpClient.Client.ReceiveTimeout = 50;
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[VisionVideo:{listenPort}] UDP Bind 실패: " +
                exception.Message,
                this);
            return;
        }

        running = true;
        receiveThread = new Thread(ReceiveLoop)
        {
            IsBackground = true,
            Name = $"VisionUdpVideoReceiver:{listenPort}"
        };
        receiveThread.Start();
    }

    private void StopReceiver()
    {
        running = false;

        try
        {
            udpClient?.Close();
        }
        catch
        {
            // Closing the socket is only used to unblock Receive().
        }

        if (receiveThread != null && receiveThread.IsAlive)
        {
            receiveThread.Join(250);
        }

        receiveThread = null;
        udpClient = null;
    }

    private void ReceiveLoop()
    {
        Dictionary<ulong, IncompleteFrame> frames =
            new Dictionary<ulong, IncompleteFrame>();
        IPEndPoint sender = new IPEndPoint(IPAddress.Any, 0);

        while (running)
        {
            try
            {
                byte[] datagram = udpClient.Receive(ref sender);
                Interlocked.Increment(ref diagReceived);
                if (!acceptFrames) Interlocked.Increment(ref diagPaused);

                if (acceptFrames)
                {
                    ProcessDatagram(datagram, frames);
                }
            }
            catch (SocketException exception)
            {
                if (exception.SocketErrorCode != SocketError.TimedOut && running)
                    Interlocked.Increment(ref diagReceiveErrors);
                if (exception.SocketErrorCode != SocketError.TimedOut &&
                    running && logRejectedPackets)
                {
                    Debug.LogWarning(
                        $"[VisionVideo:{listenPort}] UDP 수신 오류: " +
                        exception.Message);
                }
            }
            catch (ObjectDisposedException)
            {
                break;
            }
            catch (Exception exception)
            {
                if (running) Interlocked.Increment(ref diagReceiveErrors);
                if (running && logRejectedPackets)
                {
                    Debug.LogWarning(
                        $"[VisionVideo:{listenPort}] 패킷 처리 오류: " +
                        exception.Message);
                }
            }

            RemoveExpiredFrames(frames);
        }
    }

    private void ProcessDatagram(
        byte[] datagram,
        Dictionary<ulong, IncompleteFrame> frames)
    {
        if (datagram == null || datagram.Length < ProtocolHeaderSize ||
            datagram[0] != (byte)'H' || datagram[1] != (byte)'M' ||
            datagram[2] != (byte)'V' || datagram[3] != (byte)'1')
        {
            Interlocked.Increment(ref diagHeaderRejected);
            diagLastHeader = "short packet or non-HMV1";
            return;
        }

        byte version = datagram[4];
        byte streamId = datagram[5];
        ushort headerSize = ReadUInt16(datagram, 6);
        uint frameId = ReadUInt32(datagram, 8);
        ulong timestampMs = ReadUInt64(datagram, 12);
        ushort chunkIndex = ReadUInt16(datagram, 20);
        ushort chunkCount = ReadUInt16(datagram, 22);
        uint frameSize = ReadUInt32(datagram, 24);
        ushort payloadSize = ReadUInt16(datagram, 28);
        ushort reserved = ReadUInt16(datagram, 30);
        if (Volatile.Read(ref diagReceived) == 1 || version != 1 ||
            streamId != expectedStreamId || headerSize != ProtocolHeaderSize ||
            reserved != 0 || payloadSize > MaxPayloadSize ||
            datagram.Length != ProtocolHeaderSize + payloadSize ||
            chunkCount == 0 || chunkIndex >= chunkCount || frameSize == 0 ||
            frameSize > maximumFrameBytes)
            diagLastHeader = $"v={version} stream={streamId} header={headerSize} " +
                $"chunk={chunkIndex}/{chunkCount} frameBytes={frameSize} " +
                $"payload={payloadSize} datagram={datagram.Length} reserved={reserved}";

        if (version != 1 || streamId != expectedStreamId ||
            headerSize != ProtocolHeaderSize || reserved != 0 ||
            payloadSize > MaxPayloadSize ||
            datagram.Length != ProtocolHeaderSize + payloadSize ||
            chunkCount == 0 || chunkIndex >= chunkCount ||
            frameSize == 0 || frameSize > maximumFrameBytes)
        {
            Interlocked.Increment(ref diagHeaderRejected);
            return;
        }

        ulong key = ((ulong)streamId << 32) | frameId;

        if (!frames.TryGetValue(key, out IncompleteFrame frame) ||
            frame.FrameSize != (int)frameSize ||
            frame.Chunks.Length != chunkCount)
        {
            frame = new IncompleteFrame((int)frameSize, chunkCount);
            frames[key] = frame;
        }

        if (!frame.Received[chunkIndex])
        {
            byte[] payload = new byte[payloadSize];
            Buffer.BlockCopy(
                datagram, ProtocolHeaderSize, payload, 0, payloadSize);
            frame.Chunks[chunkIndex] = payload;
            frame.Received[chunkIndex] = true;
            frame.ReceivedCount++;
        }

        if (frame.ReceivedCount != frame.Chunks.Length)
        {
            return;
        }

        byte[] jpeg = new byte[frame.FrameSize];
        int offset = 0;

        for (int i = 0; i < frame.Chunks.Length; i++)
        {
            byte[] chunk = frame.Chunks[i];

            if (chunk == null || offset + chunk.Length > jpeg.Length)
            {
                Interlocked.Increment(ref diagSizeRejected);
                frames.Remove(key);
                return;
            }

            Buffer.BlockCopy(chunk, 0, jpeg, offset, chunk.Length);
            offset += chunk.Length;
        }

        frames.Remove(key);

        if (offset != jpeg.Length)
        {
            Interlocked.Increment(ref diagSizeRejected);
            return;
        }
        Interlocked.Increment(ref diagCompleted);

        lock (completedFrameLock)
        {
            if (pendingJpeg == null || timestampMs > pendingTimestampMs ||
                (timestampMs == pendingTimestampMs && frameId > pendingFrameId))
            {
                pendingJpeg = jpeg;
                pendingTimestampMs = timestampMs;
                pendingFrameId = frameId;
            }
        }
    }

    private void RemoveExpiredFrames(
        Dictionary<ulong, IncompleteFrame> frames)
    {
        if (frames.Count == 0)
        {
            return;
        }

        DateTime threshold = DateTime.UtcNow.AddMilliseconds(
            -Mathf.Max(1, incompleteFrameTimeoutMs));
        List<ulong> expired = null;

        foreach (KeyValuePair<ulong, IncompleteFrame> pair in frames)
        {
            if (pair.Value.CreatedUtc >= threshold)
            {
                continue;
            }

            expired ??= new List<ulong>();
            expired.Add(pair.Key);
        }

        if (expired == null)
        {
            return;
        }

        foreach (ulong key in expired)
        {
            Interlocked.Increment(ref diagExpired);
            frames.Remove(key);
        }
    }

    private void HandleCameraStart(InspectionPageUI.InspectionTarget value)
    {
        if (value == target)
        {
            acceptFrames = true;
        }
    }

    private void HandleCameraStop(InspectionPageUI.InspectionTarget value)
    {
        if (value == target)
        {
            acceptFrames = false;

            lock (completedFrameLock)
            {
                pendingJpeg = null;
            }
        }
    }

    private static ushort ReadUInt16(byte[] bytes, int offset)
    {
        return (ushort)((bytes[offset] << 8) | bytes[offset + 1]);
    }

    private static uint ReadUInt32(byte[] bytes, int offset)
    {
        return ((uint)bytes[offset] << 24) |
               ((uint)bytes[offset + 1] << 16) |
               ((uint)bytes[offset + 2] << 8) |
               bytes[offset + 3];
    }

    private static ulong ReadUInt64(byte[] bytes, int offset)
    {
        return ((ulong)ReadUInt32(bytes, offset) << 32) |
               ReadUInt32(bytes, offset + 4);
    }
}
