Status: plan

# SCAN-MULTI: one scan over many tables, one starter pipeline YAML

Owner: Cam. Date: 2026-11-05. Scope: CLI V1 (engine and CLI repos).

Rules consulted: `dev-rules/README.md`, `00-universal.md`,
`risk-and-exceptions.md`, `development-loop.md`, `feature-dev.md`,
`api-and-compatibility.md`, `cross-cutting-concerns.md`, `reuse-first.md`,
`testing.md`, `security.md`, `architecture.md`, `documentation.md`.

Risk: R2. The work adds public engine API, a new persisted artifact tag, a new
optional config field, a CLI flag surface, a local snapshot store and a
file-reading entry point under the file-access policy, and its output must stay
value-free. Nothing here is destructive or irreversible. Approver: Cam.

Owner direction, 2026-11-05:

- "a decoy scan should be able to scan multiple files at once in order to
  build the single YAML for the pipeline."
- "Let's park that for V2, users can set up their own FKs, but we do want
  multi-file discovery at once. 1 scan for 1 pipeline build, on however many
  tables the user selects."
- "assume we have a CSV with no headers. User must either define source path
  where header file is stored or define headers in the yaml config itself. all
  columns must be defined, even if it's just passthrough."
- Scan reports source integrity facts (keys, nulls, orphan counts only for
  relationships the user declares) and stores its result as a versioned
  snapshot in a local library that the YAML can reference as a baseline.

V1 is S1 (one `decoy scan` over every selected table, with integrity facts and
a snapshot library) and S3 (that scan writes one starter pipeline YAML). The
value-based foreign key discovery slice (S2 in the first draft) is a V2 design
in section 7. V1 infers no relationships of any kind, and the name-only
cross-file walk (`decoy_engine.walks.run_cross_file_walk`) is not wired into the
CLI.

## Revision 2 (2026-11-05): changes since head 2a391193

For the reviewer's delta. Everything else is unchanged.

1. **Explicit headers.** Scan never assumes a header. A CSV needs
   `--has-header` or `--no-header` (positional files) or an explicit
   `has_header` in the config. A headerless file needs `--column-names` or
   `--column-names-file`. The header guard only warns; it never renames.
   Removed: the "assume a header" default, the `column_N` rename, Q1.
2. **Every column listed.** The emitted YAML always writes `has_header` for CSV
   sources, plus `column_names` or `column_names_file` when headerless, and one
   column entry per column; columns with no detector suggestion get
   `passthrough` (new Q9 on that default).
3. **`column_names_file` dependency.** The field is added by the separate
   CONFIG-EXPLICIT row. S1c's `--column-names-file` flag and S3's emission of
   `column_names_file` depend on that row; neither slice adds the field.
4. **Integrity facts.** New slice S1b (engine): key, null and duplicate-key
   facts, and orphan counts only for relationships declared in `--config`.
5. **Snapshot library.** New slice S1d (CLI): every scan saves a value-free
   `scan-set/v1` snapshot under `.decoy/scans/` by default. S3 adds an optional
   `scan_baseline:` config field and `decoy scan --config p.yaml --compare`.
   `scan-set/v1` is now value-free at its core, with rich per-table detail only
   in explicit `--output` files.
6. **Slices re-cut.** S1 becomes S1a (engine scan), S1b (engine integrity),
   S1c (CLI scan), S1d (CLI library). Sizes in section 5.
7. **Owner answers recorded.** Q2 keep `decoy init FILE` as the one-file form;
   Q3 files only; Q4 local output directory for targets that cannot sit beside
   the source; Q5 keep the legacy strategy table until the platform resumes;
   Q6 fail the whole scan, `--keep-going` later.
8. **Reason registry.** No hard-coded version: each engine slice takes the next
   free minor at build time (SUB-STOP goes first, then STORM E1).

## 1. FRAME

### Problem

`decoy scan SOURCE` takes one file. A production pipeline masks several tables
from one YAML. Today a user scans each file separately and then writes the
multi-table YAML by hand, or runs `decoy init FILE`, which scaffolds a
single-table YAML with one hard-coded table named `data` and silently assumes a
CSV header. There is no path from "here are my five files" to "here is one
explicit pipeline YAML for all five", and nothing tells the user whether the
keys they plan to declare actually hold in the data.

The platform will later run the same flow (pre-scan a group of tables, then
build a pipeline). So the multi-table scan, the integrity facts and the
starter-config builder belong in the engine, with the CLI as a thin caller. The
snapshot library is CLI-only (the platform stores scans in its database).

### Guarantee (what V1 promises)

1. `decoy scan A B C ...` and `decoy scan --config p.yaml [--table T ...]`
   profile every selected table in one invocation and report each one under
   the existing scan honesty and privacy rules (H4: PII candidates are
   suggestions only; H5: privacy-safe field records).
2. Scan never guesses a header. Every CSV source states `has_header`; every
   headerless source supplies its column names.
3. Profiling memory is bounded per table: tables are scanned one at a time,
   each through the engine's bounded `ProfileSource` sample, and a table's
   frame is released before the next one is read. The integrity pass reads only
   key columns, streamed into DuckDB under a memory limit with disk spill.
4. Scan reports integrity facts: per table, null counts and key candidates in
   the scanned rows; per relationship declared in `--config`, exact parent
   duplicate-key count, child null-key count and orphan row count. It never
   prints a key value.
5. Every scan saves a value-free `scan-set/v1` snapshot to the local library
   (default `.decoy/scans/`) unless `--no-save` is given.
