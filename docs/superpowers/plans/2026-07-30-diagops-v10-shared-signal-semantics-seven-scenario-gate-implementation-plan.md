# DiagOps V10 Shared Signal Semantics And Seven-Scenario Gate Implementation Plan

Status: `approved`

Date: 2026-07-30

Spec:
`docs/superpowers/specs/2026-07-30-diagops-v10-shared-signal-semantics-seven-scenario-gate-design.md`

## 1. Goal and execution boundary

先删除全仓审计确认的死代码，再用一个纯函数 Signal Core 替换 OpenRCA 与生产
Prometheus 路径中重复的 metric 分类、异常段、onset 和 Top-N 逻辑；删除失败
Projector 的第二套归因；把 production Gate 从四场景扩展到七场景；在 source
checkpoint 前完成剩余全仓整理，最后按同一可复现 source 依次运行 production、
六案例 targeted 和正式 40-case Gate。

本计划是 Full workflow，采用 inline、分 milestone 执行。W1–W4、W7 是正式清理
任务；W5/W6 是产品决策 Gate。不新增依赖、registry、DSL、factory、配置框架、
数据库迁移、生产写 Tool、Ground Truth 规则或模型权重。

用户未授权 Git commit、push、branch 或 PR。实现和 key-free 验证可执行；到 live
Gate source checkpoint 时必须停止并单独取得 Git 授权。

## 2. Frozen inputs and stop conditions

冻结开发输入：

```text
Safe index:
D:\data\OpenRCA\prepared-v10-dev-6case\runtime-cases.json

Expected SHA-256:
B249E2F6B0B0DBD3B9F30AA71EF3302FF2C48A05CC50B914FB3DCBBC8800AD4B

Dataset:
D:\data\OpenRCA\dataset

Official evaluator:
D:\data\OpenRCA\official-evaluator
```

通用 stop conditions：

1. 新证据改变已批准的 scope、兼容、安全或 Gate 阈值时，把 spec 和 plan 退回
   `review_required`。
2. 任一 milestone focused check 未通过时，只修当前共享根因，不进入下一
   milestone。
3. source identity 未建立时，不运行任何新 live Gate。
4. production `7/7` 未通过时，不运行 OpenRCA 六案例。
5. 六案例未达到 `>=3/6` 且 time/reason/component 各命中时，不运行 40-case。
6. 不通过调权重、扩大 Top-N、读取答案或新增 case rule 绕过失败。
7. W5/W6 没有书面 retire 决策时保留，不阻塞其余 V10；不得擅自改变公共或历史
   持久化合同。

## 3. File ownership

新增：

- `backend/diagnosis/signal_semantics.py`
- `tests/diagnosis/test_signal_semantics.py`

清理：

- 删除 `backend/diagnosis/context_store.py`、
  `tests/diagnosis/test_shared_context.py`
- 精简 `backend/domain/agent_context.py`、
  `backend/domain/agent_findings.py`、
  `backend/providers/base.py`、`frontend/src/api.ts`
- 精简 `tests/frontend/test_frontend_smoke.py` 和
  `tests/frontend/test_runtime_workbench.py`
- 精简 `README.md`、`docs/superpowers/current.md`
- 在正式 repository 已完整实现的方法上精简
  `backend/api/agent_views.py`、
  `backend/diagnosis/orchestrator.py`、
  `backend/diagnosis/execution_engine.py` 的 optional fallback

按任务修改：

- Signal adapters：
  `backend/benchmarks/openrca/providers.py`、
  `backend/providers/prometheus.py`、
  `backend/domain/tool_queries.py`
- Analyzer/Attribution/Projector：
  `backend/rca/analyzer.py`、
  `backend/diagnosis/coordination_review.py`、
  `backend/diagnosis/evidence_validation.py`、
  `backend/benchmarks/openrca/projection.py`、
  `backend/benchmarks/openrca/runner.py`、
  `backend/benchmarks/openrca/evaluator.py`
- Production Gate：
  `backend/services/production_lab.py`、
  `backend/services/production_acceptance.py`、
  `compose.production-gate.yaml`、
  `config/production-gate/rules.yml`、
  `config/production-gate/seed-prometheus.sh`、
  `scripts/run_production_gate.ps1`
- 对应 focused tests、`README.md`、本 spec、plan 和
  `docs/superpowers/current.md`

除非 focused failure 证明必要，不修改 Runtime 状态机、SQLite schema、Agent SDK
runtime、frontend UI 行为或公共 Investigation API。

