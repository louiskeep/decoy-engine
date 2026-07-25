# TX — Free-text NER Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Code blocks are REPRESENTATIVE.** Built from a structural investigation (file:line grounding), not a verbatim read. The `text_redact` NER path is the working reference to copy — open it and mirror it exactly rather than pasting this plan's snippets blind.

**Goal:** Bring the already-built spaCy NER capability from `text_redact` to `text_mask` so free-text named entities **synthesize** (person names → faker, etc.) instead of being blunt-redacted, and formally activate + document the existing `text_redact` NER path.

**Architecture:** A full spaCy adapter (`storm/ner.py`) and the span-injection contract (`Span`, `iter_spans(extra_spans=...)`) already exist and are wired end-to-end for `text_redact` — config flag, compile-time availability check, determinism version-pin, out-of-core parity, and tests. TX copies that proven pattern into the `text_mask` handler and its out-of-core twin, adds the version guard, and fixes the one missing default (`location`). No new NER machinery is invented.

**Tech Stack:** spaCy `>=3.7,<4` (already an optional `ner` extra in `pyproject.toml:160`), `en_core_web_sm`, the existing `Span`/`iter_spans`/`iter_ner_spans` plumbing.

**Design doc (rationale + code trace):** `~/.claude/plans/decoy-freetext-ner-plan.md`.

## Global Constraints

- **Reuse the `text_redact` pattern verbatim in shape** — do not design a new NER integration. The reference implementation is `execution/_strategies/_text_redact.py` (config read `:71-88`, `iter_ner_spans` call `:154-158`, version guard `:96-112`) + its OOC twin `execution/out_of_core/_mask_group_b.py:199-249` + compile check `plan/_checks.py:360-391`.
- **Out-of-core parity is strict.** Any `text_mask` NER change MUST touch both the in-core handler AND the group_c OOC path, or the parity harness (`tests/parity/test_out_of_core_group_b_parity.py:320-321`) fails. Never land one without the other.
- **Determinism.** spaCy greedy decoding is deterministic per pinned model; a new NER-consuming strategy must stamp `ner_model_version` and raise on drift, exactly like `text_redact`.
- **Optional dep discipline.** NER is off by default (per-cell model cost is real — `iter_ner_spans` runs inside the row loop). Tests gate on the existing `@needs_ner` skipif (`tests/unit/storm/test_ner_spans.py:22`), mirroring the ml/geo `importorskip` convention.
- **Gate.** Dennis review before merge; this is de-identification-critical (a missed entity is a PII leak), so a Codex cross-model pass on TX-2 is warranted.

**Where things live today (reconcile against these):**
- `src/decoy_engine/storm/ner.py` — `iter_ner_spans(text, *, model, entities)` `:132-155`; `NER_ENTITY_MAP` `:22-27`,`:49-55` (emits `person_name`, `location`; DATE/ORG/address deliberately excluded).
- `src/decoy_engine/storm/detectors.py` — `Span(detector_id, start, end, matched_text)` `:872-884`; `iter_spans(text, detector_ids, *, extra_spans=None)` `:988`.
- `src/decoy_engine/transforms/text_mask.py` — `mask_cell(..., extra_spans=None)` `:340`,`:404`; `DETECTOR_DEFAULTS` `:102-133` (**no `location` key** → location spans fall to the `redact` default at `:416`).
- `src/decoy_engine/execution/_strategies/_text_mask.py` — handler `:41-93` (reads `detectors`/`per_detector_strategy`/`unmatched_span_policy`/`token`/`min_days`/`max_days` — **no `ner` key**).
- `src/decoy_engine/execution/out_of_core/_mask_group_c.py` — `text_mask` OOC twin `:130-170` (**no NER**). (`text_redact` OOC lives in group_b.)
- `src/decoy_engine/plan/_seed_envelope.py` — stamps `ner_model_version` `:238-252` (**text_redact only**).
- `src/decoy_engine/plan/_types.py:114` — `ColumnSeed.ner_model_version`.

## File Structure

- Modify: `src/decoy_engine/transforms/text_mask.py` — add a `location` default to `DETECTOR_DEFAULTS` (TX-2).
- Modify: `src/decoy_engine/execution/_strategies/_text_mask.py` — read `ner` config, call `iter_ner_spans`, forward `extra_spans` (TX-2).
- Modify: `src/decoy_engine/execution/out_of_core/_mask_group_c.py` — mirror the NER path (TX-2).
- Modify: `src/decoy_engine/plan/_seed_envelope.py` — stamp `ner_model_version` for `text_mask` (TX-2).
- Modify: `src/decoy_engine/plan/_checks.py` — a `check_text_mask_ner_available` mirroring the text_redact check (TX-2).
- Create: `docs/guides/free-text-ner.md` — activation + config guide (TX-1).
- Tests: `tests/unit/execution/test_text_mask_ner.py`, additions to `tests/unit/transforms/test_text_mask.py`, and the OOC parity harness.

