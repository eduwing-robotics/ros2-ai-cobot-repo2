from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.ai import get_conversation_service, get_interpreter, get_stt_service
from api_server.routers.inventory import get_db
from api_server.services.command_interpreter import CommandInterpreter
from api_server.services.production_conversation_service import ProductionConversationService
from api_server.services.voice_timing import VoiceTiming, activate_voice_timing
from api_server.services.voice_runtime_monitor import get_voice_runtime_monitor
from api_server.services.stt_service import STTService
from shared.enums.ai import Intent
from shared.models import Base
from shared.models.factory import (
    PendingProductionRequest,
    PendingProductionState,
    Product,
    Part,
    PartCategory,
    Inventory,
    AssemblyRecipe,
    AssemblyRecipeStage,
    ProductionJob,
    RoofOptionCode,
)
from shared.schemas.ai import StructuredCommand, TranscriptionResponse
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService
from shared.services.production_request_materialization_service import ProductionRequestMaterializationService
from tests.recipe_test_support import add_active_recipe


class FakeInterpreter:
    def __init__(self, command: StructuredCommand) -> None:
        self.command = command
        self.calls = 0

    async def interpret(self, text: str):
        self.calls += 1
        return text, self.command, "{}"


class FakeSTTService(STTService):
    @property
    def loaded(self) -> bool:
        return True

    async def transcribe_upload(self, upload) -> TranscriptionResponse:
        content = await upload.read()
        return TranscriptionResponse(text=content.decode("utf-8"), processing_time_ms=10.0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    db.add_all([Product(product_code="HOUSE_A", product_name="A형 주택"), Product(product_code="HOUSE_B", product_name="B형 주택")])
    add_active_recipe(db, "HOUSE_A")
    add_active_recipe(db, "HOUSE_B")
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


class RecordingVoiceHub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish_voice_event(self, event: dict) -> None:
        self.events.append(event)


def test_voice_conversation_publishes_stt_transcript_and_assistant_response(session: Session) -> None:
    monitor = get_voice_runtime_monitor()
    monitor.reset_for_test()
    command = StructuredCommand(intent=Intent.QUERY_JOB_STATUS)
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=FakeInterpreter(command),
    )
    app.dependency_overrides[get_conversation_service] = lambda: service
    app.dependency_overrides[get_stt_service] = lambda: FakeSTTService()
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as client:
            hub = RecordingVoiceHub()
            client.app.state.unity_hub = hub
            response = client.post(
                "/ai/voice-conversation",
                headers={"X-Voice-Runtime-Turn-ID": "voice-api-events"},
                data={"session_id": "voice-api-events"},
                files={"audio": ("audio.wav", io.BytesIO("현재 무슨 작업 중이야?".encode("utf-8")), "audio/wav")},
            )
        assert response.status_code == 200
        interpreting = next(event for event in hub.events if event["state"] == "INTERPRETING")
        responding = next(event for event in hub.events if event["state"] == "RESPONDING")
        assert interpreting["transcript"] == "현재 무슨 작업 중이야?"
        assert responding["response_text"] == response.json()["message"]
        assert responding["intent"] == "QUERY_JOB_STATUS"
    finally:
        app.dependency_overrides.clear()
        monitor.reset_for_test()


