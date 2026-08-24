# 08. 评测与测试：怎样证明它不是一个 Demo

## 1. 三种不同的“正确”

Agent 系统不能只看最终答案，还要区分：

1. **软件正确性**：状态机、校验、数据库、并发是否按契约工作；
2. **安全与可靠性**：越权、泄密、超时、取消、恢复是否受控；
3. **诊断效果**：根因判断是否真的比对照更好。

单元测试能证明前两类的一部分，不能直接证明第三类；Benchmark 能衡量效果，但不能代替软件可靠性测试。

## 2. 测试目录覆盖什么

| 测试区域 | 主要验证 |
| --- | --- |
| `tests/domain` | Pydantic 契约、状态和引用规则 |
| `tests/tools` | manifest、ToolRegistry、verified memory |
| `tests/providers` | 文件、Prometheus、Tempo、local package 等 Provider |
| `tests/diagnosis` | Planner、Agent Runtime、V11 flow、校验、重试 |
| `tests/runtime` | 并发、幂等、Checkpoint、Replay、Diff、SSE、故障注入 |
| `tests/db` | SQLite、迁移、并发、历史记录读取 |
| `tests/api` | HTTP 契约与错误映射 |
| `tests/frontend` | 页面关键投影和 smoke |
| `tests/reports` | Evidence 引用与 V11 报告 |
| `tests/safety` | 脱敏和敏感值拒绝 |
| `tests/services` | 离线、Runtime、生产 lab、Tempo 等验收 |
| `tests/benchmarks` | 数据隔离、冻结工件、runner/evaluator/custodian |

## 3. 为什么测试特别多

Agent 输出有随机性，所以系统必须把可确定的部分测得更严格：

- 模型可以选择哪个候选，但不能引用不存在的 Evidence ID；
- 模型可以决定 inconclusive，但不能绕过 Critic；
- 模型请求工具的内容会变，但 schema 和 scope 必须稳定；
- Provider 会失败，但状态必须显式；
- 并发顺序可能变化，但事件序列、owner 和事务完整性不能变化。

因此大量测试不是在固定模型答案，而是在固定“无论模型说什么，系统都必须守住的边界”。

## 4. Key-free 测试与付费 live gate

项目刻意分开：

- **Key-free deterministic tests**：不需要 API key，验证合约、编排、故障和安全；
- **Paid live gate**：使用指定真实模型，验证结构化输出、工具调用和效果；
- **Formal benchmark**：冻结源码、模型、预算、数据和评测流程后才能形成发布结论。

不能因为 fake model 测试通过，就宣称真实模型效果通过；也不能因为一次 live 演示成功，就宣称可靠性门通过。

## 5. Runtime Acceptance

命令：

```powershell
uv run python -m backend.services.runtime_acceptance
```

它验证 durable Runtime 的关键场景，例如：

- 正常阶段执行；
- 并发 Run 与单 Run 并行；
- 取消和超时；
- lease 过期与恢复；
- Tool 幂等；
- Replay 与 Diff；
- SSE 重连；
- Writer/Checkpoint 故障注入；
- 隐私和 allowlisted event payload。

结果写入 `output/runtime-acceptance/<run-id>/result.json`。它是工程可靠性验收，不是诊断准确率分数。

## 6. Production Acceptance Gate

Compose Gate 用多个服务、Prometheus、Alertmanager 和只读证据挂载跑受控事故场景。故障端点只存在于 gate image，且某些场景只导出有界指标，不会真的耗尽内存、破坏网络或杀进程。

它检查：

- 场景是否都完成；
- Top-1 原因、组件和机制；
- onset 时间误差；
- Evidence 引用有效性；
- 只读违规为零；
- 隐私扫描；
- 总时长。

这仍是本地受控环境，不应宣传为“经过真实生产验证”。

## 7. OpenRCA：历史兼容 Benchmark

OpenRCA 是微软公开的 RCA 数据集/评测流程。项目保留它用于：

