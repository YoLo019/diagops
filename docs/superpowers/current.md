# Current DiagOps Iteration

This file is the mutable routing and status entry point for the current version. It selects the active iteration and links to its authoritative documents; it does not define product requirements.

It does not override an approved spec or plan, current code contracts, or the long-term product goal and production safety boundary in `AGENT.md`.

Updated: 2026-08-10

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

Version: V11

Main goal: make adaptive, evidence-seeking Multi-Agent reasoning the diagnostic
authority for a general-purpose SRE workflow while preserving the existing
read-only safety, durable runtime, historical records, and benchmark-neutral
provider contracts.

Iteration status: `blocked`

Spec status: `approved`

Plan status: `approved`

Implementation status: `blocked`

Completion commit: `none`（M5 尚未完成正式 SS30/TT90）

M4 approved baseline: `7539c7fd8b707fc54cf2ed75a7d9fcba13d7618c`
on `codex/v11-m5`; M4 independent review approved before this worktree was
created. No merge or push was performed.

M2 baseline commit: `c2245ac` (`chore(v11): checkpoint M2 T4-T6`), committed
in the isolated M3 worktree; dirty `main` was not modified.

Verification evidence: `M1/T2–T3 third-round review-fix RED→GREEN evidence is green; RR-H1 deadline/budget 覆盖 6 项 + RR-M1 domain 契约 9 项补齐，RR-M2/RR-L2 先 RED 后 GREEN；第三轮独立复审 approve_with_followups 的 6 项发现当日全部关闭（RR-L1 INTAKE 激活原子化、RR-L3 公开事务内读取接口，均先 RED 后 GREEN）；T2 gate 209 passed，T3 gate 429 passed/3 skipped/1 warning，scoped Ruff 与 diff-check clean；M1 已合并 main 336067c`

Verification evidence (M2, 2026-08-08): `M2/T4–T6 独立复审 approve_with_followups 且无未关闭 blocking/high：评审独立重跑 T4 336 passed + 离线验收 rows=80 failed=0、T5 17 passed + Docker gate blocked（daemon 不可用，按 plan 记录）、T6 106 passed、全量 1927 passed/3 skipped、Ruff clean、runtime acceptance exit=0；M2R-1（high，§7.8 取消/no-late-commit 证据）当日补 4 条 slow-endpoint 测试经针对性复审 closed；T5 digest 环境注入裁定等价安全机制、schema 断言 6→7 确认为 M1 遗留（a40942a 复现 2 failed）；M2 基线已以 `c2245ac` 单独提交；M2R-2/3 在 M3 T7 中关闭，M2R-4 跟踪。`

Verification evidence (M3 T7–T8 and fifth-round review-fix5, 2026-08-09):
`fifth-round base e52e47984c4b460ed3b06dc4147d3d3b0532345b；M3_REVIEW_FIX5_SHA
为本次隔离提交；T7 exact gate 91 passed，T8 exact gate 207 passed；M2R-2/3
及运行时隔离 focused 84 passed/3 skipped，变更相关 runtime/review 102
passed/2 skipped；全量 pytest 1995 passed/3 skipped/1 warning；T7/T8 scoped
Ruff、uv run ruff check .、uv run ruff check backend tests 均 clean；唯一 H1
先以最小 RED 复现，再以既有 MODEL event callback GREEN 修复：持久化
actual_input_tokens 区分 provider actual 与 reservation billed/estimate，按
logical call/attempt/request 去重累计，AgentExecution 汇总整个 attempt，
summary/runtime counters 只累计成功 request actual usage，最终 result 不再
二次计数；失败 request estimate 仍保留在 attempt audit。实现保留 V10
legacy runtime 边界，V11 phase executor 仅消费 persisted V11 phases；未
merge/push，未开始 M4/M5，当前等待同一审查线程独立复审。`

