import pytest
import random
import asyncio
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from shared.models.factory import Base, ProductionJob, JobStep, JobStatus, ProductionJobControlState, StepStatus, Product
from shared.services.production_status_query_service import ProductionStatusQueryService, ProductionStatusQueryResult
from api_server.services.response_message_builder import ResponseMessageBuilder
from api_server.services.production_snapshot_service import ProductionSnapshotService
from api_server.services.command_interpreter import CommandInterpreter
from shared.schemas.ai import Intent, StructuredCommand
from api_server.services.production_conversation_service import ProductionConversationService

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionL = sessionmaker(bind=engine)
    sess = SessionL()
    yield sess
    sess.close()

def setup_product(session: Session) -> Product:
    p = Product(product_code="HOUSE_A", product_name="A형 주택")
    session.add(p)
    session.commit()
    return p

def create_job(session: Session, status: JobStatus) -> ProductionJob:
    job = ProductionJob(job_code=f"J{random.randint(1, 100000)}", product_code="HOUSE_A", status=status, requested_at=datetime.utcnow())
    session.add(job)
    session.commit()
    return job

def create_step(session: Session, job: ProductionJob, status: StepStatus, name: str, fail: str = None) -> JobStep:
    step = JobStep(job_id=job.job_id, operation_code="OP", status=status, failure_reason=fail, display_name=name, step_order=1)
    session.add(step)
    session.commit()
    return step

@pytest.fixture
def product(session: Session):
    return setup_product(session)

@pytest.mark.parametrize("product_code,product_name", [("HOUSE_A", "A형 주택"), ("HOUSE_B", "B형 주택")])
def test_voice_and_unity_share_incoming_qa_process_projection_for_both_houses(
    session: Session, product_code: str, product_name: str
):
    session.add(Product(product_code=product_code, product_name=product_name))
    session.commit()
    job = ProductionJob(
        job_code=f"{product_code}-PROCESS-VOICE", product_code=product_code,
        status=JobStatus.RUNNING, requested_at=datetime.utcnow(),
    )
    session.add(job)
    session.commit()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False)

    voice = ProductionStatusQueryService(session).get_active_job_status()
    unity = ProductionSnapshotService(factory).get_job_status(job.job_id)

    assert voice is not None and unity is not None
    assert voice.process_stage_code == unity["process_stage_code"] == "INCOMING_QA"
    assert voice.current_step_name == unity["process_stage_display_name"] == "수입검사"


def test_s1_running(session: Session, product):
    job = create_job(session, JobStatus.RUNNING)
    create_step(session, job, StepStatus.RUNNING, "외벽 설치")

    svc = ProductionStatusQueryService(session)
    res = svc.get_active_job_status()
    assert res.status == JobStatus.RUNNING
    assert res.process_stage_code == "INCOMING_QA"
    assert res.current_step_name == "수입검사"

    msg = ResponseMessageBuilder().build_job_status_message(res)
    assert msg == "현재 A형 주택을 생산 중이며 수입검사 공정을 진행하고 있습니다."


@pytest.mark.parametrize("name", ["A형 초소형 하우스", "B형 초소형 하우스"])
def test_status_narration_uses_natural_object_particle_for_demo_product_names(name: str):
    result = ProductionStatusQueryResult(status=JobStatus.RUNNING, product_name=name)
    assert ResponseMessageBuilder().build_job_status_message(result) == f"현재 {name}를 생산 중이며 작업을 진행하고 있습니다."

def test_s1_control_paused_is_reported_without_mutating_business_status(session: Session, product):
    job = create_job(session, JobStatus.RUNNING)
    job.control_state = ProductionJobControlState.PAUSED
    session.commit()

    result = ProductionStatusQueryService(session).get_active_job_status()
    assert result is not None
    assert result.status is JobStatus.RUNNING
    assert result.control_state is ProductionJobControlState.PAUSED
    assert ResponseMessageBuilder().build_job_status_message(result) == "현재 A형 주택 생산은 일시정지 상태입니다."


def test_s2_ready(session: Session, product):
    job = create_job(session, JobStatus.READY)

    svc = ProductionStatusQueryService(session)
    res = svc.get_active_job_status()
    assert res.status == JobStatus.READY

    msg = ResponseMessageBuilder().build_job_status_message(res)
    assert msg == "현재 A형 주택을 생산 중이며 수입검사 공정을 진행하고 있습니다."

def test_s3_completed(session: Session, product):
    job = create_job(session, JobStatus.COMPLETED)

    svc = ProductionStatusQueryService(session)
    res = svc.get_active_job_status()
    assert res.status == JobStatus.COMPLETED

    msg = ResponseMessageBuilder().build_job_status_message(res)
    assert msg == "최근 A형 주택 생산은 완료되었습니다."

def test_s4_failed(session: Session, product):
    job = create_job(session, JobStatus.FAILED)
    create_step(session, job, StepStatus.FAILED, "외벽 설치", fail="Jam")

    svc = ProductionStatusQueryService(session)
    res = svc.get_active_job_status()
    assert res.status == JobStatus.FAILED
    assert res.current_step_name == "외벽 설치"

    msg = ResponseMessageBuilder().build_job_status_message(res)
    assert msg == "A형 주택 생산이 중단되었습니다. 외벽 설치 단계에서 오류가 발생했습니다."

