# Phase D / D1c: date sentinel-preserve

Status: plan (rev 4, pre-gate). rev 1 NO-GO (6 HIGH/1 MEDIUM), rev 2 NO-GO (4
HIGH/2 MEDIUM/1 LOW), rev 3 NO-GO (1 HIGH/2 MEDIUM; core design confirmed sound).
rev 4 folds the final three: compile-reject `preserve_values` + `group_by`
(the group-anchor snapshot index conflict); reject `%Z` and make pandas the
authoritative parser (drop the universal-equivalence claim); and a precise
scalar-only, single-edge, non-composite, non-same-table FK boundary. Plus the LOW
refinements (date-granular typed rendering, one shared matching pass, a
value-and-mixture-dependent pre-existing-bug characterization). Grounded on
Codex-verified behavior + probes (pandas 2.3.3 / pyarrow 26.0.0).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine CLAUDE.md (established methodology, module sizing,
log hygiene, "don't add machinery for 1% scenarios; refuse them fail closed"),
compatibility-contract.md (additive-parameter rule 461).

Risk: R2. Additive config that changes masking output for opted-in values and
interacts with FK/RI. Plan + independent review + Codex plan gate before build.

## FRAME

**Problem.** `bucket_perturb` and `date_shift` perturb every parseable in-range
date. Datasets mark rows with fixed sentinel dates ("never expires" = `9999-12-31`,
"unknown" = `2099-01-01`); downstream logic tests `date = <sentinel>`. Today an
out-of-pandas-range sentinel (`9999-12-31`) survives only incidentally (coerces to
NaT, kept by the unparseable passthrough); an in-range sentinel (`2099-01-01`) is
silently perturbed and the check breaks.

**Definition of done.** A user lists exact date literals per field; a row whose
date equals a listed literal is emitted unchanged on every route the operator runs
on. Unset config = byte-identical output to today.

**Boundaries and v1 scope (deliberately narrow; the tail is refused fail-closed).**
- Date operators only. Exact match, date granularity (Cam 2026-10-11).
- `preserve_values` REQUIRES an explicit `date_format` on the field (both
  operators, string and typed sources). This is the single most load-bearing
  narrowing: it removes autodetect-dependent matching and guarantees a rendering
  format exists. Unset/empty `preserve_values` retains today's behavior exactly,
  including bucket_perturb/date_shift autodetection.
- Supported `date_format` domain under preservation is restricted to formats whose
  parse agrees between pandas and Python `datetime`, OR where pandas is authoritative
  (see match rule): reject sub-second directives (`%f`), timezone-name directives
  (`%Z`, which pandas parses but Python `strptime` rejects, so the full-range
  fallback cannot match it), and the pandas-special `date_format` values (`ISO8601`,
  `mixed`) when `preserve_values` is set. None describe a date-only sentinel.
- `preserve_values` combined with date_shift `group_by` is compile-REJECTED
  (`date_preserve_values_with_group_by_unsupported`). The group-anchor path requires
  the anchor snapshot index to equal the column index (`_date_shift.py:128`), which
  the physical row exclusion below breaks; group_by is itself an FK-restricted,
  narrower feature, so combining it with preservation is a tail refused fail-closed
  (v2). Preservation without group_by (the common payload case) is fully supported.
- FK: preservation is supported on PAYLOAD date columns and on a date key in a
  SINGLE-HOP parent->child relationship. A date key in a multi-hop FK chain
  (A->B->C) or a multi-parent shared child, with `preserve_values` set on any key
  in that component, is compile-REJECTED (`date_preserve_values_unsupported_fk_shape`),
  recorded as a v2 follow-up. Rationale: orphan REMAP re-runs the IMMEDIATE
  parent's handler at each hop, so uniform preservation across a chain needs
  per-hop machinery for a rare shape; refusing fail-closed is correct per the
  engine rule and never silently breaks RI.

**Reuse check.** Both operators already restore source for null/unparseable rows.
D1c physically excludes preserved positions from compute on EVERY route (oracle
and native), so preserved rows never reach derivation, the range guard, date
arithmetic, or whole-column formatting. No new Rust, no ABI change. Mirrors
D1a/D1b-i (leave the shared operator and oracle transform untouched; decide and
carve out in the wrapper).

## Pinned guarantee and non-goals

