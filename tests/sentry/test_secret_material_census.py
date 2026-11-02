"""Sentry: the secret-material census is derived from the code and every verdict holds.

`scripts/secret_material_census.py` lists, from the AST of `src/decoy_engine` (no hand-picked
list), every function, closure and class field where secret material can be bound to a name a
traceback frame keeps. `tests/sentry/secret_material_census.json` is that list with a reviewed
verdict per entry. This sentry re-derives the list and fails when:

- a binding exists that the census lacks (a new function taking or deriving a key);
- a census entry is stale (the binding moved, changed or is gone);
- a verdict is not borne out by the code (`scripts/_secret_census_verdicts.py`): e.g. "wiped"
  with no `except BaseException: del <name>; raise` covering the frame.

After a change: `python scripts/secret_material_census.py --write` keeps every reviewed verdict
and marks new entries `unreviewed`, which fails here until a verdict is chosen and holds.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _secret_census_verdicts as verdicts  # noqa: E402
import secret_material_census as census  # noqa: E402

SEEDED = Path(__file__).parent / "fixtures" / "secret_census_seeded.py"


@pytest.fixture(scope="module")
def scratch_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("census") / "decoy_engine"
    shutil.copytree(census.SRC, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _with_seed(root: Path, source: str) -> Path:
    (root / "_census_seeded.py").write_text(source, encoding="utf-8")
    return root


def _seeded_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "file": "decoy_engine/_census_seeded.py",
        "function": "verify_upload",
        "name": "signing_key",
        "arrival": "parameter",
        "line": 10,
        "verdict": "wiped",
        "reason": "",
    }
    row.update(overrides)
    return row


def test_the_census_matches_the_code() -> None:
    missing, stale = census.drift(census.derive(), census.load_census())
    assert not missing, "secret material the census lacks (run the script with --write): " + str(
        [f"{e.file}:{e.line} {e.function} {e.name} ({e.arrival})" for e in missing]
    )
    assert not stale, "census entries the code no longer bears out: " + str(
        [f"{r['file']}:{r['line']} {r['function']} {r['name']}" for r in stale]
    )


def test_every_verdict_holds() -> None:
    assert verdicts.check_census(census.load_census()) == []


def test_the_census_holds_the_round_5_sites() -> None:
    names = {(r["file"], r["function"], r["name"]) for r in census.load_census()}
    assert (
        "decoy_engine/profiling/model_pack/loader.py",
        "ModelPackLoader._check_signature",
        "key_box",
    ) in names
    assert (
        "decoy_engine/profiling/model_pack/provenance.py",
        "derive_pack_signing_key",
        "master",
    ) in names
    assert ("decoy_engine/profiling/model_pack/provenance.py", "sign_pack", "key") in names


def test_a_new_secret_function_is_missing_from_the_census(scratch_tree: Path) -> None:
    root = _with_seed(scratch_tree, SEEDED.read_text(encoding="utf-8"))
    missing, stale = census.drift(census.derive(root), census.load_census())
    assert stale == []
    assert [(e.file, e.function, e.name, e.arrival) for e in missing] == [
        ("decoy_engine/_census_seeded.py", "verify_upload", "signing_key", "parameter")
    ]


def test_a_census_claim_the_seeded_function_does_not_bear_out_fails(scratch_tree: Path) -> None:
    root = _with_seed(scratch_tree, SEEDED.read_text(encoding="utf-8"))
    assert verdicts.check_census([_seeded_row()], root) == [
        "decoy_engine/_census_seeded.py:10 verify_upload signing_key: "
        "no wiping try covers the frame"
    ]


def test_the_seeded_function_once_wiped_passes(scratch_tree: Path) -> None:
    fixed = SEEDED.read_text(encoding="utf-8").replace(
        """    if not payload:
        raise ValueError("empty payload")""",
        """    try:
        if not payload:
            raise ValueError("empty payload")
    except BaseException:
        del signing_key
        raise""",
    )
    root = _with_seed(scratch_tree, fixed)
    assert verdicts.check_census([_seeded_row()], root) == []


@pytest.mark.parametrize(
    ("row", "why"),
    [
        (_seeded_row(verdict="unreviewed"), "is not one of"),
        (_seeded_row(verdict="no-raise"), "needs a reason"),
        (_seeded_row(verdict="not-secret", reason="x"), "cannot be not-secret"),
        (_seeded_row(verdict="boxed"), "not boxed"),
    ],
)
def test_an_unsupported_verdict_fails(scratch_tree: Path, row: dict[str, Any], why: str) -> None:
    root = _with_seed(scratch_tree, SEEDED.read_text(encoding="utf-8"))
    [problem] = verdicts.check_census([row], root)
    assert why in problem
