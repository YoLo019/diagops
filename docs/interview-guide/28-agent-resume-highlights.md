# 28. Agent 岗位简历亮点素材：DiagOps 后端项目

这份文档把 DiagOps 当前 V11 实现整理成 Agent 开发岗位可用的简历素材。它只写后端、Agent、Runtime、Tool、Evidence、安全和评测，不含前端内容。

## 使用前必读：不要把建议写成事实

- 只有你亲自负责或能在面试中讲清源码、设计和取舍的内容，才使用“负责/设计/实现”；参与过但并非主责的内容改成“参与/协助”。
- `[X]`、`[X ms]`、`[X%]` 一律等拿到真实埋点、压测或评测结果后填写，不能编造。
- 当前正式 SS15/TT90 效果评测未完成，不能写“V11 已发布”“准确率提升 X%”。
- 当前没有 MCP、向量 RAG、动态模型路由、自动修复或生产写工具；不要把“前沿方案”写成项目现状。
- V11 工具 manifest 冻结的是有序工具名及其 hash；同名工具的 schema/handler hash 是后续增强方向，不能夸大为已实现。

## 项目一句话

DiagOps 是一个只读的 SRE 事故诊断后端：以持久化 Runtime 驱动 Lead、Investigator、Critic 多 Agent 协作，在预算内使用九个受控只读工具收集 Evidence，最终输出带证据引用、可恢复、可审计的根因候选。

## 50 条可选项目亮点

下面不是让你全部塞进一份简历。它是面试素材库：投递时通常选 4–6 条主亮点，面试追问时再展开对应编号。

### A. Multi-Agent 架构与诊断权威

| # | 可写成的亮点 | 面试展开点 |
| ---: | --- | --- |
| 01 | 设计 Lead → Investigator → Critic 的受控 Multi-Agent 诊断链路，将规划、取证、因果审查和发布拆分为明确职责。 | `V11Runtime`、`V11_PHASE_ORDER` |
| 02 | 将 Agent 的语义诊断权与确定性 Runtime 控制权分离：模型负责假设与判断，代码负责权限、预算、状态、持久化和校验。 | 为什么 Validator 不能生成根因 |
| 03 | 实现 Lead 按“信息缺口”而非日志/指标数据模态拆分调查任务，避免固定专家带来的观察偏差。 | 任务最多 3 个，服务端生成 Task ID |
| 04 | 实现最多 3 路 Round 1 Investigator 隔离并行，避免兄弟 Agent 相互锚定、复制第一个结论。 | 每个实例独立 Task、Evidence digest、ToolSession |
| 05 | 让 Investigator 使用同一份受控工具能力，但限制其上下文可见范围，平衡调查多样性与权限一致性。 | 共享预算，不共享兄弟 Finding |
| 06 | 设计 Critic 专职审查候选，不提供自由取证工具，避免调查者与裁判角色混同。 | 需要补证时只创建有归属的 Round 2 Task |
| 07 | 为每个候选实现 temporal、topology、mechanism、blast radius、symptom-vs-cause、counterevidence、alternatives 七项因果检查。 | `CausalCheckName` 与 `CriticAssessment` |
| 08 | 将补证限制为最多一轮，并要求 Round 2 Task 绑定发起它的 CriticAssessment，防止开放式反思循环。 | reconciliation 后禁止第三轮 |
| 09 | 在 live 路径中将 Critic accepted candidate 机械投影为 authority result，避免重复 Lead 模型调用造成随机分歧和额外成本。 | `lead_adjudication` 是职责名，不必然是第二次模型调用 |
| 10 | 拒绝多数投票作为根因权威机制，使用独立取证、证据引用和 Critic 反证检查降低同模型相关偏差。 | 多个 Agent 不等于独立样本 |

### B. Context engineering、结构化输出与 Agent 状态