**Guarantee.** With `preserve_values` set (and the required `date_format`), every
source row whose date matches a listed literal is emitted unchanged (string source:
byte-identical source string; typed source: the source date rendered with the
field's `date_format`), on all routes the operator runs on and in a supported FK
shape; all other rows mask exactly as today. Unset/empty: byte-identical to today.
In a supported single-hop FK, the preserved value reaches the child through the
existing parent-map/REMAP mechanism, so RI is retained by the same construction
that gives it for masked keys.

**Non-goals.** No range/pattern matching, no detection/defaults. No other operator.
No route changes (date_shift stays off OOC). No support for multi-hop/multi-parent
FK preservation in v1 (refused fail-closed). No fix for the pre-existing
typed-out-of-range ArrowTypeError (recorded separately). 100M/200M proofs in D4c.

## Config, match, and output (the design-critical contract)

**Config.** `provider_config["preserve_values"]`: a list of ISO-8601 date literals,
each validated via `datetime.date.fromisoformat` (years 0001-9999, so out-of-ns
sentinels are valid; rejects `not-a-date`, `NaT`, empty/whitespace, `2020-02-30`).
De-duplicated into a set of `datetime.date`. Empty == unset. Setting it requires a
`date_format` in the supported domain above (else a config error). Free-form
`provider_config`; no `ColumnConfig` schema change.

**Match (operator-consistent in range, full-range out of range, agreeing domain).**
Resolve the literal set once per run. Per batch, parse each source cell ONCE to a
calendar date, then test date-granular membership in the literal set:
- **String source:** parse with the field `date_format`. Because the format domain
  is restricted (no `%f`, no pandas-special values) and date-granular, the pandas
  parse (`pd.to_datetime(date_format)`) and a Python `datetime.strptime(date_format)`
  agree for every in-range value; for an OUT-of-range value pandas yields NaT while
  Python yields the true date, so the match uses: in-range -> the operator's own
  pandas-parsed date (zero divergence from how the row would otherwise mask);
  NaT-from-pandas -> a single Python `strptime` fallback under the same format
  (full range). A cell that fails both is not preserved and masks/passes exactly as
  today. Date granularity: compare `.date()` (time-of-day dropped).
- **Typed date source** (`datetime64`, Arrow `date32`/timestamp): compare the
  source value's calendar date directly to the literal set (no reparse). Timezone
  aware: compare on the local wall-clock date (documented).

**Pandas is authoritative whenever it succeeds** (no universal parser-equivalence
claim, which is false in general: e.g. `%Y-%W-%w` `2020-00-1` gives pandas
`2020-01-01` vs Python `2019-12-30`, and `%Z` pandas-parses but strptime rejects).
The algorithm never diverges from how a row otherwise masks because in range it
uses the operator's own pandas parse; the Python `strptime` fallback runs ONLY on a
pandas-NaT cell (which the operator would pass through regardless), and its supported
domain is bounded by the format restriction above (no `%f`/`%Z`/pandas-special), so
the one case it must get right (a date-only out-of-range sentinel like `9999-12-31`)
parses deterministically. An out-of-range value under a rejected format is simply
not matchable and stays today's behavior; this is why `%Z` is rejected under
preservation.

**Output for a preserved row (output column is always a strftime string).** A
rendering format always exists (required above):
- String source: emit the original source string unchanged (byte-identical).
- Typed source: extract the source value's calendar `date` (time-of-day and
  timezone dropped, consistent with the date-granular match), then render it with
  the field `date_format` via Python `strftime` (full range;
  `date(9999,12,31).strftime(...)` works), keeping the column homogeneous string.
  Never restore a raw `date`/`Timestamp` object into the string column (the HIGH-4 /
  ArrowType trap).

## Per-route carve-out (physical exclusion on EVERY route, pre-compute)

Compute preserve positions from the ORIGINAL input (after the operator resolves its
format against the full original column), then exclude those rows from compute and
scatter their rendered output back. Never format/derive/guard a preserved row.

1. **Oracle handlers (rev-2 HIGH-1 fix).** In `_strategies/_date_shift.py` the
   handler parses, shifts, and `strftime`s the WHOLE column (202-203); unioning
   preserved rows into `unusable` (216-227) stops derivation but NOT the whole-column
   `.dt.strftime`, which still crashes on a mixed/overflow column. So on the oracle
   route, physically take the non-preserved SUBFRAME, run parse/derive/shift/format
   on it alone (handle an empty remainder), and scatter the rendered preserved
   strings back at their indices. Same physical exclusion in
   `transforms/bucket_perturb.py` (166-178). Preserved rows are excluded from the
   format-error channel, and format-error row indices are rebased against the
   original positions after scatter.
2. **Native-chunked and native full-frame.** In the Python wrapper around the kernel
   (`native/_operator_step.py` ~418/443, reconciliation `native/_chunk_masking.py`
   244-268), filter preserved rows out of the array passed to the kernel, run the
   kernel on the remainder, scatter rendered strings back, subtract from the
   format-error set. Kernel returns `pa.string()`, so the column stays homogeneous.
   Covers the shared full-frame operator path AND the chunked path.
