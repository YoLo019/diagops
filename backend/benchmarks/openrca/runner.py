from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol

from backend.benchmarks.openrca.models import (
    OpenRcaBenchmarkSummary,
    OpenRcaRuntimeCase,
    OpenRcaRuntimeIndex,
    OpenRcaStrategySummary,
)
from backend.benchmarks.openrca.projection import (
    ProjectionAudit,
    project_root_causes,
    project_v11_candidates,
)
from backend.benchmarks.openrca.providers import (
    OpenRcaDependencyProvider,
    OpenRcaLogProvider,
    OpenRcaMetricProvider,
)
from backend.config.settings import canonicalize_endpoint, endpoint_id
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.diagnosis.agents_runtime import AgentsRcaRuntime
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.diagnostic_skills import skill_catalog_identity
from backend.diagnosis.openai_compatible_model import (
    OpenAICompatibleChatCompletionsModel,
)
from backend.diagnosis.openai_model import OFFICIAL_OPENAI_BASE_URL
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.diagnosis.v11_runtime import V11Runtime
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import (
    AuthorityMode,
    ExecutionContractVersion,
    FailureCategory,
    InvestigationStrategy,
    ModelProvider,
)
from backend.domain.runtime import (
    RuntimeEventType,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    seal_v11_execution_contract,
)
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.telemetry import RuntimeTelemetry
from backend.runtime.writer import RuntimeWriter
from backend.services.model_capability import latest_capability_artifact
from backend.services.v11_projection import ensure_v11_projection_owner
from backend.tools.provider_tools import build_provider_tool_registry
from backend.tools.registry import agent_manifest_hash


@dataclass(frozen=True)
class BenchmarkCaseOutcome:
    root_causes: list[RootCauseAttribution] = field(default_factory=list)
    completed: bool = False
    failure_category: str | None = None
    evidence_reference_count: int = 0
    invalid_evidence_reference_count: int = 0
    tool_call_count: int = 0
    duplicate_query_rejections: int = 0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    read_only_violations: int = 0
    runtime_run_id: str | None = None
    projection_audit: ProjectionAudit | None = None


class BenchmarkCaseRunner(Protocol):
    def run_case(
        self, case: OpenRcaRuntimeCase, strategy: InvestigationStrategy
    ) -> BenchmarkCaseOutcome: ...


def _runtime_token_usage(runtime_store, run_id: str) -> tuple[int, int]:
    events = (
        item
        for item in runtime_store.list_events(run_id)
        if item.event_type == RuntimeEventType.MODEL_COMPLETED
    )
    payloads = [item.safe_payload for item in events]
    return (
        sum(int(item.get("input_tokens", 0)) for item in payloads),
        sum(int(item.get("output_tokens", 0)) for item in payloads),
    )


