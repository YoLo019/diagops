# DiagOps V3 真实数据诊断平台设计

## 1. 背景

DiagOps V2 已经完成一个只读的运维诊断闭环：

```text
事件进入
  -> 创建 investigation
  -> 采集证据
  -> 生成 RCA 假设
  -> 生成建议动作和验证建议
  -> 生成报告
  -> 通过 API 更新动作审批和验证状态
```

V2 的价值是证明项目自己的领域模型、轻量多 Agent 协调方式、报告结构和 API 闭环可以跑通。V2 仍然有一个关键限制：大部分证据来自 mock provider，数据只保存在内存中，没有 UI，也没有真实运维数据的长期沉淀。

V3 的目标是把 DiagOps 从“可运行的 RCA 后端闭环”升级为“可接入真实数据、可持久化、可查看诊断过程的只读运维诊断平台”。

V3 继续保持安全边界：

1. 不自动修改生产系统。
2. 不自动执行 rollback、restart、scale、config change。
3. 不通过 SSH 执行命令。
4. 不把 LLM 作为唯一判断来源。
5. 所有结论、动作建议、验证建议必须能追溯到证据。

## 2. 长期目标

用户的长期目标是构建一个面向运维故障处理的平台型 Agent：

```text
告警、报错、日志或人工描述自动进入平台
  -> Agent 判断要查哪些信息
  -> 自动收集日志、指标、发布、依赖、服务元数据等证据
  -> 多 Agent 协作分析根因
  -> 判断是发版、QPS 飙升、依赖异常、机器问题、数据库慢等
  -> 输出带证据链的结论、建议动作和验证方式
  -> 工程师在平台中审批、补充信息、记录处理结果
  -> 后续逐步支持知识库、半自动执行和复盘
```

V3 是这个长期目标中的“真实数据只读平台”阶段。

## 3. 参考项目借鉴

V3 继续参考两个项目，但不照搬它们的复杂度。

### 3.1 OpenDerisk 借鉴点

OpenDerisk 更适合作为 RCA 多 Agent 框架参考。

V3 借鉴：

1. 证据优先，而不是一次性 LLM 回答优先。
2. Coordinator + Specialist 的分工形态。
3. 日志、指标、发布、依赖、服务元数据等上下文统一进入诊断上下文。
4. 报告区分事实、推断、建议、不确定性。
5. 后续预留 LLM analyst，但 LLM 只能基于已有证据补充分析。

V3 不做：

1. 不引入复杂多 Agent SDK 作为核心运行时。
2. 不做代码级深度分析。
3. 不做复杂可视化证据图谱。
4. 不做自动策略学习或强化学习。

### 3.2 ITOps Agent Platform 借鉴点

ITOps Agent Platform 更适合作为运维平台产品参考。

V3 借鉴：

1. 告警进入后形成可追踪处理记录。
2. 平台保留历史 investigation。
3. 诊断任务有状态、时间线、失败原因。
4. 支持手动问题描述。
5. 后续支持服务器、脚本、审批和自动修复。

V3 不做：

1. 不做 SSH 自动采集和自动修复。
2. 不做服务器资产管理大平台。
3. 不做 Docker/Kubernetes/网络设备的深度管理。
4. 不做完整权限系统。

## 4. V3 产品目标

V3 的一句话目标：

> 让 DiagOps 从 mock RCA 后端升级为可持久化、可接入真实告警/日志/指标、可通过 UI 查看诊断过程的只读运维诊断平台。

核心目标：

1. Investigation 不再只存在内存中，服务重启后仍可查询。
2. 支持 SQLite 本地持久化，并为 PostgreSQL 预留连接串和模型迁移空间。
3. 支持真实或半真实数据源：
   - Webhook 告警事件
   - 本地日志文件 provider
   - Prometheus HTTP provider
   - JSON 文件部署记录 provider
   - YAML/JSON 服务目录 provider
4. 提供更完整的 investigation 详情 API。
5. 提供一个 Web UI 第一版，用于查看诊断列表和详情。
6. 引入可选的只读 LLM Analyst，用于补充解释和缺失证据建议。
7. 引入配置文件，统一管理 provider、数据库、路径和 LLM 开关。
8. 建立本地依赖和数据默认落在 D 盘的约定。

## 5. 非目标

V3 明确不做：