---

## TX-1 — Activate `text_redact` NER (ops + docs; ~zero engineering)

### Task 1: Document activation + a `@needs_ner` smoke test

For person names + locations, `text_redact` NER is done in code. Deliverable is a doc/demo + one smoke test that proves it end-to-end on a real model.

**Files:**
- Create: `docs/guides/free-text-ner.md`
- Test: additions to `tests/unit/execution/test_text_redact.py`

- [ ] **Step 1: Write the failing/gated smoke test**

```python
# tests/unit/execution/test_text_redact.py  (add)
import pytest
from tests.helpers.ner import needs_ner   # reconcile: the @needs_ner marker at tests/unit/storm/test_ner_spans.py:22

@needs_ner
def test_text_redact_ner_redacts_person_name():
    from decoy_engine.execution._strategies._text_redact import run_text_redact  # reconcile entry point
    out = run_text_redact(
        values=["Contact Jane Doe in Boston about the claim."],
        provider_config={"ner": True},
    )
    assert "Jane Doe" not in out[0]
    assert "Boston" not in out[0]      # location entity also caught
```

- [ ] **Step 2: Run test**

Run: `pytest tests/unit/execution/test_text_redact.py -k ner_redacts_person -v`
Expected: If `spacy` + `en_core_web_sm` installed → PASS. If not → SKIPPED (the `@needs_ner` gate). Install with:

```bash
pip install 'decoy-engine[ner]' && python -m spacy download en_core_web_sm
```

- [ ] **Step 3: Write the activation guide**

Create `docs/guides/free-text-ner.md` covering: install the `[ner]` extra + `en_core_web_sm`; set `ner: true` (or `ner: {model, entities}`) in `provider_config` on a `text_redact` strategy; the determinism model-version pin (jobs fail closed on model drift); the per-cell cost caveat (off by default); current entity coverage (person_name, location) and the TX-3 roadmap for dates/addresses.

- [ ] **Step 4: Commit**

```bash
git add docs/guides/free-text-ner.md tests/unit/execution/test_text_redact.py
git commit -m "docs(ner): activation guide + text_redact NER smoke test (TX-1)"
```

---

## TX-2 — Wire NER into `text_mask` (the near-term win)

### Task 2: Add a `location` default to `DETECTOR_DEFAULTS`

`NER_ENTITY_MAP` emits `location`, but `text_mask.DETECTOR_DEFAULTS` has no `location` key, so an injected location span silently falls to `redact` (`text_mask.py:416`). Give it a synthesizing default so location entities are masked coherently, not blunt-redacted.

**Files:**
- Modify: `src/decoy_engine/transforms/text_mask.py` (`DETECTOR_DEFAULTS` `:102-133`)
- Test: additions to `tests/unit/transforms/test_text_mask.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/transforms/test_text_mask.py  (add)
from decoy_engine.transforms.text_mask import mask_cell
from decoy_engine.storm.detectors import Span

def test_injected_location_span_uses_location_default_not_redact():
    text = "Seen in Chicago last week."
    span = Span("location", text.index("Chicago"), text.index("Chicago") + len("Chicago"), "Chicago")
    out = mask_cell(text, extra_spans=[span], seed=b"s")   # reconcile mask_cell signature at :340
    assert "Chicago" not in out
    assert "[REDACTED" not in out      # location now synthesizes, not blunt-redact
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/transforms/test_text_mask.py -k location_default -v`
Expected: FAIL — output contains a redaction token (no `location` default today)

- [ ] **Step 3: Write minimal implementation**

```python
# text_mask.py — DETECTOR_DEFAULTS (:102-133), add:
"location": "faker",   # or "geo_generalize" — pick per product intent; faker keeps a plausible place name
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/transforms/test_text_mask.py -k location_default -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/transforms/text_mask.py tests/unit/transforms/test_text_mask.py
git commit -m "fix(text_mask): add location default so location spans synthesize not redact (TX-2)"
```

### Task 3: Read `ner` config in the `text_mask` handler + forward spans

Mirror the `text_redact` handler pattern into `_strategies/_text_mask.py`.

