# QA Review: connectors (S3 / GCS / SFTP) + relationships graph + namespace registry

**Date:** 2026-06-06  
**Reviewer:** Claude (QA session)  
**Scope:** `src/decoy_engine/connectors/{s3,gcs,sftp}.py`, `src/decoy_engine/relationships/{_graph,_namespace}.py`  
**Prior sessions avoided:** nested/shuffle/bucketize/derive (06-06), pandas-adapter-postval (06-06), synthesize-generation (06-06), session-execution-pipeline (06-06)

---

## 1. Summary

The connector layer (S3, GCS, SFTP) is well-structured: error classification, streaming semantics, and retry/abort handling are all correct. The relationships graph and namespace registry are clean, deterministic, and pass the "same seed -> same output" invariant. The most significant actionable finding is a **performance regression in the SFTP sink** (no write pipelining, causing throughput collapse on high-latency links) and a **documentation/design gap** in the S3/GCS connectors (the `head()`/`open()` prefix-bypass is intentional but asymmetric with `list()`). No correctness or determinism bugs were found in the relationship graph or namespace registry.

---

## 2. Findings

### F1 (High) -- Design | S3 + GCS: `head()` and `open()` bypass config prefix

**Location:** `connectors/s3.py::S3FileSource.head()`, `connectors/s3.py::S3FileSource.open()`, `connectors/gcs.py::GCSFileSource.head()`, `connectors/gcs.py::GCSFileSource.open()`

**Issue:** `list()` composes the config prefix with the call-level prefix via `_join_key(self.config.prefix, ...)`. `head()` and `open()` treat the `path` argument as an absolute key and bypass the prefix entirely. This is explicitly documented in the docstrings: "path is interpreted as an absolute S3 key (no prefix joining). This matches what `list()` returned."

However the asymmetry is a footgun. A caller that constructs a path independently (e.g., from a manifest file, not from a `list()` iterator) and passes it to `head()` or `open()` will silently target the wrong key -- there is no error; they just read from the root of the bucket instead of the scoped prefix.

**Impact:** Silent data corruption or reads of unexpected objects for callers who do not know to pre-join the prefix. In the engine's own usage (iterate `list()`, pass key to `open()`) this is fine. External callers of the SDK are at risk.

