from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import factory_stack


def test_benchmark_profile_is_safe_default() -> None:
    plan = factory_stack.plan_for_profile("benchmark")

    assert plan.database_target == "benchmark"
    assert plan.cell_transport == "fake"
    assert plan.material_prefetch_mode == "disabled"
    assert plan.vision_mode == "disabled"
    assert plan.telemetry_enabled is False


def test_actual_profile_is_not_a_production_database_opt_in() -> None:
    plan = factory_stack.plan_for_profile("actual")

    assert plan.database_target == "benchmark"
    assert plan.cell_transport == ""
    assert plan.telemetry_ros_enabled is True
    assert plan.telemetry_enabled is True
    assert plan.vision_mode == "actual"


def test_actual_profile_rejects_blank_transport_selection() -> None:
    inputs = iter(("", ""))

    with pytest.raises(factory_stack.LauncherError, match="Robot Cell transport requires an explicit selection"):
        factory_stack.customize(factory_stack.plan_for_profile("actual"), input_fn=lambda _: next(inputs))


@pytest.mark.parametrize("transport", ("ros2", "fake"))
def test_actual_profile_requires_and_accepts_an_explicit_transport(transport: str) -> None:
    inputs = iter(("", transport, "", "", "", "", "", ""))

    plan = factory_stack.customize(factory_stack.plan_for_profile("actual"), input_fn=lambda _: next(inputs))

    assert plan.database_target == "benchmark"
    assert plan.cell_transport == transport


def test_actual_profile_rejects_invalid_transport_selection() -> None:
    inputs = iter(("", "invalid"))

    with pytest.raises(factory_stack.LauncherError, match="Invalid selection"):
        factory_stack.customize(factory_stack.plan_for_profile("actual"), input_fn=lambda _: next(inputs))


def test_fake_cell_commands_can_be_combined_with_opt_in_real_telemetry() -> None:
    plan = factory_stack.StackPlan("custom", "benchmark", "fake", "disabled", True, False, "disabled", telemetry_enabled=True)

    plan.validate()
    assert plan.cell_transport == "fake"
    assert plan.telemetry_ros_enabled is True


def test_invalid_cell_transport_is_rejected() -> None:
    plan = factory_stack.StackPlan("custom", "benchmark", "false", "disabled", False, False, "disabled")

    with pytest.raises(factory_stack.LauncherError, match="CELL_TRANSPORT"):
        plan.validate()


def test_database_name_validation_is_fail_closed() -> None:
    factory_stack.validate_current_database("benchmark", "smart_factory_benchmark")

    with pytest.raises(factory_stack.LauncherError, match="expected smart_factory_benchmark"):
        factory_stack.validate_current_database("benchmark", "smart_factory_db")


def test_production_requires_the_exact_database_name() -> None:
    factory_stack.require_production_confirmation("smart_factory_db")

    with pytest.raises(factory_stack.LauncherError):
        factory_stack.require_production_confirmation("yes")


def test_schema_revision_preflight_allows_exact_current_source_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory_stack, "source_alembic_head", lambda: "20260907_01")
    monkeypatch.setattr(factory_stack, "current_alembic_revision", lambda url, env: "20260907_01")

    assert factory_stack.validate_alembic_revision("postgresql://unused", {}) == ("20260907_01", "20260907_01")


@pytest.mark.parametrize("actual", ("old_revision", "future_revision", ""))
def test_schema_revision_preflight_blocks_stale_ahead_or_unknown_revision(
    monkeypatch: pytest.MonkeyPatch, actual: str
) -> None:
    monkeypatch.setattr(factory_stack, "source_alembic_head", lambda: "20260907_01")
    monkeypatch.setattr(factory_stack, "current_alembic_revision", lambda url, env: actual)

    with pytest.raises(factory_stack.LauncherError, match="DATABASE_SCHEMA_REVISION_MISMATCH"):
        factory_stack.validate_alembic_revision("postgresql://unused", {})


def test_schema_revision_preflight_reads_only_alembic_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="20260907_01\n")

    monkeypatch.setattr(factory_stack.subprocess, "run", fake_run)

    assert factory_stack.current_alembic_revision("postgresql://unused", {}) == "20260907_01"
    code = calls[0][0][2]
    assert "SELECT version_num FROM alembic_version" in code
    assert "upgrade" not in code.lower()
    assert "alembic" not in calls[0][0]


