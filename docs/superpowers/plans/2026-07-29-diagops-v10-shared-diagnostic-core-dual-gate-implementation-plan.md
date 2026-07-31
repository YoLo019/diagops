# DiagOps V10 共享诊断核心与双 Gate 实施计划

状态：`approved`

规格：
`docs/superpowers/specs/2026-07-29-diagops-v10-shared-diagnostic-core-dual-gate-design.md`

工作流：Full `iteration-flow`

## 1. 执行原则

1. 严格按 T1–T8 顺序执行；每个任务先写最小失败检查，再改生产代码。
2. 每个任务完成 focused check 后才进入下一任务。
3. 每个里程碑结束执行一次冷代码审查，发现共享合同变化时按规格的 approval
   invalidation 规则停下。
4. 不增加 Python 或前端依赖。复用 Pydantic、FastAPI、stdlib HTTP、现有
   Provider、Runtime 和持久化。
5. 不修改或覆盖 V8.2/V9 OpenRCA 工件；V10 使用新目录。
6. 不执行 git commit、push、PR 或 branch finish，除非用户另行明确要求。
7. 当前 Docker client `29.2.1`、Compose `v5.1.0` 可用，但 Docker daemon 未
   运行。T6 可完成静态 `compose config`；T7/T8 的真实栈 Gate 需要 Docker
   Desktop daemon 可用，否则 V10 不能完成。

## 2. 里程碑与执行策略

| Milestone | Tasks | 可交付状态 | Review |
| --- | --- | --- | --- |
| M1 共享核心 | T1–T3 | 生产/OpenRCA Evidence 字段统一；确定性 Hypothesis 与 Attribution 可持久化、Replay | 冷审查 Provider 边界、排序、引用、Agent 覆盖和历史兼容 |
| M2 OpenRCA | T4 | key-free Fixed 通过 fixture，保留 Runtime/Replay，official Gate 命令可运行 | 冷审查 Ground Truth 隔离、Top-N、artifact identity |
| M3 Alertmanager | T5 | additive webhook API 与 trust/partial/retry 合同通过 | 冷审查输入边界、旧 API 兼容和安全 detail |
| M4 生产真实栈 | T6–T7 | 六组件 Compose 和四场景 acceptance 可重复运行 | 冷审查 ro mount、lab-only 控制面、无 secret/写 Tool |
| M5 Release | T8 | 全套测试、Runtime、Compose 和 OpenRCA official 双 Gate 有新证据 | 最终全量审查与 traceability 对账 |

执行由当前会话按里程碑顺序完成，不使用 subagent。各任务共享核心合同且文件存在
重叠，并行实现会增加覆盖和审批失效风险。

### 执行状态

| Task | Status | Evidence |
| --- | --- | --- |
| T1 | verified | 38 Provider focused tests；M1 合并套件通过 |
| T2 | verified | 62 Analyzer/Coordination focused tests；M1 合并套件通过 |
| T3 | verified | 227 Orchestrator/Runtime/Replay/Diff focused tests；历史 fixture 未改写 |
| T4 | verified | 51 benchmark tests；M1+M2 合并套件 369/369；fixture Replay 有效 |
| T5 | verified | Events focused 38/38；全 API 124/124；M1–M3 合并 493/493 |
| T6 | verified | Lab focused 6/6；全 services 132/132；Compose config 通过 |
| T7 | verified | 四场景真实 Compose Gate 4/4；Evidence 100%；只读违规 0；61.65s |
| T8 | blocked | 窄范围修复后全仓库 1469/1469、Runtime 与生产 Gate 通过；第二次正式 OpenRCA 40-case 仍未达标 |

## 3. 文件清单

### 3.1 修改

