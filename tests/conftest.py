# tests/conftest.py
"""
Common fixtures for the engine tests.
"""

import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests._dp_cert import is_certified_dp_env, should_skip_dp

# Registers the xdist-worker report scrubber (a lone surrogate in a captured log would otherwise
# abort a parallel run with an INTERNALERROR). Inert outside an xdist worker.
from tests._xdist_safe_reports import pytest_report_to_serializable  # noqa: F401

# Directories whose every test is `serial`. pytest ignores a `pytestmark` set in a conftest, so
# the mark is added to collected items below. Repo-relative, posix separators.
SERIAL_DIRS = ("tests/perf",)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# scripts/dev/gates.sh points this at a JSON file `{"order_seconds": {file: seconds}}` so a
# parallel run starts its longest files first. Unset or empty: collection order, unchanged.
ORDER_FILE_ENV = "DECOY_TEST_ORDER_FILE"


def _order_seconds() -> dict[str, float]:
    path = os.environ.get(ORDER_FILE_ENV, "")
    if not path:
        return {}
    seconds = json.loads(Path(path).read_text())["order_seconds"]
    return {str(k): float(v) for k, v in seconds.items()}


def _rel_posix(item: pytest.Item) -> str:
    try:
        return Path(str(item.path)).resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return Path(str(item.path)).as_posix()


def _in_dir(rel: str, directory: str) -> bool:
    return rel == directory or rel.startswith(directory + "/")


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """A short, fresh directory directly under /tmp for tests that bind a Unix socket.

    AF_UNIX paths are limited to 107 bytes. ``tmp_path`` sits under pytest's basetemp, which a
    long $TMPDIR or an xdist worker (``popen-gwN/``) lengthens past that limit. This path does
    not depend on the basetemp.
    """
    path = Path(tempfile.mkdtemp(prefix="sv", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.hookimpl(tryfirst=True)  # before the `-m` deselection, which reads the serial mark
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark SERIAL_DIRS items `serial`, order files by recorded seconds, skip off-stack DP items.

    Order: when ``DECOY_TEST_ORDER_FILE`` names a map, files go in descending seconds (a stable
    sort, so each file keeps its own test order and a file absent from the map sorts last).

    Skip `dp_certified` items off the certified DP proof-stack.

    `fit_dp_snapshot` only completes on the certified 77-dist `dev+lint+vault+cloud`
    Python 3.10.20 profile; every other profile -- including the default
    `regression-gate` job -- fails closed with `dp_stack_uncertified` before
    reading any private cell. Tests marked `dp_certified` call it expecting a
    successful fit, so they are collected everywhere (coverage stays visible)
    but SKIPPED here, with a reason, wherever the running env is not that
    profile. The certified profile itself runs them: see the `dp_certified`
    CI job in `.github/workflows/ci.yml`, which asserts they actually execute
    rather than merely being collected.
    """
    seconds = _order_seconds()
    if seconds:
        items.sort(key=lambda item: -seconds.get(_rel_posix(item), 0.0))
    certified = is_certified_dp_env()
    reason = "requires the certified DP proof-stack; this env is not it"
    for item in items:
        rel = _rel_posix(item)
        if any(_in_dir(rel, d) for d in SERIAL_DIRS):
            item.add_marker(pytest.mark.serial)
        has_marker = item.get_closest_marker("dp_certified") is not None
        if should_skip_dp(has_marker=has_marker, certified=certified):
            item.add_marker(pytest.mark.skip(reason=reason))
    _refuse_unstable_ids(items)


# An object repr carries its memory address, which differs per process; xdist workers then
# collect different test ids and the parallel run aborts before running anything.
_UNSTABLE_ID = re.compile(r" at 0x[0-9a-fA-F]+>")  # how every default object repr ends


def _refuse_unstable_ids(items: list[pytest.Item]) -> None:
    bad = [item.nodeid for item in items if _UNSTABLE_ID.search(item.nodeid)]
    if bad:
        raise pytest.UsageError(
            "test ids must not contain a memory address; give these params an explicit id:\n"
            + "\n".join(bad)
        )


@pytest.fixture(autouse=True)
def _file_access_latch_reset():
    """Installing a strict policy latches the process into fail-closed mode; tests that do so
    must not change how later tests resolve the default policy."""
    from decoy_engine.fileaccess import _entry

    _entry._reset_for_tests()
    yield
    _entry._reset_for_tests()


def _install_default_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fill in the shared throwaway secret at the admission seams, for keyed work only."""
    import decoy_engine.keyprovider as kp
    import decoy_engine.secret_providers as sp
    from decoy_engine.execution import _pandas_adapter, _sequential
    from decoy_engine.execution.out_of_core import _preflight as ooc_preflight
    from tests._keyed_secret import THROWAWAY_KEY_PROVIDER

    real_admit = sp.admit_mask_key
    real_require = kp.require_mask_key

    def admit(config, *, key_provider=None, **kwargs):
        has_ref = bool((config.get("global_settings") or {}).get("mask_secret_ref"))
        if key_provider is None and not has_ref and sp.secret_requirements_for_config(config):
            key_provider = THROWAWAY_KEY_PROVIDER
        return real_admit(config, key_provider=key_provider, **kwargs)

    def require(plan, key_provider):
        if key_provider is None and kp.plan_has_keyed_strategy(plan):
            key_provider = THROWAWAY_KEY_PROVIDER
        return real_require(plan, key_provider)

    monkeypatch.setattr(sp, "admit_mask_key", admit)
    monkeypatch.setattr(kp, "require_mask_key", require)
    for module in (_pandas_adapter, _sequential, ooc_preflight):
        monkeypatch.setattr(module, "require_mask_key", require)


@pytest.fixture(autouse=True)
def _default_throwaway_secret(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Shared throwaway secret for keyed tests; see tests/_keyed_secret.py for the contract."""
    path = str(request.node.path)
    if request.node.get_closest_marker("no_default_secret") or "/tests/secret_providers/" in path:
        return
    _install_default_secret(monkeypatch)


@pytest.fixture
def lenient_generate_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the compile-time generate column checks (GEN-STRICT E2) for a runtime-backstop test.

    A few suites feed the generators configs that validate no longer accept (an unknown faker type
    that falls back to `word`, a formula reading an unlisted sibling) to pin what the run does
    with them if one gets past the plan. The check has its own tests in
    tests/unit/plan/checks; this keeps the runtime behavior covered.
    """
    monkeypatch.setattr("decoy_engine.plan._compile.check_generate_columns", lambda *a, **k: None)
