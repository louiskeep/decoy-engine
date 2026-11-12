"""The planner and config vocabularies stay a subset of the run-result vocabularies."""

from __future__ import annotations

from typing import get_args

from decoy_engine.config._delivery import DeliveryMode as ConfigMode
from decoy_engine.delivery import DeliveryState
from decoy_engine.run_result._model import DeliveryMode as ResultMode
from decoy_engine.run_result._model import TableDeliveryState as ResultState


def test_at2d_config_modes_are_the_run_result_modes_minus_in_place() -> None:
    assert set(get_args(ConfigMode)) == set(get_args(ResultMode)) - {"in_place"}


def test_at2d_planner_states_are_the_run_result_states_minus_none() -> None:
    assert set(get_args(DeliveryState)) == set(get_args(ResultState)) - {"none"}
