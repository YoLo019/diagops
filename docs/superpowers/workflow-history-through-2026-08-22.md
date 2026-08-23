# DiagOps 版本演进与工程问题总结

本文压缩记录 DiagOps 从最小 RCA 原型到 V11 Agent 权威诊断平台的演进过程。
它用于理解已经做出的关键选择、开发中遇到的困难以及解决方式，不是当前需求或
发布状态入口。当前工作以 `AGENTS.md`、`current.md` 和其中链接的设计、计划为准。

更新日期：2026-08-22

## 一、始终不变的边界

- 系统定位是基于证据的 SRE 故障诊断，而不是通用聊天或自动修复平台。
- 生产 Provider 和 Agent Tool 只读，不提供 SSH、回滚、重启、扩缩容或配置修改。
- 结论、建议和验证结果必须引用真实 Evidence ID；事实、推断、缺口和冲突分开表达。
- Provider、Tool 和模型输出均视为不可信输入，必须校验、脱敏并以安全失败结束。
- 确定性代码负责安全、契约、预算、持久化、重放和降级，但在 V11 中不能替代 Agent
  生成、排序或改写诊断结论。
- 历史持久化记录必须可读，因此数据库与公共契约采用增量演进；工作流文档本身由
  Git 历史保存，不再为每个版本长期保留一套重复设计和计划。

## 二、版本演进

### V1：证据驱动的最小 RCA 闭环

最初版本完成事件输入、日志/指标/发布/依赖证据采集、规则分析、Markdown 报告和
Golden Case，证明了：

```text
Incident -> Evidence -> Hypothesis -> Report -> Engineer review
```

主要困难是先确定可信边界，而不是扩大功能。系统从第一版就禁止自动生产变更，并把
证据引用作为结论的必要条件。

### V2：从一次性 API 到故障处理流程

V2 引入 Investigation 状态、建议动作、审批状态和验证建议，使诊断结果可以被持续
管理。解决方式是先建立最小状态闭环，不引入执行器或自动审批，避免诊断平台过早变成
生产变更平台。

### V3：真实数据、持久化与前端

V3 将 Mock 为主的原型推进为可接入真实数据的只读平台，加入 SQLite、文件 Provider、
Prometheus、服务目录和前端。

困难在于真实数据不完整、来源不同且生命周期跨请求。解决方式是统一 Provider Result
和 Evidence 契约，以 SQLite 为默认存储，将缺失数据显式表示为缺口，而不是用 Mock
数据冒充生产事实。

### V4：领域化多 Agent 平台骨架

V4 增加任务规划、Agent 路由、执行记录、共享上下文、Tool Call、Memory 和任务图，
形成 Coordinator 与日志、指标、发布等 Specialist 的领域分工。

困难是多 Agent 容易退化为不可审计的自由对话。解决方式是让 Agent 通过结构化任务、
Tool Schema 和持久化 Context 协作，不直接共享无限增长的对话历史；前端只展示安全的
结构化过程。

### V5：Finding、冲突与候选根因

V5 建立独立 Specialist Finding、Coordination Review、候选根因排序和证据链预览。
当时确定性 RCA 仍是权威，LLM 只允许补充表述。

主要困难是不同 Agent 的输出无法直接比较。解决方式是统一 Finding 和 Candidate
契约，显式记录 agreement、conflict、gap 与 evidence references，并把冲突处理集中到
Coordinator。

### V6：单 Agent ReAct 探索

V6 以受限只读 Tool 验证单 Agent ReAct 循环和 Trace 展示，没有替换既有确定性链路。

实践表明，ReAct 可以证明动态 Tool 调用，但单 Agent Trace 不能解决多领域隔离、并发、
故障隔离和最终裁决问题。因此该运行路径后来退役，仅保留历史数据读取所需的契约。

### V7：真实模型驱动的多 Agent 复核

V7 接入 OpenAI Agents SDK，使用三个独立 Specialist 和一个 Coordinator；失败 Agent
不取消同组成功结果，冲突时最多进行一轮定向复核。随后增加 DeepSeek
OpenAI-compatible 适配。

开发中暴露了三类问题：

1. agreement、partial、fallback 在代码、报告、UI 和验收中含义不一致；
2. Provider 失败经常只剩字符串异常，无法判断认证、限流、超时或校验失败；
3. 真实模型输出结构合法不代表业务语义合法。

解决方式是统一决策状态机和结构化失败分类，持久化 Agent/Attempt/Step 审计信息，
并增加 Evidence 所有权、Provider 语义、时间窗和引用完整性校验。

