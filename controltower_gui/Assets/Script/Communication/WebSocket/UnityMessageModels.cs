using System;

[Serializable]
public class UnityMessageEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
}

[Serializable]
public class ProductionSnapshotEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public ProductionSnapshotData data;
}

[Serializable]
public class ProductionSnapshotData
{
    public ProductionJobData[] jobs;
    public RobotStatusData[] robots;
    public TransportStatusData[] transports;
    public ErrorEventData[] active_errors;
    public IncomingQaStatusData[] incoming_qa;
    public ProductionInspectionStatusData[] production_inspections;
    public VoiceRuntimeData voice_runtime;
}

[Serializable]
public class ProductionJobData
{
    // UI 내부에서 사용하는 문자열 Job ID
    public string job_id;

    // 실제 서버 Job 정보
    public long numeric_job_id;
    public string job_code;
    public string product_code;
    public string status;
    public string control_state;
    public string current_stage_code;
    public string process_stage_code;
    public int process_stage_order; // 1..12; missing/null is not a valid stage.
    public string process_stage_display_name;

    public string roof_option_code;
    public string requested_at;
    public string started_at;
    public string completed_at;

    // 기존 UI 호환 필드
    public string product;
    public string job_status;

    public string current_step_id;
    public string current_operation;
    public string current_step_status;
    public int current_step_order;
    public string current_step_display_name;
    public string current_step_supply_mode;

    public string next_step_id;
    public string next_operation;
    public int next_step_order;
    public string next_step_display_name;
    public string next_step_supply_mode;

    public bool ready;
    public string readiness_reason;
    public string operator_execution_ready_at;

    public float progress;
    public string error_code;
}

[Serializable]
public class RobotStatusData
{
    public string robot_id;

    // 실제 서버 필드
    public bool connected;
    public bool ready;
    public bool busy;

    // Unity 화면 표시용 필드
    public string robot_type;
    public string state;

    public string job_id;
    public string step_id;
    public string delivery_id;

    public string current_operation;
    public string lift_state;

    // FR5 gripper telemetry. JsonUtility cannot distinguish a missing/null
    // number from zero, so FactoryStateManager fills the has_* flags through
    // JObject parsing.
    public int grip;
    public bool has_grip;

    public int grip_real;
    public bool has_grip_real;

    public float grip_real_age_s;
    public bool has_grip_real_age_s;

    public float battery;

    // 배터리 정보가 실제로 제공됐는지 구분
    public bool has_battery;

    public string error_code;
}

[Serializable]
public class TransportStatusData
{
    public string req_id;
    public string job_id;
    public string delivery_id;
    public string robot_id;

    public string task_type;
    public string phase;

    // 0.0 ~ 1.0, UI 표시용
    public float progress;

    public string result;
    public string error_code;
    public string detail;
}

[Serializable]
public class ErrorEventData
{
    public string source;

    public string job_id;
    public string step_id;
    public string delivery_id;

    public string severity;
    public string error_code;
    public string detail;

    public bool recoverable;
}

[Serializable]
public class RobotJointStateEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public RobotJointStateData data;
}

[Serializable]
public class RobotJointStateData
{
    public string source_timestamp;
    public string robot_id;

    public string[] joint_names;
    public float[] positions;

    public float[] velocities;
    public float[] efforts;
}

[Serializable]
public class MobileRobotPoseEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public MobileRobotPoseData data;
}

[Serializable]
public class MobileRobotPoseData
{
    public string source_timestamp;
    public string robot_id;
    public string frame_id;

    public PositionData position;
    public QuaternionData orientation;
}

[Serializable]
public class PositionData
{
    public float x;
    public float y;
    public float z;
}

[Serializable]
public class QuaternionData
{
    public float x;
    public float y;
    public float z;
    public float w;
}

[Serializable]
public class RobotStatusEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public RobotStatusData data;
}

[Serializable]
public class ProductionStatusEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public ProductionJobData data;
}

