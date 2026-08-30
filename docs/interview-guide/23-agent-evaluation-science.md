# 23. Agent 评测科学：不只看最终答案

## 1. Agent 评测为什么比普通分类更难

普通分类可以把输入和标签直接比较。Agent 还会改变环境（调用工具、写状态、消耗预算），同一个最终答案可能来自完全不同的证据链：

- 一条是合法工具调用后得到的结论；
- 一条是没有查证据、碰巧猜对；
- 一条是越权读取后得到正确答案；
- 一条是引用不存在的 Evidence。

如果只看 Top-1 accuracy，会把后三种错误算成成功。生产 Agent 至少需要三层评测：

```text
结果层：答案/实体/机制/时间是否正确
轨迹层：计划、工具、证据、引用和停止是否合理
安全层：是否越权、泄密、超预算、破坏可恢复性
```

## 2. 当前 DiagOps 的 RCAEval 评测设计

V11 通过 `backend/benchmarks/rcaeval` 使用 opaque incident package，Agent 只看到 provider-neutral telemetry，标签由独立 custodian package 保存。`docs/superpowers/current.md` 说明正式 SS15/TT90 尚未完成，当前不能宣称准确率提升。

### 2.1 对照拓扑和预算

| 配置 | 拓扑 | Token 预算语义 |
| --- | --- | --- |
| `single_intended` | 一个 Investigator、一个 context、无 Critic | `B` |
| `multi_intended` | Lead + 最多 3 Investigator + Critic、最多 2 轮 | `≤3B` |
| `single_equal_token` | 单上下文 | `3B` |
| `multi_equal_token` | Multi 拓扑 | `3B` |

单/多两组共享 case、model/provider、工具/Skill、normalizer 和 source identity；每个配置自己的 prediction/output schema 与 topology 会被单独冻结，不能把它们误说成完全相同。公平比较必须确保除预先声明的拓扑和预算（以及其必要 schema）外没有隐藏变量。Multi 不是因为调用次数更多就天然更好，equal-token 组用于区分“多花钱”和“结构收益”。

### 2.2 冻结与隔离

正式 bundle 绑定：

- clean source commit 和 source manifest hash；
- endpoint/model/capability artifact；
- prompt、tool manifest、Skill catalog、prediction schema、normalizer、依赖锁和 retry policy hash；
- memory snapshot；
- 每个 case 的 runtime_run_id、execution contract hash、Evidence ID；
- SS15/TT90 partition 与 custodian ledger。

labels 在预测和 evidence audit 完成前不可见；修改输出目录、相对路径、符号链接或混合身份不能绕过 ledger。这个设计同时防止 benchmark leakage、结果拼接和“看完答案再调 prompt”。

## 3. 结果指标

`EvaluationSummary` 当前包含：

```text
exact_top1          服务+故障机制（严格 Top-1）
component_top1      受影响实体/服务 Top-1
mechanism_top1      故障分类/机制 Top-1
top3                候选 Top-3 命中
time_window_accuracy onset 时间窗兼容性（字段可为空，当前评测器未必计算）
completion_rate     合法完成比例
reference_integrity Evidence 引用合法比例
p95_latency_ms      运行延迟
input/output_tokens、estimated_cost_usd（可为空，需明确价格/计算路径）、tool_calls
read_only_violations、leakage_violations、failed_cases
```

不要把 `failure_class` 和 `failure_mechanism` 混成一个自由文本评分：前者用于稳定分类和比较，后者用于解释。空候选/`inconclusive` 也要计入 abstention 和 completion 统计，不能从分母删除。

## 4. 轨迹和工具指标

建议为每个 case 导出不含敏感正文的 trajectory summary：

```text
plan_validity                  Lead task 是否满足边界
tool_precision                 成功调用中真正与任务相关的比例
invalid_call_rate              schema/scope/manifest 拒绝比例
duplicate_query_rate           重复 fingerprint 比例
evidence_yield                 新 usable Evidence / successful calls
citation_precision/recall      引用是否来自允许 ledger、关键 ID 是否保留
critic_check_coverage          七项 check 是否完整且证据状态合法
stop_reason_quality            是否在 sufficient/no-new/budget/deadline 时停止
retry_correctness              retry 是否分类、幂等且不越过预算
```

工具调用“越多越努力”是错误指标；如果 20 次查询只带来 0 条新证据，应比 3 次有效查询更差或触发成本告警。

## 5. 安全与过程门

任何以下事件都应独立成为 gate，而非被平均准确率稀释：

- read-only violation > 0；
- leakage/benchmark label exposure > 0；
- 跨 Run Evidence 或伪造 ID；
- 超过 token/tool/turn/deadline budget；
- 未经 Critic accepted 的候选被发布；
- checkpoint/replay 重复调用外部 Provider；
- 非法 output 被“自动修好”后无法追溯。

当前 acceptance policy 的关键门包括：reference integrity 100%、evidence support 至少 0.95、read-only/leakage 违规为 0、p95 latency 不超过冻结上限、Multi 相对 Single 的绝对 delta 和 token ratio 上限。具体数值以 `backend/benchmarks/rcaeval/models.py` 的冻结模型为准，不能自行在报告里改写。

## 6. LLM-as-a-judge 怎样使用才不自欺

LLM judge 适合评估开放式 explanation 的可读性、因果链和是否覆盖反证，但它本身可能偏爱流畅文本、受候选顺序影响或复制被评测模型的偏差。正确做法：

