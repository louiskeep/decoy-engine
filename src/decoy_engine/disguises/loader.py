"""Disguise YAML loader.

`load_disguises()` reads every `*.yaml` file co-located with this module
and returns a list of `Disguise` instances. Loading is deterministic
(sorted by filename) so test fixtures and golden snapshots are stable.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from decoy_engine.disguises.schema import Disguise

_DISGUISES_DIR = Path(__file__).parent


def load_disguises(directory: Path | None = None) -> list[Disguise]:
    """Load every *.yaml file in `directory` (defaults to this package's dir).

    Files are validated against the Disguise schema. Pydantic raises
    ValidationError on malformed input, which surfaces immediately at
    package load time so a broken bundle fails CI rather than silently
    skewing recommendations.
    """
    target = directory or _DISGUISES_DIR
    yamls = sorted(target.glob("*.yaml"))
    out: list[Disguise] = []
    for path in yamls:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            continue
        out.append(Disguise(**data))
    return out
