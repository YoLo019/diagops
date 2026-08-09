"""离线九工具验收的端到端契约（spec 7.7/R22）。"""

import json

from backend.services import offline_tool_acceptance
from backend.services.offline_tool_acceptance import (
    EXPECTED_AGENT_TOOLS,
    main,
    run_acceptance,
)


def test_acceptance_passes_and_covers_required_conditions(tmp_path):
    report = run_acceptance(tmp_path / "gate")

    assert report["summary"]["passed"] is True
    conditions = {
        (row["tool"], row["condition"]) for row in report["rows"]
    }
    for tool in EXPECTED_AGENT_TOOLS:
        assert (tool, "success") in conditions
        assert (tool, "empty") in conditions
        assert (tool, "provenance") in conditions
        assert (tool, "scope") in conditions
        assert (tool, "idempotency") in conditions
    for tool in EXPECTED_AGENT_TOOLS:
        if tool == "lookup_memory":
            continue
        for condition in ("malformed", "missing", "absent_source", "timeout"):
            assert (tool, condition) in conditions
    assert ("read_logs", "redaction") in conditions


def test_every_tool_has_non_empty_success(tmp_path):
    report = run_acceptance(tmp_path / "gate")

    success_rows = {
        row["tool"]: row
        for row in report["rows"]
        if row["condition"] == "success"
    }
    assert set(success_rows) == set(EXPECTED_AGENT_TOOLS)
    assert all(row["passed"] for row in success_rows.values())


def test_matrix_artifact_is_written_with_stable_hash(tmp_path):
    report = run_acceptance(tmp_path / "gate")
    path = tmp_path / "gate" / "matrix.json"

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["artifact_hash"] == report["artifact_hash"]
    assert persisted["agent_manifest"] == list(EXPECTED_AGENT_TOOLS)
    assert persisted["source_class"] == "synthetic_fixture"


def test_main_returns_non_zero_when_a_row_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        offline_tool_acceptance,
        "_idempotency_row",
        lambda registry, tool_name, event: [
            offline_tool_acceptance._row(
                tool_name, "idempotency", passed=False, detail="forced failure"
            )
        ],
    )

    exit_code = main(["--output-dir", str(tmp_path / "gate")])

    assert exit_code == 1
