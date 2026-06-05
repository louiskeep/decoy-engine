# QA Review: `profile/`, `sdk.py`, `validation/_config.py`, `validation_result.py`

**Date:** 2026-06-05  
**Reviewer:** QA agent  
**Branch:** `qa/review-2026-06-05-profile-sdk-validation`  
**Engine commit:** `6e3bc97` (main)

## Scope

First-time QA coverage for:

| File | Size |
|---|---|
| `src/decoy_engine/profile/_source.py` | 16 KB |
| `src/decoy_engine/profile/_walk.py` | 7 KB |
| `src/decoy_engine/profile/_types.py` | 8.7 KB |
| `src/decoy_engine/profile/_pii.py` | 4.5 KB |
| `src/decoy_engine/profile/_serialize.py` | 5.8 KB |
| `src/decoy_engine/profile/_hash.py` | 1.1 KB |
| `src/decoy_engine/sdk.py` | 10.3 KB |
| `src/decoy_engine/validation/_config.py` | 3.2 KB |
| `src/decoy_engine/validation_result.py` | 16.8 KB |

**Previously covered (excluded):** `plan/`, `config/`, `execution/`, `generation/`, `relationships/`, `connectors/`, `determinism/`, `disguises/`, `storm/`, `quality/`, `data_discovery.py`, `providers_v2/`, `generators/`, `transforms/`, `walks/`, `internal/`, `validation/post/`.

---

## 1. Summary

The profile module is architecturally sound with commendable invariant enforcement at construction time (`ColumnProfile.__post_init__`, `Relationship.__post_init__`, `TableProfile.__post_init__`) and well-integrated prior-sprint fixes (Q15 seed fallback, QA-7 F3 seeding warning, Q17/Q18 connection lifecycle). The most important finding is that `_load_gcs_source` is missing both timeout configuration and exception sanitization: GCS SDK exceptions propagate raw, which can leak bucket names, object paths, and service-account emails into job logs. The second most important is a determinism hazard in `_data_shape_bytes`: the `tables` array reflects YAML `sources:` declaration order, so reordering source blocks in the pipeline YAML changes the `profile_hash` even when the data is identical, silently invalidating cached plans. All three loaders (`file`, `s3`, `gcs`) materialize full remote files before sampling, causing peak RSS proportional to file size rather than `sample_rows`.

---

## 2. Findings

### F1 -- HIGH | Security | `profile/_source.py:_load_gcs_source` -- GCS exceptions not sanitized; bucket/object/service-account leakage

`_load_s3_source` wraps `ClientError`, `ConnectTimeoutError`, and `EndpointConnectionError` into safe `RuntimeError` messages that strip the raw SDK context. `_load_gcs_source` wraps nothing:

```python
with storage.Client() as client:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    data = blob.download_as_bytes()   # no try/except; raw GCS exception propagates
```

The google-cloud-storage SDK raises:
- `google.cloud.exceptions.NotFound` with the full GCS URI (`gs://bucket/object`) in `str(exc)`
- `google.cloud.exceptions.Forbidden` with the service account email in the raw response
- `google.api_core.exceptions.DeadlineExceeded` with request metadata

All propagate into the platform job log and the STORM manifest as raw Python exception strings. Any operator with job-log read access can extract bucket names, object paths, and service account identities from an authorization failure -- a meaningful information disclosure in a multi-tenant deployment.

**Fix:** Mirror the `_load_s3_source` exception-wrapping pattern:

```python
from google.api_core import exceptions as gcore_exceptions

try:
    with storage.Client() as client:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        data = blob.download_as_bytes(timeout=(5, 60))  # F2 fix co-located
except gcore_exceptions.NotFound:
    raise RuntimeError("profile_source gcs: object not found") from None
except gcore_exceptions.Forbidden:
    raise RuntimeError("profile_source gcs: access denied") from None
except gcore_exceptions.DeadlineExceeded:
    raise RuntimeError("profile_source gcs: request timed out") from None
except Exception as exc:
    raise RuntimeError(
        f"profile_source gcs: unexpected error ({type(exc).__name__})"
    ) from exc
```

Using `from None` (or `from exc` with type-only info) prevents the raw exception string from appearing in the `__cause__` chain that most log formatters include.

---

### F2 -- HIGH | Reliability | `profile/_source.py:_load_gcs_source` -- no timeout; hangs indefinitely on network issues

