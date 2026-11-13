Status: plan

# DUP-FREEZE: settle the duplicated surfaces that freeze at REL-GATE (roadmap row 24zf)

Rules consulted: `/home/cam/dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md`, `feature-dev.md`, `bugfix.md`, `architecture.md`, `reuse-first.md`,
`api-and-compatibility.md`, `security.md`, `testing.md`, `exploration.md`,
`workspace-and-vcs.md`, `scope-discipline.md`, `verification.md`, `code-review.md`,
`cross-cutting-concerns.md`, `documentation.md`, `profiles/local-operations.md`,
`catalogs/recurring-defects.md`; engine `CLAUDE.md`, `CODEMAP.md` ("Where Things Go"),
`.importlinter`, `docs/compatibility-contract.md` (sections 1 and 3.3); CLI `CODEMAP.md`
("Where Things Go"), CLI `.importlinter`, CLI `docs/ROADMAP.md` (rows 24b, 24p, 24zd, 24ze, 24zf,
62, 63a, 66, 77, 93).

Revision 3, after Dennis round 2 (BLOCK: 3 HIGH, 6 MEDIUM, 5 LOW). Every finding is resolved in
the text below or logged in section 13.

Bases (2026-11-13): engine `origin/main` `8c9c1162`, CLI `origin/main` `5c9dd8e` (pins
`decoy-engine==0.23.0`). The CLI moved from `88403cc` by CLI #237, which touches only
`cli/io/_layout*.py`, `cli/io/_placement.py` and their tests; no site cited here moved. Inputs: the
duplication census `REMEDIATION.md` (CLI `docs/records/2026-11-12-duplication-census/`, merged in
CLI #211), package C (C1 to C10) and the matching guards of package D; the reachability pass
`REACHABILITY-A-B.md` (CLI branch `docs/dup-reachability-ab` `9efdc1ab`, not merged); the 24zd
plan as revised (engine `plan/dup-defects` `f659723b`), which routes A1 to C4, B5 and B8 to C5,
B6 and B7 to C1, and B9 to C7. Line numbers below were re-read at the bases above; some differ
from the census.

Owner decisions used:

- Roadmap row 24zf (owner 2026-11-12): settle one CLI error emitter and `--json` error shape,
  one exception-to-exit-code map, one canonical JSON rule for digests, the seed derivations
  (including A1, per-group `joint_mask` seeds), one config loader, `--json` only through
  `emit_json`, one flag truthiness rule per repo, one generated-at shape, one NER mismatch
  message, and the identifier locale rule. Builds after the hot `run/` lanes (RESULT, F29 C1,
  RUN-STREAM C1) merge and before REL-GATE.
- A1 (owner 2026-11-12): seed each `joint_mask` group through `derive(mask_key, namespace,
  table, group)`, a one-time output change. A9 is built in GEN-PROVIDERS (row 24p).
- 24zd OQ-2 answer (a) (owner 2026-11-13, "go recommended"): B5, a non-UTF-8 config, is a V1
  item under the 24b rule and is fixed by the one config loader in this row.
- Row 24b QA-GATE (owner 2026-11-08): "`decoy validate` refuses what `run` would refuse ...
  fail at the gate with exit 1, the config-error code, never exit 3 at run".
- The V1 bar (owner 2026-11-02): only a product-function break, data loss or corruption, or
  real values or secrets shown to a non-admin block V1. "Names may show in messages; data
  values never" (2026-11-06). V1 threat model: accidental leaks only.
- Pre-GA hard delete: no shims, no aliases (engine `CLAUDE.md`, compatibility contract 1).
- Round 1 rulings (orchestrator, 2026-11-13): OQ-4 uses the records rule with a second, named,
  byte-pinned seed form; `CanonicalJsonError` is a `ValueError`; C6 goes through
  `emit_json_document`; OQ-5 resolves the seed once per spec; OQ-8 makes
  `DECOY_NO_UPDATE_CHECK` fail safe and gives the engine flag error a reason code; OQ-1, 2, 3,
  6, 7 and 9 take option (a) as tech-lead calls. Section 11 records each.
- Round 2 rulings (orchestrator, 2026-11-13): deterministic composite masking keeps today's
  32-byte mask-key conversion and output; `_hash_config` refuses dates and datetimes with
  `config_value_not_json`; the second seed fingerprint caller and the subset refusal boundary
  are in E1; the exit-map baseline starts with the three real classifiers; the CLI engine-flag
  acceptance path uses the isolated-worker marker.

Risk: R2 overall. The row moves frozen-at-release surfaces: a `--json` error shape, exit codes,
digest bytes for non-ASCII or non-default-separator content, generated output bytes for
`joint_mask`, and environment variable meanings. No slice touches keys, vault bytes, auth or
delivery. Out of scope: A9 (row 24p), the full CX-1 conversion of every error site (V1+), the
D sentries that 24ze builds for classes this row does not settle (D1, D4, D5, D10, D11) and the
wider baselines of D2, D6 and D9 (section 6.3 says how the files are shared), every LATENT and
IDENTICAL census item not named in package C, platform callers (Platform V1 is parked).

## 1. Frame

### 1.1 What exists

REL-GATE (row 93) freezes output bytes, digests, exit codes, `--json` shapes and environment
variable meanings at release. Package C of the census lists the ten places where two or more
copies of one rule disagree on such a surface. After REL-GATE, fixing any of them is a
migration; before it, a changelog line. This row settles each one to a single owner and adds
the guard that stops a second copy from coming back.

Today, by item:

- **C1, error emitter.** About 105 `err_console.print(error(` or `"status": "error"` sites in
  25 CLI files, with ten local emitters (for example `cli/unmask.py:135`,
  `cli/fit.py:403-425`, `cli/scanning/_multi.py:216`, `cli/gen/_scaffold.py:164`). The common
  `--json` error object is `{"command", "status": "error", <context keys>, "error", "hint"?}`.
  `fit` differs: with a code it adds `code` and `message` and writes `error` as
  `"<code>: <message>"`. B6: `_multi.py` and `_scaffold.py` print the human line under
  `--quiet`, the rest print nothing. 31 `--quiet` options say "Errors still go to stderr"
  (`cli/report.py:662` and 30 more); nine do not (section 4.1). B7: `fit.py`, `_multi.py` and
  `reference.py` print engine text without `markup=False`, so Rich drops bracketed text such as
  `[vault]`.
- **C2, exit map.** `run` classifies failures in `cli/run/_errors.py:211-232`; `demo` keeps its
  own three-type map (`cli/demo.py:582-590`); `fit` its own (`cli/fit.py:663`, `:724`); the
  gate has `gate_exit_code` (`cli/gate/_layers.py:391`). The same failure can exit 1 in `run`
  and 3 in `demo` (B1, LATENT in the reachability pass).
- **C3, digest JSON.** `records/_canonical.py:18-26` is the only module that states a rule:
  sorted keys, compact, `allow_nan=False`, `ensure_ascii=False`. Its docstring (`:3-5`) says the
  rule matches the platform's canonical JSON so a record id and the CLI evidence manifest hash
  (`cli/evidence.py:171-176`, through engine `records`) agree on bytes; the CLI's
  G-ONE-CANONICAL sentry (`tests/sentry/io_policy/test_canonical_json.py`) guards that. Other
  engine sites build hashed or signed bytes with their own variants (section 6.2, Fields):
  `generators/derivation.py:204` and `generation/pool/_builder.py:39-44` feed seeds with compact
  ASCII JSON; `plan/_compile.py:660` (`_hash_config`, public as `pipeline_config_hash`),
  `profile/_serialize.py:74-79` (`profile_hash`) and
  `generation/_reproducibility.py:163-177` (`PinnedEnvelope.digest`) use compact ASCII JSON;
  `generation/_reproducibility.py:204` keeps `default=str`; `execution/physical/_inputs.py:94`
  and `subset/_scope.py:41` keep `default=repr`.
  `plan/_compile.py:651-660` removed `default=str` on purpose. `records` is import tier 1, so
  tiers 2 to 6 cannot import it.
- **C4, seeds.** A1: `transforms/joint_mask.py:338` (null-key fallback) and `:366-367` (gen
  mode) seed `default_rng(int.from_bytes(job_seed[:8], "big"))` from the mask key
  (`execution/_strategies/_joint_mask.py:123`), with no namespace, table or group, so every
  `joint_mask` group draws the same stream and 8 key bytes become a PRNG seed. A3: with no
  seed, composite providers fall back to an all-zero seed at 13 sites (section 4.4) and to
  `os.urandom(8)` at six (`_address.py:181`, `_provider.py:204` and four more), so one unseeded
  spec can build its pool from zeros and sample from fresh entropy. A4: five composite
  `int.from_bytes(seed, "big")` conversions rely on the upstream `ProviderSpec` 8-or-32-byte
  check, while the native mirror has no equivalent local check and
  `generation/pool/_sampler.py:168` checks exactly 8 bytes.
- **C5, config loader.** `cli/gate/_layers.py:91-104` `parse_pipeline_yaml` is already the
  single parser for `run` and the gate (validate-always plan), and `_run_config_base`
  (`:117-150`) owns the `yaml.empty` and `yaml.not_mapping` refusals. Ten other CLI sites read and
  `yaml.safe_load` a user config with their own except lists (for example `cli/tokenize.py:201`,
  `cli/unmask.py:155`, `cli/preflight.py:686-699`, `api.py:93`). A non-UTF-8 file raises
  `UnicodeDecodeError` to the global handler in six of them (B5), and `run` catches it in
  `cli/run/_prepare.py:56-67` and reports a runtime error with exit 3. The codes
  `yaml.parse_error`, `yaml.empty` and `yaml.not_mapping` are public: `ci/rule_codes.py:61-63`
  (SARIF rule ids), `docs/ci-evidence/verification.md:66`, `tests/unit/test_ci_rule_codes.py`.
  The mask-secret reference guard has four copies with three message variants
  (`api.py:108-125`, `cli/run/_prepare.py:199-226`, `cli/tokenize.py:215-231`, `:557-573`) and a
  case-sensitive prefix test (`_mask_secret.py:35`, `:61`), while the engine `parse_ref`
  lower-cases the scheme (B8).
- **C6, stdout JSON.** Two default-mode product documents bypass `ui/output.py`:
  `cli/schema.py:91-92` (indented schema) and the streamed scan at `cli/scanning/_multi.py:471`
  (`decoy scan --output -`, compact). The streamed scan is default mode only: `_multi.py:421-428`
  refuses `--output -` with `--json` or `--quiet`. `emit_json` (`ui/output.py:154-161`) writes
  only in `--json` mode and is a no-op otherwise, so it cannot carry either document.
- **C7, truthiness.** CLI: `_update_check.py:41-43` (`DECOY_NO_UPDATE_CHECK`: any value but `0`
  opts out of the network check), `:48` (`CI=false` counts as CI, B9), `__main__.py:141`
  (`DECOY_DEBUG == "1"`), `ui/output.py:47` (`NO_COLOR is not None`), `cli/run/_notify.py:165` (a
  non-numeric `DECOY_NOTIFY_SMTP_PORT` drops every notification with "invalid literal for
  int()", B9). Engine: `profiling/model_pack/loader.py:57-58`, `:81-98`
  (`DECOY_PACK_REQUIRE_SIGNATURE`, set `1, true, yes, on` and `0, false, no, off, ""`; any other
  value raises `ModelPackLoadError` whose text holds the raw value, and `load_with_fallback`
  (`:225-243`) turns that into "no pack, regex baseline" with a warning that logs the value)
  against `:184-186` (`DECOY_ML_DISABLED == "1"`, read outside the `try` at
  `_detectors.py:71`), `execution/_isolated_run.py:221`, `:238`, `internal/faker_setup.py:223`
  (`CUSTOM_PROVIDERS_DIR`, no `DECOY_` prefix).
- **C8, generated-at.** `profiling/types.py:193` writes `datetime.utcnow().isoformat() + "Z"`
  (deprecated call); `profiling/scan_set.py:283`, `quality/report.py:257` and
  `verification/postmask/runner.py:172` write `+00:00`.
- **C9, NER mismatch.** `execution/_strategies/_ner_config.py:77`
  (`enforce_ner_model_version`) holds one wording; `_text_mask.py:134-149` and
  `out_of_core/_mask_group_c.py:344-357` inline two more instead of calling it.
- **C10, identifier locale (A9).** Owner placed it in row 24p; this row only records the route.

### 1.2 Why one row

Each item is small, but all ten share one deadline (REL-GATE) and one method: pick the owner,
move every copy to it, add the guard. Doing them in one plan keeps one decision list, one
compatibility note, and one golden-diff budget per slice.

## 2. Guarantee

After this row merges, and before REL-GATE:

1. Every CLI command writes its `--json` error object in one documented shape
   (`docs/schemas/decoy-error-v1.schema.json` in the CLI repo), and its human error line through
   one emitter that prints with `markup=False` and prints under `--quiet` too. Sites not yet
   converted are proven to write the same shape by an AST check over every literal, not by
   sampling (AT-CL1-6).
2. Every CLI command that maps an unexpected exception to an exit code uses one function,
   `exit_code_for(exc)` in `cli/exit_codes.py`; `run` and `demo` exit the same way for the
   same failure.
3. Every sorted-key JSON byte string that is hashed, HMAC-ed or signed is built by one module,
   `decoy_engine._canonical`, in one of two named forms: `canonical_json_bytes` (the records
   rule, `ensure_ascii=False`) for digests, and `seed_fingerprint_bytes` (`ensure_ascii=True`,
   frozen under `SEED_PROTOCOL_VERSION`) for the two seed-feeding fingerprints. Both refuse
   NaN and Infinity; their direct-call record form converts dates and timezone-aware datetimes,
   while `_hash_config` refuses every date and datetime before routed config hashing. The only
   other `sort_keys` dumps left are on an exact allowlist, each with a kind and a reason.
   `generators/derivation.py:92` `column_identity` is a frozen, non-sorted JSON seed input and
   remains outside D3. No generated or masked output byte moves from C3, and no record id or CLI
   evidence manifest hash moves.
4. Each `joint_mask` group draws from its own stream derived from the mask key through
   `derive`; no `joint_mask` generator is seeded from raw key bytes; every bytes-to-int seed
   conversion is preceded by its owning length check. An unseeded composite spec resolves one
   seed once and uses it for every draw of that spec. The existing deterministic composite path
   remains the one explicit exception: after `ProviderSpec` and `resolve_seed` accept its
   32-byte mask key, its current full-width `int.from_bytes(seed, "big")` conversion is kept so
   keyed composite output does not move.
5. Every CLI command that reads a user config file reads it through one loader, so the same bad
   file gives the same code and exit in every command; a non-UTF-8, unparseable, empty or
   non-mapping config exits 1 with a `yaml.*` code in every command, `run` included.
   Mask-secret references are judged by one function that uses the engine parser.
6. CLI JSON reaches stdout only through `ui/output.py`, and the stdout bytes of `decoy schema`
   and `decoy scan --output -` do not change.
7. Each repo reads flag-type environment variables through one parser with one accepted set
   (true `1, true, yes, on`; false `0, false, no, off`). In the engine an unknown value raises a
   coded, value-free `EnvFlagError` (`env_flag_invalid`) that the CLI shows with the variable
   name. In the CLI every flag fails safe: an unknown value takes the flag's safe side and never
   exits. `CI=false` means not CI; a bad SMTP port is a named, value-free notification error that
   leaves the run exit unchanged.
8. Every engine "generated at" timestamp has one shape, `+00:00`.
9. `NER_MODEL_VERSION_MISMATCH` is raised from one function with one message.
10. Each item above has a sentry or choke point that fails when a second copy appears, and each
    guard names the mutation that turns it red (section 6.3).

No message, payload, log line or evidence field written by this row carries a data value,
secret, secret reference value, environment value or exception text. Names (commands, fields,
variables, codes) may appear.

## 3. Established solutions

- **Error envelope.** RFC 9457 (Problem Details for HTTP APIs) fixes a small set of members
  (`type`, `title`, `detail`) and lets extensions sit beside them; JSON:API error objects carry
  `code` and `detail` beside a stable status. Both separate a machine code from human text and
  never fold one into the other, which is what `fit`'s `"<code>: <message>"` does. This plan
  keeps the CLI's existing member names (`command`, `status`, `error`, `hint`) since consumers
  already read them, and adds an optional `code` member on the RFC 9457 pattern.
- **Exit codes.** BSD `sysexits.h` and the existing `cli/exit_codes.py` table use one mapping
  per process; Click and Typer centralise the usage-error code. One function from exception to
  code is the standard shape.
- **Canonical JSON.** RFC 8785 (JSON Canonicalization Scheme) sorts keys, drops whitespace,
  forbids NaN and Infinity, and writes UTF-8 without escaping. Python's `json` cannot write
  RFC 8785 numbers exactly (ES6 number form), so this plan does not claim JCS; it adopts the
  JCS rules Python can meet (sorted keys, compact separators, no NaN, UTF-8 without escaping,
  no implicit type conversion), which is the rule `records` and the platform already use.
  Sigstore and in-toto sign canonical JSON produced by one shared function for the same reason.
  The census EX-1 recommends keeping byte-pinned legacy forms as named variants of the one
  owner, which is how the seed fingerprint is kept.
- **Seed derivation.** NIST SP 800-108 and RFC 5869 (HKDF) derive independent sub-keys per
  purpose label; NumPy's `SeedSequence` documents spawning independent child streams instead of
  reusing one seed, and `default_rng(None)` draws fresh entropy once per generator. The engine
  already derives through `determinism/_derive.py:260` (`derive(seed, namespace, source)`); A1
  uses it.
- **Config loading.** PyYAML `safe_load` on decoded text, one decode step with an explicit
  codec, and a typed error per failure kind is the pattern `cli/compile_explain.py:175-181`
  already follows. The gate's `parse_pipeline_yaml` already carries the public codes.
- **Flag variables.** `NO_COLOR` (no-color.org): any non-empty value disables colour. The CI
  convention (GitHub Actions, GitLab, CircleCI set `CI=true`) treats `CI` as a boolean string.
  The engine's existing strict set (`loader.py:57-58`) is the accepted set; it is the
  `distutils.util.strtobool` set without the one-letter `y`, `t`, `n`, `f`.
- **Timestamps.** RFC 3339 allows `Z` or `+00:00`; Python 3.10's `datetime.fromisoformat`
  parses only `+00:00` (A6 in the census), so `+00:00` round-trips on every supported Python.
- **JSON Schema patterns.** JSON Schema uses ECMA-262 regular expressions, which have no `\Z`;
  `^...$` is the portable anchor, and the emitter enforces the same rule with `re.fullmatch`.
- **Guards.** Fitness functions (Ford, Parsons, Kua, "Building Evolutionary Architectures")
  with a shrink-only baseline; the dev-rules best-practice-becomes-a-sentry rule; the engine's
  `tests/sentry/_call_resolution.py` and the CLI's `tests/sentry/io_policy/_calls.py` resolve
  import aliases so a guard sees `j.dumps` after `import json as j`.

## 4. Design

### 4.1 C1: one error emitter and one `--json` error shape (CLI)

Owner: `src/decoy/ui/output.py` (162 lines today).

```python
def emit_error(
    state: OutputState,
    command: str,
    message: str,
    *,
    code: str | None = None,
    hint: str | None = None,
    context: Mapping[str, JsonValue] | None = None,
) -> None: ...
```

- `--json`: writes one object through `emit_json`:
  `{"command", "status": "error", "error": message, "code"?, "hint"?, **context}`. `context`
  keys are the existing per-command keys (`config`, `source`, `mode`, `output`) and may not
  shadow the five fixed members; a `code` that fails `re.fullmatch(r"[a-z][a-z0-9_.]*", code)`
  is refused too. Both are `ValueError` at the call, a programming error.
- Human: `error: <message>` and, when given, `  hint: <hint>`, both with `markup=False`, on
  `state.err_console`, in every mode including `--quiet` (OQ-1, closed). `--quiet` suppresses
  stdout only.
- `code` is the engine reason code or CLI warning code when the failure has one; it is never
  folded into `error`. `fit`'s `message` member goes (OQ-2, closed). `fit` warnings keep their
  own `code`, `message` and `detail` members; they are warnings, not the error object.
- `run` keeps its extra members (`error_kind`, `published_members()`, the RESULT reason
  members) as `context`; RESULT (row 62) owns their meaning. The run error arm moves to
  `emit_error` only after the hot lanes merge (slice CL4). `JsonValue` is the recursive JSON
  type (`None`, boolean, number, string, list, or string-keyed mapping), so `outputs`, `notify`
  and `extra` keep their existing list and object shapes. Callers remain responsible for the
  value-free contract.
- The schema file `docs/schemas/decoy-error-v1.schema.json` (CLI repo) states the fixed
  members, `additionalProperties: true` for context, and that `code` matches
  `^[a-z][a-z0-9_.]*$`.
- `--quiet` help text: the nine options that do not say errors reach stderr change to say it:
  `cli/report.py:1067`, `:1199`, `cli/setup/__init__.py:57`, `cli/upgrade.py:66` ("Suppress
  stdout."), `cli/fit.py:375`, `cli/report.py:1440`, `:1674` ("Suppress stdout. Exit code
  carries the result."), `cli/compile_explain.py:136` ("... carries success or failure.") and
  `cli/ci.py:131` ("Suppress stderr notes and summary.", which gains "Errors still print.").

Converted in CL1 (drifted or local emitters outside the hot lanes): `cli/unmask.py:135`,
`cli/compile_explain.py:319`, `cli/report.py:1708`, `:2220`, `cli/subset.py:484`, `:505`,
`cli/fit.py:403`, `cli/tokenize.py:180`, `:520`, `cli/scanning/_multi.py:216`,
`cli/gen/_scaffold.py:164`, `cli/reference.py:272-284`, `:300-313`. Converted in CL4:
`cli/run/_errors.py`, `cli/run/_evidence.py`, `cli/run/_notify.py`, `cli/scan.py`. Every other
`err_console.print(error(` site stays on the D7 baseline (CX-1 full conversion is V1+). AT-CL1-6
proves each of their `"status": "error"` literals already fits the schema.

### 4.2 C2: one exception-to-exit map (CLI)

Owner: `src/decoy/cli/exit_codes.py` (existing module), new
`exit_code_for(exc: BaseException) -> int`. It is a pure function from exception type and code
to an exit code, with the categories of the classifier now in `cli/run/_errors.py:211-232`
(pre-run check, capacity codes, `MemoryError`, `UNIQUENESS_IMPOSSIBLE_CODE`, usage types), the
`RUN_EXIT_MAP` categories, `ConfigFileError` (1), `OSError` on a config read (3) and the engine
`EnvFlagError` (1). It also owns `fit`'s DP categories: `ProvenanceError` exits 3;
`DpBudgetError` exits 3 only for `dp_schedule_mismatch` and 1 otherwise; `DpError` and
`CarrierError` exit 1. It imports the engine and CLI exception types inside the function, so
the existing `decoy.ui.output -> decoy.cli.exit_codes` ignore line gains no new reach and the
CI modules that import `exit_codes` do not load the engine.

Build order (fixes the round 1 CL3 and CL4 order): CL3 builds `exit_code_for` with its unit
tests and a parity test against the `_errors.py` classifier for every category (AT-CL3-7), and
the CL3 loader callers use it. `_errors.py` is a hot lane, so its classifier stays until CL4,
where `run` (`_errors.py`), `demo` (`demo.py:582-590`) and `fit` (`fit.py:663`, `:724`) call
`exit_code_for` and the `_errors.py` copy is deleted. Between CL3 and CL4 the exit-code owner
sentry has the exact baseline `{run/_errors.py classifier, demo map, fit DP arms}`. It only
shrinks, and CL4 empties it. `gate_exit_code`
(`gate/_layers.py:391`) maps gate findings, not exceptions, and stays; its error-finding exit
must equal `exit_code_for` of the matching loader error, which AT-CL3-6 pins.
`KeyboardInterrupt` keeps Typer's 130 (`RUN_EXIT_MAP` cancelled). RESULT C-EXIT (CLI #228,
exit map and exit 6) merged on 2026-11-12; CL4 waits only for the remaining row 62 C slices
that touch `_errors.py`, and for rows 63a and 66.

### 4.3 C3: one canonical JSON owner with two named forms (engine)

Owner: new private package `src/decoy_engine/_canonical/` (`__init__.py` re-exports from
`_json.py`), in import tier 6 so every tier, including `phase` (tier 5), can call it. A root
module is not possible: 24zd's `_arrow_types.py` brings the root to the 25-module fan-out cap,
and the cap's rule is that the next one uses a named sub-package.

```python
def canonical_json_bytes(value: object) -> bytes: ...     # the digest form
def seed_fingerprint_bytes(value: object) -> bytes: ...   # the seed form, byte-pinned
class CanonicalJsonError(ValueError): ...  # value-free: names the Python type, never the value
```

Shared rules (both forms): `sort_keys=True`, `separators=(",", ":")`, `allow_nan=False`, UTF-8
output. A closed `default` converts `datetime.datetime` with a UTC offset and `datetime.date`
(that is not a `datetime`) to `isoformat()`. It refuses a naive `datetime` (the records codec
refuses it on purpose at `records/_codec.py:97-102`: reading it as host time would make the
bytes depend on the machine), and every other non-JSON type, including `Decimal`, `set`,
`bytes` and arbitrary objects. Refusals and NaN or Infinity raise `CanonicalJsonError`, raised
after the encoder returns and outside any handler, so it carries no `__context__` holding the
value. Because it is a `ValueError`, the records codec's existing `except ValueError` arms
(`records/_codec.py:105-113`, `:150-156`) keep turning every refusal into the coded
`RecordError(RECORD_INVALID)`.

- `canonical_json_bytes`: `ensure_ascii=False`. This is the records rule and the platform's, so
  `records/_canonical.py` `canonical_bytes` delegates to it with identical bytes for every value
  it encodes today: no record id and no CLI evidence manifest hash moves.
- `seed_fingerprint_bytes`: `ensure_ascii=True`, kept byte-for-byte as
  `generators/derivation.py:204` and `generation/pool/_builder.py:39-44` write today, and frozen
  under `SEED_PROTOCOL_VERSION`: changing it is a seed protocol change. Its two named callers
  are `strategy_config_fingerprint` and `_config_hash`. `_config_hash` feeds
  `_derive_pool_seed`. Every compile-reachable config reaches both with the same bytes as today,
  including non-ASCII provider extras, so no per-column seed, pool seed or generated output
  moves (AT-E1-2).

Pre-compile refusal: `plan/_compile.py` `_hash_config` rejects `datetime.date` and every
`datetime.datetime` before calling `canonical_json_bytes`, then turns that refusal and every
`CanonicalJsonError` into a `PlanCompileError` with the new config reason code
`config_value_not_json` ("A config value has a type JSON cannot hold; quote it as a string."),
naming the type only. `_hash_config` runs at `:432`, before the seed envelope at `:443`, the pool
builder and every other routed site, so a refused value fails `validate`, `run` and `ci check`
with exit 1 and a code. Pydantic keeps date objects in `provider_config` and extra column fields,
so AT-E1-3 starts red. Today such a date or datetime raises a bare `TypeError`; `.nan` and `.inf`
encode as `NaN` or `Infinity`. After, all four are refused with the code (section 5).

The `subset:` block is deliberately excluded from plan compilation at
`plan/_compile.py:184`, so its `Predicate.value` and `SeedSpec.keys` literals do not pass through
`_hash_config`. `subset_request_from_config` is the second refusal point. It constructs the
`SubsetRequest`, validates its canonical fingerprint, and converts `CanonicalJsonError` to a
value-free `SubsetConfigError` with `config_value_not_json` and the hint "add a UTC offset or
quote it" in its fixed message. This covers both `decoy subset` and subset-on-run before selection begins. The other
routed `default=repr` site, `execution/physical/_inputs.py:94`, hashes the full config including
`sources` and `targets`; those models are strictly typed, and all free-form config content has
already passed `_hash_config`, so it has no separate unvalidated literal path.

Sites routed through `canonical_json_bytes` (pure digest or signature inputs; section 6.2,
Fields, gives each variant): `records/_canonical.py` (delegates; `canonical_bytes` keeps its
name), `phase.py:481`, `generation/_reproducibility.py:163-177` (`PinnedEnvelope.digest`) and
`:204` (`config_digest`), `generation/_pinned_plan.py:200`, `plan/_compile.py:660`,
`execution/physical/_inputs.py:94`, `subset/_scope.py:41`, `profile/_serialize.py:74-79`
(`profile_hash`), and `profiling/model_pack/provenance.py:200`. Through
`seed_fingerprint_bytes`: `generators/derivation.py:204` and
`generation/pool/_builder.py:39-44`.

Exact allowlist (D3), each keyed on `(path, function)` with its kind:

| Site | Kind | Why it stays |
|---|---|---|
| `vault.py:363` | stored file header | frozen vault file format, not a digest input |
| `declared_casting/_quarantine_local.py:251` | stored file header (`DQA1`) | the same category as the vault header; the header is written, not hashed |
| `declared_casting/_catalog.py:293` | in-process cache key | an `lru_cache` key that `_resolved_json` parses back; it never leaves the process |
| `declared_casting/_catalog.py:481-486` | in-process formatter/parser cache key | parsed back inside the same process; not a digest or seed input |
| `declared_casting/_quarantine_local.py:271-279`, `:327-329` | stored encrypted row and stored trailer | persisted format lines, not digest constructors; the trailer carries an already-computed digest |
| `plan/_generation.py:411` | executed payload | `config_json` is `json.loads`-ed back (`_pinned_compile.py:284`, `physical/_shadow_generation.py:248`); its exact bytes are hashed only against themselves in the same process (`physical/_compiler.py:382`, `_plan.py:203-206`) |
| `generation/_pinned_compile.py:299`, `:319` | executed payload | `spec_json` and `config_json` are `json.loads`-ed back in `_pinned_execute.py:82-84` |
| `profile/_serialize.py:33` | stored document | compact profile JSON read by `profile_from_json`; its document bytes are not hashed |
| `profiling/model_pack/_packio.py:67`, `profiling/model_pack/provenance.py:183`, `profiling/model_pack/trainer.py:592`, `subset/_api.py:217` | indented document for people | no allowlisted indented document is hashed by a reader; the model-pack signature uses the parsed dictionary and the loader hashes model bytes only |

`quarantine.py:320` and `execution/_isolated_worker.py:70` are not `sort_keys` dumps and not
digests. The `default=str` in `plan/_generation.py:411` and `_catalog.py:293` stays with those
sites and is logged in section 13.

Digests that move, and only for content that hits a changed rule:

- non-ASCII content: `pipeline_config_hash` (`plan/_compile.py:660`), the phase envelope bytes
  (`phase.py:481`), the pinned-plan fingerprint (`_pinned_plan.py:200`), the model pack
  signature scope (`provenance.py:200`; a pack signed over non-ASCII manifest text must be
  re-signed), `profile_hash`, `PinnedEnvelope.digest`, the physical-input identity hash and the
  subset scope fingerprint.
- default separators or `default=repr`: the physical-input identity hash and the subset scope
  fingerprint (every config).
- `default=str` content: the reproducibility `config_digest`; non-JSON config types are now
  refused before this site runs.

Pre-GA, each is a dated compatibility note (section 9). A pinned plan or resume fingerprint
(engine #562 keys on `pipeline_config_hash`) made before the change refuses with its existing
mismatch code and must be rebuilt; the changelog `Breaking:` line says so.

### 4.4 C4: seed derivations (engine)

- **A1.** In `transforms/joint_mask.py`, both RNGs come from
  `seed_bytes_to_int(derive(job_seed, namespace, source)[:8])` where `source` is
  `b"joint_mask/null_fallback/v1"` or `b"joint_mask/gen_row/v1"` followed by length-prefixed
  UTF-8 of the table name, the reference name and the `\x1f`-joined target columns. Length
  prefixes keep `("ab", "c")` and `("a", "bc")` apart. The keyed path for non-null keys
  (`derive(job_seed, namespace, _KEYED_ROW_SOURCE)`) is unchanged, so keyed selection output
  does not move. The table name reaches `apply_joint_mask` as a new required keyword from
  `execution/_strategies/_joint_mask.py:123`; if `plan.namespace` already carries the table,
  the builder still adds it (one rule, no inference).
- **A4.** `_seed_bytes_to_int` moves from `generation/pool/_sampler.py:168` to
  `determinism/_seeds.py` as `seed_bytes_to_int(seed: bytes) -> int`, which refuses any length
  but 8 with a value-free `ValueError`. Its callers are `_sampler.py:270`, `:518`,
  `generation/_faker_pool.py:459`, `execution/native/protocol/providers_gen.py:75`, and A1.
  The five Python composite build sites (`composite/_person.py:143`, `_name_email.py:135`,
  `_address.py:97`, `_provider.py:117`, `_custom.py:207`) keep today's
  `int.from_bytes(seed, "big")`. Their single owning check is `resolve_seed`, which accepts only
  the existing 8-byte job seed or 32-byte deterministic mask key and returns either unchanged.
  This preserves the full 32-byte integer used by keyed composite masking on main.
- **A3, one seed per spec (OQ-5, closed as (a)).** `determinism/_seeds.py` gains
  `fresh_seed() -> bytes` (8 bytes of `os.urandom`) and `resolve_seed(seed: bytes | None) ->
  bytes`. It returns an 8-byte seed unchanged, returns the existing 32-byte deterministic
  composite mask key unchanged, refuses every other supplied length, and draws one
  `fresh_seed()` for `None`. Each composite's two
  public entry points, `generate_bundle` and `build_pool`, start with
  `spec = dataclasses.replace(spec, seed=resolve_seed(spec.seed))` before any draw, and
  `generate_bundle` passes that resolved spec to `build_pool`, `_street_pool` and the sampler.
  The rule is idempotent, so a resolved spec passes through unchanged and the street pool, the
  bundle build, the `BundlePool.seed` metadata and the sample of one call all use the same seed.
  The 13 all-zero fallbacks go:
  `generation/composite/_address.py:70` (street pool), `:96` (bundle build);
  `_city_state_zip.py:86` (`BundlePool.seed`); `_custom.py:184`, `:206`;
  `_name_email.py:81`, `:134`; `_person.py:79`, `:142`; `_provider.py:78`, `:116`; and the
  native mirror `execution/native/protocol/providers_gen.py:94`
  (`CompositeBuildPoolProvider.generator`), which now requires a resolved 8-byte seed and
  refuses `None` or empty bytes through `seed_bytes_to_int` (the Python side always resolves
  first, so the mirror never sees a missing seed; it is not the keyed 32-byte Python build
  path). The six `os.urandom(8)` sample fallbacks
  (`_address.py:181`, `_provider.py:204` and the four matching lines in `_custom.py`,
  `_name_email.py`, `_person.py`, `_city_state_zip.py`) read the resolved `spec.seed` instead.
  `ProviderSpec` is a frozen dataclass whose validator accepts 8 or 32 bytes. `resolve_seed`
  repeats that boundary at the draw choke point, and the 32-byte path keeps its existing
  conversion. No production path passes `None` today (LATENT); the rule matters for library
  callers and frozen behaviour. Every seeded spec, including deterministic composite masking
  with a 32-byte mask key, is byte-identical to main.
- **Draw-site registry.** `execution/native/_draw_sites_mask.py` gains
  `mask.joint_mask_null_fallback` and `mask.joint_mask_gen_row` entries beside
  `mask.joint_mask_keyed_row` (`:205`), and the `gen.composite_build_pool` entry
  (`_draw_sites_gen.py:296`) and the provider docstring (`providers_gen.py:69`) change their
  seed text to "`resolve_seed(spec.seed)`, one per spec". This is unconditional.
  `tests/native/test_draw_sites_catalogue_pinned.py` pins the catalogue's content (length 40,
  order, entry digests); E2 re-captures it with the two added ids and the changed entry in the
  same diff. `execution/native/protocol/providers_mask.py` gains matching providers for both new
  ids, with methods that reproduce the new `derive` source bytes exactly. E2 adds one scenario
  and output golden for each provider in `tests/native/test_draw_site_providers_golden.py`, then
  re-captures `_GOLDEN_REGISTRY` and the `MASK_STRATEGY_TO_SITE` digest in
  `test_draw_sites_catalogue_pinned.py`. These are content snapshots, not ceilings or
  baselines: they grow exactly by the reviewed entries and the gate diff shows each one.

### 4.5 C5: one config loader and one secret-reference rule (CLI)

Owner: new `src/decoy/_config_file.py` (private shared primitive, CLI CODEMAP row "Private
shared filesystem or process primitives"). The leak-safe YAML text helper moves down with it:
`src/decoy/cli/_yaml_errors.py` (22 lines, imports nothing from `decoy`, loads the engine inside
the function) becomes `src/decoy/_yaml_errors.py`, so the loader does not import upward. Its
seven importers (`compile_explain.py:44`, `gate/_layers.py:20`, `preflight.py:60`,
`report.py:50`, `subset.py:39`, `tokenize.py:34`, `unmask.py:24`) change their import line, and
the flat `cli/` fan-out pin falls from 30 to 29.

```python
def read_config_text(path: Path) -> str: ...          # bytes once, strict UTF-8, BOM stripped
def parse_config_mapping(text: str) -> dict[str, Any]: ...
def load_config_mapping(path: Path) -> dict[str, Any]: ...  # read_config_text + parse
class ConfigFileError(Exception):  # .code, value-free .message
```

Steps and codes (fixed now; the gate's codes are public, so the loader uses them):

| Step | Failure | Code | Message |
|---|---|---|---|
| read bytes | `OSError` | none: re-raised unchanged | runtime class, exit 3 (the environment failed, not the config) |
| strict UTF-8 decode | `UnicodeDecodeError` | `yaml.not_utf8` (new) | "Pipeline YAML is not UTF-8 text." |
| `yaml.safe_load` | `YAMLError` | `yaml.parse_error` | `sanitize_yaml_error(exc)` |
| empty | value `None` | `yaml.empty` | "Pipeline YAML is empty." (today's gate text) |
| not a mapping | any non-`dict` | `yaml.not_mapping` | "Pipeline YAML must be a YAML mapping (object), not <type>." (today's gate text) |

Every `ConfigFileError` is raised after the handler closes, so it has no `__context__`. The new
`yaml.not_utf8` code joins `ci/rule_codes.py:61-63`, `docs/ci-evidence/verification.md` and
`tests/unit/test_ci_rule_codes.py` in CL3.

The gate keeps one owner, not two: `parse_pipeline_yaml(config_path, source_text=None)`
(`gate/_layers.py:91-104`) becomes a thin adapter. It calls `read_config_text` when
`source_text` is None and `parse_config_mapping` on the text, and wraps a `ConfigFileError` in
`GateFinding("config", "error", exc.code, exc.message)`. Its return keeps its shape. The empty
and non-mapping checks in `_run_config_base` (`:136-150`) are deleted, since every raw value
the gate sees now comes from the loader: `run_gate`, `run_config_gate`, `gate/_run.py:142`
(through `parse_pipeline_yaml`), and `gen/_scaffold.py:141-144`, which switches its own re-parse
of the text it wrote to `parse_config_mapping`. `run_gate_on_config`'s `raw` parameter is typed
`dict[str, Any]`.

Because `run` already parses through `parse_pipeline_yaml` (`cli/run/_prepare.py:64`), CL3
gives `run` the `yaml.not_utf8` refusal (exit 1, before any read or write) without touching the
run lane: the adapter returns a finding instead of raising `UnicodeDecodeError`. CL4 then
deletes the dead `UnicodeDecodeError` arm in `_prepare.py:56-67` (the census FU-7 item).

Callers moved to `load_config_mapping` in CL3: `api.py:93`, `unmask.py:155`, `report.py:1748`,
`:2003`, `:2241`, `preflight.py:686-699` (its three `acc.add_fail` arms become one that copies
the loader's code and message), `subset.py:156`, `tokenize.py:201`, `:539`,
`compile_explain.py:175`. Each caller turns a `ConfigFileError` into `emit_error(...,
code=exc.code)` and exits with `exit_code_for(exc)`.

YAML readers that stay outside the loader, on the D8 allowlist keyed on `(path, function)` with
their kind:

| Site | Kind | Why |
|---|---|---|
| `ci/manifest.py:106` | CI manifest | strict `_StrictLoader`, its own `ci.manifest_invalid` code and event check |
| `ci/locations.py:60` | source index | `yaml.compose` of text the gate already loaded, for SARIF line marks |
| `report.py:1761` | policy file | a different file kind where empty means "no policy" (`or {}`) |
| `scan.py:243` | fixed-width layout file | YAML with a JSON fallback, not a pipeline config; `cli/scan.py` is also a hot lane (stop line 4) |
| `plan.py:44`, `demo.py:229`, `init.py:124`, `:387`, `:408`, `:750` | CLI-written text | re-parses of text the CLI wrote itself |
| `io/_project.py:89` | user-editable project marker | its own trust class, bounded reader and `PROJECT_MARKER_INVALID` refusal |

Secret reference (F1, B8): `mask_secret_ref_problem(raw) -> str | None` in `_mask_secret.py`
calls `decoy_engine.secret_providers.parse_ref` (public, exported at
`secret_providers/__init__.py:59` in the pinned 0.23.0) and then checks the scheme against the
CLI's provider set (`env`, `file`; `vault` stays refused with `secret_provider_unknown`, as CLI
#204 pins). It returns the one `INVALID` text or `None`; the four copies call it. The engine
parser's error is bare and value-free by construction (`secret_providers/_types.py:94-110`).

### 4.6 C6: CLI JSON to stdout only through `ui/output.py`

`ui/output.py` gains `emit_json_document(state, doc, *, indent: int | None) -> None` beside
`emit_json`. It writes `json.dumps(doc, indent=indent) + "\n"` to stdout and flushes, and only in
default mode; a call in `--json` or `--quiet` mode is a programming error (`ValueError`). Both
callers already guard their mode: `decoy schema` returns early under `--quiet` and uses
`emit_json` under `--json`; the streamed scan refuses `--json` and `--quiet` at
`_multi.py:421-428`.

- `cli/schema.py:91-92` calls it with `indent=2`.
- `cli/scanning/_multi.py:471` (`decoy scan <src> --output -`) calls it with `indent=None`.

`json.dumps` keeps its default `ensure_ascii=True` in both, so stdout bytes do not change
(AT-CL2-4). Routing the streamed scan through `emit_json` would write nothing (the round 1
defect); AT-CL2-4 is the alarm for that mutation.

### 4.7 C7: one flag truthiness rule per repo

One accepted set in both repos, compared after `strip().lower()`: true `1`, `true`, `yes`, `on`;
false `0`, `false`, `no`, `off`. Unset or empty gives the flag's default. This is the engine's
existing strict set (`loader.py:57-58`). The pre-mortem no longer claims every `strtobool`
spelling: `y`, `t`, `n` and `f` were never accepted by the engine and are not added.

Engine owner: `src/decoy_engine/internal/env_flags.py`, `env_flag(name: str, *, default: bool)
-> bool`. `EnvFlagError(DecoyError, ValueError)` is defined beside the other public errors in
`decoy_engine/errors.py`; `internal/env_flags.py` imports it from there. An unknown value raises it with
`code = reasons.config.ENV_FLAG_INVALID` (new: `env_flag_invalid`, "An environment flag has a
value outside the accepted spellings."), a `.variable` attribute, and a message that names the
variable and the accepted spellings, never the value. This keeps the tier-6 `errors` module
from importing upward into tier-4 `internal`, and the CLI catches the public error without
reaching into `internal/`.
Engine sites and what an unknown value does:

| Site | Today | After |
|---|---|---|
| `profiling/model_pack/loader.py:81-98` `DECOY_PACK_REQUIRE_SIGNATURE` | raises `ModelPackLoadError` with the raw value in its text; `load_with_fallback` logs it and uses the regex baseline (no pack loaded) | `EnvFlagError`, value-free; `load_with_fallback`'s generic arm still returns `None`, so the outcome is the same (no pack, regex baseline) and the warning no longer holds the value; `_classifier` catches it in its generic arm and reports `down("pack")` |
| `loader.py:184-186` `DECOY_ML_DISABLED` | only `"1"` disables; any other value leaves ML on | the accepted set; an unknown value raises `EnvFlagError` from `is_ml_disabled` |
| `_detectors.py:71` (`_classifier`, "Never raises") | reads `is_ml_disabled` outside its `try` | catches `EnvFlagError` and returns `down("disabled")`: an unknown value on a switch meant to turn ML off leaves ML off |
| `execution/_isolated_run.py:221`, `:238` | `== "1"` and `!= "1"` on the engine-set worker marker; a stray user value such as `yes` acts false | `env_flag(ISOLATED_WORKER_ENV, default=False)`; the engine sets `"1"`, read as today; any accepted true spelling acts true, and an unknown spelling raises `EnvFlagError` during worker bootstrap |
| `internal/faker_setup.py:223` | `CUSTOM_PROVIDERS_DIR`, a path | renamed `DECOY_CUSTOM_PROVIDERS_DIR`, no alias (OQ-7, closed); not a flag |

`execution/_isolated_run.py:469` sets the marker and is not a read. `loader.py:351` and
`provenance.py:282` read the signing key (secret material, "non-empty means set"), not a flag,
and stay. `connectors/sftp.py:138` reads a path and is out of scope.

CLI owner: `src/decoy/_env_flags.py`. Every CLI flag fails safe and never exits, so the CLI has no
refusing parser:

- `safe_env_flag(name: str, *, default: bool, unknown: bool) -> bool`: unset or empty gives
  `default`, an accepted spelling gives its value, anything else gives `unknown`. Two values,
  no behaviour switch.
- `is_ci() -> bool`: `CI` unset, empty or a false spelling means not CI; any other value means
  CI (the CI convention).
- `no_color() -> bool`: any non-empty `NO_COLOR` (no-color.org); today's `is not None` also
  counted the empty string, which the spec excludes.
- `env_int(name: str, default: int) -> int`: refuses a non-integer with a value-free
  `ValueError` naming the variable.

| CLI site | Rule | Unknown value |
|---|---|---|
| `_update_check.py:41-43` `DECOY_NO_UPDATE_CHECK` | `safe_env_flag(..., default=False, unknown=True)` | opted out: no network check, no exit (OQ-8 ruling). `DECOY_NO_UPDATE_CHECK=0` still runs the check |
| `_update_check.py:48` `CI` | `is_ci()` | CI |
| `__main__.py:141` `DECOY_DEBUG` | `safe_env_flag(..., default=False, unknown=False)` | debug off (tracebacks can hold row data, so the safe side is off; read in the global handler, so it must not raise) |
| `ui/output.py:47` `NO_COLOR` | `no_color()` | colour off |
| `cli/run/_notify.py:165` `DECOY_NOTIFY_SMTP_PORT` (CL4) | `env_int` | a named, value-free notification error for the SMTP target; other targets still send and the run exit is unchanged, as other notification failures are |

`_update_check.py:100` (`DECOY_VERSION_URL`) is a URL, kept out (B11 is V2-LOG).

The CLI shows the engine flag error: `exit_code_for` maps `EnvFlagError` to 1, and the global
handler in `__main__.py` gains one arm that, for an `EnvFlagError`, prints its value-free
message and code through `emit_error` instead of `internal error (EnvFlagError)`. `run` shows it
through its reason-code arm. This lands in CL5, after the repin that carries `EnvFlagError`.

### 4.8 C8: one generated-at shape (engine)

`profiling/types.py:193` becomes `datetime.now(timezone.utc).isoformat()` (OQ-6, closed as
`+00:00`), the shape the other three writers use. Readers that accept `Z` keep accepting it, so
older profile files still load.

### 4.9 C9: one NER mismatch message (engine)

`_text_mask.py:134-149` and `out_of_core/_mask_group_c.py:344-357` call
`enforce_ner_model_version` (`_ner_config.py:77`) with `strategy="text_mask"` and their column,
and their inline raises go. The wording is the helper's. The model name and versions are names,
not data values. The NER config-parse copy in `_mask_group_c.py:320-340` stays (census E1,
section 13).

### 4.10 C10: identifier locale rule

Routed to GEN-PROVIDERS (row 24p) by the owner (2026-11-12). This row builds nothing for it and
records the route so REL-GATE's checklist finds it in 24p.

### 4.11 Design notes

- **Why two canonical forms and not one.** One rule for every digest is the goal, but the seed
  fingerprint is a seed input: moving its bytes moves generated output for every non-ASCII
  config, silently. Naming it as a second form in the same owner keeps one module to review, one
  set of type rules, and makes the frozen form visible instead of a stray `json.dumps`. The two
  forms differ in exactly one argument.
- **Why the loader is split in three functions.** The gate takes `source_text` (the CI adapter
  passes text it already read), so decoding and parsing must be callable apart.
  `load_config_mapping` is the one call every command uses; the two halves exist for the gate
  and the scaffold.
- **Why the CLI has no refusing flag parser.** Every CLI flag is either an opt-out of a side
  effect (`DECOY_NO_UPDATE_CHECK`) or a debug aid read inside the crash handler
  (`DECOY_DEBUG`). Refusing would make a typo exit 1 on every command, and a refusal inside the
  global handler would itself be a crash path. Engine flags gate behaviour inside a run, where a
  coded refusal is visible and the safe outcome is defined per caller.
- **Why `resolve_seed` sits at the composite entry points and not inside each draw.** Resolving
  per draw is what produced three different seeds for one spec. Resolving at entry, once, and
  passing the resolved spec down keeps one seed per spec by construction, and the sentry forbids
  the fallback literal so a new draw cannot add its own.
- **Design principles applied.** Single responsibility for each owner module (one fact, one
  place); no flag arguments that switch behaviour (`safe_env_flag` takes two values, not a
  mode); the open/closed lens does not apply, since no growing variant set is added.
- **What stays duplicated on purpose.** `gate_exit_code` (findings, not exceptions), the
  allowlisted YAML readers (different file kinds), and the allowlisted `sort_keys` dumps (stored
  headers, executed payloads, documents for people). Each list is exact and a test fails when an
  entry stops matching a real site.

## 5. Behaviour

| Input | Today | After |
|---|---|---|
| `decoy fit --json` on a refused source with a code | `{"code", "message", "error": "<code>: <message>"}` | `{"code", "error": "<message>"}` (OQ-2) |
| `decoy unmask --quiet` with a bad vault path | nothing on stderr, exit 3 | `error: ...` on stderr, exit 3 (OQ-1) |
| `decoy scan` (multi) error text holding `[vault]` | bracket text dropped | printed verbatim |
| `decoy demo` failure that `run` maps to 5 | exit 3 | exit 5 (OQ-3) |
| `decoy run` on a non-UTF-8 config | exit 3, runtime error text | exit 1, `yaml.not_utf8` (24b rule) |
| `decoy validate`, `tokenize`, `unmask`, `report`, `subset`, `preflight`, `compile-explain` on a non-UTF-8 config | traceback path to the global handler, or exit 3 | exit 1, `yaml.not_utf8`, same text as `run` |
| `decoy scan <src> --output -` | compact JSON on stdout | the same bytes |
| `decoy schema` (default mode) | indented JSON on stdout | the same bytes |
| `--mask-secret ENV:NAME` | refused as malformed | parsed as `env:NAME` (engine rule) |
| two `joint_mask` groups, null keys or gen mode | same stream for both groups | independent streams; output bytes change once |
| `joint_mask` non-null keyed rows | keyed by `derive(..., _KEYED_ROW_SOURCE)` | unchanged |
| deterministic composite masking with a 32-byte mask key | full key converted with `int.from_bytes` | byte-identical; the same full key conversion remains |
| config holding an unquoted YAML date (`2024-01-01`) | bare `TypeError` from `_hash_config` at compile (`plan/_compile.py:432`); pydantic keeps the `date` object | refused at compile with `config_value_not_json`, exit 1; quote it as a string |
| config holding a YAML datetime, naive or timezone-aware | bare `TypeError` at compile | refused at compile with `config_value_not_json`, exit 1; quote it as a string |
| subset predicate holding a naive YAML timestamp (`2024-01-01 10:00:00`) | reaches subset selection and may run | `subset_request_from_config` refuses it with `config_value_not_json` and fixed quote-or-add-offset guidance; `decoy subset --dry-run` and subset-on-run exit 1 without a traceback |
| config holding `.nan` or `.inf` | `_hash_config` encodes `NaN`; compiles | refused at compile with `config_value_not_json`, exit 1 (the review found no such config in either repo) |
| config with non-ASCII text | digests escape it as `\uXXXX`, except records | every digest writes UTF-8, as records already does; seed fingerprint and generated output unchanged; record ids and evidence manifest hashes unchanged |
| unseeded composite spec (library call) | pool from zeros, sample from fresh entropy | one fresh seed per call for pool and sample |
| `CI=false decoy run` | treated as CI (no update notice) | not CI |
| `DECOY_NO_UPDATE_CHECK=y` | opted out | opted out (unknown spelling, safe side) |
| `DECOY_NO_UPDATE_CHECK=0` | check runs | check runs |
| `DECOY_DEBUG=true` | debug off (only `"1"`) | debug on |
| `DECOY_ML_DISABLED=true` | ML stays enabled | ML disabled |
| `DECOY_ML_DISABLED=maybe` | ML stays enabled | classifier reported down (`disabled`), scan uses the regex baseline |
| `DECOY_PACK_REQUIRE_SIGNATURE=maybe` | `ModelPackLoadError` holding the value; pack not loaded, regex baseline, warning logs the value | `EnvFlagError` (`env_flag_invalid`), value-free; fallback loader returns no pack and classifier status is down with cause `pack` |
| isolated-worker marker set to `yes` | treated as false, so the isolated route may spawn again | treated as true, so the worker runs in-process and does not recurse |
| isolated-worker marker set to an unknown spelling | treated as false | `EnvFlagError` (`env_flag_invalid`), value-free, before the worker route proceeds |
| `DECOY_NOTIFY_SMTP_PORT=abc` | "invalid literal for int()", no notification | named notification error, no value, other targets still sent, run exit unchanged |
| profile `generated_at` | `...Z` | `...+00:00` |
| NER model mismatch on the out-of-core route | short wording | the helper's wording, same code |

## 6. Failure modes

### 6.1 Class table

| # | Class | Structure that closes it | Proof | Mutation that turns it red |
|---|---|---|---|---|
| 1 | Value-bearing output | `emit_error` takes only a message the caller built; the loader, `CanonicalJsonError`, `EnvFlagError`, `env_int` and `seed_bytes_to_int` errors name a variable, type or field and are raised after the handler, so no `__context__` holds file bytes, a config value, an env value or a key; the signature-flag warning stops logging the raw value | AT-CL1-5, AT-CL3-3, AT-E1-4, AT-E3-3 | put `{exc}` in the decode branch; include the env value in the `EnvFlagError` text |
| 2 | Cleanup missing on an exit path | N/A for new state: no slice adds a file, lock or scratch. The loader refuses before any output is opened, so `run` leaves nothing behind | AT-CL3-8 (no output file) | move the load after the first table write |
| 3 | State and concurrency | N/A: every helper is a pure function of its arguments or of one env read at call time; no module cache is added (an env flag read once at import would miss a test's monkeypatch); `resolve_seed` draws per call, never per process | AT-E3-2, AT-CL2-2, AT-E2-5 | cache `env_flag` results in a module dict; cache `fresh_seed()` in a module global |
| 4 | Route parity and silent fallback | `joint_mask` runs only on the whole-frame route: it is not chunk admitted (`execution/_chunked_carry.py:225-230`), the out-of-core compat rejects it as a cross-row strategy and falls back (`out_of_core/_compat.py:28-35`, `:310-323`), and it is not native eligible (`native/_capabilities.py:376`). A1 changes the one transform that route calls. The loader is the only config path for every command, the gate included | AT-E2-2, AT-CL3-2 | leave the old seed in the gen-mode branch only; admit `joint_mask` to the chunk set |
| 5 | Referential integrity | `joint_mask` keyed selection on non-null keys is unchanged, so a key that maps to a reference row still maps to the same row; null-key rows already had no key relationship | AT-E2-3 | route the keyed path through the new source too |
| 6 | Untrusted producer | a config file and environment variables are user input: every decode, parse, shape and spelling failure maps to one named code or one documented safe side before any field is used | AT-CL3-1 to AT-CL3-4, AT-E3-2, AT-CL2-2 | catch only `YAMLError` again; let `safe_env_flag` raise on an unknown value |
| 7 | Check then use (TOCTOU) | the loader reads the file once and parses those bytes; no caller re-opens the path to re-read | AT-CL3-5 | re-read the path after the decode check |
| 8 | Weak tests and overclaimed evidence | every AT is red on the bases except named controls; byte-identity ATs compare bytes, not exit codes; the `--json` shape is proven for converted sites by running every command and for unconverted sites by an AST check of every literal, not by sampling | AT-CL1-1, AT-CL1-6; gate reviewers rerun the named mutations | relax AT-CL1-6 to the converted files; run AT-CL2-4 in `--json` mode |
| 9 | Behaviour-preserving refactor drift | `seed_fingerprint_bytes` keeps `strategy_config_fingerprint` and `_config_hash` bytes (AT-E1-2); `canonical_json_bytes` keeps `records` bytes (AT-E1-5); `emit_json_document` keeps both stdout documents (AT-CL2-4); `seed_bytes_to_int` keeps `_sampler`'s 8-byte behaviour; `resolve_seed` keeps the 32-byte deterministic composite conversion and output | AT-E1-2, AT-E1-5, AT-E2-8, AT-CL2-4, pool sampler tests | set `ensure_ascii=False` in `seed_fingerprint_bytes`; truncate the composite mask key; route the streamed scan through `emit_json` |
| 10 | Frozen contract drift | every moved surface is listed in section 9 with a dated compatibility note and a `Breaking:` fragment; REL-GATE has not frozen anything yet (row 93 NOT STARTED); goldens may change only as stop line 5 allows; the CLI evidence manifest hash is checked unchanged for non-ASCII content | AT-E2-1, golden suites, AT-CL1-1, AT-CL5-2 | let a golden for a non-`joint_mask` fixture change |

### 6.2 Enumerations

#### Exit paths

| Failure | Commands | Exit after |
|---|---|---|
| config not UTF-8, not YAML, empty, not a mapping | every command that loads a user config (section 4.5 list), `run` (through `parse_pipeline_yaml`) and the gate | 1 |
| config file unreadable (`OSError`) | same | 3, unchanged |
| unexpected exception in a command's generic arm | `run`, `demo` (`cli/demo.py:582-590`), `fit` (`cli/fit.py:663`, `:724`) | `exit_code_for(exc)` (CL4) |
| `config_value_not_json` at plan compile | `validate`, `run`, `ci check` | 1 (compile error map) |
| `config_value_not_json` at subset request construction | `subset`, subset-on-run | 1 (`SubsetConfigError`) |
| engine `EnvFlagError` | any command whose run reads an engine flag | 1, message names the variable (CL5) |
| unknown CLI flag spelling | every command | none: the safe side applies, the command runs |
| Ctrl-C | every command | 130, unchanged |

#### Routes

| Route | C4 (A1) | C3 | C9 |
|---|---|---|---|
| whole-frame mask | `transforms/joint_mask.py:338` (null keys) and `:366` (`mode: gen`, the strategy's own mode, not the generation route) | plan and physical-input hashes | `_text_mask.py:134` |
| generation route (`generate_columns`) | N/A: `joint_mask` is a mask strategy | `generators/derivation.py:204` (seed form) | N/A |
| `--chunked` | N/A: `joint_mask` is not chunk admitted (`_chunked_carry.py:225-230`); the run falls back to the whole-frame route or refuses under the RUN-STREAM strict-switch rule at build time, never per chunk | same hashes | same handler |
| out-of-core | N/A: rejected as `out_of_core_cross_row_strategy_unsupported` (`_compat.py:310-323`), falls back to sequential or full-frame | same hashes | `_mask_group_c.py:344` |
| native | N/A: `joint_mask` excluded (`execution/native/_capabilities.py:376`); stop line 2 if it is not | plan hash only | N/A |

#### FK shapes

| Shape | Effect |
|---|---|
| `joint_mask` `key_by` on a declared FK child column, non-null key | unchanged keyed row, so parent and child rows that share a key still get the same reference row |
| `joint_mask` `key_by` with null keys | fallback rows change; null keys had no relationship to keep |
| any other FK shape | N/A: no key, FK or relationship path changes |

#### Fields

| Field or variant | Today | After |
|---|---|---|
| `--json` error members | `command`, `status`, `error`, `hint`?, per-command context; `fit` also `code`, `message` | `command`, `status`, `error`, `code`?, `hint`?, context |
| digest JSON, compact, ASCII, no default | `phase.py:481`, `generation/_pinned_plan.py:200`, `profiling/model_pack/provenance.py:200` | `canonical_json_bytes` (non-ASCII moves) |
| seed JSON, compact, ASCII | `generators/derivation.py:204`, `generation/pool/_builder.py:39-44` | `seed_fingerprint_bytes` (bytes equal for every compile-reachable input) |
| digest JSON, compact, `default=str` | `generation/_reproducibility.py:204` | `canonical_json_bytes` |
| digest JSON, compact, ASCII, no default | `profile/_serialize.py:74-79`, `generation/_reproducibility.py:163-177` | `canonical_json_bytes` (non-ASCII moves) |
| digest JSON, default separators, `default=repr` | `execution/physical/_inputs.py:94`, `subset/_scope.py:41` | `canonical_json_bytes` (bytes move) |
| digest JSON, explicit `ensure_ascii=True`, compact | `plan/_compile.py:660` | `canonical_json_bytes` (non-ASCII moves), refusal coded `config_value_not_json` |
| digest JSON, `ensure_ascii=False`, compact | `records/_canonical.py:18-26` | `canonical_json_bytes` (bytes equal) |
| stored headers, executed payloads, cache key, documents | section 4.3 allowlist | unchanged |
| config-file codes | `yaml.parse_error`, `yaml.empty`, `yaml.not_mapping` in the gate and preflight only | the same three plus `yaml.not_utf8`, in every command |
| `generated_at` | `profiling/types.py:193` `Z` | `+00:00` |
| env flags | section 4.7 lists | one parser per repo, one accepted set |

#### Output surfaces

- stderr human error line (every CLI command): one emitter, `markup=False`, printed under
  `--quiet`.
- stdout `--json` error object (every CLI command): one shape, validated against the schema.
- stdout JSON documents (`decoy schema`, `decoy scan --output -`): through
  `emit_json_document`, bytes unchanged.
- generated and masked output bytes: `joint_mask` null-key and gen-mode rows only. Deterministic
  composite masking with a 32-byte key stays byte-identical.
- digests in evidence, run results, pinned plans, resume fingerprints, subset manifests and model
  pack signatures: section 4.3 list. Record ids and CLI evidence manifest hashes: unchanged.
- profile documents: `generated_at` suffix.
- notification payloads: unchanged; the SMTP port error is named and value-free.
- logs: the model pack signature-flag warning no longer holds the value.

### 6.3 Guards: rule, owner, guard, seeded spellings, mutation

Every guard is AST-based, resolves import aliases through the existing helpers (engine
`tests/sentry/_call_resolution.py`, CLI `tests/sentry/io_policy/_calls.py`), and carries a
self-test in the same file that feeds it each seeded spelling below and asserts a hit. Every
allowlist keys on `(path, function)` and fails when an entry no longer matches a real site. One
file per concern: where 24ze (row 24ze, NOT STARTED) owns the file under a census name, this row
writes its rule into that file at the path below and 24ze adds its wider baseline to the same
file later, or the other way round if 24ze lands first. The orchestrator records these paths in
row 24ze when this plan passes its gate.

| Rule | Owner | Guard (file) | Seeded spellings | Mutation that turns it red |
|---|---|---|---|---|
| one canonical JSON owner (D3) | `_canonical/_json.py` | engine `tests/sentry/defects/test_canonical_json_choke_point.py` (24ze D3) | `json.dumps(x, sort_keys=True)`, `import json as j; j.dumps(..., sort_keys=True)`, `from json import dumps as d; d(..., default=str)`, `json.JSONEncoder(sort_keys=True).encode(x)` | add `json.dumps(cfg, sort_keys=True)` in `subset/_scope.py` |
| one seed form, frozen | `seed_fingerprint_bytes` | same file: exactly `strategy_config_fingerprint` and `generation/pool/_builder.py:_config_hash` may call it, and goldens pin both byte streams | an import of `seed_fingerprint_bytes` from a third module; direct `json.dumps(..., ensure_ascii=True)` restored in either named caller | call `seed_fingerprint_bytes` from `plan/_compile.py`; restore `_builder._config_hash`'s local dump |
| no unregistered raw-byte generator seed, no all-zero seed (D2 part) | `determinism/_seeds.py` | engine `tests/sentry/defects/test_rng_construction.py` (24ze D2; 24zf adds this rule, 24ze the 68-site baseline); exact allowlist is the five composite build functions whose `ProviderSpec` seed passes through `resolve_seed` | `default_rng(int.from_bytes(s, "big"))`, `n = int.from_bytes(s[:8], "big"); default_rng(n)`, `random.Random(int.from_bytes(...))`, `np.random.Generator(PCG64(int.from_bytes(...)))`, `b"\x00" * 8`, `bytes(8)` as a seed argument; direct composite conversions outside the five `(path, function)` entries | restore `default_rng(int.from_bytes(job_seed[:8], "big"))` in `joint_mask.py`; add a sixth composite conversion; restore `b"\x00" * 8` in `_address.py:96` |
| one engine flag parser (D6 part) | `internal/env_flags.py` | engine `tests/sentry/defects/test_environment_reads.py` (24ze D6; 24zf adds the flag rule) | `os.environ.get("X") == "1"`, `os.getenv("X", "0") in {...}`, `os.environ["X"].strip().lower() == "true"`, `from os import environ as e; e.get("X") != "0"`, `bool(os.environ.get("X"))` | restore `== "1"` at `loader.py:186` |
| one generated-at shape | `datetime.now(timezone.utc).isoformat()` | engine `tests/sentry/profiling/test_generated_at_shape.py` | `.isoformat() + "Z"`, `f"{dt.isoformat()}Z"`, `datetime.utcnow()`, `from datetime import datetime as D; D.utcnow()` | restore `utcnow().isoformat() + "Z"` in `profiling/types.py` |
| one NER mismatch raise | `_ner_config.enforce_ner_model_version` | engine `tests/sentry/defects/test_ner_mismatch_single_site.py` | a raise naming `NER_MODEL_VERSION_MISMATCH` directly, through `reasons.mask.NER_MODEL_VERSION_MISMATCH`, or through a local alias of the code | inline the raise again in `_mask_group_c.py` |
| one error emitter (D7) | `ui/output.emit_error` | CLI `tests/sentry/cli_text/test_one_error_emitter.py` (24ze D7), exact shrink-only baseline | `err_console.print(error(`, `state.err_console.print(error("error:"), ...)`, a dict literal with `"status": "error"`, `dict(status="error", ...)` | add `state.err_console.print(error("error:"), msg)` in `unmask.py` |
| every error literal fits the schema | `docs/schemas/decoy-error-v1.schema.json` | CLI `tests/sentry/cli_text/test_one_error_emitter.py` (AT-CL1-6 lives here) | a baseline literal with `message`, a literal missing `error`, a `code` literal that fails the pattern, or a non-JSON context value | add `"message": ...` or a `set` context value to one baseline literal |
| one exit map | `cli/exit_codes.exit_code_for` | CLI `tests/sentry/defects/test_exit_code_owner.py` (CL3 baseline is exactly `{run/_errors.py classifier, demo map, fit DP arms}` and CL4 empties it; distinct from 24ze D10, which watches literal exit codes) | an `isinstance(exc, ...)` chain returning `EXIT_*` names; a dict from exception types to exit codes | restore `demo.py`'s three-type map |
| one config loader (D8) | `_config_file.py` | CLI `tests/sentry/io_policy/test_config_load_choke_point.py` (24ze D8), kind allowlist of section 4.5 | `yaml.safe_load(path.read_text())`, `import yaml as _yaml; _yaml.safe_load(...)`, `yaml.load(..., Loader=...)`, `yaml.compose(...)`, `YAML().load(...)` | add `yaml.safe_load(p.read_text())` in `tokenize.py` |
| one secret-ref rule | `_mask_secret.mask_secret_ref_problem` | CLI `tests/sentry/test_mask_secret_ref_shape.py` (existing), widened | `raw.startswith("env:")`, `raw.split(":", 1)[0] in (...)`, a regex on `^(env|file):` | restore the case-sensitive prefix test in `tokenize.py` |
| one flag parser per repo (D6 part) | `_env_flags.py` | CLI `tests/sentry/io_policy/test_environment_reads.py` (24ze D6; 24zf adds the flag rule) | as the engine row, plus `os.environ.get("NO_COLOR") is not None` and `if os.environ.get("CI"):` | restore `os.environ.get("CI")` truthiness at `_update_check.py:48` |
| JSON to stdout only through `ui/output.py` (D9 part) | `emit_json`, `emit_json_document` | CLI `tests/sentry/cli_text/test_stdout_writes.py` (24ze D9; 24zf adds the JSON rule) | `sys.stdout.write(json.dumps(x))`, `print(json.dumps(x))`, `typer.echo(_json.dumps(x))`, `s = json.dumps(x); sys.stdout.write(s + "\n")` | restore `sys.stdout.write(json.dumps(core) + "\n")` at `_multi.py:471` |

Group folders: engine `tests/sentry/defects/` holds 4 modules today and gains 4; engine
`tests/sentry/profiling/` gains 1; CLI `tests/sentry/cli_text/` holds 5 and gains 2; CLI
`tests/sentry/io_policy/` holds 17 and gains 2; CLI `tests/sentry/defects/` holds 4 and gains 1.
No file goes in a flat `tests/sentry/` folder, and no group reaches 20 modules.

## 7. Acceptance tests, red first

Every AT below is written first and is red on the bases unless marked control.

### 7.1 Engine

| Id | Slice | Test |
|---|---|---|
| AT-E1-1 | E1 | `canonical_json_bytes({"b": 1, "a": "é"})` equals `'{"a":"é","b":1}'.encode("utf-8")`; `seed_fingerprint_bytes` of the same value equals `b'{"a":"\\u00e9","b":1}'`; NaN and Infinity raise `CanonicalJsonError`; a `set` raises it naming `set` only; `isinstance(CanonicalJsonError(...), ValueError)` |
| AT-E1-2 | E1 | controls then green: for the golden corpus configs plus one non-ASCII config, `strategy_config_fingerprint` returns the same hex before and after; a non-ASCII `provider_config` gives the same `_config_hash` and `_derive_pool_seed` bytes before and after (no column or pool seed moves) |
| AT-E1-3 | E1 | starts red because pydantic keeps date objects: configs holding `datetime.date`, a naive `datetime`, or an aware `datetime` are each refused at `_hash_config` with `config_value_not_json`, exit 1, and the quote-it-as-a-string text; direct `canonical_json_bytes` record callers still encode date and aware datetime with `isoformat()` |
| AT-E1-4 | E1 | `CanonicalJsonError` has `__cause__ is None` and `__context__ is None`, and its text does not contain the offending value's `repr` |
| AT-E1-5 | E1 | control: `records.canonical_bytes` equals its pre-change bytes for a record with non-ASCII text, and `record_id` and `payload_digest` are unchanged |
| AT-E1-6 | E1 | `records._codec.encode` and `build` of a payload holding NaN, a `set` or a naive `datetime` each raise `RecordError` with `RECORD_INVALID` (not an uncoded crash) |
| AT-E1-7 | E1 | an object with a custom `__repr__` in a config reaching `physical/_inputs.py` raises `CanonicalJsonError` instead of hashing its repr; a `.nan` config value is refused at compile with `config_value_not_json` |
| AT-E1-8 | E1 | `decoy subset --dry-run` and `decoy run` with a subset predicate holding a naive YAML timestamp each exit 1 with `config_value_not_json` and the fixed quote-or-add-offset guidance, with no traceback; the same refusal covers a timestamp in subset `keys` |
| AT-E2-1 | E2 | two `joint_mask` groups on one table with all-null keys draw different reference rows for the same seed, and the same rows across two runs |
| AT-E2-2 | E2 | gen mode: same as AT-E2-1 on the whole-frame route; `joint_mask` is absent from the chunk-admitted set and the out-of-core compat rejects it with `out_of_core_cross_row_strategy_unsupported`; a run requested on the out-of-core route falls back and its output equals the whole-frame output |
| AT-E2-3 | E2 | control: non-null keyed `joint_mask` output is byte-identical before and after |
| AT-E2-4 | E2 | `seed_bytes_to_int` refuses lengths 0, 7, 9 and 32 with a value-free `ValueError`; `resolve_seed` rejects a 4-byte seed but accepts 8 and 32 bytes unchanged; the native `CompositeBuildPoolProvider.generator` refuses `None` and `b""` |
| AT-E2-5 | E2 | with `spec.seed=None`: one `generate_bundle` call uses one seed for the street pool, the bundle build, `BundlePool.seed` and the sample (patch `fresh_seed` to count calls: exactly 1); two calls differ; no path uses an all-zero seed; a seeded spec's output is byte-identical before and after (control) |
| AT-E2-6 | E2 | the 8 bytes at `job_seed[:8]` do not appear as the seed of any generator in `joint_mask` (patch `default_rng` and inspect its argument) |
| AT-E2-7 | E2 | the draw-site catalogue lists `mask.joint_mask_null_fallback` and `mask.joint_mask_gen_row`; each has exactly one registered provider, scenario and output golden; `_GOLDEN_REGISTRY` and `MASK_STRATEGY_TO_SITE` are re-captured for those reviewed entries; `gen.composite_build_pool` names `resolve_seed` |
| AT-E2-8 | E2 | control: a deterministic `composite_person` run with a 32-byte mask key completes and its output is byte-identical to main; patching `default_rng` shows the integer argument is exactly `int.from_bytes(the_full_32_byte_key, "big")` |
| AT-E3-1 | E3 | `DECOY_ML_DISABLED` in `1`, `true`, `YES`, ` on ` disables ML; `0`, `false`, empty, unset enables it |
| AT-E3-2 | E3 | `DECOY_ML_DISABLED=maybe`: `is_ml_disabled` raises `EnvFlagError` with code `env_flag_invalid` and `.variable == "DECOY_ML_DISABLED"`; `classifier_status()` reports down with cause `disabled` and does not raise. `DECOY_PACK_REQUIRE_SIGNATURE=maybe`: `load()` raises `EnvFlagError`, `load_with_fallback()` returns `None`, and `_classifier` reports down with cause `pack` |
| AT-E3-3 | E3 | the flag error text and the fallback warning never hold the value (`maybe-secret-xyz` is absent from the message and `caplog`) and the error has no exception context |
| AT-E3-4 | E3 | `DECOY_CUSTOM_PROVIDERS_DIR` is read; `CUSTOM_PROVIDERS_DIR` alone is ignored |
| AT-E3-5 | E3 | `EnvFlagError` is importable from `decoy_engine.errors` and the reason registry lists `env_flag_invalid` and `config_value_not_json` |
| AT-E4-1 | E4 | a fresh `Profile().generated_at` parses with `datetime.fromisoformat` on Python 3.10 and ends `+00:00`; a stored `Z` profile still loads |
| AT-E4-2 | E4 | the out-of-core and whole-frame `text_mask` routes raise `NER_MODEL_VERSION_MISMATCH` with identical message text for the same column and versions |

### 7.2 CLI

| Id | Slice | Test |
|---|---|---|
| AT-CL1-1 | CL1 | for every command with `--json`, a known-bad input gives one stdout object that validates against `decoy-error-v1.schema.json` (parametrized over the command list from `__main__.py`, not hand-picked) |
| AT-CL1-2 | CL1 | `fit --json` with a coded refusal: `code` is the code, `error` does not start with it, no `message` member |
| AT-CL1-3 | CL1 | under `--quiet`, `unmask`, `scan` (multi) and `gen` scaffold all print the human error line to stderr and nothing to stdout |
| AT-CL1-4 | CL1 | an engine message containing `[vault]` reaches stderr verbatim from `fit`, `scan` and `reference` |
| AT-CL1-5 | CL1 | `emit_error` refuses a `context` key that shadows `command`, `status`, `error`, `code` or `hint`, and a `code` such as `Bad Code` or `ok\n` that fails `re.fullmatch` |
| AT-CL1-6 | CL1 | AST check over every `"status": "error"` dict literal left on the D7 baseline: `command` and `error` present, `message` absent, any literal `code` matches `^[a-z][a-z0-9_.]*$`, and every member outside the fixed schema is JSON-typed; list and object context used by run is accepted, while a set is refused |
| AT-CL1-7 | CL1 | every `--quiet` option's help text says errors still reach stderr (scan the Typer app, not the source) |
| AT-CL2-1 | CL2 | `CI=false` and `CI=0`: update check runs as non-CI; `CI=true` and `CI=1`: CI |
| AT-CL2-2 | CL2 | `DECOY_NO_UPDATE_CHECK=maybe`: the command exits 0 and makes no network call (patched fetch never called); `DECOY_NO_UPDATE_CHECK=0` runs the check; `DECOY_DEBUG=maybe` leaves debug off and the command runs |
| AT-CL2-3 | CL2 | `NO_COLOR=` (empty) keeps colour; `NO_COLOR=1` disables it |
| AT-CL2-4 | CL2 | control: stdout bytes of `decoy schema` (default mode) and of `decoy scan <fixture> --output -` (default mode) are identical before and after, and non-empty |
| AT-CL2-5 | CL2 | `emit_json_document` writes in default mode and raises `ValueError` in `--json` and `--quiet` modes |
| AT-CL3-1 | CL3 | a config holding `\xff`: `validate`, `tokenize`, `unmask`, `report`, `subset`, `preflight`, `compile-explain` and `run` each exit 1 with `yaml.not_utf8` and the same text, no codec text or `0xff` on stderr or in `--json` |
| AT-CL3-2 | CL3 | empty file and a YAML list: `yaml.empty` and `yaml.not_mapping` with today's gate text across the same commands and the gate |
| AT-CL3-3 | CL3 | loader errors carry no `__context__` and no file bytes |
| AT-CL3-4 | CL3 | `--mask-secret ENV:NAME` is accepted as `env:NAME` by `run` validation, `tokenize` and the API; `vault://x` is refused with `secret_provider_unknown` in all three; `nope:x` gives the one `INVALID` text |
| AT-CL3-5 | CL3 | the loader opens the path once (patched `open` counter) |
| AT-CL3-6 | CL3 | the gate's exit for each `yaml.*` finding equals `exit_code_for` of the loader's error (1) |
| AT-CL3-7 | CL3 | parity: for one exception of every run category, `exit_code_for` equals the `_errors.py` classifier's exit; it also equals each existing `demo` and `fit` DP arm, including both `DpBudgetError` codes |
| AT-CL3-8 | CL3 | `decoy run` on a `\xff` config writes no output file and no evidence |
| AT-CL3-9 | CL3 | `ci check` on a `\xff` config gives SARIF rule `validate.yaml.not_utf8` and `yaml.not_utf8` is in the rule code registry |
| AT-CL4-1 | CL4 | a failure `run` maps to 5 (capacity) exits 5 from `demo` too; `exit_code_for` agrees with `RUN_EXIT_MAP` for every category and with the removed fit DP arms; the `_errors.py`, demo and fit classifiers are gone and the exit-owner baseline is empty |
| AT-CL4-2 | CL4 | `DECOY_NOTIFY_SMTP_PORT=abc` with a webhook target: the webhook is still sent, the SMTP error names the variable only, and the run exit is the run's own |
| AT-CL4-3 | CL4 | `run --json` error object validates against the schema and keeps the RESULT members |
| AT-CL5-1 | CL5 | after the repin: a `joint_mask` gen run with two groups gives two different column streams; a profile file's `generated_at` ends `+00:00`; a pinned plan compiled on the old engine is refused with its digest-mismatch code |
| AT-CL5-2 | CL5 | control: the CLI evidence manifest hash of a run whose manifest holds non-ASCII text (a table named `données`) equals its value on the old engine |
| AT-CL5-3 | CL5 | set the engine isolated-worker marker to `maybe` on a `decoy run` forced through the isolated route: the global handler prints the variable name and `env_flag_invalid`, not `internal error (EnvFlagError)`, never prints the value, and exits 1; a direct handler unit case covers the same arm if process isolation is unavailable in T1 |

## 7a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| sub-key per purpose | `determinism/_derive.py:260` `derive` | no new KDF |
| length-checked seed conversion | `generation/pool/_sampler.py:168` body, moved | no second copy |
| canonical rule | `records/_canonical.py:18-26` rule, moved to `_canonical/` | records keeps its API |
| seed fingerprint bytes | `generators/derivation.py:204` arguments, named | no new form |
| `--json` writer | `ui/output.py:154` `emit_json` | no second writer |
| YAML parse for `run` and the gate | `cli/gate/_layers.py:91-104` `parse_pipeline_yaml`, now an adapter | no second gate parser |
| config-file codes | `yaml.parse_error`, `yaml.empty`, `yaml.not_mapping` and their messages | no `config.*` codes |
| YAML error text | `cli/_yaml_errors.py` `sanitize_yaml_error`, moved down a tier | no new sanitiser |
| secret-ref parsing | engine `secret_providers.parse_ref` | no CLI grammar |
| strict flag set | `profiling/model_pack/loader.py:57-58` set | no third set |
| run classifier | `cli/run/_errors.py:211-232` categories | no new categories |
| NER guard | `_ner_config.py:77` `enforce_ner_model_version` | no new helper |
| draw-site record | `execution/native/_draw_sites_mask.py` `MASK_DRAW_SITES` | no docstring record |
| sentry scanners | engine `tests/sentry/defects/_defect_scan.py`, `tests/sentry/_call_resolution.py`; CLI `tests/sentry/_ratchet.py`, `tests/sentry/io_policy/_calls.py` | no new AST walker |

## 8. Placement

Checked against each repo's CODEMAP "Where Things Go" at the bases.

| New or moved thing | Path | Repo | CODEMAP row |
|---|---|---|---|
| canonical JSON owner | `src/decoy_engine/_canonical/__init__.py`, `_json.py` | engine | "Foundation (errors, reason adapters, private leaves)"; a sub-package because the root is at the fan-out cap |
| tier entry | `.importlinter` tier 6 gains `_canonical`; `tests/sentry/layout/test_import_layers.py` `LAYERS` | engine | "Layer contracts" |
| seed helpers | `src/decoy_engine/determinism/_seeds.py` | engine | `determinism/` holds `_derive.py`, `_hkdf.py`; tier 4, below its callers |
| engine flag parser and error | `src/decoy_engine/internal/env_flags.py`; `EnvFlagError` defined in `decoy_engine/errors.py`, with `internal/env_flags.py` importing downward from it | engine | `internal/` is engine-private (`tests/sentry/test_public_import_boundary.py`); public errors are defined in `errors.py`; no `errors -> internal` edge |
| reason codes | `reasons/config/_part2.py` (92 lines) gains `ENV_FLAG_INVALID`, `CONFIG_VALUE_NOT_JSON` | engine | reason registry |
| draw-site entries and providers | `execution/native/_draw_sites_mask.py`, `_draw_sites_gen.py`, `execution/native/protocol/providers_mask.py`; provider goldens and catalogue pins in `tests/native/test_draw_site_providers_golden.py`, `test_draw_sites_catalogue_pinned.py` | engine | existing modules and the existing `native/protocol/` sub-package; nothing new in flat `execution/` |
| engine sentries | `tests/sentry/defects/test_canonical_json_choke_point.py`, `test_rng_construction.py`, `test_environment_reads.py`, `test_ner_mismatch_single_site.py`; `tests/sentry/profiling/test_generated_at_shape.py` | engine | "Sentry test": recurring-defect class rules in `tests/sentry/defects/`, package rules in their group |
| engine unit tests | `tests/unit/determinism/`, `tests/unit/transforms/`, `tests/unit/internal/`, `tests/unit/profiling/`, `tests/unit/records/` | engine | "Unit tests": `tests/unit/<package>/` |
| changelog fragments | `changelog.d/dup-freeze-e1.md` to `-e4.md` | engine | "Changelog fragments" |
| compat notes | `docs/compatibility-contract.md` 3.3 dated paragraphs | engine | "Feature and contract docs" |
| error emitter, JSON document writer | `src/decoy/ui/output.py` | CLI | "Terminal output" |
| error schema | `docs/schemas/decoy-error-v1.schema.json` | CLI | beside `docs/schemas/decoy-ci-v1.schema.json` |
| exit map | `src/decoy/cli/exit_codes.py` (existing) | CLI | "Exit codes" |
| config loader | `src/decoy/_config_file.py` | CLI | "Private shared filesystem or process primitives used by more than one package" |
| YAML error text (moved) | `src/decoy/cli/_yaml_errors.py` to `src/decoy/_yaml_errors.py` | CLI | same row; `cli/` fan-out pin 30 to 29 (`tests/sentry/layout/test_package_fanout.py`) |
| secret-ref rule | `src/decoy/_mask_secret.py` | CLI | same row (existing module) |
| CLI flag parser | `src/decoy/_env_flags.py` | CLI | same row |
| CLI layer contract | `.importlinter` (`exhaustive = true`) and the `LAYERS` snapshot in `tests/sentry/layout/test_import_layers.py`, edited in the same diff: the tier `_mask_secret`, `_mask_secret_guidance`, `_native_gate`, `_atomic`, `_row_sample` gains `_config_file` and `_env_flags` as `\|` siblings; the bottom tier `_private_file`, `_version` gains `_yaml_errors`. `_config_file` imports `_yaml_errors` (a lower tier); `_env_flags` imports nothing from `decoy`. No ignore line is added | CLI | "Layer contracts" |
| CLI rule codes | `src/decoy/ci/rule_codes.py` gains `yaml.not_utf8`; `docs/ci-evidence/verification.md`; `tests/unit/test_ci_rule_codes.py` | CLI | existing modules |
| CLI sentries | `tests/sentry/cli_text/test_one_error_emitter.py`, `tests/sentry/cli_text/test_stdout_writes.py`, `tests/sentry/io_policy/test_config_load_choke_point.py`, `tests/sentry/io_policy/test_environment_reads.py`, `tests/sentry/defects/test_exit_code_owner.py`; existing `tests/sentry/test_mask_secret_ref_shape.py` widened in place; `tests/sentry/test_yaml_error_leak_isolation.py` updated for the moved `_yaml_errors` owner | CLI | "User-facing CLI text and crash handling" (`cli_text/`), "Sentry test" (`io_policy/`, `defects/`); the existing root sentry stays in place |
| CLI tests | `tests/unit/test_ui_output.py`, `tests/unit/test_exit_codes.py`, `tests/unit/test_config_file.py`, `tests/unit/test_env_flags.py`, `tests/e2e/first_user/test_items_errors.py` | CLI | "Exit codes", "Terminal output", "First-user QA item tests" |

No module crosses 600 lines: `ui/output.py` grows by about 75 to about 237; `exit_codes.py` by
about 60; each new module is under 150. The CLI `src/decoy/` root holds 13 direct modules today
and gains three (`_config_file`, `_env_flags`, `_yaml_errors`), well under 25. The flat
`src/decoy/cli/` gains nothing and loses one; engine `execution/` gains no module.

## 9. Frozen contracts touched

None is frozen yet: REL-GATE (row 93) is NOT STARTED and `RELEASE_PHASE` is pre-GA. Each change
is recorded the way compatibility contract 3.3 records earlier pre-GA changes: a dated
paragraph, no shim, plus a `Breaking:` fragment where output or a public shape moves.

| Surface | Change | Record |
|---|---|---|
| `--json` error shape (CLI) | `fit` loses `message` and the code prefix; schema published | CLI changelog, schema file |
| `--quiet` error line (CLI) | printed on stderr everywhere; nine help texts say so | CLI changelog |
| exit codes (CLI) | `demo` and `fit` follow `exit_code_for`; `run` non-UTF-8 config 3 to 1; other commands' non-UTF-8 config to 1 | CLI changelog (`Breaking:`) |
| config-file codes (CLI) | `yaml.not_utf8` added; the three `yaml.*` codes now come from every command | CLI changelog, SARIF rule list |
| stdout documents (CLI) | `decoy schema`, `decoy scan --output -`: none | none |
| digest bytes (engine) | section 4.3 list: `pipeline_config_hash`, phase envelope, pinned-plan fingerprint, model pack signature scope, `profile_hash`, `PinnedEnvelope.digest`, physical-input identity, subset scope fingerprint, reproducibility `config_digest` | engine 3.3 note, `Breaking:` (pinned plans and resume state rebuild; non-ASCII signed packs re-sign) |
| record ids, CLI evidence manifest hash, platform agreement | none: the canonical rule is the records rule | none; AT-E1-5 and AT-CL5-2 check it |
| seed fingerprints | none: `seed_fingerprint_bytes` keeps both `strategy_config_fingerprint` and pool `_config_hash` bytes and is frozen under `SEED_PROTOCOL_VERSION` | engine 3.3 note names the form and both callers |
| `joint_mask` output (engine) | null-key and gen-mode rows | engine 3.3 note, `Breaking:` |
| deterministic composite output (engine) | none: the 32-byte mask-key conversion is unchanged | none; AT-E2-8 checks byte identity to main |
| unseeded composite specs (engine library) | one fresh seed per call instead of a zero pool seed | engine changelog |
| seed protocol | `SEED_PROTOCOL_VERSION` unchanged: no `derive` input layout changes, only new sources | none |
| config value types (engine) | dates, datetimes, NaN and Infinity refused at compile; subset date/time literals refused at request construction | engine changelog |
| env variables | the accepted set in both repos; `DECOY_ML_DISABLED`, `DECOY_DEBUG` accept it; `DECOY_NO_UPDATE_CHECK` unknown means opted out; `CUSTOM_PROVIDERS_DIR` renamed `DECOY_CUSTOM_PROVIDERS_DIR`; `CI=false`; `NO_COLOR=` | both changelogs |
| `generated_at` shape | `Z` to `+00:00` in profiles | engine changelog |
| reason codes | engine adds `env_flag_invalid` and `config_value_not_json`; none renamed | engine reason registry, changelog |
| public API (engine) | `decoy_engine.errors.EnvFlagError` added | engine changelog, public API snapshot gains one name |

## 10. Slices

| Slice | Content | Repo | Risk | Depends on | Tests at T1 |
|---|---|---|---|---|---|
| E1 | C3: `_canonical/` with both forms, the routed sites, records delegation, `_hash_config` refusal and `config_value_not_json`, `.importlinter` tier 6, D3 sentry with the kind allowlist, compat note, fragment | engine | R2 (digest bytes) | none; rebase onto 24zd E3 if it lands first (both edit `.importlinter`, a textual conflict only) | AT-E1-*, golden suites, plan, pinned, subset, records, phase and model pack provenance tests, import contract and layer snapshot |
| E2 | C4: A1, A3, A4, `determinism/_seeds.py`, draw-site entries, matching providers, provider scenarios and goldens, catalogue and registry content snapshots, D2 seed rule, compat note, fragment | engine | R2 (output change) | 24zd E1 to E3 merged (keeps golden diffs per row apart) | AT-E2-*, `tests/unit/transforms/`, composite and pool tests, golden suites, `tests/native/test_determinism_protocol.py`, `tests/native/test_draw_site_providers_golden.py`, `tests/native/test_draw_sites_catalogue_pinned.py` |
| E3 | C7 engine: `internal/env_flags.py`, `EnvFlagError` and `env_flag_invalid`, sites, `_detectors.py` arm, rename, D6 flag rule, fragment | engine | R1 | none | AT-E3-*, model pack, detectors, isolated-run and faker setup tests, reason registry tests, public API test |
| E4 | C8 and C9: `generated_at`, NER helper calls, two sentries, fragment | engine | R1 | none | AT-E4-*, profiling and text_mask tests, out-of-core group C tests |
| CL1 | C1 part 1: `emit_error`, schema, the non-hot emitters, `--quiet` help texts, D7 sentry at an exact shrink-only baseline with the AT-CL1-6 shape check | CLI | R2 (public `--json` shape) | none (OQ-1 and OQ-2 closed) | AT-CL1-*, `tests/sentry/cli_text/`, affected command unit and e2e tests |
| CL2 | C6 and C7 CLI (not `_notify.py`): `emit_json_document`, `_env_flags.py`, stdout and env sentries, layer contract entry for `_env_flags` | CLI | R1 | none | AT-CL2-*, update check, schema, scan and output tests, layer snapshot |
| CL3 | C5 part 1 and C2 part 1: `_yaml_errors.py` move, `_config_file.py`, `parse_pipeline_yaml` adapter, `_run_config_base` checks deleted, every loader site except `_prepare.py`, `yaml.not_utf8` rule code, `exit_code_for` with run, demo and fit DP parity, `mask_secret_ref_problem`, D8 sentry with the kind allowlist, exit-code owner sentry with its exact three-entry shrink-only baseline, layer contract entries, `test_yaml_error_leak_isolation.py` import text, and regenerated `docs/diagrams/deps.svg` if the repository gate reports it stale | CLI | R2 (exit codes) | CL1 merged (uses `emit_error`) | AT-CL3-*, gate, validate, ci rule code, tokenize, unmask, report, subset, preflight, compile-explain and scaffold tests, `tests/sentry/test_mask_secret_ref_shape.py`, `tests/sentry/test_yaml_error_leak_isolation.py`, layer snapshot, fan-out sentry, dependency-diagram gate |
| CL4 | Hot lanes: C1 part 2 (`run/_errors.py`, `_evidence.py`, `_notify.py`, `scan.py`), C2 part 2 (`run`, `demo`, `fit` call `exit_code_for`; `_errors.py` classifier deleted; exit-code owner baseline emptied), C5 part 2 (dead `UnicodeDecodeError` arm in `run/_prepare.py`), C7 `_notify.py` SMTP port | CLI | R2 (exit codes, hot lane) | rows 62 (the remaining C slices touching `_errors.py`), 63a (RUN-STREAM C1) and 66 (F29 C1) merged; CL1 and CL3 merged | AT-CL4-*, `tests/unit/test_exit_codes.py`, run error-arm, demo, fit and notify tests, `tests/sentry/cli_text/` |
| CL5 | End-to-end checks of E1 to E4 through the CLI, and the global-handler arm for `EnvFlagError` | CLI | R1 | E1 to E4 in a daily engine release and the CLI repinned to it (row 77) | AT-CL5-* |

E1, E3 and E4 touch no common file and can build in parallel; E2 waits for 24zd. CL1, CL2 and
CL3 need no engine release (`parse_ref` is in the pinned 0.23.0; stop line 7). CL4 is the only
slice that waits on the hot lanes; it is small once they land because CL1 and CL3 already built
the owners. Escalation triggers (`testing.md`): E1 edits `.importlinter` and moves digests
(serialization), so its merge head gets the full engine suite against the stored main baseline;
E2's goldens are the output alarm and must run; CL3 edits the CLI `.importlinter` and moves exit
codes, and CL4 touches exit codes in the hot lanes, so the CLI full suite runs on each of their
merge heads.

Estimate: E1 medium, E2 small to medium, E3 small, E4 small, CL1 medium, CL2 small, CL3 medium,
CL4 small to medium, CL5 small. Everything except CL4 and CL5 fits before 2026-11-24; CL4 lands
as soon as rows 62, 63a and 66 merge, before the 2026-12-04 build date, leaving REL-GATE (row 93)
unblocked.

## 11. Decisions (closed)

No owner question remains open. Each item below was an owner question in revision 1 and is now a
tech-lead call recorded here, per the round 1 review and the orchestrator's rulings.

- **OQ-1. `--quiet` and errors. Closed: (a).** Every command prints the human error line to
  stderr under `--quiet`. This conforms to the help text 31 options already print; the nine
  outliers are listed in section 4.1 and change in CL1.
- **OQ-2. `fit --json` error shape. Closed: (a).** `fit` joins the common envelope: `code` is a
  member, `error` is the plain message, `message` goes. This is the row's stated purpose (owner
  2026-11-12). `fit` warnings keep `code`, `message` and `detail`.
- **OQ-3. One exit map. Closed: (a).** `demo` and `fit` adopt `exit_code_for`, so some `demo`
  failures move from 3 to 1 or 5. The owner already ordered one exception-to-exit-code map in
  row 24zf. The change is a CLI changelog `Breaking:` line.
- **OQ-4. Canonical JSON rule. Closed: the records rule plus a named seed form.**
  `canonical_json_bytes` uses `ensure_ascii=False` (records, CLI evidence and platform agree);
  `seed_fingerprint_bytes` keeps `strategy_config_fingerprint` and pool `_config_hash`
  byte-pinned as a second named form under `SEED_PROTOCOL_VERSION`. No generated output, record
  id or evidence manifest hash moves.
- **OQ-5. Composite providers with no seed (A3). Closed: (a), resolved once per spec.**
  `resolve_seed` draws one fresh seed per call at the composite entry points; all 13 all-zero
  fallbacks, the native mirror included, go. Its 8-byte path uses the checked seed rule, while
  its 32-byte deterministic composite path keeps today's full-width conversion and output
  (section 4.4).
- **OQ-6. Generated-at shape. Closed: (a) `+00:00`.** Three of four writers use it and Python
  3.10 parses only it.
- **OQ-7. `CUSTOM_PROVIDERS_DIR`. Closed: (a).** Renamed `DECOY_CUSTOM_PROVIDERS_DIR` with no
  alias under the pre-GA hard-delete rule. The platform reads the same name
  (`api/config.py:712-715`) and must follow; that follow-up is in section 13.
- **OQ-8. Unknown flag spellings. Closed: (a) as corrected.** One accepted set per repo. Engine
  flags refuse an unknown value with the coded, value-free `EnvFlagError` (`env_flag_invalid`),
  and each caller defines its safe outcome (section 4.7). CLI flags fail safe:
  `DECOY_NO_UPDATE_CHECK` with an unknown value means no check, never exit 1.
  `DECOY_PACK_REQUIRE_SIGNATURE`'s current behaviour is stated as it is: an unknown value raises,
  and the fallback loader then runs without the pack.
- **OQ-9. C6 and C9 tier. Closed: (a).** Both stay in this row, slip-able to V1+ if the build
  date is at risk. C6 became slip-able only after the H1 fix: it now keeps both stdout documents
  byte-identical (AT-CL2-4). C9 changes message text, not the code.

## 12. Recurring-defect walk

| # | Catalog class | Result |
|---|---|---|
| 1 | Anchored validation | The schema's `code` pattern is `^[a-z][a-z0-9_.]*$` (ECMA-262 has no `\Z`), and `emit_error` checks the same rule with `re.fullmatch`. The flag parsers compare whole stripped strings against a set, no regex. No other regex is added. |
| 2 | Strict wire and stored types | The error schema types every fixed member and accepts recursive JSON context; `_hash_config` refuses dates, datetimes and all non-JSON values before routing; subset construction has the same coded boundary for its free-form literals; `CanonicalJsonError` is a `ValueError` so the records codec keeps its coded refusal; `seed_bytes_to_int` refuses any length but 8 and `resolve_seed` accepts exactly 8 or 32 bytes. |
| 3 | No exception context | Loader, `CanonicalJsonError`, `EnvFlagError` and seed errors are raised after the handler closes; AT-E1-4, AT-E3-3 and AT-CL3-3 assert `__cause__ is None and __context__ is None`. |
| 4 | Versioned read models | The error schema is versioned (`decoy-error-v1`); stored profiles with `Z` still load; the seed form is frozen under `SEED_PROTOCOL_VERSION`; pinned plans and resume state fail closed on digest mismatch rather than reading as current. |
| 5 | Normalized guard scanners | Section 6.3 gives each guard its seeded spellings, alias resolution through the existing helpers, and the mutation that turns it red. Deliberate evasion (`getattr`, `exec`) is V2-LOG. |
| 6 | No reaching into private modules | CLI calls only `decoy_engine.secret_providers.parse_ref` and `decoy_engine.errors.EnvFlagError` (public). Engine callers import `_canonical` through its package `__init__`; `internal/env_flags.py` is engine-only and the public import boundary sentry already covers `internal/`. |
| 7 | No hidden CI failures | No CI command or failure policy changes; E1 and CL3 edit layer contracts, so their full-suite baseline comparison is mandatory and named in section 10. |
| 8 | One input, one trust class per entry point | The loader takes user config paths and text only; the policy file, layout file, CI manifest, CLI-written text and user-editable project marker keep their own named reader kinds on the D8 allowlist. `env_flag` reads user-set variables; the engine-set isolated-worker marker uses the same parser and reads `"1"` as today. |
| 9 | Mutations on the final head | Each mutation in sections 6.1 and 6.3 reruns on the final head of its slice. |
| 10 | Allowlisted helpers are still checked | Every allowlist (D3 kinds, D8 kinds, D7 baseline, exit-code owner baseline) keys on exact `(path, function name)` and a test fails when an entry no longer matches a real site. |
| 11 | No raised caps | No ceiling or baseline grows. D7 starts at today's count minus the converted sites and only shrinks; the exit-code owner baseline starts at the three verified classifiers and only shrinks to zero in CL4; the CLI `cli/` fan-out pin falls from 30 to 29. The engine root fan-out stays at 25 (a sub-package is added, not a module). The draw-site catalogue, provider registry, provider output and strategy-map digests are content snapshots re-captured for reviewed entries, not caps. Line-count pins moved by added lines, if any, are listed in each build report. |

## 13. V2-LOG

- CX-1 full conversion of the remaining error sites to `emit_error` (about 49 lines in 23
  files), then D7 at zero. AT-CL1-6 proves their literals already fit the schema.
- CX-2 remaining `parse_pipeline_config` except lists (`api.py:476`, `report.py:1762`, and nine
  more).
- RFC 8785 number form for canonical JSON.
- Deliberate evasion of the new sentries (`getattr(json, "dumps")`, `exec`).
- B11: no scheme check on `--notify` targets and `DECOY_VERSION_URL`.
- `connectors/sftp.py:138` default path read outside a helper.
- EX-2 V1+: routing all 68 generator constructions through `determinism/_rng.py` (24ze D2
  baselines them).
- The deterministic composite 32-byte mask key still seeds NumPy's non-cryptographic generator
  through the full-width `int.from_bytes` conversion. This is frozen to avoid an output move;
  replacing it requires an explicit seed-protocol and output migration.
- ED-8 V1+: one `utc_now()` with a test override.
- L3: the platform reads `CUSTOM_PROVIDERS_DIR` (`api/config.py:712-715`, feeds file-access
  roots); it moves to `DECOY_CUSTOM_PROVIDERS_DIR` when Platform V1 resumes.
- L9: `cap_error_text` and `demo`'s 500-character cut truncate error text silently.
- L10: the NER config-parse copy in `out_of_core/_mask_group_c.py:320-340` against
  `_ner_config.py` (census package E1).
- `report.py:1761` policy file: its generic arm prints `{exc}` and it has its own decode; it
  could share `read_config_text` while keeping "empty means no policy".

## 14. Pre-mortem

- **A digest moves that something stored depends on.** Pinned plans, resume state and subset
  manifests are the stored ones; all refuse on mismatch, so the failure is loud. The `Breaking:`
  line names the rebuild. A digest that feeds a seed would be silent, which is why the seed
  fingerprint is its own frozen form and AT-E1-2 pins it.
- **The canonical rule splits the CLI from the platform.** It cannot: the rule is the records
  rule the platform already shares, and AT-E1-5 and AT-CL5-2 check record and manifest bytes.
- **A hashed site was missed.** The D3 sentry lists every `sort_keys` dump; an allowlisted
  document that turns out to be hashed is routed by the builder and the allowlist shrinks.
- **`joint_mask` table name is not reachable at the transform.** The strategy handler has
  `ctx` and the plan table; if a call reaches `apply_joint_mask` without it, the builder stops
  (stop line 2) rather than defaulting to an empty name, which would merge groups across tables.
- **The streamed scan goes silent.** Revision 1 routed it through `emit_json`, a no-op in
  default mode. It now uses `emit_json_document`, and AT-CL2-4 runs `decoy scan --output -` in
  default mode and asserts non-empty, identical bytes.
- **The hot lanes slip.** CL4 is the only slice that waits; the owners exist after CL1 and CL3,
  so CL4 is a short follow-on. If rows 62, 63a and 66 are not merged by 2026-11-28, the
  orchestrator decides whether CL4 rebases onto the open lanes.
- **`--quiet` users rely on silence.** The change is to stderr only, so stdout pipes are
  unaffected.
- **24ze lands the same sentries first.** The files and names in section 6.3 are the ones 24ze
  uses, so the second row to land extends the file instead of adding one.
- **A flag spelling a user's CI already sets stops working.** In the CLI no spelling refuses: an
  unknown value takes the safe side. In the engine only spellings outside the strict set refuse,
  and that set is the one `DECOY_PACK_REQUIRE_SIGNATURE` already uses. `DECOY_ML_DISABLED=true`,
  which did nothing before, now disables ML, which is what the user asked for.

## 15. Stop lines for builders

1. Do not change `SEED_PROTOCOL_VERSION`, any `derive` input layout, the bytes
   `seed_fingerprint_bytes` writes, the vault header bytes, or any reason code's name.
2. If `apply_joint_mask` is reached without a table name, or `joint_mask` turns out to be
   admitted to the chunked, out-of-core or native route, stop and report.
3. The only seed-form callers are `generators/derivation.py:204` and
   `generation/pool/_builder.py:39-44`. If any other site in section 4.3 or its allowlist turns
   out to feed a seed, stop and report.
4. Do not convert hot-lane files (`cli/run/_execute.py`, `_flow.py`, `_errors.py`, `_render.py`,
   `_evidence.py`, `_notify.py`, `_prepare.py`, `cli/scan.py`) outside CL4.
5. A golden baseline may change only for fixtures that use `joint_mask` with null keys or gen
   mode (E2), the two new draw-site provider scenarios and their registry or strategy-map
   content snapshots (E2), or a moved digest listed in section 9 (E1). Deterministic composite
   fixtures with a 32-byte key must remain byte-identical to main. Any other golden diff is a
   stop.
6. Do not raise any module-size ceiling, fan-out allowlist or sentry baseline. The draw-site
   catalogue pin changes only by the two entries and one text change named in section 4.4.
7. If `decoy_engine.secret_providers.parse_ref` is missing from the pinned engine, CL3 waits for
   the repin instead of copying the grammar.
8. No test runs on the devbox. Mac first, then `~/bin/vm-run`.

## 16. Verification status

Sites, line numbers, import tiers, fan-out counts and module sizes were read on engine
`8c9c1162` and CLI `88403cc`, and the round 1 and round 2 findings were re-checked on engine
`8c9c1162` and CLI `5c9dd8e`: the streamed scan path and `emit_json`, `parse_pipeline_yaml` and
`_run_config_base`, the public `yaml.*` codes, the CLI `.importlinter` tiers and snapshot, the
`_yaml_errors.py` imports, the records codec error arms, the 13 all-zero seed sites and the
native mirror, the draw-site catalogue and provider registries, the full-width 32-byte
composite conversion, both seed-feeding JSON forms, every sorted-key dump cited in section 4.3,
the subset literal path, `joint_mask` route admission, the model pack flag readers and their
fallback, the isolated-worker marker, the three exit classifiers, run's JSON context members,
the `--quiet` help texts, and the `config_json` and `spec_json` consumers. Pydantic keeps date
objects in `provider_config` and extra column fields, so AT-E1-3 starts red. No allowlisted
indented document is hashed by a reader. Census and reachability facts come from
`REMEDIATION.md` and `REACHABILITY-A-B.md` and were not re-run. No test ran.
