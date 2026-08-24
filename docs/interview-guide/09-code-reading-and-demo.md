# 09. 代码阅读与面试演示路线

## 1. 不要从 4000 行 V11Runtime 开始

推荐按“入口 → 契约 → 主链路 → 可靠性 → 展示”阅读。每一步先回答一个问题。

## 2. 两小时源码路线

### 第 1 站：项目边界（10 分钟）

读：

- [README.md](../../README.md)
- [当前迭代状态](../superpowers/current.md)
- [AGENTS.md](../../AGENTS.md)
- [config/diagops.yaml](../../config/diagops.yaml)

回答：它做什么、不做什么、默认启用了什么、当前发布到哪里。

### 第 2 站：应用装配（15 分钟）

读：

- [backend/main.py](../../backend/main.py)
- [backend/services/container.py](../../backend/services/container.py)

回答：应用启动后创建了哪些组件，谁依赖谁，V10/V11 怎样选择。

### 第 3 站：领域语言（20 分钟）

读：

- [events.py](../../backend/domain/events.py)
- [evidence.py](../../backend/domain/evidence.py)
- [agent_plan.py](../../backend/domain/agent_plan.py)
- [agent_findings.py](../../backend/domain/agent_findings.py)
- [multi_agent.py](../../backend/domain/multi_agent.py)
- [runtime.py](../../backend/domain/runtime.py)

回答：Incident、Evidence、Task、Finding、Candidate、Review、Run、Attempt、Event、Checkpoint 分别是什么。

### 第 4 站：工具调用（20 分钟）

读：

- [tool_queries.py](../../backend/domain/tool_queries.py)
- [tool_calls.py](../../backend/domain/tool_calls.py)
- [tools/registry.py](../../backend/tools/registry.py)
- [tools/provider_tools.py](../../backend/tools/provider_tools.py)
- [diagnosis/adaptive_tools.py](../../backend/diagnosis/adaptive_tools.py)
- [providers/registry.py](../../backend/providers/registry.py)

回答：九工具是什么，一次调用经过哪些校验，幂等和 Evidence 怎样形成。

### 第 5 站：Agent 拓扑（25 分钟）

在 [v11_runtime.py](../../backend/diagnosis/v11_runtime.py) 按函数跳转，不要顺序全文读：

1. `run_phase` / `_dispatch_phase`；
2. `plan_lead` / `_build_plan`；
3. `investigator_round_1` / `_run_investigator`；
4. `critic_review`；
5. `investigator_round_2`；
6. `critic_reconciliation`；
7. `lead_adjudication`；
8. `result_validation`；
9. `_call_model`。

回答：各角色看什么、能调用什么、输出什么、失败怎样处理。

### 第 6 站：Runtime（20 分钟）

读：

- [runtime/phases.py](../../backend/runtime/phases.py)
- [runtime/manager.py](../../backend/runtime/manager.py)
- [runtime/coordinator.py](../../backend/runtime/coordinator.py)
- [runtime/writer.py](../../backend/runtime/writer.py)
- [runtime/event_hub.py](../../backend/runtime/event_hub.py)

回答：阶段怎样提交，lease、取消、恢复、SSE 和单 Writer 怎样工作。

### 第 7 站：对外结果（10 分钟）

读：

- [diagnosis/result_validation.py](../../backend/diagnosis/result_validation.py)
- [reports/generator.py](../../backend/reports/generator.py)
- [api/agent_views.py](../../backend/api/agent_views.py)
- [frontend/src/RuntimeWorkbench.tsx](../../frontend/src/RuntimeWorkbench.tsx)

回答：系统怎样从内部对象变成安全报告和页面。

## 3. 本地启动

安装依赖：

```powershell
uv sync
npm.cmd --prefix frontend install
```

启动后端：

```powershell
uv run uvicorn backend.main:app --reload
```

启动前端：

```powershell
npm.cmd --prefix frontend run dev
```

健康检查：

```powershell
curl.exe http://127.0.0.1:8000/health
```

默认 Agent 关闭，不需要 API key。不要为了演示把 key 写入 YAML、代码或命令历史。

## 4. 最安全的面试演示

### 演示 A：模拟事故

