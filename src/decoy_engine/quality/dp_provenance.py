"""Certified proof-stack provenance gate for the DP fit (DPS-CODEC phase 3).

An artifact's ``(epsilon, delta)`` guarantee is only honest on the exact
software stack it was tested against. This module is the fail-closed gate that
enforces that (guide docs/plans/2026-07-23-dps-codec-implementation-guide.md,
sections 3.8 and 4).

WHY A FINGERPRINT, NOT AN ENUMERATED TUPLE (section 3.8, round-5 root-cause).
Earlier revisions hand-listed the versioned distributions the proof depends on
and the list was never complete: successive plan-reviews found a missing
transitive dep each round (SciPy, then attrs / absl-py / wrapt / Deprecated /
polars / python-dateutil / pytz / six, plus the full CPython patch). The
transitive closure is large and drifts, so any curated subset is incomplete by
construction. The fix is to STOP enumerating and instead certify a mechanical
LOCK FINGERPRINT over the COMPLETE resolved distribution set -- every
transitive dependency the lock pins is in the fingerprint, so it is complete by
construction and needs no maintenance as deps change.

METHODOLOGY (established sources, per the repo's "use established methodology"
rule). Distribution identity is read with the standard library's
``importlib.metadata`` (PEP 566 installed-package metadata), names are
canonicalized with PEP 503 (``packaging.utils.canonicalize_name``) so
``PyYAML`` and ``pyyaml`` cannot hash to two different rows, and the
reproducibility guard parses the ``uv.lock`` universal lockfile (the exact
resolution the environment is synced from). The hash is SHA-256 over a
canonical newline-joined ``name==version`` serialization.

WHAT THE GATE IS AND IS NOT (honest scope, section 3.8). The certified row is
``(platform_triple, cpython_full_version, lock_fingerprint)``. The gate is the
locked distribution/version set, NOT exact binary identity: native inputs below
that layer (OpenDP's bundled ``opendp.abi3.so`` Rust core, numpy/scipy's
bundled OpenBLAS/gfortran, the exact glibc patch, the exact CPython build) are
NOT gated in v1. This matches the non-authentication threat model: recording
the identity is audit evidence, not a MAC (the MAC is ROADMAP item 4 / schema
v4). Exact tested-binary identity (selected wheel/``RECORD`` hashes + an exact
libc constraint) is deferred.

THE CERTIFIED PROFILE (section 7, round-6 -- frozen explicitly). The fingerprint
is over the COMPLETE set of distributions installed in the environment, which is
the ``uv.lock`` resolution synced into it. An unrelated installed package
changes the fingerprint and fails the gate closed; this is deliberate ("safe but
strict"). Hosts that install a different profile (e.g. a slimmer runtime, or
extra tools) therefore need their OWN certified row, or an isolated fit
environment -- that host wiring is ROADMAP item 2 / phase 9, out of this phase's
scope. See the note on ``_CERTIFIED_STACKS`` for the exact profile pinned here.

v1 certifies EXACTLY ONE platform -- Linux / x86-64 / CPython / glibc
(manylinux) -- the only platform CI actually exercises (``ci.yml`` is
Ubuntu-only). Anything else (Windows, macOS, ARM, PyPy, musl) fails closed: a
false guarantee on an untested platform is exactly the bug class this redesign
exists to kill, so v1 refuses rather than over-certifies.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

from packaging.utils import canonicalize_name

__all__ = [
    "CERTIFIED_PLATFORM",
    "PlatformTriple",
    "Provenance",
    "ProvenanceError",
    "assert_lock_matches_installed",
    "check_fit_environment",
    "compute_lock_fingerprint",
    "current_cpython",
    "current_platform",
    "current_provenance",
    "installed_distribution_set",
    "validate_recorded_provenance",
]


class ProvenanceError(Exception):
    """A proof-stack provenance violation the DP fit/generation must not run
    through. Mirrors ``carriers.CarrierError`` / ``dp_budget.DpBudgetError``:
    it carries a machine-readable ``code`` so callers branch on the failure
    class rather than parsing prose. Fail-closed by construction -- every gate
    below raises this BEFORE any private data is read."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class PlatformTriple(NamedTuple):
    """The certified platform identity. ``NamedTuple`` so it is hashable (a
    static-manifest key) and immutable. ``system`` is ``sys.platform``,
    ``machine`` is ``platform.machine()``, ``implementation`` is
    ``platform.python_implementation()``, ``libc`` is the libc family from
    ``platform.libc_ver()`` (``"glibc"`` on manylinux; ``""``/``"unknown"`` on
    musl, which is therefore NOT the certified platform)."""

    system: str
    machine: str
    implementation: str
    libc: str


