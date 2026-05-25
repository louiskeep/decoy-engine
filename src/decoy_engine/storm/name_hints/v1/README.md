# STORM Detector Name-Hint Library (v1)

This directory holds the column-name patterns the STORM scan uses to
recognize what KIND of data is in each column of an uploaded file. The
files were extracted from a hard-coded Python dict
(`decoy_engine.storm.detectors._NAME_HINTS`) on 2026-05-25 so the
patterns can be reviewed in YAML diffs rather than buried in a
400-line Python literal.

## File layout

```
v1/
  manifest.yaml      # Lists the files and pins library_version.
  base.yaml          # Contact + networking detectors (email, ssn, phone, ...).
  identity.yaml     # Personal-name + address + fax detectors.
  dates.yaml        # ISO / US / EU date detectors (shared vocabulary).
  finance.yaml      # PAN / CVV / IBAN payments detectors.
  healthcare.yaml   # MRN / NPI / ICD-10 / health-plan / device / biometric detectors.
  identifiers.yaml  # License / vehicle identifier detectors.
```

## Entry format

Each YAML file declares a `detectors:` list. Each entry maps a
`detector_id` (which MUST match a `detect_*` function in
`decoy_engine.storm.detectors`) to the column-name substrings whose
presence boosts that detector's confidence at scan time.

```yaml
detectors:
  - detector_id: email
    description: Email address column.
    patterns:
      - email
      - e_mail
      - mail
      - contact_email
```

The loader (`decoy_engine.storm.name_hints.loader`) reads every file
listed in `manifest.yaml`, builds a case-insensitive regex per
detector via the existing `_hint(terms)` helper, and exposes the
result as the module-level `_NAME_HINTS` dict in
`decoy_engine.storm.detectors`. Behavior matches the pre-extraction
hard-coded dict bit-for-bit (verified by
`tests/snapshots/test_name_hints_baseline.py`).

## Adding a new pattern to an existing detector

1. Open the file containing that detector_id (search the directory).
2. Add the substring to its `patterns:` list. The order is not
   significant for correctness; group related forms for
   readability.
3. Run the snapshot test:
   ```
   pytest tests/snapshots/test_name_hints_baseline.py
   ```
4. If it fails (matrix drift): the new pattern is matching a header
   that previously didn't match. Confirm that's the intended effect,
   then regenerate the baseline:
   ```
   UPDATE_SNAPSHOTS=1 pytest tests/snapshots/test_name_hints_baseline.py
   ```
   The commit MUST explain WHY the snapshot drift is legitimate.

## Adding a new detector

The YAML library is one half of the work. The other half is a
`detect_*` function in `decoy_engine.storm.detectors`. The loader
will silently accept a YAML entry with no matching detector function
(no `detect_*` to call), but no scan will ever consult that entry.
Follow the existing pattern (see `detect_npi`, `detect_ssn`, etc.):
implement the function with value-shape regex + confidence
thresholds, register it in the per-column profile loop, then add the
YAML entry here.

## What does NOT live here

- The confidence thresholds (`NAME_HINT_MIN_RATE`,
  `HIGH_CONFIDENCE_WITH_HINT_FLOOR`, etc.) -- those are tuning
  constants in `decoy_engine.storm.detectors`, not data.
- The value-shape regexes (the patterns that match against actual
  cell content) -- those live in the `detect_*` functions and are
  Python regex objects, not YAML.
- The per-customer overrides -- those are a follow-up sprint. For
  now, customers who need cryptic enterprise patterns can clone any
  built-in detector into a custom detector via the Detectors tab in
  admin Settings and extend its name_hints field there.

## Versioning

`library_version` in `manifest.yaml` is the human-readable string
that ships with each release. Bump it when you change patterns. The
schema_version (`name-hints/v1`) only bumps when the YAML shape
changes incompatibly (a `v2/` directory would live alongside `v1/`
and the loader would pick the highest available).
