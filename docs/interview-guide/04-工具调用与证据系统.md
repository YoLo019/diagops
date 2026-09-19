# 04. 工具调用与证据系统

## 1. Tool、Provider、Evidence 的关系

```text
Agent 提出工具请求
    ↓
FunctionTool：SDK 能识别的函数定义
    ↓
AdaptiveToolSession：权限、参数、范围、预算、超时、幂等
    ↓
ToolRegistry：工具清单和 handler
    ↓
ProviderRegistry：选择能处理该工具的数据源
    ↓
Provider：读取文件、Prometheus、Tempo、离线包等
    ↓
ProviderResult + EvidenceItem
    ↓
RuntimeWriter 持久化后，Evidence ID 才能被 Agent 引用
```

工具是稳定的 Agent 接口，Provider 是可替换的数据源实现，Evidence 是经过验证和持久化的结果。

V11 还有一条“Agent 调工具之前”的初始证据路径：持久化阶段执行器先直接调用 `ProviderRegistry.collect_results_async`，把 Provider 结果归一成 `DiagnosisContext` 并落库。随后 Investigator 才从这本完整 Evidence ledger 的有界摘要出发，按自己的任务继续调用工具。两条路径最终使用同一个 Evidence 合约，但前者是 Runtime 主动收集，后者是 Agent 自适应查询。

## 2. 九个 Agent 可见工具

工具清单由 [build_provider_tool_registry](../../backend/tools/provider_tools.py) 注册，再由 [ToolRegistry.agent_manifest](../../backend/tools/registry.py) 过滤出 `read_only=True` 且 `exposure=agent` 的工具。

| 工具 | 读取什么 | 典型问题 |
| --- | --- | --- |
| `read_logs` | 日志 | 出现了什么异常？哪个实例先报错？ |
| `query_metrics` | 通用指标 | 错误率、延迟、吞吐或资源何时异常？ |
| `read_deployments` | 发布记录 | 事故前是否变更版本？ |
| `read_service_catalog` | 服务目录 | 服务归属、依赖和上下文是什么？ |
| `query_dependencies` | 上下游依赖 | 故障是否来自上游或下游？ |
| `query_traces` | 分布式 Trace | 请求在哪个调用节点首次失败？ |
| `read_runtime_state` | 实例/进程运行状态 | 是否重启、未就绪或状态异常？ |
| `query_related_alerts` | 相关告警 | 同一时间还有哪些实体告警？ |
| `lookup_memory` | 已验证历史事故 | 是否有同服务、同环境的已验证相似案例？ |

`query_prometheus` 仍为内部兼容别名，但被标记为 `internal`，不进入 V11 Agent manifest。Agent 使用 provider-neutral 的 `query_metrics`，底层 Provider 可以是 Prometheus。

## 3. 为什么必须冻结 manifest

创建 V11 Run 时，工具名列表和哈希写入 `execution_contract`。每次调用都会再检查：

- 工具仍存在；
- 工具在冻结 manifest 中；
- 工具仍是 Agent 可见；
- 工具仍是只读；
- manifest 顺序和哈希没有变化；
- V11 恰好有九个 Agent 工具。

这防止运行中部署新代码后，恢复的 Agent 突然获得创建 Run 时不存在的新权限。

## 4. 参数契约怎样限制模型

工具输入模型在 [tool_queries.py](../../backend/domain/tool_queries.py)。它们统一使用 `extra="forbid"`，模型多传陌生字段会失败。

### 4.1 日志、指标、发布、目录、依赖

这些查询继承 `QueryWindow`：

- 开始和结束时间必须带时区；
- 结束必须晚于开始；
- 时间窗最多两小时；
- `reason` 必填且最多 240 字符；
- `limit` 在 1～100；
- 列表长度和字符串长度均有上限。

依赖查询额外限制：

- 方向只有 upstream/downstream；
- 深度固定为 1；
- target 必须来自本次事故或已验证证据暴露的实体。

### 4.2 Trace、Runtime state、相关告警

