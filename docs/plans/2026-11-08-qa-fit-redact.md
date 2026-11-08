Status: plan

# QA-FIT-REDACT and QA-DP-GATED: fit snapshots withhold sensitive values; DP refusal says what to do (roadmap rows 24f, 24g)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-08. Scope: engine + CLI, V1 blocker for 24f (leak
class), V1 usability for 24g. Revision 4.

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
minor). Code read at engine `main` `942dae2d`, CLI `main` `de55dab5` (`fit.py`, `explain.py` and
`tests/e2e/test_fit_command.py` unchanged at `bacc05e`), and the QA-SCAN-REDACT S1 branch
`feat/scan-redact-s1` at `1daabb74` (this plan builds on it).

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
| B2 E2 refuses the `--include-values` escape hatch | Accepted in revision 2, then superseded by revision 4 C4 and C6: refusal is marker-only and the writer emits no categorical placeholder |
| B3 second rule enum and a re-implemented select-and-stamp loop | Accepted. One `Rule` enum gains the snapshot members; `value_sensitive_names` and `stamp_value_policy` are extracted in `_value_redaction.py` and called by both artifacts |
| B4 integer and constant columns rebuilt exactly from the histogram | Accepted in revision 2, then superseded by revision 4 C1: every sensitive numeric column has one bin and no interior edge |
| S1 early DP check reverses the pinned name-check ordering | Accepted, option (a): header-only read, name checks, gate, then the full read |
| S2 C2 hint claims redaction before C1 ships | Accepted. C2 lands after C1 |
| S3 joint cells stay sorted by the real sensitive value | Accepted in revision 2, then superseded by revision 4 C6: a sensitive joint emits no cells |
| S4 fidelity gate does read the pinned file | Accepted. Claim corrected; AT-15 pins the behavior; scoring on bins is F6 |
| S5 profiling cost, Guarantee 2 overclaims | Accepted. `profile_row_cap` (CLI passes 10,000, scan's default) and Guarantee 2 reworded |
| S6 AT-14b misses `field.get("pii_score")` | Accepted. The three names are forbidden as any string constant |
| S7 no runtime default-deny in the snapshot path | Accepted. Unknown keys in a sensitive column's entry become `[redacted]` |
| S8 E2 file list incomplete | Accepted. `consumer-contract.json` and `test_reason_registry.py` added |
| NIT 1 to 10 | Accepted: anchors (S1 module 312 lines; `load_spec` at `_checks.py:378`), check before HC-5, finite count, Safe Harbor 89+ deviation stated, overflow guard, AT-11 bin rule, marker sweep, withheld-line wording, E1 file cap (CHANGELOG moves to E2), R re-pins the certified fingerprint row |

Revision 3 folds in Dennis round 2 (GO-WITH-FIXES, 3 high, 6 medium, 5 low, at `2a85e9d6`), plus one defect the
author found while redoing the numbers: with an outward step of one grid unit, an integer column's real minimum
was exactly `lo' + 1`.

| Finding | Disposition |
|---|---|
| H1 the `nrows=0` header read is a third `read_csv` site in `fit`; the CLI CSV-read ratchet pins 2 | Accepted. The exact read and the DP body read become one call site with mode-dependent arguments, so `fit` keeps 2 sites and the pin does not move; C2's T1 names `tests/sentry/test_csv_read_choke_point.py` (section 3, CLI DP message) |
| H2 joint top-K truncation still keeps the cells of the alphabetically first sensitive values | Accepted in revision 3, then superseded by revision 4 C6: a sensitive joint emits no survivors or order |
| H3 the integer rule contradicts its expected values and cannot hold for dense columns | Accepted in revision 3, then superseded by revision 4 C1: every sensitive numeric column has one bin |
| Author: the outward step pins an integer minimum | Fixed with H3: bounds sit on a grid one power of ten above the range (`G > r`), so each extreme is known only to within `G`, wider than the range itself |
| M1 the outward loop never ends near float max | Accepted in revision 3, then superseded by revision 4 C2: no loop, and a collision-free finite one-bin fallback remains sampleable |
| M2 a second rule applier in `_snapshot_values.py` | Accepted. S1's `_apply` gains `EMPTY_MAP` and takes the keep test as an argument; the snapshot path calls it for NULL, EMPTY_LIST, EMPTY_MAP and unknown keys; AT-14b asserts the call |
| M3 `value_sensitive_names(profile.fields)` passes dataclasses, which fail closed | Accepted. It takes `profile.to_dict()["fields"]`, the shape S1's redactor sees |
| M4 AT-C3 is rejected by `exists=True` before D0 runs | Accepted. A real CSV, a `read_csv` spy, and the code `dp_usage_include_values` |
| M5 private imports of `_numeric_stats` and `_round` | Accepted. E1 renames `_numeric_stats` to `numeric_stats`; revision 4 no longer imports or renames `_joint_snapshot`, and `_round` is not needed |
| M6 class 7 is not N/A once the header and body are read separately | Accepted. The body's columns must equal the header's, else usage error `dp_source_changed`; class 7 row and an AT-C6 cell |
| L1 to L5 | Accepted: anchors at `1daabb74`; AT-C8 reuses the CLI call scanner and lists spellings; floor written as `Decimal(1).scaleb(-12)`; "before the CSV body is read"; the double gate call on a certified host is noted. Revision 4 removes the threshold |

Revision 4 folds in the first Codex review against the V1 bar.

| Finding | Disposition |
|---|---|
| V1 C1 an interior numeric edge can equal a real cell, and the zero clamp writes a real zero | Accepted. Every sensitive numeric release now has one bin whose ordinary bounds are both strictly outside every finite source cell. There is no zero clamp and no interior edge. AT-4 checks integer, constant, negative, zero-crossing and grid-aligned cells (`quality/snapshot.py:401-460`) |
| V1 C2 the non-finite-bound fallback makes a near-float-max numeric snapshot degenerate, so generation fails | Accepted. When an outward bound cannot be represented as a finite float, a deterministic collision-free finite fallback domain keeps one positive-count bin and remains sampleable. AT-3 covers a constant `1.79e308` and the close range `1.70e308..1.71e308`; AT-4 calls the grid helper directly for the pre-existing raw-snapshot failure at `+/-1e308` (`quality/snapshot.py:435-460`, `generation/statistical/_sample.py:265-287`) |
| V1 C3 a datetime cell already at a year boundary survives unchanged as `min` or `max` | Accepted. Written bounds are the start of the year before the earliest year and the end of the year after the latest year; `year_bins` still drive sampling. AT-5 includes exact boundary cells (`quality/snapshot.py:487-501`, `generation/statistical/_sample.py:327-360`) |
| V1 C4 unconditional refusal on the string `[redacted]` breaks a legacy or raw snapshot whose real category has that spelling | Accepted. Refusal is marker-driven only. A markerless v1 file and a `value_policy: raw` file keep the old `allow_real_categories` behavior even when a real category has that spelling; plan-produced redacted files always carry the marker (`generation/statistical/_spec.py:367-380`) |
| V1 C5 the early environment gate preempts the engine's existing typed request checks, so `--epsilon 0` changes from exit 1 to exit 3 | Accepted. E3 extracts the data-independent request preparation already ahead of the gate in `fit_dp_snapshot` into one shared engine function and exports a check-only entry point. The CLI runs that after header name checks and before the body read, then reports request errors before environment errors exactly as the engine does (`quality/dp.py:319-345`, CLI `fit.py:474-491`) |
| V1 C6 the fixed categorical and joint placeholder can equal a real sensitive cell, while a first-unused replacement token would reveal which earlier token-shaped values were present | Accepted. Sensitive categorical `top_values` and sensitive joint `cells` become empty lists through the existing `_apply`; their complete row mass moves to `other_count`. There is no value-shaped sentinel and no value-dependent joint survivor or order. AT-6 and AT-7 include real `[redacted]` cells (`quality/snapshot.py:464-484`, `:547-585`) |
| V1 C7 runtime default-deny replaced an unknown snapshot key with the profile string sentinel, which can itself be a real cell | Accepted. The one `_apply` gains a `denied_value` argument whose profile default remains `[redacted]`; the snapshot passes `None`. AT-11b checks the null and a real `[redacted]` canary (`profiling/_value_redaction.py:248-265` at `1daabb74`) |
| V1 C8 the touched CSV read catch sends `str(exc)` to human and JSON output | Accepted. C2 replaces it with a fixed value-free read error that may name the source path but never the exception text. AT-C6 raises an exception whose string is a data canary and checks every user surface (`fit.py:412-414`) |

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
   the user passed `--include-values`. Aggregate row counts, null counts, distinct counts, kinds, year counts
   and text lengths are kept. Per-value categorical and joint counts collapse into `other_count` with their
   labels because retaining entries needs a value-shaped key.
2. The set of value-sensitive columns is decided by the one engine predicate S1 introduced, through the same
   S1 helper that names them in a scan document. On the same profiled rows (the engine default profiles the
   whole frame; the CLI profiles the first 10,000 rows, which is `decoy scan`'s default), both artifacts list
   the same columns.
3. One engine module decides, for both artifacts, which fields hold values: `_value_redaction.py`. A new stats
   key or a new snapshot kind fails CI until it is classified.
4. Numeric and datetime sensitive columns still generate (`type: statistical`) from a redacted snapshot,
   including numeric columns whose outward decimal bound is not a finite float. A
   categorical sensitive column, or a `condition_on` joint through one, is refused at validate and run with a
   typed code that says what to do; `allow_real_categories: true` does not override it.
5. On an uncertified host, a well-formed `decoy fit --epsilon` says in one plain line that DP fit is not
   available on this install, gives the next step, and refuses before the CSV body is read. Only the header is
   read for the column-name checks. A malformed DP request keeps its existing typed usage error before the
   environment error. Exit codes and JSON codes stay unchanged.
6. `compute_distribution_snapshot` is unchanged (in-memory callers: fidelity reports, the fidelity gate).
   Existing `distribution-snapshot/v1` files keep loading, including a real category whose literal spelling is
   `[redacted]`. The fidelity gate does read a pinned snapshot file
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
| F-4 | A coarsened numeric bound or interior edge equals a real cell | exact `Decimal` bounds on a grid one power of ten above the range, no zero clamp, and one bin with no interior edge; AT-4 fixed rows and a property over every finite cell |
| F-4b | Integer, constant, negative or zero-valued data is rebuilt exactly from bins or a clamped bound | one bin for every sensitive numeric column and bounds strictly outside all finite cells; AT-4b |
| F-4c | A bound near float max hangs, writes `inf`, or leaves a degenerate unsampleable histogram | no loop; a collision-free finite one-bin fallback; AT-3 and AT-4 near-max rows under a timeout |
| F-5 | A fixed placeholder equals a real sensitive cell; generation emits a plan-produced sentinel; or a reader mistakes a real legacy `[redacted]` category for a sentinel | no sentinel in a redacted snapshot, marker-driven AT-12 refusal, plus real-`[redacted]` writer and legacy load cells |
| F-6 | Redaction breaks numeric or datetime generation | AT-3, AT-5 sample through `sample_column` |
| F-7 | `--include-values` used silently in CI | stderr warning naming columns and path; AT-C2 |
| F-8 | The CLI keeps calling `compute_distribution_snapshot` for output | AT-C8 AST guard |
| F-9 | DP refusal still reads the CSV body first, hides the engine detail entirely, or preempts an existing typed request error | AT-C6 (a `read_csv` spy sees only the `nrows=0` header read; invalid request cells keep their code; `--verbose` and JSON carry the engine message) |
| F-11 | The `--include-values` escape hatch is refused by E2 | AT-12 and AT-C5 raw-policy cells |
| F-12 | Joint cell order, or which cells survive the top-K cut, still follows real values | a sensitive joint has no cells or survivors; all cell mass moves to `other_count`; AT-7b relabel invariance with more than 25 tied raw cells |
| F-13 | The CSV changes between the DP header read and the body read | body columns must equal the header's; `dp_source_changed`; AT-C6 cell |
| F-14 | The DP header read raises the CLI's CSV-read pin | one call site for the exact and DP body reads; the CSV-read sentry in C2's T1 |
| F-10 | The CLI copies the certified profile (Python version, platform) into its own text | AT-C6 asserts the human line has no version or fingerprint; the detail comes only from the engine message |
| F-15 | A CSV parser or decoder exception echoes a data value through human or JSON output | fixed read error with no `str(exc)`; AT-C6 canary exception in exact, DP-header and DP-body modes |

## 2. Established solutions

- **Statistical disclosure control, global recoding and top/bottom coding** (Hundepool et al., *Statistical
  Disclosure Control*, Wiley 2012, sections 3.4.2 to 3.4.3; R `sdcMicro::globalRecode` and `topBotCoding`;
  US Census PUMS top-coding of income): a continuous variable is released over coarse, data-independent
  intervals and its extremes are replaced by a coding bound. We replace min and max with bounds on a
  power-of-ten grid one step coarser than the column's range, so the bounds say only the range's order of
  magnitude, and release one interval between them.
- **HIPAA Safe Harbor, 45 CFR 164.514(b)(2)(i)(C)**: all elements of dates except year are removed. A
  sensitive datetime column keeps its per-year counts and has min and max reduced to year bounds. Deviation:
  Safe Harbor also folds ages over 89 (and the years that imply them) into one 90-or-older group. The snapshot
  cannot tell a birth date from any other date, so years are kept as they are; top-coding birth years is F7.
- **SDC global recoding at the coarsest release** (Hundepool et al. 2012, sections 3.4.2 to 3.4.3): a sensitive
  numeric column releases one interval and its finite-row count. This is deliberately coarser than the earlier
  minimum-frequency draft. Any interior edge can itself equal a real integer or float cell, even when its bin
  has three contributors, so V1 has no interior edge.
- **Heckbert, "Nice numbers for graph labels" (Graphics Gems, 1990)**: axis bounds snapped to 1, 2, 5 times a
  power of ten. We use the simplest form (powers of ten only) so the rule is one line and deterministic.
- **The S1 pattern** (classification registry plus default-deny serializer: Google Cloud DLP
  `include_quote`, ydata-profiling `sensitive=True`, Presidio `replace`): already cited for
  `_value_redaction.py`. The snapshot uses the same default-deny applier but empties whole value-bearing
  containers, because any string placeholder can also be a real category. Aggregate counts stay. The flag is
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

1. **Which columns** come from one S1 helper. E1 extracts the selection loop of `redact_profile_dict`
   (`_value_redaction.py:333-344` at `1daabb74`) into `value_sensitive_names(fields) -> list[str]`, which takes
   the JSON form of the profile's `fields` list (what the S1 redactor walks), and the marker write
   (`:345-350`) into `stamp_value_policy(doc, names, *, include_values)`. `redact_profile_dict` and the
   snapshot function both call them, so the two artifacts select and stamp by construction, not by a parity
   test. The snapshot function runs `profile = profile_columns(df.head(profile_row_cap) if profile_row_cap
   else df, source_label)` and passes `profile.to_dict()["fields"]`: passing `profile.fields` (dataclasses, not
   mappings) would make the fail-closed predicate flag every column. No threshold, detector list or
   uniqueness rule is restated. AT-14 checks the result against a scan document; AT-14b forbids
   `_snapshot_values.py` from naming `pii_score`, `detector_matches` or `is_likely_unique` in any string
   constant or attribute, or holding a float literal in `[0, 1]` (the predicate's thresholds are scores in that
   range).
2. **Which fields are values** lives in the same file as `VALUE_FIELDS`. `_value_redaction.py` gains
   `SNAPSHOT_VALUE_STATS` and `SNAPSHOT_VALUE_FREE_STATS`, keyed by `(scope, key)`, beside the profile
   registries, and its one `Rule` enum gains the four snapshot members. There is no second enum. The marker keys (`value_policy`, `redacted_columns`), the policy values
   (`flagged-redacted/v1`, `raw`) and the profile placeholder `[redacted]` are the S1 constants, imported, never
   redefined. The snapshot does not add another placeholder: its value-bearing categorical and joint containers
   use S1's EMPTY_LIST rule. One file answers "which fields of a Decoy artifact are cell values"; a reviewer
   reads one diff to add a field to either artifact.
3. **How a value is removed.** The rules that need no data (NULL, EMPTY_LIST, PLACEHOLDER, the new EMPTY_MAP,
   and the
   default-deny replacement of unknown keys) are applied by S1's one applier, `_apply`
   (`_value_redaction.py:248-265` at `1daabb74`). E1 gives it the EMPTY_MAP branch and makes its keep test an
   arguments: `_apply(container, rules, keep, *, denied_value=REDACTED)` where `keep(name, value)` says whether
   a key outside `rules` survives. The profile path passes S1's shape check and keeps its existing sentinel. The
   snapshot path passes membership in the scope's value-free keys and `denied_value=None`, because a JSON null
   cannot equal any non-null cell that reached a snapshot stat. Its input is the engine's own in-memory snapshot,
   not a loaded file, so there is no type to distrust. The rules that need
   the data (COARSE_RANGE, PREVIOUS_YEAR_START and NEXT_YEAR_END) live in
   `profiling/_snapshot_values.py`, because re-binning needs the series. That module applies the registry and
   never decides membership. A key in a sensitive column's entry that neither registry names becomes `null` at
   runtime, not only in the CI sentry.

```python
# _value_redaction.py: the existing Rule enum (NULL, PLACEHOLDER, ...) gains four members
class Rule(enum.Enum):
    ...                              # S1 members unchanged
    COARSE_RANGE = "coarse_range"    # min, max, bin_edges, bin_counts re-derived on a coarse grid
    EMPTY_MAP = "empty_map"          # applied by _apply
    PREVIOUS_YEAR_START = "previous_year_start"
    NEXT_YEAR_END = "next_year_end"

SNAPSHOT_VALUE_STATS: Final[Mapping[tuple[str, str], Rule]] = MappingProxyType({
    ("numeric", "min"): Rule.COARSE_RANGE,
    ("numeric", "max"): Rule.COARSE_RANGE,
    ("numeric", "bin_edges"): Rule.COARSE_RANGE,
    ("numeric", "bin_counts"): Rule.COARSE_RANGE,  # one count on the new bounds
    ("numeric", "mean"): Rule.NULL,
    ("numeric", "std"): Rule.NULL,                 # with the mean, fixes both cells of a 2-row column
    ("numeric", "quantiles"): Rule.EMPTY_MAP,
    ("categorical", "top_values"): Rule.EMPTY_LIST,
    ("datetime", "min"): Rule.PREVIOUS_YEAR_START,
    ("datetime", "max"): Rule.NEXT_YEAR_END,
    ("joint", "cells"): Rule.EMPTY_LIST,
})
SNAPSHOT_VALUE_FREE_STATS: Final[frozenset[tuple[str, str]]] = frozenset(
    _free("document", "schema_version", "row_count", "columns", "joints")
    | _free("column", "dtype", "kind", "null_count", "non_null_count", "distinct_count", "stats")
    | _free("categorical", "other_count", "high_cardinality")
    | _free("datetime", "year_bins")
    | _free("year_bin", "year", "count")
    | _free("freetext", "length", "length_bin_edges", "length_bin_counts")
    | _free("length", "min", "max", "mean", "std")
    | _free("joint", "columns", "cell_count", "other_count")
)
SNAPSHOT_KINDS: Final = frozenset({"numeric", "categorical", "datetime", "freetext", "empty"})
```

`top_values` and joint `cells` are value-bearing containers and become empty for a sensitive column or joint.
Their full mass moves to `other_count`, so aggregate counts stay. The sentry treats those terminal containers as
classified and does not require a second child registry that can never be applied. `year` is value-free by the
Safe Harbor rule. Freetext lengths describe the cells, not the cells.

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
rows); `names = value_sensitive_names(profile.to_dict()["fields"])`; for each sensitive column apply the rules
for its kind. Before emptying categorical `top_values`, add every listed count and the old `other_count` into the
new `other_count`. For each joint with a sensitive side, add every listed cell count and the old `other_count`
into the new `other_count`, then empty `cells` through `_apply`. Both paths call one local
`_collapse_counted_items(container, item_key)` helper, so aggregate-mass bookkeeping has one implementation;
`stamp_value_policy(doc, names,
include_values=...)` (with `include_values=True` the raw snapshot plus `value_policy: raw` and the names). The
raw snapshot is never mutated in place after it is shared: the function owns it.

### Numeric coarsening (global recoding)

For a sensitive numeric column with finite written `min` = lo and `max` = hi, all arithmetic is in
`decimal.Decimal`, starting from decimal strings. A decimal-grid value such as 4.23 is exactly 4.23. Binary
float arithmetic was rejected because it put a real extreme back on the grid. `floor(log10(x))` is
`Decimal.adjusted()`, so the decimal step itself does not overflow.

```
FLOOR = Decimal(1).scaleb(-12)                     # the snapshot writes 12 decimal places
r     = hi - lo                 if hi > lo else max(|lo|, 1)
m     = max(|lo|, |hi|, 1)
G     = max(10 ** (floor(log10(r)) + 1), 10 ** (floor(log10(m)) - 12), FLOOR)   # G > r
lo'   = floor(lo / G) * G - G                      # strictly below the finite data
hi'   = floor(hi / G) * G + 2 * G                  # strictly above the finite data
```

There is no zero clamp. A non-negative column that contains zero would otherwise write the real cell `0` as
its lower bound. There is also no interior edge. Even a data-independent grid edge can equal a real integer or
float cell, so a three-row bin does not satisfy the V1 promise. Every sensitive numeric column therefore has
one bin over `[lo', hi']`.

For the ordinary case, both decimal bounds must convert to finite floats, the written lower bound must be
strictly below the actual finite series minimum, and the written upper bound must be strictly above the actual
finite series maximum. The function then calls `numeric_stats(series, bins=1, domain=(lower, upper))`
(`quality/snapshot.py:401-460`, renamed from `_numeric_stats` in E1). That keeps coercion, finite filtering,
rounding and histogram counting in the snapshot's one implementation. `sum(bin_counts)` equals the finite
count. The only edges are outside every finite source cell. `mean` and `std` become `null` and `quantiles`
becomes `{}` through `_apply`.

**Finite fallback.** An outward decimal bound near `sys.float_info.max` can convert to infinity. An empty
histogram would make `sample_column` raise `statistical_stats_degenerate`
(`generation/statistical/_sample.py:265-275`), so the fallback is still one positive-count bin. Let N be the
number of finite cells. From the N+1
finite half-integers `1.5, 2.5, ..., N+1.5`, choose the first B for which neither `-B` nor `B` equals a finite
source cell. One must exist because N cells cannot cover N+1 absolute candidates. Call
`numeric_stats(series, bins=1, domain=(-B, B))`; its existing clip puts every finite contributor in that bin
(`quality/snapshot.py:435-460`). This fallback deliberately does not claim to enclose the source range. It is a
sampleable, value-free domain for a range that has no representable outward float bound. AT-4 asserts finite
edges, a positive count, no edge equal to a source cell, determinism and termination.

Worked values:

| Column | G | Bounds | Result |
|---|---|---|---|
| ids 1001..1010 (`int64`, or `float64` with one NaN) | 10 | `[990, 1030]` | one bin, count 10 |
| constant `int` 7, 20 rows | 10 | `[-10, 20]` | one bin, count 20 |
| 5,000 ages 18..90 with repeats | 100 | `[-100, 200]` | one bin, count 5,000 |
| `balance` 31337.25..98765.5 | 1e5 | `[-100000, 200000]` | one bin, finite count |
| 4.23..6.56 (42 rows) | 10 | `[-10, 20]` | one bin, count 42 |
| 94.0..99.8 (39 rows) | 10 | `[80, 110]` | one bin, count 39 |

What a reader learns is the finite-row count and, in the ordinary case, the range's coarse order of magnitude.
A column with no finite value (`min: null`) is already value-free and is left alone. The cost is fidelity: a
sensitive numeric column generates uniformly inside one coarse bin. `--include-values` restores the exact
histogram.

### Datetime, categorical, joints

- Datetime: if the earliest observed year is Y0 and the latest is Y1, `min` becomes
  `(Y0-1)-01-01T00:00:00` and `max` becomes `(Y1+1)-12-31T23:59:59`; `year_bins` stay. A real cell at the start
  or end of its year therefore cannot equal a written bound. Pandas datetime years leave room for these Python
  datetime bounds. The sampler chooses a year from `year_bins`, so the wider clamp does not add a year
  (`quality/snapshot.py:487-501`, `generation/statistical/_sample.py:327-360`).
- Categorical (including HC-5): `top_values` becomes `[]`; `other_count` becomes the sum of its old value and
  every removed entry's count, which equals `non_null_count`. `high_cardinality` stays. There is no placeholder
  that can collide with a real category, and no survivor or list order that can encode a tie-break on a real
  value. HC-5 is not special-cased and generation refuses it below.
- Joints: when either side is sensitive, `cells` becomes `[]`; `other_count` becomes the sum of its old value
  and every removed cell's count, which is the number of rows contributing to the joint. `columns` and
  `cell_count` stay because names are structure and cardinalities are counts. The wrapper uses the raw joint
  already returned by `compute_distribution_snapshot`; it does not call another crosstab, rebuild an
  untruncated list, select survivors or preserve a value-dependent order (`snapshot.py:547-585`). A joint with
  no sensitive side stays byte for byte unchanged.

### Generation refusal (E2)

`load_spec` (`_spec.py:221`) gains one marker check, placed before the HC-5 requires-real check
(`_spec.py:355-364`) and the `allow_real_categories` check, so the more useful message wins:

- the column's kind is `categorical`, not `dp_verified`, `value_policy` is not exactly `"raw"`, and its source
  column is in `redacted_columns`; or
- `condition_on` is set, `value_policy` is not exactly `"raw"`, and either named source column is in
  `redacted_columns`; or
- either marker key is present but the pair is malformed: `value_policy` is missing or not one of
  `"flagged-redacted/v1"` and `"raw"`, or `redacted_columns` is not a list of strings. A malformed marker
  refuses only a categorical source or a conditional statistical column, the paths that could sample values.

A `--include-values` snapshot (`value_policy: "raw"`) lists the sensitive names but holds real values, so it
is not refused here and falls through to the ordinary `allow_real_categories` check, which is the escape hatch
the message advertises. A markerless v1 snapshot is legacy, not evidence of redaction, and keeps the same
behavior. The literal string `[redacted]` is not a decision point: it can be a real category in an old or raw
snapshot. A missing or unknown policy beside either marker still refuses.

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
  real data."). With `--epsilon` it is a usage error `dp_usage_include_values` (exit 1) in the existing D0
  block, before the CSV is read.
- Human success output reads the marker and names every sensitive column in `redacted_columns`:
  `3 sensitive column(s) marked: full_name, patient_id, ssn (cell values withheld where the snapshot carries
  them; category and joint counts aggregated, numbers reduced to one range, dates to years)`, and a hint: `to sample these as categories, re-run with --include-values; the file
  then holds real data.` With `--include-values`, a stderr warning: `warning: <path> holds real values of 3
  sensitive column(s): ... Do not share or commit it.`
- JSON success payload gains `value_policy` and `redacted_columns`.
- The CLI reads `redacted_columns` from the document; it never calls the predicate.

### CLI DP message (C2)

- DP mode reads the CSV header only (`pd.read_csv(source, dtype=str, keep_default_na=False, nrows=0)`), runs
  the existing column-name checks against it, then calls the new public
  `decoy_engine.quality.check_dp_fit_request(...)`, and only then reads the body. E3 extracts the existing
  data-independent prefix of `fit_dp_snapshot` into one private preparation function: fit-level parameter
  validation, schema freeze and parse, derived-edge validation, and `check_fit_environment`. The public
  check-only wrapper and `fit_dp_snapshot` both call that function, so the CLI does not copy a validator or a
  certified-row rule (`quality/dp.py:319-345`, `quality/dp_provenance.py:347-379`). The pinned
  ordering "name checks fire before the proof gate" (`test_phantom_number_column_reports_missing_before_proof_gate`
  and the `dp_columns_omitted` and phantom-flag tests in `tests/e2e/test_fit_command.py`) still holds, and no
  data row is read on an uncertified host. Existing request errors still win before provenance, including
  `dp_epsilon_invalid`, `dp_delta_invalid`, `dp_numeric_bins_invalid`, carrier declarations and derived numeric
  edges (`quality/dp.py:145-216`, `:319-344`). `fit_dp_snapshot` still calls the shared preparation too, so a
  certified host walks the fingerprint twice; that costs a few milliseconds and keeps the engine's own check
  authoritative. Tests that stub only `fit_dp_snapshot` (boxing capture, carrier, budget,
  `TestFitDpArtifactShape`) also stub `check_dp_fit_request`; C2 lists them.
- **Read sites stay at two.** The CLI's CSV-read sentry (`tests/sentry/test_csv_read_choke_point.py`, baseline
  `tests/sentry/baselines/csv-read-allow.json`, which may shrink and never grow) allows `cli/fit.py::fit` exactly
  2 `read_csv` sites, and today those are the exact read (`fit.py:402`) and the DP body read (`:411`). C2 merges
  those two into one call, `pd.read_csv(source, **read_kwargs)`, with `read_kwargs` chosen by mode
  (`parse_dates=...` for exact; `dtype=str, keep_default_na=False` for DP, the comment kept). The header read
  is the second site, so the pin does not move. The header read is not routed through `_sources.read_csv_frame`:
  that helper resolves a dialect, which could name the columns differently from the raw body read the name
  checks protect.
