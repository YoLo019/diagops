# DiagOps V4 中文多 Agent 平台执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development task-by-task. 每个任务都需要 worker 实现、reviewer 审查、测试通过后再进入下一步。

**Goal:** 在 V3 只读诊断平台上，增加更接近 OpenDerisk 的轻量多 Agent 能力，并将前端主要体验中文化。

**Spec:** `docs/superpowers/specs/2026-07-06-diagops-v4-cn-multi-agent-platform-design.md`

**原则:**

1. 保持只读，不执行 rollback、restart、scale、config change。
2. 不引入 CrewAI / LangGraph / OpenDerisk runtime 作为核心依赖。
3. 先用现有 FastAPI、Pydantic、SQLite、React/Vite。
4. 先做稳定协议和可视化，再考虑复杂执行引擎。
5. 每个任务必须有最小测试。

---

## Environment Rules

- Project root: `D:\agent\sre-agent`
- uv cache: `D:\agent\.uv-cache`
- npm cache: `D:\agent\.npm-cache`
- Node.js: `D:\paiflow\nodejs\node.exe`
- Backend: `http://127.0.0.1:8000`
- Frontend: `http://127.0.0.1:5173`

PowerShell 下使用 `npm.cmd`，不要直接用 `npm.ps1`。

---

## Task 1: Frontend Chinese Copy Pass

**Files:**

- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css` if needed
- Modify: `tests/frontend/test_frontend_smoke.py`

**Goal:** 将 V3 前端主要可见文案中文化，但保留枚举值和原始 payload。

- [ ] **Step 1: Update visible copy**

中文化：

1. `Manual Investigation` -> `新建诊断`
2. `Investigation List` -> `诊断列表`
3. `Investigation Detail` -> `诊断详情`
4. `Evidence List` -> `证据链`
5. `Hypotheses` -> `候选根因`
6. `Recommended Actions` -> `建议动作`
7. `Verification Suggestions` -> `验证建议`
8. `Markdown Report` -> `诊断报告`

保留：

1. provider value
2. cause_type value
3. raw error / payload text

- [ ] **Step 2: Safety wording**

确认 UI 只写“记录审批状态”“记录验证结果”，不写“执行回滚”“自动修复”。

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/frontend/test_frontend_smoke.py -v
npm.cmd run build
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add frontend/src/App.tsx frontend/src/styles.css tests/frontend/test_frontend_smoke.py
git commit -m "feat: localize v4 frontend copy"
```

---

## Task 2: Multi Agent Domain Models

**Files:**

- Create: `backend/domain/agent_plan.py`
- Create: `backend/domain/agent_context.py`
- Create: `backend/domain/tool_calls.py`
- Create: `backend/domain/memory.py`
- Test: `tests/domain/test_agent_models.py`

**Goal:** 增加 V4 协议模型，不接执行逻辑。

- [ ] **Step 1: Add models**

Create:

1. `DiagnosisPlan`
2. `DiagnosisTask`
3. `AgentExecution`
4. `SharedInvestigationContext`
5. `ContextFact`
6. `ToolSpec`
7. `ToolCallRecord`
8. `MemoryItem`

- [ ] **Step 2: Add validation**

Rules:

1. `confidence` must be finite and `0..1`.
2. `ContextFact.evidence_ids` required unless `fact_type == "missing_evidence"`.
3. `ToolSpec.read_only` defaults to `True`.
4. Task status values are explicit enums.

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/domain/test_agent_models.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add backend/domain/agent_plan.py backend/domain/agent_context.py backend/domain/tool_calls.py backend/domain/memory.py tests/domain/test_agent_models.py
git commit -m "feat: add v4 multi-agent domain models"
```

---

## Task 3: SQLite Persistence For Agent Protocol

**Files:**

- Modify: `backend/db/schema.py`
- Modify: `backend/db/serialization.py`
- Modify: `backend/db/sqlite_repository.py`
- Test: `tests/db/test_v4_agent_persistence.py`

**Goal:** 持久化 plan、tasks、executions、context facts、tool calls、memory items。

- [ ] **Step 1: Add tables**

Tables:

1. `diagnosis_plans`
2. `diagnosis_tasks`
3. `agent_executions`
4. `context_facts`
5. `tool_calls`
6. `memory_items`

Use JSON payload rows, plus indexes/columns for `investigation_id`, `service`, `environment`, `status`, `created_at` where needed.

- [ ] **Step 2: Add repository methods**

Minimal methods:

1. save/get plan
2. save/list tasks
3. save/list executions
4. save/list context facts
5. save/list tool calls
6. save/list memory by service/environment

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/db/test_v4_agent_persistence.py tests/db/test_sqlite_repository.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add backend/db/schema.py backend/db/serialization.py backend/db/sqlite_repository.py tests/db/test_v4_agent_persistence.py
git commit -m "feat: persist v4 agent protocol data"
```

---

## Task 4: Task Planner

**Files:**

- Create: `backend/diagnosis/planner.py`
- Test: `tests/diagnosis/test_task_planner.py`

**Goal:** 用规则生成诊断任务计划。

- [ ] **Step 1: Implement planner**

Rules:

1. All events create log, metric, deployment, service catalog tasks.
2. Text contains `timeout` or `dependency` -> dependency task.
3. Text contains `deploy`, `release`, `rollback` -> deployment task priority up.
4. Text contains `qps`, `traffic`, `spike` -> metric task priority up.

- [ ] **Step 2: Tests**

```powershell
uv run pytest tests/diagnosis/test_task_planner.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 3: Commit**

```powershell
git add backend/diagnosis/planner.py tests/diagnosis/test_task_planner.py
git commit -m "feat: add v4 diagnosis task planner"
```

---

## Task 5: Agent Router

**Files:**

- Create: `backend/diagnosis/router.py`
- Test: `tests/diagnosis/test_agent_router.py`

**Goal:** task_type -> agent_name/tool_names 的简单路由。

- [ ] **Step 1: Implement router**

Routes:

```text
log_investigation       -> LogAgent
metric_investigation    -> MetricAgent
deployment_check        -> DeploymentAgent
dependency_check        -> DependencyAgent
service_context         -> ServiceCatalogAgent
memory_lookup           -> MemoryAgent
rca_synthesis           -> RcaAgent
llm_review              -> LlmAnalystAgent
```

- [ ] **Step 2: Unknown fallback**

Unknown task_type routes to `ManualReviewAgent` and no tool execution.

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/diagnosis/test_agent_router.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add backend/diagnosis/router.py tests/diagnosis/test_agent_router.py
git commit -m "feat: add v4 agent router"
```

---

## Task 6: Tool Registry Around Existing Providers

**Files:**

- Create: `backend/tools/registry.py`
- Create: `backend/tools/provider_tools.py`
- Test: `tests/tools/test_tool_registry.py`

**Goal:** 将现有 provider 包装为只读 tool，不改 provider 行为。

- [ ] **Step 1: Add tool specs**

Tools:

1. `read_logs`
2. `query_metrics`
3. `read_deployments`
4. `read_service_catalog`
5. `query_prometheus`
6. `lookup_memory`

- [ ] **Step 2: Add invocation records**

Tool call produces `ToolCallRecord` with status, duration, evidence ids, error message.

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/tools/test_tool_registry.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add backend/tools/registry.py backend/tools/provider_tools.py tests/tools/test_tool_registry.py
git commit -m "feat: wrap providers as read-only tools"
```

---

## Task 7: Shared Context Builder

**Files:**

- Create: `backend/diagnosis/context_store.py`
- Test: `tests/diagnosis/test_shared_context.py`

**Goal:** 从 evidence、tool calls、agent notes 生成共享上下文事实。

- [ ] **Step 1: Implement context store**

Capabilities:

1. Add fact.
2. List facts by investigation.
3. Reject facts referencing unknown evidence ids.
4. Allow `missing_evidence` facts without evidence ids.

- [ ] **Step 2: Tests**

```powershell
uv run pytest tests/diagnosis/test_shared_context.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 3: Commit**

