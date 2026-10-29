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

1. Source framing (CSV quoting, fixed-width slices) yields an exact string or bytes. A column the caller already converted cannot be re-read: the cast refuses it (`declared_cast_input_type_incompatible`).
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

The boundary exists on the full-frame route. A job with declared columns therefore runs full-frame (the engine pins `auto` routing to it and turns off auto-chunk and the unified slice). An explicit `execution_mode` of `sequential` or `out_of_core`, and the chunked and mixed routes, are refused before any source is read with `declared_cast_route_unsupported`, by the one central decision `decoy_engine.declared_casting.evaluate_declared_casting`. A route that gains the boundary declares the capability in `ROUTE_CAPABILITIES`; it does not get its own parser.

## Counts and safe output

`ExecutionResult.quality_metrics["declared_casting"]` carries bounded integers keyed by table, column and cause: `sentinel_to_null`, `parse_null`, `parse_quarantine`, `output_null`, `output_quarantine` (and `parse_fail` / `output_fail` on an aborted run), with `total_substitutions`, `total_failures`, `quarantined_rows` and `completeness` (`complete`, `partial_before_abort`). Values, sentinel tokens, parser exception text and paths never appear in logs, progress events, reasons or evidence. Every failure is a registered F29 reason (see [Reason codes](reasons.md)).

## Protected quarantine

See [Protected quarantine](security/protected-quarantine.md).

## Compatibility

`PLAN_VERSION` is 3. A version-2 plan loads with no declared contracts; a plan naming an unknown format or catalog version never executes. `dtype` remains a legacy route hint and conflicts with a declared `logical_type` are rejected. `date_shift.provider_config.date_format` stays a strategy parameter; declaring a column parse format as well is rejected rather than choosing one. The legacy `quarantine.output_path` writer is unchanged and does not satisfy `error_policy: quarantine`.

## Not in V1

Ambient locale parsing, Unicode/CLDR currency inference, natural-language dates, time or timezone semantics, exponent notation, arbitrary grouping, rounding modes, permissive COBOL sign nibbles, copybook import, automatic encoding detection, field-level quarantine, and declared casting on the sequential, chunked, out-of-core, mixed and unified routes. A truthful refusal is the answer for those, not an approximation.
