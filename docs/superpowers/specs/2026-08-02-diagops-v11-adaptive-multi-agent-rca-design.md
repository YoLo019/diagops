# DiagOps V11 Adaptive Multi-Agent RCA Design

Status: `approved`

Date: 2026-08-02

Risk: Full — this iteration changes diagnostic authority, durable workflow
semantics, public projections, failure behavior, and benchmark claims.

## 1. Context and decision

V10.1 did not improve the frozen OpenRCA paired score: candidate and baseline
both produced official partial score `0.025`. The candidate changed 31 of 33
telemetry-present predictions without increasing correct matches. More
importantly, the official deterministic benchmark mode never executed
`AgentsRcaRuntime`; the production path still generated hypotheses through
`RcaAnalyzer`, and `DiagnosisOrchestrator` replaced Agent-produced root causes
with persisted deterministic root causes before final validation.

The current product is therefore an Agent shell around a rule-authoritative RCA
core. V11 changes that authority boundary. The Lead Agent selects the next
information gap and investigation strategy, bounded Investigator Agents collect
and interpret read-only evidence, a Critic tests causal sufficiency and
counterevidence, and the Lead adjudicates the final root-cause report. The
deterministic layer validates contracts, permissions, evidence references,
budgets, and unsafe output; it cannot add, rank, or replace a root cause.

Selected design: **scheme B, adaptive evidence investigation**. V11 retains two
useful properties from debate-style scheme C—isolated first-pass investigations
and explicit critique—but does not use majority voting. Fixed modality
specialists remain readable only as historical V5–V10 records and compatibility
fixtures; they are not the V11 production reasoning topology.

## 2. Confirmed Requirements Brief

### 2.1 Goal

Build a general-purpose, application-service SRE diagnostic system whose final
root-cause decision primarily comes from bounded Multi-Agent reasoning over
provider-neutral evidence, rather than a fixed cause taxonomy or
benchmark-specific rules.

### 2.2 Users and outcomes

- An SRE submits or receives an incident and gets an evidence-linked diagnosis,
  affected entity, failure mechanism, uncertainty, alternatives, and safe next
  checks.
- An operator can inspect which questions were investigated, which tools were
  called, what evidence was accepted or contradicted, why the Critic rejected or
  accepted a candidate, and why the run stopped.
- A reviewer can replay a durable run without silently rerunning model or tool
  calls, and can distinguish complete, partial, inconclusive, failed, cancelled,
  and timed-out results.
- A local benchmark custodian can compare single-Agent and Multi-Agent systems
  on one frozen held-out dataset without adding dataset-specific prompts or
  cause rules to the production path.

### 2.3 Confirmed constraints

- Production tools are read-only and dynamically callable within an allowlist.
- The workflow is asynchronous and bounded to a default hard deadline of 120
  seconds per investigation.
- At most three Investigator instances run concurrently; there are at most two
  investigation rounds, and the Critic may request only one supplemental round.
- A hypothesis is optional. Agents may instead use topology/trace backtracking,
  first-failure analysis, change correlation, peer or baseline comparison,
  verified-incident retrieval, or another evidence-bounded strategy.
- The final report must be evidence-bounded. Unsupported certainty is invalid;
  insufficient evidence produces `inconclusive` rather than a fabricated cause.
- Production-mode Multi-Agent billable tokens must not exceed three times the
  paired single-Agent total. Equal-total-token ablation is reported separately.
- OpenRCA remains a historical compatibility regression, not the target that
  defines the architecture or production taxonomy.
- V11 is a personal, local-first project. Tool development, evidence replay,
  deterministic orchestration checks, and local integration cannot require a
  cloud account, production cluster, or externally managed telemetry service.
  Formal Agent accuracy runs may call one frozen local or remote LLM endpoint;
  telemetry sources, incident packages, and answer labels remain local, and
  credentials are never persisted in artifacts. A remote model receives only
  the bounded, redacted evidence selected for inference and is not an evidence
  Provider or external telemetry dependency.
- Model access supports official OpenAI, the existing DeepSeek compatibility
  path, and one generic `openai_compatible` Chat Completions path with a
  configurable `base_url`. A compatible endpoint is eligible for formal scoring
  only after its tool-call, structured-result, usage, bounded-response, and
  configured-parallelism capabilities pass certification. Runtime timeout and
  cancellation safety are verified locally rather than claimed as remote-server
  capabilities.
- Every Agent-facing tool must have a deterministic local offline Provider and
  a checked-in or prepared incident package. Missing live integrations are
  represented as explicit evidence gaps, never replaced by mock production data.
- Tempo is the only real trace backend in V11 and runs locally through a pinned
  Docker image. File trace replay remains the mandatory no-Docker path.
- MCP, Kubernetes, SSH, shell execution, and write-capable infrastructure tools
  are outside V11.

## 3. Verified baseline and assumptions

### 3.1 Source-verified baseline

| Surface | Current behavior | V11 implication |
| --- | --- | --- |
| Agent topology | `AgentName` is fixed to Log, Metric, and Deployment; V9 runs them explicitly in parallel | Replace the fixed production topology; retain legacy enum values for reload compatibility |
| Agent output | `_SpecialistDraft` and `RootCauseCandidate` require `CauseType` when a cause is named | Make failure mechanism and affected entity free text; legacy `CauseType` becomes optional compatibility metadata |
| Coordinator | Synthesis prompt explicitly asks to synthesize the deterministic baseline | Lead must synthesize Agent findings and evidence; deterministic hypotheses are optional clues only |
| Authority | `DiagnosisOrchestrator` overwrites validated Agent roots with persisted deterministic roots | Remove root replacement; validator may reject but not rewrite diagnostic conclusions |
| Report | `ReportGenerator` requires at least one deterministic `Hypothesis` and leads with it | Report from the V11 diagnosis result; render legacy hypotheses only when loading historical runs |
| Runtime | Durable phases, attempts, events, checkpoints, budgets, leases, cancellation, and replay already exist; the current handlers themselves execute deterministic RCA, fixed specialists, hybrid fallback, hypothesis-led reports, and unconditional completion | Reuse the coordinator/store/writer primitives through a versioned phase profile; do not reuse or rename legacy diagnostic handlers as V11 semantics |
| Persistence | Schema V6 stores tasks, executions, findings, reviews, traces, and runtime objects in versioned JSON payload rows | Prefer additive payload fields and one schema migration only when a physical index/column is proven necessary |
| Tools | The registry exposes logs, metrics, deployments, catalog, Prometheus, dependencies, and memory; `query_prometheus` is duplicated outside the ReAct allowlist | Publish one V11 Agent-facing manifest from the registry and keep vendor aliases internal |
| Missing evidence | There is no service trace provider/query/evidence kind; production dependency has no real configured Provider; related alerts have no tool; `lookup_memory` returns empty success | Add provider-neutral trace/runtime/verified-memory contracts and make every exposed tool return real local evidence or an explicit failure |
| Local telemetry | `D:\data\OpenRCA` contains Bank, Market, and Telecom metrics, logs, and `trace_span.csv` files; repository fixtures cover only a subset | Prepare opaque local incident packages and use the same tool contracts for offline development and scoring |
| Local runtime | Docker 29.2.1 and Compose 5.1.0 are installed on the development host; the Docker Desktop daemon is not always running | Offline acceptance cannot depend on Docker; the Tempo integration gate starts from an explicit Docker prerequisite |
| Skills and MCP | No diagnostic Skill or MCP subsystem is implemented | Add four versioned prompt-strategy records only; do not add a Skill engine or MCP adapter in V11 |
| API/UI | Workbench exposes findings, candidates, evidence, executions, tool calls, review, and run summary | Extend these projections additively; preserve historical responses |
| OpenRCA | Official deterministic runner does not instantiate Agents; projection only maps final attributions | Add a V11 Agent mode through the same core; keep deterministic mode as a labeled historical regression |

### 3.2 Assumptions to validate during implementation

- Current JSON payload persistence can reload additive Pydantic fields from V3–V6
  supported databases without a physical table change.
- The existing provider/tool registry, redaction, idempotency, and evidence
  ledger can be extended without a second tool framework. The missing trace,
  runtime-state, related-alert, dependency, and verified-memory semantics are
  explicit V11 work, not assumptions.
- The selected model supports structured output and tool calls within the
  120-second bound. Model/provider certification is a deployment prerequisite,
  not a reason to weaken output validation.
- Current scored reasoning may use a supported OpenAI or DeepSeek model, or a
  certified OpenAI-compatible Chat Completions endpoint. Network and a runtime
  credential are allowed when required by the selected endpoint. A fake model
  certifies contracts and orchestration only; a real local or remote compatible
  model may produce R15 evidence only after capability certification.
- RCAEval RE2 can be repackaged into development, validation, and final held-out
  partitions without shipping answers or benchmark labels into the runtime.

## 4. Risk obligations

| ID | Obligation | Required handling |
| --- | --- | --- |
| O1 | Diagnostic authority inversion | One explicit function owns final adjudication. Validators may accept, reject, or mark inconclusive; they cannot reorder, inject, or replace root causes |
| O2 | Historical compatibility | V3–V6 databases, fixed-specialist records, deterministic OpenRCA artifacts, and current API fields remain readable and labeled `legacy` where needed |
| O3 | Partial provider/model failure | Preserve successful evidence and executions; expose `partial` only when a valid report remains possible, otherwise `inconclusive` or `failed` |
| O4 | Timeout and cancellation | Propagate cancellation, cancel sibling tasks, persist terminal tool/model events, checkpoint only committed state, and never report completion after deadline |
| O5 | Retry and idempotency | Retry a transient model/tool transport failure at most once within the same budget; reuse successful idempotent tool results on resume |
| O6 | Concurrency and SQLite | Keep the existing single-writer transactional path and per-run parallel gate; never let parallel Agents write business projections directly |
| O7 | Unsafe or untrusted evidence | Redact at ingress and egress; delimit evidence; treat evidence text as data, ignore embedded instructions, and reject non-allowlisted tool requests |
| O8 | Evidence fabrication | Every causal claim and alternative must cite persisted evidence IDs; cited IDs must belong to the investigation and support the stated entity/time scope |
| O9 | Benchmark leakage | Production prompts cannot contain dataset name, task labels, expected-answer vocabulary, scenario tokens, or per-dataset cause mappings |
| O10 | Public contract drift | API additions are additive; removed or reinterpreted fields require explicit versioning and frontend compatibility tests |
| O11 | Cost and runaway loops | Enforce global tokens, tool calls, turns, investigators, rounds, wall time, and per-call timeouts before each action |
| O12 | Replay truthfulness | Replay consumes frozen inputs and committed outputs; a replay that invokes a live model/tool is invalid unless explicitly created as a new live rerun |
| O13 | Cleanup | On failure/cancel, terminalize pending model/tool executions and release leases; retain audit records and do not delete evidence |
| O14 | Over-generalization | V11 covers application-service RCA with the current providers; host/kernel/network/storage specialization is future scope and cannot be claimed as implemented |
| O15 | Personal-project reproducibility | After dependencies and datasets are prepared, a clean local checkout can run every tool and deterministic Agent-orchestration acceptance without credentials, Docker, or network; scored reasoning separately declares its frozen model endpoint plus any applicable network and credential prerequisites |
| O16 | Adapter parity | Offline and Tempo profiles implement the same `query_traces` input/evidence contract and preserve path, status, order, and duration semantics after documented normalization; backend-generated IDs and ordering need not be byte-identical |
| O17 | Synthetic-data overclaim | File replay and Docker-local telemetry are labeled local evaluation evidence; they cannot be described as production environment validation |
| O18 | Local holdout isolation | The runtime receives only opaque telemetry packages; answer labels are mounted or opened only by the evaluator after prediction freeze, even when both run on one host |
| O19 | Model endpoint compatibility | Treat OpenAI-compatible as a certified capability contract, not a vendor claim; fail closed when tool calls, structured results, token usage, bounded response, or configured parallelism are absent, and verify client-side timeout/cancellation independently |
| O20 | Legacy semantic bleed | A V11 run cannot invoke deterministic RCA, fixed-specialist iteration, hybrid CauseType arbitration, hypothesis-led reporting/action planning, or an unversioned direct orchestrator entry; executable paths are selected by the immutable run contract and verified with forbidden-call sentinels |

## 5. Affected contract inventory

| Contract | Current consumers | V11 change | Compatibility rule |
| --- | --- | --- | --- |
| `InvestigationStrategy` | API, runtime runs, orchestrator, frontend, benchmarks | `adaptive` becomes the production default after certification; `fixed` remains legacy | Existing serialized values unchanged |
| `DiagnosisTask` | repositories, checkpoints, task API, workbench | Reuse `description` as the objective and `analysis_round` as the round; add only optional `strategy`, `evidence_scope`, and `expected_discriminator` fields | Old payloads validate with defaults |
| Task/execution enums | runtime, repositories, API/UI, acceptance | Add generic investigation task type, V11 Lead/Critic step kinds, and `cancelled` task/execution terminal state | Existing enum values remain valid; V10 handlers use only legacy values |
| `AgentExecution` | persistence, run summary, UI, telemetry | Add Lead/Investigator/Critic actor names and new step kinds | Old actor and step values remain valid |
| `AgentFinding` | repository, review builder, report, graph API/UI | Reuse `finding_type` and `summary`; add dynamic `agent_instance_id`, `task_id`, `runtime_run_id`, free-text `affected_entity`, `failure_mechanism`, counterevidence, and optional supplemental-assessment ownership; legacy `agent_name`/`CauseType` remain readable | V11 round-two findings are not forced to revise a round-one finding; legacy revision rules remain version-scoped and old rows are not rewritten |
| `RootCauseCandidate` | review, graph, frontend | Generalize beyond `CauseType`; carry affected entity, mechanism, evidence, counterevidence, uncertainty, and rank | Legacy candidate deserialization supported |
| `CoordinationReview` | repository, reports, API/UI, benchmark projection | Becomes Critic assessment plus Lead adjudication record; roots are Agent-authored | Existing fields retained; new fields additive |
| `MultiAgentRunSummary` | investigation record, API/UI, acceptance | Add diagnostic status, stop reason, rounds, investigator count, token totals, and authority mode | Absent fields imply legacy semantics |
| `ReActTrace` | trace API, persistence | Persist structured action summaries and tool observations, not hidden chain-of-thought | Historical `assistant_text` remains readable but V11 writers omit private reasoning |
| Runtime phases | manager, phase executor, replay, events, checkpoints | Keep the existing engine but add a `v11` phase profile and additive V11 phase values; phase order and handlers are selected by execution contract version | Legacy phase values retain their original meaning; they are never aliases for Lead/Investigator/Critic work |
| Runtime run identity | create API, config, store, coordinator, checkpoints | Persist immutable `execution_contract_version`, `authority_mode`, and validated JSON `execution_contract` as the sole budget/deadline source | V3–V6 runs migrate to `v10_legacy`/`legacy_deterministic` with a bounded legacy contract; V11 handlers never resume them |
| Runtime resume state | store validation, checkpoint, phase executor | Continue tracking committed evidence/findings/review/report IDs and remaining budgets | Budget accounting remains monotonic across resume |
| Runtime frozen projection/diff | replay, recovery, audit API | Include V11 candidates, entity/mechanism/time scope, Critic assessments, authority, diagnostic status, and usage | V10 projections remain readable through legacy diff logic |
| Evidence providers and kinds | tools, evidence ledger, repository, API, report, benchmarks | Add trace, runtime-state, and verified-incident providers and bounded evidence kinds; expose related alerts through a tool | Existing serialized provider/kind values remain valid |
| Tool queries | provider tools, adaptive session, benchmark adapters | Add bounded trace, runtime-state, related-alert, and verified-memory queries; keep vendor details behind Providers | Old query models and tool names remain valid |
| Provider/tool registry | Agents runtime, adaptive session, acceptance | Add a backward-compatible `ToolSpec` exposure value and publish one filtered nine-tool V11 manifest to all Investigators; Lead and Critic do not call evidence tools; `query_prometheus` is explicitly internal | Existing `list_specs()` and legacy handlers remain valid; V11 uses only `list_agent_specs()` and also verifies `read_only` at invocation |
| Diagnostic skills | Lead prompt, execution contract, benchmark artifacts | Add four versioned data-only strategy records with required tools, stop conditions, and output expectations | No executable plugin loader, discovery service, or new persistence table |
| `MemoryItem` and human verification | memory store, feedback API, investigation repository, tools | Add explicit verified-outcome metadata and a guarded human transition; free-form feedback/tags remain unverified | Existing memory payloads load with `verification_status="unverified"`; no bulk promotion |
| Local incident packages | file Providers, local acceptance, benchmark runner | Normalize metrics, logs, spans, catalog, changes, runtime state, alerts, and verified incidents under opaque case IDs | Labels are evaluator-only and never enter Provider payloads or Agent context |
| Local Tempo profile | Docker Compose, OTLP replay, Tempo Provider, acceptance | Replay locally prepared spans with time-shifted timestamps into one pinned `grafana/otel-lgtm` image and query Tempo HTTP | Optional at runtime but mandatory to certify `TempoTraceProvider`; file Provider remains the default |
| Model provider settings and adapter | config loader, container, Agents runtime, runtime API, benchmark harness | Add `openai_compatible`, configurable `base_url`, one secret environment variable, Chat Completions adaptation, and capability certification | Official OpenAI keeps Responses; DeepSeek remains readable and routes through the compatible adapter without changing serialized provider values |
| Report/API/UI | operators and frontend | Present final diagnosis, alternatives, critique, evidence graph, gaps, status, and usage; deterministic hypothesis is no longer the headline | Legacy investigations render through a compatibility projection |
| Actions/verifications/human transitions | action planner, verification updates, approval API/report | Link V11 recommendations to diagnosis candidate IDs and evidence; retain optional legacy cause links | No fixed `CauseType` is required for a V11 recommendation or transition |
| Investigation summary | list API and frontend | Add diagnostic status, authority mode, top affected entity/mechanism, and confidence | Keep `top_cause_type` as a deprecated legacy projection, returning `unknown` for V11 when no legacy type exists |
| Agent/runtime settings and run-create API | config loader, container, runtime API, benchmark harness | Add immutable execution version, authority, total token/tool budgets, hard deadline, and actor/round limits; preserve historical model/prompt request fields as compatibility inputs only | Server owns execution version, authority, endpoint, certified model tuple, and V11 prompt catalog; request values may lower maxima, while model/prompt overrides must exactly match the server-approved tuple or receive 422 |
| DB schema/migrations | SQLite and in-memory repositories | Prefer payload-only extension; if a new physical contract is required, create schema V7 with migration from every supported V3–V6 state | Fresh DB and all supported upgrades produce identical manifest |
| OpenRCA runner/projector | CLI, API, evaluator, archived artifacts | Add labeled V11 Agent execution using generic root report; retain deterministic mode and projector as compatibility | No OpenRCA-specific production prompts or taxonomy |
| RCAEval adapter/evaluator | new benchmark tooling | Offline adapter maps frozen incident packages to provider-neutral evidence and scores exact service+fault Top1 | Adapter cannot modify production reasoning or see final answers during execution |
| V11 entry points | service container, runtime API, CLI, OpenRCA/RCAEval runners | Every Agent-authoritative execution creates a validated `v11` RuntimeRun before diagnostic work | Runtime-disabled or direct-orchestrator entry rejects V11; it may execute only an explicitly labeled legacy deterministic path |
| Runtime failure categories | run/events, API, replay/diff, acceptance | Add `contract_integrity` for execution-contract/projection/hash mismatch | Legacy categories remain readable; no consumer may silently map this failure to a recoverable model/provider error |