- **The body must match the header.** After the body read, `list(df.columns)` must equal the header's columns;
  otherwise usage error `dp_source_changed` ("the CSV changed while it was being read; run the command again"),
  exit 1. Without it, a file replaced between the two reads would reach `_build_typed_frame` with names nobody
  checked and fail with a raw `KeyError`, breaking the JSON error contract.
- **Read errors are value-free.** The catch at `fit.py:412-414` no longer interpolates `str(exc)`. Human and
  JSON modes say only that the named source CSV could not be read. The path is user-written structure; parser,
  decoder and OS exception text is not copied to either surface.
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
| `decoy fit patients.csv` (QA frame) | file holds every name, SSN, id | `top_values` is empty and `other_count == 10` for all three; stdout names them; exit 0 |
| same with `--include-values` | n/a | today's file plus `value_policy: raw`, `redacted_columns`; stderr warning; exit 0 |
| `--include-values --epsilon 1 ...` | n/a | usage error, exit 1, CSV not read |
| numeric sensitive column `balance` | exact min, max, mean, quantiles | one coarse bin with both edges outside every finite cell, mean/std null, quantiles `{}` |
| integer id column 1001..1010 | every id | one bin `[990, 1030]` with count 10 |
| numeric column too near float max for outward float bounds | exact range | collision-free finite one-bin fallback that still samples |
| datetime sensitive column | exact first and last instants | preceding/following-year bounds; `year_bins` unchanged |
| non-sensitive column (`status`, `diagnosis`) | raw | unchanged, byte for byte |
| `type: statistical` on a redacted numeric or datetime column | n/a | generates inside the coarse bounds |
| `type: statistical` on a redacted categorical column | n/a | validate and run fail with `plan.statistical_snapshot_values_redacted` and the two next steps |
| same column, snapshot re-fit with `--include-values`, `allow_real_categories: true` | n/a | loads and samples real categories (escape hatch) |
| a snapshot written before this change | loads | loads (no marker: unchanged behavior) |
| an old or raw snapshot with a real category spelled `[redacted]` | loads with `allow_real_categories: true` | still loads with that opt-in |
| `decoy fit --epsilon ...` on an uncertified host | engine repr and fingerprint, exit 3, CSV read first | plain line plus next step, exit 3, header read only; detail under `--verbose` and in JSON |
| malformed DP request on an uncertified host | typed usage error before the proof gate | same typed usage error; header read only |
| DP fit, CSV replaced between the header and body reads | n/a | usage error `dp_source_changed`, exit 1 |

