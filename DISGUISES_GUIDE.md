# Disguises — engine

> **Status:** shipped — all 8 launch Disguises and 22 field detectors live on `claude/sprint-work` as of 2026-05-09. This guide describes the current state of the feature.
> **Last reviewed:** 2026-05-09

Disguises are the primary differentiator in sales: every regulated shop has a compliance officer asking "how do you ensure developers don't touch production PII?" — Disguises are the answer that gets Decoy through the door. A Disguise is a named, regulation-aware bundle of masking rules that FORECAST recommends automatically when a STORM scan detects the right sensitive fields.

## Cross-cutting rules

- **HIPAA, never HIPPA.** A misspelling in a regulation name kills credibility instantly. CI grep gate enforces this.
- **Disguise = bundle. Mask = single transform.** Don't blur the line.
- **Disguises are data, not code.** Each is a YAML file under `src/decoy_engine/disguises/`. Adding or editing a Disguise must not require a code change — the loader auto-discovers every `*.yaml` in that directory.

## Launch set (8 files)

| File | Regulation | Primary buyer | Trigger signal |
|---|---|---|---|
| `default.yaml` | Generic PII starter pack | Everyone | name / email / phone / SSN anywhere in the profile |
| `hipaa.yaml` | HIPAA Privacy Rule — Safe Harbor (45 CFR §164.514(b)(2)) — all 18 identifiers + clinical extras | Healthcare data engineers | SSN **or** `mrn`/`npi`/`icd10` **or** date+ZIP+name co-occurrence |
| `pci.yaml` | PCI DSS §3.4 — cardholder data protection | Fintech, e-commerce | PAN or CVV anywhere, or retail co-occurrence (PAN + identity + contact) |
| `glba.yaml` | GLBA — Gramm-Leach-Bliley financial data protection | Banking, insurance | SSN **or** IBAN **or** financial co-occurrence (SSN + IBAN + name) |
| `gdpr.yaml` | GDPR — EU pseudonymisation (Art. 4(5), Recital 26) | EU-facing products | IBAN **or** `eu_date` (deliberately excludes US-only email+name combos) |
| `ccpa.yaml` | CCPA — California Consumer Privacy Act | US consumer tech | Broad PII co-occurrence (name + email/phone + device/IP/zip) |
| `ferpa.yaml` | FERPA — student educational record protection | EdTech, universities | SSN **or** education co-occurrence (SSN + name + iso_date) |
| `sox.yaml` | SOX — Sarbanes-Oxley financial reporting integrity | Public companies | Financial co-occurrence (IBAN + email + iso_date) |

## Field detectors

Detectors run as part of every STORM scan. Each detector is `(series, col_name) -> DetectorMatch | None`. The profiler runs all 22 against every column. FORECAST reads the resulting `FieldStats.regex_matches` — it does not re-run detection on raw data.

### Thresholds

| Constant | Value | Meaning |
|---|---|---|
| `DEFAULT_MIN_MATCH_RATE` | 0.70 | 70% of non-null values must match (no name hint) |
| `NAME_HINT_MIN_RATE` | 0.40 | 40% if column name strongly hints at the type |

### Detector inventory

#### Phase 0 — Core PII (shipped with STORM)

| Detector ID | Value pattern | Name-hint | Validator | Notes |
|---|---|---|---|---|
| `email` | RFC 5321ish `user@host.tld` | `email`, `mail`, `email_address` | — | |
| `ssn` | `###-##-####` or 9 digits; rejects 000/666/9xx per SSA | `ssn`, `social_security` | — | |
| `us_phone` | 10 digits with separators, optional +1 | `phone`, `tel`, `mobile`, `cell` | — | |
| `us_zip` | 5 digits, optional `-####` | `zip`, `postal`, `post_code` | — | |
| `person_name` | 1–3 proper-cased tokens | `*name`, `first_name`, `patient` … | — | Name-hint **required** — too noisy without it |
| `iso_date` | `YYYY-MM-DD` with optional time | `date`, `dob`, `created`, `updated` … | — | |
| `us_date` | `MM/DD/YYYY` | same as iso_date | — | |
| `eu_date` | `DD.MM.YYYY` or `DD-MM-YYYY` | same as iso_date | — | |

#### Phase 1 — PCI / GDPR (Sprint A · Item 31 phase 1)

| Detector ID | Value pattern | Name-hint | Validator | Notes |
|---|---|---|---|---|
| `pan` | 13–19 grouped digits | `pan`, `card`, `credit_card` … | Luhn mod-10 | Rejects random digit strings that satisfy the regex |
| `cvv` | 3–4 digits | `cvv`, `cvc`, `csc`, `security_code` | — | Name-hint **required** — value pattern too broad |
| `iban` | Country code + checksum + BBAN | `iban`, `bank_account` | ISO 13616 mod-97 | Rejects bad-checksum lookalikes |
| `ipv4` | Dotted quad | `ip`, `ipv4`, `ip_addr` | Octet 0–255 | Rejects `999.1.1.1` |