| # | 可写成的亮点 | 面试展开点 |
| ---: | --- | --- |
| 11 | 按角色构建最小上下文投影，避免把完整 ORM 对象、服务器拥有字段和无关 Evidence 直接送入模型。 | incident/task/evidence 的 live projection |
| 12 | 实现确定性 Evidence digest：Round 1 默认只给模型 4 条起始证据、每 kind 最多 2 条，完整 ledger 仍保存在数据库。 | digest 不是 LLM 摘要，也不是删库 |
| 13 | 按实体、`signal_family`、`change_score` 和非指标锚点选择 digest，避免单一高分 CPU/latency 信号挤掉 socket、日志或 Trace。 | `_select_evidence_digest` |
| 14 | 为 Critic 构造候选/Finding/Assessment 的 Evidence 引用闭包，减少无关工具结果重复进入 prompt。 | `_critic_evidence` |
| 15 | 设计 compact live schema，只让模型填写诊断语义；Task ID、Candidate ID、owner、rank、预算等由服务端持有。 | 防止并行 Agent 主键冲突和状态伪造 |
| 16 | 使用 Pydantic `extra="forbid"` 和有界字段约束模型输出与工具参数，拒绝额外字段、非法枚举和非有限数值。 | 结构正确不等于诊断正确 |
| 17 | 在结构化输出边界递归拒绝 ASCII 控制字符和 DEL，避免日志/JSON/报告展示被不可见字符污染。 | `_SafeStructuredOutput` |
| 18 | 采用输入 token 启发式估算、请求前 reservation、真实 usage 结算，避免并行模型调用超卖总预算。 | 估算不是精确 tokenizer |
| 19 | 将 model turn 设计为 Run 级耐久预算，retry/resume 继承剩余额度，避免进程重启获得“免费调用”。 | `remaining_model_turns` + checkpoint |
| 20 | 不持久化私有 Chain-of-Thought，只保留任务、工具、Evidence refs、检查项、短摘要、usage 和失败类别。 | 审计性与敏感性之间的取舍 |

### C. Tool Calling、Provider 与 Evidence ledger

| # | 可写成的亮点 | 面试展开点 |
| ---: | --- | --- |
| 21 | 构建 ToolSpec → ToolRegistry → ProviderRegistry → EvidenceItem 的分层工具架构，解耦 Agent 接口和具体数据源。 | 同一 `query_traces` 可对接文件或 Tempo Provider |
| 22 | 设计九个 Agent 可见的只读工具，覆盖日志、指标、发布、服务目录、依赖、Trace、运行状态、告警和 verified memory。 | `query_prometheus` 仅为 internal 兼容别名 |
| 23 | 在 Run 创建时冻结有序工具名 manifest/hash，并在每次调用时复核工具存在、Agent exposure 和 read-only 属性。 | names-only hash 的真实边界 |
| 24 | 使用 provider-neutral 查询模型，将 PromQL、URL、文件路径等供应商/执行细节隐藏在 Provider 层。 | 不允许 Agent 任意 Shell、SSH、SQL 或 PromQL |
| 25 | 对工具参数执行事故时间窗、实体 scope、依赖深度、枚举、条数和长度限制，阻断越权或超大查询。 | `QueryWindow`、`ScopedTelemetryQuery` |
| 26 | 压缩模型可见的工具 JSON Schema，并由服务端注入时间窗、`reason`、`limit` 等固定字段，减少 prompt token 而不放松完整校验。 | `_compact_json_schema`、`_server_query_defaults` |
| 27 | 在 Provider I/O 前持久化 `ToolCallRecord=running`，让崩溃、取消和恢复均保留完整审计轨迹。 | 先写状态，再跨外部边界 |
| 28 | 使用 query fingerprint 防止重复查询；以 logical call 区分一次业务动作和多次网络 attempt。 | `reason` 不参与重复判定 |
| 29 | 通过 idempotency key 复用完全相同 key 的 durable success，避免恢复后重复访问外部系统。 | retry 的 key 含新 attempt；不要把二者混为一谈 |
| 30 | 对 Provider transport/rate-limit 实施有界工具重试，对裸 TimeoutError 不盲目重发，降低远端仍执行时的重入风险。 | 工具与模型 retry 路径不同 |
| 31 | 将 ToolCall、ProviderResult、Evidence 与 Runtime Event 原子持久化，只有已提交 Evidence ID 才允许模型引用。 | Evidence ledger 是诊断事实源 |
| 32 | 为 Evidence 建模 provider、kind、status、scope、provenance、runtime owner 和结构化 payload，支持引用、审计和 Provider 可替换。 | `success/partial` 才能支撑正向结论 |
| 33 | 区分可用证据与已提交失败证据：前者用于候选因果判断，后者可用于证明 Evidence gap。 | `validate_usable_evidence` vs `validate_committed_evidence` |
| 34 | 实现 no-new-evidence stop：成功调用没有新增 Evidence 时停止该 Investigator，避免无效工具循环。 | 证据产出而非调用次数是效率指标 |

