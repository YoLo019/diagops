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

Keep updates concise and concrete. This thread is the main/control thread for planning, orchestration, and future requirement changes.

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

Known warning:

FastAPI/TestClient may emit a Starlette deprecation warning. It is not blocking unless it becomes a failure or the dependency stack changes.

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
