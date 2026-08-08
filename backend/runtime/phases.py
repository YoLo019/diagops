from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Connection

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import investigations
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.agent_context import ContextFact
from backend.domain.agent_findings import AgentFinding, CoordinationReview
from backend.domain.agent_plan import AgentExecution, DiagnosisPlan, DiagnosisTask
from backend.domain.multi_agent import (
    ExecutionContractVersion,
    InvestigationStrategy,
    ModelProvider,
)
from backend.domain.react_trace import ReActTrace
from backend.domain.runtime import RuntimePhase, RuntimeResumeState, RuntimeRunReason
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.tools.registry import ToolInvocationResult


@dataclass(frozen=True, slots=True)
class PhaseInput:
    run_id: str
    attempt_id: str
    phase: RuntimePhase
    resume_state: RuntimeResumeState
    investigation_id: str | None = None
    strategy: InvestigationStrategy | None = None
    run_reason: RuntimeRunReason = RuntimeRunReason.INITIAL
    execution_contract_version: ExecutionContractVersion = (
        ExecutionContractVersion.V10_LEGACY
    )
    execution_contract: dict[str, Any] | None = None
    model_provider: ModelProvider | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    tool_budget: int | None = None
    token_budget: int | None = None
    timeout_seconds: float = 60.0
    deadline_at: datetime | None = None
    remaining_deadline_seconds: Callable[[], float] | None = None
    check_execution: Callable[[], None] | None = None
    resolve_tool_result: Callable[[str], ToolCallRecord | None] | None = None
    persist_tool_start: Callable[[ToolCallRecord], Any] | None = None
    persist_tool_result: Callable[[ToolInvocationResult], Any] | None = None
    persist_agent_event: Callable[[str, str], Any] | None = None
    persist_model_event: Callable[[str, str, int, int, str], Any] | None = None
    hit_fault: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if not isfinite(self.timeout_seconds) or self.timeout_seconds < 1:
            raise ValueError("phase input timeout_seconds must be at least one second")


@dataclass(frozen=True, slots=True)
class PhaseOutput:
    business_mutation: BusinessMutation
    status: str = "completed"
    safe_payload: dict[str, Any] = field(default_factory=dict)
    resume_state: RuntimeResumeState = field(default_factory=RuntimeResumeState)


