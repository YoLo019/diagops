# Current DiagOps Iteration

This file is the mutable routing and status entry point for the current version. It selects the active iteration and links to its authoritative documents; it does not define product requirements.

It does not override an approved spec or plan, current code contracts, or the long-term product goal and production safety boundary in `AGENT.md`.

Updated: 2026-08-15

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

Spec status: `approved`（第八轮全量复审 approve_with_followups，两条 Medium 修复后独立复审 approve；此前顶部字段停留在第六轮 re-review 等待期，本次对账校正）

Plan status: `approved`（同 Spec status 校正依据）

Implementation status: `blocked`（M5 T12 capability admission 的三个外部前置——可调用 endpoint/model、process-only credential、绑定干净 HEAD `584b4b0` 的 passed capability artifact——现已具备；正式预算消耗授权与修复链复审决定仍待用户）

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
initial T11 focused 79 passed，benchmark Ruff clean；真实 26 GB runtime package 再校验为
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

Verification evidence (M5 review-fix, 2026-08-11): the first and second
independent M5 reviews were reproduced with RED regressions. The custodian pair
ledger is shared across output directories, requires an exact frozen
prediction-set hash and exact pre-label audit/manual artifact identities, uses a
one-shot reservation token, and converts expired prediction/pre-label/label-open
leases to pair-level FAILED_NON_RESUMABLE before explicit reauthorization.
Prediction admission binds clean source revision, adapter/SDK/environment
identity, capability contracts, and tested parallelism. Formal SS30/TT90
cardinality is exact (30/90), current scorer dependency closure is checked at
policy freeze and final acceptance, placement-only runtime state is unknown,
Single uses one model context, and V11 model turns use a durable run-level
remaining budget carried through checkpoints and retries. Engineering tests are
green; formal predictions, attempts, and label opens remain zero. A fresh
independent re-review is still pending, so M5 remains blocked and no accuracy
claim is made.

Verification evidence (M5 review-fix repository gates, 2026-08-11): `uv run
pytest -q` → `2141 passed, 3 skipped, 1 warning`; `uv run ruff check .` clean;
frontend production build passed; offline tool acceptance `rows=80 failed=0`;
runtime acceptance produced a passed 14-scenario artifact (privacy scan
passed) at `output/runtime-acceptance/runtime-20260811T155221149309Z-6cd0f67e/result.json`
(SHA-256 `45165e1453004e8f2a6afbbdf6a41b99eb850bb23b3ff1502770d1db47da6c4d`,
clean engineering run bound to code commit `b591e40df5569cd53c40e9984b9d056f2ceb57f5`,
`git_dirty=false`);
offline matrix is `output/offline_tool_acceptance/matrix.json` (SHA-256
`d85e162d0cba644c09397b38c6756eb41bbf8b58c61120c98a72f0617d674540`);
`git diff --check` clean. Production acceptance was attempted but the
required lab/Docker environment variables were absent, so no production
scenario artifact was generated. Tempo acceptance was not run because no
Tempo claim is being published.

Verification evidence (M5 third-round review-fix, 2026-08-12): base was the
second-round clean code `977a040a`; code fix commit is
`094a20f2cd22066a00c32adead51b0e7b8489a63` on `codex/v11-m5`. RED→GREEN
regressions close canonical custodian-root/manifest ledger binding (including
alternate-root rejection), an external-cwd trusted prediction worker with
explicit source/Git root, permanent label reveal epoch and one-time
reauthorization identity, frozen-bundle candidate/evidence audit reconstruction,
sealed persisted RuntimeRun turn-state rejection, completion-failure pair
invalidation, and required lease-token APIs. Targeted M5 regression gate is
`132 passed`; full pytest is `2151 passed, 3 skipped, 1 warning`; Ruff and
`git diff --check` are clean; frontend production build passed; offline tool
acceptance is `80/80`; runtime acceptance is `14/14` with privacy passed at
`output/runtime-acceptance/runtime-20260811T180307749157Z-fcf30999/result.json`
(SHA-256
`37ff2eece9e2feb14d00d4622d13acc00f0ef21d64a6e064633bf82d7f4094ff`,
`git_commit=094a20f2cd22066a00c32adead51b0e7b8489a63`, `git_dirty=false`);
offline matrix is `output/offline_tool_acceptance/matrix.json` (SHA-256
`8c5e150a9e07923528a5c1d6a0f7016739ffaaeb07b7b21f10faf16458659c9d`).
Formal SS30/TT90 predictions, paired attempts, and label opens remain `0`.
Production acceptance stopped before scenario creation because
`DIAGOPS_PRODUCTION_SCENARIO` and the required production/endpoint environment
are absent; no Tempo claim was published or run. M5 remains blocked and awaits
fresh capability admission plus independent re-review; no accuracy claim is
made.