## 4. M0 — Safe whole-repository deletion

### CL0. Delete W1/W2 dead code before adding Signal Core

覆盖：R15。

1. 用 `rg` 再确认所有生产 caller：
   - `SharedContextStore` 只被专属测试消费；
   - `SharedInvestigationContext`、`WorkbenchGraph*`、
     `QueryEvidenceProviderProtocol` 没有生产消费者；
   - `getAgentFindings`、`getCoordinationReview`、`getAgentConfig` frontend
     wrapper 没有 frontend caller。
2. 删除 `context_store.py` 和专属测试；从现有 domain/provider 文件删除上述死
   类型；删除只证明这些死类型可构造的测试；删除三个无用 frontend wrapper。
3. 不删除 `ContextFact`、repository context persistence、RCA workbench endpoint
   或 Provider 的实际 `supported_tools` contract。

Focused check：

```powershell
uv run pytest tests/domain/test_agent_models.py tests/domain/test_agent_findings.py tests/api/test_v4_agent_views_api.py tests/frontend/test_frontend_smoke.py -q
uv run ruff check backend/domain/agent_context.py backend/domain/agent_findings.py backend/providers/base.py tests/domain/test_agent_models.py tests/frontend/test_frontend_smoke.py
npm.cmd --prefix frontend run build
```

期望：caller scan 为零，focused tests 和 build 通过，净删除约 200 行（含测试；
其中生产代码约 100 行，与 spec §13 口径一致）且无替代 abstraction。

Milestone review：确认只删无调用 contract，没有借机改变 Investigation、Evidence
或 Provider 行为。

## 5. M1 — Implement the pure Signal Core

### T1. Signal taxonomy, segments, strength and balanced selection

覆盖：R1、R2、R3。

1. 先创建 `tests/diagnosis/test_signal_semantics.py`，覆盖：
   - container-network precedence；
   - dropped/error/loss/corrupt/retransmit 与普通 packet 的区别；
   - TCP wait、memory、CPU、disk、process；
   - median/MAD、zero baseline、非有限值和 `[0, 10]` clamp；
   - 单点 segment、正常点断段、采样 gap、onset/ended_at；
   - family-first round-robin、dedup、limit 和 repeatability。
2. 创建唯一新生产模块 `backend/diagnosis/signal_semantics.py`：
   - 最多 `SeriesPoint`、`AnomalySegment` 两个内部 frozen dataclass；
   - 只公开 `classify_metric_signal`、`detect_anomaly_segments`、
     `select_balanced_evidence` 三个纯函数；
   - helper 保持私有，不读文件、URL、PromQL、task、scenario 或 Ground Truth。
3. 不增加可调参数层。固定规则只来自已批准 spec。

Focused check：

```powershell
uv run pytest tests/diagnosis/test_signal_semantics.py -q
uv run ruff check backend/diagnosis/signal_semantics.py tests/diagnosis/test_signal_semantics.py
```

期望：全部通过；模块没有外部 I/O 或新增 dependency。

Milestone review：核对公共符号数量、有限数边界、稳定排序及无答案/数据集标识。

## 6. M2 — Route both telemetry adapters through Signal Core

### T2. OpenRCA Metric and Dependency adapters

覆盖：R1、R2、R3、R7、R11。

1. 在 `tests/benchmarks/test_openrca_providers.py` 增加公开 schema 驱动的 regression：
   - network packet drop 不再分类为 process；
   - Bank TCP wait 形成 `network_latency`；
   - Market memory family 不被高 raw deviation 挤出；
   - metric/dependency Evidence 使用 segment onset 和 bounded strength；
   - component hierarchy 保留 node/service/instance；
   - optional/坏 series 只降级当前输入。
2. 修改 `backend/benchmarks/openrca/providers.py`：
   - adapter 只解析 CSV/vendor schema 和 component hierarchy；
   - 调用 Signal Core；
   - 删除 C4、C5 的私有 taxonomy、threshold 和全局 raw-deviation Top-N；
   - Log Provider 保持现状。
3. 确认 Provider、Signal Core、Analyzer 不读取 `record.csv`、score、official report
   或 `task_index`。

Focused check：

```powershell
uv run pytest tests/benchmarks/test_openrca_providers.py tests/benchmarks/test_openrca_prepare.py -q
uv run ruff check backend/benchmarks/openrca/providers.py tests/benchmarks/test_openrca_providers.py
```

### T3. Production Prometheus range adapter

覆盖：R1、R2、R3、R5、R6、R8。

