"""Stamp the reason registry and compile changelog fragments into CHANGELOG.md at release prep.

    python scripts/release/stamp_release.py --version X.Y.Z [--date YYYY-MM-DD] [--repo PATH]
        [--registry-minor auto|X.Y.0]

Run on the release prep branch, in a full clone. It reads every `changelog.d/<slug>.md` (not the
README), orders them newest first by the commit that added each file, and writes one
`## [X.Y.Z] - DATE` section directly under the empty `## [Unreleased]` heading: the merged
"Breaking for consumers" list, then the fragments, then whatever `[Unreleased]` held before. It
deletes the fragments and adds the previous newest release to
`tests/fixtures/changelog_released_sections.json`.

In the same transaction it stamps the reason registry: the one transition
`reason_manifest_policy.registry_transition` picks (next minor for new identities, next patch for
a first deprecation or a marked summary correction alone, else none) replaces every
`since=UNRELEASED` and `deprecated_in=UNRELEASED` catalog argument, sets `REGISTRY_VERSION` and
the docs/reasons.md anchor, empties `PENDING_SUMMARY_EDITS`, writes the minor's fixture (a minor
only), rebuilds consumer-contract.json and recaptures the v2 release pin. `--registry-minor` may
only restate the automatic minor of an identity release.

`parse_fragment` is the one fragment grammar; `scripts/dev/check_fragment.py` calls it too.
Every write is planned, applied to a candidate copy of HEAD and checked there first; only then is
the same plan applied to the clean tree, where any failure restores the owned paths and removes a
created fixture. There is no resume path. Messages carry file names, line numbers, rule names,
identities and versions, never file content.

Exit codes: 0 done; 1 refused (a malformed fragment, a shallow clone, an uncommitted fragment, a
bad version or date, a CHANGELOG without the expected headings, a registry the pin rejects, a
pending identity no fragment names, a candidate that fails its checks).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

FRAGMENT_DIR = "changelog.d"
REGISTRY_PATHS = ("src/decoy_engine/reasons", "tests/fixtures/f29", "docs/reasons.md")
F29 = "tests/fixtures/f29"
REASON_PIN = f"{F29}/reason-registry-release.json"
CONTRACT = f"{F29}/consumer-contract.json"
README = "README.md"
TYPES = ("Added", "Changed", "Fixed", "Deprecated", "Removed", "Security")
PIN_FILE = Path("tests") / "fixtures" / "changelog_released_sections.json"
BREAKING_HEADING = "### Breaking for consumers"

_BREAKING_LINE = re.compile(r"Breaking: (?P<text>\S.*)")
_HEADING = re.compile(
    rf"### (?:{'|'.join(TYPES)}) \((?P<title>\S.*), (?P<slice>[^,()\s][^,()]*), (?P<date>\d{{4}}-\d{{2}}-\d{{2}})\)"
)
_ANY_H3 = re.compile(r"###(?!#)")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*\.md")
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
_RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)\] - \d{4}-\d{2}-\d{2}$", re.M)
_ANY_H2 = re.compile(r"^## \[", re.M)


def is_fragment_name(name: str) -> bool:
    """The one file-name rule: `<slug>.md`, lowercase letters, digits and hyphens, not the README."""
    return name != README and _SLUG.fullmatch(name) is not None


def _heading_ok(line: str) -> bool:
    m = _HEADING.fullmatch(line)
    return m is not None and _valid_date(m.group("date"))


def _valid_date(text: str) -> bool:
    """A real calendar date written YYYY-MM-DD (fromisoformat alone also takes other forms)."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is None:
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


class FragmentError(ValueError):
    """A refusal. The message names files, lines and rules only."""


@dataclass(frozen=True)
class Fragment:
    breaking: str | None
    block: str


def parse_fragment(text: str) -> Fragment:
    """Strictly parse one fragment: optional `Breaking:` line, then exactly one heading and a body."""
    if "\r" in text:
        raise FragmentError("carriage return in fragment")
    lines = text.split("\n")
    i = 0
    breaking: str | None = None
    if lines[0].startswith("Breaking:"):
        m = _BREAKING_LINE.fullmatch(lines[0])
        if m is None:
            raise FragmentError("line 1: malformed Breaking line")
        breaking = m.group("text").rstrip()
        i = 1
    while i < len(lines) and lines[i] == "":
        i += 1
    if i >= len(lines) or not _heading_ok(lines[i]):
        raise FragmentError(
            f"line {i + 1}: expected the '### <Type> (<title>, <slice>, <date>)' heading"
        )
    for n, line in enumerate(lines[i + 1 :], start=i + 2):
        if _ANY_H3.match(line):
            raise FragmentError(f"line {n}: a second '###' heading (use '####' for sub-headings)")
    block = "\n".join(lines[i:]).strip()
    if block == lines[i]:
        raise FragmentError(f"line {i + 1}: heading with no body")
    return Fragment(breaking, block)


