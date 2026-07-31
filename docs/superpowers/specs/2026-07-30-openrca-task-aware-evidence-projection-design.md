# OpenRCA Task-Aware Evidence Projection Design

Status: `approved`

Date: 2026-07-30

## 1. Context and decision

V10 的两次正式 40-case Fixed Gate 均失败。第二次正式 run
`run-20260730T025117372947Z` 的 official partial 为 `0.0125`，63 个预测根因中
48 个为 `dependency latency`。随后完成的 Provider 边界修复排除了 dependency
self-edge，按 metric series 去重后限流，并修复了 `istio_*` 被误分类为
`disk_io` 的问题。

六案例 targeted run `run-20260730T062337369909Z` 证明上述修复提高了候选多样性，
但 compatible 和 official 仍均为 `0/6`。失败已从单纯的 Provider 噪声收敛到输出
选择边界：

- 生产需要完整、按因果可信度排序的 root cause 三元组；
- OpenRCA 的 task_1–task_7 只评分一个或部分字段；
- 当前 OpenRCA runner 虽持有 `task_index`，但只按期望数量截断共享
  `RootCauseAttribution`，没有按被评分字段投影。

决策：保留共享 Analyzer 和生产 root cause 排序，新增仅存在于 OpenRCA benchmark
adapter 的 task-aware Evidence Projector。它可以使用数据集自带的 `task_index`，
但不得读取 Ground Truth。

## 2. Goals

1. 根据 OpenRCA task 类型，从已有 validated Evidence 和
   `RootCauseAttribution` 中选择被评分字段。
2. 保持生产 Review、Runtime、Replay、API 和共享 Analyzer 行为不变。
3. 所有投影结果继续由有效 Evidence 支持，并可从 benchmark metadata 审计。
4. 六案例 targeted replay 达到 `>=3/6`，且 time、reason、component 各至少命中
   一个，才允许第三次正式 40-case。

## 3. Non-goals

- 不把 `task_index` 引入共享领域模型、Analyzer、生产 API、Runtime 或 Replay。
- 不读取或解析 OpenRCA `record.csv`、Ground Truth、评分报告或历史答案。
- 不新增可训练模型、可调权重、规则注册框架或 benchmark 专用 Provider。
- 不修改生产 root cause 排序，也不声称本设计提升生产根因准确率。
- 不在本规格中建设生产标注集；生产准确率需要独立数据集和 Gate。

## 4. Architecture

```text
OpenRCA telemetry
    |
    v
existing Providers -> Analyzer -> RootCauseAttribution
          |                         |
          |                         +-> production Review/Runtime/Replay unchanged
          v
validated Evidence
          |
          v
OpenRCA Evidence Projector
          |
          v
task_index projection -> deduplication -> deterministic ordering
          |
          v
official prediction CSV + benchmark-only audit metadata
```

### 4.1 Existing shared path

现有 Provider、Analyzer、coordination review 和
`build_root_cause_attributions()` 继续生成生产权威结果。Projector 不回写
Investigation、Review、Runtime event、Replay artifact 或生产报告。

### 4.2 OpenRCA Projector

Projector 位于 `backend/benchmarks/openrca/`，输入仅包括：

- `task_index`；
- `expected_root_cause_count`；
- 当前 Investigation 的 validated Evidence；
- 当前 Investigation 已生成的只读 Hypotheses；
- 现有有序 `RootCauseAttribution`。

输出仍为官方要求的完整三元组：

- `root cause occurrence datetime`；
- `root cause component`；
- `root cause reason`。

未被 task 评分的字段也必须来自同一候选，不能使用常量或跨候选拼接。

### 4.3 Reused canonical behavior

component 提取、reason vocabulary、Evidence 时间聚类和 signal canonicalization
复用现有共享逻辑。实现可以把现有 private pure helper 提升为可复用函数，但不得
改变其生产调用结果，也不得在 Projector 中维护第二套 reason/component 字典。

## 5. Task projection contract

