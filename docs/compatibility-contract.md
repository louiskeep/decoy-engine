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

**Pre-GA correction (main health, 2026-10-31).** `RESERVED_NAMES` of the `when` closed grammar gains `type`
(additive to the list, breaking for a config whose `when:` references a column named `type`, which now fails
with `when_outside_closed_grammar`). It is listed on every interpreter rather than derived from
`keyword.softkwlist`, so a config validates identically on every supported Python. The list is frozen
on purpose; after GA any further addition is a breaking change.

**Pre-GA correction (SUB-STOP, 2026-11-05).** A config with `subset:` set is refused by every run entry
(`run_pipeline`, `run_pipeline_isolated`, `run_mask_pipeline_chunked`, `run_mask_chunked`, `run_native_or_oracle_chunked`) and by `compile_plan`,
with `config.subset_run_unsupported` (registry 1.6.0). Before, the block validated and the run silently masked
the full sources. `plan_subset` and `run_subset` are unchanged; run them first and point `sources:` at their output.

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
distribution-snapshot/v1   decoy-vault/v2        vault-key/v1
ff1-key/v1                 quality-report/v1     synth-report/v1
quality-diagnostic/v1      quality-fidelity/v1   quality-policy/v1
quality-shape-fidelity/v1  post-scan-integrity/v2    name-hints/v1
ssn/v1  npi/v1  pan/v1  iban/v1  ein/v1  mrn/v1  ndc/v1  icd10/v1
cusip/v1  address/v1  locality/v1  person/v1  provider/v1
composite/v1  custom/v1   ... (every disguise spec)
subset-manifest/v1         decoy-run-result/v1
scan-set/v1
```

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

`scan-set/v1` is the record of one multi-table scan (`decoy_engine.scan_sources`). The engine
owns its shape: `ScanSetCore` to write (unknown fields refused), `ScanSetCoreRead` to read
(unknown fields ignored). It holds counts, rates, type labels, detector ids and column names.
Column names are the producer's header text; when the scan's header guard flags a header row
that looks like data, the core writes positional names (`c0..cn`) instead. A misdeclared header
the guard cannot recognise (a person name, an address) is the stated limit. The core holds no
cell value. The optional per-table `detail` member of `scan_set_to_dict(detail=True)` is
the nested `ColumnProfile` dict; it is additive-only within `/v1` and is not part of the core.

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

`decoy-vault/v2` + `vault-key/v1` back re-identification. If a new engine version
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

### 3.3 The determinism guarantee (the silent one)

Backed by `docs/determinism.md`, the `determinism/` module, the
`validation/post/_checks/_determinism_sample.py` post-check, and the golden
suites (`tests/snapshots/golden/mask_faker_seeded`,
`tests/integration/golden/test_determinism_invariants.py`).

The guarantee: **same input + same seed → byte-identical output, forever, within
a major version.** A user who masked with one version and later masks *new rows*
with an update — expecting the new rows to join to the old output (same person →
same pseudonym across runs) — is relying on the derivation (HKDF-SHA256 `derive`,
FPE keying, Faker seeding) being unchanged. Change any of it and you break that
user with **zero error message.**

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

The vault format is now `decoy-vault/v2` (F13, 2026-06-26). The v2 file stamps
`SEED_PROTOCOL_VERSION` in an unencrypted header; `load_vault` reads that header
before any decryption attempt and raises a typed
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

### 3.4 The public API + CLI contract

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
  string-redact and native-admissible deterministic categorical columns, the source type for passthrough), no pandas
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
  (1 to 1024; the kernel thread budget of the auto-chunk dispatcher lane, no output
  byte depends on it) and `chunked_dispatcher_enabled: bool = True` (the kill switch that
  restores the previous auto-chunk lane). This is an owner-approved pre-GA
  output-contract cutover: on the auto-chunk route masked values are route-neutral
  (equal to the full-frame route's), while schema, passthrough types, field nullability,
  field metadata, the native Faker all-null type and schema metadata follow the
  dispatcher's contract (passthrough columns are the source column exactly, string-output
  columns are `string`, no schema metadata) and may differ from the full-frame and
  unified-slice routes. Converging those routes is roadmap item ROUTE-OUTPUT-CONTRACT.
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
- **CLI:** verb names, flag names, and the exit-code contract (0 ok, 1
  validation/usage, 2 deprecated-shim, 3 runtime).
- **Config:** the `pipeline.yaml` schema. An old config must keep validating and
  running, or be deprecated through §4.4.

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

### 3.5 Disguises

Disguises are dated/versioned specs (`disguises/`, `disguises/loader.py`,
`disguises/schema.py`), with a drift-guard test. A config pins a disguise
version so its output stays stable.

**Rule:** **never edit a released disguise version.** Add a new dated version.
The drift-guard test enforces this; do not "fix" a shipped disguise in place.

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

- **Golden / determinism snapshots** (`tests/snapshots/golden`,
  `tests/integration/golden/test_determinism_invariants.py`) catch **output
  drift**. A baseline change is a red flag, not a refresh chore.
- **The cross-version compatibility corpus** (`tests/integration/compat_corpus/`).
  Golden tests regenerate artifacts with *current* code, so they do **not** catch
  *format* drift. The corpus freezes synthetic artifacts produced at a known
  engine version and verifies the *current* engine can read and round-trip every
  one. It currently covers two read-back artifact kinds:

  - `decoy-vault/v2`: full `load_vault` round-trip plus a schema-tamper bite-test
    (verifies the guard actually fires on a corrupted artifact).
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
      version-gated decision and said so in the PR).
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
`decoy-vault/v2` and `distribution-snapshot/v1`. Before GA: capture a real
(`synthetic: false`) artifact of each read-back kind at the `0.2.0` cut and
add it alongside the existing synthetic fixtures. The corpus cannot be
retrofitted after users hold artifacts we no longer have fixtures for.

## Cross-references

- Engineering best practices §8 (pre-GA/post-GA), §3.3 (library stays narrow):
  `decoy-platform/docs/guides/engineering-best-practices.md`
- Determinism contract: `decoy-engine/docs/determinism.md`
- Roadmap versioning lock: `decoy/docs/ROADMAP.md` section 5.5, the versioning freeze at V1
  (https://github.com/louiskeep/decoy/blob/main/docs/ROADMAP.md)
- This document should be linked from each repo's `CONTRIBUTING.md` and named as
  a required pre-read in the PR template.
