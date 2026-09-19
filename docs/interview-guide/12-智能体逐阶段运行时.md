# 12. Agent 逐阶段运行时：一次诊断到底发生了什么

本章把一次 V11 Run 当成流水线拆开。目标不是背函数名，而是理解每个阶段的输入、模型职责、确定性职责和持久化结果。

## 1. 先分清三个容器

| 对象 | 含义 | 通俗类比 |
| --- | --- | --- |
| Investigation | 一次业务调查，包含事故、证据、报告和历史 Run | 一份病例 |
| Runtime Run | 一套诊断方案的实际执行，有冻结合同、deadline 和状态 | 一套检查方案 |
| Attempt | 某个进程对 Run 的一次执行占有期 | 一次值班接手 |

进程崩溃后可以用新 Attempt 恢复同一个 Run。Attempt 变化不等于重开调查，也不会获得新预算。

## 2. Run 创建时冻结什么

`execution_contract` 会保存：

- execution contract version 与 authority；
- endpoint、model、provider 和 API mode；
- compatible endpoint 准入时的 capability artifact 身份；
- 精确九工具 manifest 及哈希；
- Skill catalog 版本和哈希；
- Token、model turn、工具次数、每个 Investigator 工具次数、Investigator 数、轮数、deadline、retry policy 与 topology；
- contract digest。

phase profile 由 `execution_contract_version` 选择；Provider profile/source package identity 与 `max_parallel_steps_per_run` 不属于所有产品 Run 的 contract 字段，前两者在特定 benchmark/Provider 工件中另外冻结，后者来自当前 Runtime 设置。

恢复时必须继续使用同一合同。新部署不能让旧 Run 无声获得新工具、新预算或新模型。

## 3. 阶段 0：初始证据收集

进入 Agent 推理前，[阶段执行器](../../backend/runtime/phase_executor.py) 会调用 Provider registry：

1. Provider 读取事故允许范围内的日志、指标或离线包；
2. 返回 `ProviderResult`；
3. 构造 `SpecialistResult` 和 `DiagnosisContext`；
4. 把 Evidence 持久化到当前 Investigation/Run；
5. V11 不把这一步伪装成旧版 deterministic RCA execution。

这批证据是起始材料，不是结论。后续 Investigator 仍可通过九工具补查。

## 4. 阶段 1：Lead planning

入口是 `V11Runtime.plan_lead`。

### 模型看什么

live prompt 使用紧凑事故投影，主要保留 service、environment、severity、title、必要时的 description 和剩余预算。静态工具目录、服务器已知字段和冗长完整对象不重复塞进 prompt。

### 模型交什么

live 路径使用 `LeadPlanningCompactOutput`，任务草稿只表达：

- title；
- description；
- information gap；
- 可选 discriminator；
- 选择的调查 Skill。

### 服务端做什么

`_build_plan` 验证任务数、planning action、信息缺口、Skill 和工具清单，然后生成 ID、owner、round、agent name。计划和任务在 Investigator 开始前就持久化，恢复时不会重新随机规划。

## 5. 阶段 2：Round 1 Investigator

`investigator_round_1` 最多选三个 Task，用 `asyncio.gather` 并行执行，并受共享并发门限制。每个任务建立独立 `AdaptiveToolSession`，包含事故、Task、起始 Evidence、冻结 manifest、两级工具预算、工具超时、ownership fence 和幂等接口。

### Evidence digest 不是简单截前四条

Round 1 的调用点传入 `max_total=4`、`max_per_kind=2`，只选择 `success/partial` Evidence。该辅助函数自身的通用默认值更大（`max_total=16`、`max_per_kind=4`），不要把调用点策略误说成函数全局默认值。

对带 `signal_type` 和实体的指标，选择器会：

1. 按实体分组；
2. 在同一实体内轮转不同 signal family；
3. 优先变化分数更高的条目；
4. 最多覆盖若干高价值实体；
5. 为日志、Trace、依赖等非指标证据至少保留一个位置。

这样不会因为 CPU 分数最高，就把 socket、disk、error 等因果区分信号全部挤掉。完整 Evidence ledger 仍在数据库里，digest 只是给模型的起点。

### Task scope 是过滤偏好，不是信息删除器

`_evidence_for_task` 只排除与 Task scope 明确冲突的 Evidence。没有 scope 的初始证据会保留；若过滤结果为空，会退回完整起始集合，避免关键上下文丢失。

### 工具循环与 turn ceiling

每次工具调用先检查 schema、权限、预算、deadline、scope 和重复查询，再跨 Provider 边界。ToolCall 与 Evidence 持久化成功后，模型才拿到新 Evidence ID。

multi run 中每个 Investigator 的 SDK request ceiling 是 2，典型是一轮请求工具、一轮收口输出，目的是在全局默认 8-turn 合同里给 Critic 留位置。single Investigator control 保留完整 ceiling。

### 输出准入

live 路径有 Evidence digest 时使用 `InvestigatorCandidateOutput`，最多交一个候选。服务端：

1. 先提交 ToolSession 的 ToolCall/Evidence；
2. 解析结构化输出；
3. 校验 Finding/Evidence 引用；
4. 校验字段与 scope；
5. 给候选生成服务端 ID；
6. 写入 candidate lifecycle audit；
7. 合并并分配稳定 rank。

