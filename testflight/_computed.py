"""Computed-column correctness invariant (check_computed_columns).

Split from _invariants.py to keep both modules within the 600-line limit.
Imported and re-exported by _invariants.py for backwards compatibility.
"""

from __future__ import annotations

from typing import Any

from ._spec import ComputedColumnSpec


def _recompute_line_total(row: dict[str, Any]) -> float:
    """Pure-Python recomputation of line_total from the output row."""
    la = row["line_amount"]
    u = row["units"]
    dt = row["discount_tier"]
    if dt == "copay":
        factor = 0.80
    elif dt == "preferred":
        factor = 0.90
    else:
        factor = 1.0
    return la * u * factor


def _recompute_order_total(row: dict[str, Any]) -> float:
    """Pure-Python recomputation of order_total = qty * unit_price."""
    return float(row["qty"]) * float(row["unit_price"])


def _recompute_tier(order_total: float) -> str:
    """Pure-Python recomputation of tier from order_total (case_when, 3 branches)."""
    if order_total >= 1000.0:
        return "premium"
    if order_total >= 200.0:
        return "standard"
    return "economy"


def check_computed_columns(
    job_name: str,
    spec: list[ComputedColumnSpec],
    result: Any,
) -> str:
    """Assert derived / case_when / derived_aggregate columns are correct.

    For each ComputedColumnSpec, recomputes the expected value in pure Python
    from the output's input columns and asserts equality. For case_when columns
    with branch_count > 0, also asserts that every branch is exercised by at
    least one output row (branch-coverage guard: an unused branch could hide a
    bug). For derived_aggregate, asserts the single scalar equals the Python
    aggregate of the sibling column broadcast to all rows.

    Column-specific recomputation is hardcoded for Job A. Phase 4 will
    generalise this to a formula-language interpreter.

    Args:
        job_name: Job name for error messages.
        spec: List of ComputedColumnSpec from the manifest invariants.
        result: ExecutionResult carrying all output tables.

    Returns:
        Short evidence string summarising what was verified.

    Raises:
        AssertionError: If a computed value is wrong or a branch is unexercised.
    """
    checked: list[str] = []
    for cs in spec:
        tbl = result.outputs.get(cs.table)
        assert tbl is not None, (
            f"[{job_name}] computed_columns: table '{cs.table}' not in result.outputs."
        )
        col_dict = tbl.to_pydict()

        if cs.column == "line_total":
            # Derived: line_amount * units * discount_factor (case_when, 3 branches).
            la_vals = col_dict["line_amount"]
            un_vals = col_dict["units"]
            dt_vals = col_dict["discount_tier"]
            lt_vals = col_dict["line_total"]

            errors = []
            for i in range(len(lt_vals)):
                expected = _recompute_line_total(
                    {
                        "line_amount": la_vals[i],
                        "units": un_vals[i],
                        "discount_tier": dt_vals[i],
                    }
                )
                actual = lt_vals[i]
                if abs(actual - expected) > 1e-6:
                    errors.append((i, expected, actual, dt_vals[i]))
                    if len(errors) >= 5:
                        break

            assert not errors, (
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"{len(errors)} incorrect values (first 5): {errors}."
            )

            # Branch-coverage check.
            if cs.branch_count > 0:
                seen_branches = set(dt_vals)
                required_branches = {"copay", "preferred", "standard"}
                missing = required_branches - seen_branches
                assert not missing, (
                    f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                    f"case_when branch_count={cs.branch_count} but "
                    f"branches {missing} are not exercised by any output row. "
                    f"Branches present: {seen_branches}. "
                    f"A missing branch could hide a formula bug."
                )
            checked.append(
                f"{cs.table}.{cs.column}(rows={len(lt_vals)},branches={cs.branch_count})"
            )

        elif cs.column == "claim_line_sum":
            # Derived aggregate: sum(line_amount) across all rows, broadcast.
            la_vals = col_dict["line_amount"]
            cls_vals = col_dict["claim_line_sum"]

            expected_sum = sum(la_vals)
            for i, v in enumerate(cls_vals):
                assert abs(v - expected_sum) < 1e-4, (
                    f"[{job_name}] computed_columns: {cs.table}.{cs.column} "
                    f"row {i}: value={v}, expected scalar sum={expected_sum}."
                )
            checked.append(f"{cs.table}.{cs.column}(sum={expected_sum:.2f},rows={len(cls_vals)})")

        elif cs.column == "order_total":
            # Derived: qty * unit_price (no case_when branches).
            qty_vals = col_dict["qty"]
            up_vals = col_dict["unit_price"]
            ot_vals = col_dict["order_total"]

            ot_errors: list[tuple[int, float, float]] = []
            for i in range(len(ot_vals)):
                ot_expected = _recompute_order_total({"qty": qty_vals[i], "unit_price": up_vals[i]})
                ot_actual = float(ot_vals[i])
                if abs(ot_actual - ot_expected) > 1e-4:
                    ot_errors.append((i, ot_expected, ot_actual))
                    if len(ot_errors) >= 5:
                        break

            assert not ot_errors, (
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"{len(ot_errors)} incorrect values (first 5): {ot_errors}."
            )
            checked.append(f"{cs.table}.{cs.column}(rows={len(ot_vals)})")

        elif cs.column == "tier":
            # Derived: case_when(order_total >= 1000, "premium", >= 200, "standard", "economy").
            ot_vals = col_dict["order_total"]
            tier_vals = col_dict["tier"]

            tier_errors: list[tuple[int, str, str, Any]] = []
            for i in range(len(tier_vals)):
                tier_expected = _recompute_tier(float(ot_vals[i]))
                actual = tier_vals[i]
                if actual != tier_expected:
                    tier_errors.append((i, tier_expected, actual, ot_vals[i]))
                    if len(tier_errors) >= 5:
                        break

            assert not tier_errors, (
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"{len(tier_errors)} incorrect values (first 5): {tier_errors}."
            )

            # Branch-coverage check: all three tiers must appear in output.
            if cs.branch_count > 0:
                seen_tiers = set(tier_vals)
                required_tiers = {"premium", "standard", "economy"}
                missing = required_tiers - seen_tiers
                assert not missing, (
                    f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                    f"case_when branch_count={cs.branch_count} but "
                    f"tier(s) {missing} are not exercised by any output row. "
                    f"Tiers present: {seen_tiers}. "
                    f"A missing branch could hide a formula bug."
                )
            checked.append(
                f"{cs.table}.{cs.column}(rows={len(tier_vals)},branches={cs.branch_count})"
            )

        elif cs.column == "derived_flag":
            # Derived case_when: amount > 60 -> "high", > 40 -> "mid", else "low".
            # Used by Job C synthetic_events (generate table).
            amount_vals = col_dict["amount"]
            flag_vals = col_dict["derived_flag"]

            flag_errors: list[tuple[int, str, str, Any]] = []
            for i in range(len(flag_vals)):
                a = amount_vals[i]
                if a is None:
                    expected_flag = "low"
                elif float(a) > 60.0:
                    expected_flag = "high"
                elif float(a) > 40.0:
                    expected_flag = "mid"
                else:
                    expected_flag = "low"
                actual_flag = flag_vals[i]
                if actual_flag != expected_flag:
                    flag_errors.append((i, expected_flag, actual_flag, a))
                    if len(flag_errors) >= 5:
                        break

            assert not flag_errors, (
                f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                f"{len(flag_errors)} incorrect values (first 5): {flag_errors}."
            )

            # Branch-coverage check.
            if cs.branch_count > 0:
                seen_flags = set(flag_vals)
                required_flags = {"high", "mid", "low"}
                missing_flags = required_flags - seen_flags
                assert not missing_flags, (
                    f"[{job_name}] computed_columns: {cs.table}.{cs.column}: "
                    f"case_when branch_count={cs.branch_count} but "
                    f"branches {missing_flags} are not exercised by any output row."
                )
            checked.append(
                f"{cs.table}.{cs.column}(rows={len(flag_vals)},branches={cs.branch_count})"
            )

        elif cs.column == "rolling_total":
            # Derived aggregate: sum(amount) broadcast to all rows.
            # Used by Job C synthetic_events (generate table).
            amount_vals = col_dict["amount"]
            rt_vals = col_dict["rolling_total"]

            expected_rt = sum(float(v) for v in amount_vals if v is not None)
            for i, v in enumerate(rt_vals):
                assert abs(float(v) - expected_rt) < 1e-2, (
                    f"[{job_name}] computed_columns: {cs.table}.{cs.column} "
                    f"row {i}: value={v}, expected scalar sum={expected_rt:.4f}."
                )
            checked.append(f"{cs.table}.{cs.column}(sum={expected_rt:.2f},rows={len(rt_vals)})")

        else:
            # Unknown column: fail loudly rather than silently skip.
            raise AssertionError(
                f"[{job_name}] computed_columns: no recomputation registered "
                f"for {cs.table}.{cs.column}. "
                f"Add a case to check_computed_columns for this column."
            )

    return "checked=" + ",".join(checked)
