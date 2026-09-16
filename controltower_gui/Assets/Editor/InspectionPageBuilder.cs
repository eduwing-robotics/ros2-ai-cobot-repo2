using System;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public static class InspectionPageBuilder
{
    private const string ScenePath = "Assets/Scenes/SampleScene.unity";
    private const string IncomingSuccessImagePath =
        "Assets/UI/References/IncomingInspectionSuccess.png";
    private const string AutoBuildKey =
        "ModularHouse.ProfessionalUI.v20.VoiceAssistantBuilt";

    private static readonly Color PageColor =
        new Color32(245, 247, 250, 255);

    private static readonly Color CardColor =
        new Color32(255, 255, 255, 255);

    private static readonly Color TextColor =
        new Color32(27, 36, 49, 255);

    [MenuItem("Tools/ModularHouse/Build Inspection UI")]
    public static void BuildFromMenu()
    {
        Build(false);
    }

    public static void BuildFromCommandLine()
    {
        Build(true);
    }

    // Selective restyling: never rebuild cards, references or the user's map.
    public static void ApplyIndustrialThemeFromCommandLine()
    {
        var scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
        PageManager pageManager = FindSceneComponent<PageManager>(scene);
        ConfigureControlTowerLayout(scene, pageManager);
        foreach (var list in FindSceneComponents<JobListUI>(scene))
            list.ConfigureEmptyStateLayout();
        int stepIndex = 0;
        foreach (var step in scene.GetRootGameObjects()
                     .SelectMany(root => root.GetComponentsInChildren<ProcessStepUI>(true)))
        {
            var serializedStep = new SerializedObject(step);
            var label = serializedStep.FindProperty("labelText").objectReferenceValue as Text;
            step.Initialize(++stepIndex, label != null ? label.text : "");
            step.SetConnectorCompleted(false);
        }
        foreach (var root in scene.GetRootGameObjects())
            foreach (var canvas in root.GetComponentsInChildren<Canvas>(true))
                IndustrialConsoleTheme.Apply(canvas);
        foreach (var manager in UnityEngine.Object.FindObjectsByType<PageManager>(
                     FindObjectsInactive.Include, FindObjectsSortMode.None))
        {
            manager.selectedColor = IndustrialConsoleTheme.Accent;
            manager.normalColor = IndustrialConsoleTheme.Panel;
            EditorUtility.SetDirty(manager);
        }
        foreach (var root in scene.GetRootGameObjects())
            foreach (var component in root.GetComponentsInChildren<Component>(true))
                if ((component is RectTransform || component is Graphic ||
                     component is LayoutGroup) && PrefabUtility.IsPartOfPrefabInstance(component))
                    PrefabUtility.RecordPrefabInstancePropertyModifications(component);
        EditorSceneManager.MarkSceneDirty(scene);
        EditorSceneManager.SaveScene(scene);
        VerifyIncomingJobIsolation();
        CaptureIndustrialPreview();
        Debug.Log("[IndustrialTheme] Saved scene; QA job isolation checks PASS.");
    }

    public static void DumpUiHierarchyFromCommandLine()
    {
        var scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
        foreach (var root in scene.GetRootGameObjects())
        {
            foreach (var rect in root.GetComponentsInChildren<RectTransform>(true))
            {
                int depth = 0;
                for (Transform p = rect.parent; p != null; p = p.parent) depth++;
                Debug.Log($"[UI-DUMP] {new string(' ', depth * 2)}{rect.name} " +
                    $"a=({rect.anchorMin.x:F2},{rect.anchorMin.y:F2})-({rect.anchorMax.x:F2},{rect.anchorMax.y:F2}) " +
                    $"o=({rect.offsetMin.x:F0},{rect.offsetMin.y:F0})-({rect.offsetMax.x:F0},{rect.offsetMax.y:F0})");
            }
        }
    }

    public static void CaptureExistingThemeFromCommandLine()
    {
        EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
        Canvas.ForceUpdateCanvases();
        CaptureIndustrialPreview();
        Debug.Log("[IndustrialTheme] Existing saved scene previews captured.");
    }

    public static void CaptureAssemblyInspectionPreviewFromCommandLine()
    {
        Scene scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
        InspectionPageUI inspection = FindSceneComponent<InspectionPageUI>(scene);
        const System.Reflection.BindingFlags flags =
            System.Reflection.BindingFlags.Instance |
            System.Reflection.BindingFlags.NonPublic;
        typeof(InspectionPageUI).GetMethod("Awake", flags)
            ?.Invoke(inspection, null);
        typeof(InspectionPageUI).GetField("currentIncomingJobId", flags)
            ?.SetValue(inspection, 900001L);
        var previewStatus = new ProductionInspectionStatusData
        {
            job_id = 900001,
            inspection_type = "PRE_ROOF",
            inspection = new ProductionInspectionData
            {
                inspection_id = 900001,
                inspection_cycle = 1,
                status = "RUNNING",
                result = null,
                current_view = "LEFT",
                production_valid = false,
                gate_state = "NOT_RELEASED",
                views = new[]
                {
                    new ProductionInspectionViewData
                        { view_name = "TOP", status = "PASS", result = "PASS" },
                    new ProductionInspectionViewData
                        { view_name = "LEFT", status = "IN_PROGRESS" },
                    new ProductionInspectionViewData
                        { view_name = "RIGHT", status = "PENDING" },
                    new ProductionInspectionViewData
                        { view_name = "FRONT", status = "PENDING" },
                    new ProductionInspectionViewData
                        { view_name = "BEHIND", status = "PENDING" }
                }
            }
        };
        typeof(InspectionPageUI).GetMethod(
                "HandleProductionInspectionStatusUpdated", flags)
            ?.Invoke(inspection, new object[] { previewStatus });
        Canvas.ForceUpdateCanvases();
        CaptureIndustrialPreview();
        Debug.Log("[InspectionPageBuilder] Assembly inspection preview captured.");
    }

    private static void CaptureIndustrialPreview()
    {
        var manager = UnityEngine.Object.FindAnyObjectByType<PageManager>();
        var canvas = manager.dashboardPanel.GetComponentInParent<Canvas>();
        var cameraObject = new GameObject("Theme preview camera");
        var camera = cameraObject.AddComponent<Camera>();
        camera.clearFlags = CameraClearFlags.SolidColor;
        camera.backgroundColor = IndustrialConsoleTheme.Background;
        camera.cullingMask = 1 << LayerMask.NameToLayer("UI");
        var render = new RenderTexture(1920, 1080, 24);
        camera.targetTexture = render;
        canvas.renderMode = RenderMode.ScreenSpaceCamera;
        canvas.worldCamera = camera;
        canvas.planeDistance = 1;
        var pages = new[] { manager.dashboardPanel, manager.robotPanel,
            manager.taskPanel, manager.inspectionPanel };
        var digitalTwinCamera = FindSceneObject(
            manager.gameObject.scene, "DigitalTwinCamera")?.GetComponent<Camera>();
        var buttons = new[] { manager.dashboardButton, manager.robotButton,
            manager.taskButton, manager.inspectionButton };
        const System.Reflection.BindingFlags pageFlags =
            System.Reflection.BindingFlags.Instance |
            System.Reflection.BindingFlags.NonPublic;
        typeof(PageManager).GetMethod("Start", pageFlags)?.Invoke(manager, null);
        System.IO.Directory.CreateDirectory("Logs/ThemePreview");
        for (int pageIndex = 0; pageIndex < pages.Length; pageIndex++)
        {
            GameObject page = pages[pageIndex];
            typeof(PageManager).GetMethod("OpenPage", pageFlags)?.Invoke(
                manager, new object[] { page, buttons[pageIndex] });
            if (page == null) continue;
            IndustrialConsoleTheme.Apply(canvas);
            Canvas.ForceUpdateCanvases();
            digitalTwinCamera?.Render();
            camera.Render();
            RenderTexture.active = render;
            var texture = new Texture2D(1920, 1080, TextureFormat.RGB24, false);
            texture.ReadPixels(new Rect(0, 0, 1920, 1080), 0, 0);
            texture.Apply();
            System.IO.File.WriteAllBytes("Logs/ThemePreview/" + page.name + ".png",
                texture.EncodeToPNG());
            UnityEngine.Object.DestroyImmediate(texture);
        }
        RenderTexture.active = null;
        camera.targetTexture = null;
        UnityEngine.Object.DestroyImmediate(render);
        UnityEngine.Object.DestroyImmediate(cameraObject);
        // Preview mutations are deliberately not saved.
    }

    public static void VerifyIncomingJobIsolation()
    {
        var host = new GameObject("QA isolation test");
        host.SetActive(false);
        var manager = host.AddComponent<FactoryStateManager>();
        var page = host.AddComponent<InspectionPageUI>();
        const System.Reflection.BindingFlags flags =
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic;
        typeof(InspectionPageUI).GetField("factoryStateManager", flags).SetValue(page, manager);
        void Call(string method, params object[] args) =>
            typeof(InspectionPageUI).GetMethod(method, flags).Invoke(page, args);
        void Check(bool ok, string reason)
        {
            if (!ok) throw new InvalidOperationException("QA isolation: " + reason);
        }
        void Job(long id, string state) =>
            manager.ApplyLocalProductionStatusForTest(new ProductionJobData {
                job_id = id.ToString(), numeric_job_id = id, job_status = state, status = state
            });
        void Qa(long job, long transaction)
        {
            var data = new IncomingQaStatusData {
                job_id = job, job_gate_state = "NOT_RELEASED",
                transaction = new IncomingQaTransactionData {
                    transaction_id = transaction, cycle = 1, mode = "BASE_AB",
                    status = "COMPLETED", overall_result = "PASS"
                }, items = new IncomingQaItemData[0]
            };
            typeof(FactoryStateManager).GetMethod("StoreIncomingQaStatus", flags)
                .Invoke(manager, new object[] { data });
            Call("HandleIncomingQaStatusUpdated", data);
        }
        try
        {
            Call("OnEnable");
            Job(101, "RUNNING");
            Qa(101, 1);
            Check(page.IncomingPassedInspectionCount == 1, "current job PASS");
            Job(101, "CANCELED");
            Check(page.IncomingPassedInspectionCount == 0, "cancel clears");
            Job(102, "RUNNING");
            Check(page.IncomingPassedInspectionCount == 0, "new job without QA clears");
            Qa(101, 2);
            Check(page.IncomingPassedInspectionCount == 0, "late old result ignored");
            Qa(102, 3);
            Check(page.IncomingPassedInspectionCount == 1, "new job result applied");
            Call("OnDisable");
            Job(102, "CANCELED");
            Job(103, "RUNNING");
            Call("OnEnable");
            Check(page.IncomingPassedInspectionCount == 0, "hidden page reopens on new job");
            Call("HandleInspectionSnapshot", new ProductionSnapshotData());
            Check(page.IncomingPassedInspectionCount == 0, "snapshot excludes historic QA");
            Check(manager.IncomingQaStatuses.Count == 3, "job histories retained");
            Debug.Log("[QA ISOLATION PASS] 8 assertions");
        }
        finally
        {
            Call("OnDisable");
            UnityEngine.Object.DestroyImmediate(host);
        }
    }

    [InitializeOnLoadMethod]
    private static void BuildOnceOnEditorOpen()
    {
        if (Application.isBatchMode || EditorPrefs.GetBool(AutoBuildKey, false))
        {
            return;
        }

        EditorApplication.delayCall += () =>
        {
            try
            {
                Build(false);
                EditorPrefs.SetBool(AutoBuildKey, true);
            }
            catch (Exception exception)
            {
                Debug.LogException(exception);
            }
        };
    }

    private static void Build(bool exitWhenDone)
    {
        Scene scene = EditorSceneManager.OpenScene(
            ScenePath,
            OpenSceneMode.Single);

        PageManager pageManager = FindSceneComponent<PageManager>(scene);

        if (pageManager == null)
        {
            throw new InvalidOperationException(
                "SampleScene에서 PageManager를 찾을 수 없습니다.");
        }

        Button inspectionButton = BuildInspectionButton(pageManager);
        GameObject inspectionPage = BuildInspectionPage(pageManager);
        ConfigureVisionVideoReceivers(scene, inspectionPage);

        SerializedObject pageManagerObject =
            new SerializedObject(pageManager);

        pageManagerObject.FindProperty("inspectionPanel")
            .objectReferenceValue = inspectionPage;
        pageManagerObject.FindProperty("inspectionButton")
            .objectReferenceValue = inspectionButton;
        pageManagerObject.ApplyModifiedPropertiesWithoutUndo();

        ConfigureRobotPanels(scene);
        NormalizeServerRobotIds(scene);
        ConfigureDashboardPresentation(scene);
        ConfigureProductionProcessHeader(scene);
        DisableLegacyTaskStatus(scene);
        ConfigureJobEmptyState(scene);
        ConfigureEventLog(scene);
        ConfigureMaterialFlow(scene);
        ConfigureFR5PlaneAndRackVisuals(scene);
        ConfigureProfessionalTheme(scene, pageManager);
        ConfigureControlTowerLayout(scene, pageManager);
        foreach (var root in scene.GetRootGameObjects())
            foreach (var themeCanvas in root.GetComponentsInChildren<Canvas>(true))
                IndustrialConsoleTheme.Apply(themeCanvas);

        inspectionPage.SetActive(false);
        EditorSceneManager.MarkSceneDirty(scene);
        EditorSceneManager.SaveScene(scene);
        AssetDatabase.SaveAssets();
        VerifyVoiceRuntimeContract();
        VerifyPreRoofViewContract();

        Debug.Log("[InspectionPageBuilder] 검사 페이지 UI 구성이 완료되었습니다.");

        if (exitWhenDone)
        {
            EditorApplication.Exit(0);
        }
    }

    private static void VerifyPreRoofViewContract()
    {
        GameObject host = new GameObject("PRE_ROOF View contract test");
        host.SetActive(false);
        InspectionPageUI page = host.AddComponent<InspectionPageUI>();
        Text statusText = new GameObject(
            "Status", typeof(RectTransform), typeof(CanvasRenderer), typeof(Text))
            .GetComponent<Text>();
        Text detailText = new GameObject(
            "Detail", typeof(RectTransform), typeof(CanvasRenderer), typeof(Text))
            .GetComponent<Text>();
        statusText.transform.SetParent(host.transform, false);
        detailText.transform.SetParent(host.transform, false);

        const System.Reflection.BindingFlags flags =
            System.Reflection.BindingFlags.Instance |
            System.Reflection.BindingFlags.NonPublic;
        typeof(InspectionPageUI).GetField("assemblyStatusText", flags)
            ?.SetValue(page, statusText);
        typeof(InspectionPageUI).GetField("assemblyDetailText", flags)
            ?.SetValue(page, detailText);
        typeof(InspectionPageUI).GetField("currentIncomingJobId", flags)
            ?.SetValue(page, 900001L);

        var data = new ProductionInspectionStatusData
        {
            job_id = 900001,
            inspection_type = "PRE_ROOF",
            inspection = new ProductionInspectionData
            {
                inspection_id = 1,
                inspection_cycle = 1,
                status = "RUNNING",
                current_view = "LEFT",
                production_valid = false,
                gate_state = "NOT_RELEASED",
                views = new[]
                {
                    new ProductionInspectionViewData
                        { view_name = "TOP", status = "PASS", result = "PASS" },
                    new ProductionInspectionViewData
                        { view_name = "LEFT", status = "IN_PROGRESS" },
                    new ProductionInspectionViewData
                        { view_name = "RIGHT", status = "PENDING" },
                    new ProductionInspectionViewData
                        { view_name = "FRONT", status = "PENDING" },
                    new ProductionInspectionViewData
                        { view_name = "BEHIND", status = "PENDING" }
                }
            }
        };

        try
        {
            var receive = typeof(InspectionPageUI).GetMethod(
                "HandleProductionInspectionStatusUpdated", flags);
            receive?.Invoke(page, new object[] { data });
            bool runningOk =
                page.AssemblyFeedState == InspectionPageUI.FeedState.Inspecting &&
                page.CurrentAssemblyView == "LEFT" &&
                statusText.text == "검사 중 · LEFT" &&
                detailText.text.Contains("TOP · 통과") &&
                detailText.text.Contains("LEFT · 현재") &&
                detailText.text.Contains("BEHIND · 검사대기");

            foreach (ProductionInspectionViewData view in data.inspection.views)
            {
                view.status = "PASS";
                view.result = "PASS";
            }
            data.inspection.status = "COMPLETED";
            data.inspection.result = "PASS";
            data.inspection.current_view = null;
            data.inspection.gate_state = "RELEASED";
            data.inspection.production_valid = false;
            receive?.Invoke(page, new object[] { data });

            bool releasedOk =
                page.AssemblyFeedState == InspectionPageUI.FeedState.Passed &&
                statusText.text == "검사 성공" &&
                detailText.text.Contains("Roof Gate 해제");

            if (!runningOk || !releasedOk)
            {
                throw new InvalidOperationException(
                    $"PRE_ROOF View contract failed: " +
                    $"running={runningOk}, released={releasedOk}");
            }
        }
        finally
        {
            UnityEngine.Object.DestroyImmediate(host);
        }

        Debug.Log(
            "[InspectionPageBuilder] PRE_ROOF View/Gate contract PASS " +
            "(production_valid=false ignored).");
    }

    private static void VerifyVoiceRuntimeContract()
    {
        GameObject host = new GameObject("Voice runtime contract test");
        host.SetActive(false);
        FactoryStateManager manager = host.AddComponent<FactoryStateManager>();

        try
        {
            const System.Reflection.BindingFlags flags =
                System.Reflection.BindingFlags.Instance |
                System.Reflection.BindingFlags.NonPublic;
            var receive = typeof(FactoryStateManager).GetMethod(
                "HandleVoiceRuntimeEvent", flags);

            receive?.Invoke(manager, new object[]
            {
                "{\"schema_version\":\"1.0\"," +
                "\"type\":\"voice_runtime_event\"," +
                "\"timestamp\":\"2026-09-11T16:20:04+09:00\"," +
                "\"sequence\":103,\"data\":{" +
                "\"state\":\"RESPONDING\"," +
                "\"turn_id\":\"LOCAL-VOICE-1\"," +
                "\"transcript\":\"현재 무슨 작업 중이야?\"," +
                "\"response_text\":\"현재 생산 작업을 진행 중입니다.\"," +
                "\"intent\":\"QUERY_JOB_STATUS\"}}"
            });

            VoiceRuntimeData responding = manager.CurrentVoiceRuntime;
            bool respondingStored = responding != null &&
                responding.state == "RESPONDING" &&
                responding.recent_turns != null &&
                responding.recent_turns.Length == 1;

            receive?.Invoke(manager, new object[]
            {
                "{\"schema_version\":\"1.0\"," +
                "\"type\":\"voice_runtime_event\"," +
                "\"timestamp\":\"2026-09-11T16:20:08+09:00\"," +
                "\"sequence\":104,\"data\":{" +
                "\"state\":\"IDLE\",\"transcript\":null," +
                "\"response_text\":null,\"intent\":null}}"
            });

            VoiceRuntimeData idle = manager.CurrentVoiceRuntime;
            bool idleKeepsHistory = idle != null &&
                idle.state == "IDLE" &&
                idle.recent_turns != null &&
                idle.recent_turns.Length == 1;

            if (!respondingStored || !idleKeepsHistory)
            {
                throw new InvalidOperationException(
                    "Voice runtime 상태 전환 또는 최근 대화 보존에 실패했습니다.");
            }

            Debug.Log(
                "[VOICE CONTRACT PASS] RESPONDING 기록 및 IDLE 이력 보존");
        }
        finally
        {
            UnityEngine.Object.DestroyImmediate(host);
        }
    }

    private static Button BuildInspectionButton(PageManager pageManager)
    {
        GameObject existing = FindSceneObject(
            pageManager.gameObject.scene,
            "InspectionBtn");

        GameObject buttonObject;

        if (existing != null)
        {
            buttonObject = existing;
        }
        else
        {
            buttonObject = UnityEngine.Object.Instantiate(
                pageManager.taskButton.gameObject,
                pageManager.taskButton.transform.parent);

            buttonObject.name = "InspectionBtn";

            RectTransform source =
                pageManager.taskButton.GetComponent<RectTransform>();

            RectTransform target =
                buttonObject.GetComponent<RectTransform>();

            float spacing = Mathf.Max(source.rect.height, 50f) + 2f;
            target.anchoredPosition =
                source.anchoredPosition + Vector2.down * spacing;
        }

        Text label = buttonObject.GetComponentInChildren<Text>(true);

        if (label != null)
        {
            label.text = "검사";
        }

        buttonObject.SetActive(true);
        return buttonObject.GetComponent<Button>();
    }

    private static GameObject BuildInspectionPage(PageManager pageManager)
    {
        Transform contentRoot = pageManager.taskPanel.transform.parent;
        Transform oldPage = contentRoot.Find("InspectionP");

        if (oldPage != null)
        {
            UnityEngine.Object.DestroyImmediate(oldPage.gameObject);
        }

        GameObject page = CreateRectObject("InspectionP", contentRoot);
        Stretch(page.GetComponent<RectTransform>(), Vector2.zero, Vector2.zero);

        Image pageImage = page.AddComponent<Image>();
        pageImage.color = PageColor;

        FeedWidgets incoming = CreateFeedCard(
            page.transform,
            "IncomingInspectionCard",
            "수입검사 영상",
            new Vector2(0f, 0f),
            new Vector2(0.5f, 1f),
            new Vector2(24f, 24f),
            new Vector2(-10f, -18f));

        FeedWidgets assembly = CreateFeedCard(
            page.transform,
            "AssemblyInspectionCard",
            "조립 결과 검사 영상",
            new Vector2(0.5f, 0f),
            new Vector2(1f, 1f),
            new Vector2(10f, 24f),
            new Vector2(-24f, -18f));

        Texture2D incomingSuccessTexture =
            AssetDatabase.LoadAssetAtPath<Texture2D>(IncomingSuccessImagePath);

        InspectionPageUI controller =
            page.AddComponent<InspectionPageUI>();

        SerializedObject controllerObject =
            new SerializedObject(controller);

        AssignFeed(controllerObject, "incoming", incoming);
        AssignFeed(controllerObject, "assembly", assembly);
        controllerObject.FindProperty("incomingSuccessReferenceTexture")
            .objectReferenceValue = incomingSuccessTexture;
        controllerObject.ApplyModifiedPropertiesWithoutUndo();

        return page;
    }

    private static void ConfigureVisionVideoReceivers(
        Scene scene,
        GameObject inspectionPage)
    {
        GameObject managers = FindSceneObject(scene, "Managers");

        if (managers == null)
        {
            throw new InvalidOperationException(
                "SampleScene에서 Managers를 찾을 수 없습니다.");
        }

        foreach (VisionUdpVideoReceiver receiver in
                 FindSceneComponents<VisionUdpVideoReceiver>(scene))
        {
            UnityEngine.Object.DestroyImmediate(receiver);
        }

        InspectionPageUI page = inspectionPage.GetComponent<InspectionPageUI>();
        CreateVideoReceiver(
            managers,
            page,
            21010,
            1,
            InspectionPageUI.InspectionTarget.Incoming);
        CreateVideoReceiver(
            managers,
            page,
            21020,
            2,
            InspectionPageUI.InspectionTarget.Assembly);
        CreateVideoReceiver(
            managers,
            null,
            21030,
            3,
            InspectionPageUI.InspectionTarget.Incoming);
    }

    private static void CreateVideoReceiver(
        GameObject host,
        InspectionPageUI page,
        int port,
        int streamId,
        InspectionPageUI.InspectionTarget target)
    {
        VisionUdpVideoReceiver receiver =
            host.AddComponent<VisionUdpVideoReceiver>();
        SerializedObject serialized = new SerializedObject(receiver);
        serialized.FindProperty("listenPort").intValue = port;
        serialized.FindProperty("expectedStreamId").intValue = streamId;
        serialized.FindProperty("incompleteFrameTimeoutMs").intValue = 200;
        serialized.FindProperty("maximumFrameBytes").intValue = 4 * 1024 * 1024;
        serialized.FindProperty("inspectionPage").objectReferenceValue = page;
        serialized.FindProperty("target").enumValueIndex = (int)target;
        serialized.ApplyModifiedPropertiesWithoutUndo();
    }

    private static FeedWidgets CreateFeedCard(
        Transform parent,
        string objectName,
        string titleText,
        Vector2 anchorMin,
        Vector2 anchorMax,
        Vector2 offsetMin,
        Vector2 offsetMax)
    {
        GameObject card = CreateRectObject(objectName, parent);
        SetRect(
            card.GetComponent<RectTransform>(),
            anchorMin,
            anchorMax,
            offsetMin,
            offsetMax);

        Image cardImage = card.AddComponent<Image>();
        cardImage.color = CardColor;

        Text title = CreateText(
            "TitleText",
            card.transform,
            titleText,
            20,
            FontStyle.Bold,
            TextAnchor.MiddleLeft);

        SetRect(
            title.rectTransform,
            new Vector2(0f, 1f),
            new Vector2(0.7f, 1f),
            new Vector2(18f, -52f),
            new Vector2(0f, -10f));

        Text status = CreateText(
            "StatusText",
            card.transform,
            "대기",
            16,
            FontStyle.Bold,
            TextAnchor.MiddleRight);

        SetRect(
            status.rectTransform,
            new Vector2(0.7f, 1f),
            new Vector2(1f, 1f),
            new Vector2(0f, -52f),
            new Vector2(-18f, -10f));

        GameObject videoObject = CreateRectObject(
            "Video",
            card.transform);

        SetRect(
            videoObject.GetComponent<RectTransform>(),
            new Vector2(0f, 0f),
            new Vector2(1f, 1f),
            new Vector2(18f, 152f),
            new Vector2(-18f, -62f));

        RawImage video = videoObject.AddComponent<RawImage>();
        video.color = new Color32(25, 31, 38, 255);
        video.raycastTarget = false;

        Text placeholder = CreateText(
            "PlaceholderText",
            videoObject.transform,
            "검사 영상 대기 중",
            18,
            FontStyle.Normal,
            TextAnchor.MiddleCenter);

        placeholder.color = new Color32(190, 198, 205, 255);
        Stretch(placeholder.rectTransform, Vector2.zero, Vector2.zero);

        Text defectLabel = CreateText(
            "DefectLabelText",
            card.transform,
            "불량 판별",
            14,
            FontStyle.Bold,
            TextAnchor.MiddleLeft);

        SetRect(
            defectLabel.rectTransform,
            new Vector2(0f, 0f),
            new Vector2(1f, 0f),
            new Vector2(18f, 116f),
            new Vector2(-18f, 146f));

        GameObject defectBox = CreateRectObject(
            "DefectResultBox",
            card.transform);

        SetRect(
            defectBox.GetComponent<RectTransform>(),
            new Vector2(0f, 0f),
            new Vector2(1f, 0f),
            new Vector2(18f, 18f),
            new Vector2(-18f, 112f));

        Image defectBoxImage = defectBox.AddComponent<Image>();
        defectBoxImage.color = new Color32(245, 247, 249, 255);

        Text detail = CreateText(
            "DefectText",
            defectBox.transform,
            "판별 대기",
            14,
            FontStyle.Normal,
            TextAnchor.MiddleLeft);

        SetRect(
            detail.rectTransform,
            Vector2.zero,
            Vector2.one,
            new Vector2(12f, 6f),
            new Vector2(-12f, -6f));
        detail.fontSize = 13;
        detail.lineSpacing = 1.1f;
        detail.verticalOverflow = VerticalWrapMode.Truncate;

        return new FeedWidgets
        {
            image = video,
            status = status,
            placeholder = placeholder,
            detail = detail
        };
    }

    private static void AssignFeed(
        SerializedObject target,
        string prefix,
        FeedWidgets widgets)
    {
        target.FindProperty(prefix + "InspectionImage")
            .objectReferenceValue = widgets.image;
        target.FindProperty(prefix + "StatusText")
            .objectReferenceValue = widgets.status;
        target.FindProperty(prefix + "PlaceholderText")
            .objectReferenceValue = widgets.placeholder;
        target.FindProperty(prefix + "DetailText")
            .objectReferenceValue = widgets.detail;
    }

    private static void ConfigureRobotPanels(Scene scene)
    {
        GameObject removedRobotPanel = FindSceneObject(scene, "ZK2P");

        if (removedRobotPanel != null)
        {
            removedRobotPanel.SetActive(false);
        }

        GameObject firstRobotPanel = FindSceneObject(scene, "TB3P") ??
                                     FindSceneObject(scene, "FR5P") ??
                                     FindSceneObject(scene, "ZK1P");

        if (firstRobotPanel == null)
        {
            return;
        }

        Transform cardParent = firstRobotPanel.transform.parent;
        GameObject voicePanel = FindSceneObject(scene, "VoiceAssistantP") ??
                                CreateRectObject("VoiceAssistantP", cardParent);
        string[] panelNames =
            { "TB3P", "FR5P", "ZK1P", "VoiceAssistantP" };

        for (int i = 0; i < panelNames.Length; i++)
        {
            GameObject panel = FindSceneObject(scene, panelNames[i]);

            if (panel == null)
            {
                continue;
            }

            float left = i / 4f;
            float right = (i + 1) / 4f;
            RectTransform rect = panel.GetComponent<RectTransform>();

            SetRect(
                rect,
                new Vector2(left, 0f),
                new Vector2(right, 1f),
                new Vector2(0f, 0f),
                new Vector2(0f, -46f));

            panel.SetActive(true);
            if (panel == voicePanel)
            {
                ConfigureVoiceAssistantCard(scene, panel);
            }
            else
            {
                ConfigureRobotCard(panel);
            }
        }
    }

    private static void ConfigureVoiceAssistantCard(
        Scene scene,
        GameObject panel)
    {
        Image background = GetOrAddComponent<Image>(panel);
        background.color = IndustrialConsoleTheme.Panel;

        Text title = EnsureConsoleText(
            panel.transform,
            "VoiceTitle",
            "AI VOICE ASSISTANT  /  AI 음성 비서",
            12,
            IndustrialConsoleTheme.Accent,
            TextAnchor.MiddleLeft);
        SetRect(title.rectTransform,
            new Vector2(0f, 0.91f), Vector2.one,
            new Vector2(14f, 0f), new Vector2(-14f, 0f));

        Text status = EnsureConsoleText(
            panel.transform,
            "VoiceStatus",
            "●  대기 중",
            14,
            IndustrialConsoleTheme.Dim,
            TextAnchor.MiddleLeft);
        SetRect(status.rectTransform,
            new Vector2(0f, 0.83f), new Vector2(1f, 0.91f),
            new Vector2(14f, 0f), new Vector2(-14f, 0f));

        Text conversationLabel = EnsureConsoleText(
            panel.transform,
            "ConversationLabel",
            "CURRENT DIALOGUE  /  현재 대화",
            9,
            IndustrialConsoleTheme.Dim,
            TextAnchor.MiddleLeft);
        SetRect(conversationLabel.rectTransform,
            new Vector2(0f, 0.76f), new Vector2(1f, 0.83f),
            new Vector2(14f, 0f), new Vector2(-14f, 0f));

        Image conversationBox = EnsureImage(
            panel.transform,
            "ConversationBox",
            IndustrialConsoleTheme.Raised);
        SetRect(conversationBox.rectTransform,
            new Vector2(0f, 0.34f), new Vector2(1f, 0.76f),
            new Vector2(12f, 4f), new Vector2(-12f, -4f));
        AddCrispOutline(conversationBox.gameObject);

        Text conversation = EnsureConsoleText(
            conversationBox.transform,
            "ConversationText",
            "음성 명령을 기다리고 있습니다.",
            12,
            IndustrialConsoleTheme.TextColor,
            TextAnchor.UpperLeft);
        Stretch(conversation.rectTransform,
            new Vector2(12f, 12f), new Vector2(-12f, -12f));
        conversation.fontStyle = FontStyle.Normal;
        conversation.horizontalOverflow = HorizontalWrapMode.Wrap;
        conversation.verticalOverflow = VerticalWrapMode.Truncate;
        conversation.lineSpacing = 1.15f;

        Text recentLabel = EnsureConsoleText(
            panel.transform,
            "RecentLabel",
            "RECENT  /  최근 대화",
            9,
            IndustrialConsoleTheme.Dim,
            TextAnchor.MiddleLeft);
        SetRect(recentLabel.rectTransform,
            new Vector2(0f, 0.27f), new Vector2(1f, 0.34f),
            new Vector2(14f, 0f), new Vector2(-14f, 0f));

        Text recent = EnsureConsoleText(
            panel.transform,
            "RecentTurnsText",
            "아직 기록된 대화가 없습니다.",
            10,
            IndustrialConsoleTheme.TextColor,
            TextAnchor.UpperLeft);
        SetRect(recent.rectTransform,
            new Vector2(0f, 0.06f), new Vector2(1f, 0.27f),
            new Vector2(14f, 4f), new Vector2(-14f, -4f));
        recent.fontStyle = FontStyle.Normal;
        recent.horizontalOverflow = HorizontalWrapMode.Wrap;
        recent.verticalOverflow = VerticalWrapMode.Truncate;
        recent.lineSpacing = 1.15f;

        Text monitorOnly = EnsureConsoleText(
            panel.transform,
            "MonitorOnlyText",
            "MONITOR ONLY  ·  기존 /ws/unity",
            8,
            IndustrialConsoleTheme.Dim,
            TextAnchor.MiddleLeft);
        SetRect(monitorOnly.rectTransform,
            Vector2.zero, new Vector2(1f, 0.06f),
            new Vector2(14f, 0f), new Vector2(-14f, 0f));

        VoiceRuntimePanelUI voiceUi =
            GetOrAddComponent<VoiceRuntimePanelUI>(panel);
        SerializedObject serialized = new SerializedObject(voiceUi);
        serialized.FindProperty("factoryStateManager").objectReferenceValue =
            FindSceneComponent<FactoryStateManager>(scene);
        serialized.FindProperty("statusText").objectReferenceValue = status;
        serialized.FindProperty("conversationText").objectReferenceValue =
            conversation;
        serialized.FindProperty("recentTurnsText").objectReferenceValue = recent;
        serialized.ApplyModifiedPropertiesWithoutUndo();
    }

    private static void NormalizeServerRobotIds(Scene scene)
    {
        foreach (ForkliftPoseApplier applier in
                 FindSceneComponents<ForkliftPoseApplier>(scene))
        {
            SerializedObject serialized = new SerializedObject(applier);
            serialized.FindProperty("robotId").stringValue = "turtlebot_01";
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        foreach (RobotStatusTextUI status in
                 FindSceneComponents<RobotStatusTextUI>(scene))
        {
            SerializedObject serialized = new SerializedObject(status);
            SerializedProperty robotId = serialized.FindProperty("robotId");

            if (IsLegacyTransportRobotId(robotId.stringValue))
            {
                robotId.stringValue = "turtlebot_01";
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }
        }

        foreach (RobotStatusPanelUI panel in
                 FindSceneComponents<RobotStatusPanelUI>(scene))
        {
            SerializedObject serialized = new SerializedObject(panel);
            SerializedProperty bindings = serialized.FindProperty("robotBindings");

            for (int i = 0; i < bindings.arraySize; i++)
            {
                SerializedProperty robotId = bindings
                    .GetArrayElementAtIndex(i)
                    .FindPropertyRelative("robotId");

                if (IsLegacyTransportRobotId(robotId.stringValue))
                {
                    robotId.stringValue = "turtlebot_01";
                }
                else if (robotId.stringValue == "zkbot1")
                {
                    robotId.stringValue = "zkbot2";

                    SerializedProperty displayName = bindings
                        .GetArrayElementAtIndex(i)
                        .FindPropertyRelative("displayName");

                    displayName.stringValue = "ZeKeep 02";
                }
            }

            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        foreach (ZKJointStateApplier applier in
                 FindSceneComponents<ZKJointStateApplier>(scene))
        {
            SerializedObject serialized = new SerializedObject(applier);
            SerializedProperty robotId = serialized.FindProperty("robotId");
            string objectName = applier.gameObject.name;

            bool isFr5 = objectName.IndexOf(
                    "fairino",
                    StringComparison.OrdinalIgnoreCase) >= 0;

            robotId.stringValue = isFr5 ? "fr5" : "zkbot2";

            if (isFr5)
            {
                SerializedProperty bindings =
                    serialized.FindProperty("jointBindings");

                for (int i = 0; i < bindings.arraySize && i < 6; i++)
                {
                    bindings.GetArrayElementAtIndex(i)
                        .FindPropertyRelative("jointName")
                        .stringValue = $"joint{i + 1}";
                }
            }

            serialized.ApplyModifiedPropertiesWithoutUndo();
        }
    }

    private static bool IsLegacyTransportRobotId(string robotId)
    {
        return robotId == "FORKLIFT_01" ||
               robotId == "forklift_01" ||
               robotId == "mobile_robot_1";
    }

    private static void ConfigureRobotCard(GameObject panel)
    {
        RawImage cameraImage =
            panel.GetComponentInChildren<RawImage>(true);

        Text statusText = panel.GetComponentInChildren<Text>(true);

        if (cameraImage == null || statusText == null)
        {
            return;
        }

        Transform viewportTransform = panel.transform.Find(
            "CameraViewport");

        GameObject viewport = viewportTransform != null
            ? viewportTransform.gameObject
            : CreateRectObject("CameraViewport", panel.transform);

        viewport.transform.SetAsFirstSibling();

        SetRect(
            viewport.GetComponent<RectTransform>(),
            new Vector2(0f, 0.42f),
            new Vector2(1f, 1f),
            new Vector2(10f, 6f),
            new Vector2(-10f, -10f));

        cameraImage.transform.SetParent(viewport.transform, false);
        Stretch(cameraImage.rectTransform, Vector2.zero, Vector2.zero);
        cameraImage.raycastTarget = false;

        AspectRatioFitter fitter =
            GetOrAddComponent<AspectRatioFitter>(cameraImage.gameObject);

        fitter.aspectMode = AspectRatioFitter.AspectMode.FitInParent;
        fitter.aspectRatio = cameraImage.texture != null &&
                             cameraImage.texture.height > 0
            ? (float)cameraImage.texture.width /
              cameraImage.texture.height
            : 16f / 9f;

        SetRect(
            statusText.rectTransform,
            new Vector2(0f, 0f),
            new Vector2(1f, 0.42f),
            new Vector2(12f, 10f),
            new Vector2(-12f, -4f));

        statusText.alignment = TextAnchor.UpperLeft;
        statusText.fontSize = 13;
        statusText.resizeTextForBestFit = true;
        statusText.resizeTextMinSize = 10;
        statusText.resizeTextMaxSize = 15;
        statusText.horizontalOverflow = HorizontalWrapMode.Wrap;
        statusText.verticalOverflow = VerticalWrapMode.Truncate;
    }

    private static void ConfigureJobEmptyState(Scene scene)
    {
        JobListUI jobList = FindSceneComponent<JobListUI>(scene);

        if (jobList == null)
        {
            return;
        }

        SerializedObject serialized = new SerializedObject(jobList);
        serialized.FindProperty("maxVisibleJobs").intValue = 5;
        Transform content = serialized.FindProperty("content")
            .objectReferenceValue as Transform;

        Text emptyText = EnsureEmptyText(
            content,
            "JobEmptyStateText",
            "진행 중인 작업이 없습니다.");

        serialized.FindProperty("emptyStateText")
            .objectReferenceValue = emptyText;
        serialized.ApplyModifiedPropertiesWithoutUndo();
    }

    private static void ConfigureDashboardPresentation(Scene scene)
    {
        DashboardStatusUI dashboard =
            FindSceneComponent<DashboardStatusUI>(scene);

        if (dashboard == null)
        {
            return;
        }

        SerializedObject serialized = new SerializedObject(dashboard);

        ConfigureSummaryText(
            serialized.FindProperty("completedJobsText")
                .objectReferenceValue as Text);
        ConfigureSummaryText(
            serialized.FindProperty("currentProductText")
                .objectReferenceValue as Text);
        ConfigureSummaryText(
            serialized.FindProperty("currentProcessText")
                .objectReferenceValue as Text);

        ConfigureDetailText(
            serialized.FindProperty("currentJobText")
                .objectReferenceValue as Text,
            13);
        ConfigureDetailText(
            serialized.FindProperty("robotSummaryText")
                .objectReferenceValue as Text,
            14);
        ConfigureDetailText(
            serialized.FindProperty("recentEventText")
                .objectReferenceValue as Text,
            13);
    }

    private static void ConfigureSummaryText(Text text)
    {
        if (text == null)
        {
            return;
        }

        text.alignment = TextAnchor.MiddleCenter;
        text.fontSize = 16;
        text.resizeTextForBestFit = false;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        text.raycastTarget = false;

        RectTransform rect = text.rectTransform;
        rect.offsetMin = new Vector2(10f, 6f);
        rect.offsetMax = new Vector2(-10f, -6f);

        Image card = text.transform.parent.GetComponent<Image>();

        if (card != null)
        {
            card.color = CardColor;
        }
    }

    private static void ConfigureDetailText(Text text, int fontSize)
    {
        if (text == null)
        {
            return;
        }

        text.alignment = TextAnchor.UpperLeft;
        text.fontSize = fontSize;
        text.resizeTextForBestFit = false;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        text.lineSpacing = 1.05f;
        text.raycastTarget = false;

        RectTransform rect = text.rectTransform;
        rect.offsetMin = new Vector2(10f, 8f);
        // 카드 상단의 별도 제목 Text가 차지하는 영역을 비웁니다.
        rect.offsetMax = new Vector2(-10f, -34f);

        Image card = text.transform.parent.GetComponent<Image>();

        if (card != null)
        {
            card.color = CardColor;
        }
    }

    private static void ConfigureProfessionalTheme(
        Scene scene,
        PageManager pageManager)
    {
        Color32 navy = new Color32(13, 31, 55, 255);
        Color32 primary = new Color32(28, 112, 232, 255);
        Color32 border = new Color32(225, 231, 239, 255);
        Color32 muted = new Color32(91, 104, 121, 255);

        SerializedObject pageManagerObject =
            new SerializedObject(pageManager);
        pageManagerObject.FindProperty("selectedColor").colorValue = primary;
        pageManagerObject.FindProperty("normalColor").colorValue = Color.white;
        pageManagerObject.ApplyModifiedPropertiesWithoutUndo();

        SetImageColor(FindSceneObject(scene, "TopBar"), navy);
        SetImageColor(FindSceneObject(scene, "SideBar"), Color.white);

        string[] pageNames =
        {
            "DashboardP", "RobotP", "TaskP", "InspectionP"
        };

        foreach (string pageName in pageNames)
        {
            SetImageColor(FindSceneObject(scene, pageName), PageColor);
        }

        string[] cardNames =
        {
            "SummaryP", "CurrentTaskP", "RobotSummaryP", "EventLogP",
            "ProcessFlowP", "JobListP", "TB3P", "FR5P", "ZK1P",
            "VoiceAssistantP",
            "IncomingInspectionCard", "AssemblyInspectionCard"
        };

        foreach (string cardName in cardNames)
        {
            foreach (GameObject card in FindSceneObjects(scene, cardName))
            {
                SetImageColor(card, CardColor);
                AddSoftCardShadow(card);
            }
        }

        GameObject summary = FindSceneObject(scene, "SummaryP");

        if (summary != null)
        {
            foreach (Image image in
                     summary.GetComponentsInChildren<Image>(true))
            {
                if (image.gameObject != summary)
                {
                    image.color = CardColor;
                    AddSoftCardShadow(image.gameObject);
                }
            }
        }

        GameObject topBar = FindSceneObject(scene, "TopBar");

        if (topBar != null)
        {
            foreach (Text text in topBar.GetComponentsInChildren<Text>(true))
            {
                text.color = Color.white;
                text.fontStyle = FontStyle.Bold;
            }
        }

        GameObject sideBar = FindSceneObject(scene, "SideBar");

        if (sideBar != null)
        {
            foreach (Button button in
                     sideBar.GetComponentsInChildren<Button>(true))
            {
                button.image.color = Color.white;
                ColorBlock colors = button.colors;
                colors.normalColor = Color.white;
                colors.highlightedColor = new Color32(239, 245, 253, 255);
                colors.pressedColor = new Color32(218, 231, 249, 255);
                colors.selectedColor = primary;
                button.colors = colors;

                Text label = button.GetComponentInChildren<Text>(true);

                if (label != null)
                {
                    label.color = navy;
                    label.fontStyle = FontStyle.Bold;
                }
            }
        }

        foreach (Text text in FindSceneComponents<Text>(scene))
        {
            if (text.transform.IsChildOf(topBar?.transform) ||
                text.transform.IsChildOf(sideBar?.transform))
            {
                continue;
            }

            if (text.color.r < 0.7f && text.color.g < 0.7f &&
                text.color.b < 0.7f)
            {
                text.color = TextColor;
            }

            if (text.name.IndexOf("Subtitle", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                text.color = muted;
            }
        }

        // 얇은 경계색을 카드 주변 기본 색으로 사용합니다. Legacy UI에서
        // 라운드 보더를 추가하지 않고도 웹 대시보드처럼 구획이 보입니다.
        foreach (GameObject resultBox in
                 FindSceneObjects(scene, "DefectResultBox"))
        {
            SetImageColor(resultBox, new Color32(248, 250, 252, 255));
            Outline outline = GetOrAddComponent<Outline>(resultBox);
            outline.effectColor = border;
            outline.effectDistance = new Vector2(1f, -1f);
            outline.useGraphicAlpha = true;
        }
    }

    private static void ConfigureControlTowerLayout(
        Scene scene,
        PageManager manager)
    {
        if (manager == null)
        {
            return;
        }

        Canvas canvas = manager.dashboardPanel != null
            ? manager.dashboardPanel.GetComponentInParent<Canvas>()
            : null;
        if (canvas == null)
        {
            return;
        }

        GameObject topBar = canvas.transform.Find("TopBar")?.gameObject;
        GameObject sideBar = canvas.transform.Find("SideBar")?.gameObject;
        GameObject content = canvas.transform.Find("Content")?.gameObject;
        if (topBar == null || sideBar == null || content == null)
        {
            return;
        }

        SetRect(topBar.GetComponent<RectTransform>(),
            new Vector2(0f, 1f), new Vector2(1f, 1f),
            new Vector2(0f, -64f), Vector2.zero);
        SetImageColor(topBar, IndustrialConsoleTheme.Background);

        Text brand = topBar.GetComponentsInChildren<Text>(true)
            .FirstOrDefault(text => text.name == "Title");
        if (brand != null)
        {
            brand.text = "HARMONY  /  FACTORY";
            brand.fontSize = 18;
            brand.fontStyle = FontStyle.Bold;
            brand.alignment = TextAnchor.MiddleLeft;
            brand.color = IndustrialConsoleTheme.Accent;
            SetRect(brand.rectTransform, Vector2.zero, Vector2.one,
                new Vector2(22f, 4f), new Vector2(-540f, -4f));
        }

        Image topAccent = EnsureImage(topBar.transform,
            "IndustrialTopAccent", IndustrialConsoleTheme.Accent);
        SetRect(topAccent.rectTransform,
            new Vector2(0f, 0f), new Vector2(1f, 0f),
            Vector2.zero, new Vector2(0f, 2f));
        topAccent.transform.SetAsLastSibling();

        // Existing side navigation becomes the compact top mode switcher.
        SetRect(sideBar.GetComponent<RectTransform>(),
            new Vector2(0f, 1f), new Vector2(1f, 1f),
            new Vector2(270f, -59f), new Vector2(-180f, -5f));
        SetImageColor(sideBar, Color.clear);
        Transform buttonGroup = sideBar.transform.Find("Btn");
        if (buttonGroup != null)
        {
            SetRect(buttonGroup.GetComponent<RectTransform>(),
                Vector2.zero, Vector2.one, Vector2.zero, Vector2.zero);
            VerticalLayoutGroup oldVertical =
                buttonGroup.GetComponent<VerticalLayoutGroup>();
            if (oldVertical != null)
                oldVertical.enabled = false;
        }

        Button[] buttons =
        {
            manager.dashboardButton, manager.robotButton,
            manager.taskButton, manager.inspectionButton
        };
        string[] buttonLabels =
        {
            "OVERVIEW", "ROBOTS", "PROCESS", "INSPECTION"
        };
        for (int index = 0; index < buttons.Length; index++)
        {
            Button button = buttons[index];
            if (button == null)
            {
                continue;
            }
            float x0 = index / (float)buttons.Length;
            float x1 = (index + 1) / (float)buttons.Length;
            SetRect(button.GetComponent<RectTransform>(),
                new Vector2(x0, 0f), new Vector2(x1, 1f),
                new Vector2(2f, 4f), new Vector2(-2f, -4f));
            button.image.color = IndustrialConsoleTheme.Panel;
            button.transition = Selectable.Transition.None;
            button.image.canvasRenderer.SetColor(Color.white);
            ColorBlock colors = button.colors;
            colors.normalColor = IndustrialConsoleTheme.Panel;
            colors.highlightedColor = IndustrialConsoleTheme.Raised;
            colors.pressedColor = IndustrialConsoleTheme.Accent;
            colors.selectedColor = IndustrialConsoleTheme.Accent;
            colors.fadeDuration = 0.08f;
            button.colors = colors;
            Text label = button.GetComponentInChildren<Text>(true);
            if (label != null)
            {
                label.text = buttonLabels[index];
                label.fontSize = 10;
                label.fontStyle = FontStyle.Bold;
                label.color = IndustrialConsoleTheme.TextColor;
            }
            AddCrispOutline(button.gameObject);
        }

        Image modePanel = EnsureImage(topBar.transform,
            "ConsoleModeBadge", IndustrialConsoleTheme.Panel);
        SetRect(modePanel.rectTransform,
            new Vector2(1f, 0f), new Vector2(1f, 1f),
            new Vector2(-170f, 11f), new Vector2(-92f, -11f));
        AddCrispOutline(modePanel.gameObject);
        Text modeText = EnsureConsoleText(modePanel.transform,
            "ConsoleModeText", "●  LIVE", 9, IndustrialConsoleTheme.Live,
            TextAnchor.MiddleCenter);
        Stretch(modeText.rectTransform, Vector2.zero, Vector2.zero);

        Text clockText = EnsureConsoleText(topBar.transform,
            "ConsoleClockText", DateTime.Now.ToString("yyyy.MM.dd  HH:mm:ss"),
            8, IndustrialConsoleTheme.Dim, TextAnchor.MiddleRight);
        SetRect(clockText.rectTransform,
            new Vector2(1f, 0f), new Vector2(1f, 1f),
            new Vector2(-90f, 4f), new Vector2(-12f, -4f));

        SerializedObject managerObject = new SerializedObject(manager);
        managerObject.FindProperty("consoleModeText").objectReferenceValue = modeText;
        managerObject.FindProperty("consoleClockText").objectReferenceValue = clockText;
        managerObject.FindProperty("selectedColor").colorValue =
            IndustrialConsoleTheme.Accent;
        managerObject.FindProperty("normalColor").colorValue =
            IndustrialConsoleTheme.Panel;
        managerObject.ApplyModifiedPropertiesWithoutUndo();

        SetRect(content.GetComponent<RectTransform>(),
            Vector2.zero, Vector2.one, Vector2.zero, new Vector2(0f, -64f));
        Image canvasBackground = GetOrAddComponent<Image>(canvas.gameObject);
        canvasBackground.color = IndustrialConsoleTheme.Background;
        canvasBackground.sprite = null;
        canvasBackground.raycastTarget = false;
        topBar.transform.SetAsLastSibling();
        sideBar.transform.SetAsLastSibling();

        ConfigurePageHeader(manager.dashboardPanel,
            "OVERVIEW", "운영 대시보드", "REAL-TIME FACTORY STATUS");
        ConfigurePageHeader(manager.robotPanel,
            "ROBOTS & AI", "로봇 및 음성 비서", "4 ASSETS");
        ConfigurePageHeader(manager.taskPanel,
            "PRODUCTION FLOW", "작업 진행 현황", "LIVE PROCESS");
        ConfigurePageHeader(manager.inspectionPanel,
            "QUALITY INSPECTION", "품질 검사", "UDP CAMERA");

        ConfigureDashboardGrid(scene);
        ConfigureRobotGrid(scene);
        ConfigureTaskGrid(scene);
        ConfigureInspectionGrid(scene);

        string[] cardNames =
        {
            "SummaryP", "CompletedJobsCard", "CurrentProductCard",
            "CurrentProcessCard", "DigitalTwinCard", "CurrentTaskP",
            "RobotSummaryP", "EventLogP", "TB3P", "FR5P", "ZK1P",
            "VoiceAssistantP",
            "ProcessFlowP", "JobListP", "IncomingInspectionCard",
            "AssemblyInspectionCard", "DefectResultBox"
        };
        foreach (string cardName in cardNames)
        {
            foreach (GameObject card in FindSceneObjects(scene, cardName))
            {
                SetImageColor(card, cardName == "DefectResultBox"
                    ? IndustrialConsoleTheme.Raised
                    : IndustrialConsoleTheme.Panel);
                AddCrispOutline(card);
                if (cardName != "SummaryP" && cardName != "DefectResultBox")
                    AddAccentRail(card);
            }
        }

        foreach (Shadow shadow in FindSceneComponents<Shadow>(scene))
        {
            if (shadow.GetType() == typeof(Shadow))
                shadow.enabled = false;
        }
        IndustrialConsoleTheme.Apply(canvas);
    }

    private static void ConfigureFR5PlaneAndRackVisuals(Scene scene)
    {
        GameObject planeObject = FindSceneObject(scene, "Plane");

        if (planeObject == null)
        {
            Debug.LogWarning(
                "[FactoryVisual] Plane 오브젝트를 찾지 못했습니다.");
            return;
        }

        const string materialFolder = "Assets/Materials/DigitalTwin";
        EnsureAssetFolder("Assets/Materials", "DigitalTwin");

        Material fr5PlatformMaterial = CreateOrUpdateFactoryMaterial(
            materialFolder + "/FR5_Workcell_Aluminum.mat",
            new Color32(176, 184, 192, 255),
            0.38f,
            0.58f);
        Material fr5GrooveMaterial = CreateOrUpdateFactoryMaterial(
            materialFolder + "/FR5_Workcell_Groove.mat",
            new Color32(68, 76, 84, 255),
            0.48f,
            0.48f);
        Material rackWhiteMaterial = CreateOrUpdateFactoryMaterial(
            materialFolder + "/Rack_Matte_White.mat",
            new Color32(238, 241, 244, 255),
            0.04f,
            0.2f);
        Material originalWhite = AssetDatabase.LoadAssetAtPath<Material>(
            "Assets/Materials/FactoryWhite.mat");

        Transform plane = planeObject.transform;
        Transform fr5Plane = plane.Find("FRPlane");
        Transform desk = plane.Find("Desk");
        Transform desk1 = plane.Find("Desk (1)");
        Transform desk2 = plane.Find("Desk (2)");
        Transform table = plane.Find("table");

        SetFactorySurfaceMaterial(fr5Plane, fr5PlatformMaterial);

        // 직전 전체 디지털 트윈 스타일이 바꿨던 나머지 받침대는
        // 사용자가 구성한 원래 흰색 재질로 돌린다.
        SetFactorySurfaceMaterial(desk, originalWhite);
        SetFactorySurfaceMaterial(desk1, originalWhite);
        SetFactorySurfaceMaterial(desk2, originalWhite);
        SetFactorySurfaceMaterial(table, originalWhite);

        Transform visualRoot = plane.Find("DigitalTwinVisuals");

        if (visualRoot != null)
        {
            UnityEngine.Object.DestroyImmediate(visualRoot.gameObject);
        }

        if (fr5Plane != null)
        {
            Transform detailRoot = fr5Plane.Find("FR5PlatformDetails");

            if (detailRoot == null)
            {
                detailRoot = new GameObject("FR5PlatformDetails").transform;
                detailRoot.SetParent(fr5Plane, false);
            }

            // 실제 FR5 작업대와 같은 방향으로 평행 결을 90도 회전한다.
            detailRoot.localPosition = Vector3.zero;
            detailRoot.localRotation = Quaternion.Euler(0f, 90f, 0f);
            detailRoot.localScale = Vector3.one;

            for (int i = detailRoot.childCount - 1; i >= 0; i--)
            {
                UnityEngine.Object.DestroyImmediate(
                    detailRoot.GetChild(i).gameObject);
            }

            // 실제 FR5 알루미늄 프로파일 작업대처럼 얇은 평행 홈만 추가한다.
            const int grooveCount = 11;

            for (int i = 0; i < grooveCount; i++)
            {
                float z = Mathf.Lerp(-0.43f, 0.43f,
                    i / (float)(grooveCount - 1));
                CreateFactoryDetailCube(
                    detailRoot,
                    "Groove_" + (i + 1).ToString("00"),
                    new Vector3(0f, 0.5015f, z),
                    new Vector3(0.965f, 0.002f, 0.008f),
                    fr5GrooveMaterial);
            }

            EditorUtility.SetDirty(detailRoot.gameObject);
        }

        Transform rackRoot = FindSceneComponents<Transform>(scene)
            .FirstOrDefault(candidate =>
                candidate.name == "Rack" && candidate.childCount > 0);

        if (rackRoot != null)
        {
            foreach (MeshRenderer rackRenderer in
                     rackRoot.GetComponentsInChildren<MeshRenderer>(true))
            {
                rackRenderer.sharedMaterial = rackWhiteMaterial;
                EditorUtility.SetDirty(rackRenderer);
            }
        }

        Camera digitalTwinCamera = FindSceneObject(
            scene,
            "DigitalTwinCamera")?.GetComponent<Camera>();

        if (digitalTwinCamera != null)
        {
            digitalTwinCamera.clearFlags = CameraClearFlags.Skybox;
            EditorUtility.SetDirty(digitalTwinCamera);
        }

        string[] obsoleteMaterials =
        {
            materialFolder + "/DT_AssemblyTable.mat",
            materialFolder + "/DT_CyanRail.mat",
            materialFolder + "/DT_FactoryFloor.mat",
            materialFolder + "/DT_FR5Cell.mat",
            materialFolder + "/DT_GridLine.mat",
            materialFolder + "/DT_SafetyRail.mat",
            materialFolder + "/DT_StationPlatform.mat"
        };

        foreach (string obsoleteMaterial in obsoleteMaterials)
        {
            AssetDatabase.DeleteAsset(obsoleteMaterial);
        }
    }

    private static void EnsureAssetFolder(
        string parentFolder,
        string childFolder)
    {
        string path = parentFolder + "/" + childFolder;

        if (!AssetDatabase.IsValidFolder(path))
        {
            AssetDatabase.CreateFolder(parentFolder, childFolder);
        }
    }

    private static Material CreateOrUpdateFactoryMaterial(
        string assetPath,
        Color baseColor,
        float metallic,
        float smoothness)
    {
        Material material = AssetDatabase.LoadAssetAtPath<Material>(assetPath);
        Shader shader = Shader.Find("Universal Render Pipeline/Lit") ??
                        Shader.Find("Standard");

        if (material == null)
        {
            material = new Material(shader);
            AssetDatabase.CreateAsset(material, assetPath);
        }
        else if (shader != null && material.shader != shader)
        {
            material.shader = shader;
        }

        if (material.HasProperty("_BaseColor"))
        {
            material.SetColor("_BaseColor", baseColor);
        }

        if (material.HasProperty("_Color"))
        {
            material.SetColor("_Color", baseColor);
        }

        if (material.HasProperty("_Metallic"))
        {
            material.SetFloat("_Metallic", metallic);
        }

        if (material.HasProperty("_Smoothness"))
        {
            material.SetFloat("_Smoothness", smoothness);
        }

        EditorUtility.SetDirty(material);
        return material;
    }

    private static void SetFactorySurfaceMaterial(
        Transform surface,
        Material material)
    {
        if (surface == null || material == null)
        {
            return;
        }

        MeshRenderer renderer = surface.GetComponent<MeshRenderer>();

        if (renderer != null)
        {
            renderer.sharedMaterial = material;
            EditorUtility.SetDirty(renderer);
        }
    }

    private static GameObject CreateFactoryDetailCube(
        Transform parent,
        string objectName,
        Vector3 localPosition,
        Vector3 localScale,
        Material material)
    {
        GameObject cube = GameObject.CreatePrimitive(PrimitiveType.Cube);
        cube.name = objectName;
        cube.transform.SetParent(parent, false);
        cube.transform.localPosition = localPosition;
        cube.transform.localRotation = Quaternion.identity;
        cube.transform.localScale = localScale;

        Collider collider = cube.GetComponent<Collider>();

        if (collider != null)
        {
            UnityEngine.Object.DestroyImmediate(collider);
        }

        MeshRenderer renderer = cube.GetComponent<MeshRenderer>();

        if (renderer != null)
        {
            renderer.sharedMaterial = material;
            renderer.shadowCastingMode =
                UnityEngine.Rendering.ShadowCastingMode.Off;
            renderer.receiveShadows = false;
        }

        return cube;
    }

    private static void ConfigureDashboardGrid(Scene scene)
    {
        GameObject summary = FindSceneObject(scene, "SummaryP");
        GameObject main = FindSceneObject(scene, "MainArea");
        GameObject digital = FindSceneObject(scene, "DigitalTwinCard");
        GameObject right = FindSceneObject(scene, "RightColumn");
        RawImage globalCamera = ConfigureDashboardGlobalCamera(scene, main);
        if (summary != null)
        {
            SetRect(summary.GetComponent<RectTransform>(),
                new Vector2(0f, 1f), Vector2.one,
                new Vector2(0f, -124f), new Vector2(0f, -46f));
            foreach (Text text in summary.GetComponentsInChildren<Text>(true))
            {
                if (text.transform.parent != summary.transform)
                    text.fontSize = Mathf.Min(text.fontSize, 13);
            }
        }
        Transform summaryRow = summary?.transform.Find("SummaryRow");
        if (summaryRow != null)
        {
            Stretch(summaryRow.GetComponent<RectTransform>(),
                Vector2.zero, Vector2.zero);
            LayoutGroup summaryLayout = summaryRow.GetComponent<LayoutGroup>();
            if (summaryLayout != null) summaryLayout.enabled = false;
            Image[] cards = summaryRow.GetComponentsInChildren<Image>(true)
                .Where(image => image.transform.parent == summaryRow).ToArray();
            for (int i = 0; i < cards.Length; i++)
                SetRect(cards[i].rectTransform,
                    new Vector2(i / 3f, 0f), new Vector2((i + 1) / 3f, 1f),
                    Vector2.zero, Vector2.zero);
        }
        Text legacyTitle = summary?.GetComponentsInChildren<Text>(true)
            .FirstOrDefault(text => text.transform.parent == summary.transform);
        if (legacyTitle != null) legacyTitle.gameObject.SetActive(false);
        if (main != null)
            SetRect(main.GetComponent<RectTransform>(), Vector2.zero, Vector2.one,
                Vector2.zero, new Vector2(0f, -124f));
        if (digital != null)
        {
            SetRect(digital.GetComponent<RectTransform>(),
                Vector2.zero, new Vector2(0.40f, 1f),
                Vector2.zero, Vector2.zero);
            ConfigureDigitalTwinViewport(digital.transform);
        }
        if (right != null)
        {
            SetRect(right.GetComponent<RectTransform>(),
                new Vector2(0.80f, 0f), Vector2.one,
                Vector2.zero, new Vector2(0f, -124f));
            ConfigureDashboardSection(right.transform.Find("CurrentTaskP"),
                0.75f, 1f, "CURRENT JOB  /  현재 작업");
            ConfigureDashboardSection(right.transform.Find("RobotSummaryP"),
                0.42f, 0.75f, "ROBOT STATUS  /  로봇 상태");
            ConfigureDashboardSection(right.transform.Find("EventLogP"),
                0f, 0.42f, "RECENT EVENT  /  최근 이벤트");
        }

        ConnectDashboardGlobalCamera(scene, globalCamera);
    }

    private static RawImage ConfigureDashboardGlobalCamera(
        Scene scene,
        GameObject main)
    {
        if (main == null) return null;

        GameObject card = FindSceneObject(scene, "GlobalCameraCard");
        if (card == null)
            card = CreateRectObject("GlobalCameraCard", main.transform);
        else if (card.transform.parent != main.transform)
            card.transform.SetParent(main.transform, false);

        Image cardImage = GetOrAddComponent<Image>(card);
        cardImage.color = IndustrialConsoleTheme.Background;
        cardImage.sprite = null;
        cardImage.raycastTarget = false;
        SetRect(card.GetComponent<RectTransform>(),
            new Vector2(0.40f, 0f), new Vector2(0.80f, 1f),
            Vector2.zero, Vector2.zero);

        Text title = EnsureConsoleText(card.transform, "TitleText",
            "GLOBAL CAMERA  /  글로벌 카메라", 11,
            IndustrialConsoleTheme.Accent, TextAnchor.MiddleLeft);
        SetRect(title.rectTransform,
            new Vector2(0f, 1f), new Vector2(1f, 1f),
            new Vector2(12f, -34f), new Vector2(-12f, -4f));

        Transform existingViewport = card.transform.Find("GlobalCameraViewport");
        GameObject viewport = existingViewport != null
            ? existingViewport.gameObject
            : CreateRectObject("GlobalCameraViewport", card.transform);
        AspectRatioFitter legacyFitter = viewport.GetComponent<AspectRatioFitter>();
        if (legacyFitter != null)
            UnityEngine.Object.DestroyImmediate(legacyFitter);
        RawImage legacyImage = viewport.GetComponent<RawImage>();
        if (legacyImage != null)
            UnityEngine.Object.DestroyImmediate(legacyImage);
        Image viewportBackground = GetOrAddComponent<Image>(viewport);
        viewportBackground.color = IndustrialConsoleTheme.Background;
        viewportBackground.sprite = null;
        viewportBackground.raycastTarget = false;
        SetRect(viewport.GetComponent<RectTransform>(), Vector2.zero, Vector2.one,
            new Vector2(8f, 8f), new Vector2(-8f, -38f));

        Transform existingImage = viewport.transform.Find("GlobalCameraImage");
        GameObject imageObject = existingImage != null
            ? existingImage.gameObject
            : CreateRectObject("GlobalCameraImage", viewport.transform);
        RawImage image = GetOrAddComponent<RawImage>(imageObject);
        image.color = IndustrialConsoleTheme.Background;
        image.raycastTarget = false;
        Stretch(image.rectTransform, Vector2.zero, Vector2.zero);
        AspectRatioFitter fitter = GetOrAddComponent<AspectRatioFitter>(imageObject);
        fitter.aspectMode = AspectRatioFitter.AspectMode.FitInParent;
        fitter.aspectRatio = 4f / 3f;

        Text placeholder = EnsureConsoleText(imageObject.transform,
            "PlaceholderText", "글로벌 카메라 신호 대기\nUDP 21030", 13,
            IndustrialConsoleTheme.Dim, TextAnchor.MiddleCenter);
        placeholder.fontStyle = FontStyle.Normal;
        Stretch(placeholder.rectTransform, Vector2.zero, Vector2.zero);

        AddCrispOutline(card);
        AddAccentRail(card);
        return image;
    }

    private static void ConnectDashboardGlobalCamera(
        Scene scene,
        RawImage globalCamera)
    {
        if (globalCamera == null) return;
        Text placeholder = globalCamera.transform.Find("PlaceholderText")
            ?.GetComponent<Text>();

        foreach (VisionUdpVideoReceiver receiver in
                 FindSceneComponents<VisionUdpVideoReceiver>(scene))
        {
            SerializedObject serialized = new SerializedObject(receiver);
            if (serialized.FindProperty("expectedStreamId").intValue != 3)
                continue;
            serialized.FindProperty("dashboardMirrorImage")
                .objectReferenceValue = globalCamera;
            serialized.FindProperty("dashboardMirrorPlaceholder")
                .objectReferenceValue = placeholder;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }
    }

    private static void ConfigureDigitalTwinViewport(Transform digitalCard)
    {
        Text title = digitalCard.Find("TitleText")?.GetComponent<Text>();
        if (title != null)
        {
            title.fontSize = 11;
            title.fontStyle = FontStyle.Bold;
            title.color = IndustrialConsoleTheme.Accent;
            title.alignment = TextAnchor.MiddleLeft;
            SetRect(title.rectTransform,
                new Vector2(0f, 1f), new Vector2(1f, 1f),
                new Vector2(12f, -34f), new Vector2(-12f, -4f));
        }

        RectTransform viewport = digitalCard.Find("Viewport")
            ?.GetComponent<RectTransform>();
        if (viewport == null) return;

        SetRect(viewport, Vector2.zero, Vector2.one,
            new Vector2(8f, 8f), new Vector2(-8f, -38f));

        RawImage rawImage = viewport.GetComponentInChildren<RawImage>(true);
        if (rawImage == null) return;

        Stretch(rawImage.rectTransform, Vector2.zero, Vector2.zero);
        rawImage.color = Color.white;
        rawImage.raycastTarget = false;
        AspectRatioFitter fitter = GetOrAddComponent<AspectRatioFitter>(
            rawImage.gameObject);
        fitter.aspectMode = AspectRatioFitter.AspectMode.FitInParent;
        // Factory View가 1280x960이므로 두 모니터의 실제 표시 영역도
        // 같은 4:3 크기로 고정합니다.
        fitter.aspectRatio = 4f / 3f;
    }

    private static void ConfigureDashboardSection(
        Transform section,
        float yMin,
        float yMax,
        string titleValue)
    {
        if (section == null) return;
        SetRect(section.GetComponent<RectTransform>(),
            new Vector2(0f, yMin), new Vector2(1f, yMax),
            Vector2.zero, Vector2.zero);
        Text title = section.GetComponentsInChildren<Text>(true)
            .FirstOrDefault(text => text.transform.parent == section);
        if (title != null)
        {
            title.gameObject.SetActive(true);
            title.text = titleValue;
            title.fontSize = 10;
            title.fontStyle = FontStyle.Bold;
            title.color = IndustrialConsoleTheme.Accent;
            title.alignment = TextAnchor.MiddleLeft;
            SetRect(title.rectTransform,
                new Vector2(0f, 1f), new Vector2(1f, 1f),
                new Vector2(10f, -34f), new Vector2(-10f, -4f));
        }
    }

    private static void ConfigureRobotGrid(Scene scene)
    {
        GameObject oldTitle = FindSceneObject(scene, "RobotT");
        if (oldTitle != null) oldTitle.SetActive(false);
        string[] names = { "TB3P", "FR5P", "ZK1P", "VoiceAssistantP" };
        for (int i = 0; i < names.Length; i++)
        {
            GameObject card = FindSceneObject(scene, names[i]);
            if (card == null) continue;
            SetRect(card.GetComponent<RectTransform>(),
                new Vector2(i / 4f, 0f), new Vector2((i + 1) / 4f, 1f),
                Vector2.zero, new Vector2(0f, -46f));
        }
        GameObject legacy = FindSceneObject(scene, "ZK2P");
        if (legacy != null) legacy.SetActive(false);
    }

    private static void ConfigureTaskGrid(Scene scene)
    {
        GameObject oldTitle = FindSceneObject(scene, "Title");
        if (oldTitle != null && oldTitle.transform.parent != null &&
            oldTitle.transform.parent.name == "TaskP")
            oldTitle.SetActive(false);
        GameObject flow = FindSceneObject(scene, "ProcessFlowP");
        GameObject jobs = FindSceneObject(scene, "JobListP");
        GameObject events = FindSceneObjects(scene, "EventLogP")
            .FirstOrDefault(item => item.transform.parent != null &&
                item.transform.parent.name == "TaskP");
        if (flow != null)
        {
            SetRect(flow.GetComponent<RectTransform>(),
                new Vector2(0f, 1f), Vector2.one,
                new Vector2(0f, -194f), new Vector2(0f, -46f));
            ConfigureCompactProcessFlow(flow.transform);
        }
        if (jobs != null)
            SetRect(jobs.GetComponent<RectTransform>(),
                Vector2.zero, new Vector2(0.54f, 1f),
                Vector2.zero, new Vector2(0f, -194f));
        if (events != null)
            SetRect(events.GetComponent<RectTransform>(),
                new Vector2(0.54f, 0f), Vector2.one,
                Vector2.zero, new Vector2(0f, -194f));
        ConfigureTaskSectionTitle(jobs, "ACTIVE JOBS  /  작업 목록");
        ConfigureTaskSectionTitle(events, "EVENT LOG  /  이벤트 로그");
    }

    private static void ConfigureCompactProcessFlow(Transform flow)
    {
        Text header = flow.GetComponentsInChildren<Text>(true)
            .FirstOrDefault(text => text.transform.parent == flow);
        if (header != null)
        {
            header.fontSize = 11;
            header.fontStyle = FontStyle.Bold;
            header.color = IndustrialConsoleTheme.Accent;
            header.alignment = TextAnchor.MiddleLeft;
            header.horizontalOverflow = HorizontalWrapMode.Wrap;
            header.verticalOverflow = VerticalWrapMode.Truncate;
            SetRect(header.rectTransform,
                new Vector2(0f, 1f), new Vector2(1f, 1f),
                new Vector2(12f, -30f), new Vector2(-12f, -4f));
        }

        Transform firstRow = flow.Find("ProcessRow1");
        Transform secondRow = flow.Find("ProcessRow2");
        ConfigureCompactProcessRow(firstRow, 0.45f, 1f, 27f, 0f);
        ConfigureCompactProcessRow(secondRow, 0f, 0.45f, 1f, 4f);
    }

    private static void ConfigureCompactProcessRow(
        Transform row,
        float yMin,
        float yMax,
        float topInset,
        float bottomInset)
    {
        if (row == null) return;
        HorizontalLayoutGroup legacyLayout = row.GetComponent<HorizontalLayoutGroup>();
        if (legacyLayout != null)
            legacyLayout.enabled = false;
        SetRect(row.GetComponent<RectTransform>(),
            new Vector2(0f, yMin), new Vector2(1f, yMax),
            new Vector2(10f, bottomInset),
            new Vector2(-10f, -topInset));

        ProcessStepUI[] steps = row.GetComponentsInChildren<ProcessStepUI>(true)
            .Where(step => step.transform.parent == row)
            .ToArray();
        for (int i = 0; i < steps.Length; i++)
        {
            float xMin = (float)i / steps.Length;
            float xMax = (float)(i + 1) / steps.Length;
            SetRect(steps[i].GetComponent<RectTransform>(),
                new Vector2(xMin, 0f), new Vector2(xMax, 1f),
                new Vector2(2f, 0f), new Vector2(-2f, 0f));

            Transform circle = steps[i].transform.Find("Circle");
            if (circle != null)
            {
                var stepImage = circle.GetComponent<Image>();
                if (stepImage != null)
                {
                    stepImage.color = IndustrialConsoleTheme.Edge;
                    stepImage.sprite = null;
                    stepImage.type = Image.Type.Simple;
                }
                SetRect(circle.GetComponent<RectTransform>(),
                    new Vector2(0.5f, 1f), new Vector2(0.5f, 1f),
                    new Vector2(-14f, -32f), new Vector2(14f, -4f));

                Text number = circle.Find("NumText")?.GetComponent<Text>();
                if (number != null)
                {
                    number.fontSize = 12;
                    number.color = IndustrialConsoleTheme.TextColor;
                    number.alignment = TextAnchor.MiddleCenter;
                    Stretch(number.rectTransform, Vector2.zero, Vector2.zero);
                }
            }

            Text label = steps[i].transform.Find("LabelText")?.GetComponent<Text>();
            if (label != null)
            {
                label.gameObject.SetActive(false);
                Text compactLabel = EnsureConsoleText(steps[i].transform,
                    "CompactStepLabel", label.text, 9,
                    IndustrialConsoleTheme.TextColor,
                    TextAnchor.MiddleCenter);
                compactLabel.fontStyle = FontStyle.Normal;
                compactLabel.horizontalOverflow = HorizontalWrapMode.Wrap;
                compactLabel.verticalOverflow = VerticalWrapMode.Truncate;
                compactLabel.resizeTextForBestFit = true;
                compactLabel.resizeTextMinSize = 7;
                compactLabel.resizeTextMaxSize = 9;
                compactLabel.maskable = false;
                compactLabel.raycastTarget = false;
                SetRect(compactLabel.rectTransform,
                    new Vector2(0f, 0f), new Vector2(1f, 0.42f),
                    new Vector2(2f, 0f), new Vector2(-2f, 0f));
                compactLabel.transform.SetAsLastSibling();
            }

            Transform connector = steps[i].transform.Find("ConnectorLine");
            if (connector != null)
            {
                var lineImage = connector.GetComponent<Image>();
                if (lineImage != null) lineImage.color = IndustrialConsoleTheme.Edge;
                SetRect(connector.GetComponent<RectTransform>(),
                    new Vector2(0.5f, 1f), new Vector2(1.5f, 1f),
                    new Vector2(14f, -20f), new Vector2(-14f, -17f));
                connector.gameObject.SetActive(i < steps.Length - 1);
            }
        }
    }

    private static void ConfigureTaskSectionTitle(
        GameObject panel,
        string value)
    {
        if (panel == null) return;
        Text title = panel.GetComponentsInChildren<Text>(true)
            .FirstOrDefault(text => text.transform.parent == panel.transform);
        if (title == null) return;
        title.gameObject.SetActive(true);
        title.text = value;
        title.fontSize = 11;
        title.fontStyle = FontStyle.Bold;
        title.alignment = TextAnchor.MiddleLeft;
        title.color = IndustrialConsoleTheme.Accent;
        SetRect(title.rectTransform,
            new Vector2(0f, 1f), new Vector2(1f, 1f),
            new Vector2(12f, -38f), new Vector2(-12f, -5f));
    }

    private static void ConfigureInspectionGrid(Scene scene)
    {
        GameObject incoming = FindSceneObject(scene, "IncomingInspectionCard");
        GameObject assembly = FindSceneObject(scene, "AssemblyInspectionCard");
        if (incoming != null)
            SetRect(incoming.GetComponent<RectTransform>(),
                Vector2.zero, new Vector2(0.5f, 1f),
                Vector2.zero, new Vector2(0f, -46f));
        if (assembly != null)
        {
            assembly.SetActive(true);
            SetRect(assembly.GetComponent<RectTransform>(),
                new Vector2(0.5f, 0f), Vector2.one,
                Vector2.zero, new Vector2(0f, -46f));

            Transform labelTransform =
                assembly.transform.Find("DefectLabelText");
            if (labelTransform != null &&
                labelTransform.TryGetComponent(out Text label))
            {
                label.text = "VIEW 검사 상태";
            }
        }
    }

    private static void ConfigurePageHeader(
        GameObject page,
        string code,
        string title,
        string context)
    {
        if (page == null) return;
        Transform existing = page.transform.Find("IndustrialPageHeader");
        GameObject header = existing != null ? existing.gameObject :
            CreateRectObject("IndustrialPageHeader", page.transform);
        Image background = GetOrAddComponent<Image>(header);
        background.color = IndustrialConsoleTheme.Background;
        SetRect(header.GetComponent<RectTransform>(),
            new Vector2(0f, 1f), Vector2.one,
            Vector2.zero, new Vector2(0f, 0f));
        SetRect(header.GetComponent<RectTransform>(),
            new Vector2(0f, 1f), new Vector2(1f, 1f),
            new Vector2(0f, -46f), Vector2.zero);
        Text codeText = EnsureConsoleText(header.transform, "PageCode",
            code, 10, IndustrialConsoleTheme.Accent, TextAnchor.MiddleLeft);
        SetRect(codeText.rectTransform, Vector2.zero, Vector2.one,
            new Vector2(12f, 0f), new Vector2(-570f, 0f));
        Text titleText = EnsureConsoleText(header.transform, "PageTitle",
            title, 15, IndustrialConsoleTheme.TextColor, TextAnchor.MiddleLeft);
        SetRect(titleText.rectTransform, Vector2.zero, Vector2.one,
            new Vector2(150f, 0f), new Vector2(-330f, 0f));
        Text contextText = EnsureConsoleText(header.transform, "PageContext",
            context, 8, IndustrialConsoleTheme.Dim, TextAnchor.MiddleRight);
        SetRect(contextText.rectTransform, Vector2.zero, Vector2.one,
            new Vector2(480f, 0f), new Vector2(-12f, 0f));
        Image line = EnsureImage(header.transform, "HeaderRule",
            IndustrialConsoleTheme.Edge);
        SetRect(line.rectTransform, new Vector2(0f, 0f), new Vector2(1f, 0f),
            Vector2.zero, new Vector2(0f, 1f));
        header.transform.SetAsLastSibling();
    }

    private static Image EnsureImage(Transform parent, string name, Color color)
    {
        Transform existing = parent.Find(name);
        GameObject go = existing != null ? existing.gameObject :
            CreateRectObject(name, parent);
        Image image = GetOrAddComponent<Image>(go);
        image.color = color;
        image.raycastTarget = false;
        return image;
    }

    private static Text EnsureConsoleText(
        Transform parent, string name, string value, int size,
        Color color, TextAnchor alignment)
    {
        Transform existing = parent.Find(name);
        Text text = existing != null ? existing.GetComponent<Text>() : null;
        if (text == null)
            text = CreateText(name, parent, value, size,
                FontStyle.Bold, alignment);
        text.text = value;
        text.fontSize = size;
        text.fontStyle = FontStyle.Bold;
        text.alignment = alignment;
        text.color = color;
        text.raycastTarget = false;
        return text;
    }

    private static void AddAccentRail(GameObject card)
    {
        Image rail = EnsureImage(card.transform,
            "IndustrialAccentRail", IndustrialConsoleTheme.Accent);
        SetRect(rail.rectTransform,
            new Vector2(0f, 1f), new Vector2(1f, 1f),
            new Vector2(0f, -1f), Vector2.zero);
        rail.transform.SetAsLastSibling();
    }

    private static void AddCrispOutline(GameObject target)
    {
        if (target == null || target.GetComponent<Graphic>() == null) return;
        Outline outline = GetOrAddComponent<Outline>(target);
        outline.effectColor = IndustrialConsoleTheme.Edge;
        outline.effectDistance = new Vector2(1f, -1f);
        outline.useGraphicAlpha = true;
    }

    private static void SetImageColor(GameObject target, Color color)
    {
        if (target != null && target.TryGetComponent(out Image image))
        {
            image.color = color;
        }
    }

    private static void AddSoftCardShadow(GameObject target)
    {
        if (target == null || target.GetComponent<Graphic>() == null)
        {
            return;
        }

        Shadow shadow = GetOrAddComponent<Shadow>(target);
        shadow.effectColor = new Color(0.05f, 0.12f, 0.22f, 0.10f);
        shadow.effectDistance = new Vector2(0f, -2f);
        shadow.useGraphicAlpha = true;
    }

    private static void ConfigureProductionProcessHeader(Scene scene)
    {
        ProductionProcessUI processUI =
            FindSceneComponent<ProductionProcessUI>(scene);

        if (processUI == null)
        {
            return;
        }

        SerializedObject serialized = new SerializedObject(processUI);
        Transform processPanel = serialized.FindProperty("processFlowPanel")
            .objectReferenceValue as Transform;

        if (processPanel == null)
        {
            return;
        }

        Transform visibleTitle = processPanel.parent != null
            ? processPanel.parent.Find("Title")
            : null;

        Text header = visibleTitle != null
            ? visibleTitle.GetComponent<Text>()
            : processPanel
                .GetComponentsInChildren<Text>(true)
                .FirstOrDefault(text => text.transform.parent == processPanel);

        if (header == null)
        {
            return;
        }

        header.text = "작업 프로세스";
        serialized.FindProperty("processHeaderText")
            .objectReferenceValue = header;
        serialized.ApplyModifiedPropertiesWithoutUndo();
    }

    private static void DisableLegacyTaskStatus(Scene scene)
    {
        TaskStatusUI legacyStatus =
            FindSceneComponent<TaskStatusUI>(scene);

        if (legacyStatus == null)
        {
            return;
        }

        SerializedObject serialized =
            new SerializedObject(legacyStatus);

        Text legacyText = serialized.FindProperty("processStatusText")
            .objectReferenceValue as Text;

        if (legacyText != null)
        {
            legacyText.gameObject.SetActive(false);
        }

        legacyStatus.enabled = false;
        EditorUtility.SetDirty(legacyStatus);
    }

    private static void ConfigureEventLog(Scene scene)
    {
        EventLogPanelUI[] controllers =
            FindSceneComponents<EventLogPanelUI>(scene);

        EventLogPanelUI manager = controllers.FirstOrDefault(
            item => item.gameObject.name == "EventLogManager");

        if (manager == null)
        {
            manager = controllers.FirstOrDefault();
        }

        foreach (EventLogPanelUI controller in controllers)
        {
            if (controller != manager)
            {
                UnityEngine.Object.DestroyImmediate(controller);
            }
        }

        if (manager == null)
        {
            return;
        }

        SerializedObject serialized = new SerializedObject(manager);
        FactoryStateManager factoryStateManager =
            FindSceneComponent<FactoryStateManager>(scene);

        serialized.FindProperty("factoryStateManager")
            .objectReferenceValue = factoryStateManager;
        Transform content = serialized.FindProperty("content")
            .objectReferenceValue as Transform;

        Text emptyText = EnsureEmptyText(
            content,
            "EventEmptyStateText",
            "표시할 이벤트가 없습니다.");

        serialized.FindProperty("emptyStateText")
            .objectReferenceValue = emptyText;
        serialized.FindProperty("loadMockEventsOnStart")
            .boolValue = false;
        serialized.ApplyModifiedPropertiesWithoutUndo();
    }

    private static void ConfigureMaterialFlow(Scene scene)
    {
        GameObject materialObject = FindSceneObject(scene, "material");
        GameObject forkliftObject = FindSceneObject(scene, "ForkliftRoot");
        GameObject fr5PalmObject = FindSceneObject(scene, "palm");
        GameObject zkSuctionObject = FindSceneObject(scene, "suction_cup");

        if (materialObject == null ||
            forkliftObject == null ||
            fr5PalmObject == null ||
            zkSuctionObject == null)
        {
            Debug.LogWarning(
                "[InspectionPageBuilder] 자재 또는 로봇 기준점을 찾지 못해 " +
                "접촉 운반 설정을 건너뜁니다.");
            return;
        }

        Transform material = materialObject.transform;
        CarryableObject innerPallet = ConfigureCarryable(
            FindDescendant(material, "pallet_in"),
            CarryableType.Pallet,
            "pallet_in");

        CarryableObject outerPallet = ConfigureCarryable(
            FindDescendant(material, "pallet_out"),
            CarryableType.Pallet,
            "pallet_out");

        CarryableObject baseA = ConfigureCarryable(
            FindDescendant(material, "A_base"),
            CarryableType.Base,
            "A_base");

        CarryableObject baseB = ConfigureCarryable(
            FindDescendant(material, "B_base"),
            CarryableType.Base,
            "B_base");

        CarryableObject fork = ConfigureCarryable(
            FindDescendant(material, "fork"),
            CarryableType.Tool,
            "fork");

        string[] wallNames =
        {
            "A_indoorwall1",
            "A_indoorwall2",
            "B_indoorwall",
            "rightwall",
            "leftwall",
            "doorwall",
            "backwall"
        };

        foreach (string wallName in wallNames)
        {
            ConfigureCarryable(
                FindDescendant(material, wallName),
                CarryableType.Wall,
                wallName);
        }

        RobotCargoMount palletMount = EnsureContactMount(
            forkliftObject.transform,
            "PalletContactMount",
            RobotCarrierRole.Forklift,
            CarryableType.Pallet,
            new Vector3(0.8f, 0.35f, 0.8f));

        RobotCargoMount fr5Mount = EnsureContactMount(
            fr5PalmObject.transform,
            "FR5ContactMount",
            RobotCarrierRole.FR5,
            CarryableType.Wall,
            new Vector3(0.16f, 0.16f, 0.16f),
            false,
            false);

        // The completed-house fork must never be collected by the same
        // contact mount that handles wall/base placement.  The material
        // mount releases cargo whenever FR5 reaches the assembly point, so
        // accepting Tool here could make a nearby fork follow the arm down.
        // Keep a separate, explicitly controlled mount for the fork instead.
        RobotCargoMount fr5ToolMount = EnsureContactMount(
            fr5PalmObject.transform,
            "FR5ToolContactMount",
            RobotCarrierRole.FR5,
            CarryableType.Tool,
            new Vector3(0.16f, 0.16f, 0.16f),
            false);

        RobotCargoMount zkMount = EnsureContactMount(
            zkSuctionObject.transform,
            "ZKContactMount",
            RobotCarrierRole.ZK,
            CarryableType.Base |
            CarryableType.Roof,
            new Vector3(0.16f, 0.16f, 0.16f));

        RobotCargoMount completedHouseMount = fork != null
            ? EnsureContactMount(
                fork.transform,
                "CompletedHouseContactMount",
                RobotCarrierRole.ForkTool,
                CarryableType.CompletedHouse,
                new Vector3(0.45f, 0.2f, 0.55f))
            : null;

        GameObject managersObject = FindSceneObject(scene, "Managers");
        Transform managerParent = managersObject != null
            ? managersObject.transform
            : null;

        Transform existingManager = managerParent != null
            ? managerParent.Find("MaterialFlowManager")
            : null;

        GameObject flowObject = existingManager != null
            ? existingManager.gameObject
            : new GameObject("MaterialFlowManager");

        if (existingManager == null)
        {
            flowObject.transform.SetParent(managerParent, false);
        }

        RoofVariantSpawner roofSpawner =
            GetOrAddComponent<RoofVariantSpawner>(flowObject);

        roofSpawner.Configure(
            AssetDatabase.LoadAssetAtPath<GameObject>(
                "Assets/Prefabs/house_material/roof/roof.prefab"),
            AssetDatabase.LoadAssetAtPath<GameObject>(
                "Assets/Prefabs/house_material/roof/roof2.prefab"),
            baseB != null ? baseB : baseA,
            material);

        HouseAssemblyController assembly =
            GetOrAddComponent<HouseAssemblyController>(flowObject);

        GameObject houseAPreRoof = FindSceneObject(
            scene,
            "House_A_PRE_ROOF");
        GameObject houseAComplete = FindSceneObject(
            scene,
            "House_A_COMPLETE");
        GameObject houseBPreRoof = FindSceneObject(
            scene,
            "House_B_PRE_ROOF");
        GameObject houseBComplete = FindSceneObject(
            scene,
            "House_B_COMPLETE");

        Transform houseBReferenceBase = FindDescendantNormalized(
            houseBComplete != null ? houseBComplete.transform : null,
            "B_base");

        if (houseBReferenceBase == null)
        {
            houseBReferenceBase = FindDescendantNormalized(
                houseBPreRoof != null ? houseBPreRoof.transform : null,
                "B_base");
        }

        Transform assemblyDropPoint = EnsurePoseMarker(
            flowObject.transform,
            "AssemblyBaseDropPoint",
            houseBReferenceBase);

        HouseAssemblyController.ProductAssemblyLayout layoutA =
            BuildProductAssemblyLayout(
                baseA,
                houseAPreRoof != null ? houseAPreRoof.transform : null,
                houseAComplete != null ? houseAComplete.transform : null,
                "HOUSE_A",
                "A_base",
                "roof",
                new[]
                {
                    "doorwall",
                    "rightwall",
                    "leftwall",
                    "backwall",
                    "A_indoorwall2",
                    "A_indoorwall1"
                });

        HouseAssemblyController.ProductAssemblyLayout layoutB =
            BuildProductAssemblyLayout(
                baseB,
                houseBPreRoof != null ? houseBPreRoof.transform : null,
                houseBComplete != null ? houseBComplete.transform : null,
                "HOUSE_B",
                "B_base",
                "roof2",
                new[]
                {
                    "doorwall",
                    "rightwall",
                    "leftwall",
                    "backwall",
                    "B_indoorwall"
                });

        assembly.Configure(
            new[] { layoutA, layoutB },
            assemblyDropPoint,
            "HOUSE_B");
        Transform assemblyBaseReference = fork != null
            ? FindDescendantNormalized(fork.transform, "B_base")
            : null;

        assembly.ConfigureAssemblySupport(
            fork != null ? fork.transform : null,
            assemblyBaseReference);

        foreach (CarryableObject basePart in new[] { baseA, baseB })
        {
            if (basePart == null)
            {
                continue;
            }

            AssemblyContactReceiver receiver =
                GetOrAddComponent<AssemblyContactReceiver>(
                    basePart.gameObject);

            receiver.Configure(assembly);
        }

        foreach (BasePlacementZone oldZone in
                 UnityEngine.Object.FindObjectsByType<BasePlacementZone>(
                     FindObjectsInactive.Include))
        {
            if (oldZone != null &&
                (fork == null || oldZone.transform != fork.transform))
            {
                UnityEngine.Object.DestroyImmediate(oldZone);
            }
        }

        if (fork != null)
        {
            BasePlacementZone placementZone =
                GetOrAddComponent<BasePlacementZone>(fork.gameObject);

            placementZone.Configure(assembly);
        }

        GameObject finishObject = FindSceneObjectIgnoreCase(scene, "final");
        Transform finalReferenceHouse = FindDescendantNormalized(
            finishObject != null ? finishObject.transform : null,
            "House_B_COMPLETE");
        Transform finalReferenceBase = FindDescendantNormalized(
            finalReferenceHouse,
            "B_base");
        Transform finalDropPoint = EnsurePoseMarker(
            finishObject != null ? finishObject.transform : null,
            "FinalDropPoint",
            finalReferenceBase != null
                ? finalReferenceBase
                : finalReferenceHouse);

        if (finishObject != null)
        {
            Collider finishCollider =
                finishObject.GetComponent<Collider>();

            if (finishCollider != null)
            {
                finishCollider.isTrigger = true;
            }

            CompletedHouseFinishZone finishZone =
                GetOrAddComponent<CompletedHouseFinishZone>(
                    finishObject);

            finishZone.Configure(
                finalDropPoint != null
                    ? finalDropPoint
                    : finishObject.transform);
        }

        FactoryMaterialFlowController flow =
            GetOrAddComponent<FactoryMaterialFlowController>(flowObject);

        flow.Configure(
            palletMount,
            fr5Mount,
            zkMount,
            innerPallet,
            outerPallet,
            fork,
            fr5ToolMount,
            completedHouseMount,
            assembly,
            roofSpawner,
            finalDropPoint);

        SetReferenceModelActive(houseAPreRoof, false);
        SetReferenceModelActive(houseAComplete, false);
        SetReferenceModelActive(houseBPreRoof, false);
        SetReferenceModelActive(houseBComplete, false);

        if (finalReferenceHouse != null)
        {
            SetReferenceModelActive(
                finalReferenceHouse.gameObject,
                false);
        }
    }

    private static HouseAssemblyController.ProductAssemblyLayout
        BuildProductAssemblyLayout(
            CarryableObject basePart,
            Transform preRoofReference,
            Transform completeReference,
            string productCode,
            string referenceBaseName,
            string roofPayloadId,
            string[] wallPayloadIds)
    {
        var layout = new HouseAssemblyController.ProductAssemblyLayout
        {
            productCode = productCode,
            basePart = basePart,
            roofPayloadId = roofPayloadId
        };

        if (basePart == null || preRoofReference == null ||
            completeReference == null)
        {
            Debug.LogWarning(
                $"[InspectionPageBuilder] {productCode} 기준 모델이 " +
                "완전하지 않습니다.");
            return layout;
        }

        Transform preRoofBase = FindDescendantNormalized(
            preRoofReference,
            referenceBaseName);
        Transform completeBase = FindDescendantNormalized(
            completeReference,
            referenceBaseName);

        if (preRoofBase == null || completeBase == null)
        {
            Debug.LogWarning(
                $"[InspectionPageBuilder] {productCode} 기준 Base를 " +
                "찾지 못했습니다.");
            return layout;
        }

        Transform socketRoot = EnsureChildTransform(
            basePart.transform,
            "AssemblySockets");
        Transform productSocketRoot = EnsureChildTransform(
            socketRoot,
            productCode);

        layout.wallSlots = wallPayloadIds
            .Select((payloadId, index) =>
            {
                Transform referencePart = FindDescendantNormalized(
                    preRoofBase,
                    payloadId);
                Transform socket = CreateRelativeSocket(
                    productSocketRoot,
                    preRoofBase,
                    referencePart,
                    payloadId + "_Socket");

                if (socket == null)
                {
                    Debug.LogWarning(
                        $"[InspectionPageBuilder] {productCode} " +
                        $"조립 위치가 없습니다: {payloadId}");
                }

                return new HouseAssemblyController.AssemblySlot
                {
                    operationCode = productCode + "_WALL_" + (index + 1),
                    payloadId = payloadId,
                    socket = socket
                };
            })
            .ToArray();

        Transform roofReference = FindDescendantNormalized(
            completeReference,
            roofPayloadId);
        layout.roofSocket = CreateRelativeSocket(
            productSocketRoot,
            completeBase,
            roofReference,
            roofPayloadId + "_Socket");

        if (layout.roofSocket == null)
        {
            Debug.LogWarning(
                $"[InspectionPageBuilder] {productCode} 지붕 설치 위치가 " +
                "없습니다.");
        }

        return layout;
    }

    private static Transform CreateRelativeSocket(
        Transform parent,
        Transform referenceBase,
        Transform referencePart,
        string socketName)
    {
        if (parent == null || referenceBase == null ||
            referencePart == null)
        {
            return null;
        }

        Transform socket = EnsureChildTransform(parent, socketName);
        socket.localPosition = referenceBase.InverseTransformPoint(
            referencePart.position);
        socket.localRotation = Quaternion.Inverse(referenceBase.rotation) *
                               referencePart.rotation;
        socket.localScale = Vector3.one;
        EditorUtility.SetDirty(socket.gameObject);
        return socket;
    }

    private static Transform EnsureChildTransform(
        Transform parent,
        string objectName)
    {
        if (parent == null)
        {
            return null;
        }

        Transform existing = parent.Find(objectName);

        if (existing != null)
        {
            return existing;
        }

        GameObject child = new GameObject(objectName);
        child.transform.SetParent(parent, false);
        return child.transform;
    }

    private static Transform EnsurePoseMarker(
        Transform parent,
        string objectName,
        Transform source)
    {
        if (parent == null || source == null)
        {
            return null;
        }

        Transform marker = EnsureChildTransform(parent, objectName);
        marker.SetPositionAndRotation(source.position, source.rotation);
        marker.localScale = Vector3.one;
        EditorUtility.SetDirty(marker.gameObject);
        return marker;
    }

    private static Transform FindDescendantNormalized(
        Transform root,
        string normalizedName)
    {
        if (root == null)
        {
            return null;
        }

        if (string.Equals(
                NormalizeReferenceName(root.name),
                normalizedName,
                StringComparison.OrdinalIgnoreCase))
        {
            return root;
        }

        foreach (Transform child in root)
        {
            Transform result = FindDescendantNormalized(
                child,
                normalizedName);

            if (result != null)
            {
                return result;
            }
        }

        return null;
    }

    private static string NormalizeReferenceName(string value)
    {
        string result = (value ?? string.Empty)
            .Replace("(Clone)", string.Empty)
            .Trim();

        int suffixStart = result.LastIndexOf(" (", StringComparison.Ordinal);

        if (suffixStart >= 0 && result.EndsWith(")"))
        {
            string suffix = result.Substring(
                suffixStart + 2,
                result.Length - suffixStart - 3);

            if (int.TryParse(suffix, out _))
            {
                result = result.Substring(0, suffixStart);
            }
        }

        return result;
    }

    private static void SetReferenceModelActive(
        GameObject reference,
        bool active)
    {
        if (reference == null || reference.activeSelf == active)
        {
            return;
        }

        reference.SetActive(active);
        EditorUtility.SetDirty(reference);
    }

    private static CarryableObject ConfigureCarryable(
        Transform target,
        CarryableType type,
        string payloadId)
    {
        if (target == null)
        {
            Debug.LogWarning(
                $"[InspectionPageBuilder] 자재를 찾지 못했습니다: {payloadId}");
            return null;
        }

        CarryableObject carryable =
            GetOrAddComponent<CarryableObject>(target.gameObject);

        carryable.Configure(type, payloadId);
        EnsureBoundsCollider(target.gameObject);
        EditorUtility.SetDirty(target.gameObject);
        return carryable;
    }

    private static RobotCargoMount EnsureContactMount(
        Transform endpoint,
        string objectName,
        RobotCarrierRole role,
        CarryableType acceptedTypes,
        Vector3 worldSize,
        bool pickupOnContact = true,
        bool preserveWorldPose = true)
    {
        Transform existing = endpoint.Find(objectName);
        GameObject mountObject = existing != null
            ? existing.gameObject
            : new GameObject(objectName);

        if (existing == null)
        {
            mountObject.transform.SetParent(endpoint, false);
        }

        Transform mountTransform = mountObject.transform;
        mountTransform.localPosition = Vector3.zero;
        mountTransform.localRotation = Quaternion.identity;

        Vector3 parentScale = endpoint.lossyScale;
        mountTransform.localScale = new Vector3(
            SafeInverse(parentScale.x),
            SafeInverse(parentScale.y),
            SafeInverse(parentScale.z));

        BoxCollider trigger = GetOrAddComponent<BoxCollider>(mountObject);
        trigger.isTrigger = true;
        trigger.center = Vector3.zero;
        trigger.size = worldSize;

        if (endpoint.GetComponentInParent<Rigidbody>() == null &&
            endpoint.GetComponentInParent<ArticulationBody>() == null)
        {
            Rigidbody body = GetOrAddComponent<Rigidbody>(mountObject);
            body.useGravity = false;
            body.isKinematic = true;
        }

        RobotCargoMount mount =
            GetOrAddComponent<RobotCargoMount>(mountObject);

        mount.Configure(
            role,
            acceptedTypes,
            mountTransform,
            pickupOnContact,
            preserveWorldPose);

        EditorUtility.SetDirty(mountObject);
        return mount;
    }

    private static float SafeInverse(float value)
    {
        return Mathf.Abs(value) < 0.000001f
            ? 1f
            : 1f / Mathf.Abs(value);
    }

    private static void EnsureBoundsCollider(GameObject target)
    {
        Collider existingCollider = target.GetComponent<Collider>();

        if (existingCollider != null)
        {
            existingCollider.isTrigger = true;
            EditorUtility.SetDirty(existingCollider);
            return;
        }

        Renderer[] renderers =
            target.GetComponentsInChildren<Renderer>(true);

        if (renderers.Length == 0)
        {
            return;
        }

        Bounds bounds = renderers[0].bounds;

        for (int i = 1; i < renderers.Length; i++)
        {
            bounds.Encapsulate(renderers[i].bounds);
        }

        Vector3 min = bounds.min;
        Vector3 max = bounds.max;
        Vector3 localMin = new Vector3(
            float.PositiveInfinity,
            float.PositiveInfinity,
            float.PositiveInfinity);

        Vector3 localMax = new Vector3(
            float.NegativeInfinity,
            float.NegativeInfinity,
            float.NegativeInfinity);

        for (int x = 0; x < 2; x++)
        {
            for (int y = 0; y < 2; y++)
            {
                for (int z = 0; z < 2; z++)
                {
                    Vector3 corner = new Vector3(
                        x == 0 ? min.x : max.x,
                        y == 0 ? min.y : max.y,
                        z == 0 ? min.z : max.z);

                    Vector3 local = target.transform
                        .InverseTransformPoint(corner);

                    localMin = Vector3.Min(localMin, local);
                    localMax = Vector3.Max(localMax, local);
                }
            }
        }

        BoxCollider collider = target.AddComponent<BoxCollider>();
        collider.isTrigger = true;
        collider.center = (localMin + localMax) * 0.5f;
        collider.size = localMax - localMin;
    }

    private static Transform FindDescendant(
        Transform root,
        string objectName)
    {
        if (root == null)
        {
            return null;
        }

        if (root.name == objectName)
        {
            return root;
        }

        foreach (Transform child in root)
        {
            Transform result = FindDescendant(child, objectName);

            if (result != null)
            {
                return result;
            }
        }

        return null;
    }

    private static T GetOrAddComponent<T>(GameObject target)
        where T : Component
    {
        T component = target.GetComponent<T>();
        return component != null
            ? component
            : target.AddComponent<T>();
    }

    private static Text EnsureEmptyText(
        Transform parent,
        string objectName,
        string message)
    {
        if (parent == null)
        {
            return null;
        }

        Transform existing = parent.Find(objectName);
        Text text = existing != null
            ? existing.GetComponent<Text>()
            : CreateText(
                objectName,
                parent,
                message,
                15,
                FontStyle.Normal,
                TextAnchor.MiddleCenter);

        text.text = message;
        text.color = new Color32(125, 135, 145, 255);
        text.raycastTarget = false;
        SetRect(
            text.rectTransform,
            new Vector2(0f, 1f),
            new Vector2(1f, 1f),
            new Vector2(0f, -80f),
            new Vector2(0f, -16f));

        return text;
    }

    private static GameObject CreateRectObject(
        string objectName,
        Transform parent)
    {
        GameObject result = new GameObject(
            objectName,
            typeof(RectTransform));

        result.layer = LayerMask.NameToLayer("UI");
        result.transform.SetParent(parent, false);
        return result;
    }

    private static Text CreateText(
        string objectName,
        Transform parent,
        string value,
        int fontSize,
        FontStyle fontStyle,
        TextAnchor alignment)
    {
        GameObject textObject = CreateRectObject(objectName, parent);
        Text text = textObject.AddComponent<Text>();
        text.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        text.text = value;
        text.fontSize = fontSize;
        text.fontStyle = fontStyle;
        text.alignment = alignment;
        text.color = TextColor;
        text.supportRichText = true;
        return text;
    }

    private static void Stretch(
        RectTransform rect,
        Vector2 offsetMin,
        Vector2 offsetMax)
    {
        SetRect(
            rect,
            Vector2.zero,
            Vector2.one,
            offsetMin,
            offsetMax);
    }

    private static void SetRect(
        RectTransform rect,
        Vector2 anchorMin,
        Vector2 anchorMax,
        Vector2 offsetMin,
        Vector2 offsetMax)
    {
        rect.anchorMin = anchorMin;
        rect.anchorMax = anchorMax;
        rect.offsetMin = offsetMin;
        rect.offsetMax = offsetMax;
        rect.localScale = Vector3.one;
        rect.localRotation = Quaternion.identity;
    }

    private static GameObject FindSceneObject(
        Scene scene,
        string objectName)
    {
        return Resources.FindObjectsOfTypeAll<GameObject>()
            .FirstOrDefault(item =>
                item.scene == scene &&
                item.name == objectName);
    }

    private static GameObject FindSceneObjectIgnoreCase(
        Scene scene,
        string objectName)
    {
        return Resources.FindObjectsOfTypeAll<GameObject>()
            .FirstOrDefault(item =>
                item.scene == scene &&
                string.Equals(
                    item.name,
                    objectName,
                    StringComparison.OrdinalIgnoreCase));
    }

    private static GameObject[] FindSceneObjects(
        Scene scene,
        string objectName)
    {
        return Resources.FindObjectsOfTypeAll<GameObject>()
            .Where(item =>
                item.scene == scene &&
                item.name == objectName)
            .ToArray();
    }

    private static T FindSceneComponent<T>(Scene scene)
        where T : Component
    {
        return FindSceneComponents<T>(scene).FirstOrDefault();
    }

    private static T[] FindSceneComponents<T>(Scene scene)
        where T : Component
    {
        return Resources.FindObjectsOfTypeAll<T>()
            .Where(item => item.gameObject.scene == scene)
            .ToArray();
    }

    private sealed class FeedWidgets
    {
        public RawImage image;
        public Text status;
        public Text placeholder;
        public Text detail;
    }
}
