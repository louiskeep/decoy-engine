Status: plan

# SCAN-MULTI: one scan over many tables, one starter pipeline YAML

Owner: Cam. Date: 2026-11-05. Scope: CLI V1 (engine and CLI repos).

Rules consulted: `dev-rules/README.md`, `00-universal.md`,
`risk-and-exceptions.md`, `development-loop.md`, `feature-dev.md`,
`api-and-compatibility.md`, `cross-cutting-concerns.md`, `reuse-first.md`,
`testing.md`, `security.md`, `architecture.md`, `documentation.md`.

Risk: R2. The work adds public engine API, a new persisted artifact tag, a new
optional config field, a CLI flag surface, a saved scan record (stored by LIB
L2) and a
file-reading entry point under the file-access policy, and its output must stay
value-free. Nothing here is destructive or irreversible. Approver: Cam.

## Revision note (gap fix, 2026-11-06)

- All local CSV dialect resolution and row reads, including the header guard,
  leading-zero pass and integrity key chunks, go through CLI-DIALECT's
  `resolve_csv_dialect` and `read_csv_source`. S1b adds column projection to
  that reader instead of creating another pandas path.
- Cloud CSV dialect support is dropped from V1 and recorded under V2-LOG.
  This matches CONFIG-EXPLICIT's explicit cloud limit and the existing
  CLI-CLOUD gate. S1a.2 now owns sample parity only.
- `decoy scan --config` uses CONFIG-EXPLICIT's one declaration boundary with
  a typed sources-only request. AT-C8 recognizes that mode but still rejects
  a config-reading command that reaches rows without the returned capability.
- S3a.1 tests the renderer directly. S3a.2 adds `--emit-config` and the final
  G-ONE-EMITTER command guard. Dependencies now name CONFIG-EXPLICIT C1 and
  C2 for S3a.1 and E2 for S3-engine.1.
- S3-engine.1 does not add a second detector-to-strategy table. It extends
  the existing engine registry in `profiling/recommendations.py` with one
  descriptor per detector, derives the existing public defaults unchanged,
  and puts only the starter decision algorithm in `_starter_inference.py`.

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

## Revision note (Codex remediation, 2026-11-06)

| Round-3 finding | Change |
|---|---|
| V1, saved-scan stdin regressed | `load_scan_set` remains the single reader but accepts a path or `-`. `fields`, `show`, and either side of `diff` retain stdin for legacy and `scan-set/v1` input, with at most one `-` in `diff`. |
| V1, parent nulls counted as duplicates | S1b computes duplicate excess only over parent rows whose complete key is non-null. T5-dupes covers unary and composite nulls mixed with real duplicates. |
| V1 + DESIGN, L0 missed CLI DuckDB staging | LIB L0.3 owns `run.py:654-670`, its Windows and fail-closed tests, and the first `run.py` writer slot. |
| DESIGN, protected-directory guard was incomplete | LIB G-ONE-PROTECTED-DIR now covers every raw directory creation form and uses a reasoned ratchet for protected children and published-output staging. |
| V2-LOG | No plan action: add human and JSON value canaries for legacy `show` in later test polish. |

V1 is S1 (one `decoy scan` over every selected table, with integrity facts and
a saved record in the local record library) and S3 (that scan writes one starter pipeline YAML). The
value-based foreign key discovery slice (S2 in the first draft) is a V2 design
in section 7. V1 infers no relationships of any kind, and the name-only
cross-file walk (`decoy_engine.walks.run_cross_file_walk`) is not wired into the
CLI.

## Revision 7 (2026-11-06): Codex round 2 on LIB 3585cc0f and SCAN-MULTI 6103684e

Codex returned GO-WITH-FIXES on both. Claims checked against CLI and engine
`origin/main` (8fba6af, 41a86f47); all held. LIB carries the matching
Revision 7 (export link closure, L0 reopen checks, L0 made universal).

| Finding | Change |
|---|---|
| V1 legacy bare files | Confirmed: an 8fba6af `--output` file is `profile.to_dict()` with `row_count`, `source_label` and `fields` and no `scan_schema` (CLI `tests/e2e/test_scan_command.py:85`). `load_scan_set` gains a legacy branch, strictly shape-checked against a real 8fba6af fixture, that presents the file as a one-table set. Tests T2-legacy-fields, T2-legacy-show, T2-legacy-diff, T2-legacy-reject. |
| V1 + DESIGN L0 not universal | Fixed in LIB L0.1 to L0.3: the engine's out-of-core, isolated-run and Postgres spill directories, the quarantine validator and CLI DuckDB source staging move onto the allocator; protected children and published-output staging use the guarded reasoned ratchet. S1b's spill uses the allocator unchanged. |
| V1 L0 reopen | Fixed in LIB L0: `open_protected_dir` checks and tightens or refuses an existing directory. The failure-mode table gains `scratch_unsafe`. |
| V2-LOG failure row | The "spill directory cannot be created" row now names `scratch_acl_failed` and `scratch_unsafe`, not the DuckDB error. |
| V2-LOG Totals | The duplicate header rows in Totals are removed. |

## Revision 6 (2026-11-05): joint Codex review of LIB e636baaf and SCAN-MULTI bcb25eb5

Codex returned GO-WITH-FIXES on both plans. Each claim was checked against
engine `origin/main` 41a86f47 and CLI `origin/main` 8fba6af; all held. LIB
carries the matching Revision 6.

| Finding | Change |
|---|---|
| ENGINE 1 (Windows fallback spill) | Confirmed: `execution/out_of_core/_scratch.py:46-65` uses `mkdtemp` and `chmod`, which on Windows sets no owner-only DACL. LIB's new engine slice L0 adds one protected allocator (`protected_scratch_dir`, `create_protected_dir`) with the DACL set at `CreateDirectoryW`, failing closed with `scratch_acl_failed`, and moves the out-of-core spill onto it. S1b's `spill_dir=None` fallback uses it too, and library scratch already does (LIB L1d). Tests T5-spill-default (now Windows too) and T5-spill-acl-fail; the real DACL test is LIB P0-win-dacl. S1b depends on L0. |
| ENGINE 2 (cell values in `--output`) | Confirmed: `scan.py:768` writes `profile.to_dict()` to `--output`, and `top_values` is read back at `scan.py:1048`. From S1c, `--output` (single file and multi-table, and `--output -`) writes only the value-free core, `scan_set_to_dict(detail=False)`. The value-bearing detail is written only by LIB L2's `--output-detail`, only into protected library scratch with a 24-hour TTL, and is refused when the library is off. Guarantees 7 and 8, the section 2 and 3 notes, S1c and T2-parquet-compat change; new tests T2-output-core and T2-output-stream-core. Pre-GA breaking change with a CHANGELOG line. |
| V2-LOG T1-header-fn | The test now has a mutation: M1-name-detector (add the person-name detector to the guard's set; the test goes red because a warning appears). |
| V2-LOG SM2 status | The owner confirmed SM2. "Pending owner confirm" is removed from Guarantee 6, S3-engine and section 6; Q11 is answered. |
| V2-LOG estimates | Estimates now count source, tests and docs, at about 15 lines per unit case and 25 to 35 per end-to-end case. S1a, S1c, S3-engine and S3a-cli are cut into parts (`.1`, `.2`), each under about 800; see each slice and Totals. |
| V2-LOG 5.0 "identical" | 5.0 is now word-for-word the same in both plans; LIB's OS-lane text moved to its own 5.1. 5.0 also gains L0, the RESULT rows and the part names. |

## Revision 5 (2026-11-05): joint Dennis review of LIB 6b50e729 and SCAN-MULTI 9b7cd9ab

Dennis returned GO-WITH-FIXES on both plans. This revision and LIB Revision 5
(CLI branch `plan/local-record-library`) were written in one pass so the two
plans agree. Each claim was checked against engine `origin/main` 41a86f47 and
CLI `origin/main` 8fba6af before the fix was adopted.

| Finding | Change |
|---|---|
| XH1 | S1d is now a pointer to LIB L2, the only build slice for the scan record. Kind `scan-set`, payload tag `scan-set/v1`, stored under `.decoy/library/objects/`. Guarantee 5, the library failure row, the section 3 choke point, Totals and the S3 dependencies all point at L2. |
| XH2 | One CLI reader, `load_scan_set(path)`. S1c creates it with the bare-file branch; L2 adds the envelope branch. Every saved-scan reader (`fields`, `show`, `diff`, `--compare`, preflight) calls it. New tests T2-load-bare and, in S3b-cli, T4-compare-both and T4-preflight-both. |
| XH3 | Scan's library root: `--library`, `DECOY_LIBRARY`, then the `--emit-config` directory, then the `--config` directory, then the current directory (S3a-cli and LIB 4.1). New test T4-same-library. |
| XH4 | Verified: `connect_duckdb(*, temp_dir: Path, ...)` (`execution/out_of_core/_duckdb.py:32`). `scan_sources` gains `spill_dir: Path | None`; `None` means a private 0700 temp directory removed in `finally`. The CLI passes library scratch from L2 on. New tests T5-spill-dir and T5-spill-default. |
| XM1 | S3a-cli sets the ref `scan-set/baseline/<slug>` under the library lock, with the slug rule stated. A failed save omits `scan_baseline` with a comment; a failed ref set warns. New test T4-baseline-ref. |
| XM2 | T2-parquet-compat is a superset check: every 8fba6af key is present with the same value type, and the only new key allowed is `library`. |
| XM3 | S1a defines the core pydantic model `ScanSetCore`; `scan_set_to_dict(detail=False)` dumps it; the records registry points at it. New test T1-core-model. |
| XM4 | New section 5.0, Sequencing: the registry chain (SUB-STOP 1.6.0, STORM E1 1.7.0, then merge order through REG-RENUMBER) and the cross-plan build order. The platform ROADMAP is not edited here; the orchestrator adds the rows. |
| XM5 | S3-cli is split into S3a-cli (`--emit-config`, init, baseline ref) and S3b-cli (`--compare`, preflight). |
| SH1 | CLI-DIALECT is referenced by its gated-pending handoff in the vault, `10_Projects/decoy/handoffs/phase0.5/p05-cli-dialect-build-handoff.md` (branch `fix/cli-dialect-honour-csv-fields`). That handoff branches from CLI main and is not owned or sized by CONFIG-EXPLICIT; Revision 4 item 1 was wrong on that point. CONFIG-EXPLICIT is a named dependency, "CONFIG-EXPLICIT (plan in progress)", planned by another author. No slice that depends on either starts until its row exists with a gated plan. |
| SH2 | Verified: `S3CsvSource.to_frame` downloads the whole object (`profile/_cloud_readers.py:290-293`, through `_s3_full` at `:218`), and the GCS CSV reader does the same (`:393`). V1 skips integrity for cloud sources with status `skipped_cloud` and no counts (reason below). New test T5-cloud-skip. |
| SM1 | S1a registers both warning identities (`scan_header_suspect`, `scan_key_type_mismatch`) at one minor; S1b adds no identity. S1b branches from S1a, and both ship in one engine release. |
| SM2 | Key columns named in a declared relationship get `strategy: hash` with the relationship namespace, plus a REVIEW note. With no declared namespace, the starter YAML writes one placeholder per relationship and config validation refuses it (new code `namespace_placeholder_unset`). Owner confirmed (Revision 6). New tests T3-key-hash, T3-placeholder, T4-join-kept and T4-placeholder-refused. |
| SM4 | T1-sample-parity uses a fixture with no empty cells; null normalization across formats is not claimed. |
| SL1 | `leading_zero_count` is computed for `head` and `full` only; `random` stores `null` and the card says "not checked". New test T1-leading-zero-random. |
| SL2 | T5-bounded uses 40-character text keys (key data about three times the memory limit) and asserts the spill through `duckdb_temporary_files()` sampled at a test seam, plus a non-empty spill directory. |
| SL3 | Decided: no `record_id` member on `scan_baseline`. The object path encodes the id, and `load_scan_set` checks that the record's id matches its file name. |
| SL4, prior M1 | Closed. Prior M1 was "the plan hard-codes registry 1.6.0" (Dennis review of 2a391193, which itself marked it "[27c5: fixed]"). Revision 2 item 8 removed the literal at 27c52a49. Section 5.0 now states the chain. |
| Owner rule (2026-11-05) | New subsection "Central registries and sizing" before Totals: existing copies found in the CLI and engine and what each slice reuses (the engine's `engine_scratch_root` for the spill, LIB's shared atomic writer for `--output` and the YAML, one saved-scan reader), the choke point and guard for each rule, and per-slice file sizes; touched files over 800 lines must not grow. |

Why SH2 skips rather than bounds: cloud CSV has no bounded read path today, so
a cloud integrity pass needs a new streaming reader plus test doubles for two
providers. Cloud Parquet does have one (`_RangeFileReader` with
`iter_batches`, `_cloud_readers.py:51-117`), but `decoy run` refuses cloud I/O
until CLI-CLOUD lands (Revision 4 item 12), so integrity facts for a cloud
source would describe a pipeline the CLI cannot run yet. The Parquet range path
is the first step when CLI-CLOUD is planned.

## Revision 4 (2026-11-05): Dennis review of 2a391193, applied on 27c52a49

Dennis returned NO-GO (1 blocker, 4 high, 8 medium, 6 low). Each item below
names the finding it answers.

1. **B1, new dependency CLI-DIALECT.** `decoy run` reads CSV with
   `pd.read_csv(path, dtype=str)` and ignores `has_header`, `column_names` and
   `delimiter` (CLI `_sources.py` `read_source` and `iter_source_chunks`), so
   a headerless YAML validates and compiles and then fails at run. A
   standalone CLI slice, CLI-DIALECT (owned by the CONFIG-EXPLICIT row), makes
   both readers honour all three fields. It lands before S1c and S3-cli. S3-cli
   and every CLI headerless test depend on it; new test T4-runs-headerless.
   The earlier draft also proposed passing those fields through cloud CSV
   readers. The gap-fix revision supersedes that proposal: cloud descriptors
   do not expose the fields, CONFIG-EXPLICIT excludes them, and CLI-CLOUD is
   outside V1.
2. **H1, CSV typing.** Profiling keeps the engine `CsvFileSource` type
   inference; `decoy run` reads CSV as text. The plan no longer claims scan
   sees the run's types. S1a counts sampled text values with a leading zero
   per column (count only) and S3-engine turns a non-zero count into a
   value-free REVIEW note. The CLI's integer-to-hash rule is unchanged. The
   integrity pass (S1b) reads CSV key columns as text, as run does.
3. **H2, `--config` shape.** `--config` accepts a full pipeline or a
   sources-only file (`sources:` plus optional `relationships:`), validated as
   a `SourceDescriptor` mapping and a `RelationshipConfig` list, not as a
   `PipelineConfig`. New test T2-config-sources-only.
4. **H3, paths.** `--config` relative paths resolve against the config file's
   directory before `scan_sources`, as `decoy run` does. The renderer writes
   paths relative to the emitted YAML's directory (absolute when no relative
   path exists). New test T4-runs-other-dir.
