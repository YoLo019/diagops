# SRE RCA Agent Technology Stack And Components

## 1. Selection Principles

The MVP should be easy to understand, easy to run locally, and easy to replace with real observability integrations later.

Technology choices should follow these principles:

1. Start with a small Python backend and mock providers.
2. Keep the agent workflow deterministic before adding complex LLM behavior.
3. Use provider interfaces so mock data can later be replaced by Prometheus, Loki, ELK, CI/CD, and service catalog integrations.
4. Store investigation records and evidence in a simple relational database first.
5. Keep the UI operational and compact, focused on incident reports and evidence timelines.
6. Avoid production automation and remediation in the MVP.

## 2. Recommended MVP Stack

### Backend

| Area | Choice | Reason |
| --- | --- | --- |
| Language | Python 3.11+ | Good ecosystem for APIs, agent workflows, data processing, and LLM integrations. |
| API framework | FastAPI | Simple async HTTP API, strong typing with Pydantic, good OpenAPI docs. |
| Data validation | Pydantic | Fits FastAPI and keeps event, evidence, hypothesis, and report schemas explicit. |
| Persistence | SQLite for local MVP, PostgreSQL-ready schema | SQLite keeps the first version light; SQLAlchemy can later target PostgreSQL. |
| ORM / database access | SQLAlchemy 2.x | Mature relational modeling and migration path to PostgreSQL. |
| Migrations | Alembic | Standard migration tool for SQLAlchemy. |
| Background jobs | FastAPI BackgroundTasks first | Enough for local MVP diagnosis jobs; can later move to Celery, RQ, or a message queue. |
| Testing | pytest | Simple, common, and works well for golden incident cases. |
| Dependency management | uv | Fast Python dependency and lockfile workflow. |

### Agent And RCA Logic

| Area | Choice | Reason |
| --- | --- | --- |
| MVP orchestration | Custom diagnosis orchestrator | Keeps control clear and avoids premature framework complexity. |
| RCA analyzer | Rule-based scoring first | Easier to test and explain than pure LLM reasoning. |
| LLM usage | Optional report synthesis layer | LLM can polish explanations after facts and hypotheses are structured. |
| Future agent framework | LangGraph or OpenAI Agents SDK | Add later if multi-step agent state, tool calls, and branching workflows become complex. |
| Tool abstraction | Provider interfaces | Keeps observability tools replaceable. |

### Frontend

| Area | Choice | Reason |
| --- | --- | --- |
| MVP UI option | React + TypeScript + Vite | Standard lightweight app stack for dashboards and operational tools. |
| Styling | Tailwind CSS or plain CSS modules | Fast to build compact operational UI. |
| Data fetching | TanStack Query | Good for investigation lists, details, refresh, and async job status. |
| Charts / timeline | Recharts first | Enough for basic metric trends and incident timelines. |
| Component style | Small internal components | Avoid a large design system until workflows stabilize. |

If speed matters more than UI polish, the first UI can be a FastAPI-served minimal HTML page. React should be used once the investigation list, detail page, and timeline need richer interaction.

### Storage

| Data | MVP Storage | Future Storage |
| --- | --- | --- |
| Investigation sessions | SQLite | PostgreSQL |
| Evidence items | SQLite JSON columns | PostgreSQL JSONB |
| Reports | SQLite text / JSON | PostgreSQL + object storage if needed |
| Mock incident cases | JSON files | Database or fixture registry |
| Runtime logs | Local logs | Loki / ELK / cloud logging |

### Deployment

| Area | MVP Choice | Future Choice |
| --- | --- | --- |
| Local runtime | uv + FastAPI dev server | Docker Compose |
| Packaging | Python package layout | Container image |
| Database | SQLite file | PostgreSQL container or managed database |
| Configuration | `.env` | Secret manager / deployment config |
| Production runtime | Not required for MVP | Kubernetes, VM, or platform service |

## 3. Core Components

### 3.1 Event Intake Layer

Purpose:

Receives incident events from simulated cases, Webhook requests, and manual investigation requests.

Main responsibilities:

1. Validate incoming event payloads.
2. Normalize service name, environment, severity, time window, and symptoms.
3. Create an investigation record.
4. Trigger diagnosis.

Recommended modules:

```text
backend/api/events.py
backend/domain/events.py
backend/services/event_intake.py
```

### 3.2 Diagnosis Orchestrator

Purpose:

Controls the RCA workflow for each investigation.

Main responsibilities:

1. Decide which evidence providers to query.
2. Query providers with a consistent time window.
3. Store collected evidence.
4. Call the RCA analyzer.
5. Call the report generator.
6. Persist the final report.

Recommended modules:

```text
backend/diagnosis/orchestrator.py
backend/diagnosis/context.py
backend/diagnosis/workflow.py
```

### 3.3 Evidence Providers

Purpose:

Hide data source details behind stable interfaces.

MVP provider interfaces:

```text
LogProvider
MetricProvider
DeployProvider
DependencyProvider
ServiceCatalogProvider
```

MVP implementations:

```text
MockLogProvider
MockMetricProvider
MockDeployProvider
MockDependencyProvider
MockServiceCatalogProvider
```

Future implementations:

```text
PrometheusMetricProvider
LokiLogProvider
ElkLogProvider
GitHubDeployProvider
GitLabDeployProvider
JenkinsDeployProvider
KubernetesProvider
```

