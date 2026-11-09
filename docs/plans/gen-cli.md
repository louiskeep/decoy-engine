Status: plan

# GEN-CLI: one path rule and a generation-aware CLI (roadmap row 24o)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-09. Scope: engine plus CLI, V1 (owner decision
2026-11-08). Risk: R2 (compatibility: a path rule change and additive public APIs). Revision 3.

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Established solutions, proportion rule), `feature-dev.md`, `testing.md` (test
depth, escalation triggers), `api-and-compatibility.md`, `cross-cutting-concerns.md` (the path rule has more
than one call site), `architecture.md` (module size, fan-out), `reuse-first.md`, `scope-discipline.md`,
`code-review.md`, `verification.md`, `config-secrets-and-supply-chain.md`, `security.md`, `documentation.md`,
`workspace-and-vcs.md`, `profiles/local-operations.md`, `catalogs/recurring-defects.md`; the plan contract
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
- A failed two-file generate scaffold leaves both destination paths byte-for-byte as they were before the
  command (AT-23).
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
| F12 | `init <csv>` fails during validation or publication, or overlaps another publisher, and leaves a mixed pair, a new file or a lost overwritten file | AT-23 |
| F13 | An isolated run cannot carry `base_dir` to its child process | AT-24 |
| F14 | The merged order walk changes the error for a reference to a non-generate table | AT-25 |
| F15 | A new warning code bypasses the reason registry, so identity and safe-summary consumers drift | AT-26 |
| F16 | Physical-plan capture is the one compile route that still reads a relative snapshot from the working directory | AT-27 |

## 2. Established solutions

Search run: a web search for how Docker Compose resolves relative paths, plus the codebase precedent.

