Status: plan

# SCAN-MULTI: one scan over many tables, one starter pipeline YAML

Owner: Cam. Date: 2026-11-05. Scope: CLI V1 (engine and CLI repos).

Rules consulted: `dev-rules/README.md`, `00-universal.md`,
`risk-and-exceptions.md`, `development-loop.md`, `feature-dev.md`,
`api-and-compatibility.md`, `cross-cutting-concerns.md`, `reuse-first.md`,
`testing.md`, `security.md`, `architecture.md`, `documentation.md`.

Risk: R2. The work adds public engine API, a new persisted artifact tag, a CLI
flag surface and a file-reading entry point under the file-access policy, and
its output must stay value-free. Nothing here is destructive or irreversible.
Approver: Cam.

Owner direction, 2026-11-05:

- "a decoy scan should be able to scan multiple files at once in order to
  build the single YAML for the pipeline."
- Later the same day: "Let's park that for V2, users can set up their own FKs,
  but we do want multi-file discovery at once. 1 scan for 1 pipeline build, on
  however many tables the user selects."

So V1 is two slices: S1 (one `decoy scan` over every selected table) and S3
(that scan writes one starter pipeline YAML). The value-based foreign key
discovery slice (S2 in the first draft) is a V2 design, kept short in section 7
with its source survey. V1 emits no inferred relationships of any kind, and the
existing name-only cross-file walk (`decoy_engine.walks.run_cross_file_walk`)
is not wired into the CLI.

## 1. FRAME

### Problem

`decoy scan SOURCE` takes one file. A production pipeline masks several tables
from one YAML. Today a user scans each file separately and then writes the
multi-table YAML by hand, or runs `decoy init FILE`, which scaffolds a
single-table YAML with one hard-coded table named `data`. There is no path from
"here are my five files" to "here is one pipeline YAML for all five".

The platform will later run the same flow (pre-scan a group of tables, then
build a pipeline). It already holds per-table scans grouped by run group. So
the multi-table scan and the starter-config builder belong in the engine, with
the CLI as a thin caller.

### Guarantee (what V1 promises)

1. `decoy scan A B C ...` and `decoy scan --config p.yaml [--table T ...]`
   profile every selected table in one invocation and report each one under
   the existing scan honesty and privacy rules (H4: PII candidates are
   suggestions only; H5: privacy-safe field records).
2. Memory is bounded per table, not per invocation: tables are scanned one at
   a time, each through the engine's bounded `ProfileSource` sample, and a
   table's frame is released before the next one is read.
3. `--emit-config p.yaml` writes one starter pipeline YAML: a source and a
   target for every scanned table, a `tables:` entry per table with one
   suggested column strategy per column from PII detection, `# REVIEW:`
   comments, and a commented-out `relationships:` template that names the
   scanned tables. The file passes `decoy validate config` and
   `decoy validate compile`.
4. No human output, `--json` output or emitted YAML contains a data value. The
   `--output` scan artifact keeps the engine's richer `to_dict()` under
   today's existing rule, and nothing else changes about it.
5. A single-file `decoy scan FILE` keeps today's flags, exit codes, `--json`
   keys and `--output` artifact shape. New behavior is additive.

### Non-goals

- No relationship inference, name-based or value-based. The user writes
  relationships; the YAML gives a commented template only.
- No cross-table re-identification risk. Each table keeps its own reid score
  and quasi-identifier groups.
- No database tables in V1 scan selection (DuckDB file or Postgres). Files on
  local disk, S3 and GCS only: the `SourceDescriptor` union the engine already
  reads. See owner question Q3.
- No decision on how scan relates to the in-run pre-scan, post-scan and
  data-integrity checks, or on which YAML features are standard versus opt-in.
  A separate UX discussion settles that. S1 and S3 are built so either answer
  works (section 4).
- No change to the engine's legacy `DEFAULT_STRATEGY_BY_DETECTOR` table, which
  the paused platform still reads (Q5).
- No new masking behavior. The emitted YAML uses the strategies and parameters
  `decoy init FILE` already emits and verifies.

### Failure modes

