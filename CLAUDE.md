# Claude Guide

Engine-specific guidance for Claude and other coding agents working in this repo.

Use [CODEMAP.md](CODEMAP.md) for repo navigation before broad searches. Use [CONTRIBUTING.md](CONTRIBUTING.md) for the contributor entrypoint.

**Documentation.** This repo owns the engine-scoped durable docs ([`docs/index.md`](docs/index.md)): reference, `docs/security/` + `docs/decisions/` records, and the API reference. Engine implementation plans live in `docs/plans/` (one per active roadmap item). The single cross-repo roadmap is `decoy-platform/docs/ROADMAP.md`, which indexes engine plans too; do NOT start a separate engine roadmap, next-up, or todo doc. Follow the plan → build → promote-to-reference + archive lifecycle in `/home/cam/dev-rules/documentation.md`.

## Core rule for non-trivial engine work

**Use established methodology.** For crypto, FK preservation, synth strategies, statistical methods, hash-for-joinability, and other non-trivial primitives: survey how established tools or standards approach the problem before designing, and cite the source pattern in the implementing module's docstring. We use HKDF-SHA256, Faker, pyarrow, Polars, pandas, and SDV's HMA1 pattern; we do not roll our own.

## Engineering best practices

Engine-specific rules to watch in V2 sprints:

- Snapshot before extraction (V2.0-A snapshot harness mandatory).
- Validation never mutates; mutation has a name; reports are frozen; land the assertion test first.
- `internal/` means internal (regex sentry enforced: `tests/sentry/test_public_import_boundary.py`).
- Library code does not know its callers. CLI and platform helpers live in their own repos.
- Modules aim for 600 LOC with a hard max of 700. Over 600 needs a census entry at the exact LOC (`tests/sentry/test_module_size.py`); over-max legacy files may only shrink.
- Use established methodology (the rule above).
- Pre-GA = hard delete (V2.1 framing). The switch is `decoy_engine.RELEASE_PHASE` (`release.py`); `is_pre_ga()` is what the CI gates branch on. Flipping it to `"ga"` at launch makes the [compatibility contract](docs/compatibility-contract.md) binding.

## Comments

Explain why, not what. One line unless a real invariant needs more. No references to the current task, PR, or author.

---

Full engineering-best-practices and engine-claude-guide documents live in the commercial platform repo.
