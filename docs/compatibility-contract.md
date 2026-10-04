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
quality-shape-fidelity/v1  storm-post-mask/v1    name-hints/v1
ssn/v1  npi/v1  pan/v1  iban/v1  ein/v1  mrn/v1  ndc/v1  icd10/v1
cusip/v1  address/v1  locality/v1  person/v1  provider/v1
composite/v1  custom/v1   ... (every disguise spec)
subset-manifest/v1
```

A user runs `decoy fit`, keeps the `distribution-snapshot/v1` artifact, and
feeds it to a later engine version. That artifact is a **contract**.

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
default, unchanged behavior for every existing config); it is part of the
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

### 3.4 The public API + CLI contract

- **Python:** the symbols re-exported from `decoy_engine/__init__.py` and
  `decoy_engine/sdk.py`, with their signatures and output-affecting defaults.
  As of 2026-09-30 this includes `read_fixed_width` and `FixedWidthParseError`
  (A5a): a `format: fixed_width` `FileSource` can now be read through these
  names instead of importing the private `decoy_engine.profile._fixed_width_reader`
  module, which the additive-only rule in §4.1 does not cover.
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
  and out-of-core (C1b-iv) still decline it. A deterministic one
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
- **CLI:** verb names, flag names, and the exit-code contract (0 ok, 1
  validation/usage, 2 deprecated-shim, 3 runtime).
- **Config:** the `pipeline.yaml` schema. An old config must keep validating and
  running, or be deprecated through §4.4.

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

The `storm scan` → `storm analyze` rename is the template. To retire or change a
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
- Roadmap versioning lock: `decoy-platform/docs/ROADMAP.md` ("Versioning")
- This document should be linked from each repo's `CONTRIBUTING.md` and named as
  a required pre-read in the PR template.
