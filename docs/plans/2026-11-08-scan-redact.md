Status: plan

# QA-SCAN-REDACT: saved scans carry no raw values for PII-flagged columns (roadmap row 24c)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-08. Scope: engine + CLI, V1 blocker (leak class).
Revision 1.

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Design notes, Established solutions), `feature-dev.md`, `architecture.md`,
`reuse-first.md`, `security.md`, `testing.md` (test depth, escalation triggers), `cross-cutting-concerns.md`,
`verification.md`, `catalogs/recurring-defects.md`; the plan contract
(`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md`), `LOCAL-BUILDER-BRIEF.md`; the standing rules
develop-correctly-registries-and-sizing and best-practice-becomes-a-sentry; the V1 bar (function-break, data
loss or a non-admin value leak blocks; a second copy of a central mechanism, or a new module in a flat folder
over 25, always blocks); engine `CODEMAP.md:55,98` (profiling placement); the prior disclosure decision
engine `docs/remediation-source.md:189` (D3, "don't serialize `ColumnProfile.top_values`"); the SCAN-MULTI
S1a branch `feat/scan-multi-s1a` at `919ebd3b`; the code at engine `main` `7a61be3c` and CLI `main`
`994bc7a`; platform `main` `fbaee2a9` for the one platform consumer.

Source: first-user QA, `docs/records/2026-11-08-cli-qa-first-user.md` on CLI branch
`docs/roadmap-cli-qa-intake`, finding 5 ("`scan_patients.json` includes real names and SSNs in
`top_values`; `scan show ssn` prints them ... the format is built to be diffed and checked in CI") and
section 5 ("Redact `top_values` for PII-flagged fields in saved scans (opt in to raw values)").

## 0. Review dispositions

None yet (revision 1).

## 1. FRAME

### Problem

`decoy scan --output scan.json` writes `ColumnProfile.to_dict()` verbatim (CLI `src/decoy/cli/scan.py:775-779`,
stdout stream `:808`). `to_dict()` is plain `dataclasses.asdict` (engine `profiling/types.py:195-197`), so
every value-bearing field the profiler fills lands in the file: up to 5 literal cells per column in
`top_values` (`profiler.py:229-240,355`), the mode, numeric min and max, date min and max, up to 3 invalid
date cells, up to 3 detector misses per detector, and up to 10 categorical labels. `decoy scan show` then
prints the whole field dict in `--json` (`scan.py:1056-1066`) and a "Top values" table in human mode
(`scan.py:1114-1123`). Scan files are meant to be diffed and committed for CI, so real SSNs and names end up in
git history and CI logs. The CLI's inline field output already strips values (`_safe_field_record`,
`scan.py:512-534`); the saved artifact and `scan show` do not, and there is no engine rule that says which
fields are values.

### Guarantee (what V1 promises)

1. A scan document written by any Decoy surface (CLI `--output` file or stdout, `decoy demo`, and the S1a
   scan-set `detail` member once it lands) holds no raw cell value for a PII-flagged column unless the user
   passed `--include-values`.
2. `decoy scan show` never prints a raw value for a PII-flagged column without `--include-values`, including
   for scan files written before this change.
3. One engine function decides what is a value and what is redacted. A new value-bearing field on a profile
   type fails CI until it is classified.
4. Exit codes, the inline `--json` field record, and every value-free field are unchanged.

### Non-goals (V1)

- Unflagged columns keep their values (owner wording: "for PII-flagged columns"; OQ1).
- `ColumnProfile.to_dict()` keeps returning the full form. The platform persists it into the tenant's own
  scan row (platform `api/storm/router.py:1360-1362`) and its Profile/Drill UI reads it; changing that is a
  platform decision (OQ2, follow-up F1).
- Header names that are really data (a CSV with no header row) are not redacted here. S1a already detects
  that case (`header_looks_like_data`); follow-up F2.
- In-memory callers (`decoy.scan()` returns the `ColumnProfile` object, CLI `api.py:581-637`; `decoy init`
  reads it in memory, `init.py:413-415`) are the user's own process, not an output surface. Their docstrings
  point at the safe serializer.