- **Config-relative paths.** Docker Compose resolves relative paths in a Compose file (build context, env
  files, bind mounts) from the project directory, which defaults to the folder of the first Compose file; when
  the file comes from stdin, from the working directory (docs.docker.com, "How Compose works" and the Build
  Specification). dbt and Cargo follow the same rule for their project files. The engine's current copy is
  `execution/capacity.py:156-191` (`_resolve_source_path(raw_path, base_dir)`, "the pipeline YAML's
  directory"). E1 promotes that rule to the public `fileaccess.config_relative` choke point and removes the
  private copy. The CLI's `resolve_path` delegates to it after the engine pin bump. The rule is a
  caller-supplied `base_dir`, joined lexically, with absolute paths unchanged.
- **Structured warnings over log lines.** The engine already carries `QualityWarning(code, provider, column,
  detail)` (`generation/pool/_events.py:15`) in `ExecutionResult.warnings`. The fidelity gate joins that
  channel instead of a new one.
- **Grouped help.** Typer's `rich_help_panel` (the git and kubectl command-group pattern).
- **Preview.** `dbt show --limit`: same pipeline, few rows, nothing written.
- **Two-file scaffold publication.** The CLI's `_atomic.py` already stages and replaces one local target, and
  `run.py:1592-1646` backs up and restores an existing vault around a replace. C2 adds the same rollback rule
  once to `_atomic.py` for a batch of staged local targets, then uses it for the snapshot and config together.
  The non-blocking `flock` discipline follows `distribution/lock.py:1-15`, but locks the pair's common parent
  directory descriptor so it adds no loose lock file.

No custom path or warning channel remains. The only new file mechanism is the central multi-target extension
of the existing atomic writer required by the scaffold's two destinations.

## 3. Design notes

New structure: one CLI sub-package (`cli/gen/`, section 10), one small engine file-access module and two engine
accessors. Principles: one place per fact (the path rule, the order walk and the option list each live once in
the engine and the CLI renders or delegates to them); no flag arguments that switch behavior
(`base_dir=None` means "no config file", not a mode).

### Choke points

- **Snapshot reads (engine), by construction.** `read_and_pin_snapshots` (`plan/_generation.py:102`) is the
  one read pass for a compile; `_load_snapshot` (`generation/statistical/_spec.py:174`) is the sole open site for
  a direct caller. Both gain `base_dir: str | Path | None = None` and resolve a relative locator through the one
  public helper `fileaccess.config_relative(locator, base_dir)`, implemented in
  `fileaccess/_config_path.py`. It applies `os.path.abspath` to the joined path once (so a relative `base_dir`
  means the working directory at the call), and it returns an absolute locator unchanged. The pinned map stays
  keyed by the declared string, so plan bytes and the hash do not see the resolved path. The read, DP verify and
  statistical check block appears twice in `plan/_compile.py` (`:184-191` and `:519-526`); E1 replaces both with
  one call to a new `pin_and_check_snapshots(config, *, base_dir)` in `plan/_generation.py`, which also offsets
  the new keyword lines in `_compile.py` (section 10). `capacity._resolved_config_copy` uses the same public
  helper. The CLI's `resolve_path` becomes a typed wrapper: it keeps today's absolute-path pass-through and
  today's `Path.resolve()` canonicalization for relative CLI paths, but delegates the choice of base and join
  to the engine helper. Every consumer imports through `decoy_engine.fileaccess`; none imports the private
  `_config_path` module. The change removes the existing private engine copy instead of adding a third resolver.
  The direct/legacy fallback at
  `plan/_generation.py:225` is the only call of `_load_snapshot`; `resolve_pinned_snapshot` and
  `check_statistical_columns` gain the optional keyword and pass it through when no pinned read exists.
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
    the same value to `compile_plan`, so one call never mixes two rules;
  - `capture_physical_plan_inputs` (`execution/physical/_snapshot.py:49`, compile at `:152`), exported from
    `execution.physical`. It gains the optional keyword and passes it to `compile_plan`. This was the ninth
    compile route missed by the first two plan rounds.
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
  `base_dir=config_path.parent` in C1, so a positional argument can never hide a missing one. It runs in the
  merge-gate suite. Seeded direct imports, module aliases, `from` aliases and `import decoy_engine.plan`
  attribute calls must all be seen by the shared normalized scanner. The mutation that must turn it red is to
  drop `base_dir` from `C:gate/_layers.py:245`. The build report shows the walk's count rising on each seeded
  bypass and falling on a removed site.
- **`column_names_file` (CLI), by construction.** The field is read inside `resolve_csv_dialect`. The CLI's one
  call is `C:_sources.py:65` in `read_csv_frame`, which every CLI CSV read goes through (`C:_sources.py:88-161`,
  `C:init.py:192`). That call gets a descriptor copy whose `column_names_file` went through `resolve_path`, as
  `path` already does, through the CLI wrapper over `fileaccess.config_relative`. Sources are excluded from
  `_hash_config`, so the copy is hash-safe; the engine does the same in `capacity._resolved_config_copy`.
  `resolve_csv_dialect` joins the `decoy_engine` watch list of the
  existing G-ONE-CSV-READ sentry (`C:tests/sentry/test_csv_read_choke_point.py:88`, beside `read_csv_source`
  and `read_fixed_width`), which already confines those calls to `cli/_sources.py`; no second rule in the walk
  sentry. Its only call today is `C:_sources.py:65`, so the allowlist and baseline do not change.
- **Warnings (engine).** `run_generation(..., on_warning=None)` (`generation/_streamed.py:319`) is the choke
  point. Every generation path reaches the gate through it: `_run_legacy` (`:507`) calls
  `_generate_tables_from_config` (`:538`), which calls `warn_on_low_fidelity` (`synthesize.py:188`). The four
  public entries in `generation/_plan_entry.py` (`generate_tables` `:147`, `generate_tables_streamed` `:216`,
  `generate_tables_from_generation_plan` `:252`, `generate_tables_streamed_from_generation_plan` `:284`) take
  the keyword and pass it to `run_generation`. The full-frame caller at
  `execution/_pipeline_generate_mask.py:128` collects the callback records into the returned
  `ExecutionResult.warnings`. The mixed caller at `execution/_pipeline_mixed_route.py:74` carries both generated
  tables and generation warnings through its admitted result and its runtime-decline handoff. The handoff adds
  `pregenerated_warnings` beside `pregenerated` in `_pipeline_full_frame.py:98-127` and
  `_pipeline_generate_mask.py:111`, so the fallback cannot lose a warning when it correctly avoids a second
  generation pass. `execution/physical/drivers/_synthesis.py:50` is not a warning route: its admission permits
  only sequence, categorical and faker columns (`execution/physical/_shadow_generation.py:91`), while the
  fidelity gate runs only for statistical columns.
- **Warning identities.** Both new codes, `warning.generation_fidelity_below_threshold` and
  `warning.seed_ignored_with_master_key`, are entries in the engine reason registry. The registry supplies their
  identity and fixed safe summary; `_fidelity_gate.py` and the CLI add only typed detail. The reason sentries
  and manifest fixture are the guard, and deleting either catalog entry is the mutation that turns AT-26 red.
- **Warnings (CLI).** One function `warning_records(engine_warnings, cli_warnings)` builds both the card list
  and the JSON list, so the two cannot disagree.
- **Generate scaffold publication (CLI).** `_atomic.publish_local_targets` is the one multi-target extension of
  the existing local writer. It requires one common parent directory and holds a non-blocking exclusive lock on
  that directory descriptor across staging, backup, replace and cleanup. It stages and fsyncs every candidate
  before the first replace, keeps a same-folder hard-link backup for every existing destination, publishes in
  the supplied order, and restores every old destination or removes every new one if any replace fails. Lock or
  backup failure refuses before the first replace. A create-only target is rechecked while the lock is held, so
  a file created after the CLI's first existence check is not overwritten. `_scaffold.py` supplies snapshot
  then config, and no cleanup is hand-written in `init.py`. A sentry permits a multi-file scaffold publish only
  through this function. The mutations that must turn it red are to raise on the second replace after the first
  succeeds and to remove the parent lock while two publishers overlap.

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

- Public `fileaccess.config_relative`, with the old private copy in `capacity.py` removed; `base_dir` on the
  engine entries and both snapshot readers, as in section 3.
- `quarantine.output_path`: resolved in one place, `with_quarantine_path(ctx, path, base_dir)`
  (`_pipeline_context.py:433`), which already replaces the path in a copy of the config after the plan and its
  hash are built, and which every route reads through `ctx.config`. The rule: a caller's `quarantine_path`
  wins and is used as given, like a command-line path (relative means the working directory); without one, a
  relative `quarantine.output_path` from the YAML resolves against `base_dir` through
  `fileaccess.config_relative`; with
  neither `base_dir` nor a relative YAML path the context is returned as is. `quarantine.py:254` and the other
  readers of the path (`:312`, `:478`, `:545`) are not edited and read the resolved value.
- `column_names_file`: `capacity._resolved_config_copy` (`capacity.py:167-195`) resolves every
  `FILE_SOURCE_PATH_FIELDS` field, not only `path`, so `estimate_job` and `estimate_job_capacity` read the same
  names file the run does. The other engine readers (`profiling/scan_set.py:371`,
  `profiling/_scan_integrity.py:211`, `profile/_lazy_csv.py:120`, `profile/_readers.py:528`) take descriptors
  from their callers: the CLI passes resolved descriptors after C1, and the platform passes rooted locators.
- `_fidelity_gate.py`: `score_generated_fidelity` returns records instead of strings. `warn_on_low_fidelity`
  keeps the log line and also calls
  `on_warning(QualityWarning(code=reasons.warning.GENERATION_FIDELITY_BELOW_THRESHOLD,
  provider="statistical", column=None, detail={"table", "snapshot_index", "overall_score", "threshold",
  "worst_columns"}))`. The code uses the new registered warning reason, and the `QualityWarning` docstring list
  gains it.
- `on_warning=None` on `run_generation`, `_run_legacy`, `_generate_tables_from_config` and the four public
  entries (section 3). Execution callers: `execution/_pipeline_generate_mask.py:128` and
  `execution/_pipeline_mixed_route.py:74`, including the mixed runtime-decline handoff described in section 3.
  Each folds the collected warnings into the `ExecutionResult.warnings` it builds. Output bytes are untouched.
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
    (`config/_generate_options.py:203`). E1 adds the frozen public `GenerateTypeOptions` result record beside
    the existing private `TypeOptions` (`:64`); it exposes names only, never the registry's validator callables.
    Row 24s adds descriptions to the same public record.

### CLI C1 (behavior)

- Engine pin bump.
- `base_dir=` at every engine entry: `C:run.py:868` (`config.parent`), `C:gate/_layers.py:182` and `:245`,
  `C:compile_explain.py:235`, `C:preflight.py:456` and `:754`, `C:tokenize.py:343`, `C:api.py:521` (its existing
  `base_dir`: `config.parent`, or the working directory for an in-memory dict), `C:demo.py:234`. Plus the walk
  sentry. `C:_sources.py:27` delegates its existing `resolve_path` API to
  `decoy_engine.fileaccess.config_relative`.
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
  warning `warning.seed_ignored_with_master_key`: "global_settings.seed is not used for generated tables when a master
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
  `<stem>.synthetic.csv`. The snapshot uses the same serializer that `fit` passes to `write_local_target`
  (`C:fit.py:497`). Overwrite follows init's own rule for its config (`C:init.py:512`: an existing file needs
  `--yes`), checked for both files before either is written. The candidate pair is validated from a temporary
  sibling layout first. `_atomic.publish_local_targets` then publishes snapshot and config as one rollback unit.
  A validation failure publishes neither; a failure on either replace restores the exact old bytes and modes at
  both destinations, or removes both when they were new, then exits 1.

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
- `capture_physical_plan_inputs` (`execution/physical/_snapshot.py:49`, compile at `:152`).
- Out of scope with reasons (section 3): `execution/_chunked_oracle.py:246`, `execution/native/_plan.py:163`.
- `plan/_serialize.py:442` rebuilds from pinned bytes and never reads: N/A.

