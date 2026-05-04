# Disguises — engine

Scope: the 8 launch Disguises and the field-detector layer FORECAST consults to recommend them. Disguises are the primary differentiator in the brand reference: every regulated shop has a compliance officer asking "how do you ensure developers don't touch production PII?" — Disguises are the answer that gets Decoy through the door.

## Cross-cutting rules

- **HIPAA, never HIPPA.** A misspelling in a regulation name kills credibility instantly. CI grep gate.
- **Disguise = bundle. Mask = single transform.** Don't blur the line.
- **Disguises are data, not code.** Each is a YAML file. Adding or editing a Disguise must not require a code change.

## Definition format

Each Disguise lives at `src/decoy_engine/disguises/<id>.yaml`:

```yaml
id: hipaa
name: HIPAA Disguise
regulation: "HIPAA Privacy Rule — Safe Harbor (45 CFR 164.514(b)(2))"
version: 1
primary_buyer: "Healthcare data engineers"
summary: "PHI Safe Harbor: 18 identifiers, date shifting, ZIP truncation, age 89+ handling"

# Field-level masking rules. field_pattern is matched by:
#  1) field-name regex against the column name, AND/OR
#  2) detector ids that fired on the column (cross-reference detectors/).
rules:
  - match:
      name_regex: "(?i)^(ssn|social.?security)"
      detectors: ["ssn"]
    mask: hash.sha256_truncated
    notes: "SSN — irreversible hash, keeps referential integrity across joins."
  - match:
      name_regex: "(?i)^(dob|birth.?date|date.?of.?birth)"
      detectors: ["us_date", "iso_date"]
    mask: date_shift
    params: { jitter_days: 30 }
  - match:
      name_regex: "(?i)^zip(.?code)?$"
      detectors: ["us_zip"]
    mask: truncate
    params: { keep: 3 }
    notes: "Safe Harbor: keep first 3 digits unless population <20,000."
  - match:
      name_regex: "(?i)^age$"
    mask: age_top_code
    params: { cap: 89 }

# FORECAST recommendation hints.
forecast:
  triggers_when:
    any_of:
      - detectors_match_any: ["ssn"]
      - detectors_match_any: ["icd10", "npi", "mrn"]
      - co_occurrence: ["us_date", "us_zip", "person_name"]
  match_score_weight: 1.0
```

Schema validation lives in `src/decoy_engine/disguises/schema.py` (Pydantic). Loading any malformed Disguise fails CI.

## Launch set (8 files)

| File | Regulation | Primary buyer |
|---|---|---|
| `hipaa.yaml` | HIPAA Privacy Rule — Safe Harbor 18 identifiers, date shifting, ZIP truncation, age 89+ top-coding | Healthcare data engineers |
| `pci.yaml` | PCI DSS — PAN tokenization, CVV stripping, BIN preservation | Fintech, e-commerce |
| `glba.yaml` | GLBA — SSN, account numbers, balances | Banking, insurance |
| `gdpr.yaml` | GDPR — names, emails, IPs, device IDs | EU-facing products |
| `ccpa.yaml` | CCPA — California consumer PII with opt-out markers | US consumer tech |
| `ferpa.yaml` | FERPA — student names, SSN, grades, disciplinary | EdTech, universities |
| `sox.yaml` | SOX — earnings, forecasts, pre-public figures | Public companies |
| `default.yaml` | Generic PII starter pack: names, emails, phones, addresses | Everyone |

Each Disguise includes the rule list summarized above. Use the brand reference as the source of truth for what each one *must* cover; the team can extend later.

## Field detectors

Detectors are reusable; one detector can be triggered by many Disguises. Layout:

```
src/decoy_engine/storm/detectors/
  ssn.py
  us_phone.py
  us_zip.py
  email.py
  ip.py
  icd10.py
  npi.py
  mrn.py
  pan.py            # Luhn-checking credit-card-like
  cvv.py
  iban.py
  us_date.py
  iso_date.py
  person_name.py    # uses Faker name list as a hint, distribution heuristics
  account_number.py
  ...
```

Each detector exposes `def detect(field_stats: FieldStats) -> DetectorResult`. Detectors run as part of STORM and write their hits into `FieldStats.regex_matches`. FORECAST then reads those hits — it does not rerun detection on raw data.

## FORECAST wiring

`decoy_engine.forecast.recommender` at startup loads every YAML in `disguises/` and compiles a list of `(disguise, trigger_predicate, score_weight)`. For each StormProfile it:
1. Evaluates each Disguise's trigger against the profile's detector hits.
2. Ranks by `match_score = sum(matched_field_weights) * disguise.score_weight`.
3. Returns the ranked list as `DisguiseRecommendation`s.

The brand reference's example flow ("3 fields match SSN format → apply HIPAA Disguise?") falls out of this naturally.

## Spelling guard

CI grep gate: `grep -ri "HIPPA" src/ tests/ disguises/` must return zero hits. Add a pre-commit hook too.

## Sequencing

1. Land the schema + loader + 1 Disguise (`default.yaml`) and a detector for `email`. Get the loop working.
2. Add `hipaa.yaml` + the medical-related detectors (icd10, npi, mrn, ssn, us_date, us_zip).
3. Add the remaining 6 Disguises in any order; each is independent.

## Verification

- Pydantic schema validates every Disguise on test load.
- Per-Disguise golden test: feed a synthetic dataset that should match it; assert FORECAST returns it ranked first with the expected fields flagged.
- Negative test: feed an unrelated dataset; assert the Disguise scores below threshold.
- `grep -ri "HIPPA" .` returns zero hits.
- Loading time benchmark: STORM scan + FORECAST recommend on a 1M-row table completes inside an acceptable budget (set during impl; document the number).
