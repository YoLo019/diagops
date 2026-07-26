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
from backend.benchmarks.openrca.providers import (
    OpenRcaDependencyProvider,
    OpenRcaLogProvider,
    OpenRcaMetricProvider,
)
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.diagnosis.agents_runtime import AgentsRcaRuntime, AgentsRcaRuntimeResult
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import FailureCategory, InvestigationStrategy, ModelProvider
from backend.domain.runtime import (
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
)
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.telemetry import RuntimeTelemetry
from backend.runtime.writer import RuntimeWriter
from backend.tools.provider_tools import build_provider_tool_registry


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


class BenchmarkCaseRunner(Protocol):
    def run_case(
        self, case: OpenRcaRuntimeCase, strategy: InvestigationStrategy
    ) -> BenchmarkCaseOutcome: ...


class _CapturingAgentsRuntime(AgentsRcaRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # durable Phase clone 使用浅复制；共享结果引用才能包含后续 review/synthesis usage。
        self._captured_results: list[AgentsRcaRuntimeResult] = []

    async def run(self, *args, **kwargs) -> AgentsRcaRuntimeResult:
        result = await super().run(*args, **kwargs)
        self._captured_results.append(result)
        return result

    @property
    def input_tokens(self) -> int:
        return sum(item.input_tokens for item in self._captured_results)

    @property
    def output_tokens(self) -> int:
        return sum(item.output_tokens for item in self._captured_results)


class OpenRcaDiagnosisRunner:
    def __init__(
        self,
        dataset_root: Path,
        model: str,
        *,
        repository,
        runtime_store,
        provider: ModelProvider = ModelProvider.OPENAI,
        prompt_version: str = "v8.2",
        timeout_seconds: float = 60,
    ) -> None:
        self.dataset_root = dataset_root
        self.model = model
        self.provider = provider
        self.prompt_version = prompt_version
        self.timeout_seconds = timeout_seconds
        self.repository = repository
        self.runtime_store = runtime_store

    def configure_runtime(
        self, *, provider, model, prompt_version, timeout_seconds
    ) -> None:
        self.provider = ModelProvider(provider)
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
                OpenRcaLogProvider(
                    self.dataset_root, case, evidence_namespace=record.id
                ),
                OpenRcaMetricProvider(
                    self.dataset_root, case, evidence_namespace=record.id
                ),
                OpenRcaDependencyProvider(
                    self.dataset_root, case, evidence_namespace=record.id
                ),
            ]
        )
        registry = build_provider_tool_registry(providers)
        runtime = _CapturingAgentsRuntime(
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
            default_strategy=strategy,
        )
        record = self.repository.save(record)
        runtime_run = self.runtime_store.create_run(
            RuntimeRun(
                investigation_id=record.id,
                run_kind=RuntimeRunKind.LIVE,
                strategy=strategy,
                run_reason=RuntimeRunReason.INITIAL,
                model_provider=self.provider,
                model_name=(
                    self.model
                    if isinstance(self.model, str)
                    else type(self.model).__name__
                ),
                prompt_version=self.prompt_version,
                tool_budget=8,
                timeout_seconds=self.timeout_seconds,
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
            return BenchmarkCaseOutcome(
                failure_category=f"{type(exc).__name__}: benchmark case failed",
                duration_ms=round((perf_counter() - started) * 1000),
                input_tokens=runtime.input_tokens,
                output_tokens=runtime.output_tokens,
                runtime_run_id=runtime_run.id,
            )
        runtime_run = self.runtime_store.get_run(runtime_run.id)
        record = self.repository.get(record.id)
        review = self.repository.get_coordination_review(record.id)
        root_causes = list(review.root_causes) if review is not None else []
        evidence_ids = {item.id for item in record.evidence}
        references = [
            evidence_id
            for cause in root_causes
            for evidence_id in cause.supporting_evidence_ids
        ]
        invalid_references = sum(item not in evidence_ids for item in references)
        calls = self.repository.list_tool_calls(record.id)
        duplicate_rejections = sum(
            "duplicate query" in (call.error_message or "").casefold()
            for call in calls
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
        if not root_causes and not failure_category:
            failure_category = "missing_root_cause"
        return BenchmarkCaseOutcome(
            root_causes=root_causes,
            completed=record.status == InvestigationStatus.COMPLETED,
            failure_category=failure_category,
            evidence_reference_count=len(references),
            invalid_evidence_reference_count=invalid_references,
            tool_call_count=len(calls),
            duplicate_query_rejections=duplicate_rejections,
            duration_ms=round((perf_counter() - started) * 1000),
            input_tokens=runtime.input_tokens,
            output_tokens=runtime.output_tokens,
            read_only_violations=read_only_violations,
            runtime_run_id=runtime_run.id,
        )

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
        scoring_points = _scoring_points(self.dataset_root, case)
        self.runtime_store.set_benchmark_replay_locator(
            runtime_run_id,
            {
                "schema_version": 1,
                "benchmark_run_id": benchmark_run_id,
                "strategy": strategy.value,
                "provider": self.provider.value,
                "model": self.model,
                "prompt_version": self.prompt_version,
                "case_id": case.case_id,
                "partition": case.partition.value,
                "row_id": case.row_id,
                "prediction_root": str(prediction_path.parent.resolve()),
                "prediction_file": prediction_path.name,
                "prediction_sha256": hashlib.sha256(
                    prediction.encode("utf-8")
                ).hexdigest(),
                "query_root": str(self.dataset_root.resolve()),
                "scoring_sha256": hashlib.sha256(
                    scoring_points.encode("utf-8")
                ).hexdigest(),
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
) -> BenchmarkRunResult:
    cost_rates = (input_cost_per_million, output_cost_per_million)
    if any(not math.isfinite(rate) or rate < 0 for rate in cost_rates):
        raise ValueError("cost rates must be finite and non-negative")
    index = OpenRcaRuntimeIndex.model_validate_json(
        safe_index.read_text(encoding="utf-8")
    )
    provider = ModelProvider(provider)
    configure = getattr(case_runner, "configure_runtime", None)
    if configure is not None:
        configure(
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            timeout_seconds=timeout_seconds,
        )
    started_at = datetime.now(UTC)
    run_id = started_at.strftime("run-%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / run_id
    output_dir.mkdir(parents=True)
    git_commit = _git_commit()
    manifest = {
        "run_id": run_id,
        "case_manifest_hash": index.case_manifest_hash,
        "model": model,
        "provider": provider.value,
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
            rows[strategy].append(
                {
                    "case_id": case.case_id,
                    "partition": case.partition.value,
                    "row_id": case.row_id,
                    "task_index": case.task_index,
                    "prediction": _official_prediction(outcome.root_causes),
                    "metadata": json.dumps(
                        {"runtime_run_id": outcome.runtime_run_id},
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
            register = getattr(case_runner, "register_replay_artifact", None)
            if register is not None and outcome.runtime_run_id is not None:
                register(
                    case=case,
                    strategy=strategy,
                    benchmark_run_id=run_id,
                    runtime_run_id=outcome.runtime_run_id,
                    prediction_path=(
                        output_dir / f"{strategy.value}-predictions.csv"
                    ),
                    prediction=rows[strategy][-1]["prediction"],
                )
            partition_rows = [
                row
                for row in rows[strategy]
                if row["partition"] == case.partition.value
            ]
            _write_predictions(
                output_dir
                / f"{strategy.value}-{case.partition.value.replace('/', '-')}.csv",
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
    _write_text_atomic(
        output_dir / "summary.json", summary.model_dump_json(indent=2) + "\n"
    )
    return BenchmarkRunResult(run_id=run_id, output_dir=output_dir)


def _official_prediction(root_causes: list[RootCauseAttribution]) -> str:
    payload = {
        str(index): {
            "root cause occurrence datetime": item.root_cause_occurred_at.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "root cause component": item.root_cause_component,
            "root cause reason": item.root_cause_reason,
        }
        for index, item in enumerate(
            sorted(root_causes, key=lambda cause: cause.root_cause_occurred_at),
            start=1,
        )
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _summarize(
    outcomes: list[tuple[OpenRcaRuntimeCase, BenchmarkCaseOutcome]],
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> OpenRcaStrategySummary:
    count = len(outcomes)
    completed = sum(outcome.completed for _, outcome in outcomes)
    reference_count = sum(
        outcome.evidence_reference_count for _, outcome in outcomes
    )
    invalid_references = sum(
        outcome.invalid_evidence_reference_count for _, outcome in outcomes
    )
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
        read_only_violations=sum(
            outcome.read_only_violations for _, outcome in outcomes
        ),
        average_tool_calls=(
            sum(outcome.tool_call_count for _, outcome in outcomes) / count
            if count
            else 0
        ),
        duplicate_query_rejections=sum(
            outcome.duplicate_query_rejections for _, outcome in outcomes
        ),
        average_duration_ms=(
            sum(outcome.duration_ms for _, outcome in outcomes) / count
            if count
            else 0
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=(
            input_tokens * input_cost_per_million
            + output_tokens * output_cost_per_million
        )
        / 1_000_000,
        failed_cases=failures,
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
    _write_text_atomic(
        path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


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