def test_revision_mismatch_stops_before_settings_or_process_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = factory_stack.plan_for_profile("benchmark")
    monkeypatch.setattr(factory_stack, "current_database", lambda url, env: "smart_factory_benchmark")
    monkeypatch.setattr(factory_stack, "source_alembic_head", lambda: "20260907_01")
    monkeypatch.setattr(factory_stack, "current_alembic_revision", lambda url, env: "stale")
    monkeypatch.setattr(
        factory_stack,
        "settings_from",
        lambda env: (_ for _ in ()).throw(AssertionError("settings/process preflight must not run")),
    )

    with pytest.raises(factory_stack.LauncherError, match="DATABASE_SCHEMA_REVISION_MISMATCH"):
        factory_stack.validate_environment(plan, {"POSTGRES_TEST_DATABASE_URL": "postgresql://unused"})


def test_telemetry_uses_uvicorn_application_entrypoint() -> None:
    plan = factory_stack.plan_for_profile("benchmark")
    command = factory_stack.component_command(
        "telemetry",
        plan,
        {"api_host": "0.0.0.0", "api_port": 8000, "telemetry_host": "0.0.0.0", "telemetry_port": 8001, "ros_domain_id": 73},
    )

    assert "python -m uvicorn telemetry_gateway.main:app" in command
    assert "python -m telemetry_gateway.main" not in command


def test_component_command_does_not_embed_database_credentials() -> None:
    command = factory_stack.component_command(
        "api",
        factory_stack.plan_for_profile("benchmark"),
        {"api_host": "0.0.0.0", "api_port": 8000, "telemetry_host": "0.0.0.0", "telemetry_port": 8001, "ros_domain_id": 73},
    )

    assert "POSTGRES_TEST_DATABASE_URL" in command
    assert "postgresql://" not in command


def test_diagnostics_never_include_database_url() -> None:
    text = factory_stack.display(
        factory_stack.plan_for_profile("benchmark"),
        {"api_port": 8000, "telemetry_port": 8001, "ros_domain_id": 73},
        "smart_factory_benchmark",
    )

    assert "smart_factory_benchmark" in text
    assert "postgresql://" not in text
    assert "password" not in text.lower()


def test_actual_launcher_diagnostics_distinguish_pre_roof_advertised_and_bind_hosts() -> None:
    text = factory_stack.display(
        factory_stack.plan_for_profile("actual"),
        {
            "api_port": 8000,
            "telemetry_port": 8001,
            "ros_domain_id": 73,
            "fms_pre_roof_result_udp_host": "192.168.20.20",
            "fms_pre_roof_result_udp_bind_host": "0.0.0.0",
            "fms_pre_roof_result_udp_port": 20062,
        },
        "smart_factory_benchmark",
    )

    assert "PRE_ROOF Result advertised  192.168.20.20:20062" in text
    assert "PRE_ROOF Result bind        0.0.0.0:20062" in text


def test_disabled_vision_unsets_standard_incoming_qa_transport_keys() -> None:
    command = factory_stack.component_command(
        "fms",
        factory_stack.plan_for_profile("benchmark"),
        {"api_host": "0.0.0.0", "api_port": 8000, "telemetry_host": "0.0.0.0", "telemetry_port": 8001, "ros_domain_id": 73},
    )

    assert "unset VISION_INCOMING_QA_UDP_HOST" in command
    assert "unset FMS_INCOMING_QA_RESULT_UDP_PORT" in command
    assert "unset FMS_PRE_ROOF_RESULT_UDP_BIND_HOST" in command



def test_duplicate_tmux_session_refuses_start_before_environment_or_db_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory_stack.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(factory_stack, "tmux_exists", lambda: True)

    with pytest.raises(factory_stack.LauncherError, match="already running"):
        factory_stack.start("benchmark")


def test_status_and_stop_are_safe_when_session_is_absent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(factory_stack.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(factory_stack, "tmux_exists", lambda: False)

    assert factory_stack.status() == 1
    assert factory_stack.stop() == 0
    assert "STOPPED" in capsys.readouterr().out


def test_port_conflict_reports_existing_listener_without_killing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory_stack.shutil, "which", lambda _: "/usr/bin/ss")
    monkeypatch.setattr(
        factory_stack.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\nLISTEN 0 4096 *:8000 *:* users:((\"uvicorn\",pid=123))\n"),
    )

    evidence = factory_stack.port_owner(8000)

    assert "pid=123" in evidence
