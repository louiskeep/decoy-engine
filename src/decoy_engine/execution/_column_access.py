"""Which columns each pandas dispatch surface reads or writes besides its own.

`run_mask_chunked` carries a passthrough column past pandas (see `_chunked_carry`): the
adapter sees a null placeholder and the output takes the source column back. That is only
correct when no handler reads the column and none writes it, because
`CarryPlan.reattach` would overwrite a handler's output with the source value. The read
set is therefore the union of what every dispatch surface declares here, never a scan of
config strings.

One declaration per surface, registered in `SURFACE_DECLARATIONS`:

- `scalar:<strategy>`: the 24 entries of `SCALAR_HANDLERS`.
- `composite:<provider>`: the providers `CompositeHandler` builds. The provider decides
  the surface (as in `build_work_list`), whatever `strategy` or `when:` say, because
  `_dispatch_mask_node` sends composite nodes to the handler without the when gate.
- `surface:*`: dispatch branches and adapter stages that touch no sibling column.

`tests/native/test_column_access_surfaces.py` pins the inventory against the handler
tables, the dispatcher's AST and a run of the stock adapter, so a new surface without a
declaration is a test failure. This module imports only config-level helpers (the derived
expression parser, the composite generators' output columns), never the adapter or a
handler.
"""

from __future__ import annotations

import ast
import tokenize
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# provider_config keys that name a sibling column for any strategy. Shared with
# `native/_requirements._required_input_columns` so the two cannot drift.
SIBLING_REFERENCE_KEYS: tuple[str, ...] = ("group_by", "order_by", "anchor", "reference_column")


@dataclass(frozen=True)
class ColumnAccess:
    """Columns one config entry touches besides its own value.

    `everything` means the declaration cannot be computed (an unparsable expression, a
    malformed bundle, an unknown composite provider); the caller then treats every
    passthrough column as read.
    """

    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    everything: bool = False


_NOTHING = ColumnAccess()
_EVERYTHING = ColumnAccess(everything=True)
Declaration = Callable[[Mapping[str, Any]], ColumnAccess]


def has_when(entry: Mapping[str, Any]) -> bool:
    """Same normalization the plan compiler uses: a blank predicate has no effect."""
    when = entry.get("when")
    return isinstance(when, str) and bool(when.strip())


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def predicate_names(expr: str) -> set[str] | None:
    """Every name `DataFrame.eval` could resolve in `expr`, NFKC-normalized, or None when
    the predicate cannot be read (the caller then treats every passthrough column as read).

    Uses pandas' own tokenizer. Python's `ast` NFKC-normalizes identifiers, so a fullwidth
    `x` and a `file` spelled with the U+FB01 ligature read the columns `x` and `file`.
    Only names and backtick-quoted spans are references; a string literal is a value. A
    string token that is not a plain `str` literal (an f-string prefix, a bytes literal)
    returns None, because under-collecting would hand the predicate a null placeholder
    and leave the masked column silently unmasked.
    """
    try:
        from pandas.core.computation.parsing import BACKTICK_QUOTED_STRING, tokenize_string
    except ImportError:  # pragma: no cover - a pandas without the tokenizer
        return None
    names: set[str] = set()
    try:
        for kind, value in tokenize_string(expr):
            if kind == tokenize.NAME or kind == BACKTICK_QUOTED_STRING:
                names.add(_nfkc(value))
            elif kind == tokenize.STRING and not isinstance(ast.literal_eval(value), str):
                return None
    except Exception:
        return None
    return names


def _strings(value: Any) -> frozenset[str]:
    if isinstance(value, list | tuple):
        return frozenset(v for v in value if isinstance(v, str))
    return frozenset()


