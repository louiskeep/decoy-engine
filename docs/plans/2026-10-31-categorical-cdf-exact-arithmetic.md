# Categorical CDF: exact integer arithmetic for interpreter-independent output

Status: plan (handoff `opus-plan-cdf-exact-arithmetic-handoff.md`, decoy-platform branch `handoffs/queue`). Not reviewed. No code on this branch.

Baseline commit: `545d913` (engine `origin/main`, inspected 2026-10-31). Branch `plan/cdf-exact-arithmetic`.

Risk: R2. Reason: a determinism-visible change to masked output (compatibility-contract section 3.3) on a reversible pre-GA engine. It is not R3: no destructive or irreversible operation, no vault or production data exists that the change could strand (owner decision 2026-10-31: no old-engine recovery path), and rollback is a revert. It must land before the D-4 launch-engine freeze (decoy-platform `docs/ROADMAP.md:480`), because after the freeze the same change is a major-version determinism break.

Rules consulted: `/home/cam/dev-rules/risk-and-exceptions.md` (tier table), `testing.md`, `verification.md`, `api-and-compatibility.md`, `reuse-first.md`, `documentation.md`, `performance.md`; decoy-platform `handoffs/queue` `docs/handoffs/DEFECT-CATALOG.md`, `HOSTILE-PLAN-REVIEW.md`, `OWNER-DECISIONS-2026-10-30.md`; engine `CLAUDE.md`, `docs/compatibility-contract.md` section 3.3, `docs/records/2026-10-31-main-health-engine.md` row 4 and section "4: weighted categorical CDF".

## 1. Problem and evidence (reproduced from the lock)

`_build_cdf` (`src/decoy_engine/execution/_strategies/_categorical.py:69-119`) computes `total = sum(weights)` (`:75`), a plain running sum (`:92`), then `int(running / total * _WEIGHTED_CDF_RES)` (`:94`). CPython 3.12 made builtin `sum()` of floats compensated (Neumaier summation; CPython gh-100425, cited from memory, behaviour confirmed by the probe below), so `total` differs from 3.10/3.11 for many weight vectors and the truncated thresholds move by one slot. Every weighted-categorical route consumes this one list (section 3.2), so the band boundaries, and therefore masked output, depend on the interpreter.

Probe, run from `uv.lock` (`uv sync --frozen --extra dev --python 3.X` into three scratch venvs; CPython 3.10.20, 3.11.18, 3.12.3; same checkout `545d913`):

```
python -I cdf_probe.py   # print(sum([0.6, 0.3, 0.1]), _build_cdf([0.6, 0.3, 0.1]))
3.10.20 sum= 0.9999999999999999 cdf= [600000, 900000, 1000000]
3.11.18 sum= 0.9999999999999999 cdf= [600000, 900000, 1000000]
3.12.3  sum= 1.0                cdf= [600000, 899999, 1000000]
```

A seeded corpus of 20,000 weight vectors (2 to 200 categories; rounded decimals, raw `random()`, normalised-to-1.0, and a small repeated-value set including zeros) gives an identical result digest on 3.10 and 3.11 and a different one on 3.12: 460 of 20,000 CDFs differ (2.3%). Edge vectors from the same probe:

| Input | 3.10 / 3.11 | 3.12 |
|---|---|---|
| `[0.1] * 10` | `..., 800000, 900000, 1000000` | `..., 799999, 899999, 1000000` |
| `[1e16, 1.0, -1e16]` | `categorical_weights_nonpositive` | `categorical_weight_below_resolution` (different error code) |
| `[M, 0.6h, 0.6h]` (`M` = max double, `h` = half its ulp) | `sum` finite | `sum` = inf |
| `[1e308, 1e308]` | `categorical_weight_below_resolution` | same |
| `[nan, 1.0]`, `[inf, 1.0]` | bare `ValueError` | bare `ValueError` |

`random.Random.choices` has byte-identical source on 3.10 and 3.12 (hash of `inspect.getsource`) and uses `itertools.accumulate`, not `sum`; a 10,000-draw probe digest is identical on all three. NumPy differs by interpreter in the lock (2.2.6, 2.4.6, 2.5.0); a 10,000-draw `default_rng(7).choice(p=...)` digest is identical on all three, which is evidence for this sample only (section 9, Q6).

## 2. Invariants as properties

