### Changed (pydantic floor 2.12, SAMPLE SE2, 2026-11-12)

The engine now requires pydantic 2.12 or later. The run result leaves an unset `sample` off the wire with pydantic's per-field `exclude_if`, which first shipped in 2.12.
