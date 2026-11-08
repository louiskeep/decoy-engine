Status: plan

# QA-SCAN-REDACT: saved scans carry no raw values for PII-flagged columns (roadmap row 24c)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-08. Scope: engine + CLI, V1 blocker (leak class).
Revision 3 (Codex review of revision 2 at `ec18fe6b`; fixes folded in, dispositions in section 0).
Dennis round 1 gave GO on revision 1 at `00a1cffb` (0 blockers, 8 SHOULD, 9 NIT).

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Design notes, Established solutions), `feature-dev.md`, `architecture.md`,
`reuse-first.md`, `security.md`, `testing.md` (test depth, escalation triggers), `cross-cutting-concerns.md`,
`api-and-compatibility.md`, `code-review.md`, `workspace-and-vcs.md`, `scope-discipline.md`,
`verification.md`, `documentation.md`, `catalogs/recurring-defects.md`; the plan contract
(`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md`), `LOCAL-BUILDER-BRIEF.md`; the standing rules
develop-correctly-registries-and-sizing and best-practice-becomes-a-sentry; the V1 bar (function-break, data
loss or a non-admin value leak blocks; a second copy of a central mechanism, or a new module in a flat folder
over 25, always blocks); engine `CODEMAP.md:55,98` (profiling placement); engine `CLAUDE.md` (cite the
established method; `docs/methodology-registry.yaml`); the prior disclosure decision engine
`docs/remediation-source.md:189` (D3, "don't serialize `ColumnProfile.top_values`"); the SCAN-MULTI S1a
branch `feat/scan-multi-s1a` at `919ebd3b` and its plan on `plan/scan-multi`; the code at engine `main`
`7a61be3c` and CLI `main` `994bc7a`; platform `main` `71e07b71` for the one platform consumer.

Source: first-user QA, `docs/records/2026-11-08-cli-qa-first-user.md` on CLI branch
`docs/roadmap-cli-qa-intake`, finding 5 ("`scan_patients.json` includes real names and SSNs in
`top_values`; `scan show ssn` prints them ... the format is built to be diffed and checked in CI") and
section 5 ("Redact `top_values` for PII-flagged fields in saved scans (opt in to raw values)").

Terminology: this plan calls a column whose values get redacted **value-sensitive** (section 3). That is a
wider set than the existing `pii_candidate` (score at least 0.6), so user text and the CHANGELOG say
"value-sensitive", never "PII candidate", for redaction.

## 0. Review dispositions

Codex review of revision 2 (`ec18fe6b`):

| Finding | Disposition |
|---|---|
| V1: `SentinelFlag.value` was kept even though it is a literal cell value, and `SentinelFlag.note` repeats that literal for string, numeric and date sentinels | taken: both fields are registry VALUE entries and are replaced for every value-sensitive column; the fixed-list exception and its extra sentinel export are removed; AT-1, AT-3b and AT-12 cover both fields |
| V1: the loaded-file predicate did not say how Python `bool`, non-finite floats and out-of-range scores are treated, so a malformed stored score could take an interpreter-dependent non-sensitive path | taken: exact stored types and ranges are stated; every missing or invalid predicate input fails closed; AT-9 adds a malformed-predicate fixture |
| V1: `scan show --include-values` could skip the redactor and therefore omit `value_policy` on a legacy file, contrary to the output contract | taken: `scan show` always calls `redact_profile_dict(data, include_values=...)`; the true branch preserves values and adds `raw` only when the marker is absent; AT-9 pins the legacy marker |
| V1: AT-3's `make_dataclass` mutation passed `Field` objects as field specifications and did not replace the child annotation reachable from `ColumnProfile` | taken: the sentry builds an augmented `FieldStats` from `(name, resolved_type, synthetic default)` triples, then a synthetic root whose `fields` annotation names that augmented type; the private walker accepts a root for this bite proof |
| DESIGN (pre-existing, non-blocking): CLI `scan.py` is 1451 lines and `src/decoy/cli/` is already a flat 32-module package | recorded in Placement; this plan adds no CLI module, caps `scan.py` growth at 15 net lines, and keeps the data rule in the engine choke point rather than adding a CLI mechanism |
| V2-LOG: section 5.1 claimed every valid JSON value reaches the redactor, but `_load_scan_dict` assumes a mapping | taken: the claim is narrowed to every JSON object the existing loader accepts; non-object loader behavior is unchanged and outside the saved-scan contract |
| V2-LOG: stale anchors remained in the round-1 disposition and API/F5 prose, and scan error text was described too narrowly | taken: anchors and the fixed atomic-write error description are corrected |

Dennis round 1 on revision 1 (`00a1cffb`): GO, 0 BLOCKER, 8 SHOULD, 9 NIT.

| Finding | Disposition |
|---|---|
| S1 AT-3 does not back guarantee 3 (hand type list, unstated path map, bite proof bites for the wrong reason) | taken: the type set and the walker's path map both come from one `typing.get_type_hints` walk from `ColumnProfile`; bite proof uses `make_dataclass` with one extra field; new AT-3b per-entry property test; AT-1 asserts every slot was filled before redaction (section 3, section 7) |
| S2 display path trusts the producer (marker, malformed shapes, foreign sentinels, missing predicate inputs) | taken in revision 2: never branch on the marker; shape mismatch becomes the placeholder; missing or ill-typed predicate inputs count as sensitive. Revision 3 supersedes the fixed-sentinel exception and narrows totality to a JSON object's nested shapes. |
| S3 predicate not exported, CLI would copy it | taken: `is_value_sensitive` exported, and the redactor writes `redacted_columns` so the CLI warning needs no predicate; AT-10 also forbids a CLI re-implementation |
| S4 false negatives understated | taken: two measured false-negative cases stated; OQ1 gains option (c), a fail-closed name-shape rule; `explain security` says unflagged columns keep values |
| S5 S2 is 12 files | taken: S2 split into S2a and S2b; missing files added |
| S6 S1 misses the engine sentry manifest | taken: manifest and a `methodology-registry.yaml` entry added; `what-we-cannot-prove.md` dropped from S1 (no D3 line there; the CHANGELOG records the change) |
| S7 scan.py growth and S1c ordering | taken: scan.py budget at most +15 net; the engine returns everything the warning needs; ordering against S1c and CLI-QA1 stated; post-S1c meaning of `decoy scan --include-values` stated |
| S8 "the only place" claim false | taken: claim corrected; S1a wiring adds an engine assertion that every `FieldCore` field taken from `FieldStats` is value-free; CLI `_safe_field_record` logged as F6 |
| NIT1 `include_values=True` overwrites an existing marker | taken: an existing marker is kept; `raw` is set only when none is present |
| NIT2 M1 and M8 wrong | taken: M1 moves the entry to `VALUE_FREE_FIELDS`; M8 mirrors `test_text_sentry_bites_if_raw_value_injected` |
| NIT3 anchors | taken: all corrected (types.py ranges, sentinel producers, demo.py:151-159, `_init_inference.py:247`, L2 anchors, folder count, platform head) |
| NIT4 AT-10 receiver-name only | taken: also flags `.to_dict()` and `dataclasses.asdict()` in any `src/decoy` module that imports `profile_columns` or `ColumnProfile` |
| NIT5 scan diff pre-mortem moot | taken: removed (`compute_scan_diff` compares scores and groups only, `scan.py:1160-1205`) |
| NIT6 fixed `Distribution` labels wiped | taken: stated as intended (section 3) |
| NIT7 F3 incomplete | taken: F3 lists `_pii_bucket` (`scan.py:409-419`) and the platform 0.5 cut (`router.py:1349`) |
| NIT8 terminology | taken: "value-sensitive" (header note) |
| NIT9 `scan show --json` should carry the policy | taken: the envelope carries `value_policy` |

## 1. FRAME

### Problem

`decoy scan --output scan.json` writes `ColumnProfile.to_dict()` verbatim (CLI `src/decoy/cli/scan.py:775-779`,
stdout stream `:808`). `to_dict()` is plain `dataclasses.asdict` (engine `profiling/types.py:195-197`), so
every value-bearing field the profiler fills lands in the file: up to 5 literal cells per column in
`top_values` (`profiler.py:229-240,355`), the mode, numeric min and max, date min and max, up to 3 invalid
date cells, up to 3 detector misses per detector, up to 10 categorical labels, and sentinel values and
notes that repeat those values. `decoy scan show` then
prints the whole field dict in `--json` (`scan.py:1056-1066`) and raw top-value and sentinel tables in human
mode (`scan.py:1114-1134`). Scan files are meant to be diffed and committed for CI, so real SSNs and names end up in
git history and CI logs. The CLI's inline field output already strips values (`_safe_field_record`,
`scan.py:512-534`); the saved artifact and `scan show` do not, and there is no engine rule that says which
fields are values.