def _provider_config(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    pc = entry.get("provider_config")
    return pc if isinstance(pc, Mapping) else {}


def _siblings(entry: Mapping[str, Any]) -> frozenset[str]:
    """`coherent_with` and the sibling-reference keys, which any strategy may carry."""
    pc = _provider_config(entry)
    refs = {pc[k] for k in SIBLING_REFERENCE_KEYS if isinstance(pc.get(k), str) and pc[k]}
    return _strings(entry.get("coherent_with")) | refs


def _generic(entry: Mapping[str, Any]) -> ColumnAccess:
    """A strategy with no sibling field of its own (formula applies its rule to its column)."""
    return ColumnAccess(reads=_siblings(entry))


def _derived(entry: Mapping[str, Any]) -> ColumnAccess:
    from decoy_engine.expressions import compile_expr
    from decoy_engine.transforms.derived import _get_column_refs

    expr = _provider_config(entry).get("expression")
    try:
        refs = _get_column_refs(compile_expr(str(expr))) if expr else None
    except Exception:
        refs = None
    if refs is None:
        return _EVERYTHING
    return ColumnAccess(reads=_siblings(entry) | refs)


def _derived_aggregate(entry: Mapping[str, Any]) -> ColumnAccess:
    column = _provider_config(entry).get("column")
    extra = frozenset({column}) if isinstance(column, str) and column else frozenset()
    return ColumnAccess(reads=_siblings(entry) | extra)


def _joint_mask(entry: Mapping[str, Any]) -> ColumnAccess:
    pc = _provider_config(entry)
    key = pc.get("key_by")
    reads = _siblings(entry) | (frozenset({key}) if isinstance(key, str) and key else frozenset())
    return ColumnAccess(reads=reads, writes=_strings(pc.get("columns")))


def _nested(entry: Mapping[str, Any]) -> ColumnAccess:
    """The child strategy runs on a one-column frame, but its own declaration is applied
    to the child's config, conservatively."""
    pc = _provider_config(entry)
    child = {
        "name": entry.get("name"),
        "strategy": pc.get("strategy"),
        "provider_config": pc.get("strategy_config") or {},
    }
    inner = SURFACE_DECLARATIONS.get(f"scalar:{child['strategy']}", _generic)(child)
    return _union(ColumnAccess(reads=_siblings(entry)), inner)


def _when(entry: Mapping[str, Any]) -> ColumnAccess:
    """`run_with_when_gate` evaluates the predicate over the whole frame."""
    names = predicate_names(str(entry.get("when")))
    return _EVERYTHING if names is None else ColumnAccess(reads=frozenset(names))


def _union(a: ColumnAccess, b: ColumnAccess) -> ColumnAccess:
    return ColumnAccess(
        reads=a.reads | b.reads,
        writes=a.writes | b.writes,
        everything=a.everything or b.everything,
    )


def _nothing(_entry: Mapping[str, Any]) -> ColumnAccess:
    return _NOTHING


def _composite_group(entry: Mapping[str, Any]) -> tuple[str, ...]:
    name = entry.get("name")
    members = _strings(entry.get("coherent_with")) | (
        frozenset({name}) if isinstance(name, str) else frozenset()
    )
    return tuple(sorted(members))


def _composite(outputs: Callable[[Mapping[str, Any]], frozenset[str] | None]) -> Declaration:
    def declare(entry: Mapping[str, Any]) -> ColumnAccess:
        written = outputs(entry)
        group = _composite_group(entry)
        if written is None:
            return _EVERYTHING
        # The deterministic key is the first sorted group column's source values.
        return ColumnAccess(
            reads=frozenset(group[:1]), writes=written | _strings(entry.get("coherent_with"))
        )

    return declare


def _fixed_outputs(provider: str) -> Callable[[Mapping[str, Any]], frozenset[str] | None]:
    def outputs(_entry: Mapping[str, Any]) -> frozenset[str] | None:
        # The same source the compile-time wiring check uses, so the canonical columns
        # cannot drift from what the handler writes.
        from decoy_engine.generation.composite._validate import _COMPOSITE_OUTPUT_COLUMNS

        return frozenset(_COMPOSITE_OUTPUT_COLUMNS[provider])

    return outputs


def _custom_outputs(entry: Mapping[str, Any]) -> frozenset[str] | None:
    bundle = _provider_config(entry).get("bundle") or []
    if not isinstance(bundle, list):
        return None
    columns: set[str] = set()
    for item in bundle:
        column = item.get("column") if isinstance(item, Mapping) else None
        if not isinstance(column, str) or not column.strip():
            return None
        columns.add(column)
    return frozenset(columns)


_SCALAR_STRATEGIES: tuple[str, ...] = (
    "passthrough",
    "redact",
    "truncate",
    "faker",
    "hash",
    "bucketize",
    "top_code",
    "shuffle",
    "categorical",
    "date_shift",
    "formula",
    "fpe",
    "text_redact",
    "text_mask",
    "nested",
    "joint_mask",
    "geo_generalize",
    "code_set",
    "derived",
    "bucket_perturb",
    "derived_aggregate",
    "grouped_series",
    "windowed_date",
    "group_key",
)
_FIXED_COMPOSITES: tuple[str, ...] = (
    "composite_name_email",
    "composite_city_state_zip",
    "composite_person",
    "composite_address",
    "composite_provider",
)

SURFACE_DECLARATIONS: dict[str, Declaration] = {
    **{f"scalar:{name}": _generic for name in _SCALAR_STRATEGIES},
    "scalar:derived": _derived,
    "scalar:derived_aggregate": _derived_aggregate,
    "scalar:joint_mask": _joint_mask,
    "scalar:nested": _nested,
    **{f"composite:{name}": _composite(_fixed_outputs(name)) for name in _FIXED_COMPOSITES},
    "composite:composite_custom": _composite(_custom_outputs),
    "surface:when": _when,
    # No relationship edges exist on the chunked route (`RelationshipGraph(edges=())`),
    # a composite FK group without an edge raises, and the projection and ingest-guard
    # stages read column names and own-column types only.
    "surface:fk": _nothing,
    "surface:composite_fk_group": _nothing,
    "surface:frame_setup": _nothing,
    "surface:output_projection": _nothing,
    "surface:ingest_guards": _nothing,
}


def _is_composite(provider: Any) -> bool:
    """Whether `build_work_list` makes this entry a composite node."""
    if not isinstance(provider, str) or not provider:
        return False
    from decoy_engine.execution._runner import provider_is_composite
    from decoy_engine.providers_v2 import get_default_registry

    return provider_is_composite(provider, get_default_registry())


def column_access(entry: Mapping[str, Any]) -> ColumnAccess:
    """What the pandas route reads and writes for `entry`, besides the entry's own column."""
    provider = entry.get("provider")
    if _is_composite(provider):
        declare = SURFACE_DECLARATIONS.get(f"composite:{provider}")
        return _EVERYTHING if declare is None else declare(entry)
    access = SURFACE_DECLARATIONS.get(f"scalar:{entry.get('strategy')}", _generic)(entry)
    if has_when(entry):
        access = _union(access, SURFACE_DECLARATIONS["surface:when"](entry))
    return access


def touched_columns(entries: Iterable[Mapping[str, Any]]) -> frozenset[str] | None:
    """Every column some entry reads or writes besides its own, or None for "everything"."""
    touched: set[str] = set()
    for entry in entries:
        access = column_access(entry)
        if access.everything:
            return None
        own = entry.get("name")
        touched |= {c for c in (access.reads | access.writes) if c != own}
    return frozenset(touched)