#### Phase 3 — HIPAA Safe Harbor completers + clinical (Sprint B · Item 31 phase 3)

| Detector ID | Value pattern | Name-hint | Validator | Fires without hint? |
|---|---|---|---|---|
| `icd10` | `[A-Z]\d{2}(\.[A-Z0-9]{1,4})?` | `icd`, `dx`, `diagnosis_code` | Structure (letter+2digits, 3–7 chars) | Yes — format is distinctive |
| `npi` | Exactly 10 digits | `npi`, `national_provider`, `physician_id` | CMS Luhn variant (prepend `80840`, check last digit) | Yes |
| `mrn` | `[A-Z0-9-]{4,20}` | `mrn`, `medical_record`, `patient_id`, `chart_num` | — | **No** — name-hint required |
| `url` | `https?://…` | `url`, `uri`, `href`, `link`, `website` | — | Yes — scheme is distinctive |
| `fax_number` | US phone format | `fax`, `fax_num`, `facsimile` | — | **No** — name-hint required (distinguishes fax from phone) |
| `health_plan_id` | `[A-Z0-9-]{4,30}` | `beneficiary`, `member_id`, `subscriber_id`, `coverage_id` | — | **No** |
| `license_num` | `[A-Z0-9-]{4,20}` | `license`, `licence`, `cert`, `credential` | — | **No** |
| `vehicle_id` | `[A-HJ-NPR-Z0-9]{17}` (VIN — no I/O/Q, ISO 3779) | `vin`, `vehicle_id`, `license_plate` | — | Yes — VIN format is distinctive |
| `device_id` | `[A-Z0-9._-]{4,30}` | `device_id`, `serial_num`, `equipment_id`, `implant_id` | — | **No** |
| `biometric_id` | Any non-empty string | `fingerprint`, `retina`, `iris`, `voice_print`, `bio_id` | — | **No** — name-hint is definitive |

### Name-hint logic

Detectors declare a `_NAME_HINTS` pattern keyed by their ID. When the pattern matches the column name:
- The match-rate threshold relaxes to `NAME_HINT_MIN_RATE` (40%).
- Name-hint-only detectors (`person_name`, `cvv`, `mrn`, `fax_number`, `health_plan_id`, `license_num`, `device_id`, `biometric_id`) skip the value evaluation entirely without a hit — the false-positive cost without the hint outweighs the benefit.

## HIPAA Safe Harbor — all 18 identifiers

The `hipaa.yaml` Disguise covers every identifier listed in 45 CFR §164.514(b)(2).

| Safe Harbor ID | Identifier | Detector(s) | Masking strategy |
|---|---|---|---|
| A | Names | `person_name` | `faker` — `person_name` |
| B | Geographic data smaller than state (ZIP) | `us_zip` | `truncate` — keep first 3 digits |
| C | Dates (except year) | `iso_date`, `us_date`, `eu_date` | `date_shift` — jitter ±30 days |
| D | Telephone numbers | `us_phone` | `faker` — `phone_number` |
| E | Fax numbers | `fax_number` | `faker` — `phone_number` |
| F | Email addresses | `email` | `faker` — `email` |
| G | Social security numbers | `ssn` | `hash` sha256 |
| H | Medical record numbers | `mrn` | `hash` sha256 truncate:12 |
| I | Health plan beneficiary numbers | `health_plan_id` | `hash` sha256 truncate:12 |
| J | Account numbers | `iban` | `hash` sha256 |
| K | Certificate / license numbers | `license_num` | `hash` sha256 truncate:12 |
| L | Vehicle identifiers / serial numbers (VINs) | `vehicle_id` | `hash` sha256 truncate:17 |
| M | Device identifiers / serial numbers | `device_id` | `hash` sha256 truncate:12 |
| N | Web URLs | `url` | `redact` — `[URL REDACTED]` |
| O | IP addresses | `ipv4` | `faker` — `ipv4` |
| P | Biometric identifiers | `biometric_id` | `redact` — `[BIOMETRIC REDACTED]` |
| — | Ages 90+ (not an identifier per se, but required) | age column heuristic | `bucketize` — cap at 89 |
| Clinical+ | ICD-10-CM diagnosis codes | `icd10` | `truncate` — keep first 3 chars (category generalisation) |
| Clinical+ | National Provider Identifier | `npi` | `hash` sha256 truncate:10 |

The `truncate` length on hash output is calibrated so values are short enough for display while long enough to preserve FK join integrity across tables.

## Disguise definition format

Each file lives at `src/decoy_engine/disguises/<id>.yaml`. The Pydantic schema is in `schema.py`; the auto-loader is in `loader.py`.

