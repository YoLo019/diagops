import csv
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.benchmarks.openrca import runner as openrca_runner
from backend.benchmarks.openrca.models import (
    OpenRcaPartition,
    OpenRcaRuntimeCase,
    OpenRcaRuntimeIndex,
)
from backend.benchmarks.openrca.runner import (
    BenchmarkCaseOutcome,
    run_benchmark_pair,
)
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.multi_agent import InvestigationStrategy

TZ = timezone(timedelta(hours=8))


class FakeRuntime:
    def run_case(
        self, case: OpenRcaRuntimeCase, strategy: InvestigationStrategy
    ) -> BenchmarkCaseOutcome:
        failed = (
            strategy == InvestigationStrategy.ADAPTIVE
            and case.task_index == "task_2"
        )
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
        )


def safe_index(tmp_path: Path) -> Path:
    cases = [
        OpenRcaRuntimeCase(
            case_id=f"{partition.value}:{index - 1}",
            partition=partition,
            row_id=str(index - 1),
            task_index=f"task_{index}",
            instruction="fabricated",
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


@pytest.mark.parametrize("rate", [-1.0, math.nan, math.inf])
def test_runner_rejects_invalid_cost_rates_before_creating_run(
    tmp_path: Path, rate: float
):
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
        path.read_text(encoding="utf-8")
        for path in result.output_dir.iterdir()
        if path.is_file()
    ).casefold()
    assert "scoring_points" not in serialized
    assert "record.csv" not in serialized


def test_fixture_cli_prepare_run_evaluate_smoke(tmp_path: Path):
    fixture_root = Path(__file__).parents[1] / "fixtures" / "openrca"
    prepared = tmp_path / "prepared"
    runs = tmp_path / "runs"
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)

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
        "--model",
        "test-model",
    )
    run_id = run.stdout.strip().splitlines()[-1]
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
    manifest = json.loads((runs / run_id / "run-manifest.json").read_text())
    assert set(manifest["artifact_checksums"]) >= {
        "fixed-predictions.csv",
        "adaptive-predictions.csv",
        "compatible-report.csv",
        "summary.json",
    }
    assert "run-manifest.json" not in manifest["artifact_checksums"]
