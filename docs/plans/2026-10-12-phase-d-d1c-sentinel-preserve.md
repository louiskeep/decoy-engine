# Phase D / D1c: date sentinel-preserve

Status: plan (rev 2, pre-gate). rev 1 was NO-GO at the Codex plan gate (6 HIGH, 1
MEDIUM); this rev folds all seven at root, grounded on verified code behavior and
runtime probes (pandas 2.3.3 / pyarrow 26.0.0 in the abi-3 venv).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine: CLAUDE.md (established methodology, module
sizing, log hygiene), compatibility-contract.md (additive-parameter rule 461).

Risk: R2. Additive config that changes masking output for opted-in values and
interacts with an FK/RI invariant (date_shift can be an FK key). Plan on disk +
independent plan review + Codex plan gate before build; observable behavior,
failure modes, and acceptance tests defined here first.

## FRAME

**Problem.** `bucket_perturb` and `date_shift` perturb every parseable in-range
date. Datasets use fixed sentinel dates as markers ("never expires" =
`9999-12-31`, "unknown" = `2099-01-01`), and downstream logic tests
`date = <sentinel>`. Today an out-of-pandas-range sentinel (`9999-12-31`) survives
only incidentally (it coerces to NaT, so the unparseable passthrough keeps it),
while an in-range sentinel (`2099-01-01`) is silently perturbed and the
`= sentinel` check breaks after masking.

**Definition of done.** A user lists exact date literals per field; a row whose
date equals a listed literal is emitted unchanged by `bucket_perturb` and
`date_shift` on every route the operator runs on. Unset config means byte-identical
output to today. Preservation is explicit, deterministic, and FK/RI-safe.

