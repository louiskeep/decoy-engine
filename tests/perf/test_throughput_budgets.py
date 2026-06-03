"""PV-2 P5 (2026-06-01): wall-clock + memory perf budgets for engine strategies.

Each cell runs a strategy at a documented scale and asserts wall-clock
under a documented budget. Budget regressions become PR blockers
(pytest exit non-zero on assertion failure).

Calibration target: i7-1265U / 32GB / Win 11 / Py 3.10
(auto-memory `dev_machine.md`). The pre-pilot pilot installs typically
run on beefier hardware; the dev-box budget is the "should be at least
this fast" floor. Each cell's docstring documents the observed
wall-clock at calibration + the chosen budget + the headroom factor.

Source: docs/v2/sprints/pre-pilot/pv-2-perf-budgets.md.
"""

from __future__ import annotations

import time
import tracemalloc

import pandas as pd
import pytest

from decoy_engine.execution._strategies._categorical import CategoricalStrategyHandler
from decoy_engine.execution._strategies._hash import HashStrategyHandler
from decoy_engine.execution._strategies._nested import NestedStrategyHandler
from decoy_engine.execution._strategies._text_redact import TextRedactHandler
from decoy_engine.plan._types import ColumnSeed

# Per-cell budgets are calibrated 2x the observed dev-box wall-clock
# at PV-2 implementation time (R6 lock #6 headroom factor). Revisit
# when a strategy implementation changes; loosen if headroom drops
# below 1.5x, tighten if above 4x.


def _seed(strategy: str, provider_config: dict | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=tuple(sorted((provider_config or {}).items())),
    )


class _FakeCtx:
    pass


@pytest.mark.perf
def test_text_redact_throughput_10k_rows_under_5s():
    """PV-2 P5 (2026-06-01); QA-3-F3 regression net.

    Pre-fix: pandas `.at[]` per-cell loop produced ~30% throughput
    loss. Post-fix: list-collect + single Series assignment per QA-3
    F3 closure.

    Dev-box (i7-1265U / Py 3.10) headroom budget: 5.0s for 10k rows
    of mixed-PII text. ~2x calibration factor.

    Failure: perf regression vs the QA-3-F3 fix, or a new throughput
    cliff in the text_redact strategy.
    """
    samples = [
        "Contact alice@example.com about appointment SSN 123-45-6789.",
        "Patient phone (212) 555-1234. PAN 4111 1111 1111 1111 valid.",
        "Patient ICD-10 J45.40 + NPI 1234567893. No further notes.",
        "Lorem ipsum dolor sit amet, no PII in this row at all.",
    ]
    cells = [samples[i % len(samples)] for i in range(10_000)]
    df = pd.DataFrame({"notes": cells})
    handler = TextRedactHandler()

    tracemalloc.start()
    start = time.perf_counter()
    out, _ = handler.run(df.copy(), "notes", _seed("text_redact"), _FakeCtx())
    elapsed_s = time.perf_counter() - start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(out) == 10_000
    assert elapsed_s < 5.0, (
        f"text_redact 10k rows took {elapsed_s:.2f}s; budget 5.0s. "
        "Likely regression vs QA-3-F3 list-collect fix."
    )
    # Loose RSS budget: 200 MiB peak for the 10k-row workload.
    assert peak_bytes < 200 * 1024 * 1024, (
        f"text_redact 10k rows peak RSS {peak_bytes / (1024 * 1024):.1f} MiB; budget 200 MiB."
    )


@pytest.mark.perf
def test_nested_throughput_1k_rows_under_8s():
    """PV-2 P5 (2026-06-01); QA-3-F2 + F14 regression net.

    Pre-fix: index-keyed dict iteration broke on duplicate-index
    DataFrames + JSONPath overlap silently dropped writes. Post-fix:
    positional enumeration + deepest-first writeback.

    Dev-box headroom budget: 8.0s for 1k JSON cells with leaf-level
    redact. JSONPath parsing dominates, not the writeback loop.

    Failure: perf regression in either the positional iteration loop
    or the overlap-detection helper.
    """
    cell_template = '{"user": {"name": "n%d", "email": "u%d@example.com"}}'
    cells = [cell_template % (i, i) for i in range(1_000)]
    df = pd.DataFrame({"data": cells})
    handler = NestedStrategyHandler()

    tracemalloc.start()
    start = time.perf_counter()
    out, _ = handler.run(
        df.copy(),
        "data",
        _seed("nested", {"target": "$.user.email", "strategy": "redact"}),
        _FakeCtx(),
    )
    elapsed_s = time.perf_counter() - start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(out) == 1_000
    assert elapsed_s < 8.0, f"nested 1k rows took {elapsed_s:.2f}s; budget 8.0s."
    assert peak_bytes < 100 * 1024 * 1024, (
        f"nested 1k rows peak RSS {peak_bytes / (1024 * 1024):.1f} MiB; budget 100 MiB."
    )


@pytest.mark.perf
def test_hash_throughput_10k_rows_under_2s():
    """PV-2 P5 (2026-06-01): hash throughput floor.

    Dev-box headroom budget: 2.0s for 10k rows of mixed-string input
    through the hash strategy. ~2x calibration factor.

    Failure: perf regression in HMAC-SHA256 keyed path or in the
    bytes -> hex conversion.
    """
    cells = [f"sample-input-row-{i}" for i in range(10_000)]
    df = pd.DataFrame({"id": cells})
    handler = HashStrategyHandler()
    # Hash requires a namespace on the seed (hash_requires_namespace
    # invariant); supply a synthetic one for the perf cell.
    seed = ColumnSeed(
        namespace="perf_ns",
        strategy="hash",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
    )

    class _Ctx:
        job_seed = b"\x00" * 8

    tracemalloc.start()
    start = time.perf_counter()
    out, _ = handler.run(df.copy(), "id", seed, _Ctx())
    elapsed_s = time.perf_counter() - start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(out) == 10_000
    assert elapsed_s < 2.0, f"hash 10k rows took {elapsed_s:.2f}s; budget 2.0s."
    assert peak_bytes < 100 * 1024 * 1024


@pytest.mark.perf
def test_categorical_throughput_10k_rows_under_1s():
    """PV-2 P5 (2026-06-01): categorical throughput floor.

    Dev-box headroom budget: 1.0s for 10k rows mapping into a
    50-category pool with uniform weights. ~2-3x calibration.

    Failure: perf regression in the CDF build or the derive_index
    inner loop.
    """
    cells = [f"source-{i}" for i in range(10_000)]
    df = pd.DataFrame({"category": cells})
    categories = [f"cat-{i}" for i in range(50)]
    weights = [1.0] * 50
    handler = CategoricalStrategyHandler()

    seed = _seed("categorical", {"categories": categories, "weights": weights})

    class _Ctx:
        job_seed = b"\x00" * 8
        namespace_registry = None

    tracemalloc.start()
    start = time.perf_counter()
    out, _ = handler.run(df.copy(), "category", seed, _Ctx())
    elapsed_s = time.perf_counter() - start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(out) == 10_000
    assert elapsed_s < 1.0, f"categorical 10k rows took {elapsed_s:.2f}s; budget 1.0s."
    assert peak_bytes < 100 * 1024 * 1024
