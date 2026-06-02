# QA Review — transforms · connectors · relationships

**Date:** 2026-06-02  
**Branch:** `qa/review-2026-06-02-transforms-connectors-relationships`  
**Reviewer:** QA/Performance Engineering (automated session)  
**Scope:** `src/decoy_engine/transforms/` (fpe.py, date_shift.py, formula.py, base.py), `src/decoy_engine/connectors/` (s3.py, sftp.py), `src/decoy_engine/relationships/` (_graph.py, _namespace.py), `src/decoy_engine/expressions.py`  
**Prior QA branches avoided:** determinism/, execution/, providers_v2/, config/, storm/, keys-auth, platform (all reviewed 2026-06-01/02)

---

## 1. Summary

The reviewed modules carry the marks of several prior QA passes (F1–F9 fixes, Dennis H2 revisions, session2 closures) and are materially better for it. The cryptographic core of FPE, the keyed date-shift derivation, and the FK relationship graph are architecturally sound. Three critical issues remain outstanding. First, `safe_eval`'s `__builtins__: {}` guard does not stop object-literal attribute traversal in CPython, making every config-supplied `formula` expression a full arbitrary-code-execution surface. Second, there is no resource cap on expression evaluation, so a single formula value can OOM or hang the masking worker. Third, the SFTP connector mis-classifies an unknown-host permanent failure as a transient error, causing the retry loop to spin indefinitely on an unconfigurable misconfiguration. A fourth high-severity issue — the S3 permanent-error set contains bare HTTP numeric codes (`"403"`, `"404"`) that botocore never emits — silently dead-codes the protection they were meant to provide.

---

## 2. Findings

### F1 · CRITICAL · Security — `safe_eval` sandbox is bypassable via object-literal attribute traversal

**File:** `src/decoy_engine/expressions.py` · `safe_eval`, `MASK_GLOBALS`

**Issue:**  
The guard `{"__builtins__": {}}` removes named built-in functions (`open`, `exec`, `__import__`, etc.) from the eval namespace. It does **not** block attribute access on objects reachable through expression literals. In any CPython version, the following expression evaluates successfully with `MASK_GLOBALS` as `globals_`:

```python
().__class__.__bases__[0].__subclasses__()
```

This returns every Python class currently loaded in the process. In a typical masking-engine worker, `subprocess.Popen`, `io.FileIO`, and `os.system`-wrapping classes are reachable from that list. A config-supplied formula can therefore read arbitrary files, spawn processes, or exfiltrate masked data:

```python
# YAML formula field:
# formula: "[c for c in ().__class__.__bases__[0].__subclasses__() if c.__name__=='FileIO'][0]('/etc/shadow').read()"
```

**Impact:** Full RCE from any YAML `formula` strategy field. The blast radius is the permissions of the masking-engine process, which in cloud deployments often carries IAM roles or service-account credentials.

**Recommended fix:**  
Options in priority order:

1. **RestrictedPython** (`pip install RestrictedPython`). Compiles to an AST that strips dunder attribute access and restricts the import surface before evaluation. Drop-in wrapper:
   ```python
   from RestrictedPython import compile_restricted, safe_globals
   def safe_eval(expr, globals_, locals_):
       code = compile_restricted(expr, '<formula>', 'eval')
       return eval(code, safe_globals | globals_, locals_)
   ```
2. **Dunder-attribute block at parse time.** Reject any formula expression containing `__` before evaluation:
   ```python
   if '__' in expr:
       raise ValueError(f"Formula expression contains forbidden '__': {expr!r}")
   ```
   This is a belt-and-suspenders layer, not sufficient alone.
3. **Subprocess isolation.** Evaluate formulas in a worker subprocess with seccomp/AppArmor restrictions. High operational cost; use if RestrictedPython is blocked.

Until fixed, treat the `formula` masking strategy as a privileged capability. Do not allow user-supplied YAML (e.g., API-driven configs) to reach a `formula` rule.

---

