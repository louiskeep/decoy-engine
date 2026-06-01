# QA Review — execution / quality / determinism / fpe / formula

**Date:** 2026-06-01  
**Reviewer:** QA agent (Claude Sonnet 4.6)  
**Branch:** `qa/review-2026-06-01-execution-quality-determinism`  
**Base SHA:** `d1b5b92` (main)

## Scope

This session covers the six module clusters that had no prior QA
branch coverage as of 2026-06-01:

| Module cluster | Files |
|---|---|
| `execution/` | `_pandas_adapter.py`, `_transforms.py`, `_runner.py`, `polars/` |
| `quality/` | `fidelity.py`, `snapshot.py`, `policy.py`, `synth_report.py` |
| `determinism/` | `_derive.py`, `_hkdf.py` |
| `transforms/` | `fpe.py`, `formula.py` |
| Root | `expressions.py` |

Previously reviewed and intentionally skipped:
`storm/postmask`, `date_shift`, `relationships/`, `context.py`,
`connectors/`, `synthesize`, `profile_source`, `text_redact`,
`when_gate`, `nested`, `composites`, `distribution_behavior`,
`providers_v2`, `persistence/`, `scheduler/`, platform API layer.

---

## 1. Summary

The execution and quality modules are structurally sound and clearly
authored with determinism in mind, but three live defects undermine
that goal.  The most urgent issue is in `FormulaStrategy`: the factory
function `make_mask_globals(rng)` that was written to fix unseeded RNG
was never wired into the strategy — the strategy still uses the
module-global `MASK_GLOBALS` with unseeded `random.*` bindings,
making every formula column non-deterministic across runs.  The second
most urgent is in `fpe.py`: the single-character Feistel path computes
a different HMAC shift per input character, which breaks the bijection
property that FPE promises — two distinct source characters can map to
the same output character.  Third, `synth_report.py` uses bare
`hashlib.sha1()` which raises on FIPS-hardened hosts (standard in
healthcare deployments) without the `usedforsecurity=False` flag.

---

## 2. Findings

### F1 — CRITICAL · Correctness / Determinism
**`expressions.py:MASK_GLOBALS` / `transforms/formula.py:FormulaStrategy.apply()`**

`FormulaStrategy.apply()` calls `safe_eval(expr, MASK_GLOBALS, {"value": v})`
directly.  `MASK_GLOBALS` binds `randint`, `choice`, and `random` to
Python's module-global `random` instance — unseeded, shared across every
formula column in the job.  `make_mask_globals(rng)` was written (and
documented in `QA-1 M21`) to fix this, but it is never called by
`FormulaStrategy`.

**Impact:**
- Any formula that references `randint`, `choice`, or `random` will
  produce different output on every run.  Two runs with the same seed,
  config, and source data are NOT byte-identical.  This violates the
  engine's core determinism guarantee.
- Two formula columns in the same job share state: column B's output
  depends on how many RNG calls column A made, i.e., on execution
  order and input data shape.
- The bug is silent: no error, no warning, just wrong output.

**Fix:** Wire `make_mask_globals` into `FormulaStrategy` using a
per-column seed derived from the job seed + column namespace:

```python
# transforms/formula.py
from decoy_engine.expressions import make_mask_globals
import random as _random

class FormulaStrategy(BaseMaskingStrategy):
    def apply(self, column: pd.Series, rule: dict[str, Any]) -> pd.Series:
        expr = rule.get("formula", "")
        if not expr:
            ...
            return column.copy()
        # Derive a per-column, per-job seed so output is deterministic
        # and two formula columns don't share state.
        col_name = rule.get("column", "unnamed")
        formula_seed = hash((self.seed, col_name, "formula")) & 0xFFFFFFFF
        rng = _random.Random(formula_seed)
        scope = make_mask_globals(rng)
        return column.apply(lambda v: v if pd.isna(v) else self._eval(expr, v, scope))

    def _eval(self, expr: str, value: Any, scope: dict) -> Any:
        return safe_eval(expr, scope, {"value": value})
```

**Verify:** Run the same formula job twice with the same config; diff the
outputs.  Then run with `randint(1, 100)` expression and confirm output
is identical across re-runs.

---

