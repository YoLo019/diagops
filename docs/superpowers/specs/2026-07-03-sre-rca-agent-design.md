# SRE RCA Agent MVP Design

## 1. Background

This project is a lightweight OpenDerisk-style operations agent. Its first goal is not automatic remediation, but fast and evidence-based root cause analysis for application service incidents.

The user may not have deep operations experience, so the system should hide most infrastructure details behind a simple workflow:

1. An incident event enters the platform.
2. The agent collects relevant evidence.
3. The agent identifies likely causes.
4. The platform shows a concise report with supporting evidence and suggested next actions.

The first version focuses on application service incidents, because they are closest to common development workflows and can be modeled with logs, metrics, deployment records, and dependency status.

## 2. Product Goal

Build an event-driven application service RCA agent platform.

The platform should support two entry modes:

1. Automatic event input: simulated incidents and Webhook events trigger analysis without manual prompting.
2. Manual investigation input: engineers can describe a problem in natural language and ask the agent to investigate.

The MVP should prove this core loop:

```text
Incident event
  -> evidence collection
  -> root cause hypothesis
  -> evidence-backed report
  -> engineer review
```

## 3. Non-Goals

The MVP will not:

1. Automatically modify production systems.
2. Execute rollback, restart, scaling, or configuration changes.
3. Integrate with every observability platform.
4. Implement a full multi-agent distributed runtime.
5. Diagnose all infrastructure classes, such as Kubernetes scheduling, Linux kernel issues, network packet loss, or storage faults.

These can be added later after the RCA loop is reliable.

## 4. Target Users

### Primary User

Application engineers who own services and need help answering:

- What is wrong?
- When did it start?
- Is it related to a release?
- Is it caused by traffic, dependency failure, code error, or resource pressure?
- What should I check or do next?

### Secondary User

Operations or SRE engineers who want a structured incident summary before deeper manual investigation.

## 5. MVP Scope

The MVP will support application service incidents with these example scenarios:

1. Deployment causes increased 5xx errors.
2. QPS spike causes latency and timeout growth.
3. Downstream dependency becomes slow or unavailable.
4. Database query latency increases.
5. One machine or instance behaves abnormally.

The first data sources are mock providers, but they must use the same interfaces that real providers will use later.

## 6. OpenDerisk Ideas To Reference

The project should borrow OpenDerisk's ideas at the design level, while keeping the implementation smaller.

### Keep

1. RCA-centered workflow.
2. Evidence-based conclusions.
3. Clear separation between event intake, data collection, reasoning, report generation, and visualization.
4. Role boundaries inspired by SRE-Agent, Data-Agent, Code-Agent, ReportAgent, and Vis-Agent.
5. Evaluation through incident cases.

### Simplify

1. Use one diagnosis orchestrator instead of a full multi-agent runtime.
2. Use mock data providers first.
3. Use structured reports instead of complex visual protocols.
4. Treat code analysis as a later extension.
5. Keep remediation as a human-approved future capability.

## 7. System Architecture

```text
Event Sources
  - Simulated incident cases
  - Webhook events
  - Manual investigation requests

        |
        v

Event Intake Layer
  - Validates input
  - Normalizes incident fields
  - Creates an investigation session

        |
        v

Diagnosis Orchestrator
  - Determines what evidence is needed
  - Calls provider interfaces
  - Builds root cause hypotheses
  - Requests a report

        |
        v

Evidence Providers
  - LogProvider
  - MetricProvider
  - DeployProvider
  - DependencyProvider
  - ServiceCatalogProvider

        |
        v

RCA Analyzer
  - Correlates time windows
  - Compares before/after signals
  - Scores likely causes
  - Produces evidence-linked hypotheses

        |
        v

Report Generator
  - Summary
  - Timeline
  - Root cause candidates
  - Evidence
  - Suggested next actions
  - Confidence and uncertainty

        |
        v

Platform UI / API
  - Investigation list
  - Report detail
  - Evidence timeline
  - Manual follow-up questions
```

