using System;
using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

/// <summary>
/// Direct, manual TurtleBot control over the Raspberry Pi WebSocket endpoint.
/// This connection is intentionally independent from the Main Server /ws/unity
/// monitoring connection.
/// </summary>
public sealed class TurtleBotManualDriveClient : MonoBehaviour
{
    public enum ManualState
    {
        Disconnected,
        Connecting,
        WaitingForControl,
        Auto,
        ManualReady,
        ManualActive,
        Stopped,
        TimedOut,
        Rejected,
        Error
    }

    public readonly struct StatusSnapshot
    {
        public StatusSnapshot(ManualState state, string message, string sessionId)
        {
            State = state;
            Message = message;
            SessionId = sessionId;
        }

        public ManualState State { get; }
        public string Message { get; }
        public string SessionId { get; }
    }

    private const string DefaultUrl = "ws://192.168.20.100:8765/manual";
    private const float SendIntervalSeconds = 1f / 15f;
    private const float MinLinear = -0.05f;
    private const float MaxLinear = 0.08f;
    private const float MinAngular = -0.35f;
    private const float MaxAngular = 0.35f;

    private readonly ConcurrentQueue<Action> mainThreadActions =
        new ConcurrentQueue<Action>();
    private readonly SemaphoreSlim sendLock = new SemaphoreSlim(1, 1);

    private ClientWebSocket socket;
    private CancellationTokenSource lifetimeCancellation;
    private ManualState state = ManualState.Disconnected;
    private string statusMessage = "연결되지 않음";
    private string sessionId = string.Empty;
    private long sequence;
    private bool connectInProgress;
    private bool shutdownInProgress;
    private bool driveHeld;
    private float requestedLinear;
    private float requestedAngular;
    private float nextDriveSendTime;
    private int driveGeneration;
    private bool applicationQuitting;

    public event Action<StatusSnapshot> StatusChanged;

    public string EndpointUrl => DefaultUrl;
    public ManualState State => state;
    public string StatusMessage => statusMessage;
    public string SessionId => sessionId;
    public bool IsSocketOpen => socket != null && socket.State == WebSocketState.Open;
    public bool CanDrive =>
        IsSocketOpen &&
        (state == ManualState.ManualReady ||
         state == ManualState.ManualActive ||
         state == ManualState.Stopped);

    private void Update()
    {
        while (mainThreadActions.TryDequeue(out Action action))
        {
            action?.Invoke();
        }

        if (!driveHeld || !CanDrive || Time.realtimeSinceStartup < nextDriveSendTime)
        {
            return;
        }

        nextDriveSendTime = Time.realtimeSinceStartup + SendIntervalSeconds;
        int generation = driveGeneration;
        _ = SendDrivePacketAsync(generation);
    }

    public void ConnectAndTakeControl()
    {
        if (connectInProgress || shutdownInProgress)
        {
            return;
        }

        if (IsSocketOpen)
        {
            _ = TakeControlAsync();
            return;
        }

        _ = ConnectAsync();
    }

    public void SetDrive(float linearX, float angularZ)
    {
        if (!CanDrive)
        {
            return;
        }

        requestedLinear = Mathf.Clamp(linearX, MinLinear, MaxLinear);
        requestedAngular = Mathf.Clamp(angularZ, MinAngular, MaxAngular);
        driveHeld = true;
        driveGeneration++;
        nextDriveSendTime = Time.realtimeSinceStartup + SendIntervalSeconds;
        int generation = driveGeneration;
        _ = SendDrivePacketAsync(generation);
    }

    public void StopDriving()
    {
        bool shouldSend = driveHeld && IsSocketOpen;
        driveHeld = false;
        driveGeneration++;
        requestedLinear = 0f;
        requestedAngular = 0f;

        if (shouldSend)
        {
            _ = SendControlCommandAsync("STOP", false);
        }
    }

    public void StopNow()
    {
        driveHeld = false;
        driveGeneration++;
        requestedLinear = 0f;
        requestedAngular = 0f;

        if (IsSocketOpen)
        {
            _ = SendControlCommandAsync("STOP", false);
        }
    }

    public void ReleaseAndDisconnect(string reason = "USER_RELEASE")
    {
        _ = ShutdownAsync(reason);
    }

