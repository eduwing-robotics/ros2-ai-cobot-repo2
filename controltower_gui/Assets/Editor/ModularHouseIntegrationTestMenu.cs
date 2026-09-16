using UnityEditor;
using UnityEngine;

public static class ModularHouseIntegrationTestMenu
{
    [MenuItem("Tools/ModularHouse/Run Local Integration Self-Test")]
    private static void RunLocalIntegrationSelfTest()
    {
        if (!EditorApplication.isPlaying)
        {
            EditorUtility.DisplayDialog(
                "ModularHouse 로컬 테스트",
                "먼저 Play 버튼을 누른 뒤 이 메뉴를 실행해 주세요.",
                "확인");
            return;
        }

        FactoryStateManager stateManager =
            Object.FindAnyObjectByType<FactoryStateManager>();

        if (stateManager == null)
        {
            Debug.LogError(
                "[LOCAL SELF TEST FAIL] FactoryStateManager를 찾지 못했습니다.");
            return;
        }

        GameObject runnerObject = new GameObject(
            "ModularHouseLocalIntegrationTest");
        ModularHouseLocalIntegrationTest runner =
            runnerObject.AddComponent<ModularHouseLocalIntegrationTest>();
        runner.Run(stateManager);
    }
}
