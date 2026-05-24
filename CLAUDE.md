# Claude Guide

Engine Claude guidance moved to [../decoy-platform/docs/guides/engine-claude-guide.md](../decoy-platform/docs/guides/engine-claude-guide.md).

Use [../decoy-platform/docs/README.md](../decoy-platform/docs/README.md) as the documentation entrypoint.

## Core rule for non-trivial engine work

**Use established methodology.** For crypto, FK preservation, synth strategies, statistical methods, hash-for-joinability, and other non-trivial primitives: survey how established tools / standards approach the problem before designing, and cite the source pattern in the implementing module's docstring. We use HKDF-SHA256, Faker, pyarrow, Polars, pandas, and SDV's HMA1 pattern; we do not roll our own. See [../decoy-platform/docs/guides/engine-claude-guide.md#use-established-methodology](../decoy-platform/docs/guides/engine-claude-guide.md#use-established-methodology) for the full rule, examples, and citation conventions.

## Engineering best practices

Before writing or reviewing non-trivial engine code, consult [../decoy-platform/docs/guides/engineering-best-practices.md](../decoy-platform/docs/guides/engineering-best-practices.md). Rules cover refactor safety, validation contracts, boundaries, module sizing, tooling, citations, performance discipline, pre-GA vs post-GA, decisions/RFCs, and comments/names. Each rule has a why, a how-to-apply, and an anti-rule exception. Cite sections by number ("per best-practices §4.1") in code reviews.

Engine-specific rules to watch in V2 sprints (all sourced from the document above):
- §1.1 Snapshot before extraction (V2.0-A snapshot harness mandatory)
- §2.1-2.4 Validation never mutates; mutation has a name; frozen reports; land the assertion test first
- §3.1 `internal/` means internal (import-linter enforced from V2.0-C)
- §3.3 Library code doesn't know its callers (CLI/platform helpers live in their own repos)
- §4.1 Orchestration modules cap at 600 LOC (`graph/runner.py` threshold)
- §6.2 Use established methodology (the rule already cited above)
- §8.1 Pre-GA = hard delete (V2.1 framing)
