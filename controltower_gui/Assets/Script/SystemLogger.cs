using UnityEngine;
using UnityEngine.UI;
using System; // 시간을 불러오기 위해 필요

public class SystemLogger : MonoBehaviour
{
    // 인스펙터에서 Scroll View 안의 Log_Text를 연결해줍니다.
    public Text logText;

    void Start()
    {
        // 시작할 때 텍스트를 비워줍니다.
        logText.text = "";

        // 테스트용 로그 출력
        AddLog("시스템 가동 준비 완료", "INFO");
    }

    /// <summary>
    /// 시스템 로그를 추가하는 함수
    /// </summary>
    /// <param name="message">출력할 메시지 (예: "FR5 모듈 조립 시작")</param>
    /// <param name="type">로그 타입 ("INFO", "WARN", "ERROR")</param>
    public void AddLog(string message, string type = "INFO")
    {
        // 1. 현재 시간 가져오기 (예: [14:30:15])
        string time = DateTime.Now.ToString("HH:mm:ss");

        // 2. 로그 타입에 따라 아이콘이나 색상 변경
        string prefix = "";
        if (type == "INFO") prefix = "<color=white>ℹ️ [INFO]</color>";
        if (type == "WARN") prefix = "<color=yellow>⚠️ [WARN]</color>";
        if (type == "ERROR") prefix = "<color=red>❌ [ERROR]</color>";

        // 3. 기존 텍스트 아래에 새로운 로그를 한 줄 추가 (\n 은 줄바꿈)
        string newLogLine = $"[{time}] {prefix} {message}\n";
        logText.text += newLogLine;
    }

    // 테스트용: 스페이스바를 누르면 경고 로그가 추가되게 해볼까요?
    void Update()
    {
        if (Input.GetKeyDown(KeyCode.Space))
        {
            AddLog("비전 인식 실패 - 재촬영 요망", "ERROR");
        }
    }
}