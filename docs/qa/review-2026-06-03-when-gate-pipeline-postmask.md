# QA Review: `_when_gate.py`, `_pipeline.py`, `storm/postmask/`

**Date**: 2026-06-03
**Branch**: `qa/review-2026-06-03-when-gate-pipeline-postmask`
**Scope**:
- `src/decoy_engine/execution/_when_gate.py`
- `src/decoy_engine/execution/_pipeline.py`
- `src/decoy_engine/storm/postmask/fk_preservation.py`
- `src/decoy_engine/storm/postmask/residual_pii.py`
**Previous reviews to avoid**: all `qa/review-2026-06-02-*` and `qa/review-2026-06-03-*` branches.

---

## 1. Summary

The `when:` gate and the mixed-mode pipeline (`run_pipeline`) are structurally sound,
with good security posture on the numexpr eval path. The two dominant issues here are
in the post-mask checks. `fk_preservation.py` applies a parent-row size cap (`_PARENT_TUPLE_CAP`)
only to the composite-FK path, leaving the far more common single-column path able to OOM
the worker on large parent tables. `residual_pii.py` has no awareness of generate-kind
tables: all columns in synthetic output are classified as "no strategy configured" and
emit false-positive warnings. Additionally, the pandas `run_with_when_gate` lacks the
anchor-column index-alignment guarantee that was added to the polars path in QA-3 F13,
leaving a latent misalignment risk if any pandas handler ever resets the subset's index.

---

## 2. Findings

---

### F1 — HIGH | Correctness | `fk_preservation.py:163` — single-column path has no parent-row size cap

**The issue**: `_check_composite_fk` (line 284) caps the parent-side materialisation at
`_PARENT_TUPLE_CAP = 10_000_000` rows to prevent OOM. The equivalent single-column path
in `_check_one_fk` (line 163) has no cap:

```python
# _check_one_fk, line 163
parent_set = set(parent_pks.tolist())  # no bound
```

For a 10M-row UUID-keyed parent table, `parent_pks.tolist()` produces 10M Python string
objects. CPython's per-object overhead for a 36-character UUID string is ~100-150 bytes,
giving a peak resident set of ~1.5 GB for the set alone — on top of the already-resident
post-mask DataFrame. A production schema with a high-fan-out parent table (orders, events)
trivially crosses this threshold.

**Why it matters**: The post-mask FK check is run on every pipeline completion. A single
large parent table causes an unhandled OOM kill of the job worker. The composite path got
the cap as part of QA-4 F8; the single-column path was left unprotected.

**Recommended fix** — mirror the composite cap exactly:

```python
_PARENT_ROW_CAP = 10_000_000  # shared constant, hoist to module level

def _check_one_fk(...) -> FKPreservationFinding:
    ...
    parent_pks = parent_df[parent_col].dropna()
    if len(parent_pks) > _PARENT_ROW_CAP:
        return FKPreservationFinding(
            ...,
            severity="warning",
            orphan_count=0,
            orphan_rate=0.0,
            message=(
                f"parent table {parent_table!r} has {len(parent_pks)} rows, "
                f"above the {_PARENT_ROW_CAP}-row cap for single-column FK orphan "
                "detection. Use a sample-based audit for tables this large."
            ),
        )
    parent_set = set(parent_pks.tolist())
    ...
```

Also consolidate `_DEFAULT_WARNING_THRESHOLD` / `_DEFAULT_FAIL_THRESHOLD` into the same
module-level constants block so the cap sits next to the thresholds it parallels.

**Verify**: run `memory_profiler` (`@profile` decorator, `mprof run`) against a synthetic
test that builds a 10M-row parent DataFrame and calls `check_fk_preservation`. Confirm
peak RSS stays below a configurable limit.

---

### F2 — HIGH | Correctness | `fk_preservation.py:303` — no child-side row cap for composite path

**The issue**: `_check_composite_fk` caps the PARENT side at `_PARENT_TUPLE_CAP` but has no
cap on the CHILD side. `pd.MultiIndex.from_frame(child_tuples_df)` at line 303 materialises
the full child frame into a MultiIndex:

```python
child_mi = pd.MultiIndex.from_frame(child_tuples_df)   # no bound on child size
orphan_count = int((~child_mi.isin(parent_mi)).sum())
```

