# 14. Agent 预算、重试与失败语义

Agent 系统真正难的部分，不是成功时能不能给答案，而是超时、半成功、重试、并发和恢复时是否仍守住同一份资源与安全合同。

## 1. 为什么需要多种预算

只限制 Token 不够：Agent 可以用很少 Token 发很多昂贵查询。只限制工具次数也不够：模型可能无限自我反思。

V11 同时限制：

- Run 硬 deadline；
- durable model turn 总数；
- Token 总预算；
- 全局工具调用数；
- 每个 Investigator 工具次数；
- Investigator 数；
- 最大并发步骤；
- 调查轮数；
- 单工具 timeout；
- model retry 次数。

这些是执行合同的不同维度，任何一个耗尽都不能从另一个维度借额度。

## 2. Token 为什么先预留再结算

并发模型调用若都先执行、结束后再记账，可能一起超过总预算。Runtime 使用 reservation：

1. 估算 prompt 与 context 输入；
2. 在锁内预留输入和最大输出额度；
3. 预算不足则在跨模型边界前拒绝；
4. 完成后读取可信 usage；
5. 按实际消耗结算；
6. 释放未使用输出额度。

若 Provider 没有可靠 usage，系统使用受审计估算，不能假装消耗为零。

## 3. 并行 Investigator 怎样公平分配

Round 1 不能让第一个启动的 Agent 把所有剩余 Token 预留走。Investigator 不直接拿“当前全部剩余预算”，共享 reservation 逻辑会考虑并发槽。

这解决资源公平，不保证每个 Investigator 使用完全相同的 Token。任务可以提前结束，实际 usage 仍各自结算。

## 4. Model turn 是 Run 级耐久预算

`max_turns` 不是每次 SDK 调用重新获得的额度。每个真实 model request 都从 Run 级剩余 turn 扣除，并进入 Checkpoint，retry/resume 继续沿用。

multi run 中单个 Investigator 的 SDK ceiling 额外限制为 2，以便默认 8-turn 配置还能覆盖 Lead planning、多个 Investigator、Critic 和有限纠正。这是 actor-level 更严格上限，不增加 Run 总额度。single control 保留完整 SDK ceiling。

## 5. 工具预算怎样扣

`AdaptiveToolSession` 同时检查：

- 当前 Investigator 已调用多少次；
- 全 Run 已调用多少次；
- 查询是否重复；
- 是否触发 no-new-evidence stop；
- deadline 是否有效；
- lease/cancel fence 是否允许跨外部边界。

工具 retry 仍属于同一逻辑调用和总预算，不会因为换 attempt 就免费。

## 6. 重试不是“失败了就再来一次”

| 故障 | 当前策略 |
| --- | --- |
| 模型结构化输出无效 | 在模型外层上限内反馈安全 contract error 后重试 |
| SDK API timeout | 分类为 timeout，可走模型外层受控 retry |
| SDK 5xx | 分类为 transport，可走模型外层受控 retry |
| rate limit | 按分类和上限处理 |
| 工具 Provider transport/rate limit | 最多一次外层重试 |
| 裸 `TimeoutError` 工具调用 | 不泛化为可重试 |
| Critic 非法引用 | 特定边界给一次精确白名单 correction |
| 预算、权限、manifest 违规 | 不重试 |

裸工具 timeout 不自动重试，因为远端可能仍在执行。盲目重发会造成重入、双倍成本并越过 deadline。

## 7. 为什么 SDK 内重试与外层重试不能叠加

若 SDK 自己重试三次，Runtime 外层又重试多次，可能产生乘法级请求，而持久化审计只看到少数逻辑尝试。

V11 把 SDK/provider 内部 retry 关闭，把 retry ownership 放在可持久化外层。这里要区分两条当前实现：工具 Provider 的外层 `RetryCoordinator` 最多 retry 一次；模型 `_call_model` 当前配置 `max_retries=3`（即最多四次尝试）并使用 5 秒起、30 秒封顶的 backoff。冻结 contract 中的 `retry_policy.max_retries=1` 仍是需要与模型实现对齐的契约事实，不能笼统说“所有 V11 retry 都只有一次”。

