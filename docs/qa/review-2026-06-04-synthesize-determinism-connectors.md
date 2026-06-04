# QA Review: synthesize.py determinism + connector correctness (2026-06-04)

**Reviewer:** Claude (QA session)
**Scope:** `generation/synthesize.py`, `relationships/_graph.py`,
           `relationships/_namespace.py`, `connectors/s3.py`, `connectors/sftp.py`,
           `internal/faker_setup.py`, `determinism/_derive.py`, `determinism/_hkdf.py`
**Branch:** `qa/review-2026-06-04-synthesize-determinism-connectors`
**Avoided:** `transforms/formula.py`, `transforms/text_redact.py`, `expressions.py`
           (covered by `qa/review-2026-06-04-expressions-formula-textredact`)

---

## Summary

The determinism layer (`_hkdf.py`, `_derive.py`) and FK graph (`_graph.py`,
`_namespace.py`) are well-engineered: correct RFC 5869 HKDF, length-prefixed
envelope, Kahn sort with min-heap tiebreaking, and O(1) namespace lookup. The
connectors (`s3.py`, `sftp.py`) are solid, with correct exception ordering and
proper secret handling via `SecretStr`. Two issues need action before any caller
depends on deterministic table-order output from `generate_tables`, and one
multi-worker race in `connectors/crypto.py` (platform-side; see the companion
platform review) is a latent data-loss risk on first boot.

**Single most important issue:** `_topo_sort` in `synthesize.py` iterates `set[str]`
dependency sets whose order is PYTHONHASHSEED-sensitive. The dict returned by
`generate_tables` therefore has a PYTHONHASHSEED-dependent insertion order, violating
the byte-stable output contract even though per-table content is seed-deterministic.

---

## Findings

### F1 — Medium | Determinism
**`_topo_sort` iterates `set[str]` — PYTHONHASHSEED-sensitive table ordering**

`generate_tables` builds `deps: dict[str, set[str]]` (line 115). `_topo_sort`
then iterates the set values via `iter(deps.get(start, ()))` (line 180). Python
set iteration order depends on `PYTHONHASHSEED`, which is re-randomised every
interpreter start (unless `PYTHONHASHSEED=0`). The DFS traversal therefore visits
sibling dependency nodes in a different order on each process, producing different
topological orderings for the same input graph.

**Impact:** Per-table content is unaffected (column seeds are key-derived, not
position-derived). The returned `out: dict[str, pa.Table]` has a
PYTHONHASHSEED-sensitive insertion order, so any caller that iterates the dict
positionally (`list(out.values())[i]`) or relies on Arrow record-batch ordering
sees different results across processes. This is a determinism contract violation.

**Fix:**
```python
# synthesize.py line 115: replace set with list
deps: dict[str, list[str]] = {}
for name, t in generate_by_name.items():
    d_set: set[str] = set()
    for col in t["generate_columns"]:
        if col.get("type") == "reference":
            ref = col["reference_table"]
            if ref not in generate_by_name:
                raise ValueError(...)
            d_set.add(ref)
    deps[name] = sorted(d_set)  # stable across PYTHONHASHSEED

# _topo_sort line 180: already receives an Iterable, no change needed
stack = [(start, iter(deps.get(start, ())))]
```
Sorting at `deps`-build time is O(k log k) per table (k = ref columns, usually 1-2)
and adds zero per-row cost.

**Verify:** `PYTHONHASHSEED=1 python -c "..." && PYTHONHASHSEED=2 python -c "..."`,
diff the returned dict key order on a config with multi-reference tables.

---

### F2 — Medium | Performance
**`_faker()` in `synthesize.py` constructs a fresh `Faker` per column when `locale` is set**

Lines 302-303:
```python
if locale:
    faker_inst = make_faker(locale)   # fresh construction every call
    pre_seed = None
```

`Faker()` construction registers ~200 providers and loads locale data: 50-200ms
per construction (the module-level comment on `_DEFAULT_FAKER` explicitly
documents this for the no-locale path and caches it). For a generate table with
30 per-locale columns this is 1.5-6s of pure constructor overhead — before any row
is generated. The `instance_default_locale` branch has the same problem (line 308:
`faker_inst = make_faker(instance_default_locale)` also uncached).

**Impact:** CPU-bound; linear in the number of per-locale columns. Becomes
noticeable above ~10 columns or when `generate_tables` is called repeatedly (API
request path).

