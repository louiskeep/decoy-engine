"""`DecoyError` is defined in a leaf module but is, publicly and in pickles, `decoy_engine.errors.DecoyError`."""

from __future__ import annotations

import pickle

import pytest

from decoy_engine import _error_base, errors
from decoy_engine.errors import ConfigError, DecoyError


def test_the_root_is_one_object_with_its_public_module() -> None:
    assert errors.DecoyError is _error_base.DecoyError
    assert DecoyError.__module__ == "decoy_engine.errors"
    assert DecoyError.__qualname__ == "DecoyError"


@pytest.mark.parametrize("cls", [DecoyError, ConfigError])
def test_pickle_uses_the_public_global_name(cls: type[DecoyError]) -> None:
    exc = cls("message")
    data = pickle.dumps(exc, protocol=pickle.HIGHEST_PROTOCOL)
    assert b"decoy_engine.errors" in data
    assert b"_error_base" not in data
    back = pickle.loads(data)  # noqa: S301 - bytes this test just produced
    assert type(back) is cls and back.args == ("message",)
