# 10. 面试高频问答

## 项目与业务

### 1. 这个项目解决什么问题？

它把应用服务事故调查变成可追踪的只读工作流：从日志、指标、Trace、发布、依赖等来源收集证据，用多 Agent 形成和审查根因候选，输出带 Evidence ID 的报告，同时用 durable Runtime 保证预算、超时、取消、恢复和审计。

### 2. 为什么不用人直接问 ChatGPT？

自由聊天缺少数据访问边界、结构化引用、持久化状态、超时取消、幂等和审计。DiagOps 把模型限制在明确角色和九个只读工具内，并把每一步转成可验证对象。

### 3. 它会自动修复吗？

不会。它只生成建议、记录人工审批和验证结果；没有 SSH、Shell、重启、回滚、扩容或配置修改工具。

### 4. 项目的目标用户是谁？

主要是处理应用服务事故的 SRE/运维工程师、复核诊断的 reviewer，以及验证系统效果的 benchmark custodian。

## Agent 架构

### 5. 为什么是 Lead、Investigator、Critic？

Lead 负责信息价值和最终责任，Investigator 负责相互隔离地取证，Critic 负责反证和因果充分性。这样分别处理规划偏差、相互影响和自我确认问题。

### 6. 为什么不让三个 Agent 投票？

同一模型的错误高度相关，多数意见不等于证据。项目要求 Critic 逐项审查，再由 Lead 对 accepted candidate 裁决。

### 7. 为什么不继续用 LogAgent、MetricAgent、DeploymentAgent？

按数据模态分工会先假设原因在哪类数据里。V11 按信息缺口分工，通用 Investigator 可以跨日志、指标、Trace 和依赖做完整因果调查。

### 8. Round 1 为什么隔离？

防止兄弟 Agent 相互锚定和复制结论。它们共享事故和工具能力，但只看自己的任务与证据；到 Critic 阶段才汇总。

### 9. 为什么最多两轮？

开放式反思会导致成本和延迟不可控，也可能放大相关幻觉。Critic 只允许一批针对性补证，reconciliation 后禁止第三轮。

### 10. 谁是最终诊断权威？

V11 中是 Lead Agent 的结构化裁决。Critic 决定候选是否通过审查；确定性 Validator 只有否决机械违规的权力，不能替换根因。

### 11. 如果 Agent 输出错误怎么办？

结构错误由 Pydantic 拒绝；引用错误在草稿准入或最终校验拒绝；必要阶段允许一次无工具 correction；仍不合法就 inconclusive 或 failed，不由代码补造答案。

### 12. 使用了什么 Agent 框架？

使用 OpenAI Agents SDK 作为单轮 Agent、FunctionTool、Runner 和结构化输出的执行组件。多阶段 durable orchestration 由项目自己的 Runtime 实现，没有引入 LangGraph 等重型编排框架。

### 13. 这是 handoff 还是 manager 模式？

更接近应用控制的 manager/orchestrator。Runtime 按阶段调用角色，角色不会自由 handoff 控制权；每阶段先结构化和持久化，再进入下一阶段。

## 工具与证据

### 14. Tool 和 Provider 有什么区别？

Tool 是给 Agent 的稳定、受控能力；Provider 是读取具体数据源的实现。同一 `query_traces` 可以由文件或 Tempo Provider 实现。

### 15. 为什么只有九个工具？

它们覆盖当前应用服务 RCA 所需的日志、指标、发布、目录、依赖、Trace、Runtime state、相关告警和已验证历史。固定清单便于权限审计、离线实现和正式评测；没有证据支持的能力不应宣称存在。

### 16. 怎样防止模型调用危险工具？

危险工具根本不注册；manifest 只发布 read-only + Agent exposure 的 ToolSpec。Run 冻结 manifest 和 hash，每次调用再次校验。

### 17. 怎样防止任意查询？

每个工具有严格 Pydantic schema，限制字段、时间窗、条数、枚举、实体和依赖深度；不接受任意 PromQL、URL、文件路径或命令。

### 18. Evidence ID 有什么用？

它把结论绑定到可持久化和可审计的证据。系统检查 ID 存在、状态可用、属于同 Run、scope 不冲突，前端也能沿 ID 展示证据链。

### 19. 怎样处理重复工具调用？

规范化查询生成 fingerprint 拒绝同会话重复；Run、Agent、逻辑步骤、工具和参数生成 idempotency key，恢复时复用 durable success。

### 20. 为什么 Tool result 要先落库再给模型？

否则模型可能引用一个数据库里不存在的结果；进程崩溃后也无法证明它看过什么。先提交 Evidence ledger，再允许 Finding 引用。

### 21. Provider 没配置怎么办？

显式返回 skipped/failed gap 证据，不回退 mock 冒充真实生产数据。