def test_voice_conversation_api_full_flow(session: Session, preflight_fixture: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        requires_confirmation=True,
    )
    interpreter = FakeInterpreter(command)
    now = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session, clock=lambda: now),
        interpreter=interpreter,
        materialization_service=ProductionRequestMaterializationService(session, clock=lambda: now),
    )
    stt_service = FakeSTTService()

    app.dependency_overrides[get_conversation_service] = lambda: service
    app.dependency_overrides[get_stt_service] = lambda: stt_service
    app.dependency_overrides[get_db] = lambda: session

    try:
        with TestClient(app) as client:
            # 1. First turn: request
            res1 = client.post(
                "/ai/voice-conversation",
                data={"session_id": "voice-flow"},
                files={"audio": ("audio.wav", io.BytesIO("A형 주택 하나 만들어줘".encode("utf-8")), "audio/wav")},
            )
            assert res1.status_code == 200
            assert res1.json()["conversation_state"] == "WAITING_ROOF_OPTION"
            assert res1.json()["production_job_ids"] == []
            assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0

            # 2. Second turn: roof option
            res2 = client.post(
                "/ai/voice-conversation",
                data={"session_id": "voice-flow"},
                files={"audio": ("audio.wav", io.BytesIO("경사지붕".encode("utf-8")), "audio/wav")},
            )
            assert res2.status_code == 200
            assert res2.json()["conversation_state"] == "AWAITING_CONFIRMATION"
            assert res2.json()["production_job_ids"] == []
            assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0

            # 3. Third turn: confirmation
            res3 = client.post(
                "/ai/voice-conversation",
                data={"session_id": "voice-flow"},
                files={"audio": ("audio.wav", io.BytesIO("네".encode("utf-8")), "audio/wav")},
            )
            assert res3.status_code == 200
            assert res3.json()["conversation_state"] == "CONFIRMED"
            assert len(res3.json()["production_job_ids"]) == 1
            assert session.scalar(select(func.count()).select_from(ProductionJob)) == 1

            pending = session.scalar(
                select(PendingProductionRequest).where(PendingProductionRequest.session_id == "voice-flow")
            )
            assert pending is not None
            assert pending.roof_option_code == RoofOptionCode.ROOF_02

    finally:
        app.dependency_overrides.clear()


def test_voice_conversation_reject(session: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    interpreter = FakeInterpreter(command)
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=interpreter,
        materialization_service=ProductionRequestMaterializationService(session),
    )
    stt_service = FakeSTTService()

    app.dependency_overrides[get_conversation_service] = lambda: service
    app.dependency_overrides[get_stt_service] = lambda: stt_service
    app.dependency_overrides[get_db] = lambda: session

    try:
        with TestClient(app) as client:
            res1 = client.post(
                "/ai/voice-conversation",
                data={"session_id": "voice-reject"},
                files={"audio": ("audio.wav", io.BytesIO("A형 주택 1번지붕으로 만들어줘".encode("utf-8")), "audio/wav")},
            )
            assert res1.status_code == 200
            assert res1.json()["conversation_state"] == "AWAITING_CONFIRMATION"

            res2 = client.post(
                "/ai/voice-conversation",
                data={"session_id": "voice-reject"},
                files={"audio": ("audio.wav", io.BytesIO("아니요".encode("utf-8")), "audio/wav")},
            )
            assert res2.status_code == 200
            assert res2.json()["conversation_state"] == "REJECTED"
            assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0

    finally:
        app.dependency_overrides.clear()



@pytest.fixture
def preflight_fixture(session: Session) -> Session:
    recipe = session.scalar(
        select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A", AssemblyRecipe.is_active.is_(True))
    )
    assert recipe is not None
    body = Part(
        part_code="VOICE_PREFLIGHT_BODY",
        part_name="Voice preflight body",
        category=PartCategory.STRUCTURE,
        unit="ea",
    )
    roof = Part(
        part_code="VOICE_PREFLIGHT_ROOF",
        part_name="Voice preflight roof",
        category=PartCategory.STRUCTURE,
        unit="ea",
    )
    session.add_all((body, roof))
    session.flush()
    for stage in recipe.stages:
        stage.is_terminal = False
    session.add_all((
        Inventory(part_code=body.part_code, quantity=2),
        Inventory(part_code=roof.part_code, quantity=2),
        AssemblyRecipeStage(
            recipe_id=recipe.recipe_id,
            stage_order=17,
            operation_code="VOICE_PREFLIGHT_BODY",
            display_name="Voice preflight body",
            part_code=body.part_code,
            quantity=1,
            option_code="ROOF_01",
            is_terminal=True,
        ),
        AssemblyRecipeStage(
            recipe_id=recipe.recipe_id,
            stage_order=18,
            operation_code="VOICE_PREFLIGHT_ROOF",
            display_name="Voice preflight roof",
            part_code=roof.part_code,
            quantity=1,
            option_code="ROOF_02",
            is_terminal=True,
        ),
    ))
    session.commit()
    return session


