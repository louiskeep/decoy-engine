"""Pin the reason catalog an engine release ships: registry version, catalog digest, counts.

The pin lives at tests/fixtures/f29/reason-registry-release.json and is checked by
tests/unit/test_reason_release_pin.py on every run and by cut-release.sh before a tag:

    python scripts/release/reason_release_pin.py --check   # exit 1 unless the pin is current
    python scripts/release/reason_release_pin.py --write   # re-capture at release time

The digest is SHA-256 over the canonical JSON of `decoy_engine.reasons.manifest()` (sorted keys,
no whitespace), the same canonical-JSON-then-hash shape the engine's other pinned digests use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from packaging.version import Version

PIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "f29"
    / ("reason-registry-release.json")
)


def _digest(manifest: dict) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def release_pin(manifest: dict) -> dict:
    counts = Counter(str(e["since"]) for e in manifest["entries"])
    return {
        "registry_version": manifest["registry_version"],
        "manifest_sha256": _digest(manifest),
        "identities": len(manifest["entries"]),
        "since_counts": {k: counts[k] for k in sorted(counts, key=Version)},
    }


def pin_violations(pin: dict, live: dict) -> list[str]:
    """Why ``live`` breaks the catalog frozen by ``pin``; empty when it honours it."""
    problems: list[str] = []
    pinned, current = Version(pin["registry_version"]), Version(live["registry_version"])
    if current < pinned:
        problems.append(f"registry_version went backwards ({current} < {pinned})")
    if current == pinned and _digest(live) != pin["manifest_sha256"]:
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


def _live() -> dict:
    from decoy_engine import reasons

    return json.loads(json.dumps(reasons.manifest()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    live = _live()
    want = release_pin(live)
    if args.write:
        PIN_PATH.write_text(json.dumps(want, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {PIN_PATH.name}: registry {want['registry_version']}, {want['identities']}")
        return 0
    if not PIN_PATH.is_file():
        print(f"reason release pin missing: {PIN_PATH.name}", file=sys.stderr)
        return 1
    have = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    if have != want:
        print(
            f"reason release pin is stale: pinned {have.get('registry_version')} "
            f"({have.get('identities')} identities), live {want['registry_version']} "
            f"({want['identities']}); re-capture with --write",
            file=sys.stderr,
        )
        return 1
    print(f"reason release pin current: registry {want['registry_version']}, {want['identities']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
