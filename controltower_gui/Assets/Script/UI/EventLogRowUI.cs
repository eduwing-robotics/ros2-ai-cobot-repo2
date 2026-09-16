using UnityEngine;
using UnityEngine.UI;

public enum EventLogLevel
{
    Info,
    Warning,
    Error
}

public class EventLogRowUI : MonoBehaviour
{
    [SerializeField]
    private Text timeText;

    [SerializeField]
    private Text levelText;

    [SerializeField]
    private Text messageText;

    [SerializeField]
    private Image backgroundImage;

    private readonly Color infoColor =
        IndustrialConsoleTheme.Cyan;

    private readonly Color warningColor =
        new Color32(235, 155, 45, 255);

    private readonly Color errorColor =
        new Color32(215, 65, 65, 255);

    private readonly Color evenRowColor =
        IndustrialConsoleTheme.Panel;

    private readonly Color oddRowColor =
        IndustrialConsoleTheme.Raised;

    public void SetData(
        string time,
        EventLogLevel level,
        string message,
        bool alternateBackground)
    {
        timeText.text = time;
        levelText.text = FormatLevel(level);
        levelText.color = GetLevelColor(level);
        messageText.text = message;

        if (backgroundImage != null)
        {
            backgroundImage.color =
                alternateBackground
                    ? oddRowColor
                    : evenRowColor;
        }
    }

    private string FormatLevel(EventLogLevel level)
    {
        switch (level)
        {
            case EventLogLevel.Warning:
                return "WARNING";

            case EventLogLevel.Error:
                return "ERROR";

            default:
                return "INFO";
        }
    }

    private Color GetLevelColor(EventLogLevel level)
    {
        switch (level)
        {
            case EventLogLevel.Warning:
                return warningColor;

            case EventLogLevel.Error:
                return errorColor;

            default:
                return infoColor;
        }
    }
}