### Guarantee (what V1 promises)

1. A scan document written by any Decoy surface (CLI `--output` file or stdout, `decoy demo`, and the S1a
   scan-set `detail` member once it lands) holds no raw cell value for a value-sensitive column unless the
   user passed `--include-values`.
2. `decoy scan show` never prints a raw value for a value-sensitive column without `--include-values`,
   whatever the file says about itself, including files written before this change and hand-edited ones.
3. One engine registry decides which profile fields hold cell values for redaction. A new value-bearing field
   on any type reachable from `ColumnProfile` fails CI until it is classified, and every registry entry is
   proven to be applied.
4. Exit codes, the inline `--json` field record, and every value-free field are unchanged.

### Non-goals (V1)

- Columns that are not value-sensitive keep their values (owner wording: "for PII-flagged columns"; OQ1).
- `ColumnProfile.to_dict()` keeps returning the full form. The platform persists it into the tenant's own
  scan row (platform `api/storm/router.py:1360-1362`) and rebuilds `FieldStats(**f)` from it
  (`api/storm/helpers.py:756`); changing that is a platform decision (OQ2, follow-up F1).
- Header names that are really data (a CSV with no header row) are not redacted here. S1a already detects
  that case (`header_looks_like_data`); follow-up F2.
- In-memory callers (`decoy.scan()` returns the `ColumnProfile` object, CLI `api.py:569-642`; `decoy init`
  reads it in memory, `init.py:413-415`) are the user's own process, not an output surface. Their docstrings
  point at the safe serializer.
- `decoy fit`'s distribution snapshot (`quality/snapshot.py:143,482`) is a separate artifact with its own
  opt-in (`allow_real_categories`, CLI `fit.py:13`, `:398-422`); unchanged.

### Failure modes

