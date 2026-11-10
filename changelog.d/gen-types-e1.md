### Added (constant and parametric distribution generate types, GEN-TYPES E1, 2026-11-10)

Two new generate column types need no source file.

`type: constant` with `value:` gives every row that value (a string, integer, float or bool).

`type: distribution` with `family:` draws from a named family: `normal` (`mean`, `std`), `uniform`
(`low`, `high`), `exponential` (`mean`), `poisson` (`mean`) or `zipf` (`s`, `n`, ranks 1 to `n`). Each family takes exactly its own keys and
refuses any other. Optional `min` and `max` clip the draw. Output depends only on the seed and the
column, and is the same on the resident, streamed and pinned routes. Both types accept
`null_probability`.

`generate_option_catalog()` entries gain `required_when`, which lists the keys a chosen family (or
a `weighted` reference) requires. The capability matrix now lists `constant` and no longer calls
`distribution` a legacy entry.
