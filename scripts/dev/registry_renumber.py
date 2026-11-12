"""Move an in-flight slice off a claimed reason-registry minor, and gate the pending era.

    python scripts/dev/registry_renumber.py [--repo PATH] [--main REF]
        [--check | --to-unreleased | --print-tests]

Run on a slice branch that already contains main. New reasons are declared `since=UNRELEASED`
and release prep stamps the version (scripts/release/stamp_release.py). ``--check`` is the
non-writing gate: the branch keeps main's registry version, adds no minor fixture and no numeric
identity, and every carrier agrees. ``--to-unreleased`` converts an identity-addition branch that
claimed one numeric minor under the old per-PR process: catalog declarations become
``since=UNRELEASED``, Python and JSON carriers the string ``"unreleased"``, Markdown the text
``unreleased``; the version anchors return to main's, the branch fixture is deleted and the
consumer contract is rebuilt from the converted tree. It refuses a claim that is not above main's
version, and a Python or JSON ``registry_version`` value naming the claim (``unconverted``): set
that line to main's version by hand and rerun. Without a mode it renumbers the claimed
minor to main + 1 (the retiring numeric workflow). It never runs pytest (``--print-tests``
prints the list for ``vm-run``) and holds no comparison logic of its own: every decision comes
from scripts/release/reason_manifest_policy.py.

Exit codes: 0 consistent or done; 1 ``--check`` found a problem; 2 precondition (dirty-tree,
main-not-merged, bad-ref, foreign-import, import-failed); 3 refusal before any write; 4 post-write
verification failed (the plan is applied to a temporary copy first, so the worktree is untouched).
Messages carry keys, identities, paths, line numbers and versions only, never file content.

Known limit: the token rewrite assumes the claimed version on a branch-added line means the
slice's own version. A branch-added sentence naming it for another reason is rewritten wrongly;
the printed rewrite list is for review.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

# Scripts run by file path get their own directory on sys.path, not the repo root.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import _registry_renumber_git as rg  # noqa: E402
from scripts.release.reason_manifest_policy import (  # noqa: E402
    PENDING_ARGUMENTS,
    UNRELEASED,
    VERSION_LITERAL,
    RenumberFacts,
    contract_versions,
    fixture_name,
    fixture_prefix,
    parse_fixture_name,
    pending_era_violations,
    renumber_check,
    renumber_claim,
    renumber_classification,
    renumber_target,
    restamp,
    since_window_violations,
    unreleased_violations,
    version_of,
)
from scripts.release.reason_release_pin import load_pin  # noqa: E402

REWRITE = (r"src/decoy_engine/reasons/.*\.py", r"tests/.*\.py", r"docs/.*\.md", r"CHANGELOG\.md")
FROZEN = (
    "tests/fixtures/f29/reason-registry-release.json",
    "tests/fixtures/f29/reason-manifest-v1.json",
    "tests/fixtures/f29/reason-manifest-v1.4-f06.json",
    "tests/fixtures/f29/reason-baseline.json",
    "tests/fixtures/f29/producer-inventory.json",
    "tests/sentry/baselines/f29-*.json",
)
CONTRACT = "tests/fixtures/f29/consumer-contract.json"
REASONS_MD = "docs/reasons.md"
ANCHORS = {
    rg.REGISTRY_PY: re.compile(r'^(REGISTRY_VERSION = ")([^"]*)(")$', re.M),
    REASONS_MD: re.compile(r"(`registry_version` \(`)([^`]*)(`\))"),
}
PIN = "tests/fixtures/f29/reason-registry-release.json"
# The PENDING_SUMMARY_EDITS assignment in the registry module, through its closing parenthesis.
PENDING_SUMMARIES = re.compile(
    r"(?ms)^(PENDING_SUMMARY_EDITS: [^\n=]*= )frozenset\((?:[^()]|\([^()]*\))*\)"
)
PENDING_ARG = re.compile(rf"\b({'|'.join(PENDING_ARGUMENTS)})=UNRELEASED\b")
TYPES_IMPORT = "from decoy_engine.reasons._types import "
CATALOG_ANCHOR = "from decoy_engine.reasons import _catalog"
# --to-unreleased: the carrier kind of each branch-added surface and its replacement.
CARRIERS = (
    (r"src/decoy_engine/reasons/.*\.py", "catalog"),
    (r"tests/.*\.py", "python"),
    (r"tests/.*\.json", "json"),
    (r"(docs/.*|CHANGELOG|changelog\.d/[^/]*)\.md", "markdown"),
)
# The registry version itself is never pending: outside the two anchors a carrier of it is refused.
VERSION_KEY = r"registry_version['\"]?\]?\s*(?::|==?)\s*(['\"]){}\1"
FIXTURE_DIR = "tests/fixtures/f29"
RENUMBER_LINE = "Registry renumbered at merge from"
BASE_TESTS = (
    "tests/unit/test_reason_registry.py",
    "tests/unit/test_reason_release_pin.py",
    "tests/unit/progress/test_consumer_contract.py",
    "tests/unit/progress/test_versioned_contract.py",
    "tests/sentry/test_reason_registry.py",
    "tests/sentry/test_reason_baseline_frozen.py",
    "tests/sentry/test_wave_b_no_registry_version_literal.py",
    "tests/sentry/test_baseline_registry.py",
    "tests/sentry/test_reason_minor_fixtures.py",
)
TEST_WORDS = ("REGISTRY_VERSION", "since", "reason-manifest-v", "registry_version")


def token(old: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.]){re.escape(old)}(?!\w|\.\d)")


def surface(path: str) -> str:
    if any(fnmatch.fnmatch(path, g) for g in FROZEN):
        return "frozen-fixture"
    return "rewrite" if any(re.fullmatch(p, path) for p in REWRITE) else "unknown-surface"


def read(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_bytes(text.encode("utf-8"))


def versions_in(root: Path, hunks: list[rg.Hunk], skip: set[str]) -> list[tuple[str, int, str]]:
    """Every X.Y.Z on a branch-added line of a rewrite surface, read back from ``root``."""
    found = []
    for h in hunks:
        if h.path in skip or surface(h.path) != "rewrite":
            continue
        lines = read(root, h.path).split("\n")
        for no, _ in h.added:
            text = lines[no - 1]
            if not text.startswith(RENUMBER_LINE):
                found += [(h.path, no, v) for v in VERSION_LITERAL.findall(text)]
    return found


def gather_facts(
    root: Path, repo_facts: dict, hunks: list[rg.Hunk], contract: dict, require_fixture: bool
) -> RenumberFacts:
    fixture_path = repo_facts["fixture"]
    anchors = [a[2] for a in ANCHORS[REASONS_MD].finditer(read(root, REASONS_MD))]
    on_disk = json.loads(read(root, CONTRACT))
    fixture = None
    if fixture_path:
        fixture = {
            "name": Path(fixture_path).name,
            "manifest": json.loads(read(root, fixture_path)),
        }
    skip = {CONTRACT, *([fixture_path] if fixture_path else [])}
    return {
        "branch_owned_schema_roots": repo_facts["roots"],
        "main_moved": repo_facts["moved"],
        "anchor_versions": tuple(anchors),
        "contract_versions": contract_versions(on_disk),
        "contract_matches": on_disk == json.loads(json.dumps(contract)),
        "fixture_count": repo_facts["fixture_count"],
        "require_fixture": require_fixture,
        "fixture": fixture,
        "added_versions": tuple(versions_in(root, hunks, skip)),
    }


def _literal_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of the string and comment tokens of Python source ``text``."""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return [
        (starts[t.start[0] - 1] + t.start[1], starts[t.end[0] - 1] + t.end[1])
        for t in tokenize.generate_tokens(io.StringIO(text).readline)
        if t.type in (tokenize.STRING, tokenize.COMMENT)
    ]


