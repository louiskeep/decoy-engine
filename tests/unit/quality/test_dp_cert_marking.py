"""Unit tests for the `dp_certified` collection-skip decision (CI fix #4, R5).

`tests.conftest.pytest_collection_modifyitems` runs at collection time, before
any test body or fixture, so a normal test cannot monkeypatch `is_certified_
dp_env` and observe the hook actually skipping an item. The per-item decision
is factored into the pure `should_skip_dp` helper for exactly that reason:
these tests exercise the decision table directly, deterministically and
independent of which environment runs them.
"""

from __future__ import annotations

import pytest

from tests._dp_cert import should_skip_dp


@pytest.mark.parametrize(
    ("has_marker", "certified", "expected"),
    [
        (True, False, True),  # marked, uncertified env -> skip
        (True, True, False),  # marked, certified env -> run
        (False, False, False),  # unmarked, uncertified env -> run (untouched)
        (False, True, False),  # unmarked, certified env -> run (untouched)
    ],
    ids=["marked_uncertified", "marked_certified", "unmarked_uncertified", "unmarked_certified"],
)
def test_should_skip_dp_decision_table(has_marker, certified, expected):
    assert should_skip_dp(has_marker=has_marker, certified=certified) is expected
