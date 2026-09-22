from __future__ import annotations

import re
from collections.abc import Iterable

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
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
    MultiAgentRunStatus,
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


def partial_candidate_evidence_error(supporting_ids, usable_evidence) -> str | None:
    """供 Critic 与发布校验共用 partial 的机械证据要求；不判断因果关系。"""
    supporting = set(supporting_ids)
    if len(supporting) < 2:
        return "partial_candidate_evidence"
    if not supporting <= usable_evidence.keys():
        return "candidate_evidence_reference"
    # Provider 种类不是因果支持强度；同一来源的多条证据仍由 Critic 审查相关性。
    return None


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
    committed_evidence = {
        item.id for item in evidence_items if item.runtime_run_id == runtime_run_id
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
        # 与准入层 _finding_from_draft 同一契约（spec §7.2）：GAP 的语义是
        # 证据缺失，可引用同 run 已提交的任何状态证据；非 gap 仍限 usable，
        # 编造/跨 run ID 在两条路径均硬拒绝。
        _require_committed_refs(
            [*finding.evidence_ids, *finding.contradicting_evidence_ids],
            committed_evidence if finding.finding_type == AgentFindingType.GAP else usable_evidence,
            "finding_evidence_reference",
        )
        if finding.analysis_round == 2 and (
            finding.agent_name == FindingActor.INVESTIGATOR and finding.critic_assessment_id is None
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
        # GAP 断言的是"该 entity 证据缺失"，被引用 skipped/failed 证据的 scope
        # 不覆盖 affected_entity 不改变其语义（spec §8.2 GAP 豁免）。
        if finding.finding_type != AgentFindingType.GAP:
            _validate_scope_consistency(
                finding.affected_entity, finding.evidence_ids, evidence_by_id
            )

    candidate_ids = [candidate.id for candidate in candidate_items]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise V11ResultValidationError("duplicate_candidate_id")
    ranks = [candidate.rank for candidate in candidate_items]
    if len(set(ranks)) != len(ranks) or sorted(ranks) != list(range(1, len(ranks) + 1)):
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
        _require_committed_refs(
            call.output_evidence_ids, committed_evidence, "tool_evidence_reference"
        )
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
    if {candidate.id for candidate in review.candidates} != set(candidate_ids):
        raise V11ResultValidationError("review_candidate_projection")
    if review.run_status == MultiAgentRunStatus.FAILED and not review.critic_assessments:
        # 失败运行保留已准入候选作为审计证据；它们不具备发布资格。
        return
    try:
        CoordinationReview.model_validate(review.model_dump())
    except ValueError as exc:
        raise V11ResultValidationError("final_decision_contract") from exc
    _validate_assessments(review, set(candidate_ids), usable_evidence, runtime_run_id)
    _validate_supplemental_tasks(review, task_items, runtime_run_id)
    if review.final_decision is None and review.lead_decision is None:
        if status in {DiagnosticStatus.COMPLETE, DiagnosticStatus.PARTIAL}:
            raise V11ResultValidationError("final_lead_required")
        return

    if review.final_decision is not None:
        decision = review.final_decision
        if decision.actor != "critic" and not (
            decision.actor == "lead"
            and decision.action == "inconclusive"
            and not review.candidates
            and not review.critic_assessments
        ):
            raise V11ResultValidationError("final_decision_actor")
    decision = review.final_decision or review.lead_decision
    uncertainty = getattr(decision, "uncertainty", None)
    if uncertainty and decision.action == "conclude" and status != DiagnosticStatus.PARTIAL:
        raise V11ResultValidationError("tentative_conclusion_requires_partial")
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
    if review.final_decision and decision.action == LeadAction.CONCLUDE and not uncertainty:
        if any(
            check.status == CausalCheckStatus.UNKNOWN
            and check.name in {CausalCheckName.MECHANISM, CausalCheckName.SYMPTOM_VS_CAUSE}
            for assessment in review.critic_assessments
            if assessment.candidate_id in decision.candidate_ids
            for check in assessment.checks
        ):
            raise V11ResultValidationError("final_candidate_unknown_mechanism")
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
        # 暂定或执行不完整的结论仍要求无 FAIL 检查和两条有效支撑证据。
        # 数量或 Provider 种类不能证明独立性；因果支持由 Critic 审查。
        if not usable_evidence:
            raise V11ResultValidationError("partial_usable_evidence_required")
        usable_items = {item_id: evidence_by_id[item_id] for item_id in usable_evidence}
        assessments_by_candidate = {item.candidate_id: item for item in review.critic_assessments}
        candidates_by_id = {item.id: item for item in candidate_items}
        for candidate_id in decision.candidate_ids:
            assessment = assessments_by_candidate[candidate_id]
            if any(check.status == CausalCheckStatus.FAIL for check in assessment.checks):
                raise V11ResultValidationError("partial_candidate_failed_check")
            error = partial_candidate_evidence_error(
                candidates_by_id[candidate_id].supporting_evidence_ids, usable_items
            )
            if error:
                raise V11ResultValidationError(error)
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
            item.analysis_round == final_round or (final_round == 1 and item.analysis_round is None)
        )
        for item in executions
    ):
        raise V11ResultValidationError("final_critic_execution_required")
    if not any(
        item.status == AgentExecutionStatus.COMPLETED
        and item.agent_name == ExecutionActor.LEAD.value
        and item.step_kind == ExecutionStepKind.LEAD_ADJUDICATION
        and (
            item.analysis_round == final_round or (final_round == 1 and item.analysis_round is None)
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
    if review.run_status == MultiAgentRunStatus.FAILED:
        # 终态失败时 review 投影会清空 candidates/assessments，但任务仍作为
        # 失败审计保留；失败记录不是可发布诊断，不应因其清空后的引用关系
        # 被误报成 orphan supplemental task。
        return
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


def _require_committed_refs(references: Iterable[str], usable_ids: set[str], code: str) -> None:
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
    if scoped_entities and _normalize_entity(entity) not in {
        _normalize_entity(item) for item in scoped_entities
    } and not _validate_evidence_path_entity(entity, evidence_ids, evidence_by_id):
        raise V11ResultValidationError("scope_entity_mismatch")


_ENTITY_PATH_SPLIT = re.compile(r"\s*(?:->|=>|→)\s*")


def _normalize_entity(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _validate_evidence_path_entity(
    entity: str,
    evidence_ids: Iterable[str],
    evidence_by_id: dict[str, EvidenceItem],
) -> bool:
    """验证候选实体是否是引用证据明确表达的有向调用/依赖路径。

    scope.entity_ids 只描述证据覆盖范围，不能单独证明任意两个实体存在调用关系。
    因此路径的每一条相邻边必须由同一批引用证据的结构化 payload 明确给出。
    """
    parts = [_normalize_entity(item) for item in _ENTITY_PATH_SPLIT.split(entity)]
    if len(parts) < 2 or any(not item for item in parts):
        return False
    relationships: set[tuple[str, str]] = set()

    def add_pair(source, target) -> None:
        if isinstance(source, str) and isinstance(target, str):
            left, right = _normalize_entity(source), _normalize_entity(target)
            if left and right and left != right:
                relationships.add((left, right))

    for evidence_id in evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            continue
        payload = item.payload if isinstance(item.payload, dict) else {}
        # 依赖证据可能直接给出一条边，也可能给出 edges 列表。
        add_pair(payload.get("source"), payload.get("target"))
        for edge in payload.get("edges", ()):
            if isinstance(edge, dict):
                add_pair(
                    edge.get("parent", edge.get("source")),
                    edge.get("child", edge.get("target")),
                )

        # Trace 证据给出本地服务及远端 peer，统一保留客户端/源端到被调用端的方向。
        # 服务端 span 的 service 是被调用端，不能把它反向当作 source。
        service = payload.get("service")
        attributes = payload.get("attributes")
        if isinstance(attributes, dict):
            peer_service = attributes.get("rpc.peer_service")
            peer_role = attributes.get("rpc.peer_role", payload.get("rpc.peer_role"))
            if isinstance(peer_role, str) and peer_role.casefold() == "callee":
                add_pair(peer_service, service)
            else:
                add_pair(service, peer_service)
        child_timing = payload.get("child_timing")
        if isinstance(child_timing, dict):
            add_pair(service, child_timing.get("longest_child_peer_service"))
        add_pair(payload.get("source_service"), service)

    return all(pair in relationships for pair in zip(parts, parts[1:], strict=False))


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
        decision = review.final_decision or review.lead_decision
        if decision is not None:
            values.extend(
                [
                    decision.summary,
                    decision.stop_reason or "",
                    getattr(decision, "uncertainty", None) or "",
                ]
            )
    for execution in executions:
        values.extend([execution.summary or "", execution.error_message or ""])
    if any(_CONTROL_CHARACTER.search(value) for value in values):
        raise V11ResultValidationError("unsafe_text")