### 5.1 Reuse compatibility boundary

“Reuse” applies only to the named primitive, never to an entire module or call
path. The implementation starts from this allowlist:

| Decision | Reusable surface | V11 boundary |
| --- | --- | --- |
| Reuse unchanged | `RuntimeWriter` transaction boundary, lease fencing, single-active-live-run constraint, per-run concurrency gate, ordered event append/SSE delivery, redaction/safe-failure primitives | Behavior remains infrastructure-only and is covered by the existing regression suite |
| Reuse unchanged | Payload repositories, additive migration framework, Provider/Tool registry containers, persisted tool-call idempotency key/result lookup | V11 payloads add run ownership and new values; no new table family or registry framework is introduced |
| Reuse through versioned adapter | Runtime coordinator, checkpoint/resume, phase preconditions, frozen projection, replay/diff, API/UI projections | Select a V10 or V11 schema/profile from immutable `execution_contract_version`; never infer semantics from `adaptive`, current settings, or a phase name alone |
| Reuse through versioned adapter | OpenAI Agents SDK model lifecycle hooks, timeout/cancellation cleanup, token accounting, provider model adapters | The old `AgentsRcaRuntime` orchestration is legacy because its signatures and loops require deterministic hypotheses and fixed `AgentName`; V11 reuses only these bounded transport/lifecycle primitives |
| Reuse through versioned adapter | Report, action, human-transition, graph, summary, and benchmark projection surfaces | Dispatch to V11 candidate contracts when `authority_mode="agent"`; retain the old CauseType/hypothesis renderer only for legacy data |
| Do not reuse in V11 authority path | `RcaAnalyzer`, `validate_hypotheses`, `deterministic_rca`, `_record_v5_findings`, `_record_v5_review`, `build_coordination_review`, `build_hybrid_coordination_review`, fixed `for name in AgentName` loops | These functions may serve `v10_legacy` only and cannot provide a candidate, fallback, review, or validation input to V11 |
| Do not reuse in V11 authority path | `_validate_v7_result` root replacement/hybrid arbitration, old synthesis prompt, old CauseType semantic validator, hypothesis-led `ReportGenerator.generate`, CauseType `ActionPlanner.plan` | V11 validators may reject or mark inconclusive but cannot manufacture, reorder, coerce, or replace Agent conclusions |
| Do not reuse as reasoning | Direct `DiagnosisOrchestrator.run`, deterministic OpenRCA runner/projector, hard-coded `READ_ONLY_REACT_TOOLS`/`TOOLS_BY_AGENT` manifests | V11 enters through a V11 RuntimeRun and the registry-derived manifest; compatibility code remains callable only under an explicit legacy contract |

The V11 phase executor is a new profile inside the existing durable engine, not a
second workflow engine. It receives the same writer, store, lease, event, budget,
and cancellation primitives. A forbidden-call test replaces every legacy
diagnostic function above with a sentinel that raises; complete, partial, and
inconclusive V11 scenarios must still finish with zero sentinel calls. A second
entry-point test covers service, API, CLI, OpenRCA, and RCAEval paths and proves
that none can return `authority_mode="agent"` without a persisted V11 RuntimeRun.

Existing investigation tables remain the latest business projection; frozen
runtime projections remain the historical per-run record. `InvestigationRecord`
adds payload-only `active_runtime_run_id`. Every V11 plan, task, execution,
finding, review, report, run summary, recommended action, and verification
suggestion carries the owning `runtime_run_id` in its JSON payload. A V11 commit
rejects mixed or missing run ownership.

The V11 `INTAKE` commit performs one atomic projection activation through the
existing `RuntimeWriter`: it verifies that the previous owner has no active
lease, is terminal, and has a valid frozen projection; sets
`active_runtime_run_id` to the new run; clears the old latest plan, tasks,
context/tool/Agent rows, review/ReAct trace, provider/specialist results,
evidence, hypotheses, report, actions, verifications, and run summary; then
commits the intake checkpoint. Stable incident identity and human feedback are
preserved. A crash before the transaction changes nothing; a crash after it
resumes from the new owner. Later phase commits require their run ID to equal
`active_runtime_run_id`. This activation is a new `BusinessMutation` operation,
not a table or parallel repository.

A sequential V11 rerun can activate only after the previous projection is
frozen. A legacy investigation with existing unowned diagnostic projections is
never cleared or upgraded in place: an explicit V11 rerun creates a new linked
`InvestigationRecord` with the same redacted incident input and an optional
`source_investigation_id`, leaving the legacy record intact. Empty fresh
investigations may activate their first V11 run directly. Legacy payloads
without `runtime_run_id` remain readable but cannot be resumed or imported into
a V11 active projection.

Run termination is also ownership-aware. Before freezing business state, the
terminal path compares `InvestigationRecord.active_runtime_run_id` with the
terminating run ID. If they differ—for example pre-INTAKE cancellation,
execution-contract rejection, or failure of the previous-frozen gate—it must
not call the current investigation snapshot function. It writes a valid
run-owned frozen projection with `projection_state="not_activated"`, the frozen
run/input identities, terminal reason, and empty diagnosis/artifact collections.
Replay/diff displays this as a non-activated run with no business output. Only
the active owner may freeze the current latest projection. This rule applies to
all cancel, timeout, failure, lease-expiry, and recovery-rejection paths.

## 6. Scope and non-goals

### 6.1 In scope

- Adaptive Lead, up to three general Investigator instances, one Critic, and one
  Lead adjudication pass.
- Provider-neutral, bounded, read-only tool use with isolated round-one context.
- Structured tasks, findings, critique, final diagnosis, status, evidence links,
  budget usage, persistence, checkpoint/resume, replay, API, report, and workbench
  projections.
- Removal of deterministic RCA as final diagnostic authority. It may provide a
  low-trust clue or serve as an explicitly labeled fallback only when the Agent
  runtime is unavailable; a fallback result cannot be counted as V11 Agent
  accuracy.
- Benchmark-neutral evaluation on a frozen RCAEval RE2 local holdout plus OpenRCA
  compatibility regression.
- Migration and regression coverage for all currently supported schema states.
- One provider-neutral V11 tool manifest containing logs, metrics, traces,
  service catalog, dependencies, deployments, runtime state, related alerts,
  and verified incident retrieval.
- Deterministic offline Providers and local incident packages for every exposed
  tool, runnable without external services or credentials.
- One Docker-local Tempo integration using a pinned `grafana/otel-lgtm` image,
  OTLP replay, and the same `query_traces` contract as file replay.
- Four versioned, data-only diagnostic skills: first-failure timeline,
  trace backtracking, change/peer comparison, and causal falsification.
- One generic OpenAI-compatible Chat Completions model adapter using the already
  installed OpenAI SDK. It accepts a configured endpoint and model identity,
  while official OpenAI retains the Responses path and DeepSeek remains a
  backward-compatible preset.

### 6.2 Non-goals

- Automatic remediation or any production write tool.
- A general-purpose Agent framework, planner DSL, message bus, vector database,
  or new orchestration dependency.
- Fine-tuning, online learning, self-modifying prompts, or benchmark-specific
  training.
- Majority voting, unconstrained debate, open-ended reflection, or more than one
  supplemental investigation round.
- Claiming full host, kernel, network-device, database-engine, or storage-array
  diagnosis without corresponding providers and held-out evidence.
- Any external live-environment requirement, cloud account, production
  credential, Kubernetes cluster, or enterprise observability installation.
- More than one real trace backend, a production-grade observability stack, or
  production load/authentication/multitenancy certification.
- MCP client/server/gateway support, a Skill execution framework, plugin
  discovery, arbitrary shell/SSH, `kubectl exec`, or Docker mutation tools.
- A custom microservice demo platform. The local lab replays prepared telemetry;
  the upstream OpenTelemetry Demo may be used manually but is not a V11 gate.
- Automatic model-provider discovery, protocol negotiation, multi-provider
  routing, LiteLLM/Any-LLM/LangChain integration, or support for endpoints that
  expose only a URL shape without the certified V11 capabilities.
- Optimizing OpenRCA score through label dictionaries, dataset-specific cause
  types, or case routing.

## 7. Architecture and control flow

```text
Incident + initial read-only evidence
              |
              v
        Lead: plan next information gaps
              |
       1..3 independent tasks
              |
              v
  Investigator round 1 (isolated contexts)
       |        |        |
       +--- bounded read-only tools ---+
                         |
                         v
              persisted evidence ledger
                         |
                         v
        Critic: causal and reference checks
                         |
            optional one supplemental round
                         |
                         v
              Lead: final adjudication
                         |
                         v
 deterministic safety/schema/reference/budget validator
                         |
                         v
 complete | partial | inconclusive | failed | cancelled
```

### 7.1 Lead

The Lead receives the incident, service context, available evidence summaries,
tool capabilities, and remaining budgets. It returns a structured action:

- `investigate`: create one to three independent questions that can close the
  highest-value information gaps;
- `test`: create a targeted falsification or comparison task for a named
  candidate;
- `conclude`: emit a supported final diagnosis and alternatives;
- `inconclusive`: stop because the evidence cannot support a responsible cause.

The Lead chooses tasks, not fixed modalities. A task can ask for a causal trace
across logs, metrics, deployments, dependencies, catalog, and verified memory.
It may select an investigation strategy, but strategy is guidance rather than a
required hypothesis template.

### 7.2 Investigator

Round-one Investigators have identical capabilities but isolated task context.
They see the incident, their assigned question, the tool manifest, and evidence
returned through their own calls. They do not see sibling conclusions until the
Critic stage. Each returns structured findings:

- observation or correlation;
- candidate cause with affected entity and failure mechanism;
- contradiction or counterevidence;
- unresolved evidence gap.

Every non-gap finding cites evidence IDs. Tool output first enters the shared
evidence ledger through the existing validator and single-writer path; an Agent
cannot cite raw, uncommitted output.

### 7.3 Critic

The Critic sees all committed findings and evidence after round one. For each
candidate it explicitly checks:

1. temporal order and onset alignment;
2. topology or dependency reachability;
3. plausible failure mechanism;
4. blast-radius consistency;
5. symptom-versus-cause confusion;
6. counterevidence and missing expected signals;
7. credible alternatives.

It returns `accept`, `reject`, `needs_evidence`, or `inconclusive` per candidate.
Only `needs_evidence` may request supplemental tasks, and the request must name
the exact gap and expected discriminating evidence. The Lead may schedule at
most one second round. After round two, the Critic performs exactly one
reconciliation using the same assessment IDs and updated committed evidence. It
must return final `accept`, `reject`, or `inconclusive`; it cannot return
`needs_evidence` again or request a third round. The Lead adjudicates only after
this reconciliation.

### 7.4 Final adjudication and validation

The Lead receives committed findings, Critic assessments, and remaining gaps.
`conclude` emits at least one ranked diagnosis; `inconclusive` emits none. A
diagnosis contains:

- affected entity/component;
- failure mechanism/reason;
- estimated occurrence or onset time when supported;
- supporting and contradicting evidence IDs;
- concise causal explanation;
- confidence and uncertainty;
- alternatives and discriminating next checks.

The deterministic validator checks schema, safe text, permissions, budget,
investigation ownership of references, time bounds, and internal consistency.
It can reject the result or downgrade the run to `inconclusive`; it cannot alter
the rank, component, mechanism, or evidence set. This is the V11 authority
invariant.

### 7.5 Tool and Provider boundary

V11 exposes exactly this provider-neutral manifest to every Investigator:

```text
read_logs
query_metrics
query_traces
read_service_catalog
query_dependencies
read_deployments
read_runtime_state
query_related_alerts
lookup_memory
```

`query_prometheus` remains an internal compatibility alias and is not shown to
V11 Agents. `ToolSpec` adds `exposure=agent|internal` with a compatibility
default of `agent`; the Prometheus alias is explicitly `internal`.
`ToolRegistry.list_specs()` preserves the current complete view while
`list_agent_specs()` returns only read-only Agent-visible tools. The latter is
the single source for the V11 Agent manifest, invocation validation, structured
trace validation, persisted execution contract, and benchmark hashes; no second
hard-coded allowlist may drift from it. V11 rejects a call unless the selected
spec is both present in the frozen Agent manifest and `read_only=true`.

Every exposed tool has a local Provider that reads an opaque incident package.
`query_dependencies` derives dynamic edges from parent/child spans and may
augment them with static catalog edges. `lookup_memory` emits evidence only for
records with a persisted verified outcome and provenance; empty or unverified
matches are an explicit no-evidence result. Related-alert and runtime-state data
are scenario evidence, not simulated production Providers.

Provider errors, missing files, unsupported fields, and empty windows are
distinguishable. No Provider fabricates a healthy signal or silently switches to
mock data. Tool results pass through the existing schema validation, redaction,
idempotency, budget, persistence, and evidence-ledger path before an Agent can
cite them.

### 7.6 Diagnostic skills

A diagnostic skill is a versioned prompt strategy, not executable code. Each
record contains only `name`, `version`, `when_to_use`, `required_tools`, bounded
steps, expected evidence, and stop conditions. V11 defines four records:

1. `first_failure_timeline` aligns the earliest metric, log, trace, change, and
   alert onset;
2. `trace_backtracking` follows error propagation and latency critical paths to
   the last healthy boundary;
3. `change_and_peer_comparison` compares changed and healthy peers before and
   after the incident;
4. `causal_falsification` seeks counterevidence, missing expected signals, and
   discriminating alternatives.