Verification evidence (M5 fourth-round review-fix, 2026-08-12): base was
`2e8b276ed053e6d5c87f8590954705f3f4459acd`; implementation head is
`da9dbb8496835bc1a2cca60a4d1b9764ad9bc822`. RED→GREEN regressions now close
the frozen prediction-set ledger-identity transition, exact side
output/checksum/bundle binding, authorized reauthorization lineage, and
completion/evaluation SQLite-lock failure convergence. The ledger uses one
bounded write-lock backoff policy and explicit connection close; a completion
write failure atomically attempts pair invalidation before returning the
original error. Prediction-set root and every parent path reject symlink/
junction aliases before the first freeze write, and the root checksum is
created exclusively to prevent concurrent overwrite. Packaged source identity
uses a build-time manifest/code digest without copying `.git`; runtime and
capability admission reject manifest tampering or stale source identity.
The focused M5 regression group is `164 passed, 1 warning`; full pytest is
`2163 passed, 3 skipped, 1 warning`; full Ruff, frontend build, and
`git diff --check` are clean. Offline acceptance ran in a temporary directory
(`80/80`, SHA-256
`654ec4cba7c43d28ace4918af6fe547de1a1efb30ea433039f944fb5d13d9eb1`) and
runtime acceptance ran in a temporary directory (`14/14`, privacy passed,
SHA-256
`ebe8a975eaf950fc46b320665c4fbce5c5ac9d2b8f1c6e8d0a148142870ef1af`);
both temporary trees were removed. The acceptance run was engineering-only
while the worktree was dirty, so its `git_dirty=true` is not a formal artifact.
Docker production preflight remains host-blocked because the Linux daemon is
unavailable. `D:\data\RCAEval\v11-m5` is absent, the three endpoint/credential
environment variables are absent, and formal predictions, paired attempts,
and label opens remain `0`. M5 remains blocked and awaits capability admission
and a fresh independent re-review; no accuracy or completion claim is made.

Verification evidence (M5 SS30 epoch-0 failure and contract fix chain, 2026-08-15):
用户授权正式预算后，SS30 第 1 侧（single_intended, B=24000）epoch 0 在干净 HEAD
`06901ab` 启动：30 例全部跑完但 **6 completed / 24 failed**（failure_category 全为
unknown），`validate_prediction_bundle` 全量 completed 语义 fail-closed 拒收 bundle，
pair FAILED_NON_RESUMABLE（labels 未开封，reauthorization 通道可用，partition 保留）。
实际消耗 68 次模型调用、452,970 input + 43,168 output ≈ 0.5M tokens（授权 ~5M）。
对失败 DB 做零模型调用的离线复算完成根因归因：RCAEval runtime 包 deploy provider
未配置 → 每例存在 status=skipped 证据，模型按 spec §7.2 语义让 GAP finding 引用它
（H1 已修终态校验 finding 层），但下游各层未同步该契约——报告层 usable-only 无 GAP
豁免（15/20）、candidate_finding_reference（5）、candidate_evidence_reference（2）、
非 GAP signal finding 的 scope_entity_mismatch（2，复合 affected_entity，契约硬拒
正确）、investigator draft 违约杀 run（3）、预算门（1）。用户决策：系统性修复 +
保持 bundle 全量 completed fail-closed 语义不变。修复链 M1–M7：M1 报告层 GAP 引用走
新增 `validate_committed_evidence`（committed 任意状态 + 同 run ownership），非 GAP 仍
usable-only，`ReportReferenceError(ValueError)` 类型化；M2 GAP 跳过 scope 一致性校验；
M3 investigator draft 级拒绝（违约 draft 丢弃 + FAILED AgentExecution 审计，整批全灭
才杀 run）；M4 `_admit_candidates` 准入校验（finding 引用 ⊆ 本批+已持久化、evidence ⊆
usable，违约 candidate 丢弃+审计不改写），prompt rule 扩写 + PROMPT_VERSION 升
`v11-rcaeval-v2`；M5 coordinator 逃逸契约异常映射 CONTRACT_INTEGRITY + runner
failure_category 优先取 persisted；M6 correction 预算耗尽前置 terminal；M7 离线回放复算 +
spec §7.2/§8.2/§9.2 文本同步（GAP 可引用 committed 任意状态、GAP 豁免 status/scope、
输出单元级拒绝语义）。回放断言全绿：24 failed run 报告层转绿（其中 3 例
candidate_evidence_reference 的报告层持续拒绝为两层一致的正确行为）、10 例违约持久化
数据仍以原 code 失败（无过度豁免）、6 completed 保持全绿。独立复审 **APPROVE**（无
blocking/high；1 medium——legacy authority 分支不再校验 GAP 引用存在性——当日修复并补
RED 结构可信测试；2 low 记录不改码：预算门逃逸错误归入 contract_integrity 可辩护、
candidate 审计 round 来源统一建议）。最终门禁：`uv run pytest -q` → **2293 passed,
4 skipped, 1 warning**；`uv run ruff check .` 与 `git diff --check` clean。正式计数：
SS30 paired attempts=1（failed_non_resumable），label opens 仍为 `0`。

