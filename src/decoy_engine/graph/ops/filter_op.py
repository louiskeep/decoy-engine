"""filter — keep rows that match a pandas-query predicate.

Config:
    predicate: str   - e.g. "state == 'CA' and age >= 18"

Per Q2 in PIPELINE_GRAPH_GUIDE.md, we ship pandas df.query() syntax in MVP.
The predicate string is just stored in YAML; we can swap in a SQL-WHERE
interpreter later without breaking the contract.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "filter"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    pred = config.get("predicate")
    if not isinstance(pred, str) or not pred.strip():
        raise ValidationError(
            "'predicate' must be a non-empty string", "config.predicate"
        )


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    predicate = config["predicate"]
    try:
        return df.query(predicate, engine="python")
    except Exception as exc:
        raise OpError(f"filter predicate failed ({predicate!r}): {exc}") from exc
