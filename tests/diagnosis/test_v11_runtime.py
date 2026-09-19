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
    CriticCompactOutput,
    CriticOutput,
    InvestigatorCandidateOutput,
    InvestigatorFindingDraft,
    InvestigatorOutput,
    LeadAdjudicationOutput,
    LeadPlanningCompactOutput,
    LeadPlanningOutput,
    V11ResultValidationError,
    V11Runtime,
    V11RuntimeContractError,
    V11SingleControlOutput,
    _evidence_projection,
    _resolved_execution_ids,
    _select_evidence_digest,
    _V11BudgetedModel,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    CausalCheck,
    CausalCheckName,
    CoordinationReview,
    CriticAssessment,
    FinalDiagnosisDecision,
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
from backend.providers.registry import ProviderRegistry, build_mock_provider_registry
from backend.providers.results import ProviderResult
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
from tests.model_outputs import critic_response

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
        LeadPlanningCompactOutput,
        LeadPlanningOutput,
        CriticCompactOutput,
        InvestigatorCandidateOutput,
        InvestigatorOutput,
        CriticOutput,
        LeadAdjudicationOutput,
        V11SingleControlOutput,
    ],
)
def test_v11_model_output_types_are_valid_strict_json_schemas(output_type):
    schema = AgentOutputSchema(output_type, strict_json_schema=True).json_schema()
    _assert_explicit_schema_semantics(schema)


def test_live_lead_planning_schema_contains_no_server_owned_entity_fields():
    schema = AgentOutputSchema(LeadPlanningCompactOutput, strict_json_schema=True).json_schema()
    decision = schema["$defs"]["LeadPlanningDecisionDraft"]
    task = schema["$defs"]["LeadPlanningCompactTaskDraft"]

    assert set(decision["properties"]) == {
        "action",
        "summary",
        "stop_reason",
        "selected_skills",
    }
    assert set(task["properties"]) == {
        "title",
        "description",
        "information_gap",
        "expected_discriminator",
        "evidence_scope",
    }
    assert not {
        "candidate_ids",
        "evidence_ids",
        "runtime_run_id",
        "review_round",
        "authoritative",
    } & set(json.dumps(schema))


def test_resolved_model_retry_is_not_a_persisted_failure():
    failed = AgentExecution(
        id="exec-lead-retry-1",
        task_id="lead-planning-run-retry",
        agent_name="LeadAgent",
        runtime_run_id="run-retry",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
        attempt=1,
        failure_category=FailureCategory.INVALID_OUTPUT,
    )
    completed = failed.model_copy(
        update={
            "id": "exec-lead-retry-3",
            "status": AgentExecutionStatus.COMPLETED,
            "attempt": 3,
            "failure_category": FailureCategory.NONE,
        }
    )
    unresolved = failed.model_copy(
        update={
            "id": "exec-critic-unresolved",
            "task_id": "critic-review-run-retry",
            "agent_name": "CriticAgent",
            "step_kind": ExecutionStepKind.CRITIC_REVIEW,
        }
    )

    assert _resolved_execution_ids([failed, completed, unresolved]) == {failed.id}


def test_investigator_candidate_schema_requires_publishable_fields():
    schema = AgentOutputSchema(InvestigatorOutput, strict_json_schema=True).json_schema()
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

    critic_schema = AgentOutputSchema(CriticOutput, strict_json_schema=True).json_schema()["$defs"][
        "CriticAssessmentDraft"
    ]
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
    schema = AgentOutputSchema(LeadPlanningOutput, strict_json_schema=True).json_schema()
    selected_skills = schema["$defs"]["LeadDecision"]["properties"]["selected_skills"]["items"]

    assert selected_skills["pattern"] == (r"^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9_.-]{1,32}$")


def test_planning_task_schema_requires_first_analysis_round():
    schema = AgentOutputSchema(LeadPlanningOutput, strict_json_schema=True).json_schema()
    task_schema = schema["$defs"]["LeadPlanningTaskDraft"]
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


def test_single_control_schema_contains_only_diagnostic_candidate_fields():
    schema = AgentOutputSchema(V11SingleControlOutput, strict_json_schema=True).json_schema()
    assert set(schema["properties"]) == {
        "candidates",
        "action",
        "summary",
        "evidence_ids",
        "stop_reason",
    }
    candidate = schema["$defs"]["InvestigatorCandidateDraft"]
    assert set(candidate["properties"]) == {
        "affected_entity",
        "failure_class",
        "failure_mechanism",
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
    }
    assert not {
        "id",
        "rank",
        "runtime_run_id",
        "review_round",
        "authoritative",
    } & set(candidate["properties"])


def test_structured_generation_compares_evidence_before_selection():
    tool = v11_runtime_module._strict_output_tool(CriticCompactOutput)
    schema = json.loads(tool.description.split("match this schema: ", 1)[1])
    assert list(schema["properties"])[-1] == "final_decision"
    fields = list(schema["$defs"]["CriticCompactAssessmentDraft"]["properties"])
    assert fields.index("summary") < fields.index("checks")
    assert fields.index("checks") < fields.index("verdict")
    fields = list(schema["$defs"]["CriticCompactCausalCheck"]["properties"])
    assert fields.index("summary") < fields.index("status")
    tool = v11_runtime_module._strict_output_tool(InvestigatorCandidateOutput)
    candidate = json.loads(tool.description.split("match this schema: ", 1)[1])
    fields = list(candidate["$defs"]["InvestigatorCandidateDraft"]["properties"])
    assert fields.index("failure_mechanism") < fields.index("failure_class")


def test_critic_comparison_is_persisted_and_its_citations_are_validated():
    from backend.diagnosis.v11_runtime import CriticCompactAssessmentDraft

    draft = CriticCompactAssessmentDraft(
        candidate_ref="candidate-io", verdict="inconclusive",
        summary="Filesystem work can cause the observed call wait; they are compatible.",
        supporting_evidence_ids=["ev-writes"],
        checks=[{"name": name.value, "status": "unknown", "gap": "Need resource controls"}
                for name in CausalCheckName],
    )
    runtime = V11Runtime(model="fake")
    arguments = {"candidate_ids": {"candidate-io"}, "runtime_run_id": "run-1",
                 "review_round": 1, "usable_evidence_ids": {"ev-writes"}}
    assessment = runtime._normalize_assessments([draft], **arguments)[0]
    saved = v11_runtime_module.CriticAssessment.model_validate_json(assessment.model_dump_json())
    assert saved.summary == draft.summary
    assert saved.supporting_evidence_ids == ["ev-writes"]
    with pytest.raises(V11RuntimeContractError, match="evidence reference"):
        runtime._normalize_assessments(
            [draft.model_copy(update={"supporting_evidence_ids": ["missing"]})], **arguments,
        )


@pytest.mark.anyio
async def test_rejected_tool_draft_is_repaired_without_bypassing_reference_validation(monkeypatch):
    class BoundedOutput(BaseModel):
        summary: str = Field(max_length=40)
        evidence_ids: list[str]

    calls = []

    async def no_sleep(_seconds):
        pass

    async def fake_run(agent, context, **_kwargs):
        context = json.loads(context)
        calls.append(context)
        if len(calls) == 1:
            draft = {"summary": "Observed filesystem writes correlate with kernel work. " * 3,
                     "evidence_ids": ["ev-observed"]}
        elif len(calls) == 2:
            previous = context["previous_structured_output"]
            assert previous["evidence_ids"] == ["ev-observed"]
            assert len(previous["summary"]) > 40
            assert "untrusted rejected draft" in agent.instructions
            draft = {"summary": "Filesystem contention", "evidence_ids": ["ev-invented"]}
        else:
            assert "candidate_evidence_reference" in agent.instructions
            draft = {"summary": "Filesystem contention", "evidence_ids": ["ev-observed"]}
        result = await agent.tools[0].on_invoke_tool(
            None, json.dumps({"payload_json": json.dumps(draft)}),
        )
        return SimpleNamespace(final_output=result, raw_responses=[])

    def validate(output):
        if output.evidence_ids != ["ev-observed"]:
            raise v11_runtime_module.ClassifiedRetryableError(
                FailureCategory.INVALID_OUTPUT, audit_code="candidate_evidence_reference",
            )

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(v11_runtime_module, "_run_with_model_lifecycle", fake_run)
    runtime = V11Runtime(model=OpenAICompatibleChatCompletionsModel(
        model="compat-model", api_key="local-test-key", base_url="http://127.0.0.1:8000/v1",
        structured_output_transport="strict_output_tool",
    ))
    result = await runtime._call_model(
        actor=ExecutionActor.INVESTIGATOR.value, prompt="Review observations",
        output_type=BoundedOutput, context={}, tools=[], remaining_token_budget=None,
        output_validator=validate,
    )
    assert len(calls) == 3
    assert result.output.evidence_ids == ["ev-observed"]
    assert len(result.output.summary) <= 40


def test_repair_draft_is_bounded_and_redacted():
    bounded = v11_runtime_module._bounded_repair_output
    assert bounded({"summary": "x" * 16000}) is None
    assert bounded(["not an object"]) is None
    assert bounded({"api_key": "sk-private-value", "evidence_ids": ["ev-observed"]}) == {
        "api_key": "[REDACTED]", "evidence_ids": ["ev-observed"],
    }


@pytest.mark.anyio
async def test_critic_short_references_are_restored_before_validation(monkeypatch):
    class CitationOutput(BaseModel):
        evidence_ids: list[str]
        summary: str

    evidence_id = "ev-0123456789abcdef0123456789abcdef"
    seen = []

    async def fake_run(agent, context, **_kwargs):
        assert json.loads(agent.instructions)["evidence"][0]["id"] == "ref:1"
        assert json.loads(context)["allowed_evidence_ids"] == ["ref:1"]
        return SimpleNamespace(
            final_output=CitationOutput(evidence_ids=["ref:1"], summary="Prefer the cited cause"),
            raw_responses=[],
        )

    monkeypatch.setattr(v11_runtime_module, "_run_with_model_lifecycle", fake_run)
    runtime = V11Runtime(model=OpenAICompatibleChatCompletionsModel(
        model="compat-model", api_key="local-test-key", base_url="http://127.0.0.1:8000/v1",
        structured_output_transport="strict_output_tool",
    ))
    result = await runtime._call_model(
        actor=ExecutionActor.CRITIC.value,
        prompt=json.dumps({"evidence": [{"id": evidence_id}]}),
        output_type=CitationOutput, tools=[],
        context={"allowed_evidence_ids": [evidence_id]},
        remaining_token_budget=None,
        output_validator=lambda output: seen.append(output.evidence_ids),
    )
    assert seen == [[evidence_id]]
    assert result.output["evidence_ids"] == [evidence_id]
    restore = v11_runtime_module._replace_reference_values
    assert restore({"evidence_ids": ["ref:99"], "summary": "ref:1 is discussed"},
                   {"ref:1": evidence_id}) == {
        "evidence_ids": ["ref:99"], "summary": "ref:1 is discussed",
    }


def test_structured_outputs_reject_control_text_at_model_boundary():
    with pytest.raises(ValidationError, match="unsafe text"):
        V11SingleControlOutput.model_validate(
            {
                "action": "conclude",
                "summary": "test",
                "candidates": [
                    {
                        "affected_entity": "checkout-service",
                        "failure_class": "cpu saturation",
                        "failure_mechanism": "line one\nline two",
                        "supporting_evidence_ids": ["ev-metric"],
                        "contradicting_evidence_ids": [],
                    }
                ],
            }
        )


def test_lead_prompt_exposes_exact_skill_identifiers():
    prompt = json.loads(
        V11Runtime(model=None, turn=object())._lead_prompt(_event(), ("read_logs",), 8)
    )

    assert [item["identifier"] for item in prompt["skills"]] == [
        f"{skill.name}@{skill.version}" for skill in DIAGNOSTIC_SKILLS
    ]
    assert "name@version" in prompt["rule"]


def test_live_lead_prompt_omits_redundant_static_catalogs():
    prompt = json.loads(V11Runtime(model=None)._lead_prompt(_event(), ("read_logs",), 8))

    assert set(prompt) == {
        "incident",
        "skills",
        "rule",
        "evidence",
        "provider_statuses",
        "evidence_coverage",
        "evidence_interpretation_rules",
        "metric_comparisons",
    }
    assert set(prompt["skills"]) == {f"{skill.name}@{skill.version}" for skill in DIAGNOSTIC_SKILLS}
    assert "tool_manifest" not in prompt


