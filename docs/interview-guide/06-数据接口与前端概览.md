# 06. 数据、API 与前端

本手册以 Agent 为重点。本章主要帮助你理解 Agent 产物怎样持久化和对外投影；前端只保留能支持面试架构说明的最小概览，不要求逐个组件背诵。

## 1. 两套相互关联的数据

项目同时保存：

1. **业务投影**：事故、证据、候选、报告、建议；
2. **执行审计**：Run、Attempt、Event、Checkpoint、ToolCall、AgentExecution。

前者回答“诊断结果是什么”，后者回答“系统怎样得到这个结果”。

## 2. 主要领域对象

### InvestigationRecord

一次事故调查的聚合根，包含：

- `IncidentEvent`；
- strategy；
- Investigation 状态；
- Evidence、ProviderResult；
- 旧 Hypothesis/Specialist 兼容字段；
- V11 MultiAgentRun summary；
- Report；
- RecommendedAction；
- VerificationSuggestion；
- active Runtime Run owner。

V11 投影要求所有 Evidence、Action、Verification、Report 和 summary 属于同一个 `runtime_run_id`，防止把不同 Run 的结果拼在一起。

### DiagnosisPlan / DiagnosisTask

Lead 的计划与任务。Task 保存：

- 调查目标；
- 信息缺口和预期判别信号；
- 可用工具；
- round；
- Critic assessment 所有权；
- Runtime owner；
- 状态与时间。

### AgentFinding

Investigator 的结构化发现。Finding 区分 observation、candidate、contradiction、gap，记录实体、机制、摘要、理由和 Evidence IDs。

### RootCauseCandidate

根因候选不是一行字符串，它有：

- affected entity；
- failure mechanism；
- onset time/window；
- 支持和反对 Finding IDs；
- 支持和反对 Evidence IDs；
- rank；
- uncertainty。

### CriticAssessment / CoordinationReview

Assessment 记录候选的七项因果检查、verdict 和补证任务。Review 聚合 candidates、assessments、Lead decision、diagnostic status、authority mode 和 summary。current live 路径的 LeadDecision 是服务端对 Critic accepted refs 的权威投影，不代表又调用了一次 Lead 模型。

### IncidentReport

报告是结构化对象加 Markdown 文本。V11 报告必须从 Agent-authority Review 生成，并检查 Evidence 和 Candidate 引用；不会进入旧 Hypothesis 分支。

## 3. SQLite 表怎样组织

[schema.py](../../backend/db/schema.py) 里主要有：

- 业务表：`investigations`、`evidence_items`、`reports`、`recommended_actions`、`verification_suggestions`；
- Agent 表：`diagnosis_plans`、`diagnosis_tasks`、`agent_executions`、`tool_calls`、`agent_findings`、`coordination_reviews`；
- 历史兼容：`hypotheses`、`specialist_results`、`llm_analyses`、`react_traces`；
- Runtime：`runtime_runs`、`runtime_attempts`、`runtime_events`、`runtime_checkpoints`；
- Memory：`memory_items`。

复杂领域对象主要保存在 JSON payload 中，同时提取常用索引字段，例如 investigation_id、task_id、status、created_at。V11 给 `runtime_runs` 增加执行版本、authority 和 execution contract 等强执行字段。

## 4. Repository 模式

业务代码不直接到处写 SQL，而是依赖 Repository：

- `InMemoryInvestigationRepository`：测试专用；
- `SQLiteInvestigationRepository`：默认真实实现；
- `InMemoryRuntimeStore` / `SQLiteRuntimeStore`：Runtime 专用持久化。

好处不是为了“抽象而抽象”，而是：

- 同一业务验证适用于内存和 SQLite；
- 测试可以快速运行；
- Runtime Store 能提供原子 phase/tool/terminal commit；
- 历史 JSON 读取与 schema migration 集中管理。

`memory://` 明确是测试专用，产品默认使用 SQLite。

## 5. API 分组

### 基础和配置

- `GET /health`
- `GET /config/providers`
- `GET /config/agents`

配置 API 只暴露安全、必要的 Provider/model/实现与认证状态，不返回 API key 或原始 endpoint secret。

### 事件入口

- `POST /events`
- `POST /events/alertmanager`
- `POST /events/simulated/{case_id}`
- `POST /investigations/manual`

Alertmanager 一次最多同步处理 100 条告警，firing 创建调查，resolved 忽略；当前是 at-least-once 且不去重，所以重复投递可能创建重复 Investigation。

