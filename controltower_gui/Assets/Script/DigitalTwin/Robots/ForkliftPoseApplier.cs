using System;
using System.Collections.Generic;
using UnityEngine;

public class ForkliftPoseApplier : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("Robot Identification")]
    [SerializeField]
    private string robotId = "forklift_01";

    [Header("Transform")]
    [SerializeField]
    private Transform forkliftRoot;

    [Tooltip("기존 시작점 정렬 옵션: 서버 원점을 씬 HOME에 고정합니다. 첫 수신 위치로 재설정하지 않습니다.")]
    [SerializeField]
    private bool alignFirstPoseToSceneStart = true;

    [Tooltip("서버 map 원점을 씬에 배치한 HOME 시작 자세로 사용합니다.")]
    [SerializeField]
    private bool mapOriginIsSceneStart = true;

    [Tooltip("자동 시작점 정렬 후 필요한 미세 위치 보정값입니다.")]
    [SerializeField]
    private Vector3 positionOffset = Vector3.zero;

    [Header("Odometry Home Correction")]
    [Tooltip("HOME에서 odometry 원점이 초기화되면 현재 화면 위치를 유지하도록 좌표 오프셋을 다시 계산합니다.")]
    [SerializeField]
    private bool compensateOdomResetAtHome = true;

    [Tooltip("초기화된 odometry를 원점으로 판단할 XZ 반경(m)입니다.")]
    [SerializeField]
    private float odomResetOriginRadius = 0.12f;

    [Tooltip("연속 Pose 사이의 위치 변화가 이 값보다 클 때 원점 초기화 후보로 봅니다.")]
    [SerializeField]
    private float odomResetJumpDistance = 0.25f;

    [Tooltip("연속 Pose 사이의 방향 변화가 이 값보다 클 때 원점 초기화 후보로 봅니다.")]
    [SerializeField]
    private float odomResetJumpAngle = 35f;

    [Tooltip("HOME 부근에서 서버 보정 Pose가 바뀌면 순간이동 대신 짧게 보간합니다.")]
    [SerializeField]
    private bool smoothHomeCorrections = true;

    [SerializeField]
    private float homeCorrectionRadius = 0.3f;

    [SerializeField]
    private float homeCorrectionJumpDistance = 0.08f;

    [SerializeField]
    private float homeCorrectionBlendSeconds = 0.45f;

    [Header("Rack Collision Guard")]
    [Tooltip("서버 Pose가 Rack1/Rack2를 가로질러도 Unity 모델은 랙 앞에서 멈춥니다.")]
    [SerializeField]
    private bool preventRackPassThrough = true;

    [Tooltip("터틀봇의 X/Z 충돌 반경입니다. 팔레트가 아닌 차체 기준입니다.")]
    [SerializeField]
    private Vector2 collisionHalfExtentsXZ = new Vector2(0.11f, 0.14f);

    [SerializeField]
    private float collisionSkin = 0.005f;

    [Header("Debug")]
    [SerializeField]
    private bool logAppliedPose = true;

    [SerializeField]
    private float appliedPoseLogIntervalSeconds = 2f;

    private Vector3 sceneStartPosition;
    private Quaternion sceneStartRotation;
    private Vector3 automaticPositionOffset;
    private Quaternion automaticRotationOffset = Quaternion.identity;
    private bool poseCalibrated;
    private float lastPalletPoseTime = float.NegativeInfinity;
    public bool HasRecentPalletPose => Time.unscaledTime - lastPalletPoseTime < 2f;
    public Vector3 PalletMotionPosition { get; private set; }
    public Quaternion PalletMotionRotation { get; private set; }

    private float nextAppliedPoseLogTime;
    private float nextCollisionWarningTime;
    private Collider[] rackBlockers = Array.Empty<Collider>();
    private bool hasPreviousRawPose;
    private Vector3 previousConvertedPosition;
    private Quaternion previousConvertedRotation = Quaternion.identity;
    private bool homeCorrectionActive;
    private Vector3 homeCorrectionTargetPosition;
    private Quaternion homeCorrectionTargetRotation = Quaternion.identity;

    private Vector3 workAlignmentOffset;
    private Vector3 workAlignmentTarget;

    public void AddWorkAlignment(Vector3 worldDelta)
    {
        worldDelta.y = 0f;
        workAlignmentTarget = workAlignmentOffset + worldDelta;
        homeCorrectionActive = false;
    }

    public void StopWorkAlignment()
    {
        workAlignmentTarget = workAlignmentOffset;
    }

    private void LateUpdate()
    {
        UpdatePalletContactBox();
    }

    public Vector3 SceneStartPosition => sceneStartPosition;
    public Vector3 DisplayedPosition => forkliftRoot != null
        ? forkliftRoot.position
        : transform.position;

    private void Awake()
    {
        if (forkliftRoot == null)
        {
            forkliftRoot = transform;
        }

        RecenterChassisPivot();
        UpdatePalletContactBox();
        sceneStartPosition = forkliftRoot.position;
        sceneStartRotation = forkliftRoot.rotation;
        homeCorrectionTargetPosition = sceneStartPosition;
        homeCorrectionTargetRotation = sceneStartRotation;

        // Keep the same server-map-to-scene mapping across Play sessions.
        // Legacy scenes enabled alignFirstPoseToSceneStart; interpret that
        // setting as fixed HOME alignment rather than rebasing on a live pose.
        if (alignFirstPoseToSceneStart || mapOriginIsSceneStart)
        {
            automaticRotationOffset = sceneStartRotation;
            automaticPositionOffset = sceneStartPosition;
        }
        poseCalibrated = true;

        CacheRackBlockers();
    }

    private BoxCollider palletContactBox;
    private Renderer[] forkContactRenderers;

    public void RefreshContactBoxGeometry() => UpdatePalletContactBox();

    private void UpdatePalletContactBox()
    {
        if (forkliftRoot == null) return;
        if (palletContactBox == null)
        {
            Transform mount = forkliftRoot.Find("PalletContactMount");
            if (mount == null) return;
            palletContactBox = mount.GetComponent<BoxCollider>();
        }
        if (palletContactBox == null) return;
        if (forkContactRenderers == null)
        {
            Transform visual = forkliftRoot.Find("Visual");
            if (visual == null) return;
            var matches = new List<Renderer>();
            foreach (Renderer renderer in visual.GetComponentsInChildren<Renderer>(true))
                for (Transform part = renderer.transform; part != null && part != visual; part = part.parent)
                    if (part.name.IndexOf("forklift_b", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        matches.Add(renderer);
                        break;
                    }
            forkContactRenderers = matches.ToArray();
        }
        bool hasBounds = false;
        Bounds bounds = new Bounds();
        foreach (Renderer renderer in forkContactRenderers)
        {
            if (renderer == null) continue;
            Bounds mesh = renderer.localBounds;
            for (int i = 0; i < 8; i++)
            {
                Vector3 corner = mesh.center + Vector3.Scale(mesh.extents,
                    new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
                Vector3 local = palletContactBox.transform.InverseTransformPoint(renderer.transform.TransformPoint(corner));
                if (!hasBounds) { bounds = new Bounds(local, Vector3.zero); hasBounds = true; }
                else bounds.Encapsulate(local);
            }
        }
        if (!hasBounds) return;
        // A small contact margin, expressed in world metres even for scaled models.
        Vector3 scale = palletContactBox.transform.lossyScale;
        Vector3 margin = new Vector3(0.01f / Mathf.Max(Mathf.Abs(scale.x), 0.0001f),
            0.01f / Mathf.Max(Mathf.Abs(scale.y), 0.0001f),
            0.01f / Mathf.Max(Mathf.Abs(scale.z), 0.0001f));
        palletContactBox.center = bounds.center;
        palletContactBox.size = bounds.size + margin;
    }

#if UNITY_EDITOR
    public void AlignEditorHome()
    {
        if (Application.isPlaying) return;
        if (forkliftRoot == null) forkliftRoot = transform;
        RecenterChassisPivot();
        UpdatePalletContactBox();
    }
#endif

    private void RecenterChassisPivot()
    {
        // Use the chassis, not the forks/cargo or whole renderer bounds.
        Renderer chassis = null;
        foreach (Renderer candidate in forkliftRoot.GetComponentsInChildren<Renderer>(true))
        {
            if (candidate.name.IndexOf("burger_base", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                chassis = candidate;
                break;
            }
        }
        if (chassis == null)
        {
            Debug.LogWarning($"[{robotId}] Pivot correction skipped: burger_base chassis renderer not found.", this);
            return;
        }

        Vector3 oldPivot = forkliftRoot.position;
        Vector3 newPivot = chassis.bounds.center;
        newPivot.y = oldPivot.y;
        Vector3 delta = newPivot - oldPivot;
        if (delta.sqrMagnitude < 0.00000001f) return;

        // Move the parent pivot while restoring every direct child's world
        // pose. Geometry, colliders and cargo sockets stay in place at startup.
        int count = forkliftRoot.childCount;
        Transform[] children = new Transform[count];
        Vector3[] positions = new Vector3[count];
        Quaternion[] rotations = new Quaternion[count];
        for (int i = 0; i < count; i++)
        {
            children[i] = forkliftRoot.GetChild(i);
            positions[i] = children[i].position;
            rotations[i] = children[i].rotation;
        }
        forkliftRoot.position = newPivot;
        for (int i = 0; i < count; i++)
            children[i].SetPositionAndRotation(positions[i], rotations[i]);

        Debug.Log($"[{robotId}] Chassis pivot corrected: old={oldPivot}, new={newPivot}, offset={delta}", this);
    }

    private void Update()
    {
        if (forkliftRoot != null && workAlignmentOffset != workAlignmentTarget)
        {
            Vector3 next = Vector3.MoveTowards(workAlignmentOffset, workAlignmentTarget,
                0.2f * Time.unscaledDeltaTime);
            Vector3 intended = forkliftRoot.position + next - workAlignmentOffset;
            Vector3 allowed = ClampPositionBeforeRack(forkliftRoot.position, intended);
            Vector3 step = allowed - forkliftRoot.position;
            workAlignmentOffset += step;
            forkliftRoot.position = allowed;
            homeCorrectionActive = false;
        }
        if (!homeCorrectionActive || forkliftRoot == null)
        {
            return;
        }

        float duration = Mathf.Max(homeCorrectionBlendSeconds, 0.01f);
        float blend = 1f - Mathf.Exp(-Time.unscaledDeltaTime * 5f / duration);
        Vector3 nextPosition = Vector3.Lerp(
            forkliftRoot.position,
            homeCorrectionTargetPosition,
            blend);
        Quaternion nextRotation = Quaternion.Slerp(
            forkliftRoot.rotation,
            homeCorrectionTargetRotation,
            blend);

        nextPosition = ClampPositionBeforeRack(
            forkliftRoot.position,
            nextPosition);
        forkliftRoot.SetPositionAndRotation(nextPosition, nextRotation);

        if (Vector3.Distance(
                forkliftRoot.position,
                homeCorrectionTargetPosition) <= 0.002f &&
            Quaternion.Angle(
                forkliftRoot.rotation,
                homeCorrectionTargetRotation) <= 0.5f)
        {
            forkliftRoot.SetPositionAndRotation(
                homeCorrectionTargetPosition,
                homeCorrectionTargetRotation);
            homeCorrectionActive = false;
        }
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            Debug.LogError(
                $"[{robotId}] FactoryStateManager가 연결되지 않았습니다.",
                this);

            return;
        }

        factoryStateManager.MobileRobotPoseReceived +=
            HandleMobileRobotPose;
    }

    private void OnDisable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.MobileRobotPoseReceived -=
            HandleMobileRobotPose;
    }

    private void HandleMobileRobotPose(
        MobileRobotPoseData pose)
    {
        if (pose == null || !IsMatchingRobotId(pose.robot_id))
        {
            return;
        }

        ApplyPose(pose);
    }

    public void ApplyPose(MobileRobotPoseData pose)
    {
        if (pose == null || pose.position == null ||
            pose.orientation == null || forkliftRoot == null)
        {
            Debug.LogWarning(
                $"[{robotId}] 적용할 Pose 데이터가 올바르지 않습니다.",
                this);
            return;
        }

        PositionData rosPosition = pose.position;
        QuaternionData rosRotation = pose.orientation;

        if (!IsFinite(rosPosition.x) ||
            !IsFinite(rosPosition.y) ||
            !IsFinite(rosPosition.z) ||
            !IsFinite(rosRotation.x) ||
            !IsFinite(rosRotation.y) ||
            !IsFinite(rosRotation.z) ||
            !IsFinite(rosRotation.w))
        {
            Debug.LogWarning(
                $"[{robotId}] Pose에 유효하지 않은 숫자가 있습니다.",
                this);
            return;
        }

        // ROS:
        // X forward, Y left, Z up
        //
        // Unity:
        // X right, Y up, Z forward
        Vector3 convertedPosition = new Vector3(
            -rosPosition.y,
             rosPosition.z,
             rosPosition.x
        );

        Quaternion convertedRotation = new Quaternion(
             rosRotation.y,
            -rosRotation.z,
            -rosRotation.x,
             rosRotation.w
        );

        float rotationMagnitude = Mathf.Sqrt(
            convertedRotation.x * convertedRotation.x +
            convertedRotation.y * convertedRotation.y +
            convertedRotation.z * convertedRotation.z +
            convertedRotation.w * convertedRotation.w);

        if (rotationMagnitude < 0.000001f)
        {
            Debug.LogWarning(
                $"[{robotId}] orientation이 유효한 Quaternion이 아닙니다.",
                this);
            return;
        }

        convertedRotation = new Quaternion(
            convertedRotation.x / rotationMagnitude,
            convertedRotation.y / rotationMagnitude,
            convertedRotation.z / rotationMagnitude,
            convertedRotation.w / rotationMagnitude);

        lastPalletPoseTime = Time.unscaledTime;
        PalletMotionPosition = convertedPosition;
        PalletMotionRotation = convertedRotation;
        CalibrateFirstPose(convertedPosition, convertedRotation);
        // Rotation or translation alone cannot prove an odometry reset.
        // Keep the server-to-scene frame fixed during ordinary robot motion.
        // A real odometry reset requires an explicit upstream reset signal.

        Vector3 displayedPosition =
            automaticRotationOffset * convertedPosition +
            automaticPositionOffset +
            positionOffset + workAlignmentOffset;
        Quaternion displayedRotation =
            automaticRotationOffset * convertedRotation;

        Vector3 serverMappedPosition = displayedPosition;
        displayedPosition = ClampPositionBeforeRack(
            forkliftRoot.position,
            displayedPosition);

        if (ShouldSmoothHomeCorrection(displayedPosition))
        {
            homeCorrectionTargetPosition = displayedPosition;
            homeCorrectionTargetRotation = displayedRotation;
            homeCorrectionActive = true;
        }
        else
        {
            homeCorrectionActive = false;
            forkliftRoot.SetPositionAndRotation(
                displayedPosition,
                displayedRotation);
        }

        previousConvertedPosition = convertedPosition;
        previousConvertedRotation = convertedRotation;
        hasPreviousRawPose = true;

        if (logAppliedPose &&
            Time.unscaledTime >= nextAppliedPoseLogTime)
        {
            nextAppliedPoseLogTime = Time.unscaledTime +
                Mathf.Max(appliedPoseLogIntervalSeconds, 0.25f);
            Debug.Log(
                $"[{robotId}] 서버 Pose 즉시 표시: " +
                $"robot={pose.robot_id}, frame={pose.frame_id}, source={pose.source_timestamp}, " +
                $"ros=({rosPosition.x:F3},{rosPosition.y:F3},{rosPosition.z:F3}), " +
                $"rosQ=({rosRotation.x:F4},{rosRotation.y:F4},{rosRotation.z:F4},{rosRotation.w:F4}), " +
                $"mapped={serverMappedPosition}, target={displayedPosition}, actual={forkliftRoot.position}, " +
                $"targetYaw={displayedRotation.eulerAngles.y:F1}, actualYaw={forkliftRoot.eulerAngles.y:F1}, " +
                $"rackClamped={(serverMappedPosition - displayedPosition).sqrMagnitude > 0.000001f}, " +
                $"smoothing={homeCorrectionActive}",
                this);
        }
    }

    private bool ShouldSmoothHomeCorrection(Vector3 targetPosition)
    {
        if (!smoothHomeCorrections || forkliftRoot == null)
        {
            return false;
        }

        bool nearHome =
            PlanarDistance(forkliftRoot.position, sceneStartPosition) <=
                Mathf.Max(homeCorrectionRadius, 0.01f) ||
            PlanarDistance(targetPosition, sceneStartPosition) <=
                Mathf.Max(homeCorrectionRadius, 0.01f);

        return (homeCorrectionActive ||
                PlanarDistance(forkliftRoot.position, targetPosition) >=
                    Mathf.Max(homeCorrectionJumpDistance, 0.001f)) &&
               nearHome;
    }

    private static float PlanarDistance(Vector3 first, Vector3 second)
    {
        return Vector2.Distance(
            new Vector2(first.x, first.z),
            new Vector2(second.x, second.z));
    }

    private void CalibrateFirstPose(
        Vector3 convertedPosition,
        Quaternion convertedRotation)
    {
        if (poseCalibrated)
        {
            return;
        }

        automaticRotationOffset =
            sceneStartRotation * Quaternion.Inverse(convertedRotation);
        automaticPositionOffset =
            sceneStartPosition -
            automaticRotationOffset * convertedPosition;
        poseCalibrated = true;

        Debug.Log(
            $"[{robotId}] 첫 서버 Pose를 Unity 시작 자세에 정렬했습니다. " +
            $"sceneStart={sceneStartPosition}, " +
            $"positionOffset={automaticPositionOffset}",
            this);
    }

    private void CacheRackBlockers()
    {
        var blockers = new List<Collider>();

        foreach (Collider candidate in
                 Resources.FindObjectsOfTypeAll<Collider>())
        {
            if (candidate == null ||
                !candidate.enabled ||
                candidate.isTrigger ||
                candidate.gameObject.scene != gameObject.scene ||
                !IsRackCollider(candidate.transform))
            {
                continue;
            }

            blockers.Add(candidate);
        }

        rackBlockers = blockers.ToArray();
    }

    private Vector3 ClampPositionBeforeRack(
        Vector3 currentPosition,
        Vector3 desiredPosition)
    {
        if (!preventRackPassThrough)
        {
            return desiredPosition;
        }

        if (rackBlockers == null || rackBlockers.Length == 0)
        {
            CacheRackBlockers();
        }

        Vector2 start = new Vector2(
            currentPosition.x,
            currentPosition.z);
        Vector2 end = new Vector2(
            desiredPosition.x,
            desiredPosition.z);
        Vector2 movement = end - start;
        float movementDistance = movement.magnitude;

        if (movementDistance < 0.000001f)
        {
            return desiredPosition;
        }

        float earliestEntry = 1f;
        Collider blockingRack = null;

        foreach (Collider rack in rackBlockers)
        {
            if (rack == null || !rack.enabled)
            {
                continue;
            }

            Bounds bounds = rack.bounds;
            float minX = bounds.min.x - collisionHalfExtentsXZ.x;
            float maxX = bounds.max.x + collisionHalfExtentsXZ.x;
            float minZ = bounds.min.z - collisionHalfExtentsXZ.y;
            float maxZ = bounds.max.z + collisionHalfExtentsXZ.y;

            // 랙에 이미 밀착된 상태에서는 후진하여 빠져나올 수 있어야 합니다.
            bool startsInside = start.x >= minX && start.x <= maxX &&
                                start.y >= minZ && start.y <= maxZ;

            if (startsInside)
            {
                continue;
            }

            if (!TryGetSegmentEntry(
                    start,
                    movement,
                    minX,
                    maxX,
                    minZ,
                    maxZ,
                    out float entry) ||
                entry >= earliestEntry)
            {
                continue;
            }

            earliestEntry = entry;
            blockingRack = rack;
        }

        if (blockingRack == null)
        {
            return desiredPosition;
        }

        float safeEntry = Mathf.Max(
            0f,
            earliestEntry -
            Mathf.Max(collisionSkin, 0f) / movementDistance);
        desiredPosition.x = Mathf.Lerp(
            currentPosition.x,
            desiredPosition.x,
            safeEntry);
        desiredPosition.z = Mathf.Lerp(
            currentPosition.z,
            desiredPosition.z,
            safeEntry);

        if (Time.unscaledTime >= nextCollisionWarningTime)
        {
            nextCollisionWarningTime = Time.unscaledTime + 2f;
            Debug.LogWarning(
                $"[{robotId}] {blockingRack.name} 관통 Pose를 차단했습니다. " +
                $"표시 위치={desiredPosition}",
                this);
        }

        return desiredPosition;
    }

    private static bool TryGetSegmentEntry(
        Vector2 start,
        Vector2 movement,
        float minX,
        float maxX,
        float minZ,
        float maxZ,
        out float entry)
    {
        float enter = 0f;
        float exit = 1f;

        if (!ClipSegmentAxis(
                start.x,
                movement.x,
                minX,
                maxX,
                ref enter,
                ref exit) ||
            !ClipSegmentAxis(
                start.y,
                movement.y,
                minZ,
                maxZ,
                ref enter,
                ref exit))
        {
            entry = 0f;
            return false;
        }

        entry = enter;
        return enter >= 0f && enter <= 1f;
    }

    private static bool ClipSegmentAxis(
        float origin,
        float delta,
        float minimum,
        float maximum,
        ref float enter,
        ref float exit)
    {
        if (Mathf.Abs(delta) < 0.000001f)
        {
            return origin >= minimum && origin <= maximum;
        }

        float first = (minimum - origin) / delta;
        float second = (maximum - origin) / delta;

        if (first > second)
        {
            (first, second) = (second, first);
        }

        enter = Mathf.Max(enter, first);
        exit = Mathf.Min(exit, second);
        return enter <= exit;
    }

    private static bool IsRackCollider(Transform candidate)
    {
        for (Transform current = candidate;
             current != null;
             current = current.parent)
        {
            string normalized = current.name
                .Replace("_", string.Empty)
                .Replace(" ", string.Empty)
                .ToUpperInvariant();

            if (normalized == "RACK1" || normalized == "RACK2")
            {
                return true;
            }
        }

        return false;
    }

    private static bool IsFinite(float value)
    {
        return !float.IsNaN(value) && !float.IsInfinity(value);
    }

    private bool IsMatchingRobotId(string incomingRobotId)
    {
        string normalized = NormalizeRobotId(incomingRobotId);

        return normalized == NormalizeRobotId(robotId) ||
               normalized == "forklift_01" ||
               normalized == "turtlebot_01" ||
               normalized == "mobile_robot_1";
    }

    private static string NormalizeRobotId(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? string.Empty
            : value.Trim().Replace('-', '_').ToLowerInvariant();
    }
}