CLI call sites of those entries (the sentry derives this list by walking; the list is for review):
`C:run.py:868`, `C:gate/_layers.py:182`, `C:gate/_layers.py:245`, `C:compile_explain.py:235`,
`C:preflight.py:456`, `C:preflight.py:754`, `C:tokenize.py:343`, `C:api.py:521`, `C:demo.py:234`.

Generation routes that run the fidelity gate, all through `run_generation` (`generation/_streamed.py:319`):

- Public entries in `generation/_plan_entry.py`: `generate_tables` (`:147`), `generate_tables_streamed`
  (`:216`), `generate_tables_from_generation_plan` (`:252`), `generate_tables_streamed_from_generation_plan`
  (`:284`).
- Execution callers: `execution/_pipeline_generate_mask.py:128` (pure generate and mixed full frame) and
  `execution/_pipeline_mixed_route.py:74` (admitted mixed route, plus its runtime-decline handoff to the full
  frame caller). `execution/physical/drivers/_synthesis.py:50` admits no statistical column, so it cannot reach
  this gate.
- The mixed route's chunked lane runs only mask tables (`_pipeline_mixed_route.py:214`): N/A.
- Native: no generation kernel: N/A.

### 5.2 Exit paths

| Command | Case | Before | After |
|---|---|---|---|
| `run --fidelity` | no mask table | 0, "no mask-table fidelity report" | 1, before work |
| `run --rows` | mask table or a refused flag | option unknown | 1, before work |
| `init -o -` | with `--json`, or a CSV generate scaffold | n/a | 1 |
| `run`, `validate` | relative `snapshot_file` not beside the config | 0 from the right folder, 1 elsewhere | 1 from any folder, message shows the resolved path |