- 每个 request/attempt 有稳定身份；
- 失败先写终态；
- backoff 有界；
- 重试前重查 owner、deadline、budget；
- reservation 可复用，但每个真实 model turn 都计数；
- 恢复时不重复结算已 settle usage。

## 8. Backoff 为什么必须有 cap

指数退避可缓解短时拥塞，但无上限会睡过 Run deadline。配置 backoff base 时必须同时有 cap，每次等待还要考虑剩余 deadline。

重试不能绕过用户取消。取消或 lease 丢失后，等待中的工作必须停止，晚到结果也不能写入。

## 9. 四类终态

| 状态 | 精确含义 |
| --- | --- |
| `complete` | 流程完整，权威候选可发布，无影响完整性的降级 |
| `partial` | 有可发布诊断，但部分调查能力或证据失败 |
| `inconclusive` | 流程安全结束，但没有足够证据发布根因 |
| `failed` | 必需阶段、结构契约或执行安全条件失败 |

`partial` 不是“置信度较低”的同义词，而是执行/证据完整性受损。`inconclusive` 也不是系统报错，而是高风险诊断中的合法结果。

## 10. 失败矩阵

| 位置 | 当前行为 | 原因 |
| --- | --- | --- |
| Lead planning 失败 | failed | 没有合法任务计划 |
| 单个 Round 1 Investigator 失败 | 兄弟继续，记录降级 | 保留局部成功，不掩盖失败 |
| 全部 Round 1 Investigator 失败 | failed | 没有完成的调查者 |
| 单个 Candidate 草稿违规 | 只拒绝该草稿 | 不让局部脏数据杀死兄弟结果 |
| 没有 Candidate | 可 inconclusive | 安全完成但证据不足 |
| Critic 失败 | failed，候选留审计 | 未审候选不能发布 |
| Critic 引用错误一次 | 精确白名单 correction | 错误可机械定位且范围有限 |
| Round 2 ownership 不完整 | failed | 补证必须归属于 Assessment |
| Round 2 任一选中任务失败 | failed | 它是 Critic 指定的关键补证 |
| live authority 无 accepted 候选 | inconclusive | 不凭规则发明根因 |
| live validation 证据不足类错误 | 固定 inconclusive degradation | 可安全撤销发布 |
| live validation 结构错误 | failed | 不能用无上下文模型调用掩盖损坏 |

## 11. 为什么保留失败候选审计

若 inconclusive 时把 Candidates 全部清空，会失去：

- 模型曾考虑什么；
- Critic 为什么拒绝；
- 哪条 Evidence 缺失；
- 哪个 Candidate 因非法引用被丢弃；
- 系统是没有发布，还是从未产生候选。

当前 inconclusive 只清空对外 `root_causes`。Candidate、Assessment 和 unpublished reason 留在审计投影，prediction exporter 只读取 authority IDs。

## 12. 失败类别为什么结构化

“调用失败”无法支持运营。项目用安全 failure category 区分 timeout、transport、rate limit、invalid output、invalid reference、budget、cancellation、lease/ownership、provider gap 等。

分类用于决定能否重试、如何显示、能否恢复和怎样统计。原始异常可能含 URL、凭证或 payload，因此只持久化安全分类和有界说明。

## 13. Resume 时预算为什么不能重置

崩溃不是免费额度。Checkpoint 保存剩余预算、成功工具幂等键、阶段引用和 digest。恢复时：

1. 验证 execution contract；
2. 验证 Checkpoint digest；
3. 验证 owner、lease 和 deadline；
4. 继承剩余 Token、turn、工具额度；
5. 复用已成功 ToolCall；
6. 从下一安全阶段继续。

否则反复 resume 就能获得无限调用，评测也无法公平比较。

## 14. 面试回答模板

> V11 的预算不是一个 max_tokens 参数，而是 durable Run 合同：deadline、model turns、Token、全局和单 Investigator 工具数、并发、轮次、工具 timeout 都分别受限。Token 在并发调用前预留、按可信 usage 结算；retry 和 resume 沿用同一 reservation 与剩余额度。重试只针对分类后的瞬时故障，权限、预算和结构错误不重试。结果明确区分 complete、partial、inconclusive、failed；局部 Investigator 失败可以保留兄弟结果，但 Critic 或结构性 authority 失败必须 fail-closed。