6. `--emit-config p.yaml` writes one starter pipeline YAML: an explicit source
   and a target for every scanned table, one column entry for every column with
   a suggested strategy (`passthrough` when nothing is suggested), `# REVIEW:`
   comments, a `scan_baseline:` reference to the saved snapshot, and a
   commented-out `relationships:` template naming the scanned tables. The file
   passes `decoy validate config` and `decoy validate compile`.
7. No human output, `--json` output, library snapshot or emitted YAML contains a
   data value. Only an explicit `--output` file carries the engine's richer
   per-table detail, under today's existing rule.
8. A single-file `decoy scan FILE.parquet` keeps today's `--json` keys and
   `--output` artifact shape. A single CSV now needs a header flag (a
   deliberate pre-GA change, section 5 S1c).

### Non-goals

- No relationship inference, name-based or value-based. The user writes
  relationships; the YAML gives a commented template only.
- No orphan or key checks for relationships the user has not declared.
- No cross-table re-identification risk.
- No database tables in V1 scan selection (owner answer Q3).
- No decision on how scan relates to the in-run pre-scan, post-scan and
  data-integrity checks, on which YAML features are standard versus opt-in, or
  on whether `decoy run` reads `scan_baseline`. A separate UX discussion
  settles that. Section 4 shows how S1 and S3 work with either answer.
- No change to the engine's legacy `DEFAULT_STRATEGY_BY_DETECTOR` table, which
  the paused platform still reads (owner answer Q5).
- No snapshot pruning or retention policy in V1 (Q10).
- No new masking behavior.

### Failure modes

| Case | V1 behavior |
|---|---|
| CSV given with neither `--has-header` nor `--no-header` | Usage error (exit 2) before any read, naming the file. Hint shows both flags. |
| `--config` CSV source without an explicit `has_header` | Usage error naming the source. This CLI check is deleted when CONFIG-EXPLICIT makes the field required in the schema. |
| `--no-header` with no `--column-names` / `--column-names-file` | Usage error. The engine's `FileSource` already rejects `has_header: false` without names. |
| Column-name count does not match the file's width | The engine reader's existing record-width error, reported with the table name and error class only. |
| Header given (`--has-header`) but the first row looks like data | The header guard runs the STORM detectors over the header cells. If a cell matches a PII detector, or every cell parses as a number or a date, a `warning.scan_header_suspect` warning names the table (never the cell). The guard never changes the reading. |
| Two files share a stem, or a stem is not a usable table name | Usage error before any read for a collision (no silent suffixing). Unusable names are normalized to `[a-z0-9_]`, a leading digit gets a `t_` prefix, and the card shows `name <- path`. |
| Huge file | Bounded profiling sample per table (`--rows`, default 10 000; `--rows 0` reads everything). The integrity pass reads full key columns and the card says so; `--skip-integrity` skips it. |
| Declared parent and child keys have different types (CSV string against parquet int) | Fact `key_type_mismatch`, a `warning.scan_key_type_mismatch` warning, no orphan count. No guessing or casting. |
| Nulls in a declared child key | Excluded from orphans and counted separately (SQL `MATCH SIMPLE`, the dbt `relationships` rule). |
| Duplicate keys in a declared parent | Reported as a count; the relationship is still checked. |
| Self-reference or cycle in declared relationships | Checked like any other edge; scan does not reject a cycle (the compiler does when the user runs the config). |
| One table fails to read or profile | Exit 3, naming the table and the exception class only (never `str(exc)`, which can carry cell data). No `--output`, snapshot or `--emit-config` file is written (owner answer Q6). |
| Snapshot directory not writable | Exit 3 with the path; `--no-save` is the hint. |
| `--emit-config` target already exists | Refused without `--yes`; always refused when it is the same path as `--config`. |
| `--config` names an unknown table, or positional files are mixed with `--config` | Usage error. |
| Cloud source without the cloud extra | Existing engine error, mapped to the existing extras hint. |

## 2. Established solutions

Searches run on 2026-11-05 (web search plus the engine and CLI code at engine
41a86f47 and CLI 8fba6af). What the design follows:

- **Multi-table metadata from a set of tables:** SDV
  `Metadata.detect_from_dataframes` builds one metadata object for a mapping of
  table name to frame and warns that the result must be reviewed
  (https://docs.sdv.dev/sdv/multi-table-data/data-preparation/creating-metadata).
  S1a follows its input shape and its review-required posture.
- **Scaffold, then review:** `dbt init` and cookiecutter, already cited by the
  CLI scaffolder (`decoy/cli/_init_inference.py`).
- **Bounded reads:** the engine's `profile._readers.build_profile_source`
  (metadata-first, bounded `sample_frame`; the Spark and Dask
  deferred-materialization pattern cited in that module), which also covers S3
  and GCS sources.
- **Orphan counts:** the dbt `relationships` test semantics (every non-null
  child key must exist in the parent; nulls pass), computed as a DuckDB
  anti-join with `memory_limit` and a temp directory for spill, through the
  engine's existing `execution/out_of_core/_duckdb.connect_duckdb` helper. The
  engine already counts orphans this way in
  `execution/out_of_core/_batch_join.py` (`count_batch_orphans`); S1b reuses
  the connection helper and the register-a-batch pattern, not the masked-key
  joiner itself.
- **Header detection:** `csv.Sniffer.has_header` was considered and rejected
  even as a warning source: it votes only on columns with a fixed type or
  length, so it calls an all-text header "no header". The guard reuses the
  STORM detectors and only warns.
- **Snapshot store:** content-addressed, append-only files (the layout git uses
  for objects, and the shape of dbt's `target/` artifacts): one immutable JSON
  per scan, named by time and content hash, written atomically (temp file plus
  `os.replace`).
- **Atomic output:** the publish shape the engine's `fileaccess._output` uses.

No custom algorithm remains in V1. The V2 FK survey is in section 7.

## 3. Design notes

- **New structure:** one engine entry point (`scan_sources`) with frozen result
  types and a declared-relationship integrity pass, one engine starter-config
  builder, one optional config field, a CLI snapshot library, and one CLI YAML
  renderer shared by `decoy init FILE` and `decoy scan --emit-config`.
- **Library code does not know its callers** (engine `CLAUDE.md`): the engine
  returns data (`ScanSet`, `StarterConfig`); the CLI owns cards, flags, the
  snapshot directory, comment wrapping and file writes. The platform can call
  the same engine functions and store results its own way.
- **One place per fact:** the V2-shaped detector-to-strategy table moves into
  the engine once (S3-engine) and the CLI copy is deleted (S3-cli); a parity
  test pins the port. Header explicitness ends up in the config schema
  (CONFIG-EXPLICIT); the temporary CLI check is deleted then.
- **Choke points.**
  - File reads: `scan_sources` is the one file-reading entry point for scan
    (the integrity pass is a keyword of it, not a second entry point). It is
    decorated `@fileaccess.entry_point`, takes `file_access`, and is added to
    `fileaccess.ENTRY_POINTS`. The existing file-access sentries
    (`tests/sentry/baselines/file-access-entry-seams.json` and the registry
    baseline) fail if it reads outside the policy. Mutation M-FA: drop the
    decorator; the entry-seam sentry goes red.
  - YAML emission: `decoy/cli/_scaffold_yaml.py` is the only code that writes a
    pipeline YAML from a scan. Guard G-ONE-EMITTER runs `decoy init x.csv
    --has-header` and `decoy scan x.csv --has-header --emit-config` and asserts
    identical bodies after the provenance header. Mutation M-EMIT (on a scratch
    copy, since the old builder is deleted): route init through its old
    `_build_scaffold_yaml`; the guard goes red.
  - Snapshot writes: one function, `_scan_library.save_snapshot`, writes under
    the library. Guard G-ONE-SAVER walks the CLI package syntax tree for writes
    of `scan-set/v1` payloads outside it. Mutation M-SAVE: write the snapshot
    inline in `scan.py`; the guard goes red.
  - Value-free output: `_safe_field_record` stays the only field renderer for
    human, `--json` and snapshot output. `scan_set_to_dict(..., detail=False)`
    is the only snapshot serializer and has no path to `top_values`, min, max
    or mode.
- **No registry for one site:** table-name normalization is one CLI function.

## 4. Compatibility with the open UX question

The UX discussion may make scan a step inside `decoy run`, keep it a separate
verb, or both, and may move features between standard and opt-in:

- `scan_sources` returns the same `ColumnProfile` the in-run pre-scan builds
  with `profile_columns`, and its integrity facts are the same counts a
  data-integrity check would report, so an in-run step can call it unchanged.
- The emitted YAML sets what a pipeline needs to run plus `scan_baseline`. It
  writes no `post_scan:`, `validators:`, `quarantine:` or `subset:` block.
- `scan_baseline` is read only by `decoy scan --compare` and
  `decoy validate preflight` in V1. Whether `decoy run` reads it is left open.
- `StarterConfig` is a plain config dict plus notes, so adding opt-in blocks
  later is an additive builder field.

## 5. Slices

Order: S1a, S1b (engine, can be built in parallel; S1b's tests need S1a's
types), engine release and CLI repin, S1c, S1d, then S3-engine, engine release
and repin, S3-cli. CONFIG-EXPLICIT (separate row) must merge before S1c's
`--column-names-file` flag and before S3. The CLI pins `decoy-engine==0.18.0`;
follow the engine repin checklist. Reason identities take the next free
registry minor at build time; SUB-STOP and STORM E1 go first.

Sizes are hand-written lines, code plus tests, estimated.

### S1a-engine: `scan_sources` and `scan-set/v1` (engine, ~450 lines)

**Files.**
- new `src/decoy_engine/profiling/scan_set.py`: `scan_sources`, `TableScan`,
  `ScanSet`, `scan_set_to_dict`, `scan_set_from_dict`.
- `src/decoy_engine/profiling/__init__.py`, `src/decoy_engine/__init__.py`:
  additive exports.
- `src/decoy_engine/fileaccess/__init__.py`: add to `ENTRY_POINTS`; sentry
  baselines refreshed through their own tooling.
- `src/decoy_engine/reasons/warning/_part1.py`: `warning.scan_header_suspect`;
  manifest fixture and the next free registry minor.
- `docs/compatibility-contract.md` (§3.1 tag list, §3.4 note),
  `docs/reasons.md`, `CHANGELOG.md`.
- tests: `tests/unit/profiling/test_scan_set.py`.

**Behavior.**

```python
def scan_sources(
    sources: Mapping[str, dict],          # SourceDescriptor dicts
    *,
    tables: Sequence[str] | None = None,  # selection; default all; order kept
    sample_rows: int | None = 10_000,     # None = whole table
    strategy: Literal["head", "random", "full"] = "head",
    relationships: Sequence[dict] = (),   # RelationshipConfig dicts (S1b)
    integrity: bool = True,               # S1b
    file_access: FileAccess | None = None,
) -> ScanSet
```

- Validates each descriptor through `SourceDescriptor` and reads it exactly as
  declared (`has_header`, `column_names`, and `column_names_file` once
  CONFIG-EXPLICIT adds it). No header inference.
- For each selected table, in order: `build_profile_source(...)`, bounded
  sample, header guard (when `has_header` is true), `profile_columns(...)`,
  drop the frame.
- Header guard: detectors over the header cells as a one-row frame; on a match,
  or when every cell parses as a number or a date, attach a
  `warning.scan_header_suspect` `ReasonRef`. Reading and names are unchanged.
- `ScanSet` (frozen): `tables`, `relationships` (S1b facts, empty here),
  `scan_schema = "scan-set/v1"`, `engine_version`, `scanned_at`.
  `TableScan`: `name`, `descriptor`, `profile`, `warnings`,
  `row_count_exact`, `key_candidates` (columns unique and non-null within the
  scanned rows, labeled as sample-based), `null_counts`.
- `scan_set_to_dict(s, *, detail: bool)`: the value-free core always (per table
  the descriptor, `_safe_field_record`-equivalent field facts, key candidates,
  null counts, warnings, reid score and QI groups as names); with
  `detail=True`, an optional per-table `detail` member carrying
  `profile.to_dict()`. `scan_set_from_dict` reads both. The safe field-record
  shape moves into the engine here so CLI and snapshots share it.
- Logging: one operational line per table (name, rows scanned, duration).

**Acceptance tests (written first) and named mutations.**

| Test | Asserts | Mutation that must turn it red |
|---|---|---|
| T1-order | `tables=[c, a]` gives exactly `c, a` in that order | M1-order: iterate `sources` |
| T1-bounded | 50 000-row parquet, `sample_rows=1000`: at most one batch past 1000 rows read (reader spy) | M1-full: `to_frame()` instead of `sample_frame` |
| T1-sequential | at most one table frame alive while three tables scan (weakref probe) | M1-hold: keep frames in a list |
| T1-explicit-header | a CSV source with `has_header: false` and `column_names` profiles the first row as data under those names | M1-sniff: infer the header |
| T1-header-guard | `has_header: true` on a headerless CSV whose first row holds an email: warning attached, names unchanged, the warning text has no cell content | M1-rename: rewrite names when suspect |
| T1-header-clean | header `first_name,email` raises no warning | M1-overeager: warn on any all-string header |
| T1-core-value-free | canary in every cell: `scan_set_to_dict(s, detail=False)` contains no canary | M1-leak: put `top_values` in the core |
| T1-roundtrip | both forms round-trip; unknown `scan_schema` major raises | M1-schema: drop the check |
| T1-fileaccess | deny-all `FileAccess` raises `decoy.file_access_denied` before any read | M-FA |
| T1-error-class | a corrupt parquet raises with the table name and no cell text | M1-exc: interpolate `str(exc)` |

**Frozen-contract effects.** Additive public API (§4.1). New persisted tag
`scan-set/v1` (§3.1); the optional `detail` member is additive; a core shape
change mints `v2` with a kept `v1` reader. Snapshots will be read across engine
versions, so a `scan-set/v1` sample joins the compatibility corpus. One warning
identity at the next free registry minor. No determinism effect.

### S1b-engine: declared-relationship integrity facts (engine, ~480 lines)

**Files.**
- new `src/decoy_engine/profiling/_scan_integrity.py`: key-column streaming and
  the DuckDB anti-join.
- `scan_set.py`: wire the `relationships` and `integrity` keywords; add
  `RelationshipFacts`.
- `reasons/warning/_part1.py`: `warning.scan_key_type_mismatch`.
- tests: `tests/unit/profiling/test_scan_integrity.py`.

**Behavior.**
- Runs only when `integrity` is true and `relationships` is non-empty. Each
  relationship is validated through `RelationshipConfig`; tables it names must
  be in `sources` (they need not be in the profiling selection).
- Streams only the key columns: parquet through `LazySource.iter_batches`
  with column projection; CSV through a new key-column chunk reader on the
  existing policy-bound `CsvFileSource` handle (pandas `chunksize` plus
  `usecols`); cloud sources through their readers' batch path; fixed width is
  read whole for its key columns (no bounded fixed-width iterator exists) and
  the fact says so.
- Batches are registered into a DuckDB connection from `connect_duckdb` with a
  memory limit and a temp directory, then: parent rows, parent rows with a null
  key component, parent duplicate keys (`count(*) - count(distinct key)`),
  child rows, child rows with any null key component, orphan rows (anti-join on
  all key columns, null child keys excluded) and orphan distinct keys.
  Composite keys work because the anti-join is on every listed column.
- Key types are compared from the Arrow schemas as `decoy run` reads them. A
  mismatch gives `status: key_type_mismatch` and no counts.
- `RelationshipFacts`: `parent`, `children`, `status`, the counts above,
  `exact: bool`. Counts only; no key value is ever returned.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T5-orphans | parent 1..100, child with 7 rows pointing at 101..103 and 5 null keys: orphan rows 7, distinct 3, null 5 | M5-nulls: count null keys as orphans |
| T5-composite | two-column key with one partial match counts as an orphan | M5-first-col: anti-join on the first column only |
| T5-dupes | parent with 4 duplicated keys reports 4 | M5-distinct: drop the duplicate count |
| T5-type-mismatch | CSV string child against parquet int parent gives `key_type_mismatch`, no counts | M5-cast: cast both sides to string |
| T5-undeclared | two tables with matching `*_id` names and no declared relationship produce no facts | M5-infer: derive edges from names |
| T5-bounded | with `memory_limit` set to 64 MB, a 5-million-row child completes and DuckDB's temp directory was used | M5-pandas: do the anti-join in pandas `isin` |
| T5-value-free | canary keys: no canary in `RelationshipFacts` or its dict form | M5-sample: attach example orphan keys |
| T5-skip | `integrity=False` reads no key column (reader spy) | M5-ignore: run regardless |

**Frozen-contract effects.** Additive fields on `ScanSet` and `scan-set/v1`
(the `relationships` member), one warning identity.

### S1c-cli: `decoy scan` over many tables (CLI, ~650 lines)

Depends on S1a and S1b released and repinned; `--column-names-file` also
depends on CONFIG-EXPLICIT.

**Files.**
- new `src/decoy/cli/_scan_multi.py`: argument resolution (positional files to
  explicit descriptors, `--config` loading and `--table` selection, name
  normalization and collisions), the multi-table card, the integrity block and
  the `--json` envelope. Kept out of `scan.py` (already 1442 lines).
- `src/decoy/cli/scan.py`: `SOURCE` becomes `SOURCES...`; new options
  `--config`, `--table` (repeatable), `--has-header` / `--no-header`,
  `--column-names a,b,c`, `--column-names-file PATH`, `--skip-integrity`. Every
  invocation goes through `scan_sources`. `fields`, `show` and `diff` accept a
  `scan-set/v1` file and take `--table` (required when the set holds more than
  one table).
- docs: scan epilog, CLI reference, `CHANGELOG.md`.
- tests: `tests/e2e/test_scan_multi.py`, `tests/e2e/test_scan_command.py`.

**Behavior.**
- Header flags apply to every positional CSV; a mix of header styles needs
  `--config`. `--column-names` and `--column-names-file` require `--no-header`
  and exactly one positional CSV (several headerless files need `--config`).
- `--format` and `--layout` apply only with exactly one positional file.
- `--config p.yaml`: validated through `PipelineConfig`; `sources` and
  `relationships` are read; selection defaults to every source; integrity runs
  for the declared relationships.
- Output: a summary card (table, rows scanned, columns, PII candidates, reid
  risk, key candidates), then an integrity block per declared relationship
  (`orders.customer_id -> customers.customer_id: 7 orphan rows (3 keys), 5 null,
  parent duplicates 0`), then the H4 honesty block once. `--json`:
  `{"command": "scan", "status": "ok", "scan_schema": "scan-set/v1",
  "pii_note": ..., "tables": [...], "relationships": [...]}`; a single parquet
  file keeps today's flat keys. `--output scan.json` writes
  `scan_set_to_dict(detail=True)`; a single file keeps the bare
  `profile.to_dict()`.
- Pre-GA change: a single CSV without a header flag is now a usage error
  (owner rule). CHANGELOG entry; the scan epilog examples gain the flag.
- Exit codes unchanged: 0 ok, 2 usage, 3 runtime. Integrity findings never
  change the exit code in V1.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T2-multi | three files: three tables in argument order in card and `--json` | M2-sort: sort by name |
| T2-header-required | a CSV with no header flag exits 2 before any read | M2-default: default to `has_header: true` |
| T2-headerless | `--no-header --column-names a,b` profiles the first row as data | M2-drop-names: ignore `--column-names` |
| T2-config-header | a config CSV source without explicit `has_header` exits 2 | M2-trust-default: accept the schema default |
| T2-parquet-compat | one parquet file: `--json` keys and `--output` match a golden from 8fba6af (timestamps masked) | M2-envelope: always emit the multi envelope |
| T2-collision | `a/x.csv b/x.csv` exits 2 before any read | M2-suffix: auto-suffix |
| T2-config-select | `--config p.yaml --table t2` profiles only t2 | M2-ignore-select |
| T2-integrity | config with one declared relationship: the card and `--json` show the S1b counts | M2-hide: drop the integrity block |
| T2-atomic | second table corrupt: exit 3, no `--output` file | M2-partial: write per table |
| T2-canary | unique canary in every cell: card, `--show-fields`, `--json`, integrity block, stderr and the error path carry no canary | M2-leak: add `top_values` to the field record |

**Frozen-contract effects.** CLI contract (§3.4): verb unchanged; flags
additive; positional becomes variadic. Breaking pre-GA: a CSV now needs a header
flag. The saved-scan readers accept the bare profile and `scan-set/v1`.

### S1d-cli: the scan library (CLI, ~400 lines)

Depends on S1c.

**Files.**
- new `src/decoy/cli/_scan_library.py`: `save_snapshot`, `load_snapshot`,
  `resolve_library_dir`.
- `src/decoy/cli/scan.py`: `--no-save`, `--library DIR`; `scan diff` accepts
  two snapshot paths or `--latest` (the two newest snapshots for the same table
  set).
- docs, `CHANGELOG.md`; tests: `tests/e2e/test_scan_library.py`, the G-ONE-SAVER
  guard in `tests/unit/test_scan_library_guard.py`.

**Behavior.**
- After a successful scan, write `scan_set_to_dict(detail=False)` to
  `<library>/<UTC yyyymmddThhmmssZ>-<first 12 hex of sha256 of the core>.json`.
  Default library `.decoy/scans/` under the current directory (or the
  `--config` file's directory). Directory mode 0700, file mode 0600, temp file
  plus `os.replace`. Snapshots are immutable; a same-content rescan still gets a
  new time-stamped file.
- The card and `--json` report the snapshot path.
- This reverses the earlier "persist only on `--output`" rule (H7b) by owner
  direction. The snapshot is value-free, so no cell data is persisted by
  default.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T6-saved | a scan writes exactly one snapshot under `.decoy/scans/` with mode 0600 | M6-mode: default umask |
| T6-no-save | `--no-save` writes nothing | M6-ignore |
| T6-value-free | canary fixtures: the snapshot file contains no canary | M6-detail: save with `detail=True` |
| T6-atomic | a failed scan leaves no snapshot and no temp file | M6-early: save before the last table |
| T6-diff-latest | two scans with one column added: `scan diff --latest` reports it | M6-order: pick the oldest two |
| G-ONE-SAVER | no `scan-set/v1` write outside `save_snapshot` | M-SAVE |

**Frozen-contract effects.** New CLI flags; a new on-disk location. The
snapshot format is the engine's `scan-set/v1`.

### S3-engine: `starter_config` and `scan_baseline` (engine, ~520 lines)

Depends on S1a and CONFIG-EXPLICIT.

**Files.**
- new `src/decoy_engine/profiling/starter_config.py`: `starter_config`,
  `StarterConfig`, `ColumnSuggestion`.
- new `src/decoy_engine/profiling/_starter_strategies.py`: the detector to V2
  `ColumnConfig` table ported from the CLI's `_INFERENCE_TABLE`, its REVIEW
  copy and its `_NAMESPACE_REQUIRED_STRATEGIES` rule; the fallback becomes
  `passthrough` (Q9).
- new `src/decoy_engine/config/_scan_baseline.py` and `_pipeline.py`: optional
  `scan_baseline: {path: str, schema: Literal["scan-set/v1"]}`, default `None`.
- exports, `docs/compatibility-contract.md` (§3.4 config note),
  `CHANGELOG.md`; tests: `tests/unit/profiling/test_starter_config.py`,
  `tests/unit/config/test_scan_baseline.py`.

**Behavior.**

```python
def starter_config(
    scan: ScanSet,
    *,
    baseline_path: str | None = None,
    targets: Mapping[str, dict] | None = None,
    seed: int = 42,
) -> StarterConfig
```

- `StarterConfig.config` is a plain dict; the builder runs
  `PipelineConfig.model_validate` and `compile_plan` before returning (a
  failure is a builder bug and raises).
- Sources: the scanned descriptor with every reading field explicit: CSV
  sources always carry `has_header`; headerless ones carry `column_names`, or
  `column_names_file` when the scan used one.
- Targets: local file sources get `<stem>.masked.<ext>` beside the input
  (today's init rule). Cloud sources get a local `masked/` directory next to
  the emitted YAML (owner answer Q4), with a REVIEW note.
- Tables: one entry per table and one column entry for every column. Detector
  suggestions come from the strategy table; a column with no suggestion gets
  `strategy: passthrough` and a REVIEW note saying no PII was detected and the
  column is copied unchanged. Namespaced strategies get
  `namespace: <column name>`.
- `relationships` is the user's declared list from `--config` when present
  (copied, never edited), otherwise `[]`. `StarterConfig.relationship_template`
  lists the scanned table names for the renderer's comment.
- `scan_baseline` is set when `baseline_path` is given.
- `StarterConfig.notes` maps `(table, column)` to REVIEW text built only from
  detector ids, strategy names, integrity counts and fixed copy.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T3-valid | a four-table scan set (CSV with header, headerless CSV, parquet, fixed width) validates and compiles | M3-namespace: omit `namespace` for hash |
| T3-every-column | every column of every table has exactly one entry | M3-skip: drop unsuggested columns |
| T3-passthrough | an unflagged column gets `passthrough` with its REVIEW note | M3-redact: keep the old `redact` fallback |
| T3-explicit-source | every CSV source has `has_header`; the headerless one has its names | M3-implicit: omit `has_header` when true |
| T3-no-inferred-relationships | without `--config`, `relationships == []`, even with matching `*_id` names | M3-infer: add `run_cross_file_walk` edges |
| T3-declared-kept | a declared relationship is copied unchanged | M3-edit: rewrite its `orphan_policy` |
| T3-baseline | `scan_baseline.path` equals the given path; omitted when `None` | M3-always: emit an empty baseline |
| T3-value-free | canary fixtures: no canary in `config` or `notes` | M3-leak: note quotes a top value |
| T3-parity | every non-fallback detector in the CLI table at 8fba6af ports to the same strategy, provider and `provider_config` | M3-drift: change one provider |
| T3-deterministic | two calls give equal configs and notes | M3-order: iterate a set of table names |
| T3-config-field | `scan_baseline` with another schema tag is rejected; a config without it is unchanged | M3-open: make `schema` a plain string |

**Frozen-contract effects.** Additive public API and one additive optional
config field (§3.4 config contract). No new artifact tag; no reason identity.

### S3-cli: `--emit-config` and `--compare` (CLI, ~650 lines including moved code)

Depends on S1d and on S3-engine released and repinned.

**Files.**
- new `src/decoy/cli/_scaffold_yaml.py`: the one renderer (takes
  `_yaml_quote`, `_wrap_review_comment` and the column emitter from `init.py`,
  generalized to many tables), the commented relationships template and the
  keyed-secret guidance hook.
- `src/decoy/cli/scan.py`: `--emit-config PATH`, `--yes`, `--compare`.
- `src/decoy/cli/init.py`: `decoy init FILE` (kept, owner answer Q2) now needs
  the same header flags, builds a one-table scan through `scan_sources` and
  renders with `_scaffold_yaml`. The old builder, `_load_dataframe` and
  `_init_inference.py` are deleted.
- `src/decoy/cli/validate.py`: `preflight` checks that `scan_baseline.path`
  exists and reads as `scan-set/v1`.
- docs, `CHANGELOG.md`; tests: `tests/e2e/test_scan_emit_config.py`,
  `tests/e2e/test_scan_compare.py`, updates to `test_init_command.py` and
  `test_init_scaffold_roundtrip.py` for the table name and header flag.

**Behavior.**
- `decoy scan a.csv b.parquet --has-header --emit-config pipeline.yaml` writes
  the YAML atomically after every table has scanned and the snapshot is saved.
  The header lists the scanned tables and says to read every REVIEW comment.
  `scan_baseline.path` points at the saved snapshot (relative to the YAML);
  with `--no-save` the field is omitted and a comment says so.
- The relationships block is a comment when the user declared none:

  ```yaml
  # relationships:
  #   Decoy does not guess foreign keys. Add one entry per key, for example:
  #   - parent: {table: customers, columns: [customer_id]}
  #     children:
  #       - {table: orders, columns: [customer_id]}
  #     orphan_policy: warn
  #   Give the parent and child columns the same namespace, or the compiler
  #   rejects the pair (namespace_ambiguity). Then run
  #   `decoy scan --config <this file>` to see orphan counts for each key.
  #   Scanned tables: customers, orders, payments
  ```

  The example names are fixed copy. Only the table list comes from the scan.
- `decoy scan --config p.yaml --compare` rescans the config's sources and diffs
  the result against `scan_baseline` (the existing scan-diff rules, per table);
  `--strict` exits 1 on drift, as `scan diff --strict` does today.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T4-validate | the emitted YAML for a three-table scan passes `decoy validate config`, `compile` and `preflight` (subprocess, exit 0) | M4-target: no target for the last table |
| T4-runs | `decoy run` on the emitted YAML for a two-table fixture exits 0 and passthrough columns are byte-equal to the source | M4-format: `format: csv` for a parquet source |
| T4-headerless | a headerless source round-trips: the YAML carries `has_header: false` and its names, and `decoy run` reads it | M4-implicit: drop `has_header` |
| T4-template-inert | `relationships` parses empty and the comment names every scanned table | M4-live: emit the template uncommented |
| T4-baseline | the YAML's `scan_baseline.path` resolves to the snapshot just written | M4-abs: write an absolute path from another directory |
| T4-compare | after adding a column to one file, `--compare --strict` exits 1 and names the table | M4-noop: compare the baseline with itself |
| T4-canary | no fixture canary in the YAML | M4-leak: REVIEW quotes a top value |
| G-ONE-EMITTER | init and scan bodies match after the header | M-EMIT |
| T4-atomic | a failing second table leaves no YAML file | M4-early: write before the last table |
| T4-overwrite | existing target without `--yes` exits 2; same path as `--config` always exits 2 | M4-clobber |

**Frozen-contract effects.** CLI flags additive. Pre-GA changes: `decoy init
FILE` names the table after the file (not `data`), needs a header flag for CSV,
and defaults unsuggested columns to `passthrough` instead of `redact` (Q9). The
emitted YAML is a `PipelineConfig`.

### Totals

| Slice | Repo | Lines (est.) | Depends on |
|---|---|---|---|
| S1a-engine | engine | ~450 | none |
| S1b-engine | engine | ~480 | S1a types |
| S1c-cli | CLI | ~650 | S1a + S1b release and repin; CONFIG-EXPLICIT for `--column-names-file` |
| S1d-cli | CLI | ~400 | S1c |
| S3-engine | engine | ~520 | S1a, CONFIG-EXPLICIT |
| S3-cli | CLI | ~650 | S1d, S3-engine release and repin |

Each slice stays under 800 hand-written lines. Test depth: T0 per edit, T1
impact-selected per commit. The engine slices touch the reason registry, the
file-access sentry baselines, the config schema (S3) and the compatibility
corpus (S1a); the CLI slices move the engine pin. These are escalation
triggers, so each merge head gets the full suite diffed against the saved main
baseline.

## 6. Owner questions

Answered on 2026-11-05 and applied: Q1 (explicit headers, owner rule), Q2 keep
`decoy init FILE`, Q3 files only, Q4 local output directory, Q5 keep the legacy
table, Q6 fail the whole scan (`--keep-going` later).

Open:

- **Q7. Scan inside the run; standard versus opt-in YAML features; whether
  `decoy run` reads `scan_baseline`.** The UX discussion owns it (section 4).
- **Q8. V2 FK discovery timing.** Section 7 is ready to plan as a V2 row.
- **Q9. Fallback strategy.** The owner rule says every column is listed "even
  if it's just passthrough", and the plan makes `passthrough` the default for
  columns the detectors do not flag. Today's `decoy init` defaults those to
  `redact`, which fails safe when a detector misses PII; `passthrough` copies a
  missed PII column into the output unchanged. Confirm `passthrough`, or keep
  `redact` with every column still listed.
- **Q10. Snapshot retention.** V1 keeps every snapshot. Is a `decoy scan prune`
  or a keep-last-N default wanted for V1?

## 7. V2: value-based foreign key discovery (parked)

Parked by the owner on 2026-11-05 ("users can set up their own FKs"). Kept as
the starting design. Today's engine inference (`walks.inference.infer_edges`,
`walks.cross_file.infer_cross_file_edges`) is name-only and never checks
values. S1b's anti-join already computes containment for a declared pair, so
V2 adds candidate generation and scoring on top of it.

### Survey

Searches run on 2026-11-05 (web search; the Rostin and Zhang papers were read
in full):

- **Rostin, Albrecht, Bauckmann, Naumann, Leser, "A machine learning approach
  to foreign key discovery", WebDB 2009**
  (https://hpi.de/oldsite/fileadmin/user_upload/fachgebiete/naumann/publications/2009/WebDB09_crc.pdf).
  Finds all unary inclusion dependencies with SPIDER, then classifies each with
  ten features. The most selective were coverage (F2), name similarity (F6),
  value length difference (F7) and out-of-range share (F8). About 60% of real
  FKs covered every parent value and none covered under 10%. Spurious INDs
  come from small surrogate keys starting at 1 and from boolean columns.
- **Zhang, Hadjieleftheriou, Ooi, Procopiuc, Srivastava, "On multi-column
  foreign key discovery", PVLDB 3(1), 2010**
  (https://www.comp.nus.edu.sg/~vldb2010/proceedings/files/papers/R72.pdf).
  Partial inclusion `|F ∩ P| / |F| >= θ` with θ = 0.9, estimated with
  bottom-k sketches. A randomness test (quantile Earth Mover's Distance)
  rejects prefix subsets such as `Broker.BID` inside `Trade.TID` from
  auto-increment ranges. Handles multi-column keys in two passes.
- **Nazi, Ding, Narasayya, Chaudhuri, "Efficient Estimation of Inclusion
  Coefficient using HyperLogLog Sketches", PVLDB 11(10), 2018**
  (https://www.vldb.org/pvldb/volumes/11/paper/Efficient%20Estimation%20of%20Inclusion%20Coefficient%20using%20HyperLogLog%20Sketches).
  Bottom-k error grows once distinct values exceed k; proposes an HLL-based
  estimator.
- **SPIDER, BINDER (Papenbrock et al., VLDB 2015) and Metanome**
  (https://hpi.de/oldsite/fileadmin/user_upload/fachgebiete/naumann/publications/PDFs/2015_papenbrock_divide.pdf,
  https://hpi.de/en/database-group/projects/data-integration-projects/repeatability/data-profiling/metanome-ind-algorithms/).
  Exact unary and n-ary IND discovery in Java; BINDER reports up to 26 times
  SPIDER's speed. MIND is the older n-ary method.
- **Jiang, Naumann, "Holistic primary key and foreign key detection", JIIS
  2020 (HoPF)**
  (https://hpi.de/oldsite/fileadmin/user_upload/fachgebiete/naumann/publications/PDFs/2020_jiang_holistic.pdf).
  Scores PK and FK candidates together from unique column combinations and
  INDs with name and distribution features; reports about 88% PK and 91% FK
  recall.
- **SDV** `detect_from_dataframes`: value-based FK matching (`data_match`) is
  an Enterprise feature; results must be reviewed (link in section 2).
- **dbt `relationships` test, Great Expectations:** they verify a declared FK
  and do not discover. S1b already follows the dbt null rule.
- **SchemaCrawler weak associations, Informatica TDM entity discovery:**
  name-based and commercial value-based FK suggestions, both shown as
  suggestions for review.
- **Apache DataSketches theta sketches**
  (https://datasketches.apache.org/docs/Theta/ThetaSketchSetOps.html):
  maintained set intersection over hashed values within a fixed memory budget.

### Chosen approach (V2)

- Candidates: child column to parent column where the parent is unique and
  non-null in a full key pass, the types are compatible, and the child has at
  least two distinct values. Booleans are excluded (Rostin). Self-references
  are allowed.
- Containment: exact, through S1b's DuckDB anti-join, for candidate pairs
  that pass cheap metadata filters (type, min and max range, distinct counts).
  When the candidate count makes exact checks too slow, coordinated theta
  sampling over `pandas.util.hash_array` hashes with a per-run random key
  (never persisted), cross-checked in tests against the `datasketches` package
  as a dev-only oracle.
- Normalization: a match found only after integer canonicalization (`"007"`
  against `7`) is reported with a `leading_zero_mismatch` flag and lower
  confidence, because masking would break that join.
- Confidence: a transparent rule score, not a trained model (there is no
  labeled corpus). Containment gate θ = 0.9 (Zhang), then coverage (F2),
  out-of-range share (F8), value length difference (F7) and name evidence (F6,
  F9). Bands high, medium, low. When both sides are dense integer ranges
  starting near 1, high needs name evidence (the auto-increment trap). The EMD
  randomness test is a later step.
- Composite keys: not in the first V2 cut.
- Output: an `fk-candidates` member on `scan-set/v1` (additive) with scores
  and reason codes, value-free. The starter YAML emits high-band candidates as
  active relationships under REVIEW comments and the rest as commented
  entries. Nothing applies until the user saves and runs the file.
- Platform: `decoy-platform/api/storm/helpers.py` seeds suggested relationship
  decisions from the name-only `run_cross_file_walk`. V2 replaces that call
  with the value-based API, run where the data is read (the pre-scan worker).
