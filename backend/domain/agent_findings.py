from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_serializer,
    model_validator,
)

from backend.domain.agent_plan import LeadDecision
from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    CoordinationDecisionStatus,
    CriticVerdict,
    DiagnosticStatus,
    ModelProvider,
    MultiAgentRunStatus,
    StabilizationCategory,
)


class AgentName(StrEnum):
    LOG = "LogAgent"
    METRIC = "MetricAgent"
    DEPLOYMENT = "DeploymentAgent"


class FindingActor(StrEnum):
    LOG = AgentName.LOG.value
    METRIC = AgentName.METRIC.value
    DEPLOYMENT = AgentName.DEPLOYMENT.value
    INVESTIGATOR = "InvestigatorAgent"


class AgentFindingType(StrEnum):
    SIGNAL = "signal"
    ROOT_CAUSE = "root_cause"
    CONTRADICTION = "contradiction"
    GAP = "gap"


class AgentFindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentFinding(BaseModel):
    id: str = Field(default_factory=lambda: f"finding-{uuid4().hex}")
    investigation_id: str
    agent_name: FindingActor
    agent_instance_id: str | None = Field(default=None, max_length=128)
    finding_type: AgentFindingType
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(default_factory=list)
    related_cause_type: CauseType | None = None
    severity: AgentFindingSeverity = AgentFindingSeverity.MEDIUM
    rationale: str = ""
    gaps: list[str] = Field(default_factory=list)
    blocking: bool = False
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    analysis_round: Literal[1, 2] = 1
    revises_finding_id: str | None = None
    task_id: str | None = None
    runtime_run_id: str | None = None
    critic_assessment_id: str | None = None
    affected_entity: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=512)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("agent_name")
    def serialize_agent_name(self, value: FindingActor | AgentName) -> str:
        return FindingActor(getattr(value, "value", value)).value

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        if self.runtime_run_id is None:
            for field_name in (
                "agent_instance_id",
                "task_id",
                "runtime_run_id",
                "critic_assessment_id",
                "affected_entity",
                "failure_mechanism",
                "contradicting_evidence_ids",
            ):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data

    @model_validator(mode="after")
    def validate_finding(self) -> "AgentFinding":
        self.agent_name = FindingActor(self.agent_name)
        if self.finding_type != AgentFindingType.GAP and not self.evidence_ids:
            raise ValueError("evidence_ids required unless finding_type is gap")
        if self.blocking and (self.finding_type != AgentFindingType.GAP or not self.gaps):
            raise ValueError("blocking requires a gap finding with gaps")
        if self.analysis_round == 1 and self.revises_finding_id is not None:
            raise ValueError("round 1 cannot set revises_finding_id")
        if self.agent_name == FindingActor.INVESTIGATOR:
            if self.runtime_run_id is None or self.task_id is None:
                raise ValueError("V11 Investigator finding requires run and task ownership")
            if self.agent_instance_id is None:
                raise ValueError("V11 Investigator finding requires agent_instance_id")
            if self.analysis_round == 2 and self.critic_assessment_id is None:
                raise ValueError("V11 round 2 requires critic_assessment_id")
        elif self.analysis_round == 2 and self.revises_finding_id is None:
            raise ValueError("round 2 requires revises_finding_id")
        return self


class CausalCheck(BaseModel):
    name: CausalCheckName
    status: CausalCheckStatus
    summary: str = Field(min_length=1, max_length=256)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    gap: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_check_contract(self) -> "CausalCheck":
        if self.status in {CausalCheckStatus.PASS, CausalCheckStatus.FAIL}:
            if not self.evidence_ids:
                raise ValueError("pass and fail checks require evidence_ids")
            if self.gap is not None:
                raise ValueError("pass and fail checks cannot carry a gap")
        elif not self.gap:
            raise ValueError("unknown checks require a named gap")
        return self


