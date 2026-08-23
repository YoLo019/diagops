---
name: diagops-release
description: Guide a high-risk DiagOps change from alignment through verified release.
---

# DiagOps Release

Run one gated release effort without loading its full history or every later
phase. This skill orchestrates; `grilling` and `domain-modeling` provide the
reusable alignment disciplines.

## Start Or Resume

1. Read `AGENTS.md` and `docs/superpowers/current.md`.
2. Identify the current phase and authoritative spec or plan. Read only the
   sections needed for that phase.
3. Read `docs/superpowers/workflow-history-through-2026-08-22.md` only when a
   current decision depends on historical evidence.
4. Record the source commit or working-tree baseline used for later review.

## Flow

### 1. Align

Invoke the host skill mechanism separately for `grilling` and
`domain-modeling`. Verify repository facts directly and resolve the decision
frontier with the user. Finish only when the frontier is empty, terminology and
hard-to-reverse decisions are recorded where needed, and the user confirms the
shared understanding.

### 2. Specify

Read [references/specification.md](references/specification.md). Reuse an
approved spec when it still covers the agreed contract. Finish with an approved
spec whose retained requirements each have an observable acceptance check.

### 3. Slice

Read [references/slices.md](references/slices.md). Turn the spec into approved
tracer-bullet slices with explicit blocking edges. Each slice must fit a fresh
context window and leave a demonstrable or independently verifiable result.

### 4. Implement

Read [references/implementation.md](references/implementation.md). Work only the
unblocked frontier, one vertical slice at a time, using the agreed test seams and
the smallest feedback loop.

### 5. Review And Finish

Read [references/review-and-finish.md](references/review-and-finish.md). Review
the fixed diff separately for repository standards and spec fidelity, close
valid findings, run fresh release verification, and update the short status
index.

## Stop Gates

Stop before continuing when authorization is missing, a required external gate
cannot run, or new evidence changes scope, public or persisted contracts,
safety boundaries, migration behavior, or acceptance criteria. Return to the
earliest affected phase and invalidate downstream approval.

A correction that stays inside the approved contract and slice needs no new
approval. Git finish actions still require explicit user instruction.
