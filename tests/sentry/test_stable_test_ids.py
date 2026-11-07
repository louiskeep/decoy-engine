"""The collection guard refuses test ids that embed a memory address."""

from types import SimpleNamespace

import pytest

from tests.conftest import _refuse_unstable_ids


def test_address_in_an_id_is_refused() -> None:
    items = [SimpleNamespace(nodeid="t.py::f[<list_iterator object at 0x1186e4c70>]")]
    with pytest.raises(pytest.UsageError, match="0x1186e4c70"):
        _refuse_unstable_ids(items)  # type: ignore[arg-type]


def test_plain_ids_pass() -> None:
    items = [SimpleNamespace(nodeid="t.py::f[iterator]"), SimpleNamespace(nodeid="t.py::g[0x10]")]
    _refuse_unstable_ids(items)  # type: ignore[arg-type]
