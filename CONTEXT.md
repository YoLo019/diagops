# DiagOps 领域语言

## 术语

**Diagnostic authority**:
被允许发布 V11 诊断的已记录决策。它来自 Agent 推理链，确定性 RCA 不能提供或改写它。
_避免说成_：任意模型输出、伪装成 Agent 诊断的确定性 fallback

**Lead adjudication**:
把已审查候选转换为发布集合的终态权威阶段。这个词描述工作流职责，不保证该阶段一定额外调用一次 Lead 模型。
_避免说成_：第二次 Lead model turn、Lead 投票

**Evidence digest**:
对已提交证据做出的有界、确定性 prompt 投影。它只是上下文选择视图，永远不替代完整 Evidence ledger。
_避免说成_：完整证据集、模型自行选择的证据库

**Failure class**:
有证据支持、短而稳定的故障分类，用于比较和下游评分；它与面向人的 failure mechanism 解释分开。
_避免说成_：完整根因解释、自由文本机制段落
