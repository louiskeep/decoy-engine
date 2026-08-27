# Task 0.3 report: the umbrella determinism protocol

Status: DONE (program-gating goldens gate passes; review round 2 remediated).

## Review round 2 remediation (2026-08-27)

The first submission's goldens gate had a blocking defect: the
`TestSourceKeyedPrimitiveGoldens` block asserted `provider.draw(...) ==
derive/derive_index/derive_value(...)`, which is `derive(x) == derive(x)` because
the generic provider's `draw()` IS `derive(...)`. The real shipped transform was
never invoked (tautology), and four providers diverged from the shipped
derivation. Fixed as follows:

- Rewrote all 10 source-keyed goldens to invoke the REAL shipped code and assert
  the provider reproduces its output: `apply_group_key` (mask.group_key),
  `apply_bucket_perturb` (mask.bucket_perturb), `DateShiftStrategyHandler`
  (mask.date_shift), `CategoricalStrategyHandler` uniform + weighted
  (mask.categorical_deterministic), `_pick_from_seq` (mask.code_set),
  `ReferenceTable.keyed_row` (mask.joint_mask_keyed_row), `PoolSampler.sample`
  (gen.pool_deterministic + mask.faker), `SsnAdapter` (gen.identifier_deterministic),
  and `FpeStrategyHandler` + shipped `fpe_encrypt_value` (mask.fpe).
- Fixed the four provably-wrong providers with dedicated compound providers:
  - `FpeKeyProvider`: the Feistel key is `derive(mask_key, namespace, FPE_KEY_LABEL)`
    (a fixed per-column label), not a per-value `derive`. The provider emits the
    KEY; the Feistel arithmetic stays with the transform / Task 0.4.
  - `CodeSetKeyedSelectProvider`: the two-step keyed selection
    `int(hmac_hex(derive(mask_key, ns or "code_set", _KEYED_SALT), key_value)[:8], 16)
    % candidate_count` then `hole_resolve`, not a single `mask_key`-keyed
    `derive_index`.
  - `JointMaskKeyedRowProvider`: the two-step keyed row index
    `int(hmac_hex(derive(mask_key, ns, _KEYED_ROW_SOURCE), key_value)[:8], 16)
    % row_count`, not a raw `derive` digest.
- Honest reclassification: 18 sites reproduce the shipped OUTPUT byte-for-byte; 1
  (mask.fpe) is keyed-material with the Feistel arithmetic deferred, its key
  proven by reproducing the real handler's ciphertext through the shipped
  `fpe_encrypt_value`. Drift tests pin the three inlined byte-constants and the
  inlined `hmac_hex` / `hole_resolve` to the shipped symbols.
- Module-size sentry (`tests/sentry/test_module_size.py`) was red at base for
  `_determinism_protocol.py` and now for `_draw_site_providers.py`. Both are
  data/registry modules; allowlisted at their current sizes (927 and 985) with
  justification and a decomposition target, per the sentry's ratchet protocol.

Below is the original report, updated where the remediation changed it.

## What was built

One `DrawSiteProvider` per catalogued draw site (30 sites, from Task 0.1's
`DRAW_SITES`). Providers live in a new sibling module
`src/decoy_engine/execution/native/_draw_site_providers.py` and are re-exported
from `execution/native/_determinism_protocol.py` so the protocol's public
surface is one import (`provider_for`, `all_providers`, `unit_float_from_bits53`,
`DrawSiteProvider`, `DrawSiteProtocolError`). An import-time invariant fails if
the registry drifts from `DRAW_SITES` (exactly one provider per catalogued id).