## 5. Enumerations (from the code at engine `942dae2d`, CLI `de55dab5`)

### 5.1 Exit paths (CLI fit)

| Path | Code | Change |
|---|---|---|
| usage errors in D0 (`fit.py:337-395`) | 1 | adds `--include-values` with `--epsilon` |
| unreadable CSV, missing `--parse-dates` column (`:399-419`) | 1 | none |
| DP uncertified (`:478-484`) | 3 | moved after the header read and name checks, before the body read; new human text |
| body columns differ from the header (new) | 1 | `dp_source_changed` |
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
| categorical | `top_values` / `other_count` / `high_cardinality` | empty list / aggregate count / value-free marker |
| top_value | `value` / `count` | terminal child of removed `top_values`; not serialized for a sensitive column |
| datetime | `min` / `max` / `year_bins` | previous-year start / next-year end / value-free |
| year_bin | `year`, `count` | value-free (Safe Harbor keeps year) |
| freetext | `length`, `length_bin_edges`, `length_bin_counts` | value-free |
| length | `min`, `max`, `mean`, `std` | value-free (lengths, not cells) |
| joint | `columns`, `cell_count`, `cells`, `other_count` | value-free names and count / empty list / aggregate count |
| joint_cell | `key` / `count` | terminal child of removed `cells`; not serialized for a sensitive joint |

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
| 1 | Value-bearing output | raw cells in fit snapshots (R1); a categorical or joint sentinel, numeric bound or interior edge equal to a real cell; an exact datetime boundary | `distribution_snapshot_document` over `SNAPSHOT_VALUE_STATS`; empty sensitive categorical and joint value containers with aggregate counts; one numeric bin with ordinary bounds strictly outside every finite cell or a collision-free finite fallback; preceding/following-year datetime bounds | AT-1, AT-2, AT-4, AT-4b, AT-5, AT-6, AT-7, AT-11, AT-C1 | M1: keep categorical entries or joint cells (AT-1, AT-6, AT-7 red); M1a: write any string sentinel (AT-6 real-sentinel row red); M1b: restore the zero clamp (AT-4 zero row red); M1c: use more than one bin (AT-4b grid-aligned cell red); M1d: drop the finite fallback (AT-3 near-max sampling red); M1e: use the observed year boundary (AT-5 red) |
| 2 | Cleanup missing on an exit path | N/A: no temp files or handles; the write stays on `write_local_target` | N/A | N/A | N/A |
| 3 | State and ordering | DP refusal after the CSV read; request errors preempted by provenance; redaction mutating the raw snapshot a caller holds | header read, name checks, then shared request preparation and environment gate, then the body read; the function owns its raw snapshot | AT-C6 (`read_csv` spy); existing name-check-first tests; AT-9 | M3: move the gate after the body read (AT-C6 red); M3a: call the environment gate without shared request preparation (invalid-epsilon cell red) |
| 4 | Route parity and silent fallback | the CLI keeps `compute_distribution_snapshot`; a second predicate, stamp, rule applier, histogram, crosstab or DP request validator appears; a new read site in `fit` | AST guard on `src/decoy`; one `value_sensitive_names` / `stamp_value_policy` pair and one `_apply`; the raw snapshot's one histogram and crosstab; one shared DP preparation function; one merged body read | AT-C8, AT-14, AT-14b, AT-E3, AT-C6, the CLI CSV-read sentry | M4: in `_snapshot_values.py`, add `field.get("pii_score", 0) >= 0.5` (AT-14b red); M4a: call `pd.crosstab` (AT-14b red); M4b: set `quantiles` to `{}` inline instead of through `_apply` (AT-14b red); M4c: keep the DP body read as its own call (CSV-read sentry red); M4d: validate epsilon in the CLI (AT-C6 structural guard red) |
| 5 | Referential integrity | a redacted side must not feed `condition_on`; joint survivors or order must not encode the real side | empty sensitive joint cells with aggregate `other_count`, plus E2 refusal | AT-7, AT-7b, AT-12 | M5: drop the joint clause of the E2 check (AT-12 red); M5b: retain even one sensitive joint cell (AT-7b red) |
| 6 | Untrusted producer | an ill-typed marker, a marker with no or an unknown `value_policy`; an unknown stats key in a sensitive column; a legacy real category equal to the old profile placeholder spelling | refuse malformed or redacted marker states, never infer from a category literal; runtime default-deny to null in the writer | AT-12 cells, AT-11b | M6: infer redaction from the literal `[redacted]` (legacy AT-12 cell red); M6b: treat a missing policy beside `redacted_columns` as raw (AT-12 red); M6c: keep an unknown key or replace it with a string sentinel (AT-11b red) |
| 7 | Check then use on a path (TOCTOU) | DP mode checks names on the header read, then reads the body | body columns must equal the header's, else `dp_source_changed` | AT-C6 changed-file cell | M7: drop the comparison (AT-C6 cell raises `KeyError`, red) |
| 8 | Weak tests and overclaimed evidence | a fixture that never fills a value slot; a coarse bound that happens to equal a cell; a property that never draws decimal-grid values; a joint fixture too small to reach the cut | AT-11 precondition (raw holds the canary); AT-4 fixed rows and a decimal-grid strategy; AT-7b precondition (raw joint is truncated with ties at the cut) | AT-4, AT-7b, AT-11 | M8: drop the `- G` / `+ 2 * G` terms (AT-4 grid-aligned row red) |
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
  all inside `[min, max]`; an integer column draws `int`s. A constant `1.79e308` and a close
  `1.70e308..1.71e308` range use the finite fallback and also sample without a degenerate-histogram error.
