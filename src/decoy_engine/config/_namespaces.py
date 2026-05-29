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


class NamespaceConfig(BaseModel):
    """One explicitly-declared namespace binding."""

    model_config = ConfigDict(extra="forbid")

    declared_by: list[str] = Field(default_factory=list)
