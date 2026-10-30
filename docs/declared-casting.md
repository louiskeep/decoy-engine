# Declared parsing, casting and quarantine

Status: current (catalog version 1, plan version 3)

A column can declare how its text or bytes are read, what to do when a value does not fit, and how the masked value is written back. The engine owns the contract, the format catalog, the reason codes and the counts. The CLI and Studio render and author it; neither re-implements parsing.

## Declaring a column

```yaml
- name: age
  strategy: bucketize
  provider_config: {width: 10, format: lower}
  logical_type: integer
  parse_format: {id: integer/ascii/v1, signed: true, bit_width: 64}
  sentinel_to_null: {id: sentinel/exact/v1, values: [""]}
  error_policy: fail
  output_format:
    id: integer/zero-pad/v1
    width: 3
    width_unit: code_points
```

| Field | Meaning |
|---|---|
| `logical_type` | `string`, `integer`, `decimal`, `date` or `currency`. Required with `parse_format`. |
| `parse_format` | One catalog entry (below). Every symbol, token and separator is declared; none is inferred. |
| `sentinel_to_null` | Optional exact tokens that become null **before** parsing. |
| `error_policy` | `fail` (default), `null` or `quarantine`. |
| `output_format` | Optional catalog entry that renders the masked value. Required for a fixed-width target. |

Unset fields are omitted from dumps, so a config that does not use them serializes and hashes exactly as before.

A column with `error_policy: quarantine` also needs the one top-level block (there is deliberately no path or key in config):

```yaml
protected_quarantine:
  schema_version: 1
  access_policy: tenant-job-owner
  retention_seconds: 604800
  max_rows: 100000
  max_bytes: 1073741824
  required_protection: encrypted
```

## Order of operations

1. Source framing (CSV quoting, fixed-width slices) yields an exact string or bytes. Every CSV reader (local, cloud, bounded) reads a declared column with no type inference and no default NA conversion, so `NA` or an empty field reaches the boundary as that text. A column the caller already converted cannot be re-read: the cast refuses it (`declared_cast_input_type_incompatible`). Binary COBOL formats need a source that carries bytes; compile refuses them on CSV and fixed-width sources. A consumer that reads CSV itself passes the declared columns to the reader: `decoy_engine.declared_casting.lexeme_columns_by_table(config)` gives them per table, and `iter_csv_chunks(..., lexeme_columns=...)` keeps them as exact lexemes (profiling does this itself through `profile_descriptors`).
2. **Sentinel**: an exact match becomes null (counted as `sentinel_to_null`). A physical null stays null and is never a substitution.
3. **Parse** to `str`, `int`, `Decimal` or `date`; Arrow carries them as `string`, sized `int`/`uint`, `decimal128(p, s)` and `date32`. A failure follows the column's `error_policy`.
4. The masking strategy runs on the typed value.
5. **Format**: `output_format` renders the masked value; a failure follows the same `error_policy`. The formatter runs the parser as an exact-inverse oracle on what it emitted.

## Error policies

| Policy | Scope | Effect |
|---|---|---|
| `fail` | run | abort; `DeclaredCastValueError` carries a registered code and value-free `declared_casting_counts` (`partial_before_abort`). |
| `null` | field | the field becomes null, the row stays. |
| `quarantine` | row | the whole row leaves normal output and is stored once, encrypted, in the protected quarantine; every failing field is counted and its reason recorded. |

## The format catalog

`decoy_engine.declared_casting.capability_manifest()` exports the machine-readable catalog (formats, directions, physical kind, width rule, JSON schema). `tests/fixtures/f06/capability-manifest-v1.json` pins it.

| Id | Reads / writes | Notes |
|---|---|---|
| `string/exact/v1` | text | no trim, case fold or normalization |
| `integer/ascii/v1` | text | ASCII digits, one optional leading sign; `signed`, `bit_width` 8/16/32/64 define the range |
| `integer/zero-pad/v1` | output | requires `width`, `width_unit`; the sign consumes width |
| `decimal/plain/v1` | text | `precision` 1..38, `scale` 0..precision, `.` radix, no grouping or exponent |
| `decimal/grouped/v1` | text | adds one-character `radix` and `group`, canonical Western 3-digit grouping |
| `date/iso8601/v1` | text | exactly `YYYY-MM-DD` |
| `date/strptime/v1`, `date/strftime/v1` | text | `%Y %m %d` once each, separated by `- / . space : _ ,` |
| `currency/explicit/v1` | text | `currency_code` (3 uppercase letters), `token` (literal, at most 32 UTF-8 bytes), `token_position`, `sign_style` (`leading_minus` or `accounting_parentheses`), precision, scale, radix, optional group |
| `cobol/overpunch-ascii-trailing/v1` | text | trailing sign in the last character: positive `{ABCDEFGHI`, negative `}JKLMNOPQR` |
| `cobol/zoned-ebcdic-trailing/v1` | bytes | digit zones `F`; final zone `A C E` (+) / `B D` (-) when signed, `F` when unsigned; output is canonical `C`/`D`/`F` |
| `cobol/comp-3/v1` | bytes | `ceil((precision+1)/2)` bytes, sign nibble `C` / `D` (or `F` unsigned), leading nibble zero for even precision |

Numbers are exact: text becomes an unscaled integer and a declared scale, built as `Decimal(i).scaleb(-s)` inside an engine-pinned `decimal` context, so a caller's or thread's ambient context cannot change a result. There is no float, rounding or locale. A value that would lose a digit, exceed `precision`, or overflow `bit_width` fails with a registered code.

