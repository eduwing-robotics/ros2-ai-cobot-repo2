using System;
using UnityEngine;
using UnityEngine.UI;

public class ProductionProcessUI : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("Process")]
    [SerializeField]
    private Transform processFlowPanel;

    [SerializeField]
    private Text processHeaderText;

    private ProcessStepUI[] steps;
    private GameObject transportProgressRoot;
    private Image transportProgressFill;
    private float displayedTransportProgress;

    private void Awake()
    {
        FindAndInitializeSteps();
        FindProcessHeader();
        EnsureTransportProgressBar();
        ResetProcess();
        ResetProcessHeader();
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            Debug.LogError(
                "[ProcessUI] FactoryStateManager가 " +
                "연결되지 않았습니다.",
                this);

            return;
        }

        factoryStateManager.SnapshotApplied +=
            HandleSnapshotApplied;

        factoryStateManager.ProductionStatusUpdated +=
            HandleProductionStatusUpdated;

        factoryStateManager.TransportStatusUpdated +=
            HandleTransportStatusUpdated;

        RefreshFromCurrentState();
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

        factoryStateManager.TransportStatusUpdated -=
            HandleTransportStatusUpdated;
    }

    private void FindAndInitializeSteps()
    {
        if (processFlowPanel == null)
        {
            processFlowPanel = transform;
        }

        steps =
            processFlowPanel.GetComponentsInChildren<
                ProcessStepUI>(true);

        Array.Sort(
            steps,
            CompareStepsByNumber);

        if (steps.Length < FactoryOperationCatalog.StepNames.Length)
        {
            Debug.LogError(
                $"[ProcessUI] 단계 오브젝트가 부족합니다. " +
                $"필요={FactoryOperationCatalog.StepNames.Length}, 현재={steps.Length}",
                this);

            return;
        }

        int usedStepCount =
            FactoryOperationCatalog.StepNames.Length;

        if (steps.Length > usedStepCount)
        {
            Debug.LogWarning(
                $"[ProcessUI] 씬에 사용하지 않는 단계 오브젝트가 있습니다. " +
                $"필요={usedStepCount}, 현재={steps.Length}. " +
                "에디터에서 불필요한 오브젝트를 삭제해 주세요.",
                this);
        }

        for (int i = 0; i < usedStepCount; i++)
        {
            steps[i].Initialize(
                i + 1,
                FactoryOperationCatalog.StepNames[i]);
        }
    }

    private void FindProcessHeader()
    {
        if (processFlowPanel == null)
        {
            return;
        }

        Transform page = processFlowPanel.parent;
        Transform visibleTitle = page != null
            ? page.Find("Title")
            : null;

        if (visibleTitle != null &&
            visibleTitle.TryGetComponent(out Text titleText))
        {
            processHeaderText = titleText;
            return;
        }

        if (processHeaderText != null)
        {
            return;
        }

        for (int i = 0; i < processFlowPanel.childCount; i++)
        {
            Text candidate = processFlowPanel
                .GetChild(i)
                .GetComponent<Text>();

            if (candidate != null)
            {
                processHeaderText = candidate;
                return;
            }
        }
    }

    private static int CompareStepsByNumber(
        ProcessStepUI first,
        ProcessStepUI second)
    {
        int firstNumber = GetStepNumber(first.gameObject.name);
        int secondNumber = GetStepNumber(second.gameObject.name);

        int numberComparison =
            firstNumber.CompareTo(secondNumber);

        return numberComparison != 0
            ? numberComparison
            : string.CompareOrdinal(
                first.gameObject.name,
                second.gameObject.name);
    }

    private static int GetStepNumber(string objectName)
    {
        if (string.IsNullOrWhiteSpace(objectName))
        {
            return int.MaxValue;
        }

        int openIndex = objectName.LastIndexOf('(');
        int closeIndex = objectName.LastIndexOf(')');

        if (openIndex >= 0 && closeIndex > openIndex &&
            int.TryParse(
                objectName.Substring(
                    openIndex + 1,
                    closeIndex - openIndex - 1),
                out int stepNumber))
        {
            return stepNumber;
        }

        return int.MaxValue;
    }

    private void HandleSnapshotApplied(
        ProductionSnapshotData snapshot)
    {
        RefreshFromCurrentState();
    }

    private void HandleProductionStatusUpdated(
        ProductionJobData job)
    {
        UpdateProcess(job);

        if (!factoryStateManager.TryGetActiveTransport(
                out TransportStatusData _))
        {
            UpdateProductionHeader(job);
        }
    }

    private void HandleTransportStatusUpdated(
        TransportStatusData transport)
    {
        Debug.Log(
            $"[ProcessUI] Transport 표시 요청: " +
            $"request={transport?.req_id}, phase={transport?.phase}, " +
            $"progress={transport?.progress:P0}",
            this);

        UpdateTransportHeader(transport);

        // Transport 자체에는 반복되는 운반 단계 중 몇 번째인지가 없으므로
        // 같은 job_id의 production_status가 가진 current_operation을 사용합니다.
        if (transport != null &&
            factoryStateManager.TryGetProductionJob(
                transport.job_id,
                out ProductionJobData matchingJob))
        {
            UpdateProcess(matchingJob);
        }
        else if (TryGetDisplayedJob(
                     out ProductionJobData activeJob))
        {
            UpdateProcess(activeJob);
        }
    }

    private void RefreshFromCurrentState()
    {
        if (TryGetDisplayedJob(
                out ProductionJobData job))
        {
            UpdateProcess(job);
        }
        else
        {
            ResetProcess();
        }


        if (factoryStateManager.TryGetActiveTransport(
                out TransportStatusData transport))
        {
            UpdateTransportHeader(transport);
        }
        else
        {
            if (TryGetDisplayedJob(
                    out ProductionJobData activeJob))
            {
                UpdateProductionHeader(activeJob);
            }
            else
            {
                ResetProcessHeader();
            }
        }
    }

    private bool TryGetDisplayedJob(out ProductionJobData job)
    {
        if (factoryStateManager.TryGetActiveProductionJob(out job)) return true;
        foreach (ProductionJobData candidate in factoryStateManager.ProductionJobs.Values)
            if (candidate != null && (job == null || candidate.numeric_job_id > job.numeric_job_id))
                job = candidate;
        return job != null;
    }

    private void UpdateProcess(ProductionJobData job)
    {
        if (job == null || steps == null)
        {
            return;
        }

        if (FactoryOperationCatalog.IsStopped(job))
        {
            ResetProcess();
            UpdateProductionHeader(job);
            return;
        }

        // 생산 완료는 Operation이 아니라
        // 서버 job_status로 판단합니다.
        if (FactoryOperationCatalog.JobStatus(job) == "COMPLETED")
        {
            SetAllCompleted();
            return;
        }

        if (!FactoryOperationCatalog.TryGetProcessStep(
                job,
                out int currentStep))
        {
            ResetProcess();
            Debug.LogWarning(
                $"[ProcessUI] 등록되지 않은 Operation: " +
                $"{job.current_operation}",
                this);

            return;
        }

        steps[currentStep].Initialize(currentStep + 1,
            FactoryOperationCatalog.GetProcessDisplayName(job));
        bool isPaused = IsPausedControlState(job.control_state);

        bool hasError =
            job.job_status == "FAILED" ||
            !string.IsNullOrWhiteSpace(job.error_code);

        for (int i = 0; i < FactoryOperationCatalog.StepNames.Length; i++)
        {
            if (i < currentStep)
            {
                steps[i].SetState(
                    ProcessStepUI.StepState.Completed);
            }
            else if (i == currentStep)
            {
                if (hasError)
                {
                    steps[i].SetState(
                        ProcessStepUI.StepState.Error);
                }
                else if (isPaused)
                {
                    steps[i].SetState(
                        ProcessStepUI.StepState.Paused);
                }
                else
                {
                    steps[i].SetState(
                        ProcessStepUI.StepState.Current);
                }
            }
            else
            {
                steps[i].SetState(
                    ProcessStepUI.StepState.Pending);
            }

            steps[i].SetConnectorCompleted(
                i < currentStep);
        }
    }

    private void SetAllCompleted()
    {
        for (int i = 0; i < FactoryOperationCatalog.StepNames.Length; i++)
        {
            ProcessStepUI step = steps[i];
            step.SetState(
                ProcessStepUI.StepState.Completed);

            step.SetConnectorCompleted(true);
        }
    }

    private void ResetProcess()
    {
        if (steps == null)
        {
            return;
        }

        for (int i = 0; i < FactoryOperationCatalog.StepNames.Length; i++)
        {
            ProcessStepUI step = steps[i];
            step.SetState(
                ProcessStepUI.StepState.Pending);

            step.SetConnectorCompleted(false);
        }
    }

    private void UpdateProductionHeader(ProductionJobData job)
    {
        if (processHeaderText == null || job == null)
        {
            return;
        }

        processHeaderText.text = FactoryOperationCatalog.IsStopped(job)
            ? "작업 프로세스 · " + FactoryOperationCatalog.GetProcessDisplayName(job)
            : "작업 프로세스" + FormatControlStateSuffix(job.control_state);

        if (transportProgressRoot != null)
        {
            transportProgressRoot.SetActive(false);
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

    private static string FormatControlStateSuffix(string controlState)
    {
        switch ((controlState ?? string.Empty).Trim().ToUpperInvariant())
        {
            case "PAUSE_REQUESTED":
                return " · 일시정지 요청 중";
            case "PAUSED":
                return " · 일시정지";
            case "RESUME_REQUESTED":
                return " · 재개 요청 중";
            default:
                return string.Empty;
        }
    }

    private void UpdateTransportHeader(
        TransportStatusData transport)
    {
        if (TryGetDisplayedJob(out ProductionJobData displayedJob) &&
            FactoryOperationCatalog.IsStopped(displayedJob))
        {
            UpdateProductionHeader(displayedJob);
            return;
        }
        if (transport == null)
        {
            ResetProcessHeader();
            return;
        }

        if (processHeaderText == null)
        {
            Debug.LogWarning(
                "[ProcessUI] Transport를 받았지만 작업 프로세스 제목 Text를 찾지 못했습니다.",
                this);
        }

        string result = string.IsNullOrWhiteSpace(transport.result)
            ? FormatTransportPhase(transport.phase)
            : FormatTransportResult(transport.result);

        int progress = Mathf.RoundToInt(
            Mathf.Clamp01(transport.progress) * 100f);

        if (processHeaderText != null)
        {
            processHeaderText.text =
                $"작업 프로세스 · {result} {progress}%";
        }

        EnsureTransportProgressBar();
        displayedTransportProgress =
            Mathf.Clamp01(transport.progress);

        if (transportProgressRoot != null)
        {
            transportProgressRoot.SetActive(true);
        }

        if (transportProgressFill != null)
        {
            transportProgressFill.fillAmount =
                displayedTransportProgress;

            bool completed = IsSuccessfulResult(transport.result);
            bool canceled = IsCanceledResult(transport.result);
            bool failed =
                !string.IsNullOrWhiteSpace(transport.error_code) ||
                (!string.IsNullOrWhiteSpace(transport.result) &&
                 !completed && !canceled);

            transportProgressFill.color = failed
                ? new Color32(218, 76, 76, 255)
                : completed
                    ? new Color32(52, 168, 83, 255)
                    : canceled
                        ? new Color32(145, 155, 165, 255)
                        : new Color32(62, 133, 235, 255);
        }

        Debug.Log(
            $"[ProcessUI] Transport 화면 반영 완료: " +
            $"{result} {progress}%, progress-bar={displayedTransportProgress:P0}",
            this);
    }

    private void ResetProcessHeader()
    {
        if (processHeaderText != null)
        {
            processHeaderText.text = "작업 프로세스";
        }

        displayedTransportProgress = 0f;

        if (transportProgressFill != null)
        {
            transportProgressFill.fillAmount = 0f;
        }

        if (transportProgressRoot != null)
        {
            transportProgressRoot.SetActive(false);
        }
    }

    private void EnsureTransportProgressBar()
    {
        if (transportProgressRoot != null ||
            processFlowPanel == null)
        {
            return;
        }

        transportProgressRoot = new GameObject(
            "TransportProgressBar",
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(Image));

        RectTransform rootRect =
            transportProgressRoot.GetComponent<RectTransform>();
        rootRect.SetParent(processFlowPanel, false);
        rootRect.anchorMin = new Vector2(0.02f, 1f);
        rootRect.anchorMax = new Vector2(0.98f, 1f);
        rootRect.pivot = new Vector2(0.5f, 1f);
        rootRect.anchoredPosition = new Vector2(0f, -2f);
        rootRect.sizeDelta = new Vector2(0f, 6f);

        Image background =
            transportProgressRoot.GetComponent<Image>();
        background.color = new Color32(207, 214, 224, 255);
        background.raycastTarget = false;

        GameObject fillObject = new GameObject(
            "Fill",
            typeof(RectTransform),
            typeof(CanvasRenderer),
            typeof(Image));

        RectTransform fillRect =
            fillObject.GetComponent<RectTransform>();
        fillRect.SetParent(rootRect, false);
        fillRect.anchorMin = Vector2.zero;
        fillRect.anchorMax = Vector2.one;
        fillRect.offsetMin = Vector2.zero;
        fillRect.offsetMax = Vector2.zero;

        transportProgressFill = fillObject.GetComponent<Image>();
        transportProgressFill.color =
            new Color32(62, 133, 235, 255);
        transportProgressFill.type = Image.Type.Filled;
        transportProgressFill.fillMethod = Image.FillMethod.Horizontal;
        transportProgressFill.fillOrigin = 0;
        transportProgressFill.fillAmount = 0f;
        transportProgressFill.raycastTarget = false;

        transportProgressRoot.transform.SetAsLastSibling();
        transportProgressRoot.SetActive(false);
    }

    public bool IsTransportProgressDisplayed(
        float expectedProgress,
        float tolerance = 0.01f)
    {
        return transportProgressRoot != null &&
               transportProgressRoot.activeInHierarchy &&
               Mathf.Abs(
                   displayedTransportProgress -
                   Mathf.Clamp01(expectedProgress)) <= tolerance;
    }

    public bool IsProcessStepCurrent(int oneBasedStepNumber)
    {
        int index = oneBasedStepNumber - 1;

        return steps != null &&
               index >= 0 &&
               index < steps.Length &&
               steps[index] != null &&
               steps[index].CurrentState ==
                   ProcessStepUI.StepState.Current;
    }

    private static string FormatTransportPhase(string phase)
    {
        switch (phase)
        {
            case "MOVING_TO_PICKUP":
                return "Pickup 이동 중";
            case "DOCKING_PICKUP":
                return "Pickup 정렬 중";
            case "LIFTING_UP":
                return "적재 중";
            case "LEAVING_PICKUP":
                return "Pickup 이탈 중";
            case "MOVING_TO_DROPOFF":
            case "DELIVERING_DROPOFF":
                return "Dropoff 이동 중";
            case "DOCKING_DROPOFF":
                return "Dropoff 정렬 중";
            case "LIFTING_DOWN":
                return "하역 중";
            case "LEAVING_DROPOFF":
                return "Dropoff 이탈 중";
            case "RETURNING_HOME":
                return "HOME 복귀 중";
            case "PARKING_LIFT":
                return "리프트 주차 중";
            case "COMPLETED":
                return "운반 완료";
            default:
                return DisplayValue(phase);
        }
    }

    private static string FormatTransportResult(string result)
    {
        if (IsSuccessfulResult(result))
        {
            return "운반 완료";
        }

        if (IsCanceledResult(result))
        {
            return "운반 취소";
        }

        return "운반 실패";
    }

    private static bool IsSuccessfulResult(string result)
    {
        string normalized = (result ?? string.Empty)
            .Trim()
            .ToUpperInvariant();

        return normalized == "SUCCESS" ||
               normalized == "SUCCEEDED" ||
               normalized == "COMPLETED";
    }

    private static bool IsCanceledResult(string result)
    {
        string normalized = (result ?? string.Empty)
            .Trim()
            .ToUpperInvariant();

        return normalized == "CANCELED" ||
               normalized == "CANCELLED";
    }

    private static string DisplayValue(string value)
    {
        return string.IsNullOrWhiteSpace(value) ? "운반 중" : value;
    }
}
