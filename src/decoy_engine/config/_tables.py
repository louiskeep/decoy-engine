"""TableConfig + ColumnConfig: per-table masking configuration.

S1 spec lines 154-165 verbatim:
  - name (str)
  - columns: list of ColumnConfig with name + strategy + provider +
    optional namespace + optional cardinality_mode + optional
    provider_config (free-form dict, shape per S4 provider registry) +
    optional coherent_with (composite-aware).

Strategy and provider strings are NOT closed Literals here. Reasons:
- `strategy` (e.g. "preserve_format_id", "synthetic_email",
  "replace_with_synthetic", "from_parent") is the strategy table from
  the operating model; it grows as new strategies land. Closing it now
  would require updating this file every time S4-S13 add a strategy.
- `provider` is closed-checked by the planner against `S1_STUB_REGISTRY`
  (S4 swaps the real registry behind the same check). That check fires
  with `code=unknown_provider` per S1 spec §2; the adapter does NOT
  duplicate it. Single source of truth lives at the planner.

`cardinality_mode` IS a closed Literal here because the set of valid
modes is locked by the operating model and S1 spec §2 enumerates them
exhaustively. Per the R6 reshape (S5), `deterministic_map` was removed
from the mode set; the deterministic-vs-random axis is now the separate
first-class `deterministic: bool` field, composed orthogonally with
`cardinality_mode`. The adapter rejects a `deterministic_map` mode here
(the engine also raises a migration error on the raw-dict path).
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError, ValidationError

from decoy_engine import reasons
from decoy_engine.config._generate_option_catalog import POOLED_TYPE, closed_key_schema
from decoy_engine.config._generate_options import option_problems, problem_details
from decoy_engine.config._generate_unique import (
    column_conflict_problems,
    table_unique_problems,
    unique_type_problems,
)
from decoy_engine.config._transforms import TransformOp
from decoy_engine.errors import ConfigRuleError

CardinalityModeLiteral = Literal[
    "reuse",
    "unique",
    "match_source_cardinality",
    "scale_source_cardinality",
]


class ColumnConfig(BaseModel):
    """Per-column masking declaration.

    `name` is the column name in the source data. `strategy` names the
    masking strategy (open string; S4+ catalogs the values). `provider`
    names the semantic provider; the planner validates it against
    S1_STUB_REGISTRY at compile time.

    `from_parent` strategy: the column is wired from a FK parent via the
    relationship coordinator (S2). When `strategy: from_parent`, the
    relationship coordinator owns the namespace + provider; ColumnConfig
    declares only the strategy + name.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str
    strategy: str
    provider: str | None = None
    namespace: str | None = None
    cardinality_mode: CardinalityModeLiteral | None = None
    # R6 (S5): deterministic-vs-random is a first-class field, orthogonal to
    # cardinality_mode (the 2x4 matrix in S5 spec §6). The engine reads it via
    # `col_entry.get("deterministic", False)`; defaults False when omitted.
    deterministic: bool = False
    # Read by pool/_validate.py (`float(col_entry.get("scale", 2.0))`) under
    # cardinality_mode == "scale_source_cardinality"; the planner owns the
    # scale-vs-mode interaction, so the adapter just carries the value.
    scale: float | None = None
    # provider_config is a free-form dict; S4's real provider registry
    # validates the shape per-backend. Adapter just enforces it's a dict.
    provider_config: dict[str, Any] = Field(default_factory=dict)
    coherent_with: list[str] = Field(default_factory=list)
    backend_type: Literal["faker", "mimesis", "pool", "decoy_native"] | None = None
    backend_version: str | None = None
    # Capacity hint for the planner's basic_uniqueness_pre_flight check
    # (S1 spec §2 #4). Only meaningful when cardinality_mode == "unique"
    # and backend_type == "pool"; the planner reads this directly from
    # the column dict, so the adapter must allow it. S5 ships the full
    # pool_capacity_pre_flight check.
    pool_size: int | None = None
    # Token-vault opt-in (deferred follow-up 1, 2026-06-12): record this
    # column's source->masked map into the encrypted vault artifact when
    # the run is given a vault path. Requires a namespace (the vault's
    # lookup key) and a one-way strategy (fpe is already reversible);
    # check_vault_columns enforces both at compile.
    vault: bool = False
    # Intentional-collisions knob (gap-closure item 2). Documented operator-facing
    # alias for Delphix Secure Lookup's collision-allowed semantics: at compile it
    # forces `cardinality_mode = reuse` + `deterministic = true`, yielding a stable
    # many-to-one masked mapping (distinct sources may share a masked value, the way
    # natural data recurs). Requires a namespace (the derive key) and conflicts with
    # any non-`reuse` cardinality_mode; the planner enforces both.
    allow_collisions: bool = False
    # DE-10 reland (2026-07-13): declared dtype FAMILY of this column's raw
    # source value (e.g. "int64", "float64", "string" -- coarsened by
    # `_chunked_fk._dtype_family`, exact width does not matter). Optional and
    # unused by every OTHER route; `_chunked_fk.gate_fk_child_edges`'s
    # condition (f) reads it off BOTH sides of a chunked-self-masking FK edge
    # to prove a value-sensitive strategy (hash/fpe/date_shift/bucketize/
    # truncate/text_redact) will mask the child's own value to the SAME bytes
    # as the parent's, since that route never sees the parent's data to check
    # at runtime. Without this field declared here, `extra="forbid"` above
    # made the gate's dtype check (and its two admission-error codes) provably
    # UNREACHABLE through `PipelineConfig.model_validate(...).model_dump()` --
    # any config that set `dtype` failed Pydantic's `extra_forbidden` before
    # reaching the gate, and any config that omitted it hit the "unprovable"
    # rejection unconditionally, so only a hand-built raw dict (bypassing
    # validation, as the gate's own unit tests did) could ever exercise it.
    dtype: str | None = None
    # Per-row gate: only rows where the predicate is true are masked. Accepts the closed
    # `when` grammar only (see `expressions/_when_parser`); a blank value means no gate.
    when: str | None = None
    # F06 declared parsing/casting contract (see `decoy_engine.contracts.declared_casting`).
    # All five stay unset for a legacy column and are then omitted from dumps, so configs
    # that do not use them hash and serialize exactly as before the fields existed.
    logical_type: str | None = None
    parse_format: Any = None
    sentinel_to_null: Any = None
    error_policy: str | None = None
    output_format: Any = None

    @field_validator("logical_type", mode="before")
    @classmethod
    def _f06_logical(cls, value: Any) -> Any:
        from decoy_engine.config import _declared_casting as dc

        return dc.closed_choice(value, dc.LOGICAL_TYPES, "logical_type")

    @field_validator("error_policy", mode="before")
    @classmethod
    def _f06_policy(cls, value: Any) -> Any:
        from decoy_engine.config import _declared_casting as dc

        return dc.closed_choice(value, dc.ERROR_POLICIES, "error_policy")

    @field_validator("parse_format", mode="before")
    @classmethod
    def _f06_parse(cls, value: Any) -> Any:
        from decoy_engine.config import _declared_casting as dc

        return dc.typed_format(value, "parse")

    @field_validator("output_format", mode="before")
    @classmethod
    def _f06_output(cls, value: Any) -> Any:
        from decoy_engine.config import _declared_casting as dc

        return dc.typed_format(value, "output")

    @field_validator("sentinel_to_null", mode="before")
    @classmethod
    def _f06_sentinel(cls, value: Any) -> Any:
        from decoy_engine.config import _declared_casting as dc

        return dc.typed_sentinel(value)

    @model_validator(mode="after")
    def _f06_contract(self) -> ColumnConfig:
        from decoy_engine.config import _declared_casting as dc

        default_policy = dc.check_column(self)
        if default_policy is not None:
            self.error_policy = default_policy
        return self

    @field_validator("when")
    @classmethod
    def _when_closed_grammar(cls, value: str | None) -> str | None:
        """Validate the predicate against the closed grammar at config time.

        The engine's own `ValidationError` is not a `ValueError`, so it would escape
        pydantic and bypass the structured handler; the failure is re-raised as a
        `PydanticCustomError` so it lands in the model's `ValidationError` with its location.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        from decoy_engine.errors import ValidationError as EngineValidationError
        from decoy_engine.expressions._when_parser import WHEN_OUTSIDE_GRAMMAR_CODE, parse_when

        try:
            parse_when(stripped)
        except EngineValidationError as exc:
            raise PydanticCustomError(
                WHEN_OUTSIDE_GRAMMAR_CODE, "{reason}", {"reason": exc.raw_message}
            ) from exc
        return stripped

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        """Keep the serialization-mode JSON schema field-enumerating.

        The wrap serializer below makes pydantic report this model's serialization schema as a
        generic object; asking again without the `serialization` entry recovers the field
        schema, as `GlobalSettings` does for its `dp` serializer.
        """
        from decoy_engine.config._declared_casting import recover_serialization_schema

        return recover_serialization_schema(core_schema, handler)

    @model_serializer(mode="wrap")
    def _omit_unset_when(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # An unset `when` stays out of dumps, so configs without it serialize and hash exactly
        # as before the field existed, and an older engine can re-validate them.
        data: dict[str, Any] = handler(self)
        if data.get("when") is None:
            data.pop("when", None)
        for key in (
            "logical_type",
            "parse_format",
            "sentinel_to_null",
            "error_policy",
            "output_format",
        ):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class GenerateColumnConfig(BaseModel):
    """A synthesis (generation) column declaration (S6).

    Mirrors V1's generate column shape (decoy_engine.generators.columns): ``type``
    selects the generator, and the per-type options (``faker_type``, ``start``,
    ``weights``, ...) ride FLAT on the column, exactly as V1 reads them
    (``column_config.get("faker_type")``).

    ``extra="allow"`` carries those flat options so the front-end emit and the V1 parity
    oracle line up, and so the dump and the seed fingerprint (which hashes every dumped key)
    do not change. The key set is not open: ``_options_valid`` checks every extra against the
    per-type registry in ``config/_generate_options.py`` and refuses an unknown key, a missing
    required key or a bad static value, and the JSON schema closes each type the same way.
    """

    model_config = ConfigDict(
        extra="allow", hide_input_in_errors=True, json_schema_extra=closed_key_schema
    )

    name: str
    # The closed set of generators v2 supports. Reading B: this mirrors V1
    # ColumnGenerator.generators (faker/sequence/categorical/formula/reference).
    # `reference` lands in S6-ENG-3 as the column-level mint-a-pool FK path.
    # `statistical` (capability-gaps WS3, 2026-06-12) samples from a
    # distribution-snapshot/v1 artifact; see generation/statistical.
    # `derived` (SP-10 / P5.S.derived, 2026-06-28) evaluates a closed-grammar
    # expression over already-generated sibling columns declared before it in
    # generate_columns (declared-order sequential semantics).
    type: Literal[
        "faker",
        "sequence",
        "categorical",
        "formula",
        "reference",
        "statistical",
        "derived",
        "derived_aggregate",
        "grouped_series",
        "windowed_date",
        "group_key",
        "constant",
        "distribution",
        "lookup",
    ]

    # GP2 (Codex round-2 spec A/G7): the ONLY validated `type: faker` knob.
    # `None` (omitted) and explicit `False` are both "no pool" -- generation
    # auto-pools an eligible column above threshold either way; `False` is
    # the one operator-facing opt-out. `StrictBool` (not plain `bool`) so a
    # coercible non-bool -- `"false"`, `"yes"`, `0`, `1` -- is REJECTED at
    # validation rather than silently coerced into enabling/disabling pooling
    # (Codex FINAL gate, 2026-09-17: plain `bool | None` coerced `"false"` to
    # `False` and quietly turned pooling off on a 50k eligible column).
    # `strategy_config_fingerprint` excludes this field on purpose (derivation.py):
    # it is a build-strategy toggle, not a value-affecting strategy knob, so a
    # column with `pooled` unset keeps the exact pre-GP2 seed root and bytes.
    pooled: StrictBool | None = None

    @model_validator(mode="after")
    def _options_valid(self) -> GenerateColumnConfig:
        """Check the flat per-type options against the registry and report every problem at once.

        Unknown keys, missing required keys and bad static values come from
        ``config/_generate_options.py``. ``extra="allow"`` stays so the options ride flat and the
        dump is unchanged; this validator only reads ``model_extra``.
        """
        extras = self.model_extra or {}
        problems = option_problems(self.type, extras)
        # `unique` is accepted per type, and cannot combine with the options that sample with
        # replacement. `pooled` is a declared field, so it is passed in.
        problems += unique_type_problems(self.type, extras)
        problems += column_conflict_problems(self.type, extras, pooled=self.pooled)
        # `pooled` is a faker-only build-strategy toggle. `is not None`, not field presence:
        # `model_dump()` stamps `pooled: null` on every generate column, so a dumped non-faker
        # column must revalidate. With StrictBool only a real bool reaches here.
        if self.type != POOLED_TYPE and self.pooled is not None:
            problems.append((("pooled",), str(reasons.config.GENERATE_OPTION_UNKNOWN)))
        if problems:
            raise ValidationError.from_exception_data(
                "GenerateColumnConfig", problem_details(problems)
            )
        return self


# Live registry of valid generate-column ``type`` values, derived from the
# ``GenerateColumnConfig.type`` Literal above (the single validation authority
# the ``generation.synthesize._generate_column`` dispatch mirrors).  Adding a
# generator type to the Literal surfaces it here automatically -- no second
# hand-maintained list.  The test-flight coverage guard reads this as the live
# source of its generate-type axis (TH-1.2 / P0-2).
GENERATE_TYPES: frozenset[str] = frozenset(
    get_args(GenerateColumnConfig.model_fields["type"].annotation)
)


class TableConfig(BaseModel):
    """Per-table declaration.

    A table is EITHER a MASK table (``columns`` of mask ColumnConfig, fed by a
    source) OR a GENERATE table (``generate_columns`` + ``row_count``, no source),
    enforced by the validator below. ``columns`` was ``min_length=1``; it is now
    validated CONDITIONALLY so a generate table can omit it. The mask path is
    unchanged when no generation fields are set.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str
    columns: list[ColumnConfig] = Field(default_factory=list)
    # Generation (S6): row_count is V1's per-table `rows`; generate_columns are the
    # synthesis column specs. Both unset => a mask table.
    row_count: int | None = None
    generate_columns: list[GenerateColumnConfig] = Field(default_factory=list)
    # S17-TX-NARROW (2026-05-30): the narrow transform surface (Endpoint A
    # locked). Six ops execute between source-read and the strategy loop in
    # the mask path. Empty by default. Generate tables MAY also carry
    # transforms (they apply post-synthesis); current S17 scope wires only
    # the mask side (Phase B + a follow-up enables generate-side transforms).
    # The cap bounds derived-column width growth (each derive can add a column).
    transforms: list[TransformOp] = Field(default_factory=list, max_length=32)
    # GEN-UNIQUE: pairs of `reference` columns of this generate table whose value pairs must not
    # repeat (SQL `UNIQUE (a, b)`). Omitted from dumps when empty, so existing configs keep their
    # serialized bytes.
    unique_together: list[list[str]] = Field(default_factory=list)

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        """Keep the serialization-mode JSON schema field-enumerating (see `_omit_empty_pairs`)."""
        from decoy_engine.config._declared_casting import recover_serialization_schema

        return recover_serialization_schema(core_schema, handler)

    @model_serializer(mode="wrap")
    def _omit_empty_pairs(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not data.get("unique_together"):
            data.pop("unique_together", None)
        return data

    @model_validator(mode="after")
    def _generate_names_unique(self) -> TableConfig:
        """Two generate columns with one name would silently drop one at run time."""
        seen: set[str] = set()
        problems: list[tuple[tuple[str | int, ...], str]] = []
        for index, col in enumerate(self.generate_columns):
            if col.name in seen:
                problems.append(
                    (
                        ("generate_columns", index, "name"),
                        str(reasons.config.GENERATE_COLUMN_DUPLICATE),
                    )
                )
            seen.add(col.name)
        if problems:
            raise ValidationError.from_exception_data("TableConfig", problem_details(problems))
        return self

    @model_validator(mode="after")
    def _unique_rules(self) -> TableConfig:
        """`unique_together` shape and categorical domain sizes (GEN-UNIQUE, per table)."""
        if not self.unique_together and not any(
            (c.model_extra or {}).get("unique") is True for c in self.generate_columns
        ):
            return self
        table = {
            "row_count": self.row_count,
            "unique_together": self.unique_together,
            "generate_columns": [
                {**(c.model_extra or {}), "name": c.name, "type": c.type}
                for c in self.generate_columns
            ],
        }
        problems = table_unique_problems(table)
        if problems:
            raise ValidationError.from_exception_data("TableConfig", problem_details(problems))
        return self

    @model_validator(mode="after")
    def _mask_xor_generate(self) -> TableConfig:
        if self.generate_columns:
            if self.columns:
                raise ConfigRuleError(
                    f"table {self.name!r}: a generate table (generate_columns) must "
                    f"not also declare mask columns"
                )
            if self.row_count is None or self.row_count < 0:
                raise ConfigRuleError(
                    f"table {self.name!r}: a generate table requires a non-negative row_count"
                )
        else:
            if not self.columns:
                raise ConfigRuleError(
                    f"table {self.name!r}: a mask table requires at least one column "
                    f"(or use generate_columns + row_count to generate)"
                )
            if self.row_count is not None:
                raise ConfigRuleError(
                    f"table {self.name!r}: row_count is only valid on a generate table "
                    f"(with generate_columns)"
                )
        # S17 Phase B (Dennis S17 gate MEDIUM finding, 2026-05-30): cross-check
        # drop_column transforms against the table's mask columns. A user who
        # writes `drop_column: [ssn]` while ALSO declaring a mask strategy on
        # `ssn` would otherwise see a generic `v2_runner_unexpected_error`
        # mid-strategy when the column is missing. Catch it at the choke-point
        # with a typed reason naming the affected column + strategy.
        if self.transforms and self.columns:
            from decoy_engine.config._transforms import DropColumnOp

            mask_col_names = {c.name for c in self.columns}
            for op in self.transforms:
                if isinstance(op, DropColumnOp):
                    conflicts = mask_col_names & set(op.columns)
                    if conflicts:
                        raise ConfigRuleError(
                            f"table {self.name!r}: drop_column transform drops columns "
                            f"{sorted(conflicts)} that also have mask strategies declared. "
                            f"Either drop the columns OR mask them, not both -- mask "
                            f"strategies run AFTER transforms, so the strategy would land "
                            f"on a missing column and fail mid-run."
                        )
        return self


def classify_table_kinds(config: dict[str, Any]) -> dict[str, str]:
    """Return `{table_name: "mask" | "generate"}` for every table in the config.

    Per-table kind is inferred from `columns` (mask) vs `generate_columns`
    (generate) presence on each TableConfig. The schema already enforces
    XOR at validation time (`_per_table_kind_consistency` + `TableConfig`
    invariants), so a config that reaches this helper has at most one
    populated per table. Tables with neither are classified as mask
    (defensive default; the schema rejects them upstream).
    """
    out: dict[str, str] = {}
    for table in config.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("name")
        if not isinstance(name, str):
            continue
        out[name] = "generate" if table.get("generate_columns") else "mask"
    return out
