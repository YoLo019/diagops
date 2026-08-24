"""V11 Agent authority runtime 的 RED→GREEN 契约测试。"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import openai
import pytest
from agents import (
    AgentOutputSchema,
    FunctionTool,
    Model,
    ModelBehaviorError,
    ModelResponse,
    ModelSettings,
    Usage,
)
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.config.settings import (
    AgentsSettings,
    AppSettings,
    StorageSettings,
)
from backend.config.settings import (
    endpoint_id as endpoint_identity,
)
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis import openai_compatible_model as compatible_model_module
from backend.diagnosis import v11_runtime as v11_runtime_module
from backend.diagnosis.adaptive_tools import ClassifiedRetryableError
from backend.diagnosis.diagnostic_skills import (
    DIAGNOSTIC_SKILLS,
    SKILL_CATALOG_VERSION,
    skill_catalog_hash,
)
from backend.diagnosis.openai_compatible_model import (
    OpenAICompatibleChatCompletionsModel,
)
from backend.diagnosis.openai_model import OFFICIAL_OPENAI_BASE_URL
from backend.diagnosis.v11_runtime import (
    CriticOutput,
    InvestigatorFindingDraft,
    InvestigatorOutput,
    LeadAdjudicationOutput,
    LeadPlanningOutput,
    V11Runtime,
    V11RuntimeContractError,
    V11SingleControlOutput,
    _V11BudgetedModel,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    CausalCheck,
    CausalCheckName,
    CoordinationReview,
    CriticAssessment,
    FindingActor,
    RootCauseCandidate,
)
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisPlan,
    DiagnosisTask,
    DiagnosisTaskType,
    LeadDecision,
)
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceScope,
    EvidenceStatus,
)
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CausalCheckStatus,
    CriticVerdict,
    DiagnosticStatus,
    ExecutionActor,
    ExecutionStepKind,
    FailureCategory,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
)
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePhase,
    RuntimeResumeState,
    RuntimeRunReason,
    seal_v11_execution_contract,
)
from backend.domain.tool_calls import ToolSpec
from backend.domain.v11_contracts import validate_v11_final_status
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
from backend.tools.registry import agent_manifest_hash

_SCHEMA_VALUE_KEYS = {"items", "additionalProperties", "contains", "not"}
_SCHEMA_LIST_KEYS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _assert_explicit_schema_semantics(schema: dict, path: tuple[str, ...] = ()) -> None:
    semantic_keys = {"type", "$ref", "allOf", "anyOf", "oneOf", "enum", "const"}
    assert semantic_keys & schema.keys(), ".".join(path) or "<root>"
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False

    for key in ("$defs", "definitions", "properties", "patternProperties"):
        for name, child in schema.get(key, {}).items():
            _assert_explicit_schema_semantics(child, (*path, key, name))
    for key in _SCHEMA_VALUE_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            _assert_explicit_schema_semantics(child, (*path, key))
    for key in _SCHEMA_LIST_KEYS:
        for index, child in enumerate(schema.get(key, [])):
            _assert_explicit_schema_semantics(child, (*path, key, str(index)))


@pytest.mark.parametrize(
    "output_type",
    [
        LeadPlanningOutput,
        InvestigatorOutput,
        CriticOutput,
        LeadAdjudicationOutput,
        V11SingleControlOutput,
    ],
)
def test_v11_model_output_types_are_valid_strict_json_schemas(output_type):
    schema = AgentOutputSchema(output_type, strict_json_schema=True).json_schema()
    _assert_explicit_schema_semantics(schema)


def test_investigator_candidate_schema_requires_publishable_fields():
    schema = AgentOutputSchema(
        InvestigatorOutput, strict_json_schema=True
    ).json_schema()
    candidate_schema = schema["$defs"]["InvestigatorCandidateDraft"]

    assert {
        "affected_entity",
        "failure_mechanism",
        "supporting_evidence_ids",
    } <= set(candidate_schema["required"])
    assert candidate_schema["properties"]["supporting_evidence_ids"]["minItems"] == 1
    assert not {
        "id",
        "rank",
        "runtime_run_id",
        "review_round",
        "authoritative",
        "supporting_finding_ids",
    } & set(candidate_schema["properties"])

    critic_schema = AgentOutputSchema(
        CriticOutput, strict_json_schema=True
    ).json_schema()["$defs"]["CriticAssessmentDraft"]
    assert "candidate_ref" in critic_schema["properties"]
    assert not {"id", "candidate_id", "runtime_run_id", "review_round"} & set(
        critic_schema["properties"]
    )

    with pytest.raises(ValidationError):
        InvestigatorOutput.model_validate(
            {
                "summary": "incomplete candidate",
                "findings": [],
                "candidates": [
                    {
                        "summary": "missing publishable fields",
                        "rank": 1,
                        "confidence": 0.5,
                    }
                ],
            }
        )


def test_lead_planning_schema_constrains_selected_skill_identifiers():
    schema = AgentOutputSchema(
        LeadPlanningOutput, strict_json_schema=True
    ).json_schema()
    selected_skills = schema["$defs"]["LeadDecision"]["properties"][
        "selected_skills"
    ]["items"]

    assert selected_skills["pattern"] == (
        r"^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9_.-]{1,32}$"
    )


def test_planning_task_schema_requires_first_analysis_round():
    for output_type, task_path in (
        (LeadPlanningOutput, ("$defs", "LeadPlanningTaskDraft")),
        (V11SingleControlOutput, ("$defs", "LeadPlanningTaskDraft")),
    ):
        schema = AgentOutputSchema(
            output_type, strict_json_schema=True
        ).json_schema()
        task_schema = schema[task_path[0]][task_path[1]]

        assert task_schema["properties"]["analysis_round"]["const"] == 1

    with pytest.raises(ValidationError):
        LeadPlanningOutput.model_validate(
            {
                "decision": {
                    "action": "investigate",
                    "summary": "Inspect evidence.",
                    "task_ids": ["task-round-two"],
                    "candidate_ids": [],
                    "evidence_ids": [],
                    "selected_skills": [],
                    "stop_reason": None,
                },
                "tasks": [
                    {
                        "id": "task-round-two",
                        "title": "Inspect evidence",
                        "description": "Inspect one bounded signal.",
                        "analysis_round": 2,
                        "tool_names": [],
                        "information_gap": "missing signal",
                    }
                ],
            }
        )


def test_single_control_schema_constrains_selected_skill_identifiers():
    schema = AgentOutputSchema(
        V11SingleControlOutput, strict_json_schema=True
    ).json_schema()
    selected_skills = schema["$defs"]["LeadDecisionDraft"]["properties"][
        "selected_skills"
    ]["items"]

    assert selected_skills["pattern"] == (
        r"^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9_.-]{1,32}$"
    )


def test_lead_prompt_exposes_exact_skill_identifiers():
    prompt = json.loads(
        V11Runtime(model=None)._lead_prompt(_event(), ("read_logs",), 8)
    )

    assert [item["identifier"] for item in prompt["skills"]] == [
        f"{skill.name}@{skill.version}" for skill in DIAGNOSTIC_SKILLS
    ]
    assert "name@version" in prompt["rule"]


def test_investigator_prompt_keeps_tool_descriptions_without_schema_duplication():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    runtime = V11Runtime(model=None, tool_registry=registry)
    task = DiagnosisTask(
        id="task-schema-contract",
        title="Inspect bounded telemetry",
        description="Validate the available telemetry window.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        runtime_run_id="run-schema-contract",
        information_gap="runtime state",
    )

    prompt = json.loads(
        runtime._investigator_prompt(
            _event(),
            task,
            "investigator-1",
            registry.agent_manifest(),
            [],
            [],
            None,
            [],
        )
    )
    contracts = {item["name"]: item for item in prompt["tool_contracts"]}

    assert set(contracts) == set(registry.agent_manifest())
    assert "description" in contracts["read_runtime_state"]
    assert "input_schema" not in contracts["read_runtime_state"]
    assert "unresolved cause" in prompt["rule"]


@pytest.mark.anyio
async def test_invalid_structured_output_retry_receives_safe_contract_feedback(
    monkeypatch,
):
    class Output(BaseModel):
        value: str

    prompts = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def turn(**kwargs):
        prompts.append(kwargs["prompt"])
        if len(prompts) == 1:
            raise ModelBehaviorError("untrusted provider validation details")
        return {"value": "ok"}

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", fake_sleep)
    runtime = V11Runtime(model="fake", turn=turn, token_budget=10_000)

    result = await runtime._call_model(
        actor="CriticAgent",
        prompt="review the committed candidates",
        output_type=Output,
        context={"request": "bounded"},
        tools=[],
        remaining_token_budget=10_000,
        remaining_tool_budget=0,
    )

    assert result.output == {"value": "ok"}
    assert prompts[0] == "review the committed candidates"
    assert "previous structured response was rejected" in prompts[1]
    assert "untrusted provider validation details" not in prompts[1]
    assert sleeps == [5.0]


@pytest.mark.anyio
async def test_truncated_structured_output_retries_then_parses(monkeypatch):
    class Output(BaseModel):
        value: str = Field(min_length=1)

    calls = 0
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        return {"value": ""} if calls == 1 else {"value": "recovered"}

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", fake_sleep)
    runtime = V11Runtime(model="fake", turn=turn, token_budget=10_000)

    result = await runtime._call_model(
        actor="InvestigatorAgent",
        prompt="return the bounded diagnosis",
        output_type=Output,
        context={"request": "bounded"},
        tools=[],
        remaining_token_budget=10_000,
        remaining_tool_budget=0,
    )

    assert result.output == {"value": "recovered"}
    assert calls == 2
    assert sleeps == [5.0]


def test_v11_input_estimator_is_conservative_for_cjk_and_json():
    ascii_estimate, _ = v11_runtime_module._estimate_text_tokens("a" * 40)
    cjk_estimate, cjk_counts = v11_runtime_module._estimate_text_tokens("故障" * 20)
    json_estimate, json_counts = v11_runtime_module._estimate_text_tokens(
        '{"properties":["service","window_start"]}'
    )

    assert cjk_estimate > ascii_estimate
    assert cjk_counts["cjk_chars"] == 40
    assert json_estimate >= json_counts["json_punctuation_chars"]


def test_v11_input_estimate_audit_fits_model_event_payload():
    _estimate, audit = v11_runtime_module._estimate_model_input(
        "检查故障窗口",
        {
            "input": [{"role": "user", "content": "故障"}],
            "tools": [{"name": "read_logs", "parameters": {}}],
            "output_schema": {"type": "object", "properties": {}},
        },
    )

    event = RuntimeEvent(
        run_id="run-audit",
        attempt_id="attempt-audit",
        sequence=1,
        event_type=RuntimeEventType.MODEL_STARTED,
        actor_type=RuntimeActorType.AGENT,
        safe_payload={
            "status": "started",
            **v11_runtime_module._input_estimate_audit(audit),
        },
    )

    assert event.safe_payload["input_estimate_audit"]["method"] == (
        "unicode-json-envelope-v1"
    )


@pytest.mark.anyio
async def test_v11_input_estimator_calibrates_after_provider_settlement():
    runtime = V11Runtime(model=None, token_budget=100_000)
    prompt = "a" * 1_000
    context = {"input": "b" * 1_000, "tools": [], "output_schema": {}}

    _first_cap, first_estimate, first_reserved = await runtime._reserve_model_budget(
        None,
        prompt,
        context,
        reservation_id="calibration-request-1",
        actor="InvestigatorAgent",
    )
    raw_estimate = runtime._model_reservations[
        "calibration-request-1"
    ].estimate_audit["raw_estimated_tokens"]
    actual_input = max(1, int(raw_estimate) // 2)
    await runtime._settle_model_budget(
        first_reserved,
        actual_input,
        reservation_id="calibration-request-1",
        actor="InvestigatorAgent",
        input_tokens=actual_input,
        actual_input_tokens=actual_input,
        output_tokens=0,
    )
    assert runtime.remaining_token_budget == 100_000 - actual_input

    _second_cap, second_estimate, _second_reserved = await runtime._reserve_model_budget(
        None,
        prompt,
        context,
        reservation_id="calibration-request-2",
        actor="InvestigatorAgent",
    )

    assert second_estimate < first_estimate
    audit = runtime._model_reservations["calibration-request-2"].estimate_audit
    assert audit["calibration_samples"] == 1
    assert 7_500 <= audit["calibration_factor_basis_points"] < 10_000


def test_task_evidence_scope_filters_only_explicit_conflicts():
    task = DiagnosisTask(
        id="task-scoped-evidence",
        title="Inspect service evidence",
        description="Inspect the bounded service window.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        runtime_run_id="run-scoped-evidence",
        evidence_scope={
            "entity_ids": ["service-a"],
            "start_time": "2026-01-01T00:00:00Z",
            "end_time": "2026-01-01T01:00:00Z",
        },
    )
    evidence = [
        EvidenceItem(
            id="ev-anchor",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            summary="initial anchor",
            runtime_run_id=None,
            scope=EvidenceScope(entity_ids=["service-b"]),
        ),
        EvidenceItem(
            id="ev-match",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
            summary="matching evidence",
            runtime_run_id="run-scoped-evidence",
            scope=EvidenceScope(
                entity_ids=["service-a"],
                observed_at=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
            ),
        ),
        EvidenceItem(
            id="ev-conflict",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
            summary="conflicting entity",
            runtime_run_id="run-scoped-evidence",
            scope=EvidenceScope(
                entity_ids=["service-b"],
                observed_at=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
            ),
        ),
        EvidenceItem(
            id="ev-unknown",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
            summary="unknown scope",
            runtime_run_id="run-scoped-evidence",
            scope=None,
        ),
    ]

    selected = v11_runtime_module._evidence_for_task(task, evidence)

    assert [item.id for item in selected] == ["ev-anchor", "ev-match", "ev-unknown"]


def test_critic_evidence_is_limited_to_candidate_and_finding_references():
    review = CoordinationReview(
        investigation_id="inv-critic-evidence",
        candidates=[
            RootCauseCandidate(
                id="candidate-critic-evidence",
                summary="candidate",
                rank=1,
                confidence=0.8,
                supporting_evidence_ids=["ev-referenced"],
            )
        ],
    )
    evidence = [
        EvidenceItem(
            id="ev-referenced",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            summary="referenced evidence",
        ),
        EvidenceItem(
            id="ev-unreferenced",
            provider=EvidenceProvider.METRIC,
            kind=EvidenceKind.METRIC_TREND,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            summary="unreferenced query result",
        ),
    ]

    selected = v11_runtime_module._critic_evidence(review, (), evidence)

    assert [item.id for item in selected] == ["ev-referenced"]


def test_single_control_planning_draft_tolerates_inconclusive_with_candidates():
    payload = {
        "planning": {
            "decision": {
                "action": "inconclusive",
                "summary": "Investigated but evidence is insufficient.",
                "task_ids": [],
                "candidate_ids": ["candidate-1"],
                "evidence_ids": ["ev-1"],
                "selected_skills": [],
                "stop_reason": None,
            },
            "tasks": [],
        },
        "investigator": {"summary": "", "findings": [], "candidates": []},
    }

    output = V11SingleControlOutput.model_validate(payload)

    assert output.planning.decision.action == LeadAction.INCONCLUSIVE
    assert output.planning.decision.candidate_ids == ["candidate-1"]


def test_multi_planning_output_still_rejects_inconclusive_with_candidates():
    with pytest.raises(ValidationError):
        LeadPlanningOutput.model_validate(
            {
                "decision": {
                    "action": "inconclusive",
                    "summary": "Investigated but evidence is insufficient.",
                    "task_ids": [],
                    "candidate_ids": ["candidate-1"],
                    "evidence_ids": [],
                    "selected_skills": [],
                    "stop_reason": None,
                },
                "tasks": [],
            }
        )


def test_critic_task_schema_still_allows_round_two():
    output = CriticOutput.model_validate(
        {
            "summary": "Request one supplemental signal.",
            "assessments": [],
            "tasks": [
                {
                    "id": "task-round-two",
                    "title": "Inspect a supplemental signal",
                    "description": "Collect one bounded follow-up signal.",
                    "analysis_round": 2,
                    "tool_names": [],
                    "information_gap": "missing signal",
                }
            ],
        }
    )

    assert output.tasks[0].analysis_round == 2


def _finding_gate_harness():
    runtime = V11Runtime(model=None)
    runtime.runtime_run_id = "run-1"
    task = DiagnosisTask(
        id="task-1",
        title="bounded investigation",
        description="inspect one isolated signal",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        runtime_run_id="run-1",
        information_gap="alert telemetry availability",
    )
    skipped = EvidenceItem(
        id="ev-skipped",
        provider=EvidenceProvider.RELATED_ALERT,
        kind=EvidenceKind.PROVIDER_ERROR,
        timestamp=datetime(2026, 8, 14, 10, tzinfo=UTC),
        summary="related_alert provider skipped",
        status=EvidenceStatus.SKIPPED,
        error_message="query_related_alerts provider not configured",
    )
    success = EvidenceItem(
        id="ev-success",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 14, 10, tzinfo=UTC),
        summary="error rate spiked",
        status=EvidenceStatus.SUCCESS,
    )
    return runtime, task, [skipped, success]


def test_gap_finding_may_cite_committed_failed_or_skipped_evidence():
    """GAP finding 的语义是“证据缺失”；引用已提交的 skipped/failed 证据是
    其正确出处（spec §7.2 只约束非 gap finding 与未提交输出）。"""
    runtime, task, evidence = _finding_gate_harness()
    draft = InvestigatorFindingDraft(
        finding_type=AgentFindingType.GAP,
        summary="Related-alert telemetry was not collected.",
        confidence=1.0,
        evidence_ids=["ev-skipped"],
    )

    finding = runtime._finding_from_draft(
        draft,
        investigation_id="inv-1",
        task=task,
        instance_id="investigator-1",
        round_number=1,
        assessment=None,
        evidence=evidence,
    )

    assert finding.evidence_ids == ["ev-skipped"]


def test_non_gap_finding_still_rejects_failed_or_skipped_evidence():
    runtime, task, evidence = _finding_gate_harness()
    draft = InvestigatorFindingDraft(
        finding_type=AgentFindingType.SIGNAL,
        summary="Alert telemetry proves the failure.",
        confidence=0.9,
        evidence_ids=["ev-skipped"],
    )

    with pytest.raises(V11RuntimeContractError, match="uncommitted evidence"):
        runtime._finding_from_draft(
            draft,
            investigation_id="inv-1",
            task=task,
            instance_id="investigator-1",
            round_number=1,
            assessment=None,
            evidence=evidence,
        )


def test_non_gap_finding_scope_mismatch_is_rejected_during_draft_admission():
    runtime, task, evidence = _finding_gate_harness()
    evidence[1] = evidence[1].model_copy(
        update={"scope": EvidenceScope(entity_ids=["service-a"])}
    )
    draft = InvestigatorFindingDraft(
        finding_type=AgentFindingType.SIGNAL,
        summary="The scoped signal proves the failure.",
        confidence=0.9,
        evidence_ids=["ev-success"],
        affected_entity="service-b",
    )

    with pytest.raises(
        V11RuntimeContractError, match="scope_entity_mismatch"
    ):
        runtime._finding_from_draft(
            draft,
            investigation_id="inv-1",
            task=task,
            instance_id="investigator-1",
            round_number=1,
            assessment=None,
            evidence=evidence,
        )


def test_gap_finding_still_rejects_truly_uncommitted_evidence():
    runtime, task, evidence = _finding_gate_harness()
    draft = InvestigatorFindingDraft(
        finding_type=AgentFindingType.GAP,
        summary="Missing telemetry.",
        confidence=1.0,
        evidence_ids=["ev-hallucinated"],
    )

    with pytest.raises(V11RuntimeContractError, match="uncommitted evidence"):
        runtime._finding_from_draft(
            draft,
            investigation_id="inv-1",
            task=task,
            instance_id="investigator-1",
            round_number=1,
            assessment=None,
            evidence=evidence,
        )


def _investigator_round_harness(runtime_run_id: str):
    repository, record = _repository()
    task = DiagnosisTask(
        id="task-round-1",
        title="bounded investigation",
        description="inspect one isolated signal",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        evidence_scope={"entity_ids": [record.event.service]},
        runtime_run_id=runtime_run_id,
    )
    repository.save_plan(
        DiagnosisPlan(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            tasks=[task],
            lead_decision=LeadDecision(
                action=LeadAction.INVESTIGATE,
                summary="run investigator",
                task_ids=[task.id],
            ),
        )
    )
    return repository, record


@pytest.mark.anyio
async def test_rejected_finding_draft_does_not_discard_valid_findings():
    """模型输出是不可信数据：单个 draft 违约只拒绝该 draft（持久化审计），
    同批合法 finding 照常提交（spec §7.4 校验器可拒绝输出）。"""
    runtime_run_id = "run-draft-partial-reject"
    repository, record = _investigator_round_harness(runtime_run_id)

    async def turn(**_kwargs):
        return {
            "summary": "done",
            "findings": [
                {
                    "finding_type": "gap",
                    "summary": "deployment history is unavailable",
                    "confidence": 0.4,
                    "evidence_ids": [],
                    "gaps": ["deployment history unavailable"],
                },
                {
                    "finding_type": "signal",
                    "summary": "claims a signal without any evidence",
                    "confidence": 0.9,
                    "evidence_ids": [],
                },
            ],
            "candidates": [],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        max_investigators=1,
    )
    runtime.runtime_run_id = runtime_run_id

    findings = await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert [finding.finding_type for finding in findings] == [AgentFindingType.GAP]
    executions = repository.list_executions(record.id)
    completed = [
        item for item in executions if item.status == AgentExecutionStatus.COMPLETED
    ]
    audits = [
        item for item in executions if item.status == AgentExecutionStatus.FAILED
    ]
    assert len(completed) == 1
    assert len(audits) == 1
    assert audits[0].failure_category == FailureCategory.INVALID_REFERENCE
    assert audits[0].error_message == "finding draft rejected: non_gap_requires_evidence"
    assert repository.get(record.id).status != InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_all_finding_drafts_rejected_keeps_batch_failure_semantics():
    """整批 draft 全违约 = investigator 失败；single 配置下 round 收敛 terminal。"""
    runtime_run_id = "run-draft-full-reject"
    repository, record = _investigator_round_harness(runtime_run_id)

    async def turn(**_kwargs):
        return {
            "summary": "done",
            "findings": [
                {
                    "finding_type": "signal",
                    "summary": "claims a signal without any evidence",
                    "confidence": 0.9,
                    "evidence_ids": [],
                },
            ],
            "candidates": [],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        max_investigators=1,
    )
    runtime.runtime_run_id = runtime_run_id

    findings = await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert findings == ()
    assert repository.get(record.id).status == InvestigationStatus.FAILED
    audits = [
        item
        for item in repository.list_executions(record.id)
        if item.status == AgentExecutionStatus.FAILED
        and item.error_message == "finding draft rejected: non_gap_requires_evidence"
    ]
    assert len(audits) == 1


def _candidate_draft(
    *,
    supporting_finding_ids: list[str] | None = None,
    supporting_evidence_ids: list[str] | None = None,
    rank: int = 1,
) -> dict:
    del supporting_finding_ids, rank
    return {
        "cause_type": None,
        "affected_entity": "checkout-service",
        "failure_mechanism": "bounded failure mechanism",
        "summary": "candidate from investigator",
        "confidence": 0.6,
        "supporting_evidence_ids": (
            supporting_evidence_ids
            if supporting_evidence_ids is not None
            else ["ev-candidate"]
        ),
        "contradicting_evidence_ids": [],
        "rationale": "bounded evidence-backed diagnosis",
        "uncertainty": "",
        "onset_window_start": None,
        "onset_window_end": None,
    }


async def _run_candidate_round(runtime_run_id: str, turn, seed=None):
    repository, record = _investigator_round_harness(runtime_run_id)
    if seed is None:
        seed = [
            EvidenceItem(
                id="ev-candidate",
                provider=EvidenceProvider.LOG,
                kind=EvidenceKind.LOG_PATTERN,
                timestamp=record.event.started_at,
                summary="candidate evidence",
                status=EvidenceStatus.SUCCESS,
                runtime_run_id=runtime_run_id,
            )
        ]
    repository.save(record.model_copy(update={"evidence": seed}))
    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        max_investigators=1,
    )
    runtime.runtime_run_id = runtime_run_id
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    return repository, record


@pytest.mark.anyio
async def test_candidate_with_unknown_finding_reference_is_dropped_and_audited():
    """finding ID 由服务端生成，模型填的引用几乎必然无效；准入层丢弃违约
    candidate 并留审计，不让它在终态校验升级为 run 失败（spec §7.4）。"""

    async def turn(**_kwargs):
        return {
            "summary": "done",
            "findings": [],
            "candidates": [
                _candidate_draft(supporting_evidence_ids=["ev-bogus"])
            ],
        }

    repository, record = await _run_candidate_round(
        "run-candidate-bogus-finding", turn
    )

    review = repository.get_coordination_review(record.id)
    assert review is None or review.candidates == []
    audits = [
        item
        for item in repository.list_executions(record.id)
        if item.error_message == "candidate draft rejected: candidate_evidence_reference"
    ]
    assert len(audits) == 1
    assert audits[0].failure_category == FailureCategory.INVALID_REFERENCE
    assert repository.get(record.id).status != InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_candidate_referencing_persisted_finding_is_kept():
    runtime_run_id = "run-candidate-known-finding"
    repository, record = _investigator_round_harness(runtime_run_id)
    plan = repository.get_plan(record.id)
    prior_task = DiagnosisTask(
        id="task-prior",
        title="earlier bounded investigation",
        description="already investigated",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        runtime_run_id=runtime_run_id,
        information_gap="already-resolved gap",
    )
    repository.save_plan(
        plan.model_copy(update={"tasks": [*plan.tasks, prior_task]})
    )
    seeded_evidence = EvidenceItem(
        id="ev-committed-signal",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 15, 10, tzinfo=UTC),
        summary="error rate spiked",
        status=EvidenceStatus.SUCCESS,
        runtime_run_id=runtime_run_id,
    )
    repository.save(record.model_copy(update={"evidence": [seeded_evidence]}))
    persisted = AgentFinding(
        investigation_id=record.id,
        agent_name=FindingActor.INVESTIGATOR,
        agent_instance_id="inst-seed",
        # 属于另一个任务，避免触发 _run_investigator 的重放短路。
        task_id="task-prior",
        runtime_run_id=runtime_run_id,
        finding_type=AgentFindingType.SIGNAL,
        summary="committed signal",
        confidence=0.7,
        evidence_ids=[seeded_evidence.id],
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        analysis_round=1,
    )
    repository.save_agent_findings(record.id, [persisted])

    async def turn(**_kwargs):
        return {
            "summary": "done",
            "findings": [],
            "candidates": [
                _candidate_draft(
                    supporting_finding_ids=[persisted.id],
                    supporting_evidence_ids=[seeded_evidence.id],
                )
            ],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        max_investigators=1,
    )
    runtime.runtime_run_id = runtime_run_id
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert [item.supporting_finding_ids for item in review.candidates] == [[]]


@pytest.mark.anyio
async def test_candidate_citing_skipped_evidence_is_dropped():
    skipped = EvidenceItem(
        id="ev-skipped-candidate",
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.PROVIDER_ERROR,
        timestamp=datetime(2026, 8, 15, 10, tzinfo=UTC),
        summary="deploy provider skipped",
        status=EvidenceStatus.SKIPPED,
        runtime_run_id="run-candidate-skipped-evidence",
    )

    async def turn(**_kwargs):
        return {
            "summary": "done",
            "findings": [],
            "candidates": [
                _candidate_draft(supporting_evidence_ids=[skipped.id])
            ],
        }

    repository, record = await _run_candidate_round(
        "run-candidate-skipped-evidence", turn, seed=[skipped]
    )

    review = repository.get_coordination_review(record.id)
    assert review is None or review.candidates == []
    audits = [
        item
        for item in repository.list_executions(record.id)
        if item.error_message
        == "candidate draft rejected: candidate_evidence_reference"
    ]
    assert len(audits) == 1


def test_candidate_scope_mismatch_is_dropped_at_admission():
    runtime_run_id = "run-candidate-scope-mismatch"
    repository, record = _investigator_round_harness(runtime_run_id)
    runtime = V11Runtime(model="fake", max_investigators=1)
    runtime.runtime_run_id = runtime_run_id
    task = repository.get_plan(record.id).tasks[0]
    evidence = EvidenceItem(
        id="ev-scoped-candidate",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 8, 15, 10, tzinfo=UTC),
        summary="scoped signal",
        status=EvidenceStatus.SUCCESS,
        runtime_run_id=runtime_run_id,
        scope=EvidenceScope(entity_ids=["service-a"]),
    )
    audit_executions: list[AgentExecution] = []

    admitted = runtime._admit_candidates(
        (
            RootCauseCandidate(
                summary="candidate with a conflicting entity",
                rank=1,
                confidence=0.6,
                affected_entity="service-b",
                failure_mechanism="bounded failure mechanism",
                supporting_evidence_ids=[evidence.id],
            ),
        ),
        batch_findings=[],
        committed_evidence=[evidence],
        repository=repository,
        investigation_id=record.id,
        task=task,
        instance_id="investigator-scope",
        audit_executions=audit_executions,
    )

    assert admitted == ()
    assert audit_executions[0].error_message == (
        "candidate draft rejected: scope_entity_mismatch"
    )


def test_candidate_incomplete_is_dropped_at_shared_admission():
    runtime_run_id = "run-candidate-incomplete"
    repository, record = _investigator_round_harness(runtime_run_id)
    runtime = V11Runtime(model="fake", max_investigators=1)
    runtime.runtime_run_id = runtime_run_id
    task = repository.get_plan(record.id).tasks[0]
    audit_executions: list[AgentExecution] = []

    admitted = runtime._admit_candidates(
        (
            RootCauseCandidate(
                summary="candidate without publishable fields",
                rank=1,
                confidence=0.5,
            ),
        ),
        batch_findings=[],
        committed_evidence=[],
        repository=repository,
        investigation_id=record.id,
        task=task,
        instance_id="investigator-incomplete",
        audit_executions=audit_executions,
    )

    assert admitted == ()
    assert audit_executions[0].error_message == (
        "candidate draft rejected: candidate_incomplete"
    )


@pytest.mark.anyio
async def test_candidate_drop_does_not_fail_sibling_candidates():
    async def turn(**_kwargs):
        return {
            "summary": "done",
            "findings": [],
            "candidates": [
                _candidate_draft(supporting_evidence_ids=["ev-bogus"]),
                _candidate_draft(rank=2),
            ],
        }

    repository, record = await _run_candidate_round(
        "run-candidate-sibling", turn
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert len(review.candidates) == 1
    assert review.candidates[0].supporting_evidence_ids == ["ev-candidate"]


def test_candidate_ids_are_server_generated_for_each_investigator_batch():
    runtime_run_id = "run-candidate-server-ids"
    repository, record = _investigator_round_harness(runtime_run_id)
    runtime = V11Runtime(model="fake", max_investigators=2)
    runtime.runtime_run_id = runtime_run_id
    task = repository.get_plan(record.id).tasks[0]
    evidence = EvidenceItem(
        id="ev-server-id",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=record.event.started_at,
        summary="candidate evidence",
        status=EvidenceStatus.SUCCESS,
        runtime_run_id=runtime_run_id,
    )
    audit_executions: list[AgentExecution] = []

    first = runtime._admit_candidates(
        (
            RootCauseCandidate(
                id="candidate-1",
                summary="first",
                rank=1,
                confidence=0.6,
                affected_entity=record.event.service,
                failure_mechanism="bounded failure mechanism",
                supporting_evidence_ids=[evidence.id],
            ),
        ),
        batch_findings=[],
        committed_evidence=[evidence],
        repository=repository,
        investigation_id=record.id,
        task=task,
        instance_id="investigator-first",
        audit_executions=audit_executions,
    )
    second = runtime._admit_candidates(
        (
            RootCauseCandidate(
                id="candidate-1",
                summary="second",
                rank=1,
                confidence=0.5,
                affected_entity=record.event.service,
                failure_mechanism="bounded failure mechanism",
                supporting_evidence_ids=[evidence.id],
            ),
        ),
        batch_findings=[],
        committed_evidence=[evidence],
        repository=repository,
        investigation_id=record.id,
        task=task,
        instance_id="investigator-second",
        audit_executions=audit_executions,
    )

    assert first[0].id != "candidate-1"
    assert second[0].id != "candidate-1"
    assert first[0].id != second[0].id


def test_candidate_projection_assigns_stable_global_ranks():
    runtime_run_id = "run-candidate-global-ranks"
    repository, record = _investigator_round_harness(runtime_run_id)
    runtime = V11Runtime(model="fake", max_investigators=2)
    runtime.runtime_run_id = runtime_run_id
    task = repository.get_plan(record.id).tasks[0]
    evidence = EvidenceItem(
        id="ev-global-rank",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=record.event.started_at,
        summary="candidate evidence",
        status=EvidenceStatus.SUCCESS,
        runtime_run_id=runtime_run_id,
    )
    audit_executions: list[AgentExecution] = []

    first = runtime._admit_candidates(
        (
            RootCauseCandidate(
                id="candidate-1",
                summary="first",
                rank=1,
                confidence=0.6,
                affected_entity=record.event.service,
                failure_mechanism="bounded failure mechanism",
                supporting_evidence_ids=[evidence.id],
            ),
        ),
        batch_findings=[],
        committed_evidence=[evidence],
        repository=repository,
        investigation_id=record.id,
        task=task,
        instance_id="investigator-first",
        audit_executions=audit_executions,
    )
    second = runtime._admit_candidates(
        (
            RootCauseCandidate(
                id="candidate-1",
                summary="second",
                rank=1,
                confidence=0.5,
                affected_entity=record.event.service,
                failure_mechanism="bounded failure mechanism",
                supporting_evidence_ids=[evidence.id],
            ),
        ),
        batch_findings=[],
        committed_evidence=[evidence],
        repository=repository,
        investigation_id=record.id,
        task=task,
        instance_id="investigator-second",
        audit_executions=audit_executions,
    )

    runtime._persist_candidate_projection(repository, record.id, first)
    runtime._persist_candidate_projection(repository, record.id, second)

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert [item.rank for item in review.candidates] == [1, 2]
    assert [item.summary for item in review.candidates] == ["first", "second"]


def test_strict_output_tool_uses_provider_subset_and_full_local_validation():
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str = v11_runtime_module.Field(min_length=3, max_length=8)

    tool = v11_runtime_module._strict_output_tool(StrictOutput)

    assert tool.strict_json_schema is True
    assert tool.params_json_schema["additionalProperties"] is False
    assert set(tool.params_json_schema["properties"]) == {"payload_json"}
    assert tool.params_json_schema["properties"]["payload_json"] == {
        "type": "string"
    }
    assert '"value"' in tool.description
    assert '"minLength": 3' in tool.description
    with pytest.raises(ModelBehaviorError):
        asyncio.run(
            tool.on_invoke_tool(
                None, '{"payload_json":"{\\"value\\":\\"x\\"}"}'
            )
        )
    assert asyncio.run(
        tool.on_invoke_tool(
            None, '{"payload_json":"{\\"value\\":\\"valid\\"}"}'
        )
    ) == StrictOutput(value="valid")


@pytest.mark.anyio
async def test_strict_output_tool_transport_configures_agent_finalization(monkeypatch):
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    captured = {}

    async def fake_run(agent, *_args, **_kwargs):
        captured["agent"] = agent
        return SimpleNamespace(
            final_output=StrictOutput(value="ok"), raw_responses=[]
        )

    monkeypatch.setattr(v11_runtime_module, "_run_with_model_lifecycle", fake_run)
    runtime = V11Runtime(
        model=OpenAICompatibleChatCompletionsModel(
            model="compat-model",
            api_key="local-secret",
            base_url="http://127.0.0.1:8000/v1",
            structured_output_transport="strict_output_tool",
        ),
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        token_budget=1000,
    )

    result = await runtime._call_model(
        actor="LeadAgent",
        prompt="return a structured result",
        output_type=StrictOutput,
        context={"request": "bounded"},
        tools=[],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
    )

    agent = captured["agent"]
    assert result.output == StrictOutput(value="ok")
    assert [tool.name for tool in agent.tools] == ["submit_structured_output"]
    assert agent.tool_use_behavior == {
        "stop_at_tool_names": ["submit_structured_output"]
    }
    assert agent.model_settings.tool_choice == "required"
    assert agent.reset_tool_choice is False


@pytest.mark.anyio
async def test_strict_output_tool_transport_completes_real_sdk_tool_loop(monkeypatch):
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    received_arguments = []

    async def read_logs(_context, raw_input):
        received_arguments.append(raw_input)
        return "log evidence"

    diagnostic_tool = FunctionTool(
        name="read_logs",
        description="Read log evidence.",
        params_json_schema={
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 240}},
            "required": ["reason"],
            "additionalProperties": False,
        },
        on_invoke_tool=read_logs,
        strict_json_schema=True,
    )

    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            type(self).calls += 1
            if self.calls == 1:
                name = "read_logs"
                arguments = '{"payload_json":"{\\"reason\\":\\"inspect\\"}"}'
            else:
                name = "submit_structured_output"
                arguments = '{"payload_json":"{\\"value\\":\\"valid\\"}"}'
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        arguments=arguments,
                        call_id=f"call-{self.calls}",
                        name=name,
                        type="function_call",
                    )
                ],
                usage=Usage(input_tokens=10, output_tokens=5),
                response_id=f"response-{self.calls}",
            )

    monkeypatch.setattr(
        compatible_model_module, "AsyncOpenAI", lambda **_kwargs: FakeClient()
    )
    monkeypatch.setattr(
        compatible_model_module, "OpenAIChatCompletionsModel", FakeDelegate
    )
    runtime = V11Runtime(
        model=OpenAICompatibleChatCompletionsModel(
            model="compat-model",
            api_key="local-secret",
            base_url="http://127.0.0.1:8000/v1",
            structured_output_transport="strict_output_tool",
        ),
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        token_budget=2000,
    )

    result = await runtime._call_model(
        actor="LeadAgent",
        prompt="inspect logs, then submit the structured result",
        output_type=StrictOutput,
        context={"request": "bounded"},
        tools=[diagnostic_tool],
        remaining_token_budget=2000,
        remaining_tool_budget=8,
    )

    assert result.output == StrictOutput(value="valid")
    assert received_arguments == ['{"reason":"inspect"}']
    assert FakeDelegate.calls == 2


@pytest.mark.anyio
async def test_budget_reservation_counts_projected_strict_transport_tools(monkeypatch):
    captured = {}

    async def fake_reserve(requested_budget, prompt, context, **_kwargs):
        captured.update(
            requested_budget=requested_budget,
            prompt=prompt,
            context=context,
        )
        return 100, 900, 1000

    async def fake_settle(*_args, **_kwargs):
        return None

    class Delegate(Model):
        async def get_response(self, *_args, **_kwargs):
            return ModelResponse(output=[], usage=Usage(), response_id=None)

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    compatible_delegate = OpenAICompatibleChatCompletionsModel(
        model="compat-model",
        api_key="local-secret",
        base_url="http://127.0.0.1:8000/v1",
        structured_output_transport="strict_output_tool",
    )
    async def fake_delegate_response(*_args, **_kwargs):
        return ModelResponse(output=[], usage=Usage(), response_id=None)

    monkeypatch.setattr(compatible_delegate, "get_response", fake_delegate_response)
    runtime = V11Runtime(model=compatible_delegate, token_budget=1000)
    monkeypatch.setattr(runtime, "_reserve_model_budget", fake_reserve)
    monkeypatch.setattr(runtime, "_settle_model_budget", fake_settle)
    schema_heavy_tool = FunctionTool(
        name="read_logs",
        description="Read logs.",
        params_json_schema={
            "type": "object",
            "properties": {"reason": {"type": "string", "maxLength": 240}},
            "required": ["reason"],
            "additionalProperties": False,
        },
        on_invoke_tool=lambda *_args: None,
        strict_json_schema=True,
    )
    output_schema = AgentOutputSchema(LeadPlanningOutput)
    budgeted = _V11BudgetedModel(compatible_delegate, runtime)

    await budgeted.get_response(
        system_instructions="plan",
        input="incident",
        model_settings=ModelSettings(max_tokens=1000),
        tools=[schema_heavy_tool],
        output_schema=output_schema,
        handoffs=[],
        tracing=None,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    projected = captured["context"]["tools"][0]
    assert projected["schema"] == compatible_model_module.STRICT_TOOL_ENVELOPE_SCHEMA
    assert '"maxLength": 240' in projected["description"]
    assert captured["context"]["output_schema"] is None


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


def _complete_contract(
    registry,
    *,
    manifest: tuple[str, ...] | None = None,
    model_name: str = "fake",
    model_provider: str = "openai",
    api_mode: str = "responses",
    endpoint_id: str | None = None,
    artifact_hash: str | None = None,
) -> dict:
    frozen_manifest = manifest or registry.agent_manifest()
    frozen_endpoint_id = endpoint_id
    if model_provider == "openai" and frozen_endpoint_id is None:
        frozen_endpoint_id = endpoint_identity(OFFICIAL_OPENAI_BASE_URL)
    return seal_v11_execution_contract(
        {
            "execution_contract_version": "v11",
            "authority_mode": "agent",
            "model_provider": model_provider,
            "model_name": model_name,
            "prompt_version": "v11-test",
            "api_mode": api_mode,
            "endpoint_id": frozen_endpoint_id,
            "capability_artifact_hash": artifact_hash,
            "tool_manifest": list(frozen_manifest),
            "tool_manifest_hash": agent_manifest_hash(frozen_manifest),
            "skill_catalog": {
                "catalog_version": SKILL_CATALOG_VERSION,
                "catalog_hash": skill_catalog_hash(DIAGNOSTIC_SKILLS),
                "skill_names": ",".join(
                    f"{skill.name}@{skill.version}" for skill in DIAGNOSTIC_SKILLS
                ),
            },
            "capability_identity": {
                "provider": model_provider,
                "model": model_name,
                "api_mode": api_mode,
                "structured_output_transport": "native_json_schema",
                "endpoint_id": frozen_endpoint_id,
                "artifact_hash": artifact_hash,
            },
            "limits": {
                "max_turns": 8,
                "max_investigators": 3,
                "max_rounds": 2,
                "token_budget": 10000,
                "max_tool_calls_per_specialist": 3,
                "tool_timeout_seconds": 10,
            },
            "topology": {
                "mode": "multi_lead_investigators_critic",
                "one_context": False,
                "critic": True,
                "subagent": False,
                "hidden_model_calls": False,
            },
            "retry_policy": {
                "max_retries": 1,
                "retryable_categories": ["transport", "rate_limit"],
                "provider_max_retries": 0,
                "sdk_max_retries": 0,
            },
            "tool_budget": 8,
            "token_budget": 10000,
            "timeout_seconds": 60.0,
        }
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
        remaining_token_budget=3000,
    )

    assert plan.runtime_run_id == "run-v11"
    assert plan.lead_decision is not None
    assert plan.lead_decision.action == LeadAction.INVESTIGATE
    assert len(plan.tasks) == 1
    assert plan.tasks[0].id != "task-timeline"
    assert plan.lead_decision.task_ids == [plan.tasks[0].id]
    assert repository.get_plan(record.id).model_dump(mode="json") == plan.model_dump(
        mode="json"
    )
    assert calls[0]["remaining_tool_budget"] == 8
    assert 0 < calls[0]["remaining_token_budget"] <= 3000


@pytest.mark.anyio
async def test_v11_run_owner_is_explicit_when_repository_has_multiple_investigations():
    repository, record = _repository()
    other = repository.save(
        InvestigationRecord(id="inv-other", event=_event())
    )

    async def turn(**_kwargs):
        return {
            "decision": {
                "action": "investigate",
                "summary": "collect one bounded signal",
                "task_ids": ["task-target"],
            },
            "tasks": [
                {
                    "id": "task-target",
                    "title": "Inspect target",
                    "description": "Inspect the target investigation only.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
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

    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-target",
        remaining_tool_budget=8,
        remaining_token_budget=3000,
    )

    assert repository.get_plan(record.id) is not None
    assert repository.get_plan(other.id) is None
    assert [item.runtime_run_id for item in repository.list_executions(record.id)] == [
        "run-target"
    ]
    assert repository.list_executions(other.id) == []


@pytest.mark.anyio
async def test_v11_model_turn_budget_is_shared_by_multiple_model_invocations():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    remaining = [1]

    async def reserve_turn() -> int:
        if remaining[0] <= 0:
            raise V11RuntimeContractError("V11 model turn budget exhausted")
        remaining[0] -= 1
        return remaining[0]

    async def turn(**_kwargs):
        return {"value": "ok"}

    class Output(BaseModel):
        value: str

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=registry,
        turn=turn,
    )
    runtime.bind_phase(
        PhaseInput(
            run_id="run-v11-turn-budget",
            attempt_id="attempt-v11-turn-budget",
            phase=RuntimePhase.LEAD_PLANNING,
            resume_state=RuntimeResumeState(
                remaining_model_turns=1,
                remaining_token_budget=1000,
            ),
            execution_contract_version="v11",
            execution_contract=_complete_contract(registry),
            model_provider=ModelProvider.OPENAI,
            model_name="fake",
            reserve_model_turn=reserve_turn,
        )
    )
    kwargs = {
        "actor": "LeadAgent",
        "prompt": "bounded",
        "output_type": Output,
        "context": {},
        "tools": [],
        "remaining_token_budget": 1000,
    }
    await runtime._call_model(**kwargs)
    assert runtime.remaining_model_turns == 0
    with pytest.raises(V11RuntimeContractError, match="exhausted"):
        await runtime._call_model(**kwargs)


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
            remaining_token_budget=3000,
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


def test_v11_frozen_manifest_rejects_same_cardinality_tool_swap():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    frozen = registry.agent_manifest()
    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=registry,
        turn=lambda **_kwargs: None,
    )
    runtime.bind_phase(
        PhaseInput(
            run_id="run-v11",
            attempt_id="attempt-v11",
            phase="lead_planning",
            resume_state=RuntimeResumeState(),
            execution_contract=_complete_contract(registry, manifest=frozen),
            model_provider=ModelProvider.OPENAI,
            model_name="fake",
        )
    )
    removed = frozen[0]
    registry._specs.pop(removed)
    registry._handlers.pop(removed)
    registry.register(
        ToolSpec(
            name="replacement_tool",
            description="replacement",
        ),
        lambda **_kwargs: None,
    )

    with pytest.raises(V11RuntimeContractError):
        runtime._agent_manifest()


def test_v11_frozen_manifest_rejects_unordered_contract():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    frozen = registry.agent_manifest()
    runtime = V11Runtime(
        model="fake", model_provider=ModelProvider.OPENAI, model_name="fake", tool_registry=registry
    )
    runtime._execution_contract = _complete_contract(
        registry, manifest=tuple(reversed(frozen))
    )

    with pytest.raises(V11RuntimeContractError, match="ordered"):
        runtime._agent_manifest()


def test_v11_nested_execution_contract_digest_fences_capability_and_limits():
    container = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            agents=AgentsSettings(enabled=True, model="gpt-test"),
        )
    )
    record = container.repository.save(
        InvestigationRecord(id="inv-contract-fence", event=_event())
    )
    run = container.create_runtime_run(
        record.id,
        strategy="adaptive",
        run_reason="initial",
        execution_contract_version="v11",
    )
    contract = json.loads(json.dumps(run.execution_contract))
    contract["capability_identity"]["artifact_hash"] = "f" * 64
    contract["limits"]["max_turns"] = 1

    with pytest.raises(V11RuntimeContractError, match="execution contract"):
        container.orchestrator.v11_runtime.clone_for_run(
            runtime_run_id=run.id,
            model_provider=run.model_provider,
            model_name=run.model_name,
            token_budget=run.token_budget,
            timeout_seconds=run.timeout_seconds,
            execution_contract=contract,
        )
    contract = json.loads(json.dumps(run.execution_contract))
    contract["limits"]["max_tool_calls_per_specialist"] = 5
    contract = seal_v11_execution_contract(contract)
    cloned = container.orchestrator.v11_runtime.clone_for_run(
        runtime_run_id=run.id,
        model_provider=run.model_provider,
        model_name=run.model_name,
        token_budget=run.token_budget,
        timeout_seconds=run.timeout_seconds,
        execution_contract=contract,
    )
    assert cloned.max_tool_calls_per_specialist == 5
    cloned._update_summary(container.repository, record.id)
    assert (
        container.repository.get(record.id).multi_agent_run.max_tool_calls_per_specialist
        == 5
    )
    container.close()


def test_v11_clone_disables_compatible_provider_retry_for_durable_run():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    source_model = OpenAICompatibleChatCompletionsModel(
        model="compat-model",
        api_key="local-secret",
        base_url="http://127.0.0.1:8000/v1",
    )
    runtime = V11Runtime(
        model=source_model,
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        tool_registry=registry,
    )
    contract = _complete_contract(
        registry,
        model_name="compat-model",
        model_provider="openai_compatible",
        api_mode="chat_completions",
        endpoint_id=endpoint_identity("http://127.0.0.1:8000/v1"),
        artifact_hash="a" * 64,
    )

    cloned = runtime.clone_for_run(
        runtime_run_id="run-provider-retry-fence",
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        token_budget=10000,
        timeout_seconds=60,
        execution_contract=contract,
    )

    assert isinstance(cloned.model, OpenAICompatibleChatCompletionsModel)
    assert cloned.model._max_retries == 0


@pytest.mark.anyio
async def test_v11_string_provider_client_disables_sdk_transport_retry(monkeypatch):
    captured: dict[str, object] = {}

    class FakeClient:
        base_url = OFFICIAL_OPENAI_BASE_URL

        async def close(self) -> None:
            captured["closed"] = True

    def build_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(v11_runtime_module, "AsyncOpenAI", build_client)
    provider = v11_runtime_module._V11ModelProvider(
        V11Runtime(model="gpt-test")
    )

    assert captured["max_retries"] == 0
    await provider.aclose()
    assert captured["closed"] is True


@pytest.mark.anyio
async def test_v11_sdk_provider_retry_is_only_the_persisted_outer_retry(monkeypatch):
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            429,
            request=request,
            json={"error": {"message": "rate limited", "type": "rate_limit"}},
        )

    def build_client(**kwargs):
        return openai.AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(
        "backend.diagnosis.openai_compatible_model.AsyncOpenAI", build_client
    )

    async def _no_sleep(_seconds):
        return None

    # 外层重试预算 1→3（运营噪声链），退避 sleep 打桩掉以保持测试速度
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    runtime = V11Runtime(
        model=OpenAICompatibleChatCompletionsModel(
            model="compat-model",
            api_key="local-secret",
            base_url="http://127.0.0.1:8000/v1",
        ),
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        token_budget=1000,
    )

    with pytest.raises(openai.RateLimitError):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="bounded request",
            output_type=StrictOutput,
            context={"request": "bounded"},
            tools=[],
            remaining_token_budget=1000,
            remaining_tool_budget=8,
        )

    # 外层持久化重试预算为 3：1 次首发 + 3 次重试 = 4 个请求
    assert request_count == 4


@pytest.mark.anyio
async def test_v11_sdk_model_requests_reserve_decreasing_output_caps():
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    class TwoRequestModel(Model):
        def __init__(self) -> None:
            self.calls = 0
            self.output_caps: list[int | None] = []

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id,
            conversation_id,
            prompt,
        ):
            del (
                system_instructions,
                input,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id,
                conversation_id,
                prompt,
            )
            self.calls += 1
            self.output_caps.append(model_settings.max_tokens)
            if self.calls == 1:
                output = [
                    ResponseFunctionToolCall(
                        arguments="{}",
                        call_id="probe-call",
                        name="probe",
                        type="function_call",
                    )
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        id="final-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ]
            return ModelResponse(
                output=output,
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id=f"response-{self.calls}",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    model = TwoRequestModel()

    async def probe(_context, _raw_input):
        return "probe result"

    tool = FunctionTool(
        name="probe",
        description="bounded probe",
        params_json_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        on_invoke_tool=probe,
    )
    runtime = V11Runtime(model=model, token_budget=1000)

    await runtime._call_model(
        actor="LeadAgent",
        prompt="bounded request",
        output_type=StrictOutput,
        context={"request": "bounded"},
        tools=[tool],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
    )

    assert model.calls == 2
    assert model.output_caps[1] < model.output_caps[0]


@pytest.mark.anyio
async def test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request():
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    class ReplayModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id,
            conversation_id,
            prompt,
        ):
            del (
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                previous_response_id,
                conversation_id,
                prompt,
            )
            self.calls += 1
            if self.calls in {1, 3}:
                output = [
                    ResponseFunctionToolCall(
                        arguments="{}",
                        call_id=f"probe-call-{self.calls}",
                        name="probe",
                        type="function_call",
                    )
                ]
            elif self.calls == 2:
                raise ClassifiedRetryableError(FailureCategory.TRANSPORT)
            else:
                output = [
                    ResponseOutputMessage(
                        id="final-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ]
            return ModelResponse(
                output=output,
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id=f"response-{self.calls}",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    async def probe(_context, _raw_input):
        return "probe result"

    tool = FunctionTool(
        name="probe",
        description="bounded probe",
        params_json_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        on_invoke_tool=probe,
    )
    model = ReplayModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-sdk-usage-retry"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="bounded replay request",
        output_type=StrictOutput,
        context={"request": "two provider requests"},
        tools=[tool],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
        repository=repository,
        investigation_id=record.id,
        task_id="task-sdk-usage-retry",
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
    )

    assert model.calls == 4
    started = [payload for status, payload in events if status == "started"]
    completed = [payload for status, payload in events if status == "completed"]
    retrying = [
        payload
        for status, payload in events
        if status == "failed" and payload.get("reservation_status") == "retrying"
    ]
    assert len(started) == 4
    assert len(completed) == 3
    assert len(retrying) == 1
    assert [payload["actual_input_tokens"] for payload in completed] == [20, 20, 20]
    first_request = started[0]["reservation_id"]
    failed_request = retrying[0]["reservation_id"]
    assert started[2]["reservation_id"] != first_request
    assert started[3]["reservation_id"] == failed_request
    assert failed_request != first_request
    assert runtime._model_reservations == {}
    assert runtime.remaining_token_budget == 1000 - sum(
        int(payload["input_tokens"]) + int(payload["output_tokens"])
        for payload in completed
    )
    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    assert summary.total_input_tokens == 60
    assert summary.total_output_tokens == 15
    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.runtime_run_id == runtime.runtime_run_id
    ]
    assert len(executions) == 2
    attempt_one = next(item for item in executions if item.attempt == 1)
    attempt_two = next(item for item in executions if item.attempt == 2)
    assert attempt_one.status == AgentExecutionStatus.FAILED
    assert attempt_one.input_tokens == 20 + int(retrying[0]["input_estimate"])
    assert attempt_one.output_tokens == 5
    assert attempt_two.status == AgentExecutionStatus.COMPLETED
    assert attempt_two.input_tokens == 40
    assert attempt_two.output_tokens == 10


@pytest.mark.anyio
async def test_v11_single_sdk_request_usage_is_not_counted_twice():
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    class SingleRequestModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, **_kwargs):
            self.calls += 1
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="single-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ],
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id="single-response",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    model = SingleRequestModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-single-usage"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="single request usage",
        output_type=StrictOutput,
        context={"request": "one"},
        tools=[],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
        repository=repository,
        investigation_id=record.id,
        task_id="task-single-usage",
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
    )

    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    assert model.calls == 1
    assert summary.total_input_tokens == 20
    assert summary.total_output_tokens == 5
    completed = [payload for status, payload in events if status == "completed"]
    assert len(completed) == 1
    assert completed[0]["actual_input_tokens"] == 20
    execution = repository.list_executions(record.id)[0]
    assert execution.input_tokens == 20
    assert execution.output_tokens == 5


@pytest.mark.anyio
async def test_v11_all_failed_sdk_retry_records_estimate_without_summary_usage(
    monkeypatch,
):
    async def _no_sleep(_seconds):
        return None

    # 外层重试预算 1→3（运营噪声链），退避 sleep 打桩掉以保持测试速度
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    class AlwaysFailModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, **_kwargs):
            self.calls += 1
            raise ClassifiedRetryableError(FailureCategory.TRANSPORT)

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    model = AlwaysFailModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-all-failed-usage"
    runtime._persist_model_event = persist_model_event

    with pytest.raises(ClassifiedRetryableError):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="all failed usage",
            output_type=BaseModel,
            context={"request": "fail"},
            tools=[],
            remaining_token_budget=1000,
            remaining_tool_budget=8,
            repository=repository,
            investigation_id=record.id,
            task_id="task-all-failed-usage",
            step_kind=ExecutionStepKind.LEAD_PLANNING,
            analysis_round=1,
        )

    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    # 外层持久化重试预算为 3：1 次首发 + 3 次重试 = 4 次调用 / 4 条 FAILED 审计
    assert model.calls == 4
    assert summary.total_input_tokens == 0
    assert summary.total_output_tokens == 0
    assert runtime._input_tokens == 0
    assert runtime._output_tokens == 0
    assert runtime._model_reservations == {}
    assert not [status for status, _payload in events if status == "completed"]
    executions = repository.list_executions(record.id)
    assert len(executions) == 4
    assert all(item.status == AgentExecutionStatus.FAILED for item in executions)
    assert all(item.input_tokens > 0 for item in executions)
    assert all(item.output_tokens == 0 for item in executions)


@pytest.mark.anyio
async def test_v11_sdk_retry_resume_usage_is_idempotent():
    class RetryResumeModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ClassifiedRetryableError(FailureCategory.TRANSPORT)
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="retry-resume-message",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"value":"ok"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ],
                usage=Usage(input_tokens=20, output_tokens=5),
                response_id="retry-resume-response",
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    repository, record = _repository()
    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        events.append((status, payload))

    model = RetryResumeModel()
    runtime = V11Runtime(model=model, token_budget=1000)
    runtime.runtime_run_id = "run-retry-resume-usage"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="retry resume usage",
        output_type=BaseModel,
        context={"request": "retry once"},
        tools=[],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
        repository=repository,
        investigation_id=record.id,
        task_id="task-retry-resume-usage",
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
    )

    runtime._update_summary(repository, record.id)
    summary = repository.get(record.id).multi_agent_run
    assert summary is not None
    assert model.calls == 2
    assert summary.total_input_tokens == 20
    assert summary.total_output_tokens == 5
    completed = [payload for status, payload in events if status == "completed"]
    assert len(completed) == 1
    reservation_id = completed[0]["reservation_id"]
    await runtime._settle_model_budget(
        1000,
        25,
        reservation_id=reservation_id,
        logical_call_id=completed[0]["logical_call_id"],
        input_tokens=20,
        actual_input_tokens=20,
        output_tokens=5,
        reservation_status="completed",
    )
    assert runtime._input_tokens == 20
    assert runtime._output_tokens == 5
    assert runtime._model_reservations == {}


@pytest.mark.anyio
async def test_v11_sdk_replay_cursor_rehydrates_and_repeated_replay_is_idempotent():
    logical_call_id = "logical-replay-history"
    original_request = f"{logical_call_id}:request-1"
    failed_request = f"{logical_call_id}:request-2"
    common = {
        "run_id": "run-replay-history",
        "attempt_id": "attempt-1",
        "phase": RuntimePhase.LEAD_PLANNING,
        "actor_type": RuntimeActorType.MODEL,
        "actor_name": "LeadAgent",
    }
    model_events = (
        RuntimeEvent(
            **common,
            sequence=1,
            event_type=RuntimeEventType.MODEL_STARTED,
            execution_id="execution-1",
            safe_payload={
                "status": "started",
                "logical_call_id": logical_call_id,
                "reservation_id": original_request,
                "reservation_status": "reserved",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 0,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=2,
            event_type=RuntimeEventType.MODEL_COMPLETED,
            execution_id="execution-1",
            safe_payload={
                "status": "completed",
                "input_tokens": 20,
                "output_tokens": 5,
                "logical_call_id": logical_call_id,
                "reservation_id": original_request,
                "reservation_status": "completed",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 0,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=3,
            event_type=RuntimeEventType.MODEL_STARTED,
            execution_id="execution-1",
            safe_payload={
                "status": "started",
                "logical_call_id": logical_call_id,
                "reservation_id": failed_request,
                "reservation_status": "reserved",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 1,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=4,
            event_type=RuntimeEventType.MODEL_FAILED,
            execution_id="execution-1",
            safe_payload={
                "status": "failed",
                "logical_call_id": logical_call_id,
                "reservation_id": failed_request,
                "reservation_status": "retrying",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 1,
                "request_index": 1,
            },
        ),
        RuntimeEvent(
            **common,
            sequence=5,
            event_type=RuntimeEventType.MODEL_FAILED,
            execution_id="execution-recovery",
            safe_payload={
                "status": "failed",
                "logical_call_id": logical_call_id,
                "reservation_id": failed_request,
                "reservation_status": "released",
                "reserved_tokens": 100,
                "input_estimate": 10,
                "attempt": 2,
                "request_index": 1,
            },
        ),
    )

    class ReplayDelegate(Model):
        async def get_response(self, **_kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=3, output_tokens=4)
            )

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    persisted: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        payload = dict(safe_payload or {})
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        persisted.append((status, payload))

    runtime = V11Runtime(model=ReplayDelegate(), token_budget=975)
    runtime.bind_phase(
        PhaseInput(
            run_id="run-replay-history",
            attempt_id="attempt-2",
            phase=RuntimePhase.LEAD_PLANNING,
            resume_state=RuntimeResumeState(remaining_token_budget=975),
            token_budget=975,
            model_events=model_events,
        )
    )
    runtime._persist_model_event = persist_model_event
    settings = ModelSettings(max_tokens=100)
    first = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id=logical_call_id,
        execution_id="execution-2",
        attempt_number=1,
        actor="LeadAgent",
    )
    await first.get_response(
        input="replay request one",
        system_instructions="system",
        model_settings=settings,
    )
    await first.get_response(
        input="replay request two",
        system_instructions="system",
        model_settings=settings,
    )

    repeated = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id=logical_call_id,
        execution_id="execution-3",
        attempt_number=1,
        actor="LeadAgent",
    )
    await repeated.get_response(
        input="repeated replay request one",
        system_instructions="system",
        model_settings=settings,
    )

    started = [payload for status, payload in persisted if status == "started"]
    assert started[0]["reservation_id"] == (
        f"{logical_call_id}:replay-1:request-1"
    )
    assert started[1]["reservation_id"] == (
        f"{logical_call_id}:replay-1:request-2"
    )
    assert started[1]["reservation_id"] != failed_request
    assert started[2]["reservation_id"] == f"{logical_call_id}:replay-2:request-1"
    assert all(payload.get("request_index") is not None for payload in started)
    assert runtime._model_reservations == {}
    completed = [payload for status, payload in persisted if status == "completed"]
    assert runtime.remaining_token_budget == 975 - sum(
        int(payload.get("input_tokens", 0)) + int(payload.get("output_tokens", 0))
        for payload in completed
    )


@pytest.mark.anyio
async def test_v11_sdk_budget_reservation_is_released_when_preflight_rejects():
    class NoRequestModel(Model):
        async def get_response(self, *_args, **_kwargs):
            raise AssertionError("preflight must reject before provider request")

        async def stream_response(self, *_args, **_kwargs):
            if False:
                yield None

    runtime = V11Runtime(model=NoRequestModel(), token_budget=100)
    proxy = _V11BudgetedModel(runtime.model, runtime)

    def reject_execution() -> None:
        raise V11RuntimeContractError("cancelled")

    runtime._check_execution = reject_execution
    with pytest.raises(V11RuntimeContractError, match="cancelled"):
        await proxy.get_response(
            system_instructions="inspect",
            input="one bounded request",
            model_settings=ModelSettings(max_tokens=80),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=SimpleNamespace(is_disabled=lambda: True),
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )

    assert runtime.remaining_token_budget == 100


@pytest.mark.anyio
async def test_v11_investigators_share_a_bounded_concurrent_gate():
    repository, record = _repository()
    runtime_run_id = "run-investigator-concurrency"
    tasks = [
        DiagnosisTask(
            id=f"task-concurrent-{index}",
            title="bounded investigation",
            description="inspect one isolated signal",
            task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
            agent_name="InvestigatorAgent",
            analysis_round=1,
            evidence_scope={"entity_ids": [record.event.service]},
            runtime_run_id=runtime_run_id,
        )
        for index in range(3)
    ]
    repository.save_plan(
        DiagnosisPlan(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            tasks=tasks,
            lead_decision=LeadDecision(
                action=LeadAction.INVESTIGATE,
                summary="run bounded investigators",
                task_ids=[task.id for task in tasks],
            ),
        )
    )
    active = 0
    peak = 0

    async def turn(**_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return {"summary": "completed", "findings": [], "candidates": []}
        finally:
            active -= 1

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        max_investigators=3,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert peak == 3
    assert active == 0


@pytest.mark.anyio
async def test_v11_all_investigator_failure_clears_prior_diagnostic_projection():
    repository, record = _repository()
    runtime_run_id = "run-investigator-terminal-projection"
    task = DiagnosisTask(
        id="task-terminal-projection",
        title="bounded investigation",
        description="inspect one isolated signal",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        evidence_scope={"entity_ids": [record.event.service]},
        runtime_run_id=runtime_run_id,
    )
    repository.save_plan(
        DiagnosisPlan(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            tasks=[task],
            lead_decision=LeadDecision(
                action=LeadAction.INVESTIGATE,
                summary="run investigator",
                task_ids=[task.id],
            ),
        )
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[
                RootCauseCandidate(
                    id="candidate-stale",
                    summary="stale projection",
                    rank=1,
                    confidence=0.5,
                )
            ],
            lead_decision=LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="stale decision",
                stop_reason="insufficient_evidence",
            ),
            diagnostic_status="inconclusive",
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("all investigators unavailable")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.critic_assessments == []
    assert review.lead_decision is None
    assert review.diagnostic_status is None
    assert repository.get(record.id).multi_agent_run.diagnostic_status is None


@pytest.mark.anyio
async def test_v11_model_retry_is_one_classified_transport_attempt():
    repository, record = _repository()
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "https://provider.test/model")
            response = httpx.Response(429, request=request)
            raise openai.RateLimitError("limited", response=response, body={})
        return {
            "decision": {
                "action": "investigate",
                "summary": "retry succeeded",
                "task_ids": ["task-retry"],
            },
            "tasks": [
                {
                    "id": "task-retry",
                    "title": "Retry bounded task",
                    "description": "Inspect the committed signal.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
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

    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-retry",
        remaining_tool_budget=8,
        remaining_token_budget=5000,
    )

    assert calls == 2
    executions = repository.list_executions(record.id)
    planning = [
        item
        for item in executions
        if item.step_kind == ExecutionStepKind.LEAD_PLANNING
    ]
    assert len(planning) == 2
    assert [item.attempt for item in planning] == [1, 2]
    assert planning[0].status == AgentExecutionStatus.FAILED
    assert planning[1].status == AgentExecutionStatus.COMPLETED
    assert planning[1].resume_from_execution_id == planning[0].id


@pytest.mark.anyio
async def test_v11_parse_failure_marks_latest_retry_attempt_invalid_output():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-parse-retry",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-parse-retry",
            authority_mode="agent",
            candidates=[candidate],
        )
    )
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "https://provider.test/model")
            response = httpx.Response(429, request=request)
            raise openai.RateLimitError("limited", response=response, body={})
        return {"malformed": True}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        turn=turn,
        token_budget=1000,
    )
    runtime.runtime_run_id = "run-parse-retry"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert calls == 3
    assert [item.attempt for item in executions] == [1, 2, 3, 4]
    assert executions[0].status == AgentExecutionStatus.FAILED
    assert executions[1].status == AgentExecutionStatus.FAILED
    assert executions[2].status == AgentExecutionStatus.FAILED
    assert executions[3].status == AgentExecutionStatus.FAILED


@pytest.mark.anyio
async def test_v11_critic_success_has_one_durable_audit_execution():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-audited-critic",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-critic-audit",
            authority_mode="agent",
            candidates=[candidate],
        )
    )
    checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.UNKNOWN.value,
            "summary": "not enough committed evidence",
            "gap": "collect the missing signal",
        }
        for name in CausalCheckName
    ]

    async def turn(**_kwargs):
        return (
            {
                    "summary": "critic audited",
                    "assessments": [
                        {
                            "candidate_ref": candidate.id,
                            "verdict": CriticVerdict.INCONCLUSIVE.value,
                        "checks": checks,
                        "summary": "not enough committed evidence",
                    }
                ],
            },
            {"input_tokens": 3, "output_tokens": 5},
        )

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-critic-audit"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert len(executions) == 1
    assert executions[0].status == AgentExecutionStatus.COMPLETED
    assert executions[0].runtime_run_id == "run-critic-audit"
    assert executions[0].input_tokens == 3
    assert executions[0].output_tokens == 5
    assert executions[0].deadline_at is not None


@pytest.mark.anyio
async def test_v11_critic_output_failure_updates_one_audit_execution():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-invalid-critic-output",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-invalid-critic-output",
            authority_mode="agent",
            candidates=[candidate],
        )
    )

    async def turn(**_kwargs):
        return {"summary": "missing assessment", "assessments": []}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-invalid-critic-output"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert len(executions) == 1
    assert executions[0].status == AgentExecutionStatus.FAILED
    assert executions[0].failure_category == FailureCategory.INVALID_OUTPUT
    assert executions[0].error_message == "critic output invalid"
    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert [item.id for item in review.candidates] == [candidate.id]


@pytest.mark.anyio
async def test_v11_reconciliation_has_one_durable_audit_execution():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-reconciliation-audit",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    checks = [
        CausalCheck(
            name=name,
            status=CausalCheckStatus.UNKNOWN,
            summary="missing signal",
            gap="collect the signal",
        )
        for name in CausalCheckName
    ]
    assessment = CriticAssessment(
        id="assessment-reconciliation-audit",
        candidate_id=candidate.id,
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=checks,
        gap="collect the signal",
        supplemental_task_ids=["task-reconciliation-audit"],
        summary="needs one bounded signal",
        runtime_run_id="run-reconciliation-audit",
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-reconciliation-audit",
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        )
    )
    repository.save_tasks(
        record.id,
        [
            DiagnosisTask(
                id="task-reconciliation-audit",
                title="Collect the requested signal",
                description="Close the named evidence gap.",
                task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                agent_name="InvestigatorAgent",
                tool_names=[],
                analysis_round=2,
                evidence_scope={"entity_ids": [record.event.service]},
                runtime_run_id="run-reconciliation-audit",
                critic_assessment_id=assessment.id,
            )
        ],
    )

    reconciled_checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.UNKNOWN.value,
            "summary": "still missing signal",
            "gap": "collect the signal",
        }
        for name in CausalCheckName
    ]

    async def turn(**_kwargs):
        return (
            {
                    "summary": "reconciled once",
                    "assessments": [
                        {
                            "candidate_ref": candidate.id,
                            "verdict": CriticVerdict.INCONCLUSIVE.value,
                        "checks": reconciled_checks,
                        "summary": "still inconclusive",
                    }
                ],
            },
            {"input_tokens": 2, "output_tokens": 4},
        )

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-reconciliation-audit"

    await runtime.critic_reconciliation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    executions = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.CRITIC_REVIEW
    ]
    assert len(executions) == 1
    assert executions[0].analysis_round == 2
    assert executions[0].status == AgentExecutionStatus.COMPLETED
    assert executions[0].deadline_at is not None


@pytest.mark.anyio
async def test_v11_model_precharges_input_before_setting_output_cap():
    seen: list[dict] = []

    async def turn(**kwargs):
        seen.append(kwargs)
        return {
            "decision": {
                "action": "investigate",
                "summary": "bounded",
                "task_ids": ["task-1"],
            },
            "tasks": [
                {
                    "id": "task-1",
                    "title": "bounded",
                    "description": "bounded",
                    "tool_names": [],
                    "information_gap": "bounded",
                }
            ],
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        token_budget=1000,
    )

    await runtime._call_model(
        actor="LeadAgent",
        prompt="a long bounded prompt that must be charged before request",
        output_type=LeadPlanningOutput,
        context={"context": "also charged"},
        tools=[],
        remaining_token_budget=1000,
        remaining_tool_budget=8,
    )

    assert seen[0]["remaining_token_budget"] < 1000
    assert seen[0]["max_output_tokens"] == seen[0]["remaining_token_budget"]


@pytest.mark.anyio
async def test_v11_model_deadline_preflight_blocks_request_before_start():
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        return {"output": "late"}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.bind_phase(
        PhaseInput(
            run_id="run-deadline",
            attempt_id="attempt-deadline",
            phase="lead_planning",
            resume_state=RuntimeResumeState(),
            remaining_deadline_seconds=lambda: 0.0,
        )
    )

    with pytest.raises(V11RuntimeContractError, match="deadline"):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="must not start",
            output_type=LeadPlanningOutput,
            context={},
            tools=[],
            remaining_token_budget=None,
        )

    assert calls == 0


@pytest.mark.anyio
async def test_v11_zero_tool_budget_does_not_start_model():
    repository, record = _repository()
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        return {"output": "late"}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )

    with pytest.raises(V11RuntimeContractError, match="tool budget"):
        await runtime.plan_lead(
            repository=repository,
            investigation_id=record.id,
            event=record.event,
            runtime_run_id="run-zero-tool-budget",
            remaining_tool_budget=0,
            remaining_token_budget=None,
        )

    assert calls == 0


@pytest.mark.anyio
async def test_v11_model_only_phase_runs_after_tool_budget_is_exhausted():
    calls = 0

    async def turn(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["remaining_tool_budget"] == 0
        return {
            "decision": {
                "action": "inconclusive",
                "summary": "model-only phase completed",
                "task_ids": [],
                "candidate_ids": [],
                "stop_reason": "insufficient_evidence",
            }
        }

    runtime = V11Runtime(model="fake", turn=turn)

    result = await runtime._call_model(
        actor="LeadAgent",
        prompt="adjudicate the committed review",
        output_type=LeadAdjudicationOutput,
        context={},
        tools=[],
        remaining_token_budget=None,
        remaining_tool_budget=0,
    )

    assert calls == 1
    assert result.output["decision"]["summary"] == "model-only phase completed"


@pytest.mark.anyio
async def test_v11_all_investigator_failure_is_terminal_failed_without_diagnostic():
    repository, record = _repository()
    turns = [
        {
            "decision": {
                "action": "investigate",
                "summary": "plan one task",
                "task_ids": ["task-fail"],
            },
            "tasks": [
                {
                    "id": "task-fail",
                    "title": "Failing investigator",
                    "description": "The provider will fail before output.",
                    "analysis_round": 1,
                    "tool_names": ["read_logs"],
                    "evidence_scope": {"entity_ids": ["checkout-service"]},
                }
            ],
        },
        RuntimeError("investigator transport failed"),
    ]

    async def turn(**_kwargs):
        value = turns.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-investigator-failed",
        remaining_tool_budget=8,
        remaining_token_budget=3000,
    )

    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert repository.get(record.id).status == InvestigationStatus.FAILED
    review = repository.get_coordination_review(record.id)
    assert review is None or review.diagnostic_status is None


@pytest.mark.anyio
async def test_v11_required_critic_failure_is_failed_without_inconclusive_fallback():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-failed-critic",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-critic-failed",
            authority_mode="agent",
            candidates=[candidate],
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("critic failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-critic-failed"

    await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.critic_assessments == []
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v11_persistence_failure_is_terminal_failed_without_diagnostic():
    class FailOnceRepository(InMemoryInvestigationRepository):
        fail_next_review_save = False

        def save_coordination_review(self, review):
            if self.fail_next_review_save:
                self.fail_next_review_save = False
                raise RuntimeError("review persistence failed")
            return super().save_coordination_review(review)

    repository = FailOnceRepository()
    record = repository.save(InvestigationRecord(id="inv-persistence-failed", event=_event()))
    candidate = RootCauseCandidate(
        id="candidate-persistence-failed",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-persistence-failed",
            authority_mode="agent",
            candidates=[candidate],
        )
    )
    repository.fail_next_review_save = True

    async def turn(**_kwargs):
        return {"summary": "critic result", "assessments": []}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-persistence-failed"

    with pytest.raises(RuntimeError, match="review persistence failed"):
        await runtime.run_phase(
            "critic_review",
            repository=repository,
            investigation_id=record.id,
            event=record.event,
        )

    assert repository.get(record.id).status == InvestigationStatus.FAILED
    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.diagnostic_status is None
    assert review.run_status.value == "failed"


@pytest.mark.anyio
async def test_v11_required_lead_failure_is_failed_without_inconclusive_fallback():
    repository, record = _repository()
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-lead-failed",
            authority_mode="agent",
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("lead failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-lead-failed"

    await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.critic_assessments == []
    assert review.lead_decision is None
    assert review.diagnostic_status is None
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v11_validator_correction_skipped_when_budget_exhausted():
    """预算耗尽时 correction 调用必败；跳过它直接 terminal，不发起模型调用。"""
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-budget-correction",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-budget-correction",
            authority_mode="agent",
            candidates=[candidate],
            lead_decision=LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="not enough evidence",
                stop_reason="insufficient_evidence",
            ),
            diagnostic_status="inconclusive",
        )
    )
    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("correction must not be attempted without budget")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
        token_budget=0,
    )
    runtime.runtime_run_id = "run-budget-correction"

    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert calls == 0
    assert "correction_budget_exhausted" in runtime._failures
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v11_validator_failure_is_failed_without_inconclusive_fallback():
    repository, record = _repository()
    candidate = RootCauseCandidate(
        id="candidate-validator-failed",
        summary="candidate",
        rank=1,
        confidence=0.5,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-validator-failed",
            authority_mode="agent",
            candidates=[candidate],
            lead_decision=LeadDecision(
                action=LeadAction.INCONCLUSIVE,
                summary="not enough evidence",
                stop_reason="insufficient_evidence",
            ),
            diagnostic_status="inconclusive",
        )
    )

    async def turn(**_kwargs):
        raise RuntimeError("validator correction lead failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-validator-failed"

    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    review = repository.get_coordination_review(record.id)
    assert review is not None
    assert review.candidates == []
    assert review.critic_assessments == []
    assert review.diagnostic_status is None
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v11_insufficient_partial_downgrades_to_inconclusive_via_lead_correction():
    """spec §9.2：证据不足的 partial 经一次 Lead correction 降级为 inconclusive。"""
    repository, record = _seed_repository()
    candidate = RootCauseCandidate(
        id="candidate-insufficient",
        summary="candidate supported by a single evidence item",
        rank=1,
        confidence=0.6,
        supporting_evidence_ids=["ev-metric"],
    )
    assessment = CriticAssessment(
        id="assessment-insufficient",
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.PASS,
                summary="supported",
                evidence_ids=["ev-metric"],
            )
            for name in CausalCheckName
        ],
        summary="accepted",
        runtime_run_id="run-v11",
    )
    repository.save_multi_agent_result(
        record.id,
        [],
        [
            AgentExecution(
                task_id="task-critic",
                agent_name="CriticAgent",
                runtime_run_id="run-v11",
                status=AgentExecutionStatus.COMPLETED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                analysis_round=1,
                summary="completed",
            ),
            AgentExecution(
                task_id="task-lead",
                agent_name="LeadAgent",
                runtime_run_id="run-v11",
                status=AgentExecutionStatus.COMPLETED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                step_kind=ExecutionStepKind.LEAD_ADJUDICATION,
                analysis_round=1,
                summary="completed",
            ),
        ],
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-v11",
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
            lead_decision=LeadDecision(
                action=LeadAction.CONCLUDE,
                summary="conclude the accepted candidate",
                candidate_ids=[candidate.id],
            ),
            diagnostic_status=DiagnosticStatus.PARTIAL,
        ),
    )

    async def turn(**_kwargs):
        return {
            "decision": {
                "action": "inconclusive",
                "summary": "evidence cannot support a root cause",
                "stop_reason": "insufficient_evidence",
            }
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-v11"

    review = await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert review.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
    assert len(review.candidates) == 1
    assert len(review.critic_assessments) == 1
    assert review.lead_decision is not None
    assert review.lead_decision.action == LeadAction.INCONCLUSIVE
    assert review.run_status == MultiAgentRunStatus.COMPLETED
    record_after = repository.get(record.id)
    assert record_after.status != InvestigationStatus.FAILED
    run = record_after.multi_agent_run
    assert run.status == MultiAgentRunStatus.COMPLETED
    assert run.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
    validate_v11_final_status(review, run)


@pytest.mark.anyio
async def test_v11_resume_restores_partial_failure_memory_before_lead_adjudication():
    """跨进程 resume 后 Lead conclude 必须投影 partial 而非 complete。"""
    repository, record = _repository()
    record = repository.save(
        record.model_copy(
            update={
                "evidence": [
                    EvidenceItem(
                        id="ev-log",
                        provider=EvidenceProvider.LOG,
                        kind=EvidenceKind.LOG_PATTERN,
                        timestamp=record.event.started_at,
                        summary="committed log evidence",
                        runtime_run_id="run-v11",
                    ),
                    EvidenceItem(
                        id="ev-metric",
                        provider=EvidenceProvider.METRIC,
                        kind=EvidenceKind.METRIC_TREND,
                        timestamp=record.event.started_at,
                        summary="committed metric evidence",
                        runtime_run_id="run-v11",
                    ),
                ]
            }
        )
    )
    candidate = RootCauseCandidate(
        id="candidate-resumed",
        summary="candidate supported by two provider types",
        rank=1,
        confidence=0.7,
        supporting_evidence_ids=["ev-log", "ev-metric"],
    )
    assessment = CriticAssessment(
        id="assessment-resumed",
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.PASS,
                summary="supported",
                evidence_ids=["ev-log"],
            )
            for name in CausalCheckName
        ],
        summary="accepted",
        runtime_run_id="run-v11",
    )
    repository.save_multi_agent_result(
        record.id,
        [],
        [
            # 崩溃前已持久化的 round-1 Investigator 失败与新进程 Critic 审计。
            AgentExecution(
                task_id="task-failed-investigator",
                agent_name="InvestigatorAgent",
                runtime_run_id="run-v11",
                status=AgentExecutionStatus.FAILED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                analysis_round=1,
                summary="investigator failed",
            ),
            AgentExecution(
                task_id="task-critic",
                agent_name="CriticAgent",
                runtime_run_id="run-v11",
                status=AgentExecutionStatus.COMPLETED,
                execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                step_kind=ExecutionStepKind.CRITIC_REVIEW,
                analysis_round=1,
                summary="completed",
            ),
        ],
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-v11",
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        ),
    )

    async def turn(**_kwargs):
        return {
            "decision": {
                "action": "conclude",
                "summary": "accept the Critic-approved candidate",
                "candidate_ids": [candidate.id],
            }
        }

    # 模拟崩溃后 resume：全新 runtime 的内存失败记录为空，只有持久化执行可查。
    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = "run-v11"

    review = await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert review.diagnostic_status == DiagnosticStatus.PARTIAL
    assert review.run_status == MultiAgentRunStatus.PARTIAL

    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    record_after = repository.get(record.id)
    assert record_after.status != InvestigationStatus.FAILED
    run = record_after.multi_agent_run
    assert run.status == MultiAgentRunStatus.PARTIAL
    assert run.diagnostic_status == DiagnosticStatus.PARTIAL
    validate_v11_final_status(review, run)


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
                        "cause_type": None,
                        "affected_entity": "checkout-service",
                        "failure_mechanism": "bounded failure mechanism",
                        "summary": "bounded candidate",
                        "confidence": 0.8,
                        "supporting_evidence_ids": ["ev-metric"],
                        "contradicting_evidence_ids": [],
                        "rationale": "committed evidence",
                        "uncertainty": "",
                        "onset_window_start": None,
                        "onset_window_end": None,
                    }
                ],
        },
        {
                "summary": "all seven checks are complete",
                "assessments": [
                    {
                        "candidate_ref": "candidate-ref-from-server",
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
        remaining_token_budget=100000,
    )
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    candidate_id = repository.get_coordination_review(record.id).candidates[0].id
    turns[0]["assessments"][0]["candidate_ref"] = candidate_id
    turns[1]["decision"]["candidate_ids"] = [candidate_id]
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
    assert review.lead_decision.candidate_ids == [candidate_id]
    lead_audits = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.LEAD_ADJUDICATION
    ]
    assert json.loads(lead_audits[-1].summary)["authoritative_candidate_ids"] == [
        candidate_id
    ]
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
                        "cause_type": None,
                        "affected_entity": "checkout-service",
                        "failure_mechanism": "bounded failure mechanism",
                        "summary": "candidate awaiting evidence",
                        "confidence": 0.5,
                        "supporting_evidence_ids": ["ev-metric"],
                        "contradicting_evidence_ids": [],
                        "rationale": "committed evidence",
                        "uncertainty": "",
                        "onset_window_start": None,
                        "onset_window_end": None,
                    }
                ],
        },
        {
                "assessments": [
                    {
                        "candidate_ref": "candidate-ref-from-server",
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
                        "candidate_ref": "candidate-ref-from-server",
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
        remaining_token_budget=100000,
    )
    await runtime.investigator_round_1(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    candidate_id = repository.get_coordination_review(record.id).candidates[0].id
    turns[0]["assessments"][0]["candidate_ref"] = candidate_id
    turns[2]["assessments"][0]["candidate_ref"] = candidate_id
    turns[3]["decision"]["candidate_ids"] = [candidate_id]
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
    assert [item.id for item in reconciled.critic_assessments] == [
        first.critic_assessments[0].id
    ]
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
async def test_v11_critic_downgrades_unexecutable_supplemental_work():
    repository, record = _seed_repository()
    runtime_run_id = "run-v11"
    candidate = RootCauseCandidate(
        id="candidate-budget-exhausted",
        summary="candidate needs one more signal",
        rank=1,
        confidence=0.4,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[candidate],
        )
    )
    checks = [
        {
            "name": name.value,
            "status": CausalCheckStatus.UNKNOWN.value,
            "summary": "named gap",
            "gap": "supplemental signal is unavailable",
        }
        for name in CausalCheckName
    ]

    async def turn(**kwargs):
        assert kwargs["remaining_tool_budget"] == 0
        return {
                "summary": "request one supplemental signal",
                "assessments": [
                    {
                        "candidate_ref": candidate.id,
                    "verdict": CriticVerdict.NEEDS_EVIDENCE.value,
                    "checks": checks,
                    "gap": "supplemental signal is unavailable",
                    "supplemental_task_ids": ["task-budget-exhausted"],
                    "summary": "request one supplemental signal",
                }
            ],
            "tasks": [
                {
                    "id": "task-budget-exhausted",
                    "title": "Collect supplemental signal",
                    "description": "The remaining tool budget cannot run this task.",
                    "evidence_scope": {"entity_ids": [record.event.service]},
                }
            ],
        }

    runtime = V11Runtime(
        model="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id
    runtime._phase_tool_budget = 0

    review = await runtime.critic_review(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assessment = review.critic_assessments[0]
    assert assessment.verdict == CriticVerdict.INCONCLUSIVE
    assert assessment.gap is None
    assert assessment.supplemental_task_ids == []
    assert "remaining tool budget" in assessment.summary
    assert repository.list_tasks(record.id) == []
    await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert repository.get(record.id).status != InvestigationStatus.FAILED


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
                        "cause_type": None,
                        "affected_entity": "checkout-service",
                        "failure_mechanism": "bounded failure mechanism",
                        "summary": "bounded candidate",
                        "confidence": 0.8,
                        "supporting_evidence_ids": ["ev-metric"],
                        "contradicting_evidence_ids": [],
                        "rationale": "committed evidence",
                        "uncertainty": "",
                        "onset_window_start": None,
                        "onset_window_end": None,
                    }
                ],
        },
        {
                "assessments": [
                    {
                        "candidate_ref": "candidate-ref-from-server",
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
                    remaining_token_budget=10000,
                ),
                investigation_id=record.id,
                strategy="adaptive",
                run_reason=RuntimeRunReason.INITIAL,
                    execution_contract_version="v11",
                    execution_contract=_complete_contract(
                        runtime.tool_registry, model_name="fake"
                    ),
                    model_provider=ModelProvider.OPENAI,
                    model_name="fake",
                    tool_budget=8,
                token_budget=10000,
                timeout_seconds=60,
            )
        )
        assert output.business_mutation.investigation_id == record.id
        if phase == RuntimePhase.INVESTIGATOR_ROUND_1:
            candidate_id = repository.get_coordination_review(record.id).candidates[0].id
            turns[0]["assessments"][0]["candidate_ref"] = candidate_id
            turns[1]["decision"]["candidate_ids"] = [candidate_id]

    persisted = repository.get_coordination_review(record.id)
    assert persisted is not None
    assert persisted.lead_decision is not None
    assert repository.get(record.id).report is None
    assert repository.get(record.id).actions == []


def test_durable_session_rebuild_preserves_supplemental_tasks():
    class PlanPayloadOnlyRepository(InMemoryInvestigationRepository):
        def get_plan(self, investigation_id):
            plan = super().get_plan(investigation_id)
            if plan is None:
                return None
            return plan.model_copy(
                update={
                    "tasks": [
                        task for task in plan.tasks if task.analysis_round == 1
                    ]
                }
            )

    _, record = _seed_repository()
    repository = PlanPayloadOnlyRepository()
    repository.save(record)
    runtime_run_id = "run-durable-round-two"
    first_task = DiagnosisTask(
        id="task-round-one",
        title="Inspect signal",
        description="Inspect the initial signal.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="investigator",
        analysis_round=1,
        evidence_scope={"entity_ids": ["checkout-service"]},
        runtime_run_id=runtime_run_id,
    )
    supplemental_task = DiagnosisTask(
        id="task-round-two",
        title="Collect supplemental signal",
        description="Collect the requested supplemental signal.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="investigator",
        analysis_round=2,
        information_gap="need a trace boundary",
        runtime_run_id=runtime_run_id,
        critic_assessment_id="assessment-round-one",
    )
    repository.save_plan(
        DiagnosisPlan(
            id="plan-durable-round-two",
            investigation_id=record.id,
            tasks=[first_task],
            runtime_run_id=runtime_run_id,
            lead_decision=LeadDecision(
                action=LeadAction.INVESTIGATE,
                summary="collect initial signal",
                task_ids=[first_task.id],
            ),
        )
    )
    repository.save_tasks(record.id, [first_task, supplemental_task])

    orchestrator = SimpleNamespace(
        repository=repository,
        providers=build_mock_provider_registry(),
        analyzer=None,
        report_generator=None,
        coordinator=None,
        action_planner=None,
        task_planner=None,
        agents_runtime=None,
        v11_runtime=None,
        default_strategy="adaptive",
        max_tool_calls_per_specialist=3,
        max_total_tool_calls=8,
    )
    executor = DiagnosisPhaseExecutor(orchestrator)
    sandbox = executor._build_durable_session(
        PhaseInput(
            run_id=runtime_run_id,
            attempt_id="attempt-durable-round-two",
            phase=RuntimePhase.INVESTIGATOR_ROUND_2,
            resume_state=RuntimeResumeState(
                remaining_tool_budget=8,
                remaining_token_budget=10000,
            ),
            investigation_id=record.id,
            strategy="adaptive",
        )
    )

    assert [
        task.id for task in sandbox._orchestrator.repository.list_tasks(record.id)
    ] == [first_task.id, supplemental_task.id]


def test_v11_official_provider_contract_and_client_ignore_ambient_endpoint(monkeypatch):
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.base_url = kwargs["base_url"]

        async def close(self):
            return None

    class FakeMultiProvider:
        def __init__(self, *, openai_client):
            self.openai_client = openai_client

        def get_model(self, _model_name):
            raise AssertionError("model construction is not part of this contract test")

        async def aclose(self):
            return None

    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    monkeypatch.setattr(v11_runtime_module, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(v11_runtime_module, "MultiProvider", FakeMultiProvider)
    runtime = V11Runtime(model="fake", model_provider=ModelProvider.OPENAI)

    provider = v11_runtime_module._V11ModelProvider(runtime)

    assert captured["base_url"] == OFFICIAL_OPENAI_BASE_URL
    assert provider._client.base_url == OFFICIAL_OPENAI_BASE_URL
    container = AppContainer(AppSettings(storage=StorageSettings(url="memory://")))
    try:
        assert container._v11_model_identity(ModelProvider.OPENAI, "fake")[0] == endpoint_identity(
            OFFICIAL_OPENAI_BASE_URL
        )
    finally:
        container.close()


def test_v11_official_contract_survives_sqlite_reload_with_ambient_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    settings = AppSettings(
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'v11-official.db'}"),
        agents=AgentsSettings(enabled=True, model="gpt-test"),
    )
    first = AppContainer(settings)
    try:
        record = first.repository.save(
            InvestigationRecord(id="inv-official-reload", event=_event())
        )
        run = first.create_runtime_run(
            record.id,
            strategy="adaptive",
            run_reason="initial",
            execution_contract_version="v11",
        )
        expected_endpoint = endpoint_identity(OFFICIAL_OPENAI_BASE_URL)
        assert run.execution_contract["endpoint_id"] == expected_endpoint
        expected_digest = run.execution_contract["execution_contract_digest"]
    finally:
        first.close()

    second = AppContainer(settings)
    try:
        reloaded = second.runtime_store.get_run(run.id)
        assert reloaded.execution_contract["endpoint_id"] == expected_endpoint
        assert reloaded.execution_contract["execution_contract_digest"] == expected_digest
    finally:
        second.close()


@pytest.mark.anyio
async def test_v11_round_two_required_investigator_failure_terminalizes_before_reconciliation():
    repository, record = _repository()
    runtime_run_id = "run-round-two-failure"
    candidate = RootCauseCandidate(
        id="candidate-round-two",
        summary="candidate awaiting supplemental evidence",
        rank=1,
        confidence=0.5,
    )
    assessment = CriticAssessment(
        id="assessment-round-two",
        candidate_id=candidate.id,
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="named gap",
                gap="supplemental signal is unavailable",
            )
            for name in CausalCheckName
        ],
        gap="supplemental signal is unavailable",
        supplemental_task_ids=["task-round-two-failure"],
        summary="request one supplemental task",
        runtime_run_id=runtime_run_id,
    )
    task = DiagnosisTask(
        id="task-round-two-failure",
        title="Collect supplemental signal",
        description="The supplemental provider fails before returning output.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=2,
        evidence_scope={"entity_ids": [record.event.service]},
        runtime_run_id=runtime_run_id,
        critic_assessment_id=assessment.id,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        )
    )
    repository.save_tasks(record.id, [task])

    calls = 0

    async def turn(**_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("round two model transport failed")

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert calls == 1
    assert repository.get(record.id).status == InvestigationStatus.FAILED
    assert repository.get(record.id).multi_agent_run is not None
    assert repository.get(record.id).multi_agent_run.completed_rounds == 0
    assert repository.get_coordination_review(record.id).candidates == []
    before = calls
    await runtime._dispatch_phase(
        phase="critic_reconciliation",
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id=runtime_run_id,
    )
    assert calls == before


@pytest.mark.anyio
async def test_v11_round_two_completed_partial_batch_is_not_blanket_failed():
    repository, record = _repository()
    runtime_run_id = "run-round-two-partial-success"
    task_ids = ["task-round-two-a", "task-round-two-b"]
    assessment = CriticAssessment(
        id="assessment-round-two-partial",
        candidate_id="candidate-round-two-partial",
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="named gap",
                gap="supplemental signal remains bounded",
            )
            for name in CausalCheckName
        ],
        gap="supplemental signal remains bounded",
        supplemental_task_ids=task_ids,
        summary="request two bounded signals",
        runtime_run_id=runtime_run_id,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[
                RootCauseCandidate(
                    id=assessment.candidate_id,
                    summary="candidate",
                    rank=1,
                    confidence=0.4,
                )
            ],
            critic_assessments=[assessment],
        )
    )
    repository.save_tasks(
        record.id,
        [
            DiagnosisTask(
                id=task_id,
                title=f"Collect signal {task_id[-1]}",
                description="A valid supplemental task with no new finding.",
                task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
                agent_name="InvestigatorAgent",
                analysis_round=2,
                evidence_scope={"entity_ids": [record.event.service]},
                runtime_run_id=runtime_run_id,
                critic_assessment_id=assessment.id,
            )
            for task_id in task_ids
        ],
    )

    async def turn(**_kwargs):
        return {"findings": [], "candidates": []}

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert repository.get(record.id).status != InvestigationStatus.FAILED
    assert repository.get(record.id).multi_agent_run.completed_rounds == 2
    assert all(
        item.status == AgentExecutionStatus.COMPLETED
        for item in repository.list_executions(record.id)
    )


@pytest.mark.anyio
async def test_v11_inconclusive_lead_clears_candidates_before_persist_and_reload():
    repository, record = _repository()
    runtime_run_id = "run-inconclusive-clears-candidates"
    candidate = RootCauseCandidate(
        id="candidate-inconclusive",
        summary="candidate without enough evidence",
        rank=1,
        confidence=0.4,
    )
    assessment = CriticAssessment(
        id="assessment-inconclusive",
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="evidence remains insufficient",
                gap="the final decision does not rely on this candidate",
            )
            for name in CausalCheckName
        ],
        summary="candidate was assessed but not concluded",
        runtime_run_id=runtime_run_id,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        )
    )

    async def turn(**_kwargs):
        return {
            "decision": {
                "action": "inconclusive",
                "summary": "the evidence gap remains",
                "stop_reason": "insufficient_evidence",
            }
        }

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
        turn=turn,
    )
    runtime.runtime_run_id = runtime_run_id

    review = await runtime.lead_adjudication(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    reloaded = repository.get_coordination_review(record.id)
    assert review is not None
    assert reloaded is not None
    assert len(review.candidates) == 1
    assert len(reloaded.candidates) == 1
    assert len(reloaded.critic_assessments) == 1
    assert reloaded.root_causes == []
    assert reloaded.lead_decision is not None
    assert reloaded.lead_decision.stop_reason == "insufficient_evidence"
    assert reloaded.diagnostic_status.value == "inconclusive"
    await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    validated = repository.get_coordination_review(record.id)
    assert validated is not None
    assert len(validated.candidates) == 1
    assert validated.diagnostic_status.value == "inconclusive"


@pytest.mark.anyio
async def test_v11_sdk_reservation_retry_reuses_one_durable_allocation_and_settles_once():
    calls = 0
    events: list[tuple[str, dict[str, object]]] = []

    class Delegate(Model):
        async def get_response(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ClassifiedRetryableError(FailureCategory.TRANSPORT)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=3, output_tokens=4)
            )

        async def stream_response(self, **_kwargs):
            if False:
                yield None

        async def close(self):
            return None

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, input_tokens, output_tokens, actor_name
        events.append((status, safe_payload or {}))

    runtime = V11Runtime(model=Delegate(), token_budget=100)
    runtime._persist_model_event = persist_model_event
    settings = ModelSettings(max_tokens=90)
    first = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id="logical-call-retry",
        execution_id="execution-1",
        attempt_number=1,
        actor="LeadAgent",
    )
    with pytest.raises(ClassifiedRetryableError):
        await first.get_response(
            input="one request",
            system_instructions="system",
            model_settings=settings,
        )

    assert runtime.remaining_token_budget == 10
    assert events[-1][0] == "failed"
    assert events[-1][1]["reservation_status"] == "retrying"

    second = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id="logical-call-retry",
        execution_id="execution-2",
        attempt_number=2,
        actor="LeadAgent",
    )
    await second.get_response(
        input="one request",
        system_instructions="system",
        model_settings=settings,
    )

    assert calls == 2
    assert [item[0] for item in events] == [
        "started",
        "failed",
        "started",
        "completed",
    ]
    assert len(
        {
            item[1]["reservation_id"]
            for item in events
            if item[1].get("reservation_id") is not None
        }
    ) == 1
    remaining_after_settle = runtime.remaining_token_budget
    await runtime._settle_model_budget(
        100,
        7,
        reservation_id="logical-call-retry:request-1",
        logical_call_id="logical-call-retry",
        execution_id="execution-2",
        input_tokens=3,
        output_tokens=4,
        reservation_status="completed",
    )
    assert runtime.remaining_token_budget == remaining_after_settle


@pytest.mark.anyio
async def test_v11_sdk_over_budget_response_is_audited_before_contract_failure():
    class OversizedDelegate(Model):
        async def get_response(self, **_kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=80, output_tokens=30)
            )

        async def stream_response(self, **_kwargs):
            if False:
                yield None

        async def close(self):
            return None

    events: list[tuple[str, dict[str, object]]] = []

    async def persist_model_event(
        execution_id,
        status,
        input_tokens=0,
        output_tokens=0,
        actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        del execution_id, actor_name
        events.append(
            (
                status,
                {
                    **(safe_payload or {}),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )
        )

    runtime = V11Runtime(model=OversizedDelegate(), token_budget=100)
    runtime._persist_model_event = persist_model_event
    model = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id="logical-call-over-budget",
        execution_id="execution-over-budget",
        actor="LeadAgent",
    )

    with pytest.raises(V11RuntimeContractError, match="exceeded token budget"):
        await model.get_response(
            input="one request",
            system_instructions="system",
            model_settings=ModelSettings(max_tokens=90),
        )

    assert [status for status, _payload in events] == ["started", "failed"]
    failed = events[-1][1]
    assert failed["reservation_status"] == "rejected"
    assert failed["actual_input_tokens"] == 80
    assert failed["input_tokens"] == 80
    assert failed["output_tokens"] == 30
    assert failed["budget_overrun_tokens"] == 20
    assert runtime._model_reservations == {}
    assert runtime.remaining_token_budget == 0


@pytest.mark.anyio
async def test_v11_sdk_over_budget_rejection_cannot_be_released_by_cancellation():
    class OversizedDelegate(Model):
        async def get_response(self, **_kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=80, output_tokens=30)
            )

        async def stream_response(self, **_kwargs):
            if False:
                yield None

        async def close(self):
            return None

    events: list[str] = []
    model_task: asyncio.Task[object] | None = None

    async def persist_model_event(
        _execution_id,
        status,
        _input_tokens=0,
        _output_tokens=0,
        _actor_name="CoordinatorAgent",
        safe_payload=None,
    ):
        events.append((safe_payload or {}).get("reservation_status", status))
        if (safe_payload or {}).get("reservation_status") == "rejected":
            assert model_task is not None
            model_task.cancel()
            await asyncio.sleep(0)

    runtime = V11Runtime(model=OversizedDelegate(), token_budget=100)
    runtime._persist_model_event = persist_model_event
    model = _V11BudgetedModel(
        runtime.model,
        runtime,
        logical_call_id="logical-call-cancelled-over-budget",
        execution_id="execution-cancelled-over-budget",
        actor="LeadAgent",
    )
    model_task = asyncio.create_task(
        model.get_response(
            input="one request",
            system_instructions="system",
            model_settings=ModelSettings(max_tokens=90),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await model_task

    assert events == ["reserved", "rejected"]
    assert runtime._model_reservations == {}
    assert runtime.remaining_token_budget == 0


@pytest.mark.anyio
async def test_v11_concurrent_model_reservations_cannot_oversell_token_ceiling():
    runtime = V11Runtime(model="fake", token_budget=100)

    async def reserve(index: int):
        return await runtime._reserve_model_budget(
            60,
            f"prompt-{index}",
            {"index": index},
            reservation_id=f"logical-{index}:request-1",
            logical_call_id=f"logical-{index}",
            execution_id=f"execution-{index}",
            actor="LeadAgent",
        )

    results = await asyncio.gather(*(reserve(index) for index in range(2)))

    assert sum(item[2] for item in results) == 100
    assert runtime.remaining_token_budget == 0
    for index, (_, input_estimate, reserved_total) in enumerate(results):
        await runtime._settle_model_budget(
            reserved_total,
            input_estimate,
            reservation_id=f"logical-{index}:request-1",
            logical_call_id=f"logical-{index}",
            execution_id=f"execution-{index}",
            input_tokens=input_estimate,
            reservation_status="completed",
        )
    assert runtime.remaining_token_budget == 100 - sum(
        item[1] for item in results
    )


@pytest.mark.anyio
async def test_v11_default_model_reservations_share_concurrent_budget():
    runtime = V11Runtime(
        model="fake",
        token_budget=100_000,
        max_investigators=3,
    )

    async def reserve(index: int):
        return await runtime._reserve_model_budget(
            None,
            f"prompt-{index}",
            {"index": index},
            reservation_id=f"logical-default-{index}:request-1",
            logical_call_id=f"logical-default-{index}",
            execution_id=f"execution-default-{index}",
            actor=ExecutionActor.INVESTIGATOR.value,
        )

    results = await asyncio.gather(*(reserve(index) for index in range(3)))

    assert sum(item[2] for item in results) == 100_000
    assert all(reserved > input_estimate for _, input_estimate, reserved in results)
    assert runtime.remaining_token_budget == 0
    for index, (_, input_estimate, reserved_total) in enumerate(results):
        await runtime._settle_model_budget(
            reserved_total,
            input_estimate,
            reservation_id=f"logical-default-{index}:request-1",
            logical_call_id=f"logical-default-{index}",
            execution_id=f"execution-default-{index}",
            input_tokens=input_estimate,
            reservation_status="completed",
        )
    assert runtime.remaining_token_budget == 100_000 - sum(
        item[1] for item in results
    )


@pytest.mark.anyio
async def test_v11_serial_default_model_reservation_keeps_the_full_budget():
    runtime = V11Runtime(
        model="fake",
        token_budget=100_000,
        max_investigators=3,
    )

    _, input_estimate, reserved_total = await runtime._reserve_model_budget(
        None,
        "critic prompt",
        {"review": "bounded"},
        reservation_id="logical-critic:request-1",
        logical_call_id="logical-critic",
        execution_id="execution-critic",
        actor=ExecutionActor.CRITIC.value,
    )

    assert reserved_total == 100_000
    assert reserved_total > input_estimate
    await runtime._settle_model_budget(
        reserved_total,
        input_estimate,
        reservation_id="logical-critic:request-1",
        logical_call_id="logical-critic",
        execution_id="execution-critic",
        input_tokens=input_estimate,
        reservation_status="completed",
    )
    assert runtime.remaining_token_budget == 100_000 - input_estimate


def test_domain_violating_draft_raises_contract_error_not_validation_error():
    """模型产出违反领域规则的 draft（如 blocking 的非 GAP/无 gaps），
    构造 AgentFinding 的 pydantic ValidationError 必须包装为
    V11RuntimeContractError 走 per-draft 拒绝，而不是漏网杀 run。"""
    runtime, task, evidence = _finding_gate_harness()
    draft = InvestigatorFindingDraft(
        finding_type=AgentFindingType.GAP,
        summary="blocking gap without gaps list",
        confidence=0.5,
        blocking=True,
    )

    with pytest.raises(V11RuntimeContractError, match="finding contract") as captured:
        runtime._finding_from_draft(
            draft,
            investigation_id="inv-1",
            task=task,
            instance_id="investigator-1",
            round_number=1,
            assessment=None,
            evidence=evidence,
        )

    # validator 消息是固定字符串（非模型文本），必须保留在契约错误里，
    # 否则未来调用方 bug 与模型违约在审计上不可区分。
    assert "blocking requires a gap finding" in str(captured.value)
