# DiagOps V4 中文多 Agent 运维诊断平台设计

## 1. 背景

DiagOps V3 已经完成一条可运行的只读运维诊断链路：

```text
事件 / 手工描述
  -> 创建 investigation
  -> 采集日志、指标、发布、服务目录等证据
  -> RCA 规则分析
  -> 生成建议动作、验证建议和 Markdown 报告
  -> 前端查看与记录审批 / 验证状态
```

V4 的目标不是重写 V3，而是在 V3 上补一层更接近 OpenDerisk 思路的多 Agent 调度框架，并把前端主要体验切换为中文。

本版继续保持只读边界：

1. 不自动执行回滚、重启、扩容、配置变更。
2. 不通过 SSH 自动执行命令。
3. 不让 LLM 脱离证据直接下结论。
4. 所有结论、建议和追问必须能追溯到 evidence / context。

## 2. 长期目标

长期目标仍然是一个面向运维故障处理的平台型 Agent：

```text
告警、报错、日志或工程师描述进入平台
  -> Agent 自动规划诊断任务
  -> 路由给日志、指标、发布、依赖、服务目录、知识库等 specialist agent
  -> 并行收集证据并写入共享上下文
  -> 上层 coordinator 汇总、裁剪、冲突检查
  -> RCA agent 生成候选根因
  -> LLM analyst 只基于证据补充缺失信息、风险和追问
  -> 前端用中文展示任务图、证据链、Agent 过程和报告
  -> 工程师记录判断、审批和验证结果
```

V4 是这个长期目标中的“多 Agent 调度与中文平台体验”阶段。

## 3. 参考样本借鉴

### 3.1 OpenDerisk 借鉴点

OpenDerisk 对本项目最有价值的不是某个具体 SDK，而是多 Agent 框架形态：

1. 任务规划：先把问题拆成可执行诊断任务，而不是直接问一个大模型。
2. Agent 路由：根据任务类型路由到不同 specialist。
3. 共享上下文：所有 Agent 写入同一个 investigation context。
4. 并行 / 分层执行：底层 specialist 并行采集，上层 coordinator 汇总裁剪。
5. 工具系统：Agent 不直接访问世界，通过声明式 tool 调用 provider。
6. 记忆：保留服务历史、过去 investigation、人工反馈。
7. 可视化协议：把规划、执行、证据、结论表达成前端能稳定渲染的数据结构。

V4 借鉴这些形态，但不引入大型多 Agent SDK。原因很简单：当前系统规模还不需要。先用现有 FastAPI + Python domain model 做最小多 Agent 运行时，等任务图、工具协议和上下文模型稳定后，再评估是否接入更重的框架。

### 3.2 ITOps Agent Platform 借鉴点

ITOps Agent Platform 更适合作为运维产品形态参考：

1. 以平台为中心承接事件。
2. 记录每次诊断的状态、步骤和历史。
3. 让工程师能补充上下文。
4. 让 Agent 输出可复盘过程，而不是只输出最终答案。
5. 后续支持人机协作、审批和半自动处理。

V4 重点吸收它的平台化体验，不做自动修复。

## 4. V4 范围

### 4.1 做什么

1. 前端主要中文化。
2. 新增多 Agent 任务规划模型。
3. 新增 Agent 路由与执行记录。
4. 新增共享上下文模型。
5. 新增工具调用协议。
6. 新增任务图 / Agent 过程可视化 API。
7. 新增基础记忆模型：服务历史、历史 investigation 摘要、人工反馈。
8. LLM analyst 仍保持只读，可选启用。

### 4.2 不做什么

1. 不接入生产 SSH 执行。
2. 不做自动回滚、自动重启、自动扩容。
3. 不做复杂工作流引擎。
4. 不引入 CrewAI / LangGraph / OpenDerisk runtime 作为核心依赖。
5. 不做大而全的知识库检索系统。
6. 不做权限体系和多租户。

ponytail: V4 先把协议和数据流跑通，复杂运行时等真实场景证明需要再加。

## 5. 中文前端目标

V4 前端默认中文，英文仅保留必要技术字段，例如 service、environment、provider、cause_type。

### 5.1 页面结构

前端保留 V3 三栏工作台，但文案中文化：

1. 左栏：新建诊断、诊断列表。
2. 中栏：诊断详情、证据链、候选根因。
3. 右栏：Agent 执行过程、建议动作、验证建议、诊断报告。

### 5.2 新增视图

1. 任务规划视图：展示 planner 拆出的任务。
2. Agent 执行视图：展示每个 Agent 的状态、输入、输出、耗时、错误。
3. 工具调用视图：展示 provider/tool 调用结果。
4. 共享上下文视图：展示被写入 context 的关键事实。
5. 记忆视图：展示命中的历史 investigation 和人工反馈。