```powershell
git add backend/diagnosis/context_store.py tests/diagnosis/test_shared_context.py
git commit -m "feat: add shared investigation context"
```

---

## Task 8: Execution Engine

**Files:**

- Create: `backend/diagnosis/execution_engine.py`
- Modify: `backend/diagnosis/orchestrator.py`
- Test: `tests/diagnosis/test_execution_engine.py`

**Goal:** 执行 plan tasks，记录 agent execution 和 tool calls。

- [ ] **Step 1: Implement synchronous engine**

V4 first version uses synchronous execution. Keep optional parallel flag for later only if needed by tests.

Execution rules:

1. Run tasks with no dependencies first.
2. Skip tasks whose dependencies failed.
3. Record success/failure/skipped.
4. Provider/tool failure becomes failed task, not failed investigation.

- [ ] **Step 2: Wire orchestrator**

Existing evidence collection must still work.

Add plan/task/execution records around current provider collection, without changing RCA ranking behavior.

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/diagnosis/test_execution_engine.py tests/diagnosis/test_orchestrator.py -v
uv run pytest tests/golden/test_golden_cases.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add backend/diagnosis/execution_engine.py backend/diagnosis/orchestrator.py tests/diagnosis/test_execution_engine.py
git commit -m "feat: record v4 agent task execution"
```

---

## Task 9: Memory Store And Feedback

**Files:**

- Create: `backend/memory/store.py`
- Modify: `backend/api/investigations.py`
- Test: `tests/memory/test_memory_store.py`
- Test: `tests/api/test_v4_feedback_api.py`

**Goal:** 最小历史记忆和人工反馈。

- [ ] **Step 1: Memory lookup**

Lookup by:

1. service
2. environment
3. newest first

- [ ] **Step 2: Feedback API**

Add:

```text
POST /investigations/{id}/feedback
```

Feedback records:

```text
root_cause_correct
action_useful
verification_result
note
```

Feedback only records memory. It does not modify existing report.

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/memory/test_memory_store.py tests/api/test_v4_feedback_api.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add backend/memory/store.py backend/api/investigations.py tests/memory/test_memory_store.py tests/api/test_v4_feedback_api.py
git commit -m "feat: add v4 memory and feedback"
```

---

## Task 10: Visualization APIs

**Files:**

- Create: `backend/api/agent_views.py`
- Modify: `backend/main.py`
- Test: `tests/api/test_v4_agent_views_api.py`

**Goal:** 给前端稳定读取 Agent 过程的数据协议。

- [ ] **Step 1: Add endpoints**

```text
GET /investigations/{id}/plan
GET /investigations/{id}/tasks
GET /investigations/{id}/agent-executions
GET /investigations/{id}/context
GET /investigations/{id}/tool-calls
GET /investigations/{id}/memory
GET /investigations/{id}/task-graph
```

- [ ] **Step 2: Task graph shape**

Return:

```text
nodes[]
edges[]
```

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/api/test_v4_agent_views_api.py tests/api/test_v3_investigation_detail_api.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add backend/api/agent_views.py backend/main.py tests/api/test_v4_agent_views_api.py
git commit -m "feat: expose v4 agent visualization api"
```

---

## Task 11: Frontend Agent Process Panels

**Files:**

- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `tests/frontend/test_frontend_smoke.py`

**Goal:** 前端展示任务规划、Agent 执行、工具调用、共享上下文、记忆命中。

- [ ] **Step 1: Add API client methods**

Add:

1. getPlan
2. getTasks
3. getAgentExecutions
4. getContextFacts
5. getToolCalls
6. getMemoryHits
7. getTaskGraph

- [ ] **Step 2: Add panels**

Panels:

1. `任务规划`
2. `Agent 执行过程`
3. `工具调用`
4. `共享上下文`
5. `历史记忆`

Use lists and compact status badges. No graph library.

- [ ] **Step 3: Tests/build**

```powershell
uv run pytest tests/frontend/test_frontend_smoke.py -v
cd frontend
npm.cmd run build
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css tests/frontend/test_frontend_smoke.py
git commit -m "feat: show v4 agent process panels"
```

---

## Task 12: Chinese Report And API Labels

**Files:**

- Modify: `backend/reports/generator.py`
- Modify: `frontend/src/App.tsx`
- Test: `tests/reports/test_generator.py`
- Test: `tests/frontend/test_frontend_smoke.py`

**Goal:** 报告和前端主要显示继续中文化，同时保留证据字段。

- [ ] **Step 1: Report wording pass**

Ensure report has stable Chinese headings:

1. 摘要
2. 最可能根因
3. 证据链
4. 支持该结论的证据
5. 其他可能假设
6. 建议动作
7. 验证建议
8. 事实与推断

- [ ] **Step 2: Tests**

```powershell
uv run pytest tests/reports/test_generator.py tests/golden/test_golden_cases.py -v
uv run pytest tests/frontend/test_frontend_smoke.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 3: Commit**

```powershell
git add backend/reports/generator.py frontend/src/App.tsx tests/reports/test_generator.py tests/frontend/test_frontend_smoke.py
git commit -m "feat: refine chinese report and ui labels"
```

---

## Task 13: V4 Docs And Examples

**Files:**

- Modify: `README.md`
- Create: `docs/examples/v4-feedback.json`
- Create: `docs/examples/v4-agent-view.md`

**Goal:** 写清楚 V4 怎么看 Agent 过程和怎么记录反馈。

- [ ] **Step 1: README update**

Add:

1. V4 多 Agent 概览。
2. 中文前端说明。
3. Agent 过程 API。
4. Memory/feedback 说明。
5. 只读边界。

- [ ] **Step 2: Examples**

Add feedback example:

```json
{
  "root_cause_correct": true,
  "action_useful": true,
  "verification_result": "passed",
  "note": "rollback suggestion matched the actual incident review"
}
```

- [ ] **Step 3: Tests**

```powershell
uv run pytest tests/api/test_v4_agent_views_api.py tests/api/test_v4_feedback_api.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add README.md docs/examples/v4-feedback.json docs/examples/v4-agent-view.md
git commit -m "docs: document v4 multi-agent workflow"
```

---

## Task 14: Final Verification And Push

**Files:** no feature files.

- [ ] **Step 1: Environment check**

```powershell
uv cache dir
npm.cmd config get cache
(Get-Command node).Source
```

Expected:

```text
D:\agent\.uv-cache
D:\agent\.npm-cache
D:\paiflow\nodejs\node.exe
```

- [ ] **Step 2: Backend tests**

```powershell
uv run ruff check .
uv run pytest -v
```

Expected: PASS. Existing Starlette/TestClient warning is acceptable.

- [ ] **Step 3: Frontend build**

```powershell
cd frontend
npm.cmd run build
```

Expected: PASS. React Query `use client` build warning is acceptable if exit code is 0.

- [ ] **Step 4: Manual API smoke**

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/events/simulated/deployment_regression"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/investigations/<id>/task-graph"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/investigations/<id>/context"
```

Expected:

1. investigation completed.
2. task graph exists.
3. context facts exist.

- [ ] **Step 5: Frontend smoke**

Use browser or Playwright:

1. Open `http://127.0.0.1:5173`.
2. Confirm Chinese UI.
3. Create manual investigation.
4. Confirm Agent panels render.
5. Confirm console has no errors.

- [ ] **Step 6: Push**

```powershell
git push -u origin <branch>
```

---

## Completion Criteria

V4 is complete when:

1. 中文前端可用。
2. 每个 investigation 有 plan/tasks/executions/tool calls/context/memory views.
3. Agent route 和 tool call 记录可追踪。
4. RCA 仍 evidence-grounded。
5. Memory/feedback 可记录。
6. No auto-remediation language or behavior.
7. Backend tests pass.
8. Frontend build passes.
9. Branch pushed.