### D. Memory、可靠性与 durable Runtime

| # | 可写成的亮点 | 面试展开点 |
| ---: | --- | --- |
| 35 | 构建 verified memory 查询链路，只允许同 service/environment 的已验证历史事故作为可引用 Evidence。 | 当前不是向量 RAG |
| 36 | 为历史记忆增加来源候选、验证人、验证时间、未来信息和当前/祖先 Investigation 排除规则，降低 memory poisoning 与泄漏风险。 | `VerifiedMemoryLookup` |
| 37 | 设计 Investigation、RuntimeRun、RuntimeAttempt、RuntimeEvent、RuntimeCheckpoint 四层运行模型，支持可恢复执行。 | 业务对象与执行对象分离 |
| 38 | 使用 lease owner/version 作为 execution fence，拒绝旧执行者、取消后或 deadline 后返回的 late result。 | 先后两次 `check_execution` |
| 39 | 以版本化 V10/V11 phase profile 隔离历史 deterministic 路径和 V11 Agent authority，保持历史记录可读。 | V11 是线性阶段 + 有界并行 fan-out |
| 40 | 使用 Checkpoint CAS 和 projection digest 防止 phase 重复提交、越级执行或恢复时业务投影串线。 | phase 不是普通 `while` 循环 |
| 41 | 通过 RuntimeWriter 串行化 SQLite 业务写入，将外部 I/O 并行和短事务提交分离。 | 单 Writer 不是把全部计算串行化 |
| 42 | 实现 interrupted/resume/replay 语义：resume 继续未完成 Run，replay 仅验证历史状态且不调用模型或 Provider。 | replay 不能消耗 token |
| 43 | 将取消、超时、partial、inconclusive、failed 明确建模，证据不足时安全拒绝发布根因而不是猜答案。 | `inconclusive` 是合法结果 |
| 44 | 基于 durable RuntimeEvent sequence 实现 SSE catch-up；内存通知只唤醒订阅者，数据库才是事件真相源。 | 断线使用 after sequence 补齐 |

### E. 模型能力、安全、可观测性与评测

| # | 可写成的亮点 | 面试展开点 |
| ---: | --- | --- |
| 45 | 为 OpenAI-compatible endpoint 实现 capability certification，验证非流式响应、tool call、结构化输出、usage、deadline、并发和 V11 role schema。 | 协议可用不等于诊断质量可用 |
| 46 | 将 provider/model/endpoint identity、工具名 manifest、Skill catalog、预算、topology 和 retry policy seal 到 execution contract，恢复时不允许无声换模型或换工具名集合。 | 官方 OpenAI 与 compatible admission 条件不同 |
| 47 | 设计 native JSON Schema 与 strict output tool 两种结构化 transport，兼容不同 Chat Completions endpoint 的能力差异。 | 兼容端点需通过 capability artifact |
| 48 | 对模型、Provider、Tool 和持久化失败使用安全 failure category，而不是把原始异常、URL 或凭证暴露到 API/日志。 | timeout、transport、rate-limit、invalid-output 等 |
| 49 | 实现敏感字段 redaction、endpoint canonicalization、最小上下文投影和只读工具边界，抵抗 prompt injection、数据泄漏与 excessive agency。 | prompt 不是安全边界 |
| 50 | 建立 RCAEval 单/多 Agent、intended/equal-token 对照、标签隔离、Evidence audit、paired bootstrap/McNemar 的受控评测框架。 | 正式 SS15/TT90 尚未完成，不能写效果提升 |

## 推荐写进简历的亮点

