# DiagOps Agent Instructions

This file is for AI coding agents working in this repository.

## Project Identity

DiagOps is an event-driven SRE RCA agent platform.

The long-term goal is not a generic chatbot. The goal is an operations incident platform where problems enter automatically through alerts, logs, webhooks, or engineer input; the agent gathers evidence; multiple specialists analyze the incident; and the platform returns an evidence-backed conclusion, suggested actions, approval state, verification suggestions, and a report.

The intended long-term loop is:

```text
problem enters platform
  -> agent decides what evidence to collect
  -> logs, metrics, deployments, dependencies, machine status, and ownership context are gathered
  -> specialist agents collaborate on root cause analysis
  -> likely causes are ranked with evidence
  -> recommendations are generated with risk and approval requirements
  -> humans review, approve, verify, and later may allow low-risk automation
```

## Current Direction

V1 is a backend MVP for evidence-backed RCA.

V2 is a platform-loop prototype. It should add investigation state, failed-investigation persistence, recommended actions, approval status, verification suggestions, provider failure semantics, and lightweight specialist-agent boundaries.

V2 must not automatically modify production systems.

## Primary Design References

Use these projects as design references only. Do not copy their code blindly.

1. OpenDerisk
   - Borrow: RCA-centered multi-agent thinking, evidence chains, coordinator plus specialists, explainable diagnosis.
   - Simplify: do not introduce a full complex multi-agent runtime before the DiagOps evidence model is stable.

2. ITOps Agent Platform
   - Borrow: alert-to-diagnosis-to-recommendation loop, persisted task states, failed-state visibility, approval and verification concepts, safety gates.
   - Simplify: do not build broad infrastructure management, SSH execution, mobile approval, or automatic remediation in V2.

## Communication

Use Chinese when communicating with the user unless the user asks otherwise.

Keep updates concise and concrete. 

## Repository And Git Rules

Remote:

```text
git@github.com:YoLo019/diagops.git
```

Use SSH for future pushes.

Git author:

```text
moon <1264359523@qq.com>
```

Default development branch prefix:

```text
codex/
```

Do not push directly to `main` unless the user explicitly asks. Work on feature branches such as `codex/mvp-backend`.

Keep commits focused and granular. Do not mix unrelated refactors with feature work.

Never revert user changes unless explicitly instructed.

## Technical Stack

Backend:

1. Python
2. FastAPI
3. Pydantic
4. pytest
5. ruff
6. uv

Current persistence is in-memory for the MVP. V2 may introduce stronger repository semantics before moving to SQLite/PostgreSQL.

Agent strategy:

1. Keep the project-owned RCA framework as the source of truth.
2. Use structured domain models, provider interfaces, evidence items, hypotheses, reports, and golden tests.
3. Use a lightweight coordinator plus specialists before adding a full agent SDK.
4. OpenAI Agents SDK is the preferred future execution layer when real multi-agent behavior is needed.
5. CrewAI is not the core framework for this project.
6. Consider LangGraph only if the workflow becomes a stateful graph with retries, human checkpoints, resume, or long-running branches.

## Core Architecture Principles

Evidence first:

1. Every conclusion must cite evidence.
2. Every recommended action must cite evidence.
3. Missing or failed evidence must be visible.
4. Reports must separate facts, inferences, recommendations, and uncertainty.

Structured data first:

1. Prefer Pydantic/domain models over ad hoc dictionaries.
2. Provider outputs must be structured.
3. Hypotheses must reference evidence ids, not free text only.
4. Reports may be Markdown, but the source of truth must remain structured objects.

Determinism first:

1. Use rule-based scoring for core RCA behavior while the evidence model is evolving.
2. LLMs may help summarize or explain, but must not invent facts.
3. Golden cases should protect the expected RCA behavior.

Small boundaries:

1. Keep event intake, diagnosis orchestration, providers, analyzer, action planning, reports, repositories, and APIs separate.
2. Add abstractions only when they clarify responsibilities or reduce real duplication.
3. Avoid building broad platform features that do not serve application-service incident RCA.

## Domain Model And Protocol Contracts

Agents must treat domain models as contracts, not suggestions.

Core objects:

```text
IncidentEvent
Investigation
EvidenceItem
Hypothesis
IncidentReport
RecommendedAction
VerificationSuggestion
ProviderResult
SpecialistResult
```

Minimum protocol rules:

1. `IncidentEvent` must include source, service, environment, severity, title, description, started_at, and time_window_minutes. Defaults are allowed only when documented by the domain model.
2. `Investigation` must include id, source, service, environment, severity, title, status, created_at, updated_at, and optional failure_reason.
3. `EvidenceItem` must include id, provider, kind, status, timestamp when available, summary, payload, confidence, and optional error_message.
4. `Hypothesis` must include cause_type, summary, confidence, supporting_evidence_ids, contradicting_evidence_ids, and next_actions.
5. `IncidentReport` must include summary, markdown, hypotheses, and evidence-linked sections.
6. `RecommendedAction` must include action_type, title, description, risk_level, requires_approval, status, and supporting_evidence_ids.
7. `VerificationSuggestion` must include title, description, expected_signal, status, and optional result_note.
8. `ProviderResult` must include provider, status, evidence_items, error_message, and duration_ms.
9. `SpecialistResult` must include agent_name, status, evidence_items, summary, errors, and duration_ms.

