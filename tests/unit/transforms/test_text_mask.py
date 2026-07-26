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
from decoy_engine.storm.detectors import _SPAN_DETECTORS, Span
from decoy_engine.transforms.text_mask import (
    _DEFAULT_TOKEN,
    DETECTOR_DEFAULTS,
    _detect_date_format,
    _mask_date_shift,
    _mask_faker,
    _mask_fpe,
    _mask_span,
    _span_key,
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
        # DE-02: TextMaskHandler keys off ctx.mask_key; no-secret path == job_seed.
        self.mask_key = job_seed


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


class TestOverlapResolution:
    """text_mask.1: overlapping spans resolve by leftmost-then-longest policy.

    Policy: spans are sorted by (start, -length); the greedy sweep keeps the first
    non-conflicting match.  Primary key is start position; length breaks ties.
    Earlier spec text said 'longer-match-wins' which is imprecise for the general
    case -- 'leftmost-then-longest' is the actual implementation.
    """

    def test_genuine_overlap_leftmost_longest_wins(self) -> None:
        """Genuine two-detector overlap via extra_spans: longer span at same start wins."""
        # Inject two overlapping spans at position 0:
        #   span_long: ssn (length 11) -- should win (longer when starts tie)
        #   span_short: us_zip (length 3) -- should be dropped (same start, shorter)
        text = "123-45-6789"
        span_long = Span("ssn", 0, 11, "123-45-6789")
        span_short = Span("us_zip", 0, 3, "123")
        # Pass shorter span first to prove ordering is not input-dependent
        result = mask_cell(
            text,
            _SEED,
            extra_spans=[span_short, span_long],
            unmatched_span_policy="passthrough",
        )
        # ssn wins: output must be FPE-encrypted SSN (NNN-NN-NNNN shape)
        assert re.search(r"\d{3}-\d{2}-\d{4}", result), (
            f"Expected FPE-masked SSN in result; got {result!r}"
        )
        # Raw values must not appear
        assert "123-45-6789" not in result, f"Raw SSN leaked: {result!r}"
        assert "123" not in result, f"Raw us_zip content leaked: {result!r}"

    def test_overlap_no_raw_remnant_under_passthrough(self) -> None:
        """No raw PII remnant when two spans overlap and passthrough is active.

        Verifies the M1 invariant: dropped spans' content is fully covered by the
        winning span's masking; passthrough only applies to truly undetected segments.
        """
        # Two overlapping spans: span_a covers 0-15, span_b covers 8-20 (overlap 8-15)
        # Leftmost wins (span_a), span_b is dropped.
        # span_b's leading content (8-15) is covered by span_a's masking.
        # span_b's trailing content (15-20) falls in unmatched territory.
        text = "1234567890abcde12345"  # 20 chars
        raw_a = text[0:15]  # "1234567890abcde"
        raw_b_tail = text[15:20]  # "12345" (unmatched, rides through under passthrough)
        span_a = Span("ssn", 0, 15, raw_a)  # leftmost, wins
        span_b = Span("email", 8, 20, text[8:20])  # overlaps with span_a, dropped
        result = mask_cell(
            text,
            _SEED,
            extra_spans=[span_a, span_b],
            unmatched_span_policy="passthrough",
        )
        # span_a's matched content must be masked (FPE for SSN, but here ssn= strategy)
        assert raw_a not in result, f"Winning span's raw text leaked: {result!r}"
        # span_b was dropped; its leading chars (8-15) are inside span_a's masked region
        # span_b's trailing chars (15-20) pass through under passthrough (not PII in test)
        assert raw_b_tail in result, (
            f"Unmatched trailing segment should pass through under passthrough; result: {result!r}"
        )
        # The full original text must not survive unmodified
        assert result != text, "Cell must not be returned unmodified when spans were detected"

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
        """Sentry: raw matched_text must not appear in any decoy_engine log record.

        Non-vacuous: passthrough policy causes mask_cell to emit a warning log,
        so captured is guaranteed non-empty after the call.
        """
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
                unmatched_span_policy="passthrough",  # triggers the passthrough warning log
            )
        finally:
            root.removeHandler(cap)

        # Sentry must be non-vacuous: passthrough policy must emit at least one log record.
        assert captured, (
            "Expected at least one log record from mask_cell; passthrough policy "
            "should emit a warning. If this fires, the warning log was removed."
        )
        for msg in captured:
            assert raw_ssn not in msg, f"Raw PII leaked into log record: {msg!r}"

    def test_handler_no_raw_pii_in_warnings(self) -> None:
        """Sentry: QualityWarnings and log records from TextMaskHandler contain no raw PII.

        Non-vacuous: passthrough plan config causes mask_cell to emit a warning log,
        so captured log records are guaranteed non-empty.
        """
        from decoy_engine.execution._strategies._text_mask import TextMaskHandler

        raw_ssn = "123-45-6789"
        captured: list[str] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(self.format(record))

        cap = _Cap()
        root = logging.getLogger("decoy_engine")
        root.addHandler(cap)
        root.setLevel(logging.DEBUG)

        df = pd.DataFrame({"notes": [f"SSN {raw_ssn} on file."]})
        handler = TextMaskHandler()
        try:
            _, warnings = handler.run(
                df.copy(),
                "notes",
                _make_plan(unmatched_span_policy="passthrough"),  # triggers warning log
                _FakeCtx(),
            )
        finally:
            root.removeHandler(cap)

        # QualityWarnings must not contain raw PII
        for w in warnings:
            assert raw_ssn not in str(w), f"Raw PII leaked into QualityWarning: {w!r}"
        # Sentry must be non-vacuous: passthrough policy must have emitted a log warning.
        assert captured, (
            "Expected at least one log record from TextMaskHandler run; passthrough "
            "policy should emit a warning. If this fires, the warning log was removed."
        )
        for msg in captured:
            assert raw_ssn not in msg, f"Raw PII leaked into log record: {msg!r}"

    def test_raw_value_never_interpolated_in_log_strings(self) -> None:
        """Static sentry: text_mask.py must never format matched_text into any log call.

        Scans the module source for log calls that reference 'matched_text'. If a
        future edit adds such a call, this test fails and the change must be reverted.
        The invariant is: only strategy name and detector_id are safe to log.
        """
        import inspect

        from decoy_engine.transforms import text_mask as _tm

        source = inspect.getsource(_tm)
        # Find any log call that interpolates matched_text directly
        dangerous = re.findall(
            r"_log\.\w+\s*\([^)]*\bmatched_text\b",
            source,
        )
        assert not dangerous, (
            f"text_mask.py has a log call that references matched_text (raw-value isolation "
            f"violation). Remove the interpolation: {dangerous}"
        )

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


