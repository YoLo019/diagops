# Review And Finish

## Pin The Review

Resolve the recorded baseline and inspect the complete release diff and commit
list. Fail early when the baseline is missing or the expected diff is empty.

## Two-Axis Review

Run two separate passes so one concern cannot hide the other. Use independent
reviewers only when the user explicitly requests them.

### Standards

Check the diff against `AGENTS.md`, relevant ADRs, established local patterns,
and affected invariants. Report correctness, safety, evidence integrity,
contract, lifecycle, and unnecessary-complexity findings with file and line
references. Skip style rules already enforced by tooling.

### Spec

Check every retained requirement and acceptance criterion against the diff and
fresh evidence. Report missing or partial behavior, incorrect implementation,
scope creep, and unsupported claims. Cite the requirement for each finding.

Keep the two reports distinct, but rank findings by severity within each pass.
Verify each finding against the repository before changing code. Fix valid
findings at their root cause, rerun the smallest affected check, and repeat only
the affected review scope unless a shared contract changed.

## Final Verification

Run every applicable focused check and release gate from the spec and
`AGENTS.md` against the final reviewed state. A successful check already run on
that same relevant state is current evidence; rerun after changes that could
invalidate it, or when the approved gate requires a new run. Never substitute
local checks for formal capability, SS15, or TT90 evidence. External runs still
require their protocol, credential handling, and budget authorization.

A requirement is complete only with current evidence; otherwise record an
external blocker or remove it through an approved spec change. Do not claim
release completion while required gates remain blocked.

Update `docs/superpowers/current.md` with only:

- phase and status;
- blocker and next action;
- active spec and plan links;
- verified baseline or completion commit;
- a short verification summary with evidence pointers.

Keep detailed evidence in the authoritative spec, plan, or artifact. Report
skipped gates and residual risk. Commit, push, PR, merge, branch deletion, and
worktree removal require explicit user instruction.
