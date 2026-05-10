# ADR-0002 — Two key resolvers (`derive_key` and `pipeline_derive_key`) in `ExecutionContext`

> **Status:** Accepted
> **Date:** 2026-05-06

## Context

Deterministic transforms (`hash`, `fpe`, keyed `faker`) need access to keyed material to produce stable outputs across runs. Generation also needs keyed material when a customer wants reproducible synthetic data.

Mask and generate have *opposite* scope requirements:

- **Mask** outputs must be the same across pipelines that touch the same column. If pipeline A masks `customers.id` and pipeline B masks `orders.customer_id`, the FK join across the two outputs must survive — that's the whole reason keyed determinism exists. Mask keys must be scoped to *master only* (instance-wide), not to a pipeline label.
- **Generate** outputs should *differ* by pipeline. If pipeline A generates a synthetic `customers` table and pipeline B generates a different synthetic `customers` table for a different test scenario, the two should not collide. Generate keys must be scoped to *master + pipeline label*.

A naive single-resolver design with a "scope" parameter at the call site would conflate the two and is too easy to mix up — a mask op accidentally getting pipeline-scoped keys silently breaks cross-pipeline FK joins, which is exactly the failure mode this whole subsystem exists to prevent.

## Decision

Expose **two distinct resolvers** on `ExecutionContext`, each pre-bound to the right scope by the caller:

- **`derive_key(info: str) -> bytes`** — the **mask** resolver. Caller pre-binds the tenant master only. Same input row + same column always maps to the same masked bytes across every pipeline in the instance.
- **`pipeline_derive_key(info: str) -> bytes`** — the **generate** resolver. Caller pre-binds master + pipeline label. Outputs differ between pipelines.

Engine ops pick the right resolver for their semantics: mask ops use `derive_key`, generate ops use `pipeline_derive_key`. The choice is made *inside the op*, not at the call site.

Both resolvers are built using the stdlib HKDF-SHA256 helper (`_hkdf_sha256`) shipped in `context.py`. CLI uses `make_key_resolver`; platform's `api/keys/make_resolver` is structurally identical and produces the same bytes given the same inputs (cross-instance recovery property).

## Consequences

**Negative:**
- Two concepts to teach instead of one. Contributors writing a new op must consciously pick the right resolver; the inline comment block in `context.py` and this ADR are the documentation.
- `pipeline_derive_key` falls back to seed-based RNG (random across runs) when `None`. The fallback is intentional — admin policy decides whether a given pipeline gets keyed-deterministic or fresh generation — but it means generate is *not* deterministic by default, which surprises some readers.
- Both resolvers are caller-supplied closures, which means the engine cannot introspect the master key for logging or telemetry. Intentional (the engine should never see the master), but it forces audit-log enrichment to happen in the caller.

**Positive:**
- The "wrong scope" failure mode is structurally impossible. Mask ops literally cannot reach the pipeline-scoped resolver, and vice versa.
- HKDF chain is explicit and stdlib-only — no `cryptography` dependency in the engine, which keeps the engine's supply-chain surface small.
- CLI and platform implementations are byte-identical given the same master + pipeline label, which is what makes air-gapped instance recovery work — a customer can rebuild masked outputs from raw inputs on a different physical machine if they retain the master key.

## Alternatives considered

- **Single resolver with a `scope` parameter.** Rejected: too easy to pass the wrong scope at the call site, and the failure mode (silent FK breakage) is exactly what the subsystem is designed to prevent.
- **Per-callsite key derivation (no resolver, master passed directly).** Rejected: scattered crypto, and it forces every caller to know the HKDF chain. The point of the resolver is to centralize key derivation in the platform/CLI layer that knows the master.
- **A single resolver, with mask refusing to honor a non-`None` pipeline label.** Rejected: hides intent (the resolver looks symmetric but isn't), and a caller could still misconfigure by passing a non-`None` label and expecting mask to honor it.

## References

- `decoy-engine/src/decoy_engine/context.py` — Protocol and `ExecutionContext`, plus `make_key_resolver` helper and the inline rationale comment block in the constructor.
- `decoy-engine/SHARED_ENGINE_ARCHITECTURE.md` — keyed-determinism story.
- `decoy/plans/2026-05-10-distribution-integrity-in-masking.md` — the four-invariant framing that calls out keyed determinism as serving invariant 3 (referential integrity) but **not** invariants 1, 2, or 4.
- `forge-platform/ROADMAP.md` Item 6 (`ExecutionContext` through `DataGenerator`, shipped 2026-05-10).
