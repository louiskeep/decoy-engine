"""C8-iii-a acceptance: `when:` on text_redact, bucket_perturb and date_shift, unified route.

Plan: `docs/plans/2026-10-07-c8-iii-a-when-operators.md` rev 3, section 4. Every differential
compares the lane-on run (oracle poisoned) with an explicit lane-off run: tables with schema and
`b"pandas"` metadata, warnings, row errors and every metric but the activation leaf. Decline
cases run the lane unpoisoned and assert the lane did not activate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._operator_config_rejections import (
    bucket_perturb_config_rejection,
)
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from tests.native._c6c_i_support import ADMITTED_CONFIGS
from tests.native._c8_iii_a_support import (
    BAD_DATE,
    KINDS,
    MIXED_OFFSET_VALUES,
    SPECIAL_FORMAT_JOBS,
    SPECIAL_FORMATS,
    N,
    columns,
    declines,
    make_table,
    predicates,
    value,
)
from tests.native._chunked_bucket_perturb_support import bp_col, date_value
from tests.native._chunked_entry_support import NEEDS_COMPANION
from tests.physical.test_c8_ii_unified_when import admitted, declined, with_when
from tests.physical.test_unified_slice_faker import Case, lane_run
from tests.physical.test_unified_slice_parity import _assert_full_parity
from tests.physical.test_unified_slice_positional import lane_batch_rows

GOLDENS = json.loads(
    (Path(__file__).parents[1] / "native" / "_c8_iii_a_main_goldens.json").read_text()
)
PASS_K = {"name": "k", "strategy": "passthrough"}
PASS_P = {"name": "p", "strategy": "passthrough"}


def case_for(
    tmp_path: Path,
    kind: str,
    when: str | None,
    source: pa.Table | None = None,
    **extra: Any,
) -> Case:
    cols = columns(kind, None, **extra)
    mutate = with_when({"v": when}) if when is not None else None
    return Case(tmp_path, source if source is not None else make_table(kind), cols, mutate=mutate)


def run_batched(case: Case, batch: int | None) -> tuple[ExecutionResult, ExecutionResult]:
    off = case.run(lane=False)
    if batch is None:
        return off, lane_run(case)
    with lane_batch_rows(batch):
        return off, lane_run(case)


# ---------------------------------------------------------------------------
# 1. Matrix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch", [None, 5], ids=["one_batch", "ragged_batches"])
@pytest.mark.parametrize("predicate", sorted(predicates("text_redact")))
@pytest.mark.parametrize("kind", KINDS)
def test_1_matrix(tmp_path: Path, kind: str, predicate: str, batch: int | None) -> None:
    case = case_for(tmp_path, kind, predicates(kind)[predicate])
    off, on = run_batched(case, batch)
    leaf = _assert_full_parity(off, on)
    node = next(e for e in leaf["nodes"].values() if e["operator"] != "native_passthrough")
    assert node["executed"] is True


@pytest.mark.parametrize("kind", KINDS)
def test_1_empty_table(tmp_path: Path, kind: str) -> None:
    admitted(case_for(tmp_path, kind, "k == 'x'", make_table(kind, 0)))


@pytest.mark.parametrize("kind", KINDS)
def test_1_stringdtype_sidecar_source(tmp_path: Path, kind: str) -> None:
    table = make_table(kind)
    frame = pd.DataFrame({c: pd.array(table.column(c).to_pylist(), dtype="string") for c in "vk"})
    frame["p"] = list(range(N))
    admitted(
        case_for(tmp_path, kind, "k == 'x'", pa.Table.from_pandas(frame, preserve_index=False))
    )


@pytest.mark.parametrize("kind", KINDS)
def test_1_selected_rows_change_and_unselected_rows_do_not(tmp_path: Path, kind: str) -> None:
    table = make_table(kind)
    on, _ = admitted(case_for(tmp_path, kind, "k == 'x'", table))
    sel, src = table.column("k").to_pylist(), table.column("v").to_pylist()
    out = on.outputs["t"].column("v").to_pylist()
    changed = [i for i in range(N) if out[i] != src[i]]
    assert changed and all(sel[i] == "x" for i in changed)


# ---------------------------------------------------------------------------
# 2. date_shift row errors: table-global on unified, the batch offset added once.
# ---------------------------------------------------------------------------


def _error_table(bad: tuple[int, ...], selected: tuple[int, ...], n: int = 23) -> pa.Table:
    table = make_table("date_shift", n, bad=bad)
    k = ["x" if i in selected else "y" for i in range(n)]
    return table.set_column(1, "k", pa.array(k, pa.string()))


@NEEDS_COMPANION
def test_2_unparseable_unselected_values_give_no_records_and_keep_the_value(
    tmp_path: Path,
) -> None:
    source = _error_table(bad=(1, 5, 7), selected=tuple(i for i in range(23) if i not in (1, 5, 7)))
    on, _ = admitted(case_for(tmp_path, "date_shift", "k == 'x'", source))
    assert on.row_errors == ()
    out = on.outputs["t"].column("v").to_pylist()
    assert [out[i] for i in (1, 5, 7)] == [BAD_DATE] * 3


@NEEDS_COMPANION
@pytest.mark.parametrize("batch", [None, 4, 5], ids=["one_batch", "batches_4", "batches_5"])
def test_2_selected_unparseable_values_fail_with_table_global_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: int | None
) -> None:
    """The lane's coordinator reports the oracle's records (batch offset added once), then the
    finalize step refuses them and the table reroutes, so the job fails as lane-off does."""
    source = _error_table(bad=(2, 9, 13, 14, 21), selected=(2, 13, 14, 20, 21))
    case = case_for(tmp_path, "date_shift", "k == 'x'", source)
    captured: list[Any] = []
    real_run = ShadowCoordinator.run

    def spy(self: ShadowCoordinator, *a: Any, **k: Any) -> Any:
        result = real_run(self, *a, **k)
        captured.append(result)
        return result

    with monkeypatch.context() as mp:
        mp.setattr(ShadowCoordinator, "run", spy)
        with pytest.raises(RowErrorsFailedError) as on_exc:
            if batch is None:
                case.run(lane=True)
            else:
                with lane_batch_rows(batch):
                    case.run(lane=True)
    with pytest.raises(RowErrorsFailedError) as off_exc:
        case.run(lane=False)
    assert len(captured) == 1, "the native coordinator ran before the reroute"
    assert [r.row_index for r in captured[0].row_errors] == [2, 13, 14, 21]
    assert tuple(captured[0].row_errors) == tuple(off_exc.value.records)
    assert tuple(on_exc.value.records) == tuple(off_exc.value.records)
    assert all(r.trigger == "format_error" for r in captured[0].row_errors)


# ---------------------------------------------------------------------------
# 3. text_redact.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config", sorted(ADMITTED_CONFIGS))
def test_3_text_redact_configs(tmp_path: Path, config: str) -> None:
    table = make_table("text_redact", 32)
    cols = columns("text_redact", None)
    cols[0]["provider_config"] = dict(ADMITTED_CONFIGS[config])
    case = Case(tmp_path, table, cols, mutate=with_when({"v": "k == 'x'"}))
    off, on = run_batched(case, 7)
    _assert_full_parity(off, on)
    sel, src = table.column("k").to_pylist(), table.column("v").to_pylist()
    out = on.outputs["t"].column("v").to_pylist()
    assert all(out[i] == src[i] for i in range(32) if sel[i] != "x")


# ---------------------------------------------------------------------------
# 4. bucket_perturb.
# ---------------------------------------------------------------------------

_FORMATS = {
    "%Y-%m-%d": lambda i: date_value(i),
    "%d/%m/%Y": lambda i: f"{date_value(i)[8:]}/{date_value(i)[5:7]}/{date_value(i)[:4]}",
    "%Y%m%d": lambda i: date_value(i).replace("-", ""),
}


@NEEDS_COMPANION
@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
@pytest.mark.parametrize("fmt", sorted(_FORMATS))
def test_4_explicit_formats_are_admitted(tmp_path: Path, fmt: str, bucket: str) -> None:
    vals: list[str | None] = [None if i % 5 == 2 else _FORMATS[fmt](i) for i in range(N)]
    table = make_table("bucket_perturb", v=vals)
    case = case_for(tmp_path, "bucket_perturb", "k == 'x'", table, bucket=bucket, date_format=fmt)
    off, on = run_batched(case, 6)
    _assert_full_parity(off, on)
    sel = table.column("k").to_pylist()
    out = on.outputs["t"].column("v").to_pylist()
    assert all(out[i] == vals[i] for i in range(N) if sel[i] != "x")


def _special_case(tmp_path: Path, fmt: str, vals: list[str | None], when: str | None) -> Case:
    table = make_table("bucket_perturb", v=vals, k=["x"] * len(vals))
    cols = [bp_col("v", date_format=fmt), PASS_K, PASS_P]
    return Case(tmp_path, table, cols, mutate=with_when({"v": when}) if when else None)


@pytest.mark.parametrize("fmt", SPECIAL_FORMATS)
def test_4_the_config_gate_names_special_formats(fmt: str) -> None:
    code = bucket_perturb_config_rejection(
        "v",
        "t",
        None,
        namespace="ns_d",
        provider_config={"bucket": "month", "date_format": fmt},
    )
    assert code == "bucket_perturb_special_date_format:v"


@NEEDS_COMPANION
@pytest.mark.parametrize("fmt", SPECIAL_FORMATS)
def test_4_special_formats_unmasked_decline_and_equal_the_oracle(tmp_path: Path, fmt: str) -> None:
    vals: list[str | None] = [*MIXED_OFFSET_VALUES, None, "junk"]
    declined(_special_case(tmp_path, fmt, vals, None))


@NEEDS_COMPANION
@pytest.mark.parametrize("job", sorted(SPECIAL_FORMAT_JOBS))
@pytest.mark.parametrize("fmt", SPECIAL_FORMATS)
def test_4_special_format_jobs_that_succeeded_natively_keep_their_output(
    tmp_path: Path, fmt: str, job: str
) -> None:
    golden = GOLDENS[f"{fmt}/{job}"]["unified"]
    assert golden["native"] is True, "recorded on main"
    vals = [v for chunk in SPECIAL_FORMAT_JOBS[job] for v in chunk]
    table = pa.table(
        {"d": pa.array(vals, pa.string()), "p": pa.array(list(range(len(vals))), pa.int64())}
    )
    case = Case(tmp_path, table, [bp_col("d", date_format=fmt), PASS_P])
    on = declined(case)
    out = on.outputs["t"]
    assert str(out.schema.field("d").type) == golden["schema"]
    assert out.column("d").to_pylist() == golden["d"]
    assert (b"pandas" in (out.schema.metadata or {})) is golden["has_pandas_meta"]


@NEEDS_COMPANION
@pytest.mark.parametrize("fmt", SPECIAL_FORMATS)
@pytest.mark.parametrize("predicate", ["k == 'zzz'", "k == 'x'", "k != 'zzz'"])
@pytest.mark.parametrize("shape", ["ordinary", "mixed_offsets"])
def test_4_special_formats_masked_decline_and_equal_lane_off(
    tmp_path: Path, fmt: str, predicate: str, shape: str
) -> None:
    vals: list[str | None] = (
        [date_value(i) for i in range(6)]
        if shape == "ordinary"
        else [*MIXED_OFFSET_VALUES, None, date_value(2)]
    )
    declined(_special_case(tmp_path, fmt, vals, predicate))


# ---------------------------------------------------------------------------
# 5. Degenerate outputs and the unified reconstruction.
# ---------------------------------------------------------------------------


def _degenerate(kind: str) -> dict[str, pa.Table]:
    nulls = [None] * 4
    return {
        "all_null_source": make_table(kind, v=nulls, k=["x"] * 4),
        "empty_table": make_table(kind, 0),
        "selected_rows_all_null": make_table(
            kind, v=[None, None, value(kind, 1), value(kind, 3)], k=["x", "x", "y", "y"]
        ),
        "selected_nulls_beside_unselected_values": make_table(
            kind, v=[None, value(kind, 1), None, value(kind, 3)], k=["x", "y", "x", "y"]
        ),
    }


@pytest.mark.parametrize("sidecar", [False, True], ids=["bare_arrow", "stringdtype_sidecar"])
@pytest.mark.parametrize("case", sorted(_degenerate("text_redact")))
@pytest.mark.parametrize("kind", KINDS)
def test_5_degenerate_outputs_equal_lane_off(
    tmp_path: Path, kind: str, case: str, sidecar: bool
) -> None:
    table = _degenerate(kind)[case]
    if sidecar:
        frame = pd.DataFrame(
            {c: pd.array(table.column(c).to_pylist(), dtype="string") for c in ("v", "k")}
        )
        frame["p"] = pd.array(table.column("p").to_pylist(), dtype="int64")
        table = pa.Table.from_pandas(frame, preserve_index=False)
    admitted(case_for(tmp_path, kind, "k == 'x'", table))


# ---------------------------------------------------------------------------
# 6. Declines unchanged: lane-off's outcome, the lane never activates.
# ---------------------------------------------------------------------------


def _outcome(case: Case, lane: bool) -> tuple[str, Any]:
    try:
        result = case.run(lane=lane)
    except Exception as exc:
        return ("error", (type(exc).__name__, getattr(exc, "code", None)))
    return ("ok", result)


def assert_decline_equals_lane_off(
    tmp_path: Path, cols: list[dict[str, Any]], source: pa.Table, when: str = "k == 'x'"
) -> None:
    """The lane declines and the outcome, a result or an exception, is lane-off's."""
    case = Case(tmp_path, source, cols, mutate=with_when({"v": when}))
    off, on = _outcome(case, False), _outcome(case, True)
    assert off[0] == on[0]
    if off[0] == "error":
        assert off[1] == on[1]
        return
    assert QUALITY_METRICS_KEY not in on[1].quality_metrics
    assert on[1].outputs["t"].equals(off[1].outputs["t"], check_metadata=True)
    assert tuple(on[1].warnings) == tuple(off[1].warnings)
    assert tuple(on[1].row_errors) == tuple(off[1].row_errors)


@NEEDS_COMPANION
@pytest.mark.parametrize("name", sorted(declines()))
def test_6_declines_are_unchanged(tmp_path: Path, name: str) -> None:
    cols, kind = declines()[name]
    assert_decline_equals_lane_off(tmp_path, cols, make_table(kind))
