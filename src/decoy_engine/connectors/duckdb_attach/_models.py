"""Dialect-independent request/config models for the DuckDB-attach connectors.

Extracted verbatim from ``postgres_snapshot`` (D0): the structured
identifiers-only relation selection and the connection/resource policy. No SQL
field exists on either model by design -- callers pass identifiers, never SQL.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

# Strict on purpose: unquoted-safe identifiers. Names needing quoted oddities
# (spaces, quotes, unicode) are rejected rather than escaped, so the validation
# boundary and the quoting boundary cannot disagree. (Conservative shared rule;
# a dialect with different identifier rules may tighten it further.)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def _validate_ident(value: str, what: str) -> str:
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        # The offending text is not echoed: it may be attacker-shaped input.
        raise ValueError(f"invalid {what} identifier")
    return value


class SnapshotConfig(BaseModel):
    """Connection and resource policy for one snapshot call. No SQL field."""

    # hide_input_in_errors: a ValidationError would otherwise echo the rejected
    # input, which for these models can be a DSN or attacker-shaped text.
    model_config = ConfigDict(
        extra="forbid", frozen=True, arbitrary_types_allowed=True, hide_input_in_errors=True
    )

    dsn: SecretStr
    extension_directory: Path = Field(
        description="Directory holding the bundled DuckDB extension. Never installed from the network."
    )
    memory_limit: str = Field(pattern=r"^\d+(\.\d+)?\s?(KB|MB|GB|TB|KiB|MiB|GiB|TiB|B)$")
    temp_directory: Path
    threads: int = Field(ge=1, le=256)
    row_group_size: int = Field(ge=1_000, le=10_000_000)
    statement_timeout_s: int = Field(ge=1)
    deadline_s: int = Field(ge=1)


class RelationSelection(BaseModel):
    """One relation to stage. Identifiers only; there is no predicate field."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_name: str = Field(alias="schema")
    table: str
    columns: list[str] = Field(min_length=1)
    order_by: list[str] = Field(default_factory=list)

    @field_validator("schema_name")
    @classmethod
    def _v_schema(cls, v: str) -> str:
        return _validate_ident(v, "schema")

    @field_validator("table")
    @classmethod
    def _v_table(cls, v: str) -> str:
        return _validate_ident(v, "table")

    @field_validator("columns", "order_by")
    @classmethod
    def _v_cols(cls, v: list[str]) -> list[str]:
        for c in v:
            _validate_ident(c, "column")
        if len(set(v)) != len(v):
            raise ValueError("duplicate column")
        return v

    @model_validator(mode="after")
    def _order_subset(self) -> "RelationSelection":
        if not set(self.order_by) <= set(self.columns):
            raise ValueError("order_by must be a subset of columns")
        return self

    @property
    def key(self) -> str:
        return f"{self.schema_name}.{self.table}"
