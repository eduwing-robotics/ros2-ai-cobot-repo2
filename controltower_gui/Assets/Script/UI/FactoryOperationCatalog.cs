using System;
using System.Collections.Generic;

public static class FactoryOperationCatalog
{
    public static readonly string[] StepNames =
    {
        "작업 명령 전달", "수입검사", "Zekeep 베이스 설치",
        "외벽 팔레트 운반", "FR5 외벽 설치",
        "외벽 빈 팔레트 회수\n내벽 팔레트 운반", "FR5 내벽 설치",
        "내벽 빈 팔레트 회수", "조립 결과 검사", "Zekeep 지붕 설치",
        "FR5 완성 주택 운반", "작업 완료"
    };

    private static readonly Dictionary<string, int> OperationToStep =
        new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase)
    {
        { "JOB_REQUESTED", 0 }, { "JOB_CREATED", 0 }, { "START_PRODUCTION", 0 },
        { "INCOMING_INSPECTION", 1 }, { "MATERIAL_QUALITY_INSPECTION", 1 }, { "MATERIAL_INSPECTION", 1 },
        { "INSTALL_BASE", 2 },
        { "DELIVER_OUTER_WALL", 3 }, { "DELIVER_OUTER_WALL_PALLET", 3 },
        { "INSTALL_OUTER_WALL", 4 },
        { "INSTALL_REAR_OUTER_WALL", 4 },
        { "INSTALL_DOOR_OUTER_WALL", 4 },
        { "INSTALL_LEFT_OUTER_WALL", 4 },
        { "INSTALL_RIGHT_OUTER_WALL", 4 },
        { "RETURN_OUTER_PALLET", 5 }, { "DELIVER_INNER_WALL", 5 }, { "DELIVER_INNER_WALL_PALLET", 5 },
        { "INSTALL_INNER_WALL", 6 },
        { "RETURN_INNER_PALLET", 7 },
        { "PRE_ROOF_INSPECTION", 8 }, { "FINAL_INSPECTION", 8 },
        { "STRUCTURE_QUALITY_INSPECTION", 8 }, { "PRE_ROOF_READY", 8 },
        { "INSTALL_ROOF", 9 },
        { "MOVE_COMPLETED_HOUSE", 10 }, { "HOUSE_PLACEMENT", 10 }, { "PLACE_HOUSE", 10 },
        { "JOB_COMPLETED", 11 }, { "PRODUCTION_COMPLETED", 11 }, { "WORK_COMPLETED", 11 }
    };

    private static readonly string[] ProcessCodes =
    {
        "COMMAND_RECEIVED", "INCOMING_QA", "BASE_INSTALL", "OUTER_WALL_DELIVERY", "OUTER_WALL_INSTALL", "OUTER_RETURN_INNER_DELIVERY", "INNER_WALL_INSTALL", "INNER_WALL_RETURN", "PRE_ROOF_INSPECTION", "ROOF_INSTALL", "HOUSE_OUTBOUND", "COMPLETED"
    };

    public static string JobStatus(ProductionJobData job)
    {
        return (string.IsNullOrWhiteSpace(job?.job_status) ? job?.status : job.job_status)
            ?.Trim().ToUpperInvariant() ?? string.Empty;
    }

    public static bool IsStopped(ProductionJobData job)
    {
        string status = JobStatus(job);
        return status == "FAILED" || status == "CANCELED" || status == "CANCELLED";
    }

    public static bool HasProcessStage(ProductionJobData job)
    {
        return job != null && (job.process_stage_order != 0 ||
            !string.IsNullOrWhiteSpace(job.process_stage_code));
    }

    public static bool TryGetProcessStep(ProductionJobData job, out int step)
    {
        step = -1;
        if (job == null || IsStopped(job)) return false;
        if (job.process_stage_order >= 1 && job.process_stage_order <= StepNames.Length)
        {
            step = job.process_stage_order - 1;
            return true;
        }
        for (int i = 0; i < ProcessCodes.Length; i++)
            if (string.Equals(ProcessCodes[i], job.process_stage_code, StringComparison.OrdinalIgnoreCase))
            {
                step = i;
                return true;
            }
        // Legacy messages alone fall back to the detailed operation.
        return !HasProcessStage(job) && TryGetStep(job.current_operation, out step);
    }

    public static string GetProcessDisplayName(ProductionJobData job)
    {
        if (IsStopped(job)) return JobStatus(job) == "FAILED" ? "작업 실패" : "작업 취소";
        if (!string.IsNullOrWhiteSpace(job?.process_stage_display_name))
            return job.process_stage_display_name;
        return TryGetProcessStep(job, out int step) ? StepNames[step] : "공정 정보 대기";
    }

    public static bool TryGetStep(string operation, out int step)
    {
        return OperationToStep.TryGetValue(operation ?? string.Empty, out step);
    }

    public static string GetDisplayName(string operation)
    {
        return TryGetStep(operation, out int step)
            ? StepNames[step]
            : string.IsNullOrWhiteSpace(operation) ? "공정 정보 대기" : operation;
    }
}