Verification evidence (M5 SS30 epoch-1 failure and Single-path fix chain, 2026-08-15):
epoch-1 启动后用户在 ~12/30 例时贴回失败日志并决策立即停止（省 ~0.3M tokens）；
停止时 snapshot 5 completed / 9 failed / 1 running（contract_integrity ×5 +
unknown ×4）。生产 DB 取证 + 测试 harness 复现确认四条根因：
(1) Single 对照路径（`SingleInvestigatorAgent.investigator_round_1`）未获得 M3/M4
语义——裸 tuple 推导任一违约 draft 即逃逸杀 run，candidates 无准入校验；
(2) Single 特有完整性要求（entity/mechanism/supporting evidence 非空）只在终态
校验把关，不完整 candidate 必死（contract_integrity ×5）；
(3) **更深的共享运行时缺陷（影响所有 V11 路径）**：phase 内 `_mark_terminal_failure`
后 phase 输出 status="failed"，但 `PhaseCommit` 契约只接受 completed/skipped →
ValueError 逃逸 → InMemory staging 的暂存业务变更（失败审计、清空 review）永不落库、
run 归类 unknown（epoch-0 全部 24 例失败也是同一机制）；
(4) 报告层三处引用完整性检查抛裸 ValueError → 逃逸归类 unknown（d6dce2c5 实证：
模型编造 finding ID 的 candidate 在 report_generation 死亡）。
修复链：F1/F2 Single 路径 per-draft 拒绝+审计、`_admit_candidates` 准入、Single 特有
"candidate_incomplete" 准入丢弃（刻意不进共享 `_admit_candidates`，Multi 语义不变）；
F3 `PhaseCommit` 接受 failed、两 store 后端映射 PHASE_FAILED、coordinator 去重后归类
OUTPUT_VALIDATION（失败审计随事务原子落库）；F4 三处改 ReportReferenceError（逃逸归
contract_integrity）；F5 Single prompt 规则扩写 + PROMPT_VERSION `v11-rcaeval-v3`。
TDD：4 个 harness 测试先 RED（复现 draft 杀 run、bad-ref candidate 杀 run、不完整
candidate 杀 run、批次全灭审计丢失）后 GREEN；内存后端分叉测试经 stash 验证 RED→
GREEN。独立复审 **APPROVE**（无 blocking/high；1 medium——InMemoryRuntimeStore 未同步
failed 映射——当日修复+测试；1 low 经实证不成立：Single result_validation 最终
`_update_summary` 在 INCONCLUSIVE 写入后重算，终态摘要 COMPLETED 正确；1 informational
记录：rank 违约仍留终态校验，与 Multi 一致）。最终门禁：`uv run pytest -q` →
**2299 passed, 4 skipped, 1 warning**；ruff 与 `git diff --check` clean。正式计数：
SS30 paired attempts=2（两 epoch 均 failed_non_resumable），label opens 仍为 `0`。

Verification evidence (M5 SS30 epoch-2 failure and adapter robustness fix chain, 2026-08-15):
epoch-2 在干净 HEAD `9ec895f` 启动（capability 重认证 passed，artifact_hash
`f53d3bb6e30479fba184dc0b91d5be14e198946c219baf6ed9a474be951fcadf`）；启动脚本先以
`reconcile_pending_failure()` 把 epoch-1 遗留的过期 lease 收敛为 failed_non_resumable，
再授权 epoch 1→2。30 例跑完：**11 completed / 19 failed**——全部以 output_validation
分类干净落库、零 unknown，前两条修复链的契约类失败**零复发**；唯一 1 条 candidate
违约走新审计路径 `candidate draft rejected: candidate_incomplete` 且 run 继续。
bundle 全量 completed 语义再次 fail-closed 拒收 → pair failed_non_resumable（epoch 2，
labels 仍未开封）。零模型调用取证（console 19 行 + DB 42 条失败 execution 交叉核对）：
(1) **10 例** ModelBehaviorError `Invalid JSON: trailing characters`——网关/模型在合法
JSON 后追加垃圾（尾部多 `}`/双份拼接，与 08-14 网关退化特征一致），而
`retryable_failure_category` 只放行 rate-limit/connection，单次畸形响应直接杀 run；
(2) **7 例**预算耗尽（5 exhausted + 2 exceeded）——Single 单上下文累积，失败 attempt
input 已达 15.5k–19.5k tokens，24k 预算只够约 1.5 轮（OB30 smoke 通过仅因该例 1 轮
收敛共 14902 tokens）；(3) **3 例** APIConnectionError，重试 1 次后仍失败；
(4) **1 例** enum 漂移（action='stop'）——证实该网关 json_schema 为透传而非强约束
解码，长输出时无约束兜底。用户决策：A+B——代码鲁棒性修复 + single_intended 预算
24k→48k。修复链：`_salvage_json_text`/`_salvage_response_json`（raw_decode 截到第一个
完整 JSON 值；function_call arguments 恒清洗，message 文本仅在 output_schema 非空时
清洗，纯文本输出绝不触碰；strict envelope 先 salvage 外层再拆封、payload 内层兜底；
截断 JSON/前导垃圾原样返回）+ `retryable_failure_category` 纳入
ModelBehaviorError→INVALID_OUTPUT（RetryCoordinator max_retries=1 不变，失败 attempt
只计 input estimate、重试前 reservation released，无双重计费）。TDD：13 例 RED→GREEN
（salvage 参数化 8 例、native/strict/kwargs 三个 get_response 层级、重试分类与一次性
重试语义）。独立复审 **approve_with_followups**（无 blocking/high）：medium——生产实际
走的全 kwargs 调用分支补测试关闭；medium（ModelBehaviorError 含确定性误用，重试浪费
被 max_retries=1+预算闸限住、失败类别已持久化可观测）与 low（strict envelope 结构性
失败仍无重试；salvage 原地改写后网关原始字节不可见的取证取舍；stream_response 无
salvage 已加防呆注释）记录入 T13 对账。门禁：focused 50 passed；全量
**2313 passed, 4 skipped, 1 warning**；ruff 与 `git diff --check` clean。正式计数：SS30 paired
attempts=3（三 epoch 均 failed_non_resumable），label opens 仍为 `0`。

