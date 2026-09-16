using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using UnityEngine;

public class MainServerWebSocketClient : MonoBehaviour
{
    [SerializeField]
    private string serverUrl = "ws://192.168.20.20:8000/ws/unity";

    private ClientWebSocket socket;
    private CancellationTokenSource cancellation;

    // WebSocket 수신은 별도 Thread에서 실행되므로
    // Unity Main Thread에서 처리하기 위한 Queue
    private readonly ConcurrentQueue<string> messageQueue = new();

    private long lastSequence;
    private bool synchronized;
    private bool reconnectRequested;

    private readonly int[] reconnectDelays = { 1, 2, 5 };

    public event Action<JObject> ProductionSnapshotReceived;
    public event Action<JObject> ProductionStatusReceived;
    public event Action<JObject> RobotStatusReceived;
    public event Action<JObject> RobotJointStateReceived;
    public event Action<JObject> MobileRobotPoseReceived;
    public event Action<JObject> TransportStatusReceived;
    public event Action<JObject> ErrorEventReceived;

    private void Start()
    {
        cancellation = new CancellationTokenSource();
        _ = ConnectionLoopAsync(cancellation.Token);
    }

    private void Update()
    {
        while (messageQueue.TryDequeue(out string json))
        {
            ProcessMessage(json);
        }

        if (reconnectRequested)
        {
            reconnectRequested = false;
            socket?.Abort();
        }
    }

    private async Task ConnectionLoopAsync(CancellationToken token)
    {
        int retryIndex = 0;

        while (!token.IsCancellationRequested)
        {
            socket?.Dispose();
            socket = new ClientWebSocket();

            lastSequence = 0;
            synchronized = false;

            try
            {
                Debug.Log($"[WebSocket] 연결 시도: {serverUrl}");

                await socket.ConnectAsync(new Uri(serverUrl), token);

                Debug.Log("[WebSocket] 서버 연결 성공");
                retryIndex = 0;

                await ReceiveLoopAsync(socket, token);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception exception)
            {
                Debug.LogWarning($"[WebSocket] 연결 오류: {exception.Message}");
            }

            if (token.IsCancellationRequested)
                break;

            int delay = reconnectDelays[
                Mathf.Min(retryIndex, reconnectDelays.Length - 1)
            ];

            retryIndex++;

            Debug.LogWarning($"[WebSocket] {delay}초 후 재연결");
            await Task.Delay(TimeSpan.FromSeconds(delay), token);
        }
    }

    private async Task ReceiveLoopAsync(
        ClientWebSocket activeSocket,
        CancellationToken token)
    {
        byte[] buffer = new byte[8192];

        while (activeSocket.State == WebSocketState.Open &&
               !token.IsCancellationRequested)
        {
            using MemoryStream stream = new();

            WebSocketReceiveResult result;

            do
            {
                result = await activeSocket.ReceiveAsync(
                    new ArraySegment<byte>(buffer),
                    token
                );

                if (result.MessageType == WebSocketMessageType.Close)
                {
                    Debug.LogWarning("[WebSocket] 서버가 연결을 종료함");
                    return;
                }

                stream.Write(buffer, 0, result.Count);
            }
            while (!result.EndOfMessage);

            if (result.MessageType == WebSocketMessageType.Text)
            {
                string json = Encoding.UTF8.GetString(stream.ToArray());
                messageQueue.Enqueue(json);
            }
        }
    }

    private void ProcessMessage(string json)
    {
        try
        {
            JObject message = JObject.Parse(json);

            string schemaVersion =
                message.Value<string>("schema_version");

            string type =
                message.Value<string>("type");

            long sequence =
                message.Value<long>("sequence");

            JObject data =
                message["data"] as JObject;

            // Schema major version 확인
            string majorVersion = schemaVersion?.Split('.')[0];

            if (majorVersion != "1")
            {
                Debug.LogError(
                    $"[WebSocket] 지원하지 않는 Schema: {schemaVersion}"
                );
                return;
            }

            // 첫 메시지 검증
            if (!synchronized)
            {
                if (sequence != 1 ||
                    type != "production_snapshot")
                {
                    Debug.LogError(
                        "[WebSocket] 첫 메시지가 Snapshot이 아님. 재연결"
                    );

                    reconnectRequested = true;
                    return;
                }

                synchronized = true;
                lastSequence = sequence;

                Debug.Log("[WebSocket] Initial Snapshot 동기화 완료");
            }
            else
            {
                // 중복 또는 오래된 메시지
                if (sequence <= lastSequence)
                {
                    Debug.LogWarning(
                        $"[WebSocket] 오래된 메시지 폐기: {sequence}"
                    );
                    return;
                }

                // 메시지 누락
                if (sequence != lastSequence + 1)
                {
                    Debug.LogError(
                        $"[WebSocket] Sequence 누락: " +
                        $"예상={lastSequence + 1}, 수신={sequence}"
                    );

                    reconnectRequested = true;
                    return;
                }

                lastSequence = sequence;
            }

            DispatchMessage(type, data, sequence);
        }
        catch (Exception exception)
        {
            Debug.LogError(
                $"[WebSocket] JSON 처리 실패: {exception.Message}\n{json}"
            );
        }
    }

    private void DispatchMessage(
        string type,
        JObject data,
        long sequence)
    {
        switch (type)
        {
            case "production_snapshot":
                Debug.Log(
                    $"[WS #{sequence}] Production Snapshot"
                );
                ProductionSnapshotReceived?.Invoke(data);
                break;

            case "production_status":
                Debug.Log(
                    $"[WS #{sequence}] Production Status"
                );
                ProductionStatusReceived?.Invoke(data);
                break;

            case "robot_status":
                Debug.Log(
                    $"[WS #{sequence}] Robot Status: " +
                    $"{data?["robot_id"]}"
                );
                RobotStatusReceived?.Invoke(data);
                break;

            case "robot_joint_state":
                Debug.Log(
                    $"[WS #{sequence}] Joint State: " +
                    $"{data?["robot_id"]}"
                );
                RobotJointStateReceived?.Invoke(data);
                break;

            case "mobile_robot_pose":
                MobileRobotPoseReceived?.Invoke(data);
                break;

            case "transport_status":
                TransportStatusReceived?.Invoke(data);
                break;

            case "error_event":
                ErrorEventReceived?.Invoke(data);
                break;

            default:
                Debug.LogWarning(
                    $"[WebSocket] 알 수 없는 type: {type}"
                );
                break;
        }
    }

    private void OnDestroy()
    {
        cancellation?.Cancel();
        socket?.Abort();
        socket?.Dispose();
        cancellation?.Dispose();
    }
}