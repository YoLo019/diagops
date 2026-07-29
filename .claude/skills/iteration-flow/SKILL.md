---
name: iteration-flow
description: Standalone risk-based software iteration workflow for major versions, features, behavior changes, refactors, and narrow fixes. Use when the agent should guide work through context discovery, optional requirements grilling, collaborative design, detailed specification, detailed implementation planning, execution, review, verification, and finish without depending on other workflow skills. In this repository it takes precedence over superpowers workflow skills (brainstorming, writing-plans, executing-plans, subagent-driven-development, requesting-code-review).
---

# Iteration Flow

Run a gated development workflow whose cost scales with risk. Keep specifications and implementation plans complete; save time by reducing unnecessary subagent and review calls.

When this skill is active, it owns the end-to-end workflow. Do not invoke other
workflow skills such as brainstorming, writing-plans, executing-plans, or
subagent-driven-development unless the user explicitly requests one. The phases
below contain the required disciplines without their duplicate gates.

## Core Rules

1. Read repository instructions before choosing a workflow level.
2. Treat an explicit, unambiguous Light request as confirmed. Ask only when a
   material requirement or risk boundary is unclear.
3. For Standard and Full, do not write a plan before the user approves the written spec.
4. For Standard and Full, do not execute before critically reviewing the plan.
5. Do not claim completion without fresh verification evidence.
6. Do not perform git finish actions unless the user requested them.
7. Use the smallest implementation that satisfies the confirmed request or approved spec.
8. Verify material baseline assumptions before writing requirements from them.
9. For Standard and Full, keep every requirement traceable from spec to plan task to verification.
10. Invalidate approval when later evidence changes an approved contract.

## Choose A Level

Choose the lightest level that matches the risk.

| Level | Use for | Required artifacts | Execution and review |
| --- | --- | --- | --- |
| Light | Docs, copy, tests, local refactors, contract-restoring bug fixes, and reversible local behavior changes with no contract, persisted-data, safety, concurrency, migration, or architecture impact | None | Direct execution, focused check, self-review |
| Standard | Normal features, providers, APIs, internal services, and UI behavior | Complete spec and complete plan | Inline execution, one final review |
| Full | Major versions, cross-module architecture, public contract changes, migrations, concurrency, security, safety boundaries, or difficult-to-reverse work | Complete spec and complete plan | Staged execution, milestone reviews |

The remaining phases apply only when enabled by the selected level. A skipped phase does not need an output.

### Light Fast Path

Choose Light only when all are true:

1. The goal and observable acceptance result are explicit and unambiguous.
2. The change does not alter a public or consumer contract, persisted data,
   permissions, safety boundary, migration, concurrency behavior, or shared
   architecture.
3. The change is local, reversible, and provable with a focused check.

For Light:

1. Inspect only enough context to verify the classification and locate the
   correct change point.
2. State the Light classification and reason, then execute without a separate
   Grill, Brainstorm, Spec, Plan, approval request, traceability table, review
   ledger, or subagent.
3. Add or update the smallest meaningful check, run it, self-review the diff,
   and report fresh evidence.
4. Do not create or update a status entry, spec, or plan for an independent
   Light change. If the change is inside an active approved iteration, reuse its
   artifacts and update only applicable task, evidence, and status fields.
5. Before continuing, escalate to Standard or Full if implementation reveals
   ambiguity or any excluded risk. Do not use Light to bypass an active contract.

For Full, run one independent artifact review before presenting the spec and one
before presenting the plan. These replace repeated per-task reviewers; do not
add reviewers to mechanical implementation steps. If independent review is not
available, perform a cold self-review from the written artifact and repository
state rather than the author's prior explanation.

Maintain only two control artifacts for Standard and Full:

1. One evolving traceability table, started with the spec and extended by the
   plan and verification. Use columns `requirement_id`, behavior, baseline
   evidence, acceptance check, plan task, focused check, final gate, and status.
   Leave unavailable plan columns empty until the plan exists; do not create a
   second coverage matrix.
2. One review ledger, started by the first pre-approval review and reused through
   completion. Use columns `finding_id`, origin, severity, root cause,
   disposition, resolution, regression check, and status. `origin` is `missed`,
   `new_evidence`, or `scope_change`. A rejected finding requires a technical
   rationale. A resolved blocking finding requires a focused re-review.

