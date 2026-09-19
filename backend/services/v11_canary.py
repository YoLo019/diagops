"""V11 label-blind workflow capability observation.

该模块只验证生产 Runtime 的协议和持久化边界，不读取评估输入，也不计算
诊断效果。fake endpoint 实现的是 Agents SDK 的 Chat Completions Model 边界，
因此 canary 保持 ``V11Runtime.turn is None``，与正式 compact workflow 走同一
模型、工具、预算和 phase 入口。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import openai

from backend.config.settings import AppSettings, endpoint_id, load_settings
from backend.db.models import InvestigationRecord
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.diagnostic_skills import skill_catalog_identity
from backend.diagnosis.openai_compatible_model import (
    OpenAICompatibleChatCompletionsModel,
)
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.diagnosis.v11_runtime import V11Runtime
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import (
    CausalCheckName,
    InvestigationStrategy,
    ModelProvider,
)
from backend.domain.runtime import (
    V11_DEFAULT_MAX_INVESTIGATORS,
    V11_DEFAULT_MAX_ROUNDS,
    V11_DEFAULT_MAX_TURNS,
    V11_DEFAULT_TOOL_BUDGET,
    V11_MULTI_TOPOLOGY_MODE,
    V11_RUN_DEADLINE_MAX_SECONDS,
    AuthorityMode,
    ExecutionContractVersion,
    RuntimeEventType,
    RuntimePhase,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    V11ExecutionContractInput,
    build_v11_execution_contract,
)
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.faults import NoFaultInjector
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import V11_PHASE_ORDER
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.writer import RuntimeWriter
from backend.tools.provider_tools import build_provider_tool_registry

_CANARY_ENDPOINT = "http://127.0.0.1:8000/v1"
_CANARY_MODEL_NAME = "v11-local-canary-model"
_CANARY_PROMPT_VERSION = "v11-local-canary"
_CANARY_INVESTIGATOR_COUNT = min(2, V11_DEFAULT_MAX_INVESTIGATORS)
_CANARY_EXECUTION_TIMEOUT = 30.0
_FORBIDDEN_REQUEST_MARKERS = (
    "benchmark",
    "ground truth",
    "scoring",
    "injection marker",
)


def _json_objects(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """解析 fake 请求中的服务端 JSON 投影，不保留或输出原始文本。"""
    objects: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _walk_values(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)


def _candidate_ref(messages: list[dict[str, Any]]) -> str:
    for root in _json_objects(messages):
        for value in _walk_values(root):
            for key in ("candidate_refs", "candidate_ids"):
                refs = value.get(key)
                if isinstance(refs, list):
                    for candidate in refs:
                        if isinstance(candidate, str) and candidate:
                            return candidate
            candidate = value.get("candidate_ref")
            if isinstance(candidate, str) and candidate:
                return candidate
            candidates = value.get("candidates")
            if isinstance(candidates, list):
                for item in candidates:
                    if isinstance(item, dict):
                        candidate = item.get("id")
                        if isinstance(candidate, str) and candidate:
                            return candidate
    return "missing-candidate-ref"


def _round_number(messages: list[dict[str, Any]]) -> int:
    for root in _json_objects(messages):
        for value in _walk_values(root):
            round_number = value.get("round")
            if isinstance(round_number, int):
                return round_number
    return 1


def _checks(*, invalid_evidence: bool = False, full: bool = False) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for index, name in enumerate(CausalCheckName):
        if invalid_evidence and index == 0:
            values.append(
                {
                    "name": name.value,
                    "status": "pass",
                    "evidence_ids": ["missing-evidence"],
                    **({"summary": "pass"} if full else {}),
                }
            )
        else:
            values.append(
                {
                    "name": name.value,
                    "status": "unknown",
                    "evidence_ids": [],
                    "gap": "bounded-gap",
                    **({"summary": "unknown"} if full else {}),
                }
            )
    return values


@dataclass(slots=True)
class _CanaryObservation:
    """只保存可验证的计数和 phase 身份，不保存模型请求正文。"""

    phase_inputs: list[Any] = field(default_factory=list)
    investigator_peak: int = 0
    investigator_active: int = 0
    round1_barrier: asyncio.Barrier | None = None
    round1_started: int = 0
    tool_call_count: int = 0
    schema_requests: list[str] = field(default_factory=list)
    compact_critic_responses: int = 0
    reconciliation_responses: int = 0
    critic_invalid_first: bool = False
    critic_correction_needs_evidence: bool = False
    fault_injector: Any = field(default_factory=NoFaultInjector)


class _ObservedFaultInjector:
    """记录测试故障是否真的触发，预算和状态仍由生产组件维护。"""

    def __init__(self, delegate=None) -> None:
        self._delegate = delegate or NoFaultInjector()
        self.triggered = False

    def hit(self, point: str) -> None:
        try:
            self._delegate.hit(point)
        except Exception:
            self.triggered = True
            raise


class _CanaryCompatibleModel(OpenAICompatibleChatCompletionsModel):
    """使用真实 compatible adapter、但由 httpx MockTransport 提供响应。"""

    def __init__(
        self,
        observation: _CanaryObservation,
        *,
        model_name: str = _CANARY_MODEL_NAME,
    ) -> None:
        super().__init__(
            model=model_name,
            api_key="local-canary-key",
            base_url=_CANARY_ENDPOINT,
            timeout_seconds=5.0,
            max_retries=0,
        )
        self._observation = observation
        self._critic_attempt = 0
        self._tool_emitted = False
        self._candidate_emitted = False

    def clone_for_model(
        self,
        model_name: str,
        *,
        max_retries: int | None = None,
    ) -> _CanaryCompatibleModel:
        """保留 clone_for_run 的运行期观测状态与 fake transport。"""
        del max_retries
        return type(self)(self._observation, model_name=model_name)

    def _create_client(self) -> openai.AsyncOpenAI:
        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            messages = body.get("messages", [])
            request_text = json.dumps(body, ensure_ascii=False).lower()
            if any(marker in request_text for marker in _FORBIDDEN_REQUEST_MARKERS):
                raise RuntimeError("canary request contains a forbidden marker")
            response_format = body.get("response_format") or {}
            schema = (response_format.get("json_schema") or {}).get("schema")
            schema = schema if isinstance(schema, dict) else {}
            definitions = schema.get("$defs", {})
            definition_names = set(definitions) if isinstance(definitions, dict) else set()
            is_lead = "LeadPlanningCompactTaskDraft" in definition_names
            is_investigator = "InvestigatorCandidateDraft" in definition_names and not (
                "CriticCompactAssessmentDraft" in definition_names
                or "CriticAssessmentDraft" in definition_names
            )
            is_compact_critic = "CriticCompactAssessmentDraft" in definition_names
            is_full_critic = "CriticAssessmentDraft" in definition_names
            observation = self._observation
            if is_lead:
                observation.schema_requests.append("LeadPlanningCompactOutput")
            elif is_investigator:
                observation.schema_requests.append(
                    schema.get("title", "InvestigatorCandidateOutput")
                )
            elif is_compact_critic:
                observation.schema_requests.append("CriticCompactOutput")
            elif is_full_critic:
                observation.schema_requests.append("CriticOutput")
            has_tool_result = any(
                isinstance(message, dict) and message.get("role") == "tool" for message in messages
            )

            if is_investigator and _round_number(messages) == 1 and not has_tool_result:
                observation = self._observation
                observation.round1_started += 1
                observation.investigator_active += 1
                observation.investigator_peak = max(
                    observation.investigator_peak, observation.investigator_active
                )
                try:
                    if observation.investigator_active >= 2:
                        observation.fault_injector.hit("parallel_session_failure")
                    barrier = observation.round1_barrier
                    if barrier is None:
                        raise RuntimeError("round one investigator barrier is missing")
                    await asyncio.wait_for(barrier.wait(), timeout=5.0)
                finally:
                    observation.investigator_active -= 1

            if is_investigator and not has_tool_result and not self._tool_emitted:
                # 仅一个真实 investigator 请求先调用只读工具；后续 SDK 请求
                # 会携带 tool result，再由同一 Agent 收口结构化候选。
                self._tool_emitted = True
                payload = {
                    "id": "canary-tool-call",
                    "object": "chat.completion",
                    "created": 1,
                    "model": _CANARY_MODEL_NAME,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "canary-tool-call",
                                        "type": "function",
                                        "function": {
                                            "name": "read_logs",
                                            "arguments": json.dumps(
                                                {"reason": "inspect bounded signal"}
                                            ),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
                return httpx.Response(200, request=request, json=payload)

            if is_lead:
                output = {
                    "decision": {
                        "action": "investigate",
                        "summary": "Inspect two bounded signals.",
                        "selected_skills": [],
                    },
                    "tasks": [
                        {
                            "title": "Inspect primary signal",
                            "description": "Inspect committed read-only evidence.",
                            "information_gap": "primary signal",
                            "expected_discriminator": None,
                        },
                        {
                            "title": "Inspect secondary signal",
                            "description": "Inspect an independent bounded signal.",
                            "information_gap": "secondary signal",
                            "expected_discriminator": None,
                        },
                    ],
                }
            elif is_investigator:
                ids: list[str] = []
                for root in _json_objects(messages):
                    for value in _walk_values(root):
                        candidate_ids = value.get("own_committed_evidence_ids")
                        if isinstance(candidate_ids, list):
                            ids.extend(item for item in candidate_ids if isinstance(item, str))
                if _round_number(messages) == 2 or self._candidate_emitted:
                    output = {"candidates": []}
                else:
                    self._candidate_emitted = True
                    output = {
                        "candidates": [
                            {
                                "affected_entity": "payment-service",
                                "failure_class": "resource_saturation",
                                "failure_mechanism": "bounded resource signal",
                                "supporting_evidence_ids": ids[:1],
                                "contradicting_evidence_ids": [],
                            }
                        ]
                    }
            elif is_compact_critic and _round_number(messages) == 1:
                self._critic_attempt += 1
                observation.compact_critic_responses = self._critic_attempt
                candidate = _candidate_ref(messages)
                if self._critic_attempt == 1:
                    # 首次 response 故意使用越界 candidate_ref，验证生产的
                    # bounded correction；该值不会进入任何持久化投影。
                    output = {
                        "assessments": [
                            {
                                "candidate_ref": "invalid-candidate-ref",
                                "verdict": "inconclusive",
                                "checks": _checks(invalid_evidence=True),
                            }
                        ],
                        "tasks": [],
                    }
                    observation.critic_invalid_first = True
                else:
                    output = {
                        "assessments": [
                            {
                                "candidate_ref": candidate,
                                "verdict": "needs_evidence",
                                "checks": _checks(),
                                "gap": "missing corroboration",
                                "supplemental_task_ids": ["canary-supplemental"],
                            }
                        ],
                        "tasks": [
                            {
                                "id": "canary-supplemental",
                                "title": "Inspect supplemental signal",
                                "description": "Inspect one bounded supplemental signal.",
                                "information_gap": "missing corroboration",
                                "expected_discriminator": None,
                            }
                        ],
                    }
                    observation.critic_correction_needs_evidence = True
            elif is_compact_critic or is_full_critic:
                observation.reconciliation_responses += 1
                candidate = _candidate_ref(messages)
                output = {
                    "summary": "Reconciled bounded evidence.",
                    "assessments": [
                        {
                            "candidate_ref": candidate,
                            "verdict": "inconclusive",
                            "checks": _checks(full=True),
                            "supporting_evidence_ids": [],
                            "contradicting_evidence_ids": [],
                            "gap": None,
                            "supplemental_task_ids": [],
                            "summary": "Evidence remains insufficient.",
                        }
                    ],
                    "tasks": [],
                }
                if is_compact_critic:
                    output.pop("summary")
                    for assessment in output["assessments"]:
                        for field_name in (
                            "summary", "supporting_evidence_ids", "contradicting_evidence_ids"
                        ):
                            assessment.pop(field_name)
            else:
                raise RuntimeError("canary received an unknown structured schema")

            if is_compact_critic or is_full_critic:
                needs_evidence = any(
                    item["verdict"] == "needs_evidence" for item in output["assessments"]
                )
                output["final_decision"] = (
                    None
                    if needs_evidence
                    else {
                        "action": "inconclusive",
                        "candidate_refs": [],
                        "evidence_ids": [],
                        "summary": "Evidence remains insufficient.",
                        "stop_reason": "bounded-gap",
                    }
                )
            payload = {
                "id": "canary-response",
                "object": "chat.completion",
                "created": 1,
                "model": _CANARY_MODEL_NAME,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(output, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
            return httpx.Response(200, request=request, json=payload)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_CANARY_ENDPOINT
        )
        return openai.AsyncOpenAI(
            api_key="local-canary-key",
            base_url=_CANARY_ENDPOINT,
            max_retries=0,
            http_client=client,
        )


def _build_canary_contract(registry, settings: AppSettings) -> dict[str, Any]:
    """从共享 provider-neutral builder 构造与产品同形的 sealed contract。"""
    agent_settings = settings.agents
    return build_v11_execution_contract(
        V11ExecutionContractInput(
            model_provider=ModelProvider.OPENAI_COMPATIBLE.value,
            model_name=_CANARY_MODEL_NAME,
            prompt_version=_CANARY_PROMPT_VERSION,
            api_mode="chat_completions",
            endpoint_id=endpoint_id(_CANARY_ENDPOINT),
            capability_artifact_hash="0" * 64,
            structured_output_transport="native_json_schema",
            tool_manifest=registry.agent_manifest(),
            skill_catalog=skill_catalog_identity(registry.list_agent_specs()),
            max_turns=getattr(agent_settings, "max_turns", V11_DEFAULT_MAX_TURNS),
            # 契约保留正式 Multi 的并发上限 3；fixture 只发布两个任务，
            # 用 barrier 观测实际峰值至少 2 而不额外消耗 model turn。
            max_investigators=V11_DEFAULT_MAX_INVESTIGATORS,
            max_rounds=V11_DEFAULT_MAX_ROUNDS,
            token_budget=agent_settings.token_budget,
            max_tool_calls_per_specialist=agent_settings.max_tool_calls_per_specialist,
            tool_timeout_seconds=agent_settings.tool_timeout_seconds,
            tool_budget=getattr(agent_settings, "max_total_tool_calls", V11_DEFAULT_TOOL_BUDGET),
            timeout_seconds=min(
                float(agent_settings.timeout_seconds), V11_RUN_DEADLINE_MAX_SECONDS
            ),
            topology_mode=V11_MULTI_TOPOLOGY_MODE,
        )
    )


async def run_v11_local_workflow_canary(
    *,
    settings: AppSettings | None = None,
    fault_injector=None,
) -> bool:
    """运行真实 V11 phase/Store 边界并返回 label-blind capability observation。"""
    effective_settings = settings if settings is not None else load_settings()
    if settings is None:
        # 离线 canary 验证完整链路，使用足够的合成 token 额度；真实评测预算另测。
        effective_settings = effective_settings.model_copy(
            update={"agents": effective_settings.agents.model_copy(update={"token_budget": 100000})}
        )
    repository: SQLiteInvestigationRepository | None = None
    engine = None
    temporary_root = TemporaryDirectory(prefix="diagops-v11-canary-")
    coordinator: RuntimeCoordinator | None = None
    observed_faults = _ObservedFaultInjector(fault_injector)
    try:
        database_path = Path(temporary_root.name) / "runtime.db"
        engine = create_db_engine(f"sqlite:///{database_path.as_posix()}")
        initialize_database(engine)
        repository = SQLiteInvestigationRepository(engine)
        providers = build_mock_provider_registry()
        tool_registry = build_provider_tool_registry(providers)
        event = IncidentEvent(
            source=IncidentSource.SIMULATED,
            service="payment-service",
            environment="synthetic",
            severity=Severity.WARNING,
            title="Synthetic compatibility signal",
            description="Bounded read-only signal for workflow observation.",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            signals={"cpu": "high"},
        )
        record = repository.save(
            InvestigationRecord(id="v11-local-canary-investigation", event=event)
        )
        observation = _CanaryObservation()
        observation.fault_injector = observed_faults
        observation.round1_barrier = asyncio.Barrier(_CANARY_INVESTIGATOR_COUNT)
        model = _CanaryCompatibleModel(observation)
        contract = _build_canary_contract(tool_registry, effective_settings)
        limits = contract["limits"]
        runtime = V11Runtime(
            model=model,
            model_provider=ModelProvider.OPENAI_COMPATIBLE,
            model_name=_CANARY_MODEL_NAME,
            tool_registry=tool_registry,
            turn=None,
            max_turns=int(limits["max_turns"]),
            timeout_seconds=float(contract["timeout_seconds"]),
            max_investigators=int(limits["max_investigators"]),
            max_rounds=int(limits["max_rounds"]),
            max_total_tool_calls=int(contract["tool_budget"]),
            max_tool_calls_per_specialist=int(limits["max_tool_calls_per_specialist"]),
            token_budget=int(contract["token_budget"]),
            tool_timeout_seconds=float(limits["tool_timeout_seconds"]),
        )
        if runtime.turn is not None or not isinstance(
            runtime.model, OpenAICompatibleChatCompletionsModel
        ):
            return False
        store = SQLiteRuntimeStore(
            engine,
            repository,
            lease_seconds=max(5, effective_settings.runtime.lease_seconds),
            fault_injector=observed_faults,
        )
        run = RuntimeRun.create_new(
            id="v11-local-canary-run",
            investigation_id=record.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            model_provider=ModelProvider.OPENAI_COMPATIBLE,
            model_name=_CANARY_MODEL_NAME,
            prompt_version=_CANARY_PROMPT_VERSION,
            tool_budget=int(contract["tool_budget"]),
            token_budget=int(contract["token_budget"]),
            timeout_seconds=float(contract["timeout_seconds"]),
            execution_contract_version=ExecutionContractVersion.V11,
            authority_mode=AuthorityMode.AGENT,
            execution_contract=contract,
        )
        store.create_run(run)

        orchestrator = DiagnosisOrchestrator(
            repository,
            providers,
            RcaAnalyzer(),
            ReportGenerator(),
            coordinator=DiagnosisCoordinator(providers),
            action_planner=ActionPlanner(),
            agents_runtime=None,
            v11_runtime=runtime,
            default_strategy=InvestigationStrategy.ADAPTIVE,
            max_tool_calls_per_specialist=int(limits["max_tool_calls_per_specialist"]),
            max_total_tool_calls=int(contract["tool_budget"]),
        )
        phase_executor = DiagnosisPhaseExecutor(
            orchestrator,
            # Runtime 内部 Investigator gate 仍由 frozen topology（上限 3）约束；
            # canary topology 只选两个 task 以在共享 turn budget 内覆盖真实 tool loop。
            max_parallel_steps_per_run=V11_DEFAULT_MAX_INVESTIGATORS,
        )
        original_execute_phase = phase_executor.execute_phase

        async def capture_phase(phase_input):
            observation.phase_inputs.append(phase_input)
            return await original_execute_phase(phase_input)

        phase_executor.execute_phase = capture_phase  # type: ignore[method-assign]
        writer = RuntimeWriter(store)
        coordinator = RuntimeCoordinator(
            store=store,
            writer=writer,
            phase_executor=phase_executor,
            heartbeat_seconds=1,
            fault_injector=observed_faults,
        )
        final = await asyncio.wait_for(
            coordinator.execute(run.id, "v11-local-canary-owner"),
            timeout=_CANARY_EXECUTION_TIMEOUT,
        )

        if observed_faults.triggered or final.status.value != "completed":
            return False
        persisted = store.get_run(run.id)
        if persisted.execution_contract != contract:
            return False
        # 重新从同一 SQLite 持久层读取，确认 reservation 不是只存在于
        # RuntimeCoordinator 或 canary 的内存副本。
        reloaded = SQLiteRuntimeStore(engine, repository).get_run(run.id)
        if reloaded.remaining_model_turns != persisted.remaining_model_turns:
            return False
        if not observation.phase_inputs or any(
            item.execution_contract_version != ExecutionContractVersion.V11
            or item.execution_contract != contract
            or item.run_id != run.id
            for item in observation.phase_inputs
        ):
            return False
        if [item.phase for item in observation.phase_inputs] != list(V11_PHASE_ORDER):
            return False
        if (
            "LeadPlanningCompactOutput" not in observation.schema_requests
            or "InvestigatorCandidateOutput" not in observation.schema_requests
            or observation.compact_critic_responses < 2
            or observation.reconciliation_responses < 1
            or "CriticOutput" in observation.schema_requests
            or not observation.critic_invalid_first
            or not observation.critic_correction_needs_evidence
            or "InvestigatorOutput" not in observation.schema_requests
            or "LeadPlanningOutput" in observation.schema_requests
        ):
            return False
        # reservation 只从 SQLite 已落盘的 MODEL_STARTED 事件读取，不能由
        # canary 影子计数器冒充；每个事件都由 RuntimeCoordinator 注入的
        # Store reservation 回调产生。
        initial_turns = int(limits["max_turns"])
        model_events = store.list_events(run.id, limit=10_000)
        persisted_reservation_remainders = [
            int(event.safe_payload["remaining_model_turns"])
            for event in model_events
            if event.event_type == RuntimeEventType.MODEL_STARTED
            and isinstance(event.safe_payload, dict)
            and isinstance(event.safe_payload.get("remaining_model_turns"), int)
        ]
        if not persisted_reservation_remainders or any(
            left != initial_turns - index
            for index, left in enumerate(persisted_reservation_remainders, start=1)
        ):
            return False
        if persisted.remaining_model_turns != (
            initial_turns - len(persisted_reservation_remainders)
        ):
            return False

        calls = repository.list_tool_calls(record.id)
        owned_calls = [item for item in calls if item.runtime_run_id == run.id]
        terminal_calls = [item for item in owned_calls if item.status.value != "pending"]
        logical_calls = {item.logical_call_id or item.id for item in terminal_calls}
        observation.tool_call_count = len(logical_calls)
        if observation.tool_call_count != 1 or not any(
            item.status.value == "success" and bool(item.output_evidence_ids)
            for item in terminal_calls
        ):
            return False
        round1_checkpoint = next(
            (
                checkpoint
                for checkpoint in store.list_checkpoints(run.id)
                if checkpoint.completed_phase == RuntimePhase.INVESTIGATOR_ROUND_1
            ),
            None,
        )
        if round1_checkpoint is None or round1_checkpoint.resume_state.remaining_tool_budget != (
            int(contract["tool_budget"]) - observation.tool_call_count
        ):
            return False
        if observation.investigator_peak < 2 or observation.investigator_peak > 3:
            return False
        if observation.round1_started < 2:
            return False

        review = repository.get_coordination_review(record.id)
        if review is None or review.diagnostic_status is None:
            return False
        persisted_record = repository.get(record.id)
        if (
            review.runtime_run_id != run.id
            or persisted_record.multi_agent_run is None
            or persisted_record.multi_agent_run.runtime_run_id != run.id
        ):
            return False
        round_two_tasks = [
            item for item in repository.list_tasks(record.id) if item.analysis_round == 2
        ]
        if (
            len(round_two_tasks) != 1
            or round_two_tasks[0].runtime_run_id != run.id
            or round_two_tasks[0].critic_assessment_id is None
            or not any(item.review_round == 2 for item in review.critic_assessments)
            or not any(item.verdict.value == "inconclusive" for item in review.critic_assessments)
        ):
            return False
        executions = repository.list_executions(record.id)
        critic_round_one = [
            item
            for item in executions
            if item.agent_name == "CriticAgent"
            and getattr(item.step_kind, "value", None) == "critic_review"
            and item.analysis_round == 1
        ]
        critic_round_one.sort(key=lambda item: item.attempt)
        if len(critic_round_one) != 2 or [item.status.value for item in critic_round_one] != [
            "failed",
            "completed",
        ]:
            return False
        if critic_round_one[0].failure_category.value != "invalid_output":
            return False
        if not any(
            item.agent_name == "CriticAgent"
            and getattr(item.step_kind, "value", None) == "critic_review"
            and item.analysis_round == 2
            and item.status.value == "completed"
            for item in executions
        ) or not any(
            item.agent_name == "LeadAgent"
            and getattr(item.step_kind, "value", None) == "lead_adjudication"
            and item.status.value == "completed"
            for item in executions
        ):
            return False
        if not any(
            item.completed_phase == RuntimePhase.CRITIC_RECONCILIATION
            for item in store.list_checkpoints(run.id)
        ):
            return False
        # 所有生产投影必须绑定同一 run；这也覆盖 supplemental task owner。
        if any(
            getattr(value, "runtime_run_id", None) != run.id
            for value in (
                *repository.list_tasks(record.id),
                *repository.list_executions(record.id),
                *repository.list_agent_findings(record.id),
                *repository.list_tool_calls(record.id),
                *persisted_record.evidence,
            )
        ):
            return False
        return True
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception:
        return False
    finally:
        if coordinator is not None:
            try:
                await coordinator.shutdown()
            except Exception:
                pass
        if engine is not None:
            engine.dispose()
        temporary_root.cleanup()


# 旧认证调用面使用该名称；实现只保留一个 canary。
_synthetic_v11_workflow_canary = run_v11_local_workflow_canary


__all__ = ["run_v11_local_workflow_canary", "_synthetic_v11_workflow_canary"]
