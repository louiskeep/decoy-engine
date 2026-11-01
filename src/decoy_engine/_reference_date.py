"""Reference-instant normalization shared by config validation and pinned generation.

Lives outside both `config` and `generation` so `GlobalSettings` can validate `reference_date`
without importing the generation package, and so the refusal keeps one exception identity.
"""

from __future__ import annotations

from datetime import datetime, timezone

from decoy_engine import reasons
from decoy_engine.errors import DecoyError

__all__ = ["PinnedModeError", "normalize_reference_date"]


class PinnedModeError(DecoyError):
    """A coded, fail-closed refusal in pinned generation mode.

    Messages name the table and column and the reason, never a cell value, so a refusal cannot
    leak generated or snapshot content into logs.
    """

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def normalize_reference_date(value: datetime | str) -> str:
    """Canonical UTC second-precision text (``YYYY-MM-DDTHH:MM:SSZ``) for a reference instant.

    A naive datetime is refused: it would bind the plan to the host's local zone.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise PinnedModeError(
                code=reasons.plan.PINNED_REFERENCE_DATE_INVALID,
                message="reference_date is not an ISO 8601 datetime",
            ) from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PinnedModeError(
            code=reasons.plan.PINNED_REFERENCE_DATE_INVALID,
            message="reference_date must be a timezone-aware datetime",
        )
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
