# DiagOps V11 Adaptive Multi-Agent RCA Implementation Plan

Status: `review_required`

Date: 2026-08-02

Authoritative specification:
`docs/superpowers/specs/2026-08-02-diagops-v11-adaptive-multi-agent-rca-design.md`

## 1. Execution contract

- Implement the approved R1–R27 specification. This Plan may choose file/task
  order but cannot weaken a requirement, gate, failure state, or claim boundary.
- Reuse only the primitives allowed by Spec §5.1. V11 must not call deterministic
  RCA, fixed-specialist loops, hybrid CauseType arbitration, hypothesis-led
  report/action paths, or a direct unversioned orchestrator entry.
- Keep the existing OpenAI Agents SDK, Pydantic, FastAPI, SQLAlchemy, SQLite,
  React, and test stack. Add no orchestration framework, model gateway, MCP,
  queue, vector store, remediation executor, or new table family.
- Schema V7 adds exactly the three approved physical `runtime_runs` columns.
  All other model and ownership additions remain in existing JSON payloads.
- Production tools remain read-only. Every Agent-facing tool must work through
  the normal registry against an offline local incident package without network,
  credentials, Docker, or an external telemetry service.
- Use the current single-writer, lease, event, idempotency, cancellation, and
  redaction primitives. Parallel Agents never write business projections.
- Add each behavior test-first: observe the focused test fail for the intended
  reason, implement the smallest shared fix, rerun the focused test, then run the
  affected regression group and Ruff.
- Persist structured decisions, evidence, tool observations, Critic checks,
  usage, and safe failures only. Never persist raw prompts, private reasoning,
  credentials, unrestricted provider payloads, or answer-bearing metadata.
- Formal evaluation is local and label-input-isolated. Its threat model is the
  trusted runtime's supported input surface, not a hostile process under the
  same OS account. Prediction receives no label/scorer path, argument,
  environment value, mount, Provider root, or working-tree file; package-root
  validation and path traversal fail closed. Evaluation opens labels read-only
  only after prediction hashes are frozen.
- Do not create a commit, branch, tag, push, or PR without explicit user
  authorization. A clean immutable source identity is a hard prerequisite for
  the final TT90 execution; stop before that gate if authorization is missing.
- Preserve the existing user-owned `AGENT.md` modification and unrelated dirty
  worktree content.

## 2. Milestones

| Milestone | Tasks | Exit condition | Independent review |
| --- | --- | --- | --- |
| M0 — Local holdout boundary | T1 | RCAEval partitions and process isolation are frozen before Agent implementation | Dataset/leakage review |
| M1 — Durable isolation | T2–T3 | Schema V7, run ownership, V10/V11 phases, entry gates, replay/diff are correct | Migration/runtime review |
| M2 — Local evidence and model boundary | T4–T6 | Nine offline tools, optional Tempo, skills, and three model-provider modes are certified | Tool/model/security review |
| M3 — Agent authority | T7–T8 | Lead/Investigators/Critic/Lead produce the only V11 diagnosis | Authority/concurrency review |
| M4 — Product integration | T9–T10 | Reports, actions, API/UI, recovery, privacy, legacy and OpenRCA compatibility pass | End-to-end review |
| M5 — Controlled evaluation | T11–T13 | Frozen controls, SS30 validation, one TT90 paired result, and honest claims | Evaluation/final review |

At each review, inspect the actual diff and all callers, record findings in
§10, close every blocking/high issue, update the Spec traceability evidence, and
do not weaken a failed threshold.

After the user approves this Plan, update `current.md` to `ready`; when execution
actually starts, set iteration/implementation to `implementing`/`in_progress`
and T1 to `in_progress`. At every M0–M5 review, update this Plan's task/evidence
ledger, the Spec focused-evidence column, and `current.md` before advancing.

## 3. M0 — Freeze the local held-out boundary

### T1. RCAEval preparation and label-input isolation

Requirements: R15, R17, R25.

Primary files:

- `backend/benchmarks/rcaeval/__init__.py` (new)
- `backend/benchmarks/rcaeval/__main__.py` (new)
- `backend/benchmarks/rcaeval/models.py` (new)
- `backend/benchmarks/rcaeval/prepare.py` (new)
- `backend/benchmarks/rcaeval/isolation.py` (new)
- `tests/benchmarks/test_rcaeval_prepare.py` (new)
- `tests/benchmarks/test_rcaeval_isolation.py` (new)

Steps:

1. Add red tests for the pinned upstream revision/archive hash and the exact
   seeded selector: OB30 and SS30 contain one case per `5 services × 6 faults`
   cell; TT90 contains all three repetitions per cell.
2. Reject missing/duplicate cells, mutable or changed source files, inf-family
   telemetry tokens (`inf`/`infinity` 任意大小写与符号) and JSON non-finite
   constants, unexpected labels, path traversal, and answer-bearing runtime
   fields. 字面量 `NaN` CSV 单元格按上游缺失值编码放行（RE2 istio-latency
   分位列在无有效样本时写出 `NaN`，实测 119/270 case 命中）；标识符形态
   单元格（如 16 位十六进制 trace ID `96e3653877800818`）不得再做 float
   试探——只按显式词元拒绝，避免科学计数法误解析（2026-08-02 修正案，
   经用户批准，见 §10 M0-R1/M0-R2）。Replace source case IDs with stable
   opaque IDs.
3. Implement `inspect` and `prepare` commands that emit separate runtime and
   evaluator-only label packages plus manifests and SHA-256 files. Refuse a
   non-empty output target; deterministic re-prepare must be byte-equivalent.
4. Add a prediction-process launcher whose explicit allowlist excludes label and
   scorer paths from arguments, environment, mounts, current directory, Provider
   roots, and runtime package manifests. Test unsupported path injection and
   traversal fail closed. Do not claim this prevents a hostile same-account
   process from scanning arbitrary host paths.
5. Add a separate evaluator-process launcher that accepts only an already-frozen
   prediction bundle/hash and opens labels read-only. It cannot import or invoke
   the production runtime.
6. Run synthetic fixture preparation first, then prepare the pinned real local
   RCAEval artifact. Do not copy the dataset or labels into the repository.

Focused verification:

```powershell
uv run pytest tests/benchmarks/test_rcaeval_prepare.py tests/benchmarks/test_rcaeval_isolation.py -q
uv run ruff check backend/benchmarks/rcaeval tests/benchmarks/test_rcaeval_prepare.py tests/benchmarks/test_rcaeval_isolation.py
```

Acceptance:

- OB30/SS30/TT90 counts, cell coverage, hashes, and provenance are exact.
- Runtime-package/label-package overlap is zero.
- No supported prediction input carries a label/scorer locator or answer-bearing
  payload before prediction freeze; this is not an OS sandbox claim.
- The artifact is described as locally held-out, not third-party blind.

### M0 review

Review selection reproducibility, answer leakage, filesystem/environment
isolation, overwrite behavior, archive parsing, source provenance, and wording.
T2 cannot start until real opaque runtime manifests exist and labels remain
outside the prediction process.

## 4. M1 — Durable V11 isolation

### T2. Domain contracts, run ownership, and Schema V7

Requirements: R2, R4–R6, R9–R10, R20, R27.

Primary files:

- `backend/domain/agent_plan.py`
- `backend/domain/agent_findings.py`
- `backend/domain/actions.py`
- `backend/domain/evidence.py`
- `backend/domain/memory.py`
- `backend/domain/multi_agent.py`
- `backend/domain/react_trace.py`
- `backend/domain/reports.py`
- `backend/domain/runtime.py`
- `backend/db/models.py`
- `backend/db/schema.py`
- `backend/db/migrations.py`
- `backend/db/repositories.py`
- `backend/db/sqlite_repository.py`
- corresponding `tests/domain/` and `tests/db/` files

Steps:

1. Add bounded enums and models for `FindingActor`, Lead actions/decisions,
   Critic verdicts/seven checks, diagnostic/authority/execution status, V11 step
   kinds, cancelled terminal states, free-text entity/mechanism, counterevidence,
   onset windows, and run ownership.
2. Keep legacy `AgentName` unchanged. Change only persisted finding attribution
   to its compatible superset so legacy fixed loops never gain an Investigator.
3. Scope round-two validation: legacy fixed actors require same-actor revision;
   V11 Investigator findings require same-run task and Critic assessment but may
   add a new finding without `revises_finding_id`.
4. Add payload-only `active_runtime_run_id`/`source_investigation_id` and required
   V11 `runtime_run_id` fields for plan, task, execution, finding, review, report,
   summary, action, and verification. Reject a mixed-owner V11 aggregate.
5. Put final `LeadDecision` and Critic assessments in `CoordinationReview`.
   Accepted candidate IDs are authoritative; `root_causes` is compatibility-only.
6. Add exactly three non-null Schema V7 columns to `runtime_runs`:
   `execution_contract_version`, `authority_mode`, and validated JSON
   `execution_contract`. Add `contract_integrity` to the failure enum.
7. Implement V6→V7 migration and prove fresh plus V3/V4/V5/legacy-V6/current-V6
   upgrades. Historical rows receive only bounded `v10_legacy` identity derived
   from already persisted fields; do not synthesize V11 artifacts.
8. Round-trip new payloads equally through memory and SQLite. Preserve historical
   ReAct `assistant_text` reload/redaction while V11 writers accept only bounded
   structured summaries.

Focused verification:

```powershell
uv run pytest tests/domain tests/db/test_migrations.py tests/db/test_runtime_migrations.py tests/db/test_v5_agentic_rca_persistence.py -q
uv run ruff check backend/domain backend/db tests/domain tests/db
```

Acceptance:

- Invalid ownership/task/round/Critic/status combinations fail before commit.
- Fresh and every supported upgrade produce the same Schema V7 manifest.
- Only the three approved physical columns are added.
- Legacy payloads remain readable without bulk rewriting.

### T3. Versioned phase profiles, atomic activation, entry gates, replay, and diff

Requirements: R7–R10, R13, R18, R20, R27.

Primary files:

- `backend/runtime/phases.py`
- `backend/runtime/phase_executor.py`
- `backend/runtime/coordinator.py`
- `backend/runtime/store.py`
- `backend/runtime/sqlite_store.py`
- `backend/runtime/writer.py`
- `backend/runtime/replay.py`
- `backend/runtime/diff.py`
- `backend/services/container.py`
- `backend/api/runtime_runs.py`
- corresponding `tests/runtime/` and `tests/api/test_runtime_runs_api.py`

Steps:

1. Add the approved V11 phase enum values and immutable `V10_PHASE_ORDER` and
   `V11_PHASE_ORDER`. Select order, preconditions, handler map, resume position,
   events, checkpoint digest, and UI projection from execution contract version.
2. Keep legacy phase meanings unchanged. Give shared boundary names versioned
   handlers; V11 evidence/report/finalize handlers cannot call V4 execution,
   hypothesis report, CauseType action, or unconditional completion logic.
3. Make run create atomically persist and validate the full execution contract.
   Existing compatibility columns must equal JSON projections on create/reload,
   otherwise terminally persist `contract_integrity`.
4. Add `BusinessMutation.activate_projection`. In the V11 INTAKE transaction,
   validate prior terminal/frozen state, atomically set the active owner, clear
   all previous latest diagnostic artifacts, and commit the intake checkpoint.
5. Require all later business commits to match the active owner. A sequential
   V11 rerun freezes before switching. A legacy-to-V11 rerun creates a linked new
   investigation and leaves the legacy record byte-equivalent.
6. Make termination ownership-aware. A run that never activated writes an empty
   run-owned `projection_state="not_activated"` frozen projection and never
   snapshots the current active owner's view, for every cancel/failure/timeout/
   lease/recovery path.
7. Extend V11 frozen projection/replay/diff with candidate, Lead, Critic, status,
   authority, ownership, evidence, and usage. Retain the V10 parser unchanged;
   replay invokes no model, provider, or tool.
8. Gate service/API/CLI adapters behind a persisted V11 RuntimeRun. Runtime-off
   or direct orchestrator calls reject V11. Add legacy recovery rejection and
   absolute deadline/budget monotonicity tests.
