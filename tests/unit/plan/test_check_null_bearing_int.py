"""engine-v2 S13 B1: null-bearing-int reject-at-validation compile check.

The plan-compile check `null_bearing_int_unsupported` (row 10) rejects an integer
+ null-bearing column masked under truncate/hash/categorical, because its masked
value is ambiguous across execution substrates (to_pandas widens int+null to
float; the polars-native path keeps int). PO-settled 2026-05-28. FK-child columns
are exempt (resolved via the edge, not masked; no divergence). Under
no_profile=True the check lands in checks_skipped (the execution-time guard is the
backstop there; that path is covered in tests/parity/test_strategy_substrate_parity.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from decoy_engine.plan._checks import check_null_bearing_int_unsupported
from decoy_engine.plan._compile import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile


def _profile(*columns: ColumnProfile, relationships: tuple[Relationship, ...] = ()) -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=10, columns=tuple(columns)),),
        relationships=relationships,
        profiled_at=datetime(2026, 5, 28, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _col(name: str, *, dtype: str, null_count: int) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=10,
        null_count=null_count,
        distinct_count=10,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


def _config(strategy: str, *, column: str = "c") -> dict[str, Any]:
    return {
        "tables": [
            {"name": "t", "columns": [{"name": column, "strategy": strategy, "provider": "p"}]}
        ]
    }


class TestNullBearingIntCompileCheck:
    @pytest.mark.parametrize("strategy", ["truncate", "hash", "categorical"])
    @pytest.mark.parametrize(
        "dtype", ["int64", "int64[pyarrow]", "Int64", "integer", "bigint", "intp", "uintp"]
    )
    def test_rejects_null_bearing_int(self, strategy: str, dtype: str) -> None:
        profile = _profile(_col("c", dtype=dtype, null_count=2))
        with pytest.raises(PlanCompileError) as exc:
            check_null_bearing_int_unsupported(_config(strategy), profile)
        assert exc.value.code == "null_bearing_int_unsupported"
        assert "t.c" in exc.value.path

    @pytest.mark.parametrize("strategy", ["truncate", "hash", "categorical"])
    def test_null_free_int_compiles(self, strategy: str) -> None:
        profile = _profile(_col("c", dtype="int64", null_count=0))
        check_null_bearing_int_unsupported(_config(strategy), profile)  # no raise

    @pytest.mark.parametrize("dtype", ["object", "string", "varchar", "float64", "boolean"])
    def test_non_integer_with_nulls_compiles(self, dtype: str) -> None:
        # Only INTEGER columns are ambiguous; string/float/bool with nulls are fine.
        profile = _profile(_col("c", dtype=dtype, null_count=3))
        check_null_bearing_int_unsupported(_config("hash"), profile)  # no raise

    @pytest.mark.parametrize("strategy", ["redact", "passthrough", "faker", "fpe"])
    def test_other_strategies_not_rejected(self, strategy: str) -> None:
        # Only truncate/hash/categorical hit the substrate divergence.
        profile = _profile(_col("c", dtype="int64", null_count=2))
        check_null_bearing_int_unsupported(_config(strategy), profile)  # no raise

    def test_fk_child_int_null_exempt(self) -> None:
        # An FK-child column is resolved via the edge, not masked; exempt even
        # when integer + null + hash.
        profile = _profile(
            _col("c", dtype="int64", null_count=2),
            relationships=(
                Relationship(
                    parent_table="parent",
                    parent_columns=("pid",),
                    child_table="t",
                    child_columns=("c",),
                    namespace="ns",
                ),
            ),
        )
        check_null_bearing_int_unsupported(_config("hash"), profile)  # no raise


class TestNullBearingIntUnderNoProfile:
    def test_skipped_under_no_profile(
        self, simple_config: dict[str, Any], simple_profile: Profile
    ) -> None:
        # Profile-dependent, so under no_profile the check is recorded in
        # checks_skipped (not silently dropped); the execution-time guard backstops.
        plan = compile_plan(
            simple_config, simple_profile, decoy_engine_version="0.1.0", no_profile=True
        )
        assert "null_bearing_int_unsupported" in plan.plan_compile.checks_skipped
        assert "null_bearing_int_unsupported" not in plan.plan_compile.checks_passed
