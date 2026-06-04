# QA Review: disguises, profile, timing, plan checks, registry

**Date:** 2026-06-04  
**Branch:** `qa/review-2026-06-04-disguises-profile-timing`  
**Reviewer:** Claude (QA agent)  
**Scope:**
- `src/decoy_engine/disguises/loader.py`
- `src/decoy_engine/disguises/schema.py`
- `src/decoy_engine/disguises/hipaa.yaml`
- `src/decoy_engine/disguises/gdpr.yaml`
- `src/decoy_engine/profile/_source.py`
- `src/decoy_engine/profile/_types.py`
- `src/decoy_engine/instrumentation/timing.py`
- `src/decoy_engine/plan/_compile.py`
- `src/decoy_engine/plan/_checks.py`
- `src/decoy_engine/providers_v2/_registry.py`
- `src/decoy_engine/validation/_config.py`

---

## 1. Summary

The disguise bundle system, profile layer, and instrumentation module are generally well-structured, but two compliance-critical bugs stand out. The HIPAA Disguise lists `address` as an expected identifier (HIPAA Safe Harbor §B) yet has no `field_rule` to mask it, meaning street addresses pass through unmasked while strict mode reports success. The GDPR Disguise requires `ssn` in its expected fields, making strict mode refuse on virtually every pure EU dataset. Beyond those two, the most actionable issues are: `FieldRule` schema having no non-empty detector list or strategy allowlist (silent dead-code rules pass Pydantic validation), `psutil.Process()` captured at import time (wrong RSS in forked processes), and no caching on `load_disguises()` (full YAML re-parse on every call).

---

## 2. Findings

### F1 — High | Correctness | `disguises/hipaa.yaml`

**Issue:** `address` appears in `expected_fields` (§B, street addresses — HIPAA Safe Harbor identifier) but no `field_rule` has `detectors: [address]`.

**Impact:** A column flagged `address` by STORM satisfies the strict-mode preflight check (`expected_field` matched → strict mode proceeds) but matches no `field_rule` and passes through the pipeline **unmasked**. A healthcare data engineer applying the HIPAA Disguise in strict mode receives a passing preflight signal while raw street addresses survive into the output. This directly violates HIPAA Safe Harbor §B (geographic data requires de-identification to the 3-digit ZIP level or removal).

**Verify:** Add a test column with `pii_class=PIIClass.ADDRESS` to a HIPAA-disguise application run and assert the output value differs from the input. Currently this assertion fails.

**Fix:**
```yaml
# B — Street address (complement to us_zip rule above)
- detectors: [address]
  mask: faker
  params:
    faker_type: street_address
  why: "Safe Harbor §B — street address → realistic fake street address."
```
Place this rule immediately after the `us_zip` rule (§B block) so it applies before any catch-all.

---

### F2 — Medium | Correctness | `disguises/gdpr.yaml`

**Issue:** `ssn` appears in `expected_fields`. The US Social Security Number is a US-specific identifier. A typical EU dataset matched by the GDPR triggers (`iban`, `eu_date`) would almost never contain an `ssn`-detected column.

**Impact:** The strict-mode GDPR Disguise refuses to run on any scan where `ssn` is undetected. Because `ssn` is the US SSN detector (not a generic national ID detector), a pure EU payroll dataset — GDPR's primary use case — cannot pass strict mode. The GDPR Disguise's strict mode is effectively broken for its target audience.

**Fix:** Remove `ssn` from `gdpr.yaml` `expected_fields`. The existing `field_rule` for `ssn` (format-preserving digits) continues to apply when an SSN column is detected; strict mode no longer blocks EU-only datasets. If a cross-Atlantic national-ID field is needed in expected_fields, use an any-of group: `- [ssn, national_id]` (pending a `national_id` detector).

---

### F3 — Medium | Reliability | `disguises/loader.py`

**Issue:** `load_disguises()` catches `yaml.YAMLError`, `OSError`, and broad `Exception` (for Pydantic errors), logs `error`, and continues. The caller receives a shorter `list[Disguise]` with no indication of which files failed or how many.

**Impact:** A single corrupt bundle (e.g., `hipaa.yaml` with a YAML syntax error after an edit) causes the HIPAA Disguise to silently vanish from the catalog. The platform's recommendation engine and strict-mode preflight continue to run against the truncated set. Operators may not discover the missing Disguise until a user reports that HIPAA is absent from the recommendation list.

**Fix:** Return a named result type or raise on critical failures. At minimum, return the count of skipped files as a sidecar so callers can emit a startup warning:

```python
def load_disguises(directory: Path | None = None) -> list[Disguise]:
    ...
    skipped = 0
    for path in yamls:
        try:
            ...
        except Exception as exc:
            _log.error("load_disguises: skipped %s: %s", path.name, exc)
            skipped += 1
    if skipped:
        _log.warning(
            "load_disguises: %d of %d bundle file(s) failed to load",
            skipped, len(yamls),
        )
    return out
```

The platform startup path should also assert `len(load_disguises()) > 0` or check for required IDs (`hipaa`, `gdpr`, `pci`).

---

### F4 — Medium | Performance | `disguises/loader.py`

**Issue:** `load_disguises()` reads and parses all `*.yaml` files from disk on every call. There is no module-level cache.

**Impact:** If `load_disguises()` is called in a per-scan recommendation loop or per-request preflight path, it pays full YAML parse + Pydantic validation cost on every invocation. The Disguise files are bundled at install time and never change at runtime.

**Fix:**
```python
from functools import lru_cache

@lru_cache(maxsize=1)
def load_disguises(directory: Path | None = None) -> list[Disguise]:
    ...
```
Note: `lru_cache` requires a hashable argument; pass `directory=None` (the default) for the production hot path. Tests that inject a custom directory bypass the cache naturally.

---

### F5 — Medium | Correctness | `disguises/schema.py:FieldRule`

**Issue:** `FieldRule.detectors: list[str]` has no minimum-length constraint.

**Impact:** A rule with `detectors: []` matches no column and is permanently dead code. It passes Pydantic validation silently. A YAML author who accidentally writes:
```yaml
field_rules:
  - detectors: []   # typo: forgot the detector name
    mask: faker
    params:
      faker_type: email
```
gets no error at load time and no masking at run time.

**Fix:**
```python
detectors: list[str] = Field(min_length=1)
```

---

### F6 — Medium | Correctness | `disguises/schema.py:FieldRule`

**Issue:** `FieldRule.mask: str` is unconstrained. Any string passes schema validation.

**Impact:** A typo like `mask: fakerr` or `mask: hash_stable` passes Pydantic validation at load time and fails at runtime when the engine dispatches the unknown strategy. The error surfaces only when the Disguise is actually applied to a column, not during bundle loading.

**Fix:** Constrain to the known strategy set:
```python
from typing import Literal

mask: Literal[
    "faker", "hash", "fpe", "redact", "truncate",
    "date_shift", "bucketize", "categorical", "formula", "passthrough",
]
```
Update the list when new strategies are added; the CI bundle-smoke test catches the mismatch immediately.

---

### F7 — Medium | Correctness | `disguises/schema.py:TriggerSpec`

**Issue:** `min_score: float = 0.3` has no range constraint.

**Impact:**
- `min_score < 0` means the Disguise is always recommended regardless of evidence quality.
- `min_score > 1` means the Disguise is never recommended (scores are in `[0, 1]`).

Both cases are silently accepted and produce confusing behavior at recommendation time.

**Fix:**
```python
min_score: float = Field(default=0.3, ge=0.0, le=1.0)
```

---

### F8 — Medium | Correctness | `instrumentation/timing.py`

**Issue:** `_PROCESS = psutil.Process()` is evaluated **once at module import time**, capturing the current process's PID. If the engine is later used inside a forked worker (e.g., `multiprocessing.Pool`, `ProcessPoolExecutor`, Celery task), the child process holds a `psutil.Process` bound to the **parent's** PID.

**Impact:** `rss_kb()` in forked children returns the parent's RSS, not the child's. All per-strategy memory-delta records in a multiprocessing execution context are wrong. The `StrategyTimingRecord.peak_memory_delta_kb` values are either wildly inflated (parent allocates more memory) or wildly negative before clamping. This affects evidence manifests that include timing data.

Psutil documents this explicitly: `Process` objects are not fork-safe by default.

**Fix — lazy construction:**
```python
_PROCESS: psutil.Process | None = None

def _get_process() -> psutil.Process:
    global _PROCESS
    if _PROCESS is None or _PROCESS.pid != os.getpid():
        _PROCESS = psutil.Process()
    return _PROCESS

def rss_kb() -> int:
    return int(_get_process().memory_info().rss / 1024)
```
The `os.getpid()` check detects the fork case at cost of one syscall per measurement — acceptable given that `memory_info()` itself is already a syscall.

