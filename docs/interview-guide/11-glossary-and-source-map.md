# 11. 术语表与源码地图

## 1. 零基础术语表

| 术语 | 简单解释 |
| --- | --- |
| SRE | 通过软件工程保障服务可靠性的岗位/方法 |
| Incident | 一次影响服务的事故 |
| RCA | 根因分析，不只描述症状，而要解释为什么发生 |
| LLM | 大语言模型，能理解和生成文本/结构化内容 |
| Agent | 带角色、目标、工具和循环的 LLM 应用单元 |
| Multi-Agent | 多个有不同职责的 Agent 协作 |
| Prompt | 给模型的指令和上下文 |
| Context engineering | 决定模型本轮可见哪些事实、工具结果和历史状态，并控制预算、隔离和压缩的工程方法 |
| Context window | 模型一次请求可接收/生成的 token 上限 |
| Context compaction | 在长任务中保留必要状态、减少上下文 token 的过程；不等于删除事实账本 |
| Evidence digest | 从完整 Evidence ledger 确定性选出的有界 prompt 视图，不是 LLM 摘要模型 |
| Lost in the middle | 长上下文中关键材料位于中部而被模型忽略的现象 |
| Prefix cache | 模型服务缓存稳定 prompt 前缀的机制；只能优化成本/延迟，不能作为正确性来源 |
| Token | 模型处理文本的计费/预算单位 |
| Turn | Agent/模型的一轮交互 |
| Tool Call | 模型请求应用执行一个已注册函数 |
| Function Tool | Agent SDK 能识别的函数描述与 schema |
| Provider | 从具体数据源读取证据的适配器 |
| Evidence | 有 ID、状态、范围和来源的可引用证据 |
| Evidence ledger | 本次调查已提交证据的账本 |
| Structured output | 按固定字段和类型返回，而不是任意段落 |
| Pydantic | Python 数据校验库，本项目的领域契约核心 |
| FastAPI | Python Web API 框架 |
| React | 构建浏览器界面的 JavaScript/TypeScript 库 |
| SQLite | 单文件关系数据库 |
| Repository | 隔离业务逻辑与数据库读写的对象 |
| Runtime Run | 一次具体、可持久化的执行 |
| Attempt | Run 的一次连续执行所有权周期 |
| Phase | Run 中按顺序执行的阶段 |
| Checkpoint | 已完整提交、可恢复的阶段边界 |
| Lease | 带到期时间的临时执行所有权 |
| Fence | 阻止旧执行者或晚到结果写入的检查 |
| Idempotency | 同一逻辑操作重复发生仍只产生一次有效效果 |
| Logical call | 一次业务动作的稳定身份；可包含多个网络 attempt/retry |
| Reservation | 在模型/工具外部调用前预留预算，完成后再按实际用量结算 |
| RAG | 检索增强生成：检索外部资料后投影到本轮 context；不等于把历史文本塞进 prompt |
| Embedding | 把文本映射成向量，用于语义相似检索；本项目当前未使用 |
| Reranker | 对初步检索候选做更精细相关性排序的模型/规则 |
| MCP | Model Context Protocol，Host/Client/Server 间发现与调用工具、资源、prompt 的开放协议；本项目当前未接入 |
| Model routing | 按任务/能力/成本/延迟选择模型的策略；与任务路由不同，V11 当前不做动态模型路由 |
| Replay | 不重跑外部能力，只用历史记录验证执行 |
| Rerun | 真正重新执行一次 |
| SSE | 服务端向浏览器单向推送事件的 HTTP 技术 |
| OpenTelemetry | 统一的 Trace/Metric/Log 可观测性标准 |
| Span / Trace | Span 是一次可观测操作，Trace 是关联 Span 的调用链；不是业务 Evidence 真相源 |
| Trajectory evaluation | 对 Agent 的计划、工具、证据、重试和停止轨迹做评测，而不只看最终答案 |
| LLM-as-a-judge | 用另一个模型按 rubric 评价开放式输出；需要人审校准，不能替代确定性安全检查 |
| SLO / RTO / RPO | 服务目标；恢复时间目标；可容忍的数据丢失目标 |
| Prometheus | 常见指标采集和查询系统 |
| Tempo | Grafana 的分布式 Trace 后端 |
| OpenRCA | 外部 RCA 数据集/评测兼容流程 |
| RCAEval | 本项目 V11 使用的受控 held-out 评测流程 |
| Benchmark leakage | 答案、标签或评分规则泄漏给被评模型 |
| Capability artifact | 证明精确模型端点具备所需能力的冻结工件 |
| Authority mode | 最终诊断由 Agent 还是 legacy deterministic 路径负责 |
| Inconclusive | 证据不足，安全地不下根因结论 |

