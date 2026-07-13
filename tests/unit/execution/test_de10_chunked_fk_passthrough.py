"""DE-10 remediation, MEDIUM #4: chunked/self-masking `passthrough` FK gap.

`run_mask_pipeline_chunked` (`_chunked.py`) always threads an EMPTY
`RelationshipGraph` into `PandasExecutionAdapter.run()` per chunk (self-
masking has no parent-map join), so `_fk_keys.to_pandas_fk_safe`'s ingestion
protection -- keyed off that runtime graph via `fk_columns_for_table` --
protects NOTHING on this route. Every other chunk-safe strategy (hash, fpe,
redact, truncate, text_redact, date_shift, bucketize) re-DERIVES its output
value from the source rather than preserving it, so this unprotected
ingestion never reaches the output byte-for-byte for them -- but
`passthrough` (identity) IS chunk-safe-admitted and DOES preserve the raw
key verbatim, so a null-bearing `passthrough` FK column carrying a value
beyond `2**53` silently rounded through this route's unprotected ingestion,
exactly like the pre-DE-10 full-frame/sequential bug (see
`test_de10_fk_lossless_typing.py`).

Closed with a TARGETED runtime guard
(`_chunked_fk.fk_passthrough_columns_for_table` +
`_chunked_fk.reject_lossy_chunked_fk_passthrough`), not a blanket
null-bearing-int reject: only a `passthrough` FK column that is BOTH
null-bearing AND carries a value beyond `2**53` fails closed, so a
legitimate small-int passthrough FK job is unaffected.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._chunked_fk import fk_passthrough_columns_for_table
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE

_ENGINE = "test-de10-chunked-fk-passthrough"

# Same boundary as test_de10_fk_lossless_typing.py: the largest magnitude
# every integer up to and including it can round-trip through float64 exactly.
_BIG_KEY = 9007199254740993  # 2**53 + 1


def _passthrough_fk_config(
    *, parent_dtype: str | None = "int64", child_dtype: str | None = "int64"
) -> dict:
    """Minimal customers(id) -> orders(customer_id) FK config, both columns
    `passthrough`, REMAP orphan policy -- the shape MEDIUM #4 admits onto the
    chunked route (gate conditions (a)-(d) in `_chunked_fk.gate_fk_child_edges`
    all hold: passthrough is chunk-safe, both sides declare it, passthrough is
    namespace-agnostic so no namespace sub-checks apply, orphan_policy=remap)."""
    parent_col: dict = {"name": "id", "strategy": "passthrough"}
    if parent_dtype is not None:
        parent_col["dtype"] = parent_dtype
    child_col: dict = {"name": "customer_id", "strategy": "passthrough"}
    if child_dtype is not None:
        child_col["dtype"] = child_dtype
    return {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [parent_col]},
            {"name": "orders", "columns": [child_col]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Config-parsing regression coverage: `fk_passthrough_columns_for_table` reads
# the NESTED relationships schema (`parent`/`children`), not flat
# `parent_table`/`child_table` keys -- caught in review before landing.
# ---------------------------------------------------------------------------


def test_fk_passthrough_columns_for_table_parses_nested_relationships_schema() -> None:
    """`relationships` entries nest one `parent` + a `children` list (not
    flat `parent_table`/`child_table` keys); both the child role AND the
    parent role must parse correctly (BLOCKER #2, DE-10 reland: the parent
    assertion here used to read `== set()` before the parent-column fix --
    see `test_fk_passthrough_columns_for_table_includes_parent_key_column`
    for the dedicated regression coverage)."""
    config = _passthrough_fk_config()
    assert fk_passthrough_columns_for_table(config, "orders") == {"customer_id"}
    assert fk_passthrough_columns_for_table(config, "customers") == {"id"}


def test_fk_passthrough_columns_for_table_includes_parent_key_column() -> None:
    """BLOCKER #2 (DE-10 reland, 2026-07-13): a table playing the PARENT role
    in a relationship must ALSO be protected -- the reverted cut collected
    only `relationships[].children[]`, leaving a chunked PARENT table's own
    passthrough key column completely unguarded even though
    `run_mask_pipeline_chunked` processes it through the exact same
    unprotected `table.to_pandas()` ingestion as any other chunked table."""
    config = _passthrough_fk_config()
    assert fk_passthrough_columns_for_table(config, "customers") == {"id"}
    assert fk_passthrough_columns_for_table(config, "orders") == {"customer_id"}


def test_children_only_collector_would_miss_the_parent_column() -> None:
    """Pins the PRE-FIX mechanism (BLOCKER #2): a children-only collector,
    exactly like the reverted cut, returns an EMPTY set for the parent table
    even though it has its own passthrough FK key column -- this is the gap
    that let a chunked-route parent's big-int passthrough key silently
    round (the CHANGELOG's prior "now CLOSED" claim for MEDIUM #4 was false
    for this side; see CHANGELOG.md's 2026-07-13 reland correction)."""
    config = _passthrough_fk_config()

    def _children_only_pre_fix(cfg: dict, table: str) -> set[str]:
        child_columns: set[str] = set()
        for rel_entry in cfg.get("relationships") or []:
            for child_info in rel_entry.get("children") or []:
                if child_info.get("table") == table:
                    child_columns.update(child_info.get("columns") or [])
        return child_columns

    assert _children_only_pre_fix(config, "customers") == set()  # the BLOCKER #2 gap
    assert _children_only_pre_fix(config, "orders") == {"customer_id"}  # child side was fine


def test_fk_passthrough_columns_for_table_excludes_non_passthrough_strategy() -> None:
    """Only the passthrough gap set matters here -- hash/fpe/etc. re-derive
    their output, so an int+null column masked under them is a DIFFERENT
    (pre-existing, unrelated) correctness question owned by
    `execution._guards.reject_null_bearing_int`, not this guard."""
    config = _passthrough_fk_config()
    config["tables"][1]["columns"][0] = {
        "name": "customer_id",
        "strategy": "hash",
        "namespace": "ns",
        "dtype": "int64",
    }
    config["tables"][0]["columns"][0] = {
        "name": "id",
        "strategy": "hash",
        "namespace": "ns",
        "dtype": "int64",
    }
    assert fk_passthrough_columns_for_table(config, "orders") == set()


# ---------------------------------------------------------------------------
# Pre-fix mechanism: unprotected ingestion still rounds a big-int+null column
# (the same float64-on-null fallback the DE-10 ingestion fix routes around
# for the full-frame/sequential routes, just unreachable there because those
# routes DO thread the real RelationshipGraph).
# ---------------------------------------------------------------------------


def test_bare_ingestion_without_fk_protection_still_rounds_a_big_int_null_column() -> None:
    """This is NOT a regression test for the engine -- it pins the underlying
    pyarrow/pandas default `to_pandas_fk_safe` exists to route around, which
    is exactly what the chunked route's per-chunk `table.to_pandas()` call
    falls back to for a column `fk_columns_for_table` does not protect
    (empty `RelationshipGraph` -> empty protected-column set on this route)."""
    tbl = pa.table({"customer_id": pa.array([1, None, _BIG_KEY], type=pa.int64())})
    df = tbl.to_pandas()
    assert df["customer_id"].dtype == "float64"
    assert df["customer_id"].tolist()[2] != _BIG_KEY  # rounded


# ---------------------------------------------------------------------------
# Post-fix: the targeted guard fails closed with the shared coded error.
# ---------------------------------------------------------------------------


def test_chunked_passthrough_null_bearing_big_int_fk_raises_coded_error() -> None:
    config = _passthrough_fk_config()
    chunk = pa.table({"customer_id": pa.array([1, None, _BIG_KEY], type=pa.int64())})

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


def test_chunked_passthrough_null_bearing_big_int_fk_raises_on_second_chunk_too() -> None:
    """The guard runs PER CHUNK (chunks stream lazily); a big key arriving in
    a later chunk must still fail closed, not just the first."""
    config = _passthrough_fk_config()
    chunks = [
        pa.table({"customer_id": pa.array([1, 2, None], type=pa.int64())}),
        pa.table({"customer_id": pa.array([3, None, _BIG_KEY], type=pa.int64())}),
    ]

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, chunks, table="orders", engine_version=_ENGINE))
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


