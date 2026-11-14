"""NamespaceConfig: an optional top-level namespace-declaration block.

The engine's plan-compile reads a top-level `namespaces` block via
`config.get("namespaces", {})` (`plan/_compile._build_namespaces`); each
entry declares which columns bind to that namespace. The block is
optional (S2 auto-binds FK-child namespaces from the relationship graph;
S1 also consumes explicitly-declared namespaces).

`declared_by` carries "table.column" strings; the engine splits them, the
adapter only enforces the shape (a list of strings).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import PydanticCustomError

from decoy_engine import reasons

# A starter pipeline writes `decoy-set-namespace-<n>` where a declared relationship named no
# namespace. The prefix is reserved: a config that still holds one does not validate.
NAMESPACE_PLACEHOLDER_PREFIX = "decoy-set-namespace-"


def refuse_namespace_placeholder(value: str | None) -> str | None:
    """Pass a namespace through, or refuse one that still holds the starter placeholder.

    The error carries the reason code and the field location pydantic adds, never the value.
    """
    if value is not None and value.startswith(NAMESPACE_PLACEHOLDER_PREFIX):
        raise PydanticCustomError(
            str(reasons.config.NAMESPACE_PLACEHOLDER_UNSET),
            "namespace holds an unset placeholder.",
        )
    return value


class NamespaceConfig(BaseModel):
    """One explicitly-declared namespace binding."""

    model_config = ConfigDict(extra="forbid")

    declared_by: list[str] = Field(default_factory=list)
