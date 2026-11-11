# DATE-SHIFT-UNKEYED: refuse unkeyed date_shift, delete the MD5 shift

Status: plan

Owner: Cam. Roadmap row 24zb (CLI `docs/ROADMAP.md`, section 5). Source: automated QA scan
2026-11-11, finding F2. Owner decision 2026-11-11: option (a), unkeyed `date_shift` is refused in
V1 in the engine and the CLI.

Rules consulted: 00-universal, risk-and-exceptions, development-loop, local-operations,
workspace-and-vcs, reuse-first, exploration, scope-discipline, verification, bugfix, security,
testing, architecture, cross-cutting-concerns, api-and-compatibility, documentation, and
catalogs/recurring-defects.

Base: engine main `c96e2855`, CLI main `ac0cb686`. Engine release in use by the CLI: `v0.22.0`
(no diff from main in the files below).

**Guarantee.** No public surface produces `date_shift` output without an admitted mask secret of at
least 32 bytes, in any release phase. The refusal is `keyed_strategy_requires_secret`, raised before
any source is read, with fixed text and no data values. No code that derives a shift from the value
alone remains in the engine.

**Non-goals:** a new reason code; a warning-only mode; changing keyed `date_shift` output (bytes stay
identical for a keyed run); the seeded `hash` helper `deterministic_hash` (see "Related, not in
scope").

## Proof (2026-11-11, Mac via `vm-run`, CLI venv on engine 0.22.0)

A test-only scratch file (not committed) ran five cases, all green:

1. **The MD5 primitive is reversible.** `transforms/date_shift.py` `DateShiftStrategy(seed=7)` with
   no `derive_key` shifts six known dates over a 365-day window. For each output date, trying every
   offset `k` in `-365..365` and keeping the candidates where `_shift_for_value_md5(candidate) == k`
   left 1, 3, 3, 4, 2 and 2 candidates, and the source date was always among them. So F2's static
   reading of the primitive is right: anyone with the output and the window recovers the source date
   to within a few candidates, and often exactly.
2. **`decoy run` refuses** an unkeyed `date_shift` pipeline: exit 2 (`EXIT_USAGE`), output names
   `keyed_strategy_requires_secret`, no target file written.
3. **`decoy run --chunked`** refuses the same way.
4. **`decoy.mask()`** raises `MaskSecretConfigError` carrying the same code, no target written.
5. **Engine `run_pipeline`** raises `KeyedStrategyRequiresSecret` (code
   `keyed_strategy_requires_secret`).

So F2's reachability claim does not hold on main or `v0.22.0`: the CLI does pass no key provider
without `--master-key` or `mask_secret_ref`, but the engine refuses the plan before any source is
read. The reversible code is dead: `DateShiftStrategy` has no caller in `src/` (only tests and two
sentry baselines import it), and `tests/unit/transforms/test_date_shift_key_failure.py:190` already
calls it "superseded/dead". The risk that remains is regression: one edit (dropping `date_shift` from
the keyed set, or wiring the old class back in) would make the MD5 shift live again with no test
going red, because no test pins the refusal for `date_shift`. The CLI's guidance tests cover `fpe`
and `hash` only.

## Surfaces that reach the keyed decision

| Surface | Entry | Where the refusal fires |
|---|---|---|
| `decoy run` (full frame) | CLI `cli/run/_prepare.py`, then engine `run_pipeline` | `admit_mask_key` in `E:execution/_pipeline_context.py:338` |
| `decoy run --chunked` | engine `run_mask_pipeline_chunked` / `run_mask_chunked` | `admit_mask_key` in `E:execution/_chunked_oracle.py:117` |
| `decoy.mask()` (CLI library API) | `decoy.api.mask`, then `run_pipeline` | same as `decoy run`, surfaced as `MaskSecretConfigError` |
| engine `run_pipeline` (sequential, out-of-core, auto-chunk routes) | `E:execution/_pipeline.py` | admission first; `require_mask_key` again at `_sequential.py:339`, `out_of_core/_preflight.py:65`, `_pandas_adapter.py:245`, `native/_chunked_entry.py:259` |
| platform full-frame job route | `P:api/jobs/route_dispatch_exec.py:102` -> `v2_full_frame.run_full_frame_branch` -> `v2_runner.run_v2_pipeline` | passes the attempt's `SecretKeyProvider` to `run_pipeline`; the same engine admission fires |
| platform streaming job route | `P:api/jobs/route_dispatch_exec.py:46` -> `v2_stream_coordinator.run_claim_time_streaming_route` -> `v2_runner._run_v2_pipeline_streaming` | passes the same provider to `run_mask_chunked` or `run_mask_pipeline_chunked`; chunked admission fires |
| platform sequential job route | `P:api/jobs/route_dispatch_exec.py:148` -> `v2_sequential.run_sequential_relationship_job` | passes the same provider to engine `run_sequential`; `require_mask_key` fires |
| platform out-of-core job route | `P:api/jobs/route_dispatch_exec.py:127` -> `v2_out_of_core.run_out_of_core_relationship_job` | passes the same provider to `run_fk_out_of_core`; `require_mask_key` fires |
| `unmask`, vault reports | `E:unmask.py:264`, `E:vault.py:403` | `admit_mask_key` |

**The one choke point.** The decision is made in one place: `plan_has_keyed_strategy` and
`secret_requirements_for_config`, both driven by `ALWAYS_KEYED_STRATEGIES`
(`E:secret_providers/_requirements.py:173`, which lists `date_shift`). `admit_mask_key`
(`_preflight.py:389`) applies it before any read; `require_mask_key` (`keyprovider.py:230`) is the
defense-in-depth gate at each masking route and refuses a missing provider, a `SeedKeyProvider`, and
a key under 32 bytes. The refusal stays there. This plan adds no second refusal point; it pins the
existing one for `date_shift` and removes the dead code that would bypass it.

## Behavior

- **Refusal (unchanged code, newly pinned).** A plan with a `date_shift` column (top level, inside a
  composite or `text_mask` span, any `deterministic` value) and no admitted secret fails with
  `KeyedStrategyRequiresSecret`, code `keyed_strategy_requires_secret`, text
  `KEY_REQUIRED_GUIDANCE`, before any source is read. The CLI maps it to exit 2 on `run` and
  `run --chunked`, and `decoy.mask()` raises `MaskSecretConfigError`.
- **Hard delete (engine).** In `src/decoy_engine/transforms/date_shift.py`, delete
  `_shift_for_value_md5`, `_shift_for_value_keyed` and the class `DateShiftStrategy`. Keep
  `_detect_format` and `_COMMON_FORMATS`, which the live handler, `bucket_perturb` and `text_mask`
  import. Pre-GA, so no adapter (CLI `CLAUDE.md`: "Pre-GA = hard delete").
- **Draw-site registry.** `E:execution/native/_draw_sites_mask.py` `mask.date_shift`: `call_site`
  moves to the live handler (`execution/_strategies/_date_shift.py`), and the note drops the
  "legacy no-key configs use md5(value)" sentence.
- **Docstrings.** Remove the legacy-MD5 mentions in `transforms/fpe.py:6`,
  `profiling/recommendations.py:37` and `reference_tables/_types.py:6` where they name the deleted
  class (names only; no behavior).
- **Docs.** `docs/strategies.md` `date_shift` section says the offset comes from
  `derive(job_seed, namespace, value)`; it comes from the mask key. The fix there is row 24za
  (ENGINE-DOCS-TRUTH); this slice changes only the `date_shift` keying sentences and says a secret is
  required. Whichever of the two lands second rebases.

## Failure modes and reason codes

| Failure | Code (fixed, value-free) | Surface |
|---|---|---|
| `date_shift` with no provider and no `mask_secret_ref` | `keyed_strategy_requires_secret` | engine raise; CLI exit 2; `decoy.mask()` `MaskSecretConfigError` |
| `date_shift` with a `SeedKeyProvider` | `keyed_strategy_requires_secret` | same |
| resolved key shorter than 32 bytes | `keyed_strategy_requires_secret` | same |
| `mask_secret_ref` that does not resolve | existing provider codes (`secret_provider_unknown`, `secret_env_missing`, ...) | unchanged |

No new code, no registry bump. The message is `KEY_REQUIRED_GUIDANCE`: names only, no source value,
no secret, no path.

## Pre-mortem

| Risk | Guard |
|---|---|
| Someone drops `date_shift` from `ALWAYS_KEYED_STRATEGIES` | the existing registry-equality oracle in AT-3 goes red (M1) |
| Someone bypasses keyed-column discovery specifically for `date_shift` | the refusal and no-read cases AT-1 and AT-4 go red (M5) |
| Someone makes `require_mask_key` accept a `SeedKeyProvider` | the existing direct gate tests in AT-2 go red (M2) |
| Someone makes admission accept a 32-byte `SeedKeyProvider` | the 32-byte date-shift admission case in AT-2 goes red (M6) |
| The MD5 shift returns in a new module | AT-5 sentry: no `hashlib.md5` call in any module that imports a date-shift name, and the deleted names stay absent (M3) |
| Deleting the class breaks the live handler's `_detect_format` import | the handler tests and `TestDetectFormat` stay green; the deletion keeps the helper |
| Census baselines still list the deleted functions | the two sentry baselines are regenerated in the same commit; entries only go away, no number rises |
| CLI-side change derives an admitted `SecretKeyProvider` from the visible config seed when no secret is set | AT-6 goes red (M4) |

## Acceptance tests (red first)

AT-5 is red on base `c96e2855` because the legacy names and MD5 helper still exist. The refusal
behavior is already correct on the base, so AT-1 to AT-4 and AT-6 use mutation-first evidence:
apply M1, M2, M4, M5 and M6 separately to a scratch copy, record the named test red, restore the
mutation, then run the same tests green on the final head. This makes the parametrized additions
non-vacuous without pretending the already-correct refusal fails on main. The engine tests extend
existing files; the CLI tests extend `C:tests/e2e/test_keyed_secret_guidance.py`.

| # | Test | Red on |
|---|---|---|
| AT-1 | `tests/secret_providers/test_admission_before_read.py`: `keyed_config` takes a `strategy` argument; `test_keyed_work_without_a_secret_never_falls_back` and `test_run_pipeline_keyed_without_secret_never_falls_back` run for `hash` and `date_shift`, every chunked entry, pre-GA and GA. The source is never touched and `SeedKeyProvider.mask_key` never runs | M5 |
| AT-2 | `test_seed_key_provider_is_not_a_secret` runs `date_shift` with both 8-byte and 32-byte `SeedKeyProvider` inputs, proving provider type is refused independently of length; keep the direct existing `tests/unit/test_de02_keyprovider.py::TestFailClosedGate::test_rejects_seed_provider_resolved_key_too_short` and `tests/secret_providers/test_r3_mutation_kills.py::test_require_mask_key_gates_carry_the_guidance_text` in T1 | the 32-byte admission case is red on M6; the two direct `require_mask_key` tests are red on M2 |
| AT-3 | `tests/secret_providers/test_keyed_columns.py`: keep `test_oracle_covers_every_catalog_strategy` as the registry-equality oracle; assert that a `date_shift` column inside a composite and inside a `text_mask` span is keyed for `deterministic` true and false | M1 is killed by `test_oracle_covers_every_catalog_strategy`; M5 is killed by the nested cases |
| AT-4 | `run_pipeline` with `execution_mode` `full_frame`, `sequential` and `auto` (auto-chunk threshold 1) on an unkeyed `date_shift` plan raises `keyed_strategy_requires_secret` and reads nothing | M5 |
| AT-5 | New sentry `tests/sentry/defects/test_no_unkeyed_date_shift.py`: (a) `decoy_engine.transforms.date_shift` has no `DateShiftStrategy`, `_shift_for_value_md5` or `_shift_for_value_keyed`; (b) AST scan of `src/decoy_engine/`: no call to `hashlib.md5` in a module that defines or imports a name containing `date_shift` or `shift_for_value`. Register the nested path in `tests/sentry/test_sentry_manifest.py::SENTRIES` so the gate collects it | M3; manifest coverage is proved by `test_sentry_manifest.py` |
| AT-6 | CLI: parametrize `test_run_refusal_is_usage_and_names_the_cli_remedy`, `test_run_refusal_json_error_is_the_same_fixed_text`, `test_run_refusal_chunked_matches_plain`, `test_mask_api_raises_its_documented_error_with_the_api_remedy` and `test_every_route_prints_its_fixed_refusal_text` with `date_shift`; the target file is not written and stdout, stderr and `--json` carry no source date | M4 |
| AT-7 | Keyed `date_shift` output is byte-identical before and after the deletion (existing `test_date_shift_oracle_identity.py` stays green with no golden change) | a deletion that touches the live handler |

**Named mutations** (run on the final head; each must turn at least one test red):

- M1: remove `"date_shift"` from `ALWAYS_KEYED_STRATEGIES`.
- M2: in `require_mask_key`, return `job_seed` for a `SeedKeyProvider` instead of raising.
- M3: add back a module-level `_shift_for_value_md5` that calls `hashlib.md5`.
- M4: in CLI `cli/run/_prepare.py`, derive 32 bytes solely from the visible config seed, wrap them
  in `SecretKeyProvider`, and pass that admitted provider when no secret is set. This is a genuine
  bypass of both engine gates, not the already-refused `SeedKeyProvider` shape.
- M5: in keyed-column discovery, skip a column whose strategy is `date_shift` before the
  conservative unknown-strategy fallback. This leaves the registry intact while bypassing the
  real classifier path exercised by AT-1, AT-3 and AT-4.
- M6: in `_load_legacy`, remove the `SeedKeyProvider` type refusal while leaving the 32-byte length
  check intact. The 32-byte AT-2 case must then be wrongly admitted and turn red.

## Enumerations (engine `c96e2855`, CLI `ac0cb686`)

### Routes

Each route passes `admit_mask_key` before any source read; none calls `DateShiftStrategy`.

- `run_pipeline` full frame and auto-chunk: admission at `execution/_pipeline_context.py:338`.
- Chunked entries `run_mask_chunked`, `run_mask_pipeline_chunked`: `execution/_chunked_oracle.py:117`.
- Sequential: `require_mask_key` at `execution/_sequential.py:339`.
- Out-of-core: `require_mask_key` at `execution/out_of_core/_preflight.py:65`.
- Native chunked: `require_mask_key` at `execution/native/_chunked_entry.py:259`.
- `unmask` and vault reports: `unmask.py:264`, `vault.py:403`.
- CLI `run`, `run --chunked` and `decoy.mask()` enter through the engine entries above.
- Platform full frame enters `run_pipeline`; streaming enters `run_mask_chunked` or
  `run_mask_pipeline_chunked`; sequential enters `run_sequential`; out-of-core enters
  `run_fk_out_of_core`. `route_dispatch_exec.py` passes the one provider resolved in
  `v2_orchestrator.py` to all four routes. Preview separately creates a fresh 32-byte
  `SecretKeyProvider` in `preview_child.py`, so no platform route is unkeyed.

### Exit paths

- Refusal: `KeyedStrategyRequiresSecret` from `admit_mask_key` before any read, or from
  `require_mask_key` at the route gate. CLI exit 2; `decoy.mask()` raises `MaskSecretConfigError`.
  Nothing is written: no target, no vault, no evidence manifest.
- Keyed run: unchanged.

### FK shapes

N/A: `date_shift` is a per-value column strategy; FK propagation reads the masked value and is
unchanged.

### Fields

N/A: this plan adds no config key, no result key and no reason code; it only deletes dead code and
pins an existing refusal.

### Output surfaces

- Error text: `KEY_REQUIRED_GUIDANCE`, fixed, value-free.
- CLI stdout, stderr and `--json`: the fixed text and the code only (AT-6).
- Target files, vault, evidence: not written on refusal (AT-1, AT-6).
- Logs: the deleted class's debug and warning lines go with it; no new log line.

## Class table

| # | Class | Where it applies | Structure that closes it | Proof | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | unkeyed `date_shift` output lets a holder recover source dates | refuse at admission; delete the MD5 shift | AT-1 to AT-6 | M1, M3, M4, M5, M6 |
| 2 | Cleanup missing on an exit path | refusal path | refusal fires before any source read or file open, so nothing to clean | AT-1 (`source.touched == 0`), AT-6 (no target) | M4, M5 |
| 3 | State machine and concurrency | none | N/A: no state or cache added; admission reads the provider once per run (existing test) | `test_one_provider_read_per_run_even_if_data_would_drift` | n/a |
| 4 | Route parity and silent fallback | every route in "Surfaces" | one predicate (`ALWAYS_KEYED_STRATEGIES`) and one gate pair; no seed fallback in any phase | AT-1 to AT-4, AT-6 | M1, M2, M4, M5, M6 |
| 5 | Referential integrity | none | N/A: no FK change | n/a | n/a |
| 6 | Untrusted producer | a config that sets `deterministic: false` or nests `date_shift` in a composite or span to dodge the keyed set | keyed classification ignores `deterministic` for always-keyed strategies and walks children | AT-3 | M5 |
| 7 | Check then use (TOCTOU) | none | N/A: no new read; the secret is admitted once before reads (existing) | existing admission tests | n/a |
| 8 | Weak tests | today no test names `date_shift` in the refusal; a test could pass with the strategy silently unkeyed | parametrize the existing refusal tests on `date_shift`; assert no source touch and no seed fallback; keep the registry-equality and direct route-gate tests in T1 | AT-1 to AT-4, AT-6 | M1, M2, M4, M5, M6 |
| 9 | Refactor drift | deleting the class could break `_detect_format` users or keyed bytes | keep the format helpers; keyed oracle identity stays green | AT-7, `TestDetectFormat` | delete `_detect_format` (handler tests red) |
| 10 | Frozen contract drift | the pinned `DRAW_SITES` catalogue (`tests/native/test_draw_sites_catalogue_pinned.py`) | metadata-only re-pin of `mask.date_shift` (`call_site`, `notes`); provider goldens in `test_draw_site_providers_golden.py` do not move | provider goldens unchanged; catalogue pin updated in the same commit | change the `mask.date_shift` provider (goldens red) |

## Reuse inventory, enumerations, frozen contracts

- **Reuse:** `KeyedStrategyRequiresSecret`, `KEY_REQUIRED_GUIDANCE`, `admit_mask_key`,
  `require_mask_key`, `ALWAYS_KEYED_STRATEGIES`, the `keyed_config` / `TouchedSource` /
  `profile_spy` helpers in `tests/secret_providers/`, the CLI `_keyed_pipeline` helper.
- **References to the deleted names** on main `c96e2855`:
  - `src/decoy_engine/transforms/date_shift.py:103-285` (definitions).
  - `tests/unit/transforms/test_date_shift_key_failure.py`: the `DateShiftStrategy` classes go;
    `TestDetectFormat` (line 186 on) stays.
  - `tests/unit/execution/test_expression_log_redaction.py:13, 65, 195`: the two cases move onto
    `DateShiftStrategyHandler` (same log-redaction assertions), or are deleted if the handler case
    already exists in that file.
  - `tests/sentry/baselines/secret-material-census.json:659, 662`,
    `tests/sentry/secret_material_census.json:1976-1990`,
    `tests/sentry/baselines/gcs-call-kwargs.json:386-391`: regenerated; entries removed only.
  - `tests/property/test_fpe_invariants.py:34`: comment only.
  - `src/decoy_engine/execution/native/_draw_sites_mask.py:134, 151`, its byte pin
    `tests/native/test_draw_sites_catalogue_pinned.py`, and `docs/native/draw-site-inventory.md:146-151`.
  - Comments in `transforms/fpe.py:6`, `profiling/recommendations.py:37`,
    `reference_tables/_types.py:6`.
- **Frozen contracts:** the reason code and its text are unchanged. Keyed output bytes unchanged.
  No registry or `SEED_PROTOCOL_VERSION` change. The `DRAW_SITES` catalogue byte pin moves for
  `mask.date_shift` metadata only (class 10).

## Placement

| Change | File | CODEMAP authority | Sits beside |
|---|---|---|---|
| Delete legacy class and MD5/keyed shift helpers | `src/decoy_engine/transforms/date_shift.py` | existing transform; `CODEMAP.md:242` | `_detect_format`, `_COMMON_FORMATS` (kept) |
| Draw-site `call_site` and note | `src/decoy_engine/execution/native/_draw_sites_mask.py` | existing native bridge module; `CODEMAP.md:238` | the other mask draw sites |
| `date_shift` cases in admission tests | `tests/secret_providers/test_admission_before_read.py`, `_helpers.py` | owning secret-provider tests; `CODEMAP.md:250` | the `hash` cases |
| Keyed-set assert | `tests/secret_providers/test_keyed_columns.py` | owning secret-provider tests; `CODEMAP.md:250` | its `date_shift` entry (line 31) |
| No-unkeyed-shift sentry | `tests/sentry/defects/test_no_unkeyed_date_shift.py` (new), plus its `SENTRIES` entry in `tests/sentry/test_sentry_manifest.py` | `CODEMAP.md:252`, Sentry test row, places recurring defect rules in the named `tests/sentry/defects/` subpackage and says the manifest walks nested folders | `tests/sentry/defects/test_validation_sites.py` |
| Census baselines | `tests/sentry/baselines/*.json`, `tests/sentry/secret_material_census.json` | existing sentry baselines; `CODEMAP.md:252` | existing entries |
| `date_shift` keying sentences | `docs/strategies.md` | existing strategy documentation | row 24za's edits |
| CLI refusal cases | `C:tests/e2e/test_keyed_secret_guidance.py` | existing keyed-secret surface tests | the `fpe` and `hash` cases |

## Slices

| Slice | Repo | Branch | Size | Gate |
|---|---|---|---|---|
| E1 | engine | `feat/date-shift-unkeyed-e1` | S (about 40 lines of tests, about 190 lines deleted) | T1: `tests/secret_providers/`, `tests/sentry/`, `tests/unit/transforms/`, `tests/unit/execution/test_date_shift_oracle_identity.py`, `tests/unit/execution/test_expression_log_redaction.py`, `tests/native/test_draw_sites_catalogue_pinned.py`, and `tests/native/test_draw_site_providers_golden.py`; M1, M2, M3, M5 and M6 on the final head. The draw-site catalogue is a frozen contract, so run the full default suite once on the exact merge head and diff failing ids and collected count against the stored main baseline |
| C1 | CLI | `feat/date-shift-unkeyed-c1` | XS (test-only) | T1: `tests/e2e/test_keyed_secret_guidance.py`; M4. Needs no engine release: the refusal is already in `v0.22.0` |

**When to build.** Feature group: date and number strategies (engine QA checklist section 07). Owner rule 2026-11-11: QA and its fixes run only on a V1-complete group, so E1 and C1 are built at that group's QA pass, not before.

C1 can merge first. E1 reaches CLI users with the next engine release and repin; since the deleted
code is unreachable, the repin carries no behavior change for them.

## Related, not in scope

- `E:internal/crypto.py` `deterministic_hash` (`SHA256(value + seed)`) is the same class of unkeyed
  primitive (QA F2 note). It has no live caller in `src/` and already emits a `DeprecationWarning`
  tracked by `tests/sentry/test_deprecation_expiry.py`. Keep it out of E1. Log deletion as a
  separate V2 cleanup with its own deprecation and unit-contract review.
- Row 24za owns the stale `docs/determinism.md` text that still describes a `job_seed` fallback.
