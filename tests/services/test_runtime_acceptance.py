import asyncio
import inspect
import json
import logging
import subprocess
from pathlib import Path

import pytest

from backend.runtime.faults import APPROVED_FAULT_POINTS
from backend.services import runtime_acceptance
from backend.services.runtime_acceptance import (
    REQUIRED_SCENARIOS,
    _candidate_state,
    _measure_disabled_mode,
    _parse_args,
    run_acceptance,
    validate_acceptance_artifact,
)


def _valid_artifact(observations: dict | None = None) -> dict:
    scenario_results = [
        {"name": name, "status": "passed", "observations": {}}
        for name in sorted(REQUIRED_SCENARIOS)
    ]
    scenario_results[0]["observations"] = observations or {}
    return {
        "scenario_results": scenario_results,
        "privacy_scan": {
            "status": "passed",
            "prohibited_markers_found": [],
        },
    }


def test_key_free_runtime_acceptance_runs_real_scenarios_and_writes_safe_artifact(
    tmp_path: Path,
) -> None:
    artifact_path = asyncio.run(
        run_acceptance(
            output_root=tmp_path,
            git_commit="test-commit",
            scenario_timeout_seconds=5,
        )
    )

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    serialized = json.dumps(artifact, ensure_ascii=False)
    assert artifact_path.name == "result.json"
    assert artifact_path.parent.parent == tmp_path
    assert artifact["git_commit"] == "test-commit"
    # git_dirty 的 True/False 语义由 test_candidate_diff_hash_is_stable... 用临时
    # 仓库确定性覆盖；此处只断言字段存在且为布尔，避免依赖本仓库工作区状态。
    assert isinstance(artifact["git_dirty"], bool)
    assert len(artifact["candidate_diff_sha256"]) == 64
    assert artifact["config"]["seed"] == 42
    assert REQUIRED_SCENARIOS == APPROVED_FAULT_POINTS
    assert {
        item["name"] for item in artifact["scenario_results"]
    } == REQUIRED_SCENARIOS
    assert {item["status"] for item in artifact["scenario_results"]} == {
        "passed"
    }
    assert artifact["latency"]["runtime_enabled"]["p95_wall_ms"] >= 0
    assert artifact["latency"]["runtime_enabled"]["storage"] == "sqlite"
    for mode in ("v8_2_sync", "runtime_disabled", "runtime_enabled"):
        measurement = artifact["latency"][mode]
        assert measurement["warmup_iterations"] >= 1
        assert measurement["sample_count"] >= 9
        assert len(measurement["raw_wall_ms"]) == measurement["sample_count"]
    assert artifact["database_growth"]["sqlite_bytes_per_run"] >= 0
    assert artifact["database_growth"]["event_count_per_run"] > 0
    assert artifact["database_growth"]["checkpoint_count_per_run"] > 0
    assert isinstance(artifact["otel_overhead"]["percent"], float)
    assert artifact["recovery_time"]["wall_ms"] >= 0
    assert artifact["privacy_scan"] == {
        "status": "passed",
        "prohibited_markers_found": [],
    }
    scenarios = {item["name"]: item["observations"] for item in artifact["scenario_results"]}
    assert scenarios["checkpoint_tamper"]["tamper_variant_count"] == 7
    assert scenarios["sse_reconnect"]["serialized_sse_frame_count"] >= 3
    assert scenarios["replay_external_call"]["corruption_codes"] == {
        "checkpoint_tamper": "checkpoint_digest_mismatch",
        "gap": "event_sequence_gap",
        "illegal_transition": "illegal_phase_transition",
        "invalid_reference": "bad_reference",
    }
    assert "raw_provider_payload" not in serialized
    assert "credential_injection" not in serialized
    validate_acceptance_artifact(artifact)


def test_concurrency_and_cancel_acceptance_use_production_phase_executor_path() -> None:
    source = "\n".join(
        inspect.getsource(function)
        for function in (
            runtime_acceptance._parallel_session_failure,
            runtime_acceptance._cancel_parallel_specialists,
        )
    )

    assert "DiagnosisPhaseExecutor" in source
    assert "run_parallel_steps" not in source


def test_acceptance_validation_rejects_missing_scenarios_and_sensitive_payloads() -> None:
    with pytest.raises(RuntimeError, match="missing required acceptance scenarios"):
        validate_acceptance_artifact(
            {
                "scenario_results": [],
                "privacy_scan": {
                    "status": "passed",
                    "prohibited_markers_found": [],
                },
            }
        )

    scenario_results = [
        {"name": name, "status": "passed", "observations": {}}
        for name in sorted(REQUIRED_SCENARIOS)
    ]
    scenario_results[0]["observations"] = {
        "provider_payload": "Authorization: Bearer acceptance-secret"
    }
    with pytest.raises(RuntimeError, match="prohibited runtime acceptance content"):
        validate_acceptance_artifact(
            {
                "scenario_results": scenario_results,
                "privacy_scan": {
                    "status": "passed",
                    "prohibited_markers_found": [],
                },
            }
        )

    scenario_results[0]["observations"] = {
        "safe_code": "raw-provider-payload-marker"
    }
    with pytest.raises(RuntimeError, match="prohibited runtime acceptance content"):
        validate_acceptance_artifact(
            {
                "scenario_results": scenario_results,
                "privacy_scan": {
                    "status": "passed",
                    "prohibited_markers_found": [],
                },
            }
        )


