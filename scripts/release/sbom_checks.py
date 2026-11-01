"""Fail-closed checks for sbom.sh, run with ``python -I`` in the SBOM tool environment.

Modes (each exits 1 with a registered message on refusal):

``pins-with-comments FILE``  the tool requirements file, checked with the bare tool
    interpreter before anything is installed: hashed pins and whole-line comments only.
``lock LOCK``  every package in the base runtime closure of a uv.lock comes from the public
    PyPI index. The closure is the root project's non-dev dependencies, walked by
    (name, version, source): uv.lock may hold several entries for one name, or even one
    (name, version), and a dependency edge that names a version or a source selects only
    the matching entries, so a non-public entry cannot hide behind a public twin. Requested
    extras pull in that package's optional dependencies. Dev groups and the root's own
    extras are out of scope, so a dev-only git or editable source does not block the SBOM.
    A runtime package without a version (git or path source) is a non-public source.
``pins FILE``  the requirements file ``uv export`` wrote: well-formed hashed
    ``name==version [ ; marker ]`` lines only, each re-parsed with
    ``packaging.requirements.Requirement`` (the parser ``cyclonedx-py`` relies on).
``complete LOCK FILE SBOM``  the SBOM's components equal the exported pins as a multiset
    of (PEP 503 name, PEP 440 version), the root component is present, and every exported
    pin lies in the checked runtime closure: a pin the tool dropped, merged or invented, or
    a package whose source was never checked, is a hard failure.

The grammar is a strict subset of what pip and ``pip-requirements-parser`` accept, so the
checker and the tool never disagree about where a line starts or what it says: the file is
printable ASCII plus ``\\n`` (``str.splitlines()`` also splits on ``\\r``, ``\\x0b``,
``\\x0c``, ``\\x1c``-``\\x1e``, ``\\x85``, ``\\u2028`` and ``\\u2029``), and versions are
canonical PEP 440 (regex from PEP 440, Appendix B). Whatever still parses differently is
caught by ``complete``.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_PUBLIC = {"registry": "https://pypi.org/simple"}

# PEP 440 Appendix B, canonical public version, plus a canonical (lowercase, dot) local part.
_VERSION = (
    r"(?:[1-9][0-9]*!)?(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?(?:\.post(?:0|[1-9][0-9]*))?(?:\.dev(?:0|[1-9][0-9]*))?"
    r"(?:\+[a-z0-9]+(?:\.[a-z0-9]+)*)?"
)
_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    rf"==(?P<version>{_VERSION})"
    r"(?: ; [A-Za-z0-9 _.'\"()<>=!~*,+-]+)?"
    r"(?P<cont> \\)?$"
)
_HASH = re.compile(r"^    --hash=sha256:[0-9a-f]{64}(?P<cont> \\)?$")
_COMMENT = re.compile(r"^ *#")
_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")

GRAMMAR = (
    "non-public source or unsupported requirement in the pin list "
    "(SBOM covers well-formed public-index pins only)"
)
TOOL_GRAMMAR = "tool environment install failed: sbom-tool-requirements.txt is not hashed pins only"


def canonical_name(name: str) -> str:
    """PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- lock: runtime closure and source rule ------------------------------------------


def _is_root(pkg: dict) -> bool:
    src = pkg["source"]
    return src.get("virtual") == "." or src.get("editable") == "."


def _source(src: dict) -> str:
    return json.dumps(src, sort_keys=True)


def _closure_packages(lock: dict) -> dict[tuple[str, str | None, str], dict]:
    """(name, version or None, source) -> package, for the base runtime closure."""
    by_name: dict[str, list[dict]] = {}
    for p in lock["package"]:
        by_name.setdefault(p["name"], []).append(p)
    roots = [p for p in lock["package"] if _is_root(p)]
    if len(roots) != 1:
        raise ValueError("expected exactly one root project in the lock")
    seen: dict[tuple[str, str | None, str], dict] = {}
    walked: set[tuple[tuple[str, str | None, str], str | None]] = set()
    stack = list(roots[0].get("dependencies", []))
    while stack:
        dep = stack.pop()
        for p in by_name.get(dep["name"], []):
            if "version" in dep and p.get("version") != dep["version"]:
                continue
            if "source" in dep and p["source"] != dep["source"]:
                continue
            key = (p["name"], p.get("version"), _source(p["source"]))
            seen[key] = p
            for extra in (None, *dep.get("extra", [])):
                if (key, extra) in walked:
                    continue
                walked.add((key, extra))
                if extra is None:
                    stack.extend(p.get("dependencies", []))
                else:
                    stack.extend(p.get("optional-dependencies", {}).get(extra, []))
    return seen


def runtime_closure(lock: dict) -> set[tuple[str, str | None]]:
    return {(n, v) for n, v, _ in _closure_packages(lock)}


def lock_violations(lock_text: str) -> list[str]:
    closure = _closure_packages(tomllib.loads(lock_text))
    bad = {
        f"{n} {v}" if v else n
        for (n, v, _), p in closure.items()
        if v is None or p["source"] != _PUBLIC
    }
    return sorted(bad)