1. 在 `tests/domain/test_tool_queries.py` 和
   `tests/providers/test_prometheus_provider.py` 先覆盖：
   - additive network/process enum；`metric_names` 上限与 enum 基数同步（7）；
   - baseline + observation `query_range`；
   - step 使每 series 约不超过 240 点；
   - empty optional series 与 HTTP/schema/non-finite failure 分离；
   - range failure 保留 instant Evidence 时显式 `onset_unavailable`；
   - 旧 instant request 和默认 Provider 配置继续有效。
2. 修改 `backend/domain/tool_queries.py` 和
   `backend/providers/prometheus.py`：
   - 只增加 allowlisted network drop、process restart 查询，并把 `metric_names`
     上限提升到与 enum 基数一致（7）；
   - counter 先以 PromQL rate/increase 转换；
   - adapter 调用 Signal Core，输出 additive payload；
   - 不接受任意 PromQL。

Focused check：

```powershell
uv run pytest tests/domain/test_tool_queries.py tests/providers/test_prometheus_provider.py tests/tools/test_tool_registry.py -q
uv run ruff check backend/domain/tool_queries.py backend/providers/prometheus.py tests/domain/test_tool_queries.py tests/providers/test_prometheus_provider.py
```

Milestone review：确认 OpenRCA 和 Prometheus 不再各自拥有 taxonomy、segment 或
ranking；旧 API/config contract 不变。

## 7. M3 — Make authoritative RCA segment-aware and thin the Projector

### T4. Analyzer, Attribution and Projector deletion

覆盖：R4、R7、R9、R11。

1. 先扩展：
   - `tests/rca/test_analyzer.py`
   - `tests/diagnosis/test_coordination_review.py`
   - `tests/diagnosis/test_evidence_validation.py`
   - `tests/benchmarks/test_openrca_projection.py`
   - `tests/benchmarks/test_openrca_runner.py`
   - `tests/benchmarks/test_openrca_evaluator.py`
2. 验证：
   - network/resource/process cause、component、reason；
   - 有 segment 时不同 segment 不聚类，occurred_at 使用 onset；
   - 无 segment 的历史 Evidence 继续走 `_time_clusters()`；
   - Projector 只做 task 字段映射、Evidence reference 校验、被评分字段去重和
     authoritative Top-N；
   - authoritative root cause 不足时显式 fallback，targeted Gate 失败。
3. 修改 Analyzer/Attribution，只消费 canonical payload，不再解析 vendor metric
   名。
4. 删除 Projector C1–C3：
   - 不重建 attribution；
   - 不从 Evidence/Hypothesis 生成候选；
   - 不改时间、不按 specificity/provider diversity/deviation 重排；
   - audit 只保留 spec 规定的六个字段。
5. Runner/Evaluator 只接线和验证，不增加诊断算法。

Focused check：

```powershell
uv run pytest tests/rca/test_analyzer.py tests/diagnosis/test_coordination_review.py tests/diagnosis/test_evidence_validation.py tests/benchmarks/test_openrca_projection.py tests/benchmarks/test_openrca_runner.py tests/benchmarks/test_openrca_evaluator.py -q
uv run ruff check backend/rca/analyzer.py backend/diagnosis/coordination_review.py backend/diagnosis/evidence_validation.py backend/benchmarks/openrca/projection.py backend/benchmarks/openrca/runner.py backend/benchmarks/openrca/evaluator.py
```

Milestone review：先确认 C1–C5 已删除，再接受 Signal Core 新代码；检查 Ground
Truth isolation、历史 Evidence fallback 和有效引用。

## 8. M4 — Expand the production Gate to seven scenarios

### T5. Lab signals, schema V2 and answer-leak protection

覆盖：R6、R7、R10、R14。

1. 在 `tests/services/test_production_lab.py` 先增加：
   - `memory_pressure`、`network_corruption`、`process_failure` 状态；
   - fault response 返回 `activated_at`；
   - metrics 只含通用 service/environment/container label；
   - reset 恢复正常值；
   - gate disabled 时 fault endpoint 仍为 404。
2. 修改 lab、Prometheus seed 和 rules：
   - 使用 bounded gauge/counter，不真实耗尽内存、杀进程或修改网络；
   - alert label/annotation 不含 scenario、cause、reason 或 expected component；
   - 不授予 NET_ADMIN。
