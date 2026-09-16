using UnityEngine;

[DisallowMultipleComponent]
public class RoofVariantSpawner : MonoBehaviour
{
    [SerializeField] private GameObject roofPrefab;
    [SerializeField] private GameObject roof2Prefab;
    [SerializeField] private CarryableObject basePart;
    [SerializeField] private Transform materialParent;

    private Vector3 stagingPosition;
    private Quaternion stagingRotation;
    private GameObject activeRoof;

    public CarryableObject ActiveRoof =>
        activeRoof != null
            ? activeRoof.GetComponent<CarryableObject>()
            : null;

    private void Awake()
    {
        CaptureBaseStartPose();
    }

    public void CaptureBaseStartPose()
    {
        if (basePart == null)
        {
            return;
        }

        stagingPosition = basePart.InitialWorldPosition;
        stagingRotation = basePart.InitialWorldRotation;
    }

    public void Configure(
        GameObject firstRoofPrefab,
        GameObject secondRoofPrefab,
        CarryableObject baseCarryable,
        Transform parent)
    {
        roofPrefab = firstRoofPrefab;
        roof2Prefab = secondRoofPrefab;
        basePart = baseCarryable;
        materialParent = parent;
        CaptureBaseStartPose();
    }

    public void SetStagingBase(CarryableObject baseCarryable)
    {
        if (baseCarryable == null)
        {
            return;
        }

        basePart = baseCarryable;
        CaptureBaseStartPose();
    }

    public CarryableObject SpawnRoofVariant(string variant)
    {
        GameObject selectedPrefab = SelectPrefab(variant);

        if (selectedPrefab == null)
        {
            Debug.LogError(
                $"[RoofSpawner] 알 수 없는 지붕 종류: {variant}",
                this);
            return null;
        }

        ResetRoof();

        activeRoof = Instantiate(
            selectedPrefab,
            stagingPosition,
            stagingRotation,
            materialParent);

        activeRoof.name = selectedPrefab.name + "_Staged";
        // Inspector rotation is relative to materialParent, not world space.
        Vector3 localRoofEuler = activeRoof.transform.localEulerAngles;
        localRoofEuler.x = 0f;
        activeRoof.transform.localRotation = Quaternion.Euler(localRoofEuler);

        CarryableObject carryable =
            activeRoof.GetComponent<CarryableObject>();

        if (carryable == null)
        {
            carryable = activeRoof.AddComponent<CarryableObject>();
        }

        carryable.Configure(
            CarryableType.Roof,
            selectedPrefab.name);

        if (basePart != null)
        {
            Renderer[] renderers = activeRoof.GetComponentsInChildren<Renderer>(true);
            if (renderers.Length > 0)
            {
                Bounds roofBounds = renderers[0].bounds;
                for (int i = 1; i < renderers.Length; i++) roofBounds.Encapsulate(renderers[i].bounds);
                Bounds originalBase = basePart.InitialWorldBounds;
                activeRoof.transform.position += new Vector3(
                    originalBase.center.x - roofBounds.center.x,
                    originalBase.min.y - roofBounds.min.y,
                    originalBase.center.z - roofBounds.center.z);
            }
        }
        EnsureCollider(activeRoof);
        Debug.Log($"[RoofSpawner] staged at original base position: {activeRoof.transform.position:F4}", this);
        return carryable;
    }

    [ContextMenu("Reset Roof")]
    public void ResetRoof()
    {
        if (activeRoof != null)
        {
            Destroy(activeRoof);
            activeRoof = null;
        }
    }

    private GameObject SelectPrefab(string variant)
    {
        string normalized = (variant ?? string.Empty)
            .Trim()
            .Replace("-", string.Empty)
            .Replace("_", string.Empty)
            .ToUpperInvariant();

        switch (normalized)
        {
            case "ROOF":
            case "ROOF1":
            case "ROOF01":
            case "A":
            case "TYPEA":
                return roofPrefab;

            case "ROOF2":
            case "ROOF02":
            case "B":
            case "TYPEB":
                return roof2Prefab;

            default:
                return null;
        }
    }

    private static void EnsureCollider(GameObject target)
    {
        if (target.GetComponentInChildren<Collider>(true) != null)
        {
            return;
        }

        Renderer[] renderers =
            target.GetComponentsInChildren<Renderer>(true);

        if (renderers.Length == 0)
        {
            return;
        }

        Bounds worldBounds = renderers[0].bounds;

        for (int i = 1; i < renderers.Length; i++)
        {
            worldBounds.Encapsulate(renderers[i].bounds);
        }

        BoxCollider collider = target.AddComponent<BoxCollider>();
        collider.isTrigger = true;
        collider.center = target.transform.InverseTransformPoint(
            worldBounds.center);

        Vector3 localSize = target.transform.InverseTransformVector(
            worldBounds.size);

        collider.size = new Vector3(
            Mathf.Abs(localSize.x),
            Mathf.Abs(localSize.y),
            Mathf.Abs(localSize.z));
    }
}
