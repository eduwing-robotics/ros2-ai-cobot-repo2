using System;
using System.Linq;
using UnityEngine;

[DisallowMultipleComponent]
public class FactoryMaterialFlowController : MonoBehaviour
{
    [Header("Server state")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [SerializeField]
    private string turtleBotRobotId = "forklift_01";

    [Header("TurtleBot")]
    [SerializeField]
    private RobotCargoMount turtleBotCargoMount;

    [Header("Pallet Work Alignment")]
    [SerializeField] private float palletAlignmentMaxDistance = 0.45f;
    private ForkliftPoseApplier palletPoseApplier;
    private CarryableObject aligningPallet;
    private Transform aligningDropPoint;
    private bool aligningPickup;
    private float alignmentDeadline;
    private string alignmentSource;

    [Header("Pallet Docking Fallback (Unity visual only)")]
    [SerializeField] private bool inferPalletFromDocking = true;
    [SerializeField] private float palletDockDwellSeconds = 3f;
    private bool palletIdleTracking;
    private CarryableObject palletContactCandidate;
    [SerializeField] private float palletDockPositionTolerance = 0.04f;
    private Renderer[] palletForkRenderers;
    private Transform forkliftPickupVisual;
    private bool palletPickupNeedsZRearm;
    [SerializeField] private float palletDropArrivalDistance = 0.08f;
    private float nextPalletDockLog;
    private bool pendingLiftPickup;
    private Vector3 palletIdlePosition;
    private Quaternion palletIdleRotation;
    private float palletIdleSince;
    private bool palletVisitLocked;
    private Vector3 palletVisitPosition;
    private CarryableObject observedPalletCargo;
    private readonly System.Collections.Generic.Dictionary<string, string> lastLiftSignals =
        new System.Collections.Generic.Dictionary<string, string>();

    [Header("Assembly Robots")]
    [SerializeField]
    private RobotCargoMount fr5CargoMount;

    [SerializeField]
    private RobotCargoMount zkCargoMount;

    [Header("FR5 Process Correction")]
    [SerializeField]
    private ZKJointStateApplier fr5JointStateApplier;

    [Tooltip("베이스 접촉으로 설치할 때만 적용하는 거리(m). 놓기 신호에는 적용하지 않음")]
    [SerializeField]
    private float fr5PlacementCorrectionDistance = 0.35f;

    [SerializeField]
    private CarryableObject innerPallet;

    [SerializeField]
    private CarryableObject outerPallet;

    [SerializeField]
    private Transform innerSupplyPoint;

    [SerializeField]
    private Transform outerSupplyPoint;

    private string processSceneOperation;
    private int processSceneStep = -1;
    private bool processScenePending;
    private bool waitingForFreshProcess = true;
    private string previousSessionJob;





    private bool IsPreviousSessionState(ProductionJobData job)
    {
        if (!waitingForFreshProcess || job == null) return false;
        // Explicit server PROCESS stages are authoritative, including a mid-job resume.
        if (FactoryOperationCatalog.HasProcessStage(job) &&
            FactoryOperationCatalog.TryGetProcessStep(job, out int serverStep))
        {
            waitingForFreshProcess = false;
            return false;
        }
        string id = job.job_id ?? job.numeric_job_id.ToString();
        if (previousSessionJob == null) previousSessionJob = id;
        if (id != previousSessionJob ||
            (FactoryOperationCatalog.TryGetProcessStep(job, out int step) && step <= 2 && !IsTerminalJob(job)))
        {
            waitingForFreshProcess = false;
            return false;
        }
        return true;
    }

    private void Start()
    {
        ResetMaterialFlow();
        // OnEnable may have received a snapshot before Start resets the scene.
        if (factoryStateManager != null)
        {
            ProductionJobData latest = null;
            foreach (ProductionJobData job in factoryStateManager.ProductionJobs.Values)
                if (job != null && !FactoryOperationCatalog.IsStopped(job) &&
                    (latest == null || job.numeric_job_id > latest.numeric_job_id)) latest = job;
            QueueRoofFromJob(latest);
            QueueProcessScene(latest);
            ApplyFr5OperationSignal(latest?.current_operation, "startup-resume");
        }
    }

    [SerializeField]
    private Transform innerPalletReturnPoint;

    [SerializeField]
    private Transform outerPalletReturnPoint;

    [Header("Pallet Drop Poses (World)")]
    [SerializeField]
    private Vector3 innerPalletDropPosition =
        new Vector3(0.2691f, 0.0389f, -0.4057f);

    [SerializeField]
    private Vector3 innerPalletDropEuler =
        new Vector3(0f, -90f, 0f);

    [SerializeField]
    private Vector3 outerPalletDropPosition =
        new Vector3(0.5791f, 0.0353f, 0.4021f);

    [SerializeField]
    private Vector3 outerPalletDropEuler =
        new Vector3(0f, 90f, 0f);

    [Header("House Assembly")]
    [SerializeField]
    private HouseAssemblyController houseAssembly;

    [Header("Completed House")]
    [SerializeField]
    private RobotCargoMount robotToolMount;

    [SerializeField]
    private CarryableObject forkTool;

    [SerializeField]
    private RobotCargoMount completedHouseMount;

    [SerializeField]
    private Transform completedHouseDropPoint;

    [Header("Roof staging")]
    [SerializeField]
    private RoofVariantSpawner roofVariantSpawner;

    private float nextMissingPalletWarningTime;
    private string pendingRoofVariant;
    private string pendingRoofJobId;
    private string spawnedRoofKey;
    private string activeAssemblyJobId;
    private long preRoofGateJobId;
    private bool preRoofGateReleased;
    private bool forkContactPickupEnabled;
    private bool fr5PickupCorrectionRequested;
    private bool fr5PlacementCorrectionRequested;
    private string activeFr5OperationSignal;
    private CarryableObject aligningWall;
    private Transform wallTargetSocket;
    private Vector3 wallAlignmentPosition;
    private Quaternion wallAlignmentRotation;
    private float wallAlignmentStarted;
    private Vector3 fr5WallPickupPosition;
    private float fr5WallTravel;
    private bool fr5GripperBaselineReceived;
    private bool fr5WaitForOpen;
    private readonly System.Collections.Generic.Dictionary<string, string> fr5OperationBaselines =
        new System.Collections.Generic.Dictionary<string, string>();
    private float nextFr5CorrectionWarningTime;

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            factoryStateManager = FindAnyObjectByType<FactoryStateManager>();
        }

        EnsureRuntimeSceneReferences();
        BindForkliftVisualMount();
        ConfigureAssemblyRobotCargoRoles();
        EnsurePalletDropPoints();
        BindFr5GripperSignals();

        // Lift state drives pallet attachment, with a bounded stationary docking fallback.
        // These changes affect only the Unity visual, not physical robot control.
        turtleBotCargoMount?.SetAutoPickupOnContact(false);
        robotToolMount?.SetAutoPickupOnContact(false);

