"""PipelineConfig: the top-level model.

Per advisory axis-by-axis ratification:
- `version: Literal[1]` (axis 6 + 3: schema version, single pipeline per file)
- `global_settings: GlobalSettings` required (axis 6: V1 naming convention kept)
- `sources: dict[str, SourceDescriptor]` required (axis 1=A: inline declarations)
- `tables: list[TableConfig]` required, non-empty
- `relationships: list[RelationshipConfig]` (empty list OK for single-table pipelines)
- `targets: dict[str, TargetDescriptor]` required (axis 6: explicit targets analogous to sources)
- `namespaces: dict[str, NamespaceConfig]` optional (the engine reads a top-level
  `namespaces` block via `config.get("namespaces", {})`; empty default is fine)

`extra="forbid"` at every model rejects unknown keys + V1 graph-mode
keys (`nodes`, `edges`, `mode: graph`) per axis 6 (no V1 compat).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from decoy_engine.config._global_settings import GlobalSettings
from decoy_engine.config._namespaces import NamespaceConfig
from decoy_engine.config._relationships import RelationshipConfig
from decoy_engine.config._sources import SourceDescriptor
from decoy_engine.config._tables import TableConfig
from decoy_engine.config._targets import TargetDescriptor


class PipelineConfig(BaseModel):
    """Strict, validated pipeline configuration.

    Callers do:

        cfg_dict = PipelineConfig.model_validate(parsed_yaml).model_dump()

    and hand `cfg_dict` to `profile_source` and `compile_plan`. The
    engine functions do not re-validate. Validation is a one-time event
    at the choke-point.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    # S6 (generation): mask vs generate. Mask configs OMIT it (default "mask"), so
    # existing stored configs + the run dispatch (`cfg.get("mode","mask")`) are
    # unchanged; a generate submission sets `mode: generate`.
    mode: Literal["mask", "generate"] = "mask"
    global_settings: GlobalSettings
    # Relaxed from min_length=1: a pure-generate config has NO sources. The
    # validator below keeps MASK mode requiring at least one source, so the mask
    # contract is unchanged.
    sources: dict[str, SourceDescriptor] = Field(default_factory=dict)
    tables: list[TableConfig] = Field(min_length=1)
    relationships: list[RelationshipConfig] = Field(default_factory=list)
    targets: dict[str, TargetDescriptor] = Field(min_length=1)
    namespaces: dict[str, NamespaceConfig] = Field(default_factory=dict)
    # Reframe-A (2026-05-31): per-pipeline opt-in for the Storm post-mask
    # check. When True, the platform runner fires the storm.postmask hook
    # after a successful mask job + persists the JobStormReport row. The
    # engine validates the shape; the engine does NOT consume the value at
    # run time -- the platform runner reads it. Default False so existing
    # pipelines are unchanged (run_storm omitted -> False; no new behavior).
    # Per PO lock 2026-05-30 docs/audit/po-decisions-storm-reframe-2026-05-30.md.
    run_storm: bool = False

    @model_validator(mode="after")
    def _mode_consistency(self) -> "PipelineConfig":
        """Keep `mode` consistent with the tables + sources.

        Mask mode requires a source and mask tables; generate mode requires
        generate tables (no mask columns). This preserves the mask contract
        (sources required) while admitting a no-source generate config.
        """
        if self.mode == "mask":
            if not self.sources:
                raise ValueError("mask mode requires at least one source")
            if any(t.generate_columns for t in self.tables):
                raise ValueError(
                    "mask mode tables must not declare generate_columns "
                    "(use mode: generate)"
                )
        else:  # generate
            if any(t.columns for t in self.tables):
                raise ValueError(
                    "generate mode tables must use generate_columns, not mask columns"
                )
        return self

    @model_validator(mode="after")
    def _reference_graph_valid(self) -> "PipelineConfig":
        """In generate mode: every ``reference_table`` named on a generate column must
        resolve to a generate-mode table in this config; every ``reference_column``
        must exist on that parent; the reference graph must be acyclic so the engine
        can topo-sort parent-then-child generation. Mirrors a SQL DDL FK contract
        (referenced parents must exist, no cycles)."""
        if self.mode != "generate":
            return self
        by_name = {t.name: t for t in self.tables}
        # Build dep graph: table_name -> set of parent table names referenced by
        # any of its `reference`-typed generate columns.
        deps: dict[str, set[str]] = {}
        for table in self.tables:
            d: set[str] = set()
            for col in table.generate_columns:
                if col.type != "reference":
                    continue
                extras = col.model_extra or {}
                ref_table = extras.get("reference_table")
                ref_column = extras.get("reference_column")
                if ref_table not in by_name:
                    raise ValueError(
                        f"table {table.name!r}: reference column {col.name!r} "
                        f"points to unknown table {ref_table!r}"
                    )
                parent = by_name[ref_table]
                if not parent.generate_columns:
                    raise ValueError(
                        f"table {table.name!r}: reference column {col.name!r} "
                        f"points to mask-mode table {ref_table!r}; a reference "
                        f"requires a generate-mode parent"
                    )
                parent_cols = {c.name for c in parent.generate_columns}
                if ref_column not in parent_cols:
                    raise ValueError(
                        f"table {table.name!r}: reference column {col.name!r} "
                        f"points to {ref_table}.{ref_column!r}, but "
                        f"{ref_table!r} declares no such generate_column"
                    )
                d.add(ref_table)
            deps[table.name] = d
        # Detect cycles via DFS three-color marking.
        # QA walks/generators F4 (2026-06-01, HIGH reliability): iterative
        # DFS with an explicit stack mirrors the rewrite in
        # `walks/hazards.py::_detect_cycles`. Pre-fix a chain of >1000
        # tables (or any cycle of depth >1000) raised Python's default
        # RecursionError at config-load time. Pipeline configs that
        # large are uncommon, but config validation must never fail
        # for stack-depth reasons. Iterative DFS produces identical
        # cycle-detection semantics.
        WHITE, GRAY, BLACK = 0, 1, 2
        state = {n: WHITE for n in deps}

        for start in list(deps):
            if state[start] != WHITE:
                continue
            stack: list[tuple[str, "Iterator[str]"]] = []
            path: list[str] = []

            state[start] = GRAY
            path.append(start)
            stack.append((start, iter(deps[start])))

            while stack:
                n, parents_iter = stack[-1]
                try:
                    parent_name = next(parents_iter)
                except StopIteration:
                    stack.pop()
                    path.pop()
                    state[n] = BLACK
                    continue
                ps = state.get(parent_name, WHITE)
                if ps == GRAY:
                    idx = path.index(parent_name)
                    cycle = path[idx:] + [parent_name]
                    raise ValueError(
                        f"reference cycle in generate config: {' -> '.join(cycle)}"
                    )
                if ps == WHITE:
                    state[parent_name] = GRAY
                    path.append(parent_name)
                    stack.append((parent_name, iter(deps.get(parent_name, ()))))
        return self
