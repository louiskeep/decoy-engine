"""Strategy-to-rule + column pairings for the PERF.BASE.3 baseline.

One rule per strategy, paired with the fixture column it makes sense
to apply against. The baseline harness in
``scripts/run_perf_baseline.py`` reads ``BASELINE_CELLS`` and runs
each cell against every fixture tier.

Rule shapes were derived by reading each strategy's ``apply()`` in
``src/decoy_engine/transforms/``; comments below cite the file each
rule shape came from so future engine API drift is easy to follow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BaselineCell:
    """One (strategy, column, rule) baseline cell."""

    strategy: str
    column: str
    rule: dict[str, Any]
    # Reason a cell is skipped from the matrix; None means included.
    skip_reason: str | None = None


# Rule shapes derived from src/decoy_engine/transforms/<strategy>.py.
# Each rule is the minimum-viable config that exercises the strategy's
# hot path; the goal is "measure the per-row cost of this primitive,"
# not "match a production-tuned config."
BASELINE_CELLS: tuple[BaselineCell, ...] = (
    # CHEAP BAND -------------------------------------------------------
    BaselineCell(
        strategy="passthrough",
        column="customer_id",
        rule={"column": "customer_id", "type": "passthrough"},
    ),
    BaselineCell(
        strategy="redact",
        column="ssn",
        rule={"column": "ssn", "type": "redact", "redact_with": "XXX"},
    ),
    BaselineCell(
        strategy="truncate",
        column="ssn",
        rule={"column": "ssn", "type": "truncate", "length": 4},
    ),
    # MEDIUM BAND ------------------------------------------------------
    BaselineCell(
        strategy="faker",
        column="full_name",
        rule={"column": "full_name", "type": "faker", "faker_type": "name"},
    ),
    BaselineCell(
        strategy="date_shift",
        column="dob",
        rule={
            "column": "dob",
            "type": "date_shift",
            "min_days": -365,
            "max_days": 365,
        },
    ),
    BaselineCell(
        strategy="bucketize",
        column="score",
        # bucketize.py uses ``width`` for the bucket size; ``format`` for
        # the label shape. Width=100 over 300-850 produces ~5 buckets.
        rule={"column": "score", "type": "bucketize", "width": 100, "format": "range"},
    ),
    BaselineCell(
        strategy="hash",
        column="email",
        rule={"column": "email", "type": "hash"},
    ),
    BaselineCell(
        strategy="categorical",
        column="status",
        rule={
            "column": "status",
            "type": "categorical",
            "categories": ["active", "inactive", "pending", "closed"],
        },
    ),
    BaselineCell(
        strategy="shuffle",
        column="email",
        rule={"column": "email", "type": "shuffle"},
    ),
    # EXPENSIVE BAND ---------------------------------------------------
    BaselineCell(
        strategy="formula",
        column="transaction_amount",
        # formula.py reads ``formula`` (not ``expression``) and uses
        # ``value`` as the per-row variable name in safe_eval.
        rule={
            "column": "transaction_amount",
            "type": "formula",
            "formula": "value * 1.1",
        },
    ),
    BaselineCell(
        strategy="fpe",
        column="ssn",
        # fpe.py: digits charset over SSN-shape strings.
        # preserve_separators handles the embedded dashes.
        rule={
            "column": "ssn",
            "type": "fpe",
            "charset": "digits",
            "preserve_separators": True,
        },
    ),
    # SKIPPED ----------------------------------------------------------
    # reference needs ``reference: <path-to-file>`` + a side dataset.
    # That makes it an I/O-bound benchmark instead of a CPU one, which
    # is a different question from "what does the substrate change
    # buy us?" Re-include when the baseline grows an I/O-tier matrix
    # (PERF.BASE follow-on).
    BaselineCell(
        strategy="reference",
        column="zip",
        rule={"column": "zip", "type": "reference"},
        skip_reason=(
            "reference strategy requires a side dataset; benchmarked "
            "separately in the I/O matrix (out of scope for the CPU baseline)."
        ),
    ),
)


def included_cells() -> tuple[BaselineCell, ...]:
    """Return only the cells the baseline harness will run."""
    return tuple(c for c in BASELINE_CELLS if c.skip_reason is None)


def skipped_cells() -> tuple[BaselineCell, ...]:
    """Return only the cells the harness skips (for the report)."""
    return tuple(c for c in BASELINE_CELLS if c.skip_reason is not None)
