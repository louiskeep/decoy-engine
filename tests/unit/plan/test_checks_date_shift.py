"""HC-3(a): plan-compile check for date_shift's `group_by` (entity-anchor).

Mirrors `plan/_checks_group_key.py` / `plan/_checks_windowed_date.py`'s
pattern and their test modules (`tests/unit/transforms/test_group_key.py`'s
`TestGroupKeyPlanCheck`, `tests/unit/transforms/test_windowed_date.py`).
date_shift is mask-kind only (no generate-kind `type: date_shift`), so
unlike those two this check has a single loop over `columns`.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.plan._checks_date_shift import check_date_shift_group_by_refs
from decoy_engine.plan._errors import PlanCompileError


def _config(provider_config: dict[str, Any], *, extra_columns: list[dict[str, Any]] | None = None):
    columns = [
        {
            "name": "dob",
            "strategy": "date_shift",
            "namespace": "dob_ns",
            "provider_config": provider_config,
        }
    ]
    columns.extend(extra_columns or [])
    return {"tables": [{"name": "t", "columns": columns}]}


class TestCheckDateShiftGroupByRefs:
    def test_no_group_by_passes(self) -> None:
        cfg = _config({"min_days": -30, "max_days": 30})
        check_date_shift_group_by_refs(cfg)  # no raise

    def test_valid_group_by_passes(self) -> None:
        cfg = _config(
            {"min_days": -30, "max_days": 30, "group_by": "patient_id"},
            extra_columns=[{"name": "patient_id", "strategy": "passthrough"}],
        )
        check_date_shift_group_by_refs(cfg)  # no raise

    def test_group_by_same_as_date_column_passes(self) -> None:
        """Degenerate but valid per the guide: group_by == the date column
        itself is not special-cased (it's a legitimate self-reference)."""
        cfg = _config({"min_days": -30, "max_days": 30, "group_by": "dob"})
        check_date_shift_group_by_refs(cfg)  # no raise

    def test_missing_group_by_column_raises(self) -> None:
        cfg = _config({"min_days": -30, "max_days": 30, "group_by": "nonexistent_col"})
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(cfg)
        assert exc.value.code == "date_shift_missing_group_by_ref"
        assert "nonexistent_col" in str(exc.value)
        assert exc.value.path == "tables.t.columns.dob.provider_config.group_by"

    def test_non_date_shift_columns_ignored(self) -> None:
        """A different strategy's provider_config.group_by is not this
        check's concern (group_key has its own check module)."""
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "key",
                            "strategy": "group_key",
                            "provider_config": {"group_by": "nonexistent_col"},
                        }
                    ],
                }
            ]
        }
        check_date_shift_group_by_refs(cfg)  # no raise: not a date_shift column

    def test_multiple_tables_checked_independently(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "good",
                    "columns": [
                        {"name": "patient_id", "strategy": "passthrough"},
                        {
                            "name": "dob",
                            "strategy": "date_shift",
                            "namespace": "ns",
                            "provider_config": {"group_by": "patient_id"},
                        },
                    ],
                },
                {
                    "name": "bad",
                    "columns": [
                        {
                            "name": "dob",
                            "strategy": "date_shift",
                            "namespace": "ns",
                            "provider_config": {"group_by": "missing"},
                        },
                    ],
                },
            ]
        }
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(cfg)
        assert exc.value.path == "tables.bad.columns.dob.provider_config.group_by"


class TestNonStringGroupByRejected:
    """Codex R1 P2 #3: a non-string group_by ref (`group_by: 123`) survives a
    bare `str(group_by) in cols` membership test if a column named "123"
    exists, but execution does `df[123]` -> KeyError. Reject at compile."""

    def test_int_group_by_rejected_even_with_matching_named_column(self) -> None:
        cfg = _config(
            {"min_days": -30, "max_days": 30, "group_by": 123},
            extra_columns=[{"name": "123", "strategy": "passthrough"}],
        )
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(cfg)
        assert exc.value.code == "date_shift_missing_group_by_ref"
        assert exc.value.path == "tables.t.columns.dob.provider_config.group_by"

    def test_empty_string_group_by_rejected(self) -> None:
        cfg = _config({"min_days": -30, "max_days": 30, "group_by": ""})
        # An empty string is falsy, so it is treated as "no group_by" (not an
        # error): the handler never dereferences it. No raise.
        check_date_shift_group_by_refs(cfg)

    def test_list_group_by_rejected(self) -> None:
        cfg = _config({"min_days": -30, "max_days": 30, "group_by": ["patient_id"]})
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(cfg)
        assert exc.value.code == "date_shift_missing_group_by_ref"


