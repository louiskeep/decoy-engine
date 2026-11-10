### Fixed (profile every row at near the old speed, row 24f follow-up, 2026-11-10)

`profile_columns` on a full frame is back to seconds at 1M rows x 8 columns (it took minutes after the
CLI began profiling every row). Detectors match each distinct value once and weight by count, so
the match rate, sample misses and format pattern are the same as matching every cell. The date probe
judges the parse rate on the first 200 non-null values and coerces the full series only for date
columns. K-anonymity factorizes each candidate column once, the sentinel scan judges distinct values,
and the casing and alphabet samples convert only the rows they read. Profile output is unchanged
except k-anonymity, which now also covers category-dtype and mixed-object columns that the old
groupby got wrong or skipped: a category column with unused categories used to give k=0 and now
gives k=1, and mixed-object combos the old groupby skipped are now reported.