Store these in the repository's existing workflow artifact. Otherwise keep the
traceability table in the active spec and the review ledger in the active plan
or spec. Filling plan, evidence, and status columns without changing the
approved contract does not invalidate approval.

## Phase 1: Context And Risk Triage

Read:

1. Repository agent instructions such as `AGENT.md`, `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md`.
2. `README.md` and relevant project documentation.
3. Existing specs and plans related to the request.
4. Relevant code, recent commits, and the current diff when continuing work.
5. Public and persisted contracts affected by the request: APIs, models,
   schemas, configuration, consumers, safety boundaries, and historical data.

Produce:

1. A short summary of the affected system and existing behavior.
2. Constraints, unknowns, and risks.
3. The selected workflow level.
4. Whether the Grill phase is required.
5. A baseline evidence record separating verified behavior from assumptions.
6. A risk-obligation list identifying every applicable category that requires
   design and verification evidence.
7. An affected-contract inventory with `surface`, current source of truth,
   consumers, proposed change, and compatibility treatment.

For Standard and Full, verify every material assumption that could change the
design. Use the smallest representative normal check and a separate check for
every applicable risk category; do not let one failure case stand in for
compatibility, migration, concurrency, security, and degradation simultaneously.
For persisted data, inspect every supported starting schema. Record the command
or inspection, observed result, and unresolved assumption. Passing existing
tests is evidence only for what those tests assert; it is not proof of untested
behavior.

Consider these proof obligations when applicable:

1. Public/API/configuration contracts and consumer version skew.
2. Every supported persisted-data upgrade path, interrupted migration, restart,
   rollback/atomicity behavior, and physical schema validation.
3. Concurrent interleavings, lock/busy behavior, retries, idempotency, and lost
   updates.
4. Optional dependency construction, execution, validation, persistence, and
   recovery failures, including combinations that affect fallback.
5. Partial results, timeout, cancellation, replay, and lifecycle cleanup.
6. Trust-boundary input, sensitive-data persistence/projection, authorization,
   and adversarial behavior.

Assign each applicable obligation an ID and map it to a requirement and check
in the traceability table, or mark it as an explicit external blocker. For
obligations that share the same persisted state, transaction, lifecycle, or
fallback boundary, analyze the highest-risk interaction as its own obligation;
do not generate a Cartesian product of unrelated risks.

Before Grill, split only independently releasable work whose contracts can be
approved and verified without the other children. Use one short parent design
for shared contracts and a separate spec/plan cycle for each such child. Keep
cross-cutting work with shared contracts in one spec and divide it into coherent
milestones. Do not use document length or risk count alone as the split rule.

Run Grill when any condition matches:

1. The problem, user, urgency, or success metric is unclear.
2. The request spans three or more modules or introduces a major dependency.
3. It affects security, permissions, production writes, concurrency, migrations, or public contracts.
4. It is expensive or difficult to reverse.
5. Requirements conflict or depend on unverified assumptions.
6. Existing behavior may already solve enough of the problem.

Skip Grill in Light. Run it conditionally in Standard. Run it by default in Full.

## Phase 2: Grill Requirements And Decisions

Grill turns the request into a confirmed, implementation-neutral requirements
brief. Interview the user one question at a time and follow dependent branches
until each material decision is resolved. Look up facts from the repository or
environment instead of asking the user; present a recommended answer when the
user must make a decision.

Clarify and challenge only what is relevant:

1. Problem, affected user, evidence, urgency, and consequence of doing nothing.
2. Current behavior, desired behavior, and the user-visible workflow.
3. Smallest valuable scope, explicit non-goals, and priority among competing needs.
4. Data, API, UI, safety, operational, compatibility, and migration constraints.
5. Acceptance criteria, representative normal and failure cases, and measurable success.
6. Unverified assumptions, conflicting requirements, and dangerous or misleading failures.
7. Product or behavior choices and high-cost or difficult-to-reverse technical
   constraints that materially change the requirement.

