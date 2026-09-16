using System;
using System.Globalization;
using UnityEngine;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;

public class FactoryStateManager : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private WebSocketManager webSocketManager;

    [Header("Debug")]
    [SerializeField]
    private bool logAcceptedMessages = true;

    public ProductionSnapshotData CurrentSnapshot
    {
        get;
        private set;
    }

    public long LastSequence
    {
        get;
        private set;
    }

    public bool HasInitialSnapshot
    {
        get;
        private set;
    }

    public VoiceRuntimeData CurrentVoiceRuntime
    {
        get;
        private set;
    }

    private readonly Dictionary<string, RobotStatusData>
    robotStatuses =
        new Dictionary<string, RobotStatusData>();

    public IReadOnlyDictionary<string, RobotStatusData>
        RobotStatuses => robotStatuses;

    private readonly Dictionary<string, ProductionJobData>
    productionJobs =
        new Dictionary<string, ProductionJobData>();

    public IReadOnlyDictionary<string, ProductionJobData>
        ProductionJobs => productionJobs;

    private readonly Dictionary<string, TransportStatusData>
    transportStatuses =
        new Dictionary<string, TransportStatusData>();

    public IReadOnlyDictionary<string, TransportStatusData>
        TransportStatuses => transportStatuses;

    private readonly Dictionary<string, IncomingQaStatusData>
    incomingQaStatuses =
        new Dictionary<string, IncomingQaStatusData>();

    public IReadOnlyDictionary<string, IncomingQaStatusData>
        IncomingQaStatuses => incomingQaStatuses;

    private readonly Dictionary<string, ProductionInspectionStatusData>
    productionInspectionStatuses =
        new Dictionary<string, ProductionInspectionStatusData>();

    public IReadOnlyDictionary<string, ProductionInspectionStatusData>
        ProductionInspectionStatuses => productionInspectionStatuses;

    public event Action<TransportStatusData>
        TransportStatusUpdated;

    public event Action<ProductionJobData>
        ProductionStatusUpdated;

    public event Action<RobotStatusData>
        RobotStatusUpdated;

    public event Action<ProductionSnapshotData>
        SnapshotApplied;

    public event Action<RobotJointStateData>
    RobotJointStateReceived;

    public event Action<MobileRobotPoseData>
    MobileRobotPoseReceived;

    public event Action<ErrorEventData>
        ErrorEventReceived;

    public event Action<IncomingQaStatusData>
        IncomingQaStatusUpdated;

    public event Action<ProductionInspectionStatusData>
        ProductionInspectionStatusUpdated;

    public event Action<VoiceRuntimeData>
        VoiceRuntimeUpdated;

    private readonly Dictionary<string, float> nextJointDiagnostic = new Dictionary<string, float>();
    private float nextJointWireDiagnostic;
    private bool waitingForInitialSnapshot = true;
    private readonly Dictionary<string, float> nextRoutineLogTimes =
        new Dictionary<string, float>();

    private bool ShouldLogMessage(string messageType)
    {
        if (!logAcceptedMessages)
        {
            return false;
        }

        string type = messageType ?? string.Empty;

        if (type != "robot_joint_state" &&
            type != "mobile_robot_pose")
        {
            return true;
        }

        float now = Time.unscaledTime;

        if (nextRoutineLogTimes.TryGetValue(type, out float nextTime) &&
            now < nextTime)
        {
            return false;
        }

        nextRoutineLogTimes[type] = now + 2f;
        return true;
    }

    private void OnEnable()
    {
        if (webSocketManager == null)
        {
            Debug.LogError(
                "[FactoryState] WebSocketManager가 연결되지 않았습니다.");

            return;
        }

        webSocketManager.MessageReceived +=
            HandleJsonMessage;

        webSocketManager.StateChanged +=
            HandleConnectionStateChanged;
    }

    private void OnDisable()
    {
        if (webSocketManager == null)
        {
            return;
        }

        webSocketManager.MessageReceived -=
            HandleJsonMessage;

        webSocketManager.StateChanged -=
            HandleConnectionStateChanged;
    }

    private void HandleConnectionStateChanged(
        WebSocketManager.ConnectionState state)
    {
        if (state !=
            WebSocketManager.ConnectionState.Syncing)
        {
            return;
        }

        // 새로운 연결에서는 이전 연결의 sequence를
        // 이어서 비교하지 않습니다.
        LastSequence = 0;
        HasInitialSnapshot = false;
        waitingForInitialSnapshot = true;

        Debug.Log(
            "[FactoryState] 새 Snapshot 대기");
    }

    public void HandleJsonMessage(string json)
    {
        if (string.IsNullOrWhiteSpace(json))
        {
            Debug.LogWarning(
                "[FactoryState] 빈 메시지를 폐기했습니다.");

            return;
        }

        UnityMessageEnvelope header;

        try
        {
            header =
                JsonUtility.FromJson<UnityMessageEnvelope>(
                    json);
        }
        catch (Exception exception)
        {
            RequestResync(
                $"JSON 해석 실패: {exception.Message}");

            return;
        }

        if (ShouldLogMessage(header?.type))
        {
            Debug.Log(
                $"[WebSocket RX] type={header?.type}, " +
                $"sequence={header?.sequence}, chars={json.Length}");
        }

        if (header != null && header.type == "robot_joint_state" &&
            Time.unscaledTime >= nextJointWireDiagnostic)
        {
            nextJointWireDiagnostic = Time.unscaledTime + 5f;
            Debug.Log($"[JointDiag:Wire] sequence={header.sequence} last={LastSequence} waitingSnapshot={waitingForInitialSnapshot}");
        }
        if (!ValidateCommonEnvelope(header))
        {
            return;
        }

        // 연결 후 첫 메시지 검증
        if (waitingForInitialSnapshot)
        {
            HandleFirstMessage(header, json);
            return;
        }

        // 중복 또는 과거 메시지 폐기
        if (header.sequence <= LastSequence)
        {
            Debug.LogWarning(
                $"[FactoryState] 중복/과거 메시지 폐기: " +
                $"received={header.sequence}, " +
                $"last={LastSequence}");

            return;
        }

        long expectedSequence = LastSequence + 1;

        // 중간 메시지 유실 감지
        if (header.sequence != expectedSequence)
        {
            RequestResync(
                $"Sequence gap: " +
                $"expected={expectedSequence}, " +
                $"received={header.sequence}");

            return;
        }

        LastSequence = header.sequence;

        if (ShouldLogMessage(header.type))
        {
            Debug.Log(
                $"[FactoryState ACCEPT] type={header.type}, " +
                $"sequence={header.sequence}");
        }

        // 알 수 없는 type도 sequence는 정상 처리하고
        // 연결을 종료하지 않습니다.
        HandleNormalMessage(header, json);
    }
    private void HandleFirstMessage(
    UnityMessageEnvelope header,
    string json)
    {
        if (header.type != "production_snapshot")
        {
            RequestResync(
                $"첫 메시지가 production_snapshot이 아님: " +
                $"{header.type}");

            return;
        }

        if (header.sequence != 1)
        {
            RequestResync(
                $"첫 sequence가 1이 아님: " +
                $"{header.sequence}");

            return;
        }

        if (!IsSupportedSchema(header.schema_version))
        {
            RequestResync(
                $"지원하지 않는 Schema: " +
                $"{header.schema_version}");

            return;
        }

        ProductionSnapshotWireEnvelope wireEnvelope;

        try
        {
            wireEnvelope =
                JObject.Parse(json).ToObject<ProductionSnapshotWireEnvelope>();
        }
        catch (Exception exception)
        {
            RequestResync(
                $"Snapshot 해석 실패: " +
                $"{exception.Message}");

            return;
        }

        if (!ValidateWireSnapshot(wireEnvelope))
        {
            return;
        }

        // 실제 서버 Wire 데이터를 기존 UI용 데이터로 변환
        CurrentSnapshot =
            ConvertWireSnapshot(wireEnvelope.data);

        CurrentVoiceRuntime = CurrentSnapshot.voice_runtime;

        ApplySnapshotRobotGripperData(
            json,
            CurrentSnapshot.robots);

        CurrentSnapshot.incoming_qa =
            ExtractIncomingQaStatuses(json);

        productionJobs.Clear();

        foreach (ProductionJobData job in CurrentSnapshot.jobs)
        {
            if (job == null ||
                string.IsNullOrWhiteSpace(job.job_id))
            {
                continue;
            }

            productionJobs[job.job_id] = job;
        }

        robotStatuses.Clear();

        foreach (RobotStatusData robot in CurrentSnapshot.robots)
        {
            if (robot == null ||
                string.IsNullOrWhiteSpace(robot.robot_id))
            {
                continue;
            }

            robotStatuses[robot.robot_id] = robot;
        }

        transportStatuses.Clear();

        foreach (TransportStatusData transport in
                 CurrentSnapshot.transports)
        {
            if (transport == null ||
                string.IsNullOrWhiteSpace(transport.req_id))
            {
                continue;
            }

            transportStatuses[transport.req_id] = transport;
        }

        incomingQaStatuses.Clear();

        foreach (IncomingQaStatusData status in
                 CurrentSnapshot.incoming_qa)
        {
            StoreIncomingQaStatus(status);
        }

        productionInspectionStatuses.Clear();

        foreach (ProductionInspectionStatusData status in
                 CurrentSnapshot.production_inspections)
        {
            StoreProductionInspectionStatus(status);
        }

        LastSequence = 1;
        HasInitialSnapshot = true;
        waitingForInitialSnapshot = false;

        Debug.Log(
            $"[FactoryState] 실제 Snapshot 적용 완료: " +
            $"jobs={CurrentSnapshot.jobs.Length}, " +
            $"robots={CurrentSnapshot.robots.Length}, " +
            $"transports={CurrentSnapshot.transports.Length}, " +
            $"errors={CurrentSnapshot.active_errors.Length}, " +
            $"incomingQA={CurrentSnapshot.incoming_qa.Length}, " +
            $"productionInspections=" +
            $"{CurrentSnapshot.production_inspections.Length}");

        ProductionJobData clockJob = null;
        foreach (ProductionJobData candidate in productionJobs.Values)
            if (candidate != null && (clockJob == null || candidate.numeric_job_id > clockJob.numeric_job_id))
                clockJob = candidate;
        SnapshotApplied?.Invoke(CurrentSnapshot);

        // 재접속 시 서버가 보내는 latest-effective 검사 결과도
        // 실시간 메시지와 동일한 UI 경로로 복원합니다.
        foreach (IncomingQaStatusData status in
                 CurrentSnapshot.incoming_qa)
        {
            IncomingQaStatusUpdated?.Invoke(status);
        }


        foreach (ProductionInspectionStatusData status in
                 CurrentSnapshot.production_inspections)
        {
            ProductionInspectionStatusUpdated?.Invoke(status);
        }

        VoiceRuntimeUpdated?.Invoke(CurrentVoiceRuntime);

        webSocketManager.MarkSynchronized();
    }

    private bool ValidateCommonEnvelope(
        UnityMessageEnvelope header)
    {
        if (header == null)
        {
            RequestResync("Envelope가 null입니다.");
            return false;
        }

        if (string.IsNullOrWhiteSpace(
                header.schema_version))
        {
            RequestResync(
                "schema_version이 없습니다.");

            return false;
        }

        if (string.IsNullOrWhiteSpace(header.type))
        {
            RequestResync("type이 없습니다.");
            return false;
        }

        if (string.IsNullOrWhiteSpace(header.timestamp))
        {
            RequestResync("timestamp가 없습니다.");
            return false;
        }

        bool validTimestamp =
            DateTimeOffset.TryParse(
                header.timestamp,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal |
                DateTimeStyles.AdjustToUniversal,
                out _);

        if (!validTimestamp)
        {
            RequestResync(
                $"timestamp 형식 오류: " +
                $"{header.timestamp}");

            return false;
        }

        if (header.sequence < 1)
        {
            RequestResync(
                $"잘못된 sequence: {header.sequence}");

            return false;
        }

        return true;
    }

    private bool ValidateWireSnapshot(
     ProductionSnapshotWireEnvelope snapshot)
    {
        if (snapshot == null || snapshot.data == null)
        {
            RequestResync(
                "Snapshot data가 없습니다.");

            return false;
        }

        if (snapshot.data.jobs == null)
        {
            RequestResync(
                "Snapshot jobs 배열이 없습니다.");

            return false;
        }

        if (snapshot.data.robots == null)
        {
            RequestResync(
                "Snapshot robots 배열이 없습니다.");

            return false;
        }

        if (snapshot.data.transports == null)
        {
            RequestResync(
                "Snapshot transports 배열이 없습니다.");

            return false;
        }

        if (snapshot.data.active_errors == null)
        {
            RequestResync(
                "Snapshot active_errors 배열이 없습니다.");

            return false;
        }

        return true;
    }

    private ProductionSnapshotData ConvertWireSnapshot(
    ProductionSnapshotWireData wireData)
    {
        ProductionJobData[] jobs =
            new ProductionJobData[wireData.jobs.Length];

        for (int i = 0; i < wireData.jobs.Length; i++)
        {
            jobs[i] =
                ConvertWireJob(wireData.jobs[i]);
        }

        RobotStatusData[] robots =
            new RobotStatusData[wireData.robots.Length];

        for (int i = 0; i < wireData.robots.Length; i++)
        {
            robots[i] =
                ConvertWireRobot(wireData.robots[i]);
        }

        return new ProductionSnapshotData
        {
            jobs = jobs,
            robots = robots,
            transports = ConvertWireTransports(wireData.transports),
            active_errors = ConvertWireErrors(wireData.active_errors),
            production_inspections =
                wireData.production_inspections ??
                Array.Empty<ProductionInspectionStatusData>(),
            voice_runtime = NormalizeVoiceRuntime(wireData.voice_runtime)
        };
    }

    private static VoiceRuntimeData NormalizeVoiceRuntime(
        VoiceRuntimeData runtime)
    {
        if (runtime == null)
        {
            return new VoiceRuntimeData
            {
                state = "IDLE",
                recent_turns = Array.Empty<VoiceTurnData>()
            };
        }

        runtime.state = string.IsNullOrWhiteSpace(runtime.state)
            ? "IDLE"
            : runtime.state.Trim().ToUpperInvariant();
        runtime.recent_turns = runtime.recent_turns ??
            Array.Empty<VoiceTurnData>();
        return runtime;
    }

    private IncomingQaStatusData[] ExtractIncomingQaStatuses(string json)
    {
        List<IncomingQaStatusData> results =
            new List<IncomingQaStatusData>();
        HashSet<string> keys = new HashSet<string>();

        try
        {
            JObject root = JObject.Parse(json);
            CollectIncomingQaStatuses(
                root["data"], results, keys, null);
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                $"[FactoryState] Snapshot Incoming QA 해석 실패: " +
                exception.Message);
        }

        results.Sort(CompareIncomingQaStatus);
        return results.ToArray();
    }

    private static void CollectIncomingQaStatuses(
        JToken token,
        List<IncomingQaStatusData> results,
        HashSet<string> keys,
        string inheritedGateState)
    {
        if (token == null)
        {
            return;
        }

        if (token is JObject candidate)
        {
            string gateState =
                candidate.Value<string>("job_gate_state");

            if (string.IsNullOrWhiteSpace(gateState))
            {
                gateState = inheritedGateState;
            }

            if (candidate["job_id"] != null &&
                candidate["transaction"] is JObject)
            {
                IncomingQaStatusData status =
                    candidate.ToObject<IncomingQaStatusData>();

                if (status != null &&
                    string.IsNullOrWhiteSpace(status.job_gate_state))
                {
                    status.job_gate_state = gateState;
                }

                string key = GetIncomingQaKey(status);

                if (status != null && keys.Add(key))
                {
                    results.Add(status);
                }

                return;
            }

            // 최종 계약의 Snapshot은 한 Job 아래 transactions[]를 둘 수 있습니다.
            // 실시간 메시지의 단일 transaction 형태로 펼쳐 기존 UI 캐시에 넣습니다.
            if (candidate["job_id"] != null &&
                candidate["transactions"] is JArray transactions)
            {
                long jobId = candidate.Value<long?>("job_id") ?? 0;
                JToken sharedItems = candidate["items"];

                foreach (JToken transactionToken in transactions)
                {
                    JToken transactionBody =
                        transactionToken?["transaction"] ?? transactionToken;

                    IncomingQaStatusData status =
                        new IncomingQaStatusData
                        {
                            job_id = jobId,
                            job_gate_state = gateState,
                            transaction = transactionBody?
                                .ToObject<IncomingQaTransactionData>(),
                            items = (transactionToken?["items"] ?? sharedItems)?
                                .ToObject<IncomingQaItemData[]>() ??
                                Array.Empty<IncomingQaItemData>()
                        };

                    string key = GetIncomingQaKey(status);

                    if (status.transaction != null && keys.Add(key))
                    {
                        results.Add(status);
                    }
                }

                return;
            }

            foreach (JProperty property in candidate.Properties())
            {
                CollectIncomingQaStatuses(
                    property.Value, results, keys, gateState);
            }

            return;
        }

        if (token is JArray array)
        {
            foreach (JToken item in array)
            {
                CollectIncomingQaStatuses(
                    item, results, keys, inheritedGateState);
            }
        }
    }

    private static int CompareIncomingQaStatus(
        IncomingQaStatusData left,
        IncomingQaStatusData right)
    {
        int jobComparison = left.job_id.CompareTo(right.job_id);
        if (jobComparison != 0)
        {
            return jobComparison;
        }

        int cycleComparison = (left.transaction?.cycle ?? 0)
            .CompareTo(right.transaction?.cycle ?? 0);
        if (cycleComparison != 0)
        {
            return cycleComparison;
        }

        return (left.transaction?.transaction_id ?? 0)
            .CompareTo(right.transaction?.transaction_id ?? 0);
    }

    private ProductionJobData ConvertWireJob(
    ProductionJobWireData wireJob)
    {
        if (wireJob == null)
        {
            return null;
        }

        string unityJobId =
            wireJob.job_id.ToString(
                CultureInfo.InvariantCulture);

        ProductionStepWireData readinessStep =
            wireJob.current_step ?? wireJob.next_step;

        string currentOperation =
            !string.IsNullOrWhiteSpace(wireJob.current_step?.operation_code)
                ? wireJob.current_step.operation_code
                : wireJob.current_stage_code;

        return new ProductionJobData
        {
            // 기존 UI가 문자열 ID를 사용하므로 변환
            job_id = unityJobId,

            numeric_job_id = wireJob.job_id,
            job_code = wireJob.job_code,
            product_code = wireJob.product_code,
            status = wireJob.status,
            control_state = wireJob.control_state,
            current_stage_code = wireJob.current_stage_code,
            process_stage_code = wireJob.process_stage_code,
            process_stage_order = wireJob.process_stage_order ?? 0,
            process_stage_display_name = wireJob.process_stage_display_name,

            roof_option_code =
                wireJob.roof_option_code,

            requested_at = wireJob.requested_at,
            started_at = wireJob.started_at,
            completed_at = wireJob.completed_at,

            // 기존 UI 호환 필드
            product = wireJob.product_code,
            job_status = wireJob.status,

            current_step_id = WireStepId(wireJob.current_step),
            current_operation = currentOperation,
            current_step_status = wireJob.current_step?.status,
            current_step_order = wireJob.current_step?.step_order ?? 0,
            current_step_display_name = WireStepDisplayName(
                wireJob.current_step),
            current_step_supply_mode = wireJob.current_step?.supply_mode,

            next_step_id = WireStepId(wireJob.next_step),
            next_operation = wireJob.next_step?.operation_code,
            next_step_order = wireJob.next_step?.step_order ?? 0,
            next_step_display_name = WireStepDisplayName(
                wireJob.next_step),
            next_step_supply_mode = wireJob.next_step?.supply_mode,

            // 실행 중 Step이 없는 대기 구간에서는 next_step이
            // 실제 실행 가능 여부의 기준입니다.
            ready = readinessStep != null && readinessStep.ready,
            readiness_reason = readinessStep?.readiness_reason,
            operator_execution_ready_at =
                readinessStep?.operator_execution_ready_at,

            progress = CalculateDisplayProgress(
                wireJob.status,
                currentOperation,
                wireJob.current_step?.status),

            error_code = null
        };
    }

    private string WireStepId(ProductionStepWireData step)
    {
        return step == null
            ? null
            : step.job_step_id.ToString(CultureInfo.InvariantCulture);
    }

    private static string WireStepDisplayName(ProductionStepWireData step)
    {
        if (step == null)
        {
            return null;
        }

        return !string.IsNullOrWhiteSpace(step.display_name)
            ? step.display_name
            : step.step_name;
    }

    private static float CalculateDisplayProgress(
        string jobStatus,
        string operation,
        string stepStatus)
    {
        if (string.Equals(
                jobStatus,
                "COMPLETED",
                StringComparison.OrdinalIgnoreCase))
        {
            return 1f;
        }

        if (!FactoryOperationCatalog.TryGetStep(operation, out int step))
        {
            return 0f;
        }

        bool stepCompleted = string.Equals(
            stepStatus,
            "COMPLETED",
            StringComparison.OrdinalIgnoreCase);

        int completedStepCount = stepCompleted ? step + 1 : step;
        return Mathf.Clamp01(
            completedStepCount /
            (float)FactoryOperationCatalog.StepNames.Length);
    }

    private TransportStatusData[] ConvertWireTransports(
        TransportStatusWireData[] wireTransports)
    {
        if (wireTransports == null)
        {
            return Array.Empty<TransportStatusData>();
        }

        TransportStatusData[] transports =
            new TransportStatusData[wireTransports.Length];

        for (int i = 0; i < wireTransports.Length; i++)
        {
            transports[i] = ConvertWireTransport(wireTransports[i]);
        }

        return transports;
    }

    private TransportStatusData ConvertWireTransport(
        TransportStatusWireData wire)
    {
        if (wire == null)
        {
            return null;
        }

        return new TransportStatusData
        {
            req_id = wire.req_id,
            job_id = wire.job_id.ToString(CultureInfo.InvariantCulture),
            delivery_id = wire.delivery_id.ToString(CultureInfo.InvariantCulture),
            robot_id = CanonicalizeRobotId(wire.robot_id),
            task_type = wire.task_type,
            phase = wire.phase,
            progress = wire.progress,
            result = !string.IsNullOrWhiteSpace(wire.result)
                ? wire.result
                : wire.status,
            error_code = wire.error_code,
            detail = wire.detail
        };
    }

    private ErrorEventData[] ConvertWireErrors(
        ErrorEventWireData[] wireErrors)
    {
        if (wireErrors == null)
        {
            return Array.Empty<ErrorEventData>();
        }

        ErrorEventData[] errors = new ErrorEventData[wireErrors.Length];

        for (int i = 0; i < wireErrors.Length; i++)
        {
            errors[i] = ConvertWireError(wireErrors[i]);
        }

        return errors;
    }

    private ErrorEventData ConvertWireError(ErrorEventWireData wire)
    {
        if (wire == null)
        {
            return null;
        }

        return new ErrorEventData
        {
            source = wire.source,
            job_id = wire.job_id > 0
                ? wire.job_id.ToString(CultureInfo.InvariantCulture)
                : null,
            step_id = wire.step_id > 0
                ? wire.step_id.ToString(CultureInfo.InvariantCulture)
                : null,
            delivery_id = wire.delivery_id > 0
                ? wire.delivery_id.ToString(CultureInfo.InvariantCulture)
                : null,
            severity = wire.severity,
            error_code = wire.error_code,
            detail = wire.detail,
            recoverable = wire.recoverable
        };
    }

    private RobotStatusData ConvertWireRobot(
    RobotStatusWireData wireRobot)
    {
        if (wireRobot == null)
        {
            return null;
        }

        return new RobotStatusData
        {
            robot_id = CanonicalizeRobotId(wireRobot.robot_id),

            connected = wireRobot.connected,
            ready = wireRobot.ready,
            busy = wireRobot.busy,

            robot_type =
                InferRobotType(CanonicalizeRobotId(wireRobot.robot_id)),

            state =
                ConvertRobotState(
                    wireRobot.connected,
                    wireRobot.ready,
                    wireRobot.busy),

            job_id = null,
            step_id = null,
            delivery_id = null,

            current_operation = null,
            lift_state = wireRobot.lift_state,

            battery = 0f,
            has_battery = false,

            error_code = null
        };
    }

    private string ConvertRobotState(
    bool connected,
    bool ready,
    bool busy)
    {
        if (!connected)
        {
            return "DISCONNECTED";
        }

        if (busy)
        {
            return "WORKING";
        }

        if (ready)
        {
            return "IDLE";
        }

        return "NOT_READY";
    }

    private string InferRobotType(string robotId)
    {
        if (string.IsNullOrWhiteSpace(robotId))
        {
            return "UNKNOWN";
        }

        if (robotId == "fr5")
        {
            return "ROBOT_ARM";
        }

        if (robotId.StartsWith("zkbot"))
        {
            return "ZKBOT";
        }

        if (CanonicalizeRobotId(robotId) == "forklift_01")
        {
            return "MOBILE_FORKLIFT";
        }

        return "UNKNOWN";
    }

    public static string CanonicalizeRobotId(string robotId)
    {
        if (string.IsNullOrWhiteSpace(robotId))
        {
            return robotId;
        }

        string normalized = robotId.Trim()
            .Replace('-', '_')
            .ToLowerInvariant();

        switch (normalized)
        {
            case "forklift_01":
            case "turtlebot_01":
            case "mobile_robot_1":
                return "forklift_01";
            default:
                return robotId.Trim();
        }
    }

    private void HandleNormalMessage(
    UnityMessageEnvelope header,
    string json)
    {
        switch (header.type)
        {
            case "robot_joint_state":

                HandleRobotJointState(json);
                break;

            case "mobile_robot_pose":

                HandleMobileRobotPose(json);
                break;

            case "robot_status":

                HandleRobotStatus(json);
                break;

            case "production_status":

                HandleProductionStatus(json);
                break;

            case "transport_status":

                HandleTransportStatus(json);
                break;

            case "incoming_qa_status":

                HandleIncomingQaStatus(json);
                break;

            case "production_inspection_status":

                HandleProductionInspectionStatus(json);
                break;

            case "voice_runtime_event":

                HandleVoiceRuntimeEvent(json);
                break;

            case "error_event":
                HandleErrorEvent(json);
                break;

            case "production_snapshot":

                Debug.LogWarning(
                    "[FactoryState] 동기화 이후의 추가 " +
                    "production_snapshot을 무시합니다.");
                break;

            default:

                // 새로운 메시지 type 때문에 연결을
                // 종료하지 않고 해당 메시지만 무시합니다.
                Debug.LogWarning(
                    $"[FactoryState] 알 수 없는 type을 무시함: " +
                    $"{header.type}");
                break;
        }
    }

    private void HandleTransportStatus(string json)
    {
        TransportStatusWireEnvelope message;

        try
        {
            message =
                    JsonUtility.FromJson<TransportStatusWireEnvelope>(
                    json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] TransportStatus 해석 실패: " +
                $"{exception.Message}");

            return;
        }

        if (message == null || message.data == null)
        {
            Debug.LogWarning(
                "[FactoryState] TransportStatus data가 없습니다.");

            return;
        }

        TransportStatusData transport =
            ConvertWireTransport(message.data);

        if (string.IsNullOrWhiteSpace(transport.req_id))
        {
            Debug.LogWarning(
                "[FactoryState] TransportStatus에 " +
                "req_id가 없습니다.");

            return;
        }

        if (string.IsNullOrWhiteSpace(transport.robot_id))
        {
            Debug.LogWarning(
                $"[FactoryState] {transport.req_id}에 " +
                "robot_id가 없습니다.");

            return;
        }

        if (string.IsNullOrWhiteSpace(transport.task_type))
        {
            Debug.LogWarning(
                $"[FactoryState] {transport.req_id}에 " +
                "task_type이 없습니다.");

            return;
        }

        if (transport.progress < 0f ||
            transport.progress > 1f)
        {
            Debug.LogWarning(
                $"[FactoryState] {transport.req_id}의 " +
                $"progress가 범위를 벗어났습니다: " +
                $"{transport.progress}");

            transport.progress =
                Mathf.Clamp01(transport.progress);
        }

        transportStatuses[transport.req_id] = transport;

        if (logAcceptedMessages)
        {
            string result =
                string.IsNullOrWhiteSpace(transport.result)
                    ? "진행 중"
                    : transport.result;

            Debug.Log(
                $"[FactoryState] TransportStatus 갱신: " +
                $"request={transport.req_id}, " +
                $"task={transport.task_type}, " +
                $"phase={transport.phase}, " +
                $"progress={transport.progress:P0}, " +
                $"result={result}");
        }

        TransportStatusUpdated?.Invoke(transport);
    }

    private void HandleErrorEvent(string json)
    {
        ErrorEventWireEnvelope message;

        try
        {
            message = JsonUtility.FromJson<ErrorEventWireEnvelope>(json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] ErrorEvent 해석 실패: {exception.Message}");
            return;
        }

        if (message == null || message.data == null)
        {
            Debug.LogWarning("[FactoryState] ErrorEvent data가 없습니다.");
            return;
        }

        ErrorEventData error = ConvertWireError(message.data);

        if (string.IsNullOrWhiteSpace(error.error_code) &&
            string.IsNullOrWhiteSpace(error.detail))
        {
            Debug.LogWarning("[FactoryState] 내용이 없는 ErrorEvent를 무시했습니다.");
            return;
        }

        if (logAcceptedMessages)
        {
            Debug.LogWarning(
                $"[FactoryState] ErrorEvent: " +
                $"source={error.source}, code={error.error_code}, " +
                $"detail={error.detail}");
        }

        ErrorEventReceived?.Invoke(error);
    }

    private void HandleIncomingQaStatus(string json)
    {
        IncomingQaStatusEnvelope message;

        try
        {
            message = JsonUtility.FromJson<IncomingQaStatusEnvelope>(json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] Incoming QA 해석 실패: {exception.Message}");
            return;
        }

        IncomingQaStatusData payload = message?.data;

        if (payload == null)
        {
            Debug.LogWarning(
                "[FactoryState] Incoming QA data가 없습니다.");
            return;
        }

        List<IncomingQaStatusData> statuses =
            ExpandIncomingQaPayload(payload);

        if (statuses.Count == 0)
        {
            Debug.LogWarning(
                "[FactoryState] Incoming QA transaction/transactions가 없습니다.");
            return;
        }

        foreach (IncomingQaStatusData status in statuses)
        {
            StoreIncomingQaStatus(status);

            if (logAcceptedMessages)
            {
                Debug.Log(
                    $"[FactoryState] Incoming QA 갱신: " +
                    $"job={status.job_id}, " +
                    $"transaction={status.transaction.transaction_id}, " +
                    $"mode={status.transaction.mode}, " +
                    $"cycle={status.transaction.cycle}, " +
                    $"status={status.transaction.status}, " +
                    $"result={status.transaction.overall_result}, " +
                    $"jobGate={status.job_gate_state}");
            }

            IncomingQaStatusUpdated?.Invoke(status);
        }
    }

    private static List<IncomingQaStatusData> ExpandIncomingQaPayload(
        IncomingQaStatusData payload)
    {
        List<IncomingQaStatusData> statuses =
            new List<IncomingQaStatusData>();

        if (payload.transaction != null)
        {
            statuses.Add(payload);
            return statuses;
        }

        if (payload.transactions == null)
        {
            return statuses;
        }

        foreach (IncomingQaTransactionData transaction in
                 payload.transactions)
        {
            if (transaction == null)
            {
                continue;
            }

            statuses.Add(new IncomingQaStatusData
            {
                job_id = payload.job_id,
                job_gate_state = payload.job_gate_state,
                transactions = payload.transactions,
                transaction = transaction,
                items = payload.items ?? Array.Empty<IncomingQaItemData>()
            });
        }

        return statuses;
    }

    private void StoreIncomingQaStatus(IncomingQaStatusData status)
    {
        if (status == null || status.transaction == null)
        {
            return;
        }

        incomingQaStatuses[GetIncomingQaKey(status)] = status;
    }

    private void HandleProductionInspectionStatus(string json)
    {
        ProductionInspectionStatusEnvelope message;

        try
        {
            message = JsonUtility.FromJson<
                ProductionInspectionStatusEnvelope>(json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] 생산 품질검사 해석 실패: " +
                exception.Message);
            return;
        }

        ProductionInspectionStatusData status = message?.data;

        if (status == null || status.inspection == null)
        {
            Debug.LogWarning(
                "[FactoryState] 생산 품질검사 inspection이 없습니다.");
            return;
        }

        if (status.job_id <= 0 ||
            string.IsNullOrWhiteSpace(status.inspection_type))
        {
            Debug.LogWarning(
                "[FactoryState] 생산 품질검사 식별 정보가 없습니다.");
            return;
        }

        StoreProductionInspectionStatus(status);

        if (logAcceptedMessages)
        {
            ProductionInspectionData inspection = status.inspection;
            Debug.Log(
                $"[FactoryState] 생산 품질검사 갱신: " +
                $"job={status.job_id}, type={status.inspection_type}, " +
                $"cycle={inspection.inspection_cycle}, " +
                $"status={inspection.status}, result={inspection.result}, " +
                $"currentView={inspection.current_view}, " +
                $"gate={inspection.gate_state}, " +
                $"views={inspection.views?.Length ?? 0}");
        }

        ProductionInspectionStatusUpdated?.Invoke(status);
    }

    private void HandleVoiceRuntimeEvent(string json)
    {
        VoiceRuntimeEventEnvelope message;

        try
        {
            message = JsonUtility.FromJson<VoiceRuntimeEventEnvelope>(json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] 음성 비서 상태 해석 실패: " +
                exception.Message);
            return;
        }

        if (message == null || message.data == null)
        {
            Debug.LogWarning(
                "[FactoryState] voice_runtime_event data가 없습니다.");
            return;
        }

        ApplyVoiceRuntime(message.data, message.timestamp);
    }

    private void ApplyVoiceRuntime(
        VoiceRuntimeData incoming,
        string eventTimestamp)
    {
        VoiceRuntimeData previous = NormalizeVoiceRuntime(
            CurrentVoiceRuntime);
        string state = string.IsNullOrWhiteSpace(incoming.state)
            ? "IDLE"
            : incoming.state.Trim().ToUpperInvariant();

        VoiceRuntimeData current = new VoiceRuntimeData
        {
            state = state,
            turn_id = incoming.turn_id,
            transcript = incoming.transcript,
            response_text = incoming.response_text,
            intent = incoming.intent,
            error_message = incoming.error_message,
            updated_at = string.IsNullOrWhiteSpace(incoming.updated_at)
                ? eventTimestamp
                : incoming.updated_at,
            recent_turns = MergeVoiceTurns(
                previous.recent_turns,
                incoming.recent_turns,
                incoming,
                eventTimestamp)
        };

        CurrentVoiceRuntime = current;

        if (CurrentSnapshot != null)
        {
            CurrentSnapshot.voice_runtime = current;
        }

        if (logAcceptedMessages)
        {
            Debug.Log(
                $"[FactoryState] 음성 비서 상태 갱신: " +
                $"state={current.state}, intent={current.intent ?? "-"}");
        }

        VoiceRuntimeUpdated?.Invoke(current);
    }

    private static VoiceTurnData[] MergeVoiceTurns(
        VoiceTurnData[] previous,
        VoiceTurnData[] supplied,
        VoiceRuntimeData incoming,
        string eventTimestamp)
    {
        List<VoiceTurnData> turns = new List<VoiceTurnData>();

        if (supplied != null && supplied.Length > 0)
        {
            turns.AddRange(supplied);
        }
        else if (previous != null)
        {
            turns.AddRange(previous);
        }

        bool hasCompletedTurn =
            string.Equals(
                incoming.state,
                "RESPONDING",
                StringComparison.OrdinalIgnoreCase) &&
            !string.IsNullOrWhiteSpace(incoming.response_text);

        if (hasCompletedTurn)
        {
            VoiceTurnData turn = new VoiceTurnData
            {
                turn_id = incoming.turn_id,
                timestamp = eventTimestamp,
                transcript = incoming.transcript,
                response_text = incoming.response_text,
                intent = incoming.intent
            };

            int existingIndex = turns.FindIndex(item =>
                IsSameVoiceTurn(item, turn));

            if (existingIndex >= 0)
            {
                turns[existingIndex] = turn;
            }
            else
            {
                turns.Add(turn);
            }
        }

        turns.RemoveAll(item => item == null);

        const int maxRecentTurns = 5;
        if (turns.Count > maxRecentTurns)
        {
            turns.RemoveRange(0, turns.Count - maxRecentTurns);
        }

        return turns.ToArray();
    }

    private static bool IsSameVoiceTurn(
        VoiceTurnData left,
        VoiceTurnData right)
    {
        if (left == null || right == null)
        {
            return false;
        }

        if (!string.IsNullOrWhiteSpace(left.turn_id) &&
            !string.IsNullOrWhiteSpace(right.turn_id))
        {
            return string.Equals(
                left.turn_id,
                right.turn_id,
                StringComparison.OrdinalIgnoreCase);
        }

        return string.Equals(left.transcript, right.transcript) &&
               string.Equals(left.response_text, right.response_text);
    }

    private void StoreProductionInspectionStatus(
        ProductionInspectionStatusData status)
    {
        if (status == null || status.inspection == null)
        {
            return;
        }

        string key = GetProductionInspectionKey(status);

        if (productionInspectionStatuses.TryGetValue(
                key,
                out ProductionInspectionStatusData current) &&
            !IsNewerProductionInspectionStatus(status, current))
        {
            return;
        }

        productionInspectionStatuses[key] = status;
    }

    private static string GetProductionInspectionKey(
        ProductionInspectionStatusData status)
    {
        return status.job_id.ToString(CultureInfo.InvariantCulture) +
               ":" +
               (status.inspection_type ?? string.Empty)
                   .Trim()
                   .ToUpperInvariant();
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

        if (candidate.inspection.inspection_cycle !=
            current.inspection.inspection_cycle)
        {
            return candidate.inspection.inspection_cycle >
                   current.inspection.inspection_cycle;
        }

        return candidate.inspection.inspection_id >=
               current.inspection.inspection_id;
    }

    private static string GetIncomingQaKey(IncomingQaStatusData status)
    {
        IncomingQaTransactionData transaction = status?.transaction;

        if (transaction == null)
        {
            return "missing";
        }

        string transactionKey = transaction.transaction_id > 0
            ? transaction.transaction_id.ToString(CultureInfo.InvariantCulture)
            : transaction.request_id;

        if (string.IsNullOrWhiteSpace(transactionKey))
        {
            transactionKey =
                $"{transaction.mode}:{transaction.cycle}";
        }

        return status.job_id.ToString(CultureInfo.InvariantCulture) +
               ":" + transactionKey;
    }

    private void HandleProductionStatus(string json)
    {
        ProductionStatusWireEnvelope message;

        try
        {
            message =
                JObject.Parse(json).ToObject<ProductionStatusWireEnvelope>();
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] ProductionStatus 해석 실패: " +
                $"{exception.Message}");

            return;
        }

        if (message == null || message.data == null)
        {
            Debug.LogWarning(
                "[FactoryState] ProductionStatus data가 없습니다.");

            return;
        }

        ProductionJobData job = ConvertWireJob(message.data);

        if (string.IsNullOrWhiteSpace(job.job_id))
        {
            Debug.LogWarning(
                "[FactoryState] ProductionStatus에 " +
                "job_id가 없습니다.");

            return;
        }

        if (string.IsNullOrWhiteSpace(job.job_status))
        {
            Debug.LogWarning(
                $"[FactoryState] {job.job_id}의 " +
                "job_status가 없습니다.");

            return;
        }

        if (job.progress < 0f || job.progress > 1f)
        {
            Debug.LogWarning(
                $"[FactoryState] {job.job_id}의 progress가 " +
                $"범위를 벗어났습니다: {job.progress}");

            job.progress =
                Mathf.Clamp01(job.progress);
        }

        productionJobs[job.job_id] = job;

        if (logAcceptedMessages)
        {
            Debug.Log(
                $"[FactoryState] ProductionStatus 갱신: " +
                $"job={job.job_id}, " +
                $"status={job.job_status}, " +
                $"operation={job.current_operation}, " +
                $"progress={job.progress:P0}");
        }

        ProductionStatusUpdated?.Invoke(job);
    }
    private void HandleMobileRobotPose(string json)
    {
        MobileRobotPoseEnvelope message;

        try
        {
            message =
                JsonUtility.FromJson<MobileRobotPoseEnvelope>(
                    json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] Pose JSON 해석 실패: " +
                $"{exception.Message}");

            return;
        }

        if (message == null || message.data == null)
        {
            Debug.LogWarning(
                "[FactoryState] Pose data가 없습니다.");

            return;
        }

        MobileRobotPoseData pose = message.data;

        if (string.IsNullOrWhiteSpace(pose.robot_id))
        {
            Debug.LogWarning(
                "[FactoryState] Pose에 robot_id가 없습니다.");

            return;
        }

        pose.robot_id = CanonicalizeRobotId(pose.robot_id);

        if (pose.position == null ||
            pose.orientation == null)
        {
            Debug.LogWarning(
                $"[FactoryState] {pose.robot_id}의 " +
                "position 또는 orientation이 없습니다.");

            return;
        }

        if (pose.frame_id != "map")
        {
            Debug.LogWarning(
                $"[FactoryState] 예상하지 않은 frame_id: " +
                $"{pose.frame_id}");

            // 모르는 frame이라고 연결을 종료하지는 않습니다.
            return;
        }

        MobileRobotPoseReceived?.Invoke(pose);
    }

    private void HandleRobotStatus(string json)
    {
        RobotStatusWireEnvelope wireEnvelope;

        try
        {
            wireEnvelope =
                JsonUtility.FromJson<
                    RobotStatusWireEnvelope>(json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] RobotStatus 해석 실패: " +
                $"{exception.Message}");

            return;
        }

        if (wireEnvelope == null ||
            wireEnvelope.data == null)
        {
            Debug.LogWarning(
                "[FactoryState] RobotStatus data가 없습니다.");

            return;
        }

        RobotStatusWireData wireStatus =
            wireEnvelope.data;

        if (string.IsNullOrWhiteSpace(wireStatus.robot_id))
        {
            Debug.LogWarning(
                "[FactoryState] RobotStatus에 " +
                "robot_id가 없습니다.");

            return;
        }

        wireStatus.robot_id = CanonicalizeRobotId(wireStatus.robot_id);

        RobotStatusData status;

        // Snapshot에서 이미 생성된 로봇이면
        // 기존 UI용 객체를 가져와서 상태만 갱신합니다.
        if (robotStatuses.TryGetValue(
                wireStatus.robot_id,
                out RobotStatusData existingStatus))
        {
            status = existingStatus;

            status.connected = wireStatus.connected;
            status.ready = wireStatus.ready;
            status.busy = wireStatus.busy;
            status.lift_state = wireStatus.lift_state;

            status.state =
                ConvertRobotState(
                    wireStatus.connected,
                    wireStatus.ready,
                    wireStatus.busy);
        }
        else
        {
            // Snapshot에 없던 로봇이면 새로 생성합니다.
            status =
                ConvertWireRobot(wireStatus);
        }

        ApplyRobotGripperData(json, status);

        robotStatuses[status.robot_id] = status;

        if (logAcceptedMessages)
        {
            Debug.Log(
                $"[FactoryState] 실제 RobotStatus 갱신: " +
                $"robot={status.robot_id}, " +
                $"connected={status.connected}, " +
                $"ready={status.ready}, " +
                $"busy={status.busy}, " +
                $"state={status.state}, " +
                $"grip={(status.has_grip ? status.grip.ToString() : "null")}");
        }

        RobotStatusUpdated?.Invoke(status);
    }

    private void ApplySnapshotRobotGripperData(
        string json,
        RobotStatusData[] robots)
    {
        if (robots == null || robots.Length == 0)
        {
            return;
        }

        try
        {
            JArray wireRobots =
                JObject.Parse(json)["data"]?["robots"] as JArray;

            if (wireRobots == null)
            {
                return;
            }

            Dictionary<string, RobotStatusData> targets =
                new Dictionary<string, RobotStatusData>();

            foreach (RobotStatusData robot in robots)
            {
                if (robot == null ||
                    string.IsNullOrWhiteSpace(robot.robot_id))
                {
                    continue;
                }

                targets[CanonicalizeRobotId(robot.robot_id)] = robot;
            }

            foreach (JObject wireRobot in wireRobots.Children<JObject>())
            {
                string robotId = CanonicalizeRobotId(
                    wireRobot.Value<string>("robot_id"));

                if (string.IsNullOrWhiteSpace(robotId) ||
                    !targets.TryGetValue(robotId, out RobotStatusData target))
                {
                    continue;
                }

                ApplyRobotGripperTokens(wireRobot, target);
            }
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                $"[FactoryState] Snapshot FR5 그리퍼 정보 해석 실패: " +
                exception.Message);
        }
    }

    private void ApplyRobotGripperData(
        string json,
        RobotStatusData status)
    {
        if (status == null)
        {
            return;
        }

        try
        {
            JObject data = JObject.Parse(json)["data"] as JObject;

            if (data != null)
            {
                ApplyRobotGripperTokens(data, status);
            }
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                $"[FactoryState] RobotStatus 그리퍼 정보 해석 실패: " +
                exception.Message);
        }
    }

    private static void ApplyRobotGripperTokens(
        JObject source,
        RobotStatusData target)
    {
        if (source.TryGetValue("grip", out JToken gripToken))
        {
            target.has_grip = TryReadInt(gripToken, out target.grip);
            target.grip = Mathf.Clamp(target.grip, 0, 100);
        }

        if (source.TryGetValue("grip_real", out JToken realToken))
        {
            target.has_grip_real =
                TryReadInt(realToken, out target.grip_real);
            target.grip_real = Mathf.Clamp(target.grip_real, 0, 100);
        }

        if (source.TryGetValue(
                "grip_real_age_s",
                out JToken ageToken))
        {
            target.has_grip_real_age_s =
                TryReadFloat(ageToken, out target.grip_real_age_s);
            target.grip_real_age_s =
                Mathf.Max(0f, target.grip_real_age_s);
        }
    }

    private static bool TryReadInt(JToken token, out int value)
    {
        value = 0;

        if (token == null || token.Type == JTokenType.Null)
        {
            return false;
        }

        return int.TryParse(
            token.ToString(),
            NumberStyles.Integer,
            CultureInfo.InvariantCulture,
            out value);
    }

    private static bool TryReadFloat(JToken token, out float value)
    {
        value = 0f;

        if (token == null || token.Type == JTokenType.Null)
        {
            return false;
        }

        return float.TryParse(
            token.ToString(),
            NumberStyles.Float,
            CultureInfo.InvariantCulture,
            out value);
    }

    public bool TryGetActiveTransport(
    out TransportStatusData activeTransport)
    {
        foreach (TransportStatusData transport in
                 transportStatuses.Values)
        {
            if (transport == null)
            {
                continue;
            }

            // 진행 중에는 result가 null입니다.
            if (string.IsNullOrWhiteSpace(transport.result))
            {
                activeTransport = transport;
                return true;
            }
        }

        activeTransport = null;
        return false;
    }

    // WebSocket 없이 Unity 반영 경로만 검증하는 로컬 테스트용 진입점입니다.
    // 실제 서버 메시지 처리와 같은 이벤트를 발생시키되 sequence 상태는 건드리지 않습니다.
    public void ApplyLocalJointStateForTest(
        RobotJointStateData jointState)
    {
        if (jointState == null)
        {
            return;
        }

        Debug.Log(
            $"[LOCAL TEST RX] robot_joint_state robot={jointState.robot_id}");
        RobotJointStateReceived?.Invoke(jointState);
    }

    public void ApplyLocalMobileRobotPoseForTest(MobileRobotPoseData pose)
    {
        if (pose == null)
        {
            return;
        }

        pose.robot_id = CanonicalizeRobotId(pose.robot_id);
        Debug.Log(
            $"[LOCAL TEST RX] mobile_robot_pose robot={pose.robot_id}");
        MobileRobotPoseReceived?.Invoke(pose);
    }

    public void ApplyLocalProductionStatusForTest(
        ProductionJobData job)
    {
        if (job == null || string.IsNullOrWhiteSpace(job.job_id))
        {
            return;
        }

        productionJobs[job.job_id] = job;
        Debug.Log(
            $"[LOCAL TEST RX] production_status job={job.job_id}, " +
            $"operation={job.current_operation}");
        ProductionStatusUpdated?.Invoke(job);
    }

    public void ApplyLocalTransportStatusForTest(
        TransportStatusData transport)
    {
        if (transport == null ||
            string.IsNullOrWhiteSpace(transport.req_id))
        {
            return;
        }

        transportStatuses[transport.req_id] = transport;
        Debug.Log(
            $"[LOCAL TEST RX] transport_status request={transport.req_id}, " +
            $"phase={transport.phase}, progress={transport.progress:P0}");
        TransportStatusUpdated?.Invoke(transport);
    }

    public void ApplyLocalProductionInspectionForTest(
        ProductionInspectionStatusData status)
    {
        if (status == null || status.inspection == null)
        {
            return;
        }

        StoreProductionInspectionStatus(status);
        Debug.Log(
            $"[LOCAL TEST RX] production_inspection_status " +
            $"job={status.job_id}, status={status.inspection.status}, " +
            $"result={status.inspection.result}, " +
            $"gate={status.inspection.gate_state}");
        ProductionInspectionStatusUpdated?.Invoke(status);
    }

    public void ApplyLocalVoiceRuntimeForTest(VoiceRuntimeData runtime)
    {
        if (runtime == null)
        {
            return;
        }

        Debug.Log(
            $"[LOCAL TEST RX] voice_runtime_event state={runtime.state}");
        ApplyVoiceRuntime(
            runtime,
            DateTimeOffset.Now.ToString("o", CultureInfo.InvariantCulture));
    }

    public bool TryGetTransportStatus(
    string requestId,
    out TransportStatusData transport)
    {
        if (string.IsNullOrWhiteSpace(requestId))
        {
            transport = null;
            return false;
        }

        return transportStatuses.TryGetValue(
            requestId,
            out transport);
    }

    public bool TryGetRobotStatus(
    string robotId,
    out RobotStatusData status)
    {
        if (string.IsNullOrWhiteSpace(robotId))
        {
            status = null;
            return false;
        }

        return robotStatuses.TryGetValue(
            CanonicalizeRobotId(robotId),
            out status);
    }

    private void HandleRobotJointState(string json)
    {
        RobotJointStateEnvelope message;

        try
        {
            message =
                JsonUtility.FromJson<RobotJointStateEnvelope>(
                    json);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[FactoryState] JointState JSON 해석 실패: " +
                $"{exception.Message}");

            return;
        }

        if (message == null || message.data == null)
        {
            Debug.LogWarning(
                "[FactoryState] JointState data가 없습니다.");

            return;
        }

        RobotJointStateData jointState = message.data;

        if (string.IsNullOrWhiteSpace(jointState.robot_id))
        {
            Debug.LogWarning(
                "[FactoryState] JointState에 robot_id가 없습니다.");

            return;
        }

        if (jointState.joint_names == null ||
            jointState.positions == null)
        {
            Debug.LogWarning(
                $"[FactoryState] {jointState.robot_id}의 " +
                "관절 배열이 없습니다.");

            return;
        }

        if (jointState.joint_names.Length !=
            jointState.positions.Length)
        {
            Debug.LogWarning(
                $"[FactoryState] {jointState.robot_id}의 " +
                "joint_names와 positions 길이가 다릅니다. " +
                $"names={jointState.joint_names.Length}, " +
                $"positions={jointState.positions.Length}");

            return;
        }

        if (!nextJointDiagnostic.TryGetValue(jointState.robot_id, out float nextJointLog) ||
            Time.unscaledTime >= nextJointLog)
        {
            nextJointDiagnostic[jointState.robot_id] = Time.unscaledTime + 5f;
            Debug.Log($"[JointDiag:Dispatch] robot={jointState.robot_id} " +
                $"names=[{string.Join(",", jointState.joint_names)}] " +
                $"positionsRaw=[{string.Join(",", jointState.positions)}] " +
                $"listeners={RobotJointStateReceived?.GetInvocationList().Length ?? 0}");
        }
        RobotJointStateReceived?.Invoke(jointState);
    }

    public bool TryGetProductionJob(
    string jobId,
    out ProductionJobData job)
    {
        if (string.IsNullOrWhiteSpace(jobId))
        {
            job = null;
            return false;
        }

        return productionJobs.TryGetValue(jobId, out job);
    }

    public bool TryGetActiveProductionJob(
    out ProductionJobData activeJob)
    {
        activeJob = null;

        foreach (ProductionJobData job in productionJobs.Values)
        {
            if (job == null)
            {
                continue;
            }

            if (IsActiveProductionState(job.job_status))
            {
                if (activeJob == null ||
                    job.numeric_job_id > activeJob.numeric_job_id)
                {
                    activeJob = job;
                }
            }
        }

        return activeJob != null;
    }

    private static bool IsActiveProductionState(string status)
    {
        switch ((status ?? string.Empty).Trim().ToUpperInvariant())
        {
            case "REQUESTED":
            case "QUEUED":
            case "IN_PROGRESS":
            case "RUNNING":
            case "PRE_ROOF_READY":
            case "ROOF_READY":
            case "PAUSED": // 이전 서버 호환. 최종 Pause authority는 control_state입니다.
                return true;
            default:
                return false;
        }
    }

    private bool IsSupportedSchema(
        string schemaVersion)
    {
        if (!Version.TryParse(
                schemaVersion,
                out Version version))
        {
            return false;
        }

        // Schema 1.x 지원
        return version.Major == 1;
    }

    private void RequestResync(string reason)
    {
        Debug.LogError(
            $"[FactoryState] 재동기화 필요: {reason}");

        LastSequence = 0;
        HasInitialSnapshot = false;
        waitingForInitialSnapshot = true;

        if (webSocketManager != null)
        {
            webSocketManager.RequestResync(reason);
        }
    }

    // 서버가 아직 없을 때 Inspector 메뉴로
    // Snapshot 파싱을 시험하기 위한 함수입니다.
    [ContextMenu("Test/Apply Mock Snapshot")]
    private void ApplyMockSnapshot()
    {
        LastSequence = 0;
        HasInitialSnapshot = false;
        waitingForInitialSnapshot = true;

        string mockJson =
            "{" +
            "\"schema_version\":\"1.0\"," +
            "\"type\":\"production_snapshot\"," +
            "\"timestamp\":\"2026-08-13T07:30:00.125Z\"," +
            "\"sequence\":1," +
            "\"data\":{" +
            "\"jobs\":[]," +
            "\"robots\":[]," +
            "\"transports\":[]," +
            "\"active_errors\":[]" +
            "}" +
            "}";

        HandleJsonMessage(mockJson);
    }

    [ContextMenu("Test/Apply Mock ZK Joint State")]
    private void ApplyMockZKJointState()
    {
        // Mock 테스트를 실행할 때마다 먼저
        // sequence 1의 Snapshot을 적용합니다.
        ApplyMockSnapshot();

        string mockJson =
            "{" +
            "\"schema_version\":\"1.0\"," +
            "\"type\":\"robot_joint_state\"," +
            "\"timestamp\":\"2026-08-13T07:30:01.125Z\"," +
            "\"sequence\":2," +
            "\"data\":{" +
            "\"source_timestamp\":\"2026-08-13T07:30:01.110Z\"," +
            "\"robot_id\":\"zkbot2\"," +
            "\"joint_names\":[" +
            "\"a1_joint\"," +
            "\"a2_joint\"," +
            "\"a3_joint\"" +
            "]," +
            "\"positions\":[0.3,-0.5,-0.7]" +
            "}" +
            "}";

        HandleJsonMessage(mockJson);
    }

    [ContextMenu("Test/Apply Mock Forklift Pose")]
    private void ApplyMockForkliftPose()
    {
        // Mock 테스트를 독립적으로 실행할 수 있도록
        // 먼저 sequence 1 Snapshot을 적용합니다.
        ApplyMockSnapshot();

        string mockJson =
            "{" +
            "\"schema_version\":\"1.0\"," +
            "\"type\":\"mobile_robot_pose\"," +
            "\"timestamp\":\"2026-08-13T07:30:01.125Z\"," +
            "\"sequence\":2," +
            "\"data\":{" +
            "\"source_timestamp\":\"2026-08-13T07:30:01.110Z\"," +
            "\"robot_id\":\"forklift_01\"," +
            "\"frame_id\":\"map\"," +
            "\"position\":{" +
            "\"x\":1.0," +
            "\"y\":2.0," +
            "\"z\":0.0" +
            "}," +
            "\"orientation\":{" +
            "\"x\":0.0," +
            "\"y\":0.0," +
            "\"z\":0.0," +
            "\"w\":1.0" +
            "}" +
            "}" +
            "}";

        HandleJsonMessage(mockJson);
    }

    [ContextMenu("Test/Apply Mock Forklift Status")]
    private void ApplyMockForkliftStatus()
    {
        ApplyMockSnapshot();

        string mockJson =
            "{" +
            "\"schema_version\":\"1.0\"," +
            "\"type\":\"robot_status\"," +
            "\"timestamp\":\"2026-08-13T07:30:01.100Z\"," +
            "\"sequence\":2," +
            "\"data\":{" +
            "\"robot_id\":\"forklift_01\"," +
            "\"robot_type\":\"MOBILE_FORKLIFT\"," +
            "\"state\":\"MOVING\"," +
            "\"job_id\":\"JOB-19\"," +
            "\"step_id\":null," +
            "\"delivery_id\":\"DELIVERY-07\"," +
            "\"current_operation\":\"MOVING_TO_DROPOFF\"," +
            "\"lift_state\":\"UP\"," +
            "\"battery\":78.5," +
            "\"error_code\":null" +
            "}" +
            "}";

        HandleJsonMessage(mockJson);
    }

    [ContextMenu("Test/Apply Mock All Robot Statuses")]
    private void ApplyMockAllRobotStatuses()
    {
        ApplyMockSnapshot();

        ApplyMockRobotStatus(
            sequence: 2,
            robotId: "forklift_01",
            robotType: "MOBILE_FORKLIFT",
            state: "MOVING",
            jobId: "JOB-19",
            operation: "MOVING_TO_DROPOFF",
            liftState: "UP",
            battery: 78.5f,
            errorCode: null);

        ApplyMockRobotStatus(
            sequence: 3,
            robotId: "FR5_01",
            robotType: "ROBOT_ARM",
            state: "WORKING",
            jobId: "JOB-19",
            operation: "INSTALL_OUTER_WALL",
            liftState: null,
            battery: 100f,
            errorCode: null);

        ApplyMockRobotStatus(
            sequence: 4,
            robotId: "zkbot2",
            robotType: "ZK_ROBOT",
            state: "WORKING",
            jobId: "JOB-19",
            operation: "PART_PICKING",
            liftState: null,
            battery: 91.2f,
            errorCode: null);

    }

    private void ApplyMockRobotStatus(
    long sequence,
    string robotId,
    string robotType,
    string state,
    string jobId,
    string operation,
    string liftState,
    float battery,
    string errorCode)
    {
        RobotStatusData status = new RobotStatusData
        {
            robot_id = robotId,
            robot_type = robotType,
            state = state,
            job_id = jobId,
            step_id = null,
            delivery_id = null,
            current_operation = operation,
            lift_state = liftState,
            battery = battery,
            error_code = errorCode
        };

        RobotStatusEnvelope envelope =
            new RobotStatusEnvelope
            {
                schema_version = "1.0",
                type = "robot_status",
                timestamp =
                    "2026-08-13T07:30:01.100Z",
                sequence = sequence,
                data = status
            };

        string json = JsonUtility.ToJson(envelope);

        HandleJsonMessage(json);
    }

    [ContextMenu("Test/Apply Mock Production Status")]
    private void ApplyMockProductionStatus()
    {
        ApplyMockSnapshot();

        ProductionStatusEnvelope envelope =
            new ProductionStatusEnvelope
            {
                schema_version = "1.0",
                type = "production_status",
                timestamp = "2026-08-14T01:30:00.125Z",
                sequence = 2,
                data = new ProductionJobData
                {
                    job_id = "JOB-19",
                    product = "HOUSE_A",
                    job_status = "IN_PROGRESS",

                    current_step_id = "STEP-05",
                    current_operation = "INSTALL_OUTER_WALL",
                    current_step_status = "RUNNING",

                    next_step_id = "STEP-06",
                    next_operation = "RETURN_OUTER_PALLET",

                    ready = true,
                    readiness_reason = null,
                    progress = 0.55f,
                    error_code = null
                }
            };

        string json = JsonUtility.ToJson(envelope);
        HandleJsonMessage(json);
    }

    [ContextMenu("Test/Apply Mock Transport Status")]
    private void ApplyMockTransportStatus()
    {
        ApplyMockSnapshot();

        TransportStatusEnvelope envelope =
            new TransportStatusEnvelope
            {
                schema_version = "1.0",
                type = "transport_status",
                timestamp = "2026-08-14T01:31:00.125Z",
                sequence = 2,
                data = new TransportStatusData
                {
                    req_id = "REQ-TRANSPORT-001",
                    job_id = "JOB-19",
                    delivery_id = "DELIVERY-07",
                    robot_id = "forklift_01",

                    task_type = "EXECUTE_TRANSPORT",
                    phase = "LIFTING_UP",
                    progress = 0.42f,

                    // 진행 중에는 result가 null
                    result = null,
                    error_code = null,
                    detail = "Pickup 지점에서 리프트 상승 중"
                }
            };

        string json = JsonUtility.ToJson(envelope);
        HandleJsonMessage(json);
    }

    [ContextMenu("Test/Apply Mock Server Snapshot")]
    private void ApplyMockServerSnapshot()
    {
        LastSequence = 0;
        HasInitialSnapshot = false;
        waitingForInitialSnapshot = true;

        string json =
            "{" +
            "\"schema_version\":\"1.0\"," +
            "\"type\":\"production_snapshot\"," +
            "\"timestamp\":\"2026-08-14T02:59:48.572Z\"," +
            "\"sequence\":1," +
            "\"data\":{" +
            "\"jobs\":[{" +
            "\"job_id\":17," +
            "\"job_code\":\"DEMO_HOUSE_A_RUNNING\"," +
            "\"product_code\":\"HOUSE_A\"," +
            "\"status\":\"RUNNING\"," +
            "\"roof_option_code\":null," +
            "\"requested_at\":\"2026-08-11T09:07:19.250Z\"," +
            "\"started_at\":\"2026-08-11T09:07:19.266Z\"," +
            "\"completed_at\":null" +
            "}]," +
            "\"robots\":[{" +
            "\"robot_id\":\"zkbot2\"," +
            "\"connected\":true," +
            "\"ready\":true," +
            "\"busy\":false" +
            "}]," +
            "\"transports\":[]," +
            "\"active_errors\":[]" +
            "}" +
            "}";

        HandleJsonMessage(json);
    }

    [ContextMenu("Test/Apply Mock Server Robot Status")]
    private void ApplyMockServerRobotStatus()
    {
        // 실제 서버 형식의 Snapshot부터 적용
        ApplyMockServerSnapshot();

        string json =
            "{" +
            "\"schema_version\":\"1.0\"," +
            "\"type\":\"robot_status\"," +
            "\"timestamp\":\"2026-08-14T02:59:49.000Z\"," +
            "\"sequence\":2," +
            "\"data\":{" +
            "\"robot_id\":\"zkbot2\"," +
            "\"connected\":true," +
            "\"ready\":false," +
            "\"busy\":true" +
            "}" +
            "}";

        HandleJsonMessage(json);
    }

    [ContextMenu("Mock/Apply FR5 Joint State")]
    public void ApplyMockFR5JointState()
    {
        string now = DateTime.UtcNow.ToString("o");

        string json = $@"
    {{
        ""schema_version"": ""1.0"",
        ""type"": ""robot_joint_state"",
        ""timestamp"": ""{now}"",
        ""sequence"": 2,
        ""data"": {{
            ""robot_id"": ""fr5"",
            ""joint_names"": [
                ""j1"",
                ""j2"",
                ""j3"",
                ""j4"",
                ""j5"",
                ""j6""
            ],
            ""positions"": [
                0.15,
                -0.25,
                0.35,
                -0.20,
                0.25,
                0.10
            ],
            ""velocities"": [],
            ""efforts"": [],
            ""source_timestamp"": ""{now}""
        }}
    }}";

        HandleJsonMessage(json);
    }

    [ContextMenu("Mock/Apply Forklift Pose")]
    public void ApplyMockForkliftPose2()
    {
        string now = DateTime.UtcNow.ToString("o");

        string json = $@"
    {{
        ""schema_version"": ""1.0"",
        ""type"": ""mobile_robot_pose"",
        ""timestamp"": ""{now}"",
        ""sequence"": 2,
        ""data"": {{
            ""source_timestamp"": ""{now}"",
            ""robot_id"": ""forklift_01"",
            ""frame_id"": ""map"",
            ""position"": {{
                ""x"": 0.5,
                ""y"": 0.5,
                ""z"": 0.0
            }},
            ""orientation"": {{
                ""x"": 0.0,
                ""y"": 0.0,
                ""z"": 0.258819,
                ""w"": 0.965926
            }}
        }}
    }}";

        HandleJsonMessage(json);
    }
}

