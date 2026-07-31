import csv
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.benchmarks.openrca import __main__ as openrca_cli
from backend.benchmarks.openrca import runner as openrca_runner
from backend.benchmarks.openrca.evaluator import evaluate_persisted_prediction
from backend.benchmarks.openrca.models import (
    OpenRcaPartition,
    OpenRcaRuntimeCase,
    OpenRcaRuntimeIndex,
)
from backend.benchmarks.openrca.projection import (
    ProjectionAudit,
    ProjectionResult,
    scored_fields,
)
from backend.benchmarks.openrca.runner import (
    BenchmarkCaseOutcome,
    OpenRcaDiagnosisRunner,
    run_benchmark_pair,
)
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.multi_agent import InvestigationStrategy, ModelProvider
from backend.domain.runtime import RuntimeEventType, RuntimeRunStatus
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.replay import ReplayDependencies, ReplayService
from backend.runtime.sqlite_store import SQLiteRuntimeStore

TZ = timezone(timedelta(hours=8))


def fake_projection_audit(case: OpenRcaRuntimeCase) -> ProjectionAudit:
    return ProjectionAudit(
        rule_version="v2",
        scored_fields=scored_fields(case.task_index),
        selected_evidence_ids=((f"ev-{case.case_id}",),),
    )


class FakeRuntime:
    def run_case(
        self, case: OpenRcaRuntimeCase, strategy: InvestigationStrategy
    ) -> BenchmarkCaseOutcome:
        failed = strategy == InvestigationStrategy.ADAPTIVE and case.task_index == "task_2"
        return BenchmarkCaseOutcome(
            root_causes=(
                []
                if failed
                else [
                    RootCauseAttribution(
                        root_cause_occurred_at=case.start_time,
                        root_cause_component=f"{case.partition.value}-service",
                        root_cause_reason="fabricated failure",
                        supporting_evidence_ids=[f"ev-{case.case_id}"],
                    )
                ]
            ),
            completed=not failed,
            failure_category="timeout" if failed else None,
            evidence_reference_count=0 if failed else 1,
            invalid_evidence_reference_count=0,
            tool_call_count=2 if strategy == InvestigationStrategy.ADAPTIVE else 0,
            duplicate_query_rejections=0,
            duration_ms=100,
            input_tokens=100,
            output_tokens=20,
            read_only_violations=0,
            runtime_run_id=f"runtime-{strategy.value}-{case.case_id}",
            projection_audit=fake_projection_audit(case),
        )


