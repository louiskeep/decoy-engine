# VALUE-FREE-ERRORS and CLASS-SWEEP: value-free config errors and a recurring-defect sweep

Status: plan (2026-11-06, not built). Covers engine and CLI; lives in the engine repo.

Rules consulted: `~/dev-rules/README.md` (routing table), `00-universal.md`,
`development-loop.md`, `cross-cutting-concerns.md`, `architecture.md`, `security.md`,
`catalogs/recurring-defects.md` (classes 1, 3, 5, 6, 9, 10, 11). Also read: vault
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
   structural problems: `ConfigProblem(code, location, text)`. `location` keeps schema field
   names, list indexes and valid discriminator tags; every mapping key becomes an ordinal
   placeholder (`sources[#2]`), every forbidden extra key becomes `<extra #1>`. `text` comes from a
   fixed table keyed by error type; input-derived parts (`Input tag 'X'`, a validator's own
   message, `input_value`) are dropped. The raised error has `__cause__ is None` and
   `__context__ is None`.
2. `validate_config`, `override_sources` and a new public `parse_pipeline_config` are the engine
   routes, and every CLI site that validates an engine public model goes through
   `parse_pipeline_config`. No CLI output reads `ValidationError.errors()`, `.json()` or `str()`.
3. Config validators raise `ConfigRuleError`, whose message is a `LiteralString`, or a
   `PydanticCustomError` with a registered reason code. Only those messages survive the converter.
4. A guard derives the public models from `decoy_engine.__all__` and their JSON schema, and the
   CLI routes from the Click command tree, never from a handwritten list.

Part B (CLASS-SWEEP) promises, in both repos: no `$`-anchored whole-value regex (class 1), no
`raise ... from None` inside an `except` block (class 3), and no import of another top-level
package's `_private` module beyond a shrink-only baseline (class 6). Each has a sentry whose count
can only fall.

Non-goals (a finding here is a follow-up, not a blocker):

- Value-free text for errors that do not come from a Pydantic model (expression parser, readers,
  connectors). `fix/value-free-discovery-subset-errors` and F31 own those.
- `raise X from exc` chains on non-config errors. Class 3 is the `from None` miss only.
- Private *names* imported from a public module (`reason_adapter._CODED`). Class 6 is modules.
- Fixing the 480-odd existing class-6 edges. They go in the ratchet; H-1 shrinks them.
- A debug flag that shows raw keys locally (product question Q1).
- Platform: `api/jobs/v2_runner.py` and its 422 handler read `errors()`; follow-up FU-1.

## 2. Measured today

Pydantic 2.13.4 (engine lock). A scratch model with `hide_input_in_errors=True` and
`extra="forbid"` still renders, in both `str(exc)` and `errors(include_input=False,
include_context=False, include_url=False)`: the mapping key (`sources.customers_prod.a.n`), the
forbidden key (`sources.customers_prod.a.zz_SECRETK`, `SECRET_TOP`), the discriminator echo
(`Input tag 'SECRET_TAG' found using 'type'`) and the validator text (`Value error, bad n 7
SECRETV`). `pydantic_core.list_all_errors()` returns 111 error types with their templates.

Engine config validators (on the CSV branch): 53 `raise ValueError`/`PydanticCustomError` sites in
`config/`; 42 interpolate values or names into the message, 1 passes a non-literal.

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

Followed sources: the converter drives its text table from `pydantic_core.list_all_errors()`
(the library's own catalog), so a Pydantic upgrade that adds a type fails a guard instead of
leaking. Location rewriting walks the model's own JSON schema (`model_json_schema()`:
`properties` are fields, `additionalProperties` is a mapping, `discriminator.mapping` holds the
tags). `ConfigRuleError` types its message as `typing_extensions.LiteralString` (PEP 675), which
mypy enforces, so a validator cannot format a value into it. Class 1 uses `re.fullmatch` or `\Z`
(Python `re` docs); the sentry finds `$` with the stdlib regex parser (`re._parser` on 3.11+,
`sre_parse` on 3.10), which yields `AT_END` for `$` and `AT_END_STRING` for `\Z`, so `$` inside a
class or escaped is not flagged. Class 3 follows PEP 415: `from None` only sets
`__suppress_context__`; `__context__` stays. Class 6 uses import-linter, which LAYOUT adopts.

