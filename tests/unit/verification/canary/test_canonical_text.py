"""A7: typed Arrow cells compare through one frozen canonical text (``canary-span-normalization/v1``).

Each case checks the per-cell canonical text, null-like or uncompared result and the schema
decision, then derives the report-level facts the plan states (``finding_count``, status,
reasons, completion class) with an exact-cell and exact-span helper over those texts and the E0
``CanaryVerdictPolicy``. The end-to-end re-run through ``scan_canary_artifacts`` is E3's.
"""

from __future__ import annotations

import array as _array
import datetime as dt
import decimal
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pyarrow as pa
import pytest

from decoy_engine.verification.canary import (
    CanaryBindingSource,
    CanaryColumnFinding,
    CanaryCompletionClass,
    CanaryMatchMode,
    CanaryObservedReason,
    CanaryPolicyInputError,
    CanaryReason,
    CanaryStatus,
    CanaryVerdictPolicy,
    completion_class,
)
from decoy_engine.verification.canary._canonical import (
    CellState,
    SchemaSupport,
    canonical_cells,
    classify_schema,
)
from tests.unit.verification.canary.test_report_fields import _report

REPO = Path(__file__).resolve().parents[4]
SPAN_MIN = 8
DOB = dt.date(1980, 2, 14)
EPOCH = dt.datetime(1970, 1, 1)


def _days(d: dt.date) -> int:
    return (d - EPOCH.date()).days


