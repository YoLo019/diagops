"""RCAEval 正式 prediction runner 与冻结 artifact 契约。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from backend.benchmarks.rcaeval.dependency import scorer_dependency_hash
from backend.benchmarks.rcaeval.isolation import parse_canonical_checksum_bytes
from backend.benchmarks.rcaeval.ledger import (
    canonical_locator,
    volume_identity,
)
from backend.benchmarks.rcaeval.models import (
    CandidatePrediction,
    CasePrediction,
    EndpointCapabilityIdentity,
    EvaluationBudget,
    FrozenRunIdentity,
    PredictionBundle,
    RcaEvalConfiguration,
    RuntimeCaseEntry,
    RuntimeManifest,
)
from backend.benchmarks.rcaeval.providers import (
    build_rcaeval_providers,
    incident_event_for_case,
)
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.diagnosis.adaptive_tools import AdaptiveToolSession
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.diagnostic_skills import skill_catalog_identity
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.diagnosis.v11_runtime import (
    InvestigatorOutput,
    LeadPlanningOutput,
    LeadTaskDraft,
    V11Runtime,
    V11RuntimeContractError,
    _InvestigatorResult,
)
from backend.domain.agent_findings import CoordinationReview
from backend.domain.agent_plan import (
    AgentExecution,
    AgentExecutionStatus,
    DiagnosisTask,
    DiagnosisTaskType,
    LeadDecision,
)
from backend.domain.evidence import EvidenceStatus
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    DiagnosticStatus,
    ExecutionActor,
    ExecutionContractVersion,
    ExecutionStepKind,
    InvestigationStrategy,
    LeadAction,
    ModelProvider,
    MultiAgentRunStatus,
)
from backend.domain.runtime import (
    RuntimeEventType,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
    execution_contract_digest,
    seal_v11_execution_contract,
    validate_v11_execution_contract,
)
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.sqlite_store import SQLiteRuntimeStore
from backend.runtime.telemetry import RuntimeTelemetry
from backend.runtime.writer import RuntimeWriter
from backend.services.source_identity import resolve_source_identity
from backend.services.v11_projection import ensure_v11_projection_owner
from backend.tools.provider_tools import VerifiedMemoryLookup, build_provider_tool_registry
from backend.tools.registry import agent_manifest_hash

PROMPT_VERSION = "v11-rcaeval-v1"
EMPTY_MEMORY_IDENTITY = {"schema_version": "empty-run-owned-memory-v1", "entries": []}
RETRY_POLICY = {
    "max_retries": 1,
    "retryable_categories": ["transport", "rate_limit"],
    "provider_max_retries": 0,
    "sdk_max_retries": 0,
}


class SingleControlOutput(BaseModel):
    """Single control's planning and investigation result in one model context."""

    model_config = ConfigDict(extra="forbid")

    planning: LeadPlanningOutput
    investigator: InvestigatorOutput


class EmptyRunOwnedMemory(VerifiedMemoryLookup):
    """正式对照两侧共享的显式空 memory，不接历史 incident repository。"""

    def __init__(self) -> None:
        pass

    def lookup(self, event, query=None):
        del event, query
        return []