- 验证 fixed/adaptive/V11 投影兼容；
- 比较同一模型和 prompt 下不同策略；
- 生成 upstream evaluator 可读取的 prediction CSV；
- 防止产品改动破坏已有 benchmark 流程。

关键隔离：

- `prepare` 可以读 ground truth 并生成 safe runtime index；
- `run` 只接收不含答案的 runtime cases；
- `evaluate` 在 prediction 冻结后读取答案；
- 本地 compatible report 不能冒充 upstream official report；
- 小 fixture run 不能冒充完整 40-case live gate。

V11 设计把 OpenRCA 定位为历史兼容回归，而不是让生产架构迎合它的标签体系。

## 8. RCAEval：V11 受控效果评测

RCAEval 的目标是比较：

- 冻结的单 Investigator/单 Agent 对照；
- 预期的 V11 Multi-Agent；
- equal-token 等消融配置。

它强调公平性：

- 同一 held-out 数据；
- runtime package 与 label package 分离；
- 同一 capability-certified endpoint/model；
- Token 比例有上限；
- 源码、依赖、工具 manifest、skill catalog 和 Provider profile 身份冻结；
- prediction set 先 freeze；
- label open 由 custodian ledger 约束；
- 手工 Evidence audit 在开标签前完成。

## 9. 为什么要 Model Capability Certification

“OpenAI-compatible” 只说明接口长得像，不保证：

- Function Tool 正确；
- strict structured output 正确；
- usage 数字可用；
- max output token 生效；
- 配置的并行度能工作；
- 超时行为满足要求。

所以正式预测前要对精确 endpoint/model tuple 生成 capability artifact，并绑定：

- endpoint identity；
- model 和 API mode；
- SDK/adapter 版本；
- tool/structured output/usage 等能力；
- 测试并行度；
- source manifest hash；
- artifact hash。

Run 创建时验证该工件，避免只凭厂商名称作能力假设。

## 10. 指标不仅是准确率

应观察：

- 根因字段准确率/partial score；
- completion rate；
- Evidence reference validity；
- inconclusive/failed/timeout 分类；
- 工具调用数与重复拒绝；
- Token、成本、延迟；
- 只读违规；
- 各 partition 表现；
- 人工 Evidence audit 支持率；
- 单 Agent 与 Multi-Agent 的公平预算比较。

如果准确率提升但 Evidence 引用无效或成本失控，也不能接受。

## 11. 当前项目状态

[current.md](../superpowers/current.md) 明确记录：

- 当前版本目标是 V11；
- M4 baseline 已有固定 commit；
- M5 engineering 和 SS15 protocol amendment 已进入 main；
- implementation 被 capability admission / SS15 / TT90 gate 阻塞；
- 没有 completion commit；
- 不允许声明准确率提升或 release 完成。

面试中最诚实的说法是：

“工程实现和评测协议已经构建，但正式持有集效果门尚未完成，所以我会展示架构、测试和已验证的工程能力，不会虚构最终准确率结论。”

## 12. 最小验证命令

仓库级 release claim 通常要求：

```powershell
uv run ruff check .
uv run pytest -v
uv run python -m backend.services.runtime_acceptance
npm.cmd --prefix frontend run build
```

普通改动按风险跑更小范围。文档修改主要检查链接、格式与 `git diff --check`；不要为了面试准备擅自运行付费 live gate 或正式受控评测。

## 13. 面试回答模板

“我把验证分成工程正确性、安全可靠性和诊断效果三层。单元与集成测试固定领域契约、九工具白名单、引用校验、SQLite 迁移、并发、取消、幂等、Checkpoint、Replay 和 SSE；key-free acceptance 不调用真实模型。Production Gate 验证本地受控多服务链路。OpenRCA 只作为历史兼容回归，V11 效果用标签隔离、源码与能力工件冻结的 RCAEval 比较单 Agent 和 Multi-Agent，同时统计证据有效性、Token、成本、延迟和只读违规。当前正式 SS15/TT90 尚未完成，所以不能声称准确率已经提升。”
