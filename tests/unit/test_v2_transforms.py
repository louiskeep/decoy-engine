"""S17-TX-NARROW: V2 narrow transform surface (6 ops; Endpoint A locked).

Schema cells assert the discriminated union accepts each op + rejects
bad shapes. Per-op execution cells exercise the pure apply_transform
helpers against pandas DataFrames. Compile-time validation cells pin
the typed-error contract (TransformError with .code).

Source patterns: V1 reference implementations in
decoy_engine.graph.ops.{filter_op, sort, limit, dedupe, derive,
drop_column} -- behavior intentionally mirrored, not the structure.
The V2 union is a leaner, audit-friendly shape.

NIST SP 800-188 §4 + ISO/IEC 20889 (de-identification transformation
primitives): filter + derive are recognized primitives in the
standards' taxonomy. The narrow surface here is the audit boundary.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.config import (
    DedupeOp,
    DeriveOp,
    DropColumnOp,
    FilterOp,
    LimitOp,
    PipelineConfig,
    SortOp,
)
from decoy_engine.execution._transforms import (
    TransformError,
    apply_transform,
    apply_transforms,
)

# ---------------------------------------------------------------------
# Schema acceptance + rejection at the PipelineConfig choke-point
# ---------------------------------------------------------------------


def _base_config_with_transforms(transforms: list[dict]) -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 0},
        "sources": {
            "t": {"type": "file", "format": "csv", "path": "/tmp/in.csv"},
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "c",
                        "strategy": "faker",
                        "provider": "person_email",
                        "namespace": "t_c",
                        "deterministic": True,
                    },
                ],
                "transforms": transforms,
            },
        ],
        "targets": {
            "t": {"type": "file", "format": "csv", "path": "/tmp/out.csv"},
        },
        "relationships": [],
        "namespaces": {"t_c": {"declared_by": ["t.c"]}},
    }


class TestTransformSchema:
    def test_accepts_filter_op(self):
        PipelineConfig.model_validate(
            _base_config_with_transforms([{"op": "filter", "expression": "age >= 18"}])
        )

    def test_accepts_sort_op_with_default_ascending(self):
        PipelineConfig.model_validate(_base_config_with_transforms([{"op": "sort", "by": ["age"]}]))

    def test_accepts_sort_op_with_per_column_ascending(self):
        PipelineConfig.model_validate(
            _base_config_with_transforms(
                [{"op": "sort", "by": ["age", "name"], "ascending": [False, True]}]
            )
        )

    def test_accepts_limit_op(self):
        PipelineConfig.model_validate(_base_config_with_transforms([{"op": "limit", "n": 100}]))

    def test_limit_op_rejects_negative_n(self):
        with pytest.raises(Exception):
            PipelineConfig.model_validate(_base_config_with_transforms([{"op": "limit", "n": -5}]))

    def test_accepts_dedupe_op_with_columns(self):
        PipelineConfig.model_validate(
            _base_config_with_transforms([{"op": "dedupe", "columns": ["email"]}])
        )

    def test_accepts_dedupe_op_without_columns(self):
        PipelineConfig.model_validate(_base_config_with_transforms([{"op": "dedupe"}]))

    def test_accepts_derive_op(self):
        PipelineConfig.model_validate(
            _base_config_with_transforms(
                [{"op": "derive", "column": "arpu", "expression": "revenue / users"}]
            )
        )

    def test_accepts_drop_column_op(self):
        PipelineConfig.model_validate(
            _base_config_with_transforms([{"op": "drop_column", "columns": ["pii_a", "pii_b"]}])
        )

    def test_rejects_unknown_op(self):
        # 'join' is one of the 9 cut ops (S22 / V2.1); it must not validate as a
        # narrow transform.
        with pytest.raises(Exception):
            PipelineConfig.model_validate(
                _base_config_with_transforms([{"op": "join", "left": "a", "right": "b"}])
            )

    def test_rejects_extra_field(self):
        # extra='forbid' protects against silent typo drops.
        with pytest.raises(Exception):
            PipelineConfig.model_validate(
                _base_config_with_transforms(
                    [{"op": "filter", "expression": "a > 0", "where": "z"}]
                )
            )

    def test_transforms_default_is_empty_list(self):
        cfg = PipelineConfig.model_validate(_base_config_with_transforms([]))
        assert cfg.tables[0].transforms == []


# ---------------------------------------------------------------------
# Per-op execution against real pandas DataFrames
# ---------------------------------------------------------------------


@pytest.fixture
def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "age": [25, 17, 40, 30, 22, 17],
            "country": ["US", "US", "CA", "US", "GB", "US"],
            "revenue": [100, 0, 200, 150, 80, 0],
            "users": [10, 0, 20, 15, 8, 0],
            "email": ["a@x", "b@y", "c@z", "a@x", "d@w", "b@y"],
        }
    )


class TestApplyFilter:
    def test_keeps_rows_matching_predicate(self, _sample_df):
        out = apply_transform(_sample_df, FilterOp(op="filter", expression="age >= 18"))
        # 17-year-olds dropped; remaining 4 rows.
        assert len(out) == 4
        assert (out["age"] >= 18).all()

    def test_compound_predicate(self, _sample_df):
        out = apply_transform(
            _sample_df,
            FilterOp(op="filter", expression="age >= 18 and country == 'US'"),
        )
        # 18+ AND US: 25 + 30 = 2 rows.
        assert len(out) == 2

    def test_bad_expression_raises_typed_error(self, _sample_df):
        with pytest.raises(TransformError) as exc:
            apply_transform(
                _sample_df,
                FilterOp(op="filter", expression="not_a_column > 0"),
            )
        assert exc.value.code == "filter_expression_error"


class TestApplySort:
    def test_sorts_ascending_by_one_column(self, _sample_df):
        out = apply_transform(_sample_df, SortOp(op="sort", by=["age"]))
        assert out["age"].tolist() == sorted(_sample_df["age"].tolist())

    def test_per_column_ascending(self, _sample_df):
        out = apply_transform(
            _sample_df,
            SortOp(op="sort", by=["country", "age"], ascending=[True, False]),
        )
        # First two rows: country='CA' (only one), then country='GB', then 'US' descending by age.
        # CA comes first alphabetically; within US, the row with age=30 precedes age=25 precedes age=17.
        countries = out["country"].tolist()
        assert countries[0] == "CA"
        assert countries[1] == "GB"
        us_ages = out[out["country"] == "US"]["age"].tolist()
        assert us_ages == sorted(us_ages, reverse=True)

    def test_missing_by_column_raises_typed_error(self, _sample_df):
        with pytest.raises(TransformError) as exc:
            apply_transform(
                _sample_df,
                SortOp(op="sort", by=["nonexistent_column"]),
            )
        assert exc.value.code == "sort_column_missing"

    def test_ascending_length_mismatch_raises(self, _sample_df):
        with pytest.raises(TransformError) as exc:
            apply_transform(
                _sample_df,
                SortOp(op="sort", by=["age", "country"], ascending=[True]),
            )
        assert exc.value.code == "sort_ascending_length_mismatch"


class TestApplyLimit:
    def test_caps_to_n_rows(self, _sample_df):
        out = apply_transform(_sample_df, LimitOp(op="limit", n=3))
        assert len(out) == 3

    def test_n_zero_returns_empty(self, _sample_df):
        out = apply_transform(_sample_df, LimitOp(op="limit", n=0))
        assert len(out) == 0
        # Schema columns preserved.
        assert list(out.columns) == list(_sample_df.columns)

    def test_n_larger_than_input_returns_all(self, _sample_df):
        out = apply_transform(_sample_df, LimitOp(op="limit", n=1000))
        assert len(out) == len(_sample_df)


class TestApplyDedupe:
    def test_dedupes_on_subset(self, _sample_df):
        out = apply_transform(_sample_df, DedupeOp(op="dedupe", columns=["email"]))
        # 6 rows, 4 unique emails ('a@x', 'b@y', 'c@z', 'd@w').
        assert len(out) == 4

    def test_dedupes_on_all_columns_when_columns_none(self, _sample_df):
        # Rows 1 and 5 are fully identical (age=17, country=US, revenue=0,
        # users=0, email=b@y); the all-columns dedupe drops one of them.
        out = apply_transform(_sample_df, DedupeOp(op="dedupe"))
        assert len(out) == 5

    def test_missing_column_raises_typed_error(self, _sample_df):
        with pytest.raises(TransformError) as exc:
            apply_transform(
                _sample_df,
                DedupeOp(op="dedupe", columns=["does_not_exist"]),
            )
        assert exc.value.code == "dedupe_column_missing"


class TestApplyDerive:
    def test_adds_computed_column(self, _sample_df):
        # Filter out zero-user rows first so we don't divide by zero in this cell.
        df = _sample_df[_sample_df["users"] > 0].reset_index(drop=True)
        out = apply_transform(
            df,
            DeriveOp(op="derive", column="arpu", expression="revenue / users"),
        )
        assert "arpu" in out.columns
        # arpu values: revenue/users
        assert out["arpu"].tolist() == (df["revenue"] / df["users"]).tolist()

    def test_existing_column_raises_typed_error(self, _sample_df):
        with pytest.raises(TransformError) as exc:
            apply_transform(
                _sample_df,
                DeriveOp(op="derive", column="age", expression="age + 1"),
            )
        assert exc.value.code == "derive_column_already_exists"

    def test_bad_expression_raises_typed_error(self, _sample_df):
        with pytest.raises(TransformError) as exc:
            apply_transform(
                _sample_df,
                DeriveOp(op="derive", column="x", expression="not_a_column + 1"),
            )
        assert exc.value.code == "derive_expression_error"


class TestExpressionScopeClamp:
    """Q16 + Dennis C1 regression: @var-style scope escapes must not
    execute side-effecting calls even with engine='numexpr'. pandas's
    @var resolver walks the caller's locals + globals BEFORE engine
    dispatch; the local_dict={} + global_dict={} pass in _apply_filter /
    _apply_derive clamps that walk to an empty scope.

    The payload exploits the module-top `pd` import (via @pd.compat.os);
    other reachable targets at the same depth would include any module
    imported in execution/_transforms.py.
    """

    _ESCAPE_EXPR = 'a + @pd.compat.os.system("echo DENNIS_C1_REGRESSION 1>&2")'

    def test_filter_blocks_at_var_escape(self):
        import pandas as pd

        df = pd.DataFrame({"a": [1, 2, 3]})
        with pytest.raises(TransformError) as exc:
            apply_transform(df, FilterOp(op="filter", expression=self._ESCAPE_EXPR))
        # The escape must NOT have executed -- TransformError fires
        # because the @var resolution is now blocked.
        assert exc.value.code == "filter_expression_error"

    def test_derive_blocks_at_var_escape(self):
        import pandas as pd

        df = pd.DataFrame({"a": [1, 2, 3]})
        with pytest.raises(TransformError) as exc:
            apply_transform(
                df,
                DeriveOp(op="derive", column="b", expression=self._ESCAPE_EXPR),
            )
        assert exc.value.code == "derive_expression_error"

    def test_filter_legitimate_column_reference_still_works(self):
        """Column references resolve through DataFrame's column scope,
        not local_dict / global_dict, so the clamp does not break them."""
        import pandas as pd

        df = pd.DataFrame({"age": [10, 20, 30]})
        out = apply_transform(df, FilterOp(op="filter", expression="age >= 20"))
        assert out["age"].tolist() == [20, 30]

    def test_derive_legitimate_arithmetic_still_works(self):
        import pandas as pd

        df = pd.DataFrame({"a": [1, 2, 3]})
        out = apply_transform(df, DeriveOp(op="derive", column="b", expression="a * 2"))
        assert out["b"].tolist() == [2, 4, 6]


class TestApplyDropColumn:
    def test_drops_named_columns(self, _sample_df):
        out = apply_transform(
            _sample_df,
            DropColumnOp(op="drop_column", columns=["revenue", "users"]),
        )
        assert "revenue" not in out.columns
        assert "users" not in out.columns
        # Other columns retained.
        assert "age" in out.columns

    def test_missing_column_raises_typed_error(self, _sample_df):
        with pytest.raises(TransformError) as exc:
            apply_transform(
                _sample_df,
                DropColumnOp(op="drop_column", columns=["nonexistent"]),
            )
        assert exc.value.code == "drop_column_missing"


# ---------------------------------------------------------------------
# Composition: ops apply in declared order
# ---------------------------------------------------------------------


class TestApplyTransformsInOrder:
    def test_filter_then_sort_then_limit_composes(self, _sample_df):
        ops = [
            FilterOp(op="filter", expression="age >= 18"),
            SortOp(op="sort", by=["age"], ascending=False),
            LimitOp(op="limit", n=2),
        ]
        out = apply_transforms(_sample_df, ops)
        # 4 rows pass the filter; top 2 by age desc are age=40 then age=30.
        assert len(out) == 2
        assert out["age"].tolist() == [40, 30]

    def test_derive_then_drop_column_composes(self, _sample_df):
        df = _sample_df[_sample_df["users"] > 0].reset_index(drop=True)
        ops = [
            DeriveOp(op="derive", column="arpu", expression="revenue / users"),
            DropColumnOp(op="drop_column", columns=["revenue", "users"]),
        ]
        out = apply_transforms(df, ops)
        # arpu is computed BEFORE revenue/users are dropped.
        assert "arpu" in out.columns
        assert "revenue" not in out.columns
        assert "users" not in out.columns

    def test_empty_ops_list_returns_df_unchanged(self, _sample_df):
        out = apply_transforms(_sample_df, [])
        assert out.equals(_sample_df)


# ---------------------------------------------------------------------
# S17 Phase B (Dennis S17 gate MEDIUM finding): compile-time cross-check.
# Reject configs that drop_column a column that ALSO has a mask strategy.
# ---------------------------------------------------------------------


class TestDropColumnMaskCrossCheck:
    def test_rejects_drop_column_for_masked_column(self):
        """A table with both `drop_column: [ssn]` AND mask `columns: [{name: ssn, ...}]`
        is rejected at PipelineConfig.model_validate. The previous behavior
        would fall through to a `v2_runner_unexpected_error` mid-strategy
        when the column was missing; this catches it at the choke-point."""
        cfg = _base_config_with_transforms(
            [
                {"op": "drop_column", "columns": ["c"]},  # `c` is the table's mask column
            ]
        )
        with pytest.raises(Exception) as exc:
            PipelineConfig.model_validate(cfg)
        assert "drop_column" in str(exc.value).lower()
        assert "c" in str(exc.value)

    def test_accepts_drop_column_for_unmasked_column(self):
        """Dropping a column that is NOT in the mask columns list is fine --
        the table just emits without that column. The cross-check only fires
        on overlap."""
        cfg = _base_config_with_transforms(
            [
                {"op": "drop_column", "columns": ["unrelated_column"]},
            ]
        )
        # No exception -> the validator accepted it (drop_column on a
        # non-mask column is the intended use).
        PipelineConfig.model_validate(cfg)

    def test_rejects_multi_column_drop_with_one_overlap(self):
        """If drop_column lists multiple columns AND one of them overlaps a
        mask column, the reject names ALL overlapping columns (sorted)."""
        cfg = _base_config_with_transforms(
            [
                {"op": "drop_column", "columns": ["a", "c", "b"]},  # c is the mask col
            ]
        )
        with pytest.raises(Exception) as exc:
            PipelineConfig.model_validate(cfg)
        # Reject names the conflict (sorted).
        assert "'c'" in str(exc.value) or "[c]" in str(exc.value) or "['c']" in str(exc.value)


class TestQa10F8FilterBooleanDtype:
    """QA-10 F8 (2026-06-01): filter accepts pandas nullable
    BooleanDtype masks. Pre-fix the equality check `mask.dtype != bool`
    rejected `pd.BooleanDtype()` which arises naturally from any
    pd.eval over nullable-integer or nullable-boolean columns (the
    default Arrow -> pandas conversion). Same fix shape as QA-3 F4
    closure on the masking-side `when_gate`."""

    def test_filter_accepts_nullable_boolean_mask(self):
        # A column with nullable Int64 forces pd.eval to produce a
        # BooleanDtype mask on the comparison expression.
        df = pd.DataFrame(
            {
                "v": ["a", "b", "c"],
                "n": pd.array([1, 2, None], dtype="Int64"),
            }
        )
        op = FilterOp(op="filter", expression="n == 1")
        out = apply_transform(df, op)
        # Row 0 matched (n == 1); rows 1, 2 dropped.
        assert len(out) == 1
        assert out["v"].iloc[0] == "a"

    def test_filter_still_rejects_non_boolean_dtype(self):
        # Sanity: a numeric expression that returns an Int64 series
        # is still rejected; the F8 fix relaxes the check to bool-like,
        # not to "any series."
        df = pd.DataFrame({"v": ["a", "b"], "n": [1, 2]})
        op = FilterOp(op="filter", expression="n + 1")
        with pytest.raises(TransformError) as exc:
            apply_transform(df, op)
        assert exc.value.code == "filter_expression_not_boolean"


class TestNumexprFallbackSurfaced:
    """Audit L1 (2026-06-12): pandas silently falls back from numexpr to
    the python engine on extension-array dtypes, emitting only an
    unmonitored RuntimeWarning -- the Q16 sandbox posture degraded
    invisibly. The fallback is now captured and re-emitted through the
    engine logger; the warning must not propagate to callers."""

    def test_fallback_logged_not_propagated(self, caplog):
        import logging
        import warnings as _warnings

        df = pd.DataFrame({"age": pd.array([10, 20, 30], dtype="Int64")})
        op = FilterOp(op="filter", expression="age >= 18")
        with _warnings.catch_warnings():
            _warnings.simplefilter("error", RuntimeWarning)  # propagation would raise
            with caplog.at_level(logging.WARNING, logger="decoy_engine.execution._transforms"):
                out = apply_transforms(df, [op])
        assert len(out) == 2
        fallback_logs = [r for r in caplog.records if "fell back" in r.message]
        assert fallback_logs, "numexpr fallback was not surfaced through the logger"

    def test_numexpr_native_path_logs_nothing(self, caplog):
        import logging

        df = pd.DataFrame({"age": [10, 20, 30]})
        op = FilterOp(op="filter", expression="age >= 18")
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution._transforms"):
            out = apply_transforms(df, [op])
        assert len(out) == 2
        assert not [r for r in caplog.records if "fell back" in r.message]
