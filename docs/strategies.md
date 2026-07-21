# Strategy catalog

A column's `strategy` (mask mode) or `type` (generate mode) selects how its
values are transformed or built. This page is the narrative companion to the
auto-generated API reference: what each strategy does, when to reach for it,
and the key parameters it reads from the column config.

Parameters not named here fall back to documented defaults; an invalid or
out-of-range parameter generally degrades to passthrough rather than aborting
the run (the one-bad-rule-does-not-abort contract carried from V1). Strategies
marked "needs namespace" require a `namespace` on the column; that namespace is
what keeps masked values stable and joinable.

## Mask strategies

There are seventeen mask strategies. `passthrough` (a no-op pass) and the
internal composite/nested handlers are listed separately below.

### faker

Replaces each value with a synthetic value drawn from a named provider (for
example `person_email`, `person_first_name`). In deterministic mode the same
source value maps to the same synthetic value within a namespace, which keeps
joins intact. Non-deterministic mode draws uniformly and differs run to run.

- `provider`: the provider name (required). Providers come from the engine's
  default registry.
- `namespace`: scopes determinism and joinability.

Use it for names, emails, phone numbers, addresses, and other fields where you
want realistic-looking replacements rather than a redaction.

### hash

Produces a deterministic, joinability-preserving token:
`derive(seed, namespace, value).hex()`, optionally truncated. The same source
value yields the same token within a namespace, byte-stable across runs and
processes. Nulls are preserved. Needs a namespace.

- `truncate`: positive int; keep only the first N hex characters.

Use it for opaque identifiers (SSN, MRN, account number) that must remain
join-stable but not human-readable.

### fpe

Format-preserving encryption: the output keeps the input's shape (a 9-digit
input stays 9 digits). Built on a Feistel-plus-HMAC permutation. Same value
maps to the same ciphertext within a namespace; byte-stable across runs. Needs
a namespace.

- `charset`: a named set (for example `digits`) or an explicit character set.
  A degenerate (< 2 char) set degrades to passthrough.
- `preserve_separators`: keep non-charset separators in place (default true).
- `validate_luhn`: keep Luhn-checksum validity for digit charsets (default false).
- `checksum`: scheme name for check-digit recomputation after encryption
  (default: none). When set, `checksum` takes priority over `validate_luhn`.
  After the Feistel permutation rewrites the value body, the engine recomputes
  the check digit in place, so the masked value is valid for the named scheme
  by construction. Determinism is preserved: the same input, key, and scheme
  always produce the same output.

  Supported schemes (all seven pass through `checksums.validate` and
  `checksums.calc_check_digit`): `luhn`, `npi`, `iban`, `vin`, `isbn13`,
  `ean13`, `gtin`.

  Valid-by-construction in FPE mode: `luhn`, `npi`, `vin`, `isbn13`, `ean13`,
  `gtin`. Scheme-specific constraints: NPI output pins the 1/2 NPPES leading
  digit (NPPES allocates only 1- and 2-prefixed NPIs); VIN constrains the
  permutation to the VIN alphabet (A-Z excluding I/O/Q, plus digits 0-9, per
  NHTSA 49 CFR Part 565); ISBN-13 pins the 978/979 GS1 prefix.

  Three fail-closed behaviors (no silent passthrough of unmasked data):

  1. `iban`: raises `PlanCompileError(fpe_checksum_iban_unsupported)` at
     plan-compile and `FpeChecksumError` at runtime. Per-country BBAN
     structure enforced by `stdnum.iban.validate` cannot be satisfied by a
     free Feistel permutation. For validation-only use, call
     `checksums.validate("iban", value)` directly; only FPE checksum mode is
     unsupported.
  2. Unknown or misspelled scheme: raises
     `PlanCompileError(fpe_checksum_unknown_scheme)` at plan-compile. There is
     no silent fallback to plain FPE.
  3. Incompatible charset: a charset missing characters required by the scheme
     (for example, `checksum: vin` with a digits-only charset, which is missing
     the letter characters VIN requires) raises
     `PlanCompileError(fpe_checksum_charset_incompatible)` at plan-compile.
     This prevents a silent no-op where values would pass through unmasked at
     runtime.

