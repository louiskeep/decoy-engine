"""Table-from-schema synthesis (engine-v2 S6).

Produces synthetic tables from a generate-mode ``PipelineConfig``: for each generate
table (``generate_columns`` + ``row_count``, no source), build ``row_count`` rows,
each declared column filled by its per-column generator. This is the v2 analogue of
V1 ``DataGenerator`` (``decoy_engine.generators``); it is PARITY-FROZEN to V1 under a
fixed seed (Reading B) -- we reproduce V1 output, we do not extend it.

S6-ENG-1 landed the spine + the ``sequence`` generator. S6-ENG-2 adds parity-frozen
``categorical`` (and on the next sub-commits, ``faker`` / ``formula``); S6-ENG-3 adds
FK-aware generation (mint-a-pool); S6-ENG-4 the seed / derive-key determinism envelope.

Parity seeding uses V1's ``GenDeriveContext`` (``decoy_engine.generators.derivation``)
directly so the per-column derivation is byte-identical to V1 ``ColumnGenerator._column_ctx``
under the same ``derive_key`` (always ``None`` in ENG-2; ENG-4 wires the real key).

Thread-safety: all explicit RNG use here is instance-local (``random.Random(seed)``)
so two ``generate_tables`` calls in different threads do not corrupt each other's
draws. ``random.Random(s)`` produces the same sequence as ``random.seed(s)``, so
V1 byte-parity is preserved. Faker state is likewise isolated per worker: the
no-locale default instance is thread-local and the locale paths construct fresh
instances per call. ``Faker.seed_instance`` detaches the instance onto its own
``random.Random`` and re-seeds it, so a per-thread instance seeded per row
produces the exact sequence a reseeded shared instance did; output bytes are
unchanged. This replaces the QA-7 F1 (2026-06-01) ``_FAKER_CALL_LOCK`` that
serialized every seed_instance + provider_func pair across threads: isolation
makes the race structurally impossible instead of locked away.
"""

from __future__ import annotations

import random
import threading
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
from faker import Faker

from decoy_engine.generation.statistical import StatisticalSpec
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.internal.faker_setup import get_faker_providers, make_faker
from decoy_engine.transforms.derived_aggregate import generate_derived_aggregate_column

if TYPE_CHECKING:
    from collections.abc import Iterator

# QA-7 F5 (2026-06-01): seed default aligned with plan compiler's
# _normalize_job_seed default (0). Pre-fix _DEFAULT_SEED = 42 diverged
# from plan/_compile.py which defaults to 0 when global_settings.seed
# is absent. Same config, different effective seeds for generate vs
# mask. The number 42 was historical; zero is what the rest of the
# determinism layer assumes.
_DEFAULT_SEED = 0

