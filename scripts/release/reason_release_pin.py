"""Pin the reason catalog an engine release ships: the strict v2 release pin.

The pin lives at tests/fixtures/f29/reason-registry-release.json and is checked by
tests/unit/test_reason_release_pin.py on every run and by release_checks.sh before a tag:

    python scripts/release/reason_release_pin.py --check          # the ordinary gate
    python scripts/release/reason_release_pin.py --release-ready  # nothing is left to stamp
    python scripts/release/reason_release_pin.py --write          # release only; refuses pending
    python scripts/release/reason_release_pin.py --migrate-v1     # one-time v1 -> v2 migration

Format version 2 keeps, per numeric identity, one SHA-256 for each manifest-entry field over its
canonical JSON (sorted keys, no whitespace, ASCII), plus the shipped registry version, the
greatest captured `since` and per-`since` counts. This module owns the writer and the strict
reader; every comparison against the live catalog is `reason_manifest_policy`'s. Messages name
roots, identities and fields only, never catalog values or digests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from packaging.version import Version

# Scripts run by file path get their own directory on sys.path, not the repo root.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.release import reason_manifest_policy as pol  # noqa: E402

PIN_PATH = _ROOT / "tests" / "fixtures" / "f29" / ("reason-registry-release.json")
PIN_FORMAT = 2
PIN_ROOTS = ("format_version", "registry_version", "baseline_ceiling", "identities", "since_counts")
_RECORD_KEYS = {"namespace", "code", "fields"}
_V1_ROOTS = {"registry_version", "manifest_sha256", "identities", "since_counts"}
_NUMERIC = re.compile(r"\d+\.\d+\.\d+")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def build_pin(manifest: dict, shipped: str) -> dict:
    """The v2 pin of ``manifest``'s numeric identities, released as registry ``shipped``."""
    numeric = sorted(
        pol.numeric_entries(manifest["entries"]), key=lambda e: (e["namespace"], e["code"])
    )
    if not numeric:
        raise ValueError("a release pin needs at least one numeric identity")
    counts = Counter(e["since"] for e in numeric)
    return {
        "format_version": PIN_FORMAT,
        "registry_version": shipped,
        "baseline_ceiling": max(counts, key=Version),
        "identities": [
            {
                "namespace": e["namespace"],
                "code": e["code"],
                "fields": {f: pol.field_digest(e[f]) for f in pol.PIN_FIELDS},
            }
            for e in numeric
        ],
        "since_counts": {k: counts[k] for k in sorted(counts, key=Version)},
    }


def pin_text(pin: dict) -> str:
    return json.dumps(pin, indent=2, ensure_ascii=True) + "\n"


def _version_text(value: object) -> bool:
    return isinstance(value, str) and _NUMERIC.fullmatch(value) is not None


def _record_violations(records: object) -> list[str]:
    if not isinstance(records, list):
        return ["identities must be an array"]
    out: list[str] = []
    seen: list[tuple[str, str]] = []
    for n, rec in enumerate(records):
        parts = [rec.get(k) for k in ("namespace", "code")] if isinstance(rec, dict) else []
        if not (isinstance(rec, dict) and set(rec) == _RECORD_KEYS) or not all(
            isinstance(p, str) and p for p in parts
        ):
            out.append(f"identity record {n} must hold exactly a namespace, a code and fields")
            continue
        seen.append((rec["namespace"], rec["code"]))
        name, fields = f"{rec['namespace']}.{rec['code']}", rec["fields"]
        if not isinstance(fields, dict) or set(fields) != set(pol.PIN_FIELDS):
            out.append(f"identity {name} fields must be exactly the manifest entry fields")
            continue
        out += [
            f"identity {name} field {f} is not a SHA-256 hex digest"
            for f in pol.PIN_FIELDS
            if not isinstance(fields[f], str) or not _DIGEST.fullmatch(fields[f])
        ]
    out += [f"identity {i[0]}.{i[1]} is duplicated" for i, k in Counter(seen).items() if k > 1]
    if seen != sorted(seen):
        out.append("identities are not sorted by (namespace, code)")
    return out


def _count_violations(counts: object, records: object) -> list[str]:
    if not isinstance(counts, dict):
        return ["since_counts must be an object"]
    out = ["since_counts key is not an X.Y.Z version"] * any(not _version_text(k) for k in counts)
    out += [
        f"since_counts {k} must be a non-negative integer"
        for k, v in counts.items()
        if _version_text(k) and (type(v) is not int or v < 0)
    ]
    total = sum(v for v in counts.values() if type(v) is int)
    if isinstance(records, list) and total != len(records):
        out.append(f"since_counts total {total} is not the identity count")
    return out