def release_sections(text: str) -> list[tuple[str, str]]:
    """(version, section text from its heading up to the next release heading), newest first."""
    marks = list(_RELEASE.finditer(text))
    return [
        (m.group(1), text[m.start() : marks[i + 1].start() if i + 1 < len(marks) else len(text)])
        for i, m in enumerate(marks)
    ]


def section_digest(section: str) -> str:
    lines = [line.rstrip() for line in section.strip().splitlines()]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed git executable, literal arguments
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise FragmentError(f"git {args[0]} failed")
    return proc.stdout


def _fragment_files(root: Path) -> list[str]:
    folder = root / FRAGMENT_DIR
    names = []
    for p in sorted(folder.iterdir()):
        if p.name == README:
            continue
        if not p.is_file() or not is_fragment_name(p.name):
            raise FragmentError(f"{FRAGMENT_DIR}/{p.name}: not a '<slug>.md' fragment file")
        names.append(p.name)
    return names


def _newest_first(root: Path, names: list[str]) -> list[str]:
    """Fragment names ordered by the commit that added each file, newest first."""
    if _git(root, "rev-parse", "--is-shallow-repository").strip() != "false":
        raise FragmentError("shallow clone: fragment order needs the full history")
    log = _git(
        root,
        "log",
        "--topo-order",
        "--no-renames",
        "--diff-filter=A",
        "--name-only",
        "--format=",
        "--",
        FRAGMENT_DIR,
    )
    seen: list[str] = []
    for line in log.splitlines():
        name = line.removeprefix(f"{FRAGMENT_DIR}/")
        if name in names and name not in seen:
            seen.append(name)
    missing = sorted(set(names) - set(seen))
    if missing:
        raise FragmentError(f"{FRAGMENT_DIR}/{missing[0]}: not added by any commit")
    return seen


def _split_breaking(body: str) -> tuple[str, list[str]]:
    """The body without its 'Breaking for consumers' lists, and those lists' text."""
    out: list[str] = []
    lists: list[str] = []
    parts = re.split(r"(?m)^(?=### )", body)
    for part in parts:
        if part.startswith(BREAKING_HEADING):
            lists.append(part[len(part.split("\n", 1)[0]) :].strip())
        else:
            out.append(part)
    return "".join(out).strip(), [x for x in lists if x]


def _compose(changelog: str, version: str, date: str, fragments: list[Fragment]) -> str:
    start = changelog.find("## [Unreleased]")
    if start == -1:
        raise FragmentError("CHANGELOG.md has no '## [Unreleased]' heading")
    after = start + len("## [Unreleased]")
    nxt = _ANY_H2.search(changelog, after)
    if nxt is None:
        raise FragmentError("CHANGELOG.md has no released section below [Unreleased]")
    old_body, old_lists = _split_breaking(changelog[after : nxt.start()])
    bullets = [f"- {f.breaking}" for f in fragments if f.breaking] + old_lists
    parts: list[str] = []
    if bullets:
        parts.append(BREAKING_HEADING + "\n\n" + "\n".join(bullets))
    parts += [f.block for f in fragments]
    if old_body:
        parts.append(old_body)
    section = f"## [{version}] - {date}\n\n" + "\n\n".join(parts) + "\n\n"
    return changelog[:after] + "\n\n" + section + changelog[nxt.start() :]


def _replace(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _remove(path: Path) -> None:
    path.unlink()


def _owned_paths() -> list[str]:
    return ["CHANGELOG.md", PIN_FILE.as_posix(), FRAGMENT_DIR]


def _dirty(root: Path, owned: list[str]) -> list[str]:
    out = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *owned)
    return [line[3:] for line in out.splitlines()]


def _rollback(root: Path, owned: list[str] | None = None, created: tuple[str, ...] = ()) -> None:
    """Put the owned paths (by default CHANGELOG.md, the pin and `changelog.d/`, deleted fragments
    included) back to HEAD and remove the files this run created."""
    # Created files go first: they are the one thing the restore cannot undo if it fails.
    for rel in created:
        (root / rel).unlink(missing_ok=True)
    _git(
        root, "restore", "--source=HEAD", "--staged", "--worktree", "--", *(owned or _owned_paths())
    )