```text
backend/providers/prometheus.py
backend/providers/file_logs.py
backend/providers/file_deployments.py
backend/rca/analyzer.py
backend/diagnosis/coordination_review.py
backend/diagnosis/evidence_validation.py
backend/diagnosis/orchestrator.py
backend/runtime/diff.py
backend/benchmarks/openrca/telemetry.py
backend/benchmarks/openrca/providers.py
backend/benchmarks/openrca/models.py
backend/benchmarks/openrca/prepare.py
backend/benchmarks/openrca/runner.py
backend/benchmarks/openrca/evaluator.py
backend/benchmarks/openrca/__main__.py
backend/api/events.py
tests/providers/test_prometheus_provider.py
tests/providers/test_file_logs.py
tests/providers/test_file_deployments.py
tests/rca/test_analyzer.py
tests/diagnosis/test_coordination_review.py
tests/diagnosis/test_orchestrator.py
tests/runtime/test_checkpoint_integrity.py
tests/runtime/test_golden_equivalence.py
tests/runtime/test_replay.py
tests/runtime/test_diff.py
tests/benchmarks/test_openrca_providers.py
tests/benchmarks/test_openrca_prepare.py
tests/benchmarks/test_openrca_runner.py
tests/benchmarks/test_openrca_evaluator.py
tests/api/test_events_api.py
README.md
.gitignore
docs/superpowers/current.md
docs/superpowers/specs/2026-07-29-diagops-v10-shared-diagnostic-core-dual-gate-design.md
```

### 3.2 新增

```text
Dockerfile.production-gate
compose.production-gate.yaml
backend/services/production_lab.py
backend/services/production_acceptance.py
config/production-gate/diagops.yaml
config/production-gate/prometheus.yml
config/production-gate/rules.yml
config/production-gate/alertmanager.yml
scripts/run_production_gate.ps1
docs/examples/alertmanager-webhook.json
tests/services/test_production_lab.py
tests/services/test_production_acceptance.py
docs/superpowers/plans/2026-07-29-diagops-v10-shared-diagnostic-core-dual-gate-implementation-plan.md
```

不修改 `backend/domain/evidence.py`、SQLite schema 或 frontend：现有模型已经允许
bounded JSON payload，新增 API 尚无前端消费者。

## 4. T1：统一 Provider Evidence 语义

覆盖：R1、R5、R6、R12。

### 文件

- `backend/providers/prometheus.py`
- `backend/providers/file_logs.py`
- `backend/providers/file_deployments.py`
- `backend/benchmarks/openrca/telemetry.py`
- `backend/benchmarks/openrca/providers.py`
- 对应四个 Provider 测试文件

### 步骤

1. 先更新测试：
   - Prometheus 每个 metric 返回一个 Evidence，包含 current、baseline、
     change_percent、deviation_score 和受控 signal_type；
   - current 或 baseline 单项失败得到 PARTIAL，其余 metric 保留；
   - Log 只解析 allowlisted `key=value`，按 component/dependency/exception/type
     聚合，sample 继续 redaction；
   - Deployment 增加 `component=service`、`signal_type=deployment`；
   - OpenRCA `row_component()` 识别 `tc`；
   - OpenRCA 三个 Provider 不含 `root_cause_claims`，每项异常具备统一字段，
     未知 schema 保留 partial 而不猜组件。
2. 运行测试，确认旧实现按预期失败。
3. Prometheus 对现有 allowlisted query template 只增加两个 bounded instant
   时间点，不引入任意 PromQL 或 range API。
4. 在现有 Provider 内直接构造统一 payload；不增加 helper class 或新模型。
5. OpenRCA metric 保留 median/MAD，计算 `deviation_score`，先按强度选 bounded
   candidate，再稳定排序；不按时间升序先截断。
6. 保留原 payload 中仍被旧消费者使用的字段，新增字段保持 additive。

### Focused check

```powershell
uv run pytest `
  tests/providers/test_prometheus_provider.py `
  tests/providers/test_file_logs.py `
  tests/providers/test_file_deployments.py `
  tests/benchmarks/test_openrca_providers.py -v
```

期望：全部通过；没有网络访问；OpenRCA fixture Evidence 中不存在
`root_cause_claims`。

## 5. T2：共享 Analyzer、聚类、排序与 Attribution

覆盖：R2、R4、R12。

### 文件

- `backend/rca/analyzer.py`
- `backend/diagnosis/coordination_review.py`
- `tests/rca/test_analyzer.py`
- `tests/diagnosis/test_coordination_review.py`

### 步骤

1. 先增加规格第 7.4 节的测试：
   - deployment、traffic、dependency 三规则使用统一字段；
   - resource、network、process 通用规则不引用 case ID；
   - 无强信号返回 UNKNOWN `0.2`；
   - 同输入 Hypothesis 顺序稳定；
   - 时间簇使用动态 gap，窗口起点不自动成为发生时间；
   - component 粒度、reason mapping、同簇去重和 Evidence 引用正确。
