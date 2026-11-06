# VALUE-FREE-ERRORS and CLASS-SWEEP: value-free config errors and a recurring-defect sweep

Status: plan (2026-11-06, not built). Covers engine and CLI; lives in the engine repo.

Revision note r2 (Codex applying Dennis r1 and owner Q1, 2026-11-06).

Rules consulted: `~/dev-rules/README.md` (routing table), `00-universal.md`,
`risk-and-exceptions.md`, `development-loop.md`, `workspace-and-vcs.md`, `reuse-first.md`,
`exploration.md`, `scope-discipline.md`, `verification.md`, `code-review.md`,
`cross-cutting-concerns.md`, `architecture.md`, `testing.md`, `security.md`,
`documentation.md`, `catalogs/recurring-defects.md` (all classes). Also read: vault
`handoffs/LOCAL-BUILDER-BRIEF.md`; owner priorities `v1-pragmatic-gate-priorities` (working V1
first, block only on cascading design, 800 to 900 line files are V2-LOG); LAYOUT plan
(`origin/plan/layout`, `docs/plans/2026-11-06-layout-map-and-guards.md`); engine `origin/main`
`55876436` and CLI `origin/main` `5bd60b9`; in-flight branches `feat/csv-source-reader`,
`feat/run-result-model`, `fix/ci-e1-frozen-installs`, `fix/value-free-discovery-subset-errors`,
CLI `fix/ci-l1-cli-ci`.

Risk: R2. Error text and test guards; no data path changes. Part A3 rides an engine repin (lock
change, full-suite trigger). Platform is parked: platform touch points are follow-ups only.

## 1. Guarantee and non-goals

Part A (VALUE-FREE-ERRORS) promises:

1. One engine function turns a Pydantic `ValidationError` from a public model into value-free
   structural problems: `ConfigProblem(code, location, text)`. `location` joins the original
   `loc` segments and keeps config keys, source/table/column names, file names, path patterns,
   regexes, list indexes and discriminator names. Built-in structural error text comes from a fixed
   table keyed by error type. Certified name-only or fixed validator text remains actionable;
   `input`, `ctx`, the `Input tag` echo, and validator text that can interpolate a data value, data
   literal or secret are dropped. The raised config-route error has `__cause__ is None` and
   `__context__ is None`.
2. `validate_config`, `override_sources` and a new public `parse_pipeline_config` are the engine
   routes, and every CLI site that validates an engine public model goes through
   `parse_pipeline_config`. No CLI output reads `ValidationError.errors()`, `.json()` or `str()`.
3. Config validators that interpolate data values, data literals or secrets use a
   `PydanticCustomError` with a registered reason code and fixed safe summary. Name-only and
   fixed-text validators stay as they are, and their structural text survives. The sentry pins
   each existing dynamic name-only message by `(path, qualified function, normalized AST digest)`
   and rejects a new, changed, value-bearing or unclassified message before it can reach the
   converter.
4. A guard derives the public models from `decoy_engine.__all__` and the CLI routes from the Click
   command tree, never from a handwritten list.

Part B (CLASS-SWEEP) promises, in both repos: no `$`-anchored whole-value regex (class 1), a
shrink-only ratchet at the base's regenerated count for `raise ... from None` inside an `except`
block (class 3), and the two root-private facade fixes. The class-3 ratchet lands now; only config
routes must prove `__context__ is None`. The bulk class-3 hoists and the class-6 private-module
contract are V2 corrective work.

Non-goals (a finding here is a follow-up, not a blocker):

- Value-free text for errors that do not come from a Pydantic model (expression parser, readers,
  connectors). `fix/value-free-discovery-subset-errors` and F31 own those.
- `raise X from exc` chains on non-config errors. Class 3 is the `from None` miss only.
- Private *names* imported from a public module (`reason_adapter._CODED`). Class 6 is modules.
- Fixing the 480-odd existing class-6 edges. They go in the ratchet; H-1 shrinks them.
- Hiding structural names or adding ordinal placeholders. Owner Q1 requires names everywhere.
- Platform: `api/jobs/v2_runner.py` and its 422 handler read `errors()`; follow-up FU-1.

## 2. Measured today

Pydantic 2.13.4 (engine lock). A scratch model with `hide_input_in_errors=True` and
`extra="forbid"` still renders, in both `str(exc)` and `errors(include_input=False,
include_context=False, include_url=False)`: the mapping key (`sources.customers_prod.a.n`), the
forbidden key (`sources.customers_prod.a.zz_SECRETK`, `SECRET_TOP`), the discriminator echo
(`Input tag 'SECRET_TAG' found using 'type'`) and the validator text (`Value error, bad n 7
SECRETV`). `typing.get_args(pydantic_core.core_schema.ErrorType)` exposes 104 built-in error types.
`PydanticCustomError` codes are outside that `Literal` and need separate handling.

Engine config validators (recounted on `origin/feat/csv-source-reader`): 53
`raise ValueError`/`PydanticCustomError` sites in `config/`. Of the 42 dynamic message sites, 42
interpolate names or other permitted structure and 0 interpolate a data value, data literal or
secret. One additional site passes nested `err["msg"]` as non-literal text and is treated as
potentially value-bearing until converted to its registered reason summary. A2 regenerates this
split on its base and migrates only the value-bearing or unclassified sites; the 42 name-only
sites stay unchanged.

