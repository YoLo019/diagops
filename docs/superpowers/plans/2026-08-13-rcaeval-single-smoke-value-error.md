# RCAEval Single Smoke `ValueError` Root-Cause Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely expose the swallowed RCAEval single-control exception and use one authorized production smoke to identify the exact root cause without changing the frozen evaluation contract.

**Architecture:** Add one private exception-diagnostic formatter beside the RCAEval single-control runtime and emit one bounded warning from its existing failure boundary. Keep `CasePrediction`, runtime events, budgets, transport, and persisted payloads unchanged. The captured exception becomes the input to a separate, fully concrete root-cause fix plan; separating the plans prevents an evidence-free implementation placeholder.

**Tech Stack:** Python 3.13, pytest, Pydantic, OpenAI Agents SDK, `logging`, existing `backend.safety.redaction` utilities, SQLite runtime store.

**Frozen Constraints:** Keep `native_json_schema`, the certified capability artifact, evaluation budgets, topology, tool manifest, and retry policy unchanged.

---

## File Structure

- Modify `backend/benchmarks/rcaeval/runner.py`: format and log a safe, bounded exception diagnostic at the single-control catch boundary.
- Modify `tests/benchmarks/test_rcaeval_runner.py`: prove secret/path redaction, bounded output, repository-relative location, and integration logging.
- Create a follow-up root-cause plan after the diagnostic smoke: record the exact exception and specify its red-green fix before production code changes.

### Task 1: Safe RCAEval Exception Diagnostics

**Files:**
- Modify: `backend/benchmarks/rcaeval/runner.py:1-14, 327-345`
- Test: `tests/benchmarks/test_rcaeval_runner.py`

- [ ] **Step 1: Write the failing formatter test**

Import `_safe_exception_diagnostic`, raise a `ValueError` from a helper in the test repository, and assert the diagnostic equals a bounded map:

```python
def test_safe_exception_diagnostic_redacts_message_and_reports_relative_location():
    try:
        raise ValueError(
            "api_key=sk-abcdefghijklmnopqrstuvwxyz "
            "base_url=https://user:pass@example.test/v1 "
            "path=C:\\private\\case.json "
            + "x" * 600
        )
    except ValueError as exc:
        diagnostic = _safe_exception_diagnostic(exc, Path.cwd())

    assert diagnostic["exception_type"] == "ValueError"
    assert "sk-" not in diagnostic["message"]
    assert "user:pass" not in diagnostic["message"]
    assert "C:\\private" not in diagnostic["message"]
    assert len(diagnostic["message"]) <= 256
    assert diagnostic["location"].startswith("tests/benchmarks/test_rcaeval_runner.py:")
```

- [ ] **Step 2: Run the formatter test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/benchmarks/test_rcaeval_runner.py::test_safe_exception_diagnostic_redacts_message_and_reports_relative_location -q
```

Expected: collection fails because `_safe_exception_diagnostic` does not exist.

- [ ] **Step 3: Write minimal formatter code**

Add `logging` and `traceback` imports, import `redact_text`, define `logger`, and add:

```python
def _safe_exception_diagnostic(exc: BaseException, repository_root: Path) -> dict[str, str]:
    message = " ".join(redact_text(str(exc)).split())[:256]
    location = "unknown"
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        try:
            relative = Path(frame.filename).resolve().relative_to(repository_root.resolve())
        except ValueError:
            continue
        location = f"{relative.as_posix()}:{frame.lineno}"
        break
    return {
        "exception_type": type(exc).__name__[:128],
        "message": message,
        "location": location,
    }
```

- [ ] **Step 4: Run the formatter test and verify GREEN**

Run the Step 2 command.

Expected: `1 passed`.

- [ ] **Step 5: Write the failing integration logging test**

Use the existing runtime-package fixture and a `turn` that raises a credential-bearing `ValueError`. Run `RcaEvalCaseRunner.run_case` under `caplog.at_level(logging.WARNING)`, then assert:

```python
assert prediction.completed is False
assert "rcaeval single control failed" in caplog.text
assert "exception_type=ValueError" in caplog.text
assert "tests/benchmarks/test_rcaeval_runner.py:" in caplog.text
assert "sk-abcdefghijklmnopqrstuvwxyz" not in caplog.text
```

- [ ] **Step 6: Run the integration test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/benchmarks/test_rcaeval_runner.py::test_single_control_logs_safe_exception_diagnostic -q
```

