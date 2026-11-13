Status: plan

# DUP-DEFECTS: fix the confirmed duplicated-logic defects that meet the V1 bar (roadmap row 24zd)

Rules consulted: `/home/cam/dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md`, `feature-dev.md`, `bugfix.md`, `architecture.md`, `reuse-first.md`,
`api-and-compatibility.md`, `security.md`, `testing.md`, `exploration.md`,
`workspace-and-vcs.md`, `scope-discipline.md`, `verification.md`, `code-review.md`,
`cross-cutting-concerns.md`, `documentation.md`, `profiles/local-operations.md`,
`catalogs/recurring-defects.md`; engine `CLAUDE.md`, `CODEMAP.md`, `.importlinter`,
`docs/compatibility-contract.md` (3.3); CLI `docs/ROADMAP.md` (rows 24b, 24zd, 24ze, 24zf),
CLI `CODEMAP.md`.

Revision 2, Dennis round 1 remediated; not yet re-reviewed.

Bases (2026-11-13): engine `origin/main` `8c9c1162`, CLI `origin/main` `ee65431` (pins
`decoy-engine==0.23.0`). Inputs: the duplication census `REMEDIATION.md` (CLI
`docs/records/2026-11-12-duplication-census/`, merged in CLI #211) and the reachability pass
`REACHABILITY-A-B.md` (CLI branch `docs/dup-reachability-ab` `9efdc1ab`, not merged; engine main
`c96e2855`, CLI main `e642d69b`). Line numbers below are re-read at the bases above and some
differ from both records.

Owner decisions used: roadmap row 24zd (2026-11-12: confirmed items that meet the V1 bar are
fixed in this row, the rest go to the owning feature group's QA remediation; A1 is built in 24zf;
A9 is built in GEN-PROVIDERS, row 24p); row 24b QA-GATE (2026-11-08: "`decoy validate` refuses
what `run` would refuse ... fail at the gate with exit 1, the config-error code, never exit 3 at
run"); the V1 bar (2026-11-02: only a product-function break, data loss or corruption, or real
values or secrets shown to a non-admin block V1); "names may show in messages; data values never"
(2026-11-06).

Risk: R2 (an output-byte change in a masking and generation transform, and a widened admission
on the chunked route). Out of scope: A1 (24zf), A9 (24p), every LATENT and IDENTICAL item, every
CLI item (section 4.5 routes each one), the shared-helper migrations ED-1 and ED-2 beyond the
sites named here (V1+ tier of REMEDIATION.md), the DUP-GUARDS sentries (row 24ze).

## 1. Frame

### 1.1 What exists

The census found 28 places where two copies of the same logic disagree (17 engine, package A;
11 CLI, package B). The reachability pass marked 11 REACHABLE-DEFECT, 16 LATENT and 1 IDENTICAL.
Of the 11 reachable items the owner already placed A1 (24zf) and A9 (24p, which the pass found
LATENT anyway). This plan tiers the remaining ten against the V1 bar.

| Item | Mark | V1 bar | Where it is fixed |
|---|---|---|---|
| A5 snapshot `UnicodeDecodeError` | REACHABLE | V1 under the 24b rule: `run` exits 3 with raw codec text that `validate` does not refuse | this row, slice E1 |
| A6 `fromisoformat` `Z` on Python 3.10 | REACHABLE | not V1: a hand-made profile only, refused with exit 1, no wrong output | V2-LOG (section 13) |
| A10 `NaT` and `pd.NA` in derived and formula | REACHABLE | V1: wrong output bytes (data corruption) | this row, slice E2 |
| A15 chunked `when` guard refuses `large_string` | REACHABLE | V1: a valid config on a parquet source cannot run with `--chunked` (function break, fails closed) | this row, slice E3 |
| B5 non-UTF-8 config file | REACHABLE | V1 under the 24b rule | 24zf C5 (one config loader); owner question OQ-2 |
| B6 error line under `--quiet` | REACHABLE | not V1: message only | 24zf C1 |
| B7 Rich markup drops `[tag]` text | REACHABLE | not V1: message only, `--json` unaffected | 24zf C1 |
| B8 secret-ref prefix case | REACHABLE | not V1: a usage refusal for a spelling the docs do not use; the gate gap is a 24b item | 24zf C5 (the loader carries the secret-ref guard); gate side to 24b |
| B9 `CI=false`, bad SMTP port | REACHABLE | not V1: advisory notice; a notification failure leaves the run exit unchanged and says so | 24zf C7 |
| B11 no scheme check on notify and `--url` | REACHABLE | not V1: nothing sent, release-key checks still hold | V2-LOG (section 13) |

### 1.2 Problem

Three engine defects meet the bar:

- **A10.** `transforms/derived.py:208` `_is_null` counts only `None` and float NaN. A parquet
  timestamp column with a null reaches a mask-mode `derived` expression as `pd.NaT` (the mask
  path reads `df[refs].itertuples()`), so `explicit_null` (the default) evaluates the expression
  instead of writing an empty cell. The reachability run wrote `0` where the documented output is
  null (`docs/strategies.md:962-969`). `generators/_formula.py:206` has the same narrow rule. The
  kernel predicate `kernel/_scalar.py:14` `_is_missing` already counts `NaT` and `pd.NA`.
- **A15.** `execution/_chunked_when_guard.py:66` accepts only `string` and `null`. Parquet text
  read through `ParquetFile.iter_batches` keeps a `large_string` file schema, so a categorical or
  non-deterministic faker column under `when:` is refused with
  `chunked_categorical_nondeterministic_when_not_supported` and a per-chunk widening reason that is
  false for `large_string`. Admission (`execution/native/_dispatch.py:503`,
  `execution/native/_when_admission.py:245-251`) already treats the two types as equal.
- **A5.** `plan/_generation.py:297` (`read_and_pin_snapshots`, the live compile path) and
  `generation/statistical/_spec.py:206` (`_load_snapshot`, reached only through
  `resolve_pinned_snapshot` when no read pass ran) catch only `json.JSONDecodeError`. A snapshot
  holding a byte that is not UTF-8 raises `UnicodeDecodeError` from `json.loads(bytes)`.
  `decoy validate` reports "internal error (UnicodeDecodeError)"; `decoy run` prints the codec
  text (which quotes the offending byte) and exits 3. A truncated snapshot already gets
  `STATISTICAL_SNAPSHOT_UNREADABLE` with exit 1 in both commands.

## 2. Guarantee

After this row:

1. A missing cell means the same thing in `derived`, `formula` and the masking kernels: `None`,
   IEEE NaN of any float type, `Decimal('NaN')`, `pd.NaT`, `numpy.datetime64('NaT')` and `pd.NA`.
   The string `"NaT"` and the empty string are values, not missing cells.
2. `derived` with `explicit_null` writes a null for every row where a referenced cell is missing
   by rule 1; `sentinel` and `default` substitute for every such cell.
3. A config that runs on the whole-frame route with a `string` text column also runs with
   `--chunked` when the same column is `large_string`. Engine parity means equal column values
   and the same declared output field type, not byte-equal parquet containers. The CLI parity
   test writes CSV and compares bytes.
4. A statistical snapshot for which `json.loads(raw_bytes)` raises `UnicodeDecodeError` or
   `JSONDecodeError` is refused with `STATISTICAL_SNAPSHOT_UNREADABLE` (exit 1) by
   `decoy validate` and by `decoy run`, before any row is read. The message names the field and
   the path as the user typed it, and carries no byte, line or character of the file.
5. No error added or changed by this row chains an exception whose attributes hold file content
   (`UnicodeDecodeError.object`, `JSONDecodeError.doc`).

## 3. Established solutions

| Problem | Established answer | Source | Used how |
|---|---|---|---|
| Which scalars count as missing | pandas treats `None`, `NaN`, `NaT` and `pd.NA` as missing in `isna`; `pd.NA` makes `bool()` raise and `NaT != NaT` is true | pandas user guide, "Working with missing data" (`isna`, "Experimental NA scalar"); `pandas.isna` API reference | rule 1; the kernel predicate already does this (`value != value`, then `pd.isna`) |
| One predicate, many callers | one owner for a rule; callers import it | REMEDIATION.md ED-2 ("`is_null_cell` in one place"); `dev-rules/reuse-first.md` | the kernel body moves to one public helper; three callers use it |
| `string` vs `large_string` | the same logical UTF-8 type with 32-bit or 64-bit offsets; conversion to pandas gives the same Python `str` objects | Apache Arrow columnar format spec ("Variable-size Binary Layout", "Large" variants); `pyarrow.types.is_large_string` | the guard accepts both, like admission does |
| JSON text encoding | JSON exchanged between systems must be UTF-8 | RFC 8259 section 8.1 | a non-UTF-8 snapshot is malformed, so it gets the existing unreadable code |
| Decode failures from `json.loads(bytes)` | `json.loads` detects UTF-8/16/32 and decodes; a bad byte raises `UnicodeDecodeError`, which subclasses `ValueError`, and `JSONDecodeError` also subclasses `ValueError` | Python `json` docs ("`s` can be ... bytes"); Python built-in exception hierarchy | catch `(UnicodeDecodeError, json.JSONDecodeError)`, the shape `vault.py:552` already uses (`except (ValueError, UnicodeDecodeError)`) |
| Errors that must not echo input | build the message from closed text and raise after the handler so no cause or context is linked | PEP 3134; the engine's own pattern in `transforms/derived.py` (module docstring) and `_spec.py:196-201` | the snapshot helper raises outside the `except` block |

## 4. Design

### 4.1 A10: one missing-cell predicate

- Add `is_null_cell(value: Any) -> bool` to new
  `src/decoy_engine/internal/pandas_missing.py`, matching the canonical ED-2 owner name in
  REMEDIATION.md. Its body is the kernel `_is_missing` body, moved verbatim: `None` is missing;
  else `bool(value != value)`; if that raises, lazily import pandas and return
  `bool(pd.isna(value))`; if that raises, not missing. The lazy pandas import stays, so the Arrow
  hot path does not gain a pandas import. A separate module keeps the missing-cell rule out of
  `pandas_compat.py`, whose existing concern is pandas-version dtype labels.
  The docstring keeps the kernel's explanation of why `value != value` covers float, numpy float
  and `Decimal('NaN')`, and adds `NaT` and `pd.NA`.
- `internal/` is tier 4 in `.importlinter`; `kernel` (tier 2), `transforms` and `generators`
  (tier 3) may all import it. A helper in `kernel` could not be imported by `transforms`.
- `kernel/_scalar.py`: delete the `_is_missing` body and bind
  `from decoy_engine.internal.pandas_missing import is_null_cell as _is_missing`, so
  all five existing importers keep their imports and output: `kernel/_code_set.py:24`,
  `execution/native/_crypto_reference.py:28`, `execution/native/_index_ext.py:35`,
  `execution/out_of_core/_mask_group_b.py:76` and `execution/native/_fpe_route.py:23`. The body
  did not change.
- Deleting the predicate shifts the `hash_array` `seed` pin currently at
  `kernel/_scalar.py:77` in `tests/sentry/secret_material_census.json`. Refresh that entry with
  `scripts/secret_material_census.py --write`, run
  `tests/sentry/test_secret_material_census.py`, and list the moved line-only pin in the build
  report. The key set, verdict and baseline size must not change; no baseline grows.
- `transforms/derived.py`: delete the `_is_null` body and bind `_is_null = is_null_cell` (the
  oracle test at `tests/unit/transforms/test_derived.py:649-660` imports `_is_null`; keeping the
  name avoids moving tests). Drop `import math` if nothing else uses it.
- `generators/_formula.py:206`: `if is_null_cell(val):`.
- Mask mode and generate mode both call `transforms.derived.apply_derived`
  (`execution/_strategies/_derived.py:48`, `generation/_synthesize_derived.py:40`), so one site
  covers both.
- `transforms/windowed_date.py:209` `_is_null_cell` is a third copy that already agrees with
  rule 1. It moves with the ED-2 migration (V1+), not here, so this slice stays small.

### 4.2 A15: one text-type predicate

- New `src/decoy_engine/_arrow_types.py` (the owner REMEDIATION.md names for ED-1) with two
  functions: `is_text(t)` is `pa.types.is_string(t) or pa.types.is_large_string(t)`;
  `is_text_or_null(t)` adds `pa.types.is_null(t)`. Nothing else goes in it in this row.
- `.importlinter` is `exhaustive = true`, so `_arrow_types` joins the bottom layer (the line
  holding `_value_free`, `_reference_date` and the other leaf modules). It imports only `pyarrow`.
- `execution/_chunked_when_guard.py:66`: `if is_text_or_null(typ): continue`. The refusal text
  for other types is unchanged.
- `execution/_chunked_profile.py:248` `_is_string_type` is the same predicate; replace it with
  `is_text` (two call sites, `:258-259`). This removes a private copy instead of adding a caller.
- The other 19 ED-1 sites move in ED-1 after the D5 sentry baseline (24ze); the D5 baseline then
  records `_arrow_types.py` as the owner.
- The new leaf brings `src/decoy_engine/` from 24 to exactly 25 direct modules, excluding
  `__init__.py`. That is the fan-out cap, not a raised cap or baseline. The root remains the
  right home because CODEMAP's Foundation row places private leaves at
  `src/decoy_engine/_<name>.py`, REMEDIATION ED-1 already names this owner for cross-layer Arrow
  type rules, and every ED-1 caller can import the bottom-layer leaf without reversing a layer.
  The next root module must use a named sub-package.
- `string_view` (Arrow 16+) and dictionary-encoded text stay refused by the guard. No reader on
  the chunked route produces them today; they fail closed (section 14).

### 4.3 A5: one snapshot parse helper

- New `src/decoy_engine/generation/statistical/_snapshot_io.py` (the E24 location in
  REMEDIATION.md) with `parse_snapshot_json(raw: bytes, *, shown: str) -> Any`:
  - `json.loads(raw)`; on `json.JSONDecodeError` the message stays today's text
    `snapshot_file {shown!r} could not be read: {exc}` (the decoder message holds only the error
    name, line, column and offset, never document text), so existing message tests hold;
  - on `UnicodeDecodeError` the message is the closed text
    `snapshot_file {shown!r} could not be read: the file is not valid UTF-8 JSON`;
  - both raise `StatisticalSpecError(code=STATISTICAL_SNAPSHOT_UNREADABLE)` after the `except`
    block, with no `__cause__` and no `__context__` (rule 5; `JSONDecodeError.doc` is the whole
    file and `UnicodeDecodeError.object` is the raw bytes).
- Break the `_snapshot_io.py` to `_spec.py` cycle with a function-local import of
  `StatisticalSpecError` inside `parse_snapshot_json`. `_snapshot_io.py` has no top-level import
  of `_spec.py`; when `_spec.py` imports the helper, its error class is therefore allowed to
  finish initialization before the helper is called. This keeps the class defined and exported
  from `_spec.py`, so type identity, private compatibility imports and reason-registry ownership
  do not move.
- Export `parse_snapshot_json` from `decoy_engine.generation.statistical` in `__init__.py`.
  `plan/_generation.py:296-302` keeps the established function-level lazy import, now from the
  public package rather than `_snapshot_io.py`; it calls the helper, catches
  `StatisticalSpecError` and records it in `failures[path]` as today. The file shrinks.
- `generation/statistical/_spec.py:205-211`: call the helper (596 lines today; the call is
  shorter than the block it replaces, so the module stays under 600 and needs no census entry).
  The current `raise ... from exc` there goes with it.
- `plan/_serialize.py:347-350`: replace its `json.loads(raw)` block with a function-level lazy
  import of `parse_snapshot_json` from the public package. Catch `StatisticalSpecError`, leave
  the handler, and then raise its existing `PlanCompileError` with
  `DP_PINNED_SNAPSHOT_PAYLOAD_NOT_JSON`; the embedded path intentionally keeps its own code and
  closed text. The prior sha256 check still runs first. This is the third snapshot-bytes parse
  site, so it no longer carries a separate narrow catch.
- A subprocess acceptance test imports `decoy_engine.generation.statistical` in a cold
  interpreter, calls `parse_snapshot_json` with an invalid byte and catches the exported
  `StatisticalSpecError`. It guards the cycle break, the lazy reverse import and the public
  export.
- E24 (V1+) later extends `_snapshot_io.py` with the read-hash-parse sequence and the `sha256:`
  tag; this row adds only the parse step so E24 has one place to grow.

### 4.4 CLI

No CLI source changes. All three fixes reach the CLI through the engine. Slice C1 adds
end-to-end tests once the CLI pins an engine release that carries E1 to E3, so the CLI half
of the row is proved on the shipped pin (a row closes only when the CLI pins the release).

### 4.5 Routing of the rest

| Item | Destination | Why not here |
|---|---|---|
| A1 | 24zf C4 | owner 2026-11-12 |
| A9 | 24p GEN-PROVIDERS | owner 2026-11-12; LATENT today |
| A6 | V2-LOG | below the V1 bar (1.1) |
| A2, A3, A4, A7, A8, A11, A12, A13, A14, A16, A17 | REMEDIATION.md actions (hard delete for dead copies, V1+ tiers) | LATENT: no user path reaches the difference |
| B1, B2, B4, B10 | REMEDIATION.md actions (C2, CLI io move, F6, owner call for B10) | LATENT |
| B3 | none | IDENTICAL |
| B5 | 24zf C5 | owner answered OQ-2 (a): one loader fixes all seven sites; an interim copy here would be a fourth handler |
| B6, B7 | 24zf C1 | message only |
| B8 | 24zf C5 (prefix rule); 24b (gate has no mask-secret check) | not V1; the gate side is the QA-GATE row's scope |
| B9 | 24zf C7 | not V1 |
| B11 | V2-LOG | hardening only |

## 5. Behaviour

| Input | Today | After |
|---|---|---|
| derived `explicit_null`, a referenced cell is `NaT` | expression runs with `NaT`; `case_when(a < b, 1, 0)` writes `0` | null cell |
| derived `explicit_null`, a referenced cell is `pd.NA` | expression runs with `pd.NA`; usually a row failure "an operand has an unsupported type" | null cell |
| derived `sentinel`/`default`, a referenced cell is `NaT` | no substitution; expression runs with `NaT` | `""`/`0` substituted; a type error on that row fails it with today's value-free text, per OQ-1 answer (a) |
| derived, referenced cell is `None` or float NaN | unchanged | unchanged |
| derived, referenced cell is the string `"NaT"` or `""` | a value | a value (unchanged) |
| formula, referenced cell is `NaT` or `pd.NA` | passed to `safe_eval` | `""` substituted and counted in `null_subs`, like `None` today |
| masking kernels (`_is_missing` callers) | rule 1 | rule 1, byte-identical |
| `run --chunked`, `when:` on categorical or non-deterministic faker, text column `large_string` | refused, exit 1, false widening reason | runs; values and output field type equal the `string` copy and the whole-frame route; CSV CLI outputs are byte-equal |
| same, a referenced column is `int64` or `float64` | refused (real widening risk) | refused, same code and text |
| snapshot not valid UTF-8, `decoy validate` | "internal error (UnicodeDecodeError)", exit 1 | `STATISTICAL_SNAPSHOT_UNREADABLE`, exit 1 |
| same, `decoy run` | raw codec text, exit 3 | `STATISTICAL_SNAPSHOT_UNREADABLE`, exit 1, before any row is read |
| snapshot truncated JSON | `STATISTICAL_SNAPSHOT_UNREADABLE`, exit 1 | unchanged, same text |

## 6. Failure modes

### 6.1 Class table

| # | Class | Structure that closes it | Proof | Mutation that turns it red |
|---|---|---|---|---|
| 1 | Value-bearing output | the snapshot decode refusal is closed text naming the field and the typed path; it is raised after the handler, so no `__cause__` or `__context__` carries `UnicodeDecodeError.object` or `JSONDecodeError.doc`; the derived per-row failure keeps `evaluation_failure_text` | AT-E1-3, AT-E1-4, AT-C1-1 | put `{exc}` in the decode branch; raise inside the `except` block |
| 2 | Cleanup missing on an exit path | N/A for new state: the row adds no file, lock or scratch. The A5 refusal fires in the compile pass before any output is opened, so `run` leaves nothing behind | AT-C1-1 (no output file) | move the parse after the first table write |
| 3 | State and concurrency | N/A: every helper is a pure function of its argument; no shared or module state is added | AT-E2-7, AT-E3-4 (truth tables) | cache the predicate result in a module dict |
| 4 | Route parity and silent fallback | derived mask, derived generate and `--chunked` all call `apply_derived`, so one predicate covers every route; the guard admits `large_string` exactly as `_dispatch.py:503` and `_when_admission.py:245-251` do; engine parquet runs must have equal values and declared output field type, while CLI CSV routes must be byte-equal | AT-E2-3, AT-E3-2, AT-C1-3 | keep the old narrow check in `_formula.py`; compare row counts instead of values and type |
| 5 | Referential integrity | N/A with a reason: no key, FK or vault path changes. The kernel predicate that `_crypto_reference.py` uses for keyed output moves verbatim and is parity-tested | AT-E2-7 | change `value != value` order in the moved body |
| 6 | Untrusted producer | a snapshot file is user-editable input; every decode or parse failure maps to one code before any field is read; the top-level-object and schema checks after it are unchanged | AT-E1-1, AT-E1-2, AT-E1-6 | catch only `JSONDecodeError` again |
| 7 | Check then use (TOCTOU) | N/A for new reads: the snapshot is still read once, hashed and pinned by `read_and_pin_snapshots` (`plan/_generation.py:239`); the helper parses the pinned bytes, never re-reads | AT-E1-2 | re-read the path inside the helper |
| 8 | Weak tests and overclaimed evidence | every AT is red on `8c9c1162` except the named controls; AT-E3-2 compares values and output field type, AT-C1-3 compares CSV bytes, each AT names its mutation in this table, and mutations rerun on the final head | gate reviewers rerun them | relax AT-E3-2 to "exit 0" |
| 9 | Behaviour-preserving refactor drift | the kernel body and `_chunked_profile._is_string_type` are moved or swapped without behaviour change; the old names stay bound for existing callers and tests | AT-E2-7, `tests/unit/kernel/test_scalar_is_missing_na.py`, chunked profile tests | drop `is_large_string` from `is_text` |
| 10 | Frozen contract drift | no reason code, exit map, `--json` shape or seed version changes; derived output changes only for rows with a `Decimal('NaN')`, numpy NaN scalar, `pd.NaT`, `numpy.datetime64('NaT')` or `pd.NA` reference, recorded in compatibility contract 3.3 and a `Breaking:` fragment; golden diffs limited by stop line 15.6 | AT-E2-1 to AT-E2-6, golden suites | change `explicit_null` for a `None` reference |

### 6.2 Enumerations

#### Exit paths (A5)

| Command | Where it fails | Exit |
|---|---|---|
| `decoy validate` (all forms, `--json`) | gate compile pass, `read_and_pin_snapshots` (`plan/_generation.py:297`) | 1 |
| `decoy run` | the same compile pass from `_prepare`, before any row is read | 1 |
| `decoy gen` scaffold gate | the same compile pass | 1 |
| library `resolve_pinned_snapshot` with no read pass | `_load_snapshot` (`generation/statistical/_spec.py:206`) | raises `StatisticalSpecError` |

#### Routes (A10, A15)

| Route | A10 reaches it | A15 reaches it |
|---|---|---|
| whole-frame pandas, mask | yes, `execution/_strategies/_derived.py:48` | no guard |
| generate | yes, `generation/_synthesize_derived.py:40` and `generators/_formula.py:206` | no guard |
| `--chunked` | yes, same handler per chunk | yes, guard installed at `execution/_chunked_oracle.py:208-222` |
| out-of-core, native | builder confirms no second derived evaluator (stop line 15.2) | admission already equal at `execution/native/_dispatch.py:503` |

#### FK shapes

N/A: no key, FK or relationship code path changes. Derived columns that reference an FK column
read the masked key through the same row context as today; only the missing-cell rule changes.

#### Fields

| Field | Change |
|---|---|
| `snapshot_file` (statistical generate column) | bytes that make `json.loads` raise `UnicodeDecodeError` or `JSONDecodeError` get `STATISTICAL_SNAPSHOT_UNREADABLE` |
| `null_propagation` (derived) | its three modes apply rule 1 (`transforms/derived.py:228-237`) |
| `formula` references | rule 1 for the `""` substitution (`generators/_formula.py:206`) |
| `when` (categorical, non-deterministic faker, `--chunked`) | `large_string` targets and references admitted (`execution/_chunked_when_guard.py:66`) |

Missing-cell values (rule 1): `None`, `float('nan')`, `numpy.float32('nan')`,
`numpy.float64('nan')`, `Decimal('NaN')`, `pd.NaT`, `numpy.datetime64('NaT')`, `pd.NA`.
Non-missing controls: `"NaT"`, `""`, `0`, `False`, `pd.Timestamp("2020-01-01")`.

#### Output surfaces

- Masked and generated table bytes: change for rows with a `Decimal('NaN')`, numpy NaN scalar,
  `pd.NaT`, `numpy.datetime64('NaT')` or `pd.NA` reference (A10).
- Run refusals on stderr and the `--json` error envelope: A5 gets the existing code and closed
  text; A15 refusal disappears for `large_string`.
- Evidence, reports, manifests and the run result: unchanged.

## 7. Acceptance tests, red first

Each test is written and seen to fail on `8c9c1162` before the fix, except the parity and
unchanged-behaviour controls, which pass before and after. Synthetic values only.

### 7.1 Engine

| Id | Test | Place |
|---|---|---|
| AT-E1-1 | `parse_snapshot_json(b'{"a": "\xff"}', shown="s.json")` raises `StatisticalSpecError` with `STATISTICAL_SNAPSHOT_UNREADABLE` | `tests/unit/generation/statistical/test_snapshot_io.py` (new) |
| AT-E1-2 | `compile_plan` on a statistical column whose `snapshot_file` holds a `\xff` byte raises `PlanCompileError` carrying `STATISTICAL_SNAPSHOT_UNREADABLE` (the same wrapping a truncated file gets today) | `tests/unit/plan/test_generation_plan.py` |
| AT-E1-3 | the message of AT-E1-1 and AT-E1-2 contains `s.json` and none of `\xff`, `0xff`, `ÿ`, `position`, `codec` | as above |
| AT-E1-4 | the raised error has `__cause__ is None` and `__context__ is None`, for both the decode and the JSON branch | as AT-E1-1 |
| AT-E1-5 (control) | truncated JSON keeps today's code and message text | as AT-E1-2 |
| AT-E1-6 | `_load_snapshot` on the `\xff` file raises the same code (the second site) | `tests/unit/plan/test_snapshot_base_dir.py` or the new file |
| AT-E1-7 | a cold subprocess imports `decoy_engine.generation.statistical`, calls exported `parse_snapshot_json` on an invalid byte, and catches exported `StatisticalSpecError` without an import cycle | `tests/unit/generation/statistical/test_snapshot_io.py` (new) |
| AT-E1-8 | deserializing an embedded snapshot whose verified payload reaches the parser with a decode failure maps it to `DP_PINNED_SNAPSHOT_PAYLOAD_NOT_JSON`, with no decoder exception context | `tests/unit/plan/test_serialize.py` |
| AT-E2-1 | `apply_derived` `explicit_null`, row `{a: pd.NaT, b: Timestamp}`, `case_when(a < b, 1, 0)` returns `None` | `tests/unit/transforms/test_derived.py` |
| AT-E2-2 | same with `a = pd.NA` returns `None` | as above |
| AT-E2-3 | mask-mode run on a parquet source with a null timestamp feeding the expression writes a null cell (the reachability shape, `2,,2020-02-02,` not `...,0`) | `tests/unit/execution/test_derived_strategy.py` |
| AT-E2-4 | `sentinel` and `default` substitute for `NaT`; an expression that then fails raises today's value-free per-row text | `tests/unit/transforms/test_derived.py` |
| AT-E2-5 | formula with a `NaT` reference substitutes `""` and counts one `null_subs` | `tests/unit/generators/test_formula_missing.py` (new) |
| AT-E2-6 (control) | `"NaT"` and `""` references are not missing in derived and formula | as AT-E2-1 |
| AT-E2-7 (control) | assert the literal expected table `[(None, True), (float('nan'), True), (np.float32('nan'), True), (np.float64('nan'), True), (Decimal('NaN'), True), (pd.NaT, True), (np.datetime64('NaT'), True), (pd.NA, True), ('', False), ('NaT', False), (0, False), (False, False), (pd.Timestamp('2020-01-01'), False)]`; existing kernel fixed-answer tests remain byte-identical | `tests/unit/internal/test_pandas_missing.py` (new); `tests/unit/kernel/test_scalar_is_missing_na.py` unchanged and green |
| AT-E3-1 | `PositionalWhenGuard.check_schema` accepts `large_string` targets and references | `tests/native/test_c8_iii_d2_native_positional_when.py` |
| AT-E3-2 | chunked run, `--chunk-size 2`, parquet `large_string` source, categorical under `when: keep == 'A'`: output column values equal the `string`-source chunked run and the whole-frame run, and each output field type is `pa.string()` | `tests/native/test_chunked_nondet_categorical_admission.py` |
| AT-E3-3 (control) | an `int64` reference under the same `when` is still refused with the same code and text | as AT-E3-1 |
| AT-E3-4 | `_arrow_types.is_text` and `is_text_or_null` truth table over `string`, `large_string`, `null`, `binary`, `large_binary`, `int64`, `dictionary<int32, string>` | `tests/unit/test_arrow_types.py` (new) |

### 7.2 CLI (slice C1, after the repin)

| Id | Test |
|---|---|
| AT-C1-1 | `decoy validate` and `decoy run` on a config whose statistical snapshot holds `\xff`: both exit 1, both name `STATISTICAL_SNAPSHOT_UNREADABLE`, stderr and `--json` carry no `0xff` or codec text, and `run` writes no output file |
| AT-C1-2 | `decoy run` on a parquet source with a null timestamp feeding a `derived` column writes an empty cell |
| AT-C1-3 | `decoy run --chunked --chunk-size 2` on a parquet `large_string` source with `when:` on a categorical column, targeting CSV, exits 0 and its CSV bytes equal the CSV bytes from the run without `--chunked` |

Place: `tests/e2e/dup_defects/test_dup_defects_e2e.py` (new). The named subdirectory avoids
adding another module to the roughly 89-entry flat `tests/e2e/` directory.

## 7a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| missing-cell rule | kernel `_is_missing` body (moved, not rewritten) | no new predicate |
| text-type rule | `execution/_chunked_profile.py:248` `_is_string_type` (replaced by the owner) | no third copy in the guard |
| snapshot error code | `reasons.plan.STATISTICAL_SNAPSHOT_UNREADABLE` (`reasons/plan/_part3.py:72`) | no new reason code |
| per-row derived failure text | `evaluation_failure_text` | no new text |
| decode-catch shape | `vault.py:552` | |

## 8. Placement

| Change | File | CODEMAP row |
|---|---|---|
| `is_null_cell` | `src/decoy_engine/internal/pandas_missing.py` (new) | CODEMAP `internal/` private helpers row; beside `pandas_compat.py` without mixing concerns |
| missing-cell helper tests | `tests/unit/internal/test_pandas_missing.py` (new) | tests beside the owning `internal/` package |
| kernel alias | `src/decoy_engine/kernel/_scalar.py` | `kernel/` |
| derived alias | `src/decoy_engine/transforms/derived.py` | `transforms/derived.py` |
| formula check | `src/decoy_engine/generators/_formula.py` | `generators/` |
| formula missing-value test | `tests/unit/generators/test_formula_missing.py` (new) | tests beside the owning `generators/` package |
| `is_text`, `is_text_or_null` | `src/decoy_engine/_arrow_types.py` (new) | add a row beside the other top-level leaf modules |
| layer entry | `.importlinter` bottom layer | |
| reviewed layer snapshot | `tests/sentry/layout/test_import_layers.py` (`LAYERS`) | CODEMAP Layer contracts row |
| guard | `src/decoy_engine/execution/_chunked_when_guard.py` | `execution/` chunked |
| profile helper swap | `src/decoy_engine/execution/_chunked_profile.py` | `execution/` chunked |
| `parse_snapshot_json` | `src/decoy_engine/generation/statistical/_snapshot_io.py` (new) | extend the `generation/statistical/` row (line 59) |
| public helper export | `src/decoy_engine/generation/statistical/__init__.py` | `generation/statistical/` public package API |
| lazy cycle break | function-local `StatisticalSpecError` import in `src/decoy_engine/generation/statistical/_snapshot_io.py` | beside `_spec.py`; no top-level reverse edge |
| callers | `src/decoy_engine/plan/_generation.py`, `src/decoy_engine/generation/statistical/_spec.py`, `src/decoy_engine/plan/_serialize.py` | as today; plan callers use lazy public-package imports |
| cold import and helper tests | `tests/unit/generation/statistical/test_snapshot_io.py` (new), `tests/unit/plan/test_serialize.py` | tests beside the owning statistical and plan modules |
| line-only secret census refresh | `tests/sentry/secret_material_census.json` | existing sentry artifact; no key or baseline growth |
| CLI end-to-end proof | `tests/e2e/dup_defects/test_dup_defects_e2e.py` (new) | CLI end-to-end tests, in a named subdirectory under the mapped `tests/e2e/` surface |
| changelog | `changelog.d/dup-defects-e1.md`, `-e2.md`, `-e3.md` | |
| compat note | `docs/compatibility-contract.md` 3.3 (E2 only) | |
| docs | `docs/strategies.md:962-969` (E2: name `Decimal('NaN')`, numpy NaN scalars, `pd.NaT`, `numpy.datetime64('NaT')` and `pd.NA` as null) | |

## 9. Frozen contracts touched

- **Determinism (compatibility contract 3.3), E2.** Derived and formula output changes for rows
  where a referenced cell is `Decimal('NaN')`, a numpy NaN scalar, `pd.NaT`,
  `numpy.datetime64('NaT')` or `pd.NA`. Pre-GA (`RELEASE_PHASE` pre-GA), so a one-time
  break with no shim and no flag, recorded the way 3.3 records C1b-i and C5b-i: a dated
  paragraph naming the rows that change. The fragment carries a `Breaking:` line. No
  `SEED_PROTOCOL_VERSION` change: no derivation changes.
- **Reason codes.** None added or renamed. `STATISTICAL_SNAPSHOT_UNREADABLE` gains a trigger.
- **Exit codes.** `decoy run` on a non-UTF-8 snapshot moves from 3 to 1 through the existing
  compile-error map; no map changes.
- **`--json` shapes.** Unchanged.
- **Admission.** The chunked route accepts one more Arrow type. No REL-GATE surface is frozen yet
  (row 93 freezes at release).

## 10. Slices

| Slice | Content | Risk | Depends on | Tests at T1 |
|---|---|---|---|---|
| E1 | A5: `_snapshot_io.py`, public export, `_spec.py`, lazy plan callers in `_generation.py` and `_serialize.py`, fragment | R1 (error path) | none | AT-E1-*, plan and statistical unit tests, cold package import, module-size sentry, import contract |
| E2 | A10: `is_null_cell`, kernel alias, derived and formula, line-only secret census refresh, docs, compat note, fragment | R2 (output change) | none | AT-E2-*, kernel tests, `tests/unit/transforms/`, derived strategy tests, `tests/sentry/test_secret_material_census.py`, golden suites (`tests/snapshots/golden/`, `tests/integration/golden/`), physical-seam sentry |
| E3 | A15: `_arrow_types.py`, `.importlinter`, `tests/sentry/layout/test_import_layers.py` `LAYERS`, guard, profile swap, fragment | R2 (route admission) | none | AT-E3-*, `tests/native/` chunked when and admission tests, import contract and reviewed layer snapshot |
| C1 | CLI end-to-end tests, no `src/` change | R1 | E1 to E3 merged, in a daily engine release, and the CLI repinned to it (row 77) | AT-C1-* |

E1 to E3 are independent and touch no common file, so they can be built in parallel or by one
builder in order. The orchestrator may batch them into one PR with three commits; the gate then
runs at R2. Escalation triggers in `testing.md` hit by E2: none of shared fixtures, schema,
dependency lock, serialization, or moved tests, but the golden suites are the output alarm and
must run. E3 edits `.importlinter` (test config): that is an escalation trigger, so E3's merge
head gets the full engine suite against the stored main baseline.

Estimate: E1 small, E2 small, E3 small, C1 small. Fits well before the 2026-12-04 build date
and unblocks 24zf, which depends on 24zd.

## 11. Owner questions, answered 2026-11-13 under go-recommended

- **OQ-1. `sentinel` and `default` with a `NaT` reference.** Rule 1 makes these modes substitute
  `""` or `0` for a missing date, as their docs already say for `None` and NaN. An expression
  that compares that substitute with a date then fails the row (value-free text) where today it
  silently ran with `NaT`. Options: (a) one rule for all three modes, as designed; (b) only
  `explicit_null` uses rule 1 and the other two keep passing `NaT` through, documented as an
  exception. **Recommended: (a).** One null rule per transform is the point of the row, a loud
  row failure is better than a plausible wrong value, and `explicit_null` (the default) is the
  common case. **Answered 2026-11-13 under go-recommended: (a).**
- **OQ-2. B5 (non-UTF-8 config) timing.** Under the 24b rule it is a V1 item. Options: (a) leave
  it to 24zf C5, the one config loader, which lands before REL-GATE; (b) patch the seven CLI
  sites in this row now. **Recommended: (a).** (b) adds a fourth handler that C5 then deletes,
  and C5 is already scheduled before release.
  **Answered 2026-11-13 under go-recommended: (a).**
- **OQ-3. Tiering.** Confirm A5 as V1 under the 24b rule, and A6 and B11 as V2-LOG.
  **Recommended: confirm. Answered 2026-11-13 under go-recommended: confirm.**

## 12. Recurring-defect walk (`/home/cam/dev-rules/catalogs/recurring-defects.md`)

| # | Catalog class | Result |
|---|---|---|
| 1 | Anchored validation | N/A: no regex is added or changed. |
| 2 | Strict wire and stored types | N/A: no persisted or wire model field is added or loosened. |
| 3 | No exception context | Applies to A5. The helper raises only after both decode handlers close; AT-E1-4 checks `__cause__ is None` and `__context__ is None`, and AT-E1-8 checks the embedded-plan mapping. |
| 4 | Versioned read models | The snapshot schema-version check after parsing is unchanged for file and embedded readers; missing and unknown versions keep their current typed refusals. |
| 5 | Normalized guard scanners | N/A: no command, call or import scanner is added or copied. The existing import-layer fitness test is updated with `.importlinter`. |
| 6 | No reaching into private modules | `parse_snapshot_json` is exported by `generation.statistical`; both plan callers import it lazily from that public API, never `_snapshot_io.py`. Sibling `_spec.py` may use its package-private helper. |
| 7 | No hidden CI failures | No CI command or failure policy changes. E3 changes test configuration, so the named full-suite baseline comparison remains mandatory. |
| 8 | One input, one trust class per entry point | The helper accepts already-read snapshot bytes and a separately rendered `shown` structure. File reads and embedded bytes keep their existing entry points, containment and digest checks. |
| 9 | Mutations on the final head | Section 6.1 requires every named mutation to rerun on the final slice head. |
| 10 | Allowlisted helpers are still checked | N/A: no helper is added to an allowlist. Existing physical-seam entries remain checked by their sentry. |
| 11 | No raised caps | No ceiling, cap, allowlist or sentry baseline grows. `_arrow_types.py` reaches, but does not exceed, the 25-module root cap. E2 refreshes only the moved `kernel/_scalar.py::hash_array` `seed` line pin and lists it in the build report. |

## 13. V2-LOG

- A6: `profile/_serialize.py:153` on Python 3.10 refuses a hand-made profile ending in `Z`.
  Folds into the ED-4 helper.
- B11: no scheme check on `--notify` targets and `upgrade --url`. Hardening only.
- `json.loads` raises `RecursionError` on a deliberately deep snapshot. Accidental corruption
  does not produce it (V1 threat model is accidental leaks only).
- `json.loads(bytes)` accepts UTF-16, UTF-32 and surrogate escapes it can decode. A parsed lone
  surrogate can later raise `UnicodeEncodeError` during output. This row covers only the
  `UnicodeDecodeError` and `JSONDecodeError` branches named in Guarantee 4.
- `string_view` and dictionary-encoded text under the chunked `when` guard stay refused.
- `transforms/windowed_date.py:209` `_is_null_cell` and the other ED-2 sites move to
  `is_null_cell` in the ED-2 migration.

## 14. Pre-mortem

- **The kernel move changes output.** It is a verbatim move and AT-E2-7 checks parity; the
  golden suites and `test_scalar_vectorized_parity.py` run in T1.
- **Importing `pandas_compat` from `kernel` breaks a seam sentry.** `kernel/_scalar.py` today
  imports pandas only lazily. If `tests/sentry/test_physical_seam_disconnection.py` or another
  sentry pins the kernel's import set, the builder stops (15.3).
- **A user relies on today's `sentinel` behaviour with dates.** OQ-1 answer (a) accepts the
  one-rule behavior; the changelog `Breaking:` line names the affected config shape.
- **`large_string` is admitted but a later chunked step treats it differently.** AT-E3-2 compares
  values and the declared output field type against the `string` copy and whole-frame route,
  not just exit 0. AT-C1-3 separately compares bytes on a CSV target.
- **A golden fixture holds `NaT` under derived.** Then E2 changes a golden file; that change must
  be the only one, listed in the PR, and named in the compat note.
- **E24 later rewrites `_snapshot_io.py`.** The helper is the seam E24 was going to create; E24
  extends it rather than replacing it.

## 15. Stop lines for builders

1. Do not change any reason code, exit-code map, `--json` shape, or `SEED_PROTOCOL_VERSION`.
2. If `derived` or `formula` has a second evaluator on the native or out-of-core route that does
   its own null check, stop and report it; do not patch it in this slice.
3. If any sentry pins the import set of `kernel/_scalar.py`, stop and report it instead of
   adding an allow entry.
4. Do not migrate ED-1 or ED-2 sites beyond those named in section 4.
5. Do not raise any module-size ceiling, add a secret-census key or grow a sentry baseline. The
   named `hash_array` `seed` line-only pin refresh is required and must leave the verdict and
   key count unchanged.
6. A golden baseline may change only for a fixture that holds `Decimal('NaN')`, a numpy NaN
   scalar, `pd.NaT`, `numpy.datetime64('NaT')` or `pd.NA` under derived or formula; any other
   golden diff is a stop.
7. No test runs on the devbox. Mac first, then `~/bin/vm-run`.

## 16. Verification status

Sites, line numbers, import layers and module sizes were read on engine `8c9c1162` and CLI
`ee65431`. The reachability evidence (RUN outputs for A5, A10, A15) comes from
`REACHABILITY-A-B.md` at engine `c96e2855`; it was not re-run for this plan. No test ran.