2. 保留当前 legacy 顶层字段读取，使现有 mock/golden case 不需批量重写。
3. 在 `RcaAnalyzer` 内加入最小 signal 分类和规则；不创建第二个 rule engine。
4. 在 `coordination_review.py` 增加内部
   `build_root_cause_attributions(evidence, hypotheses)`：
   - 只读取 Hypothesis supporting evidence；
   - 按 cause/component/time cluster 计算规格分数；
   - 生成 `RootCauseAttribution`；
   - stable sort、去重并验证 Evidence ID。
5. `build_coordination_review()` 使用该 helper 填充确定性 `root_causes`。

### Focused check

```powershell
uv run pytest `
  tests/rca/test_analyzer.py `
  tests/diagnosis/test_coordination_review.py -v
```

期望：全部通过；既有五类 simulated case 顺序保持，新增三类通用规则和
Attribution 合同通过。

## 6. T3：确定性权威、Agent shadow 与 Runtime 兼容

覆盖：R3、R4、R12、R13。

### 文件

- `backend/diagnosis/orchestrator.py`
- `backend/diagnosis/coordination_review.py`
- `backend/diagnosis/evidence_validation.py`
- `backend/runtime/diff.py`
- `tests/diagnosis/test_orchestrator.py`
- `tests/runtime/test_checkpoint_integrity.py`
- `tests/runtime/test_golden_equivalence.py`
- `tests/runtime/test_replay.py`
- `tests/runtime/test_diff.py`

### 步骤

1. 先增加回归测试：
   - Agent success、failure、timeout 都不覆盖确定性 `root_causes`；
   - historical CoordinationReview 空列表仍可读；
   - V8.2/V9 frozen fixture 继续 Replay；
   - V10 新 root causes 进入 terminal checkpoint、Replay 和 Diff；
   - invalid Evidence reference 仍阻塞持久化。
2. 在 Agent 执行前读取已持久化确定性 review；构造 hybrid review 时显式传入
   authoritative root causes。
3. Agent findings、candidates、summary、uncertainty 和 attribution metadata
   保持现有行为；仅阻止模型三元组覆盖确定性三元组。
4. 不改 Runtime schema、FrozenReview 模型或历史 fixture 内容；现有结构已支持
   非空 root causes。冻结 canonical Attribution 时，把已验证三元组的不可逆关联
   hash 写入现有 `FrozenEvidence.root_cause_claims` 投影，以便 Replay 在不保存
   payload 自由文本、不改 schema 的前提下重新验证。

### Focused check

```powershell
uv run pytest `
  tests/diagnosis/test_orchestrator.py `
  tests/runtime/test_checkpoint_integrity.py `
  tests/runtime/test_golden_equivalence.py `
  tests/runtime/test_replay.py `
  tests/runtime/test_diff.py -v
