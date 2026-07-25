"""PipelineConfigError: wraps Pydantic ValidationError into the engine's
error vocabulary so callers can `except PipelineConfigError as e:` without
importing Pydantic directly.

Optional in the sense that callers can also catch
`pydantic.ValidationError` directly; this wrapper exists so a future swap
of validation backend (e.g. to attrs or msgspec) does not break caller
exception-handling shapes.
"""

from __future__ import annotations


class PipelineConfigError(ValueError):
    """Raised when a pipeline config fails strict validation.

    Subclass of ValueError so the standard "bad input" catch idiom
    (`except ValueError`) works as a fallback. The instance carries the
    original Pydantic ValidationError as `__cause__` so callers can
    drill into per-field errors when needed.
    """

    def __init__(self, message: str, *, validation_error: Exception | None = None) -> None:
        super().__init__(message)
        if validation_error is not None:
            self.__cause__ = validation_error
