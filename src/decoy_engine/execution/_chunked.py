"""Chunked mask execution (capability-gaps WS4, 2026-06-12).

`run_mask_pipeline_chunked` masks ONE table chunk-by-chunk so inputs too
large for memory stream through the engine. The contract is byte parity:
for any chunking of the rows, concatenated output equals the full-frame
`run_pipeline` output exactly.

That contract is only honest for VALUE-KEYED strategies -- where each
output cell is a pure function of (input cell, config, job seed), never
of row position, neighboring rows, or whole-column state. The v1 safe
set is exactly those:

| strategy     | why chunk-safe |
|--------------|----------------|
| hash         | HMAC of the value |
| fpe          | keyed Feistel permutation of the value |
| redact       | constant |
| truncate     | prefix of the value |
| text_redact  | span replacement within the cell |
| date_shift   | offset derived from the value (derive(seed, ns, value)) |
| bucketize    | bin of the value |
| passthrough  | identity |

Rejected at compile time (`check_chunked_compatibility`):

- shuffle (whole-column permutation), composite/nested (bundle state);
- faker / categorical, even deterministic: pool construction is
  profile-sized whole-run state -- deferred, recorded in the
  capability-gaps plan, not silently wrong;
- configs with relationships (FK resolution reads whole parent frames);
- generate tables (generation is not masking; row_count is whole-run).

Each chunk runs through the SAME compiled plan and the standard pandas
adapter, so chunked output is byte-identical to a serial run by
construction rather than by re-implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

import pyarrow as pa

from decoy_engine.plan._errors import PlanCompileError

CHUNK_SAFE_STRATEGIES: frozenset[str] = frozenset(
    {
        "hash",
        "fpe",
        "redact",
        "truncate",
        "text_redact",
        "date_shift",
        "bucketize",
        "passthrough",
    }
)


def check_chunked_compatibility(config: dict[str, Any], *, table: str) -> None:
    """Reject configs whose chunked run could not match a full-frame run.

    Raises PlanCompileError with codes `chunked_table_unknown`,
    `chunked_generate_unsupported`, `chunked_relationships_unsupported`,
    or `strategy_not_chunk_safe` naming the offending columns.
    """
    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    if table_cfg is None:
        known = sorted(t.get("name", "?") for t in tables if isinstance(t, dict))
        raise PlanCompileError(
            code="chunked_table_unknown",
            path=f"tables.{table}",
            message=f"table {table!r} is not in the config (tables: {known}).",
        )
    if table_cfg.get("generate_columns"):
        raise PlanCompileError(
            code="chunked_generate_unsupported",
            path=f"tables.{table}",
            message=(
                f"table {table!r} is a generate table; chunked execution masks "
                "existing data and has no generation mode (row_count is whole-run "
                "state)."
            ),
        )
    if config.get("relationships"):
        raise PlanCompileError(
            code="chunked_relationships_unsupported",
            path="relationships",
            message=(
                "configs with FK relationships cannot run chunked: resolving a "
                "child key reads the parent's complete source->masked map, which "
                "needs the whole parent frame. Run without --chunked."
            ),
        )
    offending: list[tuple[str, str]] = []
    for col_entry in table_cfg.get("columns") or []:
        if not isinstance(col_entry, dict):
            continue
        strategy = col_entry.get("strategy")
        if strategy is not None and strategy not in CHUNK_SAFE_STRATEGIES:
            offending.append((str(col_entry.get("name", "?")), str(strategy)))
    if offending:
        details = ", ".join(f"{name} ({strategy})" for name, strategy in offending)
        raise PlanCompileError(
            code="strategy_not_chunk_safe",
            path=f"tables.{table}.columns",
            message=(
                f"column(s) {details} use strategies that are not value-keyed and "
                f"cannot produce chunk-invariant output. Chunk-safe strategies: "
                f"{', '.join(sorted(CHUNK_SAFE_STRATEGIES))}."
            ),
        )


def _first_chunk_profile(first_chunk: pa.Table, *, table: str, engine_version: str) -> Any:
    """Profile the FIRST chunk so compile_plan can build the seed envelope.

    The envelope iterates `profile.tables` (the table must exist there
    for its columns to mask at all), so a fully-empty --no-profile-style
    Profile silently masks nothing. The first chunk gives real dtypes;
    distinct counts and row_count describe only that chunk, which is
    fine -- chunk-safe strategies consume nothing distribution-dependent
    (no pools, no capacity pre-flight; those checks land in
    checks_skipped under no_profile=True). Epoch `profiled_at` keeps the
    'not a real source profile' sentinel from the --no-profile path."""
    import random

    from decoy_engine.profile import Profile
    from decoy_engine.profile._walk import walk_dataframe

    table_profile = walk_dataframe(
        first_chunk.to_pandas(),
        table_name=table,
        declared_pk_cols=frozenset(),
        fk_specs={},
        sample_rows=None,
        rng=random.Random(0),
    )
    return Profile(
        schema_version=1,
        tables=(table_profile,),
        relationships=(),
        profiled_at=datetime(1970, 1, 1, 0, 0, 0),
        decoy_engine_version=engine_version,
        profile_seed=None,
    )


def run_mask_pipeline_chunked(
    config: dict[str, Any],
    chunks: Iterable[pa.Table],
    *,
    table: str,
    engine_version: str,
    registry: Any = None,
) -> Iterator[pa.Table]:
    """Mask `table`'s rows chunk-by-chunk under `config`.

    `config` is the validated pipeline config dump; `chunks` yields
    pyarrow Tables of the table's rows in order. Returns an iterator of
    masked chunks in the same order; concatenating them is byte-identical
    to a full-frame `run_pipeline` of the same rows (the value-keyed
    contract, enforced by `check_chunked_compatibility` up front).

    Validation and plan compile happen EAGERLY at call time; only the
    per-chunk masking is lazy.
    """
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.plan import compile_plan
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import RelationshipGraph, build_namespace_registry

    check_chunked_compatibility(config, table=table)
    chunk_iter = iter(chunks)
    first = next(chunk_iter, None)
    if first is None:
        return iter(())
    profile = _first_chunk_profile(first, table=table, engine_version=engine_version)
    plan = compile_plan(config, profile, decoy_engine_version=engine_version, no_profile=True)
    resolved_registry = registry if registry is not None else get_default_registry()
    ns_registry = build_namespace_registry(config, profile)
    graph = RelationshipGraph(edges=(), ordering=())
    adapter = PandasExecutionAdapter()

    def _masked() -> Iterator[pa.Table]:
        for chunk in _chain_first(first, chunk_iter):
            result = adapter.run(
                plan,
                {table: chunk},
                registry=resolved_registry,
                relationship_graph=graph,
                namespace_registry=ns_registry,
            )
            yield result.outputs[table]

    return _masked()


def _chain_first(first: pa.Table, rest: Iterator[pa.Table]) -> Iterator[pa.Table]:
    yield first
    yield from rest
