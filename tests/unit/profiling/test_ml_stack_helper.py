"""tests/_ml.py: an unusable classifier stack skips by default and fails under DECOY_REQUIRE_ML.

The ml-gate job sets DECOY_REQUIRE_ML=1 so a runner without libgomp1 or with a broken wheel cannot
turn every `-m ml` test into a skip and stay green. Elsewhere (a Mac without libomp) the skip stays.
The stack is made unimportable by blocking `lightgbm` in sys.modules, so these run on any host.
"""

from __future__ import annotations

import sys

import pytest

from tests._ml import ml_stack_available, require_ml_stack


@pytest.fixture
def broken_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "lightgbm", None)


def test_unset_skips_when_stack_unusable(
    broken_stack: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DECOY_REQUIRE_ML", raising=False)
    assert ml_stack_available() is False
    with pytest.raises(pytest.skip.Exception, match="lightgbm"):
        require_ml_stack()


def test_set_fails_when_stack_unusable(broken_stack: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECOY_REQUIRE_ML", "1")
    with pytest.raises(pytest.fail.Exception, match="DECOY_REQUIRE_ML=1 but .*lightgbm"):
        require_ml_stack()
    with pytest.raises(pytest.fail.Exception, match="DECOY_REQUIRE_ML=1"):
        ml_stack_available()
