# 26. Agent 开发实战实验：从一个只读工具到可恢复 Multi-Agent

本章把前面概念变成可操作的学习路线。实验默认使用本地 mock/file provider 和 `memory://` 测试仓库，不需要真实 API key，也不执行生产写操作。代码片段是教学用最小实现；凡是与真实 V11 不同的地方都会标明，不能把片段直接当成生产安全边界。

## 0. 准备环境

在 PowerShell 中：

```powershell
Set-Location D:\agent\sre-agent
uv sync
uv run pytest -q
```

建议先阅读：

```text
backend/domain/evidence.py
backend/domain/tool_queries.py
backend/domain/tool_calls.py
backend/tools/registry.py
backend/diagnosis/adaptive_tools.py
backend/diagnosis/v11_runtime.py
backend/runtime/phase_executor.py
```

如果只想跑 key-free 的核心验收：

```powershell
uv run python -m backend.services.offline_tool_acceptance
uv run python -m backend.services.runtime_acceptance
```

## 实验 A：定义一个严格的只读工具

### 目标

理解 `ToolSpec → QueryModel → handler → ToolCallRecord/Evidence`，并学会拒绝额外字段。

### 教学版步骤

1. 在临时测试中定义 `ReadHealthQuery`，字段只包含 `entity_id` 和有界时间窗；
2. 使用 Pydantic `ConfigDict(extra="forbid")`；
3. 注册 `ToolSpec(read_only=True, exposure=AGENT)`；
4. handler 返回 `ToolInvocationResult`，不要返回任意字符串；
5. 断言额外字段、未知实体和过长窗口不会触发 provider。

教学版最小 schema：

```python
class ReadHealthQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    window_minutes: int = Field(default=10, ge=1, le=120)
```

### 对照真实代码

真实 V11 不让模型直接传所有公共字段，而是用 `domain/tool_queries.py` 的 QueryModel 和 `adaptive_tools._server_query_defaults` 模块函数注入 incident window；Registry 和 Provider 的职责也不能合并。

### 推荐测试

```powershell
uv run pytest tests/tools/test_tool_registry.py tests/domain/test_tool_queries.py -q
```

### 面试讲法

“工具 schema 只是模型提示；服务端再次用 Pydantic、scope 和权限校验，并把调用和结果写入审计。”

## 实验 B：实现 fingerprint、幂等和 error-as-result

### 目标

观察同一查询重复、Provider 超时和恢复时的行为。

### 步骤

1. 给查询做规范化（排序列表、统一时区、去掉 `reason`）；
2. 计算 `query_fingerprint` 和 `idempotency_key`；
3. 第一次调用先写 `running`；
4. 模拟 provider 返回 success，持久化 Evidence；
5. 在“返回模型前”抛出进程故障；
6. 重新执行同一逻辑步骤，断言复用 durable success，不再调用 provider；
7. 模拟裸 `TimeoutError`，断言记录为 timeout 且没有盲目重试；
8. 模拟明确 `transport`，断言最多一次 retry 且仍使用同一逻辑预算。

真实测试：

```powershell
uv run pytest tests/runtime/test_tool_idempotence.py tests/diagnosis/test_adaptive_tools.py -q
```

### 预期审计轨迹

```text
tool.started (logical_call_id=L, attempt=1)
tool.completed (success, evidence=[E1])
-- crash before model receives response --
resume: exact idempotency-key hit → no provider I/O
```

### 面试讲法

“网络 attempt 是执行细节，logical call 是业务动作；重试和 resume 必须沿用后者，才能既不重复副作用又不免费获得额度。”

## 实验 C：做 Evidence digest 和压缩回归

### 目标

理解确定性抽取和真正 LLM 摘要的区别，并验证不会丢掉区分机制的信号。

### 步骤

1. 构造同一服务的 cpu、latency、socket、error 四类指标，另加日志和 Trace；
2. 为指标设置不同 `change_score` 和 `scope.entity_ids`；
3. 调用 `V11Runtime` 模块中的 `_select_evidence_digest(max_total=4)`；
4. 记录选择顺序、kind、entity、signal family；
5. 增加一个高分 CPU 条目，确认不会永久挤掉 socket/log；
6. 构造跨实体候选，确认 scope 校验拒绝；
7. 统计 token saving、关键 ID recall 和反证保留率。

推荐阅读/测试：

```powershell
rg -n "_select_evidence_digest|_critic_evidence" backend/diagnosis/v11_runtime.py
uv run pytest tests/diagnosis/test_v11_runtime.py tests/runtime/test_v11_isolation_red.py -q
```

### 预期结论

digest 是原记录的有界、确定性视图；完整 Evidence ledger 不变。任何“摘要器自动补出 database root cause”的实现都应被判为越权。

## 实验 D：用结构化输出搭一条 Lead→Investigator→Critic 链

### 目标

在不接真实模型的情况下理解 Multi-Agent 的阶段边界、隔离和服务器拥有字段。

### 步骤

1. 为 Lead 提供事故和工具 manifest，返回 1～3 个信息缺口 task draft；
2. 服务端生成 Task ID、owner、round 和 scope；
3. 并行启动三个 Investigator，每个只接收自己的 task/digest；
4. 让一个 Investigator 产出 Candidate，另一个产出 Gap，第三个故意失败；
5. 服务端准入 Candidate 并生成 ID；
6. Critic 对每个候选生成七项 checks（pass/fail 必须引用 Evidence，unknown 必须给 gap）；
7. 对每个 `needs_evidence` assessment 创建一个 ownership 正确的 Round 2 task；整批最多 3 个任务；
8. Lead adjudication 只投影 accepted candidate refs，Validator 不发明新根因。

