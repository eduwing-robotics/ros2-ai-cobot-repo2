from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

import pytest
from pathlib import Path


RUNNER_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "voice_e2e_v1" / "runner.py"
SPEC = importlib.util.spec_from_file_location("voice_e2e_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


@pytest.fixture(autouse=True)
def _isolate_wav_artifacts(monkeypatch, tmp_path) -> None:
    """Keep runner tests from retaining synthetic test WAVs in the repository."""
    monkeypatch.setattr(runner, "ARTIFACTS_DIR", tmp_path / "artifacts")


def test_v1_scenarios_preserve_e001_to_e015_and_practical_turn_count() -> None:
    cases = runner.scenarios()
    assert [case.scenario_id for case in cases] == [f"E{i:03d}" for i in range(1, 16)]
    assert 15 <= sum(len(case.turns) for case in cases) <= 25


def test_v1_multiturn_contracts_keep_confirmation_and_revalidation_coverage() -> None:
    cases = {case.scenario_id: case for case in runner.scenarios()}
    assert len(cases["E002"].turns) == 3
    assert cases["E002"].turns[0].state == "WAITING_ROOF_OPTION"
    assert cases["E002"].turns[-1].creates_job is True
    assert cases["E006"].mutate_before_confirm is True


def test_v1_percentile_summary_is_empty_safe() -> None:
    assert runner.percentile([]) == {"mean": None, "median": None, "p95": None}


def test_v1_guarded_fixture_setup_and_cleanup() -> None:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        import pytest
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 for guarded benchmark fixture validation.")
    engine = runner.guarded_engine()
    fixture = runner.Fixture(engine)
    try:
        fixture.setup()
        assert fixture.parts and fixture.stage_ids
    finally:
        counts = fixture.cleanup()
        engine.dispose()
    assert counts["parts"] == len(fixture.parts)
    assert counts["stages"] == len(fixture.stage_ids)


class _FixtureStub:
    run_id = "VOICEBENCH_TEST"

    def record_pending(self, _pending_id):
        pass

    def pending_roof_option(self, _pending_id):
        return "ROOF_02"

    def deplete_house_a(self):
        pass

    def confirm_shortage_matches_fixture(self, _pending_id, _payload):
        return False

    def refresh_status_fixture(self):
        return 123

    def inventory_matches_fixture(self, _payload):
        return False

    def status_matches_fixture(self, _payload):
        return False

    def unsupported_safety_matches(self, _session_id, _payload):
        return True

    def materialized_jobs_match(self, _job_ids, _scenario):
        return False


def _runner_without_audio() -> object:
    instance = runner.Runner.__new__(runner.Runner)
    instance.base_url = "http://voice.test"
    instance.rows = []
    instance.audio = object()
    instance.vad = object()
    return instance


def _success_body() -> dict:
    return {
        "command": {"intent": "UNKNOWN"},
        "conversation_state": None,
        "pending_request_id": None,
        "production_job_ids": [],
    }


def test_wav_retention_spoken_turn_is_byte_identical_and_recorded_in_row(monkeypatch, tmp_path) -> None:
    scenario = runner.Scenario("T001", "OTHER", (runner.Turn("시험", "UNKNOWN"),))
    wav = b"RIFF\x00\x01voice-e2e-exact-bytes"
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: wav)
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    probe_wavs: list[bytes] = []
    scored_wavs: list[bytes] = []
    instance.probe_transcription_and_command = lambda value, *, include_interpret: (
        probe_wavs.append(value) or runner.ProbeResult("시험", 1.0, None, None, None, include_interpret)
    )
    instance.request = lambda _route, _session_id, value, *_args: (
        scored_wavs.append(value) or (_success_body(), 2.0, 200)
    )

    instance.run(_FixtureStub())

    artifact = tmp_path / "artifacts" / "VOICEBENCH_TEST" / "T001_turn1.wav"
    assert artifact.read_bytes() == wav
    assert probe_wavs == [wav]
    assert scored_wavs == [wav]
    assert instance.rows[0]["audio_path"] == str(artifact)


