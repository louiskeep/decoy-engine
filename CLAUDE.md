# Claude Guide

Engine-specific guidance for Claude and other coding agents working in this repo.

Use [CODEMAP.md](CODEMAP.md) for repo navigation before broad searches. Use [CONTRIBUTING.md](CONTRIBUTING.md) for the contributor entrypoint.

## Core rule for non-trivial engine work

**Use established methodology.** For crypto, FK preservation, synth strategies, statistical methods, hash-for-joinability, and other non-trivial primitives: survey how established tools or standards approach the problem before designing, and cite the source pattern in the implementing module's docstring. We use HKDF-SHA256, Faker, pyarrow, Polars, pandas, and SDV's HMA1 pattern; we do not roll our own.

## Engineering best practices

Engine-specific rules to watch in V2 sprints:

- Snapshot before extraction (V2.0-A snapshot harness mandatory).
- Validation never mutates; mutation has a name; reports are frozen; land the assertion test first.
- `internal/` means internal (import-linter enforced from V2.0-C).
- Library code does not know its callers. CLI and platform helpers live in their own repos.
- Orchestration modules cap at ~600 LOC (`graph/runner.py` threshold).
- Use established methodology (the rule above).
- Pre-GA = hard delete (V2.1 framing).

## Comments

Explain why, not what. One line unless a real invariant needs more. No references to the current task, PR, or author.

---

Full engineering-best-practices and engine-claude-guide documents live in the commercial platform repo.