```

期望：全部通过；历史 fixture 不重写，V10 新 projection 可 Replay/Diff。

### M1 review

```powershell
git diff --check
uv run ruff check backend/providers backend/rca backend/diagnosis tests/providers tests/rca tests/diagnosis
```

冷审查发现 blocking 或 contract-changing finding 时，更新规格 review ledger 并
停下；否则将 T1–T3 标为 verified。

M1 冷审查发现 F14：原计划假设现有 Frozen Evidence 已足够验证 canonical
Attribution，实际它只冻结 legacy `root_cause_claims`。实现已按上述无 schema
变化的最小投影修正，并通过 329 项 M1 合并测试；由于增加了 T3 文件所有权，
计划状态回到 `review_required`，等待重新确认后进入 T4。

## 7. T4：OpenRCA key-free Fixed、Top-N 与 official Gate

覆盖：R3、R5、R11、R14。

### 文件

- `backend/benchmarks/openrca/models.py`
- `backend/benchmarks/openrca/prepare.py`
- `backend/benchmarks/openrca/runner.py`
- `backend/benchmarks/openrca/evaluator.py`
- `backend/benchmarks/openrca/__main__.py`
- 四个 OpenRCA 测试文件

### 步骤

1. 先增加测试：
   - query instruction 的 `two`、`single`、`one`、`a failure` 和显式数字映射
     为 1/2；歧义或缺失使 prepare 失败；
   - `expected_root_cause_count` 写入 safe index，serialized runtime input 不含
     `record.csv`、`scoring_points` 或 Ground Truth；
   - `run --mode deterministic --strategy fixed` 不需要 model/key；
   - deterministic case 仍创建 completed Runtime Run/Attempt/checkpoints，可
     Replay，tokens/cost 为 0；
   - official prediction 只取排序 Top-N；
   - `run --mode agent-shadow` 写独立目录，不覆盖 deterministic；
   - `gate` 在任何 official/分项/cardinality/非空/Evidence/只读/成本阈值失败时
     非零退出。
2. 给 `OpenRcaRuntimeCase` 增加兼容默认
   `expected_root_cause_count: int | None = None`，非空时范围 1–2；旧 safe index
   继续读取，V10 prepare 必须写入，deterministic mode 遇到 `None` 显式拒绝。
3. `OpenRcaDiagnosisRunner` 在 deterministic mode 将 `agents_runtime=None`，但
   继续走 `RuntimeCoordinator`。
4. 新 deterministic summary 使用现有 string 字段：
   `model="deterministic"`、`prompt_version="v10-shared-core"`，避免修改 API 和
   frontend 类型。
5. 每行 frozen prediction metadata 在现有 `runtime_run_id` 之外写入
   `expected_root_cause_count`；`gate` 只从该安全 metadata 计算 cardinality，
   不读取 `record.csv` 或 Ground Truth。
6. CLI 增加：

```text
run --mode deterministic|agent-shadow
gate --run-dir <path>
```

   将 `--model` 从 argparse 无条件 required 改为条件校验：deterministic 禁止
   model/key，agent-backed 旧模式和 `agent-shadow` 缺 model 时显式失败。旧未传
   `--mode` 且传 model 的调用保持既有行为；V10 正式命令必须显式
   `--mode deterministic`。
7. `evaluator.py` 复用现有 report 解析计算 official 分项和 cardinality，不新建
   acceptance framework。

### Focused check

```powershell
uv run pytest tests/benchmarks -v
```

期望：全部通过；deterministic fixture 无 key、零 token、Runtime Replay 有效；
人工构造的失败 official report 使 `gate` 非零退出。

### M2 review

冷审查确认 deterministic 路径不构造 Agent Runtime、不创建 scoring locator；
Replay 的 `not_recorded` 结论不读取 Ground Truth。Top-N 保留共享核心排名，Gate
校验 frozen prediction/summary checksum、official Fixed、分项、cardinality、
非空、Evidence、只读、完成数、零 token/cost，并只记录 strict 而不设下限。
benchmark focused suite 51/51、M1+M2 合并套件 369/369、Ruff、format 和
`git diff --check` 均通过；正式 40-case 留到 T8。

### M2 fixture smoke

```powershell
$fixtureRoot = (Resolve-Path "tests\fixtures\openrca").Path
$smokeRoot = Join-Path $env:TEMP ("diagops-v10-openrca-smoke-" + [guid]::NewGuid())
$preparedRoot = Join-Path $smokeRoot "prepared"
$resultRoot = Join-Path $smokeRoot "results"

uv run python -m backend.benchmarks.openrca prepare `
  --dataset-root $fixtureRoot `
  --output $preparedRoot `
  --per-partition 1 `
  --seed 42

uv run python -m backend.benchmarks.openrca run `
  --dataset-root $fixtureRoot `
  --safe-index "$preparedRoot\runtime-cases.json" `
  --strategy fixed `
  --mode deterministic `
  --output $resultRoot
```

期望：四个 fixture case 都能解析 count；run 不访问 credential，产生 4 个 Fixed
predictions、4 个 completed Runtime Runs 和零 model cost。真实 40-case 只在 T8
最终候选上运行一次，避免重复成本和正式 artifact Git SHA 过早冻结。

## 8. T5：Alertmanager additive API

覆盖：R7、R8、R12、R13。

### 文件

- `backend/api/events.py`
- `tests/api/test_events_api.py`
- `docs/examples/alertmanager-webhook.json`
- `README.md`

### 步骤

