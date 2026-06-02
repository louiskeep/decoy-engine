"""PipelineConfig: the top-level model.

Per advisory axis-by-axis ratification:
- `version: Literal[1]` (axis 6 + 3: schema version, single pipeline per file)
- `global_settings: GlobalSettings` required (axis 6: V1 naming convention kept)
- `sources: dict[str, SourceDescriptor]` (axis 1=A: inline declarations;
  empty dict permitted IFF every table is generate-kind)
- `tables: list[TableConfig]` required, non-empty
- `relationships: list[RelationshipConfig]` (empty list OK for single-table pipelines)
- `targets: dict[str, TargetDescriptor]` required (axis 6: explicit targets analogous to sources)
- `namespaces: dict[str, NamespaceConfig]` optional (the engine reads a top-level
  `namespaces` block via `config.get("namespaces", {})`; empty default is fine)

FC-1 (2026-06-02) drops the top-level `mode` discriminator. Per-table
kind is now inferred from `columns` (mask-kind) vs `generate_columns`
(generate-kind) presence; a config that lists both kinds in `tables`
is a legitimate mixed-mode submission. The engine `run_pipeline` entry
sequences the two halves (generate first so its outputs become FK
sources for the mask half).

`extra="forbid"` at every model rejects unknown keys + V1 graph-mode
keys (`nodes`, `edges`, `mode: graph` -- the deleted `mode` field is
included here: a YAML that sets `mode:` is now a typed reject pointing
at the per-table-kind shape).
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
    # FC-1 (2026-06-02): top-level `mode` discriminator dropped. Per-table
    # kind is inferred from `columns` (mask) vs `generate_columns` (generate)
    # presence on each TableConfig; a config that lists both kinds is a
    # legitimate mixed-mode submission. Pre-FC-1 the field defaulted to
    # "mask" and gated the _mode_consistency validator; both are gone.
    global_settings: GlobalSettings
    # Sources may be empty IFF every table is generate-kind. The cross-table
    # invariant validator (`_per_table_kind_consistency` below) enforces
    # this; pure-generate, pure-mask, and mixed configs all pass.
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
    def _per_table_kind_consistency(self) -> "PipelineConfig":
        """FC-1 cross-table invariants for the mixed-mode shape.

        Replaces the pre-FC-1 `_mode_consistency` validator. The contract:

        - Every table is either mask-kind (`columns` populated) or
          generate-kind (`generate_columns` populated). The per-table
          XOR is enforced by `TableConfig` already.
        - If ANY table is mask-kind, `sources` must be non-empty (each
          mask table needs its source path to read from). The pre-FC-1
          'mask mode requires sources' rule generalizes per-table:
          mask tables need source entries by name; pure-generate
          configs may omit `sources` entirely.
        - Generate tables must declare `row_count`. The pre-FC-1
          generate-mode rule generalizes the same way.

        FC-2 carries the self-FK + multi-table FK invariants; this
        validator only checks the kind+sources+row_count gating.
        """
        mask_table_names = [t.name for t in self.tables if t.columns]
        generate_tables = [t for t in self.tables if t.generate_columns]
        if mask_table_names and not self.sources:
            raise ValueError(
                "config has at least one mask-kind table "
                f"({mask_table_names[0]!r}) but no `sources:` block; "
                "mask tables require a declared source"
            )
        for table in generate_tables:
            if not isinstance(table.row_count, int) or table.row_count < 0:
                raise ValueError(
                    f"generate table {table.name!r} must declare a "
                    "non-negative integer `row_count`"
                )
        return self

    @model_validator(mode="after")
    def _reference_graph_valid(self) -> "PipelineConfig":
        """Reference relationships are valid across mask + generate tables.

        FC-1 (2026-06-02) rewrite: pre-FC-1 the validator only ran on
        pure-generate configs and enforced that reference parents
        were also generate-kind. With mixed-mode the engine resolves
        FK relationships across both kinds (a generate child can
        reference a mask parent: the generate side mints values from
        the mask side's pool; a mask child can reference a generate
        parent: the generate output becomes a source for the mask
        side). The invariants now are: every `reference_table` must
        exist in the config; every `reference_column` must exist on
        that parent; the reference graph must be acyclic.
        """
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
                # FC-1: parent may be mask-kind (columns) or generate-kind
                # (generate_columns). The reference column must exist on
                # whichever kind the parent declares.
                if parent.generate_columns:
                    parent_cols = {c.name for c in parent.generate_columns}
                elif parent.columns:
                    parent_cols = {c.name for c in parent.columns}
                else:
                    raise ValueError(
                        f"table {table.name!r}: reference column {col.name!r} "
                        f"points to {ref_table!r} which has no columns OR "
                        "generate_columns; parent must declare at least one"
                    )
                if ref_column not in parent_cols:
                    raise ValueError(
                        f"table {table.name!r}: reference column {col.name!r} "
                        f"points to {ref_table}.{ref_column!r}, but "
                        f"{ref_table!r} declares no such column"
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
