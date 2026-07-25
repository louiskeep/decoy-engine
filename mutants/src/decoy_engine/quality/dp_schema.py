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

# Release-shape identity recorded in the v3 artifact (guide sections 3.9/4.4).
# The consume-side verifier (`plan/_checks_dp.py`) must require these EXACT
# values on every accepted artifact: the stability-1 adjacency argument is
# specific to single-column marginals under add/remove-one-row, so an artifact
# recording a different scope or adjacency (or a codec version this build does
# not implement) is not one whose guarantee this consumer can stand behind, and
# is rejected rather than accepted as DP-verified. Kept here, pandas-free, so
# both the fit (`quality/dp.py`) and the plan-time verifier reference one
# source of truth (dennis round 9 rationale for the schema version).
DP_RELEASE_SCOPE = "single-column-marginals"
DP_ADJACENCY = "add-remove-one-row"
# The `boundary` records how the fit reached its values: a pandas adapter
# (`carrier_adapter.py`) or a direct pandas-free CarrierTable. Both are
# certified; the field must be one of these known values, not absent or novel.
DP_BOUNDARY_VALUES = ("adapter", "direct")
