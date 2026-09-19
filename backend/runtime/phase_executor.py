from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.agents_runtime import (
    AgentsRcaRuntimeResult,
    ConflictReviewOutcome,
)
from backend.diagnosis.context import (
    DiagnosisContext,
    SpecialistResult,
    provider_status_to_specialist_status,
)
from backend.diagnosis.coordinator import AGENT_NAMES_BY_PROVIDER
from backend.diagnosis.evidence_validation import (
    EvidenceContractError,
    validate_hypotheses,
    validate_investigation_evidence,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.multi_agent import (
    AuthorityMode,
    ExecutionContractVersion,
    FailureCategory,
    InvestigationStrategy,
    ResultValidationCategory,
)
from backend.domain.runtime import RuntimePhase, RuntimeResumeState, RuntimeRunReason
from backend.runtime.concurrency import RunStepGate
from backend.runtime.phases import (
    BusinessMutation,
    PhaseOutput,
    phase_profile_for,
)
from backend.runtime.telemetry import RuntimeTelemetry
from backend.safety.redaction import redact_model, safe_failure

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _DiagnosisState:
    event: IncidentEvent
    strategy: InvestigationStrategy
    run_reason: RuntimeRunReason = RuntimeRunReason.INITIAL
    runtime_run_id: str | None = None
    safe_event: IncidentEvent | None = None
    record: InvestigationRecord | None = None
    supporting_evidence: list[EvidenceItem] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    v7_result: object | None = None
    session_tool_budget: int | None = None
    session_token_budget: int | None = None
    preexisting_terminal_tool_ids: frozenset[str] = frozenset()
    check_execution: Callable[[], None] = field(default=lambda: None)
    hit_fault: Callable[[str], None] = field(default=lambda _point: None)
    phase_input: object | None = None


@dataclass(frozen=True, slots=True)
class DiagnosisPhaseResult:
    record: InvestigationRecord
    outputs: dict[RuntimePhase, PhaseOutput]


class DiagnosisPhaseExecutor:
    """把既有 Diagnosis 编排映射到稳定且可恢复的 Phase 边界。"""

    def __init__(
        self,
        orchestrator,
        *,
        max_parallel_steps_per_run: int = 3,
        parallel_limit: RunStepGate | None = None,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        if max_parallel_steps_per_run < 1:
            raise ValueError("max_parallel_steps_per_run must be positive")
        self._orchestrator = orchestrator
        self._max_parallel_steps_per_run = max_parallel_steps_per_run
        self._parallel_limit = parallel_limit or RunStepGate(
            max_parallel_steps_per_run
        )
        self.telemetry = telemetry or RuntimeTelemetry.disabled()
        self._runtime_states: dict[str, _DiagnosisState] = {}
        self._durable_sessions: dict[str, DiagnosisPhaseExecutor] = {}
        self._durable_attempt_ids: dict[str, str] = {}
        self._is_durable_session = False
        self._execution_contract_version = ExecutionContractVersion.V10_LEGACY
        self._handlers: dict[
            RuntimePhase, Callable[[_DiagnosisState], Awaitable[PhaseOutput]]
        ] = {
            RuntimePhase.INTAKE: self.intake,
            RuntimePhase.EVIDENCE_COLLECTION: self.evidence_collection,
            RuntimePhase.DETERMINISTIC_RCA: self.deterministic_rca,
            RuntimePhase.SPECIALIST_ANALYSIS: self.specialist_analysis,
            RuntimePhase.CONFLICT_REVIEW: self.conflict_review,
            RuntimePhase.COORDINATION: self.coordination,
            RuntimePhase.REPORT_GENERATION: self.report_generation,
            RuntimePhase.FINALIZE: self.finalize,
        }
        self._v11_handlers: dict[
            RuntimePhase, Callable[[_DiagnosisState], Awaitable[PhaseOutput]]
        ] = {
            RuntimePhase.INTAKE: self._v11_intake,
            RuntimePhase.EVIDENCE_COLLECTION: self._v11_evidence_collection,
            RuntimePhase.LEAD_PLANNING: self._v11_runtime_phase,
            RuntimePhase.INVESTIGATOR_ROUND_1: self._v11_runtime_phase,
            RuntimePhase.CRITIC_REVIEW: self._v11_runtime_phase,
            RuntimePhase.INVESTIGATOR_ROUND_2: self._v11_runtime_phase,
            RuntimePhase.CRITIC_RECONCILIATION: self._v11_runtime_phase,
            RuntimePhase.LEAD_ADJUDICATION: self._v11_runtime_phase,
            RuntimePhase.RESULT_VALIDATION: self._v11_runtime_phase,
            RuntimePhase.REPORT_GENERATION: self._v11_report_generation,
            RuntimePhase.FINALIZE: self._v11_finalize,
        }

    async def execute_phase(self, phase_input) -> PhaseOutput:
        """供 durable coordinator 按 Run 驱动单个 Phase，不依赖全局当前会话。"""
        if not self._is_durable_session:
            executor = self._durable_sessions.get(phase_input.run_id)
            if (
                executor is None
                or self._durable_attempt_ids.get(phase_input.run_id)
                != phase_input.attempt_id
            ):
                executor = self._build_durable_session(phase_input)
                self._durable_sessions[phase_input.run_id] = executor
                self._durable_attempt_ids[phase_input.run_id] = phase_input.attempt_id
            if phase_input.execution_contract_version == ExecutionContractVersion.V11:
                # 前一阶段的提交才是事实源；工具回调及报告只写 durable 投影时，
                # 不能让复用的内存快照在后续阶段把它们覆盖掉。
                record = self._orchestrator.repository.get(phase_input.investigation_id)
                executor._orchestrator.repository.save(record)
                executor._orchestrator.repository.save_tool_calls(
                    record.id, self._orchestrator.repository.list_tool_calls(record.id)
                )
                state = executor._runtime_states.get(phase_input.run_id)
                if state is not None:
                    state.record = record
            executor._bind_phase_callbacks(phase_input)
            executor._execution_contract_version = phase_input.execution_contract_version
            output = await executor.execute_phase(phase_input)
            if phase_input.phase == RuntimePhase.FINALIZE:
                self._durable_sessions.pop(phase_input.run_id, None)
                self._durable_attempt_ids.pop(phase_input.run_id, None)
            return output
        state = self._runtime_states.get(phase_input.run_id)
        if state is None:
            state = self._rehydrate_runtime_state(phase_input)
            self._runtime_states[phase_input.run_id] = state
        state.phase_input = phase_input
        if phase_input.execution_contract_version == ExecutionContractVersion.V11:
            output = await self._v11_handlers[phase_input.phase](state)
        elif phase_input.phase == RuntimePhase.INTAKE:
            output = await self._intake_existing(state)
        else:
            output = await self._handlers[phase_input.phase](state)
        if phase_input.phase == RuntimePhase.FINALIZE:
            self._runtime_states.pop(phase_input.run_id, None)
        return output

    def release_attempt(self, run_id: str, attempt_id: str) -> None:
        """释放指定 Attempt 的隔离投影，禁止失败 Attempt 状态污染后续恢复。"""
        if self._durable_attempt_ids.get(run_id) != attempt_id:
            return
        self._durable_sessions.pop(run_id, None)
        self._durable_attempt_ids.pop(run_id, None)

    def _bind_phase_callbacks(self, phase_input) -> None:
        """刷新当前 Phase 的持久化回调，避免复用 session 时沿用首次 Phase 的作用域。"""
        agents_runtime = self._orchestrator.agents_runtime
        if agents_runtime is not None:
            agents_runtime._check_execution = phase_input.check_execution or (lambda: None)
            agents_runtime._resolve_tool_result = phase_input.resolve_tool_result
            agents_runtime._persist_tool_start = phase_input.persist_tool_start
            agents_runtime._persist_tool_result = phase_input.persist_tool_result
            agents_runtime._persist_agent_event = phase_input.persist_agent_event
            agents_runtime._persist_model_event = phase_input.persist_model_event
            agents_runtime._hit_fault = phase_input.hit_fault or (lambda _point: None)
        v11_runtime = getattr(self._orchestrator, "v11_runtime", None)
        if v11_runtime is not None:
            v11_runtime.bind_phase(phase_input)
        state = self._runtime_states.get(phase_input.run_id)
        if state is not None:
            state.phase_input = phase_input
            state.check_execution = phase_input.check_execution or (lambda: None)
            state.hit_fault = phase_input.hit_fault or (lambda _point: None)

    def _build_durable_session(self, phase_input) -> DiagnosisPhaseExecutor:
        if phase_input.investigation_id is None:
            raise RuntimeError("runtime phase input lacks investigation identity")
        # Phase 在隔离的内存投影中计算；只有 RuntimeWriter 能把业务结果与 checkpoint 原子提交。
        source = self._orchestrator.repository
        repository = InMemoryInvestigationRepository()
        record = source.get(phase_input.investigation_id)
        repository.save(record)
        plan = source.get_plan(record.id)
        tasks = list(source.list_tasks(record.id))
        context_facts = list(source.list_context_facts(record.id))
        tool_calls = list(source.list_tool_calls(record.id))
        findings = list(source.list_agent_findings(record.id))
        executions = list(source.list_executions(record.id))
        review = source.get_coordination_review(record.id)
        if plan is not None:
            repository.save_plan(plan)
        # Supplemental tasks are persisted as an independent projection.  Do
        # not rely on get_plan() to merge them into the historical plan payload
        # when rebuilding a durable phase session.
        if tasks:
            repository.save_tasks(record.id, tasks)
        if context_facts:
            repository.save_context_facts(record.id, context_facts)
        if tool_calls:
            repository.save_tool_calls(record.id, tool_calls)
        if findings or executions or review is not None:
            repository.save_multi_agent_result(
                record.id,
                findings,
                executions,
                review,
            )

        from backend.diagnosis.orchestrator import DiagnosisOrchestrator

        parallel_limit = RunStepGate(self._max_parallel_steps_per_run)
        agents_runtime = (
            self._orchestrator.agents_runtime.clone_for_run(
                model_provider=phase_input.model_provider,
                model_name=phase_input.model_name,
                prompt_version=phase_input.prompt_version,
                token_budget=phase_input.token_budget,
                timeout_seconds=phase_input.timeout_seconds,
            )
            if self._orchestrator.agents_runtime is not None
            else None
        )
        if agents_runtime is not None:
            agents_runtime._parallel_limit = parallel_limit
            agents_runtime.runtime_run_id = phase_input.run_id
            agents_runtime._check_execution = phase_input.check_execution or (lambda: None)
            agents_runtime._resolve_tool_result = phase_input.resolve_tool_result
            agents_runtime._persist_tool_start = phase_input.persist_tool_start
            agents_runtime._persist_tool_result = phase_input.persist_tool_result
            agents_runtime._persist_agent_event = phase_input.persist_agent_event
            agents_runtime._persist_model_event = phase_input.persist_model_event
            agents_runtime._hit_fault = phase_input.hit_fault or (lambda _point: None)
            if phase_input.tool_budget is not None:
                agents_runtime.max_total_tool_calls = phase_input.tool_budget
        v11_source = getattr(self._orchestrator, "v11_runtime", None)
        v11_runtime = (
            v11_source.clone_for_run(
                runtime_run_id=phase_input.run_id,
                model_provider=phase_input.model_provider,
                model_name=phase_input.model_name,
                token_budget=phase_input.token_budget,
                timeout_seconds=phase_input.timeout_seconds,
                execution_contract=phase_input.execution_contract,
            )
            if v11_source is not None
            else None
        )
        sandbox = DiagnosisOrchestrator(
            repository=repository,
            providers=self._orchestrator.providers,
            analyzer=self._orchestrator.analyzer,
            report_generator=self._orchestrator.report_generator,
            coordinator=self._orchestrator.coordinator,
            action_planner=self._orchestrator.action_planner,
            task_planner=self._orchestrator.task_planner,
            agents_runtime=agents_runtime,
            v11_runtime=v11_runtime,
            default_strategy=self._orchestrator.default_strategy,
            max_tool_calls_per_specialist=(
                self._orchestrator.max_tool_calls_per_specialist
            ),
            max_total_tool_calls=(
                phase_input.tool_budget
                if phase_input.tool_budget is not None
                else self._orchestrator.max_total_tool_calls
            ),
        )
        executor = DiagnosisPhaseExecutor(
            sandbox,
            max_parallel_steps_per_run=self._max_parallel_steps_per_run,
            parallel_limit=parallel_limit,
            telemetry=self.telemetry,
        )
        executor._is_durable_session = True
        return executor

    async def execute(
        self,
        event: IncidentEvent,
        strategy: InvestigationStrategy | None = None,
        execution_contract_version: ExecutionContractVersion = (
            ExecutionContractVersion.V10_LEGACY
        ),
    ) -> DiagnosisPhaseResult:
        if (
            ExecutionContractVersion(execution_contract_version)
            == ExecutionContractVersion.V11
        ):
            raise ValueError("V11 execution requires a persisted RuntimeRun")
        state = _DiagnosisState(
            event=event,
            strategy=InvestigationStrategy(
                strategy or self._orchestrator.default_strategy
            ),
        )
        outputs: dict[RuntimePhase, PhaseOutput] = {}
        try:
            for phase in phase_profile_for(
                self._execution_contract_version
            ).order:
                outputs[phase] = await self._handlers[phase](state)
        except asyncio.CancelledError:
            if state.record is not None:
                try:
                    self._orchestrator.repository.update_status(
                        state.record.id, InvestigationStatus.CANCELLED
                    )
                except Exception as exc:
                    logger.warning(
                        "investigation cancellation persistence failed id=%s error_type=%s",
                        state.record.id,
                        type(exc).__name__,
                    )
            raise
        except Exception as exc:
            if state.record is None:
                raise
            logger.error(
                "investigation failed id=%s error_type=%s",
                state.record.id,
                type(exc).__name__,
            )
            failure_reason = safe_failure("investigation_failure")
            state.record.failure_reason = failure_reason
            state.record.updated_at = datetime.now(UTC)
            self._orchestrator.repository.save(state.record)
            failed = self._orchestrator.repository.update_status(
                state.record.id,
                InvestigationStatus.FAILED,
                failure_reason=failure_reason,
            )
            return DiagnosisPhaseResult(record=failed, outputs=outputs)
        assert state.record is not None
        return DiagnosisPhaseResult(record=state.record, outputs=outputs)

    async def intake(self, state: _DiagnosisState) -> PhaseOutput:
        safe_event = redact_model(state.event)
        record = self._orchestrator.repository.save(
            InvestigationRecord(
                event=safe_event,
                strategy=state.strategy,
                status=InvestigationStatus.PENDING,
            )
        )
        logger.info(
            "investigation started id=%s service=%s", record.id, safe_event.service
        )
        state.safe_event = safe_event
        state.record = self._orchestrator.repository.update_status(
            record.id, InvestigationStatus.RUNNING
        )
        return self._output(state, status="completed")

    async def _v11_intake(self, state: _DiagnosisState) -> PhaseOutput:
        if state.runtime_run_id is None:
            raise ValueError("V11 intake requires a persisted runtime run")
        output = await self._intake_existing(state)
        mutation = output.business_mutation
        if mutation.investigation is None:
            raise ValueError("V11 intake requires an investigation projection")
        # 不在 handler 内提前落库激活：SQLite 的 activate_projection 自带独立
        # 事务并即时提交，会造成 owner 已切换、latest 投影已清理但无 INTAKE
        # checkpoint 的崩溃窗口。这里只构造 owner 已切换的本地投影，真正的
        # 激活与清理由 commit_phase 事务内的 apply_* 原子完成。
        activated = mutation.investigation.model_copy(
            update={"active_runtime_run_id": state.runtime_run_id}
        )
        projected = replace(
            mutation,
            investigation=activated,
            plan=None,
            tasks=None,
            context_facts=None,
            tool_calls=None,
            findings=(),
            executions=(),
            review=None,
            react_trace=None,
            replace_multi_agent_result=True,
            activate_projection=True,
        )
        # state.record 与提交后的持久投影保持一致（owner 切换 + latest 投影
        # 清空）；清空字段集合复用 _projection_record 这一单一事实来源。
        state.record = projected._projection_record()
        return replace(output, business_mutation=projected)

    async def _v11_pass(self, state: _DiagnosisState) -> PhaseOutput:
        return self._output(state)

    async def _v11_evidence_collection(self, state: _DiagnosisState) -> PhaseOutput:
        """V11 先持久化只读证据，再让 Agent 生成诊断候选。"""
        # 隔离测试和最小 Runtime 宿主可能没有 V11 Runtime；此时只能跳过，
        # 不能回退到 legacy coordinator，否则 V11 会悄悄重新走旧诊断链路。
        if not hasattr(self._orchestrator, "v11_runtime"):
            return await self._v11_pass(state)
        providers = getattr(self._orchestrator, "providers", None)
        if providers is None:
            raise RuntimeError("V11 evidence collection requires a provider registry")
        provider_results = await providers.collect_results_async(
            state.event,
            max_parallel_steps=self._max_parallel_steps_per_run,
            check_execution=state.check_execution,
            parallel_limit=self._parallel_limit,
        )
        specialist_results = [
            SpecialistResult(
                agent_name=AGENT_NAMES_BY_PROVIDER.get(
                    str(result.provider), f"{result.provider}Analyst"
                ),
                status=provider_status_to_specialist_status(result.status),
                evidence_items=result.evidence_items,
                summary=(
                    f"{result.provider} provider returned "
                    f"{len(result.evidence_items)} evidence item(s)"
                ),
                errors=[result.error_message] if result.error_message else [],
                duration_ms=result.duration_ms,
            )
            for result in provider_results
        ]
        context = DiagnosisContext(
            event=state.event,
            evidence=providers.evidence_from_results(provider_results),
            provider_results=provider_results,
            specialist_results=specialist_results,
        )
        return await self._persist_evidence_context(
            state, context, record_legacy_execution=False
        )

    async def _v11_report_generation(self, state: _DiagnosisState) -> PhaseOutput:
        """把 V11 review 投影为报告和 candidate-owned 只读建议。"""
        record, safe_event = self._required(state)
        review = self._orchestrator.repository.get_coordination_review(record.id)
        run = record.multi_agent_run
        if review is None or run is None:
            return await self._v11_pass(state)
        if not hasattr(self._orchestrator, "action_planner") or not hasattr(
            self._orchestrator, "report_generator"
        ):
            # 仅有 Runtime phase 的隔离调用不拥有产品投影服务，不能借此回退到旧路径。
            return await self._v11_pass(state)
        if (
            review.authority_mode != AuthorityMode.AGENT
            or run.authority_mode != AuthorityMode.AGENT
            or review.runtime_run_id is None
            or review.runtime_run_id != run.runtime_run_id
        ):
            raise RuntimeError("V11 report projection owner does not match the run")

        actions, verifications = await asyncio.to_thread(
            self._orchestrator.action_planner.plan_v11,
            safe_event,
            list(record.evidence),
            review,
            run,
        )
        record = record.model_copy(
            update={
                "actions": actions,
                "verification_suggestions": verifications,
            }
        )
        report = await asyncio.to_thread(
            self._orchestrator.report_generator.generate_v11,
            record.id,
            safe_event,
            list(record.evidence),
            actions=actions,
            verification_suggestions=verifications,
            coordination_review=review,
            multi_agent_run=run,
            agent_findings=self._orchestrator.repository.list_agent_findings(record.id),
        )
        # 报告和 action 必须与本 phase 的 checkpoint 一起提交；handler 只返回
        # BusinessMutation，避免在 PhaseCommit 之前留下不可回滚的业务写入。
        state.record = record.model_copy(
            update={"report": report, "updated_at": datetime.now(UTC)}
        )
        return self._output(state, report_count=1)

    async def _v11_runtime_phase(self, state: _DiagnosisState) -> PhaseOutput:
        """将 V11 phase 交给持久化 Runtime；兼容旧的隔离哨兵。"""
        if not hasattr(self._orchestrator, "v11_runtime"):
            return self._output(state)
        runtime = self._orchestrator.v11_runtime
        if runtime is None:
            raise RuntimeError("V11 Runtime is not configured")
        if state.phase_input is None or state.record is None:
            raise RuntimeError("V11 phase lacks persisted input")
        runtime.bind_phase(state.phase_input)
        await runtime.run_phase(
            state.phase_input.phase,
            repository=self._orchestrator.repository,
            investigation_id=state.record.id,
            event=state.safe_event or state.record.event,
        )
        state.record = self._orchestrator.repository.get(state.record.id)
        if state.record.status == InvestigationStatus.FAILED:
            return self._output(state, status="failed")
        status = "completed"
        if state.phase_input.phase in {
            RuntimePhase.INVESTIGATOR_ROUND_2,
            RuntimePhase.CRITIC_RECONCILIATION,
        } and not any(
            task.analysis_round == 2
            for task in self._orchestrator.repository.list_tasks(state.record.id)
        ):
            status = "skipped"
        return self._output(state, status=status)

    async def _v11_skip(self, state: _DiagnosisState) -> PhaseOutput:
        return self._output(state, status="skipped")

    async def _v11_finalize(self, state: _DiagnosisState) -> PhaseOutput:
        record, _ = self._required(state)
        if record.status == InvestigationStatus.FAILED:
            return self._output(state, status="failed")
        status = (
            record.multi_agent_run.diagnostic_status
            if record.multi_agent_run is not None
            else None
        )
        if status is not None:
            state.record = self._orchestrator.repository.update_status(
                record.id, InvestigationStatus.COMPLETED
            )
        else:
            state.record = self._orchestrator.repository.update_status(
                record.id,
                InvestigationStatus.FAILED,
                failure_reason="v11 diagnostic status is missing",
            )
        return self._output(state)

    async def _intake_existing(self, state: _DiagnosisState) -> PhaseOutput:
        record, safe_event = self._required(state)
        if record.status == InvestigationStatus.PENDING:
            record = self._orchestrator.repository.update_status(
                record.id, InvestigationStatus.RUNNING
            )
        elif (
            record.status
            in {
                InvestigationStatus.COMPLETED,
                InvestigationStatus.FAILED,
                InvestigationStatus.CANCELLED,
            }
            and state.run_reason
            in {
                RuntimeRunReason.MANUAL_RERUN,
                RuntimeRunReason.ADDITIONAL_EVIDENCE,
            }
        ):
            record = self._orchestrator.repository.update_status(
                record.id, InvestigationStatus.RUNNING
            )
        elif record.status != InvestigationStatus.RUNNING:
            raise RuntimeError("runtime intake requires pending or running investigation")
        logger.info(
            "investigation runtime started id=%s service=%s",
            record.id,
            safe_event.service,
        )
        state.record = record
        return self._output(state, status="completed")

    async def evidence_collection(
        self, state: _DiagnosisState, *, record_legacy_execution: bool = True
    ) -> PhaseOutput:
        context = await self._orchestrator.coordinator.collect_async(
            state.event,
            max_parallel_steps=self._max_parallel_steps_per_run,
            check_execution=state.check_execution,
            parallel_limit=self._parallel_limit,
        )
        return await self._persist_evidence_context(
            state, context, record_legacy_execution=record_legacy_execution
        )

    async def _persist_evidence_context(
        self,
        state: _DiagnosisState,
        context: DiagnosisContext,
        *,
        record_legacy_execution: bool,
    ) -> PhaseOutput:
        record, safe_event = self._required(state)
        state.hit_fault("provider_before_commit")
        runtime_run_id = state.runtime_run_id

        def bind_evidence_owner(items):
            if runtime_run_id is None:
                return list(items)
            return [
                item.model_copy(update={"runtime_run_id": runtime_run_id})
                for item in items
            ]

        provider_results = [
            result.model_copy(
                update={"evidence_items": bind_evidence_owner(result.evidence_items)}
            )
            for result in context.provider_results
        ]
        specialist_results = [
            result.model_copy(
                update={"evidence_items": bind_evidence_owner(result.evidence_items)}
            )
            for result in context.specialist_results
        ]
        evidence = bind_evidence_owner(context.evidence)
        validated = validate_investigation_evidence(provider_results)
        state.supporting_evidence = validated.supporting_evidence
        record.provider_results = provider_results
        record.specialist_results = specialist_results
        record.evidence = evidence
        record.updated_at = datetime.now(UTC)
        state.record = self._orchestrator.repository.save(record)
        if record_legacy_execution:
            await asyncio.to_thread(
                self._orchestrator._record_v4_execution,
                record.id,
                safe_event,
                context.provider_results,
            )
        return self._output(state, evidence_count=len(record.evidence))

    async def deterministic_rca(self, state: _DiagnosisState) -> PhaseOutput:
        record, safe_event = self._required(state)
        hypotheses = await asyncio.to_thread(
            self._orchestrator.analyzer.analyze,
            safe_event,
            state.supporting_evidence,
        )
        validate_hypotheses(state.supporting_evidence, hypotheses)
        state.hypotheses = hypotheses
        record.hypotheses = hypotheses
        record.updated_at = datetime.now(UTC)
        state.record = self._orchestrator.repository.save(record)
        return self._output(state)

    async def specialist_analysis(self, state: _DiagnosisState) -> PhaseOutput:
        record, safe_event = self._required(state)
        runtime = self._orchestrator.agents_runtime
        if runtime is None or not self._supports_split_runtime(runtime):
            await asyncio.to_thread(
                self._orchestrator._record_v5_findings,
                record.id,
                safe_event,
                state.supporting_evidence,
                state.hypotheses,
            )
        elif self._supports_split_runtime(runtime):
            self._ensure_agents_deadline(runtime)
            state.v7_result = await runtime.run(
                investigation_id=record.id,
                event=safe_event,
                evidence=state.supporting_evidence,
                hypotheses=state.hypotheses,
                strategy=state.strategy,
                stop_after_specialists=True,
            )
            self._orchestrator._persist_adaptive_artifacts(
                record.id, state.v7_result
            )
            self._orchestrator.repository.save_tasks(
                record.id, state.v7_result.tasks
            )
            self._orchestrator.repository.save_multi_agent_result(
                record.id,
                state.v7_result.findings,
                state.v7_result.executions,
                None,
            )
        state.record = self._orchestrator.repository.get(record.id)
        state.record.multi_agent_run = self._orchestrator._resolved_run_summary(
            state.strategy, state.v7_result
        )
        state.record.updated_at = datetime.now(UTC)
        state.record = self._orchestrator.repository.save(state.record)
        return self._output(state)

    async def conflict_review(self, state: _DiagnosisState) -> PhaseOutput:
        record, _ = self._required(state)
        runtime = self._orchestrator.agents_runtime
        outcome = ConflictReviewOutcome.SKIPPED
        if self._supports_split_runtime(runtime) and isinstance(
            state.v7_result, AgentsRcaRuntimeResult
        ):
            self._ensure_agents_deadline(runtime)
            outcome = await runtime.review_conflicts(
                state.v7_result,
                record.id,
                state.safe_event,
                state.supporting_evidence,
                state.hypotheses,
            )
            if outcome != ConflictReviewOutcome.SKIPPED:
                self._orchestrator.repository.save_tasks(
                    record.id, state.v7_result.tasks
                )
                self._orchestrator.repository.save_multi_agent_result(
                    record.id,
                    state.v7_result.findings,
                    state.v7_result.executions,
                    None,
                )
                record = self._orchestrator.repository.get(record.id)
                record.multi_agent_run = self._orchestrator._resolved_run_summary(
                    state.strategy, state.v7_result
                )
                record.updated_at = datetime.now(UTC)
                state.record = self._orchestrator.repository.save(record)
        status = (
            "skipped"
            if outcome == ConflictReviewOutcome.SKIPPED
            else "completed"
        )
        return self._output(state, status=status, review_count=0)

    async def coordination(self, state: _DiagnosisState) -> PhaseOutput:
        record, safe_event = self._required(state)
        v7_result = state.v7_result
        runtime = self._orchestrator.agents_runtime
        if self._supports_split_runtime(runtime) and isinstance(
            v7_result, AgentsRcaRuntimeResult
        ):
            self._ensure_agents_deadline(runtime)
            try:
                v7_result = await runtime.synthesize(
                    v7_result,
                    record.id,
                    state.supporting_evidence,
                    state.hypotheses,
                )
            except EvidenceContractError:
                logger.warning(
                    "split agents validation failed id=%s validation_category=%s",
                    record.id,
                    ResultValidationCategory.SEMANTIC_REFERENCE.value,
                )
                fallback = AgentsRcaRuntimeResult.validation_failed(
                    ResultValidationCategory.SEMANTIC_REFERENCE,
                    model_provider=v7_result.run_summary.model_provider,
                    model_name=v7_result.run_summary.model_name,
                )
                self._orchestrator._copy_adaptive_metadata(
                    fallback, v7_result, degraded=True
                )
                v7_result = fallback
            state.v7_result = v7_result
            self._orchestrator.repository.save_tasks(record.id, v7_result.tasks)
            self._orchestrator.repository.save_multi_agent_result(
                record.id,
                v7_result.findings,
                v7_result.executions,
                v7_result.review,
            )
        split_timed_out = (
            self._supports_split_runtime(runtime)
            and isinstance(v7_result, AgentsRcaRuntimeResult)
            and any(
                execution.failure_category == FailureCategory.TIMEOUT
                for execution in v7_result.executions
            )
        )
        if (
            self._orchestrator.repository.get_coordination_review(record.id) is None
            and not split_timed_out
        ):
            await asyncio.to_thread(
                self._orchestrator._record_v5_review,
                record.id,
                state.supporting_evidence,
                state.hypotheses,
            )
        if runtime is not None and not self._supports_split_runtime(runtime):
            # 旧 Runtime 扩展点只提供一次性 run；在 coordination 内沿用原有完整契约。
            v7_result = await self._orchestrator._record_v7_coordination_async(
                record.id, state.strategy
            )
            state.v7_result = v7_result
        persisted = self._orchestrator.repository.get(record.id)
        if v7_result is not None or persisted.multi_agent_run is None:
            persisted.multi_agent_run = self._orchestrator._resolved_run_summary(
                state.strategy, v7_result
            )
        persisted.updated_at = datetime.now(UTC)
        persisted = self._orchestrator.repository.save(persisted)
        actions, verifications = await asyncio.to_thread(
            self._orchestrator.action_planner.plan,
            safe_event,
            state.supporting_evidence,
            state.hypotheses,
        )
        evidence = self._orchestrator._ensure_action_evidence(
            persisted.evidence, actions, persisted.id
        )
        persisted.evidence = evidence
        persisted.actions = actions
        persisted.verification_suggestions = verifications
        persisted.updated_at = datetime.now(UTC)
        state.record = self._orchestrator.repository.save(persisted)
        return self._output(state)

    @staticmethod
    def _supports_split_runtime(runtime: object | None) -> bool:
        """仅对显式实现分阶段协议的 Runtime 拆分模型调用。"""
        return runtime is not None and all(
            callable(getattr(runtime, method, None))
            for method in ("review_conflicts", "synthesize")
        )

    @staticmethod
    def _ensure_agents_deadline(runtime: object) -> None:
        """首次进入待执行的 Agents Phase 时建立 Attempt 内共享 deadline。"""
        begin_deadline = getattr(runtime, "begin_run_deadline", None)
        if begin_deadline is not None:
            begin_deadline()

    async def report_generation(self, state: _DiagnosisState) -> PhaseOutput:
        record, safe_event = self._required(state)
        v7_result = state.v7_result
        extra = (
            {
                "coordination_review": v7_result.review,
                "multi_agent_run": v7_result.run_summary,
                "agent_findings": v7_result.findings,
            }
            if v7_result is not None
            else {}
        )
        report = await asyncio.to_thread(
            self._orchestrator.report_generator.generate,
            record.id,
            safe_event,
            record.evidence,
            state.hypotheses,
            actions=record.actions,
            verification_suggestions=record.verification_suggestions,
            **extra,
        )
        record.report = report
        record.updated_at = datetime.now(UTC)
        state.record = self._orchestrator.repository.save(record)
        return self._output(state, report_count=1)

    async def finalize(self, state: _DiagnosisState) -> PhaseOutput:
        record, _ = self._required(state)
        completed = self._orchestrator.repository.update_status(
            record.id, InvestigationStatus.COMPLETED
        )
        logger.info(
            "investigation completed id=%s evidence_count=%s action_count=%s verification_count=%s",
            completed.id,
            len(completed.evidence),
            len(completed.actions),
            len(completed.verification_suggestions),
        )
        state.record = completed
        return self._output(state)

    def _output(
        self,
        state: _DiagnosisState,
        *,
        status: str = "completed",
        evidence_count: int | None = None,
        review_count: int | None = None,
        report_count: int | None = None,
    ) -> PhaseOutput:
        record, _ = self._required(state)
        findings = self._orchestrator.repository.list_agent_findings(record.id)
        review = self._orchestrator.repository.get_coordination_review(record.id)
        tool_calls = self._orchestrator.repository.list_tool_calls(record.id)
        runtime_owner = (
            getattr(self._orchestrator, "v11_runtime", None)
            if self._execution_contract_version == ExecutionContractVersion.V11
            else self._orchestrator.agents_runtime
        )
        runtime_run_id = getattr(runtime_owner, "runtime_run_id", None)
        terminal_tool_ids = {
            call.logical_call_id or call.id
            for call in tool_calls
            if call.consumes_budget
            and runtime_run_id is not None
            and call.runtime_run_id == runtime_run_id
        }
        session_tool_budget = (
            state.session_tool_budget
            if state.session_tool_budget is not None
            else self._orchestrator.max_total_tool_calls
        )
        payload = {
            "status": status,
            "evidence_count": len(record.evidence)
            if evidence_count is None
            else evidence_count,
            "finding_count": len(findings),
            "review_count": int(review is not None)
            if review_count is None
            else review_count,
            "report_count": int(record.report is not None)
            if report_count is None
            else report_count,
        }
        resume_state = RuntimeResumeState(
            completed_evidence_ids=[item.id for item in record.evidence],
            completed_finding_ids=[item.id for item in findings],
            completed_review_ids=[review.id] if review is not None else [],
            completed_report_ids=(
                [record.report.id]
                if record.report is not None and hasattr(record.report, "id")
                else []
            ),
            remaining_tool_budget=max(
                0,
                session_tool_budget
                - len(
                    terminal_tool_ids
                    - state.preexisting_terminal_tool_ids
                ),
            ),
            remaining_token_budget=(
                getattr(runtime_owner, "remaining_token_budget", None)
                if self._execution_contract_version == ExecutionContractVersion.V11
                else (
                    state.session_token_budget
                    if state.session_token_budget is not None
                    else getattr(
                        self._orchestrator.agents_runtime,
                        "runtime_token_budget",
                        None,
                    )
                )
            ),
            remaining_model_turns=(
                getattr(runtime_owner, "remaining_model_turns", None)
                if self._execution_contract_version == ExecutionContractVersion.V11
                else None
            ),
            successful_tool_keys=sorted(
                {
                    call.idempotency_key
                    for call in tool_calls
                    if call.status.value == "success"
                    and runtime_run_id is not None
                    and call.runtime_run_id == runtime_run_id
                    and call.idempotency_key is not None
                }
            ),
        )
        plan = self._orchestrator.repository.get_plan(record.id)
        if plan is not None and plan.runtime_run_id is not None:
            plan = plan.model_copy(
                update={
                    "tasks": [
                        task
                        for task in plan.tasks
                        if task.analysis_round == 1
                    ]
                }
            )
        return PhaseOutput(
            business_mutation=BusinessMutation(
                investigation_id=record.id,
                investigation=record.model_copy(deep=True),
                plan=plan,
                tasks=tuple(self._orchestrator.repository.list_tasks(record.id)),
                context_facts=tuple(
                    self._orchestrator.repository.list_context_facts(record.id)
                ),
                tool_calls=tuple(
                    tool_calls
                ),
                findings=tuple(findings),
                executions=tuple(
                    self._orchestrator.repository.list_executions(record.id)
                ),
                review=review,
                replace_multi_agent_result=True,
            ),
            status=status,
            safe_payload=payload,
            resume_state=resume_state,
        )

    def _rehydrate_runtime_state(self, phase_input) -> _DiagnosisState:
        if phase_input.investigation_id is None or phase_input.strategy is None:
            raise RuntimeError("runtime phase input lacks frozen run identity")
        record = self._orchestrator.repository.get(phase_input.investigation_id)
        supporting_evidence: list[EvidenceItem] = []
        if record.provider_results:
            supporting_evidence = validate_investigation_evidence(
                record.provider_results
            ).supporting_evidence
        preexisting_terminal_tool_ids = frozenset(
            call.logical_call_id or call.id
            for call in self._orchestrator.repository.list_tool_calls(record.id)
            if call.consumes_budget
            and call.runtime_run_id == phase_input.run_id
        )
        state = _DiagnosisState(
            event=record.event,
            strategy=InvestigationStrategy(phase_input.strategy),
            run_reason=phase_input.run_reason,
            runtime_run_id=phase_input.run_id,
            safe_event=redact_model(record.event),
            record=record,
            supporting_evidence=supporting_evidence,
            hypotheses=list(record.hypotheses),
            session_tool_budget=phase_input.tool_budget,
            session_token_budget=phase_input.token_budget,
            preexisting_terminal_tool_ids=preexisting_terminal_tool_ids,
            check_execution=phase_input.check_execution or (lambda: None),
            hit_fault=phase_input.hit_fault or (lambda _point: None),
            phase_input=phase_input,
        )
        if record.multi_agent_run is not None:
            state.v7_result = AgentsRcaRuntimeResult(
                tasks=self._orchestrator.repository.list_tasks(record.id),
                executions=self._orchestrator.repository.list_executions(record.id),
                findings=self._orchestrator.repository.list_agent_findings(record.id),
                review=self._orchestrator.repository.get_coordination_review(record.id),
                run_summary=record.multi_agent_run,
            )
        return state

    @staticmethod
    def _required(
        state: _DiagnosisState,
    ) -> tuple[InvestigationRecord, IncidentEvent]:
        if state.record is None or state.safe_event is None:
            raise RuntimeError("intake phase has not completed")
        return state.record, state.safe_event
