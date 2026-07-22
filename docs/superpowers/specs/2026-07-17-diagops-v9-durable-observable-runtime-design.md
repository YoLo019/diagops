# DiagOps V9 Durable & Observable Agent Runtime Design

Status: `approved`

Date: 2026-07-17

## 1. Summary

DiagOps V9 upgrades the V8.2 SRE investigation pipeline into a durable and observable domain runtime. It adds explicit lifecycle management, multiple concurrent investigation sessions, crash recovery, real-time events, OpenTelemetry tracing, audit replay, and run comparison while preserving the existing Fixed and Adaptive RCA semantics.

V9 is not a generic Agent framework. Its purpose is to make the existing SRE multi-agent investigation workflow reliable, inspectable, recoverable, and operationally measurable. The runtime must provide clear contracts for concurrency, failure recovery, observability, evaluation, and security instead of treating model and Tool invocation as the complete execution system.

The V8.2 real OpenRCA 40-case gate remains an independent release prerequisite and regression baseline. V9 features cannot replace or weaken that gate.

## 2. Motivation and Competitive Positioning

The compared projects provide useful breadth in Agent platforms, tool ecosystems, workflow orchestration, and command-line interaction. DiagOps should not compete by copying their full feature surfaces. Its product boundary is a focused SRE Agent runtime whose execution can be interrupted, resumed, streamed, traced, replayed, compared, and evaluated.

V9 therefore emphasizes the following differentiators:

1. Domain-specific multi-agent RCA rather than a generic conversation or application framework.
2. Durable lifecycle and checkpoint recovery rather than only an in-memory execution loop.
3. Multiple isolated investigation sessions rather than a single global current task.
4. Safe, structured runtime events rather than exposing hidden reasoning or raw provider payloads.
5. Audit replay and deterministic validation rather than pretending that an LLM execution is exactly reproducible.
6. Run Diff and OpenRCA regression evidence for repeatable quality and runtime comparison.
7. Fault-injection verification of cancellation, lease loss, persistence failure, and resume races.

The reference projects remain sources of comparison, not compatibility targets:

- `qinshihu/itops-agent-platform`: operational Agent platform and workflow productization.
- `derisk-ai/OpenDerisk`: broad Agent application, knowledge, tool, and orchestration capabilities.
- `itwanger/PaiCLI-Python` (PaiCLI): interactive command-line Agent experience and task execution.

## 3. Goals

V9 must:

1. Represent every live analysis, supplemental analysis, manual rerun, and replay as an explicit Runtime Run.
2. Support multiple independent investigation sessions executing concurrently and visible as separate UI windows or tabs.
3. Persist a safe, append-only event timeline with a strict sequence per Run.
4. Persist phase checkpoints that allow an interrupted Run to resume from its last completed phase.
5. Make successful tool side effects idempotently reusable during recovery.
6. Provide cooperative cancellation, lease-based single ownership, and explicit manual recovery.
7. Stream events through reconnectable SSE without making client connectivity part of runtime correctness.
8. Export privacy-safe OpenTelemetry traces without making the collector a runtime dependency.
9. Replay recorded execution artifacts without calling an LLM, provider, production tool, or evidence source.
10. Compare two Runs using stable structured facts and metrics.
11. Preserve V8.2 Fixed, Adaptive, deterministic fallback, evidence, review, and report behavior.

## 4. Non-goals

V9 does not:

1. Build a generic `Thread` or `Conversation` framework. `Investigation` is the domain session boundary.
2. Add an external queue, Redis, Celery, or distributed workers.
3. Automatically resume crashed Runs or introduce unbounded retries.
4. Add MCP, a generic Skill system, RAG, arbitrary Shell execution, or production write tools.
5. Replace the OpenAI Agents SDK or provider abstraction.
6. rebuild all business state from events. Runtime events support audit and replay validation, not full Event Sourcing.
7. Promise exactly-once LLM invocation. It guarantees acceptance and persistence rules around logical results instead.
8. Store prompts, chain-of-thought, credentials, evidence bodies, or raw provider payloads in events or telemetry.
9. Backfill synthetic runtime histories for investigations created before V9.

## 5. Terminology and Session Model

The runtime uses four distinct scopes:

- **Investigation**: one domain analysis session, corresponding to one user-visible analysis window or tab. It owns incident context and its history of analysis rounds.
- **RuntimeRun**: one analysis round within an Investigation. Initial analysis, additional-evidence analysis, manual rerun, and replay are separate Runs.
- **RuntimeAttempt**: one execution ownership period for a Run. Every initial start or manual resume creates a new Attempt.
- **RuntimeEvent**: one append-only, ordered runtime fact emitted by an Attempt.

