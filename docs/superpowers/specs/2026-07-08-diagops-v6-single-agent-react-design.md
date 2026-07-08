# DiagOps V6 Single-Agent ReAct Design

## 1. Goal

V6 adds a real, read-only ReAct loop to DiagOps without replacing the existing deterministic RCA pipeline.

The main goal is:

```text
Incident investigation
  -> optional ReActInvestigationAgent receives evidence context and read-only tools
  -> LLM chooses one tool call at a time
  -> tool observation is appended to the trace
  -> loop continues until final answer or max_steps
  -> frontend shows the ReAct trace beside the existing RCA workbench
```

V6 should make the project closer to OpenDerisk's agent process model while keeping DiagOps evidence-first and read-only.

## 2. Non-Goals

V6 does not implement:

1. Multi-agent ReAct.
2. OpenDerisk runtime migration.
3. LangGraph, CrewAI, or a new agent framework.
4. Bash, shell, filesystem, browser, SSH, or code execution tools.
5. Automatic rollback, restart, scaling, configuration mutation, or remediation.
6. Long-term memory compaction, vector search, or sandbox management.
7. LLM-generated RCA as the source of truth.
8. Graph-first UI.

ponytail: V6 proves the ReAct loop with the current provider tools before adding more runtime.

## 3. Design Direction

V6 uses a single optional ReAct agent:

```text
ReActInvestigationAgent
  -> prompt builder
  -> read-only tool schema
  -> LLM client adapter
  -> trace recorder
```

The deterministic V1-V5 path remains the source of truth:

```text
provider evidence -> analyzer -> hypotheses -> V5 findings -> coordination review
```

The V6 ReAct path is additive:

```text
existing investigation context
  -> ReAct loop collects or reviews read-only evidence
  -> trace and final note are stored for inspection
```

If the ReAct agent is disabled, not configured, times out, or returns invalid tool calls, the investigation must still complete through the existing deterministic path.

## 4. User-Visible Behavior

When V6 is enabled for an investigation, the UI shows a ReAct trace panel.

The panel shows:

1. Step number.
2. Assistant text or thought summary.
3. Tool name and input.
4. Observation summary.
5. Output evidence IDs.
6. Step status.
7. Error message when a step fails.

The UI must also show the final ReAct answer when present.

If V6 is disabled or no trace exists, the panel shows a compact empty state and does not imply failure.

Safety wording must stay explicit:

1. The ReAct agent is read-only.
2. Tool calls collect or inspect evidence only.
3. The UI must not say an action was executed or a service was fixed.

## 5. ReAct Runtime

### 5.1 Loop

The runtime follows this loop:

```text
build messages
  -> call LLM with tool schemas
  -> if tool_call: validate tool, execute tool, append observation
  -> if final response: store final answer and stop
  -> stop at max_steps
```

Default `max_steps` is `5`.

The loop must stop when:

1. The LLM returns a final text response with no tool call.
2. The LLM requests an unsupported or invalid tool.
3. A tool call fails.
4. `max_steps` is reached.

Invalid or failed steps are recorded in the trace. They must not crash the base investigation unless the surrounding orchestrator already failed for another reason.

### 5.2 Prompt

The system prompt includes:

1. Role: read-only SRE investigation agent.
2. Service, environment, severity, title, description, and time window.
3. Safety boundary: no remediation or production mutation.
4. Available read-only tools.
5. Rule: final claims must reference evidence IDs returned by tools or already present in the investigation.
6. Rule: missing evidence and uncertainty must be stated.

The prompt should be generated from existing domain objects, not ad hoc request strings.

### 5.3 Tool Schema

V6 exposes only existing read-only provider tools:

```text
read_logs
query_metrics
read_deployments
query_dependencies
read_service_catalog
lookup_memory
```

Each tool schema contains:

```text
name
description
parameters
read_only=true
```

Tool inputs are intentionally small:

```text
investigation_id
```

The runtime may ignore extra LLM-provided fields. It must reject unknown tool names.

### 5.4 Observation

Each tool call returns an observation based on the existing `ToolCallRecord` contract:

```text
tool_call_id
status
output_evidence_ids
error_message?
duration_ms?
```

The observation is appended to the ReAct message history so the next LLM step can react to it.

## 6. Domain Model Changes

Add V6 domain models near the existing agent domain modules.

### 6.1 ReActTrace

```text
id
investigation_id
status
final_answer?
steps[]
created_at
completed_at?
```

Allowed statuses:

```text
running
completed
failed
max_steps
disabled
```

### 6.2 ReActTraceStep

```text
step_number
assistant_text?
tool_name?
tool_input
tool_call_id?
observation?
output_evidence_ids[]
status
error_message?
started_at
completed_at?
```

Allowed statuses:

```text
thinking
tool_called
observed
completed
failed
```

Validation rules:

1. `step_number` starts at 1.
2. `tool_name` must be one of the V6 read-only tools when present.
3. `output_evidence_ids` must reference existing evidence when the trace is attached to a completed investigation.
4. Trace payloads must remain JSON-compatible.

## 7. Persistence

Persist one ReAct trace per investigation.

Repository methods:

```text
save_react_trace(trace)
get_react_trace(investigation_id)
```

SQLite stores the trace as JSON payload plus indexed `investigation_id`.

In-memory persistence mirrors SQLite behavior.

## 8. API

Add:

```text
GET /investigations/{id}/react-trace
```

Response:

```text
ReActTrace | null
```

Existing investigation endpoints remain backward compatible.

The V6 trace may also be included in the frontend's investigation detail query only if that keeps the implementation smaller. Otherwise the frontend should fetch it as a separate query.

## 9. Frontend

Add a list-based ReAct trace panel.

The panel shows:

1. Final answer.
2. Step list.
3. Tool call rows.
4. Observation rows.
5. Evidence IDs.
6. Error state.

No graph UI is required in V6.

The copy must explicitly describe the ReAct agent as read-only.

## 10. Error Handling And Safety

The ReAct runtime must fail closed:

1. Unknown tool name -> record failed trace step and stop.
2. Malformed tool arguments -> record failed trace step and stop.
3. LLM client unavailable -> save disabled or failed trace and continue deterministic RCA.
4. Tool failure -> record observation and stop.
5. Invalid evidence references -> reject or omit invalid IDs; do not invent evidence.

The ReAct agent must never call mutation tools. There are no mutation tools in the V6 schema.

## 11. Test And Acceptance Criteria

Focused tests must prove:

1. A fake LLM tool call executes a read-only tool and records a trace step.
2. A tool observation is passed into the next LLM message.
3. A final LLM response completes the trace.
4. Unknown tool calls fail closed.
5. ReAct trace persistence round-trips in memory and SQLite.
6. The API returns `null` when no trace exists and returns the trace when present.
7. The frontend includes the ReAct trace panel and does not imply production mutation.
8. Existing V1-V5 tests still pass.

Final verification:

```bash
uv run ruff check .
uv run pytest -v
cd frontend
npm.cmd run build
```

Run a safety wording scan for rollback, restart, scaling, SSH, and configuration mutation claims.

## 12. Compatibility

V6 is additive.

Existing investigations without ReAct traces remain readable. Existing V5 workbench APIs continue to work. LLM/ReAct enablement must default to off or no-op unless configuration explicitly enables it.

## 13. Rollout

Implementation order:

1. Domain model and repository support.
2. Minimal ReAct runtime with fake-testable LLM adapter.
3. Orchestrator integration behind a config/default-disabled guard.
4. API endpoint.
5. Frontend trace panel.
6. Full verification.

The first release should prefer boring trace visibility over broad agent power.