Verification evidence (M5 SS30 epoch-3 failure and draft/projection contract fix chain, 2026-08-15):
epoch-3 在干净 HEAD `4e975ff`（重认证 passed，artifact_hash
`66aee122204f727c54b268b66c05e8eab690cf737260cdfbab4271d8afec12c5`）以 48k 预算启动；
用户在 7 例失败后决策立即止损（省剩余案例预算）。console 取证三条签名：
(1) `AgentFinding` 构造期 ValidationError ×2——模型产出 blocking=true 但无 gaps 的
draft，per-draft 拒绝只捕获 V11RuntimeContractError，pydantic ValidationError 漏网杀 run；
(2) `RuntimeEvent safe_payload.normalized_inputs.keywords[4] must be a bounded identifier`
×1——模型给 read_logs 传带空格的自由文本 keyword（工具 query 契约合法），持久化投影层
把所有列表项按标识符（无空格、≤160）校验，ValidationError 在工具调用内炸成 UserError；
(3) APIConnectionError ×4——网关抖动，重试 1 次后仍败，代码层已尽其责。
修复链：F1 `_finding_from_draft` 构造期 ValidationError 包装为
V11RuntimeContractError（保留固定 validator 消息详情以区分模型违约与未来调用方 bug；
Multi/Single 两条 per-draft 拒绝路径单点覆盖），`_draft_rejection_code` 新增
`invalid_finding_contract` 审计码；F2 持久化投影与 query 契约逐字段同向对齐
（投影接受集 ⊇ query 合法集）——keywords/levels/metric_names 列表项按有界非空字符串、
instance/version/target/name 可空标量按有界字符串、read_service_catalog 允许键补
`name`；标识符类（entity_ids/service/operation/trace_id）与枚举字段不变。
独立复审 **approve_with_followups**：H1（同类残余字段 levels/metric_names/instance/
target/version/name 是 epoch-4 可预见炸点）当日按逐字段对齐关闭，补 5 正 4 负
RED→GREEN；M1（包装吞掉 validator 消息、调用方 bug 与模型违约不可区分）以保留固定
消息详情关闭；L1（`_draft_rejection_code` 子串匹配脆弱，可改结构化 code）入 T13。
TDD：9 例 RED→GREEN（domain 违约 draft 契约化、Single 路径拒绝+审计、投影对齐正负例）。
门禁：focused 916 passed/2 skipped；全量 **2328 passed, 4 skipped, 1 warning**；ruff 与
`git diff --check` clean。正式计数：SS30 paired attempts=4，label opens 仍为 `0`。

Blocker: `T12 第四修复链（构造期 ValidationError 包装 + 投影/query 契约对齐）已完成并获
独立复审 approve_with_followups（无 blocking/high 遗留，见上）。恢复正式评测还差：
(1) 提交本修复链；(2) 对最终干净 HEAD 重新认证 capability；(3) 用户签发第四个
reauthorization token（pair 现为 predicting+过期 lease，reconcile 后 failed_non_resumable
epoch 3、label_ever_opened=0）后以 48k 预算跑 SS30 single_intended epoch 4，再续剩余 3 侧。`

Verification evidence (M5 fifth-round review-fix, 2026-08-12): base was
`75f964495d6e6f391ebd8eef4c2170ba982d53ea`; code commit is
`0bb9c4e2b0dd37068380bc07d32aeb9d7838a901` on `codex/v11-m5`. RED→GREEN
regressions now cover the trusted evaluator's single stable label-file read
(hash/identity is checked before result construction and replacement races
leave no evaluator artifact), an idempotent freeze-intent/materialize/bind
recovery path, append-only sealed ledger state across all transitions, durable
writer-lock failure reconciliation, Windows reparse/junction rejection,
relocated wheel/package manifests without Git or cwd dependence, exact formal
topology limits, candidate onset semantics, and strict checksum parsing. The
RCAEval focused group is `123 passed`; full pytest is `2176 passed, 3 skipped,
1 warning`; full Ruff, frontend build, `git diff --check`, offline `80/80`, and
runtime `14/14` with privacy scan are green. A wheel installed into a clean
target and invoked from an external cwd passed the trusted worker help smoke.
The package manifest is
`backend/services/diagops-source-manifest.json` (package identity hash
`56cca55981f24aa03f02b21d20eb931649bf65ecd5ef0ba31bf9ad2f51722448`, file
SHA-256
`841af38222554f8652291ef5e5df0cd430c050ad16ef85803d1aa8e587a56085`, 135
files). These are engineering gates only: formal SS30/TT90 predictions,
paired attempts, and label opens remain `0`; `D:\data\RCAEval\v11-m5` is
absent; capability artifact, endpoint, model, and process-only credentials are
absent. M5 remains blocked at T12 and awaits independent sixth-round review;
no accuracy or completion claim is made.

Verification evidence (M5 fifth-round follow-up hardening, 2026-08-12): code
commits `071315d4be5b599e638c7b71e3d1caac0bad5086` and
`effa4cfdf44380b3d7ce9ce006acb89d51a39412` tighten the reviewed contract
without consuming formal data. Custodian side locators now reject
relative/non-canonical spellings before persistence; direct invalidation under
a held SQLite writer lock records the same durable failure intent used by
completion paths; the packaged prediction worker and launcher reject relative
and reparse source/entrypoint roots before resolving them. The focused
ledger/isolation/evaluator/runner gate is `91 passed`; the added lock-intent
and launcher-locator regressions are green; full pytest is `2179 passed, 3 skipped, 1 warning`; Ruff, frontend
build, offline `80/80`, runtime `14/14` with privacy, wheel external-cwd
smoke, and `git diff --check` are green. The installed package manifest is
`backend/services/diagops-source-manifest.json` with identity hash
`44a5cc1ce2acda0ff9f974aec010c94a9574aec4bc102a0a7d255ad7bb708da4`, file
SHA-256 `0fbe8c5b2b9888d22719069bd6708e616f5da395005ab9b0c7132585fa2d7739`,
and 135 files. Formal predictions, paired attempts, label opens, and label
consumption remain `0`; M5 is still blocked at T12 pending capability
admission and the next independent review, with no accuracy claim.

