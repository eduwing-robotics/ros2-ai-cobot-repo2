"""AI command interpretation and speech-to-text endpoints."""
from __future__ import annotations
import time
import asyncio
from typing import Annotated
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Form, Request
from fastapi.responses import Response
from api_server.services.command_interpreter import CommandInterpreter, LLMResponseFormatError
from api_server.services.llm_service import LLMConfigurationError, LLMConnectionError, LLMTimeoutError, get_llm_service
from api_server.services.stt_service import AudioTooLargeError, AudioValidationError, STTInferenceError, STTModelLoadError, STTService, get_stt_service
from shared.config import get_settings
from shared.schemas.ai import ConversationRequest, ConversationResponse, InterpretResponse, TextInterpretRequest, TranscriptionResponse, VoiceCommandResponse, TTSRequest, TTSPreviewRequest, VoiceRuntimeDiagnosticEventRequest, VoiceRuntimeMonitorResponse
from api_server.services.tts.service import TTSConfigurationError, TTSConnectionError, TTSSynthesisError, TTSTimeoutError, get_tts_service
from api_server.services.response_message_builder import ResponseMessageBuilder
from api_server.services.production_conversation_service import ProductionConversationService
from api_server.services.inventory_voice_query import InventoryVoiceQueryService
from api_server.services.voice_timing import VoiceTiming, activate_voice_timing, voice_timing_stage
from api_server.services.voice_runtime_monitor import (
    get_voice_runtime_monitor,
    voice_runtime_realtime_event,
)
from shared.services.production_request_materialization_service import (
    PendingMaterializationInvariantError,
    PendingMaterializationStateError,
    ProductionRequestMaterializationService,
)
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService
from shared.services.pending_production_request_service import (
    ActivePendingProductionRequestExistsError,
    InvalidPendingProductionRequestInputError,
    InvalidPendingProductionRequestStateTransitionError,
    PendingProductionRequestNotFoundError,
    PendingProductionRequestService,
)
from shared.services.product_lookup_service import InvalidProductCodeError, ProductNotFoundError
from sqlalchemy.orm import Session
from sqlalchemy import select
from shared.enums.ai import Intent
from shared.schemas.ai import StructuredCommand
from shared.models.factory import Part
from shared.schemas.inventory import InventoryResponse
from shared.services.inventory_service import InventoryService, PartNotFoundError, InvalidInventoryInputError
from api_server.routers.inventory import get_db, get_inventory_service
from shared.services.production_status_query_service import ProductionStatusQueryService
from shared.services.production_control_service import ProductionControlService

router = APIRouter(prefix="/ai", tags=["AI"])
def get_interpreter() -> CommandInterpreter: return CommandInterpreter(get_llm_service())
def get_conversation_service(
    db: Session = Depends(get_db),
    interpreter: CommandInterpreter = Depends(get_interpreter),
    inventory_service: InventoryService = Depends(get_inventory_service),
) -> ProductionConversationService:
    # Direct unit callers may invoke this dependency factory without FastAPI
    # resolving its optional dependency defaults. Keep that established seam.
    if not isinstance(inventory_service, InventoryService):
        inventory_service = InventoryService(db)
    return ProductionConversationService(
        pending_service=PendingProductionRequestService(db),
        interpreter=interpreter,
        materialization_service=ProductionRequestMaterializationService(db),
        preflight_service=ProductionInventoryPreflightService(db),
        status_query_service=ProductionStatusQueryService(db),
        inventory_query_service=InventoryVoiceQueryService(inventory_service),
        control_service=ProductionControlService(db),
    )

def llm_http_error(exc: Exception):
    if isinstance(exc, LLMConfigurationError): raise HTTPException(503, "OLLAMA_MODEL 설정이 필요합니다.") from exc
    if isinstance(exc, LLMConnectionError): raise HTTPException(503, "Ollama 서버에 연결할 수 없습니다.") from exc
    if isinstance(exc, LLMTimeoutError): raise HTTPException(504, "Ollama 응답 시간이 초과되었습니다.") from exc
    if isinstance(exc, LLMResponseFormatError): raise HTTPException(502, "LLM 응답 형식이 올바르지 않습니다.") from exc
    raise exc
def _record_voice_event(event: VoiceRuntimeDiagnosticEventRequest, request: Request | None = None) -> None:
    """Record diagnostics and schedule Unity observation without a Voice dependency."""
    get_voice_runtime_monitor().record(event)
    application = request.scope.get("app") if request is not None else None
    hub = getattr(application, "state", None)
    publisher = getattr(hub, "unity_hub", None)
    publish = getattr(publisher, "publish_voice_event", None)
    if not callable(publish):
        return
    try:
        task = asyncio.create_task(publish(voice_runtime_realtime_event(event)))
    except Exception:
        return

    def consume(done: asyncio.Task) -> None:
        try:
            done.result()
        except Exception:
            # Unity observability must never affect Voice API outcomes.
            pass

    task.add_done_callback(consume)