def _apply(root: Path, writes: dict[str, str], removes: list[str], owned=None, created=()) -> None:
    """The one write path: replace, then delete; any failure rolls the owned paths back."""
    try:
        for rel, text in writes.items():
            _replace(root / rel, text)
        for rel in removes:
            _remove(root / rel)
    except BaseException:
        if owned is None:
            _rollback(root)
        else:
            _rollback(root, owned, created)
        raise


def _parse_all(root: Path, ordered: list[str]) -> tuple[list[Fragment], str | None]:
    """The parsed fragments in order, or a refusal message naming the first bad file."""
    fragments: list[Fragment] = []
    for name in ordered:
        try:
            fragments.append(
                parse_fragment((root / FRAGMENT_DIR / name).read_text(encoding="utf-8"))
            )
        except FragmentError as err:
            return [], f"{FRAGMENT_DIR}/{name}: {err}"
    return fragments, None


def _preflight(root: Path, version: str, date: str, owned: list[str]) -> None:
    if _VERSION.fullmatch(version) is None:
        raise FragmentError("version must look like X.Y.Z")
    if not _valid_date(date):
        raise FragmentError("date must be a real YYYY-MM-DD date")
    dirty = _dirty(root, owned)
    if dirty:
        # `git restore` leaves untracked files alone. A killed run cannot say which it created, so
        # none is named for deletion: a file that matches a pattern may still be the operator's draft.
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", *owned).split()
        inspect = f"; untracked, inspect before removing: {' '.join(untracked)}" * bool(untracked)
        raise FragmentError(
            f"uncommitted changes in {', '.join(dirty)}; restore them with: git restore "
            f"--source=HEAD --staged --worktree -- {' '.join(owned)}{inspect}"
        )


def compile_fragments(root: Path, version: str, date: str) -> None:
    """Fold the fragments and the old `[Unreleased]` body into a dated `version` section.

    There is no resume path. The compile starts only when CHANGELOG.md, the pin and `changelog.d/`
    match HEAD, and a failure while writing restores those paths from HEAD before it re-raises. A
    crash that skips the rollback leaves a dirty tree, which the next run refuses; the operator runs
    the `git restore` the message names.
    """
    _preflight(root, version, date, _owned_paths())
    writes, removes, _ = _changelog_plan(root, version, date)
    _apply(root, writes, removes)


def _changelog_plan(
    root: Path, version: str, date: str
) -> tuple[dict[str, str], list[str], list[Fragment]]:
    """The compile's writes and deletions, and the parsed fragments; nothing is written."""
    names = _fragment_files(root)
    ordered = _newest_first(root, names) if names else []
    fragments, problem = _parse_all(root, ordered)
    if problem is not None:
        raise FragmentError(problem)

    changelog_path = root / "CHANGELOG.md"
    before = changelog_path.read_text(encoding="utf-8")
    if any(v == version for v, _ in release_sections(before)):
        raise FragmentError("CHANGELOG.md already has a section for this version")
    text = _compose(before, version, date, fragments)

    prev_version, prev_section = release_sections(text)[1]
    pin_path = root / PIN_FILE
    pins = json.loads(pin_path.read_text(encoding="utf-8"))
    pins = {
        prev_version: section_digest(prev_section),
        **{k: v for k, v in pins.items() if k != prev_version},
    }

    writes = {PIN_FILE.as_posix(): json.dumps(pins, indent=2) + "\n", "CHANGELOG.md": text}
    return writes, [f"{FRAGMENT_DIR}/{name}" for name in ordered], fragments


