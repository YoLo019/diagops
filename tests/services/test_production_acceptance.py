import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.db.models import InvestigationRecord
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.coordination_review import build_coordination_review
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.rca.analyzer import RcaAnalyzer
from backend.services.production_acceptance import (
    _EXPECTED_ATTRIBUTIONS,
    aggregate_scenario_results,
    run_scenario,
    validate_acceptance_artifact,
)


def _scenario(name, cause, confidence=0.9):
    return {
        "scenario": name,
        "passed": True,
        "alert_source": "alertmanager",
        "investigation_id": f"inv-{name.replace('_', '')}",
        "top_cause_type": cause,
        "confidence": confidence,
        "evidence_reference_validity": 1,
        "read_only_violations": 0,
        "duration_seconds": 10,
    }


def _artifact():
    return {
        "schema_version": 1,
        "scenarios": [
            _scenario("deployment_regression", "deployment_regression"),
            _scenario(
                "dependency_timeout",
                "downstream_dependency_failure",
            ),
            _scenario("traffic_spike", "traffic_spike"),
            _scenario("healthy_control", "unknown", 0.2),
        ],
        "total_duration_seconds": 40,
        "privacy_scan_passed": True,
    }


def _scenario_v2(name, cause, confidence=0.9, **overrides):
    base = {
        "scenario": name,
        "passed": True,
        "alert_source": "alertmanager",
        "investigation_id": f"inv-{name.replace('_', '')}",
        "top_cause_type": cause,
        "confidence": confidence,
        "evidence_reference_validity": 1,
        "read_only_violations": 0,
        "duration_seconds": 10,
        "component": None,
        "reason": None,
        "occurred_at": None,
        "onset_error_seconds": None,
        "onset_available": True,
        "privacy_scan_passed": True,
    }
    base.update(overrides)
    return base


_NEW_SCENARIO_ROWS = (
    ("memory_pressure", "resource_saturation", "container memory load"),
    ("network_corruption", "network_fault", "network packet corruption"),
    ("process_failure", "process_or_container_failure", "container process failure"),
)


def _artifact_v2():
    scenarios = [
        _scenario_v2("deployment_regression", "deployment_regression"),
        _scenario_v2("dependency_timeout", "downstream_dependency_failure"),
        _scenario_v2("traffic_spike", "traffic_spike"),
        _scenario_v2("healthy_control", "unknown", 0.2),
    ]
    for name, cause, reason in _NEW_SCENARIO_ROWS:
        scenarios.append(
            _scenario_v2(
                name,
                cause,
                component="checkout-service",
                reason=reason,
                occurred_at="2026-07-30T12:00:20+00:00",
                onset_error_seconds=20,
            )
        )
    return {
        "schema_version": 2,
        "scenarios": scenarios,
        "total_duration_seconds": 70,
        "privacy_scan_passed": True,
    }


def test_acceptance_validator_accepts_exact_safe_four_scenarios():
    assert validate_acceptance_artifact(_artifact()) == []


def test_acceptance_validator_requires_exact_scenario_set():
    artifact = _artifact()
    artifact["scenarios"].pop()

    assert any(
        "exactly four scenarios" in failure for failure in validate_acceptance_artifact(artifact)
    )


def test_acceptance_validator_checks_fault_top_causes():
    artifact = _artifact()
    artifact["scenarios"][0]["top_cause_type"] = "traffic_spike"

    assert any(
        "deployment_regression Top-1" in failure
        for failure in validate_acceptance_artifact(artifact)
    )


def test_acceptance_validator_checks_healthy_control_confidence():
    artifact = _artifact()
    artifact["scenarios"][3]["top_cause_type"] = "traffic_spike"
    artifact["scenarios"][3]["confidence"] = 0.8

    assert any("healthy_control" in failure for failure in validate_acceptance_artifact(artifact))


