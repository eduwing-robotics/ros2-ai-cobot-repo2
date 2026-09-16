using UnityEngine;

[DisallowMultipleComponent]
public class CompletedHouseFinishZone : MonoBehaviour
{
    [SerializeField]
    private Transform dropPoint;

    public void Configure(Transform targetDropPoint)
    {
        dropPoint = targetDropPoint != null
            ? targetDropPoint
            : transform;
    }

    private void OnTriggerEnter(Collider other)
    {
        TryDropCompletedHouse(other);
    }

    private void OnTriggerStay(Collider other)
    {
        TryDropCompletedHouse(other);
    }

    private void TryDropCompletedHouse(Collider other)
    {
        CarryableObject house =
            other.GetComponentInParent<CarryableObject>();

        if (house == null ||
            house.PayloadType != CarryableType.CompletedHouse ||
            house.State != CarryableState.AttachedToRobot)
        {
            return;
        }

        RobotCargoMount carrier =
            house.GetComponentInParent<RobotCargoMount>();

        if (carrier == null ||
            carrier.CarrierRole != RobotCarrierRole.ForkTool ||
            carrier.CurrentCargo != house)
        {
            return;
        }

        Transform target = dropPoint != null ? dropPoint : transform;
        carrier.ReleaseAt(target, target.parent);
        Debug.Log("[MaterialFlow] 완성 주택을 Finish에 배치했습니다.", this);
    }
}
