Status: plan

# GEN-CLI: one path rule and a generation-aware CLI (roadmap row 24o)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-09. Scope: engine plus CLI, V1 (owner decision
2026-11-08). Risk: R2 (compatibility: a path rule change and two public API additions). Revision 1.

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Established solutions, proportion rule), `feature-dev.md`, `testing.md` (test
depth, escalation triggers), `api-and-compatibility.md`, `cross-cutting-concerns.md` (the path rule has more
than one call site), `architecture.md` (module size, fan-out), `reuse-first.md`, `scope-discipline.md`,
`documentation.md`, `workspace-and-vcs.md`, `catalogs/recurring-defects.md`; the plan contract
(`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md`); engine `CLAUDE.md` (modules aim for 600 lines,
hard max 700); engine `docs/compatibility-contract.md:180-182` (same input plus same seed gives byte-identical
output within a major); CLI `CODEMAP.md` "Where things go" (`cli/` is frozen, owner rule 2026-11-06; CLI file
cap 900 lines); CLI `src/decoy/cli/exit_codes.py` (the integers are a public contract). House style follows
`2026-11-09-gen-fit-shape.md` (row 24l) and `2026-11-09-gen-unique.md` (row 24m). Code read at engine `main`
`84f4290d` and CLI `main` `9f603536`. CLI references below are `C:<path>:<line>` at that CLI head.

Source: first-user generation QA (CLI `docs/records/2026-11-08-cli-qa-generation.md`), Medium M2 to M6,
section 5 (language) and section 6 (command families). Roadmap row 24o (CLI `docs/ROADMAP.md:259`).

## 1. FRAME

### Problem

A first user who writes a generate pipeline hits these CLI gaps, all named in the QA record:

- **M2.** `targets.path` resolves from the config file's folder (`C:src/decoy/cli/_sources.py:27`
  `resolve_path`, used by `C:src/decoy/cli/io/_targets.py:423-445`). `snapshot_file` resolves from the
  working directory (engine `plan/_generation.py:130` `fileaccess.read_all(FileRole.SNAPSHOT, path, ...)`
  through the ambient policy, `fileaccess/_local.py:365`). The same YAML works from one folder and fails from
  another.
- **M3.** The engine's generation fidelity gate only logs (`generation/_fidelity_gate.py:178-198`
  `warn_on_low_fidelity`), so `fidelity_warn_threshold: 0.99` with a 0.385 column printed nothing. The run
  summary has no warnings (`C:src/decoy/cli/run.py:906` keeps `result.warnings` for evidence only). `--fidelity`
  on a generate run prints "no mask-table fidelity report was produced for this run." (`C:run.py:1386-1397`).
- **M4.** `validate compile --explain` on a generate pipeline prints empty tables and "tables execute in config
  declaration order (no FK dependencies detected)." (`C:compile_explain.py:434`) with 3 references declared.
- **M5.** `init --preset generate -o -` writes a file named `-`. `init <csv>` only scaffolds masking.
- **M6.** With `--master-key`, `global_settings.seed` is ignored and nothing says so.
- **Language and families.** `explain transforms` still says `masking_rule`, "13 strategies" and "a future
  --mask flag" (`C:explain.py:56-80`). No explain topic covers generation. `decoy --help` has no panels at all
  (no `rich_help_panel` anywhere in `C:src/`), so `fit`, `run` and `report fidelity` read as unrelated. There
  is no preview.

