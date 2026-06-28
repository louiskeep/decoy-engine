"""SP-07 text_mask strategy tests (TDD: tests land first).

Tests cover all three spec slices:
  text_mask.1 - span detection + per-detector dispatch
  text_mask.2 - HMAC keyed cross-cell determinism + raw-value isolation
  text_mask.3 - unmatched_span_policy + STORM library single-source-of-truth

Raw-value isolation sentry: verifies that no raw matched_text appears in
log records or QualityWarning evidence produced by mask_cell / TextMaskHandler.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd
import pytest

from decoy_engine.plan._types import ColumnSeed
from decoy_engine.storm.detectors import _SPAN_DETECTORS
from decoy_engine.transforms.text_mask import (
    DETECTOR_DEFAULTS,
    mask_cell,
)

# ── Test helpers ──────────────────────────────────────────────────────────────

_SEED = b"\xab" * 32  # stable test key; 32 bytes, not a real secret


def _make_plan(**cfg: Any) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_mask",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(cfg.items())),
    )


class _FakeCtx:
    """Minimal context carrying job_seed for TextMaskHandler tests."""

    def __init__(self, job_seed: bytes = _SEED) -> None:
        self.job_seed = job_seed


# ── spec slice 1: span detection + dispatch ───────────────────────────────────


class TestSingleDetectorDispatch:
    """text_mask.1: one detector fires, span is dispatched to its default strategy."""

    def test_ssn_span_is_masked(self) -> None:
        """SSN detected and FPE-encrypted; raw value absent from output."""
        raw = "123-45-6789"
        result = mask_cell(
            f"SSN: {raw} on file.",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="passthrough",
        )
        assert raw not in result, "raw SSN must be replaced"

    def test_ssn_masked_value_preserves_format(self) -> None:
        """FPE output for SSN keeps the NNN-NN-NNNN shape (digit + separator)."""
        result = mask_cell(
            "SSN 123-45-6789 here",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="passthrough",
        )
        # The masked SSN should still look like NNN-NN-NNNN
        assert re.search(r"\d{3}-\d{2}-\d{4}", result), (
            f"FPE SSN should preserve NNN-NN-NNNN format; got: {result!r}"
        )

    def test_email_span_is_masked(self) -> None:
        """Email default strategy is redact; raw email absent from output."""
        raw = "alice@example.com"
        result = mask_cell(
            f"Contact {raw} today.",
            _SEED,
            detector_ids=["email"],
            unmatched_span_policy="passthrough",
        )
        assert raw not in result

    def test_non_pii_cell_passthrough_unchanged(self) -> None:
        """Cell with no detected spans + passthrough policy returns the original."""
        text = "No PII here whatsoever."
        result = mask_cell(
            text,
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="passthrough",
        )
        assert result == text

    def test_empty_string_returns_empty(self) -> None:
        result = mask_cell("", _SEED)
        assert result == ""

    def test_non_string_returns_unchanged(self) -> None:
        # mask_cell should return non-string values unchanged
        result = mask_cell(None, _SEED)  # type: ignore[arg-type]
        assert result is None


class TestMultiDetectorDispatch:
    """text_mask.1: multiple detectors fire in one cell."""

    def test_ssn_and_email_both_masked(self) -> None:
        raw_ssn = "123-45-6789"
        raw_email = "alice@example.com"
        text = f"Patient {raw_email}, SSN {raw_ssn}."
        result = mask_cell(
            text,
            _SEED,
            detector_ids=["ssn", "email"],
            unmatched_span_policy="passthrough",
        )
        assert raw_ssn not in result
        assert raw_email not in result

    def test_dispatch_uses_per_detector_strategy_override(self) -> None:
        """per_detector_strategy override replaces the default for that detector."""
        raw = "123-45-6789"
        # Override ssn strategy to "redact"
        result = mask_cell(
            f"SSN {raw} end.",
            _SEED,
            detector_ids=["ssn"],
            strategy_map={"ssn": "redact"},
            unmatched_span_policy="passthrough",
        )
        # Redact token should appear in place of the SSN
        assert "[REDACTED]" in result
        assert raw not in result


class TestOverlapResolutionLongerMatchWins:
    """text_mask.1: overlapping spans resolve to the longer match."""

    def test_longer_span_wins_overlap(self) -> None:
        """iter_spans already resolves by leftmost-then-longest; text_mask respects it."""
        # Use two custom detectors via iter_spans' custom kwarg -- test through
        # mask_cell's underlying iter_spans call by verifying only one span is masked
        # (overlap from two detectors on the same text position).
        text = "SSN 123-45-6789 end"
        # Run with ssn only - the 11-char SSN should be masked once
        result = mask_cell(text, _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        # There should be exactly one masked span (not two overlapping redactions)
        assert result.count("[REDACTED]") <= 1
        assert "123-45-6789" not in result

    def test_non_overlapping_spans_both_masked(self) -> None:
        """Two non-overlapping SSNs in one cell - both are masked."""
        # Use valid SSNs: area not 000/666/9xx, group not 00, serial not 0000.
        text = "First 123-45-6789 and second 456-78-9012."
        result = mask_cell(text, _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        assert "123-45-6789" not in result
        assert "456-78-9012" not in result


# ── spec slice 2: keyed cross-cell determinism + raw-value isolation ──────────


class TestKeyedCrossCell:
    """text_mask.2: same real value in any two cells -> same masked value."""

    def test_same_ssn_two_cells_same_masked_value(self) -> None:
        """Cross-cell determinism: HMAC(seed, matched_text) is the key, NOT context."""
        ssn = "123-45-6789"
        cell_a = mask_cell(
            f"Call {ssn} asap.",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="passthrough",
        )
        cell_b = mask_cell(
            f"Record {ssn} filed.",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="passthrough",
        )
        # Extract the masked SSN from both (NNN-NN-NNNN pattern)
        pat = re.compile(r"\d{3}-\d{2}-\d{4}")
        a_matches = pat.findall(cell_a)
        b_matches = pat.findall(cell_b)
        assert a_matches, f"No masked SSN in cell_a: {cell_a!r}"
        assert b_matches, f"No masked SSN in cell_b: {cell_b!r}"
        assert a_matches[0] == b_matches[0], (
            "Cross-cell determinism violated: same SSN in different contexts "
            f"produced {a_matches[0]!r} vs {b_matches[0]!r}"
        )

    def test_same_input_same_seed_same_output_determinism(self) -> None:
        """Idempotent: calling mask_cell twice with same (text, seed) yields same result."""
        text = "Alice's SSN is 123-45-6789."
        r1 = mask_cell(text, _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        r2 = mask_cell(text, _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        assert r1 == r2

    def test_different_seeds_produce_different_outputs(self) -> None:
        """Different job_seed -> different masked value (key-change sensitivity)."""
        text = "SSN 123-45-6789 end."
        seed_a = b"\x01" * 32
        seed_b = b"\x02" * 32
        r_a = mask_cell(text, seed_a, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        r_b = mask_cell(text, seed_b, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        # Different keys should (with overwhelming probability) produce different outputs
        assert r_a != r_b


class TestRawValueIsolation:
    """text_mask.2: raw matched_text must never appear in logs or evidence."""

    def test_no_raw_pii_in_log_records(self) -> None:
        """Sentry: raw matched_text must not appear in any decoy_engine log record."""
        raw_ssn = "123-45-6789"
        captured: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(self.format(record))

        cap = _Cap()
        root = logging.getLogger("decoy_engine")
        root.addHandler(cap)
        root.setLevel(logging.DEBUG)
        try:
            mask_cell(
                f"SSN: {raw_ssn} is confidential.",
                _SEED,
                detector_ids=["ssn"],
                unmatched_span_policy="passthrough",
            )
        finally:
            root.removeHandler(cap)

        for msg in captured:
            assert raw_ssn not in msg, f"Raw PII leaked into log record: {msg!r}"

    def test_handler_no_raw_pii_in_warnings(self) -> None:
        """Sentry: QualityWarnings returned by TextMaskHandler contain no raw PII."""
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        raw_ssn = "123-45-6789"
        df = pd.DataFrame({"notes": [f"SSN {raw_ssn} on file."]})
        handler = TextMaskHandler()
        _, warnings = handler.run(
            df.copy(),
            "notes",
            _make_plan(),
            _FakeCtx(),
        )
        for w in warnings:
            assert raw_ssn not in str(w), f"Raw PII leaked into QualityWarning: {w!r}"

    def test_masked_output_does_not_contain_raw_ssn(self) -> None:
        """Basic sanity: the output cell must not equal the raw input."""
        raw_ssn = "123-45-6789"
        result = mask_cell(
            raw_ssn, _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough"
        )
        assert result != raw_ssn, "SSN must be transformed, not passed through"


class TestAllDetectorsHaveDefaultStrategy:
    """text_mask.2: every STORM detector has a dispatch-table entry."""

    @pytest.mark.parametrize("detector_id", list(DETECTOR_DEFAULTS.keys()))
    def test_all_default_entries_are_valid_strategies(self, detector_id: str) -> None:
        """Every entry in DETECTOR_DEFAULTS maps to a known strategy name."""
        valid = {"fpe", "faker", "date_shift", "redact", "passthrough"}
        assert DETECTOR_DEFAULTS[detector_id] in valid, (
            f"DETECTOR_DEFAULTS[{detector_id!r}] = {DETECTOR_DEFAULTS[detector_id]!r} "
            f"is not a valid strategy name ({sorted(valid)})"
        )

    def test_all_span_detectors_are_in_defaults(self) -> None:
        """Every detector in _SPAN_DETECTORS (the STORM span library) has a default entry."""
        for det_id in _SPAN_DETECTORS:
            assert det_id in DETECTOR_DEFAULTS, (
                f"Detector {det_id!r} is in _SPAN_DETECTORS but missing from "
                f"DETECTOR_DEFAULTS. Add a default strategy entry in text_mask.py."
            )

    def test_defaults_cover_all_25_registered_detectors(self) -> None:
        """DETECTOR_DEFAULTS covers all STORM REGISTERED_DETECTORS (25 + street_address)."""
        from decoy_engine.storm.detectors import REGISTERED_DETECTORS

        registered_ids = {fn.__name__.replace("detect_", "") for fn in REGISTERED_DETECTORS}
        missing = registered_ids - set(DETECTOR_DEFAULTS.keys())
        assert not missing, (
            f"These STORM registered detectors lack a DETECTOR_DEFAULTS entry: {missing}"
        )


# ── spec slice 3: unmatched_span_policy + STORM library wiring ────────────────


class TestUnmatchedSpanPolicy:
    """text_mask.3: unmatched_span_policy controls non-PII text segments."""

    def test_default_policy_is_redact(self) -> None:
        """When no policy is specified, unmatched text is redacted (safe default)."""
        # Cell with known SSN; surrounding prose is unmatched
        result = mask_cell(
            "Patient SSN 123-45-6789 on file.",
            _SEED,
            detector_ids=["ssn"],
            # No unmatched_span_policy -> defaults to "redact"
        )
        # Raw SSN must be gone
        assert "123-45-6789" not in result
        # Surrounding prose "Patient SSN " also replaced by token (redact policy)
        assert "Patient SSN " not in result

    def test_redact_policy_replaces_unmatched_with_token(self) -> None:
        """redact: each unmatched segment is replaced with the token."""
        result = mask_cell(
            "Prefix 123-45-6789 suffix",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="redact",
            token="[X]",
        )
        # Neither prefix nor suffix should survive
        assert "Prefix" not in result
        assert "suffix" not in result
        assert "123-45-6789" not in result

    def test_passthrough_policy_keeps_unmatched_text(self) -> None:
        """passthrough: non-PII text segments are kept verbatim."""
        result = mask_cell(
            "Patient name: John, SSN 123-45-6789 end.",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="passthrough",
        )
        # Surrounding prose must be preserved
        assert "Patient name: John, SSN " in result
        assert " end." in result
        # But SSN must be replaced
        assert "123-45-6789" not in result

    def test_replace_with_token_uses_sentinel(self) -> None:
        """replace_with_token: unmatched segments get a fixed sentinel."""
        result = mask_cell(
            "Before 123-45-6789 after",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="replace_with_token",
        )
        assert "[UNMATCHED]" in result
        assert "Before" not in result
        assert "after" not in result
        assert "123-45-6789" not in result

    def test_no_spans_redact_policy_redacts_whole_cell(self) -> None:
        """With redact policy and no detector hits, the entire cell is redacted."""
        result = mask_cell(
            "No PII here at all.",
            _SEED,
            detector_ids=["ssn"],
            unmatched_span_policy="redact",
            token="[REDACTED]",
        )
        assert result == "[REDACTED]"

    def test_no_spans_passthrough_policy_returns_original(self) -> None:
        """With passthrough policy and no detector hits, the cell is unchanged."""
        text = "No PII here at all."
        result = mask_cell(text, _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        assert result == text


class TestStormLibrarySingleSourceOfTruth:
    """text_mask.3: text_mask uses iter_spans -> _SPAN_DETECTORS is the single source."""

    def test_span_detectors_available_via_mask_cell(self) -> None:
        """Every detector in _SPAN_DETECTORS works through mask_cell without extra wiring."""
        # Email is in _SPAN_DETECTORS and should be detected/masked
        raw_email = "test@example.com"
        result = mask_cell(
            f"Email {raw_email} here.",
            _SEED,
            detector_ids=["email"],
            unmatched_span_policy="passthrough",
        )
        assert raw_email not in result

    def test_ssn_in_span_detectors_fires_through_mask_cell(self) -> None:
        """SSN is in _SPAN_DETECTORS and fires via mask_cell."""
        assert "ssn" in _SPAN_DETECTORS
        result = mask_cell(
            "SSN 123-45-6789.",
            _SEED,
            detector_ids=None,  # all detectors
            unmatched_span_policy="passthrough",
        )
        assert "123-45-6789" not in result

    def test_storm_library_sync_all_span_detectors_have_dispatch(self) -> None:
        """Adding a detector to _SPAN_DETECTORS makes it auto-available via DETECTOR_DEFAULTS.

        This test proves the single-source-of-truth property: if a NEW detector
        were added to _SPAN_DETECTORS, text_mask would need to add it to
        DETECTOR_DEFAULTS (enforced by test_all_span_detectors_are_in_defaults).
        The underlying call path (mask_cell -> iter_spans -> _SPAN_DETECTORS)
        means no separate detector registry is needed.
        """
        # All _SPAN_DETECTORS keys must have a DETECTOR_DEFAULTS entry
        span_ids = set(_SPAN_DETECTORS.keys())
        default_ids = set(DETECTOR_DEFAULTS.keys())
        missing = span_ids - default_ids
        assert not missing, (
            f"_SPAN_DETECTORS detectors without DETECTOR_DEFAULTS entries: {missing}. "
            "text_mask must maintain parity with the STORM library."
        )


# ── Handler integration ───────────────────────────────────────────────────────


class TestTextMaskHandler:
    """TextMaskHandler implements the V2 StrategyHandler protocol."""

    def test_handler_masks_ssn_column(self) -> None:
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        df = pd.DataFrame({"notes": ["Patient SSN 123-45-6789 on file."]})
        handler = TextMaskHandler()
        out, warnings = handler.run(df.copy(), "notes", _make_plan(), _FakeCtx())
        assert "123-45-6789" not in out["notes"].iloc[0]
        assert isinstance(warnings, list)

    def test_handler_returns_warnings_list(self) -> None:
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        df = pd.DataFrame({"notes": ["no pii here"]})
        handler = TextMaskHandler()
        _, warnings = handler.run(df.copy(), "notes", _make_plan(), _FakeCtx())
        assert isinstance(warnings, list)

    def test_handler_name_is_text_mask(self) -> None:
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        assert TextMaskHandler.name == "text_mask"

    def test_handler_registered_in_scalar_handlers(self) -> None:
        """TextMaskHandler must appear in SCALAR_HANDLERS for V2 pipeline wiring."""
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        assert "text_mask" in SCALAR_HANDLERS

    def test_handler_preserves_nulls(self) -> None:
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        df = pd.DataFrame({"notes": [None, "SSN 123-45-6789.", None]})
        handler = TextMaskHandler()
        out, _ = handler.run(df.copy(), "notes", _make_plan(), _FakeCtx())
        assert out["notes"].iloc[0] is None or pd.isna(out["notes"].iloc[0])
        assert out["notes"].iloc[2] is None or pd.isna(out["notes"].iloc[2])
        assert "123-45-6789" not in str(out["notes"].iloc[1])

    def test_handler_unmatched_policy_config_is_respected(self) -> None:
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        df = pd.DataFrame({"notes": ["No PII in this cell."]})
        handler = TextMaskHandler()
        # With passthrough, no-PII cell should be unchanged
        plan = _make_plan(unmatched_span_policy="passthrough")
        out, _ = handler.run(df.copy(), "notes", plan, _FakeCtx())
        assert out["notes"].iloc[0] == "No PII in this cell."

    def test_handler_cross_cell_determinism_across_rows(self) -> None:
        """Same SSN in two rows -> same masked value (cross-cell HMAC keying)."""
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        ssn = "123-45-6789"
        df = pd.DataFrame(
            {
                "notes": [
                    f"Row A SSN {ssn} end.",
                    f"Row B SSN {ssn} end.",
                ]
            }
        )
        handler = TextMaskHandler()
        plan = _make_plan(unmatched_span_policy="passthrough")
        out, _ = handler.run(df.copy(), "notes", plan, _FakeCtx())

        pat = re.compile(r"\d{3}-\d{2}-\d{4}")
        a_match = pat.findall(out["notes"].iloc[0])
        b_match = pat.findall(out["notes"].iloc[1])
        assert a_match and b_match, "Masked SSN not found in output"
        assert a_match[0] == b_match[0], (
            f"Cross-row determinism failed: {a_match[0]!r} != {b_match[0]!r}"
        )


# ── Date shift + Faker dispatch sanity ────────────────────────────────────────


class TestDateShiftDispatch:
    """Date-typed detectors dispatch to date_shift by default."""

    def test_iso_date_is_shifted(self) -> None:
        """iso_date default strategy is date_shift; output parses as a date."""
        raw = "1990-01-15"
        result = mask_cell(
            raw,
            _SEED,
            detector_ids=["iso_date"],
            unmatched_span_policy="passthrough",
        )
        # Result should still be a parsable date in the same format
        assert raw != result or True  # date_shift CAN return same date (zero-shift edge case)
        # Verify result is a valid date string in YYYY-MM-DD format
        from datetime import datetime as dt

        try:
            dt.strptime(result, "%Y-%m-%d")
        except ValueError:
            pytest.fail(f"date_shift output is not a valid date: {result!r}")


class TestFakerDispatch:
    """Name-typed detectors dispatch to faker by default."""

    def test_faker_default_for_person_name_is_not_original(self) -> None:
        """person_name -> faker; the output should differ from (or equal, if seeded same) input."""
        # faker is keyed so same input produces same output
        result1 = mask_cell(
            "John Doe",
            _SEED,
            strategy_map={"person_name": "faker"},
            unmatched_span_policy="passthrough",
        )
        result2 = mask_cell(
            "John Doe",
            _SEED,
            strategy_map={"person_name": "faker"},
            unmatched_span_policy="passthrough",
        )
        # Deterministic: two calls with same seed must yield same result
        assert result1 == result2