## 2. V11 角色和对象速查

| 概念 | 谁创建 | 谁消费 | 关键源码 |
| --- | --- | --- | --- |
| Lead Plan | Lead | Runtime/Investigators | `V11Runtime.plan_lead` |
| DiagnosisTask | 服务端从 Lead draft 构造 | Investigator | `V11Runtime._build_plan` |
| ToolCallRecord | AdaptiveToolSession | Runtime/UI/Audit | `domain/tool_calls.py` |
| EvidenceItem | Provider/Tool bridge | Investigator/Critic/authority audit | `domain/evidence.py` |
| AgentFinding | Investigator | Critic/Validator | `domain/agent_findings.py` |
| RootCauseCandidate | Investigator draft，服务端换 ID | Critic/authority projection | `domain/agent_findings.py` |
| CriticAssessment | Critic | Round 2/authority projection | `V11Runtime.critic_review` |
| LeadDecision | live：服务端投影 Critic verdict；注入路径：Lead | Validator/Report | `domain/agent_plan.py` |
| CoordinationReview | V11Runtime 聚合 | API/Report/UI | `domain/agent_findings.py` |
| RuntimeCheckpoint | Runtime Store commit | Resume/Replay | `domain/runtime.py` |

`RootCauseCandidate.failure_class` 是短而稳定的故障分类；`failure_mechanism` 是面向人的因果机制解释。两者不能混用。`Evidence digest` 是给单个 Agent 的有界起始上下文，不是完整 Evidence ledger 的替代品。

## 3. 按问题找源码

| 想知道的问题 | 先看文件 |
| --- | --- |
| 应用怎样装配 | [backend/services/container.py](../../backend/services/container.py) |
| API 从哪里注册 | [backend/main.py](../../backend/main.py) |
| 事件怎样进入 | [backend/api/events.py](../../backend/api/events.py)、[investigations.py](../../backend/api/investigations.py) |
| V11 阶段顺序 | [backend/runtime/phases.py](../../backend/runtime/phases.py) |
| Lead/Investigator/Critic | [backend/diagnosis/v11_runtime.py](../../backend/diagnosis/v11_runtime.py) |
| SDK Agent 怎样创建 | `V11Runtime._call_model` |
| 九工具清单 | [backend/tools/provider_tools.py](../../backend/tools/provider_tools.py) |
| manifest 校验 | [backend/tools/registry.py](../../backend/tools/registry.py) |
| 工具参数限制 | [backend/domain/tool_queries.py](../../backend/domain/tool_queries.py) |
| 工具重试/幂等/scope | [backend/diagnosis/adaptive_tools.py](../../backend/diagnosis/adaptive_tools.py) |
| Provider 选择 | [backend/providers/registry.py](../../backend/providers/registry.py) |
| Evidence 契约 | [backend/domain/evidence.py](../../backend/domain/evidence.py) |
| 最终机械校验 | [backend/diagnosis/result_validation.py](../../backend/diagnosis/result_validation.py) |
| 报告生成 | [backend/reports/generator.py](../../backend/reports/generator.py) |
| Run 生命周期 | [backend/runtime/coordinator.py](../../backend/runtime/coordinator.py) |
| 单 Writer | [backend/runtime/writer.py](../../backend/runtime/writer.py) |
| SQLite 表 | [backend/db/schema.py](../../backend/db/schema.py) |
| Replay | [backend/runtime/replay.py](../../backend/runtime/replay.py) |
| Diff | [backend/runtime/diff.py](../../backend/runtime/diff.py) |
| SSE | [backend/api/runtime_runs.py](../../backend/api/runtime_runs.py)、[event_hub.py](../../backend/runtime/event_hub.py) |
| 脱敏 | [backend/safety/redaction.py](../../backend/safety/redaction.py) |
| 前端 Investigation | [frontend/src/App.tsx](../../frontend/src/App.tsx) |
| 前端 Runtime | [frontend/src/RuntimeWorkbench.tsx](../../frontend/src/RuntimeWorkbench.tsx) |
| OpenRCA | [backend/benchmarks/openrca](../../backend/benchmarks/openrca/) |
| RCAEval | [backend/benchmarks/rcaeval](../../backend/benchmarks/rcaeval/) |
| 当前完成度 | [docs/superpowers/current.md](../superpowers/current.md) |
| Context 投影、digest、token reservation | [backend/diagnosis/v11_runtime.py](../../backend/diagnosis/v11_runtime.py)、[adaptive_tools.py](../../backend/diagnosis/adaptive_tools.py) |
| Memory 与 verified lookup | [backend/memory/store.py](../../backend/memory/store.py)、[provider_tools.py](../../backend/tools/provider_tools.py) |
| 模型 capability admission | [backend/services/model_capability.py](../../backend/services/model_capability.py) |
| allowlisted OTel trace / 指标维度 | [backend/runtime/telemetry.py](../../backend/runtime/telemetry.py) |
| Agent 评测与标签隔离 | [backend/benchmarks/rcaeval](../../backend/benchmarks/rcaeval/) |
| MCP 当前状态/演进 | [19-MCP 与互操作](19-mcp-and-agent-interoperability.md) |