- P1 (interpreter independence). For every weight vector `w`, `_build_cdf(w)` returns the same tuple, or raises the same registered code, on every CPython in `requires-python` (`pyproject.toml:13`, `>=3.10,<3.13`) and on any platform. It uses no floating-point operation after converting each weight to its exact rational value.
- P2 (exactness). `cdf[i] = floor((2 * C_i * RES + T) / (2 * T))` where `C_i` and `T` are the exact rational prefix sum and total of the weights (round half up to the nearest slot). Hence `cdf[-1] == RES` by construction, `cdf` is non-decreasing, a zero weight has zero width, and every band width is within one slot of `w_i / T * RES`.
- P2a (representable total). A vector whose exact total exceeds the largest finite double (`sys.float_info.max`), or that holds a NaN or an infinity, is rejected with the registered `categorical_weights_nonfinite`. This keeps today's decline of overflowing sums (pinned by `tests/native/test_chunked_nondet_categorical_admission.py:122` and the `weight-overflow` case at `tests/parity/test_phase_d_d1b2_categorical_disposition.py:150`) while making the test exact instead of a float overflow.
- P3 (resolution rule is exact and position-free). A positive weight is rejected with `categorical_weight_below_resolution` if and only if `w_i * RES < T` exactly. Every accepted positive weight has width at least one slot.
- P4 (one builder). Every route that maps a bucket to a weighted category reads a CDF produced by `_build_cdf` and by nothing else.
- P5 (guard equals consumer). Every admission or sampler guard that decides on a weight total computes the same quantity, in the same order, as the consumer that draws with it.
- P6 (error precedence is total and interpreter-free). The code raised for an invalid vector depends only on the vector: non-finite, then negative, then non-positive total, then below-resolution, checked in that order over the whole vector.

## 3. Enumerations (from the code at `545d913`)

### 3.1 Sum and threshold sites (every `sum(`, `cumsum`, cumulative table and weight normalisation in `src/`)

Grep used: `grep -rnE '(^|[^.a-z_])sum\(|fsum|Fraction|cumsum|cumulative|bisect|searchsorted|choices|\.choice\(|weights' src/` (161 `sum(` lines). Integer-only sums (counts, byte sizes, row counts) are exact on every interpreter and are listed only by family.

| Site | What it computes | Interpreter-dependent today? | Decision |
|---|---|---|---|
| `src/decoy_engine/execution/_strategies/_categorical.py:75` | `total = sum(weights)` for the CDF | YES (probe) | Slice 1: exact integers |
| `src/decoy_engine/execution/_strategies/_categorical.py:92-94` | running sum and truncated threshold | depends on `:75` | Slice 1 |
| `src/decoy_engine/execution/_strategies/_categorical.py:112` | error message `1.0 / RES * total` | YES (message text) | Slice 1: format from the exact total |
| `src/decoy_engine/generation/statistical/_sample.py:140-146` | `_cumulative`, plain left fold for the numeric and freetext inverse CDF | no (plain `+=` in list order) | keep; pin with a KAT entry |
| `src/decoy_engine/generation/statistical/_sample.py:268,296,336,366` | `sum(counts) <= 0` / `sum(weights) <= 0` guards | only for mixed-sign or near-overflow input | Slice 3: shared left-fold total (P5) |
| `src/decoy_engine/generation/statistical/_sample.py:324,356` | `rng.choices(..., weights=...)` | no (identical stdlib source, accumulate) | keep; cross-interpreter digest test |
| `src/decoy_engine/generation/_window_admission.py:129-130,151-157,211-217,224-228` | admission `isfinite(sum(...))` and `> 0` | YES for near-overflow totals (`[M, 0.6h, 0.6h]`: finite on 3.10, inf on 3.12, while `random.choices` totals it to finite `M` on both) | Slice 3: same left fold as the consumer (P5) |
| `src/decoy_engine/generation/synthesize.py:395,626`, `src/decoy_engine/generation/_column_streams.py:139,210`, `src/decoy_engine/generators/columns.py:413,527` | `random.choices` with config weights | no | keep; digest test |
| `src/decoy_engine/generators/_distribution.py:143,157-158,259-272,379-388` | NumPy `weights.sum()`, `p = w / total`, `default_rng(seed).choice(p=...)` | not by interpreter; YES by NumPy version, and the lock resolves a different NumPy per interpreter | out of scope; Q6 |
| `src/decoy_engine/generation/window.py:322-323` | `np.cumsum` of integer row byte sizes | no (integers) | none |
| `src/decoy_engine/quality/fidelity.py:175,204,423,451,510`, `src/decoy_engine/quality/shape_fidelity.py:89,118,231`, `src/decoy_engine/quality/_distribution_gof.py:131-132,205-223` | report-plane similarity, TVD and KS from float sums; `fidelity.py:423,451,510` iterate a `set` | YES for the raw float; rounded to 6 places (`fidelity.py:138`) before it is reported | out of scope; Q5 |
| `src/decoy_engine/quality/dp_ledger.py:58-64` | `math.fsum` of epsilon and delta charges | no (fsum is correctly rounded) | reuse as precedent |
| Integer sums in `subset/`, `generation/_bounds.py`, `generation/_streamed.py`, `quality/dp.py`, `quality/snapshot.py`, `validation/post/`, `profiling/` | counts and bytes | no | none |

The `set` iteration finding for Q5 was checked with the same expression as `fidelity.py:423` over 400 string keys (not the function itself): on 3.10 six `PYTHONHASHSEED` values give four different raw sums (`0.674820540229456` to `0.6748205402294567`); on 3.12 all six agree. The six-place rounding hides it except at a rounding boundary.

