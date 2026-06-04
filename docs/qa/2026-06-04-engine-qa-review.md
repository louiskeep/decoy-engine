# Engine QA Review — 2026-06-04

**Reviewer:** QA/Performance (automated session)
**Scope:** `decoy-engine` core — determinism layer, masking strategies, generation/synthesis, FK resolution, FPE, internal crypto
**Branch:** `qa/2026-06-04-engine-review`

---

## 1. Summary

The engine's determinism envelope (HKDF-SHA256 → per-column HMAC) is structurally sound and correctly implemented against RFC 5869 + RFC 2104, with reference-vector tests pinning it. The most important finding is **`DeriveContext` was written to eliminate a per-row HKDF cost (labeled QA-10 F4) but is neither exported nor wired into any strategy handler** — the optimization exists as dead code. The second priority is a **crash path in generation mode**: custom Faker providers registered via `register_faker_provider` will raise `TypeError` when the pipeline YAML specifies non-empty `faker_kwargs` for that provider.

---

## 2. Findings

---

### F1 — HIGH | Performance | `DeriveContext` optimization is dead code

**File:** `src/decoy_engine/determinism/_derive.py:111–174`, `determinism/__init__.py:47`
`src/decoy_engine/execution/_strategies/_hash.py:55`, `_date_shift.py:63`, `_categorical.py:158`

**Issue:** `DeriveContext.for_column(seed, namespace)` was introduced (QA-10 F4, 2026-06-01) specifically to amortise the two-HMAC HKDF cost that `derive()` pays on every row. The docstring explicitly says "Strategy adapters that process a column instantiate the context once + call derive_source per row." None of the strategies do this. The class is not exported from `determinism/__init__.py`, so strategy handlers cannot import it without reaching into the private `_derive` module.

The per-row cost is: `hkdf_sha256` = 2 HMAC-SHA256 ops (extract + expand) + 1 HMAC-SHA256 (the final keyed output) = 3 HMAC calls per row. With `DeriveContext`, it becomes 2 HMAC calls once per column + 1 HMAC per row.

**Impact:** At 1M rows, `hash`, `date_shift`, and `categorical` (deterministic path) each pay ~2M avoidable HMAC-SHA256 calls per column. On modern hardware (~10ns/HMAC): ~20ms extra per column-million. For a 50-column masked table at 1M rows, that is ~1s wasted per run that the QA-10 fix was supposed to reclaim.

**Recommended fix:**

1. Export `DeriveContext` from `determinism/__init__.py`:
   ```python
   # determinism/__init__.py
   from decoy_engine.determinism._derive import (
       ...
       DeriveContext,   # add this
   )
   __all__ = [
       ...
       "DeriveContext",
   ]
   ```

2. Wire it into `HashStrategyHandler`:
   ```python
   # _hash.py
   from decoy_engine.determinism import DeriveContext, derive

   def run(self, df, column, plan, ctx):
       ...
       ctx_d = DeriveContext.for_column(ctx.job_seed, plan.namespace)
       out: list[str | None] = []
       for i, value in enumerate(source):
           if na_mask[i]:
               out.append(None)
               continue
           token = ctx_d.derive_source(plan.namespace, _canonicalize_source(value)).hex()
           out.append(token[:truncate] if truncate is not None else token)
   ```

3. Apply the same pattern to `DateShiftStrategyHandler` and the deterministic branch of `CategoricalStrategyHandler`.

**How to verify:** `python -m timeit` comparing current `hash` handler vs patched version on a `pd.DataFrame` with 1M string rows. Expected: ~33% reduction in per-column wall-clock for `hash` and `date_shift`.

---

### F2 — HIGH | Correctness | Custom provider `faker_kwargs` raises `TypeError` at runtime

**File:** `src/decoy_engine/internal/faker_setup.py:431`
`src/decoy_engine/generation/synthesize.py:337`

**Issue:** Custom providers registered via `register_faker_provider` (or loaded from disk via `load_custom_providers`) are wrapped in `get_faker_providers` as:
```python
providers[name] = lambda fn=fn, fake=fake: fn(fake)
```
This lambda accepts zero caller-supplied keyword arguments. When a generation config specifies non-empty `faker_kwargs` for a custom provider and `synthesize._faker` calls:
```python
out.append(provider_func(**faker_kwargs))  # e.g. {"min_chars": 5}
```
Python raises `TypeError: <lambda>() got an unexpected keyword argument 'min_chars'`, crashing the generation job for that table. Reflected built-in providers are safe (wrapped by `_make_reflected_provider` which accepts `**kwargs`).

