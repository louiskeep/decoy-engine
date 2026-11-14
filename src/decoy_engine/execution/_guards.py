"""Execution-time ingest guards (engine-v2 S13).

`reject_null_bearing_int` is the B1 backstop for the `no_profile` path. The
plan-compile check `null_bearing_int_unsupported` rejects integer + null-bearing
columns under truncate/categorical when a profile is present; under
`no_profile=True` that check lands in `checks_skipped` because dtype/null_count
are unavailable at compile time. This guard re-checks at ingest, on the
`pa.Table` sources BEFORE the `to_pandas` conversion (so the integer type is
still intact, not yet widened to float), and raises the typed error before any
adapter runs. The rejection is loud and identical regardless of route.

PO-settled 2026-05-28 (S13 spec section 1.5); consistent with the S5
float-canonicalization hard error. This rejects loudly + identically; it is not a
silent downgrade.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine._field_names import repeated_names
from decoy_engine.execution._column_access import created_columns
from decoy_engine.execution._errors import (
    ExecutionError,
    declared_table_dropped,
    loader_table_undeclared,
)
from decoy_engine.execution._runner import build_work_list
from decoy_engine.execution._transforms import TransformError
from decoy_engine.plan._checks import _INT_NULL_REJECTED_STRATEGIES
from decoy_engine.profile import (
    DeclarationRequest,
    PreparedDeclaredSources,
    prepare_declared_sources,
)

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.fileaccess import FileAccess
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph


def refuse_duplicate_field_names(table: str, names: Iterable[str]) -> None:
    """Refuse a source whose field names repeat, before anything looks a column up by name.

    The one implementation every entry, ingest guard and handler calls. It reads names only,
    so it cannot itself fail on the duplicate. The message carries the table (a config key)
    and a count, never the names: a header is producer-controlled text. The owner stays
    `TransformError`, the pinned owner of this reason identity.
    """
    repeated = repeated_names(names)
    if repeated:
        raise TransformError(
            code=reasons.runtime.DUPLICATE_SOURCE_FIELD_NAMES,
            message=f"table {table!r} has {len(repeated)} duplicated field name(s)",
        )


def refused_per_chunk(chunks: Iterator[pa.Table], *, table: str) -> Iterator[pa.Table]:
    """Refuse each later chunk as it is pulled; installed innermost, so it runs before every
    wrapper and drift validator a chunked entry adds around the iterator."""
    for chunk in chunks:
        refuse_duplicate_field_names(table, chunk.schema.names)
        yield chunk


# Readers that cannot present a duplicate name to the engine: pandas renames a repeated CSV
# header (`a`, `a.1`) and config validation refuses repeated headerless or fixed-width names.
# A deny-list, so a descriptor format added later is checked by default.
_NO_DUPLICATE_FORMATS = frozenset({"csv", "fixed_width"})


def refuse_duplicate_run_sources(
    config: Mapping[str, Any], caller_sources: Mapping[str, Any]
) -> None:
    """`run_pipeline`'s entry check: every caller table or `LazySource` footer, then every
    declared descriptor whose reader keeps duplicates, before profiling or routing reads any
    column by name. Metadata only: a footer for Parquet, nothing for a resident table."""
    for name, source in caller_sources.items():
        refuse_duplicate_field_names(name, source.schema.names)
    from decoy_engine.profile import build_declared_source

    for name, descriptor in (config.get("sources") or {}).items():
        if descriptor.get("format") in _NO_DUPLICATE_FORMATS:
            continue
        refuse_duplicate_field_names(name, build_declared_source(name, descriptor).schema().names)


def refuse_undeclared_loader_tables(
    table_kinds: Mapping[str, str],
    config: Mapping[str, Any],
    caller_sources: Mapping[str, Any],
    loader_schemas: Mapping[str, Any] | None,
) -> None:
    """Refuse a mask table the run could only reach through the loader and never declared.

    Profiling reads descriptors and caller tables, so such a table profiles as nothing, the plan
    holds no work for it and the adapter would echo its rows. Metadata only, before the loader runs.
    """
    declared = config.get("sources") or {}
    via_loader = loader_schemas or {}
    for name, kind in table_kinds.items():
        if kind != "mask":
            continue
        if name in via_loader and name not in declared and name not in caller_sources:
            raise loader_table_undeclared(name)


def refuse_dropped_declared_tables(
    table_kinds: Mapping[str, str], plan: Plan, supplied: Iterable[str]
) -> None:
    """Construction guard: every supplied mask table has a plan entry.

    A declared table nobody supplies is not an input of this run and stays out of scope.
    """
    planned = {name for name, _ in plan.seed_envelope.per_table}
    present = set(supplied)
    for name, kind in table_kinds.items():
        if kind == "mask" and name in present and name not in planned:
            raise declared_table_dropped(name)


def refusing_source_loader(
    loader: Callable[[str], pa.Table] | None,
) -> Callable[[str], pa.Table] | None:
    """A loader table is known only when it is loaded, so that call is its earliest point."""
    if loader is None:
        return None

    def load(name: str) -> pa.Table:
        table = loader(name)
        refuse_duplicate_field_names(name, table.schema.names)
        return table

    return load


def null_bearing_int_candidates(
    work: Iterable[WorkNode], relationship_graph: RelationshipGraph
) -> Iterator[WorkNode]:
    """Scalar nodes whose strategy is in the int+null reject set and that are masked.

    FK-child columns are EXEMPT: they are resolved through the relationship edge
    (not masked by the strategy), and an FK job runs via the pandas oracle on both
    substrates, so the int+null divergence cannot arise for them. Shared by every
    route's guard so the predicate cannot drift between resident and lazy sources.
    """
    for node in work:
        if node.kind != "scalar" or node.strategy not in _INT_NULL_REJECTED_STRATEGIES:
            continue
        if relationship_graph.parents_of(node.table, node.columns):
            continue  # FK child: resolved via the edge, not masked; no divergence
        yield node


def null_bearing_int_error(node: WorkNode) -> ExecutionError:
    """The one typed rejection every route raises for an integer column with nulls."""
    return ExecutionError(
        code=reasons.runtime.NULL_BEARING_INT_UNSUPPORTED,
        message=(
            f"Column {node.table}.{node.columns[0]} is an integer column with nulls "
            f"masked under {node.strategy!r}. Integer-with-null is not supported "
            "under truncate/categorical (the masked value is ambiguous "
            "across execution substrates); stringify or bin the column upstream."
        ),
    )


def reject_null_bearing_int(
    plan: Plan,
    sources: Mapping[str, pa.Table],
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Raise ExecutionError if a truncate/categorical node masks an integer
    source column that contains a null. Substrate-agnostic: operates on the Arrow
    sources before conversion, so both adapters reject the same input identically.
    """
    work = build_work_list(plan, registry)
    for node in null_bearing_int_candidates(work, relationship_graph):
        table = sources.get(node.table)
        if table is None:
            continue
        column = node.columns[0]
        if column not in table.column_names:
            continue
        if pa.types.is_integer(table.schema.field(column).type) and (
            table.column(column).null_count > 0
        ):
            raise null_bearing_int_error(node)


