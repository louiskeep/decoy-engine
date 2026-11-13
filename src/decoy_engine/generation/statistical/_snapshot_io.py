"""Parse step for statistical snapshot bytes, shared by every site that holds the bytes.

Value-free by construction: a refusal carries the typed path, never file content.
"""

from __future__ import annotations

import json
from typing import Any

from decoy_engine import reasons


def parse_snapshot_json(raw: bytes, *, shown: str) -> Any:
    """Parse snapshot bytes as JSON or raise `StatisticalSpecError` (unreadable).

    `shown` is the path as the user typed it. Only the typed text leaves the function: the
    decode errors hold the file (`UnicodeDecodeError.object`, `JSONDecodeError.doc`), so the
    refusal is raised after the handler, with no cause or context.
    """
    # Function-local: `_spec` imports this module, and its error class must finish defining first.
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    failure: str | None = None
    try:
        return json.loads(raw)
    except UnicodeDecodeError:
        failure = "the file is not valid UTF-8 JSON"
    except json.JSONDecodeError as exc:
        # The decoder message holds the error name, line, column and offset, never document text.
        failure = str(exc)
    raise StatisticalSpecError(
        code=reasons.plan.STATISTICAL_SNAPSHOT_UNREADABLE,
        message=f"snapshot_file {shown!r} could not be read: {failure}",
    )