    private async Task ConnectAsync()
    {
        connectInProgress = true;
        shutdownInProgress = false;
        SetStatusOnMainThread(ManualState.Connecting, "Raspberry Pi에 연결 중...");

        ClientWebSocket newSocket = new ClientWebSocket();
        CancellationTokenSource newCancellation = new CancellationTokenSource();
        socket = newSocket;
        lifetimeCancellation = newCancellation;

        try
        {
            await newSocket.ConnectAsync(
                new Uri(DefaultUrl),
                newCancellation.Token).ConfigureAwait(false);

            sessionId = Guid.NewGuid().ToString();
            Interlocked.Exchange(ref sequence, 0L);

            SetStatusOnMainThread(
                ManualState.WaitingForControl,
                "제어권 승인 대기 중...");
            _ = ReceiveLoopAsync(newSocket, newCancellation.Token);
            await SendControlCommandAsync("TAKE_CONTROL", false)
                .ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            bool cancelledByOperator = !ReferenceEquals(socket, newSocket) ||
                                       applicationQuitting;

            if (ReferenceEquals(socket, newSocket))
            {
                socket = null;
                lifetimeCancellation = null;
            }

            try { newCancellation.Cancel(); } catch (ObjectDisposedException) { }
            newCancellation.Dispose();
            newSocket.Dispose();

            if (!cancelledByOperator)
            {
                SetStatusOnMainThread(
                    ManualState.Error,
                    "연결 실패: " + exception.Message);
                Debug.LogWarning(
                    "[TurtleBot Manual] Connection failed: " + exception.Message);
            }
        }
        finally
        {
            connectInProgress = false;
        }
    }

    private async Task TakeControlAsync()
    {
        if (!IsSocketOpen || shutdownInProgress)
        {
            return;
        }

        SetStatusOnMainThread(
            ManualState.WaitingForControl,
            "제어권 재획득 대기 중...");
        // Retaking control on an open WebSocket keeps the same session and the
        // monotonically increasing sequence required for that connection.
        await SendControlCommandAsync("TAKE_CONTROL", false)
            .ConfigureAwait(false);
    }

    private async Task SendDrivePacketAsync(int generation)
    {
        await sendLock.WaitAsync().ConfigureAwait(false);
        try
        {
            if (generation != driveGeneration || !driveHeld || !CanDrive)
            {
                return;
            }

            long nextSequence = Interlocked.Increment(ref sequence);
            ManualDriveMessage message = new ManualDriveMessage
            {
                type = "manual_drive",
                session_id = sessionId,
                sequence = nextSequence,
                linear_x = requestedLinear,
                angular_z = requestedAngular,
                deadman = true
            };

            await SendJsonWithoutLockAsync(JsonUtility.ToJson(message), false)
                .ConfigureAwait(false);
        }
        finally
        {
            sendLock.Release();
        }
    }

    private async Task SendControlCommandAsync(string command, bool closing)
    {
        await sendLock.WaitAsync().ConfigureAwait(false);
        try
        {
            ManualControlMessage message = new ManualControlMessage
            {
                type = "manual_control",
                command = command,
                session_id = sessionId
            };

            Debug.Log($"[TurtleBot Manual] Control TX: {command}");
            await SendJsonWithoutLockAsync(JsonUtility.ToJson(message), closing)
                .ConfigureAwait(false);
        }
        finally
        {
            sendLock.Release();
        }
    }

    private async Task SendJsonWithoutLockAsync(string json, bool closing)
    {
        ClientWebSocket activeSocket = socket;

        if (activeSocket == null || activeSocket.State != WebSocketState.Open)
        {
            return;
        }

        try
        {
            byte[] bytes = Encoding.UTF8.GetBytes(json);
            await activeSocket.SendAsync(
                new ArraySegment<byte>(bytes),
                WebSocketMessageType.Text,
                true,
                lifetimeCancellation != null
                    ? lifetimeCancellation.Token
                    : CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            if (!closing && !shutdownInProgress)
            {
                SetStatusOnMainThread(
                    ManualState.Error,
                    "전송 오류: " + exception.Message);
                mainThreadActions.Enqueue(() => _ = ShutdownAsync("SEND_ERROR"));
            }
        }
    }

    private async Task ReceiveLoopAsync(
        ClientWebSocket activeSocket,
        CancellationToken cancellationToken)
    {
        byte[] buffer = new byte[4096];

        try
        {
            while (activeSocket.State == WebSocketState.Open &&
                   !cancellationToken.IsCancellationRequested)
            {
                StringBuilder messageBuilder = new StringBuilder();
                WebSocketReceiveResult result;

                do
                {
                    result = await activeSocket.ReceiveAsync(
                        new ArraySegment<byte>(buffer),
                        cancellationToken).ConfigureAwait(false);

                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        mainThreadActions.Enqueue(() =>
                        {
                            if (!shutdownInProgress)
                            {
                                SetStatus(
                                    ManualState.Disconnected,
                                    "Raspberry Pi가 연결을 종료함");
                            }
                        });
                        return;
                    }

                    if (result.MessageType == WebSocketMessageType.Text)
                    {
                        messageBuilder.Append(
                            Encoding.UTF8.GetString(buffer, 0, result.Count));
                    }
                }
                while (!result.EndOfMessage);

                if (result.MessageType == WebSocketMessageType.Text)
                {
                    string json = messageBuilder.ToString();
                    mainThreadActions.Enqueue(() => HandleServerMessage(json));
                }
            }
        }
        catch (OperationCanceledException)
        {
            // Expected during controlled shutdown.
        }
        catch (Exception exception)
        {
            if (!shutdownInProgress)
            {
                SetStatusOnMainThread(
                    ManualState.Error,
                    "수신 오류: " + exception.Message);
                mainThreadActions.Enqueue(() => _ = ShutdownAsync("RECEIVE_ERROR"));
            }
        }
    }

