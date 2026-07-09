"""SC7b: the SC2 size gates fire on the LAZY (`sources={}`, `source_loader`)
input path, keyed off the SC7a bounded-profiling `row_count` instead of the
resident Arrow metadata that is absent on that path.

This closes the consultant-2026-07-09 F2 hole: `largest_mask_table_rows()`
returned None whenever `sources={}`, so `decide_execution_route`'s
reject-before-read / out-of-core reroute never fired for exactly the bounded
input shape that most needs it. With the profile's cheap metadata count wired
in, a too-big lazy FK job is now rejected (Parquet: exact count) or diverted to
out-of-core BEFORE `source_loader` is ever invoked.

The size thresholds are `run_pipeline` kwargs; the tests lower them so a small
fixture exercises the same routing a multi-million-row job would, without the
data.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any
from unittest import mock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import run_pipeline

_N = 40


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _write_parquet(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _write_csv(tmp_path: Path, df: pd.DataFrame, name: str) -> str:
    p = tmp_path / f"{name}.csv"
    df.to_csv(p, index=False)
    return str(p)


def _parent_child_tables() -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(_N)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    return parent, child


def _fk_relationships() -> list[dict[str, Any]]:
    return [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["parent_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns",
        }
    ]


def _ooc_eligible_config(tmp_path: Path, src_fmt: str) -> dict[str, Any]:
    """A pure-mask FK job whose every strategy is in the out-of-core supported
    set (hash keys + a redact payload), so `check_out_of_core_compatibility`
    ADMITS it -- the shape that auto-routes to out-of-core once it is large."""
    parent, child = _parent_child_tables()
    if src_fmt == "parquet":
        parent_src = _write_parquet(tmp_path, parent, "parent")
        child_src = _write_parquet(tmp_path, child, "child")
    else:
        parent_src = _write_csv(tmp_path, parent.to_pandas(), "parent")
        child_src = _write_csv(tmp_path, child.to_pandas(), "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "sc7b-ooc-fk", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": src_fmt},
            "child": {"type": "file", "path": child_src, "format": src_fmt},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [_hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": _fk_relationships(),
    }


def _ineligible_config(tmp_path: Path, src_fmt: str) -> dict[str, Any]:
    """A generate+mask FK job: the compat gate admits the FK structure (hash
    keys), but the job is NOT sequential-eligible (a generate table) and
    out-of-core cannot generate -- the reject case, no bounded route applies."""
    parent, child = _parent_child_tables()
    if src_fmt == "parquet":
        parent_src = _write_parquet(tmp_path, parent, "parent")
        child_src = _write_parquet(tmp_path, child, "child")
    else:
        parent_src = _write_csv(tmp_path, parent.to_pandas(), "parent")
        child_src = _write_csv(tmp_path, child.to_pandas(), "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "sc7b-reject-genmask", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": src_fmt},
            "child": {"type": "file", "path": child_src, "format": src_fmt},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
            "extra": {
                "type": "file",
                "path": str(tmp_path / "extra.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [_hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
            {
                "name": "extra",
                "row_count": 3,
                "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
            },
        ],
        "relationships": _fk_relationships(),
    }


def _file_loader(config: dict[str, Any]) -> Any:
    """A real lazy loader that reads the on-disk source for `table_name`."""

    def loader(table_name: str) -> pa.Table:
        spec = config["sources"][table_name]
        if spec["format"] == "parquet":
            return pq.read_table(spec["path"])
        return pa.Table.from_pandas(pd.read_csv(spec["path"]), preserve_index=False)

    return loader


# ---------------------------------------------------------------------------
# AC 1: lazy Parquet, >= reject threshold, NOT OOC-eligible -> reject BEFORE
#       source_loader is ever called.
# ---------------------------------------------------------------------------


def test_lazy_parquet_ineligible_rejects_before_loader_is_called(tmp_path: Path) -> None:
    config = _ineligible_config(tmp_path, "parquet")
    # A spy that RAISES if invoked: proves the reject fired before any read.
    loader = mock.Mock(side_effect=AssertionError("source_loader must not be called"))
    with pytest.raises(ExecutionError) as exc:
        run_pipeline(
            config,
            sources={},  # lazy path: no resident Arrow sources
            engine_version="0.1.0",
            source_loader=loader,
            full_frame_reject_rows=10,  # 40-row fixture stands in for a huge FK job
        )
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"
    assert f"{_N:,}" in exc.value.message
    assert "not sequential-eligible" in exc.value.message
    loader.assert_not_called()


# ---------------------------------------------------------------------------
# AC 2: same lazy shape but OOC-eligible -> reroute to out_of_core (no reject).
#       AC 5: routing decision + reason land in quality_metrics["execution"].
# ---------------------------------------------------------------------------


def test_lazy_parquet_ooc_eligible_reroutes_to_out_of_core(tmp_path: Path) -> None:
    config = _ooc_eligible_config(tmp_path, "parquet")
    result = run_pipeline(
        config,
        sources={},  # lazy path
        engine_version="0.1.0",
        source_loader=_file_loader(config),
        out_of_core_threshold_rows=10,  # 40-row fixture is "large"
        full_frame_reject_rows=10,  # would reject if it were full-frame-bound
    )
    # AC 5: the decision + reason are stamped on the manifest.
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert result.quality_metrics["execution"]["route_reason"] == "out_of_core_large_fk"
    assert set(result.outputs) == {"parent", "child"}


# ---------------------------------------------------------------------------
# AC 3: lazy CSV at the same size -> distinct estimated code + Parquet guidance.
# ---------------------------------------------------------------------------


def test_lazy_csv_ineligible_rejects_with_estimated_code(tmp_path: Path) -> None:
    config = _ineligible_config(tmp_path, "csv")
    loader = mock.Mock(side_effect=AssertionError("source_loader must not be called"))
    with pytest.raises(ExecutionError) as exc:
        run_pipeline(
            config,
            sources={},
            engine_version="0.1.0",
            source_loader=loader,
            full_frame_reject_rows=5,  # CSV byte-estimate (~40) lands above this
        )
    assert exc.value.code == "fk_full_frame_oom_risk_rejected_estimated"
    assert "CSV size estimate" in exc.value.message
    assert "Parquet" in exc.value.message
    assert "execution_mode" in exc.value.message
    loader.assert_not_called()


def test_lazy_csv_ooc_eligible_still_reroutes_on_estimate(tmp_path: Path) -> None:
    # An estimated count is good enough to PREFER the bounded route: an
    # OOC-eligible CSV job reroutes rather than rejecting.
    config = _ooc_eligible_config(tmp_path, "csv")
    result = run_pipeline(
        config,
        sources={},
        engine_version="0.1.0",
        source_loader=_file_loader(config),
        out_of_core_threshold_rows=10,
        full_frame_reject_rows=10,
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert result.quality_metrics["execution"]["route_reason"] == "out_of_core_large_fk"


# ---------------------------------------------------------------------------
# AC 4: the resident-sources path still agrees with the profile count -- the
#       exact-metadata cross-check passes (no mismatch warning) and routing is
#       unchanged from pre-SC7b resident-path behavior.
# ---------------------------------------------------------------------------


def test_resident_path_agrees_with_profile_count_no_warning(tmp_path: Path) -> None:
    config = _ooc_eligible_config(tmp_path, "parquet")
    sources = {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_pipeline(
            config,
            sources,  # resident path: exact Arrow count == exact Parquet profile count
            engine_version="0.1.0",
            out_of_core_threshold_rows=10,
        )
    # No cross-check mismatch surfaced: resident 40 == profile 40 (both exact).
    assert not [w for w in caught if "row count disagrees" in str(w.message)], (
        "resident vs profile exact counts must agree"
    )
    # Routing is unchanged from pre-SC7b resident behavior (large -> out_of_core).
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"


def test_resident_path_below_threshold_stays_sequential(tmp_path: Path) -> None:
    # The resident-path size gate still keys off the resident count exactly as
    # before: below the out-of-core threshold it stays sequential.
    config = _ooc_eligible_config(tmp_path, "parquet")
    sources = {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}
    result = run_pipeline(config, sources, engine_version="0.1.0", out_of_core_threshold_rows=1_000)
    assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