| # | Failure | Guard |
|---|---|---|
| F-1 | A new value field or a new nested type is added and serializes raw | AT-3 derived type walk |
| F-2 | A CLI surface keeps calling `profile.to_dict()` or `asdict` for output | AT-10 plus canary sentries AT-7 to AT-9 |
| F-3 | `scan show` on an old, raw or hand-edited file prints values | display-time redaction that ignores the marker; AT-9 fixtures |
| F-4 | Redaction breaks the parallel `distribution.data`/`labels` arrays | AT-5 |
| F-5 | Redaction is not a fixed point | AT-4 |
| F-6 | S1a `detail=True` embeds raw `to_dict()` | AT-6 on whichever of S1a and this lands second |
| F-7 | `--include-values` used silently in CI | stderr warning line from `redacted_columns`; AT-7 |
| F-8 | The predicate is copied into the CLI | `is_value_sensitive` exported; AT-10 forbids a copy |
| F-9 | A registry entry the walker never applies | AT-3b per-entry property test |

## 2. Established solutions

- **ydata-profiling `sensitive=True`** (formerly pandas-profiling): a report mode that hides values for
  categorical and text variables and keeps counts and shape. We copy the shape-preserving redaction (keep
  counts and percentages, drop the values) but invert the default: redacted unless asked.
- **Microsoft Presidio anonymizer, `replace` operator**: a detected entity is replaced by a fixed placeholder
  (`<REDACTED>` style), keeping the surrounding structure. We use a fixed placeholder `"[redacted]"` in list
  entries so array lengths and positions survive.
- **Google Cloud DLP inspection findings**: findings carry the infoType, likelihood and location, and the
  quote is omitted unless `include_quote=True` is set. This is the closest match to our opt-in: the flag
  name `--include-values` mirrors `include_quote`.
- Pattern: classification registry plus default-deny serializer, the same discipline the engine applies to
  private files (X7b `fileaccess/_private.py`) and log fields. The module docstring cites all three, and S1
  adds the `docs/methodology-registry.yaml` entry with a `Pattern:` line.

## 3. Design notes

### Choke point and registry

New engine module `src/decoy_engine/profiling/_value_redaction.py` (about 200 lines). It holds the one
registry that says which profile fields carry cell values and how each is removed. Two older allowlists also
pick value-free `FieldStats` fields for their own output: CLI `_safe_field_record` (`scan.py:512-534`) and
S1a `_field_core` (`scan_set.py:410-422` on its branch). Both fail safe (they list what to keep). The S1a
wiring slice adds an engine assertion that every `FieldCore` field taken from `FieldStats` is in
`VALUE_FREE_FIELDS`; the CLI record is follow-up F6.

```python
REDACTED: Final = "[redacted]"
VALUE_POLICY_KEY: Final = "value_policy"
REDACTED_COLUMNS_KEY: Final = "redacted_columns"
POLICY_REDACTED: Final = "flagged-redacted/v1"
POLICY_RAW: Final = "raw"

# (type name, field name) -> how the value is removed.
VALUE_FIELDS: Final[Mapping[tuple[str, str], Rule]] = {
    ("FieldStats", "min_value"): Rule.NULL,
    ("FieldStats", "max_value"): Rule.NULL,
    ("FieldStats", "date_min"): Rule.NULL,
    ("FieldStats", "date_max"): Rule.NULL,
    ("FieldStats", "mode_value"): Rule.NULL,
    ("FieldStats", "sample_invalid"): Rule.EMPTY_LIST,
    ("TopValue", "value"): Rule.PLACEHOLDER,
    ("DetectorMatch", "sample_misses"): Rule.EMPTY_LIST,
    ("Distribution", "labels"): Rule.PLACEHOLDER_EACH,
    ("Distribution", "min"): Rule.NULL,
    ("Distribution", "max"): Rule.NULL,
    ("SentinelFlag", "value"): Rule.PLACEHOLDER,
    ("SentinelFlag", "note"): Rule.PLACEHOLDER,
}
VALUE_FREE_FIELDS: Final[frozenset[tuple[str, str]]] = frozenset({...})  # every other field, by name

def profile_types() -> Mapping[str, tuple[str, ...]]: ...   # dict path -> type, from one type-hint walk
def is_value_sensitive(field: Mapping[str, Any]) -> bool: ...
def redact_profile_dict(doc: Mapping[str, Any], *, include_values: bool = False) -> dict[str, Any]: ...
def profile_document(profile: ColumnProfile, *, include_values: bool = False) -> dict[str, Any]:
    return redact_profile_dict(profile.to_dict(), include_values=include_values)
```

**One walk, two uses.** A private `_profile_types(root)` walks `typing.get_type_hints` recursively from its
root; `profile_types()` calls it with `ColumnProfile`. The walk follows `list[X]` and `X | None`, and returns
the dict path of every dataclass it reaches
(`fields[*]` is `FieldStats`, `fields[*].top_values[*]` is `TopValue`, `fields[*].distribution` is
`Distribution`, and so on). The redactor uses that map to find each value location; AT-3 uses the same walk
to find the types it checks. No hand-kept type list or path table exists.

**Total and untrusting.** `redact_profile_dict` accepts any JSON object and never raises for its nested JSON
shapes:

- It deep-copies and never mutates its input.
- It never reads the `value_policy` marker to decide anything. Redaction is a fixed point by construction:
  a redacted value location already holds `null`, `[]` or `"[redacted]"`, so a second pass changes nothing.
- In a value-sensitive field, a value location that does not have the expected shape (a `top_values` that is
  not a list, an entry that is a string instead of an object, a non-string label) becomes the placeholder for
  that location. Unknown keys are left alone; they are not profile output.
- It sets `value_policy` to `flagged-redacted/v1` and `redacted_columns` to the names of the value-sensitive
  columns. With `include_values=True` it returns the copy with the values untouched, keeps an existing
  `value_policy` if one is present, and sets `"raw"` only when none is; `redacted_columns` still lists the
  value-sensitive columns so the CLI warning can count them.

