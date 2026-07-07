from __future__ import annotations

from datetime import UTC, datetime

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.actions import ActionStatus, VerificationStatus
from backend.domain.agent_context import ContextFact
from backend.domain.agent_plan import AgentExecution, DiagnosisPlan, DiagnosisTask
from backend.domain.memory import MemoryItem
from backend.domain.tool_calls import ToolCallRecord


class InMemoryInvestigationRepository:
    def __init__(self) -> None:
        self._records: dict[str, InvestigationRecord] = {}
        self._plans: dict[str, DiagnosisPlan] = {}
        self._tasks: dict[str, list[DiagnosisTask]] = {}
        self._executions: dict[str, dict[str, AgentExecution]] = {}
        self._context_facts: dict[str, dict[str, ContextFact]] = {}
        self._tool_calls: dict[str, dict[str, ToolCallRecord]] = {}
        self._memory_items: dict[str, MemoryItem] = {}

    def save(self, record: InvestigationRecord) -> InvestigationRecord:
        self._records[record.id] = record
        return record

    def get(self, investigation_id: str) -> InvestigationRecord:
        try:
            return self._records[investigation_id]
        except KeyError as exc:
            raise ValueError(f"Unknown investigation: {investigation_id}") from exc

    def list(self) -> list[InvestigationRecord]:
        return sorted(
            self._records.values(), key=lambda record: record.created_at, reverse=True
        )

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
        record = self.get(investigation_id)
        parsed_status = ActionStatus(status)
        for action in record.actions:
            if action.id == action_id:
                action.status = parsed_status
                action.note = note
                record.updated_at = datetime.now(UTC)
                self._records[record.id] = record
                return action
        raise ValueError(f"Unknown action: {action_id}")

    def update_verification_status(
        self,
        investigation_id: str,
        verification_id: str,
        *,
        status: VerificationStatus | str,
        result_note: str | None = None,
    ):
        record = self.get(investigation_id)
        parsed_status = VerificationStatus(status)
        for suggestion in record.verification_suggestions:
            if suggestion.id == verification_id:
                suggestion.status = parsed_status
                suggestion.result_note = result_note
                record.updated_at = datetime.now(UTC)
                self._records[record.id] = record
                return suggestion
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
