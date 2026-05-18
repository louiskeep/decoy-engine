"""unite — combine 2+ tables side by side (column-wise concat / horizontal join).

Sister to a future `stack` op that would concat row-wise. Both `mask` and
`generate` declare `INPUT_ARITY = (_, 1)` — they only ever consume a single
table — so when a user wants to feed a multi-table shape into masking they
must `unite` first. That's the whole reason this op exists.

Two modes, picked by whether `on` is set:

  * positional (no `on`): shortest-frame wins; reset indices and
    `pd.concat(axis=1)`. Names collide → pandas creates duplicate-named
    columns, which downstream nodes will trip on; this is the user's
    problem to fix with `drop_column` upstream.

  * keyed (`on: [col, ...]`): chained `df.merge(...)` over the inputs in
    order, using `join_type` (inner/left/right/outer). First merge uses
    the user's `suffixes`; later merges fall back to numbered suffixes so
    a 4-way join doesn't collide. Validation of `on` columns existing on
    each input is deferred to apply-time — the YAML validator can't see
    the dataframes.

Config:
    on:        list[str] | None       — columns to join on; None = positional
    join_type: 'inner' | 'left' |
               'right' | 'outer'      — only used when `on` is set; default 'inner'
    suffixes:  [str, str]              — collision suffixes for keyed mode
                                          (default ['_left', '_right'])
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "unite"
# Pandas-native: the op uses df.merge / pd.concat under the hood. The
# runner materializes upstream Arrow inputs to pandas DataFrames at the
# op boundary; we hand the results back as pandas and the runner
# materializes back to Arrow downstream.
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (2, None)
OUTPUT_KIND = "stream"

_VALID_JOIN_TYPES = {"inner", "left", "right", "outer"}


def validate_config(config: dict[str, Any]) -> None:
    from decoy_engine.validation_result import CODES
    on = config.get("on")
    if on is not None:
        if not isinstance(on, list) or not on or not all(
            isinstance(c, str) and c.strip() for c in on
        ):
            raise ValidationError(
                "'on' must be a non-empty list of column-name strings (or omitted for positional unite)",
                "config.on",
                code=CODES.UNITE_BAD_ON,
            )

    join_type = config.get("join_type", "inner")
    if join_type not in _VALID_JOIN_TYPES:
        raise ValidationError(
            f"'join_type' must be one of {sorted(_VALID_JOIN_TYPES)} (got {join_type!r})",
            "config.join_type",
            code=CODES.UNITE_BAD_JOIN_TYPE,
        )

    suffixes = config.get("suffixes")
    if suffixes is not None:
        if (
            not isinstance(suffixes, list)
            or len(suffixes) != 2
            or not all(isinstance(s, str) for s in suffixes)
        ):
            raise ValidationError(
                "'suffixes' must be a 2-element list of strings",
                "config.suffixes",
                code=CODES.UNITE_BAD_SUFFIXES,
            )


def apply(inputs, config, ctx) -> pd.DataFrame:
    if len(inputs) < 2:
        raise OpError(
            f"unite requires at least 2 inputs, got {len(inputs)}"
        )

    on = config.get("on")
    join_type = config.get("join_type", "inner")
    suffixes = tuple(config.get("suffixes") or ("_left", "_right"))

    try:
        if on is None:
            # Positional: line up by row position. Reset indexes so
            # mismatched indexes don't NaN-fill the result.
            normalized = [df.reset_index(drop=True) for df in inputs]
            return pd.concat(normalized, axis=1)

        # Keyed: validate the join columns exist on every input now —
        # surfacing a clean error here is friendlier than pandas's
        # KeyError stack out of the merge call.
        for i, df in enumerate(inputs):
            missing = [c for c in on if c not in df.columns]
            if missing:
                raise OpError(
                    f"unite: input #{i + 1} is missing 'on' column(s): {missing}"
                )

        result = inputs[0]
        for i, df in enumerate(inputs[1:], start=1):
            # Numbered suffixes after the first merge so a 3+ way join
            # doesn't put `_left`/`_right` on every iteration.
            iter_suffixes = suffixes if i == 1 else (f"_{i}a", f"_{i}b")
            result = result.merge(
                df, on=on, how=join_type, suffixes=iter_suffixes
            )
        return result
    except OpError:
        raise
    except Exception as exc:
        raise OpError(f"unite failed: {exc}") from exc
