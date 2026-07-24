"""Pandas-free DP artifact schema constants.

Split out of `quality/snapshot.py` (which imports pandas) so the
pandas/pyarrow-free carrier core (`quality/carriers.py`) and the `plan`
compile path can reference the DP schema version without dragging pandas
into their import closure. `snapshot.py` re-exports the name for its
existing consumers, and this module is the single source of truth (dennis
round 9: two independent literals with nothing pinning them equal let a
one-sided bump silently reject every artifact).
"""

from __future__ import annotations

# Codec-metadata schema for a differentially-private snapshot. Bumped to v3
# in DPS-CODEC phase 5 (guide section 3.9): the v3 artifact adds
# `column_schema`, per-column `carrier`, the codec id/version, the recorded
# proof-stack identity (platform triple, full CPython version, lock
# fingerprint), and the source `boundary` (adapter vs direct). Pre-GA hard
# break, no back-compat shim -- v2 artifacts are rejected by the generation
# gate, which accepts only this exact version.
DP_SNAPSHOT_SCHEMA_VERSION = "dps-marginal/v3"

# Codec identity recorded in the v3 artifact (guide section 3.9). The single
# closed carrier codec set (`quality/carriers.py`) is versioned as one unit:
# a change to any codec's totality/boxing behaviour bumps this, so an artifact
# fit under a different codec generation is distinguishable. Bytes-independent
# of the library-version annotation, which the proof-stack fingerprint gates.
DP_CODEC_ID = "decoy-carrier-codec"
DP_CODEC_VERSION = "1"