# F-5 fix: Faker() construction loads locale data + registers ~200 providers
# (50-200ms), so the no-locale instance is cached per THREAD, not per process
# (memory scales with live worker threads). A shared instance re-seeded via
# `seed_instance` races under concurrent generate_tables calls; QA-7 F1 masked
# that by locking every seed_instance + provider_func pair. Per-thread
# instances remove the race and the lock. Seed-derived output is unchanged:
# per-row seed_instance detaches onto an instance-local random.Random, so a
# fresh instance seeded the same yields the same sequence as a reseeded shared
# one. (A custom provider drawing from process-global state like fake.unique
# was already non-deterministic and is outside the seeded-draw contract.)
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
) -> dict[str, pa.Table]:
    """The actual generation logic, over a plain config dict recovered
    from ``GenerationPlan.config_json``. Not part of the public surface
    (guide section 4.8: keep lower-level generation helpers private).

    ``statistical_specs`` is ``{(table_name, column_name): StatisticalSpec}``,
    already validated and pinned at compile time (guide section 4.7/4.8):
    the ``statistical`` dispatch below (``_statistical``) consumes it
    directly and never reopens a snapshot path. ``snapshot_index_for_
    column``/``snapshot_artifacts`` feed the fidelity gate the same pinned
    artifacts, keyed the same way.
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
    deps: dict[str, set[str]] = {}
    for name, t in generate_by_name.items():
        d: set[str] = set()
        for col in t["generate_columns"]:
            if col.get("type") == "reference":
                ref = col["reference_table"]
                # F-7 fix: validate reference_table resolves to a generate table.
                # PipelineConfig._reference_graph_valid catches this at validation
                # time, but generate_tables is documented to accept unvalidated
                # dicts (V1-parity callers). Surface a typed error here instead
                # of a downstream KeyError from `pools[ref_table]` in `_reference`.
                if ref not in generate_by_name:
                    raise ValueError(
                        f"table {name!r} column {col.get('name')!r}: "
                        f"reference_table {ref!r} is not a generate table"
                    )
                d.add(ref)
        deps[name] = d
    # Topo-sort parents-before-children (PipelineConfig._reference_graph_valid
    # already pinned this acyclic + every reference_table resolves to a generate
    # table). V1 iterates in declared order + warns on missing deps; v2 is
    # stricter without breaking parity for already-orderable configs.
    ordered = _topo_sort(deps)
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
        for col in gcols:
            data[col["name"]] = _generate_column(
                col,
                n,
                seed,
                derive_key,
                pools,
                instance_default_locale,
                data,
                table_name=name,
                statistical_specs=statistical_specs,
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

            fill_referenced_formula_columns(gcols, data, seed, derive_key)
        tbl = pa.table(data)
        # Generation-time fidelity warn-gate (deferred follow-up 5,
        # 2026-06-12): score statistical columns against their source
        # snapshot and warn below global_settings.fidelity_warn_threshold.
        # Warn-only; output bytes are untouched. Lazy import mirrors the
        # `_statistical` dispatch so non-statistical configs never pay it.
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
            )
        pools[name] = tbl
        out[name] = tbl
    return out


def _topo_sort(deps: dict[str, set[str]]) -> list[str]:
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
    here (guide section 4.8)."""
    kind = col.get("type")
    if kind == "sequence":
        values: list[Any] = _sequence(col, n)
    elif kind == "categorical":
        values = _categorical(col, n, seed, derive_key)
    elif kind == "faker":
        values = _faker(col, n, seed, derive_key, instance_default_locale)
    elif kind == "formula":
        values = _formula(col, n, seed, derive_key)
    elif kind == "reference":
        values = _reference(col, n, seed, derive_key, pools or {})
    elif kind == "statistical":
        values = _statistical(
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
            values = _windowed_date_generate(col, n, seed, generated or {})
        else:
            values = _group_key_generate(col, n, seed, generated or {})
    else:
        # The Literal on GenerateColumnConfig.type rejects anything outside this set
        # at validation; this branch is the defensive fallback for callers that
        # bypass validation (e.g. an unvalidated dict).
        raise ValueError(f"generate column {col.get('name')!r}: unexpected generator type {kind!r}")
    return _apply_null_probability(values, col, seed, derive_key)


def _sequence(col: dict[str, Any], n: int) -> list[str]:
    """Sequential string values, parity-frozen vs V1 ``_generate_sequence_column``
    (``columns.py:305-319``).

    V1 ALWAYS wraps every value through ``f"{prefix}{value_str}{suffix}"`` (S6-ENG-1
    gate finding M1: the ENG-1 spine returned ints when unformatted; corrected here).
    Returns strings in every configuration. ``pad_length`` zero-fills the numeric
    body; ``prefix`` / ``suffix`` wrap it.
    """
    start = int(col.get("start", 1))
    step = int(col.get("step", 1))
    prefix = str(col.get("prefix", ""))
    suffix = str(col.get("suffix", ""))
    pad = int(col.get("pad_length", 0))
    out: list[str] = []
    for i in range(n):
        value = start + i * step
        value_str = str(value).zfill(pad) if pad > 0 else str(value)
        out.append(f"{prefix}{value_str}{suffix}")
    return out


def _statistical(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any,
    generated: dict[str, list[Any]],
    table_name: str,
    statistical_specs: dict[tuple[str, str], StatisticalSpec],
) -> list[Any]:
    """WS3 statistical synthesis: sample from a distribution-snapshot/v1
    artifact (see generation/statistical for the methodology + privacy
    gate). ADDITIVE generator type -- the existing types stay
    parity-frozen to V1. `generated` carries the table's already-built
    columns so `condition_on` can read its conditioning sibling
    (declared-order sequential conditional sampling).

    DPS Scope B (guide section 4.8): the spec comes from the Plan's
    already-validated, already-pinned ``statistical_specs`` mapping, keyed
    by ``(table_name, column_name)`` -- this function never opens a
    snapshot path itself. The mapping is built once by ``generate_tables``
    from ``GenerationPlan.statistical_specs``, which `compile_plan` froze
    from the exact bytes it read at compile time (guide section 4.7),
    closing the TOCTOU window a raw ``load_spec(col)`` call would reopen.
    """
    from decoy_engine.generation.statistical import sample_column
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    col_name = str(col.get("name"))
    spec = statistical_specs.get((table_name, col_name))
    if spec is None:
        raise StatisticalSpecError(
            code="statistical_spec_not_pinned",
            message=(
                f"statistical column {col_name!r} in table {table_name!r} has no pinned "
                "spec in this Plan's GenerationPlan. This should be unreachable through "
                "compile_plan -- every type: statistical column that compiles "
                "successfully is pinned."
            ),
        )
    parent_values: list[Any] | None = None
    if spec.condition_on is not None:
        parent_values = generated.get(spec.condition_on)
        if parent_values is None:
            raise StatisticalSpecError(
                code="statistical_condition_column_unavailable",
                message=(
                    f"statistical column {spec.column!r} conditions on "
                    f"{spec.condition_on!r}, which is not generated yet. Declare "
                    f"{spec.condition_on!r} BEFORE {spec.column!r} in generate_columns."
                ),
            )
    # Reuse the Plan's already-pinned digest (guide section 4.7/4.8, defect
    # F4) instead of letting the fingerprint step reopen snapshot_file.
    digest = f"sha256:{spec.snapshot_digest}" if spec.snapshot_digest else None
    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key,
        column_config=col,
        fallback_seed=seed,
        snapshot_content_digest=digest,
    ).base_int("np")
    return sample_column(spec, n, col_seed=col_seed, parent_values=parent_values)