def _tools():
    """The registry policy, the v2 pin and the source-rewrite tools; only the stamp loads them."""
    root = Path(__file__).resolve().parents[2]
    for path in (str(root), str(root / "scripts" / "dev")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import registry_renumber as rr
    from scripts.release import reason_manifest_policy as pol
    from scripts.release import reason_release_pin as rp

    return pol, rp, rr


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _extract(rr, tree: Path) -> dict:
    try:
        return rr.rg.extract(tree, contract=True)
    except rr.rg.Refusal as err:
        raise FragmentError(f"registry: {err.message}") from None


def _registry_plan(
    root: Path, live: dict, t, version: str
) -> tuple[dict[str, str], list[str], dict]:
    """The stamp's registry writes, the files it creates, and the manifest it must yield."""
    pol, rp, rr = _tools()
    stamped = pol.stamped_manifest(live, t)
    writes: dict[str, str] = {}
    for rel in _git(root, "ls-files", "--", "src/decoy_engine/reasons").split():
        text = _read(root, rel) if rel.endswith(".py") else ""
        new = rr.stamp_source(text, t.version)
        if rel == rr.rg.REGISTRY_PY:
            new = rr.clear_pending_summaries(rr.ANCHORS[rel].sub(rf"\g<1>{t.version}\g<3>", new))
        if new != text:
            writes[rel] = new
    doc = _read(root, rr.REASONS_MD)
    writes[rr.REASONS_MD] = rr.ANCHORS[rr.REASONS_MD].sub(rf"\g<1>{t.version}\g<3>", doc)
    created: list[str] = []
    if t.writes_fixture:
        name = f"{F29}/{pol.fixture_name(t.version, pol.release_fixture_slug(version))}"
        if (root / name).exists():
            raise FragmentError(f"{name} already exists")
        header = {k: v for k, v in stamped.items() if k != "entries"}
        body = {**header, "entries": pol.entries_since(stamped, t.version)}
        writes[name] = json.dumps(body, indent=2, sort_keys=True) + "\n"
        created.append(name)
    writes[REASON_PIN] = rp.pin_text(rp.build_pin(stamped, t.version))
    return writes, created, stamped


def _candidate_contract(root: Path, writes: dict, removes: list[str], t, stamped: dict) -> str:
    """Apply the plan to a copy of HEAD, check every release postcondition there, and return the
    consumer contract rebuilt from it; refuses, writing nothing real, on any problem."""
    pol, rp, rr = _tools()
    with tempfile.TemporaryDirectory() as tmp:
        cand = Path(tmp) / "cand"
        cand.mkdir()
        try:
            rr.rg.archive(root, "HEAD", cand)
        except rr.rg.Refusal as err:
            raise FragmentError(f"registry: {err.message}") from None
        for rel, text in writes.items():
            (cand / rel).parent.mkdir(parents=True, exist_ok=True)
            (cand / rel).write_text(text, encoding="utf-8")
        for rel in removes:
            (cand / rel).unlink()
        built = _extract(rr, cand)
        pending = frozenset(map(tuple, built["pending"]))
        manifest = built["manifest"]
        pin, problems = rp.load_pin(_read(cand, REASON_PIN))
        fixtures = {
            p.name: json.loads(p.read_text(encoding="utf-8"))
            for p in (cand / F29).glob(pol.FIXTURE_GLOB)
        }
        anchors = [a[2] for a in rr.ANCHORS[rr.REASONS_MD].finditer(_read(cand, rr.REASONS_MD))]
        facts = {"manifest": manifest, "pin": pin, "contract": built["contract"]}
        facts.update(anchors=anchors, fragments=_fragment_files(cand))
        problems += pol.release_ready_violations(manifest, pending)
        problems += pol.minor_fixture_violations(fixtures, manifest, pol.FIXTURE_FLOOR)
        if pin is not None:
            problems += pol.release_pin_violations(pin, manifest, pending)
            problems += pol.release_postcondition_violations(t, stamped, facts)
    if problems:
        raise FragmentError("the stamped candidate failed: " + "; ".join(problems))
    return json.dumps(built["contract"], indent=1, sort_keys=True) + "\n"


def stamp_release(root: Path, version: str, date: str, registry_minor: str = "auto") -> str:
    """Stamp the pending registry facts and compile the changelog in one transaction; returns the
    registry version the release ships."""
    pol, rp, rr = _tools()
    owned = [*_owned_paths(), *REGISTRY_PATHS]
    _preflight(root, version, date, owned)
    state = _extract(rr, root)
    live, pending = state["manifest"], frozenset(map(tuple, state["pending"]))
    writes, removes, fragments = _changelog_plan(root, version, date)
    pin, problems = rp.load_pin(_read(root, REASON_PIN))
    if pin is None:
        raise FragmentError("registry: " + "; ".join(problems))
    t = pol.registry_transition(live, pin, pending)
    problems = pol.release_pin_violations(pin, live, pending)
    problems += pol.registry_minor_violations(t, registry_minor)
    problems += pol.pending_fragment_violations(live, [f.block for f in fragments])
    if problems:
        raise FragmentError("registry: " + "; ".join(problems))
    registry, created, stamped = _registry_plan(root, live, t, version)
    writes = {**registry, **writes}
    writes[CONTRACT] = _candidate_contract(root, writes, removes, t, stamped)
    _apply(root, writes, removes, owned, tuple(created))
    return t.version


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--version", required=True)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--registry-minor", default="auto")
    args = ap.parse_args(argv)
    try:
        shipped = stamp_release(args.repo, args.version, args.date, args.registry_minor)
    except FragmentError as err:
        print(f"stamp_release: refused: {err}", file=sys.stderr)
        return 1
    print(f"stamp_release: registry {shipped}; compiled changelog fragments into [{args.version}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
