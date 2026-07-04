# DiagOps V2 平台闭环雏形设计

## 1. 背景

DiagOps 第一版已经完成事件接入、模拟案例、mock evidence provider、规则 RCA 分析、Markdown 报告和 golden case 测试。第一版证明了一个最小闭环：

```text
Incident event
  -> evidence collection
  -> root cause hypothesis
  -> evidence-backed report
  -> engineer review
```

第二版的目标不是马上进入自动修复，而是把系统从“一次性 RCA API”推进到“可持续管理一次故障处理流程的平台雏形”。

用户的长期目标是构建一个面向运维故障处理的平台型 Agent：

```text
问题自动进入平台
  -> Agent 自动判断要查什么
  -> 自动收集日志、指标、发布、依赖、机器状态等证据
  -> 多 Agent 协作分析根因
  -> 判断是发版、QPS 飙升、依赖异常、机器问题、数据库慢等
  -> 输出带证据链的结论和建议
  -> 后续逐步支持审批、验证，甚至半自动修复
```

第二版选择“平台闭环雏形版”：

1. 继续不自动修改生产系统。
2. 引入故障处理状态机。
3. 引入建议动作、审批状态和验证建议。
4. 引入轻量多 Agent 分工。
5. 为后续真实数据源、审批执行和验证闭环预留结构。

## 2. 参考项目借鉴

第二版参考两个项目，但不照搬它们的代码或完整复杂度。

### 2.1 OpenDerisk 借鉴点

OpenDerisk 适合作为 DiagOps 的 RCA 框架参考。

需要借鉴：

1. Coordinator + Specialist 的多角色协作形态。
2. 以证据为中心的诊断，而不是只依赖单次 LLM 回答。
3. 将日志、指标、链路、代码、知识库、服务元数据作为可组合上下文。
4. 诊断结论必须可解释、可追溯、可评估。
5. 区分事实、推断、建议和不确定性。

需要简化：

1. 不引入完整复杂的多 Agent 框架。
2. 不在 V2 做代码级深度分析。
3. 不做复杂可视化协议。
4. 不让 LLM 成为唯一决策源。

### 2.2 ITOps Agent Platform 借鉴点

ITOps Agent Platform 适合作为平台闭环参考。

需要借鉴：

1. 告警进入后自动创建处理记录。
2. 诊断任务有 `pending / running / completed / failed` 等状态。
3. 失败也要落库，不能丢失故障上下文。
4. 自动收集上下文，包括告警、设备、发布、相关告警、知识库等。
5. 生成修复建议时要区分风险等级。
6. 写操作需要审批，不能默认自动执行。
7. 修复后需要验证链，而不是只相信“命令执行成功”。

需要简化：

1. V2 不做 SSH 自动执行。
2. V2 不做真实移动端审批。
3. V2 不接入大量基础设施模块，如网络设备、虚拟化、数据中心。
4. V2 不追求大而全平台，只围绕应用服务故障 RCA。

## 3. 产品目标

V2 要让 DiagOps 从“能生成 RCA 报告”升级为“能管理一次故障调查流程”。

核心目标：

1. Webhook 或模拟事件进入后，立即创建 investigation。
2. investigation 状态可查询，失败也可查询。
3. 后台诊断流程自动收集证据、生成假设、生成报告。
4. 报告中加入建议动作、风险等级、审批状态和验证建议。
5. 引入轻量多 Agent 分工，但保持项目自己的数据模型和 RCA 规则为核心。
6. 为 V3 的真实 provider、真实通知、人工审批和半自动修复打基础。

V2 成功后的用户体验：

```text
监控系统 POST 一条告警
  -> 平台返回 investigation id
  -> 用户打开 investigation 详情
  -> 看到诊断状态从 running 到 completed
  -> 看到最可能根因、证据链、建议动作
  -> 可以将建议动作标记为 approved / rejected / skipped
  -> 可以查看建议的验证项和最终处理总结
```

## 4. 非目标

V2 明确不做：

1. 自动执行回滚、重启、扩容、配置修改等写操作。
2. 真实 SSH 命令执行。
3. 复杂工作流编排器。
4. 完整前端大屏或复杂可视化。
5. Kubernetes、网络设备、虚拟化、数据库内核等深度运维场景。
6. 完整权限系统。
7. 向量数据库或复杂 RAG 系统。
8. 将 CrewAI、LangGraph 等框架作为核心运行时。

## 5. V2 范围

V2 聚焦应用服务故障的“诊断 + 建议 + 审批状态 + 验证建议”。

### 5.1 进入方式

保留并增强：

1. `POST /events`
2. `POST /events/simulated/{case_id}`
3. `GET /investigations`
4. `GET /investigations/{id}`

新增或预留：

