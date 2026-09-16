using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

[DisallowMultipleComponent]
public class HouseAssemblyController : MonoBehaviour
{
    [Serializable]
    public class AssemblySlot
    {
        public string operationCode;
        public string payloadId;
        public CarryableObject part;
        public Transform socket;

        [HideInInspector]
        public bool installed;
    }

    [Serializable]
    public class ProductAssemblyLayout
    {
        public string productCode;
        public CarryableObject basePart;
        public string roofPayloadId;
        public Transform roofSocket;
        public AssemblySlot[] wallSlots = Array.Empty<AssemblySlot>();
    }

    [SerializeField]
    private ProductAssemblyLayout[] productLayouts =
        Array.Empty<ProductAssemblyLayout>();

    [SerializeField]
    private string activeProductCode = "HOUSE_B";

    [SerializeField]
    private CarryableObject assemblyRoot;

    [SerializeField]
    private Transform assemblyParent;

    [SerializeField]
    private Transform baseDropPoint;

    [Header("Assembly Support")]
    [SerializeField]
    private Transform assemblySupport;

    [Tooltip("fork 아래에 배치한 비활성 Base 기준 모델입니다.")]
    [SerializeField]
    private Transform assemblyBaseReference;

    [SerializeField]
    [Min(0f)]
    private float assemblySupportClearance = 0.001f;

    // 이전 씬과 Inspector 시험 기능의 호환용입니다.
    [SerializeField]
    private AssemblySlot[] slots = Array.Empty<AssemblySlot>();

    [SerializeField]
    private bool basePlaced;

    [SerializeField]
    private bool roofInstalled;

    private readonly List<CarryableObject> contactInstalledParts =
        new List<CarryableObject>();

    private ProductAssemblyLayout activeLayout;

    public CarryableObject AssemblyRoot
    {
        get
        {
            EnsureActiveLayout();
            return assemblyRoot;
        }
    }

    [SerializeField]
    private bool startWithBasePlaced = false;

    public bool StartWithBasePlaced => startWithBasePlaced;
    public bool BasePlaced => basePlaced;
    public string ActiveProductCode => activeProductCode;
    public string ActiveRoofPayloadId =>
        activeLayout != null ? activeLayout.roofPayloadId : null;

    public string[] ActiveWallPayloadIds =>
        GetActiveWallSlots()
            .Where(slot => slot != null &&
                           !string.IsNullOrWhiteSpace(slot.payloadId))
            .Select(slot => slot.payloadId)
            .ToArray();

    public bool TryGetNextPendingWall(
        out string payloadId,
        out Transform socket)
    {
        AssemblySlot pending = GetActiveWallSlots().FirstOrDefault(slot =>
            slot != null && !slot.installed && slot.socket != null &&
            !string.IsNullOrWhiteSpace(slot.payloadId));

        payloadId = pending?.payloadId;
        socket = pending?.socket;
        return pending != null;
    }

    public bool TryGetWallSocket(
        string payloadId,
        out Transform socket)
    {
        AssemblySlot slot = FindActiveWallSlot(payloadId);
        socket = slot?.socket;
        return slot != null && !slot.installed && socket != null;
    }

    private void Awake()
    {
        AutoConfigureFromSceneIfNeeded();
        ApplyProductionWallOrder();
        EnsureActiveLayout();
        ConfigureAssemblySupportFromScene();
    }

    private void Start()
    {
        PrepareBaseAtDestination();
    }

    private void LateUpdate()
    {
        // Also restore the filming start state after a new job or product reset.
        if (startWithBasePlaced && !basePlaced)
            PrepareBaseAtDestination();
    }

