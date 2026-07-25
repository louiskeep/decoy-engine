"""Property + metamorphic invariants for the DP certification / provenance gate.

TQ (Test-Quality Program) pass on `quality/dp_provenance.py`. The existing
`tests/unit/quality/test_dp_provenance.py` suite is example-based and already
exercises every coded failure mode with hand-picked inputs (wrong platform,
one wrong cpython patch, one drifted fingerprint, one malformed record shape,
...). Coverage from those examples does not prove a NEARBY input is still
refused -- a mutation could loosen a boundary (`!=` -> `not in`, `and` -> `or`,
a dropped `not`) and every hand-picked example would still pass while the
class of inputs around it silently starts validating. These are oracles for
that gap: state the invariant, let Hypothesis search the input space instead
of enumerating it.

The module is the fail-closed gate that makes a DP `(epsilon, delta)`
guarantee honest only on the exact software stack it was tested against
(module docstring, and `docs/plans/2026-07-23-dps-codec-implementation-guide.md`
sections 3.8 and 4). A bug that lets an off-stack environment validate is a
false privacy guarantee -- the worst-blast-radius failure mode a test suite
for this module can miss.

INVARIANTS (source: the module's own docstring + each function's docstring):

- FAIL-CLOSED membership is EXACT. `check_fit_environment` /
  `validate_recorded_provenance` accept only an exact
  `(platform, cpython, fingerprint)` row in `_CERTIFIED_STACKS`; any platform
  field off, any cpython string off, any fingerprint off -- including a stack
  that differs from a certified one by exactly ONE installed distribution --
  is refused, never silently accepted (section 3.8: "safe but strict").
- DETERMINISM. `compute_lock_fingerprint` and `installed_distribution_set`
  are pure: the same input (or the same underlying `importlib.metadata`
  state) yields byte-identical output on every call, and iteration order of
  the underlying distributions never changes the result (both are sorted
  internally; this is what keeps a certified fingerprint reproducible run to
  run, per the module's "STOP enumerating, certify a mechanical fingerprint"
  design note).
- SENSITIVITY (no collision). Changing any single distribution's version,
  adding a distribution, or removing one changes the fingerprint -- a
  tampered or drifted stack must never coincide with a certified hash.
  Generalizes the module's own duplicate-name guard (`dp_provenance_
  duplicate_distribution`, tested here for ANY canonicalization-colliding
  name pair, not just the one hardcoded PyYAML/pyyaml example in the unit
  suite) into "two DIFFERENT canonical identities never collapse to one
  entry silently."
- `assert_lock_matches_installed`: an installed set that is an exact subset
  of the marker-active lock passes; any version drift or any stray
  (installed, not lock-selected) distribution raises
  `dp_lock_installed_mismatch` -- for arbitrary generated lock contents, not
  one hand-built lockfile.

None of the tests below require running ON the certified DP proof-stack: each
one MONKEYPATCHES `current_platform` / `current_cpython` /
`compute_lock_fingerprint` / `installed_distribution_set` to synthesize the
certified (or near-miss) condition, following the same pattern the existing
suite uses for its own fail-closed unit tests (e.g.
`test_check_fit_environment_accepts_each_member_rejects_non_member`). None are
marked `dp_certified` for that reason -- see `tests/_dp_cert.py` /
`tests/conftest.py`'s `pytest_collection_modifyitems` for the marker that
gates the one test in the existing suite that DOES need the real synced
`.venv` (`test_assert_lock_matches_installed_passes_on_real_env`), which this
module does not duplicate.

Run:  pytest tests/property/test_dp_provenance_invariants.py -q
"""

from __future__ import annotations

import string
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from decoy_engine.quality import dp_provenance as prov
from decoy_engine.quality.dp_provenance import (
    CERTIFIED_PLATFORM,
    PlatformTriple,
    ProvenanceError,
)

