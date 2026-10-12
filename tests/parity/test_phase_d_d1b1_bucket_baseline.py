"""Phase D / D1b-i step 1: pin the pre-D1b-i OOC bucket_perturb output (test-only, no source change).

Every expected value is a hardcoded literal in `_d1b1_bucket_expected.py`, captured from the
pandas-bridge `apply_bucket_perturb` before the native route existed. The same suite stays green
when D1b-i routes admitted columns through the compiled index kernel: native output must
reproduce these bytes for shared-Unicode-version input, so drift in either route fails here. It
runs the payload through both drivers and both output paths, plus the shapes native must decline
(non-string source, tz-directive format) and the configs that must keep raising their existing
errors in their existing stage.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity._d1a_hash_data import SECRET, run_chain
from tests.parity._d1b1_bucket_data import (
    DMY,
    ISO,
    MAIN_SEEDS,
    SHAPE_SEEDS,
    TZ_COLUMNS,
    boundary_column,
    bucket_seed,
    declined_sources,
    main_payload,
    shape_payload,
)
from tests.parity._d1b1_bucket_expected import EXPECTED, EXPECTED_TYPES
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

_MODES = ["resident", "sink-batch", "sink-reorder"]


def _check(run_tables: dict[str, pa.Table], names: list[str], *, sink: bool = False) -> None:
    """The sink writes the plan's analytic `string` type, so an all-null column reads back as
    string there while the resident path keeps the data-derived null type."""
    out = run_tables["t"]
    for name in names:
        got = out.column(name)
        want = "string" if sink and EXPECTED_TYPES[name] == "null" else EXPECTED_TYPES[name]
        assert str(got.type) == want, name
        assert got.to_pylist() == EXPECTED[name], name


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 7, 1])
def test_ooc_bucket_perturb_matches_the_pinned_baseline(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    run = run_chain(
        main_payload(), MAIN_SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path)
    )
    _check(run.tables, [n for n, _ in MAIN_SEEDS])


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 5, 1])
def test_per_batch_shape_does_not_change_the_pinned_output(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    """All-null, all-unparseable and trailing/leading-null batches reconcile to the whole-column type."""
    run = run_chain(
        shape_payload(), SHAPE_SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path)
    )
    _check(run.tables, [n for n, _ in SHAPE_SEEDS], sink=mode != "resident")


def test_all_null_column_is_null_typed_and_unparseable_present_stays_string() -> None:
    assert EXPECTED_TYPES["b_allnull"] == "null"
    assert EXPECTED_TYPES["b_unparse"] == "string"
    # Unparseable values pass through unchanged, including the empty string and a wrong-format date.
    assert EXPECTED["b_unparse"][:6] == ["nope", "", "2024-99-99", None, "x" * 40, "31/12/2024"]


def test_secret_backed_mask_key_differs_from_job_seed() -> None:
    seeds = (("b_month", bucket_seed("month")),)
    table = pa.table({"b_month": main_payload().column("b_month")})
    run = run_chain(table, seeds, key_provider=SecretKeyProvider(SECRET))
    got = run.tables["t"].column("b_month").to_pylist()
    assert got != EXPECTED["b_month"]
    assert [v is None for v in got] == [v is None for v in EXPECTED["b_month"]]


def test_namespace_binds_the_offset() -> None:
    assert EXPECTED["b_month_ns"] != EXPECTED["b_month"]


def test_out_of_range_sentinels_pass_through_unchanged() -> None:
    for name in ("b_week", "b_month", "b_quarter"):
        assert EXPECTED[name][5] == "9999-12-31"
        assert EXPECTED[name][6] == "2299-01-01"
        assert EXPECTED[name][2] == "not-a-date"
        assert EXPECTED[name][1] is None


@pytest.mark.parametrize("mode", _MODES)
def test_non_string_source_keeps_its_python_output(mode: str, tmp_path: Path) -> None:
    for name, column in declined_sources().items():
        run = run_chain(
            pa.table({name: column}),
            ((name, bucket_seed("month")),),
            batch_rows=2,
            **mode_kwargs(mode, tmp_path / name),
        )
        _check(run.tables, [name])


@pytest.mark.parametrize("mode", _MODES)
def test_tz_directive_format_keeps_its_python_output(mode: str, tmp_path: Path) -> None:
    for name, (fmt, values) in TZ_COLUMNS.items():
        run = run_chain(
            pa.table({name: pa.array(values, pa.string())}),
            ((name, bucket_seed("month", fmt)),),
            batch_rows=2,
            **mode_kwargs(mode, tmp_path / name),
        )
        _check(run.tables, [name])


@pytest.mark.parametrize("batch_rows", [None, 2])
def test_dates_within_one_bucket_of_the_pandas_range_bounds_mask_on_python(
    batch_rows: int | None,
) -> None:
    """The Python path has the full calendar range; the native route fails closed here instead."""
    seeds = (("b_boundary", bucket_seed("week", "%Y-%m-%d %H:%M:%S")),)
    run = run_chain(pa.table({"b_boundary": boundary_column()}), seeds, batch_rows=batch_rows)
    _check(run.tables, ["b_boundary"])


_RAISES: list[tuple[str, object, type[Exception], str]] = [
    ("unsupported_bucket", bucket_seed("year"), StrategyError, "bucket_perturb_invalid_config"),
    (
        "missing_format",
        bucket_seed("month", None),
        ExecutionError,
        "out_of_core_bucket_perturb_autodetect_unsupported",
    ),
    (
        "empty_format",
        bucket_seed("month", ""),
        ExecutionError,
        "out_of_core_bucket_perturb_autodetect_unsupported",
    ),
    ("tz_alone", bucket_seed("month", "%z"), StrategyError, "bucket_perturb_invalid_config"),
    ("time_only", bucket_seed("month", "%H:%M"), StrategyError, "bucket_perturb_invalid_config"),
    ("mixed", bucket_seed("month", "mixed"), StrategyError, "bucket_perturb_invalid_config"),
    ("iso8601", bucket_seed("month", "ISO8601"), StrategyError, "bucket_perturb_invalid_config"),
    ("numeric", bucket_seed("month", "123"), StrategyError, "bucket_perturb_invalid_config"),
    (
        "tz_on_invalid",
        bucket_seed("month", "%H:%M %z"),
        StrategyError,
        "bucket_perturb_invalid_config",
    ),
    (
        "no_namespace",
        bucket_seed("month", namespace=None),
        StrategyError,
        "bucket_perturb_requires_namespace",
    ),
]


@pytest.mark.parametrize(
    ("seed", "exc", "code"), [c[1:] for c in _RAISES], ids=[c[0] for c in _RAISES]
)
def test_config_hard_errors_keep_their_existing_error(
    seed: object, exc: type[Exception], code: str
) -> None:
    table = pa.table({"d": pa.array(["2024-03-05", None], pa.string())})
    with pytest.raises(exc) as caught:
        run_chain(table, (("d", seed),))  # type: ignore[arg-type]
    assert caught.value.code == code  # type: ignore[attr-defined]


def test_date_formats_used_by_the_fixture_are_the_documented_ones() -> None:
    assert (ISO, DMY) == ("%Y-%m-%d", "%d/%m/%Y")
