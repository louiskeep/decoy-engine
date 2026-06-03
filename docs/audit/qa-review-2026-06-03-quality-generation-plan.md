# QA Review: quality/, generation/composite/, generation/pool/, expressions.py, validation/_config.py, plan/_compile.py

**Date:** 2026-06-03  
**Reviewer:** Senior QA / Performance Engineer (automated session)  
**Branch:** `qa/review-2026-06-03-quality-generation-plan`  
**Engine HEAD:** `d64b641e5c42445dba2581ce57e91c10bf11ad75`

## Scope

Modules reviewed in this session (not covered by prior QA branches):

| Module | Size | Focus |
|---|---|---|
| `src/decoy_engine/expressions.py` | 3.2 KB | safe_eval, MASK_GLOBALS, make_mask_globals |
| `src/decoy_engine/validation/_config.py` | 3.2 KB | PipelineValidationError, _select_validator |
| `src/decoy_engine/quality/synth_report.py` | 34 KB | D7a/D7b/D7c SynthReport, DCR, _band_for |
| `src/decoy_engine/quality/policy.py` | 19 KB | quality-policy/v1 gating |
| `src/decoy_engine/quality/fidelity.py` | 14 KB | TVD, quantile RMSE, freetext scoring |
| `src/decoy_engine/generation/composite/_custom.py` | 11 KB | CompositeCustom, _build_pools, _generate_deterministic |
| `src/decoy_engine/generation/pool/_sampler.py` | 15 KB | PoolSampler, _match_source_cardinality, sample_bundle |
| `src/decoy_engine/plan/_compile.py` | 38 KB | compile_plan, _build_seed_envelope |

Prior QA reports excluded from this scope:
- `qa/review-2026-06-02-generators-profile-context` — generators/columns.py, profile/_source.py, data_discovery.py, context.py, walks/hazards.py, instrumentation/timing.py, errors.py, disguises/loader.py
- `qa/review-2026-06-02-engine` — date_shift.py, fpe.py, synthesize.py, _when_gate.py, context.py, connectors/s3.py, determinism layer

---

## Findings Summary

| ID | Severity | Category | Module | One-line description |
|---|---|---|---|---|
| F1 | **Critical** | Determinism | `expressions.py` | `MASK_GLOBALS` exposes module-global `random` functions; FormulaStrategy callers not using `make_mask_globals(rng)` share non-isolated RNG state |
| F2 | **High** | Correctness | `quality/synth_report.py` | `_band_for` returns `"moderate"` but `_SEVERITY_BANDS` has `"medium"`; `_band_rank` falls through to `"info"`, silently under-reporting memorization risk |
| F3 | **High** | Performance | `generation/composite/_custom.py` | `_generate_deterministic` uses `.iloc[i]` per row; S21 Q6 fix (batch `tolist()` + `isna().to_numpy()`) applied to `pool/_sampler.py` but not here |
| F4 | **High** | Performance | `generation/pool/_sampler.py` | `_match_source_cardinality` output loop uses `.iloc[i]` per row; `sample_bundle` deterministic path has same issue |
| F5 | **High** | Correctness | `plan/_compile.py` | `_build_seed_envelope` duplicates the `when_with_coherent_with` guard already run at `compile_plan` top level; maintenance risk of divergence |
| F6 | **Medium** | Correctness | `generation/composite/_custom.py` | `_build_pools` cache (`self._pools is not None`) ignores `spec.seed`; instance reused across different-seed jobs returns wrong pools silently |
| F7 | **Medium** | Performance | `quality/synth_report.py` | `_gower_min_distances` allocates O(N_out × N_ref) float64 accumulator plus per-column diff arrays; ~400 MB peak at `sample_cap=5000` |
| F8 | **Medium** | Correctness | `validation/_config.py` | `_select_validator` catches bare `Exception`; swallows `MemoryError`, `RecursionError`, `SystemExit` from `PipelineConfig.model_validate` |
| F9 | **Medium** | Design | `quality/policy.py` | All `_check_*` functions hardcode `severity: "fail"`; schema promises `"warn"|"fail"` but no code path produces a warn-severity violation |
| F10 | **Low** | Correctness | `quality/synth_report.py` | `_row_hash_iter` uses `\x1f` separator; values containing `\x1f` can produce cross-column hash collisions |
| F11 | **Low** | Reliability | `generation/composite/_custom.py` | `_check_pool_size` called after `builder.build()` completes; size-overflow config raises after expensive generation work already done |
| F12 | **Nit** | Design | `expressions.py` | `safe_eval` is a one-line `eval()` wrapper with no documented guarantees about length, recursion, or time limits |

