# RCAEval SS15 Protocol Implementation Plan

Status: `implemented`; the formal SS15/TT90 run remains blocked by the
capability admission recorded in `../current.md`.

**Goal:** Replace the project-local Sock Shop sealed-validation protocol with a deterministic 15-case `SS15` partition while preserving the upstream `RE2-SS` 90-case source contract and leaving `OB30`/`TT90` unchanged.

**Architecture:** Keep source validation at 270 cases (`5 services × 6 faults × 3 repetitions` per system). Add a two-stage SS15 selector that ranks 30 Sock Shop cells by a seed-qualified digest, chooses 15 cells, then chooses one repetition per selected cell. Rename the local enum/CLI/evaluator contract from `ss30` to `ss15`; generate a new custodian root instead of migrating historical SS30 outputs.

**Tech Stack:** Python 3.12, Pydantic models, SQLite custodian ledger, pytest, Ruff, PowerShell desktop launch script, SHA-256 canonical manifests.

---

### Task 1: Add RED tests for the SS15 selector

**Files:**
- Modify: `tests/benchmarks/test_rcaeval_prepare.py`
- Replace: `tests/benchmarks/fixtures/rcaeval-selector-golden-v1.json` with `tests/benchmarks/fixtures/rcaeval-selector-golden-v2.json`

- [ ] **Step 1: Change the fixture expectations before production code**

Update the synthetic preparation assertions from `RcaEvalPartition.SS30`/30 to
`RcaEvalPartition.SS15`/15. Add the deterministic helper below beside
`_expected_selection`:

```python
def _expected_ss15_selection(system: RcaEvalSystem) -> set[str]:
    taxonomy = TAXONOMY[system]
    cells = [
        (service, fault)
        for service in taxonomy.services
        for fault in taxonomy.faults
    ]
    selected_cells = sorted(
        cells,
        key=lambda cell: _sha256_bytes(
            f"{TEST_SEED}:cell:{system.value}:{cell[0]}:{cell[1]}".encode()
        ),
    )[:15]
    return {
        min(
            (
                _case_id(system, service, fault, repetition)
                for repetition in (1, 2, 3)
            ),
            key=lambda item: _sha256_bytes(f"{TEST_SEED}:{item}".encode()),
        )
        for service, fault in selected_cells
    }
```

Add assertions that the SS15 entries contain exactly 15 unique `(service, fault)`
cells, that the source IDs equal `_expected_ss15_selection`, and that the report
still reports `source_case_count == 270` and TT90 count `90`.

- [ ] **Step 2: Run the focused tests and observe the expected RED state**

Run:

```powershell
uv run pytest tests/benchmarks/test_rcaeval_prepare.py -q
```

Expected: collection or assertion failures because `RcaEvalPartition.SS15` and
the new partition count do not exist yet. Do not change the test back to make it
pass.

### Task 2: Implement the SS15 partition and selector

**Files:**
- Modify: `backend/benchmarks/rcaeval/models.py:49-67, 209, 264`
- Modify: `backend/benchmarks/rcaeval/prepare.py:320-351`
- Test: `tests/benchmarks/test_rcaeval_prepare.py`

- [ ] **Step 1: Rename the persisted partition enum and constants**

Make the model contract exactly:

```python
class RcaEvalPartition(StrEnum):
    """本地留置分区：OB30 开发、SS15 封存验证、TT90 最终留置。"""

    OB30 = "ob30"
    SS15 = "ss15"
    TT90 = "tt90"


SYSTEM_TO_PARTITION = {
    RcaEvalSystem.ONLINE_BOUTIQUE: RcaEvalPartition.OB30,
    RcaEvalSystem.SOCK_SHOP: RcaEvalPartition.SS15,
    RcaEvalSystem.TRAIN_TICKET: RcaEvalPartition.TT90,
}

EXPECTED_PARTITION_COUNTS = {
    RcaEvalPartition.OB30: 30,
    RcaEvalPartition.SS15: 15,
    RcaEvalPartition.TT90: 90,
}
```

Rename `materialize_ss30_configurations` to
`materialize_ss15_configurations`; its four budgets and topology remain byte-for-byte
the same. Do not add a compatibility alias for the deleted SS30 enum or function.

- [ ] **Step 2: Implement the two-stage selector**

