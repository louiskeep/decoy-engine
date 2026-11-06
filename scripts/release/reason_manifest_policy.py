"""The one place the reason-manifest rules live.

Pure functions over `decoy_engine.reasons.manifest()`-shaped dicts. Standard library plus
`packaging` only, and never an import of `decoy_engine`, so the release pin script, the registry
tests, the fixture sentry and the renumber tool can all load it from a file path or a test.
Every comparison of two manifests (append-only, release pin, `since` floors and windows, schema
roots, summary versions, one fixture per minor) is defined here. Consumers bind or adapt these
functions and hold no comparison logic of their own
(tests/sentry/test_reason_policy_single_source.py enforces that).

Messages carry identities, field names and versions only, never summaries or other catalog text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping

from packaging.version import Version

_SEMANTIC_FIELDS = ("namespace", "code", "kind", "rules", "owner", "context", "since")
_SCHEMA_ROOTS = ("reason_schema_version", "event_schema_version")
# A bare X.Y.Z in a summary goes stale when a slice is renumbered. Not preceded by a word
# character or dot, not followed by a word character or ".<digit>" (so "9.9.0." ends a sentence
# while "9.9.0.1" and "9.9.0beta" are not versions).
_SUMMARY_VERSION = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?!\w|\.\d)")
_FIXTURE_NAME = re.compile(r"reason-manifest-v(\d+)\.(\d+)-")


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return (major, minor, patch)


def canonical_digest(manifest: dict) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def append_only_violations(pinned: dict, live: dict) -> list[str]:
    """Why ``live`` is not an append-only successor of ``pinned``; empty when it is."""
    problems: list[str] = []
    live_by_id = {(e["namespace"], e["code"]): e for e in live["entries"]}
    pinned_version = _version_tuple(pinned["registry_version"])
    live_version = _version_tuple(live["registry_version"])
    if live_version < pinned_version:
        problems.append("registry_version went backwards")
    if live["reason_schema_version"] < pinned["reason_schema_version"]:
        problems.append("reason_schema_version went backwards")
    summary_changed = False
    for old in pinned["entries"]:
        ident = (old["namespace"], old["code"])
        new = live_by_id.get(ident)
        if new is None:
            problems.append(f"deleted identity {ident}")
            continue
        for field in _SEMANTIC_FIELDS:
            if new[field] != old[field]:
                problems.append(
                    f"{ident}: {field} changed (retry/outcome/phase semantics are frozen)"
                )
        if new["summary"] != old["summary"]:
            summary_changed = True
        if new["symbol"] != old["symbol"]:
            problems.append(f"{ident}: symbol changed")
        if old["deprecated_in"] is not None and new["deprecated_in"] != old["deprecated_in"]:
            problems.append(f"{ident}: tombstone changed")
    if (
        summary_changed
        and live_version[:2] == pinned_version[:2]
        and live_version == pinned_version
    ):
        problems.append("a fixed safe summary changed without a registry version bump")
    added = set(live_by_id) - {(e["namespace"], e["code"]) for e in pinned["entries"]}
    if added and live_version[:2] == pinned_version[:2] and live_version[2] == pinned_version[2]:
        problems.append("identities were added without a registry minor version bump")
    for entry in live["entries"]:
        link = entry["replaced_by"]
        if link is not None and entry["deprecated_in"] is None:
            problems.append(
                f"{entry['namespace']}.{entry['code']}: replacement without deprecation"
            )
        if link is not None and tuple(link) not in live_by_id:
            problems.append(
                f"{entry['namespace']}.{entry['code']}: replacement {tuple(link)} is not an identity"
            )
    return problems


def release_pin_violations(pin: dict, live: dict) -> list[str]:
    """Why ``live`` breaks the catalog frozen by ``pin``; empty when it honours it."""
    problems: list[str] = []
    pinned, current = Version(pin["registry_version"]), Version(live["registry_version"])
    if current < pinned:
        problems.append(f"registry_version went backwards ({current} < {pinned})")
    if current == pinned and canonical_digest(live) != pin["manifest_sha256"]:
        problems.append(f"the catalog released as {pinned} changed without a registry version bump")
    counts = Counter(str(e["since"]) for e in live["entries"])
    for since, n in counts.items():
        v = Version(since)
        if v > current:
            problems.append(f"{n} identities claim since {since}, newer than the registry")
        elif v <= pinned and n != pin["since_counts"].get(since, 0):
            problems.append(
                f"{n} identities claim an already-released since {since} "
                f"(released with {pin['since_counts'].get(since, 0)})"
            )
    return problems


def since_floor_violations(v1_pin: dict, live: dict) -> list[str]:
    """Identities absent from the v1 pin whose ``since`` still claims the v1 version or older."""
    pinned_ids = {(e["namespace"], e["code"]) for e in v1_pin["entries"]}
    floor = Version(v1_pin["registry_version"])
    return [
        f"{e['namespace']}.{e['code']}: new since the v1 pin but claims since {e['since']}"
        for e in live["entries"]
        if (e["namespace"], e["code"]) not in pinned_ids and Version(e["since"]) <= floor
    ]


def since_window_violations(items: Iterable[tuple[str, str]], pinned: str, live: str) -> list[str]:
    """``(name, since)`` pairs that claim a released `since` or a version above the live registry's.

    Several unreleased versions can be live in the chain (each slice takes its own minor), so any
    `since` in (pinned, live] is valid.
    """
    return [
        f"{name}: since {since}"
        for name, since in items
        if Version(since) <= Version(pinned) or Version(since) > Version(live)
    ]


def schema_root_violations(a: dict, b: dict) -> list[str]:
    """Schema roots (reason, event) that differ between two manifests, one line per root."""
    return [
        f"{root} differs ({a[root]} vs {b[root]})" for root in _SCHEMA_ROOTS if a[root] != b[root]
    ]


def summary_version_violations(entries: Iterable[dict]) -> list[str]:
    """Entries whose summary holds an ``X.Y.Z`` literal, which a renumber would leave stale."""
    return [
        f"{e['namespace']}.{e['code']}: summary names a version literal"
        for e in entries
        if _SUMMARY_VERSION.search(e["summary"])
    ]


def _fixture_violations(name: str, fixture: dict, want: str, live_by_id: dict) -> list[str]:
    """One minor's fixture against the live identities whose ``since`` is ``want``."""
    problems = []
    if fixture["registry_version"] != want:
        problems.append(f"{name}: registry_version is not {want}")
    ids = {(e["namespace"], e["code"]): e for e in fixture["entries"]}
    live_ids = {i for i, e in live_by_id.items() if e["since"] == want}
    problems += [
        f"{name}: since {e['since']} is not {want}" for e in ids.values() if e["since"] != want
    ]
    problems += [
        f"{name}: live identity {i[0]}.{i[1]} missing" for i in sorted(live_ids - set(ids))
    ]
    problems += [
        f"{name}: identity {i[0]}.{i[1]} is not live at {want}" for i in sorted(set(ids) - live_ids)
    ]
    problems += [
        f"{name}: entry {i[0]}.{i[1]} differs from the live entry"
        for i in sorted(set(ids) & live_ids)
        if ids[i] != live_by_id[i]
    ]
    return problems