## Runtime 与数据

### 22. Investigation、Run、Attempt 有什么区别？

Investigation 是业务事故；Run 是一次具体执行；Attempt 是 Run 的一次连续执行所有权周期。恢复同一个 Run 会创建新 Attempt。

### 23. 为什么需要 Checkpoint？

它定义阶段级 durable 边界，保存引用、剩余预算和成功工具键。恢复可以从最后完整阶段继续，并通过 digest 检测投影篡改或缺失。

### 24. Resume 和 Replay 有什么区别？

Resume 继续中断 Run，可能调用未完成的模型/工具；Replay 只读取持久化状态验证一致性，绝不调用外部能力、消耗零 Token。

### 25. 为什么 SQLite 还要单 Writer？

并行 Agent/Provider 会带来并发写和乱序事务。外部 I/O 并行，业务提交通过单 Writer 的短事务串行化，兼顾吞吐和一致性。

### 26. 进程崩溃怎样恢复？

lease 到期审计把 Run 标记 interrupted；人工 resume 验证 execution contract、checkpoint digest、owner、deadline 和预算，创建新 Attempt，从下一阶段继续。

### 27. 超时后 Provider 结果回来怎么办？

调用结束后再次检查 lease/cancel fence。旧所有者或 deadline 后的晚到结果不能提交。

### 28. SSE 如何不丢事件？

数据库 append-only Event sequence 是真相源。重连先订阅 live 唤醒，再按 last sequence 补 durable events，并去重。

### 29. 为什么 V10 和 V11 都存在？

历史持久化记录必须可读，但 V11 改变了诊断权威和阶段语义。两个不可变 phase profile 防止旧记录被新语义错误解释。

## 安全与可靠性

### 30. 怎样防 prompt injection？

证据被当作不可信数据、脱敏和有界投影；模型没有任意执行工具；工具名和 schema 由代码固定；越界参数被拒绝；Event 和 Artifact 只允许固定字段。

### 31. API key 存哪里？

只在进程环境变量。不能进 YAML、源码、日志、执行契约、报告、Trace 或评测工件。

### 32. 保存 chain-of-thought 吗？

不保存。只保存任务、工具调用、Finding、Critic checks、Lead concise rationale、usage 和安全失败信息。

### 33. partial 和 inconclusive 有什么区别？

partial 有有效诊断，但有非关键失败且仍满足独立证据要求；inconclusive 表示证据不足以支持根因，是负责任的安全停止。

### 34. 确定性 fallback 什么时候能用？

只有 Agent 尚未产生可接受诊断输出且 Runtime 未配置、认证前失败或 endpoint 预先不可用等特定条件；必须明确标为 legacy fallback，不能冒充 V11。

## 评测与取舍

### 35. 怎样证明 Multi-Agent 比 Single-Agent 好？

在同一 held-out runtime package、模型、能力工件和受控 Token 预算下比较冻结单 Agent 与 Multi-Agent；prediction 先冻结、标签后打开，并同时看准确率、证据有效性、成本、延迟和失败率。

### 36. 为什么不能只看准确率？

高准确率可能来自标签泄漏、无效证据引用、超高成本或大量危险调用。必须把效果、安全、预算和完成率一起评估。

### 37. 当前效果如何？

不能声称 V11 已提升。当前工程进入 M5 受控评测阶段，但 capability admission 和正式 SS15/TT90 尚未完成。

### 38. 为什么不用 LangGraph/MCP/向量库？

现有 Runtime 已有阶段、持久化、恢复和预算，ToolRegistry 已能提供本地只读工具。新增框架会复制能力和扩大风险面，当前没有测量需求支持。

### 39. 这个项目最大的技术亮点是什么？

不是“用了多个 Agent”，而是清晰分离 Agent 诊断权与确定性执行保障，并把证据、工具权限、执行身份、预算和恢复状态都做成 durable contract。

### 40. 最大局限是什么？

正式 V11 效果门尚未完成，Agent 默认关闭；真实 Provider 覆盖有限，主要面向应用服务；不是经过企业多租户、真实生产负载和自动修复认证的平台。

### 41. 下一步你会做什么？

先完成精确 endpoint/model 的 capability admission 和冻结 SS15/TT90 gate，取得可信效果结论；再根据失败分布决定是改 prompt、证据 Provider 还是模型，而不是先扩框架或加写能力。

## 反问面试官时可展开的点

- 他们的 Agent 系统如何区分模型推理与 deterministic guardrail？
- Tool result 是否先持久化再被模型引用？
- 取消、恢复和重试是否共享同一预算？
- 评测如何隔离标签并控制 Single/Multi-Agent 公平性？
- “inconclusive” 是否是一等结果，还是系统被迫猜答案？

这些问题能自然把讨论带到本项目最强的设计部分。