## 4. 关键函数调用地图

```text
AppContainer.run_investigation
└─ RuntimeManager.start
   └─ RuntimeCoordinator.execute
      └─ DiagnosisPhaseExecutor.execute_phase
         ├─ V11 intake/evidence/report/finalize handlers
         └─ V11Runtime.run_phase
            ├─ plan_lead
            ├─ investigator_round_1
            │  └─ _run_investigator
            │     ├─ AdaptiveToolSession.tools_for
            │     └─ _call_model
            ├─ critic_review
            ├─ investigator_round_2
            ├─ critic_reconciliation
            ├─ lead_adjudication
            └─ result_validation
```

工具分支：

```text
AdaptiveToolSession.invoke
├─ schema / scope / manifest / budget / duplicate checks
├─ persist ToolCall RUNNING
├─ ToolRegistry.invoke_detailed
│  └─ invoke_provider_tool
│     └─ ProviderRegistry.query_results
│        └─ Provider.collect
├─ persist ToolCall terminal + Evidence + Events
└─ return bounded evidence projection to Agent
```

## 5. 配置速查

默认配置位于 [config/diagops.yaml](../../config/diagops.yaml)：

| 配置组 | 关键项 |
| --- | --- |
| `storage` | SQLite URL |
| `providers` | mock、文件、Prometheus、local package 开关和路径 |
| `agents` | enabled、provider、model、turn、timeout、Token、strategy、Tool budgets |
| `agents.openai_compatible` | base URL、API mode、timeout、retries，不含 key |
| `benchmark` | OpenRCA 结果目录 |
| `runtime` | Run 并发、单 Run 并行、lease、heartbeat、OTel |

环境覆盖逻辑在 [backend/config/settings.py](../../backend/config/settings.py)。敏感 key 只允许环境变量。

## 6. 状态速查

### Runtime lifecycle

`created → running → completed/failed`，取消经过 `cancelling → cancelled`，lease 失效可到 `interrupted`。

### Diagnostic status

- `complete`：完整有效诊断；
- `partial`：非关键失败但结论仍充分；
- `inconclusive`：证据不足；
- failed/cancelled/timeout 没有有效 diagnostic projection。

### ToolCall

`pending/running/success/failed/skipped/interrupted`。

### AgentExecution

记录角色、task、round、attempt、模型、usage、Evidence IDs、failure category 和时间。

## 7. 最后记住这张边界表

| 问题 | 负责者 |
| --- | --- |
| 下一步查什么 | Lead |
| 证据说明什么 | Investigator |
| 因果是否充分 | Critic |
| 最终判断候选是否通过 | Critic |
| 把 accepted 候选发布为权威结果 | Lead adjudication authority phase |
| 工具能否调用 | ToolRegistry + AdaptiveToolSession |
| 数据怎样读取 | Provider |
| 引用和结构是否合法 | Validator |
| 阶段、预算、取消、恢复 | Runtime |
| 是否执行生产修复 | 人类；DiagOps 不执行 |

如果面试中一时紧张，回到这张表，就能避免把职责讲混。