def test_acceptance_validator_checks_alert_source_and_safety_metrics():
    artifact = _artifact()
    artifact["scenarios"][0]["alert_source"] = "direct"
    artifact["scenarios"][1]["evidence_reference_validity"] = 0.9
    artifact["scenarios"][2]["read_only_violations"] = 1
    artifact["scenarios"][3]["passed"] = False

    failures = validate_acceptance_artifact(artifact)

    assert any("Alertmanager" in failure for failure in failures)
    assert any("Evidence" in failure for failure in failures)
    assert any("read-only" in failure for failure in failures)
    assert any("scenario failed" in failure for failure in failures)


def test_acceptance_validator_checks_duration_and_privacy():
    artifact = _artifact()
    artifact["total_duration_seconds"] = 900
    artifact["privacy_scan_passed"] = False

    failures = validate_acceptance_artifact(artifact)

    assert any("900 seconds" in failure for failure in failures)
    assert any("privacy" in failure for failure in failures)


def test_acceptance_validator_rejects_non_allowlisted_artifact_fields():
    artifact = _artifact()
    artifact["scenarios"][0]["raw_payload"] = "secret=unsafe"

    assert any("schema" in failure for failure in validate_acceptance_artifact(artifact))


def test_acceptance_validator_v2_accepts_exact_seven_scenarios():
    assert validate_acceptance_artifact(_artifact_v2()) == []


def test_acceptance_validator_v2_requires_exactly_seven_scenarios():
    artifact = _artifact_v2()
    artifact["scenarios"] = artifact["scenarios"][:4]

    assert any(
        "exactly seven scenarios" in failure
        for failure in validate_acceptance_artifact(artifact)
    )


def test_acceptance_validator_v2_checks_new_scenario_component_and_reason():
    artifact = _artifact_v2()
    artifact["scenarios"][4]["component"] = "payment-service"
    artifact["scenarios"][5]["reason"] = "network latency"

    failures = validate_acceptance_artifact(artifact)

    assert any("memory_pressure component" in failure for failure in failures)
    assert any("network_corruption reason" in failure for failure in failures)


def test_acceptance_validator_v2_bounds_new_scenario_onset_error():
    artifact = _artifact_v2()
    artifact["scenarios"][4]["onset_error_seconds"] = 61
    artifact["scenarios"][5]["onset_error_seconds"] = None

    failures = validate_acceptance_artifact(artifact)

    assert any("memory_pressure onset" in failure for failure in failures)
    assert any("network_corruption onset" in failure for failure in failures)


def test_acceptance_validator_v2_forbids_onset_fallback_in_new_scenarios():
    artifact = _artifact_v2()
    artifact["scenarios"][6]["onset_available"] = False

    assert any(
        "process_failure onset" in failure
        for failure in validate_acceptance_artifact(artifact)
    )


def test_acceptance_validator_v2_rejects_non_allowlisted_fields():
    artifact = _artifact_v2()
    artifact["scenarios"][0]["activated_at"] = "2026-07-30T12:00:00+00:00"

    assert any("schema" in failure for failure in validate_acceptance_artifact(artifact))


def test_aggregate_scenario_results_writes_valid_allowlisted_artifact(tmp_path):
    scenario_paths = []
    for scenario in _artifact_v2()["scenarios"]:
        path = tmp_path / f"{scenario['scenario']}.json"
        path.write_text(json.dumps(scenario), encoding="utf-8")
        scenario_paths.append(path)
    output = tmp_path / "result.json"

    artifact = aggregate_scenario_results(scenario_paths, output)

    assert output.exists()
    assert artifact.schema_version == 2
    assert artifact.total_duration_seconds == 70
    assert validate_acceptance_artifact(json.loads(output.read_text())) == []


def test_aggregate_preserves_unfavorable_artifact_before_failing(tmp_path):
    scenario_paths = []
    scenarios = _artifact_v2()["scenarios"]
    scenarios[0]["passed"] = False
    for scenario in scenarios:
        path = tmp_path / f"{scenario['scenario']}.json"
        path.write_text(json.dumps(scenario), encoding="utf-8")
        scenario_paths.append(path)
    output = tmp_path / "result.json"

    with pytest.raises(ValueError, match="scenario failed"):
        aggregate_scenario_results(scenario_paths, output)

    assert output.exists()
    assert json.loads(output.read_text())["scenarios"][0]["passed"] is False