| task_index | Scored fields | Projection deduplication key |
| --- | --- | --- |
| `task_1` | time | time |
| `task_2` | reason | reason |
| `task_3` | component | component |
| `task_4` | time, reason | time + reason |
| `task_5` | time, component | time + component |
| `task_6` | component, reason | component + reason |
| `task_7` | time, component, reason | complete triple |

选择数量等于 `expected_root_cause_count`。双根因任务必须选择两个不同的
deduplication key；重复目标字段组合不能占用第二个位置。

## 6. Candidate construction

### 6.1 Attribution candidates

每个现有 `RootCauseAttribution` 成为一个首选候选，并保留：

- 完整三元组；
- 原共享排序位置；
- supporting Evidence IDs；
- 与这些 ID 对应的 Provider、canonical signal 和 deviation。

任一 supporting Evidence ID 无效时，该候选不得参与投影。

### 6.2 Evidence fallback candidates

当 Attribution 候选不能覆盖目标字段时，可以从 validated Evidence 和已有
Hypotheses 生成 fallback 候选。Projector 不重新运行 Analyzer；它只使用
Hypothesis 的 `cause_type` 和 `supporting_evidence_ids` 调用共享
`build_root_cause_attributions()`。Evidence 必须同时提供：

- timestamp；
- canonical component；
- canonical signal type；
- 可由共享 canonical behavior 得出的 reason；
- 有效 Evidence ID。

缺少任何字段时不得生成候选。fallback 的完整三元组必须来自同一 Evidence
cluster。

### 6.3 Time behavior

共享生产 Attribution 仍保留现有 occurred-at 语义。仅在包含 time 的 OpenRCA
task 中，Projector 使用候选 cluster 内 deviation 最大的 Evidence timestamp；
平局选择更早时间，再按 Evidence ID 稳定排序。不得默认使用诊断窗口起点。

### 6.4 Multi-field coherence

task_4–task_7 的所有被评分字段必须来自同一 Evidence cluster。Projector 不得把
一个 cluster 的时间、另一个 cluster 的 component 和第三个 cluster 的 reason
拼成新根因。

## 7. Deterministic ordering

Projector 不引入数值权重。候选按以下 lexicographic tuple 排序：

1. 被评分字段是否均有直接 Evidence 支持；
2. 支持相同目标字段组合的 distinct Provider 数量，降序；
3. canonical signal specificity：非 `latency`/`traffic` 优先于这两个 generic
   signal；
4. cluster 内最大有限 `deviation_score`，降序；
5. 原共享 Attribution rank，升序；纯 Evidence fallback 使用尾部 rank；
6. occurred-at、规范化目标字段值、排序后的 Evidence IDs，升序。

进入排序前，Projector 先按 task deduplication key 合并等价候选并合并有效 Evidence
IDs。候选池上限为 64；超过上限时使用同一排序规则稳定保留前 64 个。

## 8. Failure, bounds, and audit

### 8.1 Failure behavior

- invalid `task_index` 继续由现有模型/prepare 边界拒绝；
- Evidence 引用失效时丢弃对应候选并记录安全失败分类；
- projected candidates 不足时，可以按原共享顺序补入 Attribution，但 metadata
  必须记录 `projection_fallback=true` 和枚举原因；
- Projector 异常只使当前 benchmark case 失败，不修改已持久化的生产式 Review
  或 Runtime；
- targeted Gate 要求 projection error 和 fallback 均为 0，防止静默退回旧行为。

### 8.2 Benchmark-only metadata

每行 prediction metadata 增加：

- projector rule version；
- task scored fields；
- input、valid、deduplicated candidate counts；
- selected supporting Evidence IDs；
- `projection_fallback` 和安全枚举原因。

metadata 不得包含 Ground Truth、评分结果、原始遥测内容、凭据或异常详情。

### 8.3 Read-only and cost

Projector 是纯内存确定性转换，不增加 Tool 调用、Model 调用、生产写入或外部网络
访问。现有 Evidence validity、read-only violation、token 和 cost 统计继续适用。