**Alternative (preferred if Python 3.12+):** Use `os.register_at_fork(after_in_child=lambda: globals().update(_PROCESS=None))` to reset the global at fork time.

---

### F9 — Medium | Performance | `profile/_source.py`

**Issue:** `_load_file_source`, `_load_gcs_source`, and `_load_s3_source` all materialize the entire source file into a `pd.DataFrame` (or an intermediate `BytesIO`) before returning. No file-size check is performed.

**Impact:** A 5–10 GB CSV or Parquet source passed to `profile_source` will OOM the worker process. `profile_source` already supports `sample_rows=10_000` (the default), but the full file is loaded before sampling, so the OOM happens regardless of the sample size. The S3 path downloads the entire object into `BytesIO` before calling `pd.read_csv`; GCS uses `blob.download_as_bytes()` similarly.

**Partial fix — file source:** Use chunked reading to sample:
```python
if fmt == "csv":
    if sample_rows is not None:
        return pd.read_csv(path, nrows=sample_rows)  # skip walk_dataframe sampling for file
    return pd.read_csv(path)
```
*Note:* `nrows` at the read stage avoids materializing the full file, but loses the reservoir-sampling guarantee for non-CSV formats. The right fix is a max-bytes guard at the source-load layer, documented as a production limit.

**Recommended:** Document a `MAX_PROFILE_SOURCE_BYTES` constant (e.g., 512 MB) and raise a descriptive `ConfigError` when the source exceeds it. Cloud sources can check `Content-Length` / object metadata before downloading.

---

### F10 — Low | Security | `api/pipelines/router.py: revert_pipeline`

**Issue:** `revert_pipeline` restores `p.yaml_content` from a historical `PipelineVersion` but does NOT call `_enforce_advanced_expression_gate` on the restored YAML.

**Impact:** A user with `viewer` role (or any role below `developer`) can:
1. Get a pipeline with a formula expression demoted to a non-formula version.
2. Call `POST /{pipeline_id}/revert/{older_version}` to restore the formula version.

This bypasses the server-side gate (`enforce_permission(user, db, ADVANCED_EXPRESSION_FLAG, min_role=ADVANCED_EXPRESSION_MIN_ROLE)`) that `create_pipeline` and `update_pipeline` enforce.

**Fix:** Add the gate check in `revert_pipeline` before committing:
```python
_enforce_advanced_expression_gate(target.yaml_content, user, db)
p.yaml_content = target.yaml_content
db.commit()
```

---

### F11 — Low | Reliability | `api/reporting/router.py: _load_warnings_by_node`

**Issue:**
```python
try:
    entries = _yaml.safe_load(r.warnings)
except Exception:
    continue
if not isinstance(entries, list):
    continue
```
Both failure paths silently drop the warnings for the node without any log message.

**Impact:** A `JobNodeRun.warnings` value that was written as malformed YAML (e.g., a truncated write) causes the node's known-limitation warnings to vanish from compliance reports and evidence manifests. The reviewer sees a clean report for a run that actually had warnings. There is no diagnostic trail to investigate.

**Fix:**
```python
try:
    entries = _yaml.safe_load(r.warnings)
except Exception as exc:
    _log.warning(
        "_load_warnings_by_node: job_id=%s node_id=%s: YAML parse failed: %s",
        job_id, r.node_id, exc,
    )
    continue
if not isinstance(entries, list):
    _log.warning(
        "_load_warnings_by_node: job_id=%s node_id=%s: expected list, got %s",
        job_id, r.node_id, type(entries).__name__,
    )
    continue
```

---

### F12 — Low | Security | `api/reporting/router.py: export_pipeline_compliance_report`

**Issue:** `export_pipeline_compliance_report` (GET `/reporting/compliance-export/pipeline/{pipeline_id}`) fetches the pipeline row without an ownership check:
```python
pipeline = db.get(Pipeline, pipeline_id)
if not pipeline:
    raise HTTPException(status_code=404, detail="Pipeline not found")
# No: if user.role != "admin" and pipeline.created_by != user.id: ...
```
The subsequent job query IS filtered by owner, but `pipeline_yaml = pipeline.yaml_content`, `disguise = _disguise_from_yaml(pipeline_yaml)`, and `schedule = _schedule_desc(pipeline_id, db)` all execute on ANY pipeline regardless of the caller's ownership.

**Impact:** A non-admin user can read any pipeline's YAML content (which may contain connector references, encryption key labels, column strategies, and provider configs), applied Disguise ID, and schedule details by supplying any valid `pipeline_id`. Classified as V1 single-tenant design (per the comment in `download_pipeline_yaml`), but the pipeline-level export is a higher-sensitivity endpoint (it reveals more structured data than the download endpoint). Document explicitly or add the ownership guard before V2 multi-tenant launch.

