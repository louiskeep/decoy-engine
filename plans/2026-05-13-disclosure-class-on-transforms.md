# Block 6 — disclosure class on transforms (engine side)

> **Status:** plan — part of the 2026-05-13 audit-fix series.
> **Standalone:** light, no dependencies.
> **Pairs with:** `decoy-platform/plans/2026-05-13-disclosure-badges-and-job-metadata.md`.

## Why

GDPR Recital 26 / WP216 require honesty about whether the output is **pseudonymous** (still personal data — Decoy's HMAC-SHA256 hash falls here) or **anonymous** (out of scope). Today the engine does not declare either. The masker emits a CSV; no metadata says "this column is pseudonymized, joinable on the same key."

The reference's #1 must-have is "Honest taxonomy in the UI." The engine side of that is: every strategy declares its disclosure class, and the masker emits a per-job summary the platform can render.

## Code paths

- `src/decoy_engine/transforms/base.py` — declare `disclosure_class` class attribute.
- Each `src/decoy_engine/transforms/<strategy>.py` — set the class attribute.
- `src/decoy_engine/masker/masker.py` — collect per-column classes, compute worst-case, emit `JobOutputDisclosure`.
- `src/decoy_engine/transforms/__init__.py` or `registry.py` — export the enum so the platform can import it.
- Tests assert every registered strategy declares a class.

## Engine changes

### 1. Declare the class attribute

```python
# transforms/base.py
from typing import Literal

DisclosureClass = Literal["pseudonymous", "anonymous", "display_only"]


class BaseMaskingStrategy:
    disclosure_class: DisclosureClass  # abstract — every subclass must set this
```

### 2. Set on every strategy

| Strategy | `disclosure_class` | Note |
|---|---|---|
| `passthrough` | `display_only` | Column intentionally left as-is. |
| `hash` (keyed HMAC) | `pseudonymous` | Joinable on key; reversible by attacker with key. |
| `hash` (legacy seeded) | `pseudonymous` | Strictly worse: also reversible by dictionary attack on low-entropy inputs. Same legal class. |
| `redact` | `anonymous` | Value destroyed. |
| `truncate` | `anonymous` | Many-to-one. |
| `bucketize` | `anonymous` | Generalization. |
| `shuffle` | `anonymous` | Within-column permutation; column-level only. |
| `map` (faker/fixed/manual) | `pseudonymous` | Deterministic per-value mapping. |
| `faker` (transform) | `pseudonymous` | Same. |
| `date_shift` (value-keyed) | `pseudonymous` | Deterministic. |
| `date_shift` (subject-keyed, Block 4) | `pseudonymous` | Still deterministic, just per-subject. |
| `formula` | `display_only` | Caller is responsible; report worst-case for the dataset. |
| `format_preserving_feistel` | `pseudonymous` | Reversible with key. |
| `fpe_ff1` | `pseudonymous` | Same. |
| `reference` | `display_only` | Carries another column through. |

When a strategy has multiple modes (e.g. `formula`), set the class to the **most-permissive worst case** (`display_only`) and document.

### 3. `JobOutputDisclosure` summary

```python
# masker/masker.py
@dataclass
class JobOutputDisclosure:
    per_column: dict[str, DisclosureClass]
    worst_case: DisclosureClass
    notes: list[str] = field(default_factory=list)
```

Worst-case ordering: `display_only` > `pseudonymous` > `anonymous` (i.e. any `display_only` column poisons the dataset's headline class to `display_only`; any `pseudonymous` makes the dataset `pseudonymous` at best).

The masker collects per-column classes during `run`, computes worst-case, attaches `JobOutputDisclosure` to whatever the masker already returns (today that's the output file path; promote to a dataclass `MaskResult(output_path, disclosure: JobOutputDisclosure, quality: Optional[QualityReport])` — Block 7 will add the third field).

For columns with no rule (passed through), class defaults to `display_only`.

### 4. Notes the engine emits

The `notes` list is a stable channel for compliance copy the UI can render verbatim:

- `pseudonymous` columns: "This output remains personal data under GDPR Recital 26; pseudonymization can be reversed with the master key."
- `anonymous` columns: "Per WP216 three-tests analysis, see compliance report."
- `display_only` columns: "This column is unchanged; remove or transform before sharing."

## Wire format

`MaskResult.disclosure.to_dict()` is JSON-serializable and the platform persists it on `Job` (Block 6 platform doc).

## Tests to add

`tests/unit/test_disclosure_class.py`:
- Every key in `transforms/registry.py:_STRATEGIES` resolves to a class with a non-None `disclosure_class`.
- Worst-case logic on a mixed-strategy job: 1 pseudonymous + 1 anonymous + 1 display_only → worst-case `display_only`.
- All-anonymous job → `anonymous`.
- Empty job (no columns) → `display_only` (vacuous; documented).

## Verification

1. `pytest tests/unit/test_disclosure_class.py -v` — green.
2. Smoke run a HIPAA disguise job; confirm `MaskResult.disclosure.worst_case == "pseudonymous"` (because SSN is hashed, not redacted).

## Risk

None substantive — additive change. The class attribute is read at registration time; absence raises a clear `NotImplementedError` so missed strategies fail fast in CI.
