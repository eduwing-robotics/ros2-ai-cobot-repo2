from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.ai import get_conversation_service
from api_server.routers.inventory import get_db
from api_server.services.production_conversation_service import ProductionConversationService
from api_server.services.command_interpreter import CommandInterpreter
from shared.services.production_status_query_service import ProductionStatusQueryService
from shared.enums.ai import Intent
from shared.models import Base
from tests.recipe_test_support import add_active_recipe
from shared.models.factory import AssemblyRecipe, AssemblyRecipeStage, EventType, JobMaterialDelivery, JobStep, PendingProductionRequest, PendingProductionState, Product, ProductionEvent, ProductionJob, RoofOptionCode
from shared.schemas.ai import StructuredCommand
from shared.services.pending_production_request_service import PendingProductionRequestService
from shared.services.production_request_materialization_service import ProductionRequestMaterializationService


class FakeInterpreter:
    def __init__(self, command: StructuredCommand) -> None:
        self.command = command
        self.calls = 0

    async def interpret(self, text: str):
        self.calls += 1
        return text, self.command, "{}"


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


class NeverCalledLLM:
    async def chat(self, _text: str):
        raise AssertionError("deterministic status query must not call Ollama")


def test_conversation_api_status_fastpath_uses_existing_db_query(session: Session) -> None:
    job = ProductionJob(job_code="STATUS-FASTPATH", product_code="HOUSE_A", status="RUNNING")
    session.add(job)
    session.flush()
    session.add(JobStep(
        job_id=job.job_id, step_order=1, operation_code="INSTALL_OUTER_WALL",
        display_name="외벽 설치", status="RUNNING",
    ))
    session.commit()
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=CommandInterpreter(NeverCalledLLM()),
        status_query_service=ProductionStatusQueryService(session),
    )
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        with TestClient(app) as client:
            response = client.post("/ai/conversation", json={"session_id": "status-fastpath", "text": "현재 무슨 작업 중이야?"})
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    payload = response.json()
    assert payload["command"]["intent"] == Intent.QUERY_JOB_STATUS.value
    assert payload["command"]["clarification_needed"] is False
    assert "수입검사" in payload["message"]
    assert "지원하는 생산 시스템 명령" not in payload["message"]


def test_conversation_api_three_turn_flow_confirms_without_creating_job(session: Session) -> None:
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
    )
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        with TestClient(app) as client:
            first = client.post("/ai/conversation", json={"session_id": "api-flow", "text": "A형 한 채 만들어줘"})
            second = client.post("/ai/conversation", json={"session_id": "api-flow", "text": "2번"})
            third = client.post("/ai/conversation", json={"session_id": "api-flow", "text": "네"})
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["conversation_state"] == "WAITING_ROOF_OPTION"
    assert second.json()["conversation_state"] == "AWAITING_CONFIRMATION"
    assert second.json()["message"] == "A형 주택 한 채를 2번 경사지붕으로 제작하는 것이 맞습니까?"
    assert third.json()["conversation_state"] == "CONFIRMED"
    assert third.json()["message"] == "생산 요청이 확인되었습니다."
    pending = session.scalar(select(PendingProductionRequest).where(PendingProductionRequest.session_id == "api-flow"))
    assert pending is not None and pending.state is PendingProductionState.CONFIRMED
    assert pending.roof_option_code is RoofOptionCode.ROOF_02
    assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0
    assert interpreter.calls == 1


def test_conversation_api_rejects_and_allows_a_new_request(session: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="B형 주택",
        product_code="HOUSE_B",
        quantity=2,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=FakeInterpreter(command),
    )
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        with TestClient(app) as client:
            initial = client.post("/ai/conversation", json={"session_id": "api-reject", "text": "B형 두 채 평지붕으로 만들어줘"})
            rejected = client.post("/ai/conversation", json={"session_id": "api-reject", "text": "아니요"})
            next_request = client.post("/ai/conversation", json={"session_id": "api-reject", "text": "B형 두 채 평지붕으로 만들어줘"})
    finally:
        app.dependency_overrides.clear()

    assert initial.json()["conversation_state"] == "AWAITING_CONFIRMATION"
    assert rejected.json()["conversation_state"] == "REJECTED"
    assert rejected.json()["message"] == "생산 요청을 진행하지 않겠습니다."
    assert next_request.json()["conversation_state"] == "AWAITING_CONFIRMATION"


