---
Status: target
Last reviewed: 2026-05-09
References:
  - [SHARED_ENGINE_ARCHITECTURE.md](SHARED_ENGINE_ARCHITECTURE.md) — substrate decisions that benchmarks validate.
  - [forge-platform/TESTING_GAPS.md](../forge-platform/TESTING_GAPS.md) — what we *can't* test (different concern from this doc, which is about what we *should* measure).
  - [forge-platform/ROADMAP.md](../forge-platform/ROADMAP.md) Item 49 — "Engine benchmarking discipline."
---

# Benchmarking — engine performance discipline

## Why we benchmark

Three reasons, in priority order:

1. **Catch catastrophic regressions at PR time.** A refactor that takes the STORM scan from 1.6s to 30s should turn a PR check red, not get noticed three sprints later when a customer complains.
2. **Provide canonical numbers for architectural decisions.** Item 47 (the Polars+DuckDB hybrid switch) hinged on "is the Arrow→pandas conversion cost worth the engine flexibility?" — a question the STORM Arrow-boundary benchmark answered with measurement, not intuition.
3. **Validate sales claims.** "Tens of millions out of the box, hundreds on a good box" needs measured numbers behind it before we put it in a deck.

## The rule

> **If a code change affects "how fast does X run on the engine," it ships with a benchmark.**

That covers:

- New ops (`filter`, `sort`, `dedupe`, `derive`, `join`, `aggregate`, `subset`, etc.)
- New transforms (mask strategies, generator strategies, FPE variants)
- New connectors that hit a DB (the Bug 3 DuckDB scanners, future Snowflake / Mongo / SQL Server)
- Changes to runner internals: cache, eviction, engine dispatch, conversion boundaries
- Substrate changes (anything in Item 47 phases beyond Phase 8)

If the diff includes a new file under `src/decoy_engine/{graph,transforms,generators,connectors}/`, expect to see a matching benchmark in the PR.

## Tiers

Different scales serve different purposes. Don't conflate them.

| Tier | When | Rows | Hardware | Purpose | Cost |
|---|---|---|---|---|---|
| **Smoke** | every `run-bench` PR | ~50k | CI runner (4 vCPU / 16 GB) | Catastrophic-regression guard | Free (public) / $0.024 per run (private) |
| **Regression** | nightly via cron | 100k / 1M | CI runner | Trend tracking; alerts on >20% drift | Free / pennies per night |
| **Engineering-correctness** | manual, before architectural-claim PRs | 8M | dev laptop (16 GB) | "Does the substrate scale qualitatively?" — pandas OOMs at 5M, hybrid completes at 8M | $0 |
| **Marketing** | release-candidates only | 50M+ | EC2 spot (`r5.xlarge` ~$0.04/hr spot, `r5.8xlarge` for 500M) | Sales-line numbers that go in `SHARED_ENGINE_ARCHITECTURE.md` | ~$1 per sweep |

The current `.github/workflows/benchmark.yml` runs the smoke tier. Tiers 2–4 expand from there as engine work grows.

## Conventions for every new benchmark

Five rules. Each has a reason; don't drop any without one.

1. **Mark with `@pytest.mark.benchmark`** so the default `pytest tests/` run skips it. Benchmarks are slow; nobody wants them in their fast feedback loop. The marker is configured in `pyproject.toml`.
2. **Parameterize on engine wherever the op is engine-aware.** `@pytest.mark.parametrize("engine", ["pandas", "polars", "duckdb"])` gives apples-to-apples comparisons in the same run. If an op only exists for one engine (e.g. STORM is pandas-only), skip the parameterization and document why.
3. **Always warm up.** First run pays JIT, import, and disk-cache costs that are unrelated to the workload. Run once and discard before measuring.
4. **Print one structured summary line per run.** Format: `[<name>-bench] engine=X rows=Y elapsed=Zs <other-key>=<other-val>`. Regex-friendly so the CI workflow can grab the number and post it in the PR comment without HTML scraping.
5. **Catastrophic-regression assert only.** `assert elapsed < N * baseline` where `N` is **conservatively large** (>2x, often >5x). We're measuring trends, not gating PRs over noise. Every false positive in CI erodes trust in the bench results.

## Test directory layout

```
tests/benchmark/
├── conversion/                         # Arrow → engine boundaries
│   └── test_storm_arrow_boundary.py    # ← the one we have today
├── ops/                                # graph ops in isolation
│   ├── test_filter_benchmark.py
│   ├── test_sort_benchmark.py
│   ├── test_dedupe_benchmark.py
│   └── ...
├── transforms/                         # masking strategies in isolation
│   ├── test_faker_benchmark.py
│   ├── test_hash_benchmark.py
│   ├── test_fpe_benchmark.py
│   └── ...
├── pipelines/                          # end-to-end workloads
│   ├── test_mask_pipeline_benchmark.py
│   ├── test_generate_pipeline_benchmark.py
│   └── test_storm_scan_benchmark.py
└── results/                            # tier-2 historical baselines
    ├── 2026-05-09.json
    └── 2026-05-16.json
```

