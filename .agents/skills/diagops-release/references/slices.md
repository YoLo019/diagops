# Tracer-Bullet Slices

Store the approved execution map in one plan under
`docs/superpowers/plans/`. It is a dependency map, not a transcript or a copy of
the spec.

## Slice Rules

- A slice delivers one narrow end-to-end behavior across every layer it needs.
- Its result is demonstrable or independently verifiable.
- It fits one fresh context window.
- It names only genuine blockers; a slice with none joins the frontier.
- It maps to requirement IDs and observable acceptance criteria.
- It identifies the highest useful public test seam and focused check.
- Necessary prefactoring is a separate first slice only when it makes the
  behavior change materially safer or simpler.

Do not split work horizontally into all models, then all persistence, then all
APIs, then all tests. Keep each slice coherent.

A retained public or persisted contract change may require a coordinated wide
change. Use the repository's approved additive or migration strategy and size
steps so the supported state remains valid at every promised checkpoint. Do
not add compatibility for contracts the spec explicitly removes.

## Plan Shape

```markdown
# <Release> Execution Map

Spec: <path>
Baseline: <commit or working-tree identity>

## Slices

### S1: <Outcome>

**Blocked by:** None
**Requirements:** R1, R3
**Status:** ready

**Delivers:** <observable end-to-end behavior>

**Acceptance:**
- [ ] <observable criterion>

**Test seam:** <public boundary>
**Focused check:** `<command>`
```

Present the proposed slices, blocking edges, and granularity to the user. Revise
until approved. Execution works the unblocked frontier in dependency order.
