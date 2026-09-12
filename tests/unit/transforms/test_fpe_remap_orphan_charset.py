"""DE-01 cluster-C (2026-07-14): all-out-of-charset FPE values fail closed.

This supersedes fix #42. Fix #42 replaced the verbatim passthrough of an
all-out-of-charset value with a deterministic in-charset COVERING HASH. That
covering hash is a one-way per-position PRF, so `fpe_decrypt_value` cannot
invert it: a column sold as reversible silently did not round-trip for those
values (verified `'---' -> '092' -> '858'`). DE-01 closes this at the source:
an all-out-of-charset value now RAISES `FpeUnencryptableError` (fail closed)
rather than emit a non-invertible masked value.

Fail-pre/pass-post: every test below asserted the covering-hash behavior before
the fix and PASSES only against the fail-closed source. The partial-prefix case
(some in-charset chars, an out-of-charset prefix) is intentionally NOT closed
here -- see `test_fpe_partial_prefix_preserved`.
"""

from __future__ import annotations

import pytest

from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

_KEY = bytes(range(32))  # 32-byte key for HMAC-SHA256
_TWEAK = b"manager_id"
_ALPHANUM = _CHARSETS["alphanum"]  # 0-9a-z: no uppercase, no hyphen


class TestAllOutOfCharsetFailsClosed:
    """An all-out-of-charset value has no in-charset content to encrypt.

    There is nothing to format-preserving-encrypt and the pre-fix covering hash
    was non-invertible, so the engine fails closed instead of emitting a value
    that silently will not reverse.
    """

    @pytest.mark.parametrize(
        "val",
        [
            "TERMINATED",  # uppercase -> out of the lowercase alphanum charset
            "N/A",
            "EMP-ORPHAN",
            "UNKNOWN",
            "STATUS-DELETED",
        ],
    )
    def test_all_out_of_charset_raises(self, val: str) -> None:
        """Every character is outside the charset -> FpeUnencryptableError.

        Pre-fix these returned a covering hash (non-round-trip). The
        exception carries the offending value's length (P7: never the
        value itself, which must not appear in the message either).
        """
        with pytest.raises(FpeUnencryptableError) as exc:
            fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert exc.value.value_length == len(val)
        assert "no character in the configured charset" in str(exc.value)
        assert val not in str(exc.value)

    def test_separators_only_value_raises(self) -> None:
        """A value made entirely of separators (no in-charset chars) fails closed.

        `'---'` under the digits charset was the verified non-round-trip repro
        (`'---' -> '092' -> '858'`).
        """
        with pytest.raises(FpeUnencryptableError):
            fpe_encrypt_value("---", _KEY, _CHARSETS["digits"], _TWEAK, preserve_separators=True)

    def test_in_charset_values_use_normal_fpe(self) -> None:
        """Values with in-charset characters still go through the normal FF1
        path. A fixed point on any one value is legal for a permutation, so
        the no-op regression guard is aggregate across several independent
        values rather than a universal per-value claim."""
        vals = ["emp99999", "usr12345", "acct00001"]  # all chars in alphanum charset (0-9a-z)
        results = [
            fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
            for val in vals
        ]
        assert any(result != val for result, val in zip(results, vals, strict=True)), (
            f"Normal FPE should permute in-charset values; got identity for all of {vals!r}."
        )
        charset_set = set(_ALPHANUM)
        for result in results:
            assert all(ch in charset_set for ch in result), (
                f"Normal FPE output {result!r} contains non-charset chars."
            )

    def test_empty_string_fails_closed(self) -> None:
        """An empty value is not a null; its domain (radix**0 == 1) is below
        the FF1 floor like any other sub-floor value, so it now fails closed
        (plan P2) rather than passing through as a silent no-op."""
        with pytest.raises(FpeUnencryptableError) as exc:
            fpe_encrypt_value("", _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
        assert exc.value.code == "fpe.unencryptable_domain"

    def test_fpe_partial_prefix_preserved(self) -> None:
        """PARTIAL out-of-charset (in-charset body + out-of-charset prefix) is preserved.

        DE-01 scope: the partial-prefix case is a documented residual limitation,
        NOT closed this sprint. The in-charset digits are permuted; the
        out-of-charset prefix/separators are reinserted verbatim (unchanged
        behavior). `"EMP-00001"` has in-charset digits under alphanum, so it does
        NOT fail closed (unlike the all-out-of-charset `"EMP-ORPHAN"`).
        """
        vals = ["EMP-00001", "EMP-00002", "EMP-00003"]  # digits in-charset (alphanum); "EMP-" out
        results = [
            fpe_encrypt_value(val, _KEY, _ALPHANUM, _TWEAK, preserve_separators=True)
            for val in vals
        ]
        # A fixed point on any one digit suffix is legal for a permutation, so the
        # no-op regression guard is aggregate across several independent values.
        assert any(result[4:] != val[4:] for result, val in zip(results, vals, strict=True)), (
            "Digit portion should be permuted for at least one of the test values."
        )
        for result in results:
            assert result[:4] == "EMP-", (
                f"Out-of-charset prefix 'EMP-' should be preserved; got {result[:4]!r}."
            )