class OpenRcaDiagnosisRunner:
    def __init__(
        self,
        dataset_root: Path,
        model: object | None,
        *,
        repository,
        runtime_store,
        provider: ModelProvider = ModelProvider.OPENAI,
        prompt_version: str = "v8.2",
        timeout_seconds: float = 60,
        deterministic: bool = False,
        mode: str | None = None,
        turn=None,
    ) -> None:
        self.dataset_root = dataset_root
        self.model = model
        self.provider = provider
        self.prompt_version = prompt_version
        self.timeout_seconds = timeout_seconds
        self.repository = repository
        self.runtime_store = runtime_store
        self.deterministic = deterministic
        self.mode = mode or ("deterministic" if deterministic else "agent")
        self.turn = turn

    def configure_runtime(
        self, *, provider, model, prompt_version, timeout_seconds
    ) -> None:
        self.provider = ModelProvider(provider)
        if (
            self.provider in {ModelProvider.DEEPSEEK, ModelProvider.OPENAI_COMPATIBLE}
            and isinstance(self.model, OpenAICompatibleChatCompletionsModel)
        ):
            self.model = self.model.clone_for_model(model, max_retries=0)
        else:
            self.model = model
        self.prompt_version = prompt_version
        self.timeout_seconds = timeout_seconds

    def run_case(
        self, case: OpenRcaRuntimeCase, strategy: InvestigationStrategy
    ) -> BenchmarkCaseOutcome:
        started = perf_counter()
        record = InvestigationRecord(
            event=_benchmark_event(case),
            strategy=strategy,
            runtime_available=True,
        )
        providers = ProviderRegistry(
            [
                OpenRcaLogProvider(self.dataset_root, case, evidence_namespace=record.id),
                OpenRcaMetricProvider(self.dataset_root, case, evidence_namespace=record.id),
                OpenRcaDependencyProvider(self.dataset_root, case, evidence_namespace=record.id),
            ]
        )
        registry = build_provider_tool_registry(providers)
        runtime = None
        v11_runtime = None
        model_name = self._model_name()
        if self.mode == "v11-agent":
            v11_runtime = V11Runtime(
                model=self.model,
                model_provider=self.provider,
                model_name=model_name,
                tool_registry=registry,
                turn=self.turn,
                timeout_seconds=min(120.0, self.timeout_seconds),
                token_budget=10_000,
            )
        elif not self.deterministic:
            runtime = AgentsRcaRuntime(
                model=self.model,
                strategy=strategy,
                tool_registry=registry,
                model_provider=self.provider,
                model_name=self.model,
                prompt_version=self.prompt_version,
                timeout_seconds=self.timeout_seconds,
            )
        orchestrator = DiagnosisOrchestrator(
            repository=self.repository,
            providers=providers,
            analyzer=RcaAnalyzer(),
            report_generator=ReportGenerator(),
            coordinator=DiagnosisCoordinator(providers),
            agents_runtime=runtime,
            v11_runtime=v11_runtime,
            default_strategy=strategy,
        )
        record = self.repository.save(record)
        execution_contract = (
            self._v11_execution_contract(v11_runtime)
            if v11_runtime is not None
            else {}
        )
        runtime_run = self.runtime_store.create_run(
            RuntimeRun(
                investigation_id=record.id,
                run_kind=RuntimeRunKind.LIVE,
                strategy=strategy,
                run_reason=RuntimeRunReason.INITIAL,
                model_provider=(
                    self.provider
                    if not self.deterministic
                    else None
                ),
                model_name="deterministic" if self.deterministic else model_name,
                prompt_version=self.prompt_version,
                tool_budget=8,
                token_budget=10_000 if v11_runtime is not None else None,
                timeout_seconds=(
                    min(120.0, self.timeout_seconds)
                    if v11_runtime is not None
                    else self.timeout_seconds
                ),
                execution_contract_version=(
                    ExecutionContractVersion.V11
                    if v11_runtime is not None
                    else ExecutionContractVersion.V10_LEGACY
                ),
                authority_mode=(
                    AuthorityMode.AGENT
                    if v11_runtime is not None
                    else AuthorityMode.LEGACY_DETERMINISTIC
                ),
                execution_contract=execution_contract,
            )
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
                await coordinator.execute(
                    runtime_run.id,
                    owner=f"openrca-{runtime_run.id}",
                )
            finally:
                await coordinator.shutdown()

        try:
            asyncio.run(execute())
        except Exception as exc:
            input_tokens, output_tokens = _runtime_token_usage(self.runtime_store, runtime_run.id)
            return BenchmarkCaseOutcome(
                failure_category=f"{type(exc).__name__}: benchmark case failed",
                duration_ms=round((perf_counter() - started) * 1000),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                runtime_run_id=runtime_run.id,
            )
        runtime_run = self.runtime_store.get_run(runtime_run.id)
        record = self.repository.get(record.id)
        review = self.repository.get_coordination_review(record.id)
        authoritative_root_causes = list(review.root_causes) if review is not None else []
        projection_failed = False
        try:
            if self.mode == "v11-agent":
                ensure_v11_projection_owner(
                    self.repository, self.runtime_store, record
                )
                candidates = (
                    [
                        candidate
                        for candidate in review.candidates
                        if candidate.id in review.authoritative_candidate_ids
                    ]
                    if review is not None
                    else []
                )
                projection = project_v11_candidates(
                    task_index=case.task_index,
                    expected_count=case.expected_root_cause_count or 1,
                    evidence=list(record.evidence),
                    candidates=candidates,
                    fallback_timestamp=record.event.started_at,
                    runtime_run_id=runtime_run.id,
                )
            else:
                projection = project_root_causes(
                    task_index=case.task_index,
                    expected_count=case.expected_root_cause_count or 1,
                    evidence=list(record.evidence),
                    root_causes=authoritative_root_causes,
                )
            root_causes = list(projection.causes)
            projection_audit = projection.audit
        except Exception:
            root_causes = []
            projection_audit = ProjectionAudit.failed(case.task_index)
            projection_failed = True
        evidence_ids = {item.id for item in record.evidence}
        references = [
            evidence_id for cause in root_causes for evidence_id in cause.supporting_evidence_ids
        ]
        invalid_references = sum(item not in evidence_ids for item in references)
        calls = self.repository.list_tool_calls(record.id)
        duplicate_rejections = sum(
            "duplicate query" in (call.error_message or "").casefold() for call in calls
        )
        read_only_violations = 0
        for call in calls:
            try:
                spec = registry.get(call.tool_name)
            except ValueError:
                read_only_violations += 1
            else:
                read_only_violations += int(not spec.read_only)
        executions = self.repository.list_executions(record.id)
        invalid_references += sum(
            execution.failure_category == FailureCategory.INVALID_REFERENCE
            for execution in executions
        )
        summary = record.multi_agent_run
        failure_category = (
            summary.adaptive_stop_reason.value
            if summary is not None and summary.adaptive_stop_reason is not None
            else (summary.failure_reason if summary is not None else None)
        )
        if projection_failed:
            failure_category = "projection_error"
        elif not root_causes and not failure_category:
            failure_category = "missing_root_cause"
        input_tokens, output_tokens = _runtime_token_usage(self.runtime_store, runtime_run.id)
        return BenchmarkCaseOutcome(
            root_causes=root_causes,
            completed=record.status == InvestigationStatus.COMPLETED
            and not projection_failed,
            failure_category=failure_category,
            evidence_reference_count=len(references),
            invalid_evidence_reference_count=invalid_references,
            tool_call_count=len(calls),
            duplicate_query_rejections=duplicate_rejections,
            duration_ms=round((perf_counter() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            read_only_violations=read_only_violations,
            runtime_run_id=runtime_run.id,
            projection_audit=projection_audit,
        )

    def _model_name(self) -> str:
        if isinstance(self.model, str):
            return self.model
        configured_name = getattr(self.model, "model", None)
        return configured_name if isinstance(configured_name, str) else type(self.model).__name__

    def _v11_execution_contract(self, runtime: V11Runtime) -> dict[str, object]:
        provider = self.provider.value
        model_name = self._model_name()
        api_mode = (
            "responses"
            if self.provider == ModelProvider.OPENAI
            else "chat_completions"
        )
        endpoint, capability_hash = self._v11_model_identity(model_name)
        manifest = runtime.tool_registry.agent_manifest()
        contract = {
            "execution_contract_version": ExecutionContractVersion.V11.value,
            "authority_mode": AuthorityMode.AGENT.value,
            "model_provider": provider,
            "model_name": model_name,
            "prompt_version": self.prompt_version,
            "api_mode": api_mode,
            "endpoint_id": endpoint,
            "capability_artifact_hash": capability_hash,
            "tool_manifest": list(manifest),
            "tool_manifest_hash": agent_manifest_hash(manifest),
            "skill_catalog": skill_catalog_identity(
                runtime.tool_registry.list_agent_specs()
            ),
            "capability_identity": {
                "provider": provider,
                "model": model_name,
                "api_mode": api_mode,
                "endpoint_id": endpoint,
                "artifact_hash": capability_hash,
            },
            "limits": {
                "max_turns": runtime.max_turns,
                "max_investigators": runtime.max_investigators,
                "max_rounds": runtime.max_rounds,
                "token_budget": 10_000,
                "max_tool_calls_per_specialist": runtime.max_tool_calls_per_specialist,
                "tool_timeout_seconds": runtime.tool_timeout_seconds,
            },
            "retry_policy": {
                "max_retries": 1,
                "retryable_categories": ["transport", "rate_limit"],
                "provider_max_retries": 0,
                "sdk_max_retries": 0,
            },
            "tool_budget": runtime.max_total_tool_calls,
            "token_budget": 10_000,
            "timeout_seconds": min(120.0, self.timeout_seconds),
        }
        return seal_v11_execution_contract(contract)

    def _v11_model_identity(self, model_name: str) -> tuple[str | None, str | None]:
        """冻结 OpenRCA V11 的端点身份；兼容端点必须先通过 capability gate。"""
        if self.provider == ModelProvider.OPENAI:
            return endpoint_id(OFFICIAL_OPENAI_BASE_URL), None
        if self.provider != ModelProvider.OPENAI_COMPATIBLE:
            return f"openrca-{self.provider.value}", None
        if not isinstance(self.model, OpenAICompatibleChatCompletionsModel):
            raise ValueError(
                "OpenRCA V11 openai_compatible requires the configured model adapter"
            )
        try:
            identity = endpoint_id(canonicalize_endpoint(self.model._base_url))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("OpenRCA V11 compatible endpoint is unavailable") from exc
        artifact = latest_capability_artifact(
            Path("output/model_capability"),
            provider=ModelProvider.OPENAI_COMPATIBLE.value,
            model=model_name,
            endpoint_id_value=identity,
        )
        if artifact is None or artifact.result != "passed":
            raise ValueError(
                "OpenRCA V11 compatible endpoint lacks a passed capability artifact"
            )
        return identity, artifact.artifact_hash

    def register_replay_artifact(
        self,
        *,
        case: OpenRcaRuntimeCase,
        strategy: InvestigationStrategy,
        benchmark_run_id: str,
        runtime_run_id: str,
        prediction_path: Path,
        prediction: str,
    ) -> None:
        if self.deterministic:
            return
        scoring_points = _scoring_points(self.dataset_root, case)
        self.runtime_store.set_benchmark_replay_locator(
            runtime_run_id,
            {
                "schema_version": 1,
                "benchmark_run_id": benchmark_run_id,
                "strategy": strategy.value,
                "provider": self.provider.value,
                "model": self._model_name(),
                "prompt_version": self.prompt_version,
                "case_id": case.case_id,
                "partition": case.partition.value,
                "row_id": case.row_id,
                "prediction_root": str(prediction_path.parent.resolve()),
                "prediction_file": prediction_path.name,
                "prediction_sha256": hashlib.sha256(prediction.encode("utf-8")).hexdigest(),
                "query_root": str(self.dataset_root.resolve()),
                "scoring_sha256": hashlib.sha256(scoring_points.encode("utf-8")).hexdigest(),
            },
        )


@dataclass(frozen=True)
class BenchmarkRunResult:
    run_id: str
    output_dir: Path


def _benchmark_event(case: OpenRcaRuntimeCase) -> IncidentEvent:
    duration = case.end_time - case.start_time
    return IncidentEvent(
        source=IncidentSource.SIMULATED,
        service=case.service,
        environment="openrca",
        severity=Severity.CRITICAL,
        title=f"OpenRCA {case.case_id}",
        description=case.instruction,
        started_at=case.start_time + duration / 2,
        time_window_minutes=max(1, math.ceil(duration.total_seconds() / 120)),
    )


def run_benchmark_pair(
    case_runner: BenchmarkCaseRunner,
    safe_index: Path,
    output_root: Path,
    *,
    model: str,
    provider: ModelProvider = ModelProvider.OPENAI,
    prompt_version: str = "v8.2",
    timeout_seconds: float = 60,
    input_cost_per_million: float = 0,
    output_cost_per_million: float = 0,
    strategies: tuple[InvestigationStrategy, ...] = (
        InvestigationStrategy.FIXED,
        InvestigationStrategy.ADAPTIVE,
    ),
    mode: str = "agent",
) -> BenchmarkRunResult:
    cost_rates = (input_cost_per_million, output_cost_per_million)
    if any(not math.isfinite(rate) or rate < 0 for rate in cost_rates):
        raise ValueError("cost rates must be finite and non-negative")
    index = OpenRcaRuntimeIndex.model_validate_json(safe_index.read_text(encoding="utf-8"))
    if mode not in {"agent", "agent-shadow", "v11-agent", "deterministic"}:
        raise ValueError("unsupported OpenRCA benchmark mode")
    if mode == "deterministic":
        if strategies != (InvestigationStrategy.FIXED,):
            raise ValueError("deterministic mode only supports fixed strategy")
        if any(case.expected_root_cause_count is None for case in index.cases):
            raise ValueError("deterministic mode requires expected root cause count")
    provider = ModelProvider(provider)
    configure = getattr(case_runner, "configure_runtime", None)
    if configure is not None and mode != "deterministic":
        configure(
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            timeout_seconds=timeout_seconds,
        )
    if hasattr(case_runner, "mode"):
        case_runner.mode = mode
    started_at = datetime.now(UTC)
    run_id = started_at.strftime("run-%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True)
    git_commit = _git_commit()
    manifest = {
        "run_id": run_id,
        "case_manifest_hash": index.case_manifest_hash,
        "model": model,
        "provider": None if mode == "deterministic" else provider.value,
        "mode": mode,
        "prompt_version": prompt_version,
        "git_commit": git_commit,
        "strategies": [item.value for item in strategies],
        "strategy_config": {
            "max_rounds": 2,
            "max_tool_calls_per_specialist": 3,
            "max_total_tool_calls": 8,
            "timeout_seconds": timeout_seconds,
        },
        "input_cost_per_million": input_cost_per_million,
        "output_cost_per_million": output_cost_per_million,
        "started_at": started_at.isoformat(),
        "completed_at": None,
    }
    _write_json_atomic(output_dir / "run-manifest.json", manifest)
    _write_text_atomic(output_root / "latest-run.txt", f"{run_id}\n")

    rows = {strategy: [] for strategy in strategies}
    outcomes = {strategy: [] for strategy in strategies}
    for case in index.cases:
        for strategy in strategies:
            try:
                outcome = case_runner.run_case(case, strategy)
            except Exception as exc:
                outcome = BenchmarkCaseOutcome(
                    failure_category=f"{type(exc).__name__}: benchmark case failed"
                )
            outcomes[strategy].append((case, outcome))
            published_root_causes = outcome.root_causes
            publication_failure_category = outcome.failure_category
            if mode == "v11-agent" and not outcome.completed:
                published_root_causes = []
                publication_failure_category = publication_failure_category or "failed"
            metadata = {
                "expected_root_cause_count": (case.expected_root_cause_count),
                "runtime_run_id": outcome.runtime_run_id,
                "projection": (
                    outcome.projection_audit.metadata()
                    if outcome.projection_audit is not None
                    else None
                ),
            }
            if mode == "v11-agent":
                metadata["failure_category"] = publication_failure_category
            rows[strategy].append(
                {
                    "case_id": case.case_id,
                    "partition": case.partition.value,
                    "row_id": case.row_id,
                    "task_index": case.task_index,
                    "prediction": _official_prediction(
                        published_root_causes,
                        case.expected_root_cause_count,
                    ),
                    "metadata": json.dumps(
                        metadata,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
            _write_predictions(
                output_dir / f"{strategy.value}-predictions.csv",
                rows[strategy],
            )
            if mode == "v11-agent" and strategy == InvestigationStrategy.FIXED:
                _write_predictions(
                    output_dir / "v11-agent-predictions.csv",
                    rows[strategy],
                )
            register = getattr(case_runner, "register_replay_artifact", None)
            if register is not None and outcome.runtime_run_id is not None:
                register(
                    case=case,
                    strategy=strategy,
                    benchmark_run_id=run_id,
                    runtime_run_id=outcome.runtime_run_id,
                    prediction_path=(output_dir / f"{strategy.value}-predictions.csv"),
                    prediction=rows[strategy][-1]["prediction"],
                )
            partition_rows = [
                row for row in rows[strategy] if row["partition"] == case.partition.value
            ]
            _write_predictions(
                output_dir / f"{strategy.value}-{case.partition.value.replace('/', '-')}.csv",
                partition_rows,
            )

    completed_at = datetime.now(UTC)
    manifest["completed_at"] = completed_at.isoformat()
    _write_json_atomic(output_dir / "run-manifest.json", manifest)
    summary = OpenRcaBenchmarkSummary(
        run_id=run_id,
        case_count=len(index.cases),
        model=model,
        prompt_version=prompt_version,
        git_commit=git_commit,
        started_at=started_at,
        completed_at=completed_at,
        strategies={
            strategy.value: _summarize(
                outcomes[strategy],
                input_cost_per_million,
                output_cost_per_million,
            )
            for strategy in strategies
        },
    )
    _write_text_atomic(output_dir / "summary.json", summary.model_dump_json(indent=2) + "\n")
    return BenchmarkRunResult(run_id=run_id, output_dir=output_dir)


def _official_prediction(
    root_causes: list[RootCauseAttribution],
    expected_count: int | None = None,
) -> str:
    selected = root_causes
    if expected_count is not None:
        selected = selected[:expected_count]
    payload = {
        str(index): {
            "root cause occurrence datetime": item.root_cause_occurred_at.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "root cause component": item.root_cause_component,
            "root cause reason": item.root_cause_reason,
        }
        for index, item in enumerate(selected, start=1)
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _summarize(
    outcomes: list[tuple[OpenRcaRuntimeCase, BenchmarkCaseOutcome]],
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> OpenRcaStrategySummary:
    count = len(outcomes)
    completed = sum(outcome.completed for _, outcome in outcomes)
    reference_count = sum(outcome.evidence_reference_count for _, outcome in outcomes)
    invalid_references = sum(outcome.invalid_evidence_reference_count for _, outcome in outcomes)
    input_tokens = sum(outcome.input_tokens for _, outcome in outcomes)
    output_tokens = sum(outcome.output_tokens for _, outcome in outcomes)
    failures = [
        {"case_id": case.case_id, "category": outcome.failure_category or "failed"}
        for case, outcome in outcomes
        if not outcome.completed or outcome.failure_category
    ]
    return OpenRcaStrategySummary(
        case_count=count,
        completed_count=completed,
        completion_rate=completed / count if count else 0,
        evidence_reference_validity=(
            (reference_count - invalid_references) / reference_count
            if reference_count
            else float(invalid_references == 0)
        ),
        invalid_evidence_references=invalid_references,
        read_only_violations=sum(outcome.read_only_violations for _, outcome in outcomes),
        average_tool_calls=(
            sum(outcome.tool_call_count for _, outcome in outcomes) / count if count else 0
        ),
        duplicate_query_rejections=sum(
            outcome.duplicate_query_rejections for _, outcome in outcomes
        ),
        average_duration_ms=(
            sum(outcome.duration_ms for _, outcome in outcomes) / count if count else 0
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=(
            input_tokens * input_cost_per_million + output_tokens * output_cost_per_million
        )
        / 1_000_000,
        failed_cases=failures,
        projection_errors=sum(
            bool(outcome.projection_audit and outcome.projection_audit.projection_error)
            for _, outcome in outcomes
        ),
        projection_fallbacks=sum(
            bool(outcome.projection_audit and outcome.projection_audit.projection_fallback)
            for _, outcome in outcomes
        ),
    )


def _write_predictions(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=(
                "case_id",
                "partition",
                "row_id",
                "task_index",
                "prediction",
                "metadata",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _scoring_points(dataset_root: Path, case: OpenRcaRuntimeCase) -> str:
    path = dataset_root / case.partition.value / "query.csv"
    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    try:
        return rows[int(case.row_id)].get("scoring_points", "")
    except (IndexError, ValueError) as exc:
        raise ValueError("OpenRCA scoring row identity is invalid") from exc


def _write_json_atomic(path: Path, value: object) -> None:
    _write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text_atomic(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _git_commit() -> str:
    root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    return completed.stdout.strip() or "unknown"
