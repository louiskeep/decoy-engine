# QA Review — decoy-engine: core masking + generation pipeline

**Date:** 2026-06-02  
**Reviewer:** QA / senior performance engineer  
**Branch:** `qa/review-2026-06-02-engine`  
**Engine SHA at review:** `7524b0781f0a217c5f564bfe06f02bf165f550da`

**Scope:** `src/decoy_engine/transforms/date_shift.py`, `src/decoy_engine/transforms/fpe.py`, `src/decoy_engine/generation/synthesize.py`, `src/decoy_engine/data_discovery.py`, `src/decoy_engine/execution/_when_gate.py`, `src/decoy_engine/execution/_pandas_adapter.py`, `src/decoy_engine/execution/_runner.py`, `src/decoy_engine/execution/_transforms.py`, `src/decoy_engine/determinism/_derive.py`, `src/decoy_engine/determinism/_hkdf.py`, `src/decoy_engine/context.py`, `src/decoy_engine/connectors/s3.py`.

**Prior QA sessions avoided (per branch history):** `qa/review-2026-06-01-cli` covered `decoy` CLI `run.py` and `storm.py`. The engine carries many inline annotations from prior QA sessions (QA-1, QA-2, QA-3, QA-7, QA-8, QA-10, etc.) that document already-fixed findings. This session targets new issues only.

---

## 1. Summary

The engine's determinism layer, HKDF/HMAC primitives, FK-preserving execution pipeline, FPE strategy, and data discovery surface are all in strong shape — many prior QA findings have been cleanly closed with inline attribution. The single most important finding is that `DateShiftStrategy` derives the same HMAC key for every date column in a pipeline: all columns with the same raw date value receive the same shift, breaking the cross-column temporal de-identification guarantee and enabling trivial known-plaintext re-identification across columns. A secondary cluster of medium-severity performance issues affects the Polars `when:`-gate (redundant full-frame conversions) and generation Faker throughput (process-wide lock held for entire column generation).

---

## 2. Findings

### F1 — HIGH | Security / Determinism
**`transforms/date_shift.py::_column_key` — All date columns in a pipeline share the same HMAC key; no per-column differentiation**

```python
# DateShiftStrategy._column_key (line ~240)
def _column_key(self, column_name: str) -> bytes | None:
    if self.derive_key is None:
        return None
    try:
        return self.derive_key("mask")   # ← "mask" is hardcoded; column_name is unused
    ...
```

The `column_name` parameter is documented as "kept for log context only." Every call to `_column_key` across all date columns in the same pipeline resolves to `hkdf_sha256(pipeline_key, "mask")` — byte-identical regardless of which column is being shifted.

In `_shift_for_value_keyed`, the HMAC input is solely the raw date value string:

```python
def _shift_for_value_keyed(key: bytes, val: str, min_days: int, max_days: int) -> int:
    digest = hmac.new(key, val.encode("utf-8", errors="replace"), hashlib.sha256).digest()
    return min_days + (int.from_bytes(digest[:8], "big") % range_size)
```

No column name, table name, or any per-column discriminator is mixed in. Consequence: **if `birth_date` and `admission_date` both contain `"1990-01-15"`, they will both shift to the same output date.** An adversary who observes any (original, shifted) pair from any date column in the pipeline can compute the shift amount for that specific raw date and apply it to recover or cross-link any date column containing the same value.

**Impact:**
- HIPAA/GDPR: breaks temporal de-identification across tables and columns sharing date values.
- Cross-column linkage: records can be re-identified by matching identical shifted dates across columns.
- Known-plaintext attack: any known date in any column reveals the shift for that date system-wide within the pipeline.

Compare to `FPEStrategy`, which uses `tweak = column_name.encode()` in the Feistel — providing correct per-column separation despite sharing the same base key. Date shift needs an equivalent.

**Fix:** Mix the column name into the HMAC input inside `_shift_for_value_keyed`:

```python
def _shift_for_value_keyed(
    key: bytes, col_name: str, val: str, min_days: int, max_days: int
) -> int:
    range_size = max_days - min_days + 1
    # Length-prefix col_name to prevent col+val boundary collisions.
    col_bytes = col_name.encode("utf-8")
    msg = len(col_bytes).to_bytes(4, "big") + col_bytes + val.encode("utf-8", errors="replace")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return min_days + (int.from_bytes(digest[:8], "big") % range_size)
```