- **AT-4** Coarse-domain table and property, on the grid helper directly. Fixed rows: `(4.23, 6.56, 42)` and
  `(94.0, 99.8, 39)` (a float rule wrote the real min and max here); the section 3 worked values; constants at
  `-7`, `0` and `7`; negative-only range; a range crossing 0; `lo == 0` (the lower bound is negative); ranges of
  exactly 1 and 10 (the power-of-ten boundary); `1e-9` scale; `1e15` scale; `(1e6 + 0.1, 1e6 + 0.1 + 1e-9)`
  (range tiny against magnitude); `+/-1e308` under a 5 s timeout on the grid and fallback helper directly (the
  raw snapshot already raises "Too many bins" for that full-span range); and document cases for a constant
  `1.79e308` and `1.70e308..1.71e308`. Hypothesis over
  finite floats and over a decimal-grid strategy (`Decimal` with 0 to 4 places, converted with
  `float(str(d))`). Properties on the written document: exactly two finite edges and one positive count; the
  total equals the finite count; neither edge equals any finite source cell; the ordinary path has
  `lower < min(source)` and `upper > max(source)` with `G > r`; the fallback path uses the first available
  half-integer pair and is deterministic.
- **AT-4b** One-bin invariant: ids 1001..1010 as `int64` and as `float64` with one NaN give `[990, 1030]` and
  count 10; constant `int` 7 gives `[-10, 20]`; 5,000 ages 18..90 give `[-100, 200]`; decimal-grid values equal
  to likely interior cuts still produce no interior edge. Every sensitive numeric document has
  `len(bin_edges) == 2`, `len(bin_counts) == 1`, and no edge equal to a finite source cell.
