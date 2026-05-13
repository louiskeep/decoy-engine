# Block 1 — column role taxonomy (engine side)

> **Status:** plan — part of the 2026-05-13 audit-fix series. See master index in `decoy-platform/plans/2026-05-13-audit-fix-master-index.md`.
> **Pairs with:** `decoy-platform/plans/2026-05-13-column-role-taxonomy-platform-and-web.md`
> **Block order:** ships first; every other block keys off the new `role` field.

## Why

`storm/profiler.py:45-48` hardcodes two module-level sets:

```python
_PII_DETECTORS    = {"email", "ssn", "us_phone", "person_name"}
_QUASI_ID_DETECTORS = {"us_zip", "iso_date", "us_date", "eu_date"}
```

Everything downstream — `_score_pii`, `_quasi_identifier_groups`, the FORECAST recommender — derives behavior from these two sets. The reference's three-tier taxonomy (direct / quasi / sensitive / non-identifying) has no representation in `FieldStats`, no user override surface, and no way to mark a column as a sensitive attribute (diagnosis, salary, orientation).

Sweeney's 87%-uniqueness lesson is acknowledged in `storm/profiler.py:51-53` ("Re-identification literature shows ~87% of US residents are unique on…") but is dead code — only one hardcoded trio (DOB, ZIP, gender) is recognized, and only via column-name regex.

This block adds a `role` field on every `FieldStats`, an inference function that maps detectors to roles, and the wiring so user overrides applied by the platform layer are honored by FORECAST.

## Code paths

- `src/decoy_engine/storm/types.py` — `FieldStats` dataclass.
- `src/decoy_engine/storm/profiler.py` — `_profile_column`, `_score_pii`, `_quasi_identifier_groups`, `run_storm`.
- `src/decoy_engine/forecast/recommender.py` — disguise trigger evaluation.
- `src/decoy_engine/disguises/*.yaml` — `triggers.any_roles` / `co_occurrence` keys.
- `src/decoy_engine/disguises/schema.py` — disguise schema validation.

## Engine changes

### 1. `FieldStats` gains role fields

```python
# storm/types.py
Role = Literal["direct", "quasi", "sensitive", "non_identifying", "unknown"]

@dataclass
class FieldStats:
    # ... existing fields ...
    inferred_role: Role = "unknown"
    user_role: Optional[Role] = None

    @property
    def role(self) -> Role:
        return self.user_role or self.inferred_role
```

- `inferred_role` is what `run_storm` writes.
- `user_role` is what the platform layer writes when an analyst overrides via the PATCH endpoint (Block 1 platform).
- `role` is the effective value every other consumer reads.

Back-compat: existing persisted profiles deserialize cleanly because both new fields default.

### 2. Detector → role mapping

Replace the two hardcoded sets with a single source of truth:

```python
# storm/profiler.py
_DETECTOR_TO_ROLE: dict[str, Role] = {
    # direct identifiers
    "email": "direct",
    "ssn": "direct",
    "us_phone": "direct",
    "person_name": "direct",
    "pan": "direct",
    "iban": "direct",
    "mrn": "direct",
    "npi": "direct",
    "health_plan_id": "direct",
    "license_num": "direct",
    "vehicle_id": "direct",
    "device_id": "direct",
    "biometric_id": "direct",
    "fax_number": "direct",
    "url": "direct",
    "ipv4": "direct",
    # quasi-identifiers
    "us_zip": "quasi",
    "iso_date": "quasi",
    "us_date": "quasi",
    "eu_date": "quasi",
    "icd10": "quasi",          # ICD-10 is identifying-in-aggregate, not direct
    "cvv": "direct",            # arguably sensitive-only, but rare on its own
}
```

`_score_pii` becomes a thin function of `inferred_role`:

```python
def _infer_role(matches: list[DetectorMatch]) -> Role:
    if not matches:
        return "unknown"
    return _DETECTOR_TO_ROLE.get(matches[0].detector_id, "unknown")
```