And update the call site in `DateShiftStrategy.apply`:
```python
shift_fn = lambda s: _shift_for_value_keyed(column_key, column_name, s, min_days, max_days)
```

**IMPORTANT:** This is a breaking change to the determinism contract — existing masked outputs will differ. `SEED_PROTOCOL_VERSION` must be bumped and migration documented. Alternatively, the column name can be folded into key derivation instead: `derive_key(f"date_shift:{column_name}")` rather than `derive_key("mask")`.

---

### F2 — MEDIUM | Performance
**`generation/synthesize.py::_faker` — `_FAKER_CALL_LOCK` held for the entire n-row generation loop**

```python
with _FAKER_CALL_LOCK:                      # ← acquired once ...
    if pre_seed is not None:
        faker_inst.seed_instance(pre_seed)
    for i in range(n):                      # ← ... and held for ALL n rows
        faker_inst.seed_instance(row_seed)
        out.append(provider_func(**faker_kwargs))
```

For a column with `n = 1_000_000` rows using the `name` or `address` provider, this lock can be held for 30–120 seconds. Any concurrent thread attempting to generate Faker values for any other column (in any table, in any concurrent pipeline) blocks for the entire duration of the slowest Faker column. The lock is process-wide (`_FAKER_CALL_LOCK = threading.Lock()`).

The module docstring acknowledges this as a V1 known limitation and notes a V2.1 per-call fresh-Faker plan. That plan has not yet landed, and any multi-threaded code path (e.g., a platform worker processing two concurrent generate requests) will serialize entirely on this lock.

**Bottleneck classification:** CPU-bound (Faker string generation) inside a serialized region — all Faker throughput in the process is single-threaded as long as this lock exists.

**Verification:** `py-spy record -o flame.svg -- python generate_script.py` — lock contention appears as flat wall-clock time on `_FAKER_CALL_LOCK.__enter__` / `threading.Lock.acquire`.

**Fix:** The V2.1 approach (per-call fresh `Faker(locale)` instance) is the correct resolution. Each column gets its own instance; `seed_instance` mutation is local to that instance; no lock needed. The per-call construction cost (~50–200 ms for locale data load) should be measured against the lock overhead before committing to either approach. A lighter short-term fix: construct the instance inside the lock but release the lock between rows by moving `seed_instance` + `provider_func` into a per-row `with` block — this increases lock acquisition count but reduces hold duration per acquisition to microseconds.

---

### F3 — MEDIUM | Performance
**`execution/_when_gate.py::run_with_when_gate_polars` — Full Polars→pandas frame conversion once per `when:`-gated column**

```python
def run_with_when_gate_polars(handler, frame, column, plan, ctx):
    if plan.when is None:
        return handler.run(frame, column, plan, ctx)
    
    pdf = frame.to_pandas()   # ← full conversion, paid once PER COLUMN with when:
    mask = _eval_predicate(pdf, plan.when, plan.strategy)
    ...
```

For a plan with 30 columns sharing the same `when:` expression, this converts the entire Polars frame to pandas 30 times. At 10M rows × 50 columns, each `frame.to_pandas()` costs approximately 1–3 seconds, giving 30–90 seconds of redundant conversion work for a common pattern (e.g., `when: "status == 'active'"` applied to every PII column).

The pandas adapter does not have this problem because it already holds `pd.DataFrame` frames and `_eval_predicate` returns a Series reusable across columns.

**Fix:** Cache the predicate mask for the duration of the adapter `run()` call. The runner already passes through `StrategyContext`; the mask cache can live there (keyed by `expression`) or be pre-computed in the adapter before the node loop:

```python
# In PandasExecutionAdapter.run (polars path) or passed into run_with_when_gate_polars:
when_mask_cache: dict[str, pd.Series] = {}

# In run_with_when_gate_polars:
if plan.when not in when_mask_cache:
    pdf = frame.to_pandas()
    when_mask_cache[plan.when] = _eval_predicate(pdf, plan.when, plan.strategy)
mask = when_mask_cache[plan.when]
```

Note: the mask must be scoped per-table (the same expression applied to two different tables produces different masks), so the cache key should be `(table_name, expression)`.

---

### F4 — MEDIUM | Security / Performance
**`data_discovery.py::run_discovery_sql` — `fetchmany(row_limit)` limits returned rows but does NOT prevent DuckDB from executing the full query**

