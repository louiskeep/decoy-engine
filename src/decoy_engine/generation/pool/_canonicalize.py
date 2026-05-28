"""_canonicalize_source: per-row source-value canonicalization for derive_index.

**LOAD-BEARING:** the per-dtype rules below are part of the determinism
envelope per cross-sprint contracts risk R3. Any change is a
`SEED_PROTOCOL_VERSION` bump conversation, not a per-sprint decision.
The integer encoding changed under the v1 -> v2 bump (F-series
corrections): it was a fixed 8-byte two's-complement form that (a)
overflowed for |value| >= 2**63 and (b) missed `numpy.integer` scalars
(`pd.Series.iloc[i]` returns them), silently routing them through the
str fallback so `42` and `numpy.int64(42)` canonicalized differently.

Per-dtype rules (S5 spec §5.1, v2 envelope):

| dtype family | canonical encoding |
|--------------|--------------------|
| str / object | UTF-8 with Unicode NFC normalization |
| int (any width, incl. numpy) | length-prefixed minimal two's complement |
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

import numbers
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from decoy_engine.generation.pool._errors import GenerationError


def _encode_int(n: int) -> bytes:
    """Length-prefixed minimal-width two's-complement big-endian encoding.

    Source pattern: ASN.1 DER INTEGER (X.690 §8.3) encodes integers as the
    minimal number of two's-complement big-endian octets. We frame those
    octets with a 4-byte big-endian length so the encoding is injective and
    unambiguous for arbitrary magnitude (Python ints are unbounded; numpy
    integer scalars coerce losslessly via `int()`). This replaces the prior
    fixed 8-byte form, which raised OverflowError for |value| >= 2**63.
    """
    nbytes = (n.bit_length() + 8) // 8  # +8 leaves room for the sign bit; >=1
    body = n.to_bytes(nbytes, "big", signed=True)
    return len(body).to_bytes(4, "big") + body


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
    # `numbers.Integral` catches Python int AND numpy integer scalars
    # (numpy registers its int types with the ABC), so `pd.Series.iloc[i]`
    # numpy scalars take the int path instead of the str fallback. `int()`
    # coerces losslessly and gives unbounded magnitude.
    if isinstance(value, numbers.Integral):
        return _encode_int(int(value))
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
