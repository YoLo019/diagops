---
name: grilling
description: Resolve the decision tree for a high-risk DiagOps change. Use when diagops-release needs requirements, scope, risk, or acceptance decisions aligned with the user.
---

# Grilling

Build a decision tree and work it breadth-first. The frontier is every decision
whose prerequisites are settled and can be answered now.

For each round:

1. Verify every discoverable fact from the repository, environment, or primary
   source. Ask the user for decisions, not facts the agent can find.
2. Ask the whole current frontier as numbered questions. Give a recommended
   answer and concrete tradeoff for each.
3. Wait for the user's answers, update the tree, and recompute the frontier.

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
during the same round. The session finishes only when the frontier is empty,
external blockers have owners, and the user confirms the shared understanding.
Do not begin solution execution during grilling.
