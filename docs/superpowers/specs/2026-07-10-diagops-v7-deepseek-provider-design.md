# DiagOps V7 DeepSeek Provider Design

## 1. Goal

Add an explicit `openai | deepseek` provider switch to the existing V7
multi-Agent runtime and allow the fixed 15-run reliability gate to evaluate
DeepSeek V4 Pro. Preserve the current deterministic RCA, Agents SDK
orchestration, persistence, API, report, and frontend behavior.

The DeepSeek path uses the official `deepseek-v4-pro` model and official
OpenAI-compatible API only.

## 2. Approved Direction

Reuse the current OpenAI Agents SDK execution layer. Add a small DeepSeek model
adapter around the SDK's Chat Completions model instead of creating a second
Agent runtime or a generic provider framework.

The existing Coordinator and three specialist `Agent.as_tool` calls remain the
only Agent graph. Deterministic hybrid arbitration remains the only authority
that can select or reject an Agent conclusion.

## 3. Configuration

Add `agents.provider` with these values:

- `openai`: default; current behavior remains unchanged.
- `deepseek`: use the official DeepSeek adapter.

Environment overrides:

```text
DIAGOPS_AGENTS_PROVIDER=deepseek
DIAGOPS_AGENTS_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=<local process environment only>
```

Rules:

1. The DeepSeek Base URL is fixed by the implementation. It is not configurable.
2. `DEEPSEEK_API_KEY` is read only when constructing the DeepSeek client. It is
   not stored in Pydantic settings, logs, persistence, reports, UI data, or
   acceptance artifacts.
3. Missing, empty, or whitespace-only provider keys produce a visible `skipped`
   Agent run and no network call.
4. A missing model also produces `skipped`.
5. Existing OpenAI deployments require no configuration migration.

## 4. Model Adapter And Data Flow

The DeepSeek adapter uses:

- model: `deepseek-v4-pro` for the approved live acceptance;
- protocol: OpenAI-compatible Chat Completions;
- endpoint: the official DeepSeek strict-tool Beta endpoint required for strict
  function JSON schemas;
- orchestration: the existing Agents SDK `Agent`, `Agent.as_tool`, and
  `Runner.run` path.

Data flow:

```text
provider + model + local key
  -> provider-specific SDK Model object
  -> existing Coordinator and specialist Agent tools
  -> local Pydantic validation
  -> deterministic hybrid arbitration
  -> existing persistence, API, report, and UI
```

DeepSeek documents JSON Object output rather than the JSON Schema response
format used by the Agents SDK's `output_type`. For the DeepSeek adapter only:

1. Agent instructions include the required JSON object schema and explicitly
   require JSON-only output.
2. The remote request uses JSON Object mode.
3. SDK remote `output_type` schema enforcement is not used.
4. The returned JSON is validated locally with the existing `_SpecialistDraft`
   and `_CoordinatorProposal` Pydantic models before it can become an
   `AgentFinding` or `CoordinationReview`.
5. JSON is parsed strictly. Markdown fences, prose wrappers, empty content, or
   schema mismatch are rejected rather than repaired.

OpenAI continues to use its current Responses API and SDK structured-output
path without branching behavior changes.

## 5. User-Visible Behavior

The provider switch does not add an endpoint, page, table, or report section.
Existing V7 task, execution, finding, review, report, and workbench fields remain
the public contract.

Provider failures remain visible through the existing safe failure categories.
The UI and report continue to distinguish deterministic inference, Agent
inference, evidence, recommendations, and uncertainty. No provider may claim
that rollback, restart, scaling, SSH, repair, or configuration mutation was
executed.

## 6. Failure Handling And Safety

DeepSeek authentication, rate-limit, quota, timeout, empty-output, tool-call,
and validation failures degrade to the existing deterministic RCA. They never
fail the base investigation.

Additional rules:

1. No automatic provider fallback from DeepSeek to OpenAI. A provider switch is
   explicit so reliability measurements remain attributable.
2. No retry framework is added. Existing timeout and max-turn limits apply.
3. DeepSeek runs disable OpenAI trace export. Sensitive model/tool payloads are
   never exported.
4. Reasoning content and raw responses are not persisted or rendered.
5. Evidence and revision references receive the same validation as OpenAI runs.
6. The provider cannot add production mutation tools. The tool allowlist remains
   the three specialist Agent tools.

## 7. Live Acceptance

The existing 15-run gate becomes provider-aware. It still runs five golden
cases three times with 12 clean runs and three fixed adversarial probes.

For DeepSeek V4 Pro the local environment uses:

```text
DIAGOPS_AGENTS_PROVIDER=deepseek
DIAGOPS_AGENTS_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=<local process environment only>
DIAGOPS_INPUT_COST_PER_MILLION=0.435
DIAGOPS_OUTPUT_COST_PER_MILLION=0.87
```

The input rate intentionally uses the current cache-miss price, so the estimate
is conservative. Current model and pricing values are rechecked against the
official DeepSeek documentation immediately before the paid gate and are not
added as runtime defaults.

The artifact adds the provider name. It does not include a key, Base URL, raw
prompt, raw response, or reasoning content. All existing reliability and safety
thresholds remain unchanged.

## 8. Compatibility And Rollout

1. `agents.provider` defaults to `openai`.
2. Old YAML without `provider` remains valid.
3. Existing enums, persisted JSON, SQLite schema, APIs, reports, and frontend
   models do not change.
4. V7 remains default-off.
5. Enabling DeepSeek requires both the provider selection and local key/model
   configuration.

## 9. Tests And Acceptance Criteria

Automated checks must prove:

1. Settings accept only `openai` and `deepseek`; environment override works and
   OpenAI remains the default.
2. Missing/blank `DEEPSEEK_API_KEY` skips without a model call or key leakage.
3. The adapter uses only the fixed official endpoint and Chat Completions model.
4. DeepSeek Agent requests use JSON Object output and the existing strict Agent
   tool schemas.
5. Valid specialist and Coordinator JSON passes local Pydantic validation.
6. Empty content, fenced JSON, invalid JSON, and schema-invalid JSON produce an
   explicit fallback.
7. Existing OpenAI SDK contract tests remain unchanged and pass.
8. Provider authentication, rate, quota, and timeout errors retain safe visible
   classifications.
9. Acceptance artifacts record `provider=deepseek` without credentials or raw
   model data.
10. Ruff, the full pytest suite, and the frontend production build pass before
    requesting a local DeepSeek key.
11. The real 15-run gate meets every existing V7 threshold before the DeepSeek
    path is declared reliable.

## 10. Non-Goals

This iteration does not add:

1. Arbitrary OpenAI-compatible Base URLs.
2. Generic provider plugins, factories, or routing abstractions.
3. Automatic provider fallback, load balancing, or per-Agent mixed providers.
4. LangGraph or a second Agent execution engine.
5. New persistence tables, API endpoints, frontend pages, or production tools.
6. Lenient JSON repair or parsing of Markdown-wrapped model output.

## 11. Official References

- DeepSeek API first call and model names:
  <https://api-docs.deepseek.com/>
- DeepSeek V4 model features and pricing:
  <https://api-docs.deepseek.com/quick_start/pricing>
- DeepSeek JSON Output:
  <https://api-docs.deepseek.com/guides/json_mode>
- DeepSeek strict Tool Calls:
  <https://api-docs.deepseek.com/guides/tool_calls>