9. Add forbidden-call sentinels for every Spec §5.1 legacy diagnostic function;
   complete, partial, and inconclusive fake V11 flows must trigger none.

Focused verification:

```powershell
uv run pytest tests/runtime tests/api/test_runtime_runs_api.py -q
uv run ruff check backend/runtime backend/services/container.py backend/api/runtime_runs.py tests/runtime tests/api/test_runtime_runs_api.py
```

Acceptance:

- V10 and V11 phase/resume/replay semantics cannot cross.
- Faults before/after activation never expose a half-cleared or wrong-owner view.
- Pre-INTAKE terminal runs replay as `not_activated` with no inherited output.
- No V11 entry or handler reaches a forbidden legacy diagnostic function.

### M1 execution evidence (2026-08-06)

| Task | Status | Evidence | Review state |
| --- | --- | --- | --- |
| T2 | implemented / verified | Test-first RED→GREEN domain, ownership, migration, persistence, Critic-owner, finding-linkage, and execution-owner coverage; second-round Medium 1–3 direct-write regressions first RED then GREEN; `uv run pytest tests/domain tests/db/test_migrations.py tests/db/test_runtime_migrations.py tests/db/test_v5_agentic_rca_persistence.py -q` → `199 passed in 6.81s`; scoped Ruff clean | Review fixes complete; independent migration re-review pending |
| T3 | implemented / verified | Test-first RED→GREEN phase-profile, frozen source-integrity, BusinessMutation, contract-repair, summary projection, service/API gate, and replay/diff coverage; second-round ownerless RESULT_VALIDATION and forged plan/task/Investigation aggregate regressions first RED then GREEN; `uv run pytest tests/runtime tests/api/test_runtime_runs_api.py -q` → `420 passed, 2 skipped, 1 warning in 117.53s`; `tests/runtime/test_review_fixes.py` → `27 passed, 1 skipped`; scoped Ruff clean | Review fixes complete; independent runtime re-review pending |
| T2–T3 third-round review fixes | implemented / verified | Third-round findings RR-H1/RR-M1/RR-M2/RR-L2 closed: deadline/budget monotonicity + V11 terminal-transition coverage added in `tests/runtime/test_v11_deadline_budget.py`（6 项，其中 RR-L2 先 RED 后 GREEN）；T4/T5 提前落地契约的 domain 覆盖补齐于 `tests/domain/test_evidence_memory_contracts.py`（9 项，未改实现直接 GREEN，确认契约本就正确）；RR-M2 contract-integrity 修复持久化先 RED 后 GREEN；`uv run pytest tests/domain tests/db/test_migrations.py tests/db/test_runtime_migrations.py tests/db/test_v5_agentic_rca_persistence.py tests/db/test_schema_v7_red.py -q` → `209 passed in 7.08s`；`uv run pytest tests/runtime tests/api/test_runtime_runs_api.py -q` → `427 passed, 3 skipped, 1 warning in 118.42s`；scoped Ruff clean；`git diff --check` clean | Third-round fixes verified; RR-L1/RR-L3 open (low, due before M2) |

M1 scope is limited to T2–T3. M2 evidence/model/skill work has not started.

### M1 review

Independently inspect all migration paths, phase callers, transaction boundaries,
freeze paths, run ownership, deadline arithmetic, cancellation races, and direct
entries. On 2026-08-06, the ten reproduced findings from the first M1 review
(High 1–6, Medium 7–10) were fixed with shared validators/profile selection,
transaction-local fail-closed repair, and pre-link service/API checks; focused
RED→GREEN regressions and both full Gates are recorded above. The second-round
review reproduced three medium persistence bypasses: an ownerless
RESULT_VALIDATION execution without `analysis_round`, direct plan/task writes
that skipped V11 revalidation, and direct Investigation aggregate writes that
skipped nested owner validation. They were fixed with the existing
`model_validate(model_dump(...))` boundary pattern, preserving historical
payload field presence and validating SQLite before replacement. The approved
contract and milestone scope are unchanged; independent migration/runtime
re-review remains the next action and M2 remains out of scope.

M1 third-round review (2026-08-07): conclusion `approve_with_followups`. Four
findings closed: RR-H1 (high) — T3 step 8 的 absolute deadline/budget
monotonicity tests 此前在 T3 证据行的整体 RED→GREEN 声称中被隐含覆盖，实测零测试；
现于 `tests/runtime/test_v11_deadline_budget.py` 补齐四类覆盖（check_execution
deadline → TIMEOUT 终态、`_deadline_for` 跨 attempt 绝对锚定不重置、V11
investigation failed → run FAILED/OUTPUT_VALIDATION、`timeout_seconds=121`
构造拒绝），特此修正该不实声称——在补齐前不得视为已测。RR-M1 (medium) — T4/T5
契约（EvidenceProvenance/EvidenceScope/新 evidence provider·kind、
MemoryVerificationStatus、ModelProvider.OPENAI_COMPATIBLE）提前落地却无测试；
`tests/domain/test_evidence_memory_contracts.py` 补 9 项最小构造/校验/序列化
往返测试，未改实现即全部通过，确认契约正确仅缺覆盖。RR-M2 (medium) —
`_repair_contract_integrity` 不写回修正后的 execution_contract，每次读路径重复
修复并重 freeze/改写业务投影；修复为同事务持久化 `_safe_contract_projection`，
先 RED（二次读取 investigation.updated_at 漂移、契约列仍为篡改值）后 GREEN，
既有 idempotence 语义不变。RR-L2 (low) — CANCELLING 的 V11 run 在 deadline
到期时 `_fail_if_owned` 仅处理 RUNNING 而滞留 CANCELLING；修复为 deadline
分支先 `_finish_cancel` 再 `_fail_if_owned`，先 RED（滞留 cancelling）后
GREEN（收敛 CANCELLED）。第三轮剩余两项 low（RR-L1/RR-L3）已于 2026-08-07
当日关闭：RR-L1 —— `_v11_intake` 不再在 handler 内经独立事务提前 activate，
改为本地构造 owner 已切换投影、激活与清理只发生在 commit_phase 事务内
（state.record 清空形状复用 `_projection_record` 单一事实来源），先 RED
（spy 捕获 handler 独立 activate + commit 前持久投影已切换）后 GREEN；
RR-L3 —— `SQLiteInvestigationRepository` 新增公开 `get_with_connection`，
`sqlite_store` 两处私有调用改为公开接口。回归：T2 gate 209 passed、T3 gate
429 passed/3 skipped/1 warning、scoped Ruff 与 diff-check clean。
Approved 契约与 milestone 范围不变。

## 5. M2 — Local evidence and model boundaries

### T4. Nine-tool offline incident-package profile and data-only skills

Status (2026-08-07): 九工具 manifest（list_agent_specs 单一来源，query_prometheus
internal）、scoped trace/runtime-state/related-alert/memory 查询与有界 payload
契约、local package 八 Provider（状态矩阵 success/empty/skipped/partial/failed/
timeout，绝不回退 mock）、verified-memory guarded lookup（cutoff/self/ancestor/
foreign 拒绝）、四条 data-only skill + catalog hash、离线验收 80 行全绿；
focused gate 336 passed（含 T5 归入 tests/providers 的 10 条，最终 2026-08-08 复跑）、
offline_tool_acceptance rows=80 failed=0、scoped Ruff clean。

Requirements: R3, R11, R21–R22, R24, R27.

Primary files:

- `backend/domain/tool_calls.py`
- `backend/domain/tool_queries.py`
- `backend/domain/evidence.py`
- `backend/domain/memory.py`
- `backend/config/settings.py`
- `config/diagops.yaml`
- `backend/providers/local_package.py` (new)
- `backend/providers/registry.py`
- `backend/tools/registry.py`
- `backend/tools/provider_tools.py`
- `backend/diagnosis/adaptive_tools.py`
- `backend/diagnosis/diagnostic_skills.py` (new, data only)
- `backend/services/container.py`
- `backend/services/offline_tool_acceptance.py` (new)
- corresponding domain/provider/tool/diagnosis/service tests

Steps:

1. Add bounded trace, runtime-state, related-alert, dependency, and verified-
   memory query/payload contracts, evidence scope, source class, provenance, and
   the approved success/empty/skipped/partial/failed/timeout matrix.
2. Add `ToolSpec.exposure` with compatible default, mark `query_prometheus`
   internal, and implement `list_agent_specs()`. Freeze exactly the nine approved
   read-only names; invocation rechecks manifest membership and `read_only`.
3. Implement the minimal local-package loader and provider adapters for logs,
   metrics, spans, catalog, deployments, runtime state, alerts, dependencies,
   and verified incidents. Never fall back to mocks when a source is absent.
4. Derive dynamic dependency direction only from parent/child spans, optionally
   augmenting with static catalog edges. Normalize bounded fields before storage.
5. Implement verified-memory lookup through guarded repository records. Reject
   unverified/future/foreign records and current or source-ancestor self-memory.
6. Define the four frozen data-only skill records, validate required tools against
   `list_agent_specs()`, hash catalog/order, and add no executable loader/MCP.
7. Build an offline acceptance that invokes all nine tools through the normal
   registry and writes a hashed per-tool result matrix. Each tool must produce at
   least one non-empty `success` against a prepared package; empty, malformed,
   missing, timeout, redaction, provenance, scope, and idempotency are separate
   rows and cannot satisfy that success requirement.

Focused verification:

```powershell
uv run pytest tests/domain tests/providers tests/tools tests/diagnosis/test_adaptive_tools.py tests/services/test_offline_tool_acceptance.py -q
uv run python -m backend.services.offline_tool_acceptance
uv run ruff check backend/domain backend/providers backend/tools backend/diagnosis/diagnostic_skills.py backend/services/offline_tool_acceptance.py tests
```

Acceptance:

- The manifest contains exactly nine Agent-visible read-only tools.
- All nine each produce at least one non-empty success locally with no network,
  credentials, Docker, or external service; every required failure/empty branch
  is recorded separately. Missing one success fails R22.
- Synthetic fixtures prove contracts only; formal packages retain public-dataset
  provenance and contain no labels.

### T5. File/Tempo trace parity and Docker-local gate

Status (2026-08-07): FileTraceProvider/TempoTraceProvider 共用 canonical span
投影与 select_trace_spans 语义；replay 恒定偏移 + 可逆 ID 映射持久化；
preflight（daemon/pinned digest/端口/磁盘）+ 唯一 Compose project + scoped
cleanup 实现。本机 Docker daemon 不可用，live gate 按 plan 记录 blocked
（reason: docker daemon unavailable，exit=2，report 已写
output/tempo_acceptance/tempo-gate-report.json），离线 parity 由 fake-HTTP
测试证明（file vs tempo 归一化结果完全一致）。image digest 真实值离线无法
解析，机制实现为 DIAGOPS_TEMPO_GATE_IMAGE_DIGEST 显式配置 + daemon 实测校验，
未配置即 blocked——与 plan 的偏差见最终报告。focused gate 17 passed、Ruff clean。

Requirements: R21–R23.

Primary files:

- `backend/providers/local_package.py`
- `backend/providers/tempo.py` (new)
- `backend/services/tempo_acceptance.py` (new)
- `compose.tempo-gate.yaml` (new)
- `tests/providers/test_tempo_provider.py` (new)
- `tests/services/test_tempo_acceptance.py` (new)

Steps:

1. Implement `FileTraceProvider` and `TempoTraceProvider` behind the same
   `query_traces` contract and canonical span projection.
2. Pin one `grafana/otel-lgtm` image by immutable identity. Use a unique Compose
   project, bounded daemon/image preflight, readiness polling, and scoped cleanup.
3. Replay one prepared multi-service trace through local OTLP with a persisted
   reversible time/ID mapping. Never rewrite the canonical incident scope.
4. Compare file and Tempo results after documented normalization for path,
   parent relation, service/operation, order, status, and duration tolerance.
5. When Docker is unavailable, record the gate `blocked` with the exact preflight
   reason; offline acceptance remains runnable and is never marked failed by it.

Focused verification:

```powershell
uv run pytest tests/providers/test_tempo_provider.py tests/services/test_tempo_acceptance.py -q
uv run python -m backend.services.tempo_acceptance
uv run ruff check backend/providers/tempo.py backend/services/tempo_acceptance.py tests/providers/test_tempo_provider.py tests/services/test_tempo_acceptance.py
```

Acceptance:

- File is the mandatory default; Tempo is the sole claimed real trace backend.
- A successful gate proves local Docker replay/query parity, not production
  observability readiness.

### T6. Official OpenAI, DeepSeek preset, and generic compatible adapter

Status (2026-08-08): canonicalize_endpoint 唯一算法（含 spec 全部 accept/reject
向量，cleartext 仅限本机）；官方 OpenAI 钉死 https://api.openai.com/v1 且不继承
环境 base URL；DeepSeek 重构为共享 compatible adapter 预设（序列化身份不变，
既有 6 项测试原样通过）；OpenAICompatibleChatCompletionsModel 请求级 client、
超时/重试有界、稳定失败分类；model-capability-v1 工件（endpoint_id 绑定、
自排除 canonical hash、无 URL/凭证/正文）+ live 认证 CLI（仅实现未运行）；
V11 run-create 对 openai_compatible 要求精确 tuple 的 passed 工件（未认证 422
且不插行），请求端 server-owned 字段 extra=forbid 拒绝；config API 新增
capability_certification_status + credential-free endpoint_id。focused gate
106 passed、scoped Ruff clean、全量 1927 passed/3 skipped。

Status (2026-08-08): M2R-1 关闭——补 spec 7.8/T6 step 3-4 要求的 local
slow-endpoint 取消语义证据 4 条（取消传播到在途 HTTP 请求且 handler 观测到
CancelledError、wait_for deadline 0.5s 截断 30s 慢响应、取消后晚到响应零提交
no-late-commit、取消路径 transport 在创建 loop 上关闭；真实 SDK+MockTransport
与 adapter 层各覆盖）。实况：4 条新测试首轮即全绿，未改实现——每请求 client +
finally close 结构已满足契约，仅缺覆盖（同 M1 RR-M1 先例，不伪造 RED）。
focused gate 110 passed、指定两文件 22 passed、scoped Ruff clean。

Requirements: R8, R11–R13, R26–R27.

Primary files:

- `backend/domain/multi_agent.py`
- `backend/config/settings.py`
- `config/diagops.yaml`
- `backend/diagnosis/openai_model.py`
- `backend/diagnosis/deepseek_model.py`
- `backend/diagnosis/openai_compatible_model.py` (new)
- `backend/services/model_capability.py` (new)
- `backend/services/container.py`
- `backend/api/config.py`
- `backend/api/runtime_runs.py`
- corresponding config/diagnosis/service/API tests

Steps:

1. Add `openai_compatible` settings with canonical `base_url`, Chat Completions
   API mode, model, and environment-only `DIAGOPS_AGENTS_API_KEY`. Reject URL
   credentials, unsafe cleartext non-local endpoints, and ambiguous paths; cover
   every Spec canonicalization accepted/rejected vector.
2. Pin official OpenAI to `https://api.openai.com/v1` and its existing Responses
   path regardless of ambient base-URL variables. Keep serialized DeepSeek and
   its key compatible while routing it through the shared compatible adapter.
3. Build the adapter with the installed OpenAI SDK only. Preserve structured
   result/tool-call/usage semantics, bounded output, timeout, retry categories,
   cancellation propagation, transport cleanup, and no late commit.
4. Add a local deterministic fake HTTP endpoint covering success, malformed
   structure, missing usage/tool calls, auth, rate limit, timeout, cancellation,
   connection cleanup, no-late-commit, tracing isolation, and redaction without
   contacting a model.
5. Implement separate `model-capability-v1` artifacts bound to canonical
   endpoint hash, provider/model/API mode, capability observations, code/config
   identities, and self-field-excluding canonical hash. Never persist a key.
6. Make V11 run-create compatibility fields exact-match-only against the
   server-approved tuple/prompt; reject client execution version, authority, or
   endpoint and every uncertified compatible tuple before inserting a run.
7. Add an explicit live certification command for a real compatible endpoint.
   It proves tool calls, structured results, usage, bounded response, and the
   configured parallelism through remote-observable behavior only—not accuracy
   or server-side cancellation. Formal scoring cannot start without a matching
   passed artifact.

Focused verification:

```powershell
uv run pytest tests/config tests/diagnosis/test_openai_model.py tests/diagnosis/test_deepseek_model.py tests/diagnosis/test_openai_compatible_model.py tests/services/test_model_capability.py tests/api/test_runtime_runs_api.py -q
uv run ruff check backend/config backend/diagnosis backend/services/model_capability.py backend/api tests/config tests/diagnosis tests/services/test_model_capability.py tests/api/test_runtime_runs_api.py
```

Acceptance:

- Official OpenAI, DeepSeek, and certified compatible endpoints share safe
  runtime contracts without a new gateway or framework.
- Fake-endpoint tests are network-independent; real scoring identity is exact.
- Credentials do not appear in config, contracts, artifacts, logs, events, or UI.

### M2 review

Review the nine-tool single source, offline source coverage, trace parity,
self-memory exclusion, injection/redaction, Docker cleanup, endpoint
canonicalization, official pinning, capability hashing, and run-create trust
boundary. Close blocking/high findings before Agent orchestration.

M2 review record (2026-08-08): independent review concluded
`approve_with_followups` with no blocking finding and reran every focused gate
independently (T4 336 passed + offline acceptance rows=80 failed=0; T5 17
passed + Docker gate `blocked` with preflight reason, daemon unavailable; T6
106 passed; full suite 1927 passed/3 skipped; Ruff clean). Three implementer
claims were verified: the unreported `.gitignore` change is benign (three
acceptance-output ignore rules); the two schema-version assertion fixes (6→7)
are an M1 leftover confirmed failing on merge commit `a40942a` (M1's scoped
gates never covered those two files); the T5 digest-via-environment deviation
is an equivalent safety mechanism and the plan stays approved (write the
mechanism into T5's body on the next plan touch). M2R-1 (high, §7.8
slow-endpoint cancellation evidence) was closed the same day with four tests
and focused re-review; M2R-2 (verified-memory resolver wiring) and M2R-3
(`assert_agent_callable` call site) are mandatory T7 prerequisites; M2R-4
(span-attribute redaction row) is optional hardening. The T5 live Docker
parity run remains owed on a host with a daemon. The approved contract and
milestone scope are unchanged; the M2 snapshot is committed as
`M2_BASE_SHA=c2245ac` in the isolated M3 worktree. M2R-2 and M2R-3 are closed
by the T7 implementation and focused evidence below; M2R-4 remains optional
hardening.

## 6. M3 — Make Agents authoritative

### T7. Lead planning and isolated general Investigators

Requirements: R1–R3, R5–R6, R11–R13, R24, R27.

Primary files:

- `backend/diagnosis/v11_runtime.py` (new)
- `backend/diagnosis/agents_runtime.py` (legacy only)
- `backend/diagnosis/adaptive_tools.py`
- `backend/runtime/phase_executor.py`
- `backend/services/container.py`
- `tests/diagnosis/test_v11_runtime.py` (new)
- `tests/runtime/test_phase_executor.py`

Steps:

1. Add fake-model red tests for Lead planning actions, task bounds, objectives,
   scopes, discriminators, skill selection, allowed tools, and remaining budget.
   Planning rejects `conclude` and infeasible or duplicate work.
2. Implement the V11 pipeline using existing SDK model lifecycle, timeout,
   cancellation, token, and adapter primitives. Keep `AgentsRcaRuntime` as the
   `v10_legacy` orchestration; do not add V11 branches to fixed Agent loops.
3. Persist the Lead plan before Investigator work. Spawn at most three bounded
   Investigator instances with one shared frozen nine-tool manifest and unique
   instance IDs.
4. Isolate round-one contexts: each receives only incident, assigned task, safe
   catalog/manifest, its own committed observations, and remaining budget—not
   sibling drafts or deterministic hypotheses.
5. Route tool calls through existing idempotent persisted execution. Findings
   can cite only committed same-investigation evidence and persist concise
   summaries, counterevidence, gaps, task/instance/run ownership, and usage.
6. Cover one/all Investigator failure, duplicate call, empty evidence, transient
   retry, budget exhaustion, timeout, cancellation, late result, and cleanup.

Focused verification:

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py tests/diagnosis/test_adaptive_tools.py tests/runtime/test_phase_executor.py -q
uv run ruff check backend/diagnosis/v11_runtime.py backend/diagnosis/adaptive_tools.py backend/runtime/phase_executor.py tests/diagnosis/test_v11_runtime.py tests/diagnosis/test_adaptive_tools.py tests/runtime/test_phase_executor.py
```

Acceptance:

- No fixed Log/Metric/Deployment Agent or deterministic hypothesis enters V11.
- Round-one contexts are isolated and all evidence is committed before citation.
- Actor/tool/token/turn/deadline limits remain correct under concurrency.

### T8. Critic, supplemental round, Lead adjudication, and validator

Requirements: R1, R4–R7, R9, R11–R13, R27.

Primary files:

- `backend/diagnosis/v11_runtime.py`
- `backend/diagnosis/result_validation.py`
- `backend/runtime/phase_executor.py`
- legacy validators/builders for compatibility-only guards
- corresponding diagnosis/runtime tests

Steps:

1. Require exactly the seven Critic checks per candidate, evidence for pass/fail,
   a named gap for unknown, same-review ownership, and valid verdict references.
2. Persist first Critic review after round-one findings. Only `needs_evidence` may
   create at most three round-two tasks tied to the requesting assessment.
3. Execute or explicitly skip `INVESTIGATOR_ROUND_2`, then execute or skip one
   `CRITIC_RECONCILIATION`. Reconciliation reuses assessment IDs and cannot emit
   another `needs_evidence`, task, assessment, or third round.
4. Allow final Lead `conclude` only with Critic-accepted candidate IDs;
   `inconclusive` has no candidates and requires a stop reason.
5. Implement mechanical validation for ownership, committed evidence, structured
   scope consistency, bounds, rank, permissions, safe text, budget, and state
   transitions only. Do not call CauseType/provider semantic validation.
6. Add mutation sentinels proving validator/persistence/reload never changes
   rank, entity, mechanism, evidence, counterevidence, or onset window.
7. Implement required-actor and diagnostic-status matrices, one schema-correction
   attempt, partial evidence minimum, and compatibility-only attribution omission.

Focused verification:

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py tests/diagnosis/test_result_validation.py tests/diagnosis/test_evidence_validation.py tests/diagnosis/test_orchestrator.py tests/runtime/test_phase_executor.py -q
uv run ruff check backend/diagnosis backend/runtime/phase_executor.py tests/diagnosis tests/runtime/test_phase_executor.py
```

Acceptance:

- Agent-authored conclusions survive commit/reload unchanged.
- Invalid or insufficient results reject/fail/become inconclusive per contract;
  no deterministic result is substituted.
- A complete/partial V11 diagnosis always has final Critic and Lead decisions.

### M3 execution evidence (2026-08-08)

The M2 snapshot was committed before M3 implementation as
`M2_BASE_SHA=c2245ac` (`chore(v11): checkpoint M2 T4-T6`). M3 was implemented
on branch `codex/v11-m3` in the isolated worktree
`D:\agent\worktrees\sre-agent-v11-m3`; dirty `main` was not modified.

T7 followed RED→GREEN test-first coverage for Lead persistence and bounded
planning, the shared nine-tool manifest, production verified-memory resolver
wiring, invocation-time `assert_agent_callable`, isolated Investigator
contexts, committed evidence ordering, retry/resume, budget/timeout,
cancellation, late results, cleanup, and partial failure. The exact T7 gate
passed `53 passed`; its scoped Ruff gate passed.

T8 followed RED→GREEN coverage for exactly seven Critic checks per candidate,
one bounded evidence round, same-assessment reconciliation, accepted-only Lead
conclusion, inconclusive-without-candidates, mechanical non-mutating validation,
and final Critic+Lead requirements for complete/partial results. The exact T8
gate passed `164 passed`; its scoped Ruff gate passed.