    private void HandleServerMessage(string json)
    {
        try
        {
            ManualServerMessage message =
                JsonUtility.FromJson<ManualServerMessage>(json);

            if (message == null || string.IsNullOrWhiteSpace(message.type))
            {
                return;
            }

            if (message.type == "connection")
            {
                statusMessage = string.IsNullOrWhiteSpace(message.message)
                    ? "WebSocket 연결됨"
                    : message.message;
                PublishStatus();
                return;
            }

            if (message.type != "manual_state")
            {
                return;
            }

            ManualState mappedState = MapState(message.state);
            SetStatus(
                mappedState,
                string.IsNullOrWhiteSpace(message.message)
                    ? message.state
                    : message.message);

            if (mappedState == ManualState.TimedOut ||
                mappedState == ManualState.Rejected ||
                mappedState == ManualState.Auto)
            {
                driveHeld = false;
                driveGeneration++;
            }
        }
        catch (Exception exception)
        {
            Debug.LogWarning(
                "[TurtleBot Manual] Invalid server message: " +
                exception.Message + "\n" + json);
        }
    }

    private static ManualState MapState(string value)
    {
        switch ((value ?? string.Empty).Trim().ToUpperInvariant())
        {
            case "AUTO": return ManualState.Auto;
            case "MANUAL_READY": return ManualState.ManualReady;
            case "MANUAL_ACTIVE": return ManualState.ManualActive;
            case "STOPPED": return ManualState.Stopped;
            case "TIMED_OUT": return ManualState.TimedOut;
            case "REJECTED": return ManualState.Rejected;
            default: return ManualState.Error;
        }
    }

    private async Task ShutdownAsync(string reason)
    {
        if (shutdownInProgress)
        {
            return;
        }

        shutdownInProgress = true;
        connectInProgress = false;
        driveHeld = false;
        driveGeneration++;

        ClientWebSocket activeSocket = socket;

        try
        {
            if (activeSocket != null && activeSocket.State == WebSocketState.Open)
            {
                await SendControlCommandAsync("STOP", true)
                    .ConfigureAwait(false);
                await SendControlCommandAsync("RELEASE_CONTROL", true)
                    .ConfigureAwait(false);

                using (CancellationTokenSource closeTimeout =
                       new CancellationTokenSource(TimeSpan.FromMilliseconds(500)))
                {
                    await activeSocket.CloseAsync(
                        WebSocketCloseStatus.NormalClosure,
                        reason,
                        closeTimeout.Token).ConfigureAwait(false);
                }
            }
        }
        catch (Exception)
        {
            activeSocket?.Abort();
        }
        finally
        {
            lifetimeCancellation?.Cancel();
            lifetimeCancellation?.Dispose();
            lifetimeCancellation = null;
            activeSocket?.Dispose();
            socket = null;
            sessionId = string.Empty;
            shutdownInProgress = false;
            SetStatusOnMainThread(ManualState.Disconnected, "제어권 반환 요청 후 연결 종료");
        }
    }

    private void SetStatusOnMainThread(ManualState newState, string message)
    {
        mainThreadActions.Enqueue(() => SetStatus(newState, message));
    }

    private void SetStatus(ManualState newState, string message)
    {
        Debug.Log($"[TurtleBot Manual] State={newState}, message={message}");
        state = newState;
        statusMessage = message ?? string.Empty;
        PublishStatus();
    }

    private void PublishStatus()
    {
        StatusChanged?.Invoke(new StatusSnapshot(state, statusMessage, sessionId));
    }

    private void OnApplicationFocus(bool hasFocus)
    {
        if (!hasFocus && IsSocketOpen)
        {
            _ = ShutdownAsync("FOCUS_LOST");
        }
    }

    private void OnApplicationPause(bool pauseStatus)
    {
        if (pauseStatus && IsSocketOpen)
        {
            _ = ShutdownAsync("APPLICATION_PAUSED");
        }
    }

    private void OnApplicationQuit()
    {
        applicationQuitting = true;

        if (!IsSocketOpen)
        {
            return;
        }

        try
        {
            Task criticalSend = Task.Run(async () =>
            {
                await SendControlCommandAsync("STOP", true).ConfigureAwait(false);
                await SendControlCommandAsync("RELEASE_CONTROL", true)
                    .ConfigureAwait(false);
            });
            Task.WhenAny(criticalSend, Task.Delay(400)).GetAwaiter().GetResult();
        }
        catch (Exception)
        {
            socket?.Abort();
        }
    }

