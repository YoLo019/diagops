# DiagOps

DiagOps is an event-driven SRE RCA agent MVP.

The first version focuses on application service incidents:

- simulated incidents
- Webhook incident events
- mock evidence providers
- rule-based RCA analysis
- evidence-backed Markdown reports

## Local Development

```bash
uv sync
uv run uvicorn backend.main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Trigger A Simulated Incident

```bash
curl -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
```

## Send A Webhook Event

```bash
curl -X POST http://127.0.0.1:8000/events \
  -H "Content-Type: application/json" \
  -d @docs/examples/webhook-event.json
```

## List Investigations

```bash
curl http://127.0.0.1:8000/investigations
```

## V2 Platform Loop

DiagOps V2 keeps production systems read-only. It creates an investigation,
collects evidence, ranks RCA hypotheses, generates recommended actions, records
approval status, and provides verification suggestions.

V2 does not execute rollback, restart, scaling, or configuration changes. It
only records recommendations, approval state, and verification results for
engineer review.

## Manual Investigation

```bash
curl -X POST http://127.0.0.1:8000/investigations/manual \
  -H "Content-Type: application/json" \
  -d '{"text":"checkout-service has many 500s after 14:00","service":"checkout-service","environment":"prod"}'
```

## Update A Recommended Action

```bash
curl -X PATCH http://127.0.0.1:8000/investigations/<investigation_id>/actions/<action_id> \
  -H "Content-Type: application/json" \
  -d '{"status":"approved","note":"owner approved"}'
```

Approval only changes state in V2. It does not execute rollback, restart,
scaling, or configuration changes.

## Record A Verification Result

```bash
curl -X PATCH http://127.0.0.1:8000/investigations/<investigation_id>/verifications/<verification_id> \
  -H "Content-Type: application/json" \
  -d '{"status":"passed","result_note":"5xx rate recovered"}'
```

## MVP Boundaries

The MVP does not automatically modify production systems. It only collects
evidence, ranks hypotheses, generates recommended actions, and records
approval and verification status for engineer review.