```yaml
id: hipaa
name: HIPAA Disguise
regulation: "HIPAA Privacy Rule — Safe Harbor (45 CFR §164.514(b)(2))"
version: 1
primary_buyer: "Healthcare data engineers"
summary: "PHI Safe Harbor: all 18 identifiers, date shifting, ZIP truncation, clinical ID hashing"

rules:
  - match:
      name_regex: "(?i)^(ssn|social.?security)"
      detectors: ["ssn"]
    mask: hash
    params: { algorithm: sha256 }
    notes: "SSN — irreversible hash; preserves referential integrity across joins."

  - match:
      detectors: ["us_zip"]
    mask: truncate
    params: { length: 3 }
    notes: "Safe Harbor: keep first 3 ZIP digits (county/area only)."

triggers:
  any_detectors: ["ssn", "mrn", "npi", "icd10"]
  co_occurrence:
    - ["iso_date", "us_zip", "person_name"]
    - ["mrn", "icd10"]

match_score_weight: 1.0
```

**Adding a new Disguise:** drop a `*.yaml` in `src/decoy_engine/disguises/`. The loader auto-discovers it at startup, schema validates on load, and FORECAST starts recommending it immediately. No code change needed.

## FORECAST wiring

`decoy_engine.forecast.recommender` loads every YAML in `disguises/` at startup and compiles a list of `(disguise, trigger_predicate, score_weight)`. For each StormProfile it:

1. Evaluates each Disguise's `triggers` against the profile's detector hits.
2. Ranks by `match_score = sum(matched_field_weights) * disguise.match_score_weight`.
3. Returns the ranked list as `DisguiseRecommendation` objects.

Trigger structure (both fields are OR'd against each other at the top level):
- `any_detectors` — fires if any listed detector ID hit any column in the profile.
- `co_occurrence` — each sub-list is an AND group; fires if all listed detector IDs hit at least one column in the same profile.

## Strategy availability in Disguise rules

Disguise `rules[].mask` can reference any strategy the engine supports. The full set as of Sprint B:

| Strategy | Config keys | FK-safe? |
|---|---|---|
| `hash` | `algorithm` (sha256), `truncate` | Yes — keyed determinism |
| `faker` | `provider`, `locale` | Yes — keyed determinism |
| `date_shift` | `min_days`, `max_days` (alias `jitter_days`) | Yes |
| `truncate` | `length`, `from_end` | Yes (many-to-one) |
| `bucketize` | `preset` \| `width`, `format` | Yes (many-to-one) |
| `redact` | `redact_with` | Yes |
| `fpe` | `charset`, `preserve_separators`, `validate_luhn` | Yes — bijection, keyed |
| `shuffle` | — | No — non-deterministic |
| `formula` | `expr` | Depends on expr |

FPE (`fpe`) is the recommended strategy for fields that must stay valid-looking after masking (e.g. SSNs that look like SSNs, PANs that pass Luhn checks). It requires the tenant master key.

## Spelling guard

```bash
grep -ri "HIPPA" src/ tests/ src/decoy_engine/disguises/
```

Must return zero hits. Enforced in CI and recommended as a pre-commit hook.

## Verification

| Check | Command / assertion |
|---|---|
| All 8 bundles load without error | `pytest tests/unit/test_disguises_loader.py -v` |
| Exact bundle set | `assert {d.id for d in load_disguises()} == {"default","hipaa","pci","glba","gdpr","ccpa","ferpa","sox"}` |
| Every bundle has ≥1 field rule | `test_every_disguise_has_at_least_one_field_rule` |
| Every bundle has a trigger | `test_every_disguise_has_a_trigger` |
| HIPAA FORECAST ranks first on PHI data | `test_hipaa_scores_highest_on_phi_profile` |
| PCI ranks first on PAN-heavy data | `test_pci_scores_highest_on_pan_profile` |
| Clinical detectors (ICD-10, NPI, MRN) | `pytest tests/unit/test_clinical_detectors.py -v` (27 tests) |
| HIPAA Safe Harbor completers | `pytest tests/unit/test_clinical_detectors.py::TestHIPAASafeHarborCompletors -v` (8 tests) |
| PAN/CVV/IBAN/IPv4 detectors | `pytest tests/unit/test_detectors.py -v` |
| HIPPA spelling guard | `grep -ri HIPPA src/ tests/ src/decoy_engine/disguises/` returns 0 hits |

## Adding detectors

1. Add the detector function `detect_<id>(series, col_name) -> Optional[DetectorMatch]` to `detectors.py`.
2. Add an entry to `_NAME_HINTS` if the detector uses name-based threshold relaxation.
3. Append `detect_<id>` to `REGISTERED_DETECTORS`.
4. Reference the new detector ID in the relevant Disguise YAML's `triggers.any_detectors` or `co_occurrence` lists.
5. Write tests in `tests/unit/test_<id>_detector.py` — at minimum: positive case, name-hint case, rejection case.

The detector name convention (`fn.__name__.replace('detect_', '')`) means the existing Disguise loader test auto-picks it up as a known ID — the `assert known_ids` test in `test_disguises_loader.py` will fail if you add a detector ID to a YAML without registering the function.