1. 手动提交问题描述。
2. 查询建议动作。
3. 更新建议动作审批状态。
4. 记录验证结果。

### 5.2 支持场景

继续支持第一版五类场景：

1. 发版导致错误率升高。
2. QPS 飙升导致延迟和超时。
3. 下游依赖慢或不可用。
4. 数据库慢查询或存储变慢。
5. 单实例异常。

V2 重点增强：

1. 相关告警聚合。
2. 近期开变更上下文。
3. 服务负责人和模块归属。
4. 处理建议和风险等级。
5. 验证建议。

## 6. 核心概念

### 6.1 Investigation

一次故障调查。

状态：

```text
pending
running
completed
failed
cancelled
```

含义：

1. `pending`：事件已进入，等待诊断。
2. `running`：正在收集证据或生成报告。
3. `completed`：诊断完成，有报告和建议。
4. `failed`：诊断失败，但失败原因已保存。
5. `cancelled`：用户或系统取消。

### 6.2 EvidenceItem

证据项。所有结论必须引用 evidence id。

新增要求：

1. Evidence 必须记录来源 provider。
2. Evidence 必须记录是否成功采集。
3. Provider 失败也应生成一条 missing evidence 或 provider error 记录。

### 6.3 Hypothesis

根因假设。

V2 要求：

1. 所有 supporting evidence id 必须存在。
2. 所有 contradicting evidence id 必须存在。
3. 不只校验 top hypothesis，所有 hypothesis 都要校验。
4. 低置信度必须给出缺失证据和下一步检查建议。

### 6.4 RecommendedAction

建议动作。V2 只生成建议，不自动执行。

状态：

```text
proposed
approved
rejected
skipped
done
```

风险等级：

```text
read_only
low
medium
high
```

动作类型：

```text
check
notify_owner
rollback_suggestion
scale_suggestion
restart_suggestion
config_check
dependency_check
manual_follow_up
```

规则：

1. `read_only` 和 `low` 可以作为普通建议展示。
2. `medium` 和 `high` 必须显式标记“需要人工审批”。
3. V2 不执行任何动作。
4. 报告必须说明为什么建议这个动作，以及它对应哪些证据。

### 6.5 VerificationSuggestion

验证建议。用于回答“怎么确认问题恢复了”。

例子：

1. 检查 5xx rate 是否回落到阈值以下。
2. 检查 P95 latency 是否恢复。
3. 检查 QPS 是否回到正常区间。
4. 检查新异常日志是否停止出现。
5. 检查下游依赖 latency 是否恢复。
6. 检查回滚或配置变更后错误率是否下降。

## 7. V2 架构

```text
Event Sources
  - Simulated cases
  - Webhook alerts
  - Manual problem description

        |
        v

Event Intake
  - Validate event
  - Normalize service/time/severity/signals
  - Create investigation as pending
  - Trigger diagnosis job

        |
        v

Investigation Runner
  - Mark investigation running
  - Build diagnosis context
  - Call Coordinator
  - Persist evidence/hypotheses/report/actions
  - Mark completed or failed

        |
        v

Diagnosis Coordinator
  - Select specialist agents
  - Collect evidence through providers
  - Ask analyzer to rank hypotheses
  - Ask action planner for recommended actions
  - Ask report writer to generate final report

        |
        v

Specialists
  - Log Analyst
  - Metric Analyst
  - Deploy Analyst
  - Dependency Analyst
  - Service Catalog Analyst
  - Report Writer

        |
        v

Platform API
  - Investigation list/detail
  - Report
  - Recommended actions
  - Approval status updates
  - Verification result notes
```

## 8. 轻量多 Agent 设计

V2 不引入完整多 Agent SDK。先用项目内的轻量 agent abstraction。

### 8.1 Coordinator

职责：

1. 根据事件类型选择需要的 specialist。
2. 统一 time window。
3. 汇总 specialist 输出。
4. 调用规则 analyzer。
5. 调用 action planner。
6. 保证所有输出都能追溯到 evidence。

### 8.2 Specialist

所有 specialist 使用统一接口：

```text
analyze(context) -> SpecialistResult
```

SpecialistResult 包含：

```text
agent_name
status
evidence_items
summary
errors
duration_ms
```

V2 specialist：

1. `LogAnalyst`：分析错误日志、异常模式、错误趋势。
2. `MetricAnalyst`：分析 QPS、错误率、延迟、CPU、内存、实例级指标。
3. `DeployAnalyst`：分析近期开变更、版本、提交、发布人。
4. `DependencyAnalyst`：分析下游依赖延迟、错误率、可用性。
5. `ServiceCatalogAnalyst`：提供 owner、模块、依赖、运行环境。
6. `ReportWriter`：根据结构化结果生成报告，不创造新事实。