class Provenance(NamedTuple):
    """The full boundary-independent proof-stack identity of an environment or
    artifact: the platform, the full CPython version, and the lock fingerprint.
    Boundary-independent because the fingerprint is over the installed lock and
    so is identical for the direct-carrier and DataFrame-adapter boundaries in
    one venv (section 3.8: the boundary distinction is enforced by the
    import-isolation assertion, not by two different hashes)."""

    platform: PlatformTriple
    cpython: str
    fingerprint: str


# ---------------------------------------------------------------------------
# Distribution set + fingerprint (importlib.metadata + PEP 503 + SHA-256).
# ---------------------------------------------------------------------------


def installed_distribution_set() -> tuple[tuple[str, str], ...]:
    """The sorted, PEP-503-canonicalized ``(name, version)`` pairs of every
    installed distribution.

    Duplicate canonical names are REJECTED (coded
    ``dp_provenance_duplicate_distribution``): two distributions resolving to
    one canonical name make the fingerprint ambiguous, which is exactly the
    silent drift the gate exists to catch, so it fails loud instead of picking
    one arbitrarily. A distribution with no ``Name`` metadata is likewise
    rejected (``dp_provenance_distribution_unnamed``)."""
    seen: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        raw_name = dist.metadata["Name"]
        if raw_name is None:
            raise ProvenanceError(
                code="dp_provenance_distribution_unnamed",
                message=(
                    "an installed distribution reports no 'Name' metadata; the "
                    "proof-stack fingerprint cannot be computed unambiguously"
                ),
            )
        name = str(canonicalize_name(raw_name))
        version = dist.version
        if name in seen:
            # A leftover same-version dist-info produces an identical
            # `name==version` line and so is NOT ambiguous for the hash -- collapse
            # it. Only DIFFERENT versions of one canonical name are a genuine
            # ambiguity the fingerprint cannot resolve; those fail closed.
            if seen[name] != version:
                raise ProvenanceError(
                    code="dp_provenance_duplicate_distribution",
                    message=(
                        f"distribution {name!r} is installed at two different "
                        f"versions ({seen[name]!r} and {version!r}); the proof-stack "
                        "fingerprint would be ambiguous"
                    ),
                )
            continue
        seen[name] = version
    return tuple(sorted(seen.items()))


def _canonical_serialization(dist_set: Iterable[tuple[str, str]]) -> str:
    """The exact bytes-before-hashing serialization. One ``name==version`` per
    line, sorted, newline-joined. Sorting here (not only in the producer) makes
    the fingerprint a pure function of the SET regardless of iteration order."""
    return "\n".join(f"{name}=={version}" for name, version in sorted(dist_set))


def compute_lock_fingerprint(dist_set: Iterable[tuple[str, str]]) -> str:
    """A stable SHA-256 hex fingerprint over the canonical serialization of a
    distribution set. Deterministic and platform-independent given the same
    set: the same resolved lock always yields the same fingerprint."""
    canonical = _canonical_serialization(dist_set)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Platform + interpreter identity.
# ---------------------------------------------------------------------------


