"""A config value JSON cannot hold is refused at compile with one code and value-free text."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.plan import PlanCompileError, compile_plan, pipeline_config_hash
from decoy_engine.profile import Profile

_FIXED = "A config value has a type JSON cannot hold; quote it as a string."


def _with(config: dict[str, Any], value: Any) -> dict[str, Any]:
    out = dict(config)
    out["global_settings"] = {**(config.get("global_settings") or {}), "note": value}
    return out


BAD_VALUES = [
    pytest.param(dt.date(2024, 1, 1), id="date"),
    pytest.param(dt.datetime(2024, 1, 1, 10, 0, 0), id="naive-datetime"),
    pytest.param(dt.datetime(2024, 1, 1, 10, 0, 0, tzinfo=dt.timezone.utc), id="aware-datetime"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param("secret-before\ud800secret-after", id="surrogate"),
    pytest.param({"member-secret"}, id="set"),
]


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_hash_config_refuses_each_class_with_the_one_code(bad: Any) -> None:
    config = _with({"tables": []}, bad)
    with pytest.raises(PlanCompileError) as caught:
        pipeline_config_hash(config)
    error = caught.value
    assert error.code == reasons.config.CONFIG_VALUE_NOT_JSON
    assert _FIXED in error.message
    assert error.__cause__ is None and error.__context__ is None
    text = f"{error} {error!r}"
    for needle in ("secret", "2024", "nan", "inf", "ud800", "codec"):
        assert needle not in text.replace("Unicode", "").replace("info", "")


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_compile_plan_refuses_the_same_values_before_anything_else(
    simple_config: dict[str, Any], simple_profile: Profile, bad: Any
) -> None:
    with pytest.raises(PlanCompileError) as caught:
        compile_plan(_with(simple_config, bad), simple_profile, decoy_engine_version="0.1.0")
    assert caught.value.code == reasons.config.CONFIG_VALUE_NOT_JSON


def test_a_quoted_date_is_an_ordinary_string(simple_config: dict[str, Any]) -> None:
    assert pipeline_config_hash(_with(simple_config, "2024-01-01"))


def test_a_non_ascii_config_hash_writes_utf8_not_escapes() -> None:
    import hashlib

    config = {"tables": [{"name": "données"}]}
    expected = hashlib.sha256('{"tables":[{"name":"données"}]}'.encode()).hexdigest()
    assert pipeline_config_hash(config) == expected
