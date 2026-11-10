#!/usr/bin/env python3
"""Generate and check NOTICE, and gate the licenses of what installing the engine pulls in.

Usage: python scripts/dev/licenses.py [--write [--site DIR ...] | --check] [--verify-env]
  --write       regenerate NOTICE from uv.lock and installed package metadata. Metadata
                comes from this interpreter's environment, or from each --site directory
                (scripts/dev/licenses_write.sh builds those for every locked Python and
                platform); every scoped pin must be found, and all copies must agree
  --check       fail if NOTICE is stale against the lock or a license is not allowed
                (default; reads files only, needs no installed environment)
  --verify-env  also compare NOTICE with this environment's installed metadata (run it
                in the lock-exact venv from scripts/dev/bootstrap.sh)

Scope: the packages in uv.lock reachable from the engine's [project].dependencies
("runtime") or from an optional runtime extra (named in license_policy.json), walked by
(name, version) over every Python version and platform marker in the lock. Tooling
extras (dev, docs, lint, ...) are excluded. License text is read from package metadata
(PEP 639 License-Expression, else the License field, else the OSI classifier) and
normalized; metadata that cannot be classified fails closed unless the policy records an
override for that exact name==version after reading the package's license file.
Pattern: port of decoy-platform scripts/dev/licenses.py (pip-licenses and CycloneDX read
the same metadata fields); the lock walk follows scripts/release/sbom_checks.py.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # CPython 3.10
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = "scripts/dev/license_policy.json"
FIRST_PARTY = {"decoy-engine", "decoy-engine-native"}
FORBIDDEN = re.compile(r"AGPL|SSPL|BUSL|\bBSL\b|FSL|Commons-Clause|Elastic|(?<!L)GPL", re.I)
SEPARATOR = "== Complete list =="
RUNTIME = "runtime"
LICENSOR = "Decoy Labs LLC"


class UnclassifiedLicenseError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    version: str
    license: str
    scope: str  # "runtime" or "extra: a, b"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --- lock walk ---------------------------------------------------------------------


def _root(lock: dict) -> dict:
    roots = [
        p
        for p in lock["package"]
        if p["source"].get("editable") == "." or p["source"].get("virtual") == "."
    ]
    if len(roots) != 1:
        raise ValueError("expected exactly one root project in uv.lock")
    return roots[0]


def _walk(lock: dict, deps: list[dict], env: dict | None = None) -> set[tuple[str, str]]:
    """(name, version) pairs reachable from ``deps``; with ``env``, only edges whose
    marker holds in that environment are followed."""
    by_name: dict[str, list[dict]] = {}
    for p in lock["package"]:
        by_name.setdefault(p["name"], []).append(p)
    seen: set[tuple[str, str]] = set()
    walked: set[tuple[str, str, str | None]] = set()
    stack = list(deps)
    while stack:
        dep = stack.pop()
        if env is not None and dep.get("marker"):
            from packaging.markers import Marker

            if not Marker(dep["marker"]).evaluate(env):
                continue
        for p in by_name.get(dep["name"], []):
            if "version" in dep and p.get("version") != dep["version"]:
                continue
            key = (p["name"], p.get("version", ""))
            seen.add(key)
            for extra in (None, *dep.get("extra", [])):
                if (*key, extra) in walked:
                    continue
                walked.add((*key, extra))
                if extra is None:
                    stack.extend(p.get("dependencies", []))
                else:
                    stack.extend(p.get("optional-dependencies", {}).get(extra, []))
    return {(norm(n), v) for n, v in seen if norm(n) not in FIRST_PARTY}


def lock_scope(lock_text: str, policy: dict) -> dict[tuple[str, str], str]:
    """(name, version) -> "runtime" or "extra: <sorted extras>" for every scoped pin."""
    lock = tomllib.loads(lock_text)
    root = _root(lock)
    out = {k: RUNTIME for k in _walk(lock, root.get("dependencies", []))}
    via: dict[tuple[str, str], set[str]] = {}
    optional = root.get("optional-dependencies", {})
    for extra in policy["extras"]["runtime"]:
        for k in _walk(lock, optional.get(extra, [])):
            via.setdefault(k, set()).add(extra)
    for k, extras in via.items():
        out.setdefault(k, "extra: " + ", ".join(sorted(extras)))
    return out


def env_runtime_closure(lock_text: str) -> set[tuple[str, str]]:
    """Runtime pins whose markers hold in this interpreter (what must be installed)."""
    from packaging.markers import default_environment

    lock = tomllib.loads(lock_text)
    env = dict(default_environment())
    return _walk(lock, _root(lock).get("dependencies", []), env)


def locked_versions(lock_text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in tomllib.loads(lock_text)["package"]:
        out.setdefault(norm(p["name"]), set()).add(p.get("version", ""))
    return out


# --- classification ----------------------------------------------------------------

_RAW = {
    "mit": "MIT", "mit license": "MIT", "mit-0": "MIT-0",
    "apache-2.0": "Apache-2.0", "apache 2.0": "Apache-2.0", "apache license 2.0": "Apache-2.0",
    "apache software license 2.0": "Apache-2.0", "apache software license": "Apache-2.0",
    "bsd-3-clause": "BSD-3-Clause", "bsd 3-clause license": "BSD-3-Clause",
    "3-clause bsd license": "BSD-3-Clause", "bsd-2-clause": "BSD-2-Clause",
    "isc": "ISC", "isc license": "ISC", "unlicense": "Unlicense", "mpl-2.0": "MPL-2.0",
    "mpl-2.0 and mit": "MPL-2.0 AND MIT",
    "bsd": "BSD-family-unspecified-clause", "bsd license": "BSD-family-unspecified-clause",
}  # fmt: skip
_CLS = {
    "OSI Approved :: MIT License": "MIT",
    "OSI Approved :: BSD License": "BSD-family-unspecified-clause",
    "OSI Approved :: Apache Software License": "Apache-2.0",
    "OSI Approved :: ISC License (ISCL)": "ISC",
    "OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "OSI Approved :: GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.0-or-later",
    "OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
}  # fmt: skip


def classify(meta: dict, override: dict | None = None) -> str:
    """Normalize one distribution's license metadata; raise rather than guess."""
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    raw = (meta.get("License") or "").strip()
    if raw.lower() in _RAW:
        return _RAW[raw.lower()]
    classifiers = meta.get("Classifier", [])
    cls = [_CLS[c] for c in classifiers if c in _CLS]
    unmapped = [c for c in classifiers if c.startswith("OSI Approved ::") and c not in _CLS]
    if cls and not unmapped and (len(cls) == 1 or raw.lower() == "dual license"):
        return " OR ".join(dict.fromkeys(cls))
    # an override covers only metadata that states nothing at all
    if override and not raw and not classifiers:
        return str(override["license"])
    raise UnclassifiedLicenseError(
        f"cannot classify license metadata: License={raw[:60]!r} Classifier={classifiers}"
    )


