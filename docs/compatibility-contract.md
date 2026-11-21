# Decoy Compatibility Contract — The Frozen Surface

> **Status:** governance document. **Audience:** every engineer touching
> `decoy-engine`, `decoy` (CLI), or `decoy-platform`. **Post-launch this is a
> required pre-read before any feature branch.** **Owner:** PO.

## Why this document exists

We want to add features as **minor** releases that existing users can adopt
without re-masking their data, re-generating their reports, or losing access to
their vaults. The alternative — cutting a new major version every time we add a
capability — punishes our most committed users and stalls adoption.

That is only possible if we agree, in writing and in advance, on what is
**frozen** (cannot change within a major version) versus what is **fluid** (free
to change). This document is that agreement. If you are about to change anything
in the "frozen surface" below, stop and follow the decision procedure in §5.

The one-sentence rule of thumb, which you can hand to any contributor:

> **A new version adds capability and reads everything older versions wrote;
> output for an unchanged config never changes within a major; the vault is
> forever.**

## 1. The pre-GA → GA flip (read this first)

Today we are **pre-GA**, and engineering-best-practices **§8.1 ("pre-GA = hard
delete")** is in force: breaking changes need no shims, no migration adapters,
no deprecation horizons, because **nobody depends on anything yet.** That rule
is correct right now.

**The day we ship to a real user, §8.1 inverts for everything in §3 below.**
"Defensive code for users who don't exist" becomes "the product feature that
keeps paying users working." There is no gradual transition. The single most
important cultural event at launch is recognizing that the frozen surface is now
frozen, and that the team's reflex of "just delete it and refresh the fixtures"
is, for that surface, now a breaking change to a customer.

**Pre-GA corrections already taken (F13, 2026-10-30).** The keyed `job_seed` fallback is removed in
every release phase: keyed work with no admitted secret fails with `keyed_strategy_requires_secret`, and
`SeedKeyProvider` is import-compatible but never a secret. `KeyProvider` (the two-member protocol) is
unchanged and duck-typed providers keep working through `secret_providers.prepare_legacy`; the new
`secret-provider/v1` protocol, `decoy_engine.secret_providers` exports and the `secret_registry` /
`secret_cache_policy` keywords of `run_pipeline` are additive. Removed pre-GA with no shim:
`keyprovider.resolve_mask_secret_ref` and `resolve_key_provider`.

**Pre-GA corrections taken with the file-access policy (A-4 merged with F13, 2026-11-01).** Additive:
every file-reading or file-writing public entry point listed in `decoy_engine.fileaccess.ENTRY_POINTS`
takes a keyword-only `file_access: FileAccess | None = None` (`run_pipeline` now has 42 frozen
parameters: F13's `secret_registry` and `secret_cache_policy`, A-4's `file_access`, and project-layout E2's `quarantine_path`), and
`keyprovider.key_provider_from_ref(ref, *, key_version: str | None = None, file_access=None) ->
KeyProvider` is one of them. New registered reasons: `runtime.secret_file_permissions_unsafe` and
`decoy.file_access_denied`. Breaking, no shim: a `file:` mask secret readable by group or other is
refused; `env:` and `file:` secrets are never cached even with a positive `SecretCachePolicy` TTL.
`resolve_mask_secret_ref` stays deleted (F13).

**Pre-GA corrections taken with the project layout (E2, 2026-11-09).** Additive: `run_pipeline(...,
quarantine_path: str | None = None)` is keyword-only. It replaces `quarantine.output_path` for that run,
applied after the plan and its config hash are built, so the hash and the caller's dict do not change.
Anything but `None` or a non-blank `str` raises `ExecutionError` (`invalid_execution_knob`) before any
source is read. Breaking, no shim: the engine logger no longer writes `logs/decoy_engine.log` when the
logging config has no `file`; a caller that wants a log file passes `file`.

**Pre-GA additive keyword with GEN-CLI E1 (#460, 2026-11-10).** `run_pipeline(..., base_dir: str |
Path | None = None)` is keyword-only (44 frozen parameters). Relative config paths resolve against it;
`None` leaves every path as written (today's behavior).

**Pre-GA correction (vault key floor, 2026-11-12).** Breaking, no shim: `VaultWriter`, `load_vault` and
the run-entry vault key-match guard refuse a key that is not the 32-byte mask root with
`weak_mask_secret`, and `vault_writer_for_config` raises `keyed_strategy_requires_secret` when no secret
is admitted, for every config. Seed-keyed vaults written by older engines no longer open and are
regenerated with their masked output under a secret; there is no migration. The section 3.2
forever-readable freeze takes effect at the first real `decoy-vault/v2` vault, which after slice 0 can
only be written under a secret of at least 32 bytes; from that vault on, the v2 reader (kept, read-only,
beside v3), the 32-byte vault key length and the vault key labels are permanent, per this contract's own
pre-GA to GA rule (section 1).

**Pre-GA correction (freeze batch O1, `when:` on integer columns, 2026-11-18).** Breaking, no shim: a
`when:` predicate over an integer column compares exactly beyond 2**53, for a value or an integer
literal, on every route, and uses one NaN-style rule for a blank cell on every route and column class:
`!=`, `not in` and `not (...)` select a blank row; `==`, `in` and the ordering tests do not. FK keys,
declared integers, `date_shift` anchors, `top_code` columns and `group_key` siblings used the pandas
`NA` rule before, so they now mask more rows. A null-bearing integer column that a `when:` reads and
that passes through is written as `int64` instead of `double`.

**Pre-GA additive name with GEN-CATALOG GC3-E (2026-11-17).** `decoy_engine.code_set_catalog() -> dict[str, str]` returns each shipped code set name with its one-line description, in name order, read from the engine registry. It returns a new dict per call and reads no corpus file. No reason code, schema, config key or exit code changes.

**Pre-GA additive keyword with SUB-RUN E2 (2026-11-12).** `run_pipeline(..., subset_request:
SubsetRequest | Literal["config", "skip"] = "config")` is keyword-only (46 frozen parameters). `"config"`
applies a `subset:` block, `"skip"` runs the full sources, a `SubsetRequest` replaces the block; any other
value raises `ExecutionError` (`invalid_execution_knob`) before any read. `decoy_engine.subset` adds
`split_subset_config`, `SubsetSplit` and the E1 names, and `SubsetSelection` gains the opaque
`materialization_check` stage-transfer field (never an output). A config with no `subset:` block behaves
exactly as before.

**Pre-GA additive keyword with RUN-STREAM E5 (2026-11-15).** `run_pipeline(..., resident_fallback: bool =
False)` is keyword-only (47 frozen parameters). With `True` on a plain `auto` run, the run reads whole,
before routing, each caller `LazySource` it cannot keep lazy (a vault run, a declared-casting table, a
relationship job without a bounded route, a transform-bearing mask table under a post-scan gate), so the
route and the post-scan verdict are those of a run handed those tables resident. `False` disables that
pre-pass for every other caller. It is a no-op under `execution_mode="stream"`, an explicit route and a
tokenize job. The pre-pass is private: nothing is added to `decoy_engine.execution.pipeline` or the engine
root. `RouteSummary` gains `native_executed: bool | None`, true only when an executed column ran a compiled
backend from the operator registry. The no-sink lazy auto-chunk lane returns one resident output with either
keyword value.

**Pre-GA additive function with F24 84-E2 (2026-11-18).** `decoy_engine.generation.generated_lifecycle_terms(config, spec_widths)`
is a new name; it returns the resident bytes and the largest unique working set of a config's generate tables for
`estimate_job`. `estimate_job` now fills the three lifecycle fields of `JobEstimate` for generate-only and mixed jobs
too. `peak_bytes` of a generate-only job stays `None`, and a mixed job stays `priceable=False`; no existing value
changes. New note text: `lifecycle not priced: generated column {column} has no width source`.

**Pre-GA additive fields with SAMPLE SE2 (2026-11-12).** `ExecutionResult.sample_source_rows: dict[str, pa.Table] | None`
(default `None`, hidden from `repr`, not compared) holds the first kept source rows per table, only for a sample
request with `display_rows > 0`. `RunResult.sample: SampleMarker | None` (`rows`, `roots`, `head_tables`,
`scratch`) is optional under `decoy-run-result/v1`; it is left out of the written payload when unset, so an old
record reads unchanged. A sample stamps `quality_metrics["subset"]["sample"]`. `run_pipeline` is unchanged.

**Pre-GA correction (main health, 2026-10-31).** `RESERVED_NAMES` of the `when` closed grammar gains `type`
(additive to the list, breaking for a config whose `when:` references a column named `type`, which now fails
with `when_outside_closed_grammar`). It is listed on every interpreter rather than derived from
`keyword.softkwlist`, so a config validates identically on every supported Python. The list is frozen
on purpose; after GA any further addition is a breaking change.

**Pre-GA correction (FREEZE-S5, 2026-11-20).** `RESERVED_NAMES` of the `when` closed grammar gains nine
words: `null`, `true`, `false`, `between`, `like`, `ilike`, `matches`, `startswith` and `endswith`. They are held
back for later grammar (SQL-style literals and string operators), so adding one to the grammar is not a second
breaking change. A config whose `when:` reads a column with one of these names now fails with
`when_outside_closed_grammar`; rename the column. Reserved names match exactly and case-sensitively, so `NULL`
and `Between` stay columns. The set is pinned by an exact snapshot, and a sentry checks that every keyword
literal in `when_grammar.lark` is reserved.

**Pre-GA correction (SUB-STOP, 2026-11-05).** A config with `subset:` set is refused by every run entry
(`run_pipeline`, `run_pipeline_isolated`, `run_mask_pipeline_chunked`, `run_mask_chunked`, `run_native_or_oracle_chunked`) and by `compile_plan`,
with `config.subset_run_unsupported` (registry 1.6.0). Before, the block validated and the run silently masked
the full sources. `plan_subset` and `run_subset` are unchanged; run them first and point `sources:` at their output.

**Pre-GA correction (DATE-SHIFT-UNKEYED E1, 2026-11-16).** `keyed_columns_for_config` and
`secret_requirements_for_config` now walk a raw table dict's mask `columns` even when the same dict holds a
non-empty `generate_columns`. Before, such a dict was skipped, so a keyed column could hide. A validated config
(`PipelineConfig.model_validate(...).model_dump()`) cannot have both kinds, so its output and ordering do not
move. The unexported V1 class `transforms.date_shift.DateShiftStrategy` and its three private helpers are
deleted; `_detect_format` and `_COMMON_FORMATS` stay.

**Pre-GA correction (CONFIG-EXPLICIT E1, 2026-11-08).** A csv file source must set `has_header` (registry
1.15.0). An old config without it fails validation with `source_file.has_header_required` and the fix line
"add `has_header: true` if the first row names the columns (the old default)". `column_names_file`, a path to a
one-record header file for a headerless source, is additive. A csv dump now always carries `has_header`, so the
physical `plan_hash` moves for those configs while `pipeline_config_hash` does not. Three registered codes that
were reserved are now raised by `FileSource` (`source_file.no_header_columns`,
`source_file.column_names_with_header`, `source_file.csv_param_on_non_csv`), alongside the new
`source_file.has_header_required`, `source_file.column_names_source_conflict` and
`source_file.column_names_file_invalid`. No schema version moves. `resolve_csv_dialect` and the public
`build_profile_source` are now file-access entry points: with no installed policy under `forbid_ambient()`
either raises `FileAccessUnavailable` before dispatch, including an inline-only descriptor for the resolver and a
non-file descriptor for `build_profile_source`. Ambient callers are unchanged.

**Pre-GA correction (CONFIG-EXPLICIT E2c, 2026-11-10).** Every source column must be declared in the config,
supplied tables included. `profile_source`, `run_pipeline` and `run_pipeline_isolated` refuse an undeclared column
(`source_file.undeclared_column`) or a source with no `tables` entry (`source_file.undeclared_table`) from headers,
footers and schemas, before any data row is read, staged, profiled or masked. Fixtures that relied on the DE-03
`warn` pass-through now declare the column as `passthrough`; that path is reached only by drift after the gate.
`run_pipeline` gains `source_loader_schemas`, required with a `source_loader` and refused without one, and
`profile_source` gains the keyword-only `declared` capability. No registry code is added. The chunked entries are
unchanged here; CONFIG-EXPLICIT E2d gates them (below).

**Pre-GA correction (declared table never dropped, LOADER-SRC, 2026-11-14).** A table that masks through a
`source_loader`, has strategies under `tables:` and no `sources:` entry or caller table is refused by `run_pipeline`
before the loader runs or any data row is read, as `ConfigError` with reason `config/loader_table_undeclared` (new). After the plan is built,
`run_pipeline` also refuses any supplied mask table that has no plan entry, as
`ConfigError` with reason `config/declared_table_dropped` (new). Both texts name the table, never a value. Before
this, such a run returned the loader's rows unmasked. A declared table that nothing supplies is unchanged (no
output for it). Loader tables with a `sources:` entry are unchanged.

**Pre-GA addition (scan baseline and namespace placeholder, SCAN-MULTI S3-engine.1, 2026-11-14).** `PipelineConfig`
gains the optional `scan_baseline: {path: str, schema: "scan-set/v1"}` block, default absent and omitted from
`model_dump()` while unset, so a config without it hashes as before. The block is a pointer no run reads, so it
is left out of `pipeline_config_hash` (with `sources`, `targets`, `pre_scan` and `name`): two configs that differ
only in `scan_baseline` hash the same. (SCAN-CHECKS D-1 adds the optional `scan_baseline.on_drift: warn | fail`, omitted from the dump while unset. When set it is hashed, because it decides whether a run is refused; `path` and `schema` stay out. `decoy-run-result/v1` gains the check id `scan_drift`, and `build_run_result` takes `pre_checks`. Both are additive. A reader from before this change refuses the new config key, and a tolerant `RunResultRead` reads the new check id as a plain token. F15 E2 adds the optional `scan_baseline.sha256`, 64 lowercase hex characters in a strict string, omitted from the dump while unset; when `on_drift` is set it is hashed with `on_drift`. It also tightens the released `on_drift: fail`: the config is refused with `config/scan_baseline_unbound` unless `sha256` is set. This is a pre-GA tightening of a key released in 0.28.0, allowed while `is_pre_ga()` holds; a config with `on_drift: fail` and no digest must add one.) `ColumnConfig.namespace` and
`RelationshipConfig.namespace` refuse any value starting `decoy-set-namespace-` with `config/namespace_placeholder_unset`
(new, `since` unreleased), naming the field location and never the value. No shipped config used the prefix. No
artifact tag changes. `DEFAULT_STRATEGY_BY_DETECTOR` is now derived from `profiling.DETECTOR_DESCRIPTORS` with its
content unchanged.

**Pre-GA correction (one environment flag rule, DUP-FREEZE E3, 2026-11-14).** Every boolean engine flag reads
through one parser (`decoy_engine.internal.env_flags.env_flag`). The accepted set, after `strip().lower()`, is
`1`, `true`, `yes`, `on` (true) and `0`, `false`, `no`, `off` (false); an unset or empty variable gives the flag's
default. Any other spelling raises `decoy_engine.errors.EnvFlagError` (reason `config/env_flag_invalid`, new,
`since` unreleased), whose text names the variable and never the value. `DECOY_ML_DISABLED` accepted only `1`
before and now accepts the set; an unknown spelling there leaves ML off (`classifier_status()` reports
`disabled`), and from `ModelPackLoader.is_ml_disabled` it raises. An unknown `DECOY_PACK_REQUIRE_SIGNATURE` raises
`EnvFlagError` instead of `ModelPackLoadError`, so `load()` callers catch the new error; `load_with_fallback`
still returns `None` and its warning no longer holds the value. The engine-set `DECOY_INSIDE_ISOLATED_WORKER`
marker reads through the same parser, so any accepted true spelling counts as inside a worker. The custom
provider folder variable `CUSTOM_PROVIDERS_DIR` is renamed `DECOY_CUSTOM_PROVIDERS_DIR` with no alias.

**Pre-GA correction (retired route reasons removed, 2026-11-16).** The two route tombstones the v0.16.0 reframe left
behind, the requested-check reason and the resident-output reason that `route/post_scan_integrity_requested` and
`route/post_scan_integrity` replaced, are deleted from the registry. This is a one-time exception to the append-only
rule, approved by the owner before GA. `reasons.find("route", <code>)` returns `None` for both, and a persisted
record that names one reads as `decoy/unknown_reason_from_future` with the original identity kept in its context,
the same path a record from a newer engine takes. `REGISTRY_VERSION` stays `1.29.0`, so engine 0.27.0 and the first
release with this change report one registry version with two different catalogs. An engine branch forked before
this change sees the two identities as stale in its registry checks until it rebases.

**Pre-GA correction (connector usage outcomes, 2026-11-17).** Eight released connector codes change their `rules`
from outcome `execution`, retry `unknown` to outcome `configuration`, retry `configuration_repair`:
`connector/ambiguous_name`, `connector/column_not_found`, `connector/duplicate_relation`,
`connector/extension_unavailable`, `connector/extension_untrusted_location`, `connector/relation_not_found`,
`connector/unsupported_column_type` and `connector/value_not_representable`. Each names a problem the user fixes in
the config or the database, so the CLI now exits 1 for them instead of 3. This is a one-time exception to the
append-only rule, approved by the owner before GA. It is a closed list in
`scripts/release/reason_pre_ga_corrections.py`: the release pin check and the v1 append-only check accept only these
eight identities, only the `rules` field and only this exact old and new value. Any other change to a released reason
still fails. `REGISTRY_VERSION` stays `1.29.0`, so a consumer that keys on the outcome or retry of these codes sees
the new values under the same registry version until the next release recaptures the pin.

**Pre-GA corrections taken with the freeze batch (X0, 2026-11-18).** The engine gets a third tier. Section 3.4
froze `__init__.py` and `sdk.py` whole, so every name in `sdk.py` was frozen by default. Section 3.6 moves these names to
experimental: the connector SDK objects defined in `decoy_engine.sdk` and their root re-exports, the `CAP_*` flags and
`SDK_VERSION`, and the six provider registration functions. `ConfigError` and `ConnectorError` stay frozen because
`sdk` only re-exports them from `errors`. No name is removed or renamed. A public subpackage that section 3.4 does not
name now defaults to documented internal. Later slices of the batch add their own lines here: the `FormulaStrategy`
base removal, the public renames, and the `when:`, compressed-CSV and pass-through output changes.

**Pre-GA output change (date_shift `jitter_days`, DATE-SHIFT-JITTER, 2026-11-19).** `date_shift` now reads
`jitter_days: N` as the window `[-N, N]` on every route. Before, no route read the key and a config with it shifted by
up to a year. Output changes for every config that sets `jitter_days` (the shipped disguises when a client applies
them, the recommendations default, platform fixtures). `jitter_days` together with `min_days` or `max_days` is refused
at plan compile, as is a `jitter_days` that is not an integer, is negative or exceeds 106,751, and any `min_days` or
`max_days` that `int()` coerces to a value outside [-106751, 106751]. Configs that set `min_days` and `max_days` keep
today's `int()` coercion and window. One new plan reason, `date_shift_window_invalid`. Re-pin checklist: the platform
fixtures that use `jitter_days` (`fixtures/build_hr.py`, `build_logistics.py`, `build_retail.py`, `build_energy.py`,
`build_mortgage_credit.py`, `build_insurance.py`) and any stored date_shift output made from a `jitter_days` config.

**Public renames.** This table owns every public rename since the 0.16.0 taxonomy split, so a consumer
maps an old name to its new one in one place. Kinds are `symbol`, `wire key`, `config key`, `reason code`
and `CLI-visible label`. A `symbol` row names a dotted import path: the new path must import and the old
one must not (`tests/sentry/test_contract_renames_table.py` checks both). The CLI keeps its own retired
list and points here for the pairs. A rename takes a new row in the release that makes it.

<!-- renames-table:start -->
| old | new | kind | since | note |
|---|---|---|---|---|
| `decoy_engine.storm` | `decoy_engine.detection`, `decoy_engine.profiling`, `decoy_engine.verification` | symbol | 0.16.0 | Package split by concern. |
| `decoy_engine.run_storm` | `decoy_engine.profile_columns` | symbol | 0.16.0 | Top-level re-export. |
| `decoy_engine.StormProfile` | `decoy_engine.ColumnProfile` | symbol | 0.16.0 | Top-level re-export. |
| `decoy_engine.run_storm_post_mask` | `decoy_engine.verification.run_post_mask` | symbol | 0.16.0 | The new name is not a top-level re-export. |
| `decoy_engine.StormPostMaskReport` | `decoy_engine.verification.PostMaskReport` | symbol | 0.16.0 | The new name is not a top-level re-export. |
| `run_storm: true` | `post_scan: {integrity: true}` | config key | 0.17.0 | Pipeline config `version: 2` is required; `PipelineConfig.run_storm` became `PipelineConfig.post_scan`. |
| `quality_metrics["storm_postmask"]` | `quality_metrics["post_scan_integrity"]` | wire key | 0.17.0 | Run result key. |
| `storm-post-mask/v1` | `post-scan-integrity/v2` | wire key | 0.17.0 | Report schema tag. |
| `storm.scan` | `pre_scan.profile` | wire key | 0.17.0 | Profiler event name. |
| `route/run_storm_requested` | `route/post_scan_integrity_requested` | reason code | 0.17.0 | The old identity was a registry tombstone until 0.28.0 removed it. The new one was retired in 0.25.0 and points at `route/post_scan_models_resident`. |
| `route/run_storm` | `route/post_scan_integrity` | reason code | 0.17.0 | The old identity was a registry tombstone until 0.28.0 removed it. |
| `CUSTOM_PROVIDERS_DIR` | `DECOY_CUSTOM_PROVIDERS_DIR` | config key | 0.24.0 | Environment variable, no alias. |
<!-- renames-table:end -->

**Pre-GA refusal added (date_shift `date_format` required, DATE-SHIFT-FORMAT, 2026-11-20).** A `date_shift` column
without `provider_config.date_format` is refused at plan compile with `date_shift_requires_date_format`, in both
compile entry points and before any source read. The engine no longer detects the format from a sample. A top-level
column whose own declared parse format supplies the date shape is exempt; a nested child, including one under a
declared parent, is not. A runtime reason with the same code backs the handler. `starter_config` and the starter
review write the detected variant only when every detector hit parses with it (`format_pattern_complete`, new on
`DetectorMatch` and `FieldStats`, default false) and write `redact` otherwise; the starter now offers `date_shift` for
`eu_date` too, and iso_date and us_date starters lose the fixed `min_days`/`max_days`. Re-pin checklist: the platform
disguise application path (parked row PLATFORM-DISGUISE-DATE-FORMAT), `api/templates/upload_csv_mask_to_s3.yaml`, the
platform fixtures with `date_shift`, and any config built without `date_format`. The CLI follow-up
(DATE-SHIFT-FORMAT-CLI) must merge before the CLI re-pins to the release that carries this.

Until GA: use this document as the design target so we don't paint ourselves
into a corner. After GA: this document is binding.

## 2. The compatibility surface is wider than the API

The instinct is "don't break the public API." For Decoy the API is the
*smallest* part of the surface. We are a tool that produces **persisted
artifacts users keep** and that promises **stable output across runs.** Those
two facts, not the function signatures, are where breakage actually happens —
silently, with no error message, in a user's pipeline weeks later. Internalize
the four categories in §3.

## 3. The frozen surface

Within a major version, these MUST NOT change in a way that breaks an existing
user. Each entry names the concrete thing in the codebase.

### 3.1 Persisted artifact formats (the biggest surface)

The engine already version-tags every persisted artifact with a `name/vN` tag.
As of this writing the tags include:

```
distribution-snapshot/v1   decoy-vault/v3        vault-key/v2
decoy-vault/v2 (read-only) vault-key/v1 (read-only)
ff1-key/v1                 quality-report/v1     synth-report/v1
quality-diagnostic/v1      quality-fidelity/v1   quality-policy/v1
quality-shape-fidelity/v1  post-scan-integrity/v3    name-hints/v1
ssn/v1  npi/v1  pan/v1  iban/v1  ein/v1  mrn/v1  ndc/v1  icd10/v1
cusip/v1  address/v1  locality/v1  person/v1  provider/v1
composite/v1  custom/v1   ... (every disguise spec)
subset-manifest/v1         decoy-run-result/v1
decoy-record/v1            sample-run/v1         decoy-library-export/v1
decoy-job-log/v1
scan-set/v1             scan-diff/v1
decoy-resume-run/v1    decoy-resume-table/v1
lock/v1
decoy-publication/v1    immutable_directory/v1    publication/v1    checkpoint-index/v1
pinned-plan/v1          decoy-safe-output/v1      decoy-quarantine/v1
decoy-support-bundle/v1 decoy-reasons/v1          decoy-model-pack/v1
dps-marginal/v3         post-scan-integrity/v2    canary-span-report/v1
flagged-redacted/v1     phase-result/v1           phase-event/v1
decoy.evidence.statement/v1    decoy.trust-policy/v1    decoy.required-catalog/v1
policy-expansion/v1     secret-provider/v1
```

**Tags added with the freeze batch (FREEZE-S7a, 2026-11-20).** Every `"<name>/vN"` literal in engine source
is classified, and `tests/sentry/release/test_tag_census.py` fails on a new unclassified one. The 21 tags
after `lock/v1` above were persisted, exported or reserved with no listing before. Twelve are pinned without reading a
written artifact, and the map cites `S7A-KL-1` for them: `post-scan-integrity/v2` and `flagged-redacted/v1` are
older versions the engine still reads; `publication/v1` and `checkpoint-index/v1` are reserved record kinds with
no model yet; `decoy-support-bundle/v1`, `decoy-reasons/v1` and `canary-span-report/v1` name a typed constant
the engine does not write to a file itself; `dps-marginal/v3` has a writer that needs the `opendp` extra.
`phase-result/v1` and `phase-event/v1` are frozen because the wire form `to_wire` builds carries the tag; no
engine code calls `to_wire` yet, so they have no engine writer either. `policy-expansion/v1` is the `schema` field
`expansion_to_dict` writes (the policy fingerprint excludes it), and `secret-provider/v1` is the `api_version` field of
`SecretDescriptor.to_dict()`; both are pinned at the constant. The other nine are read from a written
artifact by a sentry. The census reads Python string and bytes constants under `src/decoy_engine`. Tags formatted in
the middle or built by concatenation, and tags in Rust or YAML files, are out of its scope (`S7A-KL-2`). A tag that never leaves the process is internal
(`decoy_engine.contracts.stability.INTERNAL_TAGS`, with a reason), and a
sentry scans written artifacts to confirm no internal tag appears in one. Derivation labels are in 3.3.

A user runs `decoy fit`, keeps the `distribution-snapshot/v1` artifact, and
feeds it to a later engine version. That artifact is a **contract**.

`decoy-run-result/v1` is the record of one `decoy run` (status, exit code, per-table outcome,
delivery, checks, timings). The engine owns its shape (`decoy_engine.RunResult` to write,
`decoy_engine.RunResultRead` to read). Every string in it is an enum or a validated name, reference,
hash, version or timestamp, never a data value or message. The write model refuses unknown fields and
enum values; the read model ignores added fields, reads a new enum value as a string and reads a
reason it cannot validate as `null`. Numbers and flags are strict on both sides (`False` is not a `0`,
`"1"` is not a `1`). The `schema` key is required on read and accepts only `decoy-run-result/v1`: a
document of another major (`/v2`), free text or a missing key is refused, never read as v1; a reader
that wants `/v2` support adds a new model under a new tag. Hand-authored corpus samples (the v1 sample and its cross-minor variants) are in
`tests/integration/compat_corpus/run_result/`.

`decoy-record/v1` is the envelope of the local record library (`decoy_engine.records`): kind, payload tag, a
content-addressed `id` (SHA-256 of the canonical envelope without `id`, parsed first so a reformatted file keeps
its id), `payload_digest`, closed `labels`, typed `links`, and the payload. The kind registry owns each kind's payload
tag and models: `evidence` (`cli-local-1`, the CLI manifest), `run-result` (`decoy-run-result/v1`), `sample-run/v1`
(metadata of a `--sample N` run, never rows), `scan-set` (`scan-set/v1`, the engine's `ScanSetCore`), and the reserved `publication` and `checkpoint`. A record of a
kind or payload major this version does not know is returned as an opaque record or as `RecordNeedsNewer`, never
dropped. A default export withholds such a record (its id goes in the header's `withheld_records`); `keep_paths` copies it byte for byte. `decoy-library-export/v1` is the JSON Lines export format and
`decoy-job-log/v1` the header of a job log. Corpus samples are in `tests/integration/compat_corpus/records/`.

The `evidence` payload (`cli-local-1`) also accepts two additive optional keys, `path_base` (exactly `"project"`) and `project_manifest` (path text or `null`), both present or both absent (a reader refuses a half pair, a null `path_base` and any base other than `"project"`), as a run inside a `decoy.yaml` project writes them. Absent keys stay absent, and `project_manifest` is a path field in a default export.

It also accepts the optional `scan_baseline` member (SCAN-CHECKS B-1): the `FileFingerprint` shape (`path`, `fingerprint`, `fingerprint_method`, `size_bytes`) of the baseline file the pipeline points at, hashed at run start. The fingerprint is `null` when the file could not be read. Absent stays absent, so the `manifest_hash` of a run with no baseline is unchanged, and `scan_baseline/path` is a path field in a default export. A reader from before this change refuses the key, so the CLI writes it only after it pins the engine that reads it. The sample is `evidence-cli-local-1-baseline.json`.

`lock/v1` is the runtime lock (`decoy_engine.support`: `RuntimeLock`, `build_runtime_lock`, `read_runtime_lock`, `compare_runtime_locks`, `run_probe`, `probe_status`, `ProbeError`). It is canonical JSON of the library versions and two content digests that masked or generated bytes depend on, plus an optional `key_version` label. The reader refuses an unknown tag, a missing tag, a missing field except `probe`, which is optional and omitted when absent, and a wrong type (`True` is not a count, `"7"` is not a `7`, and a `null` `probe` is a wrong type), and ignores a field added under `lock/v1`. `probe` is the digest of the synthetic probe (`run_probe`), a SHA-256 over fixed public inputs run through the engine's Python reference code for the keyed hash, FF1 and the seeded Faker pool; `probe_status` reports `ok` only when it equals the golden for the current seed protocol. It never holds a key, a key check value, a path or a host name. The `key_version` label rule is `decoy_engine.keyprovider.is_key_version_label`, shared with the resume store records. A hand-written sample is in `tests/integration/compat_corpus/runtime-lock/`.

`decoy-resume-run/v1` and `decoy-resume-table/v1` are the two files of a `decoy run --resume` store (`decoy_engine.delivery.resume`: `RunRecord`, `TableRecord` and their encoders and decoders). They are canonical JSON read by the strict reader the publication descriptor uses: exact tag, every field, no unknown field, exact types (`False` is not a `0`, `"1"` is not a `1`), `re.fullmatch` shapes (a digest with a trailing newline is refused), relative artifact paths and at most 64 KiB. There is no tolerant read model: a change to either shape takes a new tag. They hold digests, counts, tokens and one path relative to the run folder (the artifact's placed path, the same trust class as the F22 descriptor the delivered run folder carries), never a cell value or a key. `RUN.json` is capped at 64 KiB, which holds about 337 tables; `encode_run_record` refuses a longer table list with `oversize` and the caller treats that run as not resumable. Corpus samples are in `tests/integration/compat_corpus/resume/`.

`scan-set/v1` is the record of one multi-table scan (`decoy_engine.scan_sources`). The engine
owns its shape: `ScanSetCore` to write (unknown fields refused), `ScanSetCoreRead` to read
(unknown fields ignored). It holds counts, rates, type labels, detector ids and column names.
Column names are the producer's header text; when the scan's header guard flags a header row
that looks like data, the core writes positional names (`c0..cn`) instead. A misdeclared header
the guard cannot recognise (a person name, an address) is the stated limit. The core holds no
cell value. The optional per-table `detail` member of `scan_set_to_dict(detail=True)` is
the nested `ColumnProfile` dict; it is additive-only within `/v1` and is not part of the core.

Each table also holds `basis` (`whole` or `sample`) and `scanned_rows`, the rows the field counts were taken over
(PROFILE-SAMPLE-LABELS). `whole` is written only when the read reached the end of the source, with an exact
`row_count` equal to `scanned_rows`; `key_candidates` is empty on a `sample`: the write model refuses a sampled key claim, and a read of a core with no `basis`, or any `basis` other than `whole`, drops its stored `key_candidates` (they were never proven by a whole read), so that core stays readable and exportable. Both members have read defaults, so a
core written before them reads as `basis: "sample"` with `scanned_rows: null`. A `basis` string outside the two
values reads as `sample`, so a third value added in a later minor is not a break. `basis`, `scanned_rows` and the two
values are frozen at 1.0. A plain profile document (`profile_document`, `ColumnProfile`) gains the same `basis` key, the
last field of the dataclass.

`scan-diff/v1` is the record of the drift between two scans (`decoy_engine.profiling`: `diff_scan_documents`,
`diff_scan_tables`, `scan_diff_to_dict`). The engine owns its shape: `ScanDiffCore` to write (unknown fields
refused, scores floats (bool and strings refused), drift kinds a closed set) and `ScanDiffCoreRead` to read (unknown fields ignored). The
tag is required. A reader accepts `scan-diff/v1` and `scan-diff/v1.<minor>`, and refuses another major, a missing tag
and text after the tag. A drift kind this reader does not know (one a later minor could add) is refused with fixed
text, because a reader that dropped a kind could report a drifted diff as clean. The five kinds are, per table,
`pii_bucket_increased`, `new_high_pii_field` and `new_quasi_identifier_group`, and, per set, `table_added` and
`no_common_table` (the one kind when no table matches). `tables_removed` and `detectors_changed` are information, not
drift. The document holds table and column names, PII scores, buckets, quasi-identifier group names, a risk delta and
engine versions, never a cell value. `read_scan_set` is the one reader of a parsed `scan-set/v1` or
`scan-set/v1.<minor>` document, with the same tag rule. The new names are exported from `decoy_engine.profiling`, not
from `decoy_engine`. Corpus samples and the parity pairs are in `tests/integration/compat_corpus/scan_diff/`.

A bound baseline (F15 E2) is the scan file whose bytes `scan_baseline.sha256` pins. `decoy_engine.profiling.bind_baseline` hashes the bytes, checks the digest, and only then parses them with the engine's strict JSON parser, which now also refuses a number that overflows to infinity (`1e999`). `BaselineBindingError` carries `pre_scan.baseline_digest_mismatch` or `pre_scan.baseline_unreadable` (both since UNRELEASED) with fixed text. `evaluate_drift` returns a `DriftVerdict` (`passed`, `warned` with `pre_scan.drift_detected`, or with `pre_scan.baseline_unbound` when the baseline has no digest and nothing drifted, or `failed` with `pre_scan.drift_blocked`) holding table and column names and drift kinds only. The new names are exported from `decoy_engine.profiling`, not from `decoy_engine`.

`post-scan-integrity/v3` is the report `run_post_mask` returns (v2 until SCAN-ML M4). Recorded pre-GA semantic change
(POSTSCAN-STREAM PS-4, 2026-11-12): each `fk_preservation[]` finding now derives from the declared
keys' facts (`verification/postmask/_keys.py`) and no longer from an orphan rate. `fail` means the run
introduced orphans; `warning` means every output orphan was already in the source, or the key carries
only warning codes; `info` means no orphan; `error` means the key has no counts (an output that could
not be read, a missing key column, or a type mismatch over a comparable source). The 0.1% and 1%
thresholds and the `orphan_policy: skip` demotion are gone, so one introduced orphan in a large table
now fails where it used to be `info`. A child tuple declared under several parents is counted once
against their union, and each of its edges carries those counts. `parent_column` and `child_column`
join composite names with `", "` (was `","`). Added optional fields: `parent_columns` and
`child_columns` (the names one by one) and `source_orphan_count` (`null` when the source side was not
counted). `run_post_mask` gains the keyword-only `keys=` (the facts a caller has already counted);
without it the facts are counted from the two frame dicts, over the edges whose tables are all in the
output frames, as before. A relationship entry that does not parse now raises
`PipelineConfigError` (names only). An `error` finding carries `total_child_rows` 0, because a key
with no counts has none. With `keys=`, an edge that has no key fact reads `error`. Without `keys=`, a
float key column, NumPy or nullable `Float32`/`Float64`, with at least one non-null value, all of
them whole numbers in the Int64 range (pandas' reading of a nullable integer column, such as a
self-reference with a null root), is compared as an integer key. A column with any fraction stays
float, so against an integer parent it is still a type mismatch, and so is an all-null float column.
The tag stayed `v2` for that change; the field set only grew.

Row 31 DI-1b (2026-11) adds the optional `post_scan.distribution: {tolerance_pct, on_breach}` config block and an
optional `distribution` section of the v3 report. `tolerance_pct` is a strict number above 0 and at most 100;
`on_breach` is `warn` only until the blocking slice, so `fail` is refused at config load. The block needs
`post_scan.integrity: true` (`config.post_scan_inactive` otherwise), is left out of the dump while absent (a
config without it keeps its dump and plan hash) and is hashed when set. The report section holds one row per
checked fact (`table`, `column`, `fact`, `severity`, `delta_pct`), no source or output value, and is absent
unless the block is set, so a v3 reader that ignores unknown keys is unaffected. A run that asks for the check
and has a transform-bearing mask table, a source loader or a deferred gate is refused (a transform-bearing table
with the existing `runtime/post_scan_integrity_transforms_unsupported`, the others with
`runtime/post_scan_distribution_deferred`, new, `since` unreleased). Registry version and manifests are
untouched until the release.

`distribution-snapshot/v2` (row 24f, 2026-11) is what a plain `decoy fit` now writes
(`distribution_snapshot_document` with `include_values=False`). Only the datetime bucket changed:
`year_bins` entries are `{start_year, end_year, count}` year ranges instead of `{year, count}`.
The raw writer, the DP writer and `include_values=True` keep writing v1, and every reader accepts
both (`decoy_engine.contracts.distribution_snapshot`). The same change moves `value_policy` to
`flagged-redacted/v2` (every unflagged column is also floored at 10 rows); readers accept v1 and
v2. A column too small to floor is listed in `redacted_columns`. Generated output of a statistical
column fitted from a new plain snapshot changes; an old snapshot generates as before. A plain v2
numeric histogram accompanying `exact_values` or `quantile_grid` has one bin; without either block,
it has equal-width bins on a 1, 2, 5 grid and every non-empty bin is held by at least 10 rows.
`numeric_bins` outside 1 to 1000 is refused with `quality.numeric_bins_invalid` (new, `since`
unreleased) by every snapshot writer, and above 1000 by the DP fit (`dp_numeric_bins_invalid`).

`post-scan-integrity/v3` (the dict `run_post_mask` returns and `run_pipeline` stamps under
`quality_metrics["post_scan_integrity"]`) is minted by SCAN-ML M4 (2026-11), because
`match_rate` can now be `null`. The engine's verdict reader accepts v2 and v3. Each `residual_pii` finding gains `source` (`regex`, `classifier` or `ner`), `model`
(`{id, version}` of the model that ran, or `null`), `band` (the classifier's `high` or `review`),
`dismissed` and `promoted`; `match_rate` is `null` for a classifier finding, so a reader that
assumes a number needs a `null` case. The report gains `detectors` (the runs), `tables_checked`
(every output table the residual-PII check read, clean tables included) and `config_sha256`
(`null` unless the caller sets it). A regex-only report keeps every earlier key and value.

**Rules:**

- **Never mutate the shape under an existing tag.** If you change what
  `distribution-snapshot/v1` contains, you have broken every holder of one.
  Changing the shape means minting `distribution-snapshot/v2` and **keeping a v1
  reader.**
- **New code reads all historical versions.** The current engine must load every
  `/vN` it has ever written. Readers are append-only; you add `v2` support, you
  never remove `v1` support within a major.
- **New fields are additive and optional.** Adding an optional field to a `/v1`
  artifact that old readers safely ignore is allowed and preferred over a version
  bump. Removing or repurposing a field is not.

**New in Sprint G (2026-07-03):** FK-aware subsetting (`decoy_engine.subset`)
writes a `subset-manifest.json` evidence artifact tagged `subset-manifest/v1`
(`SubsetManifest.manifest_version`). It is counts-and-identifiers-only by
design (no raw key value, no raw filter-predicate literal), so it carries no
re-identification risk on its own, but the same rule applies: a shape change
mints `subset-manifest/v2` with a kept `v1` reader. The `subset:` block added
to `PipelineConfig` (`config/_subset.py`) is additive and optional (`None` by
default, unchanged behavior for every existing config, except that a run now
refuses a config that sets it: see the SUB-STOP correction in section 1); it is part of the
§3.4 config contract like any other optional block.

### 3.2 The vault (the catastrophic one)

`decoy-vault/v3` + `vault-key/v2` back re-identification. If a new engine version
cannot read an old vault, **users can never unmask the data they already
masked.** That is unrecoverable, not inconvenient.

**Rule:** the vault format is **forever-readable.** Treat it as the most
conservative format we own. A vault change is a new version *plus* a permanent
reader for every prior version, reviewed by the PO. There is no "pre-GA hard
delete" exception for the vault once a real vault exists in the wild.

**Pre-GA hard cutover to v2 (F13, 2026-06-26):** `decoy-vault/v1` was replaced
by `decoy-vault/v2` without a v1 reader. This is legal only pre-GA because no
vaults exist in the wild. The forever-readable rule begins at the first
in-the-wild `decoy-vault/v2` vault. From that point forward, a v2 reader is
permanent and any future format bump must add a v3 alongside a kept v2 reader.

**Format bump to v3 (F09 E1, owner decision 2026-11-18):** `decoy-vault/v3` (libsodium
secretstream, an authenticated header, a key per vault) is the current format and the only one
written. `decoy-vault/v2` (Fernet) stays readable: `vault/_v2_reader.py` is the read-only v2 reader,
and `_reader._open_vault` picks v2 or v3 from the magic bytes in one place. A v2 file opens under the
same 32-byte key as before and is never adopted as a current vault (`reads_as_own` needs v3).
`decoy-vault/v1` was already unsupported before this change and stays refused with
`vault_format_unsupported`. Both the v2 reader and the v3 reader, `vault-key/v1` and `vault-key/v2`
(`VAULT_KEY_LABEL`) and the key check label are permanent. `VaultWriter` takes a `PreparedSecret` or
the older `bytes` mask key (`VaultWriter(prepared_or_key, *, file_access=None)`); both go through the
same preparation and give the same vault, and the writer owns a clone, closed by `close()`. The
`bytes` form is the frozen call and has no removal date.

**GA-flip checklist (vault key floor, 2026-11-12).** The section 3.2 forever-readable freeze takes effect
at the first real `decoy-vault/v2` vault, which after slice 0 can only be written under a secret of at
least 32 bytes; from that vault on, the v2 reader (kept, read-only, beside v3), the 32-byte vault key
length and the vault key labels are permanent, per this contract's own pre-GA to GA rule (section 1).
Before that vault, narrowing which keys open a v2 file was a legal pre-GA hard delete: seed-keyed
vaults stopped opening and are regenerated.

### 3.3 The determinism guarantee (the silent one)

Backed by `docs/determinism.md`, the `determinism/` module, the
`validation/post/_checks/_determinism_sample.py` post-check, and the golden
tests that compare against stored values: `tests/unit/determinism/test_derive_vectors.py`
(reference vectors), `tests/unit/determinism/test_hkdf.py` and
`tests/native/test_draw_site_providers_golden.py` (a digest per draw-site provider), with
`tests/native/test_determinism_goldens.py` checking that the shipped engine's output equals those
providers for the sites it lists (byte for byte, `mask.fpe` by key). The other provider sites are
pinned only at the provider (`RGF1-KL-2` in `docs/frozen-surfaces.md`).
`tests/integration/golden/test_determinism_invariants.py` compares two runs of the current code,
so it catches nondeterminism, not a changed derivation. The files under
`tests/snapshots/golden/mask_faker_seeded`, `mask_hash_only` and `mask_redact`
have had no reader since the V1 graph runner was deleted; they are known limit
`RGF1-KL-2` in [frozen surfaces](frozen-surfaces.md).

The guarantee: **same input + same seed → byte-identical output, forever, within
a major version.** A user who masked with one version and later masks *new rows*
with an update — expecting the new rows to join to the old output (same person →
same pseudonym across runs) — is relying on the derivation (HKDF-SHA256 `derive`,
FPE keying, Faker seeding) being unchanged. Change any of it and you break that
user with **zero error message.**

**Derivation labels (frozen).** These tags are inputs to a key, a digest or a seed. Changing one changes
output bytes or a stored digest with no error, so each is part of the determinism guarantee. The list equals
`decoy_engine.contracts.stability.DERIVATION_LABELS`, and the census fails on a tag that is neither here, in
3.1, a declared-casting format id (`docs/declared-casting.md`) nor internal. The 14 declared-casting ids (`string/exact/v1`,
`cobol/comp-3/v1` and the rest) name a format a user declares in config; the F06 parse and output registries own them.
`fpe-key/v1` is a retired label: no live code uses it, and `ff1-key/v1` replaced it. `ff1-key/v1` is the one label that 3.1 lists too. `vault-key/v1` (`derive(mask_key, "vault", b"vault-key/v1")`) is also a derivation label, but only 3.1 lists it, so it is not in the block below.

```
canary-span-normalization/v1           decoy-engine/determinism/v1   decoy-engine/keyprovider/v1
decoy-engine/protected-quarantine/v1   decoy-probe/v1                decoy-publication-result/v1
decoy-resume-key-check/v1              decoy-vault-key-check/v1      decoy.shuffle.unkeyed/v1
fpe-sub-floor-synthetic/v1             joint_mask/gen_row/v1         joint_mask/keyed_row/v1
joint_mask/null_fallback/v1            name_email/v1                 pinned-plan-fingerprint/v1
tokenize-key/v1
```

**Rules:**

- The derivation path (`determinism/_derive.py` and everything it feeds) is
  frozen within a major. A change to it is a major bump, even if the public API
  is untouched.
- **A changed golden baseline in CI is the alarm.** If a committed snapshot
  changes, that is the signal that you altered a user-visible guarantee. Golden
  baselines are updated only as a conscious, reviewed, version-gated act — never
  as a "tests went red so I refreshed them" reflex.

**Current state (v6, 2026-06-26, pre-GA):** `SEED_PROTOCOL_VERSION` was bumped
from 5 to 6 by the F2/F3 generation-determinism rewrite (see `CHANGELOG.md`
and `docs/determinism.md`). Both masked output and synthetic-generation output
shifted. This is a pre-GA hard cutover; no vaults exist in the wild. The
`SEED_PROTOCOL_VERSION` byte is now mixed into the generation-path HMAC as well
as the mask-path envelope, so it is the single compatibility knob across both
roots. CONSEQUENCE for future maintainers: any future `SEED_PROTOCOL_VERSION`
bump re-keys synthetic-generation output too, even a bump made for a mask-only
reason. There is no longer a "mask-only" envelope change; budget for the
generation shift (and a corpus re-baseline) whenever you bump the version.
A v5 vault over a synthetic column cannot be unmasked under v6.

The vault format is now `decoy-vault/v3` (F09 E1); `decoy-vault/v2` (F13, 2026-06-26) is read-only
and kept. Both files stamp `SEED_PROTOCOL_VERSION` in an unencrypted header; `load_vault` reads that
header before any decryption attempt and raises a typed
`VaultError(code="vault_protocol_version_mismatch")` on a mismatch, distinct
from the wrong-seed `vault_key_mismatch`. Cross-version unmask is not supported
and was not supported before F13; F13 makes the failure diagnosable rather than
opaque.

**Current state (column identity, 2026-11-08, pre-GA):** the generation column
root now mixes the column identity (table name plus column name) with the
strategy fingerprint, so columns with identical settings no longer draw the same
stream. Generated output changes for existing seeds and keys, and renaming a
column or moving it to another table re-keys it. `SEED_PROTOCOL_VERSION` is
deliberately NOT bumped: it is shared with the mask path, so a bump would re-key
all mask output for a generation-only change. Pinned plans are guarded by the
engine version recorded in their `runtime_identity`, so a plan pinned before this
change is refused by a newer engine instead of silently regenerating differently.

**Current state (v7, 2026-09-11, pre-GA):** `SEED_PROTOCOL_VERSION` moved 6 -> 7
(Task 5.2, DE-01 resolution): the `fpe` strategy's cipher, and the `fpe`-branch
text-mask spans (ZIP, SSN, phone, PAN), now run NIST SP 800-38G FF1 (AES-256)
in place of the retired 8-round HMAC-SHA256 Feistel construction. Every
deterministic mask output changes, not only `fpe` columns (same
cross-both-roots consequence the v6 paragraph above describes). The fpe key
derivation label moves from the retired `fpe-key/v1` to `ff1-key/v1` (domain
separation: no v6 key material is ever reachable under FF1). The fpe tweak is
no longer a raw UTF-8 identity string; it is the framed wire format
`build_ff1_tweak` builds (version byte, scope byte, big-endian UTF-8-length
field, identity bytes -- see `docs/native/crypto-testing-reference.md` §3.2).
See `docs/security/de-01-ff1-adoption.md` for the conformance claim, key/tweak
model, and documented leakage; `docs/quality/mutation-ledgers/transforms_ff1.md`
for the crypto-crown-jewel mutation ledger (superseding the retired
Feistel-era `transforms_fpe.md` ledger for `_ff1.py` itself -- `fpe.py`'s own
wrapper-layer ledger is unaffected). A v6 vault cannot be unmasked under v7.

**Known nuance: text normalization differs by compute path.** The determinism
guarantee holds within one hash compute path, not necessarily across the two.
Before hashing, text is normalized with Unicode NFC, and the compiled companion
(Rust) and the pure-Python path can carry different Unicode versions. For a value
containing a codepoint assigned in a Unicode version newer than the running
Python's, the companion and Python paths can derive different tokens; the
companion result is canonical. This is not a derivation change (`derive` is
unchanged), so it is not gated by `SEED_PROTOCOL_VERSION`. Plain text is
identical on both paths. The practical risk is join-stability for such rare text
when a dataset is masked partly on a Python-only install and partly on a
companion-present one. Documented for users in `docs/strategies.md` (hash). To
remove the difference entirely, align the Python runtime's Unicode version with
the companion's.

The sequential FK route (`run_sequential`, the bounded route for relationship-bearing
jobs) also hashes on the companion when it is installed. It uses the same kernel as the
out-of-core route, so the same admitted input types (utf8, large_utf8, bool, the eight
integer widths) and the same accepted Unicode nuance above apply, and its output is
byte-identical to the Python handler otherwise. Admission sits behind the existing ingest
guards. An integer hash column that holds a null is hashed, not rejected: the token is
keyed on the exact integer, so it matches the other routes. `null_bearing_int_unsupported`
covers only `truncate` and `categorical`, for parent and payload columns alike. A matched child key
inherits the parent's masked value and never calls the kernel. REMAP orphan keys are minted
on the companion only when their parent column ran there, and each orphan batch is admitted
on its own inferred type; so the parent-key derivation is single-normalization, while a native
parent's orphan batches may split between the companion and Python (a batch that declines).
Any such split stays within the accepted native-vs-Python NFC envelope and ties no referential
constraint, because orphan keys reference no parent row. `ExecutionResult.quality_metrics["sequential_hash"]` records the backend
per column (`scalar`, `fk_inherited`, `fk_remap`) with a value-free reason when a column
stays on Python. A native fault aborts the run and is never retried in Python; whether the
tables written before it are discarded depends on the sink (a `TransactionalSink` aborts, a
plain callable has already delivered them).

The same native `hash` covers every acyclic FK shape the sequential route masks: a multi-hop
chain, a diamond, a child with several parents (first declared parent wins, unchanged),
self-reference, a junction table, and a single-level composite key. A column that is both a
child and a parent key (a chain's middle column) mints its orphan keys on the companion
without ever being rehashed as a matched child. A composite tuple is built from per-member
tokens, so a tuple that mixes an admitted member with a declined one (for example a string and a
decimal) is native for the first and Python for the second. A composite foreign key that is
itself the parent of another composite key (`A.(k,j) -> B.(k,j) -> C.(k,j)`) runs as before
while every key matches: the tuples inherit the root's tokens. A genuine orphan on a `remap`
hop of that chain cannot be re-derived on the sequential route, so the run stops with
`sequential_composite_fk_chain_unsupported`, naming the tables and columns involved (the
full-frame route keeps its raw `orphan_remap_parent_missing`). The other orphan policies still
run on that shape. Follow-up: REMAP through a composite chain needs a scalar seed per composite
member, which the planner does not compile today.

`fpe` payload columns also mask on the companion on the sequential route (slice D2c-i),
through the same compiled kernel the chunked route uses. Unlike hash, FPE does NOT NFC-normalize,
so its native output is byte-identical to the Python handler with NO Unicode-version nuance. Only
a NON-KEY `fpe` column runs native: an `fpe` column used as an FK key stays on Python (the native
REMAP minting path is hash-only). Admission is configuration-aware: a column with any checksum
scheme, a non-`string` Arrow type, no namespace, or an invalid charset declines to the Python
handler (which raises its usual coded error). A per-cell encryption error (out-of-charset,
sub-minimum domain, over-length) fails the run closed before any value is written, so the table
publishes nothing. The handler's residual-risk warnings (`fpe_partial_plaintext_disclosure`,
`fpe_join_group_active`) still emit on the native path. `quality_metrics["sequential_fpe"]`
records the backend per column.

The same nuance applies to deterministic `categorical`: the keyed index derives
from the NFC-normalized source value, so for source text with a codepoint newer
than the running Python's Unicode version the companion and Python paths can
select different categories. The companion selection is canonical, and the
out-of-core route only runs a string source on the companion (documented in
`docs/strategies.md`, categorical). Deterministic `categorical` payload columns also
mask on the companion on the sequential route (slice D2c-ii), through the same
compiled index kernel, carrying this same accepted NFC nuance; a non-deterministic /
non-namespaced / non-string-category / non-string-source / unbuildable-CDF column
declines to Python, and a `categorical` FK key column stays Python (native REMAP is
hash-only). `quality_metrics["sequential_categorical"]` records the backend per column;
the SEQ disposition is now emitted per registered strategy (`sequential_hash`,
`sequential_fpe`, `sequential_categorical`), each key present only when that strategy
recorded a decision.

`bucket_perturb` carries the same nuance: the within-bucket offset derives from the
NFC-normalized source value, so a `date_format` with a Unicode literal suffix holding
a codepoint newer than the running Python's Unicode version can land on a different
day on the companion and Python paths. The companion result is canonical; ASCII
formats are identical (documented in `docs/strategies.md`, bucket_perturb).

`derived` and `formula` share one missing-cell rule with the masking kernels from 2026-11-13
(slice DUP-DEFECTS E2): `None`, any float NaN, `Decimal('NaN')`, `pd.NaT`,
`numpy.datetime64('NaT')` and `pd.NA` are missing; the strings `"NaT"` and `""` are values.
Before, `derived` and `formula` counted only `None` and float NaN. This is a one-time pre-GA
output break, with no shim and no flag, for rows where a referenced cell is `Decimal('NaN')`, a
numpy NaN scalar, `NaT` or `pd.NA`: `explicit_null` now writes a null where the expression ran,
`sentinel` and `default` now substitute for those cells (an expression that then fails the row
reports today's value-free text), and `formula` substitutes `""` and counts it. Rows without such
a reference are unchanged. No `SEED_PROTOCOL_VERSION` change.

**2026-11-13, generate `formula` types (pre-GA break).** A generate `formula` now reads a plain
`sequence`, an int or float `faker` column and a numeric column with nulls as numbers, not as
cell text. Output of such a formula changes (`a * 2` was `'11'`, is `2`), a column a formula
reads keeps its numeric Parquet type unless another reader needs it as text, and a formula that
raises fails the run instead of writing nulls. No seed draw moves and
`SEED_PROTOCOL_VERSION` is unchanged. The refreeze is recorded in
`docs/records/2026-11-13-formula-typing-refreeze.md`.

**2026-11-14, joint_mask streams and composite seeds (pre-GA break).** The null-key fallback of
`joint_mask` mask mode and all of `joint_mask` gen mode seed NumPy from
`derive(job_seed, namespace, source)` instead of `job_seed[:8]`. The source is
`b"joint_mask/null_fallback/v1"` or `b"joint_mask/gen_row/v1"` followed by the length-prefixed
(4-byte big-endian) UTF-8 table name, reference name and `\x1f`-joined target columns, so every
group draws its own stream. Output changes for rows with a null `key_by` and for every row of a
`mode: gen` group; rows with a non-null `key_by` use the unchanged keyed path and are byte-identical.
No shim and no flag. Composite specs resolve one seed per call (`resolve_seed`): a seeded spec,
including the deterministic composite masking path with a 32-byte mask key, produces the same bytes
as before, and only a spec with no seed changes (a fresh 8-byte seed per call, not an all-zero pool
seed). `SEED_PROTOCOL_VERSION` is unchanged: no `derive` input layout changes, only two new sources.

**2026-11-14, one canonical JSON owner (pre-GA break, DUP-FREEZE E1).** Every digest, HMAC
and signature input is now built by `decoy_engine._canonical.canonical_json_bytes`: sorted keys,
compact separators, UTF-8 text written as-is, NaN and Infinity refused. The same module holds the
one frozen seed form, `seed_fingerprint_bytes` (ASCII-escaped, byte-for-byte what
`strategy_config_fingerprint` and the pool `_config_hash` wrote before), named under
`SEED_PROTOCOL_VERSION`. No seed, pool seed or generated output byte moves, and no record id or
CLI evidence manifest hash moves. Some digests change, with no shim and no flag.
`pipeline_config_hash`, the phase envelope bytes, the pinned-plan fingerprint, the model pack
signature scope, `profile_hash`, `PinnedEnvelope.digest` and the reproducibility `config_digest`
move only for content that holds non-ASCII text; the physical-input identity hash and the subset
request fingerprint move for every config (they used default separators and a `repr` fallback).
A pinned plan or resume fingerprint made before the change
refuses with its existing digest-mismatch code and is rebuilt; a model pack signed over non-ASCII
manifest text is signed again. Plan compile now refuses a config value JSON cannot hold (a YAML date or
timestamp, NaN, Infinity, an unpaired surrogate) with the new reason `config_value_not_json`;
quote the value as a string. `subset_request_from_config` refuses NaN, Infinity and an unpaired
surrogate the same way, but keeps a naive timestamp literal, the only spelling a zone-less
timestamp column accepts; the subset request fingerprint writes it as tagged wall-clock text.
No `SEED_PROTOCOL_VERSION` change.

**2026-11-20, windowed_date offsets (pre-GA break, GEN-WINDOWED-BATCH S2).** `windowed_date`
(mask and generate) now draws each row's offset from the pool sampler's index kernel, in one
batch per column, instead of a numpy Generator built per row. For global row number `r` and draw
`k` the offset is `min_days + derive_index(seed, namespace, int(2 * r + k), pool_size=span + 1)`
(`k = 0`; `early` and `late` also draw `k = 1` and take the minimum or maximum). The draw index
is part of the source, not of the namespace. Output dates change for the same seed, with no shim
and no flag: regenerate stored `windowed_date` output. The window, null-anchor, determinism and
partitioning guarantees are unchanged. Global row numbers must now be below `2**62` (before:
below `2**64`); a negative or larger row number raises `windowed_date_row_number_out_of_range`.
A window with `min_days` or `max_days` more than 106751 days from zero is refused at parse with
`plan/windowed_date_window_out_of_range`.
The draw-site entry `mask.windowed_date` moves to family `source_keyed_hmac` and the catalogue
pin, provider golden and `TestWindowedDateGolden` are re-baselined with it.
`SEED_PROTOCOL_VERSION` stays 7: a bump would rekey every keyed column and vault before GA, and
only this site moves.

### 3.4 The public API + CLI contract

- **Pass-through text width (O3, 2026-11):** a pass-through `large_string` column leaves
  every route as `string` when its values fit 32-bit offsets, and as `large_string` after
  one value that cannot fit. Each batch is judged from its own offsets: a batch whose total
  exceeds the limit is split at row boundaries into ordered `string` slices, and the source is
  never pre-scanned or reread. A value that cannot fit promotes the column to `large_string`
  for the rest of the run: the CSV writer keeps the value, and resident output widens its
  earlier chunks. Parquet cannot store a single value of 2 GiB or more on any head: the raw
  writer raises `ArrowInvalid` and a strict file-access write raises `FileAccessReadError`.
  O3 adds no earlier refusal. A sink whose writer schema is fixed before the first batch
  keeps the source `large_string` field, byte-equal to the previous release: the out-of-core
  sink, the streamed `run_auto_chunk` sink and any `TransactionalSink`. Narrowing to `string`
  applies to resident results, `run_mask_chunked` chunks, whole-frame output and CSV.

- **Python:** the symbols re-exported from `decoy_engine/__init__.py` and
  `decoy_engine/sdk.py`, with their signatures and output-affecting defaults.
  As of 2026-09-30 this includes `read_fixed_width` and `FixedWidthParseError`
  (A5a): a `format: fixed_width` `FileSource` can now be read through these
  names instead of importing the private `decoy_engine.profile._fixed_width_reader`
  module, which the additive-only rule in §4.1 does not cover.
  It also includes `generated_table_to_pandas(table)` (2026-11-08): converts a generated
  Arrow table to pandas with every integer column kept as a pandas nullable integer, so
  output written from the frame prints `1` where a plain `to_pandas()` prints `1.0`.
  It also includes `decoy_engine.profile.profile_relationships_from_config(relationships_config)
  -> tuple[Relationship, ...]` (CLI-QA1 E-b): the config's `relationships:` block as one
  `Relationship` per child, skipping malformed entries; an unequal composite column count, or an
  empty column list, raises `ValueError`. It replaces the private `_build_relationships`.
  It also includes `read_csv_source`, `CsvDialect` and `resolve_csv_dialect`
  (CLI-DIALECT, 2026-11): `read_csv_source(path, dialect, *, table, dtype=None,
  nrows=None, chunksize=None, file_access=None, field_key="source.path")` reads
  a CSV source under its declared dialect. `dtype=str` with the default dialect
  equals `pd.read_csv(path, dtype=str)`. A headerless file is width-checked and
  a refusal is `PipelineValidationError` with `source_file.column_count_mismatch`,
  `source_file.nul_byte` or `source_file.lone_cr`; the message is
  `<code>: source <table> <detail>` and holds no value, path or file name. New
  parameters are keyword-only and additive. `CsvDialect` and `resolve_csv_dialect`
  refuse an incoherent dialect, or a `column_names` that is not a list or tuple of
  strings, with `ConfigError` whose fixed text never echoes a submitted value.
  A csv `FileSource` must set `has_header`. With `has_header: false` it names its
  columns inline (`column_names`) or in `column_names_file`, never both: a
  one-record UTF-8 header file of at most 1 MiB, read with the source's delimiter
  and quoting, BOM and blank lines skipped, names kept unstripped, under the bound
  file-access policy. A file that is missing, unreadable or not a regular file,
  not UTF-8, empty, over 1 MiB, holds a NUL byte or a lone CR, has more than one
  record, or a blank or duplicate name is refused with
  `source_file.column_names_file_invalid` and a fixed phrase that names no value.
  `resolve_csv_dialect(descriptor, *, table="source", file_access=None)` requires
  `has_header` in the descriptor and raises `ConfigError` before any IO without it.
  `nrows=0` returns the empty frame with the declared columns. The keyword-only
  `columns` (SCAN-MULTI S1b) parses only the named columns and returns every frame
  or chunk with exactly them in the order given; a headerless file maps the names
  onto `column_names`; an unknown or repeated name is a `ValueError` that quotes
  no name.
  It also includes `scan_sources(sources, *, tables=None, sample_rows=10_000, strategy="head",
  file_access=None)`, `ScanSet`, `TableScan`, `ScanSetCore` and `scan_set_to_dict(scan, *, detail)`
  (SCAN-MULTI S1a, 2026-11): new keyword-only parameters are additive; errors name the table and
  the cause's class only. SCAN-MULTI S1b adds the keyword-only `relationships`, `integrity` and
  `spill_dir`, and `RelationshipFacts` and `ChildFacts` (counts only, in `ScanSet.relationships`
  and the `relationships` member of `scan-set/v1`).
  It also includes `parse_pipeline_config`, `ConfigProblem` and `ConfigRuleError`
  (VALUE-FREE-ERRORS, 2026-11): `parse_pipeline_config(raw)` raises
  `PipelineConfigError` whose `problems` are `ConfigProblem(code, location, text)`
  with config names kept and submitted values dropped. `PipelineConfigError` no
  longer sets `__cause__`; its `validation_error=` argument is removed. A
  validator's message is shown only when it is a `ConfigRuleError`.
  It also includes `run_mask_chunked` (2026-10-01): the chunked
  dispatcher as a public entry point. Its signature is
  `run_mask_chunked(config, chunks, *, table, engine_version, registry=None,
  adapter=None, vault_writer=None, chunk_result_sink=None, key_provider=None,
  base_row_offset=0, native_threads=1, route_evidence_sink=None,
  pool_cache=None)`: `config` and `chunks` are positional-or-keyword, everything
  after the `*` is keyword-only, and new parameters are additive. Its pinned
  guarantees are value equality with `run_mask_pipeline_chunked` (one stated
  exception: on the stock-adapter path, a passthrough column no `when:` predicate,
  sibling-reading strategy or composite generator reads or writes is the source
  column itself and never goes through pandas, so a value the oracle's round trip rounds or refuses, such as a
  nullable integer above 2^53, comes back exact; a read passthrough column that
  pandas refuses raises `chunked_passthrough_value_unrepresentable`, and a custom
  or subclass adapter carries nothing), one output type per column per call (`string` for hash, truncate,
  string-redact and native-admissible deterministic categorical columns, the source type for passthrough, except
  that a pass-through `large_string` column is `string` while its values fit 32-bit offsets; see
  "Pass-through text width" in §3.4), no pandas
  metadata on yielded chunks, and identical validation on both routes. A column
  that is `null`-typed in the first chunk and typed in a later one raises
  `chunked_leading_null_type` on both routes. Its error codes are
  `invalid_native_threads`, `native_chunk_schema_drift`, `chunked_schema_mismatch`,
  `chunked_leading_null_type`, `chunked_passthrough_value_unrepresentable` and
  `chunked_route_evidence_inconsistent`; each chunk's
  `quality_metrics["chunked_route"]` carries `pandas_read_passthrough`. Under the
  `warn` unconfigured-column policy (the pre-GA default) source columns the config
  does not cover run on the native route and come back as the source column, with the
  oracle route's one `undeclared_output_columns` warning per chunk; under `error` the
  table keeps the oracle route and raises. The reroute reason
  `unconfigured_set_mismatch:<oracle set>:<native set>` is a defensive cross-check
  between the two definitions of "unconfigured".
  Non-deterministic categorical became seeded on 2026-10-04 (slice C1b-i), a determinism-contract
  change on the whole-frame path: the draw for the non-null row at ordinal `g` is
  `derive_index(mask_key, namespace, encode_int(g), ...)`, where `g` is the ordinal within the
  frame the handler receives (the physical row for a plain table, the match ordinal under
  `when:`, the synthetic-frame ordinal under FK orphan remapping). The same job seed and input
  now give the same output; before, two runs differed. The draw ignores the source value, so
  it does not preserve joins, and a namespace is now required (`categorical_requires_namespace`).
  No routing outcome changed: multi-table split, out-of-core and the native and chunked routes
  still decline it.
  Non-deterministic `reuse` Faker became position-keyed on 2026-10-05 (slice C5b-i), a
  determinism-contract change on the whole-frame path and a one-time pre-GA output break for every
  such column. The draw for the non-null row at ordinal `g` is
  `pool.values[derive_index(job_seed, selection_namespace, encode_int(g), pool.size)]`, with `g`
  the ordinal within the frame the handler receives (same rule as categorical) and
  `selection_namespace` the configured namespace, else `faker-nd/{len(table)}:{table}/{len(column)}:{column}`
  (a nested child uses the outer column; `None` and `""` both mean "not configured"). The key is
  `job_seed`. Pool identity and contents are unchanged. Columns without a namespace used to share a
  stream and now differ. Non-deterministic `unique`, `match_source_cardinality` and
  `scale_source_cardinality`, deterministic Faker, and generation are unchanged. No routing outcome
  changed.
  Deterministic categorical joined the native chunked route on 2026-10-04 (slice C1).
  A native-admissible column (deterministic or `allow_collisions`, namespaced, all-string
  categories, buildable CDF, `string` source) is masked by the compiled index kernel and its
  chunked output type is pinned to `string` on both chunked legs; before, the oracle gave
  `null` for an all-null chunk and `double` for an empty one. This is a new route-dependent
  output-type case under ROUTE-OUTPUT-CONTRACT: the full-frame and unified-slice routes still
  resolve the type at assembly, so an all-null categorical column is `null` there and `string`
  on the chunked route, and a split multi-table job can carry both shapes. Masked values are
  unchanged on every route. A categorical column the native operator cannot run (numeric
  categories, a non-`string` source, unbuildable weights) keeps its oracle values and
  types. A non-deterministic categorical column runs on the native chunked route (slice C1b-ii)
  when its config is complete before any chunk: a namespace, explicit all-`string` categories
  without `from_profile`, and weights the CDF can build. Its draw is keyed by global row
  position (`base_row_offset` plus the local index, as a `uint64`), so native chunked output
  equals the oracle chunked route and the whole-frame run for any chunk size. Any other
  non-deterministic categorical column fails chunked preflight with
  `categorical_nondeterministic_not_chunk_safe`. An admitted one whose first-chunk source is
  not `string` (including an all-null source) runs the chunked oracle route with the same
  seeded draw, the path a deterministic categorical already takes, so it never fails an
  auto-chunked job that the whole-frame run completes. A null-typed first chunk followed by
  a typed chunk raises the existing `chunked_leading_null_type` for every chunked strategy.
  A column carrying `when:` fails with `chunked_categorical_nondeterministic_when_not_supported`. A table with a declared
  FK relationship still runs on the oracle route, reproducibly. Multi-table split (C1b-iii)
  and out-of-core (C1b-iv) still decline it. The unified full-frame route runs it from
  2026-10-06 (slice C5b-iii), for a single-table job whose source is `string`, with no `when:`,
  vault or FK relationship, and the output equals the pandas run. A deterministic one
  without a namespace, with `from_profile`, or without explicit categories keeps
  `chunked_strategy_conditions_unmet`.
  `bucket_perturb` joined the native chunked route on 2026-10-04 (slice C2). A native-admissible
  column (explicit non-empty `date_format` without `%z` or `%Z`, valid `bucket`, namespaced, no
  `when:`, `string` source) is masked by the compiled index kernel. Its chunked output type is
  unchanged under ROUTE-OUTPUT-CONTRACT: it equals the oracle chunked route's content-dependent
  type (a zero-row or all-null chunk is `null`, any chunk holding a non-null value is `string`),
  and the schema rule does not pin it. A `large_string` source, an autodetected or timezone
  format, an invalid bucket, `when:` and FK-key edges keep their oracle path and codes. A column
  whose every chunk had no parseable row reports `executed_backend` `arrow_python`, not
  `rust_companion`, and `compiled_kernel_executed` stays `False`.
  `date_shift` joined the native chunked route on 2026-10-05 (slice C4). A native-admissible
  column (explicit non-empty `date_format` without `%z` or `%Z`, namespaced, no `group_by`,
  no `when:`, integer day bounds, `string` source) is shifted by the compiled index kernel.
  Its chunked output type is pinned to `string` on both chunked legs, with or without the
  companion. Under ROUTE-OUTPUT-CONTRACT the whole-frame route still resolves the type at
  assembly, so an empty column is `double` there and an all-null column is `null` there,
  while both are `string` on the chunked route; shifted values are unchanged. A non-null
  unparseable value fails closed with `RowErrorsFailedError` on both chunked legs, with
  chunk-local `row_index` records. A non-string source, an autodetected format, `group_by`,
  `when:` and FK-key edges keep their oracle path and codes.
  Both date operators accept the additive `preserve_values` provider_config key (Phase D / D1c):
  exact ISO dates emitted unchanged, with an explicit `date_format`. Sentinel rows are removed
  before compute on every route, so the route choice and the unset output are unchanged. A
  configured column adds a value-free `preserved_rows` count to its evidence. v1 supports payload
  columns and the parent key of one single-column, two-table relationship; `group_by` and every
  other FK shape are rejected at compile time.
  `group_key` joined the native chunked route on 2026-10-05 (slice C3). A column whose
  `group_by` sibling is an unmasked `string`, `int64` or `bool` column (no `when:`, not an FK
  key) is keyed by the compiled raw-hex kernel. Its chunked output type is pinned to `string`
  on both chunked legs, with or without the companion. Under ROUTE-OUTPUT-CONTRACT the
  whole-frame route still resolves an empty column to `double` at assembly while the chunked
  route yields `string`; keys are unchanged on every route and are never null. A wider
  sibling type, a masked sibling and a self-anchor keep running on the oracle chunked leg with
  their oracle output types.
  `text_redact` joined both native routes on 2026-10-06 (slice C6c-i) as an `arrow_python`
  operator. A native-admissible column (no `ner`, a string `token`, `detectors` absent, null, a
  list or a tuple, no `when:`, a `string` source) runs the oracle's own span detection and splice
  over Arrow values, so values equal the oracle byte for byte. The column's own speed is
  unchanged; what changes is that it no longer sends the rest of its table to pandas. Evidence
  reports `arrow_python`, never a compiled kernel. Its chunked output type is pinned to `string`
  on both chunked legs, with or without the companion. Under ROUTE-OUTPUT-CONTRACT the unified
  full-frame route still resolves an empty or all-null column to `null`, while the chunked route
  yields `string`; values are unchanged. `ner`, a non-string `token` and a `detectors` value that
  is neither null, a list nor a tuple (the oracle leaves the source column unchanged for the last
  two), a non-`string` source and `when:` keep their oracle path; the first three report
  `text_redact_ner_not_native`, `text_redact_token_not_string` and
  `text_redact_detectors_malformed` on the eligibility report, and a non-`string` chunked source
  reports `text_redact_source_type_not_string`. A table that was already declined to the oracle
  chunked leg by one of these now gets the `string` pin only when its text_redact config is
  native-admissible, so an oracle-routed degenerate chunk of such a column is `string` where it
  was `null` or `double` before.
  Non-deterministic `reuse` Faker joined the chunked route on 2026-10-06 (slice C5b-ii). It
  keeps the position-keyed draw of slice C5b-i, taken at the chunk's global row offset, so
  chunked output equals the whole-frame run for any chunk size. A column is admitted when it
  declares an explicit `pool_size` (top level or `provider_config`), uses a provider in the C1
  string-output allowlist (`person_first_name`, `person_last_name`), has no `when:` and is not
  `unique`, `match_source_cardinality` or `scale_source_cardinality`; the namespace stays
  optional. A string source runs the compiled path and any other source type runs the chunked
  oracle leg with the same draw. A column that fails one of these conditions keeps running
  whole-frame, and a missing `pool_size` or a provider outside the allowlist reports
  `chunked_strategy_conditions_unmet` naming the unmet condition. A column that is admitted and
  carries `when:` fails with `chunked_faker_nondeterministic_when_not_supported`, because the
  oracle numbers only the matching rows. The chunked output type of an admitted column is
  pinned to `string` on both chunked legs, with or without the companion. Under
  ROUTE-OUTPUT-CONTRACT a whole-frame column that is empty or entirely null keeps the type
  pandas infers there, while the chunked route yields `string`; values are unchanged. A custom
  provider registered under an allowlisted name whose pool holds non-string values fails closed
  with `chunked_faker_nondeterministic_pool_not_string` before any chunk is written on both
  dispatcher legs (the legacy `chunked_dispatcher_enabled=False` lane does not run this check and
  can instead fail later with `chunked_schema_mismatch`), which is
  a new failure for an auto-chunked job that ran whole-frame before; disable auto-chunking or
  register the provider under another name. A multi-table split run now dispatches an
  above-threshold Faker table chunked like its siblings, with the same values. Out-of-core
  still declines it. The unified full-frame route runs it from 2026-10-06 (slice C5b-iii), for a
  single-table job whose source is `string`, with no `when:`, vault or FK relationship, and the
  values equal the pandas run. A zero-row table reports `arrow_python` evidence there, as an idle
  chunked column does.
  Deterministic Faker (and the `allow_collisions: true` alias) over a `bool`, signed-integer or
  unsigned-integer source joined the native routes on 2026-10-09 (slice C5c-ii), output
  byte-identical to the pandas oracle. The draw keys from the source value, so the chunked route
  admits a column only from a producer that guarantees the stream schema (resident slices or
  fixed-schema reconstructed batches) and whose schema metadata is in a closed allowlist (metadata
  absent, or plain pyarrow `b"pandas"` metadata with an identity column mapping whose per-column
  `pandas_type` and `numpy_type` both match the physical type); any other metadata shape, and any
  ordinary table iterable, declines to the oracle with `faker_conversion_schema_not_guaranteed` or
  `faker_conversion_metadata_not_allowlisted`, output unchanged. The unified full-frame route admits
  the same families for a single-table job whose pandas round trip is value-identical, including a
  pandas nullable `Int64`/`UInt64` column that the conservative chunked allowlist still declines
  (an intentional, safe route-dependent asymmetry). Float and temporal sources stay on the oracle.
  Under ROUTE-OUTPUT-CONTRACT an admitted column whose output is empty or entirely null is now
  Arrow `string` on every route (option A), where pandas inferred `double` for empty and `null` for
  all-null before; the string-output provider makes `string` the honest type and value-bearing
  output was already `string`. Only the empty/all-null case changes, and only for a newly admitted
  column. The full-frame and unified routes carry pandas string metadata on that column; the chunked
  route carries none, the same metadata difference the other pinned strategies record here. A custom
  provider registered under the configured name whose pool holds non-string values fails closed with
  `faker_provider_output_not_string` before any chunk is written, as for the position-keyed variant.
  `ColumnConfig` gained an optional `when: str` field (2026-10-06, slice C8-i), additive. It accepts
  a closed grammar only (a comparison of one column and one literal, `in` and `not in` over a
  literal list, `and`, `or`, `not` and parentheses), validated when the config loads with the
  error type `when_outside_closed_grammar` at `tables.<i>.columns.<j>.when`; a blank value is
  `None`. The pandas oracle still evaluates a validated predicate with `DataFrame.eval`, and a raw
  dict passed to `run_pipeline` is not re-validated. On the chunked route `when:` runs natively for
  `hash`, `redact`, `truncate` and deterministic `categorical` over a `string` source: the row mask
  is the oracle's own predicate function over the oracle's own conversion of the columns the
  predicate reads, per chunk, so a null in a nullable boolean mask selects nothing, exactly as
  `df.loc[mask]` does. A predicate that reads a column an earlier work node (column-name order)
  masks, a target that is not `string`, a predicate outside the grammar and every other strategy
  keep the oracle leg, with the codes `when_predicate_reads_masked_column`,
  `when_predicate_not_native` and `when_predicate_outside_native_subset`. The chunked output type
  of a native-admitted `when:` column is pinned to `string` on both chunked legs; under
  ROUTE-OUTPUT-CONTRACT a whole-frame column that is empty or entirely null keeps the type pandas
  infers (Arrow `null`), while the chunked route yields `string`; values are unchanged. The
  auto-chunk planner keeps such a table chunked only when every column the predicate reads is a
  `string`. The unified full-frame route and out-of-core still decline `when:`.
  `run_pipeline` gains two keyword-only arguments (2026-10-01): `native_threads: int = 1`
  (the default moved on 2026-11-20 to `None`, see the ENGINE-THREADS-DEFAULT entry below; 1 to 1024; the kernel thread budget of the auto-chunk dispatcher lane, no output
  byte depends on it) and `chunked_dispatcher_enabled: bool = True` (the kill switch that
  restores the previous auto-chunk lane). This is an owner-approved pre-GA
  output-contract cutover: on the auto-chunk route masked values are route-neutral
  (equal to the full-frame route's), while schema, passthrough types, field nullability,
  field metadata, the native Faker all-null type and schema metadata follow the
  dispatcher's contract (passthrough columns are the source column exactly, string-output
  columns are `string`, no schema metadata) and may differ from the full-frame and
  unified-slice routes. Converging those routes is roadmap item ROUTE-OUTPUT-CONTRACT.
  Output Parquet key/value metadata (OUTPUT-METADATA-LEAK, 2026-11-19): on every route, a
  published Parquet file carries no field metadata at any depth (this ends "passthrough
  field metadata kept"), writes an extension-type column as its storage type (except
  `arrow.uuid`, `arrow.bool8`, `arrow.json` and the pandas period and interval types) and
  carries one schema key at most, `b"pandas"`, rebuilt from the output schema
  (see `docs/security/output-metadata.md`). The schema metadata difference between routes
  narrows to "a sanitized pandas blob or none". Output bytes change only for a source with
  custom metadata, `attrs`, field metadata, a column-index name or a range index; masked
  values, column names, other Arrow types, nullability and row-group layout do not change. The
  public names `output_schema` and `output_table` are additive. In-memory results returned
  to a caller are outside this promise.
  `run_pipeline` gains one more keyword-only argument (2026-10-02),
  `multi_table_dispatch_enabled: bool = True`: with no FK edge in the job, each mask table
  that would auto-chunk alone dispatches on the same lane, so the auto-chunk shape above now
  applies per table inside an independent multi-table job, and one result can mix that shape
  with the full-frame shape for the tables left in the full-frame group. `False` restores
  the single full-frame call.
  `run_pipeline` gains one more keyword-only argument (2026-10-02), `stream_chunked_output:
  bool = True`, next to `chunked_dispatcher_enabled`. The existing `sink` argument is now
  also consumed by the auto-chunk dispatcher lane and by a B7 split in which every table is
  dispatched: the masked chunks go to `sink.write_batches` one Parquet row group at a time,
  `sink.commit()` runs once as the last action, any failure calls `sink.abort()` once, and
  the result carries `outputs == {}` with `quality_metrics["execution"]["outputs_streamed"]`
  true and a per-table `quality_metrics["auto_chunk"]["output"]` evidence block. A caller
  that passes a sink must accept that shape; a caller that passes none, or sets
  `stream_chunked_output=False`, gets the resident outputs as before. Validators,
  quarantine, `fidelity_report` and `post_validation` keep the run resident, each with a
  recorded reason. With `ParquetTransactionalSink` the published file is byte-identical to
  `pq.write_table` of the resident table whenever no row group was cut by the byte cap.
  `run_pipeline` now admits a `LazySource` in `sources` to auto-chunk and to the B7 split
  (2026-10-03, B6b). Routing reads each lazy table's Parquet footer once (row count, schema,
  per-column null counts, row-group layout) and judges it with the same gates as a resident
  table. When the output streams, the table is read as record batches re-cut to the resident
  chunk boundaries and never materialized; otherwise it is materialized once before the lane.
  A gated column (an integer column, or a bucketize source column) with no footer null count
  declines auto-chunk with `lazy_source_null_count_unavailable`. New error codes:
  `lazy_source_changed` (the opened file's footer facts differ from the routing snapshot, raised
  before the first chunk), `lazy_source_row_count_mismatch` (the stream's row total differs from
  the footer's, raised before commit) and `hold_back_spill_unavailable` (a streamed run's schema
  hold-back must spill and the sink has no `spill_parent`). `LazySource` gains `footer_facts()`
  (one open handle) and `open_batches(batch_rows, *, pre_buffer=None, buffer_size=0,
  use_threads=True)`, which now returns an `OpenedLazyBatches` owner (`schema`, `num_rows`,
  row-group facts, `batches`, an idempotent `close()`) instead of a `(schema, iterator)` pair.
  `ParquetTransactionalSink.spill_parent` is a read-only property (the target's parent, where the
  hold-back spills); it is not part of the `TransactionalSink` protocol. Every routed table's
  input block (`quality_metrics["auto_chunk"]["input"]`, written `auto_chunk.input`) records `mode`, `reason` and, for a lazy table,
  `source_row_groups` and `source_max_row_group_rows`; `execution.loaded_fully_in_memory` is
  `False` when every source of a streamed run was read lazily. A caller that passes resident
  tables sees no change except the added `input` block.
  SCAN-ML M1 (2026-11) adds `ModelRef`, `ModelStatus`, `classifier_status`, `ner_status`,
  `load_detectors`, `ModelUnavailableError`, `PreScanConfig` and `Dismissal`, and the config fields
  `pre_scan.detectors`, `pre_scan.ner_model`, `post_scan.detectors`, `post_scan.ner_model`,
  `post_scan.ner_findings` and `post_scan.dismissed`. All are additive. A field equal to its default
  is left out of the dump, so a config that predates them keeps its exact dump,
  `pipeline_config_hash` and plan hash; the plan hash leaves out `pre_scan` and keeps
  `post_scan.dismissed` (under `ner_findings: fail` a dismissal decides what blocks). Registry 1.8.0
  adds `config.detectors_invalid`, `config.post_scan_inactive`, `config.pre_scan_ner_inactive`,
  `config.ner_model_invalid`, `config.dismissal_invalid`, `provider.classifier_unavailable` and
  `provider.ner_model_incompatible`. Three behavior changes. `ensure_ner_available` now checks the
  model name (one `fullmatch` rule) and refuses a model built for another spaCy minor, so a
  `text_redact` `ner` column fails at validate time where it used to fail at load. A name counts as
  an installed model only when it is an installed distribution that ships a spaCy `meta.json`; a
  plain importable module (`os`, `json`) is refused with `provider.ner_model_not_installed` and is
  never imported. `ModelPackLoadError` gains a `cause` field (`disabled`, `import` or `pack`).
  SCAN-ML M5 (2026-11) adds `PostScanRouteError` and one coordinated pre-GA break. The entries
  that mask without running the post-scan, `run_sequential`, `run_fk_out_of_core`,
  `run_mask_pipeline_chunked`, `run_mask_chunked` and `run_native_or_oracle_chunked`, now refuse a
  config whose `post_scan.integrity` is true and whose `post_scan.detectors` holds a model detector
  or whose `post_scan.ner_findings` is `fail`, before any source is read. The error carries
  `route.post_scan_models_unsupported` (registry unreleased); `str(error)` and `message` are the fixed
  registry summary, `route` and `setting` name the route and the config field, and the exception
  chain is empty. Regex-only `integrity: true` is not refused on these routes; `run_pipeline` judges it from the staged output (POSTSCAN-STREAM PS-12). `run_sequential`
  and `run_fk_out_of_core` take a required keyword-only `config` (the raw pipeline config), as do
  the wrappers that forward to them (`PandasExecutionAdapter.run_sequential`, the physical
  `SequentialAdapter.run` and `OutOfCoreAdapter.run`); a caller that passed none must now pass
  it. `run_pipeline` is unchanged: its resident auto-chunk lane calls guard-free cores and runs the
  post-scan itself.
  SCAN-ML M4 (2026-11) adds `PostScanBlockedError` and model findings in the post-scan.
  `run_post_mask` takes a keyword `detectors` (a `DetectorSet`); `None`, the default and the
  platform hook's call, runs the regex detectors as before. `run_pipeline` with
  `post_scan.integrity` true loads its `post_scan.detectors` strictly at run start, so a model that
  cannot run refuses the run with `ModelUnavailableError` before any row is masked, and runs each
  model over every output table after the regex pass. Under `post_scan.ner_findings: fail` an NER
  finding at `fail`, or an NER pass that raised, makes `run_pipeline` raise `PostScanBlockedError`
  (`post_scan.ner_blocked`, registry unreleased) before it returns: `report` is the value-free
  report, `verdict` the one decision, and the exception chain is empty; when NER alone blocks,
  `str(error)` and `message` are the fixed registry summary. `post_scan_verdict` takes
  `ner_findings` (`warn` by default) in place of `ner_blocked`; a model finding blocks only through
  `ner_blocked`, which ranks last, and a classifier finding never blocks. A
  `post_scan.dismissed` entry now lowers the `warning` it names to `info`. `classify_frame` gains a
  keyword `subsample` (default `False`, so `classify_fields` is unchanged): `True` classifies a
  seeded subsample of at most 60 non-null values per column.
  SCAN-ML M3a (2026-11) adds the keyword `pre_scan: PreScanConfig | None = None` to `scan_sources`,
  `ModelHit` (exported from `decoy_engine` and `decoy_engine.profiling`), `TableScan.model_hits`
  (column name to a tuple of `ModelHit`, empty by default) and `ScanSet.detectors` (a tuple of
  `DetectorRun`, `regex` always first). `None` runs the regex detectors only, as before. The models
  load strictly once before the first table, so an unavailable model raises
  `ModelUnavailableError` and no table is read; any other model failure is a `ScanSourceError`
  naming the table and the exception class. `decoy_engine.detection` exports `DetectorRun`,
  `ModelRef`, `NerRate`, `ner_rates`, `log_detector_run` and `log_detector_models`. `scan-set/v1`
  gains `detectors` on `ScanSetCore` and `model_hits` on each field, both additive with empty
  defaults and strict models (`ModelHitCore`, `ModelRefCore`, a tagged detector-run union): a record
  written before M3a still validates and reads back with empty lists, `ScanSetCoreRead` ignores
  unknown keys, and no stored field can hold a cell value (a source, a detector id, a model id and
  version, a band and a rate bucket). The starter config ignores model hits.
  SCAN-ML-MD M-1 (2026-11) makes the NER hit rate per model: `decoy_engine.detection._ner_rate`
  gains `NER_MIN_RATES` (sm 0.211, md 0.122) and `NerRate.min_rate`, and `NER_MODEL_WHEELS` gains
  the `en_core_web_md` 3.8.0 wheel. The default model is unchanged. `load_detectors` now refuses an
  installed NER model that has no entry in `NER_MIN_RATES` with `ModelUnavailableError`
  (`provider.ner_model_uncalibrated`, registry unreleased), so a config that names
  `en_core_web_lg`, `en_core_web_trf` or a non-English model for `pre_scan` or `post_scan` is
  refused at load, before any row is read, where it used to run on sm's 0.211. A model that is
  absent or incompatible keeps its own earlier reason. `text_redact` and `text_mask` `ner` columns
  read no rate and are not refused for any model name.
- **CLI:** verb names, flag names, and the exit-code contract (0 ok, 1
  validation/usage, 2 deprecated-shim, 3 runtime).
- **Config:** the `pipeline.yaml` schema. An old config must keep validating and
  running, or be deprecated through §4.4.

  Pre-GA default change (ENGINE-THREADS-DEFAULT, 2026-11-20).
  `run_pipeline(..., native_threads: int | None = None)`: `None` uses the usable cores
  (`detect_usable_cpus`, capped at 1024; 1 if detection fails). Explicit values are unchanged and
  never fall back. Output data does not depend on the thread count. Default runs now read lazy
  Parquet sources with Arrow threads and record `native_thread_clamp` under `auto_chunk`. Every
  run records `quality_metrics["execution"]["native_threads"] = {"count": int, "source": str}`,
  where `source` is one of `explicit`, `auto` or `auto_fallback` (a closed set). `count` is the
  requested value; the native kernels apply at most their own knee (4 threads), which
  `auto_chunk["native_thread_clamp"]` reports. `run_pipeline_isolated` with a `mem_cap_bytes`
  and no `native_threads` forwards `native_threads=1`, because the Rust shared pool is sized to
  the host's cores and its thread stacks count against RLIMIT_DATA. `run_job_with_governor` with
  no `native_threads` also forwards 1, because a governed job shares the host with other slots.
  Both runs record source `explicit`. The thread count now
  differs by host for library callers, as it already does for the CLI. Re-pin checklist: the
  platform passes its own thread grant; check any platform call that omits `native_threads`.

`run_pipeline` gained `token_vault_writer` on 2026-10-26 (B-6 tokenization
write-seam). It is keyword-only, defaults to `None`, and is additive under §4.1:
a caller that does not pass it sees no change, and the `tokenize` strategy only
runs when it is supplied (the job fails closed with `tokenize_requires_vault`
otherwise). Passing it forces the full-frame route and disables auto-chunk when
the route is left on `auto`; an explicitly bounded `execution_mode` is left as
set and the job fails closed instead, since the bounded routes do not thread the
writer. The frozen-signature table in
`tests/unit/execution/test_r3_public_signature_freeze.py` records the new
parameter. The platform-facing `TokenVaultWriter` protocol is scoped by
namespace: `lookup_existing(namespace, plaintext)` and
`put(namespace, plaintext, token)` both take the column namespace, so a vault
keys identity by `(namespace, plaintext)`, not plaintext alone. A platform
implementation must key its store the same way, or the same value under two
namespaces would collapse to one token. tokenize is an always-keyed strategy,
so at GA it requires a resolved >=32-byte secret like every other keyed
surface (`keyed_strategy_requires_secret`); it never derives tokens from the
public job seed.

A file-vault `vault_writer` is accepted only where the engine holds every output
row until the caller can publish the vault first (B-6 boundary rule B0, 2026-11-01;
breaking, pre-GA): `run_pipeline(sink=..., vault_writer=...)` with both set,
`run_mask_chunked` and `run_mask_pipeline_chunked` with any `vault_writer`, and
`run_fk_out_of_core` with both a `sink` and a `vault_writer` raise
`ExecutionError(code="vault_requires_resident_output")` before any source or chunk
is read. The parameters stay in the signatures (so the refusal is explicit, not a
`TypeError`); `vault_writer=None` is unchanged. A vault job runs through
`run_pipeline` without a sink; its auto-chunk lane still bounds masking memory.

`TokenVaultWriter` gained a required `derivation_scope: bytes` attribute on
2026-11-01 (B-6 D2; a breaking protocol change, legal pre-GA only): a non-secret,
vault-unique 16-to-64-byte value the vault owner mints once and stores with the
vault. Tokens are derived under it (`tokenize-key/v1` subkey), so two vaults never
mint the same token for one plaintext and a token payload is never a `hash`
output. Tokens minted before this change cannot be reproduced: a writer without a
scope is refused with `token_vault_derivation_unsupported`, and a malformed one
with `tokenize_vault_scope_required`, before any source is read. There is no
compatibility derivation and no migration (hard cut); an old vault stays readable
only by the release that wrote it.

`keyed_columns_for_config` and `KeyedColumn` joined the public API on 2026-11-08
(init wizard part E3), additive under §4.1. `keyed_columns_for_config(config)`
returns every column of a raw config mapping that needs the mask secret, in
config order, as `KeyedColumn(table, column, strategy)`; each field is the name
as written, or `None` when the config leaves it out. It reads names only, never
a value, a schema or a row. It uses the same column walk as
`secret_requirements_for_config`, so a config needs the mask secret exactly when
this list is non-empty or a relationship has a composite parent key (keyed with
no single field, so not listed) or a missing or malformed parent, which fails
safe. `tests/secret_providers/test_keyed_columns.py`
pins the result with a written-out oracle and the parity rule.

`decoy_engine.is_valid_pipeline_name` and `PipelineConfig.name` joined the public
surface on 2026-11-09 (PROJECT-LAYOUT E1), additive under §4.1.
`is_valid_pipeline_name(value)` is the one pipeline-name rule: 1 to 64
characters from `[A-Za-z0-9_.-]`, first character a letter or digit, no trailing
dot, not a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1` to `COM9`,
`LPT1` to `LPT9`, any case, any extension); a non-string is never valid.
`PipelineConfig.name` is optional and strict: a non-string or a name that fails
the rule is refused with `config.pipeline_name_invalid` (registry 1.12.0) and
nothing is coerced. While `name` is unset the config dump and
`Plan.pipeline_config_hash` are byte-identical to before, and a set `name` is
excluded from the plan hash because it is data binding, not masking semantics.
`tests/unit/config/test_pipeline_name.py` pins all of this.

**Declaration gate on the run, isolated and profile routes (CONFIG-EXPLICIT E2c).** `prepare_declared_sources`
(`DeclarationRequest.for_run`, `for_descriptors`) is the one boundary. `profile_source` calls it first, or accepts the
`PreparedDeclaredSources` that `build_run_context` already made for the same config object (`declared=`; a capability
for another config raises `ValueError`). `build_run_context` checks the config descriptors, the supplied tables and
`source_loader_schemas` together, then wraps the loader with `checked_loader` so a table that materializes with other
columns is refused before it is profiled or masked. `run_pipeline_isolated` runs the same check in the parent before
staging or spawn, and the child `run_pipeline` rechecks. The refusals are `source_file.undeclared_column` (names a
position and a count, never a column) and `source_file.undeclared_table`.
`DeclarationRequest.for_validate` is the offline form for `decoy validate`: it reads only local file headers, never opens an `s3`, `gcs` or other non-file source, and names those sources in `remote_sources` so the caller can report them as not checked (additive, 2026-11-16, QA-GATE G-eng).

`DeclarationRequest.for_scan(config, tables=None)` is the scan form (additive, SCAN-MULTI E-S1c, 2026-11-19). It reads `sources`, `relationships` and `version` of a sources-only file or a full pipeline, checks the inputs with the one `scan_sources` validator and opens no data file. A `column_names_file` is read once. `scan_sources` takes the additive keyword `relationship_positions`. The capability's `scan_declaration` property is a frozen `ScanDeclaration`; on a capability from any other request it raises `TypeError`.

**Declaration gate on the chunked entries (CONFIG-EXPLICIT E2d).** `run_mask_pipeline_chunked`,
`run_mask_chunked` and `run_native_or_oracle_chunked` call `prepare_declared_sources`
(`DeclarationRequest.for_chunks`) after the subset refusal and before `chunks` is touched, then
check every chunk's columns against the declared set. `run_mask_pipeline_chunked` gains the required
keyword-only `source_schema: pa.Schema` (a breaking signature change, no default): the table and that
schema are checked at entry, and a chunk column outside the schema is refused with
`source_file.undeclared_column`. The two native entries keep their signatures. They check the config
level at entry (an undeclared table) and the first chunk's columns right after it is read, before
profile, compile or mask (a named limit: one chunk is pulled before the column check); later chunks
are checked lazily before each masks, so earlier chunks may already be in the sink, as for drift
refusals. The private `_run_chunked`, `_oracle_preflight` and `_oracle_preflight_admitted` take a
required `declared` capability. The CLI's `run --chunked` moves in CLI Part C1.

**Columns the config creates (CONFIG-EXPLICIT E2b-r1).** `profile_source` gains the optional
`supplied_sources` mapping: a supplied-only table is profiled (a same-name descriptor keeps
profiling its own file), which `build_run_context` now passes. Execution routes obtain the
`PreparedDeclaredSources` capability only through `prepare_declared_execution_sources`
(`execution/_guards.py`), which hands `profile` a policy built from `SURFACE_DECLARATIONS`: an entry
creates its own target when its row says so, every column it reads is a source column, and none of
them is the target. This slice only records the policy; it adds no refusal and no reason code.

**Declared-but-missing columns (CONFIG-EXPLICIT E2b-r).** The same boundary now also refuses a column the config
declares for a mask table (a `columns` entry, a `drop_column` column or a composite-relationship child column) that
the source's header, footer or schema does not have: `source_file.declared_column_missing`, registry unreleased. It runs
on every catalog the routes above already check, so the refusal comes before any data row is read; for the chunked
entries the catalog is `source_schema` (pipeline entry) or the first chunk, and each later chunk is checked before it
masks. It names the first missing column and the count (config names, never a value). Order is undeclared table,
undeclared source column, then missing declared column. Before this, `run` failed late with a bare `KeyError`. The reverse rule belongs to the execution
choke point, which knows the columns the config creates: `profile_source`, `prepare_declared_sources`
and `check_columns_declared` called without a created-column policy apply the forward rule only.

**Required strategy settings (CONFIG-EXPLICIT E2b-s).** `compile_plan` and `run_config_only_checks` refuse a column
that omits a setting its strategy needs, with the code the executor guard raises (registered again under the `plan`
namespace, registry unreleased): a `namespace` for `hash`, `fpe`, `date_shift`, `bucket_perturb`, `categorical`,
`tokenize`, `shuffle` with `deterministic: true` and `code_set` with `provider_config.mode: gen`, and
`provider_config.strategy` and `target` for `nested`. Missing means absent, `None`, an empty string or an empty
collection. The requirements are `StrategyRequirement` descriptors in `decoy_engine.plan.checks`, and
`StrategyInfo.requirements` carries the same rows. Specific earlier checks keep precedence.

**Nested cell reading and output (F07 E1).** Recorded pre-GA semantic change (2026-11-17): the `nested`
strategy reads each cell with `strict_json_read` under bounds (16 MiB, depth 64) and writes a number it did
not select as it was read, so `1.10`, `1e400` and a long decimal are no longer rewritten. A cell that repeats
a key, is over a bound or holds a raw lone surrogate (an escaped `\ud800` is valid) fails its row with a closed warning code (`nested_cell_duplicate_key`,
`nested_cell_oversized`, `nested_cell_too_deep`, `nested_cell_json_parse_error`). A selected integer too long
for `int` fails the row (`nested_cell_number_out_of_range`). A leaf list of integers with a null now reaches
the child as integers and a null, not as `float64` with `NaN`, so `hash` and `tokenize` accept it. A child
output of `NaN` or a pandas null is written as JSON `null`; an infinite output fails the row
(`nested_child_non_finite`). A null-free leaf list and an all-float-exact cell give the bytes they gave before.

### 3.5 Disguises

Disguises are dated/versioned specs (`disguises/`, `disguises/loader.py`,
`disguises/schema.py`). A config pins a disguise version so its output stays
stable. No test yet refuses an edit to a released version (known limit `RGF1-KL-3` in
[frozen surfaces](frozen-surfaces.md)); the loader tests pin only the dated version field and the
bundle set.

**Rule:** **never edit a released disguise version.** Add a new dated version.
Review enforces this until a test does; do not "fix" a shipped disguise in place.

### 3.6 Experimental surfaces

Every public engine name has one of three tiers. **Frozen** names follow sections 3.1 to 3.5 and section 4. An
**experimental** name may change or be removed in any minor release; the change gets a `Changed` or `Removed`
changelog fragment that starts with "Experimental:", and no deprecation window is promised. **Internal** names may
change in any release. The registry is `src/decoy_engine/contracts/stability.py`, and
`tests/sentry/release/test_public_census.py` gives every exported name exactly one tier.

- Section 3.4 freezes `decoy_engine/__init__.py` and `decoy_engine/sdk.py` whole, plus the subpackage names it lists
  one by one. A public subpackage that section 3.4 does not name defaults to internal, unless one of its objects
  is exported by a frozen path or the registry lists the name as frozen or experimental. No name is unclassified:
  each export matches one explicit registry row or one package rule.
  Inside a subpackage that section 3.4 names, a name it does not list is internal too (for example
  `detection.load_detectors`).
- The tier belongs to the object and applies to every path that exports it. A constant is matched by its name and
  its object identity, so `SEED_PROTOCOL_VERSION` has one tier wherever it is exported. Only the paths that section
  3.4 names are a stability promise. Another path to a frozen object, such as `decoy_engine.execution.ExecutionResult`,
  is an internal import path to a frozen object: the object does not change incompatibly, but the path may move.
  `decoy_engine.FileSource` and `decoy_engine.sdk.FileSource` are one experimental object; `sdk.ConfigError` and
  `sdk.ConnectorError` are the frozen `errors` objects.
- Everything `sdk.py` defines is experimental: each class or function whose `__module__` is `decoy_engine.sdk`, and
  each constant assigned there, so a new `sdk` name starts experimental. The Faker provider registration functions
  live elsewhere and are listed in the registry by identity.
- Determinism is never experimental. Output bytes made through an experimental surface (a custom provider's values,
  a connector's rows once masked) follow section 3.3. Stored records are frozen whatever the tier of the code that
  wrote them.
- An experimental class or function starts its docstring with `Experimental:`. An experimental constant has no
  docstring, so the docstring of its module starts with the marker and names it. The documentation page for an
  experimental surface carries the marker in its first paragraph. A sentry checks the marker against the registry
  both ways.
- A public package that needs an optional extra may fail to import only for that extra. The census then reads its
  declared names from source and fails on any other import error, on an `__all__` it cannot read, and on a public
  package without `__all__` that is not on the registry's counted list. The census floors are 59 packages and
  1,186 names.
- An experimental surface becomes frozen by moving its registry row and dropping the marker in a minor release. A
  frozen name never goes back to experimental inside a major.

The experimental names at 1.0, one per line (the sentry compares this list with the registry):

```text
CAP_DRY_RUN
CAP_INTROSPECTION
CAP_MULTIPART
CAP_RESUMABLE
CAP_SIGNED_URL
CAP_STREAMING
CheckResult
ConnectorConfig
FileMeta
FileSink
FileSource
PermanentError
SDK_VERSION
TransientError
WriteResult
atomic_swap_db_providers
load_custom_providers
register_faker_list_provider
register_faker_provider
register_faker_provider_v2
unregister_faker_provider
```

## 4. The rules for adding a feature without breaking anyone

### 4.1 Additive-only public surface

New parameters are keyword-only with defaults that preserve existing behavior.
Never remove or rename a public symbol, and never change a default that changes
output, without the deprecation path in §4.4. The **engine-stays-narrow** rule
(best-practices §3.3) is your ally: the smaller the public surface, the less you
*can* break. Convenience layers (`decoy.mask`) live in the CLI package, not the
engine.

### 4.2 Format versioning, never mutation

See §3.1. Bump the tag, keep the old reader. Prefer an additive optional field
over a version bump when old readers can safely ignore it.

### 4.3 Determinism is sacred within a major

See §3.3. If a feature *needs* to change derivation (e.g. a better KDF), it is a
major-version project with a migration story, not a feature PR.

### 4.4 Deprecation mechanics (when you must change CLI/API)

The `pre-scan scan` → `pre-scan analyze` rename is the template. To retire or change a
public surface:

1. Keep the old surface working as a shim.
2. Emit a `DeprecationWarning` to stderr (CLI: exit code 2 lane).
3. Hold it for **at least one minor release.**
4. Document the removal target version.
5. Add a `CHANGELOG.md` entry (Keep-a-Changelog).

Exit codes stay stable throughout.

### 4.5 The vault is forever

See §3.2. No exceptions.

### 4.6 Platform specifics (when the platform unfreezes)

- Alembic migrations are reversible (up **and** down) and there is a **single
  head** — the gap-closure work already enforces this.
- Ship features dark behind a flag (the `DECOY_ENABLE_ENVIRONMENTS` pattern) so
  code can land before it activates.

## 5. The decision procedure

Before you change something, find it in this table.

| You want to change… | Frozen? | What to do |
|---|---|---|
| Add a new strategy / detector / report metric | No (additive) | Ship it. Register through the public path. New optional config only. |
| Add `fpe_join_group` to an fpe column | No (additive opt-in) | Permitted under the additive-only rule. A manifest that USES a join group freezes its tweak resolution: if the group name changes or a member is removed from the group, the ciphertext changes and unmask against the old config will not reverse the new output. Treat a group name as part of the masking key for that column. |
| Add an optional field to an existing artifact | No (additive) | Add it; ensure old readers ignore it; no version bump. |
| Change the **shape** of an existing `name/vN` artifact | **Yes** | Mint `name/v(N+1)`, keep the `vN` reader. PO review. |
| Change hashing / FPE / Faker seeding / `derive` | **Yes** | Major-version project + migration story. Not a feature PR. |
| Change/remove a public Python symbol or default that affects output | **Yes** | Deprecation path §4.4, or major bump. |
| Rename/remove a CLI verb or flag | **Yes** | Deprecation shim §4.4 (one-minor window). |
| Edit a released disguise version | **Yes** | Forbidden. Add a new dated version. |
| Any change to vault read/write | **Yes (forever)** | New version + permanent prior-version reader + PO review. |
| Change a `pipeline.yaml` schema field | **Yes** | Keep old configs valid, or deprecate §4.4. |
| Internal refactor with no surface change | No | Golden + compatibility tests must stay green; if a golden baseline moves, you changed a guarantee — stop. |

If your change is **Yes (frozen)** and you cannot justify a major bump, the
answer is almost always: **make it additive instead.** Most "I need to change X"
turns into "I can add X alongside the old X" with five more minutes of thought.

## 6. How the freeze is enforced (the machinery)

- **Golden / determinism snapshots** (the `tests/snapshots/golden` baselines a test
  reads, `tests/unit/determinism/test_derive_vectors.py`,
  `tests/native/test_draw_site_providers_golden.py` with
  `tests/native/test_determinism_goldens.py`) catch **output drift**. A baseline change is a red flag, not a refresh chore.
- **The cross-version compatibility corpus** (`tests/integration/compat_corpus/`).
  Golden tests regenerate artifacts with *current* code, so they do **not** catch
  *format* drift. The corpus freezes synthetic artifacts produced at a known
  engine version and verifies the *current* engine can read and round-trip every
  one. It currently covers two read-back artifact kinds, three artifacts:

  - `decoy-vault/v3` (current) and `decoy-vault/v2` (read-only, kept): each a full
    `load_vault` round-trip plus a tamper bite-test (verifies the guard actually fires on a
    corrupted artifact). The v2 file is never deleted or regenerated; `regenerate.py` carries its
    manifest entry forward.
  - `distribution-snapshot/v1`: full `load_spec` round-trip for each reader branch
    (numeric, categorical, conditioned-joint) plus a `schema_version` tamper
    bite-test.

  Intentionally **not** in corpus scope for now: masked CSV/Parquet output (the
  engine has no owned reader for its own masked output; freezing it would only
  retest pandas/pyarrow), and plan YAML / profile JSON (real cross-version readers
  exist, but these are in-process artifacts today; add them once the platform
  persists plans/profiles to disk for cross-version reuse).

  The corpus is synthetic pre-GA: artifacts carry `synthetic: true` and
  `produced_by_engine_version: "0.1.0"`. At GA, replace or supplement with a real
  (`synthetic: false`) artifact of each read-back kind.
- **`decoy-resume-run/v1` and `decoy-resume-table/v1` samples** (`tests/integration/compat_corpus/resume/`).
  Hand-authored canonical bytes with no `manifest.json` entry, like the run-result fixtures.
  `tests/unit/delivery/resume/test_records.py` decodes each and re-encodes it to the same bytes (the files end in one newline that the test strips).
- **`decoy-run-result/v1` fixtures** (`tests/integration/compat_corpus/run_result/run-result-v1*.json`).
  They live in the corpus directory, in their own subfolder, so the frozen samples sit in one place.
  They are hand-authored reader samples, not artifacts minted by `regenerate.py`: there is no
  producing code to regenerate and they have no entry in `manifest.json`. The manifest-driven corpus
  test does not read them. `tests/unit/run_result/test_model.py` reads the sample and the cross-minor
  variants through `RunResultRead` and refuses them on the write model.
- **`CHANGELOG.md`** records every user-visible change.
- **The regression-gate** runs the above on every PR.

## 7. Versioning policy and the 0.x wrinkle

Per-package independent semver (`decoy-engine` and `decoy` cut at `0.2.0`;
`decoy-platform` stays `0.1.x`). The CLI pins the engine it ships with; the
engine publishes before the CLI.

**The wrinkle:** under semver, a `0.x` *minor* bump is technically allowed to
break. Users do not read our semver philosophy; they just get broken.
**Decision: freeze the data contracts (§3.1 artifacts, §3.2 vault, §3.3
determinism) at 1.0-grade the day we have a real user, even while the Python API
remains 0.x-fluid.** The API can churn behind deprecation shims; the things users
*hold* cannot.

## 8. When you genuinely must break something

Major version bump + a real migration tool (read old format, write new) + a
deprecation window + a loud `CHANGELOG.md` entry. **Never silent.** A break the
user discovers by getting wrong output is a defect in this process, not an
acceptable cost.

## 9. Pre-flight checklist (paste into the PR description)

- [ ] I read this document.
- [ ] My change is additive, OR it follows the §5 decision for a frozen item.
- [ ] No `name/vN` artifact shape changed under its existing tag (or: I minted a
      new version and kept the old reader).
- [ ] No determinism golden baseline changed (or: I made a reviewed,
      version-gated decision, or before GA a dated entry in this document,
      and said so in the PR).
- [ ] Vault read/write is untouched (or: PO-reviewed new-version + permanent old
      reader).
- [ ] No released disguise version was edited in place.
- [ ] Any CLI/API removal goes through a deprecation shim with a `CHANGELOG`
      entry.
- [ ] The cross-version compatibility corpus still passes.
- [ ] If I touched `run_mask_chunked` or the oracle preflight it shares with
      `run_mask_pipeline_chunked`: both entry points still raise the same error
      for the same rejected config before any chunk beyond the first is read, and
      the pinned output types (§3.4) did not change.
- [ ] If I touched passthrough handling on `run_mask_chunked`: the exception list
      in §3.4 (what a carried column returns that the oracle alters or refuses)
      and the read-set rule still match the code, and the public oracle and
      `run_native_or_oracle_chunked` still convert every passthrough column.

---

## Pre-GA corpus action item

The cross-version compatibility corpus (§6) exists and runs in CI, covering
`decoy-vault/v2`, `decoy-vault/v3` and `distribution-snapshot/v1`. Before GA: capture a real
(`synthetic: false`) artifact of each read-back kind at the `0.2.0` cut and
add it alongside the existing synthetic fixtures. The corpus cannot be
retrofitted after users hold artifacts we no longer have fixtures for.

## Cross-references

- Engineering best practices §8 (pre-GA/post-GA), §3.3 (library stays narrow):
  `decoy-platform/docs/guides/engineering-best-practices.md`
- Determinism contract: `decoy-engine/docs/determinism.md`
- Roadmap versioning lock: `decoy/docs/ROADMAP.md` section 5.5, the versioning freeze at V1
  (https://github.com/decoy-data/decoy/blob/main/docs/ROADMAP.md)
- This document should be linked from each repo's `CONTRIBUTING.md` and named as
  a required pre-read in the PR template.
