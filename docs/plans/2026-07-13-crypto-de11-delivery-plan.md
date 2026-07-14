# Crypto + DE-11 GA-hardening — delivery plan

Sequenced delivery for the GA-blocker findings (DE-01, DE-02, DE-03) + DE-11, after Fable review and Cam's decisions (2026-07-13). All scope grounded in Fable's verified file:line findings (`docs/discussions/2026-07-13-crypto-ga-blockers.md`, `2026-07-13-de11-pool-size-precedence.md`, `2026-07-13-de01-fpe-options.md`).

## Decision ledger (locked)

| Item | Decision |
|---|---|
| DE-01 sequencing | **D-now / C-fast-follow.** Fix silent-failure bugs + correct "NIST FF1" language now; audited FF1 + vault fallback is a later sprint (out of this delivery). |
| DE-01 un-encryptable values | **Disallow by default (fail closed).** Per-field `on_unencryptable: error \| vault_token` opt-in; **raw is not selectable.** |
| DE-01 separators | **Declared-separator allowlist** — fail closed on out-of-charset chars that aren't declared separators; healthy dashed-SSN path must keep working. |
| DE-01 sub-minimum domains | **Separate axis — accept with documented residual risk** for D-now (does not leak; no fix available pre-FF1). Not folded into the disallow. |
| DE-02 | **KeyProvider**: opaque ≥32-byte secret + HKDF purpose/version derivation; demote `job_seed` to generation-only. **Fail-closed key gate flips at GA** (`is_pre_ga()` → false), built now, default-off until then. Rekey scope = every `derive()` call-site (see below). |
| DE-03 | **Runtime output projection**: emit only columns with a work node or explicit passthrough declaration; hard-error on any other column present in the actual runtime frame. |
| DE-11 | **Resolve `pool_size` once at compile** → typed `ColumnSeed` field; fold in the `scale` sibling (same class); **reject-on-contradiction** when top-level and `provider_config` disagree. |

## Sprints (sequential — they share `plan/_seed_envelope.py`, so serialize to avoid collisions)

**Order rationale:** land the standalone correctness fix first (DE-11), then the two silent-disclosure fixes that are the real GA blockers (DE-03, DE-01), then the highest-blast-radius key-model change last (DE-02). Each is a worktree-isolated branch, dennis-gated (dennis-only this week; Codex at weekly limit), real-input fixtures, golden gate green, fail-pre-fix/pass-post-fix regression tests.

### Sprint 1 — DE-11 (pool_size + scale resolve-once)  [lowest risk, standalone]
- Add typed `pool_size` / `scale` fields to `ColumnSeed` (`plan/_types.py`); resolve once at compile and stamp them at the envelope drop site (`plan/_seed_envelope.py:203-207`); carry through `plan/_serialize.py` round-trip.
- Consumers read the typed field, not `provider_config`/raw dict: `execution/_strategies/_faker.py:49`, `execution/_chunked.py:152-158,451-458`.
- Reject-on-contradiction at compile when top-level and `provider_config` values are both set and differ (equal is legal).
- `scale` sibling: same carriage (compile-validated at `generation/pool/_validate.py:143-147` but currently dropped → runtime uses default 2.0).
- Acceptance: a job declaring top-level `pool_size: N` / `scale: N` uses N at runtime (regression proves default-10000/2.0 pre-fix); contradiction rejected; golden gate 53/53.

### Sprint 2 — DE-03 (undeclared-column runtime projection)
- Output projection at the adapter/sink keyed off the work-list/envelope: emit only columns with a work node or explicit passthrough declaration; **hard-error** on any other column present in the runtime frame.
- Fix the sibling silent drop: declared `faker` column with no `provider` (`plan/_seed_envelope.py:101-102`) currently drops silently → make it a compile error (or route through the projection error).
- Keep the compile-time check as an early warning, but the projection is the enforcement point (covers `no_profile` mode + stale-profile schema drift).
- Reconcile with the existing opt-in `storm/postmask` `residual_pii` backstop — reuse, don't build a parallel detector.
- Acceptance: a source column with no strategy hard-errors instead of emitting raw (regression proves raw passthrough pre-fix); `no_profile` compile still enforced at runtime; golden gate 53/53.

