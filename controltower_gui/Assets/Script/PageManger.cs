using UnityEngine;
using UnityEngine.UI;
using System;

public class PageManager : MonoBehaviour
{
    [Header("Pages")]
    public GameObject dashboardPanel;
    public GameObject robotPanel;
    public GameObject taskPanel;
    public GameObject inspectionPanel;

    [Header("Menu Buttons")]
    public Button dashboardButton;
    public Button robotButton;
    public Button taskButton;
    public Button inspectionButton;

    [Header("Menu Colors")]
    public Color selectedColor = IndustrialConsoleTheme.Accent;
    public Color normalColor = IndustrialConsoleTheme.Panel;

    private GameObject[] pages;
    private Button[] menuButtons;
    private Canvas themeCanvas;
    private float nextThemeRefresh;
    private Button activeMenuButton;
    [SerializeField] private Text consoleModeText;
    [SerializeField] private Text consoleClockText;
    private int lastClockSecond = -1;

    private void Start()
    {
        themeCanvas = dashboardPanel.GetComponentInParent<Canvas>();
        selectedColor = IndustrialConsoleTheme.Accent;
        normalColor = IndustrialConsoleTheme.Panel;
        IndustrialConsoleTheme.Apply(themeCanvas);
        pages = new[]
        {
            dashboardPanel,
            robotPanel,
            taskPanel,
            inspectionPanel
        };

        menuButtons = new[]
        {
            dashboardButton,
            robotButton,
            taskButton,
            inspectionButton
        };

        dashboardButton.onClick.AddListener(
            () => OpenPage(dashboardPanel, dashboardButton));

        robotButton.onClick.AddListener(
            () => OpenPage(robotPanel, robotButton));

        taskButton.onClick.AddListener(
            () => OpenPage(taskPanel, taskButton));

        if (inspectionButton != null && inspectionPanel != null)
        {
            inspectionButton.onClick.AddListener(
                () => OpenPage(inspectionPanel, inspectionButton));
        }

        // 처음 실행 시 대시보드를 열고 대시보드 버튼을 선택 색상으로 표시
        OpenPage(dashboardPanel, dashboardButton);
    }

    private void OpenPage(GameObject selectedPage, Button selectedButton)
    {
        activeMenuButton = selectedButton;
        foreach (GameObject page in pages)
        {
            if (page != null)
            {
                page.SetActive(page == selectedPage);
            }
        }

        foreach (Button button in menuButtons)
        {
            if (button == null)
            {
                continue;
            }

            bool selected = button == selectedButton;
            button.transition = Selectable.Transition.None;
            button.image.canvasRenderer.SetColor(Color.white);
            button.image.color = selected ? selectedColor : normalColor;

            Text label = button.GetComponentInChildren<Text>(true);

            if (label != null)
            {
                label.color = selected
                    ? IndustrialConsoleTheme.Background
                    : IndustrialConsoleTheme.TextColor;
                label.fontStyle = FontStyle.Bold;
            }
        }
    }

    private void LateUpdate()
    {
        int second = (int)(DateTime.Now.Ticks / TimeSpan.TicksPerSecond % int.MaxValue);
        if (second != lastClockSecond)
        {
            lastClockSecond = second;
            if (consoleModeText != null)
                consoleModeText.text = "●  LIVE MONITORING";
            if (consoleClockText != null)
                consoleClockText.text = DateTime.Now.ToString("yyyy.MM.dd  HH:mm:ss");
        }
        // Runtime rows and server-driven labels can be created after Start.
        if (Time.unscaledTime < nextThemeRefresh || themeCanvas == null) return;
        nextThemeRefresh = Time.unscaledTime + 0.5f;
        IndustrialConsoleTheme.Apply(themeCanvas);
        foreach (var button in menuButtons)
        {
            if (button == null) continue;
            bool selected = button == activeMenuButton;
            button.image.color = selected ? selectedColor : normalColor;
            var label = button.GetComponentInChildren<Text>(true);
            if (label != null) label.color = selected
                ? IndustrialConsoleTheme.Background : IndustrialConsoleTheme.TextColor;
        }
    }
}

