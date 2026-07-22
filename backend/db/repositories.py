from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock

from backend.db.models import (
    InvestigationRecord,
    InvestigationStatus,
    InvestigationSummary,
)
from backend.domain.actions import ActionStatus, VerificationStatus
from backend.domain.agent_context import ContextFact
from backend.domain.agent_findings import AgentFinding, CoordinationReview
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
    return validated_findings, validated_executions, validated_review


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
            self._records[record.id] = record
            return record

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
            return [InvestigationSummary.from_record(record) for record in self.list()]

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
        self._plans[plan.investigation_id] = plan
        self._tasks[plan.investigation_id] = list(plan.tasks)
        return plan

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
            self._tasks[investigation_id] = list(tasks)
            return list(tasks)

    def list_tasks(self, investigation_id: str) -> list[DiagnosisTask]:
        return list(self._tasks.get(investigation_id, []))

    def save_executions(
        self,
        investigation_id: str,
        executions: list[AgentExecution],
    ) -> list[AgentExecution]:
        bucket = self._executions.setdefault(investigation_id, {})
        for execution in executions:
            bucket[execution.id] = execution
        return list(executions)

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
        bucket = self._agent_findings.setdefault(investigation_id, {})
        for finding in findings:
            bucket[finding.id] = finding
        return list(findings)

    def list_agent_findings(self, investigation_id: str) -> list[AgentFinding]:
        return list(self._agent_findings.get(investigation_id, {}).values())

    def save_coordination_review(
        self,
        review: CoordinationReview,
    ) -> CoordinationReview:
        self._coordination_reviews[review.investigation_id] = review
        return review

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