def test_canceled_job_is_narrated_explicitly(session: Session, product):
    job = create_job(session, JobStatus.CANCELED)

    result = ProductionStatusQueryService(session).get_active_job_status()

    assert result is not None
    assert result.status is JobStatus.CANCELED
    assert ResponseMessageBuilder().build_job_status_message(result) == "해당 A형 주택 생산 작업은 취소되었습니다."


def test_s5_no_job(session: Session):
    svc = ProductionStatusQueryService(session)
    res = svc.get_active_job_status()
    assert res is None

    msg = ResponseMessageBuilder().build_job_status_message(res)
    assert msg == "현재 진행 중인 생산 작업이 없습니다."

def test_s6_multiple_jobs(session: Session, product):
    job1 = create_job(session, JobStatus.COMPLETED)
    job2 = create_job(session, JobStatus.RUNNING)

    svc = ProductionStatusQueryService(session)
    res = svc.get_active_job_status()
    assert res.status == JobStatus.RUNNING

class NeverCalledLLM:
    async def chat(self, _text: str):
        raise AssertionError("deterministic status query must not call Ollama")


class FakeInterpreter(CommandInterpreter):
    def __init__(self): pass
    async def interpret(self, text: str):
        if "어디까지" in text or "작업 중" in text or "끝났어" in text or "알려줘" in text:
            return text, StructuredCommand(intent=Intent.QUERY_JOB_STATUS), "{}"
        return text, StructuredCommand(intent=Intent.UNKNOWN), "{}"

def test_s7_s10_voice_integration(session: Session, product):
    async def _run():
        job = create_job(session, JobStatus.RUNNING)
        create_step(session, job, StepStatus.RUNNING, "외벽 설치")

        class FakePending:
            def get_active_by_session(self, sid): return None

        svc = ProductionConversationService(
            pending_service=FakePending(),
            interpreter=FakeInterpreter(),
            status_query_service=ProductionStatusQueryService(session)
        )

        r1 = await svc.handle_text(session_id="S7", text="지금 생산 어디까지 됐어?")
        assert "수입검사" in r1.message
        assert r1.command.intent == Intent.QUERY_JOB_STATUS

        r2 = await svc.handle_text(session_id="S8", text="현재 무슨 작업 중이야?")
        assert "수입검사" in r2.message
    asyncio.run(_run())

def test_real_status_fastpath_reaches_existing_status_query_service(session: Session, product):
    job = create_job(session, JobStatus.RUNNING)
    create_step(session, job, StepStatus.RUNNING, "외벽 설치")

    class FakePending:
        def get_active_by_session(self, _session_id):
            return None

    service = ProductionConversationService(
        pending_service=FakePending(),
        interpreter=CommandInterpreter(NeverCalledLLM()),
        status_query_service=ProductionStatusQueryService(session),
    )
    result = asyncio.run(service.handle_text(session_id="status-fastpath", text="현재 무슨 작업 중이야?"))
    assert result.command is not None
    assert result.command.intent is Intent.QUERY_JOB_STATUS
    assert "수입검사" in result.message
    assert "지원하는 생산 시스템 명령" not in result.message


def test_j1_active_priority(session: Session, product):
    j100 = create_job(session, JobStatus.RUNNING)
    j101 = create_job(session, JobStatus.COMPLETED)
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res.status == JobStatus.RUNNING

def test_j2_active_priority_over_failed(session: Session, product):
    j100 = create_job(session, JobStatus.RUNNING)
    j101 = create_job(session, JobStatus.FAILED)
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res.status == JobStatus.RUNNING

def test_j3_ready_active_priority(session: Session, product):
    j100 = create_job(session, JobStatus.READY)
    j101 = create_job(session, JobStatus.COMPLETED)
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res.status == JobStatus.READY

def test_j4_requested_active_priority(session: Session, product):
    j100 = create_job(session, JobStatus.REQUESTED)
    j101 = create_job(session, JobStatus.COMPLETED)
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res.status == JobStatus.REQUESTED

def test_j5_paused_active_priority(session: Session, product):
    j100 = create_job(session, JobStatus.PAUSED)
    j101 = create_job(session, JobStatus.FAILED)
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res.status == JobStatus.PAUSED

def test_j6_terminal_fallback(session: Session, product):
    j100 = create_job(session, JobStatus.COMPLETED)
    j101 = create_job(session, JobStatus.FAILED)
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res.status == JobStatus.FAILED

def test_j7_no_jobs(session: Session):
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res is None

def test_j8_multiple_active(session: Session, product):
    j100 = create_job(session, JobStatus.RUNNING)
    j101 = create_job(session, JobStatus.RUNNING)
    # The second job gets a higher job_id, so it should be picked
    # But let's hack job_id to be deterministic just in case
    j100.job_id = 100
    j101.job_id = 101
    session.commit()
    res = ProductionStatusQueryService(session).get_active_job_status()
    assert res.status == JobStatus.RUNNING
