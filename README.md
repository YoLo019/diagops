# DiagOps

DiagOps is an event-driven, evidence-backed SRE incident diagnosis platform for
application service incidents. Production integrations remain read-only.

The local platform includes:

- FastAPI backend with SQLite persistence.
- Mock, local log, deployment file, service catalog, and optional Prometheus
  evidence providers.
- Rule-based RCA, evidence-backed Markdown reports, recommended actions, and
  verification tracking.
- An optional, default-off OpenAI Agents SDK review layer with deterministic RCA
  fallback.
- Vite React investigation console for list/detail, evidence, hypotheses,
  action status, verification results, reports, and frozen OpenRCA results.

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
```

The Agents SDK integration is disabled by default and keeps OpenAI as the
backward-compatible Provider default:

```yaml
agents:
  enabled: false
  provider: openai
  model: null
  max_turns: 8
  timeout_seconds: 60
  strategy: fixed
  max_tool_calls_per_specialist: 3
  max_total_tool_calls: 8
  tool_timeout_seconds: 10
```

Override these settings for a local process with
`DIAGOPS_AGENTS_ENABLED`, `DIAGOPS_AGENTS_PROVIDER`, `DIAGOPS_AGENTS_MODEL`,
`DIAGOPS_AGENTS_MAX_TURNS`, `DIAGOPS_AGENTS_TIMEOUT_SECONDS`,
`DIAGOPS_AGENTS_STRATEGY`, `DIAGOPS_AGENTS_MAX_TOOL_CALLS_PER_SPECIALIST`,
`DIAGOPS_AGENTS_MAX_TOTAL_TOOL_CALLS`, and
`DIAGOPS_AGENTS_TOOL_TIMEOUT_SECONDS`.
`DIAGOPS_AGENTS_PROVIDER` accepts only `openai` or `deepseek`. Keep the matching
`OPENAI_API_KEY` or `DEEPSEEK_API_KEY` only in the local process environment;
never put it in settings, YAML, source code, logs, or commit history. Choose the
model explicitly when enabling the integration. Current official DeepSeek V4
model names are `deepseek-v4-flash` and `deepseek-v4-pro`.

`GET /config/agents` and the RCA workbench expose only Provider, model,
implementation status, certification status, strategy, and bounded tool
budgets. `implemented` means the key-free adapter and validation contracts
passed; it does not mean that the Provider/model has passed its independent
paid live gate.

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

## V4 Multi-Agent Workflow

DiagOps V4 adds a read-only multi-agent investigation process on top of the V3
platform:

- A task planner creates diagnosis tasks for logs, metrics, deployments,
  dependencies, service context, memory lookup, RCA synthesis, and optional LLM
  review.
- Agent routing assigns each task to the matching specialist agent and tool
  names.
- Shared context records evidence-backed facts that agents can read during the
  same investigation.
- Tool calls are recorded so the Agent process can show which read-only
  provider wrapper ran, with inputs, status, duration, and output evidence IDs.
- Memory and feedback store reusable incident summaries and human review notes.
- The Chinese frontend includes an Agent panel for plan, task graph, timeline,
  context, tool calls, and memory views.

V4 keeps the same production safety boundary as V3. It does not execute
rollback, restart, scale, or configuration changes. V4 tools are currently
read-only provider wrappers plus execution records.

### Agent Process APIs

```text
GET /investigations/{id}/plan
GET /investigations/{id}/tasks
GET /investigations/{id}/agent-executions
GET /investigations/{id}/context
GET /investigations/{id}/tool-calls
GET /investigations/{id}/memory
GET /investigations/{id}/task-graph
```

### Feedback API

```bash
curl -X POST http://127.0.0.1:8000/investigations/<investigation_id>/feedback \
  -H "Content-Type: application/json" \
  -d @docs/examples/v4-feedback.json
