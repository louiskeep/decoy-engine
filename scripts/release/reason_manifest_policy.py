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
from collections.abc import Callable, Iterable, Mapping
from typing import TypedDict

from packaging.version import Version

_SEMANTIC_FIELDS = ("namespace", "code", "kind", "rules", "owner", "context", "since")
SCHEMA_ROOTS = ("reason_schema_version", "event_schema_version")
# A bare X.Y.Z in a summary goes stale when a slice is renumbered. Not preceded by a word
# character or dot, not followed by a word character or ".<digit>" (so "9.9.0." ends a sentence
# while "9.9.0.1" and "9.9.0beta" are not versions).
VERSION_LITERAL = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?!\w|\.\d)")
_FIXTURE_NAME = re.compile(r"reason-manifest-v(\d+)\.(\d+)-([a-z0-9-]+)\.json")
FIXTURE_GLOB = "reason-manifest-v*-*." + "json"  # built apart: a sentry scans for .json literals


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
        if Version(since) > current:
            problems.append(f"{n} identities claim since {since}, newer than the registry")
    # Every released bucket is checked, including one that dropped to 0 in the live catalog.
    released = {k for k in pin["since_counts"] if Version(k) <= pinned}
    released |= {k for k in counts if Version(k) <= pinned}
    for since in sorted(released, key=Version):
        n, was = counts.get(since, 0), pin["since_counts"].get(since, 0)
        if n != was:
            problems.append(
                f"{n} identities claim an already-released since {since} (released with {was})"
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
        f"{root} differs ({a[root]} vs {b[root]})" for root in SCHEMA_ROOTS if a[root] != b[root]
    ]