def test_conversation_api_yes_materializes_n_jobs(session: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="B형 주택",
        product_code="HOUSE_B",
        quantity=2,
        roof_option_code=RoofOptionCode.ROOF_01,
        requires_confirmation=True,
    )
    interpreter = FakeInterpreter(command)
    notifications: list[tuple[int, str | None]] = []
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=interpreter,
        materialization_service=ProductionRequestMaterializationService(
            session, post_commit_callback=lambda job_id, reason: notifications.append((job_id, reason))
        ),
    )
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        with TestClient(app) as client:
            initial = client.post("/ai/conversation", json={"session_id": "api-phase5", "text": "B형 두 채 평지붕으로 만들어줘"})
            approved = client.post("/ai/conversation", json={"session_id": "api-phase5", "text": "네"})
    finally:
        app.dependency_overrides.clear()

    assert initial.status_code == approved.status_code == 200
    assert approved.json()["conversation_state"] == "CONFIRMED"
    assert approved.json()["production_job_ids"] and len(approved.json()["production_job_ids"]) == 2
    assert approved.json()["message"] == "생산 요청이 확인되어 생산 작업 2건이 등록되었습니다."
    assert notifications == [(job_id, "job_created") for job_id in approved.json()["production_job_ids"]]
    jobs = list(session.scalars(select(ProductionJob).where(ProductionJob.source_pending_request_id == initial.json()["pending_request_id"]).order_by(ProductionJob.source_item_index)))
    assert [job.source_item_index for job in jobs] == [1, 2]
    assert all(job.roof_option_code is RoofOptionCode.ROOF_01 for job in jobs)
    assert session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id.in_([job.job_id for job in jobs]))) == 32
    assert session.scalar(select(func.count()).select_from(ProductionEvent).where(ProductionEvent.job_id.in_([job.job_id for job in jobs]), ProductionEvent.event_type == EventType.JOB_CREATED)) == 2


