# CONTEXT.md Format

Use one root `CONTEXT.md` unless a future `CONTEXT-MAP.md` explicitly defines
multiple bounded contexts.

```markdown
# DiagOps Domain Language

## Language

**Canonical term**:
One or two sentences defining what the project-specific concept is.
_Avoid_: ambiguous synonym, overloaded synonym
```

Pick one canonical term, keep definitions tight, and list misleading synonyms
under `_Avoid_`. Include only DiagOps-specific concepts, not general programming
terms or implementation details.