真实测试入口：

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py tests/diagnosis/test_result_validation.py -q
```

### 可观察的隔离点

```text
I1 context ≠ I2 context ≠ I3 context
I1 的 finding 不会在 Round 1 注入 I2
Critic 在所有 Round 1 结果提交后才启动
```

### 面试讲法

“这是有状态的阶段工作流，语义由多个角色模型完成，状态/预算/权限由 Runtime 控制；不是三个模型自由聊天。”

## 实验 E：注入故障验证 durable Runtime

### 目标

亲自观察 deadline、取消、lease、checkpoint、resume、replay 和 SSE catch-up。

### 步骤

1. 阅读 `tests/runtime/test_fault_injection.py` 和 `test_checkpoint_integrity.py`；
2. 在本地 acceptance 中注入 provider/model timeout、writer failure、checkpoint tamper；
3. 断言 pending Tool/Model/Attempt 被终态化；
4. 断言 `interrupted` 可以新 Attempt 恢复，`cancelled/failed` 不能偷偷继续；
5. 断言恢复继承剩余 token/tool budget，成功工具键只复用不重打；
6. 订阅 SSE，制造队列溢出，再从最后 sequence catch-up；
7. 对同一 Run 执行 replay，确认不会调用 live model/provider。

```powershell
uv run pytest tests/runtime/test_fault_injection.py tests/runtime/test_replay.py tests/api/test_runtime_sse.py -q
```

### 预期状态链

```text
created → running → interrupted → running(new Attempt) → completed
created → running → cancelling → cancelled
```

## 实验 F：比较 verified memory 与“未验证 RAG”

### 目标

理解为什么当前项目拒绝自动把反馈变成知识。

### 步骤

1. 保存一条 `MemoryItem(verification_status=unverified)`，确认 `lookup_memory` 不返回；
2. 补齐 source investigation、root candidate、verified_at、verified_by 和 verification evidence；
3. 让 verified_at 晚于当前事故开始时间，确认被拒绝；
4. 让 source investigation 属于当前 Investigation 或其 `source_investigation_id` 祖先，确认被排除；
5. 通过 guard 后观察新 `VERIFIED_INCIDENT` Evidence 的 provenance；
6. 再用一个教育版向量检索器做 shadow（不改变产品结论），比较 ACL leakage、staleness 和 citation faithfulness。

```powershell
uv run pytest tests/tools/test_verified_memory_lookup.py tests/memory/test_memory_store.py -q
```

## 实验 G：模型能力认证与安全降级

### 目标

区分“endpoint 能聊天”和“endpoint 满足 V11 合同”。

### 步骤

1. 阅读 `backend/services/model_capability.py` 的 capability manifest；
2. 用测试中的 fake endpoint 观察 non-streaming、tool calls、strict schema、usage、并发和 local canary 探针；
3. 改变 adapter/SDK/source hash，确认旧 artifact 不再 admission；
4. 删除 passed artifact，确认 compatible V11 Run 在模型调用前 fail-closed；
5. 不要在实验中粘贴真实 key；live certification 只从进程环境读取，并且需要用户明确授权。

```powershell
uv run pytest tests/services/test_model_capability.py tests/diagnosis/test_openai_compatible_model.py -q
```

## 实验 H：构造一组 Agent 评测与对抗测试

### 目标

把“模型回答看起来不错”变成可重复的质量/安全门。

### 步骤

1. 选相同 incident package，分别跑 single 和 multi；
2. 固定 model/provider/prompt/tool/Skill/memory/source hash，并分别冻结每种配置自己的 topology/output schema identity；
3. 记录 exact/component/mechanism/top3、completion、reference integrity、tokens、latency，以及在明确价格路径存在时的 cost；
4. 加入日志间接注入、非法 tool、跨 scope、未来 memory、超长 payload；
5. 检查没有 read-only/leakage violation，失败类别和审计轨迹正确；
6. 用 paired bootstrap/McNemar 比较，而不是只看一次 demo；
7. 在 labels 打开前导出 Evidence audit，避免评测污染。

```powershell
uv run pytest tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_audit.py tests/benchmarks/test_rcaeval_evaluator.py -q
```

## 3. 实验报告模板

每个实验写一页即可，但必须包括：

```text
问题与假设
输入/固定身份（model、contract、manifest、seed）
实际事件序列与关键对象 ID
成功与故障注入
质量/成本/延迟/安全指标
观察到的根因与未解决缺口
如果上线还缺什么 gate
面试用 30 秒解释
```

不要只截图最终报告；Agent 岗位面试更看重你能否解释失败、边界和可验证性。

## 4. 学习顺序建议

```text
A 严格 Tool → B 幂等/重试 → C Context digest
→ D Multi 阶段 → E Durable Runtime → F Memory
→ G Capability → H Evaluation/Security
```

完成 A～D 后就能讲清 Agent 核心链路；完成 E～H 才接近生产 Agent 工程师的完整能力。

## 5. 现状标签

**Implemented**：仓库已有 key-free tool、runtime、V11 canary、memory、capability 和 benchmark 测试，可按上述路径阅读和运行。

**Partial**：实验 H 的正式 SS15/TT90 需要独立数据包、能力工件和授权；本地 fixture 只能用于开发 smoke test。

**Not implemented**：本章的向量 RAG shadow、通用 compaction、MCP adapter、跨节点压测和在线 dashboard 是学习/演进练习，不会被假装成当前产品功能。
