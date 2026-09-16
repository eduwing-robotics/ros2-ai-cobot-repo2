using UnityEngine;

[DisallowMultipleComponent]
public class BasePlacementZone : MonoBehaviour
{
    [SerializeField]
    private HouseAssemblyController houseAssembly;

    public void Configure(HouseAssemblyController controller)
    {
        houseAssembly = controller;
    }

    private void OnTriggerEnter(Collider other)
    {
        TryPlaceBase(other);
    }

    private void OnTriggerStay(Collider other)
    {
        TryPlaceBase(other);
    }

    private void TryPlaceBase(Collider other)
    {
        if (houseAssembly == null || houseAssembly.BasePlaced)
        {
            return;
        }

        CarryableObject cargo =
            other.GetComponentInParent<CarryableObject>();

        if (cargo == null ||
            cargo.PayloadType != CarryableType.Base ||
            cargo.State != CarryableState.AttachedToRobot)
        {
            return;
        }

        RobotCargoMount carrier =
            cargo.GetComponentInParent<RobotCargoMount>();

        if (carrier == null ||
            (carrier.CarrierRole != RobotCarrierRole.ZK &&
             carrier.CarrierRole != RobotCarrierRole.FR5))
        {
            return;
        }

        houseAssembly.PlaceBaseFrom(carrier);
    }
}
