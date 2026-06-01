# QA Review — connectors / generation / profile
**Date:** 2026-06-01  
**Reviewer:** QA Agent (Claude Sonnet 4.6)  
**Scope:** `src/decoy_engine/connectors/` · `src/decoy_engine/generation/synthesize.py` · `src/decoy_engine/profile/_source.py` · `src/decoy_engine/profile/_walk.py` · `src/decoy_engine/determinism/_derive.py` · `src/decoy_engine/determinism/_hkdf.py`  
**Branches avoided (already reviewed today):**
- `qa/review-2026-06-01` — MG-1 through MG-6 strategies
- `qa/review-2026-06-01-mg2-mg3-mg4` — text_redact, when/nested, composites, distribution_behavior, registry canary
- `qa/review-2026-06-01-storm-hardening` — storm/postmask (DateShift H2, exception leak H3, composite FK H4, M11–M13, M23)
- `qa/review-2026-06-01-platform-api` (platform) — jobs, variables, key resolvers, db_io, disguises
- `qa/review-2026-06-01-cli` (decoy) — run.py, storm.py

---

## 1. Summary

The reviewed modules are generally well-structured with evidence of careful QA remediation work from earlier sessions. **The single most important issue** is a confirmed thread-safety and determinism hazard in `generation/synthesize.py`: `Faker.seed_instance()` mutates module-level `random.seed` state, meaning concurrent generation jobs sharing the `_DEFAULT_FAKER` singleton will corrupt each other's RNG streams, producing nondeterministic (and non-reproducible) output for `faker`-typed columns. This is acknowledged in the module docstring as deferred to V2.1, but given the criticality of determinism to the system's contract, it merits explicit classification. The second most significant cluster is in `profile/_source.py`: the inline boto3 client construction is missing timeout configuration and exception wrapping, leaving a credential-detail leak path and an indefinite-hang risk that the `S3FileSource` connector already correctly avoids.

---

## 2. Findings

### F1 — CRITICAL | Determinism
**`generation/synthesize.py` — `_faker()`: `Faker.seed_instance()` corrupts module-level `random` state; `_DEFAULT_FAKER` singleton is not thread-safe**

**Location:** `synthesize.py`, `_faker()` (~line 160), module docstring warning

**Issue:**  
`faker_inst.seed_instance(row_seed)` calls `random.seed(row_seed)` at the module level (Faker library internals, versions prior to addressing this). Because `_DEFAULT_FAKER` is a single shared global instance, two concurrent callers of `generate_tables()` — e.g., two platform worker threads processing different jobs — both call `seed_instance` against the same object at overlapping times. The result:

1. **Thread A** seeds with `col_seed_A + 0`, begins the Faker provider call.
2. **Thread B** seeds with `col_seed_B + 0`, clobbering the module-level `random` state before Thread A's provider function draws from it.
3. Thread A's row 0 is now derived from Thread B's seed — byte-different from a single-threaded run with the same inputs.

This violates the fundamental contract: **same seed → same output, across processes and concurrency modes**.

The module docstring acknowledges this and defers the fix ("a process-level lock around the Faker call site is the cleanest fix; deferred to V2.1"). At the time, this may have been acceptable because concurrent generation was not shipped. Before enabling any form of parallel `generate_tables` execution (multi-threaded workers, async tasks sharing a thread pool), this must be resolved.

**Impact:** Silent nondeterminism in faker-typed generation columns under concurrent use. Reproducing a job from a manifest ID produces different data if the process has other concurrent jobs. No error is raised.

**Recommended fix (two options; pick one):**

Option A — per-call lock (minimal, immediate):
```python
# At module level
_FAKER_CALL_LOCK = threading.Lock()

# In _faker(), around each provider call:
with _FAKER_CALL_LOCK:
    faker_inst.seed_instance(row_seed)
    out.append(provider_func(**faker_kwargs))
```
Note: this serializes ALL faker-type column generation across threads. Acceptable for correctness; may throttle throughput on CPU-bound pools.

