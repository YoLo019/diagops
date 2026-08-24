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

For a from-scratch Chinese walkthrough of the architecture, Agent design,
tool-calling, durable Runtime, safety, evaluation, and interview preparation,
see the [DiagOps interview study guide](docs/interview-guide/README.md).

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

Send a manual investigation:

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

## Receive Alertmanager Alerts

```bash
curl -X POST http://127.0.0.1:8000/events/alertmanager \
  -H "Content-Type: application/json" \
  -d @docs/examples/alertmanager-webhook.json
```

The endpoint processes at most 100 alerts synchronously and returns one ordered
result per alert. Firing alerts create investigations; resolved alerts are
ignored. Deliveries are at-least-once with no deduplication, so Alertmanager
retries or duplicate webhooks can create duplicate investigations. Configure
the receiver timeout above the observed batch investigation time.

## Production Acceptance Gate

The isolated six-service Gate uses the shared deterministic diagnostic core,
native Prometheus alerts, Alertmanager webhooks, and read-only evidence mounts.
It requires a running Docker Desktop Linux daemon:

```powershell
docker compose -f compose.production-gate.yaml config --quiet
powershell -ExecutionPolicy Bypass -File scripts/run_production_gate.ps1
```

Each of the seven scenarios starts a fresh Compose project. Results are written
under `output/production-acceptance/`; failed and unfavorable artifacts are
preserved. The fault endpoints exist only inside images started with
`DIAGOPS_PRODUCTION_GATE=true` and are not DiagOps tools or production APIs.
The `memory_pressure`, `network_corruption`, and `process_failure` scenarios
export bounded gauge/counter metrics only — they never exhaust real memory,
modify the network, or kill processes — and their Top-1 cause, component, and
reason must match with an onset error of at most 60 seconds; onset fallback is
forbidden for these scenarios. The whole Gate must finish below 900 seconds
with 100% Evidence reference validity, zero read-only violations, and a passing
privacy scan (no scenario or control-plane tokens in persisted records).

## List Investigations

```bash
curl http://127.0.0.1:8000/investigations
```

## Multi-Agent Workflow

DiagOps runs a read-only multi-agent investigation process:

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

The process does not execute rollback, restart, scale, or configuration
changes. Tools are read-only provider wrappers plus execution records.

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

## Agentic RCA Workbench

The read-only RCA workbench coordinates independent specialist findings:

- LogAgent, MetricAgent, and DeploymentAgent produce independent findings.
- The coordinator ranks multiple root-cause candidates instead of forcing one answer.
- Candidates cite supporting and contradicting findings plus evidence IDs.
- The frontend shows Agent 判断, 候选根因排序, and 证据链预览.

The workbench remains read-only. It does not execute rollback, restart,
scaling, SSH, or configuration changes. Optional LLM enhancement is disabled by
default and is not required for the workbench.

### RCA APIs

```text
GET /investigations/{id}/agent-findings
GET /investigations/{id}/coordination-review
GET /investigations/{id}/rca-workbench
```

## Historical ReAct And LLM Compatibility

DiagOps no longer constructs or executes the retired V6 ReAct runtime or the
pseudo-LLM analyst, and it creates no new `react_traces` or `llm_analyses`
rows. Existing rows remain readable through persisted investigation records and
the historical read-only ReAct API. New optional model analysis uses only the
unified Agents SDK runtime described below.

## Multi-Agent Reliability Artifacts

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

For DeepSeek certification, use the current cache-miss prices and an
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

## Adaptive Investigation And OpenRCA

DiagOps keeps `fixed` as the default investigation strategy. `fixed` uses the
bounded seed collection and deterministic RCA fallback. `adaptive` gives
LogAgent, MetricAgent, and DeploymentAgent their own registered read-only
tools so they can request additional evidence within the configured budget.
Select the strategy on a manual investigation or set `agents.strategy` in
`config/diagops.yaml`.

Adaptive queries support these bounded parameters:

- Every query: timezone-aware `start_time`, `end_time`, `reason`, and `limit`.
- Logs: `keywords`, `levels`, and `instance`.
- Metrics: `metric_names`, `aggregation`, and `instance`.
- Prometheus: the allowlisted `qps`, `5xx_rate`, `p95_latency`, `cpu`,
  `memory`, `network_drops`, and `process_restarts` templates; no arbitrary
  PromQL.
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
caller-supplied cost rates, and SHA-256 checksums for the other frozen run
artifacts. The summary records total and per-partition scores, completion and
Evidence validity, tool calls, duplicate rejections, latency, tokens, cost,
read-only violations, and every failed case.

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

