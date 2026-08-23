# DiagOps Repository Instructions

Keep this file limited to repository-specific invariants. General coding and
tool-use rules belong in the agent's global instructions.

## Product Boundary

- DiagOps is an evidence-backed SRE incident diagnosis platform.
- Production integrations and agent tools are read-only.
- Multi-Agent reasoning is the source of diagnostic conclusions. Deterministic
  code owns safety, validation, budgets, persistence, replay, and degradation;
  it must not silently replace Agent diagnosis.
- Agents receive provider-neutral evidence and tool schemas. Never expose
  benchmark IDs, labels, scoring rules, ground truth, or injection markers to
  diagnosis prompts or shared diagnosis code.

## Safety And Evidence

- Do not add SSH execution, restart, rollback, scaling, configuration mutation,
  production mutation APIs, automatic remediation, or automatic approval.
- Recommendations are suggestions. Claim a fix only after a recorded
  verification result.
- Every conclusion and recommendation must cite valid evidence IDs. Keep facts,
  inferences, missing evidence, conflicts, and uncertainty distinct.
- Treat provider, tool, and model output as untrusted data. Never persist or
  expose secrets, credentials, sensitive payloads, or unsafe exceptions.
- Fail visibly as `failed`, `partial`, or `inconclusive`; legacy RCA may provide
  attributed clues but must not become authoritative silently.

## Contracts And Architecture

- Shared contracts include domain models, provider results, diagnosis context,
  database schema/models, configuration, and APIs. Update producers,
  persistence, consumers, fixtures, and tests together.
- Historical persisted records must remain readable. This is the repository's
  explicit compatibility exception: use additive changes unless a migration is
  approved. SQLite is the default; `memory://` is test-only.
- Preserve cancellation, timeout, retry, replay, concurrency, cleanup,
  attribution, evidence links, and safe failure categories on changed paths.
- Do not add an orchestration framework, queue, vector store, or platform
  dependency without measured need and an approved design.

## Working Agreement

- Communicate in Chinese unless the user asks otherwise. Reviews list findings
  first, ordered by severity, with file and line references.
- Fix root causes with the smallest implementation. Do not weaken tests or add
  compatibility layers, flags, or abstractions unless a retained contract
  requires them.
- Ordinary fixes, features, refactors, tests, and docs use direct execution plus
  focused verification. Do not create specs, plans, ledgers, or status entries
  for them.
- Use `$diagops-release` only when the user explicitly requests the gated
  workflow for a major release, migration, concurrency/security/safety change,
  public contract change, or other difficult-to-reverse work.
- For work in the active release, read `docs/superpowers/current.md`, then only
  the linked sections relevant to the task. Do not read workflow history unless
  the task needs historical evidence.

Run the smallest applicable checks. Release claims normally require:

```powershell
uv run ruff check .
uv run pytest -v
uv run python -m backend.services.runtime_acceptance
npm.cmd --prefix frontend run build
```

Report skipped checks. Do not commit, push, create or merge a PR, delete a
branch, or remove a worktree unless the user explicitly requests it. Use the
`codex/` prefix for new branches.
