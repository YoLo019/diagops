# DiagOps V6 Single-Agent ReAct Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, read-only single-agent ReAct loop with persisted trace and frontend visibility.

**Architecture:** Keep the deterministic V1-V5 RCA pipeline as source of truth. Add a small `ReActInvestigationAgent` that uses existing read-only provider tools, records a `ReActTrace`, and never blocks investigation completion when disabled or failed.

**Tech Stack:** Python, Pydantic, FastAPI, SQLite/SQLAlchemy Core, pytest, React/Vite/TypeScript.

---

## File Map

- Create: `backend/domain/react_trace.py` for `ReActTrace`, `ReActTraceStep`, and enums.
- Create: `backend/diagnosis/react_agent.py` for the minimal ReAct loop and fake-testable LLM protocol.
- Modify: `backend/db/schema.py` to add `react_traces`.
- Modify: `backend/db/repositories.py` to save/get traces in memory.
- Modify: `backend/db/sqlite_repository.py` to save/get traces in SQLite.
- Modify: `backend/diagnosis/orchestrator.py` to optionally run the ReAct agent after evidence collection.
- Modify: `backend/api/agent_views.py` to expose `GET /investigations/{id}/react-trace`.
- Modify: `backend/config/settings.py` to add disabled-by-default ReAct settings.
- Modify: `frontend/src/api.ts` to add trace types and API client.
- Modify: `frontend/src/App.tsx` to add a ReAct trace panel.
- Modify: `frontend/src/styles.css` only if needed for existing compact list styles.
- Modify: `README.md` to document V6 boundaries and API.
- Test: `tests/domain/test_react_trace.py`.
- Test: `tests/db/test_v6_react_trace_persistence.py`.
- Test: `tests/diagnosis/test_react_agent.py`.
- Test: `tests/diagnosis/test_orchestrator.py`.
- Test: `tests/api/test_v6_react_trace_api.py`.
- Test: `tests/frontend/test_frontend_smoke.py`.

No commits are required unless the user explicitly asks.

---

### Task 1: ReAct Trace Domain Model

**Files:**
- Create: `backend/domain/react_trace.py`
- Test: `tests/domain/test_react_trace.py`

- [ ] **Step 1: Write failing domain tests**

Add `tests/domain/test_react_trace.py`:

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.domain.react_trace import (
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)


def test_react_trace_step_rejects_unknown_tool():
    with pytest.raises(ValidationError):
        ReActTraceStep(
            step_number=1,
            tool_name="bash",
            tool_input={"investigation_id": "inv-1"},
            status=ReActTraceStepStatus.FAILED,
            started_at=datetime.now(UTC),
        )


def test_react_trace_step_requires_positive_step_number():
    with pytest.raises(ValidationError):
        ReActTraceStep(step_number=0, status=ReActTraceStepStatus.THINKING)


def test_react_trace_orders_steps_and_defaults_to_json_safe_fields():
    later = ReActTraceStep(step_number=2, status=ReActTraceStepStatus.COMPLETED)
    first = ReActTraceStep(
        step_number=1,
        assistant_text="Need deployment evidence.",
        tool_name="read_deployments",
        tool_input={"investigation_id": "inv-1"},
        output_evidence_ids=["ev-deploy"],
        status=ReActTraceStepStatus.OBSERVED,
    )

    trace = ReActTrace(
        investigation_id="inv-1",
        status=ReActTraceStatus.COMPLETED,
        final_answer="Deployment evidence supports the top hypothesis.",
        steps=[later, first],
    )

    assert [step.step_number for step in trace.steps] == [1, 2]
    assert trace.model_dump(mode="json")["steps"][0]["tool_name"] == "read_deployments"
```

- [ ] **Step 2: Run RED check**

Run:

```bash
uv run pytest tests/domain/test_react_trace.py -v
```

Expected: FAIL because `backend.domain.react_trace` does not exist.

- [ ] **Step 3: Implement minimal domain models**

Create `backend/domain/react_trace.py`:

```python
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from backend.domain.evidence import JsonValue

READ_ONLY_REACT_TOOLS = frozenset(
    {
        "read_logs",
        "query_metrics",
        "read_deployments",
        "query_dependencies",
        "read_service_catalog",
        "lookup_memory",
    }
)


class ReActTraceStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_STEPS = "max_steps"
    DISABLED = "disabled"


class ReActTraceStepStatus(StrEnum):
    THINKING = "thinking"
    TOOL_CALLED = "tool_called"
    OBSERVED = "observed"
    COMPLETED = "completed"
    FAILED = "failed"


class ReActTraceStep(BaseModel):
    step_number: int = Field(ge=1)
    assistant_text: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, JsonValue] = Field(default_factory=dict)
    tool_call_id: str | None = None
    observation: str | None = None
    output_evidence_ids: list[str] = Field(default_factory=list)
    status: ReActTraceStepStatus = ReActTraceStepStatus.THINKING
    error_message: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @field_validator("tool_name")
    @classmethod
    def tool_must_be_read_only(cls, value: str | None) -> str | None:
        if value is not None and value not in READ_ONLY_REACT_TOOLS:
            raise ValueError(f"unsupported ReAct tool: {value}")
        return value


class ReActTrace(BaseModel):
    id: str = Field(default_factory=lambda: f"react-{uuid4().hex}")
    investigation_id: str
    status: ReActTraceStatus = ReActTraceStatus.RUNNING
    final_answer: str | None = None
    steps: list[ReActTraceStep] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @field_validator("steps")
    @classmethod
    def order_steps(cls, value: list[ReActTraceStep]) -> list[ReActTraceStep]:
        return sorted(value, key=lambda step: step.step_number)
```

- [ ] **Step 4: Run GREEN check**

Run:

```bash
uv run pytest tests/domain/test_react_trace.py -v
```

Expected: PASS.

---

### Task 2: ReAct Trace Persistence

**Files:**
- Modify: `backend/db/schema.py`
- Modify: `backend/db/repositories.py`
- Modify: `backend/db/sqlite_repository.py`
- Test: `tests/db/test_v6_react_trace_persistence.py`

- [ ] **Step 1: Write failing persistence tests**

Add `tests/db/test_v6_react_trace_persistence.py`:

```python
from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.react_trace import ReActTrace, ReActTraceStatus, ReActTraceStep


def _trace(answer: str) -> ReActTrace:
    return ReActTrace(
        investigation_id="inv-1",
        status=ReActTraceStatus.COMPLETED,
        final_answer=answer,
        steps=[ReActTraceStep(step_number=1, tool_name="read_logs")],
    )


def test_in_memory_repository_saves_and_replaces_react_trace():
    repository = InMemoryInvestigationRepository()

    first = repository.save_react_trace(_trace("first"))
    second = repository.save_react_trace(_trace("second"))

    assert first.final_answer == "first"
    assert repository.get_react_trace("inv-1") == second
    assert repository.get_react_trace("missing") is None


def test_sqlite_repository_saves_and_replaces_react_trace(tmp_path):
    repository = SQLiteInvestigationRepository(f"sqlite:///{tmp_path / 'diagops.db'}")

    repository.save_react_trace(_trace("first"))
    second = repository.save_react_trace(_trace("second"))

    assert repository.get_react_trace("inv-1") == second
    assert repository.get_react_trace("missing") is None