### V8.1：可靠性与 Provider 兼容

V8.1 的重点不是增加 Agent，而是修复可信度基础：真实 Provider 与 Mock 混用、高风险
动作状态跳转、无结果验证、旧 SQLite 未迁移、外键未启用、超时和 Provider 身份不一致。

解决方式包括：

- 生产与 Mock Provider 注册边界分离；
- 动作和验证状态使用显式合法转换；
- SQLite 启动时执行版本检查、增量迁移和外键检查；
- Provider/model/endpoint 身份进入运行与验收契约；
- 失败必须可见为 failed、partial 或 inconclusive；
- 实时验收 artifact 只保存白名单结构化字段，不保存 Prompt、原始响应或凭证。

### V8.2：受预算约束的自主调查

V8.2 让 Specialist 根据证据缺口调用参数化只读 Tool，并建立 Fixed/Adaptive
OpenRCA 对比。

困难是动态调查可能造成重复查询、预算失控、越权参数和无新证据循环。解决方式是加入
Tool allowlist、参数校验、去重、轮次/调用/时间预算、Evidence 增量判定和明确停止原因。

冻结的 40-case 结果表明 Fixed 与 Adaptive 官方得分都为 0；Adaptive 的证据引用合法率
也只有 65.615%。这个结果被保留为失败证据，证明“能自主调用 Tool”不等于“能形成正确
因果诊断”。

### V9：持久、可恢复、可观测的运行时

V9 增加 Runtime Run、Attempt、Phase、Checkpoint、单写者、Lease、取消、恢复、SSE、
OpenTelemetry、Replay 和 Diff，使调查可以并发、崩溃恢复和审计。

最难的部分不是正常执行，而是竞态与失败语义：

- Lease 丢失后旧 Owner 仍可能迟到写入；
- 取消或超时后模型/Tool 结果可能晚到；
- Replay 不能偷偷再次调用模型或 Provider；
- SSE 断开不能影响 Runtime 正确性；
- Token、事件序号和幂等 Tool 结果必须跨恢复保持一致。

解决方式是把持久化状态作为唯一事实源，所有写入经过 RuntimeWriter 和 Owner/Lease
校验；终态阻止迟到提交；Checkpoint 只恢复已批准阶段；Replay 只读持久化事件；
SSE 使用序号重连；故障注入测试覆盖取消、Lease、SQLite 和恢复竞态。

### V10：共享信号语义与双 Gate

V9 的运行时已经可靠，但 OpenRCA 质量仍差。根因不是预算，而是两条输入链语义不一致：
OpenRCA 把原始异常直接提升为 root cause claim，Prometheus 将指标放在 Analyzer
未读取的嵌套字段，生产与 Benchmark 没有共享诊断语义。

V10 建立 Provider-neutral Signal Core、异常分段、Attribution 和薄 OpenRCA Projector，
让 OpenRCA 与生产 Prometheus 使用同一套证据语义，同时增加 Alertmanager 边界和七场景
生产 Gate。

生产 Gate 达到 7/7、Evidence 引用 100%、只读违规 0、无答案泄漏；但冻结六案例
OpenRCA 只有 2/6。这说明共享字段和排序可以修复工程一致性，却不能凭确定性投影补出
缺失的因果语义。

### V10.1：信号排序修复与失败的盲测

六案例分析发现 counter 被当作 gauge、零基线单点噪声被放大、同分时最早 onset
压过高支持度信号。项目修复 counter rate、零基线 strength 和 Attribution 排序，并用
一次性冻结 40-case 配对盲测验证。

候选与基线 official partial 都是 0.025，提升为 0。虽然候选改变了大多数预测，命中数
没有增加。项目停止继续对已揭盲数据调参，也没有宣称准确率提升。

这一失败促成了关键判断：规则权威的 Agent 外壳无法成为通用 SRE Agent，继续优化
固定 CauseType 和投影只会把 Benchmark 偶然性写进生产规则。

### V11：Agent 成为诊断权威

V11 将拓扑改为 Lead、受限并发 Investigator、Critic 和最终 Lead 裁决。Agent 可以选择
调查策略和信息缺口；确定性层只负责 Tool 权限、Evidence 引用、预算、生命周期、
持久化、重放和安全校验。证据不足必须返回 inconclusive。

V11 的工程难点及解决方式：

1. **新旧权威混淆**：旧 Orchestrator 会用确定性 Hypothesis 覆盖 Agent 结果。
   通过 versioned phase profile、authority_mode 和统一 publication guard 隔离 V10
   legacy 与 V11；报告、API、UI、Benchmark 共用同一准入判断。