No exit integer is added or moved. The `--fidelity` and `--rows` refusals run before the first engine call. The
generate scaffold adds staged files and rollback, owned by `_atomic.publish_local_targets`; AT-23 covers every
failure point and both new and overwritten destinations.

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
| `sources[].path` | CLI `run.py:1559` `_load_sources_from_config`, `api.py:501` | config folder | unchanged; resolver delegates to the engine choke point |
| `snapshot_file` | engine `plan/_generation.py:130`, `_spec.py:174` | working directory | config folder |
| `sources[].column_names_file` | engine `profile/_csv_reader.py:95-111` via `resolve_csv_dialect`, called by CLI `_sources.py:65` and `capacity._resolved_config_copy` callers | working directory | config folder |
| `quarantine.output_path` | engine `quarantine.py:254`, path set by `_pipeline_context.with_quarantine_path` | working directory | config folder; a `quarantine_path` argument wins and is used as given |

`FILE_SOURCE_PATH_FIELDS` (`config/_sources.py:241-247`) marks `path` and `column_names_file` with
`x-decoy-path`; the build re-runs this walk on its base and adds any new field to the table.

New keys: no YAML key. Keyword `base_dir` on the section 3 engine entries and two readers; `on_warning` on
`run_generation` and the four generation entries; public names `fileaccess.config_relative`,
`generation_order` and `generate_option_catalog`; registered warning codes
`warning.generation_fidelity_below_threshold` and `warning.seed_ignored_with_master_key`; CLI option `run --rows`;
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
| 2 | Cleanup missing on an exit path | `--fidelity` and `--rows` refusals; `init` scaffold failure | refusals run before any engine call; the scaffold validates before publish and `_atomic.publish_local_targets` restores old files or removes new ones on any failed replace | AT-11 (empty target folder), AT-15, AT-18, AT-23 (old bytes and modes or no new files) | M2: fail the scaffold's second replace without rollback (AT-23 red) |
| 3 | State machine and concurrency | the warning sink; two scaffold publishers | a list local to one `run_pipeline` call with no module state; one non-blocking parent-directory lock spans each pair publish | AT-8 run twice in one process gives one warning each; AT-23 overlap cell refuses the second publisher without mutation | M3: hoist the sink list to module scope (AT-8 second run red); remove the publish lock (AT-23 overlap red) |
| 4 | Route parity and silent fallback | the four generation entries, the two execution callers and the mixed runtime-decline handoff; the nine CLI entry calls; `resolve_csv_dialect` | one sink keyword on `run_generation`; warnings travel beside pregenerated tables; one walk sentry over CLI calls | AT-8 route matrix; AT-5 sentry | M4: drop `pregenerated_warnings` on the mixed decline (AT-8 red) |
| 5 | Referential integrity | `run --rows` scaling; `generation_order` | one ratio for every table; the order walk is the one `generate_tables` uses | AT-14 (child keys exist in the parent sample), AT-20 | M5: scale only the largest table (AT-14 red) |
| 6 | Untrusted producer | YAML path strings; `--rows N`; the CSV given to `init` | `fileaccess.config_relative` is the one lexical join; absolute kept; the file policy still authorizes the read; `N` bounded 1 to 1000; the CSV goes through `fit`'s reader | AT-3, AT-4, AT-5, AT-15 | M6: replace the snapshot read's helper call with a local join (AT-5 path-choke cell red) |
| 7 | Check then use (TOCTOU) | the snapshot read | the one read pass pins bytes and the hash covers the bytes; resolving the locator adds no second open | AT-1, AT-2 | M7: stat the resolved path before reading it in a second open (AT-1 spy on `read_all` count red) |
| 8 | Weak tests | AT-1 from the config folder alone would pass on base | AT-1 runs from a second folder; AT-8 runs the same config at threshold 0.0 and asserts no warning | AT-1, AT-8 non-vacuity cells | M8: always emit the warning (AT-8 threshold 0.0 cell red) |
| 9 | Refactor drift | `generation_order` extraction; renderers moved out of `run.py` | `generate_tables` calls the accessor; moved renderers keep their text | AT-8 output bytes equal base; existing renderer tests unchanged | M9: reverse the order in the accessor (AT-8 bytes red) |
| 10 | Frozen contract drift | `pipeline_config_hash`, output bytes, exit codes, `run --json`, public API | the config dict is never rewritten; additive keys and keywords; exit integers unchanged | AT-2, AT-8, AT-11, AT-9 | M10: rewrite `snapshot_file` to an absolute path in the CLI (AT-2 red) |
| 11 | Parallel reason tables | the two new warning codes and their user renderers | both identities and fixed safe summaries live in the engine reason registry; render detail is derived from the `QualityWarning` record | AT-9, AT-13, AT-26 | M11: delete one registry entry while keeping its producer (AT-26 red) |

