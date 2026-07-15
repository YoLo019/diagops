from __future__ import annotations

import csv
import json
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
from backend.db.models import InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.agents_runtime import AgentsRcaRuntime, AgentsRcaRuntimeResult
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import FailureCategory, InvestigationStrategy
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
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


class BenchmarkCaseRunner(Protocol):
    def run_case(
        self, case: OpenRcaRuntimeCase, strategy: InvestigationStrategy
    ) -> BenchmarkCaseOutcome: ...


class _CapturingAgentsRuntime(AgentsRcaRuntime):
    last_result: AgentsRcaRuntimeResult | None = None

    async def run(self, *args, **kwargs) -> AgentsRcaRuntimeResult:
        self.last_result = await super().run(*args, **kwargs)
        return self.last_result


class OpenRcaDiagnosisRunner:
    def __init__(self, dataset_root: Path, model: str) -> None:
        self.dataset_root = dataset_root
        self.model = model

    def run_case(
        self, case: OpenRcaRuntimeCase, strategy: InvestigationStrategy
    ) -> BenchmarkCaseOutcome:
        started = perf_counter()
        providers = ProviderRegistry(
            [
                OpenRcaLogProvider(self.dataset_root, case),
                OpenRcaMetricProvider(self.dataset_root, case),
                OpenRcaDependencyProvider(self.dataset_root, case),
            ]
        )
        registry = build_provider_tool_registry(providers)
        runtime = _CapturingAgentsRuntime(
            model=self.model,
            strategy=strategy,
            tool_registry=registry,
        )
        repository = InMemoryInvestigationRepository()
        orchestrator = DiagnosisOrchestrator(
            repository=repository,
            providers=providers,
            analyzer=RcaAnalyzer(),
            report_generator=ReportGenerator(),
            coordinator=DiagnosisCoordinator(providers),
            agents_runtime=runtime,
            default_strategy=strategy,
        )
        record = orchestrator.run(
            IncidentEvent(
                source=IncidentSource.SIMULATED,
                service=case.service,
                environment="openrca",
                severity=Severity.CRITICAL,
                title=f"OpenRCA {case.case_id}",
                description=case.instruction,
                started_at=case.start_time,
                time_window_minutes=max(
                    1, int((case.end_time - case.start_time).total_seconds() / 60)
                ),
            ),
            strategy=strategy,
        )
        review = repository.get_coordination_review(record.id)
        root_causes = list(review.root_causes) if review is not None else []
        evidence_ids = {item.id for item in record.evidence}
        references = [
            evidence_id
            for cause in root_causes
            for evidence_id in cause.supporting_evidence_ids
        ]
        invalid_references = sum(item not in evidence_ids for item in references)
        calls = repository.list_tool_calls(record.id)
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
        executions = repository.list_executions(record.id)
        invalid_references += sum(
            execution.failure_category == FailureCategory.INVALID_REFERENCE
            for execution in executions
        )
        runtime_result = runtime.last_result
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
            input_tokens=runtime_result.input_tokens if runtime_result else 0,
            output_tokens=runtime_result.output_tokens if runtime_result else 0,
            read_only_violations=read_only_violations,
        )


@dataclass(frozen=True)
class BenchmarkRunResult:
    run_id: str
    output_dir: Path


def run_benchmark_pair(
    case_runner: BenchmarkCaseRunner,
    safe_index: Path,
    output_root: Path,
    *,
    model: str,
    prompt_version: str = "v8.2",
    input_cost_per_million: float = 0,
    output_cost_per_million: float = 0,
    strategies: tuple[InvestigationStrategy, ...] = (
        InvestigationStrategy.FIXED,
        InvestigationStrategy.ADAPTIVE,
    ),
) -> BenchmarkRunResult:
    index = OpenRcaRuntimeIndex.model_validate_json(
        safe_index.read_text(encoding="utf-8")
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
        "provider": "openai",
        "prompt_version": prompt_version,
        "git_commit": git_commit,
        "strategies": [item.value for item in strategies],
        "strategy_config": {
            "max_rounds": 2,
            "max_tool_calls_per_specialist": 3,
            "max_total_tool_calls": 8,
            "timeout_seconds": 60,
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
                    "task_index": case.case_id.rsplit(":", 1)[-1],
                    "prediction": _official_prediction(outcome.root_causes),
                }
            )
            _write_predictions(
                output_dir / f"{strategy.value}-predictions.csv",
                rows[strategy],
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
            fieldnames=("case_id", "partition", "task_index", "prediction"),
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


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
