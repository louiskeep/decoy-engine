"""MG-2 Step 5 (2026-05-31): text_redact security regression cells.

Defends against:
- Regex metacharacters in `token` being interpreted (must be literal).
- Adversarially long inputs (1k matches per cell) completing in bounded
  wall-clock time.
- The hot path short-circuiting when no detectors fire so a 100k-row
  prose column with no PII does not pay regex-scan cost per cell.
"""

from __future__ import annotations

import time

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


class TestTokenLiteralness:
    def test_text_redact_token_with_regex_metacharacters_treated_as_literal(self):
        # Tokens that look like regex patterns must appear verbatim in
        # the output; no expansion, no replacement.
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(
            df.copy(),
            "notes",
            _seed({"token": r"^.*$"}),
            _FakeCtx(),
        )
        assert out["notes"].iloc[0] == r"^.*$"

    def test_text_redact_token_with_backslashes_passes_through_literal(self):
        df = pd.DataFrame({"notes": ["alice@example.com"]})
        handler = TextRedactHandler()
        out, _ = handler.run(
            df.copy(),
            "notes",
            _seed({"token": r"\1\2\3"}),
            _FakeCtx(),
        )
        # No backreference expansion.
        assert out["notes"].iloc[0] == r"\1\2\3"


class TestPerformanceCeiling:
    def test_text_redact_long_input_with_many_matches_completes_under_5s(self):
        # 500 email spans in one cell. Bounded by iter_spans + splice.
        text = " ".join(f"user{i}@example.com noise" for i in range(500))
        df = pd.DataFrame({"notes": [text]})
        handler = TextRedactHandler()
        t0 = time.perf_counter()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0, f"text_redact took {elapsed:.2f}s for 500 matches"
        # Sanity: every email is redacted.
        assert "user0@example.com" not in out["notes"].iloc[0]
        assert out["notes"].iloc[0].count("[REDACTED]") == 500

    def test_text_redact_no_matches_no_validator_short_circuit(self):
        # Prose with no PII across many rows. Should complete fast.
        rows = ["just clinical prose; no identifiers in this row"] * 5000
        df = pd.DataFrame({"notes": rows})
        handler = TextRedactHandler()
        t0 = time.perf_counter()
        out, _ = handler.run(df.copy(), "notes", _seed({}), _FakeCtx())
        elapsed = time.perf_counter() - t0
        assert elapsed < 10.0, f"text_redact took {elapsed:.2f}s on 5k no-PII rows"
        assert out["notes"].tolist() == rows