5. **H4 (now medium), header guard.** The guard uses neutral column labels for
   its one-row frame and runs only for CSV with `has_header: true`. Guarantees
   2 and 7 are reworded: names come from the header row the user declared; the
   guard catches structured PII and all-numeric or all-date first rows, not
   names or addresses; no safety claim holds when `--has-header` is given for
   headerless data. New tests T1-header-titlecase and T1-header-fn.
6. **M2.** `scan_set_from_dict` is dropped (the CLI reads dicts); the CLI owns
   the schema-tag check (T2-schema-reject). The nested per-table `detail`
   (`ColumnProfile.to_dict()`) is declared additive-only and coordinated with
   STORM E1.
7. **M3.** T3-deterministic runs two subprocesses with different
   `PYTHONHASHSEED` values and compares bytes.
8. **M4.** T1-error-class injects a reader double whose exception message holds
   a canary.
9. **M5.** T2-parquet-compat compares shape only (keys and value types).
10. **M6.** T3-parity runs a checked-in fixture of `FieldStats` cases through
    both decision functions; the port keeps the CLI's behavior exactly,
    including its `confidence_bucket` handling, the 0.1 ambiguity window and
    the integer rule.
11. **M7.** Every data-derived scalar in the emitted YAML is quoted; a
    hostile-name fixture joins T4-validate.
12. **M8.** An emitted YAML with a cloud source carries a REVIEW note that
    `decoy run` refuses cloud I/O until CLI-CLOUD lands; new test
    T4-cloud-note.
13. **Low items.** L1 extension-to-descriptor mapping stated (S1c). L2 the init
    error paths report the exception class only and join the canary test. L3
    atomic-write details stated (section 2). L4 `scan_sources` passes
    `file_access` to `profile_columns`. L5 T2-canary excludes header cells. L6
    why `profile_source(config)` is not reused (section 2).
14. **Sizes.** S1a ~470, S1c ~690, S3-engine ~560, S3-cli ~720; S1b and S1d
    unchanged. All under 800. CLI-DIALECT is sized by its own row.

## Revision 3 (2026-11-05): changes since head 01ee6fde

