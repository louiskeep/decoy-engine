Status: plan

# QA-FIT-REDACT and QA-DP-GATED: fit snapshots withhold sensitive values; DP refusal says what to do (roadmap rows 24f, 24g)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-08. Scope: engine + CLI, V1 blocker for 24f (leak
class), V1 usability for 24g. Revision 1.

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Design notes, Established solutions), `feature-dev.md`, `architecture.md`,
`reuse-first.md`, `security.md`, `testing.md` (test depth, escalation triggers), `cross-cutting-concerns.md`,
`api-and-compatibility.md`, `code-review.md`, `workspace-and-vcs.md`, `scope-discipline.md`,
`verification.md`, `documentation.md`, `catalogs/recurring-defects.md`; the plan contract
(`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md`), `LOCAL-BUILDER-BRIEF.md`; the standing rules
develop-correctly-registries-and-sizing and best-practice-becomes-a-sentry; the V1 bar (function break, data
loss or a non-admin value leak blocks; a second copy of a central mechanism, or a new module in a flat folder
over 25, always blocks); engine `CLAUDE.md` (cite the established method; `docs/methodology-registry.yaml`);
engine `docs/compatibility-contract.md:101-126` (`distribution-snapshot/v1` is a contract; new fields are
additive and optional); engine `docs/reasons.md:21-25` (additions raise the registry minor, one fixture per
minor). Code read at engine `main` `942dae2d`, CLI `main` `de55dab5`, and the QA-SCAN-REDACT S1 branch
`feat/scan-redact-s1` at `08788ee6` (this plan builds on it).

Sources: first-user distribution-integrity QA (2026-11-08, decoy-cli 0.1.0, decoy-engine 0.19.0):

- H2: "`decoy fit` (default, non-DP) copies raw values into the snapshot with no warning. `pat.snap.json`
  held every real name, SSN and patient id in `top_values` ... a user who emails the snapshot has already
  leaked."
