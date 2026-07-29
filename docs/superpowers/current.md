# Current DiagOps Iteration

This file is the mutable routing and status entry point for the current version. It selects the active iteration and links to its authoritative documents; it does not define product requirements.

It does not override an approved spec or plan, current code contracts, or the long-term product goal and production safety boundary in `AGENT.md`.

Updated: 2026-07-29

## Implemented Baseline

V9 durable and observable agent runtime is the implemented baseline. It retains
the V8.2 bounded Specialist-owned evidence tools and reproducible OpenRCA
fixed-versus-adaptive benchmark contract while adding durable Runs, Attempts,
lifecycle events, checkpoints, recovery, Replay, and supported Run Diff.

Implemented baseline commit: `bcbcafc4569276b9c9617f042d8d6c73539081ac`

The current platform uses SQLite by default, retains an in-memory repository for tests and explicit configuration, and includes an optional default-off OpenAI Agents SDK runtime with deterministic RCA fallback.

The frozen V8.2 OpenRCA 40-case result remains registered below as the
independent comparison baseline for V9.

## Active Iteration

Version: V9

Main goal: add a durable and observable domain runtime with isolated concurrent Investigation sessions, explicit recovery, safe events, replay, and Run Diff.

Iteration status: `complete`

Spec status: `approved`

Plan status: `approved`

Implementation status: `complete`

Completion commit: `bcbcafc4569276b9c9617f042d8d6c73539081ac`

