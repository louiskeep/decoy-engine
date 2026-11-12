"""Table-from-schema synthesis (engine-v2 S6).

Produces synthetic tables from a generate-mode ``PipelineConfig``: for each generate table
(``generate_columns`` + ``row_count``, no source), build ``row_count`` rows, each declared column
filled by its per-column generator. This is the v2 analogue of V1 ``DataGenerator``
(``decoy_engine.generators``); it is PARITY-FROZEN to V1 under a fixed seed (Reading B), except an
eligible pooled ``faker`` column (GP2 + the faker-widening ADDITION slice, see ``_faker_pool``).

S6-ENG-1 landed the spine + the ``sequence`` generator. S6-ENG-2 adds parity-frozen
``categorical`` (and on the next sub-commits, ``faker`` / ``formula``); S6-ENG-3 adds FK-aware
generation (mint-a-pool); S6-ENG-4 the seed / derive-key determinism envelope.

Parity seeding uses V1's ``GenDeriveContext`` (``decoy_engine.generators.derivation``) directly so
the per-column derivation is byte-identical to V1 ``ColumnGenerator._column_ctx`` under the same
``derive_key`` (always ``None`` in ENG-2; ENG-4 wires the real key).

Thread-safety: all explicit RNG use here is instance-local (``random.Random(seed)``) so two
``generate_tables`` calls in different threads do not corrupt each other's draws.
``random.Random(s)`` produces the same sequence as ``random.seed(s)``, so V1 byte-parity is
preserved. Faker state is likewise isolated per worker: the no-locale default instance is
thread-local and the locale paths construct fresh instances per call. ``Faker.seed_instance``
detaches the instance onto its own ``random.Random`` and re-seeds it, so a per-thread instance
seeded per row produces the exact sequence a reseeded shared instance did; output bytes are
unchanged. This replaces the QA-7 F1 (2026-06-01) ``_FAKER_CALL_LOCK`` that serialized every
seed_instance + provider_func pair across threads: isolation makes the race structurally impossible
instead of locked away.
"""

from __future__ import annotations

import random
import threading
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
from faker import Faker

from decoy_engine.generation import _faker_pool
from decoy_engine.generation._numeric_typing import numeric_plan, restore_declared
from decoy_engine.generation._statistical_column import (
    arrow_table,
    statistical_generate,
    warn_on_unseen_parents,
)
from decoy_engine.generation._synthesize_derived import _derived_generate, _formula, _reference
from decoy_engine.generation.kinds import (
    LookupList,
    constant_values,
    lookup_column_values,
    lookup_digests,
    parametric_values,
)
from decoy_engine.generation.statistical import FanoutSpec, StatisticalSpec
from decoy_engine.generators.derivation import TABLE_KEY, GenDeriveContext, with_table
from decoy_engine.internal.faker_setup import get_faker_providers, make_faker
from decoy_engine.transforms.derived_aggregate import generate_derived_aggregate_column

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from decoy_engine.generation.pool._events import QualityWarning

# QA-7 F5 (2026-06-01): seed default aligned with plan compiler's _normalize_job_seed default
# (0). Pre-fix _DEFAULT_SEED = 42 diverged from plan/_compile.py which defaults to 0 when
# global_settings.seed is absent. Same config, different effective seeds for generate vs mask.
# The number 42 was historical; zero is what the rest of the determinism layer assumes.
_DEFAULT_SEED = 0

# F-5 fix: Faker() construction loads locale data + registers ~200 providers (50-200ms), so the
# no-locale instance is cached per THREAD, not per process (memory scales with live worker
# threads). A shared instance re-seeded via `seed_instance` races under concurrent
# generate_tables calls; QA-7 F1 masked that by locking every seed_instance + provider_func pair.
# Per-thread instances remove the race and the lock. Seed-derived output is unchanged: per-row
# seed_instance detaches onto an instance-local random.Random, so a fresh instance seeded the
# same yields the same sequence as a reseeded shared one. (A custom provider drawing from
# process-global state like fake.unique was already non-deterministic and is outside the
# seeded-draw contract.)
_THREAD_LOCAL = threading.local()


def _get_default_faker() -> Faker:
    fake = getattr(_THREAD_LOCAL, "default_faker", None)
    if fake is None:
        fake = Faker()
        _THREAD_LOCAL.default_faker = fake
    return fake


