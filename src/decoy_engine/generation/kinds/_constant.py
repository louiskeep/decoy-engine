"""The `constant` generate type: every row gets the one configured value.

No random draw (`GEN_KIND_TO_SITE` maps it to `DETERMINISTIC_NO_DRAW`), so the stream holds no
state and any batch layout gives the same rows. `null_probability` is the generic mask wrapped
around it by the stream builder and the whole-table post-pass, as for every type.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.config import constant_arrow_type

__all__ = [
    "ConstantStream",
    "constant_arrow_type",
    "constant_payload_bound",
    "constant_values",
]

_UTF8_MAX_BYTES_PER_CHAR = 4


def constant_payload_bound(col: dict[str, Any]) -> int:
    value = col.get("value")
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return 8


class ConstantStream:
    def __init__(self, col: dict[str, Any]) -> None:
        self._value = col.get("value")

    def next_batch(self, offset: int, size: int) -> list[Any]:
        return [self._value] * size


def constant_values(col: dict[str, Any], n: int) -> list[Any]:
    return ConstantStream(col).next_batch(0, n)
