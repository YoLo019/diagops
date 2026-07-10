# DiagOps V7 Real Multi-Agent RCA Review Design

## 1. Goal

V7 connects the project-owned SRE/RCA framework to the OpenAI Agents SDK and
adds one real, read-only multi-agent review path.

V7 keeps the current evidence collection and deterministic RCA pipeline. After
all evidence is collected, three model-backed specialists analyze their own
evidence independently. A coordinator compares their findings with the
deterministic RCA baseline, requests at most one conflict-review round, and
produces an evidence-backed hybrid decision.

The target flow is:

```text
IncidentEvent
  -> collect all existing provider evidence
  -> deterministic RCA baseline
  -> LogAgent / MetricAgent / DeploymentAgent independent first round
  -> Coordinator compares baseline and findings
  -> conflicting specialists review once
  -> Coordinator produces agreement | conflict | agent_leads | fallback
  -> existing report and workbench show the complete evidence chain
```

V7 proves that real agents collaborate reliably without making LLM output the
uncontrolled source of truth.

## 2. Approved Product Decisions

The design was confirmed through interactive brainstorming:

1. V7 uses a hybrid decision rather than choosing deterministic RCA or agents
   as the universal authority.
2. Existing providers continue collecting all evidence before agent analysis.
3. V7 includes exactly `CoordinatorAgent`, `LogAgent`, `MetricAgent`, and
   `DeploymentAgent`.
4. Specialists perform an independent first round.
5. Conflicting specialists may review one additional round and no more.
6. Agent failure never prevents the deterministic investigation from
   completing.
7. Development tests use deterministic model substitutes.
8. Final acceptance includes 15 real OpenAI agent investigations over the five
   existing golden cases.

## 3. Why V7 Is Needed

V4-V6 established most of the project-owned contracts required by an execution
SDK:

1. V4 added plans, tasks, routes, agent executions, tool calls, shared context,
   memory records, and Agent process views.
2. V5 added specialist findings, coordination reviews, candidate ranking, and
   evidence-chain views.
3. V6 added a single-agent ReAct loop that can be tested with deterministic
   model substitutes, plus trace persistence.

The current application still does not run a real multi-agent system:

1. Specialist results are derived from provider results rather than independent
   model-backed analysis.
2. The custom planner and execution engine are deterministic and serial.
3. `ReActLlm` has test implementations but no application-wired real model
   adapter.
4. The current V6 ReAct path does not prove collaboration, conflict review, or
   real-model reliability.

The SRE/RCA business contracts are now stable enough to add the preferred
execution SDK without replacing them.

## 4. Challenge Result And Minimum Scope

The smallest useful V7 must prove:

1. Multiple real SDK agents run in one investigation.
2. Specialists reach findings independently from persisted evidence.
3. The coordinator detects agreement and conflict.
4. Conflict produces at most one targeted review round.
5. Every accepted conclusion cites real evidence IDs.
6. Agent failure degrades visibly to deterministic RCA.
7. Real-model evaluation is repeatable and measurable.

The most dangerous failure modes are:

1. A model invents an evidence ID or root cause.
2. A wrong agent conclusion silently replaces a correct deterministic result.
3. Log content changes Agent instructions through prompt injection.
4. SDK, model, or specialist failure causes the investigation to fail.
5. The UI presents a candidate or recommendation as an executed or confirmed
   production action.
6. Credentials or sensitive provider content are persisted or logged.

These are blocking acceptance conditions.

## 5. Non-Goals

V7 does not implement:

1. Coordinator-controlled adaptive evidence collection.
2. LangGraph or another graph runtime.
3. A generic multi-SDK runtime abstraction.
4. Distributed workers, queues, or cross-process parallelism.
5. Durable pause/resume, checkpoints, or long-running human interrupts.
6. DependencyAgent, ServiceCatalogAgent, MemoryAgent, ReportAgent, or new agent
   roles.
7. Vector search, memory summarization, or a knowledge graph.
8. Automatic remediation, rollback, restart, scaling, SSH, shell execution, or
   configuration mutation.