## 8. Core Components

### 8.1 Event Intake

Receives incident events from simulated cases, Webhook requests, or manual user input.

Responsibilities:

1. Validate required fields.
2. Normalize service name, environment, severity, timestamps, and symptoms.
3. Create an investigation ID.
4. Trigger diagnosis.

Initial event fields:

```json
{
  "source": "simulated | webhook | manual",
  "service": "payment-service",
  "environment": "prod",
  "severity": "critical",
  "title": "5xx error rate increased",
  "description": "payment-service started returning many 500 responses",
  "started_at": "2026-07-03T14:03:00+08:00",
  "time_window_minutes": 30,
  "signals": {
    "error_rate": "high",
    "latency": "normal",
    "qps": "normal"
  }
}
```

### 8.2 Diagnosis Orchestrator

Coordinates the investigation.

Responsibilities:

1. Decide which providers to query based on the incident type.
2. Use a consistent time window around the incident.
3. Collect evidence.
4. Send evidence to the RCA analyzer.
5. Send analyzer output to the report generator.

The orchestrator should be deterministic where possible. LLM reasoning should be used for explanation and synthesis, not for silently inventing missing facts.

### 8.3 Evidence Providers

All providers expose structured data. The MVP providers are mock implementations.

Provider interfaces:

1. LogProvider: returns error logs, exception groups, sample stack traces, and log trend summaries.
2. MetricProvider: returns QPS, error rate, latency, CPU, memory, and instance-level trends.
3. DeployProvider: returns recent deployments, versions, commit IDs, authors, and changed modules.
4. DependencyProvider: returns dependency latency, error rate, and availability.
5. ServiceCatalogProvider: returns service owner, runtime, dependencies, and alert routing metadata.

### 8.4 RCA Analyzer

Builds hypotheses from evidence.

Initial hypothesis types:

1. Recent deployment regression.
2. Traffic spike or overload.
3. Downstream dependency failure.
4. Database or storage slowdown.
5. Single instance resource issue.
6. Unknown, needs manual follow-up.

Each hypothesis must include:

1. Cause type.
2. Confidence score.
3. Supporting evidence.
4. Contradicting evidence.
5. Suggested next checks.

Example:

```json
{
  "cause_type": "deployment_regression",
  "confidence": 0.84,
  "summary": "The issue is likely caused by the 14:00 deployment of payment-service v1.8.2.",
  "supporting_evidence": [
    "5xx rate increased at 14:03, three minutes after deployment",
    "new NullPointerException appears after the deployment",
    "QPS stayed within normal range",
    "CPU and memory stayed normal"
  ],
  "contradicting_evidence": [
    "dependency latency also rose slightly, but after the service errors started"
  ],
  "next_actions": [
    "Review the payment confirmation handler changed in v1.8.2",
    "Consider rollback if the error rate is still elevated",
    "Check whether the new payment channel config contains null values"
  ]
}
```

### 8.5 Report Generator

Generates a readable incident report.

Required report sections:

1. Executive summary.
2. Incident timeline.
3. Most likely root cause.
4. Alternative hypotheses.
5. Evidence table.
6. Suggested next actions.
7. Confidence and uncertainty.

The report must clearly distinguish observed facts from inferred conclusions.

## 9. Data Flow

### Simulated Event Flow

```text
User selects simulated incident
  -> Event Intake normalizes event
  -> Diagnosis Orchestrator queries mock providers
  -> RCA Analyzer scores hypotheses
  -> Report Generator creates report
  -> Platform displays investigation result
```

### Webhook Event Flow

```text
Monitoring system sends POST /events
  -> Event Intake validates payload
  -> Investigation is created
  -> Diagnosis runs asynchronously
  -> Report is saved
  -> Engineer can view the result or receive notification
```

### Manual Investigation Flow

