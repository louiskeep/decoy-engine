# Mutation grading: `execution/_strategies/_faker.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_faker.py` (116 LOC) is the pool-backed
generation handler: `FakerStrategyHandler.run` resolves pool_size / scale / locale
/ build_config, computes a cache identity via `PoolBuilder.identity_for(...)`,
checks `ctx.pool_cache`, builds the pool via `PoolBuilder.build(...)` on a miss,
draws values via `PoolSampler().sample(...)`, and restores source nulls.

**Grade scope: FOCUSED selection only** (`tests/unit/execution/test_faker_strategy.py`).

## Numbers

**79 mutants: 48 killed (61% baseline), 31 survived -> 70 killed after this pass,
9 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **22 LOGIC survivors killed** with 8 tests. 0 product bugs.
- **9 EQUIVALENT survivors** (5 identity args that cannot collide with a real
  key, 3 cache-behavior, 1 message prose). All verified behavior-preserving.

## The identity-vs-build distinction (drives the classification)

`identity_for(...)` computes the cache-lookup KEY
`(provider, effective_locale, cfg_hash, pool_seed, size)`; `build(...)`/`sample(...)`
args feed the pool seed (`_derive_pool_seed` folds provider/locale/namespace/
config_hash) or contents. The build/sample args are always LOGIC (they change the
generated pool).

The identity args are subtler and split two ways under a SHARED, pre-warmed cache
(chunked execution builds one `PoolCache` and pre-warms a pool per faker column
before any chunk -- `_chunked.py`):

- **LOGIC (locale, config):** dropping `locale`/`config` from the lookup identity
  makes it byte-EQUAL a sibling faker column's stored key (same provider/size/
  namespace, different locale-or-config), so the handler fetches the WRONG
  pre-warmed pool -- observable wrong output in chunked runs. Killed by
  `TestFakerWarmCacheIdentity` (batch-gate P1 correction: the earlier ledger
  wrongly called these EQUIVALENT on a "a wrong identity can never fetch a wrong
  pool" claim that is false for a warm shared cache).
- **EQUIVALENT (identity=None, provider, size, namespace):** these cannot collide
  with any admissible sibling's key -- `identity=None`/`provider=None`/`size=None`
  produce a slot no real pool carries (always a miss -> rebuild correct), and the
  `namespace`-drop collapses `pool_seed` to `'_default'`, but chunk admission
  REQUIRES a non-None namespace for every faker column (`_chunked.py`), so no
  admitted sibling can carry the `'_default'` form. All rebuild correctly.

## LOGIC (22): killed by new tests

| Test | Kills | Pins |
|---|---|---|
| `test_locale_selects_the_built_pool` | 13, 14, 15, 16 (handler `locale` resolution -> None), 46 (build `locale=None`), 52 (drop build `locale`) | two locales yield different pools; nulling locale makes both runs identical |
| `test_locale_is_not_forwarded_as_a_provider_method_kwarg` | 21, 22 (build_config filter uses `"XXlocaleXX"`/`"LOCALE"`, leaking the real `locale` key into `spec.extra`) | Faker `email()` rejects a `locale=` kwarg, so the leak raises; a locale-bearing run must succeed |
| `test_provider_config_reaches_the_pool_build` | 17 (`build_config=None`), 47 (build `config=None`), 53 (drop build `config`) | a valid `domain` config knob makes every value end with the domain (note: `_config_hash(None) == _config_hash({})`, so a non-empty key was required) |
| `test_namespace_selects_the_built_pool` | 48 (build `namespace=None`), 54 (drop build `namespace`) | non-deterministic mode isolates the build-side arg (same job_seed -> same draw indices, only the pool differs) |
| `test_scale_controls_target_cardinality` | 11 (`scale=None`), 12 (invert `is not None`), 65 (sampler `scale=None`), 73 (drop sampler `scale`) | SCALE_SOURCE_CARDINALITY with `scale=0.5` collapses 4 sources to 2 distinct outputs |
| `test_typed_pool_size_field_is_used` | 8 (`pool_size=None` in the `plan.pool_size is not None` branch) | the typed `ColumnSeed.pool_size` field must be used; mutant passes `size=None` to build -> TypeError |
| `TestFakerWarmCacheIdentity::test_locale_in_lookup_identity_prevents_sibling_pool_contamination` | 29 (identity `locale=None`), 35 (drop identity `locale`) | a fr_FR column sharing a pre-warmed cache with a default-locale sibling must read its own (French) pool; the locale-blind identity collides with the sibling's key and fetches the wrong pool |
| `TestFakerWarmCacheIdentity::test_config_in_lookup_identity_prevents_sibling_pool_contamination` | 30 (identity `config=None`), 36 (drop identity `config`) | a domain-configured column sharing a cache with an empty-config sibling must read its own (target-domain) pool; the config-blind identity collides with the sibling's key |

## EQUIVALENT (9)

| Mutants | Category | Why equivalent |
|---|---|---|
| `25`, `26`, `27` | identity args that cannot match a real key (`identity=None`, `provider=None`, `size=None`) | produce a lookup slot no admissible pool carries, so always a miss -> `build(...)` runs with correct args -> byte-identical output. |
| `31`, `37` | identity `namespace=None` / dropped | collapses `pool_seed` to `'_default'`, but chunk admission requires a non-None namespace for every faker column, so no admitted sibling carries that form -> always a miss -> rebuild correct. |
| `38`, `39`, `40` | cache-behavior (`cached=None`, `cached=get(None)`, `pool=None`) | all force a rebuild -> byte-identical output (a perf regression, not a correctness one); the rebuild uses the correct build args. |
| `2` | message prose | `ValueError(None)` on the no-provider guard: `ValueError` has no machine-observable code field, and the contract (raises `ValueError` when provider is missing) is unchanged. Per the TQ error-message-wording policy, equivalent. |

## Gate

Dennis batch gate: **initially FAILED** (P1) -- the identity-side `locale`/`config`
mutants were misclassified EQUIVALENT on an over-broad "a wrong identity can never
fetch a wrong pool" claim, false for a warm SHARED cache (chunked pre-warm).
REMEDIATED here: reclassified LOGIC and killed by `TestFakerWarmCacheIdentity`
(a pre-warmed shared cache with a colliding sibling; verified the tests fail under
the manually-applied locale-drop and config-drop mutants and pass on real code).
Re-verified: 70 killed / 9 survived, the 9 being non-colliding identity args +
cache-behavior + one message. The `_composite` sibling in the same gate PASSED.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `_faker.py`, selection to
`test_faker_strategy.py`, then `rm -rf mutants && python -m mutmut run`.