Verification evidence: `C:\Users\林佳威\.codex\worktrees\b551\sre-agent\output\runtime-acceptance\runtime-20260729T052807237954Z-790b99ae\result.json`, the key-free gate results below, the registered V8.2 OpenRCA baseline, and the V9 OpenRCA/Replay/Diff evidence in the dashboard

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
docs/superpowers/specs/2026-07-17-diagops-v9-durable-observable-runtime-design.md
docs/superpowers/plans/2026-07-17-diagops-v9-durable-observable-runtime-implementation-plan.md
```

## Live Execution Dashboard

Overall progress: `14/14 verified`

Current phase: V9 complete

Next action: preserve the registered artifacts and use completion commit
`bcbcafc4569276b9c9617f042d8d6c73539081ac` as the V9 release identity. The
approved same-Investigation Run Diff contract remains unchanged.

Status values: `pending | in_progress | blocked | verified`. Keep at most one
row `in_progress`.

| ID | Phase | Task | Status | Evidence or result | Next action or blocker |
| --- | --- | --- | --- | --- | --- |
| A1 | Baseline | Register frozen V8.2 identity and artifacts | verified | See `Frozen V8.2 OpenRCA Baseline` | Preserve artifacts read-only |
| A2 | V9 verification | Run key-free Ruff, pytest, frontend build, and Runtime acceptance gates | verified | Completion candidate `bcbcafc`: Ruff passed; pytest `1400/1400`; frontend production build passed with 81 modules; Runtime acceptance passed all 14 scenarios and privacy scan. Final artifact `C:\Users\林佳威\.codex\worktrees\b551\sre-agent\output\runtime-acceptance\runtime-20260729T052807237954Z-790b99ae\result.json`, SHA-256 `F712B7E9B592DBE2B8DBFF1794B4517F5D3F051C39D172C3FD217F7643D4EAA4`. | Preserve final evidence |
| A3 | V9 verification | Verify schema V5-to-V6 migration on a copy | verified | See `V9 Verification Evidence` | Rerun only if persistence code changes |
| B1 | V9 OpenRCA | Prepare the same frozen 40-case selection | verified | See `V9 OpenRCA Prepared Input` | Preserve inputs read-only |
| B2 | V9 OpenRCA | Run batch 01/05 | verified | Audited merge `run-merged-20260726T092848877633Z` at `D:\data\OpenRCA\results-v9-087b404-batch01-merged`: paired recovery replaced both Bank:51 rows; 8 Fixed and 8 Adaptive predictions are non-empty and evidence-valid; 16/16 Runtime IDs are unique and completed; tokens match Runtime at Fixed `87732/27142` and Adaptive `126352/38108`; source and artifact hashes passed; manifest SHA-256 `27f90b370fc97dbddda3279cdc17f768406ba0a54e8bfde320ac11a14bbb6c05`. Original and recovery artifacts remain preserved. | Preserve all three source/merged artifacts read-only |
| B3 | V9 OpenRCA | Run batch 02/05 | verified | Authoritative merge `run-merged-20260726T130524779541Z` at `D:\data\OpenRCA\results-v9-35df5de-batch02-merged-audited` preserves all eight Fixed rows and seven original Adaptive rows, replacing only Adaptive Telecom:5 from recovery `run-20260726T124413016863Z`. Independent audit found 8 non-empty evidence-valid rows per strategy and 16 unique completed Runtime Runs; Model lifecycles closed at Fixed `72=72+0` and Adaptive `93=93+0`; no Phase or invalid-reference failure; exact tokens Fixed `89846/22158`, Adaptive `122893/36760`; tool calls `32/113`; duplicate rejections `0/19`; manifest SHA-256 `4f079880e9bc503fa6e236991d2fe3ad618595bd53ce89a36065ae05b84a8350`. Rejected draft `run-merged-20260726T130058592316Z` is retained only for audit and named in the authoritative manifest. | Preserve original, recovery, rejected draft, and authoritative merge read-only |
| B4 | V9 OpenRCA | Run batch 03/05 | verified | Authoritative merge `run-merged-20260727T112522188715Z` at `D:\data\OpenRCA\results-v9-557638a-batch03-merged-audited` preserves 14 original rows and replaces only Fixed Telecom:9 and Adaptive Telecom:40 from their accepted single-strategy recoveries. Independent audit found 8 non-empty evidence-valid rows per strategy and 16 unique completed Runtime Runs; Model lifecycles closed at Fixed `75=75+0` and Adaptive `104=104+0`; no Phase or invalid-reference failure; exact tokens Fixed `90466/26001`, Adaptive `136770/39673`; tool calls `32/117`; duplicate rejections `0/12`; all source hashes, artifact hashes, row provenance, partition files, and case order passed. Manifest SHA-256 is `04a22eaab60c5f67591f90c6c067532d911c0dfe691759c7c8d5a86e64aed714`. Original and both recovery artifacts remain preserved. | Preserve original, recoveries, and authoritative merge read-only |
| B5 | V9 OpenRCA | Run batch 04/05 | verified | Authoritative merge `run-merged-20260728T103039795266Z` at `D:\data\OpenRCA\results-v9-9018f99-batch04-merged-audited` preserves ten accepted original rows and selects six row-level recovery sources. Per explicit approval, Adaptive cloudbed-2:62 retains its empty frozen 180-second timeout result instead of being rerun. Independent audit verified frozen case order, partition projections, exact source-row equality and checksums, 16 unique completed Runtime Runs and Attempts, Fixed `8/8` and Adaptive `7/8` non-empty predictions, 100% Evidence reference validity, zero Phase or invalid-reference failures, and closed Model lifecycles at Fixed `75=75+0` and Adaptive `95=93+2`. Exact totals: Fixed tokens `92189/23774`, tool calls `32`, duplicate rejections `0`; Adaptive tokens `120275/31765`, tool calls `118`, duplicate rejections `18`. Only the retained cloudbed-2:62 row is listed in `failed_cases`; the other Adaptive Model failure belongs to a non-empty original row that recovered within the same Run. Manifest SHA-256 `81f39170a65b4ea5ae1af382e03d73f1611a79b31454b1c695a9450383e3c7b0`; merge script SHA-256 `7C63123BC1C51DB9B0F2870D795934AAE3530B7C0F26623AA956328418EDCE4A`. | Preserve all source and merged artifacts read-only |
| B6 | V9 OpenRCA | Run batch 05/05 | verified | Per explicit approval, authoritative merge `run-merged-20260728T130642194648Z` at `D:\data\OpenRCA\results-v9-9018f99-batch05-merged-audited` preserves every original Batch05 row and retains the three frozen-protocol empty results: Fixed cloudbed-2:70, Adaptive cloudbed-2:48, and Adaptive cloudbed-2:70. Independent audit verified byte-identical source CSVs, frozen case order, all artifact checksums, exactly 16 unique completed Runtime Runs and Attempts, Fixed `7/8` and Adaptive `6/8` non-empty predictions, 100% Evidence reference validity, zero invalid references and read-only violations, and the exact three approved failure classifications. Manifest SHA-256 `A927F0B74CEC8DB5AAC7A12B5AFB20F37DB838738D85C8678A966831039D4F2F`; merge script SHA-256 `1905A694F7C1BC3210DA11A4488C51326132688D3568BC6B88CC3B64CC4A28F9`. The original run, Runtime DB, transcript, merge script, and merged artifact remain preserved. | Preserve source and authoritative merge read-only |
| B7 | V9 OpenRCA | Merge batches and run compatible and official evaluators | verified | Authoritative 40-case merge `run-merged-20260728T131350272103Z` at `D:\data\OpenRCA\results-v9-final-9018f99-merged-audited` concatenates the five exact audited batch sources in frozen order. Independent audit verified 40 unique rows per strategy, 80 unique completed Runtime Runs and Attempts, Fixed `39/40` and Adaptive `37/40` non-empty predictions, 100% Evidence reference validity, zero invalid references and read-only violations, four retained formal failures, exact source-row equality, four 10-row official query subsets, and every artifact checksum. Both strategies completed `40/40`; compatible and Microsoft official reports each contain 80 rows and score Fixed/Adaptive strict and partial `0`, equal to the frozen V8.2 baseline. The official evaluator was sparsely fetched at Microsoft OpenRCA commit `c1bd4af7f635171a1c31cdd567c07d698dff6abc`; its V8.2 validation reproduced all 80 frozen rows semantically and the same zero scores. Final hashes: manifest `66AB4BD47C80027AE0037990EAE17DBDF89833EEF4A97593EED4A897ACF33213`, summary `52B43BD7196C76DBFFDCC65647D49C5E2FDAA60332E493AD8E5B4A80D9986F87`, Fixed `0A10B5329ED4577DF0DD92B0C8DCAD720A5137C65ED9EAB472971EFC4BACFA1A`, Adaptive `F3C597DA723D808B83C23B5A246397964AD6E1C8593B01B7D776B1139FFA1DCE`, compatible report `DF38537C7AC9512BE6C77D93CBBB88D298A2B98DE782E6EB5606D8E3AED10621`, official report `695894AA795FF66A25145684C33B09D60E59A45D7B40B72935F82B73AD5D158C`. | Preserve all source, merged, query, evaluator, and validation artifacts read-only |
| C1 | Runtime audit | Replay one real Fixed and one real Adaptive Run | verified | Historical Fixed Replay passed. Real Adaptive retry02 source Run `4558eee0-5fdc-4491-a900-26642a68d87a` completed with Agent `11/11`, Model `14/14`, and Tool `6/6`; all six started Tools have ordered terminal events, including two interrupted calls closed by the phase boundary. Its formal benchmark gate retained an empty result because three Provider connections failed and Deployment recollection produced one invalid semantic reference, but no invalid reference was persisted. Event, checkpoint, and business Replay validators all returned no errors. Full Replay on the database copy is valid with zero external calls and completed Replay Run `36fa2380-d6f6-489f-9080-3537211c2ca7`; report `a74fb5a4-93b0-4c4d-a11a-afbe13c7bd30`, errors `[]`. Source DB SHA-256 immediately after the run was `FE388E715FF719747B3F83ABB78B1A6FAC1A61A7E02366A1EB5AEDC93A93A303`; opening it through the application SQLite stack checkpointed physical pages, changing the current file hash to `3C2A54CFBE8EB4CD6892B99E6BCE661E47EB54905B07F2574AFCA9243DFE2DF1`. A true `mode=ro` audit confirms it still contains exactly one Live Run, one Attempt, 90 events, eight checkpoints, and no Replay Run or report. Audit DB `D:\data\OpenRCA\replay-v9-bcbcafc-bank51-retry02-audit\runtime-audit.db`, SHA-256 `C4D706DBBCAE1C89727FFCAD6232DB9C46C1BCC016A9FBA37AE2F2F38BE502C1`. | Preserve current source and audit copy; record the physical source-hash change and Provider/model quality as residual risks |
| C2 | Runtime audit | Diff the paired Runs and confirm stable hashes twice | verified | Per explicit user decision on 2026-07-29, V9 retains the approved same-Investigation Diff contract. Supported source-to-Replay Diff is deterministic: Fixed hash repeated `D9D140A4A4244C3454279DC6F9C07C81BF7D8B6855DBA92583929E8D350B74BE`; Adaptive hash repeated `FDE0D27233D36A8C06ABECBED47B7F25DE3E15057D2B2E992A602A654D1F6B24`. Direct comparison of benchmark Fixed/Adaptive Runs correctly rejects distinct Investigations and is not a V9 requirement. Evidence `D:\data\OpenRCA\replay-diff-v9-final-9018f99-cloudbed2-52\result.json`, SHA-256 `5F59E3230259B625BF3D912E0C0D3D90EF93E7BF59264512F36870BC1226F504`. | Preserve evidence and contract |
| D1 | Release evidence | Compare V9 with V8.2 and record residual risks | verified | Both official evaluations score Fixed/Adaptive strict and partial `0`, so the benchmark does not prove higher diagnosis accuracy. V9 does prove stronger evidence integrity: Adaptive validity improves from `65.615%` with 32 invalid references to `100%` with zero invalid references; Fixed remains `100%`, and both versions have zero read-only violations. V9 additionally supplies 80 durable Runs/Attempts, ordered lifecycle events, checkpoints, recovery, valid zero-external-call Replay, and deterministic supported Diff. Costs/risks: final Runtime acceptance shows enabled p50 `528.141 ms`, `+45.237%` versus V8.2-compatible sync; V9 40-case average duration rises from `291733` to `336843 ms` Fixed and `473434` to `522245 ms` Adaptive; Provider connections, model semantic references, timeouts/empty results, and the SQLite physical-hash audit caveat remain. The formal 40-case artifact is commit `9018f99`; post-artifact lifecycle corrections at completion commit `bcbcafc` are covered by fresh regression/full tests, Runtime acceptance, and real Adaptive Tool Replay rather than a regenerated 40-case score run. | Preserve comparison and residual-risk record |
| D2 | Release decision | Record completion commit after explicit authorization | verified | User explicitly authorized V9 completion on 2026-07-29. Completion commit `bcbcafc4569276b9c9617f042d8d6c73539081ac` is a verified descendant of the V8.2 implementation baseline. Final Ruff, `1400/1400` pytest, frontend build, 14/14 Runtime acceptance, privacy scan, OpenRCA, Replay, and supported Diff gates are recorded above. | V9 complete; preserve artifacts |

Update this dashboard after every batch or verification gate. Put detailed
commands and acceptance criteria in the active plan; keep only status, evidence
paths, the next action, and blockers here.

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

## V9 OpenRCA Prepared Input

```text
V9 commit: 0e53c663f5acf2dc23a1a606514137dfbc61d1d9
Prepared root: D:\data\OpenRCA\prepared-v9-0e53c66
Case manifest hash: 434e36603328fb5f0ec58730bcde5696931d2267dad99aca448cbfd6b52e812a
Case manifest file SHA-256: 06DA1E47BD0D18592A256BF0700D50CC95C775020816FC316AF75757F695B2FD
Runtime index SHA-256: C438F8AB78C7B41016C632352D9AB63397AB86E9DA823A238DEC61604699AE0C
Batches: 5
Cases per batch: 8
```

The seven prepared files match the frozen V8.2 inputs byte-for-byte. Validation
found 40 unique cases, 10 cases in each partition, all 40 telemetry directories,
no forbidden ground-truth fields in Runtime or batch indexes, and preserved
case order across the five batches. Focused OpenRCA prepare and runner tests
passed: `13 passed in 21.97s`.

## V9 OpenRCA Batch 01 Preflight

The benchmark CLI previously ignored `DIAGOPS_AGENTS_TIMEOUT_SECONDS`: model
execution, durable Runtime metadata, and `run-manifest.json` remained at 60
seconds. Commit `71c9c026522da7d430998513757242ec326cda5a` fixed timeout
propagation. The first formal attempt then exposed two independent artifact
identity defects. Commit `d4a129e7b986c558d381b6ff763c7390b3d5f8ef`
namespaces deterministic OpenRCA Evidence IDs per Investigation; commit
`7c57ba0318171a294560c1f2c89a57a1e91fb1a3` preserves the Runtime Run ID when
case execution fails after Run creation. The final HEAD passes 1385 tests and
Ruff. A key-free CLI smoke run for timeout propagation recorded `180` in the
manifest and `180.0` in SQLite Runtime state:

```text
C:\temp\diagops-v9-timeout-smoke-20260724
```

The existing untracked `output/` directory is not part of the commits. Formal
V9 OpenRCA artifacts must record commit
`087b404eb551e5882abe4a0b96ffcd17651edcde`.

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

Final candidate gates were refreshed at commit
`bcbcafc4569276b9c9617f042d8d6c73539081ac` on 2026-07-29:

```text
uv run ruff check .
exit 0; All checks passed.