```

- [ ] **Step 2: Run RED check**

Run:

```bash
uv run pytest tests/db/test_v6_react_trace_persistence.py -v
```

Expected: FAIL because repository methods and SQLite table do not exist.

- [ ] **Step 3: Add table and repository methods**

In `backend/db/schema.py`, add a `react_traces` table following the existing JSON payload table pattern:

```python
react_traces = Table(
    "react_traces",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
```

In `backend/db/repositories.py`, add:

```python
self._react_traces: dict[str, ReActTrace] = {}

def save_react_trace(self, trace: ReActTrace) -> ReActTrace:
    self._react_traces[trace.investigation_id] = trace
    return trace

def get_react_trace(self, investigation_id: str) -> ReActTrace | None:
    return self._react_traces.get(investigation_id)
```

In `backend/db/sqlite_repository.py`, add imports and methods:

```python
def save_react_trace(self, trace: ReActTrace) -> ReActTrace:
    row = {
        "id": trace.id,
        "investigation_id": trace.investigation_id,
        "payload": trace.model_dump(mode="json"),
        "created_at": trace.created_at,
    }
    with self._engine.begin() as connection:
        connection.execute(
            delete(react_traces).where(
                react_traces.c.investigation_id == trace.investigation_id
            )
        )
        connection.execute(insert(react_traces).values(row))
    return trace

def get_react_trace(self, investigation_id: str) -> ReActTrace | None:
    with self._engine.begin() as connection:
        row = connection.execute(
            select(react_traces).where(react_traces.c.investigation_id == investigation_id)
        ).mappings().first()
    return None if row is None else ReActTrace.model_validate(row["payload"])
```

Use existing helper methods if the file already has matching JSON row helpers.

- [ ] **Step 4: Run GREEN check**

Run:

```bash
uv run pytest tests/db/test_v6_react_trace_persistence.py -v
```

Expected: PASS.

---

### Task 3: Minimal ReAct Runtime

**Files:**
- Create: `backend/diagnosis/react_agent.py`
- Test: `tests/diagnosis/test_react_agent.py`

- [ ] **Step 1: Write failing runtime tests**

Add `tests/diagnosis/test_react_agent.py` with a fake LLM and fake tool registry:

```python
from backend.diagnosis.react_agent import (
    LlmToolCall,
    ReActInvestigationAgent,
    ReActLlmResponse,
)
from backend.domain.events import IncidentEvent, IncidentSeverity, IncidentSource
from backend.domain.react_trace import ReActTraceStatus
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.tools.registry import ToolRegistry


class FakeLlm:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def generate(self, messages, tools):
        self.messages.append(messages)
        return self.responses.pop(0)


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout-service",
        environment="prod",
        severity=IncidentSeverity.HIGH,
        title="500 spike",
        description="checkout has 500s",
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="read_logs", description="Read logs.", read_only=True),
        lambda **kwargs: ToolCallRecord(
            task_id=kwargs["task_id"],
            agent_name=kwargs["agent_name"],
            tool_name=kwargs["tool_name"],
            input=kwargs["input"],
            status=ToolCallStatus.SUCCESS,
            output_evidence_ids=["ev-log"],
        ),
    )
    return registry


def test_react_agent_executes_tool_then_records_final_answer():
    llm = FakeLlm(
        [
            ReActLlmResponse(
                content="Need logs.",
                tool_call=LlmToolCall(name="read_logs", arguments={"extra": "ignored"}),
            ),
            ReActLlmResponse(content="Final answer cites ev-log."),
        ]
    )

    trace = ReActInvestigationAgent(llm=llm, tool_registry=_registry()).run(
        investigation_id="inv-1",
        event=_event(),
        existing_evidence_ids=[],
    )

    assert trace.status == ReActTraceStatus.COMPLETED
    assert trace.final_answer == "Final answer cites ev-log."
    assert trace.steps[0].tool_name == "read_logs"
    assert trace.steps[0].output_evidence_ids == ["ev-log"]
    assert "ev-log" in str(llm.messages[1])


def test_react_agent_fails_closed_for_unknown_tool():
    llm = FakeLlm(
        [ReActLlmResponse(tool_call=LlmToolCall(name="bash", arguments={}))]
    )

    trace = ReActInvestigationAgent(llm=llm, tool_registry=_registry()).run(
        investigation_id="inv-1",
        event=_event(),
        existing_evidence_ids=[],
    )

    assert trace.status == ReActTraceStatus.FAILED
    assert trace.steps[0].status == "failed"
    assert "unsupported" in (trace.steps[0].error_message or "")
```

- [ ] **Step 2: Run RED check**

Run:

```bash
uv run pytest tests/diagnosis/test_react_agent.py -v
```

Expected: FAIL because `backend.diagnosis.react_agent` does not exist.

- [ ] **Step 3: Implement minimal runtime**

Create `backend/diagnosis/react_agent.py`:

```python
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.domain.events import IncidentEvent
from backend.domain.react_trace import (
    READ_ONLY_REACT_TOOLS,
    ReActTrace,
    ReActTraceStatus,
    ReActTraceStep,
    ReActTraceStepStatus,
)
from backend.domain.tool_calls import ToolCallStatus, ToolSpec
from backend.tools.registry import ToolRegistry


