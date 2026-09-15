"""Manual, real-microphone Voice E2E v1 runner.

The scored requests use the running Voice API.  This module never starts FMS
or ROS; it owns only rows with a VOICEBENCH_ prefix in smart_factory_benchmark.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_server.services.command_interpreter import normalize_text
from api_server.services.production_conversation_service import parse_confirmation_answer
from api_server.services.llm_service import OllamaService
from api_server.services.stt_service import get_stt_service
from shared.config import get_settings
from shared.enums.ai import Intent
from shared.models.factory import (
    AssemblyRecipe,
    AssemblyRecipeStage,
    Inventory,
    JobMaterialDelivery,
    JobMaterialDeliveryItem,
    JobMaterialFeedExecution,
    JobStatus,
    Part,
    PartCategory,
    PendingProductionRequest,
    PendingProductionState,
    Product,
    ProductionEvent,
    ProductionJob,
    RoofOptionCode,
)
from voice_runtime.audio_io import AudioIO
from voice_runtime.vad import EnergyVAD

EXPECTED_DATABASE = "smart_factory_benchmark"
EXPECTED_REVISION = "476988d67923"
RESULTS_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = RESULTS_DIR / "artifacts"


@dataclass(frozen=True)
class Turn:
    reference: str
    intent: str | None = None
    product: str | None = None
    quantity: int | None = None
    roof: str | None = None
    state: str | None = None
    route: str = "conversation"
    creates_job: bool = False
    standalone_interpret: bool = True
    unsupported_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    category: str
    turns: tuple[Turn, ...]
    job_count: int = 0
    mutate_before_confirm: bool = False


def scenarios() -> tuple[Scenario, ...]:
    # E001-E015 retain the previous intent coverage; 20 spoken turns total.
    return (
        Scenario("E001", "CREATE_EXPLICIT_ROOF", (Turn("A형 주택 한 채 평지붕으로 만들어줘", "CREATE_PRODUCTION_REQUEST", "HOUSE_A", 1, "ROOF_01", "AWAITING_CONFIRMATION"), Turn("네", state="CONFIRMED", creates_job=True, standalone_interpret=False)), 1),
        Scenario("E002", "ROOF_MULTI_TURN", (Turn("A형 주택 한 채 만들어줘", "CREATE_PRODUCTION_REQUEST", "HOUSE_A", 1, None, "WAITING_ROOF_OPTION"), Turn("경사지붕", roof="ROOF_02", state="AWAITING_CONFIRMATION", standalone_interpret=False), Turn("네", state="CONFIRMED", creates_job=True, standalone_interpret=False)), 1),
        Scenario("E003", "QUANTITY", (Turn("A형 두 채 평지붕으로 생산해줘", "CREATE_PRODUCTION_REQUEST", "HOUSE_A", 2, "ROOF_01", "AWAITING_CONFIRMATION"), Turn("네", state="CONFIRMED", creates_job=True, standalone_interpret=False)), 2),
        Scenario("E004", "PREFLIGHT_SHORTAGE", (Turn("B형 주택 한 채 경사지붕으로 만들어줘", "CREATE_PRODUCTION_REQUEST", "HOUSE_B", 1, "ROOF_02", "REJECTED"),)),
        Scenario("E005", "REJECT", (Turn("A형 주택 한 채 평지붕으로 만들어줘", "CREATE_PRODUCTION_REQUEST", "HOUSE_A", 1, "ROOF_01", "AWAITING_CONFIRMATION"), Turn("아니요", state="REJECTED", standalone_interpret=False))),
        Scenario("E006", "CONFIRM_REVALIDATION", (Turn("A형 주택 한 채 평지붕으로 만들어줘", "CREATE_PRODUCTION_REQUEST", "HOUSE_A", 1, "ROOF_01", "AWAITING_CONFIRMATION"), Turn("네", state="AWAITING_CONFIRMATION", standalone_interpret=False)), mutate_before_confirm=True),
        Scenario("E007", "INVENTORY_ALL", (Turn("재고 알려줘", "QUERY_INVENTORY", route="voice-command"),)),
        Scenario("E008", "INVENTORY_ITEM", (Turn("전체 자재 재고 확인해줘", "QUERY_INVENTORY", route="voice-command"),)),
        Scenario("E009", "INVENTORY_VARIATION", (Turn("현재 모든 자재 재고 보여줘", "QUERY_INVENTORY", route="voice-command"),)),
        Scenario("E010", "JOB_STATUS", (Turn("지금 생산 어디까지 됐어", "QUERY_JOB_STATUS"),)),
        Scenario("E011", "JOB_STATUS_VARIATION", (Turn("생산 끝났어", "QUERY_JOB_STATUS"),)),
        Scenario("E012", "CANCEL_INTENT", (Turn("현재 작업 취소해줘", "CANCEL_JOB"),)),
        Scenario("E013", "PAUSE_INTENT", (Turn("잠깐 멈춰", "PAUSE_JOB"),)),
        Scenario("E014", "RESUME_INTENT", (Turn("생산 재개해줘", "RESUME_JOB"),)),
        # Negative control: unsupported low-level command; lexical preservation is scored separately from safe UNKNOWN handling.
        Scenario("E015", "UNSUPPORTED", (Turn("PLC 리셋해줘", "UNKNOWN", unsupported_keywords=("plc", "리셋")),)),
    )


def guarded_engine() -> Engine:
    settings = get_settings()
    if settings.cell_transport == "ros2":
        raise RuntimeError("CELL_TRANSPORT=ros2 is forbidden for Voice E2E v1.")
    test_url, production_url = settings.postgres_test_database_url.strip(), settings.database_url.strip()
    if not test_url:
        raise RuntimeError("POSTGRES_TEST_DATABASE_URL is required.")
    parsed = make_url(test_url)
    if parsed.get_backend_name() != "postgresql" or parsed.database != EXPECTED_DATABASE:
        raise RuntimeError("Benchmark DB safety guard rejected POSTGRES_TEST_DATABASE_URL.")
    if production_url and test_url == production_url:
        raise RuntimeError("Benchmark DB safety guard rejected DATABASE_URL equality.")
    engine = create_engine(test_url, pool_pre_ping=True)
    with engine.connect() as conn:
        if conn.execute(text("select current_database()")).scalar_one() != EXPECTED_DATABASE:
            engine.dispose(); raise RuntimeError("Benchmark database identity check failed.")
        if conn.execute(text("select version_num from alembic_version")).scalar_one() != EXPECTED_REVISION:
            engine.dispose(); raise RuntimeError("Benchmark Alembic revision is not current.")
    return engine


class Fixture:
    def __init__(self, engine: Engine) -> None:
        self.session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        self.run_id = f"VOICEBENCH_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid4().hex[:6].upper()}"
        self.parts: list[str] = []
        self.stage_ids: list[int] = []
        self.pending_ids: list[int] = []
        self.created_products: list[str] = []
        self.inventory_original_quantities: dict[str, int | None] = {}
        self.expected_inventory_quantities: dict[str, int] = {}
        self.status_job_ids: list[int] = []
        self.status_product_name: str | None = None

    def setup(self) -> None:
        with self.session_factory() as s:
            for code, name in (("HOUSE_A", "A형 주택"), ("HOUSE_B", "B형 주택")):
                if s.get(__import__("shared.models.factory", fromlist=["Product"]).Product, code) is None:
                    from shared.models.factory import Product
                    s.add(Product(product_code=code, product_name=name)); self.created_products.append(code)
            s.flush()
            for product, enough in (("HOUSE_A", True), ("HOUSE_B", False)):
                recipe = s.scalar(select(AssemblyRecipe).where(AssemblyRecipe.product_code == product, AssemblyRecipe.is_active.is_(True)))
                if recipe is None:
                    raise RuntimeError(f"{product} needs an active recipe in benchmark DB.")
                order = (s.scalar(select(func.max(AssemblyRecipeStage.stage_order)).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)) or 0) + 1
                body = f"{self.run_id}_{product}_BODY"
                roof1 = f"{self.run_id}_{product}_ROOF01"
                roof2 = f"{self.run_id}_{product}_ROOF02"
                for part_code, label in ((body, "body"), (roof1, "roof 01"), (roof2, "roof 02")):
                    s.add(Part(part_code=part_code, part_name=f"{self.run_id} {label}", category=PartCategory.STRUCTURE, vision_class="voicebench", unit="ea"))
                    s.add(Inventory(part_code=part_code, quantity=10 if enough else 0))
                    self.parts.append(part_code)
                s.flush()
                stages = (
                    AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=order, operation_code=f"{self.run_id}_BODY", display_name=f"{self.run_id} body", part_code=body, quantity=1),
                    AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=order + 1, operation_code=f"{self.run_id}_ROOF01", display_name=f"{self.run_id} roof 01", part_code=roof1, quantity=1, option_code="ROOF_01"),
                    AssemblyRecipeStage(recipe_id=recipe.recipe_id, stage_order=order + 2, operation_code=f"{self.run_id}_ROOF02", display_name=f"{self.run_id} roof 02", part_code=roof2, quantity=1, option_code="ROOF_02"),
                )
                s.add_all(stages); s.flush(); self.stage_ids.extend(stage.recipe_stage_id for stage in stages)
                if product == "HOUSE_A":
                    self._provision_house_a_inventory(s, recipe)
            self._capture_expected_inventory(s)
            self._create_status_fixture(s)
            s.commit()

    def _provision_house_a_inventory(self, s: Session, recipe: AssemblyRecipe) -> None:
        """Temporarily satisfy real active-recipe requirements for the A-house sufficient cases."""
        requirements: dict[str, dict[str, int]] = {"ROOF_01": {}, "ROOF_02": {}}
        stages = list(s.scalars(
            select(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_id == recipe.recipe_id)
        ))
        for roof_option, required in requirements.items():
            for stage in stages:
                if stage.part_code is None or stage.quantity is None or stage.quantity <= 0:
                    continue
                if stage.option_code is not None and stage.option_code != roof_option:
                    continue
                required[stage.part_code] = required.get(stage.part_code, 0) + stage.quantity
        for part_code in set().union(*requirements.values()):
            required_for_two_houses = max(
                required.get(part_code, 0) for required in requirements.values()
            ) * 2
            inventory = s.get(Inventory, part_code)
            if part_code not in self.parts and part_code not in self.inventory_original_quantities:
                self.inventory_original_quantities[part_code] = (
                    inventory.quantity if inventory is not None else None
                )
            if inventory is None:
                s.add(Inventory(part_code=part_code, quantity=required_for_two_houses))
            else:
                inventory.quantity = required_for_two_houses

    def _capture_expected_inventory(self, s: Session) -> None:
        self.expected_inventory_quantities = {
            part_code: inventory.quantity
            for part_code in self.parts
            if (inventory := s.get(Inventory, part_code)) is not None
        }

    def _create_status_fixture(self, s: Session) -> int:
        product = s.get(Product, "HOUSE_A")
        if product is None:
            raise RuntimeError("HOUSE_A product is required for the status fixture.")
        job = ProductionJob(
            job_code=f"{self.run_id}_STATUS_{len(self.status_job_ids) + 1}",
            product_code="HOUSE_A",
            status=JobStatus.REQUESTED,
        )
        s.add(job)
        s.flush()
        self.status_job_ids.append(job.job_id)
        self.status_product_name = product.product_name
        return job.job_id

    def refresh_status_fixture(self) -> int:
        """Create the newest owned REQUESTED job so latest-job status lookup is deterministic."""
        with self.session_factory() as s:
            job_id = self._create_status_fixture(s)
            s.commit()
        return job_id

    def deplete_house_a(self) -> None:
        with self.session_factory() as s:
            for code in self.parts:
                if "HOUSE_A_BODY" in code:
                    s.get(Inventory, code).quantity = 0
                    self.expected_inventory_quantities[code] = 0
            s.commit()

    def record_pending(self, pending_id: int | None) -> None:
        if isinstance(pending_id, int) and pending_id not in self.pending_ids:
            self.pending_ids.append(pending_id)

    def pending_roof_option(self, pending_id: int | None) -> str | None:
        if not isinstance(pending_id, int) or pending_id not in self.pending_ids:
            return None
        with self.session_factory() as s:
            pending = s.get(PendingProductionRequest, pending_id)
            if pending is None or pending.roof_option_code is None:
                return None
            return pending.roof_option_code.value

    def confirm_shortage_matches_fixture(self, pending_id: int | None, payload: dict[str, Any]) -> bool:
        """Evidence that a real confirmation reached the authoritative revalidation path."""
        message = payload.get("message")
        if not isinstance(pending_id, int) or not isinstance(message, str):
            return False
        if "부족하여 생산할 수 없습니다." not in message:
            return False
        with self.session_factory() as s:
            pending = s.get(PendingProductionRequest, pending_id)
            if pending is None or pending.state is not PendingProductionState.AWAITING_CONFIRMATION:
                return False
            return not s.scalars(
                select(ProductionJob.job_id).where(
                    ProductionJob.source_pending_request_id == pending_id
                )
            ).first()

    def inventory_matches_fixture(self, payload: dict[str, Any]) -> bool:
        items = payload.get("inventory_result") or []
        quantities = {item.get("part_code"): item.get("quantity") for item in items if isinstance(item, dict)}
        return bool(self.expected_inventory_quantities) and all(
            quantities.get(part_code) == quantity
            for part_code, quantity in self.expected_inventory_quantities.items()
        )

    def status_matches_fixture(self, payload: dict[str, Any]) -> bool:
        if self.status_product_name is None or not self.status_job_ids:
            return False
        with self.session_factory() as s:
            latest = s.scalar(select(ProductionJob.job_id).order_by(ProductionJob.job_id.desc()).limit(1))
        expected_message = f"{self.status_product_name} 생산 준비가 완료되어 작업 시작을 기다리고 있습니다."
        return latest == self.status_job_ids[-1] and payload.get("message") == expected_message

    def unsupported_safety_matches(self, session_id: str, payload: dict[str, Any]) -> bool:
        """Check benchmark-observable no-side-effect guarantees for the negative control."""
        if payload.get("pending_request_id") is not None or payload.get("production_job_ids"):
            return False
        if payload.get("conversation_state") is not None:
            return False
        with self.session_factory() as s:
            pending_exists = s.scalars(
                select(PendingProductionRequest.request_id).where(
                    PendingProductionRequest.session_id == session_id
                )
            ).first() is not None
            inventory_unchanged = all(
                (inventory := s.get(Inventory, part_code)) is not None
                and inventory.quantity == expected_quantity
                for part_code, expected_quantity in self.expected_inventory_quantities.items()
            )
        return not pending_exists and inventory_unchanged

    def materialized_jobs_match(self, job_ids: list[int], scenario: Scenario) -> bool:
        if len(job_ids) != scenario.job_count:
            return False
        expected = scenario.turns[0]
        expected_roof = next(turn.roof for turn in scenario.turns if turn.roof is not None)
        with self.session_factory() as s:
            jobs = list(s.scalars(select(ProductionJob).where(ProductionJob.job_id.in_(job_ids))).all())
        return (
            len(jobs) == scenario.job_count
            and all(job.product_code == expected.product for job in jobs)
            and all(job.roof_option_code.value == expected_roof for job in jobs)
            and all(job.status is JobStatus.REQUESTED for job in jobs)
            and sorted(job.source_item_index for job in jobs) == list(range(1, scenario.job_count + 1))
        )

    def cleanup(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.session_factory() as s:
            job_ids = list(s.scalars(select(ProductionJob.job_id).where(ProductionJob.source_pending_request_id.in_(self.pending_ids))).all()) if self.pending_ids else []
            delivery_ids = list(s.scalars(select(JobMaterialDelivery.job_delivery_id).where(JobMaterialDelivery.production_job_id.in_(job_ids))).all()) if job_ids else []
            if delivery_ids:
                counts["feed"] = s.execute(delete(JobMaterialFeedExecution).where(JobMaterialFeedExecution.job_delivery_id.in_(delivery_ids))).rowcount or 0
                counts["delivery_items"] = s.execute(delete(JobMaterialDeliveryItem).where(JobMaterialDeliveryItem.job_delivery_id.in_(delivery_ids))).rowcount or 0
            if job_ids:
                counts["events"] = s.execute(delete(ProductionEvent).where(ProductionEvent.job_id.in_(job_ids))).rowcount or 0
                counts["deliveries"] = s.execute(delete(JobMaterialDelivery).where(JobMaterialDelivery.job_delivery_id.in_(delivery_ids))).rowcount or 0
                from shared.models.factory import JobStep
                counts["steps"] = s.execute(delete(JobStep).where(JobStep.job_id.in_(job_ids))).rowcount or 0
                counts["jobs"] = s.execute(delete(ProductionJob).where(ProductionJob.job_id.in_(job_ids))).rowcount or 0
            if self.pending_ids: counts["pending"] = s.execute(delete(PendingProductionRequest).where(PendingProductionRequest.request_id.in_(self.pending_ids))).rowcount or 0
            counts["status_jobs"] = s.execute(delete(ProductionJob).where(ProductionJob.job_code.like(f"{self.run_id}_STATUS_%"))).rowcount or 0
            if self.stage_ids: counts["stages"] = s.execute(delete(AssemblyRecipeStage).where(AssemblyRecipeStage.recipe_stage_id.in_(self.stage_ids))).rowcount or 0
            if self.parts:
                counts["inventory"] = s.execute(delete(Inventory).where(Inventory.part_code.in_(self.parts))).rowcount or 0
                counts["parts"] = s.execute(delete(Part).where(Part.part_code.in_(self.parts))).rowcount or 0
            for part_code, original_quantity in self.inventory_original_quantities.items():
                if original_quantity is None:
                    counts["restored_inventory_created"] = counts.get("restored_inventory_created", 0) + (
                        s.execute(delete(Inventory).where(Inventory.part_code == part_code)).rowcount or 0
                    )
                else:
                    inventory = s.get(Inventory, part_code)
                    if inventory is not None:
                        inventory.quantity = original_quantity
                        counts["restored_inventory"] = counts.get("restored_inventory", 0) + 1
            if self.created_products: counts["products"] = s.execute(delete(__import__("shared.models.factory", fromlist=["Product"]).Product).where(__import__("shared.models.factory", fromlist=["Product"]).Product.product_code.in_(self.created_products))).rowcount or 0
            s.commit()
        return counts


def record_audio(audio: AudioIO, vad: EnergyVAD) -> bytes:
    audio.start_recording(); vad.reset(); frames: list[Any] = []
    print("Speak now…")
    try:
        while True:
            frame = audio.queue.get(); frames.append(frame)
            if vad.process_frame(frame.flatten()): break
    finally: audio.stop_recording()
    import numpy as np
    return audio.float32_to_wav_bytes(np.concatenate(frames).flatten())


def percentile(values: list[float]) -> dict[str, float | None]:
    return {"mean": statistics.fmean(values) if values else None, "median": statistics.median(values) if values else None, "p95": sorted(values)[max(0, int(.95 * (len(values)-1)))] if values else None}


@dataclass(frozen=True)
class ProbeResult:
    transcript: str
    stt_latency_ms: float | None
    llm_latency_ms: float | None
    command: dict[str, Any] | None
    error: str | None
    interpret_used: bool


class Runner:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.rows: list[dict[str, Any]] = []
        self.diagnostic_interpret = os.getenv("VOICE_E2E_DIAGNOSTIC_INTERPRET") == "1"
        self.audio, self.vad = AudioIO(), EnergyVAD()
        self.artifact_dir: Path | None = None

    def _prepare_artifact_dir(self, run_id: str) -> Path:
        artifact_dir = getattr(self, "artifact_dir", None)
        if artifact_dir is None:
            artifact_dir = ARTIFACTS_DIR / run_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            self.artifact_dir = artifact_dir
        return artifact_dir

    @staticmethod
    def _display_path(path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    def _retain_wav(self, scenario_id: str, turn_no: int, wav: bytes) -> str:
        """Persist the exact bytes supplied to both benchmark Voice API requests."""
        artifact_dir = getattr(self, "artifact_dir", None)
        if artifact_dir is None:
            raise RuntimeError("WAV retention requires a prepared benchmark run artifact directory.")
        path = artifact_dir / f"{scenario_id}_turn{turn_no}.wav"
        path.write_bytes(wav)
        return self._display_path(path)

    @staticmethod
    def _response_error(label: str, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        rendered = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)
        return f"{label} HTTP {response.status_code}: {rendered}"

    def probe_transcription_and_command(self, wav: bytes, *, include_interpret: bool) -> ProbeResult:
        """Measure supplemental STT and only meaningful standalone command interpretation."""
        try:
            with httpx.Client(timeout=120) as client:
                started = time.perf_counter()
                transcribe = client.post(
                    f"{self.base_url}/ai/transcribe",
                    files={"audio": ("voice.wav", wav, "audio/wav")},
                )
                if transcribe.status_code >= 300:
                    return ProbeResult(
                        "", None, None, None,
                        self._response_error("/ai/transcribe", transcribe), False,
                    )
                transcription = transcribe.json()
                stt_latency = transcription.get("processing_time_ms")
                if not isinstance(stt_latency, (int, float)):
                    stt_latency = (time.perf_counter() - started) * 1000
                transcript = transcription.get("text", "")
                if not isinstance(transcript, str):
                    transcript = ""
                if not include_interpret:
                    return ProbeResult(transcript, float(stt_latency), None, None, None, False)

                started = time.perf_counter()
                response = client.post(f"{self.base_url}/ai/interpret", json={"text": transcript})
                llm_latency = (time.perf_counter() - started) * 1000
                if response.status_code >= 300:
                    return ProbeResult(
                        transcript, float(stt_latency), None, None,
                        self._response_error("/ai/interpret", response), True,
                    )
                payload = response.json()
                command = payload.get("command") if isinstance(payload, dict) else None
                return ProbeResult(
                    transcript,
                    float(stt_latency),
                    float(llm_latency),
                    command if isinstance(command, dict) else None,
                    None,
                    True,
                )
        except httpx.HTTPError as exc:
            return ProbeResult("", None, None, None, f"supplemental probe transport error: {exc}", include_interpret)

    def request(
        self, route: str, session_id: str, wav: bytes, correlation_id: str | None = None
    ) -> tuple[dict[str, Any], float, int]:
        started = time.perf_counter()
        try:
            headers = {"X-Voice-E2E-Turn": correlation_id} if correlation_id else None
            with httpx.Client(timeout=120) as client:
                if route == "voice-command":
                    response = client.post(
                        f"{self.base_url}/ai/voice-command",
                        files={"audio": ("voice.wav", wav, "audio/wav")},
                        headers=headers,
                    )
                else:
                    response = client.post(
                        f"{self.base_url}/ai/voice-conversation",
                        data={"session_id": session_id},
                        files={"audio": ("voice.wav", wav, "audio/wav")},
                        headers=headers,
                    )
            try:
                body = response.json() if response.content else {}
            except ValueError:
                body = {"detail": response.text}
            return body if isinstance(body, dict) else {"detail": body}, (time.perf_counter() - started) * 1000, response.status_code
        except httpx.HTTPError as exc:
            return {"detail": f"scored API transport error: {exc}"}, (time.perf_counter() - started) * 1000, 599

    def _blocked_row(self, scenario: Scenario, turn: Turn, turn_no: int, session_id: str, reason: str) -> dict[str, Any]:
        return {
            "scenario_id": scenario.scenario_id,
            "turn_no": turn_no,
            "session_id": session_id,
            "category": scenario.category,
            "reference_text": turn.reference,
            "recognized_text": None,
            "expected_intent": turn.intent,
            "actual_intent": None,
            "intent_correct": False,
            "product_expected": turn.product,
            "product_actual": None,
            "product_correct": False,
            "quantity_expected": turn.quantity,
            "quantity_actual": None,
            "quantity_correct": False,
            "roof_expected": turn.roof,
            "roof_actual": None,
            "roof_correct": False,
            "roof_actual_source": None,
            "conversation_state_expected": turn.state,
            "conversation_state_actual": None,
            "state_correct": False,
            "expected_job_created": turn.creates_job,
            "actual_job_created": False,
            "actual_job_count": 0,
            "job_assertion_pass": False,
            "inventory_assertion_pass": None,
            "status_assertion_pass": None,
            "confirmation_input_correct": False,
            "confirm_revalidation_assertion_pass": None,
            "status_fixture_job_id": None,
            "standalone_interpret_scored": False,
            "probe_intent": None,
            "probe_command_json": None,
            "probe_error": None,
            "scored_api_error": None,
            "scored_response_json": None,
            "scenario_blocked_reason": reason,
            "was_spoken": False,
            "audio_path": None,
            "stt_exact": False,
            "semantic_asr_usable": False,
            "unsupported_semantic_correct": False,
            "unsupported_safety_pass": False,
            "stt_usable": False,
            "business_assertion_pass": False,
            "overall_turn_pass": False,
            "stt_latency_ms": None,
            "command_or_llm_latency_ms": None,
            "api_or_conversation_latency_ms": None,
            "e2e_latency_ms": None,
            "error_detail": reason,
        }

    @staticmethod
    def _unsupported_semantic_match(turn: Turn, transcript: str) -> bool:
        """Negative controls require their declared unsupported lexical intent to survive STT."""
        normalized = normalize_text(transcript).casefold()
        return bool(turn.unsupported_keywords) and all(
            keyword.casefold() in normalized for keyword in turn.unsupported_keywords
        )

    @staticmethod
    def _expected_confirmation_answer(scenario: Scenario, turn_no: int, turn: Turn) -> bool | None:
        if turn_no <= 1:
            return None
        if scenario.mutate_before_confirm and turn_no == 2:
            return True
        if turn.state == "CONFIRMED":
            return True
        if turn.state == "REJECTED":
            return False
        return None

    def run(self, fixture: Fixture) -> None:
        self._prepare_artifact_dir(fixture.run_id)
        for scenario in scenarios():
            session_id = f"{fixture.run_id}_{scenario.scenario_id}"
            blocked_reason: str | None = None
            for turn_no, turn in enumerate(scenario.turns, 1):
                if blocked_reason is not None:
                    row = self._blocked_row(scenario, turn, turn_no, session_id, blocked_reason)
                    self.rows.append(row)
                    print(f"Scenario {scenario.scenario_id} / Turn {turn_no}: BLOCKED ({blocked_reason})")
                    continue

                while True:
                    print(
                        f"\nScenario {scenario.scenario_id} / Turn {turn_no}/{len(scenario.turns)}"
                        f"\nReference: {turn.reference}"
                    )
                    choice = input("Enter=record, r=retry, q=stop: ").strip().lower()
                    if choice == "q":
                        raise KeyboardInterrupt
                    if choice != "r":
                        break
                wav = record_audio(self.audio, self.vad)
                audio_path = self._retain_wav(scenario.scenario_id, turn_no, wav)
                turn_started = time.perf_counter()
                probe = self.probe_transcription_and_command(
                    wav,
                    include_interpret=(
                        getattr(self, "diagnostic_interpret", False) and turn.standalone_interpret
                    ),
                )
                status_fixture_job_id = None
                if scenario.category.startswith("JOB_STATUS"):
                    status_fixture_job_id = fixture.refresh_status_fixture()
                body, latency, status = self.request(
                    turn.route, session_id, wav, f"{session_id}:turn:{turn_no}"
                )
                benchmark_turn_wall_latency = (time.perf_counter() - turn_started) * 1000
                command = body.get("command") if isinstance(body.get("command"), dict) else {}
                actual_state = body.get("conversation_state")
                pending_id = body.get("pending_request_id")
                fixture.record_pending(pending_id)
                actual_roof = command.get("roof_option_code")
                roof_actual_source = "command" if actual_roof is not None else None
                if actual_roof is None and turn.roof is not None:
                    actual_roof = fixture.pending_roof_option(pending_id)
                    if actual_roof is not None:
                        roof_actual_source = "pending_request"
                if scenario.mutate_before_confirm and turn_no == 1:
                    fixture.deplete_house_a()
                intent = command.get("intent")
                created_job_ids = body.get("production_job_ids") or []
                inventory_ok = scenario.category.startswith("INVENTORY") and fixture.inventory_matches_fixture(body)
                status_ok = scenario.category.startswith("JOB_STATUS") and fixture.status_matches_fixture(body)
                job_ok = turn.creates_job and fixture.materialized_jobs_match(created_job_ids, scenario)
                unsupported_semantic_ok = None
                unsupported_safety_ok = None
                if scenario.category == "UNSUPPORTED":
                    unsupported_semantic_ok = self._unsupported_semantic_match(turn, probe.transcript)
                    unsupported_safety_ok = (
                        status < 300
                        and intent == Intent.UNKNOWN.value
                        and fixture.unsupported_safety_matches(session_id, body)
                    )
                expected_confirmation = self._expected_confirmation_answer(scenario, turn_no, turn)
                confirmation_input_ok = (
                    expected_confirmation is None
                    or parse_confirmation_answer(probe.transcript) is expected_confirmation
                )
                revalidation_ok = None
                if scenario.mutate_before_confirm and turn_no == 2:
                    revalidation_ok = fixture.confirm_shortage_matches_fixture(pending_id, body)
                scored_api_error = None
                if status >= 300:
                    endpoint = "/ai/voice-command" if turn.route == "voice-command" else "/ai/voice-conversation"
                    scored_api_error = f"{endpoint} HTTP {status}: {json.dumps(body, ensure_ascii=False)}"
                semantic_checks = [
                    status < 300,
                    turn.intent is None or intent == turn.intent,
                    turn.product is None or command.get("product_code") == turn.product,
                    turn.quantity is None or command.get("quantity") == turn.quantity,
                    turn.roof is None or actual_roof == turn.roof,
                    confirmation_input_ok,
                    unsupported_semantic_ok is None or unsupported_semantic_ok,
                ]
                checks = semantic_checks + [
                    turn.state is None or actual_state == turn.state,
                    not scenario.category.startswith("INVENTORY") or inventory_ok,
                    not scenario.category.startswith("JOB_STATUS") or status_ok,
                    not turn.creates_job or job_ok,
                    turn.creates_job or not created_job_ids,
                    revalidation_ok is None or revalidation_ok,
                    unsupported_safety_ok is None or unsupported_safety_ok,
                ]
                exact = bool(probe.transcript) and normalize_text(probe.transcript) == normalize_text(turn.reference)
                probe_command_json = json.dumps(probe.command, ensure_ascii=False) if probe.command is not None else None
                row = {
                    "scenario_id": scenario.scenario_id,
                    "turn_no": turn_no,
                    "session_id": session_id,
                    "category": scenario.category,
                    "reference_text": turn.reference,
                    "recognized_text": probe.transcript,
                    "expected_intent": turn.intent,
                    "actual_intent": intent,
                    "intent_correct": turn.intent is None or intent == turn.intent,
                    "product_expected": turn.product,
                    "product_actual": command.get("product_code"),
                    "product_correct": turn.product is None or command.get("product_code") == turn.product,
                    "quantity_expected": turn.quantity,
                    "quantity_actual": command.get("quantity"),
                    "quantity_correct": turn.quantity is None or command.get("quantity") == turn.quantity,
                    "roof_expected": turn.roof,
                    "roof_actual": actual_roof,
                    "roof_correct": turn.roof is None or actual_roof == turn.roof,
                    "roof_actual_source": roof_actual_source,
                    "conversation_state_expected": turn.state,
                    "conversation_state_actual": actual_state,
                    "state_correct": turn.state is None or actual_state == turn.state,
                    "expected_job_created": turn.creates_job,
                    "actual_job_created": bool(created_job_ids),
                    "actual_job_count": len(created_job_ids),
                    "job_assertion_pass": job_ok if turn.creates_job else not created_job_ids,
                    "inventory_assertion_pass": inventory_ok if scenario.category.startswith("INVENTORY") else None,
                    "status_assertion_pass": status_ok if scenario.category.startswith("JOB_STATUS") else None,
                    "confirmation_input_correct": confirmation_input_ok if expected_confirmation is not None else None,
                    "confirm_revalidation_assertion_pass": revalidation_ok,
                    "status_fixture_job_id": status_fixture_job_id,
                    "standalone_interpret_scored": probe.interpret_used,
                    "probe_intent": probe.command.get("intent") if probe.command is not None else None,
                    "probe_command_json": probe_command_json,
                    "probe_error": probe.error,
                    "scored_api_error": scored_api_error,
                    "scored_response_json": json.dumps(body, ensure_ascii=False),
                    "scenario_blocked_reason": None,
                    "was_spoken": True,
                    "audio_path": audio_path,
                    "stt_exact": exact,
                    "semantic_asr_usable": all(semantic_checks),
                    "unsupported_semantic_correct": unsupported_semantic_ok,
                    "unsupported_safety_pass": unsupported_safety_ok,
                    "stt_usable": all(checks),
                    "business_assertion_pass": all(checks),
                    "overall_turn_pass": all(checks),
                    "stt_latency_ms": round(probe.stt_latency_ms, 3) if probe.stt_latency_ms is not None else None,
                    "stt_probe_latency_ms": round(probe.stt_latency_ms, 3) if probe.stt_latency_ms is not None else None,
                    "command_or_llm_latency_ms": round(probe.llm_latency_ms, 3) if probe.llm_latency_ms is not None else None,
                    "interpret_probe_latency_ms": round(probe.llm_latency_ms, 3) if probe.llm_latency_ms is not None else None,
                    "api_or_conversation_latency_ms": round(latency, 3),
                    "scored_voice_api_latency_ms": round(latency, 3),
                    "benchmark_turn_wall_latency_ms": round(benchmark_turn_wall_latency, 3),
                    "e2e_latency_ms": round(latency, 3),
                    "error_detail": scored_api_error or probe.error or "",
                }
                self.rows.append(row)
                print(
                    f"Recognized: {probe.transcript}\nIntent: {intent}\nState: {actual_state}"
                    f"\n{'PASS' if row['overall_turn_pass'] else 'FAIL'}"
                )
                prerequisite_invalid = status >= 300 or (
                    turn.state is not None and actual_state != turn.state
                )
                if prerequisite_invalid and turn_no < len(scenario.turns):
                    blocked_reason = (
                        f"prior turn {turn_no} did not establish the required conversation state"
                    )

    def write(self, fixture: Fixture, cleanup: dict[str, int]) -> tuple[Path, Path]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"); csv_path = RESULTS_DIR / f"results_{stamp}.csv"; summary_path = RESULTS_DIR / f"summary_{stamp}.md"
        fields = sorted({key for row in self.rows for key in row}) or ["scenario_id"]
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(self.rows)
        planned_turns = len(self.rows)
        spoken_rows = [row for row in self.rows if row.get("was_spoken")]
        spoken_turns = len(spoken_rows)
        blocked_turns = planned_turns - spoken_turns
        scenario_ids = {row["scenario_id"] for row in self.rows}
        failed = sorted({row["scenario_id"] for row in self.rows if not row["overall_turn_pass"]})
        def metric(key: str, rows: list[dict[str, Any]] | None = None) -> str:
            eligible = spoken_rows if rows is None else rows
            return f"{sum(bool(r.get(key)) for r in eligible)}/{len(eligible)}" if eligible else "0/0"

        def expected_metric(key: str, expected_key: str) -> str:
            return metric(key, [r for r in spoken_rows if r.get(expected_key) is not None])

        stt_latencies = [float(r["stt_probe_latency_ms"]) for r in spoken_rows if r.get("stt_probe_latency_ms") is not None]
        interpret_latencies = [float(r["interpret_probe_latency_ms"]) for r in spoken_rows if r.get("interpret_probe_latency_ms") is not None]
        product_latencies = [float(r["scored_voice_api_latency_ms"]) for r in spoken_rows if r.get("scored_voice_api_latency_ms") is not None]
        turn_wall_latencies = [float(r["benchmark_turn_wall_latency_ms"]) for r in spoken_rows if r.get("benchmark_turn_wall_latency_ms") is not None]
        settings = get_settings()
        summary_path.write_text(
            f"# Voice E2E v1 Summary\n\n"
            f"- Timestamp: {stamp}\n"
            f"- Test DB: {EXPECTED_DATABASE}\n"
            f"- Ollama: {settings.ollama_model}\n"
            f"- Whisper: {settings.whisper_model_size} / {settings.whisper_device} / {settings.whisper_compute_type}\n"
            f"- Scenarios: {len(scenario_ids)}; Planned turns: {planned_turns}\n"
            f"- Actually spoken/scored turns: {spoken_turns}; Blocked/unspoken turns: {blocked_turns}\n"
            f"- Audio artifacts (retained after fixture cleanup): {self._display_path(self.artifact_dir) if getattr(self, 'artifact_dir', None) is not None else 'none'}\n"
            f"- All accuracy metrics below use actually spoken/scored turns as their denominator.\n"
            f"- STT exact: {metric('stt_exact')}\n"
            f"- STT usable (legacy end-to-end): {metric('stt_usable')}\n"
            f"- Semantic ASR usable (intent/entities/context input only): {metric('semantic_asr_usable')}\n"
            f"- Unsupported-command safety pass (applicable negative controls): {metric('unsupported_safety_pass', [r for r in spoken_rows if r.get('unsupported_safety_pass') is not None])}\n"
            f"- Intent accuracy (expected-intent turns): {expected_metric('intent_correct', 'expected_intent')}\n"
            f"- Product accuracy (applicable): {expected_metric('product_correct', 'product_expected')}\n"
            f"- Quantity accuracy (applicable): {expected_metric('quantity_correct', 'quantity_expected')}\n"
            f"- Roof accuracy (applicable): {expected_metric('roof_correct', 'roof_expected')}\n"
            f"- State accuracy (applicable): {expected_metric('state_correct', 'conversation_state_expected')}\n"
            f"- Business assertion pass: {metric('business_assertion_pass')}\n"
            f"- Full turn pass: {metric('overall_turn_pass')}\n"
            f"- STT probe latency ms (benchmark overhead, not scored product path): {percentile(stt_latencies)}\n"
            f"- Optional /ai/interpret diagnostic latency ms (VOICE_E2E_DIAGNOSTIC_INTERPRET=1 only): {percentile(interpret_latencies)}\n"
            f"- Product-path latency / authoritative scored Voice API ms: {percentile(product_latencies)}\n"
            f"- Benchmark turn processing wall latency ms (probe + scored API; excludes speaking/recording): {percentile(turn_wall_latencies)}\n"
            f"- Failed scenarios: {', '.join(failed) if failed else 'none'}\n"
            f"- Cleanup: {cleanup}\n",
            encoding="utf-8",
        )
        return csv_path, summary_path


async def precheck(base_url: str) -> None:
    engine = guarded_engine(); fixture = Fixture(engine)
    try:
        if not await OllamaService().reachable(): raise RuntimeError("Ollama is unreachable.")
        settings = get_settings()
        if not settings.ollama_model.strip(): raise RuntimeError("OLLAMA_MODEL is not configured.")
        await OllamaService().chat("연결 확인")
        await get_stt_service()._get_model()
        import sounddevice as sd
        if not any(item.get("max_input_channels", 0) > 0 for item in sd.query_devices()): raise RuntimeError("No microphone input device.")
        response = httpx.get(f"{base_url.rstrip('/')}/ai/health", timeout=10); response.raise_for_status()
        fixture.setup(); print("VOICE_E2E_CHECK: PASS")
    finally:
        print(f"cleanup={fixture.cleanup()}"); engine.dispose()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"; base_url = os.getenv("VOICE_E2E_API_BASE_URL", "http://127.0.0.1:8010")
    if mode == "check": asyncio.run(precheck(base_url)); return 0
    if mode != "run": raise SystemExit("usage: harness.py check|run")
    engine = guarded_engine(); fixture = Fixture(engine); runner = Runner(base_url)
    try:
        fixture.setup(); runner.run(fixture)
    except KeyboardInterrupt: print("Stopped safely; writing partial results.")
    finally:
        cleanup = fixture.cleanup(); paths = runner.write(fixture, cleanup); engine.dispose(); print(f"CSV={paths[0]}\nSUMMARY={paths[1]}")
    return 0
