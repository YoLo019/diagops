# DiagOps

DiagOps is an event-driven SRE RCA agent MVP for application service incidents.

V3 turns the first backend prototype into a local read-only investigation
platform:

- FastAPI backend with SQLite persistence.
- Mock, local log, deployment file, service catalog, and optional Prometheus
  evidence providers.
- Rule-based RCA, evidence-backed Markdown reports, recommended actions, and
  verification tracking.
- Read-only LLM analyst stub that is disabled by default and does not call any
  network API.
- Vite React investigation console for list/detail, evidence, hypotheses,
  action status, verification results, and reports.

## Local Development

Install Python dependencies:

```bash
uv sync
```

Start the backend:

```bash
uv run uvicorn backend.main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Frontend:

```bash
cd frontend
npm.cmd install
npm.cmd run dev
```

The Vite dev server proxies API requests to `http://127.0.0.1:8000` by
default. Set `VITE_API_BASE_URL` only when the API is served from another
origin.

Build the frontend:

```bash
cd frontend
npm.cmd run build
```

## Local Data And Cache Paths

The default SQLite database is stored under the project data directory:

```text
data/diagops.db
```

The repository is configured so local generated state stays off C drive where
possible:

```powershell
uv cache dir
# D:\agent\.uv-cache

npm.cmd config get cache
# D:\agent\.npm-cache
```

Node.js is expected from the D-drive runtime on this machine:

```powershell
Get-Command node
# D:\paiflow\nodejs\node.exe
```

Do not commit runtime databases, frontend `node_modules`, frontend `dist`, or
Playwright scratch output.

## Provider Configuration

The default provider config is `config/diagops.yaml`. A copy is available at
`docs/examples/v3-provider-config.yaml`.

Default providers:

- `mock`: enabled, provides deterministic incident evidence.
- `log_file`: enabled, reads `data/sample-logs/checkout-service.log`.
- `deployment_file`: enabled, reads `data/deployments/deployments.json`.
- `service_catalog`: enabled, reads `config/services.yaml`.
- `prometheus`: disabled by default; when enabled it queries
  `http://127.0.0.1:9090` with standard-library HTTP calls.

Override config path:

```powershell
$env:DIAGOPS_CONFIG = "config/diagops.yaml"
```

Useful environment overrides:

```powershell
$env:DIAGOPS_DATABASE_URL = "sqlite:///data/diagops.db"
$env:DIAGOPS_PROVIDER_MOCK_ENABLED = "true"
$env:DIAGOPS_LLM_ENABLED = "false"
```

## Sample Local Data

Run with the sample deployment and log evidence:

```bash
curl -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
```

Send a V3 manual investigation:

```bash
curl -X POST http://127.0.0.1:8000/investigations/manual \
  -H "Content-Type: application/json" \
  -d @docs/examples/v3-manual-investigation.json
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

DiagOps is read-only in V3. It does not automatically modify production
systems. It does not execute rollback, restart, scaling, or configuration
changes. It collects evidence, ranks hypotheses, generates recommended actions,
records approval status, and tracks verification results for engineer review.