```powershell
curl.exe -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
```

然后展示：

- Investigation 列表和详情；
- Evidence 时间线；
- 旧/默认路径的候选和报告；
- Action 与 Verification 只是记录，没有自动执行。

说明：默认配置下这是确定性/兼容路径演示，不要说成正式 V11 多 Agent live run。

### 演示 B：Runtime 工作台

进入 Runtime Workbench，展示：

- 同一 Investigation 的 Run；
- phase timeline；
- Runtime Events；
- Checkpoints；
- cancel/resume/replay/diff 按状态启用或禁用。

### 演示 C：九工具契约

直接打开源码说明：

- manifest 只取 read-only + Agent exposure；
- query schema 限制两小时时间窗和实体范围；
- `query_prometheus` 是内部别名；
- Tool result 先持久化再能引用。

这比临时配置付费模型更稳定，也更能体现你的理解。

## 5. 如果要演示 V11

只有在你已有合规本地配置和通过的 capability artifact 时才做。必须：

- Agent enabled；
- 明确 Provider/model；
- key 只放当前进程环境；
- 对 generic compatible endpoint 使用能力认证；
- 说明本次是工程演示还是正式 gate；
- 展示 Lead/Investigator/Critic 和 Evidence 引用，而不只展示最终报告。

不要在面试现场首次尝试付费 live gate；网络、额度、模型输出和 timeout 都会让演示变得不可控。

## 6. 一条事故怎样手工追代码

以手工 Investigation 为例：

```text
POST /investigations/manual
  → backend/api/investigations.py
  → AppContainer.run_investigation
  → 创建 Investigation / RuntimeRun
  → RuntimeManager.start
  → RuntimeCoordinator.execute
  → DiagnosisPhaseExecutor.execute_phase
  → V11Runtime.run_phase（V11）
  → AdaptiveToolSession / ToolRegistry / ProviderRegistry
  → RuntimeWriter / SQLiteRuntimeStore
  → ReportGenerator
  → GET APIs / React Query / UI
```

使用 `rg` 快速定位：

```powershell
rg -n "def run_investigation|def create_runtime_run" backend
rg -n "V11_PHASE_ORDER|async def plan_lead|async def critic_review" backend
rg -n "build_provider_tool_registry|agent_manifest|tools_for" backend
rg -n "commit_phase|RuntimeCheckpoint|replay" backend/runtime backend/domain
```

## 7. 面试演示叙事顺序

推荐 8～10 分钟：

1. **问题**：事故调查跨多数据源，人工慢且有确认偏差；
2. **边界**：系统只读，不自动修复；
3. **架构**：Lead → isolated Investigators → Critic → Lead → Validator；
4. **工具**：冻结九工具，严格 schema、scope、budget、Evidence ledger；
5. **可靠性**：versioned phases、single writer、checkpoint、resume/replay；
6. **页面**：展示 Evidence、Agent 过程和 Runtime timeline；
7. **验证**：分层测试、key-free acceptance、受控 Benchmark；
8. **诚实状态**：正式 V11 效果 gate 尚未完成。

## 8. 面试前自测清单

- 我能不看稿解释 Tool 和 Provider 的区别；
- 我能画出 V11 角色和阶段；
- 我能解释为什么 Validator 不能生成根因；
- 我能解释 Resume、Replay、Rerun、Diff；
- 我能说出至少五个安全边界；
- 我知道默认 Agent 是关闭的；
- 我不会声称自动修复；
- 我不会声称正式准确率已经提升；
- 我能指出一个真实局限和下一步工作；
- 我能在源码中快速定位上述设计。

## 9. 推荐重点测试

理解主链路后，可运行不涉及付费模型的聚焦测试：

```powershell
uv run pytest tests/tools/test_tool_registry.py -q
uv run pytest tests/diagnosis/test_result_validation.py -q
uv run pytest tests/runtime/test_replay.py -q
uv run pytest tests/runtime/test_tool_idempotence.py -q
uv run pytest tests/safety/test_redaction.py -q
```

这些测试分别对应工具白名单、结果引用、重放真实性、恢复幂等和脱敏，是面试最有代表性的工程保障。