1. 自动 SSH 执行。
2. 自动重启服务。
3. 自动回滚。
4. 自动扩缩容。
5. 自动修改配置。
6. Kubernetes 控制面集成。
7. 完整 CMDB。
8. 完整用户登录和权限系统。
9. 复杂工作流编排器。
10. 向量数据库或复杂 RAG 系统。
11. PostgreSQL 强制依赖。

## 6. V3 范围

V3 分为六个模块：

1. 持久化存储。
2. 真实数据 provider 第一版。
3. 诊断详情 API 完整化。
4. Web UI 第一版。
5. LLM 只读 Analyst。
6. 配置化和本地环境约定。

## 7. 本地环境和依赖落盘约定

用户希望外部组件尽量安装在 D 盘，少占用 C 盘。

V3 约定：

1. 项目代码：
   - `D:\agent\sre-agent`
2. Python：
   - 继续使用 D 盘 Python。
3. 项目虚拟环境：
   - `D:\agent\sre-agent\.venv`
4. uv cache：
   - `D:\agent\.uv-cache`
5. SQLite 数据库：
   - `D:\agent\sre-agent\data\diagops.db`
6. 日志样例数据：
   - `D:\agent\sre-agent\data\sample-logs`
7. 部署记录样例：
   - `D:\agent\sre-agent\data\deployments`
8. 服务目录样例：
   - `D:\agent\sre-agent\config\services.yaml`
9. 如果后续引入 PostgreSQL：
   - 程序目录建议：`D:\dev\PostgreSQL`
   - 数据目录建议：`D:\data\postgres`
   - 备份目录建议：`D:\data\postgres-backups`

V3 不要求安装 PostgreSQL。V3 默认使用 SQLite。

## 8. 持久化存储

### 8.1 目标

把当前 in-memory repository 升级为可持久化 repository。

默认数据库：

```text
sqlite:///data/diagops.db
```

后续兼容 PostgreSQL：

```text
postgresql+psycopg://user:password@host:5432/diagops
```

### 8.2 技术选择

使用当前项目已有依赖：

1. SQLAlchemy 2.x。
2. Python sqlite3。
3. Pydantic domain models 继续作为 API/domain 边界。

V3 可以先不引入 Alembic。如果需要数据演进，先通过 `schema_version` 表和显式初始化脚本管理。

### 8.3 数据表

V3 最少需要这些表：

```text
investigations
events
evidence_items
hypotheses
recommended_actions
verification_suggestions
reports
provider_results
specialist_results
schema_version
```

### 8.4 存储策略

V3 采用“结构化字段 + JSON payload”混合方式。

必须结构化的字段：

1. id。
2. investigation_id。
3. status。
4. service。
5. environment。
6. severity。
7. provider。
8. kind。
9. cause_type。
10. confidence。
11. timestamps。

允许 JSON 保存的字段：

1. event.signals。
2. evidence.payload。
3. hypothesis.next_actions。
4. report markdown metadata。
5. provider raw response 摘要。

### 8.5 Repository 接口

保持现有 repository 语义：

```text
save(record)
get(investigation_id)
list()
update_status(investigation_id, status, failure_reason)
update_action_status(investigation_id, action_id, status, note)
update_verification_status(investigation_id, verification_id, status, result_note)
```

新增：

```text
list(filters)
get_timeline(investigation_id)
get_provider_results(investigation_id)
get_specialist_results(investigation_id)
```

### 8.6 兼容要求

1. API 层不直接依赖 SQLAlchemy ORM 对象。
2. API 继续返回 Pydantic/domain 模型。
3. 内存 repository 可以保留给测试。
4. SQLite repository 必须有独立测试。
5. 服务重启后可以读取历史 investigation。

## 9. 真实数据 Provider 第一版

V3 不再只依赖 mock provider。

### 9.1 Provider 配置

新增配置文件：

```text
config/diagops.yaml
```

示例：

```yaml
storage:
  url: sqlite:///data/diagops.db

providers:
  mock:
    enabled: true
  log_file:
    enabled: true
    paths:
      - data/sample-logs/checkout-service.log
  prometheus:
    enabled: false
    base_url: http://127.0.0.1:9090
  deployment_file:
    enabled: true
    path: data/deployments/deployments.json
  service_catalog:
    enabled: true
    path: config/services.yaml

llm:
  enabled: false
```

### 9.2 Log File Provider

读取本地日志文件。

输入：

1. service。
2. time window。
3. keyword patterns。

输出 evidence：

1. 错误样例。
2. 错误计数。
3. 高频异常类型。
4. 时间范围内的异常趋势摘要。

V3 只支持本地文件，不接 Loki/ELK。