Multiple Investigations may execute concurrently. Within one Investigation, at most one live Run may be active so that two rounds cannot concurrently modify the same domain session. Adding evidence to an existing session creates a new Run with `parent_run_id` and `run_reason=additional_evidence`; it never overwrites the previous Run.

Every read and write that belongs to an analysis is scoped by `investigation_id` and `run_id`. There is no process-global “current investigation” or “current run”. Incident context, evidence, findings, reviews, reports, event sequence, budgets, model configuration, prompt version, strategy, SSE subscription, and telemetry attributes remain isolated per session and Run.

## 6. Architecture

```text
Investigation API
        |
        v
Runtime Coordinator
  - lifecycle / lease / attempt
  - cancel / timeout / recovery
  - event / checkpoint transaction boundary
        |
        v
Phase Executor
        |
        v
Existing Fixed or Adaptive RCA Pipeline
        |
        +--> Provider / Agent / Tool
        |
        +--> Evidence / Finding / Review / Report repositories

Runtime Coordinator
        +--> Append-only Event Store --> SSE / OTel / Replay / Diff
        +--> Checkpoint Store -------> Manual Recovery
```

The Runtime Coordinator owns execution mechanics, not RCA decisions. The Phase Executor maps the existing pipeline into stable recovery boundaries. Existing domain repositories remain authoritative for `Investigation`, `Evidence`, `Finding`, `Review`, and `Report`. Runtime events describe what happened; they do not replace those business records.

The Event Store is append-only and supports timeline display, audit, telemetry projection, replay validation, and diff input. The Checkpoint Store contains only the minimum resumable control state. Fixed, Adaptive, specialist analysis, conflict review, coordination, reporting, and deterministic fallback continue to use their existing domain services.

## 7. Runtime Data Contracts

### 7.1 RuntimeRun

```text
id
investigation_id
run_kind: live | replay
strategy: fixed | adaptive
status
current_phase?
source_run_id?
parent_run_id?
run_reason: initial | additional_evidence | manual_rerun | replay
model_provider?
model_name?
prompt_version?
latest_checkpoint_id?
failure_category?
created_at
started_at?
completed_at?
cancel_requested_at?
lease_owner?
lease_expires_at?
lease_version
```

`source_run_id` identifies the audited source of a replay. `parent_run_id` relates a supplemental or manually repeated live Run to its predecessor. Model and prompt metadata are frozen on the Run so history and Diff remain meaningful after configuration changes.

### 7.2 RuntimeAttempt

```text
id
run_id
attempt_number
resume_from_checkpoint_id?
status
started_at
completed_at?
failure_category?
```

Initial execution and every manual resume create a new Attempt. Previous Attempts are immutable history and are never overwritten.

### 7.3 RuntimeEvent

```text
id
run_id
attempt_id
sequence
event_type
phase?
actor_type
actor_name?
task_id?
execution_id?
tool_call_id?
evidence_ids[]
safe_payload
occurred_at
schema_version
```

`sequence` is strictly increasing and unique within a Run. `safe_payload` is constructed from an event-type-specific allowlist. It must never contain a raw prompt, model free text, chain-of-thought, credentials, evidence body, or raw Provider response.

### 7.4 RuntimeCheckpoint

```text
id
run_id
attempt_id
completed_phase
event_sequence
state_digest
resume_state
created_at
schema_version
```

`resume_state` contains identifiers, phase position, remaining budgets, and idempotency keys. It does not duplicate evidence, findings, reviews, reports, or provider output. `state_digest` protects the integrity of the control state and its referenced committed records.

## 8. Lifecycle and State Machine

The valid Run transitions are:

```text
created -----> running -----> completed
   |              |---------> failed
   |              |---------> cancelling -----> cancelled
   |              |                  |----------> interrupted
   |              |---------> interrupted -----> running (explicit resume)
   |-------------------------------------------> cancelled
```

Rules:

1. `completed`, `failed`, and `cancelled` are terminal.
2. Only `interrupted` is resumable.
3. A business failure is not resumed; the user creates a new Run.
4. Expired leases on `running` or `cancelling` Runs are marked `interrupted` by a recovery audit. They are not resumed automatically.
5. Every transition from `interrupted` to `running` creates a new Attempt.
6. Invalid transitions fail atomically and emit no misleading success event.

