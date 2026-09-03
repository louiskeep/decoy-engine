"""FK self-masking gate for chunked execution (Option 1).

Extracted to keep _chunked.py under the orchestration LOC cap.
See docs/relationships-memory-scaling.md §2 for the full design and
_chunked.py module docstring for the gate conditions summary.

`fk_passthrough_columns_for_table` / `reject_lossy_chunked_fk_passthrough`
close MEDIUM #4 (DE-10 rework follow-up, 2026-07-13): `run_mask_pipeline_
chunked` (`_chunked.py`) always threads an EMPTY `RelationshipGraph` into
the pandas adapter (self-masking has no parent-map join), so `_fk_keys.
to_pandas_fk_safe`'s ingestion protection -- keyed off that same runtime
graph -- protects NOTHING on this route. Every OTHER chunk-safe strategy
(hash, fpe, redact, truncate, text_redact, date_shift, bucketize, top_code,
text_mask) re-derives its output rather than preserving the raw key, so an unprotected
float64-on-null ingestion widening never survives to the output for them --
but `passthrough` (identity) preserves the raw key verbatim, so a null-bearing
`passthrough` FK column carrying a value beyond `2**53` silently rounds
through this route's unprotected ingestion, exactly like the pre-DE-10
full-frame/sequential bug. These two functions add a TARGETED runtime guard
(not a blanket null-bearing-int reject, which would also catch legitimate
small-int passthrough jobs): reject only when a `passthrough` FK column here
is null-bearing AND carries a value beyond `2**53`, with the same
`out_of_core_fk_key_dtype_unsupported` code every other unrepresentable-key
shape raises. This guard is INDEPENDENT of `gate_fk_child_edges`'s strategy
allowlist below: it reads FK-participant columns straight off `config.
relationships`/`config.tables`, both parent- and child-role, so it still
protects a `passthrough` PARENT key column even after the 2026-09-02
cascade-safety fix narrows the CHILD-edge admission gate to `hash` only (a
`passthrough` FK CHILD column is now rejected earlier still, at that gate,
before this guard ever runs for it).
"""

from __future__ import annotations

import re
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._chunked_fk_dtype_safety import declared_fk_hash_dtype_is_safe
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.plan._errors import PlanCompileError

# Mirrors `_fk_keys._EXACT_FLOAT_INT_BOUND`: IEEE-754 double precision (53-bit
# mantissa) is the largest magnitude every integer up to and including it can
# round-trip through float64 exactly -- the bound `reject_lossy_chunked_fk_
# passthrough` uses to decide whether an unprotected ingestion float64-on-null
# widening would actually lose precision for this column's data.
_EXACT_FLOAT_INT_BOUND = 2**53

# Matches a decimal dtype string that carries explicit (precision, scale), in
# either PyArrow's own `str()` form (`str(pa.decimal128(2, 1))` ==
# "decimal128(2, 1)", `str(pa.decimal256(40, 2))` == "decimal256(40, 2)" --
# confirmed empirically) or the SQL-style form an operator might hand-write in
# config ("decimal(10, 2)" / "numeric(10, 2)", no width suffix). Captures
# (precision, scale) so two decimal declarations can be compared on that pair,
# not folded into one bare "decimal" family regardless of scale.
# Sentinel family for an UNPROVABLE decimal declaration (bare `decimal`/`numeric`
# with no precision+scale). The per-chunk runtime guard fails closed on any
# column whose DECLARED family is this sentinel: decimal scale changes the
# canonical bytes and the chunked route cannot verify parent/child scale
# agreement, so a bare decimal FK key cannot preserve RI.
_DECIMAL_UNPROVABLE_FAMILY = "decimal:unprovable"
# Scale may be NEGATIVE (Arrow/Parquet-legal, e.g. `decimal128(4, -1)`), so the
# scale group accepts an optional leading minus; precision is always >= 1.
# Covers every PyArrow decimal width (decimal32/64/128/256, PyArrow 24+) plus a
# SQL-style hand-written `decimal(P, S)`/`numeric(P, S)`. Only the SCALE (group 2,
# may be negative) is used for the RI family; precision (group 1) is captured but
# not folded in (see `_dtype_family`).
_DECIMAL_PRECISION_SCALE_RE = re.compile(
    r"^(?:decimal(?:32|64|128|256)?|numeric)\((\d+)\s*,\s*(-?\d+)\)$"
)

# Value-keyed strategies that produce the same output per (seed, namespace, value)
# regardless of row position or chunk boundary. Defined here and re-exported by
# _chunked.py so that the chunked-FK gate and the general compatibility check share
# one authoritative set without a circular import.
CHUNK_SAFE_STRATEGIES: frozenset[str] = frozenset(
    {
        "hash",
        "fpe",
        "redact",
        "truncate",
        "text_redact",
        "date_shift",
        "bucketize",
        "top_code",
        "passthrough",
        # Phase 4 slice 3 (2026-09-01): value-own-keyed per-cell deterministic
        # span masking; NOT namespace-requiring (below).
        "text_mask",
    }
)

# Subset of CHUNK_SAFE_STRATEGIES whose handlers call derive(job_seed, namespace, ...)
# and raise at execution when plan.namespace is None. Each raises a typed error code
# when the namespace is absent:
#   hash       -- hash_requires_namespace
#   fpe        -- fpe_requires_namespace
#   date_shift -- date_shift_requires_namespace
# The remaining CHUNK_SAFE strategies (redact, truncate, text_redact, bucketize,
# top_code, passthrough, text_mask) do not read plan.namespace and produce
# byte-identical output regardless of whether a namespace is declared; the
# namespace sub-checks in the FK gate are skipped for them. text_mask keys its
# spans on ctx.mask_key globally (RFC 2104 HMAC per span), not a per-column
# namespace, so it belongs in this group despite its richer dispatch.
NAMESPACE_REQUIRING_STRATEGIES: frozenset[str] = frozenset({"hash", "fpe", "date_shift"})