### F2 — HIGH · Correctness
**`transforms/fpe.py:FPEStrategy._fpe_pure()` — single-character path is not a bijection**

For length-1 strings, `_fpe_pure` computes a per-character HMAC:

```python
# current (line ~170)
if n == 1:
    idx = charset.index(s[0])
    msg = b"fpe-single\xff" + tweak + b"\xff" + s.encode()  # <-- s included
    F = int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big")
    return charset[(idx + F) % len(charset)]
```

Because `s.encode()` is part of the HMAC input, different source
characters produce different values of `F`.  The output is
`charset[(idx + F_i) % r]` where `F_i` is distinct per character `i`.
This is NOT a bijection: if `(0 + F_0) % r == (1 + F_1) % r` (which
happens with probability approximately `1/r` for random keys), two
distinct source characters map to the same output character.

**Impact:**
- FPE is documented as "a bijection regardless of the round function."
  Single-character values violate this.  A PAN column where all PANs
  share the same first digit but differ in the single check character
  (the FPE domain is one char) would collapse to a single output value,
  leaking structure.
- Format-preserving deduplication: two source rows with different
  single-char values can produce identical masked output, violating
  referential integrity for FK columns that consist of a single
  character.

**Fix:** Use the key + tweak alone to derive `F` (not the source value),
yielding a uniform rotation of the entire alphabet, which is trivially
bijective:

```python
if n == 1:
    idx = charset.index(s[0])
    # F depends on (key, tweak) only, so it is the same for every
    # character in the charset -> the function is a rotation -> bijection.
    msg = b"fpe-single\xff" + tweak
    F = int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big")
    return charset[(idx + F) % len(charset)]
```

**Verify:**
```python
from decoy_engine.transforms.fpe import FPEStrategy
strategy = FPEStrategy(seed=42)
charset = "0123456789"
key = b"\x00" * 32
tweak = b"col"
# All 10 single-digit outputs must be distinct
outputs = [strategy._fpe_pure(d, key, charset, tweak, False) for d in charset]
assert len(set(outputs)) == 10, f"collision: {outputs}"
```

---

### F3 — HIGH · Reliability
**`quality/synth_report.py:_row_hash_iter()` — bare `hashlib.sha1()` fails on FIPS hosts**

```python
yield hashlib.sha1(composite.encode("utf-8")).hexdigest()  # line ~270
```

Python 3.9+ `hashlib` raises `ValueError: [digital envelope routines]
unsupported` when SHA-1 is called without `usedforsecurity=False` on
OpenSSL builds with FIPS mode enabled.  Healthcare and federal
environments (the application's stated target, given NPI/ICD-10/NDC
providers) commonly run FIPS-hardened hosts.  The failure happens at
runtime during quality metric computation, not at import time, so CI
will not catch it unless run in a FIPS environment.

**Impact:**  `compute_new_row_synthesis()` raises on any FIPS host,
making the entire privacy metrics block unavailable without any
informative error surfaced to the operator.

**Fix:**
```python
yield hashlib.sha1(composite.encode("utf-8"), usedforsecurity=False).hexdigest()
```

`usedforsecurity=False` is harmless on non-FIPS builds (Python 3.9+)
and suppresses the FIPS rejection on compliant builds.  The docstring's
rationale (SHA-1 as a row fingerprint, not a security primitive) is
exactly the use case this flag was designed for.

---

### F4 — HIGH · Performance
**`determinism/_derive.py:derive()` — HKDF key recomputed every row**

```python
def derive(seed: bytes, namespace: str, source: bytes) -> bytes:
    hmac_key = hkdf_sha256(ikm=seed, salt=_SALT, info=namespace_bytes, length=32)
    # ... then HMAC(hmac_key, hmac_input)
```

`hkdf_sha256` is `hkdf_expand(hkdf_extract(salt, seed), namespace, 32)`,
which is two HMAC-SHA256 invocations.  `hmac_key` depends only on
`(seed, namespace)` — both of which are constant for every row in a
column.  For a 1 M-row column, `hkdf_sha256` is called 1 M times and
returns the same 32 bytes each time.  The per-row work that actually
varies is only the final `hmac.new(hmac_key, per_row_input).digest()`.

