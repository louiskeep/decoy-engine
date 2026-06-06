# QA Review: determinism / expressions / data_discovery / context / fidelity / polars-adapter

**Date:** 2026-06-06
**Reviewer:** Claude (QA session)
**Branch:** `qa/review-2026-06-06-determinism-expressions-discovery-fidelity`
**Session note:** Prior sessions today covered connectors/relationships, nested/shuffle/bucketize/derive, pandas-adapter/post-validation, synthesize/generation, and the engine execution pipeline (session branch). This review targets the six modules listed above -- none touched in today's prior sessions.

---

## Modules Reviewed

| Module | Path | Size |
|---|---|---|
| HKDF implementation | `src/decoy_engine/determinism/_hkdf.py` | 3.6 KB |
| Derive primitives | `src/decoy_engine/determinism/_derive.py` | 13.6 KB |
| Safe eval / formula scope | `src/decoy_engine/expressions.py` | 6.2 KB |
| SQL discovery | `src/decoy_engine/data_discovery.py` | 8.4 KB |
| Execution context | `src/decoy_engine/context.py` | 16.3 KB |
| Fidelity scoring | `src/decoy_engine/quality/fidelity.py` | 14.5 KB |
| Polars adapter | `src/decoy_engine/execution/polars/_polars_adapter.py` | 12.4 KB |

---

## 1. Summary

The determinism and expression layers are well-structured and the HKDF/HMAC implementation is correct against RFC 5869. The most important bug is in `DeriveContext.derive_source`: it re-accepts a `namespace` argument that must match the namespace used in `for_column()`, but there is no enforcement. A caller passing a different namespace produces a silent third output that matches neither `derive(seed, correct_ns, source)` nor `derive(seed, wrong_ns, source)`, violating the core determinism contract. The fix is a one-line store on the dataclass. The other findings are mostly Medium/Low: a ReDoS surface in the formula sandbox, a row-limit enforcement gap in the SQL discovery layer, a TVD double-counting error in the fidelity scorer, and a pair of missing guards in the key derivation path.

**Single most important issue:** `DeriveContext.derive_source` namespace mismatch (F1, High, Correctness).

---

## 2. Findings

### F1 -- High | Correctness | `determinism/_derive.py`

**Issue:** `DeriveContext.derive_source(namespace, source)` requires callers to pass the same `namespace` that was used in `DeriveContext.for_column(seed, namespace)`, but this is not enforced. If a caller passes the wrong namespace, the result is neither `derive(seed, correct_ns, source)` nor `derive(seed, wrong_ns, source)` -- it is a cryptographically distinct third value with no defined semantics. The determinism contract ("same inputs -> byte-identical output") is silently violated.

The architecture of `derive_source` re-encodes `namespace` into the HMAC input envelope, while the HMAC key (`_hmac_key`) was derived from the `for_column` namespace via HKDF. These two occurrences of `namespace` must be identical for `derive_source` to agree with the scalar `derive()` function. There is no structural guarantee that they are.

**Why it matters:** Any strategy adapter that constructs a `DeriveContext.for_column("table/col_a", ...)` but then calls `ctx.derive_source("table/col_b", ...)` (e.g., by iterating over column names in a loop and passing the wrong one) will produce wrong masked bytes -- silently, with no exception, and with output that passes type checks. The per-column `DeriveContext` pattern is specifically the hot path for all V2 scalar strategies at scale.

**Fix:** Store `namespace` on the frozen dataclass; remove the parameter from `derive_source`. The namespace was already fully encoded into `_hmac_key` at construction time; the HMAC input envelope also needs it, but there is no reason to accept it again from the caller.

```python
@dataclass(frozen=True)
class DeriveContext:
    _hmac_key: bytes
    _namespace: str          # <-- add: stored at construction, not re-accepted per call

    @classmethod
    def for_column(cls, seed: bytes, namespace: str) -> DeriveContext:
        if len(seed) != _SEED_LENGTH:
            raise DeterminismError(code="seed_wrong_length", ...)
        if not namespace:
            raise DeterminismError(code="namespace_empty", ...)
        key = hkdf_sha256(ikm=seed, salt=_SALT, info=namespace.encode("utf-8"), length=32)
        return cls(_hmac_key=key, _namespace=namespace)   # <-- pass namespace

    def derive_source(self, source: bytes) -> bytes:      # <-- namespace removed
        namespace_bytes = self._namespace.encode("utf-8") # <-- use stored namespace
        hmac_input = (
            bytes([SEED_PROTOCOL_VERSION])
            + len(namespace_bytes).to_bytes(4, "big")
            + namespace_bytes
            + len(source).to_bytes(4, "big")
            + source
        )
        return hmac.new(self._hmac_key, hmac_input, hashlib.sha256).digest()
```