        if (factoryStateManager != null)
        {
            factoryStateManager.RobotStatusUpdated += HandleRobotStatusUpdated;
            factoryStateManager.TransportStatusUpdated += HandleTransportStatusUpdated;
            factoryStateManager.ProductionStatusUpdated +=
                HandleProductionStatusUpdated;
            factoryStateManager.ProductionInspectionStatusUpdated +=
                HandleProductionInspectionStatusUpdated;
            factoryStateManager.SnapshotApplied += HandleSnapshotApplied;

            if (factoryStateManager.TryGetActiveProductionJob(
                    out ProductionJobData activeJob))
            {
                QueueRoofFromJob(activeJob);
                QueueProcessScene(activeJob);
            }

            foreach (ProductionInspectionStatusData inspection in
                     factoryStateManager.ProductionInspectionStatuses.Values)
            {
                HandleProductionInspectionStatusUpdated(inspection);
            }
        }
    }

    private void Update()
    {
        BindForkliftVisualMount();
        TrySpawnPendingRoof();
        UpdateForkContactPickupGate();
        UpdateWallSocketAlignment();
        UpdateFr5ProcessCorrection();
        UpdatePalletAlignment();
        UpdatePalletDropArrival();
        UpdatePalletIdleFallback();
    }

    private void OnDisable()
    {
        ResetPalletIdleFallback();
        lastLiftSignals.Clear();
        CancelPalletAlignment();
        if (factoryStateManager != null)
        {
            factoryStateManager.RobotStatusUpdated -= HandleRobotStatusUpdated;
            factoryStateManager.TransportStatusUpdated -= HandleTransportStatusUpdated;
            factoryStateManager.ProductionStatusUpdated -=
                HandleProductionStatusUpdated;
            factoryStateManager.ProductionInspectionStatusUpdated -=
                HandleProductionInspectionStatusUpdated;
            factoryStateManager.SnapshotApplied -= HandleSnapshotApplied;
        }

        if (fr5JointStateApplier != null)
        {
            fr5JointStateApplier.GripperClosedChanged -=
                HandleFr5GripperClosedChanged;
        }
    }

    public bool PickupInnerPallet()
    {
        return turtleBotCargoMount != null &&
               turtleBotCargoMount.TryPickup(innerPallet);
    }

    public bool DeliverInnerPallet()
    {
        return ReleasePallet(
            turtleBotCargoMount,
            innerSupplyPoint);
    }

    public bool ReturnInnerPallet()
    {
        return turtleBotCargoMount != null &&
               turtleBotCargoMount.TryPickup(innerPallet);
    }

    public bool CompleteInnerPalletReturn()
    {
        bool released = ReturnPalletHome(
            turtleBotCargoMount,
            innerPallet,
            innerPalletReturnPoint);
        return released;
    }

    public bool PickupOuterPallet()
    {
        return turtleBotCargoMount != null &&
               turtleBotCargoMount.TryPickup(outerPallet);
    }

    public bool DeliverOuterPallet()
    {
        bool released = ReleasePallet(
            turtleBotCargoMount,
            outerSupplyPoint);
        return released;
    }

    public bool ReturnOuterPallet()
    {
        return turtleBotCargoMount != null &&
               turtleBotCargoMount.TryPickup(outerPallet);
    }

    public bool CompleteOuterPalletReturn()
    {
        bool released = ReturnPalletHome(
            turtleBotCargoMount,
            outerPallet,
            outerPalletReturnPoint);
        return released;
    }

    public bool InstallPart(string operationCode)
    {
        return houseAssembly != null &&
               houseAssembly.InstallByOperation(operationCode);
    }

    public bool ReleaseFr5PartToBase()
    {
        return ReleaseRobotCargoForAssembly(fr5CargoMount);
    }

    public bool ReleaseZkPartToBase()
    {
        return ReleaseRobotCargoForAssembly(zkCargoMount);
    }

    public CarryableObject PrepareRoof(string roofVariant)
    {
        return roofVariantSpawner != null
            ? roofVariantSpawner.SpawnRoofVariant(roofVariant)
            : null;
    }

    public void Configure(
        RobotCargoMount palletCarrier,
        RobotCargoMount fr5Carrier,
        RobotCargoMount zkCarrier,
        CarryableObject incomingPallet,
        CarryableObject outgoingPallet,
        CarryableObject fork,
        RobotCargoMount forkCarrier,
        RobotCargoMount houseCarrier,
        HouseAssemblyController assembly,
        RoofVariantSpawner roofSpawner,
        Transform finishedHouseDropPoint)
    {
        turtleBotCargoMount = palletCarrier;
        fr5CargoMount = fr5Carrier;
        zkCargoMount = zkCarrier;
        innerPallet = incomingPallet;
        outerPallet = outgoingPallet;
        forkTool = fork;
        robotToolMount = forkCarrier;
        completedHouseMount = houseCarrier;
        houseAssembly = assembly;
        roofVariantSpawner = roofSpawner;
        completedHouseDropPoint = finishedHouseDropPoint;

        turtleBotCargoMount?.SetAutoPickupOnContact(false);
        robotToolMount?.SetAutoPickupOnContact(false);
        forkContactPickupEnabled = false;
        roofVariantSpawner?.SetStagingBase(houseAssembly?.AssemblyRoot);
    }

    [ContextMenu("Test/Spawn roof")]
    private void TestSpawnRoof()
    {
        PrepareRoof("roof");
    }

    [ContextMenu("Test/Spawn roof2")]
    private void TestSpawnRoof2()
    {
        PrepareRoof("roof2");
    }

    [ContextMenu("Test/Release forklift cargo here")]
    private void TestReleaseForkliftCargo()
    {
        turtleBotCargoMount?.ReleaseAtCurrentPose();
    }

    [ContextMenu("Test/Release FR5 part to base")]
    private void TestReleaseFr5Part()
    {
        ReleaseFr5PartToBase();
    }

    [ContextMenu("Test/Release ZK part to base")]
    private void TestReleaseZkPart()
    {
        ReleaseZkPartToBase();
    }

    public bool PickupFork()
    {
        if (robotToolMount == null || forkTool == null)
        {
            return false;
        }

        if (houseAssembly == null || !houseAssembly.IsAssemblyComplete())
        {
            Debug.LogWarning(
                "[MaterialFlow] 지붕까지 조립이 끝나기 전에는 " +
                "FR5가 완성 주택 운반용 fork를 집지 않습니다.",
                this);
            return false;
        }

        if (fr5CargoMount != null && fr5CargoMount.HasCargo)
        {
            Debug.LogWarning(
                "[MaterialFlow] FR5가 자재를 들고 있어 fork 결합을 보류합니다.",
                this);
            return false;
        }

        return robotToolMount.TryPickup(forkTool);
    }

    public bool PickupCompletedHouse()
    {
        return completedHouseMount != null &&
               houseAssembly != null &&
               completedHouseMount.TryPickup(
                   houseAssembly.AssemblyRoot);
    }

    public bool DropCompletedHouse()
    {
        bool released = ReleasePallet(completedHouseMount, completedHouseDropPoint);
        if (released) PlaceForkUnderCompletedHouse();
        return released;
    }

    private void PlaceForkUnderCompletedHouse()
    {
        CarryableObject house = houseAssembly?.AssemblyRoot;
        if (forkTool == null || house == null) return;
        robotToolMount?.ClearCargoReference();
        forkTool.ResetToInitialState();
        forkTool.transform.rotation = house.transform.rotation *
            Quaternion.Inverse(house.InitialWorldRotation) * forkTool.InitialWorldRotation;
        Renderer[] houseRenderers = house.GetComponentsInChildren<Renderer>();
        Renderer[] forkRenderers = forkTool.GetComponentsInChildren<Renderer>();
        if (houseRenderers.Length == 0 || forkRenderers.Length == 0) return;
        Bounds hb = houseRenderers[0].bounds, fb = forkRenderers[0].bounds;
        foreach (Renderer r in houseRenderers) hb.Encapsulate(r.bounds);
        foreach (Renderer r in forkRenderers) fb.Encapsulate(r.bounds);
        Vector3 towardRobot = palletPoseApplier != null
            ? palletPoseApplier.SceneStartPosition - hb.center : -house.transform.forward;
        towardRobot.y = 0f;
        if (towardRobot.sqrMagnitude > 0.000001f) towardRobot.Normalize();
        Vector3 exposedCenter = hb.center + towardRobot * 0.06f;
        // The tall handle is not the supporting surface. Keep the fork on the
        // table plane and expose its handle instead of burying the entire tool.
        forkTool.transform.position += new Vector3(exposedCenter.x - fb.center.x,
            hb.min.y - fb.min.y, exposedCenter.z - fb.center.z);
        forkTool.InstallAtCurrentPose(house.transform);
    }

    [ContextMenu("Reset Material Flow")]
    public void ResetMaterialFlow()
    {
        processScenePending = false;
        processSceneStep = -1;
        processSceneOperation = null;
        aligningWall = null;
        wallTargetSocket = null;
        fr5WallTravel = 0f;
        CancelPalletAlignment();
        ResetPalletIdleFallback();
        lastLiftSignals.Clear();
        palletPickupNeedsZRearm = false;
        turtleBotCargoMount?.ClearCargoReference();
        fr5CargoMount?.ClearCargoReference();
        zkCargoMount?.ClearCargoReference();
        robotToolMount?.ClearCargoReference();
        completedHouseMount?.ClearCargoReference();

        innerPallet?.ResetToInitialState();
        outerPallet?.ResetToInitialState();
        forkTool?.ResetToInitialState();
        houseAssembly?.ResetAssembly();
        roofVariantSpawner?.ResetRoof();
        pendingRoofVariant = null;
        pendingRoofJobId = null;
        spawnedRoofKey = null;
        activeAssemblyJobId = null;
        preRoofGateJobId = 0;
        preRoofGateReleased = false;
        forkContactPickupEnabled = false;
        fr5PickupCorrectionRequested = false;
        fr5PlacementCorrectionRequested = false;
        activeFr5OperationSignal = null;
        fr5GripperBaselineReceived = false;
        fr5WaitForOpen = false;
        fr5OperationBaselines.Clear();
        robotToolMount?.SetAutoPickupOnContact(false);
    }

    private void UpdateForkContactPickupGate()
    {
        bool shouldEnable =
            robotToolMount != null &&
            !robotToolMount.HasCargo &&
            (fr5CargoMount == null || !fr5CargoMount.HasCargo) &&
            houseAssembly != null &&
            houseAssembly.IsAssemblyComplete();

        if (shouldEnable == forkContactPickupEnabled)
        {
            return;
        }

        forkContactPickupEnabled = shouldEnable;
        robotToolMount?.SetAutoPickupOnContact(shouldEnable);

        if (shouldEnable)
        {
            Debug.Log(
                "[MaterialFlow] 조립 완료: FR5의 fork 접촉 결합을 활성화합니다.",
                this);
        }
    }

    [ContextMenu("Test/Run Full Material Flow")]
    private void RunFullMaterialFlowFromInspector()
    {
        bool passed = RunMaterialFlowSelfTest(out string report);

        if (passed)
        {
            Debug.Log("[MATERIAL FLOW SELF TEST PASS] " + report, this);
        }
        else
        {
            Debug.LogError("[MATERIAL FLOW SELF TEST FAIL] " + report, this);
        }
    }

    public bool RunMaterialFlowSelfTest(out string report)
    {
        if (houseAssembly == null)
        {
            report = "HouseAssemblyController 없음";
            return false;
        }

        string originalProduct = houseAssembly.ActiveProductCode;
        string[] products = { "HOUSE_A", "HOUSE_B" };
        string combinedReport = string.Empty;
        bool allPassed = true;

        foreach (string product in products)
        {
            if (!houseAssembly.HasProductLayout(product))
            {
                allPassed = false;
                combinedReport += $"{product}=기준없음; ";
                continue;
            }

            houseAssembly.SelectProduct(product, false);
            roofVariantSpawner?.SetStagingBase(houseAssembly.AssemblyRoot);

            bool passed = RunActiveMaterialFlowSelfTest(
                out string productReport);
            allPassed &= passed;
            combinedReport += $"{product}[{productReport}]; ";
        }

        if (houseAssembly.HasProductLayout(originalProduct))
        {
            houseAssembly.SelectProduct(originalProduct, false);
            roofVariantSpawner?.SetStagingBase(houseAssembly.AssemblyRoot);
        }

        report = combinedReport.Trim();
        return allPassed;
    }

    private bool RunActiveMaterialFlowSelfTest(out string report)
    {
        bool outerPalletPassed = false;
        bool innerPalletPassed = false;
        bool basePassed = false;
        int installedWalls = 0;
        bool roofPassed = false;
        bool completedPassed = false;

        try
        {
            ResetMaterialFlow();

            // 실제 생산 순서와 동일하게 외벽 팔레트를 먼저 운반합니다.
            outerPalletPassed = PickupOuterPallet() &&
                turtleBotCargoMount.ReleaseAtCurrentPose(
                    outerPallet.InitialParent) != null;

            innerPalletPassed = PickupInnerPallet() &&
                turtleBotCargoMount.ReleaseAtCurrentPose(
                    innerPallet.InitialParent) != null;

            basePassed = houseAssembly != null &&
                (houseAssembly.StartWithBasePlaced
                    ? houseAssembly.PrepareBaseAtDestination()
                    : zkCargoMount != null &&
                      houseAssembly.AssemblyRoot != null &&
                      zkCargoMount.TryPickup(houseAssembly.AssemblyRoot) &&
                      houseAssembly.PlaceBaseFrom(zkCargoMount));

            CarryableObject[] carryables =
                FindObjectsByType<CarryableObject>(
                    FindObjectsInactive.Include);
            string[] requiredWalls =
                houseAssembly.ActiveWallPayloadIds;

            for (int i = 0; i < requiredWalls.Length; i++)
            {
                CarryableObject wall = FindPayload(
                    carryables,
                    requiredWalls[i]);
                RobotCargoMount mount = fr5CargoMount;

                if (wall != null && mount != null &&
                    mount.TryPickup(wall) &&
                    houseAssembly.TryInstallFromContact(wall))
                {
                    installedWalls++;
                }
            }

            if (string.IsNullOrWhiteSpace(pendingRoofVariant))
            {
                pendingRoofVariant = houseAssembly.ActiveRoofPayloadId;
                pendingRoofJobId =
                    "LOCAL-MATERIAL-TEST-" +
                    houseAssembly.ActiveProductCode;
            }

            TrySpawnPendingRoof();
            CarryableObject roof = roofVariantSpawner?.ActiveRoof;

            bool roofPickedUp = roof != null &&
                zkCargoMount != null &&
                zkCargoMount.TryPickup(roof);
            bool gateBlockedInstallation =
                roofPickedUp && !ReleaseZkPartToBase();
            preRoofGateReleased = true;
            roofPassed = gateBlockedInstallation &&
                ReleaseZkPartToBase() &&
                houseAssembly.IsAssemblyComplete();

            completedPassed = PickupFork() &&
                PickupCompletedHouse() &&
                DropCompletedHouse();

            bool passed = outerPalletPassed && innerPalletPassed &&
                basePassed &&
                installedWalls == requiredWalls.Length &&
                roofPassed && completedPassed;

            report =
                $"제품={houseAssembly.ActiveProductCode}, " +
                $"외벽팔레트={outerPalletPassed}, " +
                $"내벽팔레트={innerPalletPassed}, " +
                $"베이스={basePassed}, " +
                $"벽={installedWalls}/{requiredWalls.Length}, " +
                $"지붕={roofPassed}, Finish이동={completedPassed}";
            return passed;
        }
        finally
        {
            ResetMaterialFlow();
        }
    }

    private static CarryableObject FindPayload(
        CarryableObject[] carryables,
        string payloadId)
    {
        if (carryables == null)
        {
            return null;
        }

        foreach (CarryableObject carryable in carryables)
        {
            if (carryable != null &&
                carryable.gameObject.activeInHierarchy &&
                string.Equals(
                    carryable.PayloadId,
                    payloadId,
                    StringComparison.OrdinalIgnoreCase))
            {
                return carryable;
            }
        }

        return null;
    }

    private static bool ReleasePallet(
        RobotCargoMount mount,
        Transform destination)
    {
        return mount != null &&
               destination != null &&
               mount.ReleaseAt(destination) != null;
    }

    private static bool ReturnPalletHome(
        RobotCargoMount mount,
        CarryableObject pallet,
        Transform configuredReturnPoint)
    {
        if (mount == null || pallet == null || mount.CurrentCargo != pallet)
        {
            return false;
        }

        if (configuredReturnPoint != null)
        {
            return mount.ReleaseAt(configuredReturnPoint) != null;
        }

        CarryableObject released = mount.ReleaseAtCurrentPose(
            pallet.InitialParent);

        if (released == null)
        {
            return false;
        }

        released.ResetToInitialState();
        return true;
    }

    private bool ReleaseRobotCargoForAssembly(RobotCargoMount mount)
    {
        if (mount == null ||
            houseAssembly == null ||
            houseAssembly.AssemblyRoot == null)
        {
            return false;
        }

        CarryableObject cargo = mount.CurrentCargo;

        if (cargo == null)
        {
            return false;
        }

        if (cargo.PayloadType == CarryableType.Tool ||
            cargo.PayloadType == CarryableType.CompletedHouse)
        {
            Debug.LogWarning(
                $"[MaterialFlow] 조립 자재 슬롯에서 {cargo.PayloadType}을 " +
                "내려놓으려는 요청을 무시합니다.",
                this);
            return false;
        }

        if (cargo.PayloadType == CarryableType.Roof &&
            !preRoofGateReleased)
        {
            Debug.LogWarning(
                "[MaterialFlow] PRE_ROOF Gate가 RELEASED가 아니므로 " +
                "지붕 설치를 보류합니다.",
                this);
            return false;
        }

        if (cargo == houseAssembly.AssemblyRoot &&
            cargo.PayloadType == CarryableType.Base)
        {
            return houseAssembly.PlaceBaseFrom(mount);
        }

        if (cargo.PayloadType == CarryableType.Wall)
        {
            fr5PlacementCorrectionRequested = true;
            return TryPlaceExpectedFr5Wall(true);
        }
        return houseAssembly.TryInstallFromContact(cargo);
    }

    private void HandleRobotStatusUpdated(RobotStatusData status)
    {
        if (status == null)
        {
            return;
        }

        if (IsFr5(status.robot_id))
        {
            ApplyFr5OperationSignal(status.current_operation, "robot_status");
        }

        if (IsTurtleBot(status.robot_id))
        {
            ApplyTurtleBotLiftSignal(status.lift_state, "robot_status");
        }
    }

    private void HandleProductionStatusUpdated(ProductionJobData job)
    {
        QueueRoofFromJob(job);
        QueueProcessScene(job);
        ApplyFr5OperationSignal(job?.current_operation, "production");
    }

    private void HandleProductionInspectionStatusUpdated(
        ProductionInspectionStatusData status)
    {
        if (status?.inspection == null ||
            NormalizeSignal(status.inspection_type) != "PRE_ROOF" ||
            status.job_id < preRoofGateJobId)
        {
            return;
        }

        preRoofGateJobId = status.job_id;
        preRoofGateReleased =
            NormalizeSignal(status.inspection.gate_state) == "RELEASED";
    }

    private void HandleSnapshotApplied(ProductionSnapshotData snapshot)
    {
        ProductionJobData newest = null;

        if (snapshot?.jobs == null)
        {
            return;
        }

        foreach (ProductionJobData job in snapshot.jobs)
        {
            if (job == null ||
                (IsTerminalJob(job) && !IsServerCompletedJob(job)))
            {
                continue;
            }

            if (newest == null || job.numeric_job_id > newest.numeric_job_id)
            {
                newest = job;
            }
        }

        QueueRoofFromJob(newest);
        QueueProcessScene(newest);
        ApplyFr5OperationSignal(newest?.current_operation, "snapshot");
    }

    private void QueueRoofFromJob(ProductionJobData job)
    {
        if (IsPreviousSessionState(job)) return;
        if (job == null)
        {
            return;
        }

        string jobId = !string.IsNullOrWhiteSpace(job.job_id)
            ? job.job_id
            : job.numeric_job_id.ToString();

        SelectAssemblyForJob(job, jobId);

        if (IsTerminalJob(job))
        {
            if (IsServerCompletedJob(job))
            {
                pendingRoofVariant = ResolveRoofVariant(job);
                pendingRoofJobId = jobId;
                TrySpawnPendingRoof();
                return;
            }
            if (pendingRoofJobId == jobId)
            {
                pendingRoofVariant = null;
                pendingRoofJobId = null;
            }

            return;
        }

        // 최종 생산 규칙은 제품 종류가 지붕 선택의 기준입니다.
        // HOUSE_A = roof(지붕1), HOUSE_B = roof2(지붕2).
        // roof_option_code는 예전 서버/로컬 데이터 호환용으로만 사용합니다.
        string variant = ResolveRoofVariant(job);

        if (variant == null)
        {
            return;
        }

        pendingRoofVariant = variant;
        pendingRoofJobId = jobId;
        TrySpawnPendingRoof();
    }

    private void SelectAssemblyForJob(
        ProductionJobData job,
        string jobId)
    {
        if (houseAssembly == null || job == null)
        {
            return;
        }

        string productCode = ResolveProductCode(job);

        if (productCode == null ||
            !houseAssembly.HasProductLayout(productCode))
        {
            return;
        }

        if (!string.Equals(
                activeAssemblyJobId,
                jobId,
                StringComparison.OrdinalIgnoreCase))
        {
            ResetMaterialFlow();
            activeAssemblyJobId = jobId;
        }

        houseAssembly.SelectProduct(productCode, false);
        roofVariantSpawner?.SetStagingBase(houseAssembly.AssemblyRoot);
    }

    private static string ResolveProductCode(ProductionJobData job)
    {
        string product = NormalizeSignal(
            !string.IsNullOrWhiteSpace(job?.product_code)
                ? job.product_code
                : job?.product ?? string.Empty);

        switch (product)
        {
            case "HOUSE_A":
            case "HOUSEA":
            case "PRODUCT_A":
            case "PRODUCTA":
                return "HOUSE_A";
            case "HOUSE_B":
            case "HOUSEB":
            case "PRODUCT_B":
            case "PRODUCTB":
                return "HOUSE_B";
            default:
                return null;
        }
    }

    private static string ResolveRoofVariant(ProductionJobData job)
    {
        if (job == null)
        {
            return null;
        }

        string product = NormalizeSignal(
            !string.IsNullOrWhiteSpace(job.product_code)
                ? job.product_code
                : job.product ?? string.Empty);

        switch (product)
        {
            case "HOUSE_A":
            case "HOUSEA":
            case "PRODUCT_A":
            case "PRODUCTA":
                return "roof";

            case "HOUSE_B":
            case "HOUSEB":
            case "PRODUCT_B":
            case "PRODUCTB":
                return "roof2";

            default:
                return NormalizeRoofVariant(job.roof_option_code);
        }
    }

    private static bool IsServerCompletedJob(ProductionJobData job)
    {
        string state = NormalizeSignal(!string.IsNullOrWhiteSpace(job?.job_status)
            ? job.job_status : job?.status ?? string.Empty);
        return state == "COMPLETED" || state == "COMPLETE";
    }

    private static bool IsTerminalJob(ProductionJobData job)
    {
        string state = NormalizeSignal(
            !string.IsNullOrWhiteSpace(job?.job_status)
                ? job.job_status
                : job?.status ?? string.Empty);

        return state == "COMPLETED" ||
               state == "COMPLETE" ||
               state == "FAILED" ||
               state == "CANCELLED" ||
               state == "CANCELED" ||
               state == "REJECTED";
    }

    private void TrySpawnPendingRoof()
    {
        if (string.IsNullOrWhiteSpace(pendingRoofVariant) ||
            houseAssembly == null ||
            !houseAssembly.BasePlaced ||
            roofVariantSpawner == null)
        {
            return;
        }

        string roofKey = pendingRoofJobId + ":" + pendingRoofVariant;

        if (spawnedRoofKey == roofKey)
        {
            return;
        }

        CarryableObject roof = PrepareRoof(pendingRoofVariant);

        if (roof == null)
        {
            return;
        }

        spawnedRoofKey = roofKey;
        Debug.Log(
            $"[MaterialFlow] 서버 지붕 선택 자동 준비: " +
            $"job={pendingRoofJobId}, option={pendingRoofVariant}",
            this);
    }

    private static string NormalizeRoofVariant(string value)
    {
        string normalized = (value ?? string.Empty)
            .Trim()
            .Replace("-", string.Empty)
            .Replace("_", string.Empty)
            .ToUpperInvariant();

        switch (normalized)
        {
            case "ROOF":
            case "ROOF1":
            case "ROOF01":
                return "roof";
            case "ROOF2":
            case "ROOF02":
                return "roof2";
            default:
                return null;
        }
    }

    private void HandleTransportStatusUpdated(TransportStatusData status)
    {
        if (status == null || !IsTurtleBot(status.robot_id))
        {
            return;
        }

        ApplyTurtleBotLiftSignal(status.phase, "transport_status");
    }

    private void ApplyTurtleBotLiftSignal(string value, string source)
    {
        if (turtleBotCargoMount == null || string.IsNullOrWhiteSpace(value))
        {
            return;
        }

        string signal = NormalizeSignal(value);
        // Repeated status snapshots must not undo an inferred lift operation.
        string liftDirection = IsLiftUpSignal(signal) ? "UP" : IsLiftDownSignal(signal) ? "DOWN" : null;
        if (liftDirection != null)
        {
            bool repeated = lastLiftSignals.TryGetValue(source, out string previous) && previous == liftDirection;
            lastLiftSignals[source] = liftDirection;
            if (repeated && (palletVisitLocked || alignmentSource == "inferred_dock_stop" && aligningPallet != null)) return;
        }
        if (aligningPallet != null)
        {
            if ((aligningPickup && IsLiftDownSignal(signal)) ||
                (!aligningPickup && IsLiftUpSignal(signal)))
                CancelPalletAlignment();
            else if (IsLiftUpSignal(signal) || IsLiftDownSignal(signal))
                return;
        }

        if (IsLiftUpSignal(signal))
        {
            // Even an early server UP must wait for contact followed by reverse motion.
            pendingLiftPickup = !turtleBotCargoMount.HasCargo;
            return;
        }
        if (IsLiftDownSignal(signal)) pendingLiftPickup = false;

        if (!IsLiftDownSignal(signal) || !turtleBotCargoMount.HasCargo)
        {
            return;
        }

        if (TryStartPalletAlignment(false, source + ":" + value)) return;
        CarryableObject cargo = turtleBotCargoMount.CurrentCargo;
        if (cargo == innerPallet || cargo == outerPallet)
        {
            if (Time.unscaledTime >= nextMissingPalletWarningTime)
            {
                nextMissingPalletWarningTime = Time.unscaledTime + 2f;
                Debug.LogWarning("[PalletAlignment] No nearby drop station; keeping pallet attached.", this);
            }
            return;
        }
        turtleBotCargoMount.ReleaseAtCurrentPose(cargo.InitialParent);

        Debug.Log(
            $"[MaterialFlow] {source} {value}: 팔레트 분리",
            this);
    }

    private static float PalletPlanarDistance(Vector3 a, Vector3 b)
    {
        a.y = b.y = 0f;
        return Vector3.Distance(a, b);
    }

    private void CancelPalletAlignment()
    {
        palletPoseApplier?.StopWorkAlignment();
        aligningPallet = null;
        aligningDropPoint = null;
    }

    private bool TryStartPalletAlignment(bool pickup, string source)
    {
        if (palletPoseApplier == null)
            palletPoseApplier = turtleBotCargoMount.GetComponentInParent<ForkliftPoseApplier>();
        if (palletPoseApplier == null) return false;

        CarryableObject cargo = turtleBotCargoMount.CurrentCargo;
        Transform destination = null;
        Vector3 reference = turtleBotCargoMount.AttachmentPosition;
        if (pickup)
        {
            cargo = null;
            float best = palletAlignmentMaxDistance;
            float second = float.PositiveInfinity;
            foreach (CarryableObject item in new[] { innerPallet, outerPallet })
            {
                if (item == null || item.State == CarryableState.AttachedToRobot) continue;
                float distance = PalletPlanarDistance(reference, item.transform.position);
                if (distance < best) { second = best; best = distance; cargo = item; }
                else second = Mathf.Min(second, distance);
            }
            if (cargo == null || (second < palletAlignmentMaxDistance && second - best < 0.05f)) return false;
        }
        else
        {
            if (cargo == null || (cargo != innerPallet && cargo != outerPallet)) return false;
            reference = cargo.transform.position;
            Transform supply = cargo == innerPallet ? innerSupplyPoint : outerSupplyPoint;
            Transform home = cargo == innerPallet ? innerPalletReturnPoint : outerPalletReturnPoint;
            foreach (Transform point in new[] { supply, home })
            {
                if (point != null && (destination == null ||
                    PalletPlanarDistance(reference, point.position) < PalletPlanarDistance(reference, destination.position)))
                    destination = point;
            }
            if (destination == null) return false;
        }

        Vector3 target = pickup ? cargo.transform.position : destination.position;
        float error = PalletPlanarDistance(reference, target);
        if (error > palletAlignmentMaxDistance) return false;
        aligningPallet = cargo;
        aligningPickup = pickup;
        aligningDropPoint = destination;
        alignmentSource = source;
        alignmentDeadline = Time.unscaledTime + 4f;
        palletPoseApplier.AddWorkAlignment(target - reference);
        Debug.Log($"[PalletAlignment] start source={source} pickup={pickup} cargo={cargo.PayloadId} error={error:F3}m", this);
        return true;
    }

    private void UpdatePalletAlignment()
    {
        if (aligningPallet == null) return;
        if (alignmentSource == "inferred_dock_stop" &&
            (palletPoseApplier == null || !palletPoseApplier.HasRecentPalletPose ||
             PalletPlanarDistance(palletPoseApplier.PalletMotionPosition, palletIdlePosition) > 0.03f ||
             Quaternion.Angle(palletPoseApplier.PalletMotionRotation, palletIdleRotation) > 5f))
        {
            CancelPalletAlignment();
            palletVisitLocked = false;
            palletIdleTracking = false;
            return;
        }
        if (turtleBotCargoMount == null ||
            (!aligningPickup && (aligningDropPoint == null || turtleBotCargoMount.CurrentCargo != aligningPallet)) ||
            (aligningPickup && turtleBotCargoMount.HasCargo))
        {
            CancelPalletAlignment();
            return;
        }
        Vector3 current = aligningPickup ? turtleBotCargoMount.AttachmentPosition : aligningPallet.transform.position;
        Vector3 target = aligningPickup ? aligningPallet.transform.position : aligningDropPoint.position;
        if (PalletPlanarDistance(current, target) <= 0.015f)
        {
            bool success = aligningPickup
                ? turtleBotCargoMount.TryPickup(aligningPallet)
                : turtleBotCargoMount.ReleaseAt(aligningDropPoint, aligningPallet.InitialParent) != null;
            if (success)
            {
                Debug.Log($"[PalletAlignment] completed source={alignmentSource} pickup={aligningPickup}", this);
                LockPalletVisit();
                CancelPalletAlignment();
                return;
            }
        }
        if (Time.unscaledTime >= alignmentDeadline)
        {
            Debug.LogWarning("[PalletAlignment] alignment timed out; attachment/drop was not forced.", this);
            CancelPalletAlignment();
        }
    }

    private void ResetPalletIdleFallback()
    {
        palletIdleTracking = false;
        palletVisitLocked = false;
        observedPalletCargo = null;
        palletContactCandidate = null;
        reverseContactPallet = null;
        pendingLiftPickup = false;
    }

    private void LockPalletVisit()
    {
        if (turtleBotCargoMount == null) return;
        reverseContactPallet = null;
        palletVisitLocked = true;
        palletDropArrivalArmed = false;
        palletVisitPosition = turtleBotCargoMount.AttachmentPosition;
        observedPalletCargo = turtleBotCargoMount.CurrentCargo;
        palletIdleTracking = false;
    }

    private bool IsPalletAtForkDockPosition(CarryableObject pallet)
    {
        // Use the visible fork mesh, independently of the broad contact trigger.
        if (pallet == null || !pallet.gameObject.activeInHierarchy ||
            pallet.State == CarryableState.AttachedToRobot || pallet.State == CarryableState.Installed) return false;
        // Compare the actual fork geometry and pallet geometry in world X/Z.
        if (palletForkRenderers == null)
            palletForkRenderers = palletPoseApplier.GetComponentsInChildren<Renderer>(true)
                .Where(r => r.name.IndexOf("fork_lift", StringComparison.OrdinalIgnoreCase) >= 0)
                .ToArray();
        bool haveFork = false;
        Bounds forkBounds = default;
        foreach (Renderer r in palletForkRenderers)
        {
            if (r == null || !r.enabled || !r.gameObject.activeInHierarchy) continue;
            if (!haveFork) { forkBounds = r.bounds; haveFork = true; }
            else forkBounds.Encapsulate(r.bounds);
        }
        bool havePallet = false;
        Bounds palletBounds = default;
        foreach (Renderer r in pallet.GetComponentsInChildren<Renderer>())
        {
            // Exclude walls or other cargo nested under the pallet.
            if (r.GetComponentInParent<CarryableObject>() != pallet) continue;
            if (!havePallet) { palletBounds = r.bounds; havePallet = true; }
            else palletBounds.Encapsulate(r.bounds);
        }
        float error = haveFork && havePallet
            ? PalletPlanarDistance(forkBounds.center, palletBounds.center)
            : float.PositiveInfinity;
        if (Time.unscaledTime >= nextPalletDockLog)
        {
            nextPalletDockLog = Time.unscaledTime + 1f;
            Debug.Log($"[PalletDock] robot={palletPoseApplier.DisplayedPosition:F4} raw={palletPoseApplier.PalletMotionPosition:F4} fork={forkBounds.center:F4} pallet={palletBounds.center:F4} error={error:F4} allowed={palletDockPositionTolerance:F3}", this);
        }
        float verticalGap = haveFork && havePallet
            ? Mathf.Max(0f, Mathf.Max(palletBounds.min.y - forkBounds.max.y,
                forkBounds.min.y - palletBounds.max.y))
            : float.PositiveInfinity;
        return error <= palletDockPositionTolerance && verticalGap <= 0.06f;
    }

    private bool palletDropArrivalArmed;

    private void UpdatePalletDropArrival()
    {
        if (turtleBotCargoMount == null || !turtleBotCargoMount.HasCargo) return;
        CarryableObject cargo = turtleBotCargoMount.CurrentCargo;
        Transform drop = cargo == outerPallet ? outerSupplyPoint :
            cargo == innerPallet ? innerSupplyPoint : null;
        if (processSceneOperation == "RETURN_OUTER_PALLET" && cargo == outerPallet)
            drop = outerPalletReturnPoint;
        if (processSceneOperation == "RETURN_INNER_PALLET" && cargo == innerPallet)
            drop = innerPalletReturnPoint;
        if (drop == null) return;
        if (PalletPlanarDistance(cargo.transform.position, drop.position) > palletDropArrivalDistance)
        {
            palletDropArrivalArmed = true;
            return;
        }
        // A pallet picked up at DROP must leave it before arrival can release it again.
        if (!palletDropArrivalArmed) return;
        if (turtleBotCargoMount.ReleaseAt(drop, cargo.InitialParent) == null) return;
        CancelPalletAlignment();
        pendingLiftPickup = false;
        palletPickupNeedsZRearm = true;
        LockPalletVisit();
        Debug.Log($"[PalletDrop] arrived at {drop.name}: released {cargo.PayloadId}", this);
    }

    private Renderer FindForkBRenderer()
    {
        if (forkliftPickupVisual == null) return null;
        foreach (Renderer r in forkliftPickupVisual.GetComponentsInChildren<Renderer>())
        {
            for (Transform t = r.transform; t != null && t != forkliftPickupVisual; t = t.parent)
                if (t.name.IndexOf("forklift_b", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    t.name.IndexOf("fork_lift (2)", StringComparison.OrdinalIgnoreCase) >= 0) return r;
        }
        return null;
    }

    private static void ProjectMeshBounds(Renderer r, Vector3 forward, ref Bounds projected, ref bool found)
    {
        Bounds local = r.localBounds;
        Vector3 side = Vector3.Cross(Vector3.up, forward);
        for (int i = 0; i < 8; i++)
        {
            Vector3 corner = local.center + Vector3.Scale(local.extents,
                new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
            Vector3 world = r.transform.TransformPoint(corner);
            Vector3 value = new Vector3(Vector3.Dot(world, side), world.y, Vector3.Dot(world, forward));
            if (!found) { projected = new Bounds(value, Vector3.zero); found = true; }
            else projected.Encapsulate(value);
        }
    }

    private CarryableObject reverseContactPallet;
    private Vector3 reverseContactPosition;
    private Vector3 reverseContactForward;
    private float reverseContactPeak;

    private bool HasTouchedThenReversed(CarryableObject pallet)
    {
        if (palletPoseApplier == null || palletVisitLocked) return false;
        Renderer fork = FindForkBRenderer();
        if (fork == null) return false;
        Renderer chassis = forkliftPickupVisual.GetComponentsInChildren<Renderer>()
            .FirstOrDefault(r => r.name.IndexOf("burger_base", StringComparison.OrdinalIgnoreCase) >= 0);
        if (chassis == null) return false;
        Vector3 forward = fork.bounds.center - chassis.bounds.center;
        forward.y = 0f;
        if (forward.sqrMagnitude < 0.000001f) return false;
        forward.Normalize();
        // Use chassis translation, not a rotating fork tip, to detect reverse.
        Vector3 position = palletPoseApplier.DisplayedPosition;
        if (reverseContactPallet == pallet)
        {
            Vector3 delta = position - reverseContactPosition;
            delta.y = 0f;
            float travel = Vector3.Dot(delta, reverseContactForward);
            Vector3 sideways = delta - reverseContactForward * travel;
            if (Vector3.Dot(forward, reverseContactForward) < 0.94f ||
                sideways.magnitude > 0.05f || delta.magnitude > 0.5f)
                reverseContactPallet = null;
            else
            {
                reverseContactPeak = Mathf.Max(reverseContactPeak, travel);
                // Three millimetres suppress pose jitter without a visible insertion requirement.
                if (reverseContactPeak - travel >= 0.003f) return true;
            }
        }
        if (reverseContactPallet != pallet && HasForkBContact(pallet))
        {
            reverseContactPallet = pallet;
            reverseContactPosition = position;
            reverseContactForward = forward;
            reverseContactPeak = 0f;
        }
        return false;
    }

    private bool HasForkBContact(CarryableObject pallet)
    {
        Renderer fork = FindForkBRenderer();
        if (fork == null || pallet == null) return false;
        // Longest horizontal model dimension is the insertion axis.
        Vector3 axis = Vector3.zero;
        foreach (Vector3 candidate in new[] { Vector3.right * fork.localBounds.size.x,
                     Vector3.up * fork.localBounds.size.y, Vector3.forward * fork.localBounds.size.z })
        {
            Vector3 world = fork.transform.TransformVector(candidate);
            world.y = 0f;
            if (world.sqrMagnitude > axis.sqrMagnitude) axis = world;
        }
        if (axis.sqrMagnitude < 0.000001f) return false;
        axis.Normalize();
        Bounds forkBox = default, palletBox = default;
        bool haveFork = false, havePallet = false;
        ProjectMeshBounds(fork, axis, ref forkBox, ref haveFork);
        foreach (Renderer r in pallet.GetComponentsInChildren<Renderer>())
            if (r.enabled && r.GetComponentInParent<CarryableObject>() == pallet)
                ProjectMeshBounds(r, axis, ref palletBox, ref havePallet);
        if (!havePallet) return false;
        float depth = Mathf.Min(forkBox.max.z, palletBox.max.z) - Mathf.Max(forkBox.min.z, palletBox.min.z);
        float width = Mathf.Min(forkBox.max.x, palletBox.max.x) - Mathf.Max(forkBox.min.x, palletBox.min.x);
        float verticalGap = Mathf.Max(0f, Mathf.Max(palletBox.min.y - forkBox.max.y, forkBox.min.y - palletBox.max.y));
        return depth >= -0.005f && width >= -0.005f && verticalGap <= 0.02f;
    }

    private CarryableObject GetProcessPallet()
    {
        switch (processSceneOperation)
        {
            case "DELIVER_OUTER_WALL":
            case "DELIVER_OUTER_WALL_PALLET":
            case "RETURN_OUTER_PALLET": return outerPallet;
            case "DELIVER_INNER_WALL":
            case "DELIVER_INNER_WALL_PALLET":
            case "RETURN_INNER_PALLET": return innerPallet;
            default: return null;
        }
    }

    private bool IsAvailablePallet(CarryableObject pallet)
    {
        return pallet != null && (pallet == outerPallet || pallet == innerPallet) &&
            pallet.gameObject.activeInHierarchy && pallet.State != CarryableState.AttachedToRobot &&
            pallet.State != CarryableState.Installed;
    }

    private bool IsPalletAllowedForProcess(CarryableObject pallet)
    {
        return IsAvailablePallet(pallet) && HasTouchedThenReversed(pallet);
    }

    private CarryableObject GetTouchedPallet()
    {
        if (IsAvailablePallet(reverseContactPallet)) return reverseContactPallet;
        CarryableObject closest = null;
        float distance = float.PositiveInfinity;
        foreach (CarryableObject pallet in new[] { outerPallet, innerPallet })
        {
            if (!IsAvailablePallet(pallet) || !HasForkBContact(pallet)) continue;
            float d = (pallet.transform.position - turtleBotCargoMount.AttachmentPosition).sqrMagnitude;
            if (d < distance) { closest = pallet; distance = d; }
        }
        return closest;
    }

    private void BindForkliftVisualMount()
    {
        if (turtleBotCargoMount == null) return;
        turtleBotCargoMount.PickupFilter = IsPalletAllowedForProcess;
        if (palletPoseApplier == null)
            palletPoseApplier = turtleBotCargoMount.GetComponentInParent<ForkliftPoseApplier>();
        if (palletPoseApplier == null) return;
        if (forkliftPickupVisual == null)
            forkliftPickupVisual = palletPoseApplier.transform.Find("Visual");
        if (forkliftPickupVisual != null)
            turtleBotCargoMount.SetVisualAttachmentPoint(forkliftPickupVisual);
    }

    private void UpdatePalletIdleFallback()
    {
        if (turtleBotCargoMount == null) return;
        if (palletPoseApplier == null)
            palletPoseApplier = turtleBotCargoMount.GetComponentInParent<ForkliftPoseApplier>();
        if (palletPoseApplier == null) return;

        // Includes explicit lift actions: never infer the opposite action at the same stop.
        if (observedPalletCargo != turtleBotCargoMount.CurrentCargo)
            LockPalletVisit();
        Vector3 mountPosition = turtleBotCargoMount.AttachmentPosition;
        if (palletVisitLocked)
        {
            // After a drop, leave contact once before picking up again.
            if (turtleBotCargoMount.HasCargo || HasForkBContact(outerPallet) || HasForkBContact(innerPallet)) return;
            palletVisitLocked = false;
            palletIdleTracking = false;
        }
        if (aligningPallet != null)
        {
            palletIdleTracking = false;
            return;
        }

        bool pickup = !turtleBotCargoMount.HasCargo;
        CarryableObject touching = null;
        if (pickup)
        {
            if (forkliftPickupVisual == null)
                forkliftPickupVisual = palletPoseApplier.GetComponentsInChildren<Transform>(true)
                    .FirstOrDefault(t => t.name == "Visual");
            if (forkliftPickupVisual == null) return;
            // Contact selects the pallet independently of production stage.
            touching = GetTouchedPallet();
            if (!IsPalletAllowedForProcess(touching)) return;
            if (touching != null && turtleBotCargoMount.TryPickup(touching))
            {
                LockPalletVisit();
                pendingLiftPickup = false;
                Debug.Log($"[PalletForkDepth] attached {touching.PayloadId}: forklift_b touched pallet, then robot reversed", this);
            }
            return;
        }

    }

    private void BindFr5GripperSignals()
    {
        if (fr5JointStateApplier == null && fr5CargoMount != null)
        {
            fr5JointStateApplier =
                fr5CargoMount.GetComponentInParent<ZKJointStateApplier>();
        }

        if (fr5JointStateApplier == null)
        {
            fr5JointStateApplier =
                FindObjectsByType<ZKJointStateApplier>(
                        FindObjectsInactive.Include)
                    .FirstOrDefault(item =>
                        item != null && IsFr5(item.RobotId));
        }

        if (fr5JointStateApplier == null)
        {
            Debug.LogWarning(
                "[MaterialFlow] FR5 그리퍼 상태 연결을 찾지 못했습니다. " +
                "공정 상태 기반 보정만 사용합니다.",
                this);
            return;
        }

        fr5JointStateApplier.GripperClosedChanged -=
            HandleFr5GripperClosedChanged;
        fr5JointStateApplier.GripperClosedChanged +=
            HandleFr5GripperClosedChanged;
    }

    private void HandleFr5GripperClosedChanged(bool closed)
    {
        if (!closed) fr5WaitForOpen = false;
        // The first received gripper value describes the initial pose, not a new command.
        if (!fr5GripperBaselineReceived)
        {
            fr5GripperBaselineReceived = true;
            return;
        }
        if (closed)
        {
            fr5PickupCorrectionRequested = true;
            TryPickupExpectedFr5Wall(true);
            return;
        }

        fr5PlacementCorrectionRequested = true;
        TryPlaceExpectedFr5Wall(true);
    }

    private void ApplyFr5OperationSignal(string value, string source)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return;
        }

        activeFr5OperationSignal = NormalizeSignal(value);
        bool known = fr5OperationBaselines.TryGetValue(source, out string previous);
        fr5OperationBaselines[source] = activeFr5OperationSignal;
        // Snapshot/repeated current_operation is state synchronization, not a grip action.
        if (!known || previous == activeFr5OperationSignal || source == "snapshot") return;

        if (IsFr5PickupSignal(activeFr5OperationSignal))
        {
            fr5PickupCorrectionRequested = true;
            TryPickupExpectedFr5Wall(false);
        }

        if (IsFr5ReleaseSignal(activeFr5OperationSignal))
        {
            fr5PlacementCorrectionRequested = true;
            TryPlaceExpectedFr5Wall(false);
        }
    }

    private void UpdateFr5ProcessCorrection()
    {
        if (houseAssembly == null || fr5CargoMount == null)
        {
            return;
        }

        if (fr5CargoMount.HasCargo)
        {
            if (fr5PlacementCorrectionRequested)
            {
                TryPlaceExpectedFr5Wall(false);
            }

            return;
        }

        // A close may arrive before binding/reset: also consume the current known state.
        if (fr5JointStateApplier != null && fr5JointStateApplier.HasReceivedGripperState)
        {
            if (!fr5JointStateApplier.IsGripperClosed) fr5WaitForOpen = false;
            else if (!fr5WaitForOpen) fr5PickupCorrectionRequested = true;
        }
        if (fr5PickupCorrectionRequested)
        {
            TryPickupExpectedFr5Wall(false);
        }
    }

    private bool TryPickupExpectedFr5Wall(bool warnWhenOutOfRange)
    {
        if (processScenePending || (processSceneStep >= 0 && processSceneStep != 4 && processSceneStep != 6)) return false;
        if (!fr5PickupCorrectionRequested || fr5WaitForOpen ||
            fr5CargoMount == null || fr5CargoMount.HasCargo ||
            houseAssembly == null || !houseAssembly.BasePlaced)
        {
            return false;
        }

        if (!houseAssembly.TryGetNextPendingWall(
                out string expectedPayloadId,
                out Transform _))
        {
            fr5PickupCorrectionRequested = false;
            return false;
        }

        CarryableObject expectedWall = FindPayload(
            FindObjectsByType<CarryableObject>(
                FindObjectsInactive.Include),
            expectedPayloadId);

        if (expectedWall == null)
        {
            WarnFr5Correction(
                $"예정 자재를 찾지 못했습니다: {expectedPayloadId}",
                warnWhenOutOfRange);
            return false;
        }

        // Grip/operation signals select the next wall regardless of its current position.
        // The FR5 mount snaps the wall to the gripper (preserveWorldPose=false).
        if (!fr5CargoMount.TryPickup(expectedWall))
        {
            return false;
        }

        fr5PickupCorrectionRequested = false;
        fr5PlacementCorrectionRequested = false;
        fr5WallPickupPosition = fr5CargoMount.transform.position;
        fr5WaitForOpen = true;
        fr5WallTravel = 0f;
        Debug.Log(
            $"[MaterialFlow] FR5 예정 자재 선택 보정: " +
            expectedPayloadId,
            this);
        return true;
    }

    private bool TryPlaceExpectedFr5Wall(bool warnWhenOutOfRange, bool requireProximity = false)
    {
        if (processScenePending) return false;
        if (aligningWall != null) return true;
        if (!fr5PlacementCorrectionRequested ||
            fr5CargoMount == null || !fr5CargoMount.HasCargo ||
            houseAssembly == null)
        {
            return false;
        }

        CarryableObject cargo = fr5CargoMount.CurrentCargo;

        if (cargo == null || cargo.PayloadType != CarryableType.Wall ||
            !houseAssembly.TryGetWallSocket(
                cargo.PayloadId,
                out Transform socket))
        {
            return false;
        }

        if (!houseAssembly.TryGetNextPendingWall(out string nextWall, out Transform _) ||
            !string.Equals(nextWall, cargo.PayloadId, StringComparison.OrdinalIgnoreCase)) return false;

        float distance = Vector3.Distance(
            fr5CargoMount.transform.position,
            socket.position);

        if (requireProximity && distance > Mathf.Max(fr5PlacementCorrectionDistance, 0.01f))
        {
            WarnFr5Correction(
                $"{cargo.PayloadId} 조립 보정 대기: 거리={distance:F3}m",
                warnWhenOutOfRange);
            return false;
        }

        aligningWall = cargo;
        wallTargetSocket = socket;
        wallAlignmentPosition = cargo.transform.position;
        wallAlignmentRotation = cargo.transform.rotation;
        wallAlignmentStarted = Time.unscaledTime;
        Debug.Log($"[WallAlignment] aligning {cargo.PayloadId} to {socket.name}", this);
        return true;
    }

    public bool RequestWallContactPlacement(CarryableObject wall)
    {
        if (wall == null || houseAssembly == null || fr5CargoMount == null || fr5CargoMount.CurrentCargo != wall ||
            fr5WallTravel < 0.05f) return false;
        // Contact alone must not trigger the remote correction reserved for release signals.
        if (!houseAssembly.TryGetWallSocket(wall.PayloadId, out Transform contactSocket) ||
            Vector3.Distance(fr5CargoMount.transform.position, contactSocket.position) >
                Mathf.Max(fr5PlacementCorrectionDistance, 0.01f)) return false;
        fr5PlacementCorrectionRequested = true;
        return TryPlaceExpectedFr5Wall(false, true);
    }

    private void UpdateWallSocketAlignment()
    {
        if (fr5CargoMount != null && fr5CargoMount.HasCargo)
            fr5WallTravel = Mathf.Max(fr5WallTravel,
                Vector3.Distance(fr5CargoMount.transform.position, fr5WallPickupPosition));
        if (aligningWall == null) return;
        if (fr5CargoMount == null || fr5CargoMount.CurrentCargo != aligningWall || wallTargetSocket == null)
        {
            aligningWall = null;
            return;
        }
        float t = Mathf.Clamp01((Time.unscaledTime - wallAlignmentStarted) / 0.65f);
        float blend = t * t * (3f - 2f * t);
        aligningWall.transform.SetPositionAndRotation(
            Vector3.Lerp(wallAlignmentPosition, wallTargetSocket.position, blend),
            Quaternion.Slerp(wallAlignmentRotation, wallTargetSocket.rotation, blend));
        if (t < 1f) return;
        if (houseAssembly.TryInstallFromContact(aligningWall))
            Debug.Log($"[WallAlignment] installed {aligningWall.PayloadId} at {wallTargetSocket.name}", this);
        aligningWall = null;
        wallTargetSocket = null;
        fr5PlacementCorrectionRequested = false;
    }

    private void WarnFr5Correction(string message, bool requested)
    {
        if (!requested || Time.unscaledTime < nextFr5CorrectionWarningTime)
        {
            return;
        }

        nextFr5CorrectionWarningTime = Time.unscaledTime + 2f;
        Debug.LogWarning($"[MaterialFlow] {message}", this);
    }

    private static bool IsWallAssemblyOperation(string signal)
    {
        return !string.IsNullOrWhiteSpace(signal) &&
               signal.Contains("INSTALL") &&
               signal.Contains("WALL");
    }

    private static bool IsFr5PickupSignal(string signal)
    {
        return !string.IsNullOrWhiteSpace(signal) &&
               (signal.Contains("PICK") ||
                signal.Contains("GRASP") ||
                signal.Contains("GRIPPER_CLOSE") ||
                signal.Contains("CLOSE_GRIPPER") ||
                signal.Contains("CLAMP"));
    }

    private static bool IsFr5ReleaseSignal(string signal)
    {
        return !string.IsNullOrWhiteSpace(signal) &&
               (signal.Contains("PLACE") ||
                signal.Contains("RELEASE") ||
                signal.Contains("GRIPPER_OPEN") ||
                signal.Contains("OPEN_GRIPPER") ||
                signal.Contains("UNGRIP"));
    }

    private static bool IsFr5(string robotId)
    {
        string normalized = NormalizeSignal(robotId ?? string.Empty);
        return normalized == "FR5" ||
               normalized == "FR5_01" ||
               normalized.StartsWith("FR5_");
    }

    private bool IsTurtleBot(string robotId)
    {
        if (string.IsNullOrWhiteSpace(robotId))
        {
            return false;
        }

        string normalized = NormalizeSignal(robotId);
        string configured = NormalizeSignal(turtleBotRobotId);

        return normalized == configured ||
               normalized == "FORKLIFT_01" ||
               normalized == "MOBILE_ROBOT_1" ||
               normalized.Contains("TURTLEBOT");
    }

    private static bool IsLiftUpSignal(string signal)
    {
        return (signal.Contains("LIFT") &&
                (signal.Contains("UP") || signal.Contains("RAIS"))) ||
               signal == "UP" ||
               signal == "LOADED" ||
               (signal.Contains("PICK") && signal.Contains("UP")) ||
               (signal.Contains("PICKUP") &&
                (signal.Contains("COMPLETE") || signal.Contains("ATTACH")));
    }

    private static bool IsLiftDownSignal(string signal)
    {
        return (signal.Contains("LIFT") &&
                (signal.Contains("DOWN") || signal.Contains("LOWER"))) ||
               signal == "DOWN" ||
               signal == "UNLOADED" ||
               (signal.Contains("DROP") && signal.Contains("OFF")) ||
               (signal.Contains("DROPOFF") && signal.Contains("COMPLETE")) ||
               signal.Contains("RELEASE");
    }

    private static string NormalizeSignal(string value)
    {
        return value.Trim()
            .Replace('-', '_')
            .Replace(' ', '_')
            .ToUpperInvariant();
    }

    private void EnsureRuntimeSceneReferences()
    {
        roofVariantSpawner?.SetStagingBase(houseAssembly?.AssemblyRoot);

        if (completedHouseDropPoint != null)
        {
            return;
        }

        Transform finish = Resources.FindObjectsOfTypeAll<Transform>()
            .FirstOrDefault(item =>
                item.gameObject.scene == gameObject.scene &&
                string.Equals(
                    item.name,
                    "final",
                    StringComparison.OrdinalIgnoreCase));

        if (finish == null)
        {
            Debug.LogWarning(
                "[MaterialFlow] final 위치를 찾지 못했습니다.",
                this);
            return;
        }

        Transform referenceHouse = FindDescendantNormalized(
            finish,
            "House_B_COMPLETE");
        Transform referenceBase = FindDescendantNormalized(
            referenceHouse,
            "B_base");
        Transform source = referenceBase != null
            ? referenceBase
            : referenceHouse;

        if (source == null)
        {
            Debug.LogWarning(
                "[MaterialFlow] final 내부의 위치 기준 주택을 " +
                "찾지 못했습니다.",
                this);
            return;
        }

        Transform marker = finish.Find("FinalDropPoint");

        if (marker == null)
        {
            GameObject markerObject = new GameObject("FinalDropPoint");
            marker = markerObject.transform;
            marker.SetParent(finish, true);
        }

        marker.SetPositionAndRotation(source.position, source.rotation);
        marker.localScale = Vector3.one;
        completedHouseDropPoint = marker;

        CompletedHouseFinishZone finishZone =
            finish.GetComponent<CompletedHouseFinishZone>();

        if (finishZone == null)
        {
            finishZone = finish.gameObject.AddComponent<
                CompletedHouseFinishZone>();
        }

        finishZone.Configure(marker);

        Collider finishCollider = finish.GetComponent<Collider>();

        if (finishCollider != null)
        {
            finishCollider.isTrigger = true;
        }

        referenceHouse.gameObject.SetActive(false);
        Debug.Log(
            "[MaterialFlow] final 기준 모델에서 FinalDropPoint를 " +
            "구성했습니다.",
            this);
    }

    private void ConfigureAssemblyRobotCargoRoles()
    {
        // 실제 공정 역할: FR5는 벽만, ZK는 Base와 지붕만 취급합니다.
        // 접촉 결합 범위를 여기서도 강제해 씬 직렬화 값이 오래됐더라도
        // FR5가 Base를 끌고 가는 현상을 막습니다.
        fr5CargoMount?.Configure(
            RobotCarrierRole.FR5,
            CarryableType.Wall,
            fr5CargoMount.transform,
            false,
            false);

        zkCargoMount?.Configure(
            RobotCarrierRole.ZK,
            houseAssembly != null && houseAssembly.StartWithBasePlaced
                ? CarryableType.Roof
                : CarryableType.Base | CarryableType.Roof,
            zkCargoMount.transform);
    }

    private void QueueProcessScene(ProductionJobData job)
    {
        processSceneOperation = NormalizeSignal(job?.current_operation);
    }





    private void EnsurePalletDropPoints()
    {
        if (innerPalletReturnPoint == null && innerPallet != null)
            innerPalletReturnPoint = CreatePalletDropPoint(innerPallet, "InnerPalletReturnPoint",
                innerPallet.InitialWorldPosition, innerPallet.InitialWorldRotation.eulerAngles);
        if (outerPalletReturnPoint == null && outerPallet != null)
            outerPalletReturnPoint = CreatePalletDropPoint(outerPallet, "OuterPalletReturnPoint",
                outerPallet.InitialWorldPosition, outerPallet.InitialWorldRotation.eulerAngles);
        if (innerSupplyPoint == null)
        {
            innerSupplyPoint = CreatePalletDropPoint(
                innerPallet,
                "InnerPalletDropPoint",
                innerPalletDropPosition,
                innerPalletDropEuler);
        }

        if (outerSupplyPoint == null)
        {
            outerSupplyPoint = CreatePalletDropPoint(
                outerPallet,
                "OuterPalletDropPoint",
                outerPalletDropPosition,
                outerPalletDropEuler);
        }
    }

    private Transform CreatePalletDropPoint(
        CarryableObject pallet,
        string markerName,
        Vector3 worldPosition,
        Vector3 worldEuler)
    {
        Transform parent = pallet != null && pallet.InitialParent != null
            ? pallet.InitialParent
            : transform;
        Transform marker = parent.Find(markerName);

        if (marker == null)
        {
            GameObject markerObject = new GameObject(markerName);
            marker = markerObject.transform;
            marker.SetParent(parent, true);
        }

        marker.SetPositionAndRotation(
            worldPosition,
            Quaternion.Euler(worldEuler));
        marker.localScale = Vector3.one;
        return marker;
    }

    private static Transform FindDescendantNormalized(
        Transform root,
        string objectName)
    {
        if (root == null)
        {
            return null;
        }

        if (string.Equals(
                NormalizeReferenceName(root.name),
                objectName,
                StringComparison.OrdinalIgnoreCase))
        {
            return root;
        }

        foreach (Transform child in root)
        {
            Transform found = FindDescendantNormalized(child, objectName);

            if (found != null)
            {
                return found;
            }
        }

        return null;
    }

    private static string NormalizeReferenceName(string value)
    {
        string result = (value ?? string.Empty)
            .Replace("(Clone)", string.Empty)
            .Trim();
        int suffixStart = result.LastIndexOf(" (", StringComparison.Ordinal);

        if (suffixStart >= 0 && result.EndsWith(")"))
        {
            string suffix = result.Substring(
                suffixStart + 2,
                result.Length - suffixStart - 3);

            if (int.TryParse(suffix, out _))
            {
                result = result.Substring(0, suffixStart);
            }
        }

        return result;
    }
}