def _derived_generate(
    col: dict[str, Any],
    n: int,
    generated: dict[str, list[Any]],
) -> list[Any]:
    """Evaluate a closed-grammar derived expression against already-generated siblings.

    Processes rows inline in declared order. Row context is built from columns in
    ``generated`` (those declared before this one). A sibling declared AFTER this
    column will be absent from the row context and raise at evaluation time;
    check_derived_column_refs validates ref existence at plan-compile time. The
    declared-order constraint mirrors the ``statistical / condition_on`` pattern.
    Deterministic by construction: same generated snapshot -> same output, no RNG.
    """
    from decoy_engine.transforms.derived import DerivedConfig, apply_derived

    config = DerivedConfig.from_dict(
        {
            "expression": col.get("expression", ""),
            "bounds": col.get("bounds"),
            "null_propagation": col.get("null_propagation", "explicit_null"),
        }
    )
    col_name = col.get("name", "?")
    out: list[Any] = []
    for i in range(n):
        row_ctx = {k: vals[i] for k, vals in generated.items() if i < len(vals)}
        out.append(apply_derived(config, row_ctx, column=col_name, row_index=i))
    return out


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
) -> list[Any]:
    """Faker-driven values, parity-frozen vs V1 ``_generate_faker_column``
    (``columns.py:205-276``).

    Pattern (mirror V1): pick the Faker instance (fresh per-locale when ``locale``
    is set, otherwise a shared instance), look up the provider by ``faker_type``
    (default ``"word"``, fall back to ``"word"`` for unknown types), then per row
    seed ``random`` AND ``faker_inst.seed_instance`` with ``col_seed + i`` and call
    ``provider_func(**faker_kwargs)``. The per-row seed_instance override means the
    initial instance seed does not affect output -- parity holds independent of how
    the instance was constructed.

    ``faker_kwargs`` is optional; non-dict values are dropped (matches V1's silent
    drop, ``columns.py:253-259``).
    """
    faker_type = col.get("faker_type", "word")
    locale = col.get("locale")
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
    providers = get_faker_providers(faker_inst)
    provider_func = providers.get(faker_type) or providers["word"]
    raw_kwargs = col.get("faker_kwargs") or {}
    faker_kwargs = raw_kwargs if isinstance(raw_kwargs, dict) else {}
    gen_ctx = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    )
    out: list[Any] = []
    # No lock: every path above yields an instance no other thread touches
    # (thread-local default, or a fresh make_faker construction), so the
    # seed_instance + provider_func pair cannot race. The QA-7 F1/C1 lock
    # existed only because the default instance was a process-wide singleton.
    if pre_seed is not None:
        faker_inst.seed_instance(pre_seed)
    for i in range(n):
        faker_inst.seed_instance(gen_ctx.row_int("faker", i))
        out.append(provider_func(**faker_kwargs))
    return out


