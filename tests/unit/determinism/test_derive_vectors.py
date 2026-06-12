"""Static reference-vector test for the v5 envelope.

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

  v4 -> v5: WS1 detokenization (2026-06-12). FPE re-keyed to one key
            per (seed, namespace) (NIST FF1 key model) + invertible
            Luhn mode; the version byte goes 0x04 -> 0x05 so every
            derive output changes; vector regenerated in the same
            change.

The vector below is recomputed against the v5 envelope; derivation
steps are in the comment block so a reviewer can recompute without
running the engine. Wired into the `golden` pytest marker so the CI
golden-fixture workflow gates the determinism path on every PR.
"""

from __future__ import annotations

import pytest

from decoy_engine.determinism import SEED_PROTOCOL_VERSION, derive

# Hand-computed reference vector against the published v5 envelope:
#
#   seed       = b"\x00" * 8
#   namespace  = "audit-test-namespace"
#   source     = b"audit-test-source"
#
#   PRK        = HMAC-SHA256(salt=b"decoy-engine/determinism/v1", IKM=seed)
#   HMAC_key   = HKDF-Expand(PRK, info=namespace.encode(), length=32)
#                (equivalent to one round of HMAC-SHA256 since 32 == HashLen)
#   HMAC_input = (
#       bytes([SEED_PROTOCOL_VERSION])              # 0x05 (v5 envelope)
#       + len(namespace).to_bytes(4, "big")         # b"\x00\x00\x00\x14" (20)
#       + namespace.encode("utf-8")                 # 20 bytes
#       + len(source).to_bytes(4, "big")            # b"\x00\x00\x00\x11" (17)
#       + source                                    # 17 bytes
#   )
#   expected   = HMAC-SHA256(HMAC_key, HMAC_input)  # 32 bytes
#
# Computed value (regenerated at the v4 -> v5 bump for WS1 FPE
# detokenization, 2026-06-12; pinned thereafter):
EXPECTED_HEX_V5 = "0a8e61e153833d918096a5815a2c127263ed0e45c8a6c80ffb8d324200df502b"

_SEED = b"\x00" * 8
_NS = "audit-test-namespace"
_SRC = b"audit-test-source"


@pytest.mark.golden
class TestDeriveReferenceVectorV5:
    def test_seed_protocol_version_is_five(self) -> None:
        """Guard: if someone bumps SEED_PROTOCOL_VERSION without updating
        the reference vector, both this assertion AND the next one fire
        together. Catches half-finished bumps."""
        assert SEED_PROTOCOL_VERSION == 5

    def test_v5_envelope_matches_reference_vector(self) -> None:
        """The contract pin. Any unintentional envelope change breaks
        this test. Intentional shifts require a SEED_PROTOCOL_VERSION
        bump in the same PR."""
        actual = derive(_SEED, _NS, _SRC).hex()
        assert actual == EXPECTED_HEX_V5, (
            f"v5 envelope drift: expected {EXPECTED_HEX_V5}, got {actual}. "
            "Either the envelope shape changed (HKDF salt, length-prefix, "
            "version byte, byte-order) or SEED_PROTOCOL_VERSION needs bumping "
            "with a same-PR update to EXPECTED_HEX_V5."
        )
