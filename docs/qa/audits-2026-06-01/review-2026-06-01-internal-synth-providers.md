# QA Review — engine-side: internal/ · transforms/date_shift · disguises/ — 2026-06-01

Full report lives in:
`decoy-platform/docs/audit/qa-review-2026-06-01-internal-synth-providers.md`

## Engine-side findings summary

| # | Sev | Module | Finding |
|---|-----|--------|---------|
| F3 | HIGH | `internal/logging.py` | Always creates `RotatingFileHandler` — crashes on read-only fs |
| F5 | MEDIUM | `transforms/date_shift.py:apply` | HMAC computed for null rows (wasted CPU) |
| F7 | MEDIUM | `internal/faker_setup.py:make_faker` | Silent locale fallback — no log warning |
| F8 | MEDIUM | `disguises/loader.py:load_disguises` | Single malformed YAML aborts entire bundle load |
| F9 | LOW | `transforms/date_shift.py:validate_rule` | `min_days`/`max_days` not type-validated at rule-check time |
| F12 | LOW | `internal/crypto.py:deterministic_hash` | No `DeprecationWarning` on legacy SHA256(value+seed) function |
| F13 | NIT | `internal/logging.py:ProgressLogger` | `import time` inside method bodies; non-atomic counter |

Proposed sprint: **QA-8** (see full report).
