using UnityEngine;
using UnityEngine.UI;

public class ProcessStepUI : MonoBehaviour
{
    public enum StepState
    {
        Pending,
        Current,
        Completed,
        Paused,
        Error
    }

    [Header("UI")]
    [SerializeField]
    private Image circleImage;

    [SerializeField]
    private Image connectorLine;

    [SerializeField]
    private Text numberText;

    [SerializeField]
    private Text labelText;

    private int stepNumber;

    public StepState CurrentState
    {
        get;
        private set;
    }

    private readonly Color pendingColor =
        IndustrialConsoleTheme.Edge;

    private readonly Color currentColor =
        new Color32(45, 199, 213, 255);

    private readonly Color completedColor =
        new Color32(42, 170, 93, 255);

    private readonly Color pausedColor =
        new Color32(235, 155, 45, 255);

    private readonly Color errorColor =
        new Color32(215, 65, 65, 255);

    public void Initialize(
        int number,
        string displayName)
    {
        stepNumber = number;

        if (numberText != null)
        {
            numberText.text = number.ToString();
        }

        if (labelText != null)
        {
            labelText.text = displayName;
        }

        SetState(StepState.Pending);
    }

    public void SetState(StepState state)
    {
        CurrentState = state;

        if (circleImage == null)
        {
            return;
        }

        switch (state)
        {
            case StepState.Completed:

                circleImage.color = completedColor;

                if (numberText != null)
                {
                    numberText.text = "✓";
                }

                break;

            case StepState.Current:

                circleImage.color = currentColor;

                if (numberText != null)
                {
                    numberText.text = stepNumber.ToString();
                }

                break;

            case StepState.Paused:

                circleImage.color = pausedColor;

                if (numberText != null)
                {
                    numberText.text = stepNumber.ToString();
                }

                break;

            case StepState.Error:

                circleImage.color = errorColor;

                if (numberText != null)
                {
                    numberText.text = "!";
                }

                break;

            default:

                circleImage.color = pendingColor;

                if (numberText != null)
                {
                    numberText.text = stepNumber.ToString();
                }

                break;
        }
    }

    public void SetConnectorCompleted(bool completed)
    {
        if (connectorLine == null)
        {
            return;
        }

        connectorLine.color =
            completed ? completedColor : pendingColor;
    }

}