class SingleInvestigatorAgent(V11Runtime):
    """冻结单上下文对照；planning 与 investigation 共用一次模型调用。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(max_investigators=1, max_rounds=1, **kwargs)

    async def plan_lead(
        self,
        *,
        repository,
        investigation_id: str,
        event,
        runtime_run_id: str,
        remaining_tool_budget: int,
        remaining_token_budget: int | None,
    ):
        """保留可审计 phase；唯一模型调用在下一 phase 的单一 context 内完成。"""
        del event, remaining_tool_budget
        self.runtime_run_id = runtime_run_id
        self._remaining_token_budget = remaining_token_budget
        self._agent_manifest()
        self._update_summary(repository, investigation_id)
        return None

    async def investigator_round_1(self, *, repository, investigation_id: str, event):
        manifest = self._agent_manifest()
        base_evidence = list(repository.get(investigation_id).evidence)
        task_id = f"single-control-task-{self.runtime_run_id}"
        task = DiagnosisTask(
            id=task_id,
            title="Single control investigation",
            description="Plan and investigate the incident in one model context.",
            task_type=DiagnosisTaskType.GENERAL_INVESTIGATION,
            agent_name=ExecutionActor.INVESTIGATOR.value,
            tool_names=list(manifest),
            execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
            analysis_round=1,
            information_gap="identify the affected service and failure mechanism",
            runtime_run_id=self.runtime_run_id,
        )
        instance_id = "single-investigator"
        selected_skills = [
            f"{skill.name}@{skill.version}" for skill in self.skills
        ]
        # 先写入唯一 owned planning action，确保 skill selection 与 task
        # identity 在首个 tool call 前已经进入 durable runtime projection；
        # 后续同一模型 context 的结构化 planning 会覆盖这份 provisional plan。
        provisional = LeadPlanningOutput(
            decision=LeadDecision(
                action=LeadAction.INVESTIGATE,
                summary="Single control planning action reserved.",
                task_ids=[task_id],
                selected_skills=selected_skills,
            ),
            tasks=[
                LeadTaskDraft(
                    id=task_id,
                    title=task.title,
                    description=task.description,
                    tool_names=list(manifest),
                    information_gap=task.information_gap,
                )
            ],
        )
        repository.save_plan(
            self._build_plan(
                provisional,
                investigation_id=investigation_id,
                runtime_run_id=self.runtime_run_id or "",
                manifest=manifest,
            )
        )
        session = AdaptiveToolSession(
            event=event,
            seed_evidence=base_evidence,
            registry=self.tool_registry,
            task_ids={instance_id: task.id},
            max_tool_calls_per_specialist=self.max_tool_calls_per_specialist,
            max_total_tool_calls=self._remaining_tool_budget_for(
                repository, investigation_id
            ),
            tool_timeout_seconds=self.tool_timeout_seconds,
            runtime_run_id=self.runtime_run_id,
            resolve_tool_result=self._resolve_tool_result,
            persist_tool_start=self._persist_tool_start,
            persist_tool_result=self._persist_tool_result,
            check_execution=self._check_execution,
            max_parallel_steps_per_run=1,
            hit_fault=self._hit_fault,
            parallel_limit=self._parallel_limit,
            agent_manifest=manifest,
            remaining_deadline_seconds=self._remaining_deadline_seconds,
        )
        self._active_sessions.add(session)
        try:
            prompt = json.dumps(
                {
                    "role": "single investigator control",
                    "incident": event.model_dump(mode="json"),
                    "task": task.model_dump(mode="json"),
                    "tool_manifest": manifest,
                    "skills": selected_skills,
                    "committed_evidence": [item.id for item in base_evidence],
                    "rule": (
                        "Return planning and investigator fields from this one context. "
                        f"The only task id is {task_id!r}; do not use sibling agents."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            turn = await self._call_model(
                actor=ExecutionActor.INVESTIGATOR.value,
                prompt=prompt,
                output_type=SingleControlOutput,
                context={
                    "incident": event.model_dump(mode="json"),
                    "task": task.model_dump(mode="json"),
                    "tool_manifest": manifest,
                    "selected_skills": selected_skills,
                    "remaining_tool_budget": self._remaining_tool_budget_for(
                        repository, investigation_id
                    ),
                    "single_context": True,
                },
                tools=session.tools_for(instance_id, 1),
                remaining_token_budget=self._remaining_token_budget,
                remaining_tool_budget=self._remaining_tool_budget_for(
                    repository, investigation_id
                ),
                repository=repository,
                investigation_id=investigation_id,
                task_id=task.id,
                step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                analysis_round=1,
            )
            await self._commit_session(repository, investigation_id, session)
            output = self._parse_output(turn.output, SingleControlOutput)
            if output.planning.tasks:
                if len(output.planning.tasks) != 1:
                    raise V11RuntimeContractError(
                        "single control planning must contain its one owned task"
                    )
                output = output.model_copy(
                    update={
                        "planning": output.planning.model_copy(
                            update={
                                "tasks": [
                                    output.planning.tasks[0].model_copy(
                                        update={"id": task_id}
                                    )
                                ],
                                "decision": output.planning.decision.model_copy(
                                    update={"task_ids": [task_id]}
                                ),
                            }
                        )
                    }
                )
            plan = self._build_plan(
                output.planning,
                investigation_id=investigation_id,
                runtime_run_id=self.runtime_run_id or "",
                manifest=manifest,
            )
            repository.save_plan(plan)
            findings = tuple(
                self._finding_from_draft(
                    draft,
                    investigation_id=investigation_id,
                    task=task,
                    instance_id=instance_id,
                    round_number=1,
                    assessment=None,
                    evidence=repository.get(investigation_id).evidence,
                )
                for draft in output.investigator.findings
            )
            candidates = tuple(output.investigator.candidates)
            execution = next(
                (
                    item
                    for item in repository.list_executions(investigation_id)
                    if item.id == turn.execution_id
                ),
                AgentExecution(
                    task_id=task.id,
                    agent_name=instance_id,
                    runtime_run_id=self.runtime_run_id,
                    status=AgentExecutionStatus.COMPLETED,
                    execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
                    analysis_round=1,
                    step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                    model_provider=self.model_provider,
                    model_name=self.model_name,
                ),
            )
            execution = execution.model_copy(
                update={
                    "task_id": task.id,
                    "agent_name": instance_id,
                    "tool_call_ids": [call.id for call in session.tool_calls],
                    "evidence_ids": sorted(session.evidence_ids_for(instance_id, 1)),
                    "summary": output.investigator.summary or "Single control completed.",
                }
            )
            result = _InvestigatorResult(findings, candidates, execution)
            self._persist_investigator_result(repository, investigation_id, result)
            self._persist_candidate_projection(repository, investigation_id, candidates)
            self._completed_rounds = 1
            self._update_summary(repository, investigation_id)
            return findings
        except asyncio.CancelledError:
            self._cleanup_session(session)
            raise
        except Exception as exc:
            self._failures.append(type(exc).__name__)
            failed = self._failed_execution(
                task_id=task.id,
                actor=instance_id,
                step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
                message="single control failed",
                analysis_round=1,
            )
            self._persist_investigator_result(
                repository,
                investigation_id,
                _InvestigatorResult((), (), failed),
            )
            self._mark_terminal_failure(repository, investigation_id, "single control failed")
            return ()
        finally:
            self._cleanup_session(session)

    async def critic_review(self, *, repository, investigation_id: str, event):
        del event
        return repository.get_coordination_review(investigation_id) or self._empty_review(
            repository, investigation_id
        )

    async def investigator_round_2(self, *, repository, investigation_id: str, event):
        del repository, investigation_id, event
        return ()

    async def critic_reconciliation(self, *, repository, investigation_id: str, event):
        del event
        return repository.get_coordination_review(investigation_id)

    async def lead_adjudication(self, *, repository, investigation_id: str, event):
        del event
        return repository.get_coordination_review(investigation_id) or self._empty_review(
            repository, investigation_id
        )

    async def result_validation(self, *, repository, investigation_id: str, event):
        del event
        review = repository.get_coordination_review(investigation_id)
        if review is None:
            review = self._empty_review(repository, investigation_id)
        evidence = {
            item.id: item
            for item in repository.get(investigation_id).evidence
            if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
        }
        ranks = [candidate.rank for candidate in review.candidates]
        if len(review.candidates) > 3 or len(ranks) != len(set(ranks)):
            raise V11RuntimeContractError("single control candidate ranks are invalid")
        for candidate in review.candidates:
            if not candidate.affected_entity or not candidate.failure_mechanism:
                raise V11RuntimeContractError("single control candidate is incomplete")
            references = [
                *candidate.supporting_evidence_ids,
                *candidate.contradicting_evidence_ids,
            ]
            if not candidate.supporting_evidence_ids or not set(references) <= set(evidence):
                raise V11RuntimeContractError(
                    "single control candidate references unusable evidence"
                )
        # 公共 CoordinationReview 的 conclude 语义要求 Critic accept；单 Agent
        # 明确没有 Critic，因此 durable 产品投影以 inconclusive 收口，benchmark
        # prediction 只读取上面机械校验过的同 run candidates。
        decision = LeadDecision(
            action=LeadAction.INCONCLUSIVE,
            summary="Frozen single-investigator control validation completed.",
            stop_reason="single_control_no_critic",
        )
        review = review.model_copy(
            update={
                "lead_decision": decision,
                "diagnostic_status": DiagnosticStatus.INCONCLUSIVE,
                "run_status": MultiAgentRunStatus.COMPLETED,
                "stop_reason": decision.stop_reason,
                "summary": decision.summary,
            }
        )
        repository.save_coordination_review(review)
        self._update_summary(repository, investigation_id)
        return review


def validate_configuration_set(
    configurations: dict[RcaEvalConfiguration, EvaluationBudget],
) -> None:
    if set(configurations) != set(RcaEvalConfiguration):
        raise ValueError("SS30 requires exactly four configurations")
    for key, value in configurations.items():
        if value.configuration != key:
            raise ValueError("configuration identity mismatch")
    single = configurations[RcaEvalConfiguration.SINGLE_INTENDED]
    multi = configurations[RcaEvalConfiguration.MULTI_INTENDED]
    single_equal = configurations[RcaEvalConfiguration.SINGLE_EQUAL_TOKEN]
    multi_equal = configurations[RcaEvalConfiguration.MULTI_EQUAL_TOKEN]
    if multi.token_budget > single.token_budget * 3:
        raise ValueError("intended Multi token budget exceeds 3x single")
    if single_equal.token_budget != multi_equal.token_budget:
        raise ValueError("equal-token configurations must share one token budget")
    if single_equal.token_budget != single.token_budget * 3:
        raise ValueError("equal-token budget must equal 3x intended single")
    common = {
        (item.max_turns, item.tool_budget, item.timeout_seconds)
        for item in configurations.values()
    }
    if len(common) != 1:
        raise ValueError("configuration tool/turn/deadline identity mismatch")
    # topology 由 configuration 机械派生：Single 一律 one-context (1,1)，
    # Multi 一律 Lead+Investigators+Critic (3,2)；拍平、互换、篡改一律拒绝。
    expected_topology = {
        RcaEvalConfiguration.SINGLE_INTENDED: (1, 1),
        RcaEvalConfiguration.SINGLE_EQUAL_TOKEN: (1, 1),
        RcaEvalConfiguration.MULTI_INTENDED: (3, 2),
        RcaEvalConfiguration.MULTI_EQUAL_TOKEN: (3, 2),
    }
    for key, value in configurations.items():
        if (value.max_investigators, value.max_rounds) != expected_topology[key]:
            raise ValueError("configuration topology limits are not frozen")


class RcaEvalCaseRunner:
    """在共享 SQLite RuntimeStore 上执行一个 opaque RCAEval case。"""

    def __init__(
        self,
        *,
        runtime_package: Path,
        model: object,
        capability: EndpointCapabilityIdentity,
        repository,
        runtime_store: SQLiteRuntimeStore,
        turn=None,
    ) -> None:
        self.runtime_package = runtime_package.resolve()
        self.manifest = RuntimeManifest.model_validate_json(
            (self.runtime_package / "manifest.json").read_text(encoding="utf-8")
        )
        self.model = model
        self.capability = capability
        self.repository = repository
        self.runtime_store = runtime_store
        self.turn = turn

    def run_case(
        self,
        case: RuntimeCaseEntry,
        budget: EvaluationBudget,
    ) -> CasePrediction:
        if case.partition.value not in {"ob30", "ss30", "tt90"}:
            raise ValueError("unsupported RCAEval partition")
        started = perf_counter()
        case_dir = self.runtime_package / "cases" / case.case_id
        event = incident_event_for_case(case_dir, case.case_id)
        strategy = (
            InvestigationStrategy.ADAPTIVE
            if budget.configuration.is_multi
            else InvestigationStrategy.FIXED
        )
        record = self.repository.save(
            InvestigationRecord(
                event=event,
                strategy=strategy,
                runtime_available=True,
            )
        )
        provider = ModelProvider(self.capability.provider)
        run_id = str(uuid4())
        providers = ProviderRegistry(
            build_rcaeval_providers(
                case_dir,
                case_id=case.case_id,
                runtime_manifest_hash=self.manifest.manifest_hash,
                evidence_namespace=run_id,
            )
        )
        memory = EmptyRunOwnedMemory()
        registry = build_provider_tool_registry(providers, memory)
        runtime_type = V11Runtime if budget.configuration.is_multi else SingleInvestigatorAgent
        runtime = runtime_type(
            model=self.model,
            model_provider=provider,
            model_name=self.capability.model,
            tool_registry=registry,
            turn=self.turn,
            max_turns=budget.max_turns,
            timeout_seconds=budget.timeout_seconds,
            max_investigators=budget.max_investigators,
            max_rounds=budget.max_rounds,
            max_total_tool_calls=budget.tool_budget,
            max_tool_calls_per_specialist=min(3, budget.tool_budget),
            token_budget=budget.token_budget,
        ) if budget.configuration.is_multi else runtime_type(
            model=self.model,
            model_provider=provider,
            model_name=self.capability.model,
            tool_registry=registry,
            turn=self.turn,
            max_turns=budget.max_turns,
            timeout_seconds=budget.timeout_seconds,
            max_total_tool_calls=budget.tool_budget,
            max_tool_calls_per_specialist=min(3, budget.tool_budget),
            token_budget=budget.token_budget,
        )
        contract = build_execution_contract(runtime, budget, self.capability)
        run = RuntimeRun.create_new(
            id=run_id,
            investigation_id=record.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=strategy,
            run_reason=RuntimeRunReason.INITIAL,
            model_provider=provider,
            model_name=self.capability.model,
            prompt_version=PROMPT_VERSION,
            tool_budget=budget.tool_budget,
            token_budget=budget.token_budget,
            timeout_seconds=budget.timeout_seconds,
            execution_contract_version=ExecutionContractVersion.V11,
            authority_mode=AuthorityMode.AGENT,
            execution_contract=contract,
        )
        persisted = self.runtime_store.create_run(run)
        _assert_persisted_contract(persisted, contract)
        orchestrator = DiagnosisOrchestrator(
            repository=self.repository,
            providers=providers,
            analyzer=RcaAnalyzer(),
            report_generator=ReportGenerator(),
            coordinator=DiagnosisCoordinator(providers),
            v11_runtime=runtime,
            default_strategy=strategy,
        )
        writer = RuntimeWriter(self.runtime_store)
        coordinator = RuntimeCoordinator(
            store=self.runtime_store,
            writer=writer,
            phase_executor=DiagnosisPhaseExecutor(
                orchestrator,
                telemetry=RuntimeTelemetry.disabled(),
            ),
            telemetry=RuntimeTelemetry.disabled(),
        )

        async def execute() -> None:
            try:
                await coordinator.execute(run.id, owner=f"rcaeval-{run.id}")
            finally:
                await coordinator.shutdown()

        failure_category = None
        try:
            asyncio.run(execute())
        except Exception as exc:
            failure_category = type(exc).__name__
        persisted = self.runtime_store.get_run(run.id)
        _assert_persisted_contract(persisted, contract)
        record = self.repository.get(record.id)
        review = self.repository.get_coordination_review(record.id)
        completed = (
            persisted.status == RuntimeRunStatus.COMPLETED
            and record.status == InvestigationStatus.COMPLETED
        )
        if completed:
            ensure_v11_projection_owner(self.repository, self.runtime_store, record)
        candidates = _published_candidates(
            review,
            single=not budget.configuration.is_multi,
            completed=completed,
        )
        evidence = {
            item.id: item
            for item in record.evidence
            if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
        }
        calls = self.repository.list_tool_calls(record.id)
        read_only_violations = 0
        for call in calls:
            try:
                read_only_violations += int(not registry.get(call.tool_name).read_only)
            except ValueError:
                read_only_violations += 1
        input_tokens, output_tokens = _runtime_token_usage(self.runtime_store, run.id)
        if not completed and failure_category is None:
            failure_category = (
                persisted.failure_category.value
                if persisted.failure_category is not None
                else record.failure_reason or "failed"
            )
        prediction = CasePrediction(
            case_id=case.case_id,
            configuration=budget.configuration,
            completed=completed,
            candidates=[
                CandidatePrediction(
                    affected_service=candidate.affected_entity or "unknown",
                    failure_mechanism=candidate.failure_mechanism or "unknown",
                    evidence_ids=list(candidate.supporting_evidence_ids),
                    onset_window_start=candidate.onset_window_start,
                    onset_window_end=candidate.onset_window_end,
                )
                for candidate in candidates
            ],
            runtime_run_id=run.id,
            execution_contract_hash=execution_contract_digest(contract),
            available_evidence_ids=sorted(evidence),
            evidence_summaries={key: item.summary[:512] for key, item in evidence.items()},
            duration_ms=round((perf_counter() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=len(calls),
            read_only_violations=read_only_violations,
            leakage_violations=0,
            failure_category=failure_category,
        )
        self.runtime_store.set_benchmark_replay_locator(
            run.id,
            {
                "schema_version": 1,
                "benchmark": "rcaeval-re2-v11",
                "case_id": case.case_id,
                "partition": case.partition.value,
                "configuration": budget.configuration.value,
                "runtime_manifest_hash": self.manifest.manifest_hash,
                "execution_contract_hash": prediction.execution_contract_hash,
            },
        )
        return prediction


def build_execution_contract(
    runtime: V11Runtime,
    budget: EvaluationBudget,
    capability: EndpointCapabilityIdentity,
) -> dict[str, object]:
    manifest = runtime.tool_registry.agent_manifest()
    contract = {
        "execution_contract_version": ExecutionContractVersion.V11.value,
        "authority_mode": AuthorityMode.AGENT.value,
        "model_provider": capability.provider,
        "model_name": capability.model,
        "prompt_version": PROMPT_VERSION,
        "api_mode": capability.api_mode,
        "endpoint_id": capability.endpoint_id,
        "capability_artifact_hash": capability.artifact_hash,
        "tool_manifest": list(manifest),
        "tool_manifest_hash": agent_manifest_hash(manifest),
        "skill_catalog": skill_catalog_identity(runtime.tool_registry.list_agent_specs()),
        "capability_identity": {
            "provider": capability.provider,
            "model": capability.model,
            "api_mode": capability.api_mode,
            "endpoint_id": capability.endpoint_id,
            "artifact_hash": capability.artifact_hash,
        },
        "limits": {
            "max_turns": budget.max_turns,
            "model_turn_budget_scope": "run",
            "max_investigators": budget.max_investigators,
            "max_rounds": budget.max_rounds,
            "token_budget": budget.token_budget,
            "max_tool_calls_per_specialist": min(3, budget.tool_budget),
            "tool_timeout_seconds": runtime.tool_timeout_seconds,
        },
        "topology": {
            "mode": (
                "multi_lead_investigators_critic"
                if budget.configuration.is_multi
                else "single_one_context"
            ),
            "one_context": not budget.configuration.is_multi,
            "critic": budget.configuration.is_multi,
            "subagent": False,
            "hidden_model_calls": False,
        },
        "retry_policy": RETRY_POLICY,
        "tool_budget": budget.tool_budget,
        "token_budget": budget.token_budget,
        "timeout_seconds": budget.timeout_seconds,
    }
    return seal_v11_execution_contract(contract)


def freeze_prediction_bundle(bundle: PredictionBundle, output_dir: Path) -> str:
    validate_prediction_bundle(bundle)
    if bundle.bundle_hash:
        raise ValueError("prediction bundle is already frozen")
    output_dir.mkdir(parents=True, exist_ok=False)
    payload = bundle.model_dump(mode="json", exclude={"bundle_hash"})
    digest = _canonical_hash(payload)
    frozen = bundle.model_copy(update={"bundle_hash": digest})
    path = output_dir / "predictions.json"
    path.write_text(
        json.dumps(frozen.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    (output_dir / "SHA256SUMS").write_text(
        f"{checksum}  predictions.json\n", encoding="utf-8"
    )
    return hashlib.sha256((output_dir / "SHA256SUMS").read_bytes()).hexdigest()


def freeze_prediction_set(
    root: Path,
    *,
    expected_configurations: set[RcaEvalConfiguration],
    expected_case_count: int,
    ledger,
) -> str:
    """在全部配置完成后冻结 evaluator 的单一输入根。"""
    raw_root = Path(root).expanduser()
    _reject_reparse_path(raw_root, "prediction set root")
    root_locator = canonical_locator(raw_root)
    root = raw_root.resolve()
    canonical_root = Path(ledger.canonical_root).resolve()
    if canonical_root not in root.parents:
        raise ValueError("prediction set root is outside the canonical custodian root")
    if _contains_reparse_point(root):
        raise ValueError("prediction set rejects symlink/junction entries")
    expected_sides = {configuration.value for configuration in expected_configurations}
    snapshot = ledger.snapshot()
    if snapshot["state"] != "predictions_frozen":
        raise ValueError("prediction set is not frozen by the custodian ledger")
    records = ledger.side_records()
    if set(records) != expected_sides:
        raise ValueError("prediction set side records are incomplete or extra")
    actual_children = {
        path.name for path in root.iterdir() if path.name != "SHA256SUMS"
    }
    if actual_children != expected_sides:
        raise ValueError("prediction set contains extra or missing side directories")
    bundles: dict[RcaEvalConfiguration, PredictionBundle] = {}
    for configuration in expected_configurations:
        path = root / configuration.value / "predictions.json"
        if not path.is_file():
            raise ValueError(f"prediction set is missing {configuration.value}")
        side = records[configuration.value]
        expected_dir = (root / configuration.value).resolve()
        if side["status"] != "completed":
            raise ValueError("prediction set side is not completed in custodian ledger")
        expected_canonical = canonical_locator(expected_dir)
        if (
            not side["output_dir"]
            or side["output_dir"] != expected_canonical
        ):
            raise ValueError("prediction set side output path differs from ledger")
        locator = side.get("output_locator")
        if (
            not locator
            or not Path(locator).is_absolute()
            or locator != expected_canonical
            or side.get("output_volume") != volume_identity(expected_dir)
        ):
            raise ValueError("prediction set side output alias is not canonical")
        _verify_side_directory(expected_dir, side["bundle_hash"])
        bundle = PredictionBundle.model_validate_json(path.read_text(encoding="utf-8"))
        validate_prediction_bundle(bundle)
        if bundle.configuration != configuration or not bundle.bundle_hash:
            raise ValueError("prediction set contains an unfrozen configuration")
        actual_bundle_hash = _canonical_hash(
            bundle.model_dump(mode="json", exclude={"bundle_hash"})
        )
        if bundle.bundle_hash != actual_bundle_hash:
            raise ValueError("prediction set bundle content hash is invalid")
        if bundle.partition.value != snapshot["partition"]:
            raise ValueError("prediction set bundle partition differs from ledger")
        if bundle.identity.runtime_manifest_hash != ledger.runtime_manifest_hash:
            raise ValueError("prediction set runtime identity differs from ledger")
        bundles[configuration] = bundle
    identities = {
        json.dumps(
            bundle.identity.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        for bundle in bundles.values()
    }
    case_sets = {
        tuple(sorted(item.case_id for item in bundle.predictions))
        for bundle in bundles.values()
    }
    if len(identities) != 1 or len(case_sets) != 1:
        raise ValueError("prediction set contains mixed identity or case rows")
    bundle_identity = next(iter(bundles.values())).identity
    expected_pair_identity = _canonical_hash(
        {
            "partition": snapshot["partition"],
            "runtime_manifest_hash": bundle_identity.runtime_manifest_hash,
            "capability_artifact_hash": bundle_identity.capability.artifact_hash,
            "source_revision": bundle_identity.source_commit,
            "source_manifest_hash": bundle_identity.source_manifest_hash,
        }
    )
    lineage_identity = (
        snapshot["lineage_identity_hash"]
        if snapshot["prediction_set_bound"]
        else snapshot["prediction_set_hash"]
    )
    if lineage_identity != expected_pair_identity:
        raise ValueError("prediction set bundle identity differs from ledger lineage")
    case_ids = next(iter(case_sets))
    if len(case_ids) != expected_case_count:
        raise ValueError("prediction set case count is not frozen")
    entries: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("prediction set rejects symlink entries")
        if path.is_file() and path != root / "SHA256SUMS":
            relative = path.relative_to(root).as_posix()
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    # canonical 序列化排序键是 posix 相对路径字符串本身；Windows Path 排序是
    # casefold，跨平台会产生不同字节序，不能作为 canonical 序。
    lines = [f"{digest}  {relative}" for relative, digest in sorted(entries)]
    checksum_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    digest = hashlib.sha256(checksum_bytes).hexdigest()
    ledger.prepare_prediction_set_freeze(
        prediction_set_hash=digest,
        root_locator=root_locator,
    )
    sums_path = root / "SHA256SUMS"
    if sums_path.exists():
        if not sums_path.is_file() or sums_path.read_bytes() != checksum_bytes:
            ledger.invalidate_pair("prediction freeze materialization is inconsistent")
            try:
                sums_path.unlink()
            except OSError:
                pass
            raise ValueError("prediction set checksum marker differs from freeze intent")
    else:
        try:
            with sums_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(checksum_bytes.decode("utf-8"))
        except FileExistsError as exc:
            if not sums_path.is_file() or sums_path.read_bytes() != checksum_bytes:
                ledger.invalidate_pair("prediction freeze materialization raced or changed")
                try:
                    sums_path.unlink()
                except OSError:
                    pass
                raise ValueError("prediction set freeze raced with another custodian") from exc
    return digest


def _verify_side_directory(path: Path, expected_bundle_hash: str | None) -> None:
    if not expected_bundle_hash or len(expected_bundle_hash) != 64:
        raise ValueError("prediction side bundle hash is missing")
    checksum_path = path / "SHA256SUMS"
    if not checksum_path.is_file() or _contains_reparse_point(path):
        raise ValueError("prediction side checksum or directory is invalid")
    actual_checksum_hash = hashlib.sha256(checksum_path.read_bytes()).hexdigest()
    if actual_checksum_hash != expected_bundle_hash:
        raise ValueError("prediction side checksum differs from ledger")
    # M3: side checksum 与 package/root checksum 共用同一严格 parser，
    # 拒绝非 canonical 拼写、duplicate、顺序与集合漂移。
    entries = parse_canonical_checksum_bytes(checksum_path.read_bytes())
    actual_files = {
        file.relative_to(path).as_posix()
        for file in path.rglob("*")
        if file.is_file() and file.name != "SHA256SUMS"
    }
    if set(entries) != actual_files:
        raise ValueError("prediction side checksum file set differs from output")
    for relative, expected in entries.items():
        if hashlib.sha256((path / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("prediction side checksum content changed")


def _contains_reparse_point(root: Path) -> bool:
    if _is_reparse_path(root):
        return True
    for path in root.rglob("*"):
        if _is_reparse_path(path):
            return True
    return False


def _reject_reparse_path(path: Path, what: str) -> None:
    raw = Path(path).expanduser()
    if any(_is_reparse_path(candidate) for candidate in (raw, *raw.parents)):
        raise ValueError(f"{what} rejects symlink/junction entries")


def _is_reparse_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("prediction set path cannot be inspected") from exc
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT (junctions too)


def _canonical_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.expanduser().resolve(strict=False))))


def validate_prediction_bundle(bundle: PredictionBundle) -> None:
    if bundle.budget.configuration != bundle.configuration:
        raise ValueError("prediction bundle budget configuration mismatch")
    ids = [item.case_id for item in bundle.predictions]
    if len(ids) != len(set(ids)):
        raise ValueError("prediction bundle contains duplicate cases")
    if any(item.configuration != bundle.configuration for item in bundle.predictions):
        raise ValueError("prediction bundle contains mixed configurations")
    contract_hashes = {item.execution_contract_hash for item in bundle.predictions}
    if len(contract_hashes) > 1:
        raise ValueError("prediction bundle contains mixed execution identities")
    for item in bundle.predictions:
        if not math.isfinite(item.duration_ms):
            raise ValueError("prediction bundle contains non-finite metrics")
        if item.failure_category and item.completed:
            raise ValueError("completed prediction cannot carry a failure category")


def frozen_run_identity(
    *,
    runtime_manifest_hash: str,
    capability: EndpointCapabilityIdentity,
    tool_manifest_hash_value: str,
    skill_catalog_hash_value: str,
    repository_root: Path,
) -> FrozenRunIdentity:
    repository_root = repository_root.resolve()
    source_identity = resolve_source_identity(repository_root)
    if source_identity.git_dirty:
        raise ValueError("formal prediction source must be clean")
    backend_root = repository_root / "backend"
    lock_paths = [repository_root / "uv.lock", repository_root / "pyproject.toml"]
    # H6: dependency lock identity 必须独立真实绑定；缺失时 fail closed，
    # 不得 fallback 到 source_manifest_hash 伪装成 lock 身份。
    if not all(path.is_file() for path in lock_paths):
        raise ValueError("dependency lock identity requires uv.lock and pyproject.toml")
    dependency_lock_hash = _hash_files(lock_paths)
    return FrozenRunIdentity(
        source_commit=source_identity.revision,
        source_manifest_hash=source_identity.manifest_hash,
        git_dirty=False,
        runtime_manifest_hash=runtime_manifest_hash,
        capability=capability,
        prompt_hash=_hash_files(
            [Path(__file__), backend_root / "diagnosis" / "v11_runtime.py"]
        ),
        tool_manifest_hash=tool_manifest_hash_value,
        skill_catalog_hash=skill_catalog_hash_value,
        prediction_schema_hash=_canonical_hash(CasePrediction.model_json_schema()),
        normalizer_hash=_hash_files([Path(__file__).with_name("evaluator.py")]),
        scorer_dependency_hash=scorer_dependency_hash(),
        dependency_lock_hash=dependency_lock_hash,
        memory_snapshot_hash=_canonical_hash(EMPTY_MEMORY_IDENTITY),
        retry_policy_hash=_canonical_hash(RETRY_POLICY),
    )


def _assert_persisted_contract(run: RuntimeRun, expected: dict[str, object]) -> None:
    validate_v11_execution_contract(run.execution_contract)
    if run.execution_contract != expected:
        raise ValueError("persisted RCAEval RuntimeRun contract mismatch")
    if execution_contract_digest(run.execution_contract) != expected[
        "execution_contract_digest"
    ]:
        raise ValueError("persisted RCAEval RuntimeRun contract hash mismatch")


def _published_candidates(
    review: CoordinationReview | None,
    *,
    single: bool,
    completed: bool,
):
    if not completed or review is None:
        return []
    if single:
        return sorted(review.candidates, key=lambda item: item.rank)
    allowed = set(review.authoritative_candidate_ids)
    return sorted(
        (candidate for candidate in review.candidates if candidate.id in allowed),
        key=lambda item: item.rank,
    )


def _runtime_token_usage(runtime_store, run_id: str) -> tuple[int, int]:
    payloads = [
        item.safe_payload
        for item in runtime_store.list_events(run_id)
        if item.event_type == RuntimeEventType.MODEL_COMPLETED
    ]
    return (
        sum(int(item.get("input_tokens", 0)) for item in payloads),
        sum(int(item.get("output_tokens", 0)) for item in payloads),
    )


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()
