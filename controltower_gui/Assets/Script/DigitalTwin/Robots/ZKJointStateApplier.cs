using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Serialization;

public class ZKJointStateApplier : MonoBehaviour
{
    [System.Serializable]
    public class JointBinding
    {
        [Tooltip("서버 joint_names에 들어오는 이름")]
        public string jointName;

        [Tooltip("실제로 움직일 Unity ArticulationBody")]
        public ArticulationBody joint;

        [Tooltip("회전 방향이 반대이면 -1")]
        public float direction = 1f;

        [Tooltip("Unity 모델 초기 자세 보정값")]
        public float offsetDegrees = 0f;

        [System.NonSerialized]
        public float targetDegrees;

        [System.NonSerialized]
        public bool hasTarget;
    }

    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("Robot Identification")]
    [SerializeField]
    private string robotId = "zkbot2";

    [Header("Joint Bindings")]
    [SerializeField]
    private JointBinding[] jointBindings;

    [Header("ZK HOME Calibration (photo estimate)")]
    [Tooltip("ZK model angles at robot HOME in degrees. Initial photo estimate; verify against measured poses. Added to each binding's fine offset.")]
    [SerializeField] private Vector3 zkHomeModelDegrees = new Vector3(0f, -60f, -42f);


    [Header("Parallel Jaw Gripper Display")]
    [Tooltip("비워 두면 FR5 아래의 Finger_L을 자동으로 찾습니다.")]
    [SerializeField]
    private Transform leftFinger;

    [Tooltip("비워 두면 FR5 아래의 Finger_R을 자동으로 찾습니다.")]
    [SerializeField]
    private Transform rightFinger;

    [Tooltip("완전히 닫혔을 때 처음 벌어진 간격 중 남길 비율")]
    [Range(0f, 0.5f)]
    [SerializeField]
    private float closedSeparationRatio = 0.12f;

    [Tooltip("robot_status.current_operation의 OPEN/CLOSE/GRIP 동작도 그리퍼에 반영합니다.")]
    [SerializeField]
    private bool inferGripperFromRobotOperation = true;

    [Range(0f, 1f)]
    [SerializeField]
    private float gripperClosedThreshold = 0.3f;

    [Range(0f, 1f)]
    [SerializeField]
    private float gripperOpenThreshold = 0.55f;

    [Header("Pose Display")]
    [FormerlySerializedAs("fr5DriveStiffness")]
    [Tooltip("표시된 자세를 유지할 Articulation Drive 강성")]
    [SerializeField]
    private float displayDriveStiffness = 10000f;

    [FormerlySerializedAs("fr5DriveDamping")]
    [Tooltip("표시된 자세를 유지할 Articulation Drive 감쇠")]
    [SerializeField]
    private float displayDriveDamping = 1000f;

    [FormerlySerializedAs("fr5MinimumForceLimit")]
    [Tooltip("표시된 자세를 유지할 Articulation Drive 최소 힘")]
    [SerializeField]
    private float displayMinimumForceLimit = 10000f;

    [Header("Debug")]
    [SerializeField]
    private bool logAppliedJointState = false;

    [SerializeField]
    private float appliedJointLogIntervalSeconds = 2f;

    private readonly Dictionary<string, JointBinding> joints =
        new Dictionary<string, JointBinding>();

    private readonly HashSet<string> warnedUnknownJoints =
        new HashSet<string>();

    private int diagJointEvents, diagMatchingEvents;
    private float nextJointSummary;
    private string lastReceivedRobot = "none";
    [Header("ZK Suction Orientation")]
    [SerializeField] private bool keepZkSuctionDown = true;
    private ArticulationBody zkToolBody;
    private Transform zkToolParent;
    private Quaternion zkToolOriginalParentAnchor;
    private bool zkToolOriginalMatchAnchors;

    private bool diagnosticPending;
    private float diagnosticTime;
    private float nextAppliedJointLogTime;
    private Transform gripperSpace;
    private Vector3 leftFingerOpenPosition;
    private Vector3 rightFingerOpenPosition;
    private Vector3 gripperCenter;
    private bool gripperReady;
    private float currentGripperOpening01 = 1f;
    private bool gripperStateInitialized;
    private bool reportedGripperClosed;

    private float lastLiveJointTime = float.NegativeInfinity;
    public bool HasRecentLiveJoints => Time.unscaledTime - lastLiveJointTime < 2f;
    public string RobotId => robotId;
    public bool HasVisualGripper => gripperReady;
    public bool HasReceivedGripperState => gripperStateInitialized;
    public bool IsGripperClosed => reportedGripperClosed;
    public float CurrentGripperOpening01 => currentGripperOpening01;
    public event Action<bool> GripperClosedChanged;

    private void Awake()
    {
        if (robotId == "zkbot2")
            Debug.Log($"[JointDiag:HomeCalibration] photoEstimate={zkHomeModelDegrees}", this);
        RegisterJoints();
        InitializeGripper();
    }

    private void OnEnable()
    {
        InitializeZkSuctionOrientation();
        if (factoryStateManager == null)
        {
            factoryStateManager =
                FindAnyObjectByType<FactoryStateManager>();
        }

        if (factoryStateManager == null)
        {
            Debug.LogError(
                $"[{robotId}] FactoryStateManager가 연결되지 않았습니다.",
                this);

            return;
        }

        factoryStateManager.RobotJointStateReceived +=
            HandleJointState;
        factoryStateManager.RobotStatusUpdated +=
            HandleRobotStatus;

        if (factoryStateManager.TryGetRobotStatus(
                robotId,
                out RobotStatusData status))
        {
            HandleRobotStatus(status);
        }
    }

    private void OnDisable()
    {
        if (zkToolBody != null)
        {
            zkToolBody.parentAnchorRotation = zkToolOriginalParentAnchor;
            zkToolBody.matchAnchors = zkToolOriginalMatchAnchors;
            zkToolBody = null;
            zkToolParent = null;
        }
        if (factoryStateManager != null)
        {
            factoryStateManager.RobotJointStateReceived -=
                HandleJointState;
            factoryStateManager.RobotStatusUpdated -=
                HandleRobotStatus;
        }
    }

    private void Update()
    {
        if (robotId != "zkbot2" || Time.unscaledTime < nextJointSummary) return;
        nextJointSummary = Time.unscaledTime + 5f;
        Debug.Log($"[JointDiag:ZK] active={isActiveAndEnabled} manager={(factoryStateManager != null)} " +
            $"registered={joints.Count} events={diagJointEvents} matched={diagMatchingEvents} " +
            $"lastRobot={lastReceivedRobot} timeScale={Time.timeScale}", this);
    }

    private void InitializeZkSuctionOrientation()
    {
        if (robotId != "zkbot2" || !keepZkSuctionDown) return;
        foreach (ArticulationBody body in GetComponentsInChildren<ArticulationBody>(true))
        {
            if (body.name != "suction_cup" || body.jointType != ArticulationJointType.FixedJoint) continue;
            zkToolBody = body;
            zkToolParent = body.transform.parent;
            zkToolOriginalParentAnchor = body.parentAnchorRotation;
            zkToolOriginalMatchAnchors = body.matchAnchors;
            body.matchAnchors = false;
            return;
        }
        Debug.LogWarning("[ZK Suction] Fixed suction_cup joint was not found.", this);
    }

    private void KeepZkSuctionPointingDown()
    {
        if (!keepZkSuctionDown || zkToolBody == null || zkToolParent == null) return;
        // Imported suction cup points down at model zero. Level its local up axis
        // in world space, preserving its heading and the wrist pivot position.
        // Adjust only the suction cup anchor; leave link_tool and all robot axes unchanged.
        Transform tool = zkToolBody.transform;
        Quaternion levelWorld = Quaternion.FromToRotation(tool.up, Vector3.up) * tool.rotation;
        zkToolBody.parentAnchorRotation = Quaternion.Inverse(zkToolParent.rotation) *
            levelWorld * zkToolBody.anchorRotation;
        zkToolBody.WakeUp();
    }

    private void FixedUpdate()
    {
        KeepZkSuctionPointingDown();
        if (diagnosticPending && Time.time >= diagnosticTime)
        {
            diagnosticPending = false;
            LogActualJointPositions();
        }
    }

    private float GetDisplayDirection(JointBinding binding)
    {
        // ZK A2 and A3 feedback run opposite to the imported model axes.
        // Keep HOME offsets independent of direction and leave FR5 unchanged.
        return robotId == "zkbot2" && (binding.jointName == "a2_joint" || binding.jointName == "a3_joint")
            ? -binding.direction
            : binding.direction;
    }

    private float GetDisplayOffset(JointBinding binding)
    {
        float home = 0f;
        if (robotId == "zkbot2")
        {
            switch (binding.jointName)
            {
                case "a1_joint": home = zkHomeModelDegrees.x; break;
                case "a2_joint": home = zkHomeModelDegrees.y; break;
                case "a3_joint": home = zkHomeModelDegrees.z; break;
            }
        }
        return binding.offsetDegrees + home;
    }

    private void RegisterJoints()
    {
        joints.Clear();

        if (jointBindings == null)
        {
            return;
        }

        foreach (JointBinding binding in jointBindings)
        {
            if (binding == null ||
                string.IsNullOrWhiteSpace(binding.jointName) ||
                binding.joint == null)
            {
                continue;
            }

            if (joints.ContainsKey(binding.jointName))
            {
                Debug.LogWarning(
                    $"[{robotId}] 중복 관절 이름: {binding.jointName}",
                    this);

                continue;
            }

            joints.Add(binding.jointName, binding);

            ConfigureDrive(binding);
        }
    }

    private void ConfigureDrive(JointBinding binding)
    {
        if (binding.joint == null)
        {
            return;
        }

        ArticulationDrive drive = binding.joint.xDrive;

        // Limits supplied by the robot are in robot coordinates. Transform both
        // endpoints so HOME calibration is not clipped by the old model limits.
        if (robotId == "zkbot2")
        {
            float robotLower;
            switch (binding.jointName)
            {
                case "a1_joint": robotLower = -190f; break;
                case "a2_joint": robotLower = -55f; break;
                case "a3_joint": robotLower = -110f; break;
                default: robotLower = float.NaN; break;
            }
            if (!float.IsNaN(robotLower))
            {
                float endA = robotLower * GetDisplayDirection(binding) + GetDisplayOffset(binding);
                float endB = 2f * GetDisplayDirection(binding) + GetDisplayOffset(binding);
                drive.lowerLimit = Mathf.Min(endA, endB);
                drive.upperLimit = Mathf.Max(endA, endB);
            }
        }

        drive.stiffness = Mathf.Max(
            displayDriveStiffness,
            10000f);
        drive.damping = Mathf.Max(
            displayDriveDamping,
            1000f);
        drive.forceLimit = Mathf.Max(
            displayMinimumForceLimit,
            10000f);

        binding.joint.xDrive = drive;

        // 서버 자세를 그대로 보여주는 디지털 트윈이므로 중력으로 처지지 않게 합니다.
        binding.joint.useGravity = false;
    }

    private void HandleJointState(
        RobotJointStateData jointState)
    {
        if (jointState == null)
        {
            return;
        }

        diagJointEvents++;
        lastReceivedRobot = jointState.robot_id;
        if (jointState.robot_id != robotId)
        {
            return;
        }

        diagMatchingEvents++;
        lastLiveJointTime = Time.unscaledTime;
        ApplyJointState(
            jointState.joint_names,
            jointState.positions);
    }

    public void ApplyJointState(
        string[] jointNames,
        float[] positions)
    {
        if (jointNames == null || positions == null)
        {
            Debug.LogWarning(
                $"[{robotId}] 관절 데이터가 null입니다.",
                this);

            return;
        }

        if (jointNames.Length != positions.Length)
        {
            Debug.LogWarning(
                $"[{robotId}] joint_names와 positions 길이가 다릅니다.",
                this);

            return;
        }

        int appliedCount = 0;
        StringBuilder appliedTargets = new StringBuilder();

        for (int i = 0; i < jointNames.Length; i++)
        {
            string jointName = jointNames[i];
            float radians = positions[i];

            if (float.IsNaN(radians) ||
                float.IsInfinity(radians))
            {
                continue;
            }

            if (TryApplyGripperJoint(jointName, radians))
            {
                appliedCount++;

                if (appliedTargets.Length > 0)
                {
                    appliedTargets.Append(", ");
                }

                appliedTargets.Append(jointName);
                appliedTargets.Append("=gripper ");
                appliedTargets.Append(
                    (currentGripperOpening01 * 100f).ToString("F0"));
                appliedTargets.Append('%');
                continue;
            }

            if (!TryGetJointBinding(
                    jointName,
                    out JointBinding binding))
            {
                if (warnedUnknownJoints.Add(jointName))
                {
                    Debug.LogWarning(
                        $"[{robotId}] 알 수 없는 관절: {jointName}",
                        this);
                }

                continue;
            }

            binding.targetDegrees =
                radians *
                Mathf.Rad2Deg *
                GetDisplayDirection(binding) +
                GetDisplayOffset(binding);

            ApplyBindingTarget(binding);
            appliedCount++;

            if (appliedTargets.Length > 0)
            {
                appliedTargets.Append(", ");
            }

            appliedTargets.Append(jointName);
            appliedTargets.Append('=');
            appliedTargets.Append(binding.targetDegrees.ToString("F1"));
            appliedTargets.Append('°');
        }

        if ((logAppliedJointState || robotId == "zkbot2") &&
            Time.unscaledTime >= nextAppliedJointLogTime)
        {
            nextAppliedJointLogTime = Time.unscaledTime +
                Mathf.Max(appliedJointLogIntervalSeconds, 0.25f);
            Debug.Log(
                $"[JointApply] robot={robotId}, " +
                $"received={jointNames.Length}, applied={appliedCount}, " +
                $"targets=[{appliedTargets}]",
                this);

            if (!diagnosticPending)
            {
                diagnosticPending = true;
                diagnosticTime = Time.time + 0.75f;
            }
        }
    }

    private void ApplyBindingTarget(JointBinding binding)
    {
        ArticulationDrive drive = binding.joint.xDrive;
        float targetDegrees = ClampToDrive(
            drive,
            binding.targetDegrees);

        binding.targetDegrees = targetDegrees;
        binding.hasTarget = true;
        drive.target = targetDegrees;
        binding.joint.xDrive = drive;

        // 목표와 현재 관절값을 수신 순간 함께 바꿉니다.
        // 별도의 보간/애니메이션/매 프레임 강제 이동은 수행하지 않습니다.
        if (binding.joint.dofCount > 0)
        {
            float targetRadians = targetDegrees * Mathf.Deg2Rad;
            binding.joint.jointPosition =
                new ArticulationReducedSpace(targetRadians);
            binding.joint.jointVelocity =
                new ArticulationReducedSpace(0f);
        }

        binding.joint.WakeUp();
    }

    private static float ClampToDrive(
        ArticulationDrive drive,
        float targetDegrees)
    {
        if (drive.lowerLimit < drive.upperLimit)
        {
            return Mathf.Clamp(
                targetDegrees,
                drive.lowerLimit,
                drive.upperLimit);
        }

        return targetDegrees;
    }

    public float GetMaxJointErrorDegrees(
        string[] jointNames,
        float[] expectedRadians)
    {
        if (jointNames == null ||
            expectedRadians == null ||
            jointNames.Length != expectedRadians.Length)
        {
            return float.PositiveInfinity;
        }

        float maximumError = 0f;

        for (int i = 0; i < jointNames.Length; i++)
        {
            if (!TryGetJointBinding(
                    jointNames[i],
                    out JointBinding binding) ||
                binding.joint == null ||
                binding.joint.jointPosition.dofCount == 0)
            {
                return float.PositiveInfinity;
            }

            float expectedDegrees =
                expectedRadians[i] *
                Mathf.Rad2Deg *
                GetDisplayDirection(binding) +
                GetDisplayOffset(binding);

            expectedDegrees = ClampToDrive(
                binding.joint.xDrive,
                expectedDegrees);

            float actualDegrees =
                binding.joint.jointPosition[0] *
                Mathf.Rad2Deg;

            maximumError = Mathf.Max(
                maximumError,
                Mathf.Abs(Mathf.DeltaAngle(
                    expectedDegrees,
                    actualDegrees)));
        }

        return maximumError;
    }

    /// <summary>
    /// 현재 Articulation 자세를 서버가 사용하는 관절 라디안 값으로 읽습니다.
    /// 로컬 통합 테스트가 끝난 뒤 테스트 전 자세를 정확히 복원할 때 사용합니다.
    /// </summary>
    public bool TryGetCurrentJointPositions(
        string[] jointNames,
        out float[] positionsRadians)
    {
        positionsRadians = null;

        if (jointNames == null || jointNames.Length == 0)
        {
            return false;
        }

        float[] currentPositions = new float[jointNames.Length];

        for (int i = 0; i < jointNames.Length; i++)
        {
            if (!TryGetJointBinding(
                    jointNames[i],
                    out JointBinding binding) ||
                binding.joint == null ||
                binding.joint.jointPosition.dofCount == 0 ||
                Mathf.Abs(GetDisplayDirection(binding)) < 0.0001f)
            {
                return false;
            }

            float articulationDegrees =
                binding.joint.jointPosition[0] * Mathf.Rad2Deg;

            currentPositions[i] =
                (articulationDegrees - GetDisplayOffset(binding)) /
                GetDisplayDirection(binding) * Mathf.Deg2Rad;
        }

        positionsRadians = currentPositions;
        return true;
    }

    private bool TryGetJointBinding(
        string jointName,
        out JointBinding binding)
    {
        if (joints.TryGetValue(jointName, out binding))
        {
            return true;
        }

        // FR5 씬은 기존에 j1~j6로 바인딩되어 있었지만
        // 서버 규격은 joint1~joint6를 사용합니다.
        if (robotId != "fr5" ||
            string.IsNullOrWhiteSpace(jointName))
        {
            return false;
        }

        const string serverPrefix = "joint";

        if (jointName.StartsWith(
                serverPrefix,
                System.StringComparison.OrdinalIgnoreCase))
        {
            string number = jointName.Substring(serverPrefix.Length);
            return joints.TryGetValue("j" + number, out binding);
        }

        if (jointName.StartsWith(
                "j",
                System.StringComparison.OrdinalIgnoreCase))
        {
            string number = jointName.Substring(1);
            return joints.TryGetValue(serverPrefix + number, out binding);
        }

        return false;
    }

    private void LogActualJointPositions()
    {
        StringBuilder actualPositions = new StringBuilder();

        foreach (JointBinding binding in jointBindings)
        {
            if (binding == null || binding.joint == null)
            {
                continue;
            }

            if (actualPositions.Length > 0)
            {
                actualPositions.Append(", ");
            }

            ArticulationReducedSpace position =
                binding.joint.jointPosition;

            float actualDegrees = position.dofCount > 0
                ? position[0] * Mathf.Rad2Deg
                : 0f;

            actualPositions.Append(binding.jointName);
            actualPositions.Append('=');
            actualPositions.Append(actualDegrees.ToString("F1"));
            actualPositions.Append('°');
        }

        Debug.Log(
            $"[JointActual +0.75s] robot={robotId}, " +
            $"positions=[{actualPositions}]",
            this);
    }

    public void SetGripperOpen(bool open)
    {
        SetGripperOpening(open ? 1f : 0f);
    }

    public void SetGripperOpening(float opening01)
    {
        if (!gripperReady)
        {
            return;
        }

        currentGripperOpening01 = Mathf.Clamp01(opening01);
        float separationScale = Mathf.Lerp(
            closedSeparationRatio,
            1f,
            currentGripperOpening01);

        SetFingerPosition(
            leftFinger,
            Vector3.LerpUnclamped(
                gripperCenter,
                leftFingerOpenPosition,
                separationScale));
        SetFingerPosition(
            rightFinger,
            Vector3.LerpUnclamped(
                gripperCenter,
                rightFingerOpenPosition,
                separationScale));

        bool nextClosed = reportedGripperClosed;

        if (currentGripperOpening01 <= gripperClosedThreshold)
        {
            nextClosed = true;
        }
        else if (currentGripperOpening01 >= gripperOpenThreshold)
        {
            nextClosed = false;
        }

        if (!gripperStateInitialized ||
            nextClosed != reportedGripperClosed)
        {
            gripperStateInitialized = true;
            reportedGripperClosed = nextClosed;
            GripperClosedChanged?.Invoke(nextClosed);
        }
    }

    private void InitializeGripper()
    {
        if (leftFinger == null)
        {
            leftFinger = FindDescendant("Finger_L");
        }

        if (rightFinger == null)
        {
            rightFinger = FindDescendant("Finger_R");
        }

        if (leftFinger == null || rightFinger == null)
        {
            return;
        }

        gripperSpace = leftFinger.parent == rightFinger.parent
            ? leftFinger.parent
            : transform;
        leftFingerOpenPosition = gripperSpace.InverseTransformPoint(
            leftFinger.position);
        rightFingerOpenPosition = gripperSpace.InverseTransformPoint(
            rightFinger.position);
        gripperCenter =
            (leftFingerOpenPosition + rightFingerOpenPosition) * 0.5f;
        gripperReady = Vector3.Distance(
            leftFingerOpenPosition,
            rightFingerOpenPosition) > 0.00001f;
    }

    private Transform FindDescendant(string exactName)
    {
        Transform[] descendants =
            GetComponentsInChildren<Transform>(true);

        foreach (Transform descendant in descendants)
        {
            if (descendant.name == exactName)
            {
                return descendant;
            }
        }

        return null;
    }

    private void SetFingerPosition(
        Transform finger,
        Vector3 positionInGripperSpace)
    {
        Vector3 worldPosition = gripperSpace.TransformPoint(
            positionInGripperSpace);
        finger.position = worldPosition;
    }

    private bool TryApplyGripperJoint(
        string jointName,
        float value)
    {
        if (!gripperReady || string.IsNullOrWhiteSpace(jointName))
        {
            return false;
        }

        string normalized = jointName.Trim()
            .Replace('-', '_')
            .ToLowerInvariant();
        bool isGripper = normalized == "joint7" || normalized == "j7" ||
            normalized.Contains("gripper") ||
            normalized.Contains("finger") ||
            normalized.Contains("jaw");

        if (!isGripper)
        {
            return false;
        }

        float magnitude = Mathf.Abs(value);
        float opening;

        if (normalized.Contains("width") ||
            normalized.Contains("opening"))
        {
            float openSeparation = Vector3.Distance(
                leftFingerOpenPosition,
                rightFingerOpenPosition);
            opening = openSeparation > 0.00001f
                ? magnitude / openSeparation
                : 0f;
        }
        else if (magnitude > 2f)
        {
            // 퍼센트(0~100)로 전달하는 제어기도 허용합니다.
            opening = magnitude / 100f;
        }
        else
        {
            // 0~1 정규화 값 또는 작은 radian 값을 그대로 개방률로 사용합니다.
            opening = magnitude;
        }

        SetGripperOpening(opening);
        return true;
    }

    private void HandleRobotStatus(RobotStatusData status)
    {
        if (status == null ||
            NormalizeRobotId(status.robot_id) != NormalizeRobotId(robotId))
        {
            return;
        }

        // Server contract: grip is 0..100 and a larger value means more open.
        // It is the authoritative visual signal. grip_real is intentionally
        // retained for diagnostics only because it can become stale in motion.
        if (status.has_grip)
        {
            SetGripperOpening(status.grip / 100f);
            return;
        }

        if (!inferGripperFromRobotOperation)
        {
            return;
        }

        string operation = string.IsNullOrWhiteSpace(status.current_operation)
            ? string.Empty
            : status.current_operation.Trim().ToUpperInvariant();

        if (operation.Contains("GRIPPER_OPEN") ||
            operation.Contains("OPEN_GRIPPER") ||
            operation.Contains("RELEASE") ||
            operation.Contains("UNGRIP"))
        {
            SetGripperOpen(true);
        }
        else if (operation.Contains("GRIPPER_CLOSE") ||
                 operation.Contains("CLOSE_GRIPPER") ||
                 operation.Contains("GRASP") ||
                 operation.Contains("GRIP") ||
                 operation.Contains("CLAMP"))
        {
            SetGripperOpen(false);
        }
    }

    private static string NormalizeRobotId(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? string.Empty
            : value.Trim().Replace('-', '_').ToLowerInvariant();
    }
}