**Fix (for V2 readiness):**
```python
if user.role != "admin" and getattr(pipeline, "created_by", None) != user.id:
    raise HTTPException(status_code=403, detail="Access denied")
```

---

### F13 — Low | Performance | `api/reporting/router.py: export_pipeline_compliance_report`

**Issue:** The pipeline-level compliance export iterates `jobs` and calls `_parse_tables(yaml_content, ..., warnings_by_node=_load_warnings_by_node(job.id, db))` per job. `_load_warnings_by_node` issues one `db.query(JobNodeRun).filter(job_id=job_id).all()` per iteration.

**Impact:** For `limit=50` jobs, this is 50 separate `JobNodeRun` queries. Each may itself join through multiple nodes per table. The total number of DB round-trips is O(limit × avg_nodes_per_job). At 50 jobs × 5 nodes, that is 250 serial queries synchronously on the HTTP worker thread.

**Fix:** Batch the `JobNodeRun` query before the loop:
```python
from api.models import JobNodeRun as _JobNodeRun

job_ids = [j.id for j in jobs]
all_node_runs = (
    db.query(_JobNodeRun)
    .filter(_JobNodeRun.job_id.in_(job_ids))
    .all()
)
# Group by job_id before the loop, pass the pre-grouped dict into each call
```
This reduces 50 queries to 1 for the `JobNodeRun` fetch.

---

### F14 — Low | Performance | `providers_v2/_registry.py: get_default_registry`

**Issue:** Non-atomic singleton build:
```python
if _DEFAULT_REGISTRY is None:
    # ... build takes ~50ms (Faker import, 9 native adapters, 6 composites) ...
    _DEFAULT_REGISTRY = ProviderRegistry(bindings)
```
Two threads entering simultaneously both see `None`, both build, and the second overwrites the first.

**Impact:** Benign under CPython GIL (both builds produce identical objects; no corruption). Under free-threaded Python 3.13+, this could be a correctness issue. At current load it wastes one redundant 50ms build on the first multi-threaded access.

**Fix:** Same pattern recommended in `pandas-fpe-compile F3`:
```python
from functools import lru_cache

@lru_cache(maxsize=1)
def _build_default_registry() -> ProviderRegistry:
    # ... move build logic here ...

def get_default_registry() -> ProviderRegistry:
    return _build_default_registry()
```
`lru_cache` uses an internal lock and guarantees exactly-once build.

---

### F15 — Nit | Reliability | `api/pipelines/router.py: validate_yaml`

**Issue:**
```python
except Exception as exc:
    return ValidateYamlOut(
        ok=False,
        messages=[{
            ...
            "message": f"preflight failed unexpectedly: {exc}",
            ...
        }],
    )
```
Full exception message (including stack details in some exception types) is returned in the API response.

**Impact:** Internal implementation details — module paths, function names, SQL fragments — may surface in client-side developer tools or browser network logs.

**Fix:**
```python
"message": f"preflight failed unexpectedly ({type(exc).__name__}); check server logs",
```
Log the full `exc` at `_log.exception` level server-side for debugging.

---

## 3. Performance Notes

| Finding | Bottleneck type | How to measure |
|---|---|---|
| F4 (load_disguises no cache) | CPU — YAML parse + Pydantic | `timeit.timeit(load_disguises, number=100)` |
| F8 (psutil fork safety) | Correctness, not perf | Unit test: fork a process, call `rss_kb()`, assert PID matches |
| F9 (full file load) | Memory — peak RSS | `memory_profiler` on a 1GB CSV; observe RSS spike before `walk_dataframe` |
| F13 (N+1 JobNodeRun queries) | I/O — serial DB round-trips | SQL query log; count distinct queries per export request |
| F14 (registry double-build) | CPU — 50ms build | Reproduce with two threads; profile with `threading` + `time.perf_counter` |

---

## 4. Suggested Tests

### F1 — HIPAA address gap
```python
def test_hipaa_disguise_masks_address_column():
    """HIPAA field_rules must include a rule for the `address` detector."""
    from decoy_engine.disguises import load_disguises
    hipaa = next(d for d in load_disguises() if d.id == "hipaa")
    address_covered = any(
        "address" in rule.detectors for rule in hipaa.field_rules
    )
    assert address_covered, (
        "HIPAA Disguise has no field_rule for `address` detector; "
        "street addresses would pass through unmasked (Safe Harbor §B violation)."
    )
```

