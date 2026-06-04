# decoy-engine Docs

In-repo documentation for `decoy-engine`. Start here.

## Top-level

These guides live at the repo root, outside the Sphinx source tree, so they
link to GitHub rather than into the rendered API reference.

- [README](https://github.com/louiskeep/decoy-engine/blob/main/README.md): what the engine is, install, quickstart, public API.
- [CODEMAP](https://github.com/louiskeep/decoy-engine/blob/main/CODEMAP.md): directory map and "Where Do I Find" pointers.
- [CLAUDE](https://github.com/louiskeep/decoy-engine/blob/main/CLAUDE.md): agent best-practice notes.
- [AGENTS](https://github.com/louiskeep/decoy-engine/blob/main/AGENTS.md): reading order for coding agents.
- [CONTRIBUTING](https://github.com/louiskeep/decoy-engine/blob/main/CONTRIBUTING.md): build, test, and PR conventions.
- [SECURITY](https://github.com/louiskeep/decoy-engine/blob/main/SECURITY.md): security posture and reporting channel.

## Security

- [Key derivation](security/key-derivation.md): HKDF-SHA256 master-key derivation contract.
- [SQL surfaces](security/sql-surfaces.md): parameter-binding posture across in-tree connectors.

## Parity

- [Pandas/Polars semantic differences](https://github.com/louiskeep/decoy-engine/blob/main/tests/parity/SEMANTIC_DIFFERENCES.md): accepted-divergence catalog.

## Methodology

- [Methodology registry](methodology-registry.yaml): citations for non-trivial domain primitives (per the "use established methodology" rule in the [CLAUDE](https://github.com/louiskeep/decoy-engine/blob/main/CLAUDE.md) guide).

---

Architecture, roadmap, and audit documentation are maintained in the commercial platform repo.

```{toctree}
:hidden:
:glob:

API reference <api/index>
api/**
security/*
```
