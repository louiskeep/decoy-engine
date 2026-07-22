# Archive (Tier 4)

Point-in-time engine documents kept for history, not maintained. These
described work that has since shipped or reviews/discussions that have been
superseded by durable records elsewhere. They are excluded from the published
docs site (`conf.py`) and are not part of any toctree.

Where the durable decision from one of these lives on:

- `discussions/` (DE-01/02/03/11 crypto-GA design notes) — the durable
  decisions were promoted to `../security/de-01-fpe-remediation-design.md` and
  `../security/de-02-keyprovider-design.md`, and the shipped state is recorded
  in `../compatibility-contract.md`.
- `review-notes/`, `adversarial-architecture-review-2026-07-12.md`,
  `engine-consultant-findings-2026-07-09.md` — review snapshots; actionable
  findings that remained open were folded into `../remediation-source.md`.
- `job-performance-sprints.md`, `relationships-out-of-core-sprints.md`,
  `relationships-memory-scaling.md` — sprint/design plans whose work has
  largely shipped; current engine plans live in `../plans/`.

Nothing here should be cited as current behavior. Check the reference docs,
`../decisions/`, or `../remediation-source.md` instead.
