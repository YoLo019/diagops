# DiagOps V5 Agentic RCA Workbench Design

## 1. Goal

V5 turns the V4 agent process view into a more complete multi-agent RCA workflow.

The main goal is:

```text
LogAgent / MetricAgent / DeploymentAgent produce independent findings
  -> Coordinator ranks root-cause candidates and records agreement/conflict/gaps
  -> frontend shows a diagnosis workbench with findings, candidate ranking, and evidence-chain preview
```

V5 should feel closer to OpenDerisk-style agentic RCA, but keep DiagOps' current safety and evidence-first boundaries.

The first V5 version uses deterministic rules as the source of truth. Optional LLM enhancement may improve wording only; it must not create facts, add evidence IDs, change rankings, or execute actions.

## 2. Non-Goals

V5 does not implement:

1. Automatic remediation, rollback, restart, scaling, SSH, or configuration changes.
2. Automatic follow-up evidence collection loops.
3. A graph-first frontend or complex graph editor.
4. LangGraph, CrewAI, OpenDerisk runtime, or any new heavy multi-agent framework.
5. Full service-catalog, dependency, or memory specialist findings in the first version.
6. LLM-first RCA where model output becomes the source of truth.
7. Multi-tenant permissions or production approval workflows.

ponytail: V5 adds the smallest collaboration protocol that can support a later graph view.

## 3. Design Direction

V5 uses the approved "rules plus optional read-only LLM" approach.

The deterministic path is:

```text
existing provider evidence
  -> finding builders for LogAgent, MetricAgent, DeploymentAgent
  -> coordination review
  -> ranked candidates
  -> workbench API
  -> frontend diagnosis workbench
```

The optional LLM path is:

```text
rules create findings and candidates
  -> LLM may rewrite summary/rationale/uncertainty fields
  -> evidence IDs, finding IDs, ranking, cause_type, and safety status stay unchanged
```

If LLM analysis fails, times out, or returns invalid output, V5 keeps the rule-generated findings and candidates.

## 4. User-Visible Behavior

For every completed investigation, the frontend should show a V5 RCA workbench section.

The first workbench layout is list-based and dense:

1. **Agent 判断**
   - Group findings by `LogAgent`, `MetricAgent`, and `DeploymentAgent`.
   - Show summary, finding type, confidence, related cause type, severity, evidence IDs, rationale, and gaps.

2. **候选根因排序**
   - Show ranked root-cause candidates.
   - Each candidate shows cause type, confidence, summary, supporting findings, contradicting findings, supporting evidence, contradicting evidence, coordinator rationale, and uncertainty.
   - The UI must not present low-confidence candidates as certain facts.

3. **证据链预览**
   - Show a compact list relationship:

```text
candidate -> finding -> evidence
```

   - The first version does not need a full graph renderer.

4. **Future graph seed**
   - The API returns enough node/edge data for a later OpenDerisk-style graph view.
   - The frontend may ignore the graph seed in V5 if the list workbench is clear.

Safety wording must stay explicit:

1. Recommended actions are suggestions only.
2. Approval records state only.
3. Verification records results only.
4. The UI must not claim rollback, restart, scaling, config changes, or repairs were executed.

## 5. Domain Model Changes

Add V5 domain models under `backend/domain/` or an existing nearby domain module if that keeps the diff smaller.

### 5.1 AgentFinding

```text
id
investigation_id
agent_name
finding_type
summary
confidence
evidence_ids[]
related_cause_type?
severity
rationale
gaps[]
created_at
```

Allowed `agent_name` values for V5:

```text
LogAgent
MetricAgent
DeploymentAgent
```

Allowed `finding_type` values:

```text
signal
root_cause
contradiction
gap
```

Validation rules:

1. `confidence` must be finite and within `0..1`.
2. `evidence_ids` must reference existing evidence, except `finding_type=gap`.
3. `summary` must be non-empty.
4. `agent_name` must be one of the supported V5 agents unless a later spec expands it.

### 5.2 CoordinationReview

```text
id
investigation_id
candidates[]
created_at
```

### 5.3 RootCauseCandidate

```text
id
cause_type
summary
rank
confidence
supporting_finding_ids[]
contradicting_finding_ids[]
supporting_evidence_ids[]
contradicting_evidence_ids[]
rationale
uncertainty
```

Validation rules:

1. `rank` starts at 1 and is stable in API responses.
2. `confidence` must be finite and within `0..1`.
3. Finding IDs must reference existing V5 findings.
4. Evidence IDs must reference existing investigation evidence.
5. Candidates may include uncertainty even when confidence is high.

## 6. Finding Builders

V5 adds three deterministic finding builders.

### 6.1 LogAgent Finding Builder

Inputs:

1. Existing log evidence.
2. Provider-error evidence for logs.
3. Existing incident event fields.

Outputs:

1. Error spike or exception signal findings.
2. Root-cause findings when logs clearly indicate a cause type.
3. Gap findings when logs are missing, failed, or too weak.

Examples:

```text
many 500 errors after deployment -> signal/root_cause finding
log provider failed -> gap finding
```

### 6.2 MetricAgent Finding Builder

Inputs:

1. Existing metric evidence.
2. Prometheus or mock metric provider results.
3. Provider-error evidence for metrics.

Outputs:

1. Latency, error-rate, traffic, saturation, or availability signal findings.
2. Contradiction findings when metrics do not support a suspected cause.
3. Gap findings when metrics are missing or failed.

### 6.3 DeploymentAgent Finding Builder

Inputs:

1. Deployment evidence.
2. Existing incident event time window.
3. Provider-error evidence for deployments.

Outputs:

1. Deployment correlation findings.
2. Root-cause findings when a recent deployment aligns with the incident window.
3. Contradiction findings when no relevant deployment exists.
4. Gap findings when deployment data is missing or failed.

## 7. Coordinator Review

The V5 coordinator ranks multiple root-cause candidates. It must not force a single answer when uncertainty remains.

Coordinator inputs:

1. V5 agent findings.
2. Existing hypotheses from the RCA analyzer.
3. Existing evidence.

Coordinator outputs:

1. `CoordinationReview`.
2. Ranked `RootCauseCandidate` list.
3. Support and contradiction links from findings and evidence.
4. Rationale and uncertainty.

Ranking principles:

1. Multiple high-confidence findings supporting the same `cause_type` should increase that candidate's rank.
2. Contradicting findings should reduce confidence or appear in `contradicting_finding_ids`.
3. Provider failures and gaps should not silently disappear.
4. Existing RCA analyzer output can seed candidates, but V5 coordinator must explain support and contradiction through finding IDs and evidence IDs.
5. Low-confidence or unknown candidates remain visible when evidence is weak.

## 8. Optional LLM Enhancement

LLM enhancement is disabled by default.

When enabled, it may update only:

```text
AgentFinding.summary
AgentFinding.rationale
AgentFinding.gaps
RootCauseCandidate.summary
RootCauseCandidate.rationale
RootCauseCandidate.uncertainty
```

It must not update:

```text
ids
agent_name
finding_type
confidence
evidence_ids
related_cause_type
severity
rank
supporting_finding_ids
contradicting_finding_ids
supporting_evidence_ids
contradicting_evidence_ids
```

LLM output must be validated before persistence. Invalid output is ignored with a visible warning or persisted non-blocking error note.

## 9. Persistence

Persist V5 data in SQLite through the existing repository style.

Minimum stored records:

1. Agent findings by `investigation_id`.
2. Coordination review by `investigation_id`.

Implementation may use JSON payload tables plus query columns, matching the V4 persistence style.

Suggested tables:

```text
agent_findings
coordination_reviews
```

Indexes should cover:

```text
investigation_id
agent_name
created_at
```

Existing investigations must remain readable. If no V5 data exists for an older investigation, APIs return empty findings and no review or a workbench with empty V5 sections.

## 10. API Changes

Add read-only endpoints:

```text
GET /investigations/{id}/agent-findings
GET /investigations/{id}/coordination-review
GET /investigations/{id}/rca-workbench
```

### 10.1 Agent Findings

Returns:

```text
AgentFinding[]
```

### 10.2 Coordination Review

Returns:

```text
CoordinationReview | null
```

### 10.3 RCA Workbench

Returns:

```text
{
  investigation,
  findings[],
  candidates[],
  evidence[],
  graph_seed: {
    nodes[],
    edges[]
  }
}
```

`graph_seed` should support a later graph view:

Node types:

```text
agent
finding
candidate
evidence
```

Edge relations:

```text
produced
supports
contradicts
cites
```

The endpoint should reuse existing response models where practical and avoid creating a broad generic graph abstraction.