Binary kinds list sentinels as hex strings and match the raw field bytes exactly, with no pad removal. Fixed-width output widths include the sign, token, separators and padding, and are measured in `width_unit` (`code_points` or `bytes`); the fixed-width writer is a code-point writer, so only `code_points` is admitted for a fixed-width target.

## Fixed-width sources

A fixed-width layout feeding a declared column must read the field as `str` and set `exact_record_width: true`, so an over-length record is a framing error (`fixed_width_record_overlong`) and never a value that bleeds into the next field. Layouts without the flag behave exactly as before, including legacy `int`/`float` types.

## Where it runs

One batch-shaped kernel (`DeclaredCastRun.cast_batch` / `format_batch`) serves every route; a resident table is a single batch and a streamed one is cast chunk by chunk with absolute row offsets, so any partition gives the same typed rows, counts and quarantine set. The generated, pinned matrix is `tests/fixtures/f06/route-format-matrix-v1.json`; a route parity test per route proves which route ran (`tests/acceptance/test_f06_route_parity.py`).

| Route | Capabilities | Where the boundary sits |
|---|---|---|
| full-frame | all | whole resident tables, before the adapter and after it |
| unified slice | all but cast-before-join | the resident table before the slice; its result is rendered and sealed |
| chunked | all but cast-before-join | per chunk, between the source slices and the masker, and between the masker and the joined output |
| sequential | all | each table as it is loaded; parent key maps are built from typed values and rendering follows them |
| mixed | cast, format, counts, binary | per chunk of each mask table, inside the chunked lane |
| out-of-core | cast, format, counts, binary | every declared source is cast once into a typed source (a private Parquet file for a lazy one), then streamed; rendering follows the join |

`cast-before-join` is needed only when a declared column is itself a relationship endpoint. Row quarantine needs a route that can drop rows without desyncing something already built from them, which the mixed route (pre-resolved key column) and the out-of-core route (parent-key relations) cannot, so they refuse it. Dropping a row after masking (a format-stage quarantine) on a table that is a relationship parent is refused on every route, under `auto` too (`post_mask_parent_row_quarantine`, which no route provides): the child keys were already mapped against the dropped row and no route re-applies the orphan policy after publication. A parse-stage quarantine of a parent row is fine; the dropped row never reaches the key maps, so `orphan_policy` applies as usual. A refusal is decided by the one central evaluator `decoy_engine.declared_casting.evaluate_declared_casting` from the capability sets in `ROUTE_CAPABILITIES`, before any source is read; under `auto` each route's own admission predicate consults it, so the router falls back to a capable route instead of failing. A sentry requires every route that claims a capability to have a wired applier and a parity test.

When a declared job falls back to a less bounded route, the result says why. `quality_metrics["execution"]["declared_route_refusals"]` holds one value-free entry per route the contract kept the run off: `{route, namespace, code, missing}`, where `code` is the registered refusal from that route's own admission decision (`declared_cast_route_unsupported`, with the capabilities the route lacks) or, for the multi-table split, `declared_casting_full_frame`. A route is listed only when it was on offer to the job (the mixed route for a generate-plus-mask job, the out-of-core route for a relational job, and so on); an undeclared job has no such key. With `explain_plan`, a run that ends on the full-frame lane repeats the list inside `execution_plan`; a run that falls back to the sequential route (including after a chunked decline) has no `execution_plan`, so `quality_metrics["execution"]` is the one place to read it. A forced `execution_mode` still refuses instead of falling back.

Notes: a declared job keeps one route for the whole job (the multi-table split is off) and its output is resident, not streamed into a sink, because the quarantine seal must follow every other failure-capable step. A decimal or currency column with no `output_format` is not chunk-stable (its Arrow precision is rebuilt per chunk), so the planner keeps such a table off the chunked route with a stated reason.

## Counts and safe output

`ExecutionResult.quality_metrics["declared_casting"]` carries bounded integers keyed by table, column and cause: `sentinel_to_null`, `parse_null`, `parse_quarantine`, `output_null`, `output_quarantine` (and `parse_fail` / `output_fail` on an aborted run), with `total_substitutions`, `total_failures`, `quarantined_rows` and `completeness` (`complete`, `partial_before_abort`). Values, sentinel tokens, parser exception text and paths never appear in logs, progress events, reasons or evidence. Every failure is a registered F29 reason (see [Reason codes](reasons.md)).

## Protected quarantine

See [Protected quarantine](security/protected-quarantine.md).

## Compatibility

`PLAN_VERSION` is 3. A version-2 plan loads with no declared contracts; a plan naming an unknown format or catalog version never executes. `dtype` remains a legacy route hint and conflicts with a declared `logical_type` are rejected. `date_shift.provider_config.date_format` stays a strategy parameter; declaring a column parse format as well is rejected rather than choosing one. The legacy `quarantine.output_path` writer is unchanged and does not satisfy `error_policy: quarantine`.

## Not in V1

Ambient locale parsing, Unicode/CLDR currency inference, natural-language dates, time or timezone semantics, exponent notation, arbitrary grouping, rounding modes, permissive COBOL sign nibbles, copybook import, automatic encoding detection, field-level quarantine, and row quarantine on the mixed and out-of-core routes, declared relationship endpoints on the chunked, out-of-core, mixed and unified-slice routes, and streamed (sink) output of a declared table. A truthful refusal is the answer for those, not an approximation.
