from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.diagnosis.evidence_validation import root_cause_claims
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingSeverity,
    AgentFindingType,
    CoordinationReview,
    CriticAssessment,
    FindingActor,
    RootCauseAttribution,
    RootCauseCandidate,
)
from backend.domain.agent_plan import LeadDecision
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    ModelProvider,
    MultiAgentRunStatus,
    StabilizationCategory,
)
from backend.domain.reports import IncidentReport
from backend.domain.runtime import RuntimeDiffSection, RuntimeEventType, RuntimeRunDiff
from backend.runtime.replay import list_all_runtime_events
from backend.safety.redaction import assert_safe_label


def _duration_ms(started: datetime | None, completed: datetime | None) -> int | None:
    if started is None or completed is None:
        return None
    return max(0, round((completed - started).total_seconds() * 1000))


SafeId = Annotated[str, Field(min_length=1, max_length=200)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FrozenRootCauseClaim(_FrozenModel):
    component_sha256: Digest
    reason_sha256: Digest
    occurred_at: datetime


class FrozenEvidence(_FrozenModel):
    id: SafeId
    provider: EvidenceProvider
    kind: EvidenceKind
    timestamp: datetime
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: EvidenceStatus
    root_cause_claims: list[FrozenRootCauseClaim] = Field(
        default_factory=list, max_length=32
    )

    def to_domain(self) -> EvidenceItem:
        return EvidenceItem(
            id=self.id,
            provider=self.provider,
            kind=self.kind,
            timestamp=self.timestamp,
            summary="frozen replay evidence",
            confidence=self.confidence,
            status=self.status,
            payload={
                "root_cause_claims": [
                    {
                        "component": item.component_sha256,
                        "reason": item.reason_sha256,
                        "occurred_at": item.occurred_at.isoformat(),
                    }
                    for item in self.root_cause_claims
                ]
            },
        )


class FrozenHypothesis(_FrozenModel):
    id: SafeId
    cause_type: CauseType
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    supporting_evidence_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    contradicting_evidence_ids: list[SafeId] = Field(default_factory=list, max_length=512)

    def to_domain(self) -> Hypothesis:
        return Hypothesis(
            id=self.id,
            cause_type=self.cause_type,
            summary="frozen replay hypothesis",
            confidence=self.confidence,
            supporting_evidence_ids=self.supporting_evidence_ids,
            contradicting_evidence_ids=self.contradicting_evidence_ids,
        )


class FrozenFinding(_FrozenModel):
    id: SafeId
    investigation_id: SafeId
    agent_name: FindingActor
    finding_type: AgentFindingType
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    related_cause_type: CauseType | None = None
    severity: AgentFindingSeverity
    blocking: bool
    execution_layer: AgentExecutionLayer
    analysis_round: Literal[1, 2]
    revises_finding_id: SafeId | None = None
    gap_count: int = Field(default=0, ge=0, le=64)
    agent_instance_id: SafeId | None = None
    task_id: SafeId | None = None
    runtime_run_id: SafeId | None = None
    critic_assessment_id: SafeId | None = None
    affected_entity: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=256)
    contradicting_evidence_ids: list[SafeId] = Field(default_factory=list, max_length=512)

    def to_domain(self) -> AgentFinding:
        return AgentFinding(
            id=self.id,
            investigation_id=self.investigation_id,
            agent_name=self.agent_name,
            finding_type=self.finding_type,
            summary="frozen replay finding",
            confidence=self.confidence,
            evidence_ids=self.evidence_ids,
            related_cause_type=self.related_cause_type,
            severity=self.severity,
            gaps=["frozen replay gap"] * self.gap_count,
            blocking=self.blocking,
            execution_layer=self.execution_layer,
            analysis_round=self.analysis_round,
            revises_finding_id=self.revises_finding_id,
            agent_instance_id=self.agent_instance_id,
            task_id=self.task_id,
            runtime_run_id=self.runtime_run_id,
            critic_assessment_id=self.critic_assessment_id,
            affected_entity=self.affected_entity,
            failure_mechanism=self.failure_mechanism,
            contradicting_evidence_ids=self.contradicting_evidence_ids,
        )


