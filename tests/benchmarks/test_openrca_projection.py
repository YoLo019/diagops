"""OpenRCA thin projection 契约：字段映射、引用校验、去重、Top-N 与安全 audit。

Projector 只做字段投影：不重建归因、不生成候选、不修改时间、不二次排序。
"""

from datetime import UTC, datetime, timedelta

import pytest

from backend.benchmarks.openrca.projection import (
    ProjectionAudit,
    project_root_causes,
    project_v11_candidates,
    scored_fields,
)
from backend.domain.agent_findings import RootCauseAttribution, RootCauseCandidate
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider

BASE = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def evidence(item_id: str, minutes: int = 0) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=BASE + timedelta(minutes=minutes),
        summary=f"evidence {item_id}",
        payload={"signal_type": "memory", "component": "checkout"},
    )


def cause(
    component: str,
    reason: str,
    occurred_at: datetime,
    evidence_ids: list[str],
) -> RootCauseAttribution:
    return RootCauseAttribution(
        root_cause_component=component,
        root_cause_occurred_at=occurred_at,
        root_cause_reason=reason,
        supporting_evidence_ids=evidence_ids,
    )


def test_task_field_mapping_is_fixed_for_all_tasks():
    assert scored_fields("task_1") == ("time",)
    assert scored_fields("task_2") == ("reason",)
    assert scored_fields("task_3") == ("component",)
    assert scored_fields("task_4") == ("time", "reason")
    assert scored_fields("task_5") == ("time", "component")
    assert scored_fields("task_6") == ("component", "reason")
    assert scored_fields("task_7") == ("time", "component", "reason")
    with pytest.raises(ValueError, match="invalid OpenRCA task_index"):
        scored_fields("task_8")


def test_projection_preserves_authoritative_values_without_modification():
    items = [evidence("ev-1", minutes=-30), evidence("ev-2", minutes=5)]
    causes = [
        cause("checkout", "container memory load", BASE, ["ev-1", "ev-2"]),
        cause("payment", "network latency", BASE + timedelta(minutes=2), ["ev-2"]),
    ]

    result = project_root_causes(
        task_index="task_7",
        expected_count=2,
        evidence=items,
        root_causes=causes,
    )

    assert result.causes == tuple(causes)
    # 时间不被 supporting evidence 的更早/更晚观测修改。
    assert result.causes[0].root_cause_occurred_at == BASE
    assert result.audit.projection_fallback is False
    assert result.audit.selected_evidence_ids == (("ev-1", "ev-2"), ("ev-2",))


def test_invalid_evidence_reference_is_dropped():
    items = [evidence("ev-1")]
    causes = [
        cause("checkout", "container memory load", BASE, ["ev-missing"]),
        cause("payment", "network latency", BASE, ["ev-1"]),
    ]

    result = project_root_causes(
        task_index="task_3",
        expected_count=2,
        evidence=items,
        root_causes=causes,
    )

    assert [item.root_cause_component for item in result.causes] == ["payment"]


def test_dedup_by_scored_fields_keeps_first_authoritative():
    items = [evidence("ev-1"), evidence("ev-2")]
    causes = [
        cause("checkout", "container memory load", BASE, ["ev-1"]),
        cause("checkout", "network latency", BASE + timedelta(minutes=1), ["ev-2"]),
    ]

    result = project_root_causes(
        task_index="task_3",
        expected_count=2,
        evidence=items,
        root_causes=causes,
    )

    assert len(result.causes) == 1
    assert result.causes[0].root_cause_reason == "container memory load"
    assert result.audit.projection_fallback is True
    assert result.audit.fallback_reason == "insufficient_distinct_candidates"


def test_truncates_to_expected_root_cause_count():
    items = [evidence(f"ev-{index}") for index in range(3)]
    causes = [
        cause(f"service-{index}", "container memory load", BASE, [f"ev-{index}"])
        for index in range(3)
    ]

    result = project_root_causes(
        task_index="task_3",
        expected_count=2,
        evidence=items,
        root_causes=causes,
    )

    assert len(result.causes) == 2
    assert result.audit.projection_fallback is False


def test_insufficient_authoritative_causes_marks_explicit_fallback():
    items = [evidence("ev-1")]

    result = project_root_causes(
        task_index="task_7",
        expected_count=2,
        evidence=items,
        root_causes=[cause("checkout", "container memory load", BASE, ["ev-1"])],
    )

    assert len(result.causes) == 1
    assert result.audit.projection_fallback is True
    assert result.audit.fallback_reason == "insufficient_distinct_candidates"
    assert result.audit.projection_error is False


def test_empty_authoritative_causes_fabricates_nothing():
    result = project_root_causes(
        task_index="task_1",
        expected_count=1,
        evidence=[evidence("ev-1")],
        root_causes=[],
    )

    assert result.causes == ()
    assert result.audit.projection_fallback is True
    assert result.audit.selected_evidence_ids == ()


def test_audit_contains_only_safe_fields():
    result = project_root_causes(
        task_index="task_3",
        expected_count=1,
        evidence=[evidence("ev-1")],
        root_causes=[cause("checkout", "container memory load", BASE, ["ev-1"])],
    )

    assert set(result.audit.metadata()) == {
        "rule_version",
        "scored_fields",
        "selected_evidence_ids",
        "projection_fallback",
        "fallback_reason",
        "projection_error",
    }
    assert result.audit.rule_version == "v2"


def test_failed_audit_carries_no_candidate_counts_or_detail():
    audit = ProjectionAudit.failed("task_2")

    assert audit.projection_error is True
    assert audit.scored_fields == ("reason",)
    assert set(audit.metadata()) == {
        "rule_version",
        "scored_fields",
        "selected_evidence_ids",
        "projection_fallback",
        "fallback_reason",
        "projection_error",
    }


def test_v11_projection_maps_generic_candidate_without_legacy_taxonomy():
    candidate = RootCauseCandidate(
        id="candidate-generic",
        summary="deployment changed request handling",
        rank=1,
        confidence=0.8,
        affected_entity="checkout-api",
        failure_mechanism="incompatible request handling",
        supporting_evidence_ids=["ev-1"],
        onset_window_start=BASE,
    )

    result = project_v11_candidates(
        task_index="task_7",
        expected_count=1,
        evidence=[evidence("ev-1")],
        candidates=[candidate],
        fallback_timestamp=BASE + timedelta(minutes=5),
    )

    assert result.causes[0].root_cause_component == "checkout-api"
    assert result.causes[0].root_cause_reason == "incompatible request handling"
    assert result.causes[0].root_cause_occurred_at == BASE
    assert result.audit.rule_version == "v11-agent-generic"
    assert result.audit.projection_fallback is False
