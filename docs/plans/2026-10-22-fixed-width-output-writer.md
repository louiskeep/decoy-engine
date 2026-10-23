# Fixed-width OUTPUT: writer that inverts the reader

Status: BUILT 2026-10-22 (branch `claude/fixed-width-output`): writer +
`FixedWidthWriteError` + `FileTarget` schema + public export; 33 tests
(incl. a 250-example Hypothesis round-trip), 100% line+branch coverage on the
new module, ruff + mypy clean. Dev loop, no cross-model review (owner
instruction). CLI/platform wiring remain separate follow-ups; roadmap +
shipped-log reconciliation lands at merge.
Date: 2026-10-22
Risk: R2 (new module + new config variant touching data semantics / output fidelity)

Rules consulted: 00-universal.md, development-loop.md, feature-dev.md,
architecture.md, testing.md, reuse-first.md, security.md,
observability-and-resilience.md, data-and-migrations.md (output format is a
persisted-contract concern), api-and-compatibility.md (pre-GA = hard add, no
shim).

## FRAME

### Problem

Fixed-width is the one remaining any-to-any file-format direction that is
asymmetric: the engine can READ a fixed-width file (`read_fixed_width` +
`FixedWidthLayout`, `FileSource.format == "fixed_width"`), but the output side
rejects it: `FileTarget.format` is `Literal["csv", "parquet"]`, so a
fixed-width output target fails config validation and there is no writer
primitive. A masked table read from a fixed-width file cannot be written back
in the same shape.

### Definition of done

1. A public `write_fixed_width(frame, layout, path)` engine primitive that is
   the exact inverse of `read_fixed_width` over a defined supported domain:
   `read_fixed_width(path, layout)` equals the input frame, cell for cell, for
   every frame the writer accepts.
2. `FileTarget` accepts `format == "fixed_width"` with a required `layout`,
   mirroring `FileSource` exactly (layout required iff fixed_width, forbidden
   otherwise).
3. Fail-closed over the supported domain: any value that would not round-trip
   exactly (overflow, pad-ambiguous string, embedded newline, type mismatch,
   null/NaN) raises `FixedWidthWriteError` before any output is published, with
   a PII-safe message (position + column name + value length only; never the
   cell value; no `__cause__`/`__context__` chain carrying it).
4. Failure-safe publication: output is written to a temp file and atomically
   moved into place only on full success; a fail-closed error mid-write leaves
   the canonical path untouched (never a half-written file).

### Boundaries / non-goals (pinned guarantee)

This slice ships the ENGINE primitive + the config vocabulary only. Explicit
non-goals, each its own later slice in its own repo:

- CLI wiring (`decoy` `_write_mask_outputs` gaining a `fixed_width` branch).
- Platform wiring (`decoy-platform` `v2_cloud_materialize` / `binding_resolve`;
  platform is release-held and currently rejects fixed-width output by design).
- Cloud fixed-width output (`S3Target` / `GCSTarget` stay `csv|parquet`, so a
  cloud fixed-width target still fails validation, fail-closed, symmetric with
  cloud fixed-width INPUT, which is also unsupported today).
- Wider type set, multi-line records, COBOL copybook, implied-decimal / sign /
  overpunch / packed-decimal fields (the file-format-contract writeup slices 6
  and 8 own those; they need the owner-decision matrix this slice deliberately
  avoids by supporting only what the reader already supports).
- `run_pipeline` does not write targets today (it returns a result the caller
  materializes); this slice does not change that. The writer is the primitive
  callers use.

A review finding outside this guarantee is a recorded follow-up, not a blocker.

## Established solutions

- There is no `pandas.to_fwf` / stdlib fixed-width writer. The established
  convention for emitting a fixed-width field is `str.ljust` / `str.rjust` with
  an explicit fill character, the exact inverse of the `(start, width)` colspec
  slicing `pandas.read_fwf` and our `read_fixed_width` use. The writer follows
  that convention.
- Inversion is made literal and future-proof by REUSING the reader's own
  per-column decode (`profile._fixed_width_reader._strip_pad` and `_cast_value`)
  to VERIFY each produced field reconstructs its source value, rather than
  re-deriving padding/strip rules independently. "The writer is correct iff the
  reader reads back what it wrote", a single source of truth for the
  round-trip contract (reuse-first; one place per fact). If the reader's decode
  changes, the writer's verification tracks it.
- Failure-safe publication follows the same atomic-move pattern already
  documented for targets (`config/_targets.py` Q12 carry: temp key → verify →
  move) and used by the engine's transactional sinks: write temp in the target
  directory, `fsync`, `os.replace` (atomic within one filesystem).
- PII-safe error discipline follows the reader's existing contract exactly
  (`errors.FixedWidthParseError`, `docs/security/error-reporting-and-data-exposure.md`):
  the raise happens from a frame holding no cell value, and the caught caster
  exception (whose text embeds the value) is never chained.