---

## Detailed Findings

### F1 · Critical · Determinism — `expressions.py`: `MASK_GLOBALS` retains module-global RNG

**File:** `src/decoy_engine/expressions.py`

**Observation:**
`MASK_GLOBALS` is the default global scope injected into `safe_eval` for `FormulaStrategy`. It contains module-level bindings:

```python
MASK_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "re": _re,
    "str": str, "int": int, "float": float, "bool": bool,
    "len": len, "round": round, "abs": abs, "min": min, "max": max,
    # Module-level bindings: share global Python random state
    "randint": _random.randint,
    "choice": _random.choice,
    "random": _random.random,
}
```

`make_mask_globals(rng)` exists as the correct fix (QA-1 M21, 2026-06-01) and constructs an isolated per-call scope:

```python
def make_mask_globals(rng: _random.Random) -> dict[str, Any]:
    scope = dict(MASK_GLOBALS)
    scope["randint"] = rng.randint
    scope["choice"] = rng.choice
    scope["random"] = rng.random
    return scope
```

**Risk:** Any `FormulaStrategy` call site that passes `MASK_GLOBALS` directly (rather than `make_mask_globals(rng)`) allows formula expressions to call `randint`, `choice`, or `random` against the module-global `random.Random` instance. This instance is not seeded per-pipeline and is affected by import order, prior test execution, parallel jobs, and OS scheduling. Output is therefore non-reproducible even when a deterministic seed is supplied — a direct violation of the determinism contract.

**Action required:**
1. Audit all call sites: `grep -rn 'MASK_GLOBALS' src/` — any site not wrapping with `make_mask_globals(rng)` is a live determinism bug.
2. Deprecate or hide `MASK_GLOBALS` as a public name once all callers are migrated.
3. Add a regression test: run a formula using `randint`/`choice` twice with the same seed via `make_mask_globals`; assert byte-identical output. Run once with `MASK_GLOBALS` directly and assert the test framework detects nondeterminism.

**Note:** The comment in the source (`# module-level bindings retained for backward compatibility`) indicates this is a known transitional state. It must not remain open after any public release that advertises deterministic masking.

---

### F2 · High · Correctness — `quality/synth_report.py`: `_band_for` returns `"moderate"` absent from `_SEVERITY_BANDS`

**File:** `src/decoy_engine/quality/synth_report.py`

**Observation:**

```python
_SEVERITY_BANDS = ("ok", "info", "low", "medium", "high", "critical")

def _band_for(fraction: float) -> str:
    if fraction < _LOW_BAND:    # < 0.50
        return "low"
    if fraction < _HIGH_BAND:   # < 0.90
        return "moderate"       # BUG: not in _SEVERITY_BANDS
    return "high"

def _band_rank(band: str | None) -> int:
    if band in _SEVERITY_BANDS:
        return _SEVERITY_BANDS.index(band)
    return _SEVERITY_BANDS.index("info")  # "moderate" falls through here → rank 1
```

For `0.50 <= fraction_new < 0.90` (moderate memorization), `_band_for` returns `"moderate"`. Since `"moderate"` is absent from `_SEVERITY_BANDS`, `_band_rank` falls through to return `1` (the `"info"` rank). The practical effect is that any synthesis run with 50–90% new rows reports memorization risk as `"info"` rather than `"medium"`. Quality policy gates built on band rank can silently pass pipelines that should be flagged.

**Fix:** Change `_band_for` line to return `"medium"`:

```python
    if fraction < _HIGH_BAND:
        return "medium"   # was "moderate"
```

Alternatively, insert `"moderate"` into `_SEVERITY_BANDS` at the correct position, but this breaks any existing band-rank comparisons that assume rank values for higher severities.

**Test:** Assert `_band_rank(_band_for(0.70)) == _SEVERITY_BANDS.index("medium")`.

---

### F3 · High · Performance — `generation/composite/_custom.py`: `.iloc[i]` per row in `_generate_deterministic`

**File:** `src/decoy_engine/generation/composite/_custom.py`