class TestLocationDefault:
    """TX-2: `location` (the NER GPE/LOC/FAC mapping, storm/ner.py's
    NER_ENTITY_MAP) had no DETECTOR_DEFAULTS entry, so an injected location
    span silently fell to the "redact" fallback at mask_cell's dispatch
    (`effective_map.get(span.detector_id, "redact")`) instead of synthesizing
    like the other Tier-2 NER detectors (person_name, address)."""

    def test_injected_location_span_uses_location_default_not_redact(self) -> None:
        text = "Seen in Chicago last week."
        span = Span(
            "location", text.index("Chicago"), text.index("Chicago") + len("Chicago"), "Chicago"
        )
        # passthrough isolates the assertion to the SPAN's own strategy dispatch
        # (unmatched_span_policy defaults to "redact" and would otherwise also
        # emit "[REDACTED]" for the surrounding prose, unrelated to this fix).
        out = mask_cell(text, _SEED, extra_spans=[span], unmatched_span_policy="passthrough")
        assert "Chicago" not in out
        assert "[REDACTED" not in out
        assert out.startswith("Seen in ") and out.endswith(" last week.")

    def test_location_default_is_faker(self) -> None:
        assert DETECTOR_DEFAULTS["location"] == "faker"


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
    """Date-typed detectors dispatch to date_shift by default.

    iso_date is a Tier-2 detector (not in _SPAN_DETECTORS); spans must be
    supplied via extra_spans= to exercise the date_shift strategy.
    """

    def test_iso_date_is_shifted(self) -> None:
        """iso_date -> date_shift via extra_spans injection; output is a shifted date.

        Uses cfg min_days=1/max_days=30 to guarantee shift > 0, avoiding the
        degenerate zero-shift edge case and making the assert unconditionally real.
        """
        raw = "1990-01-15"
        # Inject the iso_date span explicitly: iso_date is Tier-2 (NER/custom-only).
        result = mask_cell(
            raw,
            _SEED,
            extra_spans=[Span("iso_date", 0, len(raw), raw)],
            unmatched_span_policy="passthrough",
            cfg={"min_days": 1, "max_days": 30},  # shift in [1, 30]: guaranteed nonzero
        )
        # The date must have been shifted (shift > 0 guaranteed by cfg above)
        assert result != raw, (
            f"date_shift with min_days=1 must shift the date; got same value {result!r}"
        )
        # Output must still parse as YYYY-MM-DD (format preserved by date_shift)
        from datetime import datetime as dt

        try:
            dt.strptime(result, "%Y-%m-%d")
        except ValueError:
            pytest.fail(f"date_shift output is not a valid date: {result!r}")


