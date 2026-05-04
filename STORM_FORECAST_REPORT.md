# STORM → FORECAST → Report — engine side

Scope: the compute layer. The platform's API/UI/PDF half is in `forge-platform/STORM_FORECAST_REPORT.md`. This doc covers everything that runs without the platform — i.e., what `decoy-engine` exposes so the CLI can run analysis offline and the platform can call it server-side.

## Why this split

The brand reference promises buyers in healthcare and fintech that **FORECAST never touches raw data** — it only sees STORM's statistical profile. That's a real selling point, and the cleanest way to enforce it is to make FORECAST a pure function over a JSON-serializable input. The engine is the right home: contributors can audit the function signature, and platform/CLI can both reuse it.

## Cross-cutting rules

- **FORECAST never receives raw data.** Type signature accepts `StormProfile` only. A unit test asserts the signature; if a future contributor adds a `connector` or `dataset` argument, the test fails.
- **HIPAA, never HIPPA.** CI grep gate.
- **Mask = field transform; Disguise = bundle.** FORECAST recommends Disguises (bundles); per-field hints are Masks.

## Modules

### `decoy_engine.storm`

Inputs:
- a `Connector` (existing engine concept — DB / file / cloud source)
- a dataset selector (table name, file path, or query)

Outputs: a `StormProfile` dataclass.

```python
@dataclass
class FieldStats:
    name: str
    inferred_type: str          # "int", "string", "date", "phone", "email", "ssn", "zip", ...
    distinct_count: int
    null_rate: float
    top_values: list[tuple[Any, int]]  # top 10 with frequencies
    regex_matches: list[str]    # detector ids that matched (e.g. "ssn", "us_phone", "icd10")
    pii_score: float            # 0.0-1.0 likelihood
    format_signals: dict        # date format, phone format, etc.

@dataclass
class StormProfile:
    dataset_id: str
    fields: list[FieldStats]
    row_count: int
    sample_strategy: str        # "full", "stratified", "head-N"
    generated_at: datetime
    engine_version: str
```

Where the existing logic lives today: `forge-platform/api/analytics/` already does most of this (`ProfileResponse` with null_rate, distinct_count, dtype, re-id risk). **Pull that logic *down* into the engine.** The platform should become a thin caller. This makes the CLI usable for offline analysis (a real feature for air-gapped Enterprise customers).

Compute primitives to ship:
- avg, mean, min, max
- top 10 values with frequencies
- regex pattern detection (SSN, phone, email, ZIP, IP, ICD-10, NPI, MRN, account-number-like, credit-card-like)
- date format sniffing (ISO, US, EU)
- phone format sniffing (E.164, US, free-form)
- field type inference (numeric / categorical / date / freeform string / structured)
- PII likelihood scoring (combines regex + value distribution + name heuristics)

### `decoy_engine.forecast`

Pure function:

```python
def recommend(profile: StormProfile) -> ForecastReport: ...
```

The function MUST NOT take a connector, dataset, or raw values argument. This is the platform's security promise made executable.

```python
@dataclass
class FieldRecommendation:
    field_name: str
    recommended_mask: str           # transform identifier from MaskRegistry
    reasoning: str                  # short user-facing string
    confidence: float

@dataclass
class DisguiseRecommendation:
    disguise_id: str                # "hipaa", "pci", ...
    match_score: float
    matched_fields: list[str]
    summary: str                    # "3 SSN-format fields, DOB column, ICD-10 lookup → HIPAA"

@dataclass
class ForecastReport:
    profile_id: str                 # ties back to StormProfile
    disguise_recommendations: list[DisguiseRecommendation]  # ranked
    field_recommendations: list[FieldRecommendation]
    risk_flags: list[str]           # e.g., "high re-id risk on (zip, dob, gender)"
    proposed_pipeline_yaml: str     # ready-to-edit pipeline config
    generated_at: datetime
```

`ForecastReport` is JSON-serializable. **Rendering (HTML / PDF) is the platform's job — the engine never does presentation.**

### Tonal contract for platform consumers

- STORM is run-shaped: takes time, emits progress events. Expose progress via a callback so the platform can stream them as SSE/websocket. UI tone is kinetic (data flying, fields tagging) — that's a UX choice, not an engine concern, but the streaming API enables it.
- FORECAST is a single, deterministic call — no progress, no animation. UI tone is calm. Engine just returns once.

## File layout

```
src/decoy_engine/
  storm/
    __init__.py
    profiler.py        # the runner
    detectors/         # regex + heuristic detectors (ssn, phone, icd10, ...)
    types.py           # StormProfile, FieldStats
  forecast/
    __init__.py
    recommender.py     # the pure function
    types.py           # ForecastReport et al.
    rules.py           # ranking weights
```

Disguise YAMLs live in `decoy_engine/disguises/` — see `DISGUISES.md`. FORECAST loads them at startup and consults their detector hints.

## What this doc does NOT cover

- HTTP routes, RBAC, SSE encoding, Report persistence, PDF export → `forge-platform/STORM_FORECAST_REPORT.md`.
- Disguise YAML schema and the 8 launch bundles → `DISGUISES.md`.

## Verification

- `pytest` — full suite passes.
- New unit test: introspect `forecast.recommend`'s signature; assert it has exactly one parameter typed `StormProfile`. This guards the security boundary.
- New integration test: feed `examples/mask_example.yaml` fixture through STORM, then FORECAST. Snapshot the JSON output for both.
- Run STORM offline (no network) — should work; engine has no platform dependency.
- `grep -ri "HIPPA" .` returns zero hits.