Keep `_score_pii` for one release for back-compat — but its inputs are now derived from `role`:

```python
def _score_pii(matches, unique_rate, role):
    base = {"direct": 0.8, "quasi": 0.4, "sensitive": 0.5, "non_identifying": 0.0}.get(role, 0.0)
    boost = min(0.15, unique_rate * 0.15) if unique_rate > 0.5 else 0.0
    return round(min(1.0, base + boost * (1 if role != "non_identifying" else 0)), 3)
```

### 3. `_quasi_identifier_groups` becomes role-driven

Replace the DOB/ZIP/gender regex with: "every column whose role is `quasi` is a member of the QI set." Return a single sorted list, not a list-of-lists.

```python
def _quasi_identifier_columns(fields: list[FieldStats]) -> list[str]:
    return sorted(f.name for f in fields if f.role == "quasi")
```

Keep `quasi_identifier_groups` on `StormProfile` as a deprecated alias (a single-element list of the new flat list) for one release.

### 4. Forecast triggers read `role`

`disguises/schema.py` Trigger schema adds optional `any_roles: list[Role]` and `co_occurrence_roles: list[list[Role]]` (parallel to existing `any_detectors` / `co_occurrence`). Old detector-keyed triggers keep working — both sets OR'd at the top level.

`forecast/recommender.py`: when evaluating a trigger, check role-based predicates against `profile.fields[i].role`, not against detector matches.

Example HIPAA disguise YAML diff:

```yaml
# disguises/hipaa.yaml
triggers:
  any_detectors: ["ssn", "mrn", "npi", "icd10"]      # kept for back-compat
  co_occurrence:
    - ["iso_date", "us_zip", "person_name"]
  # new role-keyed triggers
  any_roles: ["direct"]
  co_occurrence_roles:
    - ["quasi", "quasi", "quasi"]                   # any 3 quasi-IDs → HIPAA territory
```

## Wire format

`StormProfile.to_dict()` exposes both `inferred_role` and `user_role` per field. Platform persists these; UI surface uses `role` (computed) for chip color, and shows the user override state.

## Tests to add

`tests/unit/test_storm_role_inference.py`:
- Every registered detector ID is present in `_DETECTOR_TO_ROLE`. (Asserts via introspection of `REGISTERED_DETECTORS`.)
- No detector ID maps to `"unknown"`.
- Field with no detector matches → `inferred_role == "unknown"`.
- Field with `email` match → `inferred_role == "direct"`.
- `user_role` override beats `inferred_role` in the `.role` property.

`tests/unit/test_forecast_role_triggers.py`:
- Profile with three quasi-role columns triggers HIPAA via `co_occurrence_roles` even when no detector ID is in the legacy `co_occurrence` list.
- Legacy `any_detectors` triggers still fire when role-keyed triggers don't match.

## Migration / back-compat

- Persisted `StormProfile` JSON deserializes cleanly: new fields default, old `quasi_identifier_groups` keeps its shape.
- `_PII_DETECTORS` and `_QUASI_ID_DETECTORS` deleted in same PR; greppable change.
- Add a `DeprecationWarning` on first read of `StormProfile.quasi_identifier_groups`.

## Verification

1. `pytest tests/unit/test_storm_role_inference.py -v` — green.
2. `pytest tests/unit/test_forecast_role_triggers.py -v` — green.
3. `pytest tests/unit/test_disguises_loader.py -v` — still green (existing tests must pass unchanged).
4. Manual smoke: run STORM on the existing `tests/fixtures/sample.csv`, confirm `profile.fields[*].role` is set on every column.

## Out of scope (in later blocks)

- Persisting user overrides — platform side, Block 1 platform doc.
- API to set overrides — platform side.
- Risk math that consumes the QI set — Block 2.
- UI chip selector — platform/web.
