from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

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
from backend.services.container import reset_container
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
    run = container.create_runtime_run(
        record_id,
        strategy=InvestigationStrategy.ADAPTIVE,
        run_reason="initial",
        execution_contract_version=ExecutionContractVersion.V11,
    )
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
    run = container.create_runtime_run(
        record_id,
        strategy=InvestigationStrategy.ADAPTIVE,
        run_reason="initial",
        execution_contract_version=ExecutionContractVersion.V11,
    )
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