def _unreleased_names(text: str) -> int:
    toks = tokenize.generate_tokens(io.StringIO(text).readline)
    return sum(1 for t in toks if t.type == tokenize.NAME and t.string == "UNRELEASED")


def stamp_source(text: str, version: str) -> str:
    """``text`` with each ``since=UNRELEASED`` / ``deprecated_in=UNRELEASED`` argument set to
    ``version``; strings and comments are untouched, and an UNRELEASED import nothing uses goes."""
    if "=UNRELEASED" not in text:
        return text
    spans = _literal_spans(text)

    def sub(m: re.Match[str]) -> str:
        return m[0] if any(a <= m.start() < b for a, b in spans) else f'{m[1]}="{version}"'

    out = PENDING_ARG.sub(sub, text)
    if out == text or _unreleased_names(out) != 1:
        return out
    out = out.replace(f"{TYPES_IMPORT}UNRELEASED\n", "", 1).replace("\n    UNRELEASED,\n", "\n", 1)
    one_line = rf"^({re.escape(TYPES_IMPORT)}.*?)(?:\bUNRELEASED, |, UNRELEASED\b)"
    return re.sub(one_line, r"\1", out, count=1, flags=re.M)


def clear_pending_summaries(text: str) -> str:
    return PENDING_SUMMARIES.sub(r"\g<1>frozenset()", text)


