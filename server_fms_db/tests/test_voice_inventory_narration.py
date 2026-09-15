from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from api_server.main import app
from api_server.routers.ai import get_interpreter, get_stt_service
from api_server.routers.inventory import get_db, get_inventory_service
from shared.enums.ai import Intent
from shared.models import Base
from shared.models.factory import Inventory, Part, PartCategory, ProductionJob
from shared.schemas.ai import StructuredCommand, TranscriptionResponse
from shared.services.inventory_service import InventoryService


class FakeInterpreter:
    def __init__(self, command: StructuredCommand) -> None:
        self.command = command
        self.calls = 0

    async def interpret(self, text: str):
        self.calls += 1
        return text, self.command, ''


class FakeSTT:
    async def transcribe_upload(self, _audio):
        return TranscriptionResponse(text='재고 조회', language='ko', processing_time_ms=0)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)

    @event.listens_for(engine, 'connect')
    def _enable_fks(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    value = factory()
    try:
        yield value
    finally:
        value.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _part(session: Session, code: str, name: str) -> Part:
    part = Part(
        part_code=code,
        part_name=name,
        vision_class=f'{code.lower()}_class',
        category=PartCategory.STRUCTURE,
        unit='EA',
    )
    session.add(part)
    session.commit()
    return part


def _voice_response(session: Session, command: StructuredCommand) -> dict:
    interpreter = FakeInterpreter(command)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(session)
    app.dependency_overrides[get_interpreter] = lambda: interpreter
    app.dependency_overrides[get_stt_service] = lambda: FakeSTT()
    try:
        with TestClient(app) as client:
            response = client.post(
                '/ai/voice-conversation',
                data={'session_id': 'inventory-narration'},
                files={'audio': ('speech.wav', b'voice-wav', 'audio/wav')},
            )
            assert response.status_code == 200, response.text
            return response.json(), interpreter
    finally:
        app.dependency_overrides.clear()


def test_i01_specific_display_name_uses_same_authoritative_quantity(session: Session):
    part = _part(session, 'WALL_EXT', '외벽')
    InventoryService(session).stock_in(part_code=part.part_code, quantity=12, reason='fixture')
    payload, interpreter = _voice_response(session, StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='ITEM', item_name='외벽'))
    assert payload['message'] == '외벽은 현재 12개 사용 가능합니다.'
    assert interpreter.calls == 1


def test_i02_part_code_query_uses_matching_authoritative_row(session: Session):
    part = _part(session, 'ROOF_01', '평지붕')
    InventoryService(session).stock_in(part_code=part.part_code, quantity=3, reason='fixture')
    payload, _ = _voice_response(session, StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='ITEM', item_name='ROOF_01'))
    assert payload['message'] == '평지붕은 현재 3개 사용 가능합니다.'


@pytest.mark.parametrize('spoken_text', ['전체 자재 재고 확인해줘', '현재 모든 자재 재고 보여줘'])
def test_i03_i04_all_inventory_narration_is_from_actual_rows(session: Session, spoken_text: str):
    wall = _part(session, 'WALL_EXT', '외벽')
    roof = _part(session, 'ROOF_01', '평지붕')
    inventory = InventoryService(session)
    inventory.stock_in(part_code=wall.part_code, quantity=12, reason='fixture')
    inventory.stock_in(part_code=roof.part_code, quantity=3, reason='fixture')
    payload, _ = _voice_response(session, StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='ALL'))
    assert payload['message'] == '현재 생산 자재 재고는 평지붕은 현재 3개 사용 가능합니다. 외벽은 현재 12개 사용 가능합니다.'


def test_i05_known_part_without_inventory_row_is_truthful_zero(session: Session):
    _part(session, 'WINDOW', '창문')
    payload, _ = _voice_response(session, StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='ITEM', item_name='창문'))
    assert payload['message'] == '창문은 현재 사용 가능한 재고가 없습니다.'


def test_i06_unknown_item_is_not_reported_as_zero(session: Session):
    payload, _ = _voice_response(session, StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='ITEM', item_name='없는 자재'))
    assert payload['message'] == '없는 자재에 해당하는 자재를 찾을 수 없습니다.'
    assert '0개' not in payload['message']


def test_i07_category_query_is_truthfully_not_supported(session: Session):
    _part(session, 'BATH_ITEM', '욕실 패널')
    payload, _ = _voice_response(session, StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='CATEGORY', category_name='화장실'))
    assert payload['message'] == '현재 카테고리별 재고 조회는 지원되지 않습니다.'


def test_i08_structured_voice_command_result_is_preserved(session: Session):
    part = _part(session, 'WALL_EXT', '외벽')
    InventoryService(session).stock_in(part_code=part.part_code, quantity=12, reason='fixture')
    command = StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='ITEM', item_name='외벽')
    interpreter = FakeInterpreter(command)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_inventory_service] = lambda: InventoryService(session)
    app.dependency_overrides[get_interpreter] = lambda: interpreter
    app.dependency_overrides[get_stt_service] = lambda: FakeSTT()
    try:
        with TestClient(app) as client:
            response = client.post('/ai/voice-command', files={'audio': ('speech.wav', b'voice-wav', 'audio/wav')})
            assert response.status_code == 200, response.text
            rows = response.json()['inventory_result']
    finally:
        app.dependency_overrides.clear()
    assert rows == [
        {
            'part_code': 'WALL_EXT',
            'part_name': '외벽',
            'quantity': 12,
            'reserved_quantity': 0,
            'available_quantity': 12,
            'updated_at': rows[0]['updated_at'],
            'products': [],
        }
    ]


def test_i09_i10_i11_i12_read_only_conversation_is_deterministic_and_tts_safe(session: Session):
    part = _part(session, 'WALL_EXT', '외벽')
    InventoryService(session).stock_in(part_code=part.part_code, quantity=12, reason='fixture')
    before_inventory = session.scalar(select(Inventory.quantity).where(Inventory.part_code == part.part_code))
    before_jobs = session.scalar(select(ProductionJob).limit(1))
    payload, interpreter = _voice_response(session, StructuredCommand(intent=Intent.QUERY_INVENTORY, inventory_scope='ITEM', item_name='외벽'))
    assert interpreter.calls == 1
    assert payload['message'] == '외벽은 현재 12개 사용 가능합니다.'
    assert isinstance(payload['message'], str) and 0 < len(payload['message']) <= 500
    assert session.scalar(select(Inventory.quantity).where(Inventory.part_code == part.part_code)) == before_inventory
    assert session.scalar(select(ProductionJob).limit(1)) is before_jobs
