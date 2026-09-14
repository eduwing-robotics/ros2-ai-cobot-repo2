"""Application configuration loaded from environment variables or .env."""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class MaterialPrefetchMode(StrEnum):
    """Worker policy for logistics work that is ahead of the execution frontier."""

    DISABLED = "disabled"
    ONE_AHEAD = "one_ahead"


class Settings(BaseSettings):
    """Configuration shared by API, FMS, and telemetry processes."""

    app_env: str = "development"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    telemetry_host: str = "0.0.0.0"
    telemetry_port: int = 8001
    vision_status_udp_host: str = "0.0.0.0"
    vision_status_udp_port: int = 20050
    # Incoming QA v0.2 uses an isolated UDP transaction channel.  These are
    # deliberately unset by default: unlike global Vision status, no port is
    # assumed safe for an inspection request/result runtime.
    vision_incoming_qa_udp_host: str | None = None
    vision_incoming_qa_udp_port: int | None = None
    fms_incoming_qa_result_udp_host: str | None = None
    fms_incoming_qa_result_udp_port: int | None = None
    incoming_qa_udp_ack_timeout_seconds: float | None = None
    incoming_qa_udp_max_retries: int | None = None
    # PRE_ROOF v0.1 is a separate UDP boundary from Incoming QA.  Every value
    # remains deliberately unset until a deployment explicitly configures it.
    vision_pre_roof_udp_host: str | None = None
    vision_pre_roof_udp_port: int | None = None
    # This is the address advertised to Vision for Final Results. It is not
    # necessarily a local socket bind address on a multi-homed server.
    fms_pre_roof_result_udp_host: str | None = None
    fms_pre_roof_result_udp_port: int | None = None
    # Local UDP listener address, deliberately separate from the advertised
    # Vision destination above. Bind all interfaces by default.
    fms_pre_roof_result_udp_bind_host: str = "0.0.0.0"
    pre_roof_udp_ack_timeout_seconds: float | None = None
    pre_roof_udp_max_retries: int | None = None
    # Incoming material inspection endpoint is deployment-specific.  Keep it
    # unset in code so a local .env/environment must choose the Vision target.
    vision_incoming_qa_base_url: str = ""
    vision_incoming_qa_request_path: str = "/api/v1/incoming-qa/requests"
    vision_incoming_qa_timeout_seconds: float = 10.0
    vision_incoming_qa_max_attempts: int = 2
    vision_incoming_qa_retry_delay_seconds: float = 0.5
    # Redis supports realtime cache and cross-process events only; PostgreSQL
    # remains the durable production source of truth.
    redis_url: str = "redis://127.0.0.1:6379/0"
    telemetry_ros_enabled: bool = False
    unity_redis_channel_prefix: str = ""
    database_url: str = ""
    # Integration tests use a separate PostgreSQL database and never fall back to
    # DATABASE_URL. Keep this empty unless a dedicated test database is prepared.
    postgres_test_database_url: str = ""
    # Explicit disposable database for process-boundary rehearsals. Application
    # runtime never selects this automatically; the rehearsal tools must opt in.
    factory_rehearsal_database_url: str = ""
    # Separate disposable DB for the operator execution-gate GUI rehearsal.
    operator_gate_rehearsal_database_url: str = ""
    pending_production_request_ttl_seconds: int = 300
    ros_domain_id: int = 73
    cell_transport: str = "fake"
    # Disabled preserves the historic step-coupled material dispatch. ONE_AHEAD
    # allows one future TRANSPORTED Delivery to prefetch independently.
    material_prefetch_mode: MaterialPrefetchMode = MaterialPrefetchMode.DISABLED
    cell_execute_task_action_name: str = "/cell/execute_task"
    cell_action_server_wait_timeout_seconds: float = 5.0
    cell_action_result_timeout_seconds: float = 60.0
    # Canonical TurtleBot identity for routing, runtime telemetry, and Unity.
    turtlebot_robot_id: str = "forklift_01"
    turtlebot_frame_id: str = "map"
    forklift_execute_transport_action_name: str = "/forklift/execute_transport"
    forklift_return_home_action_name: str = "/forklift/return_home"
    forklift_mobile_robot_pose_topic: str = "/forklift/mobile_robot_pose"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""
    ollama_timeout_seconds: float = 60
    whisper_model_size: str = "small"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    whisper_language: str = "ko"
    whisper_beam_size: int = 5
    max_audio_file_mb: int = 20
    ai_debug_response: bool = False
    # Explicitly disabled test-only lifecycle simulation. It still requires a
    # proven smart_factory_benchmark SQLAlchemy bind at the command boundary.
    test_override_enabled: bool = False
    # Emits structured Voice API stage timings to server logs only when explicitly enabled.
    voice_timing_debug: bool = False
    tts_provider: str = "edge"
    tts_voice: str = ""
    tts_rate: str = "+0%"
    tts_volume: str = "+0%"
    tts_pitch: str = "+0Hz"
    tts_timeout_seconds: float = 30
    tts_max_text_length: int = 500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached process-level settings."""

    return Settings()
