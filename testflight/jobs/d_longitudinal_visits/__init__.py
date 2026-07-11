"""Job D: longitudinal patient visits (group-aware family + statistical generation).

Topology: mixed (a providers -> patients one-to-many FK relationship on the mask
side, composed with a standalone `visits` generate table).

This job exists to exercise, END-TO-END inside `run_pipeline`, the two engine
capabilities the golden-gate suite previously only unit-tested and allowlisted
out of the coverage guard (TH-3.2 / P1-8):

  1. The group-aware generate family -- `group_key`, `grouped_series`,
     `windowed_date` -- which produce longitudinal / time-series shapes
     (per-patient episode keys, per-patient visit sequence numbers, and
     follow-up dates bounded within a window of an anchor admission date).
  2. The `statistical` generate type (the SDV-style parameterized path) which
     samples synthetic columns from a distribution-snapshot/v1 artifact.

Both are driven through the real pipeline together with relationships (the
providers -> patients FK with an orphan-warn policy), quarantine (luhn on
patients.card_no), and quality reporting (distribution invariants on the mask
tables and the generate table) -- the composition that unit tests do not prove.
"""