The Lead may select zero or more applicable skills. Selection is guidance and
never forces a hypothesis or a tool call. The execution contract freezes the
complete catalog, versions, content hash, and ordering—not the later selection.
The selected `name@version` values are a validated subset persisted in the Lead
planning decision and copied to generated task strategy metadata. The V11
single-Agent control uses the same planning-action schema and persists its own
selection before tool use. Selection consumes the normal model turn and token
budget on both sides; it receives no separate free call. Single-Agent and
Multi-Agent scored runs receive the same catalog. V11 adds no Skill registry
service, dynamic code loading, filesystem discovery, or marketplace.

### 7.7 Local validation profiles

The mandatory `offline` profile reads prepared incident packages and requires
no network, credentials, Docker, or external service. A package can contain
metrics, logs, spans, catalog, changes, runtime state, related alerts, verified
incident summaries, and an incident envelope. Ground-truth labels are stored in
an evaluator-only package and are not mounted or passed to the runtime.

The `tempo_local` profile starts one pinned `grafana/otel-lgtm` container. A
bounded replay process converts local spans to OTLP, shifts all timestamps by
one constant so relative order and duration remain unchanged, sends them to the
local collector, and queries Tempo through its HTTP API. Agent queries and
normalized Evidence always use canonical incident timestamps and canonical
trace/span IDs. `TempoTraceProvider` maps canonical query bounds and IDs to the
shifted/backend values, then maps results back before scope validation and
evidence-ID generation. The replay manifest records the constant offset and any
canonical-to-backend ID mapping. Parity compares canonical scope, parent
relation, status, order, duration, and normalized payload. Evidence IDs remain
invocation-owned and are not compared across profiles; Provider identity and raw
backend ordering may differ. The image reference and
digest, Compose file, replay manifest, mappings, and tool schema are recorded in
acceptance output.

Offline tool and deterministic orchestration gates are release requirements;
formal accuracy additionally requires the frozen supported LLM configuration.
The Docker gate is also required before claiming `TempoTraceProvider` support,
but Docker is not a runtime prerequisite for the default local product.
Docker-local results prove protocol integration only, not production
authentication, multitenancy, scale, retention, or reliability.

The Tempo Gate performs a bounded preflight for daemon availability, the pinned
image digest, required ports, and disk space. A missing daemon/image or port
conflict marks R23 `blocked`, never passed or silently skipped. Each run uses a
unique Compose project name and bounded health/ingestion polling. Success,
failure, cancellation, and timeout clean up only containers, networks, and
volumes created under that project name; pre-existing Docker resources are not
touched.

### 7.8 Model endpoint boundary

Official OpenAI continues to use the current Responses adapter and explicitly
pins `https://api.openai.com/v1`; it does not inherit `OPENAI_BASE_URL`,
`OPENAI_WEBSOCKET_BASE_URL`, or an environment-supplied custom routing header.
Any custom address must select `openai_compatible`. The generic
`openai_compatible` provider uses the already installed `AsyncOpenAI` client and
`OpenAIChatCompletionsModel` with one configured `base_url`, model name, bounded
transport timeout, and retry policy. The current DeepSeek provider keeps its
serialized identity but becomes a preset of the same Chat Completions adapter;
DeepSeek-only request fields are never sent to a generic endpoint.

V11 deliberately supports one common-denominator compatible protocol rather
than automatic negotiation. The endpoint must demonstrate non-streaming Chat
Completions, function/tool calling, JSON-object output that passes the local
Pydantic contracts, input/output token usage, a response inside the live
certification deadline, and the configured parallelism. Missing token usage makes the endpoint
ineligible for formal comparison because the 3x token gate would be
unverifiable. Streaming and Responses compatibility are not required for the
generic path. Remote certification does not claim that cancelling an HTTP
request stops server-side computation; local slow-endpoint tests instead prove
client cancellation, deadline enforcement, no late commit, and transport
cleanup.

Configuration stores `provider`, `model`, and `base_url`. The generic provider
reads only `DIAGOPS_AGENTS_API_KEY`; it never falls back to an official-provider
secret. Official OpenAI and DeepSeek retain `OPENAI_API_KEY` and
`DEEPSEEK_API_KEY` respectively for backward compatibility. The runtime and
public config projection may expose only a normalized endpoint identity without
userinfo, query, fragment, or credential. Official OpenAI tracing remains
available under its existing privacy settings; SDK cloud tracing is disabled
for generic compatible endpoints so diagnostic evidence is not sent to an
unintended exporter.

Endpoint canonicalization accepts only absolute `http` or `https` URLs, rejects
userinfo, query, fragment, percent-encoded path bytes, duplicate slashes, and
`.`/`..` path segments, lowercases scheme and IDNA host, removes port 80 for
HTTP or 443 for HTTPS, preserves a non-default port, and removes trailing path
slashes. Required test vectors include
`HTTPS://EXAMPLE.COM:443/v1/ -> https://example.com/v1` and
`http://127.0.0.1:8000/v1/ -> http://127.0.0.1:8000/v1`; rejected vectors cover
every forbidden component. Run creation hashes the canonical UTF-8 URL with
SHA-256 into `endpoint_id` and freezes that ID with the model, API mode, and
certification artifact. A live
dispatch or resume recomputes the ID from server configuration and fails before
the model call when it differs; it never silently moves an existing Run to a
new endpoint. Frozen replay remains model-free and consumes committed outputs.

Capability certification is a separate versioned artifact, not the existing
provider reliability record. `model-capability-v1` stores provider, model,
API mode, `endpoint_id`, adapter version, OpenAI/Agents SDK versions, tested
parallelism, capability-manifest hash, code revision, result, and artifact hash;
it stores no URL, credential, prompt, evidence, or response body. `artifact_hash`
is SHA-256 over UTF-8 canonical JSON with sorted keys, compact separators, no
NaN, and the `artifact_hash` field omitted, avoiding a self-referential hash.
The config API adds a distinct `capability_certification_status`. Existing
reliability artifacts remain readable but can never satisfy R26. A live Run may
use the generic endpoint only when the exact frozen tuple has a passing
capability artifact.

## 8. Domain and persistence contracts

### 8.1 Minimal model evolution

V11 reuses the existing persisted types instead of introducing parallel
`InvestigationTask`, `Finding`, and `RootCauseReport` table families.

- `DiagnosisTask`: existing `title`, `description`, `agent_name`, `tool_names`,
  `depends_on`, `priority`, status, layer, and round cover most needs. Add only
  stable optional fields that are required for UI/replay: `strategy`,
  `evidence_scope`, `expected_discriminator`, `runtime_run_id`, and
  `critic_assessment_id`. `DiagnosisPlan` and `AgentExecution` also gain the
  optional owning `runtime_run_id`; it is required for V11 writes.
- `AgentName`: keep this three-value legacy enum unchanged because current fixed
  flows iterate over every member. Introduce `FindingActor`, a bounded enum that
  contains the three legacy names plus `InvestigatorAgent`, and change only
  `AgentFinding.agent_name` to that compatible superset. Dynamic identity lives in
  `AgentFinding.agent_instance_id`; it is not represented by unbounded enum
  growth.
- `AgentFinding`: retain current fields and add optional `task_id`,
  `runtime_run_id`, `agent_instance_id`, `critic_assessment_id`,
  `affected_entity`, `failure_mechanism`, and `contradicting_evidence_ids`.
  `related_cause_type` remains optional legacy metadata and is never required by
  V11. A legacy round-two finding from one of the three fixed actors still
  requires a same-actor `revises_finding_id`. A V11 round-two
  `InvestigatorAgent` finding instead requires a same-run round-two task and its
  requesting `critic_assessment_id`; `revises_finding_id` is optional because
  supplemental evidence may create a new finding rather than revise an old one.
- `RootCauseCandidate`: make `cause_type` optional; add `affected_entity` and
  `failure_mechanism`. Existing summary, rank, confidence, finding IDs, evidence
  IDs, rationale, and uncertainty remain the common contract.
- `CoordinationReview`: retain candidates and root causes, and add structured
  `critic_assessments`, explicit `lead_decision`, `diagnostic_status`,
  `stop_reason`, `runtime_run_id`, and `authority_mode="agent"`. The
  authoritative accepted roots are exactly the candidates named by
  `lead_decision.candidate_ids`; `root_causes` is only a derived compatibility
  projection and is never read back as V11 authority. Existing V10 rows with no authority mode are
  interpreted as `legacy_deterministic` because their persisted final roots were
  deterministically replaced.
- `MultiAgentRunSummary`: add total input/output tokens, elapsed time, completed
  rounds, investigator count, diagnostic status, authority mode, and
  `runtime_run_id`. `IncidentReport` and `DiagnosisPlan` carry the same run
  ownership.
- `InvestigationRecord`: add optional payload-only `active_runtime_run_id` and
  `source_investigation_id`. The former owns the mutable latest projection; the
  latter links a new V11 investigation created from an immutable legacy record
  and grants no authority to reuse the source diagnosis.
- `ModelProvider`: retain `openai` and `deepseek`, and add
  `openai_compatible`. Generic endpoints always resolve to a concrete
  Chat Completions `Model` before `AgentsRcaRuntime` starts; the runtime no
  longer infers every non-OpenAI credential or adapter as DeepSeek.

The evidence contract gains additive values:

```text
EvidenceProvider += trace | runtime_state | verified_incident
EvidenceKind += trace_path | trace_error | trace_latency | runtime_state |
                verified_incident
EvidenceSourceClass = public_dataset | recorded_local | synthetic_fixture |
                      live_backend
MemoryVerificationStatus = unverified | verified | rejected
```

Evidence provenance records `source_class`, provider profile, source artifact
ID/hash, and adapter version. Replaying a public dataset through Tempo remains
`public_dataset`; the backend does not make it recorded or production evidence.
`synthetic_fixture` may pass tool-contract and failure-path gates but cannot
count as production validation or primary accuracy evidence.

Existing `related_alert` values are reused. `TraceQuery` contains bounded
optional `service`, `operation`, `trace_id`, `window_start`, `window_end`,
`error_only`, `min_duration_ms`, `direction`, and `limit` fields. Direction is
`upstream`, `downstream`, or `both`; service/operation are safe 1..128 character
identifiers, trace ID is 16 or 32 hexadecimal characters, and
`min_duration_ms` is finite and in 0..3,600,000. A trace evidence item carries
only canonical trace/span IDs, optional parent span ID, service/operation,
timestamps, finite non-negative duration, `ok|error|unset` status, and at most
20 allowlisted safe attributes with the same key/value bounds as alert labels;
raw payloads and unrestricted span attributes do not enter Agent context.

All telemetry queries use one bounded scope: `entity_ids` has at most 20 safe
identifiers of 1..128 characters matching
`[A-Za-z0-9][A-Za-z0-9._:/@-]*`; `window_start` and `window_end` appear together
or neither and default to the incident window; both must intersect that
incident; `limit` is 1..100 and defaults to 50. All bounded text is trimmed,
redacted, rejects control characters, and uses the stated maximum after
redaction.

`RuntimeStateQuery.states` adds at most eight unique values from `restarting`, `crash_loop`,
`oom_killed`, `pending`, `not_ready`, `terminated`, `node_pressure`, and
`healthy`, plus `include_healthy=false`. Its payload contains only `entity_id`,
`runtime_kind` (`container`, `pod`, `node`, `process`, or `unknown`), one listed
`state`, `reason` (1..256), `observed_at`, optional non-negative
`restart_count`, and optional `ready`.

`RelatedAlertQuery.severities` adds at most three unique values (`info`,
`warning`, `critical`) and `statuses` adds at most two (`firing`, `resolved`).
Its payload contains `fingerprint` (1..128), `name` (1..128), `entity_id`,
severity, status, `starts_at`, optional `ends_at`, and at most 20 safe labels
whose keys are 1..64 and values 0..256 characters.

`MemoryQuery` is structured rather than free-form: incident service and
environment are fixed by the current investigation; optional `affected_entity`
(1..128) and `failure_mechanism` (1..256) are bounded strings; `limit` is 1..10.
The knowledge cutoff is the current event `started_at`; records whose
`created_at` or `verified_at` is later are ineligible. A `MemoryItem` gains additive
`verification_status`, `verified_at`, `verified_by`, `root_candidate_id`, and
`verification_evidence_ids` fields inside its existing payload. Only an explicit
human verification transition may create `verification_status="verified"`;
free-form feedback, tags, Agent output, or an unverified summary cannot do so.
Lookup emits only verified records whose source investigation and candidate
still exist. The new evidence contains source investigation/candidate IDs,
verification time, a bounded summary, and safe provenance, but never exposes a
foreign evidence ID for direct reuse.

Lookup also rejects a memory item whose source investigation is the current
investigation or any `source_investigation_id` ancestor. A rerun therefore
cannot read its own previously frozen answer as diagnostic evidence; this
guard applies even when the verified-memory store itself is retained across
latest-projection activation.

Prepared packages cannot promote memory by writing `verification_status`
directly. The runtime memory Provider reads guarded repository records only.
Synthetic fixture imports are accepted solely in the isolated tool-contract
suite with `source_class=synthetic_fixture`; formal RCAEval packages use an empty
configured memory store and cannot import prior incident answers.

Every new evidence-kind payload is validated by a bounded Pydantic model before
entering the generic `EvidenceItem.payload`. Unknown fields are discarded or
rejected according to the Provider contract; raw backend JSON is never stored as
the normalized evidence payload.

Result status is frozen across all nine tools:

| Condition | ProviderStatus | Evidence | ToolCallStatus | Agent observation |
| --- | --- | --- | --- | --- |
| Valid non-empty result | `success` | normalized items `success` | `success` | bounded evidence summaries/IDs |
| Valid configured query with no matches | `success` | none | `success` | explicit `empty` |
| Source intentionally absent in the incident manifest | `skipped` | one `provider_error/skipped` item | `skipped` | explicit `unavailable` |
| Some malformed rows but at least one valid row | `partial` | valid items plus one `provider_error/partial` item | `success` | partial evidence and safe warning |
| Configured file/backend missing, corrupt, or all rows invalid | `failed` | one `provider_error/failed` item | `failed` | safe `provider_failure` |
| Bounded provider timeout | `failed` | one `provider_error/failed` item | `failed` | safe `timeout` |
| Invalid/unsupported query field or combination | provider not invoked | none | `failed` | safe `invalid_input` |

The following bounded enums are part of the V11 contract:

```text
LeadAction = investigate | test | conclude | inconclusive
CriticVerdict = accept | reject | needs_evidence | inconclusive
CausalCheckStatus = pass | fail | unknown
DiagnosticStatus = complete | partial | inconclusive
AuthorityMode = agent | legacy_deterministic
ExecutionContractVersion = v10_legacy | v11
```

`DiagnosisTaskType` adds `general_investigation`; Lead planning/adjudication
continue using `rca_synthesis`, and Critic uses `llm_review`. New
`ExecutionStepKind` values distinguish `lead_planning`,
`investigator_analysis`, `critic_review`, `lead_adjudication`, and
`result_validation`. `DiagnosisTaskStatus` and `AgentExecutionStatus` add
`cancelled`; it is a terminal state and cannot be rewritten to completed.

The Lead uses one `LeadDecision` contract. Planning decisions are persisted
with `DiagnosisPlan`; the final decision is persisted with
`CoordinationReview`:

```text
action: LeadAction
summary: non-empty bounded decision summary
task_ids: 0..3 IDs owned by the plan
candidate_ids: IDs from the current review, required only for final conclude
evidence_ids: committed investigation evidence IDs
selected_skills: 0..4 name@version values from the frozen catalog
stop_reason: required for inconclusive
```

Before Critic review, the Lead may return only `investigate`, `test`, or
`inconclusive`; `conclude` is valid only during final adjudication after a
successful Critic review. `investigate` and `test` require one to three task
IDs; final `conclude` requires at least one Critic-accepted candidate ID;
`inconclusive` requires zero tasks/candidates and a stop reason. A general
investigation task requires a non-empty `description`, an
`analysis_round` of 1 or 2, a bounded tool allowlist, and either an evidence
scope or an explicit information gap. `test` additionally requires
`expected_discriminator`. Round two tasks must reference the Critic assessment
that requested them.

Each `CriticAssessment` is persisted inside `CoordinationReview`:

```text
candidate_id: candidate owned by the same review
verdict: CriticVerdict
checks: exactly one result for temporal, topology, mechanism, blast_radius,
        symptom_vs_cause, counterevidence, and alternatives
supporting_evidence_ids: committed IDs
contradicting_evidence_ids: committed IDs
gap: required only for needs_evidence
supplemental_task_ids: 0..3 round-two task IDs; non-empty only for needs_evidence
summary: non-empty bounded review summary
```

A causal check contains `status`, a concise summary, and zero or more evidence
IDs. `pass` or `fail` requires at least one evidence ID; `unknown` requires a
named gap. Critic output never mutates an Investigator finding.

The authoritative `RootCauseCandidate` also adds optional
`onset_window_start` and `onset_window_end`; when both exist, start must not be
later than end. `RootCauseAttribution` remains the narrow compatibility
projection needed by OpenRCA and older clients. Attribution is emitted only
when the candidate has a supported point time or bounded onset window,
component, mechanism, and evidence. Missing values cause projection omission or
an explicitly counted compatibility fallback; they are never invented.

### 8.2 Evidence scope and validator boundary

`EvidenceItem` gains an optional provider-neutral `scope` with bounded
`entity_ids`, `observed_at` or `window_start/window_end`, and `signal_type`.
Provider adapters populate only fields present in source telemetry. Missing
scope is allowed and remains visible as an evidence gap.

The deterministic validator checks only mechanically decidable facts:

- referenced IDs exist, belong to the investigation, are committed, and have
  success or explicitly allowed partial status;
- structured entity/time scope is internally valid and does not contradict the
  candidate's structured entity/time claim;
- ranks, bounds, task ownership, rounds, permissions, budgets, and safe-text
  contracts are valid.

It does **not** decide whether a log pattern, metric, trace, or deployment is a
plausible cause. Causal sufficiency, mechanism support, symptom-versus-cause,
and evidence weighting belong to the Critic and Lead. When evidence lacks
structured scope, the deterministic validator checks reference integrity only;
the Critic must mark the associated causal check `unknown` or justify it from
other scoped evidence. The V10 `CauseType -> Provider` semantic validator is a
legacy handler and cannot validate or rewrite V11 conclusions.

### 8.3 Structured trace policy

V11 does not persist private chain-of-thought. It persists task objectives,
selected action/strategy, tool requests, bounded observations, findings,
Critic checks and verdicts, final concise rationale, token usage, and failures.
The existing `ReActTrace.assistant_text` field remains for old rows but new
writers store only a short decision summary suitable for operator audit.

### 8.4 Persistence and migration

V11 requires schema version 7 because runtime dispatch must read immutable
execution identity and budgets without consulting mutable application config.
`runtime_runs` adds three non-null columns:

- bounded string `execution_contract_version`;
- bounded string `authority_mode`;
- validated JSON `execution_contract`, containing model, provider, API mode,
  credential-free normalized endpoint identity, capability-certification
  identity, prompt/tool-manifest identities, skill-catalog identity, Provider
  profile and artifact identities, token/tool/turn limits, Investigator/round
  limits, tool timeout, retry policy, and `timeout_seconds`. The contract does
  not persist a process-monotonic value or a credential.

Migration assigns every pre-V11 run `v10_legacy`,
`legacy_deterministic`, and a bounded legacy contract derived only from fields
already persisted on that run; missing V11-only limits are explicitly `null`
and make the run ineligible for V11 resume. It does not synthesize Agent
results. All other V11 domain extensions remain in existing JSON payloads.

Migration tests cover fresh databases plus V3, V4, V5, legacy V6, and current
V6 upgrades. Additive fields must round-trip through both repositories. No
historical finding, review, report, or checkpoint is rewritten merely to look
like a V11 Agent run.

Every V11 entry—service method, runtime API, CLI, OpenRCA, and RCAEval—must
create and reload a persisted V11 RuntimeRun before collecting evidence or
calling a model. `runtime.enabled=false` and direct `DiagnosisOrchestrator.run`
cannot execute V11: they fail fast with `v11_runtime_required`, or execute only
an explicitly requested `v10_legacy` deterministic path whose output is labeled
`legacy_deterministic`. A benchmark prediction without a V11 run ID and matching
execution-contract hash is invalid, never an Agent result.

The existing run-create request fields `model_provider`, `model_name`, and
`prompt_version` remain accepted for wire compatibility. For a V11 request they
must equal the server-approved capability-certified provider/endpoint/model/API
tuple and V11 prompt catalog entry; mismatch returns 422 before a run row is
created. `execution_contract_version`, `authority_mode`, normalized endpoint,
and capability artifact identity are server-owned and cannot be supplied or
overridden by the request. User-provided budget fields may only lower server
maxima.

`RuntimeFailureCategory` adds `contract_integrity`. Execution-contract JSON,
compatibility-column, current-projection ownership, checkpoint-hash, frozen
projection, or capability-identity mismatch terminally uses this category in
run, event, API, replay, diff, and acceptance outputs; it is not retried as a
model or Provider transport failure.

### 8.5 State machine

The durable logical states are:

```text
intake
  -> evidence_collection
  -> lead_planning
  -> investigator_round_1
  -> critic_review
  -> investigator_round_2 (completed or explicitly skipped)
  -> critic_reconciliation (completed or explicitly skipped)
  -> lead_adjudication
  -> result_validation
  -> report_generation
  -> finalize
```

The engine exposes two immutable phase profiles:

```text
V10_PHASE_ORDER = INTAKE, EVIDENCE_COLLECTION, DETERMINISTIC_RCA,
                  SPECIALIST_ANALYSIS, CONFLICT_REVIEW, COORDINATION,
                  REPORT_GENERATION, FINALIZE

V11_PHASE_ORDER = INTAKE, EVIDENCE_COLLECTION, LEAD_PLANNING,
                  INVESTIGATOR_ROUND_1, CRITIC_REVIEW,
                  INVESTIGATOR_ROUND_2, CRITIC_RECONCILIATION,
                  LEAD_ADJUDICATION, RESULT_VALIDATION,
                  REPORT_GENERATION, FINALIZE
```

`LEAD_PLANNING`, `INVESTIGATOR_ROUND_1`, `CRITIC_REVIEW`,
`INVESTIGATOR_ROUND_2`, `CRITIC_RECONCILIATION`, `LEAD_ADJUDICATION`, and
`RESULT_VALIDATION` are additive `RuntimePhase` values stored in the existing
string/payload fields; no extra physical phase column is needed. Optional round
two and reconciliation still write ordered `skipped` phase events/checkpoints so
resume position is unambiguous. Existing phase values retain their legacy
meaning and never alias V11 work.

Phase order, preconditions, handler lookup, resume position, checkpoint digest,
event validation, UI label, and acceptance expectations are selected from the
immutable execution contract version. Shared boundary names such as `INTAKE`,
`EVIDENCE_COLLECTION`, `REPORT_GENERATION`, and `FINALIZE` also use
version-specific handlers: the V11 evidence handler does not create V4 legacy
executions, the V11 report handler accepts no hypotheses, and the V11 finalizer
uses the diagnostic-status mapping rather than unconditionally declaring the
investigation complete. This preserves the engine primitives without importing
legacy semantics.

Every new run writes the validated `execution_contract` atomically with
`execution_contract_version` and `authority_mode`. Create, resume, replay, diff,
API projection, and benchmark artifact generation read that durable contract as
the authoritative source of model/prompt identity, tool manifest/version,
token/tool/turn/actor/round budgets, retry policy, and `timeout_seconds`. When
the run first enters `running`, its persisted UTC `started_at` becomes immutable
and the durable deadline is `started_at + timeout_seconds`. Each process computes
remaining wall time from UTC and converts only that remainder to a local
monotonic deadline for calls. A restart never interprets a persisted monotonic
clock value. The coordinator dispatches both the phase profile and handlers by
execution contract version, never by the current application default or the
phase name alone.

Existing `runtime_runs` model, prompt, budget, and timeout columns remain
read-only compatibility projections for legacy queries and indexes. V11 create
writes each projection from the JSON contract in the same transaction. Reload,
resume, checkpoint restore, API projection, and benchmark preparation compare
the projections directly with the JSON contract. Canonical JSON SHA-256 is
computed rather than stored in another schema column; existing checkpoint,
runtime-event, and benchmark-artifact payloads include that hash when they bind
to a contract and verify it on read. Any value/hash mismatch is a fail-closed
`contract_integrity` error and cannot be repaired from mutable settings. New V11
code reads semantics from the validated JSON after this integrity check.

V11 does not maintain a second mutable implementation of the old workflow. An
interrupted `v10_legacy` run is rejected with a durable `RECOVERY_REJECTED`
event and an instruction to create an explicit new V11 live rerun; it is never
continued under V11 semantics. Completed legacy runs and their frozen replay
remain readable. A V11 frozen business projection includes the authoritative
candidates, affected entity/mechanism/time scope, Critic assessments, Lead
decision, diagnostic status, authority, evidence references, and usage. V11
replay/diff compares those fields and invokes neither a live model nor a tool.

### 8.6 Downstream projections

`InvestigationRecord` stores the authoritative V11 review/run summary while
retaining `hypotheses` for legacy data. `IncidentReport` adds diagnoses,
alternatives, diagnostic status, authority mode, Critic summary, evidence gaps,
and usage. V11 report generation accepts an empty hypotheses list and never
uses `hypotheses[0]` as the headline; the legacy renderer remains selected by
the run execution version.

`RecommendedAction` adds optional `related_candidate_ids` and
`runtime_run_id`. `VerificationSuggestion` adds optional
`related_candidate_ids` and `runtime_run_id`, and retains `related_cause_types`
only for legacy records. Human transition validation
accepts same-investigation candidate IDs or legacy cause types, validates
the candidate and action/verification run IDs against the active projection,
validates result evidence ownership identically, and does not require a
`CauseType` for a V11 result. Action generation consumes accepted candidate entity/mechanism and
evidence; it cannot silently call the old `CauseType` rule planner. If no safe
generic recommendation can be justified, V11 emits only a read-only manual
follow-up or no action.

`InvestigationSummary` and workbench add top affected entity/mechanism,
diagnostic status, authority mode, Critic assessments, and Lead decision.
Deprecated `top_cause_type` remains for clients but returns only legacy data or
`unknown`. Runtime frozen projection and diff use separate versioned V10 and
V11 models; a V11 projection cannot coerce free-text candidates back into
`CauseType`.

## 9. Budget, stop, and failure semantics

### 9.1 Default bounds

| Bound | Default/limit |
| --- | --- |
| Investigator instances | maximum 3 |
| Investigation rounds | maximum 2 |
| Critic supplemental requests | maximum 1 batch |
| Run hard deadline | 120 seconds |
| Per-tool timeout | existing configured timeout, maximum 10 seconds by default |
| Model/tool retries | one retry for classified transient transport/rate-limit failure |
| Tool calls | existing global configurable budget; no per-Agent budget may exceed it |
| Tokens | frozen per run; Multi-Agent benchmark total must be ≤3x paired single-Agent total |

The runtime checks remaining wall time, tokens, and tool calls before every
model or tool action. A call that cannot fit the remaining hard budget is not
started. Retry consumption is charged to the same budgets.

The hard deadline starts when the Runtime Run enters `running`; queue time
before that state is excluded. The immutable UTC `started_at` plus frozen
`timeout_seconds` defines one durable deadline for the run, not one per phase or
attempt. An interrupted run may resume only while time remains and inherits the
original deadline and all remaining budgets.
Cancellation and downtime do not refund time or tokens. Each model request must
set `max_output_tokens` no higher than the remaining token budget; input usage is
charged from provider usage immediately after the call, and an over-budget
response is terminally invalid rather than granting another call.

`AgentsSettings` owns server maxima for tokens, tools, turns, investigators,
rounds, tool timeout, and 120-second run deadline. The Runtime create request
freezes effective values and may only lower those maxima. Benchmarks use a
fully materialized run contract artifact rather than ambient config.

### 9.2 Terminal diagnostic status

- `complete`: final diagnosis passed all contracts and all required phases ran.
- `partial`: some provider/Investigator work failed, but the accepted final
  diagnosis remains supported by sufficient independent evidence; failure and
  missing scope are explicit.
- `inconclusive`: the workflow completed safely but evidence cannot support a
  root cause. This is not converted to a deterministic guess.
- `failed`: no valid diagnostic result can be produced because of runtime,
  persistence, contract, or total dependency failure.
- `cancelled`: cancellation was requested and acknowledged.
- `timeout`: represented as a failed or inconclusive run according to whether
  committed evidence supports a valid inconclusive report; runtime failure
  category remains `timeout`.

For accuracy evaluation, `partial` is scored normally and `inconclusive`,
`failed`, `cancelled`, and `timeout` count as incorrect.

`DiagnosticStatus` is separate from durable lifecycle status. Public mapping is
frozen as follows:

| Outcome | `InvestigationStatus` | `RuntimeRunStatus` | Diagnostic status |
| --- | --- | --- | --- |
| Supported result, no required actor failure | `completed` | `completed` | `complete` |
| Supported result after a non-critical provider/Investigator failure | `completed` | `completed` | `partial` |
| Intentional evidence-bounded stop | `completed` | `completed` | `inconclusive` |
| Required actor/validator/persistence failure | `failed` | `failed` | absent |
| User cancellation | `cancelled` | `cancelled` | absent |
| Hard deadline before valid terminal output | `failed` | `failed` with `timeout` category | absent |

Required-actor failure behavior is also fixed:

| Failure after one bounded retry | Continue rule |
| --- | --- |
| Lead planning | Fail the Agent run |
| One round-one Investigator | Continue if at least one other Investigator returned valid findings; any later valid diagnosis is `partial` |
| All round-one Investigators | Fail; no candidate can bypass Investigator and Critic stages |
| Critic | Fail; no uncriticized cause can be accepted |
| Supplemental Investigator/tool | Return `inconclusive` when the missing discriminating evidence is the only blocker; transport/model corruption fails |
| Lead adjudication | Fail |
| Result validator | One schema-correction attempt without new tools; then fail, never substitute a cause |

“Sufficient independent evidence” for `partial` means the accepted candidate
has at least one Critic `accept`, no failed causal check, and supporting
evidence from at least two distinct evidence items, including two provider
types when two are configured and successful. If those conditions are not met,
the result is `inconclusive`.

### 9.3 Degradation

Production may return the existing deterministic report only when fallback is
explicitly enabled and the Agent runtime is not configured, cannot
authenticate, or the configured model endpoint is unavailable before any Agent
diagnostic output is accepted. Rate limit, quota, timeout, invalid output,
invalid reference, unsafe output, persistence failure, or an individual Agent
failure do not trigger deterministic fallback. The response and UI label the
result `legacy_deterministic_fallback`, never `authority_mode="agent"`.
Benchmarks record every fallback as an incorrect V11 Agent result.

## 10. Security, privacy, and safety

- Only the nine-tool V11 manifest generated from the registry can be exposed.
  Tool name, arguments, provider profile, entity/time scope, and output size are
  validated at the trust boundary. Vendor aliases and unavailable tools are not
  advertised as Agent capabilities.
- Lead and Critic cannot invoke provider tools; only bounded Investigators can.
- Evidence is redacted before model input, delimited as untrusted data, and
  projected to bounded fields. Embedded requests to change role, reveal secrets,
  or invoke tools are ignored and covered by injection tests.
- All public failures use the current safe failure categories; provider payloads,
  credentials, raw prompts, private reasoning, and control-plane tokens never
  enter API/report/event payloads.
- Model endpoint URLs reject embedded credentials. API keys are environment-only
  secrets; they are neither accepted by the run-create API nor written to YAML,
  execution contracts, certification artifacts, logs, reports, or traces.
- Cleartext HTTP model endpoints are allowed only for explicitly configured
  loopback or private local-development targets; other model endpoints require
  HTTPS. The run-create API cannot override the configured endpoint.
- All production actions remain recommendations requiring existing approval
  semantics. V11 introduces no write or remediation execution path.
- Evidence IDs are scoped to one investigation. Cross-investigation memory must
  enter as a new validated evidence item with provenance; a raw foreign ID is
  invalid.
- Local incident packages and Docker responses are untrusted inputs. Dataset
  paths, source case IDs, expected answers, evaluator labels, OTLP credentials,
  unrestricted span attributes, and raw backend errors never enter prompts,
  persisted public payloads, or reports.

## 11. Benchmark-neutral evaluation

### 11.1 Dataset and local holdout

RCAEval RE2 is the primary V11 effectiveness dataset because its published
corpus contains 270 multi-source cases—90 each for Online Boutique, Sock Shop,
and Train Ticket—with service and fault labels. The benchmark custodian pins the
upstream revision and artifact checksums, then creates these immutable
partitions before Agent implementation:

- Online Boutique, 30 stratified cases, Open Book development:
  prompt/schema/debugging only;
- Sock Shop, 30 stratified cases, Sealed validation: threshold calibration,
  approach selection, and equal-token ablation;
- Train Ticket, 90 cases, final held-out: one paired final execution after policy
  freeze.

Each 30-case partition contains exactly one case for every service/fault cell
(`5 services × 6 faults`). Before Agent implementation, the custodian freezes a
seed and selects the case with the lowest
`SHA-256(seed + ":" + source_case_id)` in each cell, then replaces source IDs
with opaque IDs. Train Ticket retains all three repetitions in every cell.

Cases are repackaged under opaque IDs. Runtime inputs exclude original answer
files, labels, scenario names, dataset paths, and other answer-bearing metadata.
Formal accuracy packages retain `source_class=public_dataset` and frozen source
hashes in non-answer provenance. Synthetic fixtures may exercise tool and
failure contracts but are excluded from R15/R16 accuracy denominators.
A local custodian workflow prepares opaque runtime packages and separate label
packages. During prediction, the runtime process or container receives only the
runtime package; the label directory and scorer are absent from its arguments,
configuration, environment, mounts, and working tree. After predictions and
their hashes are frozen, a separate evaluator process mounts or opens the label
package read-only and emits a signed or hashed aggregate result artifact. The
same developer account may own both phases, so this is a locally held-out,
label-input-isolated evaluation—not independent third-party certification or a claim
of model-level contamination immunity.

### 11.2 Compared systems

The same frozen model, endpoint identity, API mode, capability certification,
offline Provider profile, provider evidence, tool and skill manifests, prompt
safety contract, and final partition are used for:

1. single-Agent baseline with intended production budget;
2. V11 Multi-Agent system with intended production budget and total tokens no
   greater than 3x the single-Agent total;
3. equal-total-token single-Agent versus Multi-Agent ablation on the 30-case
   sealed validation partition.

Both intended-budget systems run and freeze predictions before final labels are
revealed. The equal-token ablation distinguishes orchestration value from added
compute; it is reported from sealed validation and is not rerun on the 90-case
final partition.

Formal scored execution is therefore bounded to 300 case executions: four
configurations on SS30 (`120`) and two intended-budget configurations on TT90
(`180`). OB30 is unscored development work.

The single-Agent baseline is a frozen V11 control, not the V10 rule path. One
`SingleInvestigatorAgent` receives the same incident, initial evidence, complete
read-only tool registry, provider outputs, structured final diagnosis schema,
redaction/injection contract, deterministic validator, 120-second deadline,
global tool-call cap, retry rule, and model family as the Multi-Agent system. It
may alternate plan, tool, and conclude actions within one context, but has no
subagents, hidden deterministic candidate, Critic, or inter-Agent messages.

Formal scored runs use the deterministic `offline` Provider profile. The
`tempo_local` profile is a separate integration acceptance and cannot replace,
augment, or filter benchmark evidence. This keeps scoring reproducible and
prevents Docker timing or backend indexing from changing the compared evidence.

For intended-budget comparison, the single-Agent token cap is `B` and the
Multi-Agent cap is at most `3B`; both have the same total tool-call cap and
maximum model turns. For equal-token ablation, both caps are `3B`, with all
other contracts unchanged. `B`, max turns, tool cap, model/provider/version,
credential-free endpoint identity, API mode, capability certification, shared
prompt fragments, role-specific prompt, tool manifest, output schema,
normalizer, retry/stop rules, and artifact code revision are materialized and
hashed before sealed validation. They cannot change between validation and
final runs except for an identically applied provider outage correction that
invalidates and reruns both sides before label reveal.

### 11.3 Primary and supporting metrics

- Primary accuracy: exact affected service/component plus exact fault/failure
  mechanism Top1 after one frozen normalization contract.
- Supporting: component Top1, fault Top1, Top3, time-window accuracy, completion
  rate, evidence-reference validity, read-only violations, latency, input/output
  tokens, tool calls, cost, paired per-case delta, paired bootstrap 95% confidence
  interval, and McNemar exact test.
- `inconclusive` and all non-success terminal states count as wrong in accuracy.
- Normalization may standardize case, whitespace, separators, and documented
  aliases only. It cannot map semantic synonyms after seeing final answers.

The single-reviewer evidence-support rubric pass rate is a label-input-isolated
manual audit, not an LLM judge or an inter-rater reliability claim.
After predictions freeze and before answer labels are mounted, the evaluator
exports every candidate-reference pair with the claimed entity, time, mechanism,
and normalized evidence. A frozen rubric records `pass` only when the evidence
identifies or causally connects the claimed entity, is temporally compatible,
contains an observation relevant to the claimed mechanism rather than only a
generic symptom, and does not contradict the claim. Each row stores the four
sub-decisions, one bounded reason code/note, opaque reviewer identity, rubric
version/hash, and timestamp. The reviewer cannot see expected answers during
this audit. The completed pair-level artifact is hashed before label scoring and
cannot be edited afterward. Missing audit rows, duplicate pairs, a changed
rubric, or an incomplete review blocks R16. V11 does not use a model judge.
Published results identify the reviewer as the project owner and state that no
second reviewer/agreement statistic exists; they cannot relabel this rate as
objective precision or cross-reviewer reproducibility.

### 11.4 Frozen acceptance policy

Before the Train Ticket answers are revealed, the custodian freezes and hashes
the partition manifest, normalization code, predictions schema, model/prompt
identities, budgets, and `acceptance-policy.json`. The final absolute threshold
is deterministic:

```text
final_exact_gate = max(0.60,
                       sealed_validation_intended_budget_multi_agent_exact - 0.10)
```

V11 may claim the Multi-Agent architecture is effective only if one final
locally held-out paired run satisfies all of the following:

1. Multi-Agent exact service+fault Top1 is at least `final_exact_gate`.
2. Intended-budget Multi-Agent exact Top1 exceeds intended-budget single-Agent
   exact Top1 by at least `0.10` absolute.
3. Multi-Agent total billable tokens are no more than `3.0x` the single-Agent
   total.
4. Runtime reference integrity is `100%`: every accepted reference exists,
   belongs to the incident, and is committed with an allowed status. Evaluator
   single-reviewer evidence-support rubric pass rate is at least `95%`: the frozen review judges
   whether each cited item supports the specific entity/time/mechanism claim.
5. Wall-clock P95 latency is at most `120s`.
6. Read-only violations and detected answer leakage are both zero.
7. The sealed-validation equal-token ablation result is published. If
   Multi-Agent does not beat single-Agent there, the project may claim
   intended-budget effectiveness but must not claim orchestration efficiency
   independent of compute.

Reference-integrity denominator is every evidence ID in every accepted final
candidate and alternative. Evidence-support denominator is the same set of
candidate-reference pairs, not unique IDs. A case with no accepted diagnosis
contributes no reference pairs but remains incorrect for accuracy; the aggregate
support metric is reported as undefined if the entire run has no pairs and
cannot pass the gate. The paired confidence interval and McNemar result are
mandatory claim context; the confirmed +10pp rule remains the release gate.

No threshold or normalization rule can change after final reveal. Failure is
archived as a result, not repaired against the exposed partition.

### 11.5 OpenRCA compatibility

OpenRCA continues to run the archived deterministic baseline and a V11 Agent
mode through the same generic diagnosis result. Its scores, evidence validity,
fallback count, read-only violations, latency, and tokens are reported, but no
OpenRCA delta is a V11 release gate. A compatibility failure blocks release only
when it indicates a broken public contract, unsafe behavior, crash, or loss of
historical replay—not because the old benchmark score is unchanged.

## 12. Acceptance and verification

### 12.1 Focused contract checks

- Lead actions reject unknown actions, unbounded tasks, missing objectives, and
  budget-infeasible plans.
- Planning rejects `conclude`; final adjudication rejects any candidate without
  a Critic `accept`. Generic tasks, actor step kinds, and cancelled terminal
  states round-trip through both repositories and APIs.
- Legacy fixed-actor round-two findings still require a same-actor revision;
  V11 supplemental findings require same-run task/assessment ownership and may
  omit `revises_finding_id` when they add a new finding.
- Round-one Investigator contexts are isolated; Critic receives only committed,
  redacted findings/evidence.
- Findings and diagnoses reject foreign, missing, failed, or semantically
  incompatible evidence references.
- Critic supplemental work is limited to one batch and round two revises or
  closes named gaps.
- Validator rejection never substitutes deterministic component, mechanism,
  ranking, or evidence.
- Empty or conflicting evidence yields a valid `inconclusive` result.
- Partial provider, one Investigator, Critic, and Lead failures follow the
  terminal semantics in section 9.
- Timeout/cancel cancels sibling tasks, terminalizes pending executions, releases
  the lease, and leaves a resumable committed checkpoint where allowed.
- Resume reuses successful tool calls, preserves budgets, and does not duplicate
  findings or model/tool usage.
- V11 resume inherits the original absolute deadline and execution contract;
  interrupted V10 runs are durably rejected rather than dispatched to V11
  handlers. V10 and V11 replay/diff use their versioned frozen projections.
- V10 and V11 phase orders, handler maps, preconditions, skip checkpoints, and
  resume positions are version-selected. V11 forbidden-call sentinels prove no
  deterministic RCA, fixed-specialist, hybrid-arbitration, legacy report, or
  legacy action function is invoked, including through shared phase names.
- Service, API, CLI, OpenRCA, and RCAEval reject an Agent-authoritative result
  without a persisted V11 run and matching contract hash. Runtime-disabled mode
  cannot silently execute V11.
- V11 projection commits reject missing or mixed `runtime_run_id`. Fault
  injection before/after atomic intake activation proves no half-cleared latest
  view; sequential reruns freeze the prior projection before switching owner;
  resume rejects ownership mismatch. Legacy-to-V11 rerun creates a linked new
  investigation and leaves the legacy projection byte-equivalent.
- Pre-INTAKE cancel, timeout, contract rejection, previous-frozen rejection,
  lease expiry, and recovery rejection write an empty run-owned
  `projection_state="not_activated"` frozen projection and never snapshot the
  previous active owner's latest view; replay/diff expose no inherited output.
- Legacy V3–V6 records, APIs, reports, fixed specialist rows, and OpenRCA
  artifacts remain readable.
- Report, API, frontend, and graph identify Agent authority and show gaps,
  alternatives, critique, evidence, status, and usage without raw reasoning.
- Action, verification, human transition, list summary, and runtime diff work
  without `CauseType` for V11, require active-run ownership for V11 action and
  verification updates, and preserve legacy projections.
- Injection, secret redaction, cross-investigation reference, non-read-only tool,
  and answer-leak tests fail closed.
- The V11 manifest contains exactly the nine approved tool names, is generated
  from `list_agent_specs()`, and excludes internal `query_prometheus` and every
  write tool while legacy `list_specs()` remains compatible.
- Every tool succeeds against at least one offline incident package and covers
  bounded input, empty result, malformed data, missing source, timeout, redaction,
  provenance, and investigation-scoping behavior.
- Offline artifacts report `source_class` and source hashes; synthetic fixtures
  prove contracts only and are rejected from accuracy/production claims.
- File and Tempo trace Providers return semantically equivalent path, status,
  parent relation, order, and duration evidence for one replay manifest after
  documented timestamp/ID normalization; raw payloads need not match.
- Dependency edges reconstructed from parent/child spans preserve direction and
  never infer an edge from service-name co-occurrence alone.
- Memory lookup returns only verified outcomes as new current-investigation
  evidence; an unverified or foreign raw evidence reference is rejected, and a
  current/ancestor investigation cannot feed its frozen answer back into a
  rerun.
- Skill selection exposes only the four frozen data records, verifies required
  tools against the current manifest, and cannot load or execute arbitrary code.

### 12.2 Final repository gates

- Focused domain/runtime/persistence/API/report/frontend tests pass.
- Full backend lint and test suite pass.
- Frontend typecheck/build passes.
- Existing runtime, recovery, privacy, production, and OpenRCA compatibility
  acceptance suites pass under their documented contracts.
- New V11 live acceptance covers complete, partial, inconclusive, timeout,
  cancellation, resume, invalid reference, prompt injection, and deterministic
  fallback labeling.
- A no-network, no-credential, no-Docker local tool acceptance invokes all nine
  tools through the normal registry and produces a hashed result artifact.
- A Docker acceptance starts the pinned local LGTM image, replays at least one
  multi-service trace through OTLP, queries it through `TempoTraceProvider`, and
  shuts down cleanly. This gate is required for a Tempo-support claim.
- Local held-out evaluation tests prove the prediction process has no label path,
  mount, environment value, or answer-bearing package and that scoring begins
  only after prediction hashes are frozen.
- A local fake OpenAI-compatible endpoint deterministically covers request
  routing, tool calls, structured-result validation, usage accounting,
  authentication/rate-limit/timeout mapping, cancellation, transport cleanup,
  no-late-commit behavior, tracing isolation, official-endpoint pinning, URL
  canonicalization vectors, and secret redaction without contacting an external
  model.
- V11 run creation rejects model/prompt values that differ from the certified
  server tuple and rejects client-supplied execution version, authority, or
  endpoint. Contract/projection/hash mismatches persist `contract_integrity`
  consistently across API, resume, replay, and diff.
- Before a compatible endpoint participates in R15 scoring, a separate live
  certification freezes its normalized endpoint/model/API-mode identity and
  proves required remote-observable capabilities. Certification never asserts
  model accuracy or server-side cancellation and never persists the credential.
- Evidence-support audit tests freeze the pair schema and rubric, reject missing
  or duplicate pairs and post-freeze edits, require all four sub-decisions and a
  reviewer identity, and prove the audit runs before answer labels are mounted.
- Local-held-out RCAEval policy and artifact integrity checks pass before scoring; final
  effectiveness gates are evaluated exactly once.
- Intended-budget and equal-token single-Agent controls validate against their
  frozen model, prompt, tool, output, normalizer, budget, retry, and code hashes.
- `git diff --check` is clean and documentation matches implemented contracts.

## 13. Requirements and traceability