Option B — per-call fresh instance (higher memory, fully parallel):
```python
# Do not cache a shared Faker; construct + seed inside the loop:
faker_inst = Faker()
faker_inst.seed_instance(row_seed)
out.append(provider_func(**faker_kwargs))
```
Faker construction is ~50–200 ms first call; subsequent calls are faster if the locale cache is warm. The module docstring's F-5 fix (caching) optimised for this at the cost of thread-safety. Benchmarking with `timeit` under realistic locales is needed to confirm acceptability.

**How to verify:** Write a 2-thread pytest fixture that calls `generate_tables` concurrently with identical seeds, assert the outputs are byte-identical across 100 repetitions. Any failure is a thread-safety violation.

---

### F2 — HIGH | Correctness + Security
**`profile/_source.py` `_load_s3_source()` — raw boto3 client exposes exception strings containing credential details; no timeout configured**

**Location:** `profile/_source.py`, `_load_s3_source()`, ~line 175

```python
client = boto3.client("s3", **client_kwargs)
response = client.get_object(Bucket=bucket, Key=key)
```

**Sub-issue A — Credential leak in exception string (High / Security):**  
A boto3 `ClientError` raised by `get_object` (e.g., `InvalidAccessKeyId`, `SignatureDoesNotMatch`, `NoCredentialsError`) propagates raw. The exception's string representation includes the error code and sometimes the request metadata, which can expose partial credential information into log streams, job error records, or API error responses. The `S3FileSource` connector already has the correct pattern: `_wrap_client_error()` strips the raw exception text and surfaces only a typed `PermanentError` / `TransientError`. The profile source's docstring claims it applies the same pattern but does NOT wrap `get_object`.

**Sub-issue B — Missing timeout (High / Reliability):**  
`boto3.client("s3", **client_kwargs)` is constructed without `BotoConfig(connect_timeout=5, read_timeout=60)`. A misconfigured endpoint URL or a routing black-hole will cause the worker to hang indefinitely on `get_object`. The `S3FileSource` connector explicitly guards against this at lines ~150–157 of `connectors/s3.py`.

**Recommended fix:**
```python
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError

client_kwargs["config"] = BotoConfig(
    connect_timeout=5,
    read_timeout=60,
    retries={"max_attempts": 1, "mode": "standard"},
)
client = boto3.client("s3", **client_kwargs)
try:
    response = client.get_object(Bucket=bucket, Key=key)
except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
    raise RuntimeError(f"S3 transient error during profiling") from exc
except ClientError as exc:
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    raise RuntimeError(f"S3 error {code} during profiling") from exc
```

**How to verify:** Unit-mock `boto3.client` to raise `ClientError` with a synthetic `InvalidAccessKeyId`; assert the propagated exception does not contain the key ID string. Integration-test with moto-S3 and a 30s delayed response to confirm timeout fires.

---

### F3 — HIGH | Determinism
**`profile/_source.py` `profile_source()` — reservoir sampling is non-deterministic when `seed=None` reaches the RNG**

**Location:** `profile/_source.py`, `profile_source()`, ~lines 55–60

```python
rng = random.Random(seed) if seed is not None else random.Random()
```

The Q15 fix (already merged) added a belt-and-suspenders fallback from `config["global_settings"]["seed"]`. However, the fallback only applies when `config_seed` is already an `int`. If the key is absent, the value is a float, or the caller explicitly passes `seed=None` without a config-level seed, the function falls through to `random.Random()` — an OS-entropy-seeded instance. Under sampling (`sample_rows=10_000`, default), different runs produce different sample selections, yielding different `distinct_count` statistics, which then drive `is_candidate_key_sampled` differently, which changes the plan's `cardinality_mode` decisions, which changes masked output even under a fixed masking seed.

**Impact:** The profile output is not reproducible when no explicit seed is supplied. Since profiling precedes plan compilation, this transitively makes plan outputs nondeterministic even with a fixed masking seed. The `profile_seed` field recorded in the `Profile` dataclass is `None` in this case, so the profile is not replayable.

**Recommended fix:**  
Make seeding mandatory, or document clearly that `seed=None` is NOT suitable for reproducible pipelines and raise a warning:
```python
import warnings

if seed is None:
    warnings.warn(
        "profile_source called without a seed: reservoir sampling will be "
        "non-deterministic. Pass seed= or set global_settings.seed in config.",
        stacklevel=2,
    )
    rng = random.Random()
else:
    rng = random.Random(seed)
```
The platform v2_runner.py path should ALWAYS pass `seed=` from the plan's `seed_envelope.job_seed` — that is the permanent fix; this warning catches call sites that regress.