def _wire_real_conversation_api(session: Session, interpreter: FakeInterpreter) -> None:
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_interpreter] = lambda: interpreter
    app.dependency_overrides[get_stt_service] = lambda: FakeSTTService()


def test_get_conversation_service_wires_real_preflight(session: Session) -> None:
    service = get_conversation_service(
        db=session,
        interpreter=FakeInterpreter(
            StructuredCommand(intent=Intent.UNKNOWN, clarification_needed=True, clarification_message="unsupported")
        ),
    )
    assert isinstance(service._preflight, ProductionInventoryPreflightService)


def test_conversation_api_preflight_and_confirm_revalidation(preflight_fixture: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    _wire_real_conversation_api(preflight_fixture, FakeInterpreter(command))
    try:
        with TestClient(app) as client:
            initial = client.post("/ai/conversation", json={"session_id": "preflight-confirm", "text": "A형"})
            assert initial.status_code == 200
            assert initial.json()["conversation_state"] == "AWAITING_CONFIRMATION"
            assert preflight_fixture.scalar(select(func.count()).select_from(ProductionJob)) == 0

            preflight_fixture.get(Inventory, "VOICE_PREFLIGHT_BODY").quantity = 0
            preflight_fixture.commit()
            confirmed = client.post("/ai/conversation", json={"session_id": "preflight-confirm", "text": "네"})
            assert confirmed.status_code == 200
            assert confirmed.json()["conversation_state"] == "AWAITING_CONFIRMATION"
            assert "부족하여 생산할 수 없습니다" in confirmed.json()["message"]
            assert preflight_fixture.scalar(select(func.count()).select_from(ProductionJob)) == 0
    finally:
        app.dependency_overrides.clear()


def test_conversation_api_blocks_initial_and_roof_selection_shortages(preflight_fixture: Session) -> None:
    direct = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    _wire_real_conversation_api(preflight_fixture, FakeInterpreter(direct))
    try:
        with TestClient(app) as client:
            preflight_fixture.get(Inventory, "VOICE_PREFLIGHT_BODY").quantity = 0
            preflight_fixture.commit()
            initial = client.post("/ai/conversation", json={"session_id": "preflight-initial", "text": "A형"})
            assert initial.status_code == 200
            assert initial.json()["conversation_state"] == "REJECTED"
            assert preflight_fixture.scalar(select(func.count()).select_from(ProductionJob)) == 0

            preflight_fixture.get(Inventory, "VOICE_PREFLIGHT_BODY").quantity = 2
            preflight_fixture.get(Inventory, "VOICE_PREFLIGHT_ROOF").quantity = 0
            preflight_fixture.commit()
            _wire_real_conversation_api(
                preflight_fixture,
                FakeInterpreter(
                    StructuredCommand(
                        intent=Intent.CREATE_PRODUCTION_REQUEST,
                        product_name="A형 주택",
                        product_code="HOUSE_A",
                        quantity=1,
                        requires_confirmation=True,
                    )
                ),
            )
            waiting = client.post("/ai/conversation", json={"session_id": "preflight-roof", "text": "A형"})
            assert waiting.status_code == 200
            assert waiting.json()["conversation_state"] == "WAITING_ROOF_OPTION"
            roof = client.post("/ai/conversation", json={"session_id": "preflight-roof", "text": "경사지붕"})
            assert roof.status_code == 200
            assert roof.json()["conversation_state"] == "REJECTED"
            assert preflight_fixture.scalar(select(func.count()).select_from(ProductionJob)) == 0
    finally:
        app.dependency_overrides.clear()


def test_voice_conversation_api_returns_safe_shortage_response(preflight_fixture: Session) -> None:
    preflight_fixture.get(Inventory, "VOICE_PREFLIGHT_BODY").quantity = 0
    preflight_fixture.commit()
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    _wire_real_conversation_api(preflight_fixture, FakeInterpreter(command))
    try:
        with TestClient(app) as client:
            response = client.post(
                "/ai/voice-conversation",
                data={"session_id": "preflight-voice"},
                files={"audio": ("audio.wav", io.BytesIO("A형 주택".encode("utf-8")), "audio/wav")},
            )
            assert response.status_code == 200
            assert response.json()["conversation_state"] == "REJECTED"
            assert "부족하여 생산할 수 없습니다" in response.json()["message"]
            assert preflight_fixture.scalar(select(func.count()).select_from(ProductionJob)) == 0
    finally:
        app.dependency_overrides.clear()



def test_conversation_api_sufficient_preflight_creates_one_job(preflight_fixture: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    _wire_real_conversation_api(preflight_fixture, FakeInterpreter(command))
    try:
        with TestClient(app) as client:
            initial = client.post("/ai/conversation", json={"session_id": "preflight-sufficient", "text": "A형"})
            assert initial.status_code == 200
            assert initial.json()["conversation_state"] == "AWAITING_CONFIRMATION"
            assert initial.json()["production_job_ids"] == []

            confirmed = client.post("/ai/conversation", json={"session_id": "preflight-sufficient", "text": "네"})
            assert confirmed.status_code == 200
            assert confirmed.json()["conversation_state"] == "CONFIRMED"
            assert len(confirmed.json()["production_job_ids"]) == 1
            jobs = preflight_fixture.scalars(select(ProductionJob)).all()
            assert len(jobs) == 1
            assert jobs[0].product_code == "HOUSE_A"
            assert jobs[0].roof_option_code is RoofOptionCode.ROOF_01
    finally:
        app.dependency_overrides.clear()


def test_strict_create_fastpath_preserves_existing_roof_conversation_states(session: Session) -> None:
    class NeverCalled:
        async def chat(self, _text: str) -> str:
            raise AssertionError("explicit CREATE must not call Ollama")

    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=CommandInterpreter(NeverCalled()),
    )
    timing = VoiceTiming(endpoint="/ai/voice-conversation", correlation_id="fastpath-explicit")
    with activate_voice_timing(timing):
        explicit = asyncio.run(service.handle_text(
            session_id="fastpath-explicit", text="A형 주택 한 채 평지붕으로 만들어줘"
        ))
    omitted = asyncio.run(service.handle_text(
        session_id="fastpath-omitted", text="A형 주택 한 채 만들어줘"
    ))

    assert explicit.command is not None
    assert explicit.command.product_code == "HOUSE_A"
    assert explicit.command.roof_option_code is RoofOptionCode.ROOF_01
    assert explicit.pending is not None
    assert explicit.pending.state is PendingProductionState.AWAITING_CONFIRMATION
    assert {'pending_lookup_db_ms', 'command_interpretation_ms', 'interpreter_ms', 'deterministic_routing_ms', 'pending_write_db_ms'} <= set(timing.stages_ms)
    assert 'ollama_llm_ms' not in timing.stages_ms
    assert 'ollama_repair_ms' not in timing.stages_ms
    assert omitted.command is not None
    assert omitted.command.roof_option_code is None
    assert omitted.pending is not None
    assert omitted.pending.state is PendingProductionState.WAITING_ROOF_OPTION


def test_voice_preflight_reports_configuration_invalid_not_inventory_sufficient(session: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    recipe = session.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == "HOUSE_A"))
    assert recipe is not None
    for stage in recipe.stages:
        stage.part_code = None
        stage.quantity = None
    session.commit()
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session, clock=lambda: now),
        interpreter=FakeInterpreter(command),
        preflight_service=ProductionInventoryPreflightService(session),
    )

    result = asyncio.run(service.handle_text(session_id="empty-bom", text="A형 만들어줘"))

    assert result.pending is not None
    assert result.pending.state is PendingProductionState.REJECTED
    assert "생산 자재 설정" in result.message
    assert "부족" not in result.message
    assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0
