"""FK key dtype and pandas-ingest attribution errors carry no source value.

A resolved FK key is a real (preserved) or masked value. Every
`FK_KEY_DTYPE_UNSUPPORTED_CODE` raise in `execution/_fk_keys.py` names the table and column only:
no key, no range bound, no pandas/Arrow text in `str`, `repr`, `args`, the rendered traceback or
the cause/context chain. The first three cases run a normal `PandasExecutionAdapter` job (full
frame and sequential) whose categorical parent strategy resolves a key no lossless dtype holds.
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter, _fk_keys
from decoy_engine.execution._fk_keys import (
    FK_KEY_DTYPE_UNSUPPORTED_CODE,
    fk_nullable_int_array,
    to_pandas_fk_safe,
)
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

ABOVE_UINT64 = 2**64 + 7  # 18446744073709551623
BELOW_INT64 = -(2**63) - 9  # -9223372036854775817
NEGATIVE = -7777
UNSIGNED_ORPHAN = 2**63 + 5  # 9223372036854775813

LIBRARY_TEXT = ("out of bounds", "Python integer", "not in range", "OverflowError", "ArrowInvalid")
_DIGITS = re.compile(r"\d{3,}")

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_KEYS = SecretKeyProvider(secret=b"fk-value-free-mask-key-0123456789", key_version="v1")


def _seed(strategy: str, config: tuple[tuple[str, Any], ...] = ()) -> ColumnSeed:
    return ColumnSeed(
        namespace="fkvf",
        strategy=strategy,
        provider=None if strategy == "categorical" else strategy,
        backend_type="decoy_native" if strategy == "categorical" else "faker",
        backend_version="1",
        cardinality_mode="bijective" if strategy == "categorical" else "reuse",
        deterministic=True,
        provider_config=config,
        coherent_with=(),
    )


def _job(category: int) -> tuple[Any, RelationshipGraph]:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x31" * 8,
            per_table=(
                (
                    "parent",
                    TableSeed(
                        per_column=(("pk", _seed("categorical", (("categories", (category,)),))),),
                        per_group=(),
                    ),
                ),
                ("child", TableSeed(per_column=(("fk", _seed("passthrough")),), per_group=())),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="fkvf",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    return plan, RelationshipGraph(edges=(edge,), ordering=())


def _full_frame(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph) -> None:
    PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
        key_provider=_KEYS,
    )


def _sequential(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph) -> None:
    PandasExecutionAdapter().run_sequential(
        plan,
        sources.__getitem__,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
        key_provider=_KEYS,
    )


_ROUTES: dict[str, Callable[..., None]] = {"full_frame": _full_frame, "sequential": _sequential}


def _assert_value_free(
    exc: ExecutionError,
    needles: tuple[str, ...],
    *,
    code: str | None = FK_KEY_DTYPE_UNSUPPORTED_CODE,
    chained: bool = False,
) -> None:
    if code is not None:
        assert exc.code == code
    surface = [str(exc), repr(exc), repr(exc.args)]
    for form in surface:
        assert not _DIGITS.search(form), f"integer leaked in: {form!r}"
    if chained:
        # The cause is pinned elsewhere as the original conversion error (diagnosis): only the
        # public text is checked here.
        forms = surface
    else:
        # No cause; any context (a caller's own handler) is suppressed, so it never renders.
        assert exc.__cause__ is None
        assert exc.__context__ is None or exc.__suppress_context__
        forms = [*surface, "".join(traceback.format_exception(exc))]
    for form in forms:
        for needle in (*needles, *LIBRARY_TEXT):
            assert needle not in form, f"{needle!r} leaked in: {form[:400]!r}"


def _run(route: str, category: int, parent: pa.Array, child: pa.Array) -> ExecutionError:
    plan, graph = _job(category)
    sources = {"parent": pa.table({"pk": parent}), "child": pa.table({"fk": child})}
    with pytest.raises(ExecutionError) as caught:
        _ROUTES[route](plan, sources, graph)
    return caught.value


# Sequential fails earlier on these two (writing the parent's own categorical column back to
# Arrow), so only the full-frame route reaches the FK write-back for a key outside both ranges.
@pytest.mark.parametrize("route", ["full_frame"])
def test_categorical_key_above_uint64_is_value_free(route: str) -> None:
    exc = _run(
        route,
        ABOVE_UINT64,
        pa.array([1], type=pa.int64()),
        pa.array([1, None], type=pa.int64()),
    )
    _assert_value_free(exc, (str(ABOVE_UINT64), str(2**64 - 1)))
    assert str(exc).endswith("FK output column child.fk: a resolved key exceeds the UInt64 range.")


@pytest.mark.parametrize("route", ["full_frame"])
def test_categorical_key_below_int64_is_value_free(route: str) -> None:
    exc = _run(
        route,
        BELOW_INT64,
        pa.array([1], type=pa.int64()),
        pa.array([1, None], type=pa.int64()),
    )
    _assert_value_free(exc, (str(BELOW_INT64), str(-(2**63))))
    assert str(exc).endswith("FK output column child.fk: a resolved key is below the Int64 range.")


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_uint64_orphan_beside_negative_categorical_key_is_value_free(route: str) -> None:
    exc = _run(
        route,
        NEGATIVE,
        pa.array([1], type=pa.uint64()),
        pa.array([1, UNSIGNED_ORPHAN], type=pa.uint64()),
    )
    _assert_value_free(exc, (str(NEGATIVE), str(UNSIGNED_ORPHAN)))
    assert str(exc).endswith(
        "FK output column child.fk: a resolved key cannot share one lossless nullable UInt64 array."
    )


@pytest.mark.parametrize(
    ("values", "needles"),
    [
        ([ABOVE_UINT64, None], (str(ABOVE_UINT64),)),
        ([BELOW_INT64, None], (str(BELOW_INT64),)),
        ([UNSIGNED_ORPHAN, NEGATIVE], (str(UNSIGNED_ORPHAN), str(NEGATIVE))),
    ],
)
def test_write_back_helper_is_value_free(
    values: list[int | None], needles: tuple[str, ...]
) -> None:
    with pytest.raises(ExecutionError) as caught:
        fk_nullable_int_array(values, table="orders", column="customer_id")
    _assert_value_free(caught.value, needles)
    assert "FK output column orders.customer_id: a resolved key " in str(caught.value)


def test_ingestion_cast_failure_is_value_free(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unreachable from a well-formed table; a too-narrow mapper forces the backstop, whose
    # Arrow error quotes the cell ("Integer value 31337 not in range").
    monkeypatch.setattr(_fk_keys, "_exact_fk_types_mapper", lambda _t: pd.Int8Dtype())
    table = pa.table({"customer_id": pa.array([31337, None], type=pa.int64())})
    with pytest.raises(ExecutionError) as caught:
        to_pandas_fk_safe(table, {"customer_id"})
    _assert_value_free(caught.value, ("31337",))
    assert str(caught.value).endswith(
        "FK column 'customer_id' could not be ingested as a lossless nullable integer."
    )


# -- callers of `to_pandas_fk_safe` that attribute a pandas conversion failure ------------------
# Both re-convert one column with `to_pandas_fk_safe` and raise a coded error naming it; the
# pandas/Arrow error they used to append quotes the cell ("Value 3001 has non-zero nanoseconds").

UNALIGNED_NS = 45_296_123_456_789  # 12:34:56.123456789 as time64[ns]


def test_full_frame_unrepresentable_value_is_value_free(tmp_path: Any) -> None:
    from decoy_engine import run_pipeline
    from decoy_engine.config import PipelineConfig

    (tmp_path / "t.csv").write_text("tod,name\n")
    config = PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {
                "t": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "t.csv"),
                    "has_header": True,
                }
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "tod", "strategy": "passthrough"},
                        {"name": "name", "strategy": "hash", "namespace": "n"},
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        }
    ).model_dump()
    src = pa.table({"tod": pa.array([UNALIGNED_NS], pa.time64("ns")), "name": ["alice"]})
    with pytest.raises(ExecutionError) as caught:
        run_pipeline(config, sources={"t": src}, engine_version="fkvf", key_provider=_KEYS)
    exc = caught.value
    assert exc.code == "full_frame_value_unrepresentable"
    _assert_value_free(exc, (str(UNALIGNED_NS), "45296", "non-zero nanoseconds"), code=None)
    assert str(exc).startswith(
        "[full_frame_value_unrepresentable] table 't' column 'tod' holds a value pandas "
        "cannot represent. "
    )


def test_chunked_read_passthrough_unrepresentable_value_is_value_free() -> None:
    from decoy_engine import run_mask_chunked
    from tests.native._chunked_entry_support import (
        ENGINE_VERSION,
        FORCE_ORACLE_VALUE,
        TABLE,
        force_oracle,
        key_provider,
        make_config,
        redact,
    )

    forced = pa.array([FORCE_ORACLE_VALUE] * 3)
    s = pa.array(["a", "b", "c"])
    bad = pa.array([1000, None, 3001], pa.time64("ns"))
    chunks = [pa.table({"s": s, "x": bad, "c": forced})]
    config = make_config(
        [{**redact("s"), "when": "x != 'q'"}, force_oracle("c")],
        global_settings={"unconfigured_column_policy": "warn"},
    )
    with pytest.raises(ExecutionError) as caught:
        next(
            iter(
                run_mask_chunked(
                    config,
                    chunks,
                    table=TABLE,
                    engine_version=ENGINE_VERSION,
                    key_provider=key_provider(),
                )
            )
        )
    exc = caught.value
    assert exc.code == "chunked_passthrough_value_unrepresentable"
    _assert_value_free(exc, ("3001", "non-zero nanoseconds"), code=None, chained=True)
    assert type(exc.__cause__) is pa.lib.ArrowInvalid
    assert "chunk 0: passthrough column 'x' holds a value pandas cannot represent. " in str(exc)
