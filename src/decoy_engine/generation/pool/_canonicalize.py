"""_canonicalize_source: per-row source-value canonicalization for derive_index.

**LOAD-BEARING:** the per-dtype rules below are part of the determinism
envelope per cross-sprint contracts risk R3. Any change is a
`SEED_PROTOCOL_VERSION` bump conversation, not a per-sprint decision.

Per-dtype rules (S5 spec §5.1):

| dtype family | canonical encoding |
|--------------|--------------------|
| str / object | UTF-8 with Unicode NFC normalization |
| int (any width) | 8-byte big-endian two's complement |
| bool | b"\\x00" / b"\\x01" |
| datetime (tz-aware) | ISO 8601 UTC with "Z" suffix, UTF-8 |
| datetime (tz-naive) | HARD ERROR (timezone_naive_datetime) |
| date | ISO 8601 YYYY-MM-DD UTF-8 |
| float | HARD ERROR (float_canonicalization_unsupported) per PO call |
| Decimal | UTF-8 of canonical string form |
| null | HARD ERROR (null_canonicalization_unreachable; nulls preserve upstream) |

Float hard-error rationale (PO-locked at S5 PQ-call): IEEE-754 binary
representation drift across platforms is a real concern; binary equality
is not what customers usually mean. The hard error forces the
conversation upstream (bin / stringify) rather than baking a fragile
rule into the determinism envelope.
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from decoy_engine.generation.pool._errors import GenerationError


def _canonicalize_source(value: Any) -> bytes:
    """Canonicalize a per-row source value to bytes for derive_index input.

    The dispatch is type-based (not dtype-based) so the helper works
    against pd.Series.iloc[i] outputs (which return Python scalars) and
    against bare Python values used in tests.

    Raises:
        GenerationError on float, timezone-naive datetime, or null.
    """
    if value is None:
        raise GenerationError(
            code="null_canonicalization_unreachable",
            message=(
                "_canonicalize_source received None. Nulls preserve at the "
                "mask layer (see PoolSampler.sample); they should not reach "
                "canonicalization."
            ),
        )
    if isinstance(value, bool):
        return b"\x01" if value else b"\x00"
    if isinstance(value, int):
        return int(value).to_bytes(8, "big", signed=True)
    if isinstance(value, float):
        raise GenerationError(
            code="float_canonicalization_unsupported",
            message=(
                f"Float source value {value!r} cannot be canonicalized for "
                "deterministic mapping. IEEE-754 representation drifts "
                "across platforms; route through a stringified or binned "
                "integer column upstream. See S5 spec §5.1 + PQ-call."
            ),
        )
    if isinstance(value, Decimal):
        return str(value).encode("utf-8")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise GenerationError(
                code="timezone_naive_datetime",
                message=(
                    f"Datetime {value!r} has no tzinfo. Deterministic "
                    "canonicalization requires explicit UTC; localize "
                    "upstream or use a date if intra-day precision is "
                    "not needed."
                ),
            )
        from datetime import timezone

        return value.astimezone(timezone.utc).isoformat().encode("utf-8")
    if isinstance(value, date):
        return value.isoformat().encode("utf-8")
    # Default: string / object dtype. NFC normalization for cross-client
    # input stability.
    return unicodedata.normalize("NFC", str(value)).encode("utf-8")
