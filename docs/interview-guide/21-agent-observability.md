# 21. Agent 可观测性与调试：让一次推理可解释、可定位

## 1. 为什么普通 HTTP 日志不够

一次 Agent 诊断会跨越 API、Runtime phase、多个模型请求、并行工具、Provider、数据库事务和前端 SSE。只在入口打印一行 `request failed`，无法回答：

- 哪个 Agent 先消耗了预算？
- 模型到底发起了几次真实请求，还是 SDK 重试了几次？
- 哪个工具返回了新 Evidence，哪个只是重复查询？
- Critic 为什么得到 `unknown` 或 `needs_evidence`？
- 进程崩溃后是重新调用，还是复用了 durable success？
- 延迟来自排队、模型、Provider、重试还是持久化？

Agent 可观测性要把“推理过程”变成可审计的事件和指标，但不等于保存私有 Chain-of-Thought。

## 2. 三层信号模型

```text
业务事实层：Evidence / Finding / Candidate / Assessment / Decision
运行审计层：RuntimeEvent / Checkpoint / ToolCall / AgentExecution
诊断遥测层：OpenTelemetry trace/span + metrics/logs
```

三层用途不同：

| 层 | 真相来源 | 适合回答 | 失败时怎么办 |
| --- | --- | --- | --- |
| 业务事实 | SQLite Repository | “最终发布了哪些候选、引用了哪些证据？” | 不能用 telemetry 猜回业务状态 |
| 运行审计 | Runtime Store | “phase/attempt/tool/model 的生命周期是什么？” | 从 durable sequence 恢复/重放 |
| 遥测 | OTel exporter | “p95 延迟、并发和错误分布如何？” | exporter 失败不能阻塞业务 |

SSE `EventHub` 只是提交后事件的实时通知和唤醒层，数据库的 `sequence` 才是 catch-up 真相源。客户端断线后用 `after=sequence` 重连，不能把丢失的 SSE 当作丢失业务事件。

## 3. 一次 Run 的关联键

推荐把下列字段贯穿所有层：

```text
investigation_id → run_id → attempt_id → phase
                                  ├→ agent execution id
                                  │      ├→ model logical call/request index
                                  │      └→ tool logical call/attempt
                                  └→ Evidence / RuntimeEvent sequence
```

V11 的 `AgentExecution` 还记录 `runtime_attempt_id`、`resume_from_execution_id`、`analysis_round`、`step_kind`、`failure_category`、`input_tokens`、`output_tokens`、`deadline_at` 等。模型重试的 attempt 不能只靠 agent 名区分，要用逻辑调用、请求序号和 reservation 身份关联。

## 4. 当前 Runtime 事件

`backend/domain/runtime.py` 定义了细粒度事件：

```text
run/attempt/phase: created, started, completed, failed, interrupted
agent: started, completed, failed
model: started, completed, failed
tool: proposed, started, completed, failed, rejected, skipped
evidence: persisted, rejected
checkpoint/recovery: created, started, completed, rejected
```

事件 payload 经过结构化节点、长度和敏感 key 校验；模型 prompt、原始响应正文和私有推理不作为 V11 公共事件保存。模型 usage、reservation 状态、request index 和安全 failure category 足以支持预算审计。

## 5. 当前 OTel facade 的边界

`backend/runtime/telemetry.py` 的 `RuntimeTelemetry` 是**allowlist、脱敏且有界的 tracing facade**：

- `runtime.attempt` span 关联 `investigation_id`、`run_id`、`attempt_id`；恢复 Attempt 用 Link 连接前一次 trace；
- `span/start_span` 供 phase、agent、model、tool/provider 生命周期挂接；
- `ALLOWED_ATTRIBUTES` 只允许预定义字段：investigation/run/attempt ID、phase、agent/tool/provider/model、status、duration、token、cost、evidence_count、failure_category 等；
- 字符串会脱敏并限长，未知属性被丢弃；
- exporter/collector 失败只记录固定类别，不让业务 Run 失败；
- `force_flush` 和 `shutdown` 有界且可重复调用。

这是有意的安全取舍：Run/Attempt ID 适合 trace 关联，但不应作为 metric label；聚合指标仍要使用 phase、provider、failure category 等低基数维度。allowlist 和脱敏避免把 incident description、日志正文或 API key 变成 span 属性。

## 6. Agent 指标应该怎样定义

### 6.1 可靠性与质量

```text
run_success_rate          completed / all started runs
inconclusive_rate         inconclusive / completed-or-safe-ended runs
invalid_output_rate       invalid structured responses / model requests
tool_rejection_rate       rejected or skipped calls / proposed calls
evidence_yield             new usable Evidence / successful tool calls
citation_validity          valid cited IDs / all cited IDs
```

### 6.2 成本与性能

```text
model_input_tokens / output_tokens / total_cost
queue_latency, model_latency, tool_latency, persist_latency
retry_count, reservation_overrun_count, cache_hit_rate
run_duration_ms p50/p95/p99
parallel_slot_utilization
```

### 6.3 安全指标

```text
read_only_violation_count
cross_scope_rejection_count
prompt/tool injection detection count
secret-redaction hit count
late-result/fence rejection count
SSE overflow/reconnect count
```

指标名称和维度要稳定；不要把每条 Evidence ID 或完整 query 放进 metric label。详细值放在受控审计记录，聚合仪表盘只展示低基数维度。

## 7. 故障排查 Playbook

### 症状 A：Run 显示完成但没有根因

1. 查 `run_id` 的 phase sequence 是否包含 `CRITIC_REVIEW`、`LEAD_ADJUDICATION`、`RESULT_VALIDATION`；
2. 查 Critic assessments 是否有 `accept`；
3. 查 Lead decision 是否为 `inconclusive` 或 accepted refs 为空；
4. 查 Evidence status、scope 和引用 owner；
5. 不要从 OTel span 的“成功”推断业务诊断成功。