// Shared by the saved scene and runtime-created rows. Camera/robot textures and
// semantic PASS/FAIL colors are preserved; only legacy neutral surfaces change.
public static class IndustrialConsoleTheme
{
    public static readonly Color Background = new Color32(10, 10, 12, 255);
    public static readonly Color Panel = new Color32(20, 21, 24, 255);
    public static readonly Color Raised = new Color32(30, 31, 36, 255);
    public static readonly Color TextColor = new Color32(244, 246, 249, 255);
    public static readonly Color Accent = new Color32(255, 196, 0, 255);
    public static readonly Color Cyan = new Color32(45, 199, 213, 255);
    public static readonly Color Live = new Color32(235, 55, 55, 255);
    public static readonly Color Dim = new Color32(176, 180, 188, 255);
    public static readonly Color Edge = new Color32(44, 46, 54, 255);

    public static void Apply(Canvas canvas)
    {
        if (canvas == null) return;
        foreach (var video in canvas.GetComponentsInChildren<RawImage>(true))
            if (video.texture == null) video.color = Background;
        foreach (var image in canvas.GetComponentsInChildren<Image>(true))
        {
            string n = image.name;
            bool background = image.gameObject == canvas.gameObject ||
                n == "Content" || n == "TopBar" || n == "DashboardP" ||
                n == "RobotP" || n == "TaskP" || n == "InspectionP" ||
                n == "MainArea" || n == "RightColumn";
            bool surface = background || n.EndsWith("Card") ||
                n == "SummaryP" || n == "SummaryRow" || n == "CurrentTaskP" ||
                n == "RobotSummaryP" || n == "EventLogP" || n == "JobListP" ||
                n == "ProcessFlowP" || n == "TB3P" || n == "FR5P" ||
                n == "ZK1P" || n == "DefectResultBox" || n == "Viewport" ||
                n == "GlobalCameraViewport";
            if (surface)
            {
                image.color = background || n == "GlobalCameraCard" ||
                    n == "GlobalCameraViewport" ? Background :
                    n == "DefectResultBox" || n == "Viewport" ? Raised : Panel;
                // Opaque rectangular surfaces meet without rounded transparent corners.
                image.sprite = null;
                image.type = Image.Type.Simple;
                continue;
            }
            if (image.color.a < 0.05f) continue;
            if (image.GetComponentInParent<ProcessStepUI>(true) != null) continue;
            // Image-based robot photos must not be tinted.
            string sprite = image.sprite != null ? image.sprite.name : "";
            if (sprite != "" && sprite != "Background" && sprite != "UISprite" &&
                sprite != "InputFieldBackground" && sprite != "UIMask") continue;
            Color c = image.color;
            float spread = Mathf.Max(c.r, c.g, c.b) - Mathf.Min(c.r, c.g, c.b);
            if (n == "TopBar" || n == "DashboardP" ||
                n == "RobotP" || n == "TaskP" || n == "InspectionP")
                image.color = Background;
            else if (n == "SideBar" || n == "Btn")
                image.color = Color.clear;
            else if (image.GetComponent<Selectable>() != null && spread < 0.17f)
                image.color = Raised;
            else if (spread < 0.17f &&
                     n != "HeaderRule" && n != "ConsoleModeBadge")
                image.color = n.Contains("Box") || n.Contains("Row") ? Raised : Panel;
        }
        foreach (var label in canvas.GetComponentsInChildren<Text>(true))
        {
            Color c = label.color;
            float max = Mathf.Max(c.r, c.g, c.b);
            float min = Mathf.Min(c.r, c.g, c.b);
            if (label.GetComponentInParent<Selectable>(true) == null &&
                max < 0.52f && max - min < 0.25f)
                label.color = TextColor;
            if (label.name == "Title" || label.name == "TitleText")
            {
                label.color = Accent;
                label.fontStyle = FontStyle.Bold;
            }
        }
        foreach (var outline in canvas.GetComponentsInChildren<Outline>(true))
            outline.effectColor = Edge;
    }
}
