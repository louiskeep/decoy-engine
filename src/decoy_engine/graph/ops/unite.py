"""unite — combine 2+ tables side by side (column-wise concat / horizontal join).

Sister to a future `stack` op that would concat row-wise. Both `mask` and
`generate` declare `INPUT_ARITY = (_, 1)` — they only ever consume a single
table — so when a user wants to feed a multi-table shape into masking they
must `unite` first. That's the whole reason this op exists.

Three modes, in order of specificity:

  * **per-pair** (`joins: [...]`): one entry per pairing, applied in
    order. inputs[0] is the main; joins[i] describes the pairing of
    the accumulated result with inputs[i+1]. Each entry is
    ``{left_on, right_on, join_type, suffixes?}``. Lets a multi-way
    join use different keys for different pairs (e.g.
    ``customers.id = orders.customer_id`` then
    ``customers.id = products.customer_id``). ``joins.length`` MUST
    equal ``inputs.length - 1``.

  * **keyed** (`on: [col, ...]`): same `on` applied to every pairing.
    Chained `df.merge(...)` using `join_type`. First merge uses the
    user's `suffixes`; later merges fall back to numbered suffixes so
    a 4-way join doesn't collide.

  * **positional** (no `on`, no `joins`): shortest-frame wins; reset
    indices and `pd.concat(axis=1)`. Names collide → pandas creates
    duplicate-named columns, which downstream nodes will trip on; the
    user's problem to fix with `drop_column` upstream.

Validation of `on` columns existing on each input is deferred to
apply-time — the YAML validator can't see the dataframes.

Config:
    joins:     list[JoinSpec] | None  — per-pair joins; length must equal
                                        inputs - 1. Overrides on / join_type.
    on:        list[str] | None       — columns to join on; None = positional
    join_type: 'inner' | 'left' |
               'right' | 'outer'      — only used when `on` is set; default 'inner'
    suffixes:  [str, str]              — collision suffixes for keyed mode
                                          (default ['_left', '_right'])

JoinSpec shape:
    left_on:   list[str]              — columns on the accumulated left
    right_on:  list[str]              — columns on the input being joined
    join_type: 'inner' | 'left' |
               'right' | 'outer'      — default 'inner'
    suffixes:  [str, str] | None      — optional; falls back to numbered
                                        defaults for the per-pair index.
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
    joins = config.get("joins")
    if joins is not None:
        if not isinstance(joins, list) or not joins:
            raise ValidationError(
                "'joins' must be a non-empty list of join specs",
                "config.joins",
            )
        for i, spec in enumerate(joins):
            if not isinstance(spec, dict):
                raise ValidationError(
                    f"joins[{i}] must be an object",
                    f"config.joins[{i}]",
                )
            left_on = spec.get("left_on")
            right_on = spec.get("right_on")
            if not isinstance(left_on, list) or not left_on or not all(
                isinstance(c, str) and c.strip() for c in left_on
            ):
                raise ValidationError(
                    f"joins[{i}].left_on must be a non-empty list of column-name strings",
                    f"config.joins[{i}].left_on",
                )
            if not isinstance(right_on, list) or not right_on or not all(
                isinstance(c, str) and c.strip() for c in right_on
            ):
                raise ValidationError(
                    f"joins[{i}].right_on must be a non-empty list of column-name strings",
                    f"config.joins[{i}].right_on",
                )
            if len(left_on) != len(right_on):
                raise ValidationError(
                    f"joins[{i}].left_on and right_on must have the same length",
                    f"config.joins[{i}]",
                )
            spec_join_type = spec.get("join_type", "inner")
            if spec_join_type not in _VALID_JOIN_TYPES:
                raise ValidationError(
                    f"joins[{i}].join_type must be one of {sorted(_VALID_JOIN_TYPES)} (got {spec_join_type!r})",
                    f"config.joins[{i}].join_type",
                )
            spec_suffixes = spec.get("suffixes")
            if spec_suffixes is not None:
                if (
                    not isinstance(spec_suffixes, list)
                    or len(spec_suffixes) != 2
                    or not all(isinstance(s, str) for s in spec_suffixes)
                ):
                    raise ValidationError(
                        f"joins[{i}].suffixes must be a 2-element list of strings",
                        f"config.joins[{i}].suffixes",
                    )
        # `joins` and the legacy flat `on` / `join_type` are mutually
        # exclusive: per-pair specs override, the flat keys are ignored
        # when joins is set. Reject mixing so the YAML stays unambiguous.
        if config.get("on") is not None:
            raise ValidationError(
                "'on' and 'joins' are mutually exclusive; pick one",
                "config.on",
            )
        return

    on = config.get("on")
    if on is not None:
        if not isinstance(on, list) or not on or not all(
            isinstance(c, str) and c.strip() for c in on
        ):
            raise ValidationError(
                "'on' must be a non-empty list of column-name strings (or omitted for positional unite)",
                "config.on",
            )

    join_type = config.get("join_type", "inner")
    if join_type not in _VALID_JOIN_TYPES:
        raise ValidationError(
            f"'join_type' must be one of {sorted(_VALID_JOIN_TYPES)} (got {join_type!r})",
            "config.join_type",
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
            )


def apply(inputs, config, ctx) -> pd.DataFrame:
    if len(inputs) < 2:
        raise OpError(
            f"unite requires at least 2 inputs, got {len(inputs)}"
        )

    joins = config.get("joins")
    on = config.get("on")

    try:
        # Per-pair joins win. inputs[0] is the main; joins[i] describes
        # how the accumulated result merges with inputs[i+1].
        if joins is not None:
            if len(joins) != len(inputs) - 1:
                raise OpError(
                    f"unite: 'joins' length ({len(joins)}) must equal inputs - 1 "
                    f"({len(inputs) - 1})"
                )
            result = inputs[0]
            for i, spec in enumerate(joins):
                right_df = inputs[i + 1]
                left_on = list(spec["left_on"])
                right_on = list(spec["right_on"])
                spec_join_type = spec.get("join_type", "inner")
                spec_suffixes = tuple(spec.get("suffixes") or (f"_{i + 1}a", f"_{i + 1}b"))

                missing_left = [c for c in left_on if c not in result.columns]
                missing_right = [c for c in right_on if c not in right_df.columns]
                if missing_left:
                    raise OpError(
                        f"unite: joins[{i}] left_on column(s) missing from "
                        f"accumulated frame: {missing_left}"
                    )
                if missing_right:
                    raise OpError(
                        f"unite: joins[{i}] right_on column(s) missing from "
                        f"input #{i + 2}: {missing_right}"
                    )

                result = result.merge(
                    right_df, left_on=left_on, right_on=right_on,
                    how=spec_join_type, suffixes=spec_suffixes,
                )
            return result

        if on is None:
            # Positional: line up by row position. Reset indexes so
            # mismatched indexes don't NaN-fill the result.
            normalized = [df.reset_index(drop=True) for df in inputs]
            return pd.concat(normalized, axis=1)

        # Legacy keyed mode: same `on` across every pairing.
        join_type = config.get("join_type", "inner")
        suffixes = tuple(config.get("suffixes") or ("_left", "_right"))
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