### 9.3 Prometheus Provider

通过 HTTP API 查询 Prometheus。

V3 最小查询：

1. QPS。
2. 5xx rate。
3. P95 latency。
4. CPU。
5. memory。

Prometheus 未启用或连接失败时：

1. provider result 标记为 failed。
2. 生成 provider error evidence。
3. 不阻塞整个 investigation。

### 9.4 Deployment File Provider

从 JSON 文件读取发布记录。

示例文件：

```text
data/deployments/deployments.json
```

记录字段：

```json
{
  "service": "checkout-service",
  "environment": "prod",
  "version": "v1.8.2",
  "deployed_at": "2026-07-03T14:58:00+08:00",
  "operator": "moon",
  "commit": "abc123",
  "summary": "checkout promotion change"
}
```

### 9.5 Service Catalog Provider

从 YAML/JSON 读取服务信息。

字段：

1. service。
2. owner。
3. team。
4. runtime。
5. repository。
6. dependencies。
7. dashboards。
8. runbooks。

### 9.6 Mock Provider 保留

Mock provider 继续保留，用于：

1. 本地测试。
2. golden cases。
3. 没有真实数据源时的演示。

配置中可以同时启用 mock 和真实 provider。

## 10. 诊断详情 API

V3 要让 UI 和外部系统可以完整查看诊断过程。

### 10.1 保留现有 API

```text
POST /events
POST /events/simulated/{case_id}
GET /investigations
GET /investigations/{id}
POST /investigations/manual
PATCH /investigations/{id}/actions/{action_id}
PATCH /investigations/{id}/verifications/{verification_id}
```

### 10.2 新增 API

```text
GET /investigations/{id}/timeline
GET /investigations/{id}/evidence
GET /investigations/{id}/provider-results
GET /investigations/{id}/specialist-results
GET /investigations/{id}/report
GET /config/providers
```

### 10.3 Investigation Detail

`GET /investigations/{id}` 应包含：

1. event。
2. status。
3. failure_reason。
4. evidence。
5. hypotheses。
6. actions。
7. verification_suggestions。
8. provider_results。
9. specialist_results。
10. report summary。
11. created_at / updated_at / completed_at。

### 10.4 Evidence 筛选

`GET /investigations/{id}/evidence` 支持查询参数：

```text
provider
kind
status
limit
```

## 11. Web UI 第一版

V3 新增一个轻量 Web UI。

### 11.1 技术建议

建议使用：

1. Vite。
2. React。
3. TypeScript。
4. TanStack Query。
5. 简单 CSS 或轻量组件库。

前端依赖也应安装在 D 盘项目目录下：

```text
D:\agent\sre-agent\frontend
D:\agent\sre-agent\frontend\node_modules
```

如需 Node.js，优先使用 D 盘安装路径。

### 11.2 页面

V3 UI 最少包含：

1. Investigation 列表页。
2. Investigation 详情页。
3. Evidence 列表。
4. Hypothesis 排序。
5. Recommended actions。
6. Verification suggestions。
7. Markdown report 展示。

### 11.3 交互

V3 UI 允许：

1. 创建 manual investigation。
2. 查看 simulated investigation。
3. 更新 action 状态。
4. 更新 verification 状态。
5. 刷新 investigation 状态。

V3 UI 不允许：

1. 执行命令。
2. 回滚。
3. 重启。
4. 扩容。
5. 修改配置。

## 12. LLM 只读 Analyst

### 12.1 目标

在规则 RCA 之外增加一个可选的 LLM 分析层。

LLM Analyst 的职责：

1. 基于已有 evidence 解释可能原因。
2. 指出缺失证据。
3. 给出下一步排查建议。
4. 对现有 top hypothesis 提出风险提醒。

LLM Analyst 不做：

1. 不创建新的事实。
2. 不覆盖规则 analyzer 的结论。
3. 不执行工具。
4. 不调用 SSH。
5. 不直接生成高风险动作。

### 12.2 输出模型

新增：

```text
LLMAnalysis
```

字段：

```text
id
investigation_id
summary
missing_evidence
risk_notes
suggested_questions
referenced_evidence_ids
created_at
```

所有 `referenced_evidence_ids` 必须存在。

### 12.3 配置

默认关闭：

```yaml
llm:
  enabled: false
```

只有开启后才调用 LLM。

## 13. 配置系统

V3 新增配置加载模块。

配置文件：

```text
config/diagops.yaml
```

环境变量覆盖规则：