**Impact:** Any pipeline that uses a custom provider and YAML-configures `faker_kwargs` silently fails with an unhandled exception. The table lands as FAILED in the job manifest. The error message is opaque (`TypeError`, not a `GenerationError`).

**Recommended fix:**
```python
# In get_faker_providers, replace the custom provider wrapping:
for name, fn in custom_snapshot:
    # Accept and discard kwargs (custom fn only takes a Faker instance; kwargs
    # are silently dropped consistent with how _make_reflected_provider handles
    # unsupported kwargs on reflected providers).
    providers[name] = lambda fn=fn, fake=fake, **_kw: fn(fake)
```

**Test to add:** Register a custom provider, configure a generation column with `faker_kwargs: {min_chars: 5}`, call `generate_tables`. Assert the output is not empty and no exception is raised (kwargs silently dropped).

---

### F3 — HIGH | Performance | `_FAKER_CALL_LOCK` serialises all concurrent Faker generation

**File:** `src/decoy_engine/generation/synthesize.py:69,331–338`

**Issue:** `_FAKER_CALL_LOCK` is a process-level `threading.Lock()` held for the entire column generation loop:
```python
with _FAKER_CALL_LOCK:
    if pre_seed is not None:
        faker_inst.seed_instance(pre_seed)
    for i in range(n):           # n up to millions
        faker_inst.seed_instance(row_seed)
        out.append(provider_func(**faker_kwargs))
```
The platform dispatches jobs via `asyncio.to_thread()`. Two concurrent jobs each with a Faker generation column will serialize completely at this lock. For a 1M-row Faker column with 10 columns per table, a single job holds the lock for the full generation loop of all 10 columns back-to-back. A second concurrent job blocks until the first finishes all its Faker columns.

This is acknowledged in the docstring as V2.1 work ("replace the shared cached instance with a per-call fresh Faker"). Flagged here as a current operational risk because the lock scope is wider than the minimum necessary: the lock could be released between columns (the column generation loop is in `generate_tables`, not in `_faker`).

**Impact:** Concurrent generation jobs serialize at the platform level. SLA for multi-tenant workloads degrades linearly with the number of concurrent Faker-column jobs.

**Short-term fix (within V2.1 scope):** Construct a per-call `Faker(locale)` instance inside the lock and seed it once with `col_seed` as the per-call seed — the per-row seeding overrides it anyway. This removes the `_get_default_faker()` shared-instance problem and allows the lock to be eliminated entirely. One `Faker()` construction per column generation call (~50-200ms each) replaces the shared-instance pattern.

**How to verify:** `time` two concurrent `generate_tables` calls in separate threads; confirm total wall-clock is approximately `max(t1, t2)` not `t1 + t2`.

---

### F4 — MEDIUM | Correctness | `pre_seed` in `_faker` is dead code

**File:** `src/decoy_engine/generation/synthesize.py:304–338`

**Issue:** For the `instance_default_locale` and default-faker paths, `pre_seed = seed` is set and then called:
```python
with _FAKER_CALL_LOCK:
    if pre_seed is not None:
        faker_inst.seed_instance(pre_seed)  # <-- sets state
    for i in range(n):
        row_seed = col_seed + i
        faker_inst.seed_instance(row_seed)  # <-- immediately overrides for i=0
        out.append(provider_func(**faker_kwargs))
```
The `seed_instance(pre_seed)` call is overridden by `seed_instance(col_seed + 0)` in the first iteration. It has no observable effect on output. This is dead code that survives from V1 structure and makes the lock scope look larger than it is.

**Impact:** No correctness impact (outputs are correct via per-row seeding). Confuses reviewers into thinking `pre_seed` contributes to determinism and increases reluctance to refactor the lock scope.

**Recommended fix:** Remove the `pre_seed` variable and the `if pre_seed is not None: ...` block. Add a comment: `# per-row seed overrides any instance-level seed; no pre-seed needed`.

---

### F5 — MEDIUM | Performance | `_build_cdf` floating-point accumulation

**File:** `src/decoy_engine/execution/_strategies/_categorical.py:50–100`

**Issue:**
```python
running = 0.0
for i, w in enumerate(weights):
    running += w
    threshold = int(running / total * _WEIGHTED_CDF_RES)
```
Repeated addition of floats accumulates rounding error. For 1000+ equal-weight categories where each weight is small, `running` can drift from the true prefix sum by a few ULPs per step, causing intermediate CDF thresholds to be off by ±1. The last entry is forced to `_WEIGHTED_CDF_RES` to absorb drift, but intermediate categories may have subtly mis-sized slots, causing slight selection bias.