def _ns(t: dt.datetime) -> int:
    delta = t - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def unvalidated(typ: pa.DataType, values: list[bytes | None]) -> pa.Array:
    """A variable-width array built from raw buffers, so nothing checks its UTF-8."""
    code = "q" if typ in (pa.large_string(), pa.large_binary()) else "i"
    offsets, data, valid = [0], b"", 0
    for i, v in enumerate(values):
        data += v or b""
        offsets.append(len(data))
        valid |= (v is not None) << i
    bitmap = pa.py_buffer(valid.to_bytes((len(values) + 7) // 8 or 1, "little"))
    buffers = [bitmap, pa.py_buffer(_array.array(code, offsets).tobytes()), pa.py_buffer(data)]
    return pa.Array.from_buffers(typ, len(values), buffers)


def _batch(arr: pa.Array) -> pa.RecordBatch:
    return pa.RecordBatch.from_arrays([arr], names=["c0"])


def _cell_json(cell: str | CellState) -> str:
    return cell if type(cell) is str else f"<{cell.name.lower()}>"


def scan_counts(batch: pa.RecordBatch, canaries: tuple[str, ...]) -> dict[str, object]:
    """The test's own exact-cell and exact-span comparison over the canonical texts."""
    if classify_schema(batch.schema) is SchemaSupport.UNSUPPORTED:
        return {"unsupported": True, "cells": None, "findings": (), "uncompared": 0}
    exact, spans = frozenset(canaries), tuple(c for c in canaries if len(c) >= SPAN_MIN)
    findings, uncompared, cells = [], 0, []
    for ordinal in range(batch.num_columns):
        column = canonical_cells(batch.column(ordinal))
        cells.append([_cell_json(c) for c in column])
        texts = [c for c in column if type(c) is str]
        uncompared += sum(c is CellState.UNCOMPARED for c in column)
        n_exact = sum(t in exact for t in texts)
        n_span = sum(t not in exact and any(s in t for s in spans) for t in texts)
        modes = zip(tuple(CanaryMatchMode)[:2], (n_exact, n_span), strict=True)
        findings += [CanaryColumnFinding(0, ordinal, m, n, False) for m, n in modes if n]
    return dict(unsupported=False, cells=cells, findings=tuple(findings), uncompared=uncompared)


def verdict(counts: dict[str, object]) -> tuple[CanaryStatus, tuple[CanaryReason, ...], object]:
    """Feed the counts to E0's policy, exactly as the scan will."""
    observed = set()
    if counts["unsupported"]:
        observed.add(CanaryObservedReason.ARTIFACT_UNSUPPORTED)
    if counts["uncompared"]:
        observed.add(CanaryObservedReason.CELL_UNCOMPARED)
    kw = {
        "validation_failed": False,
        "binding_source": CanaryBindingSource.PUBLICATION_RECORD,
        "observed": frozenset(observed),
    }
    findings = counts["findings"]
    assert isinstance(findings, tuple)
    status = CanaryVerdictPolicy.status(**kw)
    reasons = CanaryVerdictPolicy.reasons(
        **kw, has_findings=bool(findings), has_short_excluded=False
    )
    unsupported = int(bool(counts["unsupported"]))
    report = _report(
        **dict(status=status, complete=status is CanaryStatus.COMPLETE, reasons=reasons),
        **dict(requested_artifact_count=1, scanned_artifact_count=1 - unsupported),
        **dict(unsupported_artifact_count=unsupported, resource_limited_artifact_count=0),
        **dict(uncompared_cell_count=counts["uncompared"], findings=findings),
        **dict(affected_cell_count=sum(f.affected_cell_count for f in findings)),
        finding_count=len(findings),
    )
    return status, reasons, completion_class(report)


R = CanaryReason
REASON_CODES = {
    "M": R.MATCH,
    "I": R.SCAN_INCOMPLETE,
    "X": R.ARTIFACT_UNSUPPORTED,
    "U": R.CELL_UNCOMPARED,
}
CLASS_CODES = {
    "C": CanaryCompletionClass.CLEAR,
    "F": CanaryCompletionClass.FINDINGS,
    "I": CanaryCompletionClass.INCOMPLETE,
}
U, N = "<uncompared>", "<null_like>"


class Case(NamedTuple):
    build: Callable[[], pa.Array]
    canary: str
    cells: list[str] | None  # None: the artifact is unsupported from its schema
    findings: int
    uncompared: int
    klass: str  # CLASS_CODES key
    reasons: str  # REASON_CODES keys, in canonical order


def _col(values: list, typ: pa.DataType) -> Callable[[], pa.Array]:
    return lambda: pa.array(values, typ)


def _ints(values: list, storage: pa.DataType, typ: pa.DataType) -> Callable[[], pa.Array]:
    return lambda: pa.array(values, storage).view(typ)


def _bad_strings(typ: pa.DataType) -> Callable[[], pa.Array]:
    if typ == pa.string_view():
        return lambda: unvalidated(pa.string(), [b"canary-1", b"\xff\xfe"]).cast(typ)
    return lambda: unvalidated(typ, [b"canary-1", b"\xff\xfe"])


def _bad_dictionary() -> pa.Array:
    values = unvalidated(pa.string(), [b"canary-1", b"\xff\xfe"])
    return pa.DictionaryArray.from_arrays(pa.array([0, 1, None, 1], pa.int32()), values)


TOKYO_MIDNIGHT = _ns(dt.datetime(1980, 2, 13, 15))  # local 1980-02-14T00:00:00+09:00
LA_ELEVEN_PM = _ns(dt.datetime(1980, 2, 14, 7))  # local 1980-02-13T23:00:00-08:00
DOB_NS = _ns(dt.datetime(1980, 2, 14))
NOON_US = _ns(dt.datetime(1980, 2, 14, 12, 34, 56, 500000)) // 1000
INF = float("inf")
STRUCT = pa.struct([("a", pa.string())])
I64 = pa.int64()

CASES: dict[str, Case] = {
    "7a_int64": Case(
        _col([123456789, None, -5], I64), "123456789", ["123456789", N, "-5"], 1, 0, "F", "M"
    ),
    "7b_float64_integral": Case(
        _col([223456789.0, 2.0**53, 1.2e16], pa.float64()),
        "223456789",
        ["223456789", "9007199254740992", "1.2e+16"],
        1,
        0,
        "F",
        "M",
    ),
    "7c_float64_fraction": Case(
        _col([1.5, 0.1], pa.float64()), "1.50", ["1.5", "0.1"], 0, 0, "C", ""
    ),
    "7d_negative_zero": Case(_col([-0.0], pa.float64()), "0", ["0"], 1, 0, "F", "M"),
    "7e_decimal_scale_kept": Case(
        _col([decimal.Decimal("123.40"), None], pa.decimal128(10, 2)),
        "123.40",
        ["123.40", N],
        1,
        0,
        "F",
        "M",
    ),
    "7f_date32": Case(
        _ints([_days(DOB)], pa.int32(), pa.date32()), "1980-02-14", ["1980-02-14"], 1, 0, "F", "M"
    ),
    "7g_timestamp_ns_midnight_span": Case(
        _ints([DOB_NS], I64, pa.timestamp("ns")),
        "1980-02-14",
        ["1980-02-14T00:00:00"],
        1,
        0,
        "F",
        "M",
    ),
    "7h_timestamp_utc_no_suffix": Case(
        _ints([NOON_US], I64, pa.timestamp("us", tz="UTC")),
        "1980-02-14T12:34:56.5",
        ["1980-02-14T12:34:56.5"],
        1,
        0,
        "F",
        "M",
    ),
    "7i_binary_invalid_utf8": Case(
        _col([b"\xff\xfe", b"ok", None], pa.binary()), "canary-1", [U, "ok", N], 0, 1, "I", "IU"
    ),
    "7j_nan_and_infinities_null_like": Case(
        _col([float("nan"), INF, -INF, None], pa.float64()), "nan", [N, N, N, N], 0, 0, "C", ""
    ),
    "7k_list_unsupported": Case(
        _col([[123456789]], pa.list_(I64)), "123456789", None, 0, 0, "I", "IX"
    ),
    "7l_struct_unsupported": Case(
        _col([{"a": "123456789"}], STRUCT), "123456789", None, 0, 0, "I", "IX"
    ),
    "7n_tokyo_zone_unsupported": Case(
        _ints([TOKYO_MIDNIGHT], I64, pa.timestamp("ns", tz="Asia/Tokyo")),
        "1980-02-14",
        None,
        0,
        0,
        "I",
        "IX",
    ),
    "7o_los_angeles_zone_unsupported": Case(
        _ints([LA_ELEVEN_PM], I64, pa.timestamp("ns", tz="America/Los_Angeles")),
        "1980-02-14",
        None,
        0,
        0,
        "I",
        "IX",
    ),
    "7p_string": Case(_bad_strings(pa.string()), "canary-1", ["canary-1", U], 1, 1, "F", "MIU"),
    "7p_large_string": Case(
        _bad_strings(pa.large_string()), "canary-1", ["canary-1", U], 1, 1, "F", "MIU"
    ),
    "7p_string_view": Case(
        _bad_strings(pa.string_view()), "canary-1", ["canary-1", U], 1, 1, "F", "MIU"
    ),
    "7p_dictionary_int32_string": Case(
        _bad_dictionary, "canary-1", ["canary-1", U, N, U], 1, 2, "F", "MIU"
    ),
}


def all_outputs(batches: dict[str, pa.RecordBatch]) -> dict[str, object]:
    """Canonical output of every case, JSON-shaped, for the with/without-pandas comparison."""
    out: dict[str, object] = {}
    for name, batch in batches.items():
        counts = scan_counts(batch, (CASES[name].canary,))
        out[name] = {k: counts[k] for k in ("unsupported", "cells", "uncompared")}
    return out


@pytest.mark.parametrize("name", sorted(CASES))
def test_typed_cell_matrix(name: str) -> None:
    case = CASES[name]
    counts = scan_counts(_batch(case.build()), (case.canary,))
    assert counts["cells"] == (None if case.cells is None else [case.cells])
    assert counts["unsupported"] is (case.cells is None)
    findings = counts["findings"]
    assert isinstance(findings, tuple) and len(findings) == case.findings
    if name.startswith("7g"):  # midnight text holds the date canary as a span, not a cell
        assert [f.match_mode for f in findings] == [CanaryMatchMode.EXACT_SPAN]
    assert counts["uncompared"] == case.uncompared
    status, reasons, klass = verdict(counts)
    assert reasons == tuple(REASON_CODES[c] for c in case.reasons)
    assert klass is CLASS_CODES[case.klass]
    assert (status is CanaryStatus.INCOMPLETE) is ("I" in case.reasons)
    assert klass is not CanaryCompletionClass.CLEAR or (case.uncompared == 0 and case.cells)


@pytest.mark.parametrize("name", ["7n_tokyo_zone_unsupported", "7o_los_angeles_zone_unsupported"])
def test_zone_cases_are_not_vacuous(name: str) -> None:
    # Rendered as the UTC instant, 7o would falsely hold 1980-02-14 and 7n would miss it.
    arr = CASES[name].build()
    utc_text = (EPOCH + dt.timedelta(microseconds=arr.cast(I64)[0].as_py() // 1000)).isoformat()
    assert ("1980-02-14" in utc_text) is name.startswith("7o")
    assert classify_schema(_batch(arr).schema) is SchemaSupport.UNSUPPORTED


@pytest.mark.parametrize("name", [n for n in sorted(CASES) if CASES[n].cells is None])
def test_unsupported_column_is_never_read_as_text(name: str) -> None:
    with pytest.raises(CanaryPolicyInputError):
        canonical_cells(CASES[name].build())


@pytest.mark.parametrize("name", [n for n in sorted(CASES) if n.startswith("7p_")])
def test_7p_arrays_hold_invalid_utf8_unvalidated(name: str) -> None:
    arr = CASES[name].build()
    with pytest.raises(pa.ArrowInvalid):
        arr.validate(full=True)
    with pytest.raises(UnicodeDecodeError):
        arr.to_pylist()


def test_unsupported_decision_comes_from_schema_before_cells() -> None:
    supported = pa.array(["123456789"], pa.string())
    nested = pa.array([[1]], pa.list_(pa.int64()))
    batch = pa.RecordBatch.from_arrays([supported, nested], names=["a", "b"])
    assert classify_schema(batch.schema) is SchemaSupport.UNSUPPORTED
    assert classify_schema(pa.schema([("a", pa.string())])) is SchemaSupport.SUPPORTED


# ``import decoy_engine`` itself imports pandas (a required dependency), so the subprocess
# loads only the canary subpackage: the two parent packages are bare namespace stubs. The
# batches arrive over Arrow IPC because ``pa.array`` on Python objects consults pandas.
_PANDAS_BLOCKED = """
import json, pathlib, sys, types
sys.modules["pandas"] = None
for name, sub in (("decoy_engine", ""), ("decoy_engine.verification", "/verification")):
    stub = types.ModuleType(name)
    stub.__path__ = ["src/decoy_engine" + sub]
    sys.modules[name] = stub
import pyarrow as pa
from tests.unit.verification.canary import test_canonical_text as t
batches = {p.stem: pa.ipc.open_file(p).get_batch(0) for p in pathlib.Path(sys.argv[1]).iterdir()}
out = t.all_outputs(batches)
assert sys.modules["pandas"] is None
try:
    pa.array([1])
except AttributeError:
    print(json.dumps(out, sort_keys=True))
"""


def test_7m_same_output_without_pandas(tmp_path: Path) -> None:
    import pandas  # noqa: F401  (non-vacuity: this process has pandas loaded)

    batches = {name: _batch(case.build()) for name, case in CASES.items()}
    for name, batch in batches.items():
        with pa.ipc.new_file(tmp_path / f"{name}.arrow", batch.schema) as writer:
            writer.write_batch(batch)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "src")])}
    argv = [sys.executable, "-c", _PANDAS_BLOCKED, str(tmp_path)]
    proc = subprocess.run(argv, cwd=REPO, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    assert proc.returncode == 0, proc.stderr[-2000:]
    without = json.loads(proc.stdout)
    assert without == json.loads(json.dumps(all_outputs(batches), sort_keys=True))
    assert len(without) == len(CASES)
    assert without["7g_timestamp_ns_midnight_span"]["cells"] == [["1980-02-14T00:00:00"]]