def test_wav_retention_never_creates_artifacts_for_blocked_turns(monkeypatch, tmp_path) -> None:
    scenario = runner.Scenario(
        "T001", "OTHER",
        (runner.Turn("첫 요청", "UNKNOWN"), runner.Turn("따라 말하기")),
    )
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"first-turn-only")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: runner.ProbeResult(
        "첫 요청", 1.0, None, None, None, include_interpret
    )
    instance.request = lambda *_args: ({"detail": "scored failure"}, 2.0, 502)

    instance.run(_FixtureStub())

    artifact_dir = tmp_path / "artifacts" / "VOICEBENCH_TEST"
    assert [path.name for path in artifact_dir.iterdir()] == ["T001_turn1.wav"]
    assert instance.rows[1]["was_spoken"] is False
    assert instance.rows[1]["audio_path"] is None


def test_wav_retention_survives_safe_partial_stop_and_result_write(monkeypatch, tmp_path) -> None:
    scenario = runner.Scenario(
        "T001", "OTHER",
        (runner.Turn("첫 요청", "UNKNOWN"), runner.Turn("두 번째 요청", "UNKNOWN")),
    )
    answers = iter(("", "q"))
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"preserve-before-q")
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    instance = _runner_without_audio()
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: runner.ProbeResult(
        "첫 요청", 1.0, None, None, None, include_interpret
    )
    instance.request = lambda *_args: (_success_body(), 2.0, 200)
    fixture = _FixtureStub()

    with pytest.raises(KeyboardInterrupt):
        instance.run(fixture)
    csv_path, _ = instance.write(fixture, cleanup={})

    artifact = tmp_path / "artifacts" / "VOICEBENCH_TEST" / "T001_turn1.wav"
    assert artifact.read_bytes() == b"preserve-before-q"
    assert not (artifact.parent / "T001_turn2.wav").exists()
    with csv_path.open(encoding="utf-8", newline="") as handle:
        assert next(__import__("csv").DictReader(handle))["audio_path"] == str(artifact)


def test_lv4_context_dependent_turns_do_not_use_standalone_interpret() -> None:
    cases = {case.scenario_id: case for case in runner.scenarios()}
    assert cases["E001"].turns[1].standalone_interpret is False
    assert all(turn.standalone_interpret is False for turn in cases["E002"].turns[1:])
    assert cases["E005"].turns[1].standalone_interpret is False
    assert cases["E006"].turns[1].standalone_interpret is False


def test_lv5_lv6_supplemental_interpret_502_is_recorded_without_raise(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.content = b"body"
            self.text = str(payload)

        def json(self):
            return self._payload

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, **_kwargs):
            if url.endswith("/ai/transcribe"):
                return Response(200, {"text": "네", "processing_time_ms": 12.0})
            return Response(502, {"detail": "LLM 응답 형식이 올바르지 않습니다."})

    monkeypatch.setattr(runner.httpx, "Client", lambda **_kwargs: Client())
    instance = _runner_without_audio()
    probe = instance.probe_transcription_and_command(b"wav", include_interpret=True)

    assert probe.transcript == "네"
    assert probe.llm_latency_ms is None
    assert probe.command is None
    assert probe.error is not None and "/ai/interpret HTTP 502" in probe.error


def test_lv5_supplemental_failure_does_not_abort_scored_turn(monkeypatch) -> None:
    scenario = runner.Scenario("T001", "OTHER", (runner.Turn("시험", "UNKNOWN"),))
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    instance = _runner_without_audio()
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: runner.ProbeResult(
        "시험", 1.0, None, None, "/ai/interpret HTTP 502: bad JSON", include_interpret
    )
    instance.request = lambda *_args: (_success_body(), 2.0, 200)

    instance.run(_FixtureStub())

    assert len(instance.rows) == 1
    assert instance.rows[0]["overall_turn_pass"] is True
    assert instance.rows[0]["probe_error"].startswith("/ai/interpret HTTP 502")
    assert instance.rows[0]["scored_api_error"] is None