Verification evidence (M4 second-round review-fix, 2026-08-09): `T9 exact gate
279 passed/1 warning; T10 exact gate 830 passed/3 skipped/1 warning; full
pytest 2045 passed/3 skipped/1 warning; offline tool acceptance rows=80
failed=0; runtime acceptance 14/14; uv run ruff check . and uv run ruff check
backend tests clean; frontend production build passed; git diff --check clean`.
The second-round H1 OpenRCA prediction regression now reuses the shared V11
public candidate projection for both prediction objects and CSV artifacts while
leaving V10 deterministic projection unchanged. The second-round M2 guard now
requires a non-empty active owner, matching latest Agent run summary, matching
review/run diagnostic and terminal statuses, and a legal Lead decision matrix;
API/report/graph/workbench paths fail closed and the frontend remains a second
defense. V10 historical behavior, the OpenAI-compatible adapter, and capability
gates remain intact. Independent M4 re-review remains the next gate.

Verification evidence (M4 third-round residual review-fix, 2026-08-09):
`M4 focused RED→GREEN 75 passed/1 warning; T9 exact gate 280 passed/1
warning; T10 exact gate 855 passed/3 skipped/1 warning; full pytest 2071
passed/3 skipped/1 warning; offline tool acceptance rows=80 failed=0; runtime
acceptance 14/14 scenarios; uv run ruff check . and uv run ruff check backend
tests clean; frontend production build passed; git diff --check clean`.
The status contract now freezes complete↔completed, partial↔partial, and
inconclusive↔completed; the shared public guard requires durable
RuntimeRunStatus.COMPLETED and binds report diagnosis/alternative IDs to the
Lead decision. Memory/SQLite reloads, API report/workbench/graph paths, direct
report projection, and frontend omitted/null/stale-owner regressions are
covered. The final post-commit runtime artifact is recorded in the handoff
with `git_dirty=false`; V10 legacy behavior, the OpenAI-compatible URL
adapter, capability gates, and local offline boundary remain unchanged.

Verification evidence (M4 fourth-round high-fix, 2026-08-10):
`targeted RED→GREEN real OpenRCA runner 8 passed; legacy-authority report
regressions 4 passed; M4 focused 124 passed/1 warning; T9 exact 288 passed/1
warning; T10 exact 868 passed/3 skipped/1 warning; full pytest 2084
passed/3 skipped/1 warning; offline acceptance rows=80 failed=0; runtime
acceptance 14/14 with privacy scan clean; uv run ruff check . and uv run ruff
check backend tests clean; frontend production build passed; git diff --check
clean.` H1 fixes the production V11 OpenRCA provider path by deriving the Skill
identity from the real agent tool manifest, while retaining Skill admission and
the nine-tool local registry; the real SQLite fixture runner now completes
without external telemetry. H2 calls the shared V11 publication guard before
prediction/CSV projection and fails closed for created, failed, cancelled, or
interrupted durable runs. H3 makes an active V11 owner reject every legacy-
authority report unless its status, owner, and Lead candidate bindings satisfy
the shared V11 contract; V10-only legacy reports remain unchanged. The final
post-commit runtime artifact and `git_dirty=false` binding are recorded in the
handoff.

Verification evidence (M5 engineering and stop gate, 2026-08-10): `T11/M0-L1/
M0-L2 RED→GREEN；frozen SingleInvestigatorAgent、真实 V11/SQLite runner、RCAEval
offline providers、四 SS30 配置、exact/统计 evaluator、pre-label manual audit、
controlled prediction/evaluator launch、policy/acceptance archive 均已实现；T11
focused 79 passed，benchmark Ruff clean；真实 26 GB runtime package 再校验为
manifest bb119fc9…、150 cases。正式 capability admission 在首个 OB30 model
call 前失败：本 worktree 无 passed model-capability artifact，且
DIAGOPS_AGENTS_API_KEY/BASE_URL/MODEL 均未配置。因此 SS30 未执行、SS/TT
labels 未由正式 evaluator 打开、TT90 未消耗；父任务为本 Codex task 设置的
Luna Max 只证明实现线程模型，不是 RCAEval 可调用 endpoint identity。独立 M5
复审未执行。`

