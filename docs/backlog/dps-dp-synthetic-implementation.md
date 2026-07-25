# DPS — DP Synthetic Data (Path A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Code blocks are REPRESENTATIVE.** This plan was built from a structural investigation (file:line grounding), not a verbatim read of every function body. Before implementing each task, open the cited `file:line` and reconcile the exact signature/shape. Do NOT paste plan code blind.

**Goal:** Make the per-column marginals of Decoy's `mode: generate` output honestly (ε,δ)-differentially private end-to-end — with a data-independent support, a composed privacy budget, and a lifted disclaimer — so Decoy can make a defensible "marginal DP synthetic data" claim comparable to Gretel/MOSTLY AI (for marginals; joint-distribution DP is the separate DPS-4 effort).

**Architecture:** The generate path already reads ONLY the snapshot artifact (verified: no raw-data re-touch — `generation/statistical/_spec.py` + `_sample.py`, `generators/_distribution.py`), so post-processing immunity already holds structurally. The three sub-programs are: **DPS-1** make the snapshot *support* data-independent (fixed numeric bin ranges + a DP threshold-released category label set) so the release is truly ε-DP rather than noised-counts-over-real-support; **DPS-2** add a privacy-budget accountant that composes every noisy release into one stated (ε,δ); **DPS-3** add a contract test proving generate-consumes-only-artifact, hard-reject the anti-DP knobs, and narrow the disclaimer.

**Tech Stack:** Python 3.11+, numpy, pandas, the existing `distribution-snapshot/v1` artifact, the Laplace mechanism in `quality/dp.py`.

**Design doc (rationale + code trace):** `~/.claude/plans/decoy-dp-synthetic-plan.md`.

## Global Constraints

- **Pre-GA format freedom.** `RELEASE_PHASE` is pre-ga; the `distribution-snapshot/v1` schema may change freely now (the corpus freeze happens once, later). Bump `schema_version` if the snapshot shape changes.
- **Cite established methodology in the module docstring** (per platform CLAUDE.md core rule). DP primitives cite: Dwork & Roth, *The Algorithmic Foundations of Differential Privacy* (Laplace mechanism, sequential composition); the stable-histogram / "propose-test-release" pattern for the category-set release (Korolova et al. 2009 / Dwork & Roth §3).
- **Determinism.** All noise draws from a seeded `numpy.random.Generator` threaded from the job seed — reproducible across processes. No `Date.now()`/unseeded RNG.
- **Fail-closed.** A DP mode that cannot guarantee its precondition (e.g. missing numeric domain) raises, never silently degrades to a non-DP release.
- **Module size** ≤ 600 LOC for orchestration modules; `dp.py` is already close — put the accountant in a new module.
- **Gate.** Dennis review + a Codex cross-model pass (this is privacy-critical) before any merge to main.

**Where things live today (reconcile against these):**
- `src/decoy_engine/quality/dp.py` — `apply_dp_noise(snapshot, *, epsilon, rng)` at `:80`; noises counts; rejects `joints` at `:124-133`; writes the `dp` metadata block at `:189-195`; hard-coded sensitivity 1 at `:136`.
- `src/decoy_engine/generation/snapshot.py` — `compute_distribution_snapshot` at `:114`; `_numeric_stats`/`_categorical_stats`/`_datetime_stats` at `:373-464`; numeric bin edges from real min/max at `:392-393`,`:412`; `high_cardinality` retention at `:266-370`.
- `src/decoy_engine/generation/statistical/_spec.py` — `_load_snapshot` at `:80-102`; `allow_real_categories` gate at `:177-185`.
- `docs/what-we-cannot-prove.md` — data-dependent support caveat at `:26-30`; blanket disclaimer at `:39-41`.

## File Structure

