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

## MVP Boundaries

The MVP does not automatically modify production systems. It only collects evidence, ranks hypotheses, and generates an RCA report for engineer review.
