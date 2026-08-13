# V11 生产 Structured Output Schema 认证修复设计

## 背景与根因

OB30 Single 在修复严格 schema 构造并重新认证后，仍无法完成首个模型请求。4k 正式重试在请求前因生产输入 schema 的保守估算超过总预算而失败；隔离的 24k 单案例测算越过预算预检后，端点返回 HTTP 400：`related_cause_type` 缺少 `type`。

`InvestigatorFindingDraft.related_cause_type` 当前声明为 `Any`。Pydantic 为该字段生成的 JSON Schema 只有 `title`，没有类型语义；`SingleControlOutput` 递归包含该字段，所以实际 `response_format.json_schema` 被兼容端点拒绝。现有能力认证只使用简化的 `ok/scope/items` schema，没有覆盖生产输出 schema，因而错误地认证了 `native_json_schema`。

## 目标

1. 从领域类型根源修复 `related_cause_type`，不对生成后的 JSON Schema 打补丁。
2. 让能力认证验证真实 V11 生产输出 schema，而不是仅验证简化替身。
3. 自动拒绝再次出现无类型语义的生产输出字段。
4. 保持 `strict: true`，不降级到 `json_object`，也不因本次问题强制选择 `strict_output_tool`。
5. 修复后重新认证，并先用隔离的 24k 单案例测量真实 token 使用量；正式预算另行冻结。

## 方案比较

### 方案 A：修正字段类型并认证真实生产 schema（采用）

把 `InvestigatorFindingDraft.related_cause_type` 改为 `CauseType | None`。将 Single 控制输出类型放到 V11 runtime 的共享输出契约边界，使 benchmark runner 与能力认证引用同一个 Pydantic 类型。原生 Structured Outputs 探针直接发送由 Agents SDK 为该类型生成的严格 schema，同时携带普通 strict function tool。

优点是认证路径与生产路径共享同一 schema，字段和嵌套结构变化会自然改变能力工件身份并触发重新认证。代价是认证请求比原简化探针更大，但这是端点准入所需的真实协议成本。

### 方案 B：保留简化探针，增加相似字段（不采用）

在认证 schema 中手工加入 enum/null 字段可以覆盖当前报错，但以后生产 schema 继续演进时仍可能漂移并误通过。

### 方案 C：运行时改写 schema 或切换传输（不采用）

给无类型字段猜测 `type`、关闭 strict、降级 `json_object`，或无条件切到 `strict_output_tool` 都会掩盖领域类型错误，使不同模型使用强弱不一的契约。

## 设计

### 输出类型根修复

`InvestigatorFindingDraft.related_cause_type` 使用既有 `CauseType | None`。该字段在模型输出、`AgentFinding` 和后续持久化之间保持同一个 enum 语义，不新增转换层或兼容分支。

Single 控制输出类型由 benchmark runner 移到 V11 runtime 的共享输出契约区域，并使用表达职责的公共名称。runner 只负责 Single 拓扑与提交，不再私有定义认证也需要使用的生产 schema。

### 生产 schema 认证

能力认证通过 Agents SDK `AgentOutputSchema(..., strict_json_schema=True)` 生成与生产请求相同的 schema，并继续在同一请求中携带普通 strict function tool。探针要求模型返回一个机械有效、内容最小的 Single 输出；认证只持久化通过状态和脱敏异常分类，不保存 prompt、响应正文、endpoint URL 或 schema 正文。

认证代码不复制生产 schema。能力清单或共享输出契约身份变化会改变 capability manifest hash，使旧工件 fail closed。`strict_output_tool` 的两轮真实工具循环保持不变，仍作为原生 schema 不可用时唯一允许的严格传输。

### 本地 schema 完整性

回归测试遍历所有 V11 模型输出根类型。除已有的 object `additionalProperties: false` 约束外，测试还要求每个非纯引用 schema 节点具有明确的类型语义：`type`、`anyOf`、`oneOf`、`allOf`、`$ref`、`enum` 或 `const` 至少存在一个。测试必须在修复前准确定位到 `InvestigatorFindingDraft.properties.related_cause_type`。

这里不实现通用 schema 改写器；测试只负责拒绝不完整契约，类型仍由 Pydantic 模型定义。

### 预算与失败产物

本次不修改正式 4k/10k/12k 预算。现有证据已证明 4k 无法容纳 Single 首请求的保守输入估算，但正式预算属于评测设计身份，必须依据修复后的真实单案例 token 使用量单独决定并重新冻结。

现有正式失败数据库、retry1 数据库和两个 smoke 数据库继续保留。不会覆盖旧 prediction side、手工编辑账本或打开 labels。源代码修复后旧 capability artifact 因代码/manifest 身份变化失效，必须重新认证。

### 错误处理

能力认证若发现真实生产 schema 不受端点支持，应让 `native_json_schema_with_tools` 观测失败，并仅在完整 `strict_output_tool` 探针通过时选择该传输。两者均失败则拒绝 prediction。

正式 prediction 仍要求所有 case 完成后才能冻结。PowerShell 验证命令使用单一脚本块并根据原生进程退出码决定是否打印完成标记，避免失败后继续执行造成假成功。

## 测试与验收

按 TDD 顺序实施：

1. 新增回归测试，证明当前生产 schema 的 `related_cause_type` 没有类型语义，并观察测试失败。
2. 新增认证测试，证明原生探针发送的 schema 与共享生产输出 schema 完全一致，并观察测试失败。
3. 将字段改为 `CauseType | None`，移动共享输出类型，以最小实现使测试通过。
4. 运行 V11 runtime、model capability、RCAEval runner/isolation/evaluator 相关测试及完整测试套件。
5. 在干净提交身份上重新认证当前 endpoint/model；旧工件必须被拒绝，新工件必须选择实测通过的严格传输。
6. 运行隔离的 OB30 Single 24k 单案例。要求 `completed=true`、存在真实 provider token usage、无 failure category，且不访问 labels/正式 ledger。
7. 根据单案例真实总 token 和多轮余量提出新的整组预算设计，获得批准后才生成新的正式 reauthorization token 和 prediction side。

## 非目标

- 不在本修复中决定或修改正式 Single/Multi/equal-token 预算。
- 不新增 JSON Schema 后处理器、模型名称白名单或供应商特判。
- 不放宽 strict schema、本地 Pydantic 校验或预测完整性校验。
- 不清理既有失败数据库和 smoke 证据。
