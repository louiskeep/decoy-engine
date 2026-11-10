### Changed (condition_on joint cap bounds and fit warning, GEN-COND-CAP E1, 2026-11-10)

`contingency_top_k` on `compute_distribution_snapshot` and `distribution_snapshot_document` now
accepts only an int from 1 to `MAX_CONTINGENCY_TOP_K` (1000) and raises `DistributionSnapshotError`
with the new `quality.contingency_top_k_invalid` otherwise. Before, 0 kept no cells and a negative
number sliced from the end, and `True` was accepted as 1; a caller passing `True` must pass 1. No
known caller passes the keyword, and the default (25) writes the same bytes as before.

A cap above the default is refused in a plain snapshot (`include_values=False`) when a joint column
is numeric, datetime, freetext, empty or `high_cardinality`, with the new
`quality.contingency_top_k_kind_unsupported`: the extra cells would hold raw values. Plain
categorical pairs keep every combination up to the cap, and `include_values=True` is not refused.

`decoy_engine.snapshot_joint_warnings(doc, contingency_top_k=...)` is new. It returns one
`QualityWarning` (`warning.fit_joint_cells_truncated`) per joint whose cap dropped combinations,
with `columns`, `cell_count`, `cells_kept`, `cap_dropped`, `policy_dropped` and `other_count`, and
skips joints whose values the writer withheld. It carries names and counts only (registry 1.22.0).