    private void OnDestroy()
    {
        if (!applicationQuitting && IsSocketOpen)
        {
            _ = ShutdownAsync("OBJECT_DESTROYED");
        }
    }

    [Serializable]
    private sealed class ManualControlMessage
    {
        public string type;
        public string command;
        public string session_id;
    }

    [Serializable]
    private sealed class ManualDriveMessage
    {
        public string type;
        public string session_id;
        public long sequence;
        public float linear_x;
        public float angular_z;
        public bool deadman;
    }

    [Serializable]
    private sealed class ManualServerMessage
    {
        public string type;
        public string session_id;
        public string state;
        public bool success;
        public string message;
    }
}

/// <summary>
/// Creates the entry button and the manual-drive overlay at runtime so the
/// saved scene and carefully placed digital-twin transforms stay untouched.
/// </summary>
public sealed class TurtleBotManualDrivePanel : MonoBehaviour
{
    private static readonly Color Background = new Color32(10, 10, 12, 255);
    private static readonly Color Panel = new Color32(20, 21, 24, 255);
    private static readonly Color Raised = new Color32(30, 31, 36, 255);
    private static readonly Color TextColor = new Color32(244, 246, 249, 255);
    private static readonly Color Accent = new Color32(255, 196, 0, 255);
    private static readonly Color Cyan = new Color32(45, 199, 213, 255);
    private static readonly Color Dim = new Color32(176, 180, 188, 255);
    private static readonly Color Danger = new Color32(235, 55, 55, 255);

    private TurtleBotManualDriveClient client;
    private GameObject overlay;
    private Text stateText;
    private Text messageText;
    private Text sessionText;
    private Button connectButton;
    private Text connectButtonText;
    private Button releaseButton;
    private Button[] driveButtons;
    private bool keyboardDriveActive;
    private Vector2 lastKeyboardDrive;
    private bool manualModeRequested;

    public void Build(TurtleBotManualDriveClient manualClient, GameObject robotPage)
    {
        client = manualClient;
        client.StatusChanged += HandleStatusChanged;
    }

    private void CreateKeyboardStatus()
    {
        Image statusHud = CreatePanel(transform, "ManualDriveKeyboardStatus", Background);
        SetRect(statusHud.rectTransform,
            new Vector2(0.08f, 0.77f), new Vector2(0.92f, 0.94f),
            Vector2.zero, Vector2.zero);

        Text title = CreateText(
            statusHud.transform,
            "Title",
            "KEYBOARD MANUAL  /  수동주행",
            11,
            Accent,
            TextAnchor.MiddleLeft);
        SetRect(title.rectTransform,
            new Vector2(0f, 0.67f), new Vector2(1f, 1f),
            new Vector2(10f, 0f), new Vector2(-10f, 0f));
        title.fontStyle = FontStyle.Bold;

        stateText = CreateText(
            statusHud.transform,
            "State",
            "●  OFF  ·  / 키로 수동모드 시작",
            11,
            Dim,
            TextAnchor.MiddleLeft);
        SetRect(stateText.rectTransform,
            new Vector2(0f, 0.34f), new Vector2(1f, 0.68f),
            new Vector2(10f, 0f), new Vector2(-10f, 0f));
        stateText.fontStyle = FontStyle.Bold;

        messageText = CreateText(
            statusHud.transform,
            "Message",
            "W/A/S/D 또는 방향키 이동 · Space 정지 · / 해제",
            9,
            TextColor,
            TextAnchor.MiddleLeft);
        SetRect(messageText.rectTransform,
            Vector2.zero, new Vector2(1f, 0.35f),
            new Vector2(10f, 0f), new Vector2(-10f, 0f));

        sessionText = CreateText(
            statusHud.transform,
            "Session",
            string.Empty,
            1,
            Color.clear,
            TextAnchor.MiddleLeft);
    }

    private void CreateEntryButton()
    {
        GameObject entry = CreateUiObject("ManualDriveEntry", transform);
        Image image = entry.AddComponent<Image>();
        image.color = Accent;
        Button button = entry.AddComponent<Button>();
        button.transition = Selectable.Transition.ColorTint;
        button.onClick.AddListener(ShowOverlay);

        RectTransform rect = entry.GetComponent<RectTransform>();
        rect.anchorMin = new Vector2(1f, 1f);
        rect.anchorMax = new Vector2(1f, 1f);
        rect.pivot = new Vector2(1f, 1f);
        rect.anchoredPosition = new Vector2(-14f, -14f);
        rect.sizeDelta = new Vector2(118f, 34f);

        Text text = CreateText(
            entry.transform,
            "Label",
            "MANUAL DRIVE",
            11,
            Background,
            TextAnchor.MiddleCenter);
        Stretch(text.rectTransform, Vector2.zero, Vector2.zero);
        text.fontStyle = FontStyle.Bold;
    }