| ID | Requirement | Baseline evidence | Acceptance check | Plan task | Focused/final evidence |
| --- | --- | --- | --- | --- | --- |
| R1 | Agent adjudication is authoritative | Orchestrator currently replaces Agent roots | Authority-invariant unit/integration tests | T7, T8, T13 | M3/T7–T8: Lead-authored plan/adjudication and Critic outputs are persisted and projected without deterministic root assignment; exact T7/T8 gates green; product-path authority remains in T9/T13 |
| R2 | Lead plans adaptive information gaps with frozen structured actions/tasks | Fixed modality task map today | Lead action/task/phase contract tests | T2, T7, T13 | M1/T2: bounded Lead/task/phase contracts and ownership tests; full authority behavior remains in T7/T13 |
| R3 | Up to three isolated Investigators use the same bounded nine-tool read-only manifest | Parallel fixed specialists and adaptive session exist; current allowlists drift | Isolation, registry-manifest, concurrency, and budget tests | T4, T7, T13 | M3/T7: bounded generic Investigator instances, unique IDs, isolated round-one contexts, frozen nine-tool manifest, budget/timeout/cancel/cleanup, per-request reservations, and retry evidence; second-round exact T7 gate 79 passed; final concurrency/acceptance remains in T10/T13 |
| R4 | Critic emits the frozen seven-check contract and may request one round | Current review only revisits deterministic conflicts | Critic verdict/reference/supplemental-round tests | T2, T8, T13 | M1/T2: bounded Critic verdict and seven-check validation with same-run owner plus supplemental-task linkage checks; second-round M3/T8 exact gate 195 passed with exactly seven checks/candidate, one reconciliation, no third round, and mechanical safe-text validation; final product path remains in T13 |
| R5 | Hypotheses are optional | Report and coordination currently require hypotheses | Empty-hypothesis complete/inconclusive tests | T2, T7, T9, T13 | M1/T2–T3: optional V11 candidate fields and status-aware finalize/frozen projection; complete flow remains in T7/T9/T13 |
| R6 | Findings support free-text entity/mechanism and evidence/counterevidence | Current output is `CauseType`-centered | Domain validation and persistence round-trip | T2, T7, T8, T13 | M1/T2: bounded entity/mechanism, onset, counterevidence, and memory/SQLite round-trip coverage; M3/T7–T8 produces and preserves Agent-authored fields with committed evidence/counterevidence; exact T7/T8 gates green |
| R7 | Validator rejects but never rewrites conclusions | Current authoritative root copy violates this | Mutation-sentinel tests | T3, T8, T13 | M1/T3: versioned V11 handlers and frozen projection preserve authoritative Lead/Critic fields; M3/T8 mutation sentinel proves mechanical validator does not rewrite rank/entity/mechanism/evidence/counterevidence/onset; final product validation remains in T13 |
| R8 | Versioned durable phases, cancel, resume, replay, idempotency, deadlines, and budgets remain correct | V9 runtime lacks execution contract identity | Migration, fault-injection, legacy-rejection, and replay suites | T3, T6, T10, T13 | M1/T3: persisted-contract phase ordering, activation/recovery ownership, frozen source integrity, contract-integrity reload repair, replay/diff, legacy rejection, and monotonic deadline checks green; M4 adds report/action fault-injection proving PhaseCommit rollback for memory and SQLite; later provider/model gates remain |
| R9 | Complete/partial/inconclusive/failed/cancelled semantics are explicit | Current summary lacks inconclusive | Status matrix tests across API/report/runtime | T2, T3, T8–T10, T13 | M1/T2–T3: bounded status/failure/cancelled contracts and ownership-aware frozen states; M3/T8 covers complete/partial final Critic+Lead and inconclusive-without-candidates; M4 clears diagnoses and alternatives for inconclusive reports and validates the review/run/Lead status matrix; T9/T10 green |
| R10 | Historical records and all hypothesis/cause downstream projections remain readable | Reports, actions, human transitions, summaries, and diff are CauseType-centered | V3–V7 migration, legacy fixture, API/report/action/diff compatibility tests | T2, T3, T9, T10, T13 | M1/T2–T3: V3–V7/current-V6 migration manifests, legacy payload serializers, V10 golden/replay/diff compatibility, and additive Lead/Critic summary/API projection green; M4 scopes usable-evidence validation to V11 while preserving V10 missing-ID behavior and legacy-to-V11 linked rerun; T9/T10 green |
| R11 | No production writes or unsafe tool escalation | Current read-only allowlist exists | Safety and injection suites | T4, T6–T10, T13 | M3/T7–T8: all model tool calls use the frozen registry manifest with invocation-time callable/read-only checks; no report/action/fixed specialist path is entered; full safety/privacy suite remains in T10/T13 |
| R12 | No private reasoning or secrets are persisted/exposed | Current ReAct trace can store assistant text | Payload privacy scans | T6–T10, T13 | M3/T7–T8: only bounded structured outputs/summaries and committed evidence references are persisted; M4 adds one shared V11 public projection across findings, candidates, Critic/Lead, summaries, reports, and graph seeds, with API/UI end-to-end private-marker regressions; T10 green |
| R13 | Production run is bounded to 3 Investigators, 2 rounds, and 120s | Current parallel/budget primitives exist | Boundary, timeout, and race tests | T3, T6–T8, T10, T13 | M1/T3: V11 absolute deadline/phase budget monotonicity and timeout ownership checks; second-round M3/T7–T8 covers 1–3 bounded concurrent Investigators, two rounds, per-request model/tool/turn reservations, timeout, cancellation, late results, and cleanup; final acceptance remains in T10 |
| R14 | Frozen V11 single-Agent control is reproducible and Multi-Agent tokens are ≤3x in primary evaluation | No current paired V11 single-Agent path | Control-contract tests and artifact/hash verifier | T11–T13 | pending |
| R15 | Primary locally held-out exact score meets frozen formula and gains ≥10pp | V10.1 OpenRCA delta was zero; no independent external custodian is available | One frozen RCAEval final paired gate with prediction/label process isolation | T1, T11–T13 | pending |
| R16 | Reference integrity 100%, single-reviewer evidence-support rubric pass rate ≥95%, P95 ≤120s, read-only/leakage zero | Existing acceptance measures only reference existence | Runtime integrity tests plus frozen label-input-isolated support/latency/safety gate | T10–T13 | pending |
| R17 | Equal-token ablation is published with bounded claim language | Not currently measured | Artifact presence and claim checker | T1, T11–T13 | pending |
| R18 | OpenRCA remains compatible without production-path adaptation | Deterministic historical runner/projector exist | Dual-mode compatibility suite and code scan | T3, T9, T10, T13 | M1/T3: V10 phase/profile and golden payload compatibility green; M4 adds a V11-only generic candidate projector with shared usable-evidence/status/owner validation while retaining the deterministic V10 projector; T9/T10 green |
| R19 | Report/API/UI expose V11 reasoning artifacts and legacy authority labels | Current UI is deterministic-hypothesis-led | Contract and frontend tests | T9, T13 | M4 API/UI/report regressions preserve safe Critic/Lead summaries, finding/candidate rationale, graph labels, actual finding actors/tasks/rounds/evidence, and legacy authority labels without private markers; T9/T10 green |
| R20 | Add only the three Schema V7 run-contract columns; no new framework or table family | Existing runtime/payload tables are reusable but lack immutable execution identity and full budgets | Diff/design review and schema V3–V7 manifest tests | T2, T3, T13 | M1/T2: fresh/V3/V4/V5/legacy-V6/current-V6 manifests agree, only the three approved columns are added, and contract-integrity reload is fail-closed; final review remains in T13 |
| R21 | Trace, runtime-state, related-alert, dependency, and verified-memory evidence are available through provider-neutral contracts | Trace is absent; dependency is mock-only in production; related alerts lack a tool; memory returns empty success | Domain/query/provider/tool contract tests | T4, T5, T10, T13 | pending |
| R22 | Every V11 tool is verifiable offline on one host without credentials, Docker, or external services | Local OpenRCA metrics/logs/traces exist but there is no complete incident-package gate | Nine-tool offline acceptance and artifact hash check | T4, T10, T13 | pending |
| R23 | Tempo is the only V11 real trace backend and is reproducibly testable through local Docker and OTLP replay | Docker/Compose client exists locally but the daemon may be unavailable; no Tempo Provider or replay gate exists | Pinned-image preflight, unique-project Compose, replay, parity, bounded polling, blocked-state, and scoped cleanup acceptance | T5, T10, T13 | pending |
| R24 | Four versioned data-only diagnostic skills are selectable without adding executable plugins or MCP | Strategies are prose only; no Skill/MCP subsystem exists | Catalog schema/hash, required-tool, prompt, and no-dynamic-load checks | T4, T7, T11–T13 | M3/T7 uses the existing data-only skill catalog and registry-derived manifest without executable plugin/MCP loading; exact T7 gate green; control/evaluation parity remains in T11–T13 |
| R25 | Formal scoring uses offline evidence and locally isolates runtime inputs from labels until prediction freeze | Current spec assumed an unavailable external custodian | Mount/path/environment denial tests and evaluator ordering/hash checks | T1, T11–T13 | pending |
| R26 | Official OpenAI, existing DeepSeek, and certified OpenAI-compatible Chat Completions endpoints share one safe model boundary without vendor lock-in | Current settings expose only OpenAI/DeepSeek; the DeepSeek adapter hardcodes its URL/key and runtime treats every non-OpenAI provider as DeepSeek | Generic-adapter contract tests, fake-endpoint failure matrix, secret/tracing scans, live capability certification, and paired-run identity checks | T6, T10–T13 | pending |
| R27 | Reused V10 infrastructure is isolated from V11 diagnostic semantics and every V11 artifact/entry is bound to one durable run | Current phase executor, direct orchestrator, Agent runtime, report/action paths, manifests, and latest-projection rows can execute or retain legacy semantics | Versioned-phase/profile tests, forbidden-call sentinels, all-entry RuntimeRun gate, supplemental-finding ownership, registry exposure, atomic projection activation/rerun/action ownership, run-create mismatch, `contract_integrity`, and persistence-boundary revalidation suites | T2–T4, T6–T11, T13 | M1/T2–T3: first-round ten findings plus second-round three medium persistence bypasses closed at shared profile, frozen-owner, BusinessMutation, aggregate model-validation, transaction-repair, summary, Critic/finding-linkage, execution-owner, and service/API boundaries; M3/T7–T8 adds persisted V11 phase dispatch, runtime/task/tool/evidence owner checks, V10 legacy-runtime isolation, supplemental ownership, and no report/action/fixed-loop entry; M4 closes report/action PhaseCommit atomicity, configured V11 product entry/rerun selection, active V11 rejection of V10, exact artifact owners, and shared public/evidence projections; exact T9/T10 and full gates green |

## 14. Alternatives considered

### A. Fixed modality specialists

Lowest implementation cost, but reproduces the current Log/Metric/Deployment
silos, front-loads assumptions about where the cause lives, and encourages
taxonomy fitting. Rejected as the V11 target.

### B. Adaptive evidence investigation — selected

The Lead assigns questions based on information gaps; general Investigators use
all bounded tools; the Critic challenges causal sufficiency; the Lead
adjudicates. This is the best fit for general SRE diagnosis and reuses the
current runtime/tooling while replacing the actual authority bottleneck.

### C. Debate and vote

Independent opinions can expose blind spots, but majority agreement is not
evidence and repeated debate increases tokens and correlated hallucination.
Rejected as the authority model. V11 keeps only isolated first passes and an
explicit Critic.

### D. External-first connectors and generic MCP

Starting with Kubernetes, multiple observability vendors, or a generic MCP
gateway would leave the personal project dependent on infrastructure it cannot
continuously verify. It would also duplicate the existing ToolRegistry without
improving causal reasoning. Rejected for V11. The selected local-first design
uses file Providers for every contract and one Tempo Docker adapter to prove a
real trace protocol boundary.

### E. General model gateway

Adding LiteLLM, Any-LLM, LangChain, automatic protocol probing, or multi-provider
routing would introduce another compatibility layer when the installed OpenAI
SDK and the existing DeepSeek adapter already provide the required extension
point. Rejected for V11. The selected design adds one configurable Chat
Completions adapter and certifies behavior instead of trusting a compatibility
label.

## 15. Review ledger

Independent review: completed on 2026-08-02. Initial review found four blocking,
five high, and three medium issues. A focused re-review confirmed B1–B4 closed
and found no remaining blocker. A post-approval local-first amendment began on
2026-08-02 after source inspection disproved the assumed evidence coverage and
the user confirmed that V11 must be fully verifiable on one personal host.
The amendment cold review found L6–L16 after L1–L5; two focused re-reviews
confirmed every blocking, high, medium, and low finding closed. The later R26
scope change added L17; its independent review found L18–L21. Focused re-review
closed L18, L20, and L21 and reduced L19 to one low hash-preimage ambiguity,
which the canonical self-field-excluding hash rule now closes. A full legacy
reuse audit then traced every declared reuse through runtime, persistence,
entry-point, report/action, tool, API, and benchmark consumers and found
L22–L29. Focused re-review closed L22–L27/L29 and found L28 incomplete plus
L30; the atomic activation, owner-aware pre-INTAKE termination, and
action/verification ownership amendments closed both. The pre-implementation
focused re-review found no remaining blocking, high, or medium issue.

M1 implementation second-round review (2026-08-06) found no blocking or high
issue and three medium persistence-boundary issues: ownerless
`RESULT_VALIDATION` executions without `analysis_round`, direct plan/task
writes that skipped V11 revalidation, and direct Investigation aggregate writes
that skipped nested owner validation. The fixes reuse the existing Pydantic
revalidation boundary, validate SQLite before replacement, preserve legacy
payload field presence, and add focused RED→GREEN evidence without changing
the approved contract or milestone scope. Independent re-review remains
pending.

M2 implementation review (2026-08-08): independent review concluded
`approve_with_followups` with no blocking finding. The reviewer independently
reran every focused gate (336/106/17 passed, offline acceptance rows=80
failed=0, Ruff clean, full suite 1927 passed/3 skipped) and verified three
implementer claims: the unreported `.gitignore` change is benign (three new
acceptance-output ignore rules); the two schema-version assertion fixes (6→7)
are a legitimate M1 leftover confirmed failing on merge commit `a40942a`
(M1's scoped gates never included those two files, so merge-time full-suite
green was unverified); and the T5 image-pinning deviation (digest injected via
environment and verified against daemon RepoDigests instead of a repo-pinned
value) preserves the security property that no passed gate exists without a
cryptographic digest binding, so the plan remains approved. Findings M2R-1 to
M2R-4 are recorded in the ledger below. M2R-1 was closed on 2026-08-08 with
four slow-endpoint fake-endpoint tests and a focused re-review that confirmed
their substance; M2R-2/M2R-3 remain explicit T7 wiring prerequisites.