**Cost estimate:** HMAC-SHA256 processes roughly 300 MB/s on a modern
core.  Each call processes ~80 bytes.  Two wasted HMAC calls per row
at 1 M rows ≈ 160 MB of wasted HMAC work ≈ 0.5 s wasted per column,
per run.  A 50-column masked table wastes ~25 s.  Profile with:
```
python -m cProfile -s cumulative -c \
  "from decoy_engine.determinism._derive import derive; \
   s=b'\\x00'*8; [derive(s,'ns',i.to_bytes(4,'big')) for i in range(500_000)]"
```
`hkdf_sha256` will dominate the cumulative time.

**Fix:** Expose a `DeriveContext` that pre-computes the HKDF key once
and exposes a `derive_source(source)` method for per-row calls:

```python
@dataclass(frozen=True)
class DeriveContext:
    """Pre-computed per-(seed, namespace) state. Amortises HKDF cost."""
    _hmac_key: bytes  # private

    @classmethod
    def for_column(cls, seed: bytes, namespace: str) -> "DeriveContext":
        if len(seed) != _SEED_LENGTH:
            raise DeterminismError(code="seed_wrong_length", ...)
        if not namespace:
            raise DeterminismError(code="namespace_empty", ...)
        key = hkdf_sha256(ikm=seed, salt=_SALT,
                          info=namespace.encode("utf-8"), length=32)
        return cls(_hmac_key=key)

    def derive_source(self, namespace: str, source: bytes) -> bytes:
        namespace_bytes = namespace.encode("utf-8")
        hmac_input = (
            bytes([SEED_PROTOCOL_VERSION])
            + len(namespace_bytes).to_bytes(4, "big") + namespace_bytes
            + len(source).to_bytes(4, "big") + source
        )
        return hmac.new(self._hmac_key, hmac_input, hashlib.sha256).digest()
```

The existing `derive()` scalar API stays unchanged for compatibility.
Strategy adapters that process a column call `DeriveContext.for_column()`
once and then `ctx.derive_source(namespace, row_bytes)` per row.

---

### F5 — MEDIUM · Data
**`quality/fidelity.py` — TVD underestimates divergence when values migrate between top-K and `other_count`**

In `_categorical_similarity` (and identically in `_joint_similarity`):

```python
src_probs = {item["value"]: int(item["count"]) / src_total for item in src_items}
out_probs = {item["value"]: int(item["count"]) / out_total for item in out_items}
keys = set(src_probs) | set(out_probs)
tvd = sum(abs(src_probs.get(k, 0.0) - out_probs.get(k, 0.0)) for k in keys)
tvd += abs((src_other / src_total) - (out_other / out_total))
```

If value `"X"` appears in the source's `top_values` (probability 0.03)
but was pushed into the output's `other_count` (because the output's
top-K slots are filled with different values), then `out_probs.get("X",
0.0) == 0.0`.  The TVD treats the output's probability of `"X"` as zero,
but it is actually `(count_X / out_total) > 0` — that probability was
absorbed into `other_count` without being individually tracked.

The effect is that `src_other_prob - out_other_prob` in the last line
partially compensates, but not fully, because the `other_count`
bucket bundles many values.  The TVD is systematically underestimated
whenever the output's top-K differs substantially from the source's.

**Impact:** The fidelity score is optimistic for hash / faker / redact
strategies precisely when accuracy matters most — the strategies that
completely replace value sets push all source values into `other_count`
and pull entirely new values into `top_K`, causing maximum TVD
under-reporting.

**Recommended fix:**  In snapshots consumed by fidelity scoring, the
`top_values` list should represent the top-K of the UNION of both
snapshots, not each independently.  This is a data model change that
belongs in the snapshot layer (D1a spec amendment) rather than in
fidelity.py.  Short-term mitigation: document the limitation in
`compute_fidelity`'s docstring and note it in the QualityReport metadata.

---

### F6 — MEDIUM · Data
**`quality/snapshot.py:_numeric_stats()` — infinite values silently dropped; `non_null_count` overstates effective population**

```python
# In _column_snapshot:
non_null = series.dropna()
non_null_count = len(non_null)        # counts inf/-inf as non-null

# In _numeric_stats:
finite = arr[np.isfinite(arr)]        # silently drops inf/-inf
if finite.size == 0:
    return {"min": None, ...}         # but doesn't flag this
```

