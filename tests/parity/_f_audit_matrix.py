"""F-audit: the generation compute matrix, derived from the live dispatch predicates.

Nothing here reads an observed disposition. A row is a PARTITION CELL of the dispatch, selected by
the predicates the code itself consults: the admission ladder (`_admission.classify`), the
per-kind stream ladder (`_column_streams.make_column_stream`), the reference distribution and
empty-parent branches (`ReferenceStream.next_batch`), and the pooled-faker gate
(`_faker_pool.pool_eligible`). A row carries SEPARATE fields, because one label cannot say them
all (formula is legacy-routed AND formula-expression compute; derived is legacy AND
`no_selection`):

- column admission and reason (what `classify` says about this column alone);
- whole-plan route (core or legacy, `all(column eligible)`);
- COMPUTE, a closed enum that carries no route or witness meaning;
- materialization routes the plan can take (resident, bounded streaming);
- witness status (value-equality, or the reason a positive witness is excluded).

Generation has NO compiled path by design (FR3 shelved), so there is no "compiled" compute value
and no fallback obligation. `test_phase_f_audit_matrix.py` proves coverage of every
`GenerateColumnConfig.type` Literal kind and that exactly one row matches each probed case.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from decoy_engine.generation import _faker_pool
from decoy_engine.generation._admission import ADMISSION_MATRIX

# The closed COMPUTE enum.
NO_SELECTION: Final = "no_selection"
PYTHON_RNG: Final = "python_rng_selection"
NUMPY: Final = "numpy_selection"
LIVE_PROVIDER: Final = "live_provider_generation"
FORMULA: Final = "formula_expression"
WINDOWED_SHIFT: Final = "windowed_date_shift"
LEGACY_SYNTHESIZE: Final = "legacy_synthesize"
COMPUTES: Final = frozenset(
    {NO_SELECTION, PYTHON_RNG, NUMPY, LIVE_PROVIDER, FORMULA, WINDOWED_SHIFT, LEGACY_SYNTHESIZE}
)

CORE: Final = "core"
LEGACY: Final = "legacy"
RESIDENT: Final = "resident"
STREAMED: Final = "streamed"
VALUE_EQUALITY: Final = "value_equality"
EXCLUDED: Final = "excluded"

# A kind generate_tables never dispatches: counted separately from the Literal kinds.
NON_LITERAL_EXCLUSIONS: Final = ("composite",)

ADMITTED_KINDS: Final = frozenset(k for k, (ok, _) in ADMISSION_MATRIX.items() if ok)

FAKER_MODES: Final = (
    "pooled",
    "below_threshold",
    "opt_out",
    "uncertified",
    "build_refused",
    "custom",
)


@dataclass(frozen=True)
class Probe:
    """One concrete dispatch case: the column parameters the predicates read."""

    kind: str
    fresh: bool = False  # determinism: fresh
    distribution: str = "random"  # reference only
    parent_empty: bool = False  # reference only: the parent key set is empty
    repair: bool = False  # reference only: min/max_per_parent
    faker: str = "pooled"  # faker only, one of FAKER_MODES
    referenced: bool = False  # formula only: reads sibling columns
    anchor_ok: bool = True  # windowed_date only: earlier, eligible, supported-type anchor
    plan_decliner: bool = False  # the column is fine, another column of the plan declines


def standalone_admitted(p: Probe) -> bool:
    """Would `classify` admit this column if it were alone in the plan."""
    if p.fresh or p.kind not in ADMITTED_KINDS:
        return False
    if p.kind == "reference":
        return not p.repair
    if p.kind == "faker":
        return p.faker != "custom"
    if p.kind == "windowed_date":
        return p.anchor_ok
    return True


def reason_of(p: Probe) -> str:
    """The column's own `classify` reason (a prefix for the windowed_date anchor family)."""
    if p.fresh:
        return "determinism_fresh"
    if p.kind not in ADMITTED_KINDS:
        return f"family_declined:{p.kind}"
    if p.kind == "reference" and p.repair:
        return "reference_cardinality_repair"
    if p.kind == "faker" and p.faker == "custom":
        return "faker_custom_or_unresolved_provider"
    if p.kind == "windowed_date" and not p.anchor_ok:
        return "windowed_date_anchor"
    return "eligible"


def route_of(p: Probe) -> str:
    return CORE if standalone_admitted(p) and not p.plan_decliner else LEGACY


def _pool_gate(mode: str) -> bool:
    """The real `pool_eligible` verdict for the probe's faker mode (representative inputs)."""
    n = _faker_pool.N_THRESHOLD
    if mode == "below_threshold":
        return _faker_pool.pool_eligible("city", None, n - 1, opted_out=False)
    if mode == "opt_out":
        return _faker_pool.pool_eligible("city", None, n, opted_out=True)
    if mode == "uncertified":
        return _faker_pool.pool_eligible("email", None, n, opted_out=False)
    return _faker_pool.pool_eligible("city", None, n, opted_out=False)