1. **Q9 answered: `redact` fallback.** A column with no PII suggestion gets
   `redact`, not `passthrough` (owner: "redact is an easier way to see that
   your data is wrong than passthrough for new users or if you just make a
   mistake"). Every column is still listed. This reverses Revision 2 item 2's
   default and keeps today's `decoy init` fallback.
2. **Q10 answered.** V1 keeps every snapshot; pruning comes later through the
   local record library's retention.
3. **S1d rebuilt on the local record library.** S1d is built on the library
   core (L1, CLI plan `docs/plans/2026-11-05-local-record-library.md` on branch
   `plan/local-record-library`). The library plan owns the store, envelope,
   index and retention; S1d only writes `scan-set/v1` records through it and
   depends on L1. S1d shrinks to about 250 lines.
4. **Sampling interplay.** Section 4 notes that `decoy run --sample N` stays V1
   and must behave the same across delimited, CSV, fixed-width and Parquet
   sources, and that scan's bounded sample uses the same engine sampler.

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
record library is CLI-only and planned in LIB (the platform stores scans in its
database).

### Guarantee (what V1 promises)

1. `decoy scan A B C ...` and `decoy scan --config p.yaml [--table T ...]`
   profile every selected table in one invocation and report each one under
   the existing scan honesty and privacy rules (H4: PII candidates are
   suggestions only; H5: privacy-safe field records).
2. Scan never guesses a header. Every CSV source states `has_header`; every
   headerless source supplies its column names. Column names come from the
   header row the user declared, or from the names the user supplied.
3. Profiling memory is bounded per table: tables are scanned one at a time,
   each through the engine's bounded `ProfileSource` sample, and a table's
   frame is released before the next one is read. The integrity pass reads only
   key columns, streamed into DuckDB under a memory limit with disk spill into a
   private directory that is removed when the scan ends. Two exceptions, both
   stated on the card: fixed-width key columns are read whole (no bounded
   fixed-width iterator exists), and cloud sources get no integrity pass in V1.
4. Scan reports integrity facts: per table, null counts and key candidates in
   the scanned rows; per relationship declared in `--config` whose tables are
   local files, exact parent duplicate-key count over complete non-null parent
   keys, child null-key count and orphan row count. A relationship that names a cloud table reports
   `skipped_cloud` and no counts. It never prints a key value.
5. Every scan saves one value-free record of kind `scan-set` (payload tag
   `scan-set/v1`) to the local record library under `.decoy/library/`, unless
   `--no-save` is given. LIB slice L2 builds this; this plan only defines the
   payload. A failed save warns and keeps the exit code (LIB 4.17).
6. `--emit-config p.yaml` writes one starter pipeline YAML: an explicit source
   and a target for every scanned table, one column entry for every column with
   a suggested strategy (`redact` when nothing is suggested; `hash` with the
   relationship namespace for a key column of a declared relationship),
   `# REVIEW:` comments, a `scan_baseline:` reference to the saved record, and a
   commented-out `relationships:` template naming the scanned tables. The file
   passes `decoy validate config` and `decoy validate compile`, except in one
   case by design: a declared relationship with no namespace gets a placeholder
   namespace that validation refuses until the user sets it (owner answer
   Q11).
7. No human output, `--json` output, library record or emitted YAML contains a
   cell value. Column names are printed by design, since they come from the
   declared header. `--output` files hold only the value-free core too. The
   engine's richer per-table detail, which can hold cell values, is written
   only by `--output-detail`, only into protected library scratch that
   expires after 24 hours (LIB L2), and never next to the user's files. This guarantee
   assumes the header declaration is right: when `--has-header` is given for a
   headerless file, the first data row becomes the column names and is printed.
   The header guard warns on structured PII (emails, SSNs, phone numbers and
   the like) and on all-numeric or all-date first rows; it does not catch a
   first row of names or addresses.
8. A single-file `decoy scan FILE.parquet` keeps today's `--json` keys. Two
   deliberate pre-GA changes (section 5 S1c): a single CSV now needs a header
   flag, and `--output` writes the value-free `scan-set/v1` core instead of
   the rich `profile.to_dict()`.

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
- No behavioral change to the engine's legacy
  `DEFAULT_STRATEGY_BY_DETECTOR` public mapping, which the paused platform
  still reads (owner answer Q5). S3-engine.1 derives that same mapping from
  the one detector descriptor registry so the starter does not add a copy.
- No record pruning in V1 (owner answer Q10). Retention belongs to the local
  record library plan.
- No new masking behavior.

### Failure modes

| Case | V1 behavior |
|---|---|
| CSV given with neither `--has-header` nor `--no-header` | Usage error (exit 2) before any read, naming the file. Hint shows both flags. |
| `--config` CSV source without an explicit `has_header` | Usage error naming the source. This CLI check is deleted when CONFIG-EXPLICIT makes the field required in the schema. |
| `--no-header` with no `--column-names` / `--column-names-file` | Usage error. The engine's `FileSource` already rejects `has_header: false` without names. |
| Column-name count does not match the file's width | The engine reader's existing record-width error, reported with the table name and error class only. |
| Header given (`--has-header`) but the first row looks like data | For CSV sources with `has_header: true` only, the header guard runs the STORM detectors over the header cells, in a one-row frame with neutral column labels (`c0..cn`) so name-based hints cannot fire. If a cell matches a structured PII detector, or every cell parses as a number or a date, a `warning.scan_header_suspect` warning names the table (never the cell). The guard never changes the reading, and it misses a first row of names or addresses (Guarantee 7). |
| CSV typing (`"007"` against `7`) | Profiling keeps the engine `CsvFileSource` type inference, so a zero-padded code profiles as an integer, while `decoy run` reads CSV as text. Scan counts sampled text values with a leading zero per column (count only, no value) and the starter YAML carries a REVIEW note for such columns. |
| Two files share a stem, or a stem is not a usable table name | Usage error before any read for a collision (no silent suffixing). Unusable names are normalized to `[a-z0-9_]`, a leading digit gets a `t_` prefix, and the card shows `name <- path`. |
| Huge file | Bounded profiling sample per table (`--rows`, default 10 000; `--rows 0` reads everything). The integrity pass reads full key columns and the card says so; `--skip-integrity` skips it. |
| Declared parent and child keys have different types (CSV string against parquet int) | Fact `key_type_mismatch`, a `warning.scan_key_type_mismatch` warning, no orphan count. No guessing or casting. |
| Nulls in a declared child key | Excluded from orphans and counted separately (SQL `MATCH SIMPLE`, the dbt `relationships` rule). |
| Duplicate keys in a declared parent | Reported as a count; the relationship is still checked. |
| Self-reference or cycle in declared relationships | Checked like any other edge; scan does not reject a cycle (the compiler does when the user runs the config). |
| One table fails to read or profile | Exit 3, naming the table and the exception class only (never `str(exc)`, which can carry cell data). No `--output`, record or `--emit-config` file is written (owner answer Q6). |
| Library not writable, or the record save fails | LIB 4.17: one warning naming the library root and the error class, `--json` carries `library: {status: "failed", code}`, and the exit code is unchanged. `--emit-config` still writes the YAML, with no `scan_baseline` and a comment saying the save failed (the `--no-save` shape). |
| Baseline ref cannot be set (`library_busy`, `library_lock_unavailable`) | The record is saved and `scan_baseline` is written; one warning says prune does not protect the record until the ref is set with `decoy library ref`. Exit code unchanged. |
| Declared relationship names a cloud table | Status `skipped_cloud`, no counts, one line on the card. No cloud read happens for integrity. |
| Declared relationship key columns | The starter YAML gives every key column of the relationship `strategy: hash` with one shared namespace, overriding any detector suggestion, with a REVIEW note naming what it replaced. `redact` would turn every key into one constant and destroy the join. |
| Declared relationship without a namespace | The key columns get the placeholder `decoy-set-namespace-<n>` (n is the relationship's position, from 1). `PipelineConfig` validation refuses any namespace with that prefix (`namespace_placeholder_unset`, naming the column path), so `decoy validate` and `decoy run` refuse the file until the user sets a real value. |
| Integrity spill directory cannot be created or is unsafe | The protected allocator (LIB L0) raises `runtime.scratch_acl_failed` (the Windows DACL or POSIX mode could not be set) or `runtime.scratch_unsafe` (an existing directory is a symlink or reparse point, not ours, or open to others and not empty). The scan stops with that code and the table pair before any key column is read; no spill file is written anywhere. |
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
- **Record store:** owned by the local record library plan (LIB), which
  carries its own established-solutions section. This plan adds no storage
  code.
- **Atomic output:** the publish shape the engine's `fileaccess._output` uses:
  a temp file in the target directory, `os.replace` onto the final name, and
  the temp file removed in a `finally` on any failure, through LIB's shared
  `src/decoy/_atomic.py`. Modes: `--output` files hold the value-free core and
  are still created 0600 (they name tables and columns); the emitted YAML
  holds no cell values and follows the process umask. The value-bearing
  detail never takes this path (LIB L2 `--output-detail`). Accepted race: the
  "target exists, refuse without `--yes`" check and the replace are not one
  atomic step, so a file created between them is overwritten. That needs a
  second local writer racing the same path and is accepted for a
  single-operator CLI.
- **Why not `profile_source(config)`:** the engine's `profile.profile_source`
  returns the planner's walk `Profile` (`profile._types`), not the STORM
  `ColumnProfile` with detector matches, PII scores and QI groups that scan
  reports, and it takes a full validated pipeline config. Scan needs STORM
  output per table from a sources-only input, so it reuses the lower layer
  (`build_profile_source` plus `profile_columns`) instead.

No custom algorithm remains in V1. The V2 FK survey is in section 7.

## 3. Design notes

- **New structure:** one engine entry point (`scan_sources`) with frozen result
  types and a declared-relationship integrity pass, one engine starter-config
  builder, one optional config field, and one CLI YAML
  renderer shared by `decoy init FILE` and `decoy scan --emit-config`.
- **Library code does not know its callers** (engine `CLAUDE.md`): the engine
  returns data (`ScanSet`, `StarterConfig`); the CLI owns cards, flags, the
  library root, comment wrapping and file writes. The platform can call
  the same engine functions and store results its own way.
- **One place per fact:** the V2-shaped detector-to-strategy table moves into
  the engine once (S3-engine) and the CLI copy is deleted (S3a-cli); a parity
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
    pipeline YAML from a scan. At S3a.1, G-ONE-EMITTER compares `decoy init`
    with a direct one-table renderer call because `--emit-config` is not built
    yet. At S3a.2 the guard moves to its final sentry: it walks both command
    entry points and compares `decoy init x.csv --has-header` with `decoy scan
    x.csv --has-header --emit-config`, after the provenance header. Mutation
    M-EMIT adds one inline YAML field in either command; the guard goes red.
  - Record writes: LIB owns the choke point. `Library.put` is the only writer
    under `.decoy/library/objects/`, guarded by G-LIB-WRITER (mutation
    M-LIB-INLINE) and G-KIND-CANARY (mutations M-KIND-NOFIX, M-PAYLOAD-LEAK),
    both in LIB. This plan adds no store guard; the earlier G-ONE-SAVER is
    dropped because G-LIB-WRITER covers it.
  - Saved-scan reads: `load_scan_set(path_or_dash)` (CLI, created in S1c,
    extended in LIB L2) is the only path-or-stdin reader of a saved scan, bare
    or wrapped in the record envelope. Guard G-ONE-SCAN-READER (S1c) walks the CLI syntax tree: no
    module outside `load_scan_set`'s own reads a `scan_schema` key or calls
    `records.decode` on a `scan-set` record. Mutation M-READ-INLINE: `--compare`
    checks `data["scan_schema"]` itself; the guard goes red.
  - Value-free output: `_safe_field_record` stays the only field renderer for
    human, `--json` and record output. `scan_set_to_dict(..., detail=False)`
    dumps the `ScanSetCore` model (S1a), which has no field for `top_values`,
    min, max or mode.
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
  `decoy validate preflight` in V1, both through `load_scan_set`. Whether
  `decoy run` reads it is left open; LIB L3a only links the run-result record
  to the baseline record and does not read its payload.
- `StarterConfig` is a plain config dict plus notes, so adding opt-in blocks
  later is an additive builder field.
- **Sampling.** The owner confirmed that `decoy run --sample N` stays in V1 and
  must behave the same across delimited, CSV, fixed-width and Parquet sources
  (it is planned elsewhere and is not on CLI main at 8fba6af). Scan does not
  change `run --sample`. Scan's bounded sample (`--rows N --strategy head`)
  goes through the engine's per-format `ProfileSource.sample_frame`, so the rows
  scan profiles are the rows a head sample of the same size reads. If the
  `run --sample` work settles on a shared engine sampler, `scan_sources` calls
  that sampler instead, with no change to scan's flags. S1a adds test
  T1-sample-parity: the same 1 000-row head sample from one dataset stored as
  CSV, delimited (TSV), fixed width and Parquet profiles the same row count and
  the same per-column null and distinct counts. The fixture has no empty cells:
  blank fixed-width fields, empty CSV strings and Parquet nulls do not map to
  one another today, and this plan does not claim a null normalization.

## 5. Slices

The CLI pins `decoy-engine==0.18.0`; every repin follows the engine repin
checklist. Sizes are hand-written lines, code plus tests, estimated.

### 5.0 Sequencing (shared with the other plan, identical text)

This section is word-for-word the same in SCAN-MULTI (engine branch
`plan/scan-multi`) and LIB (CLI branch `plan/local-record-library`); anything
specific to one plan sits outside it. The platform `docs/ROADMAP.md` gets its
rows from the orchestrator; neither plan edits it. A slice cut into parts
(`L1b.2`, `S1c.1`) is named by its part where the part matters; a bare name
means every part has merged.

**Outside dependencies.**

- CLI-DIALECT: gated-pending handoff
  `10_Projects/decoy/handoffs/phase0.5/p05-cli-dialect-build-handoff.md` in the
  vault, branch `fix/cli-dialect-honour-csv-fields` from CLI main. Makes
  `decoy run` read CSV with `has_header`, `column_names` and `delimiter`.
  Standalone; it does not depend on CONFIG-EXPLICIT.
- CONFIG-EXPLICIT (plan in progress): the owner rule that a CSV source states
  `has_header`, names its columns through `column_names` or
  `column_names_file`, and lists every column in the YAML. Planned by another
  author. S1c.2's `--column-names-file` and sources-only declaration mode need
  E1, E2 and C1; S3-engine.1 needs E2; S3a.1 needs C1 and C2.
- RESULT (`decoy-run-result/v1`), handoff
  `10_Projects/decoy/handoffs/phase0.5/p05-result-build-handoff.md` (Status
  plan): E1 (engine model and `build_run_result`), E2 (engine per-table
  projection and builder), E3 (collector, chunked progress events and one
  registry minor), C1a (CLI document and `_run_outcome.finish`), C1b
  (evidence, notify and typed `links.log` inputs), and C2 (CLI capacity
  widening). LIB L1a's `run-result` kind points at E1's models; LIB L5a's
  chunked case needs E3; LIB L5b and L3a need C1a and C1b.
- SAMPLE (`decoy run --sample N`) for LIB L3b; not defined on any branch today.
- CLI-CLOUD for any cloud integrity pass (V2 of S1b, section 5 S1b).

**Reason-registry chain.** Engine main is at `REGISTRY_VERSION = "1.5.0"`
(`reasons/_registry.py:24`). SUB-STOP takes 1.6.0 and STORM E1 takes 1.7.0.
After those, each registry-touching slice here claims the next free minor at
merge time through REG-RENUMBER (`scripts/dev/registry_renumber.py`, handoff
`10_Projects/decoy/handoffs/phase0.5/p05-reg-renumber-build-handoff.md`): such
slices may build in parallel but merge one at a time, one new minor per branch,
and never write a version where it does not mean the slice's own minor. The
roadmap rows already on the chain (F35 E1a, F32 E1b, F36 slice 4) and RESULT E3
interleave by merge order. Registry-touching slices in these plans:

| Slice | Identities |
|---|---|
| LIB L0.1-engine | `runtime.scratch_acl_failed`, `runtime.scratch_unsafe` |
| LIB L1a.1-engine | the CLI-side library and log event codes (LIB LM8) |
| SCAN-MULTI S1a.1-engine | `warning.scan_header_suspect`, `warning.scan_key_type_mismatch` (S1b adds none) |
| SCAN-MULTI S3-engine.1 | `config.namespace_placeholder_unset` |

**Engine releases** go one at a time. Each is followed by one cumulative CLI
repin that follows the engine repin checklist. Planned grouping: LIB L0, LIB
L1a (both parts) and RESULT E1 to E3 in one release; SCAN-MULTI S1a and S1b
in the next; SCAN-MULTI S3-engine (both parts) after CONFIG-EXPLICIT.

**Build order.**

1. LIB L0.1, L0.2, L1a.1, L1a.2 and RESULT E1 to E3; one engine release. LIB
   L1b.1 may start at once (it needs no engine change); then the repin with
   L1b.2, then L0.3 in the `run.py` lane, then L1b.3, L1c.1, L1c.2 and L1d.
2. SCAN-MULTI S1a.1, S1a.2, then S1b-engine on a branch from S1a; one engine
   release holds them. S1b uses L0's allocator, so L0 merges first.
3. CLI-DIALECT, then SCAN-MULTI S1c.1 (repin to the step 2 release; needs LIB
   L1b.1).
4. CONFIG-EXPLICIT E1 and E2 and their engine release, then C1. SCAN-MULTI
   S1c.2 uses that release and C1's config loader, then LIB L2-cli (needs L1c
   and L1d). After CONFIG-EXPLICIT C2, build SCAN-MULTI S3-engine.1 and
   S3-engine.2 and their release, then S3a.1-cli and S3a.2-cli (repin), then
   S3b-cli.
5. LIB L4a after L1c, then L4b. In the `run.py` lane after L0.3: LIB L5a
   (needs L1b.1 and the step 1 release), F29 C1, RESULT C1a, RESULT C1b,
   LIB L5b (also
   needs L4a), LIB L3a, LIB L3b; SAMPLE gates L3b. LIB L5c after L5b and
   S3b-cli.

Steps 1 and 2 can run at the same time on different branches; their releases
go out one after the other.

**One writer at a time per shared file.** The roadmap allows one `run.py`
writer at a time (`ROADMAP.md:548`), and the same rule applies here to the
other files several slices edit. Default order, which the orchestrator may
interleave with other rows (F32 C0 and CLI-FMT also write `run.py`):

| File | Writers, in order |
|---|---|
| `src/decoy/cli/run.py` | L0.3, L5a, F29 C1, RESULT C1a, RESULT C1b, L5b, L3a, L3b |
| `src/decoy/cli/scan.py` | S1c.1, S1c.2, L2, S3a.2, S3b, L5c |
| `src/decoy/cli/library.py` | L1c.2, L4a, then L4b and L5b in either order |
| `src/decoy_engine/profiling/scan_set.py` | S1a.1, S1a.2, S1b |

### S1a-engine: `scan_sources` and `scan-set/v1` (engine, two parts, ~890 lines)

Parts (Totals): S1a.1 is everything below except T1-sample-parity, which is
S1a.2.

**Files.**
- new `src/decoy_engine/profiling/scan_set.py`: `scan_sources`, `TableScan`,
  `ScanSet`, `scan_set_to_dict`, and the core pydantic model `ScanSetCore`
  (with its nested field, table and relationship models). No
  `scan_set_from_dict`: the CLI consumes the dict form, and the schema-tag
  check lives in the CLI reader (S1c).
- `src/decoy_engine/records/_kinds.py`, only if LIB L1a has merged first:
  point the `scan-set` kind at `ScanSetCore` (write model) and its read
  variant. If S1a merges first, L1a does this instead (LIB XM3).
- `src/decoy_engine/profiling/__init__.py`, `src/decoy_engine/__init__.py`:
  additive exports.
- `src/decoy_engine/fileaccess/__init__.py`: add to `ENTRY_POINTS`; sentry
  baselines refreshed through their own tooling.
- `src/decoy_engine/reasons/warning/_part1.py`: `warning.scan_header_suspect`
  and `warning.scan_key_type_mismatch` (S1b's identity, registered here so the
  pair takes one minor); one manifest fixture; the minor is assigned at merge
  by REG-RENUMBER (section 5.0).
- `docs/compatibility-contract.md` (§3.1 tag list, §3.4 note),
  `docs/reasons.md`, `CHANGELOG.md`.
- new `src/decoy_engine/profiling/_scan_text_checks.py`: the suspect-header guard and the
  leading-zero count, split out of `scan_set.py` to keep it under the size budget.
- `src/decoy_engine/execution/__init__.py`: export `refuse_duplicate_field_names` so the scan
  calls the one duplicate-name refusal through the public execution boundary.
- tests: `tests/unit/profiling/test_scan_set.py` and
  `tests/unit/profiling/test_scan_sample_parity.py`.

**Placement** (against CODEMAP "Where Things Go" on `7a61be3c`).

| New file | CODEMAP row | Neighbor | Directory count |
|---|---|---|---|
| `src/decoy_engine/profiling/scan_set.py` | `profiling/` (Profiling and detectors) | `profiling/profiler.py` | 9 direct modules after, under the 25 cap |
| `src/decoy_engine/profiling/_scan_text_checks.py` | `profiling/` | `profiling/_patterns.py` | same |
| `tests/unit/profiling/test_scan_set.py`, `test_scan_sample_parity.py` | `tests/unit/<package>/` | `tests/unit/profiling/` existing tests | existing directory |

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
    spill_dir: Path | None = None,        # S1b; None = private temp dir
    file_access: FileAccess | None = None,
) -> ScanSet
```

- Validates each descriptor through `SourceDescriptor`. For every local CSV,
  its auxiliary passes obtain a `CsvDialect` from `resolve_csv_dialect` and
  pass that object to every `read_csv_source` call; `build_profile_source`
  continues to reach the same resolver internally. No scan code reads `has_header`, `column_names`,
  `column_names_file` or `delimiter` from the descriptor, and there is no
  header inference.
- For each selected table, in order: `build_profile_source(...)`, bounded
  sample, header guard (CSV with `has_header: true` only),
  `profile_columns(frame, source_label=name, ..., file_access=file_access)`,
  drop the frame.
- Header guard: when the resolved dialect has `has_header: true`, the header
  cells come from `read_csv_source(..., nrows=0)` and feed detectors as a
  one-row frame whose
  column labels are neutral (`c0..cn`), so the detectors' name hints cannot
  fire on the header text. On a structured PII match, or when every cell
  parses as a number or a date, attach a `warning.scan_header_suspect`
  `ReasonRef`. Reading and names are unchanged.
- Leading zeros (local CSV only, strategies `head` and `full`): the same rows
  are re-read as text through `read_csv_source(path, dialect, dtype=str)`, with
  `nrows=N` for `head` and `chunksize=<bounded size>` for `full`. Per column, the number of
  non-null values matching `^0\d` is stored as `leading_zero_count`. Count
  only; each text chunk is dropped at once. With `strategy="random"` the
  sampled row positions are not reproducible from the file, so
  `leading_zero_count` is `None` and the card says "leading zeros not checked".
  Profiling itself keeps type inference.
- `ScanSet` (frozen): `tables`, `relationships` (S1b facts, empty here),
  `scan_schema = "scan-set/v1"`, `engine_version`, `scanned_at`.
  `TableScan`: `name`, `descriptor`, `profile`, `warnings`,
  `row_count_exact`, `key_candidates` (columns unique and non-null within the
  scanned rows, labeled as sample-based), `null_counts`,
  `leading_zero_counts`.
- `ScanSetCore` is the one definition of the value-free core (per table the
  descriptor, `_safe_field_record`-equivalent field facts, key candidates,
  null counts, leading-zero counts, warnings, reid score and QI groups as
  names; the `relationships` facts from S1b). Its config is `extra="forbid"`
  for writes. A read variant of the same fields with `extra="ignore"` (nested
  models included) serves older readers of newer records (LIB LH1). Non-finite
  floats are mapped to `None` before validation (a rate over zero rows is
  `None`, not NaN; LIB LL4).
- `scan_set_to_dict(s, *, detail: bool)`: builds `ScanSetCore` and dumps it;
  with `detail=True`, adds an optional per-table `detail` member carrying
  `profile.to_dict()`. The safe field-record shape moves into the engine here
  so CLI and records share it. `detail` is the nested `ColumnProfile` dict
  and is additive-only: a field may be added, never renamed or removed, within
  `scan-set/v1`. The STORM rename wave (E1) renames there go through the
  compatibility procedure and are coordinated with that row.
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
| T1-header-titlecase | header `Name,City,State,Zip` raises no warning | M1-name-hints: label the guard frame with the header cells, so name hints fire |
| T1-header-fn | header row `Jane Doe,42,NY` raises no warning; the test documents this known miss (Guarantee 7) and fails if the guard's scope silently changes | M1-name-detector: add the person-name detector to the guard's detector set (a warning appears, red) |
| T1-no-guard-parquet | a parquet table never runs the guard (detector spy) | M1-guard-all: run the guard for every format |
| T1-leading-zero | a CSV column with values `007`, `012`, `5` gives `leading_zero_count == 2`, profiles as integer, and the count field carries no value; the same with `strategy="full"` | M1-zero-from-typed: count on the inferred frame (always 0) |
| T1-leading-zero-random | `strategy="random"` gives `leading_zero_count is None` for every column | M1-random-head: re-read the first N rows for a random sample |
| T1-core-model | `scan_set_to_dict(s, detail=False)` equals `ScanSetCore.model_validate(...).model_dump()` of itself; a dict with an extra field fails the write model and passes the read variant; once both slices are in, `records.kind_spec("scan-set").write_model is ScanSetCore` | M1-second-shape: build the core dict by hand beside the model |
| T1-empty-table | a CSV with a header and no rows scans; every rate field is `None` and the dict passes `allow_nan=False` encoding | M1-nan: divide by the zero row count |
| T1-core-value-free | canary in every cell: `scan_set_to_dict(s, detail=False)` contains no canary | M1-leak: put `top_values` in the core |
| T1-schema-tag | both dict forms carry `scan_schema: "scan-set/v1"`; `detail=False` has no `detail` key | M1-tagless: omit the tag |
| T1-fileaccess | deny-all `FileAccess` raises `decoy.file_access_denied` before any read | M-FA |
| T1-error-class | a reader double raises an exception whose message holds a canary; the raised error names the table and the exception class, and the canary appears nowhere in it | M1-exc: interpolate `str(exc)` |
| T1-sample-parity | one dataset with no empty cells, as CSV, TSV, fixed width and Parquet: a 1 000-row head sample gives the same row count and per-column null and distinct counts (section 4) | M1-skip-header-row: drop the first data row for CSV only |

**Frozen-contract effects.** Additive public API (§4.1). New persisted tag
`scan-set/v1` (§3.1); the optional `detail` member is additive; a core shape
change mints `v2` with a kept `v1` reader. Records will be read across engine
versions, so a `scan-set/v1` sample joins the compatibility corpus. Two warning
identities at one registry minor, assigned at merge. No determinism effect.

**V2-LOG.** Cloud CSV dialect support remains deferred. `S3Source` and
`GCSSource` have no dialect fields and use `extra="forbid"`; CONFIG-EXPLICIT
explicitly leaves cloud outside its header and column-name rules, and the CLI
cannot run cloud sources until CLI-CLOUD. S1a.2 therefore keeps only local
format sample parity and makes no schema or cloud-reader change.

### S1b-engine: declared-relationship integrity facts (engine, ~740 lines)

Depends on S1a (branch from it) and on LIB L0 (the protected allocator).

**Files.**
- new `src/decoy_engine/profiling/_scan_integrity.py`: key-column streaming and
  the DuckDB anti-join.
- `src/decoy_engine/profile/_csv_reader.py`: additive `columns` projection on
  `read_csv_source` and `_read_csv_rows`; its existing unit test file covers
  headered and headerless whole and chunked projection.
- `scan_set.py`: wire the `relationships`, `integrity` and `spill_dir`
  keywords; add `RelationshipFacts`.
- No registry edit: `warning.scan_key_type_mismatch` is registered by S1a
  (Revision 5, SM1). S1b branches from S1a's branch and ships in the same
  engine release.
- tests: `tests/unit/profiling/test_scan_integrity.py`.
- `docs/compatibility-contract.md` section 3.4 and `CHANGELOG.md`: document
  the additive keyword-only projection and that order follows `columns`.

**Behavior.**
- Runs only when `integrity` is true and `relationships` is non-empty. Each
  relationship is validated through `RelationshipConfig`; tables it names must
  be in `sources` (they need not be in the profiling selection).
- Streams only the key columns of local files: parquet through
  `LazySource.iter_batches` with column projection; CSV through
  `read_csv_source(path, resolved_dialect, dtype=str, chunksize=<bounded size>,
  columns=<relationship key columns>)`; fixed width is read whole for its key
  columns (no bounded fixed-width iterator exists) and the fact carries
  `exact: true, bounded: false`.
- S1b extends CLI-DIALECT's public `read_csv_source` and its private
  `_read_csv_rows` with one additive keyword-only `columns: Sequence[str] |
  None = None`. The central reader translates it to pandas `usecols`; its
  existing `read_csv_chunks` adapter remains the only pandas chunk seam.
  Because pandas ignores `usecols` order, the central reader reorders every
  returned whole frame or chunk to the exact `columns` sequence. Headerless
  projection resolves declared names to positions inside the same reader and
  applies the same final order. No code in `_scan_integrity.py` calls pandas,
  opens a CSV handle or reads dialect keys. CLI-DIALECT AT-E8 and
  CONFIG-EXPLICIT AT-E1-9 are the
  guards; adding `pd.read_csv(..., chunksize=...)` in `_scan_integrity.py`
  must turn them red.
- Cloud sources: no integrity read in V1. A relationship whose parent or any
  child is an S3 or GCS source gets `status: "skipped_cloud"` and no counts,
  before any read. The cloud CSV readers have no bounded path
  (`S3CsvSource.to_frame` downloads the whole object through `_s3_full`,
  `profile/_cloud_readers.py:218,290-293`; the GCS reader likewise, `:393`).
  Revision 5 gives the reason and the V2 path.
- Spill directory: `connect_duckdb(temp_dir=...)` requires one
  (`execution/out_of_core/_duckdb.py:32`), and the spill holds key values,
  which are row data. With `spill_dir` given, the pass uses a fresh
  subdirectory of it created through LIB L0's `protected_scratch_dir`; with
  `None`, the same function creates it in the system temp directory. That
  function sets 0700 on POSIX and the owner-only DACL on Windows at creation,
  and raises `scratch_acl_failed` (no read happens) when it cannot. Either way
  the directory is removed on success and on error. The CLI passes `None`
  until LIB L2, then `Library.scratch("scan-integrity", delete_on_exit)`, so
  scratch stays the only Decoy area that holds rows.
- Batches are registered into a DuckDB connection from `connect_duckdb` with a
  memory limit and that spill directory, then: parent rows, parent rows with a
  null key component, parent duplicate excess, child rows, child rows with any
  null key component, orphan rows (anti-join on all key columns, null child
  keys excluded) and orphan distinct keys. Parent duplicate excess is computed
  only after filtering out every parent row with any null key component:
  `non_null_parent_rows - distinct_non_null_parent_keys`. Composite distinct
  keys and null filtering use the full key tuple. Composite anti-joins use every
  listed column.
- Key types are compared as `decoy run` reads them: CSV key columns are read as
  text (run reads CSV with `dtype=str`), other formats keep their declared
  types. A mismatch gives `status: key_type_mismatch` and no counts.
- `RelationshipFacts`: `parent`, `children`, `status` (`checked`,
  `key_type_mismatch`, `skipped_cloud`), the counts above, `exact: bool`,
  `bounded: bool`. Counts only; no key value is ever returned.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T5-orphans | parent 1..100, child with 7 rows pointing at 101..103 and 5 null keys: orphan rows 7, distinct 3, null 5 | M5-nulls: count null keys as orphans |
| T5-composite | two-column key with one partial match counts as an orphan | M5-first-col: anti-join on the first column only |
| T5-dupes | unary and two-column parents mix null and partially null keys with genuine repeats; only repeated complete non-null keys contribute to duplicate excess | M5-dupe-nulls: include null-bearing rows in the subtraction; M5-distinct: drop the duplicate count |
| T5-type-mismatch | CSV string child against parquet int parent gives `key_type_mismatch`, no counts | M5-cast: cast both sides to string |
| T5-undeclared | two tables with matching `*_id` names and no declared relationship produce no facts | M5-infer: derive edges from names |
| T5-bounded | with `memory_limit` set to 64 MB and 40-character text keys (5 million child rows, about 200 MB of key data), the pass completes; a test seam that runs just before the result is fetched reads `duckdb_temporary_files()` and sees at least one file, and the spill directory was non-empty at that point | M5-pandas: do the anti-join in pandas `isin` |
| T5-csv-choke | headered and headerless CSV relationships request key columns in reverse file order and receive that exact order through `read_csv_source`; a restrictive file policy remains in force for every chunk; the CSV sentries stay green | M5-direct-csv: call pandas with `chunksize` in `_scan_integrity.py`; M5-file-order: omit the final projection order |
| T5-spill-dir | with `spill_dir` given, every DuckDB temp file appears under it, and the subdirectory is gone after the scan, also when the anti-join raises | M5-keep-spill: skip the `finally` cleanup |
| T5-spill-default | with `spill_dir=None`, the spill directory comes from `protected_scratch_dir` (spy), is mode 0700 on POSIX, has the protected owner-only DACL on the Windows lane (read back with `GetNamedSecurityInfoW`), and is gone after the scan | M5-shared-tmp: pass the system temp directory itself; M5-mkdtemp: create the spill with `tempfile.mkdtemp` (also G-ONE-PROTECTED-DIR red) |
| T5-spill-acl-fail | with the allocator forced to raise `scratch_acl_failed`, the scan stops with that code before any key column is read (reader spy) and no spill directory remains | M5-acl-continue: fall back to an unprotected directory |
| T5-cloud-skip | a declared relationship with an S3 parent (reader double) gives `skipped_cloud`, no counts, and the reader double records no read | M5-cloud-read: call the cloud reader's `to_frame` |
| T5-value-free | canary keys: no canary in `RelationshipFacts` or its dict form | M5-sample: attach example orphan keys |
| T5-skip | `integrity=False` reads no key column (reader spy) | M5-ignore: run regardless |

**Frozen-contract effects.** Additive fields on `ScanSet` and `scan-set/v1`
(the `relationships` member) inside the same unreleased tag as S1a. No new
reason identity. New optional `spill_dir` and `read_csv_source(columns=...)`
keywords (additive API).

### S1c-cli: `decoy scan` over many tables (CLI, two parts, ~1 270 lines)

Parts (Totals): S1c.1 is positional files, header flags, names, the card,
`--json` and the core-only `--output`; S1c.2 is `--config`, `--table`,
relative paths, the integrity block, `load_scan_set` and its guard.

Depends on S1a and S1b released and repinned, on LIB L1b.1 (the shared atomic
writer for `--output`), and on CLI-DIALECT (handoff
`p05-cli-dialect-build-handoff.md`); `--column-names-file` also depends on
CONFIG-EXPLICIT E1. S1c.2 additionally depends on CONFIG-EXPLICIT E2 and C1
for the sources-only declaration mode and config-load sentry.

**Files.**
- new `src/decoy/cli/_scan_multi.py`: argument resolution (positional files to
  explicit descriptors, `--config` loading and `--table` selection, name
  normalization and collisions), the multi-table card, the integrity block and
  the `--json` envelope. Kept out of `scan.py` (already 1442 lines).
- new `src/decoy/cli/_scan_set_io.py`: `load_scan_set(path_or_dash) -> dict`,
  the one reader of a saved scan. `-` reads UTF-8 JSON from stdin, preserving
  `scan.py:383-405`; a filesystem path uses the same decoder on its file.
  S1c builds two bare-input branches. With a
  top-level `scan_schema`: require the major `scan-set/v1`, else usage error
  (exit 2) naming the tag found. Without one: the legacy branch for files
  that `decoy scan --output` wrote at 8fba6af and before (`profile.to_dict()`,
  for example `row_count`, `source_label`, `fields`; see
  `tests/e2e/test_scan_command.py:85`). It is strictly shape-checked: the
  required keys and their JSON types are taken from a checked-in fixture,
  `tests/fixtures/scan/legacy_profile_8fba6af.json`, produced by running
  `decoy scan --output` at 8fba6af (the capture script is checked in beside
  it); a file missing a required key or with a wrong type is a usage error
  ("not a saved scan Decoy can read"), never a guess. A legacy file is
  presented as a one-table set named from `source_label`. Its `top_values`
  are never printed: `fields` and `show` render through
  `_safe_field_record`. LIB L2 adds the envelope branch to this same
  function.
- `src/decoy/cli/scan.py`: `SOURCE` becomes `SOURCES...`; new options
  `--config`, `--table` (repeatable), `--has-header` / `--no-header`,
  `--column-names a,b,c`, `--column-names-file PATH`, `--skip-integrity`. Every
  invocation goes through `scan_sources`, with `spill_dir=None` until LIB L2
  passes library scratch. `fields`, `show` and `diff` read saved scans only
  through `load_scan_set` and take `--table` (required when the set holds more
  than one table). Their existing `-` contract remains. Either `diff` operand
  may be `-`, but both may not be `-`; the command rejects that combination
  before reading stdin.
- docs: scan epilog, CLI reference, `CHANGELOG.md`.
- tests: `tests/e2e/test_scan_multi.py`, `tests/e2e/test_scan_command.py`, the
  guard G-ONE-SCAN-READER in `tests/sentry/test_scan_reader.py`, registered in
  the sentry registry.

**Behavior.**
- Header flags apply to every positional CSV; a mix of header styles needs
  `--config`. `--column-names` and `--column-names-file` require `--no-header`
  and exactly one positional CSV (several headerless files need `--config`).
- `--format` and `--layout` apply only with exactly one positional file.
- Extension to descriptor (unchanged from today's scan, now stated): `.csv`
  becomes `format: csv`; `.tsv` becomes `format: csv` with `delimiter: "\t"`;
  `.parquet` and `.pq` become `format: parquet`; `.fwf`, `.dat`, `.fixed` and
  `.fw` become `format: fixed_width` and need `--layout`; any other extension
  becomes `format: csv` with a comma. `--format` overrides the mapping.
  Positional paths are made absolute against the current directory.
- `--config p.yaml` accepts two shapes: a full pipeline config, or a
  sources-only file holding `sources:` and an optional `relationships:` (the
  same keys and value shapes as a pipeline config; `version` optional and, when
  present, must be 2; any other top-level key is ignored in a full pipeline and
  rejected in a sources-only file). The engine boundary validates only those
  two keys through the existing `SourceDescriptor` and `RelationshipConfig`
  schemas. A full pipeline is not validated as a `PipelineConfig`, so its
  cross-table rules (tables and targets required) do not block a scan.
- This is not an exemption from CONFIG-EXPLICIT. E2's one engine
  `prepare_declared_sources` boundary gains a typed
  `SourcesOnlyDeclaration` request carrying the source mapping and optional
  relationships. It validates each `SourceDescriptor`, applies the shared CSV
  dialect rules, resolves schema metadata and returns the same opaque prepared
  capability, but deliberately does not apply the table and every-column
  declaration checks because scan exists to discover those columns. The
  normal `PipelineDeclaration` request remains unchanged. There is no boolean
  mode flag, second source validator or CLI `TypeAdapter` copy.
- CONFIG-EXPLICIT C1's `_config_load` resolves relative paths against the
  config file's directory and passes the sources-only request to that engine
  boundary. S1c.2 takes descriptors and relationships only from the returned
  capability before `scan_sources`; it never rereads the YAML dict. Selection
  defaults to every source; integrity runs for the declared relationships.
  The CLI reference documents both accepted shapes.
- AT-C8 sentry rule in CONFIG-EXPLICIT terms: every registered command that
  both loads a pipeline config and can reach rows must consume the default
  prepared capability, or be declared as a sources-only consumer and consume
  a capability returned by the same `prepare_declared_sources` boundary. The
  AST/import walk derives both cases from the mounted command tree. A direct
  reader, direct `TypeAdapter`, or `scan_sources` call fed from the raw YAML is
  a failure. Seed `scan --config` with a raw descriptor call and the sentry
  must turn red.
- Output: a summary card (table, rows scanned, columns, PII candidates, reid
  risk, key candidates), then an integrity block per declared relationship
  (`orders.customer_id -> customers.customer_id: 7 orphan rows (3 keys), 5 null,
  parent duplicates 0`), then the H4 honesty block once. `--json`:
  `{"command": "scan", "status": "ok", "scan_schema": "scan-set/v1",
  "pii_note": ..., "tables": [...], "relationships": [...]}`; a single parquet
  file keeps today's flat keys. `--output scan.json` (and `--output -`, which
  streams to stdout) writes `scan_set_to_dict(detail=False)`, the value-free
  core, for one file or many, through `_atomic.write_atomic`. Today's
  single-file `--output` writes `profile.to_dict()` (`scan.py:768`), whose
  `top_values` hold cell values; that stops here (pre-GA breaking change,
  CHANGELOG line). The detail file is LIB L2's `--output-detail`. `fields`,
  `show` and `diff` read the dict form through
  `load_scan_set`, which rejects an unknown `scan_schema` major (the engine has
  no dict reader). A single-file bare `profile.to_dict()` from an older
  `--output` is read by the legacy branch above.
- Pre-GA change: a single CSV without a header flag is now a usage error
  (owner rule). CHANGELOG entry; the scan epilog examples gain the flag.
- Exit codes unchanged: 0 ok, 2 usage, 3 runtime. Integrity findings never
  change the exit code in V1.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T2-multi | three files: three tables in argument order in card and `--json` | M2-sort: sort by name |
| T2-header-required | a CSV with no header flag exits 2 before any read | M2-default: default to `has_header: true` |
| T2-headerless | `--no-header --column-names a,b` profiles the first row as data (needs CLI-DIALECT) | M2-drop-names: ignore `--column-names` |
| T2-tsv | `x.tsv` is scanned as `format: csv` with a tab delimiter | M2-comma: treat `.tsv` as comma-separated |
| T2-config-header | a config CSV source without explicit `has_header` exits 2 | M2-trust-default: accept the schema default |
| T2-config-sources-only | a file with only `sources:` and `relationships:` scans, runs integrity, and exits 0; a full pipeline with no `targets` also scans; both consume the `SourcesOnlyDeclaration` capability from `prepare_declared_sources` | M2-full-validate: validate through `PipelineConfig`; M2-boundary-bypass: feed `scan_sources` from the raw YAML |
| T2-config-relative | a config in `cfg/` naming `data/a.csv` scans `cfg/data/a.csv` when run from another directory | M2-cwd: resolve against the current directory |
| T2-schema-reject | `scan fields` on a dict with `scan_schema: "scan-set/v2"` exits 2 | M2-no-check: skip the tag check |
| T2-load-bare | `fields`, `show` and `diff` given the same bare `scan-set/v1` file give the same table list; each call goes through `load_scan_set` (spy) | M2-own-reader: `diff` parses the file itself |
| T2-legacy-fields | `scan fields` on the 8fba6af legacy fixture exits 0 and lists every field of the fixture; its `top_values` canary appears nowhere in stdout or stderr | M2-legacy-reject: require `scan_schema` (the fixture then exits 2) |
| T2-legacy-show | `scan show` on the legacy fixture exits 0 and reports its row count | M2-legacy-reject |
| T2-legacy-diff | `scan diff` between the legacy fixture and a `scan-set/v1` file of the same data reports no drift; against a fixture copy with one field removed it reports that field | M2-legacy-reject; M2-legacy-wrong-table: name the table from the file name instead of `source_label` |
| T2-legacy-reject | a dict with `row_count` but no `fields`, and one where `fields` is a string, each exit 2 with the fixed message | M2-legacy-loose: accept any dict without `scan_schema` |
| T2-stdin-legacy | pipe the legacy fixture to `fields` and `show`; `diff - FILE` and `diff FILE -` match the file-only result | M2-file-only: call `Path.read_text` for `-` |
| T2-stdin-core | pipe a bare `scan-set/v1` file to `fields` and `show`; `diff - FILE` and `diff FILE -` match the file-only result | M2-file-only |
| T2-stdin-double | `diff - -` exits 2 with the existing fixed error before reading stdin | M2-read-twice: allow both operands to consume stdin |
| G-ONE-SCAN-READER | syntax-tree walk of the CLI package: no `scan_schema` key read and no `records.decode` of a `scan-set` record outside `_scan_set_io.py`; a seeded bypass is shown red | M-READ-INLINE |
| T2-parquet-compat | one parquet file: every key of a shape golden from 8fba6af is present in `--json` with the same value type, and the only extra key allowed is `library` (added by LIB L2) | M2-envelope: always emit the multi envelope; M2-extra-key: add any key other than `library` |
| T2-output-core | canary in every cell: `--output` for one parquet file and for three tables is a `scan-set/v1` core, has no `detail` member, and contains no canary | M2-output-detail: write `detail=True` (or `profile.to_dict()`) to `--output` |
| T2-output-stream-core | `--output -` prints the core and no canary | M2-stream-rich: stream `profile.to_dict()` |
| T2-collision | `a/x.csv b/x.csv` exits 2 before any read | M2-suffix: auto-suffix |
| T2-config-select | `--config p.yaml --table t2` profiles only t2 | M2-ignore-select |
| T2-integrity | config with one declared relationship: the card and `--json` show the S1b counts | M2-hide: drop the integrity block |
| T2-atomic | second table corrupt: exit 3, no `--output` file | M2-partial: write per table |
| T2-canary | unique canary in every data cell (header cells excluded: they are column names, printed by design): card, `--show-fields`, `--json`, integrity block, stderr and the error path carry no canary | M2-leak: add `top_values` to the field record |

**Frozen-contract effects.** CLI contract (§3.4): verb unchanged; flags
additive; positional becomes variadic. Breaking pre-GA: a CSV now needs a header
flag. The saved-scan readers accept the bare profile and `scan-set/v1`.

### S1d-cli: replaced by LIB L2 (pointer only)

S1d is not built. LIB slice L2-cli (CLI branch `plan/local-record-library`,
`docs/plans/2026-11-05-local-record-library.md`) is the only build slice for
the scan record: kind `scan-set`, payload tag `scan-set/v1` (the
`ScanSetCore` dump from S1a), stored as `.decoy/library/objects/<2>/<62>.json`,
`--no-save` and `--library DIR`, `scan diff --latest`, the envelope branch of
`load_scan_set`, and the switch of the integrity spill to library scratch. Its
tests carry S1d's T6 intents and mutation names. A failed save warns and keeps
the exit code (LIB 4.17). This reverses the earlier "persist only on
`--output`" rule (H7b) by owner direction; the record is value-free.

### S3-engine: `starter_config` and `scan_baseline` (engine, two parts, ~1 330 lines)

Parts (Totals): S3-engine.1 is the strategy port, the parity fixture, the
`scan_baseline` field and the placeholder validator with its identity;
S3-engine.2 is the `starter_config` builder.

S3-engine.1 depends on S1a and CONFIG-EXPLICIT E2 because both edit
`config/_tables.py`; S3-engine.2 depends on S3-engine.1.

**Files.**
- new `src/decoy_engine/profiling/starter_config.py`: `starter_config`,
  `StarterConfig`, `ColumnSuggestion`.
- `src/decoy_engine/profiling/recommendations.py`: extend the existing
  `DEFAULT_STRATEGY_BY_DETECTOR` authority (`:32-101` at the plan base) into
  one detector descriptor registry carrying both the existing platform
  default and the starter inference fields. Keep
  `DEFAULT_STRATEGY_BY_DETECTOR` as a derived compatibility view whose output
  is unchanged. New `src/decoy_engine/profiling/_starter_inference.py` holds
  only the decision algorithm ported from the CLI's
  `_infer_strategy_for_column`; it reads the central descriptors and has no
  detector table. The CLI `_INFERENCE_TABLE`
  (`src/decoy/cli/_init_inference.py:71`) and
  `_NAMESPACE_REQUIRED_STRATEGIES` (`src/decoy/cli/init.py:180`) are deleted
  in S3a.1. The fallback stays `redact` (owner answer Q9). The port is
  behavior-exact: the decision
  function keeps the CLI's `confidence_bucket` handling, its 0.1 ambiguity
  window and its integer-column rule as they are at CLI 8fba6af. Any later fix
  to those is its own change with a CHANGELOG line.
- new `tests/fixtures/scan_multi/cli_inference_8fba6af.json`: `FieldStats`
  cases and the CLI's decisions for them, captured by running
  `_infer_strategy_for_column` at CLI 8fba6af (the capture script is checked
  in beside it).
- new `src/decoy_engine/config/_scan_baseline.py` and `_pipeline.py`: optional
  `scan_baseline: {path: str, schema: Literal["scan-set/v1"]}`, default `None`.
  No `record_id` member (Revision 5, SL3): a library object's path already
  encodes its id, and the CLI reader checks it.
- `src/decoy_engine/config/_tables.py` (`ColumnConfig.namespace`) and
  `_relationships.py` (`RelationshipConfig.namespace`): a validator refuses any
  value starting with `decoy-set-namespace-` with
  `config.namespace_placeholder_unset`, naming the field path, never the value
  beyond the fixed prefix. `src/decoy_engine/reasons/config/_part1.py`: that
  identity; one manifest fixture; the minor is assigned at merge by
  REG-RENUMBER (section 5.0).
- exports, `docs/compatibility-contract.md` (§3.4 config note),
  `CHANGELOG.md`; tests: `tests/unit/profiling/test_starter_config.py`,
  `tests/unit/profiling/test_starter_inference.py`,
  `tests/unit/test_scan_recommendations.py`,
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
  failure is a builder bug and raises). When the config holds placeholders
  (below), the builder validates and compiles a copy with each placeholder
  replaced by a fixed test value, and separately asserts that the real config
  fails validation with `namespace_placeholder_unset` and nothing else.
- Sources: the scanned descriptor with every reading field explicit: CSV
  sources always carry `has_header`; headerless ones carry `column_names`, or
  `column_names_file` when the scan used one.
- Targets: local file sources get `<stem>.masked.<ext>` beside the input
  (today's init rule). Cloud sources get a local `masked/` directory next to
  the emitted YAML (owner answer Q4), with a REVIEW note.
- Tables: one entry per table and one column entry for every column. Detector
  suggestions come from the strategy table; a column with no suggestion gets
  `strategy: redact` and a REVIEW note saying no PII was detected, so the
  column is redacted until the user picks `passthrough` or another strategy.
  Namespaced strategies get
  `namespace: <column name>`.
- Key columns of a declared relationship (owner answer Q11).
  Every column named in a declared relationship's `parent.columns` or any
  `children[].columns` gets `strategy: hash` and one namespace shared by the
  whole relationship, overriding the detector suggestion and the `redact`
  fallback. `redact` would turn every key into one constant and destroy the
  join the user declared (Dennis probe: such a config compiles today). The
  shared namespace satisfies the engine's same-FK, same-mask rule
  (`relationships/_namespace.py`, `namespace_ambiguity`). The namespace is the
  relationship's own `namespace` when declared; otherwise it is the
  placeholder `decoy-set-namespace-<n>`, with `n` the relationship's position
  in the declared list, counting from 1. A column named by two relationships
  with different namespaces is a builder input error (`namespace_ambiguity`,
  the compiler's own code), raised before any YAML is written. The REVIEW note
  says: "Key column of a declared relationship. hash with one namespace keeps
  the join after masking; the scan suggested <strategy>." With a placeholder it
  adds: "Set the namespace on every key column of this relationship before
  running; `decoy validate` refuses the placeholder."
- A column with `leading_zero_count > 0` gets a REVIEW note: "N sampled values
  in this column start with 0. The scan read it as a number, but `decoy run`
  reads CSV as text, so check that the strategy keeps leading zeros." Count
  only.
- A cloud source gets a REVIEW note on its source and target: `decoy run` does
  not read or write S3 or GCS until the CLI-CLOUD row lands.
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
| T3-redact-fallback | an unflagged column gets `redact` with its REVIEW note | M3-passthrough: default unflagged columns to `passthrough` |
| T3-explicit-source | every CSV source has `has_header`; the headerless one has its names | M3-implicit: omit `has_header` when true |
| T3-no-inferred-relationships | without `--config`, `relationships == []`, even with matching `*_id` names | M3-infer: add `run_cross_file_walk` edges |
| T3-declared-kept | a declared relationship is copied unchanged | M3-edit: rewrite its `orphan_policy` |
| T3-baseline | `scan_baseline.path` equals the given path; omitted when `None` | M3-always: emit an empty baseline |
| T3-value-free | canary fixtures: no canary in `config` or `notes` | M3-leak: note quotes a top value |
| T3-parity | every `FieldStats` case in the checked-in 8fba6af fixture (each detector, the fallback, an ambiguous pair inside and outside the 0.1 window, the integer rule, each `confidence_bucket` value) gets the same strategy, provider and `provider_config` from the engine function as the CLI gave; `DEFAULT_STRATEGY_BY_DETECTOR` still equals its base fixture and is derived from the same descriptors | M3-drift: change one descriptor's starter provider; M3-window: widen the ambiguity window to 0.2; M3-copy-table: add a detector literal map in `_starter_inference.py` |
| T3-deterministic | two subprocesses with `PYTHONHASHSEED=1` and `PYTHONHASHSEED=2` render the same scan set; configs and notes are byte-equal | M3-order: iterate a set of table names |
| T3-leading-zero-note | a column with a non-zero `leading_zero_count` gets the note with the count and no value | M3-no-note: drop the note |
| T3-cloud-note | an S3 source gets the CLI-CLOUD REVIEW note | M3-cloud-silent: omit it |
| T3-config-field | `scan_baseline` with another schema tag is rejected; a config without it is unchanged | M3-open: make `schema` a plain string |
| T3-key-hash | a scan set with a declared relationship whose namespace is `cust` and whose parent key the detectors flag as `email`: parent and child key columns get `strategy: hash`, `namespace: cust`; the REVIEW note names `faker`; the config validates and compiles | M3-key-redact: let the `redact` fallback apply to key columns; M3-key-detector: keep the detector's `faker` |
| T3-placeholder | the same relationship with no namespace: every key column gets `decoy-set-namespace-1`; `PipelineConfig.model_validate` raises `namespace_placeholder_unset` naming each column path; replacing the placeholder with any other value validates and compiles | M3-no-guard: drop the placeholder validator; M3-split-ns: give the child a different placeholder from the parent |
| T3-placeholder-error-value-free | the validation error names the field path and the code only | M3-echo: put the whole value in the message |

**Frozen-contract effects.** Additive public API and one additive optional
config field (§3.4 config contract). The namespace validator narrows the
accepted values by one reserved prefix that no shipped config uses (pre-GA;
CHANGELOG line). One new reason identity, `config.namespace_placeholder_unset`,
at the minor REG-RENUMBER assigns. No new artifact tag.

### S3a-cli: `--emit-config`, `decoy init`, baseline ref (CLI, two parts, ~1 260 lines)

Parts (Totals): S3a.1 is `_scaffold_yaml.py`, the `decoy init` rewrite,
renderer-level T4-validate and the renderer-level form of G-ONE-EMITTER.
S3a.2 is `--emit-config`, the scan library root and the baseline ref, and
moves G-ONE-EMITTER to its final two-command sentry. It is the only part that
edits `scan.py`.

Depends on LIB L2 (the saved record and `load_scan_set`'s envelope branch),
LIB L1c (the ref lock), S3-engine released and repinned, CLI-DIALECT, and
CONFIG-EXPLICIT C1 and C2. C2 edits `init.py`'s `_build_scaffold_yaml`, which
S3a.1 deletes after preserving AT-C6 behavior through the shared renderer.

**Files.**
- new `src/decoy/cli/_scaffold_yaml.py` (~420): the one renderer (takes
  `_yaml_quote`, `_wrap_review_comment` and the column emitter from `init.py`,
  generalized to many tables), the commented relationships template, the
  key-column REVIEW notes and the keyed-secret guidance hook.
- new `src/decoy/cli/_scan_emit.py` (~150): `--emit-config` orchestration
  (library root for scan, record save hand-off, ref setting, atomic write).
  Kept out of `scan.py`, which must not grow.
- `src/decoy/cli/scan.py`: `--emit-config PATH`, `--yes`; option wiring only.
  Net line change at or below zero (the moved init helpers and old single-file
  emit path pay for it).
- `src/decoy/cli/init.py`: `decoy init FILE` (kept, owner answer Q2) now needs
  the same header flags, builds a one-table scan through `scan_sources` and
  renders with `_scaffold_yaml`. The old builder, `_load_dataframe` and
  `_init_inference.py` are deleted. Init's error paths report the table and the
  exception class only (today they print `str(exc)`), and init joins the
  canary test.
- docs, `CHANGELOG.md`; tests: `tests/e2e/test_scan_emit_config.py`, updates
  to `test_init_command.py` and `test_init_scaffold_roundtrip.py` for the table
  name and header flag.

**Behavior.**
- Library root for a scan (XH3, same rule in LIB 4.1): `--library DIR`, then
  `DECOY_LIBRARY`, then the directory of `--emit-config`, then the directory of
  `--config`, then the current directory. The record is saved there, so the
  emitted YAML and the later `decoy run <yaml>` (whose root is the YAML's
  directory) use one library.
- `decoy scan a.csv b.parquet --has-header --emit-config pipeline.yaml` writes
  the YAML atomically after every table has scanned and the record is saved.
  The header lists the scanned tables and says to read every REVIEW comment.
  `scan_baseline.path` points at the saved record's object file, relative to
  the YAML. With `--no-save`, or when the save failed (LIB 4.17 warning), the
  field is omitted and a comment says which.
- Baseline ref (XM1): after the save, set `refs/scan-set/baseline/<slug>` to
  the record id through LIB's ref API under the library lock (L1c), so prune
  keeps the baseline. Slug: the emitted YAML path relative to the library
  root's parent directory, with forward slashes, the extension dropped,
  lowercased, every run of characters outside `[a-z0-9._-]` replaced by one
  `-`, and leading `-` or `.` stripped; when longer than 48 characters, the
  first 39 characters, `-`, and the first 8 hex digits of the SHA-256 of the
  full normalized form. The result fits LIB's ref-name rule. A ref failure
  (`library_busy`, `library_lock_unavailable`) warns, keeps the YAML and its
  `scan_baseline`, and does not change the exit code.
- The relationships block is a comment when the user declared none:

  ```yaml
  # relationships:
  #   Decoy does not guess foreign keys. Add one entry per key, for example:
  #   - parent: {table: customers, columns: [customer_id]}
  #     children:
  #       - {table: orders, columns: [customer_id]}
  #     orphan_policy: warn
  #     namespace: customer_id
  #   Give the parent and child key columns that same namespace and a keyed
  #   strategy such as hash, or the join breaks after masking. Then run
  #   `decoy scan --config <this file>` to see orphan counts for each key.
  #   Scanned tables: customers, orders, payments
  ```

  The example names are fixed copy. Only the table list comes from the scan.
- Declared relationships (from `--config`) are copied unchanged; their key
  columns carry the S3-engine `hash` entries and notes, including the
  placeholder namespace when none was declared.
- Quoting: every data-derived scalar (table names, column names, paths,
  namespace values, `column_names` entries) is written through `_yaml_quote`
  as a single-quoted scalar, so names such as `yes`, `null`, `123`, `a: b` or
  `#id` survive a YAML 1.1 load unchanged.
