"""The one place the reason-manifest rules live.

Pure functions over `decoy_engine.reasons.manifest()`-shaped dicts. Standard library plus
`packaging` only, never `decoy_engine`, so every release, renumber and test caller can load it from
a file path. Every comparison of two manifests (append-only, release pin, `since` floors and
windows, schema roots, summary versions, one fixture per minor) and every pending-registry decision
(allowed version, accepted pending edits, release transitions, fragment claims) lives here; callers
adapt and never compare (tests/sentry/test_reason_policy_single_source.py).

Pending declarations follow Towncrier and Scriv (a change records itself, the release names the
version) and Rust's `StableSince::Current`; the v2 release pin keeps one SHA-256 per manifest field
over canonical JSON. Messages carry identities, field names and versions, never catalog text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import NamedTuple, TypedDict

from packaging.version import Version

_SEMANTIC_FIELDS = ("namespace", "code", "kind", "rules", "owner", "context", "since")
SCHEMA_ROOTS = ("reason_schema_version", "event_schema_version")
# A bare X.Y.Z in a summary goes stale when a slice is renumbered. No word character or dot before
# it, no word character or ".<digit>" after ("9.9.0." ends a sentence; "9.9.0.1" is no version).
VERSION_LITERAL = re.compile(r"(?<![\w.])\d+\.\d+\.\d+(?!\w|\.\d)")
_FIXTURE_NAME = re.compile(r"reason-manifest-v(\d+)\.(\d+)-([a-z0-9-]+)\.json")
FIXTURE_GLOB = "reason-manifest-v*-*." + "json"  # built apart: a sentry scans for .json literals
FIXTURE_FLOOR = "1.5.0"  # fixed: the last minor that shipped without a fixture

UNRELEASED = "unreleased"  # decoy_engine.reasons.UNRELEASED: a `since` no release stamped yet
LAST_PER_PR_MINOR = "1.24.0"  # the last minor the old per-PR process made; not a moving version
_FIX = "; run scripts/dev/registry_renumber.py --to-unreleased"  # the fix for a numeric bump
PENDING_ARGUMENTS = ("since", "deprecated_in")  # catalog arguments that may be UNRELEASED
# The two accepted pending edits to a released reason and the fields each may change before the
# release stamp. Every pin, stamp and renumber decision reads this one descriptor.
PENDING_EDITS = {"summary": ("summary",), "deprecation": ("deprecated_in", "replaced_by")}
PIN_FIELDS = tuple(sorted((*_SEMANTIC_FIELDS, "symbol", "summary", "deprecated_in", "replaced_by")))
_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return (major, minor, patch)


def field_digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


_NULL = field_digest(None)


def is_pending(value: object) -> bool:
    return value == UNRELEASED


def _ident(entry: dict) -> tuple[str, str]:
    return (entry["namespace"], entry["code"])


def append_only_violations(pinned: dict, live: dict, marked: frozenset = frozenset()) -> list[str]:
    """Why ``live`` is not an append-only successor of ``pinned``; empty when it is. A marked
    summary and an UNRELEASED identity wait for the release stamp, so neither needs a bump."""
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
        if new["summary"] != old["summary"] and ident not in marked:
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
    numeric = {i for i, e in live_by_id.items() if not is_pending(e["since"])}
    added = numeric - {(e["namespace"], e["code"]) for e in pinned["entries"]}
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


def legacy_v1_violations(pin: dict, live: dict) -> list[str]:
    """Why ``live`` breaks the retired v1 aggregate pin; only the v2 migration preflight runs it."""
    problems: list[str] = []
    pinned, current = Version(pin["registry_version"]), Version(live["registry_version"])
    if current < pinned:
        problems.append(f"registry_version went backwards ({current} < {pinned})")
    if current == pinned and field_digest(live) != pin["manifest_sha256"]:
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
        if (e["namespace"], e["code"]) not in pinned_ids
        and not is_pending(e["since"])
        and Version(e["since"]) <= floor
    ]


def since_window_violations(items: Iterable[tuple[str, str]], pinned: str, live: str) -> list[str]:
    """``(name, since)`` pairs that claim a released `since` or a version above the live registry's.

    Several unreleased versions can be live in the chain (each slice takes its own minor), so any
    `since` in (pinned, live] is valid. An UNRELEASED `since` has no version to place yet.
    """
    return [
        f"{name}: since {since}"
        for name, since in items
        if not is_pending(since)
        and (Version(since) <= Version(pinned) or Version(since) > Version(live))
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


def fixture_entry_view(entry: dict) -> dict:
    """The fields a historical fixture freezes; the v2 release pin freezes the other three."""
    return {k: v for k, v in entry.items() if k not in ("summary", *PENDING_EDITS["deprecation"])}


def _fixture_violations(
    name: str, fixture: dict, want: str, live_by_id: dict, view: Callable = fixture_entry_view
) -> list[str]:
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
        if view(ids[i]) != view(live_by_id[i])
    ]
    return problems


def minor_fixture_violations(
    fixtures: Mapping[str, dict], live: dict, floor: str, full: bool = False
) -> list[str]:
    """One complete fixture per minor above ``floor`` up to the live registry minor.

    ``fixtures`` maps a fixture file name to its parsed manifest. For each minor ``m`` with
    floor < m <= live, exactly one fixture is named for it, is stamped ``{major}.{m}.0``, holds
    exactly the live identities whose ``since`` is that version, and its entries equal the live
    entries through ``fixture_entry_view`` (whole entries when ``full``, the v1 migration). No live
    identity claims a ``since`` above the floor outside such a minor (UNRELEASED claims none), and
    the highest fixture minor is the live minor."""
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
    live_by_id = {_ident(e): e for e in live["entries"] if not is_pending(e["since"])}
    view = (lambda e: e) if full else fixture_entry_view
    problems: list[str] = []
    for minor in range(floor_v.minor + 1, live_v.minor + 1):
        names = by_minor.get(minor, [])
        if len(names) != 1:
            problems.append(f"minor {major}.{minor} has {len(names)} fixtures (want exactly 1)")
        else:
            problems += _fixture_violations(
                names[0], fixtures[names[0]], f"{major}.{minor}.0", live_by_id, view
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
    # The head's PENDING_SUMMARY_EDITS when accepted pending edits are left to the release pin
    # (the pending-era --check); absent or None, any edit to a main identity is refused.
    pending_summaries: frozenset[tuple[str, str]] | None


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
    """The distinct numeric ``since`` values claimed by identities that ``h`` adds to main ``m``."""
    main = _by_id(m)
    return sorted(
        {e["since"] for i, e in _by_id(h).items() if i not in main and not is_pending(e["since"])}
    )


def _waivable(first: bool, new: dict, marked: bool) -> set[str]:
    """Fields PENDING_EDITS let ``new`` change; ``first``: both deprecation fields were null."""
    out = set(PENDING_EDITS["summary"]) if marked else set()
    if first and is_pending(new["deprecated_in"]) and new["replaced_by"] is not None:
        out |= set(PENDING_EDITS["deprecation"])
    return out


def _only_pending_edits(old: dict, new: dict, marked: bool) -> bool:
    first = old["deprecated_in"] is None and old["replaced_by"] is None
    return {k for k in old if old[k] != new[k]} <= _waivable(first, new, marked)


def renumber_classification(b: dict, m: dict, h: dict, facts: RenumberFacts) -> list[str]:
    """Refusals for renumbering branch ``h`` (forked at ``b``) onto main ``m``; each starts with its key."""
    base, main, head = _by_id(b), _by_id(m), _by_id(h)
    marked = facts.get("pending_summaries")
    out: list[str] = []
    for ident, entry in main.items():
        name = f"{ident[0]}.{ident[1]}"
        if ident not in head:
            out.append(f"deleted-existing {name}")
        elif marked is not None and _only_pending_edits(entry, head[ident], ident in marked):
            continue
        elif head[ident] != entry:
            both = ident not in base or base[ident] != entry
            out.append(f"{'main-and-branch-changed' if both else 'edited-existing'} {name}")
    new = {i: e for i, e in head.items() if i not in main}
    claimed = renumber_claim(m, h)
    if not claimed and h["registry_version"] != m["registry_version"]:
        out.append("version-without-identity")
    floor = Version(b["registry_version"])
    for ident, entry in new.items():
        if is_pending(entry["since"]):
            continue
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
    new = {i: e for i, e in head.items() if i not in _by_id(m) and not is_pending(e["since"])}
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


def numeric_entries(entries: Iterable[dict]) -> list[dict]:
    return [e for e in entries if not is_pending(e["since"])]


def allowed_registry_version(pin: Mapping) -> str:
    return max(pin["registry_version"], LAST_PER_PR_MINOR, key=Version)


def unreleased_violations(live: dict, pin: Mapping, fixture_names: Iterable[str] = ()) -> list[str]:
    allowed = Version(allowed_registry_version(pin))
    out = [f"registry_version {live['registry_version']} is not the allowed {allowed}{_FIX}"] * (
        Version(live["registry_version"]) != allowed
    )
    out += [
        f"{e['namespace']}.{e['code']}: since {e['since']} is above the allowed {allowed}{_FIX}"
        for e in live["entries"]
        if not is_pending(e["since"]) and Version(e["since"]) > allowed
    ]
    parsed = [(n, parse_fixture_name(n)) for n in fixture_names]
    return out + [
        f"fixture {n} claims a minor above {allowed}"
        for n, p in parsed
        if p and Version(p[0]) > Version(f"{allowed.major}.{allowed.minor}.0")
    ]


def release_pin_violations(pin: dict, live: dict, marked: frozenset = frozenset()) -> list[str]:
    """Why ``live`` breaks a well-formed v2 pin (``reason_release_pin.load_pin``): drift in a field
    of a numeric identity outside a declared pending edit, a numeric identity outside the
    baseline, or numeric work above the ceiling."""
    problems, live_by, pinned = unreleased_violations(live, pin), _by_id(live), {}
    for rec in pin["identities"]:
        ident, fields = (rec["namespace"], rec["code"]), rec["fields"]
        entry = live_by.get(ident)
        if entry is None:
            problems.append(f"{ident[0]}.{ident[1]}: pinned identity is not live")
            continue
        first = all(fields[f] == _NULL for f in PENDING_EDITS["deprecation"])
        pinned[ident], waived = entry, _waivable(first, entry, ident in marked)
        problems += [
            f"{ident[0]}.{ident[1]}: {f} differs from the release pin"
            for f in PIN_FIELDS
            if f not in waived and field_digest(entry[f]) != fields[f]
        ]
    problems += [
        f"{i[0]}.{i[1]}: numeric identity is absent from the release pin"
        for i, e in sorted(live_by.items())
        if i not in pinned and not is_pending(e["since"])
    ]
    problems += [
        f"{i[0]}.{i[1]}: summary marker names no released identity"
        for i in sorted(marked)
        if i not in pinned
    ]
    counts = Counter(e["since"] for e in pinned.values())
    if dict(counts) != pin["since_counts"]:
        problems.append("since_counts does not count the pinned identities")
    numeric = [s for s in counts if not is_pending(s)]
    if numeric and max(numeric, key=Version) != pin["baseline_ceiling"]:
        problems.append("baseline_ceiling is not the greatest pinned since")
    return problems


class Transition(NamedTuple):
    kind: str  # "minor", "patch" or "none"
    version: str  # the registry version the release ships
    writes_fixture: bool
    pending_identities: tuple[tuple[str, str], ...]
    pending_deprecations: tuple[tuple[str, str], ...]
    pending_summaries: tuple[tuple[str, str], ...]


def registry_transition(live: dict, pin: Mapping, pending_summaries: frozenset) -> Transition:
    """The one release decision: new identities take the next minor, edits alone the next patch."""
    entries = sorted(live["entries"], key=_ident)
    ids = tuple(_ident(e) for e in entries if is_pending(e["since"]))
    deps = tuple(_ident(e) for e in numeric_entries(entries) if is_pending(e["deprecated_in"]))
    marks = tuple(sorted(pending_summaries))
    a = Version(allowed_registry_version(pin))
    kind, version = ("none", str(a))
    if deps or marks:
        kind, version = "patch", f"{a.major}.{a.minor}.{a.micro + 1}"
    if ids:
        kind, version = "minor", f"{a.major}.{a.minor + 1}.0"
    return Transition(kind, version, kind == "minor", ids, deps, marks)


def registry_minor_violations(t: Transition, requested: str) -> list[str]:
    ok = requested == "auto" or (t.kind == "minor" and requested == t.version)
    want = t.version if t.kind == "minor" else "auto (this release adds no identity)"
    return [] if ok else [f"--registry-minor {requested} is not the automatic {want}"]


def release_ready_violations(live: dict, pending_summaries: frozenset) -> list[str]:
    t = registry_transition(live, {"registry_version": "0.0.0"}, pending_summaries)
    return (
        [f"{n}.{c}: since is still unreleased" for n, c in t.pending_identities]
        + [f"{n}.{c}: deprecated_in is still unreleased" for n, c in t.pending_deprecations]
        + [f"{n}.{c}: summary correction still pending" for n, c in t.pending_summaries]
    )


def stamped_manifest(live: dict, t: Transition) -> dict:
    stamp = {f: t.version for f in PENDING_ARGUMENTS}
    entries = [
        {**e, **{f: v for f, v in stamp.items() if is_pending(e[f])}} for e in live["entries"]
    ]
    return {**live, "registry_version": t.version, "entries": entries}


def release_fixture_slug(package_version: str) -> str:
    slug = "release-" + re.sub(r"[^a-z0-9]+", "-", package_version.lower()).strip("-")
    if parse_fixture_name(fixture_name("1.0.0", slug)) != ("1.0.0", slug):
        raise ValueError("the package version gives no fixture slug")
    return slug


def pending_fragment_violations(live: dict, blocks: Iterable[str]) -> list[str]:
    """UNRELEASED identities no fragment names by an exact `namespace/code` code span."""
    claims = {span for block in blocks for span in _CODE_SPAN.findall(block)}
    return [
        f"{e['namespace']}.{e['code']}: no changelog fragment names it"
        for e in sorted(live["entries"], key=_ident)
        if is_pending(e["since"]) and f"{e['namespace']}/{e['code']}" not in claims
    ]


def release_postcondition_violations(t: Transition, stamped: dict, cand: dict) -> list[str]:
    """Stamp postconditions on facts read back from the candidate tree (``cand``)."""
    out = [] if cand["manifest"] == stamped else ["the candidate manifest is not the stamped one"]
    out += [] if cand["pin"]["registry_version"] == t.version else ["the pin is not at the release"]
    if contract_versions(cand["contract"]) != {t.version}:
        out.append("consumer-contract.json names another registry version")
    if tuple(cand["anchors"]) != (t.version,):
        out.append("docs/reasons.md must name the stamped registry version once")
    return out + ["changelog fragments are left"] * bool(cand["fragments"])


def pending_era_violations(m: dict, h: dict, facts: RenumberFacts) -> list[str]:
    """Why branch ``h`` is not a pending-era change to main ``m`` (the ``--check`` gate)."""
    bump = h["registry_version"] != m["registry_version"]
    out = [f"registry-version-bump {h['registry_version']}"] * bump
    out += ["branch-added-fixture"] * bool(facts["fixture_count"])
    main = _by_id(m)
    return out + [
        f"numeric-since {i[0]}.{i[1]}"
        for i, e in sorted(_by_id(h).items())
        if i not in main and not is_pending(e["since"])
    ]
