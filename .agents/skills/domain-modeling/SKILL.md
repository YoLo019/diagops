---
name: domain-modeling
description: Sharpen DiagOps domain language and durable design decisions. Use when release discussions expose ambiguous terminology, code-to-language conflicts, or a hard-to-reverse architectural tradeoff.
---

# Domain Modeling

Read the applicable `CONTEXT.md` when it exists. Challenge ambiguous or
conflicting terms against the glossary and current code. Use concrete edge
scenarios to make boundaries precise.

When a project-specific term is resolved, update `CONTEXT.md` immediately using
[references/context-format.md](references/context-format.md). Create the file
only when there is a real term to record. Keep it a glossary, free of
implementation decisions and general programming vocabulary.

Offer an ADR only when the decision is hard to reverse, surprising without
context, and the result of a real tradeoff. When all three hold, use
[references/adr-format.md](references/adr-format.md). Otherwise keep the decision
in the active spec.

If user language and code disagree, surface the contradiction and resolve which
is authoritative before updating either. Do not turn every release choice into
a glossary entry or ADR.