Expected: FAIL because the existing catch boundary emits no diagnostic warning.

- [ ] **Step 7: Emit the bounded warning from the existing catch boundary**

In `SingleInvestigatorAgent.investigator_round_1`, immediately inside `except Exception as exc`, compute the diagnostic with `Path.cwd()` and log only its three safe fields:

```python
diagnostic = _safe_exception_diagnostic(exc, Path.cwd())
logger.warning(
    "rcaeval single control failed exception_type=%s message=%s location=%s",
    diagnostic["exception_type"],
    diagnostic["message"],
    diagnostic["location"],
)
```

Keep the existing failure persistence and return behavior unchanged.

- [ ] **Step 8: Run focused tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/benchmarks/test_rcaeval_runner.py -q
```

Expected: all tests pass with no warnings from successful cases.

- [ ] **Step 9: Commit the diagnostic change**

```powershell
git add -- backend/benchmarks/rcaeval/runner.py tests/benchmarks/test_rcaeval_runner.py
git commit -m "fix(rcaeval): expose safe single smoke failures"
```

### Task 2: Authorized Diagnostic Smoke

**Files:**
- Read: `C:/Users/林佳威/.codex/attachments/96cda2c5-d476-4422-82fd-86b4e3ef7933/pasted-text.txt`
- Modify: `docs/superpowers/plans/2026-08-13-rcaeval-single-smoke-value-error.md`

- [ ] **Step 1: Verify prerequisites without printing secrets**

Run:

```powershell
if ([string]::IsNullOrWhiteSpace($env:DIAGOPS_AGENTS_API_KEY)) { throw 'DIAGOPS_AGENTS_API_KEY is missing' }
Test-Path -LiteralPath 'D:\data\RCAEval\v11-m5\runtime'
Test-Path -LiteralPath 'D:\agent\sre-agent\output\model_capability\349d8f7aeba75e46-gpt-5.6-terra\result.json'
```

Expected: both path checks print `True`; the key value is never printed.

- [ ] **Step 2: Run exactly one diagnostic smoke in an isolated PowerShell process**

Normalize the attachment's two-space paste indentation, encode the script, and execute it as one child process:

```powershell
$path = 'C:\Users\林佳威\.codex\attachments\96cda2c5-d476-4422-82fd-86b4e3ef7933\pasted-text.txt'
$code = [regex]::Replace((Get-Content -Raw -LiteralPath $path), '(?m)^ {2}', '')
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
& pwsh -NoLogo -NoProfile -NonInteractive -EncodedCommand $encoded *>&1 | Tee-Object -Variable diagnosticOutput
$diagnosticExit = $LASTEXITCODE
if ($diagnosticExit -eq 0) { throw 'Diagnostic smoke unexpectedly passed; inspect output before continuing' }
```

Expected: nonzero exit plus one `rcaeval single control failed` warning containing exception type, bounded message, and repository-relative location.

- [ ] **Step 3: Record the exact root cause and write the follow-up plan**

Append to this plan a `Diagnostic Result` section containing only the safe warning fields and event counts. Then create `docs/superpowers/plans/2026-08-13-rcaeval-single-smoke-root-cause-fix.md` with the exact failing test, production edit, verification commands, final authorized smoke command, and expected RED/GREEN results. Do not change the root-cause production boundary until that plan contains no placeholders.

The follow-up plan's final acceptance must require `completed=true`, persisted run status `completed`, at least one `model.completed`, at least one `tool.completed`, zero `model.failed`, and zero read-only violations.

- [ ] **Step 4: Commit the evidence-backed plan amendment**

```powershell
git add -- docs/superpowers/plans/2026-08-13-rcaeval-single-smoke-value-error.md
git add -- docs/superpowers/plans/2026-08-13-rcaeval-single-smoke-root-cause-fix.md
git commit -m "docs(rcaeval): plan proven single smoke fix"
```