这些查询使用 `ScopedTelemetryQuery`：

- entity ID 必须匹配安全字符格式；
- window_start/window_end 必须同时出现；
- 数量上限 100；
- Trace duration 不允许负数、无限值或超大值；
- 枚举和过滤项不能重复。

### 4.3 历史记忆

`MemoryQuery` 不允许模型传 service 和 environment。它们由当前 Investigation 固定，防止跨租户/跨事故任意查历史。模型只能按受影响实体、失败机制和小数量限制筛选。

## 5. 一次调用的详细生命周期

### 步骤 1：SDK 交出原始 JSON

`AdaptiveToolSession.tools_for` 把每个 ToolSpec 包装成 OpenAI Agents SDK `FunctionTool`，传入严格 JSON schema。

### 步骤 2：会话做前置检查

`invoke` 依次检查：

1. 本次 Run 是否还有 deadline；
2. JSON 能否解析成对象；
3. 工具是否在冻结 manifest；
4. Pydantic 参数是否有效；
5. 查询窗口是否与事故窗口相交；
6. dependency target 是否在允许范围；
7. 是否重复查询；
8. Agent 是否已经因无新证据等原因停止；
9. 单 Agent 和全局工具预算是否足够。

### 步骤 3：生成调用身份

系统计算：

- query fingerprint：识别同内容重复查询；
- logical call ID：标识一次逻辑调用；
- idempotency key：由 Run、Agent、逻辑步骤、工具名和规范化参数生成；
- execution ID：标识具体尝试；
- attempt：重试次数。

`reason` 不参与幂等参数规范化，避免模型只改理由就绕过重复查询限制。

### 步骤 4：先写 running，再做外部 I/O

ToolCallRecord 先以 `running` 持久化。这样进程在调用过程中崩溃，审计里不会出现“什么都没发生”的假象。

### 步骤 5：调用 ToolRegistry 和 Provider

ToolRegistry 查找 handler，ProviderRegistry 选择声明支持该工具的 Provider。Provider 返回：

- `ProviderResult.status`；
- EvidenceItems；
- 安全错误类别；
- duration。

如果没有配置 Provider，会显式返回 skipped/gap，不会把 mock 冒充生产数据。

### 步骤 6：落库后再返回模型

工具结果经过 redaction，并绑定当前 `runtime_run_id`。RuntimeWriter 原子写入 ToolCall、Evidence 和 Runtime Event。提交成功后，模型得到的是有界 Evidence 投影，不是任意原始日志正文。

### 步骤 7：处理停止条件

如果成功调用没有带来新 Evidence ID，会标记 `no_new_evidence`，停止该 Investigator 继续浪费工具预算。重复查询、预算耗尽和超时也有明确 stop reason。

## 6. 幂等为什么重要

假设 Provider 已经成功查询并落库，但进程在给模型返回前崩溃。恢复后，如果再查一次：

- 可能花两次钱；
- 可能读到不同时间的数据；
- 可能重复生成证据；
- 评测无法重现。

所以恢复时先按 idempotency key 查 durable success。找到成功记录就复用，不再跨外部边界。Checkpoint 同时保存成功工具键和剩余预算。

## 7. 重试策略

工具路径对分类为 transport 或 rate limit 的瞬时故障最多重试一次；裸 `TimeoutError` 不在工具路径自动重试。模型 SDK 路径会把 `APITimeoutError` 分类为 `timeout`，把 HTTP 5xx `APIStatusError` 分类为 `transport`，但这些分类不会泛化成“所有 timeout 都安全可重试”。原因是工具超时调用可能仍在服务端执行，再重试容易产生重入和超过硬 deadline。

重试前必须：

- 把旧 attempt 写成终态；
- 再检查 lease、取消、deadline、manifest 和共享预算；
- 使用新的 attempt 身份；
- 仍计入同一个全局预算。

## 8. EvidenceItem 为什么不只是字符串

Evidence 的结构包括：