Verification evidence (M5 sixth-round review-fix, 2026-08-12): base was
`cf5e4295e96e2fb8067d719e088afdbad0fc9dc2`; the sixth independent review
returned `changes_required`/`blocked` (1 blocking, 5 high, 3 medium). All nine
findings were reproduced RED and closed GREEN: the custodian ledger seal is
authenticated by an external append-only HMAC-chained anchor keyed by a
one-time random seal key (raw SQLite tamper, forged appended seals, and reveal
clearing fail closed at every write boundary; anchor/key absence refuses seal
rebuild); the evaluator child re-verifies the frozen prediction root's
canonical SHA256SUMS bytes/hash and every bundle byte before its first side
effect, parses bundles only from verified bytes, and passes the expected
custodian label manifest hash through the ledger fence (mismatch/replacement
leave zero artifact); one guarded write boundary persists verifiable failure
intent on lock/exception exhaustion and startup reconcile converges intents
plus expired leases; custodian/locator paths reject reparse/junction/symlink,
drive-alias, UNC, and nonexistent spellings before resolve; side/root checksum
manifests share one strict canonical parser; `topology` is a required
mechanically validated V11 contract key and `validate_configuration_set`
freezes per-configuration topology limits; and `setup.py` `build_py`
regenerates the wheel package manifest from final `build_lib` content while
dependency-lock identity fails closed without `uv.lock`/`pyproject.toml`.
Repository gates on the final state: focused M5 group `379 passed, 1
skipped`; full pytest `2210 passed, 4 skipped, 1 warning` (the fourth skip is
the new POSIX-only custodian symlink regression; the other three are
pre-existing allowed skips); `uv run ruff check .` clean; frontend build
green; offline acceptance `rows=80 failed=0` in a temp dir; runtime acceptance
`14/14` with privacy scan passed; wheel external-cwd smoke passed (no Git, no
`.pth`, external cwd) with installed-file tamper and missing-manifest cases
refused; `git diff --check` clean. Docker Linux daemon preflight is
unavailable and remains an unmet milestone gate. These are engineering gates
only: formal SS30/TT90 predictions, paired attempts, and label opens remain
`0` (`D:\data\RCAEval\v11-m5` absent); capability artifact, endpoint/model,
and process-only credentials remain absent. M5 stays blocked at T12 awaiting
independent seventh-round review; no accuracy or completion claim is made.

Verification evidence (M5 seventh-round review-fix, 2026-08-12): base was
`6c238e3b0187ced00891ac7aa5e914c88b9a04c3`; the follow-up review returned
`changes_required` (1 medium, 1 low). Both findings were reproduced RED and
closed GREEN: all three recursive scans (`ledger.py` custodian entries,
`isolation.py` frozen prediction root and package checksum verification)
dropped `sorted(Path.rglob("*"))` materialization in favor of
iterate-and-reject, so planted junction loops (junctions are not symlinks and
rglob recurses into them; two junctions reproduced a >10s pre-fix hang in a
subprocess probe and a 60s timeout kill, exit 124) now fail closed
immediately with explicit elapsed-bound regressions; and the shared strict
checksum parser now explicitly rejects POSIX-rooted `/`-prefixed paths, which
Windows previously accepted (not absolute, no drive, round-trip stable).
Gates on the final state: focused M5 group `383 passed, 1 skipped`; full
pytest `2214 passed, 4 skipped, 1 warning`; `uv run ruff check .` clean;
`git diff --check` clean. Formal SS30/TT90 predictions, paired attempts, and
label opens remain `0`; capability/endpoint/credential and Docker daemon
gates are unchanged. M5 stays blocked at T12 awaiting independent re-review;
no accuracy or completion claim is made.

Simplification record (v11 redundancy cleanup, 2026-08-12): a trusted
read-only review enumerated redundant checks and dead code; each item was
implemented surgically in worktree branch `codex/v11-m5` with focused tests
after every group. Dispositions — implemented: R1 (dead `_pair_ledger_path`
and `_git_revision` in `__main__.py`), R2 (dead runner `_canonical_path`;
the ledger self-use copy stays), R3 (always-false checksum-parser clause
`relative_path.parts != tuple(relative_path.parts)`), R4 (manual bundle-hash
recompute after `_validate_frozen_bundle` in `evaluate_acceptance`), R5
(`_agent_manifest` re-validation already covered by
`_validate_execution_contract`; raw manifest extraction and the
`assert_agent_callable` loop retained), R6 (duplicate
max_investigators/max_rounds bounds in `_validate_execution_contract`;
max_turns/max_tool_calls_per_specialist lower bounds retained), R7
(`v11_projection` report identity/status and digest pre-checks duplicated by
`validate_v11_report_projection`/`validate_v11_execution_contract`), R8
(`_assert_persisted_contract` digest recompute implied by validator digest
consistency plus dict equality), R10 (evaluator `main()` pre-validation
duplicated by `evaluate_bundles`; CLI partition consistency check retained),
R11 (runner local reparse helpers converged onto
`backend.services.source_identity.reject_reparse_path` with
iterate-and-reject rglob loops that never recurse into junctions; evaluator's
local copy untouched per the scorer-closure import boundary), R12 (`run_case`
dual near-identical runtime construction merged into shared kwargs plus
conditional multi-only keys), R13 (partition cardinality and
configuration-topology rules converged into `models.py`
`EXPECTED_PARTITION_COUNTS`/`EXPECTED_CONFIGURATION_TOPOLOGY`; both trust
sides keep their own validation calls), R15(a) (`_within` implied by the
parent `_same_path` check), R15(b) (second `_reject_label_reparse` implied by
the dev/ino re-check), R17 (six canonical-JSON-SHA256 copies converged onto
`models.py:canonical_json_sha256` with `allow_nan=False`; scorer-closure
modules and outside-closure callers all import it, respecting the closure
import boundary; `test_rcaeval_isolation.py` import updated). Retained by
decision: R9 (ledger reserve double-hash) and R14 (freeze-loop second symlink
check) as cheap defense-in-depth, plus the review's twelve confirmed-keep
items (H3 anchor full replay, parent+child re-verification,
read_label_manifest_once double hash, evaluator local reparse copy, idempotent
freeze re-validation, dual-boundary configuration validation calls, two-layer
final_status validation, result_validation double invocation, `_pair_row_exists`
biased-True, prelabel audit full rebuild comparison, post-reserve heartbeat,
prepare `sorted(rglob)`). Deferred: R16 (prepare three-pass 26GB reads merge)
and R18 (freeze double seal-verification merge) as efficiency refactors, not
redundancy removals. Gates on the final state: full pytest `2214 passed,
4 skipped, 1 warning` (identical to the pre-cleanup baseline);
`uv run ruff check .` clean; `git diff --check` clean. No formal OB30/SS30/TT90
run was executed: formal predictions, paired attempts, label opens, and label
consumption remain `0`; all tests use `tmp_path`. M5 stays blocked at T12;
no accuracy or completion claim is made.