def minor_fixture_violations(fixtures: Mapping[str, dict], live: dict, floor: str) -> list[str]:
    """One complete fixture per minor above ``floor`` up to the live registry minor.

    ``fixtures`` maps a fixture file name to its parsed manifest. For each minor ``m`` with
    floor < m <= live, exactly one fixture is named for it, is stamped ``{major}.{m}.0``, holds
    exactly the live identities whose ``since`` is that version, and its entries equal the live
    entries. No live identity claims a ``since`` above the floor that is not such a minor, and
    the highest fixture minor is the live minor.
    """
    floor_v, live_v = Version(floor), Version(live["registry_version"])
    major = floor_v.major
    if live_v.major != major:
        return [f"registry major {live_v.major} differs from the fixture floor major {major}"]
    by_minor: dict[int, list[str]] = {}
    for name in sorted(fixtures):
        m = _FIXTURE_NAME.match(name)
        if m and int(m[1]) == major and int(m[2]) > floor_v.minor:
            by_minor.setdefault(int(m[2]), []).append(name)
    live_by_id = {(e["namespace"], e["code"]): e for e in live["entries"]}
    problems: list[str] = []
    for minor in range(floor_v.minor + 1, live_v.minor + 1):
        names = by_minor.get(minor, [])
        if len(names) != 1:
            problems.append(f"minor {major}.{minor} has {len(names)} fixtures (want exactly 1)")
        else:
            problems += _fixture_violations(
                names[0], fixtures[names[0]], f"{major}.{minor}.0", live_by_id
            )
    for (ns, code), entry in live_by_id.items():
        since = Version(entry["since"])
        in_slot = since.major == major and since.micro == 0 and since.minor <= live_v.minor
        if since > floor_v and not in_slot:
            problems.append(f"{ns}.{code}: since {entry['since']} has no fixture slot")
    if by_minor and max(by_minor) != live_v.minor:
        problems.append(f"highest fixture minor {major}.{max(by_minor)} is not the registry minor")
    return problems
