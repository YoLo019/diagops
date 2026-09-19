# Implementation

Work one unblocked tracer-bullet slice at a time.

1. Use the current slice, relevant requirement sections, affected code, and
   existing tests at the agreed seam. Re-read when context is missing or changed;
   do not preload unrelated plan or spec sections.
2. Confirm the baseline still matches the plan. Stop if concurrent changes
   invalidate assumptions or ownership.
3. Validate observable behavior at the agreed public seam. Reuse existing tests;
   add or adjust regression coverage when needed. For a reproducible bug, observe
   the relevant failure before fixing it when practical. Follow any red-green
   requirement in the approved plan, but do not invent a fixed test count or
   mirror the implementation just to produce a test.
4. Keep the slice vertical and use feedback to choose the next small step.
5. Run the smallest relevant static, test, or build check while working. Run the
   slice's focused check before marking it complete. Inspect test fixtures and
   configuration when side effects are uncertain; this workflow does not imply
   production isolation or authorize paid model calls.
6. Update only the slice status and concise evidence pointer. Keep raw logs and
   repeated command output out of the plan and `current.md`.

Use existing dependencies and public seams. Add a new abstraction or dependency
only when the approved contract requires it and existing code cannot carry the
behavior cleanly.

If implementation exposes a contract, safety, migration, or acceptance change,
stop and return to the earliest affected release phase. A local implementation
correction inside the approved slice stays in this phase.

Do not commit automatically. Continue with the next frontier slice only after
the current slice is coherent and its focused check is fresh.