CLI sites (walk over `src/decoy`, 13): `cli/validate.py:277,462` and `cli/preflight.py:721` loop
`errors()` into output; `cli/subset.py:181` prints the first error's loc and msg; `cli/run.py:684`,
`cli/tokenize.py:243,540`, `api.py:446` wrap `str(exc)`; `cli/preflight.py:454`,
`cli/demo.py:226`, `cli/init.py:320`, `cli/report.py:1777,2047` call `model_validate` uncaught,
so the top-level handler prints whatever escapes. `ci/manifest.py:181` validates the CLI-owned
`CiManifest` and reads only `type`.

Class census (AST walk, planning only; each part regenerates on its own base):

| Class | Engine | CLI |
|---|---|---|
| 1 `$` in a Python `re` pattern | 15 (`checksums.py:86`, `connectors/postgres_snapshot.py:73,111,112`, `execution/_chunked_fk_dtype_safety.py:79,87,96`, `execution/_chunked_fk_vocab.py:36`, `execution/native/_requirements.py:88`, `execution/out_of_core/_duckdb.py:22`, `internal/pandas_compat.py:19`, `profiling/sentinels.py:87`, `reasons/_record.py:40`, `secret_providers/_types.py:43,44`) | 6 (`ci/contracts.py:124,125,484`, `ci/rule_codes.py:27`, `ci/staged.py:46`, `cli/_duckdb_source.py:32`) |
| 1 `$` in a Pydantic `pattern=` | 3; safe: pydantic-core's default Rust engine anchors `$` at end of text (checked: `"a\n"` rejected; `regex_engine="python-re"` accepts it) | 1 (`ci/contracts.py:465`), same |
| 3 `from None` inside `except` | 52 in 27 files (8 `connectors/postgres_snapshot.py`, 6 `io/bounded_reader.py`, ...) | 21 in 11 files (7 `ci/manifest.py`) |
| 6 private module of another top-level package | 487 module edges (117 `execution -> plan`, 79 `execution -> generation`, ...) | 0 |
| 6 root private with a public facade | 2 (`telemetry/_vocab.py:16`, `telemetry/_recorder.py:20` import `_progress_events`; `decoy_engine.progress` re-exports it and its docstring says "import from there") | 0 |

Class 1 sites that reach SQL text are the real risk: `postgres_snapshot._IDENT_RE` and CLI
`_duckdb_source._IDENT_RE` accept `"name\n"`. CLI `_GLOB_CHARS` (`[^\x00-\x1f...]+$`) accepts a
trailing newline the class itself excludes.

## 3. Established solutions

Search run: Pydantic and pydantic-core docs and source for error rendering options; web search
"pydantic v2 ValidationError hide_input_in_errors loc dict keys redact"; import-linter 2.15
source (`contracts/protected.py`, `domain/fields.py`); Python `re` and PEP 409/415, PEP 675;
OWASP Error Handling cheat sheet and CWE-209; the repo's own reason registry, `safe_output`,
`config/_declared_casting.py` and CLI `cli/_yaml_errors.py`.

What Pydantic covers, and what it does not:

| Option | Covers | Does not cover |
|---|---|---|
| `ConfigDict(hide_input_in_errors=True)` | drops `input_value=` from `str(exc)` and `input` from `errors()`; set on the outermost model or adapter (nested settings are not consulted, as the MCP SDK PR #2346 found) | `loc` keys, extra-key names, `msg` text built from input |
| `errors(include_input=False)` | drops `input` | same |
| `errors(include_context=False)` | drops `ctx` (the raw `ValueError`, the `tag`) | `msg` is already rendered with them |
| `errors(include_url=False)`, `PYDANTIC_ERRORS_INCLUDE_URL=0` | drops the docs URL (static, not value-bearing; noise only) | anything else |
| `PydanticCustomError(type, template, ctx)` | a fixed type and a template the author controls; already used by F06 (`_declared_casting.py:29`) with a registered reason code as the type | messages built with f-strings before the call |
| `regex_engine` | Rust default makes `pattern=` `$` safe | Python `re` elsewhere |

No Pydantic setting rewrites `loc`; the web search found only hand-written formatters over
`errors()` (FastAPI discussions, NVIDIA OSMO #891). The design follows the OWASP and CWE-209
pattern (a generic message plus a stable code, never reflected input) and the OpenTelemetry
`error.type` pattern the repo already uses in `safe_output/_exception.py`.

Followed sources: the converter drives its built-in text table from the public
`typing.get_args(pydantic_core.core_schema.ErrorType)`, so a Pydantic upgrade that adds a type
fails a guard instead of leaking. A `PydanticCustomError` type is accepted only when it is a
registered reason code; otherwise the converter emits a generic row. A migrated validator uses
the reason registry's fixed safe summary, so it cannot format a value into output. Class 1 uses
`re.fullmatch` or `\Z`
(Python `re` docs); the sentry finds `$` with the stdlib regex parser (`re._parser` on 3.11+,
`sre_parse` on 3.10), which yields `AT_END` for `$` and `AT_END_STRING` for `\Z`, so `$` inside a
class or escaped is not flagged. Class 3 follows PEP 415: `from None` only sets
`__suppress_context__`; `__context__` stays. Class 6 uses import-linter, which LAYOUT adopts.

Custom code that remains: joining `loc`, the text table and registered-custom-error handling
(about 120 lines). No library offers the value-free rendering policy.

## 4. Design notes

- **Deep module.** One converter, `safe_output.validation_problems(exc)`, hides
  the error-type table and registered-custom-error policy. Callers get a tuple of
  `ConfigProblem`. Change pain removed: today 13 CLI sites and 2 engine routes each decide how
  much of a `ValidationError` to show; the next command copies whichever it sees first.
- **Open/closed.** The text policy table has one row per Pydantic error type, derived from the
  library catalog. Rows select fixed text or the sentry-certified validator-message policy. A new
  error type is a row, not a branch.
- **Placement by layer.** The converter is generic over Pydantic models, so it sits in
  `safe_output/` (LAYOUT tier 5, under `config`). It reads custom-error codes through the public
  reason-registry API, without importing `config`.
- **One fact, one place.** Value-free text for reason-coded errors is the registered reason's
  safe summary (`reasons.get(code)`), never the raise-site message. The YAML parse text moves
  from CLI `cli/_yaml_errors.py` to the engine (`safe_output.yaml_error_text`), and the CLI name
  becomes a re-export, so one copy remains.
- **One ratchet mechanism.** `validation-name-messages.json`, class-3 counts and the later B3 keys
  use the existing `_ratchet.py`, `_baseline_sources.py` and registry flow. This plan adds baseline
  kinds, not another reader, comparison rule or update command.
- **House style.** `ConfigProblem` is a frozen dataclass (typed context object). No flag
  arguments: the converter has one mode.
- **Class 6 and LAYOUT, V2 corrective.** LAYOUT's `layers` contract checks tier direction only. Siblings in a
  tier, and any downward import, may name a private module, and import-linter wildcards replace
  whole segments only (`_*` is refused by `ModuleExpressionField`), so no built-in contract can
  say "no `_private` module of another package". The rule therefore rides LAYOUT's mechanism as
  a second contract in the same `.importlinter`: a custom contract type (import-linter's
  documented `contract_types` hook) over the same grimp graph, run by the same
  `layout/test_import_layers.py` wrapper. The custom contract reads its ratchet keys from the same
  `import-layers.json` baseline through `_layout.ignore_keys`; it does not generate hundreds of
  INI ignore lines. LAYOUT's exact config-membership snapshot gains the second contract. The
  wrapper proves `tests.sentry.layout._private_contract` is importable in `.venv-layers` and fails,
  rather than skips, when it is not. Both vendored copies of `_private_contract.py` join the one
  sha256 pin table. One tool, one config, one gate step, no second guard. The root facade fixes
  (`_progress_events` behind `progress` and `reasons._record` behind `reasons`) stay in V1 B1.
- **Class 6 scope.** "Another package" means another top-level sub-package. Root-level `_x.py`
  modules are LAYOUT's mapped "foundation private leaves" (engine) and "private cross-command
  helpers" (CLI), importable across the package by design, except a root private with a public
  facade.

## 5. Placement

LAYOUT E1 adds the CODEMAP "Where things go" section. Rows cited are from that draft; two rows are
map changes this plan makes.

| New file | Directory | Map row | Sits beside |
|---|---|---|---|
| `safe_output/_validation.py` | `src/decoy_engine/safe_output/` | map change: add row "Safe output boundary: `src/decoy_engine/safe_output/`, by output concern" | `safe_output/_exception.py` |
| `config/_parse.py` | `src/decoy_engine/config/` | "Config models" row | `config/_errors.py` |
| `tests/security/test_config_error_text.py` | `tests/security/` | existing security-test location (CODEMAP line 126) | `test_reason_boundary.py` |
| `tests/sentry/defects/__init__.py`, `_defect_scan.py`, `test_anchored_regex.py`, `test_exception_context.py`, `test_validation_sites.py` | `tests/sentry/defects/` | map change: "Sentry test" row gains "recurring-defect class rules in `tests/sentry/defects/`" (engine `tests/sentry/` holds 56 entries, over the 25 cap) | `tests/sentry/layout/` |
| `validation-name-messages.json` | `tests/sentry/baselines/` | "Sentry test" baseline row | existing ratchet baselines |
| `tests/unit/config/test_validation_messages.py` | `tests/unit/config/` | "Config models" row | `test_csv_dialect_rules.py` |
| CLI `tests/sentry/defects/` (same three tests, `_defect_scan.py` vendored) plus `test_config_error_routes.py` | CLI `tests/sentry/defects/` | same map change in CLI CODEMAP | CLI `tests/sentry/layout/` |
| V2 B3 `tests/sentry/layout/_private_contract.py` (both repos, vendored) | `tests/sentry/layout/` | "Layer contracts" row | `layout/_layout.py` |

No new product package; no package crosses its fan-out cap (`safe_output/` goes from 3 to 4).

## 6. Choke points, guards and red mutations

| Rule | Choke point | Guard (walks) | Red mutation |
|---|---|---|---|
| ValidationError text is value-free | `safe_output.validation_problems`; routes `parse_pipeline_config`, `validate_config`, `override_sources` | `tests/security/test_config_error_text.py`: for every `BaseModel` in `decoy_engine.__all__`, use model-field annotations and seeded fixtures to trigger marker mapping keys, extra keys, discriminator tags, strings and wrong types, then search `text`, `code`, `str()`, `repr()` and the chain for value markers while asserting structural names remain in `location` | drop `input` filtering: red; keep the `union_tag_invalid` template: red; pass a value-bearing validator message through: red; raise inside the `except`: red |
| Every Pydantic error type is classified | text policy table in `_validation.py` | same file: `set(get_args(core_schema.ErrorType))` equals the built-in table keys; only `value_error` may use a sentry-certified name-only or fixed message; registered `PydanticCustomError` gets its reason summary and an unregistered custom type gets the generic row | delete the `extra_forbidden` row: red; let another built-in type trust `msg`: red; trust an unregistered custom template: red |
| No raw ValidationError reaches output (engine) | the three routes | `defects/test_validation_sites.py`: AST walk of `src/decoy_engine`, aliases resolved (class 5), flags `.model_validate*`/`TypeAdapter.validate_*` on a public model outside the routes, and any `.errors(`, `.json(`, `str()`, f-string or `%` of a name bound by import source to `pydantic.ValidationError` or `pydantic_core.ValidationError` outside `_validation.py`; internal re-validation of already-checked dicts (`execution/_transforms_table.py:129`, `profile/_fixed_width_reader.py:129`) is a reasoned ratchet entry | add `PipelineConfig.model_validate(x)` with `str(exc)` in a scratch module: red; aliases of both Pydantic classes: red; `decoy_engine.errors.ValidationError`: green |
| Value-bearing validator messages are coded | the reason registry; existing name-only messages are the shrink-only `validation-name-messages.json` snapshot | `test_validation_sites.py` classifies dynamic messages into value-bearing, name-only and unclassified; value-bearing and unclassified must be zero after A2; every permitted dynamic message must match its `(path, qualified function, normalized AST digest)` baseline key | add `raise ValueError(f"bad value {value}")`: red; change an approved name-only expression: red; an unchanged table-name message: green |
| CLI routes use the engine choke point | `decoy_engine.parse_pipeline_config` | CLI `defects/test_config_error_routes.py`: AST walk with the same flags over `src/decoy` (public models read from the installed engine's `__all__`); plus a route cell that walks the Click tree, invokes each command with a config-path parameter on a marker config, and searches stdout, stderr and `--json` | revert `validate.py` to the `errors()` loop: red; add a new command calling `model_validate`: red |
| Class 1 | `re.fullmatch` or `\Z` | `defects/test_anchored_regex.py`: AST walk resolves `re` aliases and module-level pattern constants, parses each pattern, flags `AT_END`; asserts no model sets `regex_engine="python-re"` | restore `$` in `postgres_snapshot._IDENT_RE`: red; `import re as r; r.compile("x$")`: red |
| Class 3 | shrink-only ratchet, plus config-route hoists | `defects/test_exception_context.py`: AST walk counts `raise ... from None` lexically inside an `ExceptHandler`; pin is regenerated on the part base and may only fall | add one `from None` anywhere: red; restore one config-route raise inside its handler: AT-A5 red |
| Class 6 (cross-package private, V2 B3) | `.importlinter` contract `private_modules` | `layout/test_import_layers.py` (LAYOUT), after E3/C3 and H-1 | add `from decoy_engine.plan._top_code import x` in `kernel/`: red; delete one baseline key: red |
| Class 6 (facade) | built-in `protected` contract on `decoy_engine._progress_events`, allowed `decoy_engine.progress` | same | restore the telemetry import: red |

Every sentry is seeded-bypass tested (a planted violation in a scratch tree turns it red, removing
a real site lowers the count), registered in `SENTRIES` and `scripts/dev/sentry_registry.toml`,
and runs in the `gates.sh` sentry step. Ratchets use the X0a baseline and never grow (class 11).
A forwarding helper on any allowlist has its callers checked by `(path, name)` (class 10).

## 7. Acceptance tests

Each test names the mutation that turns it red. Red-first evidence is an assertion failure.

- AT-A1 (location): `sources: {customers_prod: {type: file, format: csv, path: 1}}` gives
  `location == "sources.customers_prod.file.path"`; the structural source name remains, while a
  marker supplied as the bad path value appears nowhere. Mutation: replace a `loc` name with an
  ordinal, or retain the bad input value.
- AT-A2 (extra key): an unknown key `zz_marker` at the root and under a table gives
  `zz_marker` and `tables[0].zz_marker`, code `extra_forbidden`, but never its supplied value.
  Mutation: retain the extra value.
- AT-A3 (tag): `type: marker_tag` under `sources` gives code `union_tag_invalid`, text naming
  `discriminator` and `expected_tags` only. Mutation: render the library template.
- AT-A4 (validator text): a name-only `ValueError` keeps its structural text; a planted
  value-bearing `ValueError` is rejected by the sentry; a `PydanticCustomError` with a registered
  reason code gives that reason's safe summary, not its raise-site text. Mutation: classify a data
  value as a name, or trust the registered error's custom template.
- AT-A5 (chain): for every route, `__cause__ is None`, `__context__ is None`, and
  `traceback.format_exception` of the error holds no marker. Mutation: `raise ... from exc`.
- AT-A6 (public-model matrix): section 6 row 1 over every public model; also asserts the observed
  error types include `missing`, `extra_forbidden`, `union_tag_invalid`, `literal_error`,
  `value_error`, `string_type`, so a fixture matrix that triggers nothing cannot pass. Mutation:
  drop the extra-key fixture; the coverage assert goes red.
- AT-A7 (YAML): `validate_config` on a file whose bad line holds a marker reports line and column
  only. Mutation: `f"Failed to parse YAML: {e}"`.
- AT-A8 (CLI): `decoy validate`, `validate config`, `preflight`, `subset`, `run`, `tokenize`,
  `report`, `demo`, `init` and `decoy.api.mask` on the marker config: no marker in any output,
  exit `EXIT_USAGE` unchanged, `--json` keeps `code`, `message`, `location` keys. Mutation: one
  site reverted to `str(exc)`.
- AT-A9 (no regression): every existing CLI E2E that asserts a validation message passes or is
  updated to the structural form in the same diff, with each change listed in the build record
  (an assertion may change text, never be deleted or loosened to `in`).
- AT-B1 (class 1): each fixed validator refuses `value + "\n"` (identifier, version, OID, token,
  hex, glob). Mutation: restore `$` in one.
- AT-B2 (class 3): each config route raises with `__context__ is None`; the repo-wide sentry pin is
  regenerated on the base and cannot grow. Mutation: re-add one `from None` anywhere, or move one
  config-route raise back into its handler.
- AT-B3 (class 6 facade, V1): `telemetry` imports `decoy_engine.progress` and callers use the public
  `decoy_engine.reasons` facade; behavior tests stay unchanged. Mutation: restore either private
  import; the existing boundary checks go red.
- AT-B4 (class 6 contract, V2): the LAYOUT membership snapshot includes `private_modules`, the
  wrapper imports the contract in `.venv-layers`, ignore keys come from `import-layers.json`, and
  both vendored files are sha256-pinned. Mutations: remove the test-path importability setup,
  restore an INI ignore line, or alter one vendored byte; red.

Mutations rerun on the final head and the head is recorded (class 9).

## 8. Parts (slices)

Each part: Conventional Commits subject at most 72 characters, final line `cam`. Test depth T0 per
edit, T1 per commit, T2 at gates; full suite only where section 9 lists a trigger. Counts and
baselines are regenerated on the part's base, never copied from this plan.

| Part | Repo | Branch | Base | Depends on | Hand-written lines |
|---|---|---|---|---|---:|
| A1 converter | engine | `feat/value-free-errors-a1` | engine main | CLI-DIALECT CSV reader, RESULT E1 and LAYOUT E1 merged | ~500 |
| A2 rule errors | engine | `feat/value-free-errors-a2` | engine main after A1 | A1 | ~120 |
| A3 CLI routes | CLI | `feat/value-free-errors-a3` | CLI main after repin | engine release with A2; CLI repin; CI-L1 and LAYOUT C1 merged | ~450 |
| B1 engine V1 sweep and ratchet | engine | `fix/class-sweep-b1` | engine main | CSV reader, RESULT E1, CI-E1, LAYOUT E1 and `fix/value-free-discovery-subset-errors` merged | ~300 |
| B2 CLI V1 sweep and ratchet | CLI | `fix/class-sweep-b2` | CLI main | B1 merged (vendor source); CI-L1 and LAYOUT C1 merged | ~240 |
| B3 private contract, V2 corrective | engine, CLI | `feat/class-sweep-b3-engine`, `feat/class-sweep-b3-cli` | main after LAYOUT E3 / C3 and H-1 | B1 / B2; LAYOUT E3 / C3; H-1 settled | ~170 each, no generated INI ignores |

**A1.** `safe_output/_validation.py`: `ConfigProblem`, `validation_problems`, the type table,
direct `loc` joining, `yaml_error_text`; exports from `decoy_engine.safe_output` and the
root. `config/_errors.py`: `PipelineConfigError` gains
`problems` and stops setting `__cause__` (docstring updated). The placed `config/_parse.py` owns
`parse_pipeline_config(raw) -> PipelineConfig`; `config/__init__.py` only re-exports it.
`validation/_config.py` and `config/_override.py` route through it.
`tests/security/test_config_error_text.py`, `defects/test_validation_sites.py` (validator-message
classes and the name-only AST snapshot are regenerated and reviewed on the part base). CHANGELOG and
`docs/security/error-reporting-and-data-exposure.md` gain the promise. Builds once CSV, RESULT E1
and LAYOUT E1 merge (the first two touch `__init__.py`,
`validation/_config.py`, `config/_sources.py`, and the sentry manifest and registry).

**A2.** Regenerate the validator census on the part base and classify each dynamic message as
value-bearing, name-only or unclassified. Migrate only value-bearing and unclassified sites to an
existing registered reason code with its fixed safe summary. The CSV-branch recount
is 0 value-bearing, 42 name-only and 1 unclassified nested `err["msg"]`; the 42 name-only sites
stay as they are and enter the shrink-only normalized-AST snapshot. No new reason codes. The
value-bearing/unclassified ratchet falls to 0.
`config/_tables.py` (452 lines) and `config/_pipeline.py` (330) do not grow. Release the engine
right after A2 (owner standing authority for engine releases).

**A3.** Every site in section 2 calls `parse_pipeline_config` and renders `problems`. `cli/run.py`
(1805) and `cli/report.py` (2382) only shrink. `cli/_yaml_errors.sanitize_yaml_error` becomes a
re-export of the engine function (its 23 callers and `test_yaml_error_leak_isolation.py` keep
working). `ci/manifest.py` stays on its own codes as a reasoned allowlist entry (reads `type`
only). New `defects/test_config_error_routes.py`. Repin follows the engine repin checklist
(compat, fences, FROZEN, doc, `PIN_PREFIX`, Dockerfile ARG, catalog).

**B1.** Class 1 fixes (count regenerated on base, sites move to `fullmatch` or `\Z`); class 3
`test_exception_context.py` lands as a shrink-only ratchet at the base's regenerated count; class
6 facade fixes (`telemetry` imports from
`decoy_engine.progress`; `_progress_events` and `safe_output/_event.py` read `CleanupState`,
`PublicationState`, `COUNT_KEYS`, `MAX_COUNTS` and an `is_count` from the public
`decoy_engine.reasons` instead of `reasons._record`). Sentries `test_anchored_regex.py` and
`test_exception_context.py` with `_defect_scan.py` (normalized per class 5). If LAYOUT E3 merged
first, the same diff moves `telemetry` into the `progress` tier in `.importlinter` and its
snapshot (telemetry has no importers in `src/`). B1 and A1 can build in parallel worktrees; the
second to merge rebases the `SENTRIES` and registry rows.

**B2.** CLI class 1 sites move to `fullmatch` or `\Z`; class 3 gets the same shrink-only ratchet at
the base's regenerated count. Vendored `_defect_scan.py` comes from the B1 merge commit and is
sha-pinned with the repo's existing vendored-file pin (LAYOUT C2's
`test_vendored_files.py` if merged, else CI-L1's `ci/vendored.toml`; no third mechanism). Every
rule of the engine scanner is kept (class 5).

**B3, V2 corrective after LAYOUT E3/C3 and H-1.** `layout/_private_contract.py` (custom
import-linter contract, about 100 lines, vendored to the CLI), a `private_modules` stanza and the
`protected` stanza in each `.importlinter`. Extend LAYOUT's exact config-membership snapshot for
the second contract. Make the dotted contract path importable in `.venv-layers` and assert a hard
failure when it is not. Store generated ratchet keys only in `import-layers.json`, registered
through LAYOUT's `ignore_keys`, not as INI ignore lines. Add both copies to LAYOUT's existing
vendored-file sha256 pin table. CLI starts with an empty ratchet list.

## 9. Test scope and in-flight sequencing

| In-flight | Touches | Parts that wait |
|---|---|---|
| CLI-DIALECT CSV reader (engine `feat/csv-source-reader`) | `validation/_config.py`, `config/_sources.py`, `config/_csv_dialect.py`, `__init__.py`, sentry manifest and registry | A1, A2, B1 |
| RESULT E1 (`feat/run-result-model`) | `reasons/_record.py` (class 1 site at :40), `__init__.py`, sentry manifest | A1, B1 |
| CI-E1 (`fix/ci-e1-frozen-installs`) | `test_sentry_manifest.py`, `test_module_size.py`, CI files | B1 (A1 if it lands first) |
| CI-L1 (CLI `fix/ci-l1-cli-ci`) | CLI `tests/sentry/`, `ci/` | A3, B2 |
| `fix/value-free-discovery-subset-errors` (not in the brief, found in flight) | `data_discovery.py`, `subset/_seed.py`, `tests/security/test_value_free_errors.py`, adds class-3 sites | B1 waits, then regenerates its pin |
| LAYOUT E1 / E3, C1 / C3 | CODEMAP map, `.importlinter` | A1 and B1 wait for E1; A3 and B2 wait for C1; V2 B3 waits for E3/C3 and H-1 |

Full-suite triggers: A1 (serialization of a public error, frozen-contract adjacent), A3 (version
pin and lock), B1 and B2 (shared sentry config). A2 runs T1 plus the config and CLI-facing suites.

## 10. File sizes

| File | Today | After |
|---|---:|---:|
| `src/decoy_engine/safe_output/_validation.py` | new | ~150 |
| `src/decoy_engine/config/_errors.py` | 28 | ~50 |
| `src/decoy_engine/config/_parse.py` | new | ~45 |
| `src/decoy_engine/validation/_config.py` | 89 (97 on CSV) | ~95 |
| `src/decoy_engine/config/_tables.py`, `_pipeline.py` | 452, 330 | no growth |
| `src/decoy_engine/connectors/postgres_snapshot.py`, `io/bounded_reader.py` | 539, 413 | +/- 10 |
| `tests/security/test_config_error_text.py` | new | ~250 |
| `tests/sentry/defects/_defect_scan.py` | new | ~180 |
| `tests/sentry/baselines/validation-name-messages.json` | new, generated | about 42 keys |
| CLI `cli/validate.py`, `cli/preflight.py` | 807, 886 | shrink (V2-LOG already) |
| CLI `cli/run.py`, `cli/report.py` | 1805, 2382 | shrink only (over 900, existing H-1 items) |
| CLI `tests/sentry/defects/test_config_error_routes.py` | new | ~220 |

## 10a. General review taxonomy (plan contract, not recurring-catalog numbers)

| # | Class | Applies | Closing structure | Proof and mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | Yes: the whole of Part A; data values, data literals and secrets must not enter errors, while structural names remain | `validation_problems` text table, direct `loc` joining and registered reason summaries | AT-A1 to AT-A8; mutations retain input, a tag echo or a value-bearing msg: red |
| 2 | Cleanup on an exit path | N/A: no resource is opened; `validate_config` already closes its handle in `finally`, unchanged | none new | none |
| 3 | State machine and concurrency | N/A: pure conversion and import-time regex; no shared state | none | none |
| 4 | Route parity and silent fallback | Yes: a CLI command or engine route that keeps its own `errors()` loop, and a fuzz that triggers no errors | AST walk plus Click-tree route cell; error-type coverage assert | AT-A6 coverage, AT-A8 per command; mutation: revert one site, or plant no extra keys: red |
| 5 | Referential integrity | N/A: no data or FK handling; allowlist entries are checked to name existing files | entry-exists checks in each ratchet | name a deleted file in an allowlist: red |
| 6 | Untrusted producer | Yes: the config file is user input, and so are its keys | the converter never reads `input`, `ctx["tag"]` or `ctx["error"]` text; class 1 refusals on identifiers that reach SQL | AT-A3, AT-A4, AT-B1 |
| 7 | Check then use (TOCTOU) | N/A: no path is checked and then acted on | none | none |
| 8 | Weak tests and overclaimed evidence | Yes: a sentry that cannot see a planted bypass; counts copied from this plan | seeded-bypass meta-test per sentry; counts regenerated on each base; mutations on the final head | each AT names its mutation; reviewer re-measures one census row |
| 9 | Behavior-preserving refactor drift | Yes: B1 and B2 swap `match` for `fullmatch`; bulk raise hoists are deferred | existing tests stay green unchanged; same exception type and code at each fixed regex site | AT-B1; T1 on the head; mutation: restore one `$`, red |
| 10 | Frozen contract drift | Yes: `PipelineConfigError` stops carrying `__cause__`; CLI `--json` `location` text changes form | CHANGELOG entries in both repos; `--json` keys unchanged; compat contract note in A1 | AT-A8 checks the key set; a dropped key: red |

### Recurring-defect catalog coverage

| Catalog class | Applies | Plan treatment |
|---|---|---|
| 1 Anchored validation | Yes | B1/B2 replace Python `re` end anchors; AT-B1 adds trailing-newline cells. |
| 2 Strict wire and stored types | No new wire or stored model | Existing public config strictness is unchanged; no new persisted primitive field. |
| 3 No exception context | Yes | Config routes prove empty cause and context; repo-wide bulk hoists are V2, with a shrink-only ratchet now. |
| 4 Versioned read models | No new read model | `ConfigProblem` is an in-process typed object; CLI JSON keeps its existing keys. |
| 5 Normalized guard scanners | Yes | `_defect_scan.py` resolves aliases and call forms; seeded cells cover each accepted spelling. |
| 6 No private-module imports | Yes | V1 fixes the two facade bypasses; the cross-package contract is V2 B3 after LAYOUT. |
| 7 No hidden CI failures | Yes | Sentries are mandatory registry entries and fail rather than skip when dependencies are absent. |
| 8 One trust class per entry point | Yes | Raw Pydantic errors enter one converter; engine and CLI routes use the public parse entry point. |
| 9 Mutations on final head | Yes | Section 7 mutations rerun and record the final head. |
| 10 Allowlisted helpers checked | Yes | Forwarders are keyed by `(path, name)` and their callers are scanned. |
| 11 No raised pins | Yes | Every baseline is regenerated on base and shrink-only; no part may raise it. |

## 10b. Enumerations

### Exit paths

- N/A: no resource is owned. The YAML loader keeps its `try`/`finally` close
  (`validation/_config.py:50`), unchanged.

### Routes

- Engine: `parse_pipeline_config` (new), `validate_config` (`validation/_config.py:80`),
  `override_sources` (`config/_override.py:65`); internal re-validation at
  `execution/_transforms_table.py:129` and `profile/_fixed_width_reader.py:129` (ratchet entries).
- CLI: the 13 sites in section 2 (`cli/validate.py:277`, `cli/preflight.py:721`,
  `cli/subset.py:181`, ...) plus `api.py:446`. The guards re-derive both lists from the AST and
  the Click tree.

### FK shapes

- N/A: no FK handling.

### Fields

- Model fields and mapping keys reached through `loc` and the public-model fixture matrix
  (`sources`, `targets`, `namespaces` are mappings from `config/_pipeline.py:78`).
- Discriminated-union tags (`SourceDescriptor`, `TargetDescriptor`).
- Extra keys, including `ColumnConfig`'s `extra="allow"` (`config/_tables.py:254`), which takes
  extras instead of refusing them.

### Output surfaces

- Engine: `str()`, `repr()`, `problems`, the `PipelineValidationError` message, the traceback
  chain.
- CLI: stdout, stderr and the `--json` envelope.

## 10c. Reuse inventory

- Reason registry: `reasons.get`, `is_registered` and the `SOURCE_FILE_*` config codes
  (`reasons/config/_part1.py`).
- Coded custom errors: `config/_declared_casting.py:28` `_raise` (F06 pattern, kept as is).
- Safe labels: `safe_output/_exception.py` `safe_error_type`.
- YAML text: CLI `cli/_yaml_errors.py` `sanitize_yaml_error` (moved to the engine, not copied).
- Ratchets and registry: `tests/sentry/_ratchet.py`, `scripts/dev/ratchet_check.py`,
  `sentry_registry.toml`, `_baseline_sources.py` (both repos); vendored-file pins (LAYOUT C2 or
  CI-L1).
- Layer tooling: LAYOUT's `.importlinter`, `layout/test_import_layers.py`, `_layout.ignore_keys`.
- Existing CLI boundary sentries left in place: `test_engine_internal_boundary.py`,
  `test_yaml_error_leak_isolation.py`.

## 10d. Frozen contracts

- `PipelineConfigError` no longer sets `__cause__` (its docstring promised it). Additive
  `problems`. Recorded in the engine CHANGELOG and the compatibility contract.
- New public names `parse_pipeline_config`, `ConfigProblem`; the
  `test_public_api.py` snapshot updates in A1.
- CLI `--json` keys `code`, `message`, `location` keep their names; `location` changes form and
  `message` text changes for validator rules. CLI CHANGELOG entry.
- No reason code is added, renamed or retired.

## 10e. Pre-mortem (failure modes)

- A location formatter drops or rewrites a structural name, so the user cannot find the bad field.
  Caught by AT-A1 and a test per container shape (dict, list, discriminated union, `extra="allow"`
  on `ColumnConfig`, nested model).
- A Pydantic upgrade adds an error type whose template echoes input. The catalog-equality guard
  goes red before release.
- A2 accidentally migrates a name-only validator and loses useful structure. Its regenerated
  classification inventory must show the changed site as value-bearing or unclassified; AT-A9
  shows each changed CLI message in review.
- The CLI keeps an uncaught `model_validate` path through a new command. The Click-tree route
  cell covers commands added later.
- The class-3 count rises while branches are in flight. B1 waits for the discovery fix, regenerates
  on that base and refuses any later increase; the risky bulk hoists remain V2 work.
- V2 B3 puts hundreds of generated ignores in `.importlinter` and drowns the tool input. It stores
  ratchet keys only in `import-layers.json`, and the config snapshot guards the short contract stanza.

## 11. Questions and decisions

- **Q1 (product, answered by owner 2026-11-06).** Names are structure and are shown everywhere:
  terminal, `--json`, CI logs and run records. This includes config keys, source/table/column names,
  file names, path patterns and regexes. Only data values, literals taken from data and secrets are
  hidden, including credentials in URLs or DSNs through existing secret masking. There are no
  ordinal placeholders, JSON-schema location walk or `--show-keys` mode.
- Technical calls taken (recorded, not asked): converter in `safe_output`; registered reason
  summaries for value-bearing validator failures; class 6 at top-level package granularity with root privates allowed unless they
  have a facade; Pydantic `pattern=` sites exempt while no model uses `python-re`.

## 12. Follow-ups

- FU-1 (platform, parked): `v2_runner` and the 422 handler call `parse_pipeline_config` and
  render `problems`; the platform `tests/sentry` gains the same route walk.
- FU-2: shrink the class-6 ignore list (H-1).
- FU-3: private names from public modules (`reason_adapter._CODED` in `safe_output/_exception.py`).
- FU-4: CLI `CiManifest` adopts `validation_problems` if its codes ever need a location.
- FU-5 (V2 corrective backlog): hoist the class-3 sites behind the shrink-only pins after the V1
  slices, with per-site control-flow tests; no V1 slice performs the bulk refactor.
- FU-6 (V2 corrective backlog): land B3 only after LAYOUT E3/C3 and H-1, with the four integration
  conditions in sections 4, 7 and 8.

## 13. Done when

- [ ] A1 and A2 ship together in one engine release. Structural `loc` names remain visible; no
      input, context, discriminator echo, data value, data literal or secret reaches any tested
      config error surface.
- [ ] The 104 built-in Pydantic error types equal the policy table; registered custom errors use
      reason summaries and unregistered custom errors use the generic row.
- [ ] The validator census is regenerated on the final A2 base. Value-bearing and unclassified
      messages are zero; unchanged name-only messages match the shrink-only normalized-AST snapshot.
- [ ] All engine and CLI validation routes use the public parse choke point, including terminal,
      `--json`, CI and run-record cells, with both Pydantic ValidationError import sources seeded.
- [ ] B1 and B2 replace the regenerated class-1 sites, land class-3 shrink-only ratchets at their
      final-base counts and fix the public-facade imports. Only config routes require empty context.
- [ ] Each V1 part stays under about 800 hand-written lines, respects the Placement table and runs
      its named T0/T1/T2 scope, red mutations on the final head and any section 9 full-suite trigger.
- [ ] The bulk class-3 hoists and B3 remain in the V2 corrective backlog. They are not V1 merge
      conditions.
