from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config.settings import AppSettings, StorageSettings
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.actions import (
    ActionRiskLevel,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    FindingActor,
)
from backend.domain.evidence import EvidenceStatus
from backend.domain.multi_agent import (
    DiagnosticStatus,
    ExecutionContractVersion,
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
)
from backend.domain.reports import IncidentReport
from backend.domain.runtime import (
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRunStatus,
)
from backend.main import app
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.faults import DeterministicFaultInjector
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import PhaseCommit, PhaseInput
from backend.runtime.store import RuntimePersistenceError
from backend.services.container import AppContainer, reset_container
from backend.services.v11_projection import (
    V11ProjectionIntegrityError,
    ensure_v11_projection_owner,
)
from tests.reports.test_v11_product_integration import (
    _candidate,
    _event,
    _evidence,
    _review,
    _run_summary,
)
from tests.runtime.test_v11_isolation_red import _v11_run


def _owned_review(
    investigation_id: str,
    runtime_run_id: str,
    candidate,
    *,
    diagnostic_status: DiagnosticStatus = DiagnosticStatus.COMPLETE,
    run_status: MultiAgentRunStatus = MultiAgentRunStatus.COMPLETED,
    inconclusive: bool = False,
):
    review = _review(candidate, inconclusive=inconclusive)
    return review.model_copy(
        update={
            "investigation_id": investigation_id,
            "runtime_run_id": runtime_run_id,
            "diagnostic_status": diagnostic_status,
            "run_status": run_status,
            "critic_assessments": [
                item.model_copy(update={"runtime_run_id": runtime_run_id})
                for item in review.critic_assessments
            ],
        }
    )


def _owned_run_summary(
    runtime_run_id: str,
    diagnostic_status: DiagnosticStatus,
    *,
    status: MultiAgentRunStatus = MultiAgentRunStatus.COMPLETED,
):
    return _run_summary(diagnostic_status).model_copy(
        update={
            "runtime_run_id": runtime_run_id,
            "status": status,
        }
    )


def _persisted_v11_run(run_id: str, investigation_id: str):
    contract_container = AppContainer(
        AppSettings(storage=StorageSettings(url="memory://"))
    )
    contract_container.orchestrator.agents_runtime = SimpleNamespace(
        model_provider=ModelProvider.OPENAI,
        _model_name="gpt-test",
        prompt_version="v11-test",
    )
    contract_container.repository.save(
        InvestigationRecord(id=investigation_id, event=_event())
    )
    persisted = contract_container.create_runtime_run(
        investigation_id,
        strategy=InvestigationStrategy.ADAPTIVE,
        run_reason="initial",
        execution_contract_version=ExecutionContractVersion.V11,
    )
    execution_contract = persisted.execution_contract
    contract_container.close()
    return _v11_run(
        run_id=run_id,
        investigation_id=investigation_id,
        model_name=execution_contract["model_name"],
        prompt_version=execution_contract["prompt_version"],
        tool_budget=execution_contract["tool_budget"],
        token_budget=execution_contract["token_budget"],
        timeout_seconds=execution_contract["timeout_seconds"],
        execution_contract=execution_contract,
    )


def _completed_v11_runtime_run(
    container: AppContainer, run_id: str, investigation_id: str
):
    return container.runtime_store.create_run(
        _persisted_v11_run(run_id, investigation_id).model_copy(
            update={"status": RuntimeRunStatus.COMPLETED}
        )
    )