@pytest.mark.anyio
async def test_lead_planning_receives_committed_digest_and_source_status(monkeypatch):
    repository, record = _seed_repository()
    record = repository.save(
        record.model_copy(
            update={
                "evidence": [
                    *record.evidence,
                    record.evidence[0].model_copy(
                        update={
                            "id": "ev-failed",
                            "status": EvidenceStatus.FAILED,
                        }
                    ),
                ],
                "provider_results": [
                    ProviderResult(
                        provider=EvidenceProvider.LOG,
                        status="failed",
                        error_message="private provider body",
                    )
                ],
            }
        )
    )
    runtime = V11Runtime(
        model=None,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
    )

    async def model_call(**kwargs):
        prompt = json.loads(kwargs["prompt"])
        assert [item["id"] for item in prompt["evidence"]] == ["ev-metric"]
        assert prompt["provider_statuses"] == [{"provider": "log", "status": "failed"}]
        assert "private provider body" not in kwargs["prompt"]
        return SimpleNamespace(
            output=LeadPlanningCompactOutput.model_validate(
                {
                    "decision": {"action": "investigate", "summary": "Check missing logs"},
                    "tasks": [
                        {
                            "title": "Check log availability",
                            "description": "Find missing log evidence",
                            "information_gap": "log source unavailable",
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(runtime, "_call_model", model_call)
    plan = await runtime.plan_lead(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
        runtime_run_id="run-v11",
        remaining_tool_budget=8,
        remaining_token_budget=12000,
    )
    assert len(plan.tasks) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("unexpected_candidate", [False, True])
async def test_live_supplemental_tool_evidence_reaches_critic_as_owned_findings(
    monkeypatch,
    unexpected_candidate,
):
    repository, record = _seed_repository()
    candidate = RootCauseCandidate(
        id="candidate-supplemental",
        summary="initial candidate",
        rank=1,
        confidence=0.5,
        supporting_evidence_ids=["ev-metric"],
    )
    assessment = CriticAssessment(
        candidate_id=candidate.id,
        verdict=CriticVerdict.NEEDS_EVIDENCE,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="missing log signal",
                gap="missing log signal",
            )
            for name in CausalCheckName
        ],
        summary="Need independent logs",
        gap="missing log signal",
        supplemental_task_ids=["task-supplemental"],
        runtime_run_id="run-v11",
    )
    task = DiagnosisTask(
        id="task-supplemental",
        title="Collect logs",
        description="Resolve the log gap",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="investigator",
        analysis_round=2,
        information_gap="missing log signal",
        runtime_run_id="run-v11",
        critic_assessment_id=assessment.id,
    )
    repository.save_tasks(record.id, [task])
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-v11",
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
        )
    )

    class Logs:
        provider = EvidenceProvider.LOG
        supported_tools = ("read_logs",)

        def collect(self, event, query=None):
            return ProviderResult(
                provider=self.provider,
                evidence_items=[
                    EvidenceItem(
                        id="ev-new-log",
                        provider=self.provider,
                        kind=EvidenceKind.LOG_PATTERN,
                        timestamp=event.started_at,
                        summary="independent log signal",
                    )
                ],
            )

    evidence_ids = []

    async def model_run(agent, *_args, **_kwargs):
        prompt = json.loads(agent.instructions)
        if agent.name == ExecutionActor.INVESTIGATOR.value:
            assert agent.output_type is InvestigatorOutput
            assert prompt["critic_assessment"]["id"] == assessment.id
            assert "Return findings" in prompt["rule"]
            tool = next(tool for tool in agent.tools if tool.name == "read_logs")
            response = json.loads(await tool.on_invoke_tool(None, "{}"))
            assert response["status"] == "success"
            evidence_ids.extend(item["id"] for item in response["evidence"])
            output = InvestigatorOutput(
                findings=[
                    InvestigatorFindingDraft(
                        finding_type=AgentFindingType.SIGNAL,
                        summary="supplemental log finding",
                        confidence=0.7,
                        evidence_ids=evidence_ids,
                    )
                ],
                candidates=[
                    {
                        "affected_entity": record.event.service,
                        "failure_class": "log_error",
                        "failure_mechanism": "log error",
                        "supporting_evidence_ids": evidence_ids,
                    }
                ]
                if unexpected_candidate
                else [],
            )
        else:
            assert agent.name == ExecutionActor.CRITIC.value
            model_evidence_ids = prompt["findings"][0]["evidence_ids"]
            assert set(model_evidence_ids) <= set(prompt["allowed_evidence_ids"])
            assert prompt["findings"][0]["critic_assessment_id"] == assessment.id
            assert prompt["prior_assessments"][0]["candidate_ref"] == candidate.id
            assert prompt["supplemental_task_capacity"] == 0
            assert agent.output_type is CriticCompactOutput
            output = CriticCompactOutput.model_validate(
                {
                    "final_decision": {
                        "action": "conclude",
                        "candidate_refs": [candidate.id],
                        "evidence_ids": model_evidence_ids,
                        "summary": "supplemental evidence supports candidate",
                        "stop_reason": None,
                    },
                    "assessments": [
                        {
                            "candidate_ref": candidate.id,
                            "verdict": "accept",
                            "checks": [
                                {
                                    "name": name.value,
                                    "status": "pass",
                                    "summary": "supported",
                                    "evidence_ids": model_evidence_ids,
                                }
                                for name in CausalCheckName
                            ],
                        }
                    ],
                }
            )
        return SimpleNamespace(final_output=critic_response(output), raw_responses=[])

    monkeypatch.setattr(v11_runtime_module, "_run_with_model_lifecycle", model_run)
    runtime = V11Runtime(
        model=OpenAICompatibleChatCompletionsModel(
            model="offline-test",
            api_key="offline-placeholder",
            base_url="http://127.0.0.1:1",
        ),
        tool_registry=build_provider_tool_registry(ProviderRegistry([Logs()])),
    )
    runtime.runtime_run_id = "run-v11"
    findings = await runtime.investigator_round_2(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    if unexpected_candidate:
        assert findings == ()
        assert repository.get(record.id).status == InvestigationStatus.FAILED
        assert set(evidence_ids) <= {item.id for item in repository.get(record.id).evidence}
        return
    assert len(findings) == 1
    assert findings[0].runtime_run_id == "run-v11"
    assert findings[0].task_id == task.id
    assert findings[0].critic_assessment_id == assessment.id
    assert set(evidence_ids) <= {item.id for item in repository.get(record.id).evidence}
    reconciled = await runtime.critic_reconciliation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )
    assert reconciled.critic_assessments[0].id == assessment.id
    assert reconciled.critic_assessments[0].verdict == CriticVerdict.ACCEPT
    assert all(
        check.evidence_ids == evidence_ids for check in reconciled.critic_assessments[0].checks
    )
    assert reconciled.final_decision.evidence_ids == evidence_ids


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
    assert "specific mechanism" in prompt["rule"]


def test_live_investigator_prompt_compacts_complete_evidence_context():
    registry = build_provider_tool_registry(build_mock_provider_registry())
    runtime = V11Runtime(model=None, tool_registry=registry)
    task = DiagnosisTask(
        id="task-compact-context",
        title="Use committed evidence",
        description="Diagnose the bounded signal.",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent",
        analysis_round=1,
        runtime_run_id="run-compact-context",
        information_gap="affected service",
    )
    evidence = [
        EvidenceItem(
            id="ev-compact",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            summary="bounded signal",
            payload={"signal_type": "network_latency"},
            scope=EvidenceScope(entity_ids=["service-a"]),
        )
    ]

    prompt = json.loads(
        runtime._investigator_prompt(
            _event(),
            task,
            "investigator-compact",
            registry.agent_manifest(),
            evidence,
            [],
            None,
            [],
        )
    )

    assert "tool_manifest" not in prompt
    assert "tool_contracts" not in prompt
    assert "skills" not in prompt
    assert "Return zero or one candidate" in prompt["rule"]
    assert "not exhaustive" in prompt["rule"]
    assert "at least two distinct usable supporting evidence IDs" in prompt["rule"]
    assert "never cite unrelated evidence just to reach two" in prompt["rule"]
    assert set(prompt["evidence"][0]) == {
        "id",
        "provider",
        "kind",
        "status",
        "observed_at",
        "summary",
        "scope_entity_ids",
        "signal_family",
    }
    assert prompt["evidence"][0]["signal_family"] == "network_latency"
    assert "copy that exact value into failure_class" not in prompt["rule"]
    assert "not causes or causal rankings" in prompt["evidence_interpretation_rules"]
    assert "No resource or network hypothesis has automatic priority" in (
        prompt["evidence_interpretation_rules"]
    )
    assert "alone does not show memory pressure" in prompt["evidence_interpretation_rules"]
    assert "not an anomaly timestamp" in prompt["evidence_interpretation_rules"]
    assert "User CPU growth favors computation" in prompt["evidence_interpretation_rules"]
    assert "whole-window mean mixes baseline and incident" in (
        prompt["evidence_interpretation_rules"]
    )
    assert "id" not in prompt["task"]
    assert "runtime_run_id" not in prompt["task"]


def test_v11_evidence_digest_is_bounded_and_excludes_unusable_items():
    evidence = []
    for index in range(6):
        for provider, kind in (
            (EvidenceProvider.LOG, EvidenceKind.LOG_PATTERN),
            (EvidenceProvider.METRIC, EvidenceKind.METRIC_TREND),
            (EvidenceProvider.RUNTIME_STATE, EvidenceKind.RUNTIME_STATE),
        ):
            evidence.append(
                EvidenceItem(
                    id=f"ev-{provider.value}-{index}",
                    provider=provider,
                    kind=kind,
                    timestamp=datetime(2026, 1, 1, index, tzinfo=UTC),
                    summary=f"{provider.value} signal {index}",
                    payload={"change_score": float(index)},
                )
            )
    evidence.append(
        EvidenceItem(
            id="ev-skipped",
            provider=EvidenceProvider.RELATED_ALERT,
            kind=EvidenceKind.PROVIDER_ERROR,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            summary="provider skipped",
            status=EvidenceStatus.SKIPPED,
        )
    )

    selected = _select_evidence_digest(evidence)

    assert len(selected) == 12
    assert len({item.id for item in selected}) == len(selected)
    assert all(item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL} for item in selected)
    assert {item.kind for item in selected} == {
        EvidenceKind.LOG_PATTERN,
        EvidenceKind.METRIC_TREND,
        EvidenceKind.RUNTIME_STATE,
    }


def test_v11_metric_digest_keeps_signal_families_within_an_entity():
    evidence = []
    for entity, score in (("decoy", 100.0), ("root", 80.0)):
        for family, offset in (("cpu", 0), ("latency", 1), ("socket", 2)):
            evidence.append(
                EvidenceItem(
                    id=f"ev-{entity}-{family}",
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=datetime(2026, 1, 1, offset, tzinfo=UTC),
                    summary=f"{entity} {family} anomaly",
                    payload={
                        "entity": entity,
                        "signal_type": family,
                        "change_score": score - offset,
                    },
                    scope=EvidenceScope(entity_ids=[entity]),
                )
            )

    selected = _select_evidence_digest(evidence, max_total=8)

    selected_ids = {item.id for item in selected}
    assert len(selected_ids) == 4  # 默认每类最多四条，调用方仍可进一步收紧。
    assert len(_select_evidence_digest(evidence, max_per_kind=2, max_total=6)) == 2


def test_metric_digest_preserves_small_resource_signals_beside_large_symptoms():
    evidence = [EvidenceItem(
        id=f"ev-{family}", provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND, timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        summary=f"checkout {family} change", scope=EvidenceScope(entity_ids=["checkout"]),
        payload={"entity": "checkout", "signal_type": family, "change_score": score},
    ) for family, score in [("latency", 1000), ("error", 1e9), ("cpu", 0.2), ("memory", 0.1)]]

    selected = _select_evidence_digest(evidence, max_per_kind=4, max_total=6)
    assert {item.payload["signal_type"] for item in selected[:2]} == {"memory", "cpu"}
    assert {item.id for item in selected} == {item.id for item in evidence}
    assert _select_evidence_digest(list(reversed(evidence)), max_total=6) == selected


@pytest.mark.parametrize("family", ["cpu", "disk_io", "latency"])
def test_metric_digest_never_prefers_flat_resources_to_an_observed_change(family):
    evidence = [EvidenceItem(
        id=f"ev-{signal}", provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND, timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        summary=f"checkout {signal} observation", scope=EvidenceScope(entity_ids=["checkout"]),
        payload={"entity": "checkout", "signal_type": signal, "change_score": score},
    ) for signal, score in [("socket", 0), ("memory", 0), (family, 5)]]

    selected = _select_evidence_digest(evidence, max_per_kind=1, max_total=2)
    assert [item.id for item in selected] == [f"ev-{family}"]


def test_evidence_projection_exposes_server_owned_scope_entities():
    item = EvidenceItem(
        id="ev-scoped",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        summary="bounded signal",
        scope=EvidenceScope(entity_ids=["service-b", "service-a"]),
    )

    assert _evidence_projection(item)["scope_entity_ids"] == [
        "service-a",
        "service-b",
    ]


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
            AgentOutputSchema(Output).validate_json(
                '{"value": {"untrusted": "provider validation details"}}'
            )
        return critic_response({"value": "ok"})

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", fake_sleep)
    runtime = V11Runtime(model="fake", turn=turn, token_budget=100_000)

    result = await runtime._call_model(
        actor="CriticAgent",
        prompt="review the committed candidates",
        output_type=Output,
        context={"request": "bounded"},
        tools=[],
        remaining_token_budget=100_000,
        remaining_tool_budget=0,
    )

    assert result.output == {"value": "ok"}
    assert prompts[0] == "review the committed candidates"
    assert "previous structured response was rejected" in prompts[1]
    assert "untrusted provider validation details" not in prompts[1]
    assert "provider validation details" not in prompts[1]
    assert "value:string_type" in prompts[1]
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
        return critic_response({"value": ""} if calls == 1 else {"value": "recovered"})

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

    assert event.safe_payload["input_estimate_audit"]["method"] == ("unicode-json-envelope-v2")


@pytest.mark.anyio
@pytest.mark.parametrize("usage_ratio", [0.5, 2])
async def test_v11_input_estimator_calibrates_after_provider_settlement(usage_ratio):
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
    raw_estimate = runtime._model_reservations["calibration-request-1"].estimate_audit[
        "raw_estimated_tokens"
    ]
    actual_input = max(1, int(raw_estimate * usage_ratio))
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

    assert (second_estimate < first_estimate) == (usage_ratio < 1)
    audit = runtime._model_reservations["calibration-request-2"].estimate_audit
    assert audit["calibration_samples"] == 1
    assert audit["calibration_factor_basis_points"] >= 5_000
    assert (audit["calibration_factor_basis_points"] < 10_000) == (usage_ratio < 1)


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

    assert [item.id for item in selected] == ["ev-match", "ev-unknown"]