def test_lv7_lv9_scored_api_failure_is_recorded_and_next_scenario_continues(monkeypatch) -> None:
    first = runner.Scenario("T001", "OTHER", (runner.Turn("첫 번째", "UNKNOWN"),))
    second = runner.Scenario("T002", "OTHER", (runner.Turn("두 번째", "UNKNOWN"),))
    monkeypatch.setattr(runner, "scenarios", lambda: (first, second))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    instance = _runner_without_audio()
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: runner.ProbeResult(
        "시험", 1.0, None, None, None, include_interpret
    )
    responses = iter([
        ({"detail": "LLM 응답 형식이 올바르지 않습니다."}, 2.0, 502),
        (_success_body(), 2.0, 200),
    ])
    instance.request = lambda *_args: next(responses)

    instance.run(_FixtureStub())

    assert len(instance.rows) == 2
    assert instance.rows[0]["overall_turn_pass"] is False
    assert "/ai/voice-conversation HTTP 502" in instance.rows[0]["scored_api_error"]
    assert instance.rows[1]["overall_turn_pass"] is True




def test_second_run_e002_uses_authoritative_pending_roof_and_does_not_block(monkeypatch) -> None:
    scenario = runner.Scenario(
        "E002", "ROOF_MULTI_TURN",
        (
            runner.Turn("첫 요청", "CREATE_PRODUCTION_REQUEST", "HOUSE_A", 1, state="WAITING_ROOF_OPTION"),
            runner.Turn("경사지붕", roof="ROOF_02", state="AWAITING_CONFIRMATION", standalone_interpret=False),
            runner.Turn("네", state="CONFIRMED", creates_job=False, standalone_interpret=False),
        ),
    )
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    probes = iter([
        runner.ProbeResult("첫 요청", 1.0, None, None, None, False),
        runner.ProbeResult("경사지붕", 1.0, None, None, None, False),
        runner.ProbeResult("네", 1.0, None, None, None, False),
    ])
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: next(probes)
    responses = iter([
        ({"command": {"intent": "CREATE_PRODUCTION_REQUEST", "product_code": "HOUSE_A", "quantity": 1}, "conversation_state": "WAITING_ROOF_OPTION", "pending_request_id": 1, "production_job_ids": []}, 1.0, 200),
        ({"command": None, "conversation_state": "AWAITING_CONFIRMATION", "pending_request_id": 1, "production_job_ids": []}, 1.0, 200),
        ({"command": None, "conversation_state": "CONFIRMED", "pending_request_id": 1, "production_job_ids": []}, 1.0, 200),
    ])
    instance.request = lambda *_args: next(responses)

    instance.run(_FixtureStub())

    assert len(instance.rows) == 3
    assert instance.rows[1]["roof_actual"] == "ROOF_02"
    assert instance.rows[1]["roof_actual_source"] == "pending_request"
    assert instance.rows[2]["scenario_blocked_reason"] is None