Attempt state follows the owning Run but remains a separate immutable execution record. The current Attempt is completed, failed, cancelled, or interrupted before a later Attempt starts.

## 9. Stable Execution Phases

The runtime exposes these phases:

1. `intake`
2. `evidence_collection`
3. `deterministic_rca`
4. `specialist_analysis`
5. `conflict_review`
6. `coordination`
7. `report_generation`
8. `finalize`

`conflict_review` may be recorded as skipped. Adaptive evidence queries are events within `specialist_analysis`, not dynamically invented recovery phases. A checkpoint is written after each successfully completed phase. A completed phase is not rerun during resume.

The checkpoint event, business result references, event sequence update, and checkpoint record are committed in the same SQLite transaction. Network calls never occur inside that transaction.

## 10. Concurrency Model

V9 uses **single-writer + concurrent workers + bounded asynchronous I/O**.

### 10.1 Across sessions

Multiple Investigations may have active Runs at the same time. The default process-wide limit is four concurrent Runs. A failure in one session cannot mutate, cancel, or consume the budget of another session.

### 10.2 Within one Run

The default limit is three parallel steps per Run:

- Evidence providers may execute in parallel.
- Deterministic RCA starts after required evidence collection completes.
- Log, Metric, and Deployment specialists may execute in parallel.
- ReAct calls inside one specialist remain sequential because each call depends on previous observations.
- Conflict review for affected specialists may execute in parallel.
- Coordination, report generation, and finalize remain sequential.

Workers call external models, providers, and tools and return structured results. They do not mutate a shared Run object or independently allocate event sequences. A central writer serializes Runtime Event, business result, and Checkpoint persistence.

A sibling step failure is captured as a structured result and does not automatically cancel successful siblings. A global timeout, explicit user cancellation, or lost Run lease cancels the task group and prevents new work from starting.

### 10.3 SQLite discipline

Transactions are short. No Provider, Agent, Tool, OTel exporter, or SSE operation occurs inside a database transaction. Conditional updates and unique constraints enforce ownership and ordering instead of relying only on in-process locks.

## 11. Lease and Execution Ownership

Each active Run has a database lease:

- `lease_owner` identifies the executor.
- `lease_expires_at` defines ownership expiry.
- `lease_version` provides fencing for conditional writes.

Lease acquisition and renewal use conditional updates. At most one executor can own a Run, but different executors or tasks can own different Runs; this is not a global single-thread lock. Once an executor loses its lease, it stops accepting new Agent or Tool results and cannot advance the Run.

Only an expired lease can cause a stale `running` or `cancelling` Run to become `interrupted`. The database enforces `UNIQUE(run_id, sequence)` for events. V9 remains an in-process executor design and does not introduce distributed scheduling.

## 12. Tool and Model Idempotence

Before a Tool executes, the runtime allocates a stable `tool_call_id` and idempotency key derived from the logical operation and normalized safe inputs.

1. A successfully persisted ToolCall is reused during resume and is not executed again.
2. An in-progress ToolCall found after interruption remains interrupted; it is never converted to success without a committed result.
3. A completed phase is never rerun.
4. LLM calls are not exactly-once. If a request may have reached the provider before a crash, resume may issue a new request with a new `execution_id`, but only one valid result for the logical step is accepted and persisted.
5. Late results from a lost lease, cancelled task group, or superseded execution are rejected from state advancement and retained only when a safe audit fact is needed.

## 13. Cancellation

`POST /runtime-runs/{run_id}/cancel` atomically records `cancel_requested_at` and transitions an eligible Run to `cancelling`.

Cancellation is cooperative. The coordinator checks cancellation before and after every Agent, Tool, and Provider boundary. It does not hard-kill Python threads. A legitimate in-flight result may be persisted and audited, but cancellation prevents the next step from starting. At a safe boundary, the Attempt and Run transition to `cancelled`.

A cancelled Run is terminal and cannot be resumed. Continuing the business analysis requires a new Run linked by `parent_run_id`.

## 14. Interruption and Manual Recovery

After a process crash, the database may contain a `running` Run, an expired lease, nonterminal calls, committed domain records, events, and a last complete checkpoint.

On startup or explicit audit, recovery logic:

1. Finds expired leases.
2. Atomically marks affected Runs, Attempts, and nonterminal calls `interrupted`.
3. Preserves all committed evidence, findings, reviews, reports, events, and checkpoints.
4. Performs no Agent, Provider, Tool, or production evidence call.