def add_unreleased_import(text: str) -> str:
    """``text`` importing UNRELEASED from the sibling ``_types`` module, isort-ordered."""
    if re.search(rf"^{re.escape(TYPES_IMPORT)}(\(?[^)]*)?\bUNRELEASED\b", text, re.M):
        return text
    lines = text.split("\n")
    for n, line in enumerate(lines):
        if not line.startswith(TYPES_IMPORT):
            continue
        if line.endswith("("):
            lines.insert(n + 1, "    UNRELEASED,")
        else:
            names = ["UNRELEASED", *line[len(TYPES_IMPORT) :].split(", ")]
            one = TYPES_IMPORT + ", ".join(names)
            many = TYPES_IMPORT + "(\n" + "".join(f"    {x},\n" for x in names) + ")"
            lines[n] = one if len(one) <= 100 else many
        return "\n".join(lines)
    if CATALOG_ANCHOR not in lines:
        return text
    lines.insert(lines.index(CATALOG_ANCHOR) + 1, f"{TYPES_IMPORT}UNRELEASED")
    return "\n".join(lines)


def plan_rewrites(root: Path, hunks, old: str, target: str, fixture: str | None):
    """Line edits {path: {line: text}}, record paths, and refusals; nothing is written."""
    pat, prefix, swap = token(old), fixture_prefix(old), fixture_prefix(target)
    edits: dict[str, dict[int, str]] = {}
    refuse: list[str] = []
    records: set[str] = set()

    def rewrite(text: str) -> str:
        return pat.sub(target, text.replace(prefix, swap))

    for h in hunks:
        if h.path in (CONTRACT, fixture):
            continue
        kind = surface(h.path)
        anchor = ANCHORS.get(h.path)
        outside = [anchor.sub("", r) if anchor else r for r in h.removed]
        for no, text in h.added:
            if text.startswith(RENUMBER_LINE) or not (pat.search(text) or prefix in text):
                continue
            if kind != "rewrite":
                refuse.append(f"{kind} {h.path}:{no}")
            elif any(pat.search(r) or prefix in r for r in outside):
                refuse.append(f"mixed-line {h.path}:{h.start}")
            else:
                if rewrite(text) != text:
                    edits.setdefault(h.path, {})[no] = rewrite(text)
                if h.path.startswith("docs/records/"):
                    records.add(h.path)
    if fixture:
        lines = read(root, fixture).split("\n")
        edits[fixture] = {n + 1: rewrite(t) for n, t in enumerate(lines) if rewrite(t) != t}
    return edits, records, refuse


def carrier(path: str) -> str:
    if any(fnmatch.fnmatch(path, g) for g in FROZEN):
        return "frozen-fixture"
    return next((kind for rx, kind in CARRIERS if re.fullmatch(rx, path)), "unknown-surface")


def unreleased_text(kind: str, old: str, text: str) -> str:
    """One branch-added line of a ``kind`` carrier with the claimed ``old`` version made pending."""
    v = re.escape(old)
    if kind == "catalog":
        return re.sub(rf"\bsince=(['\"]){v}\1", "since=UNRELEASED", text)
    if kind == "python":
        return re.sub(rf"(['\"]){v}\1", rf"\g<1>{UNRELEASED}\g<1>", text)
    if kind == "json":
        return text.replace(f'"{old}"', f'"{UNRELEASED}"')
    return token(old).sub(UNRELEASED, text) if kind == "markdown" else text


