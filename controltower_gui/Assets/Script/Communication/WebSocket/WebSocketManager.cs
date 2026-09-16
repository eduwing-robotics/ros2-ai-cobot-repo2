using System;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

public class WebSocketManager : MonoBehaviour
{
    public enum ConnectionState
    {
        Disconnected,
        Connecting,
        Syncing,
        Connected,
        Stale
    }

    [Header("Server Configuration")]
    [SerializeField]
    private string serverUrl =
        "ws://127.0.0.1:8000/ws/unity";

    [SerializeField]
    private bool connectOnStart = true;

    [Header("Debug")]
    [SerializeField]
    private bool logRawMessages = false;

    public ConnectionState CurrentState { get; private set; }
        = ConnectionState.Disconnected;

    public event Action<ConnectionState> StateChanged;
    public event Action<string> MessageReceived;

    private readonly int[] reconnectDelays =
    {
        1, 2, 5
    };

    private ClientWebSocket socket;
    private CancellationTokenSource destroyTokenSource;
    private Task connectionTask;

    private void Awake()
    {
        // Scene이 변경돼도 통신 관리자를 유지하려면 사용합니다.
        // 여러 Scene에서 WebSocketManager를 중복 생성하면 안 됩니다.
        DontDestroyOnLoad(gameObject);
    }

    private void Start()
    {
        destroyTokenSource = new CancellationTokenSource();

        if (connectOnStart)
        {
            Connect();
        }
    }

    public void Connect()
    {
        if (destroyTokenSource == null)
        {
            destroyTokenSource = new CancellationTokenSource();
        }

        if (connectionTask != null &&
            !connectionTask.IsCompleted)
        {
            Debug.LogWarning(
                "[WebSocket] 이미 연결 작업이 실행 중입니다.");
            return;
        }

        if (!IsValidServerUrl())
        {
            Debug.LogError(
                $"[WebSocket] 잘못된 서버 주소: {serverUrl}");
            return;
        }

        connectionTask =
            RunConnectionLoopAsync(
                destroyTokenSource.Token);
    }

    private bool IsValidServerUrl()
    {
        if (!Uri.TryCreate(
                serverUrl,
                UriKind.Absolute,
                out Uri uri))
        {
            return false;
        }

        return uri.Scheme == "ws" ||
               uri.Scheme == "wss";
    }

    private async Task RunConnectionLoopAsync(
        CancellationToken cancellationToken)
    {
        int retryIndex = 0;

        while (!cancellationToken.IsCancellationRequested)
        {
            socket = new ClientWebSocket();

            try
            {
                SetState(ConnectionState.Connecting);

                Debug.Log(
                    $"[WebSocket] 연결 시도: {serverUrl}");

                await socket.ConnectAsync(
                    new Uri(serverUrl),
                    cancellationToken);

                retryIndex = 0;

                // WebSocket 연결은 성공했지만,
                // production_snapshot은 아직 받지 않은 상태입니다.
                SetState(ConnectionState.Syncing);

                Debug.Log(
                    "[WebSocket] 연결 성공, Snapshot 대기 중");

                await ReceiveLoopAsync(
                    socket,
                    cancellationToken);
            }
            catch (OperationCanceledException)
            {
                // Unity 종료 또는 연결 중단 시 발생합니다.
                break;
            }
            catch (Exception exception)
            {
                Debug.LogWarning(
                    $"[WebSocket] 연결 오류: " +
                    $"{exception.Message}");
            }
            finally
            {
                if (socket != null)
                {
                    socket.Dispose();
                    socket = null;
                }
            }

            if (cancellationToken.IsCancellationRequested)
            {
                break;
            }

            SetState(ConnectionState.Stale);

            int delaySeconds =
                reconnectDelays[
                    Mathf.Min(
                        retryIndex,
                        reconnectDelays.Length - 1)];

            retryIndex++;

            Debug.LogWarning(
                $"[WebSocket] {delaySeconds}초 후 재연결");

            try
            {
                await Task.Delay(
                    TimeSpan.FromSeconds(delaySeconds),
                    cancellationToken);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }

        SetState(ConnectionState.Disconnected);
    }

    private async Task ReceiveLoopAsync(
        ClientWebSocket connectedSocket,
        CancellationToken cancellationToken)
    {
        byte[] buffer = new byte[8192];

        while (
            connectedSocket.State == WebSocketState.Open &&
            !cancellationToken.IsCancellationRequested)
        {
            using MemoryStream messageStream =
                new MemoryStream();

            WebSocketReceiveResult result;

            do
            {
                result = await connectedSocket.ReceiveAsync(
                    new ArraySegment<byte>(buffer),
                    cancellationToken);

                if (result.MessageType ==
                    WebSocketMessageType.Close)
                {
                    Debug.LogWarning(
                        "[WebSocket] 서버가 연결을 종료했습니다.");

                    return;
                }

                if (result.MessageType ==
                    WebSocketMessageType.Text)
                {
                    messageStream.Write(
                        buffer,
                        0,
                        result.Count);
                }
            }
            while (!result.EndOfMessage);

            if (result.MessageType !=
                WebSocketMessageType.Text)
            {
                continue;
            }

            string jsonMessage =
                Encoding.UTF8.GetString(
                    messageStream.ToArray());

            if (logRawMessages)
            {
                Debug.Log(
                    $"[WebSocket] 수신: {jsonMessage}");
            }

            MessageReceived?.Invoke(jsonMessage);
        }
    }
    public void RequestResync(string reason)
    {
        Debug.LogWarning(
            $"[WebSocket] 전체 재동기화 요청: {reason}");

        // 현재 연결을 강제로 종료하면 기존 연결 루프가
        // 재연결하고 새로운 Snapshot을 다시 받습니다.
        if (socket != null)
        {
            socket.Abort();
        }
    }

    // 다음 단계에서 정상 production_snapshot을
    // 검증한 후 호출합니다.
    public void MarkSynchronized()
    {
        if (CurrentState != ConnectionState.Syncing)
        {
            return;
        }

        SetState(ConnectionState.Connected);

        Debug.Log(
            "[WebSocket] 초기 동기화 완료");
    }

    private void SetState(ConnectionState newState)
    {
        if (CurrentState == newState)
        {
            return;
        }

        CurrentState = newState;
        StateChanged?.Invoke(newState);

        Debug.Log(
            $"[WebSocket] 상태 변경: {newState}");
    }

    private async void OnDestroy()
    {
        if (destroyTokenSource == null)
        {
            return;
        }

        destroyTokenSource.Cancel();

        if (socket != null &&
            socket.State == WebSocketState.Open)
        {
            try
            {
                await socket.CloseAsync(
                    WebSocketCloseStatus.NormalClosure,
                    "Unity closing",
                    CancellationToken.None);
            }
            catch
            {
                // 종료 과정의 예외는 무시합니다.
            }
        }

        destroyTokenSource.Dispose();
        destroyTokenSource = null;
    }
}