from __future__ import annotations

from backend.benchmarks.rcaeval.providers import (
    RcaEvalRuntimeStateProvider,
    incident_event_for_case,
)
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
