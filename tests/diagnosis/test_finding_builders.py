from datetime import UTC, datetime

from backend.diagnosis.finding_builders import build_agent_findings
from backend.domain.agent_findings import (
    AgentFindingSeverity,
    AgentFindingType,
    AgentName,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import CauseType


def test_build_agent_findings_groups_successful_core_provider_evidence():
    event = _event(title="Checkout errors after deploy")
    evidence = [
        _evidence(
            "ev-log",
            EvidenceProvider.LOG,
            EvidenceKind.LOG_PATTERN,
            "NullPointerException in checkout handler",
            confidence=0.7,
        ),
        _evidence(
            "ev-metric",
            EvidenceProvider.METRIC,
            EvidenceKind.METRIC_TREND,
            "Error rate increased",
            confidence=0.8,
        ),
        _evidence(
            "ev-deploy",
            EvidenceProvider.DEPLOY,
            EvidenceKind.DEPLOYMENT,
            "Release checkout-api 1.2.3 completed",
            confidence=0.9,
        ),
    ]

    findings = build_agent_findings("inv-1", event, evidence)

    assert {finding.agent_name for finding in findings} == {
        AgentName.LOG,
        AgentName.METRIC,
        AgentName.DEPLOYMENT,
    }
    assert {finding.investigation_id for finding in findings} == {"inv-1"}
    assert all(finding.evidence_ids for finding in findings)
    by_agent = {finding.agent_name: finding for finding in findings}
    assert by_agent[AgentName.DEPLOYMENT].related_cause_type == (
        CauseType.DEPLOYMENT_REGRESSION
    )
    assert by_agent[AgentName.DEPLOYMENT].finding_type == AgentFindingType.ROOT_CAUSE
    assert by_agent[AgentName.LOG].related_cause_type != (
        CauseType.DEPLOYMENT_REGRESSION
    )
    assert by_agent[AgentName.METRIC].related_cause_type != (
        CauseType.DEPLOYMENT_REGRESSION
    )


def test_deploy_provider_without_change_signal_is_not_deployment_regression():
    event = _event()
    evidence = [
        _evidence(
            "ev-deploy",
            EvidenceProvider.DEPLOY,
            EvidenceKind.DEPLOYMENT,
            "No deployment found in the incident window",
            payload={"current_version": "1.2.3"},
        )
    ]

    findings = build_agent_findings("inv-1", event, evidence)
    by_agent = {finding.agent_name: finding for finding in findings}
    finding = by_agent[AgentName.DEPLOYMENT]

    assert set(by_agent) == {AgentName.LOG, AgentName.METRIC, AgentName.DEPLOYMENT}
    assert finding.finding_type == AgentFindingType.CONTRADICTION
    assert finding.related_cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert finding.evidence_ids == ["ev-deploy"]


def test_build_agent_findings_emits_gap_for_missing_core_provider_evidence():
    event = _event()
    evidence = [
        _evidence(
            "ev-log",
            EvidenceProvider.LOG,
            EvidenceKind.LOG_PATTERN,
            "Timeout in checkout handler",
        )
    ]

    findings = build_agent_findings("inv-1", event, evidence)
    by_agent = {finding.agent_name: finding for finding in findings}

    assert set(by_agent) == {
        AgentName.LOG,
        AgentName.METRIC,
        AgentName.DEPLOYMENT,
    }
    assert by_agent[AgentName.METRIC].finding_type == AgentFindingType.GAP
    assert by_agent[AgentName.METRIC].summary == "metric evidence unavailable"
    assert by_agent[AgentName.METRIC].confidence == 0.2
    assert by_agent[AgentName.METRIC].evidence_ids == []
    assert by_agent[AgentName.METRIC].gaps == ["metric evidence is missing"]
    assert by_agent[AgentName.DEPLOYMENT].finding_type == AgentFindingType.GAP
    assert by_agent[AgentName.DEPLOYMENT].summary == "deploy evidence unavailable"
    assert by_agent[AgentName.DEPLOYMENT].confidence == 0.2
    assert by_agent[AgentName.DEPLOYMENT].evidence_ids == []
    assert by_agent[AgentName.DEPLOYMENT].gaps == ["deploy evidence is missing"]


def test_build_agent_findings_emits_gap_for_provider_with_only_failed_evidence():
    event = _event()
    evidence = [
        _evidence(
            "ev-log-failed",
            EvidenceProvider.LOG,
            EvidenceKind.PROVIDER_ERROR,
            "Log provider failed",
            status=EvidenceStatus.FAILED,
            error_message="log file unavailable",
        )
    ]

    findings = build_agent_findings("inv-1", event, evidence)
    by_agent = {finding.agent_name: finding for finding in findings}
    finding = by_agent[AgentName.LOG]

    assert set(by_agent) == {AgentName.LOG, AgentName.METRIC, AgentName.DEPLOYMENT}
    assert finding.agent_name == AgentName.LOG
    assert finding.finding_type == AgentFindingType.GAP
    assert finding.severity == AgentFindingSeverity.LOW
    assert finding.confidence == 0.2
    assert finding.evidence_ids == ["ev-log-failed"]
    assert "evidence unavailable" in finding.summary.lower()
    assert finding.gaps == ["log file unavailable"]


def _event(title: str = "Checkout errors") -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="checkout",
        environment="prod",
        severity=Severity.CRITICAL,
        title=title,
        description="Users see intermittent 500s.",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _evidence(
    evidence_id: str,
    provider: EvidenceProvider,
    kind: EvidenceKind,
    summary: str,
    *,
    confidence: float = 1.0,
    status: EvidenceStatus = EvidenceStatus.SUCCESS,
    error_message: str | None = None,
    payload: dict | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=provider,
        kind=kind,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        summary=summary,
        payload=payload or {},
        confidence=confidence,
        status=status,
        error_message=error_message,
    )