    private void CreateOverlay(Transform robotPage)
    {
        overlay = CreateUiObject("TurtleBotManualDriveOverlay", robotPage);
        overlay.SetActive(false);

        RectTransform overlayRect = overlay.GetComponent<RectTransform>();
        Stretch(overlayRect, new Vector2(22f, 22f), new Vector2(-22f, -22f));

        Image background = overlay.AddComponent<Image>();
        background.color = Panel;
        Outline outline = overlay.AddComponent<Outline>();
        outline.effectColor = Accent;
        outline.effectDistance = new Vector2(2f, -2f);

        Text title = CreateText(
            overlay.transform,
            "Title",
            "TURTLEBOT MANUAL DRIVE  /  직접 수동주행",
            22,
            Accent,
            TextAnchor.MiddleLeft);
        SetRect(title.rectTransform,
            new Vector2(0f, 0.89f), new Vector2(0.72f, 1f),
            new Vector2(24f, 0f), new Vector2(-8f, -4f));
        title.fontStyle = FontStyle.Bold;

        Text endpoint = CreateText(
            overlay.transform,
            "Endpoint",
            client.EndpointUrl + "  ·  Pi DIRECT / NO ROS 2",
            12,
            Dim,
            TextAnchor.MiddleLeft);
        SetRect(endpoint.rectTransform,
            new Vector2(0f, 0.83f), new Vector2(0.72f, 0.9f),
            new Vector2(24f, 0f), new Vector2(-8f, 0f));

        Button close = CreateButton(
            overlay.transform,
            "Close",
            "CLOSE",
            Raised,
            TextColor);
        SetRect(close.GetComponent<RectTransform>(),
            new Vector2(0.88f, 0.91f), new Vector2(0.98f, 0.98f),
            Vector2.zero, Vector2.zero);
        close.onClick.AddListener(HideOverlay);

        Image statusBox = CreatePanel(overlay.transform, "StatusBox", Raised);
        SetRect(statusBox.rectTransform,
            new Vector2(0.04f, 0.63f), new Vector2(0.42f, 0.81f),
            Vector2.zero, Vector2.zero);

        stateText = CreateText(
            statusBox.transform,
            "State",
            "●  DISCONNECTED",
            18,
            Danger,
            TextAnchor.MiddleLeft);
        SetRect(stateText.rectTransform,
            new Vector2(0f, 0.56f), Vector2.one,
            new Vector2(16f, 0f), new Vector2(-16f, 0f));
        stateText.fontStyle = FontStyle.Bold;

        messageText = CreateText(
            statusBox.transform,
            "Message",
            "연결되지 않음",
            13,
            TextColor,
            TextAnchor.MiddleLeft);
        SetRect(messageText.rectTransform,
            new Vector2(0f, 0.27f), new Vector2(1f, 0.58f),
            new Vector2(16f, 0f), new Vector2(-16f, 0f));

        sessionText = CreateText(
            statusBox.transform,
            "Session",
            "SESSION  -",
            10,
            Dim,
            TextAnchor.MiddleLeft);
        SetRect(sessionText.rectTransform,
            Vector2.zero, new Vector2(1f, 0.28f),
            new Vector2(16f, 0f), new Vector2(-16f, 0f));

        connectButton = CreateButton(
            overlay.transform,
            "ConnectAndTakeControl",
            "CONNECT + TAKE CONTROL",
            Accent,
            Background);
        SetRect(connectButton.GetComponent<RectTransform>(),
            new Vector2(0.04f, 0.52f), new Vector2(0.42f, 0.61f),
            Vector2.zero, Vector2.zero);
        connectButton.onClick.AddListener(client.ConnectAndTakeControl);
        connectButtonText = connectButton.GetComponentInChildren<Text>();

        releaseButton = CreateButton(
            overlay.transform,
            "ReleaseControl",
            "STOP + RELEASE CONTROL",
            Raised,
            TextColor);
        SetRect(releaseButton.GetComponent<RectTransform>(),
            new Vector2(0.04f, 0.41f), new Vector2(0.42f, 0.50f),
            Vector2.zero, Vector2.zero);
        releaseButton.onClick.AddListener(
            () => client.ReleaseAndDisconnect("USER_RELEASE"));

        Text safety = CreateText(
            overlay.transform,
            "Safety",
            "버튼을 누르는 동안만 주행합니다. 놓는 즉시 STOP.\n" +
            "화면 전환 · 포커스 상실 · 앱 종료 시 제어권을 자동 반환합니다.",
            12,
            Dim,
            TextAnchor.UpperLeft);
        SetRect(safety.rectTransform,
            new Vector2(0.04f, 0.13f), new Vector2(0.42f, 0.37f),
            Vector2.zero, Vector2.zero);

        Text shortcut = CreateText(
            overlay.transform,
            "Shortcut",
            "KEYBOARD  W/A/S/D or ARROWS  ·  SPACE = STOP",
            10,
            Cyan,
            TextAnchor.MiddleLeft);
        SetRect(shortcut.rectTransform,
            new Vector2(0.04f, 0.05f), new Vector2(0.42f, 0.12f),
            Vector2.zero, Vector2.zero);

        CreateDrivePad();
    }

