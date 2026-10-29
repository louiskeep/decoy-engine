# Error reporting and data exposure

Status: current

Decoy processes the sensitive data it masks. This note says what the engine's error messages promise about that data, and what an operator must configure so that error tooling does not undo it.

## What the engine promises

- **The fixed-width reader's bad-data errors do not contain that data.** For a failed cast, a short record, bytes that are not valid UTF-8, and a malformed layout, `decoy_engine.read_fixed_width` names the position (file, line, column, length) and never the value, and does not chain the internal exception that held it. The first three are raised from a frame that holds no file data; tests check the message, the chain and the captured frame locals for those three, and the message and chain for the malformed layout (which fails before the file is opened). Other engine code paths make no frame-level promise yet.
- **Row-level errors follow the same rule** (`RowErrorsFailedError` and the row-error framework): positions and reasons, never cell values.

- **Structured reasons carry fixed vocabulary only (F29).** A `ReasonRecord` (`decoy_engine.reason_from_exception`, `IsolatedRunResult.reason`, the `reason` group of a progress event) holds a registered identity, a phase, a severity, an outcome category, a retry class and a closed context: approved identifiers, fixed enums and bounded counts. It never holds an exception message, a class name, a cause, a traceback, a path or a connection string, and an exception's `.code` is trusted only for a registered engine exception class and a code registered for it. Tests inject markers into the message, code, class name, cause, filename and size of an exception and search every serialized form for them (`tests/security/test_reason_boundary.py`). The shared safe-output boundary (F31) does not exist yet; until it does, identifier fields are limited to a strict shape and the engine's own table and column names.

## What it does not promise

- **Failures that do not come from bad input data are out of scope.** Examples: a disk or network error part-way through a read, running out of memory, or a bug. Such an exception may be raised from code that is holding real rows at that moment. Python keeps those rows reachable through the traceback's frame locals.
- **Stack frames hold data.** Every masking step works on real values in memory. Any tool that records local variables from stack frames can capture them.

## Operator guidance

Do not enable local-variable capture in error trackers or crash reporters for processes that run Decoy jobs:

- Sentry: keep `include_local_variables` (and the older `with_locals`) set to `False`.
- Python `traceback.TracebackException(..., capture_locals=True)`, `faulthandler`-style dumpers and APM agents that snapshot frame locals: leave locals capture off for Decoy workers.
- Core dumps of worker processes contain the data being processed; treat them as sensitive or disable them.

Turning off local-variable capture stops frame locals from leaving the process. It does not stop an exception message from carrying data: a parser, validator or Arrow error can quote the cell, expression or file text it choked on, and `logging.exception` or `exc_info=True` writes that message and the stack lines to the log. So the engine's own log calls and user-visible error fields record the exception type, a stable code and counts, never `str(exc)` (enforced by `tests/sentry/test_log_interpolation.py`). `IsolatedRunResult.error` is a safe summary: return code, signal, a stable reason, memory figures and a typed `reason` record whose registry identity (never message text) is what the governor reroutes on, with no exception message, child stdout or stderr, or output path. Third-party code that logs an exception from a Decoy worker can still echo a message, so keep job logs as restricted as the data.
