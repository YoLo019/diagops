# Current DiagOps Iteration

This file is the mutable routing and status entry point for the current version. It selects the active iteration and links to its authoritative documents; it does not define product requirements.

It does not override an approved spec or plan, current code contracts, or the long-term product goal and production safety boundary in `AGENT.md`.

Updated: 2026-07-31

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

Version: V10

Main goal: remove audited whole-repository redundancy, replace duplicated
OpenRCA/production metric heuristics with one bounded signal-semantics core,
then prove cause/component/reason/onset through the OpenRCA targeted Gate and a
seven-scenario production Gate.

Iteration status: `complete`

Spec status: `approved`

Plan status: `approved`

Implementation status: `complete`

Completion commit: `6a7df40e962e514167bc539f20d89467a0f70abb` (implementation concluded; both Gates ran on this source identity; archive docs follow)

Verification evidence: T8 production Gate `output/production-acceptance/run-20260731T111932207Z/result.json` (7/7, SHA-256 `2c3a543a3816978ee9c16d335ecf145f97eb26ffb5441e244b652a7fb258e2a9`); T9 accepted failure `D:\data\OpenRCA\results-v10-shared-signal\run-20260731T123348473710Z` + `docs/superpowers/openrca-v10-6-case-failure-analysis.md`

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
docs/superpowers/specs/2026-07-30-diagops-v10-shared-signal-semantics-seven-scenario-gate-design.md
docs/superpowers/plans/2026-07-30-diagops-v10-shared-signal-semantics-seven-scenario-gate-implementation-plan.md
```

## V10 Execution Dashboard

Overall progress: `complete; T8 production Gate 7/7 verified; T9 OpenRCA targeted Gate 2/6 failed, user accepted and archived on 2026-07-31`

Current phase: V10 concluded with accepted T9 failure

Next action: none for V10. Failure analysis archived at
`docs/superpowers/openrca-v10-6-case-failure-analysis.md`; Amendment A1
(R17/R18) withdrawn after cold review (spec §16 A1-F1…A1-F8). The V10 code on
branch `agent/v10-shared-signal-semantics` is not merged; merge or discard is a
separate user decision. R13 recorded blocked with user acceptance in the spec
traceability.
Spec and plan were approved by the user on 2026-07-30 after the second cold
review resolved F16–F19. Stop at T7 source checkpoint for separate Git
authorization; do not run any live Gate before it.

| ID | Phase | Task | Status | Evidence or result | Next action or blocker |
| --- | --- | --- | --- | --- | --- |
| P1 | Baseline | Verify V9/OpenRCA failure path and affected contracts | verified | V9 completion evidence in Git history and V9 specs/plans; `docs/superpowers/openrca-v9-6-case-failure-analysis.md`; current API, Provider, Analyzer, persistence, and benchmark code inspected on 2026-07-29 | Preserve evidence |
| P2 | Requirements/design | Confirm shared-core scope, dual gates, production lab, Alertmanager boundary, and Approach A | verified | Requirements Brief and Approach A confirmed by the user on 2026-07-29 | Preserve confirmed contract |
| P3 | Initial spec | Write and cold-review the initial V10 design | verified | Initial shared-core spec approved by the user on 2026-07-29 | Superseded after targeted evidence |
| P4 | Initial plan | Write and cold-review the initial V10 implementation plan | verified | Initial revised plan approved by the user after M1 finding F14 on 2026-07-29 | Superseded after targeted evidence |
| T1 | M1 | Unify Provider Evidence semantics | verified | 38/38 focused tests; OpenRCA claims removed; canonical fields and bounded degradation verified | Preserve |
| T2 | M1 | Shared Analyzer, clustering, ranking, Attribution | verified | 62/62 focused tests; stable rules, UNKNOWN fallback, canonical component/reason and dynamic clustering verified | Preserve |
| T3 | M1 | Deterministic authority and Runtime compatibility | verified | 227/227 focused tests; Agent completed/failed/timeout cannot overwrite; historical fixture unchanged; canonical Attribution frozen and Replay-valid | Preserve |
| M1 | Review | Cold review shared core | verified | Combined M1 suite 329/329; Ruff and `git diff --check` passed; F14 resolved in code and recorded in plan/spec | Preserve |
| T4 | M2 | Key-free OpenRCA Fixed, Top-N, and release Gate | verified | Benchmark suite 51/51; fixture deterministic Run has completed Run/Attempt/checkpoints, zero Model events/tokens/cost, valid zero-external-call Replay; Top-N, safe count metadata, frozen checksums and Gate failures verified | Preserve; run real 40-case only at T8 |
| M2 | Review | Cold review OpenRCA boundary and artifacts | verified | M1+M2 combined suite 369/369; deterministic run does not build Agent Runtime or scoring locator; Top-N preserves shared-core rank; Ruff, format and `git diff --check` passed | Start T5 |
| T5 | M3 | Alertmanager webhook boundary | verified | Events focused 38/38; full API 124/124; firing/resolved, atomic envelope rejection, ordered partial failure, safe bounds/detail, URL isolation, and at-least-once duplicate delivery verified | Preserve |
| M3 | Review | Cold review Alertmanager boundary | verified | M1–M3 combined suite 493/493; old `/events` unchanged; no payload/exception echo or unknown field propagation; Ruff, format, example JSON and `git diff --check` passed | Start T6 |
| T6 | M4 | Production lab service and Compose | verified | Lab focused 6/6 and full services 132/132; six-component Compose parses; DiagOps evidence mounts are read-only; fixed metrics/log fields and lab-only controls verified | Preserve |
| T7 | M4 | Four-scenario production acceptance | verified | Real Compose Gate 4/4: deployment `0.70`, dependency `0.70`, traffic `0.60`, healthy UNKNOWN `0.20`; Evidence references 100%, read-only violations 0, duration 61.65s; artifact `output/production-acceptance/run-20260729T130529953Z/result.json`, SHA-256 `06CD6731985B534452F09276D4AD79C2DCFF9D61049C4B57229B53BEE59A0D92` | Preserve |
| M4 | Review | Cold review production boundary | verified | API/services 268/268; Ruff, `git diff --check`, Compose config and artifact validator passed; DiagOps evidence mounts remain read-only and fault control remains lab-only | Preserve |
| T8 | M5 | Full checks and dual final Gate | blocked | Task-aware Projector completed 6/6 with Evidence validity 100%, zero read-only violations and zero projection error/fallback, but official targeted Gate passed only Bank:12 at score 0.5; time and reason remained 0. Full pytest 1504/1504 and Ruff passed before the real run. | Stopped by the approved Gate; no tuning and no third 40-case run |
| P5 | Recovery spec | Shared Signal Semantics and seven-scenario Gate | verified | Whole-repository audit incorporated; written spec approved by the user on 2026-07-30 | Preserve approved contract |
| P6 | Recovery plan | Signal semantics plus whole-repository cleanup plan | verified | Amended spec and plan approved by the user on 2026-07-30 after the second cold review resolved F16–F19 | Start M0/CL0 |
| CL0 | M0 | Delete W1/W2 dead code before Signal Core | verified | Caller scan zero in production; deleted `context_store.py` (59) + dedicated test (120), `SharedInvestigationContext` (14), `WorkbenchGraph*` (17), `QueryEvidenceProviderProtocol` (7), three frontend wrappers (12), dead-type-only test code (29); net deletion 257 lines incl. 109 production; focused 57/57, Ruff clean, frontend build passed | Preserve |
| T1 | M1 | Implement pure Signal Core | verified | New `backend/diagnosis/signal_semantics.py`: 2 frozen dataclasses + 3 public pure functions; 35/35 focused tests (taxonomy precedence incl. packet scope, zero-baseline clamp, non-finite, gap/normal-point break, family round-robin, repeatability), Ruff clean; no I/O, dependencies, or dataset/task identifiers | Preserve |
| T2 | M2 | OpenRCA Metric/Dependency adapters through Signal Core | verified | Private taxonomy/raw-deviation Top-N (C4) and dependency threshold/sort (C5) deleted; segment onset, bounded strength, family-balanced selection, node/service/instance hierarchy verified; fixtures gained baseline-window rows; focused 37/37, full benchmarks 96/96, Ruff clean | Preserve |
| T3 | M2 | Production Prometheus range adapter | verified | `query_range` with ~240-point step, optional empty vs failure separation, instant fallback with explicit `onset_unavailable`; additive enum (7) and synced `metric_names` cap (F19); counter→rate templates; focused 51/51, providers/domain/tools 126/126, diagnosis/rca/api/services 682/682, Ruff clean; emptied `backend/diagnosis/__init__.py` eager re-exports (zero consumers) to break the providers↔diagnosis import cycle | Start M3/T4 |
| M2 | Review | Cold review both adapters share one semantics core | verified | OpenRCA and Prometheus no longer own taxonomy, segment, or ranking logic; old API/config contracts unchanged; instant payload preserved as additive fallback | Preserve |
| T4 | M3 | Segment-aware Attribution and thin Projector | verified | `_segment_aware_clusters` added with `_time_clusters` fallback for historical no-segment evidence; Analyzer db rule uses canonical `db_p95` only; Projector rewritten thin v2 (reference validation, scored-field dedup preserving authoritative order, Top-N, explicit fallback, 6-key safe audit); runner/evaluator synced; focused 138/138, Ruff clean | Start M3 review |
| M3 | Review | Cold review C1–C5 deletion and attribution boundary | verified | Zero remnants of `_metric_signal_type`, raw-deviation Top-N, candidate counts, or second ranking in production; Ground Truth reads confined to evaluator/prepare; historical fallback and invalid-reference drop covered by fresh tests | Start M4/T5 |
| T5 | M4 | Lab signals, schema V2 and answer-leak protection | verified | Lab exports bounded memory/drops/restarts metrics with generic 503 failures and `activated_at`; seed covers the full 60m baseline window (240s step) for the range adapter; three new generic-label alerts; schema V2 exactly seven scenarios with component/reason/occurred_at/onset fields, V1 readable, new scenarios bound onset ≤60s and forbid onset fallback; runner reads authoritative attribution via coordination-review; privacy scan covers Event/Evidence/Review; focused 81/81, Ruff clean, Compose config valid; README updated without changelog | Start M4 review |
| M4 | Review | Cold review production Gate boundary | verified | Fault control absent from Tool surface and default image (no gate env in Dockerfile, 404 when disabled); DiagOps evidence mounts stay `:ro`; both artifact schemas `extra=forbid`; V1/V2 dispatch and answer-leak scan covered by fresh tests; zero scenario tokens in Gate configs | Start M4.5/CL1 |
| CL1 | M4.5 | Remove W3/W4/W7 implementation-shape debt | verified | Seven repository `getattr` fallbacks replaced with direct calls (both formal repositories implement all methods; `_repo_call` deleted); full backend suite 1554/1554 with zero broken doubles; `test_runtime_workbench.py` rewritten to five executable node-driven projection/selection/redaction behavior tests; `test_frontend_smoke.py` trimmed to executable selector/grouping/redaction tests plus safety scans (no-automatic-action claims, no secret tokens); README and `current.md` limited to current operations, safety boundaries, active routing, latest Gate, and the frozen V8.2 artifact index (V2–V9 chronicle remains in historical specs/plans and Git history); focused 134/134, Ruff clean, frontend build passed | Start CL2 |
| CL2 | M4.5 | Record W5/W6 retain/retire disposition | verified | User confirmed on 2026-07-31 after a written risk/trade-off analysis: both `retained intentionally` — W5 remains the sole Provider/model certification producer backing `certification_status`; W6 remains the historical ReAct/LLM read path required by the historical-records contract; disposition recorded in the plan; nothing deleted | Start M4.5 review |
| M4.5 | Review | Cold review cleanup totals and contract integrity | verified | Net cleanup: CL0 −257 lines (109 production), CL1 README −61, current.md −188, frontend tests −312 source-shape lines replaced by +89 executable lines, W7 fallback −40; no public or persisted contract removed — API response models, DB schema, and repository interfaces unchanged; CL2 disposition leaves capability intact | Start M5/T6 |
| T6 | M5 | Key-free full regression | verified | Ruff clean; pytest 1538/1538; frontend production build passed; Runtime acceptance 14/14 with privacy scan passed (artifact `output/runtime-acceptance/runtime-20260731T011951755740Z-c5e8e1af/result.json`); `git diff --check` clean; zero diff in pyproject/uv.lock/migrations/schema/package.json — no new dependency, migration, public API, or write Tool | Start T7 |
| T7 | M5 | Source checkpoint | verified | User explicitly authorized the Git commit on 2026-07-31. Source identity commit `34e83097d353e6da261b32e3e92c08cc6264b7e5` on branch `agent/v10-shared-signal-semantics` (clean tree); prior HEAD `568da973`; official evaluator commit `c1bd4af7f635171a1c31cdd567c07d698dff6abc`; frozen six-case safe-index SHA-256 `b249e2f6b0b0dbd3b9f30aa71ef3302ff2c48a05cc50b914fb3dcbbc8800ad4b`; milestone diff review found no blocking finding | Start T8 |
| T8 | M5 | Production seven-scenario Gate | verified | First real run on source identity `b666d271` FAILED and was stopped per plan (artifact `output/production-acceptance/run-20260731T053859716Z/result.json`, SHA-256 `e3c61f180418aa85e4afd9905500c39284d119cfbc05622a8a1f28a826bf4496`): three new scenarios had correct cause/component but reason `traffic spike`. Root causes were in-contract implementation defects (seeded 0.05/s qps baseline let the 24 driven requests spawn a competing `traffic_spike` attribution; `_privacy_scan` mis-flagged canonical `signal_type: network_corruption`). Fixed in `6a7df40` (seed growth 24/240s; scenario tokens scanned only at the Event entry). Re-run PASSED 7/7 on source identity `6a7df40`: deployment `0.70`, dependency `0.70` (reason `dependency timeout`), traffic `0.60`, healthy UNKNOWN `0.20`, memory_pressure/network_corruption/process_failure all hit cause+component+reason with onset error 29.7–32.5s ≤60s; Evidence references 100%, read-only violations 0, privacy scan passed, total 105.2s <900s. Artifact `output/production-acceptance/run-20260731T111932207Z/result.json`, SHA-256 `2c3a543a3816978ee9c16d335ecf145f97eb26ffb5441e244b652a7fb258e2a9` | Start T9 |
| T9 | M5 | Frozen six-case targeted Gate | blocked | Real deterministic run `run-20260731T123348473710Z` on source identity `6a7df40` (frozen safe-index hash verified `b249e2f6…ad4b`): 6/6 completed, Evidence 100%, projection error/fallback 0, read-only 0, but official targeted Gate FAILED — scores 0/0/0/0/0.5/0.5 (2/6 positive < 3/6; task_1 time score 0). Official report SHA-256 `366d64d4619f82d423e889cf0ac4bce31d7d49098c442dcdb62d241a185cb109`, summary SHA-256 `d0b1fff74706c6e98c6262933e8866db5f420419a7b7acdf75d9874435a48080`. Structural diagnosis from run evidence (no Ground Truth used in any fix): quasi-zero metric series (baseline 0, MAD 0) yield zero-baseline-clamped strength-10 single-point segments at the window start (e.g. DSKBps 3.0 vs 0.0, change_percent 3e11), which outrank the true `network_latency` segments that ARE detected with correct onsets (09:16/09:18 in Bank:16); ~22 sub-second `latency` trace signals also cluster at window start. Root pattern: trivial-absolute-magnitude segments are not suppressed, so attribution ranks clamp-inflated noise above real faults | User accepted the outcome on 2026-07-31 after Amendment A1 was withdrawn post cold review; archived in `docs/superpowers/openrca-v10-6-case-failure-analysis.md`; no T10 |

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