- `decoy fit`'s distribution snapshot (`quality/snapshot.py:143,482`) is a separate artifact with its own
  opt-in (`allow_real_categories`, CLI `fit.py:13`, `:398-422`); unchanged.

### Failure modes

| # | Failure | Guard |
|---|---|---|
| F-1 | A new value field is added to `FieldStats` (or a nested type) and serializes raw | registry sentry AT-3 |
| F-2 | A CLI surface keeps calling `profile.to_dict()` for output | CLI AST guard AT-10 plus canary sentry AT-7..AT-9 |
| F-3 | `scan show` on an old raw file prints values | display-time redaction AT-9 |
| F-4 | Redaction breaks the parallel `distribution.data`/`labels` arrays and the UI or diff crashes | AT-5 |
| F-5 | Redaction is not idempotent and a re-saved file double-marks | AT-4 |
| F-6 | S1a `detail=True` embeds raw `to_dict()` | AT-6 on whichever of S1a and this lands second |
| F-7 | `--include-values` used silently in CI | stderr warning line AT-7 |
| F-8 | The "flagged" predicate drifts between engine and CLI | one predicate, engine-owned; CLI imports it |

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
  private files (X7b `fileaccess/_private.py`) and log fields.

## 3. Design notes

### Choke point and registry

New engine module `src/decoy_engine/profiling/_value_redaction.py` (about 170 lines). It is the only place
that knows which profile fields hold cell values.

```python
REDACTED: Final = "[redacted]"
VALUE_POLICY_KEY: Final = "value_policy"
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
    ("Distribution", "labels"): Rule.PLACEHOLDER_EACH,   # every kind; numeric bin edges come from real min/max
    ("Distribution", "min"): Rule.NULL,
    ("Distribution", "max"): Rule.NULL,
}
VALUE_FREE_FIELDS: Final[frozenset[tuple[str, str]]] = frozenset({...})  # every other field, by name

def is_value_sensitive(field: Mapping[str, Any]) -> bool: ...
def redact_profile_dict(doc: Mapping[str, Any], *, include_values: bool = False) -> dict[str, Any]: ...
def profile_document(profile: ColumnProfile, *, include_values: bool = False) -> dict[str, Any]:
    return redact_profile_dict(profile.to_dict(), include_values=include_values)
```

- It works on the dict form, so the same function serves a fresh profile (`profile_document`) and a loaded
  file (`scan show`). There is no second copy in the CLI.
- `redact_profile_dict` deep-copies, never mutates the input, and sets the top-level `value_policy`. With
  `include_values=True` it returns the copy unchanged apart from `value_policy: "raw"`.
- Idempotent: a document already marked `flagged-redacted/v1` comes back equal. A document with no marker
  (written before this change) is treated as raw.
- `Distribution.labels`: for a flagged column every label is replaced entry by entry with `"[redacted]"`,
  whatever the `kind`. Categorical labels are cells (`_distributions.py:85`), and numeric bin edges come from
  the real min and max (`_distributions.py:46-56`). The `data` array is untouched, so lengths stay equal (F-4).
- `mean_value` and `Distribution.mean` are aggregates, not cells: kept (VALUE_FREE).
- `SentinelFlag.value` is VALUE_FREE: it is always an entry of a fixed list, never the cell text
  (`sentinels.py:34-53`, normalized lower-case at `:228-237`, date `isoformat` of a fixed date at `:181`,
  numeric from the fixed key at `:218`). AT-12 pins that.
- `DetectionSignal.signal` holds detector ids and header names (`name-hint · col="ssn"`), VALUE_FREE under
  the header non-goal.

### What "PII-flagged" means

`is_value_sensitive(field)` is true when any of these holds:

1. `detector_matches` is non-empty: any built-in or custom detector fired at any confidence bucket
   (`detectors.py:155-160` builds the match; buckets `:165-178`). Format detectors (`iso_date`, `us_zip`)
   count: dates of service and ZIPs are identifiers under HIPAA Safe Harbor.
2. `pii_score >= PII_CANDIDATE_THRESHOLD` (0.6). With the current scorer this implies rule 1 (`_score_pii`
   returns 0.0 without a match, `profiler.py:55-58`); it is kept so a future scorer that rates a column
   without a detector still redacts.