### 调查结果

- `GET /investigations`
- `GET /investigations/summaries`
- `GET /investigations/{id}`
- `GET /investigations/{id}/timeline`
- `GET /investigations/{id}/evidence`
- `GET /investigations/{id}/report`
- `PATCH .../actions/{action_id}`
- `PATCH .../verifications/{verification_id}`

Action 和 Verification 的状态变化要经过领域状态机；它们仍是人工记录，不触发生产操作。

### Agent 过程

- Plan、Tasks；
- Agent executions；
- Context facts；
- Tool calls；
- Memory；
- Findings；
- Coordination review；
- RCA workbench；
- Task graph。

这些接口让系统的过程可检查，而不是只给最后一段答案。

### Runtime

- 创建/列出 Run；
- 查询 Run 详情、Events、SSE；
- cancel/resume；
- replay；
- diff。

冲突的活动 Run 或恢复竞态返回 409；契约不合法按 API 映射为安全错误。

### Benchmark

产品 API 只提供最新有效冻结的 OpenRCA 摘要和允许下载的工件。RCAEval 标签与本地受控评测工件不通过产品 API 暴露。

## 6. 前端结构

前端使用 React 19、TypeScript、Vite 和 TanStack Query。主要视图：

1. **Investigations**：列表、详情、证据、候选、Agent 过程、建议和报告；
2. **Runtime Workbench**：Run 列表、阶段时间线、Agent swimlanes、事件详情、取消/恢复/重放/对比；
3. **OpenRCA Benchmark**：展示冻结评测摘要与下载。

[frontend/src/api.ts](../../frontend/src/api.ts) 定义与后端对应的 TypeScript 类型和请求函数；[App.tsx](../../frontend/src/App.tsx) 负责调查视图；[RuntimeWorkbench.tsx](../../frontend/src/RuntimeWorkbench.tsx) 负责 Runtime 操作台。

## 7. 前端怎样处理新旧投影

因为历史记录必须可读，页面会区分：

- legacy record；
- 旧固定 Agent/自定义层记录；
- V11 Agent authority 投影；
- active Runtime Run owner 是否匹配。

只有 Review、Run summary 和 Investigation 的 active owner 一致时，才把 V11 候选当成当前有效结果展示。这是 UI 层的防串线，不代替后端完整性校验。

## 8. SSE 前端逻辑

`useRuntimeEvents.ts` 为每个 Run 缓存：

- `lastSequence`；
- 已接收 Events；
- connected 状态。

EventSource 断开后，前端会从 REST `/events?after=...` 补 durable events，再继续连接。事件按 Run 和 sequence 合并，避免页面刷新或网络抖动让时间线断层。

## 9. Report、Action 和 Verification 的边界

V11 报告生成器会：

- 验证 Review 和 Run 都是 Agent authority 且 owner 一致；
- 对外诊断只呈现终态 authority IDs 授权的 diagnoses 和 alternatives；
- 展示 Critic 检查、不确定性和 Evidence IDs；
- 对 `inconclusive` 明确写无激活诊断；
- 校验 Action/Verification 引用已接受 Candidate；
- 脱敏和转义 Markdown。

ActionPlanner 只产生建议。用户可以记录批准、完成和验证结果，但项目没有对应的执行工具。

## 10. Memory 的语义

Memory 不是把过去的原始日志直接塞给模型：

- 人工反馈可以保存为 MemoryItem；
- V11 `lookup_memory` 只返回已验证、同 service/environment、非当前或祖先 Run 的有界历史摘要；
- 外部 Evidence ID 不直接复用，而是生成新的 `verified_incident` Evidence；
- 记录来源 Investigation、候选、验证时间、实体和机制。

这样历史知识也必须重新进入当前证据账本，保持 provenance 和 owner 清晰。

## 11. 面试回答模板

“数据分成业务投影和执行审计。Investigation 聚合 Evidence、V11 Review、Report、Action 等业务对象；RuntimeRun、Attempt、Event、Checkpoint 记录执行。SQLite 对复杂对象保存版本化 JSON，同时把常查询和强约束字段单独成列。Repository 集中处理内存测试和 SQLite 持久化，Runtime Store 提供原子提交。FastAPI 分成事件、调查、Agent 过程、Runtime 和 Benchmark 接口；React 有调查台、Runtime 工作台和 OpenRCA 页面，通过 REST 加按 sequence 可重连的 SSE 展示过程。前后端都会核对 active Runtime owner，避免把不同 Run 的结果混在一起。”