Custom code that remains: the location walk and the text table (about 200 lines). No library
offers either.

## 4. Design notes

- **Deep module.** One converter, `safe_output.validation_problems(exc, model, raw)`, hides
  the error-type table, the schema walk and the ordinal placeholders. Callers get a tuple of
  `ConfigProblem`. Change pain removed: today 13 CLI sites and 2 engine routes each decide how
  much of a `ValidationError` to show; the next command copies whichever it sees first.
- **Open/closed.** The text table has one row per Pydantic error type, derived from the library
  catalog. A new error type is a row, not a branch.
- **Placement by layer.** The converter is generic over Pydantic models, so it sits in
  `safe_output/` (LAYOUT tier 5, under `config`). `ConfigRuleError` goes in `errors.py` (tier 6),
  so `safe_output` can recognise it without importing `config`.
- **One fact, one place.** Value-free text for reason-coded errors is the registered reason's
  safe summary (`reasons.get(code)`), never the raise-site message. The YAML parse text moves
  from CLI `cli/_yaml_errors.py` to the engine (`safe_output.yaml_error_text`), and the CLI name
  becomes a re-export, so one copy remains.
- **Ordinal placeholders.** `sources[#2]` names the second key of that mapping in input order. It
  is a count, not a value, and still tells the user which source is wrong.
- **House style.** `ConfigProblem` is a frozen dataclass (typed context object). No flag
  arguments: the converter has one mode.
- **Class 6 and LAYOUT.** LAYOUT's `layers` contract checks tier direction only. Siblings in a
  tier, and any downward import, may name a private module, and import-linter wildcards replace
  whole segments only (`_*` is refused by `ModuleExpressionField`), so no built-in contract can
  say "no `_private` module of another package". The rule therefore rides LAYOUT's mechanism as
  a second contract in the same `.importlinter`: a custom contract type (import-linter's
  documented `contract_types` hook) over the same grimp graph, run by the same
  `layout/test_import_layers.py` wrapper, with its ignore keys in the same `import-layers.json`
  baseline through `_layout.ignore_keys` (keys already carry a `<contract>::` prefix). One tool,
  one config, one gate step, no second guard. The one root facade (`_progress_events` behind
  `progress`) uses the built-in `protected` contract.
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
| `tests/security/test_config_error_text.py` | `tests/security/` | existing security-test location (CODEMAP line 126) | `test_reason_boundary.py` |
| `tests/sentry/defects/__init__.py`, `_defect_scan.py`, `test_anchored_regex.py`, `test_exception_context.py`, `test_validation_sites.py` | `tests/sentry/defects/` | map change: "Sentry test" row gains "recurring-defect class rules in `tests/sentry/defects/`" (engine `tests/sentry/` holds 56 entries, over the 25 cap) | `tests/sentry/layout/` |
| `tests/unit/config/test_config_rule_errors.py` | `tests/unit/config/` | "Config models" row | `test_csv_dialect_rules.py` |
| CLI `tests/sentry/defects/` (same three tests, `_defect_scan.py` vendored) plus `test_config_error_routes.py` | CLI `tests/sentry/defects/` | same map change in CLI CODEMAP | CLI `tests/sentry/layout/` |
| `tests/sentry/layout/_private_contract.py` (both repos, vendored) | `tests/sentry/layout/` | "Layer contracts" row | `layout/_layout.py` |

No new product package; no package crosses its fan-out cap (`safe_output/` goes from 3 to 4).

## 6. Choke points, guards and red mutations

