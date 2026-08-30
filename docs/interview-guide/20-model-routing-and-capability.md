# 20. 模型路由、能力准入与安全降级

## 1. 先区分“任务路由”和“模型路由”

项目里有两个容易被混为一谈的概念：

| 概念 | 问题 | 当前代码 |
| --- | --- | --- |
| 任务/工具路由 | 这项工作由哪个 Agent、哪些工具处理？ | `backend/diagnosis/router.py` 的 legacy `DiagnosisTaskType → AgentRoute` |
| 模型路由 | 这次模型请求用哪个 provider/model/endpoint？ | V11 当前由配置和 capability admission 固定，不做动态选择 |

`AgentRouter` 把 `LOG_INVESTIGATION` 映射到 LogAgent 等旧任务；V11 的通用 Investigator 不通过它决定模型。面试中说“router.py 已实现动态模型路由”是不准确的。

## 2. 为什么“模型能调用 API”不等于“模型适合生产 Agent”

一个 endpoint 可能能返回普通文本，却不支持：

- 非流式稳定响应；
- tool calls 的参数格式；
- 带工具时的严格 JSON Schema；
- 每个 V11 角色的输出 schema；
- 可计费的 input/output token usage；
- 配置的并发度；
- 客户端 deadline、取消和连接关闭；
- 超长或异常响应的边界。

如果只凭供应商名称切换模型，第一次线上故障才会发现这些能力缺失。V11 把“能力证明”放在模型调用之前。

## 3. 当前 capability certification 做了什么

`backend/services/model_capability.py` 生成 `model-capability-v1` 工件。工件不保存 URL、API key、prompt、证据或响应正文，只保存精确 endpoint/model tuple 的身份、观察结果、SDK/adapter/source/environment 信息和哈希。

当前 capability manifest 包含：

```text
non_streaming_chat_completions
tool_calls
native_json_schema_with_tools
strict_output_tool
token_usage
bounded_response_deadline
configured_parallelism
v11_remote_role_schema_capability
v11_local_workflow_correctness
```

认证探针会：

1. 发出最小非流式请求；
2. 要求模型产生受控 tool call；
3. 分别探测 native JSON Schema 和 `submit_structured_output` envelope；
4. 检查 usage 的输入/输出 token 字段；
5. 以配置的并发度并发探测；
6. `v11_remote_role_schema_capability` 对 Lead、Investigator、Critic、Adjudication 等 V11 输出类型逐一发远端请求并验证；
7. `v11_local_workflow_correctness` 运行本地 fake-compatible workflow canary，验证共享 Runtime/Store 边界；它不证明真实远端 endpoint 已跑通完整 V11 workflow；
8. 把代码 revision、source manifest、Python/SDK/adapter 版本绑定到工件。

`validate_capability_for_prediction` 还会检查工件是否是当前干净 source、当前 SDK/adapter、当前执行环境，并且测试并发度不低于实际配置。认证通过只表示“协议能力满足”，不表示诊断准确率高。

## 4. V11 execution contract 如何冻结模型身份

创建 Run 时，`AppContainer.create_runtime_run` 将以下字段写入不可变 `execution_contract`：

```json
{
  "execution_contract_version": "v11",
  "authority_mode": "agent",
  "model_provider": "openai_compatible",
  "model_name": "example-model",
  "api_mode": "chat_completions",
  "endpoint_id": "sha256(canonical-url)",
  "capability_artifact_hash": "...",
  "tool_manifest_hash": "...",
  "skill_catalog": {"catalog_version": "v11-skills-v1", "catalog_hash": "..."},
  "limits": {"max_turns": 8, "token_budget": 12000, "max_rounds": 2},
  "topology": {"mode": "multi_lead_investigators_critic", "hidden_model_calls": false},
  "retry_policy": {"max_retries": 1, "provider_max_retries": 0, "sdk_max_retries": 0}
}
```

`endpoint_id` 是 canonical URL 的 hash，不暴露 URL；合同摘要不允许凭证、prompt、reasoning 等敏感字段。恢复时 `V11Runtime.clone_for_run`、`bind_phase` 和 `_validate_execution_contract` 都以冻结合同为准，不能因为进程重启或当前配置变化而换模型、换工具或重置预算。官方 OpenAI 路径固定官方 endpoint，但当前 container 只对 `openai_compatible` 路径强制要求 passed capability artifact；不要把这一 admission 条件泛化成所有 V11 provider 都已有工件。

## 5. 当前 provider 适配边界

| Provider | API 形态 | 当前边界 |
| --- | --- | --- |
| 官方 OpenAI | Responses | endpoint 固定为 `https://api.openai.com/v1`，凭证只在进程环境 |
| OpenAI-compatible | Chat Completions | 必须显式配置 base URL、`DIAGOPS_AGENTS_API_KEY`，并通过 capability artifact |
| DeepSeek | 兼容适配器可用于旧路径 | V11 要求经过认证的 compatible endpoint，不接受未经认证的直连假设 |

`openai_model.py` 和 `openai_compatible_model.py` 负责 transport；V11 外层负责 retry、预算、生命周期和安全校验。兼容适配器支持 native JSON Schema 或 strict output tool 两种结构化 transport，不能把“能返回文本”当成 V11 可用。

## 6. 前沿模型路由模式比较