- Paths: each source path, target path and `scan_baseline.path` is written
  relative to the emitted YAML's directory, which is how `decoy run` resolves
  them; when no relative path exists (another drive on Windows) the absolute
  path is written. Cloud URIs are written unchanged.

**Acceptance tests and named mutations.** S3a.1 does not invoke
`decoy scan --emit-config`, which does not exist until S3a.2. It builds a
three-table `StarterConfig`, calls `_scaffold_yaml.render` directly, writes the
returned bytes to a temporary file and runs `decoy validate config`, `compile`
and `preflight` on that file. Its renderer-level G-ONE-EMITTER assertion
compares `decoy init` output with direct rendering of the same one-table
starter. In S3a.2, the G-ONE-EMITTER test moves to
`tests/sentry/test_one_emitter.py`: the AST/import walk proves both `init` and
`scan --emit-config` reach `_scaffold_yaml.render`, and the three-table CLI
case matches the direct renderer. There is one guard at each branch head, not
two production mechanisms.

| Test | Asserts | Mutation |
|---|---|---|
| T4-validate | at renderer level in S3a.1, the YAML from a three-table `StarterConfig` with no declared relationship passes `decoy validate config`, `compile` and `preflight` (subprocess, exit 0); a hostile-name fixture (columns `yes`, `null`, `123`, `a: b`, `#id`, `it's`) loads back with every name unchanged | M4-target: no target for the last table; M4-unquoted: write names as plain scalars |
| T4-runs | `decoy run` on the emitted YAML for a two-table fixture exits 0 and every unflagged column comes out redacted | M4-format: `format: csv` for a parquet source |
| T4-join-kept | `--config` declares `orders.customer_id -> customers.customer_id` with `namespace: cust` (parquet integer keys); `decoy run` on the emitted YAML exits 0, the masked keys differ from the source keys, and every masked child key that had a parent still joins to a masked parent key | M3-key-redact (S3-engine): keys fall back to `redact` |
| T4-placeholder-refused | the same with no namespace: `decoy validate config` and `decoy run` exit non-zero with `namespace_placeholder_unset` naming the column paths; after replacing the placeholder on every key column, both exit 0 | M4-strip-placeholder: emit no namespace on key columns |
| T4-runs-other-dir | scan files in `data/`, emit the YAML into `out/`, run from a third directory: exit 0 | M4-verbatim: copy the source path verbatim |
| T4-same-library | `decoy scan data/*.csv --has-header --emit-config out/p.yaml` run from the repo root saves the record under `out/.decoy/library/`, nothing under `./.decoy/`, and `scan_baseline.path` resolves inside `out/.decoy/` | M4-cwd-root: resolve the scan library from the current directory |
| T4-baseline-ref | after emit, `refs/scan-set/baseline/<slug>` holds the saved id; the slug for `out/Claims Pipeline.yaml` is `out-claims-pipeline`; a 70-character path gives the hashed short form; `decoy library prune --kind scan-set --keep-last 0 --yes` (once LIB L4b lands) keeps the record | M4-no-ref: skip the ref; M4-slug-raw: use the path unnormalized |
| T4-save-failed | with an unwritable library root, the YAML is written without `scan_baseline`, carries the "save failed" comment, and the exit code is 0 | M4-dangling: write `scan_baseline` to an unsaved path |
| T4-headerless | a headerless source round-trips: the YAML carries `has_header: false` and its names (needs CLI-DIALECT) | M4-implicit: drop `has_header` |
| T4-runs-headerless | `decoy run` on the emitted YAML for a headerless CSV exits 0 and its output has the declared column names and every data row, the first included (needs CLI-DIALECT) | M4-ignore-header: make `read_source` ignore `has_header` |
| T4-cloud-note | an emitted YAML with an S3 source carries the CLI-CLOUD REVIEW note | M4-strip-notes: drop source-level REVIEW comments |
| T4-template-inert | `relationships` parses empty and the comment names every scanned table | M4-live: emit the template uncommented |
| T4-baseline | the YAML's `scan_baseline.path` resolves to the record just written | M4-abs: write an absolute path from another directory |
| T4-canary | no fixture data-cell canary in the YAML, or in init's output and error paths | M4-leak: REVIEW quotes a top value; M4-init-exc: init prints `str(exc)` |
| G-ONE-EMITTER | in S3a.1, init and a direct one-table renderer call match after the header; in S3a.2, move the guard to the sentry and assert init and `scan --emit-config` both call that renderer and produce matching bodies | M-EMIT: add an inline YAML field in either command |
| T4-atomic | a failing second table leaves no YAML file | M4-early: write before the last table |
| T4-overwrite | existing target without `--yes` exits 2; same path as `--config` always exits 2 | M4-clobber |