def plan_unreleased(hunks, old: str, fixture: str | None):
    """Line edits {path: {line: text}} and refusals for --to-unreleased; nothing is written."""
    pat, prefix = token(old), fixture_prefix(old)
    keyed = re.compile(VERSION_KEY.format(re.escape(old)))
    edits: dict[str, dict[int, str]] = {}
    refuse: list[str] = []
    for h in hunks:
        if h.path in (CONTRACT, fixture):
            continue
        kind, anchor = carrier(h.path), ANCHORS.get(h.path)
        for no, text in h.added:
            if (anchor and anchor.search(text)) or not (pat.search(text) or prefix in text):
                continue
            new = unreleased_text(kind, old, text)
            if kind in ("python", "json") and keyed.search(text):
                refuse.append(f"unconverted {h.path}:{no}")
            elif pat.search(new) or prefix in new:
                refuse.append(f"{kind if new == text else 'unconverted'} {h.path}:{no}")
            else:
                edits.setdefault(h.path, {})[no] = new
    return edits, refuse


def apply(
    root: Path, edits: dict, records: set[str], note: str, target: str, rename, mv
) -> list[str]:
    """The one write path, for the candidate copy and the worktree alike; returns changed paths."""
    changed: list[str] = []
    for rel in sorted({*edits, *records, *ANCHORS}):
        text = old_text = read(root, rel)
        lines = text.split("\n")
        for no, new in edits.get(rel, {}).items():
            lines[no - 1] = new
        text = "\n".join(lines)
        if rel in ANCHORS:
            text = ANCHORS[rel].sub(rf"\g<1>{target}\g<3>", text)
        if rel in records and note:
            text = text.rstrip("\n") + "\n" + note + "\n"
        if text != old_text:
            write(root, rel, text)
            changed.append(rel)
    if rename and rename[0] != rename[1]:
        mv(root, *rename)
        changed.append(rename[1])
    return changed


def os_mv(root: Path, a: str, b: str) -> None:
    (root / a).rename(root / b)


def os_rm(root: Path, rel: str) -> None:
    (root / rel).unlink()


def git_rm(root: Path, rel: str) -> None:
    rg.git(root, "rm", "-q", rel)


def convert(root: Path, edits: dict, version: str, fixture: str | None, rm) -> list[str]:
    """The one write path of --to-unreleased, for the candidate copy and the worktree alike."""
    changed = apply(root, edits, set(), "", version, None, os_mv)
    for rel in sorted(edits):
        text = read(root, rel)
        if carrier(rel) == "catalog" and "since=UNRELEASED" in text:
            write(root, rel, add_unreleased_import(text))
    if fixture:
        rm(root, fixture)
    return sorted({*changed, *([fixture] if fixture else [])})