class CriticAssessment(BaseModel):
    id: str = Field(default_factory=lambda: f"assessment-{uuid4().hex}")
    candidate_id: str
    verdict: CriticVerdict
    checks: list[CausalCheck] = Field(min_length=7, max_length=7)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    gap: str | None = Field(default=None, max_length=256)
    supplemental_task_ids: list[str] = Field(default_factory=list, max_length=3)
    summary: str = Field(min_length=1, max_length=512)
    runtime_run_id: str | None = None
    review_round: Literal[1, 2] = 1

    @model_validator(mode="after")
    def validate_assessment_contract(self) -> "CriticAssessment":
        names = [check.name for check in self.checks]
        if len(set(names)) != 7 or set(names) != set(CausalCheckName):
            raise ValueError("Critic assessment requires exactly seven named checks")
        if self.verdict == CriticVerdict.NEEDS_EVIDENCE:
            if not self.gap or not self.supplemental_task_ids:
                raise ValueError("needs_evidence requires a gap and supplemental tasks")
            if self.review_round == 2:
                raise ValueError("Critic reconciliation cannot request more evidence")
        elif self.gap or self.supplemental_task_ids:
            raise ValueError("supplemental work is only valid for needs_evidence")
        return self


class RootCauseCandidate(BaseModel):
    id: str = Field(default_factory=lambda: f"candidate-{uuid4().hex}")
    cause_type: CauseType | None = None
    affected_entity: str | None = Field(default=None, max_length=128)
    # 结构化分类与可读机制分开，避免评分/下游分类被一段解释性文本污染。
    failure_class: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=512)
    summary: str = Field(min_length=1)
    rank: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    supporting_finding_ids: list[str] = Field(default_factory=list)
    contradicting_finding_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    uncertainty: str = ""
    onset_window_start: datetime | None = None
    onset_window_end: datetime | None = None

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        for field_name in (
            "affected_entity",
            "failure_class",
            "failure_mechanism",
            "onset_window_start",
            "onset_window_end",
        ):
            if field_name not in self.model_fields_set:
                data.pop(field_name, None)
        return data

    @model_validator(mode="after")
    def validate_onset_window(self) -> "RootCauseCandidate":
        if (
            self.onset_window_start is not None
            and self.onset_window_end is not None
            and self.onset_window_start > self.onset_window_end
        ):
            raise ValueError("onset window start must not be later than end")
        return self