@pytest.mark.anyio
async def test_v11_report_generation_is_atomic_for_memory_and_sqlite(runtime_store):
    """报告和 action 必须与 REPORT_GENERATION checkpoint 共用一个事务。"""
    store = runtime_store
    repository = store.investigation_repository
    run = store.create_run(
        _v11_run(
            run_id=f"run-report-atomic-{type(store).__name__}",
            current_phase=RuntimePhase.RESULT_VALIDATION,
        )
    )
    evidence = _evidence().model_copy(update={"runtime_run_id": run.id})
    candidate = _candidate()
    review = _review(candidate).model_copy(
        update={
            "investigation_id": "inv-1",
            "runtime_run_id": run.id,
            "critic_assessments": [
                item.model_copy(update={"runtime_run_id": run.id})
                for item in _review(candidate).critic_assessments
            ],
        }
    )
    run_summary = _run_summary(review.diagnostic_status).model_copy(
        update={"runtime_run_id": run.id}
    )
    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "active_runtime_run_id": run.id,
                "status": InvestigationStatus.COMPLETED,
                "evidence": [evidence],
                "multi_agent_run": run_summary,
            }
        )
    )
    repository.save_coordination_review(review)
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-report-atomic",
        expected_status=RuntimeRunStatus.CREATED,
    )
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=ProviderRegistry([]),
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(ProviderRegistry([])),
        action_planner=ActionPlanner(),
        agents_runtime=None,
        v11_runtime=SimpleNamespace(runtime_run_id=run.id, remaining_token_budget=1000),
    )
    executor = DiagnosisPhaseExecutor(orchestrator)
    executor._is_durable_session = True

    output = await executor.execute_phase(
        PhaseInput(
            run_id=run.id,
            attempt_id=attempt.id,
            phase=RuntimePhase.REPORT_GENERATION,
            resume_state=RuntimeResumeState(),
            investigation_id="inv-1",
            strategy=run.strategy,
            execution_contract_version=ExecutionContractVersion.V11,
            tool_budget=run.tool_budget,
            token_budget=run.token_budget,
            timeout_seconds=120,
        )
    )

    persisted_before_commit = repository.get("inv-1")
    assert persisted_before_commit.report is None
    assert persisted_before_commit.actions == []

    store.fault_injector = DeterministicFaultInjector(
        {"persistence_mid_transaction": 1}
    )
    with pytest.raises(RuntimePersistenceError):
        store.commit_phase(
            PhaseCommit(
                run_id=run.id,
                attempt_id=attempt.id,
                lease_owner="worker-report-atomic",
                lease_version=leased.lease_version,
                phase=RuntimePhase.REPORT_GENERATION,
                business_mutation=output.business_mutation,
                safe_payload=output.safe_payload,
                resume_state=output.resume_state,
                expected_previous_phase=RuntimePhase.RESULT_VALIDATION,
            )
        )

    persisted_after_failure = repository.get("inv-1")
    assert persisted_after_failure.report is None
    assert persisted_after_failure.actions == []
    assert store.get_run(run.id).latest_checkpoint_id is None


@pytest.mark.parametrize(
    ("diagnostic_status", "run_status"),
    [
        (DiagnosticStatus.COMPLETE, MultiAgentRunStatus.PARTIAL),
        (DiagnosticStatus.PARTIAL, MultiAgentRunStatus.COMPLETED),
    ],
)
def test_v11_status_matrix_rejects_crossed_diagnostic_and_run_status(
    diagnostic_status, run_status
):
    """相互一致但语义交叉的最终状态不得激活动作或诊断。"""
    candidate = _candidate()
    review = _owned_review(
        "inv-v11-product",
        "run-v11-status-matrix",
        candidate,
        diagnostic_status=diagnostic_status,
        run_status=run_status,
    )
    run = _owned_run_summary(
        "run-v11-status-matrix",
        diagnostic_status,
        status=run_status,
    )

    with pytest.raises(ValueError, match="status"):
        ActionPlanner().plan_v11(_event(), [_evidence()], review, run)
    with pytest.raises(ValueError, match="status"):
        ReportGenerator().generate(
            "inv-v11-product",
            _event(),
            [_evidence().model_copy(update={"runtime_run_id": run.runtime_run_id})],
            [],
            coordination_review=review,
            multi_agent_run=run,
        )


def test_runtime_api_rejects_v10_run_on_active_v11_projection():
    """旧 API 默认值不得把 active V11 投影重新污染成 V10。"""
    container = reset_container()
    record = container.repository.save(
        InvestigationRecord(id="inv-active-v11", event=_event())
    )
    run = container.runtime_store.create_run(
        _v11_run(run_id="run-active-v11", status=RuntimeRunStatus.COMPLETED).model_copy(
            update={"investigation_id": record.id}
        )
    )
    container.repository.save(record.model_copy(update={"active_runtime_run_id": run.id}))

    with TestClient(app) as client:
        response = client.post(
            f"/investigations/{record.id}/runtime-runs",
            json={
                "strategy": "fixed",
                "run_reason": "manual_rerun",
                "parent_run_id": run.id,
            },
        )

    assert response.status_code == 409
    assert "V10" in response.json()["detail"]