3. **Out-of-core (bucket_perturb only; date_shift is not on OOC, confirmed
   `out_of_core/_compat.py:70,288`).** In `out_of_core/_bucket_native.py`
   `native_bucket_perturb_array` and oracle `_mask_group_c._bucket_perturb_array`,
   compute the preserve mask first and apply `_guard_native_range` ONLY to
   non-preserved rows, so a near-bound sentinel does not abort; then filter/scatter.

Boundary sentinels therefore never reach the kernel, so the native date_shift
`OverflowError` at the 2262 bound (`_date_shift_ext.py:184`) and the OOC range guard
apply only to non-preserved rows.

## Config validation (active paths; rev-2 HIGH-2 fix)

Verified: date_shift's `transforms/date_shift.py::validate_rule` is legacy/unused;
the active handler has no `preflight`, so a zero-row `when:` gate skips all checks.
bucket_perturb's plan-compile check `plan/_checks_bucket_perturb.py` returns early
when `date_format` is absent and only checks format writability; the operator
contract DOES support autodetect (`transforms/bucket_perturb.py:192`), so an
explicit format is not currently mandatory in general (only on native admission).

**Design.** Add ONE shared preservation validator (literal grammar + format-domain +
the required-format rule) and wire it at CONFIG time on both operators so a
zero-row/zero-match job still rejects bad config, identically native and oracle,
no sink commit:
- bucket_perturb: call the shared validator from the plan-compile check
  `check_bucket_perturb_config` (NOT only from `validate_bucket_perturb_config`) AND
  from the handler `preflight` (which fires under a zero-row `when:` gate,
  `_when_gate.py:240-242`); cover nested children. Error
  `bucket_perturb_invalid_config` / a compile code.
- date_shift: add a plan-compile check in `plan/_checks_date_shift.py`
  (`check_date_preserve_values`, `PlanCompileError`), since the active handler has
  no config-time validator. Include the FK-shape rejection and the required-format
  rule.
- Native param resolution (`native/_operator_params.py`) reads `preserve_values`;
  the admission resolver (`native/_operator_config_rejections.py`) accepts the key
  (admit, not decline). Preserve OOC ordering (`out_of_core/_runner.py:183`).

## FK / RI contract (single-hop only in v1)

Verified: chunked FK self-mask admits ONLY `hash` (`_chunked_fk.py:444`), so a date
FK key runs full-frame/sequential. There the PARENT key column owns the transform;
a normal child row inherits the parent's masked output via the source->masked map,
and an orphan child is remapped by re-running the IMMEDIATE parent's handler/config
(`_strategies/_orphan.py` 118-144; multi-parent first-hit-wins
`_pandas_adapter.py:497`).

**Design.** Supported: preservation on a date key in a single parent->child edge is
PARENT-OWNED; `preserve_values` goes on the parent key; normal child rows inherit,
and the single-hop orphan REMAP re-runs the parent handler (which preserves). A
`preserve_values` on a remapped child key is compile-rejected (configure the
parent). Multi-hop chains and multi-parent shared children with preservation on any
key in the component are compile-rejected
(`date_preserve_values_unsupported_fk_shape`) because per-hop orphan REMAP would
need preservation on each intermediate handler; refused fail-closed, recorded as v2.

**Precise unsupported boundary (rev-3 MEDIUM-3 fix).** The relationship graph nodes
are whole column-tuples (`relationships/_graph.py:264`), so a scalar-vs-composite
check cannot rely on table-level or tuple-identity reasoning alone. Define a scalar
FK participation as a `(table, column)` pair and inspect RESOLVED relationship
participation: a date column carrying `preserve_values` is supported only when it
participates in exactly one parent->child edge as a SCALAR endpoint on both ends,
the parent and child tables differ (no same-table `T.parent_date -> T.child_date`
edge), and the column does not also participate in any COMPOSITE endpoint. Reject
with `date_preserve_values_unsupported_fk_shape` when the column appears in: a
composite key endpoint, both a scalar and a composite endpoint (overlap), a
same-table edge, more than one edge (chain or multi-parent), or any cycle. This is
stricter than "not a chain"; it refuses composite and same-table participation
explicitly rather than letting a tuple-node check see two composite hops sharing a
date column as independent single-hops. bucket_perturb as an FK key is rejected on
the chunked route (existing) and otherwise parent-owned on full-frame under the same
single-hop scalar rule. Payload date columns (the common case) have no FK
interaction and are always supported.

## Pre-existing bug (separate finding, dtype-specific; rev-2 LOW-7)