[Serializable]
public class ProductionStatusWireEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public ProductionJobWireData data;
}

[Serializable]
public class TransportStatusEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public TransportStatusData data;
}

[Serializable]
public class ProductionSnapshotWireEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public ProductionSnapshotWireData data;
}

[Serializable]
public class ProductionSnapshotWireData
{
    public ProductionJobWireData[] jobs;
    public RobotStatusWireData[] robots;
    public TransportStatusWireData[] transports;
    public ErrorEventWireData[] active_errors;
    public ProductionInspectionStatusData[] production_inspections;
    public VoiceRuntimeData voice_runtime;
}

[Serializable]
public class ProductionJobWireData
{
    // 실제 서버에서는 number
    public long job_id;

    public string job_code;
    public string product_code;
    public string status;
    public string control_state;

    public string roof_option_code;

    public string requested_at;
    public string started_at;
    public string completed_at;

    public string current_stage_code;
    public string process_stage_code;
    public int? process_stage_order; // 1..12; missing/null is not a valid stage.
    public string process_stage_display_name;
    public ProductionStepWireData current_step;
    public ProductionStepWireData next_step;
}

[Serializable]
public class ProductionStepWireData
{
    public long job_step_id;
    public int step_order;
    public string step_code;
    public string display_name;
    // 이전 서버 샘플 호환용. 최종 계약은 display_name을 사용합니다.
    public string step_name;
    public string operation_code;
    public string supply_mode;
    public long source_recipe_stage_id;
    public string status;
    public string started_at;
    public string completed_at;
    public string failure_reason;
    public string operator_execution_ready_at;
    public bool ready;
    public string readiness_reason;
    public bool dispatchable;
    public string dispatch_block_reason;
}

[Serializable]
public class RobotStatusWireData
{
    public string robot_id;
    public bool connected;
    public bool ready;
    public bool busy;

    // TurtleBot 서버가 제공하는 경우 상태 기반 화물 부착에 사용합니다.
    public string lift_state;
}

[Serializable]
public class RobotStatusWireEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public RobotStatusWireData data;
}

[Serializable]
public class TransportStatusWireData
{
    public string req_id;
    public long job_id;
    public long delivery_id;
    public string robot_id;
    public string task_type;
    public string phase;
    public float progress;
    // Main Server 버전에 따라 Action 종료 결과가 status 또는 result로 옵니다.
    public string status;
    public string result;
    public string error_code;
    public string detail;
}

[Serializable]
public class TransportStatusWireEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public TransportStatusWireData data;
}

[Serializable]
public class ErrorEventWireData
{
    public string source;
    public long job_id;
    public long step_id;
    public long delivery_id;
    public string severity;
    public string error_code;
    public string detail;
    public bool recoverable;
}

[Serializable]
public class ErrorEventWireEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public ErrorEventWireData data;
}

[Serializable]
public class IncomingQaStatusEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public IncomingQaStatusData data;
}

[Serializable]
public class IncomingQaStatusData
{
    public long job_id;
    public string job_gate_state;
    // 최종 계약 형식. 이전 서버의 단일 transaction도 함께 지원합니다.
    public IncomingQaTransactionData[] transactions;
    public IncomingQaTransactionData transaction;
    public IncomingQaItemData[] items;
}

[Serializable]
public class IncomingQaTransactionData
{
    public long transaction_id;
    public string request_id;
    public string mode;
    public int cycle;
    public string status;
    public int retry_count;
    public string overall_result;
    public bool production_valid;
    public string error_reason;
}

[Serializable]
public class IncomingQaItemData
{
    public long delivery_item_id;
    public string slot_id;
    public string expected_part_code;
    public string status;
    public string result;
    public string failure_type;
    public string failure_reason;
    public string[] defects;
}

