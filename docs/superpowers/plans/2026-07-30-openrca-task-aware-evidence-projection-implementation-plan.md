# OpenRCA Task-Aware Evidence Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, benchmark-only Evidence Projector that uses OpenRCA `task_index` without changing production RCA behavior, and require a passing six-case targeted Gate before another formal 40-case run.

**Architecture:** The existing Providers, Analyzer, Hypotheses, CoordinationReview, Runtime, and Replay remain authoritative and unchanged. `OpenRcaDiagnosisRunner` passes the already-produced Evidence, Hypotheses, and root causes to a new module under `backend/benchmarks/openrca/`; the module projects only the fields scored by the current task, returns Evidence-linked causes plus audit metadata, and never writes back to production state.

**Tech Stack:** Python 3.11, Pydantic domain models, pytest, Ruff, existing OpenRCA compatible and official evaluators.

**Status:** `approved`

**Execution outcome:** `blocked at Task 4 targeted Gate; Tasks 1–3 and Task 4
Steps 1–4 completed, Task 5 was not executed`

---

## 1. Scope and file map

### Create

- `backend/benchmarks/openrca/projection.py`
  - Owns task-field mapping, candidate construction, deduplication, deterministic ordering, fallback, and audit metadata.
- `tests/benchmarks/test_openrca_projection.py`
  - Owns projector unit tests with synthetic Evidence and Hypotheses.

### Modify

- `backend/benchmarks/openrca/runner.py`
  - Invokes the Projector after the existing diagnosis completes, writes projected predictions and audit metadata, and leaves persisted Review/Runtime untouched.
- `backend/benchmarks/openrca/models.py`
  - Adds backward-compatible projector counters to benchmark summaries.
- `backend/benchmarks/openrca/evaluator.py`
  - Adds the six-case targeted Gate and permits official query generation for a subset of partitions.
- `backend/benchmarks/openrca/__main__.py`
  - Exposes `targeted-gate` without changing the existing formal `gate`.
- `tests/benchmarks/test_openrca_runner.py`
  - Verifies runner integration, audit metadata, production-state isolation, and safe projection failure.
- `tests/benchmarks/test_openrca_evaluator.py`
  - Verifies subset query generation and every targeted Gate condition.
- `README.md`
  - Documents the targeted workflow and its stop/go rule.
- `docs/superpowers/current.md`
  - Records the real targeted outcome and routes the next action.
- `docs/superpowers/specs/2026-07-30-openrca-task-aware-evidence-projection-design.md`
  - Extends traceability with task/check/status without changing the approved contract.

### Explicitly unchanged

- `backend/rca/analyzer.py`
- `backend/diagnosis/coordination_review.py`
- domain, database, Runtime, Replay, production API, report, frontend, and Provider contracts

No Git commit, push, branch, or PR operation is authorized by this plan. After verification, report the diff and wait for separate user authorization.

## 2. Requirement coverage

| Requirement | Plan task | Focused proof | Final proof |
| --- | --- | --- | --- |
| R1 task mapping | T1 | seven-task parameterized test | benchmark suite |
| R2 benchmark-only boundary | T2 | runner/import audit | full pytest |
| R3 Evidence support | T1, T2 | invalid-reference tests | Evidence validity |
| R4 deterministic ordering | T1 | repeated-output test | repeated fixture run |
| R5 cluster coherence | T1 | task_4–task_7 tests | six-case artifact audit |
| R6 visible fallback/error | T2, T3 | metadata and Gate tests | targeted Gate |
| R7 Ground Truth isolation | T2, T4 | artifact privacy test | source/artifact review |
| R8 production compatibility | T2 | persisted Review/Replay comparison | full pytest |
| R9 six-case threshold | T3, T4 | Gate fixture | official targeted run |
| R10 no early 40-case | T3, T5 | CLI Gate failure | execution checkpoint |
| R11 no production accuracy claim | T4 | documentation review | final report |
| R12 reuse existing Hypotheses | T1, T2 | analyzer call-count and candidate tests | caller audit |

## 3. Execution rules

1. Use TDD for T1–T3: add the smallest failing test, run it and observe RED, then implement.
2. Do not read `record.csv`, scoring points, official reports, or Ground Truth from production code.
3. Do not tune ordering after seeing the six-case result. If targeted Gate fails, record the evidence and stop.
4. Do not run the 40-case benchmark until T4 passes and source identity is reproducible.
5. Preserve the user-owned untracked file `docs/superpowers/openrca-v9-6-case-failure-analysis.md`.

## Task 1: Implement the pure OpenRCA Projector

**Requirements:** R1, R3, R4, R5, R12

**Files:**

- Create: `backend/benchmarks/openrca/projection.py`
- Create: `tests/benchmarks/test_openrca_projection.py`

- [ ] **Step 1: Write synthetic Evidence helpers and task mapping tests**

Add tests that construct only domain objects; do not load OpenRCA query or record files:

```python
from datetime import datetime, timedelta, timezone

import pytest

from backend.benchmarks.openrca.projection import project_root_causes, scored_fields
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis

TZ = timezone(timedelta(hours=8))


def evidence(
    evidence_id: str,
    minute: int,
    component: str,
    signal_type: str,
    deviation: float,
    *,
    provider: EvidenceProvider = EvidenceProvider.METRIC,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=provider,
        kind=(
            EvidenceKind.DEPENDENCY_HEALTH
            if provider == EvidenceProvider.DEPENDENCY
            else EvidenceKind.METRIC_TREND
        ),
        timestamp=datetime(2026, 7, 14, 12, minute, tzinfo=TZ),
        summary=f"{signal_type} on {component}",
        payload={
            "component": component,
            "dependency": component,
            "signal_type": signal_type,
            "deviation_score": deviation,
        },
    )


@pytest.mark.parametrize(
    ("task_index", "expected"),
    [
        ("task_1", ("time",)),
        ("task_2", ("reason",)),
        ("task_3", ("component",)),
        ("task_4", ("time", "reason")),
        ("task_5", ("time", "component")),
        ("task_6", ("component", "reason")),
        ("task_7", ("time", "component", "reason")),
    ],
)
def test_scored_fields_maps_all_openrca_tasks(task_index, expected):
    assert scored_fields(task_index) == expected


def test_scored_fields_rejects_unknown_task():
    with pytest.raises(ValueError, match="task_index"):
        scored_fields("task_8")
```

- [ ] **Step 2: Run the mapping tests and observe RED**

Run:

```powershell
uv run pytest tests/benchmarks/test_openrca_projection.py -q
```

Expected: collection fails because `backend.benchmarks.openrca.projection` does not exist.

- [ ] **Step 3: Add candidate ordering, time projection, deduplication, and coherence tests**

Add these tests before implementation:

```python
def test_time_projection_uses_strongest_supported_evidence_timestamp():
    samples = [
        evidence("ev-early", 0, "checkoutservice", "memory", 2),
        evidence("ev-peak", 18, "checkoutservice", "memory", 9),
    ]
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.RESOURCE_SATURATION,
            summary="memory",
            confidence=0.8,
            supporting_evidence_ids=["ev-early", "ev-peak"],
        )
    ]

    result = project_root_causes(
        task_index="task_1",
        expected_count=1,
        evidence=samples,
        hypotheses=hypotheses,
        root_causes=[],
    )

    assert result.causes[0].root_cause_occurred_at == samples[1].timestamp
    assert result.audit.projection_fallback is False


def test_reason_projection_prefers_specific_signal_over_generic_latency():
    samples = [
        evidence(
            "ev-latency",
            1,
            "paymentservice",
            "latency",
            100,
            provider=EvidenceProvider.DEPENDENCY,
        ),
        evidence("ev-memory", 2, "checkoutservice", "memory", 5),
    ]
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            summary="dependency",
            confidence=0.9,
            supporting_evidence_ids=["ev-latency"],
        ),
        Hypothesis(
            cause_type=CauseType.RESOURCE_SATURATION,
            summary="memory",
            confidence=0.8,
            supporting_evidence_ids=["ev-memory"],
        ),
    ]

    result = project_root_causes(
        task_index="task_2",
        expected_count=1,
        evidence=samples,
        hypotheses=hypotheses,
        root_causes=[],
    )

    assert result.causes[0].root_cause_reason == "container memory load"


def test_component_projection_prefers_distinct_provider_support():
    samples = [
        evidence("ev-a-metric", 1, "service-a", "cpu", 5),
        evidence(
            "ev-a-dependency",
            1,
            "service-a",
            "cpu",
            5,
            provider=EvidenceProvider.DEPENDENCY,
        ),
        evidence("ev-b", 2, "service-b", "cpu", 5),
    ]
    root_causes = [
        RootCauseAttribution(
            root_cause_occurred_at=samples[0].timestamp,
            root_cause_component="service-a",
            root_cause_reason="container cpu load",
            supporting_evidence_ids=["ev-a-metric", "ev-a-dependency"],
        ),
        RootCauseAttribution(
            root_cause_occurred_at=samples[2].timestamp,
            root_cause_component="service-b",
            root_cause_reason="container cpu load",
            supporting_evidence_ids=["ev-b"],
        ),
    ]

    result = project_root_causes(
        task_index="task_3",
        expected_count=1,
        evidence=samples,
        hypotheses=[],
        root_causes=root_causes,
    )

    assert result.causes[0].root_cause_component == "service-a"


def test_projection_deduplicates_only_the_scored_fields():
    samples = [
        evidence("ev-a", 1, "checkoutservice", "memory", 9),
        evidence("ev-b", 2, "paymentservice", "memory", 8),
        evidence("ev-c", 3, "frontend", "cpu", 7),
    ]
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.RESOURCE_SATURATION,
            summary="resources",
            confidence=0.9,
            supporting_evidence_ids=["ev-a", "ev-b", "ev-c"],
        )
    ]

    result = project_root_causes(
        task_index="task_2",
        expected_count=2,
        evidence=samples,
        hypotheses=hypotheses,
        root_causes=[],
    )

    assert [item.root_cause_reason for item in result.causes] == [
        "container memory load",
        "container cpu load",
    ]


@pytest.mark.parametrize("task_index", ["task_4", "task_5", "task_6", "task_7"])
def test_multi_field_projection_never_combines_different_attributions(task_index):
    samples = [
        evidence("ev-a", 1, "checkoutservice", "memory", 9),
        evidence("ev-b", 10, "paymentservice", "cpu", 8),
    ]
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.RESOURCE_SATURATION,
            summary="resources",
            confidence=0.9,
            supporting_evidence_ids=["ev-a", "ev-b"],
        )
    ]

    result = project_root_causes(
        task_index=task_index,
        expected_count=2,
        evidence=samples,
        hypotheses=hypotheses,
        root_causes=[],
    )

    observed = {
        (
            item.root_cause_occurred_at,
            item.root_cause_component,
            item.root_cause_reason,
        )
        for item in result.causes
    }
    assert observed <= {
        (samples[0].timestamp, "checkoutservice", "container memory load"),
        (samples[1].timestamp, "paymentservice", "container cpu load"),
    }


def test_projection_is_repeatable_and_bounded():
    samples = [
        evidence(f"ev-{index}", index % 60, f"service-{index}", "cpu", index + 1)
        for index in range(70)
    ]
    hypotheses = [
        Hypothesis(
            cause_type=CauseType.RESOURCE_SATURATION,
            summary="resources",
            confidence=0.9,
            supporting_evidence_ids=[item.id for item in samples],
        )
    ]
    arguments = {
        "task_index": "task_3",
        "expected_count": 2,
        "evidence": samples,
        "hypotheses": hypotheses,
        "root_causes": [],
    }

    first = project_root_causes(**arguments)
    second = project_root_causes(**arguments)

    assert first == second
    assert first.audit.valid_candidate_count <= 64


def test_projection_discards_root_cause_with_unknown_evidence_reference():
    root_cause = RootCauseAttribution(
        root_cause_occurred_at=datetime(2026, 7, 14, 12, tzinfo=TZ),
        root_cause_component="checkoutservice",
        root_cause_reason="container memory load",
        supporting_evidence_ids=["ev-missing"],
    )

    result = project_root_causes(
        task_index="task_3",
        expected_count=1,
        evidence=[],
        hypotheses=[],
        root_causes=[root_cause],
    )

    assert result.causes == ()
    assert result.audit.projection_fallback is True
```