class RootCauseAttribution(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    root_cause_occurred_at: datetime
    root_cause_component: str = Field(min_length=1)
    root_cause_reason: str = Field(min_length=1)
    supporting_evidence_ids: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timestamp(self) -> "RootCauseAttribution":
        if self.root_cause_occurred_at.tzinfo is None:
            raise ValueError("root_cause_occurred_at must be timezone-aware")
        return self


class FinalDiagnosisDecision(BaseModel):
    """Agent 给出的发布顺序；服务端只校验引用与归属。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: Literal["critic", "lead", "single"]
    action: Literal["conclude", "inconclusive"]
    candidate_ids: list[str] = Field(default_factory=list, max_length=32)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    summary: str = Field(min_length=1, max_length=512)
    stop_reason: str | None = Field(default=None, max_length=256)
    uncertainty: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_decision(self) -> "FinalDiagnosisDecision":
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("final decision candidate IDs must be unique")
        if self.action == "conclude" and (not self.candidate_ids or not self.evidence_ids):
            raise ValueError("final conclusion requires candidates and evidence")
        if self.action == "inconclusive" and (self.candidate_ids or not self.stop_reason):
            raise ValueError("inconclusive requires no candidates and a stop reason")
        return self

    def as_lead_decision(self) -> LeadDecision:
        """仅为历史消费者投影相同内容，不产生第二次决策。"""
        return LeadDecision(**self.model_dump(exclude={"actor", "uncertainty"}))


class CoordinationReview(BaseModel):
    model_config = ConfigDict(protected_namespaces=("model_validate", "model_dump"))

    id: str = Field(default_factory=lambda: f"coordination-{uuid4().hex}")
    investigation_id: str
    candidates: list[RootCauseCandidate] = Field(default_factory=list)
    root_causes: list[RootCauseAttribution] = Field(default_factory=list)
    critic_assessments: list[CriticAssessment] = Field(default_factory=list)
    lead_decision: "LeadDecision | None" = None
    final_decision: FinalDiagnosisDecision | None = None
    diagnosis_contract_revision: Literal[1, 2] = 1
    diagnostic_status: DiagnosticStatus | None = None
    stop_reason: str | None = Field(default=None, max_length=256)
    runtime_run_id: str | None = None
    authority_mode: AuthorityMode = AuthorityMode.LEGACY_DETERMINISTIC
    execution_layer: AgentExecutionLayer = AgentExecutionLayer.CUSTOM
    run_status: MultiAgentRunStatus = MultiAgentRunStatus.COMPLETED
    decision_status: CoordinationDecisionStatus | None = None
    baseline_cause_type: CauseType | None = None
    selected_cause_type: CauseType | None = None
    model_provider: ModelProvider | None = None
    model_name: str | None = None
    primary_stabilization_category: StabilizationCategory | None = None
    secondary_stabilization_categories: list[StabilizationCategory] = Field(default_factory=list)
    summary: str = ""
    uncertainty: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def sort_ranked_values(self) -> "CoordinationReview":
        # root_causes 的排序所有权在 attribution 构建层
        # （build_root_cause_attributions 的 basis cohort 排序）；本 validator 在
        # 构建与持久化 reload 时都不得重排，否则 cohort 排序对 Projector Top-N、
        # 生产 review 与 reports 不可观测（V10.1 F17）。candidates 仍按 rank
        # 排序，兼容乱序输入。
        self.candidates.sort(key=lambda candidate: candidate.rank)
        if self.authority_mode == AuthorityMode.AGENT:
            if self.diagnosis_contract_revision == 2:
                if self.diagnostic_status is not None and self.final_decision is None:
                    raise ValueError("final decision is required for revision 2")
                if self.final_decision is not None and self.lead_decision is not None:
                    if self.final_decision.as_lead_decision() != self.lead_decision:
                        raise ValueError("legacy decision does not match final decision")
            if self.runtime_run_id is None:
                raise ValueError("V11 CoordinationReview requires runtime_run_id")
            if (
                self.lead_decision is None
                and self.final_decision is None
                and self.diagnostic_status is not None
            ):
                raise ValueError("final V11 CoordinationReview requires lead_decision")
            if any(
                assessment.runtime_run_id != self.runtime_run_id
                for assessment in self.critic_assessments
            ):
                raise ValueError("V11 Critic assessment owner mismatch")
            candidate_ids = {candidate.id for candidate in self.candidates}
            assessment_candidates = {
                assessment.candidate_id for assessment in self.critic_assessments
            }
            if not assessment_candidates <= candidate_ids:
                raise ValueError("Critic assessment references an unknown candidate")
            decision = self.final_decision or self.lead_decision
            if decision is None:
                return self
            if not set(decision.candidate_ids) <= candidate_ids:
                raise ValueError("Lead decision references an unknown candidate")
            accepted = {
                assessment.candidate_id
                for assessment in self.critic_assessments
                if assessment.verdict == CriticVerdict.ACCEPT
            }
            if (
                decision.action == "conclude"
                and not set(decision.candidate_ids) <= accepted
                and not (self.final_decision and self.final_decision.actor == "single")
            ):
                raise ValueError("Lead can conclude only with Critic-accepted candidates")
            if self.final_decision and any(
                check.status == CausalCheckStatus.FAIL
                for assessment in self.critic_assessments
                if assessment.candidate_id in decision.candidate_ids
                for check in assessment.checks
            ):
                raise ValueError("published candidate has a failed causal check")
        return self

    @property
    def authoritative_candidate_ids(self) -> list[str]:
        decision = self.final_decision or self.lead_decision
        if self.authority_mode != AuthorityMode.AGENT or decision is None:
            return []
        return list(decision.candidate_ids)

    @property
    def authoritative_candidates(self) -> list[RootCauseCandidate]:
        """按最终发布顺序读取候选，不使用展示 rank。"""
        by_id = {candidate.id: candidate for candidate in self.candidates}
        return [by_id[candidate_id] for candidate_id in self.authoritative_candidate_ids]

    @model_serializer(mode="wrap")
    def serialize_legacy_payload(self, handler):
        data = handler(self)
        for field_name in ("final_decision", "diagnosis_contract_revision"):
            if field_name not in self.model_fields_set:
                data.pop(field_name, None)
        if self.runtime_run_id is None:
            for field_name in (
                "critic_assessments",
                "lead_decision",
                "diagnostic_status",
                "stop_reason",
                "runtime_run_id",
                "authority_mode",
            ):
                if field_name not in self.model_fields_set:
                    data.pop(field_name, None)
        return data