@dataclass(frozen=True)
class LlmToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class ReActLlmResponse:
    content: str = ""
    tool_call: LlmToolCall | None = None


class ReActLlm(Protocol):
    def generate(self, messages: list[dict], tools: list[dict]) -> ReActLlmResponse:
        ...


class ReActInvestigationAgent:
    def __init__(self, *, llm: ReActLlm, tool_registry: ToolRegistry, max_steps: int = 5):
        self.llm = llm
        self.tool_registry = tool_registry
        self.max_steps = max_steps

    def run(
        self,
        *,
        investigation_id: str,
        event: IncidentEvent,
        existing_evidence_ids: list[str],
    ) -> ReActTrace:
        trace = ReActTrace(investigation_id=investigation_id)
        messages = [_system_message(event), _user_message(event, existing_evidence_ids)]
        tools = [_tool_schema(spec) for spec in self.tool_registry.list_specs() if spec.name in READ_ONLY_REACT_TOOLS]

        for step_number in range(1, self.max_steps + 1):
            response = self.llm.generate(messages, tools)
            if response.tool_call is None:
                trace.final_answer = response.content
                trace.status = ReActTraceStatus.COMPLETED
                trace.completed_at = datetime.now(UTC)
                return trace

            step = self._run_tool_step(investigation_id, event, step_number, response)
            trace.steps.append(step)
            messages.append({"role": "assistant", "content": response.content, "tool_call": response.tool_call.name})
            messages.append({"role": "tool", "content": step.observation or "", "tool_call_id": step.tool_call_id})
            if step.status == ReActTraceStepStatus.FAILED:
                trace.status = ReActTraceStatus.FAILED
                trace.completed_at = datetime.now(UTC)
                return trace

        trace.status = ReActTraceStatus.MAX_STEPS
        trace.completed_at = datetime.now(UTC)
        return trace

    def _run_tool_step(
        self,
        investigation_id: str,
        event: IncidentEvent,
        step_number: int,
        response: ReActLlmResponse,
    ) -> ReActTraceStep:
        call = response.tool_call
        if call is None or call.name not in READ_ONLY_REACT_TOOLS:
            return ReActTraceStep(
                step_number=step_number,
                assistant_text=response.content,
                status=ReActTraceStepStatus.FAILED,
                error_message=f"unsupported ReAct tool: {None if call is None else call.name}",
                completed_at=datetime.now(UTC),
            )
        try:
            record = self.tool_registry.invoke(
                call.name,
                event=event,
                task_id=f"react-{investigation_id}-{step_number}",
                agent_name="ReActInvestigationAgent",
                input={"investigation_id": investigation_id},
            )
        except Exception as exc:
            return ReActTraceStep(
                step_number=step_number,
                assistant_text=response.content,
                tool_name=call.name,
                tool_input={"investigation_id": investigation_id},
                status=ReActTraceStepStatus.FAILED,
                error_message=str(exc),
                completed_at=datetime.now(UTC),
            )
        return ReActTraceStep(
            step_number=step_number,
            assistant_text=response.content,
            tool_name=call.name,
            tool_input={"investigation_id": investigation_id},
            tool_call_id=record.id,
            observation=_observation(record.status, record.output_evidence_ids, record.error_message),
            output_evidence_ids=record.output_evidence_ids,
            status=ReActTraceStepStatus.OBSERVED if record.status == ToolCallStatus.SUCCESS else ReActTraceStepStatus.FAILED,
            error_message=record.error_message,
            completed_at=record.completed_at,
        )


def _system_message(event: IncidentEvent) -> dict:
    return {
        "role": "system",
        "content": (
            "You are a read-only SRE investigation agent. "
            "Do not remediate, restart, scale, rollback, mutate config, or use SSH. "
            f"Investigate service={event.service} environment={event.environment}."
        ),
    }


def _user_message(event: IncidentEvent, evidence_ids: list[str]) -> dict:
    return {
        "role": "user",
        "content": f"{event.title}\n{event.description}\nExisting evidence IDs: {', '.join(evidence_ids) or 'none'}",
    }