**Rule details.**

- `Distribution.labels` and `min`/`max`: for a value-sensitive column every label is replaced entry by entry,
  whatever the `kind`. Categorical labels are cells (`_distributions.py:85`), and numeric bin edges come from
  the real min and max (`_distributions.py:46-56`). This also wipes fixed labels such as the pattern buckets
  and freetext length buckets; that loses nothing a user needs and keeps the rule simple. The `data` array is
  untouched, so lengths stay equal (F-4).
- `mean_value` and `Distribution.mean` are aggregates, not cells: kept (VALUE_FREE).
- `SentinelFlag.value` is a cell literal even when it belongs to a fixed sentinel list. `SentinelFlag.note`
  also repeats the literal for known dates and numerics (`sentinels.py:34-50`) and interpolates the normalized
  string cell (`sentinels.py:226-240`). Both fields become `"[redacted]"` for a value-sensitive column. AT-12
  pins both paths.
- `DetectionSignal.signal` holds detector ids and header names (`name-hint · col="ssn"`): VALUE_FREE under
  the header non-goal.

### What "value-sensitive" means

`is_value_sensitive(field)` is true when any of these holds:

1. `detector_matches` is non-empty: any built-in or custom detector fired at any confidence bucket
   (`detectors.py:155-160` builds the match; buckets `:165-178`). Format detectors (`iso_date`, `us_zip`)
   count: dates of service and ZIPs are identifiers under HIPAA Safe Harbor.
2. `pii_score >= PII_CANDIDATE_THRESHOLD` (0.6). With the current scorer this implies rule 1 (`_score_pii`
   returns 0.0 without a match, `profiler.py:55-58`); it is kept so a future scorer that rates a column
   without a detector still redacts.
3. `is_likely_unique` is true: a near-unique column (names, emails, free identifiers) is identifying even
   when no detector fires.
4. Fail closed on a loaded file. `detector_matches` must be an exact `list`; `pii_score` must be an exact
   finite `float` in `0.0..1.0` (a `bool`, integer, NaN or infinity is invalid); and `is_likely_unique` must
   be an exact `bool`. If any input is missing or invalid, the column counts as value-sensitive. A non-empty
   valid detector list counts as sensitive regardless of the shapes of its entries.

Measured on the engine at `7a61be3c` (Dennis round 1, `profile_columns` on CLI-style `pd.read_csv` frames):
the QA shape is covered (`full_name` by `person_name`, `ssn` by `ssn`, `patients.patient_id` by rule 3).
Two known misses:

- A column of full names whose header has no name token and whose values repeat (a `member` column in a fact
  table, `unique_rate` 0.015): `person_name` needs a header hint (`detectors.py:454-468`) and repeats defeat
  rule 3, so raw names stay in `top_values`, `mode_value` and the labels. Headers like `patient`, `insured`
  or `subscriber` in fact tables hit this.
- The FK side of an identifier (`visits.patient_id`, `unique_rate` 0.5) keeps raw values while the parent
  side is redacted.

Columns that are not value-sensitive (low-cardinality codes, statuses, amounts with no detector) keep their
values: that is what makes `top_values` useful for picking strategies. OQ1 offers wider rules. `decoy explain
security` states plainly that such columns keep their values.

`PII_CANDIDATE_THRESHOLD` is defined once. S1a defines it in `scan_set.py:59` on its branch; this plan
defines it in `_value_redaction.py`. Whichever lands second imports from the first and deletes its own. The
CLI's display cut-offs are follow-up F3.

### Opt-in flag and ordering against S1c

- `decoy scan --include-values`: the saved file and the `--output -` stream carry raw values. A one-line
  stderr warning gives `len(redacted_columns)` (the count of value-sensitive columns whose values were kept);
  the CLI never evaluates the predicate itself.
- `decoy scan show --include-values`: pass the loaded document through
  `redact_profile_dict(data, include_values=True)`, which shows what the file holds and adds a `raw`
  `value_policy` only when the file has no marker. It cannot restore values a redacted file never stored. The `--json`
  envelope always carries the resulting document's `value_policy`.
- Engine API, exported from `decoy_engine` and `decoy_engine.profiling` (the CLI may import only public
  names, CLI `tests/sentry/test_engine_import_surface.py`): `profile_document`, `redact_profile_dict`,
  `is_value_sensitive`.
- S1a `scan_set_to_dict(s, *, detail, include_values=False)`: `detail=True` embeds `profile_document(...)`
  instead of `profile.to_dict()` (`scan_set.py:459-473` on the branch).
- S1c and L2: the S1a plan (engine branch `plan/scan-multi`, line 92) makes `decoy scan --output` write the
  value-free core from S1c on, and puts the per-table detail behind L2's `--output-detail` into protected
  library scratch (line 107; CLI roadmap L2-cli row `docs/ROADMAP.md:280`). This plan's R1 and R2 wiring is
  therefore the interim behavior until S1c. When S1c lands, `--include-values` moves with the detail: it
  applies to `--output-detail` (passed through as `include_values=True`) and is refused with a usage error
  on a core-only `--output`, because the core has nowhere to hold values. S1c's brief carries this.
- Ordering: S2a and S2b land before S1c's CLI slice when possible; CLI-QA1 (row 24e) also edits `scan.py`
  (its F4 card keys); whichever lands second rebases. No shared lines are expected.

## 4. Behavior

