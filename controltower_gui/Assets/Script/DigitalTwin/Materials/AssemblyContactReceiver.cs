using UnityEngine;

[DisallowMultipleComponent]
public class AssemblyContactReceiver : MonoBehaviour
{
    [SerializeField]
    private HouseAssemblyController houseAssembly;

    public void Configure(HouseAssemblyController controller)
    {
        houseAssembly = controller;
    }

    private void OnTriggerEnter(Collider other)
    {
        TryInstall(other);
    }

    private void OnTriggerStay(Collider other)
    {
        TryInstall(other);
    }

    private void TryInstall(Collider other)
    {
        if (houseAssembly == null)
        {
            return;
        }

        CarryableObject part =
            other.GetComponentInParent<CarryableObject>();

        if (part != null && part.PayloadType == CarryableType.Wall)
        {
            // Wall contacts use the same ordered alignment as release signals.
            FindAnyObjectByType<FactoryMaterialFlowController>()?.RequestWallContactPlacement(part);
            return;
        }
        houseAssembly.TryInstallFromContact(part);
    }
}