def lint(root: Path, paths: list[str]) -> list[str]:
    """Syntax, import-order and unused-import problems in the converted Python files. Ruff runs
    when the environment has it (the `lint` extra); importing the converted tree is the extract."""
    files = [p for p in paths if p.endswith(".py")]
    bad = []
    for rel in files:
        try:
            compile(read(root, rel), rel, "exec")
        except SyntaxError:
            bad.append(rel)
    ruff = shutil.which(
        "ruff", path=f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    if files and ruff and not bad:
        cmd = [ruff, "check", "--no-cache", "--quiet", "--select", "I,F401,F821", *files]
        if subprocess.run(cmd, cwd=root, capture_output=True, check=False).returncode:  # noqa: S603
            bad = files
    return ["lint " + " ".join(bad)] if bad else []


def git_mv(root: Path, a: str, b: str) -> None:
    rg.git(root, "mv", a, b)


def main_tree(repo: Path, ref: str, tmp: Path, contract: bool = False) -> dict:
    rg.archive(repo, ref, tmp, "src/decoy_engine", *([rg.CONTRACT_PY] if contract else []))
    return rg.extract(tmp)


def print_tests(repo: Path, main: str) -> None:
    changed = rg.git(
        repo, "diff", "--name-only", "--diff-filter=AM", main, "HEAD", "--", "tests"
    ).stdout.split()
    extra = [
        p for p in sorted(changed)
        if Path(p).name.startswith("test_") and p.endswith(".py") and (repo / p).exists() and any(w in read(repo, p) for w in TEST_WORDS)
    ]  # fmt: skip
    print("\n".join(dict.fromkeys(p for p in (*BASE_TESTS, *extra) if (repo / p).exists())))


def run(repo: Path, main: str, check: bool, to_unreleased: bool = False) -> int:
    if rg.git(repo, "status", "--porcelain").stdout.strip():
        raise rg.Refusal(2, "dirty-tree")
    msha = rg.git(repo, "rev-parse", "--verify", f"{main}^{{commit}}").stdout.strip()
    if rg.git(repo, "merge-base", "--is-ancestor", msha, "HEAD", check=False).returncode != 0:
        raise rg.Refusal(2, "main-not-merged")
    fork = rg.fork_point(repo, msha)
    hunks = list(rg.hunks(repo, msha))
    added = rg.git(repo, "diff", "--name-only", "--diff-filter=A", msha, "HEAD").stdout.split()
    fixtures = [
        p
        for p in added
        if Path(p).parent.as_posix() == FIXTURE_DIR and parse_fixture_name(Path(p).name)
    ]
    with tempfile.TemporaryDirectory() as work:
        w = Path(work)
        (w / "m").mkdir()
        m = main_tree(repo, msha, w / "m")["manifest"]
        if fork == msha:
            b = m
        else:
            (w / "b").mkdir()
            b = main_tree(repo, fork, w / "b")["manifest"]
        head = rg.extract(repo, contract=True)
        h = head["manifest"]
        repo_facts = {
            "fixture_count": len(fixtures),
            "roots": rg.owned_roots(repo, fork),
            "moved": fork != msha,
            "fixture": fixtures[0] if len(fixtures) == 1 else None,
        }
        facts = gather_facts(repo, repo_facts, hunks, head["contract"], check)
        if check:
            facts["pending_summaries"] = frozenset(map(tuple, head["pending"]))
            problems = renumber_check(b, m, h, facts) + pending_era_violations(m, h, facts)
            print("\n".join(problems) or "registry consistent")
            return 1 if problems else 0
        refusals = renumber_classification(b, m, h, facts)
        if len(fixtures) > 1:
            refusals.append("several-fixtures")
        if refusals:
            raise rg.Refusal(3, "\n".join(refusals))
        claim = renumber_claim(m, h)
        if not claim:
            print("no registry change")
            return 0
        if to_unreleased:
            return unreleased(repo, w, (b, m, h), claim[0], hunks, repo_facts)
        return renumber(repo, w, (b, m, h), claim[0], facts, hunks, repo_facts)


def unreleased(repo, w, manifests, old, hunks, repo_facts) -> int:
    b, m, h = manifests
    version = version_of(m)
    if since_window_violations([("claim", old)], version, old):
        # Main already holds this minor, so a branch line naming it may mean main's version.
        how = "renumber the slice above main first (run without a mode), then convert it"
        raise rg.Refusal(3, f"claim-not-above-main {old} (main is at {version}): {how}")
    for rel, rx in ANCHORS.items():
        if len(rx.findall(read(repo, rel))) != 1:
            raise rg.Refusal(3, f"anchor-missing {rel}")
    fixture = repo_facts["fixture"]
    edits, refuse = plan_unreleased(hunks, old, fixture)
    if refuse:
        raise rg.Refusal(3, "\n".join(refuse))
    cand = w / "cand"
    cand.mkdir()
    rg.archive(repo, "HEAD", cand)
    changed = convert(cand, edits, version, fixture, os_rm)
    built = rg.extract(cand, contract=True)
    bytes_ = json.dumps(built["contract"], indent=1, sort_keys=True) + "\n"
    write(cand, CONTRACT, bytes_)
    stale = _stale_contract(read(repo, CONTRACT), built["contract"], version)
    after = {**repo_facts, "fixture": None, "fixture_count": 0}
    facts = gather_facts(cand, after, hunks, built["contract"], False)
    facts["pending_summaries"] = frozenset(map(tuple, built["pending"]))
    pin, problems = load_pin(read(cand, PIN))
    problems += ["consumer contract was stale before conversion"] if stale else []
    problems += unreleased_violations(built["manifest"], pin) if pin else []
    problems += renumber_check(b, m, built["manifest"], facts)
    problems += pending_era_violations(m, built["manifest"], facts)
    if _ids(built["manifest"]) != _ids(h):
        problems.append("identity set changed")
    problems += lint(cand, changed)
    if problems:
        raise rg.Refusal(4, "\n".join(problems))
    for rel, by_line in sorted(edits.items()):
        print("\n".join(f"{rel}:{n} {old} -> {UNRELEASED}" for n in sorted(by_line)))
    convert(repo, edits, version, fixture, git_rm)
    write(repo, CONTRACT, bytes_)
    print(f"converted {old} -> {UNRELEASED}: main stays at {version}")
    return 0


def renumber(repo, w, manifests, old, facts, hunks, repo_facts) -> int:
    b, m, h = manifests
    target = renumber_target(m)
    for rel, rx in ANCHORS.items():
        if len(rx.findall(read(repo, rel))) != 1:
            raise rg.Refusal(3, f"anchor-missing {rel}")
    fixture, new_name = repo_facts["fixture"], None
    if fixture:
        slug = parse_fixture_name(Path(fixture).name)[1]  # type: ignore[index]
        new_name = f"{FIXTURE_DIR}/{fixture_name(target, slug)}"
        if new_name != fixture and (repo / new_name).exists():
            raise rg.Refusal(3, f"fixture-exists {new_name}")
    edits, records, refuse = plan_rewrites(repo, hunks, old, target, fixture)
    if refuse:
        raise rg.Refusal(3, "\n".join(refuse))
    note = (
        (
            f"{RENUMBER_LINE} {old} to {target} by scripts/dev/registry_renumber.py; "
            f"main was at {version_of(m)}."
        )
        if old != target
        else ""
    )
    rename = (fixture, new_name) if fixture and new_name else None
    cand = w / "cand"
    cand.mkdir()
    rg.archive(repo, "HEAD", cand)
    apply(cand, edits, records, note, target, rename, os_mv)
    built = rg.extract(cand, contract=True)
    stale = _stale_contract(read(repo, CONTRACT), built["contract"], target)
    bytes_ = json.dumps(built["contract"], indent=1, sort_keys=True) + "\n"
    write(cand, CONTRACT, bytes_)
    facts2 = gather_facts(
        cand, {**repo_facts, "fixture": new_name or fixture}, hunks, built["contract"], False
    )
    problems = ["consumer contract was stale before renumber"] if stale else []
    problems += renumber_check(b, m, built["manifest"], facts2)
    if _ids(built["manifest"]) != _ids(h):
        problems.append("identity set changed")
    if problems:
        raise rg.Refusal(4, "\n".join(problems))
    for rel, by_line in sorted(edits.items()):
        print("\n".join(f"{rel}:{n} {old} -> {target}" for n in sorted(by_line)))
    changed = apply(repo, edits, records, note, target, rename, git_mv)
    if read(repo, CONTRACT) != bytes_:
        write(repo, CONTRACT, bytes_)
        changed.append(CONTRACT)
    print(f"renumbered {old} -> {target}: {len(changed)} files" if changed else "nothing to do")
    return 0


def _ids(manifest: dict) -> set[tuple[str, str]]:
    return {(e["namespace"], e["code"]) for e in manifest["entries"]}


def _stale_contract(head_text: str, rebuilt: dict, target: str) -> bool:
    return restamp(json.loads(head_text), target) != json.loads(json.dumps(rebuilt))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--main", default="origin/main")
    mode = ap.add_mutually_exclusive_group()
    for flag in ("--check", "--to-unreleased", "--print-tests"):
        mode.add_argument(flag, action="store_true")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    try:
        if args.print_tests:
            print_tests(repo, args.main)
            return 0
        return run(repo, args.main, args.check, args.to_unreleased)
    except rg.Refusal as exc:
        print(exc.message, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