Independently of D1c, a date source whose object form holds a non-null value pandas
cannot represent in ns (Arrow `date32`/timestamp, or a tz-naive `datetime64[s]`,
holding year > 2262) crashes both operators today: the existing passthrough restores
a `date`/`Timestamp` object into the strftime-string column and
`pa.Table.from_pandas` (`_pandas_adapter.py:342`) raises `ArrowTypeError`. The
trigger depends on the VALUES and the resulting mixed output column (a non-null
typed object sitting among strftime strings), not dtype alone: an all-out-of-range
Arrow-date column can convert cleanly, and wide-unit numpy datetimes differed
between the two operators in probing. Record as a value-and-mixture-dependent engine
bug with an exact reproducer, separate from D1c. D1c's render-to-string means a
PRESERVED such row no longer trips it; D1c does not fix the non-preserved case.

## Acceptance tests (authored before impl; not weakened later)

- **Unset/empty == baseline** and **present-but-matching-nothing == baseline:** both
  yield a byte-identical whole-output digest to the no-config run, on every route.
- **In-range sentinel** (`2099-01-01`, verified in range): perturbed without config,
  preserved with it, on oracle, native-chunked, native full-frame, and (bucket) OOC
  resident / sink-batch / sink-reorder.
- **Out-of-range sentinel** (`9999-12-31`): preserved explicitly (full-range
  fallback), proven by an aggregate preserved-row count on the disposition (not the
  incidental NaT passthrough), no literal logged.
- **Format-variant** (source `01/01/2099`, `date_format=%d/%m/%Y`, literal
  `2099-01-01`): preserved. (rev-2 fixed the impossible `31/12/2099` case.)
- **Typed source** (`date32` holding `9999-12-31`): preserved with homogeneous
  string output (no ArrowTypeError).
- **Boundary:** sentinels within one bucket / one shift of 1677 and 2262 are
  preserved (not guarded/overflowed); non-preserved boundary rows behave as today.
- **Empty input / all-preserved batch / mixed null+preserved+malformed:** correct on
  every route; a malformed value beside a preserved value still records its format
  error while the preserved one does not; preserved rows never enter
  derivation/guard/arithmetic (spy/assert).
- **FK:** single-hop scalar parent `preserve_values` preserves on parent and child
  (normal and orphan/REMAP); child-key `preserve_values` compile-rejected; a
  multi-hop chain, a multi-parent shared child, a COMPOSITE-key endpoint, a
  scalar+composite overlap, and a same-table `parent_date -> child_date` edge each
  compile-rejected with `date_preserve_values_unsupported_fk_shape`; two independent
  safe scalar keys are NOT falsely rejected.
- **date_shift error-position rebasing** after excluding preserved rows and under a
  `when:` selection. **`group_by` + preserve_values** compile-rejected
  (`date_preserve_values_with_group_by_unsupported`); a mixed preserved/ordinary
  batch WITHOUT group_by preserves correctly (the common path).
- **Parser domain:** the `%Y-%W-%w` and `%Z` counterexamples are covered - an
  in-range row still matches via the pandas-authoritative path; a `%Z` or `%f`
  format under preservation is a config error, not a silent mismatch.
- **Config errors:** malformed literal; `preserve_values` without `date_format`; an
  unsupported format (`%f`, `%Z`, `ISO8601`, `mixed`) under preservation; and
  `group_by` + preserve_values. All raise at config time (zero-row too), identically
  native+oracle, no sink commit.
- **Counter aggregation** across batches, `when:`, and REMAP; the counter never
  credits compiled execution when no kernel ran, carries no values, and is
  absent/zero-compatible when unset.
- **Cross-route digest parity** with config set (oracle vs native-chunked vs native
  full-frame vs OOC), strict D0 contract + witness.
- **Log-hygiene sentry:** no literal/date/cell text in logs or reason codes.

## Design notes

New structure: one shared `PreserveSet` (literal + format-domain validation, the
parse-once match, the render) consumed by oracle/native/OOC; one new
`_checks_date_shift.py` function + the FK-shape check; one extension to the
bucket plan-compile check and preflight; one provider_config key. No new module,
route, or abstraction. House style: typed `PreserveSet`; single-purpose functions;
no flag arguments; comments explain why (format-domain restriction makes parsers
agree; render-to-string avoids the mixed-column trap; single-hop-only refuses the
chain tail fail-closed). Resolve literals once per run; `PreserveSet.mask()` is ONE
shared matching pass per batch (pandas parse, then a single strptime fallback only on
pandas-NaT cells), not a parse-per-literal. Applies to touched code only.

## Docs to update

`docs/strategies.md` (date_shift ~130, bucket_perturb ~143; make the `9999-12-31`
note at ~213 the explicit guarantee; note the `date_format`-required-under-
preservation contract), `docs/compatibility-contract.md` (entries 287-304; the FK
single-hop v1 scope; additive-param rule 461 governs). Generated references
regenerate.