def pin_format_violations(pin: object) -> list[str]:
    """Why ``pin`` is not a strict v2 release pin; empty when it is one."""
    if not isinstance(pin, dict):
        return ["release pin root must be an object"]
    out = [f"release pin root {r} is missing" for r in PIN_ROOTS if r not in pin]
    out += [f"release pin root {k} is not allowed" for k in sorted(pin) if k not in PIN_ROOTS]
    if out:
        return out
    if type(pin["format_version"]) is not int or pin["format_version"] != PIN_FORMAT:
        out.append(f"format_version must be the integer {PIN_FORMAT}")
    out += [f"{r} must be an X.Y.Z string" for r in PIN_ROOTS[1:3] if not _version_text(pin[r])]
    out += _record_violations(pin["identities"])
    return out + _count_violations(pin["since_counts"], pin["identities"])


def load_pin(text: str) -> tuple[dict | None, list[str]]:
    """The strict reader: ``(pin, [])``, or ``(None, problems)`` for any malformed pin."""
    dupes: list[str] = []

    def pairs(items: list[tuple[str, object]]) -> dict:
        dupes.extend(k for k, n in Counter(k for k, _ in items).items() if n > 1)
        return dict(items)

    try:
        raw = json.loads(text, object_pairs_hook=pairs)
    except ValueError:
        return None, ["release pin is not valid JSON"]
    problems = [f"release pin has a duplicate key {k}" for k in dupes] + pin_format_violations(raw)
    return (None, problems) if problems else (raw, [])


def pin_violations(pin: object, live: dict, marked: frozenset = frozenset()) -> list[str]:
    """The ordinary gate: a strict v2 pin that ``live`` honours under its pending edits."""
    return pin_format_violations(pin) or pol.release_pin_violations(pin, live, marked)


def migrate_v1(v1: dict, live: dict, fixtures: dict, floor: str) -> tuple[dict | None, list[str]]:
    """The v2 pin for the shipped v1 ``v1``; refuses while v1 or a full historical fixture
    disagrees with ``live``, so the format change cannot launder an existing mismatch."""
    if not isinstance(v1, dict) or set(v1) != _V1_ROOTS:
        return None, ["the pin is not in the v1 aggregate format"]
    problems = pol.legacy_v1_violations(v1, live)
    problems += pol.minor_fixture_violations(fixtures, live, floor, full=True)
    return (None, problems) if problems else (build_pin(live, v1["registry_version"]), [])


def _live() -> tuple[dict, frozenset]:
    from decoy_engine import reasons

    return json.loads(json.dumps(reasons.manifest())), frozenset(reasons.pending_summary_edits())


def _report(problems: list[str], ok: str, label: str = "") -> int:
    for p in problems:
        print(f"reason release pin{label}: {p}", file=sys.stderr)
    if not problems:
        print(f"reason release pin {ok}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ("--check", "--release-ready", "--write", "--migrate-v1"):
        mode.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)
    live, marked = _live()
    if not PIN_PATH.is_file():
        return _report([f"{PIN_PATH.name} is missing"], "")
    text = PIN_PATH.read_text(encoding="utf-8")
    if args.migrate_v1:
        fixtures = {
            p.name: json.loads(p.read_text()) for p in PIN_PATH.parent.glob(pol.FIXTURE_GLOB)
        }
        pin, problems = migrate_v1(json.loads(text), live, fixtures, pol.FIXTURE_FLOOR)
        if pin is not None:
            PIN_PATH.write_text(pin_text(pin), encoding="utf-8")
        return _report(problems, f"migrated to format {PIN_FORMAT}")
    pin, problems = load_pin(text)
    if args.write:
        problems = pol.release_ready_violations(live, marked)
        if not problems:
            PIN_PATH.write_text(pin_text(build_pin(live, live["registry_version"])), "utf-8")
        return _report(problems, f"written for registry {live['registry_version']}")
    if pin is not None:
        problems = pol.release_pin_violations(pin, live, marked)
    if args.release_ready and not problems:
        problems = pol.release_ready_violations(live, marked)
    label = " (not release-ready)" if args.release_ready else ""
    return _report(problems, "release-ready" if args.release_ready else "current", label)


if __name__ == "__main__":
    raise SystemExit(main())
