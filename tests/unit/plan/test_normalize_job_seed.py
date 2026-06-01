"""MG-6 QA-triage close (2026-05-31): `_normalize_job_seed` reject path.

Locks Dennis QA-triage finding F7: two pipelines with intentionally
different (but malformed) seeds previously compiled to byte-identical
plans because `_normalize_job_seed` silently fell back to `seed_int = 0`
on a non-numeric value. Post-fix it raises `seed_not_numeric` instead.

Default behavior (missing key, None value) still produces seed 0;
non-numeric explicit values are now hard rejections.
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._compile import _normalize_job_seed
from decoy_engine.plan._errors import PlanCompileError


class TestDefaults:
    def test_missing_seed_key_defaults_to_zero(self):
        assert _normalize_job_seed({}) == (0).to_bytes(8, "big")

    def test_missing_global_settings_defaults_to_zero(self):
        assert _normalize_job_seed({"tables": []}) == (0).to_bytes(8, "big")

    def test_explicit_none_seed_defaults_to_zero(self):
        assert _normalize_job_seed({"global_settings": {"seed": None}}) == (0).to_bytes(8, "big")


class TestHappyPath:
    def test_zero_seed_normalizes(self):
        assert _normalize_job_seed({"global_settings": {"seed": 0}}) == (0).to_bytes(8, "big")

    def test_positive_int_normalizes(self):
        assert _normalize_job_seed({"global_settings": {"seed": 42}}) == (42).to_bytes(8, "big")

    def test_int_coercible_string_normalizes(self):
        # "42" coerces to int(42); this is legal per pre-fix behavior.
        assert _normalize_job_seed({"global_settings": {"seed": "42"}}) == (42).to_bytes(8, "big")


class TestNonNumericRejection:
    def test_non_numeric_string_raises_seed_not_numeric(self):
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": "not-a-number"}})
        assert exc.value.code == "seed_not_numeric"
        assert exc.value.path == "global_settings.seed"

    def test_dict_seed_raises_seed_not_numeric(self):
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": {"x": 1}}})
        assert exc.value.code == "seed_not_numeric"

    def test_list_seed_raises_seed_not_numeric(self):
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": [1, 2]}})
        assert exc.value.code == "seed_not_numeric"

    def test_two_distinct_malformed_seeds_now_diverge(self):
        """The Dennis QA-triage F7 regression: pre-fix, two different
        non-numeric seeds both fell back to 0 and produced byte-identical
        plans. Post-fix both raise; the determinism class of the seed
        normalization is preserved (no silent collisions)."""
        with pytest.raises(PlanCompileError):
            _normalize_job_seed({"global_settings": {"seed": "foo"}})
        with pytest.raises(PlanCompileError):
            _normalize_job_seed({"global_settings": {"seed": "bar"}})


class TestBoolFloatRejection:
    """QA-3 F1 (2026-05-31) close: bool + float seeds must reject.

    Locks the second-pass QA-triage finding F1: `seed: yes/no/true/false`
    is parsed by PyYAML as Python bool, and `int(True) = 1`,
    `int(False) = 0`. Two pipelines with `seed: true` and `seed: 1`
    previously compiled to byte-identical plans. Same story for
    `seed: 1.5`: `int(1.5) = 1` silently truncated.

    Post-fix both raise seed_not_numeric.
    """

    def test_seed_bool_true_rejected(self):
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": True}})
        assert exc.value.code == "seed_not_numeric"
        assert "bool" in exc.value.message
        assert "yes" in exc.value.message or "true" in exc.value.message

    def test_seed_bool_false_rejected(self):
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": False}})
        assert exc.value.code == "seed_not_numeric"
        assert "bool" in exc.value.message

    def test_seed_float_rejected(self):
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": 1.5}})
        assert exc.value.code == "seed_not_numeric"
        assert "float" in exc.value.message

    def test_seed_float_whole_number_also_rejected(self):
        # `seed: 5.0` reads as float; reject the same way to avoid the
        # operator wondering why some floats work and others do not.
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": 5.0}})
        assert exc.value.code == "seed_not_numeric"

    def test_error_message_has_no_sprint_internal_reference(self):
        # QA-3 F16: operator-facing message must not name the sprint
        # session that produced the fix. Operators reading the error
        # have no context for that string.
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": "not-a-number"}})
        msg = exc.value.message
        assert "Dennis" not in msg
        assert "QA triage" not in msg
        assert "session" not in msg
        assert "F7" not in msg


class TestOverflow:
    def test_seed_overflow_still_raises(self):
        # The pre-existing seed_overflow guard is unchanged by the
        # seed_not_numeric addition.
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": 2**64}})
        assert exc.value.code == "seed_overflow"

    def test_negative_seed_raises_overflow(self):
        with pytest.raises(PlanCompileError) as exc:
            _normalize_job_seed({"global_settings": {"seed": -1}})
        assert exc.value.code == "seed_overflow"
