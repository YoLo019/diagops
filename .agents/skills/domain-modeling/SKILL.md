---
name: domain-modeling
description: Resolve ambiguous DiagOps terms, code-language conflicts, or hard-to-reverse design tradeoffs.
---

# Domain Modeling

Read the applicable `CONTEXT.md` when it exists. Challenge ambiguous or
conflicting terms against the glossary and current code. Use concrete edge
scenarios to make boundaries precise.

When a project-specific term is agreed and documentation changes are within
the user's request or authorized release workflow, update `CONTEXT.md` using
[references/context-format.md](references/context-format.md). Create the file
only when there is a real term to record. Keep it a glossary, free of
implementation decisions and general programming vocabulary. For explanation
or review-only requests, propose the wording without writing files.

Offer an ADR only when the decision is hard to reverse, surprising without
context, and the result of a real tradeoff. When all three hold, use
[references/adr-format.md](references/adr-format.md) after the decision is agreed
and recording it is authorized. Otherwise use the active spec if there is one;
do not create a spec merely to store a discussion.

If user language and code disagree, surface the contradiction and resolve which
is authoritative before updating either. Do not turn every release choice into
a glossary entry or ADR.

Finish when the relevant ambiguity is resolved or a concrete user decision is
identified, and authorized records agree. Do not extend the task into a general
domain or architecture audit.