uv run pytest -q
exit 0; 1400 passed, 1 pre-existing Starlette warning in 215.64s.

npm.cmd --prefix frontend run build
exit 0; TypeScript and Vite production build succeeded; 81 modules transformed,
Vite build time 1.73s. Existing TanStack React Query "use client" warnings
remained non-fatal.

uv run python -m backend.services.runtime_acceptance
exit 0; 14/14 required scenarios passed; privacy scan passed with zero markers.
Artifact:
C:\Users\林佳威\.codex\worktrees\b551\sre-agent\output\runtime-acceptance\runtime-20260729T052807237954Z-790b99ae\result.json
Artifact SHA-256:
F712B7E9B592DBE2B8DBFF1794B4517F5D3F051C39D172C3FD217F7643D4EAA4
```

The final acceptance artifact records `git_dirty=true` because the existing
untracked `output/` tree is intentionally excluded from commits. It records
candidate diff SHA-256
`1d1c833648a8ccd67cb94b3ba57c9cad6e60e592f194d9d973f46a526a56f924`.
Its raw measurements include V8.2-compatible sync p50 `363.640 ms`,
Runtime-disabled p50 `376.099 ms`, Runtime-enabled p50 `528.141 ms`,
Runtime-enabled versus sync `+45.237%`, recovery `11.031 ms`, SQLite growth
`64716.8 bytes/Run`, and OTel enabled overhead `+3772.695%`.

A V8.2-compatible schema V5 SQLite source containing one historical
Investigation was created under the system temporary directory, copied, and
only the copy was migrated. The copy reached schema V6,
`PRAGMA foreign_key_check` returned no rows, the historical Investigation
remained readable, no synthetic Runtime rows were created, and
`runtime_available=false`. The evidence copy is under
`C:\temp\diagops-v9-migration-d8a44f1731b344cf88e3f96cf7eeaf91`.
No user database was modified; `data/diagops.db` was absent.

## Open Release Gates And Risks

The real dataset, frozen V8.2 result, official query tree, evaluator output,
Replay evidence, and supported Diff evidence are registered above. Provider
credentials remain caller-supplied and must not be persisted in the workspace,
commands, logs, or artifacts.

V9 is complete at
`bcbcafc4569276b9c9617f042d8d6c73539081ac`. The release record retains these
limits:

- official OpenRCA strict and partial accuracy remain `0` for V8.2 and V9;
- Provider connection instability, semantic-reference failures, timeouts, and
  empty results remain observable quality risks;
- Runtime-enabled p50 is `+45.237%` versus V8.2-compatible sync in the final
  acceptance run;
- the formal 40-case scoring artifact is commit `9018f99`; the final candidate
  `bcbcafc` is covered by fresh full tests, Runtime acceptance, and real
  Adaptive Tool Replay for the post-artifact lifecycle corrections;
- the retry02 source SQLite physical hash changed during application-stack
  validation open, while a true read-only audit confirmed no logical Replay
  rows were added.

All 14 dashboard items are verified.