- **AT-5** Datetime: `admitted` includes exact cells `1991-01-01T00:00:00`, `1991-12-31T23:59:59` and dates
  between them. The document has `min == "1990-01-01T00:00:00"`,
  `max == "1992-12-31T23:59:59"`, `year_bins` equal the raw ones, no raw timestamp canary, and sampling stays
  in 1991.
- **AT-6** Categorical: `top_values == []`; `other_count == non_null_count`; an HC-5 column keeps
  `high_cardinality: True`. Fixtures include real categories `[redacted]` and `[redacted:1]`; neither appears in
  the document. Aggregate count mass before and after is equal.
- **AT-7** Joints: a `--joint full_name,diagnosis`-style pair and a pair whose real sensitive cell is
  `[redacted]` both have `cells == []`; `other_count` equals the raw sum of listed cell counts plus raw
  `other_count`; `columns` and `cell_count` stay. A joint of two non-sensitive columns is unchanged.
- **AT-7b** Relabel invariance across the cut: a 40-row frame with a unique sensitive `email` column and a
  3-value `status` column, so the raw pair sorts `(email, status)` with the sensitive side first and 40 count-1
  cells tie at `contingency_top_k = 25` (precondition: raw `other_count == 15`). Replacing the emails with a
  different unique set, permuted against `status`, gives a byte-identical document with no cells and
  `other_count == 40`. A second case puts the sensitive column second in the pair.