| ID | Severity | Finding | Resolution |
| --- | --- | --- | --- |
| B1 | blocking | Lead/Critic structured outputs and generic task/status contracts were underspecified | Closed in §8.1 with frozen enums, `LeadDecision`, `CriticAssessment`, seven checks, task/step kinds, references, rounds, and cancellation |
| B2 | blocking | Old interrupted runs could resume under new phase semantics; frozen replay was CauseType-only | Closed in §8.4–8.6 with schema V7 execution identity, legacy recovery rejection, and versioned frozen projection/diff |
| B3 | blocking | Single-Agent baseline was not reproducible and could be manipulated | Closed in §11.2 with a frozen V11 control, `B`/`3B` budgets, equal tools, shared contracts, and artifact hashes |
| B4 | blocking | Deterministic semantic validation risked recreating a rule RCA engine | Closed in §8.2 by limiting mechanical validation to references/scope/contracts and assigning causal sufficiency to Critic/Lead |
| H1 | high | Hypothesis/CauseType downstream consumer inventory was incomplete | Closed in §5 and §8.6 for reports, actions, verifications, human transitions, summaries, replay/diff, API/UI, and acceptance |
| H2 | high | Diagnostic, investigation, runtime, and actor failure status mappings were ambiguous | Closed by §9.2 status and actor-failure matrices |
| H3 | high | Deadline/token configuration and resume semantics were absent | Closed in §5 and §9.1 with server maxima, frozen run values, one absolute deadline, and monotonic resume budgets |
| H4 | high | Authoritative candidate could not preserve supported onset/time | Closed in §8.1 with optional onset window and non-inventing attribution projection |
| H5 | high | Runtime fail-closed references conflicted with the 95% benchmark gate | Closed in §11.4: runtime integrity 100%, single-reviewer support-rubric pass rate ≥95%, explicit denominators |
| M1 | medium | A 90-case +10pp result lacked uncertainty reporting | Closed in §11.3–11.4 with paired bootstrap CI and McNemar reporting; confirmed +10pp remains the release gate |
| M2 | medium | Deterministic fallback triggers were too broad | Closed in §9.3 with an explicit pre-diagnosis availability allowlist and benchmark fallback-as-wrong rule |
| M3 | medium | Serialized `adaptive` could not distinguish V10 and V11 semantics | Closed by immutable execution version and authority mode in §8.4–8.6 |
| P1 | blocking plan review | Full execution limits lacked one durable persistence source | Closed by the approved requirement's minimal Schema V7 realization: validated `execution_contract` JSON plus execution/authority strings |
| P2 | blocking plan review | Round-two evidence lacked a final Critic state transition | Closed by one same-assessment reconciliation after round two; no further `needs_evidence` is allowed |
| L1 | blocking new evidence | The approved design allowed trace backtracking but the code had no service trace query, Provider, or evidence kind | Closed in §5, §7.5, §8.1, §12, and R21–R23 with file/Tempo trace contracts and local parity gates |
| L2 | high new evidence | Dependency, related-alert, and memory capabilities were absent, mock-only, or empty despite appearing in the conceptual tool set | Closed by the exact nine-tool manifest, local Provider requirements, verified-memory provenance, and offline acceptance |
| L3 | high scope change | External live Providers and an off-host custodian were infeasible for a personal project | Closed by local incident packages, Docker-local Tempo, and locally held-out label-input isolation in §7.7 and §11 |
| L4 | high missing boundary | Diagnostic skills and MCP responsibilities were not specified | Closed with four data-only skills and explicit MCP/plugin non-goals |
| L5 | medium new evidence | `query_prometheus` was registered but omitted from the ReAct allowlist, creating duplicate manifest drift | Closed by one registry-derived V11 manifest and an internal-only compatibility alias |
| L6 | blocking independent review | The local-first wording incorrectly made credential-free formal Agent accuracy possible despite only remote model runtimes | Closed in §2.3, §3.2, §7.7, and O15 by separating credential-free tool/orchestration gates from credentialed remote-LLM scoring |
| L7 | blocking independent review | A process-monotonic absolute deadline was persisted before `started_at` existed | Closed in §8.4–§9.1 with frozen `timeout_seconds`, immutable UTC `started_at`, derived durable deadline, and per-process monotonic remainder |
| L8 | high independent review | Runtime-state, alert, and memory queries/payloads plus verified-incident eligibility were not implementable contracts | Closed in §8.1 with bounded query/payload schemas, knowledge cutoff, status matrix, and guarded verified memory |
| L9 | high independent review | Tempo timestamp shifting would violate canonical incident scope and parity was undefined | Closed in §7.7/O16 with bidirectional time/ID mapping and canonical semantic parity fields |
| L10 | high independent review | Skill catalog freeze conflicted with dynamic selection and did not define single-Agent fairness | Closed in §7.6 and `LeadDecision`: freeze catalog only, persist selection, and charge the same planning budget |
| L11 | high independent review | Existing runtime columns and JSON execution contract could diverge as competing sources | Closed in §8.5 with compatibility projections, transactional creation, canonical hash/equality validation, and fail-closed reload |
| L12 | high independent review | Evidence-support precision had no reproducible local audit contract | Closed by narrowing the claim to a frozen single-reviewer, label-input-isolated rubric pass rate with an immutable pair-level artifact |
| L13 | high independent review | The SS30-derived final threshold did not identify the intended-budget Multi-Agent configuration | Closed in §11.4 by naming `sealed_validation_intended_budget_multi_agent_exact` |
| L14 | medium independent review | Same-host isolation was overstated as blind evaluation | Closed throughout §11/R15/R25 as locally held-out, label-input-isolated evaluation, not third-party blindness |
| L15 | medium independent review | Tempo Gate lacked blocked preflight and project-scoped cleanup semantics | Closed in §7.7/R23 with bounded preflight/polling, unique Compose project, blocked status, and scoped cleanup |
| L16 | medium independent review | Hand-authored local evidence could be mistaken for recorded or production evidence | Closed with `EvidenceSourceClass`, artifact hashes, and exclusion of synthetic fixtures from accuracy/production claims |
| L17 | high scope change | The model boundary was limited to two named vendors even though the installed SDK and DeepSeek adapter already support a generic OpenAI-compatible client | Closed in §2.3, §5–§7.8, §8.1/§8.4, §10–§12, and R26 with one certified Chat Completions adapter, frozen endpoint identity, tracing/secret isolation, and no new model framework |
| L18 | high independent review | Official OpenAI could inherit `OPENAI_BASE_URL` and bypass the compatible-provider freeze, certification, and tracing boundary | Closed in §7.8/R26 by pinning the official endpoint and requiring every custom address to select `openai_compatible` |
| L19 | high independent review | Existing reliability certification was keyed only by provider/model and could be mistaken for endpoint capability certification; focused review also found the new artifact hash preimage self-referential | Closed in §7.8/§12/R26 with the separate `model-capability-v1` artifact, exact tuple binding, API status, legacy-artifact exclusion, and a canonical JSON hash that omits its own hash field |
| L20 | medium independent review | Remote HTTP cancellation could not prove that the model server stopped computation | Closed in §2.3/O19/§7.8/§12 by limiting live certification to observable responses and verifying client cancellation, no late commit, and cleanup locally |
| L21 | medium independent review | `base_url` canonicalization was undefined, allowing false mismatches or incorrect certificate reuse | Closed in §7.8/§12/R26 with one explicit algorithm, SHA-256 endpoint identity, and accepted/rejected vectors |
| L22 | blocking reuse audit | Runtime-disabled service and benchmark paths could call the old orchestrator and still be mistaken for Agent authority | Closed in §5.1/§8.4/R27 by requiring a persisted V11 RuntimeRun for every Agent-authoritative entry and rejecting direct/runtime-disabled V11 |
| L23 | high reuse audit | Reusing old serialized phase names would execute deterministic/fixed/hybrid handlers, and one linear specialist phase could not represent Critic-requested round two | Closed in §8.5/R27 with additive V11 phases, immutable V10/V11 profiles, explicit skipped checkpoints, and version-specific shared-boundary handlers |
| L24 | high reuse audit | The registry had no Agent-visible/internal distinction, so a single-source nine-tool manifest could only be recreated with another drifting allowlist | Closed in §5/§7.5/R27 with compatible `ToolSpec.exposure`, `list_agent_specs()`, and invocation-time manifest/read-only checks |
| L25 | high reuse audit | The existing `AgentFinding` validator forces every round-two finding to revise a same-fixed-Agent round-one finding, rejecting valid supplemental findings | Closed in §5/§8.1/§12/R27 by scoping legacy revision rules and binding V11 supplemental findings to their run, task, and Critic assessment |
| L26 | high reuse audit | Historical run-create model/prompt overrides could bypass the frozen certified V11 tuple | Closed in §5/§8.4/R27 by exact-match-only compatibility inputs, server-owned execution identity, and pre-create 422 rejection |
| L27 | medium reuse audit | `contract_integrity` was required behavior but absent from the persisted failure enum and consumer mapping | Closed in §5/§8.4/§12/R27 with one additive terminal failure category across run/event/API/replay/diff |
| L28 | high reuse audit and re-review | Investigation-scoped latest projections had no owning run; the first amendment lacked an atomic owner switch, and the second still allowed a pre-INTAKE terminating run to freeze the previous owner's latest view | Closed in §5.1/§8.1/§12/R27 with payload-only active owner, atomic intake activation/clear/checkpoint, owner-aware terminal freezing with empty `not_activated` projection, fault injection, terminal freeze-before-switch, linked legacy-to-V11 rerun, and fail-closed resume ownership |
| L29 | high reuse audit | Treating `AgentsRcaRuntime`, phase handlers, reports, actions, and OpenRCA modules as whole reusable units would retain deterministic hypotheses, fixed Agent loops, and CauseType authority | Closed in §5.1/O20/R27 by a primitive-level reuse allowlist and forbidden-call sentinel suite |
| L30 | medium reuse re-review | Actions and verification suggestions participated in the latest projection and human transitions but were not bound to the owning V11 run | Closed in §5.1/§8.1/§8.6/§12/R27 with payload-only run IDs and active-owner validation on generation and transition |
| M2R-1 | high M2 review | T6 adapter lacked the §7.8-required local slow-endpoint evidence for client cancellation, deadline enforcement, no late commit, and transport cleanup | Closed 2026-08-08: four slow-endpoint tests prove all four semantics against the real SDK/httpx stack; they passed with zero implementation change (coverage gap only, same shape as RR-M1), and focused re-review verified the assertions discriminate real cancellation/late-commit structures |
| M2R-2 | medium M2 review | The only production wiring of `VerifiedMemoryLookup` injects no current-investigation resolver, so the self/source-ancestor memory exclusion guard is inert outside tests | Closed 2026-08-08 in M3/T7: `AppContainer` injects the current/source-ancestor resolver through the shared provider registry; production wiring integration test passed |
| M2R-3 | low M2 review | `assert_agent_callable` invocation rechecks have no production call site until V11 orchestration exists | Closed 2026-08-08 in M3/T7: V11 dispatch, retry, and resume invoke the registry assertion before model/tool dispatch; bypass regression test passed |
| M2R-4 | low M2 review | Offline acceptance redaction rows omit the span-attribute free-text channel | Open: optional hardening, not a gate |

M3 implementation evidence (2026-08-08): the M2 snapshot was committed as
`M2_BASE_SHA=c2245ac` before implementation on isolated branch
`codex/v11-m3`. T7 exact verification passed `53 passed` and its scoped Ruff
gate passed; T8 exact verification passed `164 passed` and its scoped Ruff gate
passed. M2R-2/M2R-3 focused verification passed `27 passed`; the full suite
passed `1940 passed, 3 skipped, 1 warning`, and `uv run ruff check backend tests`
passed. The implementation uses the existing SDK/model lifecycle and keeps
`AgentsRcaRuntime` V10-only; no fixed loops, deterministic hypothesis/root
assignment, legacy report/action path, M4, or M5 was started. Independent M3
review is pending.

M3 review-fix ledger (2026-08-08): independent review verdict `BLOCKING` was
issued against base `b17da397807bacf6e155afe71062ad1c6c4fd868`. The approved
V11 contract and milestone scope were preserved. Minimal RED regressions were
added before each shared-boundary GREEN fix:

| Finding | Closed behavior and evidence |
| --- | --- |
| B1 | Explicit `investigation_id` owner fencing now crosses commit, execution, budget, summary, and resume helpers; a multi-investigation repository regression proves the target run cannot cross-write. |
| H1 | Run admission persists an ordered immutable nine-tool manifest/hash with skill/capability identity and limits; dispatch, retry, and resume reject a changed or unordered contract. |
| H2 | One persisted retry coordinator classifies failures; only transport/rate-limit gets at most one retry, with attempts/resume metadata durable and semantic/validation failures non-retryable. |
| H3 | V11 freezes a non-None token ceiling; input is atomically charged before request, output is capped by remaining budget, usage is checkpointed, and zero budget sends no model request across manual/SDK/provider paths. |
| H4 | Absolute deadline preflight runs before every model/tool action, timeout is bounded by remaining time, and timeout/cancel/fence loss prevents late commit. |
| H5 | Model/tool budgets use durable atomic reservations; concurrent Investigator, retry, resume, and cancel paths share the reservation, and transport retry reuses one logical tool action reservation. |
| H6 | Investigator, required Critic/Lead/validator, and persistence failures terminalize `failed` with no diagnostic fallback; complete/partial require final Critic and Lead. |
| H7 | Validator mechanically enforces same-run usable evidence, exact assessment coverage, inconclusive/partial contracts, and final actor coverage without mutating Agent-authored fields or invoking CauseType/provider semantics. |
| M1 | All nested CausalCheck/CriticAssessment/LeadDecision summary/gap/stop text is scanned for control characters only. |
| M2 | Critic and reconciliation success/failure update one unique durable AgentExecution audit with actor, attempt, deadline, usage, and resume metadata; final actor coverage is validated. |

Final review-fix gates: T7 exact `70 passed`, T8 exact `184 passed`, M2R
focused `83 passed/2 skipped` plus keyword focused `16 passed/33 deselected`,
full `uv run pytest -q` `1967 passed/3 skipped/1 warning`, and full
`uv run ruff check backend tests` clean. Current/Plan task rows now record the
M2 baseline `c2245ac` as committed and T4/T5 as implemented/verified; T5's
live Docker execution remains explicitly host-dependent. The fix is isolated
to `codex/v11-m3`, with no merge/push/M4/M5 action; same-thread independent
review remains pending.

M3 second-round review-fix2 ledger (2026-08-09): independent review verdict
`BLOCKING` was issued against base
`629bc34e823ff9f5ff163dca5f3924a161baafd3`. The existing §6 scope and approved
R1–R27 requirements were not changed. Every finding first received a minimal
RED regression and then a shared-boundary GREEN fix:

| Finding | RED → GREEN evidence | Resolution |
| --- | --- | --- |
| B1 | `test_v11_run_owner_is_explicit_when_repository_has_multiple_investigations`; `test_v11_nested_execution_contract_digest_fences_capability_and_limits` | Server-owned canonical nested execution contract/digest is created at admission; explicit investigation ownership and full contract validation cross clone, bind, dispatch, retry, resume, and persistence helpers. |
| H1 | `test_v11_frozen_manifest_rejects_same_cardinality_tool_swap`; `test_v11_frozen_manifest_rejects_unordered_contract` | Ordered immutable nine-tool manifest/hash plus skill/capability identity and limits are the only dispatch/retry/resume contract; same-cardinality tool or nested identity/limit mutations fail closed. |
| B2/H2 | `test_v11_sdk_provider_retry_is_only_the_persisted_outer_retry`; `test_v11_model_retry_is_one_classified_transport_attempt`; `test_v11_parse_failure_marks_latest_retry_attempt_invalid_output` | V11 SDK/provider clients use retry zero; one persisted coordinator owns at most one transport/rate-limit retry, persists attempts, and updates the latest malformed attempt. |
| H3 | `test_v11_sdk_model_requests_reserve_decreasing_output_caps`; `test_v11_model_precharges_input_before_setting_output_cap`; `test_v11_zero_tool_budget_does_not_start_model`; `test_v11_sdk_budget_reservation_is_released_when_preflight_rejects` | Every SDK request atomically charges input and derives a descending output cap from the durable reservation; zero budget sends no request and preflight rejection releases an unstarted reservation. |
| H4 | `test_v11_model_deadline_preflight_blocks_request_before_start`; `test_session_deadline_preflight_blocks_tool_before_provider_start`; `test_late_tool_result_is_rejected_after_execution_fence_loss` | Absolute deadline/cancel preflight runs before each model/tool action, timeout fits remaining time, and late results cannot commit. |
| H5 | `test_v11_tool_budget_reservation_is_atomic_and_durable`; `test_v11_transport_retry_reuses_one_durable_tool_reservation`; `test_v11_investigators_share_a_bounded_concurrent_gate` | One durable reservation belongs to one logical tool action and is reused by retry/resume/cancel; concurrent Investigators share a bounded gate and cannot oversell. |
| H6 | `test_v11_all_investigator_failure_is_terminal_failed_without_diagnostic`; `test_v11_required_critic_failure_is_failed_without_inconclusive_fallback`; `test_v11_required_lead_failure_is_failed_without_inconclusive_fallback`; `test_v11_validator_failure_is_failed_without_inconclusive_fallback`; `test_v11_required_investigator_failure_stops_before_critic_or_lead` | Required actor or persistence failure terminalizes `failed`, clears diagnostic projection, and stops later V11 phases; no pseudo-inconclusive fallback is generated. |
| H7/M1 | `test_v11_validator_rejects_orphan_supplemental_task_ids`; `test_v11_partial_requires_usable_evidence_passing_check_and_round_two_linkage`; `test_v11_validator_scans_nested_critic_check_text_for_control_characters`; `test_v11_validator_rejects_whitespace_controls_in_nested_assessment_text` | Mechanical validation requires exact same-run round-two task linkage, enforces partial/inconclusive contracts, and scans nested result text for controls without semantic CauseType/provider validation. |
| M2 | `test_v11_critic_success_has_one_durable_audit_execution`; `test_v11_reconciliation_has_one_durable_audit_execution`; `test_v11_critic_output_failure_updates_one_audit_execution`; `test_v11_parse_failure_marks_latest_retry_attempt_invalid_output` | Critic/reconciliation success and failure retain one unique durable execution audit, and parse failure updates the latest retry attempt with actor, attempt, deadline, usage, and resume metadata. |

Final second-round gates: T7 exact `79 passed`; T8 exact `195 passed`; scoped
T7/T8 Ruff clean; explicit M2R-2/M2R-3 and isolation focused `84 passed/3
skipped`; full `uv run pytest -q` `1981 passed/3 skipped/1 warning`; full
`uv run ruff check .` and `uv run ruff check backend tests` clean;
`git diff --check 629bc34..HEAD` clean. The sole warning is the existing
Starlette/httpx TestClient deprecation warning. T4/T5 remain
`implemented / verified`, with only live Docker execution host-dependent and
unavailable. The single `M3_REVIEW_FIX2_SHA` commit is isolated to
`codex/v11-m3`; no merge/push/M4/M5 action was taken and same-thread independent
review remains pending.

M3 third-round review-fix3 ledger (2026-08-09): independent review verdict
`BLOCKING` was issued against base
`b8c08d8a410877ef021cde69be0ec2faac98fb28`. The approved §6 scope and R1–R27
requirements were unchanged. Each B1/B2/H1/H2 finding first received a minimal
RED regression and then a shared-boundary GREEN fix:

| Finding | RED → GREEN evidence | Resolution |
| --- | --- | --- |
| B1 | `test_v11_started_model_reservation_is_durable_for_resume_reconciliation` (RED collection, GREEN); `test_v11_resume_releases_crash_window_reservation_once`; `test_v11_sdk_reservation_retry_reuses_one_durable_allocation_and_settles_once`; `test_v11_concurrent_model_reservations_cannot_oversell_token_ceiling` | Reused `RuntimeEvent`/`RuntimeWriter` MODEL lifecycle persistence for run/logical-call/reservation/attempt identity. Reservation is durable before SDK send; actual completion settles usage, cancellation/timeout releases, retry reuses the same reservation, resume releases pending reservations deterministically, and duplicate settlement is ignored. |
| B2 | `test_v11_official_provider_contract_and_client_ignore_ambient_endpoint` (RED, GREEN); `test_v11_official_contract_survives_sqlite_reload_with_ambient_endpoint` | Official V11 uses explicit `OFFICIAL_OPENAI_BASE_URL`, includes its endpoint identity in the sealed admission digest, validates the actual client URL on provider construction, and leaves explicit compatible URLs distinct. |
| H1 | `test_v11_round_two_required_investigator_failure_terminalizes_before_reconciliation` (RED, GREEN); `test_v11_round_two_completed_partial_batch_is_not_blanket_failed` | Required round-two assessment/task ownership or Investigator failure clears the diagnostic projection and terminalizes before round completion is persisted; phase dispatch stops later reconciliation/Critic/Lead, while valid completed supplemental work remains eligible to continue. |
| H2 | `test_v11_inconclusive_lead_clears_candidates_before_persist_and_reload` (RED, GREEN) | Lead semantic normalization clears candidates, accepted Critic assessment IDs, and root-cause attributions before persistence for legal `INCONCLUSIVE`; mechanical validation remains read-only and preserves the stop reason through reload. |