def test_critic_evidence_keeps_references_and_bounded_independent_signals():
    review = CoordinationReview(
        investigation_id="inv-critic-evidence",
        candidates=[
            RootCauseCandidate(
                id="candidate-critic-evidence",
                summary="candidate",
                rank=1,
                confidence=0.8,
                supporting_evidence_ids=["ev-referenced", "ev-extra-9"],
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

    evidence.extend(
        evidence[1].model_copy(update={"id": f"ev-extra-{index}"}) for index in range(10)
    )
    evidence = [item.model_copy(update={"runtime_run_id": "run-critic"}) for item in evidence]
    evidence.extend(
        [
            evidence[0].model_copy(update={"id": "ev-foreign", "runtime_run_id": "other-run"}),
            evidence[0].model_copy(update={"id": "ev-failed", "status": EvidenceStatus.FAILED}),
        ]
    )
    selected = v11_runtime_module._usable_critic_evidence(
        review,
        (),
        evidence,
        runtime_run_id="run-critic",
    )

    assert selected[0].id == "ev-referenced"
    selected_ids = {item.id for item in selected}
    assert {"ev-extra-9", "ev-unreferenced"} <= selected_ids
    assert not {"ev-foreign", "ev-failed"} & selected_ids
    assert 2 < len(selected) <= 6


def test_single_control_rejects_server_owned_planning_fields():
    with pytest.raises(ValidationError):
        V11SingleControlOutput.model_validate(
            {
                "planning": {"action": "inconclusive"},
                "candidates": [],
            }
        )


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
            "final_decision": None,
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
    evidence[1] = evidence[1].model_copy(update={"scope": EvidenceScope(entity_ids=["service-a"])})
    draft = InvestigatorFindingDraft(
        finding_type=AgentFindingType.SIGNAL,
        summary="The scoped signal proves the failure.",
        confidence=0.9,
        evidence_ids=["ev-success"],
        affected_entity="service-b",
    )

    with pytest.raises(V11RuntimeContractError, match="scope_entity_mismatch"):
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
@pytest.mark.parametrize("round_number", [1, 2])
@pytest.mark.parametrize("failure", ["quota", "validation", "unknown"])
async def test_investigator_outer_failure_keeps_safe_reason_and_category(
    monkeypatch, round_number, failure,
):
    repository, record = _investigator_round_harness("run-outer-failure")
    if round_number == 2:
        task = repository.list_tasks(record.id)[0].model_copy(update={
            "analysis_round": 2, "critic_assessment_id": "assessment-outer",
        })
        repository.save_tasks(record.id, [task])
        repository.save_coordination_review(CoordinationReview(
            investigation_id=record.id, runtime_run_id="run-outer-failure",
            critic_assessments=[CriticAssessment(
                id="assessment-outer", candidate_id="candidate-outer",
                verdict=CriticVerdict.NEEDS_EVIDENCE, summary="missing causal evidence",
                gap="missing causal evidence", supplemental_task_ids=[task.id],
                runtime_run_id="run-outer-failure",
                checks=[CausalCheck(
                    name=name, status=CausalCheckStatus.UNKNOWN,
                    summary="missing causal evidence", gap="missing causal evidence",
                ) for name in CausalCheckName],
            )],
        ))
    runtime = V11Runtime(model="fake")
    runtime.runtime_run_id = "run-outer-failure"

    async def fail_before_session(**_kwargs):
        if failure == "quota":
            raise V11RuntimeContractError("context_budget_exhausted")
        if failure == "validation":
            v11_runtime_module.FinalDecisionDraft.model_validate({
                "action": "private-provider-payload", "summary": "test",
            })
        raise RuntimeError("private-provider-payload token=secret-value")

    monkeypatch.setattr(runtime, "_run_investigator", fail_before_session)
    await getattr(runtime, f"investigator_round_{round_number}")(
        repository=repository, investigation_id=record.id, event=record.event,
    )
    execution = next(e for e in repository.list_executions(record.id)
                     if e.task_id == "task-round-1")
    expected = {"quota": FailureCategory.QUOTA, "validation": FailureCategory.INVALID_OUTPUT,
                "unknown": FailureCategory.UNKNOWN}[failure]
    assert execution.failure_category == expected
    assert {"quota": "context_budget_exhausted", "validation": "action:literal_error",
            "unknown": "RuntimeError at tests/diagnosis/test_v11_runtime.py:"}[failure] in (
        execution.error_message
    )
    assert "private-provider-payload" not in execution.error_message
    assert "secret-value" not in execution.error_message


@pytest.mark.parametrize("compact", [True, False])
@pytest.mark.parametrize("round_number", [1, 2])
def test_critic_prompt_and_correction_distinguish_exclusion_from_support(compact, round_number):
    repository, record = _repository()
    runtime = V11Runtime(model="fake", turn=None if compact else lambda: None)
    runtime.runtime_run_id = "run-consistency"
    review = CoordinationReview(investigation_id=record.id, runtime_run_id=runtime.runtime_run_id)
    prompt = json.loads(runtime._critic_prompt(
        repository, record.id, record.event, review, round_number=round_number,
    ))
    correction = v11_runtime_module._structured_output_retry_feedback(
        CriticCompactOutput if compact else CriticOutput,
    )
    for rules in (prompt["evidence_interpretation_rules"], correction):
        assert "healthy or ruled out requires fail and reject" in rules
        assert "Never select an entity you ruled out" in rules
        assert "not proof of causality" in rules
        assert "not causes or causal rankings" in rules
        assert "not pure network time" in rules
        assert "do not uniquely identify packet loss" in rules
        assert "kernel CPU plus fs reads/writes and cache growth favors filesystem work" in rules
        assert "whole-window mean mixes baseline and incident" in rules
        assert "Empty filtered queries do not" in rules
        assert "which observed fact distinguishes" in rules
        assert "do not force a second round" in rules


@pytest.mark.parametrize("compact", [True, False])
def test_critic_sees_bounded_run_owned_query_outcomes(compact):
    from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus

    repository, record = _repository()
    runtime = V11Runtime(model="fake", turn=None if compact else lambda: None)
    runtime.runtime_run_id = "run-query-history"
    calls = [ToolCallRecord(
        id=f"tool-{i}", task_id="task-history", agent_name="investigator-1",
        tool_name="read_logs", status=ToolCallStatus.SUCCESS,
        input={"keywords": [f"query-{i}"]}, runtime_run_id=runtime.runtime_run_id,
        output_evidence_ids=[], budget_charged=True,
    ) for i in range(14)]
    calls.append(calls[0].model_copy(update={
        "id": "other-run-call", "runtime_run_id": "another-run",
        "input": {"keywords": ["other-run-only"]},
    }))
    repository.save_tool_calls(record.id, calls)
    review = CoordinationReview(investigation_id=record.id, runtime_run_id=runtime.runtime_run_id)
    prompt = json.loads(runtime._critic_prompt(
        repository, record.id, record.event, review, round_number=1,
    ))
    history = prompt["query_history"]
    assert history["omitted_count"] == 2
    assert len(history["queries"]) == 12
    assert history["queries"][-1]["filters"] == {"keywords": ["query-13"]}
    assert history["queries"][-1]["evidence_count"] == 0
    assert "other-run-only" not in json.dumps(prompt)


@pytest.mark.anyio
async def test_failed_investigator_preserves_successful_tool_evidence(monkeypatch):
    repository, record = _investigator_round_harness("run-tool-then-failure")
    record = repository.save(record.model_copy(update={
        "event": record.event.model_copy(update={
            "service": "payment-service", "source": IncidentSource.SIMULATED,
        }),
    }))
    runtime = V11Runtime(
        model="fake", max_investigators=1,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
    )
    runtime.runtime_run_id = "run-tool-then-failure"

    async def fail_after_tool(**kwargs):
        tool = next(tool for tool in kwargs["tools"] if tool.name == "read_logs")
        response = await tool.on_invoke_tool(None, json.dumps({
            "reason": "inspect errors", "start_time": "2026-08-08T07:00:00+00:00",
            "end_time": "2026-08-08T09:00:00+00:00", "keywords": ["exception"],
        }))
        assert json.loads(response)["evidence"], response
        raise ModelBehaviorError("invalid model response after tool")

    monkeypatch.setattr(runtime, "_call_model", fail_after_tool)
    await runtime.investigator_round_1(
        repository=repository, investigation_id=record.id, event=record.event,
    )
    calls = repository.list_tool_calls(record.id)
    assert calls and calls[0].output_evidence_ids
    available = {item.id for item in repository.get(record.id).evidence}
    assert set(calls[0].output_evidence_ids) <= available
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_rejected_finding_draft_does_not_discard_valid_findings():
    """模型输出是不可信数据：单个 draft 违约只拒绝该 draft（持久化审计），
    同批合法 finding 照常提交（spec §7.4 校验器可拒绝输出）。"""
    runtime_run_id = "run-draft-partial-reject"
    repository, record = _investigator_round_harness(runtime_run_id)

    async def turn(**_kwargs):
        return critic_response(
            {
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
        )

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
    completed = [item for item in executions if item.status == AgentExecutionStatus.COMPLETED]
    audits = [item for item in executions if item.status == AgentExecutionStatus.FAILED]
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
        return critic_response(
            {
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
        )

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
        "affected_entity": "checkout-service",
        "failure_class": "bounded failure class",
        "failure_mechanism": "bounded failure mechanism",
        "supporting_evidence_ids": (
            supporting_evidence_ids if supporting_evidence_ids is not None else ["ev-candidate"]
        ),
        "contradicting_evidence_ids": [],
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
        return critic_response(
            {
                "summary": "done",
                "findings": [],
                "candidates": [_candidate_draft(supporting_evidence_ids=["ev-bogus"])],
            }
        )

    repository, record = await _run_candidate_round("run-candidate-bogus-finding", turn)

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
    repository.save_plan(plan.model_copy(update={"tasks": [*plan.tasks, prior_task]}))
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
        return critic_response(
            {
                "summary": "done",
                "findings": [],
                "candidates": [
                    _candidate_draft(
                        supporting_finding_ids=[persisted.id],
                        supporting_evidence_ids=[seeded_evidence.id],
                    )
                ],
            }
        )

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
        return critic_response(
            {
                "summary": "done",
                "findings": [],
                "candidates": [_candidate_draft(supporting_evidence_ids=[skipped.id])],
            }
        )

    repository, record = await _run_candidate_round(
        "run-candidate-skipped-evidence", turn, seed=[skipped]
    )

    review = repository.get_coordination_review(record.id)
    assert review is None or review.candidates == []
    audits = [
        item
        for item in repository.list_executions(record.id)
        if item.error_message == "candidate draft rejected: candidate_evidence_reference"
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
    assert audit_executions[0].error_message == ("candidate draft rejected: scope_entity_mismatch")


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
    assert audit_executions[0].error_message == ("candidate draft rejected: candidate_incomplete")


@pytest.mark.anyio
async def test_candidate_drop_does_not_fail_sibling_candidates():
    async def turn(**_kwargs):
        return critic_response(
            {
                "summary": "done",
                "findings": [],
                "candidates": [
                    _candidate_draft(supporting_evidence_ids=["ev-bogus"]),
                    _candidate_draft(rank=2),
                ],
            }
        )

    repository, record = await _run_candidate_round("run-candidate-sibling", turn)

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
    assert tool.params_json_schema["properties"]["payload_json"] == {"type": "string"}
    assert '"value"' in tool.description
    assert '"minLength": 3' in tool.description
    with pytest.raises(ModelBehaviorError):
        asyncio.run(tool.on_invoke_tool(None, '{"payload_json":"{\\"value\\":\\"x\\"}"}'))
    assert asyncio.run(
        tool.on_invoke_tool(None, '{"payload_json":"{\\"value\\":\\"valid\\"}"}')
    ) == StrictOutput(value="valid")


@pytest.mark.anyio
async def test_strict_output_tool_transport_configures_agent_finalization(monkeypatch):
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    captured = {}

    async def fake_run(agent, *_args, **_kwargs):
        captured["agent"] = agent
        return SimpleNamespace(final_output=StrictOutput(value="ok"), raw_responses=[])

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
    assert agent.tool_use_behavior == {"stop_at_tool_names": ["submit_structured_output"]}
    assert agent.model_settings.tool_choice == "submit_structured_output"
    assert agent.reset_tool_choice is False


@pytest.mark.anyio
async def test_v11_investigator_sdk_turn_ceiling_preserves_critic_slot(monkeypatch):
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    captured: dict[str, int] = {}

    async def fake_run(_agent, *_args, **kwargs):
        captured["max_turns"] = kwargs["max_turns"]
        return SimpleNamespace(final_output=StrictOutput(value="ok"), raw_responses=[])

    monkeypatch.setattr(v11_runtime_module, "_run_with_model_lifecycle", fake_run)
    runtime = V11Runtime(
        model=OpenAICompatibleChatCompletionsModel(
            model="compat-model",
            api_key="local-secret",
            base_url="http://127.0.0.1:8000/v1",
        ),
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        max_turns=8,
    )

    await runtime._call_model(
        actor=ExecutionActor.INVESTIGATOR.value,
        prompt="return a structured result",
        output_type=StrictOutput,
        context={"request": "bounded"},
        tools=[],
        remaining_token_budget=None,
        remaining_tool_budget=8,
        step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
    )

    assert captured["max_turns"] == 4


@pytest.mark.anyio
async def test_v11_single_investigator_keeps_full_sdk_turn_ceiling(monkeypatch):
    class StrictOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: str

    captured: dict[str, int] = {}

    async def fake_run(_agent, *_args, **kwargs):
        captured["max_turns"] = kwargs["max_turns"]
        return SimpleNamespace(final_output=StrictOutput(value="ok"), raw_responses=[])

    monkeypatch.setattr(v11_runtime_module, "_run_with_model_lifecycle", fake_run)
    runtime = V11Runtime(
        model=OpenAICompatibleChatCompletionsModel(
            model="compat-model",
            api_key="local-secret",
            base_url="http://127.0.0.1:8000/v1",
        ),
        model_provider=ModelProvider.OPENAI_COMPATIBLE,
        model_name="compat-model",
        max_turns=8,
        max_investigators=1,
    )

    await runtime._call_model(
        actor=ExecutionActor.INVESTIGATOR.value,
        prompt="return a structured result",
        output_type=StrictOutput,
        context={"request": "bounded"},
        tools=[],
        remaining_token_budget=None,
        remaining_tool_budget=8,
        step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
    )

    assert captured["max_turns"] == 4


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
                history = [
                    item for item in _kwargs["input"]
                    if item.get("type") == "function_call" and item.get("name") == "read_logs"
                ]
                assert len(history) == 1
                assert json.loads(history[0]["arguments"]) == {
                    "payload_json": '{"reason":"inspect"}'
                }
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

    monkeypatch.setattr(compatible_model_module, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(compatible_model_module, "OpenAIChatCompletionsModel", FakeDelegate)
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
async def test_invalid_strict_envelope_keeps_known_response_usage(monkeypatch):
    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        def __init__(self, **kwargs):
            pass

        async def get_response(self, **kwargs):
            return ModelResponse(
                output=[ResponseFunctionToolCall(
                    type="function_call", name="read_logs", call_id="bad-envelope",
                    arguments='{"reason":"missing envelope"}',
                )], usage=Usage(input_tokens=80, output_tokens=30), response_id=None,
            )

    monkeypatch.setattr(compatible_model_module, "AsyncOpenAI", lambda **kwargs: FakeClient())
    monkeypatch.setattr(compatible_model_module, "OpenAIChatCompletionsModel", FakeDelegate)
    adapter = OpenAICompatibleChatCompletionsModel(
        model="fake", api_key="local-secret", base_url="http://127.0.0.1:8000/v1",
        structured_output_transport="strict_output_tool",
    )
    runtime = V11Runtime(model=adapter, token_budget=1000)
    events = []

    async def persist(execution_id, status, input_tokens, output_tokens, actor, payload):
        events.append((status, input_tokens, output_tokens, payload))

    runtime._persist_model_event = persist
    model = _V11BudgetedModel(adapter, runtime, logical_call_id="invalid-envelope")
    with pytest.raises(ModelBehaviorError, match="strict tool envelope is invalid"):
        await model.get_response(input="inspect", tools=[], model_settings=ModelSettings())
    assert runtime.remaining_token_budget == 890
    status, input_tokens, output_tokens, payload = events[-1]
    assert (status, input_tokens, output_tokens) == ("failed", 80, 30)
    assert payload["usage_known"] is True
    assert payload["accounted_tokens"] == 110


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
        source=IncidentSource.SIMULATED,
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
            "diagnosis_contract_revision": 2,
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
                "model_max_retries": 3,
                "model_max_corrections": 1,
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
        return critic_response(
            {
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
        )

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
    assert repository.get_plan(record.id).model_dump(mode="json") == plan.model_dump(mode="json")
    assert calls[0]["remaining_tool_budget"] == 8
    assert 0 < calls[0]["remaining_token_budget"] <= 3000


@pytest.mark.anyio
async def test_v11_run_owner_is_explicit_when_repository_has_multiple_investigations():
    repository, record = _repository()
    other = repository.save(InvestigationRecord(id="inv-other", event=_event()))

    async def turn(**_kwargs):
        return critic_response(
            {
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
        )

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
    assert [item.runtime_run_id for item in repository.list_executions(record.id)] == ["run-target"]
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
        return critic_response({"value": "ok"})

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

    with pytest.raises(V11RuntimeContractError, match="planning may only investigate"):
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
    registry = build_provider_tool_registry(build_mock_provider_registry(), lookup)
    lookup = V11Runtime.memory_lookup_from_registry(registry)

    assert lookup._current_investigation_id() is None
    with current_investigation_scope(record.id):
        assert lookup._current_investigation_id() == record.id


def test_app_container_wires_memory_resolver_for_current_investigation():
    container = AppContainer(AppSettings(storage=StorageSettings(url="memory://")))
    try:
        record = container.repository.save(InvestigationRecord(id="container-inv", event=_event()))
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
    runtime._execution_contract = _complete_contract(registry, manifest=tuple(reversed(frozen)))

    with pytest.raises(V11RuntimeContractError, match="ordered"):
        runtime._agent_manifest()


def test_v11_nested_execution_contract_digest_fences_capability_and_limits():
    container = AppContainer(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            agents=AgentsSettings(enabled=True, model="gpt-test"),
        )
    )
    record = container.repository.save(InvestigationRecord(id="inv-contract-fence", event=_event()))
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
    assert container.repository.get(record.id).multi_agent_run.max_tool_calls_per_specialist == 5
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
    runtime._input_estimate_calibration["CriticAgent"] = [100, 200, 1]
    runtime._request_cost_samples["prior-run"] = ("CriticAgent", 200, 10)

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
    assert cloned._input_estimate_calibration == {}
    assert cloned._request_cost_samples == {}
    assert runtime._request_cost_samples


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
    provider = v11_runtime_module._V11ModelProvider(V11Runtime(model="gpt-test"))

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

    monkeypatch.setattr("backend.diagnosis.openai_compatible_model.AsyncOpenAI", build_client)

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
        token_budget=100000,
    )

    with pytest.raises(openai.RateLimitError):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="bounded request",
            output_type=StrictOutput,
            context={"request": "bounded"},
            tools=[],
            remaining_token_budget=100000,
            remaining_tool_budget=8,
        )

    # 外层持久化重试预算为 3：1 次首发 + 3 次重试 = 4 个请求
    assert request_count == 4


@pytest.mark.anyio
async def test_v11_sdk_model_requests_reserve_current_available_output_caps():
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
    runtime = V11Runtime(model=model, token_budget=10000)

    await runtime._call_model(
        actor="LeadAgent",
        prompt="bounded request",
        output_type=StrictOutput,
        context={"request": "bounded"},
        tools=[tool],
        remaining_token_budget=10000,
        remaining_tool_budget=8,
    )

    assert model.calls == 2
    assert model.output_caps == [8192, 8192]
    assert runtime._remaining_token_budget == 9950


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
    runtime = V11Runtime(model=model, token_budget=10000)
    runtime.runtime_run_id = "run-sdk-usage-retry"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="bounded replay request",
        output_type=StrictOutput,
        context={"request": "two provider requests"},
        tools=[tool],
        remaining_token_budget=10000,
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
        if status == "failed" and payload.get("reservation_status") == "unknown"
    ]
    assert len(started) == 4
    assert len(completed) == 3
    assert len(retrying) == 1
    assert [payload["actual_input_tokens"] for payload in completed] == [20, 20, 20]
    first_request = started[0]["reservation_id"]
    failed_request = retrying[0]["reservation_id"]
    assert started[2]["reservation_id"] != first_request
    assert started[3]["reservation_id"] != failed_request
    assert failed_request != first_request
    assert runtime._model_reservations == {}
    assert runtime.remaining_token_budget == 10000 - int(retrying[0]["accounted_tokens"]) - sum(
        int(payload["input_tokens"]) + int(payload["output_tokens"]) for payload in completed
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
    runtime = V11Runtime(model=model, token_budget=100000)
    runtime.runtime_run_id = "run-all-failed-usage"
    runtime._persist_model_event = persist_model_event

    with pytest.raises(ClassifiedRetryableError):
        await runtime._call_model(
            actor="LeadAgent",
            prompt="all failed usage",
            output_type=BaseModel,
            context={"request": "fail"},
            tools=[],
            remaining_token_budget=100000,
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
    runtime = V11Runtime(model=model, token_budget=10000)
    runtime.runtime_run_id = "run-retry-resume-usage"
    runtime._persist_model_event = persist_model_event

    await runtime._call_model(
        actor="LeadAgent",
        prompt="retry resume usage",
        output_type=BaseModel,
        context={"request": "retry once"},
        tools=[],
        remaining_token_budget=10000,
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
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=3, output_tokens=4))

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
    assert started[0]["reservation_id"] == (f"{logical_call_id}:replay-1:request-1")
    assert started[1]["reservation_id"] == (f"{logical_call_id}:replay-1:request-2")
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
            return critic_response({"summary": "completed", "findings": [], "candidates": []})
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
        return critic_response(
            {
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
        )

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
        remaining_token_budget=50000,
    )

    assert calls == 2
    executions = repository.list_executions(record.id)
    planning = [item for item in executions if item.step_kind == ExecutionStepKind.LEAD_PLANNING]
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
        return critic_response({"malformed": True})

    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        turn=turn,
        # 保留未知用量请求的预扣后，仍需足额验证格式纠错的第三次尝试。
        token_budget=60000,
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
    assert calls == 4
    assert [item.attempt for item in executions] == [1, 2, 3, 4]
    assert all(item.status == AgentExecutionStatus.FAILED for item in executions)


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
        return critic_response(
            (
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
        return critic_response({"summary": "missing assessment", "assessments": []})

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
    assert len(executions) == 4
    assert executions[0].status == AgentExecutionStatus.FAILED
    assert executions[0].failure_category == FailureCategory.INVALID_OUTPUT
    assert all(item.failure_category == FailureCategory.INVALID_OUTPUT for item in executions)
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
        return critic_response(
            (
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
        return critic_response(
            {
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
        )

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
        return critic_response({"output": "late"})

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
        return critic_response({"output": "late"})

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
        return critic_response(
            {
                "decision": {
                    "action": "inconclusive",
                    "summary": "model-only phase completed",
                    "task_ids": [],
                    "candidate_ids": [],
                    "stop_reason": "insufficient_evidence",
                }
            }
        )

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
        return critic_response(value)

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
        return critic_response({"summary": "critic result", "assessments": []})

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
    assert "assessment_candidate_cardinality" in runtime._failures
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
    assert [item.id for item in review.candidates] == [candidate.id]
    assert review.critic_assessments == []
    assert review.diagnostic_status is None
    assert repository.get(record.id).status == InvestigationStatus.FAILED
    validation_failures = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.RESULT_VALIDATION
        and item.status == AgentExecutionStatus.FAILED
    ]
    assert validation_failures
    assert (
        validation_failures[0].error_message
        == "result validation rejected: assessment_candidate_cardinality"
    )


@pytest.mark.anyio
async def test_v12_insufficient_partial_fails_without_invented_inconclusive():
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
        pytest.fail("validation must not call another Agent")

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

    assert review.diagnostic_status is None
    assert len(review.candidates) == 1
    assert len(review.critic_assessments) == 1
    assert review.lead_decision is None
    assert review.final_decision is None
    assert review.run_status == MultiAgentRunStatus.FAILED
    assert repository.get(record.id).status == InvestigationStatus.FAILED


@pytest.mark.anyio
async def test_v12_invalid_final_result_fails_and_retains_candidate_audit(monkeypatch):
    """校验器只拒绝，保留候选审计但不制造诊断决定。"""
    repository, record = _repository()
    runtime_run_id = "run-live-validation-degradation"
    candidate = RootCauseCandidate(
        id="candidate-live-validation-degradation",
        summary="candidate retained for audit",
        rank=1,
        confidence=0.5,
    )
    assessment = CriticAssessment(
        id="assessment-live-validation-degradation",
        candidate_id=candidate.id,
        verdict=CriticVerdict.ACCEPT,
        checks=[
            CausalCheck(
                name=name,
                status=CausalCheckStatus.UNKNOWN,
                summary="not sufficient for publication",
                gap="insufficient evidence",
            )
            for name in CausalCheckName
        ],
        summary="candidate retained for audit",
        runtime_run_id=runtime_run_id,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id=runtime_run_id,
            authority_mode="agent",
            candidates=[candidate],
            critic_assessments=[assessment],
            lead_decision=LeadDecision(
                action=LeadAction.CONCLUDE,
                summary="candidate was initially selected",
                candidate_ids=[candidate.id],
            ),
            diagnostic_status=DiagnosticStatus.COMPLETE,
        )
    )

    calls = 0

    def fake_validate(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise V11ResultValidationError("partial_candidate_evidence")

    monkeypatch.setattr(v11_runtime_module, "validate_v11_result", fake_validate)
    runtime = V11Runtime(
        model="fake",
        model_provider=ModelProvider.OPENAI,
        model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
    )
    runtime.runtime_run_id = runtime_run_id

    review = await runtime.result_validation(
        repository=repository,
        investigation_id=record.id,
        event=record.event,
    )

    assert calls == 1
    assert review.diagnostic_status is None
    assert review.run_status == MultiAgentRunStatus.FAILED
    assert [item.id for item in review.candidates] == [candidate.id]
    assert review.lead_decision is None
    assert review.final_decision is None
    assert repository.get(record.id).status == InvestigationStatus.FAILED
    audit = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.RESULT_VALIDATION
    ]
    assert len(audit) == 1
    assert audit[0].status == AgentExecutionStatus.FAILED
    assert "partial_candidate_evidence" in audit[0].error_message


@pytest.mark.anyio
@pytest.mark.parametrize("tentative", [False, True])
async def test_v11_resume_restores_partial_failure_memory_before_lead_adjudication(tentative):
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
                status=(CausalCheckStatus.UNKNOWN if tentative and name == CausalCheckName.MECHANISM
                        else CausalCheckStatus.PASS),
                summary="supported",
                evidence_ids=["ev-log"],
                gap="Capacity limit unobserved" if tentative and name == CausalCheckName.MECHANISM
                    else None,
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
                status=AgentExecutionStatus.COMPLETED if tentative else AgentExecutionStatus.FAILED,
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
            diagnosis_contract_revision=2,
            final_decision=FinalDiagnosisDecision(
                actor="critic",
                action="conclude",
                candidate_ids=[candidate.id],
                evidence_ids=["ev-log", "ev-metric"],
                summary="Most likely cause" if tentative else "Accepted by Critic",
                uncertainty="Capacity limit unobserved" if tentative else None,
            ),
        ),
    )

    async def turn(**_kwargs):
        pytest.fail("publishing stored Critic decision must not call Lead")

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

    assert review.final_decision.uncertainty == ("Capacity limit unobserved" if tentative else None)
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
    invalid_checks = [dict(item) for item in checks]
    invalid_checks[0]["evidence_ids"] = ["ev-not-committed"]
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
                    "affected_entity": "checkout-service",
                    "failure_class": "bounded failure class",
                    "failure_mechanism": "bounded failure mechanism",
                    "supporting_evidence_ids": ["ev-metric"],
                    "contradicting_evidence_ids": [],
                }
            ],
        },
        {
            "summary": "the first response used an invalid evidence ID",
            "assessments": [
                {
                    "candidate_ref": "candidate-ref-from-server",
                    "verdict": CriticVerdict.ACCEPT.value,
                    "checks": invalid_checks,
                    "summary": "accepted by the seven mechanical checks",
                }
            ],
        },
        {
            "summary": "the corrected response uses committed evidence",
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
        return critic_response(turns.pop(0))

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
    turns[1]["assessments"][0]["candidate_ref"] = candidate_id
    turns[2]["decision"]["candidate_ids"] = [candidate_id]
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
    assert json.loads(lead_audits[-1].summary)["authoritative_candidate_ids"] == [candidate_id]
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
                    "affected_entity": "checkout-service",
                    "failure_class": "bounded failure class",
                    "failure_mechanism": "bounded failure mechanism",
                    "supporting_evidence_ids": ["ev-metric"],
                    "contradicting_evidence_ids": [],
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
        return critic_response(turns.pop(0))

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
    assert [item.id for item in reconciled.critic_assessments] == [first.critic_assessments[0].id]
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
async def test_v12_critic_cannot_silently_downgrade_unexecutable_work():
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
        return critic_response(
            {
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
        )

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

    assert review.run_status == MultiAgentRunStatus.FAILED
    assert review.final_decision is None
    assert repository.list_tasks(record.id) == []
    assert repository.get(record.id).status == InvestigationStatus.FAILED


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
                    "affected_entity": "checkout-service",
                    "failure_class": "bounded failure class",
                    "failure_mechanism": "bounded failure mechanism",
                    "supporting_evidence_ids": ["ev-metric"],
                    "contradicting_evidence_ids": [],
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
        # 本用例验证 phase 路由；替身显式报告用量，避免把未知用量的保守
        # 估算与提示词长度耦合。预算预留和耗尽由专门的预算用例验证。
        return critic_response(turns.pop(0)), {"input_tokens": 500, "output_tokens": 100}

    evidence = EvidenceItem(
        id="ev-metric",
        provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND,
        timestamp=record.event.started_at,
        summary="committed evidence",
        runtime_run_id="run-v11",
    )

    class StaticProvider:
        def __init__(self, provider, evidence_items=()):
            self.provider = provider
            self.evidence_items = list(evidence_items)

        def collect(self, _event):
            return ProviderResult(
                provider=self.provider,
                evidence_items=list(self.evidence_items),
            )

    providers = ProviderRegistry(
        [
            StaticProvider(EvidenceProvider.LOG),
            StaticProvider(EvidenceProvider.METRIC, [evidence]),
            StaticProvider(EvidenceProvider.DEPLOY),
            StaticProvider(EvidenceProvider.DEPENDENCY),
            StaticProvider(EvidenceProvider.SERVICE_CATALOG),
        ]
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("V11 invoked the legacy coordinator")

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
        providers=providers,
        v11_runtime=runtime,
        agents_runtime=None,
        coordinator=SimpleNamespace(collect_async=forbidden),
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
                execution_contract=_complete_contract(runtime.tool_registry, model_name="fake"),
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
    committed_record = repository.get(record.id)
    assert {item.id for item in committed_record.evidence} >= {"ev-metric"}
    assert sum(item.kind is EvidenceKind.PROVIDER_ERROR for item in committed_record.evidence) == 1
    assert all(item.runtime_run_id == "run-v11" for item in committed_record.evidence)
    assert repository.get(record.id).report is None
    assert repository.get(record.id).actions == []


def test_durable_session_rebuild_preserves_supplemental_tasks():
    class PlanPayloadOnlyRepository(InMemoryInvestigationRepository):
        def get_plan(self, investigation_id):
            plan = super().get_plan(investigation_id)
            if plan is None:
                return None
            return plan.model_copy(
                update={"tasks": [task for task in plan.tasks if task.analysis_round == 1]}
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

    assert [task.id for task in sandbox._orchestrator.repository.list_tasks(record.id)] == [
        first_task.id,
        supplemental_task.id,
    ]


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
        return critic_response({"findings": [], "candidates": []})

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
            diagnosis_contract_revision=2,
            final_decision=FinalDiagnosisDecision(
                actor="critic",
                action="inconclusive",
                summary="the evidence gap remains",
                stop_reason="insufficient_evidence",
            ),
        )
    )

    async def turn(**_kwargs):
        pytest.fail("publishing stored Critic decision must not call Lead")

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
async def test_v12_unknown_usage_retry_reserves_again_and_settles_once():
    calls = 0
    events: list[tuple[str, dict[str, object]]] = []

    class Delegate(Model):
        async def get_response(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ClassifiedRetryableError(FailureCategory.TRANSPORT)
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=3, output_tokens=4))

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

    runtime = V11Runtime(model=Delegate(), token_budget=200)
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

    assert runtime.remaining_token_budget == 110
    assert events[-1][0] == "failed"
    assert events[-1][1]["reservation_status"] == "unknown"

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
    assert (
        len(
            {
                item[1]["reservation_id"]
                for item in events
                if item[1].get("reservation_id") is not None
            }
        )
        == 2
    )
    assert events[1][1]["usage_known"] is False
    assert events[1][1]["accounted_tokens"] == 90
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
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=80, output_tokens=30))

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
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=80, output_tokens=30))

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
    assert runtime.remaining_token_budget == 100 - sum(item[1] for item in results)


@pytest.mark.anyio
@pytest.mark.parametrize("actual_tokens", [70, 71])
async def test_model_settlement_uses_only_unreserved_balance(actual_tokens):
    runtime = V11Runtime(model="fake", token_budget=100)
    for name in ("first", "other"):
        await runtime._reserve_model_budget(30, "", {}, reservation_id=name)
    assert runtime.remaining_token_budget == 40

    async def settle():
        await runtime._settle_model_budget(
            30, actual_tokens, reservation_id="first",
            input_tokens=actual_tokens - 5, actual_input_tokens=actual_tokens - 5,
            output_tokens=5,
        )

    if actual_tokens == 70:
        await settle()
        await settle()  # 同一请求重复结算不能再次扣款。
    else:
        with pytest.raises(V11RuntimeContractError, match="exceeded token budget"):
            await settle()
    assert runtime.remaining_token_budget == 0
    assert runtime._model_reservations["other"].reserved_total == 30


@pytest.mark.parametrize(
    "message", ["context_budget_exhausted", "model response exceeded token budget"]
)
def test_budget_failure_categories_are_consistent_across_roles(message):
    from backend.domain.runtime import RuntimeFailureCategory
    from backend.runtime.coordinator import _escape_failure_category

    error = V11RuntimeContractError(message)
    assert v11_runtime_module._investigator_failure_audit(error)[1] == FailureCategory.QUOTA
    assert v11_runtime_module._critic_failure_audit(error)[1] == FailureCategory.QUOTA
    assert _escape_failure_category(error) == RuntimeFailureCategory.MODEL_FAILURE
    assert _escape_failure_category(V11RuntimeContractError("bad reference")) == (
        RuntimeFailureCategory.CONTRACT_INTEGRITY
    )


def test_failed_known_usage_is_counted_once_without_unknown_estimates():
    accumulator = v11_runtime_module._ModelUsageAccumulator({}, set())
    payload = {
        "reservation_id": "rejected", "reservation_status": "rejected",
        "attempt": 1, "actual_input_tokens": 80, "input_estimate": 120,
    }
    for _ in range(2):
        accumulator.record_event(
            status="failed", input_tokens=80, output_tokens=30, safe_payload=payload,
        )
    accumulator.record_event(
        status="failed", input_tokens=0, output_tokens=0,
        safe_payload={
            **payload, "reservation_id": "unknown", "reservation_status": "unknown",
            "usage_known": False,
        },
    )
    assert (accumulator.total_input_tokens, accumulator.total_output_tokens) == (80, 30)


@pytest.mark.anyio
async def test_closing_estimate_keeps_strict_output_tool_schema():
    async def unused(*args):
        raise AssertionError("tool must not execute")

    tools = [
        FunctionTool(
            name=name, description="return structured evidence " * 100,
            params_json_schema={"type": "object", "properties": {}}, on_invoke_tool=unused,
        )
        for name in ("read_logs", "submit_structured_output")
    ]
    projected = compatible_model_module.strict_transport_tools(tools)
    context = {
        "input": "observe", "output_schema": None,
        "tools": [
            {"name": tool.name, "description": tool.description, "schema": tool.params_json_schema}
            for tool in projected
        ],
    }
    runtime = V11Runtime(model="fake", token_budget=10000, max_investigators=1)
    request = {"tools": tools, "model_settings": ModelSettings()}
    _, estimated, _ = await runtime._reserve_model_budget(
        None, "finish", context, reservation_id="closing-schema",
        actor=ExecutionActor.INVESTIGATOR.value,
        request_index=runtime.max_tool_calls_per_specialist, request_args=request,
    )
    expected, _ = v11_runtime_module._estimate_model_input(
        request["system_instructions"], {**context, "tools": [context["tools"][1]]},
    )
    assert estimated == expected
    assert [tool.name for tool in request["tools"]] == ["submit_structured_output"]
    assert request["model_settings"].tool_choice == "submit_structured_output"


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

    assert all(item[0] == 8192 for item in results)
    assert sum(item[2] for item in results) <= 100_000
    assert all(reserved > input_estimate for _, input_estimate, reserved in results)
    assert runtime.remaining_token_budget == 100_000 - sum(item[2] for item in results)
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
    assert runtime.remaining_token_budget == 100_000 - sum(item[1] for item in results)


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

    assert reserved_total == input_estimate + 16384
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


@pytest.mark.parametrize("selected", [["candidate-b", "candidate-a"], ["candidate-b"], []])
def test_v12_real_sdk_critic_selects_publication_order_without_lead_call(selected):
    repository, record = _seed_repository()
    candidates = [
        RootCauseCandidate(
            id=f"candidate-{name}",
            rank=index,
            summary=name,
            confidence=0.8,
            affected_entity="checkout-service",
            failure_mechanism="errors",
            supporting_evidence_ids=["ev-metric"],
        )
        for index, name in enumerate(("a", "b"), 1)
    ]
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            runtime_run_id="run-v11",
            authority_mode="agent",
            diagnosis_contract_revision=2,
            candidates=candidates,
        )
    )

    class CriticModel(Model):
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            payload = {
                "assessments": [
                    {
                        "candidate_ref": candidate.id,
                        "verdict": "accept",
                        "checks": [
                            {
                                "name": name.value,
                                "status": "pass",
                                "evidence_ids": ["ev-metric"],
                                "gap": None,
                            }
                            for name in CausalCheckName
                        ],
                        "gap": None,
                        "supplemental_task_ids": [],
                    }
                    for candidate in candidates
                ],
                "tasks": [],
                "final_decision": {
                    "action": "conclude" if selected else "inconclusive",
                    "candidate_refs": selected,
                    "evidence_ids": ["ev-metric"],
                    "summary": "Compared candidates using committed evidence",
                    "stop_reason": None if selected else "remaining uncertainty",
                },
            }
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="message-critic",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text", text=json.dumps(payload), annotations=[]
                            )
                        ],
                    )
                ],
                usage=Usage(requests=1, input_tokens=100, output_tokens=100),
                response_id=None,
            )

        async def stream_response(self, *args, **kwargs):
            raise AssertionError("streaming is not used")
            yield

    model = CriticModel()
    runtime = V11Runtime(
        model=model, model_provider=ModelProvider.OPENAI, model_name="fake", token_budget=100000
    )
    runtime.runtime_run_id = "run-v11"

    async def run():
        await runtime.critic_review(
            repository=repository, investigation_id=record.id, event=record.event
        )
        return await runtime.lead_adjudication(
            repository=repository, investigation_id=record.id, event=record.event
        )

    review = asyncio.run(run())
    assert model.calls == 1
    assert review.final_decision.actor == "critic"
    assert review.authoritative_candidate_ids == selected
    assert [candidate.id for candidate in review.authoritative_candidates] == selected
    assert [candidate.id for candidate in review.candidates] == ["candidate-a", "candidate-b"]
    assert review.lead_decision == review.final_decision.as_lead_decision()
    publication = [
        item
        for item in repository.list_executions(record.id)
        if item.step_kind == ExecutionStepKind.LEAD_ADJUDICATION
    ]
    assert len(publication) == 1
    assert publication[0].execution_layer == AgentExecutionLayer.CUSTOM
    assert publication[0].input_tokens == 0
    assert (
        CoordinationReview.model_validate(review.model_dump()).final_decision
        == review.final_decision
    )


