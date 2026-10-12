"""Phase D / D1c: preserve_values matching, config validation, FK-shape rejection, admission.

Config-only: nothing here needs the compiled companion. The compile checks must fire on a
config that reads no row, with value-free messages.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import pytest

from decoy_engine.execution.native._operator_config_rejections import (
    bucket_perturb_config_rejection,
    date_shift_config_rejection,
)
from decoy_engine.plan._checks_bucket_perturb import check_bucket_perturb_config
from decoy_engine.plan._checks_date_shift import check_date_preserve_values
from decoy_engine.plan._compile import run_config_only_checks
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.date_preserve import (
    PreserveSet,
    preserve_config_problem,
    preserve_set_from_config,
)

_FK = "date_preserve_values_unsupported_fk_shape"


def _pset(dates: list[str], fmt: str = "%Y-%m-%d") -> PreserveSet:
    got = preserve_set_from_config({"preserve_values": dates, "date_format": fmt})
    assert got is not None
    return got


# ---- matching -------------------------------------------------------------------------------


def test_text_match_is_pandas_authoritative_in_range_and_strptime_out_of_range() -> None:
    mask = _pset(["2099-01-01", "9999-12-31"]).mask(
        pd.Series(["2099-01-01", "2099-01-02", "9999-12-31", "junk", None, ""], dtype=object)
    )
    assert mask.tolist() == [True, False, True, False, False, False]


def test_time_of_day_is_dropped_and_other_formats_match_by_date() -> None:
    p = _pset(["2099-01-01"], "%d/%m/%Y %H:%M")
    assert p.mask(pd.Series(["01/01/2099 23:59", "02/01/2099 00:00"], dtype=object)).tolist() == [
        True,
        False,
    ]


def test_week_based_format_counterexample_follows_pandas_where_it_parses() -> None:
    """`%Y-%W-%w` is a format where pandas and strptime disagree. In range the pandas parse
    decides (the same parse the operator masks with), so the row matches or not exactly as
    the operator would see it."""
    p = _pset(["2020-01-01"], "%Y-%W-%w")
    text = pd.Series(["2020-00-1"], dtype=object)
    expected = pd.to_datetime(text, format="%Y-%W-%w", errors="coerce").iloc[0].date()
    assert p.mask(text).tolist() == [expected == dt.date(2020, 1, 1)]


def test_typed_sources_match_by_calendar_date() -> None:
    p = _pset(["2099-01-01", "9999-12-31"])
    objs = pd.Series([dt.date(2099, 1, 1), dt.date(9999, 12, 31), dt.date(2020, 1, 1), None])
    assert p.mask(objs).tolist() == [True, True, False, False]
    stamps = pd.Series([pd.Timestamp("2099-01-01 13:00"), pd.Timestamp("2020-01-01"), pd.NaT])
    assert p.mask(stamps).tolist() == [True, False, False]
    aware = pd.Series([pd.Timestamp("2099-01-01 23:30", tz="America/New_York")])
    assert p.mask(aware).tolist() == [True]  # local wall-clock date


def test_empty_series() -> None:
    assert _pset(["2099-01-01"]).mask(pd.Series([], dtype=object)).tolist() == []


def test_render_keeps_source_text_and_formats_typed_dates() -> None:
    p = _pset(["9999-12-31", "2099-01-01"], "%d/%m/%Y")
    series = pd.Series(["31/12/9999", dt.date(2099, 1, 1)], dtype=object)
    assert p.render(series, p.mask(series)) == ["31/12/9999", "01/01/2099"]


# ---- literal and format grammar -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-date",
        "NaT",
        "",
        " ",
        "2020-02-30",
        "2020-13-01",
        "0000-01-01",
        "20200101",
        "2020-1-1",
    ],
)
def test_malformed_literals_are_rejected(bad: str) -> None:
    problem = preserve_config_problem({"preserve_values": [bad], "date_format": "%Y-%m-%d"})
    assert problem is not None
    assert bad.strip() == "" or bad not in problem  # the literal is never echoed


@pytest.mark.parametrize(
    "fmt", ["%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %Z", "ISO8601", "mixed", None, ""]
)
def test_unsupported_or_missing_formats_are_rejected(fmt: Any) -> None:
    cfg: dict[str, Any] = {"preserve_values": ["2099-01-01"]}
    if fmt is not None:
        cfg["date_format"] = fmt
    assert preserve_config_problem(cfg) is not None


def test_an_escaped_percent_is_not_a_directive() -> None:
    assert (
        preserve_config_problem({"preserve_values": ["2099-01-01"], "date_format": "%%f%Y-%m-%d"})
        is None
    )


def test_unset_and_empty_need_no_format_and_return_no_set() -> None:
    for cfg in ({}, {"preserve_values": []}, {"preserve_values": None}):
        assert preserve_config_problem(cfg) is None
        assert preserve_set_from_config(cfg) is None


def test_duplicates_collapse() -> None:
    assert len(_pset(["2099-01-01", "2099-01-01"]).dates) == 1


# ---- plan-compile checks --------------------------------------------------------------------


def _cfg(
    columns: list[dict[str, Any]],
    *,
    tables: list[str] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    names = tables or ["t"]
    cfg: dict[str, Any] = {
        "tables": [{"name": n, "columns": columns if i == 0 else []} for i, n in enumerate(names)]
    }
    if relationships is not None:
        cfg["relationships"] = relationships
    return cfg


def _col(name: str = "d", strategy: str = "date_shift", **pc: Any) -> dict[str, Any]:
    base = {"date_format": "%Y-%m-%d", "preserve_values": ["9999-12-31"]}
    if strategy == "bucket_perturb":
        base["bucket"] = "month"
    return {"name": name, "strategy": strategy, "provider_config": {**base, **pc}}


def _rel(parent: tuple[str, list[str]], *children: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "parent": {"table": parent[0], "columns": parent[1]},
        "children": [{"table": t, "columns": c} for t, c in children],
    }


def test_payload_columns_pass_both_checks() -> None:
    for strategy in ("date_shift", "bucket_perturb"):
        config = _cfg([_col(strategy=strategy)])
        check_date_preserve_values(config)
        check_bucket_perturb_config(config)


def test_date_shift_bad_config_is_a_compile_error() -> None:
    with pytest.raises(PlanCompileError) as caught:
        check_date_preserve_values(_cfg([_col(preserve_values=["nope"])]))
    assert caught.value.code == "date_shift_preserve_values_invalid"
    with pytest.raises(PlanCompileError):
        check_date_preserve_values(_cfg([_col(date_format="%Y %Z")]))


def test_bucket_bad_config_is_a_compile_error_including_nested_children() -> None:
    config = _cfg([_col(strategy="bucket_perturb", preserve_values=["nope"])])
    with pytest.raises(PlanCompileError) as caught:
        check_bucket_perturb_config(config)
    assert caught.value.code == "bucket_perturb_preserve_values_invalid"
    nested = _cfg(
        [
            {
                "name": "d",
                "strategy": "nested",
                "provider_config": {
                    "strategy": "bucket_perturb",
                    "strategy_config": {"bucket": "month", "preserve_values": ["2099-01-01"]},
                },
            }
        ]
    )
    with pytest.raises(PlanCompileError):  # no date_format
        check_bucket_perturb_config(nested)


def test_group_by_with_preserve_values_is_rejected() -> None:
    config = _cfg([_col(group_by="g"), {"name": "g", "strategy": "hash"}])
    with pytest.raises(PlanCompileError) as caught:
        check_date_preserve_values(config)
    assert caught.value.code == "date_preserve_values_with_group_by_unsupported"


def test_group_by_without_preserve_values_is_untouched() -> None:
    config = _cfg([_col(preserve_values=[], group_by="g")])
    check_date_preserve_values(config)


def test_single_hop_scalar_parent_key_is_supported() -> None:
    config = _cfg(
        [_col()],
        tables=["p", "c"],
        relationships=[_rel(("p", ["d"]), ("c", ["fk"]))],
    )
    config["tables"][0]["name"] = "p"
    check_date_preserve_values(config)


def test_two_independent_safe_scalar_keys_are_not_rejected() -> None:
    config = {
        "tables": [
            {"name": "p1", "columns": [_col("a")]},
            {"name": "p2", "columns": [_col("b")]},
            {"name": "c1", "columns": []},
            {"name": "c2", "columns": []},
        ],
        "relationships": [
            _rel(("p1", ["a"]), ("c1", ["a"])),
            _rel(("p2", ["b"]), ("c2", ["b"])),
        ],
    }
    check_date_preserve_values(config)


def _expect_fk_reject(config: dict[str, Any]) -> None:
    with pytest.raises(PlanCompileError) as caught:
        check_date_preserve_values(config)
    assert caught.value.code == _FK


def _tabled(columns_by_table: dict[str, list[dict[str, Any]]], rels: list[dict[str, Any]]) -> Any:
    return {
        "tables": [{"name": t, "columns": c} for t, c in columns_by_table.items()],
        "relationships": rels,
    }


def test_child_key_is_rejected() -> None:
    _expect_fk_reject(_tabled({"p": [], "c": [_col("fk")]}, [_rel(("p", ["d"]), ("c", ["fk"]))]))


def test_multi_hop_chain_is_rejected_whichever_key_carries_it() -> None:
    rels = [_rel(("a", ["k"]), ("b", ["k"])), _rel(("b", ["k"]), ("c", ["k"]))]
    for holder in ("a", "b", "c"):
        _expect_fk_reject(_tabled({t: [_col("k")] if t == holder else [] for t in "abc"}, rels))


def test_multi_parent_shared_child_is_rejected() -> None:
    rels = [_rel(("a", ["k"]), ("c", ["k"])), _rel(("b", ["k"]), ("c", ["k"]))]
    _expect_fk_reject(_tabled({"a": [_col("k")], "b": [], "c": []}, rels))


def test_composite_endpoint_is_rejected() -> None:
    rels = [_rel(("a", ["k", "j"]), ("c", ["k", "j"]))]
    _expect_fk_reject(_tabled({"a": [_col("k")], "c": []}, rels))


def test_scalar_and_composite_overlap_is_rejected() -> None:
    rels = [_rel(("a", ["k"]), ("b", ["k"])), _rel(("a", ["k", "j"]), ("c", ["k", "j"]))]
    _expect_fk_reject(_tabled({"a": [_col("k")], "b": [], "c": []}, rels))


def test_same_table_edge_is_rejected() -> None:
    rels = [_rel(("t", ["parent_date"]), ("t", ["child_date"]))]
    _expect_fk_reject(_tabled({"t": [_col("parent_date")]}, rels))


def test_cycle_is_rejected() -> None:
    rels = [_rel(("a", ["k"]), ("b", ["k"])), _rel(("b", ["k"]), ("a", ["k"]))]
    _expect_fk_reject(_tabled({"a": [_col("k")], "b": []}, rels))


def test_a_nested_child_and_the_config_only_entry_point_run_the_same_checks() -> None:
    names = run_config_only_checks(_cfg([_col()]))
    assert "date_shift_group_by_refs" in names
    with pytest.raises(PlanCompileError):
        run_config_only_checks(_cfg([_col(preserve_values=["nope"])]))


# ---- native admission -----------------------------------------------------------------------


def _admit(strategy: str, **pc: Any) -> str | None:
    cfg = {
        "date_format": "%Y-%m-%d",
        **({"bucket": "month"} if strategy == "bucket_perturb" else {}),
        **pc,
    }
    fn = (
        bucket_perturb_config_rejection
        if strategy == "bucket_perturb"
        else date_shift_config_rejection
    )
    return fn("d", "t", None, namespace="ns", provider_config=cfg)


@pytest.mark.parametrize("strategy", ["bucket_perturb", "date_shift"])
def test_admission_admits_a_valid_key_and_declines_an_invalid_one(strategy: str) -> None:
    assert _admit(strategy, preserve_values=["9999-12-31"]) is None
    assert _admit(strategy, preserve_values=[]) is None
    assert _admit(strategy) is None
    reason = _admit(strategy, preserve_values=["nope"])
    assert reason is not None and reason.startswith(f"{strategy}_preserve_values_invalid")
    assert "nope" not in reason