- **AT-8** Non-sensitive columns (`status`, a non-unique numeric) are equal to the raw snapshot's entries.
- **AT-9** `include_values=True` equals `compute_distribution_snapshot(...)` with the same arguments plus
  `value_policy: "raw"` and the sensitive names; the raw snapshot passed through is not mutated.
- **AT-10** Registry sentry: a fixture frame that produces every kind, a joint and an HC-5 column; every visited
  key is in exactly one of `SNAPSHOT_VALUE_STATS` and `SNAPSHOT_VALUE_FREE_STATS`; the walk stops below a
  registry-classified terminal value container such as `top_values` or `cells`. The kinds seen
  equal `SNAPSHOT_KINDS`, and `SNAPSHOT_KINDS` equals the kind literals an AST read of `snapshot.py`'s
  `_stats_for` returns plus the `"empty"` assignment in `_column_snapshot`. Bite proofs: monkeypatch
  `numeric_stats` to add a key (red: unclassified), and an AST fixture with a sixth kind literal (red).
- **AT-11** Per-entry canary: for each `SNAPSHOT_VALUE_STATS` entry, a fixture column whose raw snapshot holds
  a canary in that slot (precondition asserted), and the document does not. `bin_counts` cannot hold a canary,
  so its entry is checked as: one count with the same finite total, two finite edges, and neither edge equal to
  a finite source cell.
- **AT-11b** Runtime default-deny: monkeypatch `compute_distribution_snapshot` to add an unknown stats key
  holding a canary to a sensitive column and to a non-sensitive column; the sensitive one becomes `None`, the
  other is unchanged. A canary whose real spelling is `[redacted]` also becomes `None`.
- **AT-13** Determinism: two calls on the same frame give identical `json.dumps(..., sort_keys=True)` bytes.
- **AT-14** Parity: for the S1 canary frame and the QA frame, `redacted_columns` of the snapshot document
  equals `redacted_columns` of `profile_document(profile_columns(df, label))`; with `profile_row_cap=3` on a
  frame whose sensitivity changes after row 3, it equals the scan document of `df.head(3)`.
- **AT-14b** Structural guard: `_snapshot_values.py` calls `value_sensitive_names`, `stamp_value_policy` and
  `_apply` with `denied_value=None`, and its AST holds no call or loaded member named `crosstab`,
  `_joint_snapshot` or `joint_snapshot`,
  no string constant or attribute named
  `pii_score`, `detector_matches`,
  `is_likely_unique`, `value_policy` or `redacted_columns` (the last two come only from the S1 constants), no
  float literal in `[0, 1]` (the floor is written `Decimal(1).scaleb(-12)`), and no `None`, `{}` or `[redacted]`
  assigned to a stats key (those go through `_apply`). Both `top_values` and `cells` aggregation call
  `_collapse_counted_items`. `redact_profile_dict` calls the same selection, stamp and apply helpers (AST check
  in the same test).
- **AT-15** Fidelity gate: a generate run whose statistical numeric column uses a redacted snapshot reports
  that column as not comparable with reason `no_quantiles`; it is neither scored nor dropped from the
  report.

Engine E2 (`tests/unit/generation/test_statistical_redacted.py`):

- **AT-12** `load_spec` raises `statistical_snapshot_values_redacted` for: a categorical column in
  `redacted_columns` with and without `allow_real_categories: true` under policy `flagged-redacted/v1`; a
  missing or unknown policy beside `redacted_columns`; either marker key with an invalid type;
  `condition_on` through a named redacted side; and an HC-5 redacted column (this code, not the HC-5
  requires-real code). Loads: a redacted numeric and datetime column; a policy-`"raw"` snapshot listing the
  column, with `allow_real_categories: true`; a non-sensitive categorical column of a redacted document, with
  `allow_real_categories: true`; a markerless legacy snapshot whose real category is `[redacted]`; and a raw
  policy snapshot whose real category is `[redacted]`. The same config through the plan compiler reports the
  same refusal code at validate (`plan/_checks.py:378`).

Engine E3 (`tests/unit/quality/test_dp.py`, `tests/unit/test_public_api.py`):

- **AT-E3** The public `check_dp_fit_request` and `fit_dp_snapshot` share the same private preparation function.
  For invalid epsilon, delta, numeric bins, carrier schema and derived numeric edges, the check-only entry point
  raises the same typed code before `check_fit_environment`; for a valid request on an uncertified host it
  raises the same provenance code. The quality facade exports the entry point (`quality/__init__.py:37-65`).