M2R-2/M2R-3 focused evidence passed `27 passed`. The full repository gate passed
`1940 passed, 3 skipped, 1 warning`; `uv run ruff check backend tests` passed.
The M3 commit is intentionally handed off for independent review; M4/M5 were
not started and no merge or push was performed.

### M3 review

Trace model output through task/tool/evidence commits, Critic, Lead, validator,
projection, reload, timeout, and cancellation. Search all callers for legacy
root assignment, hypothesis and CauseType requirements. Close blocking/high
authority, race, privacy, or budget findings before product integration.

### M3 review-fix evidence (2026-08-08)

The independent M3 review returned `BLOCKING` against base
`b17da397807bacf6e155afe71062ad1c6c4fd868`. The review-fix was executed in the
same `codex/v11-m3` worktree with no reset, merge, push, M4, or M5 action. Each
finding received a minimal RED regression before the shared-boundary GREEN fix:

| Finding | RED → GREEN evidence | Shared-boundary closure |
| --- | --- | --- |
| B1 | `test_v11_run_owner_is_explicit_when_repository_has_multiple_investigations` | Explicit `investigation_id` is threaded through commit, execution, budget, summary, and resume helpers; no repository singleton inference remains. |
| H1 | `test_v11_frozen_manifest_rejects_same_cardinality_tool_swap`; `test_v11_frozen_manifest_rejects_unordered_contract` | Admission persists the ordered frozen nine-tool manifest/hash plus skill/capability identity and limits; dispatch/retry/resume read only that contract. |
| H2 | `test_v11_model_retry_is_one_classified_transport_attempt`; `test_session_retries_only_transport_once_and_persists_attempts` | One shared classified retry coordinator allows at most one transport/rate-limit retry, persists attempts, and does not retry semantic/validation errors. |
| H3 | `test_v11_model_precharges_input_before_setting_output_cap`; `test_v11_zero_tool_budget_does_not_start_model`; `test_container_freezes_effective_agent_configuration_and_budgets` | V11 freezes a non-None token ceiling, atomically charges input, caps output, checkpoints usage, and blocks zero-budget requests across manual/SDK paths. |
| H4 | `test_session_deadline_preflight_blocks_tool_before_provider_start`; `test_v11_model_deadline_preflight_blocks_request_before_start`; `test_late_tool_result_is_rejected_after_execution_fence_loss` | Absolute deadlines are propagated; every action preflights fit, uses the minimum timeout, and cannot commit a late result after timeout/cancel/fence loss. |
| H5 | `test_v11_tool_budget_reservation_is_atomic_and_durable`; `test_v11_transport_retry_reuses_one_durable_tool_reservation`; retry assertion in `test_session_retries_only_transport_once_and_persists_attempts` | Durable reservations are exact and shared by logical action retries/resume/cancel; a zero budget cannot call a provider and concurrent investigators cannot oversell. |
| H6 | `test_v11_all_investigator_failure_is_terminal_failed_without_diagnostic`; `test_v11_required_critic_failure_is_failed_without_inconclusive_fallback`; `test_v11_required_lead_failure_is_failed_without_inconclusive_fallback`; `test_v11_validator_failure_is_failed_without_inconclusive_fallback`; `test_v11_persistence_failure_is_terminal_failed_without_diagnostic` | Required actor, validator, and persistence failures terminalize `failed`, clear diagnostic projections, and never fall back to inconclusive/partial. |
| H7 | `test_v11_validator_rejects_evidence_from_another_run`; `test_v11_partial_requires_usable_evidence_passing_check_and_round_two_linkage`; `test_v11_partial_requires_final_round_critic_and_lead_audits`; `test_v11_complete_requires_final_critic_and_lead_execution_audit` | Validator mechanically enforces same-run usable evidence, exact assessment coverage, inconclusive/partial contracts, and final actor coverage without rewriting Agent-authored fields or invoking semantic CauseType/provider validation. |
| M1 | `test_v11_validator_scans_nested_critic_check_text_for_control_characters` | Safe-text validation scans all nested Critic/Lead result text for control characters only. |
| M2 | `test_v11_critic_success_has_one_durable_audit_execution`; `test_v11_reconciliation_has_one_durable_audit_execution`; `test_v11_critic_output_failure_updates_one_audit_execution` | Critic and reconciliation each retain one unique AgentExecution across success and failure, including actor, attempt, deadline, usage, and resume metadata. |

Final review-fix verification: T7 exact gate `70 passed`; T8 exact gate
`184 passed`; T7/T8 scoped Ruff clean; M2R broad focused `83 passed, 2
skipped`; M2R keyword focused `16 passed, 33 deselected`; full
`uv run pytest -q` `1967 passed, 3 skipped, 1 warning`; full
`uv run ruff check backend tests` clean. T4 and T5 remain `implemented /
verified` in the task ledger; T5's live Docker execution is the only explicitly
host-dependent unavailable check. The review-fix commit is isolated to
`codex/v11-m3`; the same-thread independent re-review is pending.

### M3 second-round review-fix2 evidence (2026-08-09)

The second independent M3 review returned `BLOCKING` against base
`629bc34e823ff9f5ff163dca5f3924a161baafd3`. The existing V11 contract and
milestone scope were preserved. Each finding first received a minimal failing
regression, then a shared-boundary GREEN fix:

| Finding | RED → GREEN evidence | Shared-boundary closure |
| --- | --- | --- |
| B1 | `test_v11_run_owner_is_explicit_when_repository_has_multiple_investigations`; `test_v11_nested_execution_contract_digest_fences_capability_and_limits` | Admission creates a server-owned canonical nested contract/digest; explicit `investigation_id` and the complete contract are validated through clone, bind, dispatch, retry, resume, and every persistence helper. |
| H1 | `test_v11_nested_execution_contract_digest_fences_capability_and_limits`; `test_v11_frozen_manifest_rejects_same_cardinality_tool_swap`; `test_v11_frozen_manifest_rejects_unordered_contract` | Dispatch/retry/resume read the immutable ordered nine-tool manifest/hash, skill/capability identity, and validated limits; a same-cardinality tool or capability/limit mutation fails closed. |
| B2/H2 | `test_v11_sdk_provider_retry_is_only_the_persisted_outer_retry`; `test_v11_model_retry_is_one_classified_transport_attempt`; `test_v11_parse_failure_marks_latest_retry_attempt_invalid_output` | SDK/provider retry is explicitly zero; the persisted coordinator owns at most one transport/rate-limit retry, persists attempts, and marks a malformed second attempt invalid rather than retaining the old failure. |
| H3 | `test_v11_sdk_model_requests_reserve_decreasing_output_caps`; `test_v11_model_precharges_input_before_setting_output_cap`; `test_v11_zero_tool_budget_does_not_start_model`; `test_v11_sdk_budget_reservation_is_released_when_preflight_rejects` | Each SDK request atomically charges input, derives a descending remaining output cap, and performs no request at zero budget; the same durable reservation applies across manual/provider paths. |
| H4 | `test_v11_model_deadline_preflight_blocks_request_before_start`; `test_session_deadline_preflight_blocks_tool_before_provider_start`; `test_late_tool_result_is_rejected_after_execution_fence_loss` | Absolute deadline and cancellation preflight every model/tool action, bound timeout by remaining time, and reject late commits. |
| H5 | `test_v11_tool_budget_reservation_is_atomic_and_durable`; `test_v11_transport_retry_reuses_one_durable_tool_reservation`; `test_v11_investigators_share_a_bounded_concurrent_gate` | One durable reservation belongs to one logical tool action and is reused by transport retry/resume/cancel; concurrent Investigators share one bounded gate and cannot oversell budget. |
| H6 | `test_v11_all_investigator_failure_is_terminal_failed_without_diagnostic`; `test_v11_required_critic_failure_is_failed_without_inconclusive_fallback`; `test_v11_required_lead_failure_is_failed_without_inconclusive_fallback`; `test_v11_validator_failure_is_failed_without_inconclusive_fallback`; `test_v11_required_investigator_failure_stops_before_critic_or_lead` | Required actor or persistence failure terminalizes `failed`, clears diagnostic projection, and stops later V11 phases; no pseudo-`inconclusive` fallback is emitted. |
| H7/M1 | `test_v11_validator_rejects_orphan_supplemental_task_ids`; `test_v11_partial_requires_usable_evidence_passing_check_and_round_two_linkage`; `test_v11_validator_scans_nested_critic_check_text_for_control_characters`; `test_v11_validator_rejects_whitespace_controls_in_nested_assessment_text` | Validator mechanically requires persisted same-run round-two task IDs to match the requesting assessment, enforces partial/inconclusive contracts, and scans nested result text for controls without semantic CauseType/provider validation. |
| M2 | `test_v11_critic_success_has_one_durable_audit_execution`; `test_v11_reconciliation_has_one_durable_audit_execution`; `test_v11_critic_output_failure_updates_one_audit_execution`; `test_v11_parse_failure_marks_latest_retry_attempt_invalid_output` | Critic/reconciliation success and failure retain one unique durable execution audit, and parse failure updates the latest retry attempt with actor, attempt, deadline, usage, and resume metadata. |

Final second-round verification: T7 exact gate `79 passed`; T8 exact gate
`195 passed`; T7/T8 scoped Ruff clean; explicit M2R-2/M2R-3 and isolation
focused gate `84 passed, 3 skipped`; full `uv run pytest -q` `1981 passed,
3 skipped, 1 warning`; `uv run ruff check .` and `uv run ruff check backend tests`
clean; `git diff --check 629bc34..HEAD` clean. The one warning is the existing
Starlette/httpx TestClient deprecation warning. T4 and T5 remain
`implemented / verified`; T5's live Docker execution remains the only
host-dependent unavailable check. The single `M3_REVIEW_FIX2_SHA` commit is
isolated to `codex/v11-m3`; no merge, push, M4, or M5 action was taken, and the
same-thread independent re-review is pending.

### M3 third-round review-fix3 evidence (2026-08-09)

The third independent M3 review returned `BLOCKING` against base
`b8c08d8a410877ef021cde69be0ec2faac98fb28`. The approved §6 scope, V10 legacy
boundary, and M2 snapshot were preserved. Each finding first received a
minimal RED regression, then a shared-boundary GREEN fix:

| Finding | RED → GREEN evidence | Shared-boundary closure |
| --- | --- | --- |
| B1 | `test_v11_started_model_reservation_is_durable_for_resume_reconciliation` (RED collection, GREEN); `test_v11_resume_releases_crash_window_reservation_once`; `test_v11_sdk_reservation_retry_reuses_one_durable_allocation_and_settles_once`; `test_v11_concurrent_model_reservations_cannot_oversell_token_ceiling` | Existing `RuntimeEvent`/`RuntimeWriter` durable model lifecycle events persist reservation identity, reserved tokens, attempt, and status before SDK send; terminal settle/release is idempotent, retry reuses the same logical reservation, and resume releases incomplete reservations once before recomputing budget. |
| B2 | `test_v11_official_provider_contract_and_client_ignore_ambient_endpoint` (RED, GREEN); `test_v11_official_contract_survives_sqlite_reload_with_ambient_endpoint` | Admission includes the pinned official endpoint identity in the sealed contract/digest; V11 `AsyncOpenAI` receives `OFFICIAL_OPENAI_BASE_URL` and `max_retries=0`, validates the actual client URL, and compatible custom URLs retain explicit endpoint semantics. |
| H1 | `test_v11_round_two_required_investigator_failure_terminalizes_before_reconciliation` (RED, GREEN); `test_v11_round_two_completed_partial_batch_is_not_blanket_failed` | Round-two assessment ownership or required Investigator failure clears the projection and terminalizes before `completed_rounds=2`; the phase boundary stops reconciliation/Critic/Lead, while valid completed supplemental work is not blanket-failed. |
| H2 | `test_v11_inconclusive_lead_clears_candidates_before_persist_and_reload` (RED, GREEN) | Lead semantic normalization clears candidates, accepted Critic assessment IDs, and root-cause attributions before persistence when the legal decision is `INCONCLUSIVE`; the mechanical validator remains read-only and the stop reason survives reload. |