**Impact:** For practical category counts (≤100), the bias is negligible. For high-cardinality configs (1000+ categories with weights < 0.001), the least-weighted categories can be systematically over- or under-selected.

**Recommended fix:** Replace the accumulation with a total-relative computation:
```python
prefix = 0.0
for i, w in enumerate(weights):
    prefix += w
    threshold = round(prefix / total * _WEIGHTED_CDF_RES)
    ...
```
Or use `math.fsum` for the prefix sums, which provides compensated summation. The last-entry force to `_WEIGHTED_CDF_RES` remains as the guard.

**How to verify:** `numpy.testing.assert_allclose(np.bincount(samples) / n, normalized_weights, atol=0.01)` on 1M samples with 1000 equal-weight categories.

---

### F6 — MEDIUM | Performance | FPE threading applies `ThreadPoolExecutor` to GIL-bound work

**File:** `src/decoy_engine/execution/_strategies/_fpe.py:91–106`

**Issue:** The `_encrypt_values` method splits values into chunks and processes them in a `ThreadPoolExecutor`. The Feistel loop (`_feistel`) is pure Python integer arithmetic: `divmod`, multiplication, modulo, and `bytes([i])` construction. These are all GIL-bound. Only the `hmac.new(...).digest()` call (8 per value) releases the GIL briefly. The comment correctly notes this: "the Feistel orchestration is GIL-bound pure Python (only the stdlib-HMAC digest releases the GIL)".

On a 2-vCPU host, 4 threads contend for the GIL during the integer arithmetic phases. Thread creation, scheduling, and GIL contention overhead can make the threaded path slower than serial for short strings (≤16 chars). The cap `workers = min(chunk_count, os.cpu_count() or 1)` is good but does not adapt to string length.

**Impact:** On a 2-vCPU CI/staging runner, `chunk_count=4` may be net-negative for short-string FPE columns. The claimed throughput improvement from chunking may be partially or fully reversed by GIL contention.

**How to verify:**
```bash
python -m timeit -r 5 -n 3 \
  "from decoy_engine.execution._strategies._fpe import FpeStrategyHandler; \
   handler = FpeStrategyHandler(chunk_count=1); handler._encrypt_values(values, encrypt_one)"
# vs chunk_count=4
```
Or `py-spy record --pid <worker_pid> -- python run_fpe_bench.py` and inspect GIL contention.

**Recommended fix:** Add a minimum chunk threshold (`if n_values < 10_000: return [encrypt_one(v) for v in values]`) so small columns fall through to serial unconditionally, independent of `chunk_count`.

---

### F7 — MEDIUM | Design | `get_default_executor()` has an unguarded check-then-set

**File:** `src/decoy_engine/execution/_pandas_adapter.py:400–416`

**Issue:**
```python
cached = _DEFAULT_EXECUTORS.get(substrate)
if cached is None:
    cached = select_execution_adapter()
    _DEFAULT_EXECUTORS[substrate] = cached
return cached
```
Two threads can both observe `cached is None` and both call `select_execution_adapter()`. The consequence is two adapter instances are constructed (not a correctness issue since adapters are stateless). However, the pattern signals unreviewed threading assumptions to future maintainers.

**Recommended fix:** Use `_DEFAULT_EXECUTORS.setdefault(substrate, select_execution_adapter())` — but this eagerly constructs the adapter even if another thread wins the race. The cleaner fix is a module-level `threading.Lock` guarding the initialisation block, consistent with `_DEFAULT_FAKER_LOCK` in `synthesize.py`.

---

### F8 — LOW | Correctness | `_topo_sort` contains a vacuous branch condition

**File:** `src/decoy_engine/generation/synthesize.py:179`

**Issue:**
```python
for start in deps:
    if start in visited or start not in deps:  # `start not in deps` always False
        continue
```
Since `start` is obtained by iterating `deps`, `start not in deps` is structurally always `False`. The dead branch adds confusion about whether `deps` can be mutated during iteration.

**Recommended fix:** Remove the dead clause: `if start in visited: continue`.

---

### F9 — LOW | Security | `deterministic_hash` concatenates without separator (collision-prone)

**File:** `src/decoy_engine/internal/crypto.py:18–40`

**Issue:** `SHA256(f"{value}{seed}")` without a separator creates preimage collisions: `value="1", seed=23` → input `"123"` collides with `value="12", seed=3` → input `"123"`. Additionally, SHA256 without a key is reversible given known plaintext. The function is correctly deprecated and emits `DeprecationWarning`. No production callers exist in the current source tree.

