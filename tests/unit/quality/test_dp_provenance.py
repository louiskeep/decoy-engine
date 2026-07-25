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
from tests._dp_cert import is_certified_dp_env

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
    """If this environment IS the certified proof-stack, the pinned literal
    must equal the live fingerprint and the fit gate must PASS. On any other
    environment this is skipped -- there the fail-closed tests below carry the
    load.

    The guard checks the FULL predicate (`is_certified_dp_env`, which wraps
    `check_fit_environment`), not merely whether the `(platform, cpython)` key
    is present in the manifest: a host on a certified CPython patch but an
    uncertified distribution set (e.g. this suite's own `--extra dev` 71-dist
    profile on 3.10.20) has a present key with no matching fingerprint, so the
    key-only guard used to fall through to the assertion below and fail there
    instead of skipping."""
    if not is_certified_dp_env():
        pytest.skip("this environment is not the certified DP proof-stack")
    key = (prov.current_platform(), prov.current_cpython())
    assert (
        prov.compute_lock_fingerprint(prov.installed_distribution_set())
        in prov._CERTIFIED_STACKS[key]
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


def test_check_fit_environment_accepts_each_member_rejects_non_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fit-gate membership, deterministic (independent of which venv runs the
    # test): every pinned member of a certified row passes check_fit_environment,
    # while a well-formed non-member and an empty set both fail closed. The lock
    # guard is stubbed so this isolates the membership decision from the
    # installed-vs-lock check that has its own tests.
    (key,) = list(prov._CERTIFIED_STACKS.items())
    (plat, cpython), members = key
    assert len(members) >= 2  # engine dev/CI profile + certified CLI runtime
    monkeypatch.setattr(prov, "current_platform", lambda: plat)
    monkeypatch.setattr(prov, "current_cpython", lambda: cpython)
    monkeypatch.setattr(prov, "assert_lock_matches_installed", lambda *a, **k: None)

    for fp in members:
        monkeypatch.setattr(prov, "compute_lock_fingerprint", lambda _s, _fp=fp: _fp)
        prov.check_fit_environment()  # a pinned member must not raise

    monkeypatch.setattr(prov, "compute_lock_fingerprint", lambda _s: "0" * 64)
    with pytest.raises(ProvenanceError) as exc:
        prov.check_fit_environment()
    assert exc.value.code == "dp_stack_uncertified"

    monkeypatch.setattr(prov, "_CERTIFIED_STACKS", {(plat, cpython): frozenset()})
    monkeypatch.setattr(prov, "compute_lock_fingerprint", lambda _s: next(iter(members)))
    with pytest.raises(ProvenanceError) as exc_empty:
        prov.check_fit_environment()
    assert exc_empty.value.code == "dp_stack_uncertified"


# ---------------------------------------------------------------------------
# Generation-time gate: validate a RECORDED identity (no local recompute).
# ---------------------------------------------------------------------------


def _certified_record() -> dict[str, Any]:
    (key,) = list(prov._CERTIFIED_STACKS.items())
    (plat, cpython), fingerprints = key
    return {
        "platform": plat._asdict(),
        "cpython": cpython,
        "fingerprint": sorted(fingerprints)[0],
    }


def test_validate_recorded_provenance_accepts_certified_record() -> None:
    prov.validate_recorded_provenance(_certified_record())  # must not raise


def test_validate_recorded_provenance_accepts_provenance_and_triple_objects() -> None:
    (key,) = list(prov._CERTIFIED_STACKS.items())
    (plat, cpython), fingerprints = key
    fingerprint = sorted(fingerprints)[0]
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


def test_certified_row_admits_multiple_fingerprints() -> None:
    """A certified row maps to a SET of fingerprints (the engine build env plus
    the certified CLI env), matched by membership, not equality. Every pinned
    member must validate on that key; an unpinned fingerprint must not."""
    (key,) = list(prov._CERTIFIED_STACKS.items())
    (plat, cpython), fingerprints = key
    assert len(fingerprints) >= 2  # engine build env + certified CLI env
    for fp in fingerprints:
        prov.validate_recorded_provenance(
            {"platform": plat._asdict(), "cpython": cpython, "fingerprint": fp}
        )  # each pinned member validates
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(
            {"platform": plat._asdict(), "cpython": cpython, "fingerprint": "0" * 64}
        )
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


@pytest.mark.dp_certified
def test_assert_lock_matches_installed_passes_on_real_env() -> None:
    """The synced .venv must be exactly the marker-selected uv.lock pins.
    A failure here means the environment drifted from the lock -- which is
    precisely what the fingerprint must never be computed over. Marked
    dp_certified: only the certified profile is frozen-synced to the lock;
    the full-suite CI jobs install unpinned deps, so the conftest hook skips
    this off-stack (the dp-certified-gate runs it, and check_fit_environment
    there asserts the same lock-match)."""
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
    # The offending name==version must survive into the report, not be nulled.
    assert "foo==2.0" in exc.value.message


def test_assert_lock_matches_installed_detects_stray_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("bar", "1.0"),))
    lock = _write_lock(tmp_path, '[[package]]\nname = "foo"\nversion = "1.0"\n')
    with pytest.raises(ProvenanceError) as exc:
        prov.assert_lock_matches_installed(lock_path=lock)
    assert exc.value.code == "dp_lock_installed_mismatch"
    assert "stray" in exc.value.message
    # The offending name==version must survive into the report, not be nulled.
    assert "bar==1.0" in exc.value.message


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