# ---------------------------------------------------------------------------
# Targeted, not blanket: a legitimate small-int (or null-free big-int, or
# non-FK) passthrough column must NOT be rejected.
# ---------------------------------------------------------------------------


def test_chunked_passthrough_small_int_null_bearing_fk_still_admitted() -> None:
    """The guard is magnitude-aware: a null-bearing passthrough FK column
    whose every value stays within exact float64 precision must NOT be
    rejected -- rejecting it too would break legitimate small-int passthrough
    FK jobs for no correctness reason."""
    config = _passthrough_fk_config()
    chunk = pa.table({"customer_id": pa.array([1, None, 42], type=pa.int64())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == [1, None, 42]


def test_chunked_passthrough_big_int_without_null_still_admitted() -> None:
    """int64 has no float64 fallback without a null in the column; a
    null-free big-int passthrough FK column was never at risk and must stay
    admitted."""
    config = _passthrough_fk_config()
    chunk = pa.table({"customer_id": pa.array([1, _BIG_KEY], type=pa.int64())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == [1, _BIG_KEY]


# ---------------------------------------------------------------------------
# BLOCKER #2 route-level coverage: the PARENT table, not just the child, must
# fail closed on a null-bearing big-int passthrough key.
# ---------------------------------------------------------------------------


def test_chunked_passthrough_parent_null_bearing_big_int_fk_raises_coded_error() -> None:
    config = _passthrough_fk_config()
    chunk = pa.table({"id": pa.array([1, None, _BIG_KEY], type=pa.int64())})

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="customers", engine_version=_ENGINE))
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE


def test_chunked_passthrough_parent_big_int_without_null_still_admitted() -> None:
    config = _passthrough_fk_config()
    chunk = pa.table({"id": pa.array([1, _BIG_KEY], type=pa.int64())})

    out = list(
        run_mask_pipeline_chunked(config, [chunk], table="customers", engine_version=_ENGINE)
    )
    vals = pa.concat_tables(out).column("id").to_pylist()
    assert vals == [1, _BIG_KEY]


def test_chunked_passthrough_parent_small_int_null_bearing_fk_still_admitted() -> None:
    config = _passthrough_fk_config()
    chunk = pa.table({"id": pa.array([1, None, 42], type=pa.int64())})

    out = list(
        run_mask_pipeline_chunked(config, [chunk], table="customers", engine_version=_ENGINE)
    )
    vals = pa.concat_tables(out).column("id").to_pylist()
    assert vals == [1, None, 42]


# ---------------------------------------------------------------------------
# MEDIUM: the guard must be gated on whether THIS adapter will actually
# touch pandas ingestion for this table -- a fully-native-Polars chunked run
# preserves nullable int64 losslessly and never touches pandas, so applying
# the pandas-only guard there is a false-positive fail-closed reject.
# ---------------------------------------------------------------------------


def test_chunked_adapter_touches_pandas_ingestion_gates_correctly() -> None:
    from decoy_engine.execution._chunked_adapter_gate import (
        chunked_adapter_touches_pandas_ingestion,
    )
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.execution.polars import PolarsExecutionAdapter

    config = _passthrough_fk_config()
    assert (
        chunked_adapter_touches_pandas_ingestion(PandasExecutionAdapter(), config, "orders") is True
    )
    assert (
        chunked_adapter_touches_pandas_ingestion(PolarsExecutionAdapter(), config, "orders")
        is False
    )

    # A table with a non-polars-native strategy alongside `passthrough` falls
    # back to the pandas oracle INSIDE the polars adapter's own `run()`, so it
    # must still be treated as pandas-touching (skipping the guard there would
    # re-open the exact silent-rounding gap this MEDIUM closes, just for a
    # mixed-strategy table instead of an all-passthrough one). `code_set` is
    # deliberately NOT chunk-safe (so this exact config could never actually
    # reach `run_mask_pipeline_chunked` in production -- every CHUNK_SAFE_
    # STRATEGIES member happens to already be polars-native today, per
    # `POLARS_SCALAR_HANDLERS`); it is used here purely to exercise this
    # helper's own non-native branch in isolation, as defensive coverage for
    # if that overlap ever narrows.
    mixed_config = _passthrough_fk_config()
    mixed_config["tables"][1]["columns"].append(
        {"name": "note", "strategy": "code_set", "provider_config": {"code_set": "iso3166-1"}}
    )
    assert (
        chunked_adapter_touches_pandas_ingestion(PolarsExecutionAdapter(), mixed_config, "orders")
        is True
    )


def test_chunked_passthrough_polars_adapter_preserves_big_int_without_false_positive_reject() -> (
    None
):
    """A fully polars-native chunked run (only `passthrough` on this table,
    no FK edges threaded -- the chunked route always passes an empty
    `RelationshipGraph`) must NOT be rejected by the pandas-only guard: it
    preserves nullable int64 losslessly without ever touching pandas."""
    from decoy_engine.execution.polars import PolarsExecutionAdapter

    config = _passthrough_fk_config()
    chunk = pa.table({"customer_id": pa.array([1, None, _BIG_KEY], type=pa.int64())})

    out = list(
        run_mask_pipeline_chunked(
            config,
            [chunk],
            table="orders",
            engine_version=_ENGINE,
            adapter=PolarsExecutionAdapter(),
        )
    )
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == [1, None, _BIG_KEY]


# ---------------------------------------------------------------------------
# MEDIUM: the `dtype` field feeding `gate_fk_child_edges`'s condition (f) is
# now reachable through the VALIDATED config API, not just a raw dict.
# ---------------------------------------------------------------------------


def test_dtype_field_reachable_through_validated_pipeline_config() -> None:
    """Before this fix, `ColumnConfig`'s `extra='forbid'` had no `dtype`
    field declared, so `PipelineConfig.model_validate(...).model_dump()`
    (the production path per `run_mask_pipeline_chunked`'s own docstring:
    "config is the validated pipeline config dump") raised `extra_forbidden`
    for any config that set `dtype`, and hit the gate's "unprovable"
    rejection unconditionally for any that omitted it -- so only a
    hand-built raw dict (bypassing validation) could ever reach the gate's
    dtype-family check. This test proves the validated path now carries it
    through and the chunked hash FK edge (value-sensitive, so condition (f)
    applies) compiles."""
    from decoy_engine import PipelineConfig
    from decoy_engine.execution._chunked import check_chunked_compatibility

    raw_config = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "customers": {"type": "file", "format": "csv", "path": "customers.csv"},
            "orders": {"type": "file", "format": "csv", "path": "orders.csv"},
        },
        "targets": {
            "customers": {"type": "file", "format": "csv", "path": "customers-out.csv"},
            "orders": {"type": "file", "format": "csv", "path": "orders-out.csv"},
        },
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {"name": "id", "strategy": "hash", "namespace": "ns", "dtype": "int64"}
                ],
            },
            {
                "name": "orders",
                "columns": [
                    {
                        "name": "customer_id",
                        "strategy": "hash",
                        "namespace": "ns",
                        "dtype": "int64",
                    }
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }

    validated = PipelineConfig.model_validate(raw_config).model_dump()
    assert validated["tables"][0]["columns"][0]["dtype"] == "int64"

    # No PlanCompileError: the dtype-family check on condition (f) is
    # satisfied through the validated dump, not just a raw dict.
    check_chunked_compatibility(validated, table="orders")