Verification evidence (M5 repository gates, 2026-08-10): `uv run ruff check .
clean；full pytest 2121 passed/3 skipped/1 existing Starlette warning；frontend
production build passed；offline_tool_acceptance rows=80 failed=0（matrix SHA
3a49df20…）；runtime_acceptance 14/14、privacy scan passed（pre-commit artifact
SHA 2a980b25…）；git diff --check clean。production gate 的 Compose config
通过，但正式脚本 preflight 因 Docker Desktop Linux daemon unavailable 阻断，
未生成或伪造 7-scenario result。未发布 Tempo claim，故按 Plan 不运行
tempo_acceptance。独立 review 仍 pending。`

Blocker: `T12 capability admission：缺少 exact passed model-capability artifact、
可调用 endpoint/model 与 process-only credential；SS30/TT90 不得以任务线程模型
或 test double 替代。外部 owner：父任务/用户提供已认证本机端点后另行恢复。`

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
docs/superpowers/specs/2026-08-02-diagops-v11-adaptive-multi-agent-rca-design.md
docs/superpowers/plans/2026-08-02-diagops-v11-adaptive-multi-agent-rca-implementation-plan.md
```

## V11 Planning Dashboard

Overall progress: `V11 Spec/Plan approved；M0–M3 已按记录完成；M4 最终提交 7539c7f 已获独立 review approve；M5 T11 与 M0-L1/L2 工程实现/ focused 验证完成，T12 在 capability admission 前阻断，T13 未开始且 TT90 未消耗。`

Latest M4 verification: fourth-round H1–H3 were reproduced and fixed at the
real OpenRCA runner and shared V11 publication boundaries; M4 focused 124,
T9 288, T10 868, and full pytest 2084 all passed with the recorded skips and
warnings. Offline 80/80, runtime 14/14 privacy-clean, Ruff, frontend build,
and diff-check are green.

Latest M4 fifth-round verification: the shared V11 guard now requires
`InvestigationStatus.COMPLETED` in addition to the existing final review and
durable `RuntimeRunStatus.COMPLETED` matrix. Non-success V11 OpenRCA outcomes
produce empty prediction/CSV output with an explicit failure category; V10
legacy output remains unchanged. M4 focused 137, T9 289, T10 881, and full
pytest 2097 passed with only the recorded skips/warning.

Current phase: Full iteration / M5 engineering implemented，T12 capability stop
gate blocked；等待独立 M5 source/artifact review 与可调用认证端点

Next action: 父任务先创建独立 M5 review；若后续提供 exact passed capability
artifact、endpoint/model 与 process-only credential，再从 OB30/SS30 继续；保持
当前 worktree/branch，不 merge、push，不打开 TT90 labels。

| ID | Phase | Task | Status | Evidence or result | Next action or blocker |
| --- | --- | --- | --- | --- | --- |
| P1 | Context | Verify V10.1 failure chain, current authority boundary, contracts, and reusable runtime | verified | V10.1 OpenRCA delta 0; deterministic benchmark path did not exercise Agents; current fixed specialists, persisted deterministic roots, APIs, schema V6, checkpoints and read-only tools inspected | Preserve evidence |
| P2 | Requirements | Confirm general SRE scope, Agent authority, safety, blindness, cost, latency, and evaluation rules | verified | User confirmed bounded dynamic tools, 30–120s async, ≤3x intended-budget tokens, evidence refs ≥95%, P95 ≤120s, inconclusive wrong, and separate equal-token ablation | Preserve decisions |
| P3 | Design | Select adaptive evidence investigation over fixed specialists or majority debate | verified | User selected scheme B; independent first pass and Critic retained without majority voting | Preserve decision |
| P4 | Spec | Write and independently review the V11 contract | verified | Legacy-reuse audit traced runtime, persistence, entry, report/action, tools, API/UI, and benchmarks; L22–L30 closed; user approved the amended R1–R27 Spec on 2026-08-02 | Preserve approved contract |
| P5 | Plan | Map approved requirements to implementation and verification tasks | approved | Rewritten as T1–T13/M0–M5; independent review found H1–H3/M1–M2, all closed in focused re-review with no remaining blocking/high/medium issue; R1–R27 Plan-task mapping recorded in Spec; user approved and authorized execution on 2026-08-02 | M0/T1 complete; M1/T2–T3 evidence recorded |
| M1/T2 | Execute | Domain contracts, run ownership, and Schema V7 | implemented / verified | 初始审查问题 H8–H10 已补最小 RED 并 GREEN；第二轮 Medium 1–3 的 domain/memory/SQLite 直接写入回归先 RED 后 GREEN；`uv run pytest tests/domain tests/db/test_migrations.py tests/db/test_runtime_migrations.py tests/db/test_v5_agentic_rca_persistence.py -q` → `199 passed in 6.81s`; T2 Ruff clean；fresh、V3/V4/V5、legacy-V6/current-V6 manifest 一致且仅三列物理变更 | 独立迁移复审通过（第三轮 RR 全关闭） |
| M1/T3 | Execute | Versioned phase profiles, activation, gates, replay, and diff | implemented / verified | 初始审查问题 H1–H7 已补最小 RED 并 GREEN；第二轮 ownerless RESULT_VALIDATION、计划/任务和 Investigation 聚合写入回归先 RED 后 GREEN；`uv run pytest tests/runtime tests/api/test_runtime_runs_api.py -q` → `420 passed, 2 skipped, 1 warning in 117.53s`; `tests/runtime/test_review_fixes.py` → `27 passed, 1 skipped`; T3 Ruff clean；V10/V11 phase、owner、replay/diff、contract-integrity reload、service/API gate 回归通过 | 独立运行时复审通过（第三轮 RR 全关闭）；M2 基线 `c2245ac` 已提交，T4–T5 已按当前 Plan 记录完成 |
| M2/T4–T6 | Execute | 九工具离线事件包与 data-only skills、File/Tempo trace 平价、模型边界与能力认证 | implemented / verified | 独立复审重跑：T4 336 passed + 离线验收 rows=80 failed=0；T5 17 passed + Docker gate blocked（daemon 不可用）；T6 106→110 passed（M2R-1 补 4 条 slow-endpoint 测试）；全量 1927 passed/3 skipped、Ruff clean、runtime acceptance exit=0；复审 approve_with_followups，M2R-1 closed；M2 基线 `c2245ac` 已单独提交，M2R-2/3 在 M3 T7 中关闭，M2R-4 跟踪 | 保持基线提交，不修改 dirty main |
| M3/T7–T8 | Execute / Verify | Lead/Investigators/Critic/Lead V11 authority runtime | implemented / verifying | fifth-round base `e52e47984c4b460ed3b06dc4147d3d3b0532345b`; T7 91 passed、T8 207 passed；M2R/隔离 84 passed/3 skipped；变更相关 102 passed/2 skipped；全量 1995 passed/3 skipped/1 warning；两套全仓 Ruff clean；H1 usage/audit RED→GREEN | 等待同一审查线程复审；不 merge/push，不进入 M4/M5 |
| M4/T9–T10 | Execute / Verify | Reports/actions, human transitions, API/UI, OpenRCA compatibility, recovery/privacy/offline gates | implemented / verified | 最终提交 `7539c7fd8b707fc54cf2ed75a7d9fcba13d7618c` 已获独立 review approve；第五轮 High RED→GREEN 与 focused/full/offline/runtime/frontend/Ruff/diff-check 证据沿用 M4 handoff | M5 worktree 精确从该提交创建；不 merge/push |
| M5/T11–T13 | Execute / Verify | Frozen RCAEval control, sealed validation, one TT90 paired result | blocked at T12 capability admission | T11 + M0-L1/L2：79 focused passed、benchmark Ruff clean；真实 runtime checksum bb119fc9…/150；受控 prediction/evaluator、policy、manual audit 与 non-resumable archive 已覆盖。SS30/TT90 无模型结果，labels 未正式打开 | 缺 passed capability artifact/endpoint/credential；T13 未开始、TT90 未消耗；等待独立 M5 review |

M1 review-fix record (2026-08-06): High 1–6 and Medium 7–10 were reproduced
with focused regressions, fixed at shared profile/ownership/transaction/entry
boundaries, and rerun GREEN. The review-fix worktree contains no M2 or main
merge changes; final independent re-review is the remaining handoff.

M1 second-round review-fix record (2026-08-06): the re-review reproduced three
medium persistence bypasses. RESULT_VALIDATION without an explicit round can no
longer fall through as a legacy execution; memory and SQLite plan/task writes
revalidate forged models before assignment or delete/insert, and Investigation
aggregate saves revalidate nested V11 owner fields. Focused RED→GREEN evidence
and the complete T2/T3 Gates are recorded above; no approved requirement or
milestone scope changed, and independent re-review remains pending.

M1 third-round review-fix record (2026-08-07): the final independent review
concluded `approve_with_followups`. RR-H1 — T3 step 8 的 deadline/budget
monotonicity 测试此前被证据行隐含声称已测、实测零覆盖，现补 6 项
（`tests/runtime/test_v11_deadline_budget.py`）并修正该声称；RR-M1 — T4/T5
提前落地契约补 9 项 domain 测试（未改实现即通过，契约正确仅缺覆盖）；
RR-M2 — contract-integrity 修复同事务持久化校正契约，先 RED 后 GREEN；
RR-L2 — CANCELLING 遇 deadline 收敛 CANCELLED，先 RED 后 GREEN。
RR-L1/RR-L3 于当日跟进关闭：INTAKE 激活收敛进 commit 事务（spy RED→GREEN）、
`get_with_connection` 公开化；T2 209 passed、T3 429 passed/3 skipped，
Ruff 与 diff-check clean；M1 全量合并 main `336067c`；approved 契约与
milestone 范围不变。

M3 review-fix record (2026-08-08): independent review verdict was `BLOCKING`
against base `b17da397807bacf6e155afe71062ad1c6c4fd868`. Each B1/H1–H5, H6–H7,
M1, and M2 finding first received a minimal failing regression and then a
shared-boundary fix. B1 now carries an explicit investigation owner through
all persistence helpers; H1 freezes the ordered nine-tool manifest plus
skill/capability identity and limits; H2–H5 persist classified attempts and
atomically enforce model/tool budgets and absolute deadlines, with transport
retry reusing one logical tool reservation; H6 terminalizes required-actor or
persistence failures as failed without diagnostic fallback; H7 validates
same-run usable evidence, exact assessment coverage, inconclusive/partial
contracts, and final actor coverage without mutating Agent-authored fields; M1
scans all nested result text mechanically; M2 updates one durable Critic or
reconciliation execution audit on both success and failure. The final focused
and full gates are recorded above and in Plan §6. T4/T5 remain implemented and
verified as task rows; only the host-dependent live Docker execution remains
explicitly recorded as unavailable. This review-fix is committed only on the
isolated `codex/v11-m3` branch; no merge, push, M4, or M5 action was taken, and
the same-thread independent re-review is pending.

M3 second-round review-fix2 record (2026-08-09): independent review verdict was
`BLOCKING` against `629bc34e823ff9f5ff163dca5f3924a161baafd3`. The review-fix2
kept the existing V11 contract and repaired only the reported boundaries. B1/H1
now validate a server-owned canonical nested execution contract and digest at
admission, clone, bind, dispatch, retry, and resume; H2 disables SDK/provider
implicit retry so the persisted coordinator is the only retry owner; H3 applies
durable input/output reservations to every SDK request; H6 stops phase execution
after required-actor failure and clears diagnostic projection; H3 concurrency
uses one shared bounded gate for Investigator tasks; H7 checks persisted
round-two task IDs and assessment linkage exactly; M2 records parse failure on
the latest retry attempt. The minimal RED→GREEN tests and final gate counts are
recorded in Plan §6 and Spec §15. The single `M3_REVIEW_FIX2_SHA` commit remains
isolated on `codex/v11-m3`; no merge, push, M4, or M5 action was taken, and the
same-thread independent re-review is pending.

M3 third-round review-fix3 record (2026-08-09): independent review verdict was
`BLOCKING` against `b8c08d8a410877ef021cde69be0ec2faac98fb28`. Four production
reproductions were independently confirmed. Minimal RED regressions failed as
expected: B1 durable reservation helper collection failed, while B2/H1/H2
produced 3 failing tests. GREEN fixes reuse RuntimeEvent/RuntimeWriter durable
model lifecycle events, pin the official client and admission endpoint identity,
terminalize required round-two Investigator failures before `completed_rounds=2`,
and normalize legal Lead `INCONCLUSIVE` projections (including accepted Critic
assessment IDs) before mechanical validation.
Added regressions cover crash-window resume release exactly once, transport retry
reuse and double-settlement idempotence, concurrent token reservations, SQLite
contract reload under ambient endpoint pollution, round-two partial valid work,
and persistence/reload of inconclusive results. T7 exact `86 passed`, T8 exact
`202 passed`, M2R/isolated focused `84 passed/3 skipped`, full `1990 passed/3
skipped/1 warning`, and full Ruff are green. `M2_BASE_SHA=c2245ac` remains the
separate M2 snapshot; the fix is isolated to `codex/v11-m3` with symbolic
`M3_REVIEW_FIX3_SHA` pending the single commit and same-thread re-review. No
merge, push, M4, or M5 action was taken.

M3 fourth-round review-fix4 record (2026-08-09): independent review verdict
`BLOCKING` was issued against base
`9a7bf4a43efd313e5dcb528361dfbfbfec1c37f0`; all other fourth-round findings
were already closed. The remaining multi-request SDK retry finding first
received a production-path RED: the second request's transport retry replayed
request one with its settled reservation and raised
`V11RuntimeContractError("model reservation was already settled")` before the
retry provider request. GREEN reuses the existing durable MODEL event stream:
`request_index` is persisted in the safe payload and rehydrated into an ordered
per-logical-call request history. A completed request gets a fresh deterministic
`replay-N` reservation; an active failed request keeps its original reservation
identity, including when a later retry allocation must be deferred and restored.
The production regression
`test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request`
proves four provider calls, a new request-one reservation, failed request-two
reuse, settled usage, and zero active reservations. The recovery regression
`test_v11_sdk_replay_cursor_rehydrates_and_repeated_replay_is_idempotent`
proves durable cursor rehydration, released-reservation non-reuse, deterministic
replay IDs, and idempotent repeated replay. Final T7 `88 passed`, T8 `204
passed`, M2R/isolated `84 passed/3 skipped`, changed/runtime `102 passed/2
skipped`, full `1992 passed/3 skipped/1 warning`, both full Ruff commands clean,
and diff-check clean are recorded in Plan §6 and Spec §15 with the single
`M3_REVIEW_FIX4_SHA` commit; no merge, push, M4, or M5 action was taken.

M3 fifth-round review-fix5 record (2026-08-09): independent review verdict
`CHANGES_REQUIRED` was issued against base
`e52e47984c4b460ed3b06dc4147d3d3b0532345b`; only H1 remained and
Blocking/Medium/Low were zero. The production RED used the real
`_call_model → Agents SDK Runner → RetryCoordinator` path: four provider calls
with request one success, request two transport failure, then both requests
successful on the outer retry. The previous counters recorded only the final
attempt's `40/10` instead of the three successful requests' `60/15`, and the
first failed attempt audit omitted request one's usage.
The GREEN fix reuses the existing per-request MODEL event callback. It adds
mechanical `actual_input_tokens` beside the existing reservation
`input_tokens`/`input_estimate`, deduplicates terminal events by reservation and
attempt, accumulates all successful request actual usage into runtime counters,
and writes per-attempt AgentExecution totals (successful actual usage plus the
existing failed-request estimate). The final RunResult is no longer added a
second time. Regressions are
`test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request`
(`4 calls`, summary `60/15`, attempt-1/2 audit `91/5` and `40/10` for this
prompt), `test_v11_single_sdk_request_usage_is_not_counted_twice`,
`test_v11_all_failed_sdk_retry_records_estimate_without_summary_usage`, and
`test_v11_sdk_retry_resume_usage_is_idempotent`. Final T7 `91 passed`, T8 `207
passed`, M2R/isolated `84 passed/3 skipped`, changed/runtime `102 passed/2
skipped`, full `1995 passed/3 skipped/1 warning`, both full Ruff commands clean,
and diff-check clean are recorded in Plan §6 and Spec §15 with the single
`M3_REVIEW_FIX5_SHA` commit; no merge, push, M4, or M5 action was taken.

M4 first-round review-fix record (2026-08-09): independent review verdict
`CHANGES_REQUIRED` was issued against the initial M4 implementation. All ten
findings were reproduced before production changes and closed in the shared
transaction, entry, projection, evidence, transition, status, ownership,
summary, and report boundaries. B1 now keeps report/action construction inside
`BusinessMutation` and commits it with the phase/checkpoint/lease transaction
for both memory and SQLite. B2 makes configured product event/manual/runtime
create/rerun paths V11 and rejects V10 on an active V11 projection while
preserving historical legacy behavior and linked legacy-to-V11 reruns. H3/H4
add the shared V11 public text projection and shared usable-evidence validator
to API/report/UI/graph/OpenRCA paths. M5–M9 enforce inconclusive candidate
clearing, immutable human verification refs, legal review/run/Lead status
combinations, exact V11 artifact owners, and safe Lead/Critic summaries. L10
renders actual finding/task/round/evidence data and the public Lead decision in
Markdown. The focused regressions live in
`tests/runtime/test_v11_m4_audit_fixes.py` plus the report/API/OpenRCA suites.
Final T9 `276 passed/1 warning`, T10 `812 passed/3 skipped/1 warning`, full
`2025 passed/3 skipped/1 warning`, offline `80/80`, runtime acceptance exit 0,
both full Ruff commands, frontend production build, and diff-check are green.
V10 historical compatibility, the OpenAI-compatible URL adapter, capability
gates, and local-only/offline constraints remain unchanged. This review-fix is
local to `codex/v11-m4`; no merge or push was performed and independent
re-review remains pending.

M4 second-round review-fix record (2026-08-09): the independent re-review
reproduced two additional issues. H1 — V11 OpenRCA prediction objects and CSV
rows bypassed the shared public candidate projection; the RED artifact regression
now passes through `public_v11_candidate`, while the V10 deterministic projector
retains its original output. M2 — the public guard accepted contradictory
review/run status and missing active/latest Agent ownership; the RED memory,
SQLite reload, API, report, and frontend regressions now share
`validate_v11_final_status` plus the durable-owner guard and fail closed. T9
`279 passed/1 warning`, T10 `830 passed/3 skipped/1 warning`, full pytest
`2045 passed/3 skipped/1 warning`, offline `80/80`, runtime acceptance `14/14`,
Ruff, frontend build, and diff-check are green. The local branch remains
`codex/v11-m4`; no merge or push was performed.

## Archived V10.1 Planning Dashboard

Overall progress: `M0/M1/M2/M3 all reviewed; T9 paired Gate FAIL（delta 0.000）归档、R13 blocked；M3 review approve（F27 informational 已记录）；不声称准确率提升`

Current phase: Full iteration / Verify（blocked：R13 未达成）

Next action: 向用户请求最终 docs 提交授权（F23：current.md/plan/spec 三份治理文件）。不声称准确率提升；production Gate 7/7 仅为模拟 lab 七场景契约 Gate。

| ID | Phase | Task | Status | Evidence or result | Next action or blocker |
| --- | --- | --- | --- | --- | --- |
| P1 | Context | Verify V10 failure chain and current contracts | verified | V10 frozen six-case artifact/hash rechecked; current OpenRCA adapter, Signal Core, Attribution and focused tests inspected on 2026-08-01 | Preserve evidence |
| P2 | Requirements | Confirm defect scope, unbiased holdout policy and V10 compatibility gates | verified | User confirmed the recommended Requirements Brief on 2026-08-01 | Preserve decision |
| P3 | Design | Select the minimum semantic repair and blind paired evaluation approach | verified | User selected scheme A on 2026-08-01 | Preserve decision |
| P4 | Spec | Write and independently cold-review the V10.1 contract | verified | F1–F9 resolved；focused re-review closed blocking F4/F5 and high F6；user approved on 2026-08-01 | Preserve artifact |
| P5 | Plan | Map R1–R14 to ordered implementation and verification gates | verified | Independent cold review found no blocking；F10–F12 resolved；user approved on 2026-08-01；M1 review F17/F18 forced T6 scope amendment, user re-approved the amended plan on 2026-08-01 | Execute M2 |
| M0 | Execute | T1–T2 blind tooling, custodian holdout, frozen V10 baseline | verified | T1: 63 focused passed + review A approve_with_followups; T2: commit `4a4b20a`, holdout 40-case SHA `aac4b89bc9b3b248bfd0fdb8b5c783805655c30d3aeff13e81eac6f23f7f9936` (overlap 0, per-partition 10), V10 baseline `run-20260801T071800886742Z` 40/40 unscored on source `7b2602457561875ae8091e4d8eeea1bcddc2d34d`; review B approve_with_followups (F13–F16 low, absorbed) | Done |
| M1 | Execute | T3–T5 signal semantics repair | verified | T3/T4 complete (51+118 focused passed); T5 selection done, Attribution consumer-layer gap found by M1 cold review: blocking F17 (domain validator onset re-sort) + medium F18 (dependency payload), fixes approved into T6 scope and closed there; 618 merged tests passed | Done |
| M2 | Execute | T6–T8 compatibility, regression, production Gate | verified | T6: F17/F18 closed, 247+111 focused passed; T7: Ruff clean, 1634 full passed, frontend build ok, runtime acceptance 14/14 + privacy; M2 review A approve_with_followups (F19 fixed with red-green evidence, F20 recorded); T8: candidate commit `0a7d81c`, production Gate 7/7 (`run-20260801T124143735Z`, evidence 100%, read-only 0, privacy passed, new-scenario onsets 30.2–32.5s, total 107s); first attempt failed and root-caused: F21 CRLF env (fixed without source change) + F22 memory_pressure timing flake (artifact archived at `run-20260801T122813729Z`, standalone repro passed); M2 review B approve_with_followups (F23–F25 low, recorded) | Done |
| M3 | Execute/Verify | T9 一次性揭盲 paired Gate + T10 最终对账 | blocked（R13） | T9: candidate `run-20260801T130348358205Z` 40/40 on clean `0a7d81c0813e163111e1c475d88d843112336aa7`（tooling `4a4b20a`、baseline source `7b2602457561875ae8091e4d8eeea1bcddc2d34d`、holdout SHA `aac4b89bc9b3b248bfd0fdb8b5c783805655c30d3aeff13e81eac6f23f7f9936`、official evaluator `c1bd4af7f635171a1c31cdd567c07d698dff6abc`）；paired-gate FAIL：official partial 0.025 vs 0.025（delta `0.000` < `0.05`）、compatible strict/component/reason/time 双侧相同且 reason 0、candidate 单 run gate 4 断言失败、projection fallbacks 两侧各 8、Evidence 100%、read-only 0；identity/checksum/query+answer multiset 全部通过；comparison artifact `D:\data\OpenRCA\comparisons-v10.1\comparison.json`（SHA-256 `b4b4bd3db4cceac01df8b1c76ac9e07b5dd16bf9a67853f0bedbb7b481418730`）；T10: Ruff clean、1636 passed、frontend build ok、runtime acceptance 14/14 + privacy（`runtime-20260801T152709422174Z-8e11162c`）、`git diff --check` clean；ledger F13/F15/F16 closed、F26 归档、F23 open；M3 review `approve`（40/40 预测逐行不同但命中计数持平，排除配对乌龙；Ruff/pytest 1636 重跑一致；RM1–RM3 informational 记入 F27） | 向用户请求最终 docs 提交授权（F23）；不声称准确率提升（7/7 仅为模拟 lab Gate 契约） |

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
