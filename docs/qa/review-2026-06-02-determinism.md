# QA Review — Determinism, Generation, Plan, Quality

**Date:** 2026-06-02  
**Session:** 3 (continuation of execution review)  
**Reviewer:** QA/Perf Engineer persona  
**Scope:** `src/decoy_engine/determinism/`, `src/decoy_engine/generation/synthesize.py`, `src/decoy_engine/generation/pool/` (structure), `src/decoy_engine/plan/`, `src/decoy_engine/quality/fidelity.py`, `src/decoy_engine/quality/policy.py`  
**Prior sessions:** `qa/review-2026-06-02-engine` (engine-core, cli), `qa/review-2026-06-02-execution` (execution adapter, runner, transforms, graph, context, sdk, s3)

---

## Summary

| ID  | Severity | File | Finding |
|-----|----------|------|---------|
| F1  | LOW      | `determinism/_derive.py` | `DeriveContext.derive_source()` re-takes `namespace` as a parameter — silent divergence trap |
| F2  | MEDIUM   | `generation/synthesize.py` | `_FAKER_CALL_LOCK` held for entire N-row loop — full serialisation of concurrent generation |
| F3  | LOW      | `generation/synthesize.py` | `_formula()` accesses `ColumnGenerator._eval_formula_inline` across module boundary (private V1 coupling) |
| F4  | NIT      | `plan/_compile.py` | `_build_seed_envelope()` silent `faker/stub-0` fallback for unrecognised providers that escape `check_unknown_provider` |
| F5  | NIT      | `quality/policy.py` | Per-violation `severity` field recorded but not used by `_verdict_for()` |

---

## Findings

### F1 · LOW · `determinism/_derive.py` — `DeriveContext.derive_source()` silent divergence trap

**Location:** `_derive.py`, `DeriveContext.derive_source()` method.

`DeriveContext` is pre-computed for a fixed `(seed, namespace)` pair. The HMAC key stored as `_hmac_key` is already bound to that namespace via HKDF. But `derive_source()` requires the caller to re-supply `namespace`:

```python
def derive_source(self, namespace: str, source: bytes) -> bytes:
    namespace_bytes = namespace.encode("utf-8")
    hmac_input = (
        bytes([SEED_PROTOCOL_VERSION])
        + len(namespace_bytes).to_bytes(4, "big")
        + namespace_bytes
        ...
    )
    return hmac.new(self._hmac_key, hmac_input, hashlib.sha256).digest()
```

If the caller passes a different `namespace` than was used in `for_column()`, the output silently diverges from what `derive(seed, namespace, source)` would return for the same inputs — the HKDF key is bound to namespace A, but the HMAC body encodes namespace B, producing a value that no valid `derive()` call can reproduce. The docstring warns about this but there is no runtime assertion.

**Root cause:** The `namespace` argument in the HMAC body is redundant for callers who always pass the same value — it only exists to make the output byte-identical to the scalar `derive()`. Storing `namespace` in the dataclass and using it internally would eliminate the trap entirely:

```python
@dataclass(frozen=True)
class DeriveContext:
    _hmac_key: bytes
    _namespace: str   # store it; callers cannot diverge

    def derive_source(self, source: bytes) -> bytes:  # namespace removed from signature
        namespace_bytes = self._namespace.encode("utf-8")
        ...
```

This is a source-compatible change if callers currently always pass the same namespace they used at construction, which is the documented contract. Removing the arg eliminates the trap without breaking correct callers.

**Risk:** Low — only callers that violate the documented contract are affected, and such violations produce incorrect determinism outputs rather than exceptions, making them hard to detect in tests.

---

### F2 · MEDIUM · `generation/synthesize.py` — `_FAKER_CALL_LOCK` held for entire N-row loop

**Location:** `synthesize.py`, `_faker()`, the `with _FAKER_CALL_LOCK:` block.

The lock serialises the `seed_instance + provider_func` pair across threads (QA-7 F1 fix), which is correct. But the lock is held for the **entire** `range(n)` iteration:

```python
with _FAKER_CALL_LOCK:
    if pre_seed is not None:
        faker_inst.seed_instance(pre_seed)
    for i in range(n):
        row_seed = col_seed + i
        faker_inst.seed_instance(row_seed)
        out.append(provider_func(**faker_kwargs))
```

For a table with 1 000 000 rows and a faker column, the lock is held for the full generation duration of that column — potentially 5–30 seconds on a modern core. Any other thread that calls `_faker()` (for a different column, a different table, a different concurrent job) must wait for the entire first column to finish. This completely serialises concurrent generation across threads even when the Faker instances involved have no shared state (locale-specific instances use `make_faker(locale)` and never touch `_DEFAULT_FAKER`).

