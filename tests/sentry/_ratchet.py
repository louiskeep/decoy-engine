"""Shared shrink-only ratchet logic for sentry baselines (stdlib plus tomllib).

A baseline is a JSON file ``{"sentry_id", "created_in", "entries": {key: {"reason"}}}``
under ``tests/sentry/baselines/``. ``scripts/dev/sentry_registry.toml`` binds each sentry id to
exactly one baseline path. ``check`` compares the working tree with a base commit and returns
findings; an empty list means the ratchet holds. Baseline keys compare exactly, except the
``permitted::<file>::<site>::<digest>`` keys of the sentries in ``REKEY_PER_SITE``, which
compare by count per site (see ``growth``). Messages carry ids, paths and keys only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REGISTRY = "scripts/dev/sentry_registry.toml"
BASELINE_DIR = "tests/sentry/baselines"
MIN_REASON = 15
PLACEHOLDERS = frozenset({"todo", "tbd", "fixme", "xxx", "n/a", "none"})
# Only sentries whose last key segment is a message digest may re-key; for any other sentry a
# changed last segment can mean a different permitted thing, so it compares exactly.
REKEY_PER_SITE = frozenset({"validation-name-messages"})


@dataclass(frozen=True)
class Baseline:
    sentry_id: str
    created_in: str
    keys: frozenset[str]
    reasons: dict[str, object]


def parse_registry(text: str) -> tuple[dict[str, str], list[str]]:
    """Return ``{id: path}`` and findings for duplicate ids or shared paths."""
    rows = tomllib.loads(text).get("sentry", [])
    out: dict[str, str] = {}
    findings: list[str] = []
    for row in rows:
        sid, path = row.get("id", ""), row.get("baseline", "")
        if sid in out:
            findings.append(f"registry: sentry id {sid} appears twice")
        out[sid] = path
    for path in sorted({p for p in out.values()}):
        ids = sorted(i for i, p in out.items() if p == path)
        if len(ids) > 1:
            findings.append(f"registry: two sentries ({', '.join(ids)}) share baseline {path}")
        if not path.startswith(BASELINE_DIR + "/"):
            findings.append(f"registry: baseline {path} is outside {BASELINE_DIR}/")
    return out, findings


def parse_baseline(text: str) -> Baseline:
    data = json.loads(text)
    entries = data.get("entries", {})
    return Baseline(
        str(data.get("sentry_id", "")),
        str(data.get("created_in", "")),
        frozenset(entries),
        entries,
    )


def reason_findings(path: str, base: Baseline) -> list[str]:
    out = []
    for key in sorted(base.keys):
        entry = base.reasons[key]
        reason = entry.get("reason") if isinstance(entry, dict) else None
        text = reason.strip() if isinstance(reason, str) else ""
        if len(text) < MIN_REASON or text.lower().strip(".!") in PLACEHOLDERS:
            out.append(
                f"{path}: reason for {key} is empty, a placeholder or under {MIN_REASON} chars"
            )
    return out


def _git_show(root: Path, ref: str, rel: str) -> str | None:
    res = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(root), "show", f"{ref}:{rel}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    return res.stdout if res.returncode == 0 else None


def _base_keys(root: Path, ref: str, path: str) -> frozenset[str]:
    text = _git_show(root, ref, path)
    return parse_baseline(text).keys if text is not None else frozenset()


def _permit_site(key: str) -> str | None:
    """``<file>::<site>`` of a ``permitted::<file>::<site>::<digest>`` key, else ``None``."""
    kind, _, rest = key.partition("::")
    site, sep, digest = rest.rpartition("::")
    return site if kind == "permitted" and sep and digest and "::" in site else None


def growth(
    head: frozenset[str], base: frozenset[str], per_site: bool = True
) -> tuple[frozenset[str], list[str]]:
    """Keys that count as growth over ``base``, and permit sites that were only re-keyed.

    A permitted key embeds a digest of its message, so any edit to a permitted site moves the
    key. For those keys growth is per site: a site absent from ``base`` or holding more keys than
    it did there is growth; the same count under new digests is a re-key, reported for review.
    The sentry still decides what is permitted. Every other key kind, and every key when
    ``per_site`` is false, compares exactly.
    """
    if not per_site:
        return frozenset(head - base), []

    def by_site(keys: frozenset[str]) -> dict[str, list[str]]:
        sites: dict[str, list[str]] = {}
        for key in keys:
            site = _permit_site(key)
            if site is not None:
                sites.setdefault(site, []).append(key)
        return sites

    head_sites, base_sites = by_site(head), by_site(base)
    grown = {k for k in head - base if _permit_site(k) is None}
    rekeyed: list[str] = []
    for site, keys in sorted(head_sites.items()):
        old = base_sites.get(site, [])
        if len(keys) > len(old):
            grown |= set(keys) - base
        elif set(keys) - base:
            rekeyed.append(site)
    return frozenset(grown), rekeyed


def _fmt(keys: frozenset[str]) -> str:
    return ", ".join(sorted(keys))


def check(root: Path, base_ref: str) -> tuple[list[str], list[str]]:
    """Return ``(findings, report_lines)`` for the tree at ``root`` against ``base_ref``."""
    reg_file = root / REGISTRY
    head, findings = parse_registry(
        reg_file.read_text(encoding="utf-8") if reg_file.exists() else ""
    )
    base_text = _git_show(root, base_ref, REGISTRY)
    base_reg = parse_registry(base_text)[0] if base_text is not None else {}
    report: list[str] = []

    on_disk = {p.relative_to(root).as_posix() for p in (root / BASELINE_DIR).glob("*.json")}
    for path in sorted(on_disk - set(head.values())):
        findings.append(f"{path}: baseline file is not registered in {REGISTRY}")

    loaded: dict[str, Baseline] = {}
    for sid, path in sorted(head.items()):
        file = root / path
        if not file.exists():
            findings.append(f"{sid}: registry points at {path} but there is no baseline file")
            continue
        loaded[sid] = base = parse_baseline(file.read_text(encoding="utf-8"))
        if base.sentry_id != sid:
            findings.append(
                f"{path}: sentry_id {base.sentry_id!r} does not match registry id {sid}"
            )
        findings += reason_findings(path, base)

    removed = set(base_reg) - set(head)
    old_union: frozenset[str] = frozenset().union(
        *(_base_keys(root, base_ref, base_reg[s]) for s in removed)
    )
    new_union: frozenset[str] = frozenset()
    union_ids = set(removed)
    for sid, base in sorted(loaded.items()):
        path = head[sid]
        report.append(f"{sid}: {len(base.keys)} key(s) in {path}")
        if sid in base_reg:
            if base_reg[sid] != path:
                findings.append(f"{sid}: baseline moved from {base_reg[sid]} to {path}")
                continue
            grown, rekeyed = growth(
                base.keys, _base_keys(root, base_ref, path), sid in REKEY_PER_SITE
            )
            report += [
                f"{sid}: re-keyed permitted site {site} (review the message)" for site in rekeyed
            ]
            if grown:
                findings.append(
                    f"{sid}: baseline gained key(s) not in the merge base: {_fmt(grown)}"
                )
        elif removed:
            new_union |= base.keys
            union_ids.add(sid)
        else:
            # Newly registered in this diff: the one way a first inventory is admitted.
            report.append(f"{sid}: first inventory, {len(base.keys)} key(s), review required")
    grown, rekeyed = growth(new_union, old_union, union_ids <= REKEY_PER_SITE)
    report += [
        f"replacement: re-keyed permitted site {site} (review the message)" for site in rekeyed
    ]
    if removed and grown:
        findings.append(
            f"replacement or split of {_fmt(frozenset(removed))} adds key(s): {_fmt(grown)}"
        )
    return findings, report
