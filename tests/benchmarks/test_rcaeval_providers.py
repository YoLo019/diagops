from __future__ import annotations

from backend.benchmarks.rcaeval.providers import (
    RcaEvalLogProvider,
    RcaEvalMetricProvider,
    RcaEvalRuntimeStateProvider,
    incident_event_for_case,
)
from backend.diagnosis.evidence_validation import validate_investigation_evidence
from backend.domain.evidence import RuntimeStateValue
from backend.domain.tool_queries import RuntimeStateQuery


def test_placement_only_runtime_file_does_not_claim_health_or_readiness(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "POD,NODE_NAME\ncheckout-0,node-a\n", encoding="utf-8"
    )
    provider = RcaEvalRuntimeStateProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(
        incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"),
        RuntimeStateQuery(limit=10),
    )

    assert result.evidence_items
    payload = result.evidence_items[0].payload
    assert payload["state"] == RuntimeStateValue.UNKNOWN.value
    assert payload["ready"] is None
    assert "scheduled on" in payload["reason"]


def test_runtime_state_duplicate_rows_have_distinct_evidence_ids(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "POD,NODE_NAME\ncheckout-0,node-a\ncheckout-0,node-a\n",
        encoding="utf-8",
    )
    provider = RcaEvalRuntimeStateProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(
        incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"),
        RuntimeStateQuery(limit=10),
    )

    ids = [item.id for item in result.evidence_items]
    assert len(ids) == 2
    assert len(ids) == len(set(ids))
    assert len(validate_investigation_evidence([result]).supporting_evidence) == 2


def test_log_duplicate_rows_have_distinct_evidence_ids(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "telemetry-00.csv").write_text(
        "time,container_name,message,level\n"
        "2026-01-01T00:00:00Z,checkout,request failed,error\n"
        "2026-01-01T00:00:00Z,checkout,request failed,error\n",
        encoding="utf-8",
    )
    provider = RcaEvalLogProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"))

    ids = [item.id for item in result.evidence_items]
    assert len(ids) == 2
    assert len(ids) == len(set(ids))
    assert len(validate_investigation_evidence([result]).supporting_evidence) == 2


def test_metric_duplicate_series_from_distinct_files_have_distinct_evidence_ids(tmp_path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    content = (
        "time,checkout.requests\n"
        "2026-01-01T00:00:00Z,1\n"
        "2026-01-01T00:01:00Z,1\n"
        "2026-01-01T00:02:00Z,5\n"
        "2026-01-01T00:03:00Z,5\n"
    )
    (case_dir / "telemetry-00.csv").write_text(content, encoding="utf-8")
    (case_dir / "telemetry-01.csv").write_text(content, encoding="utf-8")
    provider = RcaEvalMetricProvider(
        case_dir,
        case_id="re2-aaaaaaaaaaaaaaaa",
        runtime_manifest_hash="a" * 64,
        evidence_namespace="run-1",
    )

    result = provider.collect(incident_event_for_case(case_dir, "re2-aaaaaaaaaaaaaaaa"))

    ids = [item.id for item in result.evidence_items]
    assert len(ids) == 2
    assert len(ids) == len(set(ids))
    assert len(validate_investigation_evidence([result]).supporting_evidence) == 2
