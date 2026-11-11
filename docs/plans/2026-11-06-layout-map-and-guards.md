# LAYOUT: layout map and placement guards for engine and CLI

Status: plan (2026-11-06). Built: E1 (engine #410), C1 (CLI #183), E2 (engine #479), C2 (CLI
#196). E3 in review on branch `feat/layout-e3-layers`. Not built: C3. Covers both repos;
lives in the engine repo.

Rules consulted: `~/dev-rules/00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `architecture.md`
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

Revision note r2 (Codex applying Dennis r1, 2026-11-06): F1 moves engine import-linter into a
separate `layers` extra, wires the post-CI-E1 GitHub sentry install, makes absence fail, and lists
transitive licenses. F2 makes `cli/io/` the rule now and records `_sources.py` as the H-1 exception.
F3 extends the shared X0a ratchet with numeric `max` values. F4 defines and meta-tests the map-path
grammar. F5 runs layer mutations against scratch trees in subprocesses. F6 keys layers by tier and
requires mapped sub-packages. F7 freezes engine `scripts/` at 26. F8 removes unguarded counts. F9
names the strategy-family pattern. F10 records the D3 process exception. F11 parameterizes census
messages and limits byte-identical compatibility to size messages.

Revision note r3 (Codex fix to green, 2026-11-06): H1 gives the layer check its own gate step and
lock-synced `.venv-layers`, keeps the engine certified `dev+lint+vault+cloud` environment exact,
and covers local gates, Mac and VM T3, and post-CI-E1 GitHub CI. H2 chooses option (a): exact layer
membership is a guarded snapshot in `test_import_layers.py`, while only ignore keys use X0a; its
acceptance cases prove that a reviewed placement and a downward tier move can pass. M1 gives
`map_paths` an explicit base directory for repo-relative CODEMAP paths and package-relative
`_MAP.md` paths. M2 marks layer files as `(target)` until E3 and C3 create them. L1 makes each CLI
numeric source emit `max` and makes both ratchet copies use one vendored meta-test case table. L2
expands both draft maps to the grammar's five columns. The whole-plan DESIGN audit also moves all
new sentry modules into a cohesive `tests/sentry/layout/` subpackage and makes the existing
manifest guard recursive, so neither repo grows an over-cap direct sentry package.

## 1. Guarantee and non-goals

LAYOUT promises, in each repo:

1. A "Where things go" section in `CODEMAP.md`: one line per kind of module (owning package, file
   naming, where its tests and sentries live), written from today's code. Where today is
   inconsistent, the line states the target and lists today's exceptions. A sentry fails when the
   section names a concrete path that does not exist; a `(target)` path checks its existing parent.
2. A fan-out ratchet: a source package holds at most 25 direct modules. Engine `scripts/` follows
   the same rule. A package over 25 today is frozen at its exact count (shrink-only) and takes new
   modules only in a sub-package the map names.
3. Import-layer contracts checked by import-linter, declared from today's import graph, with
   today's violations in an exact ignore list that can only shrink. `exhaustive = true` means a new
   top-level package or module fails until it is placed in a layer. The layout subpackage's
   `test_import_layers.py` pins an
   exact ordered layer-expression snapshot, including `:` and `|` separators, and compares it with
   `.importlinter`, so a reviewed placement or tier
   move updates those two artifacts together and an unreviewed mismatch fails.
4. CLI file size: about 800 lines, exact-match allowlist for today's oversize files, using the
   engine census logic (no third ratchet).
5. Every new allowlist (size, fan-out, layer ignores) is a registered X0a baseline. Layer
   membership is not an X0a key set. Numeric
   baseline entries carry `max`; the existing merge-base ratchet (`scripts/dev/ratchet_check.py`)
   fails when a head value exceeds its base value as well as when a key is added. This is an about
   10-line extension to shared `_ratchet.check`, not a second ratchet.

Non-goals (a review finding here is a follow-up, not a blocker):

- No product-source file moves, splits or new sub-packages. H-1 does those (E1 to E8, C1, C3).
  LAYOUT adds the map, guards and one test-only `tests/sentry/layout/` subpackage required by the
  placement cap.
- No change to the engine size policy (goal 600, max 700, `test_module_size.py:59`).
- No broad fan-out sentry on `tests/` or `docs/` (owner question Q3). This plan still obeys the
  placement rule: engine `tests/sentry/` is already over 25 direct modules, and the CLI would cross
  25 with these additions, so all new layout sentries live in `tests/sentry/layout/`.
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
| `E:scripts/` | 26 | frozen; new tools go in the map's named topic sub-packages |
| `E:src/decoy_engine/execution/physical/` | 24 | under |
| `E:src/decoy_engine/quality/` | 24 | under |
| `E:src/decoy_engine/` (root) | 21 modules, 34 sub-packages | under; new top-level children go through `exhaustive` layers |
| `C:src/decoy/cli/` | 31 | frozen |
| `C:src/decoy/distribution/` | 13 | under |
| `E:tests/sentry/` | 48 direct modules | frozen for placement; new layout sentries use `layout/` |
| `C:tests/sentry/` | 19 direct modules | additions use `layout/` rather than crossing 25 |

LAYOUT freezes engine `scripts/` rather than making it a non-goal because it is already above the
package cap and is the active placement surface for new developer tooling. Excluding it would
leave the same fan-out and placement failure unguarded outside `src/`.

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

- Engine: without the root `__init__`, 34 top-level children form one strongly
  connected component; a 6-tier layering (section 5) leaves 21 cross-tier back imports plus 7
  child-to-root imports (all `__version__`). `lint-imports` with that config: KEPT, 28 ignored
  imports, 1.4 s.
- CLI: a 6-tier layering leaves 8 back imports (`ci/` and `ui/output.py` into
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
  Locked transitive license inventory: import-linter BSD-2-Clause; grimp BSD-2-Clause; click
  BSD-3-Clause; rich MIT; markdown-it-py MIT; mdurl MIT; Pygments BSD-2-Clause;
  typing-extensions PSF-2.0; and tomli MIT on Python below 3.11. The lock and
  `scripts/dev/licenses.py` verify the resolved set rather than relying on this prose list.
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
  shrink). LAYOUT moves it, unchanged in behavior, into `E:tests/sentry/layout/_layout.py` as one
  `ratchet_verdict` that takes the measured value, recorded value, goal, limit, message noun,
  allowlist name and fix advice. Size (engine 600/700, CLI 800/800) passes today's `LOC`,
  `ALLOWLIST` and `Decompose` wording so its messages stay byte-identical. Fan-out passes its own
  accurate terms. Both call the same verdict function. The CLI vendors `_layout.py`
  byte-identical from the engine merge commit. Why vendoring and not one tool: a shared package
  would be a fourth repo or a dev-only module inside the engine wheel (the CLI's own boundary
  sentry bans private engine names, rightly). Both repos import through the layout subpackage's
  `__init__.py`, not another package's private module. Why the engine is the source: the CLI already
  depends on the engine and the census was born there. Drift guard:
  `tests/sentry/layout/test_vendored_files.py`
  in each repo pins the file's sha256 and the engine source commit, the X0a-h pattern
  (`P:docs/plans/2026-11-05-x0a-h-ratchet-hardening.md` E1, C1, known limit L13: no cross-repo
  check). X0a-h, when it returns, adds its rows to this same pin file instead of a new
  `test_ratchet_vendor.py`. The same pin file owns `_ratchet_cases.py`; no second vendor mechanism
  is added.
- **D2. One ratchet.** Every new allowlist is a registered X0a baseline (`sentry_registry.toml`
  row, `_baseline_sources.py` source, generated JSON). Numeric entries include their live `max`.
  Shared `_ratchet.check` keeps its key comparison and adds one value comparison: a matching head
  entry with `max` above the base entry is red. No new ratchet or growth check is written.
  Allowlist constants are named `*_ALLOWLIST` so `test_baseline_registry.py` (`LIST_NAME`, engine
  `:26`, CLI `:29`) catches any copy that skips the registry.
- **D2a. Layer membership uses option (a), an exact guarded snapshot.** Each
  `tests/sentry/layout/test_import_layers.py` holds an explicit exact ordered layer-expression
  constant, including the `:` and `|` sibling separators. The test parses
  its repo's `.importlinter` and fails unless the constant and config agree exactly. A legitimate new
  package or downward tier move edits `.importlinter` and the snapshot in the same reviewed diff.
  Membership never enters the X0a baseline, because X0a intentionally rejects a new key on an
  existing sentry id. Only exact ignore-import keys go through X0a and remain shrink-only. The two
  representations are justified because `.importlinter` is the tool input and the snapshot is the
  review pin; their equality test is the guard against drift.
- **D3. No import-linter contract for CLI to engine.** import-linter sees import statements only,
  and collapses an external package to its top-level name unless both packages are roots. The
  CLI sentry already bans `decoy_engine.internal` and `_private` names through imports, attribute
  chains, literal `getattr` and dynamic imports, with an exact-count allowlist (6 files today,
  baseline `engine-internal-allow`). A second, weaker mechanism for the same rule is what the
  owner rule forbids.
  Exception `ARCH-LAYERS-D3` under `risk-and-exceptions.md`: policy domain `architecture.md`
  established-tool layer check; scope is only the CLI-to-engine public-boundary rule; risk R2;
  rationale is that import-linter cannot represent the dynamic and attribute access already
  covered by the AST sentry; compensating controls are its exact-count X0a baseline, seeded
  bypass mutations, T1 wiring and independent T2 review; residual risk is maintenance of a
  custom AST visitor; approver is Cam at plan approval before C3; expiry is FU-6, when
  import-linter can cover the same access forms or the exception is re-approved.
- **D4. `test_public_import_boundary.py` stays the owner of its rule.** It is an import-direction
  rule and could be an import-linter `forbidden` contract, but the move changes a registered
  baseline's key format (`public-import-boundary`), which the ratchet reads as a replacement that
  adds keys. Recorded as follow-up FU-1; the layers contract does not restate it.
- **D5. Map path checking is one function.** The engine already checks `_MAP.md` paths. The same
  function (`_layout.map_paths(markdown, base, columns)`) reads the CODEMAP "Where things go"
  section in both repos. CODEMAP callers pass the repo root as `base`; the engine `_MAP.md` caller
  passes `src/decoy_engine`. Resolution is always relative to the explicit base.
- **D6. Config file `.importlinter` (INI) at the repo root**, not `pyproject.toml`, so shrinking
  an ignore list never edits the file `scripts/dev/lock.sh --check` compiles from (CLI).

Custom code that remains: `tests/sentry/layout/_layout.py` (about 110 lines: `loc`, `ratchet_verdict`,
`direct_modules`, `map_paths`, `ignore_keys`), `_ratchet_cases.py` (about 20 lines), two fan-out
tests, one CLI size test, and two thin import-linter test wrappers. Each is bounded and cohesive.

## 4. The layout maps

Each repo's `CODEMAP.md` gets a `## Where Things Go` section after "Where Do I Find...". Format:
a table, one row per kind of module: `| Kind | Goes in | File naming | Tests | Sentries and registries |`.
The first cell of each row is plain text; path cells are backticked so `map_paths` can check them.
The grammar is exact: callers pass an explicit base directory and the zero-based table columns to inspect;
CODEMAP checks `Goes in`, `File naming`, `Tests`, and `Sentries and registries`, while `_MAP.md`
passes `src/decoy_engine` and its existing path column. Every CODEMAP path is repo-relative and
every `_MAP.md` path is package-relative. In a checked code span, a segment containing a `<...>`
placeholder or `*` truncates validation to the existing
parent before that segment. A code span with neither `/` nor `.py` is a symbol and is skipped.
A future path is followed immediately by `(target)` and validates its existing parent. Every
other concrete path must exist. Thus symbols such as `__all__` and `SCALAR_HANDLERS` are not
paths. A bare `_MAP.md` file such as `sdk.py` resolves below `src/decoy_engine`; the CODEMAP form
is `src/decoy_engine/sdk.py` because its base is the repo root.
`_MAP.md` (engine) stays the per-package index; the new section says where a new thing goes and
does not restate package descriptions (one place per fact). Layer membership is stated once, in
`.importlinter`; the test carries only its guarded exact review snapshot, and the section links to
the config.

Engine rows (draft; the builder confirms each path against the build base):

The builder also inventories every direct sub-package below each frozen package and adds its
repo-relative path to the applicable row if this draft does not already show it. The fan-out
guard compares that live inventory with the map, so the draft cannot silently omit one.

| Kind | Goes in | File naming | Tests | Sentries and registries |
|---|---|---|---|---|
| Public API | `src/decoy_engine/` | `src/decoy_engine/__init__.py`; root facades `src/decoy_engine/sdk.py`, `src/decoy_engine/providers.py`, `src/decoy_engine/errors.py`, `src/decoy_engine/identifiers.py` | unit tests for the owning surface | facade list `PUBLIC_BOUNDARY_FILES`; `tests/sentry/test_public_import_boundary.py` |
| Config models | `src/decoy_engine/config/` | `src/decoy_engine/config/_<topic>.py` | `tests/unit/config/` | - |
| Plan compile and checks | `src/decoy_engine/plan/`; new checks in `src/decoy_engine/plan/checks/` (target) | `src/decoy_engine/plan/_checks_<topic>.py` today | plan unit tests | fan-out freeze at 30 |
| Pipeline spine and routes | `src/decoy_engine/execution/`; new route clusters in `src/decoy_engine/execution/chunked/` (target), `src/decoy_engine/execution/pipeline/` (target), `src/decoy_engine/execution/sequential/` (target), or `src/decoy_engine/execution/isolated/` (target), per Q1 | route-specific private names | execution tests | fan-out freeze at 104 |
| Masking strategy handler | `src/decoy_engine/execution/_strategies/`; next family in `src/decoy_engine/execution/_strategies/<family>/` (target), per Q2 | `src/decoy_engine/execution/_strategies/_<strategy>.py` | strategy tests | `SCALAR_HANDLERS`; fan-out freeze at 29 |
| Native companion bridge | `src/decoy_engine/execution/native/`; new modules in `src/decoy_engine/execution/native/protocol/` (target), `src/decoy_engine/execution/native/draw/` (target), or `src/decoy_engine/execution/native/chunked/` (target) | private by bridge concern | native tests | fan-out freeze at 55 |
| Out-of-core FK | `src/decoy_engine/execution/out_of_core/`; new stream modules in `src/decoy_engine/execution/out_of_core/stream/` (target) | private by stage | out-of-core tests | fan-out freeze at 44 |
| Physical seam | `src/decoy_engine/execution/physical/` | private by seam concern | `tests/physical/` | `tests/sentry/test_physical_seam_disconnection.py` |
| Arrow kernels | `src/decoy_engine/kernel/` | by kernel operation | kernel tests | - |
| Transforms | `src/decoy_engine/transforms/`; next module creates a named sub-package | by transform | transform tests | fan-out limit 25 |
| Generation | `src/decoy_engine/generation/`; pools in `src/decoy_engine/generation/pool/` | by generation concern | `tests/generation/`, `tests/unit/generation/` | - |
| Providers | `src/decoy_engine/providers_v2/` | by provider | provider tests | `src/decoy_engine/providers_v2/_registry.py` |
| Detectors, profiling | `src/decoy_engine/detection/`, `src/decoy_engine/profiling/` | by detection or profiling concern | owning package tests | - |
| Connectors | `src/decoy_engine/connectors/` | `src/decoy_engine/connectors/<name>.py` on `src/decoy_engine/sdk.py` base classes | `tests/connectors/` | connector entry-point checks |
| Reason codes | `src/decoy_engine/reasons/<namespace>/` | by reason namespace | reason tests | `src/decoy_engine/reasons/_registry.py`; reason sentries |
| Versioned contracts | `src/decoy_engine/contracts/` | `src/decoy_engine/contracts/<contract>.py` | contract tests | contract sentries |
| Secret providers, file access | `src/decoy_engine/secret_providers/`, `src/decoy_engine/fileaccess/` | by provider or access concern | owning package tests | `tests/sentry/file_access_registry.toml`, `tests/sentry/file_access_modules.toml` |
| Foundation (errors, reasons adapters, private leaves) | `src/decoy_engine/` | `src/decoy_engine/_<name>.py` | owning surface tests | bottom layer in `./.importlinter` (target) |
| Sentry test | existing rules in `tests/sentry/`; layout rules in `tests/sentry/layout/` | `test_<rule>.py`; helper `_<name>.py` | self-tests and seeded mutations | `tests/sentry/test_sentry_manifest.py` recursively walks sentries; `*_ALLOWLIST`; `tests/sentry/baselines/<sentry-id>.json`; `scripts/dev/sentry_registry.toml`; `tests/sentry/_baseline_sources.py` |
| Layer contracts | `./.importlinter` (target) beside `./pyproject.toml` | `.importlinter` | `tests/sentry/layout/test_import_layers.py` (target) | exact membership snapshot in the test; ignore baseline `tests/sentry/baselines/import-layers.json` (target) |
| Dev tooling | `scripts/dev/`; one-off tools in `scripts/<topic>/` | by tool concern | tool tests | root `scripts/` freeze at 26; `tests/sentry/test_module_size.py` |
| Plans, decisions, records | `docs/plans/`, `docs/decisions/`, `docs/records/` | `docs/plans/YYYY-MM-DD-<slug>.md`; `docs/records/YYYY-MM-DD-<slug>.md` | - | undated older plans are listed exceptions |
| Unit tests | `tests/unit/<package>/` (target) mirroring `src/` | `test_<behavior>.py` | - | current exceptions: `tests/parity/`, `tests/native/`, `tests/physical/`, `tests/fileaccess/`, `tests/secret_providers/`, `tests/generation/`, `tests/connectors/`, and loose files in `tests/unit/` |

Known inconsistencies listed as exceptions (not fixed here): `execution/_errors.py` and 14 other
`execution` modules are imported from lower tiers (they are foundation code living in `execution/`; ignore list in section 5); `__version__`
read from the root facade by 7 modules (target: a leaf version module).

CLI rows (draft):

| Kind | Goes in | File naming | Tests | Sentries and registries |
|---|---|---|---|---|
| Command module | `src/decoy/cli/`; new commands go in a named sub-package per Q4 | `src/decoy/cli/<command>.py` | `tests/e2e/test_<command>*.py` using CliRunner or subprocess; `tests/unit/` | `src/decoy/__main__.py`; fan-out freeze at 31 |
| Command-private helpers | a named sub-package below `src/decoy/cli/` for new work | `src/decoy/cli/_<name>.py` is the current exception | `tests/unit/` | fan-out freeze at 31 |
| Source readers and target writers | `src/decoy/cli/io/` (target) | `src/decoy/cli/io/_sources.py` (target), `src/decoy/cli/io/_targets.py` (target) | I/O unit and E2E tests | `src/decoy/cli/_sources.py` is today's exception and moves under H-1 |
| Exit codes | `src/decoy/cli/` | `src/decoy/cli/exit_codes.py` | CLI tests | target layer below `src/decoy/ci/` and `src/decoy/ui/`; current imports are ignored exactly |
| Python API | `src/decoy/` | `src/decoy/api.py` | API tests | - |
| CI integration | `src/decoy/ci/` | by CI concern; wrapper `src/decoy/cli/ci.py` | CI tests | - |
| Terminal output | `src/decoy/ui/` | by output concern | UI tests | - |
| Shipped install and update | `src/decoy/distribution/` | by distribution concern | distribution tests | - |
| Build-host release tools | `packaging_tools/` | by release concern | packaging tests | `tests/sentry/layout/test_module_size.py` (target) |
| Packaged templates | `src/decoy/templates/` | by template | template tests | package-data checks |
| Private cross-command helpers | `src/decoy/` | `src/decoy/_<name>.py`; current examples `src/decoy/_mask_secret.py`, `src/decoy/_native_gate.py`; LIB adds `src/decoy/_atomic.py` (target) | owning tests | bottom layer |
| Notifications, telemetry | `src/decoy/` | `src/decoy/notify.py`, `src/decoy/telemetry.py` | notification and telemetry tests | - |
| Sentry test | existing rules in `tests/sentry/`; layout rules in `tests/sentry/layout/` (target) | `test_<rule>.py`; helper `_<name>.py` | self-tests and seeded mutations | manifest recursively walks sentries; `*_ALLOWLIST`, baselines, registry and source table as in the engine row |
| Layer contracts | `./.importlinter` (target) | `.importlinter` | `tests/sentry/layout/test_import_layers.py` (target) | exact membership snapshot in the test; ignore baseline `tests/sentry/baselines/import-layers.json` (target) |
| Dev tooling | `scripts/dev/`; release tools in `scripts/release/` | by tool concern | tool tests | tooling sentries |
| Roadmap, plans, records | `docs/`, `docs/roadmap/`, `docs/plans/`, `docs/records/` | `docs/ROADMAP.md`; dated plan and record names | - | roadmap and shipped-log checks |

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
| File size (CLI) | same function, goal = limit = 800 | `C:tests/sentry/layout/test_module_size.py` | append a line to `cli/report.py`: red |
| Fan-out | `_layout.direct_modules` walk of every directory under `src/<pkg>` plus engine `scripts/`, then `ratchet_verdict`, limit 25 | `tests/sentry/layout/test_package_fanout.py` (both repos) | add empty `execution/_probe.py`: red; add a 26th module to `transforms/`: red; add a direct sub-package under a frozen package without a map cell: red |
| Layers and placement | `.importlinter` with `exhaustive = true` plus the exact membership snapshot in `layout/test_import_layers.py` | `tests/sentry/layout/test_import_layers.py` (both repos) | delete one ignore line: red; change config membership without its snapshot: red; add `src/decoy_engine/newpkg/__init__.py` without placing it: red |
| No allowlist growth | shared X0a registry and value-aware `_ratchet.check`; layer ignore keys only | `test_baseline_registry.py`, `ratchet_check.py` | add an ignore key or raise a numeric `max`: ratchet red |
| Map paths | `_layout.map_paths` | `E:test_package_map_paths.py`, `C:tests/sentry/layout/test_codemap_paths.py` | name `src/decoy_engine/nope/` in the section: red |
| Vendored copy | one sha256 pin table | `tests/sentry/layout/test_vendored_files.py` (both repos) | change one byte of the CLI `_layout.py` or `_ratchet_cases.py`: red |
| Nested sentry discovery | recursive manifest walk | `tests/sentry/test_sentry_manifest.py` (both repos) | add `tests/sentry/layout/test_unlisted.py`: red |
| Sentry gate ownership | `gates.sh` `LAYER_TEST` names the only isolated sentry; every other listed sentry belongs to the ordinary sentry step | `tests/sentry/test_sentry_manifest.py` (both repos) | remove the layer step or ignore a second sentry: red |

Walk, not list: fan-out parametrizes over the directories it finds (`rglob`), never a hand
list, and asserts that every direct sub-package of each frozen package appears in a CODEMAP path
cell. Layers read the parsed import graph; the map check reads the markdown section. Each guard
has a seeded-bypass meta-test (below) proving the walk sees a planted violation.

Every guard except the layer wrapper runs in the sentry step of `scripts/dev/gates.sh` (engine
`:142`, CLI `:91`), which runs `ratchet_check.py` first. Each script declares the path once as
`LAYER_TEST=tests/sentry/layout/test_import_layers.py`; the sentry command derives its explicit
`--ignore=$LAYER_TEST`, ordinary changed-test selection filters that path, and the `layers` step
runs exactly that path. Import-linter deliberately is absent from the engine's exact certified
venv, so the layer wrapper belongs only to the isolated layer step. Each new test is added to
`SENTRIES` in `test_sentry_manifest.py` (engine `:20`, CLI `:24`). The manifest recursively finds
test files and verifies that the ordinary and isolated routes partition `SENTRIES` with no gap or
overlap; removing the layer step or ignoring any second sentry is red.

Both `gates.sh` scripts add a default `layers` step. It is a real gate, not a helper invocation:

- Engine sets `UV_PROJECT_ENVIRONMENT=$ROOT/.venv-layers`, runs
  `uv sync --frozen --extra dev --extra layers --python 3.10.20`, then runs only
  `$LAYER_TEST` with `.venv-layers/bin/python`. It never installs into or checks the
  certified `.venv`, whose `dev+lint+vault+cloud` profile remains exact.
- CLI sets `DECOY_VENV=$ROOT/.venv-layers`, runs `scripts/dev/bootstrap.sh` so the separate venv is
  synced exactly from `requirements-dev.lock`, then runs only `$LAYER_TEST` there.
- In both repos, the wrapper imports `importlinter.application.use_cases.lint_imports` inside the
  test function. Collection in any environment without `layers` therefore succeeds. Calling that
  function outside the layer gate fails with install advice rather than skipping.

The scripts defer their ordinary `.venv/bin` existence check until a selected step needs that
venv. A layers-only selection also skips changed-test merge-base discovery and uses the layer
interpreter for the evidence reporter after the sync. Therefore `gates.sh --steps layers` can
bootstrap and run only `.venv-layers` on a clean host with no `origin/main` ref or ordinary venv.
The step name is added to help text, validation, evidence output and the default step list.

`.venv-layers/` is ignored and never becomes an output artifact. `gates.sh --baseline` runs the
layer step for a tree that contains the wrapper and reports it not applicable for an older tree
that predates the wrapper. The layer step is included in local T1/T2, in engine Mac and VM merge
gates, and in CLI merge gates.

## 7. Acceptance tests

Each test names the mutation that must turn it red. "Shown red" evidence goes in the part's PR.

- AT-1 (map, E1, C1): `map_paths(markdown, base, columns)` applies the section 4 grammar to
  the selected path columns. Concrete paths below `base` exist; `<...>` and `*` segments check
  the parent before that segment; symbol code spans without `/` or `.py` are skipped; `(target)`
  paths check their parent; an absent concrete path is red. Separate seeded-string meta-test cases
  cover a concrete path, placeholder, glob, skipped symbol, target path, wrong column and missing
  path. One CODEMAP case passes the repo root and resolves `src/decoy_engine/sdk.py`; one `_MAP.md`
  case passes `src/decoy_engine` and resolves bare `sdk.py`. Mutation: pass the repo root to the
  `_MAP.md` case or add a selected-cell path naming a missing concrete path; red.
- AT-2 (census refactor, E2): the existing `test_size_verdict_branches` table
  (`test_module_size.py:210`) passes unchanged against the moved function, and size message
  strings are byte-identical to today's. Fan-out tests instead assert their parameterized noun,
  allowlist name and fix advice. Mutation: drop the `loc > ceiling` legacy branch in
  `ratchet_verdict`; the `(815, 809, False)` size case goes red.
- AT-3 (fan-out, E2, C2): every package directory is at or under 25 modules, or its
  `FANOUT_ALLOWLIST` entry equals its count exactly; entries exist and are still over 25.
  Mutations: plant a module in a frozen package (red, "may only shrink"); delete one module from
  a frozen package in a scratch copy (red, "ratchet down"); plant a 26th module in an unfrozen
  package (red); raise the head baseline entry's `max` above its base value (shared X0a ratchet
  red); add an unmapped direct sub-package below a frozen package (red). Engine `scripts/` is one
  of the frozen entries at its build-base count. Meta-test: `direct_modules` on a `tmp_path` tree
  with `__init__.py`, three modules and a sub-package returns 3.