The prepare, run, evaluate, and upstream `python -m main.evaluate` commands
above are the real release-gate templates. They require the separately
downloaded full dataset, a pinned supported model, a matching Provider key kept
only in the process environment, current caller-supplied input/output prices,
and a separate Microsoft OpenRCA checkout for the official evaluator. A small
fixture run is useful for development but is not the 40-case live release gate.

## V11 RCAEval Controlled Evaluation

V11 uses the separately prepared RCAEval RE2 package for its local held-out
effectiveness gate. The runtime package contains opaque case IDs and telemetry;
labels stay in a separate custodian package. Formal prediction uses the real
V11 runtime, SQLite persistence, the nine read-only Agent tools, a frozen
single-Investigator control, and a capability-certified OpenAI-compatible
endpoint. Test doubles are not accepted by the CLI.

Before a scored run, certify the exact endpoint/model tuple against the clean
source/package identity. The capability artifact also freezes the adapter/SDK
versions, required contracts, tested parallelism, execution environment, and
source manifest hash. The API key remains process-only and is never written to
artifacts:

```powershell
$env:DIAGOPS_AGENTS_API_KEY = "<process-only>"
uv run python -m backend.services.model_capability `
  --base-url $env:DIAGOPS_AGENTS_BASE_URL `
  --model $env:DIAGOPS_AGENTS_MODEL `
  --output-dir output/model_capability
```

Run each required configuration into a distinct child of one prediction root.
SS30 requires all four configurations; TT90 requires only the two intended
configurations. `single_intended` uses `B`, `multi_intended` uses at most `3B`,
and both equal-token SS30 configurations use exactly `3B`.
All children must live under one canonical custodian root. `--pair-root` is
only an output grouping path; the immutable `--custodian-manifest` selects the
single SQLite ledger shared across configurations and evaluation output
directories, so changing the output root cannot create a second pair.
Prediction output locators are required to be absolute canonical spellings;
relative paths, case/drive aliases, symlinks, junctions, and other reparse
paths are rejected before a side is recorded or a freeze marker is written.

```powershell
uv run python -m backend.benchmarks.rcaeval launch-predict `
  --runtime D:\data\RCAEval\prepared-v11-m0\runtime `
  --pair-root D:\data\RCAEval\v11-m5\ss30 `
  --custodian-manifest D:\data\RCAEval\v11-m5\custodian-manifest.json `
  --label-package D:\data\RCAEval\prepared-v11-m0\labels `
  --partition ss30 `
  --configuration single_intended `
  --base-url $env:DIAGOPS_AGENTS_BASE_URL `
  --capability-artifact <passed-result.json> `
  --database D:\data\RCAEval\v11-m5\ss30-single.db `
  --output D:\data\RCAEval\v11-m5\ss30\single_intended `
  --token-budget <B>

uv run python -m backend.benchmarks.rcaeval freeze-set `
  --root D:\data\RCAEval\v11-m5\ss30 `
  --custodian-manifest D:\data\RCAEval\v11-m5\custodian-manifest.json `
  --partition ss30