# Match the pilot's audit profile: more examples than the 100-example default,
# no deadline (Hypothesis shrinking + hashing/TOML-writing can trip the 200ms
# wall), and print_blob so any counterexample is replayable.
settings.register_profile(
    "audit",
    max_examples=300,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("audit")


class _FakeDist:
    """A minimal `importlib.metadata.Distribution` stand-in: only
    `.metadata["Name"]` and `.version` are read by `installed_distribution_set`."""

    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


# ---------------------------------------------------------------------------
# Strategies. Names are restricted to lowercase-alnum (PEP 503 canonical
# already, no `-`/`_`/`.` separators): a name with separators can
# canonicalization-collide with a DIFFERENT raw name (that is its own
# dedicated property below, built deliberately), which would otherwise make
# the general determinism/sensitivity strategies flaky by accident. This
# mirrors how `installed_distribution_set` actually feeds
# `compute_lock_fingerprint`: by the time a pair reaches the fingerprint, it
# is already PEP-503-canonical.
# ---------------------------------------------------------------------------

_NAME_ALPHABET = string.ascii_lowercase + string.digits


def _dist_name() -> st.SearchStrategy[str]:
    return st.text(alphabet=_NAME_ALPHABET, min_size=2, max_size=12)


def _dist_version() -> st.SearchStrategy[str]:
    return st.lists(st.integers(min_value=0, max_value=999), min_size=1, max_size=4).map(
        lambda parts: ".".join(str(p) for p in parts)
    )


def _dist_set(min_size: int = 0, max_size: int = 12) -> st.SearchStrategy[list[tuple[str, str]]]:
    return st.lists(
        st.tuples(_dist_name(), _dist_version()),
        min_size=min_size,
        max_size=max_size,
        unique_by=lambda pair: pair[0],
    )


# ---------------------------------------------------------------------------
# DETERMINISM: compute_lock_fingerprint / installed_distribution_set are pure
# and order-independent.
# ---------------------------------------------------------------------------


@given(_dist_set())
def test_compute_lock_fingerprint_is_deterministic(pairs) -> None:
    """Metamorphic: the same distribution set fingerprints identically on
    every call. A fingerprint that drifted between calls could never anchor a
    static certified manifest."""
    first = prov.compute_lock_fingerprint(pairs)
    second = prov.compute_lock_fingerprint(pairs)
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)


@given(_dist_set(), st.randoms(use_true_random=False))
def test_compute_lock_fingerprint_is_order_independent(pairs, rng) -> None:
    """Metamorphic: shuffling the input pairs leaves the fingerprint
    byte-identical (`_canonical_serialization` sorts before hashing), so
    config/registry iteration order never perturbs a certified hash."""
    shuffled = list(pairs)
    rng.shuffle(shuffled)
    assert prov.compute_lock_fingerprint(pairs) == prov.compute_lock_fingerprint(shuffled)