3. `is_likely_unique` is true: a near-unique column (names, emails, free identifiers) is identifying even
   when no detector fires.

Unflagged columns (low-cardinality codes, statuses, amounts with no detector) keep their values: that is
what makes `top_values` useful for picking strategies. The QA gap "`diagnosis` none detected" (finding 6)
is the cost; OQ1 offers the all-columns default.

`PII_CANDIDATE_THRESHOLD` is defined once. S1a defines it in `scan_set.py:59` on its branch; this plan
defines it in `_value_redaction.py`. Whichever lands second imports from the first and deletes its own (one
constant, F-8). The CLI's `_PII_CANDIDATE_THRESHOLD` (`scan.py:61`) and `demo.py:158` are display bucketing,
recorded as follow-up F3, not touched here.

### Opt-in flag

- `decoy scan --include-values`: the saved file and the `--output -` stream carry raw values and
  `value_policy: "raw"`. A one-line stderr warning names the count of flagged columns whose values were kept.
- `decoy scan show --include-values`: show what the file holds. It cannot restore values a redacted file
  never stored.
- Engine API: `profile_document(profile, include_values=...)` and `redact_profile_dict(...)`, both exported
  from `decoy_engine` (CLI may import only public names, CLI `tests/sentry/test_engine_import_surface.py`).
- S1a `scan_set_to_dict(s, *, detail, include_values=False)`: `detail=True` embeds `profile_document(...)`
  instead of `profile.to_dict()` (`scan_set.py:459-473` on the branch).
- LIB L2 `--output-detail` (CLI `docs/ROADMAP.md:182-186`): passes `include_values=True` only when the user
  also passes `--include-values`. The S1a plan rev 6 makes `--output` value-free core from S1c; detail goes
  to protected library scratch (S1a plan lines 92,107). This plan adds: detail is redacted by default too.

## 4. Behavior

| Command | Before | After (default) | After `--include-values` |
|---|---|---|---|
| `decoy scan f.csv --output s.json` | raw `to_dict()` | flagged columns redacted, `value_policy: flagged-redacted/v1` | raw, `value_policy: raw`, stderr warning |
| `decoy scan f.csv --output -` | raw JSON line | redacted JSON line | raw JSON line, stderr warning |
| `decoy scan f.csv` / `--json` | `_safe_field_record` | unchanged | flag accepted, no effect on inline output |
| `decoy scan show s.json col --json` | whole field dict | field dict after `redact_profile_dict` | field dict as stored |
| `decoy scan show s.json col` (human) | Top values table with cells | table with `[redacted]` and counts | cells as stored |
| `decoy scan fields` / `scan diff` | value-free | unchanged | unchanged |
| `decoy demo` | `scan.json` raw | `scan.json` via `profile_document` | n/a (demo data is synthetic; no flag) |
| `decoy explain security` | "scan JSON ... can contain top values" | names the redaction and `--include-values` | |

Exit codes: unchanged on every path; no new error path.

## 5. Enumerations (from the code at engine `7a61be3c`, CLI `994bc7a`)

### 5.1 Exit paths

- `decoy scan`: success 0; load and write errors keep their current codes (`scan.py:780-800`,
  `EXIT_RUNTIME`). Redaction cannot raise on a well-formed profile; a malformed loaded file in `scan show`
  already fails in `_load_scan_dict` (`scan.py:384`) before redaction runs.
- `decoy scan show`: field-not-found `EXIT_USAGE` (`scan.py:1049`) unchanged.
- `decoy demo`: unchanged.

### 5.2 Routes (every way a profile becomes output)

| # | Route | Anchor | After |
|---|---|---|---|
| R1 | scan `--output` file | CLI `scan.py:775-779` | `profile_document` |
| R2 | scan `--output -` stream | CLI `scan.py:808` | `profile_document` |
| R3 | scan show `--json` | CLI `scan.py:1056-1066` | `redact_profile_dict` on load |
| R4 | scan show human tables | CLI `scan.py:1114-1134` | reads the redacted dict |
| R5 | demo scan.json | CLI `demo.py:151-152` | `profile_document` |
| R6 | S1a scan-set detail | engine branch `scan_set.py:459-473` | `profile_document` |
| R7 | L2 `--output-detail` | CLI roadmap row, not built | through R6 |
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
| `SentinelFlag.value` | `sentinels.py:181,218,237` | fixed list, kept (AT-12) |
| `DetectionSignal.*` | `detection/types.py:116-133` | header text, kept |
| `FieldStats` shape and rate fields, `ColumnProfile.*` | `types.py:63-143,146-192` | kept |