### Sprint 3 — DE-01 cluster-C (fail-closed FPE + on_unencryptable knob + relabel)
- Replace every silent `return raw`/no-op with a fail-closed raise (mirror the `FpeChecksumError` pattern at `fpe.py:260-278`): checksum short-values (`:289-291, 300-301, 310-312`), whole-value no-op (`:403-404`), covering-hash non-round-trip (`:388-389`).
- Add per-field `on_unencryptable: error | vault_token` (safe default `error`); `vault_token` reuses the existing vault (`vault.py`, `collect_vault_entries`) and requires a namespace + vault path (fails closed if absent). No `raw` value.
- Declared-separator allowlist: fail closed on undeclared out-of-charset chars; keep declared separators under `preserve_separators`.
- Correct the product language: stop claiming "NIST SP 800-38G FF1" (`unmask.py:14`, `execution/_strategies/_fpe.py:6-13`) — describe the actual construction until real FF1 lands.
- Sub-minimum domains: accept + emit a documented residual-risk note (do NOT reject).
- Acceptance: each silent path now raises or vault-tokens (regressions prove raw/wrong-value pre-fix); dashed-SSN healthy path unchanged (golden gate 53/53); no "NIST FF1" claim remains in shipped strings.

### Sprint 4 — DE-02 (KeyProvider, fail-closed at GA)  [highest blast radius]
- `KeyProvider` abstraction: opaque ≥32-byte secret + HKDF purpose/version derivation; demote `job_seed` to generation-only (reproducibility, not a key).
- Rekey the **full** keyed surface (Fable): FPE (`_strategies/_fpe.py:111`), hash, unmask, vault, `date_shift` (`_strategies/_date_shift.py:73`), `text_mask`/`text_redact` (`transforms/text_mask.py:158-170` — also fix the "32-byte" docstring lie at `:353`), deterministic faker/categorical `derive_index`, and the out-of-core path (`out_of_core/_mask_group_b.py:131`). Path-complete = `derive()`-call-site-complete.
- Fail-closed gate keyed on `is_pre_ga()`: default-off now, requires a real secret when GA binds. Dev/quickstart story documented so the gate isn't watered down.
- Acceptance: every keyed strategy derives from the provider secret, not the seed; gate off pre-GA / fail-closed at GA (both proven); golden gate 53/53; determinism preserved under an explicit seed+secret.

## Fast-follow (NOT in this delivery)
- **DE-01 Option C**: adopt an audited FF1/FF3-1 for admissible domains + vault-token sub-minimum values. Makes the "NIST FF1" claim true and restores strength. Separate future sprint; dependency vetting + determinism/round-trip re-validation.

## Follow-ups logged (not blocking)
- `examples/complex_healthcare_claims.yaml` broken end-to-end (unregistered `tax_id` provider + unhandled `replace_with_synthetic`) — examples not CI-exercised. Fix or gate examples.
- `vault: true` is a third config-flow pattern (raw-config re-read); the "one sanctioned location" ruling (typed plan field) should eventually absorb it.
- §9 compat pre-flight tool doesn't flag default-value changes in `_pipeline.py` (surfaced in TB-5) — widen its watched paths.
- **DE-11 LOW (dennis):** pool capacity pre-flight `plan/_checks.py:~173` reads only top-level `pool_size`; a `provider_config`-only `pool_size` on a `unique`-mode column skips the compile-time capacity check. Fail-safe (runtime still raises), pre-existing, but DE-11 widens it — resolve `pool_size` before the checks run.
- **DE-11 LOW (dennis):** nested faker child (`execution/_strategies/_nested.py`) doesn't inherit the parent's resolved `pool_size`/`scale` (falls back to `provider_config`/2.0 default). Not a regression; extend resolve-once to nested children.
- Commit the untracked crypto/DE-11 discussion + plan docs (`docs/discussions/2026-07-13-*.md`, this plan) into the repo at a natural checkpoint (Cam: "commit later").
- **DE-03 LOW-2 (dennis):** module-size decomposition debt is comment-only. Three execution modules sit over the 600-LOC cap after DE-03/TB-5/DE-11 security wiring (`_chunked.py` 611, `_pandas_adapter.py` 613, `_pipeline.py` 613, allowlisted in `tests/sentry/test_module_size.py`). Split a cohesive unit out to drop back under 600 when the next batch touches them, and drop the allowlist entry. (GH-issue creation was blocked by the write-gate; tracking here instead.)
- **DE-11 CHANGELOG gap:** DE-11 (pool_size/scale resolve-once, merged @034dc3e) also shipped without an `[Unreleased]` CHANGELOG entry for the new typed `ColumnSeed` fields / reject-on-contradiction. Add one alongside the DE-03 entry at the next docs pass.
- **Harness (decoy-platform) fixes staged, PR owed:** two gcp-bench robustness fixes made while running TB-6 50M: (1) `lib.sh` `bench_ssh` wrapped the `gcloud_zone` shell function in `timeout` (which can only exec a binary) so every SSH probe failed silently and masqueraded as "not reachable" — fixed to invoke the gcloud binary directly under `timeout`; (2) `spin-up.sh` SSH-reachability window made env-tunable (`BENCH_SSH_MAX_ATTEMPTS`/`BENCH_SSH_RETRY_SLEEP`). Dennis-gate + PR to decoy-platform main after the 50M run lands.