- AT-4 (CLI size, C2): `src/`, `scripts/`, `packaging_tools/` files are at or under 800, or equal
  their allowlist value; legacy entries may only shrink. Mutations: grow `cli/run.py` by one line
  (red); remove `validate.py` from the allowlist (red).
- AT-5 (layers, E3, C3): the wrapper fails, rather than skips, if import-linter is unavailable;
  import-linter is imported inside the test function, so collection without the dependency passes;
  otherwise `lint_imports` returns true. `gates.sh --steps layers` also passes on a scratch checkout
  with no ordinary `.venv` and no `origin/main` ref, proving the isolated route. Each mutation runs
  in a fresh subprocess with
  `PYTHONPATH=<tmp>/src`, an absolute scratch-copy config path and `cache_dir=None`. The harness
  makes the subprocess report `importlib.util.find_spec()`'s module path and asserts that path is
  inside `tmp_path` before accepting the lint result. Mutations: (a) remove one ignore line: false; (b) seed
  `from decoy_engine.execution import _errors` into a tier 6 module: false; (c) add an unplaced
  top-level package: false (exhaustive); (d) add an ignore line that matches no import: false
  (unmatched alerting); (e) two reviewed scratch diffs go green: add a legitimate top-level
  package, its five-column CODEMAP row and its downward-legal tier plus the exact snapshot, then
  move an existing module to a lower tier and update the exact snapshot. For each, omit the
  snapshot edit and the equality guard is red. The test also asserts that neither change needs a
  new X0a membership key.