## Design notes

- **Where each fact lives.** The supported domain, padding rules, strip rules
  and type set are the reader's (`_fixed_width.py` + `_fixed_width_reader.py`).
  The writer does not restate them; it reuses `_strip_pad` + `_cast_value` and
  verifies. The record geometry (`record_width`, `[start, start+width)`, gaps)
  is `FixedWidthLayout`'s, reused via its existing `.record_width` and column
  fields.
- **Inversion algorithm (per cell).** render the value to a canonical field
  string for its declared type → check it fits `width` (else overflow →
  fail closed) → pad to `width` on the side implied by `align` with `pad` →
  run the reader's `_strip_pad` + `_cast_value` on the padded field and compare
  the result to the original value (type-aware equality) → mismatch → fail
  closed. This catches every pad/align/type hazard uniformly (e.g. a string
  ending in the pad char; an int left-aligned with pad `"0"` whose trailing
  digits would be stripped; a NaN; a null) without enumerating them.
- **Record assembly.** build a `record_width`-length buffer of the gap-fill
  character (space, 0x20; gap bytes lie outside every column slice so the
  reader never reads them, making any fill round-trip-safe), place each column's
  padded field at `[start, start+width)`, join, append `"\n"` (LF). Columns are
  non-overlapping ascending by `FixedWidthLayout`'s own validator, so placement
  never conflicts.
- **Frame contract.** the frame must contain every layout column by name;
  columns are written in layout order. An extra frame column not in the layout
  is a fail-closed error (never silently dropped, a data-loss concern).
  Zero-row frame → empty file (round-trips to a zero-row frame).
- House style: typed `FixedWidthWriteError`, single-purpose helpers, no flag
  arguments, comments explain why (the PII and round-trip invariants).

## PLAN (acceptance tests + failure modes)

New module `src/decoy_engine/profile/_fixed_width_writer.py` (est. ~180 LOC),
public `write_fixed_width` exported from `decoy_engine` and `decoy_engine.profile`
alongside `read_fixed_width`. New `FixedWidthWriteError(DecoyError)` in
`errors.py`. `FileTarget` extended + validator mirroring `FileSource`.

Observable behavior / acceptance:

- **A1 round-trip (property, Hypothesis).** For layouts and frames drawn from
  the supported domain, `read_fixed_width(write_fixed_width(df, layout, p), layout)`
  equals `df` (dtype + value). The strategy generates str/int/float columns,
  varied width/pad/align, gaps, multiple rows.
- **A2 overflow fail-closed.** A value whose rendered form is wider than its
  column `width` raises `FixedWidthWriteError`; message carries column name +
  value length, not the value; canonical path not created.
- **A3 pad-ambiguous string fail-closed.** align=left, pad=" ", value "ab "
  (trailing pad) raises (would read back as "ab"); symmetric for align=right.
- **A4 int left-align pad "0" fail-closed.** value 50, width 4, align=left,
  pad "0" raises (would read back as 5 after rstrip "0").
- **A5 null / NaN fail-closed.** None in a str column, NaN in a float column,
  None in an int column each raise.
- **A6 embedded newline fail-closed.** a str value containing "\n" or "\r".
- **A7 type mismatch fail-closed.** a non-str in a str column, a bool in an int
  column, a float in an int column, an int in a float column.
- **A8 extra / missing column fail-closed.** frame missing a layout column, or
  carrying a column the layout does not name.
- **A9 atomic publication.** a frame whose Nth row fails leaves no canonical
  file (and no leftover temp file in the directory); a pre-existing file at the
  path is left unchanged on failure.
- **A10 zero rows.** empty frame → empty file → reads back to a zero-row frame
  with the layout columns.
- **A11 gaps + zero-padded numerics + right-align + CRLF-agnostic.** explicit
  cases: gap bytes between columns; pad="0" align=right int incl. 0 and
  negative; verify the file uses LF and reads back identically.
- **A12 error message PII-safety.** assert no acceptance-case cell value ever
  appears in a raised message, and the exception has no `__cause__`/`__context__`.
- **A13 schema.** `FileTarget(format="fixed_width")` requires `layout`;
  `layout` without `fixed_width` is rejected; `S3Target`/`GCSTarget` still
  reject `fixed_width`.

Failure modes handled: overflow, pad ambiguity, null/NaN, newline injection,
type mismatch, schema drift (extra/missing columns), partial write on mid-frame
failure, non-UTF-8-encodable char (fail closed on encode).

Verification: ruff (`check` + `format --check`) and mypy on changed scope, the
new test module, and the existing fixed-width + config target suites; TQ
test-strength (coverage + a mutation spot-check) on the new writer unit.

Note: built under the dev loop with NO cross-model review per owner instruction
(2026-10-22); REVIEW is a self-review against this plan.
