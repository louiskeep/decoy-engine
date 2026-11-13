"""The post-scan gate counts orphans by the FK join's own key identity.

The oracle is the real pandas full-frame join, run with `orphan_policy: warn`, which reports the
number of child rows it left unmatched. The gate side is `check_keys` over the same tables. A
mixed-type pair (a string "7" against the integer 7, a date against a midnight timestamp) is
where an Arrow cast would disagree with the join in either direction, so each pair is checked
here and the two counts must be equal."""

from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons, run_pipeline
from decoy_engine.config import PipelineConfig, RelationshipConfig
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.verification.postmask import check_keys, from_table
from tests._declared_schema import declare_rest

D1, D2, D3 = dt.date(2024, 1, 1), dt.date(2024, 1, 2), dt.date(2024, 1, 3)


def _at(day: dt.date, hour: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour)


def _join_orphans(tmp_path: Path, parent: pa.Array, child: pa.Array) -> int:
    """Child rows the full-frame pandas join left unmatched."""
    tables = {
        "parent": pa.table({"id": parent, "note": [f"n{i}" for i in range(len(parent))]}),
        "child": pa.table({"pid": child, "note": [f"n{i}" for i in range(len(child))]}),
    }
    sources, targets = {}, {}
    for name, table in tables.items():
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path)
        sources[name] = {"type": "file", "format": "parquet", "path": str(path)}
        targets[name] = {"type": "file", "format": "parquet", "path": str(tmp_path / f"o_{name}")}
    raw = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": sources,
        "targets": targets,
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {"name": "id", "strategy": "passthrough"},
                    {"name": "note", "strategy": "redact"},
                ],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "pid", "strategy": "passthrough"},
                    {"name": "note", "strategy": "redact"},
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "warn",
                "namespace": "ns",
            }
        ],
    }
    config = PipelineConfig.model_validate(declare_rest(raw)).model_dump()
    result = run_pipeline(
        copy.deepcopy(config),
        dict(tables),
        engine_version="key-identity",
        key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
        execution_mode="full_frame",
    )
    return sum(
        int(w.detail["orphan_rows"]) for w in result.warnings if w.code == reasons.warning.ORPHAN_FK
    )


def _gate_orphans(tmp_path: Path, parent: pa.Array, child: pa.Array) -> int:
    rel = RelationshipConfig.model_validate(
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["pid"]}],
            "orphan_policy": "warn",
        }
    )
    data = {
        "parent": from_table("parent", pa.table({"id": parent})),
        "child": from_table("child", pa.table({"pid": child})),
    }
    found = check_keys([rel], data, data, scratch=tmp_path / "scratch", memory_limit="64MB")
    (res,) = (r for r in found if (r.table, r.kind) == ("child", "foreign"))
    assert res.status == "checked"
    return res.output.orphan_rows


def _dec(*values: str) -> pa.Array:
    return pa.array([Decimal(v) for v in values], pa.decimal128(12, 2))


