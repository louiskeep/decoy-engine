"""Static reference-vector test for the v4 envelope.

This is the across-engine-versions axis of the done-definition.md
determinism gate. Pin specific 32-byte outputs for fixed inputs; any
unintentional envelope change (HKDF salt typo, length-prefix off-by-one,
version byte not mixed in, byte-order flip) breaks this test.

Intentional shifts require bumping SEED_PROTOCOL_VERSION and updating
the reference vector in the same PR. Two prior bumps did exactly that:
  v1 -> v2: F-series corrections (Faker pool seeding + canonicalize
            integer encoding). Version byte mixed into the HMAC input
            went 0x01 -> 0x02; every derive output changed.
  v2 -> v4: QA walks/generators F3 / PO Q-F3=b (2026-06-01). Vectorised
            null-injection swap in generators/columns.py changes the
            null PATTERN (the null FRACTION still matches
            null_probability). Even though derive() itself is unchanged
            in shape, the protocol-version byte goes 0x02 -> 0x03 so
            every derive output changes byte-for-byte; the reference
            vector is regenerated in the same change.

The vector below is recomputed against the v4 envelope; derivation
steps are in the comment block so a reviewer can recompute without
running the engine. Wired into the `golden` pytest marker so the CI
golden-fixture workflow gates the determinism path on every PR.
"""

from __future__ import annotations

import pytest

from decoy_engine.determinism import SEED_PROTOCOL_VERSION, derive

# Hand-computed reference vector against the published v4 envelope:
#
#   seed       = b"\x00" * 8
#   namespace  = "audit-test-namespace"
#   source     = b"audit-test-source"
#
#   PRK        = HMAC-SHA256(salt=b"decoy-engine/determinism/v1", IKM=seed)
#   HMAC_key   = HKDF-Expand(PRK, info=namespace.encode(), length=32)
#                (equivalent to one round of HMAC-SHA256 since 32 == HashLen)
#   HMAC_input = (
#       bytes([SEED_PROTOCOL_VERSION])              # 0x03 (v4 envelope)
#       + len(namespace).to_bytes(4, "big")         # b"\x00\x00\x00\x14" (20)
#       + namespace.encode("utf-8")                 # 20 bytes
#       + len(source).to_bytes(4, "big")            # b"\x00\x00\x00\x11" (17)
#       + source                                    # 17 bytes
#   )
#   expected   = HMAC-SHA256(HMAC_key, HMAC_input)  # 32 bytes
#
# Computed value (regenerated at the v2 -> v4 bump for QA walks/gen F3
# null-injection vectorisation, PO Q-F3=b 2026-06-01; pinned thereafter):
EXPECTED_HEX_V4 = "6ac54bae81fee2d5f97a5d547054d3bed5c51f6cb8e5309af22bc84435edd364"

_SEED = b"\x00" * 8
_NS = "audit-test-namespace"
_SRC = b"audit-test-source"


@pytest.mark.golden
class TestDeriveReferenceVectorV4:
    def test_seed_protocol_version_is_four(self) -> None:
        """Guard: if someone bumps SEED_PROTOCOL_VERSION without updating
        the reference vector, both this assertion AND the next one fire
        together. Catches half-finished bumps."""
        assert SEED_PROTOCOL_VERSION == 4

    def test_v4_envelope_matches_reference_vector(self) -> None:
        """The contract pin. Any unintentional envelope change breaks
        this test. Intentional shifts require a SEED_PROTOCOL_VERSION
        bump in the same PR."""
        actual = derive(_SEED, _NS, _SRC).hex()
        assert actual == EXPECTED_HEX_V4, (
            f"v4 envelope drift: expected {EXPECTED_HEX_V4}, got {actual}. "
            "Either the envelope shape changed (HKDF salt, length-prefix, "
            "version byte, byte-order) or SEED_PROTOCOL_VERSION needs bumping "
            "with a same-PR update to EXPECTED_HEX_V4."
        )