A column with 1 M rows and 1 `+inf` reports `non_null_count = 1_000_000`
but statistics computed over `finite.size = 999_999`.  Downstream
fidelity comparisons subtract N from M using the `non_null_count` field
for weighting — the off-by-one is negligible at this scale, but the
discrepancy is invisible and violates the snapshot's documented
"deterministic + JSON-serializable" contract (the effective population
used for stats should be reported).

**Fix:**
```python
# In _numeric_stats, return the actual finite count:
return {
    "min": _round(lo),
    ...,
    "effective_count": int(finite.size),   # add this field
    "infinite_count": int(arr.size - finite.size),  # and this
}
```

Alternatively, count non-null and non-infinite at the `_column_snapshot`
level and store as `non_null_count`.  Either way, the population used
for stats and the count reported must agree.

---

### F7 — MEDIUM · Design
**`quality/policy.py:_verdict_for()` — per-violation `severity` field is ignored; any violation in `fail` mode returns `"fail"`**

```python
def _verdict_for(mode: str, violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "pass"
    if mode == "report":
        return "pass"
    if mode == "warn":
        return "warn"       # ignores severity
    return "fail"           # ignores severity
```

Each violation dict carries `"severity": "warn" | "fail"` (see
`_check_diagnostic`, schema documentation).  The verdict calculation
ignores this field entirely.  In `fail` mode, a single `severity=
"warn"` violation returns `verdict="fail"`, which would gate the
platform job — more aggressive than the operator intended.

Today all check functions only emit `severity="fail"`, so the impact
is latent.  But the schema explicitly reserves `"warn"`, suggesting
warn-severity violations are planned, and any future addition would
silently gate jobs in fail mode.

**Fix:**
```python
def _verdict_for(mode: str, violations: list[dict[str, Any]]) -> str:
    if not violations or mode == "report":
        return "pass"
    has_fail = any(v.get("severity") == "fail" for v in violations)
    if mode == "warn":
        return "warn"   # any violation -> warn, regardless of severity
    # fail mode: only promote to fail when at least one fail-severity
    # violation exists; warn-only violations produce warn.
    return "fail" if has_fail else "warn"
```

---

### F8 — MEDIUM · Correctness
**`execution/_transforms.py:_apply_filter()` — rejects valid `pd.BooleanDtype` (nullable boolean) Series**

```python
if not isinstance(mask, pd.Series) or mask.dtype != bool:
    raise TransformError(code="filter_expression_not_boolean", ...)
```

`pd.eval()` with `engine="numexpr"` can return a `pd.Series` with
`dtype=pd.BooleanDtype()` (nullable boolean, aka `"boolean"`) when the
operands include pandas nullable-integer or nullable-boolean columns.
`BooleanDtype() != bool` is `True`, so valid filter expressions on
nullable columns always raise `filter_expression_not_boolean`.  This
makes filter transforms unusable on any table sourced from a nullable
Arrow schema (which is the default Arrow→pandas conversion).

**Fix:**
```python
def _is_boolean_series(mask: object) -> bool:
    return (
        isinstance(mask, pd.Series)
        and pd.api.types.is_bool_dtype(mask)
    )

if not _is_boolean_series(mask):
    raise TransformError(...)
```

`pd.api.types.is_bool_dtype` accepts `bool`, `np.bool_`, and
`pd.BooleanDtype`.

---

### F9 — LOW · Performance
**`execution/_runner.py:_kahn_sorted()` — O(n²) scan per topological step**

```python
while len(placed) < len(keys):
    ready = sorted(k for k in keys if k not in placed_set and deps.get(k, set()) <= placed_set)
```

At each step, this scans all `n` keys.  For a topological sort of `n`
nodes, the total work is O(n²).  For realistic masking jobs (50–200
columns), this is immeasurable.  For very wide tables (1 000+ columns,
as sometimes occur in healthcare claims data), it becomes noticeable:
at n=1 000 this is 1 M key comparisons, each with a set-subset test
that walks the deps set.

**Fix:** Standard Kahn: maintain an in-degree counter and a `heapq`
ready-queue updated incrementally as nodes are placed.
This reduces to O((n + e) log n).

---