Verification evidence (M5 eighth-round full-version review and partial-contract
fix, 2026-08-12): an independent whole-version review of `58c6382..2b42b0f`
(M0–M5, 144 files) returned `approve_with_followups` (2 medium, 3 low) after
rerunning every engineering gate green: full pytest
`2214 passed, 4 skipped, 1 warning` (two runs), both Ruff commands, offline
`80/80`, runtime acceptance `14/14` privacy-clean on `git_dirty=false`,
frontend build, and diff-check; blindness spot-grep zero-hit; Docker/T5/T12
blocks re-confirmed as environmental. Medium-1 (R9, §9.2 partial sufficiency
bidirectional drift) and Medium-2 (R8, resume lost partial failure memory)
were fixed per the approved contract — candidate-level no-failed-check, ≥2
distinct supporting evidence items, two provider types when usable evidence
covers two, spec-external round-two linkage removed, insufficient partial
downgraded to `inconclusive` via the existing single tool-less Lead
correction, and `_restore_failure_memory` backfilling from the same persisted
failed/cancelled execution source. RED: 7 regressions failed pre-fix (5
validator, 2 runtime). GREEN: focused `60 passed`; full pytest
`2220 passed, 4 skipped, 1 warning`; both Ruff commands and diff-check clean.
An independent re-review of the uncommitted diff returned `approve` (one
informational low). The three lows are queued for T13 reconciliation:
leakage gate is structural input-plane isolation with a hardcoded zero
runtime counter, spec §15 ledger lag (closed by backfill in the same commit),
and a bare `KeyError` preflight in `production_acceptance.py`. Approved
R1–R27 contract unchanged; formal SS30/TT90 predictions, paired attempts, and
label opens remain `0`. On explicit user instruction `codex/v11-m5` was merged
to `main`; M5 stays blocked at T12 capability admission and no accuracy or
completion claim is made.

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

Overall progress: `V11 Spec/Plan approved；M0–M3 已按记录完成；M4 最终提交 7539c7f 已获独立 review approve；M5 T11 与 M0-L1/L2 工程实现/ focused 验证完成；第八轮全量复审 approve_with_followups，两条 Medium（§9.2 partial 契约、resume 失败记忆）已 RED→GREEN 关闭并获独立复审 approve，分支已按用户指示合并 main；T12 在 capability admission 前阻断，T13 未开始且 TT90 未消耗。`

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

Current phase: Full iteration / M5 T12 正式评测进行中：SS30 single_intended epoch 0
（6/30）、epoch 1（止损于 ~12/30）、epoch 2（11/30）、epoch 3（止损于 7 例失败）均
失败，四轮根因均已离线归因；第四轮修复链（构造期 ValidationError 契约化 + 投影/query
契约逐字段对齐）完成并获独立复审 approve_with_followups，全量 2328 passed、Ruff clean

Next action: 提交修复链 → 对最终干净 HEAD 重新认证 capability artifact → 用户签发第四个
reauthorization token 后以 48k 预算跑 SS30 single_intended epoch 4 → 续剩余 3 侧；T13
对账清单新增本轮记录项（_draft_rejection_code 子串匹配可改结构化 code）；不打开 TT90
labels，不做任何准确率声称。

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
| M5/T11–T13 | Execute / Verify | Frozen RCAEval control, sealed validation, one TT90 paired result | blocked at T12 capability admission | 第八轮全量复审（`58c6382..2b42b0f`）approve_with_followups：Medium-1（§9.2 partial 三条件候选级对齐、round-2 linkage 移除、不足降级 inconclusive）与 Medium-2（resume 失败记忆回填）RED 7 例→GREEN，focused 60 passed、full `2220 passed/4 skipped/1 warning`，独立复审 approve；三条 Low 入 T13 对账；SS30/TT90 无模型结果，labels 未正式打开 | 已按用户指示合并 main；缺 passed capability artifact/endpoint/credential；T13 未开始、TT90 未消耗 |