- AT-6 (registry, E2, E3, C2, C3): each new allowlist's live key set equals its registered
  baseline (`test_source_matches_its_registered_baseline`); numeric entries also equal their live
  `max`; layer entries contain ignore keys only. The legitimate new-package placement and downward
  tier move from AT-5(e) both leave the X0a key set unchanged and `ratchet_check.py` green; a new
  ignore key is red and a removed ignore key is green.
  `ratchet_check.py` against the merge base reports "first inventory" for each new id and no
  findings. Mutations: add an ignore key or raise `max`; `ratchet_check.py` red. Both repos'
  `_ratchet.py` meta-tests parameterize the same `MAX_VALUE_CASES` table from the sha-pinned,
  vendored `_ratchet_cases.py` for absent, equal, lower and higher `max` comparisons. The higher case is
  red. Changing the case table in only one repo breaks the vendor pin.
- AT-7 (vendor pin, E2, C2): sha256 for `tests/sentry/layout/_layout.py` and
  `_ratchet_cases.py` equals
  the pinned value in both repos, and each CLI header names the engine commit. Mutation: change one
  byte in either vendored helper; red.
- AT-8 (no behavior change): T1 selection green on each part's head; `git diff --stat` touches
  no file under `src/`. The manifest's on-disk walk uses `rglob("test_*.py")`; a seeded nested
  `tests/sentry/layout/test_unlisted.py` is reported missing from `SENTRIES`. Route meta-tests
  assert that `LAYER_TEST` is the sole sentry exclusion and is owned by the `layers` step; removing
  that step or adding another exclusion is red.