`POST /runtime-runs/{run_id}/resume` is explicit. It validates status, checkpoint schema, digest, referenced business records, remaining budgets, and ownership. If valid, it obtains a new lease, creates a new Attempt, links the Attempt to the checkpoint, and starts after the completed phase. Successful Tool results are reused. Interrupted LLM work receives a new `execution_id` if it must run again.

If checkpoint validation fails, the request is rejected, a safe recovery rejection is recorded, and the Run remains `interrupted`.

New runtime failure categories are:

- `lease_lost`
- `checkpoint_invalid`
- `resume_conflict`
- `persistence_failure`
- `cancelled`

Existing provider, model, timeout, output validation, and unknown failure categories remain. All diagnostic detail is redacted before persistence or publication.

## 15. Runtime Event Contract

V9 defines these stable event types:

```text
run.created
run.started
run.completed
run.failed
run.interrupted
run.cancel_requested
run.cancelled

attempt.started
attempt.completed
attempt.interrupted

phase.started
phase.completed
phase.failed
phase.skipped

agent.started
agent.completed
agent.failed

model.started
model.completed
model.failed

tool.proposed
tool.started
tool.completed
tool.failed
tool.rejected
tool.skipped

evidence.persisted
evidence.rejected

checkpoint.created

recovery.started
recovery.completed
recovery.rejected
```

Event schema evolution is controlled by `schema_version`. Unknown event types and newer schema versions remain displayable as opaque safe metadata but cannot silently drive replay state transitions.

## 16. SSE Real-time Stream

The stream endpoint is:

```http
GET /runtime-runs/{run_id}/events/stream
```

The SSE `id` is the Run-local event `sequence`. Clients reconnect with `Last-Event-ID`. The server first queries committed events after that sequence, then attaches the client to the live feed without a gap. Heartbeats carry no event sequence and are not business events.

A slow or disconnected client does not affect Run execution. The database is the catch-up source of truth; the in-memory live feed is only a delivery optimization. Each UI window subscribes to its selected Run, and reopening a page resumes from the last received sequence. Closing a page does not cancel the Run.

## 17. OpenTelemetry

Each Runtime Attempt is represented by one trace. A resumed Attempt starts a new trace and uses a Span Link to the preceding Attempt rather than pretending both Attempts are one uninterrupted execution.

The span hierarchy is:

```text
Attempt trace
  -> Phase span
    -> Agent span
      -> Model or Tool span
        -> Evidence event
```

Allowed attributes are:

```text
investigation_id
run_id
attempt_id
phase
agent_name
tool_name
provider
model
status
duration_ms
input_tokens
output_tokens
cost
evidence_count
failure_category
```

Prompts, model free text, chain-of-thought, evidence bodies, credentials, and raw provider payloads are prohibited. The OTLP exporter is optional and disabled by default. Collector delay or failure must not fail, block, or change the Run.

## 18. Audit Replay

Replay verifies a recorded execution; it does not re-execute Agents.

`POST /runtime-runs/{run_id}/replay` creates a replay Run with `source_run_id` and `run_reason=replay`. The replay engine:

1. Validates continuous, unique event sequence.
2. Validates legal lifecycle and phase transitions.
3. Validates checkpoint schemas and digests.
4. Validates referenced Evidence, Finding, Review, and Report records.
5. Reruns deterministic validators and the benchmark evaluator against persisted outputs.
6. Produces a structured `ReplayReport`.

Replay must make zero LLM, Provider, Tool, or production evidence calls, consume zero model tokens, and perform no production reads. An attempted external call is a test failure and a runtime safety rejection.

## 19. Run Diff

The endpoint is:

```http
GET /runtime-runs/{run_id}/diff?against_run_id={other_run_id}
```

Diff compares structured, stable facts:

- strategy, model, provider, and prompt version;
- phase status and duration;
- Agent success, failure, and retry counts;
- normalized Tool inputs, statuses, and counts;
- evidence additions, omissions, and reference changes;
- candidate causes, final decision, and confidence;
- tokens, cost, and wall-clock duration;
- fallback usage and failure categories.

It does not compare hidden reasoning, raw prompts, or arbitrary model prose. Textual business outputs may be represented through normalized domain fields, identifiers, and hashes where necessary.

## 20. API Surface