@given(_dist_set())
def test_installed_distribution_set_is_deterministic_for_fixed_metadata(pairs) -> None:
    """Same underlying `importlib.metadata` state -> the same output every
    call. `installed_distribution_set` re-reads the metadata each call, so
    this pins that re-read is pure over unchanged input."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            prov.importlib.metadata,
            "distributions",
            lambda: iter(_FakeDist(n, v) for n, v in pairs),
        )
        first = prov.installed_distribution_set()
        second = prov.installed_distribution_set()
    assert first == second


@given(_dist_set(), st.randoms(use_true_random=False))
def test_installed_distribution_set_is_independent_of_underlying_iteration_order(
    pairs, rng
) -> None:
    """Metamorphic: `importlib.metadata.distributions()` makes no iteration-
    order guarantee; the function sorts its result, so a shuffled underlying
    order must not change the output."""
    shuffled = list(pairs)
    rng.shuffle(shuffled)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            prov.importlib.metadata,
            "distributions",
            lambda: iter(_FakeDist(n, v) for n, v in pairs),
        )
        base = prov.installed_distribution_set()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            prov.importlib.metadata,
            "distributions",
            lambda: iter(_FakeDist(n, v) for n, v in shuffled),
        )
        perm = prov.installed_distribution_set()
    assert base == perm


@given(_dist_set())
def test_fingerprint_of_installed_set_is_deterministic_end_to_end(pairs) -> None:
    """The full pipeline `current_provenance`/`check_fit_environment` actually
    run (`compute_lock_fingerprint(installed_distribution_set())`) is
    deterministic across two independent evaluations of the same
    environment state, not merely each half in isolation."""

    def _fingerprint_once() -> str:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                prov.importlib.metadata,
                "distributions",
                lambda: iter(_FakeDist(n, v) for n, v in pairs),
            )
            return prov.compute_lock_fingerprint(prov.installed_distribution_set())

    assert _fingerprint_once() == _fingerprint_once()


# ---------------------------------------------------------------------------
# SENSITIVITY: no two distinct distribution sets collide on a fingerprint.
# ---------------------------------------------------------------------------


@given(_dist_set(min_size=1), st.data())
def test_changing_any_version_changes_the_fingerprint(pairs, data) -> None:
    """A tampered stack (one distribution bumped/rolled back) must never
    coincide with the original's fingerprint -- this is what makes the
    certified hash trustworthy evidence of the exact resolved set."""
    idx = data.draw(st.integers(min_value=0, max_value=len(pairs) - 1))
    name, version = pairs[idx]
    new_version = data.draw(_dist_version().filter(lambda v: v != version))
    perturbed = list(pairs)
    perturbed[idx] = (name, new_version)
    assert prov.compute_lock_fingerprint(pairs) != prov.compute_lock_fingerprint(perturbed)


@given(_dist_set(), _dist_name(), _dist_version())
def test_adding_a_distribution_changes_the_fingerprint(pairs, name, version) -> None:
    assume(name not in {n for n, _ in pairs})
    added = (*pairs, (name, version))
    assert prov.compute_lock_fingerprint(pairs) != prov.compute_lock_fingerprint(added)


@given(_dist_set(min_size=1), st.data())
def test_removing_a_distribution_changes_the_fingerprint(pairs, data) -> None:
    idx = data.draw(st.integers(min_value=0, max_value=len(pairs) - 1))
    reduced = pairs[:idx] + pairs[idx + 1 :]
    assert prov.compute_lock_fingerprint(pairs) != prov.compute_lock_fingerprint(reduced)


@given(
    st.tuples(_dist_name(), _dist_name()),
    st.sampled_from(["-", "_", "."]),
    st.sampled_from(["-", "_", "."]),
    _dist_version(),
    _dist_version(),
)
def test_installed_distribution_set_rejects_any_canonicalization_colliding_pair(
    parts, sep1, sep2, v1, v2
) -> None:
    """Generalizes the module's one hardcoded PyYAML/pyyaml duplicate-name
    example into a property: ANY two raw distribution names that
    PEP-503-canonicalize (`packaging.utils.canonicalize_name`: lowercase,
    collapse `[-_.]+` runs to one `-`) to the SAME value are an ambiguous
    fingerprint input, so a version mismatch between them must be rejected
    (`dp_provenance_duplicate_distribution`) rather than silently keeping
    whichever one was encountered first."""
    assume(v1 != v2)
    left, right = parts
    name1 = f"{left}{sep1}{right}"
    name2 = f"{left.upper()}{sep2}{right.upper()}"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            prov.importlib.metadata,
            "distributions",
            lambda: iter([_FakeDist(name1, v1), _FakeDist(name2, v2)]),
        )
        with pytest.raises(ProvenanceError) as exc:
            prov.installed_distribution_set()
    assert exc.value.code == "dp_provenance_duplicate_distribution"


# ---------------------------------------------------------------------------
# FAIL-CLOSED, fit-time gate: exact membership over the WHOLE input domain,
# not just the hand-picked examples the unit suite enumerates.
# ---------------------------------------------------------------------------


@given(
    st.text(min_size=0, max_size=10),
    st.text(min_size=0, max_size=10),
    st.text(min_size=0, max_size=10),
    st.text(min_size=0, max_size=10),
)
def test_check_fit_environment_rejects_any_noncertified_platform(
    system, machine, impl, libc
) -> None:
    """Property version of the unit suite's win32/PyPy examples: ANY
    `PlatformTriple` that differs from `CERTIFIED_PLATFORM` in at least one
    field is refused with `dp_platform_uncertified`, regardless of cpython
    or fingerprint (checked first, so those never even get a chance to
    matter)."""
    triple = PlatformTriple(system=system, machine=machine, implementation=impl, libc=libc)
    assume(triple != CERTIFIED_PLATFORM)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prov, "current_platform", lambda: triple)
        with pytest.raises(ProvenanceError) as exc:
            prov.check_fit_environment()
    assert exc.value.code == "dp_platform_uncertified"


@given(st.integers(min_value=0, max_value=3), st.text(min_size=0, max_size=10))
def test_check_fit_environment_rejects_platform_off_by_one_field(field_index, new_value) -> None:
    """Near-miss platform: exactly ONE field of the certified triple changed,
    the other three left exactly right. A boundary bug that compares only
    some fields (e.g. `implementation` alone, as the real PyPy example does)
    would let this through; the real check compares the whole tuple."""
    fields = list(CERTIFIED_PLATFORM)
    assume(new_value != fields[field_index])
    fields[field_index] = new_value
    near_miss = PlatformTriple(*fields)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prov, "current_platform", lambda: near_miss)
        with pytest.raises(ProvenanceError) as exc:
            prov.check_fit_environment()
    assert exc.value.code == "dp_platform_uncertified"


@given(st.text(alphabet=string.digits + ".", min_size=1, max_size=12))
def test_check_fit_environment_rejects_uncertified_cpython_patch(cpython) -> None:
    """Property version of `test_check_fit_environment_rejects_uncertified_
    cpython_patch`: ANY cpython string not exactly one of the certified rows'
    keys is refused with `dp_stack_uncertified`, on the real certified
    platform (isolating this from the platform check above)."""
    certified_cpythons = {c for (p, c) in prov._CERTIFIED_STACKS if p == CERTIFIED_PLATFORM}
    assume(cpython not in certified_cpythons)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prov, "current_platform", lambda: CERTIFIED_PLATFORM)
        mp.setattr(prov, "current_cpython", lambda: cpython)
        with pytest.raises(ProvenanceError) as exc:
            prov.check_fit_environment()
    assert exc.value.code == "dp_stack_uncertified"


@given(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
def test_check_fit_environment_rejects_arbitrary_uncertified_fingerprint(fingerprint) -> None:
    """On the real certified `(platform, cpython)` row, an arbitrary
    64-hex-char fingerprint that is not one of the pinned members is refused.
    This is the general form of `test_check_fit_environment_rejects_drifted_
    fingerprint`'s single hand-built example."""
    candidates = [(p, c) for (p, c) in prov._CERTIFIED_STACKS if p == CERTIFIED_PLATFORM]
    assume(len(candidates) > 0)
    plat, cpython = candidates[0]
    assume(fingerprint not in prov._CERTIFIED_STACKS[(plat, cpython)])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prov, "current_platform", lambda: plat)
        mp.setattr(prov, "current_cpython", lambda: cpython)
        mp.setattr(prov, "compute_lock_fingerprint", lambda _s: fingerprint)
        with pytest.raises(ProvenanceError) as exc:
            prov.check_fit_environment()
    assert exc.value.code == "dp_stack_uncertified"