## 8. Placement of this plan's new files

The map lines named are the ones this plan adds (section 4); each is a map change stated here.

| New file | Directory | Map line | Sits beside |
|---|---|---|---|
| `layout/__init__.py` | `tests/sentry/layout/` (both) | Sentry test (layout subpackage) | `_layout.py` |
| `_layout.py` | `E:tests/sentry/layout/`, vendored to `C:tests/sentry/layout/` | Sentry test (layout helper `_<name>.py`) | `_ratchet_cases.py` |
| `_ratchet_cases.py` | `E:tests/sentry/layout/`, vendored to `C:tests/sentry/layout/` | Sentry test (layout helper `_<name>.py`) | `_layout.py` |
| `test_package_fanout.py` | `tests/sentry/layout/` (both) | Sentry test (layout rule) | `test_import_layers.py` |
| `test_import_layers.py` | `tests/sentry/layout/` (both) | Sentry test; Layer contracts | `test_package_fanout.py` |
| `test_vendored_files.py` | `tests/sentry/layout/` (both) | Sentry test (layout rule) | `_layout.py` |
| `test_module_size.py` | `C:tests/sentry/layout/` | Sentry test (layout rule) | `test_package_fanout.py` |
| `test_codemap_paths.py` | `C:tests/sentry/layout/` | Sentry test (layout rule) | `test_package_fanout.py` |
| `package-fanout.json`, `import-layers.json` (both), `module-size.json` (C) | `tests/sentry/baselines/` | Sentry test (baseline) | existing baselines |
| `.importlinter` | repo root (both) | Layer contracts | `pyproject.toml` |