**How to verify:** Write a property test (Hypothesis) that calls `profile_source` on a fixed 50k-row DataFrame twice with `seed=None`; assert the returned profiles are NOT byte-identical (to confirm the hazard) before the fix, then confirm byte-identity after passing an explicit seed.

---

### F4 — HIGH | Reliability + Correctness
**`connectors/sftp.py` `_wrap_sftp_error()` — `paramiko.SSHException` classified as `TransientError`, causing MITM alarms and permanent auth failures to be retried**

**Location:** `connectors/sftp.py`, `_wrap_sftp_error()`, ~lines 55–62

```python
if isinstance(exc, paramiko.SSHException):
    # ... Treat as transient so the engine retries ...
    return TransientError(f"SFTP protocol error: {exc}")
```

`paramiko.SSHException` is the base class for all SSH/SFTP exceptions, including:
- `paramiko.BadHostKeyException` — the host presented a key that doesn't match known_hosts. This is a potential MITM indicator. It should be `PermanentError` to fail fast, alert the operator, and NOT be retried.
- `paramiko.BadAuthenticationType` — the server doesn't support the auth method offered (permanent).
- `paramiko.ChannelException` — channel-level errors that may be permanent.

Treating all `SSHException` as transient means:
1. A genuine MITM attack attempt retries N times before the job fails, amplifying exposure time.
2. A misconfigured host key causes the entire retry budget to be consumed on guaranteed-to-fail attempts.
3. `AuthenticationException` (line 53) correctly maps to `PermanentError`; `BadAuthenticationType` (also an auth failure, but via a different code path) does not.

**Recommended fix:**
```python
import paramiko

if isinstance(exc, paramiko.BadHostKeyException):
    return PermanentError(f"SFTP host key mismatch (possible MITM): {type(exc).__name__}")
if isinstance(exc, paramiko.AuthenticationException):
    return PermanentError(f"SFTP auth failed: {type(exc).__name__}")
if isinstance(exc, paramiko.SSHException):
    # Covers channel/kex transient errors; genuine network disconnects.
    return TransientError(f"SFTP protocol error: {type(exc).__name__}")
```

Note: `BadHostKeyException` is a subclass of `SSHException` in paramiko, so the more-specific check must come first (it already does in the fix above).

**How to verify:** Unit test: patch `_open_sftp` to raise `paramiko.BadHostKeyException`; assert the returned error is `PermanentError` not `TransientError`.

---

### F5 — MEDIUM | Determinism
**`generation/synthesize.py` — `_DEFAULT_SEED = 42` diverges from the plan compiler's zero-default**

**Location:** `synthesize.py`, line ~20

```python
_DEFAULT_SEED = 42
```

The plan compiler (`plan/_compile.py`, post QA-triage F7 fix) defaults `seed_int = 0` when `global_settings.seed` is absent or explicitly `None`. `generate_tables()` defaults to `42` via `_DEFAULT_SEED`. A pipeline with no explicit seed that uses both generation and masking will compile with seed 0 for the masking plan but synthesize data with seed 42. This is a cross-path inconsistency: the same config, different pipeline modes, different effective seeds.

The number 42 is also the Python convention for "I didn't really think about this", not a documented system constant. If there IS a canonical default, it should be a single named constant imported from the determinism layer.

**Recommended fix:**
```python
# Import from the authoritative source
from decoy_engine.plan._compile import _DEFAULT_PLAN_SEED  # or define a shared constant
_DEFAULT_SEED = _DEFAULT_PLAN_SEED  # must be 0 to match the plan compiler
```

Or define a shared constant in `decoy_engine.determinism`:
```python
# determinism/__init__.py
DEFAULT_JOB_SEED: int = 0
```
And import it in both `synthesize.py` and `plan/_compile.py`.

---

### F6 — MEDIUM | Performance + Reliability
**`profile/_source.py` / `profile/_walk.py` — full table materialised into memory before reservoir sampling**

