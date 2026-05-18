# Claude Guide

Engine Claude guidance moved to [../decoy-platform/docs/guides/engine-claude-guide.md](../decoy-platform/docs/guides/engine-claude-guide.md).

Use [../decoy-platform/docs/README.md](../decoy-platform/docs/README.md) as the documentation entrypoint.

## Core rule for non-trivial engine work

**Use established methodology.** For crypto, FK preservation, synth strategies, statistical methods, hash-for-joinability, and other non-trivial primitives: survey how established tools / standards approach the problem before designing, and cite the source pattern in the implementing module's docstring. We use HKDF-SHA256, Faker, pyarrow, Polars, pandas, and SDV's HMA1 pattern; we do not roll our own. See [../decoy-platform/docs/guides/engine-claude-guide.md#use-established-methodology](../decoy-platform/docs/guides/engine-claude-guide.md#use-established-methodology) for the full rule, examples, and citation conventions.