**Fix:** Add a module-level locale cache alongside `_DEFAULT_FAKER`:
```python
_LOCALE_FAKER_CACHE: dict[str, Faker] = {}
_LOCALE_FAKER_LOCK = threading.Lock()

def _get_locale_faker(locale: str | list[str]) -> Faker:
    cache_key = str(locale)   # lists are unhashable; str(list) is stable
    with _LOCALE_FAKER_LOCK:
        if cache_key not in _LOCALE_FAKER_CACHE:
            _LOCALE_FAKER_CACHE[cache_key] = make_faker(locale)
        return _LOCALE_FAKER_CACHE[cache_key]
```
Replace `make_faker(locale)` and `make_faker(instance_default_locale)` with
`_get_locale_faker(locale)` / `_get_locale_faker(instance_default_locale)`.
The per-row `seed_instance` inside `_FAKER_CALL_LOCK` already overrides the
cached instance's seed state, so sharing the instance is safe (same pattern as
`_DEFAULT_FAKER`).

**Measure:** `timeit` a 50-column per-locale generate config before and after; expect
10-40x reduction for the locale-construction portion.

---

### F3 — Low | Reliability
**`SFTPFileSink.write()` — ambiguous multipart completion after transient network failure**

In `S3FileSink.write()` (same applies to the S3 connector; SFTP does not do
multipart), if `_complete_multipart` raises (line 382) due to a transient network
drop, the outer `except` block calls `abort_multipart_upload` (line 387). If the
server actually completed the upload before the acknowledgement was lost, the abort
call will fail with `NoSuchUpload` — which is silently swallowed (inner
`except Exception: pass`, line 393). The engine's retry policy then retries
`write()`, creating a potential double-write of the output file without any warning.

**Impact:** Masked output files could be silently overwritten with identical content
(idempotent in the success case) or, if the retry produces a different result due
to a transient source change, produce a corrupted output. No data loss, but the
silent double-write is unobservable.

**Fix:** Add a log line at minimum when the abort fails with a different error than
`NoSuchUpload`:
```python
except Exception as abort_exc:
    code = getattr(abort_exc, "response", {}).get("Error", {}).get("Code", "")
    if code != "NoSuchUpload":
        _log.warning("S3 abort_multipart failed (upload may be orphaned): %s", abort_exc)
```
The `WriteResult` returned already carries the key; a caller that checks the S3
object's ETag after retry can detect the double-write.

---

### F4 — Low | Design
**`check_orphan_fk_policy_completeness` silently skips malformed config entries**

Lines 342-354 in `_graph.py`: when a config relationship entry is malformed
(non-string `parent_table`, non-list `parent_cols`, or non-string `child_table` /
`child_cols`), the parser `continue`s without error. The function returns a lookup
that is missing those relationships. The subsequent check at lines 427-449 then
raises `PlanCompileError(code='orphan_fk_policy_missing')` with a message like
"the config has no matching relationship entry" — misleading, because the entry
exists but is malformed.

**Impact:** Operator sees a confusing error. For hand-edited YAML or config coming
from an API client with a bug, the real problem (malformed type in the config) is
hidden behind a "missing" error.

**Fix:** Replace silent `continue` with an explicit error for malformed entries:
```python
if not (isinstance(parent_table, str) and isinstance(parent_cols, list)
        and all(isinstance(c, str) for c in parent_cols)):
    raise PlanCompileError(
        code="invalid_relationship_config",
        path=f"relationships[{idx}]",
        message=(
            f"Relationship entry at index {idx} has a malformed parent block. "
            "Expected: parent.table (str) and parent.columns (list[str])."
        ),
    )
```
Same treatment for the child block.

---

### F5 — Low | Reliability
**`SFTPMixin._connect()` — `self._sftp` / `self._client` accessed without a lock**

`_connect()` reads and writes `self._sftp` / `self._client` (lines 228-248)
without synchronisation. If two threads call `open()` concurrently on the same
connector instance, both can see `self._sftp is None`, both call `_open_sftp()`,
and the second assignment overwrites the first, leaking the first SSH connection.

**Impact:** Low in practice — connector instances are per-job today. If a future
sprint introduces connection pooling or shared connector objects across concurrent
requests, this becomes a resource leak.

**Fix:** Add a `threading.Lock()` to `_SFTPMixin.__init__` and hold it in `_connect()`.
Alternatively, document that `_SFTPMixin` instances are not thread-safe and must not
be shared across threads.

---

### F6 — Nit | Performance
**`NamespaceRegistry.members()` is O(N) while `for_column` is O(1)**

`members(namespace)` (line 155) scans `self.bindings` linearly, inconsistent with
the O(1) `_index` added in QA-8 F2. For typical pipeline sizes (tens of namespaces)
this is negligible, but the asymmetry will surprise a future author who sees the
`_index` optimisation in `for_column`.