Verification evidence (M5 Single control live smoke fix chain, 2026-08-13/14):
用户在含 process-only credential 的本机 PowerShell 提供已认证 endpoint
（gpt-5.6-terra @ cctq.ai）后，OB30 Single 24k 诊断 smoke 逐层暴露并修复了
三层契约恢复级根因，全部按 Light 路径 RED→GREEN 执行：
(1) dispatch 对 4 个非 QueryWindow 工具硬编码 start_time/end_time 字段导致
AttributeError → `b80a418`（`_query_window` 统一三契约）；
(2) 持久化 `_TOOL_NORMALIZED_INPUT_KEYS` 白名单缺同 4 工具导致
safe_payload ValidationError → `f9ddd38`（加性扩展白名单 + 字段类型表）；
(3) 模型输出 inconclusive+ candidate_ids 被 Single 模型侧输出契约内的领域
validator 杀死（该 decision 按设计本就被 result_validation 丢弃）→
`584b4b0`（Single 专用 `SingleControlPlanningOutput`/`LeadDecisionDraft`
草稿契约 + runner 内确定性归一化 `_single_control_planning`；Multi 路径
`LeadPlanningOutput` 领域校验不变并有守卫测试）。辅助诊断加固：
`cf1b737`（异常诊断 head+tail 截断）、`1de1c30`（pydantic 明细提取）。
工程门禁：受影响套件 1036 passed/1 skipped；全量 `2268 passed, 4 skipped,
1 warning`；Ruff 与 `git diff --check` clean。
Live 验收（用户侧执行并回贴）：capability 认证在干净 HEAD `584b4b0` passed
（transport `native_json_schema`，artifact_hash
`cfa4944b99daa98258b766bf788777995e5f4e9019235da36efae6d29b9623c9`）；
OB30 Single 24k 验收 smoke run `3f3c130a-a871-4dce-afee-1dd33b278fbe`
（case re2-11caeecc5351ba99）：completed=true、run_status=completed、
model.completed=2、model.failed=0、tool.completed=2、tool.failed=1
（`query_related_alerts` 收到 `entity_ids=[]` 空范围后按契约显式 skipped，
独立工具照常完成，非缺陷）、read_only_violations=0、15294 tokens、36.5s；
结果库 `D:\data\RCAEval\v11-smoke\ob30-single-24k-production-schema-63c9843bf6c5413db4afce5928d20c52.db`。
这些是工程与单次 smoke 证据：正式 SS30/TT90 预测、paired attempts、
label opens 仍为 `0`。注意：本组修复触碰的是被评测的 Single control 边界
本身，且晚于第八轮全量复审；是否在消耗正式预算前补一次针对
`b80a418..584b4b0` 的独立复审，为 T12 恢复前的待决门禁。M5 仍 blocked。

独立复审 (2026-08-14，范围 `b80a418..584b4b0`)：**approve_with_followups**。
复审员独立核对调用链与持久化边界并实跑目标测试 `176 passed, 2 skipped`、
`ruff check backend tests` 全绿；确认 result_validation 强制收口属实、草稿
契约不泄漏到持久化/对外契约、Multi 领域契约未松绑、归一化边界全部
fail-closed、白名单与真实模型字段精确一致、诊断截断先脱敏后截断且无多字节
切裂、NaN/Infinity 纪律保持。无 blocking/high/medium。三条 Low 入 T13 对账：
(1) 草稿契约变更使旧 capability artifact schema hash 过期（预期后果，重跑
smoke 前重新认证即可，本轮已做到）；(2) `_single_control_planning`
inconclusive 分支丢弃非空 tasks 不留审计痕迹（可选加 warning 日志）；
(3) `_query_fingerprint` 对 `entity_ids/severities/statuses/states` 列表顺序
敏感，重复查询去重不生效（可选归一化；预算硬上限不受影响）。

Verification evidence (M5 pre-T12 live diagnosis chain continued, 2026-08-14):
OB30 Single 24k smoke 在 `45ccde1` 又暴露两层问题并完成定位/修复。
(1) 长生成传输失败：走本地代理时长输出调用 7/8 "Server disconnected"，
直连（`NO_PROXY="*"`）8/8 成功且 11.5k-14.2k 字符严格解析全绿；capability
execution-environment 指纹不含代理变量，直连不需重新认证。同窗口两次
"trailing characters"（col 3234/2997）也高度疑似代理破坏响应，直连后未复现。
捕获工具链记录：实例级 client 包装会被 SDK
`should_disable_provider_managed_retries` 的 `with_options(max_retries=0)`
客户端副本绕过，类级 `AsyncCompletions.create` 包装经哨兵实验确认在调用链上。
(2) 根因 #4（契约层）：GAP finding 引用已提交的 skipped provider_error 证据
（related_alert 未配置的显式失败产物）被 `_finding_from_draft` 的
SUCCESS/PARTIAL 可引用集硬杀，运行结果随模型引用行为抽签；spec §7.2 只约束
非 gap finding 与未提交输出。用户批准选项 A 后修复 `fc20b4a`：GAP 可引用任何
已提交证据，非 gap 仍限 SUCCESS/PARTIAL，编造 ID 仍硬拒绝；RED 复现原错误后
GREEN，3 新测试（含两条守卫），全量 `2272 passed, 4 skipped`。待独立复审的
修复链现为 `45ccde1..fc20b4a`。正式计数仍为 0；M5 仍 blocked。