def test_runtime_api_defaults_to_v11_when_product_runtime_is_available(monkeypatch):
    container = reset_container()
    container.orchestrator.v11_runtime = object()
    container.repository.save(InvestigationRecord(id="inv-default-v11", event=_event()))
    seen = {}

    def fake_create_runtime_run(
        investigation_id,
        *,
        strategy,
        run_reason,
        parent_run_id=None,
        model_provider=None,
        model_name=None,
        prompt_version=None,
        execution_contract_version=None,
    ):
        del (
            strategy,
            run_reason,
            parent_run_id,
            model_provider,
            model_name,
            prompt_version,
        )
        seen["version"] = execution_contract_version
        return _v11_run(
            run_id="run-default-v11",
            investigation_id=investigation_id,
        )

    async def fake_start(run_id):
        del run_id

    async def fake_writer_start():
        return None

    monkeypatch.setattr(container, "create_runtime_run", fake_create_runtime_run)
    monkeypatch.setattr(container.runtime_manager, "start", fake_start)
    monkeypatch.setattr(container.runtime_writer, "start", fake_writer_start)
    with TestClient(app) as client:
        response = client.post(
            "/investigations/inv-default-v11/runtime-runs",
            json={"strategy": "adaptive", "run_reason": "initial"},
        )

    assert response.status_code == 202, response.text
    assert seen["version"] == ExecutionContractVersion.V11


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/events",
            {
                "source": "webhook",
                "service": "checkout-service",
                "environment": "prod",
                "severity": "warning",
                "title": "Latency increased",
                "description": "checkout-service latency increased",
                "started_at": "2026-08-09T00:00:00Z",
            },
        ),
        (
            "/investigations/manual",
            {
                "text": "checkout-service latency increased",
                "service": "checkout-service",
                "environment": "prod",
            },
        ),
    ],
)
def test_product_entries_select_v11_when_agent_runtime_is_available(
    monkeypatch, path, payload
):
    container = reset_container()
    container.orchestrator.v11_runtime = object()
    record = container.repository.save(
        InvestigationRecord(id=f"inv-entry-{path[1]}", event=_event())
    )
    seen = {}

    async def fake_run(
        event, *, strategy=None, execution_contract_version=None, runtime_run_id=None
    ):
        seen["version"] = execution_contract_version
        return record

    monkeypatch.setattr(container, "run_investigation", fake_run)
    with TestClient(app) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 200, response.text
    assert seen["version"] == ExecutionContractVersion.V11


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/events",
            {
                "source": "webhook",
                "service": "checkout-service",
                "environment": "prod",
                "severity": "warning",
                "title": "Latency increased",
                "description": "checkout-service latency increased",
                "started_at": "2026-08-09T00:00:00Z",
            },
        ),
        (
            "/investigations/manual",
            {
                "text": "checkout-service latency increased",
                "service": "checkout-service",
                "environment": "prod",
            },
        ),
    ],
)
def test_v11_product_entry_summary_preserves_safe_lead_and_critic(
    monkeypatch, path, payload
):
    container = reset_container()
    record_id = f"inv-summary-{path[1]}"
    container.repository.save(InvestigationRecord(id=record_id, event=_event()))
    container.orchestrator.agents_runtime = SimpleNamespace(
        model_provider=ModelProvider.OPENAI,
        _model_name="gpt-test",
        prompt_version="v11",
    )
    container.orchestrator.v11_runtime = SimpleNamespace()
    run = _completed_v11_runtime_run(container, f"run-summary-{record_id}", record_id)
    candidate = _candidate()
    review = _review(candidate).model_copy(
        update={
            "investigation_id": record_id,
            "runtime_run_id": run.id,
            "critic_assessments": [
                item.model_copy(update={"runtime_run_id": run.id})
                for item in _review(candidate).critic_assessments
            ],
        }
    )
    record = container.repository.save(
        InvestigationRecord(
            id=record_id,
            event=_event(),
            status=InvestigationStatus.COMPLETED,
            multi_agent_run=_run_summary(review.diagnostic_status).model_copy(
                update={"runtime_run_id": run.id}
            ),
            active_runtime_run_id=run.id,
        )
    )
    container.repository.save_coordination_review(review)

    async def fake_run(
        event, *, strategy=None, execution_contract_version=None, runtime_run_id=None
    ):
        del event, strategy, execution_contract_version, runtime_run_id
        return record

    monkeypatch.setattr(container, "run_investigation", fake_run)
    with TestClient(app) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lead_decision"]["action"] == "conclude"
    assert body["critic_assessments"][0]["candidate_id"] == candidate.id
    assert "private chain of thought" not in response.text
    container.close()


