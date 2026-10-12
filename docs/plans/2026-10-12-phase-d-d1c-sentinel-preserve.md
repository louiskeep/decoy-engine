# Phase D / D1c: date sentinel-preserve

Status: plan (rev 1, pre-gate)

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md (overnight loop). Engine: CLAUDE.md (established
methodology, module sizing, log hygiene), compatibility-contract.md (additive
parameter rule 461).

Risk: R2. Additive config that changes masking output for opted-in values and
touches an RI invariant (date_shift can be an FK key). Plan on disk + independent
plan review + Codex plan gate required before build; observable behavior, failure
modes, and acceptance tests defined here before implementation.

## FRAME

**Problem.** The date operators `bucket_perturb` and `date_shift` perturb every
parseable in-range date. Datasets use far-future or placeholder dates as
sentinels ("never expires" = `9999-12-31`, "unknown" = `2299-01-01`), and
downstream logic tests `date = <sentinel>`. Today an out-of-pandas-range sentinel
(`9999-12-31`) survives only incidentally (it fails to parse, so the existing
unparseable passthrough keeps it), while an in-range sentinel (`2299-01-01`) is
silently perturbed and the `= sentinel` check breaks after masking.

**Definition of done.** A user can list exact date literals per field; a row
whose date equals a listed literal is emitted unchanged by `bucket_perturb` and
`date_shift` on every route that runs the operator. Unset config means no
behavior change. The preservation is explicit, format-independent, and FK/RI-safe
(a preserved value maps to itself on parent and child).

**Boundaries.** Date operators only (`bucket_perturb`, `date_shift`). Exact match
only, date granularity (Cam 2026-10-11): no ranges, no "before/after", no
wildcards. Not a detection feature; the user supplies the literals (the
`storm/sentinels.py` vocabulary is a documentation suggestion, not a default).

**Reuse check.** Both operators already restore the source value for null and
unparseable rows (oracle and native). D1c is a new member of that existing
passthrough set, not a new mechanism. No new Rust and no ABI change: the carve-out
is Python-only, applied as an extension of the existing exclusion mask on each
route, leaving the shared Rust kernels and the pandas oracle reference transform
untouched. This mirrors D1a/D1b-i (leave `hash_array` / `native_bucket_perturb`
untouched; decide and carve out in the route wrapper).

## Pinned guarantee and non-goals

**Guarantee.** When `preserve_values` is set on a `bucket_perturb` or `date_shift`
field, every source row whose value matches one of the listed literals (match rule
below) is emitted byte-identical to its source on all routes the operator runs on;
all other rows mask exactly as they do today. When `preserve_values` is unset or
empty, output is byte-identical to today on all routes (proven by the existing
baselines). For an FK key column (date_shift only), preservation holds equally on
the parent and child so referential integrity is kept.

**Non-goals.** No range/pattern matching. No new detection or auto-sentinel
defaults. No change to any operator other than the two date operators. No change
to which routes an operator runs on (date_shift stays off the out-of-core route).
No change to the config-validation precedence or the native-admission rules beyond
adding the `preserve_values` decision. 100M/200M proofs stay in D4c.

## Established solutions

- **Existing passthrough precedent (the pattern D1c follows).** Oracle
  `date_shift` builds `unusable = null | parse_failed` and restores
  `out[i] = col.iloc[i]` for those positions (`execution/_strategies/_date_shift.py`
  ~157, 216-217); oracle `bucket_perturb` `continue`s past null/parse-failed
  (`transforms/bucket_perturb.py` 166-174). Native kernels start `out` from the
  original and overwrite only valid positions (`native/_bucket_perturb_ext.py`
  227-248; `native/_date_shift_ext.py` 164-191). D1c adds the sentinel-match
  positions to that exclusion set at each site.