- [ ] **Step 4: Implement the minimum pure module**

Implement these public contracts in `projection.py`:

```python
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from backend.diagnosis.coordination_review import build_root_cause_attributions
from backend.domain.agent_findings import RootCauseAttribution
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis

RULE_VERSION = "v1"
MAX_CANDIDATES = 64
_GENERIC_SIGNALS = frozenset({"latency", "traffic"})
_TASK_FIELDS = {
    "task_1": ("time",),
    "task_2": ("reason",),
    "task_3": ("component",),
    "task_4": ("time", "reason"),
    "task_5": ("time", "component"),
    "task_6": ("component", "reason"),
    "task_7": ("time", "component", "reason"),
}


@dataclass(frozen=True)
class ProjectionAudit:
    rule_version: str
    scored_fields: tuple[str, ...]
    input_candidate_count: int
    valid_candidate_count: int
    deduplicated_candidate_count: int
    selected_evidence_ids: tuple[tuple[str, ...], ...]
    projection_fallback: bool = False
    fallback_reason: str | None = None
    projection_error: bool = False

    def metadata(self) -> dict[str, object]:
        return {
            "rule_version": self.rule_version,
            "scored_fields": list(self.scored_fields),
            "input_candidate_count": self.input_candidate_count,
            "valid_candidate_count": self.valid_candidate_count,
            "deduplicated_candidate_count": self.deduplicated_candidate_count,
            "selected_evidence_ids": [list(item) for item in self.selected_evidence_ids],
            "projection_fallback": self.projection_fallback,
            "fallback_reason": self.fallback_reason,
            "projection_error": self.projection_error,
        }

    @classmethod
    def failed(cls, task_index: str) -> "ProjectionAudit":
        return cls(
            rule_version=RULE_VERSION,
            scored_fields=_TASK_FIELDS.get(task_index, ()),
            input_candidate_count=0,
            valid_candidate_count=0,
            deduplicated_candidate_count=0,
            selected_evidence_ids=(),
            projection_error=True,
        )


@dataclass(frozen=True)
class ProjectionResult:
    causes: tuple[RootCauseAttribution, ...]
    audit: ProjectionAudit


@dataclass(frozen=True)
class _Candidate:
    cause: RootCauseAttribution
    evidence: tuple[EvidenceItem, ...]
    shared_rank: int


def scored_fields(task_index: str) -> tuple[str, ...]:
    try:
        return _TASK_FIELDS[task_index]
    except KeyError as exc:
        raise ValueError(f"invalid OpenRCA task_index: {task_index}") from exc


def project_root_causes(
    *,
    task_index: str,
    expected_count: int,
    evidence: list[EvidenceItem],
    hypotheses: list[Hypothesis],
    root_causes: list[RootCauseAttribution],
) -> ProjectionResult:
    fields = scored_fields(task_index)
    by_id = {item.id: item for item in evidence}
    canonical = build_root_cause_attributions(evidence, hypotheses)
    source = [*root_causes, *canonical]
    candidates = []
    for rank, cause in enumerate(source, start=1):
        if any(item_id not in by_id for item_id in cause.supporting_evidence_ids):
            continue
        supporting = tuple(by_id[item_id] for item_id in cause.supporting_evidence_ids)
        if not supporting:
            continue
        projected = cause
        if "time" in fields:
            strongest = min(
                supporting,
                key=lambda item: (
                    -_deviation(item),
                    item.timestamp,
                    item.id,
                ),
            )
            projected = cause.model_copy(
                update={"root_cause_occurred_at": strongest.timestamp}
            )
        candidates.append(_Candidate(projected, supporting, rank))

    candidates.sort(key=lambda item: _candidate_key(item, fields))
    candidates = candidates[:MAX_CANDIDATES]
    grouped: dict[tuple[str, ...], list[_Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(_projection_key(candidate.cause, fields), []).append(candidate)

    ranked = sorted(
        (_merge_group(group, fields) for group in grouped.values()),
        key=lambda item: _candidate_key(item, fields),
    )
    selected = ranked[:expected_count]
    fallback = len(selected) < expected_count
    if fallback:
        selected_ids = {
            (
                item.cause.root_cause_occurred_at,
                item.cause.root_cause_component,
                item.cause.root_cause_reason,
            )
            for item in selected
        }
        for candidate in candidates:
            identity = (
                candidate.cause.root_cause_occurred_at,
                candidate.cause.root_cause_component,
                candidate.cause.root_cause_reason,
            )
            if identity in selected_ids:
                continue
            selected.append(candidate)
            selected_ids.add(identity)
            if len(selected) == expected_count:
                break

    causes = tuple(item.cause for item in selected[:expected_count])
    audit = ProjectionAudit(
        rule_version=RULE_VERSION,
        scored_fields=fields,
        input_candidate_count=len(source),
        valid_candidate_count=len(candidates),
        deduplicated_candidate_count=len(ranked),
        selected_evidence_ids=tuple(
            tuple(sorted(item.id for item in candidate.evidence))
            for candidate in selected[:expected_count]
        ),
        projection_fallback=fallback,
        fallback_reason="insufficient_distinct_candidates" if fallback else None,
    )
    return ProjectionResult(causes=causes, audit=audit)


def _merge_group(
    group: list[_Candidate],
    fields: tuple[str, ...],
) -> _Candidate:
    representative = min(group, key=lambda item: _candidate_key(item, fields))
    merged = {item.id: item for candidate in group for item in candidate.evidence}
    cause = representative.cause.model_copy(
        update={"supporting_evidence_ids": sorted(merged)}
    )
    return _Candidate(cause, tuple(merged.values()), representative.shared_rank)


def _candidate_key(
    candidate: _Candidate,
    fields: tuple[str, ...],
) -> tuple:
    providers = {item.provider for item in candidate.evidence}
    specific = any(
        isinstance(signal := item.payload.get("signal_type"), str)
        and signal not in _GENERIC_SIGNALS
        for item in candidate.evidence
    )
    return (
        -len(providers),
        -int(specific),
        -max((_deviation(item) for item in candidate.evidence), default=0),
        candidate.shared_rank,
        candidate.cause.root_cause_occurred_at,
        _projection_key(candidate.cause, fields),
        tuple(sorted(item.id for item in candidate.evidence)),
    )


def _projection_key(
    cause: RootCauseAttribution,
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    values = {
        "time": cause.root_cause_occurred_at.isoformat(),
        "component": cause.root_cause_component.casefold(),
        "reason": cause.root_cause_reason.casefold(),
    }
    return tuple(values[field] for field in fields)


def _deviation(item: EvidenceItem) -> float:
    value = item.payload.get("deviation_score")
    return (
        float(value)
        if isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        else 0.0
    )
```

