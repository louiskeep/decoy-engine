Status: plan

# QA-FIT-REDACT and QA-DP-GATED: fit snapshots withhold sensitive values; DP refusal says what to do (roadmap rows 24f, 24g)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-08. Scope: engine + CLI, V1 blocker for 24f (leak
class), V1 usability for 24g. Revision 2.

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

Revision 2 folds in Dennis round 1 (BLOCK, 4 blockers, 8 should, 10 nits, at `6f51c6ac`).

| Finding | Disposition |
|---|---|
| B1 float grid math lands on the real min or max (7% of decimal-grid inputs) | Accepted. Grid math in `Decimal` from the `_round`ed value, outward step until strictly outside, unit floor `1e-12`; fixed AT-4 rows and a decimal-grid strategy (section 3, AT-4) |
| B2 E2 refuses the `--include-values` escape hatch | Accepted. The marker clause applies only when `value_policy` is not exactly `"raw"`; placeholder clause stays unconditional; new AT-12 and AT-C5 cells |
| B3 second rule enum and a re-implemented select-and-stamp loop | Accepted. One `Rule` enum gains the snapshot members; `value_sensitive_names` and `stamp_value_policy` are extracted in `_value_redaction.py` and called by both artifacts |
| B4 integer and constant columns rebuilt exactly from the histogram | Accepted. Edges stay on the unit grid; integer-valued columns get the SDC minimum-frequency rule with single-bin fallback; AT rows for 1001..1010 (int and float with NaN) and constant 7 |
| S1 early DP check reverses the pinned name-check ordering | Accepted, option (a): header-only read, name checks, gate, then the full read |
| S2 C2 hint claims redaction before C1 ships | Accepted. C2 lands after C1 |
| S3 joint cells stay sorted by the real sensitive value | Accepted. Re-sort after redaction; relabel-invariance AT-7b |
| S4 fidelity gate does read the pinned file | Accepted. Claim corrected; AT-15 pins the behavior; scoring on bins is F6 |
| S5 profiling cost, Guarantee 2 overclaims | Accepted. `profile_row_cap` (CLI passes 10,000, scan's default) and Guarantee 2 reworded |
| S6 AT-14b misses `field.get("pii_score")` | Accepted. The three names are forbidden as any string constant |
| S7 no runtime default-deny in the snapshot path | Accepted. Unknown keys in a sensitive column's entry become `[redacted]` |
| S8 E2 file list incomplete | Accepted. `consumer-contract.json` and `test_reason_registry.py` added |
| NIT 1 to 10 | Accepted: anchors (S1 module 312 lines; `load_spec` at `_checks.py:378`), check before HC-5, finite count, Safe Harbor 89+ deviation stated, overflow guard, AT-11 bin rule, marker sweep, withheld-line wording, E1 file cap (CHANGELOG moves to E2), R re-pins the certified fingerprint row |

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
2. The set of value-sensitive columns is decided by the one engine predicate S1 introduced, through the same
   S1 helper that names them in a scan document. On the same profiled rows (the engine default profiles the
   whole frame; the CLI profiles the first 10,000 rows, which is `decoy scan`'s default), both artifacts list
   the same columns.
3. One engine module decides, for both artifacts, which fields hold values: `_value_redaction.py`. A new stats
   key or a new snapshot kind fails CI until it is classified.
4. Numeric and datetime sensitive columns still generate (`type: statistical`) from a redacted snapshot. A
   categorical sensitive column, or a `condition_on` joint through one, is refused at validate and run with a
   typed code that says what to do; `allow_real_categories: true` does not override it.
5. On an uncertified host, `decoy fit --epsilon` says in one plain line that DP fit is not available on this
   install, gives the next step, and refuses before the CSV is read. Exit code stays 3; the JSON `code` stays.
6. `compute_distribution_snapshot` is unchanged (in-memory callers: fidelity reports, the fidelity gate).
   Existing `distribution-snapshot/v1` files keep loading. The fidelity gate does read a pinned snapshot file
   (`generation/_fidelity_gate.py:139-144`); for a redacted numeric column it reports the column as not
   comparable (`no_quantiles`), which AT-15 pins.

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
| F-4 | Coarsened numeric bounds still equal a real min or max | exact `Decimal` grid math plus an outward step; AT-4 fixed rows (4.23/6.56/42, 94.0/99.8/39) and a decimal-grid property |
| F-4b | An integer or constant column is rebuilt exactly from the bins | minimum-frequency merge for integer-valued data; AT-4b |
| F-5 | Generation emits `[redacted]` strings as categories | AT-12 `load_spec` refusal, also with `allow_real_categories: true` |
| F-6 | Redaction breaks numeric or datetime generation | AT-3, AT-5 sample through `sample_column` |
| F-7 | `--include-values` used silently in CI | stderr warning naming columns and path; AT-C2 |
| F-8 | The CLI keeps calling `compute_distribution_snapshot` for output | AT-C8 AST guard |
| F-9 | DP refusal still reads the CSV body first, or hides the engine detail entirely | AT-C6 (a `read_csv` spy sees only the `nrows=0` header read; `--verbose` and JSON carry the engine message) |
| F-11 | The `--include-values` escape hatch is refused by E2 | AT-12 and AT-C5 raw-policy cells |
| F-12 | Joint cell order still follows the real values | re-sort after redaction; AT-7b relabel invariance |
| F-10 | The CLI copies the certified profile (Python version, platform) into its own text | AT-C6 asserts the human line has no version or fingerprint; the detail comes only from the engine message |

## 2. Established solutions

- **Statistical disclosure control, global recoding and top/bottom coding** (Hundepool et al., *Statistical
  Disclosure Control*, Wiley 2012, sections 3.4.2 to 3.4.3; R `sdcMicro::globalRecode` and `topBotCoding`;
  US Census PUMS top-coding of income): a continuous variable is released over coarse, data-independent
  intervals and its extremes are replaced by a coding bound. We recode a sensitive numeric column onto a
  power-of-ten grid at the histogram's own bin resolution and replace min and max with grid bounds outside
  the data.
- **HIPAA Safe Harbor, 45 CFR 164.514(b)(2)(i)(C)**: all elements of dates except year are removed. A
  sensitive datetime column keeps its per-year counts and has min and max reduced to year bounds. Deviation:
  Safe Harbor also folds ages over 89 (and the years that imply them) into one 90-or-older group. The snapshot
  cannot tell a birth date from any other date, so years are kept as they are; top-coding birth years is F7.
- **SDC minimum-frequency (threshold) rule** (Hundepool et al. 2012, chapter 5; `sdcMicro` and tau-Argus
  primary suppression): a released cell must hold at least *k* contributors. A sensitive integer-valued
  column's bins are merged until every non-empty bin holds at least 3 rows and spans more integers than it holds
  rows, so no bin pins down which integers are present.
- **Heckbert, "Nice numbers for graph labels" (Graphics Gems, 1990)**: axis bounds snapped to 1, 2, 5 times a
  power of ten. We use the simplest form (powers of ten only) so the rule is one line and deterministic.
- **The S1 pattern** (classification registry plus default-deny serializer: Google Cloud DLP
  `include_quote`, ydata-profiling `sensitive=True`, Presidio `replace`): already cited for
  `_value_redaction.py`. Categorical values become the same `[redacted]` placeholder, counts kept; the flag is
  `--include-values` for the same reason as `scan`.
- **Precedent in this engine**: the DP numeric release writes `mean: null`, `std: null`, `quantiles: {}`
  (`quality/dp.py:405-418`), and fidelity already handles that shape (`quality/fidelity.py:370-377`,
  `no_quantiles`, with KS on the bins). A redacted numeric entry uses the same shape.

The new module cites SDC recoding, the threshold rule, Safe Harbor and Heckbert in its docstring and gets a
`docs/methodology-registry.yaml` entry; `_value_redaction.py`'s entry gains the snapshot registry note.

## 3. Design notes

### Choke point and registry (the one decision point)

The task requires that fit reuse the redaction decision point of QA-SCAN-REDACT rather than add a second copy.
How:

1. **Which columns** come from one S1 helper. E1 extracts the selection loop of `redact_profile_dict`
   (`_value_redaction.py:288-306` at `08788ee6`) into `value_sensitive_names(fields) -> list[str]` and the
   marker write into `stamp_value_policy(doc, names, *, include_values)`. `redact_profile_dict` and the snapshot
   function both call them, so the two artifacts select and stamp by construction, not by a parity test. The
   snapshot function runs `profile_columns(df.head(profile_row_cap) if profile_row_cap else df, source_label)`
   and passes the result's `fields` to the helper. No threshold, detector list or uniqueness rule is restated.
   AT-14 checks the result against a scan document; AT-14b forbids `_snapshot_values.py` from naming
   `pii_score`, `detector_matches` or `is_likely_unique` in any string constant or attribute, or holding a
   numeric literal threshold.
2. **Which fields are values** lives in the same file as `VALUE_FIELDS`. `_value_redaction.py` gains
   `SNAPSHOT_VALUE_STATS` and `SNAPSHOT_VALUE_FREE_STATS`, keyed by `(scope, key)`, beside the profile
   registries, and its one `Rule` enum gains the four snapshot members. There is no second enum. The marker keys (`value_policy`, `redacted_columns`), the policy values
   (`flagged-redacted/v1`, `raw`) and the placeholder `[redacted]` are the S1 constants, imported, never
   redefined. One file answers "which fields of a Decoy artifact are cell values"; a reviewer reads one diff
   to add a field to either artifact.
3. **How a value is removed** (mechanics) lives in `profiling/_snapshot_values.py`, because a snapshot entry
   needs the data to re-bin (a profile entry does not). It applies the registry, never decides membership. As
   in S1's `_apply`, a key in a sensitive column's entry that neither registry names becomes `[redacted]`
   (default-deny at runtime, not only in the CI sentry).

```python
# _value_redaction.py: the existing Rule enum (NULL, PLACEHOLDER, ...) gains four members
class Rule(enum.Enum):
    ...                              # S1 members unchanged
    COARSE_RANGE = "coarse_range"    # min, max, bin_edges, bin_counts re-derived on a coarse grid
    EMPTY_MAP = "empty_map"
    YEAR_START = "year_start"
    YEAR_END = "year_end"

SNAPSHOT_VALUE_STATS: Final[Mapping[tuple[str, str], Rule]] = MappingProxyType({
    ("numeric", "min"): Rule.COARSE_RANGE,
    ("numeric", "max"): Rule.COARSE_RANGE,
    ("numeric", "bin_edges"): Rule.COARSE_RANGE,
    ("numeric", "bin_counts"): Rule.COARSE_RANGE,  # re-binned on the new edges
    ("numeric", "mean"): Rule.NULL,
    ("numeric", "std"): Rule.NULL,                 # with the mean, fixes both cells of a 2-row column
    ("numeric", "quantiles"): Rule.EMPTY_MAP,
    ("top_value", "value"): Rule.PLACEHOLDER,
    ("datetime", "min"): Rule.YEAR_START,
    ("datetime", "max"): Rule.YEAR_END,
    ("joint_cell", "key"): Rule.PLACEHOLDER,       # only the sensitive side's position
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
    profile_row_cap: int | None = None,
) -> dict[str, Any]:
    """The snapshot JSON to write for `df`: the raw snapshot with sensitive columns' values withheld."""
```

Steps: reject a non-bool `include_values` (S1 rule); compute the raw snapshot with the same arguments (defaults
taken from `quality/snapshot.py` constants, not retyped); profile the frame (or its first `profile_row_cap`
rows); `names = value_sensitive_names(profile.fields)`; for each sensitive column apply the rules for its kind;
for each joint replace the key position of each sensitive side and re-sort; `stamp_value_policy(doc, names,
include_values=...)` (with `include_values=True` the raw snapshot plus `value_policy: raw` and the names). The
raw snapshot is never mutated in place after it is shared: the function owns it.

### Numeric coarsening (global recoding)

For a sensitive numeric column with finite `min` = lo and `max` = hi, and `bins` = the snapshot's bin count:

All arithmetic is in `decimal.Decimal`, starting from the `_round`ed `lo` and `hi` as decimal strings, so a
decimal-grid value such as 4.23 is exactly 4.23 (binary floats put 4.23/0.01 at 423.00000000000006, and the
float version of this rule wrote the real min in 7% of decimal-grid inputs):

```
w     = (hi - lo) / bins            if hi > lo else max(|lo|, 1)
m     = max(|lo|, |hi|, 1)
unit  = max(10 ** floor(log10(w)), 10 ** (floor(log10(m)) - 12), 1e-12)   # Decimal(1).scaleb(e)
        integer-valued data: unit = max(unit, 1)
lo'   = (lo / unit).to_integral(ROUND_FLOOR) * unit - unit
hi'   = (hi / unit).to_integral(ROUND_CEILING) * unit + unit
while _round(float(lo')) >= lo: lo' -= unit   # the written float, not the Decimal, must be outside
while _round(float(hi')) <= hi: hi' += unit
if lo >= 0: lo' = max(lo', 0)                  # a non-negative column stays non-negative
k     = ceil((hi' - lo') / unit / bins)        # bin width is a whole number of units, at least one
bins' = ceil((hi' - lo') / (k * unit)); hi' = lo' + bins' * k * unit
```

"Integer-valued" means an integer dtype, or a float column whose finite values are all whole (pandas reads an
integer column with a blank cell as float64). `log10` runs on a `Decimal`, so a `±1e308` range cannot overflow;
a non-finite `w` (an infinite bound already filtered by `_numeric_stats`) falls back to one bin.

Then the column's stats are rebuilt by `_numeric_stats(series, bins=bins', domain=(lo', hi'))`
(`quality/snapshot.py:401`, whose `domain` parameter has had no caller since the DPS Scope A removal), so
coercion, finite filtering, rounding and the histogram are the snapshot's own code, and `sum(bin_counts)`
equals the finite count. `mean` and `std` become `null` and `quantiles` becomes `{}` (the DP numeric shape).

**Integer-valued columns, minimum frequency.** After binning, adjacent bins are merged (left to right, a
too-small bin joins its right neighbour, the last joins its left) until every non-empty bin holds at least 3
rows and spans more integers than it holds rows. If no split satisfies that, the column is one bin over
`[lo', hi']`. Edges stay on the unit grid, so the merged edges are still multiples of `unit`. A dense id column
1001..1010 therefore becomes one bin over `[1000, 1011]`, and a constant 7 becomes one bin over `[6, 8]`;
neither file shows which integers occur. The merge uses `_numeric_stats` output only (edges and counts) and
is one function in `_snapshot_values.py`.

What a reader learns: each extreme to within one bin, and which grid bins hold at least 3 rows. A column with
no finite value (`min: null`) is already value-free and is left alone.

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
  `columns[1]` is; counts, `cell_count` and `other_count` stay. `_joint_snapshot` sorts cells by
  `(-count, key[0], key[1])` (`snapshot.py:576`), so after replacement the cells are re-sorted by
  `(-count, the unredacted side)` (by `-count` alone when both sides are redacted, where tied entries are
  identical). Otherwise the cell order would still follow the alphabetical order of the real names, and a
  reader holding the roster could link each person to the other column. Top values need no re-sort: after
  replacement, tied entries are identical.

### Generation refusal (E2)

`load_spec` (`_spec.py:221`) gains one check, placed before the HC-5 requires-real check (`_spec.py:355-364`)
and the `allow_real_categories` check, so the more useful message wins:

- the column's kind is `categorical`, not `dp_verified`, and either any `top_values[*].value` equals
  `[redacted]` (always), or the snapshot's `value_policy` is not exactly `"raw"` and its source column is in
  `redacted_columns`, or `redacted_columns` is present and is not a list of strings (fail closed); or
- `condition_on` is set and either any joint cell key equals `[redacted]`, or `value_policy` is not exactly
  `"raw"` and either joint column is in `redacted_columns`.

A `--include-values` snapshot (`value_policy: "raw"`) lists the sensitive names but holds real values, so it
is not refused here and falls through to the ordinary `allow_real_categories` check, which is the escape hatch
the message advertises. A missing or unknown `value_policy` beside the marker still refuses.

Code: new `plan.statistical_snapshot_values_redacted`. Message: "statistical column 'X': the snapshot withheld
the real values of source column 'Y' (a sensitive column), so there are no categories to sample. Use a faker
or pattern generator for this column, or re-fit with `--include-values` and set `allow_real_categories:
true` (the snapshot then holds real values)." Reading the marker to refuse is safe; it is never read to grant
anything: `"raw"` only removes a refusal this check would add, and the opt-in that follows is the user's
(the `_spec.py` non-inference rule). `plan/_checks.py:378` calls `load_spec`, so validate reports the same
code.

### CLI fit (C1)

- `fit.py:422` calls `distribution_snapshot_document(df, source_label=source.name, include_values=...,
  joint_columns=..., profile_row_cap=10_000)` instead of `compute_distribution_snapshot`. 10,000 rows from the
  head is `decoy scan`'s default sample (`scan.py:616-619`, `--strategy head`, `--rows 10000`), so `fit` pays
  the same profiling cost as a default scan and flags the same columns on the same rows.
- New option `--include-values` ("Keep real values of sensitive columns in the snapshot. The file then holds
  real data."). With `--epsilon` it is a usage error (exit 1) in the existing D0 block, before the CSV is read.
- Human success output adds one line naming the sensitive columns whose entry changed in the file (a
  sensitive freetext column, which holds no values, is not named): `values withheld for 3 sensitive
  column(s): full_name, patient_id, ssn (counts kept; numbers rounded, dates to the year)`, and a hint: `to sample these as categories, re-run with --include-values; the file
  then holds real data.` With `--include-values`, a stderr warning: `warning: <path> holds real values of 3
  sensitive column(s): ... Do not share or commit it.`
- JSON success payload gains `value_policy` and `redacted_columns`.
- The CLI reads `redacted_columns` from the document; it never calls the predicate.

### CLI DP message (C2)

- DP mode reads the CSV header only (`pd.read_csv(source, dtype=str, keep_default_na=False, nrows=0)`), runs
  the existing column-name checks against it, then calls
  `decoy_engine.quality.dp_provenance.check_fit_environment()`, and only then reads the body. The pinned
  ordering "name checks fire before the proof gate" (`test_phantom_number_column_reports_missing_before_proof_gate`
  and the `dp_columns_omitted` and phantom-flag tests in `tests/e2e/test_fit_command.py`) still holds, and no
  data row is read on an uncertified host. `fit_dp_snapshot` still calls the gate too. Tests that stub only
  `fit_dp_snapshot` (boxing capture, carrier, budget, `TestFitDpArtifactShape`) also stub the early gate; C2
  lists them.
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
| integer id column 1001..1010 | every id | one bin `[1000, 1011]` with count 10 |
| datetime sensitive column | exact first and last instants | year bounds; `year_bins` unchanged |
| non-sensitive column (`status`, `diagnosis`) | raw | unchanged, byte for byte |
| `type: statistical` on a redacted numeric or datetime column | n/a | generates inside the coarse bounds |
| `type: statistical` on a redacted categorical column | n/a | validate and run fail with `plan.statistical_snapshot_values_redacted` and the two next steps |
| same column, snapshot re-fit with `--include-values`, `allow_real_categories: true` | n/a | loads and samples real categories (escape hatch) |
| a snapshot written before this change | loads | loads (no marker, no placeholders: unchanged behavior) |
| `decoy fit --epsilon ...` on an uncertified host | engine repr and fingerprint, exit 3, CSV read first | plain line plus next step, exit 3, header read only; detail under `--verbose` and in JSON |

## 5. Enumerations (from the code at engine `942dae2d`, CLI `de55dab5`)

### 5.1 Exit paths (CLI fit)

| Path | Code | Change |
|---|---|---|
| usage errors in D0 (`fit.py:337-395`) | 1 | adds `--include-values` with `--epsilon` |
| unreadable CSV, missing `--parse-dates` column (`:399-419`) | 1 | none |
| DP uncertified (`:478-484`) | 3 | moved after the header read and name checks, before the body read; new human text |
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
| `quality/report.py` fidelity (in memory) | quantiles or KS on bins | not reached by files |
| `generation/_fidelity_gate.py:139-144` | the pinned snapshot file | a redacted numeric column is `no_quantiles`, not comparable (`fidelity.py:370-377`); the gate skips it. AT-15 pins that the column is reported as not comparable rather than scored; scoring on bins is F6 |

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
| 1 | Value-bearing output | raw cells in fit snapshots (R1); exact extremes from float grid math; integer ids rebuilt from bins | `distribution_snapshot_document` over `SNAPSHOT_VALUE_STATS`; `Decimal` grid; minimum-frequency merge | AT-1, AT-2, AT-4, AT-4b, AT-11, AT-C1 | M1: move `("top_value", "value")` to the value-free set (AT-1, AT-11 red); M1b: compute the grid in floats (AT-4 fixed rows red); M1c: skip the merge (AT-4b red) |
| 2 | Cleanup missing on an exit path | N/A: no temp files or handles; the write stays on `write_local_target` | N/A | N/A | N/A |
| 3 | State and ordering | DP refusal after the CSV read; redaction mutating the raw snapshot a caller holds | header read, name checks, then `check_fit_environment`, then the body read; the function owns its raw snapshot | AT-C6 (`read_csv` spy); existing name-check-first tests; AT-9 | M3: move the gate after the body read (AT-C6 red) |
| 4 | Route parity and silent fallback | the CLI keeps `compute_distribution_snapshot`; a second predicate or stamp in the snapshot path | AST guard on `src/decoy`; one `value_sensitive_names` / `stamp_value_policy` pair | AT-C8, AT-14, AT-14b | M4: in `_snapshot_values.py`, add `field.get("pii_score", 0) >= 0.5` (AT-14b red) |
| 5 | Referential integrity | joints keep pairs; a redacted side must not feed `condition_on`; cell order must not encode the real side | key-position redaction, re-sort, E2 refusal | AT-7, AT-7b, AT-12 | M5: drop the joint clause of the E2 check (AT-12 joint cell red); M5b: skip the re-sort (AT-7b red) |
| 6 | Untrusted producer | a hand-edited snapshot: placeholders without a marker, an ill-typed `redacted_columns`, a marker with no or an unknown `value_policy`; an unknown stats key in a sensitive column | refusal on placeholder always and on the marker unless the policy is exactly `"raw"`; runtime default-deny | AT-12 cells, AT-11b | M6: refuse only on the marker (AT-12 placeholder cell red); M6b: treat a missing policy as raw (AT-12 red); M6c: keep unknown keys (AT-11b red) |
| 7 | Check then use on a path (TOCTOU) | N/A: no check-then-open added; the CSV is read once | N/A | N/A | N/A |
| 8 | Weak tests and overclaimed evidence | a fixture that never fills a value slot; a coarse bound that happens to equal a cell; a property that never draws decimal-grid values | AT-11 precondition (raw holds the canary); AT-4 fixed rows and a decimal-grid strategy | AT-4, AT-11 | M8: drop the `- unit` / `+ unit` terms and the outward loops (AT-4 grid-aligned row red) |
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
  `len(bin_edges) == len(bin_counts) + 1`; `sum(bin_counts)` equals the finite count; `mean is None`,
  `std is None`, `quantiles == {}`.
- **AT-3** Numeric generation: `load_spec` plus `sample_column` over the redacted snapshot draws 1000 values
  all inside `[min, max]`; an integer column draws `int`s.
- **AT-4** Coarse-domain table and property. Fixed rows: `(4.23, 6.56, 42)` and `(94.0, 99.8, 39)` (the float
  rule wrote the real min and max here); a constant float column; negative-only range; a range crossing 0;
  `lo == 0` (floor holds, no negative bound); `1e-9` scale; `1e15` scale; `(1e6 + 0.1, 1e6 + 0.1 + 1e-9)`
  (range tiny against magnitude); `±1e308` (no overflow); values on the grid. Hypothesis over finite floats
  and over a decimal-grid strategy (`Decimal` with 0 to 4 places, converted with `float(str(d))`), bins 1 to
  50. Property, on the written floats: `lo' < lo` unless `lo >= 0` and `lo' == 0`; `hi' > hi`; every edge is a
  multiple of `unit`; each bin width is a whole number of units, at least one.
- **AT-4b** Integer minimum frequency: ids 1001..1010 as `int64` and as `float64` with one NaN give one bin
  `[1000, 1011]` with count 10; a constant `int` 7 (20 rows) gives one bin `[6, 8]`; a 5,000-row integer
  column with repeats keeps several bins, each holding at least 3 rows and spanning more integers than rows;
  no edge is off the unit grid.
- **AT-5** Datetime: `admitted` from `1991-06-01` for 20 days: `min == "1991-01-01T00:00:00"`,
  `max == "1991-12-31T23:59:59"`, `year_bins` equal the raw ones; no `1991-06` canary; sampling stays in 1991.
- **AT-6** Categorical: `top_values` keep length, order and counts; every `value == "[redacted]"`;
  `other_count` unchanged; an HC-5 column keeps `high_cardinality: True`.
- **AT-7** Joints: `--joint full_name,diagnosis`-style pair: position of the sensitive side is `[redacted]`,
  the other side and all counts unchanged; a joint of two non-sensitive columns is unchanged.
- **AT-7b** Relabel invariance: replace the sensitive column's values with a different set of the same
  multiplicities (names permuted against `diagnosis`); the document is byte-identical.
- **AT-8** Non-sensitive columns (`status`, a non-unique numeric) are equal to the raw snapshot's entries.
- **AT-9** `include_values=True` equals `compute_distribution_snapshot(...)` with the same arguments plus
  `value_policy: "raw"` and the sensitive names; the raw snapshot passed through is not mutated.
- **AT-10** Registry sentry: a fixture frame that produces every kind, a joint and an HC-5 column; every key
  at every scope is in exactly one of `SNAPSHOT_VALUE_STATS` and `SNAPSHOT_VALUE_FREE_STATS`; the kinds seen
  equal `SNAPSHOT_KINDS`, and `SNAPSHOT_KINDS` equals the kind literals an AST read of `snapshot.py`'s
  `_stats_for` returns plus the `"empty"` assignment in `_column_snapshot`. Bite proofs: monkeypatch
  `_numeric_stats` to add a key (red: unclassified), and an AST fixture with a sixth kind literal (red).
- **AT-11** Per-entry canary: for each `SNAPSHOT_VALUE_STATS` entry, a fixture column whose raw snapshot holds
  a canary in that slot (precondition asserted), and the document does not. `bin_counts` cannot hold a canary,
  so its entry is checked as: edges on the unit grid and the same total.
- **AT-11b** Runtime default-deny: monkeypatch `compute_distribution_snapshot` to add an unknown stats key
  holding a canary to a sensitive column and to a non-sensitive column; the sensitive one becomes
  `[redacted]`, the other is unchanged.
- **AT-13** Determinism: two calls on the same frame give identical `json.dumps(..., sort_keys=True)` bytes.
- **AT-14** Parity: for the S1 canary frame and the QA frame, `redacted_columns` of the snapshot document
  equals `redacted_columns` of `profile_document(profile_columns(df, label))`; with `profile_row_cap=3` on a
  frame whose sensitivity changes after row 3, it equals the scan document of `df.head(3)`.
- **AT-14b** Structural guard: `_snapshot_values.py` calls `value_sensitive_names` and `stamp_value_policy`,
  and its AST holds no string constant or attribute named `pii_score`, `detector_matches`,
  `is_likely_unique`, `value_policy` or `redacted_columns` (the last two come only from the S1 constants), and
  no float literal in `[0, 1]` outside `_round` calls. `redact_profile_dict` calls the same two helpers
  (AST check in the same test).
- **AT-15** Fidelity gate: a generate run whose statistical numeric column uses a redacted snapshot reports
  that column as not comparable with reason `no_quantiles`; it is neither scored nor dropped from the
  report.

Engine E2 (`tests/unit/generation/test_statistical_redacted.py`):

- **AT-12** `load_spec` raises `statistical_snapshot_values_redacted` for: a categorical column in
  `redacted_columns` with and without `allow_real_categories: true` (policy `flagged-redacted/v1`, policy
  missing, policy `"other"`); a categorical column with a `[redacted]` value and no marker; a `[redacted]` value
  under policy `"raw"`; `redacted_columns` set to a string; `condition_on` through a joint with a redacted side;
  an HC-5 redacted column (this code, not the HC-5 requires-real code). Loads: a redacted numeric and datetime
  column; a policy-`"raw"` snapshot listing the column, with `allow_real_categories: true`; a non-sensitive
  categorical column of a redacted document, with `allow_real_categories: true`. The same config through the
  plan compiler reports the same code at validate (`plan/_checks.py:378`).

CLI C1 (`tests/e2e/test_fit_command.py`, `tests/sentry/test_fit_value_redaction.py`):

- **AT-C1** QA repro end to end: `decoy fit patients.csv`: the file, stdout and stderr hold no source name,
  SSN or id; stdout names the withheld columns; exit 0.
- **AT-C2** `--include-values`: the file holds the values; stderr warning names the columns and the path.
- **AT-C3** `--include-values --epsilon 1 --delta 1e-6 --dp-text x` with a nonexistent CSV: exit 1 usage error.
- **AT-C4** `--json` success payload has `value_policy` and `redacted_columns` and no source value.
- **AT-C5** fit then `decoy validate` on a generate config: a `type: statistical` numeric sensitive column
  passes; a categorical sensitive column fails with `statistical_snapshot_values_redacted` and the hint text;
  after re-fitting with `--include-values`, the same column with `allow_real_categories: true` passes.
- **AT-C8** AST guard: no module under `src/decoy` calls `compute_distribution_snapshot`.

CLI C2 (`tests/e2e/test_fit_command.py`, `tests/e2e/test_explain_command.py`):

- **AT-C6** With `check_fit_environment` patched to raise each of the two codes, a real CSV and a spy on
  `pd.read_csv`: exit 3; the only `read_csv` call has `nrows=0`; human stderr has the plain line and the hint
  and no `fingerprint`, `PlatformTriple`, or digit run of 8 or more; `--verbose` adds the engine message;
  `--json` has `code`, `message`, `hint`. A phantom `--dp-number` column still exits 1 before the gate.
- **AT-C7** `decoy fit --help` says DP runs only on a certified host; `decoy explain differential-privacy`
  summary and first paragraph say so; `decoy explain --json` lists the topic.

## 8. Owner questions

None. Recommended options taken without asking (V1 bar): redact by the S1 predicate rather than every column
(same as OQ1 of QA-SCAN-REDACT); recode numeric and datetime rather than drop them, so generation still works;
apply the SDC threshold rule (3 rows) to integer-valued columns rather than accept per-integer bins; refuse
categorical generation rather than sample placeholders; keep `--epsilon` visible but say it is gated rather
than hide it.

## 9. Reuse inventory

| Need | Reused | Not reused, why |
|---|---|---|
| Which columns | `value_sensitive_names` (extracted from S1's `redact_profile_dict`, over `is_value_sensitive`) and `profile_columns` | no snapshot-side rule |
| Marker keys, placeholder, policy values, stamping | S1 constants and `stamp_value_policy` in `_value_redaction.py` | |
| Rule vocabulary | S1's `Rule` enum, four members added | no second enum |
| Raw snapshot | `compute_distribution_snapshot` | |
| Re-binning | `_numeric_stats(domain=...)` (`snapshot.py:401`) | no second histogram |
| Defaults | `snapshot.py` `_DEFAULT_*` constants | not retyped |
| Refusal plumbing | `StatisticalSpecError`, `reasons.plan` | |
| DP gate | `check_fit_environment` | the CLI does not restate certified rows |
| CLI error output | `_emit_error` (`fit.py:302-324`) | |

## 10. Placement

| Item | Where | Size | Why there |
|---|---|---|---|
| snapshot registry, four `Rule` members, `SNAPSHOT_KINDS`, `value_sensitive_names`, `stamp_value_policy` | engine `profiling/_value_redaction.py` | +55 (312 on S1 at `08788ee6`, about 370 after) | the one file that decides which artifact fields are values and how the marker is written |
| `distribution_snapshot_document`, `Decimal` coarse domain, minimum-frequency merge, per-kind rules, joint re-sort | engine `profiling/_snapshot_values.py` (new) | about 240 | profiling owns the sensitivity decision; `profiling/` has 8 modules with S1 (9 after); `quality/` already has 25 `.py` files, so a new module there would block, and `snapshot.py` is at 600 |
| exports | `profiling/__init__.py`, `decoy_engine/__init__.py` | +4 | public surface for the CLI |
| refusal | `generation/statistical/_spec.py` | +25 (481 to about 506) | the one place generation reads snapshot stats |
| reason code | `reasons/plan/_part3.py` | +8 (61 to 69) | the file holding the other statistical snapshot codes |
| engine tests | `tests/unit/profiling/test_snapshot_values.py`, `tests/sentry/profiling/test_snapshot_value_registry.py`, `tests/unit/generation/test_statistical_redacted.py` | about 380, 160, 150 | |
| CLI wiring | `cli/fit.py` | +35 (C1) and +30 (C2), 534 to about 600 | edits in place; no new CLI module |
| CLI text | `cli/explain.py` DP topic | about +4 net | |
| CLI sentry | `tests/sentry/test_fit_value_redaction.py` plus manifest | about 60 | |

## 11. Frozen contracts touched

- **`distribution-snapshot/v1`**: two optional top-level keys (`value_policy`, `redacted_columns`), which the
  contract allows (`compatibility-contract.md:124-126`). Redacted values use types v1 already carries
  (`null` mean and std, `{}` quantiles, string top values). Old readers ignore the keys; old files load.
- **Reason registry**: one addition, `plan.statistical_snapshot_values_redacted`, at the next minor of the
  base (1.10.0 on today's main) with fixture `tests/fixtures/f29/reason-manifest-v1.10-fit-redact.json`;
  `tests/fixtures/f29/consumer-contract.json` pins `registry_version` and moves with it (as at `a5eb66e2`);
  `registry_renumber.py` moves both at merge if another plan takes 1.10.0 first. Escalation trigger (frozen contract): E2 runs the full engine
  suite.
- **Engine public API**: one new export, `distribution_snapshot_document`; nothing removed.
- **CLI `fit --json`**: two new keys on success; the DP error payload keeps its keys, `hint` text changes.
- **CLI pin**: C1 bumps `decoy-engine` (escalation trigger: dependency lock; full CLI suite).
- **`docs/cli-reference.md`**: regenerated for `--include-values` and the help text (drift-checked by
  `tests/unit/test_cli_surface.py`).

## 12. Slices

| Slice | Repo | Files (at most 10) | Lines | Tests |
|---|---|---|---|---|
| E1 | engine | `_value_redaction.py`, `_snapshot_values.py`, `profiling/__init__.py`, `decoy_engine/__init__.py`, unit test, sentry test, `tests/unit/test_public_api.py`, `tests/sentry/test_sentry_manifest.py`, `docs/methodology-registry.yaml`, `CODEMAP.md` (10) | about 850 | AT-1 to AT-11b, AT-13, AT-14, AT-14b first; T1 = `tests/unit/profiling`, `tests/sentry`, `tests/unit/quality/test_snapshot.py`, `tests/unit/generation/test_statistical.py` |
| E2 | engine | `_spec.py`, `reasons/plan/_part3.py`, fixture `reason-manifest-v1.10-fit-redact.json`, `tests/fixtures/f29/consumer-contract.json`, `reasons` version constant, `docs/reasons.md`, `tests/unit/test_reason_registry.py`, `tests/unit/generation/test_statistical_redacted.py`, `CHANGELOG.md` (one entry for E1 and E2) (9) | about 260 | AT-12, AT-15; full engine suite (registry) |
| R | engine | release request to claudeorchestrator after E1 and E2 merge, including the re-pin of the CLI runtime row in `_CERTIFIED_STACKS` (`quality/dp_provenance.py:297-320`) for the new engine version, so 24g does not get worse | | |
| C1 | CLI | `pyproject.toml`, `uv.lock`, `cli/fit.py`, `tests/e2e/test_fit_command.py`, `tests/sentry/test_fit_value_redaction.py`, `tests/sentry/test_sentry_manifest.py`, `docs/cli-reference.md`, `CHANGELOG.md` (8) | about 260 | AT-C1 to AT-C5, AT-C8; full CLI suite (pin bump) |
| C2 | CLI | `cli/fit.py`, `cli/explain.py`, `tests/e2e/test_fit_command.py`, `tests/e2e/test_explain_command.py`, `docs/cli-reference.md`, `CHANGELOG.md` (6) | about 200 | AT-C6, AT-C7, plus the updated DP tests that stub only `fit_dp_snapshot` (boxing capture `:381-403`, carrier, budget and DpError `:648-700`, `TestFitDpArtifactShape` `:803`); T1 for the touched files plus `tests/sentry` |

Order: S1 of QA-SCAN-REDACT merges first (E1 imports its module). E1, then E2, then R, then C1, then C2. C2
needs no engine change, but its hint says sensitive columns are withheld by default, which is true only once
C1 ships. C1 and C2 both edit `fit.py` in different blocks (C1: `:398-422`
and the success output; C2: the D0 tail and `:478-484`); the second to land rebases. CLI-QA1 also edits
`explain.py` (ticket ids, `keys` topic) but not the DP topic; the second to land rebases. CLI user text in C1
and C2 carries no ticket ids (CLI-QA1 AT-S2).

## 13. Pre-mortem

- **A sensitive numeric column the user meant to generate exactly now generates over a wider range.** Stated
  in the success line; `--include-values` restores exact bounds.
- **The profiler is slow on a wide CSV.** The CLI profiles the first 10,000 rows, the same as a default
  `decoy scan`. A sensitive column whose first 10,000 rows look harmless is missed by both, consistently.
- **`dtype` inference differs between the head-only read of `scan` and fit's full read** (a column numeric in
  the first 10,000 rows with text later). The profile then runs on an `object` column where scan saw numbers;
  both still decide by the same predicate. Recorded.
- **The predicate misses a column** (a repeating name column, mid-cardinality): its values stay, as in
  QA-SCAN-REDACT. AT-14 keeps both artifacts consistent, so any predicate fix lands in both at once.
- **Small integer columns lose their shape.** The threshold rule turns a 10-row id column into one bin, so
  generation from it is uniform over `[1000, 1011]`. That is the point; `--include-values` restores exact
  bins. Larger integer columns keep several bins.
- **A non-integer column with few rows still shows where rows fall** to within one bin. Bins are at least one
  grid unit wide and hold no exact value; the DP path exists for stronger guarantees. Recorded, not blocking.
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
- F3: honour `decoy scan`'s `--strategy random` option in `fit` if users ask for it.
- F4: rename `_numeric_stats` to a public name when `snapshot.py` next drops below 600 lines.
- F5: an `explain` topic for `fit` and `snapshot` (DI QA language audit); CLI-QA1 or a later docs pass.
- F6: let the fidelity gate score a redacted numeric column by KS on its bins instead of reporting it as not
  comparable.
- F7: top-code birth years (Safe Harbor's 90-or-older group) when a column is known to be a birth date.
