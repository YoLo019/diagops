# Current DiagOps Iteration

This file is the mutable routing and status entry point for the current version. It selects the active iteration and links to its authoritative documents; it does not define product requirements.

It does not override an approved spec or plan, current code contracts, or the long-term product goal and production safety boundary in `AGENT.md`.

Updated: 2026-07-22

## Implemented Baseline

V8.2 bounded Specialist-owned parameterized evidence tools and reproducible
OpenRCA fixed-versus-adaptive benchmark code are the implemented baseline.

Implemented baseline commit: `3f06d9ae87729185b3429c839f11cc366de75000`

The current platform uses SQLite by default, retains an in-memory repository for tests and explicit configuration, and includes an optional default-off OpenAI Agents SDK runtime with deterministic RCA fallback.

The real OpenRCA 40-case result remains an independent V9 release gate because its frozen evidence is not present in this workspace.

## Active Iteration

Version: V9

Main goal: add a durable and observable domain runtime with isolated concurrent Investigation sessions, explicit recovery, safe events, replay, and Run Diff.

Iteration status: `verifying`

Spec status: `approved`

Plan status: `approved`

Implementation status: `verifying`

Completion commit: `none`

Verification evidence: `output/runtime-acceptance/runtime-20260722T042706538497Z-3efea43d/result.json` and the fresh key-free gate results below

Blocker: real OpenRCA dataset, Provider credentials, 40-case artifacts, and upstream evaluator checkout are unavailable; the live paired gate and real Fixed/Adaptive Replay and Diff remain required

Allowed values:

```text
Iteration status: proposed | planning | ready | implementing | verifying | complete | blocked
Spec status: missing | draft | review_required | approved
Plan status: missing | draft | review_required | approved
Implementation status: not_started | in_progress | verifying | complete | blocked
```

Do not execute this iteration until both spec and plan status are `approved`, unless the user explicitly selects a different already-approved spec and plan.

State consistency rules:

1. `ready` requires approved spec and plan with implementation `not_started`.
2. `implementing` requires approved spec and plan with implementation `in_progress`.
3. `verifying` requires approved spec and plan with implementation `verifying`.
4. `complete` requires implementation `complete`, a non-`none` completion commit, and recorded verification evidence.
5. `blocked` requires a non-`none` `Blocker` with a concise reason or document reference.
6. Update the implemented baseline only from a verified `complete` iteration.

## Active Documents

```text
docs/superpowers/specs/2026-07-17-diagops-v9-durable-observable-runtime-design.md
docs/superpowers/plans/2026-07-17-diagops-v9-durable-observable-runtime-implementation-plan.md
```

## Update Contract

When the user declares a new active version or main iteration goal:

1. Update `Updated`.
2. Update the implemented baseline version and baseline commit SHA only if the previous iteration is verified complete.
3. Replace the active version, one-sentence main goal, statuses, and active document links.
4. Set spec and plan status to their real approval state; file existence does not mean approval.
5. Reset completion commit, verification evidence, and blocker to `none` when starting a new iteration.
6. Enforce the state consistency rules above whenever status changes.
7. Update or remove the entry-gated next iteration.
8. Keep old specs and plans as history; do not rewrite them to describe the new version.
9. Do not copy acceptance criteria into this file; keep them in the spec.
10. Do not modify the long-term product goal in `AGENT.md` unless the user explicitly says the final or long-term goal is changing.

## V9 Verification Evidence

Key-free gates were rerun from the shared V9 worktree on 2026-07-22:

```text
uv run ruff check .
exit 0; All checks passed.

uv run pytest -q
exit 0; 1383 passed, 1 warning in 217.17s.

uv run pytest -q tests/runtime/test_manager.py
tests/runtime/test_fault_injection.py
exit 0; 28 passed in 11.37s.

npm.cmd --prefix frontend run build
exit 0; TypeScript and Vite production build succeeded; 81 modules transformed,
Vite build time 1.48s. Existing TanStack React Query "use client" bundle
warnings remained non-fatal.

uv run python -m backend.services.runtime_acceptance
exit 0; 14/14 required scenarios passed; privacy scan passed with zero markers.
Artifact: output/runtime-acceptance/runtime-20260722T042706538497Z-3efea43d/result.json
The artifact directory contains only `result.json`. It records Git HEAD
`3f06d9ae87729185b3429c839f11cc366de75000`, `git_dirty=true`, and candidate
diff SHA-256 `5ef53c747980a36aa5fcec9d35fda46429c070bd4445e584ba38d44cadf1793e`.
That digest identifies the complete code/test candidate immediately before this
evidence-only routing document was updated; it is not claimed as a digest of a
self-referential final worktree containing the digest itself.
```

The acceptance artifact records these raw measurements without an additional
pass threshold:

```text
Each latency mode: 10 raw samples after 1 warmup iteration
V8.2-compatible sync wall time: p50 295.348 ms; p95 328.070 ms
Runtime-disabled wall time: p50 293.680 ms; p95 326.143 ms
Runtime-enabled wall time: p50 449.305 ms; p95 880.744 ms
Runtime-disabled versus sync p50: -0.565%
Runtime-enabled versus Runtime-disabled p50: +52.991%
Runtime-enabled versus sync p50: +52.127%
SQLite growth: 64716.8 bytes/Run
Runtime events: 28/Run
Runtime checkpoints: 8/Run
Recovery wall time: 9.274 ms
OpenTelemetry disabled/enabled wall time: 0.162/5.608 ms
OpenTelemetry enabled overhead: +3361.543%
```

A V8.2-compatible schema V5 SQLite source containing one historical
Investigation was created under the system temporary directory, copied, and
only the copy was migrated. The copy reached schema V6,
`PRAGMA foreign_key_check` returned no rows, the historical Investigation
remained readable, no synthetic Runtime rows were created, and
`runtime_available=false`. The evidence copy is under
`C:\temp\diagops-v9-migration-d8a44f1731b344cf88e3f96cf7eeaf91`.
No user database was modified; `data/diagops.db` was absent.

## Release-gate Skips And Risks

Fresh checks found `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
`DIAGOPS_OPENAI_API_KEY`, and `DIAGOPS_DEEPSEEK_API_KEY` unset. The real
OpenRCA dataset, frozen 40-case result, and separate upstream evaluator checkout
are also absent. Therefore no fixture was presented as a live gate and these
checks remain blocked:

- the real paired 40-case Fixed and Adaptive run;
- the upstream official evaluator and V8.2 score comparison;
- Replay of one real Fixed and one real Adaptive Run;
- stable Diff verification for the real paired Runs.

V9 remains `verifying`. Completion requires those release gates plus an
explicitly authorized non-`none` completion commit.
