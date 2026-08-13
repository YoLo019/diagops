# RCAEval Single Smoke Root-Cause Fix Implementation Plan

> **For agentic workers:** Execute task-by-task with RED→GREEN discipline. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `AdaptiveToolSession.invoke` dispatch all nine frozen tools without `AttributeError`, so the RCAEval Single control survives `query_related_alerts`, `query_traces`, `read_runtime_state`, and `lookup_memory` calls.

**Root cause (proven by the diagnostic smoke in
`2026-08-13-rcaeval-single-smoke-value-error.md` § Diagnostic Result):**
`_validate_scope` (`backend/diagnosis/adaptive_tools.py:623-631`) and
`_query_fingerprint` (`adaptive_tools.py:696-709`) hard-access
`query.start_time` / `query.end_time` from the `QueryWindow` contract, but
`QUERY_MODELS_BY_TOOL` (`backend/tools/provider_tools.py:71-82`) also maps
tools to `ScopedTelemetryQuery` (`window_start`/`window_end`, optional pair:
`query_traces`, `read_runtime_state`, `query_related_alerts`) and to
`MemoryQuery` (no window: `lookup_memory`). The resulting `AttributeError` is
not covered by the dispatch `except (KeyError, TypeError, ValueError)` clause
(`adaptive_tools.py:274`), escapes `invoke`, and is wrapped by the Agents SDK
as `UserError`, failing the whole single control.

**Frozen Constraints:** Keep the nine-tool manifest, tool input schemas,
domain query models (`QueryWindow`, `ScopedTelemetryQuery`, `MemoryQuery`
fields untouched), retry policy, budgets, transport, persisted payloads, and
`CasePrediction` unchanged. Scope/intersection semantics for `QueryWindow`
tools must remain byte-identical. No new dependency.

**Tech Stack:** Python 3.12, pytest, Pydantic v2.

---

## File structure

- Modify `backend/diagnosis/adaptive_tools.py`: one window-extraction helper; widen `_validate_scope` and `_query_fingerprint` to the three query contracts.
- Modify `tests/diagnosis/test_adaptive_tools.py`: dispatch coverage for the four previously crashing tools plus fingerprint/scope regressions.
- No database migration, no schema change, no manifest change.

### Task 1: Contract-aware window handling in the dispatch path

**Files:**
- Modify: `backend/diagnosis/adaptive_tools.py:20,623-631,696-709`
- Test: `tests/diagnosis/test_adaptive_tools.py`

- [ ] **Step 1: Write the failing dispatch regression for scoped-window tools**

Add to `tests/diagnosis/test_adaptive_tools.py`. The existing `QueryProvider`
fake returns empty success when `evidence_id=None` without touching query
window fields, so it is safe for these tools. Build the session with a frozen
`agent_manifest` (pattern mirrors
`tests/diagnosis/test_v11_runtime.py:718-739`) because legacy
`TOOLS_BY_AGENT` does not include these tools:

```python
def _manifest_session(providers):
    registry = build_provider_tool_registry(ProviderRegistry(providers))
    return AdaptiveToolSession(
        event=_event(),
        seed_evidence=[],
        registry=registry,
        task_ids={"investigator-1": "task-timeline"},
        agent_manifest=registry.agent_manifest(),
    )


@pytest.mark.anyio
async def test_scoped_telemetry_and_memory_tools_dispatch_without_window():
    session = _manifest_session(
        [
            QueryProvider("query_related_alerts", EvidenceProvider.RELATED_ALERT, None),
            QueryProvider("query_traces", EvidenceProvider.TRACE, None),
            QueryProvider("read_runtime_state", EvidenceProvider.RUNTIME_STATE, None),
            QueryProvider("lookup_memory", EvidenceProvider.VERIFIED_INCIDENT, None),
        ]
    )

    for tool_name in (
        "query_related_alerts",
        "query_traces",
        "read_runtime_state",
        "lookup_memory",
    ):
        response = json.loads(
            await session.invoke(
                "investigator-1", tool_name, json.dumps({"reason": "smoke"}), 1
            )
        )
        assert response["status"] == "success", tool_name
```

(`QueryProvider` and `EvidenceProvider` need no new imports: both are already
imported in this file — verify `EvidenceProvider` members against
`backend/domain/evidence.py:26-35`.)

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
uv run pytest tests/diagnosis/test_adaptive_tools.py::test_scoped_telemetry_and_memory_tools_dispatch_without_window -q
```

Expected: FAIL with `AttributeError: 'RelatedAlertQuery' object has no attribute 'end_time'` escaping `session.invoke` (the exact smoke failure mode). If a different error appears, stop and re-diagnose.

- [ ] **Step 3: Write the failing scope-enforcement and fingerprint regressions**

Prove the `ScopedTelemetryQuery` window keeps real scope semantics and
fingerprint normalization (timezone-spelling equality), and that
`QueryWindow` tools are untouched:

```python
@pytest.mark.anyio
async def test_scoped_window_intersection_is_enforced_without_attribute_error():
    provider = QueryProvider(
        "query_related_alerts", EvidenceProvider.RELATED_ALERT, None
    )
    session = _manifest_session([provider])

    outside = json.loads(
        await session.invoke(
            "investigator-1",
            "query_related_alerts",
            json.dumps(
                {
                    "reason": "outside incident window",
                    "window_start": "2026-07-15T06:00:00+00:00",
                    "window_end": "2026-07-15T06:30:00+00:00",
                }
            ),
            1,
        )
    )
    assert outside["status"] == "failed"
    assert outside["warning"] == "tool input outside investigation scope"
    assert provider.calls == 0


