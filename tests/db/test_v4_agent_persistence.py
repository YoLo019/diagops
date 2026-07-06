from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, select

from backend.db.schema import schema_version
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.agent_context import ContextFact, ContextFactType
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskStatus,
    DiagnosisTaskType,
)
from backend.domain.memory import MemoryItem, MemoryType
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus


def build_repository(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'diagops-v4-test.db'}")
    initialize_database(engine)
    return SQLiteInvestigationRepository(engine), engine


def dt(minutes: int) -> datetime:
    return datetime(2026, 7, 6, 9, minutes, tzinfo=UTC)


def task(task_id: str, status: DiagnosisTaskStatus = DiagnosisTaskStatus.PENDING):
    return DiagnosisTask(
        id=task_id,
        title=f"Task {task_id}",
        description="Collect read-only evidence.",
        task_type=DiagnosisTaskType.LOG_INVESTIGATION,
        agent_name="LogAgent",
        tool_names=["read_logs"],
        status=status,
        created_at=dt(0),
    )


def test_schema_initialization_creates_v4_agent_tables(tmp_path):
    _repository, engine = build_repository(tmp_path)

    tables = set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        version = connection.execute(select(schema_version.c.version)).scalar_one()

    assert version == 4
    assert {
        "diagnosis_plans",
        "diagnosis_tasks",
        "agent_executions",
        "context_facts",
        "tool_calls",
        "memory_items",
    } <= tables


def test_plan_round_trip_replaces_plan_and_tasks_for_investigation(tmp_path):
    repository, _engine = build_repository(tmp_path)
    first_task = task("task-1")
    first_plan = DiagnosisPlan(
        id="plan-1",
        investigation_id="inv-1",
        tasks=[first_task],
        created_at=dt(1),
    )

    repository.save_plan(first_plan)

    assert repository.get_plan("inv-1") == first_plan
    assert repository.list_tasks("inv-1") == [first_task]

    replacement_task = task("task-2", DiagnosisTaskStatus.RUNNING)
    replacement_plan = DiagnosisPlan(
        id="plan-2",
        investigation_id="inv-1",
        tasks=[replacement_task],
        created_at=dt(2),
    )

    repository.save_plan(replacement_plan)

    assert repository.get_plan("inv-1") == replacement_plan
    assert repository.list_tasks("inv-1") == [replacement_task]


def test_save_tasks_replaces_task_list_for_investigation(tmp_path):
    repository, _engine = build_repository(tmp_path)
    first = task("task-1")
    second = task("task-2")

    repository.save_tasks("inv-1", [first, second])
    repository.save_tasks("inv-1", [second])

    assert repository.list_tasks("inv-1") == [second]


def test_executions_context_facts_and_tool_calls_append_and_replace_by_id(tmp_path):
    repository, _engine = build_repository(tmp_path)
    execution = AgentExecution(
        id="exec-1",
        task_id="task-1",
        agent_name="LogAgent",
        status=AgentExecutionStatus.RUNNING,
        started_at=dt(1),
    )
    second_execution = AgentExecution(
        id="exec-2",
        task_id="task-2",
        agent_name="MetricAgent",
        status=AgentExecutionStatus.COMPLETED,
        started_at=dt(2),
    )
    updated_execution = execution.model_copy(
        update={"status": AgentExecutionStatus.COMPLETED, "duration_ms": 15}
    )

    repository.save_executions("inv-1", [execution])
    repository.save_executions("inv-1", [second_execution])
    repository.save_executions("inv-1", [updated_execution])

    assert repository.list_executions("inv-1") == [updated_execution, second_execution]

    fact = ContextFact(
        id="fact-1",
        source_agent="LogAgent",
        fact_type=ContextFactType.OBSERVATION,
        summary="Errors increased after deploy.",
        confidence=0.9,
        evidence_ids=["ev-1"],
        created_at=dt(3),
    )
    second_fact = ContextFact(
        id="fact-2",
        source_agent="MetricAgent",
        fact_type=ContextFactType.MISSING_EVIDENCE,
        summary="Need more metrics.",
        confidence=0.5,
        created_at=dt(4),
    )
    updated_fact = fact.model_copy(update={"summary": "Errors increased after release."})

    repository.save_context_facts("inv-1", [fact])
    repository.save_context_facts("inv-1", [second_fact])
    repository.save_context_facts("inv-1", [updated_fact])

    assert repository.list_context_facts("inv-1") == [updated_fact, second_fact]

    call = ToolCallRecord(
        id="tool-1",
        task_id="task-1",
        agent_name="LogAgent",
        tool_name="read_logs",
        status=ToolCallStatus.RUNNING,
        started_at=dt(5),
    )
    second_call = ToolCallRecord(
        id="tool-2",
        task_id="task-2",
        agent_name="MetricAgent",
        tool_name="query_metrics",
        status=ToolCallStatus.SUCCESS,
        started_at=dt(6),
        output_evidence_ids=["ev-2"],
    )
    updated_call = call.model_copy(
        update={"status": ToolCallStatus.SUCCESS, "output_evidence_ids": ["ev-1"]}
    )

    repository.save_tool_calls("inv-1", [call])
    repository.save_tool_calls("inv-1", [second_call])
    repository.save_tool_calls("inv-1", [updated_call])

    assert repository.list_tool_calls("inv-1") == [updated_call, second_call]


def test_memory_lookup_filters_service_environment_newest_first_and_replaces_by_id(tmp_path):
    repository, _engine = build_repository(tmp_path)
    older = MemoryItem(
        id="mem-1",
        service="checkout",
        environment="prod",
        memory_type=MemoryType.INVESTIGATION_SUMMARY,
        summary="Checkout deploy caused 5xx.",
        created_at=dt(1),
    )
    newer = MemoryItem(
        id="mem-2",
        service="checkout",
        environment="prod",
        memory_type=MemoryType.HUMAN_FEEDBACK,
        summary="Rollback matched the incident review.",
        created_at=dt(3),
    )
    other_environment = MemoryItem(
        id="mem-3",
        service="checkout",
        environment="staging",
        memory_type=MemoryType.INVESTIGATION_SUMMARY,
        summary="Staging issue.",
        created_at=dt(4),
    )
    other_service = MemoryItem(
        id="mem-4",
        service="payments",
        environment="prod",
        memory_type=MemoryType.INVESTIGATION_SUMMARY,
        summary="Payments issue.",
        created_at=dt(5),
    )
    corrected_older = older.model_copy(update={"summary": "Checkout release caused 5xx."})

    repository.save_memory_items([older, newer, other_environment, other_service])
    repository.save_memory_items([corrected_older])

    assert repository.list_memory("checkout", "prod") == [newer, corrected_older]
    assert repository.list_memory("checkout", "prod", limit=1) == [newer]


def test_missing_plan_returns_none(tmp_path):
    repository, _engine = build_repository(tmp_path)

    assert repository.get_plan("missing") is None


def test_memory_round_trip_preserves_created_at_microseconds(tmp_path):
    repository, _engine = build_repository(tmp_path)
    created_at = dt(1) + timedelta(microseconds=123)
    memory = MemoryItem(
        id="mem-1",
        service="checkout",
        environment="prod",
        memory_type=MemoryType.SERVICE_INCIDENT_SUMMARY,
        summary="Previous incident was deploy related.",
        created_at=created_at,
    )

    repository.save_memory_items([memory])

    assert repository.list_memory("checkout", "prod") == [memory]