## 9. Compatibility

以下内容在相同输入下必须保持字节或语义等价：

- persisted `CoordinationReview.root_causes`；
- Runtime events、FrozenReview 和 Replay root causes；
- 生产 API/report root causes；
- Provider 查询、Evidence IDs 和 shared Analyzer hypotheses；
- Agent shadow 行为。

只允许以下 benchmark artifacts 改变：

- official prediction JSON/CSV；
- prediction metadata；
- compatible/official evaluation report；
- benchmark summary 中新增的 projector 计数。

不新增数据库 migration、配置项或生产 feature flag。

## 10. Verification and release gates

### 10.1 Focused checks

1. task_1–task_7 字段映射和 deduplication key。
2. 单根因和双根因 cardinality。
3. 无效 Evidence 引用被拒绝。
4. time 使用最强异常 Evidence timestamp。
5. task_4–task_7 不跨 cluster 拼接。
6. generic signal 排在同等支持的 specific signal 后。
7. 相同输入重复执行产生完全相同输出。
8. fallback、error 和 audit metadata 可观察且不泄露原始遥测。
9. 生产 Review、Runtime 和 Replay snapshot 不变。

### 10.2 Targeted Gate

使用已公开 Ground Truth 的六案例开发集，仅作为开发回归：

- 6/6 completed；
- Evidence validity `1.0`；
- read-only violations `0`；
- projection error `0`；
- projection fallback `0`；
- official partial 至少 `3/6`；
- task_1、task_2、task_3 各至少一个案例 partial `>0`。

未满足任一条件时停止，不执行第三次正式 40-case，也不追加未经重新设计确认的
启发式权重。

### 10.3 Formal Gate

只有 targeted Gate 全通过后才允许第三次正式 40-case。正式 Gate 继续要求：

- 40/40 completed；
- partial `>=0.10`；
- time、reason、component 均 `>0`；
- cardinality `100%`；
- nonempty `>=95%`；
- Evidence validity `100%`；
- read-only violations `0`；
- deterministic model/tokens/cost 为 `0`。

正式运行前必须建立可复现 source identity，不能只记录 dirty worktree 的 HEAD。
commit、push 或其他 Git 操作仍需用户单独授权。

### 10.4 Repository verification

- Projector focused tests；
- `uv run pytest tests/benchmarks tests/diagnosis tests/rca -q`；
- `uv run ruff check .`；
- `uv run pytest -q`；
- `git diff --check`。

生产准确率不属于本规格完成声明。未来只有在独立生产标注集 Gate 通过后，才能声称
生产根因准确率提升。

## 11. Alternatives

### A. Reorder existing Attribution only

改动最小，但六案例审计已证明部分目标时间和原因没有进入 Attribution，无法覆盖
已确认的成功标准，因此拒绝。

### B. Benchmark-only Evidence Projector

采用。它允许被评分字段使用 validated Evidence，同时隔离生产排序和 benchmark
task 语义。

### C. Make shared Analyzer task-aware

会把 OpenRCA task 概念引入生产核心，并改变生产因果排序，与已确认范围冲突，
因此拒绝。

## 12. Requirements and traceability

