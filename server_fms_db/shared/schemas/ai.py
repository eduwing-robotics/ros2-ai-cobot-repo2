from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.enums.ai import Intent
from shared.models.factory import PendingProductionState, RoofOptionCode
from shared.schemas.inventory import VoiceInventoryItem

CHANGING_INTENTS = {Intent.CREATE_PRODUCTION_REQUEST, Intent.PAUSE_JOB, Intent.RESUME_JOB, Intent.CANCEL_JOB}


class TextInterpretRequest(BaseModel):
    text: str = Field(max_length=2000)

    @model_validator(mode="after")
    def validate_text(self) -> "TextInterpretRequest":
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("텍스트 명령은 비어 있을 수 없습니다.")
        return self


class StructuredCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    product_name: str | None = None
    product_code: str | None = None
    roof_option_code: RoofOptionCode | None = None
    quantity: int | None = Field(default=None, ge=1)
    target_job_id: str | None = None
    inventory_scope: str | None = None
    item_name: str | None = None
    category_name: str | None = None
    requires_confirmation: bool = False
    clarification_needed: bool = False
    clarification_message: str | None = None

    @model_validator(mode="after")
    def validate_command(self) -> "StructuredCommand":
        if self.intent in {Intent.UNKNOWN} and not self.clarification_needed:
            raise ValueError("UNKNOWN 명령은 추가 질문이 필요합니다.")
        if self.clarification_needed and not self.clarification_message:
            raise ValueError("추가 질문 메시지가 필요합니다.")
        if self.intent == Intent.CREATE_PRODUCTION_REQUEST and not self.product_name and not self.clarification_needed:
            raise ValueError("생산 요청에는 제품 또는 추가 질문이 필요합니다.")
        if self.intent == Intent.QUERY_JOB_STATUS and self.requires_confirmation:
            raise ValueError("상태 조회에는 확인이 필요하지 않습니다.")
        if self.intent in CHANGING_INTENTS and not self.clarification_needed and not self.requires_confirmation:
            raise ValueError("상태 변경 명령에는 사용자 확인이 필요합니다.")
        return self


class InterpretResponse(BaseModel):
    original_text: str
    normalized_text: str
    command: StructuredCommand
    model: str
    processing_time_ms: float
    raw_model_output: str | None = None
    suggested_response_text: str | None = None
    inventory_result: list[VoiceInventoryItem] | None = None


class TranscriptionResponse(BaseModel):
    text: str
    language: str | None = None
    language_probability: float | None = None
    duration_seconds: float | None = None
    processing_time_ms: float


class VoiceCommandResponse(BaseModel):
    transcription: TranscriptionResponse
    command: StructuredCommand
    model: str
    total_processing_time_ms: float
    suggested_response_text: str | None = None
    inventory_result: list[VoiceInventoryItem] | None = None


class TTSRequest(BaseModel):
    text: str = Field(max_length=500)

    @model_validator(mode="after")
    def validate_text(self) -> "TTSRequest":
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("TTS 텍스트는 비어 있을 수 없습니다.")
        return self

class TTSPreviewRequest(BaseModel):
    intent: Intent
    result: dict


class ConversationRequest(TextInterpretRequest):
    session_id: str = Field(max_length=100)

    @model_validator(mode="after")
    def validate_session_id(self) -> "ConversationRequest":
        self.session_id = self.session_id.strip()
        if not self.session_id:
            raise ValueError("세션 ID는 비어 있을 수 없습니다.")
        return self


class ConversationResponse(BaseModel):
    session_id: str
    message: str
    pending_request_id: int | None = None
    conversation_state: PendingProductionState | None = None
    command: StructuredCommand | None = None
    production_job_ids: list[int] = Field(default_factory=list)


class VoiceRuntimeDiagnosticEventRequest(BaseModel):
    """Best-effort, non-authoritative event from the microphone runtime."""

    turn_id: str | None = Field(default=None, min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=50)
    runtime_status: str | None = Field(default=None, max_length=50)
    session_id: str | None = Field(default=None, max_length=100)
    mic_active: bool | None = None
    wake_detected: bool | None = None
    speech_detected: bool | None = None
    transcript: str | None = Field(default=None, max_length=4000)
    intent: str | None = Field(default=None, max_length=100)
    parsed_command_summary: str | None = Field(default=None, max_length=1000)
    confirmation_state: str | None = Field(default=None, max_length=100)
    response_text: str | None = Field(default=None, max_length=4000)
    error: str | None = Field(default=None, max_length=1000)
    latencies_ms: dict[str, float] | None = None


class VoiceRuntimeTurnResponse(BaseModel):
    turn_id: str
    started_at: datetime
    ended_at: datetime | None
    state: str
    session_id: str | None
    wake_detected: bool
    speech_detected: bool
    transcript: str | None
    intent: str | None
    parsed_command_summary: str | None
    confirmation_state: str | None
    response_text: str | None
    error: str | None
    latencies_ms: dict[str, float]


class VoiceRuntimeMonitorResponse(BaseModel):
    runtime_status: str
    mic_active: bool
    current_turn: VoiceRuntimeTurnResponse | None
    recent_turns: list[VoiceRuntimeTurnResponse]