**Observation:**

```python
def _generate_deterministic(self, spec, source):
    is_null = source.isna()
    for i in range(len(source)):
        if is_null.iloc[i]:           # O(n) pandas index lookup per row
            ...
        canonical = _canonicalize_source(source.iloc[i])  # O(n) per row
```

The S21 Q6 fix was already applied to `pool/_sampler.py:_deterministic` (uses `source.tolist()` + `source.isna().to_numpy()`). The same transformation was not applied here.

**Impact:** For a 100 K-row column, `source.iloc[i]` triggers pandas index resolution 200 K times. On a Series backed by a large DataFrame, each `.iloc` is O(1) but still incurs Python overhead of ~1–2 µs; at 100 K rows this adds ~200 ms per invocation. For composite columns this is multiplied by the number of bundle columns.

**Fix:**

```python
def _generate_deterministic(self, spec, source):
    src_values = source.tolist()              # materialize once
    is_null_arr = source.isna().to_numpy()    # materialize once
    for i in range(len(src_values)):
        if is_null_arr[i]:
            ...
        canonical = _canonicalize_source(src_values[i])
```

---

### F4 · High · Performance — `generation/pool/_sampler.py`: `.iloc[i]` per row in `_match_source_cardinality` and `sample_bundle`

**File:** `src/decoy_engine/generation/pool/_sampler.py`

**Observation:**

`_match_source_cardinality` output loop:
```python
for i in range(n):
    if is_null.iloc[i]:           # .iloc per row
        output.append(pd.NA)
    else:
        output.append(value_map[source.iloc[i]])  # .iloc per row
```

`sample_bundle` deterministic path has the same pattern. Note that `_deterministic` within the same file already uses the S21 Q6 fix correctly, so the fix pattern is present in the codebase — it just was not applied consistently to all loops in this file.

**Fix (same pattern as F3):**

```python
src_values = source.tolist()
is_null_arr = source.isna().to_numpy()
for i in range(n):
    if is_null_arr[i]:
        output.append(pd.NA)
    else:
        output.append(value_map[src_values[i]])
```

---

### F5 · High · Correctness — `plan/_compile.py`: duplicate `when_with_coherent_with` guard

**File:** `src/decoy_engine/plan/_compile.py`

**Observation:** `_check_when_with_coherent_with(config)` is called at the top of `compile_plan`. The same logical check is reimplemented inline inside `_build_seed_envelope`:

```python
# Top of compile_plan:
_check_when_with_coherent_with(config)

# Inside _build_seed_envelope (also called from compile_plan):
if when is not None and coherent_with:
    raise PlanCompileError(code="when_with_coherent_with_unsupported", ...)
```

Two code paths implement the same invariant independently. If the rule is refined (e.g., to allow `when:` with certain `coherent_with` configurations), only one site may be updated, causing the other to either over-raise or under-raise.

**Fix:** Remove the inline check from `_build_seed_envelope` and rely solely on the top-level `_check_when_with_coherent_with` call. If early rejection inside `_build_seed_envelope` is needed for defense-in-depth, add a comment citing the canonical check location.

---

### F6 · Medium · Correctness — `generation/composite/_custom.py`: pool cache ignores `spec.seed`

**File:** `src/decoy_engine/generation/composite/_custom.py`

**Observation:**

```python
def _build_pools(self, spec):
    if self._pools is not None:
        return self._pools  # returns cached result regardless of spec.seed
    ...
    self._pools = ...
    return self._pools
```

If a `CompositeCustom` instance is created once and then `.generate()` is called with specs carrying different seeds (e.g., two pipeline jobs sharing an instance), the second call returns pools built from the first call's seed. The generated output appears valid (no error raised) but is non-deterministic relative to the requested seed.

This is only a live bug if instances are shared across jobs. Whether they are depends on the engine's job-dispatch path, which was not reviewed in this session.

**Fix:** Include the seed in the cache key, or assert that `spec.seed` is immutable after the first `_build_pools` call:

```python
def _build_pools(self, spec):
    if self._pools is not None:
        if self._pool_seed != spec.seed:
            raise RuntimeError(
                "CompositeCustom instance reused with a different seed — "
                "create a new instance per pipeline run."
            )
        return self._pools
    self._pool_seed = spec.seed
    ...
```

---