Final third-round verification: T7 exact `86 passed`; T8 exact `202 passed`;
T7/T8 scoped Ruff clean; explicit M2R-2/M2R-3 and isolation focused
`84 passed, 3 skipped`; full `uv run pytest -q` `1990 passed, 3 skipped, 1
warning`; `uv run ruff check .` and `uv run ruff check backend tests` clean.
The warning is the existing Starlette/httpx TestClient deprecation warning.
`M2_BASE_SHA=c2245ac` remains the separately committed M2 snapshot. The
third-round fix is isolated to `codex/v11-m3`; symbolic
`M3_REVIEW_FIX3_SHA` is pending the single commit and same-thread independent
review. No merge, push, M4, or M5 action was taken.

### M3 fourth-round review-fix4 evidence (2026-08-09)

The fourth independent M3 review returned `BLOCKING` against base
`9a7bf4a43efd313e5dcb528361dfbfbfec1c37f0`; the sole remaining finding was
multi-request SDK retry reservation identity. The approved §6 scope, V10
legacy boundary, and all previously closed review findings were preserved.
The production reproduction first received a minimal RED: after request one
settled and request two failed with a transport retry, the outer retry replayed
request one with the settled reservation and raised
`V11RuntimeContractError("model reservation was already settled")` before the
provider retry request.

| Finding | RED → GREEN evidence | Shared-boundary closure |
| --- | --- | --- |
| B1 | `test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request` (RED, GREEN); `test_v11_sdk_replay_cursor_rehydrates_and_repeated_replay_is_idempotent` (RED, GREEN) | Existing durable `RuntimeEvent`/`RuntimeWriter` MODEL lifecycle events now persist `request_index`. PhaseInput rehydrates ordered per-logical-call request history; completed requests receive deterministic fresh `replay-N` reservations, an active failed request reuses its original identity, and later retry allocations can be durably deferred and restored. The production regression proves four provider calls, a new request-one reservation, request-two reuse, actual usage/remaining budget, and zero active reservations; the recovery regression proves released-reservation non-reuse and repeated replay idempotence. |

Final fourth-round verification: T7 exact `88 passed`; T8 exact `204 passed`;
M2R-2/M2R-3 and isolation focused `84 passed, 3 skipped`; changed/runtime
focused `102 passed, 2 skipped`; full `uv run pytest -q` `1992 passed, 3
skipped, 1 warning`; both `uv run ruff check .` and `uv run ruff check backend
tests` clean; diff-check clean. The warning is the existing Starlette/httpx
TestClient deprecation warning. The single `M3_REVIEW_FIX4_SHA` commit remains
isolated to `codex/v11-m3`; no merge, push, M4, or M5 action was taken.

### M3 fifth-round review-fix5 evidence (2026-08-09)

The fifth independent M3 review returned `CHANGES_REQUIRED` against base
`e52e47984c4b460ed3b06dc4147d3d3b0532345b`; the sole remaining finding was H1
usage and execution-audit aggregation, with Blocking/Medium/Low all zero. The
approved §6 scope, V10 legacy boundary, and fourth-round reservation replay
contract were preserved.

The production RED used `_call_model → Agents SDK Runner → RetryCoordinator`
with four provider calls: request one succeeded, request two failed with
transport, and both requests succeeded on the outer retry. The old path kept
only the final attempt's `40/10` in runtime counters and wrote only the last
failed request estimate into attempt one. The minimal GREEN fix uses the
existing per-request MODEL event callback: durable `input_tokens` remains the
reservation billed/estimate value, `actual_input_tokens` records provider
actual input, terminal request events are deduplicated by reservation/status/
attempt, successful actual usage accumulates across outer attempts, and
AgentExecution stores each attempt's successful actual usage plus the existing
failed-request estimate. The final RunResult is not counted again.

| Finding | RED → GREEN evidence | Shared-boundary closure |
| --- | --- | --- |
| H1 | `test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request` (RED `summary=40/10`, GREEN `summary=60/15`); `test_v11_single_sdk_request_usage_is_not_counted_twice`; `test_v11_all_failed_sdk_retry_records_estimate_without_summary_usage`; `test_v11_sdk_retry_resume_usage_is_idempotent` | Existing MODEL event persistence now carries the separate actual-input field. One logical call accumulator consumes each terminal request once, updates runtime counters only for successful actual usage, aggregates per-attempt audit usage before writing AgentExecution, and leaves failed estimates in attempt audit without presenting them as successful summary usage. Duplicate settlement/retry-resume leaves counters and reservations unchanged. |

Final fifth-round verification: T7 exact `91 passed`; T8 exact `207 passed`;
M2R-2/M2R-3 and isolation focused `84 passed, 3 skipped`; changed/runtime
focused `102 passed, 2 skipped`; full `uv run pytest -q` `1995 passed, 3
skipped, 1 warning`; both `uv run ruff check .` and `uv run ruff check backend
tests` clean; diff-check clean. The warning is the existing Starlette/httpx
TestClient deprecation warning. The single `M3_REVIEW_FIX5_SHA` commit remains
isolated to `codex/v11-m3`; no merge, push, M4, or M5 action was taken.

## 7. M4 — Product and compatibility integration

### M4 first-round review-fix evidence (2026-08-09)

M4 review fixes remain isolated on `codex/v11-m4`, based on the M3 checkpoint
`290f13c`. The ten independent-review findings were reproduced with focused
RED regressions and closed at shared boundaries without changing the approved
V10/V11 contract:

- B1: report generation now returns a `BusinessMutation`; report/actions and
  phase/checkpoint/lease state commit only through the shared `PhaseCommit`
  transaction, with memory and SQLite fault-injection rollback coverage.
- B2: configured product event/manual/runtime-create/rerun paths select V11;
  an explicit or legacy V10 run is rejected while an active V11 projection is
  owned by the investigation. Historical V10 remains available when the V11
  runtime is not configured, and legacy-to-V11 linked rerun behavior remains
  covered.
- H3: API, report, graph, and workbench paths use one V11 public projection
  for findings, candidates, Critic checks, Lead decisions, summaries, and
  graph labels; the frontend projection is a second defense.
- H4: report and V11 OpenRCA projection share usable-evidence validation for
  status and durable run owner; V10 legacy evidence validation remains
  unchanged.
- M5–M9: inconclusive output clears all candidate projections; human
  verification cannot replace persisted candidate refs; action planning checks
  the review/run/Lead status matrix; V11 artifacts require exactly one shared
  runtime owner; summary APIs preserve safe Critic/Lead fields.
- L10: V11 Markdown renders actual finding actors, task IDs, analysis rounds,
  evidence references, and the public Lead decision without placeholder actors
  or private reasoning.

The focused RED→GREEN regressions are in
`tests/runtime/test_v11_m4_audit_fixes.py` and the related report/API/OpenRCA
test modules. First-round local verification:

- T9 exact gate: `uv run pytest tests/reports tests/domain/test_action_models.py tests/api tests/frontend tests/benchmarks/test_openrca_runner.py tests/benchmarks/test_openrca_projection.py -q` → `276 passed, 1 warning`.
- T10 exact gate: `uv run pytest tests/runtime tests/safety tests/services tests/api tests/frontend tests/benchmarks/test_openrca_runner.py tests/benchmarks/test_openrca_projection.py -q` → `812 passed, 3 skipped, 1 warning`.
- Full gate: `uv run pytest -q` → `2025 passed, 3 skipped, 1 warning`.
- Offline gate: `uv run python -m backend.services.offline_tool_acceptance` → `rows=80 failed=0`.
- Runtime acceptance: `uv run python -m backend.services.runtime_acceptance` → exit 0.
- Static/build gates: `uv run ruff check .`, `uv run ruff check backend tests`, frontend production build, and `git diff --check` all passed. The only warnings are the existing Starlette/httpx TestClient deprecation and standard Vite `use client` bundle notices.

This is a local review-fix commit on `codex/v11-m4`; no merge or push was
performed. Same-thread independent re-review remains the next gate.

### M4 second-round review-fix evidence (2026-08-09)

The second independent review reproduced two further V11 publication defects
against the first-round M4 result. The fixes remain isolated on `codex/v11-m4`
and preserve V10 legacy semantics:

| Finding | RED → GREEN evidence | Shared-boundary closure |
| --- | --- | --- |
| H1 | `test_v11_prediction_artifact_uses_public_candidate_projection` failed with the private marker in both the `RootCauseAttribution` prediction and CSV; it now passes and asserts the V10 deterministic reason remains unchanged. | `project_v11_candidates` applies `public_v11_candidate` before constructing the V11 `RootCauseAttribution`, so the object and every downstream official/CSV prediction share the existing V11 public text projection. |
| M2 | `test_v11_projection_guard_rejects_invalid_reloaded_payload` (memory and SQLite), `test_v11_api_guard_rejects_inconsistent_review_run_and_missing_active_owner`, report regressions, and frontend payload regressions were RED before the fix and GREEN after it. | `validate_v11_final_status` is shared by action planning, report generation, and the V11 owner guard. The guard additionally requires a non-empty active owner, a matching latest Agent run summary, an Agent coordination review, and one durable runtime owner; invalid status/owner payloads fail closed. |

Focused evidence: OpenRCA projection `14 passed`; M4 audit-fix suite `32
passed`; report/frontend integration `17 passed`; the status matrix covers
complete, partial, and inconclusive legal combinations across memory and SQLite
reloads. Final gates are T9 exact `279 passed/1 warning`, T10 exact `830
passed/3 skipped/1 warning`, full pytest `2045 passed/3 skipped/1 warning`,
offline acceptance `80/80`, runtime acceptance `14/14`, `uv run ruff check .`
and `uv run ruff check backend tests`, frontend production build, and
`git diff --check` all green. The only test warning remains the existing
Starlette/httpx TestClient deprecation; Vite emits only its existing `use
client` bundle notices. No merge or push was performed.

### M4 third-round residual review-fix (2026-08-09)

The third independent review reproduced four residual V11 publication defects
and required RED regressions before implementation. `validate_v11_final_status`
now freezes the approved semantic matrix: complete↔completed,
partial↔partial, and inconclusive↔completed. Action planning, report
generation, and the public projection guard use the same contract; invalid
combinations cannot publish an action or diagnosis and V10 legacy paths are
unchanged.

The public guard also requires the durable `RuntimeRun.status` to be exactly
`completed`, treating created/failed/cancelled and lease-expired
`interrupted` runs as non-publishable. A shared report projection validator
binds diagnosis and alternative IDs to the final Lead candidate references,
including empty outputs for inconclusive reviews. API report/workbench/graph
routes, memory/SQLite reloads, and direct report projection tests cover stale
and foreign references. The frontend second guard now requires a non-empty
active owner and mirrors the status matrix.

Verification: M4 focused `75 passed/1 warning`; T9 exact `280 passed/1
warning`; T10 exact `855 passed/3 skipped/1 warning`; full pytest `2071
passed/3 skipped/1 warning`; offline acceptance `80/80`; runtime acceptance
`14/14`; both Ruff commands, frontend production build, and diff-check passed.
The post-commit runtime artifact and `git_dirty=false` binding are recorded in
the final implementation handoff. No merge or push was performed.

### M4 fourth-round high-fix evidence (2026-08-10)

The fourth independent review reproduced three production-path High findings
against the M4 checkpoint. Each regression was run before the corresponding
production change and then rerun GREEN on the isolated `codex/v11-m4` branch:

- H1: the real `OpenRcaDiagnosisRunner(mode="v11-agent")` failed Skill admission
  because the V11 contract fingerprint passed Skill objects where the admission
  contract requires the actual agent tool manifest. The runner now derives the
  Skill identity from `runtime.tool_registry.list_agent_specs()`. The real local
  SQLite fixture path starts and completes with a bounded offline model turn;
  admission remains enabled, the nine-tool manifest remains frozen, and the V10
  deterministic runner is untouched.