@pytest.mark.anyio
@pytest.mark.parametrize("token_limited", [False, True, pytest.param(None, id="tool-calls")])
async def test_v12_sdk_closes_tools_when_only_finalization_budget_remains(token_limited):
    class ClosingModel(Model):
        async def get_response(self, **kwargs):
            assert kwargs["tools"] == []
            assert kwargs["model_settings"].tool_choice == "none"
            return SimpleNamespace(output=[], usage=Usage(input_tokens=5, output_tokens=5))

        async def stream_response(self, **kwargs):
            raise AssertionError("not used")
            yield

    async def query(*args):
        pytest.fail("closed tools must not execute")

    runtime = V11Runtime(
        model=ClosingModel(),
        token_budget=500 if token_limited else 10000,
        max_investigators=1 if token_limited else 3,
    )
    runtime._model_turn_budget_enabled = True
    runtime._remaining_model_turns = 2 if token_limited is False else 16
    if token_limited is None:
        # 一次模型请求可产生多个工具调用；尚未达到请求上限也必须收尾。
        runtime._model_tool_sessions["closing"] = SimpleNamespace(remaining_tool_calls=0)

    async def reserve():
        return runtime._remaining_model_turns - 1

    runtime._reserve_model_turn_callback = reserve
    tool = FunctionTool(
        name="read_logs",
        description="bounded query " * 500,
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=query,
    )
    model = _V11BudgetedModel(
        runtime.model, runtime, logical_call_id="closing", actor=ExecutionActor.INVESTIGATOR.value
    )
    await model.get_response(
        input="observe",
        system_instructions="close with existing evidence",
        tools=[tool],
        model_settings=ModelSettings(),
    )
    assert runtime._remaining_model_turns == (1 if token_limited is False else 15)
    assert runtime.remaining_token_budget == (490 if token_limited else 9990)


