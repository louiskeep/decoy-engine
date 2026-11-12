"""``SharedRules``: validators a write model and its derived read model both run."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from decoy_engine._value_free import SharedRules, derive_read


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)


def _derive(model: type[BaseModel]) -> type[BaseModel]:
    return derive_read(model, base=_Base, token=str, module=__name__)


class _Rules(SharedRules):
    @field_validator("name", mode="after", check_fields=False)
    @classmethod
    def _name_not_x(cls, value: str) -> str:
        if value == "x":
            raise ValueError("fixed")
        return value

    @model_validator(mode="before")
    @classmethod
    def _no_forbidden(cls, data: Any) -> Any:
        if isinstance(data, dict) and "forbidden" in data:
            raise ValueError("fixed")
        return data


class _Write(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class _WriteRuled(_Write, _Rules):
    pass


def test_both_validators_run_on_the_read_model() -> None:
    read = _derive(_WriteRuled)
    assert read.model_validate({"name": "ok"}).name == "ok"  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        read.model_validate({"name": "x"})
    with pytest.raises(ValidationError):
        read.model_validate({"name": "ok", "forbidden": 1})


def test_the_read_config_stays_the_read_base_with_rules_present() -> None:
    read = _derive(_WriteRuled)
    assert read.model_config["extra"] == "ignore" and read.model_config["frozen"] is True
    assert read.model_validate({"name": "ok", "later": 1}).name == "ok"  # type: ignore[attr-defined]


class _Inner(_Write, _Rules):
    pass


class _Outer(BaseModel):
    inner: _Inner


def test_a_nested_write_models_rules_reach_its_nested_read_model() -> None:
    read = _derive(_Outer)
    with pytest.raises(ValidationError):
        read.model_validate({"inner": {"name": "x"}})
    assert read.model_validate({"inner": {"name": "ok"}})


def test_a_validator_straight_on_a_write_model_is_refused() -> None:
    class Direct(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def _check(cls, value: str) -> str:
            return value

    with pytest.raises(TypeError) as caught:
        _derive(Direct)
    assert "Direct" in str(caught.value) and "_check" in str(caught.value)


def test_a_rules_class_with_a_field_is_refused() -> None:
    class Fielded(SharedRules):
        extra_field: str = "a"

    class Write(Fielded):
        name: str

    with pytest.raises(TypeError):
        _derive(Write)