| Case | V1 behavior |
|---|---|
| Two files share a stem (`a/customers.csv`, `b/customers.csv`) | Usage error before any read, naming both paths. Hint: use `--config` and name the sources. Table names are never silently suffixed. |
| A file stem is not a usable table name (`my file.csv`, `2024.csv`) | Name is normalized to `[a-z0-9_]`, a leading digit gets a `t_` prefix, and the card shows `name <- path`. A collision after normalization is the usage error above. |
| File has no header row | Default stays "first row is the header", the same default `FileSource.has_header` has for `decoy run`, so scan and run agree. `--no-header` (positional files) or `has_header: false` (config) reads it headerless with `column_1..n`. The header guard (next row) covers the leak case. |
| Header row that is really data (a headerless file read with the default) | The header guard runs the existing STORM detectors over the header cells. If any header cell matches a PII detector, or every header cell parses as a number or a date, the table is marked `header_suspect`: column names become `column_1..n` in every output, the YAML gets `has_header: false` plus those names, and a warning tells the user to confirm. Fail-safe direction: a real header that trips the guard costs a rename, never a leak. |
| Huge file | Bounded sample per table (`--rows`, default 10 000). `--rows 0` reads the whole table, as today, and the card says so. `--strategy random` keeps today's semantics (whole read, then a seeded sample) and is documented as not bounded. |
| CSV typing (`"007"` versus `7`) | Scan reads CSV through the engine `CsvFileSource` (string-preserving, the reader family `decoy run` uses), so the profile sees the values the masker will see. Today's CLI scan uses pandas type inference and can disagree with run; S1 removes that divergence. |
| Mixed types within a column | Reported as the profiler reports it today (`mixed`). The suggested strategy falls to the loud fallback REVIEW comment. |
| Nulls | Unchanged profiler behavior (null rate per field). |
| One table fails to read or profile | The command fails with exit 3, naming the table and the exception class only (never `str(exc)`, which can carry cell data). No `--output` or `--emit-config` file is written: outputs are written only after every table succeeds, through a temp file and rename. See Q6. |
| `--emit-config` target already exists | Refused without `--yes`, as `decoy init` does. Always refused when it is the same path as `--config`. |
| `--config` names a table that is not in `sources` | Usage error listing the known source names. |
| Positional files and `--config` together | Usage error. |
| Self-references, cycles | Out of scope in V1 (no relationships are emitted). The template comment says the compiler rejects a cycle. |
| Cloud source without the cloud extra | Existing engine error, mapped to the existing extras hint. |

## 2. Established solutions

V1 is orchestration and scaffolding over existing engine parts. Searches run on
2026-11-05 (web search plus the engine and CLI code at the commits named
below). What the design follows:

- **Multi-table metadata from a set of tables:** SDV
  `Metadata.detect_from_dataframes` builds one metadata object for a mapping of
  table name to frame and warns that the result must be reviewed
  (https://docs.sdv.dev/sdv/multi-table-data/data-preparation/creating-metadata).
  S1 follows its input shape (table name to source) and its review-required
  posture.
- **Scaffold, then review:** `dbt init` and cookiecutter, already cited by the
  CLI scaffolder (`decoy/cli/_init_inference.py`). S3 keeps that pattern and
  extends it from one table to many.
- **Bounded reads:** the engine's own `profile._readers.build_profile_source`
  (metadata-first, bounded `sample_frame`, the Spark and Dask
  deferred-materialization pattern cited in that module). S1 reuses it rather
  than adding a reader, which also brings S3 and GCS sources for free.
- **Header detection:** Python's `csv.Sniffer.has_header` was considered and
  rejected as the default. It votes only on columns with a fixed type or
  length, so an all-text CSV (names, emails) gets "no header" and every real
  header would be renamed. The guard reuses the STORM detectors instead,
  aimed at the case that matters: a data row printed as column names.
- **Atomic output:** write to a temp file in the target directory, then
  `os.replace` (POSIX rename semantics), the publish shape the engine's
  `fileaccess._output` already uses.

No custom algorithm remains in V1. The V2 FK survey is in section 7.

## 3. Design notes

- **New structure:** one engine function (`scan_sources`) with two frozen
  result types, one engine starter-config builder, and one CLI YAML renderer
  shared by `decoy init FILE` and `decoy scan --emit-config`.
- **Library code does not know its callers** (engine `CLAUDE.md`): the engine
  returns data (`ScanSet`, `StarterConfig`); the CLI owns cards, flags, comment
  wrapping and file writes. The platform can call the same two functions.
  Change pain addressed: today the detector-to-strategy table lives in the CLI,
  so the platform would have to copy it.
- **One place per fact:** the V2-shaped detector-to-strategy table moves to the
  engine once (S3-engine) and the CLI copy is deleted (S3-cli). A parity test
  pins the port.
- **Choke points.**
  - File reads: `scan_sources` is a file-reading public entry point, so it is
    decorated `@fileaccess.entry_point`, takes `file_access: FileAccess | None
    = None`, and is added to `fileaccess.ENTRY_POINTS`. The existing
    file-access sentries (`tests/sentry/baselines/file-access-entry-seams.json`
    and the registry baseline) fail if it reads outside the policy. Mutation
    M-FA: drop the decorator; the entry-seam sentry must go red.
  - YAML emission: one renderer, `decoy/cli/_scaffold_yaml.py`, is the only
    code that writes a pipeline YAML from a scan. `decoy init FILE` calls it
    with a one-table `StarterConfig`. Guard G-ONE-EMITTER runs both verbs on
    the same file and asserts identical bodies after the provenance header.
    Mutation M-EMIT: route `decoy init FILE` back through its old
    `_build_scaffold_yaml`; G-ONE-EMITTER goes red. The old builder is deleted
    in S3-cli (pre-GA hard delete), so the mutation is applied on a scratch
    copy.
  - Value-free field output: the existing `_safe_field_record` stays the only
    field renderer for human and `--json` output. S1 adds no second renderer.
- **No registry for one site:** table-name normalization is one CLI function.

## 4. Compatibility with the open UX question

The UX discussion may make scan a step inside `decoy run` (in-run pre-scan),
keep it a separate verb, or both, and may move features between standard and
opt-in. V1 keeps both doors open:

- `scan_sources` returns the same `ColumnProfile` that the in-run pre-scan
  builds with `profile_columns`, so an in-run pre-scan can call it unchanged.
- The emitted YAML sets only what a pipeline needs to run: `version`,
  `global_settings.seed`, `sources`, `tables`, `targets`, plus the commented
  relationships template. It writes no `post_scan:`, `validators:`,
  `quarantine:` or `subset:` block, so whatever defaults the UX discussion
  picks apply to it.
- `StarterConfig` is a plain config dict plus notes, so adding opt-in blocks
  later is an additive builder field, not a renderer rewrite.

## 5. Slices

Order: S1-engine, S1-cli, S3-engine, S3-cli. An engine release and CLI repin
sit between each engine part and its CLI part (the CLI pins
`decoy-engine==0.18.0`; follow the engine repin checklist). S1-engine and
S3-engine touch different files and can be built in parallel; S3-engine's tests
need S1-engine's types merged first. Both engine parts can ship in one engine
release.

Sizes are hand-written lines, code plus tests, estimated.

### S1-engine: `scan_sources` (engine repo, ~430 lines)

**Files.**
- new `src/decoy_engine/profiling/scan_set.py`: `scan_sources`, `TableScan`,
  `ScanSet`, `scan_set_to_dict`, `scan_set_from_dict` (under 300 lines).
- `src/decoy_engine/profiling/__init__.py`, `src/decoy_engine/__init__.py`:
  additive exports.
- `src/decoy_engine/fileaccess/__init__.py`: add to `ENTRY_POINTS`; sentry
  baselines refreshed through their own tooling.
- `src/decoy_engine/reasons/warning/_part1.py`: one new identity,
  `warning.scan_header_suspect`; reason manifest fixture and registry minor
  version (1.5.0 to 1.6.0).
- `docs/compatibility-contract.md` (§3.1 tag list, §3.4 public API note),
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
    file_access: FileAccess | None = None,
) -> ScanSet
```

- Validates each descriptor through `SourceDescriptor`. It is a public entry
  point that takes raw dicts, so it validates once itself.
- For each selected table, in order: `build_profile_source(descriptor,
  field_key=f"sources.{name}.path")`, take the bounded sample, run the header
  guard, call `profile_columns(frame, source_label=name, ...)`, drop the frame.
- Header guard: run the detector registry over the header cells as a one-row
  frame. Mark `TableScan.header_suspect` and attach a
  `warning.scan_header_suspect` `ReasonRef` when a cell matches a PII detector
  or every cell parses as a number or a date. When suspect, the field names
  are rewritten to `column_1..n` before the profile leaves the function, so no
  caller (including the `--output` artifact) can print them.
- `ScanSet` is frozen: `tables: tuple[TableScan, ...]` and
  `scan_schema = "scan-set/v1"`. `TableScan` carries `name`, `descriptor` (as
  given, for the YAML), `profile: ColumnProfile`, `header_suspect`,
  `warnings: tuple[ReasonRef, ...]` and `row_count_exact: bool` (from
  `RowCount.exact`).
- Errors: a read or profile failure raises the reader's existing coded error,
  wrapped with the table name only. No new failure reason.
- Logging: one operational log line per table (table name, rows scanned,
  duration), never a value or a raw exception message.

**Acceptance tests (written first) and named mutations.**

| Test | Asserts | Mutation that must turn it red |
|---|---|---|
| T1-order | three sources, `tables=[c, a]`: the result holds exactly `c, a` in that order | M1-order: iterate `sources` instead of `tables` |
| T1-bounded | a 50 000-row parquet with `sample_rows=1000` reads no more than one batch past 1000 rows (counting reader spy) | M1-full: call `to_frame()` instead of `sample_frame` |
| T1-sequential | at most one table frame is alive while three tables scan (weakref probe) | M1-hold: keep frames in a list until the end |
| T1-csv-reader | a CSV column holding `"007"` profiles as a string of length 3, matching what `decoy run` reads | M1-infer: read CSV with `pd.read_csv` defaults |
| T1-header-guard | a headerless CSV whose first row holds an email and an SSN gives `header_suspect`, names `column_1..n`, and neither canary appears in `scan_set_to_dict(result)` outside per-field `top_values` | M1-guard: skip the rename when suspect |
| T1-header-clean | a normal header (`first_name,email`) is not suspect | M1-overeager: treat any all-string header as suspect |
| T1-roundtrip | `scan_set_from_dict(scan_set_to_dict(s)) == s`; an unknown `scan_schema` major raises | M1-schema: drop the schema check |
| T1-fileaccess | with a deny-all `FileAccess`, the call raises `decoy.file_access_denied` before any read | M-FA (section 3) |
| T1-error-class | a corrupt parquet raises with the table name and no cell text | M1-leak: interpolate `str(exc)` |

**Frozen-contract effects.** Additive public API (§4.1). New persisted tag
`scan-set/v1` (§3.1), additive; the CLI is its only reader in V1; a shape
change mints `v2` with a kept `v1` reader. The reason registry gains one
warning identity with `since` 1.6.0; the release pin and manifest fixture move
in the same PR. No determinism-contract effect (no masking change).

**Value-free rule.** `TableScan` fields other than `profile` hold no data
values. `profile` is the engine's existing `ColumnProfile`, which carries
`top_values` and min and max by existing design; the header guard makes sure
field names are not data.

### S1-cli: `decoy scan` over many tables (CLI repo, ~600 lines)

Depends on S1-engine released and repinned.

**Files.**
- new `src/decoy/cli/_scan_multi.py`: argument resolution (positional files to
  descriptors, `--config` loading and `--table` selection, name normalization
  and the collision check), the multi-table card and the `--json` envelope.
  Kept out of `scan.py`, which is already 1442 lines.
- `src/decoy/cli/scan.py`: `SOURCE` becomes `SOURCES...` (one or more); new
  options `--config`, `--table` (repeatable) and `--no-header`; the body calls
  `scan_sources` for every invocation, one file included (one path). `fields`,
  `show` and `diff` accept a `scan-set/v1` file and take `--table`, required
  when the set holds more than one table.
- docs: the scan epilog, the CLI reference page, `CHANGELOG.md`.
- tests: `tests/e2e/test_scan_multi.py`, additions to
  `tests/e2e/test_scan_command.py`.

**Behavior.**
- `decoy scan a.csv b.parquet c.fwf`: `--format` and `--layout` apply only
  with exactly one file; several fixed-width files need `--config`. Usage error
  otherwise.
- `decoy scan --config p.yaml [--table orders --table customers]`: the config
  is validated through `PipelineConfig`; only `sources` is read; selection
  defaults to every source.
- One file: card, `--json` keys and `--output` artifact (bare
  `profile.to_dict()`) are unchanged.
- Several tables: one summary card (table, rows scanned, columns, PII
  candidate count, reid risk), then the H4 honesty block once.
  `--show-fields` prints one privacy-safe field table per table. `--json`
  emits `{"command": "scan", "status": "ok", "scan_schema": "scan-set/v1",
  "pii_note": ..., "tables": [ {per-table keys as today}, ... ]}`.
  `--output scan.json` writes the `scan-set/v1` dict. `--output -` keeps its
  existing conflict rules with `--json` and `--quiet`.
- A header-suspect table gets a warning line naming the table and the fix
  (`--no-header`, or `has_header: false` in the config).
- Exit codes unchanged: 0 ok, 2 usage, 3 runtime.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T2-multi | three files give three tables in argument order in the card and in `--json` | M2-sort: sort tables by name |
| T2-single-compat | one file: `--json` key set and `--output` artifact match a golden captured at CLI 8fba6af (timestamps masked) | M2-envelope: always emit the multi envelope |
| T2-collision | `a/x.csv b/x.csv` exits 2 before any read (reader spy count 0) | M2-suffix: auto-suffix to `x_2` |
| T2-config-select | `--config p.yaml --table t2` scans only t2 | M2-ignore-select: scan every source |
| T2-config-unknown | an unknown `--table` exits 2 and lists the known names | M2-silent: skip unknown names |
| T2-mix | positional files plus `--config` exit 2 | M2-mix: let the config win |
| T2-atomic | second table corrupt: exit 3 and no `--output` file afterwards | M2-partial: write per table as it goes |
| T2-canary | every cell of every fixture holds a unique canary; across the card, `--show-fields`, `--json`, `--quiet` stderr and the error path, no canary appears | M2-leak: add `top_values` to the field record |
| T2-header | headerless fixture without `--no-header`: warning printed, names are `column_N`, no canary | M2-trust-header: print profile names when suspect |
| T2-fields-set | `scan fields set.json --table orders` works; without `--table` on a two-table set it exits 2 | M2-first: default to the first table |

**Frozen-contract effects.** CLI contract (§3.4): verb unchanged; flags
additive; the positional becomes variadic, which accepts every old
invocation. The single-file `--json` shape is unchanged; the multi shape is
new. The saved-scan readers accept both the bare profile and `scan-set/v1`.

### S3-engine: `starter_config` (engine repo, ~450 lines)

**Files.**
- new `src/decoy_engine/profiling/starter_config.py`: `starter_config`,
  `StarterConfig`, `ColumnSuggestion`.
- new `src/decoy_engine/profiling/_starter_strategies.py`: the detector to
  V2 `ColumnConfig` table, ported from the CLI's `_INFERENCE_TABLE` with its
  REVIEW copy and its `_NAMESPACE_REQUIRED_STRATEGIES` rule.
- exports, `docs/compatibility-contract.md` §3.4 note, `CHANGELOG.md`.
- tests: `tests/unit/profiling/test_starter_config.py`.

**Behavior.**

```python
def starter_config(
    scan: ScanSet,
    *,
    targets: Mapping[str, dict] | None = None,  # default: derived per source
    seed: int = 42,
) -> StarterConfig
```

- `StarterConfig.config` is a plain dict. The builder runs
  `PipelineConfig.model_validate` and `compile_plan` on it before returning; a
  failure is a builder bug and raises.
- One `sources` entry per table: the scanned descriptor, plus
  `has_header: false` and `column_names` for header-suspect CSV tables.
- One `targets` entry per table. Local file sources get `<stem>.masked.<ext>`
  beside the input (today's init rule). S3 and GCS sources get the same bucket
  with a `masked/` key prefix (Q4). A format the target type cannot write
  (`fixed_width` to S3 or GCS) falls back to parquet with a REVIEW note.
- One `tables` entry per table and one column entry per profiled field, from
  the strategy table. Namespaced strategies get `namespace: <column name>`, so
  two tables that hash a same-named column already share a namespace.
- `relationships` is always `[]`. `StarterConfig.relationship_template` lists
  the scanned table names for the renderer's comment.
- `StarterConfig.notes` maps `(table, column)` to the REVIEW text. Notes are
  built only from detector ids, strategy names and fixed copy.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T3-valid | a four-table scan set (csv, parquet, fixed width, header-suspect csv) gives a config that validates and compiles | M3-namespace: omit `namespace` for hash |
| T3-every-column | every profiled field of every table has exactly one column entry | M3-skip: drop fallback columns |
| T3-no-relationships | `config["relationships"] == []` for every input, including tables with matching `*_id` names | M3-infer: call `run_cross_file_walk` and add its edges |
| T3-header-suspect | the suspect CSV source has `has_header: false` and `column_1..n` | M3-header: copy the descriptor unchanged |
| T3-value-free | with canary fixtures, no canary appears in `config` or `notes` | M3-leak: put `top_values[0]` in a note |
| T3-parity | for every detector id in the CLI table at 8fba6af, the ported entry yields the same strategy, provider and `provider_config` | M3-drift: change one entry's provider |
| T3-deterministic | two calls on the same scan set give equal configs and notes | M3-order: iterate a set of table names |

**Frozen-contract effects.** Additive public API. No new artifact tag (the
output is the existing `PipelineConfig` contract). No reason identity.

### S3-cli: `--emit-config` (CLI repo, ~550 lines including moved code)

Depends on S1-cli, and on S3-engine released and repinned.

**Files.**
- new `src/decoy/cli/_scaffold_yaml.py`: the one renderer. It takes
  `_yaml_quote`, `_wrap_review_comment` and the column emitter from `init.py`,
  generalized to many tables, plus the commented relationships template and
  the keyed-secret guidance hook.
- `src/decoy/cli/scan.py`: `--emit-config PATH` and `--yes`.
- `src/decoy/cli/init.py`: `decoy init FILE` builds a one-table `ScanSet`
  through `scan_sources` and renders with `_scaffold_yaml`. The old builder,
  `_load_dataframe` and the local inference call are deleted;
  `_init_inference.py` is deleted once the engine table is the source.
- docs: scan and init epilogs, CLI reference, `CHANGELOG.md`.
- tests: `tests/e2e/test_scan_emit_config.py`; `test_init_command.py` and
  `test_init_scaffold_roundtrip.py` updated only where the table name changes
  from `data` to the file's table name (Q2).

**Behavior.**
- `decoy scan a.csv b.parquet --emit-config pipeline.yaml` writes the YAML
  after every table has scanned, atomically. The header says it was generated
  by `decoy scan`, lists the scanned tables and says to read every REVIEW
  comment before running.
- The relationships block is a comment, for example:

  ```yaml
  # relationships:
  #   Decoy does not guess foreign keys. Add one entry per key, for example:
  #   - parent: {table: customers, columns: [customer_id]}
  #     children:
  #       - {table: orders, columns: [customer_id]}
  #     orphan_policy: warn
  #   Give the parent and child columns the same namespace, or the compiler
  #   rejects the pair (namespace_ambiguity).
  #   Scanned tables: customers, orders, payments
  ```

  The example names are fixed copy, not taken from the data. Only the table
  list comes from the scan.
- `--emit-config` with `--json` adds `"config_written": PATH` to the envelope.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T4-validate | the YAML emitted for a three-table scan passes `decoy validate config` and `decoy validate compile` (subprocess, exit 0) | M4-target: emit no target for the last table |
| T4-runs | `decoy run` on the emitted YAML for a small two-table fixture exits 0 | M4-format: write `format: csv` for a parquet source |
| T4-template-inert | the YAML parses with `relationships` absent or empty, and the comment block names every scanned table | M4-live: emit the template uncommented |
| T4-canary | no fixture canary appears in the YAML | M4-leak: REVIEW text quotes a top value |
| G-ONE-EMITTER | `decoy init x.csv` and `decoy scan x.csv --emit-config` bodies match after the header | M-EMIT (section 3) |
| T4-atomic | a failing second table leaves no YAML file | M4-early: write before the last table scans |
| T4-overwrite | an existing target without `--yes` exits 2; the same path as `--config` always exits 2 | M4-clobber: skip the check |

**Frozen-contract effects.** CLI flags additive. `decoy init FILE` output
changes its table name from `data` to the file's table name (pre-GA change
with a CHANGELOG entry; Q2). The emitted YAML is a `PipelineConfig`, already
covered by §3.4.

### Totals

| Slice | Repo | Lines (est.) | Depends on |
|---|---|---|---|
| S1-engine | engine | ~430 | none |
| S1-cli | CLI | ~600 | S1-engine release and repin |
| S3-engine | engine | ~450 | S1-engine types |
| S3-cli | CLI | ~550 | S1-cli, S3-engine release and repin |

Each slice stays under 800 hand-written lines. Test depth follows the standing
rule: T0 per edit and T1 impact-selected per commit. The engine slices touch
the reason registry and the file-access sentry baselines, and the CLI slices
move the engine pin; these are escalation triggers, so each merge head gets the
full suite diffed against the saved main baseline.

## 6. Owner questions

- **Q1. Header default.** Plan: assume a header (matches `decoy run`), with the
  detector-based guard renaming suspect headers to `column_N`. Alternative:
  require an explicit header flag for every CSV. Confirm the default.
- **Q2. `decoy init FILE`.** Plan: keep it as the one-file form of the same
  path, with the table named after the file instead of `data`. Alternative:
  retire `decoy init FILE` in favor of `decoy scan FILE --emit-config` (pre-GA
  hard delete, one verb).
- **Q3. Database tables in scan.** `decoy run --duckdb-source` exists. Should
  V1 scan also select tables from a DuckDB file or Postgres? Plan: files only
  (local, S3, GCS).
- **Q4. Cloud targets.** Plan: same bucket, `masked/` key prefix. Or a local
  `./masked/` directory with a REVIEW comment.
- **Q5. Two strategy tables.** The engine's `DEFAULT_STRATEGY_BY_DETECTOR`
  (legacy parameter names, read by the paused platform) and the new starter
  table will both exist. Plan: leave the legacy table until the platform
  resumes, then point the platform at the starter table and delete the legacy
  one. Confirm.
- **Q6. Partial failure.** Plan: one failed table fails the whole scan and
  writes nothing. Alternative: a `--keep-going` flag that reports the failed
  tables and emits YAML for the rest.
- **Q7. Scan inside the run; standard versus opt-in YAML features.** Not
  decided here. The UX discussion owns it (section 4).
- **Q8. V2 FK discovery timing.** Section 7 is ready to plan as a V2 roadmap
  row when wanted.

## 7. V2: value-based foreign key discovery (parked)

Parked by the owner on 2026-11-05 ("users can set up their own FKs"). Kept as
the starting design. Today's engine inference (`walks.inference.infer_edges`,
`walks.cross_file.infer_cross_file_edges`) is name-only and never checks
values.

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
  (every non-null child value exists in the parent) and do not discover. Their
  null rule (nulls pass) is the one to copy.
- **SchemaCrawler weak associations, Informatica TDM entity discovery:**
  name-based (SchemaCrawler) and commercial value-based (Informatica) FK
  suggestions, both shown as suggestions for review.
- **Apache DataSketches theta sketches**
  (https://datasketches.apache.org/docs/Theta/ThetaSketchSetOps.html):
  maintained set intersection over hashed values within a fixed memory budget.

### Chosen approach (V2)

- Candidates: child column to parent column where the parent is unique and
  non-null in a full key pass, the types are compatible after normalization,
  and the child has at least two distinct values. Booleans are excluded
  (Rostin). Self-references are allowed.
- Value check: hash normalized values with `pandas.util.hash_array` under a
  per-run random key (vectorized SipHash, never persisted). Parent keys are an
  exact sorted `uint64` array up to a cap (about 4 million keys, 32 MB). Above
  the cap, coordinated theta sampling on both sides (keep hashes below a shared
  threshold, the DataSketches theta rule), cross-checked in tests against the
  `datasketches` package as a dev-only oracle. Containment is
  `|child ∩ parent| / |child|` over distinct non-null values; nulls pass, as in
  SQL and dbt.
- Normalization: integers and integral floats to canonical decimal text,
  strings trimmed with case kept, dates to ISO text. A match found only after
  integer canonicalization (`"007"` against `7`) is reported with a
  `leading_zero_mismatch` flag and lower confidence, because masking would
  break that join.
- Confidence: a transparent rule score, not a trained model (there is no
  labeled corpus). Containment gate θ = 0.9 (Zhang), then coverage (F2),
  out-of-range share (F8), value length difference (F7) and name evidence
  (exact name, `<table>_id` stem, typical suffix; F6 and F9). Bands: high,
  medium, low. When both sides are dense integer ranges starting near 1, high
  needs name evidence (the auto-increment trap). The EMD randomness test is a
  later step.
- Composite keys: not in the first V2 cut; unary only.
- Output: an `fk-candidates/v1` list on the `ScanSet` with scores and
  reason codes, value-free (hashes stay in memory). The starter YAML emits
  high-band candidates as active relationships under REVIEW comments and
  medium and low as commented entries. Nothing applies until the user saves
  and runs the file.
- Platform: `decoy-platform/api/storm/helpers.py` seeds suggested
  relationship decisions from the name-only `run_cross_file_walk`. V2 replaces
  that call with the value-based API, run where the data is read (the
  pre-scan worker), since stored profiles carry no key values.