```python
rel = con.execute(sql)         # DuckDB executes the full query here
raw = rel.fetchmany(row_limit) # truncates the cursor result, NOT the plan
```

A user submitting `SELECT * FROM large_table ORDER BY id` or `SELECT count(*) OVER (), * FROM large_table` causes DuckDB to scan, sort, and window-aggregate the entire table before `fetchmany` truncates. For a 500M-row Parquet file on a platform with shared memory, this is a denial-of-service vector against the discovery endpoint.

The `_validate_select_only` filter correctly blocks DDL/DML and file-read functions, but does not prevent expensive read-only queries.

**Fix:** Wrap the user query in a limit-bearing outer query before execution, allowing DuckDB's planner to push the limit down for simple projections:

```python
limited_sql = f"SELECT * FROM ({sql}\n) AS _decoy_q LIMIT {row_limit}"
try:
    rel = con.execute(limited_sql)
except duckdb.Error as exc:
    raise DiscoverySqlError(f"SQL execution failed: {exc}") from exc
raw = rel.fetchall()
```

Note: wrapping in a subquery prevents DuckDB from pushing predicates from the outer `LIMIT` into the inner scans for certain query shapes. Profile against typical discovery queries to confirm the tradeoff is acceptable. An alternative — appending `LIMIT {row_limit}` to the user SQL after stripping any trailing semicolon — is simpler but can change semantics if the user's query already has a `LIMIT` (keep only the more restrictive of the two).

---

### F5 — LOW | Reliability
**`generation/synthesize.py::_topo_sort` — Unbounded recursive DFS; `RecursionError` on deep reference chains**

```python
def dfs(n: str) -> None:
    if n in visited or n not in deps:
        return
    visited.add(n)
    for parent in deps.get(n, ()):
        dfs(parent)    # ← unbounded recursion; limit is Python's ~1000-frame default
    result.append(n)
```

A valid pipeline with a 1001-deep reference chain (`T1 → T2 → ... → T1001`) raises `RecursionError` at runtime. The `PipelineConfig._reference_graph_valid` validator catches cycles but doesn't cap chain depth.

**Fix:** Convert to iterative using an explicit stack:

```python
def _topo_sort(deps: dict[str, set[str]]) -> list[str]:
    result: list[str] = []
    visited: set[str] = set()
    for start in deps:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            n, returning = stack.pop()
            if returning:
                result.append(n)
                continue
            if n in visited or n not in deps:
                continue
            visited.add(n)
            stack.append((n, True))
            for parent in deps.get(n, ()):
                if parent not in visited:
                    stack.append((parent, False))
    return result
```

Or add a depth guard in `_reference_graph_valid` (maximum chain length 500 is generous for any real pipeline).

---

### F6 — LOW | Performance
**`transforms/fpe.py::_fpe_pure` — Single-character path uses O(r) `charset.index()` instead of the pre-built O(1) `char_to_idx`**

```python
def _fpe_pure(self, s: str, key: bytes, charset: str, tweak: bytes, validate_luhn: bool) -> str:
    n = len(s)
    if n == 0:
        return s

    if n == 1:
        idx = charset.index(s[0])   # ← O(r) linear scan; char_to_idx is built 10 lines below
        ...

    # Built here for the multi-char path, but after the single-char branch:
    char_to_idx = _CHARSET_INDEX.get(charset)
    if char_to_idx is None:
        char_to_idx = {ch: i for i, ch in enumerate(charset)}
    x = _encode(s, charset, char_to_idx)
```

For the `ALPHANUM` charset (r=62) with 1M single-character values (e.g., a flag column like `grade`), the current code executes 62M comparisons instead of 1M dict lookups.

**Fix:** Move the `char_to_idx` build before the `n == 1` branch and use it there:

```python
char_to_idx = _CHARSET_INDEX.get(charset)
if char_to_idx is None:
    char_to_idx = {ch: i for i, ch in enumerate(charset)}

if n == 1:
    idx = char_to_idx[s[0]]   # O(1)
    msg = b"fpe-single\xff" + tweak
    F = int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big")
    return charset[(idx + F) % len(charset)]
```

---

### F7 — LOW | Security
**`transforms/fpe.py::_prf` — PRF message uses a raw `b"\xff"` separator between `tweak` and `operand_b`; ambiguous when `tweak` ends with `\xff`**