def _apply_null_probability(
    values: list[Any], col: dict[str, Any], seed: int, derive_key: Any = None
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
    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    ).base_int("np")
    out = list(values)
    null_mask = np.random.default_rng(col_seed).random(len(out)) < null_prob
    for i, is_null in enumerate(null_mask):
        if is_null:
            out[i] = None
    return out


def _formula(col: dict[str, Any], n: int, seed: int, derive_key: Any = None) -> list[Any]:
    """Python-expression-driven values, parity-frozen vs V1
    ``_generate_formula_column`` (``columns.py:974+``).

    V1's structure (mirrored here):
      - empty ``formula`` -> None series (we just return Nones).
      - ``references: [...]`` set -> return ``[None] * n`` placeholders that
        ``generate_tables``'s ``_referenced_formula.fill_referenced_formula_columns``
        post-pass overwrites once every sibling column is finalized (the
        per-column loop can't read siblings that may not be generated yet).
        No warning is emitted here: the column is filled, not dropped.
      - else (inline path) -> per-row safe_eval with row-seeded ``random`` /
        ``faker`` scope.

    For the inline path we DELEGATE to V1 ``ColumnGenerator._eval_formula_inline``
    (Reading B: pragmatic guaranteed parity; the eval scope is generic Python
    expression machinery + Faker helpers, not v1-specific). A v2-native rewrite
    that lifts the eval scope into ``generation/`` can land alongside S9 v1
    removal. The delegation is the ENG-2 commit-1 of formula; it does not block
    ENG-2's Reading-B exit gate (parity tests are byte-identical).
    """
    formula = col.get("formula") or ""
    references = col.get("references") or []
    if not formula:
        return [None] * n
    if references:
        # Cross-column formula: return None placeholders that the
        # generate_tables post-pass (_referenced_formula.fill_referenced_formula_columns)
        # overwrites once every sibling column is finalized. The per-column
        # loop can't read siblings here, so no eval happens at dispatch.
        return [None] * n
    from decoy_engine.generators.columns import ColumnGenerator

    cg = ColumnGenerator(seed=seed, derive_key=derive_key)
    series = cg._eval_formula_inline(n, formula, col.get("name", "unnamed_column"), col)
    return series.tolist()