| Command | Before | After (default) | After `--include-values` |
|---|---|---|---|
| `decoy scan f.csv --output s.json` | raw `to_dict()` | value-sensitive columns redacted, `value_policy: flagged-redacted/v1`, `redacted_columns` | values kept, `value_policy: raw`, stderr warning |
| `decoy scan f.csv --output -` | raw JSON line | redacted JSON line | raw JSON line, stderr warning |
| `decoy scan f.csv` / `--json` | `_safe_field_record` | unchanged | flag accepted, no effect on inline output |
| `decoy scan show s.json col --json` | whole field dict | field dict after `redact_profile_dict`, plus `value_policy` | field dict as stored, plus its existing policy or `raw` when absent |
| `decoy scan show s.json col` (human) | Top-value and sentinel tables with cells | value text is `[redacted]`; counts remain | cells as stored |
| `decoy scan fields` / `scan diff` | value-free | unchanged | unchanged |
| `decoy demo` | `scan.json` raw | `scan.json` via `profile_document` | n/a (demo data is synthetic; no flag) |
| `decoy explain security` | "scan JSON ... can contain top values" | names value-sensitive redaction, `--include-values`, and that other columns keep values | |

Exit codes: unchanged on every path; no new error path before S1c.

## 5. Enumerations (from the code at engine `7a61be3c`, CLI `994bc7a`)

### 5.1 Exit paths

- `decoy scan`: success 0; load and write errors keep their current codes (`scan.py:781-804`,
  `EXIT_RUNTIME`). `profile_document` cannot raise on a profile the engine built.
- `decoy scan show`: every JSON object accepted by the existing `_load_scan_dict` (`scan.py:384-406`) reaches
  the redactor; the redactor is total over a JSON object's nested shapes (section 3), so no new failure path exists for a saved-scan
  object. The loader's existing behavior for non-object JSON is unchanged. Field-not-found keeps
  `EXIT_USAGE` (`scan.py:1049`).
- `decoy demo`: unchanged.

### 5.2 Routes (every way a profile becomes output)

| # | Route | Anchor | After |
|---|---|---|---|
| R1 | scan `--output` file | CLI `scan.py:775-779` | `profile_document` |
| R2 | scan `--output -` stream | CLI `scan.py:808` | `profile_document` |
| R3 | scan show `--json` | CLI `scan.py:1012-1069` | always calls `redact_profile_dict` on load, passing `include_values` |
| R4 | scan show human tables | CLI `scan.py:1114-1134` | reads the redacted dict |
| R5 | demo scan.json | CLI `demo.py:151-152` | `profile_document` |
| R6 | S1a scan-set detail | engine branch `scan_set.py:459-473` | `profile_document` |
| R7 | L2 `--output-detail` | engine `plan/scan-multi` plan line 92, CLI roadmap `:280`; not built | through R6 |
| R8 | platform scan row | platform `api/storm/router.py:1360` | unchanged (OQ2, F1) |
| R9 | inline field record | CLI `scan.py:512-534` | already value-free |

### 5.3 FK shapes

N/A: a scan profile has no keys across tables, and S1a relationships carry column names and rates only, so no FK shape is read or written.

### 5.4 Fields (every dataclass field that can reach a scan document)

| Type.field | Source anchor | Class |
|---|---|---|
| `FieldStats.min_value`, `max_value` | `profiler.py:360-361` | VALUE (NULL) |
| `FieldStats.mean_value` | `profiler.py:362` | aggregate, kept |
| `FieldStats.date_min`, `date_max` | `profiler.py:366-367,398-399` | VALUE (NULL) |
| `FieldStats.sample_invalid` | `profiler.py:402` | VALUE (EMPTY_LIST) |
| `FieldStats.mode_value` | `profiler.py:421`, `_classification.py:162-187` | VALUE (NULL) |
| `FieldStats.mode_freq` | same | aggregate, kept |
| `TopValue.value` | `profiler.py:229-240` | VALUE (PLACEHOLDER) |
| `TopValue.count`, `pct` | same | kept |
| `DetectorMatch.sample_misses` | `detection/detectors.py:148,158` | VALUE (EMPTY_LIST) |
| `DetectorMatch.*` others | `detection/types.py:83-110` | kept |
| `Distribution.labels` | `_distributions.py:35,46-50,85` | VALUE (PLACEHOLDER_EACH) |
| `Distribution.min`, `max` | `_distributions.py:36-37,55-56,72-73` | VALUE (NULL) |
| `Distribution.kind`, `data`, `mean` | `_distributions.py` | kept |
| `SentinelFlag.value`, `note` | `sentinels.py:34-50,177-199,214-240` | VALUE (PLACEHOLDER) |
| `DetectionSignal.*` | `detection/types.py:116-133` | header text, kept |
| `FieldStats` shape and rate fields, `ColumnProfile.*` | `types.py:63-156` (FieldStats), `:159-197` (ColumnProfile) | kept |

### 5.5 Output surfaces

- Files: R1, R5, R6 and R7.
- Stdout: R2, R3, R4, R9.
- Logs: profiler log lines carry counts and detector ids only (`profiler.py:494-628`); no value field is
  logged (grep at `7a61be3c`).
- Error text: scan errors print the exception class, or a fixed atomic-write code plus OS reason
  (`scan.py:781-804`, `_atomic.py:159-163`); no raw value or exception message is added.
- Platform DB: R8, out of scope (OQ2).

## 6. Class table