**Breaking change:** All callers that pass `namespace` as positional or keyword to `derive_source` must remove the argument. A deprecation shim that accepts but ignores the old arg (with a warning) can bridge the transition if needed.

---

### F2 -- Medium | Security | `expressions.py`

**Issue:** `_SafeRe.compile` is exposed in the formula scope under the name `re.compile`. A formula author can pre-compile a catastrophically backtracking regular expression and then apply it against long input strings:

```python
# Formula string authored by a (potentially adversarial) user:
re.compile('(a+)+$').search(value)
```

For a 30-character input like `'a' * 30 + 'b'`, this pattern triggers exponential backtracking in CPython's `re` engine. The engine has no timeout, no subprocess isolation, and no GIL release during backtracking. A single malicious formula call can pin a CPU core until the process is killed. In a multi-tenant deployment this is a denial-of-service primitive.

**Why it matters:** The docstring notes that `simpleeval` blocks dunder traversal and module references, but it does not bound CPU consumption of safe operations. Compiled regex patterns are exactly the kind of safe-but-expensive object that bypasses expression-level sandboxing.

**Recommended fix (ordered by preference):**

1. Remove `compile` from `_SafeRe`. Formulas are evaluated once per row (not cached), so pre-compiling patterns inside a formula is an anti-pattern anyway. The other `_SafeRe` methods (`sub`, `search`, etc.) accept pattern strings directly and compile internally; removing `compile` loses nothing useful.

2. If `compile` must be kept: wrap it to reject patterns that fail a static complexity check (e.g., nested quantifiers: `re.compile(r'(\w+){2,}\1')` regex on the pattern string itself, or use the `re2` library which has polynomial guarantees).

3. For defense-in-depth regardless: run formula evaluation under `signal.alarm(N)` on Unix (not portable) or in a `concurrent.futures.ProcessPoolExecutor` with a timeout (portable, but changes the calling convention).

**Test to add:** `test_redos_formula_rejected_or_bounded`: attempt to evaluate `re.compile('(a+)+$').search('a' * 30 + 'b')` in `safe_eval`; assert it either raises or completes within 100 ms.

---

### F3 -- Medium | Performance | `data_discovery.py`

**Issue:** The `row_limit` is enforced at the Python `fetchmany` layer, not at the DuckDB execution layer:

```python
rel = con.execute(sql)
cols = [d[0] for d in rel.description]
raw = rel.fetchmany(row_limit)
```

DuckDB executes the full query -- including sorting for `ORDER BY`, aggregation, window functions -- before `fetchmany` cuts the result to `row_limit` rows. A user who writes `SELECT * FROM large_table ORDER BY col` will cause DuckDB to materialize and sort the full table in its in-process memory before Python discards all but the first `row_limit` rows.

**Why it matters:** The discovery surface is intended for interactive exploration ("slice and aggregate across tables"), which implies latency-sensitive use. A sort over a 10M-row Parquet file with only 10_000 rows needed is 1000x more work than necessary.

**Fix:** Auto-append `LIMIT {row_limit}` to the SQL when the (already-validated) body does not already end with a `LIMIT` clause. Since `_validate_select_only` has already confirmed this is a single SELECT-class statement with no injection surfaces, the append is safe:

```python
def _apply_row_limit(body: str, row_limit: int) -> str:
    # body is already stripped of comments and trailing semicolon.
    if not re.search(r'\bLIMIT\b', body, re.IGNORECASE):
        return f"{body} LIMIT {row_limit}"
    return body
```

