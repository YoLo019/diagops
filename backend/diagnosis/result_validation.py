from __future__ import annotations

import re
from collections.abc import Iterable

from backend.domain.agent_findings import (
    AgentFinding,
    CoordinationReview,
    FindingActor,
)
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisTask,
)
from backend.domain.evidence import EvidenceItem, EvidenceStatus
from backend.domain.multi_agent import (
    AuthorityMode,
    CausalCheckName,
    CausalCheckStatus,
    CriticVerdict,
    DiagnosticStatus,
    ExecutionActor,
    ExecutionStepKind,
    LeadAction,
    ResultValidationCategory,
)
from backend.domain.tool_calls import ToolCallRecord


class AgentResultValidationError(ValueError):
    """仅携带固定合同边界，禁止附加模型输出或动态异常文本。"""

    def __init__(self, category: ResultValidationCategory) -> None:
        self.category = category
        super().__init__(category.value)


class V11ResultValidationError(ValueError):
    """V11 结果的机械引用/归属合同错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def validate_v11_result(
    *,
    investigation_id: str,
    runtime_run_id: str,
    findings: Iterable[AgentFinding],
    candidates,
    review: CoordinationReview | None,
    executions: Iterable[AgentExecution] = (),
    evidence: Iterable[EvidenceItem] = (),
    tasks: Iterable[DiagnosisTask] = (),
    tool_calls: Iterable[ToolCallRecord] = (),
    tool_registry=None,
    agent_manifest: tuple[str, ...] | None = None,
    status: DiagnosticStatus | None = None,
) -> None:
    """只校验 V11 的机械契约，不解释因果语义。

    这个校验器只读取 Agent 输出。它不会排序或改写 rank、entity、mechanism、
    evidence、counterevidence、onset，也不会调用 V10 的 CauseType/provider 语义校验。
    """
    finding_items = tuple(findings)
    candidate_items = tuple(candidates)
    execution_items = tuple(executions)
    evidence_items = tuple(evidence)
    task_items = tuple(tasks)
    tool_call_items = tuple(tool_calls)
    evidence_by_id = {item.id: item for item in evidence_items}
    if len(evidence_by_id) != len(evidence_items):
        raise V11ResultValidationError("duplicate_evidence_id")
    usable_evidence = {
        item.id
        for item in evidence_items
        if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
        and item.runtime_run_id == runtime_run_id
    }

    _validate_safe_texts(finding_items, candidate_items, review, execution_items)
    finding_ids = {item.id for item in finding_items}
    if len(finding_ids) != len(finding_items):
        raise V11ResultValidationError("duplicate_finding_id")
    for finding in finding_items:
        if finding.investigation_id != investigation_id:
            raise V11ResultValidationError("finding_investigation_owner")
        if finding.agent_name == FindingActor.INVESTIGATOR and (
            finding.runtime_run_id != runtime_run_id
            or finding.task_id is None
            or finding.agent_instance_id is None
        ):
            raise V11ResultValidationError("finding_runtime_owner")
        if finding.runtime_run_id not in {None, runtime_run_id}:
            raise V11ResultValidationError("finding_runtime_owner")
        _require_committed_refs(
            [*finding.evidence_ids, *finding.contradicting_evidence_ids],
            usable_evidence,
            "finding_evidence_reference",
        )
        if finding.analysis_round == 2 and (
            finding.agent_name == FindingActor.INVESTIGATOR
            and finding.critic_assessment_id is None
        ):
            raise V11ResultValidationError("round_two_assessment_reference")
        if finding.agent_name == FindingActor.INVESTIGATOR:
            task = next((item for item in task_items if item.id == finding.task_id), None)
            if task is not None and (
                task.runtime_run_id != runtime_run_id
                or task.analysis_round != finding.analysis_round
            ):
                raise V11ResultValidationError("finding_task_contract")
            if task is not None and finding.analysis_round == 2:
                if task.critic_assessment_id != finding.critic_assessment_id:
                    raise V11ResultValidationError("finding_assessment_contract")
        _validate_scope_consistency(finding.affected_entity, finding.evidence_ids, evidence_by_id)

    candidate_ids = [candidate.id for candidate in candidate_items]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise V11ResultValidationError("duplicate_candidate_id")
    ranks = [candidate.rank for candidate in candidate_items]
    if len(set(ranks)) != len(ranks) or sorted(ranks) != list(
        range(1, len(ranks) + 1)
    ):
        raise V11ResultValidationError("candidate_rank")
    for candidate in candidate_items:
        _require_committed_refs(
            [
                *candidate.supporting_evidence_ids,
                *candidate.contradicting_evidence_ids,
            ],
            usable_evidence,
            "candidate_evidence_reference",
        )
        if not set(candidate.supporting_finding_ids) <= finding_ids:
            raise V11ResultValidationError("candidate_finding_reference")
        if not set(candidate.contradicting_finding_ids) <= finding_ids:
            raise V11ResultValidationError("candidate_finding_reference")
        _validate_scope_consistency(
            candidate.affected_entity,
            candidate.supporting_evidence_ids,
            evidence_by_id,
        )

    for execution in execution_items:
        if execution.runtime_run_id != runtime_run_id:
            raise V11ResultValidationError("execution_runtime_owner")
    for call in tool_call_items:
        if call.runtime_run_id != runtime_run_id:
            raise V11ResultValidationError("tool_runtime_owner")
        if agent_manifest is not None and call.tool_name not in agent_manifest:
            raise V11ResultValidationError("tool_manifest_reference")
        if tool_registry is not None and agent_manifest is not None:
            try:
                tool_registry.assert_agent_callable(call.tool_name, agent_manifest)
            except ValueError as exc:
                raise V11ResultValidationError("tool_permission") from exc

    if review is None:
        if status in {DiagnosticStatus.COMPLETE, DiagnosticStatus.PARTIAL}:
            raise V11ResultValidationError("final_review_required")
        return
    if review.investigation_id != investigation_id:
        raise V11ResultValidationError("review_investigation_owner")
    if review.authority_mode != AuthorityMode.AGENT:
        raise V11ResultValidationError("review_authority")
    if review.runtime_run_id != runtime_run_id:
        raise V11ResultValidationError("review_runtime_owner")
    if (
        review.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
        or status == DiagnosticStatus.INCONCLUSIVE
    ) and review.candidates:
        raise V11ResultValidationError("inconclusive_review_candidates")
    if {candidate.id for candidate in review.candidates} != set(candidate_ids):
        raise V11ResultValidationError("review_candidate_projection")
    _validate_assessments(review, set(candidate_ids), usable_evidence, runtime_run_id)
    _validate_supplemental_tasks(review, task_items, runtime_run_id)
    if review.lead_decision is None:
        if status in {DiagnosticStatus.COMPLETE, DiagnosticStatus.PARTIAL}:
            raise V11ResultValidationError("final_lead_required")
        return

    decision = review.lead_decision
    _require_committed_refs(
        decision.evidence_ids,
        usable_evidence,
        "lead_evidence_reference",
    )
    if not set(decision.candidate_ids) <= set(candidate_ids):
        raise V11ResultValidationError("lead_candidate_reference")
    accepted = {
        item.candidate_id
        for item in review.critic_assessments
        if item.verdict == CriticVerdict.ACCEPT
    }
    if decision.action == LeadAction.CONCLUDE and not set(decision.candidate_ids) <= accepted:
        raise V11ResultValidationError("lead_accepted_candidate_reference")
    if decision.action == LeadAction.INCONCLUSIVE and decision.candidate_ids:
        raise V11ResultValidationError("inconclusive_candidates")
    if status in {DiagnosticStatus.COMPLETE, DiagnosticStatus.PARTIAL}:
        if not review.critic_assessments:
            raise V11ResultValidationError("final_critic_required")
        if decision.action != LeadAction.CONCLUDE or not decision.candidate_ids:
            raise V11ResultValidationError("final_conclusion_required")
        _validate_final_execution_coverage(
            execution_items,
            final_round=2
            if any(
                task.analysis_round == 2 and task.runtime_run_id == runtime_run_id
                for task in task_items
            )
            or any(item.review_round == 2 for item in review.critic_assessments)
            else 1,
        )
    if status == DiagnosticStatus.PARTIAL:
        # spec §9.2：被采纳候选必须满足——无 FAIL causal check、至少两条独立
        # 支撑证据，且当两种 Provider 均成功时覆盖两种 Provider 类型。
        # Critic accept 已由 lead_accepted_candidate_reference 强制。
        if not usable_evidence:
            raise V11ResultValidationError("partial_usable_evidence_required")
        successful_providers = {
            evidence_by_id[evidence_id].provider for evidence_id in usable_evidence
        }
        assessments_by_candidate = {
            item.candidate_id: item for item in review.critic_assessments
        }
        candidates_by_id = {item.id: item for item in candidate_items}
        for candidate_id in decision.candidate_ids:
            assessment = assessments_by_candidate[candidate_id]
            if any(
                check.status == CausalCheckStatus.FAIL for check in assessment.checks
            ):
                raise V11ResultValidationError("partial_candidate_failed_check")
            supporting = set(candidates_by_id[candidate_id].supporting_evidence_ids)
            if len(supporting) < 2:
                raise V11ResultValidationError("partial_candidate_evidence")
            if len(successful_providers) >= 2 and len(
                {evidence_by_id[item].provider for item in supporting}
            ) < 2:
                raise V11ResultValidationError("partial_candidate_provider_types")
    if status == DiagnosticStatus.INCONCLUSIVE and decision.candidate_ids:
        raise V11ResultValidationError("inconclusive_candidates")


def _validate_final_execution_coverage(
    executions: tuple[AgentExecution, ...],
    *,
    final_round: int,
) -> None:
    if not any(
        item.status == AgentExecutionStatus.COMPLETED
        and item.agent_name == ExecutionActor.CRITIC.value
        and item.step_kind == ExecutionStepKind.CRITIC_REVIEW
        and (
            item.analysis_round == final_round
            or (final_round == 1 and item.analysis_round is None)
        )
        for item in executions
    ):
        raise V11ResultValidationError("final_critic_execution_required")
    if not any(
        item.status == AgentExecutionStatus.COMPLETED
        and item.agent_name == ExecutionActor.LEAD.value
        and item.step_kind == ExecutionStepKind.LEAD_ADJUDICATION
        and (
            item.analysis_round == final_round
            or (final_round == 1 and item.analysis_round is None)
        )
        for item in executions
    ):
        raise V11ResultValidationError("final_lead_execution_required")

def _validate_assessments(
    review: CoordinationReview,
    candidate_ids: set[str],
    usable_evidence: set[str],
    runtime_run_id: str,
) -> None:
    assessments = review.critic_assessments
    assessment_ids = [item.id for item in assessments]
    if len(set(assessment_ids)) != len(assessment_ids):
        raise V11ResultValidationError("duplicate_assessment_id")
    if len(assessments) != len(candidate_ids):
        raise V11ResultValidationError("assessment_candidate_cardinality")
    assessment_candidate_ids = [item.candidate_id for item in assessments]
    if set(assessment_candidate_ids) != candidate_ids:
        raise V11ResultValidationError("assessment_candidate_coverage")
    for assessment in assessments:
        if assessment.runtime_run_id != runtime_run_id:
            raise V11ResultValidationError("assessment_runtime_owner")
        if assessment.candidate_id not in candidate_ids:
            raise V11ResultValidationError("assessment_candidate_reference")
        _require_committed_refs(
            [
                *assessment.supporting_evidence_ids,
                *assessment.contradicting_evidence_ids,
            ],
            usable_evidence,
            "assessment_evidence_reference",
        )
        if len(assessment.checks) != 7:
            raise V11ResultValidationError("assessment_check_count")
        if {check.name for check in assessment.checks} != set(CausalCheckName):
            raise V11ResultValidationError("assessment_check_names")
        for check in assessment.checks:
            _require_committed_refs(
                check.evidence_ids,
                usable_evidence,
                "assessment_evidence_reference",
            )
        if assessment.verdict == CriticVerdict.NEEDS_EVIDENCE:
            if assessment.review_round == 2:
                raise V11ResultValidationError("reconciliation_requested_evidence")
            if not assessment.supplemental_task_ids:
                raise V11ResultValidationError("missing_supplemental_task")
        elif assessment.supplemental_task_ids:
            raise V11ResultValidationError("unexpected_supplemental_task")


def _validate_supplemental_tasks(
    review: CoordinationReview,
    tasks: tuple[DiagnosisTask, ...],
    runtime_run_id: str,
) -> None:
    """把 Critic 声明与同一 assessment 的持久化 round2 task 精确对齐。"""
    assessment_ids = {assessment.id for assessment in review.critic_assessments}
    linked: dict[str, set[str]] = {assessment_id: set() for assessment_id in assessment_ids}
    for task in tasks:
        if task.runtime_run_id != runtime_run_id or task.analysis_round != 2:
            continue
        if task.critic_assessment_id not in assessment_ids:
            raise V11ResultValidationError("orphan_supplemental_task")
        linked[task.critic_assessment_id].add(task.id)
    for assessment in review.critic_assessments:
        declared = set(assessment.supplemental_task_ids)
        persisted = linked[assessment.id]
        if assessment.verdict == CriticVerdict.NEEDS_EVIDENCE:
            if declared != persisted:
                raise V11ResultValidationError("supplemental_task_linkage")
        elif persisted and assessment.review_round != 2:
            raise V11ResultValidationError("unexpected_supplemental_task")


def _require_committed_refs(
    references: Iterable[str], usable_ids: set[str], code: str
) -> None:
    if not set(references) <= usable_ids:
        raise V11ResultValidationError(code)


def _validate_scope_consistency(
    entity: str | None,
    evidence_ids: Iterable[str],
    evidence_by_id: dict[str, EvidenceItem],
) -> None:
    if entity is None:
        return
    scoped_entities = {
        scoped_entity
        for evidence_id in evidence_ids
        for scoped_entity in (
            evidence_by_id[evidence_id].scope.entity_ids
            if evidence_by_id[evidence_id].scope is not None
            else []
        )
    }
    if scoped_entities and entity not in scoped_entities:
        raise V11ResultValidationError("scope_entity_mismatch")


def _validate_safe_texts(
    findings: Iterable[AgentFinding],
    candidates,
    review: CoordinationReview | None,
    executions: Iterable[AgentExecution],
) -> None:
    values: list[str] = []
    for finding in findings:
        values.extend([finding.summary, finding.rationale, *finding.gaps])
    for candidate in candidates:
        values.extend(
            [
                candidate.summary,
                candidate.rationale,
                candidate.uncertainty,
                *(
                    item
                    for item in (
                        candidate.affected_entity,
                        candidate.failure_mechanism,
                    )
                    if item
                ),
            ]
        )
    if review is not None:
        values.extend([review.summary, review.uncertainty, review.stop_reason or ""])
        for assessment in review.critic_assessments:
            values.extend([assessment.summary, assessment.gap or ""])
            for check in assessment.checks:
                values.extend([check.summary, check.gap or ""])
        if review.lead_decision is not None:
            values.extend(
                [
                    review.lead_decision.summary,
                    review.lead_decision.stop_reason or "",
                ]
            )
    for execution in executions:
        values.extend([execution.summary or "", execution.error_message or ""])
    if any(_CONTROL_CHARACTER.search(value) for value in values):
        raise V11ResultValidationError("unsafe_text")
