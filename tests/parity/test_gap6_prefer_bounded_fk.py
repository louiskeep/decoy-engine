"""Real runs for the prefer-bounded FK routing rule (`run_pipeline`, no stubs).

`tests/unit/execution/test_prefer_bounded_fk_routing.py` pins the routing decision.
This module runs the jobs end to end and pins what the diverted routes actually
produce, against the full-frame oracle forced with `execution_mode="full_frame"`:

  - T5a, diverted to SEQUENTIAL: byte-identical to the oracle for every strategy on
    NFC-stable text. The one accepted divergence is the native-versus-Python NFC
    normalization of hash/categorical/bucket keys for a codepoint newer than the
    running Python's Unicode tables (tests/parity/SEMANTIC_DIFFERENCES.md); the
    fixtures stay inside it. A negative control proves the comparator bites.
  - T5b, diverted to OUT-OF-CORE: the strict dimensions the route is documented to
    differ on (pandas schema metadata, and for empty/all-null shapes the schema) are
    pinned literally, everything else equal, and referential integrity holds.
  - T5c, no new failing job: a key type out-of-core rejects but the oracle completes
    still completes (on sequential) when it fits, on the estimate path and the probe
    path.

Needs the compiled companion for the native hash/categorical/bucket kernels on the
SEQ route (the `decoy-d1a-abi3-venv` interpreter); without it those columns run the
Python handlers and the comparison is trivially exact.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _pipeline_routing_signals as signals
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import run_pipeline
from tests.parity import _strict_compare as sc

_GB = 1 << 30
_VERSION = "0.1.0"


def _write(tmp: Path, table: pa.Table, name: str) -> str:
    path = tmp / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def _config(
    tmp: Path,
    parent: pa.Table,
    child: pa.Table,
    *,
    parent_columns: list[dict[str, Any]],
    child_columns: list[dict[str, Any]],
    key_strategy: str = "hash",
    orphan_policy: str = "preserve",
) -> dict[str, Any]:
    def key(name: str) -> dict[str, Any]:
        spec: dict[str, Any] = {"name": name, "strategy": key_strategy}
        if key_strategy == "hash":
            spec["namespace"] = "ns"
        return spec

    return {
        "version": 1,
        "global_settings": {"job_name": "gap6", "seed": 11},
        "sources": {
            "parent": {"type": "file", "path": _write(tmp, parent, "parent"), "format": "parquet"},
            "child": {"type": "file", "path": _write(tmp, child, "child"), "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {"name": "parent", "columns": [key("id"), *parent_columns]},
            {"name": "child", "columns": [key("parent_id"), *child_columns]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": orphan_policy,
                "namespace": "ns",
            }
        ],
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _run(config: dict[str, Any], sources: dict[str, pa.Table] | None = None, **kwargs: Any) -> Any:
    resident = _sources(config) if sources is None else sources
    return run_pipeline(config, resident, engine_version=_VERSION, **kwargs)


def _mode(result: Any) -> tuple[str, str]:
    execution = result.quality_metrics["execution"]
    return execution["execution_mode"], execution["route_reason"]


def _identical(a: Any, b: Any) -> bool:
    return all(a.outputs[t].equals(b.outputs[t], check_metadata=True) for t in a.outputs)


# --------------------------------------------------------------------------
# T5a: diverted to SEQUENTIAL
# --------------------------------------------------------------------------

_N = 24


def _payload_tables() -> tuple[pa.Table, pa.Table]:
    names = ["Zoë", "Ana", "Bo", "Éva", "Li", "Mo", "Nia", "Ola"]
    parent = pa.table(
        {
            "id": pa.array([f"p{i:03d}" for i in range(_N)]),
            "name": pa.array([names[i % len(names)] for i in range(_N)]),
            "region": pa.array([["north", "south", "east"][i % 3] for i in range(_N)]),
            "age": pa.array([str(20 + i * 3) for i in range(_N)]),
            "note": pa.array([f"free text {i}" for i in range(_N)]),
            "code": pa.array([f"CODE{i:04d}" for i in range(_N)]),
            "email": pa.array([f"user{i}@example.com" for i in range(_N)]),
            "joined": pa.array([f"2020-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(_N)]),
        }
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i:03d}" for i in range(_N * 2)]),
            "parent_id": pa.array(
                [f"p{i % _N:03d}" if i % 7 else f"orphan{i}" for i in range(_N * 2)]
            ),
            "tag": pa.array([f"tag-{i}" for i in range(_N * 2)]),
        }
    )
    return parent, child


_PARENT_PAYLOAD = [
    {"name": "name", "strategy": "hash", "namespace": "nm"},
    {
        "name": "region",
        "strategy": "categorical",
        "namespace": "rg",
        "deterministic": True,
        "provider_config": {"categories": ["north", "south", "east", "west"]},
    },
    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
    {"name": "note", "strategy": "redact"},
    {"name": "code", "strategy": "truncate", "provider_config": {"length": 4}},
    {
        "name": "email",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "em",
    },
    {
        "name": "joined",
        "strategy": "date_shift",
        "namespace": "js",
        "provider_config": {"min_days": -5, "max_days": 5, "date_format": "%Y-%m-%d"},
    },
]
_CHILD_PAYLOAD = [{"name": "tag", "strategy": "passthrough"}]


def _seq_config(tmp_path: Path) -> dict[str, Any]:
    parent, child = _payload_tables()
    return _config(
        tmp_path,
        parent,
        child,
        parent_columns=_PARENT_PAYLOAD,
        child_columns=_CHILD_PAYLOAD,
    )


class TestT5aSequentialEnvelope:
    def test_fitting_job_is_diverted_to_sequential(self, tmp_path: Path) -> None:
        got = _run(_seq_config(tmp_path), out_of_core_budget_bytes=_GB)
        assert _mode(got) == ("sequential", "pure_mask_fk")

    def test_output_equals_the_full_frame_oracle(self, tmp_path: Path) -> None:
        config = _seq_config(tmp_path)
        seq = _run(config, out_of_core_budget_bytes=_GB)
        oracle = _run(config, execution_mode="full_frame")
        assert _mode(oracle) == ("full_frame", "override_full_frame")
        assert _mode(seq) == ("sequential", "pure_mask_fk")
        for table in oracle.outputs:
            assert seq.outputs[table].equals(oracle.outputs[table]), table

    def test_referential_integrity_is_preserved(self, tmp_path: Path) -> None:
        config = _seq_config(tmp_path)
        seq = _run(config, out_of_core_budget_bytes=_GB)
        parent_ids = set(seq.outputs["parent"].column("id").to_pylist())
        source_parent_ids = set(_sources(config)["parent"].column("id").to_pylist())
        child_in = _sources(config)["child"].column("parent_id").to_pylist()
        child_out = seq.outputs["child"].column("parent_id").to_pylist()
        matched = [
            out for src, out in zip(child_in, child_out, strict=True) if src in source_parent_ids
        ]
        assert matched
        assert all(out in parent_ids for out in matched)

    def test_a_real_difference_fails_the_comparison(self, tmp_path: Path) -> None:
        # Negative control: the same comparison must reject output that differs from the
        # oracle in a single cell, so the equality above is not vacuous.
        config = _seq_config(tmp_path)
        seq = _run(config, out_of_core_budget_bytes=_GB)
        oracle = _run(config, execution_mode="full_frame")
        mutated = seq.outputs["parent"]
        names = mutated.column("name").to_pylist()
        names[3] = names[3] + "x"
        mutated = mutated.set_column(
            mutated.schema.get_field_index("name"), "name", pa.array(names, type=pa.string())
        )
        assert not mutated.equals(oracle.outputs["parent"])


# --------------------------------------------------------------------------
# T5b: diverted to OUT-OF-CORE
# --------------------------------------------------------------------------

_OOC_PARENT = [
    {"name": "name", "strategy": "hash", "namespace": "nm"},
    {"name": "note", "strategy": "redact"},
    {"name": "code", "strategy": "truncate", "provider_config": {"length": 4}},
]


def _ooc_tables(
    *, child_rows: int = 6, null_fk: bool = False, boundary_dates: bool = False
) -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(4)]),
            "name": pa.array(["a", "b", "c", "d"]),
            "note": pa.array(["w", "x", "y", "z"]),
            "code": pa.array(["AAAA1", "BBBB2", "CCCC3", "DDDD4"]),
        }
    )
    refs = [None] * child_rows if null_fk else [f"p{i % 4}" for i in range(child_rows)]
    columns: dict[str, pa.Array] = {
        "parent_id": pa.array(refs, type=pa.string()),
        "tag": pa.array([f"t{i}" for i in range(child_rows)], type=pa.string()),
    }
    if boundary_dates:
        columns["when"] = pa.array(
            [dt.date(1970, 1, 1), dt.date(9999, 12, 31), dt.date(1900, 1, 1)] * (child_rows // 3),
            type=pa.date32(),
        )
    return parent, pa.table(columns)


def _ooc_config(tmp_path: Path, **shape: Any) -> dict[str, Any]:
    parent, child = _ooc_tables(**shape)
    child_columns: list[dict[str, Any]] = [{"name": "tag", "strategy": "passthrough"}]
    if "when" in child.column_names:
        child_columns.append({"name": "when", "strategy": "passthrough"})
    return _config(tmp_path, parent, child, parent_columns=_OOC_PARENT, child_columns=child_columns)


# Strict dimensions on which the out-of-core output differs from the oracle on the
# success shapes, pinned as literals from a probe (tests/parity/SEMANTIC_DIFFERENCES.md,
# "Phase D / D0 strict baseline"): the oracle keeps pandas schema metadata, OOC does not.
_OOC_DIFFERS = {"field_metadata"}


def _disagreeing(oracle: pa.Table, ooc: pa.Table) -> set[str]:
    report = sc.compare_strict(oracle, ooc)
    return {dim for dim, agrees in report.items() if not agrees}


class TestT5bOutOfCoreEnvelope:
    @pytest.mark.parametrize(
        "shape",
        [{}, {"boundary_dates": True}],
        ids=["plain", "boundary_dates"],
    )
    def test_output_matches_the_oracle_up_to_the_documented_differences(
        self, tmp_path: Path, shape: dict[str, Any]
    ) -> None:
        config = _ooc_config(tmp_path, **shape)
        ooc = _run(config, out_of_core_budget_bytes=_GB)
        oracle = _run(config, execution_mode="full_frame")
        assert _mode(ooc) == ("out_of_core", "byte_estimate_bounded_out_of_core")
        for table in oracle.outputs:
            assert _disagreeing(oracle.outputs[table], ooc.outputs[table]) <= _OOC_DIFFERS, table

    @pytest.mark.parametrize(
        "shape",
        [{}, {"child_rows": 0}, {"null_fk": True}, {"boundary_dates": True}],
        ids=["plain", "empty_child", "all_null_fk", "boundary_dates"],
    )
    def test_output_equals_the_forced_out_of_core_route(
        self, tmp_path: Path, shape: dict[str, Any]
    ) -> None:
        # The diverted job runs the same route an operator would force, so its output
        # must be exactly that route's own output.
        config = _ooc_config(tmp_path, **shape)
        auto = _run(config, out_of_core_budget_bytes=_GB)
        forced = _run(config, execution_mode="out_of_core")
        assert _mode(auto)[0] == "out_of_core"
        assert _mode(forced) == ("out_of_core", "override_out_of_core")
        assert _identical(auto, forced)

    @pytest.mark.parametrize(
        "shape",
        [{"child_rows": 0}, {"null_fk": True}],
        ids=["empty_child", "all_null_fk"],
    )
    def test_empty_and_all_null_children_complete_with_equal_values(
        self, tmp_path: Path, shape: dict[str, Any]
    ) -> None:
        config = _ooc_config(tmp_path, **shape)
        ooc = _run(config, out_of_core_budget_bytes=_GB)
        oracle = _run(config, execution_mode="full_frame")
        assert ooc.outputs["child"].num_rows == oracle.outputs["child"].num_rows
        assert (
            ooc.outputs["child"].column("parent_id").null_count
            == oracle.outputs["child"].column("parent_id").null_count
        )
        assert ooc.outputs["parent"].column("id").to_pylist() == (
            oracle.outputs["parent"].column("id").to_pylist()
        )

    def test_referential_integrity_is_preserved(self, tmp_path: Path) -> None:
        config = _ooc_config(tmp_path)
        ooc = _run(config, out_of_core_budget_bytes=_GB)
        parent_ids = set(ooc.outputs["parent"].column("id").to_pylist())
        assert set(ooc.outputs["child"].column("parent_id").to_pylist()) <= parent_ids

    def test_a_real_difference_fails_the_strict_comparison(self, tmp_path: Path) -> None:
        # Negative control: the strict view must name a dimension beyond the documented
        # one when a cell really differs.
        config = _ooc_config(tmp_path)
        ooc = _run(config, out_of_core_budget_bytes=_GB)
        oracle = _run(config, execution_mode="full_frame")
        mutated = ooc.outputs["parent"]
        notes = mutated.column("note").to_pylist()
        notes[0] = "changed"
        mutated = mutated.set_column(
            mutated.schema.get_field_index("note"), "note", pa.array(notes, type=pa.string())
        )
        assert not _disagreeing(oracle.outputs["parent"], mutated) <= _OOC_DIFFERS


# --------------------------------------------------------------------------
# T5c: no new failing job
# --------------------------------------------------------------------------


def _key_classes() -> list[tuple[str, pa.Array, pa.Array]]:
    utc = pa.timestamp("us", tz="UTC")
    t = [dt.datetime(2020, 1, d, tzinfo=dt.timezone.utc) for d in range(1, 5)]
    return [
        (
            "decimal",
            pa.array([Decimal(i) for i in range(4)], pa.decimal128(18, 0)),
            pa.array([Decimal(i % 4) for i in range(8)], pa.decimal128(18, 0)),
        ),
        (
            "uint64",
            pa.array(list(range(4)), pa.uint64()),
            pa.array([i % 4 for i in range(8)], pa.uint64()),
        ),
        ("tz_timestamp", pa.array(t, utc), pa.array([t[i % 4] for i in range(8)], utc)),
        (
            "date64",
            pa.array([dt.date(2020, 1, 1 + i) for i in range(4)], pa.date64()),
            pa.array([dt.date(2020, 1, 1 + i % 4) for i in range(8)], pa.date64()),
        ),
        (
            "time64",
            pa.array([dt.time(1 + i) for i in range(4)], pa.time64("us")),
            pa.array([dt.time(1 + i % 4) for i in range(8)], pa.time64("us")),
        ),
        (
            "duration",
            pa.array(list(range(4)), pa.duration("us")),
            pa.array([i % 4 for i in range(8)], pa.duration("us")),
        ),
        (
            "fixed_size_binary",
            pa.array([bytes([65 + i, 66]) for i in range(4)], pa.binary(2)),
            pa.array([bytes([65 + i % 4, 66]) for i in range(8)], pa.binary(2)),
        ),
        (
            "dictionary",
            pa.array([f"k{i}" for i in range(4)]).dictionary_encode(),
            pa.array([f"k{i % 4}" for i in range(8)]).dictionary_encode(),
        ),
        (
            "string_parent_binary_child",
            pa.array([f"k{i}" for i in range(4)]),
            pa.array([f"k{i % 4}".encode() for i in range(8)], pa.binary()),
        ),
    ]


def _key_job(
    tmp_path: Path, parent_keys: pa.Array, child_keys: pa.Array
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    # The tables are passed to the run directly: Parquet would rewrite some of these key
    # types (date64 comes back as date32), and the type is the point of each case.
    parent = pa.table({"id": parent_keys, "note": pa.array(["n"] * len(parent_keys))})
    child = pa.table({"parent_id": child_keys, "tag": pa.array(["t"] * len(child_keys))})
    config = _config(
        tmp_path,
        parent,
        child,
        parent_columns=[{"name": "note", "strategy": "redact"}],
        child_columns=[{"name": "tag", "strategy": "passthrough"}],
        key_strategy="passthrough",
    )
    return config, {"parent": parent, "child": child}


_CLASSES = _key_classes()
_CLASS_IDS = [name for name, _, _ in _CLASSES]


@pytest.fixture
def probe_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The static estimate is inconclusive and the probe confirms the fit."""
    monkeypatch.setattr(signals, "resolve_full_frame_fits_estimate", lambda *a, **k: False)
    monkeypatch.setattr(signals, "resolve_probe_recovery", lambda *a, **k: True)