3. 在 `tests/services/test_production_acceptance.py` 先增加：
   - schema V1 四场景历史 artifact 可读；
   - schema V2 恰好七场景；
   - 新三场景校验 Top-1 cause/component/reason 和 onset `<=60s`；
   - 新三场景禁止 onset fallback；
   - unfavorable artifact 先落盘再失败；
   - persisted Event/Evidence/Review privacy scan 无答案泄漏。
4. 修改 `production_acceptance.py`：
   - 从 read-only Investigation/RCA workbench 获取 authoritative attribution；
   - `activated_at` 仅在 runner 内计算 onset error，不写入 DiagOps；
   - 保持 Evidence `100%`、read-only `0`、privacy 和 `<900s` Gate。
5. 把三个场景加入 `scripts/run_production_gate.ps1`，保持每场景独立 Compose
   project 和 finally cleanup。
6. README 只更新当前七场景命令、阈值和安全边界，不追加版本年表。

Focused check：

```powershell
uv run pytest tests/services/test_production_lab.py tests/services/test_production_acceptance.py tests/providers/test_prometheus_provider.py tests/api/test_events_api.py -q
uv run ruff check backend/services/production_lab.py backend/services/production_acceptance.py tests/services/test_production_lab.py tests/services/test_production_acceptance.py
docker compose -f compose.production-gate.yaml config
```

Milestone review：检查 fault control 不进入 Tool/默认生产镜像，Compose evidence
mount 只读，artifact extra fields 禁止，V1/V2 兼容和答案泄漏扫描完整。

## 9. M4.5 — Finish whole-repository cleanup before source identity

### CL1. Remove W3/W4/W7 implementation-shape debt

覆盖：R15。

1. Frontend tests：
   - 删除只读取 `.ts/.tsx/.css` 并断言字符串、函数名或固定代码形状的测试；
   - 保留实际执行 selector、candidate visibility、redaction 和未验证操作声明过滤
     的测试；
   - 把 `test_runtime_workbench.py` 缩成可执行的 runtime projection/selection
     行为测试；frontend production build 和 backend Runtime API tests 继续覆盖
     类型与接口。
2. Docs：
   - README 只保留当前安装、运行、Provider、安全、production Gate、OpenRCA 和
     Runtime 操作；
   - `current.md` 只保留 active routing、最新 Gate、冻结 artifact 索引；
   - V2–V9 详细年表继续由历史 specs/plans 保存，不删除历史文件。
3. Repository fallback：
   - 只对 InMemory/SQLite 两个正式 repository 均已实现的方法改为直接调用；
   - 同步最小 test double；
   - 缺方法必须尽早失败，不增加新 Protocol/factory；
   - 不碰 RuntimeStore 的多实现、故障注入或历史兼容 fallback。

Focused check：

```powershell
uv run pytest tests/api/test_v4_agent_views_api.py tests/diagnosis/test_execution_engine.py tests/diagnosis/test_orchestrator.py tests/frontend/test_frontend_smoke.py tests/frontend/test_runtime_workbench.py -q
uv run ruff check backend/api/agent_views.py backend/diagnosis/orchestrator.py backend/diagnosis/execution_engine.py tests
npm.cmd --prefix frontend run build
```

期望：保留行为/安全测试，删除 source-shape assertions 和无效 fallback；README、
current routing 无断链。

### CL2. Decide W5/W6 without silently deleting compatibility

覆盖：R16。

1. 生成明确 retain/retire 记录：
   - W5：`v7_live_acceptance.py` 是否继续承担 Provider/model certification；
   - W6：historical ReAct/LLM rows 和 read-only API 的保留期、迁移 owner。
2. 默认 disposition 是 `retained intentionally`，因为当前 capability/compatibility
   仍有消费者。
3. 若用户选择 retire，先把 spec 和 plan 退回 `review_required`，补充公共 API、
   artifact、SQLite migration/retention 处理和验证；不得在本任务里直接删除。

Milestone review：记录 CL0/CL1 实际净删除行数、CL2 disposition；确认无公共/持久化
contract 被无授权移除。

CL2 disposition record（2026-07-31，用户书面确认）：

- **W5 `backend/services/v7_live_acceptance.py`：`retained intentionally`**。
  它是 `output/reliability/` Provider/model certification artifact 的唯一生产者，
  `GET /config/agents` 的 `certification_status`（`backend/api/config.py`）与前端
  RCA 工作台的状态展示均由其结果支撑；DeepSeek 当前为"已实现未认证"，删除会永久
  关闭既有认证路径并使 `certification_status` 退化为死字段（公共 API 语义变更）。
  它与 V10 改动零耦合、全套件绿色，保留成本近零。
