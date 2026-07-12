# DiagOps V7 Specialist Recollection Error Semantics Design

## 1. Goal

Fix the V7 real multi-agent runtime so a missing or invalid first-turn
Coordinator proposal does not prevent the one approved targeted recollection
of missing round-1 specialists. Preserve unknown and duplicate specialist
output as visible partial failure without allowing it to block recollection of
other missing required specialists.

The motivating live artifact is
`artifacts/v7-live-acceptance-20260712T070821720309Z.json`. All 15 runs remained
structurally valid and all safety gates passed, but only 8 produced real reviews
and only 7 produced correct candidates. Several runs requested all three tools
but retained only two valid findings. The production SDK adapter currently
combines Coordinator proposal validation failure with SDK
transport/execution failure, while the runtime requires `first.error is None`
before recollection.

## 2. Non-goals

This change does not:

1. Change specialist or Coordinator prompts.
2. Change reliability-gate thresholds or acceptance scoring.
3. Add a retry loop, backoff, configurable retry count, or generic retry
   abstraction.
4. Retry a successful specialist or an unknown agent name.
5. Relax evidence allowlists, read-only safety, structured validation, timeout,
   turn limits, or final synthesis requirements.
6. Change domain models, persistence, APIs, frontend behavior, fixtures, or
   artifact schema.
7. Guarantee that a nondeterministic provider passes the paid live gate.

## 3. Required Behavior

### 3.1 Separate SDK Failure From Proposal Validation Failure

The production SDK turn adapter must distinguish these outcomes:

1. If `Runner.run` raises because of cancellation, transport, provider,
   execution, or SDK failure, the turn records an error. Cancellation remains
   explicitly marked.
2. If `Runner.run` succeeds but its final Coordinator output cannot be
   validated as `_CoordinatorProposal`, the turn returns
   `coordinator_proposal=None`, `error=None`, and `cancelled=False`.
3. Captured specialist drafts and token usage remain available in both cases
   when the SDK exposes them.

Proposal validation failure remains visible through the existing Coordinator
task/execution failure record created by `_consume_turn`. It is not silently
treated as success.

### 3.2 Targeted Recollection

After consuming the first turn, the runtime derives missing specialists only
from validated round-1 findings. When required specialists are missing and the
first SDK turn did not error or cancel, it performs exactly one recollection
turn containing only those missing required specialists.

The recollection uses the existing original specialist inputs, evidence
allowlists, read-only boundary, `analysis_round=1`, turn limit, and overall
runtime timeout. Successful specialists are not repeated.

An invalid or missing first Coordinator proposal does not block recollection.
The failed Coordinator record remains, so successful recollection cannot turn
that run into `completed`.

### 3.3 Unknown And Duplicate Output

Unknown or duplicate first-turn specialist output remains non-recoverable and
keeps the run `partial`. It does not block recollection of other missing
required specialists.

The runtime does not retry the unknown agent itself. It does not remove or
rewrite the failure record that makes the invalid output visible.

### 3.4 Final Synthesis

Final synthesis behavior does not change. A synthesis SDK error or missing or
invalid final Coordinator proposal still prevents a completed agent review and
preserves fallback/partial semantics.

## 4. Error Handling And Safety Boundaries

1. True SDK error or cancellation before usable first-turn findings preserves
   the existing failed/fallback path and does not recollect.
2. True SDK error after partial first-turn findings remains visible and does
   not initiate recollection.
3. Invalid evidence ids, invalid analysis rounds, unknown agents, and duplicate
   agents cannot become accepted findings.
4. Recollection failure remains visible as failed task/execution state.
5. The one overall timeout covers the first turn, recollection, and synthesis.
6. The change remains read-only and cannot execute rollback, restart, scaling,
   SSH, or configuration mutation.

## 5. Compatibility

There are no public model, enum, API, persistence, fixture, or artifact schema
changes. Existing deterministic fallback and safety behavior remains backward
compatible.

## 6. Tests And Acceptance Criteria

Automated tests must prove:

1. The production SDK adapter returns `proposal=None`, `error=None`, and
   `cancelled=False` when `Runner.run` succeeds but final proposal validation
   fails, while retaining captured specialist drafts and usage.
2. A first turn with a missing/invalid Coordinator proposal and valid partial
   specialist findings recollects only missing specialists once and remains
   partial.
3. Unknown output plus missing required specialists recollects only the missing
   required specialists once and remains partial.
4. Duplicate output plus missing required specialists recollects only the
   missing required specialists once and remains partial.
5. Successful specialists are not repeated.
6. True SDK error and cancellation do not recollect.
7. Final synthesis invalidity still fails visibly.
8. Existing timeout, evidence allowlist, read-only, and no-wrong-agreement tests
   remain green.

Required verification:

```powershell
uv run pytest tests/diagnosis/test_agents_runtime.py tests/diagnosis/test_agents_sdk_contract.py -q
uv run ruff check .
uv run pytest -q
git diff --check
```

The paid live acceptance is intentionally excluded from automated completion
because it requires user-held credentials and incurs provider cost. After all
key-free checks pass, the user may run one diagnostic cohort before deciding
whether to rerun the canonical gate.