def summary_version_violations(entries: Iterable[dict]) -> list[str]:
    """Entries whose summary holds an ``X.Y.Z`` literal, which a renumber would leave stale."""
    return [
        f"{e['namespace']}.{e['code']}: summary names a version literal"
        for e in entries
        if VERSION_LITERAL.search(e["summary"])
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
        parsed = parse_fixture_name(name)
        if (
            parsed
            and Version(parsed[0]).major == major
            and Version(parsed[0]).minor > floor_v.minor
        ):
            by_minor.setdefault(Version(parsed[0]).minor, []).append(name)
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


class RenumberFacts(TypedDict):
    """Tree and history facts the renumber script reads and the policy judges."""

    branch_owned_schema_roots: frozenset[str]
    main_moved: bool  # the branch forked from an older main than the one it is judged against
    anchor_versions: tuple[str, ...]  # every docs/reasons.md `registry_version` sentence
    contract_versions: frozenset[str]  # every registry_version in consumer-contract.json
    contract_matches: bool  # the committed contract equals the one built from this tree
    fixture_count: int  # manifest fixtures the branch added
    require_fixture: bool  # a branch with new identities must carry one (the merge gate)
    fixture: dict | None  # the branch-new manifest fixture: {"name": str, "manifest": dict}
    added_versions: tuple[tuple[str, int, str], ...]  # (path, line, X.Y.Z) on branch-added lines


def version_of(manifest: dict) -> str:
    return manifest["registry_version"]


def entries_since(manifest: dict, version: str) -> list[dict]:
    """The entries whose ``since`` is ``version``."""
    return [e for e in manifest["entries"] if e["since"] == version]


def contract_versions(node: object) -> frozenset[str]:
    """Every ``registry_version`` value anywhere inside a parsed JSON document; a value that is not
    a string is reported as ``<non-string>`` so it can never equal a version."""
    if isinstance(node, dict):
        found = {
            (v if isinstance(v, str) else "<non-string>")
            for k, v in node.items()
            if k == "registry_version"
        }
        return frozenset(found.union(*(contract_versions(v) for v in node.values())))
    if isinstance(node, list):
        return frozenset().union(*(contract_versions(v) for v in node))
    return frozenset()


def restamp(node: object, version: str) -> object:
    """``node`` with every ``registry_version`` value replaced by ``version``."""
    if isinstance(node, dict):
        return {
            k: (version if k == "registry_version" else restamp(v, version))
            for k, v in node.items()
        }
    return [restamp(v, version) for v in node] if isinstance(node, list) else node


def owned_schema_roots(
    changed: Callable[[str], bool], merge_values: Callable[[str], Iterable[tuple[str, str]]]
) -> frozenset[str]:
    """Schema roots a branch owns: changed by one of its own commits (``changed(root)``), or set by
    a merge to a value its second parent does not hold (``merge_values(root)`` yields
    ``(merge value, second parent value)`` pairs)."""
    return frozenset(
        root
        for root in SCHEMA_ROOTS
        if changed(root) or any(here != other for here, other in merge_values(root))
    )


def fixture_prefix(version: str) -> str:
    """The part of a fixture name that carries the minor (what a renumber rewrites)."""
    major, minor, _ = _version_tuple(version)
    return f"reason-manifest-v{major}.{minor}-"


def fixture_name(version: str, slug: str) -> str:
    """The pinned fixture file name for the minor of ``version``."""
    return f"{fixture_prefix(version)}{slug}." + "json"


def parse_fixture_name(name: str) -> tuple[str, str] | None:
    """``(major.minor.0, slug)`` of a fixture file name, or None when it is not one."""
    match = _FIXTURE_NAME.fullmatch(name)
    return (f"{match[1]}.{match[2]}.0", match[3]) if match else None


def renumber_target(manifest: dict) -> str:
    return next_minor(manifest["registry_version"])


def next_minor(version: str) -> str:
    major, minor, _ = _version_tuple(version)
    return f"{major}.{minor + 1}.0"


def _by_id(manifest: dict) -> dict[tuple[str, str], dict]:
    return {(e["namespace"], e["code"]): e for e in manifest["entries"]}


def renumber_claim(m: dict, h: dict) -> list[str]:
    """The distinct ``since`` values claimed by identities that ``h`` adds to main ``m``."""
    main = _by_id(m)
    return sorted({e["since"] for i, e in _by_id(h).items() if i not in main})


def renumber_classification(b: dict, m: dict, h: dict, facts: RenumberFacts) -> list[str]:
    """Refusals for renumbering branch ``h`` (forked at ``b``) onto main ``m``; each starts with its key."""
    base, main, head = _by_id(b), _by_id(m), _by_id(h)
    out: list[str] = []
    for ident, entry in main.items():
        name = f"{ident[0]}.{ident[1]}"
        if ident not in head:
            out.append(f"deleted-existing {name}")
        elif head[ident] != entry:
            both = ident not in base or base[ident] != entry
            out.append(f"{'main-and-branch-changed' if both else 'edited-existing'} {name}")
    new = {i: e for i, e in head.items() if i not in main}
    claimed = renumber_claim(m, h)
    if not claimed and h["registry_version"] != m["registry_version"]:
        out.append("version-without-identity")
    floor = Version(b["registry_version"])
    for ident, entry in new.items():
        old = Version(entry["since"])
        if old < floor or (old == floor and facts["main_moved"]):
            out.append(f"stale-since {ident[0]}.{ident[1]}")
    if len(claimed) > 1:
        out.append("several-minors")
    if any(Version(c).major != Version(m["registry_version"]).major for c in claimed):
        out.append("major-change")
    roots = {v.split(" ")[0] for v in schema_root_violations(m, h)}
    out += [f"schema-root {r}" for r in sorted(roots | set(facts["branch_owned_schema_roots"]))]
    out += [
        f"summary-version {i[0]}.{i[1]}" for i, e in new.items() if summary_version_violations([e])
    ]
    return out


def renumber_check(b: dict, m: dict, h: dict, facts: RenumberFacts) -> list[str]:
    """Why the branch head is not exactly main + 1 with every carrier agreeing; empty when it is."""
    out = renumber_classification(b, m, h, facts)
    head, version = _by_id(h), h["registry_version"]
    new = {i: e for i, e in head.items() if i not in _by_id(m)}
    want = next_minor(m["registry_version"])
    if new and version != want:
        out.append(f"version {version} is not {want}")
    out += [
        f"since {e['since']} of {i[0]}.{i[1]} is not {version}"
        for i, e in new.items()
        if e["since"] != version
    ]
    if len(facts["anchor_versions"]) != 1 or any(a != version for a in facts["anchor_versions"]):
        out.append("docs/reasons.md must hold exactly one anchor, naming the registry version")
    if not facts["contract_versions"] or any(v != version for v in facts["contract_versions"]):
        out.append("consumer-contract.json names no registry version or a different one")
    if not facts["contract_matches"]:
        out.append("consumer-contract.json differs from the contract built from this tree")
    if facts["fixture_count"] > 1:
        out.append("several manifest fixtures were added")
    elif new and not facts["fixture_count"] and facts["require_fixture"]:
        out.append("new identities carry no manifest fixture")
    fixture = facts["fixture"]
    if fixture:
        parsed = parse_fixture_name(fixture["name"])
        if not parsed or parsed[0] != version:
            out.append(f"fixture {fixture['name']} is not named for {version}")
        out += _fixture_violations(fixture["name"], fixture["manifest"], version, head)
    base_v, head_v = Version(b["registry_version"]), Version(version)
    out += [
        f"{path}:{line} names leftover version {v}"
        for path, line, v in facts["added_versions"]
        if base_v < Version(v) < head_v
    ]
    return out
