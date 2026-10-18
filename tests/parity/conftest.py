"""Run every F0 oracle module (and the FR0/FR3 re-baseline modules that reuse it) under the frozen Faker clock (see `_f0_fixtures.ORACLE_CLOCK`).

``F0_DOB_ANCHOR_CHECK`` switches the freeze off for the dob-anchor acceptance runs
(tests/unit/generation/test_dob_anchor.py documents the procedure):

- ``noclock``: no freeze at all. Only dob-bearing fixtures are expected to match (other Faker date
  providers legitimately read the wall clock), so select them with ``-k composite_person``.
- ``poison``: keep the freeze for the other date providers, but make Faker's own
  ``date_of_birth`` raise. Passing proves no fixture reaches the unanchored method.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.parity._f0_fixtures import frozen_faker_clock


@pytest.fixture(autouse=True)
def _f0_frozen_faker_clock(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    module = request.module.__name__.rsplit(".", 1)[-1]
    if not module.startswith(("test_phase_f_f0_", "test_phase_f_fr0_", "test_phase_f_fr3_")):
        yield
        return
    mode = os.environ.get("F0_DOB_ANCHOR_CHECK", "")
    if mode == "noclock":
        yield
        return
    if mode == "poison":
        from faker.providers import date_time

        def _poison(*_a: object, **_k: object) -> None:
            raise AssertionError("unanchored Faker.date_of_birth reached")

        monkeypatch.setattr(date_time.Provider, "date_of_birth", _poison)
    with frozen_faker_clock():
        yield