A 50M-row event/log child table (a realistic audit table referencing a user parent) would
materialise ~5-15 GB of MultiIndex objects. This OOM risk is symmetric but was not
addressed by QA-4 F8, which only added the parent cap.

**Why it matters**: The child side is often larger than the parent side by orders of
magnitude. Capping only the parent leaves the larger hazard in place.

**Recommended fix**: Add a child-side cap that skips to a sampled orphan estimate or a
partial check, with the same warning-severity finding pattern:

```python
_CHILD_TUPLE_CAP = 5_000_000

if total_child > _CHILD_TUPLE_CAP:
    # Sample the first _CHILD_TUPLE_CAP rows for the orphan check.
    child_tuples_df = child_tuples_df.head(_CHILD_TUPLE_CAP)
    total_child = _CHILD_TUPLE_CAP
    # Note the truncation in the message so callers know the rate is approximate.
    _capped_note = f" (child capped at {_CHILD_TUPLE_CAP} rows; rate is approximate)"
else:
    _capped_note = ""
```

Alternatively: stream the child in chunks and aggregate `isin` results, keeping peak
memory proportional to chunk size rather than table size.

---

### F3 — HIGH | Correctness | `residual_pii.py:93-126` — generate-kind table columns always emit false-positive warnings

**The issue**: `check_residual_pii` builds `strategy_by_col` from `config["tables"][*]["columns"]`.
Generate-kind tables use `generate_columns`, not `columns`, so no entry is populated for
any column in a synthetic table:

```python
# strategy_by_col only covers tables[].columns[]
for col_cfg in table_cfg.get("columns") or []:   # generate tables have no "columns"
    ...

# Later, for every column in every output frame:
configured = strategy_by_col.get((table_name, col_name))  # → None for generate tables
severity, message = _classify(top.detector_id, configured)
# configured=None → "warning" (operator may have forgotten to mask this column)
```

A pure-generate run producing realistic names, emails, or phone numbers will emit a
`"warning"` for every detector hit on every synthetic column — even though those values
are intentionally synthetic and fully under the engine's control. This drowns the report
in noise and trains operators to ignore residual-PII warnings.

