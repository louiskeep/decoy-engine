# Error reporting and data exposure

Status: current

Decoy processes the sensitive data it masks. This note says what the engine's error messages promise about that data, and what an operator must configure so that error tooling does not undo it.

## What the engine promises

- **The fixed-width reader's bad-data errors do not contain that data.** For a failed cast, a short record, bytes that are not valid UTF-8, and a malformed layout, `decoy_engine.read_fixed_width` names the position (file, line, column, length) and never the value, and does not chain the internal exception that held it. The first three are raised from a frame that holds no file data; tests check the message, the chain and the captured frame locals for those three, and the message and chain for the malformed layout (which fails before the file is opened). Other engine code paths make no frame-level promise yet.
- **Row-level errors follow the same rule** (`RowErrorsFailedError` and the row-error framework): positions and reasons, never cell values.

## What it does not promise

- **Failures that do not come from bad input data are out of scope.** Examples: a disk or network error part-way through a read, running out of memory, or a bug. Such an exception may be raised from code that is holding real rows at that moment. Python keeps those rows reachable through the traceback's frame locals.
- **Stack frames hold data.** Every masking step works on real values in memory. Any tool that records local variables from stack frames can capture them.

## Operator guidance

Do not enable local-variable capture in error trackers or crash reporters for processes that run Decoy jobs:

- Sentry: keep `include_local_variables` (and the older `with_locals`) set to `False`.
- Python `traceback.TracebackException(..., capture_locals=True)`, `faulthandler`-style dumpers and APM agents that snapshot frame locals: leave locals capture off for Decoy workers.
- Core dumps of worker processes contain the data being processed; treat them as sensitive or disable them.

Default logging (`logging.exception`, `exc_info=True`) renders messages and stack lines only, not local variables, and is safe under the promises above.