- H2: the V11 OpenRCA runner now calls the shared
  `ensure_v11_projection_owner` before constructing prediction objects or CSV
  rows. Durable `created`, `failed`, `cancelled`, and `interrupted` runs fail
  closed; a `completed` run publishes normally. The failed-run CSV regression
  proves the artifact contains an empty prediction plus a projection error,
  rather than a stale public cause. This guard is not used by the V10
  deterministic projector.
- H3: when an active V11 owner exists, the shared projection guard validates
  report authority, status, owner, and Lead candidate bindings for every report,
  including legacy-authority labels. V10-only records without an active V11
  owner continue through the historical deterministic path. Memory/SQLite
  reloads, direct report projection, API report, workbench, and coordination
  publication regressions cover both boundaries.

RED→GREEN evidence:

- real OpenRCA runner/admission/status/CSV regressions: `8 passed`;
- active-V11 legacy-report guard/API/workbench regressions: `4 passed`;
- focused source suite: `160 passed/1 warning`.

Final local gates: M4 focused `124 passed/1 warning`; T9 exact `288 passed/1
warning`; T10 exact `868 passed/3 skipped/1 warning`; full pytest `2084
passed/3 skipped/1 warning`; offline acceptance `80/80`; runtime acceptance
`14/14` with clean privacy output; both Ruff commands, frontend production
build, and `git diff --check` passed. The final post-commit runtime artifact
and `git_dirty=false` binding are recorded in the implementation handoff. No
merge or push was performed and M5 was not started.

### M4 fifth-round final High-fix evidence (2026-08-10)

The fifth independent review reproduced one remaining High publication bypass
against the fourth-round checkpoint. The RED regressions covered failed,
cancelled, running, and pending `InvestigationStatus` values with a completed
durable run in both memory and SQLite reloads, all three API publication paths,
and a real local OpenRCA V11 runner writing `v11-agent-predictions.csv`.

The shared domain status contract now accepts a V11 public projection only when
the InvestigationRecord is `completed`; it continues to require the legal
review/run/Lead matrix and durable `RuntimeRunStatus.COMPLETED`. The shared
guard is used by API, coordination-review, report, graph/workbench, and the
OpenRCA V11 runner, so failed/cancelled/running/pending investigations fail
closed before any public projection. The V11 benchmark writer also treats every
non-success outcome as non-publishable, emits `{}` in prediction/CSV, and stores
the safe `failure_category`; the V10 deterministic writer and legacy report
path remain unchanged.

RED→GREEN evidence:

- InvestigationStatus memory/SQLite guard plus API matrix: RED `12 failed`,
  GREEN `12 passed/1 warning`;
- real OpenRCA failed-investigation prediction/CSV: RED `1 failed`, GREEN
  `1 passed`;
- affected benchmark/projection suite: `45 passed`; full M4 focused suite:
  `137 passed/1 warning`.

Final pre-commit gates: T9 exact `289 passed/1 warning`; T10 exact `881
passed/3 skipped/1 warning`; full pytest `2097 passed/3 skipped/1 warning`;
offline acceptance `80/80`; both Ruff commands, frontend production build,
and `git diff --check` passed. Runtime acceptance is rerun after the final
commit so its artifact can bind the new SHA with `git_dirty=false`. No merge or
push was performed and M5 was not started.

### T9. Reports, actions, human transitions, API/UI, and OpenRCA

Requirements: R5–R12, R18–R19, R27.

Primary files:

- `backend/reports/generator.py`
- `backend/diagnosis/action_planner.py`
- `backend/domain/human_transitions.py`
- `backend/api/agent_views.py`
- `backend/api/investigations.py`
- `backend/benchmarks/openrca/runner.py`
- `backend/benchmarks/openrca/projection.py`
- `frontend/src/api.ts`
- `frontend/src/App.tsx`
- `frontend/src/RuntimeWorkbench.tsx`
- `frontend/src/runtimeProjection.ts`
- `frontend/src/OpenRcaBenchmark.tsx`
- corresponding report/domain/API/frontend/OpenRCA tests

Steps:

1. Dispatch report generation by execution contract. V11 renders accepted
   diagnoses, alternatives, Critic, gaps, evidence/counterevidence, status,
   authority, and usage with empty hypotheses; legacy rendering remains intact.
2. Generate only evidence-supported read-only recommendations from accepted
   candidate entity/mechanism. Bind action/verification/candidate/run ownership;
   never fall back to the CauseType planner.
3. Make human transitions validate active run, candidate, action/verification,
   and evidence ownership. Preserve legacy cause links and approval semantics.
4. Extend summary, graph, workbench, API, and TypeScript models additively. Keep
   legacy `top_cause_type`; V11 returns `unknown` when no legacy projection exists.
5. Render authority, diagnostic status, actor/task/round, Critic checks, evidence,
   alternatives, gaps, usage, failures, and `not_activated`; expose no raw prompt
   or private reasoning.
6. Add OpenRCA `v11-agent` mode that creates a V11 RuntimeRun and consumes the
   generic diagnosis. Keep deterministic runner/projector historical and never
   add benchmark-specific production prompts, taxonomy, aliases, or routes.
7. Prove the currently implemented service/API/CLI/OpenRCA adapters cannot return
   Agent authority without a persisted matching run/contract hash. RCAEval joins
   this gate in T11 when its runner entry exists.

Focused verification:

```powershell
uv run pytest tests/reports tests/domain/test_action_models.py tests/api tests/frontend tests/benchmarks/test_openrca_runner.py tests/benchmarks/test_openrca_projection.py -q
Push-Location frontend; npm run build; Pop-Location
uv run ruff check backend/reports backend/diagnosis/action_planner.py backend/domain/human_transitions.py backend/api backend/benchmarks/openrca tests/reports tests/api tests/benchmarks
```

Acceptance:

- V11 is candidate-led everywhere; legacy rows render without synthesis.
- Action/verification updates cannot target a prior run after rerun activation.
- OpenRCA compatibility never modifies V11 production reasoning.

### T10. Failure, recovery, safety, privacy, and local acceptance

Requirements: R8–R13, R16, R18, R21–R23, R26–R27.

Primary files:

- `backend/services/runtime_acceptance.py`
- `backend/services/production_acceptance.py`
- `backend/services/v7_live_acceptance.py`
- `backend/services/offline_tool_acceptance.py`
- `backend/services/tempo_acceptance.py`
- model capability service and related runtime/safety/API tests

Steps:

1. Add V11 acceptance scenarios for complete, partial, inconclusive, Lead/Critic
   failure, invalid reference, timeout, cancel, resume, transient retry, prompt
   injection, projection mismatch, and allowed legacy fallback labeling.
2. Prove cancellation/deadline terminalizes pending model/tool/task executions,
   cancels siblings, releases leases, blocks late commits, and persists safe
   terminal events/checkpoints.
3. Run forbidden-call and all-entry suites against complete/partial/inconclusive
   V11 flows, sequential rerun, legacy-linked rerun, and pre-INTAKE termination.
4. Scan database payloads, events, traces, reports, APIs, benchmark artifacts, and
   frontend fixtures for secrets, prompts, reasoning, raw provider data, labels,
   source IDs, and control-plane tokens.
5. Execute credential-free offline nine-tool acceptance and the fake compatible
   endpoint matrix. Execute Tempo only when its preflight passes; otherwise
   retain an explicit blocked integration result without blocking offline gates.
6. Run V3–V7 migration, legacy golden, runtime/recovery, production read-only,
   privacy, OpenRCA dual-mode, API, and frontend compatibility suites.

Focused verification:

```powershell
uv run pytest tests/runtime tests/safety tests/services tests/api tests/frontend tests/benchmarks/test_openrca_runner.py tests/benchmarks/test_openrca_projection.py -q
uv run python -m backend.services.offline_tool_acceptance
uv run python -m backend.services.runtime_acceptance
uv run ruff check backend tests
```

Acceptance:

- Runtime/reference/ownership/privacy/read-only contracts pass with zero leakage
  and zero forbidden V11 legacy calls.
- Status, failure category, replay, diff, and UI match every approved matrix.
- Offline acceptance is complete; Tempo support is claimed only after its gate.

### M4 review

Review the full public path, generated artifacts, action safety, latest/frozen
ownership, fallback labels, privacy scans, frontend compatibility, and OpenRCA
separation. Close blocking/high findings before controlled evaluation.

## 8. M5 — Controlled evaluation

### T11. Frozen single-Agent control, RCAEval runner, evaluator, and audit

Requirements: R14–R17, R24–R27.

Primary files:

- `backend/benchmarks/rcaeval/models.py`
- `backend/benchmarks/rcaeval/runner.py` (new)
- `backend/benchmarks/rcaeval/evaluator.py` (new)
- `backend/benchmarks/rcaeval/audit.py` (new)
- `backend/benchmarks/rcaeval/__main__.py`
- corresponding benchmark tests

Steps:

1. Implement the frozen one-context `SingleInvestigatorAgent` control with the
   same model tuple, offline evidence, nine-tool/skill catalogs, output schema,
   validator, deadline, tool cap, retry, and safety contract; it has no subagent,
   Critic, deterministic candidate, or hidden extra call.
2. Use the same Lead planning-action schema on both sides. Persist skill
   selection before the first tool call, validate it against the same frozen
   catalog, and charge the normal model turn and tokens; neither side gets a free
   selection call.
3. Start both sides from the same empty run-owned repository/memory snapshot.
   Formal packages cannot import verified incidents or any historical answer;
   test that `lookup_memory` sees the same explicit empty result on both sides.
4. Define four SS30 configurations: intended single `B`, intended Multi ≤`3B`,
   equal-token single `3B`, equal-token Multi `3B`. Tool/turn/deadline contracts
   are otherwise identical and materialized before execution.
5. Freeze/hash endpoint capability, provider/prompt/tool/skill/schema/normalizer/
   code identities, budgets, retries, manifests, predictions, and policy.
6. Implement exact service+mechanism Top1 plus approved supporting metrics,
   completion/reference integrity, latency, tokens/tools/cost, paired bootstrap
   (`Random(20260802)`, 10,000, frozen indices), and McNemar exact via stdlib.
7. Export candidate/evidence pairs before labels open. Validate the frozen
   four-part single-reviewer rubric, reviewer ID, uniqueness, completeness, and
   immutable audit hash; use no model judge.
8. Make RCAEval create/reload a persisted V11 RuntimeRun and verify its contract
   hash before any Agent-authoritative output. Add it to the all-entry and
   forbidden-call sentinel suites created in T3/T9.
9. Reject label-visible predictions, mixed identities, missing/duplicate cases,
   post-freeze changes, non-finite metrics, fallback Agent claims, or evaluator
   attempts to invoke runtime.

Focused verification:

```powershell
uv run pytest tests/benchmarks/test_rcaeval_prepare.py tests/benchmarks/test_rcaeval_isolation.py tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_evaluator.py tests/benchmarks/test_rcaeval_audit.py -q
uv run ruff check backend/benchmarks/rcaeval tests/benchmarks
```

Acceptance:

- Known-answer fixtures reproduce exact metrics/statistics.
- Prediction and evaluation process boundaries are mechanically enforced.
- Intended/equal-token configurations and capability identities cannot mix.

### T12. OB30 development, SS30 sealed validation, and final freeze

Requirements: R14–R17, R25–R26.

Steps:

1. Run OB30 unscored for functional debugging and bounded prompt/schema changes.
   Production prompts may not mention dataset/system/fault/case labels or answers.
2. Freeze candidate source/config and run all four SS30 configurations with one
   capability-certified endpoint. Freeze predictions and evidence audit before
   the evaluator process receives the SS label path.
3. Evaluate SS30 locally in the isolated evaluator process. Publish intended and
   equal-token results, paired uncertainty, token ratio, evidence audit, latency,
   read-only, leakage, and failure counts. Do not tune on individual sealed cases.
4. Freeze `acceptance-policy.json` with the approved formula and all hard gates;
   hash source, dependencies, endpoint capability, prompts, tools, skills,
   schemas, normalizer, budgets, TT90 manifest, and evaluator.
5. Run full repository/product gates and an independent source/artifact review.
   If no clean immutable source commit exists, request explicit commit authority
   and stop before TT90.

Acceptance:

- Four SS30 runs are complete, identity-matched, and evaluated only after freeze.
- Intended Multi tokens are ≤3x single and equal-token ablation is published.
- No blocking/high review finding remains; final policy/source bundle is sealed.

### T13. One TT90 paired execution, final reconciliation, and claims

Requirements: R1–R27 final evidence.

Steps:

1. On the exact frozen clean source, run only intended-budget single `B` and
   Multi ≤`3B` over TT90. Both use identical case/model/provider/tool/skill/
   schema/normalizer identities and freeze 90 predictions before labels open.
2. Before labels, verify row counts/hashes, runtime reference integrity, evidence
   audit completeness, privacy, read-only, token ratio, P95, no fallback claim,
   and no label-input path. A non-resumable infrastructure failure invalidates
   both sides; archive it and require user approval before any fresh paired rerun.
3. Open TT90 labels read-only in the evaluator process exactly once, run the
   frozen policy, and archive pass or fail without tuning or reusing the partition.
4. Reconcile Spec R1–R27 evidence, Plan status, `current.md`, README/API docs,
   benchmark artifacts, limitations, and review ledger. Claim effectiveness only
   if every frozen gate passes; otherwise state the measured failure.
5. Run final verification and independent diff/artifact review on the evaluated
   source identity.

Final verification:

```powershell
uv run ruff check .
uv run pytest -q
Push-Location frontend; npm run build; Pop-Location
uv run python -m backend.services.offline_tool_acceptance
uv run python -m backend.services.runtime_acceptance
uv run python -m backend.services.production_acceptance
# Required only when publishing the Tempo support claim:
uv run python -m backend.services.tempo_acceptance
git diff --check
```

Final acceptance:

- Multi exact Top1 meets `max(0.60, SS intended Multi exact - 0.10)` and exceeds
  intended single by at least 0.10.
- Runtime reference integrity is 100%; single-reviewer evidence-support rubric
  pass rate ≥95%; P95 ≤120s; Multi tokens ≤3x; read-only/leakage violations 0.
- `inconclusive`, failure, cancellation, timeout, and fallback count wrong.
- A failed accuracy gate preserves engineering evidence but forbids an accuracy
  improvement claim.

## 9. Dependency and stop gates

```text
T1 -> M0 review
   -> T2 -> T3 -> M1 review
   -> T4 -> T5
   -> T6 -> M2 review
   -> T7 -> T8 -> M3 review
   -> T9 -> T10 -> M4 review
   -> T11 -> T12 -> source/policy freeze
   -> T13 -> final review
```

T5 may be implemented while Docker is unavailable, but its integration execution
remains explicitly blocked until preflight passes. It never blocks T4 offline
acceptance or formal offline scoring.

Stop and update `current.md` when:

- local package/label separation or deterministic partitioning fails;
- a supported database cannot migrate transactionally to Schema V7;
- activation/freeze can expose a mixed or wrong-owner projection;
- a V11 entry or handler reaches forbidden legacy diagnostic code;
- read-only, evidence ownership, Critic, privacy, budget, deadline, cancellation,
  replay, endpoint identity, or capability certification fails;
- prediction sees labels before hash freeze or paired identities differ;
- final source requires Git authority that has not been granted.

Do not mark the iteration blocked merely because work is incomplete, Docker is
temporarily unavailable, or a score is unchanged. Correct the failed contract or
request only the missing authority.

## 10. Task and review ledger

| Task | Status | Requirements | Evidence |
| --- | --- | --- | --- |
| T1 | done | R15, R17, R25 | 代码侧：M0 review 三轮后 approve；真实 artifact 全链路（2026-08-04）：归一化 270 cases、双向逐文件对账 2700/2700、runtime manifest `bb119fc9fe338f7cf2d6f03a82f87a0038a1a589fde0c96ad99b02504a5f3d73`（150 cases/26 GB）、label manifest `06413003c877177e01d907106ad1181631d16a568b26a4dea86e52d73108a05d`。M0-L1/L2 已在 T11 freeze 前以 frozen golden vector 与 symlink checksum rejection RED→GREEN 关闭。 |
| T2 | implemented / verified | R2, R4–R6, R9–R10, R20, R27 | RED→GREEN domain/migration/persistence tests; `198 passed in 8.07s`; scoped Ruff clean; full requirement acceptance remains subject to later M3–M5 tasks and M1 independent review |
| T3 | implemented / verified | R7–R10, R13, R18, R20, R27 | RED→GREEN runtime isolation/recovery tests; `391 passed, 1 skipped, 1 warning in 160.40s`; scoped Ruff clean; full requirement acceptance remains subject to later M3–M5 tasks and M1 independent review |
| T4 | implemented / verified | R3, R11, R21–R22, R24, R27 | 336 focused passed; offline acceptance rows=80, failed=0; nine-tool/data-only skill evidence recorded; independent M2 review approve_with_followups |
| T5 | implemented / verified (live Docker blocked) | R21–R23 | 17 focused passed; File/Tempo parity contracts green; Docker gate explicitly blocked by unavailable daemon, retained as a host-dependent follow-up |
| T6 | implemented / verified | R8, R11–R13, R26–R27 | 110 focused passed including M2R-1 slow-endpoint cancellation/no-late-commit/cleanup evidence; independent M2 review approve_with_followups |
| T7 | implemented / verifying | R1–R3, R5–R6, R11–R13, R24, R27 | M3 fifth-round review-fix5 H1 usage/audit RED→GREEN; exact gate 91 passed; scoped Ruff clean; M2R-2 resolver wiring and M2R-3 invocation recheck remain closed; reservation replay and actual-vs-billed request usage are covered |
| T8 | implemented / verifying | R1, R4–R7, R9, R11–R13, R27 | M3 fifth-round review-fix5 preserves the already-green exact T8 validator, terminal, safe-text, supplemental-linkage, and final-actor contracts; exact gate 207 passed and scoped Ruff clean |
| T9 | implemented / verified | R5–R12, R18–R19, R27 | M4 first/second/third-round review fixes reproduced and closed with focused RED→GREEN regressions; exact gate 280 passed/1 warning; candidate-led V11 reports/actions, active-owner/rerun fencing, additive API/UI, shared privacy-safe public projection, usable-evidence validation, actual Markdown actors/tasks/rounds, generic OpenRCA `v11-agent` projection, semantic status matrix, durable lifecycle guard, and Lead-bound report references verified |
| T10 | implemented / verified | R8–R13, R16, R18, R21–R23, R26–R27 | Exact gate 855 passed/3 skipped/1 warning; full pytest 2071 passed/3 skipped/1 warning; offline rows=80 failed=0; runtime acceptance exit=0; Ruff/build/diff-check clean |
| T11 | implemented / verifying | R14–R17, R24–R27 | 首轮/二轮 review-fix 与第三轮新增 contract regressions 均按 RED→GREEN 修复；132 focused review-fix tests GREEN。冻结 Single one-context、真实 V11/SQLite accounting、四配置/空 memory、exact cardinality/statistics、scorer/capability identity、canonical custodian manifest/ledger、external-cwd worker、frozen-bundle audit reconstruction、sealed RuntimeRun persistence 与 lease/invalidation guards；M0-L1 golden vector 与 M0-L2 symlink rejection closed。独立复审尚未重新 approve。 |
| T12 | blocked at capability admission | R14–R17, R25–R26 | 真实 runtime package 完整 checksum 复核通过：manifest `bb119fc9fe338f7cf2d6f03a82f87a0038a1a589fde0c96ad99b02504a5f3d73`/150 cases；但本 worktree capability artifact=0，`DIAGOPS_AGENTS_API_KEY/BASE_URL/MODEL` 均 absent。未执行 OB30 model run 或 SS30 四配置，未打开 SS labels，未冻结 acceptance policy。任务线程 Luna Max 配置不是 benchmark endpoint certification。 |
| T13 | not started / protected | R1–R27 | TT90 predictions=0、formal label opens=0、paired attempts=0；未消耗 holdout。最新 clean code `094a20f2cd22066a00c32adead51b0e7b8489a63` 的仓库 gate：full pytest 2151 passed/3 skipped/1 warning，Ruff/frontend/offline/runtime/privacy/diff-check clean；production preflight 因缺少 `DIAGOPS_PRODUCTION_SCENARIO` 及 endpoint/credential 环境变量停止，未生成 artifact；Tempo claim 未发布/未运行。必须在同一 clean frozen source、passed capability identity 和已冻结 SS policy 下恢复；任何正式非可恢复失败仍按实现归档并停止。 |

Independent Plan review: completed on 2026-08-02; initial review found three
high and two medium issues. That plan review does not substitute for the
independent M5 implementation review; M5 review status remains tracked below.

| ID | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| H1 | high | Nine-tool acceptance could pass when every invocation was empty or failed | Closed in T4 with a hashed per-tool matrix requiring one non-empty success per tool plus separate failure/empty rows |
| H2 | high | Single-Agent control omitted equal skill-selection schema/timing/cost and formal empty memory | Closed in T11 with shared planning action, pre-tool persisted selection, normal turn/token charging, and identical empty run-owned memory |
| H3 | high | T9 required an RCAEval entry gate before the runner existed | Closed by limiting T9 to existing entries and adding RCAEval run/hash/forbidden-call gates in T11 |
| M1 | medium | Same-account label isolation wording implied an OS sandbox that the mechanism did not provide | Closed in §1/T1 by freezing the trusted-input threat model, supported-input/path-traversal denial, and bounded claim language |
| M2 | medium | Lifecycle status updates were missing at implementation start and milestone reviews | Closed in §2 with ready/implementing transitions and M0–M5 Plan/Spec/current evidence updates |
| M5-E1 | external blocker | 正式 SS30/TT90 需要一个 exact passed capability artifact、可调用 endpoint/model 和 process-only credential；当前三者均不存在 | Open；prediction 在首个模型调用前 fail closed。不得以 Codex task 的 Luna Max 配置、FakeRuntime 或测试 double 替代；TT90 保持未消耗，等待外部 owner 提供认证端点后恢复。 |
| M5-R1 | review pending | T11–T13 source/artifact independent review 尚未执行 | Open；首轮 M5 review（2026-08-10）为 `changes_required`，本实现线程已按 RED→GREEN 修复并记录 focused evidence，不自行给出 approve。父任务应基于最终本地提交创建独立 re-review 线程。 |

M5 review-fix record (2026-08-11): the second independent review reproduced
three residual findings: pair-ledger crash recovery, stale scorer identity at
acceptance, and invocation-local model-turn ceilings. They are fixed locally
with RED→GREEN tests: custodian lease expiry atomically invalidates the pair
before reauthorization; policy freeze and final acceptance verify the current
scorer dependency closure; and V11 model turns are atomically persisted at the
RuntimeRun boundary and carried through checkpoint/resume/retry. Formal
SS30/TT90 predictions, attempts, and labels remain untouched; T12 remains
blocked on a fresh passed capability artifact/endpoint/credential and a new
independent re-review.
Repository verification after the fixes: full pytest `2141 passed/3 skipped/1
warning`, full Ruff clean, frontend build passed, offline acceptance `80/80`,
runtime acceptance `14/14` with privacy scan, and diff-check clean. Production
acceptance was environment-blocked before any scenario artifact; Tempo was not
run because no Tempo claim is published.

M5 third-round implementation/review-fix record (2026-08-12): base
`977a040a1a8689fbf5b53b7266113348acdb268c`, code head
`094a20f2cd22066a00c32adead51b0e7b8489a63`. The canonical custodian-root
manifest now maps every output/pair root to one durable ledger and rejects
copied/symlinked/alternate roots; prediction children use an explicit source
entrypoint/Git root from external cwd; reveal state is permanent and
reauthorization binds a new identity without ABA/replay; audit validation
rebuilds candidate/evidence pairs from exact frozen bundles; persisted V11 runs
reject missing/null remaining turns; completion failures retry then invalidate
the pair; and lease token arguments have no optional default. Formal SS30/TT90
predictions, attempts, and label opens remain zero. This plan is
`review_required` pending independent re-review; T12 capability admission is
still the actual execution blocker.

