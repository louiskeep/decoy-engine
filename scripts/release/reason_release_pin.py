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
import json
import sys
from collections import Counter
from pathlib import Path

from packaging.version import Version

# Scripts run by file path get their own directory on sys.path, not the repo root.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.release.reason_manifest_policy import (  # noqa: E402
    canonical_digest,
    release_pin_violations,
)

pin_violations = release_pin_violations  # cut-release.sh and the tests import this name

PIN_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "f29"
    / ("reason-registry-release.json")
)


def release_pin(manifest: dict) -> dict:
    counts = Counter(str(e["since"]) for e in manifest["entries"])
    return {
        "registry_version": manifest["registry_version"],
        "manifest_sha256": canonical_digest(manifest),
        "identities": len(manifest["entries"]),
        "since_counts": {k: counts[k] for k in sorted(counts, key=Version)},
    }


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