### F10 — LOW · Performance
**`quality/synth_report.py:compute_dcr()` — 200 MB matrix allocation at default `sample_cap=5000`**

```python
dist_sum = np.zeros((n_out, n_ref), dtype=float)  # shape=(5000,5000)
```

The distance accumulator is allocated as a full `(n_out × n_ref)` float64
matrix.  At `sample_cap=5000` this is `5000 × 5000 × 8 = 200 MB`.
A per-column temporary `diff` or `ne` array of the same shape is added
each iteration, peaking at 400 MB for the first column in the loop.

This is documented as a known tradeoff (the docstring calls it out).
However, the `sample_cap` default is not validated against available
memory and there is no warning when the allocation is large.  A
worker with 512 MB RAM allocated by the platform scheduler will OOM
silently.

**Recommended:**
1. Add a check / log-warn when `n_out * n_ref * 8 > 100_000_000` (100 MB).
2. For future-tier: use block-tiled processing so peak memory is bounded
   by `tile_size²` rather than `sample_cap²`.

---

### F11 — LOW · Data
**`quality/snapshot.py:_joint_snapshot()` — `pd.crosstab` materializes full cross-product before top-K cap**

```python
ct = pd.crosstab(a_vals, b_vals)
```

`pd.crosstab` computes the full cross-product contingency table.  For
two columns each with 1 000 distinct values (e.g., ZIP code × ICD-10
code), the table is 1 M cells.  At `dtype=int64` that is 8 MB per
joint pair.  For a 50-pair joint specification, 400 MB of contingency
tables are materialized before any top-K trimming.

The snapshot already caps at `contingency_top_k=25`, but the cap is
applied after full materialization.  The docstring notes
"Snapshot is the only thing that crosses the boundary" — this is the
place to save that memory.

**Fix:** Use `value_counts()` on the composite key tuple rather than
`pd.crosstab`, then take the top-K:
```python
composite = sub.apply(tuple, axis=1)
cell_counts = composite.value_counts()
# top-K + other from value_counts, no full cross-product
```

---

### F12 — NIT · Design
**`determinism/_hkdf.py:hkdf_extract()` — empty-salt warning absent**

RFC 5869 §2.2 states that an empty salt is treated as `HashLen` zero bytes.  
`hkdf_extract(b"", ikm)` silently produces `HMAC-SHA256(b"\x00"*32, ikm)` — a
weaker salt than any application-specific context string.  Callers
passing an empty salt by accident (e.g., if a salt generation step
fails quietly) get valid-looking output without any indication of the
degradation.  No action required for the current `_SALT` in `_derive.py`
(it is a non-empty constant), but a `ValueError` on empty salt would
protect future callers.

---

### F13 — NIT · Correctness
**`quality/synth_report.py:assemble_synth_report()` — attacks disclaimer hardcoded regardless of `attacks` content**

```python
"disclaimers": [
    ...,
    "Attack-based metrics (Membership Inference, shadow-model) "
    "are OFF by default and only run when the operator "
    "explicitly opts in...",
]
```

When `attacks` is not `None` (i.e., attacks were actually run), the
disclaimer still says they are "OFF by default" — which is true in
general but misleading in the specific report where they ran.  An
operator reading the disclaimer section of a report that includes
actual attack results will be confused.

**Fix:** Emit the disclaimer conditionally:
```python
if attacks is None or not (attacks or {}).get("available"):
    disclaimer = "Attack-based metrics are OFF by default..."
else:
    disclaimer = "Attack-based metrics were run for this job..."
```

---

### F14 — NIT · Security
**`expressions.py:safe_eval()` — `__builtins__: {}` does not prevent all escape vectors**

The existing security note in `_transforms.py` documents the `@var`
scope-walk escape.  For the CPython `eval()` path in `safe_eval()`,
a separate class-hierarchy escape exists even with `__builtins__: {}`:

```python
safe_eval("().__class__.__bases__[0].__subclasses__()", MASK_GLOBALS, {})
# returns all object subclasses — exposes file I/O, socket, etc.
```

This is the standard Python sandbox escape.  Blocking `__class__`,
`__bases__`, `__subclasses__`, `__globals__`, `__builtins__` in the
globals dict is not sufficient because attribute lookups on built-in
types bypass the globals scope.