def _generate_tables_from_config(
    config: dict[str, Any],
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    *,
    statistical_specs: dict[tuple[str, str], StatisticalSpec] | None = None,
    snapshot_index_for_column: dict[tuple[str, str], int] | None = None,
    snapshot_artifacts: list[dict[str, Any]] | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
    reference_fanouts: dict[tuple[str, str], FanoutSpec] | None = None,
    lookup_lists: dict[tuple[str, str], LookupList] | None = None,
    on_warning: Callable[[QualityWarning], None] | None = None,
) -> dict[str, pa.Table]:
    """The actual generation logic, over a plain config dict recovered
    from ``GenerationPlan.config_json``. Not part of the public surface
    (guide section 4.8: keep lower-level generation helpers private).

    ``statistical_specs`` is ``{(table_name, column_name): StatisticalSpec}``,
    already validated and pinned at compile time (guide section 4.7/4.8):
    the ``statistical`` dispatch below (``_statistical_column.
    statistical_generate``) consumes it directly and never reopens a
    snapshot path. ``snapshot_index_for_
    column``/``snapshot_artifacts`` feed the fidelity gate the same pinned
    artifacts, keyed the same way. ``provider_snapshot`` (5a-faker) forwards
    to every ``_generate_column`` call; ``None`` (the default) resolves
    custom faker providers against the live registry exactly as before.
    ``reference_fanouts`` is ``{(table_name, column_name): FanoutSpec}`` decoded from the
    pinned snapshot bytes; a ``distribution: fitted`` reference column reads only this.
    ``lookup_lists`` is ``{(table_name, column_name): LookupList}`` parsed from the plan's pinned
    lookup bytes (or a shipped code set); a ``lookup`` column reads only this, never its ``file``.
    ``on_warning`` receives each fidelity-gate ``QualityWarning`` as it is raised; it never
    changes the generated tables.
    """
    statistical_specs = statistical_specs or {}
    snapshot_index_for_column = snapshot_index_for_column or {}
    snapshot_artifacts = snapshot_artifacts or []
    # F5 (2026-06-26): use the single shared seed validator so a bool/float
    # seed is rejected identically to the plan compiler + profile path
    # (QA-7 F8 / QA-3 F1 lineage), instead of int(True) silently coercing
    # `seed: true` to 1 on a direct generate call. Defaults absent seed to
    # 0 (== _DEFAULT_SEED, pinned by test_v2_generation.py).
    from decoy_engine.plan._seed import _normalize_job_seed_int

    seed = _normalize_job_seed_int(config)
    tables_list = config.get("tables") or []
    # Generate tables only (mask tables are skipped). Key by name + build the dep
    # graph so a `reference` column can read its already-generated parent's pool.
    generate_by_name = {t["name"]: t for t in tables_list if t.get("generate_columns")}
    # Parents-before-children, ties by declaration and first-reference order (never a `set`).
    ordered = [name for name, _parents in generation_order(config)]
    numeric = numeric_plan(config, provider_snapshot=provider_snapshot)
    pools: dict[str, pa.Table] = {}
    out: dict[str, pa.Table] = {}
    for name in ordered:
        table = generate_by_name[name]
        gcols = table["generate_columns"]
        n = int(table.get("row_count") or 0)
        # Declared order, explicit loop: a `statistical` column with
        # `condition_on` reads its already-generated sibling from `data`
        # (WS3 sequential conditional sampling). Iteration order is the
        # same as the prior dict comprehension; parity unaffected.
        data: dict[str, list[Any]] = {}
        # GEN-UNIQUE pair hook: a `unique_together` entry is drawn at its first-declared member's
        # turn; the second member's values wait here until the loop reaches it.
        pair_hooks: dict[str, Any] = {}
        pending: dict[str, list[Any]] = {}
        if table.get("unique_together"):
            from decoy_engine.generation._unique import pair_hooks as _pair_hooks

            pair_hooks = _pair_hooks(table)
        for col in gcols:
            cname = col["name"]
            if cname in pending:
                data[cname] = pending.pop(cname)
            elif cname in pair_hooks:
                from decoy_engine.generation._unique import unique_pair

                first, second = pair_hooks[cname]
                data[cname], pending[second["name"]] = unique_pair(
                    first, second, n, seed, derive_key, pools, table_name=name
                )
            else:
                data[cname] = _generate_column(
                    col,
                    n,
                    seed,
                    derive_key,
                    pools,
                    instance_default_locale,
                    data,
                    table_name=name,
                    statistical_specs=statistical_specs,
                    provider_snapshot=provider_snapshot,
                    numeric_type=numeric.get((name, col["name"])),
                    reference_fanouts=reference_fanouts,
                    lookup_lists=lookup_lists,
                )
                warn_on_unseen_parents(
                    col,
                    n,
                    data,
                    table_name=name,
                    statistical_specs=statistical_specs,
                    on_warning=on_warning,
                )
        # Cross-column formula post-pass: a `formula` column carrying
        # `references` was filled with None placeholders by `_formula`
        # above (the per-column loop cannot read siblings that may not
        # exist yet). Now that every sibling column is finalized in
        # `data`, `fill_referenced_formula_columns` overwrites them in
        # declared order from the finished values. Lazy import (mirrors
        # the fidelity-gate dispatch) so configs without reference-bearing
        # formulas never pay pandas.
        if any(c.get("type") == "formula" and (c.get("references") or []) for c in gcols):
            from decoy_engine.generation._referenced_formula import (
                fill_referenced_formula_columns,
            )

            fill_referenced_formula_columns(gcols, data, seed, derive_key, table_name=name)
        tbl = arrow_table(data, table_name=name, statistical_specs=statistical_specs, columns=gcols)
        # Generation-time fidelity warn-gate (deferred follow-up 5,
        # 2026-06-12): score statistical columns against their source
        # snapshot and warn below global_settings.fidelity_warn_threshold.
        # Warn-only; output bytes are untouched. Lazy import mirrors the
        # `statistical` dispatch so non-statistical configs never pay it.
        if any(c.get("type") == "statistical" for c in gcols):
            from decoy_engine.generation._fidelity_gate import (
                fidelity_warn_threshold,
                warn_on_low_fidelity,
            )

            warn_on_low_fidelity(
                gcols,
                data,
                table_name=name,
                threshold=fidelity_warn_threshold(config),
                statistical_specs=statistical_specs,
                snapshot_index_for_column=snapshot_index_for_column,
                snapshot_artifacts=snapshot_artifacts,
                on_warning=on_warning,
            )
        pools[name] = tbl
        out[name] = tbl
    return out


