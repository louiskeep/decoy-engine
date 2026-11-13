### Fixed (chained column-level derived columns, DERIVE-CHAIN, 2026-11-13)

A `derived` column that reads another column-level `derived` column is now accepted as a created column, so a declared column the source lacks is no longer refused when it sits at the end of such a chain. A chain whose root reads a missing source column, and a cycle, are still refused.