| # | Class | Applies here | Structure that closes it | Sentry or property test | Mutation that turns it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | raw cells in scan files, the stdout stream, `scan show`, S1a detail | `profile_document` / `redact_profile_dict` default-deny over the `VALUE_FIELDS` registry | AT-1 engine canary; AT-3b per entry; AT-7 to AT-9 CLI canary | M1: move `("TopValue", "value")` from `VALUE_FIELDS` to `VALUE_FREE_FIELDS` (AT-1 and AT-3b red) |
| 2 | Cleanup missing on an exit path | N/A: no temp files or handles are added; writes stay on `write_local_target` | N/A | N/A | N/A |
| 3 | State and ordering | input mutated in place; a second pass changes a redacted doc | deep copy then redact; redaction is a fixed point and never reads the marker | AT-4 (fixed point, input unchanged) | M3: redact the input dict in place instead of a copy |
| 4 | Route parity and silent fallback | a CLI route keeps `profile.to_dict()` or `asdict` (R1, R2, R5), the CLI copies the predicate, or S1a detail stays raw (R6) | every output route calls the engine serializer; the CLI counts from `redacted_columns` | AT-10 AST guard; AT-6 for R6 | M4: restore `profile.to_dict()` at `scan.py:808` |
| 5 | Referential integrity | N/A: no keys or tables; S1a relationships carry names only | N/A | N/A | N/A |
| 6 | Untrusted producer | `scan show` reading a legacy, hand-edited or forged file: no marker, a forged `flagged-redacted/v1` marker, odd shapes, sentinel literals, missing or ill-typed predicate inputs | the redactor ignores the marker, is total over a JSON object's nested shapes, replaces off-shape value locations and both sentinel text fields, and fails closed on every invalid predicate input | AT-9 fixtures (legacy, forged marker, string entries, non-list `top_values`, sentinel text, missing inputs, ill-typed inputs) | M6: skip redaction when `value_policy == "flagged-redacted/v1"` |
| 7 | Check then use on a path (TOCTOU) | N/A: no check-then-open is added; `scan show` loads once and redacts the loaded dict | N/A | N/A | N/A |
| 8 | Weak tests and overclaimed evidence | Rich truncation hides a leak; a fixture that leaves a value slot empty passes quietly; a bite proof that fails for the wrong reason | wide console; AT-1 asserts every `VALUE_FIELDS` slot was filled before redaction; `make_dataclass` bite proof with exactly one extra field | AT-1 precondition; AT-3 bite proof; AT-9 bite proof | M8: mirror `test_text_sentry_bites_if_raw_value_injected`: monkeypatch the renderer to print a raw cell after redaction; AT-9 must flag it |
| 9 | Behavior-preserving refactor drift | `to_dict()` must stay the raw form for the platform (R8) | `to_dict()` untouched; the new serializer wraps it | existing profiler tests plus AT-1 `include_values=True` equals `to_dict()` plus the two new keys | M9: make `to_dict()` call the redactor |
| 10 | Frozen contract drift | scan JSON gains `value_policy` and `redacted_columns`; field types must not change; exit codes unchanged | string fields become `null`, lists stay lists, placeholders are strings; no new exit path | AT-5 (shape and lengths), AT-11 (exit codes), existing scan diff tests | M10: replace `top_values` with `[]` instead of placeholders (AT-5 red) |

## 7. Acceptance tests (red first)

Engine (`tests/unit/profiling/test_value_redaction.py`, `tests/sentry/test_profile_value_registry.py`):

- **AT-1** A frame on the QA shape plus a numeric column, a date column, and a date-like column with one
  value that fails parsing, with canaries in every slot (SSN, unique `full_name`, numeric min and max, date
  min and max, the bad date, a detector miss, categorical labels, and sentinel value and note text).
  Every fixture column in this test meets the value-sensitive predicate. Precondition: before redaction,
  `profile.to_dict()` holds a canary in every `VALUE_FIELDS` slot (the test fails if the fixture leaves one
  empty). Then `json.dumps(profile_document(p))` contains no canary, and `include_values=True` contains all
  of them. Red today: there is no `profile_document`, and `to_dict()` holds them all.
- **AT-2** A `status` column that is not value-sensitive keeps its `top_values`, `mode_value` and labels.
- **AT-3** Registry sentry: walk `typing.get_type_hints` from `ColumnProfile` (the same walk as
  `profile_types()`); for every reached dataclass and every field, the pair is in exactly one of
  `VALUE_FIELDS` and `VALUE_FREE_FIELDS`; no registry entry names a type or field the walk does not reach.
  Bite proof: build an augmented class named `FieldStats` with one
  `(f.name, get_type_hints(FieldStats)[f.name], dataclasses.field(default=None))` triple per real field plus
  `("extra", str, dataclasses.field(default=""))`, then a synthetic root whose
  `fields: list[AugmentedFieldStats]`; `_profile_types(synthetic_root)` fails with exactly one unclassified
  pair, `("FieldStats", "extra")`.
- **AT-3b** Property test over `VALUE_FIELDS`: for each entry, build a `ColumnProfile` by hand with a
  value-sensitive column whose slot holds a canary, run `profile_document`, assert the canary is gone. An
  entry the walker never applies fails here.
- **AT-4** `redact_profile_dict(redact_profile_dict(d)) == redact_profile_dict(d)`; the input is never
  mutated; a document with the marker set but raw values inside is still redacted; `include_values=True`
  keeps an existing marker.
- **AT-5** For a value-sensitive categorical, date and numeric column, `len(distribution.labels) ==
  len(distribution.data)` after redaction, and every `top_values` entry keeps `count` and `pct`.
- **AT-6** S1a: `scan_set_to_dict(s, detail=True)` holds no canary; `include_values=True` does; every
  `FieldCore` field taken from `FieldStats` is in `VALUE_FREE_FIELDS`. Written by whichever of S1a and S1
  lands second.
