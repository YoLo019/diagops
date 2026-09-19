import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.benchmarks.rcaeval.models import CasePrediction, LabelEntry
from backend.benchmarks.rcaeval.semantic import (
    judge_prediction,
    judgment_input,
    load_scoring_inputs,
    summarize_judgments,
)


def prediction():
    return CasePrediction(
        case_id="re2-aaaaaaaaaaaaaaaa", configuration="multi_equal_token", completed=True,
        runtime_run_id="run", execution_contract_hash="a" * 64,
        candidates=[{"affected_service": "worker", "failure_class": "disk_io_writes",
                     "failure_mechanism": "Filesystem writes with page-cache growth",
                     "evidence_ids": ["ev-1"]}], available_evidence_ids=["ev-1"],
    )


def label():
    return LabelEntry(case_id="re2-aaaaaaaaaaaaaaaa", partition="ob30", source_case_id="private",
                      system="online_boutique", service="worker", fault="disk", repetition=1)


def client(content):
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
    )
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=response),
    )))


@pytest.mark.anyio
async def test_semantic_judge_uses_explanation_without_case_metadata():
    c = client(json.dumps({"entity_match": True, "mechanism_relation": "equivalent",
                           "reason": "Disk writes are disk I/O."}))
    result = await judge_prediction(prediction(), label(), c, model="judge", extra_body={})
    assert result["mechanism_relation"] == "equivalent"
    assert result["input_tokens"] == 100
    content = c.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "Filesystem writes" in content
    assert "re2-" not in content and "private" not in content and "ob30" not in content
    assert judgment_input(prediction(), label())["expected"]["fault"] == "disk"


def test_broader_connection_fault_is_not_scored_as_packet_loss():
    report = summarize_judgments([
        {"status": "scored", "entity_match": True, "mechanism_relation": "equivalent"},
        {"status": "scored", "entity_match": True, "mechanism_relation": "broader"},
        {"status": "scored", "entity_match": False, "mechanism_relation": "equivalent"},
    ])
    assert report["semantic_top1"] == 1 / 3
    assert report["broader_count"] == 1


@pytest.mark.anyio
async def test_judge_failure_is_bounded_and_does_not_become_incorrect_prediction():
    c = client('not JSON token=private-secret')
    result = await judge_prediction(prediction(), label(), c, model="judge", extra_body={})
    assert result["status"] == "judge_failed" and result["requests"] == 2
    assert result["input_tokens"] == 200
    assert "private-secret" not in json.dumps(result)
    assert summarize_judgments([result])["semantic_top1"] is None


@pytest.mark.anyio
async def test_empty_prediction_and_invalid_references_do_not_call_judge():
    c = client("unused")
    p = prediction()
    p.available_evidence_ids = []
    assert (await judge_prediction(p, label(), c, model="judge", extra_body={}))["status"] == (
        "invalid_evidence"
    )
    p.candidates = []
    assert (await judge_prediction(p, label(), c, model="judge", extra_body={}))[
        "mechanism_relation"
    ] == "insufficient"
    c.chat.completions.create.assert_not_called()


def test_prediction_process_cannot_load_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("RCAEVAL_PREDICTION_CHILD", "1")
    with pytest.raises(ValueError, match="forbidden"):
        load_scoring_inputs(tmp_path, tmp_path / "nonexistent-labels", "multi")


def test_targeted_scoring_verifies_package_and_prediction_identity(tmp_path):
    from backend.benchmarks.rcaeval.models import LabelManifest

    p = prediction()
    (tmp_path / "config.json").write_text(json.dumps({
        "case_ids": [p.case_id], "runtime_manifest": {"manifest_hash": "a" * 64},
    }), encoding="utf-8")
    labels = tmp_path / "labels.json"
    labels.write_text(LabelManifest(runtime_manifest_hash="a" * 64, entries=[label()])
                      .model_dump_json(), encoding="utf-8")
    directory = tmp_path / p.case_id
    directory.mkdir()
    target = directory / "multi.json"
    target.write_text(p.model_dump_json(), encoding="utf-8")
    assert len(load_scoring_inputs(tmp_path, labels, "multi")[0]) == 1
    p.case_id = "re2-bbbbbbbbbbbbbbbb"
    target.write_text(p.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="identity mismatch"):
        load_scoring_inputs(tmp_path, labels, "multi")
