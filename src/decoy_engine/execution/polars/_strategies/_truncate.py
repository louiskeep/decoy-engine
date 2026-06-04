"""truncate strategy, Polars-native (engine-v2 S12).

Mirrors the pandas `TruncateHandler` (S9): keep the first (or last, if
`from_end`) `length` characters of each stringified non-null value; nulls
preserved; an invalid length passes through. The pandas path stringifies via
`astype(str)`; the Polars path casts to Utf8. Output values match for string
sources (the fixtures); the parity harness accepts Arrow-type differences.

MG-1 S3 extension (2026-06-01): adds `mask_char` + `keep` so the V1
"keep last 4, replace rest with *" use case works. Mirrors the pandas
extension byte-for-byte; the parity harness verifies both backends
produce identical strings for the new shape.
"""

from __future__ import annotations

import polars as pl

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsTruncateHandler:
    """Keep the first `length` chars of each value (or last, if from_end).

    MG-1 S3 extension: see TruncateHandler (pandas sibling) for the
    keep/mask_char contract; this handler implements the identical
    shape on top of polars.expr.str primitives.
    """

    name: str = "truncate"

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        length = cfg.get("length")
        if not isinstance(length, int) or length < 1:
            # Invalid config -> passthrough (one bad rule does not abort the run).
            return frame, []
        # MG-1/S3: keep + mask_char. Legacy from_end maps to keep="tail";
        # explicit keep wins. mask_char None preserves V1 byte identity.
        from_end_legacy = bool(cfg.get("from_end", False))
        keep = cfg.get("keep")
        if keep is None:
            keep = "tail" if from_end_legacy else "head"
        if keep not in ("head", "tail"):
            return frame, []
        mask_char = cfg.get("mask_char")
        if mask_char is not None:
            if not isinstance(mask_char, str) or len(mask_char) != 1:
                return frame, []  # rejected at plan-compile, defensive
        as_str = pl.col(column).cast(pl.Utf8)
        if mask_char is None:
            # V1 path; byte-identical.
            sliced = (
                as_str.str.slice(-length) if keep == "tail" else as_str.str.slice(0, length)
            ).alias(column)
            return frame.with_columns(sliced), []

        # New path: replace truncated portion with mask_char repeated.
        # Polars doesn't have a built-in "pad with char to original
        # length" so build it via map_elements; nulls preserved by
        # passing through None.
        def _mask_one(s: str | None) -> str | None:
            if s is None:
                return None
            if keep == "tail":
                keep_part = s[-length:]
                drop_part = s[:-length] if length < len(s) else ""
                return (mask_char * len(drop_part)) + keep_part
            else:
                keep_part = s[:length]
                drop_part = s[length:] if length < len(s) else ""
                return keep_part + (mask_char * len(drop_part))

        masked = as_str.map_elements(_mask_one, return_dtype=pl.Utf8).alias(column)
        return frame.with_columns(masked), []