Challenge premature solutions when they hide an unstated requirement. Briefly
compare options only when needed to resolve a requirement or decision branch.
Do not produce complete architectures, choose libraries, assign modules, or
settle detailed data flow here; those belong to Brainstorm.

Finish with a Requirements Brief:

```text
Decision: continue | narrow | reframe | stop
Problem:
Target user:
Evidence:
Current behavior:
Desired behavior:
Scope:
Non-goals:
Priority / why now:
Constraints:
Acceptance criteria:
Representative cases:
Resolved decisions:
Unverified assumptions:
Largest failure risk:
Open questions:
```

Ask the user to confirm the Requirements Brief. Enter Brainstorm only when
`Open questions` is `none` or every remaining item has an explicit external
owner and is recorded as a blocker. For `narrow` or `reframe`, confirm the
revised brief. For `stop`, do not enter solution design.

## Phase 3: Brainstorm And Design

If Grill ran, treat its confirmed Requirements Brief as the input contract. Do
not re-ask or silently change requirements.

Present 2-3 complete approaches with tradeoffs and a recommendation. Compare
architecture, component responsibilities, interfaces, data flow, failure
handling, compatibility, migration, and testing strategy at the depth required
by the selected level. Record the chosen approach and rejected alternatives.

If design work exposes a missing or contradictory requirement, return to Grill,
update and reconfirm the Requirements Brief, then resume Brainstorm. If Grill
was skipped, perform a compressed requirements challenge before proposing
solutions: confirm the problem, desired behavior, smallest useful scope,
constraints, largest failure risk, and acceptance criteria.

Wait for user approval of the chosen design before writing the spec.

## Phase 4: Detailed Spec

Use the repository's existing spec location. Otherwise use:

```text
docs/specs/YYYY-MM-DD-<topic>-design.md
```

Include:

1. Context and goal.
2. Scope and non-goals.
3. User-visible behavior.
4. Architecture and component responsibilities.
5. Data, API, and model contracts.
6. Main data flow.
7. Error handling and safety boundaries.
8. Compatibility, migration, and rollout considerations.
9. Test strategy and acceptance criteria.
10. Open risks with explicit resolutions or owners.
11. Stable requirement IDs such as `R1`, `R2`, and `R3` for every testable
    behavior, compatibility promise, safety boundary, and release gate.

When applicable, include explicit transition tables, trust/data-flow
boundaries, failure and partial-result behavior, concurrency/transaction
boundaries, and old-record migration behavior. Do not leave these for the plan
to invent.

Self-review for placeholders, contradictions, ambiguity, missing failure
behavior, unsupported baseline assumptions, unowned risks, and scope creep.
Add each requirement to the single traceability table. Every requirement must
have baseline evidence and an acceptance check or an explicit external owner
and release blocker.

For Full, give the independent reviewer the repository instructions, raw spec,
baseline evidence, risk obligations, affected contract inventory, and
repository entry points.
Require the reviewer to inspect relevant code, callers, migrations, tests,
consumers, history, and current diff independently; do not limit review to
author-selected excerpts or provide the intended answer. Record every finding
and disposition in the review ledger. Fix valid findings, give blocking or
contract-changing resolutions a focused re-review, update traceability, then
ask the user to review the written spec. Do not proceed until it is approved.

## Phase 5: Detailed Plan

Use the repository's existing plan location. Otherwise use:

```text
docs/plans/YYYY-MM-DD-<topic>-implementation-plan.md
```

Include:

1. Exact files to create or modify and their responsibilities.
2. Small ordered tasks that each leave the repository coherent.
3. Concrete implementation steps.
4. Tests or checks for every task.
5. Exact commands and expected results.
6. Review milestones.
7. Final verification commands.
8. Documentation or migration updates.
9. Requirement IDs covered by every task.
10. A dependency order in which every task is coherent and testable without a
    later task.

Extend the existing traceability table with plan task, focused check, and final
gate. Every approved requirement must map to at least one task and one check.
Re-read the plan in execution order and fix tasks that depend on later work,
duplicate ownership, inconsistent names, stale assumptions, vague steps, or
unnecessary abstractions.

