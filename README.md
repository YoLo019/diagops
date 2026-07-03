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