- **W6 historical ReAct/LLM read path：`retained intentionally`**。
  `domain/react_trace.py` 读模型、`react_traces`/`llm_analyses` 历史表、双
  repository `get_react_trace` 与只读端点 `/react-trace` 承担 AGENT.md 硬合同
  "supported historical records readable in memory and SQLite"；新流程不写入但
  历史 SQLite 行必须可读。删除收益仅约 200 行，且 DROP 表会引入 additive-only
  迁移惯例之外的全新风险类别。
- 保留期与迁移 owner：当前不设删除计划；未来若要 retire 任一项，必须先把 spec
  和 plan 退回 `review_required`，补充公共 API、artifact、SQLite
  migration/retention 处理和验证后重新批准。

## 10. M5 — Verify, establish source identity and run Gates

### T6. Key-free regression

覆盖：R1–R16。

```powershell
uv run ruff check .
uv run pytest -q
npm.cmd --prefix frontend run build
uv run python -m backend.services.runtime_acceptance
git diff --check
```

期望：Ruff、全量 pytest、frontend build、Runtime acceptance 14/14 和 privacy
scan 全部通过；没有新的 dependency、migration、public API 或写 Tool。

### T7. Source checkpoint

覆盖：R12。

1. 记录 `git status --short`、HEAD、official evaluator commit 和 frozen safe-index
   SHA-256。
2. milestone diff review 无 blocking finding 后，停止并请求用户单独授权 Git
   commit。
3. 只有 clean、可复现 commit 才能进入 live Gate；不得把 dirty HEAD 当 source
   identity。

### T8. Production seven-scenario Gate

覆盖：R10、R12、R13、R14。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_production_gate.ps1
```

期望：`7/7`，旧四场景不回归，新三场景 cause/component/reason 正确、onset
`<=60s`，Evidence `100%`、read-only `0`、privacy 通过、总耗时 `<900s`。

失败即记录 artifact/hash/source 并停止。

### T9. Frozen six-case targeted Gate

覆盖：R1–R6、R9、R11–R14。

```powershell
Get-FileHash -Algorithm SHA256 "D:\data\OpenRCA\prepared-v10-dev-6case\runtime-cases.json"

uv run python -m backend.benchmarks.openrca run `
  --dataset-root "D:\data\OpenRCA\dataset" `
  --safe-index "D:\data\OpenRCA\prepared-v10-dev-6case\runtime-cases.json" `
  --strategy fixed `
  --mode deterministic `
  --output "D:\data\OpenRCA\results-v10-shared-signal"

$resultRoot = "D:\data\OpenRCA\results-v10-shared-signal"
$runId = (Get-Content -Raw "$resultRoot\latest-run.txt").Trim()
$runDir = (Resolve-Path "$resultRoot\$runId").Path
$queryDir = "D:\data\OpenRCA\official-query-v10-shared-signal\$runId"

uv run python -m backend.benchmarks.openrca evaluate `
  --query-root "D:\data\OpenRCA\dataset" `
  --run-dir $runDir `
  --official-query-output $queryDir

$predictionFiles = Get-ChildItem -LiteralPath $runDir -Filter "fixed-*.csv" |
  Where-Object Name -NE "fixed-predictions.csv" |
  Sort-Object Name
$queryFiles = foreach ($prediction in $predictionFiles) {
  $slug = $prediction.BaseName.Substring("fixed-".Length)
  Join-Path $queryDir "$slug-query.csv"
}

Push-Location "D:\data\OpenRCA\official-evaluator"
python -m main.evaluate `
  -p $predictionFiles.FullName `
  -q $queryFiles `
  -r "$runDir\official-report.csv"
Pop-Location

uv run python -m backend.benchmarks.openrca targeted-gate --run-dir $runDir
```

期望：6/6 completed、至少 3/6 official rows 正分、time/reason/component 各至少
一个正分、Evidence `100%`、projection error/fallback `0`、read-only `0`。

失败即记录结果并停止，不进行第三次正式 40-case。

### T10. Formal deterministic 40-case Gate

覆盖：R12、R13、R14。仅在 T8、T9 通过后执行。

