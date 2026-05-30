# QA Review — `decoy-engine` (S14–S17 New Code)

Session: 2026-05-30 (second session)  
Scope: S14-CLOUD-SRC-S3GCS, S15-CLOUD-TGT-S3GCS, S17-TX-NARROW  
Prior QA (Q1–Q14): `docs/audit/qa-review-2026-05-29.md`  
Reviewer: external QA (claude-sonnet-4-6)  
Engine HEAD at review: `8942d104`

---

## 1. Summary

The S14–S17 additions are architecturally clean. The cloud source/target schema (S14/S15) is a well-structured Pydantic discriminated union with `extra="forbid"`, the atomic-move cloud-write pattern (S15) correctly guards against partial writes, and the S17 narrow-transform surface is a lean, auditable set of six ops with sound compile-time validation. The critical problem is a determinism bug in `profile_source`: the platform's `run_v2_mask` injects a deterministic seed into the config dict but never passes it as the `seed=` keyword argument to `profile_source`, so the profiling RNG is always seeded non-deterministically from OS entropy. Since `compile_plan` derives pool sizes and category lists from the profile, two runs with identical inputs and config can produce different plans and therefore different output — the core reproducibility guarantee is broken for any source larger than 10,000 rows.

---

## 2. Findings

### Q15 — Critical — Correctness / Determinism

**`profile/_source.py` — `profile_source` seed never passed from caller; profiling RNG is non-deterministic**

The platform's `run_v2_mask` sets `cfg["global_settings"]["seed"]` and then calls `profile_source(cfg)` without a `seed=` kwarg:

```python
# platform: api/jobs/v2_runner.py
cfg.setdefault("global_settings", {})["seed"] = derive_mask_job_seed_int()
profile = profile_source(cfg)          # ← seed kwarg missing
```

`profile_source` has a separate `seed` parameter defaulting to `None`:

```python
# src/decoy_engine/profile/_source.py
def profile_source(config, *, sample_rows=10_000, seed=None):
    rng = random.Random(seed) if seed is not None else random.Random()
```

When `seed is None`, `random.Random()` draws from OS entropy. For any source table exceeding `sample_rows=10_000` rows, `walk_dataframe` uses this RNG for reservoir sampling; the resulting cardinality estimates and value lists feed `compile_plan`, which sizes pools and builds category pools. Two runs with the same table and the same config therefore compile different plans and produce different masked output.

**Impact:** The central invariant — "same seed + same input → same output" — is broken for any source larger than 10,000 rows.

**Verify:**  
```bash
# Run twice on a >10k-row CSV; diff must be empty.
python -c "
import copy, json
from api.jobs.v2_runner import run_v2_mask
cfg = json.load(open('test_pipeline.json'))
r1 = run_v2_mask(copy.deepcopy(cfg))
r2 = run_v2_mask(copy.deepcopy(cfg))
for t in r1.outputs:
    assert r1.outputs[t].equals(r2.outputs[t]), f'Non-deterministic output on table {t}'
print('PASS')
"
```

**Fix — Option A (fix at the call site in v2_runner.py):**
```python
seed_int = derive_mask_job_seed_int()
cfg.setdefault("global_settings", {})["seed"] = seed_int
profile = profile_source(cfg, seed=seed_int)
```

**Fix — Option B (defensive fallback in `profile_source` itself):**
```python
def profile_source(config, *, sample_rows=10_000, seed=None):
    if seed is None:
        seed = (config.get("global_settings") or {}).get("seed")
    rng = random.Random(seed) if seed is not None else random.Random()
```

Option B is safer long-term: any future caller that forgets the kwarg will still get deterministic sampling. Both options should be applied (Option B guards against new callers; Option A is the immediate fix for the known bug site).

The same bug exists in `inspect_v2_plan` (platform `v2_runner.py`).

---

### Q16 — High — Security

**`execution/_transforms.py:_apply_filter` / `_apply_derive` — `df.eval()` engine unspecified; `@var` injection possible via Python engine**

Both ops call `df.eval(expression)` without an explicit `engine=` argument:

```python
mask = df.eval(op.expression)   # _apply_filter
result = df.eval(op.expression)  # _apply_derive
```

The module docstring states: *"pandas DataFrame.eval uses NumPy's expression engine, NOT CPython eval."*  
This is incorrect. pandas `DataFrame.eval()` defaults to `engine="numexpr"` only when numexpr is installed. If numexpr is absent, it silently falls back to `engine="python"` — which uses Python's `eval()` with a restricted but escapable namespace. The `python` engine supports the `@` prefix to inject local variables:

```python
# With engine="python" this leaks the filesystem:
df.eval("@__import__('os').getcwd()")
```

Even with numexpr present, pandas does not document a formal sandbox boundary for the numexpr engine; a numexpr upgrade or a platform-specific build could silently widen the surface.

**Impact:** If end users can submit pipeline configs containing `transforms[].filter.expression` or `transforms[].derive.expression`, they can execute arbitrary code when the Python engine is active. The risk is gated on whether `expression` is user-controlled (UI-submitted pipeline) vs. admin-only.

**Fix — pin the engine and gate on numexpr:**
```python
def _apply_filter(df: pd.DataFrame, op: FilterOp) -> pd.DataFrame:
    try:
        mask = df.eval(op.expression, engine="numexpr")
    except ImportError:
        raise TransformError(
            code="numexpr_required",
            message="transforms require numexpr; install it with: pip install numexpr",
        )
    except Exception as exc:
        raise TransformError(
            code="filter_expression_error",
            message=f"filter expression {op.expression!r} failed: {type(exc).__name__}",
        ) from exc
    ...
```