- **Additive-config rule.** compatibility-contract.md:461 ("new parameters are
  keyword-only with defaults that preserve existing behavior") governs the config
  shape: `preserve_values` absent = today's behavior.
- **Per-column decision shape.** D1a/D1b-i `out_of_core/_hash_native.py` /
  `_bucket_native.py` give the established per-column disposition + evidence shape
  the OOC carve-out reuses.
- **No hand-rolled parsing.** Date parsing stays on pandas `to_datetime` with the
  operator's existing `date_format`, as the operators already do.

## Match rule (specified; the design-critical part)

A source row is preserved iff EITHER holds:
1. its raw source value rendered as the string it is keyed/compared on equals a
   configured literal exactly (covers out-of-range literals that parse to NaT,
   e.g. `9999-12-31`, and string-typed sources), OR
2. its parsed date value is valid and equals the parsed configured literal
   (covers in-range literals under a non-ISO `date_format`, e.g. source
   `31/12/2299` with literal `2299-12-31`).

Rationale: the union mirrors the existing null|parse_failed union and is robust to
both the out-of-range (NaT, where equality comparison fails) and the
format-variant cases. Literals are parsed once per run (ISO-8601 date-only for the
parse arm; invalid literals are a config error, see failure modes). Comparison is
date-granular (normalized to midnight); a time-of-day in the source is not part of
the match (date-only per Cam).

Config shape: `provider_config["preserve_values"]`, a list of non-empty strings
(free-form `provider_config`, no `ColumnConfig` schema change). Validated as a
list of strings, each parseable as an ISO date OR retained as a raw-literal-only
match; duplicates de-duplicated; empty list == unset.

## Observable behavior and failure modes

- Unset / empty `preserve_values`: output unchanged on all routes (baseline-proven).
- Set, row matches: source emitted unchanged, no offset derived, no format-error
  recorded for that row (it is excluded from the format-error channel exactly as a
  null is).
- Set, row does not match: masked exactly as today.
- date_shift FK key with asymmetric `preserve_values` across the parent/child edge:
  compile-reject with `date_shift_preserve_values_fk_asymmetric` and a message
  naming both columns. Fail closed: silent RI breakage is worse than a config
  error. (bucket_perturb cannot be an FK key, so no symmetry check applies.)
- Malformed config (not a list, non-string entries, a literal that is neither a
  valid ISO date nor intended as a raw literal): `bucket_perturb_invalid_config` /
  the date_shift config error, raised identically on native and oracle, no sink
  commit.
- Native routes: `preserve_values` is admitted (taught via the Python wrapper
  carve-out, kernel untouched); it does NOT force a decline to oracle. Add the
  key to `native/_operator_config_rejections.py` only to validate shape, not to
  reject.
- Log hygiene: no date value, cell, or literal appears in any log line or reason
  code; reasons carry the operator, column, and outcome only.

## Implementation approach (per route, Python-only carve-out)

1. **Config + validation.** Read `preserve_values` in the oracle config readers
   (`transforms/bucket_perturb.py validate_bucket_perturb_config`,
   `transforms/date_shift.py validate_rule`) and in the native param resolution
   (`native/_operator_params.py` BucketPerturbParams / DateShiftParams). One shared
   helper `resolve_preserve_values(provider_config, date_format) -> PreserveSet`
   parses literals once and exposes `mask(source_array) -> bool-mask` (raw-string
   ∪ parsed-date). One place for the fact (house style).
2. **Oracle carve-out.** In each handler, union the preserve-mask into the existing
   `unusable`/skip mask before derivation and before the format-error accounting.
3. **Native-chunked carve-out.** In the route wrapper (`native/_operator_step.py`
   around the `native_bucket_perturb` / `native_date_shift` calls, or
   `native/_chunk_masking.py` reconciliation), after the kernel returns, restore
   source at preserve-mask positions and subtract them from the format-error set.
   Kernel and ABI untouched.
4. **Out-of-core carve-out (bucket_perturb only).** In
   `out_of_core/_bucket_native.py native_bucket_perturb_array` and the oracle
   `_mask_group_c._bucket_perturb_array`, apply the same preserve-mask restore.
   date_shift is not on OOC, so nothing to do there.
5. **FK symmetry check (date_shift).** New `plan/_checks_date_shift.py` rule
   `check_date_shift_preserve_values_fk_symmetry`, mirroring the existing
   `check_date_shift_group_by_refs` / group_by-on-FK pattern; compares the
   `preserve_values` set on both ends of each relationship edge a date_shift column
   participates in.

## Acceptance tests (authored before impl; no later contributor weakens these)

- **Unset == baseline:** every existing bucket_perturb/date_shift parity test
  passes unchanged; a dedicated test asserts unset `preserve_values` yields a
  byte-identical digest to the no-config run on all routes.
- **In-range sentinel preserved:** `2299-01-01` (in pandas range) is perturbed
  without the config and preserved with it, on oracle, native-chunked, and (bucket
  only) OOC; FK-chain variant proves parent and child both preserve.
- **Out-of-range sentinel preserved explicitly:** `9999-12-31` preserved with the
  config via the raw-string arm (not merely incidentally via NaT), and the
  disposition/witness shows it took the preserve path.
- **Format-variant match:** source `31/12/2299` with `date_format=%d/%m/%Y` and
  literal `2299-12-31` is preserved (parsed-date arm).
- **Non-match unaffected:** a near-but-unequal date (`2299-01-02`) still masks.
- **No format-error for a preserved row:** a preserved value that would otherwise
  be a format error is not recorded as one.
- **FK symmetry:** asymmetric `preserve_values` on a date_shift FK edge is
  compile-rejected with the dedicated code; symmetric config passes and preserves
  on both ends.
- **Config errors:** malformed `preserve_values` raises the operator config error
  identically native and oracle, with no sink commit.
- **Cross-route digest parity:** with the config set, oracle vs native-chunked vs
  OOC produce byte-identical output on a mixed sentinel/non-sentinel column (strict
  D0 parity contract + witness).
- **Log hygiene sentry:** no literal/date/cell text in logs or reason codes for the
  preserve path.

## Design notes

New structure: one `PreserveSet` resolver (shared by oracle + native + OOC; one
place per fact), one new plan-check module function (mirrors the existing
`_checks_date_shift.py` pattern), and one new provider_config key. No new module,
abstraction, or route. House style: typed `PreserveSet` over passing raw lists
around; single-purpose `mask()`; no flag arguments; comments explain why
(the raw-string ∪ parsed-date union handles NaT and format variants). Applies to
the touched code only.

## Docs to update

`docs/strategies.md` (date_shift ~130, bucket_perturb ~143 passthrough notes;
clarify the `9999-12-31` note at ~213 to the explicit guarantee),
`docs/compatibility-contract.md` (bucket_perturb/date_shift route entries 287-304;
the additive-param rule already covers it). Generated references
(capability-matrix, proof manifest, mutation ledgers) regenerate, not hand-edited.

## Open question for the gate (not a blocker)

Match rule arm 1 keys on "the string the operator compares on". For a typed
date/datetime source column (date_shift), there is no raw user string; the arm-1
comparison is against the ISO rendering of the source date. The plan treats a
typed-source column as arm-2-only (parsed-date equality), and arm 1 applies only to
string-typed sources. Codex plan gate: confirm this split is complete and that a
typed-source out-of-range sentinel (if representable) is handled or explicitly out
of scope (a date outside the pandas range cannot be held in a datetime64 column at
all, so it can only arrive as a string, where arm 1 covers it).