- [ ] **Step 5: Run focused tests and make only projector-local corrections**

Run:

```powershell
uv run pytest tests/benchmarks/test_openrca_projection.py -q
uv run ruff check backend/benchmarks/openrca/projection.py tests/benchmarks/test_openrca_projection.py
```

Expected: all projector tests pass and Ruff exits 0.

## Task 2: Connect the Projector without changing persisted production state

**Requirements:** R2, R3, R6, R7, R8, R12

**Files:**

- Modify: `backend/benchmarks/openrca/runner.py`
- Modify: `backend/benchmarks/openrca/models.py`
- Modify: `tests/benchmarks/test_openrca_runner.py`

- [ ] **Step 1: Write runner integration tests**

Add a `ProjectionAudit` to `FakeRuntime` outcomes and assert nested metadata:

```python
from backend.benchmarks.openrca.projection import ProjectionAudit


def fake_projection_audit(task_index: str) -> ProjectionAudit:
    fields = {
        "task_1": ("time",),
        "task_2": ("reason",),
        "task_3": ("component",),
        "task_4": ("time", "reason"),
        "task_5": ("time", "component"),
        "task_6": ("component", "reason"),
        "task_7": ("time", "component", "reason"),
    }[task_index]
    return ProjectionAudit(
        rule_version="v1",
        scored_fields=fields,
        input_candidate_count=1,
        valid_candidate_count=1,
        deduplicated_candidate_count=1,
        selected_evidence_ids=((f"ev-{task_index}",),),
    )
```

