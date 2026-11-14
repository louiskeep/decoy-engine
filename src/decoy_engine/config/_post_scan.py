"""`post_scan`: opt-in controls for the Post-scan phase.

`integrity` enables exactly the existing post-mask integrity suite (residual
PII, FK preservation, policy validation), report-only. It does not enable
Pre-scan discovery or any other Post-scan check; `post_validation` and
`fidelity_report` stay separate subpass controls.

`detectors`, `ner_model`, `ner_findings` and `dismissed` choose model detectors for that suite.
Each is dropped from the dump while it equals its default, so a config that predates them keeps
its exact dump and plan hash; a changed value moves the hash because it changes what the run
checks or, under `ner_findings: fail`, blocks on. Comparison is by value, not by what was
written (`_global_settings.py` precedent), so writing a default out does not move the hash.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError

from decoy_engine import reasons
from decoy_engine.config._pre_scan import canonical_detectors, checked_ner_model, inactive
from decoy_engine.detection.ner import DEFAULT_NER_MODEL
from decoy_engine.detection.types import DetectorSource
from decoy_engine.reasons import ReasonCode

_DISMISSAL_INVALID = str(reasons.config.DISMISSAL_INVALID)


class Dismissal(BaseModel):
    """A reviewed finding: matches only the warning whose every field here is equal."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True, strict=True)

    table: str
    column: str
    detector_id: str
    source: DetectorSource
    model_id: str | None = None
    model_version: str | None = None
    strategy: str | None  # None: the column had no configured strategy

    @field_validator("table", "column", "detector_id", mode="before")
    @classmethod
    def _required_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise PydanticCustomError(_DISMISSAL_INVALID, "a dismissal field must be text.")
        return value

    @field_validator("strategy", "model_id", "model_version", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, str):
            raise PydanticCustomError(_DISMISSAL_INVALID, "a dismissal field must be text or null.")
        return value

    @model_validator(mode="after")
    def _model_identity_iff_model_source(self) -> Dismissal:
        has_model = self.model_id is not None and self.model_version is not None
        if self.source == "regex" and (self.model_id is not None or self.model_version is not None):
            raise PydanticCustomError(_DISMISSAL_INVALID, "a regex dismissal names no model.")
        if self.source != "regex" and not has_model:
            raise PydanticCustomError(
                _DISMISSAL_INVALID, "a model dismissal needs model_id and model_version."
            )
        return self


class PostScanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    integrity: bool = False
    detectors: tuple[DetectorSource, ...] = ("regex",)
    ner_model: str = DEFAULT_NER_MODEL
    ner_findings: Literal["warn", "fail"] = "warn"
    dismissed: tuple[Dismissal, ...] = ()

    @field_validator("detectors")
    @classmethod
    def _detectors(cls, value: tuple[DetectorSource, ...]) -> tuple[DetectorSource, ...]:
        return canonical_detectors(value)

    @field_validator("ner_model", mode="before")
    @classmethod
    def _ner_model(cls, value: Any) -> str:
        return checked_ner_model(value)

    @model_validator(mode="after")
    def _chosen_settings_must_run(self) -> PostScanConfig:
        if not self.integrity:
            if self.detectors != ("regex",):
                raise inactive("post_scan.detectors", "post_scan.integrity: true")
            if self.ner_findings == "fail":
                raise inactive("post_scan.ner_findings", "post_scan.integrity: true")
            if self.dismissed:
                raise inactive("post_scan.dismissed", "post_scan.integrity: true")
        if "ner" not in self.detectors:
            if self.ner_findings == "fail":
                raise inactive("post_scan.ner_findings", "ner in post_scan.detectors")
            if "ner_model" in self.model_fields_set:
                raise inactive("post_scan.ner_model", "ner in post_scan.detectors")
        return self

    @model_serializer(mode="wrap")
    def _omit_defaults(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if tuple(data.get("detectors", ())) == ("regex",):
            data.pop("detectors", None)
        if data.get("ner_model") == DEFAULT_NER_MODEL:
            data.pop("ner_model", None)
        if data.get("ner_findings") == "warn":
            data.pop("ner_findings", None)
        if not data.get("dismissed"):
            data.pop("dismissed", None)
        return data

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        from decoy_engine.config._declared_casting import recover_serialization_schema

        return recover_serialization_schema(core_schema, handler)


def post_scan_integrity_requested(config: Mapping[str, Any]) -> bool:
    """True when a validated config dict opts in to Post-scan integrity."""
    block = config.get("post_scan")
    return bool(block.get("integrity")) if isinstance(block, Mapping) else False


def post_scan_gate_applies(config: Mapping[str, Any]) -> bool:
    """True when a validated config dict needs the post-scan gate: a relationship is declared or
    Post-scan integrity is on. The one place that decision is made."""
    return bool(config.get("relationships")) or post_scan_integrity_requested(config)


def post_scan_model_setting(config: Mapping[str, Any]) -> str | None:
    """The post-scan setting that asks for a model check (`post_scan.detectors` other than
    regex, or `post_scan.ner_findings: fail`), or `None`. Only an enabled suite asks."""
    if not post_scan_integrity_requested(config):
        return None
    block = config["post_scan"]
    detectors = block.get("detectors")
    # Anything other than a list of `regex` entries counts as a model ask: fail closed on a raw dict.
    if isinstance(detectors, list | tuple):
        asks = any(d != "regex" for d in detectors)
    else:
        asks = detectors is not None and detectors != "regex"
    if asks:
        return "post_scan.detectors"
    return "post_scan.ner_findings" if block.get("ner_findings") == "fail" else None


NO_SINK_KIND = "None"
# The one sink class whose staged output the gate may read back. Any other class, including a
# subclass, is a foreign producer: its files are never trusted, so the run stays resident.
_READABLE_SINK_KINDS = frozenset(
    {NO_SINK_KIND, "decoy_engine.execution._transactional_sink.ParquetTransactionalSink"}
)


def post_scan_sink_kind(sink: object | None) -> str:
    """The class token of a sink (`"None"` for no sink): what readability is decided on."""
    if sink is None:
        return NO_SINK_KIND
    cls = type(sink)
    return f"{cls.__module__}.{cls.__qualname__}"


def post_scan_output_readable(sink_kind: str) -> bool:
    """Whether the gate can read this sink's output back before it commits. No sink is readable:
    the routes that write none hand their outputs back resident."""
    return sink_kind in _READABLE_SINK_KINDS


def post_scan_resident_reason(
    config: Mapping[str, Any], *, sink_kind: str = NO_SINK_KIND
) -> ReasonCode | None:
    """Why a job must stay on the resident route for the post-scan gate, or `None`.

    `post_scan.integrity` needs the whole output (the column suite is not streamed yet). Declared
    keys need an output the gate can read back, so a sink it cannot read keeps the run resident."""
    if post_scan_integrity_requested(config):
        return reasons.route.POST_SCAN_INTEGRITY_REQUESTED
    if config.get("relationships") and not post_scan_output_readable(sink_kind):
        return reasons.route.POST_SCAN_KEYS_RESIDENT
    return None
