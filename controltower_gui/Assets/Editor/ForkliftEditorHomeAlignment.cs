#if UNITY_EDITOR
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

[InitializeOnLoad]
public static class ForkliftEditorHomeAlignment
{
    static ForkliftEditorHomeAlignment() { EditorApplication.delayCall += Align; }
    [MenuItem("Tools/Factory/Align Forklift Home Without Moving Model")]
    public static void Align()
    {
        if (EditorApplication.isPlayingOrWillChangePlaymode) return;
        foreach (ForkliftPoseApplier robot in Resources.FindObjectsOfTypeAll<ForkliftPoseApplier>())
        {
            if (!robot.gameObject.scene.IsValid() || EditorUtility.IsPersistent(robot)) continue;
            Undo.RegisterFullObjectHierarchyUndo(robot.gameObject, "Align forklift HOME pivot and contact box");
            robot.AlignEditorHome();
            EditorSceneManager.MarkSceneDirty(robot.gameObject.scene);
        }
    }
}
#endif