### 症状 B：工具调用数量异常高

1. 按 `logical_call_id` 去重，而不是按网络 attempt 计数；
2. 检查 `query_fingerprint`/duplicate stop reason；
3. 检查 SDK/provider 内部 retry 是否意外开启；
4. 检查 resume 是否复用了 durable success；
5. 比较 reservation、实际 usage 和 Run 全局预算。

### 症状 C：SSE 页面漏事件

1. 记录客户端最后 `sequence`；
2. 用 Runtime API 按 sequence 查询 durable events；
3. 若订阅队列溢出，重新建立 after cursor，而不是让前端自行拼状态；
4. 检查 EventHub 是否只做通知，Writer/Store 是否已经提交。

### 症状 D：模型输出在网关成功但 phase failed

检查结构化 schema、控制字符、Evidence 引用、execution contract digest 和 deadline。网关 HTTP 200 只说明传输成功，不代表 V11 contract 合法。

## 8. 前沿实现与选择

- OpenAI Agents SDK 自带 tracing、span 和 sensitive-data 控制，适合快速查看 Agent/Tool 拓扑；但业务 Evidence、Run owner 和预算仍应在自己的 Store 中保存。见 [Tracing](https://openai.github.io/openai-agents-python/tracing/)。
- LangGraph/LangSmith 将 state transition、checkpoint 和调试视图结合，适合长生命周期图；引入前要避免与现有 Runtime 形成两套状态真相。见 [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)。
- OpenTelemetry GenAI semantic conventions 正在演进。落地时应固定版本、过滤 prompt/response 敏感内容并限制属性基数，不要直接照搬实验字段。

建议采用“业务审计为真、OTel 为观测”的双写策略：业务事务提交成功后再发布轻量 span/event，遥测丢失不改变诊断结果；反过来不能用一条 span 重建缺失的 Evidence。

## 9. 教育版 instrumentation 伪码

```python
async def run_tool_observed(run, task, tool):
    attrs = telemetry.safe_attributes({
        "run_id": run.id,
        "phase": run.current_phase,
        "agent_name": task.agent_name,
        "tool_name": tool.name,
    })
    with telemetry.span("agent.tool", attrs):
        started = monotonic()
        result = await safe_tool_call(run, task, tool)
        # 业务提交成功后再记录数量/状态，避免“span 成功但事务回滚”。
        telemetry.record("tool.completed", {
            **attrs,
            "status": result.status,
            "duration_ms": elapsed_ms(started),
            "evidence_count": len(result.evidence),
        })
        return result
```

真实实现还需要跨线程/异步上下文传播、取消清理、采样和 exporter 隔离；请以 [RuntimeTelemetry](../../backend/runtime/telemetry.py)、[EventHub](../../backend/runtime/event_hub.py) 和 [Runtime events](../../backend/domain/runtime.py) 为准。

## 10. 观测数据保留与隐私

建议按数据敏感度分层保留：

| 数据 | 典型保留策略 |
| --- | --- |
| Run/phase/tool/model 状态 | 较长，支持审计和 replay |
| Evidence 摘要与 provenance | 按事故/合规保留，原始 payload 单独控制 |
| Prompt/响应正文 | 默认不落公共 telemetry；若调试临时采样，必须脱敏、授权和自动过期 |
| OTel metrics | 聚合后长期保留，禁止把 Run/Attempt 等高基数关联 ID 当 metric label |
| SSE | 不作为持久化，断线从 Store 补齐 |

## 11. 现状标签

**Implemented**：细粒度 RuntimeEvent、AgentExecution/ToolCall 生命周期、sequence catch-up SSE、allowlisted/脱敏 OTel tracing、token/evidence/failure 字段和有界 exporter lifecycle。

**Partial**：业务结果和运行审计较完整；模型调用的 prompt/响应正文 intentionally 不保存，调试时无法直接从 Store 重建完整自然语言轨迹，只能依靠结构化摘要和 provider 日志。`cost` 是允许的事件/span 字段，但当前 V11 没有统一的模型定价计算或持续写入成本值。

**Not implemented**：统一 Prometheus/Grafana dashboard、跨 Run 分布式 trace collector 的运维模板、自动异常检测、全量 GenAI semantic convention 和在线成本告警策略。

## 12. 面试回答模板

> 我把可观测性分成业务事实、运行审计和 OTel 三层。Evidence/Decision 是业务真相，RuntimeEvent/Checkpoint 记录可恢复生命周期，OTel 提供 allowlisted trace 关联和低基数指标聚合；Run/Attempt ID 只用于 trace，不作 metric label。SSE EventHub 只是通知，断线从 durable sequence catch-up。每个 Run 用 investigation→run→attempt→phase→execution→logical tool/model call 关联，重试按逻辑调用去重。指标覆盖 valid output、evidence yield、citation validity、tokens、p95、retry、scope rejection 和 late-result fence；成本需接入明确的定价层后再作为实测指标。默认不保存 prompt、原始响应或 Chain-of-Thought，collector 失败也不能改变业务状态。

## 13. 学习核对清单

- [ ] 能说明 OTel span 为什么不能代替 Evidence/Runtime Store。
- [ ] 能按 logical call 区分 retry 与真正的新工具调用。
- [ ] 能从 sequence 解释 SSE 断线重连和队列溢出恢复。
- [ ] 能列出 Agent 质量、成本、延迟和安全指标。
- [ ] 能区分 trace 的 allowlisted 关联 ID 与 metric 的低基数维度，并避免泄漏正文。
- [ ] 能用事件顺序排查“完成但无根因”和“工具调用过多”。