**Why it's scoped too wide:** The QA-7 F1 fix added the lock, and the QA-7 C1 carry moved the pre-seed inside the lock — both correct. But neither bounds the lock to the minimum critical section. The critical section is actually just `seed_instance(row_seed) + provider_func()` — two lines per row, not the entire loop body.

However: re-acquiring and releasing the lock `n` times per column (`n` × `_FAKER_CALL_LOCK.acquire + release`) adds Python GIL overhead on top of the existing Faker overhead. The module docstring correctly scopes the fix: "V2.1 throughput optimization: replace the shared cached instance with a per-call fresh Faker to remove the lock entirely." The proper fix is per-call fresh Faker, not a tighter lock.

**Impact:** Any deployment that runs concurrent generation jobs sharing a process will see linear serialisation of faker column generation. Mask-only jobs (no generate) are not affected.

---

### F3 · LOW · `generation/synthesize.py` — `_formula()` accesses private V1 method across module boundary

**Location:** `synthesize.py`, `_formula()`.

```python
from decoy_engine.generators.columns import ColumnGenerator
cg = ColumnGenerator(seed=seed, derive_key=derive_key)
series = cg._eval_formula_inline(n, formula, col.get("name", "unnamed_column"), col)
```

Similarly, `_reference()` calls `cg._apply_cardinality_bounds(...)` — another private method.

The module docstring justifies this as "Reading B: pragmatic guaranteed parity" — these are deferred until V1 removal. The risk is:

1. A V1 refactor that renames or removes `_eval_formula_inline` will raise `AttributeError` at runtime in V2 generation, not at import time.
2. The coupling is invisible from the V2 public surface — a maintainer adding a new formula feature to V1 might not realise it must be mirrored in `synthesize.py`.

The comment trail is clear (`# DELEGATED to V1`) so the intent is known. The finding is that there is no `TODO` issue tracking the V2-native rewrite, and no `DeprecationWarning` on the V1 side that would surface if the method is removed.

---

### F4 · NIT · `plan/_compile.py` — silent `faker/stub-0` fallback for unrecognised providers

**Location:** `_compile.py`, `_build_seed_envelope()`, the `else` branch for `reg_caps is None` after a `_ProviderError`.

```python
except _ProviderError:
    # check_unknown_provider should have caught this earlier
    # in compile_plan; defensively fall back to the legacy
    # behavior here so a bug in the check-runner doesn't
    # crash the planner.
    reg_caps = None
# ...
else:
    backend_type = col_entry.get("backend_type", "faker")
    backend_version = col_entry.get("backend_version", "stub-0")
```

If `check_unknown_provider` erroneously passes an unrecognised provider AND the provider lookup raises `_ProviderError`, the column is silently stamped as `backend_type=faker, backend_version=stub-0`. Two columns with *different* unrecognised providers would receive identical stamps, potentially producing identical `pipeline_config_hash` for semantically different plans.

This is a defence-in-depth gap: the primary guard (`check_unknown_provider`) should be sufficient, but the fallback masks any bug in that guard. A safer fallback would be a compile-time `PlanCompileError` rather than a silent stamp.

---

### F5 · NIT · `quality/policy.py` — per-violation `severity` recorded but unused

**Location:** `policy.py`, `_verdict_for()`, all `_check_*` functions.

Violation dicts carry `"severity": "fail"` but `_verdict_for()` ignores the per-violation severity and uses only `mode`:

```python
def _verdict_for(mode: str, violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "pass"
    if mode == "report":
        return "pass"
    if mode == "warn":
        return "warn"
    return "fail"
```

A `"severity": "warn"` violation in `mode=fail` produces `verdict=fail` — not necessarily wrong, but the `severity` field in violation dicts has no effect. Either (a) distinguish warn vs fail violations to produce graduated verdicts, or (b) remove the `severity` field from violation dicts to avoid misleading consumers.

---

## What's Good

**`determinism/_derive.py`:**
- SEED_PROTOCOL_VERSION = 4 with well-documented, justified version bumps (each bump traces to a concrete behavioural change + pre-GA confirmation).
- `DeriveContext` correctly pre-computes the HKDF key once per `(seed, namespace)` pair (QA-10 F4 fix), eliminating per-row HKDF cost at the column level.
- Length-prefixed HMAC input makes the concatenation injective — `("abc", b"def")` and `("abcd", b"ef")` produce different outputs.
- `derive_index` modulo-bias bound is computed and documented (`pool_size / 2**64`), with a hard guard at `2**56`.
- Separate `pool_size_invalid` code for `pool_size < 1` vs `pool_size_overflow` for `pool_size > 2**56` (QA-7 F11).