CLI C1 (`tests/e2e/test_fit_command.py`, `tests/sentry/test_fit_value_redaction.py`):

- **AT-C1** QA repro end to end: `decoy fit patients.csv`: the file, stdout and stderr hold no source name,
  SSN or id; stdout names the withheld columns; exit 0.
- **AT-C2** `--include-values`: the file holds the values; stderr warning names the columns and the path.
- **AT-C3** `--include-values --epsilon 1 --delta 1e-6 --dp-text x` on a real CSV with a spy on `pd.read_csv`:
  exit 1, code `dp_usage_include_values`, and the spy saw no call (`source` has `exists=True`, `fit.py:198-204`,
  so a missing file would be rejected before D0 and prove nothing).
- **AT-C4** `--json` success payload has `value_policy` and `redacted_columns` and no source value.
- **AT-C5** fit then `decoy validate` on a generate config: a `type: statistical` numeric sensitive column
  passes; a categorical sensitive column fails with `statistical_snapshot_values_redacted` and the hint text;
  after re-fitting with `--include-values`, the same column with `allow_real_categories: true` passes.
- **AT-C8** AST guard: no module under `src/decoy` calls `compute_distribution_snapshot`. It reuses the CLI's
  call scanner (`tests/sentry/io_policy/_calls.py`: `bindings`, `qualified`, `call_sites`) and covers
  `from decoy_engine.quality.snapshot import compute_distribution_snapshot`, an `import ... as` alias, a module
  attribute call (`snapshot.compute_distribution_snapshot(...)`) and the `decoy_engine.quality` lazy export (`quality/__init__.py:51`);
  a bite fixture per spelling goes red.

CLI C2 (`tests/e2e/test_fit_command.py`, `tests/e2e/test_explain_command.py`):

- **AT-C6** With `check_fit_environment` patched to raise each of the two codes, a real CSV and a spy on
  `pd.read_csv`: exit 3; the only `read_csv` call has `nrows=0`; human stderr has the plain line and the hint
  and no `fingerprint`, `PlatformTriple`, or digit run of 8 or more; `--verbose` adds the engine message;
  `--json` has `code`, `message`, `hint`. A phantom `--dp-number` column still exits 1 before the gate. With
  the gate passing and the spy returning a header and then a body with a different column name, exit 1 with
  `dp_source_changed` and a JSON error payload, no `KeyError`. Invalid epsilon, delta, numeric bins, numeric
  bounds and carrier declarations keep their existing typed usage codes before provenance; the spy sees the
  header read but no body read. An AST guard over CLI `fit.py` rejects calls to the engine's private DP
  validators, any new semantic validator outside the pre-existing `_parse_dp_number_spec`, or a copied
  certified-row table; the early engine decision call is the public `check_dp_fit_request`. In exact mode, the
  DP header read and the DP body read, a fake `read_csv` exception whose string is `RAW-CELL-CANARY` produces a
  usage error in human and JSON modes with no canary or exception text.
- **AT-C7** `decoy fit --help` says DP runs only on a certified host; `decoy explain differential-privacy`
  summary and first paragraph say so; `decoy explain --json` lists the topic.

## 8. Owner questions

None. Recommended options taken without asking (V1 bar): redact by the S1 predicate rather than every column
(same as OQ1 of QA-SCAN-REDACT); recode numeric and datetime rather than drop them, so generation still works;
release one numeric bin because any interior edge can equal a real cell; refuse
categorical generation rather than sample placeholders; keep `--epsilon` visible but say it is gated rather
than hide it.

## 9. Reuse inventory