def test_second_run_e006_requires_actual_confirm_shortage_evidence(monkeypatch) -> None:
    scenario = runner.Scenario(
        "E006", "CONFIRM_REVALIDATION",
        (
            runner.Turn("첫 요청", "CREATE_PRODUCTION_REQUEST", "HOUSE_A", 1, "ROOF_01", "AWAITING_CONFIRMATION"),
            runner.Turn("네", state="AWAITING_CONFIRMATION", standalone_interpret=False),
        ),
        mutate_before_confirm=True,
    )
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    probes = iter([
        runner.ProbeResult("첫 요청", 1.0, None, None, None, False),
        runner.ProbeResult("에이", 1.0, None, None, None, False),
    ])
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: next(probes)
    responses = iter([
        ({"command": {"intent": "CREATE_PRODUCTION_REQUEST", "product_code": "HOUSE_A", "quantity": 1, "roof_option_code": "ROOF_01"}, "conversation_state": "AWAITING_CONFIRMATION", "pending_request_id": 1, "production_job_ids": []}, 1.0, 200),
        ({"command": None, "message": "A형 주택 한 채를 1번 평지붕으로 제작하는 것이 맞습니까?", "conversation_state": "AWAITING_CONFIRMATION", "pending_request_id": 1, "production_job_ids": []}, 1.0, 200),
    ])
    instance.request = lambda *_args: next(responses)

    instance.run(_FixtureStub())

    final = instance.rows[1]
    assert final["confirmation_input_correct"] is False
    assert final["confirm_revalidation_assertion_pass"] is False
    assert final["overall_turn_pass"] is False


def test_second_run_semantic_asr_metric_does_not_include_inventory_business_assertion(monkeypatch) -> None:
    scenario = runner.Scenario("E007", "INVENTORY_ALL", (runner.Turn("재고 알려줘", "QUERY_INVENTORY", route="voice-command"),))
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: runner.ProbeResult(
        "재고 알려줘", 1.0, 1.0, {"intent": "QUERY_INVENTORY"}, None, True
    )
    instance.request = lambda *_args: ({"command": {"intent": "QUERY_INVENTORY"}, "conversation_state": None, "pending_request_id": None, "production_job_ids": [], "inventory_result": []}, 1.0, 200)

    instance.run(_FixtureStub())

    row = instance.rows[0]
    assert row["semantic_asr_usable"] is True
    assert row["stt_usable"] is False
    assert row["business_assertion_pass"] is False




class _UnsafeUnsupportedFixture(_FixtureStub):
    def unsupported_safety_matches(self, _session_id, _payload):
        return False