`_load_s3_source` configures `BotoConfig(connect_timeout=5, read_timeout=60, retries={"max_attempts": 1})` per the QA-7 F2 annotation in the source. `_load_gcs_source` passes no timeout to `download_as_bytes()`:

```python
data = blob.download_as_bytes()   # timeout=None by default; waits forever
```

The google-cloud-storage SDK's `download_as_bytes()` accepts `timeout` (default `None` = infinite). A misconfigured GCS endpoint, a routing black hole, or a slow network stalls the profiling worker thread indefinitely. Under concurrency, a single hung call starves all other jobs waiting for profile results.

**Fix:** Pass a timeout tuple (co-located with F1 fix above):

```python
data = blob.download_as_bytes(timeout=(5, 60))  # (connect_timeout, read_timeout) in seconds
```

**Verify:** The S3 connector in `connectors/s3.py` uses `BotoConfig(connect_timeout=5, read_timeout=60)` as the reference benchmark. Align `_load_gcs_source` to the same timeouts.

---

### F3 -- MEDIUM | Performance | `profile/_source.py` -- all three loaders fully materialize remote/large files before sampling

`_load_s3_source` and `_load_gcs_source` fully materialize the remote object before handing it to pandas:

```python
# _load_s3_source
with response["Body"] as stream:
    body = io.BytesIO(stream.read())   # entire S3 object in RAM
return pd.read_parquet(body)

# _load_gcs_source
data = blob.download_as_bytes()        # entire GCS object in RAM
body = io.BytesIO(data)
return pd.read_parquet(body)
```

`_load_file_source` has the same problem: `pd.read_csv(path)` and `pd.read_parquet(path)` load the entire file before `walk_dataframe` samples it.

For a 1 GB Parquet source: `stream.read()` / `download_as_bytes()` materializes 1 GB of raw bytes. `pd.read_parquet()` then decodes it to a DataFrame (typically 2-4x the Parquet size due to columnar decompression). Peak RSS during profiling is 3-5 GB per source file, before a single row of sampling work is done.

`profile_source`'s default is `sample_rows=10_000`. A platform with a 500 M-row, 4 GB Parquet source loads 4 GB just to sample 10,000 rows. Profiling is the first step of every pipeline run; this OOM risk blocks all pipelines at V2 scale.

**Bottleneck:** Memory + I/O. Profile with:
```bash
python -m scalene --cpu --memory -c "
from decoy_engine.profile._source import _load_s3_source
_load_s3_source({'format': 'parquet', 'bucket': '...', 'key': '...'})  "
```
The `stream.read()` line will show a 1 GB spike.

**Recommended fix (Parquet -- row-group streaming):**

```python
import pyarrow.parquet as pq

def _load_file_parquet_sampled(path: str, sample_rows: int | None) -> pd.DataFrame:
    if sample_rows is None:
        return pd.read_parquet(path)
    pf = pq.ParquetFile(path)
    frames = []
    total = 0
    for batch in pf.iter_batches(batch_size=max(sample_rows, 1024)):
        frames.append(batch.to_pandas())
        total += len(frames[-1])
        if total >= sample_rows:
            break
    df = pd.concat(frames, ignore_index=True)
    return df.iloc[:sample_rows]
```

For S3, use ranged GET (`GetObject` with `Range: bytes=0-N`) to avoid downloading the full object. For GCS, `blob.download_as_bytes(start=0, end=N)` achieves the same. For both, the Parquet row-group size determines the minimum download granularity; document that `sample_rows` must be at least one row group (~10K-100K rows) to benefit from the optimization.

The full-memory path remains necessary when `sample_rows=None` (full-scan mode). Document in `profile_source` docstring that `sample_rows=None` should not be used on sources larger than the worker's available RAM.

---

### F4 -- MEDIUM | Correctness | `profile/_serialize.py:profile_from_json` -- docstring says `ValueError`; missing keys raise `KeyError`

The `profile_from_json` docstring says:

> Raises ValueError if the JSON shape does not match the expected Profile schema.

But `_profile_from_dict` uses bracket access throughout:

```python
def _profile_from_dict(data: dict[str, Any]) -> Profile:
    return Profile(
        schema_version=data["schema_version"],   # KeyError if absent
        tables=tuple(_table_from_dict(t) for t in data["tables"]),  # KeyError
        ...
    )
```

A JSON payload missing any required field raises `KeyError`, not `ValueError`. Callers that `except ValueError` per the docstring contract miss these failures.