def test_v11_workbench_uses_private_safe_public_projection():
    container = reset_container()
    record_id = "inv-v11-public-projection"
    container.repository.save(InvestigationRecord(id=record_id, event=_event()))
    container.orchestrator.agents_runtime = SimpleNamespace(
        model_provider=ModelProvider.OPENAI,
        _model_name="gpt-test",
        prompt_version="v11",
    )
    container.orchestrator.v11_runtime = SimpleNamespace()
    run = _completed_v11_runtime_run(container, "run-v11-public-projection", record_id)
    evidence = _evidence().model_copy(update={"runtime_run_id": run.id})
    candidate = _candidate().model_copy(
        update={
            "summary": "private chain of thought: candidate summary",
            "rationale": "system prompt: hidden rationale",
        }
    )
    review = _review(candidate).model_copy(
        update={
            "investigation_id": record_id,
            "runtime_run_id": run.id,
            "summary": "private chain of thought: review summary",
            "critic_assessments": [
                item.model_copy(
                    update={
                        "runtime_run_id": run.id,
                        "summary": "system prompt: critic summary",
                        "checks": [
                            check.model_copy(
                                update={"summary": "private chain of thought: check"}
                            )
                            for check in item.checks
                        ],
                    }
                )
                for item in _review(candidate).critic_assessments
            ],
            "lead_decision": _review(candidate).lead_decision.model_copy(
                update={"summary": "system prompt: lead decision"}
            ),
        }
    )
    record = container.repository.save(
        InvestigationRecord(
            id=record_id,
            event=_event(),
            status=InvestigationStatus.COMPLETED,
            evidence=[evidence],
            multi_agent_run=_run_summary(review.diagnostic_status).model_copy(
                update={"runtime_run_id": run.id}
            ),
            active_runtime_run_id=run.id,
        )
    )
    finding = AgentFinding(
        investigation_id=record.id,
        agent_name=FindingActor.LOG,
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary="private chain of thought: finding summary",
        rationale="system prompt: finding rationale",
        confidence=0.8,
        evidence_ids=[evidence.id],
        runtime_run_id=run.id,
    )
    container.repository.save_agent_findings(record.id, [finding])
    container.repository.save_coordination_review(review)

    with TestClient(app) as client:
        response = client.get(f"/investigations/{record.id}/rca-workbench")

    assert response.status_code == 200, response.text
    body = response.json()
    serialized = response.text
    assert "private chain of thought" not in serialized
    assert "system prompt" not in serialized
    assert body["findings"][0]["summary"] == "[内部推理内容已省略]"
    assert body["candidates"][0]["summary"] == "[内部推理内容已省略]"
    assert body["coordination_review"]["lead_decision"]["summary"] == (
        "[内部推理内容已省略]"
    )
    assert all(
        node["label"] != "private chain of thought: check"
        for node in body["graph_seed"]["nodes"]
    )


def test_v11_action_planner_rejects_inconsistent_final_statuses():
    """Action/verification 只能由状态和最终 Lead decision 一致的 review 生成。"""
    candidate = _candidate()
    review = _review(candidate)
    run = _run_summary(DiagnosticStatus.COMPLETE)

    with pytest.raises(ValueError, match="diagnostic status"):
        ActionPlanner().plan_v11(
            _event(),
            [_evidence()],
            review.model_copy(update={"diagnostic_status": DiagnosticStatus.PARTIAL}),
            run,
        )

    with pytest.raises(ValueError, match="run status"):
        ActionPlanner().plan_v11(
            _event(),
            [_evidence()],
            review.model_copy(update={"run_status": MultiAgentRunStatus.PARTIAL}),
            run,
        )

    inconclusive_decision = review.lead_decision.model_copy(
        update={"action": LeadAction.INCONCLUSIVE, "candidate_ids": []}
    )
    with pytest.raises(ValueError, match="Lead decision"):
        ActionPlanner().plan_v11(
            _event(),
            [_evidence()],
            review.model_copy(
                update={
                    "lead_decision": inconclusive_decision,
                    "diagnostic_status": DiagnosticStatus.COMPLETE,
                }
            ),
            run,
        )

    partial_review = review.model_copy(
        update={
            "diagnostic_status": DiagnosticStatus.PARTIAL,
            "run_status": MultiAgentRunStatus.PARTIAL,
        }
    )
    partial_run = run.model_copy(
        update={
            "diagnostic_status": DiagnosticStatus.PARTIAL,
            "status": MultiAgentRunStatus.PARTIAL,
        }
    )
    actions, verifications = ActionPlanner().plan_v11(
        _event(), [_evidence()], partial_review, partial_run
    )
    assert actions
    assert verifications


