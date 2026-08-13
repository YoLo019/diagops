# V11 Production Schema Certification Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every V11 production output schema explicitly typed and make endpoint certification exercise the exact Single production schema used by OB30.

**Architecture:** `backend/diagnosis/v11_runtime.py` remains the sole owner of V11 model-output contracts. RCAEval imports the shared Single output type, while `backend/services/model_capability.py` builds its native certification request from `AgentOutputSchema` for that same type and includes the schema digest in capability identity. No schema rewriting, provider special case, fallback, or new dependency is introduced.

**Tech Stack:** Python 3.12, Pydantic v2, OpenAI Agents SDK 0.18.1, OpenAI Python SDK, pytest, SQLite smoke evidence.

---

## File structure

- Modify `backend/diagnosis/v11_runtime.py`: own the typed Investigator finding and shared Single control output contracts.
- Modify `backend/benchmarks/rcaeval/runner.py`: consume the shared Single output type; retain only benchmark topology and execution.
- Modify `backend/services/model_capability.py`: generate the native probe and capability identity from the shared production schema.
- Modify `tests/diagnosis/test_v11_runtime.py`: reject schema nodes without explicit type semantics and cover the shared Single output.
- Modify `tests/benchmarks/test_rcaeval_runner.py`: import the shared Single output contract from its new owner.
- Modify `tests/services/test_model_capability.py`: prove native certification sends and validates the exact production schema.
- No database migration, package, compatibility layer, or cleanup script.

### Task 1: Type the production field and centralize the Single output contract

**Files:**
- Modify: `tests/diagnosis/test_v11_runtime.py:111-133`
- Modify: `backend/diagnosis/v11_runtime.py:16-17,139-175`
- Modify: `backend/benchmarks/rcaeval/runner.py:25-45,106-112,237-263`
- Modify: `tests/benchmarks/test_rcaeval_runner.py:1-35`

- [ ] **Step 1: Write a failing schema-semantics regression**

Import `V11SingleControlOutput` from `backend.diagnosis.v11_runtime` in `tests/diagnosis/test_v11_runtime.py`, add it to the existing parametrization, and replace the recursive assertion with schema-aware traversal:

```python
_SCHEMA_VALUE_KEYS = {"items", "additionalProperties", "contains", "not"}
_SCHEMA_LIST_KEYS = {"allOf", "anyOf", "oneOf", "prefixItems"}


def _assert_explicit_schema_semantics(schema: dict, path: tuple[str, ...] = ()) -> None:
    semantic_keys = {"type", "$ref", "allOf", "anyOf", "oneOf", "enum", "const"}
    assert semantic_keys & schema.keys(), ".".join(path) or "<root>"
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False

    for key in ("$defs", "definitions", "properties", "patternProperties"):
        for name, child in schema.get(key, {}).items():
            _assert_explicit_schema_semantics(child, (*path, key, name))
    for key in _SCHEMA_VALUE_KEYS:
        child = schema.get(key)
        if isinstance(child, dict):
            _assert_explicit_schema_semantics(child, (*path, key))
    for key in _SCHEMA_LIST_KEYS:
        for index, child in enumerate(schema.get(key, [])):
            _assert_explicit_schema_semantics(child, (*path, key, str(index)))


@pytest.mark.parametrize(
    "output_type",
    [
        LeadPlanningOutput,
        InvestigatorOutput,
        CriticOutput,
        LeadAdjudicationOutput,
        V11SingleControlOutput,
    ],
)
def test_v11_model_output_types_are_valid_strict_json_schemas(output_type):
    schema = AgentOutputSchema(output_type, strict_json_schema=True).json_schema()
    _assert_explicit_schema_semantics(schema)
```

- [ ] **Step 2: Run the regression and verify RED**

Run:

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py::test_v11_model_output_types_are_valid_strict_json_schemas -q
```

Expected: FAIL for `InvestigatorOutput` at `$defs.InvestigatorFindingDraft.properties.related_cause_type`, because the node contains only `title`.

- [ ] **Step 3: Implement the minimum shared contract fix**

In `backend/diagnosis/v11_runtime.py`, import the existing enum and replace `Any` on the draft field:

```python
from backend.domain.hypotheses import CauseType


class InvestigatorFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_type: AgentFindingType
    summary: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_ids: list[str] = Field(default_factory=list, max_length=32)
    related_cause_type: CauseType | None = None
    severity: AgentFindingSeverity = AgentFindingSeverity.MEDIUM
    rationale: str = Field(default="", max_length=512)
    gaps: list[str] = Field(default_factory=list, max_length=8)
    blocking: bool = False
    affected_entity: str | None = Field(default=None, max_length=128)
    failure_mechanism: str | None = Field(default=None, max_length=256)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=32)
```

Add the shared Single contract immediately after `InvestigatorOutput`:

```python
class V11SingleControlOutput(BaseModel):
    """Single control 的 planning 与 investigation 共用一个模型上下文。"""

    model_config = ConfigDict(extra="forbid")

    planning: LeadPlanningOutput
    investigator: InvestigatorOutput
```

In `backend/benchmarks/rcaeval/runner.py`, delete the local `SingleControlOutput`, import `V11SingleControlOutput`, and replace its two uses:

```python
turn = await self._call_model(
    actor=ExecutionActor.INVESTIGATOR.value,
    prompt=prompt,
    output_type=V11SingleControlOutput,
    # existing arguments unchanged
)
output = self._parse_output(turn.output, V11SingleControlOutput)
```

Update `tests/benchmarks/test_rcaeval_runner.py` to import `V11SingleControlOutput` from `backend.diagnosis.v11_runtime` and use it where it currently references `SingleControlOutput`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py::test_v11_model_output_types_are_valid_strict_json_schemas tests/benchmarks/test_rcaeval_runner.py -q
```

Expected: all selected tests PASS. The generated `related_cause_type` node contains `anyOf` with a string enum reference and `type: null`.

- [ ] **Step 5: Commit the typed shared contract**

```powershell
git add backend/diagnosis/v11_runtime.py backend/benchmarks/rcaeval/runner.py tests/diagnosis/test_v11_runtime.py tests/benchmarks/test_rcaeval_runner.py
git diff --cached --check
git commit -m "fix(v11): type single output schema"
```

### Task 2: Certify the exact production schema

**Files:**
- Modify: `tests/services/test_model_capability.py:3-23,181-245,257-288`
- Modify: `backend/services/model_capability.py:22-29,34-113,158-162,413-431`

- [ ] **Step 1: Write the failing exact-schema certification test**

In `tests/services/test_model_capability.py`, import the Agents SDK schema builder and shared production type:

```python
from agents import AgentOutputSchema

from backend.diagnosis.v11_runtime import V11SingleControlOutput
```

Replace the fake native response with a mechanically valid minimal production result:

```python
_PRODUCTION_RESULT = {
    "planning": {
        "decision": {
            "action": "inconclusive",
            "summary": "Capability probe completed.",
            "task_ids": [],
            "candidate_ids": [],
            "evidence_ids": [],
            "selected_skills": [],
            "stop_reason": "Capability probe only.",
        },
        "tasks": [],
    },
    "investigator": {"summary": "", "findings": [], "candidates": []},
}
```

Return `json.dumps(_PRODUCTION_RESULT)` for a native-schema request in `_FakeCompletions.create`, then add:

```python
@pytest.mark.anyio
async def test_native_certification_uses_exact_single_production_schema():
    completions = _FakeCompletions()

    artifact = await certify_endpoint_async(
        base_url=_CANONICAL_URL,
        model="compat-model",
        api_key="local-secret",
        client_factory=lambda: _FakeClient(completions),
    )

    request = next(
        item
        for item in completions.requests
        if item.get("response_format", {}).get("type") == "json_schema"
    )
    expected = AgentOutputSchema(
        V11SingleControlOutput, strict_json_schema=True
    ).json_schema()

    assert artifact.structured_output_transport == "native_json_schema"
    assert request["response_format"]["json_schema"] == {
        "name": "final_output",
        "strict": True,
        "schema": expected,
    }
    AgentOutputSchema(V11SingleControlOutput).validate_json(
        json.dumps(_PRODUCTION_RESULT)
    )
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```powershell
uv run pytest tests/services/test_model_capability.py::test_native_certification_uses_exact_single_production_schema -q
```

Expected: FAIL because the request still contains the simplified `capability_ok` schema.

- [ ] **Step 3: Generate certification input and identity from the shared type**

In `backend/services/model_capability.py`, import `AgentOutputSchema` and `V11SingleControlOutput`, delete `_STRICT_OK_SCHEMA`, and define only the minimal validated result plus shared schema helpers:

```python
from agents import AgentOutputSchema

