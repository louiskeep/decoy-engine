"""Pydantic schemas for Disguise YAML bundles.

A Disguise is parsed at load time into these models. Malformed bundles fail
loud — `pydantic.ValidationError` will name the offending field. CI loads
all bundles in `disguises/` as a smoke test so a broken YAML breaks the build.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TriggerSpec(BaseModel):
    """When does FORECAST recommend this Disguise.

    A Disguise is *eligible* if all conditions are satisfied:
      - every detector in `required_detectors` appears somewhere in the profile
      - if `any_detectors` is non-empty, at least one of them appears

    Each match contributes to the Disguise's score. `min_score` is the floor
    below which the Disguise is dropped from the recommendation list entirely.
    """

    required_detectors: list[str] = Field(default_factory=list)
    any_detectors: list[str] = Field(default_factory=list)
    co_occurrence: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Lists of detector ids that, when ALL present in the profile, "
            "boost this Disguise's score. E.g. [['us_date','us_zip','person_name']] "
            "for the HIPAA quasi-identifier trio."
        ),
    )
    min_score: float = 0.3


class FieldRule(BaseModel):
    """How to mask a column matched by one or more detectors.

    The first FieldRule that matches a column wins. Order in the YAML matters.
    """

    detectors: list[str]
    mask: str
    params: dict[str, Any] = Field(default_factory=dict)
    why: Optional[str] = None


class Disguise(BaseModel):
    id: str
    name: str
    summary: str
    regulation: Optional[str] = None
    primary_buyer: Optional[str] = None
    triggers: TriggerSpec
    field_rules: list[FieldRule]