    public bool PrepareBaseAtDestination(bool forceForProcess = false)
    {
        if (!startWithBasePlaced && !forceForProcess) return false;
        if (basePlaced) return true;
        EnsureActiveLayout();
        ConfigureAssemblySupportFromScene();
        UpdateBaseDropPointFromAssemblySupport();
        if (assemblyRoot == null || baseDropPoint == null) return false;

        // Use the same destination as ZK unloading, with fixed assembly physics.
        if (!assemblyRoot.InstallAt(baseDropPoint)) return false;
        assemblyRoot.transform.SetParent(assemblyParent, true);
        assemblyRoot.Configure(CarryableType.Base, assemblyRoot.PayloadId);
        basePlaced = true;
        Debug.Log($"[HouseAssembly] {activeProductCode} base preplaced at {assemblyRoot.transform.position}", this);
        return true;
    }

    public void Configure(CarryableObject basePart)
    {
        assemblyRoot = basePart;
        assemblyParent = basePart != null
            ? basePart.transform.parent
            : null;
    }

    public void Configure(
        ProductAssemblyLayout[] layouts,
        Transform placementPoint,
        string defaultProductCode = "HOUSE_B")
    {
        productLayouts = layouts ?? Array.Empty<ProductAssemblyLayout>();
        ApplyProductionWallOrder();
        baseDropPoint = placementPoint;
        activeProductCode = NormalizeProductCode(defaultProductCode) ??
                            "HOUSE_B";
        SelectProduct(activeProductCode, false);
    }

    public void ConfigureAssemblySupport(
        Transform support,
        Transform baseReference = null)
    {
        assemblySupport = support;
        assemblyBaseReference = baseReference;
    }

    public bool HasProductLayout(string productCode)
    {
        string normalized = NormalizeProductCode(productCode);
        return productLayouts != null && productLayouts.Any(layout =>
            layout != null &&
            NormalizeProductCode(layout.productCode) == normalized);
    }

    public bool SelectProduct(
        string productCode,
        bool resetWhenChanged = true)
    {
        string normalized = NormalizeProductCode(productCode);

        if (normalized == null || productLayouts == null)
        {
            return false;
        }

        ProductAssemblyLayout selected = productLayouts.FirstOrDefault(
            layout => layout != null &&
                      NormalizeProductCode(layout.productCode) == normalized);

        if (selected == null || selected.basePart == null)
        {
            Debug.LogWarning(
                $"[HouseAssembly] {normalized} 조립 기준이 없습니다.",
                this);
            return false;
        }

        bool changed = activeLayout != selected ||
                       assemblyRoot != selected.basePart;

        if (changed && resetWhenChanged && Application.isPlaying)
        {
            ResetAssembly();
        }

        if (changed && startWithBasePlaced && basePlaced && assemblyRoot != null)
            assemblyRoot.ResetToInitialState();
        if (changed) basePlaced = false;
        activeLayout = selected;
        activeProductCode = normalized;
        assemblyRoot = selected.basePart;
        assemblyParent = selected.basePart.transform.parent;

        if (changed)
        {
            Debug.Log(
                $"[HouseAssembly] 조립 모델 선택: {activeProductCode}",
                this);
        }
        return true;
    }

    public bool PlaceBaseFrom(RobotCargoMount carrier)
    {
        EnsureActiveLayout();

        if (carrier == null ||
            assemblyRoot == null ||
            carrier.CurrentCargo != assemblyRoot)
        {
            return false;
        }

        UpdateBaseDropPointFromAssemblySupport();

        CarryableObject released = baseDropPoint != null
            ? carrier.ReleaseAt(baseDropPoint, assemblyParent)
            : carrier.ReleaseAtCurrentPose(assemblyParent);

        if (released == null)
        {
            return false;
        }

        basePlaced = true;
        assemblyRoot.Configure(
            CarryableType.Base,
            assemblyRoot.PayloadId);

        Debug.Log(
            $"[HouseAssembly] {activeProductCode} 베이스 배치 완료",
            this);
        return true;
    }

