"""Sprint 2 honesty pack (2026-07-04, S6, GATE-1 Q4): fail-closed compile
check for fpe's degenerate-charset whole-column passthrough.

Discovery 0.1 (guide section 0.1, DISCOVERY 2): `_fpe.py:70` returns
`df, []` (silent whole-column passthrough) when the resolved charset has
fewer than 2 distinct characters. This is the same fail-open shape #13
closed for truncate/bucketize/categorical. This module mirrors
`plan/_checks_bucketize.py`'s pattern.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.plan._checks_fpe import check_fpe_charset_config
from decoy_engine.plan._errors import PlanCompileError


def _config(provider_config: dict[str, Any], *, column: str = "c") -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": column,
                        "strategy": "fpe",
                        "provider": "p",
                        "provider_config": provider_config,
                    }
                ],
            }
        ]
    }


class TestCheckFpeCharsetConfig:
    def test_single_character_literal_charset_rejected(self) -> None:
        cfg = _config({"charset": "1"})
        with pytest.raises(PlanCompileError) as exc:
            check_fpe_charset_config(cfg)
        assert exc.value.code == "fpe_charset_degenerate"
        assert "t.c" in exc.value.path

    def test_repeated_character_charset_rejected(self) -> None:
        """A literal charset of repeated chars dedupes to <2 distinct chars."""
        cfg = _config({"charset": "aaaa"})
        with pytest.raises(PlanCompileError) as exc:
            check_fpe_charset_config(cfg)
        assert exc.value.code == "fpe_charset_degenerate"

    def test_empty_charset_rejected(self) -> None:
        cfg = _config({"charset": ""})
        with pytest.raises(PlanCompileError) as exc:
            check_fpe_charset_config(cfg)
        assert exc.value.code == "fpe_charset_degenerate"

    def test_named_digits_charset_accepted(self) -> None:
        check_fpe_charset_config(_config({"charset": "digits"}))  # no raise

    def test_default_charset_accepted(self) -> None:
        check_fpe_charset_config(_config({}))  # defaults to "digits"

    def test_two_distinct_literal_chars_accepted(self) -> None:
        check_fpe_charset_config(_config({"charset": "01"}))  # no raise

    def test_non_fpe_column_ignored(self) -> None:
        cfg = _config({"charset": "1"})
        cfg["tables"][0]["columns"][0]["strategy"] = "hash"
        check_fpe_charset_config(cfg)  # no raise: not an fpe column
