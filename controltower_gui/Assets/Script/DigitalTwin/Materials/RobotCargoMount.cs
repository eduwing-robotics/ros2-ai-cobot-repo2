using System.Collections.Generic;
using UnityEngine;

public enum RobotCarrierRole
{
    Forklift,
    FR5,
    ZK,
    ForkTool
}

[DisallowMultipleComponent]
public class RobotCargoMount : MonoBehaviour
{
    [Header("Carrier")]
    [SerializeField]
    private RobotCarrierRole carrierRole;

    [SerializeField]
    private CarryableType acceptedCargoTypes =
        CarryableType.Pallet;

    [SerializeField]
    private bool autoPickupOnContact = true;

    [SerializeField]
    private bool preserveWorldPoseOnPickup = true;

    [Tooltip("시작할 때 이미 겹쳐 있는 자재는 한 번 떨어질 때까지 집지 않습니다.")]
    [SerializeField]
    private bool ignoreInitialOverlap = true;

    [SerializeField]
    private float initialContactGuardSeconds = 0.25f;

    [SerializeField]
    private Transform mountPoint;

    [SerializeField]
    private CarryableObject currentCargo;

    private CarryableObject nearbyCandidate;
    private readonly HashSet<Collider> initialContacts =
        new HashSet<Collider>();

    private readonly Dictionary<Collider, CarryableObject> contacts =
        new Dictionary<Collider, CarryableObject>();

    private readonly HashSet<CarryableObject> blockedUntilSeparation =
        new HashSet<CarryableObject>();

    private bool autoPickupArmed;
    private bool initialScanComplete;
    private float enabledAt;

    [Header("ZK Base Pickup")]
    [Tooltip("Wait for the suction mount to rise from its lowest contact point before attaching the base.")]
    [SerializeField] private float zkPickupRiseThreshold = 0.002f;
    private CarryableObject zkBottomCandidate;
    private float zkLowestContactY;
    private bool toolFloorReleaseArmed;
    private float previousToolBottom;

    private void LateUpdate()
    {
        GeometryContactBounds.FitFr5(this);
        if (currentCargo == null || currentCargo.PayloadType != CarryableType.Tool) return;
        bool found = false;
        Bounds bounds = default;
        foreach (Renderer r in currentCargo.GetComponentsInChildren<Renderer>())
        {
            if (r.GetComponentInParent<CarryableObject>() != currentCargo) continue;
            if (!found) { bounds = r.bounds; found = true; }
            else bounds.Encapsulate(r.bounds);
        }
        if (!found) return;
        float floorY = currentCargo.InitialWorldBounds.min.y;
        float bottom = bounds.min.y;
        if (bottom > floorY + 0.01f) toolFloorReleaseArmed = true;
        bool descending = bottom < previousToolBottom;
        previousToolBottom = bottom;
        if (!toolFloorReleaseArmed || !descending || bottom > floorY + 0.003f) return;
        CarryableObject tool = currentCargo;
        tool.transform.position += Vector3.up * (floorY - bottom);
        ReleaseAtCurrentPose(tool.InitialParent);
        toolFloorReleaseArmed = false;
        zkBottomCandidate = null;
        Debug.Log($"[CargoMount] tool released at support height {floorY:F4}", this);
    }

    public CarryableObject CurrentCargo => currentCargo;
    public CarryableObject NearbyCandidate => nearbyCandidate;
    public bool IsTouchingForPickup(CarryableObject cargo)
    {
        if (cargo == null || !Accepts(cargo) || blockedUntilSeparation.Contains(cargo)) return false;
        foreach (var contact in contacts)
        {
            if (contact.Value == cargo && contact.Key != null &&
                contact.Key.enabled && contact.Key.gameObject.activeInHierarchy)
                return true;
        }
        return false;
    }

    public Vector3 AttachmentPosition => mountPoint != null ? mountPoint.position : transform.position;

    public void SetVisualAttachmentPoint(Transform visual)
    {
        if (visual == null || mountPoint == visual) return;
        mountPoint = visual;
        preserveWorldPoseOnPickup = true;
        if (currentCargo != null) currentCargo.transform.SetParent(visual, true);
    }

    public bool HasCargo => currentCargo != null;
    public RobotCarrierRole CarrierRole => carrierRole;

    private void Reset()
    {
        mountPoint = transform;
    }

    private void OnEnable()
    {
        GeometryContactBounds.FitFr5(this);
        zkBottomCandidate = null;
        initialContacts.Clear();
        contacts.Clear();
        blockedUntilSeparation.Clear();
        enabledAt = Time.time;
        initialScanComplete = !ignoreInitialOverlap;
        autoPickupArmed = !ignoreInitialOverlap;
    }

    private void FixedUpdate()
    {
        if (initialScanComplete)
        {
            return;
        }

        initialContacts.RemoveWhere(item => item == null);

        if (Time.time - enabledAt < initialContactGuardSeconds)
        {
            return;
        }

        initialScanComplete = true;
        autoPickupArmed = initialContacts.Count == 0;
    }