```

Export and finish the four-part, single-reviewer evidence audit before labels
are opened. Then launch the evaluator with the frozen prediction-set hash and
custodian manifest hashes. Supply both frozen audit artifacts; the custodian
ledger binds their exact hashes to one atomic label-open reservation. The
formal launcher leaves the only label-file open to the evaluator child
process. A crash or lease loss is recovered by the custodian ledger into a
pair-level non-resumable state. Completion-write failures leave a durable
custodian recovery intent when a SQLite writer lock prevents immediate
invalidation; the next explicit custodian recovery entry converges it before
business work continues. Concurrent attempts and post-child artifact failures
invalidate the pair and require explicit reauthorization.
Changing the evaluation output directory cannot bypass that ledger. V11 model
turns are also a durable run-level budget carried through checkpoint, retry,
and recovery rather than an SDK-invocation-local ceiling.

```powershell
# Repeat --bundle for every frozen configuration in the partition.
uv run python -m backend.benchmarks.rcaeval.audit export `
  --bundle <single-predictions.json> `
  --bundle <multi-predictions.json> `
  --output <evidence-audit-export.json>

uv run python -m backend.benchmarks.rcaeval evaluate `
  --predictions-root D:\data\RCAEval\v11-m5\ss30 `
  --custodian-manifest D:\data\RCAEval\v11-m5\custodian-manifest.json `
  --partition ss30 `
  --prediction-set-hash <sha256> `
  --label-package D:\data\RCAEval\prepared-v11-m0\labels `
  --runtime-manifest-hash <sha256> `
  --label-manifest-hash <sha256> `
  --audit-export <evidence-audit-export.json> `
  --manual-audit <manual-audit.json> `
  --output-dir D:\data\RCAEval\v11-m5\ss30-evaluation

uv run python -m backend.benchmarks.rcaeval freeze-policy `
  --sealed-validation D:\data\RCAEval\v11-m5\ss30-evaluation\evaluation.json `
  --tt90-manifest-hash <sha256> `
  --output D:\data\RCAEval\v11-m5\acceptance-policy.json
```

OB30 is development-only and unscored. SS30 may be executed once per frozen
candidate to seal the acceptance policy. TT90 is one paired attempt on the
exact clean source; a non-resumable infrastructure failure is archived and
requires explicit authorization before a fresh pair. RCAEval artifacts and
labels are intentionally local-only and are not exposed through the product
benchmark API. No accuracy improvement may be claimed unless every frozen gate
passes.

After the one TT90 evaluator attempt, archive the frozen policy result together
with the pre-label manual audit. The command derives evidence support from the
hashed pair-level artifact; it does not accept a caller-supplied percentage.

```powershell
uv run python -m backend.benchmarks.rcaeval accept `
  --evaluation D:\data\RCAEval\v11-m5\tt90-evaluation\evaluation.json `
  --policy D:\data\RCAEval\v11-m5\acceptance-policy.json `
  --audit-export D:\data\RCAEval\v11-m5\tt90-evidence-audit-export.json `
  --manual-audit D:\data\RCAEval\v11-m5\tt90-manual-audit.json `
  --output D:\data\RCAEval\v11-m5\tt90-acceptance-result.json
```

`launch-predict` is the supported formal entry. Its child receives only the
verified runtime package, the exact capability identity, and a minimal
environment; calling the internal `predict` worker directly is rejected. A
packaged deployment carries `backend/services/diagops-source-manifest.json`,
so source identity is verified after wheel relocation without relying on Git,
editable installs, or the caller's cwd.

## Runtime Operations

An Investigation is one Runtime session. Different Investigations may execute
concurrently, subject to `max_concurrent_runs`; records, budgets, event streams,
and traces remain scoped to their Investigation and Run. One Investigation may
retain multiple historical Runs, but it may have only one active live Run. The
active set includes `created`, `running`, `cancelling`, and `interrupted`, so an
operator must finish or resolve an interrupted Run before starting another live
Run for the same Investigation.

The checked-in defaults are:

```yaml
runtime:
  enabled: true
  max_concurrent_runs: 4
  max_parallel_steps_per_run: 3
  lease_seconds: 30
  heartbeat_seconds: 10
  opentelemetry:
    enabled: false
    endpoint: null
```

Environment overrides use the existing typed settings validation:

```powershell
$env:DIAGOPS_RUNTIME_ENABLED = "true"
$env:DIAGOPS_RUNTIME_MAX_CONCURRENT_RUNS = "4"
$env:DIAGOPS_RUNTIME_MAX_PARALLEL_STEPS_PER_RUN = "3"
$env:DIAGOPS_RUNTIME_LEASE_SECONDS = "30"
$env:DIAGOPS_RUNTIME_HEARTBEAT_SECONDS = "10"
$env:DIAGOPS_RUNTIME_OTEL_ENABLED = "false"
$env:DIAGOPS_RUNTIME_OTEL_ENDPOINT = "http://127.0.0.1:4318/v1/traces"
```

OpenTelemetry remains off unless both `DIAGOPS_RUNTIME_OTEL_ENABLED=true` and a
valid HTTP(S) collector endpoint are configured. Collector creation, export,
flush, shutdown failure, or delay is isolated from Run execution.

### Run And Attempt Lifecycle

