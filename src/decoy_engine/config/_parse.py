"""parse_pipeline_config: the one route from a raw mapping to a validated PipelineConfig."""

from __future__ import annotations

from typing import Any

from pydantic_core import ValidationError

from decoy_engine.config._delivery import DeliveryConfig
from decoy_engine.config._errors import PipelineConfigError
from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.config._relationships import RelationshipConfig
from decoy_engine.safe_output import ConfigProblem, validation_problems


def parse_pipeline_config(raw: Any) -> PipelineConfig:
    """Validate ``raw`` into a ``PipelineConfig``.

    Raises:
        PipelineConfigError: with value-free ``problems``. The Pydantic error is not attached
            (``__cause__`` and ``__context__`` are None) because it holds the submitted values.
    """
    problems: tuple[ConfigProblem, ...]
    try:
        return PipelineConfig.model_validate(raw)
    except ValidationError as exc:
        problems = validation_problems(exc)
    # Raised outside the handler so the Pydantic error is not implicit context.
    raise PipelineConfigError.from_problems(problems)


def parse_relationship_config(raw: Any, *, prefix: str) -> RelationshipConfig:
    """Validate one declared relationship by the same value-free route as a whole config.

    ``prefix`` heads the error text (for example ``relationship 2 is invalid``), so a caller
    that validates a list names the entry without the submitted values.
    """
    problems: tuple[ConfigProblem, ...]
    try:
        return RelationshipConfig.model_validate(raw)
    except ValidationError as exc:
        problems = validation_problems(exc)
    raise PipelineConfigError.from_problems(problems, prefix=prefix)


def parse_delivery_config(raw: Any) -> DeliveryConfig:
    """Validate the ``delivery`` block by the same value-free route as a whole config."""
    problems: tuple[ConfigProblem, ...]
    try:
        return DeliveryConfig.model_validate(raw)
    except ValidationError as exc:
        problems = validation_problems(exc)
    raise PipelineConfigError.from_problems(problems, prefix="delivery is invalid")