### 5.5 Output surfaces

- Files: R1, R5, R6 and R7.
- Stdout: R2, R3, R4, R9.
- Logs: profiler log lines carry counts and detector ids only (`profiler.py:494-500,569-574,597-603`); no
  value field is logged (grep at `7a61be3c`).
- Error text: scan errors print the exception class only (`scan.py:780-786`).
- Platform DB: R8, out of scope (OQ2).

## 6. Class table

| # | Class | Applies here | Structure that closes it | Sentry or property test | Mutation that turns it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | raw cells in scan files, the stdout stream, `scan show`, S1a detail | `profile_document` / `redact_profile_dict` default-deny over the `VALUE_FIELDS` registry | AT-1 engine canary; AT-7 to AT-9 CLI canary | M1: map `TopValue.value` to `Rule.KEEP` in `VALUE_FIELDS` |
| 2 | Cleanup missing on an exit path | N/A: no temp files or handles are added; writes stay on `write_local_target` | N/A | N/A | N/A |
| 3 | State and ordering | a redacted document re-redacted or re-marked; input mutated in place | deep copy then redact; the marker is set last; an already-marked doc is returned equal | AT-4 (idempotent, input unchanged) | M3: redact the input dict in place instead of a copy |
| 4 | Route parity and silent fallback | a CLI route keeps `profile.to_dict()` (R1, R2, R5) or S1a detail stays raw (R6) | every output route calls the engine serializer; AST guard on `.to_dict()` of a profile in `src/decoy/` | AT-10 AST guard; AT-6 for R6 | M4: restore `profile.to_dict()` at `scan.py:808` |
| 5 | Referential integrity | N/A: no keys or tables; S1a relationships carry names only | N/A | N/A | N/A |
| 6 | Untrusted producer | `scan show` reading a legacy or hand-edited file that claims nothing or claims `raw` | display path always runs `redact_profile_dict` unless `--include-values`; an unmarked doc counts as raw | AT-9 (legacy fixture, no marker) | M6: skip redaction when the marker key is missing |
| 7 | Check then use on a path (TOCTOU) | N/A: no check-then-open is added; `scan show` loads once and redacts the loaded dict | N/A | N/A | N/A |
| 8 | Weak tests and overclaimed evidence | Rich truncation makes a canary check pass with a leak; a registry test that only counts entries | forced wide console (`COLUMNS=10000`); the registry test compares against `dataclasses.fields` both ways and has a bite proof | AT-3 bite proof; AT-1 and AT-7 `include_values=True` must show the canary | M8: drop `COLUMNS=10000` from AT-9 and inject a long canary; the test must still bite |
| 9 | Behavior-preserving refactor drift | `to_dict()` must stay the raw form for the platform (R8) | `to_dict()` untouched; the new serializer wraps it | existing profiler tests plus AT-1 `include_values=True` equals `to_dict()` plus marker | M9: make `to_dict()` call the redactor |
| 10 | Frozen contract drift | scan JSON gains `value_policy`; field types must not change; exit codes unchanged | string fields become `null`, lists stay lists, placeholders are strings; no new exit path | AT-5 (shape and lengths), AT-11 (exit codes), existing scan diff tests | M10: replace `top_values` with `[]` instead of placeholders (AT-5 red) |

## 7. Acceptance tests (red first)

Engine (`tests/unit/profiling/test_value_redaction.py`, `tests/sentry/test_profile_value_registry.py`):

- **AT-1** A frame with a canary SSN in an `ssn` column and a canary name in a unique `full_name` column:
  `json.dumps(profile_document(p))` contains neither canary; `include_values=True` contains both (bite
  proof). Red today: there is no `profile_document`, and `to_dict()` holds both.
