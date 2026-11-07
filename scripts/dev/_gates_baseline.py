"""The stored failing-id baseline of scripts/dev/gates.sh: its model, run mode and order map.

`load_baseline` is the only place that reads the JSON file, so no consumer sees an unvalidated
field. `check_stored` decides whether a run may be compared with a stored file. Stdlib only,
so it runs in any venv; `_gates_report.py` is the entry point and re-exports these names."""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# How a baseline was produced. A compare is only meaningful against a baseline written the same
# way, so these fields travel with the file and `check_stored` refuses any difference.
MODE_KEYS = ("workers", "dist", "platform", "order")
PARALLEL_DIST = "loadfile"  # gates.sh's only xdist mode; one worker runs plain pytest
# The stored file's layout version; bump it when a field changes meaning or is added.
FORMAT_VERSION = 1


def order_label(order_seconds: dict[str, float]) -> str:
    """`collection` for an empty map, else a hash of the canonical map the order was built from."""
    if not order_seconds:
        return "collection"
    canon = json.dumps(order_seconds, sort_keys=True, separators=(",", ":"))
    return "file-seconds:" + hashlib.sha256(canon.encode()).hexdigest()


def run_mode(workers: int, order_seconds: dict[str, float]) -> dict[str, object]:
    return {
        "workers": workers,
        "dist": PARALLEL_DIST if workers > 1 else "no",
        "platform": f"{sys.platform}-{platform.machine()}",
        "order": order_label(order_seconds),
    }


class BaselineError(ValueError):
    """A stored baseline that may not be compared; the message says why and what to do."""


@dataclass(frozen=True)
class Baseline:
    """The one model of a stored baseline file: every reader loads it, the writer serializes it.

    `load_baseline` is the only place that reads the JSON, so no consumer sees an unvalidated
    field (a JSON `true` is not a count, a mode field is not a guess)."""

    repo: str
    main_sha: str
    head_sha: str
    collected: int
    skipped: int
    failing: tuple[str, ...]
    workers: int
    dist: str
    platform: str
    order: str
    order_seconds: dict[str, float]
    file_seconds: dict[str, float]

    def mode(self) -> dict[str, object]:
        return {k: getattr(self, k) for k in MODE_KEYS}

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["failing"] = sorted(self.failing)
        return {"format_version": FORMAT_VERSION, **data}


_FIELDS = tuple(Baseline.__dataclass_fields__)


def _is_count(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_seconds_map(v: object) -> bool:
    return isinstance(v, dict) and all(
        isinstance(k, str)
        and isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x >= 0
        for k, x in v.items()
    )


def _parse_baseline(raw: object) -> Baseline:
    if not isinstance(raw, dict):
        raise BaselineError("the stored baseline is not a JSON object; rewrite the baseline")
    version = raw.get("format_version")
    if not _is_count(version) or version != FORMAT_VERSION:
        raise BaselineError(
            f"the stored baseline has format_version {version!r}, this gates.sh reads "
            f"format_version {FORMAT_VERSION}; rewrite the baseline"
        )
    missing = [k for k in _FIELDS if k not in raw]
    if missing:
        raise BaselineError(
            f"the stored baseline has no {', '.join(missing)}; rewrite the baseline"
        )
    extra = sorted(set(raw) - set(_FIELDS) - {"format_version"})
    if extra:
        raise BaselineError(
            f"the stored baseline has unknown field(s) {', '.join(extra)}; rewrite the baseline"
        )
    checks = {
        "repo": isinstance(raw["repo"], str),
        "main_sha": isinstance(raw["main_sha"], str) and bool(raw["main_sha"]),
        "head_sha": isinstance(raw["head_sha"], str),
        "collected": _is_count(raw["collected"]),
        "skipped": _is_count(raw["skipped"]),
        "failing": isinstance(raw["failing"], list)
        and all(isinstance(t, str) for t in raw["failing"]),
        "workers": _is_count(raw["workers"]) and raw["workers"] >= 1,
        "dist": isinstance(raw["dist"], str),
        "platform": isinstance(raw["platform"], str),
        "order": isinstance(raw["order"], str),
        "order_seconds": _is_seconds_map(raw["order_seconds"]),
        "file_seconds": _is_seconds_map(raw["file_seconds"]),
    }
    bad = [k for k, ok in checks.items() if not ok]
    if bad:
        raise BaselineError(
            f"the stored baseline's {', '.join(bad)} has the wrong type; rewrite the baseline"
        )
    fields = {k: raw[k] for k in _FIELDS}
    fields["failing"] = tuple(raw["failing"])
    return Baseline(**fields)


def load_baseline(path: Path) -> Baseline:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BaselineError(
            "the stored baseline is not readable JSON; rewrite the baseline"
        ) from exc
    return _parse_baseline(raw)


def stored_failing_under(path: Path, prefixes: list[str]) -> bool:
    """True when a stored failing id starts with any prefix (a test file path or a directory)."""
    return any(t.startswith(p) for t in load_baseline(path).failing for p in prefixes)


def check_stored(path: Path, repo: str, workers: int) -> list[str]:
    """Reasons this run may not be compared with the stored baseline (empty when it may)."""
    try:
        stored = load_baseline(path)
    except BaselineError as exc:
        return [str(exc)]
    if stored.repo != repo:
        return [f"the stored baseline is for repo {stored.repo!r}, this run is {repo!r}"]
    theirs = stored.mode()
    # A candidate orders its files by the baseline's own `order_seconds` (gates.sh passes that map
    # to the suite), so its order label is derived from that map. A label that does not match
    # the map it came with therefore differs here, and a corrupt file cannot pass as same-mode.
    mine = run_mode(workers, stored.order_seconds)
    if theirs == mine:
        return []
    diff = ", ".join(k for k in MODE_KEYS if theirs[k] != mine[k])
    return [
        f"run mode differs in {diff}: baseline {json.dumps(theirs)}, this run {json.dumps(mine)}; "
        "rewrite the baseline in this mode"
    ]


def write_order_map(dest: Path, src: Path, key: str) -> None:
    """Write `{"order_seconds": <src baseline's key map>}` for the suite's collection hook.

    `key` is `order_seconds` (a candidate orders like the baseline it is compared with) or
    `file_seconds` (a new baseline orders by the timings of an earlier one, `--order-from`)."""
    if key not in ("order_seconds", "file_seconds"):
        raise BaselineError(f"cannot order by {key!r}")
    seconds = getattr(load_baseline(src), key)
    dest.write_text(json.dumps({"order_seconds": seconds}, sort_keys=True) + "\n")


def read_order_map(path: Path) -> dict[str, float]:
    """The map a `write_order_map` file holds; empty when there is no file."""
    if not path.is_file():
        return {}
    seconds = json.loads(path.read_text())["order_seconds"]
    if not _is_seconds_map(seconds):
        raise BaselineError("the order map is not a map of seconds")
    return {str(k): float(v) for k, v in seconds.items()}