### 3.2 Routes that consume the weighted CDF

- In-memory handler, Python: `src/decoy_engine/execution/_strategies/_categorical.py:188` builds, `:237-241` bisects. Built once per `run()` call, so once per chunk on chunked routes.
- In-memory handler, native sequential: `src/decoy_engine/execution/_strategies/_categorical.py:193-204` passes the built list to `src/decoy_engine/execution/_sequential_native.py:355-376`.
- Out-of-core, Python fallback: `src/decoy_engine/execution/out_of_core/_mask_group_b.py:387` builds per batch, `:392-398` bisects.
- Out-of-core, native: `src/decoy_engine/execution/out_of_core/_categorical_native.py:68` (`prepare_categorical`) then `:225`; selection in `src/decoy_engine/execution/native/_categorical_ext.py:136-137` (`np.searchsorted`, documented equal to `bisect_right` at `:131`).
- Chunked native, deterministic and positional: `src/decoy_engine/execution/native/_categorical_prepared.py:73` and `:79-103`, built once per run (`:111-130`).
- The Rust companion has no CDF code (`grep -i 'cdf|weight' decoy-engine-native/src/*.rs` is empty); it receives the Python-built integers.
- Not consumers (separate samplers, unchanged): statistical generation (`_sample.py`), synthetic generation (`synthesize.py`, `_column_streams.py`), legacy generators (`generators/columns.py`, `generators/_distribution.py`).

### 3.3 Exit paths of the builder (today, then planned)

