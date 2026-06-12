"""Table-from-schema synthesis (engine-v2 S6).

Produces synthetic tables from a generate-mode ``PipelineConfig``: for each generate
table (``generate_columns`` + ``row_count``, no source), build ``row_count`` rows,
each declared column filled by its per-column generator. This is the v2 analogue of
V1 ``DataGenerator`` (``decoy_engine.generators``); it is PARITY-FROZEN to V1 under a
fixed seed (Reading B) -- we reproduce V1 output, we do not extend it.

S6-ENG-1 landed the spine + the ``sequence`` generator. S6-ENG-2 adds parity-frozen
``categorical`` (and on the next sub-commits, ``faker`` / ``formula``); S6-ENG-3 adds
FK-aware generation (mint-a-pool); S6-ENG-4 the seed / derive-key determinism envelope.

Parity seeding uses V1's ``synthetic_column_seed`` (``decoy_engine.generators.derivation``)
directly so the per-column seed is byte-identical to V1 ``ColumnGenerator._column_seed``
under the same ``derive_key`` (always ``None`` in ENG-2; ENG-4 wires the real key).

Thread-safety: all explicit RNG use here is instance-local (``random.Random(seed)``)
so two ``generate_tables`` calls in different threads do not corrupt each other's
draws. ``random.Random(s)`` produces the same sequence as ``random.seed(s)``, so
V1 byte-parity is preserved. The Faker dependency mutates module-level
``random`` state via ``seed_instance`` (Faker library limitation): QA-7 F1
(2026-06-01) added an intra-process lock (``_FAKER_CALL_LOCK``) around the
seed_instance + provider_func call so concurrent generate_tables calls cannot
corrupt each other's seed state. V2.1 throughput optimization: replace the
shared cached instance with a per-call fresh Faker to remove the lock entirely.
"""

from __future__ import annotations

import random
import threading
from typing import Any

import pyarrow as pa
from faker import Faker

from decoy_engine.generators.derivation import synthetic_column_seed
from decoy_engine.internal.faker_setup import get_faker_providers, make_faker

# QA-7 F5 (2026-06-01): seed default aligned with plan compiler's
# _normalize_job_seed default (0). Pre-fix _DEFAULT_SEED = 42 diverged
# from plan/_compile.py which defaults to 0 when global_settings.seed
# is absent. Same config, different effective seeds for generate vs
# mask. The number 42 was historical; zero is what the rest of the
# determinism layer assumes.
_DEFAULT_SEED = 0

# F-5 fix: Faker() construction loads locale data + registers ~200 providers
# (50-200ms per construction). The instance is re-seeded per call via
# `seed_instance`, so caching one shared no-locale instance is safe + cheap.
_DEFAULT_FAKER: Faker | None = None
_DEFAULT_FAKER_LOCK = threading.Lock()

# QA-7 F1 (2026-06-01, CRITICAL determinism): Faker.seed_instance() mutates
# module-level `random` state internally (Faker library limitation, all
# versions through 2026). Two concurrent generate_tables calls sharing the
# `_DEFAULT_FAKER` singleton (or any cached locale instance) will clobber
# each other's seed between seed_instance + provider_func: thread A seeds,
# then thread B seeds before thread A draws, and thread A's row is now
# derived from B's seed. Violates the same-seed -> same-output contract.
#
# Fix: serialize the seed_instance + provider_func pair across threads
# with a process-level lock. Acceptable throughput cost for V1 (single-
# worker generation); a per-call fresh Faker instance is the throughput-
# friendly alternative scoped for V2.1 when concurrent generation lands.
# Intra-process scope: serializes threads within a single Python process;
# does NOT serialize across separate worker processes (each has its own
# Faker singleton + own random state, no cross-process interference).
_FAKER_CALL_LOCK = threading.Lock()


def _get_default_faker() -> Faker:
    global _DEFAULT_FAKER
    if _DEFAULT_FAKER is None:
        with _DEFAULT_FAKER_LOCK:
            if _DEFAULT_FAKER is None:
                _DEFAULT_FAKER = Faker()
    return _DEFAULT_FAKER