**Location:** `_source.py` `_load_source()` → `walk_dataframe()` in `_walk.py`

The profiling pipeline loads the entire source table into a pandas DataFrame, THEN samples:
```python
df = _load_source(source_descriptor)   # full materialisation
# ... in walk_dataframe:
sample_indices = rng.sample(range(row_count), sample_rows)  # select 10k of N rows
sample_df = df.iloc[sample_indices]
```

For a 10M-row, 50-column source (realistic for healthcare claims data), a CSV load produces a ~2–8 GB in-memory DataFrame before any sampling occurs. This is an **I/O bottleneck** (read the full file) and a **memory bottleneck** (materialise the full file).

The `sample_rows=10_000` cap is designed to limit cardinality computation, not file I/O. A 10M-row file still reads fully before the 10k-row sample is drawn.

**Impact:** Profiling large sources OOMs workers with modest RAM budgets. The platform's scheduler allocates worker memory based on expected output size; a profile job against a large source punches through that limit silently.

**Recommended fix (short-term):** For Parquet sources, use PyArrow's `read_table` with row group skipping to approximate a sample without full materialisation:
```python
if fmt == "parquet":
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    # Read only enough row groups to meet sample_rows
    batches = []
    collected = 0
    for batch in pf.iter_batches(batch_size=sample_rows):
        batches.append(batch.to_pandas())
        collected += len(batch)
        if sample_rows is not None and collected >= sample_rows:
            break
    return pd.concat(batches).head(sample_rows or collected)
```
For CSV, `pd.read_csv(path, nrows=sample_rows)` reads only the first N rows (not random sampling, but sufficient for cardinality approximation).

**How to profile:** `memory_profiler` on a run against a synthetic 5M-row Parquet file. `scalene` to confirm the peak RSS spike is in `_load_source`.

---

### F7 — MEDIUM | Correctness
**`generation/synthesize.py` `_formula()` — cross-column formula references silently return `[None] * n` with no warning**

**Location:** `synthesize.py`, `_formula()`, ~lines 248–255

```python
if not formula or references:
    # V1: warn or defer -- both return a None series of length n.
    return [None] * n
```

A config with `type: formula` and `references: [other_column]` silently produces a column of NULLs. There is no warning, no log line, and no `QualityWarning`. An operator who configures a cross-column formula expecting data gets a NULL-filled column in the output without knowing why.

**Impact:** Silent data quality failure. Downstream masking on a NULL-filled column may pass quietly, and the operator discovers the problem only at consumption time.

**Recommended fix:**
```python
import warnings

if not formula:
    return [None] * n
if references:
    warnings.warn(
        f"Column {col.get('name')!r}: formula with `references` is not yet supported "
        "in v2 generation (cross-column formulas land in a later sprint). "
        "Returning nulls for this column.",
        stacklevel=4,
    )
    return [None] * n
```

Alternatively, surface a `QualityWarning` through the existing engine warning mechanism so it appears in the job's quality report.

---

### F8 — MEDIUM | Correctness
**`generation/synthesize.py` `generate_tables()` — `int()` cast on seed silently raises `ValueError` for non-numeric config values**

**Location:** `synthesize.py`, `generate_tables()`, ~line 55

```python
seed = int((config.get("global_settings") or {}).get("seed", _DEFAULT_SEED))
```

`generate_tables` is documented to accept unvalidated dicts ("V1-parity callers"). If `config["global_settings"]["seed"]` is `"abc"` or `[1, 2, 3]`, `int("abc")` raises a bare `ValueError` with a message like `invalid literal for int() with base 10: 'abc'` — unhelpful to an operator who configured a bad seed in their YAML.

The plan compiler (post QA-triage F7) raises `PlanCompileError(code="seed_not_numeric")` for the same input. `generate_tables` should match that behaviour.

**Recommended fix:**
```python
raw_seed = (config.get("global_settings") or {}).get("seed", _DEFAULT_SEED)
try:
    seed = int(raw_seed)
except (TypeError, ValueError) as exc:
    raise ValueError(
        f"generate_tables: global_settings.seed must be an integer; got {raw_seed!r}"
    ) from exc
```

