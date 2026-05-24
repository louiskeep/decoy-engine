"""Tests for FLAG gate op."""

import pandas as pd
import polars as pl
import pytest

from decoy_engine.exceptions import FlagPauseSignal
from decoy_engine.graph.ops import flag_gate
from decoy_engine.internal.validator import ValidationError


def _pd(records):
    return pd.DataFrame(records)


def _pl(records):
    return pl.DataFrame(records)


class TestValidateConfig:
    def test_valid_row_count(self):
        flag_gate.validate_config({"conditions": [{"type": "row_count", "op": "gte", "value": 1}]})

    def test_valid_schema_match(self):
        flag_gate.validate_config(
            {"conditions": [{"type": "schema_match", "columns": ["id", "email"]}]}
        )

    def test_valid_multiple_conditions(self):
        flag_gate.validate_config(
            {
                "conditions": [
                    {"type": "row_count", "op": "gte", "value": 1},
                    {"type": "schema_match", "columns": ["id"]},
                ]
            }
        )

    def test_empty_conditions_raises(self):
        with pytest.raises(ValidationError):
            flag_gate.validate_config({"conditions": []})

    def test_missing_conditions_raises(self):
        with pytest.raises(ValidationError):
            flag_gate.validate_config({})

    def test_invalid_op_raises(self):
        with pytest.raises(ValidationError):
            flag_gate.validate_config(
                {"conditions": [{"type": "row_count", "op": "bad", "value": 5}]}
            )

    def test_non_int_value_raises(self):
        with pytest.raises(ValidationError):
            flag_gate.validate_config(
                {"conditions": [{"type": "row_count", "op": "gte", "value": "5"}]}
            )

    def test_unknown_type_raises(self):
        with pytest.raises(ValidationError):
            flag_gate.validate_config({"conditions": [{"type": "unknown_type"}]})

    def test_empty_columns_raises(self):
        with pytest.raises(ValidationError):
            flag_gate.validate_config({"conditions": [{"type": "schema_match", "columns": []}]})


class TestApplyRowCount:
    def test_passes_when_satisfied(self):
        df = _pd([{"x": 1}, {"x": 2}])
        result = flag_gate.apply(
            [df], {"conditions": [{"type": "row_count", "op": "gte", "value": 1}]}
        )
        assert result is df

    def test_raises_when_not_satisfied(self):
        df = _pd([])
        with pytest.raises(FlagPauseSignal) as exc_info:
            flag_gate.apply([df], {"conditions": [{"type": "row_count", "op": "gte", "value": 1}]})
        assert exc_info.value.conditions_failed[0]["type"] == "row_count"

    def test_gate_id_propagated(self):
        df = _pd([])
        with pytest.raises(FlagPauseSignal) as exc_info:
            flag_gate.apply(
                [df],
                {
                    "gate_id": "my_gate",
                    "conditions": [{"type": "row_count", "op": "gt", "value": 0}],
                },
            )
        assert exc_info.value.gate_id == "my_gate"
        assert "my_gate" in str(exc_info.value)

    @pytest.mark.parametrize(
        "op,threshold,expect_pass",
        [
            ("lt", 10, True),
            ("lt", 5, False),
            ("lte", 5, True),
            ("lte", 4, False),
            ("gt", 4, True),
            ("gt", 5, False),
            ("gte", 5, True),
            ("gte", 6, False),
            ("eq", 5, True),
            ("eq", 4, False),
            ("ne", 4, True),
            ("ne", 5, False),
        ],
    )
    def test_all_ops(self, op, threshold, expect_pass):
        df = _pd([{"x": i} for i in range(5)])  # 5 rows
        cfg = {"conditions": [{"type": "row_count", "op": op, "value": threshold}]}
        if expect_pass:
            flag_gate.apply([df], cfg)
        else:
            with pytest.raises(FlagPauseSignal):
                flag_gate.apply([df], cfg)

    def test_multiple_failures_all_reported(self):
        df = _pd([])
        with pytest.raises(FlagPauseSignal) as exc_info:
            flag_gate.apply(
                [df],
                {
                    "conditions": [
                        {"type": "row_count", "op": "gt", "value": 0},
                        {"type": "row_count", "op": "gte", "value": 10},
                    ]
                },
            )
        assert len(exc_info.value.conditions_failed) == 2