```

`POST /investigations/{id}/feedback` records a `MemoryItem` with human feedback
for the investigation service and environment. It does not modify the
investigation report, hypotheses, recommended actions, or verification
suggestions.

## V5 Agentic RCA Workbench

DiagOps V5 adds a read-only RCA workbench on top of the V4 agent process:

- LogAgent, MetricAgent, and DeploymentAgent produce independent findings.
- The coordinator ranks multiple root-cause candidates instead of forcing one answer.
- Candidates cite supporting and contradicting findings plus evidence IDs.
- The frontend shows Agent 判断, 候选根因排序, and 证据链预览.

V5 remains read-only. It does not execute rollback, restart, scaling, SSH, or
configuration changes. Optional LLM enhancement is disabled by default and is
not required for the workbench.

### V5 RCA APIs

```text
GET /investigations/{id}/agent-findings
GET /investigations/{id}/coordination-review
GET /investigations/{id}/rca-workbench
```

## Historical V6 And LLM Compatibility

V8.1 no longer constructs or executes the retired V6 ReAct runtime or the
pseudo-LLM analyst, and it creates no new `react_traces` or `llm_analyses`
rows. Existing rows remain readable through persisted investigation records and
the historical read-only ReAct API. New optional model analysis uses only the
unified Agents SDK runtime described below.

## V8.1 Multi-Agent Reliability Artifacts

The deterministic reliability run is safe for key-free verification and does
not read live credentials:

```powershell
uv run python -m backend.services.v7_live_acceptance --mode deterministic
```

The paid live gate remains disabled during automated verification. After the
implementation and key-free tests pass, configure these names only in the local
process environment:

```text
DIAGOPS_AGENTS_PROVIDER=openai|deepseek
DIAGOPS_AGENTS_MODEL
DIAGOPS_AGENTS_TIMEOUT_SECONDS (optional, defaults to 60)
DIAGOPS_INPUT_COST_PER_MILLION
DIAGOPS_OUTPUT_COST_PER_MILLION
OPENAI_API_KEY (only when Provider is openai)
DEEPSEEK_API_KEY (only when Provider is deepseek)
```

Never paste credential values into chat, code, YAML, or committed files. The
runner uses five existing simulated cases three times: 12 clean runs and 3
adversarial safety probes.

For DeepSeek V4 Pro certification, use the current cache-miss prices and an
explicit 180-second overall budget. The adapter disables the model's default
thinking mode for this bounded structured workflow; the longer budget covers
the required Coordinator and Specialist turns without changing the gate:

```powershell
$env:DIAGOPS_AGENTS_PROVIDER = "deepseek"
$env:DIAGOPS_AGENTS_MODEL = "deepseek-v4-pro"
$env:DIAGOPS_AGENTS_TIMEOUT_SECONDS = "180"
$env:DIAGOPS_INPUT_COST_PER_MILLION = "0.435"
$env:DIAGOPS_OUTPUT_COST_PER_MILLION = "0.87"
```

Before running the canonical live gate, run the five clean investigations in
diagnostic mode:

```powershell
uv run python -m backend.services.v7_live_acceptance --mode live --runs-per-case 1
```

Diagnostic mode does not evaluate the reliability thresholds and does not claim
PASS. Review its structured failure categories before making any prompt,
mapping, timeout, or arbitration change. Run the unchanged canonical live gate
only after the diagnostic stop gate permits it:

The OpenAI transport timeout is five seconds shorter than the configured
overall runtime timeout, with a one-second minimum reserved for short test
budgets. Client-level automatic retries are disabled so a request cannot start
a retry outside that budget. Post-runtime rejection exposes only a frozen
validation boundary category; any remaining `unknown` category blocks the
canonical gate.

```powershell
uv run python -m backend.services.v7_live_acceptance --mode live
```

Every mode writes a schema-v3 JSON artifact to
`output/reliability/<run_id>/result.json` and a concise Markdown summary to
`output/reliability/<run_id>/summary.md`. Artifacts contain only allowlisted
structured diagnostics. They omit credentials, API keys, Base URL values,
prompts, raw response data, reasoning, and free-text model payloads. The runner
is read-only: it records diagnostic results but does not execute remediation,
rollback, restart, scaling, SSH, or configuration changes.

### V8.1 OpenAI Canonical Baseline

The OpenAI canonical gate passed on 2026-07-14:

```text
Provider: openai
Model: gpt-5.6-sol
Run: v8-1-live-20260714T060212861267Z-b5f43e64
Completed: 2026-07-14T06:02:12.861267Z
Result: passed
```

The artifact contains 15/15 structurally valid real Agent reviews, 15/15
correct candidates, 3/3 correct results for every case, 15/15 valid references,
15/15 valid agreement and executed-action claim contracts, zero unsafe tools,
zero successful prompt injections, and zero wrong agreements. Non-fatal tracing
403 messages observed by the local runner did not produce model execution
failures and are not part of the certification artifact.

### V8.1 DeepSeek Certification

The DeepSeek V4 Pro adapter is implemented, but its independent canonical gate
did not pass on 2026-07-14:

```text
Provider: deepseek
Model: deepseek-v4-pro
Run: v8-1-live-20260714T080700796242Z-ffee9018
Completed: 2026-07-14T08:07:00.796242Z
Implementation: implemented
Certification: failed
```

The live artifact contains 15 unique canonical rows with valid Provider/model
attribution, references, agreement contracts, executed-action claim contracts,
and zero unsafe tools, successful prompt injections, wrong agreements, or
sensitive payload fields. It produced no passing real-review cohort. Bounded
transport, generated-schema, and CauseType-ontology remediations improved later
five-case diagnostics, but the final diagnostic
`v8-1-diagnostic-20260714T090134835432Z-405b87b3` remained below the stop gate
at 2/5 correct real reviews. No later diagnostic is counted as certification,
and the canonical thresholds were not weakened.

## V8.2 Adaptive Investigation And OpenRCA

V8.2 keeps `fixed` as the default investigation strategy. `fixed` uses the
existing bounded seed collection and deterministic RCA fallback. `adaptive`
gives LogAgent, MetricAgent, and DeploymentAgent their own registered read-only
tools so they can request additional evidence within the configured budget.
Select the strategy on a manual investigation or set `agents.strategy` in
`config/diagops.yaml`.

Adaptive queries support these bounded parameters:

- Every query: timezone-aware `start_time`, `end_time`, `reason`, and `limit`.
- Logs: `keywords`, `levels`, and `instance`.
- Metrics: `metric_names`, `aggregation`, and `instance`.
- Prometheus: the allowlisted `qps`, `5xx_rate`, `p95_latency`, `cpu`, and
  `memory` templates; no arbitrary PromQL.
- Deployments: `version` and `instance`.
- Service catalog: whether to include direct dependencies.
- Dependencies: `direction`, an allowlisted `target`, and depth fixed at one.

DiagOps still does not accept arbitrary log DSL, PromQL, file paths, URLs,
Shell commands, or write tools from an Agent. Production Providers remain
read-only. Tool inputs are validated and scoped to the incident; output is
redacted, bounded, evidence-linked, and audited. Adaptive failure never removes
the deterministic RCA result.

### OpenRCA Prerequisites

Download the OpenRCA telemetry dataset separately from the link in the
[Microsoft OpenRCA README](https://github.com/microsoft/OpenRCA). Do not commit
or redistribute the dataset from this repository. Microsoft recommends at
least 80 GB of storage and 32 GB of memory for the complete data. The expected
layout is:

```text
dataset/
  Bank/{query.csv,record.csv,telemetry/}
  Telecom/{query.csv,record.csv,telemetry/}
  Market/cloudbed-1/{query.csv,record.csv,telemetry/}
  Market/cloudbed-2/{query.csv,record.csv,telemetry/}
