"""Model kind tokens live on the descriptors and agree with the report's token set."""

from __future__ import annotations

import typing

from decoy_engine.anonymize.report import ModelKind
from decoy_engine.anonymize.search._result import BUILT_IN_MODELS


def test_each_built_in_model_has_a_distinct_kind() -> None:
    kinds = [model.kind for model in BUILT_IN_MODELS]
    assert len(set(kinds)) == len(BUILT_IN_MODELS)
    assert all(type(kind) is str for kind in kinds)


def test_descriptor_kinds_equal_the_report_literal() -> None:
    assert {model.kind for model in BUILT_IN_MODELS} == set(typing.get_args(ModelKind))
    assert [model.kind for model in BUILT_IN_MODELS] == [
        "k_anonymity",
        "distinct_l_diversity",
        "entropy_l_diversity",
        "recursive_cl_diversity",
        "equal_t_closeness",
        "ordered_t_closeness",
    ]
