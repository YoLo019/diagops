from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from threading import RLock

from backend.db.models import (
    InvestigationRecord,
    InvestigationStatus,
    InvestigationSummary,
)
from backend.domain.actions import ActionStatus, VerificationStatus
from backend.domain.agent_context import ContextFact
from backend.domain.agent_findings import AgentFinding, CoordinationReview, FindingActor
from backend.domain.agent_plan import AgentExecution, DiagnosisPlan, DiagnosisTask
from backend.domain.human_transitions import (
    validate_action_transition,
    validate_verification_transition,
)
from backend.domain.hypotheses import CauseType
from backend.domain.memory import MemoryItem
from backend.domain.multi_agent import AgentExecutionLayer
from backend.domain.react_trace import ReActTrace
from backend.domain.tool_calls import ToolCallRecord


def _validate_investigation_record(record: InvestigationRecord) -> InvestigationRecord:
    """在写入前重建聚合，避免 model_copy 绕过嵌套 V11 owner 校验。"""
    return InvestigationRecord.model_validate(record.model_dump(mode="python"))


def _validate_plan_and_tasks(
    investigation_id: str,
    *,
    plan: DiagnosisPlan | None = None,
    tasks: Sequence[DiagnosisTask] = (),
) -> tuple[DiagnosisPlan | None, list[DiagnosisTask]]:
    """在替换计划投影前重建模型并拒绝混合 runtime owner。"""
    validated_plan = (
        None
        if plan is None
        else DiagnosisPlan.model_validate(plan.model_dump(mode="python"))
    )
    if validated_plan is not None and validated_plan.investigation_id != investigation_id:
        raise ValueError("Diagnosis plan investigation mismatch")
    validated_tasks = [
        DiagnosisTask.model_validate(item.model_dump(mode="python")) for item in tasks
    ]
    stored_tasks = (
        list(validated_plan.tasks) if validated_plan is not None else validated_tasks
    )
    owners = {
        item.runtime_run_id
        for item in stored_tasks
        if item.runtime_run_id is not None
    }
    if validated_plan is not None and validated_plan.runtime_run_id is not None:
        owners.add(validated_plan.runtime_run_id)
    if len(owners) > 1:
        raise ValueError("V11 plan and tasks mix runtime owners")
    return validated_plan, stored_tasks


def _validate_multi_agent_result(
    investigation_id: str,
    findings: list[AgentFinding],
    executions: list[AgentExecution],
    review: CoordinationReview | None,
) -> tuple[list[AgentFinding], list[AgentExecution], CoordinationReview | None]:
    validated_findings = [
        AgentFinding.model_validate(item.model_dump(mode="python"))
        for item in findings
    ]
    validated_executions = [
        AgentExecution.model_validate(item.model_dump(mode="python"))
        for item in executions
    ]
    validated_review = (
        None
        if review is None
        else CoordinationReview.model_validate(review.model_dump(mode="python"))
    )
    if any(item.investigation_id != investigation_id for item in validated_findings):
        raise ValueError("Agent finding investigation mismatch")
    if (
        validated_review is not None
        and validated_review.investigation_id != investigation_id
    ):
        raise ValueError("Coordination review investigation mismatch")
    owners = {
        item.runtime_run_id
        for item in validated_findings
        if item.runtime_run_id is not None
    }
    owners.update(
        item.runtime_run_id
        for item in validated_executions
        if item.runtime_run_id is not None
    )
    if validated_review is not None and validated_review.runtime_run_id is not None:
        owners.add(validated_review.runtime_run_id)
    if validated_review is not None:
        owners.update(
            assessment.runtime_run_id
            for assessment in validated_review.critic_assessments
            if assessment.runtime_run_id is not None
        )
    if len(owners) > 1:
        raise ValueError("V11 aggregate mixes runtime owners")
    finding_by_id = {item.id: item for item in validated_findings}
    for finding in validated_findings:
        if finding.analysis_round != 2 or finding.agent_name == FindingActor.INVESTIGATOR:
            continue
        previous = finding_by_id.get(finding.revises_finding_id or "")
        if previous is None or previous.agent_name != finding.agent_name:
            raise ValueError("legacy round-two finding must revise the same actor")
    if validated_review is not None and validated_review.authority_mode.value == "agent":
        if validated_review.runtime_run_id is None:
            raise ValueError("V11 review requires runtime_run_id")
        if any(
            item.runtime_run_id != validated_review.runtime_run_id
            for item in validated_findings + validated_executions
        ):
            raise ValueError("V11 aggregate owner mismatch")
        if any(
            assessment.runtime_run_id != validated_review.runtime_run_id
            for assessment in validated_review.critic_assessments
        ):
            raise ValueError("V11 aggregate owner mismatch")
    return validated_findings, validated_executions, validated_review