class TestT5cNoNewFailingJob:
    @pytest.mark.parametrize(("name", "parent_keys", "child_keys"), _CLASSES, ids=_CLASS_IDS)
    def test_the_shape_really_is_an_out_of_core_reject(
        self, tmp_path: Path, name: str, parent_keys: pa.Array, child_keys: pa.Array
    ) -> None:
        # The premise: the oracle completes it, forced out-of-core rejects it.
        config, sources = _key_job(tmp_path, parent_keys, child_keys)
        _run(config, sources, execution_mode="full_frame")
        with pytest.raises(ExecutionError) as exc:
            _run(config, sources, execution_mode="out_of_core")
        assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"

    @pytest.mark.parametrize(("name", "parent_keys", "child_keys"), _CLASSES, ids=_CLASS_IDS)
    def test_fitting_job_completes_on_the_estimate_path(
        self, tmp_path: Path, name: str, parent_keys: pa.Array, child_keys: pa.Array
    ) -> None:
        config, sources = _key_job(tmp_path, parent_keys, child_keys)
        if name == "dictionary":
            # The byte estimator cannot price a dictionary column, so no fit is confirmed
            # and the job takes the bounded route it always took (out-of-core, which
            # rejects it). This is the existing behavior, not one the routing change
            # introduces; the probe path below is where a dictionary job can fit. If the
            # estimator learns to price dictionaries this branch should be removed.
            with pytest.raises(ExecutionError) as exc:
                _run(config, sources, out_of_core_budget_bytes=_GB)
            assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"
            return
        oracle = _run(config, sources, execution_mode="full_frame")
        got = _run(config, sources, out_of_core_budget_bytes=_GB)
        assert _mode(got) == ("sequential", "pure_mask_fk")
        for table in oracle.outputs:
            assert got.outputs[table].equals(oracle.outputs[table]), table

    @pytest.mark.parametrize(("name", "parent_keys", "child_keys"), _CLASSES, ids=_CLASS_IDS)
    def test_fitting_job_completes_on_the_probe_path(
        self,
        tmp_path: Path,
        probe_fit: None,
        name: str,
        parent_keys: pa.Array,
        child_keys: pa.Array,
    ) -> None:
        config, sources = _key_job(tmp_path, parent_keys, child_keys)
        oracle = _run(config, sources, execution_mode="full_frame")
        got = _run(config, sources, out_of_core_budget_bytes=_GB)
        assert _mode(got) == ("sequential", "pure_mask_fk")
        for table in oracle.outputs:
            assert got.outputs[table].equals(oracle.outputs[table]), table

    def test_a_supported_key_type_is_still_diverted_to_out_of_core(self, tmp_path: Path) -> None:
        keys = pa.array(list(range(4)), pa.int64())
        config, sources = _key_job(tmp_path, keys, pa.array([i % 4 for i in range(8)], pa.int64()))
        assert _mode(_run(config, sources, out_of_core_budget_bytes=_GB)) == (
            "out_of_core",
            "byte_estimate_bounded_out_of_core",
        )
