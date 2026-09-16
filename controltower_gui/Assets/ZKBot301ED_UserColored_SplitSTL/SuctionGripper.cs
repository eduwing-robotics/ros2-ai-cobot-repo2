using UnityEngine;

// Call SetVacuum(true/false) from the ROS2 vacuum-state subscriber.
public class SuctionGripper : MonoBehaviour
{
    public Transform attachPoint;
    public string grabbableTag = "Grabbable";
    GameObject candidate;
    GameObject held;

    void OnTriggerEnter(Collider other)
    {
        if (other.CompareTag(grabbableTag)) candidate = other.gameObject;
    }

    void OnTriggerExit(Collider other)
    {
        if (candidate == other.gameObject) candidate = null;
    }

    public void SetVacuum(bool on)
    {
        if (on && held == null && candidate != null)
        {
            held = candidate;
            if (held.TryGetComponent<Rigidbody>(out var rb)) rb.isKinematic = true;
            held.transform.SetParent(attachPoint != null ? attachPoint : transform, true);
        }
        else if (!on && held != null)
        {
            held.transform.SetParent(null, true);
            if (held.TryGetComponent<Rigidbody>(out var rb)) rb.isKinematic = false;
            held = null;
        }
    }
}