Out-of-charset values also fail closed: a value with no in-charset characters
(or, with `preserve_separators: false`, any out-of-charset character) fails
closed with `StrategyError(code=fpe_unencryptable_value)`, which wraps the
internal `FpeUnencryptableError`, rather than emitting the value unmasked or
partially masked. The engine no longer falls back to a non-invertible
covering hash for such values (that fallback was removed in DE-01); see the
[DE-01 FPE remediation design brief](https://github.com/louiskeep/decoy-engine/blob/main/docs/security/de-01-fpe-remediation-design.md)
(excluded from the built docs site; see `docs/conf.py`).

Use it when a downstream system validates the format of an identifier (credit
card, account number) and you cannot change its shape.

### date_shift

Shifts each date by a deterministic per-value offset within a bounded range.
Same source date maps to the same shifted date within a namespace; byte-stable
across runs. Null and unparseable values are left as-is. Needs a namespace.

- `min_days` / `max_days`: the offset range (defaults -365 and 365). If
  reversed, they are swapped.
- `date_format`: a strftime format; auto-detected from the column if omitted.

Use it for HIPAA-style date generalization where relative spacing matters but
the absolute date must move.

### bucket_perturb

Coarse time-bucket generalization: snaps each date to a deterministic position
within its ISO week, calendar month, or calendar quarter. The bucket boundary
is determined by the input date; the position within the bucket is derived
deterministically from `derive(job_seed, namespace, value)`, so the same input
value always maps to the same output position. Needs a namespace.

- `bucket` (str, required): one of `week`, `month`, or `quarter`. An invalid
  or misspelled value raises `StrategyError(bucket_perturb_invalid_config)` at
  execution time, before any row is processed (fail-closed).
- `date_format`: a strftime format; auto-detected from the column if omitted.

Bucket semantics:

- `week`: ISO 8601 week (Monday = day 0, Sunday = day 6). Output day falls in
  `[0, 6]` from the ISO week start. Dates in the same ISO week map to positions
  within that week.
- `month`: calendar month. Output day in `[1, days_in_month]`. Dates in the
  same calendar month stay in that month.
- `quarter`: calendar quarter (Q1 = Jan-Mar, Q2 = Apr-Jun, Q3 = Jul-Sep, Q4 =
  Oct-Dec). Output day in `[1, days_in_quarter]`. Dates in the same quarter
  stay in that quarter.

Determinism keying: uses `derive(job_seed, namespace, value)` (HKDF-SHA256,
S3 contract) to derive a per-value offset within `[0, bucket_size - 1]` days
from the bucket start. Namespace-bound: two columns with different namespaces
sharing the same job seed produce independent offsets. Same `(job_seed,
namespace, value)` always produces the same output, byte-stable across runs
and processes.

Null and unparseable values are passed through unchanged (same contract as
`date_shift`). When no date format is detected, the column is passed through
unchanged with a WARNING log (carry-forward: surfacing as a `QualityWarning`
is deferred).

```yaml
columns:
  - name: visit_date
    strategy: bucket_perturb
    namespace: clinical_dates
    provider_config:
      bucket: month
      date_format: "%Y-%m-%d"
```

Use it to break sub-bucket temporal precision (exact appointment day within a
month) while preserving coarse ordering and temporal density. Complementary to
`date_shift`, which preserves exact cross-record ordering but shifts all dates
by a bounded amount.

### bucketize

Rounds numeric values into fixed-width bins. Deterministic by construction
(same value maps to same bucket). Non-numeric and null values pass through.

- `width`: positive bin width, or
- `preset`: a named width (`by_year`, `by_2_years`, `by_5_years`, `by_decade`,
  `by_century`, `by_thousand`, `by_ten_thousand`).
- `format`: `lower` (bin floor, default), `range` (`lo-hi`), or `midpoint`.

Use it to generalize ages, incomes, or counts into ranges.

### categorical

Remaps values onto a fixed pool of categories. Deterministic mode maps each
source value to a category via a keyed index (same source maps to same
category within a namespace). Non-deterministic mode picks uniformly and
differs run to run. Nulls are preserved.

- `categories`: the replacement pool (required).
- `weights`: per-category floats matching `categories`; picks follow the
  configured distribution. Omit for uniform.
- `from_profile`: pull categories and weights from the source column's profiled
  distribution (resolved at plan-compile time).

Use it to remap a low-cardinality field (status, region, plan type) onto a
controlled vocabulary.

### shuffle

Permutes the non-null values within a column, preserving the multiset and the
null positions. Deterministic mode seeds the permutation from the namespace, so
it is byte-stable across runs; non-deterministic mode differs. Needs a
namespace in deterministic mode.

Use it to break the row-to-value linkage while keeping the column's exact value
distribution.

### redact

Replaces every non-null value with a fixed string. Nulls are preserved. No
keying, no namespace.

- `redact_with`: the replacement string (default `REDACTED`).

Use it when a column should simply be removed from view.

### text_redact

Span-level redaction for free-text columns: scans each cell with the built-in
PII detectors and replaces only the matched spans, leaving the surrounding text
intact. Deterministic by construction. This is what lets you sanitize a
`clinical_notes` column without destroying the clinical content (contrast with
`redact`, which replaces the whole cell).

- `detectors`: detector ids to run; `None` or empty list runs every built-in
  span detector (fail-safe: empty never means "redact nothing"). The built-in
  set includes `street_address` (house number + USPS Pub 28 C1 street suffix,
  pure regex, no model needed).
- `token`: replacement token (default `[REDACTED]`).
- `label_token`: when true, emit `[REDACTED:<detector_id>]` per match (the
  `token` value is ignored).
- `ner`: opt-in spaCy person-name/location detection (`true` for the default
  `en_core_web_sm`, or `{model: <name>}` for another installed model).
  Non-English models work through the same key: `de_core_news_sm`,
  `es_core_news_sm`, and the multilingual `xx_ent_wiki_sm` emit WikiNER-style
  `PER`/`LOC` labels, which map onto the same `person_name`/`location`
  detector ids. Install models separately
  (`python -m spacy download de_core_news_sm`) and PIN the model package
  version in deployments that need byte-stable output across environments:
  NER output is deterministic per model version, and the compiled plan stamps
  the installed version (`ner_model_version`) for the audit trail.

### text_mask

Span-level PII masking for free-text columns: scans each cell with the STORM
detector library (`iter_spans`) and masks only the PII-bearing spans,
leaving surrounding prose intact. Contrast with `redact`, which replaces the
whole cell, and with `text_redact`, which replaces every matched span with a
single fixed token. `text_mask` dispatches each span to a per-detector
strategy (`fpe` for SSN, `faker` for names, `date_shift` for dates, etc.),
so the output is more useful than blanket redaction for columns such as
clinical notes or support tickets.

- `detectors` (list or null): detector IDs to run. Null or absent runs all
  built-in span detectors. Unknown IDs are skipped silently.
- `per_detector_strategy` (dict): per-detector strategy overrides. Keys are
  detector IDs; values are `fpe`, `faker`, `date_shift`, `redact`, or
  `passthrough`. Unspecified detectors fall back to the built-in
  `DETECTOR_DEFAULTS` table.
- `unmatched_span_policy` (str, default `redact`): controls text in each cell
  not covered by any detector match. See the policy table below.
- `token` (str, default `[REDACTED]`): replacement token for the `redact`
  unmatched policy and for per-span `redact` dispatch.
- `min_days` / `max_days` (int, defaults -365 / 365): date-shift offset range
  for spans dispatched to `date_shift`.

#### Detector reachability: TIER-1 and TIER-2

The 26 detector IDs in `DETECTOR_DEFAULTS` divide into two reachability
tiers. Whether a detector's default strategy fires depends entirely on which
tier it belongs to.

| Tier | IDs | Fires under built-in path? |
|---|---|---|
| TIER 1 (11) | `email`, `ssn`, `us_phone`, `us_zip`, `pan`, `iban`, `ipv4`, `icd10`, `npi`, `url`, `street_address` | Yes. `iter_spans` produces spans for these on every call. |
| TIER 2 (15) | `person_name`, `first_name`, `last_name`, `address`, `iso_date`, `us_date`, `eu_date`, `fax_number`, `cvv`, `mrn`, `health_plan_id`, `license_num`, `vehicle_id`, `device_id`, `biometric_id` | No. `iter_spans` never emits spans with TIER-2 IDs under the built-in path. |

TIER-2 defaults are active only when spans are injected via the `extra_spans=`
parameter on `mask_cell` (for example, NER spans from
`storm.ner.iter_ner_spans`). Under the built-in path alone, person names,
free-text addresses, and dates are NOT masked, even when those detector IDs
appear in `per_detector_strategy`.

#### Unmatched span policy

| Policy | Behavior | WARNING emitted? |
|---|---|---|
| `redact` (default) | Replace unmatched text with `token`. Treats all unmatched content as potentially undetected PII. TIER-2 values (names, dates) that the built-in detectors cannot reach are tokenized instead of leaking. | No |
| `passthrough` | Pass unmatched text through unchanged. The engine emits a WARNING per cell noting that only the 11 TIER-1 detectors ran and that names, addresses, and dates not supplied via `extra_spans=` ride through in the clear. | Yes, per cell |
| `replace_with_token` | Replace unmatched text with the sentinel `[UNMATCHED]`, distinct from per-span redaction tokens. | No |

Use `passthrough` only when surrounding prose is known to contain no sensitive
content. The default `redact` is the safe choice for any column where the
TIER-1 detector set may not cover all PII present.

#### Cross-cell determinism

Each matched span is keyed by `HMAC-SHA256(job_seed, matched_text)` (RFC 2104).
The key depends only on the matched value, not on surrounding cell text, column
name, or row index. The same SSN in two different cells always produces the same
masked SSN, keeping cross-column joins intact.

#### Raw-value isolation

`matched_text` is consumed only to derive HMAC key material and drive the
strategy. It is never written to logs or evidence. A sentry test enforces this
invariant.

#### Overlap resolution

When two detected spans overlap, the leftmost span wins; ties on start position
resolve to the longer match (leftmost-then-longest). An earlier spec described
this as "longer-match-wins", which is imprecise: the primary sort key is start
position, not span length.

#### NER injection and carry-forwards

To reach TIER-2 classes (names, dates, etc.), supply pre-computed spans via
`extra_spans=` on `mask_cell`. Automatic handler-level NER wiring via a `ner:`
config key (the design is in `storm/ner.py`) is not yet implemented in the
column handler; deferred to SP-16/SP-19. HIPAA-pack default wiring shipped in SP-11.
The `decoy text-mask explain` CLI subcommand is deferred to SP-16/SP-19.

```yaml
# Minimal: run all 11 built-in span detectors; redact unmatched text.
columns:
  - name: clinical_note
    strategy: text_mask

# Targeted: SSN via FPE, email redacted; unmatched prose passes through.
# WARNING: passthrough emits a warning per cell and lets names and dates
# ride through in the clear unless NER extra_spans are supplied.
columns:
  - name: support_ticket
    strategy: text_mask
    provider_config:
      detectors: [ssn, email]
      per_detector_strategy:
        ssn: fpe
        email: redact
      unmatched_span_policy: passthrough
      token: "[REDACTED]"
```

### geo_generalize

Geographic generalization via configurable cascade levels. Two types are
supported: HIPAA Safe Harbor ZIP cascade (`type: zip`, SP-08) and H3 hex-cell
lat/lng generalization (`type: lat_lng`, SP-08b). For each row, the strategy
attempts cascade levels in order and retains the most specific level whose
in-dataset count meets the k-threshold. If no level satisfies the threshold,
the value is suppressed.

- `type` (str, required): `zip` (HIPAA Safe Harbor cascade) or `lat_lng` (H3
  geospatial generalization, requires the `[geo]` extra: `pip install
  'decoy-engine[geo]'`).
- `cascade` (list, required): ordered list of generalization levels to attempt.
  Must include `suppress` as a terminator.
- `k_threshold` (int, default `20000`): minimum in-dataset record count for a
  generalization level to be retained. The default matches the HIPAA Safe
  Harbor population threshold per 45 CFR 164.514(b)(2)(i)(B). For `type:
  lat_lng`, a lower threshold is typical because H3 resolution-9 cells cover
  only ~0.1 km2; a threshold of 5-20 is common.

Cascade levels for `type: zip`:

- `zip5`: retain the full 5-digit ZIP when at least `k_threshold` records in
  the dataset share it.
- `zip3`: generalize to the 3-digit prefix. This level is skipped entirely for
  any prefix in the HHS-restricted list, regardless of the in-dataset count.
  The restricted list is the regulatory lever: it covers every geographic unit
  with population below 20,000 per the Census-based determination (45 CFR
  164.514(b)(2)(i)(B)). The canonical 17 restricted 3-digit prefixes ship as
  `reference_tables/data/us_zip3_population.parquet`, loaded via
  `load_table("us_zip3_population")`.
- `state`: generalize to the 2-letter state abbreviation derived from the ZIP5
  via the `us_zip5_city_state` reference table. Retained when at least
  `k_threshold` records in the dataset share the same state.
- `suppress`: emit an empty string (`""`). Required as the final level; omitting
  it raises `PlanCompileError(geo_generalize_missing_suppress)`.

Cascade levels for `type: lat_lng` (H3 geospatial generalization):

The target column must contain `"lat,lng"` formatted strings (e.g.
`"47.6205,-122.3493"`). Output is the H3 cell index string (not lat/lng
coordinates). Requires the optional `[geo]` extra (`h3` library). Without
it, the handler fails closed with a clear `ImportError` naming the extra.

H3 resolution scale (source: https://h3geo.org/docs/core-library/restable/):

- `h3_resolution_9`: H3 cell at resolution 9. Average edge length ~170m,
  average area ~0.105 km2.
- `h3_resolution_7`: H3 cell at resolution 7. Average edge length ~1.2km,
  average area ~5.16 km2.
- `h3_resolution_5`: H3 cell at resolution 5. Average edge length ~8.5km,
  average area ~252 km2.
- `suppress`: emit an empty string (`""`). Required as the final level.

In-dataset counts (ZIP5, ZIP3, state for zip; H3 cell at each resolution for
lat_lng) are computed once before the cascade loop, so the threshold check is
applied consistently across all rows.

Cascade decisions are recorded in a frozen `CascadeEvidence` dataclass
(a `decisions` tuple, one label per input row). When at least one row was
generalized past the top configured cascade level, the handler surfaces a
`QualityWarning` with code `geo_generalize_cascade` in
`ExecutionResult.warnings`. The warning's `cascade_decisions` map includes
only the rows that cascaded below the top level.

Config validation runs at execution time, pre-mutation, fail-closed. Raises
`PlanCompileError` on unsupported `type`, empty `cascade`, or missing
`suppress`. Error codes: `geo_generalize_unsupported_type`,
`geo_generalize_invalid_cascade`, `geo_generalize_missing_suppress`.

```yaml
# ZIP Safe Harbor cascade.
columns:
  - name: zipcode
    strategy: geo_generalize
    provider_config:
      type: zip
      cascade: [zip5, zip3, state, suppress]
      k_threshold: 20000

# Lat/lng H3 generalization (requires [geo] extra).
columns:
  - name: coordinates
    strategy: geo_generalize
    provider_config:
      type: lat_lng
      cascade: [h3_resolution_9, h3_resolution_7, h3_resolution_5, suppress]
      k_threshold: 5
```

Use `type: zip` for HIPAA Safe Harbor de-identification of ZIP-format
geographic fields. Use `type: lat_lng` for coordinate-level geospatial
generalization where sub-kilometer precision must be suppressed.

### joint_mask

Replaces a set of logically coupled columns with a consistent tuple drawn from
a reference table (for example `zip`, `city`, and `state` from
`us_zip5_city_state`). Consistency holds because the output is a real
reference-table row, never assembled field-by-field; no per-column replacement
can produce a city/state pair that does not exist in the source data.

- `columns` (list, required): the output column names to write. Every name must
  appear in the reference table (the `id` column is excluded). All listed
  columns are written in a single pass.
- `reference` (str, required): shipped or customer-provided reference table
  name. Shipped tables: `us_zip5_city_state` (USPS/Census ACS, US 5-digit ZIP
  codes with city and state) and `vehicle_make_model_year` (NHTSA vPIC).
- `key_by` (str, required): name of the source column whose value drives
  HMAC-keyed row selection in mask mode. Not used in gen mode.
- `mode` (str, default `mask`): `mask` (deterministic, HMAC-keyed) or `gen`
  (seeded random, independent of source values).

Two modes:

- **Mask mode**: calls `ReferenceTable.keyed_row(str(key_by_value))`, which
  selects a row at position `HMAC-SHA256(job_seed, key_value) % row_count` in
  the `id`-sorted table (RFC 2104). Same key value and seed always produce the
  same row. Null `key_by` values fall back to a seeded random row.
- **Gen mode**: draws row indices from `numpy.default_rng` seeded from the job
  seed, independent of source column values. Deterministic for the same seed and
  DataFrame length.

Config validation runs at execution time, before any data is mutated
(fail-closed). Raises `PlanCompileError` on missing `columns`, missing
`key_by`, unknown `reference`, or a column name absent from the reference table.
Error codes: `joint_mask_columns_missing`, `joint_mask_key_by_missing`,
`joint_mask_reference_missing`, `joint_mask_reference_not_found`,
`joint_mask_reference_invalid`, `joint_mask_column_not_in_reference`.

**Cross-version keyed-access caveat (inherited from SP-06
`ReferenceTable.keyed_row`).** `keyed_row` selects at position
`HMAC(...) % row_count` in the `id`-sorted table. This is deterministic within
a single table version. It is NOT stable if `row_count` changes (rows added or
removed): a given `key_by` value will select a different row after a row-count
change. Do not assume cross-version key stability for `joint_mask` outputs.

```yaml
columns:
  - name: zip
    strategy: joint_mask
    provider_config:
      columns: [zip, city, state]
      reference: us_zip5_city_state
      key_by: patient_id
```

Use it when a group of columns must stay internally consistent after masking
(city in state, ZIP in city).

### code_set

Replaces a code column value with a different code drawn from a named corpus
(ICD-10, HCPCS, NDC, MCC, or a customer-supplied file). The output is always a
real corpus code and always differs from the input. Technique class:
anonymisation.

Config surface (`strategy: code_set` on a column; parameters under
`provider_config:`):

- `code_set` (str, required): corpus name. Shipped corpora: `icd10`, `hcpcs`,
  `ndc`, `mcc`. For a customer corpus, provide any name here and set
  `corpus_source: customer:<path>`.
- `chapter_preserve` (bool, default `false`): when true, restrict the
  replacement candidate pool to codes in the same chapter bucket as the input.
  For ICD-10, the chapter is the first letter (`I21` maps to chapter `I`).
  See fail-closed behavior below.
- `corpus_source` (str, default `shipped`): `shipped` loads from the bundled
  corpus directory. `customer:<absolute_path>` loads a Parquet file at the
  given path. A customer corpus must have a `code` column (string) and, when
  `chapter_preserve: true`, a `chapter` column.
- `corpus_source_version` (str, optional, HC-2): pins the expected SOURCE
  release id (e.g. `"FY2024"`) of the corpus this config expects to load.
  Independently verified at load time against the loaded corpus's embedded
  `source_version` (see Provenance below) -- if they differ, or the loaded
  corpus has no `source_version` at all, the load fails closed
  (`code_set_corpus_version_mismatch`). Applies to BOTH shipped and customer
  corpora: a shipped corpus update and a customer swapping in a different
  release fail the same way. Distinct from the corpus metadata FORMAT
  version (`corpus_version` / `CORPUS_METADATA_VERSION`), which this field
  does not pin. Unset (default): no pin, today's behavior.

Two modes:

- **MASK mode** (default): picks a replacement via `HMAC-SHA256(salt, input)
  % candidate_count` over the full corpus sorted ascending by code. The
  candidate set excludes the input code, so output is never equal to input
  (domain-exclusion idiom, RFC 2104 keying, same primitive as `fpe` and
  `joint_mask`). Same input, same job_seed, same corpus version always produce
  the same output.
- **GEN mode**: picks a code for each source row via `derive_index` keyed on
  the column namespace and the row index (HKDF+HMAC, covered by
  `SEED_PROTOCOL_VERSION`). Two columns with different namespaces sharing the
  same job_seed produce decorrelated output sequences. Same namespace, seed,
  and row index always produce the same code. Needs a namespace on the column.

`chapter_preserve` fail-closed cases (both raise `PlanCompileError`,
execution-time, pre-mutation):

- Input's chapter is absent from the corpus (`code_set_chapter_absent`). No
  silent cross-chapter fallback: falling back would return a code from a
  different chapter, silently breaking the chapter_preserve invariant.
- Input's chapter bucket has only one code, which equals the input
  (`code_set_sole_member_bucket`). No valid alternative exists; a silent
  return of the input would violate the output != input guarantee.

Customer corpus (`corpus_source: customer:<path>`): a missing `code` column or
an empty corpus raises `PlanCompileError` at execution time, pre-mutation,
before any row is changed.

Shipped corpora (under `src/decoy_engine/codesets/`; the canonical list is
`CODESET_REGISTRY` in `transforms/_codeset_provenance.py` -- adding a corpus
means adding one entry there):

- `icd10` -- ICD-10-CM diagnosis codes (CDC/NCHS + CMS). Public domain.
  Chapter is the first letter of the code, per the ICD-10-CM spec.
- `hcpcs` -- HCPCS Level II procedure/supply codes (CMS). Public domain.
- `ndc` -- FDA National Drug Code Directory entries. Public domain. The
  `chapter` column is a Decoy-defined therapeutic bucket (A/B/C/D...); NDC
  has no native chapter structure, so `chapter_preserve: true` with `ndc`
  constrains to these Decoy-defined buckets, not any native NDC hierarchy.
- `mcc` -- ISO 18245 Merchant Category Codes. Public reference enumeration;
  see NOTICE.

**Licensed code sets: upload-only, never shipped (HC-2 D2b).** The clean
split: the valid code UNIVERSE (which codes exist, their format) is what the
engine ships when it is public domain; the FREQUENCY distribution (how often
each code appears) is always learned from the customer's own data, never
shipped, because real frequencies come from claims data the engine cannot
redistribute and which differ by specialty/payer/population anyway. Some
code universes are themselves licensed, not public domain -- CPT (AMA) and
APR-DRG (3M, a proprietary grouper) -- and the engine hard-refuses to ship
them: `code_set: cpt` or `code_set: apr_drg` with `corpus_source: shipped`
(or absent) raises `PlanCompileError` (`code_set_reserved_licensed_name`) at
config-validation time, before any I/O. The only legal path for a reserved
name is `corpus_source: customer:<path>` to your own separately-licensed
copy -- exactly the same customer-corpus flow documented above, just with a
reserved name. The reserved-name set (`RESERVED_LICENSED_NAMES` in
`transforms/_codeset_provenance.py`) is deliberately disjoint from
`CODESET_REGISTRY`: a reserved name is never a shipped one.

**Schema invariants (HC-2 D2c, corpus-agnostic).** Every corpus -- shipped or
customer -- must have a `code` column whose values are non-null, non-empty,
and unique (`code_set_corpus_null_code`, `code_set_corpus_empty_code`,
`code_set_corpus_duplicate_codes`); when a `chapter` column is present, it
must be populated for every row (`code_set_corpus_incoherent_chapter`). These
checks are corpus-agnostic by design -- no code-system-specific regexes, no
mandatory `description` column -- deferred until the real full corpora (HC-1
slice 2) land and their per-system format conventions are settled.

**Verifying a corpus file without a masking job (HC-2 item 1).**
`decoy_engine.transforms.code_set.verify_corpus(path)` runs the exact same
schema and provenance checks the load path runs, and returns a frozen
`CorpusVerifyReport` (`ok`, `path`, `row_count`, `provenance` summary,
`problems`) instead of raising -- the single validation source of truth a
CLI `codesets verify`/`add` command or a platform upload check can call
before registering a corpus. Like the evidence summaries above, the
provenance field carries counts and identifiers only, never raw codes.

**HC-1 (2026-07-17): these are abbreviated seeds, not full code sets.** Every
shipped file above carries `is_seed: true` in its provenance (see below) --
today's corpora are a handful of representative codes per chapter/section,
not the complete CMS/CDC/FDA/ISO data. Row counts are intentionally not
documented here (they will change when the full data replaces the seed);
call `decoy_engine.transforms.code_set.load_corpus(name)` and check
`len(...)`, or `load_corpus_provenance(name)` for the full provenance
record. Replacing the seeds with the full public code sets is a separate,
larger change (real network-fetched CMS/CDC/FDA data, its own sourcing
verification); the provenance and scale infrastructure below already exists
for that drop-in.

**Provenance and evidence surfacing (HC-1 slice 1).** Every corpus file
carries Parquet key/value metadata: `source`, `source_url`, `license`,
`citation`, `source_version` (the source's own release identifier, e.g.
`"FY2024"`), `effective_date` (ISO date the release took effect), and
`is_seed`. This is read back at load time into a typed provenance record:

- A **shipped** corpus missing any required field (`source`,
  `source_version`, `effective_date`, `license`) fails closed
  (`PlanCompileError`, code `code_set_corpus_missing_provenance`) --
  the engine ships it, so it must be able to say where the codes came from.
- A **customer** corpus (`corpus_source: customer:<path>`) may omit
  provenance entirely (logged as a warning, not an error) -- it is not
  required for the strategy to function. If a customer corpus DOES carry a
  provenance stamp, it must be complete; a partial stamp also fails closed
  (a half-filled provenance block is worse than none).

Provenance is surfaced as evidence, not as plan state: it is stamped ONLY
into `ExecutionResult.quality_metrics['code_set_corpora']`, a list of
`{code_set, source, source_version, effective_date, license, is_seed,
row_count}` entries, one per code_set column used in the job (counts and
identifiers only -- never raw codes), from the corpus actually loaded at run
time. It is never written into the Plan YAML manifest: a code corpus is
data, which may be swapped, absent, or unreachable in whatever environment
`plan_to_yaml` happens to run in, so stamping it there would make the plan
artifact non-deterministic (a swapped/absent corpus silently changes or
drops the block) for a field that does not round-trip anyway.

**Cross-version keyed-access caveat (inherited from SP-06 corpus-sort pattern).**
MASK mode selects at position `HMAC(...) % candidate_count` over the
code-sorted corpus. Deterministic within a corpus version. NOT stable if corpus
row count changes: a given input maps to a different output code after any row
is added or removed. Do not assume cross-version MASK output stability for
`code_set` columns.

**Carry-forwards (not yet built):** additional shipped corpora LOINC, CIP,
NUCC, UPC/EAN; CPT and MedDRA bring-your-own-corpus workflow documentation; an
out-of-corpus-input `QualityWarning` signal (currently, an out-of-corpus input
is silently remapped to a real code). HIPAA-pack default wiring shipped in
SP-11.

#### Full public-domain corpora (opt-in ETL)

Decoy ships a Python ETL under `scripts/codeset_etl/` that fetches, normalizes,
and writes the complete public-domain US federal healthcare code sets from their
authoritative sources (FDA, CDC, CMS). A pipeline is not limited to the tiny
shipped seed corpora; it can point `corpus_source` at the full real data.

Available corpora:

| Corpus | Source agency | ~Row count | Chapter support |
|---|---|---|---|
| `ndc` | FDA National Drug Code Directory | 217,000 | No |
| `icd10` | CDC/NCHS ICD-10-CM (FY2026) | 74,719 | Yes (22 clinical chapters) |
| `icd10pcs` | CMS ICD-10-PCS (FY2026) | 79,115 | Yes (17 sections) |
| `hcpcs` | CMS HCPCS Level II (2026Q3) | 8,725 | No |
| `msdrg` | CMS MS-DRG Definitions (v43.0) | 772 | No |

To fetch and cache a corpus:

```bash
python -m scripts.codeset_etl.update ndc
python -m scripts.codeset_etl.update icd10
python -m scripts.codeset_etl.update icd10pcs
```

Each command writes a Parquet file to the cache directory (default:
`~/.cache/decoy-engine/codesets/`; override with `DECOY_CODESET_CACHE_DIR`). A
pipeline opts in by setting `corpus_source` to the output file using the
existing customer-corpus syntax:

```yaml
columns:
  - name: drug_code
    strategy: code_set
    provider_config:
      code_set: ndc
      corpus_source: customer:~/.cache/decoy-engine/codesets/ndc.parquet
```

**Why opt-in, not bundled.** The ETL lives under `scripts/`, which the wheel
build excludes (`pyproject.toml` specifies `packages = ["src/decoy_engine"]`).
This keeps the wheel small and fetching explicit: a user who never runs the
update command sees exactly the current shipped-seed behavior, unchanged.

**License and coverage.** These five are public domain (17 U.S.C. 105, US
federal government works). Licensed code sets that coexist in this space
(CPT by the AMA, CDT Dental codes by the ADA, APR-DRG by 3M, SNOMED CT) are
never bundled; the ETL parsers reject copyrighted codes fail-closed. A customer
licensed for any of these can supply their own file via `corpus_source:
customer:<path>` just as they would for any other custom corpus.

**Chapter preservation availability.** The `chapter_preserve` config option
works for `icd10` and `icd10pcs` because every code in those corpora carries a
chapter field (ICD-10-CM has 22 clinical chapters; PCS has 17 sections derived
from each code's first character). HCPCS and MS-DRG have no chapter field in
their source data, so `chapter_preserve: true` on those corpora fails closed at
plan-compile time (`code_set_chapter_column_missing`). Set `chapter_preserve:
true` only when the corpus is known to carry a chapter column.

```yaml
# Mask ICD-10 diagnosis codes, preserving chapter grouping.
columns:
  - name: diag_code
    strategy: code_set
    provider_config:
      code_set: icd10
      corpus_source: customer:~/.cache/decoy-engine/codesets/icd10.parquet
      chapter_preserve: true

# Mask ICD-10-PCS procedure codes, preserving section (chapter).
columns:
  - name: proc_code
    strategy: code_set
    provider_config:
      code_set: icd10pcs
      corpus_source: customer:~/.cache/decoy-engine/codesets/icd10pcs.parquet
      chapter_preserve: true

# NDC, HCPCS, MS-DRG: no chapter field, chapter_preserve not applicable.
columns:
  - name: drug_code
    strategy: code_set
    provider_config:
      code_set: ndc
      corpus_source: customer:~/.cache/decoy-engine/codesets/ndc.parquet
```

Use it for diagnosis codes, procedure codes, and other controlled-vocabulary
fields where the output must be a real code from the same code system.

### truncate

Keeps the first (or last) N characters of each value; nulls preserved.

- `length`: number of characters to keep (>= 1).
- `keep`: `head` (default) or `tail`. The legacy `from_end: true` is a
  deprecated synonym for `keep: tail`.
- `mask_char`: when set, the dropped span is replaced with this single
  character repeated (output length matches input); when unset, the dropped
  span is simply removed.

Use it for ZIP-to-3-digits generalization or "keep last 4" identifier masking.

### formula

Applies a user-defined expression to each value through a safe-eval boundary.
Deterministic by its expression; nulls pass through.

- `formula`: the expression string.

Use it for derived transforms that none of the other strategies cover. Prefer a
purpose-built strategy where one exists.

### derived

Computes a column's value from other columns in the same row via a
closed-vocabulary Lark expression (SP-06). Works in both mask mode (replaces
an existing column value) and generate mode (computes a new column from
already-generated siblings). Deterministic by construction: same row context,
same output. No RNG is involved; there is no code branching between mask and
generate paths.

**Config (mask mode):** `strategy: derived` on a column; parameters under
`provider_config:`.

**Config (generate mode):** `type: derived` on a generate column; the same
parameters sit at the top level of the column config, not nested under
`provider_config:`.

Common parameters:

- `expression` (str, required): a closed-grammar expression. Column references
  are bare identifiers (no dots, no dunders). The permitted forms are:
  arithmetic (`+`, `-`, `*`, `/`, `//`), comparison (`==`, `!=`, `<`, `>`,
  `<=`, `>=`, `in`), logical (`and`, `or`, `not`), string concat
  (`concat(a, b, ...)`, two or more arguments), substring
  (`slice(s, start[, end])`, Python-native 0-indexed slicing -- e.g.
  `slice(firstname, -4)` for the last 4 characters), date
  (`days_between(start, end)`), ternary (`value if condition else other`),
  and literals (integers, floats, double-quoted strings, `True`, `False`,
  `None`). Anything outside that set raises `ValidationError` at config-parse
  time, before any row data is touched. A column value that looks like an
  expression string is treated as data and is never re-evaluated.
- `bounds` (dict, optional): `{min: float, max: float}`. Clips numeric output
  after evaluation. Non-numeric results (strings, booleans, `None`) pass
  through unchanged. Bounds apply only when the expression produces a real
  number (int or float, but not bool). `min > max` is rejected at config-parse
  with `PlanCompileError(derived_bounds_inverted)`.
- `null_propagation` (str, default `explicit_null`): controls how `None` or
  `NaN` values in referenced columns are handled.
  - `explicit_null` (default): output is `None` when any referenced column
    is `None` or `NaN`.
  - `sentinel`: `None`/`NaN` values in referenced columns are replaced with
    `""` before evaluation; the expression always runs.
  - `default`: `None`/`NaN` values in referenced columns are coerced to `0`
    before evaluation; the expression always runs.

Validation timing:

- Expression syntax: validated at config-parse time via `compile_expr`.
  Raises `ValidationError` before any row data is touched.
- Column-ref existence: validated at plan-compile time via
  `check_derived_column_refs` in `plan/_checks.py`. A missing column ref
  raises `PlanCompileError(derived_missing_column_ref)`.
- Cyclic references (direct and transitive, detected via DFS): same check,
  same timing. Raises `PlanCompileError(derived_cyclic_ref)`. Both checks
  run before any execution begins.

Row-level evaluation errors (for example `ZeroDivisionError`, `TypeError`)
fail the job. The error message names the column and row index so the
offending data can be located without re-running. There is no silent
passthrough of rows that fail evaluation.

**Security.** The SP-06 Lark grammar is the sole security boundary on this
path. The engine calls no `eval()`, `exec()`, or `__import__`. A column value
that looks like an expression string is treated as data and is never re-parsed.

In generate mode, each derived column reads from already-generated sibling
columns in declared order. A forward reference (a derived column that
references a sibling declared later in `generate_columns`) is caught at
evaluation time with a fail-closed error, not yet at plan-compile time.
Declare dependencies before the columns that read them.

**Shipped in SP-10b:** `case_when` (closed-grammar CASE WHEN conditional
expression) and `derived_aggregate` (intra-table scalar aggregate). Neither
implements `conditioned_on`: `case_when` is a conditional expression construct
in the closed grammar; `conditioned_on` is a separate planned parameter on the
`categorical` strategy (per-row nested weight tables) and is not yet built.

**Carry-forwards (SP-10c and later, not yet built):** `grouped_series`,
`windowed_date`; FK extensions (`cardinality`, `composite_depth`, `null_m2m`);
layer-2/3 features (`conditioned_on`, `group_key`, `reconciliation_pass`). The
FK-driven cross-table aggregate (parent.total = sum(child.amount), Gen Gap 4)
is also not yet built; it is deferred to SP-10c. Forward-reference detection
at plan-compile time is also deferred.

```yaml
# Mask mode: replace an existing column value using source siblings.
columns:
  - name: age_in_months
    strategy: derived
    provider_config:
      expression: "age * 12"
      bounds:
        min: 0
        max: 1500
      null_propagation: explicit_null

# Generate mode: build a column from already-generated siblings.
# Declare first_name and last_name before full_name.
generate_columns:
  - name: first_name
    type: faker
    faker_type: first_name
  - name: last_name
    type: faker
    faker_type: last_name
  - name: full_name
    type: derived
    expression: "concat(first_name, last_name)"
```

Use it to compute a column that is a deterministic function of other columns in
the same row, without writing a custom provider.

### derived_aggregate

Computes a single aggregate scalar over a named source column in the same table
and fills every row of the target column with that scalar (SP-10b /
ISO/IEC 9075-1 set-function semantics).

**Intra-table only.** Source and target must be columns in the same table. The
FK-driven cross-table aggregate (parent.total = sum(child.amount), closing Gen
Gap 4) is not yet built; it is deferred to SP-10c. Do not use this strategy
expecting cross-table or parent-from-children aggregate behaviour.

Config keys:

- `op` (str, required): one of `sum`, `mean`, `min`, `max`, `count`. Any other
  value raises `PlanCompileError(derived_aggregate_op_invalid)` at config-parse
  time before any DataFrame is touched.
- `column` (str, required): name of the source column in the same table. A
  missing reference raises `PlanCompileError(derived_aggregate_missing_column_ref)`
  at plan-compile time.

Works in both mask mode (`strategy: derived_aggregate` on a column with `op`
and `column` under `provider_config:`) and generate mode (`type: derived_aggregate`
with `op` and `column` as top-level generate-column keys).

Null handling follows the SQL standard (ISO/IEC 9075-1): NULLs are excluded
from sum / mean / min / max; count counts non-null rows only.

**Privacy note on min / max.** sum, mean, and count produce aggregate scalars
with no individual-row signal. min and max return an actual extremal value from
the source column, broadcast to every row; that value is a real data point from
the source. Do not assume min or max hides individual data. The GDPR technique
class defaults to pseudonymisation (conservative assumption per Art 4(5) GDPR);
override at the column level if the aggregate is demonstrably non-identifying.

```yaml
# Mask mode: fill every row of 'total_paid' with the sum of 'amount'.
columns:
  - name: total_paid
    strategy: derived_aggregate
    provider_config:
      op: sum
      column: amount

# Generate mode: fill every row of 'row_count' with the count of 'id'.
generate_columns:
  - name: row_count
    type: derived_aggregate
    op: count
    column: id
```

Use it to attach a table-level summary scalar to every row (for example: a
grand total, a mean rate, or a non-null row count computed from a sibling
column).

### passthrough and structural handlers

- `passthrough`: leaves the column untouched. Use it to make an unmasked column
  explicit in the config.
- composite and nested: internal handlers. Composite columns (coherent
  multi-field synthesis such as name-plus-email or city-state-zip) are driven by
  the generation composite providers rather than a user-set `strategy`; nested
  handles struct-typed columns. You do not set these as a column `strategy`
  directly.

### Intentional collisions (allow_collisions)

By default deterministic identifier and pool strategies preserve cardinality:
distinct source values map to distinct masked values within a namespace. Set
`allow_collisions: true` on a column when you deliberately want distinct inputs
to collapse onto the same output (the classic "Tom and Peter both become Matt"
case), for example to model a smaller masked population.

```yaml
columns:
  - name: customer_name
    strategy: faker
    provider: person_first_name
    namespace: people
    allow_collisions: true
```

It is a compile-time alias for `cardinality_mode: reuse` plus `deterministic:
true`, so it requires a namespace and conflicts with an explicitly different
`cardinality_mode` (the compiler raises `allow_collisions_mode_conflict`). When
such a column is vaulted, the ambiguous source-to-masked pairs cannot be
reversed and are counted in the vault's `ambiguous_dropped`. The default stays
collision-free; this knob is purely additive.

## Generation strategies (generate mode)

In `mode: generate`, each column declares a `type` instead of a `strategy`.

### sequence

Monotonic counter.

- `start`: first value.
- `step`: increment.

Use it for surrogate primary keys.

### faker

A synthetic value per row from a named faker type.

- `faker_type`: the faker generator (for example `first_name`, `email`, `job`).
- locale and faker kwargs are supported per the generator.

### categorical

Draws each row from a fixed category pool.

- `categories`: the pool.
- `weights`: optional per-category distribution.

### reference

Draws values that reference an already-generated parent column, so a generated
child can point at a generated parent's keys.

- `reference_table` / `reference_column`: the generated parent table and column
  to draw from (both required).
- `distribution`: how draws are spread across the parent values: `random`
  (default), `sequential`, or `weighted`.
- `weights`: per-value weights, used when `distribution` is `weighted`.
- `min_per_parent` / `max_per_parent`: optional per-parent cardinality bounds
  (0 = unbounded). These bounds do not compose with `sequential`.

### formula

Computes a column from an expression over the other generated columns.

- `formula`: the Python expression evaluated per row (required).
- `references`: the sibling column names the expression reads; filled in a
  single declared-order post-pass after the other columns exist. A formula
  column that reads a later-declared referenced formula sibling sees that
  sibling's null placeholder, not its computed value. Declare dependencies
  before the columns that read them.

### derived

Computes a column from other already-generated sibling columns via the SP-06
Lark closed-grammar expression. See the `derived` mask-strategy section above
for the full config reference (expression, bounds, null_propagation),
validation timing, and security note.

In generate mode the same parameters (`expression`, `bounds`,
`null_propagation`) are top-level keys on the generate column config, not
nested under `provider_config:`. Sibling columns must be declared before the
`derived` column that reads them; a forward reference fails at evaluation time.

### distribution

Samples rows whose distribution matches a provided snapshot (numeric,
categorical, or datetime). Use it to generate a column shaped like a real
source column's distribution.

- `snapshot`: a dict with `kind` (`numeric`, `categorical`, or `datetime`) and a
  `stats` block. `numeric` needs `bin_edges` + `bin_counts`; `categorical` needs
  `top_values` + `other_count`; `datetime` needs `year_bins` + `min` + `max`.
  This matches `compute_distribution_snapshot`'s output.

## Job-level config: validators and quarantine (SP-05, extended Sprint 2 honesty pack)

These two top-level blocks run after all column passes complete. They operate on
the assembled output tables, not on individual columns mid-run.

### validators:

Declares a list of validators to run against the pipeline outputs. The engine
runs them in declared order after all column passes complete. A validator failure
fails the job by default (fail-closed).

```yaml
validators:
  - name: luhn
    columns:
      orders: [credit_card_number]
  - name: npi
    columns:
      providers: [npi_number]
  - name: iban
    columns:
      payments: [iban_field]
  - name: vin
    columns:
      vehicles: [vin_number]
  - name: fk_intact
  - name: no_orphan_children
  - name: leak_check
    columns:
      orders: [card_number, email]
    params:
      exempt: {orders: [status_code]}
      max_identical_ratio: 0.02
      min_rows: 1
  - name: regex_match
    columns:
      orders: [order_code]
    params:
      pattern: "[A-Z]{2}\\d{4}"
  - name: column_in_set
    columns:
      orders: [status]
    params:
      allowed_values: [active, inactive, pending]
      allow_null: true
  - name: parent_window_respected
    params:
      child_table: events
      child_column: event_date
      parent_table: subscriptions
      window_start_column: window_start
      window_end_column: window_end
  - name: reconciliation_holds
    params:
      parent_table: orders
      parent_column: total
      child_table: order_items
      child_column: amount
      op: sum
      tolerance: 0.01
```

Built-in validators (11):

- `luhn`: Luhn mod-10 check digit per column. Delegates to
  `checksums.validate('luhn', value)` (SP-04).
- `npi`: US National Provider Identifier check digit per column. Delegates to
  `checksums.validate('npi', value)`.
- `iban`: IBAN ISO 13616 mod-97 check digits per column. Delegates to
  `checksums.validate('iban', value)`.
- `vin`: VIN ISO 3779 check character per column. Delegates to
  `checksums.validate('vin', value)`.
- `fk_intact`: Every non-null child FK value resolves to a parent PK. Reads the
  `relationships:` block. Uses the SDV HMA1 parent-first DAG pattern.
- `no_orphan_children`: Every child row has a non-null FK value. Uses the SDV
  HMA1 parent-first DAG pattern.
- `leak_check` (Sprint 2 honesty pack): compares masked output values against
  their source values per column and flags residual source values. Established
  methodology: the Delphix/Informatica "masking verification" pre/post-value
  comparison pattern and the Great Expectations
  `expect_column_pair_values_A_to_not_equal_B` check. Two tiers: a COLUMN tier
  (every non-excluded column; `identical_ratio == 1.0` over non-null cells means
  the strategy had no effect at all) and a CELL tier (TRANSFORMATIVE strategies
  only -- `hash`, `fpe`, `redact`, `text_redact`, `faker`, `code_set`, and
  composite-bundle columns -- `identical_ratio > max_identical_ratio` flags
  residual per-row leaks, quarantinable row-by-row). `passthrough` columns,
  FK-child columns, and `when:`-gated columns are excluded by construction.
  Requires the pre-mask source table for every table in scope (passed by
  `run_pipeline` automatically); a table in scope with no source raises rather
  than silently skipping.
- `regex_match`: every non-null value in the named columns matches
  `params.pattern` via `re.fullmatch` (whole-cell; GE
  `expect_column_values_to_match_regex` semantics).
- `column_in_set`: every value in the named columns belongs to
  `params.allowed_values` (str-canonicalized; GE
  `expect_column_values_to_be_in_set` semantics). `params.allow_null` (default
  `true`) controls whether null cells are findings.
- `parent_window_respected`: every child date falls within its parent's
  declared window (inclusive bounds; pairs with the `windowed_date` generate
  strategy). Reads the matching `relationships:` edge the same way `fk_intact`
  does.
- `reconciliation_holds`: a parent aggregate cell reconciles with its child
  rows under an absolute `params.tolerance` (default `1e-6`; pairs with the
  `derived_aggregate` strategy). `params.op` is `sum` (default) or `count`.

Parameters:

- `name` (required): one of the 11 built-in validator names above. Unknown
  names raise `ValueError` at the `validate()` call.
- `columns` (optional for FK/relationship validators): dict mapping table name
  to a list of column names. Required for `luhn`, `npi`, `iban`, `vin`,
  `regex_match`, `column_in_set`; optional scope-narrowing for `leak_check`
  (default: all non-excluded columns of every mask-kind table). Unused for
  `fk_intact`, `no_orphan_children`, `parent_window_respected`, and
  `reconciliation_holds`, which read `relationships:` instead.
- `params` (Sprint 2 honesty pack): free-form per-validator knob bag. Each
  validator validates its own `params` at run time with a loud `ValueError`
  naming the validator and the offending key.

Results are persisted to the evidence manifest under
`quality_metrics["validation"]["validators"]` as a serialised `ValidationReport`
(frozen dataclass with `passed`, `validators_run`, `findings`, `elapsed_ms`).

Fail-closed by default: any validator failure raises `ValidatorFailedError`
(exported from `decoy_engine.errors`), which carries the `ValidationReport` for
inspection. Enable `quarantine:` with the `validation_fail` trigger to route
failing rows to a JSONL file instead of failing the job.

### Per-row strategy errors (Sprint 2 honesty pack, D7/D8)

Independent of the `validators:` block, `bucketize`, `date_shift`, and
`code_set` can each produce PER-ROW errors during masking itself (not a
post-mask check): a non-null `bucketize`/`date_shift` cell that fails
numeric/date coercion (`format_error`), or a `code_set` `chapter_preserve`
value that cannot be masked -- input chapter absent from the corpus, or a
sole-member chapter bucket (`mask_error`).

**Behavior change (pre-GA hard cutover, no flag):** before this sprint, such a
cell silently kept its ORIGINAL source value in the masked output -- a silent
per-row leak. Now the job either fails loud by default (`RowErrorsFailedError`,
exported from `decoy_engine.errors`, naming counts by table/column/trigger with
no cell values) or, when quarantine is enabled with the matching trigger, the
offending rows are removed from the main output and written to the quarantine
JSONL exactly like a validator finding. There is no way to restore the old
silent-leak behavior.

Row-error counts are persisted to the evidence manifest under
`quality_metrics["row_errors"]` (counts by `table.column[trigger]`, no cell
values). `ExecutionResult.row_errors` carries the full `RowErrorRecord` tuple
for callers that want per-row detail before quarantine/fail-closed disposition.

### quarantine:

Routes rows that fail validation OR hit a per-row strategy error to a separate
JSONL file instead of failing the job. When active, the job continues and
completes successfully; only the failing rows are removed from the main output.

```yaml
quarantine:
  enabled: true
  output_path: /mnt/quarantine/run-2026-06-27.jsonl
  triggers:
    - validation_fail
    - format_error
    - mask_error
```

Parameters:

- `enabled` (bool, default `false`): activates the quarantine block.
- `output_path` (str, required when `enabled: true`): path for the JSONL output
  file. Must be non-empty and non-whitespace when `enabled` is `true`; an empty
  path with `enabled: true` raises at config validation to prevent silent data
  loss.
- `triggers` (list): which conditions route rows to quarantine. Three triggers
  are wired:
  - `validation_fail` (SP-05): row failed a job-level validator.
  - `format_error` (Sprint 2 honesty pack S5): a `bucketize`/`date_shift` cell
    could not be coerced/parsed.
  - `mask_error` (Sprint 2 honesty pack S6): a `code_set` value could not be
    masked under `chapter_preserve`.
  A trigger name is accepted ONLY once its producer is wired and tested (no
  pre-added names); an unrecognised trigger raises at config validation.

Each quarantined row is written as one JSON object containing all original column
values plus three metadata fields: `_quarantine_trigger` (name of the first
trigger that fired), `_quarantine_reason` (human-readable explanation from the
first finding/row-error), and `_source_table` (output table name).

Deduplication: a row that fails multiple validators/triggers appears once in the
JSONL file (first entry wins for `_quarantine_trigger` and `_quarantine_reason`;
validator findings are ordered before row errors). `total_quarantined` counts
distinct rows removed from the main output. `counts_by_trigger` tallies per
finding/row-error and may sum higher when a row fails multiple triggers. The
JSONL file is not written when no rows are quarantined.

Quarantine state is persisted to the evidence manifest under
`quality_metrics["quarantine"]` as a serialised `QuarantineSummary` (frozen
dataclass with `enabled`, `output_path`, `counts_by_trigger`, `total_quarantined`).

Fail-closed guards (no silent data loss):

1. `enabled: true` with an empty or whitespace `output_path` raises at config
   validation. A backstop in `apply_quarantine` covers callers that bypass
   Pydantic config validation.
2. An unwired trigger name raises at config validation. A silent no-op is
   rejected up front.
3. A misconfigured FK/relationship validator (unknown parent table or column
   in `relationships:`, or an ambiguous/missing relationship edge) raises at
   `validate()` call time rather than mass-flagging every row.
4. A per-row strategy error whose trigger is NOT quarantine-enabled fails the
   job with `RowErrorsFailedError` rather than silently keeping the leaked
   value in the output (D8).

## Infrastructure: expression parser and reference tables (SP-06)

The features in this section are infrastructure built in SP-06. `joint_mask`
(SP-08), `code_set` (SP-09), `derived` (SP-10), `case_when` (SP-10b), and
`derived_aggregate` (SP-10b) are now built and documented in their strategy
sections above.

### Closed-vocabulary expression parser

Source: `src/decoy_engine/expressions/_lark_parser.py` + `grammar.lark`.

Config-supplied expressions for the `derived` strategy (SP-10, built) and
the `case_when` built-in (SP-10b, built) route through a Lark EBNF closed
grammar. The grammar is the complete security boundary: only the listed forms
can parse. Any expression outside the set raises `ValidationError` at compile
time, before any row data is touched. There is no `eval()` or dynamic code
execution on this path.

Two safety bounds are checked before parsing:

- Maximum expression length: 4096 characters.
- Maximum parenthesis nesting depth: 50 levels.

#### Permitted operator set

| Category | Permitted forms |
|---|---|
| Arithmetic | `+` `-` `*` `/` `//` |
| Comparison | `==` `!=` `<` `>` `<=` `>=` `in` |
| Logical | `and` `or` `not` |
| String | `concat(a, b, ...)` (two or more arguments) |
| Substring | `slice(s, start[, end])` (Python-native 0-indexed, half-open, negative-from-end slicing; out-of-range indices clamp rather than error, e.g. `slice(firstname, -4)` for the last 4 characters) |
| Date | `days_between(start, end)` (integer days; accepts `datetime.date` or ISO-8601 strings) |
| Conditional | `case_when(cond1, val1, ..., condN, valN, default)` -- **non-short-circuit**: all sub-expressions are evaluated before branch selection; every branch value must be safe to evaluate for all rows |
| Ternary | `value if condition else other` |
| Literals | integers, floats, double-quoted strings, `True`, `False`, `None` |
| Column refs | bare identifiers (no dots, no dunders) |

Anything not in the table above is rejected. This includes: function calls
other than `concat`, `slice`, `days_between`, and `case_when`, attribute
access (`.`), subscript syntax (`[]`), `import`, and dunder identifiers
(`__class__`, etc.).

String literals must use double quotes (`"hello"`); single-quoted strings
(`'hello'`) are not in the grammar. String escape sequences are validated at
compile time.

#### API

```python
from decoy_engine.expressions import compile_expr, evaluate, CompiledExpression

compiled: CompiledExpression = compile_expr("age + 1")   # once per column
value = evaluate(compiled, {"age": 30})                  # called at row evaluation time
```

`CompiledExpression` is immutable and safe to share across threads and rows.
`compile_expr` raises `ValidationError` on any expression outside the closed
set or outside the safety bounds. `evaluate` raises `KeyError` when a column
reference is absent from the row context.

The existing `formula` strategy continues to use `safe_eval` (simpleeval
sandbox, separate evaluator in `_safe_eval.py`). The two evaluators are
intentionally separate: `formula` uses a permissive-but-sandboxed approach;
`derived`/`case_when` use the closed-grammar approach with zero dynamic
execution surface.

### Reference-table loader

Source: `src/decoy_engine/reference_tables/`.

Reference tables are static Parquet datasets loaded once per pipeline and
used by `joint_mask` (SP-08) to look up replacement values (for example: map
a ZIP code to a canonical city/state pair, or draw a vehicle make/model from a
controlled set). The `code_set` strategy (SP-09) uses a separate corpus loader
in `decoy_engine/codesets/`, not the reference-table loader.

#### Schema convention

Every reference table must have:

- `id` column, type `int64` (enforced at load; raises `ValueError` otherwise).
  Rows are sorted ascending by `id` at load time to establish a stable,
  file-order-independent row ordering.
- Domain columns specific to the table (for example `zip`, `city`, `state`
  for the US ZIP table).

Shipped tables carry a `decoy_table_version` string in Parquet file-level
metadata. A mismatch between the file version and the engine's expected version
is logged as a WARNING; the table is still used.

#### Shipped tables

| Name | Version | Source | Content |
|---|---|---|---|
| `us_zip5_city_state` | 1.0 | USPS/Census ACS | US 5-digit ZIP codes, city, state (public domain; 50-row foundation slice) |
| `vehicle_make_model_year` | 1.0 | NHTSA vPIC | Vehicle make, model, year (public domain; 50-row foundation slice) |

#### Customer-provided tables

Replace a shipped table with a fuller dataset by passing a Parquet path:

```python
from pathlib import Path
from decoy_engine.reference_tables import load_table

table = load_table("us_zip5_city_state", path=Path("/data/us_zip_full.parquet"))
```

The file must follow the same schema convention: an `id` column (int64) plus
the expected domain columns.

#### API

```python
from decoy_engine.reference_tables import load_table

table = load_table("us_zip5_city_state")
row = table.row(0)                      # {"id": 10001, "zip": "10001", ...}
row = table.keyed_row("some-masked-pk") # deterministic per key_value
```

`load_table` raises `FileNotFoundError` when no shipped table exists for the
given name and no `path` is supplied. It raises `ValueError` when the file is
unreadable or lacks the required `id` column.

#### Keyed-access semantics and cross-version caveat

`ReferenceTable.keyed_row(key_value)` selects a row by reducing an
HMAC-SHA256 digest of `key_value` modulo `row_count` over the `id`-sorted row
order. Access is deterministic for a given `key_value` within a single table
version (same row count, same `id` column).

Adding or removing rows changes `row_count`, which remaps the modular index.
A given `key_value` will then select a different row. Cross-version key
stability is NOT guaranteed. `joint_mask` (SP-08) ships with this caveat
documented; operators must not assume cross-version key stability for
`joint_mask` outputs. `code_set` (SP-09) carries the same caveat: see the
cross-version caveat in the `code_set` strategy section above.