@pytest.mark.parametrize("artifact", ["evidence", "action", "verification"])
def test_v11_projection_rejects_ownerless_business_artifacts(artifact):
    """V11 的 evidence/action/verification 都必须属于同一个 durable run。"""
    container = reset_container()
    container.repository.save(InvestigationRecord(id="inv-1", event=_event()))
    container.orchestrator.agents_runtime = SimpleNamespace(
        model_provider=ModelProvider.OPENAI,
        _model_name="gpt-test",
        prompt_version="v11",
    )
    container.orchestrator.v11_runtime = SimpleNamespace()
    run = container.create_runtime_run(
        "inv-1",
        strategy=InvestigationStrategy.ADAPTIVE,
        run_reason="initial",
        execution_contract_version=ExecutionContractVersion.V11,
    )
    values = {
        "id": "inv-1",
        "event": _event(),
        "active_runtime_run_id": run.id,
        "multi_agent_run": _run_summary(DiagnosticStatus.COMPLETE).model_copy(
            update={"runtime_run_id": run.id}
        ),
    }
    if artifact == "evidence":
        values["evidence"] = [_evidence().model_copy(update={"runtime_run_id": None})]
    elif artifact == "action":
        values["actions"] = [
            RecommendedAction(
                action_type=ActionType.CHECK,
                title="Inspect signal",
                description="Read-only inspection.",
                risk_level=ActionRiskLevel.READ_ONLY,
                requires_approval=False,
                supporting_evidence_ids=["ev-deploy"],
            )
        ]
    else:
        values["verification_suggestions"] = [
            VerificationSuggestion(
                title="Verify signal",
                description="Verify the committed signal.",
                expected_signal="signal remains stable",
            )
        ]
    with pytest.raises(ValueError, match="requires runtime_run_id"):
        InvestigationRecord(**values)

    # 用 model_construct 模拟旧数据/绕过写入校验后的内存对象，验证公开 guard 仍 fail closed。
    record = InvestigationRecord.model_construct(**values)

    with pytest.raises(V11ProjectionIntegrityError, match="owner"):
        ensure_v11_projection_owner(container.repository, container.runtime_store, record)
    container.close()


@pytest.mark.parametrize(
    "invalid_case",
    [
        "diagnostic_status",
        "run_status",
        "crossed_complete_partial",
        "crossed_partial_complete",
        "inconclusive_lead",
        "missing_active_owner",
        "missing_latest_run",
    ],
)
def test_v11_projection_guard_rejects_invalid_reloaded_payload(
    runtime_store, invalid_case
):
    """memory/SQLite reload 后的无效 Agent 投影必须 fail closed。"""
    repository = runtime_store.investigation_repository
    run = runtime_store.create_run(
        _persisted_v11_run(
            f"run-invalid-projection-{type(runtime_store).__name__}-{invalid_case}",
            "inv-1",
        ).model_copy(update={"status": RuntimeRunStatus.COMPLETED})
    )
    candidate = _candidate()
    review = _owned_review("inv-1", run.id, candidate)
    run_summary = _owned_run_summary(run.id, DiagnosticStatus.COMPLETE)
    active_runtime_run_id = run.id

    if invalid_case == "diagnostic_status":
        run_summary = _owned_run_summary(run.id, DiagnosticStatus.INCONCLUSIVE)
    elif invalid_case == "run_status":
        run_summary = _owned_run_summary(
            run.id,
            DiagnosticStatus.COMPLETE,
            status=MultiAgentRunStatus.PARTIAL,
        )
    elif invalid_case == "crossed_complete_partial":
        review = _owned_review(
            "inv-1",
            run.id,
            candidate,
            diagnostic_status=DiagnosticStatus.COMPLETE,
            run_status=MultiAgentRunStatus.PARTIAL,
        )
        run_summary = _owned_run_summary(
            run.id,
            DiagnosticStatus.COMPLETE,
            status=MultiAgentRunStatus.PARTIAL,
        )
    elif invalid_case == "crossed_partial_complete":
        review = _owned_review(
            "inv-1",
            run.id,
            candidate,
            diagnostic_status=DiagnosticStatus.PARTIAL,
            run_status=MultiAgentRunStatus.COMPLETED,
        )
        run_summary = _owned_run_summary(
            run.id,
            DiagnosticStatus.PARTIAL,
            status=MultiAgentRunStatus.COMPLETED,
        )
    elif invalid_case == "inconclusive_lead":
        review = _owned_review(
            "inv-1",
            run.id,
            candidate,
            diagnostic_status=DiagnosticStatus.INCONCLUSIVE,
        )
        run_summary = _owned_run_summary(run.id, DiagnosticStatus.INCONCLUSIVE)
    elif invalid_case == "missing_active_owner":
        active_runtime_run_id = None
    else:
        run_summary = None

    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "active_runtime_run_id": active_runtime_run_id,
                "multi_agent_run": run_summary,
            }
        )
    )
    repository.save_coordination_review(review)
    reloaded = repository.get("inv-1")

    with pytest.raises(V11ProjectionIntegrityError):
        ensure_v11_projection_owner(repository, runtime_store, reloaded)