Recommended modules:

```text
backend/providers/base.py
backend/providers/mock_logs.py
backend/providers/mock_metrics.py
backend/providers/mock_deploys.py
backend/providers/mock_dependencies.py
backend/providers/mock_service_catalog.py
```

### 3.4 RCA Analyzer

Purpose:

Converts evidence into ranked root-cause hypotheses.

MVP strategy:

Use deterministic rules and weighted scoring.

Initial rules:

1. If errors start soon after deployment and new exception groups appear, rank deployment regression high.
2. If QPS rises sharply before latency and errors rise, rank traffic spike high.
3. If dependency latency or error rate rises before local errors, rank downstream dependency failure high.
4. If database latency rises and affected endpoints share database access, rank database slowdown high.
5. If one instance has abnormal CPU, memory, or error rate, rank single instance issue high.
6. If evidence is incomplete or conflicting, rank unknown with specific next checks.

Recommended modules:

```text
backend/rca/analyzer.py
backend/rca/rules.py
backend/rca/scoring.py
backend/rca/hypotheses.py
```

### 3.5 Report Generator

Purpose:

Generates a readable incident report for engineers.

MVP report output:

1. Summary.
2. Timeline.
3. Most likely cause.
4. Alternative hypotheses.
5. Evidence table.
6. Suggested next actions.
7. Confidence and uncertainty.

LLM use is optional. The first version can generate reports from templates. Later, an LLM can improve wording, but it must not invent evidence.

Recommended modules:

```text
backend/reports/generator.py
backend/reports/templates.py
backend/reports/markdown.py
```

### 3.6 Persistence Layer

Purpose:

Stores investigations, evidence, hypotheses, and reports.

Recommended modules:

```text
backend/db/session.py
backend/db/models.py
backend/db/repositories.py
backend/db/migrations/
```

Core tables:

```text
investigations
evidence_items
hypotheses
reports
```

### 3.7 API Layer

Purpose:

Exposes the platform operations.

MVP endpoints:

```text
POST /events
POST /events/simulated/{case_id}
POST /investigations/manual
GET  /investigations
GET  /investigations/{id}
GET  /investigations/{id}/report
```

Recommended modules:

```text
backend/api/events.py
backend/api/investigations.py
backend/api/health.py
```

### 3.8 Frontend UI

Purpose:

Lets engineers trigger simulated cases, submit manual investigations, and review RCA reports.

MVP views:

1. Investigation list.
2. Investigation detail.
3. Evidence timeline.
4. Hypothesis and confidence panel.
5. Simulated incident launcher.
6. Manual investigation input.

Recommended structure:

```text
frontend/src/pages/InvestigationsPage.tsx
frontend/src/pages/InvestigationDetailPage.tsx
frontend/src/components/EvidenceTimeline.tsx
frontend/src/components/HypothesisPanel.tsx
frontend/src/components/SimulatedIncidentLauncher.tsx
frontend/src/api/client.ts
```

## 4. Observability Integrations Roadmap

### MVP

Use mock providers and JSON fixture data.

### Phase 2

Add one real metric provider and one real log provider:

1. Prometheus for metrics.
2. Loki or ELK for logs.

### Phase 3

Add release and ownership context:

1. GitHub Actions, GitLab CI, Jenkins, or ArgoCD deployment events.
2. Service catalog with owners, dependencies, and runtime metadata.

### Phase 4

Add deeper infrastructure context:

1. Kubernetes pod status and events.
2. Instance-level resource signals.
3. Tracing through Jaeger, SkyWalking, or Tempo.

## 5. LLM And Agent Strategy

The MVP should not depend on LLM reasoning for core correctness.

Recommended split:

1. Rules decide hypotheses and confidence.
2. Structured evidence is stored and cited.
3. Templates generate a complete report.
4. LLM optionally rewrites the report for clarity.

This protects the system from hallucinating root causes.

Future LLM capabilities:

1. Natural language incident parsing.
2. Follow-up question generation.
3. Report summarization.
4. Code diff explanation.
5. Runbook recommendation.

## 6. What Not To Add In The MVP

Avoid these until the evidence loop is useful:

1. Full Kubernetes diagnosis.
2. Full multi-agent runtime.
3. Automatic rollback or restart.
4. Message queue infrastructure.
5. Real-time streaming UI.
6. Complex permission system.
7. Large design system.
8. Vector database.

These may become useful later, but they are not needed to prove the first RCA workflow.

## 7. Suggested Repository Layout

```text
backend/
  api/
  db/
  diagnosis/
  domain/
  providers/
  rca/
  reports/
  services/
  main.py

frontend/
  src/
    api/
    components/
    pages/
    main.tsx

data/
  incidents/
  mock_logs/
  mock_metrics/
  mock_deploys/

tests/
  api/
  diagnosis/
  providers/
  rca/
  reports/
  golden/

docs/
  superpowers/
    specs/
    plans/
```

## 8. First Implementation Recommendation

Start with the backend only:

1. FastAPI app.
2. Pydantic domain schemas.
3. JSON mock incident cases.
4. Mock providers.
5. Rule-based RCA analyzer.
6. Markdown report generator.
7. API tests and golden case tests.

After the backend loop works, add the frontend detail page and simulated incident launcher.

This keeps the first version focused: prove that an event can enter the system and produce a useful, evidence-backed RCA report.