def compute_of(p: Probe) -> str:
    if p.kind == "sequence":
        return NO_SELECTION
    if p.kind == "categorical":
        return PYTHON_RNG
    if p.kind == "reference":
        # An empty parent returns None before any draw; `sequential` is a direct index.
        if p.parent_empty and not p.repair:
            return NO_SELECTION
        return NO_SELECTION if p.distribution == "sequential" else PYTHON_RNG
    if p.kind == "faker":
        # The gate is necessary, not sufficient: a refused pool build still runs per row.
        pooled = _pool_gate(p.faker) and p.faker not in {"build_refused", "custom"}
        return NUMPY if pooled else LIVE_PROVIDER
    if p.kind == "windowed_date":
        return WINDOWED_SHIFT
    if p.kind == "formula":
        return FORMULA
    if p.kind in {"derived", "derived_aggregate"}:
        return NO_SELECTION
    return LEGACY_SYNTHESIZE


def witness_of(p: Probe) -> tuple[str, str]:
    """(status, why). A positive value witness is granted only where one can be compared."""
    if p.kind in {"statistical", "grouped_series", "group_key"}:
        return EXCLUDED, "unobserved kind on the legacy path; proven by route and reason"
    if p.kind == "windowed_date" and not p.anchor_ok:
        return EXCLUDED, "legacy windowed_date is unobserved; proven by route and reason"
    if p.kind == "reference" and p.repair:
        return EXCLUDED, "whole-table cardinality repair rewrites the drawn keys"
    if p.kind == "reference" and p.parent_empty:
        return EXCLUDED, "every position is an intrinsic null; the vacuity fence refuses a label"
    return VALUE_EQUALITY, ""


@dataclass(frozen=True)
class AuditRow:
    row_id: str
    probe: Probe
    matches: Callable[[Probe], bool]
    admission: bool
    reason: str
    route: str
    compute: str
    materialization: tuple[str, ...]
    witness: str
    witness_why: str
    note: str


def _row(row_id: str, probe: Probe, matches: Callable[[Probe], bool], note: str) -> AuditRow:
    route = route_of(probe)
    witness, why = witness_of(probe)
    return AuditRow(
        row_id=row_id,
        probe=probe,
        matches=matches,
        admission=standalone_admitted(probe),
        reason=reason_of(probe),
        route=route,
        compute=compute_of(probe),
        # A legacy plan is always resident: a bounded sink is left untouched.
        materialization=(RESIDENT, STREAMED) if route == CORE else (RESIDENT,),
        witness=witness,
        witness_why=why,
        note=note,
    )


def _clean(p: Probe) -> bool:
    return not p.fresh and not p.plan_decliner


def _ref(p: Probe, dist: str) -> bool:
    return (
        p.kind == "reference"
        and _clean(p)
        and not p.repair
        and not p.parent_empty
        and p.distribution == dist
    )


def _faker(p: Probe, mode: str) -> bool:
    return p.kind == "faker" and _clean(p) and p.faker == mode


def _legacy_kind(p: Probe, kind: str) -> bool:
    return p.kind == kind and not p.fresh