独立复审 (2026-08-14，范围 `45ccde1..fc20b4a`)：**approve_with_followups**（1 High +
1 Low，复审员实跑目标套件 125 passed、tests/diagnosis 528 passed、Ruff clean）。
H1：GAP 放宽只改了准入层 `_finding_from_draft`，最终校验层 `validate_v11_result`
的 `_require_committed_refs` 仍要求所有 finding（含 GAP）仅引用 SUCCESS/PARTIAL
证据，两层契约互相矛盾；Multi 正式路径（基类 `V11Runtime.result_validation`）会把
合法 GAP finding 以 `finding_evidence_reference` 终态杀 run，且 Lead correction
改不了 finding——失败模式从"investigator 级失败、run 继续"恶化为"整 run FAILED"。
当日按 Light 路径 RED→GREEN 修复（已提交 `788a0b3`）：`result_validation.py` 新增同 run
`committed_evidence` 集合，finding 引用校验改为 GAP → refs ⊆ committed、非 GAP →
refs ⊆ usable（逐字节不变），candidate/assessment/lead 路径未动。RED 确认目标测试
修复前以 `finding_evidence_reference` 失败；4 条新测试含三条硬拒绝守卫（非 GAP
引用 skipped、GAP 引用未提交 ID、GAP 引用跨 run failed 证据均仍拒绝）。门禁：
focused 17 passed、tests/diagnosis 532 passed、全量 `2276 passed, 4 skipped,
1 warning`、Ruff 与 `git diff --check` clean。跟进复审（同一审查线程）：**approve**，
确认两层契约一致、无新豁免面、RED 可信，无 blocking/high/medium/low 遗留。
L1（lease ≤1.5s 时 heartbeat 下限边界；实际 manifest 默认 900s 不可达）按复审员
建议记录不改码，入 T13 对账。正式 SS30/TT90 预测、paired attempts、label opens
仍为 `0`；H1 修复提交后 capability artifact 需绑定新的干净 HEAD 重新认证；
M5 仍 blocked。

Verification evidence (M5 capability re-certification, 2026-08-15): 用户在含
process-only credential 的 PowerShell（隐藏输入密钥与 base_url、NO_PROXY="*" 直连）
对干净 HEAD `36f03a1` 重新认证 gpt-5.6-terra @ cctq.ai：**result=passed**，全部 7 项
observation 通过，transport `native_json_schema`，tested_parallelism=3，
`git_dirty=false`，code_revision=`36f03a141d84f75d45b3833b2090bbb76d6ef9e5`，
endpoint_id=`349d8f7aeba75e46…`（与既往同一 endpoint），adapter
`openai-compatible-adapter-v2`、openai SDK 2.45.0、agents SDK 0.18.1，artifact_hash
`048e371b00f82e66a1d4822eda8ccd25d2fa3ca76dee766dd1ec6e0b3ed7c4c0`，artifact 位于
`D:\data\RCAEval\v11-m5\model-capability\349d8f7aeba75e46-gpt-5.6-terra\result.json`。
同日早些时候仓库 `output/model_capability/` 下的两次工程尝试（terra 全 probe 超
30s 死线、kimi-k3 全 AuthenticationError）均为 failed artifact，不具正式效力；
用户已决定正式 run 继续走 gpt-5.6-terra。首次 OB30 smoke 尝试因 PowerShell 窗口
残留旧 base_url 环境变量在 capability 准入处 fail-closed（`endpoint identity is
stale`，未发生任何模型调用，无预算消耗）；第二次因仓库存在未提交 docs 改动触发
`code revision is stale or dirty` fail-closed（同样零模型调用）。经用户指示，
smoke 级准入松绑为 `result=passed` + endpoint_id 匹配 + model 匹配（桌面 harness
`task4-ob30-acceptance-smoke.txt` v3，加非明文 endpoint 预检与整体脚本块防级联）；
**正式 SS30/TT90 准入契约（code revision/git_dirty/SDK 版本/环境指纹绑定）不变**，
正式 run 前仍需对最终干净 HEAD 重新认证一次。

Verification evidence (M5 OB30 Single 24k smoke passed, 2026-08-15): 2026-08-14 晚
provider 网关出现持续约 12 小时的退化——输入计费翻倍（同 prompt actual 4221→8608，
08-14 16:32 起，4 次运行稳定复现）且长结构化响应以两份 JSON 拼接返回（capture
`0df22f35` 两条 raw 均 Extra data/trailing characters，SDK 解析失败→重试→第三次
请求撞 24k 预算墙 fail-closed；预算门禁行为正确，零正式预算消耗）。2026-08-15 上午
网关自行恢复（actual 回落至 4213，单份合法 JSON），capture smoke
`d94dda42-17e0-46e9-bdef-4fac99105c92` **验收全门槛通过**：completed=true、
run_status=completed、model.completed=2/failed=0、tool.completed=2/failed=1（契约内
skipped）、read_only_violations=0、input 13507 + output 1395 = total 14902、33.4s、
transport native_json_schema、artifact_hash `4d6e11f9db4dd2c035e19d42ce2e03e536b28640018d1d69cdbee420839cd53b`（gpt-5.6-terra @ 349d8f7a…，code_revision `36f03a1`，parallelism 3；**git_dirty=true**——认证时 current.md 有未提交 docs 改动，smoke 级准入不查此项，正式准入会拒，正式 run 前需提交 docs 后对最终干净 HEAD 重认证）。证据：`D:\data\RCAEval\v11-smoke\capture-02b52ddab4a441fd8ad3df6183bee61f.{db,json,raw.jsonl}`。
同日用户在其它端点的认证尝试（gpt-5.6-terra/kimi-k3 @ 882e677d…、deepseek-v4-flash
@ a34e2a47…）均 failed，其中 deepseek-v4-flash 经隔离 probe 证实为端点能力边界
（不支持 response_format json_schema 且 thinking 模式禁 tool_choice=required），
认证正确 fail-closed。正式计数仍为 0；M5 仍 blocked，只待 docs 提交 + 终态重认证 +
用户授权正式预算。

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