def _runner_request(calls, detail, review, investigation_reads):
    def request(method, url, payload=None, timeout=10):
        calls.append((method, url, payload))
        if url.endswith("/investigations"):
            investigation_reads.append(1)
            return [] if len(investigation_reads) == 1 else [{"id": "inv-test"}]
        if url.endswith("/investigations/inv-test"):
            return detail
        if url.endswith("/coordination-review"):
            return review
        if url.endswith("/tool-calls"):
            return [
                {
                    "tool_name": "read_logs",
                    "output_evidence_ids": ["ev-1"],
                }
            ]
        for mode in ("memory_pressure", "network_corruption", "process_failure"):
            if url.endswith(f"/fault/{mode}"):
                return {
                    "mode": mode,
                    "activated_at": "2026-07-30T12:00:00+00:00",
                }
        return {"status": "ok"}

    return request


def test_scenario_runner_uses_lab_fault_and_alertmanager_chain():
    calls = []
    investigation_reads = []

    def request(method, url, payload=None, timeout=10):
        calls.append((method, url, payload))
        if url.endswith("/investigations"):
            investigation_reads.append(1)
            if len(investigation_reads) == 1:
                return []
            return [{"id": "inv-test"}]
        if url.endswith("/investigations/inv-test"):
            return {
                "id": "inv-test",
                "status": "completed",
                "event": {"source": "webhook"},
                "evidence": [{"id": "ev-1"}],
                "hypotheses": [
                    {
                        "cause_type": "deployment_regression",
                        "confidence": 0.9,
                        "supporting_evidence_ids": ["ev-1"],
                    }
                ],
            }
        if url.endswith("/coordination-review"):
            return {"root_causes": []}
        if url.endswith("/tool-calls"):
            return [
                {
                    "tool_name": "read_logs",
                    "output_evidence_ids": ["ev-1"],
                }
            ]
        return {"status": "ok"}

    result = run_scenario(
        "deployment_regression",
        app_url="http://app",
        dependency_url="http://dependency",
        diagops_url="http://diagops",
        alertmanager_url="http://alertmanager",
        requester=request,
        sleeper=lambda _seconds: None,
    )

    assert result.passed is True
    assert result.top_cause_type == "deployment_regression"
    assert ("POST", "http://app/fault/deployment", None) in calls
    assert all("/events/alertmanager" not in url for _, url, _ in calls)


def test_healthy_runner_injects_only_through_alertmanager_api():
    calls = []
    investigation_reads = 0

    def request(method, url, payload=None, timeout=10):
        nonlocal investigation_reads
        calls.append((method, url, payload))
        if url.endswith("/investigations"):
            investigation_reads += 1
            return [] if investigation_reads == 1 else [{"id": "inv-healthy"}]
        if url.endswith("/investigations/inv-healthy"):
            return {
                "id": "inv-healthy",
                "status": "completed",
                "event": {"source": "webhook"},
                "evidence": [],
                "hypotheses": [
                    {
                        "cause_type": "unknown",
                        "confidence": 0.2,
                        "supporting_evidence_ids": [],
                    }
                ],
            }
        if url.endswith("/coordination-review"):
            return {"root_causes": []}
        if url.endswith("/tool-calls"):
            return []
        return {"status": "ok"}

    result = run_scenario(
        "healthy_control",
        app_url="http://app",
        dependency_url="http://dependency",
        diagops_url="http://diagops",
        alertmanager_url="http://alertmanager",
        requester=request,
        sleeper=lambda _seconds: None,
    )

    alert_calls = [item for item in calls if item[1] == "http://alertmanager/api/v2/alerts"]
    assert result.passed is True
    assert len(alert_calls) == 1
    assert alert_calls[0][2][0]["labels"] == {
        "service": "checkout-service",
        "environment": "prod",
        "severity": "warning",
    }
    assert all("/events/alertmanager" not in url for _, url, _ in calls)


