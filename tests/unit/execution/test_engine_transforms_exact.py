"""Exact Arrow preservation, row-lineage reconstruction, nullable-integer behavior and
the schema guard for table transforms.

Expected values come from pure-Python reasoning over `to_pylist()` and the test-local
reference in `_transform_testkit`, never from the helper under test.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pytest

from decoy_engine import apply_table_transforms, run_pipeline
from decoy_engine.config import TableConfig
from decoy_engine.execution import PandasExecutionAdapter, _transforms
from decoy_engine.execution._transforms import TransformError
from decoy_engine.plan._errors import PlanCompileError
from tests.unit.execution._transform_testkit import (
    ENGINE_VERSION,
    cleared,
    reference_bindings,
    reference_frame,
    reference_transform,
    single_table_config,
    tables_equal,
)

_DUP = "duplicate_source_field_names"
_STORED = "config_references_stored_index"


def _cfg(ops: list[dict[str, Any]], *, columns: list[dict[str, Any]] | None = None) -> dict:
    """A config dict that is just enough for `apply_table_transforms`."""
    return {
        "tables": [
            {
                "name": "t",
                "columns": columns or [{"name": "s", "strategy": "redact"}],
                "transforms": ops,
            }
        ]
    }


def _ops_models(ops: list[dict[str, Any]]) -> list[Any]:
    return TableConfig.model_validate(
        {"name": "x", "columns": [{"name": "a", "strategy": "redact"}], "transforms": ops}
    ).transforms


def _same_column(a: pa.ChunkedArray, b: pa.ChunkedArray) -> None:
    """Identical logical values, validity and NaN bit patterns, same type."""
    assert a.type == b.type, f"{a.type} != {b.type}"
    assert a.is_null().to_pylist() == b.is_null().to_pylist()
    if pa.types.is_dictionary(a.type):
        # chunks may carry different dictionaries; compare the decoded values
        a = pa.chunked_array([c.dictionary_decode() for c in a.chunks], type=a.type.value_type)
        b = pa.chunked_array([c.dictionary_decode() for c in b.chunks], type=b.type.value_type)
    if not pa.types.is_floating(a.type):
        assert a.equals(b)
    if pa.types.is_floating(a.type):
        width = np.dtype(a.type.to_pandas_dtype()).itemsize
        view = {2: np.uint16, 4: np.uint32, 8: np.uint64}[width]
        av = pc.fill_null(a.combine_chunks(), 0).to_numpy(zero_copy_only=False).view(view)
        bv = pc.fill_null(b.combine_chunks(), 0).to_numpy(zero_copy_only=False).view(view)
        assert (av == bv).all()


# --------------------------------------------------------------------------
# Lineage fixture: every column is a function of i % 6, so whole rows repeat
# and dedupe has something to remove; `grp`, `d` and `ts` make subset dedupe
# span a dictionary and a time-zone column.
# --------------------------------------------------------------------------


def _lineage_table(n: int = 12) -> pa.Table:
    df = pd.DataFrame(
        {
            "id": [i % 6 for i in range(n)],
            "qn": pd.array(
                [None if (i % 6) == 1 else (i % 6) * 3 for i in range(n)], dtype="Int32"
            ),
            "grp": [f"g{(i % 6) % 3}" for i in range(n)],
            "f": [
                float("nan") if i % 6 == 2 else (None if i % 6 == 3 else (i % 6) / 2)
                for i in range(n)
            ],
            "d": pd.Categorical([f"c{(i % 6) % 2}" for i in range(n)]),
            "ts": pd.Series(
                pd.to_datetime([f"2024-11-0{1 + (i % 6)} 12:00" for i in range(n)])
            ).dt.tz_localize("America/New_York"),
            "b": [(i % 6) % 2 == 0 for i in range(n)],
            "s": [f"secret{i % 6}" for i in range(n)],
            "extra": [f"e{i % 6}" for i in range(n)],
        }
    )
    tbl = pa.Table.from_pandas(df, preserve_index=False)
    fields = [
        f.with_metadata({"note": f"field-{f.name}"}) if f.name in ("id", "grp", "d") else f
        for f in tbl.schema
    ]
    meta = dict(tbl.schema.metadata or {})
    meta[b"custom"] = b"keep-me"
    return tbl.cast(pa.schema(fields)).replace_schema_metadata(meta)


def _norm(v: Any) -> Any:
    return "<missing>" if v is None or (isinstance(v, float) and v != v) else v


def _py_apply(rows: list[tuple[int, dict]], op: dict[str, Any]) -> list[tuple[int, dict]]:
    kind = op["op"]
    if kind == "filter":
        return [(o, r) for o, r in rows if r["id"] > 2 and r["qn"] is not None and r["qn"] > 5]
    if kind == "sort":
        return sorted(rows, key=lambda x: x[1]["grp"], reverse=not op["ascending"])
    if kind == "limit":
        return rows[: op["n"]]
    if kind == "dedupe":
        cols = op["columns"]
        seen: set[tuple] = set()
        out = []
        for o, r in rows:
            key = tuple(_norm(r[c]) for c in (cols if cols is not None else r.keys()))
            if key not in seen:
                seen.add(key)
                out.append((o, r))
        return out
    if kind == "derive":
        return [(o, {**r, op["column"]: r["id"] * 2}) for o, r in rows]
    if kind == "drop_column":
        return [(o, {k: v for k, v in r.items() if k not in op["columns"]}) for o, r in rows]
    raise AssertionError(kind)


def _slot_op(name: str, slot: int) -> dict[str, Any]:
    return {
        "filter": {"op": "filter", "expression": "id > 2 and qn > 5"},
        "sort": {"op": "sort", "by": ["grp"], "ascending": False},
        "limit": {"op": "limit", "n": 5 - slot},
        "dedupe_all": {"op": "dedupe", "columns": None},
        "dedupe_subset": {"op": "dedupe", "columns": ["grp", "d", "ts"]},
        "derive": {"op": "derive", "column": f"dv{slot}", "expression": "id * 2"},
        "drop_column": {"op": "drop_column", "columns": ["extra" if slot == 0 else "b"]},
    }[name]


_OP_NAMES = ["filter", "sort", "limit", "dedupe_all", "dedupe_subset", "derive", "drop_column"]


def _check_lineage(ops: list[dict[str, Any]]) -> None:
    from decoy_engine.execution._transforms_table import to_transform_frame

    tbl = _lineage_table()
    rows = [(i, r) for i, r in enumerate(tbl.to_pylist())]
    frame = to_transform_frame(tbl)
    assert list(frame.index) == list(range(tbl.num_rows))
    for op, model in zip(ops, _ops_models(ops), strict=True):
        frame = _transforms.apply_transform(frame, model)
        rows = _py_apply(rows, op)
        assert [int(i) for i in frame.index] == [o for o, _ in rows], op
    ordinals = pa.array([o for o, _ in rows], pa.int64())

    out = apply_table_transforms(_cfg(ops), "t", tbl)
    bindings = reference_bindings(tbl, ops)
    assert out.column_names == [b[0] for b in bindings]
    for name, origin in bindings:
        if origin == "source":
            _same_column(out.column(name), pc.take(tbl.column(name), ordinals))
            assert out.schema.field(name).equals(tbl.schema.field(name), check_metadata=True)
        else:
            assert out.column(name).to_pylist() == [r[name] for _, r in rows]
            assert out.schema.field(name).type == pa.int64()
    # rule 4: non-pandas keys kept, pandas key rebuilt with no index columns
    meta = out.schema.metadata
    assert meta[b"custom"] == b"keep-me"
    pandas_meta = json.loads(meta[b"pandas"])
    assert pandas_meta["index_columns"] == []
    assert [c["field_name"] for c in pandas_meta["columns"]] == out.column_names
    expected_meta = json.loads(reference_transform(tbl, ops).schema.metadata[b"pandas"])
    assert pandas_meta == expected_meta


class TestRowLineage:
    @pytest.mark.parametrize("name", _OP_NAMES)
    def test_each_op_alone(self, name):
        _check_lineage([_slot_op(name, 0)])

    @pytest.mark.parametrize(("first", "second"), list(itertools.product(_OP_NAMES, _OP_NAMES)))
    def test_every_adjacent_pair(self, first, second):
        _check_lineage([_slot_op(first, 0), _slot_op(second, 1)])

    def test_longer_chain(self):
        _check_lineage(
            [
                _slot_op("filter", 0),
                _slot_op("sort", 0),
                _slot_op("limit", 0),
                _slot_op("dedupe_subset", 0),
                _slot_op("drop_column", 0),
                _slot_op("derive", 0),
            ]
        )

    def test_dedupe_over_all_columns_ignores_the_ordinal(self):
        tbl = _lineage_table()
        out = apply_table_transforms(_cfg([_slot_op("dedupe_all", 0)]), "t", tbl)
        assert out.num_rows == 6
        assert out.column("id").to_pylist() == [0, 1, 2, 3, 4, 5]


class TestNameReuse:
    def _run(self, ops):
        return apply_table_transforms(_cfg(ops), "t", _lineage_table())

    def test_drop_then_derive_same_name_emits_derived_values(self):
        out = self._run(
            [
                {"op": "drop_column", "columns": ["extra"]},
                {"op": "derive", "column": "extra", "expression": "id * 100"},
            ]
        )
        assert out.column("extra").to_pylist() == [(i % 6) * 100 for i in range(12)]
        assert out.column_names[-1] == "extra"

    def test_derive_then_drop_emits_no_column(self):
        out = self._run(
            [
                {"op": "derive", "column": "y", "expression": "id + 1"},
                {"op": "drop_column", "columns": ["y"]},
            ]
        )
        assert "y" not in out.column_names

    def test_repeated_chain_emits_only_the_final_binding(self):
        ops: list[dict[str, Any]] = []
        for k in range(3):
            ops += [
                {"op": "drop_column", "columns": ["extra"] if k == 0 else ["z"]},
                {"op": "derive", "column": "z", "expression": f"id + {k}"},
            ]
        # first round drops the source `extra`; later rounds drop the derived z
        out = self._run(ops)
        assert out.column("z").to_pylist() == [(i % 6) + 2 for i in range(12)]
        assert out.column_names.count("z") == 1

    def test_column_order_follows_final_bindings(self):
        out = self._run(
            [
                {"op": "derive", "column": "a1", "expression": "id + 1"},
                {"op": "drop_column", "columns": ["id"]},
                {"op": "derive", "column": "a2", "expression": "a1 + 1"},
            ]
        )
        assert out.column_names == ["qn", "grp", "f", "d", "ts", "b", "s", "extra", "a1", "a2"]


class TestFrameDtypes:
    def test_null_free_ints_stay_numpy_and_metadata_ints_stay_extension(self, monkeypatch):
        seen: dict[str, Any] = {}
        real = _transforms.apply_transforms

        def spy(df, ops):
            seen.update({c: df[c].dtype for c in df.columns})
            return real(df, ops)

        monkeypatch.setattr(_transforms, "apply_transforms", spy)
        df = pd.DataFrame(
            {
                "plain": pd.array([1, 2, 3], dtype="int64"),
                "meta_i8": pd.array([1, 2, 3], dtype="Int8"),
                "meta_null": pd.array([1, None, 3], dtype="Int16"),
                "s": ["a", "b", "c"],
            }
        )
        with_meta = pa.Table.from_pandas(df, preserve_index=False)
        raw = pa.table(
            {
                "plain": pa.array([1, 2, 3], pa.int32()),
                "raw_null": pa.array([1, None, 3], pa.int32()),
                "s": pa.array(["a", "b", "c"]),
            }
        )
        apply_table_transforms(_cfg([{"op": "limit", "n": 3}]), "t", with_meta)
        assert seen["plain"] == np.dtype("int64")
        assert seen["meta_i8"] == pd.Int8Dtype()
        assert seen["meta_null"] == pd.Int16Dtype()
        apply_table_transforms(_cfg([{"op": "limit", "n": 3}]), "t", raw)
        assert seen["plain"] == np.dtype("int32")
        assert seen["raw_null"] == pd.Int32Dtype()


# --------------------------------------------------------------------------
# Exact preservation fixtures
# --------------------------------------------------------------------------


def _chunked_dictionaries() -> pa.ChunkedArray:
    a = pa.DictionaryArray.from_arrays(pa.array([0, 1, 0], pa.int8()), pa.array(["x", "y"]))
    b = pa.DictionaryArray.from_arrays(pa.array([1, 0, 1], pa.int8()), pa.array(["q", "z"]))
    return pa.chunked_array([a, b])


def _exact_table() -> pa.Table:
    n = 6
    cols: dict[str, pa.Array | pa.ChunkedArray] = {}
    for width in (8, 16, 32, 64):
        for signed in (True, False):
            t = getattr(pa, f"{'' if signed else 'u'}int{width}")()
            hi = (2 ** (width - 1) - 1) if signed else (2**width - 1)
            lo = -(2 ** (width - 1)) if signed else 0
            cols[f"{t}_full"] = pa.array([lo, 0, 1, hi, hi - 1, lo + 1], t)
            cols[f"{t}_null"] = pa.array([lo, None, 1, hi, None, 7], t)
            cols[f"{t}_allnull"] = pa.array([None] * n, t)
    cols["u64_big"] = pa.array([2**63 + 1, None, 2**63 + 3, 2**64 - 1, 5, 2**63], pa.uint64())
    cols["f_nan_null"] = pa.array([float("nan"), None, 1.5, -0.0, float("inf"), 2.0], pa.float64())
    cols["f32"] = pa.array([float("nan"), None, 1.5, -0.0, float("inf"), 2.0], pa.float32())
    cols["f_nonnull"] = pa.array([float("nan"), 1.0, 2.0, 3.0, 4.0, 5.0], pa.float64())
    cols["str"] = pa.array(["a", None, "c", "", "e", "f"])
    cols["bool"] = pa.array([True, None, False, True, False, True])
    cols["date32"] = pa.array([0, None, 19000, -5, 1, 2], pa.date32())
    cols["date64"] = pa.array([0, None, 86400000 * 3, 86400000, 0, 86400000 * 9], pa.date64())
    nat = -(2**63)
    for unit in ("s", "ms", "us", "ns"):
        cols[f"ts_{unit}"] = pa.array([0, None, 1_700_000_000, nat, 5, 6], pa.timestamp(unit))
        for tz in ("UTC", "America/New_York"):
            cols[f"ts_{unit}_{tz}"] = pa.array(
                [0, None, 1_699_164_000, nat, 1_699_167_600, 6], pa.timestamp(unit, tz=tz)
            )
    from decimal import Decimal

    cols["dec"] = pa.array(
        [
            Decimal("1.2345"),
            None,
            Decimal("-99999999.9999"),
            Decimal("0"),
            Decimal("1"),
            Decimal("2"),
        ],
        pa.decimal128(12, 4),
    )
    for idx in (pa.int8(), pa.int32()):
        for ordered in (False, True):
            cols[f"dict_{idx}_{ordered}"] = pa.DictionaryArray.from_arrays(
                pa.array([0, None, 1, 0, 1, 1], idx), pa.array(["u", "v"]), ordered=ordered
            )
    cols["dict_chunked"] = _chunked_dictionaries()
    cols["id"] = pa.array(list(range(n)), pa.int64())
    tbl = pa.table(cols)
    fields = [
        f.with_metadata({"k": f"meta-{f.name}"}) if i % 3 == 0 else f
        for i, f in enumerate(tbl.schema)
    ]
    fields[-1] = fields[-1].with_nullable(True)
    meta = {b"custom": b"kept", b"pandas": b'{"index_columns": [], "columns": [], "creator": {}}'}
    return tbl.cast(pa.schema(fields)).replace_schema_metadata(meta)


class TestExactPreservation:
    NEUTRAL = [{"op": "limit", "n": 6}]

    @pytest.mark.parametrize(
        "ops",
        [
            [{"op": "limit", "n": 6}],
            [{"op": "sort", "by": ["id"]}],
            [{"op": "filter", "expression": "id >= 0"}, {"op": "limit", "n": 6}],
        ],
        ids=["limit", "sort", "filter_limit"],
    )
    def test_every_surviving_column_is_take_of_the_source(self, ops):
        tbl = _exact_table()
        out = apply_table_transforms(
            _cfg(ops, columns=[{"name": "str", "strategy": "redact"}]), "t", tbl
        )
        assert out.column_names == tbl.column_names
        for name in tbl.column_names:
            _same_column(out.column(name), tbl.column(name))
            assert out.schema.field(name).equals(tbl.schema.field(name), check_metadata=True)
        assert out.schema.metadata[b"custom"] == b"kept"
        assert json.loads(out.schema.metadata[b"pandas"])["index_columns"] == []

    def test_select_subset_takes_with_metadata(self):
        tbl = _exact_table()
        ops = [
            {"op": "filter", "expression": "id != 1"},
            {"op": "sort", "by": ["id"], "ascending": False},
        ]
        out = apply_table_transforms(
            _cfg(ops, columns=[{"name": "str", "strategy": "redact"}]), "t", tbl
        )
        ordinals = pa.array([5, 4, 3, 2, 0], pa.int64())
        for name in tbl.column_names:
            _same_column(out.column(name), pc.take(tbl.column(name), ordinals))


# --------------------------------------------------------------------------
# Source tables written from pandas, end to end through the masking adapter
# --------------------------------------------------------------------------


def _df(n: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": pd.array(list(range(n)), dtype="int64"),
            "x": pd.array([127, 1, 2, 3][:n], dtype="int8"),
            "s": [f"secret{i}" for i in range(n)],
        }
    )


def _exact_view_tables() -> dict[str, pa.Table]:
    out: dict[str, pa.Table] = {}
    df = _df()
    df.index = pd.Index(["a", "b", "a", "c"], name="ix")
    out["ordinary_index"] = pa.Table.from_pandas(df)
    df = _df()
    df.index = pd.Index(pd.array([1, None, 3, 3], dtype="Int64"), name="ix")
    out["nullable_int_index"] = pa.Table.from_pandas(df)
    df = _df()
    df.index = pd.MultiIndex.from_arrays(
        [pd.array([1, None, 2, 2], dtype="Int64"), ["x", "y", "x", "y"]], names=["l1", "l2"]
    )
    out["nullable_multiindex_level"] = pa.Table.from_pandas(df)
    out["range_metadata"] = pa.Table.from_pandas(_df())
    out["no_metadata"] = pa.table(
        {
            "id": pa.array([0, 1, 2, 3], pa.int64()),
            "x": pa.array([127, 1, 2, 3], pa.int8()),
            "s": pa.array([f"secret{i}" for i in range(4)]),
        }
    )
    df = _df()
    df["x"] = pd.array([127, 1, 2, 3], dtype="Int8")
    df["u"] = pd.array([255, 1, 2, 3], dtype="UInt8")
    out["meta_null_free_ext"] = pa.Table.from_pandas(df, preserve_index=False)
    df = _df()
    df["x"] = pd.array([127, None, 2, 3], dtype="Int8")
    df["allna"] = pd.array([None, None, None, None], dtype="UInt16")
    df["wide"] = pd.array([2**62, None, 5, 6], dtype="Int64")
    out["meta_nullable_ext"] = pa.Table.from_pandas(df, preserve_index=False)
    return out


def _capture_frames(monkeypatch):
    seen: list[dict[str, Any]] = []
    real = PandasExecutionAdapter._dispatch_mask_node

    def spy(self, node, frames, *a, **k):
        seen.append({str(c): str(d) for c, d in frames["t"].dtypes.items()})
        return real(self, node, frames, *a, **k)

    monkeypatch.setattr(PandasExecutionAdapter, "_dispatch_mask_node", spy)
    return seen


def _run(cfg, tbl, **kw):
    return run_pipeline(
        cfg, {"t": tbl}, engine_version=ENGINE_VERSION, unified_slice_enabled=False, **kw
    )


@pytest.mark.parametrize("shape", sorted(_exact_view_tables()))
class TestExactViewShapes:
    def test_neutral_transform_equals_no_transform(self, tmp_path, monkeypatch, shape):
        tbl = _exact_view_tables()[shape]
        plain_cfg = single_table_config(tmp_path, tbl)
        neutral_cfg = single_table_config(
            tmp_path, tbl, transforms=[{"op": "limit", "n": tbl.num_rows}]
        )
        seen = _capture_frames(monkeypatch)
        plain = _run(plain_cfg, tbl)
        neutral = _run(neutral_cfg, tbl)
        tables_equal(neutral.outputs["t"], plain.outputs["t"])
        assert len(seen) == 2
        assert seen[0] == seen[1], "adapter-visible dtypes must match the no-transform run"
        assert list(seen[0]) == list(seen[1])

    def test_boundary_arithmetic_agrees_with_the_bare_pandas_view(self, shape):
        tbl = _exact_view_tables()[shape]
        ops = [{"op": "derive", "column": "x1", "expression": "x + 1"}]
        out = apply_table_transforms(_cfg(ops), "t", tbl)
        bare = tbl.to_pandas()
        expected = pa.array(bare.eval("x + 1"), from_pandas=True)
        assert out.column("x1").to_pylist() == expected.to_pylist()
        assert out.column("x1").type == expected.type


class TestStoredIndexSource:
    def _tbl(self) -> pa.Table:
        df = pd.DataFrame(
            {
                "id": [1, 2, 2, 3],
                "s": ["a", "b", "b", "c"],
                "x": pd.array([5, 6, 6, 7], dtype="int64"),
            },
            index=pd.Index([7, 7, 3, 3], name="ix"),
        )
        return pa.Table.from_pandas(df)

    def test_output_hides_the_stored_index_and_matches_no_transform(self, tmp_path):
        tbl = self._tbl()
        assert "ix" in tbl.column_names
        neutral = [{"op": "limit", "n": 4}]
        plain = _run(single_table_config(tmp_path, tbl), tbl).outputs["t"]
        with_t = _run(single_table_config(tmp_path, tbl, transforms=neutral), tbl).outputs["t"]
        assert "ix" not in with_t.column_names
        tables_equal(with_t, plain)

    def test_dedupe_all_matches_the_no_transform_frame(self):
        tbl = self._tbl()
        expected = tbl.to_pandas().reset_index(drop=True).drop_duplicates().index.tolist()
        out = apply_table_transforms(_cfg([{"op": "dedupe", "columns": None}]), "t", tbl)
        assert out.column("id").to_pylist() == [tbl.column("id").to_pylist()[i] for i in expected]
        assert "ix" not in out.column_names

    def test_stored_index_is_not_visible_to_expressions(self):
        tbl = self._tbl()
        with pytest.raises(TransformError) as exc:
            apply_table_transforms(_cfg([{"op": "filter", "expression": "ix > 0"}]), "t", tbl)
        assert exc.value.code == "filter_expression_error"
        with pytest.raises(TransformError) as exc:
            apply_table_transforms(
                _cfg([{"op": "derive", "column": "y", "expression": "ix + 1"}]), "t", tbl
            )
        assert exc.value.code == "derive_expression_error"


# --------------------------------------------------------------------------
# Nullable integers
# --------------------------------------------------------------------------


class TestMetadataBackedNullableIntegers:
    """Group (i): exact in both the old platform path and the new one."""

    @pytest.mark.parametrize(
        "dtype", ["Int8", "UInt8", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"]
    )
    @pytest.mark.parametrize("with_null", [False, True])
    def test_boundaries_equal_the_platform_path(self, dtype, with_null):
        hi = int(np.iinfo(dtype.lower()).max)
        values = [hi, 1, None if with_null else 2, hi - 1]
        tbl = pa.Table.from_pandas(
            pd.DataFrame({"x": pd.array(values, dtype=dtype), "s": list("abcd")}),
            preserve_index=False,
        )
        ops = [
            {"op": "derive", "column": "x1", "expression": "x + 1"},
            {"op": "filter", "expression": "x > 1"},
        ]
        platform = tbl.to_pandas()
        platform = platform.assign(x1=platform.eval("x + 1"))
        platform = platform[platform.eval("x > 1")]
        out = apply_table_transforms(_cfg(ops), "t", tbl)
        assert out.column("x").to_pylist() == platform["x"].tolist() or out.column(
            "x"
        ).to_pylist() == [None if pd.isna(v) else v for v in platform["x"]]
        assert out.column("x1").to_pylist() == [
            None if pd.isna(v) else int(v) for v in platform["x1"]
        ]


class TestRawNullableIntegers:
    """Group (ii): raw (no pandas metadata) nullable integers become exact."""

    def _raw(self) -> pa.Table:
        return pa.table(
            {
                "i8": pa.array([127, None, 5, 127], pa.int8()),
                "u8": pa.array([255, None, 5, 255], pa.uint8()),
                "i64": pa.array([2**53 + 1, None, 2**53, 2**53 + 1], pa.int64()),
                "u64": pa.array([2**63 + 1, None, 2**63 + 3, 2**63 + 1], pa.uint64()),
                "allnull": pa.array([None, None, None, None], pa.int32()),
                "s": pa.array(["a", "b", "c", "d"]),
            }
        )

    def test_boundaries_match_the_exact_reference(self):
        tbl = self._raw()
        ops = [
            {"op": "derive", "column": "i8p", "expression": "i8 + 1"},
            {"op": "derive", "column": "u8p", "expression": "u8 + 1"},
            {"op": "filter", "expression": "i8 > 0"},
        ]
        out = apply_table_transforms(_cfg(ops), "t", tbl)
        ref = reference_transform(tbl, ops)
        tables_equal(out, ref)
        assert out.column("i8p").to_pylist() == [-128, 6, -128]  # int8 127 + 1 wraps
        assert out.schema.field("i8").type == pa.int8()

    def test_beyond_2_53_filter_sort_dedupe_are_exact(self):
        tbl = self._raw()
        ops = [
            {"op": "filter", "expression": "i64 > 9007199254740992"},
            {"op": "sort", "by": ["i64"], "ascending": False},
            {"op": "dedupe", "columns": ["i64"]},
        ]
        out = apply_table_transforms(_cfg(ops), "t", tbl)
        assert out.column("i64").to_pylist() == [2**53 + 1]
        assert out.column("s").to_pylist() == ["a"]
        u = apply_table_transforms(
            _cfg([{"op": "sort", "by": ["u64"]}, {"op": "dedupe", "columns": ["u64"]}]), "t", tbl
        )
        assert u.column("u64").to_pylist() == [2**63 + 1, 2**63 + 3, None]
        assert u.column("s").to_pylist() == ["a", "c", "b"]
        allnull = apply_table_transforms(_cfg([{"op": "limit", "n": 4}]), "t", tbl)
        assert allnull.schema.field("allnull").type == pa.int32()
        assert allnull.column("allnull").null_count == 4

    def test_characterization_no_transform_path_rounds(self, tmp_path):
        """Known, separately tracked rounding (roadmap item 1): recorded, not asserted equal."""
        tbl = self._raw()
        bare = tbl.to_pandas()
        assert bare["i64"].dtype == np.float64
        assert bare["i64"].iloc[0] == bare["i64"].iloc[2]  # 2**53 + 1 collapsed onto 2**53
        plain = _run(single_table_config(tmp_path, tbl), tbl).outputs["t"]
        assert plain.schema.field("i8").type == pa.float64()
        neutral = _run(
            single_table_config(tmp_path, tbl, transforms=[{"op": "limit", "n": 4}]), tbl
        ).outputs["t"]
        assert neutral.schema.field("i8").type == pa.int8()
        assert neutral.column("i64").to_pylist() == tbl.column("i64").to_pylist()


# --------------------------------------------------------------------------
# Schema guard
# --------------------------------------------------------------------------


def _stored_table() -> pa.Table:
    df = pd.DataFrame({"id": [1, 2, 3], "s": ["a", "b", "c"]}, index=pd.Index([9, 8, 7], name="ix"))
    return pa.Table.from_pandas(df)


def _dup_table() -> pa.Table:
    return pa.Table.from_arrays(
        [pa.array([1, 2]), pa.array([3, 4]), pa.array(["a", "b"])], names=["a", "a", "s"]
    )


def _guard_cfg(kind: str) -> dict[str, Any]:
    limit = [{"op": "limit", "n": 2}]
    cols: list[dict[str, Any]] = [{"name": "s", "strategy": "redact"}]
    cfg: dict[str, Any] = {}
    ops = limit
    if kind == "mask_column":
        cols = [{"name": "ix", "strategy": "redact"}]
    elif kind == "date_shift_group_by":
        cols = [
            {
                "name": "s",
                "strategy": "date_shift",
                "provider_config": {"group_by": "ix", "min_days": -1, "max_days": 1},
            }
        ]
    elif kind == "group_key_group_by":
        cols = [{"name": "s", "strategy": "group_key", "provider_config": {"group_by": "ix"}}]
    elif kind == "top_code":
        cols = [{"name": "ix", "strategy": "top_code", "provider_config": {"threshold": 5}}]
    elif kind == "relationship_parent":
        cfg["relationships"] = [
            {
                "parent": {"table": "t", "columns": ["ix"]},
                "children": [{"table": "other", "columns": ["k"]}],
                "orphan_policy": "preserve",
            }
        ]
    elif kind == "relationship_child":
        cfg["relationships"] = [
            {
                "parent": {"table": "other", "columns": ["k"]},
                "children": [{"table": "t", "columns": ["ix"]}],
                "orphan_policy": "preserve",
            }
        ]
    elif kind == "sort_by":
        ops = [{"op": "sort", "by": ["ix"]}]
    elif kind == "dedupe_columns":
        ops = [{"op": "dedupe", "columns": ["ix"]}]
    elif kind == "drop_column":
        ops = [{"op": "drop_column", "columns": ["ix"]}]
    else:  # pragma: no cover
        raise AssertionError(kind)
    cfg["tables"] = [{"name": "t", "columns": cols, "transforms": ops}]
    return cfg


_GUARD_KINDS = [
    "mask_column",
    "date_shift_group_by",
    "group_key_group_by",
    "top_code",
    "relationship_parent",
    "relationship_child",
    "sort_by",
    "dedupe_columns",
    "drop_column",
]


class TestSchemaGuard:
    @pytest.mark.parametrize("kind", _GUARD_KINDS)
    def test_structured_reference_to_stored_index_is_rejected(self, kind):
        cfg = _guard_cfg(kind)
        with pytest.raises(TransformError) as exc:
            apply_table_transforms(cfg, "t", _stored_table())
        assert exc.value.code == _STORED

    def test_derive_then_reference_by_same_name_is_accepted(self):
        ops = [
            {"op": "derive", "column": "ix", "expression": "id + 100"},
            {"op": "sort", "by": ["ix"], "ascending": False},
        ]
        out = apply_table_transforms(_cfg(ops), "t", _stored_table())
        assert out.column("ix").to_pylist() == [103, 102, 101]
        assert out.column("id").to_pylist() == [3, 2, 1]

    def test_derived_mask_column_named_like_the_stored_index_is_accepted(self):
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [{"name": "ix", "strategy": "redact"}],
                    "transforms": [{"op": "derive", "column": "ix", "expression": "id * 2"}],
                }
            ]
        }
        assert apply_table_transforms(cfg, "t", _stored_table()).column_names[-1] == "ix"

    def test_duplicate_physical_names_rejected(self):
        with pytest.raises(TransformError) as exc:
            apply_table_transforms(_cfg([{"op": "limit", "n": 1}]), "t", _dup_table())
        assert exc.value.code == _DUP

    def test_without_transforms_behaves_as_on_main(self):
        assert apply_table_transforms(_cfg([]), "t", _dup_table()) is not None
        tbl = _stored_table()
        assert apply_table_transforms(_cfg([]), "t", tbl) is tbl


class _Events:
    def __init__(self, monkeypatch):
        from decoy_engine.execution import _transforms_table

        self.events: list[str] = []
        real_guard = _transforms.check_transform_source_schema
        real_frame = _transforms_table.to_transform_frame

        def guard(*a, **k):
            self.events.append("guard")
            return real_guard(*a, **k)

        def frame(*a, **k):
            self.events.append("convert")
            return real_frame(*a, **k)

        monkeypatch.setattr(_transforms, "check_transform_source_schema", guard)
        monkeypatch.setattr(_transforms_table, "to_transform_frame", frame)


class TestGuardPlacement:
    OPS = [{"op": "limit", "n": 2}]

    def test_runs_once_before_conversion_for_the_public_helper(self, monkeypatch):
        ev = _Events(monkeypatch)
        apply_table_transforms(
            _cfg(self.OPS), "t", pa.table({"id": [1, 2, 3], "s": ["a", "b", "c"]})
        )
        assert ev.events == ["guard", "convert"]

    def test_runs_once_before_conversion_full_frame(self, tmp_path, monkeypatch):
        tbl = pa.table({"id": [1, 2, 3], "s": ["a", "b", "c"]})
        cfg = single_table_config(tmp_path, tbl, transforms=self.OPS)
        ev = _Events(monkeypatch)
        _run(cfg, tbl)
        assert ev.events == ["guard", "convert"]

    def test_runs_once_per_table_before_conversion_sequential(self, tmp_path, monkeypatch):
        from tests.unit.execution._transform_testkit import fk_config, fk_tables

        parent, child = fk_tables()
        cfg = fk_config(
            tmp_path, parent, child, parent_transforms=self.OPS, child_transforms=self.OPS
        )
        ev = _Events(monkeypatch)
        run_pipeline(
            cfg,
            {"parent": parent, "child": child},
            engine_version=ENGINE_VERSION,
            execution_mode="sequential",
        )
        assert ev.events == ["guard", "convert", "guard", "convert"]

    def test_zero_runs_on_config_rejected_routes(self, tmp_path, monkeypatch):
        from decoy_engine.execution._chunked import run_mask_pipeline_chunked
        from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked
        from tests.unit.execution._transform_testkit import fk_config, fk_tables

        ev = _Events(monkeypatch)
        parent, child = fk_tables()
        cfg = fk_config(tmp_path, parent, child, parent_transforms=self.OPS)
        with pytest.raises(PlanCompileError):
            run_pipeline(cfg, {}, engine_version=ENGINE_VERSION, execution_mode="out_of_core")
        single = single_table_config(
            tmp_path, pa.table({"a": [1], "s": ["x"]}), transforms=self.OPS
        )

        class _Poison:
            def __iter__(self):
                raise AssertionError("consumed")

        for fn in (run_mask_pipeline_chunked, run_native_or_oracle_chunked):
            with pytest.raises(PlanCompileError) as exc:
                fn(single, _Poison(), table="t", engine_version=ENGINE_VERSION)
            assert exc.value.code == "per_table_transforms_present"
        assert ev.events == []

    def test_config_rejection_wins_over_both_schema_defects(self, tmp_path, monkeypatch):
        """A source with a duplicate field AND a stored-index reference still gets
        `per_table_transforms_present` on every config-rejected route, and the
        schema guard never runs."""
        import decoy_engine
        from decoy_engine.execution._chunked import run_mask_pipeline_chunked
        from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked
        from decoy_engine.execution.physical.drivers._chunked import (
            MaskPipelineChunkedAdapter,
            NativeOrOracleChunkedAdapter,
            ResidentChunkedAggregatorAdapter,
        )

        df = pd.DataFrame({"a": [1, 2], "s": ["x", "y"]}, index=pd.Index([9, 8], name="ix"))
        base = pa.Table.from_pandas(df)
        defective = pa.Table.from_arrays(
            [*base.columns, base.column("a")], names=[*base.column_names, "a"]
        ).replace_schema_metadata(base.schema.metadata)
        cfg = single_table_config(
            tmp_path,
            pa.table({"a": [1], "s": ["x"]}),
            transforms=[{"op": "sort", "by": ["ix"]}],
        )
        ev = _Events(monkeypatch)
        chunked_calls = [
            lambda: run_mask_pipeline_chunked(
                cfg, iter([defective]), table="t", engine_version=ENGINE_VERSION
            ),
            lambda: run_native_or_oracle_chunked(
                cfg, iter([defective]), table="t", engine_version=ENGINE_VERSION
            ),
            lambda: MaskPipelineChunkedAdapter().run(
                cfg, iter([defective]), table="t", engine_version=ENGINE_VERSION
            ),
            lambda: NativeOrOracleChunkedAdapter().run(
                cfg, iter([defective]), table="t", engine_version=ENGINE_VERSION
            ),
            lambda: ResidentChunkedAggregatorAdapter().run(
                cfg,
                defective,
                table="t",
                engine_version=ENGINE_VERSION,
                registry=decoy_engine.get_default_registry(),
                adapter=None,
                vault_writer=None,
                chunk_size_rows=1,
            ),
            lambda: run_pipeline(
                cfg,
                {"t": defective},
                engine_version=ENGINE_VERSION,
                execution_mode="out_of_core",
            ),
        ]
        for call in chunked_calls:
            with pytest.raises(PlanCompileError) as exc:
                call()
            assert exc.value.code == "per_table_transforms_present"
        assert ev.events == []


class TestDefectsThroughRoutes:
    """Both defects are rejected before any value is read, on every applying route."""

    def _poison(self, monkeypatch):
        from decoy_engine.execution import _transforms_table

        def boom(*a, **k):
            raise AssertionError("value access before the schema guard")

        monkeypatch.setattr(_transforms_table, "to_transform_frame", boom)

    @pytest.mark.parametrize("defect", ["dup", "stored"])
    def test_public_helper(self, monkeypatch, defect):
        self._poison(monkeypatch)
        tbl, cfg, code = self._case(defect)
        with pytest.raises(TransformError) as exc:
            apply_table_transforms(cfg, "t", tbl)
        assert exc.value.code == code

    @staticmethod
    def _case(defect):
        if defect == "dup":
            return _dup_table(), _cfg([{"op": "limit", "n": 1}]), _DUP
        return (
            _stored_table(),
            _cfg([{"op": "sort", "by": ["ix"]}]),
            _STORED,
        )

    @pytest.mark.parametrize("defect", ["dup", "stored"])
    def test_full_frame(self, tmp_path, monkeypatch, defect):
        tbl, _, code = self._case(defect)
        ops = [{"op": "limit", "n": 1}] if defect == "dup" else [{"op": "sort", "by": ["ix"]}]
        clean = pa.table({"a": [1, 2], "s": ["a", "b"]}) if defect == "dup" else tbl
        cfg = single_table_config(
            tmp_path, clean, transforms=ops, columns=[{"name": "s", "strategy": "redact"}]
        )
        self._poison(monkeypatch)
        with pytest.raises(TransformError) as exc:
            _run(cfg, tbl)
        assert exc.value.code == code

    @pytest.mark.parametrize("defect", ["dup", "stored"])
    def test_sequential(self, tmp_path, monkeypatch, defect):
        from tests.unit.execution._transform_testkit import fk_config, fk_tables

        parent, child = fk_tables()
        bad = {"dup": [{"op": "limit", "n": 1}], "stored": [{"op": "sort", "by": ["ix"]}]}[defect]
        cfg = fk_config(tmp_path, parent, child, parent_transforms=bad)
        if defect == "dup":
            parent = pa.Table.from_arrays(
                [*parent.columns, parent.column("id")], names=[*parent.column_names, "id"]
            )
        else:
            df = parent.to_pandas()
            df.index = pd.Index(list(reversed(range(len(df)))), name="ix", dtype="int64")
            parent = pa.Table.from_pandas(df)
        self._poison(monkeypatch)
        with pytest.raises(TransformError) as exc:
            run_pipeline(
                cfg,
                {"parent": parent, "child": child},
                engine_version=ENGINE_VERSION,
                execution_mode="sequential",
            )
        assert exc.value.code == {"dup": _DUP, "stored": _STORED}[defect]

    def test_same_tables_without_transforms_behave_as_on_main(self, tmp_path):
        tbl = _stored_table()
        cfg = single_table_config(tmp_path, tbl, columns=[{"name": "s", "strategy": "redact"}])
        out = _run(cfg, tbl).outputs["t"]
        assert "ix" not in out.column_names
        assert cleared(cfg)["tables"][0]["transforms"] == []
        assert reference_frame(tbl).shape[0] == 3