1. 先增加 API 测试：
   - 标准 firing 创建一个 Investigation；
   - resolved 先判状态并 ignored，不要求其他创建字段；
   - mixed firing/resolved/invalid 顺序返回，单项失败不阻塞；
   - 非对象、alerts 非列表、超过 100 返回 422 且创建数为 0；
   - service/environment、string length、timezone 边界；
   - externalURL/generatorURL/未知 labels 不出现在 Event、signals、响应或持久化；
   - duplicate delivery 创建第二个 Investigation；
   - 原 `/events` 全部现有测试保持。
2. 在 `events.py` 内定义最小 envelope/response Pydantic 模型和私有转换 helper，
   不增加通用 webhook framework。
3. envelope 结构先整体验证；每条 alert 手工 `IncidentEvent.model_validate`，
   捕获安全分类后继续下一条。
4. 使用现有 `container.run_investigation()` 和 `to_summary()`，顺序 await。
5. 错误 detail 只使用固定安全文本，不回显 payload 或异常。
6. README 和 example 写明 synchronous、at-least-once、resolved ignored、100
   alert 上限和无 dedupe。

### Focused check

```powershell
uv run pytest tests/api/test_events_api.py -v
```

期望：全部通过；旧 `/events` 响应未改变。

### M3 review

冷审查确认 envelope 先整体校验，resolved 在创建字段前处理，firing 只投影
allowlist 字段且逐条顺序隔离；失败 detail 为固定文本，不回显 payload 或异常。
重复 delivery 保持 at-least-once，无去重，并已记录同步处理、100 条上限和 receiver
timeout 约束。Events focused suite 38/38、全 API 124/124、M1–M3 合并套件
493/493、Ruff、format、example JSON 和 `git diff --check` 均通过。

## 9. T6：最小真实服务与 Compose

覆盖：R9、R10、R12。

### 文件

- `Dockerfile.production-gate`
- `compose.production-gate.yaml`
- `backend/services/production_lab.py`
- `config/production-gate/diagops.yaml`
- `config/production-gate/prometheus.yml`
- `config/production-gate/rules.yml`
- `config/production-gate/alertmanager.yml`
- `tests/services/test_production_lab.py`

### 步骤

1. 先测试 lab service 状态机：
   - app/dependency 正常、deployment、timeout、traffic、reset；
   - `/metrics` 只暴露固定 metric names 和 service/environment labels；
   - 日志采用规格 allowlist `key=value`；
   - 只有 app 写 log/deployment；DiagOps 路径只读；
   - fault endpoint 仅在 `DIAGOPS_PRODUCTION_GATE=true` 时存在。
2. 一个 `production_lab.py` 同时支持 app/dependency role，避免复制两个服务。
3. Dockerfile 使用 Python 3.11 slim，并在最终落盘前固定 base image digest；不把
   API key、dataset 或本地数据库复制进镜像。
4. Compose 固定：
   - `prom/prometheus:v3.13.1`
   - `prom/alertmanager:v0.32.1`
   - app、dependency、diagops、scenario-runner 使用同一项目镜像。
5. Prometheus 只 scrape app/dependency，并把三个故障规则发送给 Alertmanager。
   三条规则可以有不同内部 alert name，但 annotations 统一为无原因提示的
   “service degradation detected”；API 不把未知 label 或内部 alert name 放入
   `signals`，避免把场景答案传给 Analyzer。
6. Alertmanager receiver 指向
   `http://diagops:8000/events/alertmanager`，timeout 高于单次 lab 调查实测。
7. named volumes 挂载：
   - app：log/deployment `rw`；
   - diagops：同 volume `ro`；
   - scenario-runner 不直接写证据 volume。
8. lab DiagOps config 关闭 mock、service_catalog、Agent，打开 Prometheus、
   log_file、deployment_file 和 Runtime。

### Focused check

```powershell
uv run pytest tests/services/test_production_lab.py -v
docker compose -f compose.production-gate.yaml config --quiet
```

期望：测试通过；Compose 配置解析成功，不要求 daemon；配置中 DiagOps evidence
volume mount 为 read-only。

## 10. T7：四场景 production acceptance

覆盖：R7、R9、R10、R12、R14。

### 文件

- `backend/services/production_acceptance.py`
- `scripts/run_production_gate.ps1`
- `tests/services/test_production_acceptance.py`
- `.gitignore`
- `README.md`

### 步骤