```http
POST /investigations/{investigation_id}/runtime-runs
GET  /investigations/{investigation_id}/runtime-runs
GET  /runtime-runs/{run_id}
GET  /runtime-runs/{run_id}/events
GET  /runtime-runs/{run_id}/events/stream
POST /runtime-runs/{run_id}/cancel
POST /runtime-runs/{run_id}/resume
POST /runtime-runs/{run_id}/replay
GET  /runtime-runs/{run_id}/diff?against_run_id={other_run_id}
```

The new API is additive. Existing Investigation APIs remain compatible. Existing synchronous analysis APIs wait for completion but execute through and record the Runtime Run. The new Runtime Run creation endpoint returns `202 Accepted` and allows background execution and polling or streaming.

All endpoints enforce Investigation and Run ownership consistency. A Run from one Investigation cannot be resumed, diffed, or streamed through another Investigation context.

## 21. Runtime UI

The workbench exposes:

- A left panel containing multiple Investigation sessions and their real-time statuses.
- A middle panel containing the phase timeline and parallel Agent swimlanes.
- A right panel containing safe Event, Tool, Evidence, and Checkpoint detail.
- Run history switching within a session.
- Explicit cancel, recover, replay, and compare actions when valid.
- Read-only historical and terminal Runs.

The UI clearly distinguishes session concurrency from Run-internal parallelism. Users can switch among several active windows while the backend continues their isolated Runs. Closing or switching a window changes only the subscription, not execution.

## 22. Persistence and Migration

V9 adds these SQLite tables:

- `runtime_runs`
- `runtime_attempts`
- `runtime_events`
- `runtime_checkpoints`

The migration is incremental. Existing Investigation records are not assigned fabricated history; APIs expose `runtime_available=false` for them. New analyses write both existing business records and Runtime records. The in-memory repository maintains behavioral parity for tests and explicit configuration.

Foreign keys are enabled and verified with `PRAGMA foreign_key_check`. Runtime event and checkpoint schemas have explicit versions separate from the database migration version. The first V9 release does not automatically delete runtime events.

Required indexes and constraints include:

- unique event `(run_id, sequence)`;
- unique Attempt `(run_id, attempt_number)`;
- indexes for Investigation Run history, active Run lookup, lease expiry, and event catch-up;
- foreign keys from Attempt, Event, and Checkpoint to Run;
- foreign keys or validated references for source, parent, and latest checkpoint relationships.

## 23. Configuration

```yaml
runtime:
  enabled: true
  max_concurrent_runs: 4
  max_parallel_steps_per_run: 3
  lease_seconds: 30
  heartbeat_seconds: 10
  opentelemetry:
    enabled: false
    endpoint: null
```

Environment overrides follow the existing typed configuration pattern. Limits must be finite and validated as positive integers. OpenTelemetry remains disabled unless both configuration and a valid endpoint enable it.

## 24. Security and Privacy

All externally visible runtime artifacts use allowlisted structured fields. Redaction happens before database persistence, SSE publication, logging, OTel export, replay report generation, and Diff generation.

The following are prohibited across all runtime artifacts:

- credentials, API keys, authorization headers, and connection secrets;
- raw prompts and provider request or response payloads;
- chain-of-thought or hidden model reasoning;
- raw log bodies, evidence bodies, or unbounded tool output;
- arbitrary exception representations that may embed inputs.

Tool inputs are normalized to approved identifiers and bounded metadata. Error records use a failure category, safe code, and redacted message. Injection-like text inside evidence or model output is data and cannot introduce new event fields, telemetry attributes, URLs, or Tool calls.

## 25. Fault-injection Matrix

V9 verification includes deterministic injection points:

1. Crash immediately before a phase starts.
2. Provider returns immediately before evidence commit.
3. Tool success commits, then the process crashes before the phase checkpoint.
4. An LLM request is sent, then the process crashes before result persistence.
5. Event and business result persistence raises mid-transaction, proving the entire transaction rolls back.
6. The executor loses its Run lease.
7. Two clients concurrently resume the same interrupted Run.
8. SSE reconnects from an old sequence while new events are arriving.
9. Checkpoint state or referenced records are tampered with.
10. Four sessions run concurrently while one fails.
11. Cancellation occurs during parallel specialist execution.
12. The OTel collector is unavailable or slow.
13. Replay code attempts an external call.
14. Evidence, provider errors, or Tool output attempts to inject credentials or raw payloads into an Event.

Each injection has assertions for Run and Attempt state, event order, committed business records, checkpoint position, external call count, lease ownership, and absence of sensitive data.

## 26. Acceptance Criteria

V9 is accepted only when all of the following are demonstrated:

1. Four Investigation sessions execute concurrently within configured limits.
2. There is zero cross-session contamination of context, records, budgets, events, streams, or traces.
3. Every Run has a continuous, unique, strictly increasing business event sequence.
4. SSE catch-up and reconnect produce no missing or duplicate business events.
5. Interruption leaves the Run at its last fully committed checkpoint.
6. Resume does not repeat a successfully persisted Tool call.
7. In a concurrent resume race, exactly one requester acquires execution ownership.
8. A tampered or invalid checkpoint cannot advance the Run.
9. Replay performs zero external calls and consumes zero model tokens.
10. Replay detects sequence gaps, illegal transitions, invalid references, and checkpoint tampering.
11. Run Diff produces stable structured output for the same input pair.
12. Every Attempt, Phase, Agent, Model, and Tool record has correct OTel linkage when export is enabled.
13. Event, SSE, OTel, Replay, Diff, and logs contain zero prohibited secrets or raw content.
14. Fixed and Adaptive semantics, evidence contracts, review behavior, and deterministic fallback do not regress.
15. The complete V8.2 OpenRCA 40-case suite is rerun and the score does not fall below the frozen V8.2 baseline.
16. Runtime latency, SQLite growth, OTel overhead, and recovery time are reported; regressions are visible rather than excluded.

## 27. Verification Strategy

Standard verification remains:

```powershell
ruff check .
pytest -v
npm run build
```

The implementation plan must add focused suites for:

- lifecycle transition and repository contracts;
- SQLite migration and `foreign_key_check`;
- lease fencing and concurrent resume;
- multiple concurrent session isolation;
- bounded intra-Run parallelism;
- tool idempotence and late result rejection;
- cancellation at every Agent, Tool, and Provider boundary;
- fault-injection recovery;
- SSE catch-up, heartbeat, slow client, and reconnect;
- OTel span linkage, disabled mode, collector failure, and attribute safety;
- replay zero-external-call enforcement and corruption detection;
- stable Run Diff fixtures;
- all V8.2 OpenRCA 40 cases and frozen-score comparison.

## 28. Recommended Implementation Sequence

This sequence records architectural dependency order only. The implementation plan must decompose it into test-first tasks after this design is approved.

1. Runtime contracts, state transitions, schema versioning, and SQLite migration.
2. Event Store, central writer, repository parity, and compatibility path for existing APIs.
3. Lease fencing, Checkpoint Store, cancellation, recovery audit, and manual resume.
4. Multi-session Runtime API, background execution, and bounded parallel Phase Executor.
5. SSE catch-up/live bridge and runtime workbench UI.
6. OTel projection/export, audit replay, and Run Diff.
7. Fault-injection suite, OpenRCA regression, performance measurements, and operational documentation.

## 29. Design Decisions and Trade-offs

### 30.1 Domain runtime over generic framework

A generic Thread and Skill platform would add breadth, dilute the SRE-specific product boundary, and duplicate mature frameworks. V9 keeps `Investigation` as the domain session and invests in the reliability of the runtime used by that workflow.

### 30.2 Manual recovery over automatic retry

Automatic resume could unknowingly repeat costly or side-effecting work. Explicit interruption and manual resume keep state and operational risk visible.

### 30.3 Append-only audit over full Event Sourcing

Existing domain tables already express business state. Rebuilding everything from events would increase migration and correctness risk without improving runtime correctness. Events are authoritative for runtime history; domain repositories remain authoritative for RCA facts.

### 30.4 In-process bounded concurrency over distributed workers

Four concurrent sessions and parallel specialists satisfy the initial capacity target while exercising async orchestration, isolation, leases, and backpressure. External scheduling would expand operations scope before the single-process contracts are proven.

### 30.5 Checkpointed phases over arbitrary instruction resume

Stable phase boundaries allow validation, idempotence, and deterministic recovery. Resuming inside arbitrary LLM reasoning would be unverifiable and provider-dependent.

### 30.6 Audit replay over Agent re-execution

LLM and external data execution cannot be made deterministic. Audit replay proves that recorded state is internally consistent and re-runs deterministic evaluation without claiming reproducibility that the system cannot guarantee.

## 30. Approval Gate

This document is the proposed V9 design baseline. No V9 implementation plan or code execution begins until the user reviews this written specification and its status changes from `review_required` to `approved`.

After approval, the implementation plan must preserve every acceptance criterion, define exact files and test commands, and keep the V8.2 OpenRCA 40-case release gate visible as an independent prerequisite and regression baseline.
