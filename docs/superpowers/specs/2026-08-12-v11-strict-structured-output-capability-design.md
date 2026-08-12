# V11 严格 Structured Outputs 与能力认证修复设计

## 背景与根因

OB30 `single_intended` 生成了 30 条冻结预测，但全部在第一次模型请求前失败。运行事件只有 `agent.started` / `agent.failed`，没有 `MODEL_STARTED`，token 与 tool call 均为 0。

原始异常来自 `openai-agents 0.18.1` 的严格 schema 校验：`SingleControlOutput` 递归引用的 `LeadTaskDraft.evidence_scope: dict[str, Any] | None` 会生成 `additionalProperties: true`，不能作为 strict Structured Outputs schema。运行时随后将该异常归一化成 `ValueError`，导致整批预测表面完成、实际全部失败。

## 目标

1. 保留 `response_format.type = "json_schema"` 和严格 schema，不降级到 `json_object`。
2. 让 V11 所有实际模型输出类型均能通过 Agents SDK 的严格 schema 构造。
3. 让模型能力认证覆盖生产请求所需的 Structured Outputs 契约，并与工具调用组合验证。
4. 阻止“案例全部失败但预测包仍被冻结为成功产物”的同类误判。
5. 修复后重新认证当前 endpoint/model，并从干净身份重新运行 OB30 Single。

## 方案比较

### 方案 A：显式类型化 schema，并补强认证（采用）

将开放字典 `evidence_scope` 替换为字段明确、`extra="forbid"` 的 Pydantic 输出类型；继续让 Agents SDK 生成 strict `json_schema`。能力认证新增实际的 `json_schema + tools` 请求探针，确保端点支持生产组合。

优点是模型侧约束最强、失败最早、认证与运行路径一致。代价是需要明确冻结 `evidence_scope` 的允许字段。

### 方案 B：用 `AgentOutputSchema(..., strict_json_schema=False)`

保留开放字典并关闭严格校验。改动小，但约束退化，错误延迟到响应解析阶段，且不能证明端点支持 Structured Outputs，不采用。

### 方案 C：退回 `json_object`

仅保证输出是 JSON 对象，无法可靠约束嵌套字段、类型和必填项。它也是现有能力认证与生产路径发生偏差的原因之一，不采用。

## 设计

### 输出契约

新增一个只用于模型输出的 `EvidenceScopeDraft`，配置 `extra="forbid"`，只暴露当前 V11 planning 已使用的范围字段：

- `entity_ids: list[str]`
- `start_time: datetime | None`
- `end_time: datetime | None`

列表和字符串沿用现有域契约的上限。`LeadTaskDraft.evidence_scope` 改为 `EvidenceScopeDraft | None`。在写入领域模型 `DiagnosisTask` 时显式 `model_dump(mode="json")`，保持持久化领域字段仍为 JSON map，避免扩大数据模型改动。

所有 V11 模型输出根类型（`LeadPlanningOutput`、`InvestigatorOutput`、`CriticOutput`、`LeadAdjudicationOutput`、`SingleControlOutput`）必须通过 Agents SDK `AgentOutputSchema(..., strict_json_schema=True)` 的本地构造测试。测试递归拒绝任何 `additionalProperties` 非 `false` 的 object schema。

### 能力认证

能力清单以新契约替换旧的 `json_object_output`：

- `strict_json_schema_output`
- `strict_json_schema_with_tools`

探针使用小型、无业务数据的严格 schema。组合探针在同一 Chat Completions 请求中同时携带 strict function tool 与 `response_format.type="json_schema"`，验证生产所依赖的组合可被 endpoint 接受。探针只记录通过状态和脱敏后的错误类别，不持久化 prompt、响应正文或 endpoint URL。

能力清单与 required contracts 的变化会自然改变 manifest hash，使旧认证工件失效；修复提交后必须重新认证。

### 预测完整性

在冻结单侧预测包前要求该分区每条 `CasePrediction.completed` 都为 `true`。任意 case 失败时 prediction worker 非零退出，由现有 pair ledger 将本次 side 标记为失败/失效，不再产生可误认为有效的冻结 bundle。

这项校验不隐藏单 case 失败信息：数据库仍保留安全分类和运行事件，命令行输出只报告失败数量与类别汇总，不泄露模型内容。

### 失败产物处理

当前 OB30 Single 产物保留作为故障证据，不覆盖、不作为有效结果继续使用。修复提交与重新认证完成后，通过 custodian ledger 的既有 reauthorization 流程创建新的 pair identity 或获得明确重跑授权；不手工删除、篡改或复用已冻结 side。

## 测试与验收

1. 回归测试先证明当前 `SingleControlOutput` 无法通过 strict schema 构造。
2. 类型修复后，所有 V11 输出 schema 均通过严格校验，现有 planning/investigation 测试保持通过。
3. 能力认证单元测试断言请求发送 `response_format.type="json_schema"`，schema 为 strict，并验证与 tools 的组合。
4. prediction worker 单元测试断言任一 case 未完成时不会冻结 bundle。
5. 运行相关测试集和 V11 回归测试。
6. 在干净提交身份上重新执行 live 能力认证；两项 strict schema 观测均通过。
7. 重新运行 OB30 Single，验收标准为 30/30 `completed=true`、存在模型使用记录、checksum/identity/ledger 校验通过且 labels 从未打开。

## 非目标

- 不为不支持 strict Structured Outputs 的 endpoint 增加 fallback。
- 不迁移既有 capability artifact；旧 artifact 直接因 manifest/代码身份不匹配而失效。
- 不修改 Multi/Single 的预算比例、工具预算或拓扑。
- 不在本修复中重构整个 V11 输出模型层。