@dataclass(frozen=True, slots=True)
class BusinessMutation:
    """描述 Phase 产生的业务投影；实际写入由 Store 的本地事务执行。"""

    investigation_id: str
    investigation: InvestigationRecord | None = None
    plan: DiagnosisPlan | None = None
    tasks: tuple[DiagnosisTask, ...] | None = None
    context_facts: tuple[ContextFact, ...] | None = None
    tool_calls: tuple[ToolCallRecord, ...] | None = None
    findings: tuple[AgentFinding, ...] | None = None
    executions: tuple[AgentExecution, ...] | None = None
    review: CoordinationReview | None = None
    react_trace: ReActTrace | None = None
    replace_multi_agent_result: bool = False
    activate_projection: bool = False

    def apply_memory(self, repository: InMemoryInvestigationRepository) -> None:
        self._validate_investigation_record()
        repository.get(self.investigation_id)
        if self.activate_projection:
            owner = (
                self.investigation.active_runtime_run_id
                if self.investigation is not None
                else None
            )
            if owner is None:
                raise ValueError("projection activation requires a runtime owner")
            repository.activate_projection(self.investigation_id, owner)
        if self.investigation is not None:
            repository.save(self._projection_record())
        if self.plan is not None:
            repository.save_plan(self.plan)
        if self.tasks is not None:
            repository.save_tasks(self.investigation_id, list(self.tasks))
        if self.context_facts is not None:
            repository.save_context_facts(
                self.investigation_id, list(self.context_facts)
            )
        if self.react_trace is not None:
            repository.save_v11_react_trace(self.react_trace)
        if self.tool_calls is not None:
            repository.save_tool_calls(self.investigation_id, list(self.tool_calls))
        if self.replace_multi_agent_result:
            repository.save_multi_agent_result(
                self.investigation_id,
                list(self.findings or ()),
                list(self.executions or ()),
                self.review,
            )

    def apply_sqlite(
        self,
        repository: SQLiteInvestigationRepository,
        connection: Connection,
    ) -> None:
        self._validate_investigation_record()
        exists = connection.execute(
            select(investigations.c.id).where(
                investigations.c.id == self.investigation_id
            )
        ).scalar_one_or_none()
        if exists is None:
            raise ValueError(f"Unknown investigation: {self.investigation_id}")
        if self.activate_projection:
            owner = (
                self.investigation.active_runtime_run_id
                if self.investigation is not None
                else None
            )
            if owner is None:
                raise ValueError("projection activation requires a runtime owner")
            repository.activate_projection_with_connection(
                connection, self.investigation_id, owner
            )
        if self.investigation is not None:
            repository.save_with_connection(connection, self._projection_record())
        if self.plan is not None:
            repository.save_plan_with_connection(connection, self.plan)
        if self.tasks is not None:
            repository.save_tasks_with_connection(
                connection, self.investigation_id, self.tasks
            )
        if self.context_facts is not None:
            repository.save_context_facts_with_connection(
                connection, self.investigation_id, self.context_facts
            )
        if self.react_trace is not None:
            repository.save_v11_react_trace_with_connection(
                connection, self.react_trace
            )
        if self.tool_calls is not None:
            repository.save_tool_calls_with_connection(
                connection, self.investigation_id, self.tool_calls
            )
        if self.replace_multi_agent_result:
            repository.save_multi_agent_result_with_connection(
                connection,
                self.investigation_id,
                self.findings or (),
                self.executions or (),
                self.review,
            )

    def _validate_investigation_record(self) -> None:
        if (
            self.investigation is not None
            and self.investigation.id != self.investigation_id
        ):
            raise ValueError("business mutation investigation mismatch")

    def _projection_record(self) -> InvestigationRecord:
        if not self.activate_projection or self.investigation is None:
            return self.investigation
        return self.investigation.model_copy(
            update={
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


@dataclass(frozen=True, slots=True)
class ToolCommit:
    run_id: str
    attempt_id: str
    lease_owner: str
    lease_version: int
    business_mutation: BusinessMutation
    call: ToolCallRecord
    phase: RuntimePhase | None = None


@dataclass(frozen=True, slots=True)
class PhaseCommit:
    run_id: str
    attempt_id: str
    lease_owner: str
    lease_version: int
    phase: RuntimePhase
    business_mutation: BusinessMutation
    safe_payload: dict[str, Any]
    resume_state: RuntimeResumeState
    status: str = "completed"
    expected_checkpoint_id: str | None = None
    expected_previous_phase: RuntimePhase | None = None
    checkpoint_id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in {"completed", "skipped"}:
            raise ValueError("phase commit status must be completed or skipped")


V10_PHASE_ORDER: tuple[RuntimePhase, ...] = (
    RuntimePhase.INTAKE,
    RuntimePhase.EVIDENCE_COLLECTION,
    RuntimePhase.DETERMINISTIC_RCA,
    RuntimePhase.SPECIALIST_ANALYSIS,
    RuntimePhase.CONFLICT_REVIEW,
    RuntimePhase.COORDINATION,
    RuntimePhase.REPORT_GENERATION,
    RuntimePhase.FINALIZE,
)

V11_PHASE_ORDER: tuple[RuntimePhase, ...] = (
    RuntimePhase.INTAKE,
    RuntimePhase.EVIDENCE_COLLECTION,
    RuntimePhase.LEAD_PLANNING,
    RuntimePhase.INVESTIGATOR_ROUND_1,
    RuntimePhase.CRITIC_REVIEW,
    RuntimePhase.INVESTIGATOR_ROUND_2,
    RuntimePhase.CRITIC_RECONCILIATION,
    RuntimePhase.LEAD_ADJUDICATION,
    RuntimePhase.RESULT_VALIDATION,
    RuntimePhase.REPORT_GENERATION,
    RuntimePhase.FINALIZE,
)


@dataclass(frozen=True, slots=True)
class PhaseProfile:
    version: ExecutionContractVersion
    order: tuple[RuntimePhase, ...]
    handler_names: tuple[str, ...]
    ui_labels: tuple[tuple[str, str], ...]


_V10_PROFILE = PhaseProfile(
    version=ExecutionContractVersion.V10_LEGACY,
    order=V10_PHASE_ORDER,
    handler_names=tuple(phase.value for phase in V10_PHASE_ORDER),
    ui_labels=tuple((phase.value, phase.value) for phase in V10_PHASE_ORDER),
)
_V11_PROFILE = PhaseProfile(
    version=ExecutionContractVersion.V11,
    order=V11_PHASE_ORDER,
    handler_names=tuple(phase.value for phase in V11_PHASE_ORDER),
    ui_labels=tuple((phase.value, phase.value) for phase in V11_PHASE_ORDER),
)


def phase_profile_for(
    version: ExecutionContractVersion | str,
) -> PhaseProfile:
    """只按持久化执行版本选择 phase，避免 current config 串线。"""
    version = ExecutionContractVersion(version)
    return _V11_PROFILE if version == ExecutionContractVersion.V11 else _V10_PROFILE


def phase_order_for(
    version: ExecutionContractVersion | str,
) -> tuple[RuntimePhase, ...]:
    return phase_profile_for(version).order


RUNTIME_PHASE_ORDER = V10_PHASE_ORDER


def ensure_phase_precondition(
    *,
    current_phase: RuntimePhase | None,
    latest_checkpoint_id: str | None,
    commit: PhaseCommit,
    phase_order: tuple[RuntimePhase, ...] = V10_PHASE_ORDER,
) -> None:
    """用 checkpoint CAS 与稳定 Phase 顺序拒绝重复或越级提交。"""
    if commit.expected_checkpoint_id != latest_checkpoint_id:
        raise ValueError("phase checkpoint compare-and-set failed")
    if commit.expected_previous_phase != current_phase:
        raise ValueError("phase predecessor changed")
    expected_index = 0 if current_phase is None else phase_order.index(current_phase) + 1
    if (
        expected_index >= len(phase_order)
        or phase_order[expected_index] != commit.phase
    ):
        raise ValueError("phase commit is duplicate or out of order")


def checkpoint_digest(
    *,
    run_id: str,
    attempt_id: str,
    completed_phase: RuntimePhase,
    resume_state: RuntimeResumeState,
    schema_version: int = 1,
) -> str:
    """仅对恢复控制字段计算稳定摘要，不复制任何业务正文。"""
    payload = {
        "schema_version": schema_version,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "completed_phase": completed_phase.value,
        "referenced_record_ids": {
            "evidence": sorted(resume_state.completed_evidence_ids),
            "findings": sorted(resume_state.completed_finding_ids),
            "reviews": sorted(resume_state.completed_review_ids),
            "reports": sorted(resume_state.completed_report_ids),
        },
        "remaining_budgets": {
            "tool": resume_state.remaining_tool_budget,
            "token": resume_state.remaining_token_budget,
        },
        "successful_tool_keys": sorted(resume_state.successful_tool_keys),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def durable_projection_digest(
    *,
    repository,
    investigation_id: str,
    run_id: str,
    resume_state: RuntimeResumeState,
    require_exact: bool,
    mutation: BusinessMutation | None = None,
) -> str:
    """摘要 checkpoint 引用的 durable 业务对象；提交时还要求引用集合与投影完全一致。"""
    persisted_record = repository.get(investigation_id)
    record = (
        mutation.investigation
        if mutation is not None and mutation.investigation is not None
        else persisted_record
    )
    evidence_by_id = {item.id: item for item in record.evidence}
    findings = (
        mutation.findings
        if mutation is not None and mutation.findings is not None
        else repository.list_agent_findings(investigation_id)
    )
    findings_by_id = {item.id: item for item in findings}
    review = (
        mutation.review
        if mutation is not None and mutation.replace_multi_agent_result
        else repository.get_coordination_review(investigation_id)
    )
    reviews_by_id = {review.id: review} if review is not None else {}
    report = record.report
    reports_by_id = {report.id: report} if report is not None else {}
    calls = (
        mutation.tool_calls
        if mutation is not None and mutation.tool_calls is not None
        else repository.list_tool_calls(investigation_id)
    )
    successful_tools = {
        call.idempotency_key: call
        for call in calls
        if call.runtime_run_id == run_id
        and call.status == ToolCallStatus.SUCCESS
        and call.idempotency_key is not None
    }
    expected = {
        "evidence": set(resume_state.completed_evidence_ids),
        "findings": set(resume_state.completed_finding_ids),
        "reviews": set(resume_state.completed_review_ids),
        "reports": set(resume_state.completed_report_ids),
        "tools": set(resume_state.successful_tool_keys),
    }
    actual = {
        "evidence": set(evidence_by_id),
        "findings": set(findings_by_id),
        "reviews": set(reviews_by_id),
        "reports": set(reports_by_id),
        "tools": set(successful_tools),
    }
    if require_exact and expected != actual:
        raise ValueError("checkpoint resume references differ from durable projection")
    if any(not expected[name] <= actual[name] for name in expected):
        raise ValueError("checkpoint resume reference is missing from durable projection")

    def dumps(items_by_id, ids: set[str]) -> list[dict[str, Any]]:
        return [
            items_by_id[item_id].model_dump(mode="json")
            for item_id in sorted(ids)
        ]

    projection = {
        "evidence": dumps(evidence_by_id, expected["evidence"]),
        "findings": dumps(findings_by_id, expected["findings"]),
        "reviews": dumps(reviews_by_id, expected["reviews"]),
        "reports": dumps(reports_by_id, expected["reports"]),
        "tools": dumps(successful_tools, expected["tools"]),
        "remaining_budgets": {
            "tool": resume_state.remaining_tool_budget,
            "token": resume_state.remaining_token_budget,
        },
    }
    encoded = json.dumps(
        projection,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def durable_tool_call_count(
    *, repository, investigation_id: str, run_id: str, mutation: BusinessMutation | None
) -> int:
    calls = (
        mutation.tool_calls
        if mutation is not None and mutation.tool_calls is not None
        else repository.list_tool_calls(investigation_id)
    )
    return len(
        {
            call.logical_call_id or call.id
            for call in calls
            if call.runtime_run_id == run_id and call.status != ToolCallStatus.PENDING
        }
    )


def durable_token_usage(events, run_id: str) -> int:
    """从模型终态事件计算真实计费 usage，供 checkpoint 校验预算投影。"""
    terminal = {"model.completed", "model.failed"}
    return sum(
        int(event.safe_payload.get("input_tokens", 0))
        + int(event.safe_payload.get("output_tokens", 0))
        for event in events
        if event.run_id == run_id
        and event.schema_version == 1
        and isinstance(event.event_type, str)
        and str(event.event_type) in terminal
    )