Keep `_validate_cells` unchanged so all 270 source cases and all three repetitions
per cell are still required. Replace the SS branch of `_select_partitions` with
this behavior:

```python
        cells: dict[tuple[str, str], list[_SourceCase]] = {}
        for case in by_system[system]:
            key = (case.descriptor.service, case.descriptor.fault)
            cells.setdefault(key, []).append(case)
        if partition is RcaEvalPartition.SS15:
            cell_keys = sorted(
                cells,
                key=lambda cell: _sha256_bytes(
                    f"{seed}:cell:{system.value}:{cell[0]}:{cell[1]}".encode()
                ),
            )[:EXPECTED_PARTITION_COUNTS[partition]]
        else:
            cell_keys = sorted(cells)
        chosen = [
            min(
                cells[cell],
                key=lambda case: _sha256_bytes(
                    f"{seed}:{case.case_id}".encode()
                ),
            ).case_id
            for cell in cell_keys
        ]
        selection[partition] = tuple(sorted(chosen))
```

Retain the TT90 all-repetition branch and the final count guard. Update the
selector docstring/comment to say `OB/SS15` rather than `OB/SS`.

- [ ] **Step 3: Run the selector tests GREEN and inspect the deterministic report**

Run:

```powershell
uv run pytest tests/benchmarks/test_rcaeval_prepare.py -q
```

Expected: all preparation tests pass, including `OB30=30`, `SS15=15`, `TT90=90`,
full source count 270, unique SS15 cells 15, and repeat preparation byte equality.

- [ ] **Step 4: Compute and freeze the v2 golden vector**

Use the passing `inspect_source` result to write
`tests/benchmarks/fixtures/rcaeval-selector-golden-v2.json` with
`schema_version: "rcaeval-selector-golden-v2"`, the unchanged selection seed, and
the exact `ob30` and `ss15` source ID lists. Update the golden-vector test to load
v2, keep `tt90` out of the vector, and remove v1 so no test can silently use the old
30-case SS selection.

### Task 3: Add RED tests for formal SS15 cardinality and CLI identity

**Files:**
- Modify: `tests/benchmarks/test_rcaeval_runner.py`
- Modify: `tests/benchmarks/test_rcaeval_evaluator.py`
- Modify: `tests/benchmarks/test_rcaeval_isolation.py`
- Modify: `tests/benchmarks/test_rcaeval_custodian_ledger.py`

- [ ] **Step 1: Rename formal test fixtures to SS15 and set the new count**

Replace all live references to `RcaEvalPartition.SS30`, `partition="ss30"`,
`_synthetic_ss30_artifact`, and `materialize_ss30_configurations` with their SS15
names. In tiny manifests use `RcaEvalPartition.SS15: 0` where the fixture contains
no sealed cases. Rename test names and temporary filenames so failure output cannot
refer to a nonexistent SS30 protocol.

- [ ] **Step 2: Add a formal 15-case acceptance test**

Extend the evaluator fixture helper so it can create exactly 15 labels/predictions
for SS15. Add one test that evaluates four complete SS15 bundles and asserts every
summary has `case_count == 15`. Add a companion test that removes one prediction and
expects `ValueError` matching `cardinality|15`, proving 14/15 cannot freeze.

- [ ] **Step 3: Add a negative test for the deleted protocol name**

Exercise the CLI argument parser or launch construction with `partition="ss30"`
and assert it rejects the value before a model call. The valid choices must include
`ss15` and retain `ob30`/`tt90` where applicable.

- [ ] **Step 4: Run the contract tests and observe the expected RED state**

Run:

```powershell
uv run pytest tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_evaluator.py tests/benchmarks/test_rcaeval_isolation.py tests/benchmarks/test_rcaeval_custodian_ledger.py -q
```

Expected: failures identify the old enum, old CLI value, or old 30-case formal gate.

### Task 4: Implement SS15 in the runtime launcher and evaluator

**Files:**
- Modify: `backend/benchmarks/rcaeval/__main__.py:70-150, 510-565, 760-780`
- Modify: `backend/benchmarks/rcaeval/runner.py:558, 613`
- Modify: `backend/benchmarks/rcaeval/evaluator.py:44-48, 282-315, 477, 719-738`
- Test: `tests/benchmarks/test_rcaeval_runner.py`, `tests/benchmarks/test_rcaeval_evaluator.py`, `tests/benchmarks/test_rcaeval_isolation.py`, `tests/benchmarks/test_rcaeval_custodian_ledger.py`

