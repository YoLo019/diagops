# Current DiagOps Iteration

This is a routing summary, not a requirements or evidence archive. Read only
the linked document sections relevant to the current task. The condensed
version evolution and engineering lessons are in
`workflow-history-through-2026-08-22.md`.

Updated: 2026-08-22

## Status

| Field | Value |
| --- | --- |
| Version | V11 |
| Goal | Make adaptive, evidence-seeking Multi-Agent reasoning the diagnostic authority while preserving read-only safety, durable execution, historical records, and benchmark-neutral provider contracts. |
| Iteration | `blocked` |
| Spec | `approved` |
| Plan | `approved` |
| Implementation | `blocked` at M5 T12 |
| Completion commit | `none` |

Blocker: generate a passed capability artifact from a clean HEAD under the SS15
protocol. The formal run also requires endpoint/model/process-only credential
preflight, explicit budget authorization, and the pending review decision.

Next action: complete the T12 capability admission, then run the authorized SS15
and TT90 gates. Do not claim completion or accuracy improvement before those
gates pass.

## Active Documents

- V11 design: `specs/2026-08-02-diagops-v11-adaptive-multi-agent-rca-design.md`
- V11 plan: `plans/2026-08-02-diagops-v11-adaptive-multi-agent-rca-implementation-plan.md`
- SS15 amendment: `specs/2026-08-19-rcaeval-ss15-protocol-design.md`
- SS15 plan: `plans/2026-08-19-rcaeval-ss15-protocol-implementation-plan.md`
- Version evolution and engineering lessons:
  `workflow-history-through-2026-08-22.md`

## Latest Verified Baseline

- M4 baseline: `7539c7fd8b707fc54cf2ed75a7d9fcba13d7618c`.
- M5 engineering and the SS15 protocol amendment are merged to `main`.
- Formal SS15 and TT90 evaluation has not completed; no accuracy or release
  claim is authorized.

Independent fixes, docs, tests, and reversible local behavior changes do not
update this file unless they are part of the active release.