### 最推荐的 6 条：投递 Agent 后端/AI Infra 岗位

如果这些确实是你的主责内容，优先使用下面六条；它们能同时体现 Agent、后端工程、安全和可靠性。

1. **受控 Multi-Agent 编排**：设计 Lead–Investigator–Critic 诊断工作流，将信息缺口规划、隔离取证、因果反证与权威发布拆分为持久化阶段，避免自由 Agent 群聊的状态和成本失控。
2. **Agent Tool / Evidence 闭环**：构建九个只读工具的 ToolRegistry–ProviderRegistry–Evidence ledger 链路；工具调用经 schema、scope、预算、幂等和 deadline 校验，Evidence 持久化后才允许模型引用。
3. **Durable Agent Runtime**：实现 Run/Attempt/lease/checkpoint/Replay 运行模型，借助单 Writer、CAS、execution fence 和幂等复用处理崩溃恢复、取消、重试和 late result。
4. **Context engineering**：按角色构建最小 prompt 投影，设计实体/signal-family 感知的 Evidence digest 和 Critic 引用闭包，在固定 token 预算下保留高区分度证据。
5. **模型能力准入与结构化输出**：实现兼容模型 endpoint 的 tool call、strict schema、usage、并发、deadline 等 capability certification，并冻结 execution contract 防止恢复时模型/工具漂移。
6. **安全与评测**：以只读 manifest、脱敏、同 Run Evidence 引用和标签隔离保证 Agent 安全；构建 Single/Multi/equal-token 对照和 Evidence audit，避免只看最终答案。

### 按岗位选择的补充亮点

| 岗位方向 | 优先追加 |
| --- | --- |
| Agent 平台 / AI Infra | 18、19、37–44、45–47 |
| LLM 应用 / Agent 后端 | 01–17、21–34、49–50 |
| 分布式系统 / 后端基础设施 | 27–34、37–44、48 |
| 安全 / 可信 AI | 23–33、35–36、48–50 |
| 数据 / 评测工程 | 13–14、31–33、45、50 |

### 不建议写入简历的表述

- “实现 MCP/RAG/动态模型路由/自动修复”：当前没有这些产品能力。
- “V11 准确率提升 X%”“正式上线”：SS15/TT90 尚未完成。
- “全链路 exactly-once”：外部网络调用无法诚实承诺；应写幂等、durable commit 和 late-result fence。
- “工具 schema/handler 全量冻结”：当前仅冻结有序工具名 hash，并在调用时复核属性。
- “所有 OTel 字段都是低基数”：Run/Attempt ID 用于 trace 关联，不应当作 metric label；当前也没有统一模型定价写入。

## 推荐的简历成稿（可直接改日期和真实数据）

**DiagOps｜Agent 后端开发项目｜20XX.XX–至今**

- **项目简介：** 面向应用服务事故诊断场景，构建只读的 Multi-Agent 根因分析后端；以 Lead 规划、隔离 Investigator 取证、Critic 因果审查为核心，通过受控工具、Evidence ledger 和 durable Runtime 输出可追溯诊断结果。
- **数据与效果（取得真实数据后择 1–3 项填写）：**
  - 覆盖 **[X]** 个事故案例、**[X]** 类日志/指标/Trace/依赖 Evidence，单次 Run 平均 **[X]** 次模型调用、**[X]** 次工具调用。
  - Agent Run p95 耗时 **[X ms/s]**，单 Run Token 消耗 **[X]**，有效 Evidence 产出率 **[X%]**，重复查询拒绝率 **[X%]**。
  - 在冻结的 Single/Multi 对照中，完成率 **[X%]**、Evidence 引用合法率 **[X%]**；仅填写已完成、可复现的真实评测数据。
