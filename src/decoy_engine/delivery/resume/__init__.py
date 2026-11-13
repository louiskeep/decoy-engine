"""Resume of a failed chunked run: the fingerprint that decides a stored table may be reused.

Public surface: `RunFingerprint`, `build_fingerprint`, `binding_digest`. The records, the codec and
`plan_resume` join this package in the next slice. Everything here is pure and value-free.
"""

from __future__ import annotations

from decoy_engine.delivery.resume._fingerprint import (
    RunFingerprint,
    binding_digest,
    build_fingerprint,
)

__all__ = ["RunFingerprint", "binding_digest", "build_fingerprint"]