Return it from `FakeRuntime.run_case()` and extend the artifact assertion:

```python
metadata = json.loads(rows[0]["metadata"])
assert metadata["projection"]["rule_version"] == "v1"
assert metadata["projection"]["projection_error"] is False
assert summary["strategies"]["fixed"]["projection_errors"] == 0
assert summary["strategies"]["fixed"]["projection_fallbacks"] == 0
```

Add a real-run isolation test:

```python
def run_one_real_fixture_case(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    runtime_store = SQLiteRuntimeStore(engine, repository)
    case = OpenRcaRuntimeIndex.model_validate_json(
        safe_index(tmp_path, 1).read_text(encoding="utf-8")
    ).cases[0]
    runner = OpenRcaDiagnosisRunner(
        Path(__file__).parents[1] / "fixtures" / "openrca",
        None,
        repository=repository,
        runtime_store=runtime_store,
        deterministic=True,
    )
    outcome = runner.run_case(case, InvestigationStrategy.FIXED)
    return outcome, repository, runtime_store


def test_real_runner_projects_output_without_mutating_persisted_review(
    tmp_path: Path,
    monkeypatch,
):
    projected_component = "projected-only"
    original_projector = openrca_runner.project_root_causes

    def project(**arguments):
        result = original_projector(**arguments)
        causes = tuple(
            item.model_copy(update={"root_cause_component": projected_component})
            for item in result.causes
        )
        return result.__class__(causes=causes, audit=result.audit)

    monkeypatch.setattr(openrca_runner, "project_root_causes", project)
    outcome, repository, runtime_store = run_one_real_fixture_case(tmp_path)
    assert outcome.runtime_run_id is not None
    runtime_run = runtime_store.get_run(outcome.runtime_run_id)
    review = repository.get_coordination_review(runtime_run.investigation_id)

    assert outcome.root_causes[0].root_cause_component == projected_component
    assert review is not None
    assert all(
        item.root_cause_component != projected_component
        for item in review.root_causes
    )
```

Add a safe-failure test:

```python
def test_real_runner_marks_projection_failure_without_exception_detail(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        openrca_runner,
        "project_root_causes",
        lambda **_arguments: (_ for _ in ()).throw(ValueError("secret telemetry")),
    )

    outcome, _repository, _runtime_store = run_one_real_fixture_case(tmp_path)

    assert outcome.completed is False
    assert outcome.failure_category == "projection_error"
    assert outcome.projection_audit is not None
    assert outcome.projection_audit.projection_error is True
    assert "secret telemetry" not in str(outcome)
```

Add a no-rerun test:

```python
def test_projector_reuses_persisted_hypotheses_without_rerunning_analyzer(
    tmp_path: Path,
    monkeypatch,
):
    calls = 0
    original = openrca_runner.RcaAnalyzer.analyze

    def count(self, event, evidence):
        nonlocal calls
        calls += 1
        return original(self, event, evidence)

    monkeypatch.setattr(openrca_runner.RcaAnalyzer, "analyze", count)

    outcome, _repository, _runtime_store = run_one_real_fixture_case(tmp_path)

    assert outcome.completed is True
    assert calls == 1
```

- [ ] **Step 2: Run runner tests and observe RED**

Run:

```powershell
uv run pytest tests/benchmarks/test_openrca_runner.py -q
```

Expected: failures for missing audit fields, summary counters, and Projector invocation.

- [ ] **Step 3: Add backward-compatible summary counters**

Modify `OpenRcaStrategySummary`:

```python
projection_errors: int = Field(default=0, ge=0)
projection_fallbacks: int = Field(default=0, ge=0)
```

Defaults are required so existing frozen summaries remain readable.

- [ ] **Step 4: Invoke the Projector after diagnosis and before BenchmarkCaseOutcome**

Import:

```python
from backend.benchmarks.openrca.projection import (
    ProjectionAudit,
    project_root_causes,
)
```

Extend `BenchmarkCaseOutcome`:

```python
projection_audit: ProjectionAudit | None = None
```

After loading `record` and `review`, replace only the local output selection:

```python
authoritative_root_causes = list(review.root_causes) if review is not None else []
input_tokens, output_tokens = _runtime_token_usage(
    self.runtime_store,
    runtime_run.id,
)
try:
    projection = project_root_causes(
        task_index=case.task_index,
        expected_count=case.expected_root_cause_count or 1,
        evidence=list(record.evidence),
        hypotheses=list(record.hypotheses),
        root_causes=authoritative_root_causes,
    )
except Exception:
    return BenchmarkCaseOutcome(
        completed=False,
        failure_category="projection_error",
        duration_ms=round((perf_counter() - started) * 1000),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        runtime_run_id=runtime_run.id,
        projection_audit=ProjectionAudit.failed(case.task_index),
    )
root_causes = list(projection.causes)
```