| Rule | Choke point | Guard (walks) | Red mutation |
|---|---|---|---|
| ValidationError text is value-free | `safe_output.validation_problems`; routes `parse_pipeline_config`, `validate_config`, `override_sources` | `tests/security/test_config_error_text.py`: for every `BaseModel` in `decoy_engine.__all__` (and every model its JSON schema reaches), build marker input from the schema (marker mapping keys, an extra marker key per object, a marker tag per discriminated union, marker strings and wrong types per field), validate, convert, search `text`, `location`, `code`, `str()`, `repr()` and the chain for any marker | keep the raw mapping key in `location`: red; keep the `union_tag_invalid` template: red; pass `value_error` msg through: red; raise inside the `except`: red |
| Every Pydantic error type is classified | text table in `_validation.py` | same file: `{t["type"] for t in list_all_errors()}` equals the table keys, and each kept placeholder is in the schema-derived safe set | delete the `extra_forbidden` row: red; mark `tag` safe: red |
| No raw ValidationError reaches output (engine) | the three routes | `defects/test_validation_sites.py`: AST walk of `src/decoy_engine`, aliases resolved (class 5), flags `.model_validate*`/`TypeAdapter.validate_*` on a public model outside the routes, and any `.errors(`, `.json(`, `str()`, f-string or `%` of a `ValidationError`-bound name outside `_validation.py`; internal re-validation of already-checked dicts (`execution/_transforms_table.py:129`, `profile/_fixed_width_reader.py:129`) is a reasoned ratchet entry | add `PipelineConfig.model_validate(x)` with `str(exc)` in a scratch module: red; alias `from pydantic import ValidationError as VE`: still red |
| Validator messages are literal | `errors.ConfigRuleError(message: LiteralString)` | mypy on `config/`; `test_validation_sites.py` counts `raise ValueError`/`AssertionError` and non-literal `PydanticCustomError` messages in modules that define a public model, ratchet 0 after A2 | add `raise ValueError(f"{name}")` in `_tables.py`: red |
| CLI routes use the engine choke point | `decoy_engine.parse_pipeline_config` | CLI `defects/test_config_error_routes.py`: AST walk with the same flags over `src/decoy` (public models read from the installed engine's `__all__`); plus a route cell that walks the Click tree, invokes each command with a config-path parameter on a marker config, and searches stdout, stderr and `--json` | revert `validate.py` to the `errors()` loop: red; add a new command calling `model_validate`: red |
| Class 1 | `re.fullmatch` or `\Z` | `defects/test_anchored_regex.py`: AST walk resolves `re` aliases and module-level pattern constants, parses each pattern, flags `AT_END`; asserts no model sets `regex_engine="python-re"` | restore `$` in `postgres_snapshot._IDENT_RE`: red; `import re as r; r.compile("x$")`: red |
| Class 3 | raise after the `except` closes | `defects/test_exception_context.py`: AST walk flags `raise ... from None` lexically inside an `ExceptHandler`; baseline 0 | re-add one `from None` in `bounded_reader.py`: red |
| Class 6 (cross-package private) | `.importlinter` contract `private_modules` | `layout/test_import_layers.py` (LAYOUT) | add `from decoy_engine.plan._top_code import x` in `kernel/`: red; delete one ignore line: red |
| Class 6 (facade) | built-in `protected` contract on `decoy_engine._progress_events`, allowed `decoy_engine.progress` | same | restore the telemetry import: red |

Every sentry is seeded-bypass tested (a planted violation in a scratch tree turns it red, removing
a real site lowers the count), registered in `SENTRIES` and `scripts/dev/sentry_registry.toml`,
and runs in the `gates.sh` sentry step. Ratchets use the X0a baseline and never grow (class 11).
A forwarding helper on any allowlist has its callers checked by `(path, name)` (class 10).

## 7. Acceptance tests

Each test names the mutation that turns it red. Red-first evidence is an assertion failure.

- AT-A1 (location): `sources: {customers_prod: {type: file, format: csv, path: 1}}` gives
  `location == "sources[#1].file.path"`; the marker `customers_prod` appears nowhere. Mutation:
  return `str(seg)` for mapping keys.
- AT-A2 (extra key): an unknown key `zz_marker` at the root and under a table gives
  `<extra #1>` and `tables[0].<extra #1>`, code `extra_forbidden`. Mutation: keep the key.
- AT-A3 (tag): `type: marker_tag` under `sources` gives code `union_tag_invalid`, text naming
  `discriminator` and `expected_tags` only. Mutation: render the library template.
- AT-A4 (validator text): a `ConfigRuleError` keeps its literal text; a plain `ValueError` with a
  marker gives "failed a config rule"; a `PydanticCustomError` with a registered reason code gives
  that reason's safe summary, not its raise-site text. Mutation: trust `value_error` msg.
- AT-A5 (chain): for every route, `__cause__ is None`, `__context__ is None`, and
  `traceback.format_exception` of the error holds no marker. Mutation: `raise ... from exc`.
- AT-A6 (schema-derived fuzz): section 6 row 1 over every public model; also asserts the observed
  error types include `missing`, `extra_forbidden`, `union_tag_invalid`, `literal_error`,
  `value_error`, `string_type`, so a fuzz that triggers nothing cannot pass. Mutation: drop the
  extra-key planting; the coverage assert goes red.
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
- AT-B2 (class 3): for each fixed site with a reachable test, the raised error has
  `__context__ is None`; the sentry covers the rest. Mutation: re-add `from None`.
- AT-B3 (class 6): `telemetry` imports `decoy_engine.progress`; `SpanRecorder` behavior tests
  unchanged. Mutation: restore the private import; the `protected` contract goes red.

Mutations rerun on the final head and the head is recorded (class 9).

## 8. Parts (slices)

Each part: Conventional Commits subject at most 72 characters, final line `cam`. Test depth T0 per
edit, T1 per commit, T2 at gates; full suite only where section 9 lists a trigger. Counts and
baselines are regenerated on the part's base, never copied from this plan.

| Part | Repo | Branch | Base | Depends on | Hand-written lines |
|---|---|---|---|---|---:|
| A1 converter | engine | `feat/value-free-errors-a1` | engine main | CLI-DIALECT CSV reader and RESULT E1 merged | ~620 |
| A2 rule errors | engine | `feat/value-free-errors-a2` | engine main after A1 | A1 | ~420 |
| A3 CLI routes | CLI | `feat/value-free-errors-a3` | CLI main after repin | engine release with A2; CLI repin; CI-L1 merged | ~450 |
| B1 engine sweep | engine | `fix/class-sweep-b1` | engine main | CSV reader, RESULT E1, CI-E1 merged | ~600 |
| B2 CLI sweep | CLI | `fix/class-sweep-b2` | CLI main | B1 merged (vendor source); CI-L1 merged | ~380 |
| B3 private contract | engine, CLI | `feat/class-sweep-b3-engine`, `feat/class-sweep-b3-cli` | main after LAYOUT E3 / C3 | B1 / B2; LAYOUT E3 / C3 | ~150 each + generated ignores |

**A1.** `safe_output/_validation.py`: `ConfigProblem`, `validation_problems`, the type table,
the schema location walk, `yaml_error_text`; exports from `decoy_engine.safe_output` and the
root. `errors.py`: `ConfigRuleError`. `config/_errors.py`: `PipelineConfigError` gains
`problems` and stops setting `__cause__` (docstring updated). `config/__init__.py` (or a new
`config/_parse.py` if `__init__` would mix concerns): `parse_pipeline_config(raw) ->
PipelineConfig`. `validation/_config.py` and `config/_override.py` route through it.
`tests/security/test_config_error_text.py`, `defects/test_validation_sites.py` (validator-literal
ratchet starts at today's count). CHANGELOG and `docs/security/error-reporting-and-data-exposure.md`
gain the promise. Builds once CSV and RESULT E1 merge (both touch `__init__.py`,
`validation/_config.py`, `config/_sources.py`, and the sentry manifest and registry).

**A2.** Migrate the 53 validator raises in `config/` to `ConfigRuleError` with literal text, or
to an existing reason code where one exists (`SOURCE_FILE_BAD_DELIMITER` and the other
`SOURCE_FILE_*` codes for the CSV dialect rules in `config/_csv_dialect.py`). A name the old
message carried is replaced by the problem's location. No new reason codes. The ratchet falls to
0. `config/_tables.py` (452 lines) and `config/_pipeline.py` (330) do not grow. Release the engine
right after A2 (owner standing authority for engine releases).

**A3.** Every site in section 2 calls `parse_pipeline_config` and renders `problems`. `cli/run.py`
(1805) and `cli/report.py` (2382) only shrink. `cli/_yaml_errors.sanitize_yaml_error` becomes a
re-export of the engine function (its 23 callers and `test_yaml_error_leak_isolation.py` keep
working). `ci/manifest.py` stays on its own codes as a reasoned allowlist entry (reads `type`
only). New `defects/test_config_error_routes.py`. Repin follows the engine repin checklist
(compat, fences, FROZEN, doc, `PIN_PREFIX`, Dockerfile ARG, catalog).

**B1.** Class 1 fixes (15 sites to `fullmatch` or `\Z`), class 3 fixes (52 sites: save what the
message needs, leave the `except`, then raise), class 6 fixes (`telemetry` imports from
`decoy_engine.progress`; `_progress_events` and `safe_output/_event.py` read `CleanupState`,
`PublicationState`, `COUNT_KEYS`, `MAX_COUNTS` and an `is_count` from the public
`decoy_engine.reasons` instead of `reasons._record`). Sentries `test_anchored_regex.py` and
`test_exception_context.py` with `_defect_scan.py` (normalized per class 5). If LAYOUT E3 merged
first, the same diff moves `telemetry` into the `progress` tier in `.importlinter` and its
snapshot (telemetry has no importers in `src/`). B1 and A1 can build in parallel worktrees; the
second to merge rebases the `SENTRIES` and registry rows.

**B2.** CLI class 1 (6 sites) and class 3 (21 sites); vendored `_defect_scan.py` from the B1 merge
commit, sha-pinned with the repo's existing vendored-file pin (LAYOUT C2's
`test_vendored_files.py` if merged, else CI-L1's `ci/vendored.toml`; no third mechanism). Every
rule of the engine scanner is kept (class 5).

**B3.** `layout/_private_contract.py` (custom import-linter contract, about 80 lines, vendored to
the CLI), a `private_modules` stanza and the `protected` stanza in each `.importlinter`, ignore
lines regenerated from the graph, keys registered through LAYOUT's `ignore_keys`. CLI starts with
an empty ignore list.

## 9. Test scope and in-flight sequencing

| In-flight | Touches | Parts that wait |
|---|---|---|
| CLI-DIALECT CSV reader (engine `feat/csv-source-reader`) | `validation/_config.py`, `config/_sources.py`, `config/_csv_dialect.py`, `__init__.py`, sentry manifest and registry | A1, A2, B1 |
| RESULT E1 (`feat/run-result-model`) | `reasons/_record.py` (class 1 site at :40), `__init__.py`, sentry manifest | A1, B1 |
| CI-E1 (`fix/ci-e1-frozen-installs`) | `test_sentry_manifest.py`, `test_module_size.py`, CI files | B1 (A1 if it lands first) |
| CI-L1 (CLI `fix/ci-l1-cli-ci`) | CLI `tests/sentry/`, `ci/` | A3, B2 |
| `fix/value-free-discovery-subset-errors` (not in the brief, found in flight) | `data_discovery.py`, `subset/_seed.py`, `tests/security/test_value_free_errors.py`, may add `from None` sites | B1 re-measures after it |
| LAYOUT E1 / E3, C1 / C3 | CODEMAP map, `.importlinter` | Placement rows; B3 |

Full-suite triggers: A1 (serialization of a public error, frozen-contract adjacent), A3 (version
pin and lock), B1 and B2 (shared sentry config). A2 runs T1 plus the config and CLI-facing suites.

## 10. File sizes

| File | Today | After |
|---|---:|---:|
| `src/decoy_engine/safe_output/_validation.py` | new | ~230 |
| `src/decoy_engine/errors.py` | 521 | ~540 |
| `src/decoy_engine/config/_errors.py` | 28 | ~50 |
| `src/decoy_engine/validation/_config.py` | 89 (97 on CSV) | ~95 |
| `src/decoy_engine/config/_tables.py`, `_pipeline.py` | 452, 330 | no growth |
| `src/decoy_engine/connectors/postgres_snapshot.py`, `io/bounded_reader.py` | 539, 413 | +/- 10 |
| `tests/security/test_config_error_text.py` | new | ~300 |
| `tests/sentry/defects/_defect_scan.py` | new | ~180 |
| CLI `cli/validate.py`, `cli/preflight.py` | 807, 886 | shrink (V2-LOG already) |
| CLI `cli/run.py`, `cli/report.py` | 1805, 2382 | shrink only (over 900, existing H-1 items) |
| CLI `tests/sentry/defects/test_config_error_routes.py` | new | ~220 |

## 10a. Class table (plan contract)

| # | Class | Applies | Closing structure | Proof and mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | Yes: the whole of Part A; config keys, extra-key names, tags and validator text are values | `validation_problems` text table and ordinal locations; `ConfigRuleError` literal messages | AT-A1 to AT-A8; mutations keep a key, a tag or a msg: red |
| 2 | Cleanup on an exit path | N/A: no resource is opened; `validate_config` already closes its handle in `finally`, unchanged | none new | none |
| 3 | State machine and concurrency | N/A: pure conversion and import-time regex; no shared state | none | none |
| 4 | Route parity and silent fallback | Yes: a CLI command or engine route that keeps its own `errors()` loop, and a fuzz that triggers no errors | AST walk plus Click-tree route cell; error-type coverage assert | AT-A6 coverage, AT-A8 per command; mutation: revert one site, or plant no extra keys: red |
| 5 | Referential integrity | N/A: no data or FK handling; allowlist entries are checked to name existing files | entry-exists checks in each ratchet | name a deleted file in an allowlist: red |
| 6 | Untrusted producer | Yes: the config file is user input, and so are its keys | the converter never reads `input`, `ctx["tag"]` or `ctx["error"]` text; class 1 refusals on identifiers that reach SQL | AT-A3, AT-A4, AT-B1 |
| 7 | Check then use (TOCTOU) | N/A: no path is checked and then acted on | none | none |
| 8 | Weak tests and overclaimed evidence | Yes: a sentry that cannot see a planted bypass; counts copied from this plan | seeded-bypass meta-test per sentry; counts regenerated on each base; mutations on the final head | each AT names its mutation; reviewer re-measures one census row |
| 9 | Behavior-preserving refactor drift | Yes: B1 and B2 move raises out of `except` blocks and swap `match` for `fullmatch` | existing tests stay green unchanged; same exception type and code at each site | AT-B2; T1 on the head; mutation: change one site's exception type, its existing test goes red |
| 10 | Frozen contract drift | Yes: `PipelineConfigError` stops carrying `__cause__`; CLI `--json` `location` text changes form | CHANGELOG entries in both repos; `--json` keys unchanged; compat contract note in A1 | AT-A8 checks the key set; a dropped key: red |

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

- Model fields and mapping keys reachable from the public models' JSON schema
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
- New public names `parse_pipeline_config`, `ConfigProblem`, `ConfigRuleError`; the
  `test_public_api.py` snapshot updates in A1.
- CLI `--json` keys `code`, `message`, `location` keep their names; `location` changes form and
  `message` text changes for validator rules. CLI CHANGELOG entry.
- No reason code is added, renamed or retired.

## 10e. Pre-mortem (failure modes)

- The schema walk mislabels a field as a mapping key, so a real field name becomes `[#n]`.
  Caught by AT-A1 and a test per container shape (dict, list, discriminated union, `extra="allow"`
  on `ColumnConfig`, nested model).
- A Pydantic upgrade adds an error type whose template echoes input. The catalog-equality guard
  goes red before release.
- A2 drops a name from a message and users cannot find the bad table. The location carries the
  index (`tables[3]`); AT-A9 shows each changed CLI message in review.
- The CLI keeps an uncaught `model_validate` path through a new command. The Click-tree route
  cell covers commands added later.
- B1's class 3 hoists change control flow (a loop `continue` or a cleanup). Existing tests stay
  unchanged; a site that cannot be hoisted cleanly is a reasoned allowlist entry, not a rewrite.
- The class 6 ignore list is large (about 480 lines) and drowns review. Keys are generated, never
  hand-edited; the ratchet only shrinks.

## 11. Questions

- **Q1 (product).** Should the CLI print the real source or table name in a validation error on
  the user's own terminal? Recommended: no for V1. Ordinal placeholders (`sources[#2]`) are the
  only rendering in every output (terminal, `--json`, CI logs, run records); a local opt-in
  `--show-keys` is a V2 follow-up if users ask.
- Technical calls taken (recorded, not asked): converter in `safe_output`; `ConfigRuleError` with
  `LiteralString`; class 6 at top-level package granularity with root privates allowed unless they
  have a facade; Pydantic `pattern=` sites exempt while no model uses `python-re`.

## 12. Follow-ups

- FU-1 (platform, parked): `v2_runner` and the 422 handler call `parse_pipeline_config` and
  render `problems`; the platform `tests/sentry` gains the same route walk.
- FU-2: shrink the class-6 ignore list (H-1).
- FU-3: private names from public modules (`reason_adapter._CODED` in `safe_output/_exception.py`).
- FU-4: CLI `CiManifest` adopts `validation_problems` if its codes ever need a location.