No new `src/` file or direct `tests/sentry/*.py` module is added. The engine's 48-module sentry
package stays frozen, the CLI's 19-module package does not approach the cap, and the cohesive new
`layout/` subpackage starts below the cap in both repos.

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
| E3 layers | engine | `feat/layout-e3-layers` | engine main after E2 | E2; after CI-E1 | ~180 + lock | yes |
| C2 census | CLI | `feat/layout-c2-census` | CLI main after C1 | E2 merged (vendor source); after LIB L1b.1 | ~300 + vendored file | no |
| C3 layers | CLI | `feat/layout-c3-layers` | CLI main after C2 | C2 | ~150 + lock | yes |

E1 (engine map). `CODEMAP.md` "Where Things Go" section (section 4 rows), with `.importlinter`,
`layout/test_import_layers.py` and its baseline marked `(target)`; the new
`tests/sentry/layout/` subpackage with public `__init__.py` and `_layout.py`
with `map_paths` only; `test_package_map_paths.py` gains one test reading the CODEMAP section
through `map_paths` with the repo root as `base`, and its `_MAP.md` test calls the same function
with `src/decoy_engine` as `base`. AT-1. Touches no file in the
in-flight list: buildable now.

C1 (CLI map). `CODEMAP.md` "Where Things Go" section only (docs), with `.importlinter`,
`tests/sentry/layout/`, `layout/test_import_layers.py` and its baseline marked `(target)`. Its path
guard and layout subpackage arrive in C2 with the vendored helper. Buildable now.

