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
  post-pass after the other columns exist.

### distribution

Samples rows whose distribution matches a provided snapshot (numeric,
categorical, or datetime). Use it to generate a column shaped like a real
source column's distribution.

- `snapshot`: a dict with `kind` (`numeric`, `categorical`, or `datetime`) and a
  `stats` block. `numeric` needs `bin_edges` + `bin_counts`; `categorical` needs
  `top_values` + `other_count`; `datetime` needs `year_bins` + `min` + `max`.
  This matches `compute_distribution_snapshot`'s output.
