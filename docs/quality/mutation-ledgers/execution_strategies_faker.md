# Mutation grading: `execution/_strategies/_faker.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_faker.py` (116 LOC) is the pool-backed
generation handler: `FakerStrategyHandler.run` resolves pool_size / scale / locale
/ build_config, computes a cache identity via `PoolBuilder.identity_for(...)`,
checks `ctx.pool_cache`, builds the pool via `PoolBuilder.build(...)` on a miss,
draws values via `PoolSampler().sample(...)`, and restores source nulls.

**Grade scope: FOCUSED selection only** (`tests/unit/execution/test_faker_strategy.py`).

## Numbers

**79 mutants: 48 killed (61% baseline), 31 survived -> 66 killed after this pass,
13 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **18 LOGIC survivors killed** with 6 new tests. 0 product bugs.
- **13 EQUIVALENT survivors** (9 cache-only identity args, 3 cache-behavior,
  1 message prose). All verified behavior-preserving.

## The identity-vs-build distinction (drives the classification)

`run_single` builds a fresh empty `PoolCache()` per call (`_pandas_adapter.py`),
so the focused tests always hit a COLD cache. `pool = cached if isinstance(cached,
ValuePool) else None` plus `PoolCache.get` returning `None` on any miss (including
`get(None)`) means a wrong/None cache identity can never fetch a WRONG pool: it
always misses, and `build(...)` runs with the CORRECT args. So `identity_for`
arg mutations are output-equivalent, while `build(...)`/`sample(...)` arg
mutations change the pool seed (`_derive_pool_seed` folds provider/locale/
namespace/config_hash) or contents and are LOGIC.

## LOGIC (18): killed by new tests

| Test | Kills | Pins |
|---|---|---|
| `test_locale_selects_the_built_pool` | 13, 14, 15, 16 (handler `locale` resolution -> None), 46 (build `locale=None`), 52 (drop build `locale`) | two locales yield different pools; nulling locale makes both runs identical |
| `test_locale_is_not_forwarded_as_a_provider_method_kwarg` | 21, 22 (build_config filter uses `"XXlocaleXX"`/`"LOCALE"`, leaking the real `locale` key into `spec.extra`) | Faker `email()` rejects a `locale=` kwarg, so the leak raises; a locale-bearing run must succeed |
| `test_provider_config_reaches_the_pool_build` | 17 (`build_config=None`), 47 (build `config=None`), 53 (drop build `config`) | a valid `domain` config knob makes every value end with the domain (note: `_config_hash(None) == _config_hash({})`, so a non-empty key was required) |
| `test_namespace_selects_the_built_pool` | 48 (build `namespace=None`), 54 (drop build `namespace`) | non-deterministic mode isolates the build-side arg (same job_seed -> same draw indices, only the pool differs) |
| `test_scale_controls_target_cardinality` | 11 (`scale=None`), 12 (invert `is not None`), 65 (sampler `scale=None`), 73 (drop sampler `scale`) | SCALE_SOURCE_CARDINALITY with `scale=0.5` collapses 4 sources to 2 distinct outputs |
| `test_typed_pool_size_field_is_used` | 8 (`pool_size=None` in the `plan.pool_size is not None` branch) | the typed `ColumnSeed.pool_size` field must be used; mutant passes `size=None` to build -> TypeError |

## EQUIVALENT (13)

| Mutants | Category | Why equivalent |
|---|---|---|
| `25`, `26`, `27`, `29`, `30`, `31`, `35`, `36`, `37` | cache-only identity args (`identity=None`, and its provider/size/locale/config/namespace -> None or dropped) | `identity_for` only computes the cache-lookup key; on the cold cache the focused tests use, a wrong/None identity misses and `build(...)` runs with the correct args, so output is byte-identical. A wrong identity cannot fetch a wrong pool (`isinstance(cached, ValuePool)` guard + miss-returns-None). |
| `38`, `39`, `40` | cache-behavior (`cached=None`, `cached=get(None)`, `pool=None`) | all force a rebuild on the cold cache -> byte-identical output; only a warm-cache-reuse test (the cache subsystem's own contract, out of this module's scope) could distinguish them, and only as a perf regression, not a correctness one. |
| `2` | message prose | `ValueError(None)` on the no-provider guard: `ValueError` has no machine-observable code field, and the contract (raises `ValueError` when provider is missing) is unchanged. Per the TQ error-message-wording policy, equivalent. |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `_faker.py`, selection to
`test_faker_strategy.py`, then `rm -rf mutants && python -m mutmut run`.
