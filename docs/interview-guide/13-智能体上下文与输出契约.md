# 13. Agent 上下文与输出契约

Agent 效果不仅取决于 prompt，更取决于每个角色看见什么、看不见什么、哪些字段由模型填写、哪些字段必须由服务端拥有。

## 1. Context 本身就是能力边界

模型不知道数据库里的一切，只知道 Runtime 明确投影的内容。Context 设计同时控制：

- 隐私：不把敏感原文塞给模型；
- 安全：不暴露 benchmark 标签和内部 marker；
- 成本：避免重复静态字段；
- 注意力：减少无关证据；
- 权责：模型不能修改服务端拥有的 ID 和 owner；
- 可复现性：同一阶段使用稳定的有界结构。

把所有数据塞进大上下文看似信息更多，实际会增加 Token、噪声、提示词注入面和证据遗漏。

## 2. 三种核心投影

### Incident projection

live 路径只保留当前角色需要的 service、environment、severity、title 和必要 description。Critic 可省略 description，因为它应审查候选和证据，而不是重新从事故叙述里发散。

### Evidence projection

给模型的 Evidence 摘要包括：

- `id`；
- `kind`；
- `observed_at`；
- 有界 `summary`；
- `scope_entity_ids`；
- 可用时的 `signal_family`。

Provider 与 status 已由服务端过滤，可从 live prompt 省略。完整 provenance/payload 仍保存在 ledger，不等于被删除。

### Task projection

Investigator 主要看到 title、description、information gap 和 discriminator。Task ID、Agent name、runtime owner 等服务器字段不需要模型重复理解或抄写。

## 3. Evidence digest 如何兼顾短与有区分度

默认最多 4 条、每 kind 最多 2 条。指标不是简单按分数取 Top 4，而是按实体和 signal family 轮转，并为非指标证据留位置。

假设起始证据有：

```text
checkout CPU       变化分 0.95
checkout latency   变化分 0.91
checkout socket    变化分 0.83
checkout error     变化分 0.80
payment CPU        变化分 0.79
connection refused 日志
```

只按分数可能丢掉解释机制的日志；只按 kind 又可能只留下一个 metric。当前选择器在指标信号家族间轮转，并预留非指标锚点，让模型既看到症状强度，也看到可能机制。

Digest 不是 Evidence 真相源。Agent 仍能用工具补查，Critic 也通过引用链读取需要的完整已提交证据。

## 4. 为什么要结构化输出

自由文本可以写得漂亮，却很难稳定回答：

- Candidate ID 在哪里？
- 证据引用有哪些？
- verdict 是 accept 还是 reject？
- 七项检查是否齐全？
- gap 是否明确？
- 哪些字段可以持久化？

Pydantic schema 把输出变成机器可验证的表格。结构正确不代表诊断正确，但至少能机械拒绝明显违规。

## 5. Live compact schema

| Schema | 模型负责 | 模型不负责 |
| --- | --- | --- |
| `LeadPlanningCompactOutput` | 任务语义、信息缺口、Skill | ID、owner、完整 manifest |
| `InvestigatorCandidateOutput` | 实体、分类、机制、Evidence refs | Candidate ID、全局 rank、Run 归属 |
| `CriticCompactOutput` | candidate ref、verdict、七项 checks | Assessment ID、真正补证 Task ID |
| `LeadAdjudicationOutput` | 测试/适配路径的终态语义 | current live 路径不额外调用它 |

生产 prompt 不要求模型重复服务器已经知道的字段，能减少 Token、幻觉字段和并行主键冲突。

## 6. 服务端拥有字段原则

以下字段不能交给模型自由决定：

- 数据库主键；
- `runtime_run_id`；
- Task/Assessment ownership；
- execution attempt；
- model/provider identity；
- tool manifest；
- rank 的全局合并序号；
- budget 使用量；
- durable status；
- capability/skill hash。

模型是非确定性输入源，不是事务系统。多个并行 Agent 很容易同时生成 `candidate-1`，也可能把别的 Run ID 抄进输出。

## 7. failure_class 与 failure_mechanism

这是最新领域模型的重要拆分：

| 字段 | 目标 | 好例子 | 坏例子 |
| --- | --- | --- | --- |
| `failure_class` | 稳定、短、便于评分/聚合 | `socket`、`disk`、`memory` | “服务似乎有某种异常” |
| `failure_mechanism` | 人能理解的因果过程 | “连接建立被拒绝导致请求超时” | 只重复 `socket` |

