import asyncio
import re
from datetime import UTC, datetime
from threading import Barrier, Event, Lock

import pytest

from backend.config.settings import AppSettings, StorageSettings
from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.agents_runtime import (
    AgentsRcaRuntime,
    _CapturedDraft,
    _CoordinatorProposal,
    _SdkTurnResult,
    _SpecialistDraft,
)
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.evidence_validation import EvidenceContractError
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.agent_findings import AgentFindingType, AgentName
from backend.domain.events import IncidentSource
from backend.domain.evidence import EvidenceProvider
from backend.domain.hypotheses import CauseType
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    ExecutionStepKind,
    FailureCategory,
    InvestigationStrategy,
    ModelProvider,
    ResultValidationCategory,
)
from backend.domain.runtime import (
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus
from backend.providers.registry import ProviderRegistry, build_mock_provider_registry
from backend.providers.results import ProviderResult, ProviderStatus
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.diff import RuntimeDiffService
from backend.runtime.faults import DeterministicFaultInjector, RuntimeInjectedFault
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import PhaseInput
from backend.runtime.replay import ReplayDependencies, ReplayService
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.store import InMemoryRuntimeStore, RuntimePersistenceError
from backend.runtime.writer import RuntimeWriter
from backend.services.container import AppContainer
from backend.services.incident_cases import load_incident_case


def _orchestrator(repository=None) -> DiagnosisOrchestrator:
    providers = build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=repository or InMemoryInvestigationRepository(),
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )


def test_phase_executor_preserves_stable_order_and_explicit_conflict_skip() -> None:
    executor = DiagnosisPhaseExecutor(_orchestrator())

    result = asyncio.run(
        executor.execute(
            load_incident_case("deployment_regression"),
            strategy=InvestigationStrategy.FIXED,
        )
    )

    assert list(result.outputs) == [
        RuntimePhase.INTAKE,
        RuntimePhase.EVIDENCE_COLLECTION,
        RuntimePhase.DETERMINISTIC_RCA,
        RuntimePhase.SPECIALIST_ANALYSIS,
        RuntimePhase.CONFLICT_REVIEW,
        RuntimePhase.COORDINATION,
        RuntimePhase.REPORT_GENERATION,
        RuntimePhase.FINALIZE,
    ]
    assert result.outputs[RuntimePhase.CONFLICT_REVIEW].status in {
        "completed",
        "skipped",
    }
    assert result.record.status.value == "completed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "run_reason",
    [RuntimeRunReason.MANUAL_RERUN, RuntimeRunReason.ADDITIONAL_EVIDENCE],
)
async def test_container_runs_two_live_rounds_sequentially_for_one_investigation(
    run_reason: RuntimeRunReason,
) -> None:
    container = AppContainer(
        AppSettings(storage=StorageSettings(url="memory://"))
    )
    record = container.repository.save(
        InvestigationRecord(
            id=f"inv-two-runs-{run_reason.value}",
            event=load_incident_case("deployment_regression"),
        )
    )
    await container.startup()
    first = container.create_runtime_run(
        record.id,
        strategy=InvestigationStrategy.FIXED,
        run_reason=RuntimeRunReason.INITIAL,
    )
    await (await container.runtime_manager.start(first.id))
    second = container.create_runtime_run(
        record.id,
        strategy=InvestigationStrategy.FIXED,
        run_reason=run_reason,
        parent_run_id=first.id,
    )

    await (await container.runtime_manager.start(second.id))

    history = container.runtime_store.list_runs(record.id)
    assert [item.status for item in history] == [
        RuntimeRunStatus.COMPLETED,
        RuntimeRunStatus.COMPLETED,
    ]
    assert {item.id for item in history} == {first.id, second.id}
    assert container.repository.get(record.id).status.value == "completed"
    references = container.diff_runs(first.id, first.id).sections[
        "evidence_references"
    ].left
    assert references
    frozen = container.runtime_store.get_frozen_business_projection(first.id)
    assert references == sorted(item["id"] for item in frozen["evidence"])
    await container.shutdown()


def test_phase_input_rejects_invalid_frozen_timeout() -> None:
    with pytest.raises(ValueError, match="timeout"):
        PhaseInput(
            run_id="run-invalid-timeout",
            attempt_id="attempt-1",
            phase=RuntimePhase.INTAKE,
            resume_state=RuntimeResumeState(),
            timeout_seconds=0,
        )


def test_phase_outputs_are_immutable_snapshots() -> None:
    result = asyncio.run(
        DiagnosisPhaseExecutor(_orchestrator()).execute(load_incident_case("traffic_spike"))
    )

    output = result.outputs[RuntimePhase.EVIDENCE_COLLECTION]
    assert output.safe_payload["evidence_count"] > 0
    assert output.resume_state.completed_evidence_ids
    assert output.business_mutation.investigation is not result.record