- Create: `src/decoy_engine/quality/dp_budget.py` — `PrivacyBudget` accountant (DPS-2). One responsibility: track and compose (ε,δ) across labeled releases.
- Modify: `src/decoy_engine/generation/snapshot.py` — accept a caller-supplied `numeric_domains` (fixed bin ranges) and record support origin (DPS-1).
- Modify: `src/decoy_engine/quality/dp.py` — threshold-released category set; wire the accountant; emit `epsilon_total` (DPS-1, DPS-2).
- Modify: `src/decoy_engine/generation/statistical/_spec.py` (+ the generate-mode config surface) — a `dp:` generate flag; hard-reject `allow_real_categories` / `high_cardinality:true` under DP (DPS-3).
- Modify: `docs/what-we-cannot-prove.md` — narrow the disclaimer to the proven marginal claim (DPS-3).
- Tests: `tests/unit/quality/test_dp_budget.py`, additions to `tests/unit/quality/test_dp.py`, `tests/unit/generation/test_snapshot_dp_support.py`, `tests/unit/generation/test_generate_dp_contract.py`.

---

## DPS-2 — Privacy-budget accountant (do first; foundational)

### Task 1: `PrivacyBudget` accountant

**Files:**
- Create: `src/decoy_engine/quality/dp_budget.py`
- Test: `tests/unit/quality/test_dp_budget.py`

**Interfaces:**
- Produces: `PrivacyBudget` with `.charge(label: str, *, epsilon: float, delta: float = 0.0, mechanism: str = "laplace") -> None`, `.total_epsilon() -> float`, `.total_delta() -> float`, `.breakdown() -> list[dict]`. Basic sequential composition (sum of ε, sum of δ) for Path A; an RDP accountant for Gaussian is deferred to DPS-4.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/quality/test_dp_budget.py
import pytest
from decoy_engine.quality.dp_budget import PrivacyBudget

def test_sequential_composition_sums_epsilon():
    b = PrivacyBudget()
    b.charge("col_a.histogram", epsilon=1.0)
    b.charge("col_b.histogram", epsilon=0.5)
    assert b.total_epsilon() == pytest.approx(1.5)
    assert b.total_delta() == 0.0
    assert len(b.breakdown()) == 2