def generate_tables(
    config: dict[str, Any],
    derive_key: Any = None,
    instance_default_locale: str | None = None,
) -> dict[str, pa.Table]:
    """Build one Arrow table per generate table in ``config``.

    ``config`` is a validated, ``model_dump``-ed generate-mode ``PipelineConfig``.
    Returns ``{table_name: pa.Table}`` for every table that declares
    ``generate_columns`` (mask tables, if any, are skipped). ``derive_key`` is the
    pipeline-bound key resolver V1 ``ColumnGenerator`` threads -- ALWAYS ``None`` in
    S6-ENG-2 (parity-tested against V1 seed-only path); S6-ENG-4 wires the real
    ``pipeline_derive_key`` so generation + masking share one determinism envelope.

    The platform run path (S6-PLT) writes these through the same ``write_v2_outputs``
    + ``build_v2_target_node_runs`` path the mask spine uses.
    """
    # QA-7 F8 (2026-06-01): typed error for non-numeric seed. Pre-fix
    # int("abc") leaked a bare ValueError with cryptic message; matches
    # the plan compiler's behavior post-QA-3 F1.
    raw_seed = (config.get("global_settings") or {}).get("seed", _DEFAULT_SEED)
    try:
        seed = int(raw_seed)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"generate_tables: global_settings.seed must be an integer; "
            f"got {type(raw_seed).__name__} {raw_seed!r}"
        ) from exc
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
                col, n, seed, derive_key, pools, instance_default_locale, data
            )
        tbl = pa.table(data)
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
    from collections.abc import Iterator as _IteratorRT

    result: list[str] = []
    visited: set[str] = set()

    for start in deps:
        if start in visited or start not in deps:
            continue
        stack: list[tuple[str, _IteratorRT[str]]] = [(start, iter(deps.get(start, ())))]
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
) -> list[Any]:
    """Dispatch a generate column to its generator by ``type`` (mirrors V1
    ``ColumnGenerator.generators``), then apply the V1 ``null_probability``
    post-process (V1 ``generate_column`` lines 174-187) so the same fraction of
    rows is nulled at byte-identical row positions. ``pools`` carries already-
    generated parent tables for ``reference`` columns (S6-ENG-3 mint-a-pool).
    ``instance_default_locale`` (S6-ENG-4 M1) flows the platform's
    ``AppSettings.default_faker_locale`` into the shared-Faker path for the
    no-per-column-locale branch of ``_faker``, mirroring V1 ``ColumnGenerator``."""
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
        values = _statistical(col, n, seed, derive_key, generated or {})
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
) -> list[Any]:
    """WS3 statistical synthesis: sample from a distribution-snapshot/v1
    artifact (see generation/statistical for the methodology + privacy
    gate). ADDITIVE generator type -- the existing types stay
    parity-frozen to V1. `generated` carries the table's already-built
    columns so `condition_on` can read its conditioning sibling
    (declared-order sequential conditional sampling)."""
    from decoy_engine.generation.statistical import load_spec, sample_column
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    spec = load_spec(col)
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
    col_seed = synthetic_column_seed(derive_key=derive_key, column_config=col, fallback_seed=seed)
    return sample_column(spec, n, col_seed=col_seed, parent_values=parent_values)