def _reference(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any = None,
    pools: dict[str, pa.Table] | None = None,
) -> list[Any]:
    """FK / mint-a-pool: sample values from a parent table's already-generated key
    column. Parity-frozen vs V1 ``_generate_reference_column`` (``columns.py:758-865``).

    Pattern (mirror V1):
      - Read parent values from ``pools[reference_table].column(reference_column)``.
      - INSERTION-ORDER unique + dropna (V1 uses pandas ``Series.dropna().unique()``,
        which is insertion-order; a naive ``set()`` would break parity).
      - Empty parent pool -> ``[None] * n`` (V1 warns + returns Nones).
      - Reseed ``random`` from the per-column seed (V1's exact pattern -- the
        ``categorical`` generator does the same).
      - Dispatch by ``distribution``: ``random`` (random.choice), ``sequential``
        (parent_vals[i % len]), ``weighted`` (random.choices with weights;
        size-mismatch falls back to None for uniform). Unknown -> random
        (V1 warns; the v2 falls through silently, values match).
      - Optional cardinality repair via V1 ``_apply_cardinality_bounds`` (~150 LoC
        repair algorithm) -- DELEGATED to V1 the same way ``_formula`` delegates
        ``_eval_formula_inline``: Reading B pragmatic guaranteed parity; the
        repair is generic set-cover-like logic, not v1-specific; v2-native rewrite
        lifts at S9 alongside v1 removal.

    The PipelineConfig ``_reference_graph_valid`` validator + topo-sort in
    ``generate_tables`` guarantee the parent is in ``pools`` by the time we get
    here; this function does not re-check existence.
    """
    pools = pools or {}
    ref_table = col["reference_table"]
    ref_column = col["reference_column"]
    distribution = col.get("distribution", "random")
    min_per = int(col.get("min_per_parent") or 0)
    max_per = int(col.get("max_per_parent") or 0)

    parent_tbl = pools[ref_table]
    raw_vals = parent_tbl.column(ref_column).to_pylist()
    # Insertion-order unique + drop None, matching V1 `dropna().unique()` on a
    # pandas Series. A naive set() would lose order -> different random.choice
    # output for the same seed.
    seen: set = set()
    ref_vals: list[Any] = []
    for v in raw_vals:
        if v is None or v in seen:
            continue
        seen.add(v)
        ref_vals.append(v)

    if not ref_vals:
        return [None] * n

    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    ).base_int("py")
    # Instance-local Random: parity-preserving, thread-safe (F1 fix).
    rng = random.Random(col_seed)

    values: list[Any] = []
    for i in range(n):
        if distribution == "random":
            values.append(rng.choice(ref_vals))
        elif distribution == "sequential":
            values.append(ref_vals[i % len(ref_vals)])
        elif distribution == "weighted":
            weights = col.get("weights")
            if not weights or len(weights) != len(ref_vals):
                weights = None  # V1: size-mismatch -> uniform
            values.append(rng.choices(ref_vals, weights=weights, k=1)[0])
        else:
            # V1 unknown -> warn + random; the v2 falls through silently (parity
            # in values, not in log lines).
            values.append(rng.choice(ref_vals))

    if min_per > 0 or max_per > 0:
        from decoy_engine.generators.columns import ColumnGenerator

        # QA-1 H6 carry (2026-06-01): pass the column-scoped rng so the
        # repair's shuffle/choices stay deterministic without touching
        # module-global random. The local `rng` above is column-scoped
        # via col_seed.
        cg = ColumnGenerator(seed=seed, derive_key=derive_key)
        values = cg._apply_cardinality_bounds(
            values,
            ref_vals,
            min_per,
            max_per,
            rng=rng,
        )
    return values


# Re-exported at the bottom (not the top) to avoid a real import cycle:
# `_plan_entry.py` imports `_generate_tables_from_config` FROM this module,
# so importing `_plan_entry` back at module-top here would import this
# not-yet-finished module from inside itself. By the time this line runs,
# `_generate_tables_from_config` above is already defined, so the cycle
# resolves cleanly. `generate_tables` stays importable from its documented
# path (`decoy_engine.generation.synthesize.generate_tables`,
# `decoy_engine/__init__.py`) even though the implementation moved.
from decoy_engine.generation._plan_entry import generate_tables as generate_tables  # noqa: E402