# --- pins: grammar, Requirement re-parse, completeness --------------------------------


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def pin_violations(text: str, *, require_hashes: bool, allow_comments: bool = False) -> list[int]:
    """1-based numbers of the lines that break the grammar (empty list = well formed).

    Any character outside printable ASCII is a violation, so ``\\r`` (and CRLF) is refused
    here exactly as on the script path, which reads the raw bytes. A pin ending in a
    backslash must be followed by sha256 lines, each but the last also ending in a
    backslash; a hash line anywhere else is an orphan. With ``require_hashes`` every pin
    must carry at least one hash.
    """
    bad: list[int] = []
    in_continuation = False
    lines = _lines(text)
    for n, line in enumerate(lines, 1):
        if not _PRINTABLE.match(line):
            bad.append(n)
            in_continuation = False
            continue
        if in_continuation:
            m = _HASH.match(line)
            if not m:
                bad.append(n)
            in_continuation = bool(m and m.group("cont"))
            continue
        if line.strip(" ") == "":
            continue
        if allow_comments and _COMMENT.match(line) and not line.endswith("\\"):
            continue
        m = _PIN.match(line)
        if not m or "--" in line:
            bad.append(n)
            continue
        in_continuation = bool(m.group("cont"))
        if require_hashes and not in_continuation:
            bad.append(n)
    if in_continuation:
        bad.append(len(lines))
    return sorted(set(bad))


def pins(text: str) -> list[tuple[str, str, str]]:
    """(name, version, requirement text) per pin line of a well-formed file."""
    out = []
    for line in _lines(text):
        m = _PIN.match(line)
        if m:
            req = line[: -len(m.group("cont"))] if m.group("cont") else line
            out.append((m.group("name"), m.group("version"), req))
    return out


def requirement_violations(text: str) -> list[str]:
    """Pins that ``packaging`` does not read as exactly ``name==version [; marker]``."""
    from packaging.requirements import InvalidRequirement, Requirement

    bad = []
    for name, version, line in pins(text):
        try:
            req = Requirement(line)
        except InvalidRequirement:
            bad.append(line)
            continue
        spec = list(req.specifier)
        if (
            req.name != name
            or req.url
            or req.extras
            or len(spec) != 1
            or spec[0].operator != "=="
            or spec[0].version != version
        ):
            bad.append(line)
    return bad


def _key(name: str, version: str | None) -> tuple[str, object]:
    from packaging.version import InvalidVersion, Version

    try:
        return canonical_name(name), Version(version or "")
    except InvalidVersion:
        return canonical_name(name), f"<unparsed {version!r}>"


def completeness_violations(text: str, sbom: dict) -> list[str]:
    want = Counter(_key(n, v) for n, v, _ in pins(text))
    have = Counter(_key(c.get("name", ""), c.get("version")) for c in sbom.get("components", []))
    diff = [f"{n} {v}" for n, v in sorted({*(want - have), *(have - want)}, key=str)]
    if not (sbom.get("metadata", {}).get("component") or {}).get("name"):
        diff.append("<root component>")
    return diff


def uncovered_pins(lock_text: str, text: str) -> list[str]:
    """Exported pins outside the runtime closure whose sources ``lock`` checked."""
    closure = {_key(n, v) for n, v in runtime_closure(tomllib.loads(lock_text)) if v}
    return sorted(f"{n} {v}" for n, v, _ in pins(text) if _key(n, v) not in closure)


# --- entry point ----------------------------------------------------------------------


def _read(path: str) -> str | None:
    # Raw bytes: text mode would translate CRLF and lone CR before the check sees them.
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        return data.decode("ascii")
    except UnicodeDecodeError:
        return None


def _refuse(message: str) -> int:
    print(f"sbom.sh: {message}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    mode, args = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    arity = {"pins-with-comments": 1, "lock": 1, "pins": 1, "complete": 3}
    if arity.get(mode) != len(args):
        print(
            "usage: sbom_checks.py pins-with-comments|lock|pins FILE | complete LOCK PINS SBOM",
            file=sys.stderr,
        )
        return 2
    if mode == "lock":
        with open(args[0], encoding="utf-8") as fh:
            lock_text = fh.read()
        try:
            bad = lock_violations(lock_text)
        except (ValueError, KeyError, tomllib.TOMLDecodeError):
            return _refuse("unreadable lock (no single root project)")
        if bad:
            return _refuse(
                "non-public source in the runtime closure "
                f"(SBOM covers public-index pins only): {', '.join(bad)}"
            )
        return 0
    if mode == "pins-with-comments":
        text = _read(args[0])
        if text is None or pin_violations(text, require_hashes=True, allow_comments=True):
            return _refuse(TOOL_GRAMMAR)
        return 0
    if mode == "pins":
        text = _read(args[0])
        if (
            text is None
            or pin_violations(text, require_hashes=True)
            or requirement_violations(text)
        ):
            return _refuse(GRAMMAR)
        return 0
    with open(args[0], encoding="utf-8") as fh:
        lock_text = fh.read()
    text = _read(args[1])
    with open(args[2], encoding="utf-8") as fh:
        sbom = json.load(fh)
    if text is None or uncovered_pins(lock_text, text) or completeness_violations(text, sbom):
        return _refuse(
            "SBOM does not match the runtime closure (a pin was dropped, merged or added, "
            "or its source was not checked)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