- H3: "DP fit is unusable on a default install. Every `--epsilon` run ends with `dp_stack_uncertified ...
  fingerprint '4cd745...'`, exit 3 ... the message talks about proof stacks and lock fingerprints. Needs a
  plain-English line ... and a pointer in `explain differential-privacy` that this is gated."

Terminology: **value-sensitive** is the S1 predicate `is_value_sensitive` (any detector hit, a PII score at
0.6 or above, or a near-unique column; fails closed). User text says "sensitive columns", never "PII
candidate".

## 0. Review dispositions

None yet (revision 1).

## 1. FRAME

### Problem

`decoy fit` writes `compute_distribution_snapshot(df, ...)` verbatim (CLI `fit.py:422`, written at
`fit.py:497-499`). That snapshot holds, per column (engine `quality/snapshot.py`):

| Kind | Value-bearing stats | Producer |
|---|---|---|
| categorical (30 distinct or fewer, or bool, or HC-5) | `top_values[*].value`: up to 20 literal cells, every cell for HC-5 | `_categorical_stats` `:464-485`, `_high_cardinality_categorical_stats` `:294-398` |
| numeric | `min`, `max` (exact cells), `mean` (a cell when one value or constant), `std`, `quantiles` p05 to p95 (order statistics, often cells at small n), `bin_edges` (derived from the exact min and max) | `_numeric_stats` `:401-462` |
| datetime | `min`, `max`: exact timestamps in ISO form | `_datetime_stats` `:487-501` |
| joints | `cells[*].key`: both sides are literal cells | `_joint_snapshot` `:547-586` |
| freetext | none: lengths only | `_freetext_stats` `:504-531` |

On the QA frame (`patients`: 10 rows, `patient_id`, `full_name`, `ssn`) every column has 10 distinct values, so
all three are categorical and the file carries every real name, SSN and id. The engine refuses to *emit*
those values at generation without `allow_real_categories: true` (`generation/statistical/_spec.py:367-380`),
but the file has already left the process: it is the artifact users keep, share and commit.

DP fit (`--epsilon`) refuses on every normal install: `check_fit_environment` (`quality/dp_provenance.py:347-379`)
admits one platform (Linux, x86-64, CPython, glibc) and only the exact interpreter patches and lock
fingerprints in `_CERTIFIED_STACKS` (`:273`). The CLI prints the engine's message, which names a Python repr of
the platform triple and a 64-hex fingerprint, plus "this host is not a certified DP platform/stack."
(`fit.py:478-484`), and exits 3. Nothing says what to do instead, and `decoy explain differential-privacy`
(`explain.py:383-407`) mentions the gate only in its last paragraph.

### Guarantee (what V1 promises)

1. A distribution snapshot written by `decoy fit` holds no raw cell value of a value-sensitive column unless
   the user passed `--include-values`. Counts, null counts, distinct counts, kinds, year counts and text
   lengths are kept.
2. The set of value-sensitive columns is decided by the one engine predicate S1 introduced. A scan document and
   a snapshot of the same frame name the same columns in `redacted_columns`.
3. One engine module decides, for both artifacts, which fields hold values: `_value_redaction.py`. A new stats
   key or a new snapshot kind fails CI until it is classified.
4. Numeric and datetime sensitive columns still generate (`type: statistical`) from a redacted snapshot. A
   categorical sensitive column, or a `condition_on` joint through one, is refused at validate and run with a
   typed code that says what to do; `allow_real_categories: true` does not override it.
5. On an uncertified host, `decoy fit --epsilon` says in one plain line that DP fit is not available on this
   install, gives the next step, and refuses before the CSV is read. Exit code stays 3; the JSON `code` stays.
6. `compute_distribution_snapshot` is unchanged (in-memory callers: fidelity reports, the fidelity gate).
   Existing `distribution-snapshot/v1` files keep loading.

### Non-goals (V1)

- In-memory snapshots (`quality/report.py:140-147`, `generation/_fidelity_gate.py:143`) are the user's own
  process; they stay raw.
- The DP path (`fit_dp_snapshot`) is not redacted by this plan: its release is the DP mechanism's business.
  `--include-values` with `--epsilon` is a usage error.
- Columns the predicate does not flag keep their values (same scope and same known misses as QA-SCAN-REDACT,
  section 3 there).
- Making DP fit run on more hosts (certifying more rows) is DPS work, not this plan.
- A display-time redactor for loaded snapshot files: no CLI command prints a snapshot. Follow-up F2.

### Failure modes

| # | Failure | Guard |
|---|---|---|
| F-1 | A second "is this column sensitive" rule appears in the snapshot path | AT-14 parity plus the AT-14b structural guard (no threshold, no `pii_score` read in `_snapshot_values.py`) |
| F-2 | A new stats key or snapshot kind ships unclassified | AT-10 registry sentry (observed keys plus an AST read of the kinds `snapshot.py` can emit) |
| F-3 | A registry entry is never applied | AT-11 per-entry canary |
| F-4 | Coarsened numeric bounds still equal a real min or max | AT-4 property: bounds strictly outside the data (except the documented zero floor) |
| F-5 | Generation emits `[redacted]` strings as categories | AT-12 `load_spec` refusal, also with `allow_real_categories: true` |
| F-6 | Redaction breaks numeric or datetime generation | AT-3, AT-5 sample through `sample_column` |
| F-7 | `--include-values` used silently in CI | stderr warning naming columns and path; AT-C2 |
| F-8 | The CLI keeps calling `compute_distribution_snapshot` for output | AT-C8 AST guard |
| F-9 | DP refusal still reads the CSV first, or hides the engine detail entirely | AT-C6 (nonexistent CSV path still gets the DP refusal; `--verbose` and JSON carry the engine message) |
| F-10 | The CLI copies the certified profile (Python version, platform) into its own text | AT-C6 asserts the human line has no version or fingerprint; the detail comes only from the engine message |

## 2. Established solutions

- **Statistical disclosure control, global recoding and top/bottom coding** (Hundepool et al., *Statistical
  Disclosure Control*, Wiley 2012, sections 3.4.2 to 3.4.3; R `sdcMicro::globalRecode` and `topBotCoding`;
  US Census PUMS top-coding of income): a continuous variable is released over coarse, data-independent
  intervals and its extremes are replaced by a coding bound. We recode a sensitive numeric column onto a
  power-of-ten grid at the histogram's own bin resolution and replace min and max with grid bounds outside
  the data.
- **HIPAA Safe Harbor, 45 CFR 164.514(b)(2)(i)(C)**: all elements of dates except year are removed. A
  sensitive datetime column keeps its per-year counts and has min and max reduced to year bounds.
- **Heckbert, "Nice numbers for graph labels" (Graphics Gems, 1990)**: axis bounds snapped to 1, 2, 5 times a
  power of ten. We use the simplest form (powers of ten only) so the rule is one line and deterministic.
- **The S1 pattern** (classification registry plus default-deny serializer: Google Cloud DLP
  `include_quote`, ydata-profiling `sensitive=True`, Presidio `replace`): already cited for
  `_value_redaction.py`. Categorical values become the same `[redacted]` placeholder, counts kept; the flag is
  `--include-values` for the same reason as `scan`.
- **Precedent in this engine**: the DP numeric release writes `mean: null`, `std: null`, `quantiles: {}`
  (`quality/dp.py:405-418`), and fidelity already handles that shape (`quality/fidelity.py:370-377`,
  `no_quantiles`, with KS on the bins). A redacted numeric entry uses the same shape.

The new module cites SDC recoding, Safe Harbor and Heckbert in its docstring and gets a
`docs/methodology-registry.yaml` entry; `_value_redaction.py`'s entry gains the snapshot registry note.

## 3. Design notes

### Choke point and registry (the one decision point)

The task requires that fit reuse the redaction decision point of QA-SCAN-REDACT rather than add a second copy.
How:

1. **Which columns** come from `is_value_sensitive`, called on the profile of the same frame. The new function
   runs `profile_columns(df, source_label)` (the profiler's defaults, the same call `decoy scan` makes for a
   CSV) and passes each field record to `is_value_sensitive`. No threshold, detector list or uniqueness rule is
   restated. AT-14 proves a scan document and a snapshot document of the same frame list the same columns;
   AT-14b forbids `_snapshot_values.py` from reading `pii_score`, `detector_matches` or `is_likely_unique`,
   or holding a numeric literal threshold.
2. **Which fields are values** lives in the same file as `VALUE_FIELDS`. `_value_redaction.py` gains
   `SNAPSHOT_VALUE_STATS` and `SNAPSHOT_VALUE_FREE_STATS`, keyed by `(scope, key)`, beside the profile
   registries. The marker keys (`value_policy`, `redacted_columns`), the policy values
   (`flagged-redacted/v1`, `raw`) and the placeholder `[redacted]` are the S1 constants, imported, never
   redefined. One file answers "which fields of a Decoy artifact are cell values"; a reviewer reads one diff
   to add a field to either artifact.
3. **How a value is removed** (mechanics) lives in `profiling/_snapshot_values.py`, because a snapshot entry
   needs the data to re-bin (a profile entry does not). It applies the registry, never decides membership.

```python
# _value_redaction.py, beside VALUE_FIELDS
class SnapshotRule(enum.Enum):
    COARSE_RANGE = "coarse_range"   # min, max, bin_edges, bin_counts re-derived on a coarse grid
    NULL = "null"
    EMPTY_MAP = "empty_map"
    PLACEHOLDER = "placeholder"
    YEAR_START = "year_start"
    YEAR_END = "year_end"

