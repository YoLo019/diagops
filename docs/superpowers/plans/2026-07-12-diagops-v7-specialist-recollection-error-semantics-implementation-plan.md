# DiagOps V7 Specialist Recollection Error Semantics Implementation Plan

> **Spec:** `docs/superpowers/specs/2026-07-12-diagops-v7-specialist-recollection-error-semantics-design.md`

## Goal

Apply the smallest production fix that separates first-turn Coordinator
proposal validation from true SDK failure and allows one targeted recollection
of other missing required specialists despite unknown or duplicate output.

## Scope And Constraints

Modify only:

1. `backend/diagnosis/agents_runtime.py`
2. `tests/diagnosis/test_agents_runtime.py`
3. `tests/diagnosis/test_agents_sdk_contract.py`

Do not change prompts, domain models, APIs, persistence, frontend, live-gate
thresholds, artifact schemas, or provider configuration. Do not add a helper,
new exception type, retry abstraction, loop, configuration flag, or dependency
unless a failing test proves the two direct edits cannot satisfy the spec.

Repository comments remain Simplified Chinese where a comment is necessary;
technical identifiers stay in English. No new comment is expected for the
straight-line exception split.

## Task 1: Lock The Production SDK Error Contract With TDD

### Files

- Modify: `tests/diagnosis/test_agents_sdk_contract.py`
- Modify: `backend/diagnosis/agents_runtime.py`

### RED

Add one focused test using the installed SDK adapter boundary. Arrange for
`Runner.run` to return captured valid specialist output but a final output that
cannot validate as `_CoordinatorProposal`.

Assert:

1. `coordinator_proposal is None`.
2. `error is None`.
3. `cancelled is False`.
4. Captured drafts and available token usage remain returned.

Run:

```powershell
uv run pytest tests/diagnosis/test_agents_sdk_contract.py -q
```

Expected RED: the new assertion fails because proposal validation is currently
caught by the same block as SDK execution failure and populates `error`.

### GREEN

In the production SDK turn function, keep `Runner.run` and response capture in
the SDK exception boundary. Validate `run_result.final_output` only after the
SDK call succeeds. A proposal validation exception sets `proposal=None` without
setting `error`; `_consume_turn` remains responsible for recording the visible
invalid Coordinator output.

Do not weaken handling for exceptions raised by `Runner.run`, including
`ModelBehaviorError` raised before a successful result exists.

Run the focused test again. Expected GREEN: pass.

## Task 2: Allow Missing Required Specialists To Recollect Despite Invalid Peers

### Files

- Modify: `tests/diagnosis/test_agents_runtime.py`
- Modify: `backend/diagnosis/agents_runtime.py`

### RED

Add the smallest tests for the missing contract:

1. Unknown first-turn agent output plus one valid required finding recollects
   only the other two required specialists once; final status remains partial.
2. Duplicate first-turn output plus another missing required specialist
   recollects only that missing specialist once; final status remains partial.

Each test must assert the recollection call's `specialist_names`, call count,
`analysis_round=1`, retained valid findings, and final partial status.

Run:

```powershell
uv run pytest tests/diagnosis/test_agents_runtime.py -k "unknown or duplicate" -q
```

Expected RED: recollection is skipped because
`nonrecoverable_first_output` is currently part of the recollection condition.

### GREEN

Remove `nonrecoverable_first_output` only from the condition that permits the
single targeted recollection. Keep it in final partial-state calculation so
unknown or duplicate output remains visible and cannot become completed.

Do not retry unknown agents, repeat successful specialists, or add another
turn beyond the existing single recollection.

Run the focused tests again. Expected GREEN: pass.

## Task 3: Regression And Safety Verification

Run:

```powershell
uv run pytest tests/diagnosis/test_agents_runtime.py tests/diagnosis/test_agents_sdk_contract.py -q
```

Expected: all focused runtime and SDK contract tests pass.

Confirm specifically that true SDK errors/cancellation do not recollect, final
synthesis invalidity fails visibly, evidence allowlists remain enforced, and
the overall timeout still covers recollection.

## Review Gates

### Spec Compliance Review

Provide the reviewer:

1. The complete new spec.
2. This plan and the implemented task text.
3. `AGENT.md` and the DiagOps review checklist.
4. The scoped diff and focused test output.

The reviewer must reject prompt/gate changes, generic retries, hidden partial
failures, weakened evidence validation, or any production mutation behavior.

### Code Quality Review

After spec approval, review for correctness, exception-boundary regressions,
unnecessary abstractions, test realism, stale comments, and accidental changes
outside the three scoped files.

Blocking findings return to the implementer and must be re-reviewed.

## Final Verification

Run fresh:

```powershell
uv run pytest tests/diagnosis/test_agents_runtime.py tests/diagnosis/test_agents_sdk_contract.py -q
uv run ruff check .
uv run pytest -q
git diff --check
```

Expected:

1. Focused tests pass with zero failures.
2. Ruff exits zero.
3. Full pytest exits zero; the known Starlette deprecation warning is allowed.
4. `git diff --check` exits zero.

Do not run paid live acceptance automatically. Report the user-held live
diagnostic as the next optional verification step.