# Strategies whose masked output does NOT depend on the key's value (only on
# its null-ness), so parent and child provably mask to identical values for the
# same logical key REGARDLESS of dtype. `redact` emits a constant replacement
# for every non-null input (condition (e) already forces an identical
# `redact_with` on both sides); a null stays null on both. Every OTHER
# chunk-safe strategy is value-sensitive: its output depends on the raw key's
# representation, which differs across dtype families for equal logical keys
# (int 1 vs float 1.0 stringify/canonicalize/FPE to different bytes), so it can
# only be proven safe when both sides declare the same dtype family.
DTYPE_INVARIANT_STRATEGIES: frozenset[str] = frozenset({"redact"})


def _dtype_family(dtype: str) -> str:
    """Coarse dtype family for the chunked-FK dtype gate (condition (f)).

    Widths within a family (int32 vs int64) reproduce identical masked bytes
    for equal values -- the kernel canonicalizer encodes any-width integers
    as a length-prefixed minimal two's complement form regardless of storage
    width (kernel/_canonicalize.py) -- so only the family needs to agree, not
    the exact dtype string. Unrecognized strings pass through lowercased and
    unmodified, so an unknown dtype only ever matches another occurrence of
    that exact same string.

    `decimal`/`numeric` is SCALE-AWARE (Codex gpt-5.6-sol HIGH-1, 2026-07-14):
    unlike int/float, a decimal's canonical bytes depend on its declared
    SCALE, not just its family -- `str(1.0)` for `decimal128(2, 1)` and
    `str(1.00)` for `decimal128(3, 2)` canonicalize to different byte strings
    for the "same" logical value under the hash/truncate/fpe kernels, so two
    decimal columns are only provably interchangeable when they share the same
    SCALE. Precision is IRRELEVANT to RI: the canonicalizer encodes by
    (unscaled_int, scale), so `decimal128(2, 1)` and `decimal128(3, 1)` mask an
    equal key to identical bytes -- folding precision in would over-reject a
    healthy scale-matched / precision-mismatched FK pair. A dtype string that
    carries explicit precision+scale (any PyArrow width `str()` form
    `decimal{32,64,128,256}(P, S)`, or the SQL-style "decimal(P, S)"/
    "numeric(P, S)" an operator might hand-write) parses to the scale-keyed
    family `"decimal(scale=S)"`, so a scale-matched declaration on both sides of
    an FK edge is admitted (regardless of precision or width), and a genuine
    scale MISmatch (parent scale 1 vs child scale 2) is rejected at the COMPILE
    gate the same way an int/float mismatch is (different family strings).

    A BARE `"decimal"` or `"numeric"` declaration (no scale) is UNPROVABLE --
    the chunked route only ever sees one table's dtype at a time, so there is
    no runtime check that can recover the missing scale -- and returns the
    distinct sentinel family `_DECIMAL_UNPROVABLE_FAMILY`. Two bare declarations
    on both sides of an edge compare EQUAL at the compile gate (same sentinel)
    and are admitted there; the rejection happens one layer down, at the
    per-chunk runtime guard
    (`_chunked_fk_dtype.reject_mismatched_chunked_fk_declared_dtype`), which
    fails closed on ANY column whose DECLARED family is the sentinel -- it does
    NOT depend on the real data's family, so it holds even for the negative-scale
    real decimals that the scale regex now parses concretely. Real Arrow decimals
    (any precision/scale, including negative scale) resolve to a concrete
    `"decimal(P,S)"` family, so a correctly-scaled declaration matches its real
    data and is admitted, while a genuine scale mismatch is rejected.
    """
    family = dtype.strip().lower()
    if family.startswith(("int", "uint")):
        return "int"
    if family.startswith(("float", "double")):
        return "float"
    if family.startswith(("decimal", "numeric")):
        match = _DECIMAL_PRECISION_SCALE_RE.match(family)
        if match is None:
            return _DECIMAL_UNPROVABLE_FAMILY
        # RI keys on SCALE ONLY: the canonicalizer encodes a decimal by its
        # (unscaled_int, scale), so two decimals of the same scale produce
        # identical masked bytes for equal logical keys regardless of precision
        # (verified: decimal128(2,1) and decimal128(3,1) mask 1.0 to the same
        # hash). Precision only bounds range, so folding it in would over-reject
        # a healthy scale-matched / precision-mismatched FK pair.
        scale = match.group(2)
        return f"decimal(scale={scale})"
    if family.startswith("bool"):
        return "bool"
    if family.startswith(("str", "string", "object", "utf8", "large_string")):
        return "string"
    # date vs timestamp/datetime are DISTINCT families: date32 and timestamp
    # canonicalize to different bytes for the same instant, so a declared-date /
    # real-timestamp FK key voids RI. Check timestamp/datetime BEFORE date --
    # "datetime" is a prefix-superset of "date".
    if family.startswith(("timestamp", "datetime")):
        return "timestamp"
    if family.startswith("date"):
        return "date"
    if family.startswith(("bytes", "binary", "large_binary", "fixed_size_binary")):
        return "bytes"
    return family