`results/` is for the regression tier — one JSON file per nightly run, diffed by the regression workflow to alert on >20% drift. Doesn't apply to smoke tier.

## Test anatomy — worked example

A new benchmark for the polars `filter` op (lands with Bug 3, conceptually):

```python
"""Filter op benchmark — measures pandas vs polars across row counts.

Smoke tier: runs at 50k rows on CI to catch regressions.
"""
import time

import pandas as pd
import polars as pl
import pytest

from decoy_engine.graph.ops import filter_op


def _build_fixture(rows: int, engine: str):
    """Deterministic fixture — same shape every run for stable numbers."""
    base = {"id": list(range(rows)), "value": [i % 100 for i in range(rows)]}
    pdf = pd.DataFrame(base)
    if engine == "polars":
        return pl.from_pandas(pdf)
    return pdf


@pytest.mark.benchmark
@pytest.mark.parametrize("engine", ["pandas", "polars"])
def test_filter_at_50k(engine):
    df = _build_fixture(rows=50_000, engine=engine)
    cfg = {"predicate": "value > 50"}

    # Warmup — kills JIT / cold-import noise.
    _ = filter_op.apply([df], cfg, ctx=None)

    start = time.perf_counter()
    result = filter_op.apply([df], cfg, ctx=None)
    elapsed = time.perf_counter() - start

    print(f"[filter-bench] engine={engine} rows=50000 elapsed={elapsed:.4f}s")

    # Catastrophic-regression guard — well above any plausible normal range.
    # Real measurement is < 50ms; we fail the workflow at 5s.
    assert elapsed < 5.0, (
        f"filter on {engine} at 50k rows took {elapsed:.2f}s — investigate."
    )
```

That's the whole pattern. Copy / adapt for new ops.

## CI integration

**Today** — one workflow, one tier:

```
.github/workflows/benchmark.yml
  on: pull_request label run-bench  ▼
  runs-on: ubuntu-latest
  pytest tests/benchmark/ -m benchmark
  posts result as PR comment
```

**Target** — two workflows, two tiers:

```
.github/workflows/benchmark.yml      # smoke (current)
  on: pull_request label run-bench
  runs-on: ubuntu-latest

.github/workflows/benchmark-nightly.yml  # regression (target)
  on: schedule (nightly UTC 06:00)
  runs-on: ubuntu-latest
  pytest tests/benchmark/ -m benchmark --bench-tier=regression
  appends result to tests/benchmark/results/<date>.json
  diffs against previous; opens an issue if any benchmark dropped > 20%
```

Engineering-correctness + marketing tiers stay manual — they're not CI-friendly because they need either a real laptop (tier 3) or a paid cloud box (tier 4).

## Results storage (regression tier)

```json
// tests/benchmark/results/2026-05-16.json
{
  "commit": "abc1234",
  "runner": "ubuntu-latest-4core-16g",
  "python": "3.11.15",
  "results": {
    "filter": {
      "pandas": {"50000": 0.012, "1000000": 0.234},
      "polars": {"50000": 0.008, "1000000": 0.151}
    },
    "storm-arrow-boundary": {
      "pandas": {"50000": -0.008}
    }
  }
}
```

Diff'd by the nightly workflow. The diff logic is: if any `(op, engine, row_count)` cell drops more than 20% from the previous result, open a regression issue with both numbers + a link to the PR that touched the relevant code.

## What we don't benchmark

Out of scope for this doc. Each has a reason.

- **Network I/O paths** (file uploads, HTTP, websockets). Different concern; lives in the platform repo.
- **DB-driver layers without DuckDB** (raw `psycopg`, `sqlalchemy.execute`). Bug 3 (the per-DB DuckDB scanners) supersedes this — benchmark once the scanners replace the SQLAlchemy fallback.
- **ML model paths** (Item 8 — the future PII detector). Different scaling profile, different benchmark style; deserves its own doc when it lands.
- **Frontend perf** (bundle size, render time). Lives in the platform repo if at all.

## How this relates to other test concerns

Three docs live in the same conceptual space; don't conflate them.

| Doc | Question it answers |
|---|---|
| **`tests/parity/SEMANTIC_DIFFERENCES.md`** | "When pandas and polars produce different output, which is right?" |
| **`forge-platform/TESTING_GAPS.md`** | "What can't we test today, and what harness would close the gap?" |
| **This doc** | "What should we be measuring as the engine grows, and at what scales?" |

Bug fixes and feature work touch all three: a parity bug goes in SEMANTIC_DIFFERENCES, an untestable hot path goes in TESTING_GAPS, a new op's perf number goes in a benchmark file under this convention.

## See also

- [`tests/benchmark/test_storm_arrow_boundary.py`](tests/benchmark/test_storm_arrow_boundary.py) — the canonical smoke-tier example. Read this before writing your first benchmark.
- [`.github/workflows/benchmark.yml`](.github/workflows/benchmark.yml) — current CI workflow; ground truth for "how does the smoke tier actually run."
- [`plans/2026-05-09-hybrid-engine-bug-followup.md`](plans/2026-05-09-hybrid-engine-bug-followup.md) — the plan that motivated this discipline; Bugs 2 + 5 are why benchmarks are now first-class.
