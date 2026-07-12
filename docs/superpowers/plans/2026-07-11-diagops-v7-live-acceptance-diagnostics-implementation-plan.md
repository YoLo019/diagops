# DiagOps V7 Live Acceptance Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe structured Agent diagnostics and a five-run diagnostic mode to the existing V7 live acceptance artifact without changing prompts, arbitration, or reliability thresholds.

**Architecture:** Reuse the orchestrator-accepted `AgentsRcaRuntimeResult` already consumed by `_build_run`. Project only allowlisted enum/numeric/ID fields into frozen dataclasses, then serialize them through the existing artifact writer. Parameterize the existing cohort loop with `runs_per_case`; retain `3` as the canonical gate default and treat `1` as non-gating diagnostics.

**Tech Stack:** Python 3.12, stdlib `argparse`/`dataclasses`/`json`, Pydantic domain models, pytest, Ruff.

---

## File Map

- Modify `backend/services/v7_live_acceptance.py`: safe diagnostic projections, support counts, cohort mode, CLI parsing, schema-v2 artifact.
- Modify `tests/services/test_v7_live_acceptance.py`: TDD coverage for projections, redaction boundary, canonical compatibility, and five-run mode.
- Modify `README.md`: document the diagnostic command and its non-gating meaning.

No domain, persistence, API, report, frontend, prompt, or arbitration file changes.

---

### Task 1: Safe Structured Diagnostic Projection

**Files:**
- Modify: `backend/services/v7_live_acceptance.py`
- Test: `tests/services/test_v7_live_acceptance.py`

- [ ] **Step 1: Write failing projection tests**

Add tests that construct an accepted result with two findings from the same
Agent and one finding from another Agent, plus one candidate referencing all
three findings:

```python
def test_build_run_projects_only_safe_structured_agent_diagnostics():
    record, accepted = _accepted_diagnostic_result()

    row = acceptance._build_run(
        record,
        accepted,
        "deployment_regression",
        1,
        CauseType.DEPLOYMENT_REGRESSION,
        False,
        LiveConfig("gpt-test", 1.0, 2.0),
        1,
        metrics_result=accepted,
    )

    assert row.run_status == MultiAgentRunStatus.COMPLETED
    assert row.fallback_reason is None
    assert row.findings[0] == AcceptanceFinding(
        agent_name="LogAgent",
        finding_type="root_cause",
        related_cause_type="deployment_regression",
        confidence=0.9,
        evidence_ids=["ev-log"],
        analysis_round=1,
        revises_finding_id=None,
    )
    assert row.candidates[0].supporting_agent_count == 2
    assert row.candidates[0].contradicting_agent_count == 0
```

Add a serialization boundary test:

```python
def test_diagnostic_projection_omits_free_text_and_raw_model_data(tmp_path):
    row = _diagnostic_row_with_sensitive_source_text()
    path = write_artifact(
        tmp_path,
        LiveConfig("gpt-test", 1.0, 2.0),
        [row],
        evaluation=None,
        mode="diagnostic",
    )
    content = path.read_text(encoding="utf-8")

    for forbidden in [
        "secret-test-value",
        "https://api.example.invalid",
        "raw prompt",
        "raw response",
        "private reasoning",
        "finding summary",
        "rationale text",
        "uncertainty text",
    ]:
        assert forbidden not in content
```

Add a dangling-reference test proving `_build_run` cannot emit diagnostics from
an accepted result for which `_references_valid(record, result)` is false.

- [ ] **Step 2: Verify RED**

Run:

```powershell
uv run pytest tests/services/test_v7_live_acceptance.py -k "diagnostic_projection or projects_only_safe" -v
```

Expected: fail because diagnostic dataclasses/fields and the diagnostic writer arguments do not exist.

- [ ] **Step 3: Add the minimum projection types**

In `backend/services/v7_live_acceptance.py`, add frozen dataclasses containing
only approved fields:

```python
@dataclass(frozen=True)
class AcceptanceFinding:
    agent_name: str
    finding_type: str
    related_cause_type: str | None
    confidence: float
    evidence_ids: list[str]
    analysis_round: int
    revises_finding_id: str | None


@dataclass(frozen=True)
class AcceptanceCandidate:
    cause_type: str
    rank: int
    confidence: float
    supporting_finding_ids: list[str]
    contradicting_finding_ids: list[str]
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]
    supporting_agent_count: int
    contradicting_agent_count: int
```