    public bool TryInstallFromContact(CarryableObject part)
    {
        EnsureActiveLayout();

        if (!basePlaced ||
            assemblyRoot == null ||
            part == null ||
            part == assemblyRoot ||
            part.State != CarryableState.AttachedToRobot ||
            (part.PayloadType != CarryableType.Wall &&
             part.PayloadType != CarryableType.Roof))
        {
            return false;
        }

        RobotCargoMount carrier =
            part.GetComponentInParent<RobotCargoMount>();

        if (carrier == null || carrier.CurrentCargo != part)
        {
            return false;
        }

        AssemblySlot slot = part.PayloadType == CarryableType.Wall
            ? FindActiveWallSlot(part.PayloadId)
            : FindActiveRoofSlot(part.PayloadId);

        if (slot == null || slot.socket == null)
        {
            Debug.LogWarning(
                $"[HouseAssembly] {activeProductCode}에 없는 자재: " +
                part.PayloadId,
                part);
            return false;
        }

        if (part.PayloadType == CarryableType.Wall &&
            (!TryGetNextPendingWall(out string nextWall, out Transform _) ||
             !string.Equals(nextWall, part.PayloadId, StringComparison.OrdinalIgnoreCase))) return false;

        if (carrier.ReleaseAtInstallationSocket(part, slot.socket) == null)
        {
            return false;
        }

        slot.installed = true;

        if (!contactInstalledParts.Contains(part))
        {
            contactInstalledParts.Add(part);
        }

        if (part.PayloadType == CarryableType.Roof)
        {
            roofInstalled = true;
        }

        Debug.Log(
            $"[HouseAssembly] {activeProductCode} 자재 설치 완료: " +
            part.PayloadId,
            part);

        EvaluateCompletion();
        return true;
    }

    public bool InstallForProcessStart(CarryableObject part)
    {
        EnsureActiveLayout();
        if (!basePlaced || part == null) return false;
        AssemblySlot slot = part.PayloadType == CarryableType.Wall
            ? FindActiveWallSlot(part.PayloadId)
            : part.PayloadType == CarryableType.Roof ? FindActiveRoofSlot(part.PayloadId) : null;
        if (slot == null || slot.socket == null || !part.InstallAt(slot.socket)) return false;
        slot.installed = true;
        if (!contactInstalledParts.Contains(part)) contactInstalledParts.Add(part);
        if (part.PayloadType == CarryableType.Roof) roofInstalled = true;
        EvaluateCompletion();
        return true;
    }

