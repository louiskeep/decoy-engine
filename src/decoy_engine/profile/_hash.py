"""SHA-256 hash over a Profile's data-shape fields.

profile_hash is the second input to compile_plan (the first is the
config). Two Profile instances with identical data-shape fields produce
the same hash regardless of their sidecar metadata (profiled_at,
decoy_engine_version, profile_seed). This is the resolution of B1 in
the Dennis spec review: the planner contract requires byte-identical
plans for repeat compiles, and a timestamp in the hash would break that
contract for the natural case of "run decoy plan twice in a row."

The canonical-bytes function lives in _serialize. profile_hash is just
hashlib.sha256(_data_shape_bytes(profile)).hexdigest().
"""

from __future__ import annotations

import hashlib

from decoy_engine.profile._serialize import _data_shape_bytes
from decoy_engine.profile._types import Profile


def profile_hash(profile: Profile) -> str:
    """Return the hex-encoded SHA-256 hash of the profile's data-shape fields.

    Data-shape fields are schema_version, tables, and relationships.
    Sidecar metadata is excluded.
    """
    return hashlib.sha256(_data_shape_bytes(profile)).hexdigest()