```powershell
$datasetRoot = "D:\data\OpenRCA\dataset"
$preparedRoot = "D:\data\OpenRCA\prepared-v10-shared-signal"
$resultRoot = "D:\data\OpenRCA\results-v10-shared-signal-40"
$queryRoot = "D:\data\OpenRCA\official-query-v10-shared-signal-40"

uv run python -m backend.benchmarks.openrca prepare `
  --dataset-root $datasetRoot `
  --output $preparedRoot `
  --per-partition 10 `
  --seed 42

uv run python -m backend.benchmarks.openrca run `
  --dataset-root $datasetRoot `
  --safe-index "$preparedRoot\runtime-cases.json" `
  --strategy fixed `
  --mode deterministic `
  --output $resultRoot

$runId = (Get-Content -Raw "$resultRoot\latest-run.txt").Trim()
$runDir = (Resolve-Path "$resultRoot\$runId").Path
$officialQueries = Join-Path $queryRoot $runId

uv run python -m backend.benchmarks.openrca evaluate `
  --query-root $datasetRoot `
  --run-dir $runDir `
  --official-query-output $officialQueries

Push-Location "D:\data\OpenRCA\official-evaluator"
python -m main.evaluate `
  -p `
    "$runDir\fixed-Bank.csv" `
    "$runDir\fixed-Market-cloudbed-1.csv" `
    "$runDir\fixed-Market-cloudbed-2.csv" `
    "$runDir\fixed-Telecom.csv" `
  -q `
    "$officialQueries\Bank-query.csv" `
    "$officialQueries\Market-cloudbed-1-query.csv" `
    "$officialQueries\Market-cloudbed-2-query.csv" `
    "$officialQueries\Telecom-query.csv" `
  -r "$runDir\official-report.csv"
Pop-Location

uv run python -m backend.benchmarks.openrca gate --run-dir $runDir
```

期望：

- 40/40 cardinality 和 completed Runtime；
- official partial `>=0.10`，time/reason/component 均 `>0`；
- nonempty `>=95%`；
- Evidence `100%`、read-only `0`；
- deterministic token/cost `0`；
- 所有 manifest/checksum/source identity 可复现。

## 11. Final reconciliation

完成前：

1. 把 spec traceability 的每个 requirement 更新为 `verified` 或带外部 owner 的
   `blocked`。
2. 更新同一 review ledger；只有 regression check 通过后才能关闭 finding。
3. 在 `current.md` 记录 source commit、Gate run IDs、artifact SHA-256 和真实失败。
4. 不夸大七个 lab 场景或已查看六案例的泛化能力。
5. 未经用户授权不 commit、push、建 branch/PR 或删除历史 artifact。
6. 记录 CL0/CL1 净删除、保留的 executable tests，以及 CL2 retain/retire disposition。

## 12. Cold review

2026-07-30 按原 approved spec、F14 scope amendment、当前 repository 和 dirty
diff 进行冷审：

- F13：原 T10 只引用 README，没有满足“计划内给出 exact final Gate command”；
  已内联完整 deterministic 40-case、official evaluator 和 release Gate 命令。
- `production_acceptance.py` 可通过现有 read-only RCA workbench 响应读取
  `coordination_review.root_causes`，无需新增公共 API。
- 没有发现 dependency、migration、Runtime 状态机或未列明 cleanup scope creep。
- 没有 placeholder 或未分配 requirement；T7 Git 授权是执行期显式 Gate。
- F14：用户要求 W1–W7 纳入 V10；已增加 M0/M4.5、R15/R16，并把所有实际代码
  清理放在 T6 key-free regression 和 T7 source checkpoint 前。
- F15：CL1 最初拟整文件删除 Runtime frontend source-shape tests，会同时丢失
  projection 行为覆盖；已改为保留最小 executable behavior tests。

冷审后无 unresolved blocking finding。

2026-07-30 第二轮独立冷审（核对仓库实际代码后）：

- F16：清理任务编号 C0–C2 与 spec mandatory findings C1–C7 冲突；已改名 CL0–CL2，
  spec traceability 同步。
- F17：删除行数口径不一致；CL0 期望已注明净删除含测试、生产代码约 100 行。
- F18：C5 删除行范围在 spec 修正为 `providers.py:339-389`，执行以函数边界为准。
- F19：`PrometheusQuery.metric_names` 上限与新增后 enum 基数（7）同步，维持
  "单调用覆盖全部受支持信号"语义；T3 测试与实现步骤已补充该检查。

## 13. Approval state

第二轮独立冷审的 F16–F19 全部 resolved 后，用户于 2026-07-30 书面批准 amended
spec 和本计划。执行从 M0/CL0 开始，按 milestone 顺序推进；T7 source checkpoint
处必须停止并单独取得 Git commit 授权，未经用户明确请求不得执行任何其他 Git
finish 操作。
