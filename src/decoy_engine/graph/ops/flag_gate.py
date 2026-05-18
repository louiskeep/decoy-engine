"""FLAG gate op: pre/mid-run data quality gate.

Evaluates one or more conditions against the current DataFrame. If any
condition fails, raises FlagPauseSignal so the platform runner can
transition the job to `review_pending`.

Supported condition types:
  row_count    -- assert (rows op value); op in {lt, lte, gt, gte, eq, ne}
  schema_match -- assert all listed columns are present

YAML config:
  gate_id: "pre_send_check"   # optional; surfaced in review record
  conditions:
    - type: row_count
      op: gte
      value: 1
    - type: schema_match
      columns: [id, email, created_at]

Returns input table unchanged when all conditions pass.
Raises FlagPauseSignal when any condition fails.
"""

from decoy_engine.exceptions import FlagPauseSignal
from decoy_engine.graph.ops._base import OpError  # noqa: F401 (re-exported for tests)

KIND = "flag_gate"
# Intentionally on pandas: condition evaluation (len/df.columns membership,
# comparison arithmetic) uses pure Python paths with no vectorized ops.
# Migrating to polars would add complexity with zero throughput gain since
# this op never processes bulk row data -- it only inspects metadata.
NATIVE_ENGINE = "pandas"
INPUT_ARITY = (0, 1)  # 0 = pre-run check before any source; 1 = mid-run
OUTPUT_KIND = "stream"

_VALID_OPS = frozenset({"lt", "lte", "gt", "gte", "eq", "ne"})


def validate_config(config: dict) -> None:
    from decoy_engine.internal.validator import ValidationError

    conditions = config.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise ValidationError(
            "conditions must be a non-empty list", "config.conditions"
        )
    for i, cond in enumerate(conditions):
        ctype = cond.get("type")
        if ctype == "row_count":
            op = cond.get("op")
            if op not in _VALID_OPS:
                raise ValidationError(
                    f"row_count op must be one of {sorted(_VALID_OPS)}, got {op!r}",
                    f"config.conditions[{i}].op",
                )
            if not isinstance(cond.get("value"), int):
                raise ValidationError(
                    "row_count value must be an integer",
                    f"config.conditions[{i}].value",
                )
        elif ctype == "schema_match":
            cols = cond.get("columns")
            if not isinstance(cols, list) or not cols:
                raise ValidationError(
                    "schema_match columns must be a non-empty list",
                    f"config.conditions[{i}].columns",
                )
        else:
            raise ValidationError(
                f"unsupported condition type {ctype!r} (supported: row_count, schema_match)",
                f"config.conditions[{i}].type",
            )


def apply(inputs: list, config: dict, ctx=None):
    df = inputs[0] if inputs else None
    gate_id = config.get("gate_id", "")
    conditions = config.get("conditions", [])

    row_count = _row_count(df)
    col_names = _column_names(df)

    failed: list[dict] = []
    for cond in conditions:
        ctype = cond["type"]
        if ctype == "row_count":
            op = cond["op"]
            threshold = cond["value"]
            if not _compare(row_count, op, threshold):
                failed.append({
                    "type": "row_count",
                    "op": op,
                    "value": threshold,
                    "actual": row_count,
                    "message": f"row_count {row_count} did not satisfy {op} {threshold}",
                })
        elif ctype == "schema_match":
            required = cond["columns"]
            missing = [c for c in required if c not in col_names]
            if missing:
                failed.append({
                    "type": "schema_match",
                    "missing_columns": missing,
                    "message": f"missing columns: {missing}",
                })

    if failed:
        raise FlagPauseSignal(conditions_failed=failed, gate_id=gate_id)

    return df


def _row_count(df) -> int:
    if df is None:
        return 0
    try:
        return len(df)
    except Exception:
        return 0


def _column_names(df) -> set:
    if df is None:
        return set()
    try:
        return set(df.columns)
    except Exception:
        return set()


def _compare(actual: int, op: str, threshold: int) -> bool:
    return {
        "lt": actual < threshold,
        "lte": actual <= threshold,
        "gt": actual > threshold,
        "gte": actual >= threshold,
        "eq": actual == threshold,
        "ne": actual != threshold,
    }[op]