| Need | Reused | Not reused, why |
|---|---|---|
| Which columns | `value_sensitive_names` (extracted from S1's `redact_profile_dict`, over `is_value_sensitive`) and `profile_columns` | no snapshot-side rule |
| Marker keys, policy values, stamping | S1 constants and `stamp_value_policy` in `_value_redaction.py` | no snapshot-side copy |
| Rule vocabulary | S1's `Rule` enum, four members added | no second enum |
| Applying NULL, EMPTY_LIST, EMPTY_MAP and default-deny | S1's `_apply`, with keep and denied-value arguments | no second applier |
| Raw snapshot | `compute_distribution_snapshot` | |
| Re-binning | `numeric_stats(domain=..., bins=1)` (`snapshot.py:401-460`, renamed in E1) | no second histogram |
| Joint cells | the joint already returned by `compute_distribution_snapshot`; sensitive `cells` are emptied and their listed counts added to `other_count` | no second crosstab |
| Defaults | `snapshot.py` `_DEFAULT_*` constants | not retyped |
| Refusal plumbing | `StatisticalSpecError`, `reasons.plan` | |
| DP gate | `check_fit_environment` | the CLI does not restate certified rows |
| DP request validation before the early gate | E3's public check-only wrapper and `fit_dp_snapshot` call one private preparation function (`quality/dp.py:319-345`) | no CLI-side validator |
| CLI error output | `_emit_error` (`fit.py:302-324`) | |

## 10. Placement

| Item | Where | Size | Why there |
|---|---|---|---|
| snapshot registry, four `Rule` members, `SNAPSHOT_KINDS`, `value_sensitive_names`, `stamp_value_policy`, `_apply` keep and denied-value arguments, and EMPTY_MAP | engine `profiling/_value_redaction.py` | +65 (356 on S1 at `1daabb74`, about 420 after) | the one file that decides which artifact fields are values and how the marker is written |
| `distribution_snapshot_document`, `Decimal` coarse domain, collision-free finite fallback, one-bin rule, per-kind rules, categorical and joint aggregation | engine `profiling/_snapshot_values.py` (new) | about 235 | profiling owns the sensitivity decision, as placed by `CODEMAP.md:227`; `profiling/` has 8 direct modules with S1 (9 after); `quality/` already has 25 direct `.py` files, so a new module there would block, and `snapshot.py` is at 600 |
| public name `numeric_stats` | engine `quality/snapshot.py` | 0 (rename to a shorter name; stays at 600, no census entry) | the cross-package import uses a public name; `snapshot.py` has no `__all__` and no outside caller of it |
| exports | `profiling/__init__.py`, `decoy_engine/__init__.py` | +4 | public surface for the CLI |
| refusal | `generation/statistical/_spec.py` | +25 (481 to about 506) | the one place generation reads snapshot stats |
| shared DP request preparation and check-only public entry point | engine `quality/dp.py`, exported by `quality/__init__.py` | about +25 (529 to about 554) | the landed fit already owns this validation and gate at `quality/dp.py:319-345`; E3 extracts that exact prefix instead of adding a CLI copy; no new module in the 25-module package |
| reason code | `reasons/plan/_part3.py` | +8 (61 to 69) | the file holding the other statistical snapshot codes |
| engine tests | `tests/unit/profiling/test_snapshot_values.py`, `tests/sentry/profiling/test_snapshot_value_registry.py`, `tests/unit/generation/test_statistical_redacted.py` | about 380, 160, 150 | |
| CLI wiring | `cli/fit.py` | +35 (C1) and +30 (C2), 534 to about 600 | edits in place; no new CLI module |
| CLI text | `cli/explain.py` DP topic | about +4 net | |
| CLI sentry | `tests/sentry/test_fit_value_redaction.py` plus manifest | about 60 | |

## 11. Frozen contracts touched

- **`distribution-snapshot/v1`**: two optional top-level keys (`value_policy`, `redacted_columns`), which the
  contract allows (`compatibility-contract.md:124-126`). Redacted values use types v1 already carries
  (`null` mean and std, `{}` quantiles, empty `top_values` and `cells` lists). Old readers ignore the keys; old
  files load.
- **Reason registry**: one addition, `plan.statistical_snapshot_values_redacted`, at the next minor of the
  base (1.10.0 on today's main) with fixture `tests/fixtures/f29/reason-manifest-v1.10-fit-redact.json`;
  `tests/fixtures/f29/consumer-contract.json` pins `registry_version` and moves with it (as at `a5eb66e2`);
  `registry_renumber.py` moves both at merge if another plan takes 1.10.0 first. Escalation trigger (frozen contract): E2 runs the full engine
  suite.
- **Engine public API**: new exports `distribution_snapshot_document` and
  `decoy_engine.quality.check_dp_fit_request`; nothing removed. Public exports are declared through package
  facades as required by `CODEMAP.md:215` and `:254`.
- **CLI `fit --json`**: two new keys on success; the DP error payload keeps its keys, `hint` text changes.
- **CLI pin**: C1 bumps `decoy-engine` (escalation trigger: dependency lock; full CLI suite).
- **`docs/cli-reference.md`**: regenerated for `--include-values` and the help text (drift-checked by
  `tests/unit/test_cli_surface.py`).

## 12. Slices

| Slice | Repo | Files (at most 10) | Lines | Tests |
|---|---|---|---|---|
| E1 | engine | `_value_redaction.py`, `_snapshot_values.py`, `quality/snapshot.py` (rename only), `profiling/__init__.py`, `decoy_engine/__init__.py`, `tests/unit/profiling/test_snapshot_values.py`, `tests/sentry/profiling/test_snapshot_value_registry.py`, `tests/unit/test_public_api.py`, `tests/sentry/test_sentry_manifest.py`, `docs/methodology-registry.yaml` (10) | about 820 | AT-1 to AT-11b, AT-13, AT-14, AT-14b first; T1 = `tests/unit/profiling`, `tests/sentry`, `tests/unit/quality/test_snapshot.py`, `tests/unit/generation/test_statistical.py` |
| E2 | engine | `_spec.py`, `reasons/plan/_part3.py`, fixture `reason-manifest-v1.10-fit-redact.json`, `tests/fixtures/f29/consumer-contract.json`, `reasons` version constant, `docs/reasons.md`, `tests/unit/test_reason_registry.py`, `tests/unit/generation/test_statistical_redacted.py`, `CHANGELOG.md` (one entry for E1 and E2), `CODEMAP.md` (moved from E1) (10) | about 260 | AT-12, AT-15; full engine suite (registry) |
| E3 | engine | `quality/dp.py`, `quality/__init__.py`, `tests/unit/quality/test_dp.py`, `tests/unit/test_public_api.py`, `CHANGELOG.md` (5) | about 100 | AT-E3; T1 = the two named unit files, `tests/unit/quality/test_dp_provenance.py`, `tests/sentry` |
| R | engine | release request to claudeorchestrator after E1, E2 and E3 merge, including the re-pin of the CLI runtime row in `_CERTIFIED_STACKS` (`quality/dp_provenance.py:297-320`) for the new engine version, so 24g does not get worse | | |
| C1 | CLI | `pyproject.toml`, `uv.lock`, `cli/fit.py`, `tests/e2e/test_fit_command.py`, `tests/sentry/test_fit_value_redaction.py`, `tests/sentry/test_sentry_manifest.py`, `docs/cli-reference.md`, `CHANGELOG.md` (8) | about 260 | AT-C1 to AT-C5, AT-C8; full CLI suite (pin bump) |
| C2 | CLI | `cli/fit.py`, `cli/explain.py`, `tests/e2e/test_fit_command.py`, `tests/e2e/test_explain_command.py`, `docs/cli-reference.md`, `CHANGELOG.md` (6) | about 220 | AT-C6, AT-C7, plus the updated DP tests that stub only `fit_dp_snapshot` (boxing capture `:381-403`, carrier, budget and DpError `:648-700`, `TestFitDpArtifactShape` `:803`); those tests also stub `check_dp_fit_request`. T1 for the touched files plus `tests/sentry`, naming `tests/sentry/test_csv_read_choke_point.py` (its pin for `fit` stays 2; the baseline file is not edited) |

Order: S1 of QA-SCAN-REDACT merges first (E1 imports its module). E1, then E3, then E2, then R, then C1, then C2. E2's
frozen-contract full suite therefore runs on the final engine head that includes E3. C2
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
- **Sensitive numeric columns lose their histogram shape.** A 10-row id column becomes one bin over
  `[990, 1030]`; ages and balances also have one bin. This is the price of ensuring no interior edge can equal a
  real cell. `--include-values` restores the exact histogram.
- **Sensitive numeric columns generate over a wider domain.** Ordinary bounds sit a grid step `G > r` outside
  the data and generation is uniform over that bin. Near float max, the collision-free fallback may not enclose
  the source range at all. It exists to keep generation functional when no outward finite float exists.
  `--include-values` restores the exact histogram, and F6 lets the fidelity gate score the released bin.
- **A user's CSV changes during a DP fit.** The body-versus-header comparison turns it into a usage error;
  content changes that keep the header are the user's own file and are fitted as read.
- **A redacted snapshot is mistaken for a DP release.** The DP verifier requires `dps-marginal/v3` and a `dp`
  block; a `v1` file fails that check before the shape test in `_checks_dp_carrier.py`.
- **The CLI's early DP check drifts from the engine's.** It calls the same function; there is nothing to
  drift.
- **E1 lands without E2.** A redacted categorical column with `allow_real_categories: true` would reach the
  existing degenerate-histogram refusal instead of the new directive. It cannot emit a source value because
  `top_values` is empty; R waits for both, and C1 waits for R.

## 14. Follow-ups (recorded, not in V1)

- V2-LOG F1: platform: if it ever writes a distribution snapshot for users, route it through
  `distribution_snapshot_document` (`quality/snapshot.py:143-226`).
- V2-LOG F2: add a display-time `redact_snapshot_dict` if a CLI command ever prints a loaded snapshot
  (`generation/statistical/_spec.py:169-198`).
- V2-LOG F3: honour `decoy scan`'s `--strategy random` option in `fit` if users ask for it
  (CLI `scan.py:616-619`).
- V2-LOG F4: comments at `quality/_distribution_gof.py:194` and
  `tests/unit/quality/test_distribution_gof.py:71` still name `_numeric_stats` after the E1 rename; fix them when
  those files are next touched.
- V2-LOG F5: add an `explain` topic for `fit` and `snapshot` in a later docs pass
  (CLI `explain.py:383-407`).
- V2-LOG F6: let the fidelity gate score a redacted numeric column by KS on its bin instead of reporting it as
  not comparable (`quality/fidelity.py:315-327`, `:366-377`).
- V2-LOG F7: top-code birth years when a column is known to be a birth date; this needs semantics the snapshot
  does not carry (`quality/snapshot.py:487-501`).
- V2-LOG F8: the existing high-cardinality UTF-8 error keeps a `UnicodeEncodeError` cause whose traceback can
  retain a label. This is server-admin crash evidence, not a user artifact (`quality/snapshot.py:364-377`).
- V2-LOG F9: this hand-written plan is in the 800 to 900 line cleanup band
  (`2026-11-08-qa-fit-redact.md:1`).
- DESIGN F10 (pre-existing, non-blocking): CLI `_parse_dp_number_spec` checks finite ordered bounds after parsing
  the option text, while engine `parse_column_schema` checks the prepared bounds again. E3 adds no third copy;
  consolidate the old pair only in a separately scoped compatibility plan (CLI `fit.py:83-112`, engine
  `quality/dp.py:329-337`).