### F2 · CRITICAL · Reliability — `safe_eval` has no memory or time resource limit

**File:** `src/decoy_engine/expressions.py` · `safe_eval`

**Issue:**  
No expression complexity cap, no memory limit, and no CPU timeout guard exist. The following are valid Python expressions that evaluate silently:

```python
"A" * (10 ** 9)                  # allocates ~1 GB string
"x" * 10 ** int("9" * 8)        # allocates >> available RAM
list(range(10 ** 8))             # 800 MB list before the column can proceed
```

A single malformed or adversarial formula field will OOM the masking worker. Because the formula is evaluated inside `column.apply(lambda v: ...)`, the OOM hits mid-column with no graceful cleanup.

**Impact:** Denial of service against the masking worker from any config field. On cloud workers with fixed memory envelopes, a single job aborts and the operator sees an OOM signal with no pointer to the offending formula.

**Recommended fix:**  
On Linux, use `resource.setrlimit` to cap virtual memory before entering eval:
```python
import resource

_MAX_FORMULA_EXPR_BYTES = 512  # reject obviously-large expressions at the string level
_FORMULA_MEM_LIMIT_BYTES = 256 * 1024 * 1024  # 256 MB per eval call

def safe_eval(expr, globals_, locals_):
    if len(expr) > _MAX_FORMULA_EXPR_BYTES:
        raise ValueError(f"Formula expression too long ({len(expr)} chars; max {_MAX_FORMULA_EXPR_BYTES})")
    # For time: use signal.alarm (Unix only) or threading.Timer.
    return eval(expr, globals_, locals_)  # noqa: S307
```

For a production-grade fix, run formula evaluation in an isolated subprocess with an explicit memory cap and a wall-clock timeout (e.g., 2s per expression). This also addresses F1.

---

### F3 · CRITICAL · Reliability — SFTP unknown-host failure mis-classified as `TransientError`; causes infinite retry

**File:** `src/decoy_engine/connectors/sftp.py` · `_wrap_sftp_error`, `_open_sftp`