如果 metric Evidence 已有 `signal_type=socket`，prompt 要求沿用这个稳定分类。Trace/依赖适合定位故障连接，日志适合解释资源或错误机制。

## 8. Finding、Candidate、Assessment、Decision 的区别

| 对象 | 回答什么 | 是否一定发布 |
| --- | --- | --- |
| Finding | 调查中发现了什么 | 否 |
| Candidate | 哪个根因值得正式审查 | 否 |
| Assessment | Critic 如何评价候选 | 否 |
| LeadDecision | 哪些候选获得终态权威 | 决定发布集合 |
| Root cause projection | 对外输出的根因 | 是 |

Observation Finding 不能自动成为根因。Candidate 即使留在数据库里，也可能被 Critic reject 或被终态标为 unpublished。

## 9. 引用链如何限制幻觉

候选不能只说“我看到了日志”，必须提交 Evidence ID。机械校验至少保证：

- ID 存在且已提交；
- Evidence 状态适合支撑结论；
- Evidence 属于同一个 Runtime Run；
- scope 不与 affected entity 明显冲突；
- Critic check 引用同样合法；
- authority decision 只选择允许候选。

机械层无法证明语义有说服力，所以需要 Critic；Critic 再聪明也不能绕过 ID 所有权。

## 10. multi 交叉证据要求的精确边界

multi Investigator prompt 要求候选引用至少两条不同、共同支持同一实体和机制的可用 Evidence，相关时优先不同 kind/provider。若摘要只有一条相关证据，应做一次有界查询；找不到就不产出候选。

但当前共享 Pydantic/准入机械下限仍为至少一条 supporting Evidence。代码能验证两条 ID 是否存在，却不能机械证明它们语义独立。因此应描述成：

> prompt 强制要求交叉印证，Critic负责语义审查，机械层负责引用合法性。

不能夸大为“Validator 已证明两条证据相互独立”。

## 11. Gap 为什么能引用 failed/skipped Evidence

普通结论只能引用 usable Evidence，即 `success/partial`。Gap 表达“尝试取得某证据但失败”，因此可以引用已提交的 `failed/skipped` 记录。

```text
错误：Provider 超时，所以数据库就是根因。
正确：Provider 超时，无法验证数据库延迟，留下 database latency gap。
```

失败记录是缺口的审计依据，不是正向因果证据。

## 12. 控制字符与不可信文本

Provider、Tool 和模型输出都视为不可信。结构化输出安全基类递归扫描字符串，拒绝 ASCII 控制字符和 DEL。这样能：

- 防止日志/终端展示被伪造；
- 避免 JSON、数据库和报告出现不可见边界；
- 让无效文本在模型边界进入有限重试；
- 不把危险原文带入后续 Agent prompt。

Redaction、长度限制和安全错误枚举也在不同边界执行。系统不持久化私有 Chain-of-Thought。

## 13. Prompt 与代码各负责什么

| 规则 | Prompt | 代码 |
| --- | --- | --- |
| 选择有价值调查方向 | 主责 | 只校验边界 |
| multi 候选相关交叉证据 | 明确要求 | 校验引用合法，不判断语义独立性 |
| 工具只读 | 提醒 | 强制 manifest/exposure |
| Evidence ID 存在 | 提醒 | 强制 |
| 七项检查 | 解释目的 | 强制名称、数量、状态 |
| 不足时 inconclusive | 引导判断 | 限制合法终态 |
| 不泄漏标签/秘密 | 指示 | 包隔离、投影、redaction |

成熟 Agent 系统不会把硬安全边界只写在 prompt 里，也不会把语义判断全部硬编码。

## 14. 面试回答模板

> 我把 context 和 output schema 当成 Agent 的权限边界。每个角色只看到完成职责所需的事故、任务和证据投影；Investigator 的起始 Evidence digest 最多四条，并按实体、signal family 和非指标锚点选择。live schema 只让模型填写诊断语义，ID、owner、manifest、rank 和预算都由服务端拥有。所有结论沿 Finding—Candidate—Assessment—Decision 引用持久化 Evidence ID，控制字符和非法引用在边界拒绝。这既减少 Token，也降低模型伪造服务器状态的机会。