def safe_index(tmp_path: Path, expected_count: int | None = None) -> Path:
    cases = [
        OpenRcaRuntimeCase(
            case_id=f"{partition.value}:{index - 1}",
            partition=partition,
            row_id=str(index - 1),
            task_index=f"task_{index}",
            instruction="fabricated",
            expected_root_cause_count=expected_count,
            start_time=datetime(2026, 7, 14, 12, tzinfo=TZ),
            end_time=datetime(2026, 7, 14, 12, 10, tzinfo=TZ),
            telemetry_dir=f"{partition.value}/telemetry/2026-07-14",
        )
        for partition in OpenRcaPartition
        for index in (1, 2)
    ]
    path = tmp_path / "runtime-cases.json"
    path.write_text(
        OpenRcaRuntimeIndex(
            case_manifest_hash="manifest-sha256",
            cases=cases,
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    return path


def test_runtime_case_requires_official_row_identity():
    with pytest.raises(ValidationError):
        OpenRcaRuntimeCase(
            case_id="Bank:0",
            partition=OpenRcaPartition.BANK,
            instruction="fabricated",
            start_time=datetime(2026, 7, 14, 12, tzinfo=TZ),
            end_time=datetime(2026, 7, 14, 12, 10, tzinfo=TZ),
            telemetry_dir="Bank/telemetry/2026-07-14",
        )


def test_runtime_case_keeps_legacy_index_readable_without_expected_count(
    tmp_path: Path,
):
    case = OpenRcaRuntimeIndex.model_validate_json(
        safe_index(tmp_path).read_text(encoding="utf-8")
    ).cases[0]

    assert case.expected_root_cause_count is None


def test_official_prediction_keeps_only_expected_top_n():
    causes = [
        RootCauseAttribution(
            root_cause_occurred_at=datetime(2026, 7, 14, 12, index, tzinfo=TZ),
            root_cause_component=f"service-{index}",
            root_cause_reason="failure",
            supporting_evidence_ids=[f"ev-{index}"],
        )
        for index in (2, 0, 1)
    ]

    prediction = json.loads(openrca_runner._official_prediction(causes, 2))

    assert list(prediction) == ["1", "2"]
    assert [item["root cause component"] for item in prediction.values()] == [
        "service-2",
        "service-0",
    ]


def test_cli_disables_agents_tracing(monkeypatch):
    calls = []
    monkeypatch.setattr(openrca_cli, "set_tracing_disabled", calls.append, raising=False)
    monkeypatch.setattr(sys, "argv", ["openrca", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        openrca_cli.main()

    assert exc_info.value.code == 0
    assert calls == [True]


def test_gate_cli_exits_nonzero_when_frozen_artifacts_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["openrca", "gate", "--run-dir", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        openrca_cli.main()

    assert exc_info.value.code == 2


def test_targeted_gate_cli_routes_to_targeted_gate(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        openrca_cli,
        "targeted_gate_run",
        lambda path: calls.append(path) or ["blocked"],
        raising=False,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["openrca", "targeted-gate", "--run-dir", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        openrca_cli.main()

    assert exc_info.value.code == 2
    assert calls == [tmp_path]


def test_benchmark_event_window_matches_runtime_case_window(tmp_path: Path):
    case = OpenRcaRuntimeIndex.model_validate_json(
        safe_index(tmp_path).read_text(encoding="utf-8")
    ).cases[0]
    event_factory = getattr(openrca_runner, "_benchmark_event", None)

    assert event_factory is not None
    event = event_factory(case)
    window = timedelta(minutes=event.time_window_minutes)
    assert event.started_at - window == case.start_time
    assert event.started_at + window == case.end_time


def test_runner_writes_fixed_and_adaptive_artifacts(tmp_path: Path):
    result = run_benchmark_pair(
        FakeRuntime(),
        safe_index(tmp_path),
        tmp_path / "runs",
        model="test-model",
        input_cost_per_million=1.5,
        output_cost_per_million=6.0,
    )

    assert (result.output_dir / "fixed-predictions.csv").exists()
    assert (result.output_dir / "adaptive-predictions.csv").exists()
    assert (result.output_dir / "adaptive-Bank.csv").exists()
    manifest = json.loads((result.output_dir / "run-manifest.json").read_text())
    summary = json.loads((result.output_dir / "summary.json").read_text())
    assert manifest["model"] == "test-model"
    assert manifest["case_manifest_hash"] == "manifest-sha256"
    assert manifest["git_commit"]
    assert manifest["input_cost_per_million"] == 1.5
    assert summary["case_count"] == 8
    assert set(summary["strategies"]) == {"fixed", "adaptive"}
    assert summary["strategies"]["adaptive"]["completion_rate"] == 0.5
    assert summary["strategies"]["adaptive"]["failed_cases"]
    assert (tmp_path / "runs" / "latest-run.txt").read_text().strip() == result.run_id

    with (result.output_dir / "adaptive-predictions.csv").open(
        encoding="utf-8", newline=""
    ) as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 8
    assert json.loads(rows[1]["prediction"]) == {}
    assert rows[0]["row_id"] == "0"
    assert rows[0]["task_index"] == "task_1"
    metadata = json.loads(rows[0]["metadata"])
    assert metadata["runtime_run_id"].startswith("runtime-adaptive-")
    assert metadata["projection"]["rule_version"] == "v2"
    assert metadata["projection"]["projection_error"] is False
    assert summary["strategies"]["fixed"]["projection_errors"] == 0
    assert summary["strategies"]["fixed"]["projection_fallbacks"] == 0


def run_one_real_fixture_case(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    runtime_store = SQLiteRuntimeStore(engine, repository)
    case = OpenRcaRuntimeIndex.model_validate_json(
        safe_index(tmp_path, 1).read_text(encoding="utf-8")
    ).cases[0]
    runner = OpenRcaDiagnosisRunner(
        Path(__file__).parents[1] / "fixtures" / "openrca",
        None,
        repository=repository,
        runtime_store=runtime_store,
        deterministic=True,
    )
    outcome = runner.run_case(case, InvestigationStrategy.FIXED)
    return outcome, repository, runtime_store


def test_real_runner_projects_output_without_mutating_persisted_review(
    tmp_path: Path,
    monkeypatch,
):
    projected_component = "projected-only"
    original_projector = openrca_runner.project_root_causes

    def project(**arguments):
        result = original_projector(**arguments)
        causes = tuple(
            item.model_copy(update={"root_cause_component": projected_component})
            for item in result.causes
        )
        return ProjectionResult(causes=causes, audit=result.audit)

    monkeypatch.setattr(openrca_runner, "project_root_causes", project)
    outcome, repository, runtime_store = run_one_real_fixture_case(tmp_path)

    assert outcome.runtime_run_id is not None
    runtime_run = runtime_store.get_run(outcome.runtime_run_id)
    review = repository.get_coordination_review(runtime_run.investigation_id)
    assert outcome.root_causes[0].root_cause_component == projected_component
    assert review is not None
    assert all(
        item.root_cause_component != projected_component for item in review.root_causes
    )


def test_real_runner_marks_projection_failure_without_exception_detail(
    tmp_path: Path,
    monkeypatch,
):
    def fail_projection(**_arguments):
        raise ValueError("secret telemetry")

    monkeypatch.setattr(openrca_runner, "project_root_causes", fail_projection)

    outcome, _repository, _runtime_store = run_one_real_fixture_case(tmp_path)

    assert outcome.completed is False
    assert outcome.failure_category == "projection_error"
    assert outcome.projection_audit is not None
    assert outcome.projection_audit.projection_error is True
    assert "secret telemetry" not in str(outcome)


def test_projector_reuses_persisted_hypotheses_without_rerunning_analyzer(
    tmp_path: Path,
    monkeypatch,
):
    calls = 0
    original = openrca_runner.RcaAnalyzer.analyze

    def count(self, event, evidence):
        nonlocal calls
        calls += 1
        return original(self, event, evidence)

    monkeypatch.setattr(openrca_runner.RcaAnalyzer, "analyze", count)

    outcome, _repository, _runtime_store = run_one_real_fixture_case(tmp_path)

    assert outcome.completed is True
    assert calls == 1


def test_agent_shadow_always_writes_an_independent_run_directory(tmp_path: Path):
    index = safe_index(tmp_path, 1)
    output = tmp_path / "runs"

    deterministic = run_benchmark_pair(
        FakeRuntime(),
        index,
        output,
        model="deterministic",
        strategies=(InvestigationStrategy.FIXED,),
        mode="deterministic",
    )
    shadow = run_benchmark_pair(
        FakeRuntime(),
        index,
        output,
        model="test-model",
        strategies=(InvestigationStrategy.FIXED,),
        mode="agent-shadow",
    )

    assert deterministic.output_dir != shadow.output_dir
    assert (
        json.loads((shadow.output_dir / "run-manifest.json").read_text(encoding="utf-8"))["mode"]
        == "agent-shadow"
    )


def test_deterministic_run_rejects_legacy_index_without_expected_count(
    tmp_path: Path,
):
    with pytest.raises(ValueError, match="expected root cause count"):
        run_benchmark_pair(
            FakeRuntime(),
            safe_index(tmp_path),
            tmp_path / "runs",
            model="deterministic",
            strategies=(InvestigationStrategy.FIXED,),
            mode="deterministic",
        )


def test_runner_optionally_configures_real_case_runner_without_breaking_old_fakes(
    tmp_path: Path,
) -> None:
    class ConfigurableFake(FakeRuntime):
        configured = None

        def configure_runtime(self, *, provider, model, prompt_version, timeout_seconds) -> None:
            self.configured = (provider, model, prompt_version, timeout_seconds)

    runner = ConfigurableFake()

    run_benchmark_pair(
        runner,
        safe_index(tmp_path),
        tmp_path / "runs",
        provider=ModelProvider.DEEPSEEK,
        model="deepseek-chat",
        prompt_version="v9",
        timeout_seconds=180,
    )

    assert runner.configured == (
        ModelProvider.DEEPSEEK,
        "deepseek-chat",
        "v9",
        180,
    )
    run_id = (tmp_path / "runs" / "latest-run.txt").read_text().strip()
    manifest = json.loads((tmp_path / "runs" / run_id / "run-manifest.json").read_text())
    assert manifest["strategy_config"]["timeout_seconds"] == 180


def test_real_case_runner_preserves_runtime_audit_on_execution_failure(
    tmp_path: Path, monkeypatch
) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    runtime_store = SQLiteRuntimeStore(engine, repository)
    case = OpenRcaRuntimeIndex.model_validate_json(
        safe_index(tmp_path).read_text(encoding="utf-8")
    ).cases[0]

    async def fail_execution(*_args, **_kwargs):
        raise RuntimeError("injected execution failure")

    monkeypatch.setattr(RuntimeCoordinator, "execute", fail_execution)
    monkeypatch.setattr(
        runtime_store,
        "list_events",
        lambda _run_id: [
            SimpleNamespace(
                event_type=RuntimeEventType.MODEL_COMPLETED,
                safe_payload={"input_tokens": 7, "output_tokens": 2},
            ),
            SimpleNamespace(
                event_type=RuntimeEventType.MODEL_COMPLETED,
                safe_payload={"input_tokens": 3, "output_tokens": 1},
            ),
            SimpleNamespace(
                event_type=RuntimeEventType.PHASE_COMPLETED,
                safe_payload={"input_tokens": 999, "output_tokens": 999},
            ),
        ],
    )
    outcome = OpenRcaDiagnosisRunner(
        Path(__file__).parents[1] / "fixtures" / "openrca",
        "test-model",
        repository=repository,
        runtime_store=runtime_store,
        prompt_version="v9",
    ).run_case(case, InvestigationStrategy.FIXED)

    assert outcome.failure_category == "RuntimeError: benchmark case failed"
    assert outcome.runtime_run_id is not None
    assert (outcome.input_tokens, outcome.output_tokens) == (10, 3)
    assert runtime_store.get_run(outcome.runtime_run_id).id == outcome.runtime_run_id


@pytest.mark.parametrize("rate", [-1.0, math.nan, math.inf])
def test_runner_rejects_invalid_cost_rates_before_creating_run(tmp_path: Path, rate: float):
    output = tmp_path / "runs"

    with pytest.raises(ValueError, match="cost rates"):
        run_benchmark_pair(
            FakeRuntime(),
            safe_index(tmp_path),
            output,
            model="test-model",
            input_cost_per_million=rate,
        )

    assert not output.exists()


def test_runner_artifacts_do_not_contain_ground_truth_fields(tmp_path: Path):
    result = run_benchmark_pair(
        FakeRuntime(), safe_index(tmp_path), tmp_path / "runs", model="test-model"
    )

    serialized = "\n".join(
        path.read_text(encoding="utf-8") for path in result.output_dir.iterdir() if path.is_file()
    ).casefold()
    assert "scoring_points" not in serialized
    assert "record.csv" not in serialized


def test_fixture_cli_prepare_run_evaluate_smoke(tmp_path: Path):
    fixture_root = Path(__file__).parents[1] / "fixtures" / "openrca"
    prepared = tmp_path / "prepared"
    runs = tmp_path / "runs"
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    runtime_database = tmp_path / "openrca-runtime.db"
    environment["DIAGOPS_DATABASE_URL"] = f"sqlite:///{runtime_database}"

    def command(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "backend.benchmarks.openrca", *arguments],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

    command(
        "prepare",
        "--dataset-root",
        str(fixture_root),
        "--output",
        str(prepared),
        "--per-partition",
        "1",
    )
    run = command(
        "run",
        "--dataset-root",
        str(fixture_root),
        "--safe-index",
        str(prepared / "runtime-cases.json"),
        "--output",
        str(runs),
        "--mode",
        "deterministic",
        "--strategy",
        "fixed",
    )
    run_id = run.stdout.strip().splitlines()[-1]
    with (runs / run_id / "fixed-predictions.csv").open(encoding="utf-8", newline="") as file:
        runtime_run_ids = {
            json.loads(row["metadata"])["runtime_run_id"] for row in csv.DictReader(file)
        }
    engine = create_db_engine(f"sqlite:///{runtime_database}")
    runtime_store = SQLiteRuntimeStore(
        engine,
        SQLiteInvestigationRepository(engine),
    )
    assert runtime_run_ids
    runtime_runs = [runtime_store.get_run(run_id) for run_id in runtime_run_ids]
    assert all(item.status == RuntimeRunStatus.COMPLETED for item in runtime_runs)
    assert all(item.model_provider is None for item in runtime_runs)
    assert all(item.model_name == "deterministic" for item in runtime_runs)
    assert all(item.prompt_version == "v10-shared-core" for item in runtime_runs)
    for item in runtime_runs:
        assert runtime_store.list_attempts(item.id)
        assert runtime_store.list_checkpoints(item.id)
        assert runtime_store.get_benchmark_replay_locator(item.id) is None
        assert not any(
            event.event_type == RuntimeEventType.MODEL_COMPLETED
            for event in runtime_store.list_events(item.id)
        )
        assert (
            ReplayService(
                ReplayDependencies(
                    store=runtime_store,
                    benchmark_evaluator=partial(evaluate_persisted_prediction, runtime_store),
                )
            )
            .replay(item.id)
            .valid
        )
    engine.dispose()
    command(
        "evaluate",
        "--query-root",
        str(fixture_root),
        "--run-dir",
        str(runs / run_id),
        "--official-query-output",
        str(tmp_path / "official-queries"),
    )

    assert (runs / run_id / "compatible-report.csv").exists()
    assert (tmp_path / "official-queries" / "Bank-query.csv").exists()
    assert json.loads((runs / run_id / "summary.json").read_text())["case_count"] == 4
    summary = json.loads((runs / run_id / "summary.json").read_text())
    assert summary["model"] == "deterministic"
    assert summary["prompt_version"] == "v10-shared-core"
    assert summary["strategies"]["fixed"]["input_tokens"] == 0
    assert summary["strategies"]["fixed"]["output_tokens"] == 0
    assert summary["strategies"]["fixed"]["estimated_cost"] == 0
    manifest = json.loads((runs / run_id / "run-manifest.json").read_text())
    assert manifest["mode"] == "deterministic"
    assert manifest["strategies"] == ["fixed"]
    assert set(manifest["artifact_checksums"]) >= {
        "fixed-predictions.csv",
        "compatible-report.csv",
        "summary.json",
    }
    assert "run-manifest.json" not in manifest["artifact_checksums"]