- [ ] **Step 1: Update CLI partition choices and formal branches**

Use `("ob30", "ss15", "tt90")` for prediction; use `("ss15", "tt90")` for
freeze-set/evaluate. Change every SS30 branch to SS15, with `count = 15` and all
four configurations. Keep the TT90 branch at 90 and its two intended configurations.
Update help text from “SS30 evaluation” to “SS15 evaluation”.

- [ ] **Step 2: Update runner partition validation and messages**

Accept `{"ob30", "ss15", "tt90"}` in `RcaEvalCaseRunner.run_case` and change the
configuration guard message to `SS15 requires exactly four configurations`. Rename
any remaining SS30-only test helper references; no runtime topology or retry limits
change.

- [ ] **Step 3: Update evaluator formal policy gates**

Build `FORMAL_CASE_COUNTS` from `ss15` and `tt90`. Require
`sealed_validation.partition.value == "ss15"`, compare summaries against
`FORMAL_CASE_COUNTS["ss15"]`, and update rejection messages to say `SS15` and `15`.
The formal configuration set remains all four for SS15 and only intended single/multi
for TT90. The equal-token 3× budget checks remain unchanged.

- [ ] **Step 4: Run the contract tests GREEN**

```powershell
uv run pytest tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_evaluator.py tests/benchmarks/test_rcaeval_isolation.py tests/benchmarks/test_rcaeval_custodian_ledger.py -q
```

Expected: all renamed tests pass, including the 15-case formal bundle and the
rejection of the deleted `ss30` value.

### Task 5: Reconcile remaining code/tests and documentation

**Files:**
- Modify: `tests/benchmarks/test_rcaeval_prepare.py`, `tests/benchmarks/test_rcaeval_runner.py`, `tests/benchmarks/test_rcaeval_evaluator.py`, `tests/benchmarks/test_rcaeval_isolation.py`, `tests/benchmarks/test_rcaeval_custodian_ledger.py`
- Modify: `docs/superpowers/current.md`
- Modify: `docs/superpowers/plans/2026-08-02-diagops-v11-adaptive-multi-agent-rca-implementation-plan.md`

- [ ] **Step 1: Prove no live code path still accepts SS30**

Run:

```powershell
rg -n 'RcaEvalPartition\.SS30|partition[= ]+"ss30"|choices=.*ss30|FORMAL_CASE_COUNTS\["ss30"\]|materialize_ss30' backend tests
```

The command must return no output. Historical prose and old external artifact
paths are handled separately below and must not be blindly replaced.

- [ ] **Step 2: Add an explicit current-status amendment**

At the active-status section of `docs/superpowers/current.md`, record that the
formal Sock Shop protocol is now SS15, with 15 cases and a newly generated custodian
root; state that prior SS30 epochs remain immutable historical evidence. Update the
active M5 target/count wording in the implementation plan, but leave dated SS30
failure entries unchanged so their database and artifact references remain truthful.

- [ ] **Step 3: Run documentation integrity checks**

```powershell
git diff --check
```

### Task 6: Update the desktop launch script without touching credentials

**Files:**
- Modify: `C:/Users/林佳威/Desktop/task14-ss30-single-intended-epoch7.txt`

- [ ] **Step 1: Switch the script to the new custodian root and protocol**

Change only the protocol-specific values:

```powershell
$ssRoot = "D:\data\RCAEval\v11-m5-ss15"
$epochRoot = "$ssRoot\epoch0"
$epochDatabase = "$ssRoot\ss15-single_intended-epoch0.db"
--runtime "$ssRoot\runtime"
--partition ss15
--custodian-manifest "$ssRoot\custodian-manifest.json"
--label-package "$ssRoot\labels"
```

Change status labels/messages from `SS30_*` to `SS15_*`. Keep API key input
process-only, do not add a key or base URL to the file, and do not modify the
historical `v11-m5-ss30` root.

- [ ] **Step 2: Parse the PowerShell script before using it**

