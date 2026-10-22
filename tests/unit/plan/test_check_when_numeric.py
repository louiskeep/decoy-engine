"""§WHEN-NUMERIC: reject-at-validation for a `when:`-gated string-output mask on
a numeric column.

The plan-compile check `when_string_output_on_numeric_unsupported` rejects a
`when:` row gate combined with a string-output strategy (`truncate`, or `redact`
with a string replacement) on an integer or float column: matched rows would be
strings and unmatched rows numbers, which has no single Arrow type and crashes
the pandas route at reconstruction (`pyarrow.ArrowInvalid`). `hash` is left to its
own dedicated guards. FK-child columns are exempt (resolved via the edge). Under
no_profile=True the check lands in checks_skipped. Plan:
docs/plans/2026-10-22-engine-correctness-when-numeric.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from decoy_engine.plan._checks_correctness import check_when_string_output_on_numeric_unsupported
from decoy_engine.plan._compile import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile


def _profile(*columns: ColumnProfile, relationships: tuple[Relationship, ...] = ()) -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=10, columns=tuple(columns)),),
        relationships=relationships,
        profiled_at=datetime(2026, 10, 22, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _col(name: str, *, dtype: str) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=10,
        null_count=0,
        distinct_count=10,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


def _config(
    strategy: str,
    *,
    when: str | None = "c > 2",
    provider_config: dict[str, Any] | None = None,
    column: str = "c",
) -> dict[str, Any]:
    col: dict[str, Any] = {"name": column, "strategy": strategy, "provider": "p"}
    if when is not None:
        col["when"] = when
    if provider_config is not None:
        col["provider_config"] = provider_config
    return {"tables": [{"name": "t", "columns": [col]}]}


_NUMERIC_DTYPES = ["int64", "int64[pyarrow]", "Int64", "integer", "bigint", "float64", "Float32"]


class TestWhenNumericRejected:
    @pytest.mark.parametrize("dtype", _NUMERIC_DTYPES)
    def test_rejects_redact_string_replacement_on_numeric(self, dtype: str) -> None:
        profile = _profile(_col("c", dtype=dtype))
        config = _config("redact", provider_config={"redact_with": "X"})
        with pytest.raises(PlanCompileError) as exc:
            check_when_string_output_on_numeric_unsupported(config, profile)
        assert exc.value.code == "when_string_output_on_numeric_unsupported"
        assert "t.c" in exc.value.path

    @pytest.mark.parametrize("dtype", _NUMERIC_DTYPES)
    def test_rejects_truncate_on_numeric(self, dtype: str) -> None:
        profile = _profile(_col("c", dtype=dtype))
        config = _config("truncate", provider_config={"length": 1})
        with pytest.raises(PlanCompileError) as exc:
            check_when_string_output_on_numeric_unsupported(config, profile)
        assert exc.value.code == "when_string_output_on_numeric_unsupported"

    def test_rejects_redact_default_replacement_on_numeric(self) -> None:
        # No explicit redact_with -> default "REDACTED" (a string) -> rejected.
        profile = _profile(_col("c", dtype="int64"))
        with pytest.raises(PlanCompileError):
            check_when_string_output_on_numeric_unsupported(_config("redact"), profile)


class TestWhenNumericAllowed:
    def test_redact_with_numeric_replacement_allowed(self) -> None:
        # A numeric replacement keeps the column numeric -> no mixed-type crash.
        profile = _profile(_col("c", dtype="int64"))
        config = _config("redact", provider_config={"redact_with": 0})
        check_when_string_output_on_numeric_unsupported(config, profile)  # no raise

    @pytest.mark.parametrize("strategy", ["redact", "truncate"])
    def test_string_output_on_non_numeric_allowed(self, strategy: str) -> None:
        # The mixed-type crash is specific to a numeric source; a string column
        # under the same gate is fine.
        profile = _profile(_col("c", dtype="object"))
        config = _config(strategy, provider_config={"redact_with": "X", "length": 1})
        check_when_string_output_on_numeric_unsupported(config, profile)  # no raise

    @pytest.mark.parametrize("strategy", ["redact", "truncate"])
    def test_no_when_allowed(self, strategy: str) -> None:
        # Whole-column masking retypes the column uniformly -> fine.
        profile = _profile(_col("c", dtype="int64"))
        config = _config(strategy, when=None, provider_config={"redact_with": "X", "length": 1})
        check_when_string_output_on_numeric_unsupported(config, profile)  # no raise

    def test_hash_not_handled_here(self) -> None:
        # hash has its own dedicated guards (gated_hash_on_integer_unsupported /
        # float_canonicalization_unsupported); this check must not fire on it.
        profile = _profile(_col("c", dtype="int64"))
        check_when_string_output_on_numeric_unsupported(_config("hash"), profile)  # no raise

    def test_fk_child_numeric_exempt(self) -> None:
        # An FK-child column is resolved through the edge, not written by this
        # strategy, so it is exempt (mirrors null_bearing_int).
        profile = _profile(
            _col("c", dtype="int64"),
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
        config = _config("redact", provider_config={"redact_with": "X"})
        check_when_string_output_on_numeric_unsupported(config, profile)  # no raise


class TestWhenNumericUnderNoProfile:
    def test_skipped_under_no_profile(
        self, simple_config: dict[str, Any], simple_profile: Profile
    ) -> None:
        plan = compile_plan(
            simple_config, simple_profile, decoy_engine_version="0.1.0", no_profile=True
        )
        assert "when_string_output_on_numeric_unsupported" in plan.plan_compile.checks_skipped
        assert "when_string_output_on_numeric_unsupported" not in plan.plan_compile.checks_passed