@given(st.data())
def test_check_fit_environment_rejects_tampered_certified_fingerprint(data) -> None:
    """A single-character edit of an ACTUAL pinned fingerprint (a typo'd or
    truncated-and-repadded literal, not a wholly different hash) must still
    be refused: membership is exact-string equality via `in frozenset`, so
    near-identical is not good enough."""
    candidates = [(p, c) for (p, c) in prov._CERTIFIED_STACKS if p == CERTIFIED_PLATFORM]
    assume(len(candidates) > 0)
    plat, cpython = data.draw(st.sampled_from(candidates))
    members = prov._CERTIFIED_STACKS[(plat, cpython)]
    real_fp = data.draw(st.sampled_from(sorted(members)))
    idx = data.draw(st.integers(min_value=0, max_value=len(real_fp) - 1))
    new_char = data.draw(st.sampled_from([c for c in "0123456789abcdef" if c != real_fp[idx]]))
    tampered = real_fp[:idx] + new_char + real_fp[idx + 1 :]
    assume(tampered not in members)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prov, "current_platform", lambda: plat)
        mp.setattr(prov, "current_cpython", lambda: cpython)
        mp.setattr(prov, "compute_lock_fingerprint", lambda _s: tampered)
        with pytest.raises(ProvenanceError) as exc:
            prov.check_fit_environment()
    assert exc.value.code == "dp_stack_uncertified"