def reject_gated_hash_on_integer(
    plan: Plan,
    schemas: Mapping[str, pa.Schema],
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Raise if a `when:`-gated hash node masks an integer source column.

    Keyed on the Arrow schema, so it fires with or without nulls and independent of the
    profile or chunk partition. A gated hash would write string tokens into an integer
    frame column and crash late on the first chunk that happens to carry a null. A table
    absent from `schemas` is skipped; the map is by table so a same-named column of
    another type is not confused. FK children are exempt (resolved through the edge).
    """
    for node in build_work_list(plan, registry):
        gate = getattr(node.plan_slice, "when", None)
        if node.kind != "scalar" or node.strategy != "hash" or gate is None:
            continue
        schema = schemas.get(node.table)
        column = node.columns[0]
        if schema is None or column not in schema.names:
            continue
        if not pa.types.is_integer(schema.field(column).type):
            continue
        if relationship_graph.parents_of(node.table, node.columns):
            continue
        raise ExecutionError(
            code=reasons.runtime.GATED_HASH_ON_INTEGER_UNSUPPORTED,
            message=(
                f"Column {node.table}.{column} is an integer column hashed under a "
                "`when:` condition. Conditional hashing of an integer column is not "
                "supported yet; hash the whole column, or stringify it upstream."
            ),
        )


def prepare_declared_execution_sources(
    request: DeclarationRequest,
    *,
    file_access: FileAccess | None = None,
    registry: ProviderRegistry | None = None,
) -> PreparedDeclaredSources:
    """The one pre-read boundary every execution route calls instead of `prepare_declared_sources`.

    It adds the execution layer's rule for columns the config creates itself (from
    `SURFACE_DECLARATIONS`), which `profile` cannot import. Execution routes build the
    capability here and nowhere else. `registry` is the one the run resolves, so a created
    column is classified as the runtime classifies it; the default registry applies when a
    route has no caller registry.
    """
    from decoy_engine.providers_v2 import get_default_registry

    registry = registry if registry is not None else get_default_registry()
    return prepare_declared_sources(
        request,
        file_access=file_access,
        created_targets=lambda entry, columns: created_columns(entry, columns, registry),
    )


def run_chunk_ingest_guards(
    plan: Plan,
    sources: Mapping[str, pa.Table],
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
) -> None:
    """Every per-chunk ingest guard that runs on the Arrow sources before masking.

    The one place both chunked routes call: the pandas adapter at the top of
    `run`, and the native chunk loop before it masks, so an input one route
    refuses is refused by the other with the same error. A new ingest guard is
    added here and reaches both. The duplicate-name refusal runs first: the guards after
    it look fields up by name and would raise an uncoded `KeyError` on a repeated one.
    """
    for name, tbl in sources.items():
        refuse_duplicate_field_names(name, tbl.schema.names)
    reject_null_bearing_int(plan, sources, registry, relationship_graph)
    reject_gated_hash_on_integer(
        plan, {name: tbl.schema for name, tbl in sources.items()}, registry, relationship_graph
    )


__all__ = [
    "null_bearing_int_candidates",
    "null_bearing_int_error",
    "prepare_declared_execution_sources",
    "refuse_dropped_declared_tables",
    "refuse_duplicate_field_names",
    "refuse_duplicate_run_sources",
    "refuse_undeclared_loader_tables",
    "refused_per_chunk",
    "refusing_source_loader",
    "reject_gated_hash_on_integer",
    "reject_null_bearing_int",
    "run_chunk_ingest_guards",
]
