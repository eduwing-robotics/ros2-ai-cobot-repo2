using UnityEngine;

[System.Flags]
public enum CarryableType
{
    None = 0,
    Pallet = 1,
    Wall = 2,
    Base = 4,
    Roof = 8,
    Tool = 16,
    CompletedHouse = 32
}

public enum CarryableState
{
    Home,
    AttachedToRobot,
    Installed,
    Dropped
}

[DisallowMultipleComponent]
public class CarryableObject : MonoBehaviour
{
    [SerializeField]
    private string payloadId;

    [SerializeField]
    private CarryableType payloadType;

    [SerializeField]
    private CarryableState state = CarryableState.Home;

    private Transform initialParent;
    private Vector3 initialLocalPosition;
    private Quaternion initialLocalRotation;
    private Vector3 initialLocalScale;

    private Rigidbody[] rigidbodies;
    private bool[] initialKinematicStates;
    private bool[] initialGravityStates;

    public string PayloadId => payloadId;
    public CarryableType PayloadType => payloadType;
    public CarryableState State => state;
    private bool initialStateCaptured;
    private Vector3 initialWorldPosition;
    private Quaternion initialWorldRotation;
    private Bounds initialWorldBounds;
    public Vector3 InitialWorldPosition { get { CaptureInitialState(); return initialWorldPosition; } }
    public Quaternion InitialWorldRotation { get { CaptureInitialState(); return initialWorldRotation; } }
    public Bounds InitialWorldBounds { get { CaptureInitialState(); return initialWorldBounds; } }
    public Transform InitialParent => initialParent;

    private void Awake()
    {
        GeometryContactBounds.FitCargo(this);
        CaptureInitialState();
    }

    private void CaptureInitialState()
    {
        if (initialStateCaptured) return;
        initialStateCaptured = true;
        initialWorldPosition = transform.position;
        initialWorldRotation = transform.rotation;
        initialWorldBounds = new Bounds(transform.position, Vector3.zero);
        bool foundBounds = false;
        foreach (Renderer r in GetComponentsInChildren<Renderer>(true))
        {
            if (r.GetComponentInParent<CarryableObject>() != this) continue;
            if (!foundBounds) { initialWorldBounds = r.bounds; foundBounds = true; }
            else initialWorldBounds.Encapsulate(r.bounds);
        }
        initialParent = transform.parent;
        initialLocalPosition = transform.localPosition;
        initialLocalRotation = transform.localRotation;
        initialLocalScale = transform.localScale;

        rigidbodies = GetComponentsInChildren<Rigidbody>(true);
        initialKinematicStates = new bool[rigidbodies.Length];
        initialGravityStates = new bool[rigidbodies.Length];

        for (int i = 0; i < rigidbodies.Length; i++)
        {
            initialKinematicStates[i] = rigidbodies[i].isKinematic;
            initialGravityStates[i] = rigidbodies[i].useGravity;
        }
    }

    public bool AttachTo(
        Transform mountPoint,
        bool preserveWorldPose = false)
    {
        if (mountPoint == null)
        {
            Debug.LogError(
                $"[Carryable] {name}: 부착 위치가 없습니다.",
                this);

            return false;
        }

        SetCarriedPhysics();
        transform.SetParent(mountPoint, true);

        if (!preserveWorldPose)
        {
            transform.SetPositionAndRotation(
                mountPoint.position,
                mountPoint.rotation);
        }

        state = CarryableState.AttachedToRobot;
        return true;
    }

    public void Configure(
        CarryableType type,
        string id = null)
    {
        payloadType = type;

        if (!string.IsNullOrWhiteSpace(id))
        {
            payloadId = id;
        }
    }

    public bool InstallAt(Transform socket)
    {
        if (socket == null)
        {
            Debug.LogError(
                $"[Carryable] {name}: 설치 Socket이 없습니다.",
                this);

            return false;
        }

        SetCarriedPhysics();
        transform.SetParent(socket, true);
        transform.SetPositionAndRotation(
            socket.position,
            socket.rotation);

        state = CarryableState.Installed;
        return true;
    }

    public bool InstallAtCurrentPose(Transform assemblyParent)
    {
        if (assemblyParent == null)
        {
            Debug.LogError(
                $"[Carryable] {name}: 조립 부모가 없습니다.",
                this);
            return false;
        }

        SetCarriedPhysics();
        transform.SetParent(assemblyParent, true);
        state = CarryableState.Installed;
        return true;
    }

    public bool DropAt(
        Transform dropPoint,
        Transform stableParent = null)
    {
        if (dropPoint == null)
        {
            Debug.LogError(
                $"[Carryable] {name}: 내려놓을 위치가 없습니다.",
                this);

            return false;
        }

        transform.SetParent(
            stableParent != null
                ? stableParent
                : dropPoint.parent,
            true);

        transform.SetPositionAndRotation(
            dropPoint.position,
            dropPoint.rotation);

        RestorePhysics();
        state = CarryableState.Dropped;
        return true;
    }

    public bool DropAtCurrentPose(Transform stableParent = null)
    {
        transform.SetParent(stableParent, true);
        RestorePhysics();
        state = CarryableState.Dropped;
        return true;
    }

    public void ResetToInitialState()
    {
        transform.SetParent(initialParent, false);
        transform.localPosition = initialLocalPosition;
        transform.localRotation = initialLocalRotation;
        transform.localScale = initialLocalScale;

        RestorePhysics();
        state = CarryableState.Home;
    }

    private void SetCarriedPhysics()
    {
        if (rigidbodies == null)
        {
            return;
        }

        foreach (Rigidbody body in rigidbodies)
        {
            if (body == null) continue;

            body.linearVelocity = Vector3.zero;
            body.angularVelocity = Vector3.zero;
            body.useGravity = false;
            body.isKinematic = true;
        }
    }

    private void RestorePhysics()
    {
        if (rigidbodies == null)
        {
            return;
        }

        for (int i = 0; i < rigidbodies.Length; i++)
        {
            Rigidbody body = rigidbodies[i];
            if (body == null) continue;

            body.isKinematic = initialKinematicStates[i];
            body.useGravity = initialGravityStates[i];
        }
    }
}
