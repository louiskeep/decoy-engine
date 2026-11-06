# LAYOUT: layout map and placement guards for engine and CLI

Status: plan (2026-11-06, not built). Covers both repos; lives in the engine repo.

Rules consulted: `~/dev-rules/00-universal.md`, `development-loop.md`, `architecture.md`
(section "Layout: where things go"), `cross-cutting-concerns.md`, `testing.md`, `reuse-first.md`,
`README.md` (routing table); CLI roadmap `docs/ROADMAP.md` sections 4.1, 4.2 and row 7 (read from
CLI `origin/main` `7329c68`). Also read: engine and CLI `CODEMAP.md`, engine
`tests/sentry/test_module_size.py`, `test_public_import_boundary.py`, `test_package_map_paths.py`,
`_ratchet.py`, `_baseline_sources.py`, `test_baseline_registry.py`, `test_sentry_manifest.py`,
`scripts/dev/sentry_registry.toml`; CLI `tests/sentry/` (X0a port, CLI #81, #82) and
`test_engine_internal_boundary.py`; the H-1 tracker (vault `handoffs/phase1/h1/H1-TRACKER.md`).

Risk: R2. Test and guard tooling only, no product behavior change. Two parts change a dependency
lock (full-suite trigger, see section 9).

Owner request (2026-11-06): planning should say where files, folders and directory structures go,
so the codebase stays consistent and neat. LAYOUT absorbs roadmap row 7 (FILE-SIZE) and the size
census half of H-1 C0 (row 46).

## 1. Guarantee and non-goals

LAYOUT promises, in each repo:

1. A "Where things go" section in `CODEMAP.md`: one line per kind of module (owning package, file
   naming, where its tests and sentries live), written from today's code. Where today is
   inconsistent, the line states the target and lists today's exceptions. A sentry fails when the
   section names a path that does not exist.
2. A fan-out ratchet: a source package holds at most 25 direct modules. A package over 25 today is
   frozen at its exact count (shrink-only) and takes new modules only in a sub-package the map names.
3. Import-layer contracts checked by import-linter, declared from today's import graph, with
   today's violations in an exact ignore list that can only shrink. `exhaustive = true` means a new
   top-level package or module fails until it is placed in a layer.
4. CLI file size: about 800 lines, exact-match allowlist for today's oversize files, using the
   engine census logic (no third ratchet).
5. Every new allowlist (size, fan-out, layer ignores) is a registered X0a baseline, so the
   existing merge-base ratchet (`scripts/dev/ratchet_check.py`) stops it from growing.

Non-goals (a review finding here is a follow-up, not a blocker):

- No file moves, splits or new sub-packages. H-1 does those (E1 to E8, C1, C3). LAYOUT adds the
  map and the guards only.
- No change to the engine size policy (goal 600, max 700, `test_module_size.py:59`).
- No fan-out limit on `tests/` or `docs/` (owner question Q3).
- No size cap on CLI test files (the engine census also skips `tests/`).
- No new rule for "the CLI reaches the engine only through its public API". The CLI's
  `test_engine_internal_boundary.py` already owns it, and sees more than import-linter can
  (section 3, decision D3).
- No byte-identical re-vendor of `_ratchet.py` (X0a-h, roadmap row 9, deferred by the owner).
- No change to `test_public_import_boundary.py` (decision D4).

## 2. Measured today

Bases: engine `origin/main` `55876436`, CLI `origin/main` `7329c68` (2026-11-06). Counts are
re-measured on each part's build base; anything that merges first only moves the numbers.

Fan-out (direct `*.py` files, `__init__.py` not counted; sub-packages not counted):

| Package | Modules | Over 25 |
|---|---:|---|
| `E:src/decoy_engine/execution/` | 104 | frozen |
| `E:src/decoy_engine/execution/native/` | 55 | frozen |
| `E:src/decoy_engine/execution/out_of_core/` | 44 | frozen |
| `E:src/decoy_engine/plan/` | 30 | frozen |
| `E:src/decoy_engine/execution/_strategies/` | 29 | frozen (Q2) |
| `E:src/decoy_engine/transforms/` | 25 | at limit; next module goes in a sub-package |
| `E:src/decoy_engine/execution/physical/` | 24 | under |
| `E:src/decoy_engine/quality/` | 24 | under |
| `E:src/decoy_engine/` (root) | 21 modules, 34 sub-packages | under; new top-level children go through `exhaustive` layers |
| `C:src/decoy/cli/` | 31 | frozen |
| `C:src/decoy/distribution/` | 13 | under |

The brief's figures (93, 49, 40, 29, 28, 25; CLI 28) predate H-1 E3 (#386) and the CLI I/O modules;
the table above is what main holds now.

CLI files over 800 lines (`src/`, `scripts/`, `packaging_tools/`; `wc -l` convention):
`cli/report.py` 2382, `cli/run.py` 1812, `cli/scan.py` 1442, `cli/preflight.py` 886,
`cli/validate.py` 807 (new since the roadmap's 2026-11-05 list). Over 800 in CLI `tests/` (not
capped): `tests/unit/test_distribution.py` 1610, `tests/e2e/test_fit_command.py` 906,
`tests/unit/test_native_gate.py` 860, `tests/sentry/test_engine_internal_boundary.py` 808.

Engine size census: 34 entries, no drift against `test_module_size.py:69`, no unrecorded file
over 600.

Import graph (grimp 3.18, the import-linter engine, run against both trees in a scratch venv):

- Engine: 722 modules. Without the root `__init__`, 34 top-level children form one strongly
  connected component; a 6-tier layering (section 5) leaves 21 cross-tier back imports plus 7
  child-to-root imports (all `__version__`). `lint-imports` with that config: KEPT, 28 ignored
  imports, 1.4 s.
- CLI: 137 modules; a 6-tier layering leaves 8 back imports (`ci/` and `ui/output.py` into
  `cli/`) plus 8 child-to-root `__version__` imports. `lint-imports`: KEPT, 16 ignored imports.
- Each ignore line was dropped in turn and the contract went red every time (minimality check;
  one redundant CLI line, `decoy.__main__ -> decoy`, was removed).

## 3. Established solutions

Searches run (2026-11-06): web search for Python architecture and import-boundary tools
(import-linter, tach, pytestarch); tach's maintenance status; PyPI for the current import-linter
release; in-codebase search of both repos and the platform for existing size, fan-out, map and
boundary sentries.

Found:

- **import-linter** (seddonym/import-linter, BSD-2-Clause, 2.15 on PyPI, uses grimp 3.18,
  BSD-2-Clause). Contracts: `layers` (with `containers`, `exhaustive`, `:` non-independent and `|`
  independent siblings), `forbidden`, `independence`, `protected`, `acyclic_siblings`.
  `ignore_imports` with `unmatched_ignore_imports_alerting = error` (the default) fails on a stale
  ignore line, which gives the shrink half of a ratchet for free. Static parse, no import side
  effects, runs from Python (`importlinter.application.use_cases.lint_imports`). Designed for
  incremental adoption on legacy code. The design follows it.
- **tach** (gauge-sh): package-boundary manifest (`tach.yml`) with visualisation. Its maintenance
  passed to a new organisation after a gap (the DTach fork README said it was unmaintained, merged
  back upstream November 2025). Rejected: a second config format and a weaker history.
- **pytestarch** (ArchUnit-style assertions in pytest): rules are code, not config; smaller user
  base. Rejected.
- **Ruff `flake8-tidy-imports` banned-api (TID251)**: already a dependency, bans named modules,
  but has no notion of layers or direction. Not enough for layers.
- **pylint `too-many-lines` (C0302)**: the established file-length check, but no exact-match
  ratchet and it would add pylint for one rule. No established tool counts package fan-out.
- In-codebase: the engine census (`test_module_size.py:154` `_size_verdict`, exact-match, legacy
  shrink-only), the X0a key ratchet (`tests/sentry/_ratchet.py:97`, `ratchet_check.py`), the
  registry mirror (`_baseline_sources.py:100` engine, `:120` CLI), the map path sentry
  (`test_package_map_paths.py:15`), the engine public-boundary regex sentry
  (`test_public_import_boundary.py:40`), the CLI engine-internal AST sentry
  (`test_engine_internal_boundary.py:68`). The platform has no import-linter config.

Decisions:

- **D1. One census mechanism, vendored.** The engine's `_size_verdict` already implements the
  wanted policy (recorded value must equal the measured value; over the hard limit may only
  shrink). LAYOUT moves it, unchanged in behavior, into `E:tests/sentry/_layout.py` as one
  `ratchet_verdict` that takes the measured value, the recorded value, goal, limit and unit. Size
  (engine 600/700, CLI 800/800) and fan-out (25/25) all call it. The CLI vendors `_layout.py`
  byte-identical from the engine merge commit. Why vendoring and not one tool: a shared package
  would be a fourth repo or a dev-only module inside the engine wheel (the CLI's own boundary
  sentry bans private engine names, rightly). Why the engine is the source: the CLI already
  depends on the engine and the census was born there. Drift guard: `tests/sentry/test_vendored_files.py`
  in each repo pins the file's sha256 and the engine source commit, the X0a-h pattern
  (`P:docs/plans/2026-11-05-x0a-h-ratchet-hardening.md` E1, C1, known limit L13: no cross-repo
  check). X0a-h, when it returns, adds its rows to this same pin file instead of a new
  `test_ratchet_vendor.py`.
- **D2. One ratchet.** Every new allowlist is a registered X0a baseline (`sentry_registry.toml`
  row, `_baseline_sources.py` source, generated JSON). No new growth check is written. Allowlist
  constants are named `*_ALLOWLIST` so `test_baseline_registry.py` (`LIST_NAME`, engine `:26`,
  CLI `:29`) catches any copy that skips the registry.
- **D3. No import-linter contract for CLI to engine.** import-linter sees import statements only,
  and collapses an external package to its top-level name unless both packages are roots. The
  CLI sentry already bans `decoy_engine.internal` and `_private` names through imports, attribute
  chains, literal `getattr` and dynamic imports, with an exact-count allowlist (6 files today,
  baseline `engine-internal-allow`). A second, weaker mechanism for the same rule is what the
  owner rule forbids.
- **D4. `test_public_import_boundary.py` stays the owner of its rule.** It is an import-direction
  rule and could be an import-linter `forbidden` contract, but the move changes a registered
  baseline's key format (`public-import-boundary`), which the ratchet reads as a replacement that
  adds keys. Recorded as follow-up FU-1; the layers contract does not restate it.
- **D5. Map path checking is one function.** The engine already checks `_MAP.md` paths. The same
  function (`_layout.map_paths`) reads the CODEMAP "Where things go" section in both repos.
- **D6. Config file `.importlinter` (INI) at the repo root**, not `pyproject.toml`, so shrinking
  an ignore list never edits the file `scripts/dev/lock.sh --check` compiles from (CLI).

Custom code that remains: `_layout.py` (about 110 lines: `loc`, `ratchet_verdict`,
`direct_modules`, `map_paths`, `ignore_keys`), two fan-out tests, one CLI size test, two thin
import-linter test wrappers. Each is smaller than the tool it would replace.

## 4. The layout maps

Each repo's `CODEMAP.md` gets a `## Where Things Go` section after "Where Do I Find...". Format:
a table, one row per kind of module: `| Kind | Goes in | File naming | Tests | Sentries and registries |`.
The first cell of each row is plain text; path cells are backticked so `map_paths` can check them.
`_MAP.md` (engine) stays the per-package index; the new section says where a new thing goes and
does not restate package descriptions (one place per fact). Layer membership is stated once, in
`.importlinter`; the section links to it.

Engine rows (draft; the builder confirms each path against the build base):

| Kind | Goes in | Note |
|---|---|---|
| Public API | `src/decoy_engine/__init__.py` `__all__`; public facades at the root (`sdk.py`, `providers.py`, `errors.py`, `identifiers.py`) | facade list is `PUBLIC_BOUNDARY_FILES`; tests `tests/sentry/test_public_import_boundary.py` |
| Config models | `src/decoy_engine/config/` | tests `tests/unit/config/` |
| Plan compile and checks | `src/decoy_engine/plan/`; plan checks `_checks_<topic>.py` | frozen at 30: new checks go in `plan/checks/` (target; 19 `_checks_*` today) |
| Pipeline spine and routes | `src/decoy_engine/execution/` | frozen at 104: new route modules go in the cluster sub-package named for them: `execution/chunked/` (22 `_chunked*`), `execution/pipeline/` (16 `_pipeline*`), `execution/sequential/` (8), `execution/isolated/` (6); Q1 |
| Masking strategy handler | `src/decoy_engine/execution/_strategies/_<strategy>.py`, registered in `SCALAR_HANDLERS` | frozen at 29 (Q2) |
| Native companion bridge | `src/decoy_engine/execution/native/` | frozen at 55: new modules in `native/protocol/` (exists) or `native/draw/`, `native/chunked/` (targets) |
| Out-of-core FK | `src/decoy_engine/execution/out_of_core/` | frozen at 44: new modules in `out_of_core/stream/` (target) |
| Physical seam | `src/decoy_engine/execution/physical/` | new or changed modules allowlisted in `test_physical_seam_disconnection.py`; tests `tests/physical/` |
| Arrow kernels | `src/decoy_engine/kernel/` | |
| Transforms | `src/decoy_engine/transforms/` | at 25: the next module creates a sub-package |
| Generation | `src/decoy_engine/generation/`, pools in `generation/pool/` | tests `tests/generation/`, `tests/unit/generation/` |
| Providers | `src/decoy_engine/providers_v2/`, registered in `_registry.py` | |
| Detectors, profiling | `src/decoy_engine/detection/`, `src/decoy_engine/profiling/` | |
| Connectors | `src/decoy_engine/connectors/<name>.py` on the `sdk.py` base classes | tests `tests/connectors/` |
| Reason codes | `src/decoy_engine/reasons/<namespace>/` | registry `reasons/_registry.py`; reason sentries |
| Versioned contracts | `src/decoy_engine/contracts/<contract>.py` | |
| Secret providers, file access | `src/decoy_engine/secret_providers/`, `src/decoy_engine/fileaccess/` | registries `tests/sentry/file_access_registry.toml`, `file_access_modules.toml` |
| Foundation (errors, reasons adapters, private leaves) | root `_<name>.py` | bottom layer in `.importlinter` |
| Sentry test | `tests/sentry/test_<rule>.py`, helpers `tests/sentry/_<name>.py` | listed in `test_sentry_manifest.py` `SENTRIES`; allowlist named `*_ALLOWLIST`; baseline `tests/sentry/baselines/<sentry-id>.json`, row in `scripts/dev/sentry_registry.toml`, source in `_baseline_sources.py` |
| Layer contracts | `.importlinter` beside `pyproject.toml` | `tests/sentry/test_import_layers.py` |
| Dev tooling | `scripts/dev/`; one-off tools `scripts/<topic>/` | sized by `test_module_size.py` |
| Plans, decisions, records | `docs/plans/YYYY-MM-DD-<slug>.md`, `docs/decisions/`, `docs/records/YYYY-MM-DD-<slug>.md` | undated older plans are listed as exceptions |
| Unit tests | target `tests/unit/<package>/` mirroring `src/` | exceptions today: top-level `tests/parity/` (152 files), `tests/native/` (123), `tests/physical/` (61), `tests/fileaccess/`, `tests/secret_providers/`, `tests/generation/`, `tests/connectors/`, and 70 loose files in `tests/unit/` |

Known inconsistencies listed as exceptions (not fixed here): `execution/_errors.py` and 14 other
`execution` modules are imported from lower tiers (they are foundation code living in `execution/`; ignore list in section 5); `__version__`
read from the root facade by 7 modules (target: a leaf version module).

CLI rows (draft):

| Kind | Goes in | Note |
|---|---|---|
| Command module | `src/decoy/cli/<command>.py`, registered in `src/decoy/__main__.py` | frozen at 31: a new command or helper goes in a named sub-package (Q4); tests `tests/e2e/test_<command>*.py` (CliRunner or subprocess), unit tests `tests/unit/` |
| Command-private helpers | `src/decoy/cli/_<name>.py` | same freeze |
| Exit codes | `src/decoy/cli/exit_codes.py` | target: below `ci/` and `ui/` (imported by both; listed in the layer ignores) |
| Python API | `src/decoy/api.py` | |
| CI integration | `src/decoy/ci/` | command wrapper stays `cli/ci.py` |
| Terminal output | `src/decoy/ui/` | |
| Shipped install and update | `src/decoy/distribution/` | |
| Build-host release tools | `packaging_tools/` (not shipped) | sized by `test_module_size.py` |
| Packaged templates | `src/decoy/templates/` | |
| Private cross-command helpers | root `src/decoy/_<name>.py` (`_mask_secret.py`, `_native_gate.py`, LIB's `_atomic.py`) | bottom layer |
| Notifications, telemetry | `src/decoy/notify.py`, `src/decoy/telemetry.py` | |
| Sentry test | as engine row | |
| Layer contracts | `.importlinter` | |
| Dev tooling | `scripts/dev/`, release `scripts/release/` | |
| Roadmap, plans, records | `docs/ROADMAP.md`, `docs/roadmap/`, `docs/plans/`, `docs/records/` | |

## 5. Import-layer contracts

Engine `.importlinter` (one contract, `containers = decoy_engine`, `exhaustive = true`), top to
bottom, `:` = siblings may import each other:

1. `unmask : subset : walks : identifiers : providers`
2. `execution : kernel : quarantine : validation : verification : vault`
3. `plan : generation : generators : transforms : declared_casting : secret_providers : keyprovider : relationships : providers_v2 : reference_tables : quality`
4. `profile : profiling : detection : internal : progress : context : config : determinism : contracts : connectors : expressions : validators : sdk`
5. `io : data_discovery : disguises : phase : telemetry : safe_output : fileaccess`
6. `errors : reason_adapter : validation_result : release : reasons : instrumentation : checksums : _opaque : _lexeme : _error_base : _progress_events : _reference_date : codesets`

Tiers 5 and 6 are placed by meaning where the graph allows it (`safe_output`, `telemetry` and
`phase` sit low so execution can use them), not at the top where a pure sort puts unimported
modules. Ignore list (28, measured on `55876436`): 7 `declared_casting -> execution` (`_errors`,
`_pipeline_sources`, `out_of_core._budget`, `_scratch`), 6 `plan -> execution`
(`_strategies._bucketize`, `_top_code`, `_errors`, `out_of_core._mask`, `_distribution_behavior`,
`_technique_class`), 5 `generation -> execution` (`_chunked_output_sink`, `_mem_estimate`,
`out_of_core._budget`, `native._crypto_ext`, `native._index_ext`), `errors -> execution._row_error_summary`,
`fileaccess._errors -> execution._isolated_common`, `internal.faker_setup -> generation._dob_anchor`,
and 7 `-> decoy_engine` (`__version__`). Tier 3 is wide because plan, generation and transforms
import each other today (`plan -> transforms` 9, `transforms -> plan` 10); splitting it is a
later shrink, not this slice.

CLI `.importlinter` (`containers = decoy`, `exhaustive = true`; `|` = independent):

1. `__main__`
2. `api`
3. `cli : _update_check`
4. `ci`
5. `ui | distribution | notify | telemetry | templates`
6. `_mask_secret | _mask_secret_guidance | _native_gate` (plus `_atomic` once LIB L1b.1 merges)

Ignore list (16): `ci.adapters -> cli.evidence, cli.exit_codes, cli.scan, cli.validate`;
`ci.contracts, ci.runner, ci.staged, ui.output -> cli.exit_codes`; 8 `-> decoy` (`__version__`).

Both configs keep import-linter's default `unmatched_ignore_imports_alerting = error`.

## 6. Choke points, guards and red mutations

| Rule | Choke point | Guard test | Red mutation |
|---|---|---|---|
| File size (engine, unchanged) | `_layout.ratchet_verdict` via `_size_verdict` | `E:tests/sentry/test_module_size.py` | grow `scripts/gen_proof_manifest.py` by one line: red |
| File size (CLI) | same function, goal = limit = 800 | `C:tests/sentry/test_module_size.py` | append a line to `cli/report.py`: red |
| Fan-out | `_layout.direct_modules` walk of every directory under `src/<pkg>` + `ratchet_verdict`, limit 25 | `test_package_fanout.py` (both repos) | add empty `execution/_probe.py`: red; add a 26th module to `transforms/`: red |
| Layers and placement | `.importlinter` with `exhaustive = true` | `test_import_layers.py` (both repos) | delete one ignore line: red; add `src/decoy_engine/newpkg/__init__.py`: red (unplaced) |
| No allowlist growth | X0a registry and `ratchet_check.py` | `test_baseline_registry.py`, `ratchet_check.py` | add a key to `FANOUT_ALLOWLIST` or an ignore line not in the merge base: ratchet red |
| Map paths | `_layout.map_paths` | `E:test_package_map_paths.py`, `C:tests/sentry/test_codemap_paths.py` | name `src/decoy_engine/nope/` in the section: red |
| Vendored copy | sha256 pin | `test_vendored_files.py` (both repos) | change one byte of the CLI `_layout.py`: red |

Walk, not list: fan-out parametrizes over the directories it finds (`rglob`), never a hand
list; layers read the parsed import graph; the map check reads the markdown section. Each guard
has a seeded-bypass meta-test (below) proving the walk sees a planted violation.

The guards run in the sentry step of `scripts/dev/gates.sh` (engine `:142`, CLI `:91`), which
runs `ratchet_check.py` first and is what the merge gate executes; each new file is added to
`SENTRIES` in `test_sentry_manifest.py` (engine `:20`, CLI `:24`), whose own test fails otherwise.

## 7. Acceptance tests

Each test names the mutation that must turn it red. "Shown red" evidence goes in the part's PR.

- AT-1 (map, E1, C1): every backticked path in the first two cells of the "Where Things Go" rows
  exists. Mutation: add a row naming a missing path; red. Meta-test: `map_paths` on a seeded
  markdown string returns the planted path.
- AT-2 (census refactor, E2): the existing `test_size_verdict_branches` table
  (`test_module_size.py:210`) passes unchanged against the moved function, and every message
  string is byte-identical to today's. Mutation: drop the `loc > ceiling` legacy branch in
  `ratchet_verdict`; the `(815, 809, False)` case goes red.
- AT-3 (fan-out, E2, C2): every package directory is at or under 25 modules, or its
  `FANOUT_ALLOWLIST` entry equals its count exactly; entries exist and are still over 25.
  Mutations: plant a module in a frozen package (red, "may only shrink"); delete one module from
  a frozen package in a scratch copy (red, "ratchet down"); plant a 26th module in an unfrozen
  package (red). Meta-test: `direct_modules` on a `tmp_path` tree with `__init__.py`, three
  modules and a sub-package returns 3.
- AT-4 (CLI size, C2): `src/`, `scripts/`, `packaging_tools/` files are at or under 800, or equal
  their allowlist value; legacy entries may only shrink. Mutations: grow `cli/run.py` by one line
  (red); remove `validate.py` from the allowlist (red).
- AT-5 (layers, E3, C3): `lint_imports(config_filename=".importlinter", cache_dir=None)` returns
  true on the tree. Mutations: (a) copy the config to `tmp_path` minus one ignore line: false;
  (b) seed `from decoy_engine.execution import _errors` into a tier 6 module in a scratch copy:
  false; (c) add an unplaced top-level package in a scratch copy: false (exhaustive); (d) add an
  ignore line that matches no import: false (unmatched alerting).
- AT-6 (registry, E2, E3, C2, C3): each new allowlist's live key set equals its registered
  baseline (`test_source_matches_its_registered_baseline`), and `ratchet_check.py` against the
  merge base reports "first inventory" for each new id and no findings. Mutation: add one key to
  a baseline in a follow-up commit; `ratchet_check.py` red.
- AT-7 (vendor pin, E2, C2): `sha256(tests/sentry/_layout.py)` equals the pinned value in both
  repos, and the CLI header names the engine commit. Mutation: change one byte; red.
- AT-8 (no behavior change): T1 selection green on each part's head; `git diff --stat` touches
  no file under `src/`.

## 8. Placement of this plan's new files

The map lines named are the ones this plan adds (section 4); each is a map change stated here.

| New file | Directory | Map line | Sits beside |
|---|---|---|---|
| `_layout.py` | `E:tests/sentry/`, vendored to `C:tests/sentry/` | Sentry test (helpers `_<name>.py`) | `_ratchet.py` |
| `test_package_fanout.py` | `tests/sentry/` (both) | Sentry test | `test_module_size.py` |
| `test_import_layers.py` | `tests/sentry/` (both) | Sentry test; Layer contracts | `test_public_import_boundary.py` (E), `test_engine_internal_boundary.py` (C) |
| `test_vendored_files.py` | `tests/sentry/` (both) | Sentry test | `test_ratchet_check.py` |
| `test_module_size.py` | `C:tests/sentry/` | Sentry test | `test_source_hygiene.py` |
| `test_codemap_paths.py` | `C:tests/sentry/` | Sentry test | `test_sentry_manifest.py` |
| `package-fanout.json`, `import-layers.json` (both), `module-size.json` (C) | `tests/sentry/baselines/` | Sentry test (baseline) | existing baselines |
| `.importlinter` | repo root (both) | Layer contracts | `pyproject.toml` |

No new `src/` file, so no frozen package grows.

## 9. Parts (slices)

Each part: Conventional Commits, subject at most 72 characters, final line `cam`. Test scope per
`testing.md`: T0 per edit, T1 per commit (selection + the guards above + lint, format, mypy);
gates run T2 on the recorded head. The census allowlist values, fan-out counts and ignore lists are
regenerated on the part's build base, never copied from this plan.

| Part | Repo | Branch | Base | Depends on | Hand-written lines | Lock |
|---|---|---|---|---|---:|---|
| E1 map | engine | `feat/layout-e1-map` | engine main | none | ~150 | no |
| C1 map | CLI | `feat/layout-c1-map` | CLI main | none | ~70 | no |
| E2 census | engine | `feat/layout-e2-census` | engine main after E1 | E1; after CI-E1 and SUB-STOP merge | ~330 | no |
| E3 layers | engine | `feat/layout-e3-layers` | engine main after E2 | E2; after CI-E1s | ~170 + lock | yes |
| C2 census | CLI | `feat/layout-c2-census` | CLI main after C1 | E2 merged (vendor source); after LIB L1b.1 | ~300 + vendored file | no |
| C3 layers | CLI | `feat/layout-c3-layers` | CLI main after C2 | C2 | ~150 + lock | yes |

E1 (engine map). `CODEMAP.md` "Where Things Go" section (section 4 rows); `tests/sentry/_layout.py`
with `map_paths` only; `test_package_map_paths.py` gains one test reading the CODEMAP section
through `map_paths`, and its `_MAP.md` test calls the same function. AT-1. Touches no file in the
in-flight list: buildable now.

C1 (CLI map). `CODEMAP.md` "Where Things Go" section only (docs). Its path guard arrives in C2
with the vendored helper. Buildable now.

E2 (engine census and fan-out). `_layout.py` grows to the full helper (`loc`, `ratchet_verdict`,
`direct_modules`, `ignore_keys`); `test_module_size.py` keeps `GOAL`, `MAX`, `ALLOWLIST` and its
tests, and `_size_verdict` becomes a one-line call into `_layout`; new `test_package_fanout.py`
(`FANOUT_ALLOWLIST` with a reason and the map's target sub-package per entry); new
`test_vendored_files.py`; registry row, `_baseline_sources.py` source, generated
`package-fanout.json` (`created_in = "LAYOUT-E2"`); `SENTRIES` row. AT-2, AT-3, AT-6, AT-7.
Waits for CI-E1 (edits `test_module_size.py` and `test_sentry_manifest.py`) and SUB-STOP (edits
`test_module_size.py` and `_baseline_sources.py`; may add execution modules).

E3 (engine layers). `import-linter` in the `dev` extra of `pyproject.toml`, `uv.lock` regenerated;
`.importlinter` (section 5); `test_import_layers.py`; `_baseline_sources.py` source keyed
`<contract>::<importer> -> <imported>` via `_layout.ignore_keys`; generated `import-layers.json`;
registry row; `SENTRIES` row; `.gitignore` gains `.import_linter_cache/`. AT-5, AT-6. Lock change:
after CI-E1s (lock security bump). Re-measure the ignore list after CLI-DIALECT Part E
(`profile/`, `__init__.py`) and SUB-STOP and REG-RENUMBER (`reasons/`) if they merged first.
`scripts/dev/licenses.py` must pass (both new packages are BSD-2-Clause, allowed).

C2 (CLI census, fan-out, map guard). Vendored `_layout.py` (from the E2 merge commit, header
names it); `test_vendored_files.py`; `test_module_size.py` (goal = limit = 800, `SIZE_ALLOWLIST`
of section 2); `test_package_fanout.py` (`cli/` frozen); `test_codemap_paths.py`; three
registry rows, sources and generated baselines (`created_in = "LAYOUT-C2"`, which the CLI
`_ratchet.py` checks for a first inventory); `SENTRIES` rows. AT-1, AT-3, AT-4, AT-6, AT-7. Waits
for LIB L1b.1 (edits `sentry_registry.toml`, `_baseline_sources.py`, `test_sentry_manifest.py`).

C3 (CLI layers). `import-linter` in the `dev` extra, `requirements-dev.lock` regenerated with
`scripts/dev/lock.sh`; `.importlinter`; `test_import_layers.py`; source, baseline, registry row;
`.gitignore`. The CLI `Source` type gains a callable key form next to `InCode` (about 10 lines,
`_baseline_sources.py:105`). AT-5, AT-6.

Full-suite triggers: E3 and C3 change a dependency lock, so each runs the full suite (T3) on its
merge head, diffed against the saved main baseline (engine on the Mac or a test VM, CLI on the
devbox). E1, E2, C1, C2 run T1 plus the gate set only; E2 and C2 touch no shared fixture or test
config (helpers under `tests/sentry/` are not `conftest.py`).

Build now, given the in-flight list: E1 and C1. Then E2 when CI-E1 and SUB-STOP merge; C2 when E2
and LIB L1b.1 merge; E3 after CI-E1s; C3 after C2.

## 10. File-size table

Hand-written lines; vendored and generated files named separately.

| File | Repo | Before | After (about) |
|---|---|---:|---:|
| `CODEMAP.md` | engine | 233 | 300 |
| `CODEMAP.md` | CLI | 83 | 140 |
| `tests/sentry/_layout.py` (new) | engine | 0 | 110 |
| `tests/sentry/_layout.py` (vendored) | CLI | 0 | 110 (copy) |
| `tests/sentry/test_package_map_paths.py` | engine | 31 | 45 |
| `tests/sentry/test_module_size.py` | engine | 262 | 225 |
| `tests/sentry/test_module_size.py` (new) | CLI | 0 | 90 |
| `tests/sentry/test_package_fanout.py` (new) | each | 0 | 100 |
| `tests/sentry/test_import_layers.py` (new) | each | 0 | 80 |
| `tests/sentry/test_vendored_files.py` (new) | each | 0 | 30 |
| `tests/sentry/test_codemap_paths.py` (new) | CLI | 0 | 25 |
| `tests/sentry/_baseline_sources.py` | engine | 250 | 265 |
| `tests/sentry/_baseline_sources.py` | CLI | 189 | 215 |
| `tests/sentry/test_sentry_manifest.py` | engine, CLI | 125, 125 | 128, 129 |
| `scripts/dev/sentry_registry.toml` | engine, CLI | 85, 15 | 93, 27 |
| `.importlinter` (new) | engine, CLI | 0 | 50, 35 |
| `pyproject.toml` | engine, CLI | 1063, 256 | +1 each |
| baselines JSON, `uv.lock`, `requirements-dev.lock` | both | generated | generated |

Every file stays under its repo's cap; `test_module_size.py` shrinks.

## 11. Design notes

- Principles: single source of truth for the census policy (one `ratchet_verdict` instead of a
  copy per check), open/closed for checks (a new census is a new call site with its own limit,
  not a new ratchet). Change pain removed: today a CLI size rule would be a second copy of the
  engine's verdict, and a fan-out rule a third.
- Pattern followed: import-linter layered contracts with a ratcheted ignore list (its documented
  legacy-adoption path); the X0a baseline registry for growth; the X0a-h sha pin for vendoring.
- Where each fact lives: limits in each repo's test (`GOAL`, `MAX`, `LIMIT`); allowlist values in
  the test constant (exact counts); allowlist keys mirrored in the baseline JSON (the ratchet's
  copy, guarded for agreement); layers and ignores in `.importlinter`; "where a new thing goes"
  in CODEMAP; per-package description in `_MAP.md` (engine). None restated.
- House style: pure verdict functions, typed returns, no flag arguments (goal, limit and unit are
  values, not switches), comments explain why.
- Owner question 4.3 asked of each piece: the census function (does what `_size_verdict` does,
  so it is that function); fan-out (no existing check); import layers (overlaps
  `test_public_import_boundary.py` and the CLI internal-boundary sentry, handled by D3 and D4);
  map path check (reuses the `_MAP.md` check, D5); baseline reader (reuses X0a sources).

## 12. Pre-mortem (failure modes)

- A slice merges before E2 or C2 and grows a frozen package: no harm, the freeze records the
  count on its build base.
- CLI-FMT adds `cli/_targets.py` after C2 merges: `cli/` is frozen, so it must land in the named
  sub-package (Q4). If CLI-FMT merges first, the freeze absorbs it.
- An H-1 move renames a frozen package's modules: the count falls, the test asks for a ratchet
  down in the same PR (intended), and the ignore list may need a matching shrink.
- import-linter or grimp release breaks parsing: the lock pins both; a bump is a reviewed lock
  change.
- `.import_linter_cache/` written into the tree: the tests pass `cache_dir=None`, and
  `.gitignore` covers manual runs.
- Windows lane (WIN-RUNNER): grimp ships Windows wheels; paths in `_layout` use `Path.as_posix()`
  for keys.

## 12a. Class table (plan contract)

| # | Class | Applies | Closing structure | Proof and mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | N/A: guard messages carry repo-relative paths, module names and counts only, never data, config text or exception text | guard messages built from paths and integers in `_layout.py` | AT-3 message text checked; no value source exists |
| 2 | Cleanup on an exit path | N/A: no resource is owned; the meta-tests write only under pytest `tmp_path`, and lint runs with `cache_dir=None` | pytest `tmp_path` fixture | AT-5 asserts no `.import_linter_cache/` appears in the repo after the run |
| 3 | State machine and concurrency | N/A: pure functions over the checked-out tree; no shared state, no lock | none needed | none |
| 4 | Route parity and silent fallback | Yes, as "a check that silently skips": a guard that finds zero packages or zero rows would pass vacuously | non-empty assertions in each walk | meta-tests: `direct_modules` and `map_paths` on seeded trees; mutation: make the walk return `[]`, the non-empty assertion goes red |
| 5 | Referential integrity | N/A: no data or FK handling; the only references are allowlist keys to files, checked by "entry exists" tests | `test_allowlist_paths_exist`-style checks | mutation: name a deleted package in `FANOUT_ALLOWLIST`; red |
| 6 | Untrusted producer | N/A: inputs are the repo's own source tree and config files, read at test time | none | none |
| 7 | Check then use (TOCTOU) | N/A: no path is checked and then acted on; files are only read | none | none |
| 8 | Weak tests and overclaimed evidence | Yes: a ratchet that cannot see a planted violation, or numbers copied from this plan instead of re-measured | seeded-bypass meta-test per guard; counts regenerated on the build base | AT-1 to AT-7 each name a mutation shown red in the PR; reviewer re-measures one count |
| 9 | Behavior-preserving refactor drift | Yes: E2 moves `_size_verdict` into `_layout.py` | the existing branch table stays the characterization test | AT-2: unchanged `test_size_verdict_branches` plus byte-identical messages; mutation drops one branch, red |
| 10 | Frozen contract drift | N/A: no product contract changes; see section 12c | none | none |

## 12b. Enumerations

### Exit paths

- N/A: the guards own no files or state. The only writers are the meta-tests under `tmp_path`
  (`test_module_size.py:256` is the existing pattern).

### Routes (where the guards run)

- Local gate sentry step: engine `scripts/dev/gates.sh:142`, CLI `scripts/dev/gates.sh:91`
  (runs `ratchet_check.py`, then pytest over `SENTRIES`).
- Manifest: `tests/sentry/test_sentry_manifest.py:20` (engine), `:24` (CLI); a sentry file not
  listed fails `test_manifest_lists_every_sentry_file_and_no_ghost`.
- Registry mirror: `tests/sentry/test_baseline_registry.py:26` (`LIST_NAME`), sources at
  `tests/sentry/_baseline_sources.py:100` (engine) and `:120` (CLI).
- T1 selection: guard tests are always selected (`testing.md` T1).

### FK shapes

- N/A: no data path is touched; LAYOUT reads source files and config only.

### Fields

- N/A: no public result type is added or changed. The new constants are test-local:
  `FANOUT_ALLOWLIST`, CLI `SIZE_ALLOWLIST`, each `dict[str, int]` keyed by repo-relative path.

### Output surfaces

- pytest assertion messages: repo-relative path, count, limit and remedy.
- `ratchet_check.py` report lines (`tests/sentry/_ratchet.py:97`): sentry ids and keys.
- `lint-imports` broken-contract output: module names and line numbers. Nothing else.

## 12c. Reuse inventory

- Census verdict: `tests/sentry/test_module_size.py:154` `_size_verdict` (moved, not copied).
- Line count: `tests/sentry/test_module_size.py:148` `_loc` (moved).
- Growth ratchet: `tests/sentry/_ratchet.py:97` `check` and `scripts/dev/ratchet_check.py` (both repos).
- Registry: `scripts/dev/sentry_registry.toml`, `_baseline_sources.py` `SOURCES` and `write_initial`.
- Map path check: `tests/sentry/test_package_map_paths.py:15`.
- Boundary rules left in place: `tests/sentry/test_public_import_boundary.py:40` (engine),
  `tests/sentry/test_engine_internal_boundary.py:68` (CLI).
- License policy: `scripts/dev/license_policy.json` (BSD-2-Clause allowed).
- Lock tooling: engine `uv.lock`, CLI `scripts/dev/lock.sh`.

## 12d. Frozen contracts

None touched. No engine public surface, CLI command, flag, exit code, file format or reason code
changes. The X0a baseline JSON schema is used as is (three new files, same fields).

## 13. Follow-ups (recorded, not in scope)

- FU-1: fold `test_public_import_boundary.py` into an import-linter `forbidden` contract once the
  baseline key change can be reviewed as a replacement.
- FU-2: shrink engine tier 3 (split plan below generation and transforms) as H-1 moves land.
- FU-3: move `execution/_errors.py` and the other foundation-in-execution modules down (H-1 E8).
- FU-4: a leaf version module in each repo, removing the 7 + 8 `__version__` ignore lines.
- FU-5: H-1 C0 keeps only the pure-move checker port; its size census half is C2 here.

## 14. Owner questions (answered by Cam, 2026-11-06)

- Q1 answered: yes. The sub-package names in section 4 are the targets, and H-1 E8 keeps them (this also answers H-1 Q3).
- Q2 answered: freeze. `execution/_strategies/` is frozen at today's count, and the next strategy starts a family sub-package.
- Q3 answered: not in LAYOUT, but recorded as follow-up LAYOUT-T (test-folder fan-out and layout). Owner: "clear organized folder structure is important." The map lists today's large test folders as exceptions, and LAYOUT-T is a roadmap row.
- Q4 answered: yes, and the naming conventions apply now. Owner: "start new proper naming conventions now, let's not introduce more technical debt." New CLI source-reader and target-writer modules go in `src/decoy/cli/io/` starting today, including CLI-FMT's target writer (`cli/io/_targets.py`, not `cli/_targets.py`), even before C2's freeze lands. The C1 map states this rule, and the CLI-FMT handoff is updated to match.