@pytest.mark.anyio
async def test_scoped_window_fingerprint_normalizes_timezone_spelling():
    provider = QueryProvider(
        "query_related_alerts", EvidenceProvider.RELATED_ALERT, None
    )
    session = _manifest_session([provider])
    base = {
        "window_start": "2026-07-15T07:50:00+00:00",
        "window_end": "2026-07-15T08:10:00+00:00",
    }

    first = json.loads(
        await session.invoke(
            "investigator-1",
            "query_related_alerts",
            json.dumps({"reason": "first", **base}),
            1,
        )
    )
    duplicate = json.loads(
        await session.invoke(
            "investigator-1",
            "query_related_alerts",
            json.dumps(
                {
                    "reason": "same window, +08:00 spelling",
                    "window_start": "2026-07-15T15:50:00+08:00",
                    "window_end": "2026-07-15T16:10:00+08:00",
                }
            ),
            1,
        )
    )
    assert first["status"] == "success"
    assert duplicate["status"] == "skipped"
    assert provider.calls == 1
```

Incident window for `_event()` is `2026-07-15T07:30–08:30Z`
(`started_at=08:00Z`, `time_window_minutes=30`), so `06:00–06:30Z` is outside
and `07:50–08:10Z` intersects.

Run the two tests and verify RED: both currently raise `AttributeError`
instead of returning a response.

- [ ] **Step 4: Implement the minimal contract-aware window helper**

In `backend/diagnosis/adaptive_tools.py`, extend the line-20 import:

```python
from backend.domain.tool_queries import (
    DependencyQuery,
    MemoryQuery,
    QueryWindow,
    ScopedTelemetryQuery,
)
```

Add one private helper beside `_query_fingerprint`:

```python
def _query_window(
    query: QueryWindow | ScopedTelemetryQuery | MemoryQuery,
) -> tuple[datetime, datetime] | None:
    if isinstance(query, QueryWindow):
        return query.start_time, query.end_time
    if isinstance(query, ScopedTelemetryQuery):
        if query.window_start is None or query.window_end is None:
            return None
        return query.window_start, query.window_end
    return None
```

Rewrite `_validate_scope` (semantics for `QueryWindow` unchanged; no-window
queries skip only the intersection check; the `DependencyQuery` target check
is preserved for every contract):

```python
def _validate_scope(
    self, query: QueryWindow | ScopedTelemetryQuery | MemoryQuery
) -> None:
    window = _query_window(query)
    if window is not None:
        incident_span = timedelta(minutes=self.event.time_window_minutes)
        incident_start = self.event.started_at - incident_span
        incident_end = self.event.started_at + incident_span
        if window[1] < incident_start or window[0] > incident_end:
            raise ValueError("query window does not intersect incident window")
    if isinstance(query, DependencyQuery):
        if query.target and query.target not in self._allowed_targets:
            raise ValueError("dependency target outside investigation scope")
```

Rewrite `_query_fingerprint` to normalize whichever window fields exist:

```python
def _query_fingerprint(
    tool_name: str, query: QueryWindow | ScopedTelemetryQuery | MemoryQuery
) -> str:
    values = query.model_dump(mode="json", exclude={"reason"})
    window = _query_window(query)
    if window is not None:
        if isinstance(query, QueryWindow):
            values["start_time"] = window[0].astimezone(UTC).isoformat()
            values["end_time"] = window[1].astimezone(UTC).isoformat()
        else:
            values["window_start"] = window[0].astimezone(UTC).isoformat()
            values["window_end"] = window[1].astimezone(UTC).isoformat()
    for name in ("keywords", "levels", "metric_names"):
        items = values.get(name)
        if not isinstance(items, list):
            continue
        normalized = [str(item) for item in items]
        if name in {"keywords", "levels"}:
            normalized = [item.casefold() for item in normalized]
        values[name] = sorted(set(normalized))
    canonical = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()
```

Do not touch the dispatch `except (KeyError, TypeError, ValueError)` clause:
after this fix no `AttributeError` remains possible on this path, and widening
the except clause would mask future contract drift.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
uv run pytest tests/diagnosis/test_adaptive_tools.py -q
```

Expected: all tests pass, including the three new ones and every pre-existing
`QueryWindow` dispatch test (byte-identical legacy behavior).

