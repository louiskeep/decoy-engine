"""MG-2 Step 2 (2026-05-31): text_redact strategy regression cells.

Locks the cell-level behavior of `TextRedactHandler`:
- Default token replaces every detected span.
- Non-PII text is preserved byte-for-byte around redacted spans.
- `label_token=True` emits per-detector labels.
- Custom token strings work, including metacharacters (literal).
- Null cells pass through.
- Subset detector_ids only redacts the listed detectors.
- Non-string token + non-list detectors fall back to passthrough.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._strategies._text_redact import TextRedactHandler
from decoy_engine.plan._types import ColumnSeed


def _seed(provider_config: dict) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
    )


class _FakeCtx:
    pass


# ── core redaction ────────────────────────────────────────────────────


class TestCoreRedaction:
    def test_text_redact_replaces_all_spans_with_default_token(self):
        df = pd.DataFrame({"notes": ["Contact alice@example.com, SSN 123-45-6789."]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        cell = out["notes"].iloc[0]
        assert "alice@example.com" not in cell
        assert "123-45-6789" not in cell
        assert cell.count("[REDACTED]") == 2

    def test_text_redact_preserves_non_pii_text_byte_for_byte(self):
        original = "Patient presented with cough. Phone (212) 555-1234. Discharged."
        df = pd.DataFrame({"notes": [original]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        cell = out["notes"].iloc[0]
        # Non-PII chunks preserved verbatim.
        assert cell.startswith("Patient presented with cough. Phone ")
        assert cell.endswith(". Discharged.")
        # The phone span is replaced.
        assert "(212) 555-1234" not in cell

    def test_text_redact_label_token_emits_per_detector_label(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"label_token": True}), _FakeCtx())
        assert out["notes"].iloc[0] == "[REDACTED:email]"

    def test_text_redact_custom_token_string(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"token": "<PHI>"}), _FakeCtx())
        assert out["notes"].iloc[0] == "<PHI>"


# ── null + empty + no-match ───────────────────────────────────────────


class TestNullAndEmpty:
    def test_text_redact_null_cell_stays_null(self):
        df = pd.DataFrame({"notes": ["alice@example.com", None, "no pii"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].iloc[0] == "[REDACTED]"
        assert pd.isna(out["notes"].iloc[1])
        assert out["notes"].iloc[2] == "no pii"

    def test_text_redact_empty_column_no_error(self):
        df = pd.DataFrame({"notes": pd.Series([], dtype=object)})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert len(out) == 0

    def test_text_redact_column_with_no_matches_passes_through_unchanged(self):
        df = pd.DataFrame({"notes": ["just prose", "no identifiers here"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].tolist() == ["just prose", "no identifiers here"]


# ── overlaps + detector selection ─────────────────────────────────────


class TestOverlapAndSelection:
    def test_text_redact_handles_overlapping_matches_per_iter_spans_policy(self):
        # `iter_spans` is responsible for de-overlapping; this test
        # exercises the strategy's `_splice` walking non-overlapping spans
        # (a regression cell that catches a future iter_spans bug from
        # producing overlapping output).
        df = pd.DataFrame({"notes": ["alice@example.com bob@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        assert out["notes"].iloc[0] == "[REDACTED] [REDACTED]"

    def test_text_redact_subset_detectors_only_redacts_listed(self):
        df = pd.DataFrame({"notes": ["Contact alice@example.com, SSN 123-45-6789."]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"detectors": ["email"]}), _FakeCtx())
        cell = out["notes"].iloc[0]
        assert "alice@example.com" not in cell
        # SSN was not in the list -> stays.
        assert "123-45-6789" in cell


# ── bad config ────────────────────────────────────────────────────────


class TestBadConfig:
    def test_text_redact_non_string_token_falls_back_to_passthrough(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(df.copy(), "notes", _seed({"token": 42}), _FakeCtx())
        # Bad config -> unchanged.
        assert out["notes"].iloc[0] == "alice@example.com"

    def test_text_redact_non_list_detectors_falls_back_to_passthrough(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(
            df.copy(),
            "notes",
            _seed({"detectors": "email"}),  # str, not list
            _FakeCtx(),
        )
        assert out["notes"].iloc[0] == "alice@example.com"

    def test_text_redact_token_with_regex_metacharacters_treated_as_literal(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(
            df.copy(),
            "notes",
            _seed({"token": r"<\d+>"}),
            _FakeCtx(),
        )
        # The token is emitted as-is; no regex interpretation.
        assert out["notes"].iloc[0] == r"<\d+>"
