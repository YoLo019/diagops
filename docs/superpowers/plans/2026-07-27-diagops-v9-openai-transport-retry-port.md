# DiagOps V9 OpenAI Transport Retry Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the proven V8.2 two-retry OpenAI transport behavior to V9 and prepare a fresh, non-overwriting recovery for Fixed `Market/cloudbed-1:37`.

**Architecture:** Keep the existing request-owned `AsyncOpenAI` client and 180-second Runtime deadline. Change only the SDK retry count and its direct construction test; preserve all failure contracts and prior benchmark artifacts.

Two retries exhausted still map through the existing
`FailureCategory.TRANSPORT` path; no fallback prediction is introduced.

**Tech Stack:** Python 3.12, OpenAI Python SDK, pytest, Ruff, PowerShell, SQLite.

---

### Task 1: Port the bounded SDK retry behavior with TDD

**Files:**
- Modify: `tests/diagnosis/test_openai_model.py`
- Modify: `backend/diagnosis/openai_model.py`

- [ ] **Step 1: Change the existing test to require two retries**

Rename the test to:

```python
async def test_openai_responses_model_owns_bounded_retry_client(monkeypatch):
```

and change its constructor assertion to:

```python
assert captured == {"timeout": 55.0, "max_retries": 2}
```

- [ ] **Step 2: Run the target test and verify RED**

Run:

```powershell
uv run pytest tests/diagnosis/test_openai_model.py -q
```

Expected: one failure showing actual `max_retries` is `0` while expected is `2`.

- [ ] **Step 3: Apply the minimal production change**

In `backend/diagnosis/openai_model.py`, update the function documentation and
constructor only:

```python
"""创建由当前运行独占且最多自动重试两次的 OpenAI Responses model。"""
client = AsyncOpenAI(
    timeout=transport_timeout_seconds(overall_timeout_seconds),
    max_retries=2,
)
```

- [ ] **Step 4: Run the target test and verify GREEN**

Run:

```powershell
uv run pytest tests/diagnosis/test_openai_model.py -q
```

Expected: all tests in the file pass.

### Task 2: Verify and commit the V9 code change

**Files:**
- Verify: `backend/diagnosis/openai_model.py`
- Verify: `tests/diagnosis/test_openai_model.py`

- [ ] **Step 1: Run focused runtime regression tests**

Run:

```powershell
uv run pytest tests/diagnosis/test_openai_model.py tests/diagnosis/test_agents_runtime.py -q
```

Expected: all selected tests pass with no warnings.

- [ ] **Step 2: Run full Python verification**

Run:

```powershell
uv run ruff check .
uv run pytest -q
```

Expected: Ruff exits zero and the complete pytest suite passes.

- [ ] **Step 3: Commit only the implementation and test**

Run:

```powershell
git add -- backend/diagnosis/openai_model.py tests/diagnosis/test_openai_model.py
git diff --cached --check
git commit -m "fix: retry transient OpenAI transport failures"
```

Expected: one commit containing only the two intended files.

### Task 3: Prepare the next one-case recovery

**Files:**
- Reuse: `D:\data\OpenRCA\prepared-v9-0e53c66\recovery-batch04-fixed-cloudbed1-37-557638a.json`
- Create: `D:\data\OpenRCA\commands\run-v9-batch04-fixed37-recovery-retry.ps1`
- Modify: `D:\agent\sre-agent\docs\superpowers\current.md`
- Modify: `D:\agent\sre-agent\docs\superpowers\plans\2026-07-17-diagops-v9-durable-observable-runtime-implementation-plan.md`

- [ ] **Step 1: Derive fresh targets from the new commit**

Use the full new commit as `$expectedCommit` and its seven-character prefix in
new output, database and transcript paths. Reuse the exact frozen one-case
index; do not create or modify case data.

- [ ] **Step 2: Create the non-overwriting recovery script**

Copy the accepted validation contract from
`D:\data\OpenRCA\commands\run-v9-batch04-fixed37-recovery.ps1`, changing only
the expected commit and three output target paths. Preserve the 180-second
timeout, Base URL/API Key prompts, zero-transcript-error gates and Runtime
database audit.

- [ ] **Step 3: Verify the operational artifact**

Parse the PowerShell AST, compile its embedded Python, assert the reused index
still matches the frozen Batch04 source object, confirm all fresh targets are
absent, and record SHA-256.

- [ ] **Step 4: Update the live tracker**

Record the new V9 commit, code verification evidence, script hash and exact next
command. Keep B5 `in_progress` until the user-run recovery returns
`RECOVERY_ACCEPTED`.