Insert this between `_validate_select_only` and `con.execute`. Keep `fetchmany` as a backstop (the user's explicit `LIMIT N` might exceed `row_limit`; `fetchmany` still enforces the cap).

**Bottleneck classification:** CPU-bound (DuckDB sort) then memory-bound (materialized result). Profile with `time.perf_counter()` around `con.execute` vs `fetchmany` on a 1M-row Parquet file with an `ORDER BY` column.

---

### F4 -- Medium | Security | `data_discovery.py`

**Issue:** The SQL denylist (`_BANNED_RE`, `_QUOTED_PATH_FROM_RE`) is applied as a regex over the raw text of the query body. This has two problems:

1. **False positives:** A legitimate query containing a banned keyword in a string literal -- `SELECT 'INSERT this is a label' AS action` -- is rejected with a confusing error message. Users writing documentation-quality column values or string manipulation queries will hit this.

2. **Potential bypass via Unicode normalization:** DuckDB's tokenizer normalizes some Unicode characters to their ASCII equivalents (e.g., fullwidth Latin letters U+FF41-U+FF5A). A Python `re.IGNORECASE` pattern match against the raw query string does not apply DuckDB's tokenizer normalization. A query using `ＩＮＳＥＲＴ` (fullwidth) would pass the Python regex check but might be accepted by DuckDB's parser as `INSERT`. This is a theoretical bypass and its exploitability depends on DuckDB's parser version, but it is a known class of SQL injection technique.

**Why it matters:** The defense-in-depth (in-memory DB with no ATTACH, no persistent state) means a successful bypass results in a statement that creates an in-memory table and is immediately discarded when the connection closes. No persistent mutation is possible. Nevertheless, regex-over-string is the wrong primitive for SQL classification.

**Fix (preferred):** Use DuckDB's own query classification. DuckDB exposes `con.execute("SELECT query_type FROM duckdb_queries() WHERE ...").fetchone()` or the relation API's `.type` attribute. Before executing the user query, prepare it (without running it) and assert the type is `SELECT`. This is immune to comment tricks, Unicode normalization, and string literals.

**Fix (interim):** Keep the current denylist but improve the false-positive story by using DuckDB's tokenizer: strip string literals from the body before scanning for banned keywords. Python's `re` can do this with a `'[^']*'|"[^"]*"|<banned_pattern>` alternation that skips string-literal spans.

---

### F5 -- Medium | Data | `quality/fidelity.py`

**Issue:** `_categorical_similarity` and `_joint_similarity` can double-count probability mass when a value appears in the output's `top_values` but was in the source's `other_count` bucket.

Walk-through: suppose source top-K is `[A: 80, B: 70]` with `other_count=50` (total=200). Output top-K is `[A: 60, B: 50, C: 80]` with `other_count=10` (total=200). `C` was a rare value in source (part of the `50` other_count).

Current computation for key `C`:
- `src_probs.get('C', 0.0)` = 0.0 (C not in source top-K explicit dict)
- `out_probs.get('C', ...)` = 80/200 = 0.4
- Contributes `|0.0 - 0.4|` = 0.4 to `tvd`

Current computation for `other` bucket:
- `src_other / src_total` = 50/200 = 0.25
- `out_other / out_total` = 10/200 = 0.05
- Contributes `|0.25 - 0.05|` = 0.2 to `tvd`

But C's source probability (say 30/200 = 0.15) is included in both the C term (as 0.0, missing from src top-K) and the `other` term (as part of the 0.25 "other" probability). The TVD is over-estimated because C's source mass is in `other` but is also implicitly penalized by the explicit `C` term (the difference is 0.4 but it should be 0.4 - 0.15 = 0.25).

Before 0.5 normalization, `tvd` can exceed 2.0, making `tvd_normalized > 1.0` and `similarity = max(0.0, 1.0 - tvd_normalized)` clamped to 0.0 for cases that should score positively.

**Why it matters:** Fidelity scores drive the "how much shape did masking preserve?" question. An artificially deflated score for strategies that produce high-cardinality outputs (hash, faker, FPE) would incorrectly signal poor shape preservation and could trigger false alerts if D4 policy thresholds are wired to these scores.

**Fix:** The correct TVD treatment for bounded top-K snapshots is to fold `other_count` into the comparison as a single opaque bucket on both sides. The current approach does this already for the other-vs-other term, but the explicit-key loop must be restricted to keys that appear in both top-K sets explicitly. Keys in only one side should contribute their probability against the matched side's `other_count`, not against 0:

```python
# Only compare keys where both sides are explicit.
shared_explicit = set(src_probs) & set(out_probs)
tvd = sum(abs(src_probs[k] - out_probs[k]) for k in shared_explicit)
# Keys in src_top but not out_top: compare against out_other fraction.
out_other_frac = out_other / out_total
for k in set(src_probs) - set(out_probs):
    tvd += abs(src_probs[k] - out_other_frac / max(len(set(src_probs) - set(out_probs)), 1))
# ... and symmetrically for src_other.
```

Alternatively, reconstitute full probability vectors including `other` as a single key before the TVD sum:

```python
SENTINEL = "__other__"
src_full = dict(src_probs); src_full[SENTINEL] = src_other / src_total
out_full = dict(out_probs); out_full[SENTINEL] = out_other / out_total
all_keys = set(src_full) | set(out_full)
tvd_normalized = 0.5 * sum(abs(src_full.get(k, 0.0) - out_full.get(k, 0.0)) for k in all_keys)
```

Note: this formulation still has the `other` approximation (we can't know which explicit-in-one-side values are inside the other's `other` bucket from the snapshot alone), but it correctly bounds TVD in [0, 1] and eliminates the clamping-to-zero artifact.

---

### F6 -- Medium | Security | `context.py`

**Issue:** `_hkdf_sha256` in `context.py` uses `salt=b"\x00" * 32` (thirty-two zero bytes) while the canonical `_derive.py` uses `salt=b"decoy-engine/determinism/v1"` (a domain-separator string). This inconsistency means the key derivation for `make_key_resolver` uses HKDF without domain separation between the master key and any other HKDF outputs that share the same master but use the all-zero salt.

RFC 5869 §3.1: "HKDF uses a non-secret and reusable salt value... it is strongly recommended that applications adhere to some mechanism that assigns distinct values to the `salt` parameter for different operations." An all-zero salt is the RFC's designated fallback when no salt is available, not a security feature.

**Why it matters:** If a future feature derives a second type of key from the same 32-byte master using HKDF with a zero salt but a different `info` prefix, all such derivations share the same extraction layer (PRK = HMAC(b"\x00" * 32, master)). While `info` differentiates the expanded outputs, having a shared PRK weakens the independence argument between different key types. The canonical `_derive.py` pattern -- using a named domain-separator salt -- is the correct pattern and should be adopted here.

**Fix:** Replace `b"\x00" * 32` with a named domain separator:

```python
_KEY_RESOLVER_SALT = b"decoy-engine/key-resolver/v1"

def _hkdf_sha256(master: bytes, info: str, length: int = 32) -> bytes:
    from decoy_engine.determinism._hkdf import hkdf_sha256
    return hkdf_sha256(ikm=master, salt=_KEY_RESOLVER_SALT, info=info.encode("utf-8"), length=length)
```

**Breaking change:** Existing deployed instances using `make_key_resolver` will derive different column subkeys from the same master after this change. This is a key-protocol bump and must be coordinated with a SEED_PROTOCOL_VERSION bump and migration plan. Flag for the next key-protocol sprint.

---

### F7 -- Low | Correctness | `determinism/_hkdf.py`

**Issue:** `hkdf_expand` does not validate `len(prk) >= _HASH_LEN` (32 bytes). RFC 5869 §2.3 requires PRK to be at least `HashLen` bytes. A caller passing a shorter PRK (e.g., a 16-byte key) would produce output that is neither RFC-compliant nor byte-identical to a correct implementation.

In practice this never fails today because `hkdf_expand` is always called through `hkdf_sha256 -> hkdf_extract` (which always returns 32 bytes). But the interface is callable standalone and the guard is missing.

**Fix:**
```python
def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    if len(prk) < _HASH_LEN:
        raise ValueError(f"HKDF PRK must be at least {_HASH_LEN} bytes; got {len(prk)}")
    ...
```

---

### F8 -- Low | Reliability | `determinism/_derive.py`

**Issue:** `DeterminismError`'s docstring lists three valid `code` values (`seed_wrong_length`, `namespace_empty`, `pool_size_overflow`) but `derive_index` also raises `DeterminismError(code="pool_size_invalid")` for non-positive pool sizes. Callers that catch `DeterminismError` and dispatch on `e.code` will not handle `pool_size_invalid` and will fall through to a default/re-raise they may not have intended.

**Fix:** Add `pool_size_invalid: pool_size must be >= 1` to the `DeterminismError` docstring codes table.

---

### F9 -- Low | Design | `determinism/_derive.py`

**Issue:** `IdentityDomain` is exported from the production `determinism/__init__.py` and carries a docstring saying it is "NOT a customer-facing API" and exists only for integration tests. Production modules should not export test utilities.

**Why it matters:** Exporting `IdentityDomain` from the production package means it appears in `help(decoy_engine.determinism)`, in IDE autocomplete, and in `__all__`. A future author unfamiliar with its test-only status might use it in production, returning raw HMAC bytes to users instead of a typed domain value.

**Fix:** Move `IdentityDomain` to `tests/unit/helpers/determinism.py` or a dedicated `src/decoy_engine/determinism/_testing.py` that is importable from tests but not exported from `__init__.py`. Update `__all__` in `determinism/__init__.py` accordingly.

---

### F10 -- Low | Correctness | `context.py`

**Issue:** `make_key_resolver` does not validate that `pipeline_label` is non-empty:

```python
def make_key_resolver(master: bytes, pipeline_label: str) -> Callable[[str], bytes]:
    ...
    pipeline_key = _hkdf_sha256(master, f"pipeline:{pipeline_label}")
```

An empty `pipeline_label` produces `pipeline_key = _hkdf_sha256(master, "pipeline:")`. This is technically unique and deterministic, but it is almost certainly a caller error. An empty label would, for instance, make two different pipelines with empty labels share the same column subkeys, defeating pipeline-scoped key isolation.

**Fix:** Add a guard at the top of `make_key_resolver`:
```python
if not pipeline_label:
    raise ValueError("pipeline_label must be non-empty")
```

---

### F11 -- Low | Data | `data_discovery.py`

**Issue:** `_coerce` handles `str`, `int`, `float`, `bool`, `None`, and objects with `isoformat()` (datetime family), and `bytes`. DuckDB returns additional Python types for which `_coerce` falls through to `str(value)`:

- `decimal.Decimal` -> `str(Decimal('1.5'))` = `'1.5'` (acceptable, minor precision loss risk)
- `datetime.timedelta` -> `str(timedelta(days=1))` = `'1 day, 0:00:00'` (non-standard format, not ISO 8601)
- `uuid.UUID` -> `str(uuid.UUID(...))` = the standard UUID string (acceptable)
- DuckDB `struct` types -> `str(...)` produces a Python dict repr, not JSON
- DuckDB `list` types -> `str(...)` produces a Python list repr, not JSON

**Why it matters:** The docstring says values are "coerced to JSON-friendly Python types so the platform can return them directly through FastAPI without extra serialization." A `timedelta` repr string (`'1 day, 0:00:00'`) passed to `json.dumps` would succeed (it's a string) but would not round-trip as a machine-readable duration. A `struct` repr (`"{'x': 1, 'y': 2}"`) is a Python repr, not valid JSON.

**Fix:** Add explicit cases for `decimal.Decimal`, `datetime.timedelta` (ISO 8601 P format or total seconds), and DuckDB struct/list/array types. Alternatively, use DuckDB's built-in conversion to JSON for complex types before fetching.

---

### F12 -- Low | Design | `data_discovery.py`

**Issue:** Table names in the `tables: Mapping[str, str]` parameter are passed directly to `create_view(name, replace=True)` without identifier validation:

```python
con.read_parquet(path).create_view(name, replace=True)
```

DuckDB's `create_view` quotes the name as an identifier internally, so SQL injection via the view name is not a risk. However, names containing null bytes, names longer than DuckDB's identifier limit, or names containing characters that round-trip through DuckDB's quoting differently than expected (e.g., names containing double-quotes) may produce unexpected behavior or confusing error messages.

**Fix:** Validate `name` before registration:
```python
import re as _re
_SAFE_VIEW_NAME = _re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

for name, path in tables.items():
    if not _SAFE_VIEW_NAME.fullmatch(name):
        raise DiscoverySqlError(f"Table name {name!r} is not a valid SQL identifier.")
    con.read_parquet(path).create_view(name, replace=True)
```

---

### F13 -- Nit | Design | `execution/polars/_polars_adapter.py`

**Issue:** `_run_via_pandas_oracle` performs a `pa -> pl -> pa` round-trip on every source table before handing to the pandas adapter:

```python
substrate_sources: dict[str, pa.Table] = {
    table: boundary.to_arrow(boundary.to_polars(tbl)) for table, tbl in sources.items()
}
```

The comment says this is intentional for timing. But this means every pandas-oracle invocation (which is the current majority of real jobs -- FK, composite, unmigrated strategies) pays a full double-conversion overhead before masking even starts. For a 1M-row table with 50 columns the conversion cost is measurable (benchmarks in `tests/benchmark/calibration/results.md` can confirm, but ~100-300 ms per leg is realistic based on the existing boundary benchmark numbers).

**Why it matters:** Since `fallback_to_pandas=True` by default and FK/composite jobs always fall to the oracle, the Polars adapter currently imposes additional latency on all FK jobs compared to using `PandasExecutionAdapter` directly, even though the masking outputs are byte-identical.

**Recommended action:** Document this overhead explicitly in the module docstring's fallback-behavior section (currently the note says "byte-for-byte identical outputs" but does not flag the conversion tax). Consider making the round-trip opt-in via a `benchmark_boundary=False` flag, or removing it from the production path now that the pa->pl->pa losslessness is established and can be verified by dedicated parity tests.

---

### F14 -- Nit | Design | `determinism/_derive.py`

**Issue:** The `DeriveContext` frozen dataclass stores `_hmac_key: bytes` with a leading underscore. While the underscore signals "internal," the dataclass constructor exposes it as a positional / keyword parameter (`DeriveContext(_hmac_key=key)`), so callers outside the module could in principle construct a `DeriveContext` with an arbitrary key. The intent is that all instances come from `for_column()`, but the constructor is unrestricted.

Additionally, `repr=True` (the default for dataclass fields) means `repr(ctx)` would print the raw HMAC key bytes. This is a mild information-exposure risk in logs.

**Fix:** Mark the field with `field(repr=False)`:
```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class DeriveContext:
    _hmac_key: bytes = field(repr=False)
```

If F1's fix (storing `_namespace`) is applied, mark both fields with `repr=False`.

---

## 3. Performance Notes

| Module | Bottleneck | Complexity | Profile command |
|---|---|---|---|
| `_derive.py` `derive()` | CPU (2x HMAC-SHA256 per call) | O(1) per call | `python -m timeit -s "from decoy_engine.determinism._derive import derive; s=b'12345678'; n='tbl/col'" "derive(s,n,b'row_value')"` |
| `_derive.py` `DeriveContext.derive_source()` | CPU (1x HMAC-SHA256 per call) | O(1) per call | Same, but compare factory construction + N calls vs N scalar `derive()` calls |
| `expressions.py` `safe_eval()` | CPU (`EvalWithCompoundTypes()` construction overhead per row) | O(1) per call (but non-trivial constant) | `python -m cProfile -s cumulative` on a formula strategy run; expect `EvalWithCompoundTypes.__init__` in top-5 |
| `data_discovery.py` | CPU (DuckDB query execution) + Memory (full result materialisation for ORDER BY) | O(n log n) for sorted queries | `time` the `con.execute(sql)` step vs `fetchmany`; the gap measures unnecessary work |
| `quality/fidelity.py` | CPU negligible (dict arithmetic) | O(K) where K = top_K size per column | Not a bottleneck; pure Python over small summary dicts |
| `polars/_polars_adapter.py` oracle path | CPU (double pa<->pl conversion) | O(n * cols) | Compare `PandasExecutionAdapter.run()` vs `PolarsExecutionAdapter.run()` wall time on an FK job |

---

## 4. Suggested Tests

| ID | Module | Test Case |
|---|---|---|
| T1 | `_derive.py` | `test_derive_context_namespace_mismatch`: construct `DeriveContext.for_column(seed, "ns_a")`, call `derive_source("ns_b", source)` (after F1 fix, assert the api signature no longer accepts namespace); verify output != `derive(seed, "ns_a", source)` with the old API to document the former bug. |
| T2 | `_derive.py` | `test_derive_context_agrees_with_scalar`: property-based test (Hypothesis) over random 8-byte seeds, non-empty namespaces, and arbitrary source bytes: `DeriveContext.for_column(seed, ns).derive_source(source) == derive(seed, ns, source)`. |
| T3 | `_hkdf.py` | `test_hkdf_expand_rejects_short_prk`: `hkdf_expand(b"short", b"", 32)` raises `ValueError` after F7 fix. |
| T4 | `expressions.py` | `test_redos_bounded`: evaluate `re.compile('(a+)+$').search(value)` via `safe_eval` with a 30-char string; assert completes within 200 ms (or assert `compile` is not in scope after F2 fix). |
| T5 | `expressions.py` | `test_make_mask_globals_rng_isolation`: two `make_mask_globals(rng1)` and `make_mask_globals(rng2)` with different seeds produce different sequences from `randint(0, 100)`, and neither shares state with module-level `MASK_GLOBALS`. |
| T6 | `data_discovery.py` | `test_row_limit_applied_at_sql_level` (after F3 fix): verify a query against a 100k-row Parquet runs faster with `LIMIT` appended than without (benchmark with `time.perf_counter`; use a query with an `ORDER BY` to expose the sort cost). |
| T7 | `data_discovery.py` | `test_banned_keyword_in_string_literal`: `SELECT 'INSERT is a verb' AS note` is accepted (not rejected) after F4 fix; currently this is rejected as a false positive. |
| T8 | `data_discovery.py` | `test_coerce_timedelta_iso8601` (after F11 fix): DuckDB query `SELECT INTERVAL '1 day'` returns a value that `_coerce` converts to an ISO-8601 duration string, not a Python `timedelta` repr. |
| T9 | `quality/fidelity.py` | `test_categorical_tvd_bounded`: construct a pair of snapshots where a value in `out_top_values` was in `src_other_count`; assert `0.0 <= similarity <= 1.0`. Currently fails (similarity is clamped to 0.0 when it should be positive). |
| T10 | `quality/fidelity.py` | `test_fidelity_identity_is_one`: `compute_fidelity(snap, snap)` for the same snapshot twice returns `overall_score == 1.0` for all columns. |
| T11 | `context.py` | `test_make_key_resolver_rejects_empty_label` (after F10 fix): `make_key_resolver(b'\x00' * 32, "")` raises `ValueError`. |
| T12 | `context.py` | `test_key_resolver_pipeline_isolation`: two resolvers with different `pipeline_label` values and the same master key return different bytes for the same `info` string. |

---

## 5. What's Good

- **HKDF implementation is correct.** The RFC 5869 §2.2 / §2.3 implementation in `_hkdf.py` is mathematically correct: salt is the HMAC key, IKM is the message in Extract; PRK is the HMAC key in Expand; the counter is appended inside the message per spec. The empty-salt rejection guard (QA-10 F12 closure) is a good defensive addition that prevents silently degraded security from a failed salt-generation step.

- **HMAC envelope is injective.** The length-prefixed encoding `len(namespace) || namespace || len(source) || source` in `_derive.py` correctly prevents domain-separation collisions. The comment explaining this is a good example of the "explain why" rule.

- **`safe_eval` security posture is well-thought-out.** The decision to use `simpleeval` (established library) rather than a custom AST sandbox, the explicit `_SafeRe` proxy to avoid handing a module reference to simpleeval, the `make_mask_globals` factory for per-formula RNG isolation, and the audit trail (SEC.1 comment) are all good practice. The only gap is the ReDoS surface from `re.compile`.

- **`data_discovery.py` defense-in-depth is sound.** The three-layer filter (allowlist + denylist + quoted-path scan), the relational API for Parquet registration (no string interpolation), and the use of a private in-memory DuckDB connection that is unconditionally closed in `finally` are all correct. The previous QA-2 gap (arbitrary-file-read via `read_csv`) was correctly fixed and the fix is well-documented in-code.

- **`fidelity.py` method selection is defensible.** Quantile RMSE for numeric, TVD for categorical/datetime, length-mean for freetext, and equal-weight aggregation all cite prior art (SDV QualityReport, NIST SP 800-188). The `_SCORE_PRECISION = 6` pin for float round-trip determinism is a good habit borrowed from the snapshot module.

- **`context.py` `emit_*` helpers swallow exceptions without crashing the pipeline.** The pattern of `getattr(logger, method, None)` + `try/except Exception: pass` correctly treats structured events as best-effort observability, not a correctness dependency. The explicit DEBUG log on the signature fallback in `emit_step` is a good debugging aid.