### F7 · Medium · Performance — `quality/synth_report.py`: `_gower_min_distances` ~400 MB peak allocation

**File:** `src/decoy_engine/quality/synth_report.py`

**Observation:**

```python
dist_sum = np.zeros((n_out, n_ref), dtype=float)   # 200 MB at sample_cap=5000
for col in cols:
    ...
    diff = np.abs(out_vals[:, None] - ref_vals[None, :]) / r  # 200 MB per column
    dist_sum += diff
```

At `sample_cap=5000`, `n_out = n_ref = 5000`. A `(5000, 5000)` float64 array is `5000 * 5000 * 8 = 200 MB`. The `diff` intermediate per column is another `200 MB` (freed after `+=`). Total peak: ~400 MB per `_gower_min_distances` invocation. For a report with multiple DCR calls this can cause OOM on memory-constrained runners.

**Recommended fix:** Process rows in tiles to bound peak memory:

```python
TILE = 500  # tune to target peak < 50 MB
for row_start in range(0, n_out, TILE):
    row_end = min(row_start + TILE, n_out)
    tile_sum = np.zeros((row_end - row_start, n_ref), dtype=float)
    for col in cols:
        ...
        diff = np.abs(out_tile[:, None] - ref_vals[None, :]) / r
        tile_sum += diff
    dist_sum[row_start:row_end] = tile_sum.min(axis=1)
```

This reduces peak from ~400 MB to ~(2 × TILE × n_ref × 8) bytes = ~4 MB at TILE=500.

**Alternative:** Use `scipy.spatial.distance.cdist` with `metric='cityblock'` on normalized columns; it is implemented in C and avoids Python loop overhead, though it still allocates the full matrix.

---

### F8 · Medium · Correctness — `validation/_config.py`: bare `except Exception` in `_select_validator`

**File:** `src/decoy_engine/validation/_config.py`

**Observation:**

```python
def _select_validator(data: dict) -> Any:
    if data.get("version") == 1:
        from decoy_engine.config import PipelineConfig
        try:
            PipelineConfig.model_validate(data)
        except Exception as exc:    # catches MemoryError, RecursionError, SystemExit
            raise PipelineValidationError(str(exc)) from exc
        return None
```

A bare `except Exception` wraps `MemoryError`, `RecursionError`, `KeyboardInterrupt` (Python 3.x: `KeyboardInterrupt` is `BaseException`, so actually not caught here), and `SystemExit` (also `BaseException`). More concretely, a deeply nested config YAML can trigger Python's recursion limit inside Pydantic's validator, raising `RecursionError`. Wrapping it in `PipelineValidationError` makes it look like a user config error rather than an engine resource exhaustion event, and swallows the stack frame that would identify where recursion occurred.

**Fix:** Narrow to `pydantic.ValidationError`:

```python
from pydantic import ValidationError as PydanticValidationError

try:
    PipelineConfig.model_validate(data)
except PydanticValidationError as exc:
    raise PipelineValidationError(str(exc)) from exc
# MemoryError / RecursionError propagate naturally
```

---

### F9 · Medium · Design — `quality/policy.py`: no code path emits `"warn"` severity

**File:** `src/decoy_engine/quality/policy.py`

**Observation:** The module docstring and the quality-policy/v1 schema both describe a `"severity"` field with values `"warn" | "fail"`. Every `violations.append(...)` call across all `_check_*` functions hardcodes `"severity": "fail"`:

```python
violations.append({
    "check": "overall",
    "severity": "fail",   # the only value used anywhere
    ...
})
```

This means operators have no way to express soft thresholds that should surface as warnings without blocking the pipeline. The schema implies this capability exists; the implementation does not deliver it.

**This is a design gap, not a runtime bug.** The concern is:
1. External callers (platform, CLI) may already be testing for `severity == "warn"` based on the documented schema, and will never receive it.
2. Future additions of warn-severity checks require touching every `_check_*` function, since there is no shared helper enforcing the distinction.

**Recommendation:** Either (a) remove `"warn"` from the schema if it is intentionally unimplemented, or (b) introduce a `_violation(check, severity, ...)` helper that enforces `severity in ("warn", "fail")` and migrate all sites to it, then implement warn-threshold parameters.

---

### F10 · Low · Correctness — `quality/synth_report.py`: `_row_hash_iter` separator collision