class TestFakerDispatch:
    """Name-typed detectors dispatch to faker by default.

    person_name is a Tier-2 detector (not in _SPAN_DETECTORS); spans must be
    supplied via extra_spans= to exercise the faker strategy.
    """

    def test_faker_default_for_person_name_is_not_original(self) -> None:
        """person_name -> faker via extra_spans injection; output is a different synthetic name."""
        raw_name = "John Doe"
        # Inject the person_name span explicitly: person_name is Tier-2 (NER/custom-only).
        span = Span("person_name", 0, len(raw_name), raw_name)
        result1 = mask_cell(
            raw_name,
            _SEED,
            extra_spans=[span],
            unmatched_span_policy="passthrough",
        )
        result2 = mask_cell(
            raw_name,
            _SEED,
            extra_spans=[span],
            unmatched_span_policy="passthrough",
        )
        # Deterministic: same input + same seed -> same synthetic name
        assert result1 == result2, (
            f"Faker dispatch must be deterministic; got {result1!r} vs {result2!r}"
        )
        # The output must NOT be the original name (Faker generates a synthetic name)
        assert result1 != raw_name, (
            f"faker output should not equal the original name; got {result1!r}"
        )
        # Output must be a non-empty string
        assert isinstance(result1, str) and result1.strip(), (
            f"faker output must be a non-empty string; got {result1!r}"
        )


# ── TQ crown-jewels mutation-kill pass (2026-07-26) ───────────────────────────
# Oracle + known-answer tests that kill the LOGIC survivors from the mutmut run
# on transforms/text_mask.py. Crypto/FPE/faker/date-shift paths are pinned with
# hard-coded real outputs so seed, parameter, and dispatch-key mutants die.
# Docstring invariants (§ "Cross-cell keyed determinism", § "Overlap resolution",
# § "Unmatched-span interpretation") are the source for each assertion.
# Ledger: docs/quality/mutation-ledgers/transforms_text_mask.md


class TestSpanKeyHmac:
    """`_span_key` is HMAC-SHA256(mask_key, matched_text) (RFC 2104, module docstring).

    KATs pin the exact digest so any change to the key-derivation bytes (dropped
    message, changed encoding error-handler) changes the result and dies.
    """

    # Real HMAC-SHA256(b"\xab"*32, "123-45-6789".encode("utf-8", errors="replace")).
    _KAT_SSN = "6696204014bc01a16168a0e786c70620416f46ff72142d5f8ebdb6075db466fd"
    # Real digest for a lone surrogate: only encode(errors="replace") produces it;
    # a strict encoder (or a bogus handler name) raises instead of returning it.
    _KAT_SURROGATE = "a64bbf452f2b074be41b0fadd0023300ad666f298b71c5c6de78a7a960bfa2d4"

    def test_span_key_known_answer(self) -> None:
        assert _span_key(_SEED, "123-45-6789").hex() == self._KAT_SSN

    def test_span_key_hashes_the_message_not_empty(self) -> None:
        """Digest must depend on matched_text (kills msg=None -> HMAC-of-empty)."""
        empty = _span_key(_SEED, "").hex()
        assert _span_key(_SEED, "123-45-6789").hex() != empty

    def test_span_key_replace_encodes_lone_surrogate(self) -> None:
        """errors='replace' must survive an un-encodable char (raw-value isolation)."""
        assert _span_key(_SEED, "\ud800").hex() == self._KAT_SURROGATE

    def test_span_key_is_key_sensitive(self) -> None:
        assert _span_key(b"\x01" * 32, "x") != _span_key(b"\x02" * 32, "x")

    def test_span_key_is_text_sensitive(self) -> None:
        assert _span_key(_SEED, "a") != _span_key(_SEED, "b")


