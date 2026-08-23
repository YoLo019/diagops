# Implementation

Work one unblocked tracer-bullet slice at a time.

1. Re-read the slice, its requirement sections, affected code, and existing
   tests at the agreed seam. Do not preload unrelated plan or spec sections.
2. Confirm the baseline still matches the plan. Stop if concurrent changes
   invalidate assumptions or ownership.
3. For non-trivial behavior, run a red-green loop at the agreed public seam:
   write one failing behavior test, observe the expected failure, implement the
   minimum behavior, then observe it pass.
4. Keep the slice vertical. Let each test teach the next small step instead of
   writing all tests or all layers in advance.
5. Run the smallest relevant static, test, or build check while working. Run the
   slice's focused check before marking it complete.
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