| 字段 | 意义 |
| --- | --- |
| `id` | 可引用的唯一编号 |
| `provider` | 日志、指标、Trace 等来源类别 |
| `kind` | 更具体的证据类型 |
| `timestamp` | 证据时间 |
| `summary` | 给人和模型看的有界摘要 |
| `payload` | JSON 结构化数据，不允许 NaN/Infinity |
| `confidence` | 数据级置信度，不等于根因置信度 |
| `status` | success/partial/failed/skipped 等 |
| `scope` | 实体与时间范围 |
| `provenance` | 来源档案、工件哈希、适配器版本 |
| `runtime_run_id` | 所属运行，防串线 |

`scope` 让校验器能机械发现明显矛盾，例如候选说 `checkout-service`，引用证据却只属于不相关的 `search-service`。但它不能判断“这个错误日志是否真是根因”，那仍是 Investigator 与 Critic 的语义工作。

## 9. Evidence digest：给模型的是摘要，不是删减后的真相

完整 Evidence ledger 可能很长，不能每次全部塞入 prompt。当前 Investigator 起始摘要默认最多 4 条，每个 Evidence kind 最多 2 条，只选 `success/partial`：

- 带实体和 `signal_type` 的指标先按实体分组；
- 同一实体内轮转 CPU、latency、socket、disk、error 等 signal family；
- 按 change score 选择更强信号；
- 为日志、Trace、依赖等非指标证据保留至少一个位置；
- 老 Evidence 没有 signal family 时退回按 kind 轮转。

这避免“一个高分 CPU 指标遮住真正有区分度的 socket 信号”，也避免摘要全是曲线而没有解释机制的日志。摘要只是起点，九工具和完整持久化账本没有被删除。

multi run 的 prompt 还要求候选至少引用两条相关、不同的可用 Evidence ID；摘要不足时允许做一次有界只读查询找第二条。共享准入代码负责 ID、状态、Run 和 scope 合法性，证据是否在语义上真正互相印证仍由 Agent/Critic 判断。

## 10. 证据状态与 Gap

普通因果 Finding 和 Candidate 只能引用 usable evidence，也就是 success 或允许的 partial。Gap Finding 不同：它描述“想要的证据没有拿到”，因此可以引用已提交的 failed/skipped 证据记录来证明缺口。

项目分别提供：

- `validate_usable_evidence`：结论引用；
- `validate_committed_evidence`：Gap 引用，只要求存在且 owner 一致。

这是一个很好的面试设计点：失败记录本身也是审计证据，但不能被当作正向因果证据。

## 11. Provider-neutral 的价值

Agent 只知道 `query_traces`，不知道底层是：

- 本地 incident package 中的 Trace 文件；
- Docker 本地 Tempo；
- 未来另一个实现同契约的只读 Trace Provider。

Provider 必须把不同后端归一为同一个 Evidence 合约。这样可以离线开发、做真实协议集成测试，并避免 prompt 依赖供应商语法。

## 12. 为什么不使用任意 PromQL、URL、文件路径或 Shell

如果模型可以自由生成这些内容，会出现：

- 查询超大时间范围拖垮系统；
- 访问本不应访问的服务或文件；
- prompt injection 诱导执行命令；
- 数据外带；
- 无法稳定复现与审计。

项目只允许预定义参数和模板。Agent 表达“我要看 5xx_rate”，确定性 Provider 决定实际查询怎样构造。

## 13. 面试回答模板

“工具调用分成 Agent Tool 和 Provider 两层。V11 从 ToolRegistry 冻结出九个只读工具并把清单哈希写入执行契约。SDK 只把模型的结构化请求交给 AdaptiveToolSession；会话校验 schema、事故时间窗、实体范围、工具权限、重复查询、deadline 和两级调用预算，再生成 logical ID 与 idempotency key。Provider 返回的内容先脱敏并原子持久化为 ToolCall 和 Evidence，模型只能引用已提交的 Evidence ID。恢复时复用同幂等键的成功结果，不重打外部系统。这样工具调用是受审计的数据读取，而不是给模型开一个任意执行入口。”