```

`prepare` is the only runtime-preparation step allowed to read `record.csv` and
the `scoring_points` column. It writes a frozen case manifest and a separate
`runtime-cases.json` containing no ground truth. Benchmark execution receives
only that safe index, and telemetry Providers can open only CSV files beneath
each allowlisted case directory. `evaluate` reads ground truth after execution;
never place its selected query output inside a frozen run directory.

### Prepare, Run, And Evaluate

The commands below select 10 cases from each partition with seed 42, run the
same pinned OpenAI model and prompt for both strategies, and create the local
compatible report. Keep the API key only in the process environment and supply
current model prices explicitly; no price is hardcoded.

```powershell
$datasetRoot = "D:\data\OpenRCA\dataset"
$prepared = "output\openrca-prepared"
$results = "output\benchmarks\openrca"

uv run python -m backend.benchmarks.openrca prepare `
  --dataset-root $datasetRoot `
  --output $prepared `
  --per-partition 10 `
  --seed 42

uv run python -m backend.benchmarks.openrca run `
  --dataset-root $datasetRoot `
  --safe-index "$prepared\runtime-cases.json" `
  --strategy both `
  --model $env:DIAGOPS_AGENTS_MODEL `
  --input-cost-per-million $env:DIAGOPS_INPUT_COST_PER_MILLION `
  --output-cost-per-million $env:DIAGOPS_OUTPUT_COST_PER_MILLION `
  --output $results

$runId = (Get-Content -Raw "$results\latest-run.txt").Trim()
$runDir = (Resolve-Path "$results\$runId").Path
$officialQueries = Join-Path (Resolve-Path ".").Path "output\openrca-official-queries\$runId"

uv run python -m backend.benchmarks.openrca evaluate `
  --query-root $datasetRoot `
  --run-dir $runDir `
  --official-query-output $officialQueries