9. Unbounded debate or more than one specialist review round.
10. Replacing the existing provider layer or deterministic RCA pipeline.
11. A graph-first frontend, new frontend route, or graph library.
12. Streaming model output to the frontend.

ponytail: prove one bounded multi-agent review loop before adding adaptive
collection, more roles, or a workflow engine.

## 6. Execution Layer

V7 uses the OpenAI Agents SDK in a centralized coordinator pattern.

```text
CoordinatorAgent
  -> LogAgent as agent tool
  -> MetricAgent as agent tool
  -> DeploymentAgent as agent tool
```

The coordinator retains ownership of the investigation. V7 does not use a
handoff that transfers control to a specialist.

The SDK owns:

1. Model calls.
2. Agent-as-tool invocation.
3. Structured agent output.
4. Turn limits and SDK tracing.

DiagOps continues to own:

1. Incident, evidence, finding, hypothesis, report, action, and verification
   contracts.
2. Evidence filtering, persistence, and reference validation.
3. Deterministic RCA scoring.
4. Hybrid decision rules.
5. Golden evaluation, APIs, and frontend wording.

V7 adds one concrete `AgentsRcaRuntime`. It does not add an interface, factory,
or plugin system for hypothetical future SDKs.

Official SDK references:

1. <https://github.com/openai/openai-agents-python>
2. <https://openai.github.io/openai-agents-python/tools/#agents-as-tools>
3. <https://openai.github.io/openai-agents-python/guardrails/>
4. <https://openai.github.io/openai-agents-python/tracing/>

## 7. Runtime Flow

### 7.1 Base Investigation

The existing path runs first:

```text
create investigation
  -> collect every enabled provider result
  -> persist evidence and provider failures
  -> run deterministic analyzer
  -> persist hypotheses and the existing V5 findings/review
```

V7 does not let the coordinator choose providers or collect new evidence.
The V7 runtime runs after this baseline exists and before final report
generation. Action planning and report generation then continue through the
existing path.

### 7.2 Independent First Round

The coordinator must invoke all three specialists once.

Each specialist receives:

1. Incident metadata.
2. Its assigned evidence subset.
3. Evidence IDs and compact summaries.
4. Visible missing, failed, or partial provider state for its subset.
5. The read-only and evidence-citation rules.

The first-round specialist must not receive:

1. The deterministic top hypothesis.
2. Another specialist's finding.
3. The coordinator's preferred cause.

This avoids anchoring the independent assessments.

### 7.3 Coordinator Comparison

After all first-round results are available, the coordinator receives:

1. The deterministic hypotheses.
2. All validated first-round findings.
3. Compact summaries for all persisted evidence, including dependency, service
   catalog, related alert, and memory evidence.

The coordinator identifies:

1. Agreement between the baseline and specialists.
2. Agreement among specialists.
3. Direct contradictions.
4. Missing evidence and failed specialists.
5. Whether a second round is required.

A specialist is involved in a first-round conflict when its valid root-cause
finding differs from the non-low-confidence baseline, differs from another
specialist's valid root-cause finding, or explicitly contradicts either. An
`unknown` or low-confidence baseline does not create a conflict by itself.

### 7.4 One Conflict-Review Round

Only specialists involved in a conflict may run again.

The review input contains:

1. The specialist's original finding.
2. Every conflicting peer finding and its cited evidence.
3. A request to keep, revise, or withdraw the original conclusion.

The specialist returns a new finding. It does not overwrite the first-round
record. No third round is allowed.

### 7.5 Final Hybrid Decision

The coordinator produces one decision:

```text
agreement
conflict
agent_leads
fallback
```

DiagOps code, not free-form coordinator text, assigns the final
`decision_status`. The coordinator may propose a cause, summary, and
uncertainty, but the runtime validates them and applies these fixed V7
definitions:

1. The baseline is the deterministic analyzer's top hypothesis.
2. A baseline is low confidence when its confidence is less than `0.5`.
3. An active specialist conclusion is that specialist's latest valid
   `root_cause` finding after any review round, with a non-`unknown` cause,
   confidence of at least `0.5`, and valid evidence references.
