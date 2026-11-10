"""The tolerant read model of a record model: the shared deriver with this package's exact tags.

A record is written by one version and read by another. The write model refuses unknown fields so
a producer cannot slip in a free-text field; the read model ignores fields a later version added
under the same tag and reads a later enum value as a checked token. The schema and producer tags
stay exact: a reader must match them, and the callers compare them to decide what to do.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from decoy_engine._value_free import derive_read
from decoy_engine.run_result._model import Token

__all__ = ["read_model_of"]

_EXACT = ("schema_version", "producer", "record_schema", "log_schema", "export_schema", "type")


class _ReadBase(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)


def read_model_of(model: type[BaseModel]) -> type[BaseModel]:
    """``model`` with unknown fields ignored at every level and enums read as tokens."""
    return derive_read(model, base=_ReadBase, token=Token, exact_fields=_EXACT, module=__name__)
