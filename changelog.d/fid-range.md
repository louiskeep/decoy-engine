Breaking: numeric fidelity scores drop for skewed or drifted columns; re-check fidelity_warn_threshold and policy floors.

### Changed (numeric fidelity scale, FID-RANGE, 2026-11-10)

Numeric column similarity now divides the quantile error by the source interquartile range
(p75 - p25) instead of the source min-to-max range, so one extreme value no longer hides a wrong
shape. When that range is 0 it uses p95 - p05, then max - min. A constant column scores 1 when the
output quantiles match it and 0 otherwise. The old 1.0 floor is gone, so scores no longer depend
on the column's units. The report shape and method name do not change.