### F2 — GDPR ssn in expected_fields
```python
def test_gdpr_strict_mode_works_without_ssn():
    """GDPR strict mode must pass for a dataset with no SSN column."""
    from decoy_engine.disguises import load_disguises
    gdpr = next(d for d in load_disguises() if d.id == "gdpr")
    groups = gdpr.expected_field_groups()
    # Verify `ssn` is NOT a required standalone field
    assert ["ssn"] not in groups, (
        "GDPR expected_fields contains `ssn` as a required field; "
        "strict mode will always reject pure EU datasets without SSN columns."
    )
```

### F5/F6/F7 — schema constraints
```python
import pytest
from pydantic import ValidationError
from decoy_engine.disguises.schema import FieldRule, TriggerSpec

def test_field_rule_rejects_empty_detectors():
    with pytest.raises(ValidationError):
        FieldRule(detectors=[], mask="faker", params={})

def test_field_rule_rejects_unknown_mask():
    with pytest.raises(ValidationError):
        FieldRule(detectors=["email"], mask="fakerr", params={})

def test_trigger_spec_rejects_negative_min_score():
    with pytest.raises(ValidationError):
        TriggerSpec(min_score=-0.1)

def test_trigger_spec_rejects_min_score_above_one():
    with pytest.raises(ValidationError):
        TriggerSpec(min_score=1.5)
```

### F8 — psutil fork safety
```python
import os, multiprocessing
from decoy_engine.instrumentation.timing import rss_kb

def _child_rss_kb(q):
    q.put((os.getpid(), rss_kb()))

def test_rss_kb_returns_child_pid_in_fork():
    q = multiprocessing.Queue()
    p = multiprocessing.Process(target=_child_rss_kb, args=(q,))
    p.start()
    p.join()
    child_pid, _ = q.get()
    assert child_pid != os.getpid(), "child_pid should differ from parent"
    # If rss_kb() is broken post-fork, psutil would raise NoSuchProcess;
    # the test would error rather than fail, which is also actionable.
```

### F9 — large file OOM guard
```python
def test_profile_source_raises_on_oversized_file(tmp_path):
    """profile_source should not silently OOM on multi-GB files."""
    # Write a hint file, check that the size guard fires before load.
    # (Full OOM test impractical in CI; this tests the guard path.)
    import csv
    big = tmp_path / "big.csv"
    with big.open("w") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name"])
    # After F9 is fixed, configure MAX_PROFILE_SOURCE_BYTES = 100
    # and assert ConfigError raised for files > threshold.
    pass  # placeholder until guard is implemented
```

---

## 5. What's Good

- **`profile/_types.py`**: Tight `__post_init__` guards on `ColumnProfile` (is_candidate_key_sampled vs sampled, is_fk/fk_target consistency, null_count ≤ row_count, distinct_count ≤ row_count) and `Relationship` (non-empty columns, equal lengths) are exactly right. Fail-loud at construction time keeps bad data out of the planner.
- **`plan/_compile.py`**: `_normalize_job_seed` is exemplary: bool guard, float guard, TypeError/ValueError catch, overflow check, and 8-byte big-endian canonicalization are all correct. The module-level comment documenting the lazy-import cycle rationale is the right amount of explanation.
- **`plan/_checks.py`**: `_is_integer_dtype` correctly handles lowercase normalization, bracket stripping (for `int64[pyarrow]`), nullable `Int64` → `int64`, and the full numpy/pandas/DB type zoo. The FK-child exemption in `check_null_bearing_int_unsupported` is the right carve-out.
- **`profile/_source.py`**: The Q15 seed fallback (`config["global_settings"]["seed"] → profile_source seed`) with a `warnings.warn` when seed is still None is the right belt-and-suspenders pattern. The S3 source correctly wraps network-class errors to avoid leaking `botocore` request metadata into job logs.
- **`instrumentation/timing.py`**: Thread-local collector design is clean. The zero-overhead fast path (`if collector is None: yield; return`) is correct and the claim of <2% overhead on a tight loop is plausible given that `get_active_collector()` is just a `getattr` on a `threading.local`.
- **`disguises/hipaa.yaml`**: The regulatory citation per field rule (`why: "Safe Harbor §B..."`) is excellent practice for a compliance product. The `co_occurrence` scoring boosts for the canonical HIPAA quasi-identifier trio (date + ZIP + name) show careful regulatory understanding.
