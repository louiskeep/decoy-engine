# Parity E3: engine surface hygiene (vestigial knobs, dead config, stale maps)

Status: plan, PRE-RELEASE (2026-10-24). Part of the parity program; source findings: parity audit §3.3,
§4 (`run_storm: true` inert, `substrate` vestigial), §9 rows 9, 10, 12, and the engine inventory's
"dead / legacy still shipping" list.

Rules consulted: 00-universal, development-loop, risk-and-exceptions, api-and-compatibility,
scope-discipline, documentation, testing; engine `CLAUDE.md` (pre-GA = hard delete; `RELEASE_PHASE`).

Risk: R2 (removes public kwargs and a config key while pre-GA; compat contract is non-binding but both
consumers must move in lockstep).

## 1. Guarantee and non-goals

Guarantee: every public `run_pipeline` kwarg does something; every accepted config key is honoured or
rejected at validate time; the engine's own maps and docs describe the code that exists. No behaviour
change for a caller that passes none of the removed knobs.

Non-goals: new capability; renaming routes; the Rust coverage itself (A-9 and V2 Tier 4 own that).

## 2. Items

| # | Item | Action | Consumers to update |
|---|---|---|---|
| 1 | `substrate` kwarg, `DECOY_SUBSTRATE`, `resolve_substrate`, `VALID_SUBSTRATES=("pandas",)` | hard delete the knob; `select_execution_adapter(plan)` keeps one adapter | CLI `--substrate` flag and `decoy.mask(substrate=)` (plan C1); platform passes none |
| 2 | `max_workers`, `fallback_to_pandas` ("reserved no-op") | delete | none pass them |
| 3 | `fpe_chunk_count` | keep only if a benchmark shows effect on the pandas adapter; else delete | none |
| 4 | `run_storm: true` on `PipelineConfig` (`config/_pipeline.py:81`) accepted but inert in `execution/` | either wire to `storm.postmask.run_storm_post_mask` after finalize (emitting findings into `quality_metrics["storm_postmask"]`) or reject at validate with a coded message pointing to the post-hoc check. Decision: wire it; the engine already owns the postmask code and both consumers want in-run integrity. | CLI surfaces in `--post-validate`; platform replaces its external post-mask hook |
| 5 | `run_mask_pipeline_chunked` top-level export (legacy pandas chunked lane) | keep as the `chunked_dispatcher_enabled=False` internal lane; drop from `__all__` once CLI C3 lands | CLI (C3) |
| 6 | `SchemaInspector` (`NotImplementedError`), `LicenseVerifier` (always free) | delete both exports; no consumer uses them | none |
| 7 | `walks/` (no run-path consumer), `forecast/` references | confirm platform's only use, then delete or move to platform | platform grep first |
| 8 | `__version__`/`pyproject` 0.7.0 vs 0.14.1 tags | align `main` to the released line or document the release-branch convention in `CONTRIBUTING.md` and make `__version__` derive from the tag at build | CLI pin (C1) |
| 9 | `src/decoy_engine/_MAP.md`, CODEMAP "four native scalar strategies", `transforms/__init__.py` "slim 3-file", `generators/__init__.py` graph refs, `docs/native/supported-matrix.md` abi-2, `execution-backends.md` "CLI bundle ships the companion" | rewrite to current facts; add a sentry that `_MAP.md` paths exist | none |

## 3. Slices and acceptance

1. **E3a knob deletion (1, 2, 3, 6)** with CHANGELOG "Removed"; `tests/sentry/test_public_import_boundary.py`
   and the API-reference generator updated; CLI C1 merges the same day it re-pins.
2. **E3b `run_storm: true` wiring (4)**: golden test with a seeded residual-PII fixture asserting findings
   appear in `quality_metrics` and the run still succeeds; validator-off path unchanged.
3. **E3c dead code + version (5, 7, 8)**.
4. **E3d maps and docs (9)** with the path-existence sentry.

## 4. Done when

No public kwarg is a no-op; no accepted config key is inert; sentry guards the maps; CLI and platform pins
updated; plan archived.