    private void CreateDrivePad()
    {
        Transform parent = overlay.transform;
        driveButtons = new Button[5];

        driveButtons[0] = CreateHoldButton(
            parent, "Forward", "▲\nFORWARD", 0.05f, 0f,
            new Vector2(0.64f, 0.61f), new Vector2(0.78f, 0.79f));
        driveButtons[1] = CreateHoldButton(
            parent, "Left", "◀\nLEFT", 0f, 0.25f,
            new Vector2(0.49f, 0.40f), new Vector2(0.63f, 0.58f));
        driveButtons[2] = CreateHoldButton(
            parent, "Right", "▶\nRIGHT", 0f, -0.25f,
            new Vector2(0.79f, 0.40f), new Vector2(0.93f, 0.58f));
        driveButtons[3] = CreateHoldButton(
            parent, "Reverse", "▼\nREVERSE", -0.04f, 0f,
            new Vector2(0.64f, 0.19f), new Vector2(0.78f, 0.37f));

        Button stop = CreateButton(parent, "Stop", "■\nSTOP", Danger, TextColor);
        SetRect(stop.GetComponent<RectTransform>(),
            new Vector2(0.64f, 0.40f), new Vector2(0.78f, 0.58f),
            Vector2.zero, Vector2.zero);
        stop.onClick.AddListener(client.StopNow);
        driveButtons[4] = stop;
    }

    private Button CreateHoldButton(
        Transform parent,
        string name,
        string label,
        float linear,
        float angular,
        Vector2 anchorMin,
        Vector2 anchorMax)
    {
        Button button = CreateButton(parent, name, label, Raised, TextColor);
        SetRect(button.GetComponent<RectTransform>(),
            anchorMin, anchorMax, Vector2.zero, Vector2.zero);
        ManualDriveHoldButton hold = button.gameObject
            .AddComponent<ManualDriveHoldButton>();
        hold.Configure(client, linear, angular);
        return button;
    }

    private void ShowOverlay()
    {
        overlay.SetActive(true);
        overlay.transform.SetAsLastSibling();
    }

    private void HideOverlay()
    {
        client.ReleaseAndDisconnect("PANEL_CLOSED");
        overlay.SetActive(false);
        keyboardDriveActive = false;
    }

    private void Update()
    {
        if (client == null)
        {
            return;
        }

        if (EventSystem.current != null &&
            EventSystem.current.currentSelectedGameObject != null &&
            EventSystem.current.currentSelectedGameObject.GetComponent<InputField>() is InputField field &&
            field.isFocused)
        {
            keyboardDriveActive = false;
            client.StopDriving();
            return;
        }

        if (Input.GetKeyDown(KeyCode.Slash) || Input.GetKeyDown(KeyCode.KeypadDivide))
        {
            ToggleManualMode();
            return;
        }

        if (manualModeRequested && client.State == TurtleBotManualDriveClient.ManualState.TimedOut)
        {
            keyboardDriveActive = false;
            // Keep the operator's manual-mode selection until explicit release.
            // Rearm only on a fresh key press, never from a held key after timeout.
            if (Input.GetKeyDown(KeyCode.W) || Input.GetKeyDown(KeyCode.UpArrow) ||
                Input.GetKeyDown(KeyCode.S) || Input.GetKeyDown(KeyCode.DownArrow) ||
                Input.GetKeyDown(KeyCode.A) || Input.GetKeyDown(KeyCode.LeftArrow) ||
                Input.GetKeyDown(KeyCode.D) || Input.GetKeyDown(KeyCode.RightArrow))
            {
                Debug.Log("[TurtleBot Manual] Fresh movement key: re-requesting control after timeout");
                client.ConnectAndTakeControl();
            }
            return;
        }

        if (!manualModeRequested || !client.CanDrive)
        {
            if (keyboardDriveActive)
            {
                keyboardDriveActive = false;
                client.StopDriving();
            }
            return;
        }

        if (Input.GetKeyDown(KeyCode.Space))
        {
            keyboardDriveActive = false;
            client.StopNow();
            return;
        }

        Vector2 drive = ReadKeyboardDrive();

        if (drive != Vector2.zero)
        {
            if (!keyboardDriveActive || drive != lastKeyboardDrive)
            {
                keyboardDriveActive = true;
                lastKeyboardDrive = drive;
                client.SetDrive(drive.x, drive.y);
            }
        }
        else if (keyboardDriveActive)
        {
            keyboardDriveActive = false;
            client.StopDriving();
        }
    }