M0 review (dataset/leakage): completed on 2026-08-02 in worktree
`agent+v11-m0`; conclusion `approve_with_followups`. Reviewer independently
reran the focused and regression suites (39/126 passed, Ruff clean) and probed
the isolation boundaries with adversarial vectors. M0 exit is NOT met: T2
remains blocked until H1 is closed and real RCAEval RE2 opaque runtime
manifests exist (the real artifact is not available locally; only synthetic
fixtures have been prepared).

M0 focused re-review (2026-08-03): H1/M1/M2 fixes verified with reruns and new
adversarial variants; two residual items (repeated-separator module token,
bare `..` argv) fixed and confirmed closed in a final pass (49 focused + 136
regression passed, Ruff clean). Final code-side conclusion: `approve`, no
remaining blocking/high/medium/low. The real-artifact exit gap is unchanged
and still blocks T2.

M0 exit review (dataset/leakage on the real artifact, 2026-08-04): independent
reviewer re-derived the seeded selection from pin + source case.json (OB30/SS30/TT90
exact match to labels.json; 150 opaque IDs recomputed), ran full-package byte scans
(zero source-case-ID hits across all 1561 runtime files; zero answer metadata keys;
zero file overlap with the label package), re-verified both manifests' hashes and the
26 GB runtime SHA256SUMS (1561/1561 OK), re-checked archive_sha256, pin file-set
identity (2970/2970), and the bidirectional semantics of `verify_against_raw.py`.
Conclusion: `approve_with_followups` — M0 exit criterion ("real opaque runtime
manifests exist and labels remain outside the prediction process") is met; T2 is
unblocked. Followups: one low (M0-I2 wording materialized in a test assertion over
the whole-package blob — fixed same day by scoping service/fault assertions to
manifest bytes + file names, 28 focused + 142 benchmarks regression passed, Ruff
clean; reviewer's duplicate-content and `"service"`-token measurements folded into
the M0-I2 closure evidence); M0-L1/L2 were closed in T11 before any formal M5 freeze.

| ID | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| M0-H1 | high | Prediction locator scanning matches unnormalized substrings and argv has no traversal check; relative `..\..`, drive-letter case, and forward-slash variants pass on Windows | Closed on 2026-08-03: dual-layer raw+normalized comparison (`resolve`+`normcase`) and argv traversal rejection in `isolation.py`; re-review verified original and new variants all rejected |
| M0-M1 | medium | Evaluator forbidden tokens match only dotted module names; slash-path references to production runtime pass | Closed on 2026-08-03: `_module_token_in` matches dotted and slashed forms with repeated-separator folding; residual `backend//runtime//coordinator.py` bypass fixed and confirmed |
| M0-M2 | medium | Evaluator launcher does not cross-check `LabelManifest.runtime_manifest_hash`, so swapped label/runtime package pairs are accepted | Closed on 2026-08-03: required `expected_runtime_manifest_hash` parameter with format and pairing validation |
| M0-I1 | informational | Bare `..` argv element escaped both path-form and traversal checks | Closed on 2026-08-03: explicit bare `..` rejection in `_reject_argv_traversal`; benign `wait..please` control still passes |
| M0-L1 | low | Selector test mirrors the formula without a frozen golden vector | closed in T11：独立 frozen OB30/SS30 selection vector fixture 与 exact match regression |
| M0-L2 | low | Package checksum verification does not reject symlink entries | closed in T11：checksum traversal rejects symlink entries；Windows 无权限环境以边界 mock 验证同一分支 |
| M0-L3 | low | AST no-runtime-import scan covers direct imports only | recorded |
| M0-I1 | informational | Worktree base `7b26024` predates V10.1 archive commits; final TT90 freeze must rebase onto the approved baseline | Closed on 2026-08-04: 分支 `worktree-agent+v11-m0` 已 rebase 到 main `58c6382`（V10.1 归档提交）之上，唯一冲突 current.md 按"V11 active + V10.1 archived"语义解决；M0 以单个提交落在该基线上，后续 freeze 不再背负基线债 |
| M0-I2 | informational | "Runtime package contains no service/fault strings" holds only on synthetic fixtures; reword after real prepare | Closed on 2026-08-04: M0 exit review 实测确认真实遥测正文合法包含全部 15 个服务名（抽样 3 case CSV 头部 15/15）与通用 fault 词元；泄漏判据限定为答案元数据（源 case ID、manifest/文件名字段的标签值）。`test_runtime_package_has_no_labels_or_source_ids` 的 service/fault 断言已从全包 blob 收缩到 manifest 字节 + 文件名（源 case ID 仍对全包字节断言，真实包 1561 文件 0 命中）；spec §11.1/prepare.py/models.py 的字段级表述经评审确认无需修改；28 focused + 142 regression passed。附带实测记录：51 组字节相同遥测（均为 telemetry-05/06.csv 小文件、无一跨分区，内容不随答案变化，非泄漏）；2 处带引号 `"service"` 命中定位于单个遥测 CSV 正文（内容非字段，非泄漏） |
| M0-I3 | informational | Telemetry-body case-ID guard is exact case-sensitive match; must run `inspect` against the real artifact first to rule out false rejects | Closed on 2026-08-02: real-artifact full-tree scan found zero case-ID body hits outside each case's own `case.json`（prepare 不校验 case.json），误拒风险在真实数据上不成立 |
| M0-R1 | high | 真实 RE2 `metrics.csv`/`simple_metrics.csv` 的 istio-latency 分位列在无有效样本时写出字面量 `NaN`（238 文件 / 119 case：TT 90/90、SS 21/90、OB 8/90；`inf/infinity` 词形与 JSON 非常量 0 命中），旧"拒绝非有限遥测"合同下 prepare 不可能成功 | Closed on 2026-08-04：2026-08-02 修正案（用户批准）TDD 落地（red 3 acceptance → green 55 focused + 142 regression + ruff clean）；真实 prepare 成功产出完整 runtime/标签包（manifest 哈希与对账证据见 T1 证据行） |
| M0-R2 | high | `_reject_non_finite_csv` 对每个单元格 float 试探：16 位十六进制 trace/span ID（如 `96e3653877800818`，形如 `[0-9]+e[0-9]+`）被按科学计数法解析溢出为 inf，OB+TT 全部 180 个 `traces.csv` 被误拒——M0-I3 同类"正文误拒"在标识符列上成立 | Closed on 2026-08-04：同一修正案落地（CSV 只拒绝显式 inf 系词元，不再 float 试探）；标识符列放行经真实数据验证——二次 prepare 的 `diff -r` 零差异证明 180 个 `traces.csv` 全部正常通过并字节等价 |
| M0-R3 | medium | 真实 `traces.csv` 最大 233,353,404 B（110 文件超 100 MB），撞破 `MAX_TELEMETRY_BYTES` 64 MiB 临时上限；该上限在 M0 review 偏差裁决中即标注 "provisional pending real-artifact reconciliation" | Closed on 2026-08-02：真实 artifact 对账后上调至 256 MiB，代码注释记录实测最大值与理由；用户已于 2026-08-02 追认 |
| M0-I4 | informational | 归一化树 `D:\data\RCAEval\source-normalized`（270 cases / 2970 文件 / 32 GB，确定性生成）与 custodian pin（SHA `b064b85816d28216ae4d5a562c0f949650556e0c67d19763a77783c212ea8462`，archive hash `10863f25ea70416de0b05613376e821a78a931f8edfc9dbdea9e9626ded0b64e`）；工具脚本 `normalize_re2.py` SHA `fb00d9d2be8af09d19a203b0ef5beb2beb2010cfdf28ddf30f255061bd465eaf`、`make_pin.py` SHA `911c874a258f1431240edf30b6864270cded965e52339c4acba3d1fd7f995eaf`；upstream_revision `1f88b632…` 为用户 2024-12-21 下载快照时点前的最近上游提交，字节级等价性为 user-attested，强身份是 archive hash | recorded（归一化合同、数据出处、工具外置三项均经用户 2026-08-02 确认）；2026-08-04 升级：`verify_against_raw.py`（SHA `88fa5ab266773352d531d056ed8346a41a5fd666220c9f2187e9f2820ce33e05`）对归一化树与用户原始下载快照做双向逐文件 SHA-256 对账，2700/2700 全匹配——字节级等价性由 user-attested 升级为 byte-verified；对账完成后原始快照 `dataset/` 与中间归档 `archives/` 经用户点名确认删除（回收约 38 GB，当前 D: 38G 可用），`source-normalized` + pin 即唯一权威副本且其完整性由 pin 逐文件哈希钉住 |

Deviation rulings from the M0 review: normalized source layout contract
accepted pending user confirmation (normalization step identity should join
the M5 freeze scope); telemetry-body case-ID guard accepted as fail-closed
hardening conditional on real-artifact validation; telemetry suffix allowlist
`{.csv,.json,.log}` plus 64 MiB cap accepted as provisional pending real-artifact
reconciliation.

M1 third-round review (migration/runtime, 2026-08-07): conclusion
`approve_with_followups`.

| ID | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| RR-H1 | high | T3 step 8 absolute deadline/budget monotonicity tests 被 T3 证据行隐含声称已测，实测零覆盖 | Closed on 2026-08-07: `tests/runtime/test_v11_deadline_budget.py` 6 项覆盖（TIMEOUT 终态、跨 attempt 绝对 deadline 锚定、investigation failed → OUTPUT_VALIDATION、timeout_seconds=121 拒绝、CANCELLING 收敛、单调性）；T3 证据声称已修正 |
| RR-M1 | medium | T4/T5 契约（evidence provenance/scope、memory verification、OPENAI_COMPATIBLE provider）提前落地 M1 且无测试 | Closed on 2026-08-07: `tests/domain/test_evidence_memory_contracts.py` 9 项；未改实现直接通过，确认契约正确仅缺覆盖 |
| RR-M2 | medium | `_repair_contract_integrity` 不持久化修正后的 execution_contract，读路径重复修复、V11 重复 freeze/reseal、业务投影被反复改写 | Closed on 2026-08-07: 同事务写回 `_safe_contract_projection`；RED（updated_at 漂移 + 契约列仍篡改）→ GREEN；既有 idempotence 测试语义不变 |
| RR-L2 | low | CANCELLING 的 V11 run 遇 deadline 到期滞留 CANCELLING 直至 lease 过期 | Closed on 2026-08-07: `_RuntimeDeadlineExceeded` 分支先 `_finish_cancel` 后 `_fail_if_owned`；RED（滞留 cancelling）→ GREEN（收敛 CANCELLED） |
| RR-L1 | low | `phase_executor._v11_intake` 双激活窗口（已核实可恢复） | Closed on 2026-08-07: handler 不再独立事务提前 activate，激活只在 commit_phase 事务内发生；`test_v11_intake_activates_projection_only_inside_commit_transaction`（memory+sqlite 参数化）先 RED 后 GREEN |
| RR-L3 | low | `sqlite_store._repair_contract_integrity` 跨模块调用 repository 私有 `_get_with_connection` | Closed on 2026-08-07: 新增公开 `get_with_connection` 并替换两处调用点；行为不变，全套 T2/T3 gate 回归 green |

## 11. Approval state

- Specification: approved by the user on 2026-08-02 after L22–L30 reuse review.
- Implementation Plan: independently reviewed with H1–H3/M1–M2 closed; approved
  by the user on 2026-08-02 with authorization to begin execution.
- Implementation: M0/T1 complete; M1/T2–T3 implemented and verified on 2026-08-05 in the delegated worktree; third-round independent migration/runtime review concluded `approve_with_followups` on 2026-08-07 with all six findings closed the same day (RR-H1/RR-M1/RR-M2/RR-L2 at commit `336067c`, RR-L1/RR-L3 in the follow-up Light fix); M2 snapshot committed as `M2_BASE_SHA=c2245ac`; M3 fifth-round review-fix5 remains isolated on `codex/v11-m3`; M4 second-round review-fix is RED→GREEN verified on isolated branch `codex/v11-m4`, with the local commit SHA recorded in the final handoff. No merge or push was performed; M5 review-fix remains isolated on `codex/v11-m5`, with formal SS30/TT90 protected and independent re-review pending.