**Impact:** Zero in current production code. Risk is future accidental reuse by a developer who copies a V1 code pattern without noticing the deprecation. The deprecation warning makes this machine-detectable in `-W error` test runs.

**Recommended fix:** The function should remain (for backward compatibility in existing manifests), but add the collision fact to the warning message and add a CI gate (`pytest -W error::DeprecationWarning`) to catch any new caller.

---

## 3. Performance Notes

| Path | Bottleneck | Current Big-O | With Fix |
|---|---|---|---|
| `hash` strategy | CPU (HKDF per row) | O(3n) HMAC/row | O(1+n) HMAC/row with `DeriveContext` |
| `date_shift` strategy | CPU (HKDF per row) | O(3n) HMAC/row | O(1+n) HMAC/row |
| `categorical` (deterministic) | CPU (HKDF per row) | O(3n) HMAC/row | O(1+n) HMAC/row |
| FPE per-value | CPU (Feistel/HMAC per value) | O(8n HMAC) — can't avoid | GIL-bound threading may not help |
| Faker generation (concurrent jobs) | Lock contention (process-level) | O(n) serialized across jobs | O(n) parallelised per V2.1 fix |
| Parent FK map build | CPU (Python iteration) | O(n_parent × k_cols) | Vectorisable with numpy masked ops |

**Profiling recommendations:**
- `python -m cProfile -o fpe.prof` on a 1M-row FPE run; inspect HMAC call count.
- `scalene` on `generate_tables` with 10 Faker columns × 1M rows to confirm lock-time dominance.
- `memory_profiler` on `_apply_per_table_transforms` + adapter `run` on a 1M-row table with transforms to confirm the double-materialization cost.

---

## 4. Suggested Tests

| Area | Test Case |
|---|---|
| `DeriveContext` parity | Assert `DeriveContext.for_column(seed, ns).derive_source(ns, source) == derive(seed, ns, source)` for 1000 random inputs |
| `DeriveContext` performance | `timeit` `hash` handler at 1M rows with and without DeriveContext; assert ≥25% reduction |
| Custom provider + `faker_kwargs` | Register a custom provider, set non-empty `faker_kwargs` in config, call `generate_tables`; assert no `TypeError` |
| Concurrent Faker generation | Two threads both call `generate_tables` with a 100K-row Faker column; assert each thread's output matches a single-threaded run with the same seed (determinism) and wall-clock < 1.5× single-thread time (no deadlock/serialization) |
| Categorical CDF bias | 1000 equal-weight categories, 1M samples; assert `max(abs(observed_freq - expected_freq)) < 0.002` |
| FPE thread vs serial parity | Compare `chunk_count=1` vs `chunk_count=4` output on 10K rows; assert byte-identical |
| FPE bijectivity | For all 4-digit SSN-prefix strings over `digits`, assert no two inputs map to the same ciphertext under a fixed key |
| `_topo_sort` stability | Assert `_topo_sort({"B": {"A"}, "A": set()})` always returns `["A", "B"]` regardless of dict ordering |
| S3 tmp cleanup on verify failure | Mock `head_object` to raise; assert canonical key is untouched after `_materialize_s3_output` |

---

## 5. What Is Done Well

- **HKDF implementation is correct.** The RFC 5869 extract+expand split, salt enforcement, and reference-vector pinning against §A.1/§A.2/§A.3 are exactly right. The decision to stay on stdlib rather than add a PyCA dep is defensible and the 30-line implementation is clean.
- **`DeterminismError` is well-structured.** Typed codes + distinct `pool_size_overflow` vs `pool_size_invalid` (QA-7 F11) give callers reliable programmatic handling.
- **FK resolution parent-map batching (QA Q7 fix).** `.tolist()` materialisation before the loop eliminates the O(n·k) pandas scalar-unboxing anti-pattern correctly.
- **`atomic_swap_db_providers` eliminates the partial-unregister window.** The single locked swap is the right structural fix for the concurrent-reader race.
- **`_canonicalize_source` float hard-error.** Refusing to canonicalise floats (and raising `GenerationError`) forces the "do you want IEEE-754 identity or string identity?" conversation upstream, which is the right call for a determinism-first system.
- **`_fpe_pure` single-character fix (QA-10 F2).** Removing the source character from the single-char HMAC input makes the function a uniform rotation (bijective by construction) and closes the format-preserving deduplication hole.
- **S3/GCS atomic-move upload pattern.** The tmp-key → head-verify → server-side copy → delete pattern is correct and the ETag cross-check for non-multipart objects adds a meaningful integrity gate.
- **`_materialize_file_output` uses `os.replace`.** Atomic on POSIX; avoids partial-write artifacts from OOM or container restart.