@pytest.mark.anyio
async def test_closing_request_preserves_observations_without_closed_tool_history():
    class ClosingModel(Model):
        async def get_response(self, **kwargs):
            assert kwargs["tools"] == []
            assert kwargs["model_settings"].tool_choice == "none"
            messages = kwargs["input"]
            assert len(messages) == 1 and messages[0]["role"] == "user"
            data = json.loads(messages[0]["content"])
            assert data["collected_observations"][0]["evidence"][0]["id"] == "ev-measured"
            assert data["context"] == ["assigned investigation"]
            assert "function_call" not in json.dumps(messages)
            return SimpleNamespace(output=[], usage=Usage(input_tokens=10, output_tokens=10))

        async def stream_response(self, **kwargs):
            raise AssertionError("not used")
            yield

    runtime = V11Runtime(model=ClosingModel(), token_budget=10000, max_investigators=1)
    runtime._model_tool_sessions["closing"] = SimpleNamespace(remaining_tool_calls=0)
    history = [
        {"role": "user", "content": "assigned investigation"},
        {"type": "function_call", "name": "query_metrics", "call_id": "call-1",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-1", "output": json.dumps({
            "status": "success", "evidence": [{"id": "ev-measured", "summary": "observed"}],
            "returned_count": 1, "truncated": False, "stop_reason": None,
        })},
    ]
    original = json.dumps(history)
    model = _V11BudgetedModel(runtime.model, runtime, logical_call_id="closing",
                              actor=ExecutionActor.INVESTIGATOR.value)
    await model.get_response(input=history, system_instructions="investigate", tools=[],
                             model_settings=ModelSettings())
    assert json.dumps(history) == original
    assert runtime.remaining_token_budget == 9980