E2 (engine census and fan-out). `layout/_layout.py` grows to the full helper (`loc`, `ratchet_verdict`,
`direct_modules`, `ignore_keys`); `test_module_size.py` keeps `GOAL`, `MAX`, `ALLOWLIST` and its
tests, and `_size_verdict` becomes a one-line call into `_layout`; new `test_package_fanout.py`
under `tests/sentry/layout/` (`FANOUT_ALLOWLIST` with `scripts/` frozen at its build-base count, a reason and the map's target
sub-package per entry); shared `_ratchet.check` gains the about 10-line numeric `max` comparison;
numeric baseline sources emit `max`; `_ratchet_cases.py` owns the shared `MAX_VALUE_CASES`
meta-test table and is vendored with the existing sha-pin mechanism;
new `layout/test_vendored_files.py`; registry row, `_baseline_sources.py` source, generated
`package-fanout.json` (`created_in = "LAYOUT-E2"`); `SENTRIES` row. AT-2, AT-3, AT-6, AT-7.
`test_sentry_manifest.py` changes its on-disk discovery from direct `glob` to recursive `rglob` and
adds the nested-file red mutation from AT-8.
Waits for CI-E1 (edits `test_module_size.py` and `test_sentry_manifest.py`) and SUB-STOP (edits
`test_module_size.py` and `_baseline_sources.py`; may add execution modules).

E3 (engine layers). `import-linter==2.15` in a separate `layers` extra of `pyproject.toml`; the
certified DP install remains exactly `dev+lint+vault+cloud` and 77 packages. After CI-E1 has
landed, `gates.sh` gains the isolated `layers` step from section 6 and its sentry step explicitly
ignores `tests/sentry/layout/test_import_layers.py`; `uv.lock` is regenerated;
because engine `pyproject.toml` is already over 800 lines, compact one adjacent optional-dependency
array without changing its entries so adding `layers` does not increase the file's build-base line count;
`.importlinter` (section 5); `layout/test_import_layers.py` with its exact membership snapshot;
`_baseline_sources.py` source keyed only by
`<contract>::<importer> -> <imported>` ignore keys via `_layout.ignore_keys`; generated
`import-layers.json`; registry row; `SENTRIES` row; `.gitignore` gains `.import_linter_cache/` and
`.venv-layers/`. The same diff drops `(target)` from the three E1 layer paths because it creates
them. The test imports import-linter inside the test function, fails with install advice when the
dependency is absent, and never skips. `test_sentry_manifest.py` adds the ordinary-versus-isolated
route partition assertions from section 6. AT-5, AT-6. Lock and CI changes land only
after CI-E1 (lock security bump and `ci.yml` rewrite). Re-measure the ignore list after CLI-DIALECT Part E
(`profile/`, `__init__.py`) and SUB-STOP and REG-RENUMBER (`reasons/`) if they merged first.
`scripts/dev/licenses.py` must pass for the full transitive license inventory in section 3.