Final third-round verification: T7 exact `86 passed`; T8 exact `202 passed`;
T7/T8 scoped Ruff clean; M2R-2/M2R-3 and isolation focused `84 passed, 3
skipped`; full `uv run pytest -q` `1990 passed, 3 skipped, 1 warning`; full
Ruff (`uv run ruff check .` and `uv run ruff check backend tests`) clean. The
warning is the existing Starlette/httpx TestClient deprecation warning.
`M2_BASE_SHA=c2245ac` remains the separate M2 snapshot. The third-round fix is
isolated to `codex/v11-m3` with symbolic `M3_REVIEW_FIX3_SHA` pending the single
commit and same-thread independent review; no merge, push, M4, or M5 action was
taken.

M3 fourth-round review-fix4 ledger (2026-08-09): independent review verdict
`BLOCKING` was issued against base
`9a7bf4a43efd313e5dcb528361dfbfbfec1c37f0`; the sole remaining finding was
multi-request SDK retry reservation identity. The approved §6 contract, V10
legacy boundary, and all previously closed findings remain unchanged. The
production reproduction first went RED: after request one settled and request
two failed with a transport retry, the outer retry replayed request one with
the settled reservation and raised
`V11RuntimeContractError("model reservation was already settled")` before the
provider retry request.

| Finding | RED → GREEN evidence | Contract-preserving resolution |
| --- | --- | --- |
| B1 | `test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request` (RED, GREEN); `test_v11_sdk_replay_cursor_rehydrates_and_repeated_replay_is_idempotent` (RED, GREEN) | Reuse the existing durable MODEL `RuntimeEvent` lifecycle: persist `request_index`, rehydrate ordered request history through `PhaseInput`, assign deterministic fresh `replay-N` reservations to completed requests, and reuse the original identity only for the active failed request. If a later retry reservation holds the remaining budget, it is durably deferred and restored when its request index is reached. Production coverage proves four provider calls, new request-one identity, request-two reuse, settled usage/remaining budget, zero active reservations, released-reservation non-reuse, and repeated replay idempotence. |

Final fourth-round verification: T7 exact `88 passed`; T8 exact `204 passed`;
M2R-2/M2R-3 and isolation focused `84 passed, 3 skipped`; changed/runtime
focused `102 passed, 2 skipped`; full `uv run pytest -q` `1992 passed, 3
skipped, 1 warning`; both `uv run ruff check .` and `uv run ruff check backend
tests` clean; diff-check clean. The warning is the existing Starlette/httpx
TestClient deprecation warning. The single `M3_REVIEW_FIX4_SHA` commit remains
isolated to `codex/v11-m3`; no approved requirement changed; no merge, push, M4,
or M5 action was taken.

M3 fifth-round review-fix5 ledger (2026-08-09): independent review verdict
`CHANGES_REQUIRED` was issued against base
`e52e47984c4b460ed3b06dc4147d3d3b0532345b`; only H1 remained and
Blocking/Medium/Low were zero. The approved §6 contract, V10 legacy boundary,
and fourth-round reservation identity/replay behavior remain unchanged.

The production RED used `_call_model → Agents SDK Runner → RetryCoordinator`
with four provider calls: request one succeeded, request two failed with
transport, and both requests succeeded on the outer retry. The old runtime
counter and execution audit retained only final-attempt `40/10` and the last
failed request estimate. GREEN reuses the existing per-request MODEL event
callback: `input_tokens` remains the reservation billed/estimate value,
`actual_input_tokens` records provider actual input, terminal events are
deduplicated by reservation/status/attempt, successful actual usage accumulates
across outer attempts, AgentExecution aggregates each attempt, and the final
RunResult is not counted again.

| Finding | RED → GREEN evidence | Contract-preserving resolution |
| --- | --- | --- |
| H1 | `test_v11_sdk_outer_retry_replays_success_with_new_reservation_and_reuses_failed_request` (RED `summary=40/10`, GREEN `summary=60/15`); `test_v11_single_sdk_request_usage_is_not_counted_twice`; `test_v11_all_failed_sdk_retry_records_estimate_without_summary_usage`; `test_v11_sdk_retry_resume_usage_is_idempotent` | Durable MODEL events carry actual-vs-billed input distinctly. A per-logical-call accumulator counts each terminal request once, adds only successful actual usage to summary/runtime counters, writes aggregate attempt usage before AgentExecution persistence, retains failed estimates in attempt audit, and keeps duplicate retry/resume settlement idempotent. |

Final fifth-round verification: T7 exact `91 passed`; T8 exact `207 passed`;
M2R-2/M2R-3 and isolation focused `84 passed, 3 skipped`; changed/runtime
focused `102 passed, 2 skipped`; full `uv run pytest -q` `1995 passed, 3
skipped, 1 warning`; both `uv run ruff check .` and `uv run ruff check backend
tests` clean; diff-check clean. The warning is the existing Starlette/httpx
TestClient deprecation warning. The single `M3_REVIEW_FIX5_SHA` commit remains
isolated to `codex/v11-m3`; no approved requirement changed; no merge, push, M4,
or M5 action was taken.

M4 review-fix ledger (2026-08-09): the independent review verdict was
`CHANGES_REQUIRED` against the initial M4 implementation. The approved R1–R27
contract and V10 historical boundary were preserved. Each finding first had a
production-shaped RED regression and then a shared-boundary GREEN fix:

| Finding | RED → GREEN evidence | Contract-preserving resolution |
| --- | --- | --- |
| B1 | `test_v11_report_generation_is_atomic_for_memory_and_sqlite` | Report/action handlers construct a `BusinessMutation`; the common `PhaseCommit` owns repository writes and rollback for both memory and SQLite, including checkpoint/lease fault injection. |
| B2 | `test_runtime_api_rejects_v10_run_on_active_v11_projection`; `test_product_entries_select_v11_when_agent_runtime_is_available`; `test_runtime_api_defaults_to_v11_when_product_runtime_is_available` | Configured product entrypoints and reruns select persisted V11; explicit V10 is fenced from an active V11 projection, while unconfigured historical V10 and legacy-to-V11 linked reruns remain supported. |
| H3 | `test_v11_workbench_uses_private_safe_public_projection` | One shared V11 public projection scrubs private markers for findings, candidates, Critic/Lead review, reports, executions, and graph seeds; the UI applies a second projection before rendering. |
| H4 | `test_v11_report_rejects_unusable_referenced_evidence`; `test_v11_projection_rejects_unusable_evidence`; `test_v11_projection_rejects_cross_run_evidence` | Report and V11 OpenRCA paths share `validate_usable_evidence`, requiring SUCCESS/PARTIAL status and the same durable runtime owner; the V10 legacy projector/validation path is unchanged. |
| M5 | `test_v11_inconclusive_report_does_not_activate_diagnosis_or_actions` | Inconclusive V11 output clears both diagnoses and alternatives, so no candidate output is emitted. |
| M6 | `test_v11_human_verification_cannot_replace_persisted_candidate_refs` | Human verification requests with a persisted V11 owner must retain the original candidate references and same-run action/evidence linkage. |
| M7 | `test_v11_action_planner_rejects_inconsistent_final_statuses` | Action/verification planning requires a legal review/run diagnostic-status pair and matching final Lead decision before producing outputs. |
| M8 | `test_v11_projection_rejects_ownerless_business_artifacts` | Agent evidence/actions/verifications require exactly one shared non-null durable `runtime_run_id` in model validation and aggregate/public projection guards. |
| M9 | `test_v11_product_entry_summary_preserves_safe_lead_and_critic` | Event/manual/list summaries pass the persisted coordination review through the shared V11 public projection, preserving safe Lead/Critic fields. |
| L10 | `test_v11_markdown_renders_actual_findings_tasks_rounds_and_lead_decision` | V11 Markdown renders actual finding actor/instance/task/round/evidence data and the public Lead decision; placeholder actors and private reasoning are absent. |

First-round M4 verification was green: T9 exact `276 passed/1 warning`; T10 exact
`812 passed/3 skipped/1 warning`; full `uv run pytest -q` `2025 passed/3
skipped/1 warning`; offline tool acceptance `rows=80 failed=0`; runtime
acceptance exit 0; `uv run ruff check .`, `uv run ruff check backend tests`,
frontend production build, and `git diff --check` passed. The only test
warning is the existing Starlette/httpx TestClient deprecation; Vite emitted
only its existing `use client` bundle notices. The review-fix is local to
`codex/v11-m4`; no merge or push was performed and independent re-review is
pending.

M4 second-round review-fix ledger (2026-08-09): the independent re-review
reproduced two additional V11 publication defects against the first-round M4
commit. H1 covered the OpenRCA V11 prediction boundary: a private marker in
`affected_entity`, `failure_mechanism`, or `summary` was present in the
`RootCauseAttribution` and `v11-agent-predictions.csv`. The V11 projector now
reuses `backend/services/v11_public.py::public_v11_candidate` before building
the artifact; the V10 deterministic projector and its legacy reason output are
unchanged. M2 covered the shared public guard: contradictory review/run status,
an invalid Lead/status combination, missing `active_runtime_run_id`, and a
missing or mismatched latest Agent run summary were accepted. The shared domain
`validate_v11_final_status` is now used by action planning, reports, and the
V11 owner guard; the guard requires one active/latest durable Agent owner and
fails closed for API, report, graph, and workbench publication. The frontend
mirrors the same status/owner checks as a second defense.

Second-round RED→GREEN evidence includes
`test_v11_prediction_artifact_uses_public_candidate_projection`, the memory and
SQLite `test_v11_projection_guard_rejects_invalid_reloaded_payload` matrix,
`test_v11_api_guard_rejects_inconsistent_review_run_and_missing_active_owner`,
the direct report status regressions, and frontend payload regressions. Final
verification: T9 `279 passed/1 warning`, T10 `830 passed/3 skipped/1 warning`,
full pytest `2045 passed/3 skipped/1 warning`, offline `80/80`, runtime
acceptance `14/14`, Ruff, frontend build, and `git diff --check` all passed.
V10 legacy behavior, the OpenAI-compatible URL adapter, capability gates, and
local-only/offline boundary were not changed; no merge or push was performed.

M4 third-round residual review-fix ledger (2026-08-09): the independent
review reproduced four remaining V11 publication defects. The approved final
status matrix is now explicit and shared: complete↔completed,
partial↔partial, and inconclusive↔completed. Action planning, report
generation, and the public guard reject both crossed combinations before
publishing actions or diagnoses; V10 legacy behavior remains outside this
guard.

The public guard additionally requires the durable RuntimeRun to be exactly
`completed`; created, failed, cancelled, and lease-expired `interrupted` runs
are fail-closed. A shared report projection validator requires diagnosis IDs
to equal the final Lead candidate IDs, alternatives to match the remaining
review candidates, and inconclusive reports to emit neither collection. API
report/workbench/graph publication, memory/SQLite reloads, direct report
projection, and frontend omitted/null/stale active-owner cases are covered by
RED→GREEN regressions. Final verification is T9 `280 passed/1 warning`, T10
`855 passed/3 skipped/1 warning`, full pytest `2071 passed/3 skipped/1
warning`, offline `80/80`, runtime acceptance `14/14`, Ruff/build/diff-check
green; the post-commit runtime artifact is bound with `git_dirty=false` in the
implementation handoff.

M4 fourth-round high-fix ledger (2026-08-10): the independent review reproduced
three production-path High defects against the latest M4 commit. The approved
V11/V10 boundary remains unchanged. The real OpenRCA V11 runner now computes
the execution-contract Skill identity from its actual nine-tool agent manifest,
so Skill admission matches the local provider registry and the real SQLite
fixture path can complete without external telemetry. The V10 deterministic
projector retains its existing tool/reason semantics.

The V11 OpenRCA runner now enters the shared durable V11 publication guard
before constructing prediction or CSV artifacts. Non-terminal or failed
`RuntimeRun` states (`created`, `failed`, `cancelled`, `interrupted`) fail
closed; only `completed` can publish. The shared guard also treats any report
under an active V11 owner as V11-bound, regardless of its authority label, and
rejects stale/foreign diagnosis or alternative references, status mismatches,
owner mismatches, and invalid Lead bindings. Historical V10-only reports still
use the legacy deterministic path.

The required RED→GREEN regressions cover the real runner (`8 passed`), the
active-V11 legacy-report boundary in memory and SQLite plus API/workbench
publication (`4 passed`), and the combined focused source suite (`160 passed/1
warning`). Final verification is M4 focused `124 passed/1 warning`, T9 exact
`288 passed/1 warning`, T10 exact `868 passed/3 skipped/1 warning`, full pytest
`2084 passed/3 skipped/1 warning`, offline `80/80`, runtime acceptance `14/14`
with privacy scan clean, both Ruff commands, frontend build, and diff-check
green. The post-commit runtime artifact is bound with `git_dirty=false` in the
implementation handoff; no merge or push was performed and M5 was not started.

M4 fifth-round final High-fix ledger (2026-08-10): the independent review
reproduced the last publication bypass: the shared V11 guard validated the
durable runtime and final Agent review but not `InvestigationRecord.status`.
The approved publication matrix is now explicit at both lifecycle layers:
V11 complete/partial/inconclusive output requires
`InvestigationStatus.COMPLETED` and `RuntimeRunStatus.COMPLETED`; failed,
cancelled, running, pending, created, or interrupted state fails closed.

The lifecycle check is part of the shared V11 domain/publication contract used
by the guard, rather than an API-only filter. Memory/SQLite reload, report,
coordination-review, graph/workbench, and API regressions cover the public
boundary. The OpenRCA V11 runner calls the same guard before projection, and
its CSV writer refuses to serialize root causes for any non-success outcome,
writing an empty prediction plus safe `failure_category` metadata. The V10
deterministic projector/writer and V10-only legacy report behavior are
unchanged.

Required RED→GREEN evidence is guard/API `12 failed` then `12 passed`, real
OpenRCA failed-investigation CSV `1 failed` then `1 passed`, affected
OpenRCA runner/projection `45 passed`, M4 focused `137 passed/1 warning`, T9
`289 passed/1 warning`, T10 `881 passed/3 skipped/1 warning`, and full pytest
`2097 passed/3 skipped/1 warning`. Offline `80/80`, Ruff, frontend build, and
diff-check passed; post-commit runtime acceptance is recorded in the final
handoff with `git_dirty=false`. No merge or push was performed and M5 was not
started.

## 16. Approval state

- Requirements Brief: confirmed by user.
- Architecture choice: scheme B confirmed by user.
- The 2026-08-02 amended specification was approved by the user after the
  local-first tool, trace, Skill, evaluation-isolation, and OpenAI-compatible
  model-provider scope review; its requirements remain the approved contract.
- The legacy-reuse audit materially changes runtime phase identity, V11 entry
  requirements, supplemental-finding validation, tool exposure, projection run
  ownership, and failure contracts. The pre-audit implementation Plan is stale
  and must be rewritten from this independently reviewed amended Spec;
  L22–L30 passed focused re-review and the user approved the amended Spec on
  2026-08-02.
- Post-approval plan review added two non-behavioral contract clarifications:
  the single durable `execution_contract` JSON location and the mandatory
  round-two Critic reconciliation. Both close already-approved invariants and
  introduce no new product scope.
- The implementation Plan was rewritten from this specification, independently
  reviewed, and approved by the user on 2026-08-02 with execution authorization.
- Implementation: M2 baseline `c2245ac` and the M3 review-fix history remain
isolated; M4 review-fix is RED→GREEN verified in isolated branch
`codex/v11-m4`, with its local commit SHA recorded in the implementation
handoff. No merge or push was performed; M5 has not started.