For Full, give the independent reviewer the repository instructions, raw spec,
raw plan, baseline evidence, traceability table, and repository entry points.
Reuse the validated contract inventory and baseline discovery. Require the plan
reviewer to inspect repository evidence independently only for implementation
feasibility, changed assumptions, disputed findings, task ordering, and areas
the plan newly touches. Ask for findings only: missing requirements, contract
conflicts, unsafe ordering, compatibility gaps, unverifiable tasks, and excess
scope. Record each disposition and rationale in the review ledger, fix valid
findings, and run a focused re-review for blocking or contract-changing
resolutions before presenting the plan for user approval.

After plan approval, Standard defaults to inline execution without a separate execution-strategy decision. For Full, include the proposed milestones and execution strategy in the plan approval request.

## Approval Invalidation

Treat approval as applying to the written contract, not merely the filename.

1. Return the spec to `review_required` when new evidence changes the goal,
   scope, non-goals, user behavior, public or persisted contract, safety
   boundary, migration behavior, or acceptance criteria. Any plan based on it
   also becomes `review_required`.
2. Return only the plan to `review_required` when implementation ownership,
   task order, architecture, or verification changes without changing the
   approved spec contract.
3. Keep approval for a local implementation correction that remains inside the
   approved contract and existing task responsibility.
4. Update the repository's status entry point when one exists. Never continue
   execution under stale approval.

## Phase 6: Execute

For Light, follow the confirmed request directly and use the Light Fast Path.
For Standard and Full, work in plan order and keep each change scoped.

1. Add the smallest meaningful check for non-trivial logic.
2. Run each task's focused check before moving on.
3. Keep task status current.
4. Diagnose the root cause of repeated failures before continuing.
5. Stop when the plan becomes stale, unsafe, or materially ambiguous.
6. Apply the approval-invalidation rules before editing outside the approved
   task or contract.

Standard execution:

1. Implement related tasks inline as one coherent batch.
2. Avoid subagents for mechanical steps.
3. Review once after the batch.

Full execution:

1. Group related tasks into milestones.
2. Use subagents only for independent work with non-overlapping files and contracts, or for milestone review.
3. Give each subagent the repository instructions, relevant spec section, plan task, and expected checks.
4. Review each milestone before starting dependent work.

For Full, keep the existing review ledger current. Close a finding only after
its resolution and smallest regression check are verified. Reuse it in later
reviews so resolved findings are not rediscovered without new evidence.

## Phase 7: Review

Review the complete Standard batch or each Full milestone. For Full, this Phase
is the review invoked between Phase 6 milestones; do not run a duplicate review
for the same diff and evidence. Review a single task only when it is
independently high risk.

Provide the reviewer with:

1. Repository instructions.
2. Relevant spec and plan sections.
3. The diff under review.
4. Current test results.

Report findings first, ordered by severity, with file and line references.
Check the affected risk categories, not only the happy path:

1. Requirement and traceability-matrix compliance.
2. Public, persisted, configuration, and consumer compatibility.
3. State transitions, invariants, reference integrity, and unsupported claims.
4. Transaction, concurrency, retry, replay, and lifecycle behavior.
5. Failure, partial, timeout, cancellation, and recovery behavior.
6. Trust boundaries, sensitive-data handling, permissions, and production
   safety.
7. Focused regression checks and unnecessary code, dependencies, or scope.

Verify reviewer feedback against the codebase. Record valid findings in the
review ledger, apply approval invalidation when required, fix blocking issues,
and re-review only the affected scope unless a shared contract changed.

## Phase 8: Verify And Finish

Run the checks required by repository instructions, the plan, and the changed areas. Typically include focused tests, lint or type checks, builds, and API or UI smoke checks.

Do not report a check as passing unless it ran during the current work. State any skipped check and why.

Before completion, reconcile the traceability matrix against fresh evidence.
Every requirement must be `verified`, `blocked` with an external owner, or
explicitly removed through an approved spec amendment. Completion requires no
unresolved blocking finding and no requirement that is merely assumed.

Final response must state:

1. What changed.
2. What was verified.
3. Skipped checks and residual risks.
4. Deliberate simplifications.

Only commit, push, create a PR, merge, delete a branch, or remove a worktree when the user explicitly requests that action.