**Frozen-contract effects.** CLI flags additive. Pre-GA changes: `decoy init
FILE` names the table after the file (not `data`) and needs a header flag for
CSV. Unsuggested columns keep today's `redact` default. The emitted YAML is a
`PipelineConfig`. New stored ref name pattern `scan-set/baseline/<slug>`.

### S3b-cli: `--compare` and preflight (CLI, ~330 lines)

Depends on S3a-cli (the `scan.py` lane) and LIB L2 (the envelope branch of
`load_scan_set`).

**Files.**
- new `src/decoy/cli/_scan_compare.py` (~140): rescan and diff per table,
  reusing the existing scan-diff rules.
- `src/decoy/cli/scan.py`: `--compare` and `--strict` wiring only, net line
  change at or below zero.
- `src/decoy/cli/validate.py` (807 lines, must not grow): `preflight` calls one
  new helper in `_scan_set_io.py`, `check_scan_baseline(config, yaml_dir)`,
  which resolves `scan_baseline.path` and loads it through `load_scan_set`;
  validate.py gains only the call.
- docs, `CHANGELOG.md`; tests: `tests/e2e/test_scan_compare.py`,
  `tests/e2e/test_preflight_baseline.py`.

**Behavior.**
- `decoy scan --config p.yaml --compare` rescans the config's sources and diffs
  the result against `scan_baseline` (the existing scan-diff rules, per table);
  `--strict` exits 1 on drift, as `scan diff --strict` does today.