- [ ] **Step 6: Commit the dispatch contract fix**

```powershell
git add -- backend/diagnosis/adaptive_tools.py tests/diagnosis/test_adaptive_tools.py
git diff --cached --check
git commit -m "fix(v11): dispatch scoped telemetry and memory tools"
```

### Task 2: Regression gates and stale-artifact rejection

**Files:**
- Modify only if a failing regression exposes a defect caused by Task 1.

- [ ] **Step 1: Run the affected suites**

```powershell
uv run pytest tests/diagnosis tests/benchmarks tests/services/test_model_capability.py -q
```

Expected: PASS with only pre-existing skips/warnings.

- [ ] **Step 2: Prove the pre-fix capability artifact is stale**

The Task 1 commit moves HEAD, so the diagnostic-smoke artifact
(`bfd1cf692f0b9e4d4b13ec3ba7a2cea6a4aecb51c0f41531eb46de25b682ccfe`, code
revision `c90aa6c`) must now be rejected:

```powershell
uv run python -c "from pathlib import Path; from backend.config.settings import canonicalize_endpoint, endpoint_id; from backend.services.model_capability import read_capability_artifact, validate_capability_for_prediction; artifact = read_capability_artifact(Path(r'D:\agent\sre-agent\output\model_capability\349d8f7aeba75e46-gpt-5.6-terra\result.json')); validate_capability_for_prediction(artifact, provider='openai_compatible', model=artifact.model, endpoint_id_value=endpoint_id(canonicalize_endpoint('https://www.cctq.ai/v1')), expected_parallelism=3, repository_root=Path.cwd())"
```

Expected: `ValueError` reporting stale code revision (or stale source
manifest). Exit non-zero. If it unexpectedly passes, stop and diagnose.

- [ ] **Step 3: Run the full suite and linters**

```powershell
uv run pytest -q
uv run ruff check .
git diff --check
```

Expected: full suite PASS with only pre-existing skips/warnings; Ruff clean;
diff-check clean.

- [ ] **Step 4: Commit any verification-only correction**

Skip when Steps 1-3 are clean. If a scoped correction was required, stage only
that correction plus its failing-first regression and commit
`test(v11): close dispatch contract regression`.

### Task 3: Authorized acceptance smoke

**Files:**
- No source files.
- Create: a new ignored capability artifact under `output/model_capability/`.
- Create: a uniquely named SQLite database and JSON summary under `D:\data\RCAEval\v11-smoke`.

- [ ] **Step 1: Re-certify in the secret-bearing PowerShell**

The artifact binds the code revision, so certification must rerun at the Task
1 commit. Reuse the full block in `C:\Users\林佳威\Desktop\task2-diagnostic-smoke.txt`
(第 0 步), keeping `DIAGOPS_AGENTS_BASE_URL=https://www.cctq.ai/v1`,
`DIAGOPS_AGENTS_MODEL=gpt-5.6-terra`, `parallelism=3`, and the default 30s
certification deadline. Do not relax the deadline; on repeated
`probe exceeded certification deadline`, stop and report instead.

Expected: `result=passed`, `code_revision` equals the Task 1 commit, a new
artifact hash, transport `native_json_schema` or `strict_output_tool`
(whichever passes its complete live probe).

- [ ] **Step 2: Run exactly one OB30 Single 24k smoke**

Continue with the same desktop script (Task 2 Step 2 portion): the attachment
harness with `token_budget=24000`, `max_turns=8`, `tool_budget=8`,
`timeout_seconds=120`, case `re2-11caeecc5351ba99`, fresh database/result
paths.

Final acceptance (all required):

```text
completed=true
persisted run status=completed
model.completed >= 1
tool.completed >= 1
model.failed = 0
read_only_violations = 0
input_tokens > 0, output_tokens > 0, total = input + output
```

A returned `rcaeval single control failed` warning, a non-completed run, or
any `model.failed` event means FAILURE: stop, preserve the database/JSON, and
report; do not retry with modified budgets.

- [ ] **Step 3: Report and stop before formal reauthorization**

Report measured tokens, tool calls, duration, transport, model turns, the
artifact hash, and the smoke evidence paths. Do not generate a
reauthorization token, touch the formal ledger, open labels, or start any
30-case prediction.

## Deferred follow-ups (non-blocking, recorded from the diagnostic-plan review)

1. `redact_text` does not mask bare base64, separator-less tokens, or
   comma-containing bearer strings (`backend/safety/redaction.py:73-83`). The
   smoke warning only reaches local stderr/logs today; revisit before any
   diagnostic output is persisted or exported.
2. `_safe_exception_diagnostic` derives the repository root from
   `Path.cwd()`; from a foreign cwd the location degrades to `"unknown"`.
   Optionally derive from `Path(__file__)` later.
3. The diagnostic location can point into `.venv` SDK frames because `.venv`
   is repository-relative; the message field remains the root-cause carrier.
