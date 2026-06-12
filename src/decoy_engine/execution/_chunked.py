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

CONDITIONALLY admitted (deferred follow-up 2, 2026-06-12): faker and
categorical, exactly when their deterministic value-keyed path is the
one that runs and every whole-run input is declared in config rather
than derived from the data:

- faker: `deterministic: true` + `namespace` + an explicit
  `provider_config.pool_size` + `cardinality_mode` absent or `reuse`.
  The deterministic sampler maps each value via
  `derive_index(job_seed, namespace, canonicalize(value), pool_size)`,
  independent of row position or chunk arrival, and the pool build is
  RNG-seeded by its identity, so a pre-built pool equals any rebuild.
  A chunk with more distinct values than `pool_size` changes nothing:
  derive_index maps any value into [0, pool_size) with collisions
  allowed, byte-identical to the full-frame run of the same rows
  (pool_size controls collision rate, not admission).
- categorical: `deterministic: true` + `namespace` + explicit
  `provider_config.categories`, and NOT `from_profile` (profile-derived
  categories would come from the first chunk only).

Rejected at compile time (`check_chunked_compatibility`):

- shuffle (whole-column permutation), composite/nested (bundle state):
  `strategy_not_chunk_safe`;
- faker / categorical with the conditions above unmet:
  `chunked_strategy_conditions_unmet`, naming each unmet condition;
- configs with relationships (FK resolution reads whole parent frames);
- generate tables (generation is not masking; row_count is whole-run).

Each chunk runs through the SAME compiled plan, one execution adapter
(pandas by default; polars via the `adapter` parameter), and one shared
pre-warmed pool cache, so chunked output is byte-identical to a serial
run on the same substrate by construction rather than by
re-implementation. Cross-substrate parity is value-level, per the v2
rows in tests/parity/SEMANTIC_DIFFERENCES.md.
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

# Admitted only when the column's config pins the deterministic
# value-keyed path (see module docstring for the per-strategy rules).
CHUNK_CONDITIONAL_STRATEGIES: frozenset[str] = frozenset({"faker", "categorical"})


def _conditional_admission_failures(col_entry: dict[str, Any]) -> list[str]:
    """Unmet chunked-admission conditions for a faker/categorical column.

    Returns one human-readable string per unmet condition; an empty
    list means the column is admitted.
    """
    strategy = col_entry.get("strategy")
    cfg = col_entry.get("provider_config") or {}
    failures: list[str] = []
    if not col_entry.get("deterministic"):
        failures.append(
            "requires deterministic: true (the non-deterministic path draws "
            "per-row randomness, which is chunk-variant)"
        )
    if not col_entry.get("namespace"):
        failures.append("requires a namespace (the value-keyed mapping derives from it)")
    if strategy == "faker":
        if cfg.get("pool_size") is None:
            failures.append(
                "requires an explicit provider_config.pool_size as the chunked "
                "capacity declaration (the non-chunked default of 10000 is not "
                "applied silently here)"
            )
        if col_entry.get("cardinality_mode") not in (None, "reuse"):
            failures.append(
                "requires cardinality_mode absent or 'reuse' (source-cardinality "
                "modes describe whole-run state)"
            )
    if strategy == "categorical":
        if cfg.get("from_profile"):
            failures.append(
                "from_profile derives categories from the profile, which chunked "
                "mode builds from the first chunk only; declare categories "
                "explicitly"
            )
        elif not cfg.get("categories"):
            failures.append("requires explicit provider_config.categories")
    return failures


