"""Prepare resident transform-bearing tables before routing.

Admission has to price the data that will actually run, so a resident table's
transforms are applied once, up front, and the result replaces the raw table in
the sources every later stage reads (routing, the probe, the adapter, validators,
fidelity, quarantine). A lazy table (a `LazySource` or one supplied only through
`source_loader`) is not prepared here: that would materialize it before the
admission decision. It stays raw, is transformed once when its route loads it, and
is never admitted to full-frame under `auto`.

`PreparedSources` records which tables are prepared, so every consumer transforms
only the tables still raw and a prepared table is never transformed twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.execution import _transforms
from decoy_engine.execution._transforms_gate import transform_bearing_mask_tables
from decoy_engine.execution._transforms_table import apply_resident_table
from decoy_engine.profile._readers import LazySource

__all__ = ["PreparedSources", "prepare_resident_transforms", "prepare_transform_sources"]


@dataclass(frozen=True)
class PreparedSources:
    """`sources` with every resident transform-bearing table replaced by its
    transformed table; `prepared` names them; `lazy` names the transform-bearing
    tables that stay raw (lazy or loader-supplied)."""

    sources: dict[str, pa.Table | LazySource]
    prepared: frozenset[str]
    lazy: frozenset[str]


def prepare_transform_sources(
    config: Mapping[str, Any],
    caller_sources: Mapping[str, pa.Table | LazySource],
    *,
    profile: Any,
    graph: Any,
    execution_mode: str,
    has_generate_table: bool,
    has_mask_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
    post_validation: bool,
    resolved_substrate: str,
) -> PreparedSources:
    """`run_pipeline`'s one call before routing: refuse what an explicit mode would
    refuse from the profile and graph alone (so a rejected run never transforms
    anything), then prepare the resident transform-bearing tables."""
    from decoy_engine.execution._transforms_admission import check_explicit_sequential

    check_explicit_sequential(
        config,
        profile,
        graph,
        execution_mode=execution_mode,
        has_generate_table=has_generate_table,
        has_mask_table=has_mask_table,
        validators=validators,
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        post_validation=post_validation,
        resolved_substrate=resolved_substrate,
    )
    return prepare_resident_transforms(config, caller_sources)


def prepare_resident_transforms(
    config: Mapping[str, Any], caller_sources: Mapping[str, pa.Table | LazySource]
) -> PreparedSources:
    bearing = transform_bearing_mask_tables(config)
    sources: dict[str, pa.Table | LazySource] = dict(caller_sources)
    prepared: set[str] = set()
    for name in sorted(bearing):
        source = sources.get(name)
        if isinstance(source, pa.Table):
            _transforms.check_transform_source_schema(config, name, source.schema)
            sources[name] = apply_resident_table(config, name, source)
            prepared.add(name)
        # The raw table is no longer referenced from `sources`; the caller's own
        # mapping still owns it.
    return PreparedSources(
        sources=sources,
        prepared=frozenset(prepared),
        lazy=frozenset(bearing - prepared),
    )