def _tool_schema(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": {"type": "object", "properties": {"investigation_id": {"type": "string"}}},
        "read_only": spec.read_only,
    }


def _observation(status, evidence_ids: list[str], error: str | None) -> str:
    return f"status={status}; evidence_ids={','.join(evidence_ids) or 'none'}; error={error or 'none'}"
```

- [ ] **Step 4: Run GREEN check**

Run:

```bash
uv run pytest tests/diagnosis/test_react_agent.py -v
```

Expected: PASS.

---

### Task 4: Orchestrator Integration Behind Disabled Guard

**Files:**
- Modify: `backend/config/settings.py`
- Modify: `backend/diagnosis/orchestrator.py`
- Test: `tests/diagnosis/test_orchestrator.py`
- Optional Test: `tests/config/test_settings.py`

- [ ] **Step 1: Write failing orchestrator test**

Append to `tests/diagnosis/test_orchestrator.py`:

```python
from backend.diagnosis.react_agent import ReActInvestigationAgent, ReActLlmResponse
from backend.domain.react_trace import ReActTraceStatus


class OneShotReactLlm:
    def generate(self, messages, tools):
        return ReActLlmResponse(content="Read-only ReAct review complete.")


def test_orchestrator_records_v6_react_trace_when_agent_is_configured():
    repository = InMemoryInvestigationRepository()
    orchestrator = _orchestrator(repository)
    orchestrator.react_agent = ReActInvestigationAgent(
        llm=OneShotReactLlm(),
        tool_registry=orchestrator.execution_engine.tool_registry,
        max_steps=1,
    )

    record = orchestrator.run(_event())
    trace = repository.get_react_trace(record.id)

    assert trace is not None
    assert trace.status == ReActTraceStatus.COMPLETED
    assert trace.final_answer == "Read-only ReAct review complete."
```

Use the existing local helpers in `test_orchestrator.py` instead of duplicating setup names if they differ.

- [ ] **Step 2: Run RED check**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py::test_orchestrator_records_v6_react_trace_when_agent_is_configured -v
```

Expected: FAIL because orchestrator does not accept or run `react_agent`.

- [ ] **Step 3: Add minimal orchestrator hook**

In `backend/diagnosis/orchestrator.py`:

```python
def __init__(..., react_agent: ReActInvestigationAgent | None = None) -> None:
    ...
    self.react_agent = react_agent

def _record_v6_react_trace(self, investigation_id: str, event: IncidentEvent, evidence: list[EvidenceItem]) -> None:
    if self.react_agent is None:
        return
    try:
        trace = self.react_agent.run(
            investigation_id=investigation_id,
            event=event,
            existing_evidence_ids=[item.id for item in evidence],
        )
        save_trace = getattr(self.repository, "save_react_trace", None)
        if callable(save_trace):
            save_trace(trace)
    except Exception as exc:
        logger.warning("v6 react trace failed id=%s reason=%s", investigation_id, exc, exc_info=True)
```

Call `_record_v6_react_trace(record.id, event, evidence)` after evidence is saved and before report generation. Keep it warning-only.

In `backend/config/settings.py`, add:

```python
class ReActSettings(BaseModel):
    enabled: bool = False
    max_steps: int = 5

class AppSettings(BaseModel):
    ...
    react: ReActSettings = Field(default_factory=ReActSettings)
```

Add env overrides:

```python
if react_enabled := _get_env("DIAGOPS_REACT_ENABLED"):
    settings.react.enabled = _parse_bool(react_enabled)
```

Do not wire a real network LLM in this task. Default remains disabled.

- [ ] **Step 4: Run GREEN check**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py::test_orchestrator_records_v6_react_trace_when_agent_is_configured tests/config/test_settings.py -v
```

Expected: PASS.

---

### Task 5: ReAct Trace API

**Files:**
- Modify: `backend/api/agent_views.py`
- Test: `tests/api/test_v6_react_trace_api.py`

- [ ] **Step 1: Write failing API tests**

Add `tests/api/test_v6_react_trace_api.py`:

```python
from backend.domain.react_trace import ReActTrace, ReActTraceStatus, ReActTraceStep


