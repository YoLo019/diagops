# V11 Strict Structured Outputs Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every admitted V11 model use a locally valid strict JSON Schema, certify and bind either native `json_schema + tools` or strict output-tool transport, and refuse to freeze failed predictions.

**Architecture:** Keep the existing OpenAI Agents SDK structured-output path. Replace the one open-ended output field with a strict Pydantic draft type. Select native `json_schema` where certified; otherwise use a strict function envelope whose decoded payload is validated by the full local schema. Bind that choice into capability and prediction identities, and enforce completeness in the shared prediction bundle validator.

**Tech Stack:** Python 3.12, Pydantic v2, openai-agents 0.18.1, OpenAI Python SDK, pytest, uv.

---

## File map

- Modify `backend/diagnosis/v11_runtime.py`: define the strict model-facing evidence scope and convert it to the persisted JSON map.
- Modify `tests/diagnosis/test_v11_runtime.py`: prove every V11 output type is accepted by the Agents SDK strict schema builder.
- Modify `backend/services/model_capability.py`: certify strict JSON Schema alone and in combination with strict function tools.
- Modify `tests/services/test_model_capability.py`: verify exact probe request shapes and admission identities.
- Modify `backend/benchmarks/rcaeval/runner.py`: reject incomplete prediction bundles before writing files.
- Modify `tests/benchmarks/test_rcaeval_runner.py`: prove failed cases cannot be frozen.

### Task 1: Make every V11 output schema strict

**Files:**
- Modify: `tests/diagnosis/test_v11_runtime.py`
- Modify: `tests/benchmarks/test_rcaeval_runner.py`
- Modify: `backend/diagnosis/v11_runtime.py`

- [x] **Step 1: Write the failing strict-schema regression tests**

Use `AgentOutputSchema(output_type, strict_json_schema=True).json_schema()` for `LeadPlanningOutput`, `InvestigatorOutput`, `CriticOutput`, `LeadAdjudicationOutput`, and RCAEval's `SingleControlOutput`. Recursively assert every object schema has `additionalProperties is False`.

- [x] **Step 2: Run tests and verify RED**

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py -k "valid_strict_json_schemas" tests/benchmarks/test_rcaeval_runner.py -k "strict_json_schema" -q
```

Expected: FAIL with the Agents SDK message that `additionalProperties` is set for `LeadTaskDraft.evidence_scope`.

- [x] **Step 3: Implement the minimal strict draft type**

Add this model in `backend/diagnosis/v11_runtime.py` and use it for `LeadTaskDraft.evidence_scope`:

```python
class EvidenceScopeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_ids: list[str] = Field(default_factory=list, max_length=20)
    start_time: datetime | None = None
    end_time: datetime | None = None
```

At both `DiagnosisTask` construction sites, convert a non-null draft with `model_dump(mode="json")`. Keep the persisted domain type `dict[str, JsonValue] | None`.

- [x] **Step 4: Run focused tests**

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py tests/benchmarks/test_rcaeval_runner.py -q
```

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add backend/diagnosis/v11_runtime.py tests/diagnosis/test_v11_runtime.py tests/benchmarks/test_rcaeval_runner.py
git commit -m "fix(v11): make model outputs strict-schema compatible"
```

### Task 2: Certify Structured Outputs per endpoint and model

**Files:**
- Modify: `tests/services/test_model_capability.py`
- Modify: `backend/services/model_capability.py`

- [x] **Step 1: Write failing request-shape tests**

Make the fake completions client retain request kwargs. Assert certification sends one `response_format.type == "json_schema"` request with `json_schema.strict is True`, and one request combining that format with a strict function tool. Assert `json_object_output` is absent and both new capabilities are present.

- [x] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/services/test_model_capability.py -q
```

Expected: FAIL because current certification sends `json_object` and has no combined strict-schema/tool probe.

- [x] **Step 3: Implement strict probes and transport binding**

Replace the obsolete capability and required-contract names with:

```python
"native_json_schema_with_tools",
"strict_output_tool",
```

The native probe combines strict `json_schema` with a strict diagnostic tool. The alternate probe forces a strict `submit_structured_output` tool with a closed `payload_json` envelope. Persist the selected transport in the capability artifact and apply it at every runtime entry point. Do not persist request or response bodies.

- [x] **Step 4: Run capability tests**

```powershell
uv run pytest tests/services/test_model_capability.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/services/model_capability.py tests/services/test_model_capability.py
git commit -m "fix(v11): certify strict structured outputs"
```

### Task 3: Refuse to freeze failed predictions

**Files:**
- Modify: `tests/benchmarks/test_rcaeval_runner.py`
- Modify: `backend/benchmarks/rcaeval/runner.py`

- [x] **Step 1: Write the failing bundle-integrity test**

Construct an otherwise-valid prediction bundle with one prediction changed to `completed=False` and `failure_category="ValueError"`. Assert `freeze_prediction_bundle` raises `ValueError("...incomplete prediction...")` and creates no output directory.

- [x] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/benchmarks/test_rcaeval_runner.py -k "incomplete_prediction" -q
```

Expected: FAIL because the current validator permits incomplete predictions.

- [x] **Step 3: Add the shared invariant**

Add to `validate_prediction_bundle` before filesystem creation:

```python
if any(not item.completed for item in bundle.predictions):
    raise ValueError("prediction bundle contains incomplete prediction")
```

- [ ] **Step 4: Run benchmark tests**

```powershell
uv run pytest tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_isolation.py tests/benchmarks/test_rcaeval_evaluator.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/benchmarks/rcaeval/runner.py tests/benchmarks/test_rcaeval_runner.py
git commit -m "fix(v11): reject incomplete prediction bundles"
```

### Task 4: Verify, re-certify, and rerun OB30 Single

**Files:**
- No source files expected.
- External artifacts are created only through existing custodian/certification commands.

- [ ] **Step 1: Run the relevant regression set**

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py tests/services/test_model_capability.py tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_isolation.py tests/benchmarks/test_rcaeval_evaluator.py -q
git diff --check
git status --short
```

Expected: all tests PASS, no diff errors, clean working tree after commits.

- [ ] **Step 2: Re-run capability certification**

In the user's secret-bearing PowerShell, use the existing endpoint/model environment variables. Require passed observations for `strict_json_schema_output` and `strict_json_schema_with_tools`, bound to the new clean revision.

- [ ] **Step 3: Obtain ledger reauthorization**

Use the existing custodian reauthorization command for the failed `ob30/single_intended` side. Do not delete or overwrite the frozen failure evidence. If a new pair root is required, create it through the custodian CLI rather than editing SQLite or seal files.

- [ ] **Step 4: Re-run OB30 Single**

Run `launch-predict` with the new capability artifact and frozen Single limits: token budget 4000, max turns 8, tool budget 8, timeout 120 seconds.

- [ ] **Step 5: Verify before issuing Multi**

Require 30 unique cases, 30 `completed=true`, no failure categories, nonzero model lifecycle/usage evidence, valid hashes, matching clean identities, completed ledger side, and unopened labels. Only then provide the complete `multi_intended` PowerShell command.