Reference integrity rules:

1. Every supporting evidence id must point to an existing `EvidenceItem`.
2. Every contradicting evidence id must point to an existing `EvidenceItem`.
3. Every recommended action evidence id must point to an existing `EvidenceItem`.
4. Report text may quote evidence summaries, but report correctness is judged by structured ids first.
5. If evidence is missing, create explicit missing/provider-error evidence instead of silently omitting the gap.

State transition rules:

1. Investigation: `pending -> running -> completed | failed | cancelled`.
2. Recommended action: `proposed -> approved | rejected | skipped`, and `approved -> done` only when an external human-recorded result exists.
3. Verification suggestion: `pending -> passed | failed | skipped`.
4. Invalid state transitions must be rejected by domain or service logic.

Schema discipline:

1. Add new fields as optional or with safe defaults unless a migration/spec explicitly says otherwise.
2. Do not remove or rename public fields without updating specs, tests, examples, and compatibility notes.
3. Do not store non-JSON-compatible payload values such as NaN or Infinity.
4. Do not use free-form strings where an enum is already defined.

## V2 Safety Rules

V2 is read-only with respect to production systems.

Do not implement:

1. SSH command execution.
2. Automatic rollback.
3. Automatic restart.
4. Automatic scaling.
5. Automatic configuration changes.
6. Direct calls to real production mutation APIs.

Recommended actions are allowed, but they are suggestions only.

Risk levels:

```text
read_only
low
medium
high
```

Medium and high risk actions must require approval. In V2, approval changes state only; it does not execute the action.

Reports must not say an action was executed or a service was fixed unless there is a recorded verification result.

## Investigation State Rules

Investigations should be stateful.

Expected states:

```text
pending
running
completed
failed
cancelled
```

Rules:

1. Create an investigation before running diagnosis.
2. Mark it `running` while collecting evidence.
3. Mark it `completed` only after evidence, hypotheses, report, actions, and verification suggestions are persisted.
4. Mark it `failed` if diagnosis cannot complete.
5. Persist `failure_reason`; failed investigations must be queryable.
6. Provider failures should usually produce provider error evidence instead of failing the whole investigation.

## Multi-Agent Rules

The coordinator owns the investigation flow.

Specialists may include:

1. Log Analyst
2. Metric Analyst
3. Deploy Analyst
4. Dependency Analyst
5. Service Catalog Analyst
6. Report Writer

Specialists must return structured results:

```text
agent_name
status
evidence_items
summary
errors
duration_ms
```

Specialists must not:

1. Invent evidence.
2. Execute remediation.
3. Override the project-owned domain model.
4. Hide provider errors.

## Testing Requirements

Before claiming work is complete, run fresh verification.

Default checks:

```bash
uv run ruff check .
uv run pytest -v
```

For RCA behavior, add or update focused tests and golden cases.

Golden tests should check:

1. Correct top cause type.
2. Required evidence ids are cited.
3. Report sections contain the expected evidence in the correct section.
4. Recommended actions cite valid evidence.
5. Medium/high risk actions require approval.
6. Failed investigations persist failure reason.

## Test And Fixture Standards

Tests are part of the RCA contract. Do not weaken tests to make an implementation pass.

Fixture rules:

1. Mock incident cases must be deterministic.
2. Fixture ids should be stable and human-readable when they are asserted in golden tests.
3. Fixture timestamps must be explicit and timezone-aware when relevant.
4. Fixture payloads must be valid JSON.
5. Add a fixture version field when changing the shape of incident or provider fixture files.
6. Keep simulated cases close to real operational stories: symptom, time window, service, environment, signals, and expected cause.

Required test coverage for new behavior:

1. Happy path.
2. Missing input or invalid input.
3. Provider `failed`.
4. Provider `partial`.
5. Low-confidence or unknown root cause.
6. Evidence reference validation.
7. Recommended action risk and approval rules.
8. Failed investigation persistence.
9. Report section placement for key evidence.
10. Backward compatibility for existing examples and golden cases.

Data quality checks:

1. Do not rely only on broad substring checks over the full report.
2. Prefer structured matchers for provider, kind, payload keys, cause type, status, and evidence ids.
3. When checking Markdown, extract the specific section before asserting section-specific content.
4. Golden tests should fail if the top hypothesis, cited evidence, or action risk changes unexpectedly.

Known warning:

FastAPI/TestClient may emit a Starlette deprecation warning. It is not blocking unless it becomes a failure or the dependency stack changes.

## Failure And Degradation Rules

DiagOps must fail visibly and preserve investigation context.

Provider failures:

1. A single provider failure should usually produce provider-error evidence and allow diagnosis to continue.
2. Multiple provider failures may lower confidence and must appear in the report.
3. If all core providers fail, mark the investigation `failed`.
4. Provider timeout, malformed provider payload, and empty provider result should be distinguishable.

Analyzer failures:

1. If the analyzer cannot rank a specific cause, return an `unknown` hypothesis with low confidence and next checks.
2. If analyzer execution raises unexpectedly, mark the investigation `failed` and persist failure_reason.
3. Never convert an internal exception into a confident RCA conclusion.

Report failures:

1. If report generation fails after evidence and hypotheses are produced, mark the investigation `failed`.
2. Persist enough failure_reason context for debugging.
3. Do not return a partial report as completed unless the report explicitly states it is partial and tests cover that behavior.

Action planner failures:

1. Action planner failure should not erase a valid RCA result.
2. The investigation may still complete if evidence, hypotheses, and report are valid.
3. The report must indicate that recommendations could not be generated.

Manual/API failures:

1. API validation failures should return 4xx and should not create investigations.
2. Failures after investigation creation must be visible through `GET /investigations/{id}`.
3. Error responses should be concise and not leak secrets.

## Platform Observability

The operations agent platform must be observable itself.

Logging rules:

1. Log investigation id for every investigation lifecycle step.
2. Log provider name, status, duration_ms, and evidence count for every provider call.
3. Log specialist name, status, duration_ms, and error count for every specialist run.
4. Log analyzer duration and top cause type.
5. Log report generation duration.
6. Log action planner duration and action count.

Trace/correlation rules:

1. Every request-created investigation should have a request id or correlation id.
2. Background diagnosis logs must include the investigation id.
3. Provider and specialist results should carry enough metadata to reconstruct the diagnosis path.

Metrics to preserve or expose when practical:

1. Investigation count by status.
2. Diagnosis duration.
3. Provider failure count by provider.
4. Top cause distribution.
5. Action count by risk level.
6. Report generation failures.

Operational debugging:

1. Do not swallow exceptions without logging context.
2. Do not log secrets, tokens, raw credentials, or private keys.
3. If adding real provider integrations, redact sensitive request and response fields.

## Versioning And Data Evolution

Assume investigation records, fixtures, reports, and API responses will evolve.

Versioning rules:

1. Add `schema_version` to new persistent or fixture formats when practical.
2. API-breaking changes require spec updates, tests, and README/example updates.
3. Report format changes that affect tests require golden test updates in the same change.
4. Fixture shape changes require migration notes in the relevant spec or plan.
5. Do not silently change enum values; add new values and preserve old values when possible.

Compatibility rules:

1. Prefer additive changes.
2. Prefer optional fields with defaults over required fields in existing public schemas.
3. Keep existing API examples working unless a versioned change explicitly replaces them.
4. If a new domain object supersedes an old one, keep adapter/translation logic until tests and examples are moved.

Migration rules:

1. Any persistent storage migration must be reversible or clearly documented.
2. Migrations must preserve existing investigation readability.
3. If persistence changes from in-memory to SQLite/PostgreSQL, tests must cover repository behavior and status transitions.

## Documentation Rules

Keep product decisions in:

```text
docs/superpowers/specs/
```

Keep implementation plans in:

```text
docs/superpowers/plans/
```

When creating a new major version or feature, write the spec first, get user approval, then write an execution plan.

Do not let implementation drift away from the accepted spec without updating the spec or explicitly noting the deviation.

## Current Key Specs

V1 design:

```text
docs/superpowers/specs/2026-07-03-sre-rca-agent-design.md
```

Technology stack:

```text
docs/superpowers/specs/2026-07-03-sre-rca-agent-tech-stack.md
```

V2 platform-loop design:

```text
docs/superpowers/specs/2026-07-04-diagops-v2-platform-loop-design.md
```

## Development Workflow

For substantial work:

1. Read the relevant spec.
2. Inspect existing code before proposing changes.
3. Keep changes scoped.
4. Prefer tests before or alongside behavior changes.
5. Run verification before committing.
6. Commit with a focused message.

For review tasks:

1. Findings first.
2. Order by severity.
3. Include file and line references.
4. Focus on bugs, regressions, missing tests, safety risks, and spec mismatches.

## Boundaries To Preserve

Do not turn DiagOps into:

1. A generic chat assistant.
2. A broad infrastructure management platform.
3. A command execution system before safety gates exist.
4. A pure LLM diagnosis toy without structured evidence.
5. A copy of OpenDerisk or ITOps Agent Platform.

DiagOps should stay focused on application-service incident diagnosis and evidence-backed operational decision support.
