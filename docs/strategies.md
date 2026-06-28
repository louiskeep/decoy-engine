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

There are eleven mask strategies. `passthrough` (a no-op pass) and the
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

### distribution

Samples rows whose distribution matches a provided snapshot (numeric,
categorical, or datetime). Use it to generate a column shaped like a real
source column's distribution.

- `snapshot`: a dict with `kind` (`numeric`, `categorical`, or `datetime`) and a
  `stats` block. `numeric` needs `bin_edges` + `bin_counts`; `categorical` needs
  `top_values` + `other_count`; `datetime` needs `year_bins` + `min` + `max`.
  This matches `compute_distribution_snapshot`'s output.

## Job-level config: validators and quarantine (SP-05)

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
```

Built-in validators:

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

Parameters:

- `name` (required): one of the six built-in validator names above. Unknown
  names raise `ValueError` at the `validate()` call.
- `columns` (optional for FK validators): dict mapping table name to a list of
  column names. Required for `luhn`, `npi`, `iban`, and `vin`. Unused for
  `fk_intact` and `no_orphan_children`, which read `relationships:` instead.

Results are persisted to the evidence manifest under
`quality_metrics["validation"]["validators"]` as a serialised `ValidationReport`
(frozen dataclass with `passed`, `validators_run`, `findings`, `elapsed_ms`).

Fail-closed by default: any validator failure raises `ValidatorFailedError`
(exported from `decoy_engine.errors`), which carries the `ValidationReport` for
inspection. Enable `quarantine:` with the `validation_fail` trigger to route
failing rows to a JSONL file instead of failing the job.

### quarantine:

Routes rows that fail validation to a separate JSONL file instead of failing the
job. When active, the job continues and completes successfully; only the failing
rows are removed from the main output.

```yaml
quarantine:
  enabled: true
  output_path: /mnt/quarantine/run-2026-06-27.jsonl
  triggers:
    - validation_fail
```

Parameters:

- `enabled` (bool, default `false`): activates the quarantine block.
- `output_path` (str, required when `enabled: true`): path for the JSONL output
  file. Must be non-empty and non-whitespace when `enabled` is `true`; an empty
  path with `enabled: true` raises at config validation to prevent silent data
  loss.
- `triggers` (list): which conditions route rows to quarantine. Only
  `validation_fail` is wired in SP-05. Two names are reserved for future
  wiring but not yet active:
  - `format_error`: reserved, not wired. Rejected at config validation.
  - `mask_error`: reserved, not wired. Rejected at config validation.

Each quarantined row is written as one JSON object containing all original column
values plus three metadata fields: `_quarantine_trigger` (name of the first
trigger that fired), `_quarantine_reason` (human-readable explanation from the
first `ValidatorFinding`), and `_source_table` (output table name).

Deduplication: a row that fails multiple validators appears once in the JSONL
file (first finding wins for `_quarantine_trigger` and `_quarantine_reason`).
`total_quarantined` counts distinct rows removed from the main output.
`counts_by_trigger` tallies per finding and may sum higher when a row fails
multiple validators. The JSONL file is not written when no rows are quarantined.

Quarantine state is persisted to the evidence manifest under
`quality_metrics["quarantine"]` as a serialised `QuarantineSummary` (frozen
dataclass with `enabled`, `output_path`, `counts_by_trigger`, `total_quarantined`).

Three fail-closed guards (no silent data loss):

1. `enabled: true` with an empty or whitespace `output_path` raises at config
   validation. A backstop in `apply_quarantine` covers callers that bypass
   Pydantic config validation.
2. An unwired trigger (`format_error`, `mask_error`) raises at config
   validation. A silent no-op is rejected up front.
3. A misconfigured FK validator (unknown parent table or column in
   `relationships:`) raises at `validate()` call time rather than
   mass-flagging every row.

## Infrastructure: expression parser and reference tables (SP-06)

The features in this section are infrastructure built in SP-06 and are not
yet directly reachable from column config. The strategies that consume them
(`derived`, `case_when`, `derived_aggregate`, `code_set`, `joint_mask`) are
planned for SP-08 through SP-10 and are not yet built.

### Closed-vocabulary expression parser

Source: `src/decoy_engine/expressions/_lark_parser.py` + `grammar.lark`.

Config-supplied expressions for the future `derived` and `case_when`
strategies route through a Lark EBNF closed grammar. The grammar is the
complete security boundary: only the listed forms can parse. Any expression
outside the set raises `ValidationError` at compile time, before any row
data is touched. There is no `eval()` or dynamic code execution on this path.

Two safety bounds are checked before parsing:

- Maximum expression length: 4096 characters.
- Maximum parenthesis nesting depth: 50 levels.

#### Permitted operator set

| Category | Permitted forms |
|---|---|
| Arithmetic | `+` `-` `*` `/` `//` |
| Comparison | `==` `!=` `<` `>` `<=` `>=` `in` |
| Logical | `and` `or` `not` |
| String | `concat(a, b)` (exactly two arguments) |
| Date | `days_between(start, end)` (integer days; accepts `datetime.date` or ISO-8601 strings) |
| Ternary | `value if condition else other` |
| Literals | integers, floats, double-quoted strings, `True`, `False`, `None` |
| Column refs | bare identifiers (no dots, no dunders) |

Anything not in the table above is rejected. This includes: function calls
other than `concat` and `days_between`, attribute access (`.`), subscript
syntax (`[]`), `import`, and dunder identifiers (`__class__`, etc.).

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
used by the future `code_set` and `joint_mask` strategies to look up
replacement values (for example: map a ZIP code to a canonical city/state
pair, or draw a vehicle make/model from a controlled set).

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
stability is NOT guaranteed. This constraint must be revisited and resolved
before the `joint_mask` (SP-09) and `code_set` (SP-08) strategies make any
cross-version stability assumptions.