$officialQueries = (Resolve-Path $officialQueries).Path
```

The local command writes `compatible-report.csv`; it must not be renamed to or
presented as an upstream result. From a separate Microsoft OpenRCA checkout,
run its evaluator against the four partition prediction files for each
strategy. The selected query files are sorted by the original `row_id`, as is
the upstream evaluator's prediction input.

```powershell
python -m main.evaluate `
  -p `
    "$runDir\fixed-Bank.csv" `
    "$runDir\fixed-Market-cloudbed-1.csv" `
    "$runDir\fixed-Market-cloudbed-2.csv" `
    "$runDir\fixed-Telecom.csv" `
    "$runDir\adaptive-Bank.csv" `
    "$runDir\adaptive-Market-cloudbed-1.csv" `
    "$runDir\adaptive-Market-cloudbed-2.csv" `
    "$runDir\adaptive-Telecom.csv" `
  -q `
    "$officialQueries\Bank-query.csv" `
    "$officialQueries\Market-cloudbed-1-query.csv" `
    "$officialQueries\Market-cloudbed-2-query.csv" `
    "$officialQueries\Telecom-query.csv" `
    "$officialQueries\Bank-query.csv" `
    "$officialQueries\Market-cloudbed-1-query.csv" `
    "$officialQueries\Market-cloudbed-2-query.csv" `
    "$officialQueries\Telecom-query.csv" `
  -r official-report.csv

Copy-Item official-report.csv "$runDir\official-report.csv"
```

The frozen run contains `run-manifest.json`, combined and per-partition
prediction CSV files, `summary.json`, the local `compatible-report.csv`, and,
only after upstream verification, `official-report.csv`. The manifest records
the case manifest hash, model, prompt, Git commit, strategy budgets, timestamps,
and caller-supplied cost rates. The summary records total and per-partition
scores, completion and Evidence validity, tool calls, duplicate rejections,
latency, tokens, cost, read-only violations, and every failed case.

Start the API and frontend, then open the `OpenRCA Benchmark` view. The read-only
API serves the latest valid frozen summary and only these download names:

```text
GET /benchmarks/openrca/latest
GET /benchmarks/openrca/latest/run-manifest.json
GET /benchmarks/openrca/latest/fixed-predictions.csv
GET /benchmarks/openrca/latest/adaptive-predictions.csv
GET /benchmarks/openrca/latest/official-report.csv
GET /benchmarks/openrca/latest/summary.json
```

V8.2 is complete only after a real 40-case paired run has 40 predictions per
strategy, Adaptive completion of at least 95%, 100% valid Evidence references,
zero mutation or out-of-scope executions, and an upstream official Adaptive
partial score not below Fixed. Failed cases and unfavorable results must remain
in the frozen artifacts.

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

`approved` and `done` are human-recorded workflow states only. Neither state
means DiagOps executed rollback, restart, scaling, or a configuration change.

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