- Both `--compare` and preflight read the baseline through `load_scan_set`,
  which accepts a library record (detected by `record_schema`; id checked
  against its file name, `RecordNeedsNewer` reported as "needs a newer Decoy")
  and a bare `scan-set/v1` file (`scan_schema` major checked).
- Preflight fails with the existing missing-file error when the path is gone,
  and with `record_corrupt` when the record's id does not match its name.

**Acceptance tests and named mutations.**

| Test | Asserts | Mutation |
|---|---|---|
| T4-compare | after adding a column to one file, `--compare --strict` exits 1 and names the table | M4-noop: compare the baseline with itself |
| T4-compare-both | `--compare` gives the same drift report against a library record and against a bare `scan-set/v1` file of the same scan | M4-top-level: read only a top-level `scan_schema` (every record then fails) |
| T4-preflight-both | preflight exits 0 for an envelope baseline and for a bare file; exits non-zero for a missing path, for a record renamed to another id, and for `scan-set/v9` (needs-newer message) | M4-skip-id: do not compare the record id with its file name |

**Frozen-contract effects.** CLI flags additive. `preflight` gains one check
when `scan_baseline` is set.

### Central registries and sizing

Owner standing rule (2026-11-05): for each component, ask whether it repeats
something the CLI or engine already does, and whether one choke point would
make a later change one edit. Searched CLI 8fba6af and engine 41a86f47 for
path resolvers, atomic writers, error builders, retention, reason codes,
permission helpers, record and manifest hashing.

