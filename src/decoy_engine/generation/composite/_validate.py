"""composite_wiring_consistent: compile-check row 8 (engine-v2 S8).

Per S8 spec §6 + cross-sprint contracts §4 row 8. For every column declaring
`coherent_with: [<col>, ...]`:

1. Each referenced column exists in the same table.
2. All columns in the coherent group route through the SAME provider (the
   composite named in the `provider` field).
3. That provider is a known composite whose canonical output_columns match the
   (sorted) coherent group.
4. The NamespaceRegistry binds the whole `(table, sorted(group))` tuple to one
   namespace (the whole-tuple form per R7; the S2 composite-auto-binding step
   creates this binding).

Raises `PlanCompileError(code='composite_wiring_inconsistent')` with a path
pointing at the offending column. Follows the established compile-check pattern
(raise on failure; row 5 `composite_columns_length_match` handles basic length
match, this is the wiring-correctness layer on top). Lives here and is wired
into `decoy_engine.plan.compile_plan` (S10 consolidates the check registry; it
does not re-implement, per R11).

Config model: a composite column declares `provider: <composite_name>` +
`coherent_with: [...]`; it does NOT use the per-column `deterministic` flag
(the composite owns determinism via generate_bundle, routed by S9), so rows 3
and 9 are unaffected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decoy_engine.generation.composite._address import CompositeAddress
from decoy_engine.generation.composite._city_state_zip import CompositeCityStateZip
from decoy_engine.generation.composite._name_email import CompositeNameEmail
from decoy_engine.generation.composite._person import CompositePerson
from decoy_engine.generation.composite._provider import CompositeProvider
from decoy_engine.plan._errors import PlanCompileError

if TYPE_CHECKING:
    from decoy_engine.relationships._namespace import NamespaceRegistry

# composite_name -> its canonical output_columns, sorted (the form the coherent
# group is compared against). MG-4 (2026-05-31) adds person / address /
# provider with fixed output_columns; composite_custom is handled separately
# because its outputs are config-defined.
_COMPOSITE_OUTPUT_COLUMNS: dict[str, tuple[str, ...]] = {
    CompositeNameEmail.composite_name: tuple(sorted(CompositeNameEmail.output_columns)),
    CompositeCityStateZip.composite_name: tuple(sorted(CompositeCityStateZip.output_columns)),
    CompositePerson.composite_name: tuple(sorted(CompositePerson.output_columns)),
    CompositeAddress.composite_name: tuple(sorted(CompositeAddress.output_columns)),
    CompositeProvider.composite_name: tuple(sorted(CompositeProvider.output_columns)),
}

# composite_custom is variable-length; its output_columns are the coherent
# group itself (declared in the YAML). The wiring check skips the
# group-vs-output-columns equality check for this composite.
_VARIABLE_OUTPUT_COMPOSITES: frozenset[str] = frozenset({"composite_custom"})


def composite_wiring_consistent(config: dict[str, Any], registry: NamespaceRegistry) -> None:
    """Compile-check row 8: validate composite (coherent_with) wiring."""
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        columns: dict[str, dict[str, Any]] = {
            c["name"]: c
            for c in table_entry.get("columns", []) or []
            if isinstance(c, dict) and "name" in c
        }
        for col_name, col_entry in columns.items():
            coherent = [c for c in (col_entry.get("coherent_with") or []) if isinstance(c, str)]
            if not coherent:
                continue
            path = f"tables.{table_name}.columns.{col_name}.coherent_with"

            # 1. Each referenced column exists in the same table.
            for ref in coherent:
                if ref not in columns:
                    raise PlanCompileError(
                        code="composite_wiring_inconsistent",
                        path=path,
                        message=(
                            f"Column {table_name}.{col_name} is coherent_with {ref!r}, "
                            f"which does not exist in table {table_name!r}."
                        ),
                    )

            group = tuple(sorted({col_name, *coherent}))

            # 2. All columns in the group route through the same provider.
            providers = {columns[c].get("provider") for c in group}
            if len(providers) != 1:
                raise PlanCompileError(
                    code="composite_wiring_inconsistent",
                    path=path,
                    message=(
                        f"Coherent group {group} in table {table_name!r} routes through "
                        f"multiple providers {sorted(str(p) for p in providers)!r}; every "
                        "column in a composite group must declare the same composite provider."
                    ),
                )
            provider = next(iter(providers))

            # 3. Provider is a known composite whose output_columns match the group.
            #    composite_custom (MG-4) is variable-length: its output_columns
            #    ARE the coherent group, so the group-vs-output-columns check
            #    is skipped for it.
            known_composites = sorted(
                set(_COMPOSITE_OUTPUT_COLUMNS) | set(_VARIABLE_OUTPUT_COMPOSITES)
            )
            if provider not in _COMPOSITE_OUTPUT_COLUMNS and provider not in _VARIABLE_OUTPUT_COMPOSITES:
                raise PlanCompileError(
                    code="composite_wiring_inconsistent",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"Column {table_name}.{col_name} declares coherent_with but its "
                        f"provider {provider!r} is not a composite generator "
                        f"({known_composites!r})."
                    ),
                )
            if provider in _COMPOSITE_OUTPUT_COLUMNS:
                expected = _COMPOSITE_OUTPUT_COLUMNS[provider]
                if expected != group:
                    raise PlanCompileError(
                        code="composite_wiring_inconsistent",
                        path=path,
                        message=(
                            f"Composite {provider!r} writes columns {expected!r} but the "
                            f"coherent group in {table_name!r} is {group!r}; they must match."
                        ),
                    )

            # 4. The registry binds the whole (table, sorted(group)) tuple.
            if registry.for_column(table_name, group) is None:
                raise PlanCompileError(
                    code="composite_wiring_inconsistent",
                    path=path,
                    message=(
                        f"Composite group ({table_name!r}, {group!r}) has no namespace "
                        "binding. build_namespace_registry should auto-bind the whole "
                        "output-column tuple to one namespace."
                    ),
                )


__all__: list[str] = ["composite_wiring_consistent"]