@given(st.data())
def test_check_fit_environment_rejects_stack_one_distribution_off(data) -> None:
    """CORE fail-closed invariant: a stack that differs from THIS process's
    real installed set by exactly one distribution's version is refused. This
    exercises the real pipeline (`compute_lock_fingerprint(installed_
    distribution_set())`), not a stubbed fingerprint, so it proves the
    end-to-end gate -- not just the membership lookup -- fails closed on a
    near-miss stack ("membership is EXACT: near-miss stacks are rejected")."""
    candidates = [(p, c) for (p, c) in prov._CERTIFIED_STACKS if p == CERTIFIED_PLATFORM]
    assume(len(candidates) > 0)
    plat, cpython = candidates[0]
    members = prov._CERTIFIED_STACKS[(plat, cpython)]
    base = prov.installed_distribution_set()
    assume(len(base) > 0)
    idx = data.draw(st.integers(min_value=0, max_value=len(base) - 1))
    name, version = base[idx]
    new_version = data.draw(_dist_version().filter(lambda v: v != version))
    mutated = tuple(
        sorted((n, v) if i != idx else (n, new_version) for i, (n, v) in enumerate(base))
    )
    assume(prov.compute_lock_fingerprint(mutated) not in members)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prov, "current_platform", lambda: plat)
        mp.setattr(prov, "current_cpython", lambda: cpython)
        mp.setattr(prov, "installed_distribution_set", lambda: mutated)
        with pytest.raises(ProvenanceError) as exc:
            prov.check_fit_environment()
    assert exc.value.code == "dp_stack_uncertified"


_CERTIFIED_ROWS = [
    (plat, cpython, fp) for (plat, cpython), fps in prov._CERTIFIED_STACKS.items() for fp in fps
]


@given(st.sampled_from(_CERTIFIED_ROWS))
def test_check_fit_environment_accepts_every_certified_member(row) -> None:
    """Positive counterpart: property over the WHOLE manifest (every pinned
    row and every pinned fingerprint within it), not just "the" row the unit
    suite happens to destructure assuming exactly one. Generalizes cleanly if
    a future CI run adds more certified rows/members."""
    plat, cpython, fingerprint = row
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(prov, "current_platform", lambda: plat)
        mp.setattr(prov, "current_cpython", lambda: cpython)
        mp.setattr(prov, "compute_lock_fingerprint", lambda _s: fingerprint)
        prov.check_fit_environment()  # must not raise


# ---------------------------------------------------------------------------
# FAIL-CLOSED, generation-time gate: the same exact-membership invariant
# applied to a RECORDED identity instead of the live environment.
# ---------------------------------------------------------------------------