**Impact:** Platform callers that catch `ValueError` to handle corrupted manifests or truncated evidence artifacts will see `KeyError` escape as an unhandled exception. The contract says one thing; the code does another.

**Fix (preferred -- enforce the contract):**

```python
def profile_from_json(s: str) -> Profile:
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        raise ValueError(f"profile_from_json: invalid JSON: {exc}") from exc
    try:
        return _profile_from_dict(data)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"profile_from_json: malformed profile JSON: {exc}") from exc
```

**Fix (minimal -- honest docstring):** Amend to `Raises ValueError or KeyError if...` until the wrapping is in place.

---

### F5 -- MEDIUM | Determinism | `profile/_serialize.py:_data_shape_bytes` -- profile hash includes table iteration order; YAML source reorder changes the hash

`_data_shape_bytes` serializes `profile.tables` in tuple order:

```python
payload = {
    "schema_version": profile.schema_version,
    "tables": [_table_to_dict(t) for t in profile.tables],  # iteration order
    "relationships": [_relationship_to_dict(r) for r in profile.relationships],
}
```

`profile.tables` is built in `config["sources"].items()` order in `profile_source`. `sources` comes from `PipelineConfig.model_dump()`, which preserves YAML declaration order (Python 3.7+ dict order).

**Consequence:** Reordering the `sources:` block in a pipeline YAML -- without adding, removing, or changing any source -- changes `profile_hash`, which changes the plan hash from `compile_plan`, which invalidates platform plan caches and breaks audit replay matching for unchanged pipelines. Two operators who declare the same sources in different order get semantically identical profiles with different hashes.

**Verify the gap:**
```python
from decoy_engine.profile._types import Profile, TableProfile
from decoy_engine.profile._hash import profile_hash
# Build two Profiles with same tables in different orders
p1 = Profile(..., tables=(table_a, table_b), ...)
p2 = Profile(..., tables=(table_b, table_a), ...)
assert profile_hash(p1) == profile_hash(p2)  # CURRENTLY FAILS
```

**Fix:** Sort tables (and relationships) by a stable key inside `_data_shape_bytes` only:

```python
def _data_shape_bytes(profile: Profile) -> bytes:
    payload = {
        "schema_version": profile.schema_version,
        "tables": [
            _table_to_dict(t)
            for t in sorted(profile.tables, key=lambda t: t.name)
        ],
        "relationships": [
            _relationship_to_dict(r)
            for r in sorted(
                profile.relationships,
                key=lambda r: (r.parent_table, r.child_table, r.parent_columns, r.child_columns),
            )
        ],
    }
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
```

Do NOT sort `profile.tables` itself -- consumers downstream of profile_source may rely on declaration order. The sort is local to the hash function.

This is a breaking change to stored `profile_hash` values. Pre-GA hard delete applies (best-practices §8.1); stored hashes are invalidated on the next engine release.

---

### F6 -- MEDIUM | Design | `profile/_types.py` + `profile/_walk.py` -- `is_candidate_key_sampled` field name inverts its semantics

`ColumnProfile.is_candidate_key_sampled` is `True` ONLY when the column uniqueness check was made from a FULL SCAN (i.e., `not sampled`). The `__post_init__` enforces this: `sampled=True` with `is_candidate_key_sampled=True` raises. The name reads as "candidate key determined via a sample?" but the logic means the opposite: "candidate key confirmed by a full scan, not a sample."

```python
# _walk.py ~L93 -- is_candidate_key_sampled is True ONLY when not sampled
is_candidate_key_sampled = (
    not sampled and row_count > 0 and distinct_count is not None and distinct_count == row_count
)
```

The `_types.py` docstring partially explains: "it is only True when the column passed a definitive (non-sampled) full-scan uniqueness check" -- which is the opposite of what the name implies. Every reader must hold this inversion in their head.

**Impact:** Future refactors that read `is_candidate_key_sampled` and interpret the name literally will invert the predicate. The planner code (already reviewed) is correct but harder to read because of the naming.

**Fix:** Rename to `is_definitive_candidate_key` (reflects the actual meaning):

```python
# _types.py ColumnProfile field
is_definitive_candidate_key: bool
```

Update `_walk.py` (computation), `_serialize.py` (`_column_to_dict` / `_column_from_dict`), all plan compiler reads, and the `__post_init__` message in `_types.py`. Five files, no logic change, pure rename. The serialization key name in `_column_to_dict` should also change (`"is_candidate_key_sampled"` -> `"is_definitive_candidate_key"`) -- this is a breaking JSON format change; pre-GA hard delete applies.

