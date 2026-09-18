# Faker pool determinism harness

Slice 0 of the faker-pool widening program
(`docs/plans/plan_faker_determinism_harness_v2.md`). Certifies which
`(faker_type, locale)` pairs pool deterministically -- byte-identical
across fresh processes, fixed `PYTHONHASHSEED` values, and pinned
locale/timezone env -- so a later slice can widen
`POOL_ELIGIBLE_FAKER_TYPES` only onto pairs this harness actually proved
safe. This slice changes no production admission behavior: it does not
touch `pool_eligible` or `POOL_ELIGIBLE_FAKER_TYPES`.

## Files

- `digest_codec.py` -- `_pool_digest`: a versioned, type-tagged,
  length-framed byte encoding for a pool's values (not `repr()`). Callers
  SHA-256 the returned bytes.
- `pool_determinism_worker.py` -- one fresh-process build of one
  `(faker_type, locale, kwargs)` candidate. Calls the real production seam
  (`_faker_pool._build_and_sample_returning_pool`) exactly once, digests
  the returned pool and selected output, and prints one
  `POOL_DETERMINISM_JSON` line.
- `hash_order_probe_worker.py` -- the mandatory synthetic fail-control
  target: its digest is built from Python `set` iteration order over
  strings, which genuinely depends on `PYTHONHASHSEED`. Used only to prove
  the driver can report divergence, never for a real candidate.
- `check_determinism.py` -- the driver. Spawns K=8 fresh workers per
  candidate with pinned env, computes a per-candidate verdict, and
  writes `certified_pairs.json` + `determinism_report.json`. Also owns the
  candidate matrix (10 positive-control types + Tier 1/Tier 2 widening
  candidates from `faker_widening_research.md`, crossed with
  `en_US`/`en_GB`/`fr_FR`/`de_DE`/`es_ES`).
- `golden_digests.json` -- committed cross-time baseline. `--write-golden`
  records it explicitly (a reviewed baseline op, never automatic); the
  default mode asserts every already-recorded candidate's digest still
  matches, and fails loud on drift.
- `certified_pairs.json` -- the current run's certified `(type, locale,
  kwargs)` triples with their agreed digests. This is the input the
  addition slice consumes.

## Running it

```
# from the engine repo root, with the dev venv active
python scripts/faker-determinism/check_determinism.py                # assert against golden_digests.json
python scripts/faker-determinism/check_determinism.py --write-golden  # explicit baseline (re)write
python scripts/faker-determinism/check_determinism.py --types city,job_female --locales en_US --k 3  # a narrow, fast probe
```

`--jobs N` runs N candidates concurrently (each candidate still spawns its
own K sequential subprocesses); the default matrix at K=8 is a genuine
multi-minute run, which is why every determinism test that spawns the sweep
carries the `determinism_harness` marker and is excluded from the default
pytest loop (`pytest -m determinism_harness` runs them explicitly). The
default loop keeps only the version-independent codec, characterization, and
driver-unit tests (including the synthetic hash-order fail-control).

## Reading a candidate's status

- `certified` -- all K runs agreed on both digests, the type is available
  at that locale with no custom override, and the requested locale never
  fell back. This is the only status the addition slice may consume.
- `not_available` -- the Faker method does not exist for that locale.
  Expected and common; not a failure.
- `custom_override` -- a registered custom provider claims the type name
  (never happens for a real candidate in a fresh interpreter with no
  custom providers registered; present for completeness).
- `locale_fallback` -- `make_faker` silently substituted `en_US` for an
  invalid requested locale. Rejected so an accidental fallback can never
  masquerade as a certified pair for the locale that was actually asked
  for.
- `divergent` -- a genuine cross-process digest disagreement. This is the
  finding the harness exists to catch.
- `inconsistent` -- `exact_name_available`/`custom_override_present`/the
  resolved locale itself disagreed across K identical-input runs, a
  stronger signal than an ordinary non-certification.

Determinism-approved is not the same claim as reuse-safe: this harness
proves ONLY that a pool builds and selects identically across processes
and time. Whether reusing a value across rows is semantically acceptable
for a given type is a separate judgment the addition slice still owes.
