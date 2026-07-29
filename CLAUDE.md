# DiagOps — Claude Code 入口

本仓库的稳定规则维护在 `AGENT.md`（与 Codex 共用同一份事实来源），
必须完整遵循其中的安全边界、契约、工作流与测试评审要求：

@AGENT.md

## 工作流优先级（Claude 侧）

本仓库的迭代工作流以项目级 skill `iteration-flow`（`.claude/skills/iteration-flow/`）
为准。它自包含 Context Triage、Grill、Brainstorm、Spec、Plan、Execute、Review、
Verify 全部阶段，并按 Light / Standard / Full 分级裁剪成本。

- **不要**重复触发 superpowers 的流程类 skill（`brainstorming`、`writing-plans`、
  `executing-plans`、`subagent-driven-development`、`requesting-code-review`），
  除非用户显式要求。`iteration-flow` 各阶段已包含这些纪律且门禁不重复。
- superpowers 的**垂直纪律类** skill 与 `iteration-flow` 不冲突，照常使用：
  - `superpowers:systematic-debugging` —— 任何 bug、测试失败、异常行为先走它；
  - `superpowers:test-driven-development` —— 实现任务遵循红绿循环；
  - `superpowers:verification-before-completion` —— 结项证据标准。

## 对 iteration-flow 的三点补充（借自 superpowers 6.1.1）

不改变 `iteration-flow` 的分级与门禁，仅在其对应阶段内增强：

1. **Plan 阶段**：Full 级计划在文档头部写 `Global Constraints`（从 spec
   逐字拷贝的全局约束），每个任务块写 `Interfaces`（Consumes / Produces，
   精确到签名），使只看到单个任务的执行者也能对齐命名与类型。
2. **Execute 阶段**：多任务执行时维护进度 ledger 文件（仓库已有状态文件优先，
   否则用 `.superpowers/sdd/progress.md`），每完成一个任务追加一行
   （任务号、commit 范围、评审结论）。上下文压缩或会话恢复后，以 ledger 和
   `git log` 为准，不得凭记忆重复执行已完成任务。
3. **Verify 阶段**：遵循 Iron Law —— 未在本次工作中重新运行的检查不得声称
   通过；跳过的检查必须说明原因。这与 `iteration-flow` Phase 8 的追溯表对账
   同时执行，不互相替代。