def _categorical(col: dict[str, Any], n: int, seed: int, derive_key: Any = None) -> list[Any]:
    """Weighted / uniform random choice over ``categories``, parity-frozen vs V1
    ``_generate_categorical_column`` (``columns.py:321-353``).

    V1 reseeds ``random`` from the column seed (so output is stable across runs +
    order-independent across columns when keyed), then ``random.choices(categories,
    weights=weights, k=num_rows)``. ``weights`` is optional; when omitted the choice
    is uniform. We reuse V1 ``synthetic_column_seed`` for the per-column seed (Dennis
    S6-ENG-2 plan: import V1's helper, do not reinvent), so seed-only output is
    byte-identical to V1's under the same ``seed`` + ``derive_key=None``.
    """
    cats = col.get("categories", ["Category A", "Category B"])
    weights = col.get("weights")  # optional; None -> uniform
    col_seed = synthetic_column_seed(derive_key=derive_key, column_config=col, fallback_seed=seed)
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
        # F-5 fix: cache the no-locale instance at module level. Per-row
        # seed_instance below overrides the initial seed, so sharing is safe.
        faker_inst = _get_default_faker()
        pre_seed = seed
    providers = get_faker_providers(faker_inst)
    provider_func = providers.get(faker_type) or providers["word"]
    raw_kwargs = col.get("faker_kwargs") or {}
    faker_kwargs = raw_kwargs if isinstance(raw_kwargs, dict) else {}
    col_seed = synthetic_column_seed(derive_key=derive_key, column_config=col, fallback_seed=seed)
    out: list[Any] = []
    # QA-7 F1 + C1 (2026-06-01): both seed_instance call sites are in
    # the critical section. The pre-loop seed_instance(seed) used to
    # live OUTSIDE the lock (Dennis QA-7 gate carry C1) which left a
    # window where thread B's pre-seed could race thread A's row-seed.
    # Now the pre-seed + per-row seeds + provider_func calls are all
    # inside one lock acquisition; different-seed concurrency is also
    # safe.
    with _FAKER_CALL_LOCK:
        if pre_seed is not None:
            faker_inst.seed_instance(pre_seed)
        for i in range(n):
            row_seed = col_seed + i
            faker_inst.seed_instance(row_seed)
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
    # synthetic_column_seed so two columns with different strategies +
    # the same name no longer share a null mask. Pre-fix this used
    # only `{"name": col["name"]}` to mirror V1; V1 has been
    # updated to also pass column_config (qa-1 step 3) so V1 and V2
    # stay byte-identical AND the null-mask collision is closed.
    col_seed = synthetic_column_seed(derive_key=derive_key, column_config=col, fallback_seed=seed)
    out = list(values)
    # Per-row reseed preserves V1 byte-parity (V1 reseeds the global RNG per
    # row at columns.py:183). Switched to instance-local Random so the
    # mutation no longer leaks to module-global state. F6 fix.
    #
    # QA 2026-05-31 session2 F3 (HIGH perf) closure: allocate the Random
    # ONCE + reseed in place each row. Previously we allocated a new
    # random.Random(col_seed + i) per row; each allocation initializes
    # the 624-word Mersenne Twister state (~2.5 KB write), adding ~3-5x
    # overhead on large tables. rng.seed(s) on a reused instance runs
    # the same mt_init_genrand(s) so the first draw is byte-identical
    # to a fresh Random(s) -- V1 byte-parity preserved.
    rng = random.Random()
    for i in range(len(out)):
        rng.seed(col_seed + i)
        if rng.random() < null_prob:
            out[i] = None
    return out


def _formula(col: dict[str, Any], n: int, seed: int, derive_key: Any = None) -> list[Any]:
    """Python-expression-driven values, parity-frozen vs V1
    ``_generate_formula_column`` (``columns.py:974+``).

    V1's structure (mirrored here):
      - empty ``formula`` -> warn + None series (we just return Nones).
      - ``references: [...]`` set -> DEFER to V1's post-pass
        (``DataGenerator._process_referenced_formulas``); the per-column generator
        returns ``[None] * n`` placeholders. v2 returns the same placeholders;
        cross-column-reference formulas land in a later sprint (alongside the
        v2 post-pass plumbing).
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
        # QA-7 F7 (2026-06-01): emit a warning when references is set.
        # Pre-fix the cross-column-reference path silently returned
        # all-None placeholders with no signal to the operator. The
        # column landed in the output as nulls + no warning anywhere.
        # The V2 post-pass for cross-column formulas lands in a later
        # sprint; until then warn loud.
        import warnings

        warnings.warn(
            f"column {col.get('name', 'unnamed_column')!r}: formula with "
            f"`references` is not yet supported in v2 generation "
            f"(cross-column formulas land in a later sprint). Returning "
            "nulls for this column.",
            stacklevel=4,
        )
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

    col_seed = synthetic_column_seed(derive_key=derive_key, column_config=col, fallback_seed=seed)
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