| 模式 | 决策方式 | 适合 | 主要风险 | 是否适合当前 V11 |
| --- | --- | --- | --- | --- |
| 静态绑定 | 每个产品/Run 固定一个模型 | 强复现、评测、审计 | 无法利用价格/延迟差异 | 当前采用 |
| 规则 cascade | 先小模型，失败/低置信度升级大模型 | 成本优化、简单任务 | 质量阈值漂移、双倍延迟 | 可做 shadow |
| 能力矩阵路由 | 按 schema/tool/context/语言能力筛选 | 多供应商协议兼容 | 维护矩阵成本 | capability admission 已覆盖协议层 |
| 质量路由 | 预测任务难度或置信度后选模型 | 动态质量/成本平衡 | router 自身错误、偏差 | 尚未实现 |
| Bandit/在线学习 | 用反馈更新模型选择 | 流量足、目标稳定 | 探索成本、评测污染 | 不适合未完成正式 gate 阶段 |
| Ensemble/MoA | 多模型并行再聚合 | 高风险少量任务 | 成本、相关错误、裁决复杂 | V11 只做角色分工，不做模型投票 |

“小模型先做，失败再大模型”不是免费 fallback：两次请求都要计入预算、审计和隐私边界，且第二个模型不能看到未授权的第一模型私有推理。

## 7. 如果未来按角色路由，合同必须怎样扩展

V11 当前要求同一 Run 的 Lead、Investigator、Critic 使用冻结模型 tuple，便于 paired evaluation。若要让 Critic 使用更强模型，不能只在代码里加 `if actor == "CriticAgent"`；应把每个 actor 的身份显式加入合同：

```json
{
  "actor_models": {
    "LeadAgent": {"provider": "...", "model": "...", "endpoint_id": "..."},
    "InvestigatorAgent": {"provider": "...", "model": "...", "endpoint_id": "..."},
    "CriticAgent": {"provider": "...", "model": "...", "endpoint_id": "..."}
  },
  "routing_policy_hash": "..."
}
```

还要重新定义：总 token/成本上限、每角色能力认证、重试和数据隔离、replay 输入、评测对照组。否则“路由优化”会破坏可复现性，甚至让 Critic 获得未声明能力。

## 8. 教育版安全路由伪码

```python
def choose_model(run, actor, task_features):
    candidates = capability_registry.match(
        required={"structured_output", "tool_calls"},
        parallelism=run.contract["limits"]["max_investigators"],
    )
    candidates = [item for item in candidates if item.endpoint_id in allowlist]
    ranked = sorted(
        candidates,
        key=lambda item: (item.estimated_cost(task_features), item.p95_latency),
    )
    selected = ranked[0]
    # 选择结果必须写进新 Run 合同；恢复时不重新选择。
    return freeze_actor_identity(run, actor, selected)
```

真正上线前还需要把 `quality_floor`、租户策略、区域/数据驻留、fallback 状态、模型故障类别和人工升级路径纳入合同；不应把教育版排序函数直接替换生产 V11 的固定身份。

## 9. 路由效果怎样测

至少要记录并按 actor/model/tenant 分组比较：

```text
task success / valid structured output rate
tool-call precision、citation validity、abstention/inconclusive rate
input/output tokens、cache hit、cost per successful diagnosis
queue/model/tool/retry latency（p50/p95/p99）
rate-limit/timeout/transport/invalid-output 分布
模型切换率、升级率、fallback 后质量 delta
```

离线先用固定 case 做 shadow 路由，比较同一输入下的 paired 结果；只有能力认证、质量门和成本门都通过，才允许 live canary。当前 `docs/superpowers/current.md` 明确 SS15/TT90 尚未完成，不能据此宣称某模型或路由策略更准确。

## 10. 现状标签

**Implemented**：配置级 provider/model 选择、官方/compatible adapter、endpoint canonicalization、model capability certification、V11 execution contract 冻结、恢复时身份校验、结构化 transport 选择。

**Partial**：legacy `AgentRouter` 有任务类型到旧 Agent 的静态路由；它不参与 V11 动态模型选择。V11 可以在创建 Run 时通过配置收紧预算，但不能在运行中切换模型。

**Not implemented**：按角色动态模型路由、质量预测器、在线 bandit、跨模型 ensemble、自动故障切换和成本感知全局调度。

## 11. 面试回答模板

> 我会先区分任务路由和模型路由：`router.py` 是旧任务到 Agent/tool 的静态映射，V11 当前不做动态模型路由。因为兼容 endpoint 的 tool、strict schema、usage、并发和 deadline 能力不能靠供应商名称假设，项目用 model-capability-v1 对精确 provider/model/endpoint 做协议探针，并绑定当前代码、SDK、adapter 和环境。Run 创建时把模型身份、能力工件 hash、工具/Skill manifest、预算和 retry policy seal 进 execution contract，resume 不能换模型。以后若按角色路由，必须把每个 actor 的身份和 routing policy hash 写进合同，并重新做 paired quality/cost/latency 评测；能力认证不等于准确率证明。

## 12. 参考资料与源码

- [OpenAI Agents SDK Models](https://openai.github.io/openai-agents-python/models/)
- [OpenAI Agents SDK Configuration](https://openai.github.io/openai-agents-python/config/)
- [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- 仓库：[model_capability.py](../../backend/services/model_capability.py)、[openai_model.py](../../backend/diagnosis/openai_model.py)、[openai_compatible_model.py](../../backend/diagnosis/openai_compatible_model.py)、[runtime contract](../../backend/domain/runtime.py)、[legacy router](../../backend/diagnosis/router.py)。
