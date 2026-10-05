"""Shared builders for the C4 (date_shift on the chunked route) tests.

The oracle leg is the same table run through `run_mask_chunked` with a still-vetoed
`group_key` column beside the date_shift one (`force_oracle`), so the schema rule applies
to both legs and the comparison is native-chunked vs oracle-chunked, byte for byte.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from decoy_engine import run_mask_chunked
from decoy_engine.vault import VaultWriter
from tests.native._b8_support import (
    FORCE,
    Run,
    assert_same_as_oracle,
    run_one,
    run_pair,
    with_force,
)
from tests.native._chunked_bucket_perturb_support import date_value, spy_index_kernel
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    force_oracle,
    key_provider,
    make_config,
    passthrough,
    vault_key,
)

__all__ = [
    "FORCE",
    "FORMAT_ERROR_REASON",
    "Outcome",
    "Run",
    "assert_same_as_oracle",
    "date_value",
    "ds_col",
    "make_config",
    "passthrough",
    "run_one",
    "run_outcome",
    "run_pair",
    "source",
    "spy_index_kernel",
    "with_force",
]

NAMESPACE = "ns_d"
FORMAT_ERROR_REASON = "value is not a parseable date under date_shift"


def ds_col(
    name: str = "d",
    *,
    date_format: str | None = "%Y-%m-%d",
    namespace: str | None = NAMESPACE,
    min_days: int = -30,
    max_days: int = 30,
    group_by: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A date_shift column. `date_format=None` omits the key (autodetect)."""
    cfg: dict[str, Any] = {"min_days": min_days, "max_days": max_days}
    if date_format is not None:
        cfg["date_format"] = date_format
    if group_by is not None:
        cfg["group_by"] = group_by
    col: dict[str, Any] = {"name": name, "strategy": "date_shift", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    col.update(extra)
    return col


def source(values: Sequence[str | None], *, typ: pa.DataType | None = None) -> pa.Table:
    """A table with the date_shift source `d` and an integer passthrough `p`."""
    return pa.table(
        {
            "d": pa.array(values, typ or pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


@dataclass
class Outcome:
    """One run of `run_mask_chunked` that keeps what a failing run leaves behind."""

    out: list[pa.Table] = field(default_factory=list)
    sink: list[Any] = field(default_factory=list)
    ev: list[Any] = field(default_factory=list)
    error: Exception | None = None
    vault_adds: int = 0
    chunks_pulled: int = 0


class _CountingVault(VaultWriter):
    """A real `VaultWriter` (the entry point type-checks it) that counts `add` calls."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(vault_key())
        self._outcome = outcome

    def add(self, entries: Any) -> None:
        self._outcome.vault_adds += 1
        super().add(entries)


def run_outcome(
    config: dict[str, Any],
    chunks: list[pa.Table],
    *,
    base_row_offset: int = 0,
    native_threads: int = 1,
    with_vault: bool = False,
    secret: bytes | None = None,
) -> Outcome:
    """Run to exhaustion or to the first error; never raises. The caller-owned sinks keep
    the failing chunk's record, the route decision and the vault-add count."""
    outcome = Outcome()

    def pulled() -> Any:
        for chunk in chunks:
            outcome.chunks_pulled += 1
            yield chunk

    try:
        for table in run_mask_chunked(
            config,
            pulled(),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider() if secret is None else key_provider(secret),
            chunk_result_sink=outcome.sink,
            route_evidence_sink=outcome.ev,
            base_row_offset=base_row_offset,
            native_threads=native_threads,
            vault_writer=_CountingVault(outcome) if with_vault else None,
        ):
            outcome.out.append(table)
    except Exception as exc:
        outcome.error = exc
    return outcome


def force_leg(
    columns: list[dict[str, Any]], chunks: list[pa.Table]
) -> tuple[list[dict[str, Any]], list[pa.Table]]:
    return [*columns, force_oracle(FORCE)], [with_force(c) for c in chunks]
