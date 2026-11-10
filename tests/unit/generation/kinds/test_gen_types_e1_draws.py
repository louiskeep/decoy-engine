"""GEN-TYPES E1: AT-3 prefix stability, AT-4 goldens, AT-5 route parity, AT-6 moments, AT-19 nulls.

Mutants named in the plan (run by hand, listed in the slice report): reseed per batch (AT-3),
seed the draw from `base_int("np")` (AT-5, AT-19), pass the variance as `std` (AT-6).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.generation import kinds
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._plan_entry import generate_tables
from decoy_engine.generation.kinds import _parametric
from decoy_engine.generation.pinned import compile_pinned_plan, execute_pinned
from decoy_engine.generators.derivation import GEN_FAMILIES, GenDeriveContext, with_table
from tests.parity._fr0_support import (
    fingerprints,
    partition_from_cuts,
    plan_of,
    run_resident,
    run_streamed,
)
from tests.unit.generation.kinds._support import (
    FAMILY_COLS,
    INT_FAMILIES,
    SEED,
    cfg,
    dist_col,
    generate,
    one,
    table,
    values,
)

FAMILIES = sorted(FAMILY_COLS)


def _stream(col: dict[str, Any], seed: int = SEED) -> Any:
    full = with_table(col, "t")
    ctx = GenDeriveContext.for_column(derive_key=None, column_config=full, fallback_seed=seed)
    return kinds.ParametricStream(col, ctx)


def test_dist_is_a_new_rng_family_and_moves_no_existing_one() -> None:
    assert GEN_FAMILIES[-1] == "dist"
    assert GEN_FAMILIES[:5] == ("py", "np", "faker", "faker_unique", "unique_pair")
    ctx = GenDeriveContext.for_column(
        derive_key=None, column_config=with_table({"name": "a"}, "t"), fallback_seed=1
    )
    assert len({ctx.base_int(f) for f in GEN_FAMILIES}) == len(GEN_FAMILIES)


# AT-3 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("batch", [1, 7, 1000])
def test_batches_concatenate_to_the_one_shot_draw(family: str, batch: int) -> None:
    col = dist_col(family)
    total = 2000
    whole = _stream(col).next_batch(0, total)
    stream = _stream(col)
    got: list[Any] = []
    offset = 0
    while offset < total:
        size = min(batch, total - offset)
        got.extend(stream.next_batch(offset, size))
        offset += size
    assert got == whole


@pytest.mark.parametrize("family", FAMILIES)
def test_an_irregular_partition_gives_the_same_table(family: str) -> None:
    config = one(dist_col(family, null_probability=0.2), rows=900)
    whole = run_resident(config)
    cut = run_resident(config, partition_from_cuts({"t": [1, 8, 8, 300, 899]}))
    assert fingerprints(cut) == fingerprints(whole)
    assert cut["t"].equals(whole["t"])


def test_the_zipf_cdf_is_built_once_per_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    real = _parametric.zipf_cdf
    calls: list[tuple[float, int]] = []

    def counting(s: float, n: int) -> Any:
        calls.append((s, n))
        return real(s, n)

    monkeypatch.setattr(_parametric, "zipf_cdf", counting)
    stream = _stream(dist_col("zipf"))
    for i in range(50):
        stream.next_batch(i * 10, 10)
    assert len(calls) == 1


# AT-4 ---------------------------------------------------------------------------------

# Seed 7, 20 rows, column `x` of table `t`, under the locked numpy (a band whose stream disagrees re-pins here).
GOLDEN: dict[str, list[float | int]] = {
    "poisson": [10, 4, 6, 7, 5, 6, 1, 8, 4, 8, 7, 5, 12, 6, 6, 5, 6, 7, 11, 5],
    "uniform": [
        8.264155134401758,
        4.333485997108449,
        8.286329105183889,
        7.5087704720179,
        3.7731159582758345,
        5.9887388906773715,
        7.732607261273001,
        5.056871531785168,
        3.8703313108694237,
        9.145645575944144,
        7.680579631107882,
        7.805128179691388,
        9.365972672991195,
        2.1627855643348766,
        5.15712962433718,
        5.111338468174068,
        8.357081361295181,
        6.938725860853876,
        3.4552381675783264,
        4.754355976289915,
    ],
    "zipf": [2, 1, 6, 5, 2, 1, 1, 3, 1, 2, 2, 9, 32, 11, 1, 14, 1, 1, 1, 24],
}


def _golden_family(family: str) -> list[Any]:
    return values(one(dist_col(family)))


@pytest.mark.parametrize("family", FAMILIES)
def test_goldens(family: str) -> None:
    got = _golden_family(family)
    want = GOLDEN[family]
    if family in INT_FAMILIES:
        assert got == want
    else:
        assert got == pytest.approx(want, rel=1e-9, abs=1e-12)


def test_constant_golden_and_types() -> None:
    for value, arrow in (
        ("US", pa.string()),
        (3, pa.int64()),
        (2.5, pa.float64()),
        (True, pa.bool_()),
        (False, pa.bool_()),
        ("", pa.string()),
    ):
        out = generate(one({"name": "x", "type": "constant", "value": value}, rows=5))["t"]
        assert out.column("x").to_pylist() == [value] * 5
        assert out.schema.field("x").type == arrow


def test_distribution_arrow_types() -> None:
    out = generate(one(dist_col("uniform", name="a"), rows=4))["t"]
    assert out.schema.field("a").type == pa.float64()
    out = generate(cfg([table("t", 4, [dist_col("poisson", name="p")])]))["t"]
    assert out.schema.field("p").type == pa.int64()
    # An all-null column keeps its declared type rather than inferring `null`.
    out = generate(one(dist_col("uniform", null_probability=1.0), rows=4))["t"]
    assert out.schema.field("x").type == pa.float64()
    assert out.column("x").to_pylist() == [None] * 4


def test_constant_with_nulls_and_no_draw() -> None:
    col = {"name": "x", "type": "constant", "value": "US", "null_probability": 0.5}
    got = values(one(col, rows=400))
    assert set(got) == {"US", None}
    assert 140 < got.count(None) < 260


def test_a_column_does_not_move_when_a_sibling_is_added() -> None:
    alone = values(one(dist_col("uniform"), rows=50))
    both = values(
        cfg([table("t", 50, [{"name": "k", "type": "constant", "value": 1}, dist_col("uniform")])])
    )
    assert alone == both


def test_two_columns_with_equal_parameters_differ() -> None:
    out = generate(cfg([table("t", 30, [dist_col("uniform", "a"), dist_col("uniform", "b")])]))["t"]
    assert out.column("a").to_pylist() != out.column("b").to_pylist()


# AT-5 ---------------------------------------------------------------------------------


def _legacy(config: dict[str, Any]) -> dict[str, pa.Table]:
    """The whole-table path: a `formula` sibling makes the plan ineligible for the stream core."""
    tbl = config["tables"][0]
    legacy_cols = [*tbl["generate_columns"], {"name": "zz", "type": "formula", "formula": "1"}]
    legacy = cfg([table("t", tbl["row_count"], legacy_cols)], config["global_settings"]["seed"])
    return generate(legacy)


def _pinned(config: dict[str, Any]) -> dict[str, pa.Table]:
    config["global_settings"]["reference_date"] = "2026-10-22T09:30:00Z"
    return execute_pinned(compile_pinned_plan(config), config["global_settings"]["seed"])


COLUMNS: list[tuple[str, dict[str, Any]]] = [
    *((f, dist_col(f, null_probability=0.25)) for f in FAMILIES),
    ("clip", dist_col("uniform", min=4, max=6)),
    ("const_str", {"name": "x", "type": "constant", "value": "US", "null_probability": 0.25}),
    ("const_int", {"name": "x", "type": "constant", "value": 4}),
]


@pytest.mark.parametrize(("label", "col"), COLUMNS, ids=[c[0] for c in COLUMNS])
def test_resident_streamed_legacy_and_pinned_agree(label: str, col: dict[str, Any]) -> None:
    config = one(col, rows=600)
    stream_core = generate(config)["t"].column("x").to_pylist()
    partitioned = run_resident(config, partition_from_cuts({"t": [1, 7, 333]}))["t"]
    assert partitioned.column("x").to_pylist() == stream_core
    assert _legacy(config)["t"].column("x").to_pylist() == stream_core
    assert _pinned(one(col, rows=600))["t"].column("x").to_pylist() == stream_core


def _prepared(config: dict[str, Any]) -> dict[str, Any]:
    return json.loads(plan_of(config).generation.config_json)


def test_the_plan_is_eligible_for_the_stream_core() -> None:
    adm = classify_plan(_prepared(one(dist_col("uniform"))))
    assert adm.eligible, adm.reason()
    adm = classify_plan(_prepared(one({"name": "x", "type": "constant", "value": 1})))
    assert adm.eligible, adm.reason()


@pytest.mark.parametrize("family", FAMILIES)
def test_the_parquet_sink_route_agrees(family: str, tmp_path: Path) -> None:
    config = one(dist_col(family, null_probability=0.25), rows=600)
    result, tables = run_streamed(config, tmp_path / "out", partition_from_cuts({"t": [5, 250]}))
    assert result.mode == "streamed"
    want = generate(config)["t"].column("x").to_pylist()
    assert tables["t"].column("x").to_pylist() == want


# AT-6 ---------------------------------------------------------------------------------

ROWS = 200_000


def _zipf_pmf(s: float, n: int) -> np.ndarray:
    w = 1.0 / np.arange(1, n + 1, dtype=float) ** s
    return w / w.sum()


def _analytic(family: str) -> tuple[float, float]:
    c = FAMILY_COLS[family]
    if family == "uniform":
        return (c["low"] + c["high"]) / 2, (c["high"] - c["low"]) ** 2 / 12
    if family == "poisson":
        return c["mean"], c["mean"]
    pmf = _zipf_pmf(c["s"], c["n"])
    k = np.arange(1, c["n"] + 1)
    mean = float((k * pmf).sum())
    return mean, float((((k - mean) ** 2) * pmf).sum())


@pytest.mark.parametrize("family", FAMILIES)
def test_moments_match_the_analytic_values(family: str) -> None:
    draws = np.asarray(_stream(dist_col(family)).next_batch(0, ROWS), dtype=float)
    mean, var = _analytic(family)
    s2 = draws.var(ddof=1)
    se_mean = math.sqrt(s2 / ROWS)
    m4 = float(((draws - draws.mean()) ** 4).mean())
    se_var = math.sqrt(max(m4 - s2**2, 0.0) / ROWS)
    assert abs(draws.mean() - mean) < 4 * se_mean, family
    assert abs(s2 - var) < 4 * se_var, family


@pytest.mark.parametrize("family", FAMILIES)
def test_clip_holds_min_and_max_exactly(family: str) -> None:
    mean, var = _analytic(family)
    sd = math.sqrt(var)
    lo, hi = mean - 0.3 * sd, mean + 0.3 * sd
    if family in INT_FAMILIES:
        lo, hi = math.floor(lo), math.ceil(hi)
    draws = _stream(dist_col(family, min=lo, max=hi)).next_batch(0, 20_000)
    assert min(draws) == lo
    assert max(draws) == hi
    assert all(lo <= v <= hi for v in draws)
    kinds_ = {type(v) for v in draws}
    assert kinds_ == ({int} if family in INT_FAMILIES else {float})


def test_clip_with_one_bound() -> None:
    low_only = _stream(dist_col("uniform", min=5)).next_batch(0, 5000)
    assert min(low_only) == 5
    high_only = _stream(dist_col("uniform", max=5)).next_batch(0, 5000)
    assert max(high_only) == 5


# AT-19 --------------------------------------------------------------------------------


def test_nulls_do_not_correlate_with_a_uniform_draw() -> None:
    p = 0.3
    out = values(one(dist_col("uniform", low=0, high=1, null_probability=p), rows=ROWS))
    non_null = np.sort(np.asarray([v for v in out if v is not None]))
    null_rate = 1 - len(non_null) / ROWS
    se = math.sqrt(p * (1 - p) / ROWS)
    assert abs(null_rate - p) < 4 * se
    n = len(non_null)
    cdf = non_null
    d = max(
        float(np.max(np.arange(1, n + 1) / n - cdf)),
        float(np.max(cdf - np.arange(0, n) / n)),
    )
    critical = math.sqrt(-0.5 * math.log(0.001 / 2)) / math.sqrt(n)
    assert d < critical, (d, critical)


def test_generate_tables_is_the_public_entry() -> None:
    assert callable(generate_tables)


# Cross-arch bytes ------------------------------------------------------------------------
# Digest of the raw float64 bits of 2,000,000 draws, observed identical on arm64 (macOS) and x86
# (Linux) under numpy 2.2.6. The count matters: numpy's normal and exponential tails call platform
# libm and differ by one ulp only at rare rows (the first around row 267,000 of 2M), so a small pin
# looks stable and is not. Those families are deferred (plan: Deferred to E1b). uniform is built
# from random() with separate multiply and add ufuncs because arm64 fuses `low + width * x` into
# an FMA. A change that moves a digest on one architecture breaks docs/determinism.md.

_BIT_ROWS = 2_000_000
_BIT_DIGESTS = {
    "poisson": "1d6becbcdc7420dcaf4fd0338cfedcd212b9cc52f976501933549e30075961e7",
    "uniform": "004f9f6941cbc6d91ce04f96f5714435a369fbbb12cf64e72bb6cf1d7590a085",
    "zipf": "125ecd7e0c7a032edf7023d52391c6cece3778511542ef795fe51d158d60486c",
}
_CDF_DIGESTS = {
    (1.2, 50): "6288863be0e8f1524f24f00ced55ec7bea3fb63ad8ac740bcaaa8552e81196cd",
    (1.05, 1_000_000): "c8ba8bcb467251b22f6704a70ccf396ccd12eb2d424c040d2d43a67b0e287a2b",
    (0.5, 1_000_000): "62754c9b036ab9057d60e24bd275d781660fb3548801569b7ad279b7b47eea18",
    (3.0, 1000): "74ffa0d381763cd4ddb61dd269013c3017b4b782188941f25febc9b909810489",
}


def test_every_shipped_family_has_a_pinned_bit_digest() -> None:
    assert set(_BIT_DIGESTS) == set(FAMILIES)


@pytest.mark.parametrize("family", FAMILIES)
def test_draw_bits_are_identical_on_every_architecture(family: str) -> None:
    import hashlib

    draws = _stream(dist_col(family)).next_batch(0, _BIT_ROWS)
    bits = np.asarray(draws, dtype=np.float64).tobytes()
    assert hashlib.sha256(bits).hexdigest() == _BIT_DIGESTS[family]


@pytest.mark.parametrize(("s", "n"), sorted(_CDF_DIGESTS))
def test_zipf_cdf_bits_are_identical_on_every_architecture(s: float, n: int) -> None:
    import hashlib

    assert hashlib.sha256(_parametric.zipf_cdf(s, n).tobytes()).hexdigest() == _CDF_DIGESTS[(s, n)]