@router.post('/debug/voice-runtime/events', status_code=202)
async def record_voice_runtime_event(
    event: VoiceRuntimeDiagnosticEventRequest, request: Request,
) -> dict[str, bool]:
    """Best-effort microphone-runtime diagnostics; process-local only."""
    _record_voice_event(event, request)
    # Yield once so the best-effort task can enqueue a local fast client; never
    # await a websocket send or let observability determine this response.
    await asyncio.sleep(0)
    return {"accepted": True}


@router.get('/debug/voice-runtime', response_model=VoiceRuntimeMonitorResponse)
async def get_voice_runtime_debug() -> VoiceRuntimeMonitorResponse:
    """Read-only snapshot: never starts STT, TTS, or business execution."""
    return get_voice_runtime_monitor().snapshot()


def _voice_command_summary(command: StructuredCommand | None) -> str | None:
    if command is None:
        return None
    fields = [f"intent={command.intent.value}"]
    for name in ("product_code", "quantity", "roof_option_code", "target_job_id", "inventory_scope"):
        value = getattr(command, name)
        if value is not None:
            fields.append(f"{name}={getattr(value, 'value', value)}")
    return ", ".join(fields)


@router.get('/health')
async def health():
    settings = get_settings(); llm = get_llm_service(); stt = get_stt_service()
    return {"service":"ai", "ollama_configured":bool(settings.ollama_model.strip()), "ollama_reachable": await llm.reachable() if settings.ollama_model.strip() else False, "ollama_model":settings.ollama_model or None, "stt_model":settings.whisper_model_size, "stt_device":settings.whisper_device, "stt_loaded":stt.loaded}

def _fetch_inventory_for_command(command: StructuredCommand, db: Session, service: InventoryService) -> list[InventoryResponse] | None:
    # Retain the existing VoiceCommand/Interpret structured-result contract while
    # sharing the exact same read-only projection used by Voice conversation
    # narration. ``db`` remains in this signature for route compatibility.
    del db
    return InventoryVoiceQueryService(service).fetch(command)

