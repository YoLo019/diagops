# Specification

Create or amend a spec only when the confirmed release contract is not already
covered by an approved document. Store it under `docs/superpowers/specs/`.

## Inputs

- Confirmed decision tree from alignment.
- Current code and contract evidence.
- Applicable `CONTEXT.md` vocabulary and ADRs.
- Existing spec sections that remain authoritative.

## Required Content

1. Problem and desired observable outcome.
2. Scope and explicit non-goals.
3. User-visible and operator-visible behavior.
4. Affected domain, API, configuration, persistence, and provider contracts.
5. Evidence, trust, read-only, and sensitive-data boundaries.
6. Failure, partial-result, timeout, cancellation, retry, replay, and cleanup
   behavior where applicable.
7. Historical-read compatibility and approved migration or rollout behavior.
8. Test seams and measurable release gates.
9. Open external blockers and their owners.

Give each testable behavior, retained contract, safety boundary, and release
gate a stable requirement ID. Map every retained ID to at least one observable
acceptance check; do not create a second coverage matrix.

Prefer behavior and interfaces over file paths or implementation snippets. Use
a snippet only when it records a decision more precisely than prose, such as a
state transition or schema shape.

Self-review the spec for contradictions, unverified assumptions, missing
failure behavior, stale compatibility claims, and scope creep. Present the
written document for user approval. Implementation planning starts only after
the current content is approved.