**Files:**
- Modify: `src/decoy_engine/execution/_strategies/_text_mask.py` (`:41-93`)
- Test: `tests/unit/execution/test_text_mask_ner.py`

**Interfaces:**
- Consumes: `provider_config["ner"]` — `True` or `{model, entities}`, mirroring `_text_redact.py:71-88`; `iter_ner_spans` from `storm/ner.py:132`.
- Produces: the handler forwards NER spans through the existing `mask_cell(..., extra_spans=...)` param.

- [ ] **Step 1: Write the failing test (deterministic via stubbed NER)**

```python
# tests/unit/execution/test_text_mask_ner.py
from unittest.mock import patch
from decoy_engine.storm.detectors import Span

def test_text_mask_routes_ner_person_to_faker():
    text = "Call Jane Doe today."
    fake_span = [Span("person_name", text.index("Jane Doe"), text.index("Jane Doe") + 8, "Jane Doe")]
    with patch("decoy_engine.execution._strategies._text_mask.iter_ner_spans", return_value=fake_span):
        from decoy_engine.execution._strategies._text_mask import run_text_mask  # reconcile entry point
        out = run_text_mask(values=[text], provider_config={"ner": True})
    assert "Jane Doe" not in out[0]     # name synthesized via person_name default (faker)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/execution/test_text_mask_ner.py -k routes_ner_person -v`
Expected: FAIL — handler ignores `ner` (name passes through unmasked)

- [ ] **Step 3: Write minimal implementation**

Copy the shape from `_text_redact.py:71-88,154-158`:

```python
# _text_mask.py handler
from decoy_engine.storm.ner import iter_ner_spans
ner_cfg = provider_config.get("ner")
def _spans_for(cell):
    if not ner_cfg:
        return None
    model = ner_cfg.get("model") if isinstance(ner_cfg, dict) else None
    entities = ner_cfg.get("entities") if isinstance(ner_cfg, dict) else None
    return iter_ner_spans(cell, model=model, entities=entities)
# then per cell:
out = mask_cell(cell, extra_spans=_spans_for(cell), ...)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/execution/test_text_mask_ner.py -k routes_ner_person -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/_strategies/_text_mask.py tests/unit/execution/test_text_mask_ner.py
git commit -m "feat(text_mask): read ner config, forward NER spans to mask_cell (TX-2)"
```

### Task 4: Stamp `ner_model_version` for `text_mask` + drift guard

Without the version pin, a model upgrade silently changes output — breaking determinism/reproducibility. Mirror `text_redact`'s stamp + guard.

**Files:**
- Modify: `src/decoy_engine/plan/_seed_envelope.py` (`:238-252`)
- Modify: `src/decoy_engine/execution/_strategies/_text_mask.py` (mismatch guard, mirroring `_text_redact.py:96-112`)
- Test: additions to `tests/unit/execution/test_text_mask_ner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/execution/test_text_mask_ner.py  (add)
import pytest
from unittest.mock import patch
from decoy_engine.errors import StrategyError   # reconcile

def test_text_mask_ner_model_drift_fails_closed():
    with patch("decoy_engine.execution._strategies._text_mask.installed_model_version", return_value="v9.9"):
        from decoy_engine.execution._strategies._text_mask import run_text_mask
        with pytest.raises(StrategyError, match="ner_model_version_mismatch"):
            run_text_mask(values=["x"], provider_config={"ner": True},
                          column_seed={"ner_model_version": "v1.0"})  # stamped != installed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/execution/test_text_mask_ner.py -k drift -v`
Expected: FAIL — no guard yet (no error raised)

- [ ] **Step 3: Write minimal implementation**

In `_seed_envelope.py:238-252`, extend the `ner_model_version` stamping so it also fires when a `text_mask` strategy sets `ner`. In `_text_mask.py`, add the runtime check mirroring `_text_redact.py:96-112`:

```python
if ner_cfg:
    installed = installed_model_version(model)
    stamped = column_seed.get("ner_model_version")
    if stamped is not None and stamped != installed:
        raise StrategyError("ner_model_version_mismatch", ...)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/execution/test_text_mask_ner.py -k drift -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/plan/_seed_envelope.py src/decoy_engine/execution/_strategies/_text_mask.py tests/unit/execution/test_text_mask_ner.py
git commit -m "feat(text_mask): stamp + guard ner_model_version for determinism (TX-2)"
```

### Task 5: Out-of-core parity — mirror NER into `_mask_group_c.py`

The strict parity harness requires in-core and OOC to agree byte-for-byte.