**Assessment:** Per the audit map, `eval()` is an acknowledged risk
(`sql_run` and formula paths are both called out).  The current
mitigation (builtins block + noqa:S307) is appropriate if formulas
are operator-authored YAML, not customer-supplied input.  The finding
is a NIT here because the threat model is presumably admin-only.
If the formula surface is ever exposed to tenants or users directly,
this becomes a Critical security finding.  Recommend adding a comment
stating the assumed threat boundary explicitly.

---

## 3. Performance Notes

**Bottleneck classification:**

| Module | Bottleneck | Note |
|---|---|---|
| `determinism._derive` | CPU (HMAC) | HKDF recomputed per row (F4) |
| `transforms.fpe` | CPU (HMAC) | 8 rounds × 1 HMAC each = 8 HMACs per value; already documented |
| `quality.synth_report.compute_dcr` | Memory | 200 MB matrix at default cap (F10) |
| `quality.synth_report.compute_new_row_synthesis` | Memory + CPU | SHA-1 row hashing via `itertuples`; scales with source rows |
| `quality.snapshot._joint_snapshot` | Memory | Full crosstab before top-K (F11) |
| `execution._runner._kahn_sorted` | CPU | O(n²) for wide schemas (F9) |

**What to profile:**

```bash
# F4 — HKDF per-row cost:
python -m cProfile -s cumulative -c \
  "from decoy_engine.determinism._derive import derive; \
   s=b'\\x00'*8; [derive(s,'patients.ssn',i.to_bytes(4,'big')) for i in range(500_000)]"

# F10 — DCR memory:
python -c "
import tracemalloc, pandas as pd, numpy as np
tracemalloc.start()
src = pd.DataFrame(np.random.randn(5000, 20), columns=[f'c{i}' for i in range(20)])
out = pd.DataFrame(np.random.randn(5000, 20), columns=[f'c{i}' for i in range(20)])
from decoy_engine.quality.synth_report import compute_dcr
compute_dcr(src, out)
current, peak = tracemalloc.get_traced_memory()
print(f'Peak: {peak/1e6:.1f} MB')
"

# F9 — kahn O(n²) timing:
python -c "
import time
from decoy_engine.execution._runner import _kahn_sorted
# build 500-node linear chain (worst-case for _kahn_sorted)
n = 500
by_key = {(str(i),(str(i),)): None for i in range(n)}
deps = {(str(i),(str(i),)): {(str(i-1),(str(i-1),))} for i in range(1,n)}
t0 = time.perf_counter()
_kahn_sorted(by_key, deps)
print(f'{(time.perf_counter()-t0)*1000:.1f} ms')
"
```

---

## 4. Suggested Tests

### For F1 (FormulaStrategy unseeded RNG)
```python
def test_formula_rng_deterministic():
    """Same seed + same data -> byte-identical output across runs."""
    strategy = FormulaStrategy(seed=42)
    rule = {"column": "age", "formula": "randint(1, 100)"}
    col = pd.Series([1, 2, 3, 4, 5])
    out1 = strategy.apply(col, rule)
    out2 = strategy.apply(col, rule)
    pd.testing.assert_series_equal(out1, out2)

def test_formula_rng_isolated_between_columns():
    """Column A's formula output does not depend on column B running first."""
    strategy = FormulaStrategy(seed=42)
    rule_a = {"column": "col_a", "formula": "randint(1, 100)"}
    rule_b = {"column": "col_b", "formula": "randint(1, 100)"}
    col = pd.Series([1, 2, 3])
    out_a_first = strategy.apply(col, rule_a)   # A before B
    strategy.apply(col, rule_b)                 # now B runs
    out_a_after_b = strategy.apply(col, rule_a) # A again
    pd.testing.assert_series_equal(out_a_first, out_a_after_b)
```

### For F2 (FPE single-char bijection)
```python
def test_fpe_single_char_bijection():
    """Every single-char input must map to a distinct output."""
    from decoy_engine.transforms.fpe import FPEStrategy
    strategy = FPEStrategy(seed=42)
    for charset in ["0123456789", "abcdefghijklmnopqrstuvwxyz", "AB"]:
        key = b"\x00" * 32
        tweak = b"col"
        outputs = [strategy._fpe_pure(c, key, charset, tweak, False) for c in charset]
        assert len(set(outputs)) == len(charset), (
            f"charset {charset!r}: collision in outputs {outputs}"
        )
```

