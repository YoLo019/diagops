from __future__ import annotations

import json

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingSeverity,
    AgentFindingType,
    AgentName,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceProvider, EvidenceStatus
from backend.domain.hypotheses import CauseType

_PROVIDERS = (
    EvidenceProvider.LOG,
    EvidenceProvider.METRIC,
    EvidenceProvider.DEPLOY,
)

_AGENT_NAMES = {
    EvidenceProvider.LOG: AgentName.LOG,
    EvidenceProvider.METRIC: AgentName.METRIC,
    EvidenceProvider.DEPLOY: AgentName.DEPLOYMENT,
}


def build_agent_findings(
    investigation_id: str, event: IncidentEvent, evidence: list[EvidenceItem]
) -> list[AgentFinding]:
    findings: list[AgentFinding] = []

    for provider in _PROVIDERS:
        provider_evidence = [item for item in evidence if item.provider == provider]
        if not provider_evidence:
            findings.append(_gap_finding(investigation_id, provider, []))
            continue

        usable = [
            item for item in provider_evidence if item.status != EvidenceStatus.FAILED
        ]
        if not usable:
            findings.append(_gap_finding(investigation_id, provider, provider_evidence))
            continue

        cause_type, finding_type = _infer_cause_type(provider, usable)
        findings.append(
            AgentFinding(
                investigation_id=investigation_id,
                agent_name=_AGENT_NAMES[provider],
                finding_type=finding_type,
                summary=f"{provider.value} evidence signal",
                confidence=max(item.confidence for item in usable),
                evidence_ids=[item.id for item in usable],
                related_cause_type=cause_type,
                severity=(
                    AgentFindingSeverity.HIGH
                    if cause_type
                    else AgentFindingSeverity.MEDIUM
                ),
                rationale="; ".join(item.summary for item in usable[:3]),
            )
        )

    return findings


def _gap_finding(
    investigation_id: str, provider: EvidenceProvider, evidence: list[EvidenceItem]
) -> AgentFinding:
    return AgentFinding(
        investigation_id=investigation_id,
        agent_name=_AGENT_NAMES[provider],
        finding_type=AgentFindingType.GAP,
        summary=f"{provider.value} evidence unavailable",
        confidence=0.2,
        evidence_ids=[item.id for item in evidence],
        severity=AgentFindingSeverity.LOW,
        gaps=[item.error_message or item.summary for item in evidence]
        or [f"{provider.value} evidence is missing"],
    )


def _infer_cause_type(
    provider: EvidenceProvider, evidence: list[EvidenceItem]
) -> tuple[CauseType | None, AgentFindingType]:
    text = _evidence_text(evidence)
    if provider == EvidenceProvider.DEPLOY:
        if any(
            phrase in text
            for phrase in (
                "no deployment",
                "no deploy",
                "no release",
                "no change",
                "empty change",
            )
        ):
            return CauseType.DEPLOYMENT_REGRESSION, AgentFindingType.CONTRADICTION
        if any(
            phrase in text
            for phrase in ("deployed", "release ", "rollback", "version_change", "new_version")
        ):
            return CauseType.DEPLOYMENT_REGRESSION, AgentFindingType.ROOT_CAUSE
    if any(word in text for word in ("traffic", "qps", "spike")):
        return CauseType.TRAFFIC_SPIKE, AgentFindingType.ROOT_CAUSE
    if any(word in text for word in ("timeout", "dependency", "downstream")):
        return CauseType.DOWNSTREAM_DEPENDENCY_FAILURE, AgentFindingType.ROOT_CAUSE
    if any(word in text for word in ("database", "db", "slow query")):
        return CauseType.DATABASE_SLOWDOWN, AgentFindingType.ROOT_CAUSE
    return None, AgentFindingType.SIGNAL


def _evidence_text(evidence: list[EvidenceItem]) -> str:
    return " ".join(
        f"{item.summary} {json.dumps(item.payload, sort_keys=True)}"
        for item in evidence
    ).lower()
