"""Run every F0 oracle module under the frozen Faker clock (see `_f0_fixtures.ORACLE_CLOCK`)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.parity._f0_fixtures import frozen_faker_clock


@pytest.fixture(autouse=True)
def _f0_frozen_faker_clock(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.module.__name__.rsplit(".", 1)[-1].startswith("test_phase_f_f0_"):
        with frozen_faker_clock():
            yield
    else:
        yield