# ---------------------------------------------------------------------------
# TQ crown-jewels mutation-kill pass (2026-07-25).
#
# The tests above validate the fail-closed GATE logic by monkeypatching the
# real fingerprint / distribution-set / platform helpers, so mutations INSIDE
# those helpers survive. The tests below grade those implementation functions
# directly, plus the uncovered malformed-record and lock-guard branches, to
# kill the LOGIC survivors from the baseline mutmut run. See
# docs/quality/mutation-ledgers/quality_dp_provenance.md.
# ---------------------------------------------------------------------------


def test_compute_lock_fingerprint_known_answer() -> None:
    # Pins the exact pre-hash serialization (sorted name==version per line,
    # newline-joined) to a hand-computed sha256, so any change to the separator
    # bytes changes the digest and fails here.
    dist_set = [("beta", "2.0"), ("alpha", "1.0")]
    assert (
        prov.compute_lock_fingerprint(dist_set)
        == "1aae3c6af7745e831aea5a0fcea2383818a79ad75aee7b65db642b6f5aaf95e0"
    )


def test_canonical_serialization_is_bare_newline_joined_sorted() -> None:
    # The separator is a bare newline, not a wrapped or alternate token.
    assert prov._canonical_serialization([("beta", "2.0"), ("alpha", "1.0")]) == (
        "alpha==1.0\nbeta==2.0"
    )


def test_installed_distribution_set_processes_dists_after_a_collapsed_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A same-version duplicate is collapsed and iteration must CONTINUE, so a
    # distribution listed after the duplicate is still included.
    monkeypatch.setattr(
        prov.importlib.metadata,
        "distributions",
        lambda: iter([_FakeDist("aaa", "1.0"), _FakeDist("aaa", "1.0"), _FakeDist("zzz", "2.0")]),
    )
    assert prov.installed_distribution_set() == (("aaa", "1.0"), ("zzz", "2.0"))


def test_current_platform_libc_is_the_real_family_not_unknown() -> None:
    # libc comes from libc_ver()[0] with an "unknown" fallback; on this glibc
    # host it is the real family and never the fallback sentinel.
    import platform as _platform

    expected = _platform.libc_ver()[0] or "unknown"
    assert prov.current_platform().libc == expected
    assert prov.current_platform().libc != "unknown"


def test_current_platform_libc_falls_back_to_unknown_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When libc_ver reports no family (musl / non-glibc), libc is exactly the
    # literal "unknown" sentinel, which is what makes such a host != certified.
    monkeypatch.setattr(prov.platform, "libc_ver", lambda: ("", "", ""))
    assert prov.current_platform().libc == "unknown"


def test_validate_recorded_provenance_rejects_platform_of_unsupported_type() -> None:
    # A recorded platform that is neither a PlatformTriple, mapping, nor 4-seq
    # hits the else branch; its coded error is dp_provenance_record_malformed.
    record = _certified_record()
    record["platform"] = 42
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(record)
    assert exc.value.code == "dp_provenance_record_malformed"


def test_validate_recorded_provenance_rejects_platform_fields_not_all_strings() -> None:
    # A 4-element platform sequence whose fields are not all strings hits the
    # all-strings guard; coded dp_provenance_record_malformed.
    record = _certified_record()
    record["platform"] = ["linux", "x86_64", "CPython", 123]
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(record)
    assert exc.value.code == "dp_provenance_record_malformed"