**File:** `src/decoy_engine/quality/synth_report.py`

**Observation:**

```python
def _row_hash_iter(df):
    for row in df.itertuples(index=False):
        joined = "\x1f".join(str(v) for v in row)
        yield hashlib.sha1(joined.encode(), usedforsecurity=False).digest()
```

The `\x1f` (ASCII Unit Separator) is used as a column delimiter. If a column value contains `\x1f`, the concatenation is ambiguous:
- Row `("a\x1fb", "c")` hashes identically to row `("a", "b\x1fc")`.

This produces false-positive "identical row" matches in privacy metrics (DCR near-duplicate counting).

**Fix:** Encode each field's length before the value (length-prefixed encoding), or use a hash-of-hashes approach:

```python
import struct
def _row_hash_iter(df):
    for row in df.itertuples(index=False):
        h = hashlib.sha1(usedforsecurity=False)
        for v in row:
            encoded = str(v).encode()
            h.update(struct.pack(">I", len(encoded)))
            h.update(encoded)
        yield h.digest()
```

**Practical risk:** Low. Synthetic data produced by Faker/SDV strategies is unlikely to contain `\x1f`. The risk materializes if passthrough columns from real source data contain control characters.

---

### F11 · Low · Reliability — `generation/composite/_custom.py`: pool size validation after `builder.build()`

**File:** `src/decoy_engine/generation/composite/_custom.py`

**Observation:**

```python
def _build_pools(self, spec):
    pools = builder.build()            # expensive: generates all pool values
    _check_pool_size(spec, pools)      # raises if size exceeded — after build
    return pools
```

`_check_pool_size` is a config validation step (checking that the requested pool size fits within configured limits). Calling it after `builder.build()` means a config error (e.g., `pool_size: 10_000_000`) causes the engine to generate 10 M rows before raising. The correct place for this check is before `builder.build()`, ideally at `compile_plan` time.

**Fix:** Move `_check_pool_size(spec, ...)` to validate against `spec` alone (before build), or promote the check into `compile_plan`'s config validation phase.

---

### F12 · Nit · Design — `expressions.py`: `safe_eval` underdocumented

**File:** `src/decoy_engine/expressions.py`

**Observation:**

```python
def safe_eval(expr, globals_, locals_) -> Any:
    return eval(expr, globals_, locals_)  # noqa: S307
```

The function name implies safety guarantees. The implementation provides:
- No arbitrary builtins (controlled by `globals_["__builtins__"] = {}`)
- No import access (if callers set `__builtins__` correctly)

The implementation does **not** provide:
- Length limits on `expr` (a 1 MB expression is accepted)
- Recursion depth limits
- Execution time limits
- Protection against `compile`-time resource exhaustion

A docstring clarifying what "safe" means here would prevent future callers from relying on guarantees that don't exist.

**Recommendation:** Add a one-line docstring:
```python
def safe_eval(expr, globals_, locals_) -> Any:
    """Eval with a restricted namespace; does not bound time, recursion, or expression size."""
    return eval(expr, globals_, locals_)  # noqa: S307
```

---

## Coverage Notes

- `quality/fidelity.py` (14 KB): reviewed, no findings above low severity. TVD implementation correct. Quantile RMSE uses `np.nanquantile` correctly. Freetext length-mean-diff is a sensible proxy. No determinism issues.
- `plan/_compile.py` compile-path beyond F5: the `_build_seed_envelope` silent `backend_type` fallback to `"faker"` for unknown values is a minor design note (no warning emitted on unknown backend type), below the filing threshold for this session.

## Recommended Fix Priority

1. **F2** (one-line fix, high impact on quality reporting correctness) — fix immediately.
2. **F1** (audit all `MASK_GLOBALS` call sites; block pre-release) — audit immediately, fix before any public release.
3. **F3 + F4** (performance, same pattern) — fix together in a single commit; apply the S21 Q6 pattern consistently.
4. **F8** (narrow exception type) — low risk, quick fix.
5. **F5** (remove duplicate guard) — safe to do in any cleanup pass.
6. **F6** (pool cache seed check) — fix before instance sharing is possible in job dispatch.
7. **F7** (tiled Gower allocation) — fix before enabling large-table DCR in production.
8. **F9** (warn severity design gap) — address in policy schema revision.
9. **F10, F11, F12** — low/nit, next maintenance window.