## 7. Acceptance tests (red first)

Each test is red on the named wrong build. "Base" is engine `84f4290d` with CLI `9f603536`.

| # | Test | Red on |
|---|---|---|
| AT-1 | Config in folder A with `snapshot_file: snap.json` beside it. `decoy run` from folder B succeeds and writes the same bytes to the same absolute targets as a run from A; a spy shows one `read_all` per snapshot in each snapshot-read pass. A run has three passes: config-only gate (`C:gate/_layers.py:182`), compile gate (`:245`) and run compile (`C:run.py:868`) | base (B cannot read `snap.json`) |
| AT-2 | `pipeline_config_hash` and the evidence config hash are equal for runs from A and from B, and equal the checked-in hash captured from engine `84f4290d` run from A. The fixture records its source config and capture command, not only the expected digest | a build that rewrites `snapshot_file` to an absolute path in the CLI |
| AT-3 | An absolute `snapshot_file` reads the same file from A and from B | a mutant that joins `base_dir` by string concatenation |
| AT-4 | `compile_plan` with no `base_dir` under a `RootedFileAccess` reads a root-relative snapshot exactly as on base | a mutant that calls `abspath` on the locator when `base_dir` is `None` |
| AT-5 | Walk sentry (`C:tests/sentry/io_policy/test_engine_base_dir.py`): every call of the section 3 engine entries in `src/decoy/` passes `base_dir=` as a keyword. The shared scanner sees direct imports, module aliases, `from` aliases and imported submodule attributes, and refuses positional `base_dir`. Engine path-choke guard: the snapshot readers and every `FILE_SOURCE_PATH_FIELDS` rewrite call `fileaccess.config_relative`; the old `_resolve_source_path` is absent. Behavioral half: `validate config`, `validate compile --explain` and `ci check` from B pass on AT-1's config | a seeded copy without `base_dir` at `gate/_layers.py:245`; a seeded positional `estimate_job_capacity(d, p)`; replacing the snapshot helper call with a local join |
| AT-6 | `snapshot_file` missing from A but present in B, run from B: exit 1, the message names `<A>/snap.json` and the rule | a build whose message shows only the declared string |
| AT-7 | Run from B: a relative `column_names_file` is read from A by `decoy run`, `decoy validate preflight` (the capacity estimate) and `decoy report fidelity --config`; a relative `quarantine.output_path` is written in A | base |
| AT-8 | Statistical generate config, `fidelity_warn_threshold: 0.99`, one weak column: the callback or `ExecutionResult.warnings` has one `warning.generation_fidelity_below_threshold` with table, scores, threshold and worst columns. Parametrized over the four `_plan_entry` entries and the actual execution routes: pure-generate full frame, forced-full-frame mixed, admitted mixed, and admitted mixed forced to runtime-decline after generation. The decline case asserts the warning appears once, not zero or twice. Output bytes equal checked-in golden bytes captured from engine `84f4290d`; the fixture records its source config and capture command. Non-vacuity: the same config at threshold 0.0 raises none; two runs in one process raise one each | base (no warning); a mutant that drops the sink on the mixed route or its decline handoff |
| AT-9 | Same config through `decoy run`: the card shows `Warnings: 1` and the sentence; `--json` `warnings` has that code; exit 0. A clean run has `"warnings": []`. No warning line or `detail` value holds a generated or source cell value | base CLI (no key) |
| AT-10 | Human `run` output lists each table with rows and file | base CLI |
| AT-11 | Generate-only config with `--fidelity`: exit 1, the target folder is empty, stderr equals the exact text in section 4 C1 | base (exit 0); a build that names a `report fidelity <config>` form |
| AT-12 | Mixed config with `--fidelity`: the mask report prints, then the generated-tables line, exit 0 | a build that refuses any config with a generate table |
| AT-13 | Generate-only, seed plus key: warning `warning.seed_ignored_with_master_key`, exit 0, output equal to a key-only run. Mixed, seed plus key: the same warning text ("for generated tables"), and the masked table, which uses `strategy: shuffle` with no mask secret (its permutation is seeded from `derive(job_seed, ...)`, `execution/_strategies/_shuffle.py:3-5`), changes bytes when the seed changes. Mask-only, seed plus key: no warning. Seed without key: no warning | base (silent); a mutant that warns whenever a seed is set; a mutant that warns on mask-only configs; a build that drops the seed from mask runs when a key is set |
| AT-14 | `run --rows 5` on a parent of 1,000 and a child of 5,000: `--json` reports 1 and 5 rows, child FK values exist in the parent sample, no file exists under the target folder, no evidence file | a build that ignores `--rows` and writes targets |
| AT-15 | `run --rows 5` with a mask table, with `--chunked`, with `--evidence-out`, with `--explain-plan`, with `--native`, with `--duckdb-source`, and `--rows 0` or `--rows 1001`: exit 1 before work | a build that silently drops the conflicting flag |
| AT-16 | `init --preset generate -o -`: stdout is YAML that `decoy validate config` accepts; no file named `-` exists; `-o - --json` exits 1 | base (writes `-`) |
| AT-17 | `init people.csv --preset generate -o gen/pipeline.yaml`: writes `gen/people.snapshot.json` and a config whose `snapshot_file` is `people.snapshot.json`; `decoy run gen/pipeline.yaml` from the parent folder succeeds with the CSV's columns and row count | base (mask scaffold); a build that writes `snapshot_file: gen/people.snapshot.json` |
| AT-18 | `init people.csv --preset generate -o -`: exit 1, no snapshot written | a build that writes the snapshot to the working directory |
| AT-19 | Help sentry (`C:tests/sentry/cli_text/test_help_panels.py`) walks the Typer app's registered commands and groups: each has a panel; `decoy --help` shows a Generate panel with `fit` | a seeded command without a panel |
| AT-20 | QA config with 3 references: `validate compile --explain` lists the generation order parents first and the 3 references, not "no FK dependencies detected"; `--json` has `generation` | base |
| AT-21 | `explain generation` names every type in `generate_option_catalog()` and each of its options; the test reads the catalog, not a list | a topic with one type removed from rendering |
| AT-22 | `explain transforms` lists exactly the `list strategies` set, contains `strategy:`, and contains neither `masking_rule` nor "future --mask"; the engine-internal allowlist baseline is unchanged | base |
| AT-23 | `init people.csv --preset generate` with validation forced to fail: exit 1 and neither new destination exists. With both destinations pre-existing and `--yes`, inject failure on the second replace: exit 1, both old byte strings and modes remain exact, and no staging or backup file remains. Repeat with both destinations new: neither remains. An existing config or snapshot without `--yes` is refused before staging. Pause one publisher after its first replace, start a second for the same pair, and assert that the second refuses without mutation and the final pair belongs wholly to the first | a build that validates after writing; a rollback that deletes overwritten files; two independent atomic writes; a publisher without the parent lock |
| AT-24 | `run_pipeline_isolated(..., isolate=True, base_dir=str(A))` from B reads `<A>/snap.json` in the child and matches the in-process run; `base_dir=Path(A)` raises the existing JSON-serializable `ValueError` | base (no `base_dir`); a build that resolves in the parent and drops the value from the payload |
| AT-25 | A raw dict, and forged-but-typed `Plan` and `GenerationPlan` fixtures whose embedded config has generate table `orders` reference mask table `customers`: `generation_order`, `generate_tables`, `generate_tables_streamed` and both `_from_generation_plan` entries raise `ValueError` "table 'orders' column 'customer_id': reference_table 'customers' is not a generate table" | a build where `generation_order` keeps `_table_order`'s skip (the streamed entry raises `KeyError`) |
| AT-26 | The reason manifest contains both new warning codes with fixed value-free summaries; every produced warning resolves through `require_known`; deleting either catalog entry fails the reason sentries and the manifest fixture | a producer with a free string code or a parallel CLI-only code table |
| AT-27 | `capture_physical_plan_inputs(..., base_dir=A)` from B reads `<A>/snap.json`; omitting `base_dir` keeps the base working-directory behavior | a build that adds `base_dir` to the other compile callers but misses `execution/physical/_snapshot.py:152` |