### 8.3 Agent 约束

1. Agent 不能发明 evidence。
2. Agent 只能引用 provider 返回的数据。
3. Agent 输出必须结构化。
4. Agent 失败不能中断整个 investigation，除非核心数据完全不可用。
5. 所有 agent 执行结果要记录 duration、status、error。

## 9. Provider 设计

V2 继续保留 mock provider，但扩展 provider 类型和失败语义。

Provider 接口统一包含：

```text
name
kind
collect(context) -> ProviderResult
```

ProviderResult：

```text
provider
status: success | partial | failed | skipped
evidence_items
error_message
duration_ms
```

V2 provider：

1. `LogProvider`
2. `MetricProvider`
3. `DeployProvider`
4. `DependencyProvider`
5. `ServiceCatalogProvider`
6. `RelatedAlertProvider`

新增 `RelatedAlertProvider` 用于模拟同一时间窗口内的相关告警，为后续告警聚合做准备。

## 10. Action Planner

Action Planner 根据 top hypothesis 和 evidence 生成建议动作。

输入：

1. Investigation event。
2. Evidence list。
3. Ranked hypotheses。
4. Service catalog。

输出：

1. Recommended actions。
2. Risk level。
3. Approval requirement。
4. Verification suggestions。

示例：

```json
{
  "action_type": "rollback_suggestion",
  "title": "建议评估回滚 payment-service v1.8.2",
  "description": "错误率在 v1.8.2 发布后 3 分钟开始升高，并出现新的 NullPointerException。",
  "risk_level": "high",
  "requires_approval": true,
  "supporting_evidence_ids": ["ev-deploy-001", "ev-log-002", "ev-metric-003"],
  "status": "proposed"
}
```

V2 不执行建议动作，只允许更新状态：

```text
proposed -> approved
proposed -> rejected
proposed -> skipped
approved -> done
```

## 11. 报告设计

V2 报告继续使用 Markdown，但新增闭环字段。

必需章节：

1. 摘要。
2. 当前处理状态。
3. 事实与推断。
4. 时间线。
5. 最可能根因。
6. 备选假设。
7. 支持该结论的证据。
8. 缺失或失败的证据。
9. 建议动作。
10. 需要审批的动作。
11. 验证建议。
12. 不确定性。

报告必须遵守：

1. 事实和推断分开。
2. 建议动作必须引用 evidence。
3. 缺失证据必须显式展示。
4. 低置信度时不能给强结论。
5. 不出现“已修复”这样的表述，除非有验证记录。

## 12. 数据模型

### 12.1 Investigation

```text
id
source
service
environment
severity
title
description
started_at
time_window_minutes
status
failure_reason
created_at
updated_at
completed_at
```

### 12.2 EvidenceItem

```text
id
investigation_id
provider
kind
status
timestamp
summary
payload_json
confidence
error_message
created_at
```

### 12.3 Hypothesis

```text
id
investigation_id
cause_type
summary
confidence
supporting_evidence_ids
contradicting_evidence_ids
next_actions
created_at
```

### 12.4 IncidentReport

```text
id
investigation_id
summary
markdown
timeline_json
hypotheses_json
action_ids
verification_suggestion_ids
created_at
```

### 12.5 RecommendedAction

```text
id
investigation_id
action_type
title
description
risk_level
requires_approval
status
supporting_evidence_ids
created_at
updated_at
```

### 12.6 VerificationSuggestion

```text
id
investigation_id
title
description
expected_signal
status
result_note
created_at
updated_at
```

## 13. API 设计

### 13.1 创建事件

```http
POST /events
```

行为：

1. 创建 investigation。
2. 状态先为 `pending`。
3. 后台启动诊断。
4. 返回 investigation summary。

### 13.2 创建模拟事件

```http
POST /events/simulated/{case_id}
```

行为与 `POST /events` 一致。

### 13.3 列出 investigations

```http
GET /investigations
```

返回：

1. id。
2. status。
3. service。
4. severity。
5. title。
6. top cause。
7. confidence。
8. action count。
9. created_at / updated_at。

### 13.4 获取 investigation 详情

```http
GET /investigations/{id}
```

返回：

1. event。
2. status。
3. evidence。
4. hypotheses。
5. report。
6. recommended actions。
7. verification suggestions。
8. failure reason。

### 13.5 更新建议动作状态

```http
PATCH /investigations/{id}/actions/{action_id}
```

请求：

```json
{
  "status": "approved",
  "note": "工程师确认先走回滚评估"
}
```

V2 只更新状态，不执行动作。

### 13.6 记录验证结果

```http
PATCH /investigations/{id}/verifications/{verification_id}
```

请求：

```json
{
  "status": "passed",
  "result_note": "5xx rate 已恢复到 0.2%"
}
```

