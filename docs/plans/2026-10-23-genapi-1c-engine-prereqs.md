# GEN-API 1c engine prerequisites: admission bridge + reference_date field

Rules consulted: 00-universal, development-loop, feature-dev, testing, risk-and-exceptions,
reuse-first, architecture.

Status: plan rev 2 (Opus, 2026-10-23). Folds the Codex plan-gate round-1 NO-GO (1 HIGH + 3 MED + 1 LOW).
Unblocks the platform GEN-API 1c adapter. Scope set by
Cam's locked decisions (2026-10-23): the generation API rides the PINNED subsystem for
identity/execution; V1 REFUSES statistical-reference generation jobs (at the platform adapter, not
here); `reference_date` is a digest-covered `global_settings` field. Sized from a verified engine
survey (origin/main fb144c65): a single small slice, ~30-45 LOC across 3 files, no PinnedPlan->specs
reconstruction.

## 1. Goal

Two engine changes that let the platform 1c adapter admit and reproducibly execute a generation job:
1. A `GenerationPlan`-based admission entry so the platform can get a Class-A/B verdict per table
   WITHOUT passing a full `Plan` (it holds `compile_plan(config).generation`), mirroring the 1c-E
   generation bridge.
2. A digest-covered `reference_date` on `GlobalSettings` so a reproducible generation job pins its
   clock anchor inside the approved config (the `pipeline_hash`/`_hash_config` digest 1b-P approves),
   read by `compile_pinned_plan` as the default when no positional `reference_date` is supplied.

The platform adapter calls BOTH `compile_plan(config).generation` (for the cheap admission bridge) and
`compile_pinned_plan(config, reference_date)` (for the seed-independent fingerprint + reference_date +
`execute_pinned`); the two existing entries cover identity + admission with no reconstruction.

## 2. Risk

R2 (engine surface: a new admission entry + a digest-covered config field that feeds the determinism
contract). A wrong digest treatment would let `reference_date` drift without changing the approved
digest (breaking reproducibility), so it MUST ride `_hash_config`. Full gate chain: this plan -> Codex
plan-gate -> Sonnet build tests-first -> self-check -> dennis -> Codex final -> engine CI -> merge
(PR + squash). This slice lands on engine main; it ships to the platform via the shared Phase-1
engine release (0.14.0) + platform pin bump, together with #297 and the other post-0.13.0 engine
merges.

## 3. Dependencies (verified origin/main fb144c65)

- `classify_window(plan, table, ...)` (`generation/window.py:90`) requires a full `Plan` via
  `_prepare` (`_plan_entry.py:67`). `_prepare_pinned(generation_plan, caller)` (`_plan_entry.py:131`)
  already guards + prepares a `GenerationPlan`, and `_prepare_generation` (`_plan_entry.py:85`) decodes
  `statistical_specs` from `generation.statistical_specs` (EMPTY for a non-statistical job).
- `classify_window_table(config, table, *, statistical_specs, max_cell_chars)`
  (`_window_admission.py:359`) only INDEXES `statistical_specs` inside the `type=="statistical"` branch
  (`:406-411`, `:429`); for a table with no statistical columns the mapping is never read. So a
  non-statistical admission works with `statistical_specs={}`.
- `compile_plan(config, profile, *, decoy_engine_version, no_profile=False) -> Plan`
  (`plan/_compile.py:109`); `.generation` is a `GenerationPlan`.
- `compile_pinned_plan(config, reference_date: datetime | str)` (`_pinned_compile.py:190`, normalizes at
  `:201` via `normalize_reference_date`, `_pinned_plan.py:172`, tz-aware required). It ALREADY runs
  `compile_plan` internally (`:207-212`). Only caller besides its def is the `pinned.py` docstring.
- `_hash_config` (`plan/_compile.py:624-666`) hashes every `global_settings` key except
  `_NON_SEMANTIC_GLOBAL_SETTINGS` (`:615-621` = categorical_retention_warn_threshold,
  freetext_advisory_min_avg_length, freetext_advisory_min_distinctness). Its `json.dumps` has NO
  `default=str` (`:647-655`): a non-JSON-native value raises `TypeError` at compile.
- `GlobalSettings` (`config/_global_settings.py`, fields `:103-112`, `model_config extra="forbid"`
  `:101`). Config reaching the planner is `PipelineConfig.model_validate(...).model_dump()`.