@pytest.mark.parametrize(
    ("product_code", "product_name", "request_text", "roof_text", "roof_option"),
    [
        ("HOUSE_A", "A형 주택", "A형 주택 한 채 생산해줘", "평지붕", RoofOptionCode.ROOF_01),
        ("HOUSE_B", "B형 주택", "B형 주택 한 채 생산해줘", "경사지붕", RoofOptionCode.ROOF_02),
    ],
)
def test_conversation_api_final_confirmation_materializes_active_recipe_job(
    session: Session,
    product_code: str,
    product_name: str,
    request_text: str,
    roof_text: str,
    roof_option: RoofOptionCode,
) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name=product_name,
        product_code=product_code,
        quantity=1,
        requires_confirmation=True,
    )
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=FakeInterpreter(command),
        materialization_service=ProductionRequestMaterializationService(session),
    )

    def override_get_db():
        yield session

    app.dependency_overrides[get_conversation_service] = lambda: service
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            initial = client.post(
                "/ai/conversation",
                json={"session_id": f"recipe-{product_code}", "text": request_text},
            )
            assert initial.status_code == 200
            assert initial.json()["conversation_state"] == "WAITING_ROOF_OPTION"
            assert initial.json()["production_job_ids"] == []
            assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0

            roof_selected = client.post(
                "/ai/conversation",
                json={"session_id": f"recipe-{product_code}", "text": roof_text},
            )
            assert roof_selected.status_code == 200
            assert roof_selected.json()["conversation_state"] == "AWAITING_CONFIRMATION"
            assert roof_selected.json()["production_job_ids"] == []
            assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0

            confirmed = client.post(
                "/ai/conversation",
                json={"session_id": f"recipe-{product_code}", "text": "네"},
            )
            assert confirmed.status_code == 200
            assert confirmed.json()["conversation_state"] == "CONFIRMED"
            assert len(confirmed.json()["production_job_ids"]) == 1
            job_id = confirmed.json()["production_job_ids"][0]

            snapshot = client.get(f"/production/jobs/{job_id}/execution-snapshot")
    finally:
        app.dependency_overrides.clear()

    assert snapshot.status_code == 200
    assert snapshot.json()["job_status"] == "REQUESTED"
    assert snapshot.json()["current_step"] is None
    assert snapshot.json()["next_step"] is None

    job = session.get(ProductionJob, job_id)
    assert job is not None
    assert job.product.product_code == product_code
    assert job.roof_option_code is roof_option
    assert job.assembly_recipe_id is not None
    recipe = session.get(AssemblyRecipe, job.assembly_recipe_id)
    assert recipe is not None
    assert recipe.product_code == product_code
    assert recipe.is_active is True

    stages = list(
        session.scalars(
            select(AssemblyRecipeStage)
            .where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)
            .order_by(AssemblyRecipeStage.stage_order)
        )
    )
    steps = list(
        session.scalars(
            select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.step_order)
        )
    )
    assert [
        (step.step_order, step.operation_code, step.display_name, step.source_recipe_stage_id)
        for step in steps
    ] == [
        (stage.stage_order, stage.operation_code, stage.display_name, stage.recipe_stage_id)
        for stage in stages
    ]
    assert all(step.source_recipe_stage_id is not None for step in steps)
    assert all(step.operation_code is not None for step in steps)
    assert not any(step.operation_code == "INSTALL_ROOF" for step in steps)
    assert session.scalar(
        select(func.count())
        .select_from(JobMaterialDelivery)
        .where(JobMaterialDelivery.production_job_id == job_id)
    ) == 1

    replay = ProductionRequestMaterializationService(session).confirm_and_create_jobs(
        pending_request_id=job.source_pending_request_id
    )
    assert replay.created is False
    assert [replayed.job_id for replayed in replay.jobs] == [job_id]
    assert session.scalar(
        select(func.count())
        .select_from(ProductionJob)
        .where(ProductionJob.source_pending_request_id == job.source_pending_request_id)
    ) == 1


def test_conversation_api_rejects_before_materialization(session: Session) -> None:
    command = StructuredCommand(
        intent=Intent.CREATE_PRODUCTION_REQUEST,
        product_name="A형 주택",
        product_code="HOUSE_A",
        quantity=1,
        requires_confirmation=True,
    )
    service = ProductionConversationService(
        pending_service=PendingProductionRequestService(session),
        interpreter=FakeInterpreter(command),
        materialization_service=ProductionRequestMaterializationService(session),
    )
    app.dependency_overrides[get_conversation_service] = lambda: service
    try:
        with TestClient(app) as client:
            initial = client.post(
                "/ai/conversation",
                json={"session_id": "recipe-reject", "text": "A형 주택 생산해줘"},
            )
            roof_selected = client.post(
                "/ai/conversation",
                json={"session_id": "recipe-reject", "text": "평지붕"},
            )
            rejected = client.post(
                "/ai/conversation",
                json={"session_id": "recipe-reject", "text": "아니"},
            )
    finally:
        app.dependency_overrides.clear()

    assert initial.json()["conversation_state"] == "WAITING_ROOF_OPTION"
    assert roof_selected.json()["conversation_state"] == "AWAITING_CONFIRMATION"
    assert rejected.json()["conversation_state"] == "REJECTED"
    pending = session.scalar(
        select(PendingProductionRequest).where(PendingProductionRequest.session_id == "recipe-reject")
    )
    assert pending is not None
    assert pending.state is PendingProductionState.REJECTED
    assert session.scalar(select(func.count()).select_from(ProductionJob)) == 0