A new Run moves from `created` to `running`, then to `completed`, `failed`, or
`cancelled`. A cancellation request first moves an executing Run to
`cancelling`; work already past a durable boundary is retained, while late
results are fenced out. `cancelled` is terminal and cannot be resumed.

Each execution ownership period creates one Attempt. An Attempt starts as
`running` and ends as `completed`, `failed`, `cancelled`, or `interrupted`.
When startup audit or an operator audit finds an expired lease, it marks the
Run and current Attempt `interrupted`; startup never invokes an Agent, Provider,
Tool, or automatic resume. Only an `interrupted` Run can be manually resumed,
and resume creates a new Attempt from the last validated complete checkpoint.
Successfully committed Tool results are reused instead of called again.

Runtime APIs are additive:

```text
POST /investigations/{investigation_id}/runtime-runs
GET  /investigations/{investigation_id}/runtime-runs
GET  /runtime-runs/{run_id}
GET  /runtime-runs/{run_id}/events?after={sequence}&limit={1..500}
GET  /runtime-runs/{run_id}/events/stream
POST /runtime-runs/{run_id}/cancel
POST /runtime-runs/{run_id}/resume
POST /runtime-runs/{run_id}/replay
GET  /runtime-runs/{run_id}/diff?against_run_id={other_run_id}
```

Creating a Run requires `strategy` and `run_reason`; Provider, model, prompt
version, and parent Run are optional frozen metadata. A conflicting active live
Run or resume race returns `409`.

### SSE Reconnect

Runtime SSE event IDs are durable per-Run sequence numbers. Reconnect with the
last processed sequence in the standard `Last-Event-ID` header:

```powershell
curl.exe -N `
  -H "Last-Event-ID: 17" `
  http://127.0.0.1:8000/runtime-runs/<run_id>/events/stream
```

Clients that cannot set the header may use
`?last_event_id=17`. The header takes precedence when both are present. The
server subscribes before durable catch-up and deduplicates by sequence, so new
events arriving during reconnect are neither missed nor repeated. Heartbeats
are comments and have no event ID. A client disconnect or slow-client overflow
closes only that stream and does not cancel or otherwise affect execution.

### Replay, Diff, Migration, And Retention

`POST /runtime-runs/{run_id}/replay` performs audit replay from persisted state.
It does not call a Provider, Tool, or model, consumes zero model tokens, and
reports corruption such as sequence gaps, illegal transitions, invalid
references, or checkpoint tampering. `GET /runtime-runs/{run_id}/diff` compares
two Runs from the same Investigation using stable structured sections and
frozen terminal business projections; it does not re-execute either Run.

Database initialization migrates supported SQLite schema V5 to schema V6
in one transaction by adding `runtime_runs`, `runtime_attempts`,
`runtime_events`, and `runtime_checkpoints`. Back up or copy the database before
an operational migration and run `PRAGMA foreign_key_check` afterward. Existing
Investigations remain readable and expose `runtime_available=false`; migration
does not invent historical Runs, Attempts, Events, or Checkpoints for them.

Runtime has no automatic event retention or cleanup. Runtime events remain
append-only until an explicitly designed retention policy is approved.

### Runtime Safety And Key-free Acceptance

Runtime events, SSE, OpenTelemetry, Replay, Diff, and Runtime logs use
allowlisted structured metadata. They prohibit prompts, chain-of-thought or
hidden reasoning, credentials, authorization values, evidence or log bodies,
raw Provider request/response payloads, arbitrary Tool output, arbitrary URLs,
PromQL, Shell/SSH commands, and production mutations. Production Providers and
Tools remain read-only. Injection-like text is treated as data and cannot add
fields, external calls, or actions. Collector failure and client disconnect do
not affect execution.

Run the deterministic, key-free gate with:

```powershell
uv run python -m backend.services.runtime_acceptance
```

It writes only `output/runtime-acceptance/<run-id>/result.json`, reports raw
latency, SQLite growth, recovery time, and OpenTelemetry overhead without an
extra pass threshold, and fails if a required scenario or privacy check is
missing.

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

## Safety Boundaries

DiagOps is read-only. It does not automatically modify production
systems. It does not execute rollback, restart, scaling, or configuration
changes. It collects evidence, ranks hypotheses, generates recommended actions,
records approval status, and tracks verification results for engineer review.
