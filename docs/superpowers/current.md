# Current DiagOps Iteration

This file is the mutable routing and status entry point for the current version. It selects the active iteration and links to its authoritative documents; it does not define product requirements.

It does not override an approved spec or plan, current code contracts, or the long-term product goal and production safety boundary in `AGENT.md`.

Updated: 2026-07-14

## Implemented Baseline

V8.1 reliability hardening and explicit OpenAI/DeepSeek Provider compatibility
are the implemented baseline.

Implemented baseline commit: `87489de3336514040154d61f3d4ba3e4819d71d6`

The current platform uses SQLite by default, retains an in-memory repository for tests and explicit configuration, and includes an optional default-off OpenAI Agents SDK runtime with deterministic RCA fallback.

## Active Iteration

Version: V8.1

Main goal: harden the deterministic platform and legacy boundaries, close the remaining V7 live-reliability gaps, and make the project-owned multi-agent RCA runtime work consistently with explicitly supported OpenAI-compatible model providers.

Iteration status: `complete`

Spec status: `approved`

Plan status: `approved`

Implementation status: `complete`

Completion commit: `87489de3336514040154d61f3d4ba3e4819d71d6`

Verification evidence: Ruff passed; full pytest 774 passed with one existing Starlette warning; migration/concurrency/redaction 24 passed; frontend build passed with existing TanStack warnings; git diff check passed; canonical artifact attribution/reference/action/safety/classification scans passed; OpenAI run `v8-1-live-20260714T060212861267Z-b5f43e64` certified; DeepSeek run `v8-1-live-20260714T080700796242Z-ffee9018` failed; final DeepSeek diagnostic `v8-1-diagnostic-20260714T090134835432Z-405b87b3` stopped below its gate at 2/5 correct real reviews

Blocker: `none`

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
docs/superpowers/specs/2026-07-12-diagops-v8.1-reliability-and-provider-compatibility-design.md
docs/superpowers/specs/2026-07-14-diagops-v8.1-live-validation-and-timeout-amendment-design.md
docs/superpowers/specs/2026-07-14-diagops-v8.1-specialist-finding-and-fallback-review-amendment-design.md
docs/superpowers/specs/2026-07-14-diagops-v8.1-diagnostic-entry-policy-amendment-design.md
docs/superpowers/plans/2026-07-12-diagops-v8.1-reliability-and-provider-compatibility-implementation-plan.md
docs/superpowers/plans/2026-07-14-diagops-v8.1-live-validation-and-timeout-amendment-implementation-plan.md
docs/superpowers/plans/2026-07-14-diagops-v8.1-specialist-finding-and-fallback-review-amendment-implementation-plan.md
docs/superpowers/plans/2026-07-14-diagops-v8.1-diagnostic-entry-policy-amendment-implementation-plan.md
```

## Entry-Gated Next Iteration

V8.2 bounded follow-up evidence is a proposal, not active implementation.

Entry gate: use the linked V8.2 spec. Do not activate V8.2 until that gate passes and the user approves proceeding.

```text
docs/superpowers/specs/2026-07-12-diagops-v8.2-bounded-follow-up-evidence-design.md
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
