# Current DiagOps Iteration

This file is the mutable routing and status entry point for the current version. It selects the active iteration and links to its authoritative documents; it does not define product requirements.

It does not override an approved spec or plan, current code contracts, or the long-term product goal and production safety boundary in `AGENT.md`.

Updated: 2026-08-01

## Implemented Baseline

V10 shared signal semantics is the implemented baseline. It retains the V9
durable and observable agent runtime while routing both OpenRCA and production
Prometheus telemetry through one bounded signal-semantics core, segment-aware
Attribution, a thin field-only OpenRCA Projector, an Alertmanager webhook
boundary, and a seven-scenario production Gate (passed 7/7 with onset error
≤60s, evidence references 100%, read-only violations 0, zero answer leakage).
The OpenRCA frozen six-case targeted Gate scored 2/6 and was accepted by the
user on 2026-07-31; see
`docs/superpowers/openrca-v10-6-case-failure-analysis.md`.

Implemented baseline commit: merge `26c8670` of `agent/v10-shared-signal-semantics`
(gates ran on source identity `6a7df40e962e514167bc539f20d89467a0f70abb`)

The current platform uses SQLite by default, retains an in-memory repository for tests and explicit configuration, and includes an optional default-off OpenAI Agents SDK runtime with deterministic RCA fallback.

The frozen V8.2 OpenRCA 40-case result remains registered below as the
independent comparison baseline for V9.

## Active Iteration

Version: V10.1

Main goal: correct the known OpenRCA counter, degenerate-baseline strength, and
equal-score ranking defects without fitting the exposed six-case development
set or regressing the V10 production Gate.

Iteration status: `implementing`

Spec status: `approved`

Plan status: `approved`

Implementation status: `in_progress`

Completion commit: `none`

Verification evidence: `none`

Blocker: `none`

Allowed values:

```text
Iteration status: proposed | planning | ready | implementing | verifying | complete | blocked
Spec status: missing | draft | review_required | approved
Plan status: missing | draft | review_required | approved
Implementation status: not_started | in_progress | verifying | complete | blocked
```

Do not execute this iteration until both spec and plan status are `approved`, unless the user explicitly selects a different already-approved spec and plan.

This gate applies to tracked Standard and Full work and to changes claimed as
part of this active iteration. An independent Light change that meets
`iteration-flow`'s Light criteria requires no Spec, Plan, or update to this
file. A Light correction inside this approved iteration reuses the existing
artifacts and updates only applicable task, evidence, and status fields.

State consistency rules:

1. `ready` requires approved spec and plan with implementation `not_started`.
2. `implementing` requires approved spec and plan with implementation `in_progress`.
3. `verifying` requires approved spec and plan with implementation `verifying`.
4. `complete` requires implementation `complete`, a non-`none` completion commit, and recorded verification evidence.
5. `blocked` requires a non-`none` `Blocker` with a concise reason or document reference.
6. Update the implemented baseline only from a verified `complete` iteration.

## Active Documents

```text
docs/superpowers/specs/2026-08-01-diagops-v10.1-signal-ranking-reliability-design.md
docs/superpowers/plans/2026-08-01-diagops-v10.1-signal-ranking-reliability-implementation-plan.md
```

## V10.1 Planning Dashboard

Overall progress: `M0/M1 verified; M2 code+regression verified by review A; T8 candidate checkpoint awaiting user Git authorization`

Current phase: Full iteration / Execute

Next action: user authorizes the candidate Git commit, then T8 runs the
production Gate (must stay 7/7) and M2 review B verifies artifacts; only then
M3 one-time blind paired Gate.