def test_react_trace_endpoint_returns_null_when_missing(client):
    response = client.post("/events/simulated/deployment_regression")
    investigation_id = response.json()["investigation_id"]

    trace_response = client.get(f"/investigations/{investigation_id}/react-trace")

    assert trace_response.status_code == 200
    assert trace_response.json() is None


def test_react_trace_endpoint_returns_persisted_trace(client, repository):
    response = client.post("/events/simulated/deployment_regression")
    investigation_id = response.json()["investigation_id"]
    repository.save_react_trace(
        ReActTrace(
            investigation_id=investigation_id,
            status=ReActTraceStatus.COMPLETED,
            final_answer="done",
            steps=[ReActTraceStep(step_number=1, tool_name="read_logs")],
        )
    )

    trace_response = client.get(f"/investigations/{investigation_id}/react-trace")

    assert trace_response.status_code == 200
    assert trace_response.json()["final_answer"] == "done"
    assert trace_response.json()["steps"][0]["tool_name"] == "read_logs"
```

Use existing API fixtures. If the repository fixture name differs, use the fixture already present in V5 API tests.

- [ ] **Step 2: Run RED check**

Run:

```bash
uv run pytest tests/api/test_v6_react_trace_api.py -v
```

Expected: FAIL because endpoint does not exist.

- [ ] **Step 3: Add endpoint**

In `backend/api/agent_views.py`:

```python
from backend.domain.react_trace import ReActTrace


def _get_react_trace(investigation_id: str) -> ReActTrace | None:
    get_trace = getattr(_repository(), "get_react_trace", None)
    if not callable(get_trace):
        return None
    return get_trace(investigation_id)


@router.get("/{investigation_id}/react-trace", response_model=ReActTrace | None)
def get_investigation_react_trace(investigation_id: str) -> ReActTrace | None:
    _get_investigation_record(investigation_id)
    return _get_react_trace(investigation_id)
```

- [ ] **Step 4: Run GREEN check**

Run:

```bash
uv run pytest tests/api/test_v6_react_trace_api.py -v
```

Expected: PASS.

---

### Task 6: Frontend ReAct Trace Panel

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css` only if existing classes are insufficient.
- Test: `tests/frontend/test_frontend_smoke.py`

- [ ] **Step 1: Write failing frontend smoke tests**

Update `tests/frontend/test_frontend_smoke.py`:

```python
def test_api_exposes_v6_react_trace_method():
    api = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "export type ReActTrace" in api
    assert "getReActTrace" in api
    assert "/react-trace" in api


def test_app_contains_react_trace_panel_copy():
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert "ReAct 推理过程" in app
    assert "只读" in app
    assert "getReActTrace" in app
```

Keep the existing unsafe wording test and add these strings to the same file.

- [ ] **Step 2: Run RED check**

Run:

```bash
uv run pytest tests/frontend/test_frontend_smoke.py -v
```

Expected: FAIL because V6 frontend strings and API method do not exist.

- [ ] **Step 3: Add API types and client**

In `frontend/src/api.ts`, add:

```ts
export type ReActTraceStep = {
  step_number: number;
  assistant_text?: string | null;
  tool_name?: string | null;
  tool_input: Record<string, unknown>;
  tool_call_id?: string | null;
  observation?: string | null;
  output_evidence_ids: string[];
  status: string;
  error_message?: string | null;
  started_at: string;
  completed_at?: string | null;
};

export type ReActTrace = {
  id: string;
  investigation_id: string;
  status: string;
  final_answer?: string | null;
  steps: ReActTraceStep[];
  created_at: string;
  completed_at?: string | null;
};

export function getReActTrace(id: string) {
  return request<ReActTrace | null>(`/investigations/${id}/react-trace`);
}
```

- [ ] **Step 4: Add trace panel**

In `frontend/src/App.tsx`, add a small component near `RcaWorkbenchPanel`:

```tsx
function ReActTracePanel({ investigationId }: { investigationId: string }) {
  const traceQuery = useQuery({
    queryKey: ["react-trace", investigationId],
    queryFn: () => getReActTrace(investigationId),
    enabled: Boolean(investigationId),
  });
  const trace = traceQuery.data;

  return (
    <section className="panel">
      <div className="panel-heading">
        <h2>ReAct 推理过程</h2>
        <span>只读</span>
      </div>
      {traceQuery.isLoading ? (
        <div className="empty-state">加载 ReAct 轨迹...</div>
      ) : traceQuery.isError ? (
        <p className="error">ReAct 轨迹加载失败：{traceQuery.error.message}</p>
      ) : !trace ? (
        <div className="empty-state">暂无 ReAct 轨迹。</div>
      ) : (
        <div className="compact-list">
          {trace.final_answer ? <p>{trace.final_answer}</p> : null}
          {trace.steps.map((step) => (
            <article className="compact-row" key={step.step_number}>
              <strong>#{step.step_number} {step.tool_name ?? step.status}</strong>
              {step.assistant_text ? <p>{step.assistant_text}</p> : null}
              {step.observation ? <p>{step.observation}</p> : null}
              <div className="process-detail">
                <span>状态: {step.status}</span>
                <span>证据: {formatIdList(step.output_evidence_ids)}</span>
              </div>
              {step.error_message ? <p className="error">{step.error_message}</p> : null}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
```

Render it beside the existing RCA workbench for the active investigation.

- [ ] **Step 5: Run GREEN checks**

Run:

```bash
uv run pytest tests/frontend/test_frontend_smoke.py -v
cd frontend
npm.cmd run build
```

Expected: pytest PASS; build exits 0. The existing React Query `"use client"` Vite warning is acceptable.

---

### Task 7: README And Final Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document V6**

Add a short `## V6 Single-Agent ReAct` section after V5:

```markdown
## V6 Single-Agent ReAct

DiagOps V6 adds an optional read-only ReAct trace for investigations:

- A single ReActInvestigationAgent can ask the LLM for one read-only tool call at a time.
- Supported tools are provider evidence tools only: logs, metrics, deployments, dependencies, service catalog, and memory lookup.
- The trace records assistant text, tool call, observation, evidence IDs, status, and final answer.
- The deterministic RCA pipeline remains the source of truth.

V6 does not execute shell commands, SSH, rollback, restart, scaling, configuration changes, or remediation.

```text
GET /investigations/{id}/react-trace
```
```

- [ ] **Step 2: Run focused V6 tests**

Run:

```bash
uv run pytest tests/domain/test_react_trace.py tests/db/test_v6_react_trace_persistence.py tests/diagnosis/test_react_agent.py tests/api/test_v6_react_trace_api.py tests/frontend/test_frontend_smoke.py -v
```

Expected: PASS.

- [ ] **Step 3: Run full backend checks**

Run:

```bash
uv run ruff check .
uv run pytest -v
```

Expected: ruff PASS; pytest PASS. The known Starlette deprecation warning is acceptable.

- [ ] **Step 4: Run frontend build**

Run:

```bash
cd frontend
npm.cmd run build
```

Expected: build exits 0. The existing React Query `"use client"` Vite warning is acceptable.

- [ ] **Step 5: Run safety wording scan**

Run:

```bash
rg -n "automatic rollback|automatic restart|automatic scale|automatic config change|SSH command execution|自动回滚|自动重启|自动扩容|自动配置变更" README.md frontend/src backend docs/superpowers/specs/2026-07-08-diagops-v6-single-agent-react-design.md
```

Expected: no matches. `rg` exit code 1 is acceptable for no matches.

- [ ] **Step 6: Final review**

Review the diff against:

- `AGENT.md`
- `docs/superpowers/specs/2026-07-08-diagops-v6-single-agent-react-design.md`
- `C:/Users/林佳威/.codex/skills/iteration-flow/references/diagops-review-checklist.md`

Required review result:

```text
PASS
```

or blocking findings with file and line numbers.

---

## Self-Review Notes

- Spec coverage: domain, persistence, runtime, orchestrator guard, API, frontend, docs, and verification are covered.
- Scope kept small: no multi-agent ReAct, no sandbox, no general command tools, no OpenDerisk runtime migration.
- Safety: V6 exposes only existing read-only provider tools and default-disabled ReAct integration.
- TDD: each behavior task starts with a failing test and focused RED/GREEN command.