Each provider is a pure function of its site's identity tuple and reproduces the
shipped draw exactly: the same seed derivation (verbatim from the inventory),
the same RNG object construction, the same API operation and call shape, and the
same null-consumption rule. Versioning is per `draw_site_id` (each provider
carries its site's `provider_version`), which is why a single generator or
per-family versioning would fail: the same family draws differently at different
sites.

Design rules honored:

- Per-draw-site versioning within each family (30 distinct provider instances,
  not one generator, not per-family).
- Global sites are `partitionable=False` and REFUSE a partitioned request with
  coded error `site_not_partitionable`: `mask.shuffle` (whole-column
  permutation), `mask.grouped_series_monotone_walk` (per-group walk),
  `gen.categorical` / `gen.reference` (one Python stream), `gen.null_probability`
  / `gen.distribution_snapshot` / `gen.pool_nondeterministic` /
  `gen.composite_build_pool` (whole-column numpy), `mask.formula`. No per-row
  substream is faked for any of them.
- The two unseeded non-deterministic sites
  (`mask.categorical_nondeterministic`, `gen.identifier_nondeterministic`) refuse
  reproduction with coded error `site_not_reproducible` and hand back a genuinely
  unseeded generator.
- Three distinct Faker contracts as separate providers: pool BUILD
  (`gen.pool_build_faker`), pool SELECT (`mask.faker`, a `derive_index` over the
  pool), and synthetic per-row Faker (`gen.faker_per_row`).
- Built on `decoy_engine.determinism.derive` / `derive_index` / `derive_value`
  and `generators.derivation.GenDeriveContext`; NumPy `default_rng` (PCG64),
  Python `random.Random`, Faker `seed_instance`. Module docstring cites NumPy
  NEP-19, CPython MT19937, and RFC 5869 / RFC 2104.
- `unit_float_from_bits53(raw_u64)` extracts the upper 53 bits of a FULL u64
  (`(raw_u64 >> 11) / 2**53`), always `< 1.0`; the all-ones input maps to
  `(2**53 - 1) / 2**53`. Validated across the range and against
  `numpy.random.Generator.random()` reconstructed from the raw bit generator.

## Tests (strict TDD steps mapped)

- `tests/native/test_determinism_protocol.py` (44 tests): Step 1 float fix, Step
  2 per-draw-site emulation against the shipped seed derivations, Step 6 partition
  contract (non-partitionable sites refuse; partitionable sites are shown
  batch-invariant), Step 7 subprocess stability (a fresh child reproduces the
  parent's shuffle permutation, faker row seeds, and hash digest; and a
  fresh-process partition test where two disjoint global-index child batches
  concatenate to the in-process schedule). Plus registry totality (one provider
  per site, per-site versions, versions not collapsible to one global string).
- `tests/native/test_determinism_goldens.py` (20 tests, Step 8, program-gating):
  routes the REAL shipped engine code through the providers on fixed seeds and
  asserts reproduction. Real functions exercised:
  `ShuffleStrategyHandler.run`, `hash_array`, `_apply_monotone_walk`,
  `apply_windowed_date`, `_categorical`, `_reference`, `_apply_null_probability`,
  `_faker`, `sample_column`, and the source-keyed transforms `apply_group_key`,
  `apply_bucket_perturb`, `DateShiftStrategyHandler.run`, `CategoricalStrategyHandler.run`,
  `_pick_from_seq`, `ReferenceTable.keyed_row`, `PoolSampler.sample`, `SsnAdapter.generate`,
  and `FpeStrategyHandler.run` + `fpe_encrypt_value`. Of the 19 seed-reproducible
  sites, 18 reproduce the shipped OUTPUT byte-for-byte; 1 (`mask.fpe`) is
  keyed-material (the provider emits the per-column Feistel key, and the golden
  drives that key through the shipped `fpe_encrypt_value` to match the real handler
  output). A coverage assertion locks the routed set at 18 output-exact + 1
  keyed-material so the gate cannot silently shrink.
- `tests/native/test_draw_site_inventory_coverage.py` (Task 0.1 suite):
  allowlisted the new `_draw_site_providers.py` as protocol plumbing (it
  reproduces catalogued draws; it is not a new output-producing draw site), the
  same treatment the existing `_determinism_protocol.py` gets.

Result: `tests/native/` 118 passed. The existing determinism / generation /
transform golden suites stay green with zero fingerprint movement
(`tests/unit/determinism/`, `test_generate_determinism_ratchets.py`,
`test_gen_derive_context.py`, `test_derive_invariants.py`,
`test_shuffle_categorical.py`, `test_v2_transforms.py`: 142 passed). No golden
was weakened or edited to pass.

## Goldens-gate result

19 draw sites route through the REAL shipped engine code (no derive-vs-derive
tautologies). 18 reproduce the shipped OUTPUT byte-for-byte; mask.fpe reproduces
the keyed material (the per-column Feistel key), with the ciphertext confirmed by
driving that key through the shipped `fpe_encrypt_value` and matching the real
`FpeStrategyHandler`. The remaining 11 sites are non-reproducible by contract (9
global stream sites proven to refuse partitioning + 2 unseeded non-deterministic
sites proven to refuse reproduction). The protocol is FROZEN; each site's
`provider_version` is locked and recorded in the version table in
`docs/native/draw-site-inventory.md`.

## Inventory corrections

None required. One apparent inconsistency was investigated and cleared:
`mask.bucket_perturb` records `seed_derivation = derive(job_seed, namespace, ...)`
while its `entropy_root = mask_key`. This is not a discrepancy: `job_seed` is only
the local parameter name of `transforms/bucket_perturb.py`; the mask execution
handler binds it to `ctx.mask_key` at the call site
(`execution/_strategies/_bucket_perturb.py:74`, `job_seed=ctx.mask_key`). The
provider keys `mask.bucket_perturb` on `mask_key`, matching shipped behavior. No
inventory entry was changed.

## Concerns / notes

- `mask.windowed_date` stays flagged `uncertain` (Task 0.1) and
  `partitionable=True`: partition safety holds ONLY if the native executor pins
  the enumerate index `i` to a stable GLOBAL row number. The provider keys on the
  global index, and the fresh-process partition test exercises exactly this. If a
  future executor resets `i` per batch, the sequence diverges; that is an
  executor obligation, recorded here so Phase 1 routing respects it.
- The goldens gate reproduces the DETERMINISM (seed derivation + RNG object +
  draw sequence) at each site. Each of the 10 source-keyed sites now routes the
  REAL shipped transform or handler (not a re-derived primitive), and the provider
  reproduces its output: 18 sites reproduce the shipped OUTPUT byte-for-byte. The
  one exception, `mask.fpe`, is proven as keyed-material: the provider reproduces
  the per-column Feistel key, and the golden drives that key through the shipped
  `fpe_encrypt_value` to confirm the ciphertext matches the real handler. Nothing
  in the "reproduces the shipped output" set relies on a `derive(x) == derive(x)`
  comparison.
- Test path: the parent repo `.venv` resolves `decoy_engine` from the parent
  worktree, so tests were run with `PYTHONPATH=<worktree>/src` to bind the
  worktree source. CI installs the worktree package directly, so this is a
  local-run detail only.

## Files

- New: `src/decoy_engine/execution/native/_draw_site_providers.py`
- Modified: `src/decoy_engine/execution/native/_determinism_protocol.py`
  (re-export providers at the bottom)
- New: `tests/native/test_determinism_protocol.py`
- New: `tests/native/test_determinism_goldens.py`
- Modified: `tests/native/test_draw_site_inventory_coverage.py` (allowlist)
- Modified: `docs/native/draw-site-inventory.md` (freeze record + version table)
