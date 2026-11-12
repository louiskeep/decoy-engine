"""PipelineConfig: the top-level model.

Per advisory axis-by-axis ratification:
- `version: Literal[2]` (schema version 2; single pipeline per file)
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

import os
import re
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError, ValidationError

from decoy_engine import reasons
from decoy_engine.config._delivery import DeliveryConfig
from decoy_engine.config._generate_options import problem_details
from decoy_engine.config._generate_relational import per_parent_problems
from decoy_engine.config._generate_unique import cross_table_unique_problems
from decoy_engine.config._global_settings import GlobalSettings
from decoy_engine.config._namespaces import NamespaceConfig
from decoy_engine.config._post_scan import PostScanConfig
from decoy_engine.config._pre_scan import PreScanConfig
from decoy_engine.config._relationships import RelationshipConfig
from decoy_engine.config._sources import FileSource, SourceDescriptor
from decoy_engine.config._subset import SubsetConfig
from decoy_engine.config._tables import TableConfig
from decoy_engine.config._targets import FileTarget, TargetDescriptor
from decoy_engine.config._validators import QuarantineConfig, ValidatorEntry
from decoy_engine.contracts.declared_casting import ProtectedQuarantineConfig
from decoy_engine.errors import ConfigRuleError

_PIPELINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# Windows device names are reserved with any extension and in any case. The pattern is ASCII only,
# so the superscript 1, 2 and 3 forms (COM\u00b9 ...) already fail it; COM0 and LPT0 are not reserved.
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def is_valid_pipeline_name(value: object) -> bool:
    """The one pipeline-name rule: a folder-safe slug on every platform.

    The engine owns the `name:` field and the CLI checks folder names and file stems with this
    same function, so a name that loads here also joins safely under `pipelines/`. A non-string
    is never valid; nothing is coerced.
    """
    if not isinstance(value, str) or _PIPELINE_NAME.fullmatch(value) is None:
        return False
    if value.endswith("."):
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_DEVICES


class PipelineConfig(BaseModel):
    """Strict, validated pipeline configuration.

    Callers do:

        cfg_dict = PipelineConfig.model_validate(parsed_yaml).model_dump()

    and hand `cfg_dict` to `profile_source` and `compile_plan`. The
    engine functions do not re-validate. Validation is a one-time event
    at the choke-point.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: Literal[2]
    # Optional pipeline name (PROJECT-LAYOUT). Strict: a non-string is refused, never coerced.
    # Unset stays out of dumps and hashes, so a config without it is byte-identical to before.
    name: str | None = None
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
    # Pre-scan detector choice (regex always; classifier and ner opt in). Equal to the default
    # it stays out of dumps and hashes, so a legacy config is unchanged.
    pre_scan: PreScanConfig = Field(default_factory=PreScanConfig)
    # Post-scan controls. `post_scan.integrity` runs the post-mask integrity suite
    # (report-only) over the finished output; omitted -> disabled.
    post_scan: PostScanConfig = Field(default_factory=PostScanConfig)
    # SP-05 (2026-06-27): job-level validator framework (P5.INFRA.4).
    # Each entry names a validator and supplies its per-validator config.
    # The engine runs them after all column passes complete. Default empty
    # list -> no validators run -> zero overhead on existing pipelines.
    validators: list[ValidatorEntry] = Field(default_factory=list)
    # SP-05 (2026-06-27): quarantine block (P5.B.quarantine_rows).
    # When enabled and a trigger fires, the offending row is written to
    # output_path instead of the main output; the job continues successfully.
    # Default None -> quarantine disabled -> fail-closed on validator failures.
    quarantine: QuarantineConfig | None = None
    # Sprint G (2026-07-03): FK-aware subsetting pre-mask stage. None means
    # "no subsetting" (the pipeline masks its full sources, unchanged
    # behavior). No run applies the block yet: every run entry refuses a
    # config that sets it (`config/_subset_run.py`) until SUB-RUN wires
    # `decoy_engine.subset.run_subset` in ahead of masking. There is no config
    # syntax for the reverse order (subset-then-mask is structural, not a
    # flag). See `_subset_stage_constraints` below for the one rejectable
    # mask-then-subset shape.
    subset: SubsetConfig | None = None
    # F06 (declared parsing/casting): the one protected-quarantine declaration. Required by any
    # column with `error_policy: quarantine`; there is deliberately no path or key here, because
    # the store is a runtime capability. Unset stays out of dumps (legacy hashes are unchanged).
    protected_quarantine: ProtectedQuarantineConfig | None = None
    # How the run's outputs become visible. Equal to the default it stays out of dumps and
    # hashes, so a legacy config is unchanged.
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        from decoy_engine.config._declared_casting import recover_serialization_schema

        return recover_serialization_schema(core_schema, handler)

    @model_serializer(mode="wrap")
    def _omit_unset_optional_blocks(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("name") is None:
            data.pop("name", None)
        if data.get("protected_quarantine") is None:
            data.pop("protected_quarantine", None)
        # By value, not by what was written: a pre_scan equal to the default is the legacy shape.
        if self.pre_scan == PreScanConfig():
            data.pop("pre_scan", None)
        if self.delivery == DeliveryConfig():
            data.pop("delivery", None)
        return data

    @field_validator("name", mode="before")
    @classmethod
    def _name_is_a_valid_slug(cls, value: object) -> object:
        """Refuse anything but a rule-passing string. The error never carries the submitted text."""
        if value is None or is_valid_pipeline_name(value):
            return value
        raise PydanticCustomError(
            str(reasons.config.PIPELINE_NAME_INVALID), "name is not an acceptable pipeline name."
        )

    @model_validator(mode="after")
    def _f06_protected_quarantine_required(self) -> PipelineConfig:
        from decoy_engine.config._declared_casting import require_protected_block

        require_protected_block(self)
        return self

    @model_validator(mode="after")
    def _per_table_kind_consistency(self) -> PipelineConfig:
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
            raise ConfigRuleError(
                "config has at least one mask-kind table "
                f"({mask_table_names[0]!r}) but no `sources:` block; "
                "mask tables require a declared source"
            )
        for table in generate_tables:
            if not isinstance(table.row_count, int) or table.row_count < 0:
                raise ConfigRuleError(
                    f"generate table {table.name!r} must declare a non-negative integer `row_count`"
                )
        return self

    @model_validator(mode="after")
    def _reference_graph_valid(self) -> PipelineConfig:
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
                    raise ConfigRuleError(
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
                    # QA finding fix (2026-06-02, engine FC-1 review
                    # Finding 2): the generate-child -> mask-parent FK
                    # direction is deferred to V2.1 per the engine
                    # execution/_pipeline.py module docstring ("Generate
                    # child to mask parent FK direction... the resolution
                    # path is V2.1"). The synthesize pipeline raises a
                    # plain ValueError on this case at runtime, which
                    # the platform's typed-exception handler does not
                    # catch -- the job hangs in `running`. Reject here
                    # at validation time so the operator sees a clear
                    # error at submit, not a hung job.
                    raise ConfigRuleError(
                        f"table {table.name!r}: reference column {col.name!r} "
                        f"points to mask-kind table {ref_table!r}; generate-to-mask "
                        "FK is deferred to V2.1. Use a generate parent, or wait "
                        "for V2.1."
                    )
                else:
                    raise ConfigRuleError(
                        f"table {table.name!r}: reference column {col.name!r} "
                        f"points to {ref_table!r} which has no columns OR "
                        "generate_columns; parent must declare at least one"
                    )
                if ref_column not in parent_cols:
                    raise ConfigRuleError(
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
            stack: list[tuple[str, Iterator[str]]] = []
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
                    cycle = " -> ".join([*path[path.index(parent_name) :], parent_name])
                    raise ConfigRuleError(f"reference cycle in generate config: {cycle}")
                if ps == WHITE:
                    state[parent_name] = GRAY
                    path.append(parent_name)
                    stack.append((parent_name, iter(deps.get(parent_name, ()))))
        return self

    @model_validator(mode="after")
    def _per_parent_bounds_valid(self) -> PipelineConfig:
        """GEN-REL: a reference column's per-parent bounds must be able to hold.

        Runs after the reference graph is known valid. Only the maximum is judged against the
        parent's declared `row_count`; the run re-checks both bounds on the real key count.
        """
        if not any(
            (c.model_extra or {}).get("min_per_parent")
            or (c.model_extra or {}).get("max_per_parent")
            for t in self.tables
            for c in t.generate_columns
        ):
            return self
        problems = per_parent_problems(
            [
                {
                    "name": t.name,
                    "row_count": t.row_count,
                    "generate_columns": [
                        {**(c.model_extra or {}), "name": c.name, "type": c.type}
                        for c in t.generate_columns
                    ],
                }
                for t in self.tables
            ]
        )
        if problems:
            raise ValidationError.from_exception_data("PipelineConfig", problem_details(problems))
        return self

    @model_validator(mode="after")
    def _unique_domains_valid(self) -> PipelineConfig:
        """GEN-UNIQUE: a unique reference or junction pair cannot exceed its parents' row counts.

        Uses each parent's declared `row_count`, an upper bound on its key count. The real
        distinct key count is known only after generation, and the run re-checks it.
        """
        if not any(
            t.unique_together
            or any((c.model_extra or {}).get("unique") is True for c in t.generate_columns)
            for t in self.tables
        ):
            return self
        tables = [
            {
                "name": t.name,
                "row_count": t.row_count,
                "unique_together": t.unique_together,
                "generate_columns": [
                    {**(c.model_extra or {}), "name": c.name, "type": c.type}
                    for c in t.generate_columns
                ],
            }
            for t in self.tables
        ]
        problems = cross_table_unique_problems(tables)
        if problems:
            raise ValidationError.from_exception_data("PipelineConfig", problem_details(problems))
        return self

    @model_validator(mode="after")
    def _subset_stage_constraints(self) -> PipelineConfig:
        """Sprint G: validate the `subset:` block against the rest of the config.

        1. Every `subset.seeds[].table` must be a declared mask-kind table
           with a source (subsetting operates on the same tables masking
           reads).
        2. Every source referenced by a subsetted or relationship-bearing
           table must be `format == "parquet"` -- a submit-time mirror of
           preflight check 5.0, so the operator sees the reject at
           validation time, not at run time.
        3. Subset-after-mask rejection: the run path is structurally
           subset-stage -> mask-stage; there is no syntax to express
           mask-then-subset within one config. The one shape that WOULD
           express it is pointing the subset job's sources at this same
           config's mask targets -- reject that.
        """
        if self.subset is None:
            return self

        mask_table_names = {t.name for t in self.tables if t.columns}
        for seed in self.subset.seeds:
            if seed.table not in mask_table_names:
                raise ConfigRuleError(
                    f"subset seed names table {seed.table!r}, which is not a declared "
                    "mask-kind table with a source in this config"
                )

        tables_needing_parquet = {seed.table for seed in self.subset.seeds}
        for rel in self.relationships:
            tables_needing_parquet.add(rel.parent.table)
            tables_needing_parquet.update(c.table for c in rel.children)
        for source_name in tables_needing_parquet:
            src = self.sources.get(source_name)
            if src is not None and src.format != "parquet":
                raise ConfigRuleError(
                    f"subsetting is configured but source {source_name!r} is "
                    f"{src.format!r}; convert to Parquet for subsetting"
                )

        target_paths = {
            os.path.normpath(t.path) for t in self.targets.values() if isinstance(t, FileTarget)
        }
        for src in self.sources.values():
            if not isinstance(src, FileSource):
                continue
            if os.path.normpath(src.path) in target_paths:
                raise ConfigRuleError(
                    f"subset input {src.path!r} is also a mask target of this pipeline; "
                    "subsetting runs BEFORE masking (subset-then-mask), it cannot consume "
                    "masked output"
                )
        return self