def check_chunked_compatibility(config: dict[str, Any], *, table: str) -> None:
    """Reject configs whose chunked run could not match a full-frame run.

    Raises PlanCompileError with codes `chunked_table_unknown`,
    `chunked_generate_unsupported`, `chunked_relationships_unsupported`,
    `strategy_not_chunk_safe`, or `chunked_strategy_conditions_unmet`
    naming the offending columns.
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
    conditions_unmet: list[tuple[str, str, list[str]]] = []
    for col_entry in table_cfg.get("columns") or []:
        if not isinstance(col_entry, dict):
            continue
        strategy = col_entry.get("strategy")
        if strategy is None or strategy in CHUNK_SAFE_STRATEGIES:
            continue
        if strategy in CHUNK_CONDITIONAL_STRATEGIES:
            failures = _conditional_admission_failures(col_entry)
            if failures:
                conditions_unmet.append((str(col_entry.get("name", "?")), str(strategy), failures))
            continue
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
    if conditions_unmet:
        details = "; ".join(
            f"{name} ({strategy}: {'; '.join(failures)})"
            for name, strategy, failures in conditions_unmet
        )
        raise PlanCompileError(
            code="chunked_strategy_conditions_unmet",
            path=f"tables.{table}.columns",
            message=(
                f"column(s) {details}. faker/categorical run chunked only on "
                "their deterministic value-keyed path with all whole-run inputs "
                "declared in config (see run_mask_pipeline_chunked docs)."
            ),
        )


def _first_chunk_profile(first_chunk: pa.Table, *, table: str, engine_version: str) -> Any:
    """Profile the FIRST chunk so compile_plan can build the seed envelope.

    The envelope iterates `profile.tables` (the table must exist there
    for its columns to mask at all), so a fully-empty --no-profile-style
    Profile silently masks nothing. The first chunk gives real dtypes;
    distinct counts and row_count describe only that chunk, which is
    fine -- admitted strategies consume nothing distribution-dependent.
    Faker pools size from the config-declared pool_size (the admission
    rule requires it explicitly), never from profile distinct counts,
    and the pool-capacity pre-flight lands in checks_skipped under
    no_profile=True, which is correct here: with admission restricted
    to deterministic REUSE, pool capacity is a collision-rate knob, not
    a correctness input. Epoch `profiled_at` keeps the 'not a real
    source profile' sentinel from the --no-profile path."""
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
    adapter: Any = None,
) -> Iterator[pa.Table]:
    """Mask `table`'s rows chunk-by-chunk under `config`.

    `config` is the validated pipeline config dump; `chunks` yields
    pyarrow Tables of the table's rows in order. Returns an iterator of
    masked chunks in the same order; concatenating them is byte-identical
    to a full-frame `run_pipeline` of the same rows (the value-keyed
    contract, enforced by `check_chunked_compatibility` up front).

    `adapter` selects the execution substrate; None keeps the pandas
    adapter (the byte-stable default this mode shipped with). Pass a
    `PolarsExecutionAdapter` (e.g. via `select_execution_adapter`) for
    polars-substrate streaming; cross-substrate output is VALUE-equal,
    not Arrow-schema-equal (string widens to large_string etc.; the
    recorded v2 rows in tests/parity/SEMANTIC_DIFFERENCES.md).

    Validation and plan compile happen EAGERLY at call time; only the
    per-chunk masking is lazy.
    """
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.generation.pool import PoolCache
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
    if adapter is None:
        adapter = PandasExecutionAdapter()
    # One cache for the whole run: faker pools build ONCE (eagerly, so a
    # provider failure surfaces before any output streams) and every
    # chunk samples from the same pool via the handler's cache consult.
    pool_cache = PoolCache()
    _warm_faker_pools(
        config,
        table=table,
        job_seed=plan.seed_envelope.job_seed,
        registry=resolved_registry,
        pool_cache=pool_cache,
    )

    def _masked() -> Iterator[pa.Table]:
        for chunk in _chain_first(first, chunk_iter):
            result = adapter.run(
                plan,
                {table: chunk},
                registry=resolved_registry,
                pool_cache=pool_cache,
                relationship_graph=graph,
                namespace_registry=ns_registry,
            )
            yield result.outputs[table]

    return _masked()


def _warm_faker_pools(
    config: dict[str, Any],
    *,
    table: str,
    job_seed: bytes,
    registry: Any,
    pool_cache: Any,
) -> None:
    """Build each admitted faker column's pool once into `pool_cache`.

    The build parameters mirror FakerStrategyHandler exactly (same
    pool_size/locale/config split), so the handler's identity_for
    lookup hits this cache on every chunk. Caching is byte-safe: the
    build is RNG-seeded by the identity's pool_seed (S5 F2), so any
    rebuild of the same identity is value-identical.
    """
    from decoy_engine.generation.pool import PoolBuilder

    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    if table_cfg is None:
        return
    builder = PoolBuilder(registry)
    for col_entry in table_cfg.get("columns") or []:
        if not isinstance(col_entry, dict) or col_entry.get("strategy") != "faker":
            continue
        provider = col_entry.get("provider")
        if provider is None:
            continue
        cfg = dict(col_entry.get("provider_config") or {})
        pool_size = int(cfg["pool_size"])  # admission requires it explicitly
        locale = cfg.get("locale")
        build_config = {k: v for k, v in cfg.items() if k not in ("pool_size", "locale")}
        identity = builder.identity_for(
            str(provider),
            size=pool_size,
            job_seed=job_seed,
            locale=locale,
            config=build_config,
            namespace=col_entry.get("namespace"),
        )
        if pool_cache.get(identity) is not None:
            continue
        pool_cache.put(
            builder.build(
                provider=str(provider),
                size=pool_size,
                job_seed=job_seed,
                locale=locale,
                config=build_config,
                namespace=col_entry.get("namespace"),
            )
        )


def _chain_first(first: pa.Table, rest: Iterator[pa.Table]) -> Iterator[pa.Table]:
    yield first
    yield from rest