**Why it matters**: False-positive warnings degrade signal quality of the post-mask
validation surface. When a REAL residual-PII warning fires (e.g., a hash strategy that
silently no-op'd), it is buried in a report full of expected synthetic-PII warnings.

**Recommended fix** — build a parallel lookup for generate-kind columns and classify them
as `"info"` (synthetic values look like PII intentionally):

```python
_GENERATE_COLUMN_SENTINEL = "__generate__"

for table_cfg in config.get("tables") or []:
    table_name = table_cfg.get("name")
    if not isinstance(table_name, str):
        continue
    for col_cfg in table_cfg.get("columns") or []:
        ...  # existing mask-side logic

    # Generate-kind columns: mark with a sentinel so _classify can distinguish.
    for col_cfg in table_cfg.get("generate_columns") or []:
        col_name = col_cfg.get("name")
        if isinstance(col_name, str):
            strategy_by_col[(table_name, col_name)] = _GENERATE_COLUMN_SENTINEL
```

Then in `_classify`:

```python
if configured == _GENERATE_COLUMN_SENTINEL:
    return (
        "info",
        f"column matched {detector_id!r}; expected because this column is "
        "synthetically generated (generate_columns) and produces realistic-looking values.",
    )
```

---

### F4 — MEDIUM | Correctness | `_when_gate.py:148-150` — pandas gate lacks index-alignment guarantee

**The issue**: The polars path received the anchor-column writeback fix (QA-3 F13,
2026-05-31) to guard against a handler that reorders or resets the index on its input
subset. The pandas path has no equivalent guard:

```python
# run_with_when_gate, lines 148-150
sub_df = df.loc[mask].copy()
sub_df, warnings = handler.run(sub_df, column, plan, ctx)
df.loc[mask, column] = sub_df[column]   # ← label-aligned: breaks if handler reset index
```

If any pandas strategy handler calls `reset_index(drop=True)` on `sub_df`, its index
becomes `0, 1, 2, ...` instead of the original masked row labels. The label-based
`df.loc[mask, column] = sub_df[column]` assignment would then silently write values to
whichever rows in `mask` match those integer labels — which may not be the intended rows,
or may match no rows at all (NaN-filling).

**Why it matters**: No current V2 pandas handler resets the index on its input. But this
is an undocumented contract; a future handler could easily violate it. The bug mode is
silent: no exception, incorrect masked values in output, discovered only via a
byte-identical reproducibility check. This is exactly the class of hidden nondeterminism
the application context treats as critical.

**Recommended fix**: Mirror the polars anchor pattern for the pandas path. The pandas
version is simpler because index alignment is native:

```python
_ANCHOR = "_decoy_when_row_pos"

sub_df = df.loc[mask].copy()
sub_df[_ANCHOR] = range(mask.sum())          # positional anchor within subset
sub_df, warnings = handler.run(sub_df, column, plan, ctx)
# Recover original positions even if handler reset the subset's index.
original_positions = sub_df[_ANCHOR].to_numpy()
result_values = sub_df[column].to_numpy()
# Write back by original integer position (iloc), not label (loc).
col_pos = df.columns.get_loc(column)
df.iloc[original_positions, col_pos] = result_values
sub_df.drop(columns=[_ANCHOR], inplace=True)
```

Alternatively, enforce the contract in a unit test that asserts `_when_gate` correctness
when the handler resets the subset's index. If every current handler preserves the index,
pin that with a regression test rather than fixing the gate — but document the contract
explicitly.

**Verify**: add `tests/unit/test_when_gate_index_reset.py` that passes a handler stub
which calls `sub_df.reset_index(drop=True)` before returning; assert the masked values
land in the correct rows.

---

### F5 — MEDIUM | Correctness | `_pipeline.py:139-140` — seed silently drops non-int values

**The issue**:

```python
# run_pipeline, lines 139-140
job_seed_raw = (config.get("global_settings") or {}).get("seed")
job_seed = job_seed_raw if isinstance(job_seed_raw, int) else None
```

`profile_source(config, seed=job_seed)` at line 142 receives `None` if the seed is not
a Python `int`. YAML `seed: 42` parses as `int`, but `seed: 42.0` parses as `float`.
A misconfigured config silently falls back to a seedless (nondeterministic) profile
rather than raising a validation error. The typed config model (`PipelineConfig`) should
catch this at validation time; `run_pipeline` documents "no re-validation here." Still,
the silent coercion from "wrong type → None" is a hidden nondeterminism trap: a wrong
config produces nondeterministic output with no error.

**Why it matters**: Determinism is a first-class invariant. Any code path that silently
drops the seed when the type is wrong violates it in a way that is hard to detect
(same inputs → different outputs across runs, with no exception or log entry).

**Recommended fix**: Raise explicitly on unexpected seed types rather than coercing to None:

```python
job_seed_raw = (config.get("global_settings") or {}).get("seed")
if job_seed_raw is not None and not isinstance(job_seed_raw, int):
    raise TypeError(
        f"global_settings.seed must be an int; got {type(job_seed_raw).__name__!r}. "
        "Pass a validated PipelineConfig dump to run_pipeline."
    )
job_seed: int | None = job_seed_raw
```

---

### F6 — MEDIUM | Correctness | `residual_pii.py:123` — `sample_match_count` computed over full series length including nulls

**The issue**:

```python
# line 123
sample_match_count=int(top.match_rate * len(series)),
```

`len(series)` is the total row count including null values. If detectors compute
`match_rate` over the non-null subset (e.g., `matches / non_null_count`), multiplying
by `len(series)` (which includes nulls) produces an understated `sample_match_count`.
At 50% null density on a 1M-row column, the reported match count would be 2× too low.
Conversely, if the detector's denominator is the full series length, the count is correct
— but that contract is not visible from this call site.

**Why it matters**: Operators use `sample_match_count` to triage severity. An
underreported count masks how many values actually leaked. This is a diagnostic accuracy
issue, not a blocking correctness failure, but it could cause operators to under-triage
a `fail`-severity finding.

**Recommended fix**: Use the non-null count for the denominator, consistent with most
detector conventions:

```python
non_null_count = int(series.notna().sum())
sample_match_count = int(top.match_rate * non_null_count)
```

Or: expose `matched_count` directly from `run_all_detectors` so the call site
doesn't have to recompute it from `match_rate`.

---

### F7 — LOW | Design | `_pipeline.py:200-202` — silent tie-breaking with no log on output collision

**The issue**:

```python
outputs.update(generate_outputs)
outputs.update(mask_outputs)   # mask wins ties silently
```

The comment says "no real conflicts," relying on the schema's XOR constraint. If a
config somehow produces a table name in both `generate_outputs` and `mask_outputs` (e.g.,
a future code path that relaxes the XOR rule), the generate output is silently discarded.
No log entry marks the event.

**Recommended fix**: Add an assertion or log at the merge point:

```python
overlap = set(generate_outputs) & set(mask_outputs)
if overlap:
    logger.warning("run_pipeline: tables %r appear in both generate and mask outputs; mask wins", overlap)
```

---

## 3. Performance Notes

**`fk_preservation.py` — `_check_one_fk`**: The bottleneck is `set(parent_pks.tolist())`.
`.tolist()` is O(N) memory (Python objects); the set construction is O(N) time. The subsequent
`child_fks.isin(parent_set)` is vectorized C-level. Profile with `scalene` or `memory_profiler`
on a 10M-row parent table; expect the set construction to dominate both time and memory.
Alternative: use `numpy.isin(child_arr, parent_arr)` after `.to_numpy()` on both sides —
avoids Python object creation entirely, drops peak memory by ~5-10×.

**`residual_pii.py` — `check_residual_pii`**: Runs all Storm detectors over every column
of every output frame. This is O(tables × columns × rows × detectors). For a 50-column
500k-row table with 10 detectors, this is ~250M cell evaluations. Profile with `cProfile
-s cumulative` on a representative post-mask call; the bottleneck is almost certainly
inside `run_all_detectors`'s per-column scan. Consider caching or short-circuiting
on zero-match columns early.

**`_when_gate.py` — `run_with_when_gate_polars`**: `frame.to_pandas()` at line 177 converts
the full polars frame to pandas for the predicate eval. This is a full memory copy: peak
resident set = polars frame + pandas frame simultaneously. For a 10M-row frame this is
a 2× memory spike. If the predicate matches zero rows (short-circuit at line 180), the
copy was wasted. Consider evaluating predicates natively in polars expressions for
the short-circuit path, falling back to pandas only when `mask.any()`.

---

## 4. Suggested Tests

| Test | File | What it covers |
|---|---|---|
| `test_fk_single_col_parent_cap` | `tests/unit/test_fk_preservation.py` | 10M-row parent → expect a `"warning"` finding, not OOM |
| `test_fk_composite_child_cap` | `tests/unit/test_fk_preservation.py` | 5M+ row child → expect partial-check finding |
| `test_residual_pii_generate_columns` | `tests/unit/test_residual_pii.py` | synthetic name column → severity `"info"`, not `"warning"` |
| `test_when_gate_handler_resets_index` | `tests/unit/test_when_gate.py` | handler calls `reset_index(drop=True)` → values written to correct rows |
| `test_pipeline_seed_wrong_type` | `tests/unit/test_pipeline.py` | `seed: 42.0` → explicit error, not silent None |
| `test_fk_single_col_uuid_parent` | `tests/integration/test_fk_preservation.py` | UUID-keyed parent, no OOM, correct orphan count |
| `test_residual_pii_shuffle_classifier` | `tests/unit/test_residual_pii.py` | `shuffle` strategy → `"warning"` (real values survive; confirm intentional) |
| `test_pipeline_generate_then_mask_fk` | `tests/integration/test_pipeline.py` | generate table as FK parent for mask child → FK pool resolves correctly |

---

## 5. What's Good

- **`_when_gate.py` security posture**: `local_dict={}` + `global_dict={}` scope clamping
  on `df.eval` (Dennis C1 fix) is correctly applied to both the pandas and polars paths.
  The typed error codes (`numexpr_required`, `when_expression_error`,
  `when_expression_not_boolean`) are clean and complete.

- **`fk_preservation.py` composite FK fix (H4)**: The tuple-wise `MultiIndex.isin` is the
  correct vectorized approach; the old `itertuples` per-row loop would have been O(N^2)
  on large child tables.

- **`_pipeline.py` sequencing**: The generate-first, merge-sources, then mask-second
  ordering is unambiguous and the comments document the FC-1 design decisions clearly.
  The `classify_table_kinds` helper is a clean, testable single-responsibility function.

- **`residual_pii.py` passthrough classification (Dennis M12)**: Correctly demotes
  passthrough detector hits to `"info"` rather than `"warning"`, avoiding the most
  common false-positive on intentional no-op columns.
