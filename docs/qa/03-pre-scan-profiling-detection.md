# 03. Pre-scan, profiling and detection

## What it covers

Looking at a source before masking: `profile_columns` and `scan_sources` produce a
profile (per-field statistics, PII detector hits, sentinel flags, re-identification
risk). The 25 built-in detectors, the optional lgbm classifier and spaCy NER, strategy
recommendations, and the 10 compliance disguises (`hipaa`, `pci`, `gdpr` and the rest).

## Expected behaviour

- Pre-scan only suggests. It never changes data and never blocks a run on its own.
- A saved or displayed profile carries no raw cell values for value-sensitive columns
  unless the caller explicitly opts in (`profile_document`, `redact_profile_dict`;
  roadmap row 24c).
- `regex` detectors are always on. `classifier` and `ner` are opt-in, and a missing
  model is reported at preflight as a status, not a crash.
- The `pre_scan` block is left out of the plan hash, because it does not change output.
- A profile taken with a seed is reproducible. Without a seed, a table larger than the
  sample cap can profile differently run to run, and the engine warns.
- Each detector has a default strategy recommendation, and a disguise is data (YAML),
  not code.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| SCAN-01 | Profile a CSV with email, SSN, phone, ZIP and card-number columns. | Each column is flagged by the matching detector (`email`, `ssn`, `us_phone`, `us_zip`, `pan`). |
| SCAN-02 | Profile a column of random 9-digit numbers that are not SSNs, and a column of 16-digit numbers that fail Luhn. | Not flagged as `ssn` or `pan`, or flagged with clearly lower confidence. Record the false-positive rate. |
| SCAN-03 | Save the profile of a table with a PII column, then search the saved file for a known source value. | The value is absent. `top_values` and other sample text are withheld for flagged columns. |
| SCAN-04 | Save the same profile with the explicit include-values option. | Values are present, and the document records that the raw policy was used. |
| SCAN-05 | Profile the same file twice with the same seed, in two processes. | Byte-identical profile documents. |
| SCAN-06 | Profile a table above the sample cap with no seed anywhere. | A warning says the profile is not reproducible. |
| SCAN-07 | Request `detectors: [classifier]` without `regex`; then list a detector twice. | Both refused with `config.detectors_invalid`. |
| SCAN-08 | Request `ner` on a machine without the spaCy model. | A clear status that the model is unavailable (`ner_status`, `ModelUnavailableError`). No traceback, and no silent skip. |
| SCAN-09 | Add a `pre_scan` block to a config and compare the plan hash. | Unchanged. |
| SCAN-10 | Load all disguises with `load_disguises()`. | Ten load: `ccpa`, `cpni`, `default`, `ferpa`, `gdpr`, `glba`, `hipaa`, `pc`, `pci`, `sox`, with the rule counts in `docs/capability-matrix.md`. |
| SCAN-11 | Apply the `hipaa` disguise recommendation to a profiled healthcare table and compile the result. | Every recommended rule compiles. Each strategy named by a disguise exists. |
| SCAN-12 | Profile a column with sentinel values (`9999-12-31`, `-1`, `N/A`). | The sentinels are flagged as sentinels, not counted as ordinary data. |

## Open points

- The profiling docstring calls the consumer "FORECAST"; the public docs say Pre-scan.
  Confirm the naming users see.
- No doc states detector accuracy targets. SCAN-01 and SCAN-02 need an agreed pass
  threshold before they can pass or fail.
- `what-we-cannot-prove.md` says the ML column classifier does not de-identify
  anything. QA should confirm no output or message implies otherwise.