---

### F7 -- LOW | Performance | `profile/_walk.py` -- unsorted `sample_indices` passed to `df.iloc[list]`; slower columnar access

```python
sample_indices = rng.sample(range(row_count), sample_rows)
sample_df = df.iloc[sample_indices]   # indices in random order
```

`rng.sample` returns indices in random (non-monotone) order. `df.iloc[list]` on a columnar DataFrame (Parquet-backed, or standard pandas with contiguous columns) performs random row access. Sorted indices allow pandas to use a faster sequential scan path; unsorted indices trigger a full fancy-index copy pass.

**Fix (one line):**

```python
sample_indices = sorted(rng.sample(range(row_count), sample_rows))
sample_df = df.iloc[sample_indices]
```

Sorting 10,000 integers is O(10_000 × log 10_000) ≈ 130,000 operations -- negligible vs. the I/O cost of reading any significant DataFrame. Note: sorting does not change statistical representativeness since the indices are chosen by `rng.sample` (uniform without replacement) before sorting.

---

### F8 -- LOW | Performance | `profile/_walk.py` -- duplicate column name check is O(n²)

```python
dupes = sorted({n for n in col_names if col_names.count(n) > 1})
```

`col_names.count(n)` is O(len(col_names)) per element; the set comprehension calls it `len(col_names)` times → O(n²) total. At 500 columns: 250,000 operations. At 1,000 columns: 1,000,000.

**Fix:**

```python
from collections import Counter
counts = Counter(col_names)
dupes = sorted(n for n, count in counts.items() if count > 1)
```

O(n) time and space. Same error message, same output.

---

### F9 -- LOW | Correctness | `profile/_source.py:_load_file_source` -- CSV loaded without explicit encoding; Windows mojibake

```python
if fmt == "csv":
    return pd.read_csv(path)   # encoding not specified
```

`pd.read_csv(path)` without `encoding=` uses Python's default (`"utf-8"` on Linux/macOS, `locale.getpreferredencoding(False)` on Windows). On a Windows worker with cp1252 locale, a UTF-8 CSV with non-ASCII characters (international names, addresses, IBANs) silently corrupts the data. The resulting `ColumnProfile` statistics reflect the corrupted values; the STORM PII detector may miss PII that was mangled at the byte level.

**Fix:**

```python
return pd.read_csv(path, encoding="utf-8-sig")  # strips optional BOM, reads as UTF-8
```

`utf-8-sig` transparently handles the UTF-8 BOM that Windows-generated CSVs often include, and reads as strict UTF-8 otherwise. If callers need to override encoding (e.g., a legacy cp1252 source), expose an `encoding` field in the file source descriptor and pass it through.

---

### F10 -- LOW | Design | `sdk.py:FileSource.head()` -- default O(n) listing is a latency trap for connector authors

```python
def head(self, path: str) -> FileMeta:
    for item in self.list():        # O(n) in number of files in the connector scope
        if item.path == path:
            return item
    raise PermanentError(...)
```

The default walks the entire listing to find one path. Connector authors who forget to override `head()` inherit this default silently. A bucket with 1M objects causes a multi-second (or multi-minute) listing stall on every `head()` call.

The docstring says "Connectors with a native HEAD operation... should override for efficiency" but this is easy to miss, especially for community connector authors.

**Fix (documentation):** Upgrade the docstring to a prominent WARNING:

```
IMPORTANT: The default implementation is O(n) in the number of objects
in the connector scope. Any production connector managing more than
~1,000 files MUST override head() with a native HEAD call (e.g., S3
head_object, GCS Blob.reload, SFTP stat) to avoid full-listing stalls.
```

**Fix (optional enforcement):** Add a `CAP_NATIVE_HEAD = "native_head"` capability flag. The engine's connector loader can log a `WARNING` when `head()` is called on a connector that hasn't declared `native_head=True` and hasn't overridden the default method.

---

### F11 -- NIT | Design | `profile/_hash.py` -- imports private `_data_shape_bytes` across module boundary

```python
from decoy_engine.profile._serialize import _data_shape_bytes
```

`_data_shape_bytes` is a module-private function (underscore-prefixed in `_serialize.py`). `_hash.py` reaches across `_serialize.py`'s internal boundary. Linters and import-linter rules that enforce `internal/` protection (applied elsewhere in the engine) won't catch this because both files are in the `profile/` package.

