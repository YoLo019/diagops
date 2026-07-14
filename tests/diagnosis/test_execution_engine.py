from datetime import UTC, datetime

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.execution_engine import DiagnosisExecutionEngine
from backend.domain.agent_context import ContextFactType
from backend.domain.agent_plan import (
    AgentExecutionStatus,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskStatus,
    DiagnosisTaskType,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.providers.results import ProviderResult, ProviderStatus
from backend.tools.registry import ToolRegistry


def test_successful_tool_call_completes_task_execution_and_call():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    register_tool(registry, "read_logs", ToolCallStatus.SUCCESS, ["ev-log"])
    plan = DiagnosisPlan(investigation_id="inv-1", tasks=[task("task-log")])

    completed = DiagnosisExecutionEngine(repository, registry).run(plan, event())

    assert completed.tasks[0].status == DiagnosisTaskStatus.COMPLETED
    assert repository.list_tasks("inv-1")[0].status == DiagnosisTaskStatus.COMPLETED
    assert repository.list_executions("inv-1")[0].status == AgentExecutionStatus.COMPLETED
    assert repository.list_executions("inv-1")[0].evidence_ids == ["ev-log"]
    assert repository.list_tool_calls("inv-1")[0].status == ToolCallStatus.SUCCESS
    assert repository.list_tool_calls("inv-1")[0].input == {"investigation_id": "inv-1"}
    assert repository.list_context_facts("inv-1")[0].fact_type == ContextFactType.OBSERVATION
    assert repository.list_context_facts("inv-1")[0].evidence_ids == ["ev-log"]


def test_failed_tool_call_marks_task_and_execution_failed_without_raising():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    register_tool(registry, "read_logs", ToolCallStatus.FAILED, error_message="logs down")
    plan = DiagnosisPlan(investigation_id="inv-1", tasks=[task("task-log")])

    completed = DiagnosisExecutionEngine(repository, registry).run(plan, event())

    assert completed.tasks[0].status == DiagnosisTaskStatus.FAILED
    assert repository.list_executions("inv-1")[0].status == AgentExecutionStatus.FAILED
    assert repository.list_executions("inv-1")[0].error_message == "logs down"
    assert repository.list_tool_calls("inv-1")[0].status == ToolCallStatus.FAILED


def test_dependency_failed_task_is_skipped():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    skipped_calls: list[str] = []
    register_tool(registry, "read_logs", ToolCallStatus.FAILED, error_message="logs down")
    register_tool(registry, "read_service_catalog", ToolCallStatus.SUCCESS, calls=skipped_calls)
    plan = DiagnosisPlan(
        investigation_id="inv-1",
        tasks=[
            task("task-log", priority=10),
            task(
                "task-service",
                task_type=DiagnosisTaskType.SERVICE_CONTEXT,
                priority=20,
                depends_on=["task-log"],
            ),
        ],
    )

    completed = DiagnosisExecutionEngine(repository, registry).run(plan, event())

    assert [item.status for item in completed.tasks] == [
        DiagnosisTaskStatus.FAILED,
        DiagnosisTaskStatus.SKIPPED,
    ]
    assert [item.status for item in repository.list_executions("inv-1")] == [
        AgentExecutionStatus.FAILED,
        AgentExecutionStatus.SKIPPED,
    ]
    assert skipped_calls == []


def test_provider_skipped_tool_skips_task_and_dependent_without_calling_tool():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    skipped_calls: list[str] = []
    register_tool(registry, "read_logs", ToolCallStatus.SUCCESS, provider=EvidenceProvider.LOG)
    register_tool(registry, "read_service_catalog", ToolCallStatus.SUCCESS, calls=skipped_calls)
    plan = DiagnosisPlan(
        investigation_id="inv-1",
        tasks=[
            task("task-log", priority=10),
            task(
                "task-service",
                task_type=DiagnosisTaskType.SERVICE_CONTEXT,
                priority=20,
                depends_on=["task-log"],
            ),
        ],
    )

    completed = DiagnosisExecutionEngine(repository, registry).run(
        plan,
        event(),
        provider_results=[
            ProviderResult(
                provider=EvidenceProvider.LOG,
                status=ProviderStatus.SKIPPED,
                error_message="logs disabled",
            )
        ],
    )

    assert [item.status for item in completed.tasks] == [
        DiagnosisTaskStatus.SKIPPED,
        DiagnosisTaskStatus.SKIPPED,
    ]
    assert [item.status for item in repository.list_executions("inv-1")] == [
        AgentExecutionStatus.SKIPPED,
        AgentExecutionStatus.SKIPPED,
    ]
    assert repository.list_executions("inv-1")[0].error_message == "logs disabled"
    assert repository.list_executions("inv-1")[0].summary == "logs disabled"
    assert repository.list_tool_calls("inv-1")[0].status == ToolCallStatus.SKIPPED
    assert repository.list_tool_calls("inv-1")[0].error_message == "logs disabled"
    assert skipped_calls == []


def test_provider_mixed_success_and_skipped_completes_with_successful_evidence():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    register_tool(
        registry,
        "read_service_catalog",
        ToolCallStatus.SUCCESS,
        provider=EvidenceProvider.SERVICE_CATALOG,
    )
    plan = DiagnosisPlan(
        investigation_id="inv-1",
        tasks=[
            task(
                "task-service",
                task_type=DiagnosisTaskType.SERVICE_CONTEXT,
            )
        ],
    )

    completed = DiagnosisExecutionEngine(repository, registry).run(
        plan,
        event(),
        provider_results=[
            ProviderResult(
                provider=EvidenceProvider.SERVICE_CATALOG,
                evidence_items=[
                    evidence_item(
                        "ev-service",
                        EvidenceProvider.SERVICE_CATALOG,
                        EvidenceKind.SERVICE_METADATA,
                    )
                ],
            ),
            ProviderResult(
                provider=EvidenceProvider.SERVICE_CATALOG,
                status=ProviderStatus.SKIPPED,
                error_message="file catalog disabled",
            ),
        ],
    )

    call = repository.list_tool_calls("inv-1")[0]
    execution = repository.list_executions("inv-1")[0]
    assert call.status == ToolCallStatus.SUCCESS
    assert call.output_evidence_ids == ["ev-service"]
    assert call.error_message == "file catalog disabled"
    assert completed.tasks[0].status == DiagnosisTaskStatus.COMPLETED
    assert execution.status == AgentExecutionStatus.COMPLETED
    assert execution.evidence_ids == ["ev-service"]


def test_provider_partial_with_evidence_is_success_with_partial_message():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    register_tool(
        registry,
        "read_logs",
        ToolCallStatus.SUCCESS,
        provider=EvidenceProvider.LOG,
    )
    plan = DiagnosisPlan(investigation_id="inv-1", tasks=[task("task-log")])

    completed = DiagnosisExecutionEngine(repository, registry).run(
        plan,
        event(),
        provider_results=[
            ProviderResult(
                provider=EvidenceProvider.LOG,
                status=ProviderStatus.PARTIAL,
                evidence_items=[
                    evidence_item("ev-log", EvidenceProvider.LOG, EvidenceKind.LOG_PATTERN)
                ],
                error_message="logs partial",
            )
        ],
    )

    call = repository.list_tool_calls("inv-1")[0]
    assert completed.tasks[0].status == DiagnosisTaskStatus.COMPLETED
    assert repository.list_executions("inv-1")[0].status == AgentExecutionStatus.COMPLETED
    assert call.status == ToolCallStatus.SUCCESS
    assert call.output_evidence_ids == ["ev-log"]
    assert call.error_message == "logs partial"


def test_provider_partial_without_evidence_skips_task_with_partial_message():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    register_tool(
        registry,
        "read_logs",
        ToolCallStatus.SUCCESS,
        provider=EvidenceProvider.LOG,
    )
    plan = DiagnosisPlan(investigation_id="inv-1", tasks=[task("task-log")])

    completed = DiagnosisExecutionEngine(repository, registry).run(
        plan,
        event(),
        provider_results=[
            ProviderResult(
                provider=EvidenceProvider.LOG,
                status=ProviderStatus.PARTIAL,
                error_message="logs partial",
            )
        ],
    )

    call = repository.list_tool_calls("inv-1")[0]
    execution = repository.list_executions("inv-1")[0]
    assert completed.tasks[0].status == DiagnosisTaskStatus.SKIPPED
    assert execution.status == AgentExecutionStatus.SKIPPED
    assert execution.summary == "logs partial"
    assert call.status == ToolCallStatus.SKIPPED
    assert call.error_message == "logs partial"


def test_dependency_waits_when_dependent_has_higher_priority():
    repository = InMemoryInvestigationRepository()
    registry = ToolRegistry()
    calls: list[str] = []
    register_tool(registry, "read_logs", ToolCallStatus.SUCCESS, calls=calls)
    register_tool(registry, "read_service_catalog", ToolCallStatus.SUCCESS, calls=calls)
    plan = DiagnosisPlan(
        investigation_id="inv-1",
        tasks=[
            task(
                "task-service",
                task_type=DiagnosisTaskType.SERVICE_CONTEXT,
                priority=1,
                depends_on=["task-log"],
            ),
            task("task-log", priority=20),
        ],
    )

    completed = DiagnosisExecutionEngine(repository, registry).run(plan, event())

    assert {item.id: item.status for item in completed.tasks} == {
        "task-service": DiagnosisTaskStatus.COMPLETED,
        "task-log": DiagnosisTaskStatus.COMPLETED,
    }
    assert calls == ["read_logs", "read_service_catalog"]


def test_sqlite_repository_round_trips_engine_plan_tasks_executions_and_tool_calls(
    tmp_path,
):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'diagops-execution.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    repository.save(InvestigationRecord(id="inv-1", event=event()))
    registry = ToolRegistry()
    register_tool(registry, "read_logs", ToolCallStatus.SUCCESS, ["ev-log"])
    plan = DiagnosisPlan(investigation_id="inv-1", tasks=[task("task-log")])

    DiagnosisExecutionEngine(repository, registry).run(plan, event())

    assert repository.get_plan("inv-1") is not None
    assert repository.list_tasks("inv-1")[0].status == DiagnosisTaskStatus.COMPLETED
    assert repository.list_executions("inv-1")[0].status == AgentExecutionStatus.COMPLETED
    assert repository.list_tool_calls("inv-1")[0].output_evidence_ids == ["ev-log"]
    assert repository.list_context_facts("inv-1")[0].evidence_ids == ["ev-log"]


def register_tool(
    registry: ToolRegistry,
    name: str,
    status: ToolCallStatus,
    evidence_ids: list[str] | None = None,
    *,
    error_message: str | None = None,
    calls: list[str] | None = None,
    provider: EvidenceProvider | None = None,
) -> None:
    def handler(**kwargs):
        if calls is not None:
            calls.append(kwargs["tool_name"])
        return ToolCallRecord(
            task_id=kwargs["task_id"],
            agent_name=kwargs["agent_name"],
            tool_name=kwargs["tool_name"],
            input=kwargs["input"] or {},
            status=status,
            output_evidence_ids=evidence_ids or [],
            error_message=error_message,
        )

    registry.register(ToolSpec(name=name, description=f"{name} tool", provider=provider), handler)


def task(
    task_id: str,
    *,
    task_type: DiagnosisTaskType = DiagnosisTaskType.LOG_INVESTIGATION,
    priority: int = 100,
    depends_on: list[str] | None = None,
) -> DiagnosisTask:
    return DiagnosisTask(
        id=task_id,
        title=task_id,
        description="Collect evidence.",
        task_type=task_type,
        agent_name="UnroutedAgent",
        tool_names=["unrouted_tool"],
        priority=priority,
        depends_on=depends_on or [],
    )


def evidence_item(
    evidence_id: str,
    provider: EvidenceProvider,
    kind: EvidenceKind,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=provider,
        kind=kind,
        timestamp=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        summary=evidence_id,
    )


def event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="Checkout errors",
        description="Users see 500s.",
        started_at=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
    )