```text
Engineer asks: "payment-service has many 500s after 14:00"
  -> System extracts service, symptom, time window
  -> Missing fields are inferred or requested
  -> Diagnosis runs with the same orchestrator
  -> Report is shown in the platform
```

## 10. Storage Model

The MVP should persist investigation sessions and reports.

Core entities:

### Investigation

```text
id
source
service
environment
severity
title
description
started_at
time_window
status
created_at
updated_at
```

### EvidenceItem

```text
id
investigation_id
provider
kind
timestamp
summary
payload_json
confidence
```

### Hypothesis

```text
id
investigation_id
cause_type
summary
confidence
supporting_evidence_ids
contradicting_evidence_ids
next_actions
```

### Report

```text
id
investigation_id
summary
timeline_json
hypotheses_json
markdown
created_at
```

## 11. Error Handling

The agent must degrade gracefully.

Rules:

1. If a provider fails, the report should mention missing evidence.
2. If required incident fields are missing, the system should use defaults or ask for clarification in manual mode.
3. If evidence is contradictory, the report should show multiple hypotheses instead of forcing one conclusion.
4. If confidence is low, the report should recommend specific next checks.
5. The system must not claim certainty when evidence is weak.

## 12. API Sketch

### Create Event

```http
POST /events
```

Creates an investigation from a Webhook or simulated event.

### List Investigations

```http
GET /investigations
```

Returns recent investigations and their status.

### Get Investigation

```http
GET /investigations/{id}
```

Returns event details, evidence, hypotheses, and report.

### Manual Ask

```http
POST /investigations/manual
```

Creates an investigation from natural language text.

## 13. UI Sketch

The first UI should be operational and compact, not marketing-like.

Views:

1. Investigation list: status, service, severity, title, started time, likely cause.
2. Investigation detail: summary, timeline, evidence, hypotheses, suggested actions.
3. Simulated incident launcher: buttons to trigger predefined cases.
4. Manual investigation input: text box for asking the agent to investigate.

The detail page should make the evidence chain easy to scan:

```text
14:00 deployment v1.8.2
14:03 5xx starts rising
14:04 NullPointerException appears
14:05 /pay/confirm dominates errors
14:06 QPS remains normal
```

## 14. Testing And Evaluation

The MVP should include golden incident cases.

Each case defines:

1. Input event.
2. Mock logs.
3. Mock metrics.
4. Mock deployments.
5. Expected primary cause type.
6. Expected key evidence.

Initial cases:

1. Deployment regression.
2. Traffic spike.
3. Downstream dependency timeout.
4. Database slowdown.
5. Single bad instance.

Evaluation checks:

1. The correct cause type is ranked first.
2. The report cites required evidence.
3. The report does not cite unavailable evidence.
4. Low-confidence cases are marked as uncertain.

## 15. Recommended Implementation Order

1. Project skeleton and domain models.
2. Mock incident cases.
3. Provider interfaces and mock providers.
4. Diagnosis orchestrator.
5. Rule-based RCA analyzer.
6. Report generator.
7. API endpoints.
8. Minimal UI.
9. Golden case tests.
10. Webhook payload examples.

The first analyzer can be mostly rule-based. LLM synthesis can be added after structured evidence and expected outputs are stable.

## 16. Future Extensions

1. Real Prometheus or Grafana metrics provider.
2. Real Loki, ELK, or cloud log provider.
3. CI/CD and deployment integration.
4. Git code diff analysis.
5. Service dependency graph.
6. Notification integrations for Feishu, DingTalk, Slack, or WeCom.
7. Human approval workflow for rollback suggestions.
8. Multi-agent runtime when provider and reasoning boundaries become complex enough.

## 17. Success Criteria

The MVP is successful when:

1. A simulated incident can trigger diagnosis end to end.
2. A Webhook event can create an investigation.
3. The agent can produce a report with timeline, likely root cause, evidence, and next actions.
4. At least five golden cases run through the same diagnosis pipeline.
5. The report clearly separates facts, assumptions, and recommendations.