    public bool InstallByOperation(string operationCode)
    {
        if (string.IsNullOrWhiteSpace(operationCode))
        {
            return false;
        }

        foreach (AssemblySlot slot in GetActiveWallSlots()
                     .Concat(slots ?? Array.Empty<AssemblySlot>()))
        {
            if (slot == null ||
                !string.Equals(
                    slot.operationCode,
                    operationCode,
                    StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            return Install(slot);
        }

        Debug.LogWarning(
            $"[HouseAssembly] 등록되지 않은 공정: {operationCode}",
            this);
        return false;
    }

    public bool InstallAtIndex(int index)
    {
        AssemblySlot[] activeSlots = GetActiveWallSlots();
        return index >= 0 && index < activeSlots.Length &&
               Install(activeSlots[index]);
    }

    public bool IsAssemblyComplete()
    {
        EnsureActiveLayout();
        return basePlaced && roofInstalled && AreAllWallsInstalled();
    }

    public void ResetAssembly()
    {
        HashSet<CarryableObject> resetParts =
            new HashSet<CarryableObject>();

        foreach (CarryableObject part in contactInstalledParts)
        {
            if (part != null && resetParts.Add(part))
            {
                part.ResetToInitialState();
            }
        }

        contactInstalledParts.Clear();
        basePlaced = false;
        roofInstalled = false;

        if (productLayouts != null)
        {
            foreach (ProductAssemblyLayout layout in productLayouts)
            {
                if (layout == null)
                {
                    continue;
                }

                foreach (AssemblySlot slot in layout.wallSlots ??
                         Array.Empty<AssemblySlot>())
                {
                    if (slot != null)
                    {
                        slot.installed = false;
                    }
                }

                if (layout.basePart != null &&
                    resetParts.Add(layout.basePart))
                {
                    layout.basePart.Configure(
                        CarryableType.Base,
                        layout.basePart.PayloadId);
                    layout.basePart.ResetToInitialState();
                }
            }
        }

        foreach (AssemblySlot slot in slots ?? Array.Empty<AssemblySlot>())
        {
            if (slot == null)
            {
                continue;
            }

            if (slot.part != null && resetParts.Add(slot.part))
            {
                slot.part.ResetToInitialState();
            }

            slot.installed = false;
        }

        EnsureActiveLayout();
    }

    private bool Install(AssemblySlot slot)
    {
        if (slot == null || slot.part == null || slot.socket == null)
        {
            Debug.LogError(
                "[HouseAssembly] Part 또는 Socket 연결이 없습니다.",
                this);
            return false;
        }

        if (!slot.part.InstallAt(slot.socket))
        {
            return false;
        }

        slot.installed = true;
        EvaluateCompletion();
        return true;
    }

    private AssemblySlot FindActiveWallSlot(string payloadId)
    {
        return GetActiveWallSlots().FirstOrDefault(slot =>
            slot != null &&
            string.Equals(
                slot.payloadId,
                payloadId,
                StringComparison.OrdinalIgnoreCase));
    }

    private AssemblySlot FindActiveRoofSlot(string payloadId)
    {
        if (activeLayout == null ||
            activeLayout.roofSocket == null ||
            !string.Equals(
                activeLayout.roofPayloadId,
                payloadId,
                StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        return new AssemblySlot
        {
            payloadId = activeLayout.roofPayloadId,
            socket = activeLayout.roofSocket
        };
    }

    private AssemblySlot[] GetActiveWallSlots()
    {
        EnsureActiveLayout();
        return activeLayout?.wallSlots ?? Array.Empty<AssemblySlot>();
    }

    private bool AreAllWallsInstalled()
    {
        AssemblySlot[] wallSlots = GetActiveWallSlots();

        if (wallSlots.Length == 0)
        {
            return false;
        }

        return wallSlots.All(slot =>
            slot != null && slot.socket != null && slot.installed);
    }

    private void EvaluateCompletion()
    {
        if (!IsAssemblyComplete() || assemblyRoot == null)
        {
            return;
        }

        assemblyRoot.Configure(
            CarryableType.CompletedHouse,
            assemblyRoot.PayloadId);

        Debug.Log(
            $"[HouseAssembly] {activeProductCode}: 지붕까지 설치되어 " +
            "완성 주택 운반이 가능합니다.",
            this);
    }

    private void EnsureActiveLayout()
    {
        if (activeLayout != null && assemblyRoot != null)
        {
            return;
        }

        if (!SelectProduct(activeProductCode, false) &&
            productLayouts != null)
        {
            ProductAssemblyLayout fallback = productLayouts.FirstOrDefault(
                layout => layout != null && layout.basePart != null);

            if (fallback != null)
            {
                SelectProduct(fallback.productCode, false);
            }
        }
    }

    private void AutoConfigureFromSceneIfNeeded()
    {
        if (HasConfiguredRuntimeLayout("HOUSE_A") &&
            HasConfiguredRuntimeLayout("HOUSE_B"))
        {
            return;
        }

        Transform material = FindSceneTransformExact("material");
        Transform houseAPreRoof = FindSceneTransformExact(
            "House_A_PRE_ROOF");
        Transform houseAComplete = FindSceneTransformExact(
            "House_A_COMPLETE");
        Transform houseBPreRoof = FindSceneTransformExact(
            "House_B_PRE_ROOF");
        Transform houseBComplete = FindSceneTransformExact(
            "House_B_COMPLETE");

        if (material == null || houseAPreRoof == null ||
            houseAComplete == null || houseBPreRoof == null ||
            houseBComplete == null)
        {
            Debug.LogWarning(
                "[HouseAssembly] A/B 조립 기준 모델을 찾지 못했습니다.",
                this);
            return;
        }

        CarryableObject baseA = ConfigureRuntimeCarryable(
            FindDescendantExact(material, "A_base"),
            CarryableType.Base,
            "A_base");
        CarryableObject baseB = ConfigureRuntimeCarryable(
            FindDescendantExact(material, "B_base"),
            CarryableType.Base,
            "B_base");

        string[] wallIds =
        {
            "leftwall",
            "rightwall",
            "backwall",
            "doorwall",
            "A_indoorwall1",
            "A_indoorwall2",
            "B_indoorwall"
        };

        foreach (string wallId in wallIds)
        {
            ConfigureRuntimeCarryable(
                FindDescendantExact(material, wallId),
                CarryableType.Wall,
                wallId);
        }

        Transform bCompleteBase = FindDescendantNormalized(
            houseBComplete,
            "B_base");
        baseDropPoint = EnsurePoseMarker(
            transform,
            "AssemblyBaseDropPoint",
            bCompleteBase);

        ProductAssemblyLayout layoutA = BuildRuntimeLayout(
            baseA,
            houseAPreRoof,
            houseAComplete,
            "HOUSE_A",
            "A_base",
            "roof",
            new[]
            {
                "doorwall",
                "rightwall",
                "backwall",
                "leftwall",
                "A_indoorwall2",
                "A_indoorwall1"
            });

        ProductAssemblyLayout layoutB = BuildRuntimeLayout(
            baseB,
            houseBPreRoof,
            houseBComplete,
            "HOUSE_B",
            "B_base",
            "roof2",
            new[]
            {
                "doorwall",
                "rightwall",
                "backwall",
                "leftwall",
                "B_indoorwall"
            });

        productLayouts = new[] { layoutA, layoutB };

        foreach (CarryableObject basePart in new[] { baseA, baseB })
        {
            if (basePart == null)
            {
                continue;
            }

            AssemblyContactReceiver receiver =
                basePart.GetComponent<AssemblyContactReceiver>();

            if (receiver == null)
            {
                receiver = basePart.gameObject.AddComponent<
                    AssemblyContactReceiver>();
            }

            receiver.Configure(this);
        }

        houseAPreRoof.gameObject.SetActive(false);
        houseAComplete.gameObject.SetActive(false);
        houseBPreRoof.gameObject.SetActive(false);
        houseBComplete.gameObject.SetActive(false);

        activeLayout = null;
        SelectProduct(activeProductCode, false);
        Debug.Log(
            "[HouseAssembly] 씬 기준 모델에서 A/B 조립 좌표를 구성했습니다.",
            this);
    }

    private bool HasConfiguredRuntimeLayout(string productCode)
    {
        string normalized = NormalizeProductCode(productCode);

        return productLayouts != null && productLayouts.Any(layout =>
            layout != null &&
            NormalizeProductCode(layout.productCode) == normalized &&
            layout.basePart != null &&
            layout.wallSlots != null &&
            layout.wallSlots.Length > 0);
    }

    private void ConfigureAssemblySupportFromScene()
    {
        if (assemblySupport == null)
        {
            Transform material = FindSceneTransformExact("material");
            assemblySupport = FindDescendantExact(material, "fork");
        }

        if (assemblySupport == null)
        {
            Debug.LogWarning(
                "[HouseAssembly] material/fork 조립 받침을 찾지 못했습니다.",
                this);
            return;
        }

        if (assemblyBaseReference == null ||
            !assemblyBaseReference.IsChildOf(assemblySupport))
        {
            assemblyBaseReference = FindDescendantNormalized(
                assemblySupport,
                "B_base");
        }

        if (assemblyBaseReference != null)
        {
            // 위치 기준용 복제 Base는 Game 화면에 표시하거나
            // 실제 운반 자재로 인식하지 않습니다.
            assemblyBaseReference.gameObject.SetActive(false);
        }

        BasePlacementZone[] zones = FindObjectsByType<BasePlacementZone>(
            FindObjectsInactive.Include);

        foreach (BasePlacementZone zone in zones)
        {
            if (zone != null && zone.transform != assemblySupport)
            {
                zone.enabled = false;
            }
        }

        BasePlacementZone supportZone =
            assemblySupport.GetComponent<BasePlacementZone>();

        if (supportZone == null)
        {
            supportZone = assemblySupport.gameObject.AddComponent<
                BasePlacementZone>();
        }

        supportZone.Configure(this);
        supportZone.enabled = true;
        UpdateBaseDropPointFromAssemblySupport();
    }

    private void UpdateBaseDropPointFromAssemblySupport()
    {
        if (assemblySupport == null || assemblyRoot == null)
        {
            return;
        }

        if (baseDropPoint == null)
        {
            baseDropPoint = EnsureChild(transform, "AssemblyBaseDropPoint");
            baseDropPoint.rotation = assemblyRoot.transform.rotation;
        }


        if (assemblyBaseReference != null)
        {
            baseDropPoint.SetPositionAndRotation(
                assemblyBaseReference.position,
                assemblyBaseReference.rotation);
            return;
        }

        if (!TryGetSupportBounds(assemblySupport, out Bounds supportBounds) ||
            !TryGetRootLocalRenderBounds(
                assemblyRoot.transform,
                out Bounds baseLocalBounds))
        {
            baseDropPoint.position = assemblySupport.position;
            return;
        }

        Quaternion targetRotation = baseDropPoint.rotation;
        Vector3 rootScale = assemblyRoot.transform.lossyScale;
        Bounds rotatedOffsets = RotateLocalBounds(
            baseLocalBounds,
            targetRotation,
            rootScale);

        Vector3 targetPosition = new Vector3(
            supportBounds.center.x - rotatedOffsets.center.x,
            supportBounds.max.y + assemblySupportClearance -
            rotatedOffsets.min.y,
            supportBounds.center.z - rotatedOffsets.center.z);

        baseDropPoint.SetPositionAndRotation(
            targetPosition,
            targetRotation);
    }

    private static bool TryGetSupportBounds(
        Transform support,
        out Bounds bounds)
    {
        Collider directCollider = support.GetComponent<Collider>();

        if (directCollider != null && directCollider.enabled)
        {
            bounds = directCollider.bounds;
            return true;
        }

        Renderer[] renderers = support.GetComponentsInChildren<Renderer>(true);
        bool found = false;
        bounds = default;

        foreach (Renderer renderer in renderers)
        {
            if (renderer == null)
            {
                continue;
            }

            if (!found)
            {
                bounds = renderer.bounds;
                found = true;
            }
            else
            {
                bounds.Encapsulate(renderer.bounds);
            }
        }

        return found;
    }

    private static bool TryGetRootLocalRenderBounds(
        Transform root,
        out Bounds bounds)
    {
        bool found = false;
        bounds = default;

        foreach (MeshFilter filter in root.GetComponentsInChildren<MeshFilter>(true))
        {
            if (filter == null || filter.sharedMesh == null)
            {
                continue;
            }

            EncapsulateLocalBounds(
                root,
                filter.transform,
                filter.sharedMesh.bounds,
                ref bounds,
                ref found);
        }

        foreach (SkinnedMeshRenderer renderer in
                 root.GetComponentsInChildren<SkinnedMeshRenderer>(true))
        {
            if (renderer == null)
            {
                continue;
            }

            EncapsulateLocalBounds(
                root,
                renderer.transform,
                renderer.localBounds,
                ref bounds,
                ref found);
        }

        return found;
    }

    private static void EncapsulateLocalBounds(
        Transform root,
        Transform source,
        Bounds sourceBounds,
        ref Bounds result,
        ref bool found)
    {
        Vector3 min = sourceBounds.min;
        Vector3 max = sourceBounds.max;

        for (int x = 0; x < 2; x++)
        {
            for (int y = 0; y < 2; y++)
            {
                for (int z = 0; z < 2; z++)
                {
                    Vector3 corner = new Vector3(
                        x == 0 ? min.x : max.x,
                        y == 0 ? min.y : max.y,
                        z == 0 ? min.z : max.z);
                    Vector3 localPoint = root.InverseTransformPoint(
                        source.TransformPoint(corner));

                    if (!found)
                    {
                        result = new Bounds(localPoint, Vector3.zero);
                        found = true;
                    }
                    else
                    {
                        result.Encapsulate(localPoint);
                    }
                }
            }
        }
    }

    private static Bounds RotateLocalBounds(
        Bounds localBounds,
        Quaternion rotation,
        Vector3 scale)
    {
        Vector3 min = localBounds.min;
        Vector3 max = localBounds.max;
        Bounds result = default;
        bool found = false;

        for (int x = 0; x < 2; x++)
        {
            for (int y = 0; y < 2; y++)
            {
                for (int z = 0; z < 2; z++)
                {
                    Vector3 corner = new Vector3(
                        x == 0 ? min.x : max.x,
                        y == 0 ? min.y : max.y,
                        z == 0 ? min.z : max.z);
                    Vector3 offset = rotation * Vector3.Scale(corner, scale);

                    if (!found)
                    {
                        result = new Bounds(offset, Vector3.zero);
                        found = true;
                    }
                    else
                    {
                        result.Encapsulate(offset);
                    }
                }
            }
        }

        return result;
    }

    private void ApplyProductionWallOrder()
    {
        if (productLayouts == null)
        {
            return;
        }

        foreach (ProductAssemblyLayout layout in productLayouts)
        {
            if (layout?.wallSlots == null || layout.wallSlots.Length == 0)
            {
                continue;
            }

            string[] productionOrder = GetProductionWallOrder(
                layout.productCode);

            if (productionOrder == null)
            {
                continue;
            }

            var orderedSlots = new List<AssemblySlot>();

            foreach (string payloadId in productionOrder)
            {
                AssemblySlot slot = layout.wallSlots.FirstOrDefault(item =>
                    item != null &&
                    string.Equals(
                        item.payloadId,
                        payloadId,
                        StringComparison.OrdinalIgnoreCase));

                if (slot != null && !orderedSlots.Contains(slot))
                {
                    orderedSlots.Add(slot);
                }
            }

            // 이후 품목이 추가되어도 누락시키지 않고 정의된 순서 뒤에 보존합니다.
            orderedSlots.AddRange(layout.wallSlots.Where(item =>
                item != null && !orderedSlots.Contains(item)));
            layout.wallSlots = orderedSlots.ToArray();
        }
    }

    private static string[] GetProductionWallOrder(string productCode)
    {
        switch (NormalizeProductCode(productCode))
        {
            case "HOUSE_A":
                return new[]
                {
                    "doorwall",
                    "rightwall",
                    "backwall",
                    "leftwall",
                    "A_indoorwall2",
                    "A_indoorwall1"
                };

            case "HOUSE_B":
                return new[]
                {
                    "doorwall",
                    "rightwall",
                    "backwall",
                    "leftwall",
                    "B_indoorwall"
                };

            default:
                return null;
        }
    }

    private ProductAssemblyLayout BuildRuntimeLayout(
        CarryableObject basePart,
        Transform preRoofReference,
        Transform completeReference,
        string productCode,
        string referenceBaseName,
        string roofPayloadId,
        string[] wallPayloadIds)
    {
        ProductAssemblyLayout layout = new ProductAssemblyLayout
        {
            productCode = productCode,
            basePart = basePart,
            roofPayloadId = roofPayloadId
        };

        Transform preRoofBase = FindDescendantNormalized(
            preRoofReference,
            referenceBaseName);
        Transform completeBase = FindDescendantNormalized(
            completeReference,
            referenceBaseName);

        if (basePart == null || preRoofBase == null ||
            completeBase == null)
        {
            return layout;
        }

        Transform socketRoot = EnsureChild(
            basePart.transform,
            "AssemblySockets");
        Transform productSocketRoot = EnsureChild(
            socketRoot,
            productCode);

        layout.wallSlots = wallPayloadIds.Select((payloadId, index) =>
        {
            Transform source = FindDescendantNormalized(
                preRoofBase,
                payloadId);
            Transform socket = CreateRelativeSocket(
                productSocketRoot,
                preRoofBase,
                source,
                payloadId + "_Socket");

            return new AssemblySlot
            {
                operationCode = productCode + "_WALL_" + (index + 1),
                payloadId = payloadId,
                socket = socket
            };
        }).ToArray();

        Transform roofSource = FindDescendantNormalized(
            completeReference,
            roofPayloadId);
        layout.roofSocket = CreateRelativeSocket(
            productSocketRoot,
            completeBase,
            roofSource,
            roofPayloadId + "_Socket");
        return layout;
    }

    private static CarryableObject ConfigureRuntimeCarryable(
        Transform target,
        CarryableType type,
        string payloadId)
    {
        if (target == null)
        {
            return null;
        }

        CarryableObject carryable = target.GetComponent<CarryableObject>();

        if (carryable == null)
        {
            carryable = target.gameObject.AddComponent<CarryableObject>();
        }

        carryable.Configure(type, payloadId);
        return carryable;
    }

    private static Transform CreateRelativeSocket(
        Transform parent,
        Transform referenceBase,
        Transform referencePart,
        string socketName)
    {
        if (parent == null || referenceBase == null ||
            referencePart == null)
        {
            return null;
        }

        Transform socket = EnsureChild(parent, socketName);
        socket.localPosition = referenceBase.InverseTransformPoint(
            referencePart.position);
        socket.localRotation = Quaternion.Inverse(referenceBase.rotation) *
                               referencePart.rotation;
        socket.localScale = Vector3.one;
        return socket;
    }

    private static Transform EnsurePoseMarker(
        Transform parent,
        string markerName,
        Transform source)
    {
        if (parent == null || source == null)
        {
            return null;
        }

        Transform marker = EnsureChild(parent, markerName);
        marker.SetPositionAndRotation(source.position, source.rotation);
        marker.localScale = Vector3.one;
        return marker;
    }

    private static Transform EnsureChild(
        Transform parent,
        string childName)
    {
        Transform existing = parent.Find(childName);

        if (existing != null)
        {
            return existing;
        }

        GameObject child = new GameObject(childName);
        child.transform.SetParent(parent, false);
        return child.transform;
    }

    private Transform FindSceneTransformExact(string objectName)
    {
        return Resources.FindObjectsOfTypeAll<Transform>()
            .FirstOrDefault(item =>
                item.gameObject.scene == gameObject.scene &&
                item.name == objectName);
    }

    private static Transform FindDescendantExact(
        Transform root,
        string objectName)
    {
        if (root == null)
        {
            return null;
        }

        if (root.name == objectName)
        {
            return root;
        }

        foreach (Transform child in root)
        {
            Transform found = FindDescendantExact(child, objectName);

            if (found != null)
            {
                return found;
            }
        }

        return null;
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

    private static string NormalizeProductCode(string value)
    {
        string normalized = (value ?? string.Empty)
            .Trim()
            .Replace("-", string.Empty)
            .Replace("_", string.Empty)
            .ToUpperInvariant();

        switch (normalized)
        {
            case "HOUSEA":
            case "PRODUCTA":
            case "A":
                return "HOUSE_A";
            case "HOUSEB":
            case "PRODUCTB":
            case "B":
                return "HOUSE_B";
            default:
                return null;
        }
    }
}