@router.post('/conversation', response_model=ConversationResponse)
async def conversation(
    request: ConversationRequest,
    service: Annotated[ProductionConversationService, Depends(get_conversation_service)],
):
    try:
        result = await service.handle_text(session_id=request.session_id, text=request.text)
    except (LLMConfigurationError, LLMConnectionError, LLMTimeoutError, LLMResponseFormatError) as exc:
        llm_http_error(exc)
    except (InvalidPendingProductionRequestInputError, InvalidProductCodeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except (PendingProductionRequestNotFoundError, ProductNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ActivePendingProductionRequestExistsError, InvalidPendingProductionRequestStateTransitionError, PendingMaterializationInvariantError, PendingMaterializationStateError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return ConversationResponse(
        session_id=result.session_id,
        message=result.message,
        pending_request_id=result.pending.request_id if result.pending is not None else None,
        conversation_state=result.pending.state if result.pending is not None else None,
        command=result.command,
        production_job_ids=[job.job_id for job in result.production_jobs],
    )


@router.post('/interpret', response_model=InterpretResponse)
async def interpret(request: TextInterpretRequest, interpreter: Annotated[CommandInterpreter, Depends(get_interpreter)], db: Annotated[Session, Depends(get_db)], inventory_service: Annotated[InventoryService, Depends(get_inventory_service)]):
    started=time.perf_counter()
    try: normalized, command, raw = await interpreter.interpret(request.text)
    except (LLMConfigurationError, LLMConnectionError, LLMTimeoutError, LLMResponseFormatError) as exc: llm_http_error(exc)

    inventory_result = _fetch_inventory_for_command(command, db, inventory_service)

    response=InterpretResponse(original_text=request.text, normalized_text=normalized, command=command, model=get_settings().ollama_model, processing_time_ms=(time.perf_counter()-started)*1000, suggested_response_text=ResponseMessageBuilder().build_interpretation(command), inventory_result=inventory_result)
    if get_settings().ai_debug_response: response.raw_model_output=raw
    return response
async def do_transcribe(stt: STTService, audio: UploadFile):
    started=time.perf_counter()
    try: result=await stt.transcribe_upload(audio)
    except AudioTooLargeError as exc: raise HTTPException(413, str(exc)) from exc
    except AudioValidationError as exc: raise HTTPException(422, str(exc)) from exc
    except STTModelLoadError as exc: raise HTTPException(503, str(exc)) from exc
    except STTInferenceError as exc: raise HTTPException(500, "음성 변환 중 오류가 발생했습니다.") from exc
    result.processing_time_ms=(time.perf_counter()-started)*1000
    return result
@router.post('/transcribe', response_model=TranscriptionResponse)
async def transcribe(audio: Annotated[UploadFile, File(...)], stt: Annotated[STTService, Depends(get_stt_service)]): return await do_transcribe(stt,audio)
@router.post('/voice-command', response_model=VoiceCommandResponse)
async def voice_command(
    audio: Annotated[UploadFile, File(...)],
    stt: Annotated[STTService, Depends(get_stt_service)],
    interpreter: Annotated[CommandInterpreter, Depends(get_interpreter)],
    db: Annotated[Session, Depends(get_db)],
    inventory_service: Annotated[InventoryService, Depends(get_inventory_service)],
    http_request: Request,
):
    settings = get_settings()
    timing = VoiceTiming(
        endpoint="/ai/voice-command",
        correlation_id=http_request.headers.get("X-Voice-E2E-Turn"),
    ) if settings.voice_timing_debug else None
    with activate_voice_timing(timing):
        started = time.perf_counter()
        with voice_timing_stage("stt_total_ms"):
            transcription = await do_transcribe(stt, audio)
        try:
            with voice_timing_stage("command_total_ms"):
                _, command, _ = await interpreter.interpret(transcription.text)
        except (LLMConfigurationError, LLMConnectionError, LLMTimeoutError, LLMResponseFormatError) as exc:
            if timing is not None:
                timing.record(error=type(exc).__name__)
            llm_http_error(exc)
        with voice_timing_stage("inventory_query_db_ms"):
            inventory_result = _fetch_inventory_for_command(command, db, inventory_service)
        with voice_timing_stage("response_build_ms"):
            response = VoiceCommandResponse(
                transcription=transcription,
                command=command,
                model=settings.ollama_model,
                total_processing_time_ms=(time.perf_counter()-started)*1000,
                suggested_response_text=ResponseMessageBuilder().build_interpretation(command),
                inventory_result=inventory_result,
            )
    if timing is not None:
        timing.record(intent=command.intent.value)
    return response


@router.post('/voice-conversation', response_model=ConversationResponse)
async def voice_conversation(
    session_id: Annotated[str, Form(...)],
    audio: Annotated[UploadFile, File(...)],
    stt: Annotated[STTService, Depends(get_stt_service)],
    service: Annotated[ProductionConversationService, Depends(get_conversation_service)],
    http_request: Request,
):
    settings = get_settings()
    record_voice_event = lambda event: _record_voice_event(event, http_request)
    turn_id = http_request.headers.get("X-Voice-Runtime-Turn-ID") or session_id
    record_voice_event(VoiceRuntimeDiagnosticEventRequest(
        turn_id=turn_id, state="TRANSCRIBING", runtime_status="PROCESSING",
        session_id=session_id, mic_active=False,
    ))
    timing = VoiceTiming(
        endpoint="/ai/voice-conversation",
        correlation_id=http_request.headers.get("X-Voice-E2E-Turn") or session_id,
    ) if settings.voice_timing_debug else None
    started = time.perf_counter()
    try:
        with activate_voice_timing(timing):
            try:
                with voice_timing_stage("stt_total_ms"):
                    transcription = await do_transcribe(stt, audio)
            except HTTPException as exc:
                record_voice_event(VoiceRuntimeDiagnosticEventRequest(
                    turn_id=turn_id, state="ERROR", runtime_status="READY", session_id=session_id,
                    error=f"STT_HTTP_{exc.status_code}", latencies_ms={"total": (time.perf_counter() - started) * 1000},
                ))
                raise
            record_voice_event(VoiceRuntimeDiagnosticEventRequest(
                turn_id=turn_id, state="INTERPRETING", runtime_status="PROCESSING", session_id=session_id,
                transcript=transcription.text, latencies_ms={"stt": transcription.processing_time_ms},
            ))
            try:
                with voice_timing_stage("conversation_total_ms"):
                    result = await service.handle_text(session_id=session_id, text=transcription.text)
            except (LLMConfigurationError, LLMConnectionError, LLMTimeoutError, LLMResponseFormatError) as exc:
                if timing is not None:
                    timing.record(error=type(exc).__name__)
                record_voice_event(VoiceRuntimeDiagnosticEventRequest(
                    turn_id=turn_id, state="ERROR", runtime_status="READY", session_id=session_id,
                    error=type(exc).__name__, latencies_ms={"total": (time.perf_counter() - started) * 1000},
                ))
                llm_http_error(exc)
            except (InvalidPendingProductionRequestInputError, InvalidProductCodeError) as exc:
                record_voice_event(VoiceRuntimeDiagnosticEventRequest(turn_id=turn_id, state="ERROR", runtime_status="READY", session_id=session_id, error=type(exc).__name__))
                raise HTTPException(422, str(exc)) from exc
            except (PendingProductionRequestNotFoundError, ProductNotFoundError) as exc:
                record_voice_event(VoiceRuntimeDiagnosticEventRequest(turn_id=turn_id, state="ERROR", runtime_status="READY", session_id=session_id, error=type(exc).__name__))
                raise HTTPException(404, str(exc)) from exc
            except (ActivePendingProductionRequestExistsError, InvalidPendingProductionRequestStateTransitionError, PendingMaterializationInvariantError, PendingMaterializationStateError) as exc:
                record_voice_event(VoiceRuntimeDiagnosticEventRequest(turn_id=turn_id, state="ERROR", runtime_status="READY", session_id=session_id, error=type(exc).__name__))
                raise HTTPException(409, str(exc)) from exc
            with voice_timing_stage("response_build_ms"):
                response = ConversationResponse(
                    session_id=result.session_id,
                    message=result.message,
                    pending_request_id=result.pending.request_id if result.pending is not None else None,
                    conversation_state=result.pending.state if result.pending is not None else None,
                    command=result.command,
                    production_job_ids=[job.job_id for job in result.production_jobs],
                )
    except HTTPException:
        raise
    state_value = response.conversation_state.value if response.conversation_state is not None else None
    record_voice_event(VoiceRuntimeDiagnosticEventRequest(
        turn_id=turn_id,
        state="WAITING_CONFIRMATION" if state_value in {"COLLECTING_DETAILS", "WAITING_ROOF_OPTION", "AWAITING_CONFIRMATION"} else "RESPONDING",
        runtime_status="READY" if state_value in {"COLLECTING_DETAILS", "WAITING_ROOF_OPTION", "AWAITING_CONFIRMATION"} else "RESPONDING",
        session_id=session_id,
        transcript=transcription.text,
        intent=response.command.intent.value if response.command is not None else None,
        parsed_command_summary=_voice_command_summary(response.command),
        confirmation_state=state_value,
        response_text=response.message,
        latencies_ms={"total": (time.perf_counter() - started) * 1000},
    ))
    if timing is not None:
        timing.record(
            session_id=session_id,
            conversation_state=state_value,
            intent=response.command.intent.value if response.command is not None else None,
        )
    return response



def _tts_error(exc: Exception):
    if isinstance(exc, ValueError): raise HTTPException(422, str(exc)) from exc
    if isinstance(exc, TTSConfigurationError): raise HTTPException(503, "TTS 설정이 필요합니다.") from exc
    if isinstance(exc, TTSConnectionError): raise HTTPException(503, "TTS provider에 연결할 수 없습니다.") from exc
    if isinstance(exc, TTSTimeoutError): raise HTTPException(504, "TTS 응답 시간이 초과되었습니다.") from exc
    if isinstance(exc, TTSSynthesisError): raise HTTPException(502, "TTS 음성을 생성하지 못했습니다.") from exc
    raise exc

@router.get('/tts/health')
async def tts_health():
    service=get_tts_service(); settings=get_settings()
    return {"service":"tts", "configured":bool(settings.tts_voice), "provider":settings.tts_provider, "voice":settings.tts_voice or None, "reachable":await service.health()}

@router.post('/tts')
async def tts(request: TTSRequest):
    try: result=await get_tts_service().synthesize(request.text)
    except (ValueError, TTSConfigurationError, TTSConnectionError, TTSTimeoutError, TTSSynthesisError) as exc: _tts_error(exc)
    return Response(content=result.audio, media_type=result.content_type, headers={"Content-Disposition":f"inline; filename=speech.{result.file_extension}", "X-TTS-Provider":result.provider})

@router.post('/tts/preview')
async def tts_preview(request: TTSPreviewRequest):
    text=ResponseMessageBuilder().build(request.intent, request.result)
    try: result=await get_tts_service().synthesize(text)
    except (ValueError, TTSConfigurationError, TTSConnectionError, TTSTimeoutError, TTSSynthesisError) as exc: _tts_error(exc)
    return Response(content=result.audio, media_type=result.content_type, headers={"Content-Disposition":f"inline; filename=speech.{result.file_extension}", "X-TTS-Response-Text":text})