def test_rejects_nonpositive_epsilon():
    b = PrivacyBudget()
    with pytest.raises(ValueError):
        b.charge("bad", epsilon=0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/quality/test_dp_budget.py -v`
Expected: FAIL — `ModuleNotFoundError: decoy_engine.quality.dp_budget`

- [ ] **Step 3: Write minimal implementation**

```python
# src/decoy_engine/quality/dp_budget.py
"""Privacy-budget accountant for the DP snapshot pipeline.

Basic sequential composition per Dwork & Roth, *The Algorithmic Foundations
of Differential Privacy*, Thm 3.16 (sum of epsilons, sum of deltas over a
sequence of DP mechanisms on disjoint or adaptive queries). RDP/zCDP tight
composition for the Gaussian mechanism is deferred to DPS-4 (PrivBayes).
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class _Charge:
    label: str
    epsilon: float
    delta: float
    mechanism: str


@dataclass
class PrivacyBudget:
    _charges: list[_Charge] = field(default_factory=list)

    def charge(self, label, *, epsilon, delta=0.0, mechanism="laplace"):
        if epsilon <= 0:
            raise ValueError(f"epsilon must be > 0, got {epsilon}")
        if delta < 0:
            raise ValueError(f"delta must be >= 0, got {delta}")
        self._charges.append(_Charge(label, float(epsilon), float(delta), mechanism))

    def total_epsilon(self):
        return sum(c.epsilon for c in self._charges)

    def total_delta(self):
        return sum(c.delta for c in self._charges)

    def breakdown(self):
        return [
            {"label": c.label, "epsilon": c.epsilon, "delta": c.delta, "mechanism": c.mechanism}
            for c in self._charges
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/quality/test_dp_budget.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/quality/dp_budget.py tests/unit/quality/test_dp_budget.py
git commit -m "feat(dp): add PrivacyBudget sequential-composition accountant (DPS-2)"
```

---

## DPS-1 — Data-independent support

### Task 2: Fixed numeric bin ranges (caller-supplied domain)

Today numeric bin edges come from the real `min`/`max` of the data (`snapshot.py:392-393`), so the histogram *range* leaks raw data even after counts are noised. Under DP, edges must be data-independent: caller-supplied. Absent a domain, DP mode fails closed.

**Files:**
- Modify: `src/decoy_engine/generation/snapshot.py` (`_numeric_stats` and `compute_distribution_snapshot` signature)
- Test: `tests/unit/generation/test_snapshot_dp_support.py`

**Interfaces:**
- Consumes: `compute_distribution_snapshot(frame, *, numeric_domains: dict[str, tuple[float, float]] | None = None, dp_mode: bool = False, ...)` — new optional params, defaulted so existing callers are unchanged.
- Produces: numeric column stats whose `bin_edges` derive from `numeric_domains[col]` when provided; each column stat carries `support_origin: "data" | "caller"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/generation/test_snapshot_dp_support.py
import pandas as pd, pytest
from decoy_engine.generation.snapshot import compute_distribution_snapshot

def test_numeric_domain_overrides_data_min_max():
    df = pd.DataFrame({"age": [31, 42, 55]})  # data range 31..55
    snap = compute_distribution_snapshot(
        df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True
    )
    stats = snap["columns"]["age"]["stats"]
    assert stats["bin_edges"][0] == 0.0
    assert stats["bin_edges"][-1] == 120.0
    assert snap["columns"]["age"]["support_origin"] == "caller"

def test_dp_mode_requires_domain_for_numeric():
    df = pd.DataFrame({"age": [31, 42, 55]})
    with pytest.raises(ValueError, match="numeric_domain"):
        compute_distribution_snapshot(df, dp_mode=True)  # no domain -> fail closed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/generation/test_snapshot_dp_support.py -v`
Expected: FAIL — `TypeError: unexpected keyword 'numeric_domains'` (params not added yet)

- [ ] **Step 3: Write minimal implementation**

Reconcile with the actual `_numeric_stats` at `snapshot.py:373-412`. Representative shape:

```python
# in compute_distribution_snapshot(...)  (snapshot.py:114)
def compute_distribution_snapshot(frame, *, numeric_domains=None, dp_mode=False, **kw):
    numeric_domains = numeric_domains or {}
    ...
    # per numeric column:
    if dp_mode and col not in numeric_domains:
        raise ValueError(
            f"dp_mode requires a data-independent numeric_domain for column {col!r}"
        )
    domain = numeric_domains.get(col)  # (lo, hi) or None
    col_stats = _numeric_stats(series, domain=domain)
    columns[col] = {..., "support_origin": "caller" if domain else "data"}

# in _numeric_stats(series, *, domain=None):  (snapshot.py:373)
lo, hi = domain if domain is not None else (float(series.min()), float(series.max()))
bin_edges = _linspace(lo, hi, n_bins)   # edges no longer read from data when domain given
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/generation/test_snapshot_dp_support.py -v`
Expected: PASS

- [ ] **Step 5: Run the existing snapshot suite (no regression for non-DP callers)**

Run: `pytest tests/unit/generation/ -k snapshot -v`
Expected: PASS (defaults keep the old data-derived behavior)

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/generation/snapshot.py tests/unit/generation/test_snapshot_dp_support.py
git commit -m "feat(dp): data-independent numeric bin ranges under dp_mode (DPS-1)"
```

### Task 3: DP threshold-released category label set (stable histogram)

Category labels are real source strings released exactly today — only their counts are noised — so a unique rare category leaks a real individual. Release a label only if its noised count clears a threshold τ; suppress the rest into `other`. This makes the released label *set* itself DP.

**Files:**
- Modify: `src/decoy_engine/quality/dp.py` (categorical branch, around `top_values`/`other_count` at `dp.py:164-168`)
- Test: additions to `tests/unit/quality/test_dp.py`

**Interfaces:**
- Consumes: `apply_dp_noise(snapshot, *, epsilon, rng, delta=1e-6)` — add `delta`.
- Produces: categorical stats whose `top_values` contains only labels whose noised count ≥ τ; suppressed mass folded into `other_count`. τ = `1 + ceil((1/epsilon) * ln(1 / (2*delta)))` (stable-histogram threshold).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/quality/test_dp.py  (add)
import numpy as np
from decoy_engine.quality.dp import apply_dp_noise

def _cat_snapshot(counts: dict[str, int]):
    return {
        "schema_version": "distribution-snapshot/v1",
        "row_count": sum(counts.values()),
        "columns": {"dx": {"kind": "categorical", "dtype": "object",
            "null_count": 0, "non_null_count": sum(counts.values()),
            "distinct_count": len(counts),
            "stats": {"top_values": [{"value": k, "count": v} for k, v in counts.items()],
                      "other_count": 0}}},
        "joints": [],
    }

def test_rare_category_suppressed_into_other():
    rng = np.random.default_rng(7)
    snap = _cat_snapshot({"common": 1000, "rare_unique_patient": 1})
    out = apply_dp_noise(snap, epsilon=0.5, delta=1e-6, rng=rng)
    labels = {tv["value"] for tv in out["columns"]["dx"]["stats"]["top_values"]}
    assert "rare_unique_patient" not in labels        # the leak is gone
    assert out["columns"]["dx"]["stats"]["other_count"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/quality/test_dp.py -k suppressed -v`
Expected: FAIL — the rare label survives (current code releases labels exactly)

- [ ] **Step 3: Write minimal implementation**

Reconcile with the categorical branch at `dp.py:164-168`. Representative:

```python
# dp.py — categorical branch
import math
def _threshold(epsilon, delta):
    return 1.0 + math.ceil((1.0 / epsilon) * math.log(1.0 / (2.0 * delta)))

tau = _threshold(epsilon, delta)
kept, suppressed_mass = [], 0
for tv in top_values:
    noised = max(0, round(tv["count"] + rng.laplace(0.0, 1.0 / epsilon)))
    if noised >= tau:
        kept.append({"value": tv["value"], "count": noised})
    else:
        suppressed_mass += noised
stats["top_values"] = kept
stats["other_count"] = max(0, round(other_count + rng.laplace(0.0, 1.0 / epsilon))) + suppressed_mass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/quality/test_dp.py -k suppressed -v`
Expected: PASS

- [ ] **Step 5: Run the full DP suite**

Run: `pytest tests/unit/quality/test_dp.py -v`
Expected: PASS (existing count-noising tests still hold; adjust any that asserted exact label retention — that assertion is the bug being fixed)

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/quality/dp.py tests/unit/quality/test_dp.py
git commit -m "feat(dp): threshold-release category label set (stable histogram) (DPS-1)"
```

---

## DPS-2 (cont.) — Compose the whole-snapshot budget

### Task 4: Charge every release; emit `epsilon_total`

Today `row_count`/`distinct_count` are noised (`dp.py:144-149`) but nothing accounts for the budget they spend, and the `(k+1)·ε` figure is docstring prose only. Charge each release to a `PrivacyBudget` and emit the composed total in the metadata block.

**Files:**
- Modify: `src/decoy_engine/quality/dp.py` (thread `PrivacyBudget`; extend the `dp` metadata block at `:189-195`)
- Test: additions to `tests/unit/quality/test_dp.py`

**Interfaces:**
- Produces: the snapshot's `dp` block gains `epsilon_total: float`, `delta_total: float`, `composition: "sequential"`, and `charges: list[{label, epsilon, delta, mechanism}]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/quality/test_dp.py  (add)
def test_reports_composed_epsilon_total():
    rng = np.random.default_rng(1)
    snap = _cat_snapshot({"a": 500, "b": 500})   # 1 categorical col + row_count + distinct_count
    out = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=rng)
    dp = out["dp"]
    assert dp["epsilon_total"] >= 1.0            # per-column + scalar releases all charged
    assert dp["composition"] == "sequential"
    assert any(c["label"].startswith("dx") for c in dp["charges"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/quality/test_dp.py -k composed -v`
Expected: FAIL — `KeyError: 'epsilon_total'`

- [ ] **Step 3: Write minimal implementation**

```python
# dp.py — at the top of apply_dp_noise
from decoy_engine.quality.dp_budget import PrivacyBudget
budget = PrivacyBudget()
# at each release site, after drawing noise:
budget.charge(f"{col}.histogram", epsilon=epsilon)         # per column
budget.charge("row_count", epsilon=epsilon)                # scalar releases
budget.charge(f"{col}.distinct_count", epsilon=epsilon)
# extend the metadata block (dp.py:189-195):
snapshot["dp"] = {
    "epsilon": epsilon, "delta": delta, "mechanism": "laplace",
    "sensitivity": 1, "adjacency": "add-remove-one-row",
    "scope": "per-column-histogram",
    "epsilon_total": budget.total_epsilon(),
    "delta_total": budget.total_delta(),
    "composition": "sequential",
    "charges": budget.breakdown(),
}
```

Decision to record in the docstring: either charge `distinct_count`/`row_count` (as above) or STOP releasing them under DP. Charging is simpler and honest; prefer it unless a downstream consumer needs an un-noised count (none found in the generate trace).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/quality/test_dp.py -k composed -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/quality/dp.py tests/unit/quality/test_dp.py
git commit -m "feat(dp): compose whole-snapshot budget, emit epsilon_total (DPS-2)"
```

---

## DPS-3 — Gate, proof, disclaimer

### Task 5: DP-mode config gate (mutual exclusion with anti-DP knobs)

`allow_real_categories` (`_spec.py:177-185`) and `high_cardinality: true` (`snapshot.py:266-370`) deliberately release real/full vocabulary — the opposite of DP. Under a DP generate mode they must be hard-rejected at compile.

**Files:**
- Modify: the generate-mode config model (where `dp`/`epsilon` for generation is declared) + `src/decoy_engine/generation/statistical/_spec.py`
- Test: `tests/unit/generation/test_generate_dp_contract.py`

**Interfaces:**
- Consumes: a generate-mode `dp: {epsilon: float, delta: float, numeric_domains: {col: [lo, hi]}}` config block.
- Produces: a compile-time `ConfigError` when `dp` is set together with `allow_real_categories: true` or any `high_cardinality: true` column.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/generation/test_generate_dp_contract.py
import pytest
from decoy_engine.config import compile_plan   # reconcile actual import
from decoy_engine.errors import ConfigError    # reconcile actual error type

def _dp_generate_cfg(**over):
    cfg = {"mode": "generate", "dp": {"epsilon": 1.0, "delta": 1e-6,
            "numeric_domains": {"age": [0, 120]}},
           "generate_tables": {"t": {"columns": {"dx": {"strategy": "statistical"}}}}}
    cfg.update(over); return cfg

def test_dp_rejects_high_cardinality():
    cfg = _dp_generate_cfg()
    cfg["generate_tables"]["t"]["columns"]["dx"]["high_cardinality"] = True
    with pytest.raises(ConfigError, match="high_cardinality"):
        compile_plan(cfg)

def test_dp_rejects_allow_real_categories():
    cfg = _dp_generate_cfg(allow_real_categories=True)
    with pytest.raises(ConfigError, match="allow_real_categories"):
        compile_plan(cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/generation/test_generate_dp_contract.py -v`
Expected: FAIL — config compiles without error (gate not present)

- [ ] **Step 3: Write minimal implementation**

Add the check in the compile/validation pass for generate mode:

```python
# generate-mode validation
if cfg.get("dp"):
    if cfg.get("allow_real_categories"):
        raise ConfigError("allow_real_categories cannot be combined with dp (releases real vocab)")
    for tname, t in cfg.get("generate_tables", {}).items():
        for cname, c in t.get("columns", {}).items():
            if c.get("high_cardinality"):
                raise ConfigError(
                    f"high_cardinality:true on {tname}.{cname} cannot be combined with dp "
                    "(retains full real vocabulary — incompatible with a DP release)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/generation/test_generate_dp_contract.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/ tests/unit/generation/test_generate_dp_contract.py
git commit -m "feat(dp): compile-time reject of anti-DP knobs under dp generate mode (DPS-3)"
```

### Task 6: Consume-only contract test + disclaimer lift

Encode the traced property (generation reads only the artifact) as a regression, then narrow the disclaimer from "nothing inherits" to the proven marginal claim.

**Files:**
- Test: additions to `tests/unit/generation/test_generate_dp_contract.py`
- Modify: `docs/what-we-cannot-prove.md` (`:39-41`)

- [ ] **Step 1: Write the failing/asserting test**

```python
# tests/unit/generation/test_generate_dp_contract.py  (add)
def test_generation_consumes_only_the_snapshot(tmp_path):
    """Sampling from a DP snapshot must not require or read the raw source frame.
    Contract lock for post-processing immunity."""
    from decoy_engine.generation.statistical._sample import sample_column
    from decoy_engine.generation.statistical._spec import load_spec_from_dict
    dp_snapshot = {  # a minimal DP'd categorical spec, no raw data present
        "kind": "categorical",
        "stats": {"top_values": [{"value": "A", "count": 480}, {"value": "B", "count": 520}],
                  "other_count": 0},
        "dp": {"epsilon_total": 1.0},
    }
    spec = load_spec_from_dict(dp_snapshot)     # reconcile actual loader entry point
    out = sample_column(spec, n=100, col_seed=b"seed", parent_values=None)
    assert len(out) == 100
    assert set(out) <= {"A", "B"}               # only labels present in the artifact
```

- [ ] **Step 2: Run test to verify it passes (property already holds)**

Run: `pytest tests/unit/generation/test_generate_dp_contract.py -k consumes_only -v`
Expected: PASS — this locks the current no-re-touch behavior so a future refactor can't silently break it. If it FAILS because the sampler reaches for raw data, that is a real finding — stop and fix the sampler first.

- [ ] **Step 3: Lift the disclaimer**

Replace the blanket statement at `docs/what-we-cannot-prove.md:39-41` with the narrowed, precondition-qualified claim:

```markdown
Generated **marginals** ARE (ε,δ)-differentially private by post-processing of the
DP snapshot — PROVIDED: (a) the snapshot was produced with `dp_mode` (data-independent
numeric domains + threshold-released category sets, DPS-1); (b) the composed budget is
the `dp.epsilon_total` recorded in the artifact (DPS-2); (c) generation reads only the
artifact (locked by `test_generation_consumes_only_the_snapshot`, DPS-3). Cross-column
JOINT structure is NOT covered — joint-distribution DP is DPS-4 (PrivBayes). Masked
output still carries no ε (deterministic transform).
```

- [ ] **Step 4: Commit**

```bash
git add tests/unit/generation/test_generate_dp_contract.py docs/what-we-cannot-prove.md
git commit -m "test(dp): lock consume-only contract; narrow disclaimer to marginal DP (DPS-3)"
```

---

## Later: DPS-4 — joint-distribution DP (PrivBayes) — NOT in this plan

Gretel/MOSTLY parity means DP that preserves *correlations*, not just marginals. This is a separate, larger effort with its own plan. Outline only (full rationale in `~/.claude/plans/decoy-dp-synthetic-plan.md`):

1. **DP marginal selection** — a DP mutual-information greedy Bayesian-network build (PrivBayes) over candidate pairs.
2. **DP measurement + RDP accountant** — measure selected 2-/3-way marginals under a Gaussian mechanism; extend `PrivacyBudget` with an RDP/zCDP path (the reason Task 1 kept composition pluggable).
3. **k-parent conditional sampler** — generalize the existing single-parent `condition_on` sampler (`_sample.py:169-186`) to k parents.
4. Reuses: pairwise crosstabs (`snapshot.py:510-548`), the sequential-conditional sampler, the Laplace/Gaussian mechanisms, the consume-only artifact boundary (DPS-3 lock applies unchanged).

Do NOT start DPS-4 until DPS-1..3 ship and joint-DP is confirmed a buyer priority.

## Self-review notes
- Spec coverage: DPS-1 = Tasks 2–3; DPS-2 = Tasks 1, 4; DPS-3 = Tasks 5–6. DPS-4 outlined, out of scope.
- The one place to watch: Task 3 changes what `apply_dp_noise` emits for categoricals — any existing `test_dp.py` assertion that a rare label survives is asserting the bug; update it.
- Type consistency: `PrivacyBudget.charge(...)` / `.total_epsilon()` / `.breakdown()` used identically in Tasks 1 and 4.