def _validate_investigator_finding_linkage(
    finding: AgentFinding,
    tasks: dict[str, DiagnosisTask],
    review: CoordinationReview | None,
) -> None:
    """校验 Investigator finding 与任务、Critic 请求的同轮归属。"""
    if finding.agent_name != FindingActor.INVESTIGATOR:
        return
    task = tasks.get(finding.task_id or "")
    if task is None or task.runtime_run_id != finding.runtime_run_id:
        raise ValueError("V11 finding task ownership mismatch")
    if task.analysis_round != finding.analysis_round:
        raise ValueError("V11 finding task round mismatch")
    if finding.analysis_round != 2:
        return
    assessment = next(
        (
            item
            for item in (review.critic_assessments if review is not None else [])
            if item.id == finding.critic_assessment_id
        ),
        None,
    )
    if assessment is None:
        raise ValueError("V11 finding Critic assessment ownership mismatch")
    if assessment.runtime_run_id != finding.runtime_run_id:
        raise ValueError("V11 finding Critic assessment owner mismatch")
    if task.critic_assessment_id != assessment.id:
        raise ValueError("V11 finding task Critic assessment mismatch")
    if task.id not in assessment.supplemental_task_ids:
        raise ValueError("V11 finding task is not linked to Critic request")


class InMemoryInvestigationRepository:
    def __init__(self, lock=None) -> None:
        self._lock = lock or RLock()
        self._records: dict[str, InvestigationRecord] = {}
        self._plans: dict[str, DiagnosisPlan] = {}
        self._tasks: dict[str, list[DiagnosisTask]] = {}
        self._executions: dict[str, dict[str, AgentExecution]] = {}
        self._context_facts: dict[str, dict[str, ContextFact]] = {}
        self._tool_calls: dict[str, dict[str, ToolCallRecord]] = {}
        self._memory_items: dict[str, MemoryItem] = {}
        self._agent_findings: dict[str, dict[str, AgentFinding]] = {}
        self._coordination_reviews: dict[str, CoordinationReview] = {}
        self._react_traces: dict[str, ReActTrace] = {}

    @property
    def transaction_lock(self):
        """返回 Runtime 原子提交复用的可重入锁。"""
        return self._lock

    def save(self, record: InvestigationRecord) -> InvestigationRecord:
        with self._lock:
            _validate_investigation_record(record)
            self._records[record.id] = record
            return record

    def activate_projection(
        self, investigation_id: str, runtime_run_id: str
    ) -> InvestigationRecord:
        """在共享锁内切换 latest owner，并清除上一轮可变诊断投影。"""
        with self._lock:
            record = self.get(investigation_id)
            if (
                record.active_runtime_run_id is not None
                and record.active_runtime_run_id != runtime_run_id
                and record.status
                not in {
                    InvestigationStatus.COMPLETED,
                    InvestigationStatus.FAILED,
                    InvestigationStatus.CANCELLED,
                }
            ):
                raise ValueError("active projection owner is still running")
            cleared = record.model_copy(
                update={
                    "active_runtime_run_id": runtime_run_id,
                    "evidence": [],
                    "provider_results": [],
                    "specialist_results": [],
                    "hypotheses": [],
                    "report": None,
                    "llm_analysis": None,
                    "multi_agent_run": None,
                    "actions": [],
                    "verification_suggestions": [],
                }
            )
            self._records[investigation_id] = cleared
            self._plans.pop(investigation_id, None)
            self._tasks.pop(investigation_id, None)
            self._context_facts.pop(investigation_id, None)
            self._tool_calls.pop(investigation_id, None)
            self._agent_findings.pop(investigation_id, None)
            self._executions.pop(investigation_id, None)
            self._coordination_reviews.pop(investigation_id, None)
            self._react_traces.pop(investigation_id, None)
            return cleared

    def get(self, investigation_id: str) -> InvestigationRecord:
        with self._lock:
            try:
                return self._records[investigation_id]
            except KeyError as exc:
                raise ValueError(f"Unknown investigation: {investigation_id}") from exc

    def list(self) -> list[InvestigationRecord]:
        with self._lock:
            return sorted(
                self._records.values(),
                key=lambda record: record.created_at,
                reverse=True,
            )

    def list_summaries(self) -> list[InvestigationSummary]:
        with self._lock:
            return [
                InvestigationSummary.from_record(
                    record, self._coordination_reviews.get(record.id)
                )
                for record in self.list()
            ]

    def update_status(
        self,
        investigation_id: str,
        status: InvestigationStatus,
        *,
        failure_reason: str | None = None,
    ) -> InvestigationRecord:
        record = self.get(investigation_id)
        record.status = status
        record.failure_reason = failure_reason
        record.updated_at = datetime.now(UTC)
        if status == InvestigationStatus.COMPLETED:
            record.completed_at = record.updated_at
        self._records[record.id] = record
        return record

    def update_action_status(
        self,
        investigation_id: str,
        action_id: str,
        *,
        status: ActionStatus | str,
        note: str | None = None,
    ):
        with self._lock:
            record = self.get(investigation_id)
            updated = validate_action_transition(record, action_id, status, note)
            for index, action in enumerate(record.actions):
                if action.id != action_id:
                    continue
                record.actions[index] = updated
                record.updated_at = datetime.now(UTC)
                self._records[record.id] = record
                return updated
            raise ValueError(f"Unknown action: {action_id}")

    def update_verification_status(
        self,
        investigation_id: str,
        verification_id: str,
        *,
        status: VerificationStatus | str,
        result_note: str | None = None,
        result_evidence_ids: list[str] | None = None,
        related_action_ids: list[str] | None = None,
        related_cause_types: list[CauseType | str] | None = None,
        related_candidate_ids: list[str] | None = None,
    ):
        with self._lock:
            record = self.get(investigation_id)
            updated = validate_verification_transition(
                record,
                verification_id,
                status,
                result_note,
                result_evidence_ids or [],
                related_action_ids or [],
                related_cause_types or [],
                related_candidate_ids,
            )
            for index, suggestion in enumerate(record.verification_suggestions):
                if suggestion.id != verification_id:
                    continue
                record.verification_suggestions[index] = updated
                record.updated_at = datetime.now(UTC)
                self._records[record.id] = record
                return updated
            raise ValueError(f"Unknown verification suggestion: {verification_id}")

    def save_plan(self, plan: DiagnosisPlan) -> DiagnosisPlan:
        with self._lock:
            validated, tasks = _validate_plan_and_tasks(
                plan.investigation_id, plan=plan
            )
            self._plans[plan.investigation_id] = validated
            self._tasks[plan.investigation_id] = tasks
            return validated

    def get_plan(self, investigation_id: str) -> DiagnosisPlan | None:
        plan = self._plans.get(investigation_id)
        if plan is None:
            return None
        return plan.model_copy(update={"tasks": self.list_tasks(investigation_id)})

    def save_tasks(
        self,
        investigation_id: str,
        tasks: list[DiagnosisTask],
    ) -> list[DiagnosisTask]:
        with self._lock:
            _, validated = _validate_plan_and_tasks(
                investigation_id, tasks=tasks
            )
            self._tasks[investigation_id] = validated
            return validated

    def list_tasks(self, investigation_id: str) -> list[DiagnosisTask]:
        return list(self._tasks.get(investigation_id, []))

    def save_executions(
        self,
        investigation_id: str,
        executions: list[AgentExecution],
    ) -> list[AgentExecution]:
        with self._lock:
            validated = [
                AgentExecution.model_validate(item.model_dump(mode="python"))
                for item in executions
            ]
            bucket = self._executions.setdefault(investigation_id, {})
            for execution in validated:
                bucket[execution.id] = execution
            return validated

    def list_executions(self, investigation_id: str) -> list[AgentExecution]:
        return list(self._executions.get(investigation_id, {}).values())

    def save_context_facts(
        self,
        investigation_id: str,
        facts: list[ContextFact],
    ) -> list[ContextFact]:
        bucket = self._context_facts.setdefault(investigation_id, {})
        for fact in facts:
            bucket[fact.id] = fact
        return list(facts)

    def list_context_facts(self, investigation_id: str) -> list[ContextFact]:
        return list(self._context_facts.get(investigation_id, {}).values())

    def save_tool_calls(
        self,
        investigation_id: str,
        calls: list[ToolCallRecord],
    ) -> list[ToolCallRecord]:
        with self._lock:
            bucket = self._tool_calls.setdefault(investigation_id, {})
            for call in calls:
                bucket[call.id] = call
            return list(calls)

    def list_tool_calls(self, investigation_id: str) -> list[ToolCallRecord]:
        return list(self._tool_calls.get(investigation_id, {}).values())

    def save_memory_items(self, items: list[MemoryItem]) -> list[MemoryItem]:
        for item in items:
            self._memory_items[item.id] = item
        return list(items)

    def list_memory(
        self,
        service: str,
        environment: str,
        *,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        items = sorted(
            (
                item
                for item in self._memory_items.values()
                if item.service == service and item.environment == environment
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        return items if limit is None else items[:limit]

    def save_agent_findings(
        self,
        investigation_id: str,
        findings: list[AgentFinding],
    ) -> list[AgentFinding]:
        with self._lock:
            validated = [
                AgentFinding.model_validate(item.model_dump(mode="python"))
                for item in findings
            ]
            if any(item.investigation_id != investigation_id for item in validated):
                raise ValueError("Agent finding investigation mismatch")
            bucket = self._agent_findings.setdefault(investigation_id, {})
            all_findings = {**bucket, **{item.id: item for item in validated}}
            review = self.get_coordination_review(investigation_id)
            tasks = {
                task.id: task for task in self.list_tasks(investigation_id)
            }
            for finding in validated:
                if (
                    finding.analysis_round == 2
                    and finding.agent_name != FindingActor.INVESTIGATOR
                ):
                    previous = all_findings.get(finding.revises_finding_id or "")
                    if previous is None or previous.agent_name != finding.agent_name:
                        raise ValueError(
                            "legacy round-two finding must revise the same actor"
                        )
                _validate_investigator_finding_linkage(finding, tasks, review)
                if (
                    finding.agent_name == FindingActor.INVESTIGATOR
                    and review is not None
                    and review.runtime_run_id != finding.runtime_run_id
                ):
                    raise ValueError("V11 finding and review owner mismatch")
            for finding in validated:
                bucket[finding.id] = finding
            return validated

    def list_agent_findings(self, investigation_id: str) -> list[AgentFinding]:
        return list(self._agent_findings.get(investigation_id, {}).values())

    def save_coordination_review(
        self,
        review: CoordinationReview,
    ) -> CoordinationReview:
        with self._lock:
            validated = CoordinationReview.model_validate(
                review.model_dump(mode="python")
            )
            self._coordination_reviews[validated.investigation_id] = validated
            return validated

    def save_multi_agent_result(
        self,
        investigation_id: str,
        findings: list[AgentFinding],
        executions: list[AgentExecution],
        review: CoordinationReview | None,
    ) -> None:
        """原子替换一次 SDK Agent 运行产生的持久化投影。"""
        validated_findings, validated_executions, validated_review = (
            _validate_multi_agent_result(
                investigation_id, findings, executions, review
            )
        )
        with self._lock:
            finding_buckets = dict(self._agent_findings)
            finding_bucket = {
                item_id: item
                for item_id, item in self._agent_findings.get(
                    investigation_id, {}
                ).items()
                if item.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK
            }
            finding_bucket.update(
                {item.id: item for item in validated_findings}
            )
            finding_buckets[investigation_id] = finding_bucket

            execution_buckets = dict(self._executions)
            execution_bucket = {
                item_id: item
                for item_id, item in self._executions.get(
                    investigation_id, {}
                ).items()
                if item.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK
            }
            execution_bucket.update(
                {item.id: item for item in validated_executions}
            )
            execution_buckets[investigation_id] = execution_bucket

            reviews = dict(self._coordination_reviews)
            if validated_review is not None:
                reviews[investigation_id] = validated_review
            elif (
                investigation_id in reviews
                and reviews[investigation_id].execution_layer
                == AgentExecutionLayer.OPENAI_AGENTS_SDK
            ):
                reviews.pop(investigation_id)

            self._agent_findings = finding_buckets
            self._executions = execution_buckets
            self._coordination_reviews = reviews

    def get_coordination_review(
        self,
        investigation_id: str,
    ) -> CoordinationReview | None:
        return self._coordination_reviews.get(investigation_id)

    def get_react_trace(self, investigation_id: str) -> ReActTrace | None:
        return self._react_traces.get(investigation_id)

    def save_v11_react_trace(self, trace: ReActTrace) -> ReActTrace:
        """只保存已由模型校验的 V11 结构化摘要轨迹。"""
        if trace.runtime_run_id is None or any(
            step.runtime_run_id != trace.runtime_run_id
            or step.assistant_text is not None
            for step in trace.steps
        ):
            raise ValueError("V11 ReAct trace requires structured same-run steps")
        self._react_traces[trace.investigation_id] = trace
        return trace