Extend `AcceptanceRun` with:

```python
run_status: MultiAgentRunStatus | None
fallback_reason: str | None
findings: list[AcceptanceFinding]
candidates: list[AcceptanceCandidate]
```

Use the existing `result.findings`, `result.review.candidates`, and finding-ID
map. Count unique agent names with a set; do not add a generic projection layer.
Only project when `result is not None` and `_references_valid(record, result)` is
true. Use the already sanitized `result.run_summary.failure_reason` for
`fallback_reason`; do not inspect exceptions or raw responses.

- [ ] **Step 4: Verify GREEN**

Run:

```powershell
uv run pytest tests/services/test_v7_live_acceptance.py -k "diagnostic_projection or projects_only_safe" -v
uv run ruff check backend/services/v7_live_acceptance.py tests/services/test_v7_live_acceptance.py
```

Expected: focused tests and Ruff pass.

- [ ] **Review gate**

Confirm the projection contains no free text except the existing safe failure
category, references are validated first, and support counts deduplicate Agent
names rather than findings.

---

### Task 2: Five-Run Diagnostic Cohort Without Gate Semantics

**Files:**
- Modify: `backend/services/v7_live_acceptance.py`
- Test: `tests/services/test_v7_live_acceptance.py`

- [ ] **Step 1: Write failing cohort and CLI tests**

Add a substituted-runtime test:

```python
def test_diagnostic_cohort_runs_each_case_once_without_probes():
    runtime = _SubstituteRuntime()
    rows = run_live_cohort(
        LiveConfig("gpt-test", 1.0, 2.0),
        runtime=runtime,
        runs_per_case=1,
    )

    assert [(row.case_id, row.repetition) for row in rows] == [
        (case_id, 1) for case_id in EXPECTED_CAUSES
    ]
    assert len(rows) == 5
    assert {row.cohort for row in rows} == {"clean"}
    assert not any(
        item.payload.get("v7_safety_probe") is True
        for _, evidence, _ in runtime.calls
        for item in evidence
    )
```

Add argument validation tests:

```python
@pytest.mark.parametrize("value", [0, 2, 4])
def test_run_live_cohort_rejects_unsupported_runs_per_case(value):
    with pytest.raises(ValueError, match="runs_per_case must be 1 or 3"):
        run_live_cohort(LiveConfig("gpt-test", 1.0, 2.0), runs_per_case=value)
```

Add a `main(["--runs-per-case", "1"])` test that substitutes configuration,
cohort execution, and artifact writing, then asserts:

```python
assert captured["mode"] == "diagnostic"
assert captured["evaluation"] is None
assert exit_code == 0
assert "DIAGNOSTIC" in captured_output
assert "PASS" not in captured_output
```

Retain the existing default-main test and assert it still uses three runs,
evaluates thresholds, and returns nonzero for a failed canonical gate.

- [ ] **Step 2: Verify RED**

Run:

```powershell
uv run pytest tests/services/test_v7_live_acceptance.py -k "diagnostic_cohort or runs_per_case or main" -v
```

Expected: fail because the parameter and CLI do not exist.

- [ ] **Step 3: Parameterize the existing loop**

Change only the loop boundary and adversarial selection:

```python
def run_live_cohort(
    config: LiveConfig,
    *,
    runtime: AgentsRcaRuntime | None = None,
    runs_per_case: int = 3,
) -> list[AcceptanceRun]:
    if runs_per_case not in {1, 3}:
        raise ValueError("runs_per_case must be 1 or 3")
    canonical = runs_per_case == 3
    ...
    for repetition in range(1, runs_per_case + 1):
        adversarial = canonical and (case_id, repetition) in INJECTION_RUNS
```

Do not parameterize case selection, injection runs, thresholds, or model
settings.

- [ ] **Step 4: Add the stdlib CLI boundary**

Use `argparse` only:

```python
def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runs-per-case",
        type=int,
        choices=(1, 3),
        default=3,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ...
    results = run_live_cohort(config, runs_per_case=args.runs_per_case)
    diagnostic = args.runs_per_case == 1
    evaluation = None if diagnostic else evaluate_results(results)
    path = write_artifact(
        Path("artifacts"),
        config,
        results,
        evaluation,
        mode="diagnostic" if diagnostic else "reliability_gate",
    )
    if diagnostic:
        print("V7 live acceptance: DIAGNOSTIC ONLY")
        print(f"Artifact: {path}")
        return 0
    ...
```

`write_artifact` must write schema version `2`, top-level `mode`, actual cohort
counts derived from results, and `evaluation: null` in diagnostic mode. Preserve
exclusive file creation and canonical version-2 evaluation output.

- [ ] **Step 5: Verify GREEN and canonical compatibility**

Run:

```powershell
uv run pytest tests/services/test_v7_live_acceptance.py -v
uv run ruff check backend/services/v7_live_acceptance.py tests/services/test_v7_live_acceptance.py
```

Expected: all acceptance tests pass without network access; existing canonical
threshold tests remain unchanged.

- [ ] **Review gate**

Confirm `--runs-per-case 1` cannot print PASS or call `evaluate_results`, while
the no-argument command remains the exact 15-run gate.

---

### Task 3: Documentation, Safety Review, And Verification

**Files:**
- Modify: `README.md`
- Review: `backend/services/v7_live_acceptance.py`
- Review: `tests/services/test_v7_live_acceptance.py`

- [ ] **Step 1: Document the diagnostic command**

Add beneath the existing V7 live gate command:

```powershell
uv run python -m backend.services.v7_live_acceptance --runs-per-case 1
```

State that it runs five clean investigations, writes a schema-v2 diagnostic
artifact, does not evaluate or weaken the reliability gate, and must be reviewed
before changing prompts or arbitration. Repeat that artifacts contain no
prompts, raw responses, reasoning, credentials, or free-text findings.

- [ ] **Step 2: Run focused regression checks**

```powershell
uv run pytest tests/services/test_v7_live_acceptance.py tests/diagnosis/test_agents_runtime.py tests/reports/test_generator.py tests/golden/test_golden_cases.py -v
uv run ruff check backend/services/v7_live_acceptance.py tests/services/test_v7_live_acceptance.py
git diff --check -- backend/services/v7_live_acceptance.py tests/services/test_v7_live_acceptance.py README.md
```

Expected: all commands exit zero; no external model call occurs.

- [ ] **Step 3: Apply the DiagOps review checklist**

Inspect the diff and confirm:

1. Accepted/persisted results, not raw model output, supply diagnostics.
2. Evidence and finding references remain valid.
3. No mutation behavior or executed-action wording is added.
4. No prompt, summary, rationale, uncertainty, reasoning, raw response, Base URL,
   or credential enters JSON.
5. Thresholds, prompt text, arbitration, domain models, persistence, API, report,
   and frontend remain untouched.

- [ ] **Step 4: Run standard verification**

```powershell
uv run ruff check .
uv run pytest -v
Set-Location frontend
npm.cmd run build
Set-Location ..
git diff --check
```

Expected: all commands exit zero. Existing Starlette and TanStack warnings may
remain non-blocking.

- [ ] **Step 5: Request the five-run local diagnostic**

Only after every automated check passes, ask the user to reuse their locally
configured OpenAI environment and run:

```powershell
uv run python -m backend.services.v7_live_acceptance --runs-per-case 1
```

Read the resulting artifact and classify each fallback using the approved
decision gate. Do not implement a prompt or arbitration change until that
artifact provides evidence and the user approves the resulting fix.

---

## Plan Self-Review Result

- Spec coverage: Tasks 1-3 cover safe diagnostic fields, unique Agent support
  counts, five clean runs, non-gating CLI semantics, schema-v2 artifacts,
  documentation, review, and verification.
- Scope: no prompt, arbitration, provider, domain, persistence, API, report, UI,
  retry, dashboard, or experiment framework changes.
- Compatibility: default invocation remains the canonical 15-run gate with all
  existing thresholds; diagnostic mode is an explicit CLI option.
- Security: diagnostics originate only from validated persisted objects and omit
  all model/provider free text and secrets.
- Placeholders: none.
- Git: do not commit, stage, or push unless the user explicitly requests it.
