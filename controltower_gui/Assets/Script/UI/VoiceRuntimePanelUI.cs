using System;
using System.Text;
using UnityEngine;
using UnityEngine.UI;

public class VoiceRuntimePanelUI : MonoBehaviour
{
    [Header("Communication")]
    [SerializeField]
    private FactoryStateManager factoryStateManager;

    [Header("Display")]
    [SerializeField]
    private Text statusText;

    [SerializeField]
    private Text conversationText;

    [SerializeField]
    private Text recentTurnsText;

    private static readonly Color IdleColor =
        new Color32(145, 155, 165, 255);
    private static readonly Color ActiveColor =
        new Color32(46, 204, 143, 255);
    private static readonly Color ProcessingColor =
        new Color32(255, 196, 0, 255);
    private static readonly Color RespondingColor =
        new Color32(62, 166, 255, 255);
    private static readonly Color ErrorColor =
        new Color32(239, 68, 68, 255);

    private void Awake()
    {
        Render(null);
    }

    private void OnEnable()
    {
        if (factoryStateManager == null)
        {
            Debug.LogError(
                "[VoicePanel] FactoryStateManager가 연결되지 않았습니다.",
                this);
            Render(null);
            return;
        }

        factoryStateManager.SnapshotApplied += HandleSnapshotApplied;
        factoryStateManager.VoiceRuntimeUpdated += HandleVoiceRuntimeUpdated;
        Render(factoryStateManager.CurrentVoiceRuntime);
    }

    private void OnDisable()
    {
        if (factoryStateManager == null)
        {
            return;
        }

        factoryStateManager.SnapshotApplied -= HandleSnapshotApplied;
        factoryStateManager.VoiceRuntimeUpdated -= HandleVoiceRuntimeUpdated;
    }

    private void HandleSnapshotApplied(ProductionSnapshotData snapshot)
    {
        Render(snapshot?.voice_runtime);
    }

    private void HandleVoiceRuntimeUpdated(VoiceRuntimeData runtime)
    {
        Render(runtime);
    }

    private void Render(VoiceRuntimeData runtime)
    {
        string state = string.IsNullOrWhiteSpace(runtime?.state)
            ? "IDLE"
            : runtime.state.Trim().ToUpperInvariant();

        if (statusText != null)
        {
            statusText.text = "●  " + GetStateLabel(state);
            statusText.color = GetStateColor(state);
        }

        VoiceTurnData latest = GetLatestTurn(runtime);
        string transcript = runtime?.transcript;
        string response = runtime?.response_text;

        if (string.IsNullOrWhiteSpace(transcript))
        {
            transcript = latest?.transcript;
        }

        if (string.IsNullOrWhiteSpace(response))
        {
            response = latest?.response_text;
        }

        if (conversationText != null)
        {
            conversationText.supportRichText = true;

            if (state == "ERROR" &&
                !string.IsNullOrWhiteSpace(runtime?.error_message))
            {
                conversationText.text =
                    "<color=#EF4444>오류</color>\n" +
                    Escape(runtime.error_message);
            }
            else if (string.IsNullOrWhiteSpace(transcript) &&
                     string.IsNullOrWhiteSpace(response))
            {
                conversationText.text =
                    "<color=#919BA5>음성 명령을 기다리고 있습니다.</color>";
            }
            else
            {
                StringBuilder builder = new StringBuilder();

                if (!string.IsNullOrWhiteSpace(transcript))
                {
                    builder.Append("<color=#FFC400>사용자</color>\n\"")
                        .Append(Escape(transcript))
                        .Append('"');
                }

                if (!string.IsNullOrWhiteSpace(response))
                {
                    if (builder.Length > 0)
                    {
                        builder.Append("\n\n");
                    }

                    builder.Append("<color=#3EA6FF>AI</color>\n\"")
                        .Append(Escape(response))
                        .Append('"');
                }

                conversationText.text = builder.ToString();
            }
        }

        if (recentTurnsText != null)
        {
            recentTurnsText.supportRichText = true;
            recentTurnsText.text = BuildRecentTurns(runtime?.recent_turns);
        }
    }

    private static VoiceTurnData GetLatestTurn(VoiceRuntimeData runtime)
    {
        VoiceTurnData[] turns = runtime?.recent_turns;
        if (turns == null)
        {
            return null;
        }

        for (int i = turns.Length - 1; i >= 0; i--)
        {
            if (turns[i] != null)
            {
                return turns[i];
            }
        }

        return null;
    }

    private static string BuildRecentTurns(VoiceTurnData[] turns)
    {
        if (turns == null || turns.Length == 0)
        {
            return "<color=#919BA5>아직 기록된 대화가 없습니다.</color>";
        }

        StringBuilder builder = new StringBuilder();
        int shown = 0;

        for (int i = turns.Length - 1; i >= 0 && shown < 4; i--)
        {
            VoiceTurnData turn = turns[i];
            if (turn == null)
            {
                continue;
            }

            if (builder.Length > 0)
            {
                builder.Append('\n');
            }

            builder.Append("<color=#919BA5>")
                .Append(FormatTime(turn.timestamp))
                .Append("</color>  ")
                .Append(Escape(GetIntentLabel(turn.intent, turn.transcript)));
            shown++;
        }

        return shown > 0
            ? builder.ToString()
            : "<color=#919BA5>아직 기록된 대화가 없습니다.</color>";
    }

    private static string GetIntentLabel(string intent, string transcript)
    {
        switch ((intent ?? string.Empty).Trim().ToUpperInvariant())
        {
            case "QUERY_JOB_STATUS": return "생산 상태 조회";
            case "QUERY_INVENTORY": return "재고 조회";
            case "QUERY_ROBOT_STATUS": return "로봇 상태 조회";
            case "QUERY_ERROR_STATUS": return "오류 상태 조회";
        }

        if (!string.IsNullOrWhiteSpace(intent))
        {
            return intent.Trim().Replace('_', ' ');
        }

        if (!string.IsNullOrWhiteSpace(transcript))
        {
            const int maxLength = 20;
            string value = transcript.Trim();
            return value.Length <= maxLength
                ? value
                : value.Substring(0, maxLength) + "…";
        }

        return "음성 대화";
    }

    private static string FormatTime(string timestamp)
    {
        if (DateTimeOffset.TryParse(timestamp, out DateTimeOffset parsed))
        {
            return parsed.ToLocalTime().ToString("HH:mm");
        }

        return "--:--";
    }

    private static string GetStateLabel(string state)
    {
        switch (state)
        {
            case "IDLE": return "대기 중";
            case "LISTENING": return "듣는 중...";
            case "TRANSCRIBING": return "음성 인식 중";
            case "INTERPRETING": return "명령 처리 중";
            case "RESPONDING": return "응답 중";
            case "WAITING_CONFIRMATION": return "추가 입력 대기 중";
            case "ERROR": return "오류";
            default: return "상태 확인 중";
        }
    }

    private static Color GetStateColor(string state)
    {
        switch (state)
        {
            case "LISTENING": return ActiveColor;
            case "TRANSCRIBING":
            case "INTERPRETING":
            case "WAITING_CONFIRMATION":
                return ProcessingColor;
            case "RESPONDING": return RespondingColor;
            case "ERROR": return ErrorColor;
            default: return IdleColor;
        }
    }

    private static string Escape(string value)
    {
        return (value ?? string.Empty)
            .Replace("&", "&amp;")
            .Replace("<", "&lt;")
            .Replace(">", "&gt;");
    }
}