@pytest.mark.parametrize(
    "durable_status",
    [
        RuntimeRunStatus.CREATED,
        RuntimeRunStatus.FAILED,
        RuntimeRunStatus.CANCELLED,
        # lease 过期在 durable 状态中收敛为 interrupted。
        RuntimeRunStatus.INTERRUPTED,
    ],
)
def test_v11_projection_guard_rejects_nonterminal_durable_run_after_reload(
    runtime_store, durable_status
):
    """非 COMPLETED 的 durable RuntimeRun 不得公开已收敛的 Agent 投影。"""
    repository = runtime_store.investigation_repository
    run = runtime_store.create_run(
        _persisted_v11_run(
            f"run-nonterminal-{type(runtime_store).__name__}-{durable_status}",
            "inv-1",
        ).model_copy(update={"status": durable_status})
    )
    candidate = _candidate()
    review = _owned_review("inv-1", run.id, candidate)
    run_summary = _owned_run_summary(run.id, DiagnosticStatus.COMPLETE)
    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "active_runtime_run_id": run.id,
                "status": InvestigationStatus.COMPLETED,
                "multi_agent_run": run_summary,
            }
        )
    )
    repository.save_coordination_review(review)

    with pytest.raises(V11ProjectionIntegrityError, match="final status contract"):
        ensure_v11_projection_owner(repository, runtime_store, repository.get("inv-1"))


@pytest.mark.parametrize(
    "report_shape",
    ["foreign_diagnosis", "foreign_alternative", "inconclusive_diagnosis"],
)
def test_v11_projection_guard_rejects_report_candidate_refs_not_led(
    runtime_store, report_shape
):
    """公开报告的候选引用必须与 Lead 的最终裁决保持同一投影。"""
    repository = runtime_store.investigation_repository
    run = runtime_store.create_run(
        _persisted_v11_run(
            f"run-report-refs-{type(runtime_store).__name__}-{report_shape}",
            "inv-1",
        ).model_copy(update={"status": RuntimeRunStatus.COMPLETED})
    )
    candidate = _candidate()
    inconclusive = report_shape == "inconclusive_diagnosis"
    review = _owned_review(
        "inv-1",
        run.id,
        candidate,
        diagnostic_status=(
            DiagnosticStatus.INCONCLUSIVE
            if inconclusive
            else DiagnosticStatus.COMPLETE
        ),
        inconclusive=inconclusive,
    )
    diagnostic_status = (
        DiagnosticStatus.INCONCLUSIVE if inconclusive else DiagnosticStatus.COMPLETE
    )
    run_summary = _owned_run_summary(run.id, diagnostic_status)
    foreign = candidate.model_copy(update={"id": "candidate-foreign"})
    report = IncidentReport(
        investigation_id="inv-1",
        summary="safe report",
        markdown="safe report",
        diagnoses=[foreign] if report_shape != "foreign_alternative" else [candidate],
        alternatives=[foreign] if report_shape == "foreign_alternative" else [],
        diagnostic_status=diagnostic_status,
        authority_mode="agent",
        runtime_run_id=run.id,
    )
    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "active_runtime_run_id": run.id,
                "status": InvestigationStatus.COMPLETED,
                "multi_agent_run": run_summary,
                "report": report,
            }
        )
    )
    repository.save_coordination_review(review)

    with pytest.raises(V11ProjectionIntegrityError, match="report"):
        ensure_v11_projection_owner(repository, runtime_store, repository.get("inv-1"))


@pytest.mark.parametrize(
    ("diagnostic_status", "run_status", "inconclusive"),
    [
        (DiagnosticStatus.COMPLETE, MultiAgentRunStatus.COMPLETED, False),
        (DiagnosticStatus.PARTIAL, MultiAgentRunStatus.PARTIAL, False),
        (DiagnosticStatus.INCONCLUSIVE, MultiAgentRunStatus.COMPLETED, True),
    ],
)
def test_v11_projection_guard_accepts_legal_final_status_matrix(
    runtime_store,
    diagnostic_status,
    run_status,
    inconclusive,
):
    repository = runtime_store.investigation_repository
    run = runtime_store.create_run(
        _persisted_v11_run(
            f"run-valid-projection-{type(runtime_store).__name__}-{diagnostic_status}",
            "inv-1",
        ).model_copy(update={"status": RuntimeRunStatus.COMPLETED})
    )
    candidate = _candidate()
    review = _owned_review(
        "inv-1",
        run.id,
        candidate,
        diagnostic_status=diagnostic_status,
        run_status=run_status,
        inconclusive=inconclusive,
    )
    run_summary = _owned_run_summary(
        run.id,
        diagnostic_status,
        status=run_status,
    )
    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "active_runtime_run_id": run.id,
                "multi_agent_run": run_summary,
            }
        )
    )
    repository.save_coordination_review(review)

    ensure_v11_projection_owner(repository, runtime_store, repository.get("inv-1"))


