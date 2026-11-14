"""Both `text_mask` routes raise the one NER version-mismatch error from the shared helper, so the
same column and versions give the same text on the whole-frame and the out-of-core route."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._ner_config import enforce_ner_model_version
from decoy_engine.execution._strategies._text_mask import TextMaskHandler
from decoy_engine.execution.out_of_core._mask_group_c import _text_mask_array
from decoy_engine.plan._types import ColumnSeed

_COLUMN = "notes"
_MODEL = "custom_model_xyz"
_CFG: dict = {"ner": {"model": _MODEL}}


def _seed() -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_mask",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(_CFG.items())),
        ner_model_version="1.0.0",
    )


class _Ctx:
    mask_key = b"\xab" * 32


@pytest.fixture(autouse=True)
def _drifted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "decoy_engine.detection.ner.installed_model_version", lambda model=None: "2.0.0"
    )


def _whole_frame() -> StrategyError:
    df = pd.DataFrame({_COLUMN: ["Contact Jane Doe"]})
    with pytest.raises(StrategyError) as exc:
        TextMaskHandler().run(df, _COLUMN, _seed(), _Ctx())
    return exc.value


def _out_of_core() -> StrategyError:
    values = pa.array(["Contact Jane Doe"], type=pa.string())
    with pytest.raises(StrategyError) as exc:
        _text_mask_array(values, _seed(), job_seed=b"\xab" * 32, cfg=_CFG, column=_COLUMN)
    return exc.value


def _helper() -> StrategyError:
    with pytest.raises(StrategyError) as exc:
        enforce_ner_model_version(
            ner_model=_MODEL,
            plan_ner_model_version="1.0.0",
            column=_COLUMN,
            strategy="text_mask",
        )
    return exc.value


def test_both_routes_raise_the_same_code_and_text() -> None:
    whole, ooc = _whole_frame(), _out_of_core()
    assert whole.code == ooc.code == "ner_model_version_mismatch"
    assert whole.strategy == ooc.strategy == "text_mask"
    assert whole.message == ooc.message


def test_the_text_is_the_helpers_text() -> None:
    expected = _helper().message
    assert _whole_frame().message == expected
    assert _out_of_core().message == expected


def test_the_text_names_the_column_and_versions_not_cell_values() -> None:
    message = _out_of_core().message
    for name in (_COLUMN, _MODEL, "2.0.0", "1.0.0"):
        assert name in message
    assert "Jane" not in message
