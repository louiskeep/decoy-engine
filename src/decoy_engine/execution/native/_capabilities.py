"""Static strategy capabilities for the native planning boundary (Task 0.2).

Replaces the earlier single ``locality`` enum, which conflated orthogonal
properties: a hash is BOTH keyed AND row-local, and no single enum value can
say both. Each capability here is an independent axis, so a strategy that is
keyed, row-local, and zero-diagnostic (``hash``) is described exactly, and a
strategy that is global, order-sensitive, and needs a durable global row
number (``shuffle``) is described exactly.

These are STATIC, strategy-only properties: they depend on the strategy name,
not on a particular column's config or the source schema. The config- and
schema-resolved half (which columns a node scans, the resolved Arrow output
schema, required prepasses, per-node fallback policy) lives in
``_requirements.py`` and reads the compiled node + profile.

Sourcing (never a hand-copied constant set):

- ``draw_family`` and ``key_source`` are read from the Task 0.1 draw-site
  inventory (``_determinism_protocol.MASK_STRATEGY_TO_SITE`` /
  ``GEN_KIND_TO_SITE`` -> ``DrawSite``) for every real strategy, so a
  reclassified draw site flows through here automatically. The two node-kind
  placeholders (``<composite>`` / ``<group>``) are the exception: they are
  hand-authored, because a composite bundle has no single draw site to read.
- ``row_error_modes`` / ``warning_codes`` / ``quality_obligations`` are the
  strategy handlers' ACTUAL diagnostic surface (the ``RowError`` triggers each
  handler appends to ``StrategyContext.row_errors`` and the ``QualityWarning``
  codes each handler returns), so "error-capable" and "quality-sensitive"
  become field reads for the Phase 1 eligibility predicate, not prose. The
  totality test enumerates the live ``SCALAR_HANDLERS`` + ``GENERATE_TYPES``
  registries; a new strategy without an entry fails loudly.

The orthogonal-classification pattern follows the existing central strategy
maps in this engine (``execution._technique_class.TECHNIQUE_CLASS_BY_STRATEGY``
for the GDPR label, ``distribution_behavior_for`` for the drift badge): one
audited table, read by every consumer, a new strategy surfaces as unclassified
rather than silently defaulting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from decoy_engine.execution.native._determinism_protocol import (
    DETERMINISTIC_NO_DRAW,
    GEN_KIND_TO_SITE,
    MASK_STRATEGY_TO_SITE,
    draw_site_by_id,
)

KeySource = Literal["mask_key", "generation_seed"]

# entropy_root (Task 0.1) -> the capabilities key_source enum.
_ENTROPY_TO_KEY: dict[str, KeySource | None] = {
    "mask_key": "mask_key",
    "job_seed": "generation_seed",
    "none": None,
}


@dataclass(frozen=True)
class StrategyCapabilities:
    """Static, strategy-only execution properties.

    Orthogonal axes (each independent; a strategy sets any combination):

    - ``is_row_local``: a row's output is a pure function of that row's own
      input value(s). Partition-safe by construction. False when the output
      depends on other rows, a whole-column statistic, or the row's global
      position.
    - ``is_keyed``: draws from the keyed re-identification derivation
      (``mask_key``). Equals ``key_source == "mask_key"``.
    - ``is_order_sensitive``: output depends on row ORDER (a shared sequential
      RNG stream, a permutation).
    - ``is_global``: needs a whole-column / whole-table pass (a permutation, a
      column statistic bound, an aggregate).
    - ``needs_global_row_identity``: needs a stable global row number that
      survives partition boundaries (a permutation index, a per-group ordinal,
      a per-row-index seed).
    - ``output_type_is_static``: the output Arrow type is knowable from the
      strategy + config alone. False for an arbitrary expression whose result
      type depends on the data (``formula``, ``derived``) or a child-dependent
      wrapper (``nested``). A node whose type is indeterminate is excluded from
      the native route.
    - ``draw_family`` / ``key_source``: sourced from the Task 0.1 inventory.

    Machine-classifiable diagnostics (Phase 1 eligibility reads these):

    - ``row_error_modes``: the distinct ``RowError.trigger`` values the handler
      can emit. Empty = provably cannot emit a row error.
    - ``warning_codes``: every ``QualityWarning.code`` the handler returns
      through its ``run()`` tuple. Empty = zero-warning.
    - ``quality_obligations``: class-A quality checks the strategy's output
      requires (the pool-fidelity decider the streaming route skips). Empty =
      none.
    - ``quarantine_required``: on a row error the strategy leaves the un-masked
      source value in the output, so the row is unsafe unless quarantined (a
      feature the streaming route does not have).
    """

    is_row_local: bool
    is_keyed: bool
    is_order_sensitive: bool
    is_global: bool
    needs_global_row_identity: bool
    output_type_is_static: bool
    draw_family: str | None
    key_source: KeySource | None
    row_error_modes: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    quality_obligations: tuple[str, ...] = ()
    quarantine_required: bool = False
    # Additive honesty fields (mirrors DrawSite): a strategy whose static
    # classification could not be fully pinned from the code is flagged rather
    # than silently guessed.
    notes: str = ""
    uncertain: bool = False


@dataclass(frozen=True)
class _Ortho:
    """The strategy-authored half of a capability row.

    ``draw_family`` / ``key_source`` / ``is_keyed`` are NOT here: they are
    resolved from the Task 0.1 inventory at build time, so this table only
    carries the properties that must be read from the strategy's behavior.
    """

    is_row_local: bool
    is_order_sensitive: bool
    is_global: bool
    needs_global_row_identity: bool
    output_type_is_static: bool
    row_error_modes: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    quality_obligations: tuple[str, ...] = ()
    notes: str = ""
    uncertain: bool = False
    # For a name that spans mask + generation (faker, categorical, ...), the
    # draw family / key source are read from the MASK site by default. A
    # generation-only kind (sequence, reference, statistical) sets gen=True so
    # the lookup consults GEN_KIND_TO_SITE instead.
    gen: bool = False


# ---------------------------------------------------------------------------
# The classification table. One audited row per strategy. Explicit over clever:
# this is a security-relevant routing table, so every value is spelled out and
# testable against the handler surface, not derived from a heuristic.
#
# Diagnostics (row_error_modes / warning_codes / quality_obligations) come from
# the handlers' real emission sites, verified per strategy:
#   row errors: bucketize/date_shift/top_code (format_error), code_set
#     (mask_error), nested (format_error + propagated child trigger).
#   warnings:   top_code (top_code_generalized), geo_generalize
#     (geo_generalize_cascade), fpe (3 codes), nested (2 codes).
#   pool quality: faker only (the sole PoolBuilder/PoolSampler user).
# ---------------------------------------------------------------------------

_POOL_QUALITY = ("pool_quality",)

_MASK: dict[str, _Ortho] = {
    # -- zero-diagnostic row-local transforms (Phase 1 admitted set) ---------
    "passthrough": _Ortho(True, False, False, False, True),
    "redact": _Ortho(True, False, False, False, True),
    "truncate": _Ortho(True, False, False, False, True),
    "hash": _Ortho(True, False, False, False, True),
    # -- keyed row-local, zero-diagnostic ------------------------------------
    "fpe": _Ortho(
        True,
        False,
        False,
        False,
        True,
        warning_codes=(
            "fpe_join_group_active",
            "fpe_sub_minimum_domain",
            "fpe_partial_plaintext_disclosure",
        ),
    ),
    "date_shift": _Ortho(
        True,
        False,
        False,
        False,
        True,
        row_error_modes=("format_error",),
        notes=(
            "Output is always a formatted date string (static type). Without an "
            "explicit date_format the NODE needs a format_detect prepass "
            "(resolved in _requirements.py), not a change to the static type."
        ),
    ),
    "bucket_perturb": _Ortho(True, False, False, False, True),
    "group_key": _Ortho(True, False, False, False, True),
    "code_set": _Ortho(
        True,
        False,
        False,
        False,
        True,
        row_error_modes=("mask_error",),
        notes="Keyed corpus remap; the corpus is a required state table (see _requirements).",
    ),
    "text_mask": _Ortho(
        True,
        False,
        False,
        False,
        True,
        notes="Per-span source-keyed dispatch; Phase 5 (NER) held.",
    ),
    "categorical": _Ortho(
        True,
        False,
        False,
        False,
        True,
        notes=(
            "Deterministic (default) mode is source-keyed and row-local. The "
            "non-deterministic mode draws a whole-column unseeded vector (global, "
            "not reproducible); that is a config-resolved variant, excluded from "
            "the native route by its unseeded draw, not the static row."
        ),
    ),
    "faker": _Ortho(
        True,
        False,
        False,
        False,
        True,
        quality_obligations=_POOL_QUALITY,
        notes=(
            "Pool SELECTION is source-keyed and row-local (deterministic mode). "
            "The only handler that builds/samples a bounded value pool, so the "
            "sole class-A pool-fidelity obligation; pool warnings ride the "
            "PoolCache side channel, not the run() return tuple."
        ),
    ),
    # -- irreversible row-local generalizers ---------------------------------
    "text_redact": _Ortho(True, False, False, False, True),
    "geo_generalize": _Ortho(
        True,
        False,
        False,
        False,
        True,
        warning_codes=("geo_generalize_cascade",),
    ),
    "bucketize": _Ortho(
        True,
        False,
        False,
        False,
        True,
        row_error_modes=("format_error",),
    ),
    # -- row-context expression (indeterminate output type) ------------------
    "derived": _Ortho(
        True,
        False,
        False,
        False,
        False,
        notes="Closed-grammar row-context expression; output type depends on the data.",
    ),
    # -- global / order-sensitive strategies ---------------------------------
    "top_code": _Ortho(
        False,
        False,
        True,
        False,
        True,
        row_error_modes=("format_error",),
        warning_codes=("top_code_generalized",),
        notes="The cap can be a column percentile, so a row's output can depend on a column-global bound.",
    ),
    "derived_aggregate": _Ortho(
        False,
        False,
        True,
        False,
        False,
        notes="Fills every row from one intra-table aggregate scalar; output type depends on the aggregate.",
    ),
    "shuffle": _Ortho(
        False,
        True,
        True,
        True,
        True,
        notes="Whole-column keyed permutation; non-partitionable (Task 0.1 mask.shuffle).",
    ),
    "formula": _Ortho(
        False,
        True,
        True,
        False,
        False,
        notes="One shared RNG across rows via column.apply; order-dependent, self-seeded (no key), arbitrary output type.",
    ),
    "grouped_series": _Ortho(
        False,
        True,
        True,
        True,
        True,
        notes="Per-group sequential RNG stream; non-partitionable (Task 0.1 mask.grouped_series_monotone_walk).",
    ),
    "windowed_date": _Ortho(
        False,
        False,
        False,
        True,
        True,
        uncertain=True,
        notes=(
            "Per-row seed keys on the GLOBAL row index, not the source value "
            "(Task 0.1 flagged uncertain), so it is not row-local and needs the "
            "global row number preserved across partitions."
        ),
    ),
    # -- child-delegating wrapper (Phase 5 held) -----------------------------
    "nested": _Ortho(
        True,
        False,
        False,
        False,
        False,
        row_error_modes=("format_error", "mask_error"),
        warning_codes=("nested_cell_json_parse_error", "nested_jsonpath_path_overlap"),
        uncertain=True,
        notes=(
            "Delegates to a config-resolved child handler: its effective surface "
            "is the union of the direct JSON errors above with the child's "
            "triggers/warnings/pool-obligation. Output type is child-dependent, "
            "so it is excluded from the native route regardless."
        ),
    ),
    # -- reference-table joint selection (Phase 5 held) ----------------------
    "joint_mask": _Ortho(
        True,
        False,
        False,
        False,
        False,
        notes="Keyed selection of a whole reference-table row; multi-column output, requires the reference state table.",
    ),
}

# Generation-only kinds (not also a mask strategy). draw_family/key_source read
# from GEN_KIND_TO_SITE via gen=True.
_GEN_ONLY: dict[str, _Ortho] = {
    "sequence": _Ortho(
        False,
        True,
        False,
        True,
        True,
        gen=True,
        notes="Positional counter; needs the global row number, deterministic (no draw).",
    ),
    "reference": _Ortho(
        False,
        True,
        True,
        False,
        True,
        gen=True,
        notes="One sequential python Random advanced across all rows; non-partitionable (Task 0.1 gen.reference).",
    ),
    "statistical": _Ortho(
        True,
        False,
        False,
        True,
        True,
        gen=True,
        notes="Per-row reseed from col_seed + row index; partition-safe when the global index is preserved.",
    ),
}

# Composite / FK-group placeholder strategy names stamped by build_work_list.
# capabilities_for resolves them so node-kind coverage is total; requirements_for
# keys on node.kind + resolved strategy for the config-resolved half.
_PLACEHOLDERS: dict[str, StrategyCapabilities] = {
    "<composite>": StrategyCapabilities(
        is_row_local=True,
        is_keyed=False,
        is_order_sensitive=False,
        is_global=False,
        needs_global_row_identity=False,
        output_type_is_static=False,
        draw_family="source_keyed_hmac",
        key_source="generation_seed",
        quality_obligations=_POOL_QUALITY,
        notes="Composite bundle node: pool-backed multi-column coherent output, keyed selection.",
    ),
    "<group>": StrategyCapabilities(
        is_row_local=True,
        is_keyed=True,
        is_order_sensitive=False,
        is_global=False,
        needs_global_row_identity=False,
        output_type_is_static=True,
        draw_family="source_keyed_hmac",
        key_source="mask_key",
        notes="Composite-FK group node: keyed per-group identifier over the coherent column tuple.",
    ),
}


def _resolve_draw(name: str, *, gen: bool) -> tuple[str | None, KeySource | None]:
    """Read (draw_family, key_source) from the Task 0.1 inventory for ``name``."""
    site_map = GEN_KIND_TO_SITE if gen else MASK_STRATEGY_TO_SITE
    site_id = site_map[name]
    if site_id == DETERMINISTIC_NO_DRAW:
        return None, None
    site = draw_site_by_id(site_id)
    return site.family, _ENTROPY_TO_KEY[site.entropy_root]


def _build(name: str, spec: _Ortho) -> StrategyCapabilities:
    draw_family, key_source = _resolve_draw(name, gen=spec.gen)
    return StrategyCapabilities(
        is_row_local=spec.is_row_local,
        is_keyed=key_source == "mask_key",
        is_order_sensitive=spec.is_order_sensitive,
        is_global=spec.is_global,
        needs_global_row_identity=spec.needs_global_row_identity,
        output_type_is_static=spec.output_type_is_static,
        draw_family=draw_family,
        key_source=key_source,
        row_error_modes=spec.row_error_modes,
        warning_codes=spec.warning_codes,
        quality_obligations=spec.quality_obligations,
        # A row error leaves the un-masked source value in the output, so the
        # row is unsafe without quarantine (which the streaming route lacks).
        quarantine_required=bool(spec.row_error_modes),
        notes=spec.notes,
        uncertain=spec.uncertain,
    )


# The assembled, frozen capability table. Mask entries win when a name spans
# mask + generation (the mask semantics are the re-identification-relevant
# ones); generation-only kinds fill the rest.
_CAPS: dict[str, StrategyCapabilities] = {
    **{name: _build(name, spec) for name, spec in _MASK.items()},
    **{name: _build(name, spec) for name, spec in _GEN_ONLY.items()},
    **_PLACEHOLDERS,
}


def capabilities_for(strategy: str) -> StrategyCapabilities:
    """Return the static capabilities for ``strategy``.

    Raises ``KeyError`` for an unclassified strategy so a newly added strategy
    fails loudly until it is entered here (the coverage guarantee Task 0.1
    established for draw sites, extended to capabilities).
    """
    try:
        return _CAPS[strategy]
    except KeyError:
        raise KeyError(
            f"strategy {strategy!r} has no StrategyCapabilities entry; classify it in _capabilities.py"
        ) from None


def classified_strategies() -> frozenset[str]:
    """Every strategy/kind name with a capabilities entry (for coverage tests)."""
    return frozenset(_CAPS)


__all__ = [
    "KeySource",
    "StrategyCapabilities",
    "capabilities_for",
    "classified_strategies",
]
