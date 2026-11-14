"""One engine parser for boolean environment flags: accepted set, default, value-free refusal."""

from __future__ import annotations

import pickle

import pytest

from decoy_engine import errors, reasons
from decoy_engine.errors import EnvFlagError
from decoy_engine.internal.env_flags import env_flag

VAR = "DECOY_TEST_FLAG"
SECRET = "maybe-secret-xyz"

TRUE_SPELLINGS = ["1", "true", "TRUE", "True", "yes", "YES", "on", "On", " on ", "\ttrue\n"]
FALSE_SPELLINGS = ["0", "false", "FALSE", "no", "No", "off", "OFF", " off "]


@pytest.mark.parametrize("raw", TRUE_SPELLINGS)
@pytest.mark.parametrize("default", [True, False])
def test_true_spellings_are_true_whatever_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str, default: bool
) -> None:
    monkeypatch.setenv(VAR, raw)
    assert env_flag(VAR, default=default) is True


@pytest.mark.parametrize("raw", FALSE_SPELLINGS)
@pytest.mark.parametrize("default", [True, False])
def test_false_spellings_are_false_whatever_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str, default: bool
) -> None:
    monkeypatch.setenv(VAR, raw)
    assert env_flag(VAR, default=default) is False


@pytest.mark.parametrize("default", [True, False])
def test_unset_gives_the_default(monkeypatch: pytest.MonkeyPatch, default: bool) -> None:
    monkeypatch.delenv(VAR, raising=False)
    assert env_flag(VAR, default=default) is default


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
@pytest.mark.parametrize("default", [True, False])
def test_empty_or_blank_gives_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str, default: bool
) -> None:
    monkeypatch.setenv(VAR, raw)
    assert env_flag(VAR, default=default) is default


@pytest.mark.parametrize("raw", ["y", "t", "n", "f", "2", "maybe", "truee", "1.0", "trué"])
def test_any_other_spelling_is_refused_with_the_coded_error(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(VAR, raw)
    with pytest.raises(EnvFlagError) as caught:
        env_flag(VAR, default=False)
    assert caught.value.code == reasons.config.ENV_FLAG_INVALID == "env_flag_invalid"
    assert caught.value.variable == VAR


def test_the_refusal_never_holds_the_value_and_has_no_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VAR, SECRET)
    with pytest.raises(EnvFlagError) as caught:
        env_flag(VAR, default=False)
    text = str(caught.value)
    assert SECRET not in text and SECRET not in repr(caught.value)
    assert VAR in text
    for spelling in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        assert spelling in text
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_the_error_survives_a_pickle_round_trip() -> None:
    err = pickle.loads(pickle.dumps(EnvFlagError(VAR)))
    assert isinstance(err, EnvFlagError)
    assert err.variable == VAR and str(err) == str(EnvFlagError(VAR))


def test_the_error_is_a_decoy_error_and_a_value_error() -> None:
    assert issubclass(EnvFlagError, errors.DecoyError)
    assert issubclass(EnvFlagError, ValueError)


def test_the_error_and_its_reason_are_public() -> None:
    assert "EnvFlagError" in errors.__all__
    spec = reasons.get("config", "env_flag_invalid")
    assert spec.identity == ("config", "env_flag_invalid")
    assert spec.since == reasons.UNRELEASED
