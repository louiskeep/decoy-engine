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

import stat
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from decoy_engine import fileaccess
from decoy_engine.errors import DecoyError
from decoy_engine.fileaccess import DirHandle, FileAccess, FileAccessDenied, FileRequest, FileRole
from decoy_engine.fileaccess.adapters import serial

_V1_DIR = Path(__file__).parent / "v1"
_MANIFEST_NAME = "manifest.yaml"


class NameHintLoaderError(DecoyError):
    """Raised when the name-hint library cannot be loaded.

    Names the offending file and a clear reason so the operator can fix the YAML without
    grep-bisecting the directory: the real path under the ambient CLI policy, the capability
    display under any other. A strict policy preserves a denial rather than translating it.
    """


def load_name_hint_terms(
    directory: Path | None = None, *, file_access: FileAccess | None = None
) -> dict[str, list[str]]:
    """Read the YAML library and return ``{detector_id: [terms]}``.

    The directory defaults to ``v1/`` next to this module, which is package data the engine
    owns. Tests pass a temp directory to exercise edge cases (malformed YAML, missing
    files, duplicate ids) without touching the shipped data; a directory a caller names is
    read through the file-access policy.

    Returns a fresh dict on each call; the caller owns it.
    """
    handle: DirHandle
    if directory is None:
        handle = fileaccess.engine_dir_handle(_V1_DIR)
        target = _V1_DIR
    else:
        policy = fileaccess.capture(file_access)
        handle = policy.open_dir(
            FileRequest(FileRole.MODEL_PACK_ROOT, "read_dir", str(directory), "storm.name_hints")
        )
        # Diagnostics name the directory by path under the ambient CLI policy and by config
        # position under any other.
        target = directory if fileaccess.is_ambient(policy) else Path("storm.name_hints")
    try:
        return _load_terms(handle, target)
    finally:
        handle.close()


def _child_display(target: Path, name: str) -> str:
    """How a diagnostic names a file the manifest lists. Never `target / name`: an absolute or
    `..` member name would replace or climb out of the directory display and surface a real
    locator (``Path("storm.name_hints") / "/etc/secret"`` is ``Path("/etc/secret")``). The
    ambient CLI keeps the real path; any other policy names the capability display and the bare
    member name only."""
    if fileaccess.is_ambient():
        return str(target / name)
    return f"{target}/{PurePosixPath(name).name}"


def _is_file(handle: DirHandle, name: str) -> bool:
    try:
        member = handle.open_file(name)
        try:
            return stat.S_ISREG(member.stat().st_mode)
        finally:
            member.close()
    except FileAccessDenied:
        # A strict policy's denial is a real refusal: an outside-root member, or (deny-by-default,
        # no existence oracle) an absent one. Preserve it instead of laundering it into an "absent"
        # result and a "does not exist" message built from a locator; it is already locator-free.
        if not fileaccess.is_ambient():
            raise
        return False
    except (FileNotFoundError, NotADirectoryError):
        return False


def _load_terms(handle: DirHandle, target: Path) -> dict[str, list[str]]:
    manifest_display = _child_display(target, _MANIFEST_NAME)
    if not _is_file(handle, _MANIFEST_NAME):
        raise NameHintLoaderError(
            f"name-hint manifest not found at {manifest_display}. "
            "Every name-hint directory must contain a manifest.yaml that "
            "lists the YAML files to load."
        )

    manifest = _read_yaml(handle, _MANIFEST_NAME, manifest_display)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files:
        raise NameHintLoaderError(f"{manifest_display} must define a non-empty `files:` list.")

    # Tracks which file declared each detector_id so a collision error
    # can name both sides. Order of insertion mirrors manifest order
    # so error messages point at the LATER file (the new one being
    # added), not the first.
    detector_origin: dict[str, str] = {}
    merged: dict[str, list[str]] = {}

    for fname in files:
        if not isinstance(fname, str):
            raise NameHintLoaderError(
                f"{manifest_display} `files:` entry must be a string, got {fname!r}."
            )
        file_display = _child_display(target, fname)
        if not _is_file(handle, fname):
            raise NameHintLoaderError(
                f"{manifest_display} lists a file ({file_display}) that does not exist."
            )
        for det_id, terms in _read_detector_file(handle, fname, file_display).items():
            if det_id in merged:
                first = detector_origin[det_id]
                raise NameHintLoaderError(
                    f"detector_id {det_id!r} is declared in both "
                    f"{first} and {file_display}. Each detector_id may "
                    "appear in exactly one file."
                )
            merged[det_id] = terms
            detector_origin[det_id] = file_display

    return merged


def _read_detector_file(handle: DirHandle, name: str, display: str) -> dict[str, list[str]]:
    """Parse one YAML file's ``detectors:`` list into {detector_id: [terms]}.

    A file with no detectors (empty list or missing key) is permitted
    -- it's a no-op contribution. A file with an entry that lacks
    ``detector_id`` or ``patterns`` is an error.
    """
    data = _read_yaml(handle, name, display)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise NameHintLoaderError(
            f"{display} must be a YAML mapping at the top level, got {type(data).__name__}."
        )
    raw_entries = data.get("detectors")
    if raw_entries is None:
        return {}
    if not isinstance(raw_entries, list):
        raise NameHintLoaderError(
            f"{display} `detectors:` must be a list, got {type(raw_entries).__name__}."
        )

    out: dict[str, list[str]] = {}
    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise NameHintLoaderError(
                f"{display} entry #{idx} must be a mapping, got {type(entry).__name__}."
            )
        det_id = entry.get("detector_id")
        patterns = entry.get("patterns")
        if not isinstance(det_id, str) or not det_id:
            raise NameHintLoaderError(
                f"{display} entry #{idx} is missing a non-empty `detector_id:` string."
            )
        if not isinstance(patterns, list) or not patterns:
            raise NameHintLoaderError(
                f"{display} detector {det_id!r} must define a non-empty `patterns:` list."
            )
        cleaned_terms: list[str] = []
        for t_idx, term in enumerate(patterns):
            if not isinstance(term, str) or not term:
                raise NameHintLoaderError(
                    f"{display} detector {det_id!r} pattern #{t_idx} must be a "
                    f"non-empty string, got {term!r}."
                )
            cleaned_terms.append(term)
        if det_id in out:
            raise NameHintLoaderError(
                f"{display} declares detector_id {det_id!r} twice. Each "
                "detector_id may appear at most once per file."
            )
        out[det_id] = cleaned_terms
    return out


def _read_yaml(handle: DirHandle, name: str, display: str) -> Any:
    """yaml.safe_load with a clear error chain on parse failure."""
    member = handle.open_file(name)
    try:
        return serial.yaml_safe_load(member)
    except yaml.YAMLError as exc:
        raise NameHintLoaderError(f"could not parse {display}: {exc}") from exc
    finally:
        member.close()