def _run_e015(monkeypatch, transcript: str, actual_intent: str, *, fixture=None):
    case = next(case for case in runner.scenarios() if case.scenario_id == "E015")
    monkeypatch.setattr(runner, "scenarios", lambda: (case,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: runner.ProbeResult(
        transcript, 1.0, 1.0, {"intent": actual_intent}, None, include_interpret
    )
    instance.request = lambda *_args: ({
        "command": {"intent": actual_intent},
        "conversation_state": None,
        "pending_request_id": None,
        "production_job_ids": [],
    }, 1.0, 200)
    instance.run(_FixtureStub() if fixture is None else fixture)
    return instance.rows[0]


def test_e015_t1_faithful_unsupported_command_passes_semantic_and_safety(monkeypatch) -> None:
    row = _run_e015(monkeypatch, "PLC 리셋해줘", "UNKNOWN")
    assert row["stt_exact"] is True
    assert row["semantic_asr_usable"] is True
    assert row["unsupported_semantic_correct"] is True
    assert row["unsupported_safety_pass"] is True
    assert row["overall_turn_pass"] is True


def test_e015_t2_corrupted_unsupported_command_fails_semantic_but_not_safety(monkeypatch) -> None:
    row = _run_e015(monkeypatch, "피해시 리셋 해줘", "UNKNOWN")
    assert row["stt_exact"] is False
    assert row["semantic_asr_usable"] is False
    assert row["unsupported_semantic_correct"] is False
    assert row["unsupported_safety_pass"] is True
    assert row["overall_turn_pass"] is False


def test_e015_t3_arbitrary_unknown_is_not_semantic_pass(monkeypatch) -> None:
    row = _run_e015(monkeypatch, "안녕하세요", "UNKNOWN")
    assert row["semantic_asr_usable"] is False
    assert row["unsupported_safety_pass"] is True
    assert row["overall_turn_pass"] is False


def test_e015_t4_supported_command_is_safety_and_overall_failure(monkeypatch) -> None:
    row = _run_e015(monkeypatch, "PLC 리셋해줘", "CREATE_PRODUCTION_REQUEST")
    assert row["intent_correct"] is False
    assert row["unsupported_safety_pass"] is False
    assert row["overall_turn_pass"] is False


def test_e015_t5_fixture_safety_failure_is_not_hidden(monkeypatch) -> None:
    row = _run_e015(monkeypatch, "PLC 리셋해줘", "UNKNOWN", fixture=_UnsafeUnsupportedFixture())
    assert row["unsupported_safety_pass"] is False
    assert row["overall_turn_pass"] is False


def test_e015_t6_blocked_turns_are_not_accuracy_denominator(monkeypatch, tmp_path) -> None:
    instance = _runner_without_audio()
    instance.rows = [
        {"scenario_id": "T001", "was_spoken": True, "stt_exact": True, "semantic_asr_usable": True, "stt_usable": True, "intent_correct": True, "expected_intent": "UNKNOWN", "product_correct": True, "product_expected": None, "quantity_correct": True, "quantity_expected": None, "roof_correct": True, "roof_expected": None, "state_correct": True, "conversation_state_expected": None, "unsupported_safety_pass": None, "business_assertion_pass": True, "overall_turn_pass": True, "stt_latency_ms": 1.0, "stt_probe_latency_ms": 1.0, "command_or_llm_latency_ms": None, "interpret_probe_latency_ms": None, "scored_voice_api_latency_ms": 1.0, "benchmark_turn_wall_latency_ms": 1.2, "e2e_latency_ms": 1.0},
        {"scenario_id": "T001", "was_spoken": False, "stt_exact": False, "semantic_asr_usable": False, "stt_usable": False, "intent_correct": False, "expected_intent": None, "product_correct": False, "product_expected": None, "quantity_correct": False, "quantity_expected": None, "roof_correct": False, "roof_expected": None, "state_correct": False, "conversation_state_expected": None, "unsupported_safety_pass": False, "business_assertion_pass": False, "overall_turn_pass": False, "stt_latency_ms": None, "stt_probe_latency_ms": None, "command_or_llm_latency_ms": None, "interpret_probe_latency_ms": None, "scored_voice_api_latency_ms": None, "benchmark_turn_wall_latency_ms": None, "e2e_latency_ms": None},
    ]
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    _, summary = instance.write(_FixtureStub(), {})
    text = summary.read_text(encoding="utf-8")
    assert "Planned turns: 2" in text
    assert "Actually spoken/scored turns: 1; Blocked/unspoken turns: 1" in text
    assert "STT exact: 1/1" in text
    assert "Semantic ASR usable (intent/entities/context input only): 1/1" in text
    assert "Full turn pass: 1/1" in text
    assert "STT probe latency ms" in text
    assert "Product-path latency / authoritative scored Voice API ms" in text




def test_latency_t1_t3_default_run_skips_interpret_and_calls_scored_api_once(monkeypatch) -> None:
    scenario = runner.Scenario("T001", "OTHER", (runner.Turn("시험", "UNKNOWN"),))
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    instance.diagnostic_interpret = False
    interpret_flags = []
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: (
        interpret_flags.append(include_interpret) or runner.ProbeResult("시험", 1.0, None, None, None, False)
    )
    scored_calls = []
    instance.request = lambda *_args: (scored_calls.append(True) or (_success_body(), 2.0, 200))

    instance.run(_FixtureStub())

    assert interpret_flags == [False]
    assert len(scored_calls) == 1
    row = instance.rows[0]
    assert row["recognized_text"] == "시험"
    assert row["interpret_probe_latency_ms"] is None
    assert row["scored_voice_api_latency_ms"] == 2.0


def test_latency_t4_t5_diagnostic_interpret_is_opt_in_and_non_authoritative(monkeypatch) -> None:
    scenario = runner.Scenario("T001", "OTHER", (runner.Turn("시험", "UNKNOWN"),))
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    instance.diagnostic_interpret = True
    interpret_flags = []
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: (
        interpret_flags.append(include_interpret) or runner.ProbeResult("시험", 1.0, None, None, "/ai/interpret HTTP 502", True)
    )
    scored_calls = []
    instance.request = lambda *_args: (scored_calls.append(True) or (_success_body(), 2.0, 200))

    instance.run(_FixtureStub())

    assert interpret_flags == [True]
    assert len(scored_calls) == 1
    assert instance.rows[0]["probe_error"] == "/ai/interpret HTTP 502"
    assert instance.rows[0]["overall_turn_pass"] is True


def test_latency_t6_context_followup_never_uses_diagnostic_interpret(monkeypatch) -> None:
    scenario = runner.Scenario(
        "T001", "OTHER",
        (runner.Turn("첫 요청", "UNKNOWN"), runner.Turn("네", standalone_interpret=False)),
    )
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    instance = _runner_without_audio()
    instance.diagnostic_interpret = True
    flags = []
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: (
        flags.append(include_interpret) or runner.ProbeResult("시험", 1.0, None, None, None, include_interpret)
    )
    instance.request = lambda *_args: (_success_body(), 2.0, 200)

    instance.run(_FixtureStub())

    assert flags == [True, False]


def test_lv8_dependent_turn_is_blocked_after_scored_failure(monkeypatch) -> None:
    scenario = runner.Scenario(
        "T001",
        "OTHER",
        (runner.Turn("첫 번째", "UNKNOWN"), runner.Turn("네")),
    )
    monkeypatch.setattr(runner, "scenarios", lambda: (scenario,))
    monkeypatch.setattr(runner, "record_audio", lambda *_args: b"wav")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")

    instance = _runner_without_audio()
    instance.probe_transcription_and_command = lambda _wav, *, include_interpret: runner.ProbeResult(
        "시험", 1.0, None, None, None, include_interpret
    )
    calls = []
    def request(*_args):
        calls.append(True)
        return {"detail": "bad"}, 2.0, 502
    instance.request = request

    instance.run(_FixtureStub())

    assert len(calls) == 1
    assert len(instance.rows) == 2
    assert instance.rows[1]["scenario_blocked_reason"] is not None
    assert instance.rows[1]["overall_turn_pass"] is False


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Set RUN_POSTGRES_INTEGRATION=1 for guarded benchmark fixture validation.",
)
def test_lv1_lv2_lv3_lv10_guarded_fixture_matches_real_preflight() -> None:
    from api_server.services.production_conversation_service import ProductionConversationService
    from shared.enums.ai import Intent
    from shared.models.factory import PendingProductionState, RoofOptionCode
    from shared.schemas.ai import StructuredCommand
    from shared.services.pending_production_request_service import PendingProductionRequestService
    from shared.services.production_inventory_preflight_service import ProductionInventoryPreflightService
    from shared.services.production_request_materialization_service import ProductionRequestMaterializationService

    class Interpreter:
        def __init__(self, command):
            self.command = command

        async def interpret(self, text):
            return text, self.command, ""

    engine = runner.guarded_engine()
    fixture = runner.Fixture(engine)
    originals = {}
    try:
        fixture.setup()
        originals = dict(fixture.inventory_original_quantities)
        with fixture.session_factory() as session:
            e001 = StructuredCommand(
                intent=Intent.CREATE_PRODUCTION_REQUEST,
                product_code="HOUSE_A",
                product_name="A형 주택",
                quantity=1,
                roof_option_code=RoofOptionCode.ROOF_01,
                requires_confirmation=True,
                clarification_needed=False,
            )
            service = ProductionConversationService(
                pending_service=PendingProductionRequestService(session),
                interpreter=Interpreter(e001),
                materialization_service=ProductionRequestMaterializationService(session),
                preflight_service=ProductionInventoryPreflightService(session),
            )
            first = asyncio.run(service.handle_text(
                session_id=f"{fixture.run_id}_E001",
                text="A형 주택, 1개, 평지붕, 만들어줘",
            ))
            assert first.pending is not None
            assert first.pending.state is PendingProductionState.AWAITING_CONFIRMATION
            fixture.record_pending(first.pending.request_id)

            confirmed = asyncio.run(service.handle_text(
                session_id=f"{fixture.run_id}_E001",
                text="네",
            ))
            assert confirmed.pending is not None
            assert confirmed.pending.state is PendingProductionState.CONFIRMED
            assert len(confirmed.production_jobs) == 1
            assert fixture.materialized_jobs_match(
                [job.job_id for job in confirmed.production_jobs],
                next(case for case in runner.scenarios() if case.scenario_id == "E001"),
            )

            e002 = StructuredCommand(
                intent=Intent.CREATE_PRODUCTION_REQUEST,
                product_code="HOUSE_A",
                product_name="A형 주택",
                quantity=1,
                roof_option_code=None,
                requires_confirmation=True,
                clarification_needed=False,
            )
            roof_service = ProductionConversationService(
                pending_service=PendingProductionRequestService(session),
                interpreter=Interpreter(e002),
                materialization_service=ProductionRequestMaterializationService(session),
                preflight_service=ProductionInventoryPreflightService(session),
            )
            roof_pending = asyncio.run(roof_service.handle_text(
                session_id=f"{fixture.run_id}_E002",
                text="A형 주택 한 채 만들어줘",
            ))
            assert roof_pending.pending is not None
            assert roof_pending.pending.state is PendingProductionState.WAITING_ROOF_OPTION
            fixture.record_pending(roof_pending.pending.request_id)
            roof_selected = asyncio.run(roof_service.handle_text(
                session_id=f"{fixture.run_id}_E002",
                text="경사지붕",
            ))
            assert roof_selected.pending is not None
            assert roof_selected.pending.state is PendingProductionState.AWAITING_CONFIRMATION
            assert roof_selected.pending.roof_option_code is RoofOptionCode.ROOF_02
            assert fixture.pending_roof_option(roof_selected.pending.request_id) == "ROOF_02"
            roof_confirmed = asyncio.run(roof_service.handle_text(
                session_id=f"{fixture.run_id}_E002",
                text="네",
            ))
            assert fixture.materialized_jobs_match(
                [job.job_id for job in roof_confirmed.production_jobs],
                next(case for case in runner.scenarios() if case.scenario_id == "E002"),
            )

            e003 = StructuredCommand(
                intent=Intent.CREATE_PRODUCTION_REQUEST,
                product_code="HOUSE_A",
                product_name="A형 주택",
                quantity=2,
                roof_option_code=RoofOptionCode.ROOF_01,
                requires_confirmation=True,
                clarification_needed=False,
            )
            quantity_service = ProductionConversationService(
                pending_service=PendingProductionRequestService(session),
                interpreter=Interpreter(e003),
                materialization_service=ProductionRequestMaterializationService(session),
                preflight_service=ProductionInventoryPreflightService(session),
            )
            quantity_pending = asyncio.run(quantity_service.handle_text(
                session_id=f"{fixture.run_id}_E003",
                text="A형 두 채 평지붕으로 생산해줘",
            ))
            assert quantity_pending.pending is not None
            assert quantity_pending.pending.state is PendingProductionState.AWAITING_CONFIRMATION
            fixture.record_pending(quantity_pending.pending.request_id)
            quantity_confirmed = asyncio.run(quantity_service.handle_text(
                session_id=f"{fixture.run_id}_E003",
                text="네",
            ))
            assert len(quantity_confirmed.production_jobs) == 2
            assert fixture.materialized_jobs_match(
                [job.job_id for job in quantity_confirmed.production_jobs],
                next(case for case in runner.scenarios() if case.scenario_id == "E003"),
            )

            e004 = StructuredCommand(
                intent=Intent.CREATE_PRODUCTION_REQUEST,
                product_code="HOUSE_B",
                product_name="B형 주택",
                quantity=1,
                roof_option_code=RoofOptionCode.ROOF_02,
                requires_confirmation=True,
                clarification_needed=False,
            )
            shortage_service = ProductionConversationService(
                pending_service=PendingProductionRequestService(session),
                interpreter=Interpreter(e004),
                materialization_service=ProductionRequestMaterializationService(session),
                preflight_service=ProductionInventoryPreflightService(session),
            )
            shortage = asyncio.run(shortage_service.handle_text(
                session_id=f"{fixture.run_id}_E004",
                text="B형 주택, 1개, 경사지붕, 만들어줘",
            ))
            assert shortage.pending is not None
            assert shortage.pending.state is PendingProductionState.REJECTED
            assert shortage.production_jobs == ()
            fixture.record_pending(shortage.pending.request_id)

            e006 = StructuredCommand(
                intent=Intent.CREATE_PRODUCTION_REQUEST,
                product_code="HOUSE_A",
                product_name="A형 주택",
                quantity=1,
                roof_option_code=RoofOptionCode.ROOF_01,
                requires_confirmation=True,
                clarification_needed=False,
            )
            revalidation_service = ProductionConversationService(
                pending_service=PendingProductionRequestService(session),
                interpreter=Interpreter(e006),
                materialization_service=ProductionRequestMaterializationService(session),
                preflight_service=ProductionInventoryPreflightService(session),
            )
            revalidation_pending = asyncio.run(revalidation_service.handle_text(
                session_id=f"{fixture.run_id}_E006",
                text="A형 주택 한 채 평지붕으로 만들어줘",
            ))
            assert revalidation_pending.pending is not None
            assert revalidation_pending.pending.state is PendingProductionState.AWAITING_CONFIRMATION
            fixture.record_pending(revalidation_pending.pending.request_id)
            fixture.deplete_house_a()
            revalidation_shortage = asyncio.run(revalidation_service.handle_text(
                session_id=f"{fixture.run_id}_E006", text="네",
            ))
            assert revalidation_shortage.pending is not None
            assert revalidation_shortage.pending.state is PendingProductionState.AWAITING_CONFIRMATION
            assert "부족하여 생산할 수 없습니다." in revalidation_shortage.message
            assert revalidation_shortage.production_jobs == ()
            assert fixture.confirm_shortage_matches_fixture(
                revalidation_shortage.pending.request_id,
                {"message": revalidation_shortage.message},
            )

            unsupported = StructuredCommand(
                intent=Intent.UNKNOWN,
                clarification_needed=True,
                clarification_message="지원하지 않는 명령입니다.",
            )
            unsupported_service = ProductionConversationService(
                pending_service=PendingProductionRequestService(session),
                interpreter=Interpreter(unsupported),
                materialization_service=ProductionRequestMaterializationService(session),
                preflight_service=ProductionInventoryPreflightService(session),
            )
            unsupported_result = asyncio.run(unsupported_service.handle_text(
                session_id=f"{fixture.run_id}_E015", text="PLC 리셋해줘",
            ))
            assert unsupported_result.command is not None
            assert unsupported_result.command.intent is Intent.UNKNOWN
            assert unsupported_result.pending is None
            assert unsupported_result.production_jobs == ()
            assert fixture.unsupported_safety_matches(
                f"{fixture.run_id}_E015",
                {
                    "pending_request_id": None,
                    "conversation_state": None,
                    "production_job_ids": [],
                },
            )
    finally:
        cleanup = fixture.cleanup()
        engine.dispose()

    assert cleanup["parts"] == len(fixture.parts)
    assert cleanup["stages"] == len(fixture.stage_ids)
    assert cleanup["status_jobs"] >= 1
    assert cleanup.get("restored_inventory", 0) + cleanup.get("restored_inventory_created", 0) == len(originals)