- Return of the threshold list: `_categorical.py:119`.
- `categorical_weights_nonpositive`: `_categorical.py:76-81`, checked BEFORE any negative check, so the code for a mixed-sign vector depends on the total (and on the interpreter, section 1).
- `categorical_weights_negative`: `_categorical.py:86-91`, per element inside the loop.
- `categorical_weight_below_resolution`: `_categorical.py:103-114`, phase-dependent (a sub-slot weight passes or fails depending on where its band falls).
- Bare `ValueError` ("cannot convert float NaN to integer") from `int(...)` at `_categorical.py:94` when a NaN or an infinity reaches it (an infinite weight makes `running / total` NaN), caught only by `src/decoy_engine/execution/native/_categorical_prepared.py:99` (which also names `OverflowError`); the in-memory and out-of-core handlers let it escape unregistered. This is a class 6 instance, and `tests/parity/test_phase_d_d1b2_categorical_baseline.py:100-104` pins the bare error as expected behaviour.
- A finite vector whose float sum overflows (`[1e308] * 4`): `running / inf` is 0, so it exits as `categorical_weight_below_resolution`; the chunked admission declines it as `categorical_weights_unbuildable_cdf`.
- Planned: one validation pass in P6 order (P2a's representable-total check sits with the non-finite check), every exit a registered `StrategyError` code, adding `categorical_weights_nonfinite` to `src/decoy_engine/reasons/runtime/_part3.py` (beside `:97-102`). `_categorical_prepared.py:99` then catches nothing new; the slice keeps the clause and adds a test that it is unreachable for a list of floats.

### 3.4 FK shapes

- Single-column FK key that is a deterministic categorical column: stays on Python (`docs/compatibility-contract.md:253`). Parent and child keys call the same builder in the same process, so a run joins with itself under either builder. Cross-run join stability (old output plus new rows) breaks once at the cutover, which is the section 6 contract entry.
- Composite, self-referencing, cross-table and children-in-another-batch FK shapes: no categorical-specific code path; the builder is a pure per-column function of the config weights, so every shape reduces to the single-column case. The relational test in section 5 (T8) covers single and composite keys.

### 3.5 Fields of the public and shared result types touched

- `PreparedCategorical.categories`, `.cdf`, `.positional` (`src/decoy_engine/execution/native/_categorical_prepared.py:26-31`): `cdf` stays `tuple[int, ...] | None`; values change per P2.
- `CategoricalCall` fields built at `src/decoy_engine/execution/_sequential_native.py:370-375` (namespace, mask key, categories tuple, cdf tuple): unchanged shape.
- `StrategyError.code`, `.strategy`, `.message` for each builder exit: one new code, message text for below-resolution changes (computed from the exact total).

### 3.6 Output surfaces

- Masked data: weighted categorical columns on every route in 3.2 (one-slot band moves; section 4).
- Error text: the three existing builder messages and the new non-finite one. Messages carry the weight value and index (`_categorical.py:90,108`) and never a category label or a source value; the slice keeps that.
- Admission reasons: `categorical_weights_unbuildable_cdf:{name}` (`_categorical_prepared.py:75,100`), now reached for exactly the vectors that raise a registered code.
- Run metrics: `quality_metrics["sequential_categorical"]` backend per column (contract `docs/compatibility-contract.md:254`), unchanged.
- Docs and proof surfaces: `docs/proof-manifest.json:441-445` (`mask.categorical`) has no weights, so it does not change; `scripts/gen_proof_manifest.py` is re-run as the check.

## 4. Method, source pattern, and output consequence

Method. Every finite IEEE 754 binary64 value is a dyadic rational `n / 2**k`, available exactly through `float.as_integer_ratio()` (IEEE 754-2019 section 3.3; Python `float.as_integer_ratio` documentation). Scale every weight to the largest power-of-two denominator in the vector, so each weight is an exact Python `int` and the prefix sums and total are exact integer sums, independent of order and interpreter. Thresholds are `(2 * C_i * RES + T) // (2 * T)`: integer division, round half up. This is the integer-frequency normalisation used where a CDF must be bit-identical across encoder and decoder, such as range coding and asymmetric numeral systems (Witten, Neal, Cleary 1987, "Arithmetic coding for data compression", CACM 30(6); Duda 2013, arXiv:1311.2540), with the exact input taken from the float rather than pre-quantised. `fractions.Fraction` gives the same values and is the independent test oracle (section 5, T1), not the implementation, because per-step `gcd` normalisation is slower.

Rejected alternatives:

- `math.fsum` for the total (Shewchuk 1997, correctly rounded): interpreter-independent, but the division, multiplication and truncation stay in floating point, the guard stays phase-dependent, and `[0.6, 0.3, 0.1]` becomes `899999` on every interpreter (the audit checked this). It fixes P1 only.
- Pin the reduction order (an explicit left fold reproducing the 3.10 total): zero output change on 3.10, but it keeps float truncation and the phase-dependent guard, and any later `sum()` or vectorised refactor silently reintroduces the drift. Kept as Q1's fallback.
- Exact arithmetic with floor (truncation): the `[0.6, 0.3, 0.1]` thresholds become `[599999, 899999]` because binary `0.6` is below `0.6`, and `[1.0, 1e-20, 1.0]` passes the phase guard; two existing tests go red for no gain.

Output consequence (measured on the 20,000-vector corpus, exact round-half-up with the P3 rule against today's 3.10 output):

- Every moved threshold moves by at most one slot (max |delta| = 1). Mean moved slots per vector 21.85, so a given row changes category with probability at most about 2.2e-5 for these vectors (at most `(categories - 1) / 1,000,000` in general).
- 20 of 20,000 vectors (0.1%) that build today are rejected by P3: each has a weight whose exact share is below one slot and which passed only because of where its band fell. None move the other way.
- `[0.6, 0.3, 0.1]` stays `[600000, 900000, 1000000]` (the pinned expectation in `tests/unit/execution/test_categorical_weighted.py:76-78` holds on 3.12 after the change); `[1.0, 1.0, 1.0]` becomes `[333333, 666667, 1000000]` (the test at `:64-71` tolerates this).
- `[1e308, 1e308]` and `[1e308] * 4` change code from `categorical_weight_below_resolution` to `categorical_weights_nonfinite` (P2a); every route still declines or raises, and the chunked decline reason string is unchanged.

Goldens, proof manifest and Faker fixtures: see section 5.3 for the measured list. The Faker-value snapshot failures already on main (`tests/snapshots/golden/mask_faker_seeded`, audit baseline) are a separate known baseline; nothing here touches Faker and the plan does not regenerate them.

Performance (3.12, best of 3, one call, `1.0 + U(0, 1e-3)` weights; prototype in scratch, not engine code):

| Categories | Today | Exact | Ratio |
|---|---|---|---|
| 10 | 0.003 ms | 0.02 ms | 3.5x |
| 1,000 | 0.31 ms | 1.18 ms | 3.8x |
| 100,000 | 33 ms | 131 ms | 4.0x |
| 999,000 | 272 ms | 1,358 ms | 5.0x |

Above 1,000,000 positive categories no vector can pass P3, so 999,000 is the ceiling. Because the out-of-core Python fallback builds per batch (`_mask_group_b.py:387`) and the handler per chunk (`_categorical.py:188`), Slice 2 memoises the builder so a run builds each distinct weight vector once; the net cost per run then drops below today's for any column processed in six or more batches. A single-chunk run with 999,000 categories pays the one-off 5x (about 1.1 s more on this container).

## 5. Acceptance tests, red first

Generated tests are written first and recorded red on the stated interpreter before the builder changes. Each has a non-vacuity check.

### 5.1 Tests

- T1 Known-answer vectors. `tests/vectors/categorical_cdf_kat.json`, generated by a committed script that uses only `fractions.Fraction` (independent of the integer implementation), with provenance in `tests/vectors/README.md`. Corpus: ties (`C_i * RES / T` exactly `k + 0.5`), zero weights (leading, trailing, all-but-one), tiny weights at, one ulp below and one ulp above `T / RES`, subnormals, very many categories (10, 1,000, 100,000, 999,000 with a digest instead of the full list), weights normalised to sum to `1.0 +- 1 ulp` and `+- 3 ulp`, integers above `2**53`, `-0.0`, huge finite (`1e308` pairs), `[0.6, 0.3, 0.1]`, `[0.1] * 10`, the 20,000-vector seeded corpus (as a digest). Red today: on 3.12 for `[0.6, 0.3, 0.1]` and `[0.1] * 10`; on 3.10 for every vector where round-half-up differs from truncation (most of the corpus). Non-vacuity: the test re-implements today's float builder inline and asserts at least 50 KAT vectors where it disagrees with the expected value.
- T2 Property test (Hypothesis, already in the lock: 6.155.7). For generated finite non-negative vectors: builder equals the `Fraction` oracle; P2 monotone, `cdf[-1] == RES`, zero weight zero width, width within one slot of the exact share; P3 both directions; scaling all weights by `2**k` (exact) leaves the CDF unchanged. Mutation: replace the integer total with `sum(weights)`; the scaling and oracle properties go red on every interpreter.
- T3 Error precedence table (P6): `[nan, -1]` nonfinite, `[inf, 1]` nonfinite, `[1e308] * 4` nonfinite, `[1e16, 1.0, -1e16]` negative, `[0, 0]` nonpositive, `[1, 1e-20, 1]` below-resolution, `[1.0, 2.0e-6, 1.0]` below-resolution. Red today: the third row is `nonpositive` on 3.10 and `below_resolution` on 3.12; the first two raise bare `ValueError`.
- T4 Cross-interpreter CI gate. A new `cdf-interpreter-matrix` job in `.github/workflows/ci.yml` runs T1 to T3 and T6 on 3.10.20, 3.11 and 3.12 from `uv.lock` (`uv sync --frozen`), asserts `sys.version_info[:2]` matches the matrix cell inside the test session, and parses JUnit the way `dp-certified-gate` does (`.github/workflows/ci.yml:213-250`): fail unless at least one test passed and none skipped. Red-first evidence is the T1 run on 3.12 before Slice 1.
- T5 Route parity: for 30 KAT vectors and 2,000 source values, plus, for three KAT vectors, a committed list of "slot-hitting" source values whose bucket (under the test mask key and namespace) equals a threshold that moves between today's builder and the exact one, so a route that still uses a float CDF changes a visible output and not only an internal list, the in-memory Python handler, the out-of-core `categorical_array`, the `_categorical_ext._select` reference with a prepared CDF, and the positional variant pick identical categories (native companion cells run where it is installed and are recorded as not run otherwise). Mutation: give one route a local float builder; the parity test goes red on 3.12.
- T6 Guard equals consumer (P5): `[M, 0.6h, 0.6h]` year bins and top values are admitted by `_window_admission` and drawn by `_sample` on every interpreter; an AST sentry asserts no builtin `sum(` over weights or counts remains in `_window_admission.py` and `_sample.py` guard sites. Red today on 3.12 (admission refuses).
- T7 One builder (P4): an AST sentry over `src/` asserts that `bisect_right(cdf` and `searchsorted(cdf` consume a value whose only producer is `_build_cdf` or `PreparedCategorical.cdf`, and that no module other than `_categorical.py` computes `_WEIGHTED_CDF_RES` thresholds. Seeded evasion: a fixture module with a local builder must trip it.
- T8 Relational: weighted categorical as a single and a composite FK key over parent and child tables, every route that admits it, keyed with the T5 slot-hitting values; 100% join; output equals the KAT-predicted category for every slot-hitting key; the same seed run twice is byte-identical.
- T9 Memo safety (Slice 2): the memoised builder returns a `tuple`; a barrier-forced test runs 16 threads over 4 weight vectors and asserts every result equals the unmemoised builder; a call-count test over a 20-batch out-of-core run asserts one build per distinct vector. Mutation: return a list and mutate it in one consumer; T9 goes red.
- T10 Performance: extend `tests/perf/_d1b2_categorical_bench.py` with a 100,000-category weighted column over 10 out-of-core batches. Budgets are relative to today's builder measured in the same environment, as owner decisions require for throughput: (a) one build at 999,000 categories at most 6x today's (measured 5.0x); (b) total builder time for the 10-batch run no worse than today's (memo makes it about 0.5x).
- T11 Message plane: every builder error message for a corpus whose category labels carry a marker string contains no marker (labels must never reach the message). Mutation: interpolate `categories[i]` into the below-resolution message; T11 goes red.

### 5.2 Existing tests that change (finding)

Three existing tests change their assertion; none is widened or skipped, and each needs the owner's sign-off (Q2, Q7). The section 5.3 probe found no other change in the 47 files.

1. `tests/parity/test_phase_d_d1b2_categorical_baseline.py:100-104` (`nan` and `inf` cases) assert `pytest.raises(ValueError, match="cannot convert float NaN to integer")`, pinning the unregistered error. Under P6 the oracle raises `StrategyError` with `categorical_weights_nonfinite`, which is not a `ValueError` (`StrategyError` derives from `ExecutionError`, `src/decoy_engine/execution/_errors.py:24,33`, and `DecoyError(Exception)`, `src/decoy_engine/_error_base.py:11`). The plan replaces the assertion with the exact code and strategy, which is stricter than a message match. Keeping a bare `ValueError` with the same text would keep a class 6 defect alive on purpose; the plan does not recommend it.
2. `tests/unit/execution/test_categorical_weighted.py:106-112` (`test_weight_just_above_resolution_passes`) feeds `[1.0, 2.0e-6, 1.0]`, whose exact share is `2.0e-6 / 2.000002 = 0.999999` of one slot, so it is below the resolution its name claims to be above; it passes today only because of where the band falls. Under P3 it raises. The plan changes its input to a genuinely-above weight (exact share of one slot plus one ulp, from the KAT) and adds a sibling asserting the old input raises. That changes an existing assertion's input (Q2).

### 5.3 Probe of the existing suite under the exact builder

Probe-only pytest plugin (scratch, not committed) that rebinds `_build_cdf` in the three importing modules (`_categorical.py`, `out_of_core/_mask_group_b.py`, `native/_categorical_prepared.py`) to a prototype of the P2, P2a, P3 and P6 builder, with the existing nonpositive code standing in for the not-yet-registered non-finite code. Files: the 47 test modules under `tests/` that mention both `weights` and `categorical` or `_build_cdf`, excluding `tests/perf`, `benchmark`, `codspeed`, `connectors`. Command, per interpreter, from the `uv.lock` venvs: `EXACT_CDF={0,1} PYTHONPATH=<plugin dir> python -m pytest -p exactcdf2_plugin -q -o addopts="" <47 files>`.

| Interpreter | Today's builder | Exact builder |
|---|---|---|
| 3.10.20 | 1797 passed, 181 skipped | 3 failed, 1794 passed: the three 5.2 tests |
| 3.12.3 | 2 failed (`test_skewed_weights_skew_cdf`, plus audit row 7 `test_an_unparseable_sibling_when_declines_rather_than_guessing`, an unrelated f-string tokeniser drift), 1795 passed | 4 failed: the three 5.2 tests plus the same unrelated row 7; `test_skewed_weights_skew_cdf` passes |

A first prototype without P2a also turned 5 overflow-decline cases red (`test_chunked_nondet_categorical_admission.py` `weights_sum_overflows` x4, `test_phase_d_d1b2_categorical_disposition.py` `weight-overflow`), which is why P2a exists.

Goldens, proof manifest, Faker fixtures: no committed golden uses weighted categorical. `grep -rl weights` over `tests/snapshots/golden`, `tests/integration/golden`, `docs/proof-manifest.json` and `scripts/gen_proof_manifest.py` is empty; `docs/proof-manifest.json:441-445` is the unweighted `mask.categorical`. The `tests/native/test_determinism_goldens.py:514-533` weighted case rebuilds its expectation through `_build_cdf` itself, so it passes under either builder and proves route agreement, not the thresholds (T1 closes that gap). Nothing is regenerated except the F29 reason fixtures under `tests/fixtures/f29/`: `producer-inventory.json:190-191` pins `_build_cdf` producer line numbers (`:76`, `:86`) and the new code is a registry addition. The worker regenerates them only through `python tests/sentry/_reason_baseline.py --write` (its docstring: the registry may add identities; the inventory may only shrink) and stops and reports if the sentry refuses the addition. The Faker-value snapshot failures on main are a separate baseline and are untouched.

## 6. Class table (DEFECT-CATALOG section 3)

| # | Class | Structure that closes it here | Sentry or property test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | Builder messages carry only the weight value and index (config, not source data); category labels never interpolated | T11 marker corpus over every builder exit | Interpolate `categories[i]` into the below-resolution message |
| 2 | Cleanup on exit paths | N/A: the builder and the memo own no file, handle, lock or scratch resource; every exit is a return or a raise with no side effect | T3 enumerates every exit and its code | Remove the non-finite check; T3 row 1 raises bare `ValueError` |
| 3 | State machine and concurrency | Memo is a pure function of an immutable key; values are tuples; no invalidation exists because the function has no external input | T9 barrier-forced threads plus call count | Return a mutable list from the memo and mutate it in one consumer |
| 4 | Route parity and silent fallback | One builder for every route (P4); guards share the consumer's total (P5); unbuildable vectors decline with the registered reason | T5 route parity, T6 guard parity, T7 one-builder sentry | Local float builder in `_mask_group_b.py`; `sum(` back in `_window_admission.py` |
| 5 | Referential integrity | Parent and child keys use the same pure builder in one process | T8 relational test, single and composite key, over slot-hitting keys | Build the child CDF with the float builder; the slot-hitting child keys stop joining and stop matching the KAT |
| 6 | Untrusted producer (config and profile weights) | One validation pass in P6 order at builder entry; every rejection is a registered `StrategyError`; `categorical_weights_nonfinite` added (also covers an exact total above the largest double, P2a). Today's bare `ValueError` is a pinned class 6 instance (section 5.2) | T3 precedence table, T2 over generated input including NaN, inf, `-0.0`, bools, `2**53 + 1`, `[1e308] * 4` | Drop the `isfinite` check; a NaN escapes as bare `ValueError`. Drop the P2a check; `[1e308] * 4` builds and the chunked admission tests at `test_chunked_nondet_categorical_admission.py:122` go red |
| 7 | Check then use on a path (TOCTOU) | N/A: no path, file descriptor or filesystem object is involved anywhere in the change | none needed; T7 shows the change stays inside the pure builder | not applicable, no path API is touched |
| 8 | Weak tests and overclaimed evidence | KAT from an independent `Fraction` oracle; CI matrix with a JUnit non-skip check; probe numbers in this plan come from `uv.lock` in one checkout | T1 non-vacuity count, T4 version assertion and JUnit parse | Regenerate the KAT from the implementation (self-referential); T1's independent-oracle check fails |
| 9 | Behavior-preserving refactor drift | The change is intentionally not behavior-preserving; the characterization is the measured delta (section 4) and the precedence table pins the reordered checks | T3, T1 digest of the 20,000-vector corpus | Swap the negative and non-positive checks; T3 row 3 goes red |
| 10 | Frozen contract drift | Contract section 3.3 gets an interpreter-independence entry and a dated pre-GA output-change record; reasons registry change is additive | `tests/sentry/test_reason_registry.py`, T1 committed vectors as the golden | Edit a KAT expectation without the contract entry; review rule "changed golden is the alarm" (contract section 3.3) |

## 7. Reuse inventory

- `_build_cdf` stays the single owner (`src/decoy_engine/execution/_strategies/_categorical.py:69`); no new module. Current size 244 lines, well under the 600 target (`tests/sentry/test_module_size.py`).
- Reasons registry: `src/decoy_engine/reasons/runtime/_part3.py:12-15,97-102` for the existing codes; add the non-finite code there and in its export list (`tests/sentry/test_reason_registry.py`).
- `math.fsum` precedent and comment style: `src/decoy_engine/quality/dp_ledger.py:58-64`.
- KAT layout and provenance file: `tests/vectors/README.md` and the FF1 KAT JSON files beside it.
- CI non-vacuity pattern: `dp-certified-gate` in `.github/workflows/ci.yml` (JUnit parse, fail on skips).
- Methodology citation: add `_categorical.py` to `docs/methodology-registry.yaml` with a `Pattern:` line naming exact dyadic-rational integer normalisation; enforced by `tests/sentry/test_methodology_citations.py`.
- Perf harness: `tests/perf/_d1b2_categorical_bench.py` and `tests/perf/test_throughput_budgets.py`.
- Proof manifest regeneration and diff: `scripts/gen_proof_manifest.py`, `tests/sentry/test_proof_manifest.py`.
- Hypothesis (in the lock) for T2; no new dependency. `fractions` and `functools.lru_cache` are stdlib.

## 8. Frozen contracts touched

- `docs/compatibility-contract.md` section 3.3 (determinism guarantee). Breaking for weighted categorical output (one-slot band moves, about 0.1% of weight vectors newly rejected), pre-GA hard change. Add: (a) a dated "Current state" entry in the style of the C1b-i paragraph (`docs/compatibility-contract.md:301-307`) stating what moved and that no vault or routing outcome changed except the newly rejected sub-slot weights; (b) a standing sentence that weighted categorical output is identical on every supported CPython, with the interpreter range stated, since the contract pins libraries and says nothing about the interpreter today.
- `SEED_PROTOCOL_VERSION`: not bumped (derivation unchanged; precedent C1b-i and C5b-i did not bump). Q3.
- `docs/strategies.md:296-297` (`weights`): state the exact minimum share (`1 / 1,000,000` of the total) and that thresholds are exact.
- Reasons registry `decoy-reasons/v1`: additive code `categorical_weights_nonfinite`.
- `CHANGELOG.md`: one entry covering the output change and the new rejections.
- Golden baselines: the new KAT file is itself a golden; any later change to it is a contract event.

## 9. Owner questions

- Q1. Method: exact integer arithmetic with round half up (recommended), or pin the 3.10 left fold (zero output change on 3.10, keeps the float truncation and the phase-dependent guard)?
- Q2. Accept P3 (exact resolution rule): about 0.1% of random weight vectors that build today are rejected, and `test_weight_just_above_resolution_passes` gets a corrected input plus a sibling that asserts the old input raises (section 5.2)? Recommended yes.
- Q3. Leave `SEED_PROTOCOL_VERSION` unchanged and record the change in contract section 3.3 (recommended), or bump it?
- Q4. CI matrix scope: only the CDF and sampler modules on 3.11 and 3.12 (recommended now), or the whole suite (blocked by the other 3.12-only failures in the main-health audit rows 5 to 8)?
- Q5. Report-plane float sums (`fidelity.py:423,451,510` iterate a `set`; raw value depends on `PYTHONHASHSEED` on 3.10, hidden by six-place rounding): separate follow-up with `math.fsum` (recommended), not part of this plan.
- Q6. The lock resolves NumPy 2.2.6, 2.4.6 and 2.5.0 for 3.10, 3.11 and 3.12. `generators/_distribution.py` draws with `default_rng(...).choice(p=...)`, whose stream NumPy does not promise across versions. Pin one NumPy for all supported interpreters, or record it as a known cross-interpreter nuance? Separate item.
- Q7. Replace the pinned bare `ValueError` for NaN and infinite weights with the registered `categorical_weights_nonfinite` and update the two pinning tests (section 5.2 item 1)? Recommended yes; the alternative keeps a class 6 instance.

## 10. Slices

| Slice | Repo | Size | Content | Gate |
|---|---|---|---|---|
| 1 | decoy-engine | M | T1, T2, T3, T11 red first; exact builder with P2a and P6; non-finite code and F29 fixture regeneration; message from the exact total; KAT generator script and provenance; the three 5.2 test changes (after Q2 and Q7); methodology registry entry; contract 3.3, strategies, CHANGELOG | Codex (R2) |
| 2 | decoy-engine | S | T9, T10 red first; `functools.lru_cache(maxsize=8)` around a tuple-keyed builder returning a tuple; consumers read the tuple | argon |
| 3 | decoy-engine | S | T6 red first on 3.12; one left-fold total helper in `_sample.py`, imported by `_window_admission.py`; AST sentry | argon |
| 4 | decoy-engine | S | T4 CI matrix job; T5, T7, T8 | argon |

Order: 1, then 2 and 3 in either order, then 4. Slice 4 can merge with 1 if the reviewer prefers one CI change. Worst-case memo memory: 8 entries of up to 999,000 ints, about 36 MB each, about 290 MB; realistic runs hold a few small vectors. If that bound is unacceptable, Slice 2 hoists the build into the out-of-core per-column setup instead.

## 11. Pre-mortem (ten ways a gate reviewer breaks it, each with its red-first test)

1. Common denominator built from the smallest instead of the largest power of two, losing precision for subnormals: T1 subnormal rows, T2 oracle.
2. A leftover float operation (message formatting, a `float(T)` used in a comparison) keeps 3.12 different: T4 on 3.12, T1 message row, T7.
3. One route still builds its own CDF, or caches the old float list: T5, T7.
4. The memo hands out a mutable list that a consumer edits: T9.
5. 999,000 categories times per-batch building blows the run time: T10, T9 call count.
6. NaN or infinity escapes as a bare error on the in-memory route: T3.
7. Error precedence still depends on the total for mixed-sign input: T3 row 3.
8. `-0.0`, bools, integers above `2**53`, NumPy scalars convert differently on one route: T2 generated types, T5.
9. A `from_profile` long-tail column (a singleton among more than 1,000,000 rows) now fails where it sometimes passed: T1 row for share just below one slot; Q2 records the decision; the profile path is exercised in T5 with a long-tail distribution.
10. The KAT is generated by the implementation, so it proves nothing: T1 independent-oracle and non-vacuity checks.
11. The CI job silently runs 3.10 in every cell, or skips: T4 version assertion and JUnit parse.

## 12. Worker evidence rules (DEFECT-CATALOG section 5)

- Name the class of every finding and fix the class, not the instance.
- Write the generated tests first (T1 to T3, T6, T9, T11) and record them red, with the interpreter.
- Before pushing, attack your own diff: mutate each new mechanism once (the mutations in section 6) and show the test go red.
- Run baselines from `uv.lock` in the same environment as the branch (`scripts/dev/gates.sh --baseline` where present); state every suite not run.
- Never widen a threshold, skip a test or reduce a budget to make this pass. The one changed test input (5.2) is owner-gated.
- If a defect of a catalogued class recurs, stop and report it with a class-level proposal; do not patch a third time.

## 13. What this plan did not run or verify

- The engine suite outside the 47 files of section 5.3 (in particular the full suite on 3.11 under the exact builder: only the CDF probe and corpus ran on 3.11); the native companion (not installed: native cells in T5 are design only); Postgres, `ml`, `dp_certified` (3.10.20 certified profile not re-checked here).
- `decoy-engine-native` was read only by grep.
- Performance numbers come from a scratch prototype of the exact builder on one container; T10 re-measures with the real code.
- The `[M, 0.6h, 0.6h]` admission divergence is derived from the probed `sum` values plus reading `_window_admission.py:151-157` and `random.choices`; it was not run end to end through admission.
- Literature citations in section 4 (Witten, Neal, Cleary 1987; Duda 2013; Shewchuk 1997; IEEE 754-2019) and the CPython issue number are from memory and were not fetched here.
- CPython 3.12.3 is the system 3.12 used by `uv` here; 3.10.20 and 3.11.18 are uv-managed.