CASES: dict[str, tuple[pa.Array, pa.Array]] = {
    "string-parent-int-child": (pa.array(["1", "2"]), pa.array([1, 2, 3])),
    "int-parent-string-child": (pa.array([1, 2]), pa.array(["1", "2", "3"])),
    "int-parent-whole-float-child": (pa.array([1, 2]), pa.array([1.0, 2.0, 3.0])),
    "int-parent-fractional-float-child": (pa.array([1, 2]), pa.array([1.5, 2.0, 3.0])),
    "float-parent-int-child": (pa.array([1.0, 2.5]), pa.array([1, 2, 3])),
    "bool-parent-int-child": (pa.array([True, False]), pa.array([1, 0, 1])),
    "int-parent-bool-child": (pa.array([0, 1]), pa.array([True, False, True])),
    "decimal-parent-int-child": (_dec("1.00", "2.50"), pa.array([1, 2, 3])),
    "int-parent-decimal-child": (pa.array([1, 2]), _dec("1.00", "2.00", "2.50")),
    "date-parent-timestamp-child": (
        pa.array([D1, D2], pa.date32()),
        pa.array([_at(D1), _at(D2, 12), _at(D3)], pa.timestamp("us")),
    ),
    "timestamp-parent-date-child": (
        pa.array([_at(D1), _at(D2, 12)], pa.timestamp("us")),
        pa.array([D1, D2, D3], pa.date32()),
    ),
    "timestamp-us-parent-ns-child": (
        pa.array([_at(D1), _at(D2, 5)], pa.timestamp("us")),
        pa.array([_at(D1), _at(D2, 5), _at(D3)], pa.timestamp("ns")),
    ),
    "timestamp-s-parent-ms-child": (
        pa.array([_at(D1), _at(D2, 5)], pa.timestamp("s")),
        pa.array([_at(D1), _at(D2, 5), _at(D3)], pa.timestamp("ms")),
    ),
    "tz-timestamp-us-parent-ns-child": (
        pa.array([_at(D1), _at(D2, 5)], pa.timestamp("us", tz="UTC")),
        pa.array([_at(D1), _at(D2, 5), _at(D3)], pa.timestamp("ns", tz="UTC")),
    ),
    "duration-us-parent-ns-child": (
        pa.array([1, 2], pa.duration("us")),
        pa.array([1000, 2000, 3000], pa.duration("ns")),
    ),
    "duration-ns-keys-one-nanosecond-apart": (
        pa.array([1, 2], pa.duration("ns")),
        pa.array([1, 2, 3], pa.duration("ns")),
    ),
    "tz-timestamp-ns-keys-one-nanosecond-apart": (
        pa.array([10**18 + 1, 10**18 + 2], pa.timestamp("ns", tz="UTC")),
        pa.array([10**18 + 1, 10**18 + 2, 10**18 + 3], pa.timestamp("ns", tz="UTC")),
    ),
    "int8-parent-uint64-child": (
        pa.array([1, 2], pa.int8()),
        pa.array([1, 2, 3, 2**63 + 1], pa.uint64()),
    ),
    "int64-parent-uint64-child": (
        pa.array([1, 2**40], pa.int64()),
        pa.array([1, 2**40, 5], pa.uint64()),
    ),
    "string-parent-dictionary-child": (
        pa.array(["a", "b"]),
        pa.array(["a", "z", "b"]).dictionary_encode(),
    ),
    "dictionary-parent-large-string-child": (
        pa.array(["a", "b"]).dictionary_encode(),
        pa.array(["a", "z", "b"], pa.large_string()),
    ),
    "nulls-in-the-child": (pa.array(["1", "2"]), pa.array([1, None, 3])),
    "float-parent-float-child-with-nan": (
        pa.array([1.0, 2.0]),
        pa.array([1.0, float("nan"), 3.0]),
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_the_gate_counts_the_orphans_the_join_leaves(tmp_path: Path, name: str) -> None:
    parent, child = CASES[name]
    (tmp_path / "join").mkdir()
    want = _join_orphans(tmp_path / "join", parent, child)
    assert _gate_orphans(tmp_path, parent, child) == want


def test_the_battery_is_not_vacuous(tmp_path: Path) -> None:
    """A string never equals an integer to the join, so every child is an orphan, and a date
    never equals a midnight timestamp."""
    parent, child = CASES["string-parent-int-child"]
    assert _gate_orphans(tmp_path, parent, child) == 3
    parent, child = CASES["date-parent-timestamp-child"]
    assert _gate_orphans(tmp_path, parent, child) == 3


def test_a_nan_float_key_counts_as_a_null_key_as_the_join_reads_it(tmp_path: Path) -> None:
    rel = RelationshipConfig.model_validate(
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["pid"]}],
            "orphan_policy": "warn",
        }
    )
    data = {
        "parent": from_table("parent", pa.table({"id": pa.array([1.0, float("nan")])})),
        "child": from_table("child", pa.table({"pid": pa.array([1.0, float("nan")])})),
    }
    found = check_keys([rel], data, data, scratch=tmp_path / "scratch", memory_limit="64MB")
    (pk,) = (r for r in found if (r.table, r.kind) == ("parent", "primary"))
    (fk,) = (r for r in found if (r.table, r.kind) == ("child", "foreign"))
    assert pk.output.null_key_rows == 1
    assert (fk.output.orphan_rows, fk.output.null_key_rows) == (0, 1)


@pytest.mark.parametrize(
    "keys",
    [
        pa.array([1, 2], pa.duration("ns")),
        pa.array([10**18 + 1, 10**18 + 2], pa.timestamp("ns", tz="UTC")),
        pa.array([1, 2], pa.time64("ns")),
    ],
    ids=["duration", "tz-timestamp", "time-of-day"],
)
def test_keys_one_nanosecond_apart_stay_distinct(tmp_path: Path, keys: pa.Array) -> None:
    """DuckDB keeps these types at microseconds; two keys 1 ns apart must not read as one."""
    rel = RelationshipConfig.model_validate(
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["pid"]}],
            "orphan_policy": "warn",
        }
    )
    data = {
        "parent": from_table("parent", pa.table({"id": keys})),
        "child": from_table("child", pa.table({"pid": keys})),
    }
    found = check_keys([rel], data, data, scratch=tmp_path / "scratch", memory_limit="64MB")
    (pk,) = (r for r in found if (r.table, r.kind) == ("parent", "primary"))
    assert pk.status == "checked"
    assert pk.output.duplicate_excess == 0