def _col_index_from_config(config: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Build a (table_name, col_name) -> col_entry lookup from config.tables."""
    idx: dict[tuple[str, str], dict[str, Any]] = {}
    for tbl in config.get("tables") or []:
        if not isinstance(tbl, dict):
            continue
        tbl_name = tbl.get("name")
        if not isinstance(tbl_name, str):
            continue
        for col in tbl.get("columns") or []:
            if isinstance(col, dict):
                col_name = col.get("name")
                if isinstance(col_name, str):
                    idx[(tbl_name, col_name)] = col
    return idx


def _child_endpoints_from_config(
    config: dict[str, Any],
) -> set[tuple[str, tuple[str, ...]]]:
    """Every `(child_table, child_columns)` key node across ALL relationships
    in `config`, regardless of which table is currently being gated.

    Predicate 8 needs this global view -- a parent key node is rejected when
    IT is itself a child endpoint SOMEWHERE in the config (a same-key cascade
    A -> B -> C), not just within the edge(s) into the table `gate_fk_child_
    edges` was called for. Precomputing it once here (rather than checking
    per-edge against only the CURRENT relationship entry) makes the predicate
    order-independent: it gives the same answer no matter which table in the
    chain gets gated first.
    """
    endpoints: set[tuple[str, tuple[str, ...]]] = set()
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict):
                continue
            child_table = child_info.get("table")
            child_cols = child_info.get("columns") or []
            if isinstance(child_table, str):
                endpoints.add((child_table, tuple(str(c) for c in child_cols)))
    return endpoints


def gate_fk_child_edges(config: dict[str, Any], *, table: str) -> None:
    """Gate each FK edge where `table` is the child against the self-mask conditions.

    Walks config.relationships and checks every edge whose child_table matches
    `table`. An edge is ADMITTED only when every condition below holds:

    (a) Parent key strategy is EXACTLY `hash`. Every other CHUNK_SAFE_STRATEGIES
        member (passthrough, fpe, redact, truncate, text_redact, date_shift,
        bucketize, top_code, text_mask) is a strategy x representation x
        substrate hole for FK self-masking specifically (2026-09-02 cascade-
        safety plan): each either depends on cascade/`when` state, whole-column
        state, or value representation/execution substrate in a way that makes
        the child's independent self-mask diverge from the join oracle's output
        for the SAME logical key. hash is the one strategy proven cross-adapter
        byte-identical (via the shared kernel canonicalizer), and only for the
        exact dtype set predicate 12 (below) admits.
    (b) Child FK column explicitly declares the same value-keyed strategy.
    (c) ONLY when parent strategy is in NAMESPACE_REQUIRING_STRATEGIES (hash,
        fpe, date_shift): child FK column explicitly declares the same namespace
        as the parent COLUMN. The parent-column namespace is the authoritative
        masking key: ColumnSeed.namespace = col_entry.get("namespace")
        (_seed_envelope.py:260). A config whose edge namespace disagrees with the
        parent-column namespace is rejected (chunked_fk_parent_namespace_mismatch)
        because the edge namespace is a label only and is never used to derive a
        masked value. A parent column with no namespace is rejected fail-closed
        (chunked_fk_parent_namespace_missing), consistent with the per-strategy
        namespace-required error at execution time.
        For namespace-agnostic strategies (redact, truncate, text_redact,
        bucketize, top_code, passthrough, text_mask), namespace sub-checks are skipped
        entirely:
        their output is pure(value, config) and byte-identical regardless of
        whether a namespace is declared. (After (a)'s narrowing, only hash ever
        reaches this point, so these strategies never actually exercise this
        sub-check on the FK route anymore -- retained for the general
        namespace-requiring-strategy contract this predicate documents.)
    (d) orphan_policy is 'remap'.
    (8) The parent KEY NODE `(parent_table, (parent_col,))` is not ITSELF an FK
        child endpoint anywhere in `config.relationships` (e.g. a same-key
        chain A -> B -> C). If it were, the oracle FK-RESOLVES the parent key
        (possibly to None on an upstream cascade failure) instead of masking it
        by its declared strategy, so self-masking the grandchild would compute
        `hash(value)` where the oracle computes `hash(resolved_parent_value)` --
        a silent divergence. Checked against every `(child_table, child_columns)`
        endpoint across the WHOLE config (not just edges into `table`), so the
        result is independent of which table happens to be gated first. A
        distinct-column self-FK on the same table (`employees.id ->
        employees.manager_id`) is NOT caught by this -- the key NODE
        `(employees, ("id",))` differs from `(employees, ("manager_id",))`, so
        `id` stays a root key even though `manager_id` on the same table is a
        child column.
    (9/10) Neither the parent nor the child FK key column has an EFFECTIVE
        `when` (a non-blank string, mirroring the compiler's own normalization
        at `plan/_seed_envelope.py:245`). A row-gated key is masked
        conditionally by the oracle (some rows stay raw); self-masking ignores
        `when` entirely and always masks, so a `when`-bearing FK key silently
        diverges on the gated rows.
    (11) Neither FK endpoint declares a `provider`. A provider can route even a
        hash-declared key through composite bundle generation
        (`_runner.py`/`_pandas_adapter.py`) instead of the plain hash handler;
        this gate has no registry to check which providers are safe, so any
        provider on an FK key is rejected conservatively.
    (12) The FK key dtype is in the EXACT cross-adapter-safe set (declared
        stage; see `_chunked_fk_dtype_safety.py`). This is intentionally
        NARROWER than the existing dtype-family comparison in the block below
        (which admits `date64`-as-`date32`, `decimal256`-as-`decimal128`, and a
        fixed-offset tz as IANA) -- hash is cross-adapter byte-identical only
        for the precise dtype set that module defines. This is the DECLARED
        half of a two-stage check; the per-chunk runtime guard
        (`_chunked_fk_dtype.reject_mismatched_chunked_fk_declared_dtype`)
        re-validates the REAL Arrow type at each chunk boundary, since this
        declared check -- like the family check beside it -- only ever runs
        when `table` is the CHILD of the edge being walked.

    First cut: single-column edges only; composite FK rejected.
    Tables that are FK parents but not children are not gated here (except
    predicate 8, which reads their child-endpoint identity from the config
    directly rather than requiring them to be the currently-gated table).

    Raises:
        PlanCompileError: if any condition fails (fail closed).
            chunked_fk_orphan_policy_not_remap: orphan_policy is not 'remap'.
            chunked_fk_composite_unsupported: FK edge has more than one column.
            chunked_fk_parent_strategy_not_self_mask_safe: parent strategy is
                not exactly 'hash'.
            chunked_fk_parent_namespace_missing: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and parent column has no namespace.
            chunked_fk_parent_namespace_mismatch: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and edge namespace disagrees with
                the parent-column namespace.
            chunked_fk_child_namespace_missing: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and child column has no namespace.
            chunked_fk_child_namespace_mismatch: parent strategy is in
                NAMESPACE_REQUIRING_STRATEGIES and child namespace does not match
                the parent-column namespace.
            chunked_fk_child_strategy_missing: child column has no strategy.
            chunked_fk_child_strategy_mismatch: child strategy differs from
                the parent strategy.
            chunked_fk_child_config_mismatch: child provider_config differs
                from the parent's (same strategy name but different
                value-affecting settings, e.g. redact_with, truncate length,
                hash truncation), so self-masking would NOT reproduce the
                parent's masked value even though the strategy name matches.
            chunked_fk_child_key_dtype_mismatch: parent and child FK key
                columns declare different dtype families (e.g. int vs
                float), so self-masking the child's own value independently
                cannot be guaranteed to reproduce the parent's masked value
                even when strategy, namespace, and provider_config all match.
            chunked_fk_child_key_dtype_unprovable: a value-sensitive strategy
                (anything but redact) does not have BOTH FK key dtypes declared,
                so identical masked values cannot be proven. Fail closed: an
                undeclared dtype is not knowable, and the child's chunked run
                never sees the parent's data to check at runtime.
            chunked_fk_parent_not_root: predicate 8 (parent key node is itself
                an FK child endpoint elsewhere).
            chunked_fk_parent_when_unsupported / chunked_fk_child_when_unsupported:
                predicates 9/10 (an effective `when` on the parent/child key).
            chunked_fk_endpoint_not_scalar: predicate 11 (a `provider` on either
                FK key endpoint).
            chunked_fk_key_dtype_not_cross_adapter_safe: predicate 12, declared
                stage (an FK key dtype outside the exact cross-adapter-safe
                set).
    """
    col_index = _col_index_from_config(config)
    child_endpoints = _child_endpoints_from_config(config)

    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        parent_info = rel_entry.get("parent") or {}
        parent_table = parent_info.get("table")
        parent_cols = parent_info.get("columns") or []
        rel_ns: str | None = rel_entry.get("namespace")
        orphan_policy = rel_entry.get("orphan_policy")

        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict):
                continue
            child_table = child_info.get("table")
            child_cols = child_info.get("columns") or []

            # Only gate edges where the current table is the FK child.
            if child_table != table:
                continue

            path_prefix = f"relationships[{parent_table}.{parent_cols}->{child_table}.{child_cols}]"

            # Condition (d): REMAP is the only policy byte-identical to self-masking.
            # REMAP mints via parent_strategy(seed, ns, orphan_key) == self-mask.
            # WARN/FAIL/PRESERVE require the parent key set resident.
            if orphan_policy != "remap":
                raise PlanCompileError(
                    code="chunked_fk_orphan_policy_not_remap",
                    path=f"{path_prefix}.orphan_policy",
                    message=(
                        f"FK edge {parent_table}->{child_table}: chunked self-masking "
                        f"requires orphan_policy 'remap'; got {orphan_policy!r}. "
                        "REMAP mints via the same parent strategy and namespace as "
                        "self-masking. WARN/FAIL/PRESERVE require the parent key set "
                        "resident. Use run_pipeline or run_sequential instead."
                    ),
                )

            # First cut: single-column FK edges only.
            if len(parent_cols) != 1 or len(child_cols) != 1:
                raise PlanCompileError(
                    code="chunked_fk_composite_unsupported",
                    path=f"{path_prefix}.columns",
                    message=(
                        f"FK edge {parent_table}->{child_table}: composite FK edges "
                        "(multi-column keys) are not supported in chunked self-masking. "
                        "Use run_pipeline or run_sequential instead."
                    ),
                )

            parent_col = str(parent_cols[0])
            child_col = str(child_cols[0])

            # Condition (a): parent strategy must be EXACTLY hash (2026-09-02
            # cascade-safety fix). Every other CHUNK_SAFE_STRATEGIES member is
            # a proven strategy x representation x substrate hole for FK self-
            # masking (see this function's docstring); a minimal single-
            # strategy allowlist closes that whole hole class by construction
            # and fails closed against any future CHUNK_SAFE_STRATEGIES
            # addition.
            parent_cfg = col_index.get((str(parent_table), parent_col), {})
            parent_strategy = parent_cfg.get("strategy")
            if parent_strategy != "hash":
                raise PlanCompileError(
                    code="chunked_fk_parent_strategy_not_self_mask_safe",
                    path=f"tables.{parent_table}.columns.{parent_col}.strategy",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"parent key strategy {parent_strategy!r} is not self-mask "
                        "safe. Chunked FK self-masking admits exactly one strategy: "
                        "'hash' (proven cross-adapter byte-identical for the exact "
                        "dtype set predicate 12 restricts FK keys to). Use "
                        "run_pipeline or run_sequential for any other strategy."
                    ),
                )

            # Condition (c): namespace sub-checks apply ONLY when the parent
            # strategy is in NAMESPACE_REQUIRING_STRATEGIES (hash, fpe, date_shift).
            # Those handlers call derive(job_seed, namespace, ...) and raise at
            # execution when plan.namespace is None.  Namespace-agnostic strategies
            # (redact, truncate, text_redact, bucketize, top_code, passthrough,
            # text_mask) do not read plan.namespace; their output is pure(value,
            # config) and byte-identical
            # regardless of whether a namespace is declared, so no namespace
            # sub-checks are needed and imposing them would over-reject configs that
            # the full-frame path accepts.
            #
            # Sub-checks for namespace-requiring strategies (all fail-closed):
            #   c1. Parent column must declare a namespace; the strategy raises at
            #       execution when plan.namespace is None.  Closed here so chunked
            #       is not more permissive than the full-frame path.
            #   c2. When rel_ns is set it must equal the parent-column namespace;
            #       a disagreement flags a mis-wiring (edge ns != masking ns).
            #   c3. Child namespace must equal the parent-column namespace;
            #       otherwise derive() uses a different key and output diverges.
            # text_mask is namespace-agnostic (keys on ctx.mask_key, not a
            # per-column namespace), so it never reaches these sub-checks.
            parent_ns: str | None = parent_cfg.get("namespace")
            child_cfg = col_index.get((str(child_table), child_col), {})
            if parent_strategy in NAMESPACE_REQUIRING_STRATEGIES:
                if parent_ns is None:
                    raise PlanCompileError(
                        code="chunked_fk_parent_namespace_missing",
                        path=f"tables.{parent_table}.columns.{parent_col}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"parent column has no namespace and strategy "
                            f"{parent_strategy!r} requires one. "
                            f"The {parent_strategy!r} strategy raises an error at "
                            "execution when no namespace is declared. "
                            "Add 'namespace: <ns>' to the parent column."
                        ),
                    )
                if rel_ns is not None and rel_ns != parent_ns:
                    raise PlanCompileError(
                        code="chunked_fk_parent_namespace_mismatch",
                        path=f"{path_prefix}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"edge namespace {rel_ns!r} disagrees with parent column "
                            f"namespace {parent_ns!r}. The parent column namespace is "
                            "the authoritative masking key; the edge namespace is a "
                            "label only and is never used to derive a masked value. "
                            f"Update the edge namespace to {parent_ns!r} or remove it."
                        ),
                    )

                # Child must explicitly declare the same namespace.
                # ColumnSeed.namespace is set from col_entry.get('namespace') directly;
                # FK auto-binding updates the NamespaceRegistry but NOT the ColumnSeed.
                child_ns: str | None = child_cfg.get("namespace")
                if child_ns is None:
                    raise PlanCompileError(
                        code="chunked_fk_child_namespace_missing",
                        path=f"tables.{child_table}.columns.{child_col}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"child column {child_table}.{child_col} has no explicit namespace. "
                            "ColumnSeed.namespace comes from the config column entry, not from "
                            "FK auto-binding. "
                            f"Add 'namespace: {parent_ns}' to the child column."
                        ),
                    )
                if child_ns != parent_ns:
                    raise PlanCompileError(
                        code="chunked_fk_child_namespace_mismatch",
                        path=f"tables.{child_table}.columns.{child_col}.namespace",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"child namespace {child_ns!r} does not match parent column "
                            f"namespace {parent_ns!r}. "
                            "Self-masking requires child_ns == parent_ns so "
                            "derive(seed, ns, value) produces byte-identical output."
                        ),
                    )

            # Condition (b): child must explicitly declare the same strategy.
            # By-reference model (no child strategy) would stream raw FK keys.
            child_strategy: str | None = child_cfg.get("strategy")
            if child_strategy is None:
                raise PlanCompileError(
                    code="chunked_fk_child_strategy_missing",
                    path=f"tables.{child_table}.columns.{child_col}.strategy",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child column {child_table}.{child_col} has no explicit strategy. "
                        "The by-reference model would stream raw FK key values without "
                        "the parent map resident. "
                        f"Add 'strategy: {parent_strategy}' to the child column."
                    ),
                )
            if child_strategy != parent_strategy:
                raise PlanCompileError(
                    code="chunked_fk_child_strategy_mismatch",
                    path=f"tables.{child_table}.columns.{child_col}.strategy",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child strategy {child_strategy!r} != parent strategy "
                        f"{parent_strategy!r}. Self-masking requires identical strategies. "
                        f"Update the child strategy to {parent_strategy!r}."
                    ),
                )

            # Predicate 8: the parent KEY NODE must not itself be an FK child
            # endpoint anywhere in the config (e.g. a same-key cascade
            # A -> B -> C, or A.id -> B.id -> C.id). If it were, the oracle
            # FK-RESOLVES the parent key via ITS OWN parent map (and an
            # upstream cascade failure can null it out) instead of masking it
            # by the declared strategy -- self-masking the grandchild computes
            # hash(raw_value) where the oracle computes
            # hash(resolved_grandparent_value), a silent divergence. The check
            # is on the EXACT (table, columns) tuple, matching how
            # `RelationshipGraph`'s FK override keys edges
            # (`relationships/_graph.py`), so a distinct-column self-FK on the
            # same table (`employees.id -> employees.manager_id`) is NOT
            # caught: `(employees, ("id",))` and `(employees, ("manager_id",))`
            # are different key nodes, and `id` is never resolved by any edge.
            parent_key_node = (str(parent_table), (parent_col,))
            if parent_key_node in child_endpoints:
                raise PlanCompileError(
                    code="chunked_fk_parent_not_root",
                    path=f"tables.{parent_table}.columns.{parent_col}",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"{parent_table}.{parent_col} is itself an FK child "
                        "column elsewhere (a same-key cascade). The oracle FK-"
                        "resolves it via its own parent map rather than masking "
                        "it directly, so self-masking this edge's child would "
                        "diverge from the resolved value. Use run_pipeline or "
                        "run_sequential for a multi-hop same-key cascade."
                    ),
                )

            # Predicates 9/10: neither the parent nor the child FK key column
            # may have an EFFECTIVE `when` (a non-blank string). `when`
            # normalization mirrors the compiler's own
            # (`plan/_seed_envelope.py:245`): a row-gated key is masked
            # CONDITIONALLY by the oracle (rows failing `when` stay raw), but
            # self-masking has no concept of `when` and always masks every
            # row, so a `when`-bearing FK key would diverge on the gated rows.
            def _has_effective_when(col_entry: dict[str, Any]) -> bool:
                when_raw = col_entry.get("when")
                return isinstance(when_raw, str) and bool(when_raw.strip())

            if _has_effective_when(parent_cfg):
                raise PlanCompileError(
                    code="chunked_fk_parent_when_unsupported",
                    path=f"tables.{parent_table}.columns.{parent_col}.when",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"parent key column {parent_table}.{parent_col} has an "
                        "effective 'when'. The oracle masks this key "
                        "conditionally (rows failing 'when' stay raw); self-"
                        "masking has no 'when' and always masks, so the child "
                        "would diverge on the gated rows. Remove 'when' from the "
                        "parent key column, or use run_pipeline / run_sequential."
                    ),
                )
            if _has_effective_when(child_cfg):
                raise PlanCompileError(
                    code="chunked_fk_child_when_unsupported",
                    path=f"tables.{child_table}.columns.{child_col}.when",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child key column {child_table}.{child_col} has an "
                        "effective 'when'. The oracle ignores 'when' on an FK-"
                        "resolved child column, but self-masking would honor it "
                        "and mask only the rows that pass, diverging from the "
                        "oracle on the gated rows. Remove 'when' from the child "
                        "key column, or use run_pipeline / run_sequential."
                    ),
                )

            # Predicate 11: neither FK endpoint may declare a `provider`. A
            # provider can route even a hash-declared key through composite
            # bundle generation (`_runner.py` / `_pandas_adapter.py`) instead
            # of the plain hash handler; this gate has no provider registry to
            # check which providers are safe for FK self-masking, so any
            # provider on either endpoint is rejected conservatively.
            if parent_cfg.get("provider") is not None:
                raise PlanCompileError(
                    code="chunked_fk_endpoint_not_scalar",
                    path=f"tables.{parent_table}.columns.{parent_col}.provider",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"parent key column {parent_table}.{parent_col} declares "
                        "a provider. A provider can route a hash-declared key "
                        "through composite bundle generation instead of the "
                        "plain hash handler, which this gate cannot verify is "
                        "safe. Remove the provider from the parent key column, "
                        "or use run_pipeline / run_sequential."
                    ),
                )
            if child_cfg.get("provider") is not None:
                raise PlanCompileError(
                    code="chunked_fk_endpoint_not_scalar",
                    path=f"tables.{child_table}.columns.{child_col}.provider",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child key column {child_table}.{child_col} declares a "
                        "provider. A provider can route a hash-declared key "
                        "through composite bundle generation instead of the "
                        "plain hash handler, which this gate cannot verify is "
                        "safe. Remove the provider from the child key column, or "
                        "use run_pipeline / run_sequential."
                    ),
                )

            # Condition (e): child provider_config must be IDENTICAL to the
            # parent's, not just the same strategy name. Matching names alone
            # is not enough: e.g. redact_with='P' on the parent vs 'C' on the
            # child, or different truncate lengths / hash truncation, produce
            # different masked bytes even though both sides say "redact" or
            # "truncate". run_mask_pipeline_chunked self-masks the child
            # independently of the parent, so unless the two configs are
            # byte-for-byte identical, self-masking cannot be guaranteed to
            # reproduce the parent's masked value and FK referential
            # integrity would silently break.
            parent_provider_cfg = parent_cfg.get("provider_config") or {}
            child_provider_cfg = child_cfg.get("provider_config") or {}
            if parent_provider_cfg != child_provider_cfg:
                raise PlanCompileError(
                    code="chunked_fk_child_config_mismatch",
                    path=f"tables.{child_table}.columns.{child_col}.provider_config",
                    message=(
                        f"FK edge {parent_table}.{parent_col}"
                        f"->{child_table}.{child_col}: "
                        f"child provider_config {child_provider_cfg!r} != parent "
                        f"provider_config {parent_provider_cfg!r}. Both declare "
                        f"strategy {parent_strategy!r} but with different "
                        "value-affecting settings, so self-masking the child "
                        "independently would not reproduce the parent's masked "
                        "value. Make the child provider_config identical to the "
                        "parent's."
                    ),
                )

            # Condition (f): parent and child FK key column dtypes must be
            # PROVABLY compatible, or self-masking cannot be guaranteed to
            # reproduce the parent's masked value. `run_mask_pipeline_chunked`
            # masks the child's OWN value independently of the parent (there
            # is no parent-map lookup): matching strategy + namespace +
            # provider_config only guarantees the same masked BYTES when the
            # two sides feed the SAME raw value through that strategy. A
            # parent int64 key and a child float64 FK key holding the "same"
            # logical value (1 vs 1.0) hash/truncate/FPE to DIFFERENT bytes
            # (the kernels key on the value's own representation, not on the
            # normalized-equal FK identity the full-frame/out-of-core parent-
            # map routes use), silently breaking referential integrity.
            #
            # FAIL CLOSED (fix, 2026-07-07): a value-sensitive strategy is
            # admitted ONLY when both sides declare the SAME dtype family. The
            # child's chunked run never sees the parent's data (the parent is
            # not a source here), so the parent's real dtype is not knowable at
            # runtime either -- there is no first-chunk check that can recover
            # it. The prior guard fired ONLY when BOTH dtypes were declared, so
            # an undeclared dtype (the common case) let an int-parent/float-
            # child mismatch through fail-OPEN. An undeclared dtype on either
            # side is now unprovable and is REJECTED; full-frame / run_sequential
            # (which normalize FK key equality via the parent map) handle it.
            # The one exception is a value-independent strategy (redact emits a
            # constant, condition (e) pins it identical on both sides), which
            # masks to the same value under any dtype.
            if parent_strategy not in DTYPE_INVARIANT_STRATEGIES:
                parent_dtype = parent_cfg.get("dtype")
                child_dtype = child_cfg.get("dtype")
                if parent_dtype is None or child_dtype is None:
                    raise PlanCompileError(
                        code="chunked_fk_child_key_dtype_unprovable",
                        path=f"tables.{child_table}.columns.{child_col}.dtype",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"strategy {parent_strategy!r} is value-sensitive, so "
                            "chunked self-masking reproduces the parent's masked "
                            "value only when the parent and child FK key columns "
                            "share a dtype family. That cannot be proven here: "
                            f"parent dtype {parent_dtype!r}, child dtype "
                            f"{child_dtype!r} (an undeclared dtype is not knowable "
                            "at compile time, and the child's chunked run never "
                            "sees the parent's data to check at runtime). Declare a "
                            "matching 'dtype' on both FK key columns, or use "
                            "run_pipeline / run_sequential (which normalize FK key "
                            "equality via the parent map)."
                        ),
                    )
                if _dtype_family(parent_dtype) != _dtype_family(child_dtype):
                    raise PlanCompileError(
                        code="chunked_fk_child_key_dtype_mismatch",
                        path=f"tables.{child_table}.columns.{child_col}.dtype",
                        message=(
                            f"FK edge {parent_table}.{parent_col}"
                            f"->{child_table}.{child_col}: "
                            f"child dtype {child_dtype!r} is not the same family as "
                            f"parent dtype {parent_dtype!r}. Chunked self-masking "
                            "hashes/truncates/FPEs the child's own value "
                            "independently of the parent, with no parent-map lookup "
                            "to normalize FK key equality; a dtype-family mismatch "
                            "(e.g. parent int vs child float) means the child's raw "
                            "value differs byte-for-byte from the parent's even when "
                            "both represent the 'same' logical key, so self-masking "
                            "cannot be guaranteed to reproduce the parent's masked "
                            "value. Use run_pipeline or run_sequential instead, or "
                            "normalize both columns to the same dtype before masking."
                        ),
                    )

                # Predicate 12 (declared stage): hash is cross-adapter byte-
                # identical only for the EXACT dtype set
                # `_chunked_fk_dtype_safety` defines -- narrower than the
                # family match just above, which collapses date32/date64,
                # every timestamp unit/tz, and every decimal width/scale into
                # one family string (so it would admit date64-as-date32,
                # decimal256-as-decimal128, and a fixed-offset tz as IANA).
                # Both endpoints are checked independently: sharing a family
                # does not mean either one is individually safe (e.g. both
                # declared "date64" share a family but neither is safe). This
                # is a TRUSTED declared-string check, like the family check
                # beside it; the per-chunk runtime guard
                # (`_chunked_fk_dtype.reject_mismatched_chunked_fk_declared_
                # dtype`) re-validates the REAL Arrow type for the same reason
                # the family check's runtime companion exists.
                for endpoint_table, endpoint_col, endpoint_dtype in (
                    (parent_table, parent_col, parent_dtype),
                    (child_table, child_col, child_dtype),
                ):
                    if not declared_fk_hash_dtype_is_safe(str(endpoint_dtype)):
                        raise PlanCompileError(
                            code="chunked_fk_key_dtype_not_cross_adapter_safe",
                            path=f"tables.{endpoint_table}.columns.{endpoint_col}.dtype",
                            message=(
                                f"FK edge {parent_table}.{parent_col}"
                                f"->{child_table}.{child_col}: "
                                f"{endpoint_table}.{endpoint_col} declares FK key "
                                f"dtype {endpoint_dtype!r}, which is not in the "
                                "exact cross-adapter-safe set for hash-only FK "
                                "self-masking (string, large_string, any integer "
                                "width, bool, date32, an IANA-tz timestamp, or a "
                                "32/64/128-bit decimal with scale >= 0). hash is "
                                "only proven cross-adapter byte-identical for "
                                "these dtypes. Declare one of them, or use "
                                "run_pipeline / run_sequential."
                            ),
                        )


def fk_hash_strategy_columns_for_table(config: dict[str, Any], table: str) -> set[str]:
    """Every FK-participant column on `table` (either endpoint role) declared
    with EXACTLY the `hash` strategy.

    Scopes predicate 12's exact cross-adapter-safe dtype restriction to the
    population the hash-only FK self-mask allowlist (condition (a)) actually
    admits, for the per-chunk REAL-type stage of the check
    (`_chunked_fk_dtype.reject_mismatched_chunked_fk_declared_dtype`). Mirrors
    `fk_passthrough_columns_for_table`'s both-sides scan. A `truncate`/`fpe`/
    `passthrough` FK column elsewhere on the same table still gets the
    (unchanged) coarse family-level guard via `fk_declared_dtypes_for_table`,
    but is NOT held to this narrower exact-dtype rule: predicate 12 is specific
    to the hash-only cascade-safety fix, not every chunk-safe strategy.
    """
    fk_columns: set[str] = set()
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        parent_info = rel_entry.get("parent") or {}
        if isinstance(parent_info, dict) and parent_info.get("table") == table:
            fk_columns.update(c for c in parent_info.get("columns") or [] if isinstance(c, str))
        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict) or child_info.get("table") != table:
                continue
            fk_columns.update(c for c in child_info.get("columns") or [] if isinstance(c, str))
    if not fk_columns:
        return set()
    table_cfg = next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        None,
    )
    if table_cfg is None:
        return set()
    hash_columns: set[str] = set()
    for col in table_cfg.get("columns") or []:
        if not isinstance(col, dict) or col.get("strategy") != "hash":
            continue
        col_name = col.get("name")
        if isinstance(col_name, str) and col_name in fk_columns:
            hash_columns.add(col_name)
    return hash_columns


def fk_passthrough_columns_for_table(config: dict[str, Any], table: str) -> set[str]:
    """Every FK-relevant column on `table` admitted onto the chunked route
    under a `passthrough` strategy -- the MEDIUM #4 / BLOCKER #2 gap set (see
    module docstring).

    Covers BOTH sides of a relationship edge where `table` participates,
    symmetric with `_fk_keys.fk_columns_for_table` (the full-frame/sequential
    equivalent, which protects parent AND child columns identically). A
    `passthrough` PARENT key column is not resolved through a join on this
    route (self-masking has no parent-map lookup), but it goes through the
    SAME unprotected `table.to_pandas()` ingestion as every other table this
    route processes (`run_mask_pipeline_chunked` runs one table at a time,
    each with an empty `RelationshipGraph`) -- the vulnerability is about
    ingestion, not joins, so a chunked PARENT table with a null-bearing
    `passthrough` key beyond `2**53` is exactly as exposed as a child one.
    Restricting this to child columns only (the prior cut) left every
    chunked-route parent key column silently rounding through this same
    float64-on-null path; dennis reproduced `[1.0, None,
    9007199254740992.0]` for a parent-side `passthrough` key.
    `check_chunked_compatibility` requires every admitted FK CHILD edge's
    column to declare a strategy explicitly, but a parent-only table has no
    such requirement, so this reads directly off `config.relationships` /
    `config.tables` rather than the (deliberately empty) runtime
    `RelationshipGraph`.

    Mirrors `gate_fk_child_edges`'s config parsing: `relationships` entries
    nest one `parent` (`{"table": ..., "columns": [...]}`) and a list of
    `children` (same shape each), NOT flat `parent_table`/`child_table` keys.
    """
    fk_columns: set[str] = set()
    for rel_entry in config.get("relationships") or []:
        if not isinstance(rel_entry, dict):
            continue
        parent_info = rel_entry.get("parent") or {}
        if isinstance(parent_info, dict) and parent_info.get("table") == table:
            fk_columns.update(c for c in parent_info.get("columns") or [] if isinstance(c, str))
        for child_info in rel_entry.get("children") or []:
            if not isinstance(child_info, dict) or child_info.get("table") != table:
                continue
            fk_columns.update(c for c in child_info.get("columns") or [] if isinstance(c, str))
    if not fk_columns:
        return set()
    table_cfg = next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        None,
    )
    if table_cfg is None:
        return set()
    passthrough_columns = {
        col.get("name")
        for col in table_cfg.get("columns") or []
        if isinstance(col, dict) and col.get("strategy") == "passthrough"
    }
    return fk_columns & passthrough_columns


def reject_lossy_chunked_fk_passthrough(
    chunk: pa.Table, *, table: str, passthrough_fk_columns: set[str]
) -> None:
    """Fail closed on a null-bearing `passthrough` FK column carrying a key
    beyond `2**53` (MEDIUM #4, see module docstring) instead of letting the
    chunked route's unprotected ingestion silently round it through
    float64-on-null.

    A narrower guard than `execution._guards.reject_null_bearing_int`
    deliberately: that guard rejects ANY null-bearing int column under
    truncate/hash/categorical (those strategies re-derive their output, so
    ANY int+null ambiguity is a cross-substrate correctness question, not a
    magnitude one). `passthrough` preserves the raw value, so a null-bearing
    `passthrough` int column with every value within exact float64 precision
    round-trips losslessly through this route's unprotected ingestion even
    without `to_pandas_fk_safe`'s protection -- rejecting it too would reject
    legitimate small-int passthrough FK jobs for no correctness reason. Only
    the genuinely lossy shape (null-bearing AND a value beyond `2**53`) fails
    closed here.
    """
    for column in passthrough_fk_columns:
        if column not in chunk.column_names:
            continue
        arrow_type = chunk.schema.field(column).type
        if not pa.types.is_integer(arrow_type):
            continue
        arrow_column = chunk.column(column)
        if arrow_column.null_count == 0:
            continue  # null-free integer column never hits the float64 fallback
        bounds = pc.min_max(  # type: ignore[attr-defined, unused-ignore]
            arrow_column, skip_nulls=True
        ).as_py()
        col_min, col_max = bounds["min"], bounds["max"]
        if col_min is None or col_max is None:
            continue  # every value null; nothing to lose
        if col_max > _EXACT_FLOAT_INT_BOUND or col_min < -_EXACT_FLOAT_INT_BOUND:
            raise ExecutionError(
                code=FK_KEY_DTYPE_UNSUPPORTED_CODE,
                message=(
                    f"Column {table}.{column} is a null-bearing passthrough FK "
                    "key column carrying a value beyond exactly-representable "
                    "float64 precision (> 2**53). The chunked route's ingestion "
                    "does not protect this column (self-masking runs with an "
                    "empty RelationshipGraph); preserving it verbatim would "
                    "silently round the key."
                ),
            )