| ID | Phase | Task | Status | Evidence or result | Next action or blocker |
| --- | --- | --- | --- | --- | --- |
| P1 | Context | Verify V10 failure chain and current contracts | verified | V10 frozen six-case artifact/hash rechecked; current OpenRCA adapter, Signal Core, Attribution and focused tests inspected on 2026-08-01 | Preserve evidence |
| P2 | Requirements | Confirm defect scope, unbiased holdout policy and V10 compatibility gates | verified | User confirmed the recommended Requirements Brief on 2026-08-01 | Preserve decision |
| P3 | Design | Select the minimum semantic repair and blind paired evaluation approach | verified | User selected scheme A on 2026-08-01 | Preserve decision |
| P4 | Spec | Write and independently cold-review the V10.1 contract | verified | F1–F9 resolved；focused re-review closed blocking F4/F5 and high F6；user approved on 2026-08-01 | Preserve artifact |
| P5 | Plan | Map R1–R14 to ordered implementation and verification gates | verified | Independent cold review found no blocking；F10–F12 resolved；user approved on 2026-08-01；M1 review F17/F18 forced T6 scope amendment, user re-approved the amended plan on 2026-08-01 | Execute M2 |
| M0 | Execute | T1–T2 blind tooling, custodian holdout, frozen V10 baseline | verified | T1: 63 focused passed + review A approve_with_followups; T2: commit `4a4b20a`, holdout 40-case SHA `aac4b89bc9b3b248bfd0fdb8b5c783805655c30d3aeff13e81eac6f23f7f9936` (overlap 0, per-partition 10), V10 baseline `run-20260801T071800886742Z` 40/40 unscored on source `7b2602457561875ae8091e4d8eeea1bcddc2d34d`; review B approve_with_followups (F13–F16 low, absorbed) | Done |
| M1 | Execute | T3–T5 signal semantics repair | verified | T3/T4 complete (51+118 focused passed); T5 selection done, Attribution consumer-layer gap found by M1 cold review: blocking F17 (domain validator onset re-sort) + medium F18 (dependency payload), fixes approved into T6 scope and closed there; 618 merged tests passed | Done |
| M2 | Execute | T6–T8 compatibility, regression, production Gate | in_progress | T6: F17/F18 closed, 247+111 focused passed; T7: Ruff clean, 1634 full passed, frontend build ok, runtime acceptance 14/14 + privacy; M2 review A approve_with_followups (F19 fixed with red-green evidence, F20 recorded) | T8 blocked on user candidate Git authorization |

## V10 Task-Aware Projector Targeted Gate

The approved task-aware Evidence Projector was executed inline against the
frozen six-case development set and stopped at its first real Gate failure:

```text
Run: D:\data\OpenRCA\results-v10-targeted-projector\run-20260730T090304525928Z
Run ID: run-20260730T090304525928Z
Cases: 6/6 completed
Evidence reference validity: 100%
Read-only violations: 0
Projection errors/fallbacks: 0/0
Official rows with score > 0: 1/6
Official partial score: 0.08333333333333333
task_1/time: 0/2 positive
task_2/reason: 0/2 positive
task_3/component: 1/2 positive
Gate errors:
- official targeted score must pass at least 3 of 6 cases
- official targeted task_1 must have a positive score
- official targeted task_2 must have a positive score
```

Only `Bank:12` received a positive official score (`0.5`) because `MG01`
matched one of two expected components. The projector selected the wrong
times for both time cases and the wrong reasons for both reason cases. This
shows that deterministic field-aware projection can expose candidate
diversity without recovering the missing causal semantics required by the
target tasks.

Artifact identities:

```text
Frozen six-case runtime-cases.json:
B249E2F6B0B0DBD3B9F30AA71EF3302FF2C48A05CC50B914FB3DCBBC8800AD4B
fixed-predictions.csv:
F92D84E49EB0A927F6B4FCACEC0D0AA84AA182A997BDB7CDD65CCEF5F635E828
compatible-report.csv:
B24629EBC9C9E337409E34F63544FD5DCB9D141DE0B544AEF08D28A77AB0F553
official-report.csv:
4B80A6F2DBEEEA7516C6EA2DEECF8F2130B873BA0F5F36788B66FF1914C90867
summary.json:
DCEC7A5F15E6DF8D64B475C91793FFBBD14520722724B44EBBC36E3A3FC648BF
Official evaluator commit:
c1bd4af7f635171a1c31cdd567c07d698dff6abc
Run-reported repository HEAD:
568da9733e5a703355ac9cb743d5089b612412b5
```

