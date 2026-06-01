---
Archived: 2026-06-01
Source: branches `origin/qa/review-2026-06-01-*` extracted + their branches deleted.
Status: every finding shipped to engine main. Documents here are read-only history.
---

# QA audit archive: 2026-06-01

Eight audit reviews ran against the engine over 2026-05-31 to 2026-06-01.
Each was a single-doc branch on `origin/qa/review-*`; the branches were
extracted into this directory and the remote branches deleted to keep
the branch list clean.

Every finding was processed during the autonomous /loop session of
2026-05-31 to 2026-06-01. Disposition tracked in
`decoy-platform/docs/audit/dennis-*-gate-*` review docs (one per
partial sprint).

## Index

| File | Scope | Findings (closed in /loop session) |
|---|---|---|
| `qa-review-2026-06-01-engine.md` | engine-wide MG-1 through MG-6 review | MG sprints all shipped pre-loop |
| `qa-review-2026-06-01-mg2-mg3-mg4.md` | MG-2 / MG-3 / MG-4 specific | MG sprints all shipped pre-loop |
| `qa-review-2026-06-01-storm-hardening.md` | storm post-mask hardening | QA-4 shipped |
| `qa-review-2026-06-01-relationships-context.md` | relationships + context | QA-8 shipped |
| `review-2026-06-01-connectors-generation-profile.md` | connectors + synthesize + profile_source | QA-7 + followups shipped |
| `review-2026-06-01-execution-quality-determinism.md` | execution + quality + determinism | QA-10 partials shipped (8 of 14 findings closed; 6 deferred to qa-10-quality-report-hardening.md) |
| `review-2026-06-01-walks-generators.md` | walks/ + generators/ | walks-gen followup spec; partials 1+2+5 shipped; 3+4 PO-gated then shipped |
| `review-2026-06-01-internal-synth-providers.md` | internal + transforms + disguises + synth + custom_faker_providers | qa-internal-synth-providers 5 partials shipped (all 13 findings closed) |

## Cross-references

- Sprint specs that drove the partials live under
  `decoy-platform/docs/v2/sprints/qa-carries/`.
- Per-partial Dennis gate findings live under
  `decoy-platform/docs/audit/dennis-*-gate-2026-06-01.md`.
- Session-by-session narrative lives in
  `decoy-platform/docs/overnight-dev/log.md` (main session) +
  `decoy-platform/docs/overnight-dennis/log.md` (Dennis sessions).