```python
def _prf(key: bytes, round_index: int, tweak: bytes, operand: int) -> bytes:
    operand_b = operand.to_bytes(max((operand.bit_length() + 7) // 8, 1), "big")
    msg = struct.pack(">B", round_index) + tweak + b"\xff" + operand_b
    return hmac.new(key, msg, hashlib.sha256).digest()
```

The separator `b"\xff"` is a single byte. `tweak` is `column_name.encode("utf-8")`. For a column name containing multi-byte UTF-8 characters whose encoding ends in byte `0xFF` — e.g., the character U+00FF (ÿ) encodes to `b"\xc3\xbf"`, so its last byte is `0xBF`, not `0xFF`. However, U+D7FF (Hangul Jamo) encodes to `b"\xed\x9f\xbf"` — also not `0xFF`. Characters whose UTF-8 encoding genuinely ends in `0xFF` are extremely rare (the UTF-8 standard restricts leading/continuation byte ranges), but the encoding is NOT collision-proof for arbitrary byte sequences.

More concretely: the `operand_b` encoding is variable-length (`max((operand.bit_length() + 7) // 8, 1)` bytes). A tweak ending with `b"\xff"` and an operand `V` produces the same message as a shorter tweak and an operand `V'` where `V' = 0xFF * 256^(len(operand_b_original)) + V`. For an input string whose `v_mod = 10^6`, operand values up to 999,999 (6-digit digit strings) exceed this threshold, making valid collisions constructible.

For ASCII column names (bytes 0x20–0x7E), `0xFF` never appears and the separator is unambiguous. The risk is non-zero only for non-ASCII column names.

**Fix:** Length-prefix the tweak to make the concatenation injective:

```python
msg = (struct.pack(">B", round_index)
       + len(tweak).to_bytes(4, "big") + tweak
       + operand_b)
```

Drop the `b"\xff"` separator entirely — the 4-byte length prefix on `tweak` makes parsing unambiguous. **IMPORTANT:** This changes the FPE output for all columns. Bump `SEED_PROTOCOL_VERSION`.

---

### F8 — NIT | Design
**`context.py::emit_step` — Inner `except Exception: pass` in the fallback path silently swallows non-signature errors**

```python
except TypeError:
    import logging
    logging.getLogger(__name__).debug(
        "emit_step: step() rejected new kwargs ..."
    )
    try:
        fn(name, status=status, rows_in=rows_in, rows_out=rows_out)
    except Exception:     # ← swallows transient DB errors, not just signature mismatches
        pass
```

The outer `except TypeError` is correctly narrow (signature mismatch). The inner `except Exception: pass` catches everything including transient `JobLogger` DB failures. A `step()` call that fails with a connection error in the fallback is silently discarded — the engine proceeds and the operator has no record that step logging broke mid-job.

**Fix:** Log at `DEBUG` level inside the inner except so silent swallowing is at least observable:

```python
except Exception as fallback_exc:
    logging.getLogger(__name__).debug(
        "emit_step: fallback step() also failed: %s",
        type(fallback_exc).__name__,
    )
```

---

## 3. Performance Notes

**Bottleneck classification for the masking pipeline (as reviewed):**

| Hot path | Bottleneck type | Current state |
|----------|-----------------|---------------|
| FPE strategy | CPU (8× HMAC-SHA256 per value) | Acceptable; chunked parallelism via `fpe_chunk_count` knob |
| Date shift (keyed) | CPU (1× HMAC-SHA256 per value); I/O for large Parquet | Acceptable; F1 fix adds negligible HMAC overhead |
| Faker generation | CPU; serialized by `_FAKER_CALL_LOCK` | F2: lock contention blocks all Faker in the process |
| FK child resolution | Memory (parent map dict per edge); CPU O(n) scan | S21 Q7 fix landed; `.tolist()` batch materalization is correct |
| `when:` gate (polars) | Memory (full frame conversion per column) | F3: redundant conversions per column with shared predicate |
| Data discovery | I/O + DuckDB query execution | F4: no limit pushdown; full-scan DoS vector |
| `_topo_sort` | O((n+e) log n) with heapq (QA-10 F9 fix already landed) | Fine |

**What to profile:** For generation throughput, run `scalene --profile-interval 0.01 generate_script.py` and look for time concentrated in `threading.Lock.acquire`. For masking throughput, `cProfile` on `PandasExecutionAdapter.run` will show the strategy-by-strategy breakdown; `date_shift` and `fpe` will dominate for typical HIPAA configs.