class TestNestedGroupByRejected:
    """Codex R1 P2 #4: date_shift+group_by inside a `nested` strategy child is
    rejected -- the nested child masks a synthetic single-column leaf batch
    (`_nested_leaves`) with no sibling columns, so the entity anchor can never
    be present. Fail closed at compile instead of KeyError at execution."""

    def test_nested_date_shift_with_group_by_rejected(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "patient_id", "strategy": "passthrough"},
                        {
                            "name": "payload",
                            "strategy": "nested",
                            "namespace": "ns",
                            "provider_config": {
                                "strategy": "date_shift",
                                "target": "$.dates[*]",
                                "strategy_config": {
                                    "min_days": -30,
                                    "max_days": 30,
                                    "group_by": "patient_id",
                                },
                            },
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(cfg)
        assert exc.value.code == "date_shift_group_by_unsupported_in_nested"
        assert exc.value.path == "tables.t.columns.payload.provider_config.strategy_config.group_by"

    def test_nested_date_shift_without_group_by_passes(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "payload",
                            "strategy": "nested",
                            "namespace": "ns",
                            "provider_config": {
                                "strategy": "date_shift",
                                "target": "$.dates[*]",
                                "strategy_config": {"min_days": -30, "max_days": 30},
                            },
                        }
                    ],
                }
            ]
        }
        check_date_shift_group_by_refs(cfg)  # no raise


class TestWhenPlusGroupByRejected:
    """Codex R1 P1 #1 residual: `when` + `group_by` on a date_shift column is
    fail-closed at compile. The pre-mask anchor is label-aligned, which is
    correct on pandas but silently mis-anchors on the polars-native when-gate
    (fresh RangeIndex after filter), so the combination is rejected until
    per-route positional anchoring lands."""

    def _cfg_with_when(self, when: str | None) -> dict[str, Any]:
        dob: dict[str, Any] = {
            "name": "dob",
            "strategy": "date_shift",
            "namespace": "dob_ns",
            "provider_config": {"min_days": -30, "max_days": 30, "group_by": "patient_id"},
        }
        if when is not None:
            dob["when"] = when
        return {
            "tables": [
                {
                    "name": "t",
                    "columns": [dob, {"name": "patient_id", "strategy": "passthrough"}],
                }
            ]
        }

    def test_when_with_group_by_rejected(self) -> None:
        cfg = self._cfg_with_when("record_type == 'inpatient'")
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(cfg)
        assert exc.value.code == "date_shift_group_by_with_when_unsupported"
        assert exc.value.path == "tables.t.columns.dob.when"

    def test_group_by_without_when_still_passes(self) -> None:
        # The guard must not reject the common (no-`when`) group_by case.
        check_date_shift_group_by_refs(self._cfg_with_when(None))  # no raise

    def test_when_without_group_by_passes(self) -> None:
        # `when` alone (no group_by) is unaffected by this check.
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "dob",
                            "strategy": "date_shift",
                            "namespace": "dob_ns",
                            "provider_config": {"min_days": -30, "max_days": 30},
                            "when": "record_type == 'inpatient'",
                        }
                    ],
                }
            ]
        }
        check_date_shift_group_by_refs(cfg)  # no raise


class TestGroupByOnFkColumnRejected:
    """Codex R2 P1s: date_shift+group_by on an FK-participating column is
    fail-closed at compile. A per-entity anchored shift makes equal FK keys mask
    to different dates -- breaking RI on the chunked FK self-masking route (FK
    child) and mis-anchoring orphan REMAP (FK parent). date_shift WITHOUT
    group_by stays FK-safe, so only the combination is rejected."""

    def _cfg(self, *, group_by: str | None, as_parent: bool) -> dict[str, Any]:
        # `visit_date` is the FK column that is also date_shift'd.
        pc: dict[str, Any] = {"min_days": -30, "max_days": 30}
        if group_by is not None:
            pc["group_by"] = group_by
        visits_cols = [
            {
                "name": "visit_date",
                "strategy": "date_shift",
                "namespace": "vd",
                "provider_config": pc,
            },
            {"name": "patient_id", "strategy": "passthrough"},
        ]
        other_cols = [{"name": "visit_date", "strategy": "passthrough"}]
        # date_shift column is the parent end if as_parent, else the child end.
        parent_tbl, child_tbl = ("visits", "encounters") if as_parent else ("encounters", "visits")
        rel = {
            "parent": {"table": parent_tbl, "columns": ["visit_date"]},
            "children": [{"table": child_tbl, "columns": ["visit_date"]}],
            "orphan_policy": "remap",
        }
        return {
            "tables": [
                {"name": "visits", "columns": visits_cols},
                {"name": "encounters", "columns": other_cols},
            ],
            "relationships": [rel],
        }

    def test_group_by_on_fk_parent_column_rejected(self) -> None:
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(self._cfg(group_by="patient_id", as_parent=True))
        assert exc.value.code == "date_shift_group_by_on_fk_column_unsupported"

    def test_group_by_on_fk_child_column_rejected(self) -> None:
        with pytest.raises(PlanCompileError) as exc:
            check_date_shift_group_by_refs(self._cfg(group_by="patient_id", as_parent=False))
        assert exc.value.code == "date_shift_group_by_on_fk_column_unsupported"

    def test_fk_date_shift_without_group_by_passes(self) -> None:
        # date_shift on an FK column is FK-safe WITHOUT group_by (per-value
        # deterministic), so it must NOT be rejected.
        check_date_shift_group_by_refs(self._cfg(group_by=None, as_parent=True))  # no raise

    def test_group_by_on_non_fk_column_passes(self) -> None:
        # No relationships: the same config is fine.
        cfg = self._cfg(group_by="patient_id", as_parent=True)
        cfg["relationships"] = []
        check_date_shift_group_by_refs(cfg)  # no raise