**Recommended fix:** Document the contract more prominently at the class level (not just in `open()`'s docstring) -- or add a `resolve_key(path)` helper that callers can use to construct absolute keys correctly. A typed `AbsoluteKey` vs `RelativePath` newtype would catch misuse statically but is likely overkill here.

```python
# In class S3FileSource, add a class-level note:
# NOTE: list() returns absolute keys (prefix already joined).
# open() and head() accept absolute keys only.
# Use _join_key(self.config.prefix, path) to convert a relative path.
```

---

### F2 (Medium) -- Performance | SFTP sink: no write pipelining

**Location:** `connectors/sftp.py::SFTPFileSink.write()`, around the chunk write loop

**Issue:** `SFTPFileSink.write()` calls `remote_fp.write(chunk)` in a serial loop. Paramiko's SFTP file defaults to non-pipelined mode: each `write()` call waits for a server acknowledgment before issuing the next one. On a link with 50 ms RTT, a 100 MB file (100 chunks at 1 MB each) takes at least 5 seconds in pure RTT overhead alone, regardless of available bandwidth.

Paramiko provides `SFTPFile.set_pipelined(True)` which enables asynchronous window filling, matching `sftp(1)` throughput behavior. Not calling it is a significant regression for any non-LAN SFTP usage.

**Impact:** Throughput collapse on high-latency links (WAN, cross-region, VPN). For 100 MB at 50 ms RTT: ~5 s overhead without pipelining vs. effectively zero overhead with pipelining. For 1 GB files, the overhead is 50+ seconds of pure protocol delay.

**Recommended fix:**

```python
try:
    remote_fp = sftp.open(full_path, "wb")
except Exception as exc:
    raise _wrap_sftp_error(exc) from exc

remote_fp.set_pipelined(True)   # <-- add this line

try:
    for chunk in chunks:
        ...
```

`set_pipelined()` must be called before the first `write()`. After the last write, the implicit `remote_fp.close()` drains the pipeline. No other changes needed.

**Verification:** `timeit` a 50 MB file write via a paramiko SFTP session with artificial latency (`tc netem delay 50ms`). Compare throughput with and without `set_pipelined(True)`.

---

### F3 (Nit) -- Reliability | S3 + GCS: `_client_or_build()` not thread-safe

**Location:** `connectors/s3.py::S3FileSource._client_or_build()`, `connectors/s3.py::S3FileSink._client_or_build()`, same pattern in `connectors/gcs.py`

**Issue:**

```python
def _client_or_build(self):
    if self._client is None:
        self._client = _build_s3_client(self.config)
    return self._client
```

Two threads sharing the same source/sink instance can both observe `self._client is None`, both call `_build_s3_client`, and one overwrites the other's result. In CPython, the attribute write is atomic under the GIL, so both threads end up using the same (winning) client. boto3 clients are thread-safe for concurrent operations, so this is benign in practice.

**Impact:** Double-build cost (negligible). If a non-CPython runtime drops the GIL guarantee, a reference-counting bug could manifest.

**Recommended fix (low priority):** If thread-safe lazy init is desired, use `threading.Lock`. If the engine guarantees one connector per worker thread, a code comment documenting that assumption is sufficient.

---

### F4 (Nit) -- Performance | `_graph.py`: redundant `sorted()` inside heapq loop

**Location:** `relationships/_graph.py`, Kahn's algorithm loop, ~line 205

```python
for nxt in sorted(out_edges[node]):
    indegree[nxt] -= 1
    if indegree[nxt] == 0:
        heapq.heappush(queue, nxt)
```

**Issue:** `sorted(out_edges[node])` iterates the successors in sorted order before deciding whether to push them to the heap. The heap handles ordering on its own -- `heappush` maintains the invariant. The `sorted()` call adds O(k log k) work per node where k is the out-degree. For small graphs this is negligible; for graphs with high-fanout nodes (e.g., a central `users` table referenced by 50 FK children) it adds unnecessary sort work on every iteration.

**Impact:** Minor CPU overhead on large pipelines with high-fanout FK graphs. The sorted order does not affect correctness because the heap always pops the minimum.

**Recommended fix:** Remove `sorted()`. The existing heap guarantees lexicographic ordering.

```python
for nxt in out_edges[node]:   # heap handles ordering
    indegree[nxt] -= 1
    if indegree[nxt] == 0:
        heapq.heappush(queue, nxt)
```

---

### F5 (Low) -- Correctness | Namespace registry: derived composite namespace can collide with explicit declaration

**Location:** `relationships/_namespace.py::build_namespace_registry`, Step 2.5, composite binding

```python
ns = explicit if explicit is not None else f"composite/{tbl}/{'__'.join(grp)}"
```

**Issue:** When a composite group has no explicit namespace, the registry assigns the derived name `composite/{tbl}/{col1}__{col2}`. If a user's explicit `namespaces:` block declares a namespace with that exact name (e.g., `composite/orders/id__type`), the derived binding and the explicit binding collide. The collision detection in `build_namespace_registry` checks `existing = column_owner.get((tbl, grp))` before registering -- but only for the group key, not for the derived namespace name itself. Two groups in different tables could independently derive the same namespace name if they have the same sorted column names.

**Impact:** Silent namespace aliasing: two logically independent composite groups would share a namespace and mask to the same value family, breaking cross-table independence. This requires an unlikely naming coincidence but is worth guarding.

**Recommended fix:** Prefix the derived namespace with a non-user-reachable sentinel:

```python
ns = explicit if explicit is not None else f"__composite_auto__/{tbl}/{'__'.join(grp)}"
```

Or: validate at registry build time that no derived name already exists as an explicit namespace key in the config.

---

### F6 (Low) -- Design | `NamespaceRegistry.__post_init__` uses `object.__setattr__` bypass

**Location:** `relationships/_namespace.py::NamespaceRegistry.__post_init__`

```python
object.__setattr__(self, "_index", built)
```

**Issue:** This is the standard CPython idiom for mutating a frozen dataclass field after `__init__`. It is correct. However, it silently bypasses the type-checking and invariant-checking that the frozen annotation is supposed to enforce. A future developer who doesn't recognize the pattern might either remove it (breaking the index) or add a second such bypass for another field.

**Impact:** Low risk. The comment above the line explains the intent. Adding a brief invariant note would make the bypass reason unmistakable.

**Recommended fix:** Add a one-line comment:

```python
# Frozen dataclass: use object.__setattr__ to set the index field after construction.
object.__setattr__(self, "_index", built)
```

---

## 3. Performance Notes

| Path | Bottleneck | Complexity | Notes |
|---|---|---|---|
| `S3FileSource.open()` | I/O bound | O(chunks) | 1-MB chunks amortize boto3 per-chunk overhead well. |
| `S3FileSink.write()` multipart | I/O + memory | O(chunks) | 5-MB buffer is the AWS minimum part size; correct. |
| `SFTPFileSink.write()` | Network RTT (no pipelining) | O(chunks * RTT) | Critical; see F2. |
| `build_relationship_graph` Kahn | CPU | O(E log N) after heapq fix | Current O(E + N log N) with sorted() inside loop. |
| `build_namespace_registry` | CPU | O(N * K) where K = columns per namespace | Fast for typical configs. |
| `NamespaceRegistry.for_column` | CPU | O(1) after QA-8 F2 index fix | Pre-fix was O(B*K); already patched per history. |
| `RelationshipGraph.parents_of` | CPU | O(edges) | Acceptable at <1000 edges; add index if engine S9 profiling flags it. |

**What to profile:** For a 500-table pipeline, run `python -m cProfile -o out.prof <engine-plan-script>` and use `pstats.Stats('out.prof').sort_stats('cumulative').print_stats(20)`. Focus on `build_namespace_registry` and `build_relationship_graph` call counts -- if either is called more than once per pipeline run, investigate caching.

---

## 4. Suggested Tests

### Connectors

1. **S3 prefix bypass trap:** `S3FileSource(config_with_prefix='data/').open('data/foo.csv')` -- verify this double-prefixes to `data/data/foo.csv` (the documented behavior) and the test documents the caller mistake. Forces callers to read the contract.

2. **S3 multipart abort on failure:** Mock `_upload_part` to raise on part 2. Assert `abort_multipart_upload` is called and the original exception propagates (not the abort's exception).

3. **S3 multipart exact-boundary:** Stream exactly 5 MB (no trailing bytes). Assert `complete_multipart_upload` is called with 1 part, not `put_object`. Stream 5 MB + 1 byte. Assert 1 part + final small part.

4. **SFTP liveness probe:** Simulate dead channel (mock `sftp.stat` raising `SSHException`). Assert `_connect()` tears down and rebuilds the session rather than returning the stale object.

5. **SFTP auth type ordering:** Supply an RSA key to `_parse_private_key`. Assert it succeeds on the third iteration (Ed25519 and ECDSA fail gracefully first).

6. **GCS `_wrap_gcs_error` coverage:** Assert `NotFound` -> `PermanentError`, `ServiceUnavailable` -> `TransientError`, plain `ValueError` -> `PermanentError`.

### Relationships graph

7. **Multi-parent FK rejection:** Two profile relationships where the same `(child_table, child_columns)` pair points to two different parent tables. Assert `PlanCompileError(code='multi_parent_fk_unsupported')`.

8. **Cycle detection:** Build a cycle: A -> B -> C -> A. Assert `PlanCompileError(code='fk_cycle')` with all three nodes in the message.

9. **Topological order is deterministic:** Same graph built twice in different insertion orders. Assert `ordering` tuples are byte-identical.

10. **Orphan policy duplicate key conflict:** Two config entries for the same (parent, child) tuple with different policies. Assert `PlanCompileError(code='orphan_fk_policy_duplicate')`.

11. **Same-parent-different-child allows distinct policies:** Two config entries for the same parent but different children, each with a different policy. Assert both are accepted and each child gets its own policy in the returned lookup.

### Namespace registry

12. **Composite auto-namespace collision:** Two groups in different tables that produce the same derived name. Assert they remain independent (different namespace entries, different `for_column` results).

13. **Namespace ambiguity on FK child override:** A child FK column already bound to `ns_a` via an explicit declaration, then a relationship tries to bind it to `ns_b`. Assert `NamespaceConfigError(code='namespace_ambiguity')`.

14. **`for_column` vs single-column tuple:** Assert `registry.for_column('t', ('col',))` returns the bound namespace and `registry.for_column('t', 'col')` either returns None (unhashable) or raises -- documents the tuple contract.

---

## 5. What's Good

- **S3 error classification** (`_wrap_client_error`): The QA-2026-05-31 fix correctly catches `ConnectTimeoutError` and `ReadTimeoutError` as `TransientError` before the generic `ClientError` branch. The `frozenset` of permanent error codes is the right pattern (fast membership test, immutable).

- **SFTP host key verification**: `RejectPolicy + load_known_hosts` is the correct replacement for the prior `AutoAddPolicy` MITM vector. The `DECOY_SFTP_KNOWN_HOSTS` env-var escape hatch is a sensible operator hook without opening a security hole.

- **SFTP session liveness probe**: The `stat(".")` probe-before-return pattern in `_connect()` correctly detects dead sessions (the prior always-return-cached bug was a silent failure loop). Best-effort teardown on stale session is correct.

- **Kahn's algorithm implementation** (`_graph.py`): Correct, deterministic, and the heapq refactor (from the QA-8 O(n^2) fix) is well-motivated. The cycle-detection path (ordered length vs nodes length) is the standard check. The error message lists the cycle participants, which makes debugging tractable.

- **`check_orphan_fk_policy_completeness`** 4-tuple key: The S13-rebaseline fix that changed the key from (parent only) to (parent, child) correctly allows legitimate same-parent-different-child configurations while still rejecting conflicting duplicate entries. The comment thread explaining the history is exemplary.

- **`NamespaceRegistry._index` rebuild in `__post_init__`**: The lazy-default `field(default_factory=dict)` + `__post_init__` rebuild ensures old callers that construct `NamespaceRegistry(bindings=...)` without `_index` get fast lookups automatically -- a clean backwards-compatible evolution.