def current_platform() -> PlatformTriple:
    """The running platform triple. ``libc`` is the family only (not the patch,
    which v1 does not gate on -- see the module docstring's honest-scope note)."""
    libc_family = platform.libc_ver()[0] or "unknown"
    return PlatformTriple(
        system=sys.platform,
        machine=platform.machine(),
        implementation=platform.python_implementation(),
        libc=libc_family,
    )


def current_cpython() -> str:
    """The full CPython version: ``major.minor.micro.releaselevel``. The FULL
    patch, not a bare minor (section 3.8, round-5): a ``3.10`` row admits a
    moving patch release, but the PLD overflow boundary and other numerics can
    drift by patch, so the gate pins the exact micro."""
    info = sys.version_info
    return f"{info.major}.{info.minor}.{info.micro}.{info.releaselevel}"


def current_provenance() -> Provenance:
    """The full boundary-independent identity of THIS running environment,
    computed live from ``importlib.metadata`` + the interpreter."""
    return Provenance(
        platform=current_platform(),
        cpython=current_cpython(),
        fingerprint=compute_lock_fingerprint(installed_distribution_set()),
    )


# ---------------------------------------------------------------------------
# The STATIC certified manifest.
# ---------------------------------------------------------------------------
#
# This MUST be a checked-in literal, never a value recomputed at import time:
# a manifest that recomputed the local fingerprint would trivially match itself
# and could never fail closed. The gate below compares the LIVE-computed
# fingerprint against these frozen literals.

CERTIFIED_PLATFORM = PlatformTriple(
    system="linux",
    machine="x86_64",
    implementation="CPython",
    libc="glibc",
)

# Certified rows keyed by (platform, cpython_full) -> lock_fingerprint.
#
# The 3.10 row below is pinned from a REPRODUCIBLE synced environment: exactly
# ``uv sync --frozen --extra dev --extra lint --extra vault`` (77 distributions =
# 76 registry + the editable ``decoy-engine``). That exact command is the frozen
# profile -- anyone who runs it against this ``uv.lock`` on Linux/x86-64/CPython
# 3.10.20 reproduces this fingerprint. (An earlier draft pinned a "dirty" working
# ``.venv`` that no single ``uv sync`` reproduced; that was the phase-3 Codex
# MEDIUM -- fixed by pinning the clean ``dev+lint+vault`` profile the CI workflow
# actually syncs.) See ``assert_lock_matches_installed`` for the guard that proves
# the installed set is exactly the lock's marker-selected pins.
#
# CI ADDS THE OTHER ROWS. The dependency-matrix workflow
# (``.github/workflows/dps-dependency-matrix.yml``) runs on exact-pinned CPython
# patches (3.10.x / 3.11.x / 3.12.x), computes the fingerprint per row, and
# asserts it against the row present here (a regression guard). For a patch/row
# not yet present, the workflow PRINTS the computed fingerprint for a maintainer
# to paste in as a new certified row; only patches CI actually exercises may be
# admitted (section 3.8: "admit no patch the matrix does not exercise"). Until a
# row is added, that interpreter fails closed with ``dp_stack_uncertified``.
# A given (platform, cpython) admits a SET of certified fingerprints, one per
# reproducible locked profile tested on that interpreter: the engine's own
# dev/CI lock, and the `decoy` CLI's pristine runtime profile (engine + CLI
# runtime deps, no dev tooling), whose proof-critical library versions are pinned to EXACTLY the
# certified ones (opendp/dp-accounting/numpy/scipy/pandas/pyarrow, per the
# annotation below) so the DP behaviour is the tested behaviour; the CLI's extra
# packages (typer/rich/...) are off the DP code path. Membership, not equality:
# a distinct profile that pins the same proof-critical set earns its own row
# rather than forcing an isolated fit environment (ROADMAP item 2 / phase 9,
# CLI host wiring).
_CERTIFIED_STACKS: dict[tuple[PlatformTriple, str], frozenset[str]] = {
    (CERTIFIED_PLATFORM, "3.10.20.final"): frozenset(
        {
            # decoy-engine dev/CI certification profile: 77 distributions from
            # `uv sync --frozen --extra dev --extra lint --extra vault` on Python
            # 3.10.20 (the profile the dps-dependency-matrix workflow installs).
            # Reproduces from the regenerated 0.5.0 uv.lock. The prior value
            # (6c0b2bbd...) was this same 77-dist profile on the PRE-0.5.0 lock,
            # before the release bump and the packaging>=21.0 direct dep, so it
            # no longer matches a clean build; reverting only decoy-engine to
            # 0.4.0 reproduces it exactly.
            "895b9a20f0fc8a5cd84c94c49a4a7537866f9b45e656a9eb7463103dc8e81161",
            # decoy-cli pristine RUNTIME profile: engine 0.5.0 + CLI (typer/rich/
            # duckdb) + the DP closure, no dev tooling (pytest/ruff/mypy absent).
            # The exact third-party set is pinned in the CLI repo's
            # decoy-fix/requirements-certified.txt (proof-critical opendp/
            # dp-accounting/numpy/scipy/pandas/pyarrow at the annotated versions,
            # packaging==26.2). To reproduce: `--no-dev` install that file into a
            # 3.10.20 venv and run the CLI repo's scripts/cert_smoke.py, which
            # recomputes compute_lock_fingerprint over the running set (this hash)
            # and then exercises fit -> dps-marginal/v3 -> generate end to end.
            # Its legitimacy is verified in the CLI repo, not here. An earlier
            # draft pinned a dev-polluted env (c2c766...); this is the clean one.
            "5a2f7ef75ba38c5c338d5dcbc0a790f1c104cb7f6b49c2b25908540e63bb8495",
        }
    ),
}