- **我的职责：** 负责 Multi-Agent Runtime、受控 Tool Calling、Evidence 证据链和可靠性边界的后端实现：
  - **Multi-Agent 编排：** 设计 Lead → 最多 3 个隔离 Investigator → Critic 的持久化阶段流；以信息缺口驱动任务拆分，Critic 对候选执行七项因果/反证检查，最多允许一轮有归属的补证。
  - **Tool Calling 与 Evidence：** 构建 ToolRegistry–ProviderRegistry–Evidence ledger 分层；对九个只读工具执行 Pydantic schema、事故时间窗、实体 scope、工具预算、deadline、重复查询和幂等校验，确保 Evidence 落库后才被模型引用。
  - **Context 与结构化输出：** 构建角色最小上下文投影、实体/signal-family 感知 Evidence digest 和 compact schema；服务端拥有 ID、owner、预算等字段，降低上下文噪声、模型伪造状态和并行主键冲突。
  - **Durable Runtime：** 实现 Run/Attempt/lease/checkpoint/Replay 机制；通过 RuntimeWriter、phase CAS、reservation、execution fence 和 late-result 拦截，处理并发、超时、取消、重试和崩溃恢复。
  - **模型准入与安全：** 实现 OpenAI-compatible endpoint 的 capability certification，验证 tool call、strict structured output、usage、并发与 deadline；结合敏感信息脱敏、只读权限、同 Run 引用校验和安全失败分类，避免 Agent 越权与数据泄漏。
  - **评测与可靠性：** 构建标签隔离的 Single/Multi/equal-token 对照、Evidence audit 和 paired 统计框架；将正确性、引用完整性、Token、延迟、只读违规和 failure category 分开评估。

## 面试时的 30 秒版本

“我做的是一个只读的 SRE Multi-Agent 诊断后端。核心不是让多个模型自由聊天，而是由 durable Runtime 驱动 Lead 规划、隔离 Investigator 取证、Critic 做七项因果审查。模型只能调用九个冻结的只读工具，工具结果先经 schema、scope、预算、幂等和持久化处理，生成 Evidence ID 后才允许引用。系统还实现了 Run/Attempt/lease/checkpoint/replay，能处理超时、取消、重试和恢复；我把模型能力准入、上下文压缩、安全脱敏和 Single/Multi 对照评测也纳入了同一条工程链路。”

## 附录：你提供的量化交易简历参考样式

**量化交易系统｜Java 后端开发实习生｜20XX.XX–20XX.XX**

- **项目简介：** 面向贵金属量化交易场景，提供策略订单接入、自动报价、风控预占、撮合平盘、外部交易执行、成交回报、交易数据同步和日终对账能力，支撑策略系统与外部市场之间的完整交易闭环。
- **数据与效果（取得真实数据后择 1–3 项填写）：**
  - 覆盖[策略订单/成交回报/交易文件]日均 **[X]** 笔（峰值 **[X]** 笔/分钟），服务于 **[X]** 个策略、**[X]** 个交易品种或 **[X]** 家机构。
  - 外部交易文件单文件最大 **[X] MB/GB**、单日 **[X]** 条明细；同步任务耗时 **[X] 分钟**，批量入库吞吐 **[X] 条/秒**。
- **我的职责：** 负责贵金属自动报价，积存金日终对账，自动平盘的后端链路功能实现与一致性方案设计：
  - **交易执行链路：** 负责策略订单从参数校验、风险检查、撮合决策、交易生成到外部执行和成交回报的链路整理；基于订单级锁、状态机和幂等校验，避免重复处理和状态错误推进。
  - **自动报价与风控：** 设计自动报价责任链，拆分资格校验、行情读取、价格计算、机构额度/期限笔数预占、EAIP 发送和结果落库；异常时回退报价状态并释放已占用的风控资源。
  - **交易数据基础设施：** 实现 GZIP 交易文件流式解析和 FLG 配置化字段映射，采用 20000 条聚合、1000 条分批、CompletableFuture 并发入库及 CountDownLatch 超时控制，保障日终交易数据完整入库。
  - **缓存与并发控制：** 构建订单、交易和请求 ID 的内存—Redis—数据库三级缓存，使用 ConcurrentHashMap、队列串行化和版本号控制异步同步，并支持服务重启后的 Redis 数据恢复。
  - **分布式一致性：** 针对风控数据库事务与外部状态推送时序不一致问题，设计 Transactional Outbox 方案，覆盖事务内事件落库、提交后分发、原子抢占、失败重试、消费幂等、消息乱序保护和卡死任务恢复。