**`determinism/_hkdf.py`:**
- RFC 5869 fully compliant; empty-salt guard (QA-10 F12) correctly rejects the all-zero fallback.
- Stdlib-only implementation avoids PyCA dependency, consistent with the engine's anti-PyCA design choice documented in `transforms/fpe.py`.
- Length upper bound (`255 * 32`) matches the RFC 5869 §2.3 maximum.

**`generation/synthesize.py`:**
- `_FAKER_CALL_LOCK` and `_DEFAULT_FAKER_LOCK` correctly handle the double-checked locking pattern for the singleton (QA-7 F1 + C1).
- `_apply_null_probability()` reuses a single `random.Random()` instance and calls `rng.seed()` in-place per row — avoids 624-word Mersenne Twister re-initialisation cost per row (session2 F3 perf fix).
- `_reference()` preserves insertion-order uniqueness via explicit `seen: set` + `ref_vals: list` pattern, matching V1 `dropna().unique()` byte-for-byte.
- `_topo_sort()` uses DFS over the declared `deps` dict — Python 3.7+ insertion-order guarantees deterministic iteration for a given config input.

**`plan/_compile.py`:**
- `_hash_config()` explicitly excludes `sources` and `targets` from the semantic hash — correct (data binding ≠ masking semantics).
- `json.dumps` uses `sort_keys=True, ensure_ascii=True, separators=(",", ":"`) — byte-stable across Python runtimes.
- No more `default=str` in `json.dumps` — non-JSON-native types now raise `TypeError` at plan-compile time rather than silently coercing (QA walks/generators F9).
- `_normalize_job_seed()` guards against `bool` and `float` seed values (QA-3 F1) — `seed: yes` / `seed: 1.5` both raise `seed_not_numeric`.
- `_build_relationships()` iterates `sorted(grouped.items())` — byte-stable ordering for `PlanRelationship` tuples.
- S13-rebaseline-P1 4-tuple key `(parent_table, parent_cols, child_table, child_cols)` in orphan_policy_lookup — per-(parent, child) policies are honoured.

**`quality/policy.py`:**
- D5a corrected defaults match what `compute_fidelity` actually reports (not an aspirational future comparator) — calibrated against value-identity TVD reality.
- D5b `_DEFAULT_SHAPE_STRATEGY_EXPECTATIONS` correctly reflects that `hash` and `faker` score high on shape (they preserve frequency distributions) even though they score low on value identity.
- `_normalize_column_overrides()` accepts both dict and list-of-dict shapes — defensively handles both config formats.
- Module is pure: takes dicts, returns dicts, never raises on bad input.

**`quality/fidelity.py`:**
- TVD formula correct: `0.5 * sum |P(x) - Q(x)|`; TVD-as-complement (`1 - TVD_normalized`) is bounded in `[0, 1]` and symmetric.
- `_numeric_similarity()` normalises RMSE by `max(src_range, 1.0)` — avoids division-by-zero for constant-valued columns.
- `_freetext_similarity()` normalises by `max(src_max, out_max, 1)` — accounts for wholesale length collapse (hash output fixed-width).
- `_SCORE_PRECISION = 6` pins float round-trip determinism across BLAS/numpy versions.

---

## Suggested Tests

```python
# F1: DeriveContext namespace mismatch produces wrong output
ctx = DeriveContext.for_column(seed=b'\x00' * 8, namespace='ns_A')
result_correct  = ctx.derive_source('ns_A', b'source')
result_diverged = ctx.derive_source('ns_B', b'source')  # wrong namespace!
scalar = derive(b'\x00' * 8, 'ns_A', b'source')
assert result_correct == scalar
assert result_diverged != scalar  # currently produces no assertion error

# F2: concurrent faker column generation time
import concurrent.futures, time
def gen():
    return synthesize._faker({'name': 'email', 'faker_type': 'email'}, 100_000, 0)
start = time.perf_counter()
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
    futs = [ex.submit(gen) for _ in range(4)]
    [f.result() for f in futs]
elapsed = time.perf_counter() - start
# With the current lock: ~4x sequential time
# With per-call fresh Faker: ~1x sequential time

# F4: unknown provider escaping check_unknown_provider gets bad stamp
# (inject a provider_error into check_unknown_provider to verify fallback)
```