def test_specialist_conflict_and_coordination_have_distinct_projections() -> None:
    result = asyncio.run(
        DiagnosisPhaseExecutor(_orchestrator()).execute(
            load_incident_case("deployment_regression"),
            strategy=InvestigationStrategy.FIXED,
        )
    )

    specialist = result.outputs[RuntimePhase.SPECIALIST_ANALYSIS]
    conflict = result.outputs[RuntimePhase.CONFLICT_REVIEW]
    coordination = result.outputs[RuntimePhase.COORDINATION]
    assert len(specialist.business_mutation.findings or ()) == 3
    assert specialist.business_mutation.review is None
    assert specialist.resume_state.completed_review_ids == []
    assert conflict.status == "skipped"
    assert conflict.business_mutation.review is None
    assert coordination.business_mutation.review is not None


@pytest.mark.anyio
async def test_durable_sessions_use_each_runs_frozen_model_and_budgets() -> None:
    orchestrator = _orchestrator()
    adapters = {}

    def model_adapter_factory(provider, model_name):
        adapter = object()
        adapters[(provider, model_name)] = adapter
        return adapter

    seen_models = []

    async def capturing_turn(**kwargs):
        await asyncio.sleep(0)
        seen_models.append(kwargs["model"])
        return _SdkTurnResult([], None, [])

    orchestrator.agents_runtime = AgentsRcaRuntime(
        model="process-global-model",
        turn=capturing_turn,
        model_adapter_factory=model_adapter_factory,
    )
    for investigation_id in ("inv-a", "inv-b"):
        orchestrator.repository.save(
            InvestigationRecord(
                id=investigation_id,
                event=load_incident_case("deployment_regression"),
            )
        )
    executor = DiagnosisPhaseExecutor(orchestrator)
    inputs = (
        PhaseInput(
            run_id="run-a",
            attempt_id="attempt-a",
            phase=RuntimePhase.INTAKE,
            resume_state=RuntimeResumeState(),
            investigation_id="inv-a",
            strategy=InvestigationStrategy.ADAPTIVE,
            model_provider=ModelProvider.OPENAI,
            model_name="model-a",
            prompt_version="prompt-a",
            tool_budget=2,
            token_budget=100,
        ),
        PhaseInput(
            run_id="run-b",
            attempt_id="attempt-b",
            phase=RuntimePhase.INTAKE,
            resume_state=RuntimeResumeState(),
            investigation_id="inv-b",
            strategy=InvestigationStrategy.ADAPTIVE,
            model_provider=ModelProvider.DEEPSEEK,
            model_name="model-b",
            prompt_version="prompt-b",
            tool_budget=5,
            token_budget=200,
        ),
    )

    await asyncio.gather(*(executor.execute_phase(item) for item in inputs))

    runtime_a = executor._durable_sessions["run-a"]._orchestrator.agents_runtime
    runtime_b = executor._durable_sessions["run-b"]._orchestrator.agents_runtime
    session_a = executor._durable_sessions["run-a"]
    session_b = executor._durable_sessions["run-b"]
    assert (
        runtime_a.model,
        runtime_a.model_provider,
        runtime_a.prompt_version,
        runtime_a.max_total_tool_calls,
        runtime_a.runtime_token_budget,
    ) == (
        adapters[(ModelProvider.OPENAI, "model-a")],
        ModelProvider.OPENAI,
        "prompt-a",
        2,
        100,
    )
    assert (
        runtime_b.model,
        runtime_b.model_provider,
        runtime_b.prompt_version,
        runtime_b.max_total_tool_calls,
        runtime_b.runtime_token_budget,
    ) == (
        adapters[(ModelProvider.DEEPSEEK, "model-b")],
        ModelProvider.DEEPSEEK,
        "prompt-b",
        5,
        200,
    )
    assert runtime_a.model is not runtime_b.model
    assert runtime_a._parallel_limit is session_a._parallel_limit
    assert runtime_b._parallel_limit is session_b._parallel_limit
    assert session_a._parallel_limit is not session_b._parallel_limit

    await asyncio.gather(
        runtime_a._invoke_turn(None, model=runtime_a.model),
        runtime_b._invoke_turn(None, model=runtime_b.model),
    )
    assert len(seen_models) == 2
    assert any(model is runtime_a.model for model in seen_models)
    assert any(model is runtime_b.model for model in seen_models)


@pytest.mark.anyio
async def test_resumed_durable_session_does_not_double_deduct_prior_tools() -> None:
    orchestrator = _orchestrator()

    async def turn(**_kwargs):
        return _SdkTurnResult([], None, [])

    orchestrator.agents_runtime = AgentsRcaRuntime(model="fake", turn=turn)
    orchestrator.repository.save(
        InvestigationRecord(
            id="inv-resume-budget",
            event=load_incident_case("deployment_regression"),
        )
    )
    prior_calls = [
        ToolCallRecord(
            id=f"tool-prior-{index}",
            task_id="task-prior",
            agent_name="LogAgent",
            tool_name="read_logs",
            status=ToolCallStatus.SUCCESS,
            runtime_run_id="run-resume-budget",
            logical_call_id=f"LogAgent:1:{index}",
            idempotency_key=f"prior-key-{index}",
            execution_id=f"tool-exec-prior-{index}",
        )
        for index in (1, 2)
    ]
    orchestrator.repository.save_tool_calls("inv-resume-budget", prior_calls)
    executor = DiagnosisPhaseExecutor(orchestrator)

    output = await executor.execute_phase(
        PhaseInput(
            run_id="run-resume-budget",
            attempt_id="attempt-resume-budget",
            phase=RuntimePhase.INTAKE,
            resume_state=RuntimeResumeState(
                remaining_tool_budget=3,
                remaining_token_budget=3,
                successful_tool_keys=["prior-key-1", "prior-key-2"],
            ),
            investigation_id="inv-resume-budget",
            strategy=InvestigationStrategy.ADAPTIVE,
            model_provider=ModelProvider.OPENAI,
            model_name="fake",
            prompt_version="v9",
            tool_budget=3,
            token_budget=3,
        )
    )

    session = executor._durable_sessions["run-resume-budget"]
    runtime = session._orchestrator.agents_runtime
    assert runtime.max_total_tool_calls == 3
    assert runtime.runtime_token_budget == 3
    assert output.resume_state.remaining_tool_budget == 3
    assert output.resume_state.remaining_token_budget == 3