1. 先用确定性规则计算 ID、scope、时间、结构和安全指标；
2. 对 judge 提供盲化、固定顺序、最小必要证据；
3. 固定 rubric、模型、温度/seed（若支持）和版本；
4. 用人审样本估计 judge precision/一致性；
5. 对边界/争议案例保留双 judge 或人工仲裁；
6. 报告 judge 与 deterministic 指标分开，不用一个分数替代全部质量。

## 7. 统计比较：为什么需要 paired 分析

Single 和 Multi 应在同一 case 上成对比较。设 `d_i = multi_i - single_i`：

```text
平均 delta = Σ d_i / n
bootstrap CI = 对 case 索引有放回重采样得到 delta 分布
McNemar     = 只看二元命中/未命中的 discordant pairs
```

当前 RCAEval 模型包含 `BootstrapResult` 和 `McNemarResult`，并保存 seed、sample 数和 indices hash，保证别人能重算同一个区间。不要把重复运行同一 case 当成独立样本；TT90 的 repetition 仍应按协议聚合和解释。

## 8. 数据污染、泄漏和可重复性

评测前应检查：

- 训练/提示词是否包含 holdout incident 或标签；
- verified memory 是否意外含未来 case；
- provider package 是否暴露 ground truth 字段、case label 或 injection marker；
- 输出 artifact 是否包含标签路径、scorer 结果或隐藏答案；
- source/依赖/endpoint/环境是否在两组一致；
- 随机排序、时间、并发和 retry 是否可重放。

Prediction、Evidence audit、label-open 和 acceptance artifact 需要 hash 链；任何 post-freeze 改动应使评测拒绝，而不是“重新算一个更好分数”。

## 9. 从离线到线上

推荐四阶段：

```text
离线 golden/对抗集
  → shadow/canary（不影响用户结论）
  → 小流量受控发布 + 人工审核
  → 在线 SLO/反馈/回滚
```

线上反馈不能直接写入 verified memory 或训练集；先脱敏、去重、人工验证和版本化。在线指标应监控分布漂移、inconclusive、成本、延迟和工具失败，而不只监控用户点赞。

## 10. 教育版评测 harness 伪码

```python
def evaluate_case(case, topology, budget, seed):
    run = launch_frozen_run(case, topology=topology, budget=budget, seed=seed)
    assert run.contract_hash == frozen_contract_hash
    prediction = export_public_prediction(run)
    trajectory = export_safe_trajectory(run)
    deterministic = score_structure_and_safety(prediction, trajectory)
    semantic = score_against_labels_only_in_custodian(prediction)
    return {**deterministic, **semantic}

paired = [
    (evaluate_case(case, "single", B, seed),
     evaluate_case(case, "multi", 3 * B, seed))
    for case in shared_cases
]
report = paired_bootstrap_and_mcnemar(paired)
```

教育版伪码省略了 label-open lease、artifact hash 和隔离进程；真实入口是 `python -m backend.benchmarks.rcaeval` 的 `launch-predict`、`freeze-set`、`audit export` 和 `evaluate`，不能直接调用内部 prediction worker 冒充正式 run。

## 11. 现状标签

**Implemented**：OpenRCA 兼容评测、RCAEval opaque package、Single/Multi/equal-token 配置、运行契约与能力身份冻结、Prediction/Evidence audit、paired bootstrap/McNemar 模型、read-only/leakage/reference gates。

**Partial**：结构/安全/Token/延迟指标较完整；`time_window_accuracy` 与 `estimated_cost_usd` 是 nullable contract 字段，当前评测器不一定计算。通用 trajectory quality、judge calibration、在线 shadow dashboard、统一定价和长期数据漂移监控仍需补齐。

**Not implemented**：大规模在线 A/B 平台、自动 prompt optimizer、跨任务 bandit 评测、统一多模型质量基准和自动回滚策略。

## 12. 面试回答模板

> 我不会只报 Agent 的准确率。DiagOps 的 V11 评测把结果、轨迹和安全分开：结果看 exact/component/mechanism/top3/时间窗，轨迹看 plan、tool、evidence yield、citation、Critic 七项检查和 stop reason，安全看 read-only、leakage、预算和 replay。Single/Multi 在同一 opaque case 上成对运行，SS15 还提供 equal-token 对照，固定 endpoint、prompt、manifest、Skill、memory、代码和依赖 hash；用 paired bootstrap 与 McNemar 给不确定区间。LLM judge 只做开放式解释的补充，确定性引用/权限校验优先。正式 SS15/TT90 尚未完成，所以当前不能声称 Multi 已提升准确率。

## 13. 学习核对清单

- [ ] 能解释结果、轨迹、安全三层评测的区别。
- [ ] 能说出 `single_intended`、`multi_intended`、equal-token 的公平性意义。
- [ ] 能列出至少五个 tool/evidence/citation 指标。
- [ ] 能解释为什么 paired bootstrap 比独立均值比较更合适。
- [ ] 能指出 LLM-as-a-judge 的偏差和校准方法。
- [ ] 能说明 label isolation、memory snapshot 和 freeze hash 防什么泄漏。

## 14. 参考资料

- [OpenAI Evals / agent evaluation guidance](https://platform.openai.com/docs/guides/evals)
- [OpenAI Agents SDK tracing](https://openai.github.io/openai-agents-python/tracing/)
- 仓库：[RCAEval models](../../backend/benchmarks/rcaeval/models.py)、[runner](../../backend/benchmarks/rcaeval/runner.py)、[audit](../../backend/benchmarks/rcaeval/audit.py)、[evaluation chapter](08-evaluation-and-testing.md)。
