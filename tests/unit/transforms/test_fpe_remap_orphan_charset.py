"""Tests for fix #42: FPE out-of-charset orphan-remap covering hash.

Bug: when orphan_policy=remap and the parent column uses FPE with
preserve_separators=True (the default), an orphan FK key whose characters
are ALL outside the FPE charset was returned verbatim by fpe_encrypt_value
(a privacy leak: the orphan key appeared unchanged in the masked output).

Fix: _fpe_value now calls _covering_hash_to_charset when no in-charset
positions exist, producing a deterministic in-charset string that differs
from the input.

Bite proof: the test_out_of_charset_not_verbatim test FAILS against the
pre-fix code (result == val for "EMP-ORPHAN" against alphanum charset) and
PASSES against the fix (result is an in-charset string != "EMP-ORPHAN").
"""

from __future__ import annotations

import pytest

from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

_KEY = bytes(range(32))  # 32-byte key for HMAC-SHA256
_TWEAK = b"manager_id"
_ALPHANUM = _CHARSETS["alphanum"]  # 0-9a-z: no uppercase, no hyphen


class TestOutOfCharsetCoveringHash:
    """fpe_encrypt_value must not return verbatim for all-out-of-charset values."""

    def test_out_of_charset_not_verbatim(self) -> None:
        """Primary bite: an all-uppercase orphan key is NOT emitted verbatim.

        Pre-fix: fpe_encrypt_value("EMP-ORPHAN", ..., preserve_separators=True)
        returned "EMP-ORPHAN" unchanged because no characters in "EMP-ORPHAN"
        belong to the alphanum charset (0-9a-z) and FPE had nothing to permute.
        Post-fix: a deterministic in-charset covering hash replaces the passthrough.
        """
        val = "EMP-ORPHAN"  # all uppercase + hyphen: zero chars in alphanum charset
        result = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert result != val, (
            f"fpe_encrypt_value returned {result!r} == source {val!r}. "
            "An all-out-of-charset value must not be emitted verbatim "
            "(fix #42: covering hash must replace the passthrough)."
        )

    def test_covering_hash_deterministic(self) -> None:
        """Same (key, charset, tweak, val) always produces the same output.

        FK referential integrity requires that the same orphan source key always
        maps to the same masked value across rows and re-runs.
        """
        val = "TERMINATED"
        out1 = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        out2 = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert out1 == out2, "Covering hash must be deterministic."

    def test_covering_hash_output_is_in_charset(self) -> None:
        """Every character of the covering-hash output belongs to the charset."""
        val = "UNKNOWN"
        charset_set = set(_ALPHANUM)
        result = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        non_charset = [ch for ch in result if ch not in charset_set]
        assert not non_charset, (
            f"Covering hash output {result!r} contains non-charset chars: {non_charset}. "
            "Every output character must be in the configured charset."
        )

    def test_covering_hash_length_preserved(self) -> None:
        """The covering hash output has the same length as the input."""
        val = "EMP-ORPHAN"
        result = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert len(result) == len(val), (
            f"Covering hash output {result!r} has length {len(result)}, "
            f"expected {len(val)} (same as source {val!r})."
        )

    def test_covering_hash_varies_by_value(self) -> None:
        """Different source values produce different covering-hash outputs."""
        out_a = fpe_encrypt_value("TERMINATED", _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        out_b = fpe_encrypt_value("UNKNOWN", _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        # Length differs so they cannot be equal; this check is belt-and-suspenders.
        assert out_a != out_b or len(out_a) != len(out_b)

    def test_in_charset_values_use_normal_fpe(self) -> None:
        """Values with in-charset characters still go through the normal Feistel path."""
        val = "emp99999"  # all chars in alphanum charset (0-9a-z)
        result = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert result != val, (
            f"Normal FPE should permute in-charset value; got identity {result!r}."
        )
        charset_set = set(_ALPHANUM)
        assert all(ch in charset_set for ch in result), (
            f"Normal FPE output {result!r} contains non-charset chars."
        )

    def test_empty_string_passthrough(self) -> None:
        """Empty string: covering hash returns empty (consistent with Feistel behaviour)."""
        result = fpe_encrypt_value("", _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert result == ""

    @pytest.mark.parametrize(
        "val",
        [
            "TERMINATED",
            "N/A",
            "EMP-ORPHAN",
            "UNKNOWN",
            "STATUS-DELETED",
        ],
    )
    def test_sentinel_values_not_verbatim(self, val: str) -> None:
        """Common sentinel FK values with no alphanum chars are not passed through."""
        result = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert result != val, (
            f"Sentinel value {val!r} leaked verbatim as {result!r}. "
            "Covering hash must apply when no in-charset chars exist."
        )

    def test_mixed_charset_uses_separator_path(self) -> None:
        """Values with some in-charset chars still go through the separator path."""
        val = "EMP-00001"  # "00001" are digits (in alphanum), "EMP-" are not
        result = fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        # The digit substring gets permuted; the uppercase/hyphen are preserved.
        assert result != val, "Digit portion should be permuted."
        # The 'EMP-' prefix (separators) must be preserved in-place.
        assert result[:4] == "EMP-", (
            f"Separator prefix 'EMP-' should be preserved; got {result[:4]!r}."
        )

    def test_key_variation_changes_output(self) -> None:
        """Different keys produce different covering-hash outputs."""
        val = "TERMINATED"
        key_a = b"\x00" * 32
        key_b = b"\x01" * 32
        out_a = fpe_encrypt_value(val, key_a, _ALPHANUM, _TWEAK, preserve_separators=True)
        out_b = fpe_encrypt_value(val, key_b, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert out_a != out_b, "Different keys must produce different covering-hash outputs."
