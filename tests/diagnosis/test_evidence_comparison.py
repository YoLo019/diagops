"""同窗数值比较保留范围、缺失和冲突，不替模型判断根因。"""

import copy
import json

from backend.diagnosis.evidence_comparison import comparison_evidence


def metric(identity="ev-1", **overrides):
    return {
        "id": identity, "kind": "metric_trend", "status": "partial",
        "scope_entity_ids": ["worker"], "metric_name": "worker_cpu", "unit": "seconds",
        "window_start": "2026-09-01T00:00:00Z", "split_at": "2026-09-01T00:01:00Z",
        "window_end": "2026-09-01T00:02:00Z", "baseline_mean": 1, "observation_mean": 9,
        "sampling_interval_seconds": 2, "sample_count": 61,
        **overrides,
    }


def test_trace_comparison_shares_only_identical_explanations_and_keeps_timing():
    evidence = [{"id": f"trace-{i}", "provider": "trace", "rpc_pair": {
        "client_duration_ms": 400 + i, "server_duration_ms": 0.02,
        "semantics": "Compare caller resource evidence.",
    }, "child_timing": {"semantics": f"Different observation boundary {i}"}}
        for i in range(3)]
    original = copy.deepcopy(evidence)
    result = comparison_evidence(evidence)
    assert result["trace_semantics"] == {"rpc_pair": "Compare caller resource evidence."}
    assert [item["rpc_pair"]["client_duration_ms"] for item in result["evidence"]] == (
        [400, 401, 402]
    )
    assert all("semantics" not in item["rpc_pair"] for item in result["evidence"])
    assert [item["child_timing"] for item in result["evidence"]] == (
        [item["child_timing"] for item in evidence]
    )
    assert evidence == original


def test_table_keeps_normal_controls_deltas_and_citation_identity_without_mutation():
    evidence = [metric(related_observations=[
        {"metric": "worker_cpu_system", "baseline_mean": 0, "observation_mean": 7},
        {"metric": "worker_cpu_user", "baseline_mean": 1, "observation_mean": 1},
    ]), metric("ev-2"), metric("ev-conflict", observation_mean=12)]
    original = copy.deepcopy(evidence)
    result = comparison_evidence(evidence)
    table = result["metric_comparisons"]
    assert table["groups"][0]["rows"] == [
        ["worker_cpu", 1, 9, 8, ["ev-1", "ev-2"], None],
        ["worker_cpu_system", 0, 7, 7, ["ev-1"], None],
        ["worker_cpu_user", 1, 1, 0, ["ev-1"], None],
        ["worker_cpu", 1, 12, 11, ["ev-conflict"], None],
    ]
    assert result["evidence"][0]["status"] == "partial"
    assert result["evidence"][0]["sampling_interval_seconds"] == 2
    assert result["evidence"][0]["sample_count"] == 61
    assert "baseline_mean" not in result["evidence"][0]
    assert evidence == original


def test_different_scopes_windows_and_units_are_not_silently_merged():
    evidence = [metric(), metric("other", scope_entity_ids=["other"]),
                metric("narrow", window_end="2026-09-01T00:01:30Z"),
                metric("unknown", window_start=None), metric("unit", unit="milliseconds"),
                metric("invalid", split_at="2026-09-01T00:03:00Z")]
    result = comparison_evidence(evidence)
    groups = result["metric_comparisons"]["groups"]
    assert len(groups) == 3
    assert groups[0]["rows"] == [["worker_cpu", 1, 9, 8, ["ev-1"], None],
                                  ["worker_cpu", 1, 9, 8, ["unit"], None]]
    assert result["evidence"][3]["baseline_mean"] == 1
    assert result["evidence"][5]["baseline_mean"] == 1


def test_comparison_limits_preserve_overflow_in_evidence():
    evidence = [metric(f"ev-{i}", metric_name=f"metric-{i}") for i in range(35)]
    evidence += [metric(f"scope-{i}", scope_entity_ids=[f"entity-{i}"]) for i in range(8)]
    result = comparison_evidence(evidence)
    groups = result["metric_comparisons"]["groups"]
    assert len(groups) == 6
    assert len(groups[0]["rows"]) == 32
    assert result["metric_comparisons"]["omitted_table_entries"] == 6
    assert len(result["evidence"]) == len(evidence)
    assert result["evidence"][34]["observation_mean"] == 9
    assert result["evidence"][-1]["observation_mean"] == 9


def test_table_keeps_threshold_details_without_converting_them_to_causal_order():
    item = metric(related_observations=[{
        "metric": "worker_cpu_system", "baseline_mean": 0, "observation_mean": 7,
        "first_sustained_deviation": "2026-09-01T00:01:02Z",
    }])
    result = comparison_evidence([item])
    assert result["evidence"][0]["related_observations"][0] == {
        "metric": "worker_cpu_system", "first_sustained_deviation": "2026-09-01T00:01:02Z",
    }
    assert "do not prove cause/effect order" in result["metric_comparisons"]["rule"]
    assert "failure_class" not in json.dumps(result)


def test_time_profiles_keep_bursts_and_do_not_align_different_buckets():
    profile = [{"start": f"2026-09-01T00:0{i}:00Z", "end": f"2026-09-01T00:0{i}:59Z",
                "mean": value} for i, value in enumerate([0, 10])]
    different = copy.deepcopy(profile)
    different[0]["end"] = "2026-09-01T00:00:30Z"
    result = comparison_evidence([
        metric(time_profile=profile), metric("different", time_profile=different),
        metric("no-profile"),
    ])
    group = result["metric_comparisons"]["groups"][0]
    assert group["rows"][0][5] == [0, 10]
    assert len(group["time_buckets"]) == 2
    assert group["rows"][1][5] is None
    assert "time_profile" not in result["evidence"][0]
    assert result["evidence"][1]["time_profile"] == different