def _meta(dist: md.Distribution) -> dict:
    m = dist.metadata
    # classifiers are stored without the "License ::" prefix to match _CLS keys
    cls = [
        c.split("::", 1)[1].strip()
        for c in m.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    return {
        "License-Expression": m.get("License-Expression"),
        "License": m.get("License"),
        "Classifier": cls,
    }


def _distributions(sites: list[str] | None) -> list[md.Distribution]:
    if sites:
        return [d for s in sites for d in md.distributions(path=[s])]
    return list(md.distributions())


def collect_entries(
    scope: dict[tuple[str, str], str], policy: dict, sites: list[str] | None = None
) -> list[Entry]:
    found: dict[tuple[str, str], set[str]] = {}
    for d in _distributions(sites):
        key = (norm(d.metadata["Name"]), d.version)
        if key in scope:
            override = policy["overrides"].get(f"{key[0]}=={key[1]}")
            try:
                lic = classify(_meta(d), override)
            except UnclassifiedLicenseError as e:
                raise UnclassifiedLicenseError(f"{key[0]}=={key[1]}: {e}") from None
            found.setdefault(key, set()).add(lic)
    missing = sorted(f"{n}=={v}" for n, v in scope if (n, v) not in found)
    if missing:
        raise UnclassifiedLicenseError(
            f"locked but not found in the metadata sources: {', '.join(missing)}"
        )
    split = sorted(f"{n}=={v}" for (n, v), lic in found.items() if len(lic) != 1)
    if split:
        raise UnclassifiedLicenseError(f"metadata differs between sources: {', '.join(split)}")
    return [Entry(n, v, next(iter(found[(n, v)])), s) for (n, v), s in sorted(scope.items())]


# --- policy ------------------------------------------------------------------------


def load_policy(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def policy_file_problems(policy: dict, pyproject_text: str) -> list[str]:
    out = [
        f"allowlist contains forbidden license {a!r}"
        for a in policy["allowed"]
        if FORBIDDEN.search(a)
    ]
    for name, exc in policy["exceptions"].items():
        if FORBIDDEN.search(exc["license"]):
            out.append(f"exception {name} uses forbidden license {exc['license']!r}")
        if not exc.get("reason", "").strip():
            out.append(f"exception {name} has no justification")
        if exc.get("scope") not in {RUNTIME, "extra"}:
            out.append(f"exception {name} has no scope (runtime or extra)")
    for key, ovr in policy["overrides"].items():
        if FORBIDDEN.search(ovr["license"]) or not ovr.get("evidence", "").strip():
            out.append(f"override {key} is forbidden or has no evidence")
    declared = set(tomllib.loads(pyproject_text)["project"].get("optional-dependencies", {}))
    runtime, tooling = set(policy["extras"]["runtime"]), set(policy["extras"]["tooling"])
    if runtime & tooling:
        out.append(f"extras classed both runtime and tooling: {sorted(runtime & tooling)}")
    if declared != runtime | tooling:
        out.append(
            "every pyproject extra must be classed runtime or tooling in the policy: "
            f"unclassed {sorted(declared - runtime - tooling)}, "
            f"unknown {sorted((runtime | tooling) - declared)}"
        )
    return out


@dataclass(frozen=True)
class Violation:
    name: str
    version: str
    license: str
    why: str


def _expr_ok(expr: str, allowed: set[str]) -> bool:
    """OR: the licensee may pick any alternative; AND: every term applies."""
    return any(all(t.strip() in allowed for t in alt.split(" AND ")) for alt in expr.split(" OR "))


def policy_violations(entries: list[Entry], policy: dict) -> list[Violation]:
    allowed = set(policy["allowed"])
    out = []
    for e in entries:
        exc = policy["exceptions"].get(e.name)
        if exc and exc["license"] == e.license:
            if exc["scope"] == "extra" and e.scope == RUNTIME:
                out.append(Violation(e.name, e.version, e.license, "excepted for extras only"))
            continue
        if not _expr_ok(e.license, allowed):
            out.append(Violation(e.name, e.version, e.license, "disallowed license"))
    return out


# --- NOTICE ------------------------------------------------------------------------


def render_notice(entries: list[Entry], policy: dict) -> str:
    by_name: dict[str, list[Entry]] = {}
    for e in entries:
        by_name.setdefault(e.name, []).append(e)
    lines = [
        "Decoy Engine",
        f"Copyright 2026 {LICENSOR}",
        "",
        f"This product includes software developed by {LICENSOR.rstrip('.')}.",
        "",
        "THIRD-PARTY NOTICES",
        "",
        "Generated by scripts/dev/licenses.py from uv.lock; do not edit by hand.",
        "Regenerate: scripts/dev/licenses_write.sh   Verify: python scripts/dev/licenses.py --check",
        "",
        "No third-party code is bundled in this distribution. The packages below are",
        "installed by pip from the Python package index, each under its own license.",
        "Scope: every package in uv.lock that installing decoy-engine can pull in: its",
        "runtime dependencies and those of the optional runtime extras",
        f"({', '.join(policy['extras']['runtime'])}),",
        "across every Python version and platform the lock covers. Development and",
        f"tooling extras ({', '.join(policy['extras']['tooling'])}) are excluded, as is the",
        "separate decoy-engine-native companion package.",
        "",
        "Named license exceptions (scripts/dev/license_policy.json). Each is used",
        "unmodified, pip-installed; vendored or modified copies are forbidden:",
        "",
    ]
    for name, exc in sorted(policy["exceptions"].items()):
        pins = [e.version for e in by_name.get(name, []) if e.license == exc["license"]]
        ver = " " + ", ".join(pins) if pins else ""
        where = "runtime" if exc["scope"] == RUNTIME else "optional"
        lines.append(f"  {name}{ver} -- {exc['license']} ({where})")
        lines += textwrap.wrap(exc["reason"], 88, initial_indent="    ", subsequent_indent="    ")
    lines += [
        "",
        SEPARATOR,
        "",
        "Format: name==version, two spaces, license, two spaces, [runtime] or",
        "[extra: <the optional extras that pull it in>].",
        "",
    ]
    lines += [f"  {e.name}=={e.version}  {e.license}  [{e.scope}]" for e in entries]
    return "\n".join(lines) + "\n"


def parse_notice(text: str) -> list[Entry]:
    body = text.split(SEPARATOR, 1)[-1]
    return [
        Entry(m.group(1), m.group(2), m.group(3), m.group(4))
        for ln in body.splitlines()
        if (m := re.match(r"^  (\S+)==(\S+)  (\S.*?)  \[([^\]]+)\]$", ln))
    ]


def notice_problems(lock_text: str, pyproject_text: str, notice: str, policy: dict) -> list[str]:
    want = lock_scope(lock_text, policy)
    entries = parse_notice(notice)
    have = {(e.name, e.version): e.scope for e in entries}
    out = [f"NOTICE missing {n}=={v}" for n, v in sorted(want) if (n, v) not in have]
    out += [
        f"NOTICE lists {n}=={v} but it is not in the lock's runtime or extras scope"
        for n, v in sorted(have)
        if (n, v) not in want
    ]
    out += [
        f"NOTICE scopes {n}=={v} as [{have[(n, v)]}] but the lock says [{s}]"
        for (n, v), s in sorted(want.items())
        if (n, v) in have and have[(n, v)] != s
    ]
    out += policy_file_problems(policy, pyproject_text)
    out += [
        f"policy exception {n} matches no scoped pin; remove it or fix its license"
        for n, exc in sorted(policy["exceptions"].items())
        if not any(e.name == n and e.license == exc["license"] for e in entries)
    ]
    if not out and render_notice(entries, policy) != notice:
        out.append("NOTICE differs from generator output (hand edit or stale policy)")
    return out


@dataclass(frozen=True)
class EnvCheck:
    drifted: list[str]  # installed versions the lock does not pin: not a lock-exact venv
    problems: list[str]
    compared: int
    required: int


def verify_env(lock_text: str, notice: str, policy: dict) -> EnvCheck:
    """Compare NOTICE with this environment's installed metadata."""
    locked = locked_versions(lock_text)
    installed = {norm(d.metadata["Name"]): d for d in md.distributions()}
    drifted = sorted(
        f"{n}=={d.version}"
        for n, d in installed.items()
        if n in locked and n not in FIRST_PARTY and d.version not in locked[n]
    )
    recorded = {(e.name, e.version): e.license for e in parse_notice(notice)}
    required = env_runtime_closure(lock_text)
    problems = [
        f"{n}=={v} is a runtime dependency here but is not installed at that version"
        for n, v in sorted(required)
        if n not in installed or installed[n].version != v
    ]
    compared = 0
    for (n, v), lic in sorted(recorded.items()):
        d = installed.get(n)
        if d is None or d.version != v:
            continue
        compared += 1
        try:
            have = classify(_meta(d), policy["overrides"].get(f"{n}=={v}"))
        except UnclassifiedLicenseError as e:
            problems.append(f"{n}=={v}: {e}")
            continue
        if have != lic:
            problems.append(f"{n}=={v}: installed metadata says {have!r}, NOTICE says {lic!r}")
    return EnvCheck(drifted, problems, compared, len(required))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--verify-env", action="store_true")
    ap.add_argument("--site", action="append", default=None)
    a = ap.parse_args(argv)
    lock_text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    policy = load_policy(ROOT / POLICY_PATH)
    notice_path = ROOT / "NOTICE"
    problems = policy_file_problems(policy, pyproject_text)
    if a.write and not problems:
        try:
            entries = collect_entries(lock_scope(lock_text, policy), policy, a.site)
        except UnclassifiedLicenseError as e:
            print(f"LICENSES FAIL: {e}", file=sys.stderr)
            return 1
        bad = policy_violations(entries, policy)
        problems += [f"{v.name}=={v.version} {v.license}: {v.why}" for v in bad]
        if not bad:
            notice_path.write_text(render_notice(entries, policy), encoding="utf-8")
            print(f"licenses: wrote NOTICE ({len(entries)} pins)")
    if not notice_path.exists():
        problems.append("NOTICE is missing; run scripts/dev/licenses_write.sh")
    elif not problems:
        notice = notice_path.read_text(encoding="utf-8")
        problems += notice_problems(lock_text, pyproject_text, notice, policy)
        problems += [
            f"{v.name}=={v.version} {v.license}: {v.why}"
            for v in policy_violations(parse_notice(notice), policy)
        ]
        if a.verify_env:
            env = verify_env(lock_text, notice, policy)
            problems += [f"not a lock-exact environment: {d}" for d in env.drifted]
            problems += env.problems
    for p in problems:
        print(f"LICENSES FAIL: {p}", file=sys.stderr)
    if problems:
        print(
            "  fix: scripts/dev/licenses_write.sh, or replace the dependency; never loosen "
            "the policy to pass",
            file=sys.stderr,
        )
        return 1
    if not a.write:
        print("licenses: NOTICE matches the lock and every license is allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