- **AT-2** An unflagged low-cardinality `status` column keeps its `top_values`, `mode_value` and labels.
- **AT-3** Registry sentry: for every field of `ColumnProfile`, `FieldStats`, `TopValue`, `Distribution`,
  `DetectorMatch`, `SentinelFlag`, `DetectionSignal` (via `dataclasses.fields`), the pair is in exactly one of
  `VALUE_FIELDS` and `VALUE_FREE_FIELDS`; no registry entry names a field that does not exist. Bite proof:
  a test dataclass subclass with an extra field fails the check.
- **AT-4** `redact_profile_dict` on its own output is equal; it never mutates its input; an unmarked doc
  is treated as raw; the marker values are the two constants.
- **AT-5** For a flagged categorical, date and numeric column, `len(distribution.labels) ==
  len(distribution.data)` after redaction, and every `top_values` entry keeps `count` and `pct`.
- **AT-6** S1a: `scan_set_to_dict(s, detail=True)` holds no canary; `include_values=True` does. Written by
  whichever of S1a and S1 lands second.
- **AT-12** Sentinel values in a flagged column are kept and are members of the fixed lists.

CLI (`tests/sentry/test_scan_value_redaction.py`, registered in `tests/sentry/test_sentry_manifest.py`):

- **AT-7** `decoy scan f.csv --output s.json` on the QA shape (names, SSNs, a diagnosis column): no canary
  in the file; `value_policy` is `flagged-redacted/v1`; with `--include-values` the canary is there, the
  marker is `raw`, and stderr has the warning line.
- **AT-8** Same for `--output -`.
- **AT-9** `decoy scan show` on a legacy raw file (hand-written fixture, no marker), `--json` and human with
  `COLUMNS=10000` (same wide-console reason as `test_profile_raw_value_isolation.py:12-24`): no canary;
  with `--include-values` the canary shows.
- **AT-10** AST guard: no call `<expr>.to_dict()` whose receiver source text ends in `profile` anywhere in
  `src/decoy/`. Named limit: a profile bound to another name escapes; AT-7 to AT-9 catch the output anyway.
- **AT-11** Exit codes of `scan`, `scan show`, `demo` on the AT-7 inputs equal the pre-change codes.
- `decoy demo` writes `scan.json` with the marker (added to the existing demo test).

## 8. Owner questions (each answerable alone; recommended answer first)

- **OQ1. Redact every column by default, or only flagged ones?** Recommend flagged only for V1, as the QA
  item says: unflagged `top_values` are what make `init` and strategy choice useful, and S1c makes the
  default `--output` value-free core anyway. Alternative: redact all columns unless `--include-values`
  (closes the `diagnosis` gap; costs the useful categorical hints).
- **OQ2. Should `ColumnProfile.to_dict()` itself redact by default?** Recommend no for V1: the platform
  stores it in the tenant's own scan row (`api/storm/router.py:1360`) and its UI reads the values; flipping
  it is a platform change (F1). The engine docstring on `to_dict()` names it as the raw form and points at
  `profile_document`.
- **OQ3. Flag name.** Recommend `--include-values` (mirrors DLP `include_quote`). Alternative `--raw-values`.

## 9. Reuse inventory

| Need | Reused | Not reused, why |
|---|---|---|
| Serialize a profile | `ColumnProfile.to_dict()` (`types.py:195`) feeds `profile_document` | |
| Write the file | CLI `write_local_target` (`scan.py:775`) | |
| PII threshold | S1a `PII_CANDIDATE_THRESHOLD` (`scan_set.py:59`) or this plan's, one survives | CLI `_PII_CANDIDATE_THRESHOLD` is display-only (F3) |
| Canary sentry shape | CLI `tests/sentry/test_profile_raw_value_isolation.py` (wide console) | extended in a sibling file, not edited: it covers `decoy profile` |
| Public import rule | CLI `test_engine_import_surface.py` | |
| Placeholder idea | Presidio `replace`, ydata `sensitive` | no dependency taken |

## 10. Placement