- No sentry couples `_NON_SEMANTIC_GLOBAL_SETTINGS` or the `GlobalSettings` field set; the
  public-import-boundary sentry (`tests/sentry/test_public_import_boundary.py`) + module-size sentry
  apply to any new import/LOC.

## 4. Design + exact edits

### 4.1 Admission bridge (`generation/window.py`)
Add `classify_window_from_generation_plan(generation_plan, table, *, max_cell_chars=DEFAULT,
window_end=DEFAULT, instance_default_locale=None, provider_snapshot=None) -> WindowAdmission`: mirror
`classify_window` (`:90-110`) but call `_prepare_pinned(generation_plan,
"classify_window_from_generation_plan")` (import from `_plan_entry`) instead of `_prepare`, then the
unchanged `classify_window_table`. **Forward ALL FOUR keywords explicitly** (do not pass only
defaults): `classify_window_table(prepared.config, table,
statistical_specs=prepared.legacy_kwargs["statistical_specs"], max_cell_chars=max_cell_chars,
window_end=window_end, instance_default_locale=instance_default_locale,
provider_snapshot=provider_snapshot)` matching `classify_window`'s own delegation exactly
(`_window_admission.py:359` uses `instance_default_locale` + `provider_snapshot` for Faker eligibility
+ reproducibility, so dropping either silently changes admission).
Add the name to `window.py:__all__` (`:59`). Public module, no re-export plumbing needed; the platform
imports `from decoy_engine.generation.window import classify_window_from_generation_plan`.
Do NOT add `generate_rows_from_generation_plan` here (that is 1d/1f execution; admission only needs the
classify entry).

### 4.2 reference_date field (`config/_global_settings.py`)
Add `reference_date: str | None = None` to the field block, with a **`@field_validator("reference_date",
mode="before")`** (NOT a default/after validator: an after-validator runs post-`str | None` coercion
and would reject an aware `datetime`, and YAML timestamps decode as `datetime` objects; `mode="before"`
accepts the raw input). It accepts `None`, a `str`, or a `datetime`, and returns the canonical
`normalize_reference_date(value)` string (`"%Y-%m-%dT%H:%M:%SZ"`) when not None, so `model_dump()`
yields a JSON-native STRING (never a datetime) and `_hash_config`'s no-`default=str` `json.dumps` does
not raise. **Translate the error contract:** `normalize_reference_date` raises `PinnedModeError`
(inherits `Exception`, not `ValueError`), which escapes Pydantic's validation-error contract; catch it
in the validator and `raise ValueError("<value-free message>") from None` so a bad reference_date
becomes a field-level `ValidationError` (test the exception type + field location). Keep
`extra="forbid"`.
IMPORT-BOUNDARY (round-1 LOW): the `test_public_import_boundary.py` sentry only checks the four
designated boundary files for `decoy_engine.internal` imports; it will NOT decide a config->generation
dependency. So make the decision directly: `generation._pinned_plan`'s `normalize_reference_date` has
no import cycle with `config` today (verify with a smoke import test). PREFER relocating
`normalize_reference_date` to a neutral/shared module (carrying its `PinnedModeError` dependency) and
importing it from BOTH `_pinned_plan.py` and `_global_settings.py` (behavior-identical, preserves
exception identity) to keep `config` from importing `generation`; if the smoke test shows the direct
import is clean and acceptable, that is allowed too. Decide in the plan-build, verify by smoke import.

### 4.3 compile_pinned_plan reads the field (`generation/_pinned_compile.py`)
Change the signature (`:190`) to `reference_date: datetime | str | None = None`. Before `:201`:
`reference_date = reference_date if reference_date is not None else
config.get("global_settings", {}).get("reference_date")`; if still None, raise
`PinnedModeError(code="pinned_reference_date_invalid", message=...)` (existing code, value-free).
Then `normalize_reference_date(reference_date)` unchanged (idempotent on an already-normalized string).

### 4.4 Behavior preservation (must hold)
- A SUPPLIED `reference_date` must be digest-covered (do NOT add it to
  `_NON_SEMANTIC_GLOBAL_SETTINGS`): two configs differing only in a supplied `reference_date` hash
  differently.
