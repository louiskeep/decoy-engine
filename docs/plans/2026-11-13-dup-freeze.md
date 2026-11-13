Status: plan

# DUP-FREEZE: settle the duplicated surfaces that freeze at REL-GATE (roadmap row 24zf)

Rules consulted: `/home/cam/dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md`, `feature-dev.md`, `bugfix.md`, `architecture.md`, `reuse-first.md`,
`api-and-compatibility.md`, `security.md`, `testing.md`, `exploration.md`,
`workspace-and-vcs.md`, `scope-discipline.md`, `verification.md`, `code-review.md`,
`cross-cutting-concerns.md`, `documentation.md`, `profiles/local-operations.md`,
`catalogs/recurring-defects.md`; engine `CLAUDE.md`, `CODEMAP.md` ("Where Things Go"),
`.importlinter`, `docs/compatibility-contract.md` (sections 1 and 3.3); CLI `CODEMAP.md`
("Where Things Go"), CLI `docs/ROADMAP.md` (rows 24b, 24p, 24zd, 24ze, 24zf, 62, 63a, 66, 93).

Revision 1, not yet reviewed.

Bases (2026-11-13): engine `origin/main` `8c9c1162`, CLI `origin/main` `88403cc` (pins
`decoy-engine==0.23.0`). Inputs: the duplication census `REMEDIATION.md` (CLI
`docs/records/2026-11-12-duplication-census/`, merged in CLI #211), package C (C1 to C10) and
the matching guards of package D; the reachability pass `REACHABILITY-A-B.md` (CLI branch
`docs/dup-reachability-ab` `9efdc1ab`, not merged); the 24zd plan as revised (engine
`plan/dup-defects` `f659723b`), which routes A1 to C4, B5 and B8 to C5, B6 and B7 to C1, and
B9 to C7. Line numbers below were re-read at the bases above; some differ from the census.

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

Risk: R2 overall. The row moves frozen-at-release surfaces: a `--json` error shape, exit codes,
digest bytes, generated output bytes for `joint_mask`, and environment variable meanings. No
slice touches keys, vault bytes, auth or delivery. Out of scope: A9 (row 24p), the full CX-1
conversion of every error site (V1+), the D sentries that 24ze builds for classes this row does
not settle (D1, D4, D5, D10, D11), every LATENT and IDENTICAL census item not named in package
C, platform callers (Platform V1 is parked).

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
  `--quiet`, the rest print nothing, while the `--quiet` help says "Suppress stdout. Errors
  still go to stderr." (`cli/report.py:662`). B7: `fit.py`, `_multi.py` and `reference.py`
  print engine text without `markup=False`, so Rich drops bracketed text such as `[vault]`.
- **C2, exit map.** `run` classifies failures in `cli/run/_errors.py:211-232`; `demo` keeps its
  own three-type map (`cli/demo.py:582-590`); `fit` its own (`cli/fit.py:663`, `:724`); the
  gate has `gate_exit_code` (`cli/gate/_layers.py:391`). The same failure can exit 1 in `run`
  and 3 in `demo` (B1, LATENT in the reachability pass).
- **C3, digest JSON.** 19 `json.dumps` sites build bytes that are hashed or signed, in eight
  variants of `sort_keys`, separators, `ensure_ascii` and `default=` (section 6.2, Fields).
  `plan/_compile.py:660` removed `default=str` on purpose ("any such value raises TypeError at
  plan-compile time"); `generation/_reproducibility.py:204`, `plan/_generation.py:411` and
  `declared_casting/_catalog.py:293` keep `default=str`, and `execution/physical/_inputs.py:94`
  and `subset/_scope.py:41` keep `default=repr`. `records/_canonical.py:18-26` is the only
  module that already states a rule, but `records` is import tier 1 and tiers 2 to 6 cannot
  import it.
- **C4, seeds.** A1: `transforms/joint_mask.py:338` (null-key fallback) and `:366-367` (gen
  mode) seed `default_rng(int.from_bytes(job_seed[:8], "big"))` from the mask key
  (`execution/_strategies/_joint_mask.py:123`), with no namespace, table or group, so every
  `joint_mask` group draws the same stream and 8 key bytes become a PRNG seed. A3: with no
  seed, composite providers fall back to an all-zero seed on some paths
  (`generation/composite/_address.py:96`, `_provider.py:116`) and to `os.urandom(8)` on others
  (`_address.py:181`, `_provider.py:204`, four more). A4: six `int.from_bytes(seed, "big")`
  conversions with no length check, while `generation/pool/_sampler.py:168` checks length.
- **C5, config loader.** Eleven CLI sites read and `yaml.safe_load` a user config, each with
  its own except list (for example `cli/tokenize.py:201`, `cli/unmask.py:155`,
  `cli/gate/_layers.py:101`, `api.py:93`). A non-UTF-8 file raises `UnicodeDecodeError` to the
  global handler in six of them (B5) and `run` reports it as a runtime error with exit 3
  (`cli/run/_prepare.py:56-67`). The mask-secret reference guard has four copies with three
  message variants (`api.py:108-125`, `cli/run/_prepare.py:199-226`, `cli/tokenize.py:215-231`,
  `:557-573`) and a case-sensitive prefix test (`_mask_secret.py:35`, `:61`), while the engine
  `parse_ref` lower-cases the scheme (B8).
- **C6, stdout JSON.** `cli/scanning/_multi.py:471` and `cli/schema.py:91-92` write JSON to
  stdout without `emit_json` (`ui/output.py:154`).
- **C7, truthiness.** CLI: `_update_check.py:42`, `:48` (`CI=false` counts as CI, B9),
  `__main__.py:142`, `ui/output.py:47`, `cli/run/_notify.py:165` (a non-numeric
  `DECOY_NOTIFY_SMTP_PORT` drops every notification with "invalid literal for int()", B9).
  Engine: `profiling/model_pack/loader.py:90` (strict set) against `:186` (`== "1"`),
  `execution/_isolated_run.py:221`, `internal/faker_setup.py:223` (`CUSTOM_PROVIDERS_DIR`, no
  `DECOY_` prefix).
- **C8, generated-at.** `profiling/types.py:193` writes `datetime.utcnow().isoformat() + "Z"`
  (deprecated call); `profiling/scan_set.py:283`, `quality/report.py:257` and
  `verification/postmask/runner.py:172` write `+00:00`.
- **C9, NER mismatch.** `execution/_strategies/_ner_config.py:77`
  (`enforce_ner_model_version`) holds one wording; `_text_mask.py:134-149` and
  `out_of_core/_mask_group_c.py:344-357` inline two more instead of calling it.
- **C10, identifier locale (A9).** Owner placed it in row 24p; this row only records the route.

### 1.2 Why one row

Each item is small, but all ten share one deadline (REL-GATE) and one method: pick the owner,
move every copy to it, add the guard. Doing them in one plan keeps one owner question list, one
compatibility note, and one golden-diff budget per slice.

## 2. Guarantee

After this row merges, and before REL-GATE:

1. Every CLI command writes its `--json` error object in one documented shape
   (`docs/schemas/decoy-error-v1.schema.json` in the CLI repo), and its human error line through
   one emitter that prints with `markup=False` and follows one `--quiet` rule (OQ-1).
2. Every CLI command that maps an unexpected exception to an exit code uses one function,
   `exit_code_for(exc)` in `cli/exit_codes.py`; `run` and `demo` exit the same way for the
   same failure.
3. Every engine byte string that is hashed, HMAC-ed or signed and is built from JSON is built by
   one function, `canonical_json_bytes(value)`, with one `ensure_ascii`, separator, NaN and
   non-JSON-type rule (OQ-4). The only other `sort_keys` dumps left are on an exact allowlist,
   each with a reason.
4. Each `joint_mask` group draws from its own stream derived from the mask key through
   `derive`; no engine module seeds a generator from raw key bytes, and every bytes-to-int seed
   conversion checks length.
5. Every CLI command that reads a user config file reads it through one loader, so the same bad
   file gives the same code and exit in every command; a non-UTF-8, unparseable, empty or
   non-mapping config exits 1 with a config-error code in every command, `run` included.
   Mask-secret references are judged by one function that uses the engine parser.
6. CLI JSON reaches stdout only through `ui/output.py`.
7. Each repo reads flag-type environment variables through one parser with one documented set
   of accepted values; `CI=false` means not CI; a bad SMTP port is a named, value-free
   notification error that leaves the run exit unchanged.
8. Every engine "generated at" timestamp has one shape (OQ-6).
9. `NER_MODEL_VERSION_MISMATCH` is raised from one function with one message.
10. Each item above has a sentry or choke point that fails when a second copy appears.

No message, payload, log line or evidence field written by this row carries a data value,
secret, secret reference value or exception text. Names (commands, fields, variables, codes)
may appear.

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
  JCS rules Python can meet (sorted keys, compact separators, no NaN, no implicit type
  conversion) and asks the owner about escaping (OQ-4). Sigstore and in-toto sign canonical
  JSON produced by one shared function for the same reason.
- **Seed derivation.** NIST SP 800-108 and RFC 5869 (HKDF) derive independent sub-keys per
  purpose label; NumPy's `SeedSequence` documents spawning independent child streams instead of
  reusing one seed. The engine already does this through `determinism/_derive.py:260`
  (`derive(seed, namespace, source)`); A1 uses it.
- **Config loading.** PyYAML `safe_load` on decoded text, one decode step with an explicit
  codec, and a typed error per failure kind is the pattern `cli/compile_explain.py:175-181`
  already follows.
- **Flag variables.** `NO_COLOR` (no-color.org): any non-empty value disables colour. The CI
  convention (GitHub Actions, GitLab, CircleCI set `CI=true`) treats `CI` as a boolean string.
  `distutils.util.strtobool` (removed in Python 3.12) defined the common accepted sets
  `y, yes, t, true, on, 1` and `n, no, f, false, off, 0`.
- **Timestamps.** RFC 3339 allows `Z` or `+00:00`; Python 3.10's `datetime.fromisoformat`
  parses only `+00:00` (A6 in the census), so `+00:00` round-trips on every supported Python.
- **Guards.** Fitness functions (Ford, Parsons, Kua, "Building Evolutionary Architectures")
  with a shrink-only baseline; the dev-rules best-practice-becomes-a-sentry rule.

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
    context: Mapping[str, str] | None = None,
) -> None: ...
```

- `--json`: writes one object through `emit_json`:
  `{"command", "status": "error", "error": message, "code"?, "hint"?, **context}`. `context`
  keys are the existing per-command keys (`config`, `source`, `mode`, `output`) and may not
  shadow the five fixed members (refused with `ValueError` at the call, a programming error).
- Human: `error: <message>` and, when given, `  hint: <hint>`, both with `markup=False`, on
  `state.err_console`. Under `--quiet` the rule is OQ-1 (recommended: still printed, matching
  the help text).
- `code` is the engine reason code or CLI warning code when the failure has one; it is never
  folded into `error`. `fit`'s `message` member goes (OQ-2).
- `run` keeps its extra members (`error_kind`, `published_members()`, the RESULT reason
  members) as `context`; RESULT (row 62) owns their meaning. The run error arm moves to
  `emit_error` only after the hot lanes merge (slice CL4).
- The schema file `docs/schemas/decoy-error-v1.schema.json` (CLI repo) states the fixed
  members, `additionalProperties: true` for context, and that `code` matches
  `^[a-z][a-z0-9_.]*\Z`.

Converted in CL1 (drifted or local emitters outside the hot lanes): `cli/unmask.py:135`,
`cli/compile_explain.py:319`, `cli/report.py:1708`, `:2220`, `cli/subset.py:484`, `:505`,
`cli/fit.py:403`, `cli/tokenize.py:180`, `:520`, `cli/scanning/_multi.py:216`,
`cli/gen/_scaffold.py:164`, `cli/reference.py:272-284`, `:300-313`. Converted in CL4:
`cli/run/_errors.py`, `cli/run/_evidence.py`, `cli/run/_notify.py`, `cli/scan.py`. Every other
`err_console.print(error(` site stays on the 24ze D7 baseline (CX-1 full conversion is V1+);
those sites already write the common shape, which AT-CL1-1 proves.

### 4.2 C2: one exception-to-exit map (CLI)

Owner: `src/decoy/cli/exit_codes.py`, new `exit_code_for(exc: BaseException) -> int`, built
from the classifier now in `cli/run/_errors.py:211-232` (pre-run check, capacity codes,
`MemoryError`, `UNIQUENESS_IMPOSSIBLE_CODE`, usage types) and the `RUN_EXIT_MAP` categories.
`run` (`_errors.py`), `demo` (`demo.py:582-590`) and `fit` (`fit.py:663`, `:724`) call it.
`gate_exit_code` (`gate/_layers.py:391`) maps gate findings, not exceptions, and stays; its
error-finding exit must equal `exit_code_for` of the matching config error, which AT-CL4-3
pins. `KeyboardInterrupt` keeps Typer's 130 (`RUN_EXIT_MAP` cancelled). Built in CL4 because
`_errors.py` is a hot lane and RESULT C-EXIT (CLI #228) is still landing its pieces.

### 4.3 C3: one canonical JSON rule for digests (engine)

Owner: new private package `src/decoy_engine/_canonical/` (`__init__.py` re-exports from
`_json.py`), in import tier 6 so every tier, including `phase` (tier 5), can call it. A root
module is not possible: 24zd's `_arrow_types.py` brings the root to the 25-module fan-out cap,
and the cap's rule is that the next one uses a named sub-package.

```python
def canonical_json_bytes(value: object) -> bytes: ...
class CanonicalJsonError(TypeError): ...  # value-free: names the Python type, never the value
```

Rule (recommended answer to OQ-4): `sort_keys=True`, `separators=(",", ":")`,
`allow_nan=False`, `ensure_ascii=True`, UTF-8; a closed `default` that converts
`datetime.datetime` and `datetime.date` to `isoformat()` and `decimal.Decimal` to `str`, and
raises `CanonicalJsonError` for any other non-JSON type. The error is raised after the encoder
returns, outside any handler, so it carries no `__context__` holding the value.

Why `ensure_ascii=True`: `generators/derivation.py:166` (`strategy_config_fingerprint`) feeds
per-column seeds and already uses compact separators with the default `ensure_ascii=True` and
no `default`. With this rule its bytes stay identical for every config that encodes today, so
no generated output moves; a config value that is a `datetime.date` (which `yaml.safe_load`
produces for an unquoted `2024-01-01`) would raise `TypeError` there today and is encoded as its
ISO string instead. `plan/_compile.py:655-660` removed `default=str` to stop silent coercion;
the closed conversion is explicit and refuses every other type, so that intent holds. `records/_canonical.py` (`ensure_ascii=False`) moves for non-ASCII content only;
records are pre-release and none exist outside development.

Sites routed (section 6.2, Fields, lists each with its variant): `records/_canonical.py`
(delegates; `canonical_bytes` keeps its name), `phase.py:481`, `generators/derivation.py:204`,
`generation/_reproducibility.py:204`, `generation/_pinned_plan.py:200`,
`generation/_pinned_compile.py:299`, `:319`, `plan/_generation.py:411`, `plan/_compile.py:660`,
`execution/physical/_inputs.py:94`, `subset/_scope.py:41`, `declared_casting/_catalog.py:293`,
`declared_casting/_quarantine_local.py:251`, `profiling/model_pack/provenance.py:200`.

Exact allowlist, each with a reason: `vault.py:363` (stored vault header, a frozen file format,
not a digest input); `profile/_serialize.py:33`, `profiling/model_pack/_packio.py:67`,
`profiling/model_pack/provenance.py:183`, `profiling/model_pack/trainer.py:592`,
`subset/_api.py:217` (indented documents written for people; if the builder finds one hashed,
it routes it and the allowlist shrinks). `quarantine.py:320` and
`execution/_isolated_worker.py:70` are not `sort_keys` dumps and not digests.

Digests that move, and only for configs that hit a changed rule (non-default separators,
`default=str` or `default=repr` conversions): the reproducibility `config_digest`, the plan
pinned-column `config_json`, the physical-input identity hash, the subset scope fingerprint,
the declared-casting catalog key, the quarantine header blob. Pre-GA, each is a dated
compatibility note (section 9). A pinned plan compiled before the change refuses with its
existing digest-mismatch code and must be recompiled; the changelog `Breaking:` line says so.

### 4.4 C4: seed derivations (engine)

- **A1.** In `transforms/joint_mask.py`, both RNGs come from
  `seed_bytes_to_int(derive(job_seed, namespace, source)[:8])` where `source` is
  `b"joint_mask/null_fallback/v1"` or `b"joint_mask/gen_row/v1"` followed by length-prefixed
  UTF-8 of the table name, the reference name and the `\x1f`-joined target columns. Length
  prefixes keep `("ab", "c")` and `("a", "bc")` apart. The keyed path for non-null keys
  (`derive(job_seed, namespace, _KEYED_ROW_SOURCE)`) is unchanged, so keyed selection output
  does not move. The table name reaches `apply_joint_mask` as a new keyword from
  `execution/_strategies/_joint_mask.py:123`; if `plan.namespace` already carries the table,
  the builder still adds it (one rule, no inference).
- **A4.** `_seed_bytes_to_int` moves from `generation/pool/_sampler.py:168` to
  `determinism/_seeds.py` as `seed_bytes_to_int(seed: bytes) -> int`, which refuses any length
  but 8 with a value-free `ValueError`. Callers: `_sampler.py:270`, `:518`,
  `generation/_faker_pool.py:459`, the six A4 sites (`composite/_person.py:143`,
  `_name_email.py:135`, `_address.py:97`, `_provider.py:117`, `_custom.py:207`,
  `execution/native/protocol/providers_gen.py:75`), and A1.
- **A3.** One rule for a missing seed (OQ-5, recommended (a)): every composite path calls
  `fresh_seed()` in `determinism/_seeds.py` (8 bytes of `os.urandom`) when `spec.seed is None`;
  the all-zero fallbacks go. No production path passes `None` today (LATENT); the rule matters
  for library callers and frozen behaviour.
- Draw-site records: `execution/native/_draw_sites_mask.py:205` documents the keyed draw only;
  the builder adds `mask.joint_mask_null_fallback` and `generate.joint_mask_row` entries if the
  draw-site registry sentry requires every `default_rng` site to be listed, and otherwise
  records them in the module docstring.

### 4.5 C5: one config loader and one secret-reference rule (CLI)

Owner: new `src/decoy/_config_file.py` (private shared primitive, CLI CODEMAP row "Private
shared filesystem or process primitives").

```python
def load_config_mapping(path: Path) -> dict[str, Any]: ...
class ConfigFileError(Exception):  # .code, value-free .message, .hint
```

Steps: read bytes (an `OSError` keeps today's runtime class, exit 3: the environment, not the
config, failed); decode strict UTF-8, BOM allowed (`config.not_utf8`); `yaml.safe_load` with
the existing `sanitize_yaml_error` text (`config.yaml_syntax`); empty (`config.empty`); not a
mapping (`config.not_mapping`). The gate's existing finding codes `yaml.parse_error`,
`yaml.empty`, `yaml.not_mapping` (`gate/_layers.py:101`) are the public ones today; the
builder keeps those names as the codes rather than minting the `config.*` spellings above if
any doc or test outside the gate already reads them, and records the choice. Every failure is
raised after the handler closes and maps to exit 1 through `exit_code_for`.

Callers moved: `api.py:93`, `unmask.py:155`, `report.py:1748`, `:1761`, `:2003`, `:2241`,
`preflight.py:686`, `subset.py:156`, `tokenize.py:201`, `:539`, `compile_explain.py:175`,
`gate/_layers.py:101` (the gate wraps `ConfigFileError` in its finding), `scan.py:243`, and in
CL4 `cli/run/_prepare.py:56-67`. Re-parses of text the CLI wrote itself stay outside the loader
on an exact `(path, function)` allowlist: `plan.py:44`, `demo.py:229`, `io/_project.py:89`,
`init.py:124`, `:387`, `:408`, `:750`, `gen/_scaffold.py:141`.

Secret reference (F1, B8): `mask_secret_ref_problem(raw) -> str | None` in `_mask_secret.py`
calls `decoy_engine.secret_providers.parse_ref` (public, exported at
`secret_providers/__init__.py:59`) and then checks the scheme against the CLI's provider set
(`env`, `file`; `vault` stays refused with `secret_provider_unknown`, as CLI #204 pins). It
returns the one `INVALID` text or `None`; the four copies call it. The engine parser's error is
bare and value-free by construction (`secret_providers/_types.py:94-110`).

### 4.6 C6: CLI JSON to stdout only through `ui/output.py`

`emit_json` gains a sibling `emit_json_document(state, doc, *, indent=2)` for commands whose
product output is a JSON document in default mode (`schema.py:91-92`). The streamed scan
result at `scanning/_multi.py:471` calls `emit_json` (it is `--json` mode). Bytes stay identical:
AT-CL2-4 compares them.

### 4.7 C7: one flag truthiness rule per repo

Engine owner: `src/decoy_engine/internal/env_flags.py`, `env_flag(name, *, default: bool) ->
bool`. CLI owner: `src/decoy/_env_flags.py`, same signature. Accepted values (OQ-8,
recommended (a)): true `1`, `true`, `yes`, `on`; false `0`, `false`, `no`, `off`, and empty or
unset gives `default`; case-insensitive after strip. Anything else raises a typed error naming
the variable and the accepted spellings, never the value. A security flag keeps the existing
fail-closed reading at `profiling/model_pack/loader.py:90` (an unknown value means "require"),
which the helper supports through `on_unknown="true"`.

External conventions keep their own meaning and do not use the strict set: `NO_COLOR` (any
non-empty value disables colour; today's `is not None` also counts the empty string, which the
spec excludes) and `CI` (a false spelling means not CI, any other non-empty value means CI).

Sites: engine `profiling/model_pack/loader.py:90`, `:186`, `execution/_isolated_run.py:221`,
`:238`, `:469` (the isolated-worker handshake is engine-set; the strict parser reads `"1"` as
today). `profiling/model_pack/loader.py:351` and `profiling/model_pack/provenance.py:282`, which
the census listed, read the signing key (secret material, "non-empty means set"), not a flag;
they stay as they are;
`internal/faker_setup.py:223` reads a path, not a flag, and is renamed
`DECOY_CUSTOM_PROVIDERS_DIR` with no alias (OQ-7). `connectors/sftp.py:138` reads a path and is
out of scope. CLI `_update_check.py:42`, `:48`, `__main__.py:142`, `ui/output.py:47`, and in CL4
`cli/run/_notify.py:165` (the SMTP port is parsed by one `env_int(name, default)` beside
`env_flag`; a bad value is a named notification error and the run exit is unchanged, as other
notification failures are). `_update_check.py:100` (`DECOY_VERSION_URL`) is a URL, kept out
(B11 is V2-LOG).

### 4.8 C8: one generated-at shape (engine)

`profiling/types.py:193` becomes `datetime.now(timezone.utc).isoformat()` (OQ-6, recommended
`+00:00`), the shape the other three writers use. Readers that accept `Z` keep accepting it, so
older profile files still load.

### 4.9 C9: one NER mismatch message (engine)

`_text_mask.py:134-149` and `out_of_core/_mask_group_c.py:344-357` call
`enforce_ner_model_version` (`_ner_config.py:77`) with `strategy="text_mask"` and their column,
and their inline raises go. The wording is the helper's. The model name and versions are names,
not data values.

### 4.10 C10: identifier locale rule

Routed to GEN-PROVIDERS (row 24p) by the owner (2026-11-12). This row builds nothing for it and
records the route so REL-GATE's checklist finds it in 24p.

## 5. Behaviour

| Input | Today | After |
|---|---|---|
| `decoy fit --json` on a refused source with a code | `{"code", "message", "error": "<code>: <message>"}` | `{"code", "error": "<message>"}` (OQ-2 (a)) |
| `decoy unmask --quiet` with a bad vault path | nothing on stderr, exit 3 | `error: ...` on stderr, exit 3 (OQ-1 (a)) |
| `decoy scan` (multi) error text holding `[vault]` | bracket text dropped | printed verbatim |
| `decoy demo` failure that `run` maps to 5 | exit 3 | exit 5 |
| `decoy run` on a non-UTF-8 config | exit 3, runtime error text | exit 1, `config.not_utf8` (24b rule) |
| `decoy tokenize`, `unmask`, `report`, `subset`, `preflight` on a non-UTF-8 config | traceback path to the global handler | exit 1, same code and text as `run` |
| `--mask-secret ENV:NAME` | refused as malformed | parsed as `env:NAME` (engine rule) |
| two `joint_mask` groups, null keys or gen mode | same stream for both groups | independent streams; output bytes change once |
| `joint_mask` non-null keyed rows | keyed by `derive(..., _KEYED_ROW_SOURCE)` | unchanged |
| generated column whose config holds a YAML date | `TypeError` in the fingerprint (by reading; not run) | fingerprinted as the ISO string |
| digest of a config with `default=str` content | `str()` of any object | closed conversion or `CanonicalJsonError` |
| `CI=false decoy run` | treated as CI (no update notice) | not CI |
| `DECOY_ML_DISABLED=true` | ML stays enabled | ML disabled |
| `DECOY_ML_DISABLED=maybe` | ML stays enabled | refused, names the variable |
| `DECOY_NOTIFY_SMTP_PORT=abc` | "invalid literal for int()", no notification | named notification error, no value, run exit unchanged |
| profile `generated_at` | `...Z` | `...+00:00` |
| NER model mismatch on the out-of-core route | short wording | the helper's wording, same code |

## 6. Failure modes

### 6.1 Class table

| # | Class | Structure that closes it | Proof | Mutation that turns it red |
|---|---|---|---|---|
| 1 | Value-bearing output | `emit_error` takes only a message the caller built; the loader, `CanonicalJsonError`, `env_flag` and `seed_bytes_to_int` errors name a variable, type or field and are raised after the handler, so no `__context__` holds file bytes, a config value, an env value or a key | AT-CL1-5, AT-CL3-3, AT-E1-4, AT-E3-3 | put `{exc}` in the decode branch; include the env value in the `env_flag` error |
| 2 | Cleanup missing on an exit path | N/A for new state: no slice adds a file, lock or scratch. The loader refuses before any output is opened, so `run` leaves nothing behind | AT-CL3-1 (no output file) | move the load after the first table write |
| 3 | State and concurrency | N/A: every helper is a pure function of its arguments or of one env read at call time; no module cache is added (an env flag read once at import would miss a test's monkeypatch) | AT-E3-2, AT-CL2-2 | cache `env_flag` results in a module dict |
| 4 | Route parity and silent fallback | A1 changes the one function every route calls (`apply_joint_mask`); `joint_mask` is not on the native route (`execution/native/_capabilities.py:376`), and the chunked and out-of-core routes reach the same transform; the loader is the only path for every command, the gate included | AT-E2-2, AT-CL3-2 | leave the old seed in the gen-mode branch only |
| 5 | Referential integrity | `joint_mask` keyed selection on non-null keys is unchanged, so a key that maps to a reference row still maps to the same row; null-key rows already had no key relationship | AT-E2-3 | route the keyed path through the new source too |
| 6 | Untrusted producer | a config file and environment variables are user input: every decode, parse, shape and spelling failure maps to one named code before any field is used | AT-CL3-1 to AT-CL3-4, AT-E3-2 | catch only `YAMLError` again; accept unknown flag spellings as false |
| 7 | Check then use (TOCTOU) | the loader reads the file once and parses those bytes; no caller re-opens the path to re-read | AT-CL3-5 | re-read the path after the decode check |
| 8 | Weak tests and overclaimed evidence | every AT is red on the bases except named controls; byte-identity ATs compare bytes, not exit codes; the `--json` shape AT validates every command's error object against the schema, not one command | gate reviewers rerun the named mutations | relax AT-CL1-1 to the five converted commands |
| 9 | Behaviour-preserving refactor drift | `strategy_config_fingerprint` bytes stay identical (AT-E1-2 golden); `emit_json` stdout bytes stay identical for C6 (AT-CL2-4); `seed_bytes_to_int` keeps `_sampler`'s behaviour for 8-byte seeds | AT-E1-2, AT-CL2-4, pool sampler tests | switch the rule to `ensure_ascii=False` |
| 10 | Frozen contract drift | every moved surface is listed in section 9 with a dated compatibility note and a `Breaking:` fragment; REL-GATE has not frozen anything yet (row 93 NOT STARTED); goldens may change only as stop line 15.5 allows | AT-E2-1, golden suites, AT-CL1-1 | let a golden for a non-`joint_mask` fixture change |

### 6.2 Enumerations

#### Exit paths

| Failure | Commands | Exit after |
|---|---|---|
| config not UTF-8, not YAML, empty, not a mapping | every command that loads a user config (section 4.5 list), `run` (`cli/run/_prepare.py:56-67`) and the gate (`cli/gate/_layers.py:101`) | 1 |
| config file unreadable (`OSError`) | same | 3, unchanged |
| unexpected exception in a command's generic arm | `run`, `demo` (`cli/demo.py:582-590`), `fit` (`cli/fit.py:663`, `:724`) | `exit_code_for(exc)` |
| `CanonicalJsonError` during plan compile | `validate`, `run`, `ci check` | 1 (compile error map) |
| bad flag spelling | the command that reads the flag | 1 (CLI); typed engine error mapped by the caller |
| Ctrl-C | every command | 130, unchanged |

#### Routes

| Route | C4 (A1) | C3 | C9 |
|---|---|---|---|
| whole-frame mask | `transforms/joint_mask.py:338` | plan and physical-input hashes | `_text_mask.py:134` |
| generate | `transforms/joint_mask.py:366` | `generators/derivation.py:204` | N/A |
| `--chunked` | same transform per chunk | same hashes | same handler |
| out-of-core | same transform (`execution/out_of_core/_compat.py` dispatch) | same hashes | `_mask_group_c.py:344` |
| native | `joint_mask` excluded (`execution/native/_capabilities.py:376`); stop line 15.2 if it is not | plan hash only | N/A |

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
| digest JSON, compact, ASCII, no default | `phase.py:481`, `generators/derivation.py:204`, `generation/_pinned_plan.py:200`, `profiling/model_pack/provenance.py:200` | the rule (bytes equal for encodable input) |
| digest JSON, compact, `default=str` | `generation/_reproducibility.py:204` | the rule |
| digest JSON, default separators | `generation/_pinned_compile.py:299`, `:319`, `declared_casting/_quarantine_local.py:251` | the rule (bytes move) |
| digest JSON, default separators, `default=str` | `plan/_generation.py:411`, `declared_casting/_catalog.py:293` | the rule |
| digest JSON, default separators, `default=repr` | `execution/physical/_inputs.py:94`, `subset/_scope.py:41` | the rule |
| digest JSON, explicit `ensure_ascii=True`, compact | `plan/_compile.py:660` | the rule |
| digest JSON, `ensure_ascii=False`, compact | `records/_canonical.py:18-26` | the rule (non-ASCII moves) |
| `generated_at` | `profiling/types.py:193` `Z` | `+00:00` |
| env flags | section 4.7 list | one parser per repo |

#### Output surfaces

- stderr human error line (every CLI command): one emitter, `markup=False`, one `--quiet` rule.
- stdout `--json` error object (every CLI command): one shape, validated against the schema.
- stdout JSON documents (`decoy schema`, streamed `decoy scan`): through `ui/output.py`, bytes
  unchanged.
- generated and masked output bytes: `joint_mask` null-key and gen-mode rows only.
- digests in evidence, run results, pinned plans, subset manifests and records: section 4.3
  list.
- profile documents: `generated_at` suffix.
- notification payloads: unchanged; the SMTP port error is named and value-free.

## 7. Acceptance tests, red first

Every AT below is written first and is red on the bases unless marked control.

### 7.1 Engine

| Id | Slice | Test |
|---|---|---|
| AT-E1-1 | E1 | `canonical_json_bytes({"b": 1, "a": "é"})` equals `b'{"a":"\\u00e9","b":1}'` (OQ-4 (a)); NaN and Infinity raise `CanonicalJsonError`; a `set` raises it naming `set` only |
| AT-E1-2 | E1 | control then green: for the golden corpus configs, `strategy_config_fingerprint` returns the same hex before and after (no seed moves) |
| AT-E1-3 | E1 | a generated column whose config holds a `datetime.date` fingerprints without error and equals the fingerprint of the same config with the ISO string |
| AT-E1-4 | E1 | `CanonicalJsonError` has `__cause__ is None` and `__context__ is None`, and its text does not contain the offending value's `repr` |
| AT-E1-5 | E1 | `records.canonical_bytes` equals `canonical_json_bytes` for a record with non-ASCII text |
| AT-E1-6 | E1 | an object with a custom `__repr__` in a config reaching `physical/_inputs.py` raises `CanonicalJsonError` instead of hashing its repr |
| AT-E2-1 | E2 | two `joint_mask` groups on one table with all-null keys draw different reference rows for the same seed, and the same rows across two runs |
| AT-E2-2 | E2 | gen mode: same as AT-E2-1 on whole-frame, `--chunked` and out-of-core routes, all routes equal to each other |
| AT-E2-3 | E2 | control: non-null keyed `joint_mask` output is byte-identical before and after |
| AT-E2-4 | E2 | `seed_bytes_to_int` refuses lengths 0, 7, 9 and 32 with a value-free `ValueError`; each A4 site rejects a 4-byte `ProviderSpec.seed` the same way |
| AT-E2-5 | E2 | with `spec.seed=None`, two composite pools built in one process differ (OQ-5 (a)) and no code path uses an all-zero seed |
| AT-E2-6 | E2 | the 8 bytes at `job_seed[:8]` do not appear as the seed of any generator in `joint_mask` (patch `default_rng` and inspect its argument) |
| AT-E3-1 | E3 | `DECOY_ML_DISABLED` in `1`, `true`, `YES`, `on` disables ML; `0`, `false`, empty enables it |
| AT-E3-2 | E3 | `DECOY_ML_DISABLED=maybe` raises the typed flag error naming the variable; `DECOY_PACK_REQUIRE_SIGNATURE=maybe` still means "require" |
| AT-E3-3 | E3 | the flag error text never holds the value (`maybe-secret-xyz` is absent) and has no exception context |
| AT-E3-4 | E3 | `DECOY_CUSTOM_PROVIDERS_DIR` is read; `CUSTOM_PROVIDERS_DIR` alone is ignored |
| AT-E4-1 | E4 | a fresh `Profile().generated_at` parses with `datetime.fromisoformat` on Python 3.10 and ends `+00:00`; a stored `Z` profile still loads |
| AT-E4-2 | E4 | the out-of-core and whole-frame `text_mask` routes raise `NER_MODEL_VERSION_MISMATCH` with identical message text for the same column and versions |

### 7.2 CLI

| Id | Slice | Test |
|---|---|---|
| AT-CL1-1 | CL1 | for every command with `--json`, a known-bad input gives one stdout object that validates against `decoy-error-v1.schema.json` (parametrized over the command list from `__main__.py`, not hand-picked) |
| AT-CL1-2 | CL1 | `fit --json` with a coded refusal: `code` is the code, `error` does not start with it, no `message` member |
| AT-CL1-3 | CL1 | under `--quiet`, `unmask`, `scan` (multi) and `gen` scaffold all print the human error line to stderr and nothing to stdout (OQ-1 (a)) |
| AT-CL1-4 | CL1 | an engine message containing `[vault]` reaches stderr verbatim from `fit`, `scan` and `reference` |
| AT-CL1-5 | CL1 | `emit_error` refuses a `context` key that shadows `command`, `status`, `error`, `code` or `hint` |
| AT-CL2-1 | CL2 | `CI=false` and `CI=0`: update check runs as non-CI; `CI=true` and `CI=1`: CI |
| AT-CL2-2 | CL2 | `DECOY_NO_UPDATE_CHECK=maybe` exits 1 naming the variable, no value |
| AT-CL2-3 | CL2 | `NO_COLOR=` (empty) keeps colour; `NO_COLOR=1` disables it |
| AT-CL2-4 | CL2 | control: `decoy schema` and streamed `decoy scan --json` stdout bytes are identical before and after |
| AT-CL3-1 | CL3 | a config holding `\xff`: `validate`, `tokenize`, `unmask`, `report`, `subset`, `preflight`, `compile-explain` each exit 1 with the same code and text, no codec text or `0xff` on stderr or in `--json` |
| AT-CL3-2 | CL3 | empty file and a YAML list: same code per kind across the same commands and the gate |
| AT-CL3-3 | CL3 | loader errors carry no `__context__` and no file bytes |
| AT-CL3-4 | CL3 | `--mask-secret ENV:NAME` is accepted as `env:NAME` by `run` validation, `tokenize` and the API; `vault://x` is refused with `secret_provider_unknown` in all three; `nope:x` gives the one `INVALID` text |
| AT-CL3-5 | CL3 | the loader opens the path once (patched `open` counter) |
| AT-CL4-1 | CL4 | `decoy run` on a `\xff` config exits 1 with the AT-CL3-1 code and writes no output file |
| AT-CL4-2 | CL4 | a failure `run` maps to 5 (capacity) exits 5 from `demo` too; `exit_code_for` agrees with `RUN_EXIT_MAP` for every category |
| AT-CL4-3 | CL4 | the gate's exit for a YAML syntax finding equals `exit_code_for` of the loader's error |
| AT-CL4-4 | CL4 | `DECOY_NOTIFY_SMTP_PORT=abc` with a webhook target: the webhook is still sent, the SMTP error names the variable only, and the run exit is the run's own |
| AT-CL4-5 | CL4 | `run --json` error object validates against the schema and keeps the RESULT members |
| AT-CL5-1 | CL5 | after the repin: a `joint_mask` gen run with two groups gives two different column streams; a profile file's `generated_at` ends `+00:00`; a pinned plan compiled on the old engine is refused with its digest-mismatch code |

## 7a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| sub-key per purpose | `determinism/_derive.py:260` `derive` | no new KDF |
| length-checked seed conversion | `generation/pool/_sampler.py:168` body, moved | no second copy |
| canonical rule | `records/_canonical.py:18-26` statement, generalised | records keeps its API |
| `--json` writer | `ui/output.py:154` `emit_json` | no second writer |
| YAML error text | `cli/_yaml_errors.py` `sanitize_yaml_error` | no new sanitiser |
| secret-ref parsing | engine `secret_providers.parse_ref` | no CLI grammar |
| strict flag set | `profiling/model_pack/loader.py:90` set and fail-closed rule | no third set |
| run classifier | `cli/run/_errors.py:211-232` body, moved | no new categories |
| NER guard | `_ner_config.py:77` `enforce_ner_model_version` | no new helper |
| sentry scanner | engine `tests/sentry/defects/_defect_scan.py`, CLI `tests/sentry/_ratchet.py` | no new AST walker |

## 8. Placement

Checked against each repo's CODEMAP "Where Things Go" at the bases.

| New or moved thing | Path | Repo | CODEMAP row |
|---|---|---|---|
| canonical JSON owner | `src/decoy_engine/_canonical/__init__.py`, `_json.py` | engine | "Foundation (errors, reason adapters, private leaves)"; a sub-package because the root is at the fan-out cap |
| tier entry | `.importlinter` tier 6 gains `_canonical`; `tests/sentry/layout/test_import_layers.py` `LAYERS` | engine | "Layer contracts" |
| seed helpers | `src/decoy_engine/determinism/_seeds.py` | engine | "Generation" neighbours use it; `determinism/` holds `_derive.py`, `_hkdf.py` (3 modules, under the cap) |
| engine flag parser | `src/decoy_engine/internal/env_flags.py` | engine | `internal/` is engine-private (`tests/sentry/test_public_import_boundary.py`) |
| engine sentries | `tests/sentry/test_canonical_json_choke_point.py` (the 24ze D3 name), `tests/sentry/test_rng_seed_conversion.py`, `tests/sentry/test_env_flag_reads.py`, `tests/sentry/test_generated_at_shape.py`, `tests/sentry/test_ner_mismatch_single_site.py` | engine | "Sentry test": existing rules in `tests/sentry/` |
| engine unit tests | `tests/unit/determinism/`, `tests/unit/transforms/`, `tests/unit/internal/`, `tests/unit/profiling/` | engine | "Unit tests": `tests/unit/<package>/` |
| changelog fragments | `changelog.d/dup-freeze-e1.md` to `-e4.md` | engine | "Changelog fragments" |
| compat notes | `docs/compatibility-contract.md` 3.3 dated paragraphs | engine | "Feature and contract docs" |
| error emitter | `src/decoy/ui/output.py` | CLI | "Terminal output" |
| error schema | `docs/schemas/decoy-error-v1.schema.json` | CLI | beside `docs/schemas/decoy-ci-v1.schema.json` |
| exit map | `src/decoy/cli/exit_codes.py` | CLI | "Exit codes" |
| config loader | `src/decoy/_config_file.py` | CLI | "Private shared filesystem or process primitives used by more than one package" |
| secret-ref rule | `src/decoy/_mask_secret.py` | CLI | same row (existing module) |
| CLI flag parser | `src/decoy/_env_flags.py` | CLI | same row |
| CLI sentries | `tests/sentry/cli_text/test_one_error_emitter.py` (24ze D7), `tests/sentry/cli_text/test_exit_code_owner.py`, `tests/sentry/cli_text/test_stdout_json.py`, `tests/sentry/test_config_load_choke_point.py` (24ze D8), `tests/sentry/test_env_flag_reads.py` | CLI | "User-facing CLI text and crash handling" for text rules; "Sentry test" for the rest |
| CLI tests | `tests/unit/test_ui_output.py`, `tests/unit/test_exit_codes.py`, `tests/unit/test_config_file.py`, `tests/e2e/first_user/test_items_errors.py` | CLI | "Exit codes", "Terminal output", "First-user QA item tests" |

No module crosses 600 lines: `ui/output.py` grows by about 60 to about 222; `exit_codes.py` by
about 50; each new module is under 150. The CLI flat `cli/` folder gains no module.

## 9. Frozen contracts touched

None is frozen yet: REL-GATE (row 93) is NOT STARTED and `RELEASE_PHASE` is pre-GA. Each change
is recorded the way compatibility contract 3.3 records earlier pre-GA changes: a dated
paragraph, no shim, plus a `Breaking:` fragment where output or a public shape moves.

| Surface | Change | Record |
|---|---|---|
| `--json` error shape (CLI) | `fit` loses `message` and the code prefix; schema published | CLI changelog, schema file |
| `--quiet` error line (CLI) | printed on stderr everywhere (OQ-1 (a)) | CLI changelog |
| exit codes (CLI) | `demo` and `fit` follow `exit_code_for`; `run` non-UTF-8 config 3 to 1; other commands' non-UTF-8 config to 1 | CLI changelog |
| digest bytes (engine) | section 4.3 list; seeds unchanged | engine 3.3 note, `Breaking:` (pinned plans recompile) |
| `joint_mask` output (engine) | null-key and gen-mode rows | engine 3.3 note, `Breaking:` |
| seed protocol | `SEED_PROTOCOL_VERSION` unchanged: no `derive` input layout changes, only new sources | none |
| env variables | `DECOY_ML_DISABLED` accepts the strict set; `CUSTOM_PROVIDERS_DIR` renamed; `CI=false`; `NO_COLOR=` | both changelogs |
| `generated_at` shape | `Z` to `+00:00` in profiles | engine changelog |
| reason codes | none added or renamed in the engine; CLI config-file codes per section 4.5 | CLI warning or error code list |

## 10. Slices

| Slice | Content | Repo | Risk | Depends on | Tests at T1 |
|---|---|---|---|---|---|
| E1 | C3: `_canonical/`, rule, 14 routed sites, records delegation, `.importlinter` tier 6, D3 sentry with the exact allowlist, compat note, fragment | engine | R2 (digest bytes) | 24zd E3 merged (both edit `.importlinter` and the root fan-out) | AT-E1-*, golden suites, plan, pinned, subset, casting and records tests, import contract and layer snapshot |
| E2 | C4: A1, A3, A4, `determinism/_seeds.py`, seed-conversion sentry, compat note, fragment | engine | R2 (output change) | 24zd E1 to E3 merged (keeps golden diffs per row apart); OQ-5 answered | AT-E2-*, `tests/unit/transforms/`, composite and pool tests, golden suites, draw-site sentries |
| E3 | C7 engine: `internal/env_flags.py`, sites, rename, env-flag sentry, fragment | engine | R1 | none | AT-E3-*, model pack and isolated-run tests, faker setup tests |
| E4 | C8 and C9: `generated_at`, NER helper calls, two sentries, fragment | engine | R1 | none | AT-E4-*, profiling and text_mask tests, out-of-core group C tests |
| CL1 | C1 part 1: `emit_error`, schema, the non-hot emitters, D7 sentry at an exact shrink-only baseline | CLI | R2 (public `--json` shape) | OQ-1, OQ-2 answered | AT-CL1-*, `tests/sentry/cli_text/`, affected command unit and e2e tests |
| CL2 | C6 and C7 CLI (not `_notify.py`): `emit_json_document`, `_env_flags.py`, stdout and env sentries | CLI | R1 | none | AT-CL2-*, update check and output tests |
| CL3 | C5 part 1: `_config_file.py`, `mask_secret_ref_problem`, every loader site except `run`, D8 sentry with the CLI-written allowlist | CLI | R2 (exit codes) | CL1 merged (uses `emit_error`) | AT-CL3-*, gate, tokenize, unmask, report, subset, preflight tests, `tests/sentry/test_mask_secret_ref_shape.py` |
| CL4 | Hot lanes: C1 part 2 (`run/_errors.py`, `_evidence.py`, `_notify.py`, `scan.py`), C2 `exit_code_for`, C5 part 2 (`run/_prepare.py`), C7 `_notify.py` SMTP port | CLI | R2 (exit codes, hot lane) | rows 62 (RESULT C slices touching `_errors.py`), 63a (RUN-STREAM C1) and 66 (F29 C1) merged; CL1 and CL3 merged | AT-CL4-*, `tests/unit/test_exit_codes.py`, run error-arm, demo, fit and notify tests, `tests/sentry/cli_text/` |
| CL5 | End-to-end checks of E1, E2, E4 through the CLI, no `src/` change | CLI | R1 | E1, E2, E4 in a daily engine release and the CLI repinned to it (row 77) | AT-CL5-1 |

E1, E3 and E4 touch no common file and can build in parallel; E2 waits for 24zd. CL1, CL2 and
CL3 need no engine release (`parse_ref` is in the pinned 0.23.0; stop line 15.7). CL4 is the
only slice that waits on the hot lanes; it is small once they land because CL1 and CL3 already
built the owners. Escalation triggers (`testing.md`): E1 edits `.importlinter` and moves digests
(serialization), so its merge head gets the full engine suite against the stored main baseline;
E2's goldens are the output alarm and must run; CL4 touches exit codes in the hot lanes, so the
CLI full suite runs on its merge head.

Estimate: E1 medium, E2 small, E3 small, E4 small, CL1 medium, CL2 small, CL3 medium, CL4
small to medium, CL5 small. Everything except CL4 and CL5 fits before 2026-11-24; CL4 lands as
soon as rows 62, 63a and 66 merge, before the 2026-12-04 build date, leaving REL-GATE (row 93)
unblocked.

## 11. Open questions (owner)

- **OQ-1. `--quiet` and errors.** (a) Every command prints the human error line to stderr under
  `--quiet`, as the help text says ("Suppress stdout. Errors still go to stderr."); (b) quiet
  prints nothing on error, as most commands do today, and the help text changes.
  **Recommended: (a).** A quiet script still needs to know why it failed, stderr does not mix
  with piped stdout, and it matches the documented contract.
- **OQ-2. `fit --json` error shape.** (a) Fold `fit` into the common envelope: `code` is a
  member, `error` is the plain message, `message` goes; (b) keep `fit`'s extra member and code
  prefix. **Recommended: (a).** One shape is the point of the row; consumers read `code`
  directly instead of splitting a string.
- **OQ-3. One exit map.** (a) `demo` and `fit` adopt `run`'s classifier through
  `exit_code_for`, so some `demo` failures move from 3 to 1 or 5; (b) keep per-command maps.
  **Recommended: (a).** The same failure should exit the same way in every command; exit codes
  are public UX, hence this question.
- **OQ-4. Canonical JSON rule.** (a) `ensure_ascii=True` with the closed `default` (dates,
  datetimes, decimals; anything else refused); (b) `ensure_ascii=False` (closer to RFC 8785),
  same `default`. **Recommended: (a).** Under (a) the seed-feeding fingerprint
  (`generators/derivation.py:204`) keeps its bytes, so no generated output moves; under (b)
  every generated column whose config holds non-ASCII text changes output. Escaping is not a
  privacy or correctness issue either way.
- **OQ-5. Composite providers with no seed (A3).** (a) One rule: `fresh_seed()` from
  `os.urandom`, documented as not reproducible; (b) one all-zero seed everywhere, reproducible
  but every unseeded pool identical; (c) refuse a missing seed. **Recommended: (a).** It
  matches NumPy's `default_rng(None)`, and two unseeded pools silently sharing draws is the
  correlation defect this row exists to remove. No production path passes no seed.
- **OQ-6. Generated-at shape.** (a) `+00:00`; (b) `Z`. **Recommended: (a).** Three of four
  writers use it and Python 3.10 parses only it.
- **OQ-7. `CUSTOM_PROVIDERS_DIR`.** (a) Rename to `DECOY_CUSTOM_PROVIDERS_DIR` with no alias
  (pre-GA hard delete); (b) keep the name. **Recommended: (a).** Every other engine variable has
  the prefix, and an unprefixed name can collide with an unrelated tool's variable.
- **OQ-8. Unknown flag spellings.** (a) Strict sets; an unknown value on a `DECOY_*` flag is
  refused with a message naming the variable, a security flag reads an unknown value as its
  safe side, and `CI` and `NO_COLOR` follow their external conventions; (b) an unknown value
  warns and uses the default. **Recommended: (a).** A typo in a flag that turns a check off
  should not pass silently, and a refusal names the fix.
- **OQ-9. C6 and C9 tier.** Neither moves a frozen surface (C6 keeps bytes; C9 changes message
  text, not the code). (a) Keep them in this row, marked slip-able to V1+ if the build date is
  at risk; (b) move them to V1+ now. **Recommended: (a).** Both are small and their sentries
  stop the copies growing while V1 rows add code.

## 12. Recurring-defect walk

| # | Catalog class | Result |
|---|---|---|
| 1 | Anchored validation | The schema's `code` pattern uses `\Z`; the flag parser compares whole stripped strings against a set, no regex. No other regex is added. |
| 2 | Strict wire and stored types | The error schema types every fixed member; `canonical_json_bytes` refuses non-JSON types instead of `str()` or `repr()`; `seed_bytes_to_int` refuses any length but 8. |
| 3 | No exception context | Loader, `CanonicalJsonError`, flag and seed errors are raised after the handler closes; AT-E1-4, AT-E3-3 and AT-CL3-3 assert `__cause__ is None and __context__ is None`. |
| 4 | Versioned read models | The error schema is versioned (`decoy-error-v1`); stored profiles with `Z` still load; the canonical rule change is versioned through the compat note, and pinned plans fail closed on digest mismatch rather than reading as current. |
| 5 | Normalized guard scanners | Each new sentry is AST-based and lists its covered shapes: D3 (`json.dumps` with `sort_keys` or `default` keywords, aliases of `json` and `dumps`), seed conversion (`default_rng`, `Generator`, `random.Random` called on `int.from_bytes(...)`, directly or through a local name), env flags (comparisons and membership tests on `os.environ.get`, `os.getenv`, `os.environ[...]`, with `.strip()` and `.lower()` chains), stdout JSON (`sys.stdout.write`, `print`, `typer.echo` of a `json.dumps` result), config load (`yaml.safe_load`, `yaml.load`, `YAML().load`). Deliberate evasion (`getattr`, `exec`) is V2-LOG. |
| 6 | No reaching into private modules | CLI calls only `decoy_engine.secret_providers.parse_ref` (public). Engine callers import `_canonical` through its package `__init__`; `internal/env_flags.py` is engine-only and the public import boundary sentry already covers `internal/`. |
| 7 | No hidden CI failures | No CI command or failure policy changes; E1 edits test configuration, so its full-suite baseline comparison is mandatory and named in section 10. |
| 8 | One input, one trust class per entry point | The loader takes a user path only; CLI-written text has its own allowlisted re-parse sites and never goes through the loader. `env_flag` reads user-set variables; the engine-set isolated-worker handshake keeps its own constant name and the same parser. |
| 9 | Mutations on the final head | Each mutation in section 6.1 reruns on the final head of its slice. |
| 10 | Allowlisted helpers are still checked | Every allowlist (D3 stored documents, D8 CLI-written re-parses, D7 baseline) keys on exact `(path, function name)` and a test fails when an entry no longer matches a real site. |
| 11 | No raised caps | No ceiling or baseline grows. D7 starts at today's count minus the converted sites and only shrinks. The engine root fan-out stays at 25 (a sub-package is added, not a module). Line-count pins moved by added lines, if any, are listed in each build report. |

## 13. V2-LOG

- CX-1 full conversion of the remaining error sites to `emit_error` (about 49 lines in 23
  files), then D7 at zero. Their output already matches the schema (AT-CL1-1).
- CX-2 remaining `parse_pipeline_config` except lists (`api.py:476`, `report.py:1762`, and nine
  more).
- RFC 8785 number form for canonical JSON.
- Deliberate evasion of the new sentries (`getattr(json, "dumps")`, `exec`).
- B11: no scheme check on `--notify` targets and `DECOY_VERSION_URL`.
- `connectors/sftp.py:138` default path read outside a helper.
- EX-2 V1+: routing all 68 generator constructions through `determinism/_rng.py` (24ze D2
  baselines them).
- ED-8 V1+: one `utc_now()` with a test override.

## 14. Pre-mortem

- **A digest moves that something stored depends on.** Pinned plans and subset manifests are the
  stored ones; both refuse on mismatch today, so the failure is loud. The `Breaking:` line names
  the recompile. A digest that feeds a seed would be silent, which is why OQ-4 (a) keeps the
  one seed-feeding fingerprint byte-identical and AT-E1-2 pins it.
- **A hashed site was missed.** The D3 sentry lists every `sort_keys` dump; an allowlisted
  document that turns out to be hashed is routed by the builder and the allowlist shrinks.
- **`joint_mask` table name is not reachable at the transform.** The strategy handler has
  `ctx` and the plan table; if a route calls `apply_joint_mask` without it, the builder stops
  (15.2) rather than defaulting to an empty name, which would merge groups across tables.
- **The hot lanes slip.** CL4 is the only slice that waits; the owners exist after CL1 and CL3,
  so CL4 is a short follow-on. If rows 62, 63a and 66 are not merged by 2026-11-28, the
  orchestrator decides whether CL4 rebases onto the open lanes.
- **`--quiet` users rely on silence.** OQ-1 decides; the change is to stderr only, so stdout
  pipes are unaffected.
- **24ze lands the same sentries first.** Each slice then shrinks the 24ze baseline instead of
  adding a file; the sentry names in section 8 match 24ze's so there is one file per rule.
- **A strict flag refuses a value a user's CI already sets.** Only unknown spellings refuse;
  every spelling `strtobool` accepted is accepted.

## 15. Stop lines for builders

1. Do not change `SEED_PROTOCOL_VERSION`, any `derive` input layout, the vault header bytes, or
   any reason code's name.
2. If `apply_joint_mask` is reached on any route without a table name, or `joint_mask` turns out
   to run on the native route, stop and report.
3. If a site in section 4.3 turns out to feed a seed, stop and report; only
   `generators/derivation.py:204` may feed seeds, and its bytes must not change.
4. Do not convert hot-lane files (`cli/run/_execute.py`, `_flow.py`, `_errors.py`, `_render.py`,
   `_evidence.py`, `_notify.py`, `cli/scan.py`) outside CL4.
5. A golden baseline may change only for fixtures that use `joint_mask` with null keys or gen
   mode (E2) or that print a moved digest (E1); any other golden diff is a stop.
6. Do not raise any module-size ceiling, fan-out allowlist or sentry baseline.
7. If `decoy_engine.secret_providers.parse_ref` is missing from the pinned engine, CL3 waits for
   the repin instead of copying the grammar.
8. No test runs on the devbox. Mac first, then `~/bin/vm-run`.

## 16. Verification status

Sites, line numbers, import tiers, fan-out counts and module sizes were read on engine
`8c9c1162` and CLI `88403cc`. Census and reachability facts come from `REMEDIATION.md` and
`REACHABILITY-A-B.md` and were not re-run. Which `sort_keys` dumps feed a hash was read from
the surrounding code for the routed sites; the allowlisted documents were not traced to every
reader. No test ran.
