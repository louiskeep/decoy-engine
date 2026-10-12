"""Phase D / D1b-i: the REAL compiled companion on the out-of-core bucket_perturb route.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the declined-path
and reference-double suites cover the same routing. These tests run the real index kernel and
check what only it can show: byte parity with the pinned baseline, the Python reference and the
pandas oracle, the date-range guard against the native operator's silent wrap near the pandas
Timestamp bounds, the accepted post-Unicode-version divergence (characterized in a test of its
own, apart from a locked native selection) and the guard that keeps the normalization tables a
reviewed decision.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._operator_registry import PYTHON_PANDAS, RUST_COMPANION
from decoy_engine.execution.native._bucket_perturb_ext import native_bucket_perturb
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.out_of_core._bucket_native import (
    BucketColumnDecision,
    NativeBucket,
    native_bucket_perturb_array,
)
from decoy_engine.execution.out_of_core._categorical_native import BoundIndexKernel
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from decoy_engine.execution.out_of_core._mask import mask_batch
from decoy_engine.transforms.bucket_perturb import apply_bucket_perturb
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import (
    JOB_SEED,
    chain_graph,
    chain_plan,
    chain_sources,
    run_chain,
)
from tests.parity._d1b1_bucket_data import (
    ISO,
    MAIN_SEEDS,
    SHAPE_SEEDS,
    STAMP,
    TZ_COLUMNS,
    boundary_column,
    bucket_seed,
    declined_sources,
    main_payload,
    shape_payload,
)
from tests.parity._d1b1_bucket_expected import EXPECTED, EXPECTED_TYPES
from tests.parity._kernel_witness import ExpectedMask, KernelWitness, force_companion_unavailable
from tests.parity.test_out_of_core_fk_parity import _run_oracle
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.parity.test_phase_d_d1a_hash_native import (
    _NORMALIZATION_CRATE,
    _locked_package,
    _vendored_table_version,
)


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _companion_loads(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)

_MODES = ["resident", "sink-batch", "sink-reorder"]
KEY = "out_of_core_bucket_perturb"
RANGE_CODE = "out_of_core_bucket_perturb_date_out_of_native_range"
_NS = "ns_b"
# `a` + U+0897 + U+0316: the combining marks reorder under Unicode >= 16, not under 14.
_SENSITIVE = "a̖ࢗ"
_SENSITIVE_FORMAT = f"{ISO} {_SENSITIVE}"


def test_the_loaded_companion_is_abi_3() -> None:
    from decoy_engine_native import _kernel

    assert _kernel.abi_version() == "decoy-native-abi-3"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 7])
def test_native_output_is_the_pinned_baseline_and_is_consumed(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(
            main_payload(), MAIN_SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path)
        )
    expected = [ExpectedMask("t", name, "bucket_perturb_array") for name, _ in MAIN_SEEDS]
    witness.assert_native_ran(chain_sources(main_payload()), run.tables, expected)
    assert witness.native_attempts > 0
    for name, _ in MAIN_SEEDS:
        got = run.tables["t"].column(name)
        assert str(got.type) == EXPECTED_TYPES[name], name
        assert got.to_pylist() == EXPECTED[name], name
    disposition = {(e["table"], e["column"]): e for e in run.result.quality_metrics[KEY]}
    assert {e["executed_backend"] for e in disposition.values()} == {RUST_COMPANION}
    assert all("reason" not in e for e in disposition.values())


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 5, 1])
def test_per_batch_shape_matches_the_pinned_output(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    run = run_chain(
        shape_payload(), SHAPE_SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path)
    )
    for name, _ in SHAPE_SEEDS:
        assert run.tables["t"].column(name).to_pylist() == EXPECTED[name], name
    entries = {e["column"]: e for e in run.result.quality_metrics[KEY]}
    # Nothing in these two columns can be perturbed, so no compiled call ran for them.
    for name in ("b_allnull", "b_unparse"):
        assert entries[name]["executed_backend"] == "none", name
        assert entries[name]["reason"] == "no_native_kernel_calls", name
    for name in ("b_tail_null", "b_head_null"):
        assert entries[name]["executed_backend"] == RUST_COMPANION, name


def _reference(values: pa.Array, bucket: str, fmt: str, namespace: str = _NS) -> list[Any]:
    out = apply_bucket_perturb(
        values.to_pandas(), bucket=bucket, job_seed=JOB_SEED, namespace=namespace, date_format=fmt
    )
    return pa.array(out, from_pandas=True).to_pylist()


def test_native_equals_the_python_reference_for_shared_version_input() -> None:
    run = run_chain(main_payload(), MAIN_SEEDS)
    payload = main_payload()
    for name, seed in MAIN_SEEDS:
        cfg = dict(seed.provider_config)
        want = _reference(
            payload.column(name).combine_chunks(), cfg["bucket"], cfg["date_format"], seed.namespace
        )
        assert run.tables["t"].column(name).to_pylist() == want, name


def test_native_matches_the_pandas_oracle_strictly_across_buckets() -> None:
    # Payload columns on the FK-chain table `t` (which also carries a parent key and an FK).
    # Only the frozen D0 field_metadata gap may differ from the oracle.
    dates = ["2024-03-05", "2024-02-29", "2023-12-31", None, "bad", "2021-01-01", "2020-02-29"]
    payload = pa.table({b: pa.array(dates, pa.string()) for b in ("week", "month", "quarter")})
    seeds = tuple((b, bucket_seed(b)) for b in ("week", "month", "quarter"))
    ooc = run_chain(payload, seeds, batch_rows=3).tables
    oracle = _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs
    for table in ("up", "t", "child"):
        report = sc.compare_strict(oracle[table], ooc[table])
        assert {dim for dim, agree in report.items() if not agree} <= {"field_metadata"}, table
    for column in ("week", "month", "quarter"):
        assert ooc["t"].column(column).to_pylist() == oracle["t"].column(column).to_pylist()


def test_an_all_null_column_reconciles_to_the_oracle_null_type() -> None:
    payload = pa.table({"u": pa.array([None, None, None, None], pa.string())})
    seeds = (("u", bucket_seed("month")),)
    ooc = run_chain(payload, seeds, batch_rows=2).tables
    oracle = _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs
    assert ooc["t"].column("u").type == oracle["t"].column("u").type == pa.null()
    report = sc.compare_strict(oracle["t"], ooc["t"])
    assert {dim for dim, agree in report.items() if not agree} <= {"field_metadata"}


def test_an_all_unparseable_column_stays_a_string_like_the_oracle() -> None:
    payload = pa.table({"u": pa.array(["nope", "bad", "", None], pa.string())})
    seeds = (("u", bucket_seed("month")),)
    ooc = run_chain(payload, seeds, batch_rows=2).tables
    oracle = _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs
    assert ooc["t"].column("u").type == oracle["t"].column("u").type == pa.string()
    assert ooc["t"].column("u").to_pylist() == ["nope", "bad", "", None]


# --- Declined columns stay Python even with the real companion present ---------------------


def test_a_non_string_source_and_a_tz_format_stay_python_with_the_real_companion() -> None:
    for name, column in declined_sources().items():
        run = run_chain(pa.table({name: column}), ((name, bucket_seed("month")),))
        (entry,) = run.result.quality_metrics[KEY]
        assert entry["executed_backend"] == PYTHON_PANDAS, name
        assert entry["reason"] == f"bucket_perturb_source_not_string:{name}:{column.type}"
        assert run.tables["t"].column(name).to_pylist() == EXPECTED[name], name
    for name, (fmt, values) in TZ_COLUMNS.items():
        run = run_chain(
            pa.table({name: pa.array(values, pa.string())}), ((name, bucket_seed("month", fmt)),)
        )
        (entry,) = run.result.quality_metrics[KEY]
        assert entry["executed_backend"] == PYTHON_PANDAS, name
        assert entry["reason"].startswith(f"bucket_perturb_config_declined:{name}:"), name
        assert run.tables["t"].column(name).to_pylist() == EXPECTED[name], name


def test_an_abi_incompatible_real_companion_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine_native import _kernel

    monkeypatch.setattr(_kernel, "abi_version", lambda: "decoy-native-abi-0")
    run = run_chain(main_payload().select(["b_month"]), (MAIN_SEEDS[1],))
    assert {(e["executed_backend"], e["reason"]) for e in run.result.quality_metrics[KEY]} == {
        (PYTHON_PANDAS, "index_extension_unavailable")
    }
    assert run.tables["t"].column("b_month").to_pylist() == EXPECTED["b_month"]


def test_the_loader_self_test_is_not_observed_as_a_masking_call() -> None:
    witness = KernelWitness()
    with witness.observe():
        load_compiled_index_kernel()
    assert witness.observations == []
    assert witness.native_attempts == 0


# --- Date range: the native operator wraps silently near the pandas Timestamp bounds -------


def _native_handle() -> NativeBucket:
    decision = BucketColumnDecision("t", "c", RUST_COMPANION, None, pa.string())
    return NativeBucket(BoundIndexKernel(load_compiled_index_kernel()), decision)


def _one(value: str, bucket: str, fmt: str = STAMP) -> tuple[str, Any]:
    """`("guarded", None)` when the adapter fails closed, else `("ok", (native, python))`."""
    arr = pa.array([value], pa.string())
    try:
        got = native_bucket_perturb_array(
            arr,
            native=_native_handle(),
            mask_key=JOB_SEED,
            namespace=_NS,
            bucket=bucket,
            date_format=fmt,
        )
    except ExecutionError as exc:
        assert exc.code == RANGE_CODE
        return "guarded", None
    return "ok", (got.to_pylist(), _reference(arr, bucket, fmt))


def test_the_raw_operator_wraps_or_fails_at_the_lower_bound_which_is_why_the_guard_exists() -> None:
    arr = pa.array(["1677-09-21 12:00:00"], pa.string())
    kernel = load_compiled_index_kernel()
    try:
        raw = native_bucket_perturb(
            arr,
            bucket="week",
            date_format=STAMP,
            mask_key=JOB_SEED,
            namespace=_NS,
            index_kernel=kernel,
        ).to_pylist()
    except Exception:  # either failure shape shows the operator cannot be trusted at the bound
        return
    assert raw != _reference(arr, "week", STAMP)


@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
def test_every_date_near_a_bound_is_guarded_or_exactly_the_python_answer(bucket: str) -> None:
    low = dt.datetime(1677, 9, 21, 12)
    high = dt.datetime(2262, 4, 11, 12)
    guarded = 0
    checked = 0
    for k in range(0, 130):
        for base, sign in ((low, 1), (high, -1)):
            value = (base + sign * dt.timedelta(days=k)).strftime(STAMP)
            status, pair = _one(value, bucket)
            if status == "guarded":
                guarded += 1
                continue
            checked += 1
            native_out, python_out = pair
            assert native_out == python_out, (bucket, k, sign)
    assert guarded > 0
    assert checked > 0


@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
def test_dates_clearly_inside_the_range_are_never_guarded(bucket: str) -> None:
    for value in ("1700-01-15 00:00:00", "2000-06-15 08:00:00", "2250-01-15 00:00:00"):
        status, pair = _one(value, bucket)
        assert status == "ok"
        assert pair[0] == pair[1]


@pytest.mark.parametrize("mode", _MODES)
def test_the_known_wrap_case_fails_closed_with_no_commit(mode: str, tmp_path: Path) -> None:
    from tests.parity.test_phase_d_d1a_hash_pinning import RecordingSink

    seeds = (("b_boundary", bucket_seed("week", STAMP)),)
    out_dir = tmp_path / "out"
    kwargs: dict[str, Any] = {}
    sink = None
    if mode != "resident":
        sink = RecordingSink(out_dir)
        kwargs = {"sink": sink, "out_dir": out_dir, "reorder": mode.endswith("reorder")}
    with pytest.raises(ExecutionError) as caught:
        run_chain(pa.table({"b_boundary": boundary_column()}), seeds, batch_rows=3, **kwargs)
    assert caught.value.code == RANGE_CODE
    if sink is not None:
        assert (sink.commits, sink.aborts) == (0, 1)
        assert not out_dir.exists()


def test_the_same_boundary_column_masks_on_the_python_path(monkeypatch: pytest.MonkeyPatch) -> None:
    force_companion_unavailable(monkeypatch)
    seeds = (("b_boundary", bucket_seed("week", STAMP)),)
    run = run_chain(pa.table({"b_boundary": boundary_column()}), seeds)
    assert run.tables["t"].column("b_boundary").to_pylist() == EXPECTED["b_boundary"]


def test_out_of_range_sentinels_pass_through_identically_on_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = ["9999-12-31", "2299-01-01", "0001-01-01", "2024-03-05"]
    table = pa.table({"d": pa.array(values, pa.string())})
    seeds = (("d", bucket_seed("quarter")),)
    native = run_chain(table, seeds).tables["t"].column("d").to_pylist()
    force_companion_unavailable(monkeypatch)
    python = run_chain(table, seeds).tables["t"].column("d").to_pylist()
    assert native == python
    assert native[:3] == values[:3]
    assert native[3] != values[3]


# --- The accepted Unicode-version divergence, and the guard that keeps it reviewed ---------


def _select_through_the_route(values: list[str], *, native: bool) -> list[str | None]:
    """Mask `values` through `mask_batch` with a Unicode date_format suffix, native or Python."""
    seed = bucket_seed("month", _SENSITIVE_FORMAT, namespace=_NS)
    plan = chain_plan((("c", seed),))
    batch = pa.record_batch([pa.array(values, pa.string())], names=["c"])
    table_plan = None
    if native:
        table_plan = OocHashRun().prepare_table(
            plan, "t", batch.schema, frozenset(), (), site="test"
        )
        assert table_plan.bucket is not None
        assert table_plan.bucket.decisions["c"].native
    out = mask_batch(plan, "t", batch, hash_plan=table_plan)
    return out.column(0).to_pylist()


_SENSITIVE_DAYS = [f"2024-02-{d:02d} {_SENSITIVE}" for d in (1, 2, 3)]


def test_native_selection_for_a_post_python_version_suffix_is_locked() -> None:
    # Native (Rust, newer tables) is canonical: this selection must not drift.
    got = _select_through_the_route(_SENSITIVE_DAYS, native=True)
    assert [v.split(" ")[0] for v in got if v is not None] == [
        "2024-02-02",
        "2024-02-19",
        "2024-02-02",
    ]


def test_post_python_version_suffix_selects_differently_from_the_python_path() -> None:
    # Accepted by Cam 2026-10-11 for hash and carried to categorical and bucket_perturb: native
    # normalizes under the newer Unicode tables, the Python path under its runtime's. Only
    # meaningful while the runtime predates Unicode 16, where U+0897 gained its combining class.
    import unicodedata

    assert tuple(int(p) for p in unicodedata.unidata_version.split(".")) < (16, 0, 0)
    native = _select_through_the_route(_SENSITIVE_DAYS, native=True)
    python = _select_through_the_route(_SENSITIVE_DAYS, native=False)
    assert native != python
    assert python[0].startswith("2024-02-08")
    # The divergence is the normalization, not the format: an ordinary ASCII suffix selects
    # identically on both paths.
    ascii_seed = bucket_seed("month", f"{ISO} xyz", namespace=_NS)
    plan = chain_plan((("c", ascii_seed),))
    batch = pa.record_batch(
        [pa.array([f"2024-02-{d:02d} xyz" for d in range(1, 29)], pa.string())], names=["c"]
    )
    table_plan = OocHashRun().prepare_table(plan, "t", batch.schema, frozenset(), (), site="test")
    assert mask_batch(plan, "t", batch, hash_plan=table_plan).column(0).to_pylist() == (
        mask_batch(plan, "t", batch).column(0).to_pylist()
    )


def test_the_resolved_normalization_crate_and_its_unicode_table_version_are_pinned() -> None:
    locked = _locked_package("unicode-normalization")
    assert locked["version"] in _NORMALIZATION_CRATE, (
        "unicode-normalization moved: its Unicode tables may have changed. Record the new "
        "table version in the D1a hash guard after reviewing the effect on native hash tokens, "
        "categorical selections and bucket_perturb offsets."
    )
    table_version, checksum = _NORMALIZATION_CRATE[locked["version"]]
    assert locked["checksum"] == checksum
    assert table_version == (17, 0, 0)


def test_the_recorded_table_version_matches_the_vendored_crate_when_present() -> None:
    vendored: Any = _vendored_table_version("0.1.25")
    if vendored is None:
        pytest.skip("the crate source is not in the local cargo registry")
    assert vendored == _NORMALIZATION_CRATE["0.1.25"][0]