## 11. Frontend Changes

Modify the existing React console instead of creating a separate app.

Add RCA workbench UI near the existing Agent process and report areas.

Panels:

1. `Agent 判断`
2. `候选根因排序`
3. `证据链预览`

Frontend API client adds:

```text
getAgentFindings
getCoordinationReview
getRcaWorkbench
```

The first V5 frontend should remain list-based:

1. No graph library.
2. No draggable nodes.
3. No graph editing.
4. No separate route unless the current layout becomes too crowded.

ponytail: keep `graph_seed` as data, not UI complexity.

## 12. Error Handling And Safety Boundaries

V5 failure rules:

1. Finding-builder failure should mark V5 workbench data incomplete, but should not erase the base investigation report.
2. One specialist failure should become a gap finding when possible.
3. If all V5 finding builders fail, keep the base investigation completed but expose missing V5 workbench data.
4. LLM failures are non-blocking.
5. Invalid finding evidence references must be rejected before persistence.
6. APIs must return 404 for unknown investigations.
7. APIs must not leak secrets or raw credentials.

Production safety:

1. V5 remains read-only.
2. Findings and candidates are diagnostic output only.
3. Recommended actions remain suggestions.
4. Approval and verification remain human-recorded state changes.

## 13. Test And Acceptance Criteria

### 13.1 Backend Tests

Domain:

1. `AgentFinding` validates confidence.
2. Non-gap findings require valid evidence references.
3. `RootCauseCandidate` finding and evidence references are traceable.

Builders:

1. LogAgent creates a finding from log error evidence.
2. MetricAgent creates a finding from metric anomaly evidence.
3. DeploymentAgent creates a finding from deployment evidence.
4. Provider failure creates a gap finding.

Coordinator:

1. Multiple findings supporting the same cause type increase candidate rank.
2. Contradicting findings are recorded.
3. Low-confidence or missing evidence creates uncertainty instead of confident conclusions.

API:

1. `agent-findings` returns stable structures.
2. `coordination-review` returns review or null.
3. `rca-workbench` returns investigation, findings, candidates, evidence, and graph seed.
4. Unknown investigation returns 404.

Golden:

1. `deployment_regression` ranks a deployment-related candidate near the top.
2. Every candidate evidence ID points to existing evidence.
3. Every candidate finding ID points to existing findings.

### 13.2 Frontend Tests

1. Page contains `Agent 判断`.
2. Page contains `候选根因排序`.
3. Page contains `证据链预览`.
4. API client exposes V5 workbench methods.
5. UI does not contain unsafe automatic remediation wording.
6. Frontend build passes.

### 13.3 Verification Commands

Required before completion:

```powershell
uv run ruff check .
uv run pytest -v
cd frontend
npm.cmd run build
```

## 14. Rollout And Compatibility

V5 is additive.

Compatibility rules:

1. Existing V1-V4 APIs remain unchanged.
2. Existing investigations remain readable.
3. Existing report and action behavior remains valid.
4. Existing V4 agent process APIs remain available.
5. V5 workbench data may be empty for old records.
6. LLM enhancement remains disabled unless explicitly configured.

README should gain a short V5 section after implementation, not before behavior exists.

## 15. OpenDerisk-Inspired Frontend Direction

OpenDerisk presents RCA as agent collaboration plus a visual evidence chain. V5 borrows that direction but implements the smallest stable first step:

1. Show agent roles and independent findings.
2. Show ranked RCA candidates.
3. Show evidence-chain relationships as a preview list.
4. Return graph seed data for a future visual graph.

Future V5.1 or V6 may add:

1. Graph renderer for `agent -> finding -> candidate` and `evidence -> finding`.
2. Metric charts inside evidence details.
3. Round-based agent collaboration timeline.
4. Report panel that highlights selected graph nodes.

These are intentionally outside V5's first implementation.

## 16. Acceptance Summary

V5 is complete when:

1. Every new investigation can produce LogAgent, MetricAgent, and DeploymentAgent findings.
2. Coordinator produces ranked root-cause candidates with support, contradiction, and uncertainty.
3. Candidates trace back to findings and evidence.
4. The frontend workbench displays findings, candidate ranking, and evidence-chain preview.
5. Optional LLM enhancement is disabled by default and cannot change facts or ranking.
6. No production mutation capability is introduced.
7. Ruff, backend tests, and frontend build pass.
