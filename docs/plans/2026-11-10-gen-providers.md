# GEN-PROVIDERS: generate columns from the masking provider registry, with coherent person and address records

Status: plan

Rules consulted: 00-universal, risk-and-exceptions, development-loop, code-review, testing,
architecture, feature-dev, cross-cutting-concerns, api-and-compatibility, exploration, reuse-first,
security, observability-and-resilience, documentation, verification, profiles/local-operations,
catalogs/recurring-defects

Base: engine main `1155b20c`. Roadmap row 24p (CLI repo `docs/ROADMAP.md:260`). Requirements from
the CLI record `docs/records/2026-11-08-generation-completeness-audit.md`. That record has no
numbered "Decisions" section. This plan reads "Decision 1" as its finding B1 (domain identifiers are
unreachable from generation) and "Decision 2" as B2 (coherent person and address records). Its
section 5 groups the two as priority 1. Engine only. CLI help picks up the new type and keys from
`generate_option_catalog()` (GEN-CLI, row 24o).

Risk: R2. The work adds a generate type, config keys and reason codes. It refactors how masking
builds composite generators, without changing behavior. Required evidence is the reviewed plan,
slice T0 and T1, independent T2 with the named mutations, and the trigger-bound full suite per slice.
The owner is the approver. Dennis and Codex are the independent plan gates.

**Prerequisites.**
- E1 waits for GEN-STRICT E2 (row 24k, plan `docs/plans/2026-11-09-gen-strict.md` on
  `plan/gen-strict`, engine PR #465, branch `feat/gen-strict-e2`, head `4b1d1efb`, OPEN on
  2026-11-10). E1 needs the `plan/checks/` package and `check_generate_columns(config, *,
  provider_snapshot=None)` in `plan/checks/_generate_columns.py`. That function runs at validate
  (`run_config_only_checks`) and at compile (`compile_plan`). E1 adds one `elif kind ==
  "provider"` to its `_check_column` (`_generate_columns.py:106` on `4b1d1efb`) and one
  per-table call. E1 also reuses its fixed-text `_refusal(code, table, column, key)`
  (`_generate_columns.py:56`). GEN-STRICT section 12 already names rows 24p and 24q as the
  extenders of its faker check. E1 does not start until #465 merges. If review changes the module
  shape, E1 follows the merged shape and adds no second check entry point.
- GEN-STRICT E0, E1 and E3 are on main: the option registry `config/_generate_options.py`, the
  strict-option codes, and `generation_budget_exceeded`.
- GEN-TYPES (plan `docs/plans/2026-11-10-gen-types.md` on `plan/gen-types`) creates the
  `generation/kinds/` sub-package. GEN-PROVIDERS puts its modules there. If GEN-TYPES E1 has not
  merged, E1 creates `generation/kinds/__init__.py` (a docstring-only module) and GEN-TYPES
  rebases onto it. Whichever slice merges first owns the file. No other ordering is needed.
- GEN-INHERIT (plan on `plan/gen-inherit`) and GEN-TYPES also add lines to `synthesize.py`
  (571 lines on `1155b20c`) and bump the proof-manifest generate count. This plan adds at most 10
  lines to `synthesize.py`. See Placement for the over-600 rule.
- Line numbers are from `1155b20c` unless marked `4b1d1efb`. They will move.

