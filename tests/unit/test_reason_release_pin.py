"""The reason catalog an engine release shipped is frozen at its registry version.

The A1 gate (`test_reason_registry.py`) checks the live catalog against the v1 pin
(`reason-manifest-v1.json`, registry 1.0.0) and demands a minor bump only for identities new
relative to THAT pin. Once the live registry has moved past the pin (1.1.0), an identity added at
1.1.0 with no bump passes A1. This pin closes that gap per release: it records the registry version
the last engine release shipped plus a digest of that catalog, so any change at the same version
fails, and per-`since` counts, so a new identity cannot claim an already-released version.

It is a digest and counts, not a second copy of the catalog: the catalog carries the retired
tombstone spellings that only the v1 pin is excepted to hold. `scripts/release/cut-release.sh`
refuses to tag unless this pin describes the live registry (re-capture with
`python scripts/release/reason_release_pin.py --write`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from packaging.version import Version
from scripts.release.reason_manifest_policy import since_floor_violations
from scripts.release.reason_release_pin import PIN_PATH, pin_violations, release_pin

from decoy_engine import reasons

V1_PIN = Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.json"


def _pin() -> dict:
    return json.loads(PIN_PATH.read_text())


def _live() -> dict:
    return json.loads(json.dumps(reasons.manifest()))


def test_live_registry_honours_the_released_pin() -> None:
    assert pin_violations(_pin(), _live()) == []


def test_pin_is_well_formed() -> None:
    pin = _pin()
    assert set(pin) == {"registry_version", "manifest_sha256", "identities", "since_counts"}
    assert sum(pin["since_counts"].values()) == pin["identities"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda m: m["entries"].append({**m["entries"][0], "code": "brand_new_code"}),
            "changed without a registry version bump",
        ),
        (
            lambda m: m["entries"][0].__setitem__("summary", "Edited."),
            "changed without a registry version bump",
        ),
        (
            lambda m: (
                m.__setitem__("registry_version", "9.9.0"),
                m["entries"].append({**m["entries"][0], "code": "brand_new_code"}),
            ),
            "claim an already-released since",
        ),
        (lambda m: m.__setitem__("registry_version", "0.0.1"), "went backwards"),
        (
            # A whole released bucket moves to a new version under a bump: no live bucket is
            # left at 1.3.0, so only a check over the pinned buckets sees it drop to 0.
            lambda m: (
                m.__setitem__("registry_version", "9.9.0"),
                [e.__setitem__("since", "9.9.0") for e in m["entries"] if e["since"] == "1.3.0"],
            ),
            "0 identities claim an already-released since 1.3.0",
        ),
    ],
)
def test_pin_mutants_are_rejected(mutate, expected) -> None:
    pin = release_pin(_live())
    live = _live()
    mutate(live)
    assert any(expected in p for p in pin_violations(pin, live))


def test_a_bumped_addition_with_a_new_since_is_accepted() -> None:
    pin = release_pin(_live())
    live = copy.deepcopy(_live())
    live["registry_version"] = "9.9.0"
    live["entries"].append({**live["entries"][0], "code": "brand_new_code", "since": "9.9.0"})
    assert pin_violations(pin, live) == []


def test_an_unbumped_addition_to_the_live_catalog_fails_the_checked_in_pin() -> None:
    # The premise is stated, not inherited: the live catalog sits at the PINNED version (no
    # bump), whatever version the registry has moved to since the release.
    pin = _pin()
    live = copy.deepcopy(_live())
    live["registry_version"] = pin["registry_version"]
    live["entries"].append(
        {**live["entries"][0], "code": "brand_new_code", "since": pin["registry_version"]}
    )
    expected = (
        f"the catalog released as {pin['registry_version']} changed without a registry version bump"
    )
    assert expected in pin_violations(pin, live)


def test_a_bumped_addition_to_the_live_catalog_passes_the_checked_in_pin() -> None:
    # The sibling: the same addition under a version above the pin, claiming that version, is a
    # correct bump and reports nothing.
    pin = _pin()
    pinned = Version(pin["registry_version"])
    bumped = max(
        Version(_live()["registry_version"]), Version(f"{pinned.major}.{pinned.minor + 1}.0")
    )
    live = copy.deepcopy(_live())
    live["registry_version"] = str(bumped)
    live["entries"].append({**live["entries"][0], "code": "brand_new_code", "since": str(bumped)})
    assert bumped > pinned
    assert pin_violations(pin, live) == []


since_violations = since_floor_violations


def test_identities_added_after_the_v1_pin_name_their_own_since() -> None:
    assert since_violations(json.loads(V1_PIN.read_text()), _live()) == []


def test_reasons_doc_states_the_live_registry_version() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "reasons.md").read_text(encoding="utf-8")
    assert f"`registry_version` (`{reasons.REGISTRY_VERSION}`)" in doc


# REG-RELEASE E2: the strict v2 pin (one digest per manifest field) and its CLI modes.
import hashlib  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from scripts.release import reason_manifest_policy as pol  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = {
    "namespace": "config", "code": "example", "symbol": "EXAMPLE", "summary": "Example.",
    "owner": "ExampleError", "kind": "failure", "rules": [], "context": [], "since": "1.23.0",
    "deprecated_in": None, "replaced_by": None,
}  # fmt: skip
GOLDEN = """{
  "format_version": 2,
  "registry_version": "1.22.0",
  "baseline_ceiling": "1.23.0",
  "identities": [
    {
      "namespace": "config",
      "code": "example",
      "fields": {
        "code": "2e23250164957d519115f2dd253384e487806e87cb3557eb1372b937dff93959",
        "context": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "deprecated_in": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
        "kind": "3045b5c998d76c75f480b2e91388b6adef07205004129c0b8dffee745b4aa77e",
        "namespace": "48882ab48bfdf3bef2fe65e3304e91ccfd8f74e357c183367ad6fe903fe0e697",
        "owner": "8f92c9a8acd15b4719b1d1e3ace2a7d07618948904ae2ebc4c99f4f18fe02281",
        "replaced_by": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
        "rules": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "since": "dd89545194911b5b0e3ddbc7285adf096b7196163c2fbf42e2a382cab8fc6c23",
        "summary": "cc6db7b8b4b774cc4f1743881776cb076e906271caa9dd3958db760598b9c304",
        "symbol": "81d3380871cf9d1c4db281c5f5f051a708922e920e0e8d0b80d40fc9fa3b0b2d"
      }
    }
  ],
  "since_counts": {
    "1.23.0": 1
  }
}
"""


def _canonical_sha(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def test_at1_one_entry_writer_output_is_the_normative_golden() -> None:
    manifest = {"registry_version": "1.23.0", "entries": [EXAMPLE]}
    assert pol.pin_text(pol.build_pin(manifest, "1.22.0")) == GOLDEN
    assert pol.load_pin(GOLDEN) == (json.loads(GOLDEN), [])


def test_at1_field_digests_are_sha256_over_canonical_json() -> None:
    fields = json.loads(GOLDEN)["identities"][0]["fields"]
    assert {f: _canonical_sha(EXAMPLE[f]) for f in EXAMPLE} == fields
    assert (
        pol.field_digest({"b": 1, "a": [2, 1]}) == hashlib.sha256(b'{"a":[2,1],"b":1}').hexdigest()
    )
    assert pol.field_digest({"a": [2, 1], "b": 1}) == pol.field_digest({"b": 1, "a": [2, 1]})
    assert pol.field_digest([1, 2]) != pol.field_digest([2, 1])
    assert pol.field_digest(None) == hashlib.sha256(b"null").hexdigest()
    assert pol.field_digest("é") == hashlib.sha256(b'"\\u00e9"').hexdigest()


def test_at1_the_checked_in_pin_is_v2_and_round_trips_byte_for_byte() -> None:
    text = PIN_PATH.read_text(encoding="utf-8")
    pin, problems = pol.load_pin(text)
    assert problems == [] and pin is not None
    assert pol.pin_text(pin) == text
    numeric = {
        (e["namespace"], e["code"]) for e in _live()["entries"] if e["since"] != "unreleased"
    }
    assert {(r["namespace"], r["code"]) for r in pin["identities"]} <= numeric


# The CLI cases run the real script against a copy of src/ so a catalog edit stays local.
@pytest.fixture(scope="module")
def engine_copy(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("engine")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "src", base / "src", ignore=ignore)
    shutil.copytree(ROOT / "scripts" / "release", base / "scripts" / "release", ignore=ignore)
    shutil.copytree(ROOT / "tests" / "fixtures" / "f29", base / "tests" / "fixtures" / "f29")
    return base


@pytest.fixture
def tree(engine_copy: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "tree"
    shutil.copytree(engine_copy, dest)
    return dest


def pin_cli(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(tree / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(tree / "scripts/release/reason_release_pin.py"), *args],
        capture_output=True, text=True, env=env, cwd=tree.parent, check=False,
    )  # fmt: skip


def edit_file(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, path
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


CONFIG2 = "src/decoy_engine/reasons/config/_part2.py"
CONFIG1 = "src/decoy_engine/reasons/config/_part1.py"
REGISTRY = "src/decoy_engine/reasons/_registry.py"
IMPORT_ANCHOR = "from decoy_engine.reasons import _catalog\n"
TYPES_IMPORT = "from decoy_engine.reasons._types import UNRELEASED\n"
UNTAGGED = '"A validation failure with no registered code."'
EMPTY_MARKERS = "PENDING_SUMMARY_EDITS: frozenset[tuple[str, str]] = frozenset()"


def _ensure_import(tree: Path) -> None:
    if TYPES_IMPORT not in (tree / CONFIG2).read_text(encoding="utf-8"):
        edit_file(tree / CONFIG2, IMPORT_ANCHOR, IMPORT_ANCHOR + TYPES_IMPORT)


def add_pending_identity(tree: Path, code: str = "probe_pending") -> None:
    _ensure_import(tree)
    with (tree / CONFIG2).open("a", encoding="utf-8") as fh:
        fh.write(f'\n{code.upper()} = _d("{code}", "A pending probe.", since=UNRELEASED)\n')


def add_pending_deprecation(tree: Path) -> None:
    _ensure_import(tree)
    edit_file(
        tree / CONFIG2,
        '    since="1.12.0",\n)',
        '    since="1.12.0",\n    deprecated_in=UNRELEASED,\n    replaced_by=("config", "untagged"),\n)',
    )


def add_pending_summary(tree: Path) -> None:
    edit_file(tree / CONFIG1, UNTAGGED, '"A validation failure that carries no registered code."')
    edit_file(
        tree / REGISTRY,
        EMPTY_MARKERS,
        EMPTY_MARKERS.replace("frozenset()", 'frozenset({("config", "untagged")})'),
    )


PENDING_CLASSES = {
    "identity": (add_pending_identity, "config.probe_pending"),
    "deprecation": (add_pending_deprecation, "config.pipeline_name_invalid"),
    "summary": (add_pending_summary, "config.untagged"),
}


@pytest.mark.parametrize("case", sorted(PENDING_CLASSES))
def test_at4_release_ready_refuses_each_pending_class_that_check_accepts(tree, case) -> None:
    clean = pin_cli(tree, "--release-ready")
    assert clean.returncode == 0, clean.stderr[-800:]
    add, name = PENDING_CLASSES[case]
    add(tree)
    check = pin_cli(tree, "--check")
    assert check.returncode == 0, check.stderr[-800:]
    ready = pin_cli(tree, "--release-ready")
    assert ready.returncode == 1 and name in ready.stderr, ready.stdout + ready.stderr[-800:]


def test_at1b_a_marked_summary_and_a_first_deprecation_pass_check_together(tree) -> None:
    add_pending_deprecation(tree)
    add_pending_summary(tree)
    assert pin_cli(tree, "--check").returncode == 0
    ready = pin_cli(tree, "--release-ready")
    assert ready.returncode == 1
    assert "config.untagged" in ready.stderr and "config.pipeline_name_invalid" in ready.stderr


def test_at1b_unmarked_edits_fail_the_pin_cli(tree) -> None:
    edit_file(tree / CONFIG1, UNTAGGED, '"A validation failure that carries no registered code."')
    res = pin_cli(tree, "--check")
    assert res.returncode == 1 and "config.untagged: summary" in res.stderr, res.stderr[-800:]


def test_at2_a_changed_value_under_its_old_digest_fails_the_pin_cli(tree) -> None:
    old = '_d("top_level.bad_mode", "Top level bad mode.")'
    edit_file(tree / CONFIG1, old, old[:-1] + ", kind=Kind.WARNING)")
    res = pin_cli(tree, "--check")
    assert res.returncode == 1 and "config.top_level.bad_mode: kind" in res.stderr, res.stderr[
        -800:
    ]


def test_at2_a_flipped_digest_nibble_under_the_live_value_fails_the_pin_cli(tree) -> None:
    pin_path = tree / "tests/fixtures/f29/reason-registry-release.json"
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    record = pin["identities"][7]
    digest = record["fields"]["summary"]
    record["fields"]["summary"] = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    pin_path.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    res = pin_cli(tree, "--check")
    name = f"{record['namespace']}.{record['code']}"
    assert res.returncode == 1 and f"{name}: summary" in res.stderr, res.stderr[-800:]
    assert re.search(r"[0-9a-f]{64}", res.stderr) is None, "messages name fields, never digests"
