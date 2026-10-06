"""Disguise YAML loader.

`load_disguises()` reads every `*.yaml` file co-located with this module
and returns a list of `Disguise` instances. Loading is deterministic
(sorted by filename) so test fixtures and golden snapshots are stable.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from decoy_engine.disguises.schema import Disguise

_log = logging.getLogger(__name__)

_DISGUISES_DIR = Path(__file__).parent


def load_disguises(directory: Path | None = None) -> list[Disguise]:
    """Load every *.yaml file in `directory` (defaults to this package's dir).

    Files are validated against the Disguise schema. QA-internal-synth-
    providers F8 (2026-06-01, MEDIUM correctness): per-file errors are
    now caught + logged + skipped instead of aborting the whole load.
    Pre-fix a single malformed YAML or schema-invalid bundle file
    aborted load_disguises entirely, leaving the engine running with
    a partial disguise set (every bundle sorted after the bad file
    silently absent). Now each bad file logs an error + skips so the
    rest of the bundle catalogue still loads.
    """
    target = directory or _DISGUISES_DIR
    yamls = sorted(target.glob("*.yaml"))
    out: list[Disguise] = []
    for path in yamls:
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            # PyYAML's message quotes the offending text; log only where it failed.
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            _log.error(
                "load_disguises: YAML parse failed for %s (%s%s)",
                path.name,
                type(exc).__name__,
                where,
            )
            continue
        except OSError as exc:
            _log.error(
                "load_disguises: could not read %s: %s",
                path.name,
                exc,
            )
            continue

        if data is None:
            continue

        try:
            out.append(Disguise(**data))
        except Exception as exc:
            # pydantic.ValidationError + any other constructor failure.
            # Broad except keeps the loader resilient to schema drift. The
            # exception text echoes the offending input, which can hold config
            # expressions and literals, so only locations and error types are logged.
            _log.error(
                "load_disguises: schema validation failed for %s: %s",
                path.name,
                _safe_validation_summary(exc),
            )
    return out


def _safe_validation_summary(exc: Exception) -> str:
    """Field locations and error types from a validation failure, never the input values."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors(include_input=False, include_url=False, include_context=False)
        except TypeError:
            items = None
        if isinstance(items, list):
            parts = [
                f"{'.'.join(str(p) for p in item.get('loc', ()))}: {item.get('type', '?')}"
                for item in items
                if isinstance(item, dict)
            ]
            return f"{type(exc).__name__} ({'; '.join(parts)})"
    return type(exc).__name__