class FrozenCandidate(_FrozenModel):
    id: SafeId
    cause_type: CauseType | None = None
    rank: int = Field(ge=1, le=256)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    supporting_finding_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    contradicting_finding_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    supporting_evidence_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    contradicting_evidence_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    affected_entity: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=256)
    onset_window_start: datetime | None = None
    onset_window_end: datetime | None = None

    def to_domain(self) -> RootCauseCandidate:
        return RootCauseCandidate(
            id=self.id,
            cause_type=self.cause_type,
            summary="frozen replay candidate",
            rank=self.rank,
            confidence=self.confidence,
            supporting_finding_ids=self.supporting_finding_ids,
            contradicting_finding_ids=self.contradicting_finding_ids,
            supporting_evidence_ids=self.supporting_evidence_ids,
            contradicting_evidence_ids=self.contradicting_evidence_ids,
            affected_entity=self.affected_entity,
            failure_mechanism=self.failure_mechanism,
            onset_window_start=self.onset_window_start,
            onset_window_end=self.onset_window_end,
        )


class FrozenRootCause(_FrozenModel):
    occurred_at: datetime
    component_sha256: Digest
    reason_sha256: Digest
    supporting_evidence_ids: list[SafeId] = Field(min_length=1, max_length=512)

    def to_domain(self) -> RootCauseAttribution:
        return RootCauseAttribution(
            root_cause_occurred_at=self.occurred_at,
            root_cause_component=self.component_sha256,
            root_cause_reason=self.reason_sha256,
            supporting_evidence_ids=self.supporting_evidence_ids,
        )


class FrozenReview(_FrozenModel):
    id: SafeId
    investigation_id: SafeId
    candidates: list[FrozenCandidate] = Field(default_factory=list, max_length=256)
    root_causes: list[FrozenRootCause] = Field(default_factory=list, max_length=64)
    execution_layer: AgentExecutionLayer
    run_status: MultiAgentRunStatus
    decision_status: CoordinationDecisionStatus | None = None
    baseline_cause_type: CauseType | None = None
    selected_cause_type: CauseType | None = None
    model_provider: ModelProvider | None = None
    model_name: str | None = Field(default=None, max_length=160)
    primary_stabilization_category: StabilizationCategory | None = None
    secondary_stabilization_categories: list[StabilizationCategory] = Field(
        default_factory=list, max_length=16
    )
    critic_assessments: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    lead_decision: dict[str, Any] | None = None
    diagnostic_status: str | None = Field(default=None, max_length=32)
    authority_mode: str | None = Field(default=None, max_length=32)
    runtime_run_id: SafeId | None = None

    def to_domain(self) -> CoordinationReview:
        return CoordinationReview(
            id=self.id,
            investigation_id=self.investigation_id,
            candidates=[item.to_domain() for item in self.candidates],
            root_causes=[item.to_domain() for item in self.root_causes],
            execution_layer=self.execution_layer,
            run_status=self.run_status,
            decision_status=self.decision_status,
            baseline_cause_type=self.baseline_cause_type,
            selected_cause_type=self.selected_cause_type,
            model_provider=self.model_provider,
            model_name=self.model_name,
            primary_stabilization_category=self.primary_stabilization_category,
            secondary_stabilization_categories=self.secondary_stabilization_categories,
            critic_assessments=[
                CriticAssessment.model_validate(item)
                for item in self.critic_assessments
            ],
            lead_decision=(
                None
                if self.lead_decision is None
                else LeadDecision.model_validate(self.lead_decision)
            ),
            diagnostic_status=self.diagnostic_status,
            authority_mode=self.authority_mode or "legacy_deterministic",
            runtime_run_id=self.runtime_run_id,
        )