    private void ToggleManualMode()
    {
        Debug.Log($"[TurtleBot Manual] Slash toggle: requested={!manualModeRequested}, state={client.State}");
        keyboardDriveActive = false;

        if (manualModeRequested)
        {
            manualModeRequested = false;
            client.ReleaseAndDisconnect("SLASH_TOGGLE_OFF");
            Refresh(client.State, "수동모드 해제 중...", client.SessionId);
            return;
        }

        manualModeRequested = true;
        client.ConnectAndTakeControl();
        Refresh(client.State, "수동모드 연결 요청 중...", client.SessionId);
    }

    private static Vector2 ReadKeyboardDrive()
    {
        if (Input.GetKey(KeyCode.W) || Input.GetKey(KeyCode.UpArrow))
            return new Vector2(0.05f, 0f);
        if (Input.GetKey(KeyCode.S) || Input.GetKey(KeyCode.DownArrow))
            return new Vector2(-0.04f, 0f);
        if (Input.GetKey(KeyCode.A) || Input.GetKey(KeyCode.LeftArrow))
            return new Vector2(0f, 0.25f);
        if (Input.GetKey(KeyCode.D) || Input.GetKey(KeyCode.RightArrow))
            return new Vector2(0f, -0.25f);
        return Vector2.zero;
    }

    private void HandleStatusChanged(
        TurtleBotManualDriveClient.StatusSnapshot snapshot)
    {
        Refresh(snapshot.State, snapshot.Message, snapshot.SessionId);
    }

    private void Refresh(
        TurtleBotManualDriveClient.ManualState currentState,
        string currentMessage,
        string currentSession)
    {
        if (currentState == TurtleBotManualDriveClient.ManualState.TimedOut)
        {
            keyboardDriveActive = false;
        }

        if (currentState == TurtleBotManualDriveClient.ManualState.Rejected ||
            currentState == TurtleBotManualDriveClient.ManualState.Error ||
            currentState == TurtleBotManualDriveClient.ManualState.Disconnected ||
            currentState == TurtleBotManualDriveClient.ManualState.Auto)
        {
            manualModeRequested = false;
        }

        // Manual control is intentionally invisible. There is no runtime
        // button, mode badge, help text, or connection status on the UI.
        if (stateText == null)
        {
            return;
        }

        bool canDrive = manualModeRequested && client.CanDrive;
        stateText.text = canDrive
            ? "●  ON  ·  " + ToServerStateLabel(currentState) + "  ·  / 해제"
            : manualModeRequested
                ? "●  STARTING  ·  " + ToServerStateLabel(currentState) + "  ·  / 취소"
                : "●  OFF  ·  / 키로 수동모드 시작";
        stateText.color = canDrive
            ? Cyan
            : manualModeRequested
                ? Accent
                : Dim;
        messageText.text = canDrive
            ? "W/A/S/D 또는 방향키 이동 · Space 정지 · / 수동모드 해제"
            : string.IsNullOrWhiteSpace(currentMessage)
                ? "W/A/S/D 또는 방향키 이동 · Space 정지"
                : currentMessage;

        if (sessionText != null)
        {
            sessionText.text = string.Empty;
        }
    }

    private static string ToServerStateLabel(
        TurtleBotManualDriveClient.ManualState value)
    {
        switch (value)
        {
            case TurtleBotManualDriveClient.ManualState.ManualReady:
                return "MANUAL_READY";
            case TurtleBotManualDriveClient.ManualState.ManualActive:
                return "MANUAL_ACTIVE";
            case TurtleBotManualDriveClient.ManualState.WaitingForControl:
                return "WAITING_FOR_CONTROL";
            case TurtleBotManualDriveClient.ManualState.TimedOut:
                return "TIMED_OUT";
            default:
                return value.ToString().ToUpperInvariant();
        }
    }

    private void OnDisable()
    {
        if (manualModeRequested && client != null)
        {
            manualModeRequested = false;
            client.ReleaseAndDisconnect("ROBOT_PAGE_CHANGED");
        }
        keyboardDriveActive = false;
    }

    private void OnDestroy()
    {
        if (client != null)
        {
            client.StatusChanged -= HandleStatusChanged;
        }
    }

    private static GameObject CreateUiObject(string name, Transform parent)
    {
        GameObject gameObject = new GameObject(
            name,
            typeof(RectTransform),
            typeof(CanvasRenderer));
        gameObject.layer = 5;
        gameObject.transform.SetParent(parent, false);
        return gameObject;
    }

    private static Text CreateText(
        Transform parent,
        string name,
        string value,
        int fontSize,
        Color color,
        TextAnchor alignment)
    {
        GameObject gameObject = CreateUiObject(name, parent);
        Text text = gameObject.AddComponent<Text>();
        text.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        text.text = value;
        text.fontSize = fontSize;
        text.color = color;
        text.alignment = alignment;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        text.raycastTarget = false;
        return text;
    }

