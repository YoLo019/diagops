import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.benchmarks.openrca.models import (
    OpenRcaBenchmarkSummary,
    OpenRcaStrategySummary,
)
from backend.config.settings import AppSettings, BenchmarkSettings, StorageSettings
from backend.main import app
from backend.services.container import reset_container


def _strategy(case_count: int) -> OpenRcaStrategySummary:
    return OpenRcaStrategySummary(
        case_count=case_count,
        completed_count=case_count,
        completion_rate=1,
        evidence_reference_validity=1,
        invalid_evidence_references=0,
        read_only_violations=0,
        average_tool_calls=1,
        duplicate_query_rejections=0,
        average_duration_ms=100,
        input_tokens=10,
        output_tokens=5,
        estimated_cost=0,
        strict_accuracy=0.5,
        partial_score=0.75,
        component_score=0.8,
        reason_score=0.7,
        time_score=0.75,
    )


def _write_run(root: Path, run_id: str, completed_at: datetime, case_count: int) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    summary = OpenRcaBenchmarkSummary(
        run_id=run_id,
        case_count=case_count,
        model="gpt-test",
        prompt_version="v8.2-test",
        git_commit="abc123",
        started_at=completed_at - timedelta(minutes=5),
        completed_at=completed_at,
        strategies={"fixed": _strategy(case_count), "adaptive": _strategy(case_count)},
    )
    (run_dir / "summary.json").write_text(
        summary.model_dump_json(indent=2), encoding="utf-8"
    )
    (run_dir / "run-manifest.json").write_text(
        json.dumps({"run_id": run_id, "completed_at": completed_at.isoformat()}),
        encoding="utf-8",
    )
    (run_dir / "fixed-predictions.csv").write_text(
        "row_id,prediction\n0,{}\n", encoding="utf-8"
    )
    return run_dir


@pytest.fixture
def benchmark_client(tmp_path: Path) -> tuple[TestClient, Path]:
    results = tmp_path / "openrca"
    reset_container(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            benchmark=BenchmarkSettings(results_path=results),
        )
    )
    older = datetime(2026, 7, 14, 12, tzinfo=UTC)
    _write_run(results, "run-z-older", older, 20)
    latest = _write_run(results, "run-a-latest", older + timedelta(hours=1), 40)
    return TestClient(app), latest


def test_latest_openrca_summary_uses_manifest_completion_time(
    benchmark_client: tuple[TestClient, Path],
):
    client, _ = benchmark_client

    response = client.get("/benchmarks/openrca/latest")

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-a-latest"
    assert response.json()["case_count"] == 40
    assert set(response.json()["strategies"]) == {"fixed", "adaptive"}


def test_artifact_download_is_allowlisted(
    benchmark_client: tuple[TestClient, Path],
):
    client, _ = benchmark_client

    allowed = client.get("/benchmarks/openrca/latest/summary.json")
    denied = client.get("/benchmarks/openrca/latest/../../diagops.db")

    assert allowed.status_code == 200
    assert denied.status_code in {404, 422}
    assert client.get("/benchmarks/openrca/latest/compatible-report.csv").status_code == 404


def test_missing_allowlisted_artifact_returns_404(
    benchmark_client: tuple[TestClient, Path],
):
    client, _ = benchmark_client

    assert client.get("/benchmarks/openrca/latest/official-report.csv").status_code == 404


def test_latest_openrca_returns_404_without_a_valid_frozen_run(tmp_path: Path):
    results = tmp_path / "openrca"
    invalid = results / "run-invalid"
    invalid.mkdir(parents=True)
    (invalid / "summary.json").write_text("{}", encoding="utf-8")
    (invalid / "run-manifest.json").write_text(
        json.dumps({"completed_at": "2026-07-14T12:00:00+00:00"}),
        encoding="utf-8",
    )
    reset_container(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            benchmark=BenchmarkSettings(results_path=results),
        )
    )

    response = TestClient(app).get("/benchmarks/openrca/latest")

    assert response.status_code == 404