Test placement: engine AT-2 to AT-4, AT-8 and AT-27 in `tests/unit/plan/`, `tests/unit/execution/` and
`tests/physical/` beside the existing snapshot, gate and physical-input tests; reason test AT-26 beside the
reason manifest tests; CLI behavior tests in `tests/e2e/first_user/test_items_generate.py` (a new file in the
named folder); atomic rollback tests beside `tests/unit/test_atomic.py`; the sentries as named above.

Mutations to run on each final head: E1, drop `base_dir` in `read_and_pin_snapshots`, drop the mixed-decline
warning handoff, and remove one new reason entry. C1, remove `base_dir` from one call site, and warn on a seed
without a key. C2, write targets under `--rows`, fail the second scaffold replace, and remove the scaffold
publish lock. C3, drop one type from the topic render. Each must turn a test red.

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

- `capacity.py:156-191` supplies the existing rule promoted to `fileaccess.config_relative`; `resolve_path`
  (`C:_sources.py:27`) stays the CLI API but delegates to that public engine choke point.
- `QualityWarning` and `ExecutionResult.warnings` carry the new warning; no new channel.
- The reason registry carries both new warning identities and fixed summaries; no parallel code list.
- `_topo_sort` (`synthesize.py:202`) becomes the core of `generation_order`; `synthesize.py` and
  `_streamed._table_order` both call the accessor, so one walk exists.
