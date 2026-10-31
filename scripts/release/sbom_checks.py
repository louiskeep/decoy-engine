"""Fail-closed input checks for sbom.sh (stdlib only, runs in the hashed tool environment).

``lock``: every package in the base runtime closure of a uv.lock must come from the public
PyPI index. The closure is the root project's non-dev dependencies, walked per
(name, version) because one name can be locked at several versions with different
dependencies. Dev groups and extras are out of scope, so a dev-only git or editable source
does not block the SBOM.

``pins``: the requirements file that ``uv export`` wrote must be a well-formed sequence of
``name==version [ ; marker ]`` lines, each followed by its sha256 hash lines, and nothing
else: no options, comments, URLs or paths, which would carry infrastructure text into the SBOM.
"""

from __future__ import annotations

import re
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_PUBLIC = {"registry": "https://pypi.org/simple"}

# PEP 440 version characters only; marker text limited to marker grammar characters.
_PIN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"==[A-Za-z0-9.!+_-]+"
    r"(?: ; [A-Za-z0-9 _.'\"()<>=!~*,+-]+)?"
    r"(?P<cont> \\)?$"
)
_HASH = re.compile(r"^    --hash=sha256:[0-9a-f]{64}(?P<cont> \\)?$")


def _is_root(pkg: dict) -> bool:
    src = pkg["source"]
    return src.get("virtual") == "." or src.get("editable") == "."


def runtime_closure(lock: dict) -> set[tuple[str, str]]:
    by_name: dict[str, list[dict]] = {}
    for p in lock["package"]:
        by_name.setdefault(p["name"], []).append(p)
    roots = [p for p in lock["package"] if _is_root(p)]
    if len(roots) != 1:
        raise ValueError("expected exactly one root project in the lock")
    seen: dict[tuple[str, str], dict] = {}
    stack = list(roots[0].get("dependencies", []))
    while stack:
        dep = stack.pop()
        for p in by_name.get(dep["name"], []):
            if "version" in dep and p["version"] != dep["version"]:
                continue
            key = (p["name"], p["version"])
            if key not in seen:
                seen[key] = p
                stack.extend(p.get("dependencies", []))
    return set(seen)


def lock_violations(lock_text: str) -> list[str]:
    lock = tomllib.loads(lock_text)
    pkgs = {(p["name"], p["version"]): p for p in lock["package"]}
    return sorted(f"{n} {v}" for n, v in runtime_closure(lock) if pkgs[(n, v)]["source"] != _PUBLIC)


def pin_violations(text: str, *, require_hashes: bool, allow_comments: bool = False) -> list[int]:
    """1-based numbers of the lines that break the grammar (empty list = well formed).

    A pin ending in a backslash must be followed by sha256 lines, each but the last also
    ending in a backslash; a hash line anywhere else is an orphan. With ``require_hashes``
    every pin must carry at least one hash.
    """
    bad: list[int] = []
    in_continuation = False
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for n, line in enumerate(lines, 1):
        if in_continuation:
            m = _HASH.match(line)
            if not m:
                bad.append(n)
            in_continuation = bool(m and m.group("cont"))
            continue
        if line.strip() == "":
            continue
        if allow_comments and re.match(r"^\s*#", line) and not line.endswith("\\"):
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


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"lock", "pins", "pins-with-comments"}:
        print("usage: sbom_checks.py lock|pins|pins-with-comments FILE", file=sys.stderr)
        return 2
    with open(argv[2], encoding="utf-8") as fh:
        text = fh.read()
    if argv[1] == "lock":
        try:
            bad = lock_violations(text)
        except (ValueError, KeyError, tomllib.TOMLDecodeError):
            print("sbom.sh: unreadable lock (no single root project)", file=sys.stderr)
            return 1
        if bad:
            print(
                "sbom.sh: non-public source in the runtime closure "
                f"(SBOM covers public-index pins only): {', '.join(bad)}",
                file=sys.stderr,
            )
            return 1
        return 0
    if pin_violations(
        text, require_hashes=argv[1] == "pins", allow_comments=argv[1] == "pins-with-comments"
    ):
        print(
            "sbom.sh: non-public source or unsupported requirement in the pin list "
            "(SBOM covers well-formed public-index pins only)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