# INFORMATIONAL annotation only (section 3.8: retained for auditability; the
# GATE is the fingerprint above, NOT this annotation). Human-readable key
# versions of the proof-critical libraries per certified cpython row.
_CERTIFIED_ANNOTATIONS: dict[str, dict[str, str]] = {
    "3.10.20.final": {
        "opendp": "0.15.1",
        "dp-accounting": "0.6.0",
        "numpy": "2.2.6",
        "scipy": "1.15.3",
        "pandas": "2.3.3",
        "pyarrow": "24.0.0",
    },
}


# ---------------------------------------------------------------------------
# The gates.
# ---------------------------------------------------------------------------


def check_fit_environment() -> None:
    """FIT-TIME gate. Fail closed BEFORE any private data is read.

    Raises ``dp_platform_uncertified`` when the running platform is not the
    certified Linux / x86-64 / CPython / glibc, and ``dp_stack_uncertified``
    when the ``(platform, cpython, fingerprint)`` identity is not a certified
    row (an uncertified CPython patch, or any drift in the installed
    distribution set)."""
    plat = current_platform()
    if plat != CERTIFIED_PLATFORM:
        raise ProvenanceError(
            code="dp_platform_uncertified",
            message=(
                f"the DP fit is certified only on {CERTIFIED_PLATFORM!r}; this "
                f"environment is {plat!r}. The (epsilon, delta) guarantee is not "
                "honest on an untested platform, so the fit refuses rather than "
                "over-certify."
            ),
        )
    cpython = current_cpython()
    fingerprint = compute_lock_fingerprint(installed_distribution_set())
    certified = _CERTIFIED_STACKS.get((plat, cpython))
    if certified is None or fingerprint not in certified:
        raise ProvenanceError(
            code="dp_stack_uncertified",
            message=(
                f"no certified proof-stack row for (cpython {cpython!r}, "
                f"fingerprint {fingerprint!r}) on {plat!r}. The installed "
                "distribution set does not match a tested lock, so the fit "
                "refuses. A host with a different profile needs its own "
                "certified row or an isolated fit environment."
            ),
        )


