"""Phase D / D1c: sentinel-preserve on the out-of-core bucket_perturb route.

Both backends run here: the Python `apply_bucket_perturb` path (forced by hiding the companion)
and the compiled index kernel (skipped without it). They must agree on every sentinel.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity._d1a_hash_data import SECRET, run_chain
from tests.parity._d1b1_bucket_data import bucket_seed
from tests.parity._d1c_data import IN_RANGE, NORMAL, OUT_OF_RANGE, table_digest
from tests.parity._kernel_witness import force_companion_unavailable
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

KEY = "out_of_core_bucket_perturb"
_MODES = ["resident", "sink-batch", "sink-reorder"]
_HAS_COMPANION = importlib.util.find_spec("decoy_engine_native") is not None
_BACKENDS = [
    "python",
    pytest.param(
        "native",
        marks=pytest.mark.skipif(not _HAS_COMPANION, reason="companion not installed"),
    ),
]
_NEAR_BOUNDS = ["2262-04-10", "1677-09-22"]
_OOC = [*NORMAL, IN_RANGE, None, OUT_OF_RANGE, "not-a-date", IN_RANGE, *_NEAR_BOUNDS]


@pytest.fixture(params=_BACKENDS)
def _backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    backend: str = request.param
    if backend == "python":
        force_companion_unavailable(monkeypatch)
    return backend


def _run(
    values: list[str | None],
    extra: tuple[tuple[str, Any], ...],
    *,
    mode: str = "resident",
    tmp: Path,
    batch: int | None = None,
    fmt: str | None = "%Y-%m-%d",
) -> Any:
    table = pa.table({"b": pa.array(values, pa.string())})
    seeds = (("b", bucket_seed("month", fmt, extra=extra)),)
    return run_chain(table, seeds, batch_rows=batch, **mode_kwargs(mode, tmp))


def _col(run: Any) -> list[Any]:
    return run.tables["t"].column("b").to_pylist()


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch", [None, 4, 1])
def test_sentinels_survive_on_every_driver_and_batch_shape(
    _backend: str, mode: str, batch: int | None, tmp_path: Path
) -> None:
    keep = (IN_RANGE, OUT_OF_RANGE, *_NEAR_BOUNDS)
    got = _col(_run(_OOC, (("preserve_values", keep),), mode=mode, tmp=tmp_path, batch=batch))
    for out, src in zip(got, _OOC, strict=True):
        if src in keep:
            assert out == src
    assert got[1] is not None  # a non-sentinel row was still masked


@pytest.mark.parametrize("mode", _MODES)
def test_unset_empty_and_matching_nothing_are_the_baseline(
    _backend: str, mode: str, tmp_path: Path
) -> None:
    values = [*NORMAL, IN_RANGE, None, OUT_OF_RANGE, "x"]

    def digest(extra: tuple[tuple[str, Any], ...], sub: str) -> str:
        run = _run(values, extra, mode=mode, tmp=tmp_path / sub, batch=3)
        return table_digest(run.tables["t"])

    base = digest((), "a")
    assert digest((("preserve_values", ()),), "b") == base
    assert digest((("preserve_values", ("1999-09-09",)),), "c") == base


def test_in_range_sentinel_is_perturbed_without_config_and_kept_with_it(
    _backend: str, tmp_path: Path
) -> None:
    base = _col(_run([NORMAL[0], IN_RANGE], (), tmp=tmp_path / "a"))
    kept = _col(
        _run([NORMAL[0], IN_RANGE], (("preserve_values", (IN_RANGE,)),), tmp=tmp_path / "b")
    )
    assert base[1] != IN_RANGE
    assert kept == [base[0], IN_RANGE]


def test_all_sentinel_and_empty_batches(_backend: str, tmp_path: Path) -> None:
    extra = (("preserve_values", (IN_RANGE,)),)
    assert _col(_run([IN_RANGE, IN_RANGE], extra, tmp=tmp_path / "a", batch=1)) == [IN_RANGE] * 2
    assert _col(_run([], extra, tmp=tmp_path / "b")) == []


def test_typed_date_source_keeps_its_python_path_and_a_string_sentinel(
    _backend: str, tmp_path: Path
) -> None:
    import datetime as dt

    table = pa.table({"b": pa.array([dt.date(2099, 1, 1), dt.date(2024, 3, 5)], pa.date32())})
    seeds = (("b", bucket_seed("month", extra=(("preserve_values", (IN_RANGE,)),))),)
    out = _col(run_chain(table, seeds))
    assert out[0] == IN_RANGE and out[1] != "2024-03-05"


def test_disposition_counts_sentinels_with_no_values(_backend: str, tmp_path: Path) -> None:
    values = [NORMAL[0], IN_RANGE, OUT_OF_RANGE, IN_RANGE]
    extra = (("preserve_values", (IN_RANGE, OUT_OF_RANGE)),)
    run = _run(values, extra, tmp=tmp_path / "a", batch=2)
    (entry,) = [e for e in run.result.quality_metrics[KEY] if e["column"] == "b"]
    assert entry["preserved_rows"] == 3
    assert IN_RANGE not in repr(entry) and OUT_OF_RANGE not in repr(entry)
    unset = _run(values, (), tmp=tmp_path / "b", batch=2)
    (plain,) = [e for e in unset.result.quality_metrics[KEY] if e["column"] == "b"]
    assert "preserved_rows" not in plain


def test_a_preserved_native_chunk_that_runs_no_kernel_is_not_credited(
    _backend: str, tmp_path: Path
) -> None:
    extra = (("preserve_values", (IN_RANGE,)),)
    run = _run([IN_RANGE, IN_RANGE], extra, tmp=tmp_path, batch=1)
    (entry,) = run.result.quality_metrics[KEY]
    assert entry["executed_backend"] in ("none", "python_pandas")
    assert entry["preserved_rows"] == 2


@pytest.mark.parametrize(
    "extra",
    [(("preserve_values", ("nope",)),), (("preserve_values", "2099-01-01"),)],
)
def test_invalid_preserve_values_is_a_coded_config_error(
    _backend: str, extra: tuple[tuple[str, Any], ...], tmp_path: Path
) -> None:
    with pytest.raises((StrategyError, ExecutionError)) as caught:
        _run([NORMAL[0]], extra, tmp=tmp_path)
    assert getattr(caught.value, "code", None) == "bucket_perturb_invalid_config"


def test_preserve_values_without_a_format_is_a_coded_config_error(
    _backend: str, tmp_path: Path
) -> None:
    with pytest.raises((StrategyError, ExecutionError)):
        _run([NORMAL[0]], (("preserve_values", (IN_RANGE,)),), tmp=tmp_path, fmt=None)


def test_secret_keyed_run_keeps_sentinels(_backend: str) -> None:
    table = pa.table({"b": pa.array([NORMAL[0], IN_RANGE], pa.string())})
    seeds = (("b", bucket_seed("month", extra=(("preserve_values", (IN_RANGE,)),))),)
    run = run_chain(table, seeds, key_provider=SecretKeyProvider(SECRET))
    assert _col(run)[1] == IN_RANGE


@pytest.mark.skipif(not _HAS_COMPANION, reason="companion not installed")
def test_native_and_python_agree_with_sentinels_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = (("preserve_values", (IN_RANGE, OUT_OF_RANGE)),)
    values = [*NORMAL, IN_RANGE, None, OUT_OF_RANGE, "not-a-date"]
    native = _run(values, keep, tmp=tmp_path / "n", batch=3)
    force_companion_unavailable(monkeypatch)
    python = _run(values, keep, tmp=tmp_path / "p", batch=3)
    assert table_digest(native.tables["t"]) == table_digest(python.tables["t"])