审计只保存候选数量和安全 SHA-256 摘要，不保存原始模型输出正文。

## 6. Round 1 的局部失败

- 一个 Investigator 失败、兄弟完成：继续；
- 一个候选违规、兄弟候选合法：只拒绝违规候选；
- 所有选中 Investigator 都失败：Run 失败；
- 执行正常但无 Candidate：可安全走 inconclusive。

局部失败会进入 failure memory 和 execution audit，后续不得伪装成无损 complete。

## 7. 阶段 3：Critic review

`critic_review` 只在有候选时调用模型。Critic 看到的是候选、Finding 和 Assessment 引用链所需的、同 Run 且状态可用的 Evidence，不是整个 Run 的任意噪声。

候选数受控时 live 使用 `CriticCompactOutput`，每个 candidate ref 必须有恰好七项检查。Candidate ID 或 Evidence ID 越过白名单时：

1. 第一次响应被拒绝；
2. prompt 追加精确 candidate/evidence whitelist；
3. 允许一次 correction；
4. 仍失败则 Critic 阶段失败。

Critic 没有工具。缺证据时只能声明 gap 并请求补证，不能自行制造新 Evidence。

## 8. 阶段 4：可选 Round 2

只有 `needs_evidence` Assessment 能拥有 Round 2 Task。服务端生成真正 Task ID，并把 ownership 写回 Assessment。

执行前检查每个 Task 是否绑定现存 Assessment；不完整就 fail-closed。当前实现要求选中的 Round 2 执行全部完成，任一失败会终止关键补证批次。完成后只允许一次 `critic_reconciliation`，并复用原 Assessment ID，禁止第三轮。

## 9. 阶段 5：Lead adjudication / authority projection

这是最容易被旧文档说错的阶段。

### Live 路径

`self.turn is None` 时，`_critic_authority_decision`：

1. 找到仍存在的 Candidate ID；
2. 选择 Critic verdict 为 `accept` 的 candidate refs；
3. 聚合 Candidate、Assessment 和 checks 引用的 Evidence IDs；
4. 有接受候选则生成 `conclude` LeadDecision；
5. 没有则生成 `inconclusive`；
6. 记录 `CUSTOM` execution，说明这是服务器 authority projection。

这里不进行新的语义诊断。Critic verdict 才是 live 路径的最终诊断判断来源。

### 注入式 turn 路径

测试或兼容适配器可以返回 `LeadAdjudicationOutput`。校验器只允许它选择 Critic 接受的 ID；首个输出违规时可有一次无工具 correction。

### Candidate lifecycle audit

终态审计记录持久化候选数、权威 IDs、未发布候选及原因。`inconclusive` 只清空 `root_causes`，候选和 Assessment 仍保留。

## 10. 阶段 6：Result validation

`validate_v11_result` 校验整个持久化图：

- Task、Finding、Candidate、Assessment、LeadDecision 的引用；
- Run owner 和 `runtime_run_id`；
- exact seven checks；
- tool manifest 与调用记录；
- Evidence 状态和 scope；
- execution 状态；
- Run status 与 diagnostic status 组合；
- authoritative candidate projection。

live 路径不再用缺少上下文的模型调用修机械错误：

- 属于证据不足/终态发布条件不足的白名单错误，可由服务端降级为固定 `inconclusive` 并记录 degradation；
- 其他结构错误直接 fail-closed；
- 注入式 turn 路径仍可覆盖一次 bounded correction 合同。

Validator 只撤销发布或拒绝结构，不改写候选实体、分类、机制和证据。

## 11. 最终输出从哪里来

最终对外投影只把 authority phase 授权的 Candidate 变成 root causes/predictions。候选审计列表可以比发布列表更长。

```text
Critic verdict → LeadDecision candidate_ids → root_causes/predictions
```

数据库里有 Candidate，不代表系统已确认根因。

## 12. 阶段职责总表

| 阶段 | 模型判断 | 确定性代码 | 主要持久化对象 |
| --- | --- | --- | --- |
| Initial evidence | 无 | Provider 收集、脱敏、落库 | Evidence |
| Lead planning | 信息缺口与任务意图 | 验证、生成 ID、冻结能力 | Plan、Task、Execution |
| Investigator | 工具选择、发现、候选 | 权限、预算、准入、ID | ToolCall、Evidence、Finding、Candidate |
| Critic | 七项因果审查 | 引用白名单、任务归属 | Assessment、Task |
| Round 2 | 定向补证 | 限制轮次和 ownership | ToolCall、Evidence、Finding |
| Authority | live 无新增语义判断 | 投影 accepted refs | LeadDecision、audit |
| Validation | 无 | 全图机械校验 | status、degradation/failure |

## 13. 推荐源码阅读顺序

1. [phase_executor.py](../../backend/runtime/phase_executor.py)：初始证据怎样进入 V11；
2. [phases.py](../../backend/runtime/phases.py)：阶段顺序；
3. [v11_runtime.py](../../backend/diagnosis/v11_runtime.py)：搜索各阶段方法；
4. [adaptive_tools.py](../../backend/diagnosis/adaptive_tools.py)：Agent 工具会话；
5. [result_validation.py](../../backend/diagnosis/result_validation.py)：最终否决边界；
6. [test_v11_runtime.py](../../tests/diagnosis/test_v11_runtime.py)：用测试理解失败场景。