```text
DIAGOPS_CONFIG
DIAGOPS_DATABASE_URL
DIAGOPS_PROVIDER_MOCK_ENABLED
DIAGOPS_LLM_ENABLED
```

配置加载要求：

1. 缺少配置文件时使用安全默认值。
2. 路径默认相对于项目根目录。
3. 数据、缓存、样例文件默认在 D 盘项目目录中。
4. 配置错误要在启动时给出清晰错误。

## 14. 数据演进和版本兼容

V3 引入数据库后必须有版本规则。

### 14.1 Schema Version

新增表：

```text
schema_version
```

字段：

```text
version
applied_at
description
```

### 14.2 兼容规则

1. 新字段优先 nullable 或有默认值。
2. 不删除 V2 API 字段。
3. API 增量添加字段必须向后兼容。
4. 老的 in-memory tests 仍然保留。
5. SQLite 文件可删除重建，但生产模式不应自动破坏历史数据。

## 15. 可观测性

V3 延续 V2 logging，并增强：

1. 数据库初始化日志。
2. provider 配置加载日志。
3. provider 查询耗时。
4. UI/API 请求关键错误日志。
5. LLM Analyst 开关和调用耗时日志。

日志不能包含：

1. 密码。
2. token。
3. 完整敏感请求头。
4. 大段原始日志。

## 16. 测试要求

V3 至少需要这些测试：

### 16.1 存储测试

1. SQLite 初始化。
2. save/get/list。
3. 重启 repository 后仍可读取。
4. action/verification 状态更新持久化。
5. failed investigation 持久化。

### 16.2 Provider 测试

1. log file provider 解析错误日志。
2. deployment file provider 匹配 time window。
3. service catalog provider 返回 owner/dependencies。
4. Prometheus provider 连接失败降级。
5. provider result 仍能转 error evidence。

### 16.3 API 测试

1. detail 包含 provider/specialist results。
2. evidence filter 生效。
3. report endpoint 返回 markdown。
4. manual investigation 持久化。
5. 服务重启后历史 investigation 可查。

### 16.4 UI 测试

V3 可以先做最小 smoke：

1. UI 能加载 investigation 列表。
2. UI 能打开详情页。
3. UI 能更新 action 状态。
4. UI 能更新 verification 状态。

### 16.5 Golden 测试

Golden cases 继续必须通过，并改为使用持久化 repository 路径至少跑一次。

## 17. 验收标准

V3 完成时必须满足：

1. `uv run ruff check .` 通过。
2. `uv run pytest -v` 通过。
3. 默认 SQLite 数据库创建在 `data/diagops.db`。
4. 重启 API 后，之前创建的 investigation 仍可查询。
5. webhook event 可以创建持久化 investigation。
6. manual investigation 可以创建持久化 investigation。
7. 至少一个本地日志文件 provider 能生成 evidence。
8. 至少一个 deployment file provider 能生成 evidence。
9. service catalog provider 能生成 owner/dependency evidence。
10. provider 失败不会导致整个 investigation 失败。
11. UI 可以查看列表和详情。
12. UI 可以更新 action/verification 状态。
13. README 包含 V3 本地启动、SQLite 文件位置、provider 配置说明。
14. 不会执行任何生产写操作。

## 18. 推荐实施顺序

V3 建议按以下顺序实现：

1. 配置系统和 D 盘路径约定。
2. SQLite repository。
3. 数据库初始化和 schema version。
4. 持久化接入 container。
5. 真实 provider：service catalog file。
6. 真实 provider：deployment file。
7. 真实 provider：log file。
8. Prometheus provider 降级版。
9. 诊断详情 API 完整化。
10. Web UI 第一版。
11. LLM 只读 Analyst。
12. README 和最终 smoke test。

## 19. V3 成功后的能力边界

V3 成功后，DiagOps 应达到：

```text
真实或半真实告警进入
  -> 持久化 investigation
  -> 从本地日志、部署记录、服务目录、可选 Prometheus 收集证据
  -> 多 specialist 汇总诊断上下文
  -> 规则 RCA 生成根因假设
  -> 可选 LLM Analyst 给出补充分析
  -> 生成建议动作和验证建议
  -> 生成报告
  -> UI 查看证据链、结论、建议和验证状态
  -> 数据重启不丢
```

V3 之后，V4 才适合考虑：

1. PostgreSQL 正式支持。
2. 用户和权限。
3. 审批流。
4. SSH 只读采集。
5. Runbook/RAG。
6. 受控半自动修复。