- `GENERATE_OPTIONS` and `COMMON_OPTIONS` back the topic; the strategy data function in `cockpit.py` backs
  both `_strategies_list` and `explain transforms`.
- `decoy fit`'s fitting code and `write_local_target` back `init <csv> --preset generate`. Row 24l's
  `_csv_frame` (the Arrow-to-CSV writer in `io/_targets.py`) is not reused: init writes JSON and YAML.
- `capacity._resolved_config_copy` gains the other `FILE_SOURCE_PATH_FIELDS` fields instead of a second
  resolver.
- `_atomic.py`'s staged local writer and `run.py`'s vault rollback pattern become one multi-target publisher;
  its parent-directory lock follows `distribution/lock.py`; the scaffold does not grow a private cleanup or
  locking protocol.
- `render_card` and `make_table` (`C:ui/`) for all new output.

## 10. Placement (checked against engine `CODEMAP.md:225-246` and CLI `CODEMAP.md:42-63`)

| Change | File | Sits beside | CODEMAP |
|---|---|---|---|
| Shared config-relative rule | new engine `fileaccess/_config_path.py`, exported by `fileaccess/__init__.py` | `_component.py` and the file-access public facade | engine line 240, "Secret providers, file access"; no map change; package has 13 direct Python modules before, 14 after |
| `base_dir`, `pin_and_check_snapshots` | engine `plan/_generation.py`, `generation/statistical/_spec.py`, `plan/_compile.py`, `execution/_pipeline.py`, `_pipeline_context.py` (`build_run_context`, `with_quarantine_path`) | the existing read pass | no new module |
| `base_dir` on the other compile callers | engine `plan/validate/_consolidator.py`, `generation/_pinned_compile.py`, `execution/capacity.py` (`_resolved_config_copy` gains the names file), `execution/_job_estimate.py` | the existing `compile_plan` calls | none |
| Physical snapshot `base_dir` | engine `execution/physical/_snapshot.py` | its existing `compile_plan` call | engine line 230, "Physical seam" |
| Fidelity sink | engine `generation/_fidelity_gate.py`, `synthesize.py`, `_streamed.py` (`run_generation`, `_run_legacy`), `_plan_entry.py`, the two execution callers and full-frame handoff | `warn_on_low_fidelity` | none (`generation/` has 26 entries, so no new file there) |
| Warning reason identities | engine `reasons/warning/` catalog and manifest fixture | existing warning reasons | engine line 238, "Reason codes" |
| `generation_order` | engine `generation/synthesize.py`, called from `_streamed.py` | `_topo_sort` | none |
| `GenerateTypeOptions`, `generate_option_catalog` | engine `config/_generate_options.py` | `TypeOptions`, `GENERATE_OPTIONS` | none |
| CLI `base_dir` call sites | the eight files in 5.1 | the existing calls | none |
| CLI path wrapper and `column_names_file` resolution | `src/decoy/cli/_sources.py` (`resolve_path`, `read_csv_frame`) | existing source reads | existing frozen exception at line 50; no new module |
| Multi-target local publication | `src/decoy/_atomic.py` | `write_local_target`, `staged_local_target` | line 48, shared filesystem primitives |
| Strategy data for `list strategies` and `explain transforms` | `src/decoy/cli/cockpit.py` | `_strategies_list` | none; the engine-internal allowlist gains no entry |
| Walk sentry | `tests/sentry/io_policy/test_engine_base_dir.py` | `_calls.py` (the shared call scanner) | row "I/O policy sentries" |
| Summary rendering and moved renderers | `src/decoy/ui/run_summary.py` | `card.py`, `table.py` | add row "Run summary renderers" in C1; `ui/` has 7 direct modules before, 8 after |
| Preview, CSV generate scaffold, generation topic | new sub-package `src/decoy/cli/gen/` (`_preview.py`, `_scaffold.py`, `_topic.py`) | | new row "Generation CLI helpers" (a map change, made in C2), because `cli/` is frozen and over the fan-out limit |
| Help panels | `src/decoy/__main__.py` | the registrations at `:242-260` | none |
| Help sentry | `tests/sentry/cli_text/test_help_panels.py` | `test_no_traceback_escape.py` | row "User-facing CLI text" |
| Behavior tests | `tests/e2e/first_user/test_items_generate.py` | `test_items_text.py` | row "First-user QA item tests" |