The repository was dirty, so the recorded HEAD is not a reproducible source
identity for a formal candidate. The targeted Gate failed before the source
identity checkpoint, and no third 40-case run was started. This result is
OpenRCA-only and is not evidence of improved production root-cause accuracy.

## Frozen V8.2 OpenRCA Baseline

```text
Run ID: run-merged-20260724T043319743452Z
Commit: 38b118270542c3449c951b8f8425d618ee44fa59
Model: gpt-5.6-sol
Prompt version: v8.2
Cases: 40
Case manifest hash: 434e36603328fb5f0ec58730bcde5696931d2267dad99aca448cbfd6b52e812a
Manifest file SHA-256: FBB8C6D1D03A990E5753D22C34412A4CEB6F212B97F325EB51FD10176DAD345B
Artifact root: D:\data\OpenRCA\results-v8.2-final-38b1182-merged\run-merged-20260724T043319743452Z
Official query root: D:\data\OpenRCA\official-query-v8.2-final-38b1182-merged
Official report: D:\data\OpenRCA\results-v8.2-final-38b1182-merged\run-merged-20260724T043319743452Z\official-report.csv
```

The merged artifact contains 40 Fixed and 40 Adaptive rows. Both official
strict and partial scores are `0`. Fixed evidence-reference validity is
`100%`; Adaptive validity is `65.615%`; both strategies record zero read-only
violations. These are comparison facts, not V9 acceptance results.

## Artifact And Update Contract

Maintain exactly three core files for a tracked Standard or Full iteration:

1. This file is the mutable routing and status entry point. It stores the
   active version, main goal, approval and implementation status, authoritative
   document links, completion commit, verification evidence, and blockers. It
   must not duplicate requirements or override `AGENT.md`, an approved spec or
   plan, or implemented contracts.
2. The active spec under `docs/superpowers/specs/` is the requirements and
   design contract. Consolidate the verified baseline, risk obligations,
   affected-contract inventory, confirmed Requirements Brief, approved design,
   stable requirement IDs, traceability table, and review ledger there. Do not
   create separate Grill, Brainstorm, traceability, or review-ledger files.
3. The active plan under `docs/superpowers/plans/` is the execution contract.
   Store ordered file-level tasks, requirement mappings, focused checks, final
   gates, review milestones, migration or documentation work, and task status
   there.

Update the three files through the lifecycle:

1. On a new iteration, update this file first, set the iteration to `planning`,
   record the real artifact statuses, reset implementation and completion
   fields, and replace the active document links.
2. Set the spec to `draft` while it is written, `review_required` when it is
   ready for approval or its contract changes, and `approved` only after the
   user approves the current written content.
3. Create or revise the plan only from an approved spec. Set it to `draft`
   while it is written, `review_required` when ready for approval or when task
   ownership, ordering, architecture, or verification changes, and `approved`
   only after the user approves the current written content.
4. Set the iteration to `ready` only when both artifacts are approved. Set it
   to `implementing` with implementation `in_progress` when execution starts,
   and to `verifying` when final verification starts.
5. During planning and execution, extend the spec's single traceability table
   with plan tasks, focused checks, final gates, evidence, and status. Maintain
   the same review ledger from the first pre-approval review through final
   verification. Update Plan task status as work progresses.
6. If new evidence changes the goal, scope, behavior, public or persisted
   contract, safety boundary, migration, or acceptance criteria, set both spec
   and plan to `review_required`. If only implementation ownership, ordering,
   architecture, or verification changes, set only the plan to
   `review_required`. Never execute under stale approval.
7. Set the iteration and implementation to `complete` only when every retained
   requirement has fresh verification evidence, no blocking finding remains,
   and the completion commit is recorded. Use `blocked` only with a concise
   blocker or evidence reference.
8. Keep the statuses and document links in this file consistent with the
   actual artifact contents. File existence alone is not approval; stop when
   this file, the spec, and the plan disagree materially.

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