SNAPSHOT_VALUE_STATS: Final[Mapping[tuple[str, str], SnapshotRule]] = MappingProxyType({
    ("numeric", "min"): SnapshotRule.COARSE_RANGE,
    ("numeric", "max"): SnapshotRule.COARSE_RANGE,
    ("numeric", "bin_edges"): SnapshotRule.COARSE_RANGE,
    ("numeric", "bin_counts"): SnapshotRule.COARSE_RANGE,  # re-binned on the new edges
    ("numeric", "mean"): SnapshotRule.NULL,
    ("numeric", "std"): SnapshotRule.NULL,                 # with the mean, fixes both cells of a 2-row column
    ("numeric", "quantiles"): SnapshotRule.EMPTY_MAP,
    ("top_value", "value"): SnapshotRule.PLACEHOLDER,
    ("datetime", "min"): SnapshotRule.YEAR_START,
    ("datetime", "max"): SnapshotRule.YEAR_END,
    ("joint_cell", "key"): SnapshotRule.PLACEHOLDER,       # only the sensitive side's position
})
SNAPSHOT_VALUE_FREE_STATS: Final[frozenset[tuple[str, str]]] = frozenset(
    _free("document", "schema_version", "row_count", "columns", "joints")
    | _free("column", "dtype", "kind", "null_count", "non_null_count", "distinct_count", "stats")
    | _free("categorical", "top_values", "other_count", "high_cardinality")
    | _free("top_value", "count")
    | _free("datetime", "year_bins")
    | _free("year_bin", "year", "count")
    | _free("freetext", "length", "length_bin_edges", "length_bin_counts")
    | _free("length", "min", "max", "mean", "std")
    | _free("joint", "columns", "cell_count", "cells", "other_count")
    | _free("joint_cell", "count")
)
SNAPSHOT_KINDS: Final = frozenset({"numeric", "categorical", "datetime", "freetext", "empty"})
```

`top_values` is value-free as a container (its length and order are kept); its entries' `value` is the value
slot. `year` is value-free by the Safe Harbor rule. Freetext lengths describe the cells, not the cells.

The function:

```python
def distribution_snapshot_document(
    df: pd.DataFrame,
    *,
    source_label: str = "<frame>",
    include_values: bool = False,
    joint_columns: Sequence[tuple[str, str]] | None = None,
    numeric_bins: int = 10,
    categorical_top_k: int = 20,
    contingency_top_k: int = 25,
    high_cardinality_columns: Collection[str] = (),
) -> dict[str, Any]:
    """The snapshot JSON to write for `df`: the raw snapshot with sensitive columns' values withheld."""