### 13.7 手动问题输入

```http
POST /investigations/manual
```

请求：

```json
{
  "text": "checkout-service 从 14:00 开始 500 增多，帮我看是不是发版导致的",
  "service": "checkout-service",
  "environment": "prod"
}
```

V2 要求 `service` 和 `environment` 显式提供。自然语言字段抽取放到后续版本，避免在第二版引入不可控解析行为。

## 14. 前端/平台视图

V2 必做 API 闭环。最小平台 UI 不纳入 V2 必做范围，可以作为 V2 之后的独立计划；若后续实现 UI，优先做操作型页面。

视图：

1. Investigation 列表。
2. Investigation 详情。
3. Evidence 时间线。
4. Hypothesis 面板。
5. Recommended Actions 面板。
6. Verification Suggestions 面板。

页面风格：

1. 紧凑、适合扫描。
2. 不做营销页。
3. 不夸张装饰。
4. 重点展示状态、证据、动作和不确定性。

## 15. 错误处理

V2 的关键改进是失败也要可见。

规则：

1. Event 创建失败时返回 4xx 或 5xx，不创建 investigation。
2. Investigation 创建成功后，诊断失败必须保存为 `failed`。
3. Provider 失败生成 provider error evidence。
4. 单个 provider 失败不应导致整个 investigation 失败，除非所有核心 provider 都失败。
5. Report 生成失败时 investigation 标记为 `failed`，保存 failure_reason。
6. Action Planner 失败不应覆盖 RCA 结果，可以报告“建议动作生成失败”。
7. API detail 必须能查看 failure reason。

## 16. 安全边界

V2 默认只读。

规则：

1. 不执行命令。
2. 不调用真实回滚、重启、扩容接口。
3. 所有写操作建议都只是 recommended action。
4. 中高风险动作必须 `requires_approval=true`。
5. 报告中必须明确“V2 未执行该动作”。
6. 后续如果接入工具调用，必须先实现安全闸门：
   - read-only 默认允许。
   - destructive 默认禁止。
   - medium/high 需要审批。
   - 所有工具调用审计。

## 17. 测试与评估

V2 继续以 golden cases 作为质量核心。

新增测试：

1. Investigation 状态从 pending/running 到 completed。
2. 诊断异常时状态变为 failed 且保存 failure_reason。
3. Provider partial/failed 时报告包含缺失证据。
4. 所有 hypothesis 的 evidence id 都存在。
5. Recommended action 引用的 evidence id 都存在。
6. 中高风险 action 必须 requires_approval。
7. PATCH action status 只更新状态，不执行动作。
8. Verification suggestion 可以记录结果。
9. Report 包含建议动作、审批状态、验证建议和不确定性。
10. Golden cases 仍能正确识别五类主因。

## 18. V2 实施顺序建议

1. 扩展 domain model：Investigation 状态、RecommendedAction、VerificationSuggestion。
2. 扩展 repository：保存 failed 状态、actions、verifications。
3. 将 orchestrator 改为 runner：先落 pending/running，成功 completed，异常 failed。
4. 增加 provider result 状态语义：success/partial/failed/skipped。
5. 引入轻量 SpecialistResult 和 Coordinator。
6. 增加 RelatedAlertProvider 和 ServiceCatalogProvider mock 数据。
7. 增加 Action Planner。
8. 扩展 Report Generator。
9. 扩展 API：actions 和 verifications 状态更新。
10. 扩展 golden tests。
11. 更新 README 和 examples。

## 19. V2 验收标准

V2 完成时必须满足：

1. Webhook 或模拟事件能创建 investigation，并可查询状态。
2. 诊断成功时，investigation 为 `completed`，包含 evidence、hypotheses、report、recommended actions、verification suggestions。
3. 诊断失败时，investigation 为 `failed`，包含 failure_reason。
4. 报告能展示事实、推断、证据、建议动作、审批要求和验证建议。
5. 所有结论和建议动作都引用合法 evidence id。
6. 中高风险建议动作不会被执行，只能被标记审批状态。
7. 五个 golden cases 仍通过，并新增 action/verification 断言。
8. 全量测试和 lint 通过。

## 20. V2 之后的方向

V2 之后可以进入 V3：

1. 接入真实 Prometheus metric provider。
2. 接入真实 Loki/ELK log provider。
3. 接入 GitHub/GitLab/Jenkins/ArgoCD 发布记录。
4. 引入 OpenAI Agents SDK 作为 specialist execution layer。
5. 加入真实通知渠道，如飞书、钉钉、Slack、企业微信。
6. 加入人工审批流。
7. 加入只读诊断工具调用。
8. 在安全闸门成熟后，实验低风险自动执行。