class TestMaskFpeDispatch:
    """`_mask_fpe` routes charset + checksum per detector via `_FPE_CONFIG`.

    KATs pin the FPE output so a wrong config key, wrong charset default, wrong
    checksum scheme, or a flipped validate_luhn changes the ciphertext and dies.
    """

    def test_pan_uses_luhn_checksum(self) -> None:
        """pan -> ("digits","luhn"); dropping/nulling the checksum changes output."""
        pan = "4111111111111111"
        assert _mask_fpe(pan, _span_key(_SEED, pan), "pan") == "4885959915148828"

    def test_ssn_has_no_checksum(self) -> None:
        """ssn -> ("digits",None); validate_luhn stays False (flipping it re-shapes)."""
        ssn = "123-45-6789"
        assert _mask_fpe(ssn, _span_key(_SEED, ssn), "ssn") == "904-52-1741"

    def test_unknown_detector_uses_digits_default(self) -> None:
        """A detector absent from _FPE_CONFIG falls back to ("digits", None) and
        still FPE-encrypts (kills default-key/charset mutants that break the fallback)."""
        val = "12345678"
        assert _mask_fpe(val, _span_key(_SEED, val), "unknown_det_xyz") == "67241497"

    def test_fpe_failure_returns_configured_token(self) -> None:
        """All-separator input fails closed to the caller's token, not _DEFAULT_TOKEN."""
        assert _mask_fpe("--", _span_key(_SEED, "--"), "ssn", "[X]") == "[X]"

    def test_fpe_failure_defaults_to_redacted_token(self) -> None:
        assert _mask_fpe("--", _span_key(_SEED, "--"), "ssn") == _DEFAULT_TOKEN


class TestMaskFakerDispatch:
    """`_mask_faker` seeds a Faker from span_key[:4] and routes by `_FAKER_METHOD`.

    KATs pin the synthetic value so a wrong method, wrong seed slice, or a
    swallowed method result changes the name and dies.
    """

    def test_first_name_method_routing(self) -> None:
        assert _mask_faker("John", _span_key(_SEED, "John"), "first_name") == "Joy"

    def test_person_name_method_routing(self) -> None:
        assert (
            _mask_faker("John Doe", _span_key(_SEED, "John Doe"), "person_name") == "Kenneth Thomas"
        )

    def test_location_maps_to_city(self) -> None:
        assert _mask_faker("Boston", _span_key(_SEED, "Boston"), "location") == "New Connieville"

    def test_unknown_detector_falls_back_to_name(self) -> None:
        """Unknown detector -> "name" default; returns a real name, never raises."""
        out = _mask_faker("x", _span_key(_SEED, "x"), "unknown_det")
        assert isinstance(out, str) and out.strip()

    def test_faker_seed_uses_first_four_bytes(self) -> None:
        """Same span_key -> same synthetic value (seed = span_key[:4])."""
        key = _span_key(_SEED, "John Doe")
        assert _mask_faker("a", key, "person_name") == _mask_faker("b", key, "person_name")


class TestDetectDateFormat:
    """`_detect_date_format` scans every entry in `_COMMON_FORMATS`, not just the first."""

    def test_matches_non_first_format(self) -> None:
        """A format that is NOT first in the list must still be found (kills break-on-fail)."""
        assert _detect_date_format("01/15/1990") == "%m/%d/%Y"

    def test_matches_first_format(self) -> None:
        assert _detect_date_format("1990-01-15") == "%Y-%m-%d"

    def test_unrecognised_returns_none(self) -> None:
        assert _detect_date_format("not a date") is None


class TestMaskDateShift:
    """`_mask_date_shift` shifts by min_days + (span_key[:8] % range_size) days.

    KAT parameters chosen so every arithmetic/boundary/seed mutant lands on a
    different date than the pinned answer.
    """

    def test_shift_known_answer(self) -> None:
        d = "2001-09-11"
        assert _mask_date_shift(d, _span_key(_SEED, d), -365, 365) == "2001-02-22"

    def test_unparseable_date_passes_through(self) -> None:
        assert _mask_date_shift("nope", _span_key(_SEED, "nope"), -365, 365) == "nope"

    def test_format_is_preserved(self) -> None:
        from datetime import datetime

        d = "01/15/1990"
        out = _mask_date_shift(d, _span_key(_SEED, d), 1, 30)
        datetime.strptime(out, "%m/%d/%Y")  # raises if the format was not preserved


