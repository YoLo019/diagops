# RCAEval SS15 协议设计

## 背景

上游 RCAEval RE2 的 Sock Shop 数据集仍然包含完整的 90 个 case：5 个服务、6
种故障、每个服务/故障单元格 3 次重复。本项目当前把其中每个单元格选 1 个，
形成 30-case 的本地封存验证分区；该分区名称和数量与用户希望的分批执行成本不
匹配。

本变更把本项目的封存验证分区正式改名为 `SS15`，但不修改、删除或缩减上游
Sock Shop 的 90-case 源数据校验。现有 `SS30` 输出、数据库和 bundle 是历史证据，
不迁移、不覆盖，也不与新协议拼接。

## 目标

- 用一个新的、明确命名的 `SS15` 分区承载 15-case 封存验证。
- 保持上游源包的 270 个 RE2 case、每个系统的 5×6×3 完整性校验和 provenance
  不变。
- 让 runtime manifest、label manifest、ledger、prediction bundle、evaluator、
  acceptance policy 和命令行在同一个 `SS15` 身份上闭合。
- 保持原有四种配置和预算拓扑，只把每个配置的 case cardinality 改为 15。
- 让 case 选择完全由已冻结 seed 可重算、可审计，并且不把标签元数据放回 runtime
  包。

## 非目标

- 不把上游 `RE2-SS` 数据集改成 15 个；上游仍是 90 个 case。
- 不修改 `OB30`（30 个开发 case）或 `TT90`（90 个最终留置 case）。
- 不把历史 `SS30` 预测、不同 endpoint 的结果或失败 epoch 拼进 SS15 bundle。
- 不新增 15+15 的跨进程恢复协议；SS15 本身已经是单个 15-case 正式 bundle。

## 固定选择规则

源校验继续要求每个系统恰好覆盖 `5 services × 6 faults × 3 repetitions`。选择器
对系统分别处理：

1. `OB30` 保留现有规则：每个服务/故障单元格取
   `SHA-256(seed + ":" + source_case_id)` 最小的一个 repetition。
2. `SS15` 先列出 Sock Shop 的 30 个服务/故障单元格。对每个单元格计算
   `SHA-256(seed + ":cell:sock_shop:" + service + ":" + fault)`，取 digest 最小
   的 15 个单元格。
3. 对每个入选单元格，再从其 3 个 source case 中按现有的
   `SHA-256(seed + ":" + source_case_id)` 规则取一个 repetition。
4. 最终 source ID 按稳定字典序写入 manifest；opaque ID 继续使用 partition 名称参与
   哈希，因此 SS15 会得到全新的 runtime/label 身份。
5. `TT90` 继续保留每个单元格的全部 3 个 repetition，共 90 个 case。

该规则保证 SS15 恰好包含 15 个不同的服务/故障单元格、每格 1 个 repetition，且
重复 prepare 的 manifest 和包字节完全一致。未入选的 15 个单元格仍在上游源包中，
也仍然通过源完整性校验。

## 协议与数据流

### 类型与 cardinality

- `RcaEvalPartition.SS30` 删除，新增 `RcaEvalPartition.SS15`。
- `SYSTEM_TO_PARTITION[Sock Shop] = SS15`。
- `EXPECTED_PARTITION_COUNTS` 变为 `OB30=30, SS15=15, TT90=90`。
- source taxonomy、`REPETITIONS_PER_CELL` 和上游 manifest 的总 case 数仍为
  `270`、每系统 `90`、每单元格 `3`。
- SS15 仍要求四个配置：`single_intended`、`multi_intended`、
  `single_equal_token`、`multi_equal_token`。预算拓扑和 `Multi ≤ 3×Single` 约束
  不变。

### 运行时与评测器

- prediction CLI 只接受 `ob30`、`ss15`、`tt90`，并按 manifest 精确验证 case 数。
- `freeze-set` 对 `ss15` 期待 15 个 case；formal bundle、summary 和 acceptance
  policy 的 SS15 cardinality gate 统一期待 15。
- bundle 仍拒绝 incomplete prediction、重复 case、混合 execution identity；新的
  SS15 manifest/capability/source revision 形成新的 ledger identity。
- evaluator 仍在 labels 打开前只接收完整冻结 bundle；SS15 不能用两个不同时代、
  endpoint 或 manifest 的半包合成。

### 目录与迁移

- 重新 prepare 到新的 custodian 根，例如
  `D:\data\RCAEval\v11-m5-ss15`；不复用或覆盖
  `D:\data\RCAEval\v11-m5-ss30`。
- 新根拥有新的 runtime/label manifest hash、custodian manifest、pair ledger 和
  reauthorization lineage。
- 旧 SS30 epoch/JSON/SQLite 文件保持只读历史证据；代码不提供迁移兼容层。
- 桌面认证和预测脚本的 partition、输出根、数据库名、检查消息和结果字段同步改为
  SS15，API capability artifact 仍必须与新 clean HEAD 重新认证后才能准入。

## 代码边界

- `backend/benchmarks/rcaeval/models.py`：分区枚举、系统映射、partition counts。
- `backend/benchmarks/rcaeval/prepare.py`：SS15 两阶段 seeded selector 和报告。
- `backend/benchmarks/rcaeval/__main__.py`：CLI choices、prediction/freeze-set/evaluate
  cardinality。
- `backend/benchmarks/rcaeval/runner.py`：配置与正式 bundle 的 SS15 约束消息。
- `backend/benchmarks/rcaeval/evaluator.py`：formal cardinality、policy 和摘要验证。
- `backend/benchmarks/rcaeval/ledger.py`、`isolation.py`、`audit.py`：只更新与分区名
  或预期数量直接相关的契约，不改变 ledger 的不可恢复失败和身份绑定语义。
- `tests/benchmarks/`：更新 selector golden、cardinality、CLI/evaluator/ledger 测试，
  并增加源包仍为 90-case Sock Shop 的回归断言。
- `docs/superpowers/current.md` 与当前 M5 计划：把未执行的正式目标改成 SS15；历史
  epoch 记录明确保留为 SS30 证据，不重写历史结果。

## 错误处理与安全边界

- 缺少任何源 cell/repetition、重复源 cell、改变的 pin 或非完整 90-case 源包仍然
  fail closed。
- 选择器得到的数量不是 `SS15=15`、`OB30=30` 或 `TT90=90` 时立即失败，不生成
  部分 runtime/label 包。
- 新旧 partition、不同 manifest、不同 capability 或不同 source revision 混合时由
  bundle/evaluator/ledger 拒绝；不通过修改 SQLite 或 JSON 绕过。
- runtime 包继续只包含 opaque case ID 和遥测，service/fault/repetition/source ID
  只进入 evaluator-only labels。

## 验证与验收

1. 合成 fixture 验证源仍为 270、每系统 90、每 cell 3；OB30=30、SS15=15、TT90=90。
2. 固定 seed 的 SS15 golden vector 验证 15 个 cell 和 15 个 source ID 完全一致，重复
   prepare 字节等价。
3. SS15 bundle 15/15 通过；14/15、重复 case、incomplete、混合 identity 均拒绝。
4. evaluator/acceptance policy 只接受四个 SS15 配置，每个 summary case count 为 15；
   TT90 的 90-case gate 继续通过。
5. 运行 RCAEval、runtime、diagnosis、Ruff 和 diff-check 相关测试；重新生成的新
   runtime manifest 记录上游仓库、revision、archive hash 和 `ss15=15`，旧 SS30
   artifact hash 保持可读且不被覆盖。
