# 09. Domain strategies

Status: plan

## What it covers

Strategies tied to a subject area: `geo_generalize` (ZIP and lat/lng), `joint_mask`
(coupled columns from a reference table), `code_set` (ICD-10, HCPCS, NDC, MCC and
customer corpora), the coherent composites (`composite_name_email`,
`composite_city_state_zip`, `composite_address`, `composite_person`,
`composite_provider`), the identifier providers (SSN, NPI, EIN, MRN, NDC and others),
`group_key` and `grouped_series`.

## Expected behaviour

- **geo_generalize, `type: zip`**: for each row, keep the most specific level in
  `cascade` (`zip5`, `zip3`, `state`) whose count in the dataset meets `k_threshold`
  (default 20,000). Otherwise suppress to an empty string. The cascade must end in
  `suppress`.
- **geo_generalize, `type: lat_lng`**: turns `"lat,lng"` strings into H3 cell ids at the
  cascade's resolutions. Needs the `[geo]` extra and fails closed without it.
- **joint_mask**: writes a whole real row from a reference table (`us_zip5_city_state`,
  `vehicle_make_model_year`) into the listed columns, chosen by a key column, so the
  values always belong together.
- **code_set**: replaces a code with a different real code from the corpus. Output never
  equals input. `chapter_preserve` keeps the replacement in the same chapter and fails
  closed where it cannot. Licensed sets (`cpt`, `apr_drg`) are refused unless the
  customer supplies the file.
- **Composites**: columns linked with `coherent_with` are replaced together, so the
  email matches the name and the ZIP matches the city and state.
- **Identifier providers**: outputs are valid for their format (check digits included).
- **group_key**: every row sharing a `group_by` value gets the same key; different
  groups get different keys.
- **grouped_series**: `cumcount` counts rows within a group; `monotone_walk` gives
  non-decreasing values within a group.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| DOM-01 | `geo_generalize` zip cascade with `k_threshold: 5` on a table where one ZIP5 has 10 rows, one ZIP3 has 6 rows across rare ZIP5s, and one ZIP is unique. | Rows keep ZIP5, fall to ZIP3, or are suppressed accordingly. A `geo_generalize_cascade` warning lists the rows that cascaded. |
| DOM-02 | Cascade without `suppress`; unknown `type`; empty cascade. | `geo_generalize_missing_suppress`, `geo_generalize_unsupported_type`, `geo_generalize_invalid_cascade`. |
| DOM-03 | `type: lat_lng` without the `[geo]` extra. | Fails closed with an error naming the extra. |
| DOM-04 | `joint_mask` over `zip`, `city`, `state` keyed by `patient_id`. | Every output triple is a row of the reference table. The same `patient_id` gets the same triple. |
| DOM-05 | `joint_mask` with a column not in the reference table; unknown reference; missing `key_by`. | `joint_mask_column_not_in_reference`, `joint_mask_reference_not_found`, `joint_mask_key_by_missing`, all before any row changes. |
| DOM-06 | `code_set: icd10` on a column of valid codes. | Every output is a corpus code and differs from its input. Rerun is identical. |
| DOM-07 | `code_set` with `chapter_preserve: true` where one input's chapter is absent from the corpus. | That row is a `mask_error`: the job fails, or the row is quarantined if the trigger is on. No cross-chapter fallback. |
| DOM-08 | `code_set: cpt` with the shipped source; then with `corpus_source: customer:<path>`. | First refused with `code_set_reserved_licensed_name`. Second runs. |
| DOM-09 | `corpus_source_version: "FY1999"` against a corpus stamped otherwise. | Load fails with `code_set_corpus_version_mismatch`. |
| DOM-10 | Run a `code_set` job and read `quality_metrics["code_set_corpora"]`. | One entry per code_set column with source, version, licence, `is_seed` and row count. No codes. |
| DOM-11 | `composite_city_state_zip` across `city`, `state`, `zip` with `coherent_with`. | Every output triple is a real combination. Rerun is identical. |
| DOM-12 | `composite_name_email` across first name, last name and email. | The email's local part reflects the masked name on the same row. |
| DOM-13 | Mask with `synthetic_ssn`, `synthetic_npi`, `synthetic_ein` providers. | Every output passes the format's validator. None equals its source. |
| DOM-14 | `group_key` with `group_by: household_id`; `grouped_series` with `cumcount`. | Same household, same key; different households, different keys. `cumcount` restarts at each group and follows `order_by`. |

## Open points

- The shipped code-set corpora are abbreviated seeds (`is_seed: true`), a handful of
  codes per chapter. With a seed corpus, many inputs map to few outputs. Decide whether
  V1 QA runs against the seeds or the full corpora from `scripts/codeset_etl/`.
- An input code that is not in the corpus is silently remapped to a real code (a
  documented carry-forward). Confirm that is acceptable for V1.
- `joint_mask` with a null `key_by` falls back to a seeded random row. `joint_mask` and
  `code_set` outputs change when the reference table's row count changes.
- Composites are internal handlers reached through `strategy: faker` with a composite
  provider. `docs/strategies.md` does not document `coherent_with`; the only example is
  `examples/runnable_demo.py`.
- `composite_address`, `composite_person` and `composite_provider` have no checks yet.
  Their expected coherence rules (which fields agree) are not written down anywhere.