def generation_order(config: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Each generate table with the generate tables it references, parents first.

    The one order walk for every generation route. Roots are visited in table declaration order
    and each table's parents in first-reference order (the first `type: reference` column naming
    each parent, deduplicated), so the result is a function of the config alone and does not
    depend on `PYTHONHASHSEED`. Takes unvalidated dicts, so a `reference_table` that is not a
    generate table raises a typed `ValueError` here rather than a `KeyError` downstream.
    """
    generate_by_name = {
        t["name"]: t for t in config.get("tables") or [] if t.get("generate_columns")
    }
    deps: dict[str, tuple[str, ...]] = {}
    for name, t in generate_by_name.items():
        refs: list[str] = []
        for col in t["generate_columns"]:
            if col.get("type") != "reference":
                continue
            ref = col["reference_table"]
            if ref not in generate_by_name:
                raise ValueError(
                    f"table {name!r} column {col.get('name')!r}: "
                    f"reference_table {ref!r} is not a generate table"
                )
            refs.append(ref)
        deps[name] = tuple(dict.fromkeys(refs))
    return tuple((name, deps[name]) for name in _topo_sort(deps))


def _topo_sort(deps: Mapping[str, Iterable[str]]) -> list[str]:
    """Iterative DFS post-order over the reference dep graph. The
    PipelineConfig validator already pinned the graph acyclic, so this
    is order-only; missing nodes (e.g. a parent referenced by name that
    is not in deps) are tolerated -- the validator would have caught a
    genuinely missing parent before we got here.

    QA finding fix (2026-06-02, engine FC-1 review Finding 1): the
    prior implementation used recursive Python DFS, which hits the
    default 1000-frame recursion limit on long reference chains
    (>~1000 generate tables) and crashes with RecursionError at
    runtime. The iterative DFS below uses an explicit work stack of
    (node, parent_iterator) pairs and emits the same post-order. The
    sibling iterative pattern in config/_pipeline.py
    `_reference_graph_valid` was written for the same reason.
    """
    result: list[str] = []
    visited: set[str] = set()

    for start in deps:
        if start in visited or start not in deps:
            continue
        stack: list[tuple[str, Iterator[str]]] = [(start, iter(deps.get(start, ())))]
        visited.add(start)
        while stack:
            node, parent_iter = stack[-1]
            next_parent = next(parent_iter, None)
            if next_parent is None:
                result.append(node)
                stack.pop()
                continue
            if next_parent in visited or next_parent not in deps:
                continue
            visited.add(next_parent)
            stack.append((next_parent, iter(deps.get(next_parent, ()))))
    return result


def _generate_column(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any = None,
    pools: dict[str, pa.Table] | None = None,
    instance_default_locale: str | None = None,
    generated: dict[str, list[Any]] | None = None,
    *,
    table_name: str = "",
    statistical_specs: dict[tuple[str, str], StatisticalSpec] | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
    reference_date: str | None = None,
    pinned: bool = False,
    numeric_type: pa.DataType | None = None,
    reference_fanouts: dict[tuple[str, str], FanoutSpec] | None = None,
    lookup_lists: dict[tuple[str, str], LookupList] | None = None,
) -> list[Any]:
    """Dispatch a generate column to its generator by ``type`` (mirrors V1
    ``ColumnGenerator.generators``), then apply the V1 ``null_probability``
    post-process (V1 ``generate_column`` lines 174-187) so the same fraction of
    rows is nulled at byte-identical row positions. ``pools`` carries already-
    generated parent tables for ``reference`` columns (S6-ENG-3 mint-a-pool).
    ``instance_default_locale`` (S6-ENG-4 M1) flows the platform's
    ``AppSettings.default_faker_locale`` into the shared-Faker path for the
    no-per-column-locale branch of ``_faker``, mirroring V1 ``ColumnGenerator``.
    ``table_name``/``statistical_specs`` (DPS Scope B) are the pinned-spec
    lookup for the ``statistical`` branch -- no snapshot path is reopened
    here (guide section 4.8). ``provider_snapshot`` (5a-faker) forwards to the ``faker`` branch
    only. ``reference_date`` (pinned execution only) anchors ``windowed_date`` ``now``/``today``."""
    col = with_table(col, table_name)
    kind = col.get("type")
    if kind == "sequence":
        values: list[Any] = _sequence(col, n, numeric_type is not None)
    elif kind == "categorical":
        values = _categorical(col, n, seed, derive_key)
    elif kind == "faker":
        values = _faker(
            col, n, seed, derive_key, instance_default_locale, provider_snapshot=provider_snapshot
        )
    elif kind == "formula":
        values = _formula(col, n, seed, derive_key)
    elif kind == "reference":
        if col.get("distribution") == "fitted":
            fanout = (reference_fanouts or {}).get((table_name, str(col.get("name"))))
            values = _reference(col, n, seed, derive_key, pools or {}, fanout=fanout)
        else:
            # The non-fitted call keeps its five-argument shape: parity tests wrap `_reference`.
            values = _reference(col, n, seed, derive_key, pools or {})
    elif kind == "constant":
        values = constant_values(col, n)
    elif kind == "distribution":
        values = parametric_values(
            col,
            n,
            GenDeriveContext.for_column(
                derive_key=derive_key, column_config=col, fallback_seed=seed
            ),
        )
    elif kind == "lookup":
        values = lookup_column_values(col, n, seed, derive_key, table_name, lookup_lists)
    elif kind == "statistical":
        values = statistical_generate(
            col, n, seed, derive_key, generated or {}, table_name, statistical_specs or {}
        )
    elif kind == "derived":
        values = _derived_generate(col, n, generated or {})
    elif kind == "derived_aggregate":
        values = generate_derived_aggregate_column(col, n, generated or {})
    elif kind in ("grouped_series", "windowed_date", "group_key"):
        from decoy_engine.generation._grouped_windowed_generators import (
            _group_key_generate,
            _grouped_series_generate,
            _windowed_date_generate,
        )

        if kind == "grouped_series":
            values = _grouped_series_generate(col, n, seed, generated or {})
        elif kind == "windowed_date":
            values = _windowed_date_generate(
                col, n, seed, generated or {}, reference_date=reference_date
            )
        else:
            values = _group_key_generate(col, n, seed, generated or {})
    else:
        # The Literal on GenerateColumnConfig.type rejects anything outside this set
        # at validation; this branch is the defensive fallback for callers that
        # bypass validation (e.g. an unvalidated dict).
        raise ValueError(f"generate column {col.get('name')!r}: unexpected generator type {kind!r}")
    values = restore_declared(col, values, numeric_type)
    # Pinned plans store `snapshot_file` as `sha256:<digest>`, not a path. Passing the digest keeps
    # for_column from trying to open that string as a file. The ordinary path passes None and
    # derives the null seed exactly as before.
    pinned_spec = (statistical_specs or {}).get((table_name, str(col.get("name"))))
    digests: dict[tuple[str, str], str] | None = None
    if pinned and kind == "statistical" and pinned_spec is not None and pinned_spec.snapshot_digest:
        digests = {("statistical", "snapshot_file"): f"sha256:{pinned_spec.snapshot_digest}"}
    if kind == "reference" and col.get("distribution") == "fitted":
        # The null mask derives its seed through `for_column` too; the pinned digest keeps
        # it from reopening `snapshot_file`. A missing spec already raised in `_reference`.
        fanout = (reference_fanouts or {}).get((table_name, str(col.get("name"))))
        digests = {("reference", "snapshot_file"): fanout.seed_digest} if fanout else None
    elif kind == "lookup":
        digests = lookup_digests(col, (lookup_lists or {}).get((table_name, str(col.get("name")))))
    return _apply_null_probability(values, col, seed, derive_key, content_digests=digests)


def _sequence(col: dict[str, Any], n: int, numeric: bool = False) -> list[Any]:
    """Sequential values, parity-frozen vs V1 ``_generate_sequence_column``
    (``columns.py:305-319``): the streamed generator's own batch, so both routes agree.

    A bare counter is ints; a ``prefix`` / ``suffix`` / ``pad_length`` renders every value through
    ``f"{prefix}{value_str}{suffix}"`` as text.
    """
    from decoy_engine.generation._column_streams import SequenceStream

    return SequenceStream(col, numeric=numeric).next_batch(0, n)


def _categorical(col: dict[str, Any], n: int, seed: int, derive_key: Any = None) -> list[Any]:
    """Weighted / uniform random choice over ``categories``, parity-frozen vs V1
    ``_generate_categorical_column`` (``columns.py:321-353``).

    V1 reseeds ``random`` from the column seed (so output is stable across runs +
    order-independent across columns when keyed), then ``random.choices(categories,
    weights=weights, k=num_rows)``. ``weights`` is optional; when omitted the choice
    is uniform. We reuse V1 ``GenDeriveContext`` for the per-column derivation
    (import V1's helper, do not reinvent), so seed-only output is byte-identical
    to V1's under the same ``seed`` + ``derive_key=None``.
    """
    if col.get("unique") is True:
        from decoy_engine.generation._unique import unique_categorical

        return unique_categorical(col, n, seed, derive_key)
    cats = col.get("categories", ["Category A", "Category B"])
    weights = col.get("weights")  # optional; None -> uniform
    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    ).base_int("py")
    # Instance-local Random: parity-preserving (same Mersenne Twister state
    # initialization as random.seed); thread-safe (no module-global mutation).
    rng = random.Random(col_seed)
    return rng.choices(cats, weights=weights, k=n)


def _faker(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    *,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> list[Any]:
    """Faker-driven values, parity-frozen vs V1 ``_generate_faker_column``
    (``columns.py:205-276``), except the GP2 pool path below.

    Pattern (mirror V1): pick the Faker instance (fresh per-locale when ``locale`` is set,
    otherwise a shared instance), look up the provider by ``faker_type`` (default ``"word"``, fall
    back to ``"word"`` for unknown types), then per row seed ``random`` AND
    ``faker_inst.seed_instance`` with ``col_seed + i`` and call ``provider_func(**faker_kwargs)``.
    The per-row seed_instance override means the initial instance seed does not affect output --
    parity holds independent of how the instance was constructed.

    ``faker_kwargs`` is optional; non-dict values are dropped (matches V1's silent
    drop, ``columns.py:253-259``). GP2: an eligible pooled column dispatches to
    ``_faker_pool.try_pool`` first (see its docstring); else falls through unchanged.

    ``provider_snapshot`` (5a-faker, additive): threaded to both the pooled
    and per-row custom-provider resolution below. ``None`` (the default)
    resolves against the live custom-provider registry exactly as before --
    this parameter exists so a caller comparing two independent
    `generate_tables` calls (the shadow-parity harness) can pin both to the
    SAME captured registry state instead of two live reads that a
    concurrent register/unregister could straddle.
    """
    faker_type = col.get("faker_type", "word")
    locale = col.get("locale")
    raw_kwargs = col.get("faker_kwargs") or {}
    faker_kwargs = raw_kwargs if isinstance(raw_kwargs, dict) else {}
    pooled = _faker_pool.try_pool(
        col,
        n,
        seed,
        derive_key,
        instance_default_locale,
        faker_kwargs,
        provider_snapshot=provider_snapshot,
    )
    if pooled is not None:
        return pooled
    gen_ctx = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    )
    if locale:
        faker_inst = make_faker(locale)
        pre_seed: int | None = None
    elif instance_default_locale:
        # S6-ENG-4 M1: when no per-column locale, fall through to the platform's
        # instance default locale (mirrors V1 `ColumnGenerator.__init__` lines
        # 68-72 which uses `make_faker(instance_default_locale)` for `self.faker`).
        faker_inst = make_faker(instance_default_locale)
        pre_seed = seed
    else:
        # F-5 fix: cache the no-locale instance per thread. Per-row
        # seed_instance below overrides the initial seed, so per-thread reuse
        # is output-identical to a fresh instance.
        faker_inst = _get_default_faker()
        pre_seed = seed
    providers = get_faker_providers(faker_inst, provider_snapshot=provider_snapshot)
    provider_func = providers.get(faker_type) or providers["word"]
    out: list[Any] = []
    # No lock: every path above yields an instance no other thread touches (thread-local default, or
    # a fresh make_faker construction), so the seed_instance + provider_func pair cannot race. The
    # QA-7 F1/C1 lock existed only because the default instance was a process-wide singleton.
    if pre_seed is not None:
        faker_inst.seed_instance(pre_seed)
    if col.get("unique") is True:
        from decoy_engine.generation._unique import unique_faker

        return unique_faker(
            faker_inst,
            provider_func,
            faker_kwargs,
            gen_ctx,
            n,
            table=col.get(TABLE_KEY),
            column=col.get("name"),
        )
    for i in range(n):
        faker_inst.seed_instance(gen_ctx.row_int("faker", i))
        out.append(provider_func(**faker_kwargs))
    return out


def _apply_null_probability(
    values: list[Any],
    col: dict[str, Any],
    seed: int,
    derive_key: Any = None,
    *,
    content_digests: dict[tuple[str, str], str] | None = None,
) -> list[Any]:
    """Apply V1's ``null_probability`` post-process (``columns.py:174-187``): per-row
    seeded coin-flip; same column + same row -> same null/non-null decision across
    runs. No-op when ``null_probability`` is unset or 0. Used uniformly by every
    generator (V1 applies it generically in ``generate_column``)."""
    null_prob = float(col.get("null_probability") or 0.0)
    if null_prob <= 0:
        return values
    # QA-1 M17 (2026-06-01): pass the FULL column_config to
    # GenDeriveContext so two columns with different strategies +
    # the same name no longer share a null mask. Pre-fix this used
    # only `{"name": col["name"]}` to mirror V1; V1 has been
    # updated to also pass column_config (qa-1 step 3) so V1 and V2
    # stay byte-identical AND the null-mask collision is closed.
    # F2/F3 (2026-06-26): unify the null mask with V1 (columns.py null
    # injection) -- a single numpy.default_rng(base_int("np")) vectorized
    # draw, NOT a per-row Python random loop. Both engines now compute the
    # identical null PATTERN (same seed, same N) so a null-prob column is
    # byte-identical V1<->V2, closing the pre-fix divergence the parity
    # oracle previously tolerated as fraction-only convergence.
    # Caller-supplied digests replace the file read inside for_column.
    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key,
        column_config=col,
        fallback_seed=seed,
        content_digests=content_digests,
    ).base_int("np")
    out = list(values)
    null_mask = np.random.default_rng(col_seed).random(len(out)) < null_prob
    for i, is_null in enumerate(null_mask):
        if is_null:
            out[i] = None
    return out


# Re-exported at the bottom (not the top) to avoid a real import cycle:
# `_plan_entry.py` imports `_generate_tables_from_config` FROM this module,
# so importing `_plan_entry` back at module-top here would import this
# not-yet-finished module from inside itself. By the time this line runs,
# `_generate_tables_from_config` above is already defined, so the cycle
# resolves cleanly. `generate_tables` stays importable from its documented
# path (`decoy_engine.generation.synthesize.generate_tables`,
# `decoy_engine/__init__.py`) even though the implementation moved.
from decoy_engine.generation._plan_entry import (  # noqa: E402, F401
    generate_tables,
    generate_tables_from_generation_plan,
    generate_tables_streamed_from_generation_plan,
)