def test_new_scenario_runner_scores_authoritative_attribution_and_onset():
    calls = []
    detail = {
        "id": "inv-test",
        "status": "completed",
        "event": {"source": "webhook"},
        "evidence": [
            {
                "id": "ev-1",
                "payload": {
                    "signal_type": "memory",
                    "anomaly_onset": "2026-07-30T12:00:20+00:00",
                },
            }
        ],
        "hypotheses": [
            {
                "cause_type": "resource_saturation",
                "confidence": 0.9,
                "supporting_evidence_ids": ["ev-1"],
            }
        ],
    }
    review = {
        "root_causes": [
            {
                "root_cause_component": "checkout-service",
                "root_cause_reason": "container memory load",
                "root_cause_occurred_at": "2026-07-30T12:00:20+00:00",
                "supporting_evidence_ids": ["ev-1"],
            }
        ]
    }

    result = run_scenario(
        "memory_pressure",
        app_url="http://app",
        dependency_url="http://dependency",
        diagops_url="http://diagops",
        alertmanager_url="http://alertmanager",
        requester=_runner_request(calls, detail, review, []),
        sleeper=lambda _seconds: None,
    )

    assert result.passed is True
    assert result.top_cause_type == "resource_saturation"
    assert result.component == "checkout-service"
    assert result.reason == "container memory load"
    assert result.occurred_at is not None
    assert result.onset_error_seconds == 20
    assert result.onset_available is True
    assert result.privacy_scan_passed is True


def test_new_scenario_runner_fails_on_onset_fallback():
    calls = []
    detail = {
        "id": "inv-test",
        "status": "completed",
        "event": {"source": "webhook"},
        "evidence": [
            {
                "id": "ev-1",
                "payload": {
                    "signal_type": "memory",
                    "onset_unavailable": True,
                },
            }
        ],
        "hypotheses": [
            {
                "cause_type": "resource_saturation",
                "confidence": 0.9,
                "supporting_evidence_ids": ["ev-1"],
            }
        ],
    }
    review = {
        "root_causes": [
            {
                "root_cause_component": "checkout-service",
                "root_cause_reason": "container memory load",
                "root_cause_occurred_at": "2026-07-30T12:00:20+00:00",
                "supporting_evidence_ids": ["ev-1"],
            }
        ]
    }

    result = run_scenario(
        "memory_pressure",
        app_url="http://app",
        dependency_url="http://dependency",
        diagops_url="http://diagops",
        alertmanager_url="http://alertmanager",
        requester=_runner_request(calls, detail, review, []),
        sleeper=lambda _seconds: None,
    )

    assert result.onset_available is False
    assert result.passed is False


def test_runner_privacy_scan_flags_answer_leak_in_persisted_records():
    calls = []
    detail = {
        "id": "inv-test",
        "status": "completed",
        "event": {
            "source": "webhook",
            "title": "memory_pressure detected in checkout-service",
        },
        "evidence": [{"id": "ev-1", "payload": {"signal_type": "memory"}}],
        "hypotheses": [
            {
                "cause_type": "resource_saturation",
                "confidence": 0.9,
                "supporting_evidence_ids": ["ev-1"],
            }
        ],
    }
    review = {
        "root_causes": [
            {
                "root_cause_component": "checkout-service",
                "root_cause_reason": "container memory load",
                "root_cause_occurred_at": "2026-07-30T12:00:20+00:00",
                "supporting_evidence_ids": ["ev-1"],
            }
        ]
    }

    result = run_scenario(
        "memory_pressure",
        app_url="http://app",
        dependency_url="http://dependency",
        diagops_url="http://diagops",
        alertmanager_url="http://alertmanager",
        requester=_runner_request(calls, detail, review, []),
        sleeper=lambda _seconds: None,
    )

    assert result.privacy_scan_passed is False
    assert result.passed is False