class FrozenReport(_FrozenModel):
    id: SafeId
    investigation_id: SafeId
    hypotheses: list[FrozenHypothesis] = Field(default_factory=list, max_length=64)
    action_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    verification_suggestion_ids: list[SafeId] = Field(
        default_factory=list, max_length=512
    )
    diagnoses: list[FrozenCandidate] = Field(default_factory=list, max_length=256)
    alternatives: list[FrozenCandidate] = Field(default_factory=list, max_length=256)
    diagnostic_status: str | None = Field(default=None, max_length=32)
    authority_mode: str | None = Field(default=None, max_length=32)
    critic_assessments: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    critic_summary: str | None = Field(default=None, max_length=512)
    evidence_gaps: list[str] = Field(default_factory=list, max_length=32)
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    elapsed_time_ms: int = Field(default=0, ge=0)
    runtime_run_id: SafeId | None = None

    def to_domain(self) -> IncidentReport:
        return IncidentReport(
            id=self.id,
            investigation_id=self.investigation_id,
            summary="frozen replay report",
            markdown="frozen replay report",
            hypotheses=[item.to_domain() for item in self.hypotheses],
            action_ids=self.action_ids,
            verification_suggestion_ids=self.verification_suggestion_ids,
            diagnoses=[item.to_domain() for item in self.diagnoses],
            alternatives=[item.to_domain() for item in self.alternatives],
            diagnostic_status=self.diagnostic_status,
            authority_mode=self.authority_mode,
            critic_assessments=[
                CriticAssessment.model_validate(item)
                for item in self.critic_assessments
            ],
            critic_summary=self.critic_summary,
            evidence_gaps=self.evidence_gaps,
            total_input_tokens=self.total_input_tokens,
            total_output_tokens=self.total_output_tokens,
            elapsed_time_ms=self.elapsed_time_ms,
            runtime_run_id=self.runtime_run_id,
        )


class FrozenBusinessProjection(_FrozenModel):
    schema_version: Literal[1] = 1
    investigation_id: SafeId
    environment: str = Field(max_length=160)
    evidence: list[FrozenEvidence] = Field(default_factory=list, max_length=1024)
    hypotheses: list[FrozenHypothesis] = Field(default_factory=list, max_length=64)
    task_ids: list[SafeId] = Field(default_factory=list, max_length=2048)
    execution_ids: list[SafeId] = Field(default_factory=list, max_length=2048)
    tool_call_ids: list[SafeId] = Field(default_factory=list, max_length=2048)
    findings: list[FrozenFinding] = Field(default_factory=list, max_length=512)
    review: FrozenReview | None = None
    report: FrozenReport | None = None
    action_ids: list[SafeId] = Field(default_factory=list, max_length=512)
    verification_suggestion_ids: list[SafeId] = Field(
        default_factory=list, max_length=512
    )
    projection_state: Literal["activated", "not_activated"] = "activated"
    runtime_run_id: SafeId | None = None
    active_runtime_run_id: SafeId | None = None
    source_investigation_id: SafeId | None = None
    diagnostic_status: str | None = Field(default=None, max_length=32)
    authority_mode: str | None = Field(default=None, max_length=32)
    lead_decision: dict[str, Any] | None = None
    critic_assessments: list[dict[str, Any]] = Field(default_factory=list, max_length=3)
    usage: dict[str, int | float] = Field(default_factory=dict, max_length=8)
    checkpoint_projections: dict[SafeId, Digest] = Field(
        default_factory=dict, max_length=64
    )
    integrity_sha256: Digest

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        """拒绝会在 Replay snapshot 中形成持久化凭据的环境标签。"""
        assert_safe_label(value)
        return value


