"""Job E: hostile / edge-case data (unicode PII, degenerate shapes).

Topology: mixed (a people -> accounts one-to-many FK relationship on the mask
side, composed with a standalone single-row table and a standalone empty
generate table).

This job exists to exercise data SHAPES that appear NOWHERE in the current
fixtures (verified zero non-ASCII across Jobs A-D; TH-3.3 / P1-10):

  1. Unicode names/notes through fpe, text_mask, and text_redact -- the FPE
     charset catalog is ASCII-only (digits/alpha/ALPHA/alphanum/ALPHANUM), so
     a fully non-ASCII value has ZERO in-charset characters and exercises the
     ALL-out-of-charset covering-hash fallback (fix #42) rather than the
     ordinary Feistel permutation path. text_mask/text_redact operate on raw
     Python strings via regex span detectors, unicode-transparent by
     construction; both are proven against unicode-bearing free text with an
     embedded ASCII PII span (phone / email).
  2. An all-null column (people.middle_name): every source value is null.
  3. A single-row table (singleton, row_count=1).
  4. Duplicate parent-FK values: 40 of the 150 accounts rows reference the
     SAME single "hub" person_id (a heavy repeat, not the varied 1-to-many
     fan-out already exercised by Jobs A-D).
  5. An empty table (empty_table, a GENERATE table with row_count=0) -- the
     engine's own generate-table validator (config._pipeline.py) accepts a
     non-negative row_count, so 0 is a real engine-supported shape, not a
     forced/unsupported one.
"""
