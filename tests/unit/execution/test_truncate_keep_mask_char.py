"""MG-1 S3 (2026-06-01): truncate strategy extension regression cells.

The V1 byte-identical path is preserved when both new keys are unset.
The new `keep` + `mask_char` shape unlocks the canonical "keep last 4,
mask the rest with *" pattern (cc_last4 / SSN-last-4 / phone-last-4
use cases).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._strategies._truncate import TruncateHandler
from decoy_engine.plan._types import ColumnSeed


def _seed(provider_config: dict) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="truncate",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
    )


class _FakeCtx:
    pass


class TestTruncateV1ByteIdentity:
    """The V1 path (length + from_end only) MUST be byte-identical
    after the MG-1 S3 extension."""

    def test_length_only_keeps_head(self):
        df = pd.DataFrame({"col": ["hello", "world", "foo"]})
        handler = TruncateHandler()
        out, _ = handler.run(df.copy(), "col", _seed({"length": 3}), _FakeCtx())
        assert out["col"].tolist() == ["hel", "wor", "foo"]

    def test_from_end_true_keeps_tail(self):
        df = pd.DataFrame({"col": ["hello", "world", "ab"]})
        handler = TruncateHandler()
        out, _ = handler.run(df.copy(), "col", _seed({"length": 2, "from_end": True}), _FakeCtx())
        assert out["col"].tolist() == ["lo", "ld", "ab"]

    def test_invalid_length_passes_through(self):
        df = pd.DataFrame({"col": ["hello", "world"]})
        handler = TruncateHandler()
        out, _ = handler.run(df.copy(), "col", _seed({"length": 0}), _FakeCtx())
        assert out["col"].tolist() == ["hello", "world"]


class TestTruncateNewKeepShape:
    def test_keep_head_matches_default(self):
        df = pd.DataFrame({"col": ["hello"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 3, "keep": "head"}),
            _FakeCtx(),
        )
        assert out["col"].tolist() == ["hel"]

    def test_keep_tail_matches_from_end(self):
        df = pd.DataFrame({"col": ["hello"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 3, "keep": "tail"}),
            _FakeCtx(),
        )
        assert out["col"].tolist() == ["llo"]

    def test_explicit_keep_wins_over_legacy_from_end(self):
        """When both keep and from_end are set, keep wins."""
        df = pd.DataFrame({"col": ["hello"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 3, "keep": "head", "from_end": True}),
            _FakeCtx(),
        )
        assert out["col"].tolist() == ["hel"]

    def test_unknown_keep_value_passes_through(self):
        df = pd.DataFrame({"col": ["hello"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 3, "keep": "middle"}),
            _FakeCtx(),
        )
        assert out["col"].tolist() == ["hello"]


class TestTruncateMaskChar:
    """The V1 'keep last 4, replace rest with *' use case."""

    def test_mask_char_with_keep_tail(self):
        df = pd.DataFrame({"col": ["1234567890", "abc"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 4, "keep": "tail", "mask_char": "*"}),
            _FakeCtx(),
        )
        # 1234567890 -> ******7890 (6 stars + last 4)
        # abc        -> abc        (length 3 < keep 4 -> the whole string survives)
        assert out["col"].tolist() == ["******7890", "abc"]

    def test_mask_char_with_keep_head(self):
        df = pd.DataFrame({"col": ["1234567890"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 4, "keep": "head", "mask_char": "*"}),
            _FakeCtx(),
        )
        # 1234567890 -> 1234****** (first 4 + 6 stars)
        assert out["col"].tolist() == ["1234******"]

    def test_mask_char_preserves_overall_length(self):
        """Output length matches input length when mask_char is set."""
        df = pd.DataFrame({"col": ["abcdefghij"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 2, "keep": "tail", "mask_char": "#"}),
            _FakeCtx(),
        )
        assert len(out["col"][0]) == 10

    def test_mask_char_rejects_multi_char(self):
        """A 2-char mask_char passes through with no mutation."""
        df = pd.DataFrame({"col": ["hello"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 2, "keep": "tail", "mask_char": "XY"}),
            _FakeCtx(),
        )
        assert out["col"].tolist() == ["hello"]

    def test_mask_char_rejects_non_string(self):
        df = pd.DataFrame({"col": ["hello"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 2, "keep": "tail", "mask_char": 42}),
            _FakeCtx(),
        )
        assert out["col"].tolist() == ["hello"]

    def test_mask_char_with_short_string(self):
        """When the string is shorter than keep-length, mask_char
        path leaves it unchanged (drop_part is empty)."""
        df = pd.DataFrame({"col": ["ab"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 4, "keep": "tail", "mask_char": "*"}),
            _FakeCtx(),
        )
        assert out["col"].tolist() == ["ab"]

    def test_nulls_preserved(self):
        df = pd.DataFrame({"col": ["1234567890", None, "abc"]})
        handler = TruncateHandler()
        out, _ = handler.run(
            df.copy(),
            "col",
            _seed({"length": 4, "keep": "tail", "mask_char": "*"}),
            _FakeCtx(),
        )
        assert out["col"][0] == "******7890"
        assert pd.isna(out["col"][1])
        assert out["col"][2] == "abc"