**Boundaries.** Date operators only. Exact match, date granularity (Cam
2026-10-11): no ranges, no before/after, no wildcards. Not detection; the user
supplies the literals. Primary supported source type is string-typed date columns
(the common case; the only type bucket_perturb's native route admits). Typed date
sources (numpy `datetime64`, Arrow `date32`/timestamp) are supported with the
output rule in the match/output section; the byte-identical promise applies to
string sources (typed sources have no original string to be identical to).

**Reuse check.** Both operators already restore the source for null and
unparseable rows. D1c is a new member of that passthrough set. No new Rust, no ABI
change: the carve-out is Python-only and applied as a PRE-compute input exclusion
on each route (not a post-compute restore), so preserved rows take the existing
identity path and never reach the kernel's derivation, range guard, or date
arithmetic. Mirrors D1a/D1b-i (leave the shared operator and the oracle transform
untouched; decide and carve out in the route wrapper).

## Pinned guarantee and non-goals

**Guarantee.** When `preserve_values` is set on a `bucket_perturb` or `date_shift`
field, every source row whose date matches a listed literal (match rule below) is
emitted unchanged (string source: byte-identical to the source string; typed
source: the source date rendered to the operator's string output), on all routes
the operator runs on; all other rows mask exactly as today. When unset/empty,
output is byte-identical to today on all routes. For an FK key, the preserved value
flows to the child through the existing parent-map/REMAP mechanism, so referential
integrity is retained by the same construction that gives it for masked keys.

**Non-goals.** No range/pattern matching. No detection or auto-sentinel defaults.
No change to any other operator. No change to which routes an operator runs on
(date_shift stays off the out-of-core route). No new fix for the pre-existing
typed-out-of-range ArrowTypeError (see "Known pre-existing bug"); D1c neither
causes nor is required to fix it, and its preserved rows render to string so they
do not trip it. 100M/200M proofs stay in D4c.

## Match rule and output (the design-critical contract)

**Config.** `provider_config["preserve_values"]`: a list of date literals. Each
literal is validated as an ISO-8601 calendar date via `datetime.date.fromisoformat`
(covers years 0001-9999, so out-of-nanosecond-range sentinels are valid; rejects
`not-a-date`, `NaT`, empty/whitespace, `2020-02-30`). The list is de-duplicated;
empty == unset (no behavior change, byte-identical output). Free-form
`provider_config`, no `ColumnConfig` schema change.

**One match function, full calendar range, no pandas NaT involved.** A source row
is preserved iff its source date equals a configured literal, compared at
date granularity:
- **String source:** parse the source cell with the operator's `date_format` using
  Python `datetime.datetime.strptime` (full range, so `9999-12-31` parses where
  `pd.to_datetime` would give NaT); a cell that fails strptime is not preserved
  (and keeps masking/passthrough exactly as today). Compare the parsed `date()` to
  the literal. Because D1c needs a deterministic format, `preserve_values` on a
  string source REQUIRES an explicit `date_format` (config error if absent); this
  is already true for bucket_perturb and is a new, fail-closed requirement for
  date_shift when the feature is used.
- **Typed date source** (`datetime64`, Arrow `date32`/timestamp): compare the
  source value's calendar date directly to the literal (no reparse). For a
  timezone-aware source, compare on the local wall-clock date (documented).

This single rule replaces rev 1's raw-string-OR-parsed-date union. It is
format-independent in the sense that matters (the literal is always ISO; the source
is parsed by its own `date_format`), handles in-range and out-of-range uniformly,
and never depends on NaT equality. A match outcome never diverges from the
operator's own treatment: an out-of-range date the operator would pass through as
unparseable is instead passed through as an explicit preserve (same output); an
in-range match is preserved instead of perturbed.

**Output for a preserved row (the output column is always a strftime string).**
- String source: emit the original source string unchanged (byte-identical).
- Typed source: emit the source date formatted with the operator's `date_format`
  via Python `strftime` (full range: `date(9999,12,31).strftime(...)` works), so
  the output column stays a homogeneous string column. Never restore a raw
  `datetime.date`/`Timestamp` object into the string column (that is the HIGH-4 /
  ArrowTypeError trap).

## How the carve-out attaches per route (Python-only, pre-compute)

The preserve positions are computed from the ORIGINAL input (after the operator's
own format detection runs against the full original column, so excluding rows does
not change the detected format), then those positions are removed from the input
handed to the compute and their rendered output scattered back:

1. **Oracle handlers.** Union the preserve mask into the existing
   `unusable`/skip set BEFORE derivation and BEFORE the format-error accounting,
   in `_strategies/_date_shift.py` (~157, 216-227) and `transforms/bucket_perturb.py`
   (166-178). A preserved row skips derivation and is written as its rendered
   string; it is excluded from the format-error channel (same as a null).
2. **Native-chunked / native full-frame.** In the Python wrapper around the kernel
   call (`native/_operator_step.py` ~418/443, with reconciliation in
   `native/_chunk_masking.py` 244-268), filter preserved rows OUT of the array
   passed to the kernel, run the kernel on the remainder, and scatter the rendered
   strings back at the preserved positions; subtract them from the format-error
   set. The kernel returns `pa.string()`, so the scattered strings keep the column
   homogeneous. Kernel and ABI untouched. This covers BOTH the shared full-frame
   operator path and the chunked path (not only `_chunk_masking`).
3. **Out-of-core (bucket_perturb only; date_shift is not on OOC, confirmed
   `out_of_core/_compat.py:70,288`).** In `out_of_core/_bucket_native.py`
   `native_bucket_perturb_array` and oracle `_mask_group_c._bucket_perturb_array`,
   compute the preserve mask first and apply the range guard
   (`out_of_core_bucket_perturb_date_out_of_native_range`) ONLY to non-preserved
   rows, so a preserved sentinel near the 1677/2262 bound does not abort the run
   (rev-1 HIGH-1). Then filter/scatter as in (2).

Boundary sentinels therefore never reach the kernel, so the native date_shift
`OverflowError` at the 2262 boundary (`_date_shift_ext.py:184`) and the OOC range
guard both apply only to non-preserved rows, exactly as today.

## FK / RI contract (route-by-route; rev-1 premise was wrong)

Verified: the chunked FK self-mask gate admits ONLY `hash`
(`_chunked_fk.py:444`), so a date_shift (or bucket_perturb) FK key never
self-masks on the chunked route; such a table runs full-frame/sequential. There,
the PARENT key column owns the transform: a normal child row gets the parent's
masked output via the source->masked parent map, and an orphan child is remapped by
re-invoking the PARENT's handler with the parent's config (`_strategies/_orphan.py`
12-16, 118-147; REMAP calls the parent handler at ~144). The child never runs its
own date config.

**Design:** preservation on an FK key is PARENT-OWNED. `preserve_values` is honored
on the parent key column; it then flows to every child (normal rows inherit the
preserved parent output; orphan rows re-run the parent handler, which preserves
identically) so RI holds by the same construction that gives it for masked keys. A
`preserve_values` set on a child FK key that is remapped from a parent is rejected
at plan-compile (`date_preserve_values_on_remapped_child_fk_unsupported`, naming
the parent column to configure instead) to avoid a silent no-op. No symmetry check
(rev-1's idea): symmetry is neither necessary nor the RI mechanism; inheritance is.
REMAP does not create a missing parent, so D1c changes nothing about pre-existing
orphans; the guarantee is retained RI, not orphan repair. bucket_perturb as an FK
key is rejected on the chunked route (`_chunked_bucket_perturb.py` ~265, a
chunked-route gate, not a global ban) and otherwise parent-owned on
full-frame/sequential, covered by the same rule.

## Config validation (active paths; rev-1 named the legacy validator)

Verified: date_shift's `transforms/date_shift.py::validate_rule` is legacy and
never called by the active `DateShiftStrategyHandler.run`; date_shift has no
`preflight`, so a zero-row `when:` gate skips all config checks. bucket_perturb
validates at two config-time slots that fire independent of rows: the plan-compile
check `plan/_checks_bucket_perturb.py::check_bucket_perturb_config` and the handler
`preflight -> validate_bucket_perturb_config` (invoked even under a zero-row
`when:` gate, `_when_gate.py:240-242`).

**Design:** validate `preserve_values` at CONFIG time on both operators so a
zero-row/zero-match job still rejects bad config, identically on oracle and native,
with no sink commit:
- bucket_perturb: extend `validate_bucket_perturb_config` (hit via the existing
  `preflight` and the plan-compile check). Error `bucket_perturb_invalid_config`.
- date_shift: add a plan-compile check in `plan/_checks_date_shift.py`
  (`check_date_preserve_values`, raising `PlanCompileError`), since there is no
  active handler-level config validator; this fires regardless of selected rows.
  Add the string-source `date_format` requirement check here too.
- The native param resolution (`native/_operator_params.py`) reads
  `preserve_values` but does not own validation; the admission resolver
  (`native/_operator_config_rejections.py`) is updated only to accept the key
  (admit, not decline), so native does not reroute eligible columns to oracle.
Preserve the OOC compatibility -> config-preflight -> seam ordering
(`out_of_core/_runner.py:183`), the D1b-i lesson.

## Known pre-existing bug (separate finding, not D1c's to fix)

Independently of D1c, an Arrow/extension date source (`date32`/`datetime64[s]`)
holding an out-of-nanosecond-range date (year > 2262) already crashes both date
operators today: the existing passthrough restores a `datetime.date` object into
the strftime-string output column, and `pa.Table.from_pandas` at
`_pandas_adapter.py:342` raises `ArrowTypeError`. Record as a separate engine bug.
D1c's output rule (render preserved values to string) means a preserved such row no
longer trips it, but D1c does not undertake to fix the non-preserved case.

## Acceptance tests (authored before impl; no later contributor weakens these)

- **Unset/empty == baseline:** every existing bucket_perturb/date_shift parity test
  passes; a dedicated test asserts unset AND present-but-matching-nothing
  `preserve_values` both yield a byte-identical whole-output digest to the
  no-config run, on every route.
- **In-range sentinel** (`2099-01-01`, verified in-range): perturbed without
  config, preserved with it, on oracle, native-chunked, native full-frame, and
  (bucket only) OOC resident / sink-batch / sink-reorder.
- **Out-of-range sentinel** (`9999-12-31`): preserved explicitly via the strptime
  full-range match, and an aggregate preserved-row count on the disposition proves
  the preserve branch fired (distinguishing explicit preservation from the
  incidental NaT passthrough), without logging the literal.
- **Format-variant** (source `31/12/2099`, `date_format=%d/%m/%Y`, literal
  `2099-01-01`): preserved; and a typed `date32` source holding `9999-12-31`
  preserved with homogeneous string output (no ArrowTypeError).
- **Non-match unaffected** (`2099-01-02`): still masks.
- **Selective error suppression:** a preserved invalid/out-of-range value beside a
  genuinely malformed value proves the malformed one still records its format error
  while the preserved one does not.
- **FK:** a date_shift FK chain with `preserve_values` on the PARENT preserves the
  value on parent and child (normal and orphan/REMAP rows); `preserve_values` on a
  remapped child FK key is compile-rejected with the dedicated code; a
  bucket_perturb FK key on full-frame with parent preservation behaves the same.
- **date_shift error-position rebasing** after filtering preserved rows and under a
  `when:` selection: format-error row indices still point at the right rows.
- **Config errors:** malformed `preserve_values`, and a string source with
  `preserve_values` but no `date_format`, raise at config time (zero-row too),
  identically native and oracle, with no sink commit.
- **Cross-route digest parity:** with config set, oracle vs native-chunked vs
  native full-frame vs OOC byte-identical on a mixed sentinel/non-sentinel column
  (strict D0 parity contract + witness).
- **Log-hygiene sentry:** no literal/date/cell text in any log line or reason code.

## Design notes

New structure: one shared `PreserveSet` resolver (literal validation + the
per-source-type match + the rendered-output; one place per fact, consumed by
oracle, native, and OOC), one new plan-check function in `_checks_date_shift.py`
(mirrors `check_date_shift_group_by_refs`), one extension to
`validate_bucket_perturb_config`, and one new provider_config key. No new module,
route, or abstraction. House style: typed `PreserveSet` over passing raw lists;
single-purpose functions; no flag arguments; comments explain why (full-range
Python parsing avoids the pandas NaT trap; render-to-string avoids the mixed-column
ArrowType trap). Applies to touched code only.

## Docs to update

`docs/strategies.md` (date_shift ~130, bucket_perturb ~143 passthrough notes;
make the `9999-12-31` note at ~213 the explicit guarantee), and
`docs/compatibility-contract.md` (bucket_perturb/date_shift entries 287-304; the
additive-param rule 461 already governs). Generated references regenerate.
