"""`pre_scan`: opt-in detector choice for the Pre-scan phase.

`detectors` picks which sources look for PII when a source is scanned: `regex` (always on in
V1), the lgbm `classifier` and spaCy `ner`. Pre-scan only suggests, so this block is left out
of the plan hash (`plan/_compile._hash_config`) and out of the dump when it is the default.
The checks here are shape only; whether a model is installed is a preflight
(`profiling/model_pack/_detectors.load_detectors`), never a config rule.
"""

from __future__ import annotations

from typing import Any

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
from decoy_engine.detection.ner import DEFAULT_NER_MODEL, NerModelNameError
from decoy_engine.detection.types import (
    DETECTOR_ORDER,
    DetectorSource,
    validate_ner_model_name,
)


def canonical_detectors(value: tuple[DetectorSource, ...]) -> tuple[DetectorSource, ...]:
    """`regex` present, no duplicates, in the canonical order (dumps and hashes stay stable)."""
    if len(set(value)) != len(value):
        raise PydanticCustomError(
            str(reasons.config.DETECTORS_INVALID), "detectors must not repeat a source."
        )
    if "regex" not in value:
        raise PydanticCustomError(
            str(reasons.config.DETECTORS_INVALID), "detectors must include regex."
        )
    return tuple(source for source in DETECTOR_ORDER if source in value)


def checked_ner_model(value: Any) -> str:
    """The model name, or a custom error carrying `config.ner_model_invalid` and no part of it."""
    try:
        return validate_ner_model_name(value)
    except NerModelNameError as exc:
        raise PydanticCustomError(
            str(exc.code), "ner_model must be a plain package name."
        ) from None


def inactive(field: str, needs: str) -> PydanticCustomError:
    """A chosen setting that would be silently ignored; names the field, never a value."""
    return PydanticCustomError(str(reasons.config.POST_SCAN_INACTIVE), f"{field} needs {needs}.")


class PreScanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    detectors: tuple[DetectorSource, ...] = ("regex",)
    ner_model: str = DEFAULT_NER_MODEL

    @field_validator("detectors")
    @classmethod
    def _detectors(cls, value: tuple[DetectorSource, ...]) -> tuple[DetectorSource, ...]:
        return canonical_detectors(value)

    @field_validator("ner_model", mode="before")
    @classmethod
    def _ner_model(cls, value: Any) -> str:
        return checked_ner_model(value)

    @model_validator(mode="after")
    def _ner_model_needs_ner(self) -> PreScanConfig:
        if "ner_model" in self.model_fields_set and "ner" not in self.detectors:
            raise PydanticCustomError(
                str(reasons.config.PRE_SCAN_NER_INACTIVE),
                "pre_scan.ner_model needs ner in pre_scan.detectors.",
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_default_model(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # An explicit default model would re-validate as "a set ner_model" and trip the rule above.
        data: dict[str, Any] = handler(self)
        if data.get("ner_model") == DEFAULT_NER_MODEL:
            data.pop("ner_model")
        return data

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        from decoy_engine.config._declared_casting import recover_serialization_schema

        return recover_serialization_schema(core_schema, handler)
