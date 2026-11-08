"""PipelineConfigError: the engine's typed refusal of an invalid pipeline config.

Callers catch ``PipelineConfigError`` without importing Pydantic. It carries the value-free
``problems`` (see ``decoy_engine.safe_output.validation_problems``) and never the Pydantic error:
that error holds the submitted values, so it is neither the ``__cause__`` nor the ``__context__``.
"""

from __future__ import annotations

from collections.abc import Sequence

from decoy_engine.errors import DecoyError
from decoy_engine.safe_output import ConfigProblem


class PipelineConfigError(DecoyError, ValueError):
    """Raised when a pipeline config fails strict validation.

    Subclass of ValueError so the standard "bad input" catch idiom (`except ValueError`) works
    as a fallback. ``problems`` lists each structural problem with its location; the message
    renders the same list.
    """

    def __init__(self, message: str, *, problems: Sequence[ConfigProblem] = ()) -> None:
        super().__init__(message)
        self.problems: tuple[ConfigProblem, ...] = tuple(problems)

    @classmethod
    def from_problems(
        cls, problems: Sequence[ConfigProblem], *, prefix: str = "pipeline config is invalid"
    ) -> PipelineConfigError:
        lines = [f"{prefix} ({len(problems)} problem{'' if len(problems) == 1 else 's'}):"]
        lines += [f"  {p.location or '(root)'}: {p.text} [{p.code}]" for p in problems]
        return cls("\n".join(lines), problems=problems)
