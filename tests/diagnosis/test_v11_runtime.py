"""V11 Agent authority runtime 的 RED→GREEN 契约测试。"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.config.settings import AppSettings, StorageSettings
from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.v11_runtime import V11Runtime, V11RuntimeContractError
from backend.domain.agent_findings import (
    AgentFindingType,
    CausalCheckName,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.multi_agent import (
    CausalCheckStatus,
    CriticVerdict,
    LeadAction,
    ModelProvider,
)
from backend.domain.runtime import RuntimeResumeState, RuntimeRunReason
from backend.providers.registry import build_mock_provider_registry
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import V11_PHASE_ORDER, PhaseInput
from backend.services.container import AppContainer
from backend.tools.provider_tools import (
    VerifiedMemoryLookup,
    build_provider_tool_registry,
    current_investigation_id,
    current_investigation_scope,
)


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="checkout errors",
        description="elevated checkout failures",
        started_at=datetime(2026, 8, 8, 8, 0, tzinfo=UTC),
    )


def _repository() -> tuple[InMemoryInvestigationRepository, InvestigationRecord]:
    repository = InMemoryInvestigationRepository()
    record = repository.save(InvestigationRecord(id="inv-v11", event=_event()))
    return repository, record


def _seed_repository() -> tuple[InMemoryInvestigationRepository, InvestigationRecord]:
    repository, record = _repository()
    return repository, repository.save(
        record.model_copy(
            update={
                "evidence": [
                    EvidenceItem(
                        id="ev-metric",
                        provider=EvidenceProvider.METRIC,
                        kind=EvidenceKind.METRIC_TREND,
                        timestamp=record.event.started_at,
                        summary="error rate increased",
                        runtime_run_id="run-v11",
                    )
                ]
            }
        )
    )


@pytest.mark.anyio
async def test_lead_planning_persists_bounded_owned_tasks_before_investigation():
    repository, record = _repository()
    calls: list[dict] = []

    async def turn(**kwargs):
        calls.append(kwargs)
        assert kwargs["actor"] == "LeadAgent"
        return {
            "decision": {
                "action": "investigate",
                "summary": "Close the highest-value evidence gap.",
                "task_ids": ["task-timeline"],
                "selected_skills": ["first_failure_timeline@1.0.0"],
            },
            "tasks": [
                {
                    "id": "task-timeline",
                    "title": "Find the first failure",
                    "description": "Align the earliest error across signals.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs", "query_metrics"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                    "information_gap": "earliest causal boundary",
                }
            ],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )

    plan = await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-v11",
        remaining_tool_budget=8,
        remaining_token_budget=1000,
    )

    assert plan.runtime_run_id == "run-v11"
    assert plan.lead_decision is not None
    assert plan.lead_decision.action == LeadAction.INVESTIGATE
    assert [task.id for task in plan.tasks] == ["task-timeline"]
    assert repository.get_plan(record.id).model_dump(mode="json") == plan.model_dump(
        mode="json"
    )
    assert calls[0]["remaining_tool_budget"] == 8
    assert calls[0]["remaining_token_budget"] == 1000


@pytest.mark.anyio
async def test_lead_planning_rejects_conclude_and_duplicate_or_infeasible_work():
    repository, record = _repository()

    async def conclude(**_kwargs):
        return {
            "decision": {
                "action": "conclude",
                "summary": "premature",
                "candidate_ids": ["candidate-1"],
            },
            "tasks": [],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=conclude,
    )

    with pytest.raises(V11RuntimeContractError, match="planning cannot conclude"):
        await runtime.plan_lead(
            repository=repository,
            investigation_id=record.id,
            event=record.event,
            runtime_run_id="run-v11",
            remaining_tool_budget=8,
            remaining_token_budget=1000,
        )


def test_production_memory_wiring_exposes_the_current_investigation_resolver():
    repository, record = _repository()
    lookup = VerifiedMemoryLookup(
        repository,
        current_investigation_id=current_investigation_id,
    )
    registry = build_provider_tool_registry(
        build_mock_provider_registry(), lookup
    )
    lookup = V11Runtime.memory_lookup_from_registry(registry)

    assert lookup._current_investigation_id() is None
    with current_investigation_scope(record.id):
        assert lookup._current_investigation_id() == record.id


def test_app_container_wires_memory_resolver_for_current_investigation():
    container = AppContainer(
        AppSettings(storage=StorageSettings(url="memory://"))
    )
    try:
        record = container.repository.save(
            InvestigationRecord(id="container-inv", event=_event())
        )
        lookup = V11Runtime.memory_lookup_from_registry(container.tool_registry)
        assert lookup._current_investigation_id() is None
        with current_investigation_scope(record.id):
            assert lookup._current_investigation_id() == record.id
    finally:
        container.close()


@pytest.mark.anyio
async def test_v11_tool_dispatch_rechecks_the_frozen_manifest():
    from backend.diagnosis.adaptive_tools import AdaptiveToolSession

    repository, record = _repository()
    registry = build_provider_tool_registry(build_mock_provider_registry())
    manifest = registry.agent_manifest()
    observed: list[tuple[str, tuple[str, ...]]] = []
    original = registry.assert_agent_callable

    def assert_callable(tool_name: str, frozen_manifest: tuple[str, ...]):
        observed.append((tool_name, frozen_manifest))
        return original(tool_name, frozen_manifest)

    registry.assert_agent_callable = assert_callable
    session = AdaptiveToolSession(
        event=record.event,
        seed_evidence=[],
        registry=registry,
        task_ids={"investigator-1": "task-timeline"},
        runtime_run_id="run-v11",
        agent_manifest=manifest,
    )

    payload = {
        "reason": "find first failure",
        "start_time": "2026-08-08T07:00:00+00:00",
        "end_time": "2026-08-08T09:00:00+00:00",
        "keywords": ["error"],
        "levels": ["error"],
    }
    await session.invoke("investigator-1", "read_logs", json.dumps(payload), 1)
    await session.invoke(
        "investigator-1",
        "read_logs",
        json.dumps({**payload, "keywords": ["different"]}),
        1,
        attempt=2,
    )

    assert observed == [("read_logs", manifest), ("read_logs", manifest)]


@pytest.mark.anyio
async def test_v11_critic_and_lead_authority_complete_without_semantic_validation():
    repository, record = _seed_repository()
    checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.PASS.value,
            "summary": "committed evidence supports this mechanical check",
            "evidence_ids": ["ev-metric"],
        }
        for name in CausalCheckName
    ]
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "collect bounded evidence",
                "task_ids": ["task-1"],
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Inspect the first signal",
                    "description": "Inspect the committed metric boundary.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        {
            "summary": "candidate from the investigator",
            "findings": [
                {
                    "finding_type": AgentFindingType.ROOT_CAUSE.value,
                    "summary": "bounded root-cause finding",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-metric"],
                }
            ],
            "candidates": [
                {
                    "id": "candidate-1",
                    "summary": "bounded candidate",
                    "rank": 1,
                    "confidence": 0.8,
                    "supporting_evidence_ids": ["ev-metric"],
                }
            ],
        },
        {
            "summary": "all seven checks are complete",
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.ACCEPT.value,
                    "checks": checks,
                    "summary": "accepted by the seven mechanical checks",
                }
            ],
        },
        {
            "decision": {
                "action": "conclude",
                "summary": "accept the Critic-approved candidate",
                "candidate_ids": ["candidate-1"],
            }
        },
    ]

    async def turn(**_kwargs):
        return turns.pop(0)

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        tool_registry=build_provider_tool_registry(
            build_mock_provider_registry(),
            VerifiedMemoryLookup(
                repository,
                current_investigation_id=current_investigation_id,
            ),
        ),
        turn=turn,
    )
    runtime.runtime_run_id = "run-v11"
    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-v11",
        remaining_tool_budget=8,
        remaining_token_budget=1000,
    )
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    review = await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert len(review.critic_assessments) == 1
    assert len(review.critic_assessments[0].checks) == 7
    review = await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert review.lead_decision is not None
    assert review.lead_decision.candidate_ids == ["candidate-1"]
    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert runtime.active_session_count == 0


@pytest.mark.anyio
async def test_v11_needs_evidence_has_one_round_two_batch_and_one_reconciliation():
    repository, record = _seed_repository()
    unknown_checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.UNKNOWN.value,
            "summary": "named gap remains",
            "gap": "need one more signal",
        }
        for name in CausalCheckName
    ]
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "collect evidence",
                "task_ids": ["task-1"],
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Inspect signal",
                    "description": "Inspect the initial signal.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        {
            "findings": [
                {
                    "finding_type": AgentFindingType.GAP.value,
                    "summary": "one evidence gap",
                    "confidence": 0.4,
                    "gaps": ["need a trace boundary"],
                    "blocking": True,
                }
            ],
            "candidates": [
                {
                    "id": "candidate-1",
                    "summary": "candidate awaiting evidence",
                    "rank": 1,
                    "confidence": 0.5,
                    "supporting_evidence_ids": ["ev-metric"],
                }
            ],
        },
        {
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.NEEDS_EVIDENCE.value,
                    "checks": unknown_checks,
                    "gap": "need a trace boundary",
                    "supplemental_task_ids": ["task-round-2"],
                    "summary": "request one bounded evidence batch",
                }
            ],
            "tasks": [
                {
                    "id": "task-round-2",
                    "title": "Collect trace boundary",
                    "description": "Collect the requested trace boundary.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                    "information_gap": "need a trace boundary",
                }
            ],
        },
        {
            "findings": [
                {
                    "finding_type": AgentFindingType.SIGNAL.value,
                    "summary": "trace boundary observed",
                    "confidence": 0.7,
                    "evidence_ids": ["ev-metric"],
                }
            ]
        },
        {
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.ACCEPT.value,
                    "checks": [
                        {
                            "name": name.value,
                            "status": CausalCheckStatus.PASS.value,
                            "summary": "committed evidence",
                            "evidence_ids": ["ev-metric"],
                        }
                        for name in CausalCheckName
                    ],
                    "summary": "reconciled",
                }
            ]
        },
        {
            "decision": {
                "action": "conclude",
                "summary": "accept after reconciliation",
                "candidate_ids": ["candidate-1"],
            }
        },
    ]

    async def turn(**_kwargs):
        return turns.pop(0)

    runtime = V11Runtime(
        model="fake",
        tool_registry=build_provider_tool_registry(
            build_mock_provider_registry(),
            VerifiedMemoryLookup(
                repository,
                current_investigation_id=current_investigation_id,
            ),
        ),
        turn=turn,
    )
    runtime.runtime_run_id = "run-v11"
    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-v11",
        remaining_tool_budget=8,
        remaining_token_budget=1000,
    )
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    first = await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert first.critic_assessments[0].verdict == CriticVerdict.NEEDS_EVIDENCE
    assert len(repository.list_tasks(record.id)) == 2
    await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    reconciled = await runtime.critic_reconciliation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert reconciled is not None
    assert [item.id for item in reconciled.critic_assessments] == ["assessment-1"]
    assert reconciled.critic_assessments[0].verdict == CriticVerdict.ACCEPT
    assert repository.list_tasks(record.id)[-1].critic_assessment_id is not None
    assert len(turns) == 1
    await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )


@pytest.mark.anyio
async def test_v11_phase_executor_uses_runtime_phases_without_legacy_report_or_action():
    repository, record = _seed_repository()
    checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.PASS.value,
            "summary": "committed evidence",
            "evidence_ids": ["ev-metric"],
        }
        for name in CausalCheckName
    ]
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "plan one investigator",
                "task_ids": ["task-1"],
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Inspect signal",
                    "description": "Inspect the committed signal.",
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        {
            "findings": [
                {
                    "finding_type": AgentFindingType.ROOT_CAUSE.value,
                    "summary": "bounded finding",
                    "confidence": 0.8,
                    "evidence_ids": ["ev-metric"],
                }
            ],
            "candidates": [
                {
                    "id": "candidate-1",
                    "summary": "bounded candidate",
                    "rank": 1,
                    "confidence": 0.8,
                    "supporting_evidence_ids": ["ev-metric"],
                }
            ],
        },
        {
            "assessments": [
                {
                    "id": "assessment-1",
                    "candidate_id": "candidate-1",
                    "verdict": CriticVerdict.ACCEPT.value,
                    "checks": checks,
                    "summary": "accepted",
                }
            ]
        },
        {
            "decision": {
                "action": "conclude",
                "summary": "conclude accepted candidate",
                "candidate_ids": ["candidate-1"],
            }
        },
    ]

    async def turn(**_kwargs):
        return turns.pop(0)

    runtime = V11Runtime(
        model="fake",
        tool_registry=build_provider_tool_registry(
            build_mock_provider_registry(),
            VerifiedMemoryLookup(
                repository,
                current_investigation_id=current_investigation_id,
            ),
        ),
        turn=turn,
    )
    orchestrator = SimpleNamespace(
        repository=repository,
        v11_runtime=runtime,
        agents_runtime=None,
        max_total_tool_calls=8,
    )
    executor = DiagnosisPhaseExecutor(orchestrator)
    executor._is_durable_session = True
    for phase in V11_PHASE_ORDER:
        output = await executor.execute_phase(
            PhaseInput(
                run_id="run-v11",
                attempt_id="attempt-v11",
                phase=phase,
                resume_state=RuntimeResumeState(
                    remaining_tool_budget=8,
                    remaining_token_budget=1000,
                ),
                investigation_id=record.id,
                strategy="adaptive",
                run_reason=RuntimeRunReason.INITIAL,
                execution_contract_version="v11",
                tool_budget=8,
                token_budget=1000,
                timeout_seconds=60,
            )
        )
        assert output.business_mutation.investigation_id == record.id

    persisted = repository.get_coordination_review(record.id)
    assert persisted is not None
    assert persisted.lead_decision is not None
    assert repository.get(record.id).report is None
    assert repository.get(record.id).actions == []