| Concern | Existing code found | Decision | Choke point and guard |
|---|---|---|---|
| Private spill directory | engine `execution/out_of_core/_scratch.py:26-65` (`engine_scratch_root`, `scratch_root`, `private_dir`: `mkdtemp` plus `chmod`, removed on every exit, policy-checked for caller-named parents; no Windows DACL) | One engine allocator, LIB L0's `protected_scratch_dir` (owner-only DACL on Windows, fail closed). The out-of-core helpers move onto it in L0, and S1b calls it directly; no new temp-dir code here. A caller-named `spill_dir` keeps the ambient-policy rule. | G-ONE-PROTECTED-DIR (LIB L0, mutations M-PATH-MKDIR-INLINE and M-OS-MAKEDIRS-INLINE); T5-spill-dir, T5-spill-default, T5-spill-acl-fail |
| DuckDB connection | `connect_duckdb` (`_duckdb.py:32`) | reused unchanged | existing |
| CSV dialect and row reads | CLI-DIALECT Part E's `resolve_csv_dialect` and `read_csv_source`; chunk iteration is only `fileaccess.adapters.pandas.read_csv_chunks` | S1a uses the resolved dialect for the header guard and leading-zero reads; S1b adds only the reader's `columns` projection keyword and never calls pandas | CLI-DIALECT AT-E8 and CONFIG-EXPLICIT AT-E1-9; M5-direct-csv |
| Config declaration before row reads | CONFIG-EXPLICIT E2 `prepare_declared_sources` and its opaque capability | add one typed `SourcesOnlyDeclaration` request at the same boundary; it skips table/column declaration checks but keeps source schema, dialect, path and file-access checks | CONFIG-EXPLICIT AT-C8 walks registered commands; M2-boundary-bypass |
| Table name from a file path | engine `walks/cross_file.py:239` `_table_name_from_source_label` (strips directory and extension, keeps schema-qualified names) | S1c's normalization is a different rule (`[a-z0-9_]`, `t_` prefix, collision refusal) for a different input (positional paths only). One CLI function in `_scan_multi.py`; not merged with the walk helper, which keeps connector labels verbatim. Recorded as a deliberate second rule. | one site, no registry (cross-cutting rule: no registry for one site) |
| Safe field record | CLI `scan.py:511` `_safe_field_record` | moves into the engine as part of `ScanSetCore` (S1a); the CLI copy is deleted in S1c | `ScanSetCore`; T1-core-model, T2-canary |
| Saved-scan reading | `scan.py:383-405` reads file or stdin for `fields`, `show`, `diff` | one path-or-stdin reader, `load_scan_set`; `diff` retains the one-stdin-operand check | G-ONE-SCAN-READER (M-READ-INLINE); T2-stdin-legacy, T2-stdin-core, T2-stdin-double |
| Detector-to-strategy facts | engine `profiling/recommendations.py:32-101` and CLI `_init_inference.py:71`, `init.py:180` | extend the engine authority to one descriptor per detector; derive existing `DEFAULT_STRATEGY_BY_DETECTOR` unchanged; delete both CLI copies; `_starter_inference.py` contains decision logic only | T3-parity plus an AST no-copy guard; M3-drift, M3-copy-table |
| Pipeline YAML emission | CLI `init.py` (`_yaml_quote`, `_emit_column_yaml`) | moves into `_scaffold_yaml.py` (renderer) | G-ONE-EMITTER (M-EMIT) |
| Reason codes | engine registry (`reasons/`), REG-RENUMBER at merge | new identities only through the registry | existing manifest and registry sentries |
| Atomic file writes (`--output`, emitted YAML) | CLI `cli/ci.py:111-115`, `ci/debug.py:44-48`, `run.py:1622-1686`, `distribution/` (installer, layout, first install); `scan.py` has no atomic write today | S1c (`--output`) and S3a-cli (the YAML) write through LIB's shared atomic writer, `src/decoy/_atomic.py` (LIB L1b), instead of adding another copy. S1c therefore also depends on LIB L1b. | G-ONE-ATOMIC-WRITER (LIB L1b, mutation M-ATOMIC-INLINE) |
| Record writes, hashing, retention, permissions | none in scan | all owned by LIB | LIB guards |

