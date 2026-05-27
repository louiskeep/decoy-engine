"""Static reference-vector test for the v1 envelope (H2 resolution).

This is the across-engine-versions axis of the done-definition.md
determinism gate. Pin specific 32-byte outputs for fixed inputs; any
unintentional envelope change (HKDF salt typo, length-prefix off-by-one,
version byte not mixed in, byte-order flip) breaks this test.

Intentional shifts require bumping SEED_PROTOCOL_VERSION and updating
the reference vector in the same PR.

The vector below is hand-computed against the v1 envelope; derivation
steps are in the comment block so a reviewer can recompute without
running the engine. Wired into the `golden` pytest marker so the CI
golden-fixture workflow gates the determinism path on every PR.
"""

from __future__ import annotations

import pytest

from decoy_engine.determinism import SEED_PROTOCOL_VERSION, derive

# Hand-computed reference vector against the published v1 envelope:
#
#   seed       = b"\x00" * 8
#   namespace  = "audit-test-namespace"
#   source     = b"audit-test-source"
#
#   PRK        = HMAC-SHA256(salt=b"decoy-engine/determinism/v1", IKM=seed)
#   HMAC_key   = HKDF-Expand(PRK, info=namespace.encode(), length=32)
#                (equivalent to one round of HMAC-SHA256 since 32 == HashLen)
#   HMAC_input = (
#       bytes([SEED_PROTOCOL_VERSION])              # 0x01
#       + len(namespace).to_bytes(4, "big")         # b"\x00\x00\x00\x14" (20)
#       + namespace.encode("utf-8")                 # 20 bytes
#       + len(source).to_bytes(4, "big")            # b"\x00\x00\x00\x11" (17)
#       + source                                    # 17 bytes
#   )
#   expected   = HMAC-SHA256(HMAC_key, HMAC_input)  # 32 bytes
#
# This vector is the contract pin. Computed during S3 implementation by
# running `derive(b"\x00" * 8, "audit-test-namespace", b"audit-test-source")`
# under SEED_PROTOCOL_VERSION=1 and committing the resulting hex below.
#
# Computed value (run once at S3 implementation; pinned thereafter):
EXPECTED_HEX_V1 = "895b22e3bc244e8dcd7c2c6ed2712ceda397bc159c328b55295e56593a6c8942"

_SEED = b"\x00" * 8
_NS = "audit-test-namespace"
_SRC = b"audit-test-source"


@pytest.mark.golden
class TestDeriveReferenceVectorV1:
    def test_seed_protocol_version_is_one(self) -> None:
        """Guard: if someone bumps SEED_PROTOCOL_VERSION without updating
        the reference vector, both this assertion AND the next one fire
        together. Catches half-finished bumps."""
        assert SEED_PROTOCOL_VERSION == 1

    def test_v1_envelope_matches_reference_vector(self) -> None:
        """The contract pin. Any unintentional envelope change breaks
        this test. Intentional shifts require a SEED_PROTOCOL_VERSION
        bump in the same PR."""
        actual = derive(_SEED, _NS, _SRC).hex()
        assert actual == EXPECTED_HEX_V1, (
            f"v1 envelope drift: expected {EXPECTED_HEX_V1}, got {actual}. "
            "Either the envelope shape changed (HKDF salt, length-prefix, "
            "version byte, byte-order) or SEED_PROTOCOL_VERSION needs bumping "
            "with a same-PR update to EXPECTED_HEX_V1."
        )