| Item | Where | Size | Why there |
|---|---|---|---|
| registry, predicate, `redact_profile_dict`, `profile_document` | engine `profiling/_value_redaction.py` (new) | about 170 lines | profiling owns the profile types (`CODEMAP.md:98`); folder has 8 modules, 9 after |
| exports | engine `profiling/__init__.py`, `decoy_engine/__init__.py` | +6 lines | public surface for the CLI |
| `to_dict` docstring | engine `profiling/types.py:195` | +3 lines | names the raw form |
| engine tests | `tests/unit/profiling/test_value_redaction.py`, `tests/sentry/test_profile_value_registry.py` | about 220 and 90 | |
| CLI wiring | `cli/scan.py` R1 to R4 plus option, `cli/demo.py` R5 | +35 lines in scan.py (1451 now) | edits in place; no new CLI module (the `cli/` folder is flat at 34 entries) |
| CLI text | `cli/explain.py:417`, `api.py:600-602` docstring | few lines | |
| CLI sentry | `tests/sentry/test_scan_value_redaction.py` plus manifest entry | about 160 | |

## 11. Frozen contracts touched

- **CLI scan JSON** (header comment `scan.py:22-24`): gains top-level `value_policy`; field types keep
  their types (string fields become `null`, lists stay lists, placeholders are strings). Readers in the CLI
  (`scan show`, `scan fields`, `scan diff`, `ci/adapters.py:148-170`) read by key and tolerate the extra
  member; AT-9 and the existing scan diff tests cover it. Old files stay readable (treated as raw).
- **Engine public API**: two new exports; nothing removed or changed.
- **S1a `scan-set/v1`**: `detail` content changes (redacted by default); the core schema
  (`FieldCore` `extra=forbid`) is untouched.
- **Engine pin in the CLI**: S2 bumps it (escalation trigger: dependency lock, so the CLI full suite runs
  for S2).

## 12. Slices

| Slice | Repo | Files (at most 10) | Lines | Tests |
|---|---|---|---|---|
| S1 | engine | `_value_redaction.py`, 2 `__init__.py`, `types.py`, 2 test files, `CHANGELOG.md`, `docs/what-we-cannot-prove.md` (supersede D3 disclosure line) | about 560 | AT-1 to AT-5, AT-12; T1 = `tests/unit/profiling`, `tests/sentry`, `tests/unit/detection` |
| S1a wiring | engine | on whichever of S1a and S1 lands second: `scan_set.py`, its test | about 40 | AT-6 |
| S2 | CLI | `pyproject.toml` and `uv.lock` (engine pin), `cli/scan.py`, `cli/demo.py`, `cli/explain.py`, `api.py`, sentry file, manifest, `CHANGELOG.md` | about 300 | AT-7 to AT-11; full CLI suite (pin bump) |

Order: S1, then S2 on the released engine pin. S1a wiring rides with S1a if S1 merges first (the
orchestrator passes AT-6 into the S1a brief).

## 13. Pre-mortem

- **Platform breaks.** Avoided by leaving `to_dict()` raw; the platform keeps its current data (OQ2).
- **A flagged column hides what a user needs for config.** `init` reads the in-memory profile, not the
  file, so wizard suggestions keep working; a user who wants raw values in the file passes the flag.
- **S1a and S1 race on the threshold constant.** Rule in section 3: second lander imports and deletes.
- **scan diff noise.** Two redacted files compare equal on redacted fields; a raw-vs-redacted pair shows
  value changes. Accepted; the marker says why.
- **Canary sentries pass for the wrong reason** (Rich truncation). Forced wide console, as the existing
  sentry does.
- **A future field named innocuously holds values.** AT-3 forces a classification decision at review time.

## 14. Follow-ups (recorded, not in V1)

- F1: platform scan row and Profile/Drill UI: decide whether the stored profile should be redacted
  (platform owner call).
- F2: header names that are data (S1a `header_looks_like_data`): redact names in detail when flagged.
- F3: CLI `_PII_CANDIDATE_THRESHOLD` (`scan.py:61`) and `demo.py:158` import the engine constant.
- F4: `scan show` human card reads keys that do not exist (`dtype`, `unique_count`, `detector`,
  `scan.py:1082-1090`), so it prints `Detectors ?` (QA finding 6). Owned by CLI-QA1.
- F5: `_init_inference.py:246-247` reads `confidence_bucket`, which the engine names `confidence`.
