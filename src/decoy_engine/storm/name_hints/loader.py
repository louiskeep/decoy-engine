"""YAML loader for the STORM detector name-hint library.

Reads ``v1/manifest.yaml`` to find the file load order, walks each
listed file, validates the entries, and returns a flat dict mapping
``detector_id`` to the list of column-name term strings whose presence
should boost that detector's confidence. The caller
(``decoy_engine.storm.detectors``) applies its existing ``_hint(terms)``
regex helper to build the actual ``re.Pattern`` objects; this loader
is intentionally a pure data layer so the regex-construction logic
lives in exactly one place.

Mirrors the established pattern in ``decoy_engine.disguises.loader``:
package-relative path lookup via ``Path(__file__).parent``, eager
validation at import time, hatchling ships the YAML files in the
wheel by default.

Failure modes (all raise ``NameHintLoaderError`` with a clear message):
  - ``manifest.yaml`` missing or unparseable
  - File listed in manifest does not exist
  - Same detector_id declared in two different files
  - Empty patterns list (a detector with no terms cannot hint anything)
  - YAML entry missing the required ``detector_id`` or ``patterns`` key
  - Pattern term is not a string

Loader is called once at engine import time
(``decoy_engine.storm.detectors`` module-level statement). The result
is cached for the lifetime of the process; reloading at runtime would
require restarting the engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_V1_DIR = Path(__file__).parent / "v1"
_MANIFEST_NAME = "manifest.yaml"


class NameHintLoaderError(Exception):
    """Raised when the name-hint library cannot be loaded.

    Always carries a concrete file path and a clear reason so the
    operator can fix the YAML without grep-bisecting the directory.
    """


def load_name_hint_terms(directory: Path | None = None) -> dict[str, list[str]]:
    """Read the YAML library and return ``{detector_id: [terms]}``.

    The directory defaults to ``v1/`` next to this module. Tests pass
    a temp directory to exercise edge cases (malformed YAML, missing
    files, duplicate ids) without touching the shipped data.

    Returns a fresh dict on each call; the caller owns it.
    """
    target = directory or _V1_DIR
    manifest_path = target / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise NameHintLoaderError(
            f"name-hint manifest not found at {manifest_path}. "
            "Every name-hint directory must contain a manifest.yaml that "
            "lists the YAML files to load."
        )

    manifest = _read_yaml(manifest_path)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files:
        raise NameHintLoaderError(
            f"{manifest_path} must define a non-empty `files:` list."
        )

    # Tracks which file declared each detector_id so a collision error
    # can name both sides. Order of insertion mirrors manifest order
    # so error messages point at the LATER file (the new one being
    # added), not the first.
    detector_origin: dict[str, str] = {}
    merged: dict[str, list[str]] = {}

    for fname in files:
        if not isinstance(fname, str):
            raise NameHintLoaderError(
                f"{manifest_path} `files:` entry must be a string, got {fname!r}."
            )
        file_path = target / fname
        if not file_path.is_file():
            raise NameHintLoaderError(
                f"{manifest_path} lists {fname!r} but {file_path} does not exist."
            )
        for det_id, terms in _read_detector_file(file_path).items():
            if det_id in merged:
                first = detector_origin[det_id]
                raise NameHintLoaderError(
                    f"detector_id {det_id!r} is declared in both "
                    f"{first} and {file_path}. Each detector_id may "
                    "appear in exactly one file."
                )
            merged[det_id] = terms
            detector_origin[det_id] = str(file_path)

    return merged


def _read_detector_file(path: Path) -> dict[str, list[str]]:
    """Parse one YAML file's ``detectors:`` list into {detector_id: [terms]}.

    A file with no detectors (empty list or missing key) is permitted
    -- it's a no-op contribution. A file with an entry that lacks
    ``detector_id`` or ``patterns`` is an error.
    """
    data = _read_yaml(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise NameHintLoaderError(
            f"{path} must be a YAML mapping at the top level, got {type(data).__name__}."
        )
    raw_entries = data.get("detectors")
    if raw_entries is None:
        return {}
    if not isinstance(raw_entries, list):
        raise NameHintLoaderError(
            f"{path} `detectors:` must be a list, got {type(raw_entries).__name__}."
        )

    out: dict[str, list[str]] = {}
    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise NameHintLoaderError(
                f"{path} entry #{idx} must be a mapping, got {type(entry).__name__}."
            )
        det_id = entry.get("detector_id")
        patterns = entry.get("patterns")
        if not isinstance(det_id, str) or not det_id:
            raise NameHintLoaderError(
                f"{path} entry #{idx} is missing a non-empty `detector_id:` string."
            )
        if not isinstance(patterns, list) or not patterns:
            raise NameHintLoaderError(
                f"{path} detector {det_id!r} must define a non-empty `patterns:` list."
            )
        cleaned_terms: list[str] = []
        for t_idx, term in enumerate(patterns):
            if not isinstance(term, str) or not term:
                raise NameHintLoaderError(
                    f"{path} detector {det_id!r} pattern #{t_idx} must be a "
                    f"non-empty string, got {term!r}."
                )
            cleaned_terms.append(term)
        if det_id in out:
            raise NameHintLoaderError(
                f"{path} declares detector_id {det_id!r} twice. Each "
                "detector_id may appear at most once per file."
            )
        out[det_id] = cleaned_terms
    return out


def _read_yaml(path: Path) -> Any:
    """yaml.safe_load with a clear error chain on parse failure."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise NameHintLoaderError(f"could not parse {path}: {exc}") from exc
