"""Tests for the certified lock-fingerprint provenance gate (DPS-CODEC phase 3).

The gate exists because an artifact's (epsilon, delta) guarantee is only honest
on the exact software stack it was tested against (guide
docs/plans/2026-07-23-dps-codec-implementation-guide.md, section 3.8). These
tests pin the mechanical properties that make the gate trustworthy:
determinism + canonicalization + duplicate rejection of the fingerprint;
fail-closed fit-time refusal on a wrong platform or drifted stack;
generation-time accept/reject against the recorded identity; and the
lock==installed reproducibility guard (Codex round-6).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.quality import dp_provenance as prov
from decoy_engine.quality.dp_provenance import (
    CERTIFIED_PLATFORM,
    PlatformTriple,
    Provenance,
    ProvenanceError,
)

# ---------------------------------------------------------------------------
# Fingerprint: determinism, order-independence, canonicalization, duplicates.
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic() -> None:
    dist_set = prov.installed_distribution_set()
    assert prov.compute_lock_fingerprint(dist_set) == prov.compute_lock_fingerprint(dist_set)


def test_fingerprint_is_order_independent() -> None:
    forward = [("alpha", "1.0"), ("beta", "2.0"), ("gamma", "3.0")]
    shuffled = [("gamma", "3.0"), ("alpha", "1.0"), ("beta", "2.0")]
    assert prov.compute_lock_fingerprint(forward) == prov.compute_lock_fingerprint(shuffled)


def test_fingerprint_changes_when_a_version_changes() -> None:
    base = [("alpha", "1.0"), ("beta", "2.0")]
    bumped = [("alpha", "1.0"), ("beta", "2.1")]
    assert prov.compute_lock_fingerprint(base) != prov.compute_lock_fingerprint(bumped)


def test_fingerprint_changes_when_a_distribution_is_added() -> None:
    base = [("alpha", "1.0")]
    plus = [("alpha", "1.0"), ("extra", "0.1")]
    assert prov.compute_lock_fingerprint(base) != prov.compute_lock_fingerprint(plus)


def test_installed_distribution_set_is_sorted_canonical_and_unique() -> None:
    from packaging.utils import canonicalize_name

    pairs = prov.installed_distribution_set()
    names = [name for name, _ in pairs]
    assert names == sorted(names), "not sorted"
    assert len(names) == len(set(names)), "duplicate canonical names slipped through"
    for name in names:
        assert name == canonicalize_name(name), f"{name!r} is not PEP-503-canonical"


class _FakeDist:
    def __init__(self, name: str | None, version: str = "1.0") -> None:
        self.metadata = {"Name": name}
        self.version = version


def test_installed_distribution_set_rejects_duplicate_canonical_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PyYAML and pyyaml canonicalize to the same name -> ambiguous fingerprint.
    monkeypatch.setattr(
        prov.importlib.metadata,
        "distributions",
        lambda: iter([_FakeDist("PyYAML", "6.0"), _FakeDist("pyyaml", "6.1")]),
    )
    with pytest.raises(ProvenanceError) as exc:
        prov.installed_distribution_set()
    assert exc.value.code == "dp_provenance_duplicate_distribution"


def test_installed_distribution_set_collapses_same_version_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A leftover same-version dist-info produces an identical name==version line,
    # so it is not ambiguous for the hash -- collapse to one entry, do NOT raise
    # (dennis phase-3 LOW-2). Only DIFFERENT versions of one name are ambiguous.
    monkeypatch.setattr(
        prov.importlib.metadata,
        "distributions",
        lambda: iter([_FakeDist("PyYAML", "6.0"), _FakeDist("pyyaml", "6.0")]),
    )
    result = prov.installed_distribution_set()
    assert result == (("pyyaml", "6.0"),)


def test_installed_distribution_set_rejects_unnamed_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        prov.importlib.metadata,
        "distributions",
        lambda: iter([_FakeDist(None)]),
    )
    with pytest.raises(ProvenanceError) as exc:
        prov.installed_distribution_set()
    assert exc.value.code == "dp_provenance_distribution_unnamed"


# ---------------------------------------------------------------------------
# Platform + interpreter identity.
# ---------------------------------------------------------------------------


def test_current_platform_shape() -> None:
    plat = prov.current_platform()
    assert isinstance(plat, PlatformTriple)
    assert all(isinstance(field, str) and field for field in plat)


def test_current_cpython_is_full_patch_and_releaselevel() -> None:
    cpython = prov.current_cpython()
    info = sys.version_info
    assert cpython == f"{info.major}.{info.minor}.{info.micro}.{info.releaselevel}"
    # full patch, not a bare minor (section 3.8 round-5)
    assert cpython.count(".") == 3


def test_current_provenance_is_boundary_independent_identity() -> None:
    identity = prov.current_provenance()
    assert isinstance(identity, Provenance)
    assert identity.platform == prov.current_platform()
    assert identity.cpython == prov.current_cpython()
    assert identity.fingerprint == prov.compute_lock_fingerprint(prov.installed_distribution_set())


# ---------------------------------------------------------------------------
# The pinned certified row: on the certification host, the live env IS a row.
# ---------------------------------------------------------------------------


def test_pinned_row_matches_this_env_when_this_env_is_certified() -> None:
    """If (this platform, this cpython) is a certified row, the pinned literal
    must equal the live fingerprint and the fit gate must PASS. On any other
    interpreter/patch (e.g. a CI python whose patch is not yet certified) this
    is skipped -- there the fail-closed tests below carry the load."""
    key = (prov.current_platform(), prov.current_cpython())
    if key not in prov._CERTIFIED_STACKS:
        pytest.skip(f"this environment {key} is not a pinned certified row")
    assert prov._CERTIFIED_STACKS[key] == prov.compute_lock_fingerprint(
        prov.installed_distribution_set()
    )
    prov.check_fit_environment()  # must not raise


# ---------------------------------------------------------------------------
# Fit-time gate: fail closed on wrong platform / wrong stack.
# ---------------------------------------------------------------------------


def test_check_fit_environment_rejects_wrong_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong = PlatformTriple(system="win32", machine="AMD64", implementation="CPython", libc="")
    monkeypatch.setattr(prov, "current_platform", lambda: wrong)
    with pytest.raises(ProvenanceError) as exc:
        prov.check_fit_environment()
    assert exc.value.code == "dp_platform_uncertified"


def test_check_fit_environment_rejects_pypy_as_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pypy = PlatformTriple(system="linux", machine="x86_64", implementation="PyPy", libc="glibc")
    monkeypatch.setattr(prov, "current_platform", lambda: pypy)
    with pytest.raises(ProvenanceError) as exc:
        prov.check_fit_environment()
    assert exc.value.code == "dp_platform_uncertified"


def test_check_fit_environment_rejects_uncertified_cpython_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prov, "current_platform", lambda: CERTIFIED_PLATFORM)
    monkeypatch.setattr(prov, "current_cpython", lambda: "9.9.9.final")
    with pytest.raises(ProvenanceError) as exc:
        prov.check_fit_environment()
    assert exc.value.code == "dp_stack_uncertified"


def test_check_fit_environment_rejects_drifted_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Platform + cpython land on a real certified row, but the installed set
    # drifted, so the live fingerprint no longer matches the pinned literal.
    (certified_cpython,) = [c for (_p, c) in prov._CERTIFIED_STACKS]
    monkeypatch.setattr(prov, "current_platform", lambda: CERTIFIED_PLATFORM)
    monkeypatch.setattr(prov, "current_cpython", lambda: certified_cpython)
    monkeypatch.setattr(
        prov, "installed_distribution_set", lambda: (("smuggled-package", "0.0.0"),)
    )
    with pytest.raises(ProvenanceError) as exc:
        prov.check_fit_environment()
    assert exc.value.code == "dp_stack_uncertified"


# ---------------------------------------------------------------------------
# Generation-time gate: validate a RECORDED identity (no local recompute).
# ---------------------------------------------------------------------------


def _certified_record() -> dict[str, Any]:
    (key,) = list(prov._CERTIFIED_STACKS.items())
    (plat, cpython), fingerprint = key
    return {
        "platform": plat._asdict(),
        "cpython": cpython,
        "fingerprint": fingerprint,
    }


def test_validate_recorded_provenance_accepts_certified_record() -> None:
    prov.validate_recorded_provenance(_certified_record())  # must not raise


def test_validate_recorded_provenance_accepts_provenance_and_triple_objects() -> None:
    (key,) = list(prov._CERTIFIED_STACKS.items())
    (plat, cpython), fingerprint = key
    prov.validate_recorded_provenance(
        Provenance(platform=plat, cpython=cpython, fingerprint=fingerprint)
    )
    prov.validate_recorded_provenance(
        {"platform": plat, "cpython": cpython, "fingerprint": fingerprint}
    )


def test_validate_recorded_provenance_accepts_platform_as_sequence() -> None:
    record = _certified_record()
    record["platform"] = list(CERTIFIED_PLATFORM)  # 4-element sequence form
    prov.validate_recorded_provenance(record)


def test_validate_recorded_provenance_rejects_wrong_platform() -> None:
    record = _certified_record()
    record["platform"] = {
        "system": "darwin",
        "machine": "arm64",
        "implementation": "CPython",
        "libc": "",
    }
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(record)
    assert exc.value.code == "dp_platform_uncertified"


def test_validate_recorded_provenance_rejects_wrong_fingerprint() -> None:
    record = _certified_record()
    record["fingerprint"] = "deadbeef" * 8
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(record)
    assert exc.value.code == "dp_stack_uncertified"


def test_validate_recorded_provenance_rejects_uncertified_cpython() -> None:
    record = _certified_record()
    record["cpython"] = "3.9.0.final"
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(record)
    assert exc.value.code == "dp_stack_uncertified"


@pytest.mark.parametrize(
    "bad",
    [
        42,
        "not-a-mapping",
        {"cpython": "3.10.20.final", "fingerprint": "x"},  # missing platform
        {"platform": [1, 2, 3], "cpython": "x", "fingerprint": "y"},  # 3-elem seq
        {"platform": CERTIFIED_PLATFORM, "cpython": 3, "fingerprint": "y"},  # non-str
        {"platform": {"system": "linux"}, "cpython": "x", "fingerprint": "y"},  # partial
    ],
)
def test_validate_recorded_provenance_rejects_malformed_records(bad: Any) -> None:
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(bad)
    assert exc.value.code == "dp_provenance_record_malformed"


# ---------------------------------------------------------------------------
# Reproducibility guard: lock == installed (Codex round-6).
# ---------------------------------------------------------------------------


def test_assert_lock_matches_installed_passes_on_real_env() -> None:
    """The synced .venv must be exactly the marker-selected uv.lock pins.
    A failure here means the environment drifted from the lock -- which is
    precisely what the fingerprint must never be computed over."""
    prov.assert_lock_matches_installed()


def test_assert_lock_matches_installed_missing_lock_is_parse_error() -> None:
    with pytest.raises(ProvenanceError) as exc:
        prov.assert_lock_matches_installed(lock_path="/nonexistent/uv.lock")
    assert exc.value.code == "dp_lock_parse_error"


def _write_lock(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "uv.lock"
    path.write_text(body, encoding="utf-8")
    return path


def test_assert_lock_matches_installed_accepts_matching_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "1.0"),))
    lock = _write_lock(
        tmp_path,
        '[[package]]\nname = "foo"\nversion = "1.0"\n\n'
        '[[package]]\nname = "docs-only"\nversion = "2.0"\n',  # lock-only extra: fine
    )
    prov.assert_lock_matches_installed(lock_path=lock)


def test_assert_lock_matches_installed_detects_version_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "2.0"),))
    lock = _write_lock(tmp_path, '[[package]]\nname = "foo"\nversion = "1.0"\n')
    with pytest.raises(ProvenanceError) as exc:
        prov.assert_lock_matches_installed(lock_path=lock)
    assert exc.value.code == "dp_lock_installed_mismatch"
    assert "drift" in exc.value.message


def test_assert_lock_matches_installed_detects_stray_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("bar", "1.0"),))
    lock = _write_lock(tmp_path, '[[package]]\nname = "foo"\nversion = "1.0"\n')
    with pytest.raises(ProvenanceError) as exc:
        prov.assert_lock_matches_installed(lock_path=lock)
    assert exc.value.code == "dp_lock_installed_mismatch"
    assert "stray" in exc.value.message


def test_assert_lock_matches_installed_respects_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # foo is pinned only for win32 in the lock; on this (linux) host it is not
    # marker-active, so an installed foo is a stray, proving marker evaluation.
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "1.0"),))
    lock = _write_lock(
        tmp_path,
        '[[package]]\nname = "foo"\nversion = "1.0"\n'
        "resolution-markers = [\"sys_platform == 'win32'\"]\n",
    )
    with pytest.raises(ProvenanceError) as exc:
        prov.assert_lock_matches_installed(lock_path=lock)
    assert exc.value.code == "dp_lock_installed_mismatch"


# ---------------------------------------------------------------------------
# Import isolation: the provenance module must not pull pandas/pyarrow, so it
# is safe on the direct-carrier path (section 3.8 MEDIUM).
# ---------------------------------------------------------------------------

_ISOLATION_SCRIPT = r"""
import sys, types, os
src = os.environ["DECOY_SRC"]
sys.path.insert(0, src)

stub = types.ModuleType("decoy_engine")
stub.__path__ = [os.path.join(src, "decoy_engine")]
sys.modules["decoy_engine"] = stub

from decoy_engine.quality import dp_provenance
# Exercise the fingerprint + gate helpers that the direct path would touch.
dp_provenance.current_provenance()
dp_provenance.current_platform()
dp_provenance.current_cpython()

leaked = sorted(m for m in sys.modules if m == "pandas" or m.startswith("pandas."))
leaked += sorted(m for m in sys.modules if m == "pyarrow" or m.startswith("pyarrow."))
assert not leaked, "dp_provenance pulled: " + ", ".join(leaked)
print("ISOLATION_OK")
"""


def test_dp_provenance_imports_neither_pandas_nor_pyarrow() -> None:
    import os

    repo_root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "DECOY_SRC": str(repo_root / "src")}
    proc = subprocess.run(  # noqa: S603  # fixed argv, our own interpreter + script constant
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    assert "ISOLATION_OK" in proc.stdout, proc.stdout