- **AT-12** A fresh value-sensitive profile containing date, numeric and string sentinels has both
  `SentinelFlag.value` and `SentinelFlag.note` replaced with the placeholder; `include_values=True` keeps
  both raw fields. A loaded document gets the same replacement without consulting a fixed-value list.

CLI (`tests/sentry/test_scan_value_redaction.py`, registered in `tests/sentry/test_sentry_manifest.py`;
`gates.sh` lists the `tests/sentry` folder, so it needs no change):

- **AT-7** `decoy scan f.csv --output s.json` on the QA shape (names, SSNs, and an unflagged diagnosis
  control): distinct canaries in the value-sensitive columns are absent, while the diagnosis control remains;
  `value_policy` is `flagged-redacted/v1`. With `--include-values` the sensitive canaries are present, the
  marker is `raw`, and stderr has the warning line with the value-sensitive column count.
- **AT-8** Same for `--output -`.
- **AT-9** `decoy scan show`, `--json` and human with `COLUMNS=10000` (same wide-console reason as
  `test_profile_raw_value_isolation.py:12-24`), on seven hand-written fixtures: legacy raw (no marker), forged
  `flagged-redacted/v1` marker over raw values, string `top_values` entries, a non-list `top_values`, a
  sentinel whose `value` and `note` both contain the canary, a field with missing predicate inputs, and a
  field with `detector_matches={}`, `pii_score=False`, and `is_likely_unique=0`. Each: no canary, no
  traceback, exit code equal to today's. With `--include-values` the canary shows; a legacy file also reports
  `value_policy: raw`. Bite proof as M8.
- **AT-10** AST guard over `src/decoy/**`: no `.to_dict()` call whose receiver text ends in `profile`; in any
  module that imports `profile_columns` or `ColumnProfile`, no `.to_dict()` and no `dataclasses.asdict()`
  call at all; no function body outside the engine compares `pii_score` with a threshold together with
  `detector_matches` and `is_likely_unique` (a copied predicate). Named limit: the AST check is syntactic;
  AT-7 to AT-9 catch the output either way.
- **AT-11** Exit codes of `scan`, `scan show`, `demo` on the AT-7 inputs equal the pre-change codes. This is
  an unchanged-contract regression and is green before implementation by design; AT-7 to AT-10 are the
  fail-before acceptance proof.
- `decoy demo` writes `scan.json` with the marker (added to `tests/e2e/test_demo_command.py`).
- `tests/e2e/test_storm_explore.py::test_storm_show_includes_top_values_table` (`:201-205`) asserts "Alice"
  for a column the predicate now flags: S2b changes it to assert `[redacted]` by default and "Alice" with
  `--include-values`.

## 8. Owner questions (each answerable alone; recommended answer first)

- **OQ1. Which columns are redacted by default?** Recommend (a) value-sensitive only, as section 3 defines,
  for V1: the QA case is covered, and values in other columns are what make strategy choice useful; S1c
  makes the default `--output` value-free core anyway. Known cost: a repeating full-name column with no name
  token in its header, and the FK side of an identifier, keep raw values. (b) Redact every column unless
  `--include-values`: closes those gaps and the `diagnosis` gap, loses the categorical hints. (c) Add a
  fail-closed rule 5: a column where at least 70% of non-null values match the 2-to-3-token person-name
  shape (`_PERSON_NAME_RE`) is value-sensitive regardless of header; over-redaction only costs usefulness.
- **OQ2. Should `ColumnProfile.to_dict()` itself redact by default?** Recommend no for V1: the platform
  stores it in the tenant's own scan row (`api/storm/router.py:1360`) and rebuilds dataclasses from it
  (`helpers.py:756`); flipping it is a platform change (F1). The engine docstring on `to_dict()` names it as
  the raw form and points at `profile_document`.
- **OQ3. Flag name.** Recommend `--include-values` (mirrors DLP `include_quote`). Alternative `--raw-values`.

## 9. Reuse inventory

| Need | Reused | Not reused, why |
|---|---|---|
| Serialize a profile | `ColumnProfile.to_dict()` (`types.py:195`) feeds `profile_document` | |
| Write the file | CLI `write_local_target` (`scan.py:775`) | |
| PII threshold | S1a `PII_CANDIDATE_THRESHOLD` (`scan_set.py:59`) or this plan's, one survives | CLI display cut-offs (F3) |
| Dataclass discovery | `dataclasses.fields` plus `typing.get_type_hints` | no hand-kept type or path table |
| Canary sentry shape | CLI `tests/sentry/test_profile_raw_value_isolation.py` (wide console, bite proof) | extended in a sibling file, not edited: it covers `decoy profile` |
| Public import rule | CLI `test_engine_import_surface.py` | |
| Placeholder idea | Presidio `replace`, ydata `sensitive` | no dependency taken |

## 10. Placement

