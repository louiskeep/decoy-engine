# Engine release R-0190: facts for the CLI and platform repin

Status: prepared, not tagged. Branch `chore/release-0.19.0-prep`, cut off engine main `b4c6915d` (it carries #392
CLI-DIALECT Part E, #393 ENGINE-PAR P1, #394 CI-E1s, #389 subset refusal, #395 CI gate runner). The tag is cut by
`scripts/release/cut-release.sh` from main after this branch merges; the sha below is a placeholder until then.
Two more branches may merge first (RESULT, ENGINE-PAR P2 xdist); if either does, merge main and re-run
"Re-deriving the rows" below, which is mechanical.

Every value is followed by the command that prints it, run from the engine checkout root at the release commit with
the lock environment active (`uv sync --frozen --extra dev --extra lint --extra vault --extra cloud --python
3.10.20`, then `.venv/bin/python`).

## The release

| Fact | Value | Command |
|---|---|---|
| Engine version | `0.19.0` | `sed -n '/^\[project\]$/,/^\[/{s/^version = "\(.*\)"$/\1/p;}' pyproject.toml` |
| Same, as the module reports it | `0.19.0` | `python -c "import decoy_engine; print(decoy_engine.__version__)"` |
| Tag | `v0.19.0` (annotated, message `decoy-engine 0.19.0`) | `git ls-remote --tags origin refs/tags/v0.19.0` (empty until tagged) |
| Release commit | `<main sha that carries this branch>` | `git rev-parse origin/main` before tagging (the `EXPECTED_SHA` for cut-release.sh); after: `git rev-parse 'v0.19.0^{commit}'` |
| `PIN_PREFIX` for the platform | `<first 8 hex of the release commit>` | `git rev-parse 'v0.19.0^{commit}' \| cut -c1-8` |
| CHANGELOG section | `## [0.19.0] - 2026-11-07` | `grep -m2 '^## \[' CHANGELOG.md` |
| Release phase | `pre-ga` | `python -c "import decoy_engine; print(decoy_engine.RELEASE_PHASE)"` |

Why 0.19.0. The newest tag is `v0.18.0` (`git tag -l 'v0.18*'`; commit `f5b46a2c`). The release adds public names
(`read_csv_source`, `CsvDialect`, `resolve_csv_dialect`, the declared-casting module, the telemetry and safe-output
modules) and a few consumer-visible breaks (CHANGELOG `[0.19.0]` Breaking list), which a 0.x minor may carry
(compatibility contract sections 1 and 7). 1.0.0 stays reserved for GA.

Version single source: `pyproject.toml` `[project].version`; `tests/unit/test_version_agreement.py` holds
`__version__`, the installed metadata, the `uv.lock` root entry, the newest dated CHANGELOG section and the
`docs/proof-manifest.json` stamp to it. Pins that hash the engine's own version or catalog move with it: the reason
release pin, the DP certified rows, the FR0 manifest and the CHANGELOG ratchet.

## Contract versions the consumers hold

| Fact | Value | Command |
|---|---|---|
| Reason registry version | `1.6.0` (was `1.2.0` in v0.18.0) | `python -c "from decoy_engine import reasons; print(reasons.REGISTRY_VERSION)"` |
| Reason identities | `1006` (884 since 1.0.0, 7 since 1.1.0, 2 since 1.2.0, 12 since 1.3.0, 29 since 1.4.0, 71 since 1.5.0, 1 since 1.6.0); 893 at v0.18.0, none removed or renamed | `python -c "from decoy_engine import reasons; print(len(reasons.manifest()['entries']))"` |
| New identity at 1.6.0 | `config.subset_run_unsupported` | `python -c "from decoy_engine import reasons; [print(e['namespace'], e['code']) for e in reasons.manifest()['entries'] if e['since'] == '1.6.0']"` |
| Registry catalog digest | `cabb82cb592ecd9a8ea5ad80409283a916745d718ca0d06358cf7bdf0973c297` | `cat tests/fixtures/f29/reason-registry-release.json`; `python scripts/release/reason_release_pin.py --check` |
| Reason / event schema versions | `1` / `1` (unchanged) | `python -c "from decoy_engine.reasons import _registry as r; print(r.REASON_SCHEMA_VERSION, r.EVENT_SCHEMA_VERSION)"` |
| `PLAN_VERSION` | `3` (was `2`); a version-2 plan still loads | `git grep -n '^PLAN_VERSION' v0.18.0 HEAD -- src` |
| `SEED_PROTOCOL_VERSION` | `7` (unchanged) | `git grep -n '^SEED_PROTOCOL_VERSION' v0.18.0 HEAD -- src` |
| Native ABI the engine loads | `decoy-native-abi-3` (unchanged) | `git grep -n '^_EXPECTED_ABI_VERSION' v0.18.0 HEAD -- src` |
| Native companion version | `0.1.0` (own line, unpublished, not bumped here) | `grep -m1 '^version' decoy-engine-native/pyproject.toml decoy-engine-native/Cargo.toml` |
| Compatibility contract | no version number; blob `cb8d09c6064c47b79467b6de89c549b1011c2b64`, last changed in `b2ee6502` | `git rev-parse HEAD:docs/compatibility-contract.md`; `git log -1 --format=%h -- docs/compatibility-contract.md` |
| Compat contract changes since v0.18.0 | the SUB-STOP correction (subset refused by every run entry), `read_csv_source` / `CsvDialect` / `resolve_csv_dialect`, the roadmap link | `git diff v0.18.0 HEAD -- docs/compatibility-contract.md` |
| File-access entry points | `43` (was 42; the new one is `decoy_engine.profile._csv_reader.read_csv_source`) | `python -c "import decoy_engine.fileaccess as f; print(len(f.ENTRY_POINTS))"` |
| SBOM of this lock (CycloneDX 1.6) | sha256 `ce2ef0450e26be76fafa6c79746543c9fbc88f53693e28c519ad45230874c254` | `bash scripts/release/sbom.sh uv.lock /tmp/sbom.cdx.json && sha256sum /tmp/sbom.cdx.json` |
| FR0 manifest | `engine_minor` `0.19`; 15 envelope digests moved; every golden table digest unchanged | `python -m tests.parity._fr0_refreeze > tests/fixtures/phase_f_fr0/manifest.json` (byte-identical on re-run) |
| Proof manifest | only the `engine_version` stamp moved | `python scripts/gen_proof_manifest.py && git diff --stat docs/proof-manifest.json` |

## DP certified rows

`dp_provenance.check_fit_environment` refuses a fingerprint outside `_CERTIFIED_STACKS` with `dp_stack_uncertified`.
The fingerprint hashes every installed distribution's name and version, the engine's own included, so the version
bump moves both profiles. Unlike R-b, the dependency set also moved since v0.18.0: #394 (CI-E1s) re-locked eight
packages, so the dev profile differs from the released v0.18.0 row by more than the engine version.

Both rows below were derived by `derive_rows.py` (the script is the last section; run on a GCP VM, CPython
3.10.20 lock venv from `bootstrap.sh`, Linux x86-64 glibc):

```
~/bin/vm-run engine . '$PY .r0190/derive_rows.py .r0190/cli-requirements-certified.txt 3.10.20 0.18.0 .r0190/prior'
```

with `.r0190/cli-requirements-certified.txt` = `git -C <cli> show origin/main:requirements-certified.txt` (CLI main
`dd40398`) and `.r0190/prior/{pyproject.toml,uv.lock}` = `git show v0.18.0:<file>`. Output, unedited. To re-run, write the two scripts in the last section to `.r0190/` (not tracked) first:

```
installed set == lock set: True  (77 vs 77 dists)
DEV engine 0.19.0: dists=77 fp=7d1c1a2a2be450a59fa17d810663a7bfe5e825204cca99bfe039ba12b6226aed
DEV current lock, engine back to 0.18.0: fp=b9b42047dc09ce89593e0be2dc6f7125a91af661bbb644b3a27342c84fd44ab0
DEV v0.18.0 lock: dists=77 fp=31ecf94503eff107631191426072eee757af72a2229ab594d2fc2e8e6bfac7b1
DEV dists differing from the v0.18.0 lock (excluding nothing):
  certifi: 2026.6.17 -> 2026.10.18
  cryptography: 49.0.0 -> 50.0.0
  decoy-engine: 0.18.0 -> 0.19.0
  pyasn1: 0.6.3 -> 0.6.5
  urllib3: 2.7.0 -> 2.8.0
  werkzeug: 3.1.8 -> 3.1.9
CLI pins=63  profile dists=65
CLI engine 0.19.0: fp=795732eb0808001d9c4591f3693637187637bb383a87b8caa95b8f2f269f9211
CLI engine 0.18.0: fp=b643894ded2ab84c4a0a9d9032a1de3d2330db89161f231e509024428b7533f3
CLI dists differing from the prior-release row:
  decoy-engine: 0.18.0 -> 0.19.0
proof-critical opendp annotated 0.15.1: dev=0.15.1 cli=0.15.1
proof-critical dp-accounting annotated 0.6.0: dev=0.6.0 cli=0.6.0
proof-critical numpy annotated 2.2.6: dev=2.2.6 cli=2.2.6
proof-critical scipy annotated 1.15.3: dev=1.15.3 cli=1.15.3
proof-critical pandas annotated 2.3.3: dev=2.3.3 cli=2.3.3
proof-critical pyarrow annotated 24.0.0: dev=24.0.0 cli=24.0.0
```

| Row | Fingerprint | Dists | Differs from the 0.18.0 rows by |
|---|---|---|---|
| Engine dev/CI profile, 0.19.0 (new) | `7d1c1a2a2be450a59fa17d810663a7bfe5e825204cca99bfe039ba12b6226aed` | 77 | vs `b9b42047...` (same lock, engine 0.18.0, from #394): `decoy-engine` only. vs released `31ecf945...`: `decoy-engine` plus certifi, cryptography, pyasn1, urllib3, werkzeug (#394). Not in `_CERTIFIED_ANNOTATIONS`. |
| decoy-cli runtime profile, 0.19.0 (new) | `795732eb0808001d9c4591f3693637187637bb383a87b8caa95b8f2f269f9211` | 65 | vs `b643894d...`: `decoy-engine` only. The 63 pins are the CLI's `requirements-certified.txt`; derived by hash, not installed; `cert_smoke.py` on a real install is the proof of record (CLI Part C). |

No proof-critical library (`_CERTIFIED_ANNOTATIONS`: opendp, dp-accounting, numpy, scipy, pandas, pyarrow) moved. The
engine row was also checked on a fresh install: the installed set equals the lock set (77 dists) and
`check_fit_environment()` admits it (`tests/sentry/test_dp_lock_row.py`, `release_checks.sh`). All earlier rows stay
(`5a76ea92...`, `a7b40d54...` for 0.17.0; `31ecf945...`, `b643894d...` for 0.18.0; `b9b42047...`, the 0.18.0 engine
on the CI-E1s lock) so older snapshots still compile (`tests/unit/generation/test_dp_prior_release_rows.py`).
`dp_provenance.py` stays at its 605-line census: the new rows are paid for by merging comment lines.

## Re-deriving the rows

When another branch merges first, merge `origin/main` into this branch and re-run, in order: the `derive_rows.py`
command above (the dev row moves if `uv.lock` or the engine version changed, for example ENGINE-PAR P2 adding
pytest-xdist to the dev extra; the CLI row moves only if the CLI's `requirements-certified.txt` changed), then
`python scripts/release/reason_release_pin.py --write`, `python scripts/gen_proof_manifest.py`, the FR0 command in
the table, and the CHANGELOG pin (`_digest` of the `[0.18.0]` section, see `test_changelog_breaking_ratchet.py`).
STOP if a proof-critical library in `_CERTIFIED_ANNOTATIONS` moved or a golden table digest in the FR0 manifest moved.

## Pin sites the consumers hold

CLI (`louiskeep/decoy`, read at CLI main `dd40398`; on 0.18.0):

| Site | Now | Becomes |
|---|---|---|
| `pyproject.toml` dependency | `decoy-engine==0.18.0` | `decoy-engine==0.19.0` |
| `tests/unit/test_pyproject_metadata.py:112-113` | `==0.18.0` | `==0.19.0` |
| `.github/workflows/ci.yml:263`, `release-smoke.yml:83` | `decoy-engine@v0.18.0` | `@v0.19.0` |
| `scripts/dev/bootstrap.sh:32` (comment), `scripts/dev/lock_version.sh:17` (`ENGINE_SPEC`) | `@v0.18.0` | `@v0.19.0` |
| `tests/sentry/test_ci_lock_installs.py:45` (`ENGINE`) | `@v0.18.0` | `@v0.19.0` |
| `docs/release/fresh-install-smoke.md` (lines 47, 87, 115) | `@v0.18.0` | `@v0.19.0` |
| `tests/unit/test_sbom_script.py:721` | `decoy-engine==0.18.0` fixture text | check whether it must follow |
| `requirements-certified.txt` header (and its install line), `scripts/cert_smoke.py` docstring | `b643894d...` on engine 0.18.0 | `795732eb...` on engine 0.19.0 (the 63 pins do not change) |
| `requirements-dev.lock` | engine line | `scripts/dev/lock.sh` (only the engine line moves) |
| `src/decoy/ci/rule_codes.py:105` literal codes (sources at engine v0.18.0), `tests/unit/test_ci_rule_codes.py:97` | v0.18.0 | the #79 way after the repin |
| `tests/e2e/test_plan_command.py` | `plan_version` 2 | 3 (an intended plan format change) |
| `src/decoy/cli/_token_vault.py:3`, `tests/sentry/test_engine_secret_file_check.py:3` (comment and docstring text only) | "engine ==0.18.0" / "Engine 0.18.0" | optional wording update to 0.19.0; no behavior reads them |

Command: `git grep -n '0\.18\.0\|b643894d\|@v0\.18' origin/main -- pyproject.toml tests .github requirements-certified.txt scripts src docs/release`.

Platform (`louiskeep/decoy-platform`, read at platform main `fbaee2a9`; parked on 0.18.0, no repin now). The
engine-repin checklist lists seven items to move together when it follows:

| Site | Now | Becomes |
|---|---|---|
| `pyproject.toml` dependency (`:43`) | `decoy-engine==0.18.0` | `decoy-engine==0.19.0` |
| `api/engine_compat.py:190` `ENGINE_TRANSFORM_OWNERSHIP_SPECIFIER` | `"==0.18.0"` | `"==0.19.0"` |
| `deploy/Dockerfile.production:92` `ARG DECOY_ENGINE_EXPECTED_VERSION` | `0.18.0` | `0.19.0` |
| `.github/workflows/backend.yml:65` and `fixtures-verify.yml:44` `ENGINE_PIN_VERSION` (and `ENGINE_PIN_SHA`) | `"0.18.0"` / `f5b46a2c...` | `"0.19.0"` / release commit |
| `tests/test_engine_version_fence_packaging.py` `PIN_PREFIX` / `PIN_VERSION` | `f5b46a2c` / `0.18.0` | `PIN_PREFIX` above / `0.19.0` |
| `tests/test_engine_ownership_gate.py:25,50-52` | `0.18.0` variants | `0.19.0` variants |
| `tests/test_ds2_postgres_pull.py:817-818`, `tests/test_di0_quality_contract.py:58` (`FROZEN_ENGINE_VERSION`) | `0.18.0` | `0.19.0` |

Also the platform's `api/engine_compat.py:48` (PipelineConfig `version` accepted) and the plan-format fence need a
look because `PLAN_VERSION` is now 3. Command: `git grep -n '0\.18\.0' origin/main -- pyproject.toml api deploy .github tests ':!*.md'`.

## What consumers must change

| Consumer | Change | Why (engine side) |
|---|---|---|
| CLI, platform | Anything that runs a config with `subset:` set must run `run_subset` first; the run entries refuse it with `config.subset_run_unsupported`. | #389 |
| CLI, platform | Map the 113 reason identities added since 1.2.0 (registry 1.6.0) wherever codes are enumerated or pinned. | registry 1.2.0 -> 1.6.0 |
| CLI, platform | Plans written by this engine are `version: 3`; a 0.18.0 engine cannot load them. Tests that assert `plan_version == 2` move to 3. | F06 declared casting |
| CLI | Headerless CSV refusals now start with the code and name the source table: output that prints the code apart from the message shows it twice. Read every CSV source through `decoy_engine.read_csv_source` (CLI Part C). | CLI-DIALECT |
| CLI | The pin sites above, including the certified fingerprint. Without the new header and an engine 0.19.0 install, `fit_dp_snapshot` refuses with `dp_stack_uncertified`. | DP row `795732eb...` |

## Public surface changed since v0.18.0

Compared by `surface_diff.py` (last section) over `git archive v0.18.0 src` and the release tree. Surface = every
module with no `_`-prefixed or `internal` path segment, by `__all__` (public top-level names where a module has
none), with re-exports resolved to their definitions in private modules. Class members are public methods and
annotated fields only, so it is a census to read, not a proof of absence. Output, unedited:

```
modules removed: none
modules added: ['decoy_engine.contracts', 'decoy_engine.contracts.declared_casting', 'decoy_engine.declared_casting', 'decoy_engine.reasons.platform', 'decoy_engine.reasons.safe_output', 'decoy_engine.safe_output', 'decoy_engine.telemetry', 'decoy_engine.verification.canary']
ADDED   decoy_engine.CsvDialect
ADDED   decoy_engine.read_csv_source
ADDED   decoy_engine.resolve_csv_dialect
CHANGED decoy_engine.ExecutionResult
   old: class() outputs: dict[str, pa.Table]; timings: tuple[StrategyTimingRecord, ...]; boundary_conversion_ms: float; warnings: tuple[QualityWarning, ...]; quality_metrics: dict[str, Any]; table_kinds: dict[str, str]; row_errors: tuple[RowErrorRecord, ...]; output(self) -> pa.Table
   new: class() outputs: dict[str, pa.Table]; timings: tuple[StrategyTimingRecord, ...]; boundary_conversion_ms: float; warnings: tuple[QualityWarning, ...]; quality_metrics: dict[str, Any]; table_kinds: dict[str, str]; row_errors: tuple[RowErrorRecord, ...]; quarantine_access: Any; output(self) -> pa.Table
CHANGED decoy_engine.PipelineConfig
   old: class(BaseModel) version: Literal[2]; global_settings: GlobalSettings; sources: dict[str, SourceDescriptor]; tables: list[TableConfig]; relationships: list[RelationshipConfig]; targets: dict[str, TargetDescriptor]; namespaces: dict[str, NamespaceConfig]; post_scan: PostScanConfig; validators: list[V
   new: class(BaseModel) version: Literal[2]; global_settings: GlobalSettings; sources: dict[str, SourceDescriptor]; tables: list[TableConfig]; relationships: list[RelationshipConfig]; targets: dict[str, TargetDescriptor]; namespaces: dict[str, NamespaceConfig]; post_scan: PostScanConfig; validators: list[V
CHANGED decoy_engine.run_pipeline
   old: def(config: dict[str, Any], sources: Mapping[str, pa.Table | LazySource] | None=None, *, engine_version: str, registry: ProviderRegistry | None=None, derive_key: Any=None, instance_default_locale: str | None=None, vault_writer: Any=None, token_vault_writer: Any=None, fidelity_report: bool=False, pos
   new: def(config: dict[str, Any], sources: Mapping[str, pa.Table | LazySource] | None=None, *, engine_version: str, registry: ProviderRegistry | None=None, derive_key: Any=None, instance_default_locale: str | None=None, vault_writer: Any=None, token_vault_writer: Any=None, fidelity_report: bool=False, pos
CHANGED decoy_engine.config.ColumnConfig
   old: class(BaseModel) name: str; strategy: str; provider: str | None; namespace: str | None; cardinality_mode: CardinalityModeLiteral | None; deterministic: bool; scale: float | None; provider_config: dict[str, Any]; coherent_with: list[str]; backend_type: Literal['faker', 'mimesis', 'pool', 'decoy_nativ
   new: class(BaseModel) name: str; strategy: str; provider: str | None; namespace: str | None; cardinality_mode: CardinalityModeLiteral | None; deterministic: bool; scale: float | None; provider_config: dict[str, Any]; coherent_with: list[str]; backend_type: Literal['faker', 'mimesis', 'pool', 'decoy_nativ
CHANGED decoy_engine.config.FixedWidthLayout
   old: class(BaseModel) columns: list[FixedWidthColumn]; record_width(self) -> int
   new: class(BaseModel) columns: list[FixedWidthColumn]; exact_record_width: bool; record_width(self) -> int
CHANGED decoy_engine.config.PipelineConfig
   old: class(BaseModel) version: Literal[2]; global_settings: GlobalSettings; sources: dict[str, SourceDescriptor]; tables: list[TableConfig]; relationships: list[RelationshipConfig]; targets: dict[str, TargetDescriptor]; namespaces: dict[str, NamespaceConfig]; post_scan: PostScanConfig; validators: list[V
   new: class(BaseModel) version: Literal[2]; global_settings: GlobalSettings; sources: dict[str, SourceDescriptor]; tables: list[TableConfig]; relationships: list[RelationshipConfig]; targets: dict[str, TargetDescriptor]; namespaces: dict[str, NamespaceConfig]; post_scan: PostScanConfig; validators: list[V
ADDED   decoy_engine.errors.FixedWidthRecordOverlongError
CHANGED decoy_engine.execution.ExecutionResult
   old: class() outputs: dict[str, pa.Table]; timings: tuple[StrategyTimingRecord, ...]; boundary_conversion_ms: float; warnings: tuple[QualityWarning, ...]; quality_metrics: dict[str, Any]; table_kinds: dict[str, str]; row_errors: tuple[RowErrorRecord, ...]; output(self) -> pa.Table
   new: class() outputs: dict[str, pa.Table]; timings: tuple[StrategyTimingRecord, ...]; boundary_conversion_ms: float; warnings: tuple[QualityWarning, ...]; quality_metrics: dict[str, Any]; table_kinds: dict[str, str]; row_errors: tuple[RowErrorRecord, ...]; quarantine_access: Any; output(self) -> pa.Table
CHANGED decoy_engine.execution.run_pipeline
   old: def(config: dict[str, Any], sources: Mapping[str, pa.Table | LazySource] | None=None, *, engine_version: str, registry: ProviderRegistry | None=None, derive_key: Any=None, instance_default_locale: str | None=None, vault_writer: Any=None, token_vault_writer: Any=None, fidelity_report: bool=False, pos
   new: def(config: dict[str, Any], sources: Mapping[str, pa.Table | LazySource] | None=None, *, engine_version: str, registry: ProviderRegistry | None=None, derive_key: Any=None, instance_default_locale: str | None=None, vault_writer: Any=None, token_vault_writer: Any=None, fidelity_report: bool=False, pos
CHANGED decoy_engine.execution.out_of_core.run_fk_out_of_core
   old: def(plan: Plan, sources: Mapping[str, pa.Table | LazySource], *, registry: ProviderRegistry, relationship_graph: RelationshipGraph, sink: TransactionalSink | None=None, temp_dir: Path | None=None, memory_limit: str | None=None, batch_rows: int | None=None, budget_bytes: int | None=None, temp_disk_bu
   new: def(plan: Plan, sources: Mapping[str, pa.Table | LazySource], *, registry: ProviderRegistry, relationship_graph: RelationshipGraph, sink: TransactionalSink | None=None, temp_dir: Path | None=None, memory_limit: str | None=None, batch_rows: int | None=None, budget_bytes: int | None=None, temp_disk_bu
ADDED   decoy_engine.fileaccess.write_private
CHANGED decoy_engine.fileaccess.adapters.arrow.guarded_iter
   old: def(display: str, strict: bool, it: Iterable[T]) -> Iterator[T]
   new: def(display: str, strict: bool, it: Iterable[T], parser_errors: tuple[type[Exception], ...]=()) -> Iterator[T]
ADDED   decoy_engine.fileaccess.adapters.pandas.guarded_iter
ADDED   decoy_engine.fileaccess.adapters.pandas.guarded_with_parser_errors
ADDED   decoy_engine.fileaccess.adapters.pandas.read_csv_chunks
ADDED   decoy_engine.fileaccess.adapters.pandas.read_csv_chunks_parser_errors
ADDED   decoy_engine.fileaccess.adapters.pandas.read_csv_parser_errors
CHANGED decoy_engine.io.csv_convert_options
   old: def(names: list[str]) -> pa_csv.ConvertOptions
   new: def(names: list[str], lexeme_columns: frozenset[str]=frozenset()) -> pa_csv.ConvertOptions
CHANGED decoy_engine.io.iter_csv_chunks
   old: def(path: str, budget: ChunkBudget, *, table: str, file_access: FileAccess | None=None, field_key: str='source.path') -> Iterator[pa.Table]
   new: def(path: str, budget: ChunkBudget, *, table: str, file_access: FileAccess | None=None, field_key: str='source.path', lexeme_columns: frozenset[str]=frozenset()) -> Iterator[pa.Table]
CHANGED decoy_engine.io.bounded_reader.csv_convert_options
   old: def(names: list[str]) -> pa_csv.ConvertOptions
   new: def(names: list[str], lexeme_columns: frozenset[str]=frozenset()) -> pa_csv.ConvertOptions
CHANGED decoy_engine.io.bounded_reader.iter_csv_chunks
   old: def(path: str, budget: ChunkBudget, *, table: str, file_access: FileAccess | None=None, field_key: str='source.path') -> Iterator[pa.Table]
   new: def(path: str, budget: ChunkBudget, *, table: str, file_access: FileAccess | None=None, field_key: str='source.path', lexeme_columns: frozenset[str]=frozenset()) -> Iterator[pa.Table]
CHANGED decoy_engine.plan.Plan
   old: class() plan_version: int; seed_protocol_version: int; engine_version: str; pipeline_config_hash: str; profile_hash: str; seed_envelope: SeedEnvelope; relationships: tuple[PlanRelationship, ...]; namespaces: tuple[NamespaceBinding, ...]; ordering: tuple[OrderingNode, ...]; plan_compile: PlanCompileR
   new: class() plan_version: int; seed_protocol_version: int; engine_version: str; pipeline_config_hash: str; profile_hash: str; seed_envelope: SeedEnvelope; relationships: tuple[PlanRelationship, ...]; namespaces: tuple[NamespaceBinding, ...]; ordering: tuple[OrderingNode, ...]; plan_compile: PlanCompileR
CHANGED decoy_engine.profile.build_profile_source
   old: def(descriptor: dict[str, Any], *, field_key: str='source.path') -> ProfileSource
   new: def(descriptor: dict[str, Any], *, field_key: str='source.path', table: str='source') -> ProfileSource
REMOVED decoy_engine.profiling.eval.corpus.make_iban
REMOVED decoy_engine.profiling.eval.corpus.make_npi
ADDED   decoy_engine.reasons.platform
ADDED   decoy_engine.reasons.safe_output
```

Reading it. No public module was removed. `profiling.eval.corpus.make_iban` and `make_npi` are listed as removed
only because `corpus.py` imported them and the H-1 split moved that code to `_corpus_values.py`; they are defined in
`profiling.eval.fixtures` and still exported by `decoy_engine.profiling.eval` (`git grep -n 'make_iban' HEAD -- src/decoy_engine/profiling/eval`). Every
`CHANGED` line is additive: `ExecutionResult.quarantine_access`, `FixedWidthLayout.exact_record_width`, new keyword
parameters with defaults on `run_pipeline` (`quarantine_runtime`), `run_fk_out_of_core`, `iter_csv_chunks` and
`csv_convert_options` (`lexeme_columns`), `guarded_iter` (`parser_errors`), `build_profile_source` (`table`), and new
`ColumnConfig` and `Plan` fields. Breaking behavior is in the CHANGELOG Breaking list: the subset refusal, plan
version 3, registry 1.6.0, the headerless refusal wording and the `column_names` type refusal.

## Not run here

- The real tag push (`APPLY=1 scripts/release/cut-release.sh <sha>`), a GitHub release, PyPI upload.
- The native companion build and release workflow.
- The CLI-profile DP row on a real install (`cert_smoke.py`); derived by hash only.
- The CLI and platform repins themselves.
- A full suite (the orchestrator runs it on the merge head).

## Derivation scripts (exact text that produced the output above)

`.r0190/derive_rows.py`:

```python
"""Derive the 0.19.0 DP certified rows. Run with the lock venv python from the engine checkout root.

usage: python derive_rows.py CLI_REQUIREMENTS_CERTIFIED PYTHON_FULL PRIOR_VERSION PRIOR_DIR
PRIOR_DIR holds the prior release's pyproject.toml and uv.lock (git show v0.18.0:<file>).
Prints, for the dev/CI profile and the CLI runtime profile: the distribution count, the
fingerprint, the fingerprint with only decoy-engine set back to the prior version, and the
distributions that differ from the prior release's lock. No network.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

from decoy_engine.quality import dp_provenance as d

EXTRAS = ["dev", "lint", "vault", "cloud"]


def lock_set(root: Path, py_full: str) -> dict[str, str]:
    """The dev/CI profile as test_dp_lock_row computes it: uv export + markers, plus the engine."""
    flags = [f for e in EXTRAS for f in ("--extra", e)]
    out = subprocess.run(
        ["uv", "export", "--frozen", "--no-emit-project", "--no-hashes", "--no-header", *flags],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    env = {
        "implementation_name": "cpython", "implementation_version": py_full, "os_name": "posix",
        "platform_machine": "x86_64", "platform_release": "", "platform_system": "Linux",
        "platform_version": "", "platform_python_implementation": "CPython",
        "python_version": py_full.rsplit(".", 1)[0], "python_full_version": py_full,
        "sys_platform": "linux",
    }
    pins: dict[str, str] = {}
    for line in out.splitlines():
        if not line or line.startswith((" ", "#")):
            continue
        spec, _, marker = line.partition(" ; ")
        if marker and not Marker(marker).evaluate(env):
            continue
        name, _, version = spec.partition("==")
        pins[str(canonicalize_name(name))] = version
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    pins["decoy-engine"] = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    return pins


def fp(pins: dict[str, str], **override: str) -> str:
    merged = {**pins, **{k.replace("_", "-"): v for k, v in override.items()}}
    return d.compute_lock_fingerprint(merged.items())


def diff(old: dict[str, str], new: dict[str, str]) -> list[str]:
    rows = [f"{n}: {old.get(n, '-')} -> {new.get(n, '-')}" for n in sorted(set(old) | set(new))
            if old.get(n) != new.get(n)]
    return rows or ["(none)"]


def main(cli_req: str, py_full: str, prior_version: str, prior_dir: str) -> None:
    root = Path.cwd()
    now = lock_set(root, py_full)
    installed = dict(d.installed_distribution_set())
    print(f"installed set == lock set: {installed == now}  ({len(installed)} vs {len(now)} dists)")
    print(f"DEV engine {now['decoy-engine']}: dists={len(now)} fp={fp(now)}")
    print(f"DEV current lock, engine back to {prior_version}: fp={fp(now, decoy_engine=prior_version)}")
    prior = lock_set(Path(prior_dir), py_full)
    print(f"DEV v{prior_version} lock: dists={len(prior)} fp={fp(prior)}")
    print("DEV dists differing from the v" + prior_version + " lock (excluding nothing):")
    print("\n".join("  " + r for r in diff(prior, now)))

    cli = {}
    for line in Path(cli_req).read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([^\s;#]+)", line.strip())
        if m:
            cli[str(canonicalize_name(m.group(1)))] = m.group(2)
    base = {**cli, "decoy-cli": "0.1.0"}
    new_cli = {**base, "decoy-engine": now["decoy-engine"]}
    old_cli = {**base, "decoy-engine": prior_version}
    print(f"CLI pins={len(cli)}  profile dists={len(new_cli)}")
    print(f"CLI engine {now['decoy-engine']}: fp={fp(new_cli)}")
    print(f"CLI engine {prior_version}: fp={fp(old_cli)}")
    print("CLI dists differing from the prior-release row:")
    print("\n".join("  " + r for r in diff(old_cli, new_cli)))
    ann = d._CERTIFIED_ANNOTATIONS[f"{py_full}.final"]
    for lib, ver in ann.items():
        got = {"dev": now.get(lib), "cli": cli.get(lib)}
        print(f"proof-critical {lib} annotated {ver}: dev={got['dev']} cli={got['cli']}")


if __name__ == "__main__":
    main(*sys.argv[1:5])
```

`.r0190/surface_diff.py`:

```python
"""AST diff of the public decoy_engine surface between two source trees.

usage: python surface_diff.py OLD_SRC NEW_SRC     (each a `src` dir, e.g. from `git archive TAG src`)
Public module = no path segment starting with `_` and none named `internal`. Public name =
`__all__` entry when the module has one, else a top-level def/class/assignment without a leading `_`.
Prints modules added/removed, names added/removed, and changed signatures (functions, methods,
dataclass-style fields). Re-exports from private modules show up through the importing module's
`__all__`/imports only as names; their bodies are compared where defined (private modules too).
"""

from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path


def public_module(rel: Path) -> bool:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return not any(p.startswith("_") or p == "internal" for p in parts)


def sig(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"({ast.unparse(node.args)}) -> {ast.unparse(node.returns) if node.returns else ''}"
    return ""


@functools.lru_cache(maxsize=None)
def resolve(src: Path, modname: str, name: str, depth: int = 0) -> str | None:
    """Description of `name` as defined in (or re-exported by) module `modname`, private or not."""
    base = src.joinpath(*modname.split("."))
    f = base / "__init__.py" if base.is_dir() else base.with_suffix(".py")
    if not f.exists() or depth > 4:
        return None
    return describe(ast.parse(f.read_text(encoding="utf-8")), src, modname, f.name == "__init__.py",
                    all_names=True, depth=depth + 1).get(name)


def describe(tree: ast.Module, src: Path, modname: str, is_pkg: bool, all_names: bool = False,
             depth: int = 0) -> dict[str, str]:
    out: dict[str, str] = {}
    allv = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in n.targets):
            try:
                allv = set(ast.literal_eval(n.value))
            except Exception:
                allv = None
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = "def" + sig(n)
        elif isinstance(n, ast.ClassDef):
            members = []
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    not m.name.startswith("_") or m.name in ("__init__", "__call__")
                ):
                    members.append(f"{m.name}{sig(m)}")
                elif isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
                    members.append(f"{m.target.id}: {ast.unparse(m.annotation)}")
            out[n.name] = "class(" + ", ".join(ast.unparse(b) for b in n.bases) + ") " + "; ".join(members)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = "value"
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and allv:
            pass
        if isinstance(n, ast.ImportFrom) and n.level + (1 if n.module is None else 0) >= 0:
            pkg = modname.split(".") if is_pkg else modname.split(".")[:-1]
            if n.level:
                target = pkg[: len(pkg) - (n.level - 1)] + (n.module.split(".") if n.module else [])
            else:
                target = (n.module or "").split(".")
            if target and target[0] == "decoy_engine":
                for a in n.names:
                    got = resolve(src, ".".join(target), a.name, depth)
                    out.setdefault(a.asname or a.name, got or "import")
    if all_names:
        return out
    if allv is not None:
        return {k: v for k, v in out.items() if k in allv}
    return {k: v for k, v in out.items() if not k.startswith("_")}


def load(src: Path) -> dict[str, dict[str, str]]:
    mods: dict[str, dict[str, str]] = {}
    for f in sorted(src.rglob("*.py")):
        rel = f.relative_to(src)
        if public_module(rel):
            parts = list(rel.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mods[".".join(parts)] = describe(ast.parse(f.read_text(encoding="utf-8")), src, ".".join(parts), f.name == "__init__.py")
    return mods


def main(old: str, new: str) -> None:
    a, b = load(Path(old)), load(Path(new))
    print("modules removed:", sorted(set(a) - set(b)) or "none")
    print("modules added:", sorted(set(b) - set(a)) or "none")
    for m in sorted(set(a) & set(b)):
        for name in sorted(set(a[m]) - set(b[m])):
            print(f"REMOVED {m}.{name}")
        for name in sorted(set(b[m]) - set(a[m])):
            print(f"ADDED   {m}.{name}")
        for name in sorted(set(a[m]) & set(b[m])):
            if a[m][name] != b[m][name]:
                print(f"CHANGED {m}.{name}\n   old: {a[m][name][:300]}\n   new: {b[m][name][:300]}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```