class TestApplySchemaMatch:
    def test_passes_when_all_cols_present(self):
        df = _pd([{"id": 1, "email": "a@b.com"}])
        result = flag_gate.apply(
            [df], {"conditions": [{"type": "schema_match", "columns": ["id", "email"]}]}
        )
        assert result is df

    def test_raises_when_col_missing(self):
        df = _pd([{"id": 1}])
        with pytest.raises(FlagPauseSignal) as exc_info:
            flag_gate.apply(
                [df], {"conditions": [{"type": "schema_match", "columns": ["id", "email"]}]}
            )
        failed = exc_info.value.conditions_failed[0]
        assert failed["type"] == "schema_match"
        assert "email" in failed["missing_columns"]


class TestApplyPreRun:
    def test_no_input_zero_row_count_passes(self):
        result = flag_gate.apply(
            [], {"conditions": [{"type": "row_count", "op": "gte", "value": 0}]}
        )
        assert result is None

    def test_no_input_nonzero_row_count_fails(self):
        with pytest.raises(FlagPauseSignal):
            flag_gate.apply([], {"conditions": [{"type": "row_count", "op": "gt", "value": 0}]})


class TestApplyPolarsInput:
    def test_polars_frame_passes_row_count(self):
        df = _pl([{"id": 1, "name": "Alice"}])
        result = flag_gate.apply(
            [df], {"conditions": [{"type": "row_count", "op": "eq", "value": 1}]}
        )
        assert result is df

    def test_polars_frame_schema_match(self):
        df = _pl([{"id": 1, "email": "x@y.com"}])
        flag_gate.apply(
            [df], {"conditions": [{"type": "schema_match", "columns": ["id", "email"]}]}
        )


class TestMetadata:
    def test_kind(self):
        assert flag_gate.KIND == "flag_gate"

    def test_input_arity(self):
        assert flag_gate.INPUT_ARITY == (0, 1)

    def test_output_kind(self):
        assert flag_gate.OUTPUT_KIND == "stream"

    def test_native_engine(self):
        assert flag_gate.NATIVE_ENGINE == "pandas"


class TestFlagPauseSignalStr:
    def test_message_includes_detail(self):
        sig = FlagPauseSignal(
            [{"message": "row_count 0 did not satisfy gte 1"}],
            gate_id="check",
        )
        assert "check" in str(sig)
        assert "row_count" in str(sig)

    def test_message_no_gate_id(self):
        sig = FlagPauseSignal([{"message": "missing cols"}])
        assert str(sig).startswith("flag gate: ")


class _RecordingCtx:
    """Minimal ctx stub with .export so flag_gate can record warnings."""

    def __init__(self):
        self.exports: dict = {}

    def export(self, key, value):
        self.exports[key] = value


class TestOnFail:
    def test_default_is_pause(self):
        df = _pd([])
        with pytest.raises(FlagPauseSignal) as exc_info:
            flag_gate.apply(
                [df],
                {
                    "conditions": [{"type": "row_count", "op": "gte", "value": 1}],
                },
            )
        # Default on_fail = pause; failure record carries the explicit mode.
        assert exc_info.value.conditions_failed[0]["on_fail"] == "pause"

    def test_warn_does_not_raise(self):
        df = _pd([])
        ctx = _RecordingCtx()
        result = flag_gate.apply(
            [df],
            {
                "conditions": [
                    {"type": "row_count", "op": "gte", "value": 1, "on_fail": "warn"},
                ],
            },
            ctx=ctx,
        )
        # Returns the df unchanged + exports the warning.
        assert result is df
        warnings = ctx.exports.get("flag_gate_warnings")
        assert warnings and warnings["warnings"][0]["type"] == "row_count"
        assert warnings["warnings"][0]["on_fail"] == "warn"

    def test_pause_and_warn_mix_pauses(self):
        df = _pd([])
        ctx = _RecordingCtx()
        # First condition warns, second pauses. Pause wins.
        with pytest.raises(FlagPauseSignal) as exc_info:
            flag_gate.apply(
                [df],
                {
                    "conditions": [
                        {"type": "row_count", "op": "gte", "value": 1, "on_fail": "warn"},
                        {"type": "schema_match", "columns": ["id"], "on_fail": "pause"},
                    ],
                },
                ctx=ctx,
            )
        # Warning still got recorded even though pause raised.
        assert ctx.exports.get("flag_gate_warnings") is not None
        # Pause signal only carries the pause-mode failure, not the warn.
        types = [c["type"] for c in exc_info.value.conditions_failed]
        assert types == ["schema_match"]

    def test_invalid_on_fail_rejected(self):
        from decoy_engine.internal.validator import ValidationError

        with pytest.raises(ValidationError) as exc:
            flag_gate.validate_config(
                {
                    "conditions": [
                        {"type": "row_count", "op": "gte", "value": 1, "on_fail": "bogus"},
                    ],
                }
            )
        assert "on_fail" in (exc.value.path or "")
