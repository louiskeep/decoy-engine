"""RFC 5869 HKDF-SHA256 reference vector tests.

The RFC 5869 §A.1, §A.2, §A.3 vectors are the published cross-
implementation reference. If our 30-line stdlib implementation passes
all three, the implementation is correct against the specification.

Source: https://datatracker.ietf.org/doc/html/rfc5869#appendix-A
"""

from __future__ import annotations

import pytest

from decoy_engine.determinism._hkdf import hkdf_expand, hkdf_extract, hkdf_sha256


class TestRFC5869VectorA1:
    """Test Case 1: Basic test case with SHA-256."""

    IKM = bytes.fromhex("0b" * 22)
    SALT = bytes.fromhex("000102030405060708090a0b0c")
    INFO = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    L = 42
    PRK = bytes.fromhex("077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5")
    OKM = bytes.fromhex(
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
    )

    def test_extract(self) -> None:
        assert hkdf_extract(self.SALT, self.IKM) == self.PRK

    def test_expand(self) -> None:
        assert hkdf_expand(self.PRK, self.INFO, self.L) == self.OKM

    def test_oneshot(self) -> None:
        assert hkdf_sha256(self.IKM, self.SALT, self.INFO, self.L) == self.OKM


class TestRFC5869VectorA2:
    """Test Case 2: Test with SHA-256 and longer inputs/outputs."""

    IKM = bytes.fromhex(
        "000102030405060708090a0b0c0d0e0f"
        "101112131415161718191a1b1c1d1e1f"
        "202122232425262728292a2b2c2d2e2f"
        "303132333435363738393a3b3c3d3e3f"
        "404142434445464748494a4b4c4d4e4f"
    )
    SALT = bytes.fromhex(
        "606162636465666768696a6b6c6d6e6f"
        "707172737475767778797a7b7c7d7e7f"
        "808182838485868788898a8b8c8d8e8f"
        "909192939495969798999a9b9c9d9e9f"
        "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
    )
    INFO = bytes.fromhex(
        "b0b1b2b3b4b5b6b7b8b9babbbcbdbebf"
        "c0c1c2c3c4c5c6c7c8c9cacbcccdcecf"
        "d0d1d2d3d4d5d6d7d8d9dadbdcdddedf"
        "e0e1e2e3e4e5e6e7e8e9eaebecedeeef"
        "f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff"
    )
    L = 82
    PRK = bytes.fromhex("06a6b88c5853361a06104c9ceb35b45cef760014904671014a193f40c15fc244")
    OKM = bytes.fromhex(
        "b11e398dc80327a1c8e7f78c596a4934"
        "4f012eda2d4efad8a050cc4c19afa97c"
        "59045a99cac7827271cb41c65e590e09"
        "da3275600c2f09b8367793a9aca3db71"
        "cc30c58179ec3e87c14c01d5c1f3434f"
        "1d87"
    )

    def test_extract(self) -> None:
        assert hkdf_extract(self.SALT, self.IKM) == self.PRK

    def test_expand(self) -> None:
        assert hkdf_expand(self.PRK, self.INFO, self.L) == self.OKM

    def test_oneshot(self) -> None:
        assert hkdf_sha256(self.IKM, self.SALT, self.INFO, self.L) == self.OKM


class TestRFC5869VectorA3:
    """Test Case 3: SHA-256 with zero-length salt/info.

    QA-10 F12 (2026-06-01) closure: `hkdf_extract` now rejects EMPTY
    salts to defend against accidental degradation. RFC 5869 §2.2
    documents the equivalence between empty-salt and a 32-zero-byte
    salt; callers that genuinely want the RFC 5869 §A.3 default must
    pass `b"\\x00" * 32` explicitly. This cell uses the explicit
    zero-byte salt + asserts the RFC A.3 vector is reproduced from
    that input, locking the equivalence.
    """

    IKM = bytes.fromhex("0b" * 22)
    # QA-10 F12: explicit zero-byte salt instead of empty bytes.
    SALT_EQUIV = b"\x00" * 32
    INFO = b""
    L = 42
    PRK = bytes.fromhex("19ef24a32c717b167f33a91d6f648bdf96596776afdb6377ac434c1c293ccb04")
    OKM = bytes.fromhex(
        "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8"
    )

    def test_extract(self) -> None:
        # RFC 5869 §2.2: empty-salt is equivalent to HashLen zero bytes.
        # QA-10 F12 makes us pass the equivalent explicitly.
        assert hkdf_extract(self.SALT_EQUIV, self.IKM) == self.PRK

    def test_expand(self) -> None:
        assert hkdf_expand(self.PRK, self.INFO, self.L) == self.OKM

    def test_oneshot(self) -> None:
        assert hkdf_sha256(self.IKM, self.SALT_EQUIV, self.INFO, self.L) == self.OKM

    def test_empty_salt_rejected_by_extract(self) -> None:
        """QA-10 F12: passing literal empty bytes raises ValueError
        with a hint about the explicit-zero-byte workaround."""
        with pytest.raises(ValueError, match="salt must be non-empty"):
            hkdf_extract(b"", self.IKM)


class TestHkdfBounds:
    def test_expand_rejects_length_exceeding_rfc_max(self) -> None:
        prk = b"\x00" * 32
        with pytest.raises(ValueError, match="exceeds RFC 5869 maximum"):
            hkdf_expand(prk, b"info", 255 * 32 + 1)

    def test_expand_accepts_max_length(self) -> None:
        prk = b"\x00" * 32
        out = hkdf_expand(prk, b"info", 255 * 32)
        assert len(out) == 255 * 32