### 5.3 中文文案原则

1. “建议动作”不能写成“执行动作”。
2. “审批”只能表示记录人工审批状态。
3. “验证”只能表示记录验证结果。
4. 所有自动化相关文案必须明确“只读”。
5. 错误信息尽量中文解释，保留原始英文错误作为 details。

## 6. 多 Agent 框架设计

V4 引入一个轻量多 Agent runtime：

```text
IncidentEvent
  -> TaskPlanner
  -> AgentRouter
  -> AgentExecutionEngine
  -> SharedInvestigationContext
  -> RCA / Report / LLM Analyst
```

### 6.1 TaskPlanner

输入：

1. IncidentEvent
2. service catalog
3. 可用 providers / tools
4. 历史 memory 摘要

输出 `DiagnosisPlan`：

```text
plan_id
investigation_id
tasks[]
created_at
```

每个 `DiagnosisTask`：

```text
id
title
description
task_type
agent_name
tool_names[]
depends_on[]
priority
status
created_at
started_at
completed_at
```

V4 先用规则规划：

1. 所有事件默认查日志、指标、发布、服务目录。
2. 如果描述包含 timeout / dependency，增加依赖检查。
3. 如果描述包含 deploy / release / rollback，增加发布检查优先级。
4. 如果描述包含 qps / traffic / spike，增加指标检查优先级。

### 6.2 AgentRouter

根据 `task_type` 路由：

```text
log_investigation       -> LogAgent
metric_investigation    -> MetricAgent
deployment_check        -> DeploymentAgent
dependency_check        -> DependencyAgent
service_context         -> ServiceCatalogAgent
memory_lookup           -> MemoryAgent
rca_synthesis           -> RcaAgent
llm_review              -> LlmAnalystAgent
```

Router 不做业务判断，只做映射和兜底。

### 6.3 AgentExecutionEngine

V4 支持两种执行模式：

1. 并行执行无依赖 specialist tasks。
2. 分层执行 synthesis tasks，例如 RCA 必须等基础证据任务结束。

最小实现：

1. 使用 Python 标准库 `concurrent.futures` 或同步循环。
2. 默认同步，配置开启并行。
3. 每个任务独立记录 status / error / duration。

不引入队列系统。等任务运行时间超过单进程承受范围，再考虑 Celery / Dramatiq / RQ。

### 6.4 SharedInvestigationContext

共享上下文是 V4 核心。所有 Agent 不直接互相传对象，而是写入 context。

```text
investigation_id
facts[]
evidence_ids[]
agent_notes[]
tool_calls[]
memory_hits[]
conflicts[]
updated_at
```

`ContextFact`：

```text
id
source_agent
fact_type
summary
confidence
evidence_ids[]
created_at
```

约束：

1. fact 必须引用 evidence，除非 fact_type 是 `missing_evidence`。
2. confidence 必须在 0 到 1。
3. 同一 Agent 可以追加 fact，但不能覆盖其他 Agent fact。

### 6.5 Tool System

V4 把现有 provider 包装成 tool。

`ToolSpec`：

```text
name
description
input_schema
output_schema
read_only
provider
```

`ToolCallRecord`：

```text
id
task_id
agent_name
tool_name
input
status
output_evidence_ids[]
error_message
started_at
completed_at
duration_ms
```

V4 内置 tools：

1. `read_logs`
2. `query_metrics`
3. `read_deployments`
4. `read_service_catalog`
5. `query_prometheus`
6. `lookup_memory`

所有工具默认 read_only=true。

### 6.6 Memory

V4 记忆先做最小版本：

1. 历史 investigation 摘要。
2. 服务最近 N 次事故摘要。
3. 人工反馈：根因是否正确、建议是否有用、验证是否通过。

`MemoryItem`：

```text
id
service
environment
memory_type
summary
source_investigation_id
tags[]
created_at
```

V4 不做向量库。先用 SQLite 条件查询：

1. service + environment
2. cause_type
3. tags
4. created_at desc

ponytail: SQLite 查询够用；等历史数据多到检索慢，再加向量检索。

### 6.7 可视化协议

前端不要猜后端内部结构。V4 新增稳定 visualization API：

```text
GET /investigations/{id}/agent-timeline
GET /investigations/{id}/task-graph
GET /investigations/{id}/context
GET /investigations/{id}/tool-calls
GET /investigations/{id}/memory
```

`TaskGraphResponse`：

```text
nodes[]
edges[]
```

node：

```text
id
label
type
status
agent_name
started_at
completed_at
```

edge：

```text
source
target
relation
```

前端只渲染这些协议，不依赖后端 class 名。

## 7. 后端演进

### 7.1 Domain Models

新增：