```

Steps: compute the raw snapshot with the same arguments (defaults taken from `quality/snapshot.py` constants,
not retyped); profile the frame; pick sensitive names; for each sensitive column apply the rules for its kind;
for each joint replace the key position of each sensitive side; stamp `value_policy` and `redacted_columns`
exactly as `redact_profile_dict` does (with `include_values=True` the raw snapshot plus `value_policy: raw` and
the sensitive names). The raw snapshot is never mutated in place after it is shared: the function owns it.

### Numeric coarsening (global recoding)

For a sensitive numeric column with finite `min` = lo and `max` = hi, and `bins` = the snapshot's bin count:

```
w    = (hi - lo) / bins            if hi > lo else max(|lo|, 1)
unit = 10 ** floor(log10(w));      integer dtype: unit = max(unit, 1)
lo'  = unit * (ceil(lo / unit) - 1)     # strictly below lo
hi'  = unit * (floor(hi / unit) + 1)    # strictly above hi
if lo >= 0: lo' = max(lo', 0)            # a non-negative column stays non-negative
```

Then the column's stats are rebuilt by `_numeric_stats(series, bins=bins, domain=(lo', hi'))`
(`quality/snapshot.py:401`, whose `domain` parameter has had no caller since the DPS Scope A removal), so
coercion, finite filtering, rounding and the histogram are the snapshot's own code, and `sum(bin_counts)`
equals the finite count. `mean` and `std` become `null` and `quantiles` becomes `{}` (the DP numeric shape).
What a reader learns: each extreme to within one `unit`, and `unit` is at most the new bin width, so the bounds
say nothing the histogram does not already say. A column with no finite value (`min: null`) is already
value-free and is left alone. Rounding uses the snapshot's `_round` (12 places), so `0.1 * 3` style noise does
not reach the file.

The private import of `_numeric_stats` from a sibling package is deliberate: it is the one histogram the
snapshot uses, and copying it would be a second copy of the binning rule. `snapshot.py` stays at 600 lines (no
census entry); renaming it public is follow-up F4.

### Datetime, categorical, joints

- Datetime: `min` becomes `YYYY-01-01T00:00:00` of the earliest year and `max` `YYYY-12-31T23:59:59` of the
  latest; `year_bins` stay. The sampler draws a year from `year_bins` and a uniform instant in it, clamped to
  min and max (`_sample.py:328-360`), so it keeps working and now spans whole years.
- Categorical (including HC-5): each `top_values[i].value` becomes `[redacted]`; counts, order, `other_count`
  and the `high_cardinality` marker stay. HC-5 is not special-cased: its values are withheld like any
  categorical, and generation refuses it (below).
- Joints: in `cells[*].key`, position 0 is replaced when `columns[0]` is sensitive and position 1 when
  `columns[1]` is; counts, `cell_count` and `other_count` stay.

### Generation refusal (E2)

`load_spec` (`_spec.py:221`) gains one check, placed before the `allow_real_categories` check so the more
useful message wins:

- the column's kind is `categorical`, not `dp_verified`, and either its source column is in the snapshot's
  `redacted_columns`, or any `top_values[*].value` equals `[redacted]`, or `redacted_columns` is present and is
  not a list of strings (fail closed); or
- `condition_on` is set and either joint column is in `redacted_columns` or any joint cell key equals
  `[redacted]`.

Code: new `plan.statistical_snapshot_values_redacted`. Message: "statistical column 'X': the snapshot withheld
the real values of source column 'Y' (a sensitive column), so there are no categories to sample. Use a faker
or pattern generator for this column, or re-fit with `--include-values` and set `allow_real_categories:
true` (the snapshot then holds real values)." Reading the marker to refuse is safe; it is never read to grant
anything (the `_spec.py` non-inference rule). `plan/_checks.py:362` calls `load_spec`, so validate reports the
same code.

### CLI fit (C1)

- `fit.py:422` calls `distribution_snapshot_document(df, source_label=source.name, include_values=...,
  joint_columns=...)` instead of `compute_distribution_snapshot`.
- New option `--include-values` ("Keep real values of sensitive columns in the snapshot. The file then holds
  real data."). With `--epsilon` it is a usage error (exit 1) in the existing D0 block, before the CSV is read.
- Human success output adds one line when `redacted_columns` is non-empty and values were withheld:
  `values withheld for 3 sensitive column(s): full_name, patient_id, ssn (counts kept; numbers rounded,
  dates to the year)`, and a hint: `to sample these as categories, re-run with --include-values; the file
  then holds real data.` With `--include-values`, a stderr warning: `warning: <path> holds real values of 3
  sensitive column(s): ... Do not share or commit it.`
- JSON success payload gains `value_policy` and `redacted_columns`.
- The CLI reads `redacted_columns` from the document; it never calls the predicate.

### CLI DP message (C2)

- After the D0 usage checks and before `pd.read_csv` (`fit.py:400`), DP mode calls
  `decoy_engine.quality.dp_provenance.check_fit_environment()`. `fit_dp_snapshot` still calls it too; the
  early call only moves the refusal ahead of reading private data.
- On `dp_platform_uncertified` or `dp_stack_uncertified`, human stderr shows:
  `error: DP fit is not available on this install: it runs only on a certified platform and package set.` and
  `hint: run decoy fit without --epsilon (sensitive columns are withheld by default); see decoy explain
  differential-privacy.` The engine's own message (code, platform, fingerprint) is printed only under
  `--verbose`. Exit 3 as today.
- JSON keeps `code`, `message` (the engine text) and `error`, and `hint` carries the new next step.
- The CLI names no Python version or platform of its own: the certified rows live only in the engine.
- `--epsilon` help adds "Runs only on a certified DP host; see `decoy explain differential-privacy`." The
  `differential-privacy` topic summary becomes "Fit a DP release with `decoy fit --epsilon` (certified hosts
  only)." and its body opens with the gate and the no-DP alternative instead of ending with it. The QA note
  that the topic is missing from `decoy explain` is not reproduced on `de55dab5` (`_render_index` lists every
  `_TOPICS` entry, `explain.py:565-576`); AT-C7 pins it.

## 4. Behavior

| Input | Today | After |
|---|---|---|
| `decoy fit patients.csv` (QA frame) | file holds every name, SSN, id | `top_values[*].value` is `[redacted]` for all three; stdout names them; exit 0 |
| same with `--include-values` | n/a | today's file plus `value_policy: raw`, `redacted_columns`; stderr warning; exit 0 |
| `--include-values --epsilon 1 ...` | n/a | usage error, exit 1, CSV not read |
| numeric sensitive column `balance` | exact min, max, mean, quantiles | grid bounds outside the data, re-binned counts, mean/std null, quantiles `{}` |
| datetime sensitive column | exact first and last instants | year bounds; `year_bins` unchanged |
| non-sensitive column (`status`, `diagnosis`) | raw | unchanged, byte for byte |
| `type: statistical` on a redacted numeric or datetime column | n/a | generates inside the coarse bounds |
| `type: statistical` on a redacted categorical column | n/a | validate and run fail with `plan.statistical_snapshot_values_redacted` and the two next steps |
| a snapshot written before this change | loads | loads (no marker, no placeholders: unchanged behavior) |
| `decoy fit --epsilon ...` on an uncertified host | engine repr and fingerprint, exit 3, CSV read first | plain line plus next step, exit 3, CSV not read; detail under `--verbose` and in JSON |

## 5. Enumerations (from the code at engine `942dae2d`, CLI `de55dab5`)

### 5.1 Exit paths (CLI fit)

| Path | Code | Change |
|---|---|---|
| usage errors in D0 (`fit.py:337-395`) | 1 | adds `--include-values` with `--epsilon` |
| unreadable CSV, missing `--parse-dates` column (`:399-419`) | 1 | none |
| DP uncertified (`:478-484`) | 3 | moved before the CSV read; new human text |
| DP budget, carrier errors (`:485-491`) | 1 or 3 | none |
| write failure (`:496-502`) | 3 | none |
| success | 0 | new output line, JSON keys |

### 5.2 Routes (every way a distribution snapshot reaches a file or a user)

| # | Route | Before | After |
|---|---|---|---|
| R1 | `decoy fit` exact mode (`fit.py:422`, `:497`) | raw | `distribution_snapshot_document` |
| R2 | `decoy fit --epsilon` (`fit.py:475`) | DP release | unchanged (DP owns it) |
| R3 | engine library callers of `compute_distribution_snapshot` (`quality/report.py:140-147`, `_fidelity_gate.py:143`) | in memory, raw | unchanged; the docstring names the document function for anything written |
| R4 | platform | does not call fit (`grep` of platform `main` finds no `compute_distribution_snapshot`) | none; F1 if it starts |

### 5.3 Snapshot kinds (`_stats_for`, `snapshot.py:269-291`, plus `empty` at `:247-249`)

numeric, categorical (bool, low-cardinality object, HC-5), datetime, freetext, empty. AT-10 reads these
literals from the source so a sixth kind fails the sentry.

### 5.4 Snapshot readers

| Reader | Uses | Effect of redaction |
|---|---|---|
| `_spec.load_spec` | kind, dtype, stats, joints | refusal for redacted categorical and joints (E2) |
| `_sample.sample_column` numeric (`:265-286`) | `bin_edges`, `bin_counts` | works on the coarse grid |
| `_sample.sample_column` datetime (`:328-360`) | `year_bins`, `min`, `max` | works, whole years |
| `_sample.sample_column` freetext | length bins | unchanged |
| `plan/_generation.read_and_pin_snapshots` (`:102`) | bytes, digest | unchanged |
| `plan/_checks_dp_carrier.py:187-199` | DP-shape test, only on DP-declared pipelines over `dps-marginal/v3` | a redacted `v1` snapshot fails the DP schema check first; no false DP verdict |
| fidelity (in memory only) | quantiles or KS on bins | not reached by files |

### 5.5 FK shapes

The snapshot has no keys or references of its own; two shapes touch keys:

- A key column fitted on its own (QA `patient_id`): near-unique, so value-sensitive; categorical values are
  withheld and a numeric id is recoded.
- A joint of a key column and another column (`--joint patient_id,diagnosis`): the key side of each cell is
  withheld, and `condition_on` through it is refused (E2).

Generated FK columns use `type: reference`, not the snapshot (DI QA H4 is row 24h, not this plan).

### 5.6 Fields (every key a snapshot can carry, `snapshot.py` at `942dae2d`)

| Scope | Keys | Class |
|---|---|---|
| document | `schema_version`, `row_count`, `columns`, `joints` (+ `value_policy`, `redacted_columns` new) | value-free |
| column | `dtype`, `kind`, `null_count`, `non_null_count`, `distinct_count`, `stats` | value-free |
| numeric | `min`, `max`, `bin_edges`, `bin_counts` | coarse range |
| numeric | `mean`, `std` | null |
| numeric | `quantiles` | empty map |
| categorical | `top_values`, `other_count`, `high_cardinality` | value-free containers and counts |
| top_value | `value` / `count` | placeholder / value-free |
| datetime | `min` / `max` / `year_bins` | year start / year end / value-free |
| year_bin | `year`, `count` | value-free (Safe Harbor keeps year) |
| freetext | `length`, `length_bin_edges`, `length_bin_counts` | value-free |
| length | `min`, `max`, `mean`, `std` | value-free (lengths, not cells) |
| joint | `columns`, `cell_count`, `cells`, `other_count` | value-free |
| joint_cell | `key` / `count` | placeholder on the sensitive side / value-free |

### 5.7 Output surfaces

| Surface | Change |
|---|---|
| snapshot file (`fit --output` or `<csv>.snapshot.json`) | redacted by default |
| `fit` human stdout | one withheld-columns line plus a hint |
| `fit` stderr | `--include-values` warning; DP refusal text (C2) |
| `fit --json` | `value_policy`, `redacted_columns`; DP error `hint` text |
| `validate` and `run` errors | new refusal code and message (E2) |
| `fit --help`, `explain differential-privacy`, `docs/cli-reference.md` | gated-DP wording, new option |

## 6. Class table

| # | Class | Applies here | Structure that closes it | Sentry or property test | Mutation that turns it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | raw cells in fit snapshots (R1) | `distribution_snapshot_document` over `SNAPSHOT_VALUE_STATS` | AT-1, AT-2, AT-11, AT-C1 | M1: move `("top_value", "value")` to the value-free set (AT-1, AT-11 red) |
| 2 | Cleanup missing on an exit path | N/A: no temp files or handles; the write stays on `write_local_target` | N/A | N/A | N/A |
| 3 | State and ordering | DP refusal after the CSV read; redaction mutating the raw snapshot a caller holds | early `check_fit_environment`; the function owns its raw snapshot | AT-C6 (nonexistent CSV still gets the DP refusal); AT-9 | M3: move the early check after `pd.read_csv` (AT-C6 red) |
| 4 | Route parity and silent fallback | the CLI keeps `compute_distribution_snapshot`; a second predicate in the snapshot path | AST guard on `src/decoy`; parity of `redacted_columns` with the scan document | AT-C8, AT-14, AT-14b | M4: in `_snapshot_values.py`, treat `pii_score >= 0.5` as sensitive (AT-14b red) |
| 5 | Referential integrity | joints keep pairs; a redacted side must not feed `condition_on` | key-position redaction plus E2 refusal | AT-7, AT-12 | M5: drop the joint clause of the E2 check (AT-12 joint cell red) |
| 6 | Untrusted producer | a hand-edited snapshot: placeholders without a marker, an ill-typed `redacted_columns` | refusal on marker or placeholder, fail closed on a bad marker type | AT-12 cells | M6: refuse only on the marker (AT-12 placeholder cell red) |
| 7 | Check then use on a path (TOCTOU) | N/A: no check-then-open added; the CSV is read once | N/A | N/A | N/A |
| 8 | Weak tests and overclaimed evidence | a fixture that never fills a value slot; a coarse bound that happens to equal a cell | AT-11 precondition (raw holds the canary); AT-4 strictly-outside property | AT-4, AT-11 | M8: drop the `- 1` in the `lo'` formula (AT-4 red on grid-aligned data) |
| 9 | Behavior-preserving refactor drift | `compute_distribution_snapshot` must stay raw; non-sensitive columns byte-identical | wrapper, not an edit; `snapshot.py` untouched | AT-8, AT-9, existing `tests/unit/quality/test_snapshot.py` | M9: redact inside `compute_distribution_snapshot` (snapshot tests and AT-9 red) |
| 10 | Frozen contract drift | `distribution-snapshot/v1` gains two optional keys; value types already allowed (`mean: null` exists for all-non-finite columns, `snapshot.py:412-420`); reason registry gains one code | additive keys only; registry minor plus fixture | AT-9, AT-13, `test_reason_minor_fixtures.py`, `test_reason_registry.py` | M10: write `mean` as the string `[redacted]` (AT-2 type assertion red) |