    public bool RestoreCargoForProcess(CarryableObject cargo)
    {
        if (cargo == null || !Accepts(cargo)) return false;
        if (currentCargo == cargo) return true;
        if (currentCargo != null) return false;
        if (!cargo.AttachTo(mountPoint != null ? mountPoint : transform, false)) return false;
        currentCargo = cargo;
        toolFloorReleaseArmed = false;
        nearbyCandidate = null;
        return true;
    }

    public System.Func<CarryableObject, bool> PickupFilter { private get; set; }

    public bool TryPickup(CarryableObject cargo)
    {
        if (cargo == null ||
            (PickupFilter != null && !PickupFilter(cargo)) ||
            currentCargo != null ||
            blockedUntilSeparation.Contains(cargo) ||
            !Accepts(cargo))
        {
            return false;
        }

        if (cargo.PayloadType == CarryableType.Tool &&
            (!IsTouchingForPickup(cargo) || !HasZkReachedPickupBottom(cargo))) return false;

        Transform targetMount =
            mountPoint != null ? mountPoint : transform;

        if (!cargo.AttachTo(
                targetMount,
                cargo.PayloadType == CarryableType.Tool || preserveWorldPoseOnPickup))
        {
            return false;
        }

        currentCargo = cargo;
        toolFloorReleaseArmed = false;
        previousToolBottom = cargo.InitialWorldBounds.min.y;
        nearbyCandidate = null;
        return true;
    }

    public void Configure(
        RobotCarrierRole role,
        CarryableType acceptedTypes,
        Transform targetMount = null,
        bool pickupOnContact = true,
        bool preserveWorldPose = true)
    {
        carrierRole = role;
        acceptedCargoTypes = acceptedTypes;
        mountPoint = targetMount != null ? targetMount : transform;
        autoPickupOnContact = pickupOnContact;
        preserveWorldPoseOnPickup = preserveWorldPose;
    }

    public bool Accepts(CarryableObject cargo)
    {
        return cargo != null &&
               cargo.PayloadType != CarryableType.None &&
               (acceptedCargoTypes & cargo.PayloadType) != 0;
    }

    public bool TryPickupNearbyCandidate()
    {
        return TryPickup(nearbyCandidate);
    }

    public void SetAutoPickupOnContact(bool enabled)
    {
        autoPickupOnContact = enabled;
    }

    public CarryableObject ReleaseAt(
        Transform dropPoint,
        Transform stableParent = null)
    {
        if (currentCargo == null)
        {
            return null;
        }

        CarryableObject releasedCargo = currentCargo;

        if (!releasedCargo.DropAt(dropPoint, stableParent))
        {
            return null;
        }

        BlockReleasedCargo(releasedCargo);
        currentCargo = null;
        return releasedCargo;
    }

    public CarryableObject ReleaseAtCurrentPose(
        Transform stableParent = null)
    {
        if (currentCargo == null)
        {
            return null;
        }

        CarryableObject releasedCargo = currentCargo;
        releasedCargo.DropAtCurrentPose(stableParent);
        BlockReleasedCargo(releasedCargo);
        currentCargo = null;
        nearbyCandidate = null;
        return releasedCargo;
    }

    public CarryableObject ReleaseForInstallation(
        CarryableObject cargo,
        Transform assemblyParent)
    {
        if (cargo == null || cargo != currentCargo)
        {
            return null;
        }

        if (!cargo.InstallAtCurrentPose(assemblyParent))
        {
            return null;
        }

        BlockReleasedCargo(cargo);
        currentCargo = null;
        nearbyCandidate = null;
        return cargo;
    }

    public CarryableObject ReleaseAtInstallationSocket(
        CarryableObject cargo,
        Transform socket)
    {
        if (cargo == null || cargo != currentCargo || socket == null)
        {
            return null;
        }

        if (!cargo.InstallAt(socket))
        {
            return null;
        }

        BlockReleasedCargo(cargo);
        currentCargo = null;
        nearbyCandidate = null;
        return cargo;
    }

    public void ClearCargoReference()
    {
        zkBottomCandidate = null;
        currentCargo = null;
        nearbyCandidate = null;
        blockedUntilSeparation.Clear();
        contacts.Clear();
    }

    private void OnTriggerEnter(Collider other)
    {
        HandleContact(other);
    }

    private void OnTriggerStay(Collider other)
    {
        if (currentCargo == null)
        {
            HandleContact(other);
        }
    }