**Guarantee.** A generate column can draw from any provider that masking can use, by the same name,
through the same adapter and kernel code. This covers the 9 `decoy_native` identifiers
(`synthetic_ssn`, `synthetic_ein`, `synthetic_npi`, `synthetic_ndc`, `synthetic_mrn`,
`synthetic_pan`, `synthetic_icd10`, `synthetic_iban`, `synthetic_cusip`) and the 19 Faker-bound
names. The composites `composite_person`, `composite_name_email`, `composite_city_state_zip`,
`composite_address` and `composite_provider` give coherent rows across several columns: the email
is built from the same row's first and last name, and the ZIP belongs to the same row's state.
`composite_person` can also emit `sex` (the first name is drawn from that sex's name list) and
`age` (whole years from the same row's date of birth). An unknown provider name fails at
`decoy validate` with a fixed-text code. Output is reproducible for a fixed config and seed.

**Non-goals:** a second provider table, list of names or copy of any kernel; `unique: true` on
provider columns (V2-LOG); per-provider options such as the MRN prefix or the email format
(V2-LOG); `composite_custom` in generation; the pinned route; streamed admission; locales beyond
each provider's declared `supported_locales`; any change to masking output bytes.

**Design notes.**
- One new generate type, `provider`, with keys `provider`, `locale`, `field` and `group`.
- Choke point: the shared registry `providers_v2.get_default_registry()`
  (`providers_v2/_registry.py:146`) is the only source of provider names, adapters and
  capabilities. The checks, the draw and the tests read it. Guard: AT-2 parametrizes over the live
  `known_providers()`, and AT-4 scans the new modules for kernel tokens and private imports.
  Mutation: replace the registry read with a fixed tuple, or construct `NpiAdapter()` directly.
- Open/closed: the draw path for each provider follows from its `CapabilityMatrix`
  (`backend_type`, `poolable`), never from a hand list. A new masking provider with a known backend
  works in generation with no edit here.
- Parallel tables: the four lists of composite names (`_COMPOSITE_NAMES`,
  `_COMPOSITE_OUTPUT_COLUMNS`, the registry loop and the masking handler's if/elif chain) become
  one descriptor table that both masking and generation read. Guard: AT-13. Mutation: add a
  descriptor with no registry binding.
- Established pattern: GP2's generation pool bridge (`generation/_faker_pool.py`) and masking's
  `CompositeHandler`. Each new fact (provider names, slot names, seed labels) lives in one place.

Recurring-defect catalog check:
- "second registry": no provider list; the four composite name lists fold into one (AT-2, AT-13).
- "copied kernel": the generation modules call adapters and composite generators only (AT-4).
- "value in error text" and "no exception context": every refusal uses fixed text. Adapter errors
  are translated by a raise placed after the `except` block closes, so the new error has no
  `__cause__` or `__context__` (AT-5, AT-16).
- "route forgot the new branch": pinned refuses and streamed declines, both explicit (AT-9).
- "shared RNG stream": the null mask keeps its own `np` stream (AT-8).
- "option read outside the registry": `READER_FILES` gains the new modules (AT-17).

## Why

The audit found that generation cannot produce a valid NPI, MRN, ICD-10 code, NDC, PAN, IBAN,
CUSIP, EIN or SSN, although masking ships kernels for all nine. A user who wants a synthetic
claims table has to pick "close enough" Faker methods with no check digits. It also found that
generated people are not coherent: `faker: first_name` and `faker: email` are drawn
independently, so `mary.smith@` sits beside "John Ortiz", and a ZIP has no link to its state.
Masking already solves both problems with the provider registry and the composite generators.
Generation should call that code, not grow its own.

## Behavior

### E1: scalar providers

Config shape:

```yaml
generate:
  tables:
    providers:
      rows: 1000
      columns:
        - {name: npi, type: provider, provider: synthetic_npi}
        - {name: mrn, type: provider, provider: synthetic_mrn, null_probability: 0.05}
        - {name: phone, type: provider, provider: person_phone, locale: en_US}
```

1. **Type and keys.** `provider` joins the `GenerateColumnConfig.type` Literal and
   `GENERATE_TYPES` (`config/_tables.py:336`). Its `GENERATE_OPTIONS` entry
   (`config/_generate_options.py:230`) is `TypeOptions(keys={provider, locale, field, group},
   required={provider})`. All four are strings. In E1, a `field` or `group` refuses with
   `plan.generate_provider_field_unexpected`, because no E1 provider has slots. `COMMON_OPTIONS`
   (`:219`) apply unchanged. `unique` stays refused by `config/_generate_unique.py:55`, because
   `provider` is not in `UNIQUE_TYPES`.
2. **Validate and compile.** A new `plan/checks/_generate_providers.py` holds
   `check_provider_column(table, col)` and `check_provider_groups(table, columns)`. GEN-STRICT's
   `_check_column` calls the first. `check_generate_columns` calls the second once per table. In
   order:
   - `provider` not in `get_default_registry().known_providers()` refuses with
     `plan.generate_provider_unknown`. The name is not quoted.
   - `provider` is `composite_custom`, or its `backend_type` has no draw path (step 5), refuses
     with `plan.generate_provider_unsupported`.
   - A composite name without `field` refuses with `plan.generate_provider_field_required`. In
     E1 every composite refuses this way. E2 lifts it.
   - `locale` given and not in the provider's `CapabilityMatrix.supported_locales` refuses with
     `plan.generate_provider_locale_unsupported`.
   Each refusal is `_refusal(code, table, column, key)`, which carries the table, the column and
   the config key. It has no value text.
3. **Faker hint.** GEN-STRICT's `_FakerView.check` refuses an unknown `faker_type` with
   `plan.generate_faker_type_unknown`. When that unknown name is a registry name
   (`synthetic_npi`), E1 raises `plan.generate_faker_type_is_domain_provider` instead. Its fixed
   message says to use `type: provider`. The check is one membership test against
   `known_providers()` before the generic refusal.
4. **Seed.** The column root is `GenDeriveContext.for_column(derive_key, col, fallback_seed)`, as
   for every generate column. Its fingerprint is `strategy_config_fingerprint` over the config
   less `_EXCLUDED_FROM_FINGERPRINT`, and its identity is (table, column). The provider draw uses
   the column's `"faker"` family from `GEN_FAMILIES` (`generators/derivation.py:191`) as
   `K = gen_ctx.family_bytes("faker")` (32 bytes, `generators/derivation.py:305`). Why `faker`: a
   provider column is the registry-backed sibling of a faker column and draws from the same kind
   of value source. A column has one type, so no other draw on a provider column reads that
   family. The null mask keeps `base_int("np")` in `_apply_null_probability`
   (`generation/synthesize.py:517`) and does not share K. Pool paths take GP2's two existing
   labels, `family_bytes("faker_pool_build")[:8]` and `family_bytes("faker_pool_selection")[:8]`,
   exactly as `generation/_faker_pool.py:432` does. No new family label is added.
5. **Draw, by capability.** A new `generation/kinds/_provider.py` holds
   `provider_generate(col, n, gen_ctx, registry, locale)`. It reads
   `caps = registry.get_capabilities(p)` and picks one of three paths:
   - `backend_type` is `faker` or `mimesis` and `poolable`: masking's pool seam.
     `PoolBuilder(registry).build(p, size=DEFAULT_POOL_SIZE, job_seed=build_seed, locale=loc)`
     (`generation/pool/_builder.py:166`), then `PoolSampler().sample(pool, n, mode=REUSE,
     seed=selection_seed, deterministic=False)` (`generation/pool/_sampler.py:200`). This is
     GP2's pattern, through the registry instead of raw Faker reflection.
   - `backend_type` is `decoy_native` (the 9 identifiers, all `poolable=False`): per row
     `adapter.generate(p, spec=ProviderSpec(locale=loc, deterministic=True, namespace=ns, seed=K),
     source_value=str(i))`. Inside the adapter this reaches the kernel's
     `derive_value(seed, namespace, canonical, domain=...)`, the same keyed draw masking uses (for
     example `providers_v2/identifiers/_npi.py:107`). The row ordinal is the source value, which
     is masking's positional pattern (`execution/_strategies/_faker_positional.py:52`). `ns` is
     the fixed string `gen-provider/<table>/<column>`. The unseeded `generate_batch`
     (`providers_v2/identifiers/_npi.py:141`) is never called, because it uses
     `np.random.default_rng()` with no seed.
   - `backend_type` is `faker` and not `poolable` (`uuid`, `lorem_text`, `random_int_range`,
     `random_choice`, `address_full`): `adapter.generate_batch(p, spec=ProviderSpec(locale=loc,
     seed=build_seed), count=n)`. That seeds a fresh Faker instance
     (`providers_v2/_faker_adapter.py:194`), so the batch is reproducible. It is sequential.
   Any other `backend_type` is refused at compile (step 2). That is the only closed branch.
6. **Locale.** The `locale` key, else the instance default locale passed to `_generate_column`,
   else `en_US`. A default locale that the provider does not declare refuses at run with
   `runtime.generation_provider_failed`, because compile cannot see the instance default.
7. **Run-time errors.** `ProviderError`, `CompositeError`, `IdentifierError`
   (`providers_v2/identifiers/_errors.py:17`) and `DeterminismError`
   (`determinism/_derive.py:148`) raised inside the draw are caught at one site in
   `provider_generate`. The `except` block only records a flag. The new
   `GenerationError(code=runtime.generation_provider_failed)`, with fixed text naming the table and
   column, is raised after the block closes. `raise ... from None` is not enough: it clears
   `__cause__` but keeps `__context__`, and AT-5 and AT-16 require both to be `None`. Adapter
   messages can quote the provider name or a value, so they are never forwarded.
   Every draw path here is integer-only: HMAC-SHA256, integer modulo, and Faker's or numpy's
   integer index draws. None uses libm or floating point, so the bytes match across x86 and
   arm64. A later change that adds a float step to a provider draw breaks AT-3's committed
   digests.
8. **Dispatch.** `_generate_column` (`generation/synthesize.py:292`) gains one
   `elif kind == "provider":` branch (about 4 lines) that calls `provider_generate`.
   `restore_declared` and `_apply_null_probability` then run on the result as for every type.
   `ProviderSpec` is always built with `deterministic` and `namespace` set explicitly on the
   keyed and composite paths, and with `seed` set on every path, so no adapter falls back to an
   unseeded draw.
   Memory: a table with a provider column runs on the legacy resident route (streamed declines,
   pinned refuses). Its memory grows with `n` and is bounded only by the run's row count and the
   existing generation budget, the same as `faker` today.
9. **Registries.**
   - `ADMISSION_MATRIX["provider"] = (False, ...)` (`generation/_admission.py:40`): streamed
     declines and the run falls back to resident.
   - `REJECTED_TYPES` (`generation/_pinned_compile.py:52`) gains `provider` with the new
     `plan.pinned_provider_unsupported`.
   - `READER_KEYS["provider"] = ()` (`generation/_numeric_typing.py:142`): it reads no column.
   - `compute_payload_bounds` (`generation/_bounds.py:174`): None (unbounded), as for `faker`.
     V2-LOG: per-provider width bounds.
   - `_reproducibility._direct_reason` (`generation/_reproducibility.py:70`): unchanged.
     `register_faker_provider_v2` overrides apply inside `FakerAdapter` the same way on every
     route, so there is no route-specific custom-provider reason to report.
10. **Draw sites.** `GEN_KIND_TO_SITE["provider"] = "gen.provider_row_keyed"`
    (`execution/native/_determinism_protocol.py:97`). One new `DrawSite` goes in
    `execution/native/_draw_sites_gen.py`: entropy root `job_seed` through the column's `faker`
    family, identity `row_index`, partitionable, call site `generation/kinds/_provider.py`, mirror
    call sites at the identifier kernels. Its notes say that poolable providers use the existing
    `gen.pool_build_faker` and `gen.pool_nondeterministic` sites, and non-poolable Faker uses
    `gen.pool_build_faker`. One `DrawSiteProvider` goes in
    `execution/native/protocol/providers_gen.py` (322 lines). One entry goes in
    `tests/native/test_draw_site_providers_golden.py`. `PROVIDER_IDENTIFIER_SITES` (`:118`) is
    unchanged. The new module has no raw draw tokens, so the static draw scan in
    `tests/native/test_draw_site_inventory_coverage.py` needs no allowlist change. `_GEN_ONLY`
    (`execution/native/_capabilities.py:388`) gains a python-only `provider` entry shaped like the
    `faker` entry. No Rust kernel is added.
11. **Catalog and docs.** `generate_option_catalog()` (`config/_generate_options.py:415`) lists the
    new type. `docs/capability-matrix.md` and `docs/proof-manifest.json` are regenerated. The
    `surface["generate"] == 12` assertion (`tests/sentry/test_proof_manifest.py:66`) counts types,
    so it moves to 13. It is a count, not a ceiling. Whichever of GEN-TYPES, GEN-INHERIT and
    GEN-PROVIDERS merges later adds its own increment.

### E2: composite groups

Config shape:

```yaml
columns:
  - {name: first_name, type: provider, provider: composite_person, field: first_name}
  - {name: last_name,  type: provider, provider: composite_person, field: last_name}
  - {name: email,      type: provider, provider: composite_person, field: email}
  - {name: g_first,    type: provider, provider: composite_person, field: first_name, group: guardian}
  - {name: city,  type: provider, provider: composite_city_state_zip, field: city}
  - {name: state, type: provider, provider: composite_city_state_zip, field: state}
  - {name: zip,   type: provider, provider: composite_city_state_zip, field: zip}
```

1. **Factory table (behavior-preserving masking refactor).** The refactor is two commits.
   - Commit 1 adds `tests/unit/execution/test_composite_handler_digests.py` and nothing else. It
     runs each of the six masking composites through `CompositeHandler` on a fixed frame, in
     deterministic mode (a fixed `mask_key`) and in non-deterministic mode (a fixed seed), and
     asserts a SHA-256 digest of every output column. The digests are captured on the base commit
     and committed as constants. No byte golden for these composites exists today:
     `tests/integration/golden/test_execution_e2e.py` checks coherence, not bytes.
   - Commit 2 is the refactor. The digest test must pass unchanged.
   A new `generation/composite/_factory.py` holds `COMPOSITES: Mapping[str, CompositeDescriptor]`,
   exported from `generation/composite/__init__.py`. Callers import it from
   `decoy_engine.generation.composite`, never from `_factory`. Each descriptor has a factory, the
   canonical output columns and the optional output columns (empty until E3). Every factory has
   the one signature `(coherent_namespace, registry, provider_config) -> CompositeGenerator`.
   Factories that do not need an argument ignore it (`composite_city_state_zip` takes no
   registry today). The `composite_custom` factory reads `provider_config["bundle"]`. The bundle
   shape check and its `ExecutionError(COMPOSITE_CUSTOM_BUNDLE_SHAPE)` stay in the handler
   (`execution/_strategies/_composite.py:87-110`), before the factory call, because `generation/`
   must not raise execution errors. These derive from the table:
   - `_COMPOSITE_NAMES` (`generation/composite/_generator.py:31`);
   - `_COMPOSITE_OUTPUT_COLUMNS` and `_VARIABLE_OUTPUT_COMPOSITES`
     (`generation/composite/_validate.py:47`, `:58`);
   - the registry's composite loop (`providers_v2/_registry.py:245`);
   - the `CompositeHandler` if/elif chain (`execution/_strategies/_composite.py:63`).
   - the masking-side name lists `execution/_column_access.py:279`, `validators/_leak_check.py:95`
     and the name constants in `validation/post/_checks/_composite_coherence.py:30`.
   One registry per concern: `_column_access.py` and `_leak_check.py` read their name sets from
   `COMPOSITES` where the import-linter layer contract allows it. Where it does not, they keep
   their literal and the set guard below pins it. The coherence check keeps its per-composite
   checker functions, because each composite's rule differs. The guard asserts that every
   `COMPOSITES` key other than `composite_custom` has a checker.
   The per-composite constructor arguments the handler passes today move into each descriptor's
   factory, so the generators masking builds are unchanged. The set guard asserts that the
   registry's composite names, the `COMPOSITES` keys and every list above are one set. The commit 1
   digest test stays byte-identical.
2. **Fields.** On a composite provider, `field` is required. It must be one of that descriptor's
   output columns, or else `plan.generate_provider_field_unknown`. On a scalar provider, `field`
   refuses with `plan.generate_provider_field_unexpected`. `group` defaults to the provider name.
3. **Groups.** Members are the provider columns of one table with the same (provider, group). In
   `check_provider_groups`:
   - all members must have the same effective `locale`, or else
     `plan.generate_provider_group_mismatch`;
   - each `field` appears at most once per group, or else
     `plan.generate_provider_field_duplicate`.
   A table can hold two people by giving two groups.
4. **Group seed.** The group root is `GenDeriveContext.for_column(derive_key, group_cfg,
   fallback_seed)`. `group_cfg` is `{name: "<provider-group:" + group + ">", type: provider,
   provider: p, locale: loc}` with the table set. Members carry `field` and the group config does
   not, so its fingerprint differs from every member's. No real column can match its identity and
   fingerprint, because a composite column without `field` is refused. The bundle uses
   `K_g = group_ctx.family_bytes("faker")` and namespace `gen-provider/<table>/<group>`. Member
   order and member names do not reach the group seed, so reordering or renaming a member leaves
   the values the same.
5. **Draw.** `generation/kinds/_provider_group.py` calls `COMPOSITES[p].factory(...)` and then
   `generate_bundle(ProviderSpec(locale=loc, deterministic=True, namespace=ns_g, seed=K_g), n,
   source=pd.Series(range(n)).astype(str), deterministic=True)`. Masking's handler makes the same
   call. For `composite_person` that is one `derive` per row split into four index slots
   (`generation/composite/_person.py:171`), so combinations are near-unique, not capped at a
   10,000-tuple pool. The bundle is computed once per (table, group) and held in a per-table
   `bundle_cache` dict that the table loop creates and passes to `_generate_column`. The first
   member computes it. Each member takes its column from the bundle, then runs `restore_declared`
   and its own `_apply_null_probability`. So a member's nulls are independent of other members,
   as in masking.
6. **Run-time errors.** `CompositeError` follows E1 step 7. The `composite_custom` bundle-shape
   check is unreachable from generation, because generation refuses `composite_custom` at compile.
7. **Dispatch.** The `provider` branch passes `bundle_cache` when `field` is present (about 3
   more lines). The table loop in `_generate_tables_from_config` creates one dict per table (about
   2 lines). The pinned route never reaches here.

### E3: `sex` and `age` on `composite_person`

1. `CompositePerson` gains a constructor argument, `optional_fields: frozenset[str] =
   frozenset()`. Masking never passes it, so masking bytes and masking wiring
   (`composite_wiring_consistent`) do not change. The descriptor's optional columns for
   `composite_person` become `(age, sex)`. Generation passes the subset its group requests.
2. **sex.** When `sex` is requested, each row does a second derive on `coherent_namespace +
   "/ext"`. `generation/composite/_custom.py` has the same precedent for a fifth slot. The low bit
   of byte 0 picks `F` or `M`. The first name then comes from a female or male pool, indexed by the
   main latent's slot 0. The two pools come from two new registry entries,
   `person_first_name_female` and `person_first_name_male`. They map to Faker's
   `first_name_female` and `first_name_male` in `_FAKER_METHOD_MAP`
   (`providers_v2/_faker_adapter.py:43`) and `_CATALOG` (`providers_v2/_real_registry.py:67`).
   The registry grows from 34 to 36 names. The change is additive, and masking can use the new
   names too. The count pins that move in E3:
   - `tests/sentry/test_proof_manifest.py:67`;
   - `tests/unit/providers_v2/test_registry.py:43`;
   - `tests/unit/providers_v2/test_capability_matrix.py:19` (Faker-bound 19 to 21) and `:29`;
   - `tests/unit/providers_v2/mimesis/test_optional_dep.py:28` and `:92`;
   - the count docstrings in `providers_v2/_registry.py` and `faker_providers()`;
   - `docs/decisions/mimesis-adoption.md:51`.
   These are counts, not ceilings. The platform's provider picker reads `faker_providers()`, so it
   gains two entries. Faker 40.23.0 en_US has 13 names in both lists (for example Jordan, Taylor), so the
   name alone cannot fix the sex. Drawing sex first is the only coherent order.
3. **age.** When `age` is requested, it is computed per row from that row's `dob` to `DOB_ANCHOR`
   (`generation/_dob_anchor.py:28`), in whole years with the birthday rule. It is a pure function
   with no draw, and it never reads the wall clock. `dob` need not be emitted for `age` to work.
4. When neither field is requested, the person bundle is byte-identical to E2.

## Pre-mortem (failure modes)

| # | Failure | Where it would bite | Guard |
|---|---|---|---|
| P1 | A builder copies an identifier kernel or a Luhn helper into generation | two NPI implementations drift; masking fixes miss generation | AT-4 token and import scan on `generation/kinds/_provider*.py`; reuse inventory names the adapters |
| P2 | A hand list of "generatable" providers grows beside the registry | a new masking provider is silently missing from generation | AT-2 parametrizes over the live `known_providers()`; the branch choice reads the capability matrix |
| P3 | The identifier path calls the unseeded `generate_batch` | output changes every run | AT-3 two-run and two-process byte equality per identifier; mutant calls `generate_batch` |
| P4 | Unknown provider caught only at run | `decoy validate` passes a bad config | check in `check_generate_columns`, which validate calls; AT-5 runs `run_config_only_checks` |
| P5 | `ProviderError` text, which quotes the name and the known list, reaches the user | provider or value text in a refusal | `from None` translation at one site; AT-5 and AT-16 sentinel checks |
| P6 | The null mask draws from the provider stream, or the provider path advances `np` | null positions or values move when the other setting changes | K comes from `faker`, nulls from `np` base_int; AT-8 |
| P7 | Each composite member computes its own bundle | email does not match the name on the same row | one bundle per (table, group) via `bundle_cache`; AT-10 mutant |
| P8 | The group seed depends on the first member's config or order | reordering columns changes people | group root from `group_cfg` with no `field` or member name; AT-11 reorder cell |
| P9 | The factory refactor changes a masking constructor argument | masking output bytes change | commit 1 adds per-composite value digests through `CompositeHandler`, captured on base, for both modes; commit 2 refactors with them unchanged; AT-13 |
| P10 | `sex` is added to masking's canonical columns | masking wiring check refuses existing configs, or bytes change | optional columns are separate from canonical ones and masking never requests them; AT-14 masking golden cell |
| P11 | `age` reads `date.today()` | output changes daily | `DOB_ANCHOR` only; AT-15 mutant |
| P12 | Pinned or streamed silently half-supports `provider` | route divergence | explicit `REJECTED_TYPES` and `ADMISSION_MATRIX` entries; AT-9 |
| P13 | The new module reads a key not in the registry (for example `provider_options`) | silently ignored key | `READER_FILES` entry; AT-17 |
| P14 | `synthesize.py` crosses 600 lines after GEN-TYPES and GEN-INHERIT also land | module size census entry | dispatch is at most 10 lines; the bundle-cache plumbing moves into `kinds/_provider_group.py` if needed; Placement rule |
| P15 | The identifier draw is too slow | large tables stall | floor of 20k rows/s per column at 100k rows, measured at T2; Dennis measured NPI 59k, IBAN 53k, MRN 70k and `composite_person` 26k rows/s per group on x86; below the floor blocks the slice |
| P16 | A float or libm step enters a draw path | bytes differ between x86 and arm64 | integer-only paths stated in E1 step 7; AT-3 committed digests run on the Mac (arm64) and a Linux VM at T2 |

## Acceptance tests

| AT | What | Red on base / mutant |
|---|---|---|
| AT-1 | `provider` is in `GENERATE_TYPES`, the option catalog, `ADMISSION_MATRIX` (declined), `REJECTED_TYPES`, `READER_KEYS`, `GEN_KIND_TO_SITE`, `_GEN_ONLY`, the capability matrix and the proof manifest | red on base |
| AT-2 | For every name in the live `known_providers()` except composites, a 1,000-row column generates with no nulls. Each value passes that provider's `format_regex` and `blocklist_validators` from its `CapabilityMatrix` (NPI and PAN Luhn, IBAN mod 97, CUSIP check digit, SSN blocklist) | red on base; mutants: skip `decoy_native` in the branch choice; replace the registry read with a fixed tuple missing one name |
| AT-3 | Determinism: the same config and seed in two runs, and in two subprocesses with different `PYTHONHASHSEED`, give identical bytes for each backend path. A different seed changes the output. Adding an unrelated column does not change it. Committed SHA-256 digest constants pin one column per backend path: keyed identifier (`synthetic_npi`), Faker pool REUSE (`person_email`), seeded `generate_batch` (`uuid`), and `composite_person` deterministic (E2 adds that cell). T2 runs the digest cells on the Mac (arm64) and on a Linux VM, as a cross-arch check | red on base; mutants: identifier path calls `generate_batch`; add a float step to the identifier source value |
| AT-4 | Reuse sentry: `generation/kinds/_provider.py` and `_provider_group.py` contain none of `derive(`, `derive_value(`, `default_rng(`, `Faker(`, `seed_instance(`, and import nothing from `providers_v2.identifiers` or private composite modules. Adapters come only from `get_default_registry()` and generators only from `COMPOSITES`. The existing static draw scan stays green with an unchanged allowlist | mutants: inline a Luhn digit helper; construct `NpiAdapter()` directly |
| AT-5 | `provider: not_a_provider` refuses at `run_config_only_checks` and at `compile_plan` with `plan.generate_provider_unknown`, located by table, column and key. A sentinel provider name is absent from `str(exc)`, the log and the exception chain; `__cause__` and `__context__` are `None` | red on base; mutant: let the registry `ProviderError` propagate |
| AT-6 | `type: faker, faker_type: synthetic_npi` refuses with `plan.generate_faker_type_is_domain_provider`; `faker_type: not_real` still gives `plan.generate_faker_type_unknown` | red on base; mutant: drop the membership test |
| AT-7 | `locale: fr_FR` on a provider that declares only `en_US` refuses with `plan.generate_provider_locale_unsupported`; `en_US` passes. A run-time default locale outside the declared set gives `runtime.generation_provider_failed` | red on base; mutant: skip the locale check |
| AT-8 | With `null_probability: 0.3`, null positions are the ones `_apply_null_probability` picks from the column's `np` base seed, and the non-null values equal the same column's values at `null_probability: 0` with the same fingerprint inputs | mutant: seed the null mask from `family_bytes("faker")` |
| AT-9 | Pinned compile refuses with `plan.pinned_provider_unsupported`. Streamed admission declines, and the fallback output equals the direct resident output. A `reference` child of a provider column draws only parent values | red on base; mutant: admit streamed |
| AT-10 | E2 coherence at 10,000 rows. `composite_person`: every email's local part is built from that row's first and last name under the default format. `composite_city_state_zip` and `composite_address`: every (city, state, zip) is a row of the locality table. `composite_provider`: every `npi` passes Luhn | red on base; mutant: compute a separate bundle per member |
| AT-11 | Two `composite_person` groups in one table give different people. Members of one group agree row for row. Reordering the members, renaming a member, or adding an unrelated column does not change any value. Two tables with the same group name do not share a bundle | mutants: derive the group root from the first member's config; create the cache once per run |
| AT-12 | E2 refusals, each with its code, location and no value text: composite without `field`; `field` on a scalar; unknown `field`; duplicate `field` in a group; locale mismatch in a group; `composite_custom` | red on base for the E2 codes |
| AT-13 | Commit 1's `CompositeHandler` digest test (six composites, deterministic and non-deterministic, digests captured on base) passes unchanged after commit 2. The `composite_custom` malformed-bundle cell still raises `ExecutionError(COMPOSITE_CUSTOM_BUNDLE_SHAPE)` from the handler. Set guard: the registry's composite names, `COMPOSITES` keys, `_COMPOSITE_NAMES`, `_COMPOSITE_OUTPUT_COLUMNS` keys, and the lists at `execution/_column_access.py:279` and `validators/_leak_check.py:95` are one set; every `COMPOSITES` key except `composite_custom` has a coherence checker | mutants: change one constructor argument in the factory (digest red); add a descriptor without a registry binding (set guard red); move the bundle-shape check into the factory |
| AT-14 | E3 at 20,000 rows: every `F` row's first name is in Faker's `first_names_female`, every `M` row's in `first_names_male`. Each sex is between 45% and 55% of rows. With no optional field requested, masking and E2 person bundles are byte-identical | red on base; mutants: draw the first name from the unisex pool; put `sex` in the canonical columns |
| AT-15 | E3 `age` equals whole years from `dob` to `DOB_ANCHOR` on every row. Direct cells cover a birthday on the anchor day, the day after, and 29 February | red on base; mutants: use `date.today()`; drop the birthday rule |
| AT-16 | A monkeypatched adapter that raises `ProviderError`, `IdentifierError` or `DeterminismError` with a sentinel message gives `GenerationError` with `runtime.generation_provider_failed`. The sentinel is absent from `str(exc)` and the log, and the exception chain is empty. A registry `override` that removes a name between compile and run gives the same code | red on base; mutants: `raise ... from exc`; `raise ... from None` inside the `except` block (keeps `__context__`); skip the run-time catch |
| AT-17 | `tests/sentry/defects/test_generate_option_reads.py` `READER_FILES` lists the new modules and passes | mutant: read `provider_options` in `_provider.py`; the sentry goes red |
| AT-18 | `tests/native/test_draw_site_inventory_coverage.py` passes with `provider` mapped, and the providers golden lists the new `DrawSiteProvider` | mutant: drop the `GEN_KIND_TO_SITE` entry |

## Class table

| # | Class | Where it can bite here | Closing structure | Sentry or property test | Mutation |
|---|---|---|---|---|---|
| 1 | Wrong value | Identifier with a bad check digit; email not matching the row's name; ZIP outside its state; age off by one | Values come only from masking's adapters and composites; one bundle per group; age is a pure function of `dob` | AT-2, AT-10, AT-14, AT-15 | per-member bundle; drop the birthday rule |
| 2 | Cleanup | Bundle cache kept across tables or runs | `bundle_cache` is created per table in the table loop and dropped with it; no files or handles | AT-11 two-table cell | create the cache once per run |
| 3 | State | Shared mutable state: the singleton registry, the person pool cache, the RNG streams | The registry is read-only here; each group builds a fresh generator; K and the null `np` stream are separate HMAC families | AT-3, AT-8 | seed nulls from K; reuse one generator across groups |
| 4 | Route divergence | Pinned or streamed half-supports the type; validate and compile check differently | Explicit pinned refusal and streamed decline; one check module that validate and compile both call | AT-5, AT-9 | admit streamed; check only at compile |
| 5 | Referential integrity | A `reference` to a provider column assumes unique parent values | `unique` stays refused on `provider`; a reference child draws from parent values as for any parent; GEN-REL's uniqueness rule still applies where it is required | AT-9 reference-child cell | allow `unique: true` without a unique draw |
| 6 | Untrusted input | Provider names and adapter messages reach refusal text or logs | Fixed-text `_refusal`; one translation site that raises after the `except` block | AT-5, AT-16 | forward the adapter message; `raise ... from None` inside the block |
| 7 | TOCTOU / check-then-use | Registry checked at compile, changed by `override` or a custom provider before the run | The run reads the same singleton; a missing name at run is caught and translated, not trusted from compile | AT-16 override cell | skip the run-time catch |
| 8 | Weak test | Coherence or format checked on a few rows | AT-2 runs every provider at 1,000 rows; AT-10 and AT-14 check every row at 10,000 and 20,000 | the mutation column | each listed mutation |
| 9 | Refactor drift | The composite factory refactor changes masking generators | Own commit; masking goldens unchanged; guard test on the name sets | AT-13 | change one constructor argument in the factory |
| 10 | Frozen contract | Type Literal, option catalog, reason registry, provider registry names, draw-site catalogue | All additive: one type, four keys, new codes, two new registry names, one new draw site; masking bytes unchanged | AT-1, AT-13, AT-14, reason manifest sentry | rename a code; drop a manifest entry |

## Established solutions and reuse inventory

Search run: the engine tree (`git grep` on `origin/main` for provider, composite, pool and seed
code), the masking strategy handlers, GP2's pool bridge, and the GEN-STRICT, GEN-TYPES and
GEN-INHERIT plans. External patterns: Synthea's person module (one latent person record from
which name, sex, birth date and address are drawn together) and SDV's `FixedCombinations`
constraint (columns that must co-occur are drawn as one tuple). Both match the composite design
masking already ships. Check digits follow their issuing standards (Luhn for NPI and PAN, ISO 13616
mod 97 for IBAN, the CUSIP check digit), which the kernels already implement. No custom kernel is
needed. The only new code is wiring and one pure age function.

| Need | Reused | Source |
|---|---|---|
| provider names and capabilities | `get_default_registry()`, `known_providers`, `get_capabilities`, `get_adapter` | `providers_v2/_registry.py:146`, `:87` |
| identifier kernels | the 9 `decoy_native` adapters' `generate(deterministic=True)` | `providers_v2/identifiers/_npi.py:107` and siblings |
| Faker-bound values | `FakerAdapter` and `_FAKER_METHOD_MAP` | `providers_v2/_faker_adapter.py:87`, `:43` |
| pool build and sampling | `PoolBuilder.build`, `PoolSampler.sample`, `DEFAULT_POOL_SIZE` | `generation/pool/_builder.py:166`, `generation/pool/_sampler.py:200` |
| pool seed labels | GP2 `faker_pool_build` and `faker_pool_selection` | `generation/_faker_pool.py:432` |
| positional keying | row ordinal as the derive source | `execution/_strategies/_faker_positional.py:52` |
| composite generators | `generate_bundle(deterministic=True)` on each composite | `generation/composite/_person.py:215` and siblings |
| fifth-slot precedent | second derive on `coherent_namespace + "/ext"` | `generation/composite/_custom.py` docstring |
| column seed | `GenDeriveContext.for_column`, `family_bytes` | `generators/derivation.py:243`, `:305` |
| option registry and catalog | `GENERATE_OPTIONS`, `TypeOptions`, `generate_option_catalog` | `config/_generate_options.py:230`, `:415` |
| check entry and fixed-text refusal | `check_generate_columns`, `_check_column`, `_refusal` | `plan/checks/_generate_columns.py:79`, `:106`, `:56` on `4b1d1efb` |
| run-time coded refusal | `GenerationError` and the runtime reason registry | `generation/pool/_errors.py`; `reasons/runtime/_part3.py` |
| date of birth anchor | `DOB_ANCHOR` | `generation/_dob_anchor.py:28` |
| established method | one latent per entity, columns drawn as one tuple | Synthea `Person` generation; SDV `FixedCombinations` |

## Enumerations

### Exit paths

- Config refusals from the option registry: a missing `provider` uses GEN-STRICT E1's required-key
  code; an unknown key or a wrong type uses its existing codes (`config/_generate_options.py:230`).
- `generate_unique_unsupported` for `unique: true` (`config/_generate_unique.py:55`).
- Plan refusals from `plan/checks/_generate_providers.py`, at validate and compile:
  `generate_provider_unknown`, `generate_provider_unsupported`, `generate_provider_field_required`,
  `generate_provider_field_unexpected`, `generate_provider_locale_unsupported`, the hint
  `generate_faker_type_is_domain_provider`, and in E2 `generate_provider_field_unknown`,
  `generate_provider_field_duplicate` and `generate_provider_group_mismatch`.
- Pinned compile refusal `plan.pinned_provider_unsupported` (`generation/_pinned_compile.py:52`).
- Streamed admission declines, so the run falls back to resident (`generation/_admission.py:40`).
- Run-time `runtime.generation_provider_failed` from the one translation site.

### Routes

- Resident: `_generate_column` (`generation/synthesize.py:292`), a new `provider` branch beside
  `faker` at `:325`.
- Pinned: refused at compile; never runs.
- Streamed: declined by `ADMISSION_MATRIX`; runs resident.
- Validate (`decoy validate`): `run_config_only_checks` through `check_generate_columns`.

### FK shapes

- A `reference` column whose parent column is `type: provider`: reads the generated values like
  any parent. Values can repeat, because `unique` is refused (V2-LOG).
- A provider column as an FK child: N/A because provider draws read no other table or column
  (`READER_KEYS["provider"] = ()`).

### Fields

- `provider` (required string, a registry name), `locale` (optional string), `field` (E2, a slot
  of the composite), `group` (E2, default the provider name).
- Common options: `null_probability`, `determinism`. `determinism` is excluded from the
  fingerprint but still acts: `determinism: fresh` gives the column an `os.urandom` root
  (`generators/derivation.py:265`), so that column is not reproducible, as for every type.
- Composite slots: `composite_person` `dob, email, first_name, last_name`, plus E3 `age, sex`;
  `composite_name_email` `email, first_name, last_name`; `composite_city_state_zip`
  `city, state, zip`; `composite_address` `city, state, street_address, zip`;
  `composite_provider` `npi, practice_address, provider_name`.

### Output surfaces

- Generated tables (CSV, Parquet, fixed width, `decoy.mask()` generate path): the provider values.
- Refusals: value-free `PlanCompileError` and `GenerationError` with the codes above.
- `generate_option_catalog()` and the JSON schema: the new type and keys.
- `docs/capability-matrix.md` and `docs/proof-manifest.json` (regenerated).
- Draw-site catalogue and the providers golden: one new site.
- The provider registry `known_providers()`: E3 adds two names, which masking can use too.

## Frozen contracts

| Contract | Change | Kind |
|---|---|---|
| `GenerateColumnConfig.type` Literal and `GENERATE_TYPES` | + `provider` | additive |
| Option registry and catalog | + `provider` entry with four keys | additive |
| Reason registry | E1: 7 plan codes and 1 runtime code; E2: 3 plan codes; E3: none. Next minor per slice at merge | additive |
| Provider registry names | E3: + `person_first_name_female`, `person_first_name_male` (34 to 36; the count pins listed in E3 step 2 move; the platform picker gains two entries) | additive |
| Draw-site catalogue | + `gen.provider_row_keyed`; `PROVIDER_IDENTIFIER_SITES` unchanged | additive |
| Masking composite output | unchanged bytes, pinned by the new digest test (AT-13, AT-14) | none |
| `composite_person` canonical columns and masking wiring | unchanged; `age` and `sex` are optional columns only | none |
| Root `decoy_engine/__init__.py` (600 lines) | no new export | none |

## Placement

Checked against `CODEMAP.md` "Where Things Go" rows: Config models, Plan compile and checks
(`plan/checks/`), Generation (by concern, sub-packages), Providers (`providers_v2/`, registry in
`_registry.py`), Native companion bridge (protocol modules in `execution/native/protocol/`),
Masking strategy handler, Reason codes, Sentry test, Unit tests, Plans, Changelog fragments.

| Change | File | CODEMAP row | Existing neighbor |
|---|---|---|---|
| type Literal | `config/_tables.py` | Config models | `GENERATE_TYPES` |
| `provider` options | `config/_generate_options.py` (or GEN-TYPES' `config/_generate_type_checks.py` if merged) | Config models | `faker` entry |
| provider checks | new `plan/checks/_generate_providers.py` | Plan compile and checks | GEN-STRICT's `_generate_columns.py` |
| faker hint | `plan/checks/_generate_columns.py` `_FakerView.check` (+ 3 lines) | Plan compile and checks | the unknown-type refusal |
| scalar draw | new `generation/kinds/_provider.py` | Generation (kinds sub-package) | GEN-TYPES' `_constant.py`, `_parametric.py` |
| group draw and bundle cache | new `generation/kinds/_provider_group.py` (E2) | Generation (kinds sub-package) | `_provider.py` |
| dispatch | `generation/synthesize.py` (at most 10 lines) | Generation | the `faker` branch |
| composite factory | new `generation/composite/_factory.py`, exported as `COMPOSITES` from `generation/composite/__init__.py` (E2) | Generation (composite sub-package) | `_generator.py`, `_validate.py` |
| composite digest test | new `tests/unit/execution/test_composite_handler_digests.py` (E2 commit 1) | Unit tests | `CompositeHandler` tests |
| optional person fields | `generation/composite/_person.py` (E3) | Generation (composite sub-package) | `_generate_deterministic` |
| two registry names | `providers_v2/_faker_adapter.py`, `providers_v2/_real_registry.py` (E3) | Providers | `person_first_name` |
| masking handler reads the factory | `execution/_strategies/_composite.py` (E2) | Masking strategy handler | the if/elif chain it replaces |
| registries | `_admission.py`, `_pinned_compile.py`, `_numeric_typing.py`, `_bounds.py` | Generation | one entry each |
| draw site | `execution/native/_draw_sites_gen.py`, `execution/native/_determinism_protocol.py`, `execution/native/protocol/providers_gen.py`, `execution/native/_capabilities.py` | Native companion bridge | `gen.faker_per_row` |
| reason codes | `reasons/plan/_part3.py` (143 lines), `reasons/runtime/_part3.py` (335 lines) | Reason codes | GEN-STRICT codes |
| sentries | `tests/sentry/defects/test_generate_option_reads.py` (`READER_FILES`), new `tests/sentry/defects/test_provider_reuse.py` (AT-4) | Sentry test | the option-read sentry |
| tests | `tests/unit/generation/kinds/test_provider.py`, `test_provider_group.py`, `tests/unit/generation/composite/test_factory.py`, `tests/unit/plan/test_generate_providers.py` | Unit tests | GEN-STRICT tests |

Module counts: `generation/` has 26 modules and gains no direct module; the two new ones are in
`generation/kinds/`. `plan/checks/` gains one module. No root export. If GEN-TYPES and GEN-INHERIT
have pushed `synthesize.py` near 600 lines, the E2 bundle-cache creation moves into a helper in
`kinds/_provider_group.py`, leaving one call line. No census entry is added and no ceiling moves.

## Slices

| Slice | Branch | Size | Content | Gate |
|---|---|---|---|---|
| E1 | `feat/gen-providers-e1` | M (about 330 lines plus tests) | type, option entry, `plan/checks/_generate_providers.py` scalar checks, faker hint, `kinds/_provider.py` with the three backend paths and the error translation, dispatch, registries, draw site and golden, 8 reason codes, capability matrix and proof manifest regeneration; AT-1 to AT-9, AT-16 to AT-18 | full suite: the reason manifest changes; T1 impact set and T2 on the exact head |
| E2 | `feat/gen-providers-e2` | M (about 320 lines plus tests) | commit 1: `CompositeHandler` digest test captured on base; commit 2: composite factory refactor; then `field` and `group`, group checks, `kinds/_provider_group.py`, bundle cache, 3 reason codes; AT-10 to AT-13 | full suite: reason manifest change and the masking handler refactor |
| E3 | `feat/gen-providers-e3` | S (about 150 lines plus tests) | `optional_fields` on `CompositePerson`, `sex` and `age`, two registry names; AT-14, AT-15 | full suite: the shared provider registry that masking reads changes; T1 impact set and T2 on the exact head |

Order: E1 after GEN-STRICT E2 (#465) merges, then E2, then E3. Each slice takes the next registry
minor at merge and adds `changelog.d/gen-providers-e<N>.md` (`### Added`).

## Engineering calls made

- A new type `provider`, not new `faker_type` values. One key then names one registry, and the
  masking spelling (`provider: synthetic_npi`) carries over unchanged (Owner question 1).
- The branch choice comes from `CapabilityMatrix.backend_type` and `poolable`. `classify_provider`
  (`execution/native/_provider_class.py:68`) is not imported. It serves the native companion's
  routing and lives in the execution layer.
- The seed is the column's existing `faker` family plus GP2's two pool labels. No new family is
  added, so `GEN_FAMILIES` does not change.
- Composite members are separate generate columns tied by `field` and `group`. This matches how
  masking binds a composite with `coherent_with` and needs no nested config shape.
- Member nulls are independent per column, as in masking.
- `sex` values are `F` and `M`, the two lists Faker ships. Other labels are V2-LOG.
- `random_choice` and `random_int_range` generate with their registry defaults. The catalog text
  points users at `categorical` and `uniform` for real use.

## Owner questions

1. **Config shape.** Recommend `type: provider, provider: <name>`. Alternative: accept registry
   names in `faker_type`. That mixes two registries under one key and makes the unknown-name
   check ambiguous.
2. **`unique: true` on provider columns.** Recommend V2-LOG. Identifier output is near-unique
   but not guaranteed, and a true unique draw needs a retry family like GEN-UNIQUE's.
   Alternative: build it now on the `faker_unique` retry pattern (about 150 more lines).
3. **Per-provider options (MRN prefix and digits, composite `email_format`, `domain_pool`).**
   Recommend V2-LOG: defaults only in V1. Alternative: a `provider_options` mapping checked
   against each adapter's declared keys, which needs every adapter to declare its keys first.
4. **`composite_custom` in generation.** Recommend refusing it. Its coherence is identity
   stability over a source value, which means nothing when there is no source. Alternative:
   allow it as independent draws, which would mislead users.
5. **`sex` values.** Recommend `F` and `M` in V1. Alternative: a configurable label map (V2-LOG).

## V2-LOG / deferred

- `unique: true` on provider columns (Owner question 2).
- `provider_options` (Owner question 3).
- Pinned route support and streamed admission for `provider`.
- Per-provider payload width bounds in `_bounds.py`.
- A batch keyed path for identifiers, if T2 shows per-row `derive_value` slower than per-row faker.
- Locales beyond each provider's declared `supported_locales`.
- More optional person fields (for example `phone` tied to `state`) and other `sex` labels.

## Not verified

- Throughput of per-row identifier `generate` in generation. No tests were run for this plan. T2
  measures it.
- That `PoolBuilder.build` and the composite pool paths accept a 32-byte `spec.seed` in every
  path. Masking passes its 32-byte key there today, which suggests they do. `PoolSampler`'s
  non-deterministic path needs exactly 8 bytes (`generation/pool/_sampler.py:180`), which is why
  the poolable path uses the `[:8]` labels.
- The final shape of GEN-STRICT E2 after review (#465 is open at `4b1d1efb`).
- The exact docstrings and count tests that pin the registry at 34 names. Dennis r1 listed them
  (E3 step 2); the builder confirms the list at E3 start.
- Whether the import-linter layer contract lets `validators/` import `generation.composite`.

## Review round 1 resolution

Dennis round 1 on `d782ffc4`: NO-GO, one blocker, three required edits, six fold-ins.

- B1 (no byte golden for the masking composites). Accepted. E2 step 1 now splits the refactor into
  two commits. Commit 1 adds a `CompositeHandler` digest test for all six composites, in
  deterministic and non-deterministic mode, captured on base. The factory signature is fixed at
  `(coherent_namespace, registry, provider_config)`. The `composite_custom` bundle-shape check
  and its `ExecutionError` stay in the handler. `COMPOSITES` is exported from
  `generation.composite`. AT-13 and P9 updated.
- M1 (cross-arch digests). Accepted. AT-3 commits digest constants for the four backend paths and
  runs them on the Mac and a Linux VM at T2. E1 step 7 states that every draw path is
  integer-only. New P16.
- M2 (count pins). Accepted. E3 step 2 lists the pins that move from 34 to 36 and notes the
  platform picker's two new entries.
- M3 (`from None` keeps `__context__`). Accepted. The translation now raises after the `except`
  block closes. Design notes, E1 step 7, class 6 and the AT-16 mutant updated.
- Fold-ins: P15 now sets a 20k rows/s floor with Dennis's measurements. The catch set adds
  `IdentifierError` and `DeterminismError`. The Fields line now says `determinism: fresh` uses an
  `os.urandom` root. `ProviderSpec` sets `deterministic`, `namespace` and `seed` explicitly. The
  three extra composite name lists join the set guard, and the two plain lists read `COMPOSITES`
  where the layer contract allows it. The legacy resident route and its memory bound are stated
  in E1 step 8.