@pytest.mark.anyio
async def test_new_attempt_rebuilds_durable_session_from_repository() -> None:
    orchestrator = _orchestrator()
    orchestrator.repository.save(
        InvestigationRecord(
            id="inv-attempt-session",
            event=load_incident_case("deployment_regression"),
        )
    )
    executor = DiagnosisPhaseExecutor(orchestrator)

    def phase_input(attempt_id: str) -> PhaseInput:
        return PhaseInput(
            run_id="run-attempt-session",
            attempt_id=attempt_id,
            phase=RuntimePhase.INTAKE,
            resume_state=RuntimeResumeState(),
            investigation_id="inv-attempt-session",
            strategy=InvestigationStrategy.FIXED,
        )

    await executor.execute_phase(phase_input("attempt-1"))
    first_session = executor._durable_sessions["run-attempt-session"]
    await executor.execute_phase(phase_input("attempt-2"))

    assert executor._durable_sessions["run-attempt-session"] is not first_session


@pytest.mark.anyio
async def test_resume_rebuilds_specialist_sandbox_without_duplicate_findings(
    runtime_store,
) -> None:
    store = runtime_store
    orchestrator = _orchestrator(store.investigation_repository)
    delegate = DiagnosisPhaseExecutor(orchestrator)

    class CrashAfterSpecialistOutput:
        def __init__(self) -> None:
            self.crashed = False

        async def execute_phase(self, phase_input):
            output = await delegate.execute_phase(phase_input)
            if phase_input.phase == RuntimePhase.SPECIALIST_ANALYSIS and not self.crashed:
                self.crashed = True
                raise RuntimeInjectedFault("crash after specialist output")
            return output

    run = store.create_run(
        RuntimeRun(
            id="run-specialist-resume",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=CrashAfterSpecialistOutput(),
        heartbeat_seconds=1,
    )

    crashed = await coordinator.execute(run.id, owner="worker-a")
    assert crashed.status.value == "running"
    if hasattr(store, "_runs"):
        store._runs[run.id] = store._runs[run.id].model_copy(
            update={"lease_expires_at": datetime(2026, 7, 16, tzinfo=UTC)}
        )
    else:
        from backend.db.schema import runtime_runs

        with store.engine.begin() as connection:
            connection.execute(
                runtime_runs.update()
                .where(runtime_runs.c.id == run.id)
                .values(lease_expires_at="2026-07-16T00:00:00+00:00")
            )
    coordinator.audit_expired_leases(datetime(2026, 7, 17, tzinfo=UTC))

    completed = await coordinator.resume(run.id, owner="worker-b")

    assert completed.status.value == "completed"
    assert len(store.investigation_repository.list_agent_findings("inv-1")) == 3
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_attempt_fault_releases_durable_session(runtime_store) -> None:
    store = runtime_store
    executor = DiagnosisPhaseExecutor(_orchestrator(store.investigation_repository))
    run = store.create_run(
        RuntimeRun(
            id="run-release-session",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
        fault_injector=DeterministicFaultInjector({"provider_before_commit": 1}),
    )

    crashed = await coordinator.execute(run.id, owner="worker-a")

    assert crashed.status.value == "running"
    assert run.id not in executor._durable_sessions
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_sdk_model_turns_are_committed_in_their_own_runtime_phases(
    runtime_store,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "present")
    store = runtime_store
    orchestrator = _orchestrator(store.investigation_repository)
    live_agent_observations: list[tuple[bool, bool]] = []

    async def turn(**kwargs):
        runtime_events = store.list_events("run-sdk-phase-boundaries")
        model_started = [
            item for item in runtime_events if item.event_type.value == "model.started"
        ][-1]
        previous_agent_terminal = max(
            (
                item.sequence
                for item in runtime_events
                if item.phase == model_started.phase
                and item.event_type.value in {"agent.completed", "agent.failed"}
            ),
            default=0,
        )
        live_agent_observations.append(
            (
                any(
                    item.phase == model_started.phase
                    and item.event_type.value == "agent.started"
                    and previous_agent_terminal < item.sequence < model_started.sequence
                    for item in runtime_events
                ),
                any(
                    item.phase == model_started.phase
                    and item.event_type.value in {"agent.completed", "agent.failed"}
                    and item.sequence > model_started.sequence
                    for item in runtime_events
                ),
            )
        )
        drafts = []
        for name in kwargs["specialist_names"]:
            prompt = kwargs["specialist_inputs"][name]
            evidence_match = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
            drafts.append(
                _CapturedDraft(
                    agent_name=name,
                    analysis_round=kwargs["analysis_round"],
                    draft=_SpecialistDraft(
                        finding_type=(
                            AgentFindingType.ROOT_CAUSE
                            if evidence_match is not None
                            else AgentFindingType.GAP
                        ),
                        summary=f"{name.value} finding",
                        confidence=0.8,
                        evidence_ids=(
                            [evidence_match.group(1)] if evidence_match is not None else []
                        ),
                        related_cause_type=(
                            (
                                CauseType.TRAFFIC_SPIKE
                                if name == AgentName.METRIC
                                else CauseType.DEPLOYMENT_REGRESSION
                            )
                            if evidence_match is not None
                            else None
                        ),
                        gaps=(["missing specialist evidence"] if evidence_match is None else []),
                        blocking=evidence_match is None,
                    ),
                )
            )
        proposal = _CoordinatorProposal(
            summary="Coordinator synthesis",
            uncertainty="none",
            proposed_cause=CauseType.DEPLOYMENT_REGRESSION,
        )
        return _SdkTurnResult(drafts, proposal, [])

    orchestrator.agents_runtime = AgentsRcaRuntime(model="fake", turn=turn)
    run = store.create_run(
        RuntimeRun(
            id="run-sdk-phase-boundaries",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    completed = await coordinator.execute(run.id, owner="worker-a")

    checkpoints = {item.completed_phase: item for item in store.list_checkpoints(run.id)}
    model_events = [
        item for item in store.list_events(run.id) if item.event_type.value.startswith("model.")
    ]
    agent_events = [
        item for item in store.list_events(run.id) if item.event_type.value.startswith("agent.")
    ]
    assert completed.status.value == "completed"
    assert live_agent_observations
    assert all(started and not terminal for started, terminal in live_agent_observations)
    assert checkpoints[RuntimePhase.SPECIALIST_ANALYSIS].resume_state.completed_review_ids == []
    assert checkpoints[RuntimePhase.CONFLICT_REVIEW].resume_state.completed_review_ids == []
    assert checkpoints[RuntimePhase.COORDINATION].resume_state.completed_review_ids
    assert [item.event_type.value for item in model_events] == [
        "model.started",
        "model.completed",
        "model.started",
        "model.completed",
        "model.started",
        "model.completed",
    ]
    assert {item.actor_name for item in model_events} == {"CoordinatorAgent"}
    assert agent_events
    assert {item.event_type.value for item in agent_events} >= {
        "agent.started",
        "agent.completed",
    }
    assert all(item.phase is not None and item.actor_name for item in agent_events)
    for started, finished in zip(model_events[::2], model_events[1::2], strict=True):
        phase_agents = [item for item in agent_events if item.phase == started.phase]
        assert (
            min(item.sequence for item in phase_agents if item.event_type.value == "agent.started")
            < started.sequence
        )
        assert (
            max(
                item.sequence
                for item in phase_agents
                if item.event_type.value in {"agent.completed", "agent.failed"}
            )
            > finished.sequence
        )
    projected = RuntimeDiffService(store=store).compare(run.id, run.id)
    assert projected.sections["agents"].left
    assert model_events[1].sequence < checkpoints[RuntimePhase.SPECIALIST_ANALYSIS].event_sequence
    assert (
        checkpoints[RuntimePhase.SPECIALIST_ANALYSIS].event_sequence
        < model_events[3].sequence
        < checkpoints[RuntimePhase.CONFLICT_REVIEW].event_sequence
    )
    assert (
        checkpoints[RuntimePhase.CONFLICT_REVIEW].event_sequence
        < model_events[5].sequence
        < checkpoints[RuntimePhase.COORDINATION].event_sequence
    )
    replay = ReplayService(
        ReplayDependencies(
            store=store,
        )
    ).replay(run.id)
    assert replay.valid is True, replay.validation_errors
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_split_coordination_semantic_error_uses_deterministic_fallback(
    runtime_store,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "present")
    store = runtime_store
    orchestrator = _orchestrator(store.investigation_repository)

    async def turn(**kwargs):
        drafts = []
        for name in kwargs["specialist_names"]:
            prompt = kwargs["specialist_inputs"][name]
            evidence_match = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
            drafts.append(
                _CapturedDraft(
                    agent_name=name,
                    analysis_round=kwargs["analysis_round"],
                    draft=_SpecialistDraft(
                        finding_type=(
                            AgentFindingType.ROOT_CAUSE
                            if evidence_match is not None
                            else AgentFindingType.GAP
                        ),
                        summary=f"{name.value} finding",
                        confidence=0.8,
                        evidence_ids=(
                            [evidence_match.group(1)]
                            if evidence_match is not None
                            else []
                        ),
                        related_cause_type=(
                            CauseType.DEPLOYMENT_REGRESSION
                            if evidence_match is not None
                            else None
                        ),
                        gaps=(
                            []
                            if evidence_match is not None
                            else ["missing evidence"]
                        ),
                        blocking=evidence_match is None,
                    ),
                )
            )
        return _SdkTurnResult(
            drafts,
            _CoordinatorProposal(
                summary="invalid semantic synthesis",
                uncertainty="unsupported root cause",
                proposed_cause=CauseType.DEPLOYMENT_REGRESSION,
            ),
            [],
        )

    def reject_semantics(*_args, **_kwargs):
        raise EvidenceContractError("root_cause_component_mismatch")

    monkeypatch.setattr(
        "backend.diagnosis.agents_runtime.build_hybrid_coordination_review",
        reject_semantics,
    )
    orchestrator.agents_runtime = AgentsRcaRuntime(model="fake", turn=turn)
    run = store.create_run(
        RuntimeRun(
            id="run-semantic-fallback",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    completed = await coordinator.execute(run.id, owner="worker-a")

    record = store.investigation_repository.get("inv-1")
    sdk_executions = [
        item
        for item in store.investigation_repository.list_executions("inv-1")
        if item.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
    ]
    review = store.investigation_repository.get_coordination_review("inv-1")
    phase_failures = [
        item
        for item in store.list_events(run.id)
        if item.event_type.value == "phase.failed"
    ]
    assert completed.status == RuntimeRunStatus.COMPLETED
    assert record.status.value == "completed"
    assert len(sdk_executions) == 1
    assert sdk_executions[0].step_kind == ExecutionStepKind.RESULT_VALIDATION
    assert (
        sdk_executions[0].result_validation_category
        == ResultValidationCategory.SEMANTIC_REFERENCE
    )
    assert sdk_executions[0].failure_category == FailureCategory.INVALID_REFERENCE
    assert review is not None
    assert review.execution_layer == AgentExecutionLayer.CUSTOM
    assert phase_failures == []
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_evidence_time_does_not_consume_frozen_agents_timeout(
    runtime_store,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "present")
    store = runtime_store
    orchestrator = _orchestrator(store.investigation_repository)
    original_collect = orchestrator.coordinator.collect_async
    model_calls = 0

    async def delayed_collect(*args, **kwargs):
        await asyncio.sleep(1.05)
        return await original_collect(*args, **kwargs)

    async def turn(**kwargs):
        nonlocal model_calls
        model_calls += 1
        drafts = []
        for name in kwargs["specialist_names"]:
            prompt = kwargs["specialist_inputs"][name]
            evidence_match = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
            drafts.append(
                _CapturedDraft(
                    agent_name=name,
                    analysis_round=kwargs["analysis_round"],
                    draft=_SpecialistDraft(
                        finding_type=(
                            AgentFindingType.ROOT_CAUSE
                            if evidence_match is not None
                            else AgentFindingType.GAP
                        ),
                        summary=f"{name.value} finding",
                        confidence=0.8,
                        evidence_ids=(
                            [evidence_match.group(1)] if evidence_match is not None else []
                        ),
                        related_cause_type=(
                            CauseType.DEPLOYMENT_REGRESSION if evidence_match is not None else None
                        ),
                        gaps=[] if evidence_match is not None else ["missing evidence"],
                        blocking=evidence_match is None,
                    ),
                )
            )
        return _SdkTurnResult(
            drafts,
            _CoordinatorProposal(
                summary="instant synthesis",
                uncertainty="none",
                proposed_cause=CauseType.DEPLOYMENT_REGRESSION,
            ),
            [],
        )

    orchestrator.coordinator.collect_async = delayed_collect
    orchestrator.agents_runtime = AgentsRcaRuntime(model="fake", turn=turn, timeout_seconds=9)
    run = store.create_run(
        RuntimeRun(
            id="run-evidence-outside-timeout",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            timeout_seconds=1,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    completed = await coordinator.execute(run.id, owner="worker-a")

    model_events = [
        event.event_type.value
        for event in store.list_events(run.id)
        if event.event_type.value.startswith("model.")
    ]
    assert completed.status.value == "completed"
    assert model_calls >= 1
    assert model_events
    assert model_events.count("model.started") == model_events.count("model.completed")
    assert "model.failed" not in model_events
    await coordinator.shutdown()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("slow_phase", "expected_step"),
    [
        ("conflict", ExecutionStepKind.SPECIALIST_COLLECTION),
        ("coordination", ExecutionStepKind.FINAL_SYNTHESIS),
    ],
)
async def test_durable_split_phases_share_deadline_from_first_agents_phase(
    runtime_store,
    monkeypatch,
    slow_phase,
    expected_step,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "present")
    store = runtime_store
    orchestrator = _orchestrator(store.investigation_repository)
    original_collect = orchestrator.coordinator.collect_async
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def delayed_collect(*args, **kwargs):
        await asyncio.sleep(0.5)
        return await original_collect(*args, **kwargs)

    orchestrator.coordinator.collect_async = delayed_collect

    async def slow_turn():
        started.set()
        try:
            await asyncio.sleep(3)
        finally:
            cancelled.set()

    async def turn(**kwargs):
        if kwargs["analysis_round"] == 1:
            await asyncio.sleep(0.15)
            drafts = []
            for name in kwargs["specialist_names"]:
                prompt = kwargs["specialist_inputs"][name]
                evidence_match = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
                draft = _SpecialistDraft(
                    finding_type=(
                        AgentFindingType.ROOT_CAUSE
                        if evidence_match is not None
                        else AgentFindingType.GAP
                    ),
                    summary=f"{name.value} finding",
                    confidence=0.8,
                    evidence_ids=([evidence_match.group(1)] if evidence_match is not None else []),
                    related_cause_type=(
                        (
                            CauseType.TRAFFIC_SPIKE
                            if name == AgentName.METRIC
                            else CauseType.DEPLOYMENT_REGRESSION
                        )
                        if evidence_match is not None
                        else None
                    ),
                    gaps=(["missing specialist evidence"] if evidence_match is None else []),
                    blocking=evidence_match is None,
                )
                drafts.append(
                    _CapturedDraft(
                        agent_name=name,
                        analysis_round=1,
                        draft=draft,
                    )
                )
            return _SdkTurnResult(
                drafts,
                _CoordinatorProposal(
                    summary="initial",
                    uncertainty="metric gap",
                    proposed_cause=CauseType.DEPLOYMENT_REGRESSION,
                ),
                [],
            )
        if kwargs["specialist_names"]:
            if slow_phase == "conflict":
                return await slow_turn()
            await asyncio.sleep(0.1)
            return _SdkTurnResult(
                [
                    _CapturedDraft(
                        agent_name=AgentName.METRIC,
                        analysis_round=2,
                        draft=_SpecialistDraft(
                            finding_type=AgentFindingType.GAP,
                            summary="Metric gap remains non-blocking",
                            confidence=0.5,
                            gaps=["optional saturation metric"],
                            blocking=False,
                        ),
                    )
                ],
                None,
                [],
            )
        return await slow_turn()

    orchestrator.agents_runtime = AgentsRcaRuntime(model="fake", turn=turn, timeout_seconds=1.3)
    run = store.create_run(
        RuntimeRun(
            id=f"run-split-timeout-{slow_phase}",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            timeout_seconds=1.3,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        completed = await asyncio.wait_for(execution, timeout=5)
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await coordinator.shutdown()

    executions = store.investigation_repository.list_executions("inv-1")
    findings = store.investigation_repository.list_agent_findings("inv-1")
    timeout_executions = [
        item for item in executions if item.failure_category == FailureCategory.TIMEOUT
    ]
    checkpoint_phases = {
        checkpoint.completed_phase for checkpoint in store.list_checkpoints(run.id)
    }
    completed_phase_events = {
        event.phase
        for event in store.list_events(run.id)
        if event.event_type.value == "phase.completed"
    }
    assert completed.status.value == "completed"
    assert started.is_set()
    assert cancelled.is_set()
    assert len(findings) >= 3
    assert len(timeout_executions) == 1
    assert timeout_executions[0].step_kind == expected_step
    assert {
        RuntimePhase.CONFLICT_REVIEW,
        RuntimePhase.COORDINATION,
    } <= checkpoint_phases
    assert {
        RuntimePhase.CONFLICT_REVIEW,
        RuntimePhase.COORDINATION,
    } <= completed_phase_events
    assert store.investigation_repository.get_coordination_review("inv-1") is None
    assert [attempt.status.value for attempt in store.list_attempts(run.id)] == ["completed"]


@pytest.mark.anyio
async def test_conflict_timeout_checkpoint_resumes_without_more_model_calls(
    runtime_store,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "present")
    store = runtime_store
    orchestrator = _orchestrator(store.investigation_repository)
    model_calls: list[tuple[int, tuple[AgentName, ...]]] = []
    hang_conflicts = True

    async def turn(**kwargs):
        nonlocal hang_conflicts
        names = tuple(kwargs["specialist_names"])
        model_calls.append((kwargs["analysis_round"], names))
        if hang_conflicts and kwargs["analysis_round"] == 2 and names:
            await asyncio.Event().wait()
        drafts = []
        for name in names:
            prompt = kwargs["specialist_inputs"][name]
            evidence_match = re.search(r'"id"\s*:\s*"([^"]+)"', prompt)
            drafts.append(
                _CapturedDraft(
                    agent_name=name,
                    analysis_round=kwargs["analysis_round"],
                    draft=_SpecialistDraft(
                        finding_type=(
                            AgentFindingType.ROOT_CAUSE
                            if evidence_match is not None
                            else AgentFindingType.GAP
                        ),
                        summary=f"{name.value} conflict",
                        confidence=0.8,
                        evidence_ids=(
                            [evidence_match.group(1)] if evidence_match is not None else []
                        ),
                        related_cause_type=(
                            (
                                CauseType.TRAFFIC_SPIKE
                                if name == AgentName.METRIC
                                else CauseType.DEPLOYMENT_REGRESSION
                            )
                            if evidence_match is not None
                            else None
                        ),
                        gaps=[] if evidence_match is not None else ["missing evidence"],
                        blocking=evidence_match is None,
                    ),
                )
            )
        return _SdkTurnResult(
            drafts,
            _CoordinatorProposal(
                summary="conflicting specialists",
                uncertainty="metric conflict",
                proposed_cause=CauseType.DEPLOYMENT_REGRESSION,
            ),
            [],
        )

    runtime = AgentsRcaRuntime(model="fake", turn=turn, timeout_seconds=1)
    clone_timeouts: list[float | None] = []
    original_clone = runtime.clone_for_run

    def recording_clone(**kwargs):
        clone_timeouts.append(kwargs.get("timeout_seconds"))
        return original_clone(**kwargs)

    runtime.clone_for_run = recording_clone
    orchestrator.agents_runtime = runtime
    run = store.create_run(
        RuntimeRun(
            id="run-conflict-timeout-resume",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            timeout_seconds=1,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
        fault_injector=DeterministicFaultInjector({"before_phase_start": 6}),
    )

    crashed = await coordinator.execute(run.id, owner="worker-a")

    assert crashed.status == RuntimeRunStatus.RUNNING
    assert store.get_run(run.id).current_phase == RuntimePhase.CONFLICT_REVIEW
    conflict_events = [
        event.event_type.value
        for event in store.list_events(run.id)
        if event.phase == RuntimePhase.CONFLICT_REVIEW
    ]
    assert "phase.completed" in conflict_events
    assert "phase.skipped" not in conflict_events
    calls_after_checkpoint = len(model_calls)
    runtime.timeout_seconds = 9
    if hasattr(store, "_runs"):
        store._runs[run.id] = store._runs[run.id].model_copy(
            update={"lease_expires_at": datetime(2026, 7, 16, tzinfo=UTC)}
        )
    else:
        from backend.db.schema import runtime_runs

        with store.engine.begin() as connection:
            connection.execute(
                runtime_runs.update()
                .where(runtime_runs.c.id == run.id)
                .values(lease_expires_at="2026-07-16T00:00:00+00:00")
            )
    interrupted = coordinator.audit_expired_leases(datetime(2026, 7, 17, tzinfo=UTC))[0]
    assert interrupted.status == RuntimeRunStatus.INTERRUPTED

    completed = await coordinator.resume(run.id, owner="worker-b")

    executions = store.investigation_repository.list_executions("inv-1")
    timeout_executions = [
        item for item in executions if item.failure_category == FailureCategory.TIMEOUT
    ]
    assert completed.status == RuntimeRunStatus.COMPLETED
    assert len(model_calls) == calls_after_checkpoint
    assert clone_timeouts == [1, 1]
    assert len(timeout_executions) == 1
    assert timeout_executions[0].step_kind == ExecutionStepKind.SPECIALIST_COLLECTION
    assert store.investigation_repository.list_tasks("inv-1")
    assert store.investigation_repository.list_agent_findings("inv-1")
    assert store.investigation_repository.get_coordination_review("inv-1") is None

    new_run = store.create_run(
        RuntimeRun(
            id="run-new-timeout-config",
            investigation_id="inv-1",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.MANUAL_RERUN,
            parent_run_id=run.id,
            timeout_seconds=runtime.timeout_seconds,
        )
    )
    assert store.get_run(new_run.id).timeout_seconds == 9
    assert clone_timeouts == [1, 1]
    await coordinator.shutdown()


def test_evidence_collection_uses_bounded_parallel_provider_io() -> None:
    barrier = Barrier(3)
    lock = Lock()
    active = 0
    peak = 0

    class BlockingProvider:
        provider = EvidenceProvider.LOG

        def collect(self, _event):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            return ProviderResult(provider=self.provider)

    coordinator = DiagnosisCoordinator(
        ProviderRegistry([BlockingProvider() for _index in range(6)])
    )
    event = load_incident_case("deployment_regression").model_copy(
        update={"source": IncidentSource.MANUAL}
    )

    context = asyncio.run(coordinator.collect_async(event, max_parallel_steps=3))

    assert peak == 3
    assert len(context.provider_results) >= 6


def test_runtime_coordinator_drives_real_phase_executor_without_duplicate_session() -> None:
    orchestrator = _orchestrator()
    event = load_incident_case("deployment_regression")
    investigation = orchestrator.repository.save(InvestigationRecord(id="inv-runtime", event=event))
    store = InMemoryRuntimeStore(orchestrator.repository)
    run = store.create_run(
        RuntimeRun(
            id="run-runtime",
            investigation_id=investigation.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    async def execute_and_shutdown():
        completed_run = await coordinator.execute(run.id, owner="worker-a")
        await coordinator.shutdown()
        return completed_run

    completed = asyncio.run(execute_and_shutdown())

    assert completed.status.value == "completed"
    assert [item.id for item in orchestrator.repository.list()] == [investigation.id]


def test_real_runtime_phase_overlaps_log_metric_deploy_and_isolates_failure() -> None:
    barrier = Barrier(3)
    lock = Lock()
    active = 0
    peak = 0

    class SpecialistProvider:
        def __init__(self, provider: EvidenceProvider, *, fail: bool = False) -> None:
            self.provider = provider
            self.fail = fail

        def collect(self, _event):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=2)
            with lock:
                active -= 1
            if self.fail:
                raise RuntimeError("isolated metric failure")
            return ProviderResult(provider=self.provider)

    providers = ProviderRegistry(
        [
            SpecialistProvider(EvidenceProvider.LOG),
            SpecialistProvider(EvidenceProvider.METRIC, fail=True),
            SpecialistProvider(EvidenceProvider.DEPLOY),
        ]
    )
    repository = InMemoryInvestigationRepository()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )
    investigation = repository.save(
        InvestigationRecord(
            id="inv-specialist-parallel",
            event=load_incident_case("deployment_regression").model_copy(
                update={"source": IncidentSource.MANUAL}
            ),
        )
    )
    store = InMemoryRuntimeStore(repository)
    run = store.create_run(
        RuntimeRun(
            id="run-specialist-parallel",
            investigation_id=investigation.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator, max_parallel_steps_per_run=3),
        heartbeat_seconds=1,
    )

    async def execute_and_shutdown():
        completed = await coordinator.execute(run.id, owner="worker-a")
        await coordinator.shutdown()
        return completed

    completed = asyncio.run(execute_and_shutdown())
    provider_results = repository.get(investigation.id).provider_results

    assert completed.status.value == "completed"
    assert peak == 3
    assert {
        result.provider: result.status
        for result in provider_results
        if result.provider
        in {EvidenceProvider.LOG, EvidenceProvider.METRIC, EvidenceProvider.DEPLOY}
    } == {
        EvidenceProvider.LOG: ProviderStatus.SUCCESS,
        EvidenceProvider.METRIC: ProviderStatus.FAILED,
        EvidenceProvider.DEPLOY: ProviderStatus.SUCCESS,
    }


@pytest.mark.anyio
async def test_running_cancel_discards_real_provider_results_at_phase_boundary() -> None:
    lock = Lock()
    release = Event()
    active = 0

    class BlockingProvider:
        def __init__(self, provider: EvidenceProvider) -> None:
            self.provider = provider

        def collect(self, _event):
            nonlocal active
            with lock:
                active += 1
            release.wait(timeout=2)
            return ProviderResult(provider=self.provider)

    providers = ProviderRegistry(
        [
            BlockingProvider(EvidenceProvider.LOG),
            BlockingProvider(EvidenceProvider.METRIC),
            BlockingProvider(EvidenceProvider.DEPLOY),
        ]
    )
    repository = InMemoryInvestigationRepository()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )
    investigation = repository.save(
        InvestigationRecord(
            id="inv-provider-cancel",
            event=load_incident_case("deployment_regression").model_copy(
                update={"source": IncidentSource.MANUAL}
            ),
        )
    )
    store = InMemoryRuntimeStore(repository)
    run = store.create_run(
        RuntimeRun(
            id="run-provider-cancel",
            investigation_id=investigation.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    while True:
        with lock:
            if active == 3:
                break
        await asyncio.sleep(0)
    await coordinator.request_cancel(run.id)
    release.set()
    cancelled = await execution

    assert cancelled.status.value == "cancelled"
    assert repository.get(investigation.id).provider_results == []
    assert [item.completed_phase for item in store.list_checkpoints(run.id)] == [
        RuntimePhase.INTAKE
    ]
    assert [event.event_type.value for event in store.list_events(run.id)].count(
        "run.cancelled"
    ) == 1
    await coordinator.shutdown()


def test_durable_phase_failure_does_not_leak_uncheckpointed_business_writes() -> None:
    orchestrator = _orchestrator()
    investigation = orchestrator.repository.save(
        InvestigationRecord(
            id="inv-atomic",
            event=load_incident_case("deployment_regression"),
        )
    )
    hits = 0

    def fail_second_phase(point: str) -> None:
        nonlocal hits
        if point != "after_business_before_event":
            return
        hits += 1
        if hits == 2:
            raise RuntimeError("injected persistence failure")

    store = InMemoryRuntimeStore(
        orchestrator.repository,
        failpoint=fail_second_phase,
    )
    run = store.create_run(
        RuntimeRun(
            id="run-atomic",
            investigation_id=investigation.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    async def execute_and_shutdown() -> None:
        with pytest.raises(RuntimePersistenceError):
            await coordinator.execute(run.id, owner="worker-a")
        await coordinator.shutdown()

    asyncio.run(execute_and_shutdown())

    assert orchestrator.repository.get(investigation.id).evidence == []
    assert store.get_run(run.id).current_phase == RuntimePhase.INTAKE
    assert len(store.list_checkpoints(run.id)) == 1


def test_sqlite_runtime_coordinator_commits_real_phase_outputs(tmp_path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'phase-runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    orchestrator = _orchestrator(repository)
    investigation = repository.save(
        InvestigationRecord(
            id="inv-sqlite-runtime",
            event=load_incident_case("traffic_spike"),
        )
    )
    store = SQLiteRuntimeStore(engine, repository)
    run = store.create_run(
        RuntimeRun(
            id="run-sqlite-runtime",
            investigation_id=investigation.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator),
        heartbeat_seconds=1,
    )

    async def execute_and_shutdown():
        completed_run = await coordinator.execute(run.id, owner="worker-a")
        await coordinator.shutdown()
        return completed_run

    completed = asyncio.run(execute_and_shutdown())

    assert completed.status.value == "completed"
    assert repository.get(investigation.id).report is not None
    assert len(store.list_checkpoints(run.id)) == len(RuntimePhase)
    engine.dispose()