**Issue:**  
When `DECOY_SFTP_KNOWN_HOSTS` is not set and `~/.ssh/known_hosts` does not exist (or does not contain the target server's key), `RejectPolicy` causes paramiko to raise:
```
paramiko.SSHException: Server 'hostname' not found in known_hosts
```
In `_wrap_sftp_error`:
```python
if isinstance(exc, paramiko.SSHException):
    return TransientError(f"SFTP protocol error: {type(exc).__name__}")
```
Because `SSHException` is the base class and the permanent subclasses are checked earlier, the unknown-host error falls into the generic `SSHException → TransientError` branch. The engine's retry loop fires, re-attempts the connection, gets the same error, and continues until it exhausts its attempt budget.

Cross-reference: a similar pattern was already caught and fixed for `BadHostKeyException` (QA-7 F4, 2026-06-01) — that fix ensured MITM-indicator errors are permanent. The same reasoning applies to "not in known_hosts".

**Impact:** A fully misconfigured SFTP environment burns the entire retry budget (wall time, connection threads) before failing. If the job runs in a large loop, this can saturate the SFTP server's connection pool with repeated failed auth attempts.

**Recommended fix:**  
```python
if isinstance(exc, paramiko.SSHException):
    msg = str(exc).lower()
    if "not found in known_hosts" in msg or "no hostkey" in msg:
        return PermanentError(
            f"SFTP host key not in known_hosts: {exc}. "
            "Populate ~/.ssh/known_hosts or set DECOY_SFTP_KNOWN_HOSTS "
            "to the path of a pre-populated known_hosts file."
        )
    return TransientError(f"SFTP protocol error: {type(exc).__name__}")
```

---

### F4 · HIGH · Correctness — S3 permanent-error set contains HTTP numeric codes that botocore never emits

**File:** `src/decoy_engine/connectors/s3.py` · `_PERMANENT_S3_ERROR_CODES`, `_wrap_client_error`

**Issue:**  
```python
_PERMANENT_S3_ERROR_CODES = frozenset({
    "NoSuchBucket",
    "NoSuchKey",
    "404",          # ← dead code
    "403",          # ← dead code
    "AccessDenied",
    ...
})
```
botocore `ClientError.response["Error"]["Code"]` carries XML error-code strings (`"NoSuchKey"`, `"AccessDenied"`) — never bare HTTP status integers. The strings `"403"` and `"404"` will never match. The consequence depends on what error code an edge-case server (S3-compatible endpoint, custom gateway) returns:

- A non-standard gateway that returns `Code: 403` (numeric string) instead of `AccessDenied` would be classified as `TransientError` by `_wrap_client_error` and retried, amplifying requests against a server that has already rejected the credentials.
- A non-standard `Code: 404` would be retried rather than failing immediately.

For standard AWS endpoints the real codes (`"AccessDenied"`, `"NoSuchKey"`) are already present, so this is a latent bug — but one that can bite on any S3-compatible service with different error-code conventions.

**Fix:** Remove `"403"` and `"404"` from the frozenset. Add a comment explaining that botocore uses XML code strings, not HTTP status integers:
```python
_PERMANENT_S3_ERROR_CODES = frozenset({
    # botocore Error.Code values from the S3 XML error body.
    # These are not HTTP status codes; never use numeric strings here.
    "NoSuchBucket",
    "NoSuchKey",
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "AllAccessDisabled",
})
```

Also audit: `_wrap_client_error`'s final fallback converts any non-boto exception to `PermanentError`. Unknown exceptions are more likely network-layer transients; the default should be `TransientError` with a WARNING log:
```python
# Anything else: assume transient so an ephemeral error doesn't abort a long job.
return TransientError(f"Unexpected S3 error (treating as transient): {exc}")
```

---

### F5 · HIGH · Correctness — `SFTPFileSource.list()` is single-directory; `S3FileSource.list()` is recursive

**File:** `src/decoy_engine/connectors/sftp.py` · `SFTPFileSource.list`

**Issue:**  
`SFTPFileSource.list()` calls `sftp.listdir_attr(target_dir)` — one directory, non-recursive, skips subdirectories. `S3FileSource.list()` uses a `list_objects_v2` paginator that returns all keys under the configured prefix, arbitrarily deep. A pipeline written against S3 that is switched to SFTP will silently process only the top-level directory and miss all nested files. No error is raised; the job completes with fewer records.

This is a contract mismatch at the `FileSource` SDK level. The SDK makes no guarantee about recursion, but customers will assume parity between connectors given the identical method signature.

**Fix:** Implement recursive walking in `SFTPFileSource.list()`:
```python
def list(self, prefix: str | None = None) -> Iterator[FileMeta]:
    sftp = self._connect()
    root = _join_path(self.config.base_path, prefix or "")
    yield from self._list_recursive(sftp, root)

def _list_recursive(self, sftp, directory: str) -> Iterator[FileMeta]:
    try:
        entries = sftp.listdir_attr(directory or ".")
    except Exception as exc:
        raise _wrap_sftp_error(exc) from exc
    for entry in entries:
        full_path = f"{directory.rstrip('/')}/{entry.filename}" if directory else entry.filename
        if entry.st_mode is not None and stat_lib.S_ISDIR(entry.st_mode):
            yield from self._list_recursive(sftp, full_path)
        else:
            yield FileMeta(
                path=full_path,
                size=entry.st_size,
                content_type=None,
                modified=(
                    datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc).isoformat()
                    if entry.st_mtime is not None else None
                ),
            )
```
If flat-list semantics are intentional (e.g., for SFTP servers with deep hierarchies where recursion is too expensive), document it explicitly in the class docstring and add a `recursive: bool = True` config field.

---

### F6 · HIGH · Correctness — Composite namespace separator `"__"` collides with SQL column names containing double-underscore

**File:** `src/decoy_engine/relationships/_namespace.py` · `build_namespace_registry`

**Issue — namespace derivation:**  
Composite group namespaces are derived as:
```python
ns = f"composite/{tbl}/{'__'.join(grp)}"
```
If a table has columns `("a__b", "c")`, the namespace is `"composite/t/a__b__c"`. So is the namespace for columns `("a", "b__c")`. Two distinct composite groups in the same table map to the same namespace string. Columns from both groups are masked into the same namespace — they will produce identical outputs for identical inputs, breaking the independence guarantee between the two groups.

**Issue — `declared_by` parsing:**  
```python
cols = tuple(col_part.split("__")) if "__" in col_part else (col_part,)
```
A column named `"order__id"` (common in snake-case schemas) is parsed as the composite `("order", "id")`. The registry sees a 2-column tuple when the config declared one column. Any subsequent `for_column(table, ("order__id",))` lookup returns `None` (miss), because the key is `(table, ("order", "id"))` instead of `(table, ("order__id",))`.

**Impact:** Silent incorrect masking. A column intended to mask independently gets grouped with another column, or a single-column FK lookup misses entirely (falls back to `None` → may raise `namespace_missing`).

**Fix:** Use a separator that cannot appear in SQL identifiers. Unicode null (`\x00`) or a JSON-array format are safe options:
```python
# In build_namespace_registry, composite namespace derivation:
ns = f"composite/{tbl}/" + "\x00".join(grp)

# In declared_by parsing, switch from __-split to an explicit delimiter:
# Config format change: use "." for table and "|" for composite columns:
# "my_table.col1|col2" instead of "my_table.col1__col2"
table, col_part = entry.split(".", 1)
cols = tuple(col_part.split("|"))
```
This is a config schema change; coordinate with the YAML spec and migration guide.

---

### F7 · MEDIUM · Performance — `_encrypt` rebuilds `charset_set` on every row in the FPE hot path

**File:** `src/decoy_engine/transforms/fpe.py` · `FPEStrategy._encrypt`

**Issue:**  
```python
def _encrypt(self, val, key, charset, tweak, preserve_sep, validate_luhn, column_name):
    charset_set = set(charset)  # ← O(|charset|) allocation per row
```
For a 1M-row column, this is 1M set constructions. For the `digits` charset (10 chars) the allocations are fast but not free. For custom charsets with hundreds of characters, the cost is measurable.

The same pattern appears in `_fpe_pure`'s fallback:
```python
char_to_idx = _CHARSET_INDEX.get(charset)
if char_to_idx is None:
    char_to_idx = {ch: i for i, ch in enumerate(charset)}  # per call
```
For custom charsets, this is one dict construction per value.

**Fix:** Compute both `charset_set` and the custom `char_to_idx` once in `apply()` and pass them through:
```python
def apply(self, column, rule):
    ...
    charset_set = frozenset(charset)  # build once
    char_to_idx = _CHARSET_INDEX.get(charset) or {ch: i for i, ch in enumerate(charset)}
    encrypted = [
        self._encrypt(s, key, charset, charset_set, char_to_idx, tweak, ...)
        for s in non_na_str
    ]
```
Estimated improvement for 1M rows with `digits` charset: ~30–50ms saved (verifiable with `timeit` on the `_encrypt` call with vs. without `charset_set` pre-built).

---

### F8 · MEDIUM · Correctness — `check_orphan_fk_policy_completeness` silently degrades on non-list `config["relationships"]`

**File:** `src/decoy_engine/relationships/_graph.py` · `check_orphan_fk_policy_completeness`

**Issue:**  
```python
config_relationships = config.get("relationships", [])
if isinstance(config_relationships, list):
    for idx, entry in enumerate(config_relationships):
        ...
```
If `relationships:` is parsed from YAML as a `dict` (e.g., due to a YAML anchor deserialization quirk or an editor mistake), the `isinstance` check is `False` and the entire config-lookup build is skipped. `config_lookup` remains empty. Every subsequent profile relationship then raises:
```
PlanCompileError(code='orphan_fk_policy_missing', ..."no matching entry")
```
The actual problem — `config.relationships` has the wrong type — is never reported. The operator sees N errors about missing policies for every relationship, with no clue that the YAML structure is wrong.

**Fix:**
```python
if config_relationships and not isinstance(config_relationships, list):
    raise PlanCompileError(
        code="config_schema_error",
        path="relationships",
        message=(
            f"config.relationships must be a list, got "
            f"{type(config_relationships).__name__}. Check your YAML structure."
        ),
    )
```

---

### F9 · MEDIUM · Reliability — SFTP liveness probe swallows permanent server errors on reconnect

**File:** `src/decoy_engine/connectors/sftp.py` · `_SFTPMixin._connect`

**Issue:**  
```python
try:
    self._sftp.stat(".")
    return self._sftp
except Exception:  # ← bare catch-all
    # Stale session; tear down + reconnect below.
```
If `stat(".")` raises a `PermissionError` (server-side, permanent) or a `FileNotFoundError` for a restricted CWD, the bare `except Exception` incorrectly treats it as a stale-session indicator, tears down the connection, reconnects, and calls `stat(".")` again. This loop continues indefinitely because the real problem is a server-side permanent condition, not a dead socket.

The intent is to distinguish "dead TCP connection" (→ reconnect) from "live but slow server" (→ trust the cached session). Session-liveness errors in paramiko are `SSHException`, `EOFError`, and `socket.error` — not `PermissionError` or `FileNotFoundError`.

**Fix:**
```python
import socket as _socket

try:
    self._sftp.stat(".")
    return self._sftp
except (paramiko.SSHException, EOFError, _socket.error):
    # Dead session; reconnect.
    pass
except Exception:
    # Live session, real server error — propagate without reconnecting.
    raise
```

---

### F10 · LOW · Design — `_wrap_client_error` S3 fallback defaults to `PermanentError` for unknown exceptions

**File:** `src/decoy_engine/connectors/s3.py` · `_wrap_client_error`

**Issue:**  
```python
# Anything else: assume permanent so callers don't loop on programmer error.
return PermanentError(f"Unexpected S3 error: {exc}")
```
The comment says this prevents retry loops on programmer errors. But the caller is `_wrap_client_error`, invoked only inside try/except blocks wrapping real boto3 calls. An unknown exception reaching this branch is more likely a novel network error than a programming error. Defaulting to `PermanentError` causes the engine to abort the job immediately, with no retry, for an error that might have recovered on the next attempt.

**Fix:** Default to `TransientError` with a WARNING log:
```python
import logging as _logging
_log = _logging.getLogger(__name__)

# Final branch:
_log.warning("Unexpected S3 exception type %s; treating as transient: %s", type(exc).__name__, exc)
return TransientError(f"Unexpected S3 error: {exc}")
```

---

### F11 · LOW · Performance — `formula.py` uses `Series.apply` where a list comprehension is cheaper

**File:** `src/decoy_engine/transforms/formula.py` · `FormulaStrategy.apply`

**Issue:**  
```python
return column.apply(
    lambda v: v if pd.isna(v) else safe_eval(expr, scope, {"value": v})
)
```
`Series.apply` boxes each Python scalar, calls the lambda, then unboxes back into the Series. This dispatch overhead is ~2–3× slower than a list comprehension for columns where the per-element work (eval) is fast or moderate. The FPE and date_shift strategies already moved off `Series.apply` for this reason.

**Fix:**
```python
na_mask = column.isna()
values = column.tolist()
result_vals = [
    v if is_na else safe_eval(expr, scope, {"value": v})
    for v, is_na in zip(values, na_mask.tolist())
]
return pd.Series(result_vals, index=column.index, dtype=object)
```
For a 500K-row column where the formula is `"str(value).upper()"`, expect ~40–60% throughput improvement (verify with `timeit` against the current `apply` path).

---

### F12 · LOW · Performance — `_log_stats` runs `nunique()` on every masking operation regardless of log level

**File:** `src/decoy_engine/transforms/base.py` · `BaseMaskingStrategy._log_stats`

**Issue:**  
```python
unique_original = column.nunique()   # O(n) full-column pass
unique_result = result.nunique()     # O(n) full-column pass
self.logger.debug(...)               # only emits if DEBUG enabled
```
Two full O(n) passes run unconditionally for every strategy application, even in INFO or WARNING log modes where the output is discarded. For a 5M-row column, each `nunique()` call takes ~150–300ms. Over dozens of masked columns in a large job, this can add seconds of invisible overhead.

**Fix:** Guard the expensive computation behind the log level:
```python
if self.logger.isEnabledFor(logging.DEBUG):
    unique_original = column.nunique()
    unique_result = result.nunique()
    self.logger.debug(
        f"Unique values: {unique_original} original, {unique_result} masked"
    )
```

---

### NIT-1 · Nit · Design — `date_shift._detect_format` has no early exit after first matching format

**File:** `src/decoy_engine/transforms/date_shift.py` · `_detect_format`

The format detection loop tries all 11 formats in `_COMMON_FORMATS` against up to 200 sample rows. If the first format (e.g., `"%Y-%m-%d"`) matches all 200 rows, the other 10 formats are still attempted (2000 wasted `strptime` calls). When only one format matches, there is no ambiguity to warn about. Restructure to try formats, collect candidates early-exit on `len(candidates) == 1` after the first full-sample pass, or break after finding the first candidate if no ambiguity check is needed:

```python
# Early exit variant: warn only when >1 candidate survives
for fmt in _COMMON_FORMATS:
    if all(safe_strptime(v.strip(), fmt) for v in sample):
        candidates.append(fmt)
```
Impact: ~10× fewer `strptime` calls on clean ISO-date columns (the common case).

---

### NIT-2 · Nit · Design — `NamespaceRegistry.__post_init__` bypasses frozen dataclass via `object.__setattr__`

**File:** `src/decoy_engine/relationships/_namespace.py` · `NamespaceRegistry.__post_init__`

`object.__setattr__(self, "_index", built)` is the standard pattern for mutating a `frozen=True` dataclass field in `__post_init__`. It works but is fragile: if `_index` is renamed or if the dataclass gains slots (`__slots__`), the bypass silently fails or raises. Consider using `dataclasses.field(init=False)` + making `_index` a regular (non-frozen) field, or converting `NamespaceRegistry` to a regular class with `__init__` + a `@staticmethod` builder.

---

### NIT-3 · Nit · Reliability — SFTP `write` does not create parent directories

**File:** `src/decoy_engine/connectors/sftp.py` · `SFTPFileSink.write`

If `full_path = "/data/output/2026/06/masked.csv"` and `/data/output/2026/06/` does not exist on the SFTP server, `sftp.open(full_path, "wb")` raises `FileNotFoundError` (wrapped as `PermanentError`). The error message says "path not found" with no indication that the parent directory is missing. Add an `_makedirs` helper using `sftp.mkdir` in a try/except loop, or document the requirement explicitly in the class docstring so operators know to pre-create directory trees.

---

## 3. Performance Notes

| Area | Bottleneck class | Complexity | Profiling approach |
|------|-----------------|------------|--------------------|
| FPE `_fpe_pure` | CPU (HMAC-SHA256 × 8 rounds × N rows) | O(N × n × rounds) where n = string length | `timeit` the `_encrypt` call at 100K rows; `py-spy` to confirm HMAC dominates |
| FPE `_encrypt` `charset_set` rebuild | Memory allocation | O(|charset|) per row — wasted | Addressed in F7; `timeit` before/after to quantify |
| `date_shift` HMAC calls | CPU | O(N) — irreducible per-value crypto | Already optimized (valid_mask filter per session review notes); `scalene` for per-line CPU |
| `formula` `Series.apply` dispatch | CPU (Python boxing) | O(N) with constant overhead per row | `%timeit col.apply(lambda v: ...)` vs. list comprehension at 500K rows |
| `_log_stats` `nunique()` | CPU + memory | O(N) × 2 per strategy | `cProfile` on a masked run; look for `nunique` in top entries |
| SFTP recursive `list` (after F5 fix) | I/O | O(depth × files per dir) | Benchmark against a controlled SFTP tree with known depth |

The masking pipeline bottleneck at realistic scale (1M+ rows) is the per-value crypto inside FPE and date_shift. Both strategies have already been optimized to move all vectorizable work (null checks, type casting) outside the per-row loop. The Feistel round itself (8 × HMAC-SHA256 per value) is irreducible without parallelism. If throughput targets require >10M rows/sec, evaluate a Polars strategy variant that batches values into a native thread pool.

---

## 4. Suggested Tests

### Correctness / determinism

```python
# FPE: same seed + same key → byte-identical output across process restarts
def test_fpe_determinism_across_runs():
    key = b"\x00" * 32
    tweak = b"col"
    s = "1234567890"
    out1 = _fpe_pure(s, key, _CHARSETS["digits"], tweak, False)
    out2 = _fpe_pure(s, key, _CHARSETS["digits"], tweak, False)
    assert out1 == out2

# FPE: verify Feistel is bijective over a small charset
def test_fpe_bijection_digits_len2():
    key = b"\x01" * 32
    tweak = b"t"
    charset = "0123456789"
    inputs = [f"{i:02d}" for i in range(100)]
    outputs = [_fpe_pure(s, key, charset, tweak, False) for s in inputs]
    assert len(set(outputs)) == 100  # all distinct

# FPE: single-character is a bijection
def test_fpe_single_char_bijection():
    key = b"\x02" * 32
    tweak = b"c"
    charset = "0123456789"
    outputs = [_fpe_pure(d, key, charset, tweak, False) for d in charset]
    assert len(set(outputs)) == len(charset)

# DateShift: HMAC path stable across two calls
def test_date_shift_keyed_determinism():
    key = b"\x03" * 32
    shift1 = _shift_for_value_keyed(key, "2023-01-15", -365, 365)
    shift2 = _shift_for_value_keyed(key, "2023-01-15", -365, 365)
    assert shift1 == shift2

# Namespace: composite separator collision
def test_composite_namespace_no_collision_double_underscore_column():
    # Column named 'a__b' in table 't' should NOT collide with composite ('a', 'b')
    ...
```

### Security / sandbox

```python
# safe_eval: object-literal escape attempt should be blocked (currently FAILS — tracks F1)
def test_safe_eval_blocks_subclass_escape():
    with pytest.raises(Exception):
        safe_eval(
            "().__class__.__bases__[0].__subclasses__()",
            MASK_GLOBALS,
            {},
        )

# safe_eval: large allocation should be blocked (tracks F2)
def test_safe_eval_blocks_memory_bomb():
    with pytest.raises((MemoryError, ValueError)):
        safe_eval('"A" * (10 ** 9)', MASK_GLOBALS, {})
```

### Connector edge cases

```python
# S3: ConnectTimeoutError → TransientError (regression guard for the existing fix)
def test_s3_connect_timeout_is_transient():
    from botocore.exceptions import ConnectTimeoutError
    result = _wrap_client_error(ConnectTimeoutError(endpoint_url="http://x"))
    assert isinstance(result, TransientError)

# S3: "403" string (dead code) is no longer in the frozenset after fix
def test_s3_permanent_codes_no_numeric_strings():
    assert "403" not in _PERMANENT_S3_ERROR_CODES
    assert "404" not in _PERMANENT_S3_ERROR_CODES

# SFTP: known_hosts missing → PermanentError, not TransientError (tracks F3)
def test_sftp_unknown_host_is_permanent():
    import paramiko
    exc = paramiko.SSHException("Server 'x.y.z' not found in known_hosts")
    result = _wrap_sftp_error(exc)
    assert isinstance(result, PermanentError)

# SFTP: liveness probe PermissionError should propagate, not reconnect (tracks F9)
def test_sftp_connect_probe_permission_error_propagates():
    ...
```

### Relationship graph

```python
# Cycle detection
def test_fk_cycle_raises():
    rels = (
        Relationship(parent_table="a", parent_columns=("id",), child_table="b", ...),
        Relationship(parent_table="b", parent_columns=("id",), child_table="a", ...),
    )
    with pytest.raises(PlanCompileError, match="fk_cycle"):
        build_relationship_graph(rels, ...)

# Multi-parent FK rejection
def test_multi_parent_fk_raises():
    rels = (
        Relationship(parent_table="p1", parent_columns=("id",), child_table="c", child_columns=("fk",), ...),
        Relationship(parent_table="p2", parent_columns=("id",), child_table="c", child_columns=("fk",), ...),
    )
    with pytest.raises(PlanCompileError, match="multi_parent_fk_unsupported"):
        build_relationship_graph(rels, ...)

# Orphan policy duplicate conflict (QA-8 F3 regression)
def test_duplicate_orphan_policy_conflict_raises():
    config = {"relationships": [
        {"parent": {"table": "p", "columns": ["id"]}, "orphan_policy": "preserve",
         "children": [{"table": "c", "columns": ["fk"]}]},
        {"parent": {"table": "p", "columns": ["id"]}, "orphan_policy": "fail",
         "children": [{"table": "c", "columns": ["fk"]}]},
    ]}
    with pytest.raises(PlanCompileError, match="orphan_fk_policy_duplicate"):
        check_orphan_fk_policy_completeness(config, ...)

# Non-list relationships config → clear error (tracks F8)
def test_orphan_policy_check_non_list_config_raises():
    config = {"relationships": {"key": "value"}}  # dict instead of list
    with pytest.raises(PlanCompileError, match="config_schema_error"):
        check_orphan_fk_policy_completeness(config, ...)
```

---

## 5. What's Good

- **FPE Feistel is mathematically correct.** The 8-round type-II Feistel over Z_(r^u) × Z_(r^v) is a provable bijection regardless of the round function. The single-character special case (keyed modular shift) is correct and bijective. The `_CHARSET_INDEX` pre-computation is a well-targeted O(n·r) → O(n) optimization.

- **Prior QA fixes are properly absorbed.** The `derive_key` failure path in both FPE and DateShift now raises instead of silently degrading to seed-only masking (QA F1/H2). The keyed date-shift path correctly skips null/parse-failed rows before incurring HMAC cost. The Feistel single-character bijection fix is documented and correct.

- **SFTP `BadHostKeyException` is already permanent.** QA-7 F4 correctly upgraded the MITM-indicator error before the generic `SSHException` branch. The fix pattern is sound; F3 in this report extends the same logic to the unknown-host case.

- **S3 multipart upload handles abort correctly.** The `try/except/abort` pattern in `S3FileSink.write` is correct: any exception after multipart initiation triggers `abort_multipart_upload` in a best-effort inner try/except that doesn't mask the original error. The part-number sequencing and ETag tracking are correctly handled.

- **Kahn's algorithm with heap queue gives stable topological order.** The QA-8 F1 fix replacing list-pop(0) + sort with `heapq` is correct and gives the same lexicographically-smallest ordering at O(n log n) instead of O(n² log n). The cycle detection (remaining nonzero indegree nodes) is correct and produces a useful error message.

- **`_column_key` failure is now explicit in both transforms.** Both `FPEStrategy._column_key` and `DateShiftStrategy._column_key` now raise `RuntimeError` on derive_key failure instead of falling through to legacy paths. This is the right contract for a production engine.

- **`make_mask_globals` correctly isolates per-formula RNG state.** The factory correctly rebinds `randint`, `choice`, and `random` to a seeded `random.Random` instance, and `FormulaStrategy.apply` builds a deterministic seed from `col_name | expr`. Two formula strategies in the same job no longer share global RNG state.