**Fix (preferred):** `profile_hash` is a one-liner (`hashlib.sha256(_data_shape_bytes(profile)).hexdigest()`). Collapse it into `_serialize.py` and expose it from `profile/__init__.py`. This eliminates `_hash.py` and the cross-private import.

**Fix (minimal):** Rename `_data_shape_bytes` to `data_shape_bytes` (no underscore) in `_serialize.py` to make the export intentional.

---

### F12 -- NIT | Correctness | `validation/_config.py:_select_validator` -- error message conflates legacy V1 and current "version: 1" naming

```python
raise PipelineValidationError(
    "v1 mask + v1 generate config shapes are no longer validated by "
    "validate_config (S9 removal). Use a `version: 1` PipelineConfig ..."
)
```

The message says "v1 mask + v1 generate config shapes" but then says "Use a `version: 1` PipelineConfig" -- the current V2 PipelineConfig also uses `version: 1` as its YAML key. An operator seeing this error on an unversioned YAML must parse the message carefully to understand that the solution (`version: 1`) is the modern V2 schema, not the old V1 schema.

**Fix:**

```python
raise PipelineValidationError(
    "Config must include `version: 1` to use the V2 PipelineConfig schema. "
    "Legacy masking-engine (masking_rules:) and generate-engine (tables: dict) "
    "configs are no longer supported (removed in S9). "
    "See decoy_engine.PipelineConfig for the current config format."
)
```

---

## 3. Performance Notes

| Module | Bottleneck | Complexity | Class |
|---|---|---|---|
| `_source.py:_load_gcs_source` | Full-object download to RAM | O(file_size) | Memory + I/O |
| `_source.py:_load_s3_source` | Full-object download to RAM | O(file_size) | Memory + I/O |
| `_source.py:_load_file_source` | Full CSV/Parquet to RAM | O(file_size) | Memory + I/O |
| `_walk.py:walk_dataframe` | Unsorted `df.iloc[list]` | minor constant | CPU |
| `_walk.py:walk_dataframe` | O(n²) dup-col check | O(cols²) per table | CPU |
| `_walk.py:_walk_column` | `series.isna().sum()` | O(rows × cols) | CPU (expected, vectorized) |

**Primary bottleneck is the source loaders** (F3). Profile with:
```bash
python -m memory_profiler -m decoy_engine.profile._source
# Expects a 1 GB RAM spike at stream.read() / download_as_bytes() for any source over 100 MB.
```

**Walk phase:** `series.isna().sum()` iterates all rows for null counting. At 10M rows × 100 columns = 1B null-flag checks; NumPy vectorization puts this at ~1-2 seconds total. This is expected and not worth optimizing at current row-count tiers.

**Sampling path:** `rng.sample(range(row_count), sample_rows)` is O(sample_rows) in memory. For the default `sample_rows=10_000`, this allocates a 10K-element list of Python ints -- negligible.

---

## 4. Suggested Tests