```powershell
$script = Get-Content -Raw -LiteralPath 'C:\Users\林佳威\Desktop\task14-ss30-single-intended-epoch7.txt'
[scriptblock]::Create($script) | Out-Null
Write-Output 'SS15_SCRIPT_PARSE_OK'
```

- [ ] **Step 3: Verify repository documentation and record the external script in the handoff**

Run `git diff --check` and inspect `git status --short`. The desktop file is
outside the repository and is verified by its parse result; no credential
material is committed.

### Task 7: Generate and verify the new SS15 custodian packages

**Files/External artifacts:**
- Read: `D:\data\RCAEval\source-normalized`
- Read: `D:\data\RCAEval\pin-v11-m0.json`
- Create: `D:\data\RCAEval\v11-m5-ss15`
- Preserve: `D:\data\RCAEval\v11-m5-ss30`

- [ ] **Step 1: Preview the new selection without writing output**

```powershell
uv run python -m backend.benchmarks.rcaeval inspect `
  --source 'D:\data\RCAEval\source-normalized' `
  --pin 'D:\data\RCAEval\pin-v11-m0.json'
```

Assert from the JSON output: `source_case_count=270`, `ob30=30`, `ss15=15`,
`tt90=90`, and 15 SS15 source IDs.

- [ ] **Step 2: Prepare into a new empty root**

```powershell
uv run python -m backend.benchmarks.rcaeval prepare `
  --source 'D:\data\RCAEval\source-normalized' `
  --pin 'D:\data\RCAEval\pin-v11-m0.json' `
  --output 'D:\data\RCAEval\v11-m5-ss15'
```

The command must create new runtime/labels packages and a new custodian manifest;
it must not modify the SS30 root.

- [ ] **Step 3: Verify the generated package and ledger identities**

```powershell
$root = 'D:\data\RCAEval\v11-m5-ss15'
$runtime = Get-Content -Raw -LiteralPath "$root\runtime\manifest.json" | ConvertFrom-Json
$labels = Get-Content -Raw -LiteralPath "$root\labels\labels.json" | ConvertFrom-Json
if ($runtime.partition_counts.ss15 -ne 15) { throw 'ss15 runtime count is not 15' }
if (($runtime.cases | Where-Object partition -eq 'ss15').Count -ne 15) { throw 'ss15 runtime cases are not 15' }
if (($labels.entries | Where-Object partition -eq 'ss15').Count -ne 15) { throw 'ss15 labels are not 15' }
if (($runtime.cases | Where-Object partition -eq 'tt90').Count -ne 90) { throw 'tt90 changed' }
Write-Output "SS15_PACKAGE_OK runtime_manifest=$($runtime.manifest_hash)"
```

Verify runtime/labels file sets remain disjoint and compare the old SS30 root's
manifest hash before and after preparation; a changed old hash is a failure.

### Task 8: Run the full verification gates and hand off

**Files:**
- Read-only verification across the modified code, tests, desktop script, and new artifact root.

- [ ] **Step 1: Run all RCAEval and contract tests**

```powershell
uv run pytest tests/benchmarks/test_rcaeval_prepare.py tests/benchmarks/test_rcaeval_runner.py tests/benchmarks/test_rcaeval_evaluator.py tests/benchmarks/test_rcaeval_isolation.py tests/benchmarks/test_rcaeval_custodian_ledger.py tests/benchmarks/test_rcaeval_audit.py -q
```

- [ ] **Step 2: Run the adjacent runtime/model gates**

```powershell
uv run pytest tests/diagnosis/test_v11_runtime.py tests/services/test_model_capability.py -q
uv run ruff check backend tests
git diff --check
```

- [ ] **Step 3: Run a repository-wide live-reference scan**

```powershell
rg -n 'RcaEvalPartition\.SS30|partition[= ]+"ss30"|choices=.*ss30|FORMAL_CASE_COUNTS\["ss30"\]|materialize_ss30' backend tests
```

Expected: no output. Any remaining `SS30` text must be an explicitly dated
historical record or the preserved old external artifact path.

- [ ] **Step 4: Report the new identity and stop before API consumption**

Record the new runtime/labels manifest hashes, source count, SS15/TT90 counts,
PowerShell parse result, test totals, Ruff result, and the fact that no formal API
prediction or labels-open operation was started. A new endpoint capability artifact
must be recertified against the post-change clean HEAD before any SS15 model run.