def test_assert_lock_matches_installed_no_package_array_is_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lockfile that parses but has no [[package]] array is a coded parse error.
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "1.0"),))
    lock = _write_lock(tmp_path, "version = 1\n")
    with pytest.raises(ProvenanceError) as exc:
        prov.assert_lock_matches_installed(lock_path=lock)
    assert exc.value.code == "dp_lock_parse_error"


def test_assert_lock_matches_installed_skips_entries_missing_name_or_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An entry missing name OR version must be skipped, not indexed; a valid
    # entry alongside it still drives the drift check. An or->and swap in the
    # shape guard would dereference the missing key and crash instead.
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "2.0"),))
    lock = _write_lock(
        tmp_path,
        '[[package]]\nname = "foo"\nversion = "1.0"\n\n'
        '[[package]]\nname = "bar"\n\n'  # missing version
        '[[package]]\nversion = "9.9"\n',  # missing name
    )
    with pytest.raises(ProvenanceError) as exc:
        prov.assert_lock_matches_installed(lock_path=lock)
    assert exc.value.code == "dp_lock_installed_mismatch"
    assert "foo==2.0 (lock:" in exc.value.message


def test_assert_lock_matches_installed_continues_past_a_malformed_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A malformed entry (no name, no version) is skipped WITHOUT abandoning the
    # loop, so a valid entry after it is still indexed.
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "1.0"),))
    lock = _write_lock(
        tmp_path,
        '[[package]]\nlicense = "MIT"\n\n'  # neither name nor version
        '[[package]]\nname = "foo"\nversion = "1.0"\n',
    )
    prov.assert_lock_matches_installed(lock_path=lock)  # foo matches; must not raise


def test_assert_lock_matches_installed_honors_a_true_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An entry whose marker is TRUE here must be indexed as active, so a matching
    # installed dist is not flagged a stray. Nulling _any_marker_true's args
    # would treat every marked entry as inactive.
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "1.0"),))
    lock = _write_lock(
        tmp_path,
        '[[package]]\nname = "foo"\nversion = "1.0"\n'
        f"resolution-markers = [\"sys_platform == '{sys.platform}'\"]\n",
    )
    prov.assert_lock_matches_installed(lock_path=lock)  # marker true here; must not raise


def test_assert_lock_matches_installed_continues_past_a_marker_inactive_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A marker-inactive entry is skipped WITHOUT abandoning the loop, so a valid
    # entry after it is still indexed.
    monkeypatch.setattr(prov, "installed_distribution_set", lambda: (("foo", "1.0"),))
    lock = _write_lock(
        tmp_path,
        '[[package]]\nname = "winonly"\nversion = "1.0"\n'
        "resolution-markers = [\"sys_platform == 'win32'\"]\n\n"
        '[[package]]\nname = "foo"\nversion = "1.0"\n',
    )
    prov.assert_lock_matches_installed(lock_path=lock)  # foo matches; must not raise


def test_any_marker_true_evaluates_a_true_marker() -> None:
    from packaging.markers import Marker

    # A list with a marker satisfied on THIS host returns True.
    assert prov._any_marker_true([f"sys_platform == '{sys.platform}'"], Marker) is True


def test_any_marker_true_false_marker_is_not_satisfied() -> None:
    from packaging.markers import Marker

    # A marker not satisfied here returns False.
    assert prov._any_marker_true(["sys_platform == 'win32'"], Marker) is False


def test_any_marker_true_non_list_is_false() -> None:
    from packaging.markers import Marker

    # A non-list markers value cannot make a version active.
    assert prov._any_marker_true("not-a-list", Marker) is False
    assert prov._any_marker_true(None, Marker) is False


def test_any_marker_true_ignores_non_string_elements_but_evaluates_the_rest() -> None:
    from packaging.markers import Marker

    # A non-str element is skipped (not evaluated, not loop-ending); a later true
    # marker still returns True.
    assert prov._any_marker_true([123, f"sys_platform == '{sys.platform}'"], Marker) is True


def test_any_marker_true_malformed_marker_is_not_satisfied_and_does_not_stop() -> None:
    from packaging.markers import Marker

    # A malformed marker is fail-closed (not-satisfied) and does not abort the
    # scan, so a later true marker still returns True.
    assert prov._any_marker_true(["!!! not a marker", "x ==="], Marker) is False
    assert (
        prov._any_marker_true(["!!! not a marker", f"sys_platform == '{sys.platform}'"], Marker)
        is True
    )