from backend.diagnosis.v11_runtime import V11SingleControlOutput


_NATIVE_PROBE_RESULT = {
    "planning": {
        "decision": {
            "action": "inconclusive",
            "summary": "Capability probe completed.",
            "task_ids": [],
            "candidate_ids": [],
            "evidence_ids": [],
            "selected_skills": [],
            "stop_reason": "Capability probe only.",
        },
        "tasks": [],
    },
    "investigator": {"summary": "", "findings": [], "candidates": []},
}


def _native_production_schema() -> dict[str, object]:
    return AgentOutputSchema(
        V11SingleControlOutput, strict_json_schema=True
    ).json_schema()


def _native_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "final_output",
            "strict": True,
            "schema": _native_production_schema(),
        },
    }
```

Bind the schema into `capability_manifest_hash` without adding a new manifest observation:

```python
def capability_manifest_hash() -> str:
    canonical = json.dumps(
        {
            "capabilities": list(CAPABILITY_MANIFEST),
            "native_output_schema": _native_production_schema(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Change the native probe to send the shared response format and validate the returned content with the same schema:

```python
async def native_json_schema_with_tools() -> bool:
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": (
                    "Do not call read_logs. Return exactly this JSON result: "
                    "planning.decision with action=\"inconclusive\", "
                    "summary=\"Capability probe completed.\", empty task_ids, "
                    "candidate_ids, evidence_ids, selected_skills, and "
                    "stop_reason=\"Capability probe only.\"; planning.tasks empty; "
                    "investigator with summary=\"\" and empty findings and candidates."
                ),
            }
        ],
        response_format=_native_response_format(),
        tools=[_READ_LOGS_TOOL],
        tool_choice="auto",
        max_tokens=256,
    )
    content = response.choices[0].message.content if response.choices else None
    if not isinstance(content, str):
        return False
    parsed = AgentOutputSchema(V11SingleControlOutput).validate_json(content)
    return parsed.model_dump(mode="json") == _NATIVE_PROBE_RESULT
```

- [ ] **Step 4: Run capability tests and verify GREEN**

Run:

```powershell
uv run pytest tests/services/test_model_capability.py -q
```

Expected: all tests PASS; native selection uses the exact shared schema, and the existing strict-output-tool fallback test remains green.

- [ ] **Step 5: Commit production-schema certification**

```powershell
git add backend/services/model_capability.py tests/services/test_model_capability.py
git diff --cached --check
git commit -m "fix(v11): certify production output schema"
```

### Task 3: Verify source behavior and stale-artifact rejection

**Files:**
- Modify only if a failing regression exposes a defect in the files changed by Tasks 1-2.

- [ ] **Step 1: Run the targeted regression set**

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py tests/services/test_model_capability.py tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_isolation.py tests/benchmarks/test_rcaeval_evaluator.py -q
```

Expected: PASS. Fix only failures caused by Tasks 1-2; do not broaden scope.

- [ ] **Step 2: Prove the old capability artifact is stale**

Run this read-only check:

```powershell
@'
from pathlib import Path
from backend.config.settings import canonicalize_endpoint, endpoint_id
from backend.services.model_capability import (
    read_capability_artifact,
    validate_capability_for_prediction,
)

artifact = read_capability_artifact(Path(
    r"D:\agent\sre-agent\output\model_capability\349d8f7aeba75e46-gpt-5.6-terra\result.json"
))
try:
    validate_capability_for_prediction(
        artifact,
        provider="openai_compatible",
        model=artifact.model,
        endpoint_id_value=endpoint_id(canonicalize_endpoint("https://www.cctq.ai/v1")),
        expected_parallelism=3,
        repository_root=Path.cwd(),
    )
except ValueError as exc:
    print(type(exc).__name__, str(exc))
else:
    raise SystemExit("old artifact unexpectedly remained valid")
'@ | .\.venv\Scripts\python.exe -
```

Expected: `ValueError` reporting stale code revision, source manifest, or capability manifest.

- [ ] **Step 3: Run the full suite**

```powershell
uv run pytest -q
git diff --check
git status --short --branch
```

Expected: full suite PASS, no diff errors, and only intentional commits ahead of `origin/main`.

- [ ] **Step 4: Commit any verification-only correction**

Skip this step when Step 3 is clean and no files changed. If a scoped correction was required, stage only that correction and its failing-first regression:

```powershell
git add backend/diagnosis/v11_runtime.py backend/benchmarks/rcaeval/runner.py backend/services/model_capability.py tests/diagnosis/test_v11_runtime.py tests/benchmarks/test_rcaeval_runner.py tests/services/test_model_capability.py
git diff --cached --check
git commit -m "test(v11): close production schema regression"
```

Before committing, `git diff --cached --name-only` must be a subset of the six paths above; unmodified paths are ignored by `git add`.

### Task 4: Re-certify and run one isolated live smoke

**Files:**
- No source files.
- Create: a new ignored capability artifact under `output/model_capability/`.
- Create: a uniquely named SQLite database and JSON summary under `D:\data\RCAEval\v11-smoke`.

- [ ] **Step 1: Re-certify in the secret-bearing PowerShell**

Use the existing certification command with:

```text
DIAGOPS_AGENTS_BASE_URL=https://www.cctq.ai/v1
DIAGOPS_AGENTS_MODEL=gpt-5.6-terra
parallelism=3
```

Expected: `result=passed`; the selected transport is whichever of `native_json_schema` or `strict_output_tool` passes its complete live probe. Record the new artifact path and hash; do not reuse `aaaced56...`.

- [ ] **Step 2: Run one OB30 Single smoke with 24k**

Reuse the isolated single-case harness from the approved diagnostic workflow, but point it at the new capability artifact and use a new database name. Keep:

```text
token_budget=24000
max_turns=8
tool_budget=8
timeout_seconds=120
```

The PowerShell wrapper must capture `$LASTEXITCODE` and print `OB30_SINGLE_24K_SMOKE_COMPLETED` only when it is zero.

Expected:

```text
completed=true
failure_category=null
input_tokens>0
output_tokens>0
total_tokens=input_tokens+output_tokens
```

- [ ] **Step 3: Inspect live evidence without opening labels or touching the formal ledger**

Read the smoke JSON and SQLite runtime events. Require at least one `model.started` and `model.completed`, no `model.failed`, and no non-read-only tool call. Preserve the database and JSON as smoke evidence.

- [ ] **Step 4: Stop before formal reauthorization**

Report the measured input, output, total tokens, tool calls, duration, transport, and number of model turns. Do not generate a reauthorization token, change the formal ledger, or start a 30-case prediction until the user approves a new formal Single/Multi/equal-token budget design.

## Review ledger

| finding_id | origin | severity | root cause | disposition | resolution | regression check | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | missed | high | Task 2 Step 3 原 prompt 未钉死逐字相等门禁要求的固定字符串，真实模型几乎不可能复述，live 时 native_json_schema 几乎不可能被选中（fail closed，不会误认证） | accepted | prompt 改为逐字规定探针返回内容（`model_capability.py`），保持精确相等门禁不变；用户 2026-08-13 确认最小修复 | `uv run pytest tests/services/test_model_capability.py -q` → 15 passed | closed |
| R2 | missed | low | `test_rcaeval_runner.py` 保留旧 `additionalProperties` 弱断言，与 runtime 强化遍历重复 | accepted | 保留为冗余防御，不扩大本次范围 | — | closed |
| R3 | missed | low | `capability_manifest_hash` 绑定 `native_output_schema` 后无 hash 敏感性自动化测试 | accepted | 新增 `test_capability_manifest_hash_tracks_the_shared_production_schema`（确定性 + 变异 schema 必改 hash） | 同上 15 passed | closed |
