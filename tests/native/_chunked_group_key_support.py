"""Shared builders for the C3 (group_key on the chunked route) tests.

The oracle leg is the same table run through `run_mask_chunked` with a column that is not
natively admissible beside the group_key one (`force_oracle`, a numeric-categories
categorical), so the schema rule applies to both legs and the comparison is native-chunked
vs oracle-chunked.

group_key keys on a SIBLING column: the group_by source `g`, a passthrough the native leg
reads from the source chunk. The target `k` holds a placeholder string (group_key overwrites
it) and `p` is an unrelated integer passthrough.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa

from decoy_engine.determinism._derive import derive
from tests.native._b8_support import (
    FORCE,
    FORCE_REASON,
    Run,
    identical,
    run_one,
    run_pair,
    with_force,
)
from tests.native._chunked_entry_support import (
    key_provider,
    make_config,
    passthrough,
    redact,
    split,
)

__all__ = [
    "FORCE",
    "FORCE_REASON",
    "GB",
    "MASK_KEY",
    "TARGET",
    "Run",
    "assert_native_equals_oracle",
    "columns",
    "expected_key",
    "gk_col",
    "gk_source",
    "make_config",
    "passthrough",
    "redact",
    "run_one",
    "run_pair",
    "split",
    "with_force",
]

GB = "g"
TARGET = "k"
MASK_KEY = key_provider().mask_key()

_UNSET: Any = object()


def gk_col(
    name: str = TARGET,
    *,
    group_by: str = GB,
    length: int = 16,
    prefix: Any = _UNSET,
    **extra: Any,
) -> dict[str, Any]:
    """A group_key column. `prefix` is omitted unless given (None/False/ints are legal)."""
    cfg: dict[str, Any] = {"group_by": group_by, "length": length}
    if prefix is not _UNSET:
        cfg["prefix"] = prefix
    col: dict[str, Any] = {"name": name, "strategy": "group_key", "provider_config": cfg}
    col.update(extra)
    return col


def columns(gk: dict[str, Any] | None = None, *, sibling: bool = True) -> list[dict[str, Any]]:
    """The default table: passthrough sibling `g` (configured unless `sibling=False`, then
    it is an unconfigured passthrough under the `warn` policy), group_key `k`, passthrough `p`."""
    cols: list[dict[str, Any]] = [gk if gk is not None else gk_col()]
    if sibling:
        cols.insert(0, passthrough(GB))
    cols.append(passthrough("p"))
    return cols


def gk_source(
    values: Sequence[Any],
    typ: pa.DataType | None = None,
    *,
    target: pa.DataType | None = None,
) -> pa.Table:
    """A table with the group_by source `g`, the group_key target's own source `k` (a
    placeholder; `target` varies its Arrow type) and an integer passthrough `p`."""
    n = len(values)
    k_values: list[Any] = ["x"] * n if target in (None, pa.string()) else list(range(n))
    return pa.table(
        {
            GB: pa.array(values, typ),
            TARGET: pa.array(k_values, target or pa.string()),
            "p": pa.array(list(range(n)), pa.int64()),
        }
    )


def expected_key(
    value: Any,
    *,
    column: str = TARGET,
    length: int = 16,
    prefix: str = "",
    mask_key: bytes = MASK_KEY,
) -> str:
    """The scalar definition of one key: `prefix + derive(mask_key, "group_key/<col>",
    str(value))[:length//2].hex()`, computed here and not through the code under test."""
    raw = derive(mask_key, f"group_key/{column}", str(value).encode("utf-8"))
    return prefix + raw[: length // 2].hex()


def assert_native_equals_oracle(
    native: Run, forced: Run, *, expect_native: bool = True, forced_columns: Sequence[str] = ()
) -> None:
    """Values, Arrow types, field nullability and metadata, warnings, timing columns, vault,
    sink lengths and the route each side took. Unlike the B8 comparison it does not demand an
    empty `pandas_read_passthrough`: a group_key node reads its sibling, so both legs list it."""
    assert native.ev[0].native_admitted is expect_native, native.ev[0]
    if expect_native:
        assert native.ev[0].reroute_reason is None
    assert forced.ev[0].native_admitted is False, forced.ev[0]
    assert FORCE_REASON in (forced.ev[0].reroute_reason or "")
    assert len(native.out) == len(forced.out) == len(native.sink) == len(forced.sink)
    for i, (got, want) in enumerate(zip(native.out, forced.out, strict=True)):
        assert identical(got, want.drop_columns([FORCE])), i
    for got, want in zip(native.sink, forced.sink, strict=True):
        assert got.warnings == want.warnings
        assert {r.column for r in got.timings} == {r.column for r in want.timings} - {FORCE}
        assert (
            got.quality_metrics["chunked_route"]["pandas_read_passthrough"]
            == want.quality_metrics["chunked_route"]["pandas_read_passthrough"]
        )
    assert native.vault == forced.vault