Apply the same pattern to `_apply_derive`. Add `numexpr` to `[project]` dependencies in `pyproject.toml` (do not leave it optional).

---

### Q17 — Medium — Reliability

**`profile/_source.py:_load_s3_source` — `response["Body"]` StreamingBody not closed after `.read()`**

```python
response = client.get_object(Bucket=bucket, Key=key)
body = io.BytesIO(response["Body"].read())
```

`response["Body"]` is a `botocore.response.StreamingBody` backed by an HTTP connection. Calling `.read()` drains the body but does not close the underlying socket or return the connection to boto3's urllib3 pool. Under a pipeline with many S3 sources, this exhausts the pool (default 10 connections) and causes hangs or `ConnectionPool is full` warnings.

**Fix:**
```python
with client.get_object(Bucket=bucket, Key=key)["Body"] as stream:
    body = io.BytesIO(stream.read())
```
`StreamingBody` supports `__enter__`/`__exit__` since botocore 1.9.

The same issue exists in `api/jobs/v2_runner.py:_fetch_s3_to_bytesio` on the platform side.

---

### Q18 — Low — Reliability

**`profile/_source.py:_load_gcs_source` — `storage.Client()` not closed**

```python
client = storage.Client()
bucket = client.bucket(bucket_name)
blob = bucket.blob(object_name)
data = blob.download_as_bytes()
```

`storage.Client` holds an internal HTTP transport (requests Session or gRPC channel). No `client.close()` is called. Under a pipeline with many GCS sources, HTTP sessions accumulate until garbage-collected (non-deterministic timing).

**Fix:**
```python
with storage.Client() as client:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    data = blob.download_as_bytes()
```
`storage.Client` supports the context manager protocol since google-cloud-storage ≥ 1.31.

Same pattern applies to `_fetch_gcs_to_bytesio` and `_materialize_gcs_output` on the platform side.

---

## 3. Performance Notes

| Bottleneck | Location | Estimated impact |
|---|---|---|
| Double source read (profile + Arrow) | `profile_source` + `_read_sources_as_arrow` | For cloud sources: 2× network egress per table. At 100 MB/object over a 100 Mbps link, a 5-table pipeline wastes ~8 s. Documented as deferred; schedule before GA. |
| `_apply_derive` full DataFrame copy | `execution/_transforms.py:_apply_derive` | `df.copy()` allocates a full copy of all existing columns just to add one column. At 1M rows × 50 object columns, this is ~200–400 MB per derive op. pandas 2.0 copy-on-write mitigates this if the caller upgrades; `df.assign()` is equivalent in earlier pandas. |
| `TypeAdapter(list[TransformOp])` per run | `api/jobs/v2_runner.py:_apply_per_table_transforms` | Pydantic TypeAdapter construction compiles a JSON schema (~5–20 ms per call). Move to a module-level constant: `_TRANSFORMS_ADAPTER: TypeAdapter[list[TransformOp]] = TypeAdapter(list[TransformOp])` |

**Profiling recipe for Q15 (once fixed):** `python -m scalene --cpu --memory -m pytest tests/integration/` — confirm that two runs with the same seed produce identical output and that profile sampling time is deterministic.

---

## 4. Suggested Tests

| # | What | How |
|---|---|---|
| T15 | `profile_source` is deterministic when seed is provided | Call twice with same seed on a 50k-row CSV; assert `profile1 == profile2`. Run in separate processes (`subprocess.run`) to confirm no RNG state leakage. |
| T16 | `FilterOp` blocks `@var` injection | With `engine="numexpr"` pinned: `df.eval("@__import__('os').getcwd()", engine="numexpr")` should raise `pandas.core.computation.ops.UndefinedVariableError`, not return a value. |
| T17 | `FilterOp` raises `TransformError(code="numexpr_required")` when numexpr absent | Patch `importlib.import_module('numexpr')` to raise `ImportError`; assert correct code. |
| T18 | S3 StreamingBody closed after read | Mock `get_object` to return a mock Body; assert `close()` or `__exit__` was called. |
| T19 | GCS Client closed after read | Mock `storage.Client`; assert `close()` was called (or `__exit__` invoked). |
| T20 | Same seed + same source → same output (end-to-end property test) | Hypothesis: for any seed in `0..2^32` and a fixed CSV, `run_v2_mask(seed=s)` twice produces identical Arrow tables. |
| T21 | `_apply_derive` does not mutate input DataFrame | Pass a DataFrame; run DeriveOp; assert original DataFrame unchanged (input columns unmodified). |

---

## 5. What's Good

The **S17 transform schema** is exactly right: six ops in a narrow discriminated union, `extra="forbid"` on every variant, non-negative `n` for LimitOp validated at schema time, and compile-time column-existence checks that fire before the DataFrame is touched. Pre-validating column references (sort_column_missing, dedupe_column_missing, drop_column_missing, derive_column_already_exists) turns config errors into typed, actionable errors rather than mid-run pandas exceptions.

The **S14/S15 cloud schema** correctly models credentials as opaque references — the engine never handles raw secrets — and uses `extra="forbid"` throughout. S3-compatible `endpoint_url` support with clean test coverage via moto is production-quality. The `max_length=255` on bucket names and `min_length=1` on keys prevent common validation oversights.

The **S15 atomic-move write pattern** is the correct answer to Q12. The try/except structure guarantees the canonical object is never clobbered by a partial upload: the tmp key gets cleaned up on failure, and the canonical key is only written via server-side copy after the tmp has been verified.
