"""Token-vault collection and admission for the out-of-core FK route (D3b).

Established methodology: the per-batch collector mirrors the chunked oracle's vault
collection (`_chunked_oracle.py`: per chunk, `vault_writer.add(collect_vault_entries(
config, {table: chunk}, {table: masked}))`), which `VaultWriter` already dedups, so the
resident footprint is O(distinct triples), never a full-table copy. Masking is
element-wise and order-preserving, so pairing a source batch with its masked batch
positionally is the same pairing the full-frame oracle makes over whole columns.

Why admission is an allowlist. The vault stores `str(masked)` / `str(source)` triples, so
any documented OOC-vs-full-frame representation difference (int64-vs-float64 widening,
NaN-vs-null) would leak into the vault as a different key. `vault_ooc_safe` therefore
admits a job only when every vaulted column uses a reviewed strategy on a string/binary
source, resolves to a string masked output, and is not an incoming FK child key (those
columns hold RAW values at the mask step and are remapped at the join, so a mask-step
pairing would record the wrong masked value). Everything else stays on the full-frame
route. `hash` and `categorical` carry the accepted native-vs-Python NFC envelope their
masked output already has (Cam, Option A); the vault key is that run's own masked value,
so recovery stays internally consistent.

Transactional contract. The engine never calls `VaultWriter.write()`; the caller does,
after `run_pipeline` returns. An out-of-core failure raises, so a failed run's writer is
discarded un-persisted even though earlier batches already called `add()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.vault import assert_vault_writer_keyed, collect_vault_entries, iter_vault_columns

if TYPE_CHECKING:
    from collections.abc import Mapping

    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "VAULT_OOC_STRATEGIES",
    "assert_vault_inputs",
    "collect_batch_vault",
    "vault_ooc_safe",
]

# Reviewed-safe vaulted strategies. Anything else that the OOC route can run
# (bucket_perturb, text_redact, text_mask, code_set) is not reviewed for route-stable
# vault parity and declines to full-frame; fpe+vault is a config reject upstream.
VAULT_OOC_STRATEGIES = frozenset({"hash", "categorical", "truncate", "redact", "passthrough"})

_STRING_SOURCE = (pa.string(), pa.large_string(), pa.binary(), pa.large_binary())
_STRING_OUTPUT = (pa.string(), pa.large_string())


def assert_vault_inputs(
    vault_writer: Any, config: Mapping[str, Any] | None, mask_key: bytes
) -> None:
    """Fail closed on a vault request the route cannot honour.

    A writer without the job config cannot know which columns to collect, and an unkeyed
    writer would encrypt reversible plaintext under the wrong secret (DE-02). Direct
    callers of `run_fk_out_of_core` do not pass through `run_pipeline`'s own guard.
    """
    if vault_writer is None:
        return
    if config is None:
        raise ExecutionError(
            code="out_of_core_vault_config_required",
            message="a vault_writer on the out-of-core route needs the job config.",
        )
    assert_vault_writer_keyed(vault_writer, mask_key)


def collect_batch_vault(
    config: Mapping[str, Any] | None,
    table: str,
    source_batch: pa.RecordBatch,
    masked_batch: pa.RecordBatch,
    vault_writer: Any,
) -> None:
    """Record one aligned (source, masked) batch pair into the vault.

    A no-op without a writer, so both drivers call it unconditionally at their mask step.
    Called only after the driver's own mask succeeded; a later failure raises and the
    caller never persists the writer.
    """
    if vault_writer is None:
        return
    vault_writer.add(
        collect_vault_entries(
            dict(config or {}),
            {table: pa.Table.from_batches([source_batch])},
            {table: pa.Table.from_batches([masked_batch])},
        )
    )


def _incoming_child_keys(graph: RelationshipGraph) -> set[tuple[str, str]]:
    return {(e.child_table, c) for e in graph.edges for c in e.child_columns}


def _column_admitted(
    plan: Plan,
    table: str,
    column: str,
    source: pa.Table | LazySource | None,
    child_keys: set[tuple[str, str]],
) -> bool:
    from decoy_engine.execution.out_of_core._compat import _column_seed
    from decoy_engine.execution.out_of_core._mask import masked_output_type

    if (table, column) in child_keys or source is None:
        return False
    seed = _column_seed(plan, table, column)
    if seed is None or seed.strategy not in VAULT_OOC_STRATEGIES:
        return False
    schema = source.schema
    if column not in schema.names:
        return False
    source_type = schema.field(column).type
    if source_type not in _STRING_SOURCE:
        return False
    try:
        # The RESOLVED output type, not the source dtype: categorical categories and a
        # redact replacement can be numeric even on a string source.
        return masked_output_type(seed, source_type) in _STRING_OUTPUT
    except (ExecutionError, pa.ArrowException, TypeError, ValueError):
        return False


def vault_ooc_safe(
    config: Mapping[str, Any],
    plan: Plan,
    graph: RelationshipGraph,
    caller_sources: Mapping[str, pa.Table | LazySource],
    vault_writer: Any,
) -> bool:
    """True when a vault-bearing job may take the out-of-core route.

    Parent-key columns are admitted (vaulting a parent key to recover IDs is the common
    case); only columns that are the child side of some edge decline.
    """
    if vault_writer is None:
        return False
    child_keys = _incoming_child_keys(graph)
    return all(
        _column_admitted(plan, table, column, caller_sources.get(table), child_keys)
        for table, column, _namespace in iter_vault_columns(dict(config))
    )