def test_v11_api_guard_rejects_inconsistent_review_run_and_missing_active_owner():
    """Workbench/API 不能发布未绑定 active owner 或状态矛盾的候选。"""
    container = reset_container()
    candidate = _candidate()
    cases = {
        "status-mismatch": {
            "active_runtime_run_id": "run-api-status-mismatch",
            "run_status": DiagnosticStatus.INCONCLUSIVE,
        },
        "missing-owner": {
            "active_runtime_run_id": None,
            "run_status": DiagnosticStatus.COMPLETE,
        },
    }
    for suffix, values in cases.items():
        investigation_id = f"inv-api-invalid-{suffix}"
        container.repository.save(
            InvestigationRecord(id=investigation_id, event=_event())
        )
        run = container.runtime_store.create_run(
            _persisted_v11_run(f"run-api-{suffix}", investigation_id)
        )
        review = _owned_review(investigation_id, run.id, candidate)
        run_summary = _owned_run_summary(
            run.id,
            values["run_status"],
        )
        container.repository.save(
            InvestigationRecord(
                id=investigation_id,
                event=_event(),
                status=InvestigationStatus.COMPLETED,
                active_runtime_run_id=(
                    run.id if values["active_runtime_run_id"] is not None else None
                ),
                multi_agent_run=run_summary,
            )
        )
        container.repository.save_coordination_review(review)

    with TestClient(app) as client:
        for suffix in cases:
            response = client.get(f"/investigations/inv-api-invalid-{suffix}/rca-workbench")
            assert response.status_code == 409, response.text

    container.close()


@pytest.mark.parametrize(
    "durable_status",
    [
        RuntimeRunStatus.CREATED,
        RuntimeRunStatus.FAILED,
        RuntimeRunStatus.CANCELLED,
        RuntimeRunStatus.INTERRUPTED,
    ],
)
def test_v11_api_rejects_nonterminal_durable_run_for_report_and_workbench(
    durable_status,
):
    """报告、工作台和 graph seed 共用 durable lifecycle guard。"""
    container = reset_container()
    investigation_id = f"inv-api-nonterminal-{durable_status}"
    container.repository.save(InvestigationRecord(id=investigation_id, event=_event()))
    run = container.runtime_store.create_run(
        _persisted_v11_run(
            f"run-api-nonterminal-{durable_status}", investigation_id
        ).model_copy(update={"status": durable_status})
    )
    candidate = _candidate()
    review = _owned_review(investigation_id, run.id, candidate)
    summary = _owned_run_summary(run.id, DiagnosticStatus.COMPLETE)
    container.repository.save(
        container.repository.get(investigation_id).model_copy(
            update={
                "status": InvestigationStatus.COMPLETED,
                "active_runtime_run_id": run.id,
                "multi_agent_run": summary,
            }
        )
    )
    container.repository.save_coordination_review(review)

    with TestClient(app) as client:
        for path in ("rca-workbench", "report"):
            response = client.get(f"/investigations/{investigation_id}/{path}")
            assert response.status_code == 409, response.text
    container.close()


def test_v11_report_api_rejects_stale_candidate_reference():
    """报告 API 不得发布脱离 Lead 裁决的 diagnosis candidate。"""
    container = reset_container()
    investigation_id = "inv-api-stale-report"
    container.repository.save(InvestigationRecord(id=investigation_id, event=_event()))
    run = container.runtime_store.create_run(
        _persisted_v11_run("run-api-stale-report", investigation_id).model_copy(
            update={"status": RuntimeRunStatus.COMPLETED}
        )
    )
    candidate = _candidate()
    review = _owned_review(investigation_id, run.id, candidate)
    summary = _owned_run_summary(run.id, DiagnosticStatus.COMPLETE)
    report = IncidentReport(
        investigation_id=investigation_id,
        summary="stale report",
        markdown="stale report",
        diagnoses=[candidate.model_copy(update={"id": "candidate-foreign"})],
        diagnostic_status=DiagnosticStatus.COMPLETE,
        authority_mode="agent",
        runtime_run_id=run.id,
    )
    container.repository.save(
        container.repository.get(investigation_id).model_copy(
            update={
                "status": InvestigationStatus.COMPLETED,
                "active_runtime_run_id": run.id,
                "multi_agent_run": summary,
                "report": report,
            }
        )
    )
    container.repository.save_coordination_review(review)

    with TestClient(app) as client:
        for path in ("report", "rca-workbench"):
            response = client.get(f"/investigations/{investigation_id}/{path}")
            assert response.status_code == 409, response.text
    container.close()