def test_runner_privacy_scan_allows_canonical_signal_type_matching_scenario():
    """signal_type 的 canonical 值（如 network_corruption）与 scenario 同词时
    不得误判为答案泄漏；event 入口仍扫描 scenario token。"""
    calls = []
    detail = {
        "id": "inv-test",
        "status": "completed",
        "event": {"source": "webhook"},
        "evidence": [
            {
                "id": "ev-1",
                "payload": {
                    "signal_type": "network_corruption",
                    "anomaly_onset": "2026-07-30T12:00:20+00:00",
                },
            }
        ],
        "hypotheses": [
            {
                "cause_type": "network_fault",
                "confidence": 0.9,
                "supporting_evidence_ids": ["ev-1"],
            }
        ],
    }
    review = {
        "root_causes": [
            {
                "root_cause_component": "checkout-service",
                "root_cause_reason": "network packet corruption",
                "root_cause_occurred_at": "2026-07-30T12:00:20+00:00",
                "supporting_evidence_ids": ["ev-1"],
            }
        ]
    }

    result = run_scenario(
        "network_corruption",
        app_url="http://app",
        dependency_url="http://dependency",
        diagops_url="http://diagops",
        alertmanager_url="http://alertmanager",
        requester=_runner_request(calls, detail, review, []),
        sleeper=lambda _seconds: None,
    )

    assert result.privacy_scan_passed is True
    assert result.passed is True


def test_production_competition_presence_signal_outranks_relative_noise(tmp_path):
    # 生产竞争回归（V10.1 T6, spec F6/F17/R6）：真实零基线 network 信号
    # （presence-only、多点支持、更晚 onset）与 relative 单点噪声
    # （deviation 10、更早 onset）同时存在时，经 SQLite 持久化 reload 的
    # authoritative review Top-1 仍是真实信号；绝不比较跨 basis 的
    # numeric strength。
    base = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)

    def metric_signal(
        evidence_id: str,
        offset_seconds: float,
        payload: dict,
    ) -> EvidenceItem:
        return EvidenceItem(
            id=evidence_id,
            provider=EvidenceProvider.METRIC,
            kind=EvidenceKind.METRIC_TREND,
            timestamp=base + timedelta(seconds=offset_seconds),
            summary=f"{evidence_id} summary",
            payload=payload,
        )

    noise = metric_signal(
        "ev-noise",
        0,
        {
            "component": "checkout",
            "signal_type": "cpu",
            "signal_name": "cpu_usage",
            "deviation_score": 10.0,
            "anomaly_segment_id": "seg-noise",
            "strength_basis": "relative",
            "anomaly_point_count": 1,
        },
    )
    real = metric_signal(
        "ev-real",
        100,
        {
            "component": "checkout-service",
            "signal_type": "network_corruption",
            "signal_name": "container_network_receive_packets_dropped",
            "deviation_score": 1.0,
            "anomaly_segment_id": "seg-real",
            "strength_basis": "presence_only",
            "anomaly_point_count": 3,
        },
    )
    evidence = [noise, real]
    event = IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="checkout-service",
        environment="prod",
        severity=Severity.WARNING,
        title="production competition",
        description="presence real signal versus relative noise",
        started_at=base,
    )
    hypotheses = RcaAnalyzer().analyze(event, evidence)
    assert {item.cause_type.value for item in hypotheses} >= {
        "network_fault",
        "resource_saturation",
    }
    review = build_coordination_review("inv-compete", [], evidence, hypotheses)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'diagops-compete.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    repository.save(InvestigationRecord(id="inv-compete", event=event))
    repository.save_coordination_review(review)

    reloaded = repository.get_coordination_review("inv-compete")

    assert reloaded is not None
    expected_cause, expected_component, expected_reason = _EXPECTED_ATTRIBUTIONS[
        "network_corruption"
    ]
    assert expected_cause == "network_fault"
    top = reloaded.root_causes[0]
    assert top.supporting_evidence_ids == ["ev-real"]
    assert top.root_cause_component == expected_component
    assert top.root_cause_reason == expected_reason
    assert reloaded.root_causes[1].supporting_evidence_ids == ["ev-noise"]