- **An UNSET `reference_date` must NOT change an existing config's digest** (round-1 MEDIUM-4): a
  default field means `model_dump()` now emits `reference_date: None` for every pre-existing config,
  and `_hash_config` would include that key and silently shift every existing approval digest. Fix: in
  `_hash_config`, drop `global_settings` keys whose value is `None` from the hashed semantic config (a
  value-None key is semantically "unset"), so a config with no reference_date hashes IDENTICALLY to
  before the field existed, while a supplied reference_date is covered. (Implement in the same filter
  that already excludes `_NON_SEMANTIC_GLOBAL_SETTINGS`.) Test: a config with no reference_date hashes
  identically before/after the field is added; `reference_date: null` == field absent.
- `compile_plan` / `generate_tables` (non-pinned path) ignore the new field (benign carry on
  `GlobalSettings`).
- Guarantee is limited to the VALIDATED config path: a raw unvalidated dict could carry a `datetime`
  into `_hash_config` and raise; the engine entries all validate via `PipelineConfig.model_validate`
  first, so the normalized-string guarantee holds for them.
- `execute_pinned` reads `plan.reference_date` (`_pinned_execute.py:199`) unchanged; `classify_window`
  (full-`Plan`) unchanged; the new bridge is additive.
- No change to the pinned statistical path; statistical jobs are refused at the platform adapter.

## 5. Acceptance tests (tests-first; red-first captured)

1. Admission-bridge parity: for a non-statistical config, `classify_window(compile_plan(config)...,
   table)` and `classify_window_from_generation_plan(compile_plan(config).generation, table)` return
   IDENTICAL `WindowAdmission` (a Class-A table and a Class-B table, e.g. null_injection/categorical).
   Parity must exercise ALL FOUR kwargs with OBSERVABLE effect, not defaults only: a non-default
   `instance_default_locale` and a `provider_snapshot` that change a Faker-eligibility verdict, plus a
   non-default `window_end` and `max_cell_chars`; compare REFUSAL CODES as well as successful verdicts
   (a dropped kwarg must make the test fail).
2. Bridge type guard: `classify_window_from_generation_plan` raises `TypeError` on a dict, a full
   `Plan`, and a `PinnedPlan` (only a `GenerationPlan` is accepted).
3. Non-statistical admission uses `statistical_specs={}` (no decode path touched): assert via a
   non-statistical table admits without constructing any StatisticalSpec.
4. reference_date validation (mode="before"): an aware `datetime` object normalizes to the canonical
   ISO string in `model_dump()`; an offset-bearing string normalizes; a naive `datetime` AND a naive
   string are BOTH refused as a field-level `ValidationError` (assert it is a `ValidationError` with
   the `reference_date` field location, NOT a raw `PinnedModeError`). A naive-only rejection test is
   vacuous since the fix must reject naive while ACCEPTING aware datetimes.
5. Digest coverage: two configs identical except a SUPPLIED `reference_date` produce DIFFERENT
   `_hash_config` digests; `_hash_config` does not raise on the string value (JSON-native); the field
   is NOT in `_NON_SEMANTIC_GLOBAL_SETTINGS`. Omission: a config with NO reference_date (and one with
   `reference_date: null`) hashes IDENTICALLY to the pre-field baseline (the None-drop in `_hash_config`
   holds), so adding the field does not migrate existing approval digests.
6. compile_pinned_plan: reads `global_settings.reference_date` when the positional arg is absent;
   positional arg still wins when supplied; raises `pinned_reference_date_invalid` when neither is
   present.
7. Sentries: module-size + public-import-boundary green (relocate `normalize_reference_date` if the
   boundary sentry flags the config->generation import; re-run to confirm).

## 6. Done-when

- 4 files changed (`generation/window.py` bridge, `config/_global_settings.py` field+validator,
  `generation/_pinned_compile.py` read-the-field, `plan/_compile.py` `_hash_config` None-drop) +
  possibly a neutral relocation of `normalize_reference_date`.
- Section-5 tests pass; red-first captured. ruff + format + mypy clean on changed modules; module-size
  + import-boundary sentries green.
- dennis GO + Codex final GO; merge to engine main (PR + squash).
- Ships to platform via the shared Phase-1 engine release 0.14.0 + platform pin bump (with #297 and
  the other post-0.13.0 merges), not independently.

## 7. Explicitly deferred

`generate_rows_from_generation_plan` (1d/1f execution); the platform 1c adapter (separate platform
slice, consumes this); statistical-artifact digest binding (P3, deferred, refuse statistical jobs for
V1); the engine release + platform pin bump (shared Phase-1 slice).