def validate_recorded_provenance(recorded: object) -> None:
    """GENERATION-TIME gate. Validate an artifact's RECORDED
    ``(platform, cpython, fingerprint)`` against the static certified set.

    Generation does NOT recompute the fingerprint from locally installed
    libraries (the generating host may legitimately differ from the fitting
    host); it checks the identity the artifact carries. Same coded fail-closed
    as the fit gate, plus ``dp_provenance_record_malformed`` when the record's
    shape is unusable."""
    prov = _coerce_recorded(recorded)
    if prov.platform != CERTIFIED_PLATFORM:
        raise ProvenanceError(
            code="dp_platform_uncertified",
            message=(
                f"artifact records platform {prov.platform!r}, which is not the "
                f"certified {CERTIFIED_PLATFORM!r}"
            ),
        )
    certified = _CERTIFIED_STACKS.get((prov.platform, prov.cpython))
    if certified is None or prov.fingerprint not in certified:
        raise ProvenanceError(
            code="dp_stack_uncertified",
            message=(
                f"artifact's recorded proof stack (cpython {prov.cpython!r}, "
                f"fingerprint {prov.fingerprint!r}) is not a certified row"
            ),
        )


def _coerce_recorded(recorded: object) -> Provenance:
    """Narrow a recorded provenance (a ``Provenance``, or a mapping with
    ``platform`` / ``cpython`` / ``fingerprint`` keys where ``platform`` is a
    ``PlatformTriple``, a 4-element sequence, or a mapping of its fields) to a
    ``Provenance``. Anything else is a coded malformed record."""
    if isinstance(recorded, Provenance):
        return recorded
    if not isinstance(recorded, Mapping):
        raise ProvenanceError(
            code="dp_provenance_record_malformed",
            message=f"recorded provenance must be a mapping, got {type(recorded).__name__}",
        )
    try:
        plat = _coerce_platform(recorded["platform"])
        cpython = recorded["cpython"]
        fingerprint = recorded["fingerprint"]
    except KeyError as exc:
        raise ProvenanceError(
            code="dp_provenance_record_malformed",
            message=f"recorded provenance missing required key {exc.args[0]!r}",
        ) from exc
    if not isinstance(cpython, str) or not isinstance(fingerprint, str):
        raise ProvenanceError(
            code="dp_provenance_record_malformed",
            message="recorded 'cpython' and 'fingerprint' must both be strings",
        )
    return Provenance(platform=plat, cpython=cpython, fingerprint=fingerprint)


def _coerce_platform(value: object) -> PlatformTriple:
    """Narrow a recorded platform to a ``PlatformTriple``."""
    if isinstance(value, PlatformTriple):
        return value
    if isinstance(value, Mapping):
        try:
            fields = (
                value["system"],
                value["machine"],
                value["implementation"],
                value["libc"],
            )
        except KeyError as exc:
            raise ProvenanceError(
                code="dp_provenance_record_malformed",
                message=f"recorded platform missing required key {exc.args[0]!r}",
            ) from exc
    elif isinstance(value, (list, tuple)):
        if len(value) != 4:
            raise ProvenanceError(
                code="dp_provenance_record_malformed",
                message=f"recorded platform sequence must have 4 fields, got {len(value)}",
            )
        fields = (value[0], value[1], value[2], value[3])
    else:
        raise ProvenanceError(
            code="dp_provenance_record_malformed",
            message=(
                "recorded platform must be a PlatformTriple, a mapping, or a "
                f"4-element sequence, got {type(value).__name__}"
            ),
        )
    if not all(isinstance(field, str) for field in fields):
        raise ProvenanceError(
            code="dp_provenance_record_malformed",
            message="recorded platform fields must all be strings",
        )
    return PlatformTriple(*fields)


# ---------------------------------------------------------------------------
# Reproducibility guard: lock == installed (section 7, round-6 -- Codex).
# ---------------------------------------------------------------------------


def _default_lock_path() -> Path:
    # dp_provenance.py -> quality -> decoy_engine -> src -> <repo root>.
    return Path(__file__).resolve().parents[3] / "uv.lock"