Sizes at base: `run.py` 1,748 (over the 900 cap; must not grow), `init.py` 691 (C2 adds only the call into
`cli/gen/_scaffold.py` and the `-o -` branch, about 25 lines), `explain.py` 677 (C3 adds the render hook and
text, net about 30), `compile_explain.py` 498, `__main__.py` 264. Engine: `synthesize.py` 500,
`_fidelity_gate.py` 198, `_generate_options.py` 382, `plan/_generation.py` 286 (gains
`pin_and_check_snapshots`, about 35), `_spec.py` 581 (the `base_dir` keyword keeps it under 600),
`_streamed.py` 557, `_plan_entry.py` 306, `capacity.py` 551, `_consolidator.py` 113, `_pinned_compile.py` 326,
`_pipeline_context.py` 545, `_job_estimate.py` 208, `execution/physical/_snapshot.py` 261. CLI `_atomic.py`
is 521 and gains about 70 lines, staying under 900. Three pinned files:

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
- Reason manifest: gains two warning codes. If REG-RELEASE (`plan/reg-release`) has merged, they use
  `since=UNRELEASED` and E1 leaves `REGISTRY_VERSION` alone for the release to stamp. Otherwise E1 takes the
  next minor at build time, as today, and renumbers at merge.
- CLI exit codes: integers unchanged; two cases move from 0 to 1 (5.2).
- `run --json`: additive `warnings`; the preview payload is new and marked `"preview": true`.

## 12. Slices

| Slice | Repo | Branch | Size (hand-written) | Content | Gate |
|---|---|---|---|---|---|
| E1 | engine | `feat/gen-cli-e1` | M, about 300 lines plus tests | public config-path helper and guard; `base_dir` on the section 3 entries (with D-PATHS fields, `_resolved_config_copy` and physical capture); `pin_and_check_snapshots`; registered warning codes; fidelity sink and the mixed-decline handoff; `generation_order`; `GenerateTypeOptions` and `generate_option_catalog`; docstrings; AT-2 to AT-4, AT-7, AT-8 engine halves, AT-24, AT-26, AT-27 | full suite: reason and serialized result contracts plus public API |
| R | engine | release | n/a | engine release with E1 (owner step, not a build slice) | release checks |
| C1 | CLI | `feat/gen-cli-c1` | M, about 210 lines plus 104 moved | engine pin bump, `base_dir` at every entry and the walk sentry, `resolve_path` delegation, the `read_csv_frame` names-file descriptor, warnings and per-table lines, `--fidelity` refusal, seed warning, renderers moved to `ui/run_summary.py`, CODEMAP row; AT-1, AT-5, AT-6, AT-9 to AT-13 | full suite (version pin) |
| C2 | CLI | `feat/gen-cli-c2` | M, about 300 lines | locked `_atomic.publish_local_targets`; `cli/gen/` with `_preview.py` and `_scaffold.py`; CODEMAP row; `run --rows`; `init -o -`; `init <csv> --preset generate` with prevalidation and rollback; AT-14 to AT-18, AT-23 | T1, then reviewer T2 on the exact head |
| C3 | CLI | `feat/gen-cli-c3` | S, about 180 lines plus docs | help panels and sentry, `validate compile --explain` for generate, `explain generation` (`cli/gen/_topic.py`), `transforms` (with the `cockpit.py` strategy data function), `pipeline`, `yaml`, README, CLI reference, CHANGELOG, roadmap status; AT-19 to AT-22 | T1 |

Order: row 24m E2, then E1, R and C1. Row 24e's cited CLI slices are already in the base; C3 owns the remaining
topic overlap as OQ1 states. Row 24l E2 is already in the engine base; its E3 may land before or after E1 because
E3 changes `quality/fidelity.py`, not the warning transport. Serializing E1 after row 24m E2 avoids extracting
`generation_order` across an in-flight rewrite of the `synthesize.py` table loop and makes the option catalog
include `unique` on first release. C2 builds after row 24l C1 because both touch `fit.py`; C3 builds after row
24m C1 because its generation topic points to the `explain unique` topic and both edit `_TOPICS`. Row 24s
builds after E1 and extends the same catalog record. Row 24t P2b/P4 build after C1 for the shared run summary,
and its P7 builds after C2 so `write_pipeline_scaffold` can call the generation scaffold.

Commit subjects follow `workspace-and-vcs.md`: Conventional Commits, 72 characters at most, final line `cam`.

## 13. Pre-mortem

| Risk | Guard |
|---|---|
| A user's config relied on the working directory and breaks | AT-6 message names the resolved path and the rule; CHANGELOG entry; pre-GA hard change |
| A new CLI command calls an engine entry without `base_dir` | AT-5 walk sentry in the merge-gate suite |
| A future generate route skips the warning sink or loses it with pregenerated tables | the sink sits on `run_generation`; the callback is on every public entry; AT-8 covers both mixed outcomes and the handoff |
| Warnings flood the card on large runs | one line per warning; revisit with a cap only if a real run shows it |
| `run.py` grows past where it is | the renderer move nets about minus 60 lines in C1; the build report states the count |
| C2 and row 24t both create an init scaffold | D-SCAFFOLD names one owner and one call direction |
| `init <csv>` leaves half a scaffold, deletes an overwritten file or collides with another writer | central staged batch publication with rollback and a parent-directory lock; AT-23 failure and overlap cells |
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