1. 先测试 acceptance artifact validator：
   - 必须恰好四场景；
   - 三故障 Top-1 CauseType 正确；
   - healthy UNKNOWN 或 confidence `<0.5`；
   - 三故障来源记录 Alertmanager；
   - Evidence ID 100% 有效、read-only violation 0；
   - 总耗时 `<900s`；
   - artifact 不含 credential、raw payload、URL、log body 或异常原文。
2. `production_acceptance.py` 每次只运行环境指定的一个 scenario：
   - 等待 health；
   - 建立 baseline；
   - 通过 lab-only endpoint 注入；
   - 等待 Prometheus rule → Alertmanager → DiagOps Investigation；
   - 查询安全摘要/详情并验证；
   - 写 allowlisted scenario result。
   healthy control 通过 Alertmanager `/api/v2/alerts` 注入同一条通用 degradation
   alert，再由 Alertmanager 调用 DiagOps；不直接构造 `IncidentEvent`。
3. PowerShell runner 按四场景循环。每个 scenario 使用唯一、显式
   `diagops-v10-<scenario>` Compose project；先对该 project 执行
   `down -v --remove-orphans`，再 `up --build --abort-on-container-exit
   --exit-code-from scenario-runner`，最后再次清理同一 project。
4. runner 聚合四个 scenario artifact，验证总耗时和隐私扫描，写
   `output/production-acceptance/<run-id>/result.json`。
5. `.gitignore` 忽略 `/output/production-acceptance/`。

### Focused check

```powershell
uv run pytest tests/services/test_production_acceptance.py -v
docker version
powershell -ExecutionPolicy Bypass -File scripts/run_production_gate.ps1
```

期望：Docker daemon 可用；四个全新 Compose project 均退出 0；最终 artifact
四场景 passed、总耗时 `<900s`、privacy scan passed。

### M3/M4 review

```powershell
git diff --check
uv run ruff check backend/api backend/services tests/api tests/services
uv run pytest tests/api tests/services -v
docker compose -f compose.production-gate.yaml config --quiet
```

重点检查：旧 API、unknown input、partial batch、同步 retry、lab-only 控制面、只读
mount 和 artifact allowlist。

M3/M4 冷审查已验证：API/services `268/268`，Ruff、`git diff --check` 和
Compose config 均通过。真实四场景 artifact 位于
`output/production-acceptance/run-20260729T130529953Z/result.json`，四场景
`4/4`、Evidence 引用 `100%`、只读违规 `0`、总场景时长 `61.65s`、privacy
scan 通过，SHA-256
`06CD6731985B534452F09276D4AD79C2DCFF9D61049C4B57229B53BEE59A0D92`。

## 11. T8：文档、双 Gate 与最终验证

覆盖：R1–R14。

### 11.1 全仓库 key-free checks

```powershell
uv run ruff check .
uv run pytest -v
npm.cmd --prefix frontend run build
uv run python -m backend.services.runtime_acceptance
powershell -ExecutionPolicy Bypass -File scripts/run_production_gate.ps1
```

期望：

- Ruff 退出 0；
- full pytest 全通过；
- frontend production build 退出 0；
- Runtime acceptance 14/14 和 privacy scan 通过；
- production acceptance 四场景通过、总耗时 `<900s`。

### 11.2 OpenRCA official Fixed Gate