    private void HandleContact(Collider other)
    {
        CarryableObject candidate =
            other.GetComponentInParent<CarryableObject>();

        if (candidate != null &&
            candidate != currentCargo &&
            Accepts(candidate))
        {
            contacts[other] = candidate;
            nearbyCandidate = candidate;

            if (!initialScanComplete)
            {
                initialContacts.Add(other);
                return;
            }

            if (autoPickupOnContact &&
                autoPickupArmed &&
                currentCargo == null)
            {
                if (carrierRole != RobotCarrierRole.ZK ||
                    candidate.PayloadType != CarryableType.Base ||
                    HasZkReachedPickupBottom(candidate))
                {
                    if (TryPickup(candidate)) zkBottomCandidate = null;
                }
            }
        }
    }

    private bool HasZkReachedPickupBottom(CarryableObject candidate)
    {
        Transform pickupMount = mountPoint != null ? mountPoint : transform;
        float y = pickupMount.position.y;
        if (zkBottomCandidate != candidate)
        {
            zkBottomCandidate = candidate;
            zkLowestContactY = y;
            return false;
        }
        zkLowestContactY = Mathf.Min(zkLowestContactY, y);
        // The endpoint cannot be known while still descending. Confirm it by
        // a small upward movement; preserveWorldPoseOnPickup keeps the base
        // at its supported position instead of snapping down to the tool.
        return y - zkLowestContactY >= Mathf.Max(0.001f, zkPickupRiseThreshold);
    }

    private void OnTriggerExit(Collider other)
    {
        initialContacts.Remove(other);

        CarryableObject contactedCargo;
        contacts.TryGetValue(other, out contactedCargo);
        contacts.Remove(other);

        if (contactedCargo != null &&
            !HasRemainingContact(contactedCargo))
        {
            blockedUntilSeparation.Remove(contactedCargo);
            if (zkBottomCandidate == contactedCargo) zkBottomCandidate = null;
        }

        if (initialScanComplete && initialContacts.Count == 0)
        {
            autoPickupArmed = true;
        }

        CarryableObject candidate =
            other.GetComponentInParent<CarryableObject>();

        if (candidate == nearbyCandidate)
        {
            nearbyCandidate = null;
        }
    }

    private void BlockReleasedCargo(CarryableObject cargo)
    {
        if (cargo != null)
        {
            blockedUntilSeparation.Add(cargo);
        }
    }

    private bool HasRemainingContact(CarryableObject cargo)
    {
        foreach (CarryableObject contact in contacts.Values)
        {
            if (contact == cargo)
            {
                return true;
            }
        }

        return false;
    }
}


// Project renderer-local corners into the collider's coordinate system.
// Using a world AABB here would inflate boxes when an object rotates.
public static class GeometryContactBounds
{
    public static bool TryBounds(Transform space, IEnumerable<Renderer> renderers, out Bounds bounds, bool includeInactive = false)
    {
        bounds = default;
        bool found = false;
        foreach (Renderer r in renderers)
        {
            if (r == null || !r.enabled || (!includeInactive && !r.gameObject.activeInHierarchy)) continue;
            Bounds local = r.localBounds;
            for (int i = 0; i < 8; i++)
            {
                Vector3 corner = local.center + Vector3.Scale(local.extents,
                    new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
                Vector3 point = space.InverseTransformPoint(r.transform.TransformPoint(corner));
                if (!found) { bounds = new Bounds(point, Vector3.zero); found = true; }
                else bounds.Encapsulate(point);
            }
        }
        return found;
    }

    public static void FitCargo(CarryableObject cargo)
    {
        BoxCollider box = cargo.GetComponent<BoxCollider>();
        if (box == null) return;
        var renderers = new List<Renderer>();
        foreach (Renderer r in cargo.GetComponentsInChildren<Renderer>(true))
            if (cargo.PayloadType == CarryableType.CompletedHouse || r.GetComponentInParent<CarryableObject>() == cargo) renderers.Add(r);
        if (!TryBounds(box.transform, renderers, out Bounds bounds, true)) return;
        box.center = bounds.center;
        box.size = bounds.size;
    }

    public static void FitFr5(RobotCargoMount mount)
    {
        if (mount.CarrierRole != RobotCarrierRole.FR5 || mount.transform.parent == null) return;
        BoxCollider box = mount.GetComponent<BoxCollider>();
        if (box == null) return;
        Transform palm = mount.transform.parent;
        var renderers = new List<Renderer>();
        foreach (Transform finger in palm.GetComponentsInChildren<Transform>(true))
        {
            if (finger.name != "Finger_L" && finger.name != "Finger_R") continue;
            foreach (Renderer r in finger.GetComponentsInChildren<Renderer>(true))
                if (r.GetComponentInParent<CarryableObject>() == null) renderers.Add(r);
        }
        if (!TryBounds(palm, renderers, out Bounds palmBounds)) return;
        // Both the attachment point and contact volume share the finger geometry centre.
        mount.transform.position = palm.TransformPoint(palmBounds.center);
        if (!TryBounds(box.transform, renderers, out Bounds bounds)) return;
        box.center = bounds.center;
        box.size = bounds.size;
    }
}