@given(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
def test_validate_recorded_provenance_rejects_arbitrary_uncertified_fingerprint(
    fingerprint,
) -> None:
    candidates = [(p, c) for (p, c) in prov._CERTIFIED_STACKS if p == CERTIFIED_PLATFORM]
    assume(len(candidates) > 0)
    plat, cpython = candidates[0]
    assume(fingerprint not in prov._CERTIFIED_STACKS[(plat, cpython)])
    record = {"platform": plat._asdict(), "cpython": cpython, "fingerprint": fingerprint}
    with pytest.raises(ProvenanceError) as exc:
        prov.validate_recorded_provenance(record)
    assert exc.value.code == "dp_stack_uncertified"


@given(st.sampled_from(_CERTIFIED_ROWS))
def test_validate_recorded_provenance_accepts_every_certified_member(row) -> None:
    plat, cpython, fingerprint = row
    record = {"platform": plat._asdict(), "cpython": cpython, "fingerprint": fingerprint}
    prov.validate_recorded_provenance(record)  # must not raise


# ---------------------------------------------------------------------------
# assert_lock_matches_installed: exact-subset passes, any drift/stray raises.
# ---------------------------------------------------------------------------


def _lock_toml(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n' for n, v in pairs)


@given(_dist_set(min_size=1))
def test_assert_lock_matches_installed_passes_when_sets_are_equal(pairs) -> None:
    """The base case of the reproducibility guard: an installed set equal to
    the (universal, unconditional) lock passes for an arbitrary generated
    lock, not only the one hand-built lockfile in the unit suite.

    `min_size=1`: an empty package list serializes to an empty TOML file,
    which `assert_lock_matches_installed` correctly treats as a malformed
    lock (`dp_lock_parse_error`, no `[[package]]` array) rather than "zero
    packages, trivially matches" -- that is the real, separately-covered
    parse-error path (`test_assert_lock_matches_installed_missing_lock_is_
    parse_error` in the unit suite), not this property's subset invariant."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "uv.lock"
        lock_path.write_text(_lock_toml(pairs), encoding="utf-8")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(prov, "installed_distribution_set", lambda: tuple(sorted(pairs)))
            prov.assert_lock_matches_installed(lock_path=lock_path)  # must not raise


@given(_dist_set(min_size=1), st.data())
def test_assert_lock_matches_installed_flags_any_version_drift(pairs, data) -> None:
    """Metamorphic: perturbing exactly one installed distribution's version
    away from the lock's pin is drift, for an arbitrary lock and an arbitrary
    perturbed entry -- generalizes the unit suite's one `foo 1.0 -> 2.0`
    example."""
    idx = data.draw(st.integers(min_value=0, max_value=len(pairs) - 1))
    name, version = pairs[idx]
    new_version = data.draw(_dist_version().filter(lambda v: v != version))
    installed = list(pairs)
    installed[idx] = (name, new_version)
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "uv.lock"
        lock_path.write_text(_lock_toml(pairs), encoding="utf-8")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(prov, "installed_distribution_set", lambda: tuple(sorted(installed)))
            with pytest.raises(ProvenanceError) as exc:
                prov.assert_lock_matches_installed(lock_path=lock_path)
    assert exc.value.code == "dp_lock_installed_mismatch"
    assert "drift" in exc.value.message


@given(_dist_set(min_size=1), _dist_name(), _dist_version())
def test_assert_lock_matches_installed_flags_any_stray_install(
    pairs, extra_name, extra_version
) -> None:
    """Metamorphic: adding one installed distribution absent from the lock is
    a stray, for an arbitrary lock and an arbitrary extra distribution.
    `min_size=1`: see the note on `test_assert_lock_matches_installed_passes_
    when_sets_are_equal` -- an empty lock is the parse-error path, not this
    property's stray-detection invariant."""
    assume(extra_name not in {n for n, _ in pairs})
    installed = tuple(sorted((*pairs, (extra_name, extra_version))))
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "uv.lock"
        lock_path.write_text(_lock_toml(pairs), encoding="utf-8")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(prov, "installed_distribution_set", lambda: installed)
            with pytest.raises(ProvenanceError) as exc:
                prov.assert_lock_matches_installed(lock_path=lock_path)
    assert exc.value.code == "dp_lock_installed_mismatch"
    assert "stray" in exc.value.message