def _relationship_hash(value: str) -> str:
    normalized = " ".join(re.findall(r"\w+", value.casefold()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validation_integrity(projection: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in projection.items() if key != "integrity_sha256"
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reseal_frozen_projection(projection: dict[str, Any]) -> dict[str, Any]:
    candidate = {**projection, "integrity_sha256": "0" * 64}
    validated = FrozenBusinessProjection.model_validate(candidate)
    values = validated.model_dump(mode="json")
    values["integrity_sha256"] = _validation_integrity(values)
    return FrozenBusinessProjection.model_validate(values).model_dump(mode="json")


def parse_frozen_projection(projection: Any) -> FrozenBusinessProjection:
    raw = dict(projection)
    validated = FrozenBusinessProjection.model_validate(projection)
    values = validated.model_dump(mode="json")
    if raw["integrity_sha256"] != _validation_integrity(raw) and values[
        "integrity_sha256"
    ] != _validation_integrity(values):
        raise ValueError("frozen business projection digest mismatch")
    return validated


def finalize_frozen_projection(
    projection: dict[str, Any], checkpoints=()
) -> dict[str, Any]:
    """把 checkpoint 投影纳入源 Run 的防篡改 Replay 清单。"""
    validated = parse_frozen_projection(projection)
    values = validated.model_dump(mode="json")
    values["checkpoint_projections"] = {
        item.id: item.projection_digest for item in checkpoints
    }
    return reseal_frozen_projection(values)


def freeze_business_projection(
    repository,
    investigation_id: str,
    *,
    runtime_run_id: str | None = None,
    authority_mode: str | None = None,
) -> dict[str, Any]:
    """冻结 Replay 所需的结构化关系；自由文本只保留不可逆关联摘要。"""
    record = repository.get(investigation_id)
    review = repository.get_coordination_review(investigation_id)
    findings = repository.list_agent_findings(investigation_id)
    summary = record.multi_agent_run
    snapshot_is_v11 = runtime_run_id is not None
    projection_is_active = (
        not snapshot_is_v11 or record.active_runtime_run_id == runtime_run_id
    )
    effective_authority = authority_mode or (
        review.authority_mode.value
        if review is not None
        else summary.authority_mode.value
        if summary is not None and summary.authority_mode is not None
        else None
    )
    critic_assessments = (
        []
        if review is None
        else [item.model_dump(mode="json") for item in review.critic_assessments]
    )
    lead_decision = (
        None
        if review is None or review.lead_decision is None
        else review.lead_decision.model_dump(mode="json")
    )
    projection = {
        "schema_version": 1,
        "investigation_id": investigation_id,
        "environment": record.event.environment,
        "projection_state": "activated" if projection_is_active else "not_activated",
        "runtime_run_id": runtime_run_id,
        "active_runtime_run_id": (
            record.active_runtime_run_id if projection_is_active else None
        ),
        "source_investigation_id": record.source_investigation_id,
        "diagnostic_status": (
            review.diagnostic_status.value
            if review is not None and review.diagnostic_status is not None
            else summary.diagnostic_status.value
            if summary is not None and summary.diagnostic_status is not None
            else None
        ),
        "authority_mode": effective_authority,
        "lead_decision": lead_decision,
        "critic_assessments": critic_assessments,
        "usage": (
            {}
            if summary is None
            else {
                "input_tokens": summary.total_input_tokens,
                "output_tokens": summary.total_output_tokens,
                "elapsed_time_ms": summary.elapsed_time_ms,
            }
        ),
        "evidence": [
            {
                "id": item.id,
                "provider": item.provider,
                "kind": item.kind,
                "timestamp": item.timestamp,
                "confidence": item.confidence,
                "status": item.status,
                "root_cause_claims": _frozen_root_cause_claims(item, review),
            }
            for item in record.evidence
        ],
        "hypotheses": [
            {
                "id": item.id,
                "cause_type": item.cause_type,
                "confidence": item.confidence,
                "supporting_evidence_ids": item.supporting_evidence_ids,
                "contradicting_evidence_ids": item.contradicting_evidence_ids,
            }
            for item in record.hypotheses
        ],
        "task_ids": sorted(item.id for item in repository.list_tasks(investigation_id)),
        "execution_ids": sorted(
            item.id for item in repository.list_executions(investigation_id)
        ),
        "tool_call_ids": sorted(
            item.id for item in repository.list_tool_calls(investigation_id)
        ),
        "findings": [
            {
                "id": item.id,
                "investigation_id": item.investigation_id,
                "agent_name": item.agent_name,
                "finding_type": item.finding_type,
                "confidence": item.confidence,
                "evidence_ids": item.evidence_ids,
                "related_cause_type": item.related_cause_type,
                "severity": item.severity,
                "blocking": item.blocking,
                "execution_layer": item.execution_layer,
                "analysis_round": item.analysis_round,
                "revises_finding_id": item.revises_finding_id,
                "gap_count": len(item.gaps),
                "agent_instance_id": item.agent_instance_id,
                "task_id": item.task_id,
                "runtime_run_id": item.runtime_run_id,
                "critic_assessment_id": item.critic_assessment_id,
                "affected_entity": item.affected_entity,
                "failure_mechanism": item.failure_mechanism,
                "contradicting_evidence_ids": item.contradicting_evidence_ids,
            }
            for item in findings
        ],
        "review": (
            None
            if review is None
            else {
                "id": review.id,
                "investigation_id": review.investigation_id,
                "candidates": [
                    {
                        "id": item.id,
                        "cause_type": item.cause_type,
                        "rank": item.rank,
                        "confidence": item.confidence,
                        "supporting_finding_ids": item.supporting_finding_ids,
                        "contradicting_finding_ids": item.contradicting_finding_ids,
                        "supporting_evidence_ids": item.supporting_evidence_ids,
                        "contradicting_evidence_ids": item.contradicting_evidence_ids,
                        "affected_entity": item.affected_entity,
                        "failure_mechanism": item.failure_mechanism,
                        "onset_window_start": item.onset_window_start,
                        "onset_window_end": item.onset_window_end,
                    }
                    for item in review.candidates
                ],
                "root_causes": [
                    {
                        "occurred_at": item.root_cause_occurred_at,
                        "component_sha256": _relationship_hash(
                            item.root_cause_component
                        ),
                        "reason_sha256": _relationship_hash(item.root_cause_reason),
                        "supporting_evidence_ids": item.supporting_evidence_ids,
                    }
                    for item in review.root_causes
                ],
                "execution_layer": review.execution_layer,
                "run_status": review.run_status,
                "decision_status": review.decision_status,
                "baseline_cause_type": review.baseline_cause_type,
                "selected_cause_type": review.selected_cause_type,
                "model_provider": review.model_provider,
                "model_name": review.model_name,
                "primary_stabilization_category": (
                    review.primary_stabilization_category
                ),
                "secondary_stabilization_categories": (
                    review.secondary_stabilization_categories
                ),
                "critic_assessments": critic_assessments,
                "lead_decision": lead_decision,
                "diagnostic_status": (
                    review.diagnostic_status.value
                    if review.diagnostic_status is not None
                    else None
                ),
                "authority_mode": review.authority_mode,
                "runtime_run_id": review.runtime_run_id,
            }
        ),
        "report": (
            None
            if record.report is None
            else {
                "id": record.report.id,
                "investigation_id": record.report.investigation_id,
                "hypotheses": [
                    {
                        "id": item.id,
                        "cause_type": item.cause_type,
                        "confidence": item.confidence,
                        "supporting_evidence_ids": item.supporting_evidence_ids,
                        "contradicting_evidence_ids": item.contradicting_evidence_ids,
                    }
                    for item in record.report.hypotheses
                ],
                "action_ids": record.report.action_ids,
                "verification_suggestion_ids": (
                    record.report.verification_suggestion_ids
                ),
                "diagnoses": [
                    {
                        "id": item.id,
                        "cause_type": item.cause_type,
                        "rank": item.rank,
                        "confidence": item.confidence,
                        "supporting_finding_ids": item.supporting_finding_ids,
                        "contradicting_finding_ids": item.contradicting_finding_ids,
                        "supporting_evidence_ids": item.supporting_evidence_ids,
                        "contradicting_evidence_ids": item.contradicting_evidence_ids,
                        "affected_entity": item.affected_entity,
                        "failure_mechanism": item.failure_mechanism,
                        "onset_window_start": item.onset_window_start,
                        "onset_window_end": item.onset_window_end,
                    }
                    for item in record.report.diagnoses
                ],
                "alternatives": [
                    {
                        "id": item.id,
                        "cause_type": item.cause_type,
                        "rank": item.rank,
                        "confidence": item.confidence,
                        "supporting_finding_ids": item.supporting_finding_ids,
                        "contradicting_finding_ids": item.contradicting_finding_ids,
                        "supporting_evidence_ids": item.supporting_evidence_ids,
                        "contradicting_evidence_ids": item.contradicting_evidence_ids,
                        "affected_entity": item.affected_entity,
                        "failure_mechanism": item.failure_mechanism,
                        "onset_window_start": item.onset_window_start,
                        "onset_window_end": item.onset_window_end,
                    }
                    for item in record.report.alternatives
                ],
                "diagnostic_status": (
                    record.report.diagnostic_status.value
                    if record.report.diagnostic_status is not None
                    else None
                ),
                "authority_mode": record.report.authority_mode,
                "critic_assessments": [
                    item.model_dump(mode="json")
                    for item in record.report.critic_assessments
                ],
                "critic_summary": record.report.critic_summary,
                "evidence_gaps": record.report.evidence_gaps,
                "total_input_tokens": record.report.total_input_tokens,
                "total_output_tokens": record.report.total_output_tokens,
                "elapsed_time_ms": record.report.elapsed_time_ms,
                "runtime_run_id": record.report.runtime_run_id,
            }
        ),
        "action_ids": sorted(item.id for item in record.actions),
        "verification_suggestion_ids": sorted(
            item.id for item in record.verification_suggestions
        ),
        "checkpoint_projections": {},
        "integrity_sha256": "0" * 64,
    }
    if snapshot_is_v11 and not projection_is_active:
        for key in (
            "evidence",
            "hypotheses",
            "task_ids",
            "execution_ids",
            "tool_call_ids",
            "findings",
            "action_ids",
            "verification_suggestion_ids",
        ):
            projection[key] = []
        projection.update(
            {
                "review": None,
                "report": None,
                "diagnostic_status": None,
                "lead_decision": None,
                "critic_assessments": [],
                "usage": {},
            }
        )
    return reseal_frozen_projection(projection)


def _frozen_root_cause_claims(
    evidence: EvidenceItem, review: CoordinationReview | None
) -> list[dict[str, Any]]:
    claims = [
        {
            "component_sha256": _relationship_hash(claim["component"]),
            "reason_sha256": _relationship_hash(claim["reason"]),
            "occurred_at": claim["occurred_at"],
        }
        for claim in root_cause_claims(evidence)
    ]
    if review is not None:
        claims.extend(
            {
                "component_sha256": _relationship_hash(item.root_cause_component),
                "reason_sha256": _relationship_hash(item.root_cause_reason),
                "occurred_at": item.root_cause_occurred_at,
            }
            for item in review.root_causes
            if evidence.id in item.supporting_evidence_ids
        )
    return list(
        {
            (
                claim["component_sha256"],
                claim["reason_sha256"],
                str(claim["occurred_at"]),
            ): claim
            for claim in claims
        }.values()
    )[:32]


class RuntimeDiffService:
    def __init__(self, *, store) -> None:
        self.store = store

    def compare(self, run_id: str, against_run_id: str) -> RuntimeRunDiff:
        left = self.store.get_run(run_id)
        right = self.store.get_run(against_run_id)
        if left.investigation_id != right.investigation_id:
            raise ValueError("runtime runs belong to different investigations")
        left_projection = self._project(left)
        right_projection = self._project(right)
        sections = {
            name: RuntimeDiffSection(
                left=left_projection[name],
                right=right_projection[name],
                changed=left_projection[name] != right_projection[name],
            )
            for name in (
                "configuration",
                "phases",
                "agents",
                "tools",
                "evidence_references",
                "causes",
                "final_decision",
                "metrics",
                "fallback_and_failure",
            )
        }
        return RuntimeRunDiff(
            run_id=left.id,
            against_run_id=right.id,
            sections=sections,
        )

    def _project(self, run) -> dict[str, Any]:
        events = list_all_runtime_events(self.store, run.id)
        semantic_events = [
            event
            for event in events
            if event.schema_version == 1 and isinstance(event.event_type, RuntimeEventType)
        ]
        business = None
        try:
            business = parse_frozen_projection(
                self.store.get_frozen_business_projection(run.id)
            )
        except (TypeError, ValueError):
            # 终态 Run 缺失快照时只能报告不可用；回读当前 Investigation 会篡改历史。
            causes: Any = {"status": "unavailable"}
            final_decision: Any = {"status": "unavailable"}
        else:
            causes = sorted(
                (
                    {
                        "cause_type": item.cause_type.value,
                        "confidence": item.confidence,
                    }
                    for item in business.hypotheses
                ),
                key=lambda item: (item["cause_type"], item["confidence"]),
            )
            final_decision = self._final_decision(business.review)
            if run.is_v11:
                causes = self._v11_candidates(business)
                final_decision = self._v11_final_decision(business)
        return {
            "configuration": {
                "strategy": run.strategy.value,
                "provider": run.model_provider.value if run.model_provider else None,
                "model": run.model_name,
                "prompt_version": run.prompt_version,
                "execution_contract_version": run.execution_contract_version.value,
                "authority_mode": run.authority_mode.value,
            },
            "phases": self._phases(semantic_events),
            "agents": self._agents(semantic_events),
            "tools": self._tools(semantic_events),
            "evidence_references": sorted(
                {
                    item
                    for event in semantic_events
                    for item in event.evidence_ids
                }
                | (
                    {item.id for item in business.evidence}
                    if run.is_v11 and business is not None
                    else set()
                )
            ),
            "causes": causes,
            "final_decision": final_decision,
            "metrics": self._metrics(run, semantic_events, business),
            "fallback_and_failure": {
                "run_kind": run.run_kind.value,
                "run_reason": run.run_reason.value,
                "status": run.status.value,
                "projection_state": (
                    business.projection_state if business is not None else "unavailable"
                ),
                "failure_category": (run.failure_category.value if run.failure_category else None),
                "event_failure_categories": sorted(
                    {
                        str(event.safe_payload["failure_category"])
                        for event in semantic_events
                        if event.safe_payload.get("failure_category") is not None
                    }
                ),
            },
        }

    @staticmethod
    def _final_decision(review: FrozenReview | None) -> dict[str, Any] | None:
        if review is None:
            return None
        return {
            "decision_status": (
                review.decision_status.value if review.decision_status else None
            ),
            "selected_cause_type": (
                review.selected_cause_type.value
                if review.selected_cause_type
                else None
            ),
            "root_causes": [
                {
                    "component_sha256": item.component_sha256,
                    "reason_sha256": item.reason_sha256,
                    "occurred_at": item.occurred_at.isoformat(),
                    "evidence_ids": sorted(item.supporting_evidence_ids),
                }
                for item in review.root_causes
            ],
        }

    @staticmethod
    def _v11_candidates(projection: FrozenBusinessProjection) -> list[dict[str, Any]]:
        if projection.review is None:
            return []
        return [
            {
                "id": item.id,
                "cause_type": item.cause_type.value if item.cause_type else None,
                "rank": item.rank,
                "confidence": item.confidence,
                "affected_entity": item.affected_entity,
                "failure_mechanism": item.failure_mechanism,
                "onset_window_start": (
                    item.onset_window_start.isoformat()
                    if item.onset_window_start is not None
                    else None
                ),
                "onset_window_end": (
                    item.onset_window_end.isoformat()
                    if item.onset_window_end is not None
                    else None
                ),
                "supporting_finding_ids": sorted(item.supporting_finding_ids),
                "contradicting_finding_ids": sorted(
                    item.contradicting_finding_ids
                ),
                "supporting_evidence_ids": sorted(item.supporting_evidence_ids),
                "contradicting_evidence_ids": sorted(
                    item.contradicting_evidence_ids
                ),
            }
            for item in projection.review.candidates
        ]

    @staticmethod
    def _v11_final_decision(
        projection: FrozenBusinessProjection,
    ) -> dict[str, Any]:
        if projection.projection_state == "not_activated":
            return {
                "status": "not_activated",
                "runtime_run_id": projection.runtime_run_id,
                "active_runtime_run_id": projection.active_runtime_run_id,
            }
        return {
            "diagnostic_status": projection.diagnostic_status,
            "authority_mode": projection.authority_mode,
            "runtime_run_id": projection.runtime_run_id,
            "active_runtime_run_id": projection.active_runtime_run_id,
            "authoritative_candidate_ids": (
                (projection.lead_decision or {}).get("candidate_ids", [])
            ),
            "lead_decision": projection.lead_decision,
            "critic_assessments": projection.critic_assessments,
        }

    @staticmethod
    def _phases(events) -> list[dict[str, Any]]:
        starts: dict[str, datetime] = {}
        rows: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.schema_version != 1 or event.phase is None:
                continue
            phase = str(event.phase)
            if event.event_type == RuntimeEventType.PHASE_STARTED:
                starts[phase] = event.occurred_at
            elif event.event_type in {
                RuntimeEventType.PHASE_COMPLETED,
                RuntimeEventType.PHASE_FAILED,
                RuntimeEventType.PHASE_SKIPPED,
            }:
                rows[phase] = {
                    "phase": phase,
                    "status": event.safe_payload.get("status"),
                    "duration_ms": _duration_ms(starts.get(phase), event.occurred_at),
                }
        return [rows[key] for key in sorted(rows)]

    @staticmethod
    def _agents(events) -> list[dict[str, Any]]:
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for event in events:
            if event.schema_version != 1:
                continue
            if event.event_type in {
                RuntimeEventType.AGENT_COMPLETED,
                RuntimeEventType.AGENT_FAILED,
                RuntimeEventType.AGENT_STARTED,
            }:
                name = event.actor_name or "unknown"
                counts[name][str(event.safe_payload.get("status", "unknown"))] += 1
        return [
            {"agent_name": name, **dict(sorted(counts[name].items()))} for name in sorted(counts)
        ]

    @staticmethod
    def _tools(events) -> list[dict[str, Any]]:
        rows = []
        for event in events:
            if (
                event.schema_version != 1
                or not isinstance(event.event_type, RuntimeEventType)
                or not event.event_type.value.startswith("tool.")
            ):
                continue
            rows.append(
                {
                    "tool_name": event.safe_payload.get("tool_name"),
                    "status": event.safe_payload.get("status"),
                    "normalized_inputs": event.safe_payload.get("normalized_inputs", {}),
                }
            )
        return sorted(
            rows,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _metrics(run, events, projection=None) -> dict[str, Any]:
        metrics = {
            "input_tokens": sum(
                int(event.safe_payload.get("input_tokens", 0))
                for event in events
                if event.schema_version == 1
            ),
            "output_tokens": sum(
                int(event.safe_payload.get("output_tokens", 0))
                for event in events
                if event.schema_version == 1
            ),
            "cost": round(
                sum(
                    float(event.safe_payload.get("cost", 0))
                    for event in events
                    if event.schema_version == 1
                ),
                9,
            ),
            "duration_ms": _duration_ms(run.started_at, run.completed_at),
        }
        if projection is not None and run.is_v11:
            metrics["usage"] = projection.usage
        return metrics


def canonical_diff_json(diff: RuntimeRunDiff) -> str:
    return json.dumps(
        diff.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