    private static Button CreateButton(
        Transform parent,
        string name,
        string label,
        Color background,
        Color foreground)
    {
        GameObject gameObject = CreateUiObject(name, parent);
        Image image = gameObject.AddComponent<Image>();
        image.color = background;
        Button button = gameObject.AddComponent<Button>();
        button.targetGraphic = image;
        button.transition = Selectable.Transition.ColorTint;
        Navigation navigation = button.navigation;
        navigation.mode = Navigation.Mode.None;
        button.navigation = navigation;

        Text text = CreateText(
            gameObject.transform,
            "Label",
            label,
            12,
            foreground,
            TextAnchor.MiddleCenter);
        Stretch(text.rectTransform, new Vector2(4f, 2f), new Vector2(-4f, -2f));
        text.fontStyle = FontStyle.Bold;
        return button;
    }

    private static Image CreatePanel(
        Transform parent,
        string name,
        Color color)
    {
        GameObject gameObject = CreateUiObject(name, parent);
        Image image = gameObject.AddComponent<Image>();
        image.color = color;
        Outline outline = gameObject.AddComponent<Outline>();
        outline.effectColor = new Color32(44, 46, 54, 255);
        outline.effectDistance = new Vector2(1f, -1f);
        return image;
    }

    private static void Stretch(
        RectTransform rect,
        Vector2 offsetMin,
        Vector2 offsetMax)
    {
        rect.anchorMin = Vector2.zero;
        rect.anchorMax = Vector2.one;
        rect.pivot = new Vector2(0.5f, 0.5f);
        rect.offsetMin = offsetMin;
        rect.offsetMax = offsetMax;
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
        rect.pivot = new Vector2(0.5f, 0.5f);
        rect.offsetMin = offsetMin;
        rect.offsetMax = offsetMax;
    }
}

public sealed class ManualDriveHoldButton : MonoBehaviour,
    IPointerDownHandler,
    IPointerUpHandler,
    IPointerExitHandler,
    ICancelHandler
{
    private TurtleBotManualDriveClient client;
    private float linear;
    private float angular;
    private bool pressed;

    public void Configure(
        TurtleBotManualDriveClient manualClient,
        float linearX,
        float angularZ)
    {
        client = manualClient;
        linear = linearX;
        angular = angularZ;
    }

    public void OnPointerDown(PointerEventData eventData)
    {
        if (eventData.button != PointerEventData.InputButton.Left || client == null)
        {
            return;
        }

        pressed = true;
        client.SetDrive(linear, angular);
    }

    public void OnPointerUp(PointerEventData eventData)
    {
        Release();
    }

    public void OnPointerExit(PointerEventData eventData)
    {
        Release();
    }

    public void OnCancel(BaseEventData eventData)
    {
        Release();
    }

    private void OnDisable()
    {
        Release();
    }

    private void Release()
    {
        if (!pressed)
        {
            return;
        }

        pressed = false;
        client?.StopDriving();
    }
}

public static class TurtleBotManualDriveBootstrap
{
    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
    private static void Install()
    {
        PageManager pageManager = UnityEngine.Object
            .FindAnyObjectByType<PageManager>();

        if (pageManager == null || pageManager.robotPanel == null)
        {
            Debug.LogWarning(
                "[TurtleBot Manual] PageManager/RobotP를 찾지 못해 UI를 설치하지 못했습니다.");
            return;
        }

        Transform turtleBotCard = FindDescendant(
            pageManager.robotPanel.transform,
            "TB3P");

        if (turtleBotCard == null)
        {
            Debug.LogWarning(
                "[TurtleBot Manual] TB3P 카드를 찾지 못해 UI를 설치하지 못했습니다.");
            return;
        }

        TurtleBotManualDriveClient existingClient = UnityEngine.Object
            .FindAnyObjectByType<TurtleBotManualDriveClient>();
        TurtleBotManualDriveClient client = existingClient;

        if (client == null)
        {
            GameObject runtime = new GameObject("TurtleBotManualDriveRuntime");
            client = runtime.AddComponent<TurtleBotManualDriveClient>();
        }

        // Keyboard control must remain active when the robot page is hidden.
        if (client.GetComponent<TurtleBotManualDrivePanel>() == null)
        {
            TurtleBotManualDrivePanel panel = client.gameObject
                .AddComponent<TurtleBotManualDrivePanel>();
            panel.Build(client, pageManager.robotPanel);
        }

        Debug.Log(
            "[TurtleBot Manual] Direct manual-drive UI installed for " +
            client.EndpointUrl);
    }

    private static Transform FindDescendant(Transform root, string name)
    {
        foreach (Transform child in root)
        {
            if (child.name == name)
            {
                return child;
            }

            Transform nested = FindDescendant(child, name);
            if (nested != null)
            {
                return nested;
            }
        }

        return null;
    }
}