```powershell
$datasetRoot = "D:\data\OpenRCA\dataset"
$preparedRoot = "D:\data\OpenRCA\prepared-v10-deterministic"
$resultRoot = "D:\data\OpenRCA\results-v10-deterministic"
$queryRoot = "D:\data\OpenRCA\official-query-v10-deterministic"

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

- 40/40 predictions 和 completed Runtime Runs；
- partial `>=0.10`；
- time、reason、component 均 `>0`；
- cardinality `100%`；
- nonempty `>=95%`；
- Evidence validity `100%`；
- read-only violations `0`；
- model cost `0 <= 6.337199`；
- strict 仅记录；
- gate 退出 0。

正式 Gate 前记录 `D:\data\OpenRCA\official-evaluator` commit，并在 run manifest
中核对 safe-index hash、Git SHA 和所有 artifact checksum。六个已查看 Ground
Truth 的 case 只标记为开发回归，不把该 40-case 结果描述为无偏泛化。

### 11.2.1 正式运行实际结果（2026-07-29）

- 唯一正式 run：`run-20260729T132337926030Z`；
- 路径：`D:\data\OpenRCA\results-v10-deterministic\run-20260729T132337926030Z`；
- 40/40 predictions、40/40 completed Runtime Runs、Evidence validity `1.0`、
  read-only violations `0`、model/tokens/cost `0`；
- official partial `0.025`、strict `0`；compatible component `0.0625`、
  reason `0`、time `0.04`；
- cardinality `38/40`，`Bank:107` 与 `Bank:53` 因
  `root_cause_reason_mismatch` 未写入 review，预测为空；
- official Gate 退出 `2`，T8 保持 blocked，不重复运行该候选。

根因证据：

1. 61 个预测根因中 48 个被映射为 `dependency latency`；OpenRCA dependency
   Provider 把存在的调用边直接输出为故障 Evidence，并使用该边最早 span 时间，
   使正常依赖边压过异常定位且时间偏向窗口起点。
2. `database_slowdown` Analyzer 接受名称中含 `sql` 的 metric，但 Evidence
   validator 只接受 `database` 或 `db_`，规则不一致触发两次
   `root_cause_reason_mismatch`。
3. 上述 validator 修复只能恢复 cardinality，无法把 partial 从 `0.025` 提升到
   `0.10`；必须先为 dependency latency 建立基线异常判定，并从异常点而非最早
   span 生成 occurred_at。

### 11.2.2 窄范围修复（2026-07-30）

- dependency Provider 复用 metric 的 median/MAD 阈值，只输出窗口内错误边或相对
  紧邻且等长的前置基线窗口显著变慢的边；正常调用边不再成为故障 Evidence；
- 前置基线与诊断窗口一起有界读取，避免正式数据中约 2.6GB 的单日 trace 文件
  被整日载入内存，也不使用事后数据；
- dependency occurred_at 改为首个错误 span，或无错误时首个显著慢 span；
- canonical Evidence validator 与 Analyzer 对齐，名称含 `sql` 的 metric 可支持
  `database latency`；
- TDD 初始检查按预期出现 3 个失败；修复后 focused 33/33、受影响套件
  437/437、全仓库 1469/1469，Ruff 与 `git diff --check` 通过；
- 本节完成时未重跑正式 40-case；后续正式结果见 11.2.3。

### 11.2.3 窄范围修复后的正式运行（2026-07-30）

- run：`run-20260730T025117372947Z`；
- 路径：
  `D:\data\OpenRCA\results-v10-deterministic\run-20260730T025117372947Z`；
- 40/40 predictions、40/40 completed Runtime Runs，耗时 2h51m49s；
- cardinality 40/40、nonempty 40/40、Evidence validity `1.0`、read-only
  violations `0`、model/tokens/cost `0`；
- official partial `0.0125`、strict `0`；compatible component `0`、reason `0`、
  time `0.04`；
- Gate 因 partial、component、reason 三项失败；T8 继续 blocked；
- 63 个预测根因中仍有 48 个 `dependency latency`，与修复前的数量相同。正常边
  过滤和 SQL validator 修复恢复了 cardinality，但没有改变正式数据中的
  dependency 主导输出，原“正常边是主要来源”的假设被正式结果否定；
- 与第一次正式运行相比，27/40 案的冻结预测发生变化、两个空预测被补齐，但
  component 从 `0.0625` 降为 `0`，说明修复生效但未改善诊断质量；
- `official-report.csv` SHA-256：
  `2A068205711A837FC5C5F3382540CBA81B4FDE4C4F7190FF2D9F5DBCDAE8278E`；
- `compatible-report.csv` SHA-256：
  `6D91993BE2677BCD41D93B908347024326A4B687A34748725878B3B74BD8D23B`；
- `summary.json` SHA-256：
  `0A4945007FCEDB03D1359722ECF09D455C4A2AB66605EB2C1734EAEA0A2FD1BC`；
- official evaluator commit：
  `c1bd4af7f635171a1c31cdd567c07d698dff6abc`；
- run manifest 记录 Git commit
  `568da9733e5a703355ac9cb743d5089b612412b5`，但运行来自含未提交 V10
  修改的工作树，因此该失败工件可用于诊断，不能作为可复现的 release identity。

下一步先从冻结预测引用的 dependency Evidence 反查错误边、状态码和基线分布，
在不读取 Ground Truth 的前提下建立 targeted replay。没有证据表明诊断质量发生
实质改善前，不执行第三次正式 40-case。

工件核对：

- official evaluator commit：
  `c1bd4af7f635171a1c31cdd567c07d698dff6abc`；
- safe-index SHA-256：
  `1144EACEB98E45174E5C7FB28BA759D2B6668347E371D4C994BD6B98AA8B7852`；
- `official-report.csv` SHA-256：
  `527DE8FC71EF4F99EF23AF128AF66BADD2B2A518EC13817B068E90381FE4AAC6`；
- `summary.json` SHA-256：
  `6D3615892C5FCFD078389B526E8B2CBD0034ABC83B49DB7925889F4DE7B9F37E`。

### 11.2.4 dependency/metric 审计与六案例定向回放（2026-07-30）

- 冻结 63 个正式预测根因后反查：48 个 `dependency latency` 中 28 个由
  self-edge 支持，23 个使用零中位数基线，48 个均没有错误 span；问题不是错误
  状态码解析，而是 self-edge、零基线放大和候选限流共同造成的噪声；
- metric Provider 原先对异常时间点限流，同一序列可占满全部名额；同时
  `istio_*` 因包含 `io_` 被误分类为 `disk_io`。最小修复改为按
  `(component, instance, metric_name)` 去重后限流、排除 dependency self-edge，
  并收紧 disk signal token；
- 三个新测试先 RED、修复后 Provider suite 16/16；受影响套件 440/440、全仓库
  1472/1472、Ruff 均通过；
- 六案例 run：`run-20260730T062337369909Z`，路径：
  `D:\data\OpenRCA\results-v10-targeted-6case\run-20260730T062337369909Z`；
- 6/6 completed、Evidence validity `1.0`、read-only violations `0`、
  model/tokens/cost `0`，耗时 30m01s；
- compatible 与 official 均为 strict `0/6`、partial `0/6`。局部候选发生变化，
  例如 `Market/cloudbed-2:52` 出现 `checkoutservice-1`，但六个任务仍全部未命中；
- targeted safe-index SHA-256：
  `B249E2F6B0B0DBD3B9F30AA71EF3302FF2C48A05CC50B914FB3DCBBC8800AD4B`；
- `fixed-predictions.csv` SHA-256：
  `58A477024F08EAA52B2A36B6E05DF94D8809797E780F27A38BDC9E97DB413EA9`；
- `compatible-report.csv` SHA-256：
  `CBD9CB47CCDEFFC3F5E29121AE821D78EE599DCC70BDE8FE499F7DEF93775D46`；
- `official-report.csv` SHA-256：
  `C44A734BF5ED5E8891A8A08764886FF7A198B75789CBCFF91B3654B83A2CC99A`；
- official evaluator commit：
  `c1bd4af7f635171a1c31cdd567c07d698dff6abc`。

结论：Provider 边界修复提高了候选多样性并消除了明确误分类，但不是根因准确率的
充分修复。停止叠加第三个启发式补丁，也不执行第三次正式 40-case；下一步先明确
OpenRCA task-aware 输出投影与生产通用 causal ranking 的边界，再形成新规格。

### 11.3 最终审查与状态

1. 审查完整 diff、API/config/payload 兼容、Runtime Replay/Diff、生产只读、
   partial failure 和 artifact privacy。
2. 将规格单一追踪表每行更新为 `verified` 或有外部 owner 的 `blocked`；不得保留
   assumed。
3. review ledger 不得有 blocking open finding。
4. 将 `current.md` 的任务、命令、工件绝对路径、hash 和残余风险更新为真实结果。
5. 只有双 Gate 全部通过才把 implementation/iteration 标记 complete；Docker
   daemon 或 official evaluator 不可用时保持 verifying/blocked，不声称完成。

## 12. Deliberate simplifications

- 不新增 DiagnosticSignal、rule registry、webhook framework 或 acceptance
  framework；逻辑放回现有 Provider、Analyzer、coordination 和 evaluator。
- Alertmanager 同步、at-least-once、最多 100 alerts，无队列和去重。
- Lab 复用一个 Python 服务模块模拟 app/dependency，不引入第三方示例应用。
- Agent shadow 只记录 agreement/conflict、tokens、cost 和失败，不拥有
  authoritative root cause。
- 不增加 Loki/Trace/Git Provider；真实栈只证明当前四个 Provider 可驱动共享核心。