def test_active_v11_rejects_legacy_authority_report_with_foreign_candidate(
    runtime_store,
):
    """active V11 下 legacy report 也必须服从同一候选绑定契约。"""
    repository = runtime_store.investigation_repository
    run = runtime_store.create_run(
        _persisted_v11_run(
            f"run-legacy-report-{type(runtime_store).__name__}",
            "inv-1",
        ).model_copy(update={"status": RuntimeRunStatus.COMPLETED})
    )
    candidate = _candidate()
    review = _owned_review("inv-1", run.id, candidate)
    summary = _owned_run_summary(run.id, DiagnosticStatus.COMPLETE)
    report = IncidentReport(
        investigation_id="inv-1",
        summary="legacy stale report",
        markdown="legacy stale report",
        diagnoses=[candidate.model_copy(update={"id": "candidate-foreign"})],
        authority_mode="legacy_deterministic",
    )
    repository.save(
        repository.get("inv-1").model_copy(
            update={
                "status": InvestigationStatus.COMPLETED,
                "active_runtime_run_id": run.id,
                "multi_agent_run": summary,
                "report": report,
            }
        )
    )
    repository.save_coordination_review(review)

    with pytest.raises(V11ProjectionIntegrityError, match="report"):
        ensure_v11_projection_owner(repository, runtime_store, repository.get("inv-1"))


def test_v10_legacy_report_remains_usable_without_active_v11_owner(runtime_store):
    """没有 active V11 owner 时保留 V10 legacy deterministic 兼容。"""
    repository = runtime_store.investigation_repository
    candidate = _candidate()
    repository.save(
        InvestigationRecord(
            id="inv-v10-legacy-report",
            event=_event(),
            report=IncidentReport(
                investigation_id="inv-v10-legacy-report",
                summary="legacy report",
                markdown="legacy report",
                diagnoses=[candidate],
                authority_mode="legacy_deterministic",
            ),
        )
    )

    ensure_v11_projection_owner(
        repository,
        runtime_store,
        repository.get("inv-v10-legacy-report"),
    )


def test_v11_api_and_workbench_reject_legacy_authority_stale_report():
    """active V11 的 report、workbench、graph 共享 legacy report guard。"""
    container = reset_container()
    investigation_id = "inv-api-legacy-stale-report"
    container.repository.save(InvestigationRecord(id=investigation_id, event=_event()))
    run = container.runtime_store.create_run(
        _persisted_v11_run("run-api-legacy-stale-report", investigation_id).model_copy(
            update={"status": RuntimeRunStatus.COMPLETED}
        )
    )
    candidate = _candidate()
    review = _owned_review(investigation_id, run.id, candidate)
    report = IncidentReport(
        investigation_id=investigation_id,
        summary="legacy stale report",
        markdown="legacy stale report",
        diagnoses=[candidate.model_copy(update={"id": "candidate-foreign"})],
        authority_mode="legacy_deterministic",
    )
    container.repository.save(
        container.repository.get(investigation_id).model_copy(
            update={
                "status": InvestigationStatus.COMPLETED,
                "active_runtime_run_id": run.id,
                "multi_agent_run": _owned_run_summary(
                    run.id, DiagnosticStatus.COMPLETE
                ),
                "report": report,
            }
        )
    )
    container.repository.save_coordination_review(review)

    with TestClient(app) as client:
        for path in ("report", "rca-workbench", "coordination-review"):
            response = client.get(f"/investigations/{investigation_id}/{path}")
            assert response.status_code == 409, response.text
    container.close()


@pytest.mark.parametrize("status", [EvidenceStatus.FAILED, EvidenceStatus.SKIPPED])
def test_v11_report_rejects_unusable_referenced_evidence(status: EvidenceStatus):
    candidate = _candidate()
    review = _review(candidate)
    with pytest.raises(ValueError, match="usable evidence"):
        ReportGenerator().generate(
            "inv-v11-product",
            _event(),
            [_evidence().model_copy(update={"status": status})],
            [],
            coordination_review=review,
            multi_agent_run=_run_summary(review.diagnostic_status),
        )