**Reproducibility check:** To verify determinism for any fixed config + seed, run the same job twice and `diff` the output Parquet files:
```bash
python -c "import pyarrow.parquet as pq; import sys; pq.write_table(pq.read_table('out.parquet'), '/tmp/sorted.parquet', sort_keys=[])" 
diff <(python job.py | xxd) <(python job.py | xxd)  # byte-identical expected
```

---

## 4. Suggested Tests

| Finding | Test case |
|---------|----------|
| F1: date_shift key sharing | Two date columns with the same raw value; assert post-mask values DIFFER after the fix. Before fix: assert they are EQUAL (documents current behavior + gives a failing test when fix lands). |
| F1: date_shift known-plaintext | Given one (original, shifted) pair from column A, verify that predicting the shift on column B's matching date is NOT possible after the fix (i.e., the predicted shifted date ≠ the actual shifted date because keys now differ). |
| F2: Faker lock throughput | Spawn two threads simultaneously running `_faker` on a 100K-row column; measure combined wall time vs expected 2× serial time. Pre-fix: sequential. Post-fix: parallel. |
| F3: Polars when-gate conversion count | Patch `frame.to_pandas` with a counter; run a 30-column plan where all 30 share the same `when:` expression; assert counter = 1 after fix, not 30. |
| F4: discovery row limit | Register a 10M-row synthetic view; submit `SELECT * FROM t ORDER BY id`; assert the query completes in < 5 seconds (limit pushdown) and returns exactly `row_limit` rows. |
| F5: deep reference chain | Build a config with 1100 chained reference tables; assert `generate_tables` completes without `RecursionError`. |
| F6: single-char FPE throughput | `timeit` `_fpe_pure("5", key, "0123456789", tweak, False)` before and after fix; assert post-fix is faster for large r. |
| F7: PRF injectivity cross-column | For a column name whose UTF-8 encoding ends in a byte ≥ 0x80, assert that `_prf(key, 0, tweak_a, operand_x)` ≠ `_prf(key, 0, tweak_b, operand_y)` for the constructed collision inputs. |
| General determinism | Same seed + same config twice → `diff` Arrow table bytes; should be empty diff. Run on all strategy types. |
| General FK stability | Mask a parent table; verify all child FK values map to values that exist in the parent's masked output (no broken FK references). |

---

## 5. What's Good

- **The determinism layer (`_derive.py`, `_hkdf.py`) is exemplary.** Length-prefixed HMAC inputs, injective namespace+source encoding, `SEED_PROTOCOL_VERSION` discipline with inline changelog, `DeriveContext` pre-computation to amortise HKDF cost per column — all correct and well-reasoned. RFC 5869 compliance is pinned by reference-vector tests.

- **FPE key derivation failure now raises explicitly** (QA 2026-05-31 F1 closure). Silently degrading to seed-only encryption was the right issue to fix. The same pattern applied to `DateShiftStrategy` (Dennis H2 fix) is consistent. F1 in this session asks for the complementary fix: column-level separation inside the shared-key path.

- **FK child resolution is correct and efficiently implemented.** The `.tolist()` batch materialization (S21 Q7 fix), the O((n+e) log n) Kahn topological sort (QA-10 F9), and the `_fk_key_value` int/float normalizer for null-bearing integer columns are all sound. The parent-map cache design prevents redundant key-map rebuilds across multiple FK children of the same parent.

- **`data_discovery.py` SQL injection posture is solid.** The use of DuckDB's relational API (`con.read_parquet(path).create_view(name)`) for view registration avoids SQL string construction entirely. The three-layer `_validate_select_only` (leading-keyword whitelist + banned-keyword regex + quoted-path FROM guard) is thorough and the QA-2 file-function denylist closure is correct.

- **The `when:` predicate gate correctly rejects non-boolean eval results** (QA-3 F4 fix), accepts pandas nullable `BooleanDtype`, and includes a polars-path anchor column to survive row-reordering handlers (QA-3 F13). The numexpr pin + empty `local_dict`/`global_dict` (Dennis C1) is the correct defense against scope-walk code execution.

- **`S3FileSink.write` multipart abort is robust.** Best-effort abort on any exception after multipart initiation, with the abort failure swallowed so it doesn't mask the original error. The transient/permanent error classification (QA 2026-05-31 F1 closure: `ConnectTimeoutError` and `ReadTimeoutError` now correctly map to `TransientError`) is the right shape.