### For F3 (SHA-1 FIPS)
```python
def test_row_hash_iter_usedforsecurity_false(monkeypatch):
    """sha1 call must pass usedforsecurity=False."""
    import hashlib
    calls = []
    real_sha1 = hashlib.sha1
    def patched_sha1(data, usedforsecurity=True):
        calls.append(usedforsecurity)
        return real_sha1(data, usedforsecurity=usedforsecurity)
    monkeypatch.setattr(hashlib, "sha1", patched_sha1)
    import decoy_engine.quality.synth_report as sr
    src = pd.DataFrame({"a": [1, 2, 3]})
    sr.compute_new_row_synthesis(src, src)
    assert all(not f for f in calls), "sha1 called with usedforsecurity=True"
```

### For F5 (TVD underestimate)
```python
def test_fidelity_categorical_disjoint_sets_tvd():
    """Two snapshots with completely disjoint value sets -> similarity near 0."""
    src_snapshot = {"columns": {"col": {"kind": "categorical", "stats": {
        "top_values": [{"value": "A", "count": 50}, {"value": "B", "count": 50}],
        "other_count": 0
    }}}}
    out_snapshot = {"columns": {"col": {"kind": "categorical", "stats": {
        "top_values": [{"value": "C", "count": 50}, {"value": "D", "count": 50}],
        "other_count": 0
    }}}}
    result = compute_fidelity(src_snapshot, out_snapshot)
    col_sim = result["marginal"]["columns"][0]["similarity"]
    assert col_sim == 0.0, f"Expected 0.0 for disjoint sets, got {col_sim}"
```

### For F8 (nullable boolean filter)
```python
def test_filter_nullable_bool_column():
    """Filter on a nullable-int column (BooleanDtype result) must not raise."""
    from decoy_engine.execution._transforms import apply_transforms
    from decoy_engine.config._transforms import FilterOp
    df = pd.DataFrame({"score": pd.array([1, 2, 3, None], dtype="Int64")})
    result = apply_transforms(df, [FilterOp(expression="score >= 2")])
    assert list(result["score"]) == [2, 3]
```

---

## 5. What's Good

- **`determinism/_derive.py`** — The HMAC envelope is well-designed:
  length-prefixed namespace + source prevents injection collisions;
  the version byte is in the HMAC input (not the salt) which is the
  correct placement; RFC citations are accurate.
- **`execution/_pandas_adapter.py`** — The Q7 performance fix (batch
  `tolist()` instead of per-row `.iloc[i]`) is the right call and
  correctly applied to both source and masked sides of the parent map.
  The FK null-key handling (skip null FK children, never an orphan) is
  semantically correct.
- **`execution/_transforms.py`** — The `local_dict={}`/`global_dict={}`
  clamp on `df.eval()` (Dennis C1 fix) correctly addresses the
  `@var`-scope-walk attack vector. The derive error message on
  `derive_column_already_exists` is specific and actionable.
- **`quality/snapshot.py`** — `_categorical_stats` secondary sort on
  `(-count, str(value))` guarantees byte-stable JSON across different
  pandas build orderings. Timezone strip before `isoformat()` in
  `_datetime_stats` is the right pre-emption for cross-machine snapshot
  comparison.
- **`quality/synth_report.py`** — The dual-gate design for attack metrics
  (explicit caller opt-in AND extras package installed) is the correct
  architecture for a dangerous optional capability. The `compute_dcr`
  holdout interpretation thresholds match SDV's published values.
- **`quality/policy.py`** — The `_DEFAULT_SHAPE_STRATEGY_EXPECTATIONS`
  table correctly distinguishes value-identity TVD from shape TVD for
  hash/faker, and the D5a correction history is traceable in the
  comments.
- **`transforms/fpe.py`** — The F5 pre-computed `_CHARSET_INDEX` lookup
  table (O(1) char indexing vs O(r) `charset.index`) is a correct and
  measurable optimization. `_column_key` now raises on derivation
  failure (QA 2026-05-31 F1 closure) — correct.