```
tests/unit/profile/test_source_gcs_exceptions.py
  - test_gcs_not_found_wraps_to_runtime_error
      Monkeypatch blob.download_as_bytes to raise
      google.cloud.exceptions.NotFound; assert profile_source raises
      RuntimeError with no bucket/object name in str(exc). (F1)

  - test_gcs_forbidden_wraps_no_service_account_in_message
      Monkeypatch to raise Forbidden; assert RuntimeError and no @-sign
      (service account email) in str(exc). (F1)

  - test_gcs_timeout_configured
      Monkeypatch blob.download_as_bytes and assert it was called with a
      non-None timeout kwarg. (F2)

  - test_gcs_deadline_exceeded_wraps_to_runtime_error
      Monkeypatch to raise google.api_core.exceptions.DeadlineExceeded;
      assert RuntimeError with message containing "timed out". (F2)

tests/unit/profile/test_profile_hash_determinism.py
  - test_profile_hash_stable_under_source_reorder
      Build two Profiles with tables (a, b) and (b, a) respectively but
      same content; assert profile_hash(p1) == profile_hash(p2).
      CURRENTLY FAILS -- gates the F5 fix. (F5)

  - test_profile_hash_changes_on_column_change
      Change one column's null_count by 1; assert profile_hash changes.
      Regression guard that the hash is still load-bearing after the fix.

  - test_relationship_hash_stable_under_reorder
      Two profiles with relationships in different list orders; same hash. (F5)

tests/unit/profile/test_profile_roundtrip.py
  - test_profile_from_json_missing_schema_version_raises_value_error
      Pass JSON missing "schema_version"; assert ValueError (not KeyError).
      CURRENTLY FAILS -- gates the F4 fix. (F4)

  - test_profile_from_json_missing_tables_raises_value_error
      Same for missing "tables" key. (F4)

  - test_profile_roundtrip_with_pii_class
      Profile with PIIClass.SSN on a column;
      profile_from_json(profile_to_json(p)) == p.

  - test_profile_roundtrip_with_composite_fk
      Profile with a two-column Relationship; round-trip equality holds.

tests/unit/profile/test_walk_dataframe.py
  - test_sample_indices_are_sorted_before_iloc
      Monkeypatch df.iloc and capture the index argument;
      assert it is sorted. (F7)

  - test_duplicate_col_check_is_linear
      DataFrame with 1,000 duplicate column names; assert ValueError
      raised in < 5ms (timeit regression guard). (F8)

  - test_walk_csv_utf8_non_ascii
      Write a UTF-8 CSV with a column containing "Muller" (ASCII) and
      "Mueller" (ASCII), plus "Müller" (non-ASCII, U+00FC); load via
      _load_file_source; assert distinct_count == 3, not corrupted. (F9)

  - test_full_scan_unique_column_is_definitive_candidate_key
      5-row DataFrame with a unique ID column, sample_rows=None;
      assert is_candidate_key_sampled (or renamed field) is True.

  - test_sampled_unique_column_not_definitive
      100-row DataFrame with unique ID, sample_rows=10;
      assert is_candidate_key_sampled is False (no false positive on sample). (F6)
```

---

## 5. What's Good

- **Invariant enforcement at construction time** is excellent throughout: `ColumnProfile.__post_init__`, `Relationship.__post_init__`, and `TableProfile.__post_init__` all catch bad state (null_count > row_count, fk/fk_target mismatch, duplicate column names, empty column tuples, sampled+is_candidate_key_sampled contradiction) with specific, named error messages. Problems cannot survive construction and reach the planner silently.

- **Q15 / QA-7 F3 seed-fallback pattern** is well-executed: `profile_source` first checks `config["global_settings"]["seed"]` as a fallback when `seed=None`, then emits a loud `warnings.warn()` if still `None`. Belt + suspenders. The in-source reference to the QA finding that motivated the pattern (`# Q15 fix (Option B, ...)`) is exactly the kind of "why" comment the guide calls for.

- **`_column_to_dict` is hand-listed** rather than using `dataclasses.asdict()`. The comment explains why: new `ColumnProfile` fields should not silently change the wire shape. This is the right discipline for a public serialization format shared across engine, CLI, and platform.

- **`_best_high_confidence_match` is deterministic:** Python's `max()` returns the first element when keys are tied (uses `>`, never `>=`). STORM's detector evaluation order is stable, so tie resolution is deterministic across runs. The comment correctly explains this invariant.

- **`_data_shape_bytes` excludes sidecar metadata** (profiled_at, decoy_engine_version, profile_seed) by construction. Two profiles over identical source data taken at different times by different engine versions produce the same hash -- the B1 invariant from the Dennis spec review. The hash is a genuine content fingerprint, not an artifact fingerprint.

- **`sdk.py` capability flag design** is additive: `CAP_*` string constants let old connectors that don't know about new capabilities default to `False` at the engine without breakage. New capabilities can be added without bumping `SDK_VERSION` or breaking existing connectors.

- **`validation_result.py:ValidationResult.ok` property** gives callers a single canonical "can this run?" signal rather than requiring `len(errors) == 0` checks scattered across callers. The errors/warnings split keeps the lists type-homogeneous and lets callers render all problems at once.

- **`CODES` class namespace** in `validation_result.py` is a clean alternative to string enums: existing code using literal strings still works, while new code using `CODES.X` gets IDE completion and typo protection without forcing a migration. Adding codes is non-breaking; renaming is explicitly called out as a breaking change in the module docstring.

- **S3 source exception sanitization** (QA-7 F2) is correctly applied: `ClientError`, `EndpointConnectionError`, `ConnectTimeoutError`, and `ReadTimeoutError` all get safe RuntimeError wrappers. F1 above extends this pattern to the GCS path.