Sizes per slice (estimated hand-written lines; every new file under about 800).
Oversize files at 8fba6af that these slices touch: CLI `scan.py` 1442,
`run.py` 1812, `validate.py` 807, `report.py` 2382. Each slice that edits one
keeps its net line change at or below zero by moving code out, and the gate
checks it with `git diff --stat`.

Estimates below count source only; the Totals count source, tests and docs,
at about 15 lines per unit case and 25 to 35 per end-to-end case (generated
fixtures not counted). A slice over about 800 in total is cut into parts.

| Slice | New or touched file | Expected size |
|---|---|---|
| S1a | engine `profiling/scan_set.py` (new) | ~380 |
| S1a | engine `reasons/warning/_part1.py` (51) | +~20 |
| S1b | engine `profiling/_scan_integrity.py` (new) | ~260 |
| S1b | engine `profiling/scan_set.py` | +~60, stays under ~450 |
| S1c | CLI `_scan_multi.py` (new) | ~420 |
| S1c | CLI `_scan_set_io.py` (new) | ~90 |
| S1c | CLI `scan.py` (1442) | net 0 or less: the single-file path and `_safe_field_record` move out |
| S3-engine | engine `profiling/starter_config.py` (new) | ~330 |
| S3-engine | engine `profiling/_starter_inference.py` (new) | ~170 |
| S3-engine | engine `profiling/recommendations.py` (about 120) | about 300 after descriptors and the derived compatibility view |
| S3-engine | engine `config/_scan_baseline.py` (new) | ~40 |
| S3-engine | engine `config/_tables.py` (452), `_relationships.py` (49) | +~15 each |
| S3a-cli | CLI `_scaffold_yaml.py` (new) | ~420 |
| S3a-cli | CLI `_scan_emit.py` (new) | ~150 |
| S3a-cli | CLI `init.py` (627) | shrinks to ~300 |
| S3a-cli | CLI `scan.py` | net 0 or less |
| S3b-cli | CLI `_scan_compare.py` (new) | ~140 |
| S3b-cli | CLI `_scan_set_io.py` | +~40, stays under ~200 |
| S3b-cli | CLI `validate.py` (807) | net 0 or less |

### Totals

| Slice or part | Repo | Lines (source + tests + docs) | Contents | Depends on |
|---|---|---|---|---|
| S1a.1-engine | engine | ~770 (420 + 300 + 50) | `scan_sources`, `ScanSetCore`, `scan_set_to_dict`, header guard, leading zeros, both warning identities; T1 tests except the two below | none |
| S1a.2-engine | engine | ~120 (0 + 100 + 20) | local CSV, TSV, fixed-width and Parquet sample parity; T1-sample-parity | S1a.1 |
| S1b-engine | engine | ~740 (350 + 350 + 40) | integrity pass, CSV projection at the shared reader, spill through L0 | S1a (branch from it; one release), LIB L0 |
| S1c.1-cli | CLI | ~620 (360 + 220 + 40) | positional files, header flags, names, card, `--json`, core-only `--output`; T2-multi, header-required, headerless, tsv, collision, atomic, parquet-compat, output-core, output-stream-core | S1a + S1b release and repin, LIB L1b.1, CLI-DIALECT |
| S1c.2-cli | CLI | ~780 (340 + 410 + 30) | `--config` and `--table`, CONFIG-EXPLICIT sources-only capability, relative paths, integrity block, path-or-stdin `load_scan_set` bare and legacy branches with the 8fba6af fixture, G-ONE-SCAN-READER; T2-config-*, schema-reject, load-bare, legacy-*, stdin-*, integrity, canary | S1c.1; CONFIG-EXPLICIT E1, E2 and C1 |
| S1d-cli | none | 0 | replaced by LIB L2 | |
| S3-engine.1 | engine | ~650 (470 + 140 + 40) | central detector descriptors and starter decision port, parity fixture and capture script, `scan_baseline` field, placeholder validator and its identity; T3-parity, config-field, placeholder-error-value-free | S1a, CONFIG-EXPLICIT E2 |
| S3-engine.2 | engine | ~680 (340 + 320 + 20) | `starter_config` builder; T3-valid to T3-cloud-note, key-hash, placeholder | S3-engine.1 |
| S3a.1-cli | CLI | ~700 (480 + 190 + 30) | `_scaffold_yaml.py`, `decoy init` rewrite, renderer-level G-ONE-EMITTER and T4-validate, template-inert, headerless, canary | LIB L2, S3-engine release and repin, CLI-DIALECT, CONFIG-EXPLICIT C1 and C2 |
| S3a.2-cli | CLI | ~560 (190 + 340 + 30) | `--emit-config`, final G-ONE-EMITTER sentry, scan library root, baseline ref; the remaining T4 tests of S3a | S3a.1, LIB L1c |
| S3b-cli | CLI | ~330 (185 + 120 + 25) | `--compare`, preflight | S3a.2, LIB L2 |
| CLI-DIALECT | CLI | sized in its handoff | | none (branch from CLI main) |

Each slice or part stays under about 800 hand-written lines. Parts are
separate branches and gates, merged in order. Test depth: T0 per edit, T1
impact-selected per commit. The engine slices touch the reason registry, the
file-access sentry baselines, the config schema (S3) and the compatibility
corpus (S1a); the CLI slices move the engine pin. These are escalation
triggers, so each merge head gets the full suite diffed against the saved main
baseline.

## 6. Owner questions

Answered on 2026-11-05 and applied: Q1 (explicit headers, owner rule), Q2 keep
`decoy init FILE`, Q3 files only, Q4 local output directory, Q5 keep the legacy
table, Q6 fail the whole scan (`--keep-going` later), Q9 `redact` for columns
with no PII suggestion, every column listed, Q10 keep every record in V1,
prune later through the library.

Answered in Revision 6:

- **Q11. Declared key columns (Revision 5, SM2): confirmed by the owner.** Key
  columns of a declared relationship get `hash` with the relationship's
  namespace instead of `redact`; with no declared namespace the YAML writes a
  placeholder that validation refuses until it is set. Written into
  S3-engine and S3a-cli that way.

Status: pending owner confirm.

- **Q7. Scan inside the run; standard versus opt-in YAML features; whether
  `decoy run` reads `scan_baseline`.** The UX discussion owns it (section 4).
- **Q8. V2 FK discovery timing.** Section 7 is ready to plan as a V2 row.

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
