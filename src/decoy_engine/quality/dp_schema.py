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

# Codec-metadata schema for a differentially-private snapshot. Stays at v2
# in DPS-CODEC phase 1; the v3 bump that adds `column_schema`/carrier/codec
# fields (guide section 3.9) lands with the artifact work in a later phase.
DP_SNAPSHOT_SCHEMA_VERSION = "dps-marginal/v2"