@pytest.mark.anyio
@pytest.mark.parametrize("request_detail", [False, True])
async def test_v12_digest_reports_omissions_and_explicit_scope_returns_details(request_detail):
    repository, record = _investigator_round_harness("run-details")
    evidence = [
        EvidenceItem(
            id=f"ev-detail-{index}",
            runtime_run_id="run-details",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            timestamp=record.event.started_at,
            summary=f"observation {index}",
            payload={"message": f"detail {index}"},
        )
        for index in range(6)
    ]
    plan = repository.get_plan(record.id)
    plan.tasks[0].information_gap = "inspect omitted log observations"
    plan.tasks[0].evidence_scope = {"evidence_ids": [evidence[0].id]} if request_detail else None
    repository.save_plan(plan)
    repository.save(record.model_copy(update={"evidence": evidence}))
    captured = []

    async def turn(**kwargs):
        captured.append(kwargs["context"])
        return {"findings": [], "candidates": []}

    runtime = V11Runtime(
        model="fake",
        turn=turn,
        token_budget=100000,
        max_investigators=1,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
    )
    runtime.runtime_run_id = "run-details"
    await runtime.investigator_round_1(
        repository=repository, investigation_id=record.id, event=record.event
    )
    assert len(captured) == 1
    context = captured[0]
    if request_detail:
        assert context["assigned_evidence_details"][0]["payload"] == {"message": "detail 0"}
        assert context["own_committed_evidence_ids"] == [evidence[0].id]
        assert context["evidence_coverage"]["available"] == 1
    else:
        assert context["assigned_evidence_details"] == []
        assert context["evidence_coverage"]["available"] == 6
        assert context["evidence_coverage"]["included"] == 4
        assert context["evidence_coverage"]["omitted"] == 2
        assert evidence[0].id in context["evidence_coverage"]["omitted_evidence_ids"]


def test_v12_detail_scope_does_not_escape_run_or_requested_entity():
    task = DiagnosisTask(
        id="task",
        title="read details",
        description="read scoped details",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="investigator",
        runtime_run_id="run",
        analysis_round=1,
        evidence_scope={"evidence_ids": ["missing"]},
    )
    item = EvidenceItem(
        id="ev",
        runtime_run_id="run",
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime.now(UTC),
        summary="details",
        scope=EvidenceScope(entity_ids=["checkout"]),
    )
    with pytest.raises(V11RuntimeContractError, match="another run or missing"):
        v11_runtime_module._evidence_for_task(task, [item])
    task.evidence_scope = {"entity_ids": ["payment"]}
    assert v11_runtime_module._evidence_for_task(task, [item]) == []
    task.evidence_scope = {"evidence_ids": ["ev"]}
    assert v11_runtime_module._evidence_for_task(task, [item]) == [item]
    task.evidence_scope = {"start_time": "2026-01-01T00:00:00"}
    with pytest.raises(V11RuntimeContractError, match="invalid evidence scope"):
        v11_runtime_module._evidence_for_task(task, [item])


@pytest.mark.anyio
async def test_large_tool_history_is_compacted_before_concurrent_budget_reservation():
    from backend.diagnosis.adaptive_tools import compact_tool_history

    payload = {
        "status": "success", "evidence": [
            {"id": f"ev-{i}", "summary": "observed latency " * 30, "provider": "metric"}
            for i in range(100)
        ], "returned_count": 100, "truncated": False, "stop_reason": None,
    }
    history = [
        {"type": "function_call", "name": "query_metrics", "call_id": "call-1",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-1", "output": json.dumps(payload)},
    ]
    original = json.dumps(history)

    class CapturingModel(Model):
        async def get_response(self, **kwargs):
            sent = kwargs["input"]
            assert sent[0] == history[0]
            assert sent[1]["call_id"] == "call-1"
            page = json.loads(sent[1]["output"])
            assert page["truncated"] and page["omitted_count"] > 0
            assert len(sent[1]["output"]) <= 6000
            return SimpleNamespace(output=[], usage=Usage(input_tokens=100, output_tokens=20))

        async def stream_response(self, **kwargs):
            raise AssertionError("not used")
            yield

    runtime = V11Runtime(model=CapturingModel(), token_budget=30000, max_investigators=3)
    runtime._active_sessions = {object(), object(), object()}
    raw, _ = v11_runtime_module._estimate_model_input("investigate", {"input": history})
    compact, _ = v11_runtime_module._estimate_model_input(
        "investigate", {"input": compact_tool_history(history)}
    )
    assert compact < raw / 4
    with pytest.raises(V11RuntimeContractError, match="context_budget_exhausted"):
        await runtime._reserve_model_budget(
            None, "investigate", {"input": history, "tools": []},
            actor=ExecutionActor.INVESTIGATOR.value,
            request_args={"tools": [], "model_settings": ModelSettings()},
        )
    model = _V11BudgetedModel(
        runtime.model, runtime, logical_call_id="large-history",
        actor=ExecutionActor.INVESTIGATOR.value,
    )
    await model.get_response(
        input=history, system_instructions="investigate", tools=[],
        output_schema=None, model_settings=ModelSettings(),
    )
    assert json.dumps(history) == original
    assert runtime.remaining_token_budget == 29880


def test_critic_gap_bounds_and_validation_feedback_are_actionable():
    from backend.diagnosis.v11_runtime import CriticCompactCausalCheck

    gap = "Dependency timing is unavailable; correlate the upstream spans before accepting."
    CriticCompactCausalCheck(name="topology", status="unknown", gap=gap)
    with pytest.raises(ValidationError) as error:
        CriticCompactCausalCheck(name="topology", status="unknown", gap="x" * 129)
    signature = v11_runtime_module._structured_validation_signature(error.value)
    assert signature == "gap:string_too_long(max=128)"
    assert "x" * 20 not in signature


def test_critic_sees_and_validates_partial_publication_requirements():
    repository, record = _seed_repository()
    candidate = RootCauseCandidate(
        id="candidate-partial", rank=1, summary="bounded signal", confidence=0.8,
        affected_entity="checkout-service", failure_mechanism="latency",
        supporting_evidence_ids=["ev-metric"],
    )
    review = CoordinationReview(
        investigation_id=record.id, runtime_run_id="run-v11", authority_mode="agent",
        diagnosis_contract_revision=2, candidates=[candidate],
    )
    runtime = V11Runtime(model=None)
    runtime.runtime_run_id = "run-v11"
    runtime._failures.append("investigator_failed")
    prompt = json.loads(runtime._critic_prompt(
        repository, record.id, record.event, review, round_number=1
    ))
    assert prompt["publication_requirements"]["candidate_errors"] == {
        candidate.id: "partial_candidate_evidence"
    }
    assert prompt["publication_requirements"]["eligible_candidate_refs"] == []
    assert prompt["candidates"][0]["publication_error"] == "partial_candidate_evidence"
    assert "If it is zero" in prompt["rule"]
    output = CriticCompactOutput.model_validate({
        "assessments": [], "tasks": [], "final_decision": {
            "action": "conclude", "candidate_refs": [candidate.id],
            "evidence_ids": ["ev-metric"], "summary": "bounded result", "stop_reason": None,
        },
    })
    with pytest.raises(ClassifiedRetryableError) as error:
        runtime._normalize_final_decision(output, review, [], repository, record.id)
    assert error.value.audit_code == "partial_candidate_evidence"
    output.final_decision.action = "inconclusive"
    output.final_decision.candidate_refs = []
    output.final_decision.stop_reason = "insufficient independent support"
    decision = runtime._normalize_final_decision(output, review, [], repository, record.id)
    assert decision.action == LeadAction.INCONCLUSIVE


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["settle", "cancel", "unknown", "timeout"])
async def test_budget_waits_for_inflight_settlement_without_stealing_reservations(outcome):
    runtime = V11Runtime(model="fake", token_budget=3000)
    _, first_input, first_total = await runtime._reserve_model_budget(
        None, "first", {}, reservation_id="first:request-1", logical_call_id="first",
    )
    runtime._inflight_model_requests.add("first:request-1")
    remaining = runtime.remaining_token_budget
    if outcome == "timeout":
        runtime.timeout_seconds = 0.01
    waiting = asyncio.create_task(runtime._reserve_model_budget(
        None, "second " * 600, {}, reservation_id="second:request-1", logical_call_id="second",
    ))
    await asyncio.sleep(0)
    assert not waiting.done()
    assert runtime.remaining_token_budget == remaining
    assert "second:request-1" not in runtime._model_reservations
    if outcome == "timeout":
        with pytest.raises(TimeoutError):
            await waiting
        assert runtime.remaining_token_budget == remaining
        assert "first:request-1" in runtime._model_reservations
        return
    if outcome == "cancel":
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert runtime.remaining_token_budget == remaining
        assert "first:request-1" in runtime._model_reservations
        return
    await runtime._settle_model_budget(
        first_total, first_input, reservation_id="first:request-1", logical_call_id="first",
        input_tokens=first_input, usage_known=outcome != "unknown",
    )
    runtime._inflight_model_requests.discard("first:request-1")
    if outcome == "unknown":
        with pytest.raises(V11RuntimeContractError, match="context_budget_exhausted"):
            await asyncio.wait_for(waiting, 1)
        assert runtime.remaining_token_budget == remaining
    else:
        _, second_input, second_total = await asyncio.wait_for(waiting, 1)
        assert second_total > second_input
        assert runtime.remaining_token_budget == 3000 - first_input - second_total