// 검사 화면과 이벤트 로그가 동일한 용어를 사용합니다.
public static class IncomingQaFailureText
{
    public static string Format(IncomingQaItemData item)
    {
        if (item == null) return "불량 상세 정보 없음";
        var reasons = new System.Collections.Generic.List<string>();
        string type = (item.failure_type ?? "").Trim().ToUpperInvariant();
        if (!string.IsNullOrWhiteSpace(item.failure_reason))
            Add(reasons, Translate(item.failure_reason));
        // DEFECT는 하위 불량으로 표현하고, 복합 실패는 상위 분류도 유지합니다.
        if (type != "" && type != "DEFECT")
            Add(reasons, Translate(type));
        if (item.defects != null)
            foreach (string defect in item.defects)
                if (!string.IsNullOrWhiteSpace(defect))
                    Add(reasons, Translate(defect));
        return reasons.Count > 0
            ? string.Join(" · ", reasons)
            : "불량 상세 정보 없음";
    }

    private static void Add(System.Collections.Generic.List<string> items, string value)
    {
        if (!items.Contains(value)) items.Add(value);
    }

    public static string Translate(string value)
    {
        string text = (value ?? "").Trim();
        switch (text.ToUpperInvariant())
        {
            case "MISSING": return "자재 누락";
            case "COMPONENT_MISSING": return "구성품 누락";
            case "WRONG_CLASS": return "오투입";
            case "COLOR_NG": return "색상 불량";
            case "CRACK_DAMAGE": return "균열·파손";
            case "INCOMPLETE_FORMATION": return "미성형";
            case "QUANTITY_MISMATCH": return "수량 불일치";
            case "DEFECT": return "품질 불량";
            case "MULTIPLE_FAILURE": return "복합 불량";
            default:
                // 자유 서술 사유와 미등록 코드는 의미를 추측하지 않고 보존합니다.
                return text.Replace("<", "＜").Replace(">", "＞");
        }
    }
}


[Serializable]
public class ProductionInspectionStatusEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public ProductionInspectionStatusData data;
}

[Serializable]
public class ProductionInspectionStatusData
{
    public long job_id;
    public string inspection_type;
    public ProductionInspectionData inspection;
}

[Serializable]
public class ProductionInspectionData
{
    public long inspection_id;
    public string inspection_request_id;
    public int inspection_cycle;
    public string status;
    public string result;
    public string current_view;
    public bool vision_production_valid;
    public bool production_valid;
    public string gate_state;
    public ProductionInspectionTransportData transport;
    public ProductionInspectionViewData[] views;
    public string runtime_profile;
    public ProductionInspectionRuntimeVersions runtime_versions;
    public string requested_at;
    public string started_at;
    public string completed_at;
}

[Serializable]
public class ProductionInspectionTransportData
{
    public string request_sent_at;
    public bool acked;
    public string acked_at;
    public int retry_count;
    public string wire_error_code;
}

[Serializable]
public class ProductionInspectionViewData
{
    public string view_name;
    public string status;
    public string result;
    public string reason_code;
}

[Serializable]
public class ProductionInspectionRuntimeVersions
{
    public string controller;
    public string TOP;
    public string LEFT;
    public string RIGHT;
    public string FRONT;
    public string BEHIND;
}

[Serializable]
public class VoiceRuntimeEventEnvelope
{
    public string schema_version;
    public string type;
    public string timestamp;
    public long sequence;
    public VoiceRuntimeData data;
}

[Serializable]
public class VoiceRuntimeData
{
    public string state;

    // 아래 필드는 서버 계약상 optional입니다. turn_id와 error_message가
    // 나중에 추가돼도 기존 메시지와 함께 처리할 수 있습니다.
    public string turn_id;
    public string transcript;
    public string response_text;
    public string intent;
    public string error_message;
    public string updated_at;
    public VoiceTurnData[] recent_turns;
}

[Serializable]
public class VoiceTurnData
{
    public string turn_id;
    public string timestamp;
    public string transcript;
    public string response_text;
    public string intent;
}