Already done, not in this plan: per-table `rows` and `path` in `run --json` and the Tables, Rows and Files card
facts (row 24e C5, CLI #151 `9f752825`, `C:run.py:1197-1243`); `explain pipeline` version 2 and the platform doc
path in `explain yaml` (row 24e C2a, CLI #146 `cfdee4e`).

### Guarantee (what V1 promises)

1. **One path rule.** A relative path written in the YAML is read relative to the folder that holds the
   YAML file. A relative path given on the command line is relative to the working directory. An absolute path
   is used as given. This holds for `targets[].path`, `sources[].path`, `sources[].column_names_file`,
   `snapshot_file` and `quarantine.output_path` (section 5.4), on every command that reads paths from the YAML:
   `run`, `validate`, `validate compile`, `validate preflight`, `ci check`, `tokenize`, the `report` verbs given
   `--config`, and the Python API. `decoy scan` reads only files named on the command line (`C:scan.py:316`)
   and a `--layout` YAML that names no files (`C:scan.py:226-262`), so the command-line half of the rule covers
   it.
2. **Paths never change identity.** The config dict the engine hashes is not rewritten.
   `pipeline_config_hash` (`plan/_compile.py:642` `_hash_config`, which includes
   `tables[].generate_columns[].snapshot_file`) and every output byte are the same as on the base build for a
   run from the config's own folder, and the same from any other folder.
3. **A run says what it did.** The human summary lists each table's rows and file, and every warning the run
   raised. `run --json` carries the same warnings as a `warnings` list (always present, empty when none). A
   generated table that scores below `fidelity_warn_threshold` raises a warning.
4. **No flag is silently dead.** `--fidelity` on a pipeline with no mask table is refused before any work.
   On a pipeline with generate tables, `global_settings.seed` set together with a master key raises a warning
   that says the seed is not used for generated tables.
5. **Generation is discoverable.** `decoy --help` groups commands into panels with a Generate panel. `decoy
   explain generation` lists every generate type and option from the engine's registry. `validate compile
   --explain` shows generated tables, their references and the generation order. `run --rows N` previews a
   generate pipeline without writing files. `init` scaffolds a generate pipeline, from a preset or from a CSV,
   and `-o -` writes to stdout.

### Invariants (as properties)

- For any config `C` in folder `A` and any working directory `W`: `run C` from `W` writes the same bytes to the
  same absolute target paths as `run C` from `A` (AT-1).
- `_hash_config(C)` is a function of the YAML content only, never of `W` or `A` (AT-2).
- Engine callers that pass no `base_dir` (the platform, direct library users) see exactly today's behavior
  (AT-4).
- Generation output bytes do not depend on whether a fidelity warning was raised (AT-8).
- `run.py` does not grow: its line count at each CLI slice head is at most 1,748 (the base).

### Non-goals (V1)

- Where relative targets land. Row 24t PROJECT-LAYOUT later resolves relative targets under
  `out/<pipeline>/<run-id>/` and prints the folder used. This plan keeps today's target rule and brings the
  other paths in line with it; 24t changes targets on top (`origin/plan/project-layout`,
  `docs/plans/2026-11-09-project-layout.md:165`: "row 24o owns sources and `snapshot_file`").
- In-run fidelity scoring of generated tables. `decoy report fidelity <source.csv> <synthetic.csv> --generate` already does
  that (OQ3).
- Folding the seed into keyed derivation (OQ4). It would change every keyed output.
- A `list column-types` command and per-option descriptions: row 24s GEN-CATALOG (OQ2).
- `run --fail-on-warning` (OQ5), JSON targets (M8), the `sequence` start default (M7), namespace (M9), boolean
  snapshots (M10), STORM wording (row 56), `report fidelity` mode detection.
- Preview of mask or mixed pipelines.

### Failure modes (each has an acceptance test in section 7)

| # | Failure | Test |
|---|---|---|
| F1 | A snapshot path that worked relative to the working directory now fails because it is read from the config folder | AT-6: the message shows the path it tried and the rule |
| F2 | A CLI entry forgets `base_dir`, so `validate` and `run` disagree on one YAML | AT-5 (walk sentry) |
| F3 | The fix rewrites `snapshot_file` in the dict, so the config hash moves with the folder | AT-2 |
| F4 | `base_dir` leaks into the strict platform policy and breaks rooted reads | AT-4 |
| F5 | The warning sink changes generated bytes or drops a warning on one execution route | AT-8 (every route) |
| F6 | `--fidelity` refusal fires after targets are written, or on a mixed pipeline | AT-11, AT-12 |
| F7 | The seed warning fires when no key is set, or says masking ignores the seed | AT-13 |
| F8 | `--rows` writes target files, evidence or notifications | AT-14, AT-15 |
| F9 | `init` from a CSV writes `snapshot_file` relative to the working directory | AT-17, AT-18 |
| F10 | A new top-level command ships with no help panel | AT-19 |
| F11 | The generation topic drifts from the engine registry | AT-21 |
| F12 | `init <csv>` fails after writing and leaves a snapshot or config behind | AT-23 |
| F13 | An isolated run cannot carry `base_dir` to its child process | AT-24 |
| F14 | The merged order walk changes the error for a reference to a non-generate table | AT-25 |

## 2. Established solutions

Search run: a web search for how Docker Compose resolves relative paths, plus the codebase precedent.

- **Config-relative paths.** Docker Compose resolves relative paths in a Compose file (build context, env
  files, bind mounts) from the project directory, which defaults to the folder of the first Compose file; when
  the file comes from stdin, from the working directory (docs.docker.com, "How Compose works" and the Build
  Specification). dbt and Cargo follow the same rule for their project files. The engine's own precedent is
  `execution/capacity.py:156-191` (`_resolve_source_path(raw_path, base_dir)`, "the pipeline YAML's
  directory"). We follow it: a caller-supplied `base_dir`, joined lexically, absolute paths unchanged.
- **Structured warnings over log lines.** The engine already carries `QualityWarning(code, provider, column,
  detail)` (`generation/pool/_events.py:15`) in `ExecutionResult.warnings`. The fidelity gate joins that
  channel instead of a new one.
- **Grouped help.** Typer's `rich_help_panel` (the git and kubectl command-group pattern).
- **Preview.** `dbt show --limit`: same pipeline, few rows, nothing written.

No custom mechanism remains beyond the threading of one keyword and one sink.

## 3. Design notes

New structure: one CLI sub-package (`cli/gen/`, section 10) and two engine accessors. Principles: one place per
fact (the order walk and the option list each live once in the engine and the CLI renders them); no flag
arguments that switch behavior (`base_dir=None` means "no config file", not a mode).

### Choke points

- **Snapshot reads (engine), by construction.** `read_and_pin_snapshots` (`plan/_generation.py:102`) is the
  one read pass for a compile; `_load_snapshot` (`generation/statistical/_spec.py:174`) is the sole open site for
  a direct caller. Both gain `base_dir: str | Path | None = None` and resolve a relative locator through one
  helper `config_relative(locator, base_dir)` in `plan/_generation.py`, which applies `os.path.abspath` to
  `base_dir` once (so a relative `base_dir` means the working directory at the call). The pinned map stays keyed
  by the declared string, so plan bytes and the hash do not see the resolved path. The read, DP verify and
  statistical check block appears twice in `plan/_compile.py` (`:184-191` and `:519-526`); E1 replaces both with
  one call to a new `pin_and_check_snapshots(config, *, base_dir)` in `plan/_generation.py`, which also offsets
  the new keyword lines in `_compile.py` (section 10).
- **Engine entries.** These take `base_dir` and pass it down:
  - `run_pipeline` (`execution/_pipeline.py:116`), typed `base_dir: str | None = None`, to `compile_plan`
    through `build_run_context` (`_pipeline_context.py:337`) and to `with_quarantine_path`
    (`_pipeline_context.py:433`, section 4);
  - `run_pipeline_isolated`: `base_dir` rides in `**run_pipeline_kwargs`, which is JSON-serialized into the
    child's payload (`execution/_isolated_run.py:425`), so callers pass an absolute string. A `Path` gets the
    existing "must be JSON-serializable" `ValueError`. `_isolated_run.py` is not edited;
  - `compile_plan` (`plan/_compile.py:115`) and `run_config_only_checks` (`:484`);
  - `validate_plan` (`plan/validate/_consolidator.py:88`) and `compile_pinned_plan_with_generation`
    (`generation/_pinned_compile.py:255`), which call `compile_plan`;
  - `estimate_job_capacity` (`execution/capacity.py:246`, compile at `:351`) and `estimate_job`
    (`execution/_job_estimate.py:82`, compile at `:130`). Both already take `base_dir` for sources; they now pass
    the same value to `compile_plan`, so one call never mixes two rules.
- **Engine compiles out of scope, by reason.** `execution/_chunked_oracle.py:246` compiles a per-table mask
  config: the mixed route hands the chunked lane `static.mask_configs[name]` (`_pipeline_mixed_route.py:214`)
  and the CLI refuses `--chunked` with generate tables (`C:run.py:718`), so it never reads a snapshot.
  `execution/native/_plan.py:163` compiles before it rejects a generate table at `:251`, so a generate config
  forced onto the native route with a relative snapshot path fails on the read instead of the native refusal.
  That is a known issue with a follow-up (section 14): move the rejection before the compile.
- **CLI entries, by guard.** Every CLI call of the engine entries above passes `base_dir`. A required keyword
  would enforce this by construction, but it breaks the platform and library callers in the same release, so
  the CLI side is a guard instead: an AST walk sentry over `src/decoy/` that resolves imports and attribute
  access (`decoy_engine.run_config_only_checks(...)` at `C:gate/_layers.py:182`) and fails on a call without
  `base_dir=`. It accepts the keyword form only. The one positional call today,
  `estimate_job_capacity(config_dump, config_path.parent)` at `C:preflight.py:456`, changes to
  `base_dir=config_path.parent` in C1, so a positional argument can never hide a missing one. It runs in the merge-gate suite. The mutation that must turn it red: drop `base_dir` from
  `C:gate/_layers.py:245`. The build report shows the walk's count rising on a seeded bypass and falling on a
  removed site.
- **`column_names_file` (CLI), by construction.** The field is read inside `resolve_csv_dialect`. The CLI's one
  call is `C:_sources.py:65` in `read_csv_frame`, which every CLI CSV read goes through (`C:_sources.py:88-161`,
  `C:init.py:192`). That call gets a descriptor copy whose `column_names_file` went through `resolve_path`, as
  `path` already does. Sources are excluded from `_hash_config`, so the copy is hash-safe; the engine does the
  same in `capacity._resolved_config_copy`. `resolve_csv_dialect` joins the `decoy_engine` watch list of the
  existing G-ONE-CSV-READ sentry (`C:tests/sentry/test_csv_read_choke_point.py:88`, beside `read_csv_source`
  and `read_fixed_width`), which already confines those calls to `cli/_sources.py`; no second rule in the walk
  sentry. Its only call today is `C:_sources.py:65`, so the allowlist and baseline do not change.
- **Warnings (engine).** `run_generation(..., on_warning=None)` (`generation/_streamed.py:319`) is the choke
  point. Every generation path reaches the gate through it: `_run_legacy` (`:507`) calls
  `_generate_tables_from_config` (`:538`), which calls `warn_on_low_fidelity` (`synthesize.py:188`). The four
  public entries in `generation/_plan_entry.py` (`generate_tables` `:147`, `generate_tables_streamed` `:216`,
  `generate_tables_from_generation_plan` `:252`, `generate_tables_streamed_from_generation_plan` `:284`) take
  the keyword and pass it to `run_generation`. The three execution callers fold the collected warnings into
  `ExecutionResult.warnings`.
- **Warnings (CLI).** One function `warning_records(engine_warnings, cli_warnings)` builds both the card list
  and the JSON list, so the two cannot disagree.

### Why `base_dir` and not a rewrite or a contextvar

Rewriting `snapshot_file` to an absolute path in the CLI breaks guarantee 2 (the hash includes it). A
contextvar (the `snapshot_content_guard` pattern in `plan/_generation.py`) would hide the dependency from the
signature, and the platform would need to know to clear it. A keyword argument is visible, defaults to
today's behavior, and matches `capacity.py`.

### Strict policies

`base_dir` applies only when given and only to a relative locator. The platform never passes it, so its
`RootedFileAccess` keeps reading root-relative locators (`fileaccess/_rooted.py:251` `lexical_ok`). The helper
never calls `abspath` or `resolve` when `base_dir` is `None` (AT-4).

### Messages

An unreadable snapshot keeps reason `plan.statistical_snapshot_unreadable`. The text names the declared value
and, when `base_dir` was given, the resolved path and the rule: `snapshot_file 'snap.json' could not be read at
'<base_dir>/snap.json' (a relative path is read from the config file's folder): <OS error>`. Display goes
through `fileaccess.shown` as today, so a strict policy still redacts.

## 4. Behavior

### Engine (E1)

- `base_dir` on the engine entries and both snapshot readers, as in section 3.
- `quarantine.output_path`: resolved in one place, `with_quarantine_path(ctx, path, base_dir)`
  (`_pipeline_context.py:433`), which already replaces the path in a copy of the config after the plan and its
  hash are built, and which every route reads through `ctx.config`. The rule: a caller's `quarantine_path`
  wins and is used as given, like a command-line path (relative means the working directory); without one, a
  relative `quarantine.output_path` from the YAML resolves against `base_dir` through `config_relative`; with
  neither `base_dir` nor a relative YAML path the context is returned as is. `quarantine.py:254` and the other
  readers of the path (`:312`, `:478`, `:545`) are not edited and read the resolved value.
- `column_names_file`: `capacity._resolved_config_copy` (`capacity.py:167-191`) resolves every
  `FILE_SOURCE_PATH_FIELDS` field, not only `path`, so `estimate_job` and `estimate_job_capacity` read the same
  names file the run does. The other engine readers (`profiling/scan_set.py:371`,
  `profiling/_scan_integrity.py:211`, `profile/_lazy_csv.py:120`, `profile/_readers.py:528`) take descriptors
  from their callers: the CLI passes resolved descriptors after C1, and the platform passes rooted locators.
- `_fidelity_gate.py`: `score_generated_fidelity` returns records instead of strings. `warn_on_low_fidelity`
  keeps the log line and also calls `on_warning(QualityWarning(code="generation_fidelity_below_threshold",
  provider="statistical", column=None, detail={"table", "snapshot_index", "overall_score", "threshold",
  "worst_columns"}))`. The code is the string the log line already starts with. Like the other
  `QualityWarning` codes it is not a registered reason; the `QualityWarning` docstring list gains it.
- `on_warning=None` on `run_generation`, `_run_legacy`, `_generate_tables_from_config` and the four public
  entries (section 3). Execution callers: `execution/_pipeline_generate_mask.py:128` (and its `pregenerated`
  input), `execution/_pipeline_mixed_route.py:74`, `execution/physical/drivers/_synthesis.py:50`. Each folds
  the collected warnings into the `ExecutionResult.warnings` it builds. Output bytes are untouched.
- Two public read-only accessors, exported from `decoy_engine`:
  - `generation_order(config) -> tuple[tuple[str, tuple[str, ...]], ...]`: each generate table with its
    referenced parents, parents first. It is the dependency walk and `_topo_sort` that
    `generation/synthesize.py:110-137` and `generation/_streamed.py:153-163` (`_table_order`) each run today,
    moved into one function both call. The two differ on a `reference_table` that is not a generate table
    (possible only on an unvalidated dict): `synthesize.py:126-130` raises a typed `ValueError` ("reference_table
    'x' is not a generate table"), while `_table_order` skips it, because `_topo_sort` tolerates missing nodes,
    and the streamed route fails later with a `KeyError`. The `ValueError` wins: `generation_order` raises it
    for every caller (AT-25).
  - `generate_option_catalog() -> Mapping[str, GenerateTypeOptions]`: per generate type, the required and
    optional option names plus `COMMON_OPTIONS`, read from `GENERATE_OPTIONS`
    (`config/_generate_options.py:203`). Names only; row 24s adds descriptions.

### CLI C1 (behavior)

- Engine pin bump.
- `base_dir=` at every engine entry: `C:run.py:868` (`config.parent`), `C:gate/_layers.py:182` and `:245`,
  `C:compile_explain.py:235`, `C:preflight.py:754`, `C:tokenize.py:343`, `C:api.py:521` (its existing
  `base_dir`: `config.parent`, or the working directory for an in-memory dict), `C:demo.py:234`. Plus the walk
  sentry.
- `read_csv_frame` (`C:_sources.py:43-70`) passes `resolve_csv_dialect` a descriptor copy whose
  `column_names_file` is resolved like `path`.
- Warnings in the summary. Card fact `Warnings: N` and, under the card, one line per warning. Known codes get a
  sentence ("table orders: fidelity 0.42 is below fidelity_warn_threshold 0.99; lowest: start_date 0.39");
  unknown codes print `code` and `detail`. `--json` adds `"warnings": [{"code", "table", "column", "message",
  "detail"}]`. Exit code stays 0 (OQ5). Every `ExecutionResult.warnings` entry is shown (D-WARN).
- Per-table lines under the card: table, rows, file (the records already built at `C:run.py:553`).
- `--fidelity` with no mask table in the config: exit 1 (`EXIT_USAGE`) before any engine call, exact text:
  "--fidelity scores mask tables and this pipeline has none. Score generated tables with: decoy report fidelity
  <source.csv> <synthetic.csv> --generate". `report fidelity` takes two CSV arguments and no config
  (`C:report.py:1554`, `:1598-1613`). On a mixed pipeline the mask report prints as today, followed by one line
  pointing generated tables at the same command form.
- Seed and key. `--master-key` affects generation only (`C:run.py:292-300`); masking still reads
  `global_settings.seed` through `plan/_seed.py`. When the config has at least one generate table, a key is in
  effect (`--master-key` or `DECOY_MASTER_KEY`) and the YAML sets `global_settings.seed`, the run adds CLI
  warning `seed_ignored_with_master_key`: "global_settings.seed is not used for generated tables when a master
  key is set; the key and --key-label decide them. Masked tables still use the seed." The `--master-key` help
  says the same.
- `run.py` stays flat: the three report renderers (`C:run.py:1352-1455`: `_render_execution_route`,
  `_render_fidelity`, `_render_post_validation`) move to `src/decoy/ui/run_summary.py` with the new warning and
  table rendering.

### CLI C2 (behavior)

- **Preview.** `run --rows N` (1 to 1000) on a generate-only pipeline. Every generate table's `row_count`
  becomes `max(1, round(row_count * N / largest))`, where `largest` is the largest declared `row_count`, so the
  largest table has N rows and parent-to-child ratios hold (D-SCALE). The scaled dict is a preview copy: nothing is
  written (no targets, evidence, vault, notifications). Human output prints each table's first N rows in
  generation order. `--json` prints `{"command": "run", "status": "ok", "preview": true, "tables": [{"name",
  "rows", "columns", "sample"}]}` plus `warnings`. Refused with exit 1 before work: a config with a mask table,
  `--chunked`, `--chunk-size`, `--evidence-out`, `--notify`, `--vault`, `--fidelity`, `--explain-plan`,
  `--post-validate`, `--post-validate-enforce`, `--native`, `--duckdb-source`. A per-parent bound the scaled
  counts cannot meet is the engine's normal refusal, with the CLI hint "raise --rows".
- **`init -o -`.** With `--preset` (the path that today creates `-`), the YAML goes to stdout and every other
  line to stderr. `-o -` with `--json` is refused (both want stdout). `-o -` with a CSV generate scaffold is
  refused, because that scaffold also writes a snapshot file that has to sit beside the config.
- **`init <csv> --preset generate`.** Fits a snapshot from the CSV with the fitting code `decoy fit` uses
  (moved out of `fit.py` into one function both call) and writes a config with one generate table:
  `row_count` equal to the CSV's rows, one `type: statistical` column per CSV column with
  `snapshot_file: <stem>.snapshot.json` (relative to the config, per guarantee 1), and target
  `<stem>.synthetic.csv`. Both files are written with `write_local_target`, as `fit` writes its snapshot
  (`C:fit.py:497`). Overwrite follows init's own rule for its config (`C:init.py:512`: an existing file needs
  `--yes`), checked for both files before either is written. Order: snapshot, config, `decoy validate`. If the
  config write or validation fails, init removes the files it created in this call and exits 1.

### CLI C3 (help and explain text, low risk)

- **Help panels.** Every top-level command in `C:src/decoy/__main__.py:242-260` gets a `rich_help_panel`:
  Start (`init`, `demo`, `info`, `doctor`, `upgrade`), Run (`run`, `subset`), Generate (`fit`), Mask (`scan`,
  `tokenize`, `detokenize`, `unmask`), Check and report (`validate`, `ci`, `report`), Reference (`explain`,
  `list`, `show`, `schema`). The app epilog adds the generation path: `decoy fit`, `decoy run`, `decoy report
  fidelity <source.csv> <synthetic.csv> --generate`. OQ1 confirms the layout.
- **`validate compile --explain`.** For generate tables: row count, each column's type, the referenced parents
  and a "Generation order" section from `generation_order`. The line at `C:compile_explain.py:434` prints only
  when there are neither mask FK dependencies nor generate references. `--json` gains a `generation` key.
- **`decoy explain generation`.** The workflow (`fit`, `run`, `report fidelity <source.csv> <synthetic.csv>
  --generate`), the path rule, seed versus key, warnings and `fidelity_warn_threshold`, `run --rows`, a pointer
  to `explain unique` (row 24m), and the type and option list rendered from `generate_option_catalog()`. `_Topic` (`C:explain.py:27`) gains an
  optional render function so the list is never typed by hand.
- **`explain transforms`.** Uses `strategy:`; the strategy list comes from the data `decoy list strategies`
  reads (`SCALAR_HANDLERS`, imported in `C:cockpit.py` `_strategies_list` under the engine-internal allowlist).
  C3 moves that read into one function in `cockpit.py` that both `_strategies_list` and the topic call, so the
  allowlist gains no entry. No count literal; generate types leave the mask list; "a future --mask flag" goes;
  a pointer to `explain generation`.
- **`explain pipeline` and `explain yaml`.** Add the path rule sentence, the generate column shape, the
  relationship options and a pointer to `explain generation`. STORM wording stays for row 56.
- **Docs.** README generation section, the generated CLI reference, CHANGELOG (the path rule is a behavior
  change for configs that relied on the working directory), roadmap row 24o status.

## 5. Enumerations (engine `84f4290d`, CLI `9f603536`)

### 5.1 Routes

Engine entries that reach a snapshot read (section 3 lists what each passes):

- `compile_plan` (`plan/_compile.py:115`, read at `:185`).
- `run_config_only_checks` (`plan/_compile.py:484`, read at `:520`).
- `run_pipeline` (`execution/_pipeline.py:116`, through `_pipeline_context.py:337`).
- `run_pipeline_isolated` (`execution/_isolated_run.py:145`, through the child's `run_pipeline`).
- `validate_plan` (`plan/validate/_consolidator.py:88`).
- `compile_pinned_plan_with_generation` (`generation/_pinned_compile.py:255`).
- `estimate_job_capacity` (`execution/capacity.py:351`) and `estimate_job` (`execution/_job_estimate.py:130`).
- Out of scope with reasons (section 3): `execution/_chunked_oracle.py:246`, `execution/native/_plan.py:163`.
- `plan/_serialize.py:442` rebuilds from pinned bytes and never reads: N/A.

CLI call sites of those entries (the sentry derives this list by walking; the list is for review):
`C:run.py:868`, `C:gate/_layers.py:182`, `C:gate/_layers.py:245`, `C:compile_explain.py:235`,
`C:preflight.py:754`, `C:tokenize.py:343`, `C:api.py:521`, `C:demo.py:234`.

Generation routes that run the fidelity gate, all through `run_generation` (`generation/_streamed.py:319`):

- Public entries in `generation/_plan_entry.py`: `generate_tables` (`:147`), `generate_tables_streamed`
  (`:216`), `generate_tables_from_generation_plan` (`:252`), `generate_tables_streamed_from_generation_plan`
  (`:284`).
- Execution callers: `execution/_pipeline_generate_mask.py:128` (generate and mixed full frame; its
  `pregenerated` input comes from the same entry on an earlier stage), `execution/_pipeline_mixed_route.py:74`
  (mixed route), `execution/physical/drivers/_synthesis.py:50` (physical plan synthesis stage).
- The mixed route's chunked lane runs only mask tables (`_pipeline_mixed_route.py:214`): N/A.
- Native: no generation kernel: N/A.

### 5.2 Exit paths

| Command | Case | Before | After |
|---|---|---|---|
| `run --fidelity` | no mask table | 0, "no mask-table fidelity report" | 1, before work |
| `run --rows` | mask table or a refused flag | option unknown | 1, before work |
| `init -o -` | with `--json`, or a CSV generate scaffold | n/a | 1 |
| `run`, `validate` | relative `snapshot_file` not beside the config | 0 from the right folder, 1 elsewhere | 1 from any folder, message shows the resolved path |

No exit integer is added or moved. No file, lease or temp resource is added, so no cleanup path is added;
the `--fidelity` and `--rows` refusals run before the first engine call.

### 5.3 FK shapes

- `reference` columns: `generation_order` reports each table's parents; `run --rows` scales parents and
  children by one ratio, so every child key still draws from its parent's generated keys (AT-14).
- Mask FKs: unchanged; `validate compile --explain` keeps its current FK section.
- Composite FKs in generation: not supported today (QA record section 8): N/A.

### 5.4 Fields

YAML path fields and the rule each follows:

| Field | Read or written by | Today | After |
|---|---|---|---|
| `targets[].path` | CLI `io/_targets.py:423-445` | config folder | unchanged (24t changes it later) |
| `sources[].path` | CLI `run.py:1559` `_load_sources_from_config`, `api.py:501` | config folder | unchanged |
| `snapshot_file` | engine `plan/_generation.py:130`, `_spec.py:174` | working directory | config folder |
| `sources[].column_names_file` | engine `profile/_csv_reader.py:95-111` via `resolve_csv_dialect`, called by CLI `_sources.py:65` and `capacity._resolved_config_copy` callers | working directory | config folder |
| `quarantine.output_path` | engine `quarantine.py:254`, path set by `_pipeline_context.with_quarantine_path` | working directory | config folder; a `quarantine_path` argument wins and is used as given |

`FILE_SOURCE_PATH_FIELDS` (`config/_sources.py:241-247`) marks `path` and `column_names_file` with
`x-decoy-path`; the build re-runs this walk on its base and adds any new field to the table.

New keys: no YAML key. Keyword `base_dir` on the section 3 engine entries and two readers; `on_warning` on
`run_generation` and the four generation entries; public names `generation_order` and `generate_option_catalog`; `QualityWarning` code
`generation_fidelity_below_threshold`; CLI warning code `seed_ignored_with_master_key`; CLI option `run --rows`;
`run --json` key `warnings`; `validate compile --explain --json` key `generation`.

### 5.5 Output surfaces

- `run` card and the lines under it (warnings, per-table rows and files).
- `run --json`: `warnings` added; the preview payload.
- `validate compile --explain`, human and `--json` (`generation` added).
- `decoy --help` panels and epilog.
- `explain generation`, `transforms`, `pipeline`, `yaml`.
- `init` stdout under `-o -`; the snapshot and config files `init <csv> --preset generate` writes.
- Evidence manifest `quality_summary` (fed by `ExecutionResult.warnings`, so a low-fidelity generate run now
  records the warning there too).
- Engine log: the existing fidelity log line stays.

## 6. Class table

| # | Class | Where it applies | Structure that closes it | Proof | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | warning lines and `--json` warnings; preview rows; the unreadable-snapshot message | warnings carry table and column names and scores only; preview prints only what a real run would write (statistical categorical columns can carry snapshot categories the config already admits with `allow_real_categories`) and refuses mask tables, so no source row from a mask table prints; the message goes through `fileaccess.shown` | AT-9 (no cell values in warnings), AT-15 (mask table refused), AT-6 | M1: let `--rows` run a mixed config (AT-15 red) |
| 2 | Cleanup missing on an exit path | `--fidelity` and `--rows` refusals; `init` scaffold failure | refusals run before any engine call; the scaffold writes through `write_local_target`, validates, and removes the files it created when the config write or validation fails | AT-11 (empty target folder), AT-15, AT-18, AT-23 (no files left) | M2: refuse `--fidelity` after the engine call (AT-11 red) |
| 3 | State machine and concurrency | the warning sink | a list local to one `run_pipeline` call; no module state | AT-8 run twice in one process gives one warning each | M3: hoist the sink list to module scope (AT-8 second run red) |
| 4 | Route parity and silent fallback | the four generation entries and three execution callers; the eight CLI entry calls; `resolve_csv_dialect` | one sink keyword on `run_generation`; one walk sentry over CLI calls | AT-8 route matrix; AT-5 sentry | M4: drop the sink on the mixed route (AT-8 red) |
| 5 | Referential integrity | `run --rows` scaling; `generation_order` | one ratio for every table; the order walk is the one `generate_tables` uses | AT-14 (child keys exist in the parent sample), AT-20 | M5: scale only the largest table (AT-14 red) |
| 6 | Untrusted producer | YAML path strings; `--rows N`; the CSV given to `init` | lexical join only when relative; absolute kept; the file policy still authorizes the read; `N` bounded 1 to 1000; the CSV goes through `fit`'s reader | AT-3, AT-4, AT-15 | M6: accept `--rows 0` (AT-15 bound cell red) |
| 7 | Check then use (TOCTOU) | the snapshot read | the one read pass pins bytes and the hash covers the bytes; resolving the locator adds no second open | AT-1, AT-2 | M7: stat the resolved path before reading it in a second open (AT-1 spy on `read_all` count red) |
| 8 | Weak tests | AT-1 from the config folder alone would pass on base | AT-1 runs from a second folder; AT-8 runs the same config at threshold 0.0 and asserts no warning | AT-1, AT-8 non-vacuity cells | M8: always emit the warning (AT-8 threshold 0.0 cell red) |
| 9 | Refactor drift | `generation_order` extraction; renderers moved out of `run.py` | `generate_tables` calls the accessor; moved renderers keep their text | AT-8 output bytes equal base; existing renderer tests unchanged | M9: reverse the order in the accessor (AT-8 bytes red) |
| 10 | Frozen contract drift | `pipeline_config_hash`, output bytes, exit codes, `run --json`, public API | the config dict is never rewritten; additive keys and keywords; exit integers unchanged | AT-2, AT-8, AT-11, AT-9 | M10: rewrite `snapshot_file` to an absolute path in the CLI (AT-2 red) |

## 7. Acceptance tests (red first)

Each test is red on the named wrong build. "Base" is engine `84f4290d` with CLI `9f603536`.

| # | Test | Red on |
|---|---|---|
| AT-1 | Config in folder A with `snapshot_file: snap.json` beside it. `decoy run` from folder B succeeds and writes the same bytes to the same absolute targets as a run from A; a spy shows one `read_all` per snapshot per compile (the run's validate gate compiles once, `C:run.py:669`, and the run compiles once) | base (B cannot read `snap.json`) |
| AT-2 | `pipeline_config_hash` and the evidence config hash are equal for runs from A and from B, and equal to the base build's run from A | a build that rewrites `snapshot_file` to an absolute path in the CLI |
| AT-3 | An absolute `snapshot_file` reads the same file from A and from B | a mutant that joins `base_dir` by string concatenation |
| AT-4 | `compile_plan` with no `base_dir` under a `RootedFileAccess` reads a root-relative snapshot exactly as on base | a mutant that calls `abspath` on the locator when `base_dir` is `None` |
| AT-5 | Walk sentry (`C:tests/sentry/io_policy/test_engine_base_dir.py`): every call of the section 3 engine entries in `src/decoy/` passes `base_dir=` as a keyword. Behavioral half: `validate config`, `validate compile --explain` and `ci check` from B pass on AT-1's config | a seeded copy without `base_dir` at `gate/_layers.py:245`; a seeded positional `estimate_job_capacity(d, p)` |
| AT-6 | `snapshot_file` missing from A but present in B, run from B: exit 1, the message names `<A>/snap.json` and the rule | a build whose message shows only the declared string |
| AT-7 | Run from B: a relative `column_names_file` is read from A by `decoy run`, `decoy validate preflight` (the capacity estimate) and `decoy report fidelity --config`; a relative `quarantine.output_path` is written in A | base |
| AT-8 | Statistical generate config, `fidelity_warn_threshold: 0.99`, one weak column: `ExecutionResult.warnings` has one `generation_fidelity_below_threshold` with table, scores, threshold and worst columns. Parametrized over the four `_plan_entry` entries and every run route that generates (auto, sequential, full frame, mixed with a mask table). Output bytes equal the base build's. Non-vacuity: the same config at threshold 0.0 raises none; two runs in one process raise one each | base (no warning); a mutant that drops the sink on the mixed route |
| AT-9 | Same config through `decoy run`: the card shows `Warnings: 1` and the sentence; `--json` `warnings` has that code; exit 0. A clean run has `"warnings": []`. No warning line or `detail` value holds a generated or source cell value | base CLI (no key) |
| AT-10 | Human `run` output lists each table with rows and file | base CLI |
| AT-11 | Generate-only config with `--fidelity`: exit 1, the target folder is empty, stderr equals the exact text in section 4 C1 | base (exit 0); a build that names a `report fidelity <config>` form |
| AT-12 | Mixed config with `--fidelity`: the mask report prints, then the generated-tables line, exit 0 | a build that refuses any config with a generate table |
| AT-13 | Generate-only, seed plus key: warning `seed_ignored_with_master_key`, exit 0, output equal to a key-only run. Mixed, seed plus key: the same warning text ("for generated tables"), and the masked table, which uses `strategy: shuffle` with no mask secret (its permutation is seeded from `derive(job_seed, ...)`, `execution/_strategies/_shuffle.py:3-5`), changes bytes when the seed changes. Mask-only, seed plus key: no warning. Seed without key: no warning | base (silent); a mutant that warns whenever a seed is set; a mutant that warns on mask-only configs; a build that drops the seed from mask runs when a key is set |
| AT-14 | `run --rows 5` on a parent of 1,000 and a child of 5,000: `--json` reports 1 and 5 rows, child FK values exist in the parent sample, no file exists under the target folder, no evidence file | a build that ignores `--rows` and writes targets |
| AT-15 | `run --rows 5` with a mask table, with `--chunked`, with `--evidence-out`, with `--explain-plan`, with `--native`, with `--duckdb-source`, and `--rows 0` or `--rows 1001`: exit 1 before work | a build that silently drops the conflicting flag |
| AT-16 | `init --preset generate -o -`: stdout is YAML that `decoy validate config` accepts; no file named `-` exists; `-o - --json` exits 1 | base (writes `-`) |
| AT-17 | `init people.csv --preset generate -o gen/pipeline.yaml`: writes `gen/people.snapshot.json` and a config whose `snapshot_file` is `people.snapshot.json`; `decoy run gen/pipeline.yaml` from the parent folder succeeds with the CSV's columns and row count | base (mask scaffold); a build that writes `snapshot_file: gen/people.snapshot.json` |
| AT-18 | `init people.csv --preset generate -o -`: exit 1, no snapshot written | a build that writes the snapshot to the working directory |
| AT-19 | Help sentry (`C:tests/sentry/cli_text/test_help_panels.py`) walks the Typer app's registered commands and groups: each has a panel; `decoy --help` shows a Generate panel with `fit` | a seeded command without a panel |
| AT-20 | QA config with 3 references: `validate compile --explain` lists the generation order parents first and the 3 references, not "no FK dependencies detected"; `--json` has `generation` | base |
| AT-21 | `explain generation` names every type in `generate_option_catalog()` and each of its options; the test reads the catalog, not a list | a topic with one type removed from rendering |
| AT-22 | `explain transforms` lists exactly the `list strategies` set, contains `strategy:`, and contains neither `masking_rule` nor "future --mask"; the engine-internal allowlist baseline is unchanged | base |
| AT-23 | `init people.csv --preset generate` with validation forced to fail (a test seam on the validate call): exit 1, neither `people.snapshot.json` nor the config exists afterwards. An existing config without `--yes`: refused before either file is written | a build that validates after writing and keeps both files |
| AT-24 | `run_pipeline_isolated(..., isolate=True, base_dir=str(A))` from B reads `<A>/snap.json` in the child and matches the in-process run; `base_dir=Path(A)` raises the existing JSON-serializable `ValueError` | base (no `base_dir`); a build that resolves in the parent and drops the value from the payload |
| AT-25 | An unvalidated dict where generate table `orders` references mask table `customers`: `generate_tables`, `generate_tables_streamed` and both `_from_generation_plan` entries raise `ValueError` "table 'orders' column 'customer_id': reference_table 'customers' is not a generate table", and `generation_order` raises the same | a build where `generation_order` keeps `_table_order`'s skip (the streamed entry raises `KeyError`) |

Test placement: engine AT-2 to AT-4 and AT-8 in `tests/unit/plan/` and `tests/unit/execution/` beside the existing
snapshot and gate tests; CLI behavior tests in `tests/e2e/first_user/test_items_generate.py` (a new file in the
named folder); the sentries as named above.

Mutations to run on each final head: E1, drop `base_dir` in `read_and_pin_snapshots`, and drop the sink in one
`generate_tables` caller. C1, remove `base_dir` from one call site, and warn on a seed without a key. C2, write
targets under `--rows`. C3, drop one type from the topic render. Each must turn a test red.

## 8. Owner questions

| # | Question | Recommended answer |
|---|---|---|
| OQ1 | Row 24e (`ROADMAP.md:249`) also lists explain topics for fit, generate and fidelity and a Generate help panel. Who owns them? | 24o. Strike them from 24e. The panel layout in section 4 C3 stands unless you want different groups |
| OQ2 | The generation topic overlaps 24s GEN-CATALOG (a registry-built list of types, options and providers). How do they split? | 24o adds `generate_option_catalog()` (names only) and renders the topic from it. 24s adds descriptions and providers to the same accessor and owns `decoy list column-types` |
| OQ3 | `--fidelity` on generate: refuse, or score generated tables in the run? | Refuse on generate-only pipelines and point at `report fidelity <source.csv> <synthetic.csv> --generate`, which exists. Scoring in the run needs a new public result field for a check one command already does |
| OQ4 | Seed with a master key: state it, or fold the seed into keyed derivation? | State it (a warning and help text, only when generate tables exist, saying masked tables still use the seed). Folding changes every keyed output byte |
| OQ5 | Should a run with warnings exit non-zero? | No: exit 0 as today; `fidelity_warn_threshold` is documented as warn-only. A `run --fail-on-warning` (exit 2, as `validate` has) is a follow-up |

### Engineering calls (decided in the plan, not owner questions)

| # | Call | Reason |
|---|---|---|
| D-PATHS | `column_names_file` and `quarantine.output_path` join the path rule in E1 and C1, through the same helper; quarantine resolves only in `with_quarantine_path`, and a caller's `quarantine_path` is used as given | Otherwise "every YAML path" is false. Tell the CONFIG-EXPLICIT CLI half, which also touches `column_names_file` |
| D-SCALE | Preview scales every table by one ratio, with the largest table at N | Flat N breaks per-parent minimums on small parents |
| D-CAP | Preview cap is 1,000 rows | Above that, run for real; the preview prints rows to the terminal |
| D-SCAFFOLD | The CSV generate scaffold lives in `cli/gen/_scaffold.py` (C2); row 24t's `write_pipeline_scaffold` calls it | 24o lands first and 24t depends on its path rule, so the call points one way |
| D-WARN | The summary shows every `ExecutionResult.warnings` entry | They are the engine's non-fatal observations and already go to evidence; hiding some is how M3 happened |

## 9. Reuse inventory

- `resolve_path` (`C:_sources.py:27`) stays the CLI's one YAML path resolver for targets and sources.
- `capacity.py:156-191` is the pattern for the engine helper.
- `QualityWarning` and `ExecutionResult.warnings` carry the new warning; no new channel.
- `_topo_sort` (`synthesize.py:202`) becomes the core of `generation_order`; `synthesize.py` and
  `_streamed._table_order` both call the accessor, so one walk exists.
- `GENERATE_OPTIONS` and `COMMON_OPTIONS` back the topic; the strategy data function in `cockpit.py` backs
  both `_strategies_list` and `explain transforms`.
- `decoy fit`'s fitting code and `write_local_target` back `init <csv> --preset generate`. Row 24l's
  `_csv_frame` (the Arrow-to-CSV writer in `io/_targets.py`) is not reused: init writes JSON and YAML.
- `capacity._resolved_config_copy` gains the other `FILE_SOURCE_PATH_FIELDS` fields instead of a second
  resolver.
- `render_card` and `make_table` (`C:ui/`) for all new output.

## 10. Placement (checked against CLI `CODEMAP.md` "Where things go", `:42-63`)

| Change | File | Sits beside | CODEMAP |
|---|---|---|---|
| `base_dir`, `config_relative`, `pin_and_check_snapshots` | engine `plan/_generation.py`, `generation/statistical/_spec.py`, `plan/_compile.py`, `execution/_pipeline.py`, `_pipeline_context.py` (`build_run_context`, `with_quarantine_path`) | the existing read pass | no new engine module |
| `base_dir` on the other compile callers | engine `plan/validate/_consolidator.py`, `generation/_pinned_compile.py`, `execution/capacity.py` (`_resolved_config_copy` gains the names file), `execution/_job_estimate.py` | the existing `compile_plan` calls | none |
| Fidelity sink | engine `generation/_fidelity_gate.py`, `synthesize.py`, `_streamed.py` (`run_generation`, `_run_legacy`), `_plan_entry.py`, the three execution callers | `warn_on_low_fidelity` | none (`generation/` has 26 entries, so no new file there) |
| `generation_order` | engine `generation/synthesize.py`, called from `_streamed.py` | `_topo_sort` | none |
| `generate_option_catalog` | engine `config/_generate_options.py` | `GENERATE_OPTIONS` | none |
| CLI `base_dir` call sites | the eight files in 5.1 | the existing calls | none |
| `column_names_file` resolution | `src/decoy/_sources.py` (`read_csv_frame`) | `resolve_path` | none |
| Strategy data for `list strategies` and `explain transforms` | `src/decoy/cli/cockpit.py` | `_strategies_list` | none; the engine-internal allowlist gains no entry |
| Walk sentry | `tests/sentry/io_policy/test_engine_base_dir.py` | `_calls.py` (the shared call scanner) | row "I/O policy sentries" |
| Summary rendering and moved renderers | `src/decoy/ui/run_summary.py` | `card.py`, `table.py` | `ui/` is an existing package with 7 modules; no row needed |
| Preview, CSV generate scaffold, generation topic | new sub-package `src/decoy/cli/gen/` (`_preview.py`, `_scaffold.py`, `_topic.py`) | | new row "Generation CLI helpers" (a map change, made in C2), because `cli/` is frozen and over the fan-out limit |
| Help panels | `src/decoy/__main__.py` | the registrations at `:242-260` | none |
| Help sentry | `tests/sentry/cli_text/test_help_panels.py` | `test_no_traceback_escape.py` | row "User-facing CLI text" |
| Behavior tests | `tests/e2e/first_user/test_items_generate.py` | `test_items_text.py` | row "First-user QA item tests" |

Sizes at base: `run.py` 1,748 (over the 900 cap; must not grow), `init.py` 691 (C2 adds only the call into
`cli/gen/_scaffold.py` and the `-o -` branch, about 25 lines), `explain.py` 677 (C3 adds the render hook and
text, net about 30), `compile_explain.py` 498, `__main__.py` 264. Engine: `synthesize.py` 500,
`_fidelity_gate.py` 198, `_generate_options.py` 382, `plan/_generation.py` 286 (gains `config_relative` and
`pin_and_check_snapshots`, about 50), `_spec.py` 581 (the `base_dir` keyword keeps it under 600),
`_streamed.py` 557, `_plan_entry.py` 306, `capacity.py` 551, `_consolidator.py` 113, `_pinned_compile.py` 326,
`_pipeline_context.py` 545, `_job_estimate.py` 208. Three pinned files:

- `plan/_compile.py` 680, census entry at exact LOC (`tests/sentry/test_module_size.py:92`). The
  `pin_and_check_snapshots` fold removes the duplicated read and check block (`:184-191`, `:519-526`), which
  offsets the two `base_dir` keywords, so the file does not grow; the census entry is updated to the new count
  if it shrinks.
- `execution/_isolated_run.py` 652 (census `:75`): not edited; `base_dir` rides in `**run_pipeline_kwargs`.
- `execution/_pipeline.py` 351, pinned at exactly that count by `test_b6a_bookkeeping.py:29`,
  `test_b6b_bookkeeping.py:34` and `test_auto_chunk_bookkeeping.py:31` (all lines, `wc -l` style,
  `test_module_size.py:144`). The pins stay at 351. `run_pipeline` lists one parameter per line with a trailing
  comma, so ruff format keeps each keyword on its own line. The edit, line by line:
  - plus 1: `base_dir: str | None = None,` after `quarantine_path` (`:159`);
  - plus 1: `base_dir=base_dir,` in the `build_run_context` call (after `:333`);
  - plus 1: a docstring line after `:256`: "`base_dir` is the folder relative YAML paths resolve from
    (snapshots, `quarantine.output_path`); `None` keeps the working directory.";
  - 0: `:335` becomes `ctx = _ctx.with_quarantine_path(ctx, quarantine_path, base_dir)` in place;
  - minus 3: the `_provider_snapshot` docstring paragraph (`:268-273`, six lines) is rewritten in three lines
    under 100 characters, keeping its content (private harness hook, forwarded to `generate_tables` so the
    shadow-parity harness pins the call to its own registry snapshot, `None` uses the live registry).

  Net 0. `str | None` needs no new import (`Path` is not imported in `_pipeline.py`). The rest of the work
  (the forward to `compile_plan`, the quarantine rule) sits in `_pipeline_context.py`, 545 lines.

## 11. Frozen contracts touched

- Engine public API: additive keywords and two new names (`api-and-compatibility.md`: additive, documented in
  the engine CHANGELOG and API reference).
- `pipeline_config_hash` and output bytes: unchanged, pinned by AT-2 and AT-8.
- Evidence manifest `quality_summary`: gains the new code for low-fidelity generate runs (same shape).
- CLI exit codes: integers unchanged; two cases move from 0 to 1 (5.2).
- `run --json`: additive `warnings`; the preview payload is new and marked `"preview": true`.

## 12. Slices

| Slice | Repo | Branch | Size (hand-written) | Content | Gate |
|---|---|---|---|---|---|
| E1 | engine | `feat/gen-cli-e1` | M, about 220 lines plus tests | `base_dir` and the helper on the section 3 entries (with the D-PATHS fields and `_resolved_config_copy`), `pin_and_check_snapshots`, the fidelity sink on `run_generation` and the four entries, `generation_order` for both walks, `generate_option_catalog`, docstrings; AT-2 to AT-4, AT-7, AT-8 engine halves, AT-24 | full suite: a serialized contract (manifest `quality_summary`) and the public API |
| R | engine | release | n/a | engine release with E1 (owner step, not a build slice) | release checks |
| C1 | CLI | `feat/gen-cli-c1` | M, about 210 lines plus 104 moved | engine pin bump, `base_dir` at every entry and the walk sentry, the `read_csv_frame` names-file descriptor, warnings and per-table lines, `--fidelity` refusal, seed warning, renderers moved to `ui/run_summary.py`; AT-1, AT-5, AT-6, AT-9 to AT-13 | full suite (version pin) |
| C2 | CLI | `feat/gen-cli-c2` | M, about 230 lines | `cli/gen/` with `_preview.py` and `_scaffold.py`, the CODEMAP row, `run --rows`, `init -o -`, `init <csv> --preset generate` with cleanup; AT-14 to AT-18, AT-23 | T1 plus the T2 set on the exact head |
| C3 | CLI | `feat/gen-cli-c3` | S, about 180 lines plus docs | help panels and sentry, `validate compile --explain` for generate, `explain generation` (`cli/gen/_topic.py`), `transforms` (with the `cockpit.py` strategy data function), `pipeline`, `yaml`, README, CLI reference, CHANGELOG, roadmap status; AT-19 to AT-22 | T1 |

Order: E1, R, C1, then C2 and C3 in either order. Engine collisions: row 24l E2 edits
`generation/_fidelity_gate.py:140-143` (frame typing), so E1 builds after 24l E2 merges. Row 24m E1 and E2 edit
the `synthesize.py` table loop (the `_unique.py` branches) that `generation_order` replaces; whichever of 24m
and 24o E1 lands second rebases over the other, with the loop's order and the `_unique` branches both kept.
C2 builds after row 24l C1 merges because both touch `fit.py` (C2 moves the fitting code into one function
that `fit` and init call). C3 coordinates with row 24m C1, which adds `explain unique` to the same `_TOPICS`
table. Row 24t builds after C1.

Commit subjects follow `workspace-and-vcs.md`: Conventional Commits, 72 characters at most, final line `cam`.

## 13. Pre-mortem

| Risk | Guard |
|---|---|
| A user's config relied on the working directory and breaks | AT-6 message names the resolved path and the rule; CHANGELOG entry; pre-GA hard change |
| A new CLI command calls an engine entry without `base_dir` | AT-5 walk sentry in the merge-gate suite |
| A future generate route skips the warning sink | the sink sits on `run_generation`, which every route reaches; AT-8 is parametrized over the entries and routes |
| Warnings flood the card on large runs | one line per warning; revisit with a cap only if a real run shows it |
| `run.py` grows past where it is | the renderer move nets about minus 60 lines in C1; the build report states the count |
| C2 and row 24t both create an init scaffold | D-SCAFFOLD names one owner and one call direction |
| `init <csv>` leaves half a scaffold on failure | write order plus cleanup, AT-23 |
| A pinned engine module grows | the `_compile.py` fold, the `_pipeline.py` line budget and no `_isolated_run.py` edit (section 10) |
| The topic and row 24s build two lists | OQ2: one accessor, extended by 24s |

## 14. Follow-ups (recorded, not in V1)

- `run --fail-on-warning` (exit 2 on warnings, as `validate` and `preflight` already do).
- `report fidelity` detecting generate mode without `--generate`.
- Preview for mask and mixed pipelines (a source row limit).
- `execution/native/_plan.py:163` compiles before it rejects generate tables at `:251`, so a generate config
  forced onto the native route fails on a relative snapshot read instead of the native refusal. Move the
  rejection before the compile.
- Row 24s: per-option descriptions, providers and `decoy list column-types` on top of
  `generate_option_catalog()`.
