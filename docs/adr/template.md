# ADR-NNNN — &lt;short imperative title&gt;

> **Status:** Proposed | Accepted | Deprecated | Superseded by ADR-NNNN
> **Date:** YYYY-MM-DD

## Context

What forces are at play. What constraints we're under. The shape of the problem before the decision was made. State the problem in terms a reader who hasn't seen the codebase can follow.

## Decision

What we decided. One paragraph; declarative. No hedging — if you find yourself writing "we will probably" or "we might," the decision isn't ready for an ADR; write a plan or RFC instead.

## Consequences

What this enables, what it costs, what new constraints it imposes on future work. Both positive and negative. Lead with the negative — those are what future readers most need to know.

## Alternatives considered

The options we looked at and rejected, with a one-line rejection reason each. Three to five alternatives is the sweet spot; more becomes noise, fewer suggests the decision wasn't really considered.

## References

Code paths, plans, guides, external links the decision depends on. Use repo-relative paths so links survive directory renames.

---

## How to use this template

1. Copy this file to `docs/adr/NNNN-kebab-case-title.md` with the next sequential number.
2. Fill in every section. If a section doesn't apply, write "n/a" — don't delete the heading.
3. Commit. ADRs are **immutable** once landed; if you need to reverse a decision, write a new ADR with `Status: Supersedes ADR-NNNN` and flip the old ADR's status to `Superseded by ADR-MMMM`. Never edit the body of a landed ADR.
4. Cross-reference from the relevant guide / `architecture.md` so readers find the rationale from the spec.

ADRs are for decisions where (a) multiple plausible options existed, (b) the choice locks in a constraint others must work around, and (c) a new contributor would otherwise re-ask "why didn't we just X?" If none of those apply, the decision doesn't need an ADR — it's either too small (commit message) or too contested (write an RFC first).
