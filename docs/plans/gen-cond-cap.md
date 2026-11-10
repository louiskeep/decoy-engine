Status: plan

# GEN-COND-CAP: a raisable `condition_on` joint cap and two warnings (roadmap row 24w)

Owner: Cam. Date: 2026-11-10. Scope: engine plus CLI, V1, blocks V1. Source: DI models QA
2026-11-10 item H1 (CLI `docs/records/2026-11-10-cli-qa-generation-distribution-models.md`).
Base: engine `97a1cbbd`, CLI `8c5a1a1`. Risk: E1 R2 (value-free warning surfaces, a disclosure
bound, new registry codes), C1 R1. M1 (renamed parent) and M2 (hand-written unseen parent) stay in
V2-LOG. M2 gets the warning anyway, because it goes through the same fallback.

Rules consulted: `README.md` (routing), `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md` (Proportion, PLAN), `feature-dev.md`, `api-and-compatibility.md`.

## 1. Goal and guarantee

If a fitted pair has more than 25 combinations, the user can keep all of them, and fit says when
some were dropped. If generation draws a child for a parent value that has no fitted cell, the run
says so, with counts. With no cap set, behavior and snapshot bytes stay the same as today.

Non-goals: conditioning on a renamed parent (M1); a cap per pair; a YAML key; warnings from
windowed reads (`generate_rows`); a count-of-one rule for joint cells (OQ1, row 24f); changing how
the sampler falls back (it still uses the child's overall counts, and now it says so).

## 2. Current behavior

- Cap: `quality/snapshot.py:111` `DEFAULT_CONTINGENCY_TOP_K = 25`. `compute_distribution_snapshot`
  already takes `contingency_top_k` (`:134`) and passes it to `_joint_snapshot` (`:214`). That
  function sorts cells by count and keeps the head (`:572-575`), then writes `cell_count` and
  `other_count` (`:576-581`). Nothing checks the bound: 0 keeps no cells, and a negative number
  slices from the end. `distribution_snapshot_document` forwards the same keyword
  (`profiling/_snapshot_values.py:157`, `:182`). No caller passes it.
- CLI: `decoy fit` calls `compute_distribution_snapshot(df, joint_columns=...)` with no cap
  (`C:src/decoy/cli/fit.py:422`). It has no flag for the cap and prints nothing about truncation
  (`:505-527`).
- Generation: `_conditional_tables` builds the parent-to-child tables from the kept cells
  (`generation/statistical/_sample.py:249-266`). `sample_column` looks up `str(parent)`. On a miss
  it draws from the child's overall counts and gives no signal (`:355-366`). In a run, the only
  caller is `statistical_generate` (`generation/_statistical_column.py:65-85`, reached through
  `synthesize._generate_column` `:311`). `statistical` columns cannot stream
  (`generation/_admission.py:44`), so on every `run_generation` route a conditional column is drawn
  once, as a whole column, through that path. `window.generate_rows` (`window.py:203-209`) also
  calls `sample_column`, once per chunk.
- Warnings: generation has no structured warning channel today. The fidelity gate only writes a
  log line (`synthesize.py:211`). GEN-CLI E1 (plan `E:plan/gen-cli`, row 24o) adds an `on_warning`
  sink on `run_generation` that feeds `ExecutionResult.warnings`. This row uses that sink and does
  not add a second channel.

## 3. Established solutions

Search run (2026-11-10): synthpop conditional categorical synthesis, unseen parent levels,
small-cell disclosure. synthpop's saturated methods (`syn.catall`, `syn.ipf`) sample from a full
cross-tabulation and handle empty cells with an explicit prior (`nprior`), not a silent fallback.
Its disclosure guidance names small cells as the main risk and offers merging small categories
([syn.catall](https://search.r-project.org/CRAN/refmans/synthpop/html/syn.catall.html),
[disclosure vignette](https://bnowok.r-universe.dev/synthpop/doc/disclosure.Rnw)). The engine's
sampler already follows synthpop's sequential conditional scheme (`_sample.py:25-27`). What we take
from this: keep the full table when it is affordable (the raised cap), say when it is not (the
warnings), and leave small-cell handling to the redaction rules (OQ1). We do not adopt a prior:
that would change output bytes for every conditional column, which this row does not need.

## 4. The knob

- Engine: the existing keyword `contingency_top_k` on `compute_distribution_snapshot` and
  `distribution_snapshot_document`. No YAML key and no new snapshot field. The cap is chosen at fit
  time, and the snapshot already stores `cell_count` beside the kept cells.
- Bounds: an `int` (a `bool` is refused) from 1 to `MAX_CONTINGENCY_TOP_K = 1000`. The check runs
  once, in `compute_distribution_snapshot`, which both writers call. A value out of range raises
  `DistributionSnapshotError` with the new code `quality.contingency_top_k_invalid`. The message
  gives the bounds and the type it got, never a value from the frame.
- Why 1,000: an ordinary categorical column has at most 30 distinct values
  (`quality/snapshot.py:118`). An ordinary pair therefore has at most 30 x 30 = 900 cells, and a
  cap of 1,000 keeps every such pair whole. Only a pair with an opt-in `high_cardinality` column or
  a freetext column can go past that, and `condition_on` is a poor fit for those. The fit warning
  tells the user when it happens.
- Disclosure: each cell is a real value pair with an exact count. The cap is not a privacy
  control, since the head it keeps holds the most common pairs. But raising it keeps rarer pairs,
  and rare pairs identify people. Two rules already hold this in V1. `distribution_snapshot_document`
  withholds every joint that touches a sensitive column (engine #420), and generation refuses
  `condition_on` on a withheld joint. Values with a count of one belong to rows 24f and 24c (OQ1).
- CLI: `decoy fit --joint-max-cells N`, absent by default (the engine default of 25 applies).
  Using it without `--joint`, or together with `--epsilon`, is a usage error (exit 1). The CLI does
  not keep its own copy of the bounds. It maps the engine's `contingency_top_k_invalid` to exit 1
  with that code.

## 5. The two warnings

Both codes go in the engine reason registry (warning namespace, `QualityWarning` catalog). Their
`since` follows the registry rule in force at build time: `UNRELEASED` if REG-RELEASE has merged.
The text carries table names, column names and counts only, never a cell value or label.

| Code | Raised by | `detail` | Text (CLI) |
|---|---|---|---|
| `warning.fit_joint_cells_truncated` | new `snapshot_joint_warnings(doc)` in `profiling/_snapshot_values.py`, exported from `decoy_engine`. One record per joint where `len(cells) < cell_count`. Joints withheld as sensitive are skipped, because #420 already reports them | `columns`, `cell_count`, `cells_kept`, `other_count` | "joint parent x kid: 36 combinations, 25 kept; 366 rows fall in the dropped ones. A parent value with no kept combination is generated from the child's overall counts. Raise --joint-max-cells (at most 1000)." |
| `warning.generation_condition_parent_unseen` | `statistical_generate`, after the draw, through the GEN-CLI `on_warning` sink. One record per conditional column per run, only when the count is above 0 | `table`, `condition_on`, `rows`, `rows_total`, `parent_values` | "table t, column kid (condition_on parent): 61 of 6000 rows had one of 1 parent values with no fitted combination and drew from the column's overall counts." |

- Choke point: `sample_column` and the new `conditional_fallback_counts(spec, parent_values) ->
  (rows, distinct)` both use one helper, `_parent_key(parent)`, in `_sample.py`. The warning
  therefore counts exactly the rows that fell back. The guard is AT-3, and the mutation is
  counting with the raw parent in place of `_parent_key`.
- Null parents are left out of the count. A joint drops null pairs, so a null parent always falls
  back, and counting those rows would warn on every nullable parent. A parent equal to the
  `other_mode: emit` token is counted.
- The count reads the same parent list the sampler saw. It never draws again and never touches
  the RNG.
- Fit output: each record prints on stderr as `warning:` in human mode (not with `--quiet`).
  `fit --json` gains `"warnings": [{"code", "message", "detail"}]`, present even when empty. The
  exit code stays 0.
- Run output: the generation record goes through `ExecutionResult.warnings` to the run summary,
  `run --json` and the evidence manifest `quality_summary`, rendered the way GEN-CLI C1 renders
  warnings. This row's C1 adds the code's text to GEN-CLI's table of known codes.

Design notes: no new module and no new abstraction. One fact sits in one place: the bound is in
`quality/snapshot.py`, the lookup key in `_sample.py`, and the codes in the registry.

## 6. Failure modes

| # | Failure | Guard |
|---|---|---|
| F1 | Default fit bytes change and pinned snapshot digests break | AT-1 golden digest at the default |
| F2 | A warning or error carries a cell value or label | AT-6 scans str, repr, JSON and logs for the sentinel |
| F3 | The warning count disagrees with the rows that fell back | one `_parent_key`; AT-3 counts on its own |
| F4 | Counting changes output bytes or RNG use | AT-7: bytes equal with and without a sink |
| F5 | The warning repeats per chunk or route, or a route loses it | whole-column route only; GEN-CLI E1 transport; AT-8 |
| F6 | A bad cap (0, negative, bool, float, 1001) is accepted or crashes | AT-4 |
| F7 | `--joint-max-cells` is silently ignored (DP mode, no `--joint`) | AT-10 |
| F8 | A withheld joint gets a misleading "raise the cap" warning | AT-5 |
| F9 | Null parents set off the warning on every nullable parent | AT-3 null case |

## 7. Acceptance tests (red first)

Fixture `cond.csv`, built without an RNG: 6,000 rows. Parents `SENTP01` to `SENTP12` have weights
20, 15, 12, 10, 9, 8, 7, 6, 5, 4, 3, 1 (each weight x 60 rows). Parent i allows the children
`SENTK{(i+j) mod 6 + 1}` for j = 0, 1, 2, with weights 6, 3, 1. That gives 36 pairs. At cap 25 the
snapshot keeps 25 of them, `other_count` is 366, and `SENTP12` (60 source rows) has no kept cell.

- AT-1 Default unchanged: `compute_distribution_snapshot` and `distribution_snapshot_document`,
  run on the fixture with no cap argument, give bytes equal to a digest recorded at the base commit.
- AT-2 Cap raised: with `contingency_top_k=36` the snapshot keeps 36 cells, `other_count` is 0, and
  `snapshot_joint_warnings` returns nothing. Generating 6,000 rows of `kid` with
  `condition_on: parent` gives zero pairs that are absent from the source, and no generation
  warning.
- AT-3 Fallback warns once: at the default cap, `snapshot_joint_warnings` returns one record
  (`cell_count` 36, `cells_kept` 25, `other_count` 366). Generating 6,000 rows gives exactly one
  `generation_condition_parent_unseen` record. Its `rows` equals the number of generated rows with
  parent `SENTP12`, counted by the test, and `parent_values` is 1. When `cell_format` makes 10% of
  parents null, `rows` does not count the null rows.
- AT-4 Bounds: 0, -1, 1001, `True` and 2.5 raise `contingency_top_k_invalid`. 1 and 1000 work.
- AT-5 A pair with a sensitive column, written by `distribution_snapshot_document`, gives no
  truncation record.
- AT-6 No values: no warning, error message or captured log line from AT-2 to AT-5 contains the
  substring `SENT`.
- AT-7 The output bytes of AT-3 are the same with and without an `on_warning` sink.
- AT-8 Route matrix (the GEN-CLI AT-8 cells): resident, full-frame and mixed, including the
  runtime-decline handoff, each carry exactly one record in `ExecutionResult.warnings`.
- AT-9 CLI: `decoy fit cond.csv --joint parent,kid` exits 0, prints one `warning:` line on stderr
  with the three counts, and its `--json` output has one `warnings` entry. With
  `--joint-max-cells 36` it prints none.
- AT-10 CLI: `--joint-max-cells 0` exits 1 with `contingency_top_k_invalid`. With `--epsilon`, or
  without `--joint`, it exits 1 as a usage error and writes no snapshot.
- AT-11 CLI: `decoy run` on the AT-3 config shows the generation warning in the summary and in the
  `run --json` `warnings` list.

Mutations (core logic only): change `<` to `<=` in the truncation test; remove the null exclusion;
count with the raw parent in place of `_parent_key`. Each must turn an AT red.

## 8. Slices

| Slice | Repo | Content | Size | Gate |
|---|---|---|---|---|
| E1 | engine `feat/gen-cond-cap-e1` | bounds check and `MAX_CONTINGENCY_TOP_K`; `snapshot_joint_warnings` and its export; `_parent_key`, `conditional_fallback_counts`, the emission in `statistical_generate`; three registry codes (`quality.contingency_top_k_invalid` and the two warnings); `QualityWarning` docstring list; fragment `changelog.d/gen-cond-cap-e1.md`; AT-1 to AT-8 | S, about 120 lines plus tests | Dennis, then Codex. Full suite: the reason registry and the public API are escalation triggers |
| R | engine | release carrying E1 (owner release train) | n/a | release checks |
| C1 | CLI `feat/gen-cond-cap-c1` | engine pin bump; `--joint-max-cells`; fit warnings on stderr and in `--json`; run-summary text for the generation code; `cli-reference.md`; guide `03-fit-and-generate.md` (a row for the flag, and line 170 now says the run warns); CHANGELOG; AT-9 to AT-11 | S, about 80 lines plus tests | Codex. Full suite (version pin) |

Order: E1 builds after GEN-CLI E1 merges, because it needs `on_warning`. If GEN-CLI E1 has not
merged when E1 is ready, E1 ships everything except the generation emission and AT-7/AT-8, and that
part becomes E1b, built after GEN-CLI E1. C1 builds after three things: release R; row 24f's CLI
half, since both edit `fit.py` and C1 then passes the cap to the `distribution_snapshot_document`
call that 24f adds; and GEN-CLI C1, which owns the run-summary renderer.

Changelog: E1 adds behavior and is not Breaking. The new bounds only refuse values that already
misbehaved without an error, and no caller passes one. Fragment heading: `### Added (condition_on
joint cap and warnings, GEN-COND-CAP E1, <date>)`. C1 adds an `Added` entry to the CLI CHANGELOG.

## 9. Placement (engine `CODEMAP.md` "Where Things Go", CLI `CODEMAP.md` "Where things go")

| Change | Path | CODEMAP row |
|---|---|---|
| Bounds check, `MAX_CONTINGENCY_TOP_K` | `src/decoy_engine/quality/snapshot.py` (596 lines, +15; past 600 it needs a census entry and ceiling in `tests/sentry/test_module_size.py`, or the check moves into a small `quality/_snapshot_bounds.py` beside it) | snapshot computation stays in `quality/` |
| `snapshot_joint_warnings` | `src/decoy_engine/profiling/_snapshot_values.py` (199 lines, +30); export in `src/decoy_engine/__init__.py` | Detectors, profiling row (the fit snapshot writer); Public API row |
| `_parent_key`, `conditional_fallback_counts` | `src/decoy_engine/generation/statistical/_sample.py` (439 lines, +25) | Generation row; `_sample.py` is "the one sampler" |
| Emission | `src/decoy_engine/generation/_statistical_column.py` (110 lines, +15); `synthesize.py` passes the sink | Generation row |
| Codes | `src/decoy_engine/reasons/quality/_part1.py`, `src/decoy_engine/reasons/warning/_part1.py` | Reason codes row |
| Engine tests | `tests/unit/quality/test_snapshot_joint_cap.py`, `tests/unit/generation/statistical/test_condition_fallback.py` | Unit tests row |
| Fragment | `changelog.d/gen-cond-cap-e1.md` | Changelog fragments row |
| CLI flag and output | `src/decoy/cli/fit.py` (534 lines, +40) | existing command module; no new reader or writer, so `cli/io/` does not apply |
| CLI tests | `tests/e2e/first_user/test_items_fit_joint_cap.py` | first-user QA item tests row |

If a top-level import of `QualityWarning` in `_snapshot_values.py` makes an import cycle, it uses
a lazy import, as the file already does for `scrub_cell_format`.

## 10. Owner questions

- OQ1. Should joint cells with a count of one follow row 24f's count-of-one rule, and be folded
  into `other_count` in a plain snapshot unless values are opted in? Recommended: yes, built in
  24f's engine extension through the existing `("joint", "cells")` registry rule, not in this row.
  A raised cap lets those cells through on small data. This row does not wait on the answer. If
  that rule removes a parent's only cells, the generation warning reports it.
