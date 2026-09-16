#if UNITY_EDITOR
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

[InitializeOnLoad]
public static class ContactBoundsEditor
{
    static ContactBoundsEditor()
    {
        EditorApplication.delayCall += RefreshLoadedScenes;
        EditorSceneManager.sceneOpened += (scene, mode) => RefreshLoadedScenes();
    }

    [MenuItem("Tools/ModularHouse/Align Contact Boxes To Geometry")]
    public static void RefreshLoadedScenes()
    {
        if (EditorApplication.isPlayingOrWillChangePlaymode) return;
        int count = 0;
        foreach (CarryableObject cargo in Object.FindObjectsByType<CarryableObject>(FindObjectsInactive.Include, FindObjectsSortMode.None))
        {
            if (!cargo.gameObject.scene.IsValid() || EditorUtility.IsPersistent(cargo)) continue;
            BoxCollider box = cargo.GetComponent<BoxCollider>();
            if (box == null) continue;
            Undo.RecordObject(box, "Align material contact box");
            GeometryContactBounds.FitCargo(cargo);
            EditorSceneManager.MarkSceneDirty(cargo.gameObject.scene);
            count++;
        }
        foreach (RobotCargoMount mount in Object.FindObjectsByType<RobotCargoMount>(FindObjectsInactive.Include, FindObjectsSortMode.None))
        {
            if (mount.CarrierRole != RobotCarrierRole.FR5 || EditorUtility.IsPersistent(mount) || !mount.gameObject.scene.IsValid()) continue;
            BoxCollider box = mount.GetComponent<BoxCollider>();
            if (box == null) continue;
            Undo.RecordObjects(new Object[] { mount.transform, box }, "Align FR5 contact box");
            GeometryContactBounds.FitFr5(mount);
            EditorSceneManager.MarkSceneDirty(mount.gameObject.scene);
            count++;
        }
        foreach (ForkliftPoseApplier forklift in Object.FindObjectsByType<ForkliftPoseApplier>(FindObjectsSortMode.None))
        {
            if (EditorUtility.IsPersistent(forklift) || !forklift.gameObject.scene.IsValid()) continue;
            BoxCollider forkBox = forklift.GetComponentInChildren<BoxCollider>();
            Transform forkMount = forklift.transform.Find("PalletContactMount");
            if (forkMount != null) forkBox = forkMount.GetComponent<BoxCollider>();
            if (forkBox != null) Undo.RecordObject(forkBox, "Align forklift contact box");
            forklift.RefreshContactBoxGeometry();
            EditorSceneManager.MarkSceneDirty(forklift.gameObject.scene);
        }
        SceneView.RepaintAll();
        Debug.Log($"[ContactBounds] Refreshed {count} material/FR5 boxes and forklift contact geometry. Scene transforms preserved; save the scene to persist collider changes.");
    }
}
#endif
