"""One engine parser for boolean environment flags: accepted set, default, value-free refusal."""

from __future__ import annotations

import os
import pickle

import pytest

from decoy_engine import errors, reasons
from decoy_engine.errors import EnvFlagError
from decoy_engine.internal import env_flags
from decoy_engine.internal.env_flags import FLAGS, FlagPolicy, env_flag

VAR = "DECOY_TEST_FLAG"
SECRET = "maybe-secret-xyz"


@pytest.fixture(autouse=True)
def _registered(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Register VAR with the Decoy set; a test parametrized on `default` sets its default."""
    callspec = getattr(request.node, "callspec", None)
    default = callspec.params.get("default", False) if callspec else False
    base = FLAGS["DECOY_ML_DISABLED"]
    table = {**FLAGS, VAR: FlagPolicy(default, base.true, base.false)}
    monkeypatch.setattr(env_flags, "FLAGS", table)


TRUE_SPELLINGS = ["1", "true", "TRUE", "True", "yes", "YES", "on", "On", " on ", "\ttrue\n"]
FALSE_SPELLINGS = ["0", "false", "FALSE", "no", "No", "off", "OFF", " off "]


@pytest.mark.parametrize("raw", TRUE_SPELLINGS)
@pytest.mark.parametrize("default", [True, False])
def test_true_spellings_are_true_whatever_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str, default: bool
) -> None:
    monkeypatch.setenv(VAR, raw)
    assert env_flag(VAR, os.environ.get(VAR)) is True


@pytest.mark.parametrize("raw", FALSE_SPELLINGS)
@pytest.mark.parametrize("default", [True, False])
def test_false_spellings_are_false_whatever_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str, default: bool
) -> None:
    monkeypatch.setenv(VAR, raw)
    assert env_flag(VAR, os.environ.get(VAR)) is False


@pytest.mark.parametrize("default", [True, False])
def test_unset_gives_the_default(monkeypatch: pytest.MonkeyPatch, default: bool) -> None:
    monkeypatch.delenv(VAR, raising=False)
    assert env_flag(VAR, os.environ.get(VAR)) is default


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
@pytest.mark.parametrize("default", [True, False])
def test_empty_or_blank_gives_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str, default: bool
) -> None:
    monkeypatch.setenv(VAR, raw)
    assert env_flag(VAR, os.environ.get(VAR)) is default


@pytest.mark.parametrize("raw", ["y", "t", "n", "f", "2", "maybe", "truee", "1.0", "trué"])
def test_any_other_spelling_is_refused_with_the_coded_error(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(VAR, raw)
    with pytest.raises(EnvFlagError) as caught:
        env_flag(VAR, os.environ.get(VAR))
    assert caught.value.code == reasons.config.ENV_FLAG_INVALID == "env_flag_invalid"
    assert caught.value.variable == VAR


def test_the_refusal_never_holds_the_value_and_has_no_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(VAR, SECRET)
    with pytest.raises(EnvFlagError) as caught:
        env_flag(VAR, os.environ.get(VAR))
    text = str(caught.value)
    assert SECRET not in text and SECRET not in repr(caught.value)
    assert VAR in text
    for spelling in ("1", "true", "yes", "on", "0", "false", "no", "off"):
        assert spelling in text
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_the_error_survives_a_pickle_round_trip() -> None:
    err = pickle.loads(pickle.dumps(EnvFlagError(VAR)))  # noqa: S301 -- own round trip
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


def test_the_decoder_never_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VAR, "off")
    assert env_flag(VAR, "yes") is True
    assert env_flag(VAR, None) is False
    with pytest.raises(EnvFlagError):
        env_flag(VAR, "maybe")


def test_an_unregistered_name_is_refused_even_with_a_valid_spelling() -> None:
    with pytest.raises(KeyError):
        env_flag("DECOY_NOT_REGISTERED", "1")


def test_every_registered_decoy_flag_defaults_false_and_shares_one_set() -> None:
    decoy = [n for n in FLAGS if n.startswith("DECOY_")]
    assert decoy
    for name in decoy:
        assert FLAGS[name] == FLAGS[decoy[0]]
        assert FLAGS[name].default is False


# ----- the Vault set: exactly what the Vault adapter accepted before the registry -------------

VAULT = "VAULT_SKIP_VERIFY"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "t", "T", "yes", "y", "Y", "on", " y ", "ON"])
def test_vault_true_spellings_include_t_and_y(raw: str) -> None:
    assert env_flag(VAULT, raw) is True


@pytest.mark.parametrize(
    "raw", [None, "", "  ", "0", "false", "no", "n", "f", "off", "maybe", "2", "none", "truee"]
)
def test_vault_everything_else_is_false_and_never_raises(raw: str | None) -> None:
    assert env_flag(VAULT, raw) is False


def test_the_vault_set_differs_from_the_decoy_set_only_by_t_and_y_and_leniency() -> None:
    assert FLAGS[VAULT].true == FLAGS["DECOY_ML_DISABLED"].true | {"t", "y"}
    assert FLAGS[VAULT].false is None