E3 also adds a dedicated post-CI-E1 GitHub `layers-gate` job. After setup-uv and the pinned Python
setup, it calls `scripts/dev/gates.sh --steps layers`, so CI uses the same isolated sync and test
route as local gates. Every GitHub pytest route that includes the ordinary sentry suite, including
the regression gate, explicitly ignores the layer test; marker-only ML and packaging jobs need no
dependency because the import is function-local. The certified job and its 77-distribution
environment are unchanged.

C2 (CLI census, fan-out, map guard). New public `tests/sentry/layout/` subpackage with vendored
`_layout.py` (from the E2 merge commit, header names it); `test_vendored_files.py`;
`test_module_size.py` (goal = limit = 800, `SIZE_ALLOWLIST`
of section 2); existing `_ratchet.check` gains the same numeric `max` comparison as engine;
`test_package_fanout.py` (`cli/` frozen); `test_codemap_paths.py`; two
registry rows, sources and generated baselines (`created_in = "LAYOUT-C2"`, which the CLI
`_ratchet.py` checks for a first inventory); `SENTRIES` rows. `test_sentry_manifest.py` changes its
walk to `rglob` and carries the AT-8 nested-file mutation. AT-1, AT-3, AT-4, AT-6, AT-7. Waits
for LIB L1b.1 (edits `sentry_registry.toml`, `_baseline_sources.py`, `test_sentry_manifest.py`).
Each numeric CLI `Source`/`InCode` form emits both its key-to-reason mapping and live `max`; its
ratchet meta-test consumes the same vendored `_ratchet_cases.py` table as the engine.
C2 drops `(target)` from the C1 `tests/sentry/layout/` and `layout/test_module_size.py` map cells in
the same diff that creates them; the layer config, layer test and ignore baseline stay marked until
C3 creates them.

C3 (CLI layers). `import-linter` in the `dev` extra, `requirements-dev.lock` regenerated with
`scripts/dev/lock.sh`; `.importlinter`; `layout/test_import_layers.py` with its exact membership snapshot;
ignore-key source, baseline and registry row; `.gitignore`. The CLI `Source` type gains a callable
key form next to `InCode` (about 10 lines, `_baseline_sources.py:105`). The layer source emits
ignore keys only. `gates.sh` gains the isolated lock-synced `.venv-layers` step, and the sentry
step explicitly ignores the layer wrapper. The same diff drops `(target)` from the three C1 layer
paths because it creates them. The function-local import fails if import-linter is absent when the
test runs. `test_sentry_manifest.py` adds the ordinary-versus-isolated route partition assertions.
AT-5, AT-6.

Full-suite triggers: E3 and C3 change a dependency lock, so each runs the full suite (T3) on its
merge head, diffed against the saved main baseline. Engine Mac and VM T3 first run the isolated
`layers` gate on that head, then run the one full suite in the certified venv with
`--ignore=tests/sentry/layout/test_import_layers.py`; CLI T3 on the devbox follows the same two-step shape
with its two lock-synced venvs. The ignored test is not lost coverage because the immediately prior
layer step runs it on the same head. E1, E2, C1, C2 run T1 plus the gate set only; E2 and C2 touch
no shared fixture or test config (helpers under `tests/sentry/` are not `conftest.py`).

Build now, given the in-flight list: E1 and C1. Then E2 when CI-E1 and SUB-STOP merge; C2 when E2
and LIB L1b.1 merge; E3 after CI-E1; C3 after C2.

## 10. File-size table

Hand-written lines; vendored and generated files named separately.

| File | Repo | Before | After (about) |
|---|---|---:|---:|
| `CODEMAP.md` | engine | 233 | 300 |
| `CODEMAP.md` | CLI | 83 | 140 |
| `tests/sentry/layout/__init__.py` (new) | both | 0 | about 15 each |
| `tests/sentry/layout/_layout.py` (new) | engine | 0 | 110 |
| `tests/sentry/layout/_layout.py` (vendored) | CLI | 0 | 110 (copy) |
| `tests/sentry/layout/_ratchet_cases.py` (new and vendored) | both | 0 | about 20 each |
| `tests/sentry/_ratchet.py` | engine, CLI | existing | about +10 each |
| `tests/sentry/test_package_map_paths.py` | engine | 31 | 45 |
| `tests/sentry/test_module_size.py` | engine | 262 | 225 |
| `tests/sentry/layout/test_module_size.py` (new) | CLI | 0 | 90 |
| `tests/sentry/layout/test_package_fanout.py` (new) | each | 0 | 100 |
| `tests/sentry/layout/test_import_layers.py` (new) | each | 0 | 80 |
| `tests/sentry/layout/test_vendored_files.py` (new) | each | 0 | 30 |
| `tests/sentry/layout/test_codemap_paths.py` (new) | CLI | 0 | 25 |
| `tests/sentry/_baseline_sources.py` | engine | 250 | 265 |
| `tests/sentry/_baseline_sources.py` | CLI | 189 | 215 |
| `scripts/dev/gates.sh` | engine, CLI | existing | about +25 each for the isolated `layers` route |
| `tests/sentry/test_sentry_manifest.py` | engine, CLI | 125, 125 | about 145 each |
| `scripts/dev/sentry_registry.toml` | engine, CLI | 85, 15 | 93, 27 |
| `.importlinter` (new) | engine, CLI | 0 | 50, 35 |
| `pyproject.toml` | engine, CLI | 1063, 256 | engine stays at or below its build-base count while adding `layers`; CLI adds one line |
| `ci.yml` | engine | after CI-E1 | adds isolated `layers-gate`; certified profile unchanged |
| baselines JSON, `uv.lock`, `requirements-dev.lock` | both | generated | generated |

Every new hand-written file stays under its repo's cap. The already-over engine `pyproject.toml`
does not grow, and `test_module_size.py` shrinks.

## 11. Design notes

- Principles: single source of truth for the census policy (one `ratchet_verdict` instead of a
  copy per check), open/closed for checks (a new census is a new call site with its own limit,
  not a new ratchet). Change pain removed: today a CLI size rule would be a second copy of the
  engine's verdict, and a fan-out rule a third.