@pytest.mark.parametrize(
    "observations",
    [
        {"endpoint": "https://operator:ordinary-secret@example.invalid/v1"},
        {"nested": [{"OPENAI_API_KEY": "ordinary-secret"}]},
        {"user_prompt_text": "diagnose this incident"},
        {"nested": [{"tool_result_text": "raw tool output"}]},
        {"provider_response": {"status": "ok"}},
        {"model_output": "raw model response"},
        {"password": "ordinary-secret"},
        {"raw_output": "raw tool response"},
        {"request_payload": {"status": "ok"}},
        {"database_url": "postgresql://diagops:supersecret@host/db"},
        {"retrieved_evidence_text": "verbatim stack trace"},
        {"log_body": "verbatim stack trace"},
    ],
)
def test_acceptance_validation_rejects_nested_prohibited_semantics(
    observations: dict,
) -> None:
    with pytest.raises(RuntimeError, match="prohibited runtime acceptance content"):
        validate_acceptance_artifact(_valid_artifact(observations))


def test_replay_acceptance_privacy_surfaces_include_frozen_environment() -> None:
    observations = asyncio.run(runtime_acceptance._replay_external_call())

    assert any(
        isinstance((surface := json.loads(item)), dict)
        and surface.get("environment") == "prod"
        for item in observations["_privacy_surfaces"]
    )


def test_failed_privacy_validation_publishes_no_artifact_or_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def safe_scenario() -> dict[str, object]:
        return {"status": "safe"}

    async def leaking_scenario() -> dict[str, object]:
        return {"message": "Authorization: Bearer acceptance-secret"}

    scenarios = {name: safe_scenario for name in REQUIRED_SCENARIOS}
    scenarios[sorted(REQUIRED_SCENARIOS)[0]] = leaking_scenario

    async def performance():
        return (
            {
                "v8_2_sync": {"p50_wall_ms": 1.0, "p95_wall_ms": 1.0},
                "runtime_disabled": {"p50_wall_ms": 1.0, "p95_wall_ms": 1.0},
                "runtime_enabled": {"p50_wall_ms": 1.0, "p95_wall_ms": 1.0},
                "comparison_percentages": {},
            },
            {
                "sqlite_bytes_per_run": 1,
                "event_count_per_run": 1,
                "checkpoint_count_per_run": 1,
                "run_count": 1,
            },
            {"disabled_wall_ms": 1.0, "enabled_wall_ms": 1.0, "percent": 0.0},
        )

    monkeypatch.setattr(runtime_acceptance, "_SCENARIOS", scenarios)
    monkeypatch.setattr(runtime_acceptance, "_measure_performance", performance)

    with pytest.raises(RuntimeError, match="prohibited runtime acceptance content"):
        asyncio.run(
            run_acceptance(
                output_root=tmp_path,
                git_commit="test-commit",
                scenario_timeout_seconds=1,
            )
        )

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []

    async def logging_scenario() -> dict[str, object]:
        logging.getLogger("backend.runtime.acceptance").error(
            "Authorization: Bearer acceptance-secret"
        )
        return {"status": "safe"}

    scenarios[sorted(REQUIRED_SCENARIOS)[0]] = logging_scenario
    monkeypatch.setattr(runtime_acceptance, "_SCENARIOS", scenarios)

    with pytest.raises(RuntimeError, match="prohibited runtime acceptance content"):
        asyncio.run(
            run_acceptance(
                output_root=tmp_path,
                git_commit="test-commit",
                scenario_timeout_seconds=1,
            )
        )

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_candidate_diff_hash_is_stable_and_includes_untracked_content(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "acceptance@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Runtime Acceptance"],
        cwd=tmp_path,
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)

    clean = _candidate_state(tmp_path)
    assert clean == _candidate_state(tmp_path)
    assert clean[0] is False

    candidate = tmp_path / "candidate.txt"
    candidate.write_text("first\n", encoding="utf-8")
    first = _candidate_state(tmp_path)
    assert first == _candidate_state(tmp_path)
    assert first[0] is True
    assert first[1] != clean[1]

    candidate.write_text("second\n", encoding="utf-8")
    assert _candidate_state(tmp_path)[1] != first[1]


def test_cli_rejects_output_root_override(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--output-root", str(tmp_path)])


def test_runtime_disabled_measurement_uses_container_compatibility_branch() -> None:
    from backend.config.settings import AppSettings, RuntimeSettings, StorageSettings
    from backend.services.container import AppContainer

    settings = AppSettings(
        storage=StorageSettings(url="memory://"),
        runtime=RuntimeSettings(enabled=False),
    )
    container = AppContainer(settings)
    original = container.orchestrator.run
    calls = 0

    def run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    container.orchestrator.run = run
    values = asyncio.run(
        _measure_disabled_mode(
            ("traffic_spike",),
            iterations=1,
            warmup_iterations=1,
            container_factory=lambda: container,
        )
    )

    assert len(values) == 1
    assert calls == 2