4. Support counts distinct specialists, not the number of findings.
5. An explicit active `contradiction` finding remains a contradiction even if
   no alternative root-cause finding is present.

Apply the statuses in this order:

1. `agreement`: the Agent run is complete; the baseline is non-`unknown` and
   not low confidence; at least two active specialist conclusions support the
   baseline cause; no active specialist conclusion supports another cause; and
   no active contradiction targets the baseline cause. The deterministic
   result remains formal and is marked as Agent-confirmed.
2. `conflict`: the non-low-confidence baseline differs from any active
   specialist conclusion, two active specialist conclusions name different
   causes, or an active contradiction targets the otherwise selected cause.
   All conflicting conclusions are shown and human confirmation is required.
3. `agent_leads`: the baseline is `unknown` or low confidence; at least two
   active specialist conclusions support the same non-`unknown` cause; that
   cause has more specialist support than every competing cause; and no active
   contradiction targets it. It is the main candidate, not a confirmed root
   cause.
4. `fallback`: none of the preceding rules applies, or the SDK path is skipped
   or fails to produce a valid review. The deterministic result remains
   available.

No model may silently convert `conflict` into `agreement` or label
`agent_leads` as confirmed. A partial run cannot produce `agreement`.

## 8. Agent Responsibilities

### 8.1 LogAgent

Input:

1. Log evidence.
2. Log provider error or partial-result evidence.

Responsibilities:

1. Identify error patterns, exception changes, and timing correlations.
2. Distinguish symptoms from root-cause indicators.
3. Cite only supplied evidence IDs.

### 8.2 MetricAgent

Input:

1. Metric evidence.
2. Metric provider error or partial-result evidence.

Responsibilities:

1. Identify traffic, latency, error-rate, saturation, availability, database,
   and instance anomalies.
2. Record evidence that contradicts a suspected cause.
3. Cite only supplied evidence IDs.

### 8.3 DeploymentAgent

Input:

1. Deployment evidence.
2. Deployment provider error or partial-result evidence.

Responsibilities:

1. Evaluate deployment timing and changed version correlation.
2. Distinguish a relevant deployment from an unrelated or missing change.
3. Cite only supplied evidence IDs.

### 8.4 CoordinatorAgent

Responsibilities:

1. Invoke every specialist in the first round.
2. Compare findings with the deterministic baseline.
3. Select only conflicting specialists for the review round.
4. Produce a structured hybrid decision with evidence and uncertainty.

The coordinator does not execute provider or production tools.

## 9. Domain Model Changes

### 9.1 Shared Enums

Add:

```text
AgentExecutionLayer = custom | openai_agents_sdk
CoordinationDecisionStatus = agreement | conflict | agent_leads | fallback
MultiAgentRunStatus = completed | partial | failed | skipped
```

### 9.2 AgentFinding

Add backward-compatible fields:

```text
execution_layer: AgentExecutionLayer = custom
analysis_round: 1 | 2 = 1
revises_finding_id: str | null = null
```

Validation:

1. Round 1 cannot set `revises_finding_id`.
2. Round 2 must reference an existing round-1 finding from the same
   investigation and agent.
3. Non-gap findings require one or more existing evidence IDs.
4. The runtime, not the model, sets ID, investigation ID, agent name, execution
   layer, round, revision link, and created time.
5. Unknown IDs, enum values, and non-finite confidence are rejected.

### 9.3 DiagnosisTask And AgentExecution

Add backward-compatible fields:

```text
execution_layer: AgentExecutionLayer = custom
analysis_round: 1 | 2 | null = null
```

Observed coordinator and specialist calls are appended to the existing plan and
execution timeline. Existing V4 tasks and executions remain `custom`.

### 9.4 CoordinationReview

Add backward-compatible fields:

```text
execution_layer: AgentExecutionLayer = custom
run_status: MultiAgentRunStatus = completed
decision_status: CoordinationDecisionStatus | null
baseline_cause_type: CauseType | null
selected_cause_type: CauseType | null
summary: str = ""
uncertainty: str = ""
```

`run_status` has fixed meanings across the persisted review and the workbench
run summary:

1. `completed`: all three first-round specialists, every required review-round
   specialist, and the final coordinator call completed with valid output.
2. `partial`: at least one required Agent call failed, but enough valid output
   remained to persist a V7 review.
3. `failed`: the enabled runtime started but could not produce a valid V7
   review.
4. `skipped`: the runtime was enabled for the investigation but lacked required
   local configuration, so no SDK call started.

A persisted V7 `CoordinationReview` can be `completed` or `partial`. For
`failed` or `skipped`, no V7 review replaces the V5 review; those states appear
only in the workbench run summary derived from Agent executions and runtime
configuration.

The existing candidate list, finding references, and evidence references remain
the structured source for the workbench.

Rule-generated V5 findings remain stored. SDK findings are appended with
`execution_layer=openai_agents_sdk`.

The V7 coordination review replaces the current review only after all findings,
candidates, and evidence references validate. A failed or invalid SDK run
leaves the V5 review unchanged.

## 10. Persistence And API

V7 reuses existing SQLite JSON payload tables and repository patterns.

No new table is required for:

1. Diagnosis tasks.
2. Agent executions.
3. Agent findings.
4. Coordination review.

Existing payloads without V7 fields deserialize with safe defaults.

V7 adds no endpoint. Reuse:

```text
GET /investigations/{id}/plan
GET /investigations/{id}/tasks
GET /investigations/{id}/agent-executions
GET /investigations/{id}/context
GET /investigations/{id}/agent-findings
GET /investigations/{id}/coordination-review
GET /investigations/{id}/rca-workbench
```

The workbench response additively exposes the complete coordination review,
Agent executions, and this run summary while retaining its existing
`candidates` field:

```text
multi_agent_run: MultiAgentRunSummary | null
MultiAgentRunSummary.status: MultiAgentRunStatus
MultiAgentRunSummary.failure_reason: str | null
```

On a failed or skipped SDK run, the persisted V5 review remains the
coordination review. The run summary is derived from the V7 review and the
latest `openai_agents_sdk` coordinator execution; its failure reason is
redacted. When V7 is disabled and no run is attempted, `multi_agent_run` is
`null`. Existing clients that ignore the additive fields continue to work.

Persistence ordering:

1. Persist each validated specialist finding and execution.
2. Build the coordination review in memory.
3. Validate every finding and evidence reference.
4. Persist the complete review only after validation succeeds.

## 11. Configuration And Credentials

Add:

```text
agents.enabled: false
agents.model: null
agents.max_turns: 8
agents.timeout_seconds: 60
```

Environment overrides:

```text
DIAGOPS_AGENTS_ENABLED
DIAGOPS_AGENTS_MODEL
DIAGOPS_AGENTS_MAX_TURNS
DIAGOPS_AGENTS_TIMEOUT_SECONDS
```

Rules:

1. Real agents are disabled by default.
2. Enabling agents requires an explicit model and `OPENAI_API_KEY` in the local
   process environment.
3. The API key is never accepted through an API request, frontend field, YAML
   file, database record, prompt, or log.
4. Automated tests inject model substitutes and make no network call.
5. The live acceptance command checks only that the key exists; it never prints
   the key.
6. V6 `react` settings remain separate and backward compatible.

The implementation plan must recheck current official OpenAI model guidance
before choosing the model used for live acceptance. The spec does not hard-code
a time-sensitive model name.

## 12. User-Visible Behavior

The existing investigation detail page adds a `混合 RCA 裁决` section. It shows:

1. Deterministic RCA baseline and confidence.
2. First-round findings grouped by specialist.
3. Evidence IDs for every finding.
4. Conflicts that triggered a review round.
5. Round-1 and round-2 findings without overwriting history.
6. Coordinator decision, summary, and uncertainty.
7. SDK status and visible fallback reason.

Final report generation occurs after the V7 runtime attempt. It adds the same
decision wording and evidence references. If the runtime fails, the report uses
the redacted coordinator execution reason and the deterministic fallback; it
does not claim that a V7 review was persisted.

Report wording:

1. `agreement`: `多 Agent 复核一致`.
2. `conflict`: `存在冲突，需要人工确认`.
3. `agent_leads`: `多 Agent 主要候选，尚未确认`.
4. `fallback`: `多 Agent 复核未完成，以下为确定性 RCA 结果`.

The UI must separate facts, agent inference, deterministic inference,
recommendations, and uncertainty.

The UI does not show raw chain-of-thought. It shows structured findings,
evidence links, concise rationale, revision history, and execution status.

No new page or graph library is added.

## 13. Error Handling And Degradation

### 13.1 Configuration And SDK Failure

1. A disabled runtime records no V7 task, execution, or SDK call and preserves
   current behavior.
2. Missing key or model records the Agent path as skipped without leaking
   credential details; this creates a skipped local coordinator execution but
   no SDK call. A configured runtime that starts and then errors is failed.
3. Authentication, rate-limit, timeout, quota, and SDK errors remain visible in
   a redacted execution error.
4. SDK errors never fail the base investigation.

### 13.2 Specialist Failure

1. One failed specialist produces a partial run.
2. Valid findings from other specialists remain visible.
3. A partial run cannot produce a high-confidence agent confirmation.
4. A failed specialist does not enter an unlimited retry loop.

### 13.3 Coordinator Failure

1. Valid specialist findings remain persisted.
2. No incomplete coordination review replaces the V5 review.
3. The investigation returns the deterministic RCA with visible fallback
   wording.

### 13.4 Invalid Output

Reject:

1. Invalid JSON or schema output.
2. Unknown evidence or finding IDs.
3. Unsupported agent names, cause types, statuses, or rounds.
4. Non-finite confidence.
5. Round-2 findings without a valid round-1 revision link.

Invalid output cannot change hypotheses, the base report, actions, approvals,
or verification results.

## 14. Safety And Data Handling

V7 remains read-only with respect to production systems.

Structural enforcement:

1. Specialists are agents-as-tools but receive no production mutation tools.
2. The coordinator receives no shell, SSH, browser, filesystem, generic HTTP,
   rollback, restart, scale, deployment mutation, or configuration tool.
3. Agent findings are diagnostic records, not executed actions.
4. Existing recommendation approval and verification rules remain unchanged.

External input boundary:

1. Logs are diagnostic evidence whose trustworthiness is evaluated; they are
   not allowed to control Agent behavior.
2. User-controlled request values, third-party responses, and service output may
   appear inside logs and can contain prompt-injection text.
3. Evidence is delimited and labelled as untrusted data in Agent input.
4. Evidence content cannot introduce tools or replace system instructions.
5. A prompt-injection fixture must not change the Agent tool set or decision
   rules.

Data minimization:

1. Send evidence IDs, compact summaries, timestamps, provider state, and only
   the structured fields needed by each specialist.
2. Do not send complete raw payloads by default.
3. Redact known credential, token, connection-string, and personal-data shapes
   before model submission.
4. Do not log API keys, full prompts, or full model responses at normal logging
   levels.
5. Configure SDK tracing without sensitive model/tool payload capture.

## 15. Automated Test And Acceptance Criteria

### 15.1 Runtime Tests With Model Substitutes

Prove:

1. The runtime is constructed only when enabled and configured.
2. The coordinator invokes all three specialists in round 1.
3. Round-1 specialist inputs exclude baseline and peer findings.
4. The coordinator receives the validated first-round findings and baseline.
5. Only conflicting specialists enter round 2.
6. No specialist runs more than twice.
7. Agreement, conflict, agent-leads, and fallback decisions follow the defined
   rules.
8. SDK/model failure preserves a completed deterministic investigation.

### 15.2 Evidence And Safety Tests

1. Every non-gap finding evidence ID resolves to investigation evidence.
2. Every candidate finding and evidence ID resolves.
3. Unknown IDs are rejected before persistence.
4. Provider failed and partial states remain visible.
5. A log containing instructions to roll back, run a command, or ignore system
   rules remains evidence text and cannot alter Agent permissions.
6. No mutation tool exists in the SDK run.
7. Sensitive sample fields are redacted before model submission.

