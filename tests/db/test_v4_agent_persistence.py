from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, inspect, select

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import agent_executions, diagnosis_tasks, schema_version
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
from backend.domain.multi_agent import AgentExecutionLayer
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.services.incident_cases import load_incident_case


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


def seed_investigation(repository, investigation_id: str = "inv-1") -> None:
    repository.save(
        InvestigationRecord(
            id=investigation_id,
            event=load_incident_case("deployment_regression"),
        )
    )


def test_schema_initialization_creates_current_agent_tables(tmp_path):
    _repository, engine = build_repository(tmp_path)

    tables = set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        version = connection.execute(select(schema_version.c.version)).scalar_one()

    assert version == 7
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
    seed_investigation(repository)
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
    seed_investigation(repository)
    first = task("task-1")
    second = task("task-2")

    repository.save_tasks("inv-1", [first, second])
    repository.save_tasks("inv-1", [second])

    assert repository.list_tasks("inv-1") == [second]


def test_executions_context_facts_and_tool_calls_append_and_replace_by_id(tmp_path):
    repository, _engine = build_repository(tmp_path)
    seed_investigation(repository)
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


def test_v7_tasks_and_executions_round_trip_in_memory_and_sqlite(tmp_path):
    sqlite_repository, _engine = build_repository(tmp_path)
    seed_investigation(sqlite_repository)
    repositories = [InMemoryInvestigationRepository(), sqlite_repository]
    tasks = [
        task(f"task-sdk-{round_number}").model_copy(
            update={
                "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
                "analysis_round": round_number,
            }
        )
        for round_number in (1, 2)
    ]
    executions = [
        AgentExecution(
            id=f"exec-sdk-{round_number}",
            task_id=tasks[round_number - 1].id,
            agent_name="LogAgent",
            status=AgentExecutionStatus.COMPLETED,
            execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
            analysis_round=round_number,
            started_at=dt(round_number),
            completed_at=dt(round_number + 1),
        )
        for round_number in (1, 2)
    ]

    for repository in repositories:
        repository.save_tasks("inv-1", tasks)
        repository.save_executions("inv-1", executions)

        assert repository.list_tasks("inv-1") == tasks
        assert repository.list_executions("inv-1") == executions


def test_old_task_and_execution_payloads_use_v7_defaults(tmp_path):
    old_task = task("task-old").model_dump(
        mode="json", exclude={"execution_layer", "analysis_round"}
    )
    old_execution = AgentExecution(
        id="exec-old",
        task_id="task-old",
        agent_name="LogAgent",
        status=AgentExecutionStatus.COMPLETED,
        started_at=dt(1),
    ).model_dump(mode="json", exclude={"execution_layer", "analysis_round"})

    restored_task = DiagnosisTask.model_validate(old_task)
    restored_execution = AgentExecution.model_validate(old_execution)

    assert restored_task.execution_layer == AgentExecutionLayer.CUSTOM
    assert restored_task.analysis_round is None
    assert restored_execution.execution_layer == AgentExecutionLayer.CUSTOM
    assert restored_execution.analysis_round is None

    memory_repository = InMemoryInvestigationRepository()
    memory_repository.save_tasks("inv-old", [restored_task])
    memory_repository.save_executions("inv-old", [restored_execution])
    assert memory_repository.list_tasks("inv-old") == [restored_task]
    assert memory_repository.list_executions("inv-old") == [restored_execution]

    sqlite_repository, engine = build_repository(tmp_path)
    seed_investigation(sqlite_repository, "inv-old")
    with engine.begin() as connection:
        connection.execute(
            insert(diagnosis_tasks).values(
                id=old_task["id"],
                investigation_id="inv-old",
                position=0,
                status=old_task["status"],
                created_at=old_task["created_at"],
                payload=old_task,
            )
        )
        connection.execute(
            insert(agent_executions).values(
                id=old_execution["id"],
                investigation_id="inv-old",
                task_id=old_execution["task_id"],
                status=old_execution["status"],
                created_at=old_execution["started_at"],
                payload=old_execution,
            )
        )
    assert sqlite_repository.list_tasks("inv-old") == [restored_task]
    assert sqlite_repository.list_executions("inv-old") == [restored_execution]
