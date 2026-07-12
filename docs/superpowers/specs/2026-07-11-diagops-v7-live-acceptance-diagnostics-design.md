# DiagOps V7 Live Acceptance Diagnostics Design

## Goal

Make a failed live reliability gate diagnosable before changing prompts or
hybrid arbitration. Add safe structured details to the acceptance artifact and
a five-run diagnostic cohort mode.

## Confirmed Problem

The first `gpt-5.6-sol` gate completed 15/15 investigations with valid
references and no safety failures, but 12/15 runs became deterministic
fallback. The artifact omits specialist findings and candidate support, so it
cannot distinguish model error from an intentionally conservative arbitration
decision.

## Design

Extend each acceptance result with accepted, persisted V7 data only:

1. Run status and safely classified fallback reason.
2. Specialist findings: agent name, finding type, related cause, confidence,
   evidence IDs, analysis round, and revision link.
3. Coordination candidates: cause, rank, confidence, supporting and
   contradicting finding/evidence IDs.
4. A derived per-candidate count of unique supporting and contradicting agents.

Do not store finding summary, rationale, gaps, review summary, uncertainty,
prompts, raw responses, reasoning content, exception text, provider Base URL,
or credentials. These fields are unnecessary for deciding whether fallback was
caused by insufficient Agent support, disagreement, or missing root-cause
findings.

Add a CLI option:

```text
--runs-per-case 1|3
```

The default remains `3`, preserving the canonical 15-run reliability gate.
`--runs-per-case 1` runs exactly one clean investigation for each of the five
golden cases. It is diagnostic only: it writes an artifact with
`mode=diagnostic`, reports metrics, and never claims the reliability gate
passed. Adversarial probes and threshold evaluation remain exclusive to the
canonical 15-run mode.

## Decision Gate After Five Runs

Classify each failed case using structured fields:

1. Expected cause is present but supported by only one Agent: arbitration
   mismatch; propose the smallest deterministic rule change for user approval.
2. Expected cause is absent and specialists return only signal/gap: specialist
   output contract or prompt issue.
3. Multiple supported causes remain: Coordinator review/input issue; preserve
   conflict rather than force agreement.
4. Specialist cause is wrong despite valid evidence: model/prompt mapping issue.

No RCA rule or prompt changes are authorized by this spec. The five-run artifact
must be reviewed first, and any behavioral fix gets a focused spec amendment or
explicit user approval.

## Compatibility And Safety

1. Artifact schema stays JSON-compatible and increments from version 1 to 2.
2. Existing version-1 artifacts remain readable by humans; no migration is
   required because artifacts are append-only evaluation output.
3. The canonical five-cases-times-three cohort and all eight reliability
   thresholds remain unchanged.
4. Raw runtime metrics remain separate from orchestrator-accepted persisted
   findings and review.
5. Production remains read-only; no mutation tool or executed-action wording is
   added.

## Tests

Automated tests must prove:

1. Diagnostic projection contains only the approved structured fields.
2. Unknown evidence/finding references cannot enter the projection.
3. Agent support counts are deduplicated by agent name.
4. Default CLI behavior remains 15 canonical runs and unchanged thresholds.
5. Diagnostic mode produces five clean runs, no probes, `mode=diagnostic`, and
   cannot report gate success.
6. Artifact JSON contains no secret test value, Base URL, prompt, raw response,
   reasoning, summary, rationale, gaps, or uncertainty.
7. Existing acceptance, runtime, report, and golden tests remain green.

## Ordered Follow-Up

1. Implement and automatically verify this diagnostic artifact.
2. Ask the user to run the five-run `gpt-5.6-sol` diagnostic locally.
3. Read the artifact and classify the root cause using the decision gate.
4. Implement only the evidence-supported fix and run automated verification.
5. Re-run the canonical OpenAI 15-run gate.
6. After OpenAI passes, execute the already-approved DeepSeek provider plan and
   run the same canonical gate for `deepseek-v4-pro`.

## Non-Goals

- Persisting prompts, raw model responses, reasoning, or free-text findings.
- Weakening thresholds or the two-Agent agreement rule before evidence review.
- A generic experiment framework, dashboard, database table, or new API/UI.
- Automatically launching paid runs or reading provider keys from files.
