"""leakage scan (engine-v2 S10): the masked output does not expose the source.

The leak contract differs by strategy class (Dennis S10 B1 fix):

- SUBSTITUTION strategies (hash, faker, fpe, redact, truncate, date_shift,
  bucketize, formula, composite, FK-resolved) replace a value with a NEW one, so
  any source value reappearing in the output is genuine leakage -> set-membership
  check, HARD fail.
- VALUE-REUSE strategies (shuffle, categorical) legitimately re-emit source values
  (a shuffle is a permutation; categorical re-emits a category), so set-membership
  is meaningless for them. The meaningful privacy property is POSITIONAL: a row
  that did not move (output[i] == source[i]) is a fixed point. Fixed points are
  expected to be rare and are a WARNING, never a hard fail (a documented
  fixed-point allowance).

passthrough is excluded entirely (its output equals the source by design). Every
warning carries only a COUNT, never the value, so the manifest never echoes PII.
"""

from __future__ import annotations

from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.validation.post._scan import (
    ScanContext,
    ScanOutcome,
    column_values,
    masked_columns,
)

_NAME = "leakage"
# Strategies whose output re-emits source values by design; set-membership leakage
# is wrong for them (it would flag every row), so they get a positional check.
_VALUE_REUSE_STRATEGIES = frozenset({"shuffle", "categorical"})


def run_leakage(ctx: ScanContext) -> ScanOutcome:
    failed = False
    warnings: list[QualityWarning] = []
    for table_name, col_name, strategy in masked_columns(ctx.plan):
        if strategy == "passthrough":
            continue  # passthrough output == source by design; not a leak
        out_table = ctx.outputs.get(table_name)
        src_table = ctx.sources.get(table_name)
        if out_table is None or src_table is None:
            continue
        out_vals = column_values(out_table, col_name)
        src_vals = column_values(src_table, col_name)

        if strategy in _VALUE_REUSE_STRATEGIES:
            # Positional fixed-point check: a value-reuse strategy is expected to
            # re-emit source values, so only a row that did NOT move is notable.
            # Rare self-maps warn (documented allowance); never a hard fail.
            if len(out_vals) != len(src_vals):
                continue
            fixed_points = sum(
                1
                for out_v, src_v in zip(out_vals, src_vals, strict=True)
                if out_v is not None and src_v is not None and out_v == src_v
            )
            if fixed_points:
                warnings.append(
                    QualityWarning(
                        code="value_reuse_fixed_point",
                        provider=strategy,
                        column=col_name,
                        detail={"table": table_name, "fixed_point_count": fixed_points},
                    )
                )
            continue

        # Substitution strategy: any source value in the output is genuine leakage.
        source_values = {v for v in src_vals if v is not None}
        if not source_values:
            continue
        leaked = {v for v in out_vals if v is not None and v in source_values}
        if leaked:
            failed = True
            warnings.append(
                QualityWarning(
                    code="source_value_leak",
                    provider=strategy,
                    column=col_name,
                    detail={"table": table_name, "leaked_count": len(leaked)},
                )
            )
    return ScanOutcome(name=_NAME, failed=failed, warnings=tuple(warnings))