## 7. Acceptance tests (red first)

Engine E1 (`tests/unit/profiling/test_snapshot_values.py`, `tests/sentry/profiling/test_snapshot_value_registry.py`):

- **AT-1** QA repro: the QA `patients` frame (10 rows; `patient_id`, `full_name`, `ssn`; the DI QA seed-7
  shape) through `distribution_snapshot_document`. No source name, SSN or id appears in `json.dumps` of the
  result; `full_name` and `ssn` are in `redacted_columns`; `value_policy == "flagged-redacted/v1"`. Red: the
  function does not exist.
- **AT-2** Numeric canary frame (S1's `balance`: 31337.25 to 98765.5, mean 50155.5875): no canary in the
  document; `min`/`max` are multiples of the computed unit with `min < 31337.25 < 98765.5 < max`;
  `len(bin_edges) == bins + 1`; `sum(bin_counts) == non_null_count`; `mean is None`, `std is None`,
  `quantiles == {}`.
- **AT-3** Numeric generation: `load_spec` plus `sample_column` over the redacted snapshot draws 1000 values
  all inside `[min, max]`; an integer column draws `int`s.
- **AT-4** Coarse-domain table and property (Hypothesis over finite floats and ints, bins 1 to 50): constant
  column; negative-only range; a range crossing 0; `lo == 0` (floor holds, no negative bound); integer dtype
  (unit at least 1); `1e-9` scale; `1e15` scale; values on the grid. Property: `lo' < lo` unless `lo >= 0`
  and `lo' == 0`; `hi' > hi`; `(hi' - lo') / bins >= unit`; both bounds are multiples of `unit` after
  `_round`.
- **AT-5** Datetime: `admitted` from `1991-06-01` for 20 days: `min == "1991-01-01T00:00:00"`,
  `max == "1991-12-31T23:59:59"`, `year_bins` equal the raw ones; no `1991-06` canary; sampling stays in 1991.
- **AT-6** Categorical: `top_values` keep length, order and counts; every `value == "[redacted]"`;
  `other_count` unchanged; an HC-5 column keeps `high_cardinality: True`.
- **AT-7** Joints: `--joint full_name,diagnosis`-style pair: position of the sensitive side is `[redacted]`,
  the other side and all counts unchanged; a joint of two non-sensitive columns is unchanged.
- **AT-8** Non-sensitive columns (`status`, a non-unique numeric) are equal to the raw snapshot's entries.
- **AT-9** `include_values=True` equals `compute_distribution_snapshot(...)` with the same arguments plus
  `value_policy: "raw"` and the sensitive names; the raw snapshot passed through is not mutated.
- **AT-10** Registry sentry: a fixture frame that produces every kind, a joint and an HC-5 column; every key
  at every scope is in exactly one of `SNAPSHOT_VALUE_STATS` and `SNAPSHOT_VALUE_FREE_STATS`; the kinds seen
  equal `SNAPSHOT_KINDS`, and `SNAPSHOT_KINDS` equals the kind literals an AST read of `snapshot.py`'s
  `_stats_for` returns plus the `"empty"` assignment in `_column_snapshot`. Bite proofs: monkeypatch
  `_numeric_stats` to add a key (red: unclassified), and an AST fixture with a sixth kind literal (red).
- **AT-11** Per-entry canary: for each `SNAPSHOT_VALUE_STATS` entry, a fixture column whose raw snapshot holds
  a canary in that slot (precondition asserted), and the document does not.
- **AT-13** Determinism: two calls on the same frame give identical `json.dumps(..., sort_keys=True)` bytes.
- **AT-14** Parity: for the S1 canary frame and the QA frame, `redacted_columns` of the snapshot document
  equals `redacted_columns` of `profile_document(profile_columns(df, label))`.
- **AT-14b** Structural guard: `_snapshot_values.py` imports `is_value_sensitive`, and its AST contains no
  attribute or subscript read of `pii_score`, `detector_matches` or `is_likely_unique` and no float literal in
  `[0, 1]` outside the `_round` call.

Engine E2 (`tests/unit/generation/test_statistical_redacted.py`):

- **AT-12** `load_spec` raises `statistical_snapshot_values_redacted` for: a categorical column in
  `redacted_columns` with and without `allow_real_categories: true`; a categorical column with a `[redacted]`
  value and no marker; `redacted_columns` set to a string; `condition_on` through a joint with a redacted side.
  A redacted numeric and datetime column load. The same config through the plan compiler reports the same code
  at validate (`plan/_checks.py:362`).

CLI C1 (`tests/e2e/test_fit_command.py`, `tests/sentry/test_fit_value_redaction.py`):

- **AT-C1** QA repro end to end: `decoy fit patients.csv`: the file has no source name or SSN; stdout names the
  withheld columns; exit 0.
- **AT-C2** `--include-values`: the file holds the values; stderr warning names the columns and the path.
- **AT-C3** `--include-values --epsilon 1 --delta 1e-6 --dp-text x` with a nonexistent CSV: exit 1 usage error.
- **AT-C4** `--json` success payload has `value_policy` and `redacted_columns`.
- **AT-C5** fit then `decoy validate` on a generate config: a `type: statistical` numeric sensitive column
  passes; a categorical sensitive column fails with `statistical_snapshot_values_redacted` and the hint text.
- **AT-C8** AST guard: no module under `src/decoy` calls `compute_distribution_snapshot`.

CLI C2 (`tests/e2e/test_fit_command.py`, `tests/e2e/test_explain_command.py`):

- **AT-C6** With `check_fit_environment` patched to raise each of the two codes, and a nonexistent CSV path:
  exit 3; human stderr has the plain line and the hint and no `fingerprint`, `PlatformTriple`, or digit run of
  8 or more; `--verbose` adds the engine message; `--json` has `code`, `message`, `hint`.
- **AT-C7** `decoy fit --help` says DP runs only on a certified host; `decoy explain differential-privacy`
  summary and first paragraph say so; `decoy explain --json` lists the topic.

## 8. Owner questions

None. Recommended options taken without asking (V1 bar): redact by the S1 predicate rather than every column
(same as OQ1 of QA-SCAN-REDACT); recode numeric and datetime rather than drop them, so generation still works;
refuse categorical generation rather than sample placeholders; keep `--epsilon` visible but say it is gated
rather than hide it.

## 9. Reuse inventory

| Need | Reused | Not reused, why |
|---|---|---|
| Which columns | `is_value_sensitive` and `profile_columns` (S1, profiler) | no snapshot-side rule |
| Marker keys, placeholder, policy values | S1 constants in `_value_redaction.py` | |
| Raw snapshot | `compute_distribution_snapshot` | |
| Re-binning | `_numeric_stats(domain=...)` (`snapshot.py:401`) | no second histogram |
| Defaults | `snapshot.py` `_DEFAULT_*` constants | not retyped |
| Refusal plumbing | `StatisticalSpecError`, `reasons.plan` | |
| DP gate | `check_fit_environment` | the CLI does not restate certified rows |
| CLI error output | `_emit_error` (`fit.py:302-324`) | |

## 10. Placement

| Item | Where | Size | Why there |
|---|---|---|---|
| snapshot registry, `SnapshotRule`, `SNAPSHOT_KINDS` | engine `profiling/_value_redaction.py` | +45 (282 now on S1, about 330 after) | the one file that decides which artifact fields are values |
| `distribution_snapshot_document`, coarse domain, per-kind rules | engine `profiling/_snapshot_values.py` (new) | about 170 | profiling owns the sensitivity decision; `profiling/` has 8 modules with S1 (9 after); `quality/` already has 25 `.py` files, so a new module there would block, and `snapshot.py` is at 600 |
| exports | `profiling/__init__.py`, `decoy_engine/__init__.py` | +4 | public surface for the CLI |
| refusal | `generation/statistical/_spec.py` | +25 (481 to about 506) | the one place generation reads snapshot stats |
| reason code | `reasons/plan/_part3.py` | +8 (61 to 69) | the file holding the other statistical snapshot codes |
| engine tests | `tests/unit/profiling/test_snapshot_values.py`, `tests/sentry/profiling/test_snapshot_value_registry.py`, `tests/unit/generation/test_statistical_redacted.py` | about 300, 140, 120 | |
| CLI wiring | `cli/fit.py` | +35 (C1) and +20 (C2), 534 to about 590 | edits in place; no new CLI module |
| CLI text | `cli/explain.py` DP topic | about +4 net | |
| CLI sentry | `tests/sentry/test_fit_value_redaction.py` plus manifest | about 60 | |

## 11. Frozen contracts touched

- **`distribution-snapshot/v1`**: two optional top-level keys (`value_policy`, `redacted_columns`), which the
  contract allows (`compatibility-contract.md:124-126`). Redacted values use types v1 already carries
  (`null` mean and std, `{}` quantiles, string top values). Old readers ignore the keys; old files load.
- **Reason registry**: one addition, `plan.statistical_snapshot_values_redacted`, at the next minor of the
  base (1.10.0 on today's main) with fixture `tests/fixtures/f29/reason-manifest-v1.10-fit-redact.json`;
  `registry_renumber.py` moves it at merge. Escalation trigger (frozen contract): E2 runs the full engine
  suite.
- **Engine public API**: one new export, `distribution_snapshot_document`; nothing removed.
- **CLI `fit --json`**: two new keys on success; the DP error payload keeps its keys, `hint` text changes.
- **CLI pin**: C1 bumps `decoy-engine` (escalation trigger: dependency lock; full CLI suite).
- **`docs/cli-reference.md`**: regenerated for `--include-values` and the help text (drift-checked by
  `tests/unit/test_cli_surface.py`).

## 12. Slices

| Slice | Repo | Files (at most 10) | Lines | Tests |
|---|---|---|---|---|
| E1 | engine | `_value_redaction.py`, `_snapshot_values.py`, `profiling/__init__.py`, `decoy_engine/__init__.py`, unit test, sentry test, `tests/unit/test_public_api.py`, `tests/sentry/test_sentry_manifest.py`, `docs/methodology-registry.yaml`, `CHANGELOG.md` (10) | about 700 | AT-1 to AT-11, AT-13, AT-14, AT-14b first; T1 = `tests/unit/profiling`, `tests/sentry`, `tests/unit/quality/test_snapshot.py`, `tests/unit/generation/test_statistical.py` |
| E2 | engine | `_spec.py`, `reasons/plan/_part3.py`, fixture `reason-manifest-v1.10-fit-redact.json`, `reasons` version constant, `docs/reasons.md`, `tests/unit/generation/test_statistical_redacted.py`, `CHANGELOG.md` (7) | about 200 | AT-12; full engine suite (registry) |
| R | engine | release request to claudeorchestrator after E1 and E2 merge | | |
| C1 | CLI | `pyproject.toml`, `uv.lock`, `cli/fit.py`, `tests/e2e/test_fit_command.py`, `tests/sentry/test_fit_value_redaction.py`, `tests/sentry/test_sentry_manifest.py`, `docs/cli-reference.md`, `CHANGELOG.md` (8) | about 260 | AT-C1 to AT-C5, AT-C8; full CLI suite (pin bump) |
| C2 | CLI | `cli/fit.py`, `cli/explain.py`, `tests/e2e/test_fit_command.py`, `tests/e2e/test_explain_command.py`, `docs/cli-reference.md`, `CHANGELOG.md` (6) | about 150 | AT-C6, AT-C7; T1 for the touched files plus `tests/sentry` |

Order: S1 of QA-SCAN-REDACT merges first (E1 imports its module). E1, then E2, then R. C2 needs no engine
change and can go any time; C1 waits for R. C1 and C2 both edit `fit.py` in different blocks (C1: `:398-422`
and the success output; C2: the D0 tail and `:478-484`); the second to land rebases. CLI-QA1 also edits
`explain.py` (ticket ids, `keys` topic) but not the DP topic; the second to land rebases. CLI user text in C1
and C2 carries no ticket ids (CLI-QA1 AT-S2).

## 13. Pre-mortem

- **A sensitive numeric column the user meant to generate exactly now generates over a wider range.** Stated
  in the success line; `--include-values` restores exact bounds.
- **The profiler is slow on a wide CSV.** It runs once per fit on the frame `fit` already holds; `decoy scan`
  pays the same cost on the same data. If it shows up, pass the scan sampler's cap (F3).
- **The predicate misses a column** (a repeating name column, mid-cardinality): its values stay, as in
  QA-SCAN-REDACT. AT-14 keeps both artifacts consistent, so any predicate fix lands in both at once.
- **Small-n integer histograms still show spacing.** A 10-row id column re-binned over `[0, 11]` shows about
  one value per bin. That is what any histogram shows; exact cells never appear as numbers, and the DP path
  exists for stronger guarantees. Recorded, not blocking.
- **A redacted snapshot is mistaken for a DP release.** The DP verifier requires `dps-marginal/v3` and a `dp`
  block; a `v1` file fails that check before the shape test in `_checks_dp_carrier.py`.
- **The CLI's early DP check drifts from the engine's.** It calls the same function; there is nothing to
  drift.
- **E1 lands without E2.** A redacted categorical column with `allow_real_categories: true` would generate the
  string `[redacted]`. Not a leak; R waits for both, and C1 waits for R.

## 14. Follow-ups (recorded, not in V1)

- F1: platform: if it ever writes a distribution snapshot for users, route it through
  `distribution_snapshot_document`.
- F2: a display-time `redact_snapshot_dict` for loaded snapshot files, if a CLI command ever prints one.
- F3: profile a sample for very wide or long frames, using the same sampler `decoy scan` uses.
- F4: rename `_numeric_stats` to a public name when `snapshot.py` next drops below 600 lines.
- F5: an `explain` topic for `fit` and `snapshot` (DI QA language audit); CLI-QA1 or a later docs pass.
