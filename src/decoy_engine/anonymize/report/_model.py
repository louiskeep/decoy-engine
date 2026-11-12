"""The ``risk-utility-report/v1`` document: one strict writer and a reader derived from it.

Value-free by construction: every string is the schema tag, a column name that passes
``approve_name``, or a fixed token; every number is strict and range-checked. The reader comes
from ``decoy_engine.run_result.derive_read_model`` (the run-result document's compatibility rule:
tokens tolerant of later minors, added fields ignored, the tag required), so it is never a
hand-kept copy. Under ``/v1`` only optional fields may be added (compatibility contract 3.1).

One cross-field rule: journalist and marketer figures exist only when a population was
supplied. The writer enforces it strictly; the reader enforces it for the two v1 population
tokens and lets a token added in a later minor carry figures.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from annotated_types import Ge, Gt, Le
from pydantic import (
    AfterValidator,
    AllowInfNan,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    model_validator,
)

from decoy_engine.reasons import approve_name
from decoy_engine.run_result import ValueFree, derive_read_model

RISK_UTILITY_REPORT_SCHEMA: Final = "risk-utility-report/v1"


def _tag(value: str) -> str:
    if value != RISK_UTILITY_REPORT_SCHEMA:
        raise ValueError("not a valid schema_tag")
    return value


def _name(value: str) -> str:
    if approve_name(value) is None:
        raise ValueError("not a valid column_name")
    return value


SchemaTag = Annotated[str, ValueFree("schema_tag"), AfterValidator(_tag)]
ColumnName = Annotated[str, ValueFree("column_name"), AfterValidator(_name)]

Count = Annotated[int, Strict(), Ge(0)]
Positive = Annotated[int, Strict(), Ge(1)]
Unit = Annotated[float, Strict(), Ge(0), Le(1), AllowInfNan(False)]
Threshold = Annotated[float, Strict(), Gt(0), Le(1), AllowInfNan(False)]
PositiveReal = Annotated[float, Strict(), Gt(0), AllowInfNan(False)]
NonNegative = Annotated[float, Strict(), Ge(0), AllowInfNan(False)]
Flag = Annotated[bool, Strict()]

Method = Literal["full_domain_generalization_with_record_suppression"]
ModelKind = Literal[
    "k_anonymity",
    "distinct_l_diversity",
    "entropy_l_diversity",
    "recursive_cl_diversity",
    "equal_t_closeness",
    "ordered_t_closeness",
]
MetricToken = Literal["discernibility", "loss", "non_uniform_entropy"]
PopulationToken = Literal["supplied", "not_supplied"]
Assumption = Literal[
    "prosecutor_target_known_in_release",
    "population_consistent_with_superset",
    "suppressed_rows_not_released",
    "t_closeness_against_input_distribution",
]
NotEstablished = Literal[
    "expert_determination",
    "linkage_with_external_data",
    "population_risk_without_population",
    "suppressed_rows_risk",
]


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True, extra="forbid")

    def to_payload(self) -> dict[str, Any]:
        """The JSON-compatible dict, keyed by wire names."""
        return self.model_dump(mode="json", by_alias=True)


class ModelEntry(_Base):
    kind: ModelKind
    sensitive: ColumnName | None
    k: Positive | None
    l: Positive | None  # noqa: E741 -- the model's published name
    c: PositiveReal | None
    t: Unit | None
    met: Flag


class SearchSection(_Base):
    lattice_size: Count
    nodes_evaluated: Count
    metric: MetricToken


class UtilitySection(_Base):
    discernibility: Count
    loss_metric: Unit
    non_uniform_entropy: NonNegative


class SuppressionSection(_Base):
    rows_input: Count
    suppression_limit: Unit
    allowed_suppressed: Count
    rows_suppressed: Count
    rows_released: Count


class ClassesSection(_Base):
    class_count: Count
    k_min: Positive | None


class RiskFigures(_Base):
    highest: Unit
    average: Unit
    records_at_risk: Unit
    rows_at_risk: Count


class MarketerFigures(_Base):
    average: Unit


def _population_sections_agree(section: Any) -> Any:
    """Writer: journalist and marketer figures and a population row count only with a
    supplied population."""
    supplied = section.population == "supplied"
    if not supplied and (section.journalist is not None or section.marketer is not None):
        raise ValueError("journalist and marketer figures need a supplied population")
    if supplied != (section.population_rows is not None):
        raise ValueError("population_rows is given exactly when a population was supplied")
    return section


def _population_sections_agree_on_read(section: Any) -> Any:
    """Reader: the same rule for the two v1 tokens only. A later minor may add a population
    token (for example an estimated population) that carries figures, and must still read."""
    if section.population == "not_supplied":
        if section.journalist is not None or section.marketer is not None:
            raise ValueError("journalist and marketer figures need a supplied population")
        if section.population_rows is not None:
            raise ValueError("population_rows is given only when a population was supplied")
    if section.population == "supplied" and section.population_rows is None:
        raise ValueError("population_rows is given when a population was supplied")
    return section


class RiskSection(_Base):
    risk_threshold: Threshold
    population: PopulationToken
    population_rows: Count | None
    prosecutor: RiskFigures | None
    journalist: RiskFigures | None
    marketer: MarketerFigures | None

    @model_validator(mode="after")
    def check_population_sections(self) -> Any:
        return _population_sections_agree(self)


class RiskUtilityReport(_Base):
    # ``schema`` would shadow ``BaseModel.schema``, so the field has another name and the wire key.
    schema_tag: SchemaTag = Field(default=RISK_UTILITY_REPORT_SCHEMA, alias="schema")
    method: Method
    quasi_identifiers: tuple[ColumnName, ...]
    heights: tuple[Count, ...]
    node: tuple[Count, ...]
    models: tuple[ModelEntry, ...]
    search: SearchSection
    utility: UtilitySection
    suppression: SuppressionSection
    classes: ClassesSection
    risk: RiskSection
    assumptions: tuple[Assumption, ...]
    not_established: tuple[NotEstablished, ...]


def _derive_reader() -> Any:
    # The derivation does not carry model validators, so the risk section's reader is derived
    # first, given the same rule, and handed to the derivation of the whole document.
    cache: dict[type, type] = {}
    risk_read = derive_read_model(RiskSection, cache)

    class RiskSectionRead(risk_read):  # type: ignore[valid-type, misc]
        @model_validator(mode="after")
        def check_population_sections(self) -> Any:
            return _population_sections_agree_on_read(self)

    cache[RiskSection] = RiskSectionRead
    return derive_read_model(RiskUtilityReport, cache)


RiskUtilityReportRead: Any = _derive_reader()