2. **并发写入与归属**：并行 Investigator 不能直接写业务投影。所有结果先形成
   Agent-owned 草稿，再由单写者持久化；Run、Review、Candidate、Report 必须绑定同一
   active runtime owner。
3. **状态语义漂移**：冻结 complete/completed、partial/partial、
   inconclusive/completed 映射，非法或陈旧组合 fail closed。
4. **Token 与重试重复计数**：区分 reservation、estimate 和 Provider actual usage，
   按 logical call/attempt/request 去重；成功请求计入运行总量，失败估算保留在审计中。
5. **模型输出局部违约杀死整批**：对 Finding、Candidate、Report 分层准入，丢弃单个
   违约输出并记录失败；只有整批无有效结果时才终止 Run，不通过放宽 Evidence 契约救活
   错误结果。
6. **离线开发与真实集成冲突**：九个 Agent Tool 都必须有确定性本地 Provider；
   Tempo 是可选集成，Docker 不可用时不伪造通过；正式模型端点必须先通过 capability
   admission。
7. **评测泄漏与不可复现**：Runtime 包与 Label 包隔离，Prediction 进程看不到
   scorer/label 路径；冻结 source revision、manifest、prediction hash、capability 和
   pair ledger，开封标签后失败不可静默重试。

M1-M4 的工程和产品集成 Gate 已通过多轮独立复审。M5 首次 SS30 正式尝试暴露 GAP
Evidence、Candidate 准入、Single 路径和预算终态等跨层契约遗漏；这些问题通过离线回放、
最小 RED 复现和共享校验器逐层修复，没有放宽 bundle 必须全量 completed 的规则。

正式评测随后从 SS30 修订为 SS15：上游 Sock Shop 仍校验完整 90 cases，本地按冻结
seed 选择 15 个服务/故障单元格，每格一个 repetition；OB30 和 TT90 不变。新协议使用
新的 manifest、custodian root 和 ledger，不拼接旧 epoch。

截至 2026-08-22，V11 仍阻塞在新的 clean HEAD capability artifact 和正式 SS15/TT90
授权运行。没有完成正式准确率 Gate，因此不能声称 V11 已发布或准确率已提升。

## 三、反复出现的工程困难与通用解法

| 困难 | 根因 | 最终采用的解法 |
| --- | --- | --- |
| 测试通过但生产事实不可信 | Mock、Provider、持久化和 UI 契约各自演进 | 共享 Domain Contract，生产/Mock 边界分离，跨层契约测试 |
| 模型输出格式正确但语义错误 | JSON Schema 只能约束形状 | Evidence ownership、时间、范围、状态和引用语义校验 |
| Agent 失败被静默替换 | 确定性 fallback 同时承担权威 | V11 authority_mode 隔离，失败显式化，确定性层只拒绝不改写 |
| 并发、取消和恢复出现迟到写 | 内存状态被当作真相 | SQLite 状态机、Lease、单写者、幂等键和终态写屏障 |
| Benchmark 提升无法复现 | 数据泄漏、脏工作树、重复调参 | 冻结 manifest/source/hash、预测与标签隔离、一次性 ledger |
| Agent 自主性导致成本和循环 | Tool 调用无硬边界 | 参数化只读 Tool、去重、轮次/调用/Token/Deadline 预算 |
| Provider 差异污染业务代码 | Endpoint 能力被假设而非验证 | Provider-neutral 契约、显式 identity、capability admission |
| 历史兼容拖累新架构 | 旧持久化记录必须可读 | 数据契约增量演进；V10/V11 versioned phase 和 authority 隔离 |
| 文档与状态不断漂移 | 每个版本保留大量重复计划和流水 | 当前路由 + 当前权威设计/计划 + 本总结，细节交给 Git 历史 |

## 四、当前保留的权威资料

- 当前状态：`docs/superpowers/current.md`
- V11 主设计：`docs/superpowers/specs/2026-08-02-diagops-v11-adaptive-multi-agent-rca-design.md`
- V11 实施计划：`docs/superpowers/plans/2026-08-02-diagops-v11-adaptive-multi-agent-rca-implementation-plan.md`
- SS15 修订设计：`docs/superpowers/specs/2026-08-19-rcaeval-ss15-protocol-design.md`
- SS15 修订计划：`docs/superpowers/plans/2026-08-19-rcaeval-ss15-protocol-implementation-plan.md`

更细的旧版本设计、逐任务命令、评审流水和失败 artifact 身份可从 Git 历史恢复，
不再作为日常 Agent 上下文。
