### Changed (fitted number columns keep their shape, GEN-NUM-SHAPE, 2026-11-10)

A fit now stores a quantile grid (`stats.quantile_grid`) for each number column with at least
100 values and more than 30 distinct values, and statistical generation samples from it instead
of ten equal-width bins. One extreme value no longer spreads generated rows across the whole
range, and skewed columns keep their median. Every interval and tail has at least 10 source rows,
and generated numbers stay between the 10th smallest and 10th largest source value. This applies
to columns that get a grid or exact values; a column under 100 rows keeps the earlier histogram
behavior, which can emit values anywhere in the source range. An interior
grid knot can equal a source value held by fewer than 10 rows, as today's plain value-bearing
snapshot quantiles and bin edges already can. A redacted fit withholds the grid.

A number column with 30 or fewer distinct values stores those values (`stats.exact_values`),
leaving out any value held by fewer than 10 rows, and generation emits them at their source
shares. No extra flag is needed: every stored value is held by at least 10 source rows.

Snapshots fitted by earlier versions have neither key and generate exactly as before; re-fit to
get the new shape. A redacted fit drops both keys for sensitive columns.
