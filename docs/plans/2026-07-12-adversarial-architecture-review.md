# Adversarial Architecture Review Plan

## Objective

Produce a current, evidence-backed technical-lead review of `decoy-engine` that:

- explains what the engine does and how its critical paths fit together;
- identifies defects, blockers, unsafe assumptions, and measurable inefficiencies;
- ranks findings from most severe to least severe;
- gives a concrete remediation method, verification gate, and source for every finding;
- preserves independent reviewer notes and a directory into the supporting evidence.

This is a report-only review. Production code and behavior are out of scope unless the
user explicitly expands the task.

## Evidence Standard

1. Treat existing audits and plans as hypotheses, not proof.
2. Confirm material claims against the current source tree and, where practical, a
   focused runtime test or static-analysis result.
3. Cite repository paths and line numbers. Cite external primary sources only when a
   standard or established methodology is part of the recommendation.
4. Separate verified defects, design risks, documentation drift, and unverified gaps.
5. State coverage limits and do not infer production behavior from mocked tests alone.

## Work Phases

1. Establish the intended architecture, public contract, prior findings, and current
   working-tree state.
2. Independently inspect execution/data integrity, security/privacy, and
   test/performance/operations concerns.
3. Reproduce or falsify the highest-severity candidates with targeted checks.
4. Synthesize one severity-ranked report with an architecture directory, remediation
   roadmap, source index, and raw reviewer-note appendix.
5. Have a fresh-context reviewer challenge evidence, severity, completeness, and
   remediation feasibility; revise before delivery.

## Guardrails

- Do not contact external data services or mutate remote state without explicit
  authorization.
- Do not expose credentials, raw PII, or local secret values in notes or command output.
- Do not alter or remove unrelated working-tree changes.
- Prefer the repository's existing test and lint commands; record any check that cannot
  run and why.

## Open Questions

- Whether configured external integrations may be exercised.
- Whether known production incidents or undocumented requirements should be included.

## Deliverables

- `docs/adversarial-architecture-review-2026-07-12.md`
- `docs/review-notes/2026-07-12/` containing scoped reviewer notes and evidence pointers
  where separate notes improve traceability.