- Pattern followed: import-linter layered contracts with a ratcheted ignore list (its documented
  legacy-adoption path); the X0a baseline registry for growth; the X0a-h sha pin for vendoring.
- Where each fact lives: limits in each repo's test (`GOAL`, `MAX`, `LIMIT`); allowlist values in
  the test constant (exact counts); allowlist keys mirrored in the baseline JSON (the ratchet's
  copy, guarded for agreement), with numeric `max` on the same entry; layer definitions and ignores
  in `.importlinter`; exact layer membership review pin in `layout/test_import_layers.py`, guarded for
  equality with the config; ignore keys only in the registered X0a baseline; the shared ratchet
  value-case table in sha-pinned `_ratchet_cases.py`; "where a new thing goes" in CODEMAP; per-package
  description in `_MAP.md` (engine). None is unguardedly restated.
- House style: pure verdict functions, typed returns, no flag arguments (goal, limit, message
  noun, allowlist name and fix advice are values, not switches), comments explain why.
- Owner question 4.3 asked of each piece: the census function (does what `_size_verdict` does,
  so it is that function); fan-out (no existing check); import layers (overlaps
  `test_public_import_boundary.py` and the CLI internal-boundary sentry, handled by D3 and D4);
  map path check (reuses the `_MAP.md` check, D5); baseline reader (reuses X0a sources).

## 12. Pre-mortem (failure modes)

- A slice merges before E2 or C2 and grows a frozen package: no harm, the freeze records the
  count on its build base.
- CLI-FMT adds `src/decoy/cli/io/_targets.py`, never `cli/_targets.py`, because the Q4 convention
  applies now even before C2. C1 names `src/decoy/cli/_sources.py` as today's exception and H-1
  moves it to `src/decoy/cli/io/_sources.py`.
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
| 9 | Behavior-preserving refactor drift | Yes: E2 moves `_size_verdict` into `_layout.py` | the existing branch table stays the characterization test | AT-2: unchanged `test_size_verdict_branches` plus byte-identical size messages; mutation drops one branch, red |
| 10 | Frozen contract drift | N/A: no product contract changes; see section 12c | none | none |

## 12b. Enumerations

### Exit paths

- N/A: the guards own no files or state. The only writers are the meta-tests under `tmp_path`
  (`test_module_size.py:256` is the existing pattern).

### Routes (where the guards run)

- Local gate sentry step: engine `scripts/dev/gates.sh:142`, CLI `scripts/dev/gates.sh:91` runs
  `ratchet_check.py`, then pytest over `SENTRIES` with
  `tests/sentry/layout/test_import_layers.py` explicitly ignored.
- Local layer step: both `gates.sh` scripts sync `.venv-layers` from the repo lock and run only
  `tests/sentry/layout/test_import_layers.py`; ordinary changed-test selection filters that path.
- Engine GitHub CI: after CI-E1, a dedicated `layers-gate` syncs `dev+layers` into
  `.venv-layers` and runs only the layer wrapper. Ordinary sentry routes ignore that wrapper. The
  certified `dev+lint+vault+cloud` job is unchanged.
- Engine Mac and VM T3 and CLI devbox T3: run the layer step first, then the single full suite on
  the same head with the layer wrapper ignored in the ordinary venv.
- Manifest: `tests/sentry/test_sentry_manifest.py:20` (engine), `:24` (CLI); a sentry file not
  listed at any depth fails `test_manifest_lists_every_sentry_file_and_no_ghost`.
- Registry mirror: `tests/sentry/test_baseline_registry.py:26` (`LIST_NAME`), sources at
  `tests/sentry/_baseline_sources.py:100` (engine) and `:120` (CLI).
- T1 selection: non-layer guard tests are always selected; the default isolated `layers` step
  always runs the layer wrapper (`testing.md` T1).

### FK shapes

- N/A: no data path is touched; LAYOUT reads source files and config only.

### Fields

- N/A: no public result type is added or changed. The new constants are test-local:
  `FANOUT_ALLOWLIST`, CLI `SIZE_ALLOWLIST`, each `dict[str, int]` keyed by repo-relative path.
  Registered numeric baseline entries add `max: int`; layer baseline keys use only the existing
  exact ignore-key form. Exact tier membership is a snapshot in `layout/test_import_layers.py` and is not
  an X0a entry.

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

No product contract is touched. No engine public surface, CLI command, flag, exit code, file
format or reason code changes. The internal X0a baseline entry schema gains optional `max`; old
key-only baselines retain their behavior, and a meta-test covers both forms.

## 13. Follow-ups (recorded, not in scope)

- FU-1: fold `test_public_import_boundary.py` into an import-linter `forbidden` contract once the
  baseline key change can be reviewed as a replacement.
- FU-2: shrink engine tier 3 (split plan below generation and transforms) as H-1 moves land.
- FU-3: move `execution/_errors.py` and the other foundation-in-execution modules down (H-1 E8).
- FU-4: a leaf version module in each repo, removing the 7 + 8 `__version__` ignore lines.
- FU-5: H-1 C0 keeps only the pure-move checker port; its size census half is C2 here.
- FU-6: retire or re-approve exception `ARCH-LAYERS-D3` if import-linter gains coverage of the
  CLI boundary sentry's dynamic imports, literal `getattr` and attribute chains.

## 14. Owner questions (answered by Cam, 2026-11-06)

- Q1 answered: yes. The sub-package names in section 4 are the targets, and H-1 E8 keeps them (this also answers H-1 Q3).
- Q2 answered: freeze. `execution/_strategies/` is frozen at today's count, and the next strategy starts a family sub-package.
- Q3 answered: not in LAYOUT, but recorded as follow-up LAYOUT-T (test-folder fan-out and layout). Owner: "clear organized folder structure is important." The map lists today's large test folders as exceptions, and LAYOUT-T is a roadmap row.
- Q4 answered: yes, and the naming conventions apply now. Owner: "start new proper naming conventions now, let's not introduce more technical debt." New CLI source-reader and target-writer modules go in `src/decoy/cli/io/` starting today, including CLI-FMT's target writer (`cli/io/_targets.py`, not `cli/_targets.py`), even before C2's freeze lands. The C1 map states this rule, and the CLI-FMT handoff is updated to match.
