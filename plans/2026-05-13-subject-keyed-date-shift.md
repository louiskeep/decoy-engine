# Block 4 — subject-keyed `date_shift` (Hripcsak SANT correctness)

> **Status:** plan — part of the 2026-05-13 audit-fix series.
> **Standalone:** no dependency on Blocks 1–3.
> **Repos touched:** `decoy-engine` only.

## Why

`transforms/date_shift.py` today computes the offset from `MD5(value)` — see the docstring at `CAPABILITIES_GUIDE.md §1` ("MD5-based deterministic per-value shift"). This is **value-keyed**: the same DOB everywhere produces the same shift, regardless of which patient owns it.

What this breaks: imagine patient P with `dob=1985-03-01`, `admission_date=2023-07-15`, `discharge_date=2023-07-18`. With value-keyed shifting, `admission_date` and `discharge_date` are offset by *different* deltas (because the input values differ), so the 3-day stay becomes random. The Hripcsak et al. 2016 SANT method (*JAMIA*) is the field-standard fix: **a per-subject random offset, applied uniformly to every date column belonging to that subject**, plus truncation of the leading/trailing 366 days.

This is a HIPAA Safe Harbor quality bug. The reference (§6, date-of-birth row) calls out "Never offer constant offset across all subjects without a warning — it is trivially reversible" — but the *current* behavior is also wrong, in the other direction: temporal order within a subject is destroyed.

## Code paths

- `src/decoy_engine/transforms/date_shift.py` — primary change.
- `src/decoy_engine/transforms/base.py:BaseMaskingStrategy.apply` — extend signature.
- `src/decoy_engine/masker/masker.py` — call sites pass `context_df` for row-aligned strategies.
- `src/decoy_engine/transforms/formula.py` — already row-aligned via masker; serves as the pattern.
- `src/decoy_engine/disguises/hipaa.yaml` — opt in to subject-keyed mode when patient_id is present.

## Engine changes

### 1. Extend the strategy contract

```python
# transforms/base.py
class BaseMaskingStrategy:
    def apply(
        self,
        column: pd.Series,
        rule: dict,
        *,
        context_df: Optional[pd.DataFrame] = None,
    ) -> pd.Series:
        ...
```

Every existing strategy ignores `context_df`; `date_shift` and `formula` consume it. Back-compat: callers that don't pass `context_df` keep working.

### 2. `DateShiftStrategy.apply` — new path

```python
# transforms/date_shift.py
def apply(self, column, rule, *, context_df=None):
    subject_col = rule.get("subject_key_column")
    if subject_col and context_df is None:
        raise ConfigError(
            f"date_shift on column '{rule['column']}' requested "
            f"subject_key_column='{subject_col}' but the masker did not "
            f"pass context_df; check masker integration"
        )
    key = self._column_key(rule.get("column", "date"))
    min_d, max_d = rule.get("min_days", -365), rule.get("max_days", 365)
    if subject_col:
        subject_values = context_df[subject_col]
        shifts = subject_values.map(
            lambda sv: self._offset_for_subject(sv, key, min_d, max_d)
        )
    else:
        # legacy: value-keyed (kept for back-compat)
        shifts = column.map(
            lambda v: self._offset_for_value(v, key, min_d, max_d)
        )
    # ... apply shifts to parsed dates, preserve format, etc.
```

`_offset_for_subject(sv, key, min_d, max_d)`:

```python
def _offset_for_subject(self, subject_value, key, min_d, max_d):
    if pd.isna(subject_value):
        # Subjects with no key get the legacy value-keyed path (best-effort)
        return 0
    raw = hmac.new(key, str(subject_value).encode(), hashlib.sha256).digest()
    span = max_d - min_d
    return int.from_bytes(raw[:8], "big") % span + min_d
```

### 3. Truncate first/last days (SANT step 2)

New rule param `truncate_first_last_days: bool` (default `false`). When `true`, after the per-subject shift, any output date falling in the leading 366 days of the dataset's min date *or* the trailing 366 days of max date is set to NULL. Implement as a column-level post-pass in `DateShiftStrategy.apply` — needs `context_df` for the dataset-wide min/max anyway.

### 4. Masker wiring

`masker.py` already runs strategies per-column. Add the `context_df=df` kwarg at every `strategy.apply(...)` call site. The change is mechanical; covered by existing tests.

### 5. HIPAA Disguise YAML opt-in

```yaml
# disguises/hipaa.yaml
- match:
    detectors: ["iso_date", "us_date", "eu_date"]
  mask: date_shift
  params:
    min_days: -30
    max_days: 30
    subject_key_column: patient_id      # opt-in: requires a column with this name
    truncate_first_last_days: true
  notes: |
    Hripcsak et al. 2016 SANT method. Same patient's dates all shift by the
    same delta; first/last 366 days of the dataset are dropped. Requires a
    'patient_id' column in the input. Falls back gracefully when absent.
```

Graceful fallback: if `subject_key_column` is set but the named column is missing from `context_df`, log a `WARNING` and revert to legacy value-keyed shifting. The Disguise stays usable on schemas without a canonical subject column.

## Tests to add

`tests/unit/test_date_shift_subject_keyed.py`:
- **Within-subject consistency:** 3 rows for `patient_id=42` with different dates → all three shift by the same delta.
- **Across-subjects independence:** rows for `patient_id=42` and `patient_id=43` → different deltas.
- **Relative-order preservation:** for any subject, `admission_date < discharge_date` before → `admission_date < discharge_date` after.
- **Back-compat:** rule without `subject_key_column` produces byte-identical output to the legacy behavior on the same fixture.
- **NaN subject:** rows where `patient_id` is NaN get a 0-offset (no shift) and a log warning.
- **Truncation:** date in first 366 days → NULL.

## Verification

1. `pytest tests/unit/test_date_shift_subject_keyed.py -v` — green.
2. Existing `pytest tests/unit/test_date_shift.py -v` — green (back-compat).
3. Manual: run HIPAA disguise on a synthetic patient-events CSV; visually confirm all of patient 42's dates shift together.

## Performance

`map` over the subject column is O(N). HMAC per *unique* subject can be memoized via `functools.lru_cache(maxsize=None)` inside the strategy instance — most healthcare tables have many rows per subject.

## Risk

The contract change to `BaseMaskingStrategy.apply` is breaking for any out-of-tree custom strategies. Mitigations:
- Default-`None` kwarg, so existing call sites compile.
- Document in CHANGELOG.
- The `formula` strategy is currently the only consumer; promoting it to use `context_df` formally cleans up a known wart.
