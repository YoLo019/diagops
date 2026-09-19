---
name: grilling
description: Resolve user decisions blocking a DiagOps release's scope, risk, or acceptance criteria.
---

# Grilling

Resolve the material decisions needed for the requested release or alignment
session. The frontier contains unresolved decisions whose prerequisites are
settled; exclude speculative choices and decisions already approved.

For each round:

1. Check relevant repository facts before asking. Separate verified facts,
   assumptions, and user choices; expand research only when it affects a decision.
2. Ask related, answerable questions together, with a recommendation and concrete
   tradeoff. Prioritize blocking choices; do not exhaust an entire hypothetical
   decision tree or ask the user to rediscover repository facts.
3. Incorporate answers and identify any remaining material blockers. Reuse
   explicit decisions instead of asking for the same approval again.

Add only applicable branches. For DiagOps releases, consider:

- problem, actor, desired outcome, scope, and non-goals;
- current and target behavior;
- public, provider, configuration, and persisted contracts;
- production read-only, evidence, benchmark-neutrality, and sensitive-data
  boundaries;
- failure, partial result, cancellation, timeout, retry, replay, and cleanup;
- concurrency, migration, rollout, budget, and external authorization;
- representative normal and failure cases with measurable release gates.

When a term or decision needs durable domain context, use `domain-modeling`
only for that unresolved context. Finish with the agreed scope, acceptance
criteria, remaining external blockers and their owners, and the user's
confirmation of the shared understanding. Do not treat a recorded blocker as
resolved. Alignment alone does not authorize implementation; return to the
invoking workflow after its alignment gate is satisfied.