def assert_lock_matches_installed(lock_path: str | Path | None = None) -> None:
    """Assert every installed distribution is a marker-selected ``uv.lock`` pin
    at the exact same version. This is the guard that the fingerprint is
    REPRODUCIBLE from the lock: a fingerprint over a set that has drifted from
    the lock, or carries an off-lock package, is not what CI certified.

    A distribution is "marker-selected" when a ``[[package]]`` entry for its
    canonical name is active in the current interpreter environment -- either it
    carries no ``resolution-markers`` (universal) or at least one of them
    evaluates true here. The installed set must be a SUBSET of the
    marker-active lock at matching versions: the lock is the full multi-group
    resolution (docs / ml / ner / lint / ... extras), so the installed profile
    is legitimately a subset; what must NOT happen is an installed package that
    is absent from the lock (a stray) or pinned to a different version (drift).
    (The reverse direction -- pinning WHICH lock subset the profile installs --
    is fixed by freezing the exact install command in CI, out of this function's
    scope; documented in the workflow.)

    Raises ``dp_lock_installed_mismatch`` on any stray or drift, and
    ``dp_lock_parse_error`` if the lockfile is missing or unreadable. The TOML
    parser and marker evaluator are imported lazily so the fit-time gate
    (``check_fit_environment``) never pays for them."""
    # Version-guarded (not try/except) so mypy under python_version 3.10 does
    # not try to resolve stdlib `tomllib`, which does not exist before 3.11.
    if sys.version_info >= (3, 11):
        import tomllib as toml_reader
    else:
        import tomli as toml_reader  # 3.10 backport, pinned in the lock

    from packaging.markers import Marker

    path = Path(lock_path) if lock_path is not None else _default_lock_path()
    try:
        with path.open("rb") as handle:
            lock = toml_reader.load(handle)
    except (OSError, ValueError) as exc:
        raise ProvenanceError(
            code="dp_lock_parse_error",
            message=f"could not read/parse lockfile {str(path)!r}: {exc}",
        ) from exc

    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ProvenanceError(
            code="dp_lock_parse_error",
            message=f"lockfile {str(path)!r} has no [[package]] array",
        )

    # canonical name -> set of versions active in THIS environment.
    lock_active: dict[str, set[str]] = {}
    for entry in packages:
        if not isinstance(entry, dict) or "name" not in entry or "version" not in entry:
            continue
        markers = entry.get("resolution-markers")
        if markers and not _any_marker_true(markers, Marker):
            continue
        name = str(canonicalize_name(str(entry["name"])))
        lock_active.setdefault(name, set()).add(str(entry["version"]))

    strays: list[str] = []
    drift: list[str] = []
    for name, version in installed_distribution_set():
        active_versions = lock_active.get(name)
        if active_versions is None:
            strays.append(f"{name}=={version}")
        elif version not in active_versions:
            drift.append(f"{name}=={version} (lock: {sorted(active_versions)})")

    if strays or drift:
        raise ProvenanceError(
            code="dp_lock_installed_mismatch",
            message=(
                "installed distributions do not match the marker-selected lock. "
                f"strays (installed, not in lock): {strays}; "
                f"version drift: {drift}"
            ),
        )


def _any_marker_true(
    markers: object,
    marker_cls: type,
) -> bool:
    """True if any marker string in ``markers`` evaluates true in the current
    environment. A malformed marker is treated as not-satisfied (it cannot
    make a version active) rather than crashing the guard."""
    if not isinstance(markers, list):
        return False
    for marker in markers:
        if not isinstance(marker, str):
            continue
        try:
            if marker_cls(marker).evaluate():
                return True
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # Any marker that cannot be constructed OR evaluated (InvalidMarker,
            # UndefinedComparison, UndefinedEnvironmentName, ...) is treated as
            # not-satisfied. That is the fail-CLOSED direction here: an
            # un-evaluable locked version is not counted active, so a matching
            # installed dist surfaces as a stray rather than being silently
            # accepted. The try body is only marker construction + evaluation.
            continue
    return False