1. `DiagnosisPlan`
2. `DiagnosisTask`
3. `AgentExecution`
4. `SharedInvestigationContext`
5. `ContextFact`
6. `ToolSpec`
7. `ToolCallRecord`
8. `MemoryItem`

优先放在 `backend/domain/`，不要先抽象成框架包。

### 7.2 Services

新增：

1. `backend/diagnosis/planner.py`
2. `backend/diagnosis/router.py`
3. `backend/diagnosis/execution_engine.py`
4. `backend/diagnosis/context_store.py`
5. `backend/tools/registry.py`
6. `backend/memory/store.py`

每个服务先一个实现，不做 interface。

### 7.3 Persistence

SQLite 新增表：

1. `diagnosis_plans`
2. `diagnosis_tasks`
3. `agent_executions`
4. `context_facts`
5. `tool_calls`
6. `memory_items`

所有表保留 JSON payload，少量索引用于列表查询：

1. `investigation_id`
2. `service`
3. `environment`
4. `created_at`
5. `status`

### 7.4 API

新增：

```text
GET /investigations/{id}/plan
GET /investigations/{id}/tasks
GET /investigations/{id}/agent-executions
GET /investigations/{id}/context
GET /investigations/{id}/tool-calls
GET /investigations/{id}/memory
POST /investigations/{id}/feedback
```

反馈只记录，不反向改历史结论。

## 8. 前端演进

### 8.1 中文化范围

V4 中文化：

1. 顶部标题。
2. 新建诊断表单。
3. 诊断列表。
4. 诊断详情。
5. 证据链。
6. 候选根因。
7. Agent 过程。
8. 建议动作。
9. 验证建议。
10. 报告。

保留英文：

1. provider 枚举值。
2. cause_type 枚举值。
3. 原始日志 / 错误 / payload。

### 8.2 新增组件

1. `TaskGraphPanel`
2. `AgentTimelinePanel`
3. `ContextFactsPanel`
4. `ToolCallsPanel`
5. `MemoryHitsPanel`
6. `FeedbackPanel`

不引入图形库。V4 先用 CSS + 列表 + 简单连线表达任务图。

ponytail: 图可视化先用列表和缩进，等节点多到看不懂再引入图组件库。

## 9. 测试策略

### 9.1 后端测试

1. planner 对不同事件生成不同任务。
2. router 把 task_type 路由到正确 Agent。
3. execution engine 记录成功、失败、跳过。
4. context fact 必须引用存在 evidence。
5. tool call 失败不会让整个 investigation 崩。
6. memory lookup 只返回同 service / environment 的历史。
7. visualization API 返回稳定结构。

### 9.2 前端测试

Smoke test 继续保持轻量：

1. 页面包含中文主文案。
2. 不出现“自动执行回滚 / 重启 / 扩容 / 配置变更”。
3. Agent timeline / task graph / context 面板存在。
4. API base 仍支持 Vite proxy。

不在 V4 引入复杂 e2e 测试框架。

## 10. 验收标准

V4 完成时应满足：

1. 前端主要 UI 文案为中文。
2. 每次 investigation 有 plan / tasks / agent executions。
3. specialist tasks 能写入共享 context。
4. tool calls 有持久化记录。
5. RCA 报告仍能追溯 evidence。
6. 记忆能展示同服务历史 investigation 摘要。
7. 前端能展示 Agent 过程，而不是只展示最终报告。
8. 全量测试通过。
9. 前端 build 通过。
10. 仍保持只读安全边界。

## 11. 建议实施顺序

1. 中文化前端文案。
2. 增加 DiagnosisPlan / DiagnosisTask domain。
3. 增加 planner 和 router。
4. 将现有 provider 包装成 tools。
5. 增加 execution record 和 tool call record。
6. 增加 shared context。
7. 增加 SQLite 持久化。
8. 增加 visualization API。
9. 增加前端 Agent timeline / task graph / context 面板。
10. 增加 memory lookup 和 feedback。

## 12. 风险

1. 多 Agent 概念过早复杂化。
   - 控制方式：只做一个轻量 runtime，不引入大框架。
2. 前端任务图过度设计。
   - 控制方式：先列表化展示，不做复杂图编辑。
3. context 变成垃圾桶。
   - 控制方式：fact 必须有类型、来源、证据引用。
4. LLM 输出不可信。
   - 控制方式：LLM 只读、必须引用 evidence、不允许直接执行。
5. SQLite 表增长。
   - 控制方式：V4 仍接受 SQLite；等数据规模证明需要再迁 PostgreSQL。

## 13. V4 一句话目标

V4 要把 DiagOps 从“能做 RCA 的只读平台”升级为“能展示任务规划、Agent 路由、工具调用、共享上下文和历史记忆的中文多 Agent 运维诊断平台”。
