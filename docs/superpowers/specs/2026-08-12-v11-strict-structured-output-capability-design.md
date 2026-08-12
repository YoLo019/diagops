# V11 严格 Structured Outputs 与能力认证修复设计

## 背景与根因

OB30 `single_intended` 生成了 30 条冻结预测，但全部在第一次模型请求前失败。运行事件只有 `agent.started` / `agent.failed`，没有 `MODEL_STARTED`，token 与 tool call 均为 0。

原始异常来自 `openai-agents 0.18.1` 的严格 schema 校验：`SingleControlOutput` 递归引用的 `LeadTaskDraft.evidence_scope: dict[str, Any] | None` 会生成 `additionalProperties: true`，不能作为 strict Structured Outputs schema。运行时随后将该异常归一化成 `ValueError`，导致整批预测表面完成、实际全部失败。

## 目标

1. 优先使用 `response_format.type = "json_schema"`；端点缺少该能力时，只允许切换到 `strict: true` 的最终输出工具，不降级到 `json_object`。
2. 让 V11 所有实际模型输出类型均能通过 Agents SDK 的严格 schema 构造。
3. 让模型能力认证覆盖生产请求所需的 Structured Outputs 契约，并与工具调用组合验证。
4. 阻止“案例全部失败但预测包仍被冻结为成功产物”的同类误判。
5. 修复后重新认证当前 endpoint/model，并从干净身份重新运行 OB30 Single。

## 跨模型适配与准入

实现不绑定某个模型名称。每个 `(canonical endpoint, model, API mode, adapter/SDK/code identity)` 组合必须分别通过能力认证，才能进入 prediction：

- 通过 `native_json_schema_with_tools` 的模型选择 `native_json_schema` 传输。
- 原生接口不支持 `json_schema`、但通过 `strict_output_tool` 的模型选择 `strict_output_tool` 传输。所有远端 function schema 均保持 `strict: true`；供应商仅支持 schema 子集时，传输一个闭合的 `payload_json` 外壳，解码后继续用完整 Pydantic schema 本地严格校验。
- 两种严格传输均未通过、仅实现旧 JSON mode，或缺少普通工具调用能力的模型认证失败，不得启动评测。
- 同一模型名经不同 OpenAI-compatible endpoint 暴露时视为不同能力主体，不复用认证结果。
- endpoint 或模型升级后必须重新认证，避免用名称推断实际协议行为。

因此该方案可适配 Kimi 等原生支持 strict `json_schema` 的兼容模型，也可适配 DeepSeek 等支持 strict function calling、但不支持原生 `json_schema` 的模型。最终准入仍以具体 endpoint/model 的实测认证为准，而不是以模型名称推断。

## 方案比较

### 方案 A：显式类型化 schema，并按能力选择严格传输（采用）

将开放字典 `evidence_scope` 替换为字段明确、`extra="forbid"` 的 Pydantic 输出类型；能力认证分别探测原生 `json_schema + tools` 和 strict final-output tool。前者直接让 Agents SDK 生成 strict `json_schema`；后者将每个远端 function 投影成 strict `payload_json` 外壳，并在本地调用前恢复原始参数、执行原 schema 校验。

优点是模型侧约束最强、失败最早、认证与运行路径一致。代价是需要明确冻结 `evidence_scope` 的允许字段。

### 方案 B：用 `AgentOutputSchema(..., strict_json_schema=False)`

保留开放字典并关闭严格校验。改动小，但约束退化，错误延迟到响应解析阶段，且不能证明端点支持 Structured Outputs，不采用。

### 方案 C：退回 `json_object`

仅保证输出是 JSON 对象，无法可靠约束嵌套字段、类型和必填项。它也是现有能力认证与生产路径发生偏差的原因之一，不采用。

“不降级”表示 strict 能力认证失败时明确拒绝运行，而不是静默改发 `json_object`；“不关闭 strict”表示不把 `strict: true` 改成 `false` 来绕过 schema 错误。这样所有被准入的模型都遵守同一输出契约，评测结果不会因模型不同而使用强弱不一的解析规则。

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

- `native_json_schema_with_tools`
- `strict_output_tool`

原生探针在同一 Chat Completions 请求中同时携带 strict function tool 与 `response_format.type="json_schema"`。工具输出探针强制调用 `submit_structured_output`，其参数只有必填字符串 `payload_json`、`additionalProperties: false` 且 `strict: true`。认证工件记录选中的 `structured_output_transport`，所有运行入口必须从该工件配置 adapter。探针只记录通过状态和脱敏后的错误类别，不持久化 prompt、响应正文或 endpoint URL。

能力清单与 required contracts 的变化会自然改变 manifest hash，使旧认证工件失效；修复提交后必须重新认证。

### 预测完整性

在冻结单侧预测包前要求该分区每条 `CasePrediction.completed` 都为 `true`。任意 case 失败时 prediction worker 非零退出，由现有 pair ledger 将本次 side 标记为失败/失效，不再产生可误认为有效的冻结 bundle。

这项校验不隐藏单 case 失败信息：数据库仍保留安全分类和运行事件，命令行输出只报告失败数量与类别汇总，不泄露模型内容。

### 失败产物处理

当前 OB30 Single 产物保留作为故障证据，不覆盖、不作为有效结果继续使用。修复提交与重新认证完成后，通过 custodian ledger 的既有 reauthorization 流程创建新的 pair identity 或获得明确重跑授权；不手工删除、篡改或复用已冻结 side。

## 测试与验收

1. 回归测试先证明当前 `SingleControlOutput` 无法通过 strict schema 构造。
2. 类型修复后，所有 V11 输出 schema 均通过严格校验，现有 planning/investigation 测试保持通过。
3. 能力认证单元测试断言原生请求发送 strict `json_schema + tools`，并断言原生不可用时只选择 strict output tool。
4. prediction worker 单元测试断言任一 case 未完成时不会冻结 bundle。
5. 运行相关测试集和 V11 回归测试。
6. 在干净提交身份上重新执行 live 能力认证；两个传输中至少一个通过且认证工件选中该传输。
7. 重新运行 OB30 Single，验收标准为 30/30 `completed=true`、存在模型使用记录、checksum/identity/ledger 校验通过且 labels 从未打开。

## 非目标

- 不为两种严格传输均不支持的 endpoint 增加 fallback。
- 不迁移既有 capability artifact；旧 artifact 直接因 manifest/代码身份不匹配而失效。
- 不修改 Multi/Single 的预算比例、工具预算或拓扑。
- 不在本修复中重构整个 V11 输出模型层。