**Files:**
- Modify: `src/decoy_engine/execution/out_of_core/_mask_group_c.py` (`:130-170`)
- Test: extend `tests/parity/test_out_of_core_group_b_parity.py` (or its group_c sibling)

- [ ] **Step 1: Write the failing parity test**

```python
# tests/parity/...  (add — reconcile with the existing parity harness at group_b :320-321)
def test_text_mask_ner_incore_matches_out_of_core():
    cfg = {...text_mask strategy with "ner": True over a name-bearing column...}
    incore = run_incore(cfg)
    ooc = run_out_of_core(cfg, budget_bytes=SMALL)
    assert incore.equals(ooc)     # byte-identical
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/parity/ -k text_mask_ner -v`
Expected: FAIL — OOC group_c ignores NER, so output diverges

- [ ] **Step 3: Write minimal implementation**

Mirror the group_b NER handling (`_mask_group_b.py:199-249`) into group_c's text_mask path: build `extra_spans` per cell the same way, apply the same version pin.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/parity/ -k text_mask_ner -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/out_of_core/_mask_group_c.py tests/parity/
git commit -m "feat(text_mask): out-of-core NER parity in group_c (TX-2)"
```

### Task 6: Compile-time availability check for `text_mask` NER

Fail at compile with a clear message if `ner` is requested but spaCy/model isn't installed — don't crash mid-run.

**Files:**
- Modify: `src/decoy_engine/plan/_checks.py` (mirror `check_text_redact_ner_available` `:360-391`) + wire into `plan/_compile.py`
- Test: additions to `tests/unit/execution/test_text_mask_ner.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/execution/test_text_mask_ner.py  (add)
from unittest.mock import patch
import pytest
from decoy_engine.errors import ConfigError

def test_text_mask_ner_missing_spacy_fails_at_compile():
    from decoy_engine.config import compile_plan
    cfg = {...text_mask + "ner": True...}
    with patch("decoy_engine.plan._checks.spacy_installed", return_value=False):
        with pytest.raises(ConfigError, match="spacy"):
            compile_plan(cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/execution/test_text_mask_ner.py -k missing_spacy -v`
Expected: FAIL — no compile check for text_mask ner

- [ ] **Step 3: Write minimal implementation**

Add `check_text_mask_ner_available(plan)` mirroring `_checks.py:360-391`; wire it into the compile pass next to the text_redact check (`plan/_compile.py:190,484`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/execution/test_text_mask_ner.py -k missing_spacy -v`
Expected: PASS

- [ ] **Step 5: Run the full text-mask + NER suites (no regression)**

Run: `pytest tests/unit/execution/test_text_mask_ner.py tests/unit/transforms/test_text_mask.py tests/unit/storm/test_ner_spans.py -v`
Expected: PASS (NER-gated cases SKIPPED if model absent)

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/plan/_checks.py src/decoy_engine/plan/_compile.py tests/unit/execution/test_text_mask_ner.py
git commit -m "feat(text_mask): compile-time NER availability check (TX-2)"
```

---

## Later: TX-3 — date + address NER coverage — NOT in this plan

Larger and design-reopening (full rationale in `~/.claude/plans/decoy-freetext-ner-plan.md`). Outline only:

1. Reverse the deliberate DATE/ORG/address exclusion in `NER_ENTITY_MAP` (`ner.py:22-27`).
2. Add DATE → `date_shift` with format detection (spaCy `DATE` spans are free-text; must parse to a shiftable value).
3. Add address/GPE → address mapping + a `text_mask` address default.
4. Known limitation: spaCy `PERSON` does **not** split first/last name, so the `first_name`/`last_name` tier-2 ids are unreachable from the standard model — needs a second pass or a different model; decide scope before starting.

Distinct from the DEFERRED-V3 `ML6` Presidio/GLiNER item. Complements HC-7 (which flags clinical free-text columns but currently leaves them to redact — TX-2 is what lets those columns synthesize). Do NOT start TX-3 until free-text dates/addresses are a confirmed priority.

## Self-review notes
- Spec coverage: TX-1 = Task 1; TX-2 = Tasks 2–6; TX-3 outlined, out of scope.
- Ordering matters: Task 2 (`location` default) before Task 3 (handler wiring) so the first end-to-end NER run doesn't blunt-redact locations.
- Parity is the trap: Task 5 is not optional — Task 3 alone would break the OOC parity harness. Land Tasks 3–5 as a set behind one review.
- Type consistency: `Span(detector_id, start, end, matched_text)`, `iter_ner_spans(text, *, model, entities)`, `mask_cell(..., extra_spans=...)` used identically across tasks and matching the cited source lines.