**Fix:** Extend `__post_init__` to also build a
`_ns_index: dict[str, tuple[...]]` mapping namespace -> declared_by, and use it in
`members()`. Same `object.__setattr__` bypass pattern.

---

## Performance Notes

**`_topo_sort`:** O(V + E) DFS where V = generate tables, E = reference columns.
Negligible at realistic sizes (< 1000 tables). The recursion-limit fix (iterative
DFS) is correct and complete.

**`generate_tables` inner loop:** The per-table `dict comprehension` at line 145
calls `_generate_column` for each column synchronously. With 50 columns and 1M
rows, the bottleneck is in `_apply_null_probability` (random.seed + draw per row,
O(n) per column). Profile with:
```
python -m cProfile -s cumulative -c "from decoy_engine.generation.synthesize import generate_tables; ..."
```

**Faker construction (F2):** The 50-200ms Faker construction cost is CPU-bound.
Profile with `timeit` measuring `make_faker("de_DE")` in isolation to establish the
baseline before caching.

**`_FAKER_CALL_LOCK` contention:** All Faker generation (default, locale, and
instance-default-locale paths) is serialised through `_FAKER_CALL_LOCK`. For a
workload with many concurrent `generate_tables` calls this becomes a global
throughput bottleneck. The V2.1 per-call-fresh-Faker plan removes the lock; until
then, measure contention with `py-spy` or `threading.Lock` subclass that counts
acquires.

---

## Suggested Tests

1. **Determinism / F1:** Property-based test: for `config` with tables
   `A -> B` and `A -> C` (multi-reference), run `generate_tables` 100 times with
   `PYTHONHASHSEED` set to each of 1-10 via `subprocess`. Assert
   `list(result.keys())` is identical across all runs.

2. **Perf / F2:** Micro-benchmark: `timeit` a 30-column per-locale generate config
   before and after the locale-caching fix. Assert construction time drops > 5x.

3. **F3:** Integration test: mock `S3FileSink._complete_multipart` to raise an
   exception for one specific `upload_id`; verify the abort is attempted and that
   re-running `write()` succeeds and produces the correct output (no torn file).

4. **F4:** Unit test: pass a config with a malformed relationship entry
   (`parent_cols: "not_a_list"`) to `check_orphan_fk_policy_completeness`; assert
   `PlanCompileError(code='invalid_relationship_config')` is raised, not
   `orphan_fk_policy_missing`.

5. **Regression for F1 fix:** Existing `tests/integration/golden/test_execution_e2e.py`
   and any reference-column test should run clean after the `set` -> `sorted list`
   change. The byte output must be identical under both `PYTHONHASHSEED=0` and
   `PYTHONHASHSEED=random`.

---

## What's Good

- **HKDF implementation** (`_hkdf.py`): textbook RFC 5869 Extract + Expand on stdlib
  HMAC. Empty-salt rejection (QA-10 F12) and RFC max-length guard are correct.
  Reference-vector unit tests pin correctness.

- **Determinism envelope** (`_derive.py`): length-prefixed envelope prevents
  injection collisions. Version byte is in the HMAC input (not the HKDF salt) —
  the docstring correctly explains why: salt binds "what primitive" while the byte
  binds "which envelope shape." `DeriveContext` amortises HKDF cost correctly.

- **`build_relationship_graph`**: Kahn's algorithm with `heapq` min-heap (O(n log n),
  same lexicographic ordering as sorted-list Kahn but O(n log n) instead of
  O(n^2 log n)). Cycle detection is correct; cycle message names the participating
  nodes.

- **`_wrap_client_error` in `s3.py`**: specific exception subclasses (`ConnectTimeout`,
  `ReadTimeout`) checked before the generic `ClientError`, so timeout errors are
  correctly classified as transient. Pre-fix ordering bug was QA 2026-05-31 F1.

- **SFTP host-key policy**: `RejectPolicy` + `load_host_keys` from a configurable
  known-hosts path. The MITM-safe pattern (QA-7 F4 closure) is correct and the
  specific-before-generic exception ordering in `_wrap_sftp_error` is right.

- **`atomic_swap_db_providers`**: closes the read-window race (QA-internal F1) with
  a single locked swap. The copy-on-registration snapshot pattern prevents caller
  mutation from affecting generation state.

- **`build_namespace_registry`**: iterates `sorted(namespaces_block.keys())` (line
  202) to guarantee deterministic processing order regardless of YAML dict ordering.

- **SEC.1 `simpleeval` propagation confirmed**: `generators/columns.py` imports
  `safe_eval` from `decoy_engine.expressions` for `_eval_formula_inline`, so
  generate-mode formula columns reach the sandboxed evaluator, not raw `eval()`.
