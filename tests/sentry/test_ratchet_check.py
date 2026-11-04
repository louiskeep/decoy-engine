"""AT-CI0: ``scripts/dev/ratchet_check.py`` rejects every way to grow or launder a baseline.

Each case builds a scratch git repo, commits a base, edits the working tree, and runs the real
script against the base commit. Sentry ids and reasons here are synthetic.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev" / "ratchet_check.py"
REASON = "reviewed on 2026-11-04, tracked for removal"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _baseline(sentry_id: str, keys: list[str], created_in: str = "S0", reason: str = REASON) -> str:
    entries = {k: {"reason": reason} for k in keys}
    return json.dumps({"sentry_id": sentry_id, "created_in": created_in, "entries": entries})


def _registry(rows: dict[str, str]) -> str:
    return "".join(f'[[sentry]]\nid = "{i}"\nbaseline = "{p}"\n\n' for i, p in rows.items())


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


REG = "scripts/dev/sentry_registry.toml"
A = "tests/sentry/baselines/a.json"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _write(tmp_path, REG, _registry({"a": A}))
    _write(tmp_path, A, _baseline("a", ["m::f", "m::g"]))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), "--root", str(repo), "--base", "HEAD", *extra],
        capture_output=True,
        text=True,
    )


def _assert_red(repo: Path, needle: str) -> None:
    res = _run(repo)
    assert res.returncode == 1, res.stdout + res.stderr
    assert needle in res.stdout + res.stderr


def test_unchanged_baseline_passes(repo: Path) -> None:
    res = _run(repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "a: 2 key(s)" in res.stdout


def test_shrinking_a_baseline_passes(repo: Path) -> None:
    _write(repo, A, _baseline("a", ["m::f"]))
    assert _run(repo).returncode == 0


def test_baseline_gaining_a_key_fails(repo: Path) -> None:
    _write(repo, A, _baseline("a", ["m::f", "m::g", "m::new"]))
    _assert_red(repo, "m::new")


@pytest.mark.parametrize("reason", ["", "   ", "todo", "TBD.", "too short"])
def test_empty_or_placeholder_reason_fails(repo: Path, reason: str) -> None:
    _write(repo, A, _baseline("a", ["m::f"], reason=reason))
    _assert_red(repo, "reason")


def test_renamed_baseline_with_extra_key_fails(repo: Path) -> None:
    (repo / A).unlink()
    new = "tests/sentry/baselines/b.json"
    _write(repo, new, _baseline("a", ["m::f", "m::g", "m::extra"]))
    _write(repo, REG, _registry({"a": new}))
    _assert_red(repo, "moved")


def test_replaced_baseline_with_new_sentry_id_fails(repo: Path) -> None:
    _write(repo, A, _baseline("a2", ["m::f", "m::g", "m::extra"]))
    _write(repo, REG, _registry({"a2": A}))
    _assert_red(repo, "m::extra")


def test_split_whose_union_adds_a_key_fails(repo: Path) -> None:
    (repo / A).unlink()
    _write(repo, "tests/sentry/baselines/a1.json", _baseline("a1", ["m::f"]))
    _write(repo, "tests/sentry/baselines/a2.json", _baseline("a2", ["m::g", "m::extra"]))
    _write(
        repo,
        REG,
        _registry(
            {
                "a1": "tests/sentry/baselines/a1.json",
                "a2": "tests/sentry/baselines/a2.json",
            }
        ),
    )
    _assert_red(repo, "m::extra")


def test_split_keeping_the_union_passes(repo: Path) -> None:
    (repo / A).unlink()
    _write(repo, "tests/sentry/baselines/a1.json", _baseline("a1", ["m::f"]))
    _write(repo, "tests/sentry/baselines/a2.json", _baseline("a2", ["m::g"]))
    reg = {"a1": "tests/sentry/baselines/a1.json", "a2": "tests/sentry/baselines/a2.json"}
    _write(repo, REG, _registry(reg))
    assert _run(repo).returncode == 0


def test_new_baseline_without_registry_entry_fails(repo: Path) -> None:
    _write(repo, "tests/sentry/baselines/z.json", _baseline("z", ["m::z"]))
    _assert_red(repo, "not registered")


def test_registry_entry_without_baseline_file_fails(repo: Path) -> None:
    _write(repo, REG, _registry({"a": A, "b": "tests/sentry/baselines/b.json"}))
    _assert_red(repo, "no baseline file")


def test_two_ids_for_one_path_fail(repo: Path) -> None:
    _write(repo, REG, _registry({"a": A, "a-twin": A}))
    _assert_red(repo, "two sentries")


def test_baseline_id_must_match_registry_id(repo: Path) -> None:
    _write(repo, A, _baseline("other", ["m::f"]))
    _assert_red(repo, "sentry_id")


def test_new_sentry_with_its_first_inventory_in_one_diff_passes(repo: Path) -> None:
    _write(repo, "tests/sentry/baselines/n.json", _baseline("n", ["m::one"], created_in="X9"))
    _write(repo, REG, _registry({"a": A, "n": "tests/sentry/baselines/n.json"}))
    res = _run(repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "first inventory" in res.stdout


def test_env_var_does_not_allow_growth(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("RATCHET_SLICE", "RATCHET_ALLOW", "CI"):
        monkeypatch.setenv(name, "X0a")
    _write(repo, A, _baseline("a", ["m::f", "m::g", "m::new"]))
    _assert_red(repo, "m::new")


def test_growth_fails_even_when_the_registry_is_also_edited(repo: Path) -> None:
    _write(repo, A, _baseline("a", ["m::f", "m::g", "m::new"]))
    _write(repo, "tests/sentry/baselines/n.json", _baseline("n", ["m::one"]))
    _write(repo, REG, _registry({"a": A, "n": "tests/sentry/baselines/n.json"}))
    _assert_red(repo, "m::new")
