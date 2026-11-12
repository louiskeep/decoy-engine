### Added (`decoy_engine.support` bundle types, F32 E1, 2026-11-12)

A new `decoy_engine.support` package holds the closed types of a support bundle: the structure of a
run (versions, limits, routes, timings, coded errors, topology) with no cell value, name, path or
free text. Table, column and node names are `Label` ordinals, versions are checked `Version`
strings, and reasons are `ReasonRef` values that must be registered. A test walks every type
reachable from `SupportBundle` and fails on any free `str` field. The package reads `execution`,
`profile` and `reasons`; nothing in the engine imports it. Nothing builds or sends a bundle yet.