class TestMaskSpanDispatch:
    """`_mask_span` dispatches one span to its strategy and wires the token/params.

    Pins the per-strategy output and the token/date-param plumbing so dispatch-key,
    dropped-argument, and default-value mutants die.
    """

    def test_fpe_strategy_ssn(self) -> None:
        sp = Span("ssn", 0, 11, "123-45-6789")
        assert _mask_span(sp, _SEED, "fpe", {"token": _DEFAULT_TOKEN}) == "904-52-1741"

    def test_fpe_strategy_pan_passes_detector_id(self) -> None:
        """detector_id must reach _mask_fpe (drives tweak + checksum): pan -> luhn."""
        sp = Span("pan", 0, 16, "4111111111111111")
        assert _mask_span(sp, _SEED, "fpe", {"token": _DEFAULT_TOKEN}) == "4885959915148828"

    def test_faker_strategy_uses_detector_id(self) -> None:
        sp = Span("first_name", 0, 4, "John")
        assert _mask_span(sp, _SEED, "faker", {}) == "Joy"

    def test_passthrough_strategy_returns_matched_text(self) -> None:
        sp = Span("ssn", 0, 11, "123-45-6789")
        assert _mask_span(sp, _SEED, "passthrough", {}) == "123-45-6789"

    def test_redact_strategy_uses_configured_token(self) -> None:
        sp = Span("ssn", 0, 11, "123-45-6789")
        assert _mask_span(sp, _SEED, "redact", {"token": "[X]"}) == "[X]"

    def test_redact_strategy_defaults_token(self) -> None:
        sp = Span("ssn", 0, 11, "123-45-6789")
        assert _mask_span(sp, _SEED, "redact", {}) == _DEFAULT_TOKEN

    def test_unknown_strategy_falls_back_to_redact(self) -> None:
        sp = Span("ssn", 0, 11, "123-45-6789")
        assert _mask_span(sp, _SEED, "no_such_strategy", {"token": "[X]"}) == "[X]"

    def test_fpe_failure_uses_span_token(self) -> None:
        """FPE fail-closed carries the cfg token through _mask_span's token wiring."""
        sp = Span("ssn", 0, 2, "--")
        assert _mask_span(sp, _SEED, "fpe", {"token": "[X]"}) == "[X]"

    def test_fpe_failure_defaults_token_when_absent(self) -> None:
        """cfg without a token -> _DEFAULT_TOKEN (kills token-default -> None mutants)."""
        sp = Span("ssn", 0, 2, "--")
        assert _mask_span(sp, _SEED, "fpe", {}) == _DEFAULT_TOKEN

    def test_date_shift_uses_cfg_bounds(self) -> None:
        sp = Span("iso_date", 0, 10, "1990-01-15")
        assert _mask_span(sp, _SEED, "date_shift", {"min_days": 1, "max_days": 30}) == "1990-01-25"

    def test_date_shift_defaults_when_bounds_absent(self) -> None:
        """No min/max in cfg -> defaults -365/365 (kills int(None) and shifted defaults)."""
        sp = Span("iso_date", 0, 10, "1990-01-15")
        assert _mask_span(sp, _SEED, "date_shift", {}) == "1989-01-30"


class TestMaskCellReassembly:
    """`mask_cell` reassembles matched spans + unmatched segments in order.

    Pins the exact reassembled string (kills the join-separator, iter_spans
    argument, token-plumbing, and non-string-guard mutants).
    """

    def test_multi_span_reassembly_known_answer(self) -> None:
        text = "Patient alice@example.com, SSN 123-45-6789."
        out = mask_cell(
            text, _SEED, detector_ids=["ssn", "email"], unmatched_span_policy="passthrough"
        )
        assert out == "Patient [REDACTED], SSN 904-52-1741."

    def test_no_junk_separator_between_parts(self) -> None:
        out = mask_cell(
            "SSN 123-45-6789 end", _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough"
        )
        assert "XXXX" not in out

    def test_truthy_non_string_returns_unchanged(self) -> None:
        """A non-empty non-str (int) returns unchanged (kills the `or`->`and` guard)."""
        assert mask_cell(123, _SEED) == 123

    def test_detector_ids_restrict_detection(self) -> None:
        """detector_ids must be forwarded to iter_spans; email is not masked here."""
        text = "SSN 123-45-6789 email alice@example.com"
        out = mask_cell(text, _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough")
        assert "alice@example.com" in out
        assert "123-45-6789" not in out

    def test_custom_token_reaches_span_masking(self) -> None:
        """token= must be threaded into extra_cfg and used by the redact strategy."""
        out = mask_cell(
            "SSN 123-45-6789 end.",
            _SEED,
            detector_ids=["ssn"],
            strategy_map={"ssn": "redact"},
            token="[X]",
            unmatched_span_policy="passthrough",
        )
        assert "[X]" in out
        assert "[REDACTED]" not in out

    def test_cross_cell_same_pii_same_mask(self) -> None:
        """Same SSN in two cells -> identical masked span (span key is context-free)."""
        a = mask_cell(
            "A 123-45-6789 z", _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough"
        )
        b = mask_cell(
            "B 123-45-6789 y", _SEED, detector_ids=["ssn"], unmatched_span_policy="passthrough"
        )
        assert "904-52-1741" in a
        assert "904-52-1741" in b
