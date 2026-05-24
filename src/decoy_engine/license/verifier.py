"""
LicenseVerifier — stub.

The real verifier will take a JWT, verify the signature against an
embedded public key, and check expiration. The private key lives in
decoy-platform per REPO_ARCHITECTURE_PLAN.md; the public key will be
embedded here.

Current behavior: always returns a free-tier license so CLI and platform
development is unblocked. Replaced before paid tier launch.
"""

from typing import Any


class LicenseVerifier:
    @staticmethod
    def verify(token: str | None = None) -> dict[str, Any]:
        return {
            "tier": "free",
            "features": [],
            "expires_at": None,
        }