### 15.3 Persistence And API Tests

1. All additive fields round-trip through memory and SQLite.
2. Old records deserialize with backward-compatible defaults.
3. Round-1 and round-2 findings remain separately queryable.
4. Failed V7 runs leave the V5 review intact.
5. Existing Agent process and workbench endpoints expose the new fields.
6. Unknown investigations remain 404.

### 15.4 Frontend Tests

1. The mixed-decision section renders all four decision statuses.
2. First-round and revised findings are distinguishable.
3. Evidence IDs, conflict, uncertainty, and fallback reason are visible.
4. The UI does not claim an Agent candidate is confirmed.
5. The UI does not claim rollback, restart, scale, SSH, configuration change,
   or repair was executed.
6. Frontend build passes.

### 15.5 Standard Verification

```powershell
uv run ruff check .
uv run pytest -v
Set-Location frontend
npm.cmd run build
```

## 16. Real OpenAI Agent Reliability Gate

V7 is not declared reliable after only model-substitute tests.

After implementation and standard verification:

1. Ask the user to configure `OPENAI_API_KEY` in the local process environment.
2. Do not ask the user to paste the key into chat, code, YAML, or a committed
   file.
3. Use only existing simulated/golden incident data, not production logs.
4. Run each of the five golden RCA cases three times: 15 investigations total.
5. Record model identifier, duration, token usage, and estimated cost for each
   run.

Reliability thresholds:

1. 15/15 investigations finish without crashing and return a valid review or
   explicit fallback.
2. At least 14/15 finish a real multi-agent review rather than fallback.
3. Evidence-reference validity is 100%.
4. Prompt injection or unsupported production action succeeds 0 times.
5. The multi-agent main candidate matches the expected cause in at least 12/15
   runs.
6. Each individual golden case is correct in at least 2/3 runs.
7. The hybrid decision never labels a wrong agent conclusion as confirmed; it
   must preserve the deterministic result or return conflict.

Latency, token usage, and estimated cost are reported but have no V7 hard limit.

Failure to meet these thresholds means the SDK is connected but the real Agent
path is not yet reliable. V7 cannot be declared complete until the blocking
reliability failures are resolved or the user explicitly revises the gate.

## 17. Compatibility And Rollout

V7 is additive and default-off.

1. Existing V1-V6 APIs remain available.
2. Existing investigations and V6 ReAct traces remain readable.
3. Existing provider collection, deterministic hypotheses, reports, actions,
   approvals, and verification behavior remain valid.
4. Disabled V7 behavior matches the current baseline.
5. Failed V7 behavior produces visible fallback without losing base results.
6. The first rollout uses local simulated cases and explicit configuration.
7. V7 introduces no production automation.

Rollout order:

1. Domain and model-substitute tests.
2. Default-off local SDK integration.
3. Persistence, API, and frontend integration.
4. Standard verification.
5. User-configured key and 15-run reliability gate.
6. Review metrics and resolve blocking failures before completion.

## 18. When To Consider LangGraph

LangGraph remains deferred until DiagOps needs at least one of:

1. Investigation resume after process restart.
2. Long-running branches beyond a request/job lifetime.
3. Independent branch retry and checkpoint semantics.
4. Human interruption that pauses and later resumes the same run.
5. Persisted graph state that current investigation/task records cannot
   represent cleanly.

Do not add LangGraph only to draw the task graph or implement one bounded review
round.

## 19. Acceptance Summary

V7 is complete when:

1. OpenAI Agents SDK runs one coordinator and three model-backed specialists.
2. All evidence is collected before Agent analysis.
3. First-round findings are independent.
4. Conflicting specialists review at most once.
5. The hybrid decision follows agreement, conflict, agent-leads, and fallback
   rules.
6. Every accepted conclusion resolves to persisted evidence.
7. Agent failure never removes the deterministic RCA result.
8. The existing report and workbench clearly show baseline, Agent findings,
   revisions, conflict, uncertainty, and fallback.
9. No production mutation capability or raw chain-of-thought persistence is
   introduced.
10. Ruff, all tests, frontend build, and the real 15-run reliability gate pass.