| requirement_id | Behavior | Baseline evidence | Acceptance check | Plan task | Status |
| --- | --- | --- | --- | --- | --- |
| R1 | task_1–task_7 使用固定字段映射 | runner 已持有但未使用 `task_index` | seven-task mapping tests | T1 | verified |
| R2 | Projector 只存在于 OpenRCA adapter | 生产与 benchmark 当前共享 Attribution | import/caller audit | T2 | verified |
| R3 | 所有输出字段由有效 Evidence 支持 | Evidence contract 已验证引用 | invalid-reference and field-support tests | T1, T2 | verified |
| R4 | 排序确定且无可调权重 | 当前共享排序确定，但不感知 task | repeatability and ordering tests | T1 | verified |
| R5 | 多字段不得跨 cluster 拼接 | 当前完整 Attribution 保持 cluster 一致 | multi-field coherence tests | T1 | verified |
| R6 | fallback/error 可审计且 targeted Gate 禁止 fallback | 当前 metadata 无 projector 状态 | metadata and gate tests | T2, T3 | verified |
| R7 | 不读取 Ground Truth、评分或 case-specific answer | 六案例已是开发集 | dependency/caller review and artifact privacy scan | T2, T4 | verified |
| R8 | 生产 Review、Runtime、Replay、API 行为不变 | V10 全量测试与冻结 fixture | snapshot/replay/full regression | T2, T4 | verified |
| R9 | 六案例至少 3/6 且三类各命中一个 | 当前 compatible/official 均 0/6 | official targeted evaluation | T3, T4 | failed: 1/6; time/reason 0 |
| R10 | targeted 通过前禁止第三次正式 40-case | 两次正式 Gate 均失败 | release preflight | T3, T5 | verified: stopped before T5 |
| R11 | 不宣称生产准确率提升 | 当前没有生产标注 Gate | documentation assertion review | T4 | verified |
| R12 | Evidence fallback 只使用 Analyzer 已生成的 Hypotheses | Evidence 本身没有 canonical `CauseType` | projector input and no-analyzer-rerun tests | T1, T2 | verified |

## 13. Review ledger

| finding_id | origin | severity | Root cause | Disposition | Resolution | Regression check | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | new_evidence | high | 只重排 Attribution 无法覆盖缺失的时间/原因候选 | accepted | 允许 validated Evidence fallback | fallback candidate tests | resolved in design |
| F2 | scope_change | high | task-aware 排序若进入 Analyzer 会污染生产 | accepted | Projector 限定在 benchmark adapter | caller/import audit | resolved in design |
| F3 | missed | high | 多字段独立投影可能生成不存在的组合 | accepted | 所有字段必须来自同一 Evidence cluster | coherence tests | resolved in design |
| F4 | missed | high | silent fallback 会伪装成新方案结果 | accepted | metadata 显式记录，targeted Gate 要求 0 fallback | fallback gate test | resolved in design |
| F5 | new_evidence | high | dirty worktree HEAD 不能唯一标识正式候选 | accepted | 正式 Gate 前建立可复现 source identity | release preflight | not reached: targeted Gate failed |
| F6 | missed | high | Evidence 只有 `signal_type`，没有共享 reason resolver 所需的 `CauseType` | accepted | Projector 只读使用当前 Investigation 已生成的 Hypotheses，不重跑 Analyzer、不复制 reason 字典 | hypothesis-backed fallback tests | resolved in design |
| F7 | new_evidence | high | task-aware Evidence 投影只命中 1 个 component case，两个 time 和两个 reason case 均未命中 | accepted | 按预先批准的 Gate 停止，不调权重、不加 case 规则、不运行第三次 40-case | official targeted Gate | blocked |

## 14. Verification outcome

真实 targeted run
`D:\data\OpenRCA\results-v10-targeted-projector\run-20260730T090304525928Z`
完成 `6/6`，Evidence 引用有效率 `100%`，read-only violation、projection error 和
projection fallback 均为 `0`。官方评分只有 `Bank:12` 因 `MG01` 命中一个 component
而得到 `0.5`；其余五行均为 `0`。因此正分行数为 `1/6`，task_1/time 为 `0/2`，
task_2/reason 为 `0/2`，task_3/component 为 `1/2`，未达到 R9 的 `>=3/6` 和三类
各至少一条正分要求。

实现前后的局部、benchmark、受影响范围和全量回归均通过；最终全量结果为
`1504 passed`，Ruff 通过。Projector 和预测 artifact 的边界扫描未发现 Ground
Truth、官方报告或评分数据读取。失败发生后按 R10 停止，未执行 source identity
checkpoint，也未启动第三次 40-case。

该结果否定的是“仅靠 benchmark-only 字段感知投影即可达到 targeted 门槛”的假设。
它不改变生产 Review，也不构成生产根因准确率提升证据。