@pytest.mark.anyio
@pytest.mark.parametrize("corrected", [True, False])
@pytest.mark.parametrize("saved_corrections", [None, 1, 3])
async def test_unknown_planning_skill_uses_at_most_three_structured_corrections(
    corrected, saved_corrections, monkeypatch,
):
    repository, record = _repository()
    calls = []

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", no_sleep)

    async def turn(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            assert "planning_unknown_skill" in kwargs["prompt"]
        return {"decision": {
            "action": "inconclusive", "summary": "Insufficient evidence",
            "stop_reason": "missing observations",
            "selected_skills": [] if corrected and len(calls) == 4 else ["not_in_catalog@1.0.0"],
        }, "tasks": []}

    runtime = V11Runtime(
        model="fake", turn=turn,
        model_provider=ModelProvider.OPENAI, model_name="fake",
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
    )
    async def run():
        return await runtime.plan_lead(
            repository=repository, investigation_id=record.id, event=record.event,
            runtime_run_id="run-v11", remaining_tool_budget=8, remaining_token_budget=100000,
        )
    if saved_corrections is not None:
        contract = _complete_contract(runtime.tool_registry)
        contract["retry_policy"]["model_max_corrections"] = saved_corrections
        runtime._execution_contract = seal_v11_execution_contract(contract)
    if corrected and saved_corrections != 1:
        plan = await run()
        assert plan.lead_decision.selected_skills == []
    else:
        with pytest.raises(
            V11RuntimeContractError,
            match=r"structured correction exhausted \[planning_unknown_skill\]",
        ):
            await run()
        assert repository.get_plan(record.id) is None
        with pytest.raises(V11RuntimeContractError, match="exhausted"):
            await run()
    assert len(calls) == (2 if saved_corrections == 1 else 4)
    failures = [e for e in repository.list_executions(record.id) if e.status == "failed"]
    assert failures and all("planning_unknown_skill" in e.error_message for e in failures)


@pytest.mark.anyio
async def test_planning_repairs_unknown_evidence_before_creating_tasks(monkeypatch):
    repository, record = _repository()
    calls = []

    async def no_sleep(_seconds):
        pass

    async def turn(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            assert "planning_evidence_reference" in kwargs["prompt"]
            assert repository.list_tasks(record.id) == []
        return {"decision": {"action": "investigate", "summary": "Inspect service",
                             "task_ids": ["task-1"]},
                "tasks": [{"id": "task-1", "title": "Inspect service",
                           "description": "Inspect latency and resource changes",
                           "evidence_scope": {"entity_ids": ["checkout-service"],
                                              "evidence_ids": (
                                                  ["missing-id"] if len(calls) == 1 else [])}}]}

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", no_sleep)
    runtime = V11Runtime(model="fake", turn=turn,
                        tool_registry=build_provider_tool_registry(build_mock_provider_registry()))
    await runtime.plan_lead(
        repository=repository, investigation_id=record.id, event=record.event,
        runtime_run_id="run-v11", remaining_tool_budget=8, remaining_token_budget=100000,
    )
    assert len(calls) == 2
    tasks = repository.list_tasks(record.id)
    assert len(tasks) == 1
    assert tasks[0].evidence_scope["evidence_ids"] == []


def test_compact_critic_cross_field_errors_have_safe_actionable_codes():
    from backend.diagnosis.v11_runtime import CriticCompactAssessmentDraft

    with pytest.raises(ValidationError) as error:
        CriticCompactAssessmentDraft(
            candidate_ref="candidate-1", verdict="accept", gap="private-value",
            checks=[{
                "name": name.value, "status": "pass", "evidence_ids": ["ev-1"], "gap": None,
            } for name in CausalCheckName], supplemental_task_ids=[],
        )
    signature = v11_runtime_module._structured_validation_signature(error.value)
    assert signature == "root:supplemental_fields_require_needs_evidence"
    assert "private-value" not in signature
    feedback = v11_runtime_module._structured_output_retry_feedback(CriticCompactOutput)
    assert "assessment.gap must be null" in feedback
    assert "Never combine these branches" in feedback


@pytest.mark.anyio
async def test_critic_correction_removes_closed_tools_and_explains_publication_error():
    calls = []

    async def turn(**kwargs):
        calls.append(kwargs)
        return {"final_decision": {
            "action": "inconclusive", "candidate_refs": [], "evidence_ids": [],
            "summary": "Missing independent support", "stop_reason": "support gap",
        }, "assessments": [], "tasks": []}

    def validate(_output):
        if len(calls) == 1:
            raise ClassifiedRetryableError(
                FailureCategory.INVALID_OUTPUT, audit_code="partial_candidate_provider_types",
            )

    runtime = V11Runtime(model="fake", turn=turn, token_budget=60000)
    result = await runtime._call_model(
        actor="CriticAgent", prompt="Review the committed evidence",
        output_type=CriticCompactOutput, context={"tools": [{"name": "query_metrics"}]},
        tools=[], remaining_token_budget=60000, output_validator=validate,
        correction_prompt=lambda: "Updated supplemental_task_capacity=0",
    )
    assert len(calls) == 2
    assert calls[1]["context"]["tools"] == []
    assert "lack sufficient independent support" in calls[1]["prompt"]
    assert "Updated supplemental_task_capacity=0" in calls[1]["prompt"]
    assert result.output["final_decision"]["action"] == "inconclusive"


def test_supplemental_admission_keeps_tokens_for_final_review_and_correction():
    runtime = V11Runtime(model="fake", token_budget=60000)
    runtime._remaining_token_budget = 23656
    assert runtime._supplemental_capacity(8, current_critic=False) == 0
    runtime._remaining_token_budget = 100000
    assert runtime._supplemental_capacity(8, current_critic=False) >= 1
    assert runtime._supplemental_capacity(0, current_critic=False) == 0
    assert runtime._supplemental_capacity(8, current_critic=True) <= (
        runtime._supplemental_capacity(8, current_critic=False)
    )


@pytest.mark.parametrize("current_critic", [True, False])
def test_supplemental_capacity_depends_on_remaining_cost_not_original_limit(current_critic):
    capacities = []
    for original_budget in (100000, 200000, 600000):
        runtime = V11Runtime(model="fake", token_budget=original_budget, max_turns=48)
        runtime._remaining_token_budget = 90000
        capacities.append(runtime._supplemental_capacity(8, current_critic=current_critic))
    assert len(set(capacities)) == 1
    assert capacities[0] > 0

    runtime._model_turn_budget_enabled = True
    runtime._remaining_model_turns = 3 + int(current_critic)
    assert runtime._supplemental_capacity(8, current_critic=current_critic) == 0
    runtime._remaining_model_turns += 1
    assert runtime._supplemental_capacity(8, current_critic=current_critic) == 1


def test_compact_planning_summary_uses_persisted_decision_bound():
    draft = v11_runtime_module.LeadPlanningDecisionDraft
    assert draft(action="investigate", summary="x" * 512).summary == "x" * 512
    with pytest.raises(ValidationError):
        draft(action="investigate", summary="x" * 513)


def test_supplemental_capacity_uses_known_request_costs_and_restores_them():
    runtime = V11Runtime(model="fake", token_budget=50000, max_turns=48)
    events = []
    for index, actor in enumerate(("InvestigatorAgent", "CriticAgent"), start=1):
        payload = {
            "reservation_id": f"cost-{index}", "reservation_status": "completed",
            "usage_known": True, "input_tokens": 5000, "output_tokens": 500,
        }
        runtime._record_model_usage_event(actor, "completed", 5000, 500, payload)
        runtime._record_model_usage_event(actor, "completed", 5000, 500, payload)
        events.append(RuntimeEvent(
            run_id="run-cost", attempt_id="attempt-1", sequence=index,
            actor_type=RuntimeActorType.AGENT,
            event_type=RuntimeEventType.MODEL_COMPLETED, actor_name=actor, safe_payload=payload,
        ))
    assert len(runtime._request_cost_samples) == 2
    assert runtime._supplemental_capacity(8, current_critic=True) == 1
    runtime._record_model_usage_event("CriticAgent", "completed", 90000, 10000, {
        "reservation_id": "unknown", "reservation_status": "unknown", "usage_known": False,
    })
    assert runtime._supplemental_capacity(8, current_critic=True) == 1
    restored = V11Runtime(model="fake", token_budget=50000, max_turns=48)
    restored.bind_phase(PhaseInput(
        run_id="run-cost", attempt_id="attempt-2", phase=RuntimePhase.CRITIC_REVIEW,
        resume_state=RuntimeResumeState(remaining_token_budget=50000),
        token_budget=50000, model_events=tuple(events),
    ))
    assert restored._request_cost_samples == runtime._request_cost_samples
    assert restored._supplemental_capacity(8, current_critic=True) == 1


def test_metric_names_are_available_in_live_and_persisted_prompt_projections():
    item = EvidenceItem(
        id="ev-metric-name", provider=EvidenceProvider.METRIC,
        kind=EvidenceKind.METRIC_TREND, summary="Memory increased",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        payload={"metric": "checkout_mem", "signal_type": "memory"},
    )
    for project in (_evidence_projection, v11_runtime_module._live_evidence_prompt_projection):
        assert project(item)["metric_name"] == "checkout_mem"


@pytest.mark.anyio
async def test_parallel_first_round_preserves_supplemental_tools_and_resume_limits():
    from tests.diagnosis.test_adaptive_tools import QueryProvider

    repository, record = _repository()
    provider = QueryProvider("read_logs", EvidenceProvider.LOG, None)
    registry = build_provider_tool_registry(ProviderRegistry([provider]))

    async def turn(**kwargs):
        tool = next(tool for tool in kwargs["tools"] if tool.name == "read_logs")
        responses = await asyncio.gather(*[
            tool.on_invoke_tool(None, json.dumps({"keywords": [f"query-{i}"]}))
            for i in range(8)
        ])
        assert all(json.loads(r)["remaining_tool_calls"] >= 0 for r in responses)
        return ({"findings": [], "candidates": []}, {"input_tokens": 1, "output_tokens": 1})

    runtime = V11Runtime(
        model="fake", turn=turn, tool_registry=registry, max_total_tool_calls=24,
        max_tool_calls_per_specialist=8, max_rounds=2, max_investigators=3,
        token_budget=200000,
    )
    runtime.runtime_run_id = "run-v11"
    tasks = [DiagnosisTask(
        id=f"task-{i}", title="Collect discriminator", description="Check bounded logs",
        information_gap="Find the cause",
        task_type=DiagnosisTaskType.GENERAL_INVESTIGATION, agent_name="InvestigatorAgent",
        analysis_round=1, runtime_run_id="run-v11",
    ) for i in range(3)]
    await asyncio.gather(*[runtime._run_investigator(
        repository=repository, investigation_id=record.id, event=record.event,
        task=task, round_number=1, seed_evidence=[], own_findings=[],
    ) for task in tasks])
    assert provider.calls == 18
    assert runtime._remaining_tool_budget_for(repository, record.id) == 6
    calls = repository.list_tool_calls(record.id)
    assert sum(call.consumes_budget for call in calls) == 18
    assert any(call.budget_charged is False for call in calls)
    # 恢复后仍使用已持久化准入记录，首轮任务不能重新领六次额度。
    restored = V11Runtime(
        model="fake", turn=turn, tool_registry=registry, max_total_tool_calls=24,
        max_tool_calls_per_specialist=8, max_rounds=2, max_investigators=3,
        token_budget=200000,
    )
    restored.runtime_run_id = "run-v11"
    assert all(restored._task_tool_capacity(repository, record.id, t) == 0 for t in tasks)
    supplemental = tasks[0].model_copy(update={"id": "supplemental", "analysis_round": 2})
    await restored._run_investigator(
        repository=repository, investigation_id=record.id, event=record.event,
        task=supplemental, round_number=2, seed_evidence=[], own_findings=[],
    )
    assert provider.calls == 24
    assert restored._remaining_tool_budget_for(repository, record.id) == 0


@pytest.mark.anyio
async def test_critic_can_finish_large_output_without_raising_run_budget():
    async def turn(**kwargs):
        assert kwargs["max_output_tokens"] == 16384
        return ({"final_decision": {
            "action": "inconclusive", "candidate_refs": [], "evidence_ids": [],
            "summary": "Missing causal support", "stop_reason": "support gap",
        }, "assessments": [], "tasks": []}, {"input_tokens": 100, "output_tokens": 3000})

    runtime = V11Runtime(model="fake", turn=turn, token_budget=60000)
    result = await runtime._call_model(
        actor=ExecutionActor.CRITIC.value, prompt="Review all candidates",
        output_type=CriticCompactOutput, context={}, tools=[], remaining_token_budget=60000,
    )
    assert result.output_tokens == 3000
    assert runtime.remaining_token_budget == 56900

    bounded = V11Runtime(model="fake", token_budget=500)
    cap, input_tokens, reserved = await bounded._reserve_model_budget(
        None, "Review", {}, actor=ExecutionActor.CRITIC.value,
    )
    assert 0 < cap < 500
    assert input_tokens + cap == reserved == 500
    assert bounded.remaining_token_budget == 0


@pytest.mark.anyio
async def test_critic_repairs_invalid_reference_then_missing_final_decision(monkeypatch):
    repository, record = _seed_repository()
    candidate = RootCauseCandidate(
        id="candidate-repair", rank=1, summary="observed latency", confidence=0.5,
        affected_entity="checkout-service", failure_mechanism="latency",
        supporting_evidence_ids=["ev-metric"],
    )
    review = CoordinationReview(
        investigation_id=record.id, runtime_run_id="run-v11", authority_mode="agent",
        diagnosis_contract_revision=2, candidates=[candidate],
    )
    prompts = []

    async def no_sleep(_seconds):
        pass

    async def turn(**kwargs):
        prompts.append(kwargs["prompt"])
        assert kwargs["tools"] == []
        if len(prompts) > 1:
            assert kwargs["context"]["previous_structured_output"]["assessments"][0][
                "candidate_ref"
            ] == candidate.id
        return ({
            "assessments": [{
                "candidate_ref": candidate.id, "verdict": "inconclusive",
                "checks": [{
                    "name": name.value, "status": "unknown", "summary": "Missing support",
                    "evidence_ids": ["unavailable-id"] if len(prompts) == 1 else [],
                    "gap": "Missing causal observation",
                } for name in CausalCheckName],
            }], "tasks": [], "final_decision": None if len(prompts) == 2 else {
                "action": "inconclusive", "candidate_refs": [], "evidence_ids": ["ev-metric"],
                "summary": "Insufficient support", "stop_reason": "Missing causal observations",
            },
        }, {"input_tokens": 100, "output_tokens": 500})

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", no_sleep)
    runtime = V11Runtime(model="fake", turn=turn, token_budget=100000)
    runtime.runtime_run_id = "run-v11"

    def validate(output):
        assessments = runtime._normalize_assessments(
            output.assessments, candidate_ids={candidate.id}, runtime_run_id="run-v11",
            review_round=1, usable_evidence_ids={"ev-metric"},
        )
        runtime._normalize_final_decision(output, review, assessments, repository, record.id)

    # 模拟输入应与声明的 usage 相称，避免极短占位词把自校准倍率放大到几十倍。
    prompt = (
        "Review the candidate against the committed evidence. Check temporal alignment, "
        "topology, mechanism, blast radius, symptom versus cause, counterevidence, and "
        "alternatives. Use only the supplied evidence IDs. Missing causal support requires "
        "an inconclusive assessment and a final decision with no selected candidates. "
    ) * 3
    result = await runtime._call_model(
        actor=ExecutionActor.CRITIC.value, prompt=prompt, output_type=CriticCompactOutput,
        context={}, tools=[], remaining_token_budget=100000, output_validator=validate,
    )
    assert len(prompts) == 3
    assert "Copy IDs exactly" in prompts[1]
    assert "final_decision as an object" in prompts[2]
    assert "assessment_evidence_reference;final_decision_required" in prompts[2]
    assert result.output["final_decision"]["action"] == "inconclusive"
    assert runtime.remaining_token_budget == 98200


@pytest.mark.anyio
@pytest.mark.parametrize("unknown_check", ["mechanism", "symptom_vs_cause"])
async def test_critic_repairs_conclusion_with_its_own_unresolved_mechanism(
    monkeypatch, unknown_check,
):
    repository, record = _seed_repository()
    candidate = RootCauseCandidate(
        id="candidate-gap", rank=1, summary="Resource increased", confidence=0.5,
        affected_entity="checkout-service", failure_mechanism="resource exhaustion",
        supporting_evidence_ids=["ev-metric"],
    )
    review = CoordinationReview(
        investigation_id=record.id, runtime_run_id="run-v11", authority_mode="agent",
        diagnosis_contract_revision=2, candidates=[candidate],
    )
    prompts = []

    async def no_sleep(_seconds):
        pass

    async def turn(**kwargs):
        prompts.append(kwargs["prompt"])
        repairing = len(prompts) > 1
        return ({
            "assessments": [{
                "candidate_ref": candidate.id,
                "verdict": "inconclusive" if repairing else "accept",
                "checks": [{
                    "name": name.value,
                    "status": "unknown" if name.value == unknown_check else "pass",
                    "summary": "Capacity unobserved" if name.value == unknown_check else "Observed",
                    "evidence_ids": [] if name.value == unknown_check else ["ev-metric"],
                    "gap": "Missing capacity evidence" if name.value == unknown_check else None,
                } for name in CausalCheckName],
            }], "tasks": [], "final_decision": {
                "action": "inconclusive" if repairing else "conclude",
                "candidate_refs": [] if repairing else [candidate.id],
                "evidence_ids": ["ev-metric"], "summary": "Missing causal discriminator",
                "stop_reason": "Missing capacity evidence" if repairing else None,
            },
        }, {"input_tokens": v11_runtime_module._estimate_model_input(
            kwargs["prompt"], kwargs["context"],
        )[0], "output_tokens": 300})

    monkeypatch.setattr(v11_runtime_module.asyncio, "sleep", no_sleep)
    runtime = V11Runtime(model="fake", turn=turn, token_budget=100000)
    runtime.runtime_run_id = "run-v11"

    def validate(output):
        assessments = runtime._normalize_assessments(
            output.assessments, candidate_ids={candidate.id}, runtime_run_id="run-v11",
            review_round=1, usable_evidence_ids={"ev-metric"},
        )
        runtime._normalize_final_decision(output, review, assessments, repository, record.id)

    result = await runtime._call_model(
        actor=ExecutionActor.CRITIC.value, prompt="Review", output_type=CriticCompactOutput,
        context={}, tools=[], remaining_token_budget=100000, output_validator=validate,
    )
    assert len(prompts) == 2
    assert "final_candidate_unknown_mechanism" in prompts[1]
    assert result.output["final_decision"]["action"] == "inconclusive"


def test_compact_causal_check_allows_bounded_joint_evidence():
    from backend.diagnosis.v11_runtime import CriticCompactCausalCheck

    check = {
        "name": "mechanism", "status": "pass", "summary": "Joint observations",
        "evidence_ids": [f"ev-{i}" for i in range(4)],
    }
    assert len(CriticCompactCausalCheck.model_validate(check).evidence_ids) == 4
    with pytest.raises(ValidationError):
        CriticCompactCausalCheck.model_validate({**check, "evidence_ids": ["ev"] * 5})


def test_final_decision_validation_has_safe_actionable_code():
    from backend.domain.agent_findings import FinalDiagnosisDecision

    with pytest.raises(ValidationError) as error:
        FinalDiagnosisDecision(
            actor="critic", action="inconclusive", candidate_ids=["private-candidate"],
            evidence_ids=[], summary="Insufficient evidence",
        )
    signature = v11_runtime_module._structured_validation_signature(error.value)
    assert signature == "root:inconclusive_shape"
    assert "private-candidate" not in signature


@pytest.mark.parametrize("values", [
    {"action": "conclude", "candidates": [], "evidence_ids": ["ev-1"]},
    {"action": "inconclusive", "stop_reason": None},
])
def test_single_invalid_final_shape_is_rejected_before_domain_publication(values):
    with pytest.raises(ValidationError):
        V11SingleControlOutput.model_validate({"summary": "Observed", **values})


def test_successful_retry_removes_cached_failure_without_hiding_unresolved_failure():
    failed = AgentExecution(
        id="failed", task_id="task", agent_name="InvestigatorAgent", runtime_run_id="run",
        status=AgentExecutionStatus.FAILED, attempt=1,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
    )
    pending_failure = failed.model_copy(update={"id": "unresolved", "task_id": "other-task"})
    executions = [failed, pending_failure]
    repo = SimpleNamespace(list_executions=lambda _: executions)
    runtime = V11Runtime(model="unused")
    runtime.runtime_run_id = "run"
    runtime._restore_failure_memory(repo, "investigation")
    assert set(runtime._failures) == {"failed", "unresolved"}
    executions.append(failed.model_copy(update={
        "id": "retry", "status": AgentExecutionStatus.RUNNING, "attempt": 2,
        "agent_name": "investigator-renamed",
    }))
    runtime._restore_failure_memory(repo, "investigation")
    assert runtime._failures == ["unresolved"]
    executions[-1] = executions[-1].model_copy(update={"status": AgentExecutionStatus.FAILED})
    runtime._restore_failure_memory(repo, "investigation")
    assert set(runtime._failures) == {"failed", "retry", "unresolved"}
    executions.append(failed.model_copy(update={
        "id": "success", "status": AgentExecutionStatus.COMPLETED, "attempt": 3,
    }))
    runtime._restore_failure_memory(repo, "investigation")
    assert runtime._failures == ["unresolved"]


@pytest.mark.anyio
async def test_high_budget_reserve_allows_correction_and_retains_critic_room(monkeypatch):
    runtime = V11Runtime(model="unused", token_budget=200000, max_investigators=3)
    runtime._remaining_token_budget = 71060
    monkeypatch.setattr(v11_runtime_module, "_estimate_model_input", lambda *_: (5596, {}))
    output_cap, estimated_input, reserved = await runtime._reserve_model_budget_once(
        None, "correct with committed evidence", {"tools": []},
        reservation_id="correction:request-1", logical_call_id="correction",
        actor=ExecutionActor.INVESTIGATOR.value, request_args={"tools": []},
    )
    assert estimated_input == 5596
    assert output_cap >= 1024
    assert runtime.remaining_token_budget == 71060 - reserved
    assert runtime.remaining_token_budget >= 2 * (5596 + 16384)


@pytest.mark.anyio
async def test_required_closing_takes_priority_over_optional_second_critic(monkeypatch):
    runtime = V11Runtime(model="unused", token_budget=200000, max_investigators=3)
    runtime._remaining_token_budget = 75381
    runtime._active_sessions = {object(), object()}
    monkeypatch.setattr(v11_runtime_module, "_estimate_model_input", lambda *_: (16023, {}))
    request = {"tools": [], "model_settings": ModelSettings()}
    cap, _, _ = await runtime._reserve_model_budget_once(
        None, "finish the investigation", {"tools": []},
        logical_call_id="closing", reservation_id="closing:request-1",
        actor=ExecutionActor.INVESTIGATOR.value, request_args=request, request_index=99,
    )
    critic_cost = runtime._expected_request_tokens(ExecutionActor.CRITIC.value, input_hint=16023)
    assert cap >= 2048
    assert runtime.remaining_token_budget >= critic_cost + 16023 + 256
    assert "Query tools are now closed" in request["system_instructions"]


@pytest.mark.anyio
async def test_closing_never_borrows_the_last_critic_reservation(monkeypatch):
    runtime = V11Runtime(model="unused", token_budget=200000, max_investigators=3)
    runtime._remaining_token_budget = 35000
    monkeypatch.setattr(v11_runtime_module, "_estimate_model_input", lambda *_: (16023, {}))
    with pytest.raises(V11RuntimeContractError, match="context_budget_exhausted"):
        await runtime._reserve_model_budget_once(
            None, "finish", {"tools": []}, logical_call_id="closing",
            actor=ExecutionActor.INVESTIGATOR.value, request_index=99,
            request_args={"tools": [], "model_settings": ModelSettings()},
        )
    assert runtime.remaining_token_budget == 35000


@pytest.mark.anyio
async def test_investigator_scope_mismatch_enters_correction_before_admission(monkeypatch):
    repository, record = _seed_repository()
    evidence = record.evidence[0].model_copy(update={
        "scope": EvidenceScope(entity_ids=["worker"]),
    })
    repository.save(record.model_copy(update={"evidence": [evidence]}))
    runtime = V11Runtime(
        model="unused", max_investigators=1,
        tool_registry=build_provider_tool_registry(build_mock_provider_registry()),
    )
    runtime.runtime_run_id = "run-v11"

    async def model_call(**kwargs):
        output = InvestigatorCandidateOutput.model_validate({"candidates": [{
            "affected_entity": "worker -> cache connection",
            "failure_class": "network_connection_failure",
            "failure_mechanism": "Worker outbound connection fails",
            "supporting_evidence_ids": [evidence.id],
        }]})
        with pytest.raises(ClassifiedRetryableError) as error:
            kwargs["output_validator"](output)
        assert error.value.audit_code == "scope_entity_mismatch"
        output.candidates[0].affected_entity = "worker"
        kwargs["output_validator"](output)
        return SimpleNamespace(output=output.model_dump(), execution_id="repaired")

    monkeypatch.setattr(runtime, "_call_model", model_call)
    task = DiagnosisTask(
        id="task-scope", title="Locate connection failure", description="Inspect RPC evidence",
        information_gap="Causal endpoint", task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
        agent_name="InvestigatorAgent", runtime_run_id="run-v11", analysis_round=1,
    )
    result = await runtime._run_investigator(
        repository=repository, investigation_id=record.id, event=record.event,
        task=task, round_number=1, seed_evidence=[evidence], own_findings=[],
    )
    assert len(result.candidates) == 1
    assert result.candidates[0].affected_entity == "worker"
    assert not result.audit_executions


def test_critic_preserves_candidate_resource_controls_alongside_global_signals():
    review = CoordinationReview(
        investigation_id="inv-controls", candidates=[RootCauseCandidate(
            id="candidate-controls", summary="resource hypothesis", rank=1, confidence=0.7,
            affected_entity="worker", supporting_evidence_ids=["ev-disk"],
        )],
    )
    evidence = []
    for entity in ["worker", "other-a", "other-b", "other-c", "other-d"]:
        for family in ["disk_io", "cpu", "memory", "traffic", "latency"]:
            evidence.append(EvidenceItem(
                id="ev-disk" if entity == "worker" and family == "disk_io"
                   else f"ev-{entity}-{family}",
                provider=EvidenceProvider.METRIC, kind=EvidenceKind.METRIC_TREND,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC), summary="measured resource",
                payload={"entity": entity, "signal_type": family,
                         "change_score": 1 if entity == "worker" else 1000},
                runtime_run_id="run-controls",
            ))
    selected = v11_runtime_module._usable_critic_evidence(
        review, (), evidence, runtime_run_id="run-controls",
    )
    ids = {e.id for e in selected}
    assert {"ev-disk", "ev-worker-cpu", "ev-worker-memory", "ev-worker-traffic"} <= ids
    assert any(e.payload["entity"] != "worker" for e in selected)
    assert len(selected) <= 11


def test_small_digest_keeps_multi_resource_profiles_when_many_entities_compete():
    evidence = []
    for entity in range(5):
        for family in ["cpu", "memory", "disk_io", "latency"]:
            evidence.append(EvidenceItem(
                id=f"ev-profile-{entity}-{family}", provider=EvidenceProvider.METRIC,
                kind=EvidenceKind.METRIC_TREND, timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                summary="resource observation", payload={
                    "entity": f"worker-{entity}", "signal_type": family,
                    "change_score": 100 - entity,
                },
            ))
    selected = _select_evidence_digest(evidence, max_per_kind=8, max_total=10)
    assert len(selected) <= 10
    for entity in {e.payload["entity"] for e in selected}:
        assert {e.payload["signal_type"] for e in selected
                if e.payload["entity"] == entity} >= {"cpu", "memory", "disk_io"}
    assert len({e.payload["entity"] for e in selected}) == 2