---

### F9 — LOW | Correctness
**`connectors/sftp.py` `SFTPFileSource.list()` and `SFTPFileSink.write()` — `datetime.fromtimestamp()` uses local timezone**

**Location:** `sftp.py`, `list()` ~line 210, `head()` ~line 224

```python
modified=(
    datetime.fromtimestamp(entry.st_mtime).isoformat()
    if entry.st_mtime is not None
    else None
),
```

`datetime.fromtimestamp(ts)` without a `tz` argument converts the Unix timestamp to LOCAL system time. On a worker running in UTC vs a developer machine in US/Eastern, the same SFTP file appears to have a `modified` 5 hours apart. If `modified` is used as a cache invalidation key or change-detection signal, this produces false positives or false negatives depending on the relative timezones involved.

**Recommended fix:**
```python
from datetime import timezone

modified=(
    datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc).isoformat()
    if entry.st_mtime is not None
    else None
),
```

Same fix applies to the identical pattern in `head()`.

---

### F10 — LOW | Correctness
**`profile/_source.py` — `profiled_at=datetime.now()` captures local system time as a naive datetime**

**Location:** `profile/_source.py`, `profile_source()`, ~line 80

```python
profiled_at=datetime.now(),
```

`datetime.now()` returns a **naive** datetime in local time. If two profile runs happen on machines in different timezones, the `profiled_at` timestamps cannot be meaningfully compared (they carry no zone info). Any serialisation/deserialisation that assumes UTC will interpret local-time naive datetimes incorrectly.

**Recommended fix:**
```python
from datetime import timezone
profiled_at=datetime.now(timezone.utc),
```

---

### F11 — LOW | Design
**`determinism/_derive.py` `derive_index()` — `pool_size < 1` raises `pool_size_overflow`, a misleading error code**

**Location:** `_derive.py`, `derive_index()`, ~lines 130–134

```python
if pool_size < 1:
    raise DeterminismError(
        code="pool_size_overflow",
        message=f"pool_size must be >= 1; got {pool_size}",
    )
```

A zero or negative pool size is not an overflow — it's an underflow or an invalid input. A caller catching `DeterminismError` and inspecting `e.code == "pool_size_overflow"` will misclassify the failure. The message is correct but the code is wrong.

**Recommended fix:**
```python
if pool_size < 1:
    raise DeterminismError(
        code="pool_size_invalid",
        message=f"pool_size must be >= 1; got {pool_size}",
    )
```

This is a breaking API change for any caller switching on the error code, but it's the right correction before GA. Add a release note.

---

## 3. Performance Notes

| Area | Bottleneck | Complexity | What to Measure |
|------|-----------|------------|----------------|
| `profile/_source.py` `_load_file_source` CSV path | I/O + memory | O(N rows × cols) full materialisation | `memory_profiler` peak RSS for a 5M-row, 50-col CSV before and after streaming fix |
| `synthesize.py` `_faker` per-row Faker call | CPU-bound (when locked) or thread-safety (unlocked) | O(n) per column | `timeit` on 1M-row faker generation single-threaded vs 4 threads |
| `synthesize.py` `_apply_null_probability` | CPU-bound inner loop | O(n) | Existing fix (F3 closure from prior session) already moved to rng.seed() reuse; verify with `cProfile` on 1M-row run |
| `connectors/s3.py` multipart write | Network I/O + memory | O(file_size / 5MB) parts | Measure throughput vs `put_object` on a 100 MB file; S3 multipart has 10-50ms part upload overhead |
| `profile/_walk.py` `walk_dataframe` sampling | CPU (iloc + nunique) | O(sample_rows × cols) after full load | Not the bottleneck after fixing F6; verify with `scalene` |

The dominant cost in the profile pipeline is full table materialisation (F6). The dominant correctness risk is F1 (Faker thread safety). All other items are O(expected) once those two are resolved.

---

## 4. Suggested Tests