| Item | Where | Size | Why there |
|---|---|---|---|
| registry, type walk, predicate, `redact_profile_dict`, `profile_document` | engine `profiling/_value_redaction.py` (new) | about 200 lines | profiling owns the profile types (`CODEMAP.md:98`); the folder has 7 `.py` modules plus 3 subpackages on main, 8 modules after (9 with S1a) |
| exports | engine `profiling/__init__.py`, `decoy_engine/__init__.py` | +8 lines | public surface for the CLI |
| `to_dict` docstring | engine `profiling/types.py:195` | +3 lines | names the raw form |
| methodology entry | engine `docs/methodology-registry.yaml` | +6 lines | engine CLAUDE.md citation rule |
| engine tests | `tests/unit/profiling/test_value_redaction.py`, `tests/sentry/test_profile_value_registry.py` | about 260 and 110 | |
| CLI wiring | `cli/scan.py` R1 to R4 plus the options, `cli/demo.py` R5 | `scan.py` at most +15 net (1451 now); the engine supplies the count, so the CLI adds two call swaps, one option per command and the warning line | edits in place; no new CLI module (`cli/` is flat at 34 entries) |
| CLI text | `cli/explain.py:417`, `api.py:600-602` docstring | few lines | |
| CLI sentry | `tests/sentry/test_scan_value_redaction.py` plus manifest entry | about 200 | |

## 11. Frozen contracts touched

- **CLI scan JSON** (header comment `scan.py:22-24`): gains top-level `value_policy` and `redacted_columns`;
  field shapes stay compatible (nullable scalar value fields become `null`, lists stay lists, and redacted
  list or sentinel text remains a string placeholder).
  Readers in the CLI (`scan show`, `scan fields`, `scan diff`, `ci/adapters.py:148-170`) read by key and
  tolerate extra members; nothing loads CLI scan files into dataclasses. Old files stay readable (redacted
  at display).
- **`scan show --json` envelope**: gains `value_policy`.
- **Engine public API**: three new exports; nothing removed or changed.
- **S1a `scan-set/v1`**: `detail` content changes (redacted by default); the core schema
  (`FieldCore` `extra=forbid`) is untouched.
- **`docs/cli-reference.md`**: regenerated for the new options (drift-checked by `tests/unit/test_cli_surface.py`).
- **Engine pin in the CLI**: S2a bumps it (escalation trigger: dependency lock, so the CLI full suite runs
  for S2a).

## 12. Slices

| Slice | Repo | Files (at most 10) | Lines | Tests |
|---|---|---|---|---|
| S1 | engine | `_value_redaction.py`, `profiling/__init__.py`, `decoy_engine/__init__.py`, `types.py`, unit test, sentry test, `tests/sentry/test_sentry_manifest.py`, `docs/methodology-registry.yaml`, `CHANGELOG.md` (9) | about 630 | AT-1 to AT-5, AT-3b, AT-12; T1 = `tests/unit/profiling`, `tests/sentry`, `tests/unit/detection` |
| S1a wiring | engine | on whichever of S1a and S1 lands second: `scan_set.py`, its test | about 60 | AT-6 |
| S2a | CLI | `pyproject.toml`, `uv.lock` (engine pin), `cli/scan.py` (R1, R2, `--include-values` on `scan`), `cli/demo.py`, `tests/e2e/test_demo_command.py`, sentry file (AT-7, AT-8, AT-10, AT-11), manifest, `docs/cli-reference.md`, `CHANGELOG.md` (9) | about 330 | AT-7, AT-8, AT-10, AT-11; full CLI suite (pin bump) |
| S2b | CLI | `cli/scan.py` (R3, R4, `--include-values` on `scan show`), `tests/e2e/test_storm_explore.py`, sentry file (AT-9), `cli/explain.py`, `api.py`, `docs/cli-reference.md`, `CHANGELOG.md` (7) | about 300 | AT-9; T1 for the touched files plus `tests/sentry` |

Order: S1, then S2a on the released engine pin, then S2b. S1a wiring rides with S1a if S1 merges first (the
orchestrator passes AT-6 into the S1a brief). S1c's brief carries the post-S1c `--include-values` rule
(section 3).

## 13. Pre-mortem

- **Platform breaks.** Avoided by leaving `to_dict()` raw; the platform keeps its current data (OQ2).
- **A value-sensitive column hides what a user needs for config.** `init` reads the in-memory profile, not
  the file, so wizard suggestions keep working; a user who wants raw values in the file passes the flag.
- **S1a and S1 race on the threshold constant.** Rule in section 3: second lander imports and deletes.
- **A forged or stale marker is trusted.** The redactor never reads it; AT-9 has a forged-marker fixture.
- **Canary sentries pass for the wrong reason** (Rich truncation, an empty fixture slot). Wide console, the
  AT-1 precondition, and bite proofs.
- **A future field or nested type holds values.** AT-3 walks the types from `ColumnProfile`, so both are
  forced into a classification at review time.
- **A user reads `pii_candidate: false` and is surprised a column is redacted.** User text says
  "value-sensitive" and `explain security` lists the rules.

## 14. Follow-ups (recorded, not in V1)

- F1: platform scan row and Profile/Drill UI: decide whether the stored profile should be redacted
  (platform owner call).
- F2: header names that are data (S1a `header_looks_like_data`): redact names in detail when flagged.
- F3: CLI display cut-offs import the engine constant: `_PII_CANDIDATE_THRESHOLD` (`scan.py:61`),
  `_pii_bucket` (`scan.py:409-419`), `demo.py:159`; the platform's 0.5 cut (`router.py:1349`).
- F4: `scan show` human card reads keys that do not exist (`dtype`, `unique_count`, `detector`,
  `scan.py:1082-1090`), so it prints `Detectors ?` (QA finding 6). Owned by CLI-QA1.
- F5: `_init_inference.py:247` reads `confidence_bucket`, which the engine names `confidence`.
- F6: CLI `_safe_field_record` (`scan.py:512-534`) asserts its fields are in `VALUE_FREE_FIELDS`.
- F7: the init wizard's sample display (row 32b, not on CLI main) may want `is_value_sensitive`.
