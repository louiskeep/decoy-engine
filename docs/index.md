# decoy-engine Docs

In-repo documentation for `decoy-engine`. Start here.

## Top-level

- [README](../README.md): what the engine is, install, quickstart, public API.
- [CODEMAP](../CODEMAP.md): directory map and "Where Do I Find" pointers.
- [CLAUDE](../CLAUDE.md): agent best-practice notes.
- [AGENTS](../AGENTS.md): reading order for coding agents.
- [CONTRIBUTING](../CONTRIBUTING.md): build, test, and PR conventions.
- [SECURITY](../SECURITY.md): security posture and reporting channel.

## Security

- [Key derivation](security/key-derivation.md): HKDF-SHA256 master-key derivation contract.
- [SQL surfaces](security/sql-surfaces.md): parameter-binding posture across in-tree connectors.

## Parity

- [Pandas/Polars semantic differences](../tests/parity/SEMANTIC_DIFFERENCES.md): accepted-divergence catalog.

## Methodology

- [Methodology registry](methodology-registry.yaml): citations for non-trivial domain primitives (per the "use established methodology" rule in [CLAUDE.md](../CLAUDE.md)).

---

Architecture, roadmap, and audit documentation are maintained in the commercial platform repo.