| Test | What it catches | Where |
|------|----------------|-------|
| `test_generate_tables_faker_concurrent_determinism` — run 2 threads with identical seed, assert byte-identical outputs × 100 repetitions | F1 Faker thread-safety | `tests/unit/generation/test_synthesize_concurrent.py` |
| `test_generate_tables_faker_single_thread_determinism` — call twice sequentially with same seed, diff the outputs | Regression guard for F1 fix | same file |
| `test_load_s3_source_client_error_no_credential_leak` — mock `get_object` to raise `ClientError(InvalidAccessKeyId)`; assert propagated exception does not contain `"InvalidAccessKeyId"` string | F2 credential leak | `tests/unit/profile/test_source_s3.py` |
| `test_load_s3_source_timeout` — mock `get_object` to raise `ConnectTimeoutError`; assert RuntimeError raised within 1 second | F2 timeout | same |
| `test_profile_source_seed_none_warns` — call `profile_source` with a 15k-row df and `seed=None`; assert `UserWarning` is emitted | F3 non-deterministic seed | `tests/unit/profile/test_source.py` |
| `test_profile_source_seed_reproducible` — call twice with `seed=42`; assert returned profiles are byte-identical | F3 determinism guard | same |
| `test_sftp_bad_host_key_is_permanent` — mock `_open_sftp` to raise `BadHostKeyException`; assert `PermanentError` returned from `_wrap_sftp_error` | F4 MITM alarm classification | `tests/unit/connectors/test_sftp.py` |
| `test_sftp_auth_exception_is_permanent` — mock `BadAuthenticationType`; assert `PermanentError` | F4 auth error classification | same |
| `test_generate_tables_default_seed_matches_plan_compiler` — assert `_DEFAULT_SEED == 0` (or the same constant the plan compiler uses) | F5 seed consistency | `tests/unit/generation/test_synthesize.py` |
| `test_formula_column_with_references_emits_warning` — config with `type: formula, references: [other]`; assert `UserWarning` and output is all-None | F7 silent null | `tests/unit/generation/test_synthesize.py` |
| `test_generate_tables_nonnumeric_seed_raises` — pass `config` with `seed: "not_a_number"`; assert `ValueError` with helpful message | F8 seed cast guard | same |
| `test_derive_index_zero_pool_size_code` — assert `DeterminismError(code="pool_size_invalid")` for `pool_size=0` | F11 error code correctness | `tests/unit/determinism/test_derive.py` |

---

## 5. What's Good

- **`determinism/_derive.py` and `_hkdf.py` are excellent.** The HMAC-HKDF envelope is correctly implemented to RFC 5869 with length-prefixed namespace/source encoding to prevent injection collisions. Pinning reference vectors against RFC 5869 §A.1–A.3 is exactly the right test strategy. The `SEED_PROTOCOL_VERSION` bump history and rationale are documented clearly. The `Domain` Protocol contract documentation is thorough and honest about what it cannot enforce at the type level.

- **`connectors/s3.py` is high quality.** The multipart upload strategy is correct (5 MiB minimum part size, abort on failure), timeout configuration is present, error classification is accurate (including the F1 closure from the prior session that fixed `ConnectTimeoutError` / `ReadTimeoutError` misclassification). The `_join_key` defensive stripping of leading/trailing slashes prevents prefix bypass. Lazy client construction is correctly guarded.

- **`connectors/sftp.py` host-key enforcement** (the `RejectPolicy` + `DECOY_SFTP_KNOWN_HOSTS` path) is a correct fix to what would have been a critical MITM exposure. The session-liveness probe in `_connect()` (F2 closure) is a clean approach.

- **`profile/_source.py` Q15 belt-and-suspenders seed fallback** is good defensive coding even if incomplete (F3). The Q17 fix (StreamingBody context manager to return the urllib3 connection to the pool) prevents connection exhaustion on multi-S3-source pipelines.

- **`synthesize.py` `_apply_null_probability` F3 fix** (reuse `rng.seed()` rather than allocating `random.Random(seed)` per row) is well-commented and correct — the re-seed produces identical first-draw output to a fresh instance while avoiding 624-word Mersenne Twister re-initialisation overhead per row.

- **`profile/_walk.py`** duplicate column name guard is correct and well-placed. The `is_candidate_key_sampled` invariant (`not sampled AND distinct == row_count AND row_count > 0`) correctly avoids the vacuous-truth empty-table case.