ROWS: Final[tuple[AuditRow, ...]] = (
    _row("sequence", Probe("sequence"), lambda p: p.kind == "sequence" and _clean(p), "direct"),
    _row(
        "categorical",
        Probe("categorical"),
        lambda p: p.kind == "categorical" and _clean(p),
        "one persistent random.Random.choices stream",
    ),
    _row(
        "reference-sequential",
        Probe("reference", distribution="sequential"),
        lambda p: _ref(p, "sequential"),
        "direct index vals[i % len]; no RNG event",
    ),
    _row(
        "reference-random",
        Probe("reference", distribution="random"),
        lambda p: _ref(p, "random"),
        "random.Random.choice",
    ),
    _row(
        "reference-weighted",
        Probe("reference", distribution="weighted"),
        lambda p: _ref(p, "weighted"),
        "random.Random.choices",
    ),
    _row(
        "reference-empty-parent",
        Probe("reference", parent_empty=True),
        lambda p: p.kind == "reference" and _clean(p) and not p.repair and p.parent_empty,
        "intrinsic None, no draw",
    ),
    _row(
        "reference-cardinality-repair",
        Probe("reference", repair=True),
        lambda p: p.kind == "reference" and not p.fresh and p.repair,
        "declines to the whole-table generator",
    ),
    _row(
        "faker-pooled",
        Probe("faker", faker="pooled"),
        lambda p: _faker(p, "pooled"),
        "table row_count >= N_THRESHOLD, certified type, pool built once",
    ),
    _row(
        "faker-below-threshold",
        Probe("faker", faker="below_threshold"),
        lambda p: _faker(p, "below_threshold"),
        "row_count < N_THRESHOLD",
    ),
    _row(
        "faker-opt-out",
        Probe("faker", faker="opt_out"),
        lambda p: _faker(p, "opt_out"),
        "pooled: false",
    ),
    _row(
        "faker-uncertified",
        Probe("faker", faker="uncertified"),
        lambda p: _faker(p, "uncertified"),
        "type outside POOL_ELIGIBLE_FAKER_TYPES",
    ),
    _row(
        "faker-build-refused",
        Probe("faker", faker="build_refused"),
        lambda p: _faker(p, "build_refused"),
        "gate passes, provider resolution refuses the pool: falls to per-row. No config reaches "
        "this past admission, so the sweep injects the refusal (stand-in evidence).",
    ),
    _row(
        "faker-custom-provider",
        Probe("faker", faker="custom"),
        lambda p: p.kind == "faker" and not p.fresh and p.faker == "custom",
        "custom override: no deterministic-output contract, legacy",
    ),
    _row(
        "windowed-date",
        Probe("windowed_date"),
        lambda p: p.kind == "windowed_date" and _clean(p) and p.anchor_ok,
        "masked and unmasked; NumPy offset draw plus date arithmetic",
    ),
    _row(
        "windowed-date-declined",
        Probe("windowed_date", anchor_ok=False),
        lambda p: p.kind == "windowed_date" and not p.fresh and not p.anchor_ok,
        "anchor not earlier / ineligible / unsupported type",
    ),
    _row(
        "formula-inline",
        Probe("formula"),
        lambda p: _legacy_kind(p, "formula") and not p.referenced,
        "family declined; evaluated inline",
    ),
    _row(
        "formula-referenced",
        Probe("formula", referenced=True),
        lambda p: _legacy_kind(p, "formula") and p.referenced,
        "family declined; evaluated in the post-pass over finished siblings",
    ),
    _row("derived", Probe("derived"), lambda p: _legacy_kind(p, "derived"), "row-wise, no RNG"),
    _row(
        "derived-aggregate",
        Probe("derived_aggregate"),
        lambda p: _legacy_kind(p, "derived_aggregate"),
        "whole-column scalar, no RNG",
    ),
    _row(
        "statistical",
        Probe("statistical"),
        lambda p: _legacy_kind(p, "statistical"),
        "pinned-snapshot sampler inside synthesize",
    ),
    _row(
        "grouped-series",
        Probe("grouped_series"),
        lambda p: _legacy_kind(p, "grouped_series"),
        "group sort then restore",
    ),
    _row(
        "group-key",
        Probe("group_key"),
        lambda p: _legacy_kind(p, "group_key"),
        "group-scoped keys",
    ),
    _row(
        "determinism-fresh",
        Probe("categorical", fresh=True),
        lambda p: p.kind == "categorical" and p.fresh,
        "the fresh check precedes the kind dispatch, so the route is kind-independent",
    ),
    _row(
        "mixed-plan-forced-legacy",
        Probe("categorical", plan_decliner=True),
        lambda p: p.plan_decliner and standalone_admitted(p),
        "an admitted column whose plan holds a decliner (any table) runs the whole-table path",
    ),
)


def probe_grid() -> list[Probe]:
    """Every probed case. Parameters vary only where a predicate reads them, so each probe is a
    case the dispatch can actually distinguish."""
    from typing import get_args

    from decoy_engine.config._tables import GenerateColumnConfig

    kinds = get_args(GenerateColumnConfig.model_fields["type"].annotation)
    grid: list[Probe] = []
    for kind in kinds:
        grid.append(Probe(kind))
    grid.append(Probe("categorical", fresh=True))
    grid.append(Probe("categorical", plan_decliner=True))
    for dist in ("random", "sequential", "weighted"):
        for empty in (False, True):
            for repair in (False, True):
                grid.append(
                    Probe("reference", distribution=dist, parent_empty=empty, repair=repair)
                )
    grid.extend(Probe("faker", faker=m) for m in FAKER_MODES)
    grid.append(Probe("formula", referenced=True))
    grid.append(Probe("windowed_date", anchor_ok=False))
    return grid


def matching_rows(p: Probe) -> list[AuditRow]:
    return [r for r in ROWS if r.matches(p)]


def row(row_id: str) -> AuditRow:
    return next(r for r in ROWS if r.row_id == row_id)
