"""Pydantic schemas for Disguise YAML bundles.

A Disguise is parsed at load time into these models. Malformed bundles fail
loud: `pydantic.ValidationError` will name the offending field. CI loads
all bundles in `disguises/` as a smoke test so a broken YAML breaks the build.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Detection sprint (V1): each entry in expected_fields is either a single
# detector_id (str) or a group of detector_ids (list[str]) meaning "any
# of these satisfies the expectation". The platform's preflight code
# expands the groups when evaluating strict-mode coverage.
ExpectedField = str | list[str]


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
    why: str | None = None


class Disguise(BaseModel):
    id: str
    name: str
    summary: str
    regulation: str | None = None
    primary_buyer: str | None = None
    triggers: TriggerSpec
    # Detection sprint (V1) strict-mode contract. Defaults to empty so
    # pre-V1 bundles parse cleanly; in V1 every shipped Disguise has a
    # populated list. The platform preflight cross-references this
    # against the scan's detection + override set to decide whether the
    # Disguise can run in strict mode.
    expected_fields: list[ExpectedField] = Field(default_factory=list)
    field_rules: list[FieldRule]

    def expected_field_groups(self) -> list[list[str]]:
        """Normalize expected_fields into a list of any-of groups.

        Each top-level entry becomes a list[str]: single-detector entries
        become singletons, group entries pass through. Used by the
        platform's preflight code so it doesn't have to special-case the
        Union-typed YAML shape.
        """
        out: list[list[str]] = []
        for entry in self.expected_fields:
            if isinstance(entry, str):
                out.append([entry])
            else:
                out.append(list(entry))
        return out