Delete the later duplicate `_runtime_token_usage()` call. Do not include the exception message
in persisted or returned state.

Return `projection_audit=projection.audit` in the successful outcome.

- [ ] **Step 5: Write audit metadata and summary counts**

Add one nested key to prediction metadata:

```python
"projection": (
    outcome.projection_audit.metadata()
    if outcome.projection_audit is not None
    else None
),
```

Extend `_summarize()`:

```python
projection_errors=sum(
    bool(outcome.projection_audit and outcome.projection_audit.projection_error)
    for _, outcome in outcomes
),
projection_fallbacks=sum(
    bool(outcome.projection_audit and outcome.projection_audit.projection_fallback)
    for _, outcome in outcomes
),
```

Do not alter `review.root_causes`, `record.hypotheses`, Runtime events, or Replay serialization.

- [ ] **Step 6: Run runner, Runtime, and Replay regression tests**

Run:

```powershell
uv run pytest tests/benchmarks/test_openrca_runner.py tests/runtime/test_replay.py -q
uv run ruff check backend/benchmarks/openrca/runner.py backend/benchmarks/openrca/models.py tests/benchmarks/test_openrca_runner.py
```

Expected: all tests pass; historical summaries without projector counters still validate.

## Task 3: Add subset official-query support and the six-case targeted Gate

**Requirements:** R6, R7, R9, R10

**Files:**

- Modify: `backend/benchmarks/openrca/evaluator.py`
- Modify: `backend/benchmarks/openrca/__main__.py`
- Modify: `tests/benchmarks/test_openrca_evaluator.py`
- Modify: `tests/benchmarks/test_openrca_runner.py`

- [ ] **Step 1: Write a failing subset-query test**

Create a run directory with only `fixed-Bank.csv`, invoke
`write_official_query_inputs()`, and assert only the Bank query is written:

```python
def test_official_query_writer_skips_partitions_absent_from_targeted_run(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_partition_prediction(run_dir / "fixed-Bank.csv", row_id="0")

    written = write_official_query_inputs(
        FIXTURE_ROOT,
        run_dir,
        tmp_path / "queries",
    )

    assert [path.name for path in written] == ["Bank-query.csv"]
```

- [ ] **Step 2: Write targeted Gate fixtures and failure tests**

Build a six-row frozen fixture whose `official-report.csv` includes `task_index` and `score`.
Each prediction metadata must include:

```python
"projection": {
    "rule_version": "v1",
    "scored_fields": ["time"],
    "input_candidate_count": 2,
    "valid_candidate_count": 2,
    "deduplicated_candidate_count": 2,
    "selected_evidence_ids": [["ev-1"]],
    "projection_fallback": False,
    "fallback_reason": None,
    "projection_error": False,
}
```

Add these tests:

```python
def test_targeted_gate_accepts_three_dimensions_and_three_of_six(tmp_path):
    assert targeted_gate_run(write_targeted_gate_fixture(tmp_path)) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("two_positive_scores", "at least 3 of 6"),
        ("missing_time_hit", "task_1"),
        ("missing_reason_hit", "task_2"),
        ("missing_component_hit", "task_3"),
        ("projection_error", "projection errors"),
        ("projection_fallback", "projection fallbacks"),
        ("invalid_evidence", "evidence reference"),
        ("read_only_violation", "read-only"),
    ],
)
def test_targeted_gate_reports_each_blocker(tmp_path, mutation, expected):
    run_dir = write_targeted_gate_fixture(tmp_path, mutation=mutation)
    assert any(expected in item for item in targeted_gate_run(run_dir))
```

Add a CLI test that `targeted-gate` exits 2 for an invalid directory.

- [ ] **Step 3: Run evaluator tests and observe RED**

Run:

```powershell
uv run pytest tests/benchmarks/test_openrca_evaluator.py tests/benchmarks/test_openrca_runner.py -q
```

Expected: failures for missing `targeted_gate_run`, CLI command, and subset skip behavior.

- [ ] **Step 4: Make official query generation subset-safe**

Replace the current unconditional fallback with:

```python
prediction_path = next(
    (
        candidate
        for candidate in (
            run_dir / f"fixed-{slug}.csv",
            run_dir / f"adaptive-{slug}.csv",
        )
        if candidate.exists()
    ),
    None,
)
if prediction_path is None:
    continue
```

Preserve existing row sorting and the requirement that output is outside the frozen run.

- [ ] **Step 5: Implement the targeted Gate as a separate frozen-artifact check**

Add:

```python
def targeted_gate_run(run_dir: Path) -> list[str]:
    """检查六案例开发回归，不替代正式 40-case release Gate。"""
    failures: list[str] = []
    try:
        summary = OpenRcaBenchmarkSummary.model_validate_json(
            (run_dir / "summary.json").read_text(encoding="utf-8")
        )
        fixed = summary.strategies["fixed"]
        with (run_dir / "fixed-predictions.csv").open(
            encoding="utf-8",
            newline="",
        ) as file:
            predictions = list(csv.DictReader(file))
        with (run_dir / "official-report.csv").open(
            encoding="utf-8-sig",
            newline="",
        ) as file:
            official = list(csv.DictReader(file))
    except (FileNotFoundError, KeyError, ValueError, ValidationError) as exc:
        return [f"targeted artifact is invalid: {type(exc).__name__}"]

    if summary.case_count != 6 or fixed.case_count != 6:
        failures.append("targeted gate requires exactly 6 cases")
    if fixed.completed_count != 6:
        failures.append("targeted gate requires 6 completed Runtime Runs")
    if (
        fixed.evidence_reference_validity != 1
        or fixed.invalid_evidence_references != 0
    ):
        failures.append("targeted evidence reference validity must be 100%")
    if fixed.read_only_violations:
        failures.append("targeted read-only violations must be zero")
    if fixed.projection_errors:
        failures.append("targeted projection errors must be zero")
    if fixed.projection_fallbacks:
        failures.append("targeted projection fallbacks must be zero")

    parsed = []
    for row in official:
        try:
            score = float(row["score"])
            task_index = row["task_index"]
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(score):
            parsed.append((task_index, score))
    if len(parsed) != 6 or sum(score > 0 for _, score in parsed) < 3:
        failures.append("official targeted score must pass at least 3 of 6 cases")
    for task_index in ("task_1", "task_2", "task_3"):
        if not any(task == task_index and score > 0 for task, score in parsed):
            failures.append(f"official targeted {task_index} must have a positive score")

    valid_projection_rows = 0
    for row in predictions:
        try:
            projection = json.loads(row["metadata"])["projection"]
            valid_projection_rows += int(
                projection["rule_version"] == "v1"
                and projection["projection_error"] is False
                and projection["projection_fallback"] is False
                and bool(projection["selected_evidence_ids"])
            )
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    if len(predictions) != 6 or valid_projection_rows != 6:
        failures.append("all targeted predictions require valid projection audit metadata")
    return failures
```

Keep `gate_run()` unchanged so the formal 40-case contract cannot be weakened.

- [ ] **Step 6: Add the CLI command**

In `__main__.py`:

```python
from backend.benchmarks.openrca.evaluator import targeted_gate_run

targeted_gate = commands.add_parser("targeted-gate")
targeted_gate.add_argument("--run-dir", type=Path, required=True)
```

Handle it before the normal run path:

```python
if arguments.command == "targeted-gate":
    failures = targeted_gate_run(arguments.run_dir)
    if failures:
        parser.error("; ".join(failures))
    print("targeted gate passed")
    return
```

- [ ] **Step 7: Run focused evaluator and CLI tests**

Run:

```powershell
uv run pytest tests/benchmarks/test_openrca_evaluator.py tests/benchmarks/test_openrca_runner.py -q
uv run ruff check backend/benchmarks/openrca/evaluator.py backend/benchmarks/openrca/__main__.py tests/benchmarks/test_openrca_evaluator.py
```

Expected: all tests pass; existing `gate` tests remain unchanged.

## Task 4: Verify repository compatibility and run the six-case targeted Gate

**Requirements:** R7, R8, R9, R10, R11

**Files:**

- Modify: `README.md`
- Modify: `docs/superpowers/current.md`
- Modify: `docs/superpowers/specs/2026-07-30-openrca-task-aware-evidence-projection-design.md`

- [ ] **Step 1: Run the complete local verification before expensive replay**

Run:

```powershell
uv run pytest tests/benchmarks tests/diagnosis tests/rca tests/runtime -q
uv run ruff check .
uv run pytest -q
git diff --check
```

Expected: all commands exit 0. Record exact counts and warnings; do not reuse earlier counts.

- [ ] **Step 2: Audit the production boundary and Ground Truth isolation**

Run:

```powershell
rg -n "task_index|project_root_causes|ProjectionAudit" backend
rg -n "record\\.csv|scoring_points|groundtruth|ground_truth" backend/benchmarks/openrca/projection.py backend/benchmarks/openrca/runner.py
git diff -- backend/rca backend/diagnosis backend/domain backend/runtime backend/api
```

Expected:

- `task_index` projection calls exist only under `backend/benchmarks/openrca/`;
- Projector source contains no Ground Truth/scoring reads;
- there is no behavioral production diff introduced by this implementation;
- any pre-existing dirty V10 diff is distinguished from this implementation in the report.

- [ ] **Step 3: Reuse the frozen six-case safe index and run a new deterministic replay**

Preflight:

```powershell
Get-FileHash -Algorithm SHA256 `
  "D:\data\OpenRCA\prepared-v10-dev-6case\runtime-cases.json"
```

Expected SHA-256:

```text
B249E2F6B0B0DBD3B9F30AA71EF3302FF2C48A05CC50B914FB3DCBBC8800AD4B
```

Run:

```powershell
uv run python -m backend.benchmarks.openrca run `
  --dataset-root "D:\data\OpenRCA\dataset" `
  --safe-index "D:\data\OpenRCA\prepared-v10-dev-6case\runtime-cases.json" `
  --strategy fixed `
  --mode deterministic `
  --output "D:\data\OpenRCA\results-v10-targeted-projector"
```

Resolve the emitted `run_id` and never reuse or overwrite an earlier run directory.

- [ ] **Step 4: Generate compatible and official reports**

```powershell
$resultRoot = "D:\data\OpenRCA\results-v10-targeted-projector"
$runId = (Get-Content -Raw "$resultRoot\latest-run.txt").Trim()
$runDir = (Resolve-Path "$resultRoot\$runId").Path
$queryDir = "D:\data\OpenRCA\official-query-v10-targeted-projector\$runId"

uv run python -m backend.benchmarks.openrca evaluate `
  --query-root "D:\data\OpenRCA\dataset" `
  --run-dir $runDir `
  --official-query-output $queryDir

$predictionFiles = Get-ChildItem -LiteralPath $runDir -Filter "fixed-*.csv" |
  Where-Object Name -NE "fixed-predictions.csv" |
  Sort-Object Name
$queryFiles = foreach ($prediction in $predictionFiles) {
  $slug = $prediction.BaseName.Substring("fixed-".Length)
  Join-Path $queryDir "$slug-query.csv"
}

Push-Location "D:\data\OpenRCA\official-evaluator"
python -m main.evaluate `
  -p $predictionFiles.FullName `
  -q $queryFiles `
  -r "$runDir\official-report.csv"
Pop-Location
```

Expected: compatible and official reports each contain exactly six rows.

- [ ] **Step 5: Run the targeted Gate and obey the stop/go result**

```powershell
uv run python -m backend.benchmarks.openrca targeted-gate --run-dir $runDir
```

Pass condition:

- 6/6 completed;
- Evidence validity 100%;
- read-only violations 0;
- projection error/fallback 0;
- at least 3/6 official rows have score `>0`;
- task_1, task_2, task_3 each have at least one score `>0`.

If the command exits nonzero:

1. Record the exact failures, predictions, audit metadata, report hashes, run ID, source status,
   and evaluator commit in `current.md` and the design review ledger.
2. Stop. Do not change weights, add case rules, or run the 40-case benchmark.

If the command exits 0:

1. Record the same artifacts and hashes.
2. Mark R1–R9 and R11–R12 verified; keep R10 pending source identity and formal Gate.
3. Continue only to Task 5.

- [ ] **Step 6: Update operator documentation**

Add the exact targeted commands and stop/go rule to README. Update `current.md` and the design
traceability table with observed results only. Run:

```powershell
git diff --check -- README.md docs/superpowers/current.md docs/superpowers/specs/2026-07-30-openrca-task-aware-evidence-projection-design.md
```

Expected: exit 0.

## Task 5: Establish the formal-run checkpoint without unauthorized Git mutation

**Requirements:** R10

**Files:**

- Modify only after a real result: `docs/superpowers/current.md`
- Modify only after a real result: `docs/superpowers/specs/2026-07-30-openrca-task-aware-evidence-projection-design.md`

- [ ] **Step 1: Stop if the targeted Gate did not pass**

There is no override. A failed targeted Gate ends this implementation attempt.

- [ ] **Step 2: Verify source identity status**

Run:

```powershell
git status --short
git rev-parse HEAD
git diff --check
git diff --stat
git -C "D:\data\OpenRCA\official-evaluator" rev-parse HEAD
```

Expected before a formal candidate: the exact implementation source must have a reproducible
identity. A dirty worktree plus HEAD alone is insufficient.

- [ ] **Step 3: Report and request separate Git authorization**

Do not stage or commit. Report:

- targeted Gate result and artifact hashes;
- changed files;
- full verification evidence;
- current HEAD and dirty-worktree status;
- proposed focused commit scope.

Wait for the user to explicitly authorize staging/commit or provide another reproducible source
identity method. Do not run the third 40-case benchmark in the same turn without that authority
and a clean source preflight.

- [ ] **Step 4: Run the formal candidate only after the checkpoint is independently satisfied**

After separate authorization and a clean reproducible source:

```powershell
uv run python -m backend.benchmarks.openrca run `
  --dataset-root "D:\data\OpenRCA\dataset" `
  --safe-index "D:\data\OpenRCA\prepared-v10-deterministic\runtime-cases.json" `
  --strategy fixed `
  --mode deterministic `
  --output "D:\data\OpenRCA\results-v10-deterministic"
```

Evaluate with the existing official workflow, then run:

```powershell
uv run python -m backend.benchmarks.openrca gate --run-dir $formalRunDir
```

Expected: all existing formal Gate checks pass. If they fail, preserve the artifact and stop;
do not call the projector a production accuracy improvement.

## 4. Final review checklist

- [ ] Every selected prediction cause cites valid Evidence IDs.
- [ ] `task_index` is absent from production domain/API/Runtime contracts.
- [ ] Projector never reads query scoring points, record files, Ground Truth, or official reports.
- [ ] Existing Analyzer and Attribution behavior is unchanged.
- [ ] Fallback and error are visible in both row metadata and summary counters.
- [ ] Existing formal `gate` remains unchanged and still requires exactly 40 cases.
- [ ] Targeted Gate is a separate six-case development check.
- [ ] Full tests and Ruff are fresh.
- [ ] No third formal run occurred before targeted pass and reproducible source identity.
- [ ] Final wording separates OpenRCA score from production accuracy.
