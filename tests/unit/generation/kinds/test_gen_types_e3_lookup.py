"""GEN-TYPES E3: the `lookup` generate type from a file or a shipped code set (AT-16 and the
generation side of AT-10/AT-11/AT-20).

Mutants named in the plan (run by hand, listed in the slice report): fingerprint keeps the path
(AT-16), run re-reads the path (AT-10), `file` gets the disk fallback (AT-20).
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons
from decoy_engine.generation import kinds
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation.kinds import _lookup
from decoy_engine.generators.derivation import (
    GenDeriveContext,
    strategy_config_fingerprint,
    with_table,
)
from decoy_engine.plan import PlanCompileError, compile_plan
from tests.unit._dps_helpers import empty_profile
from tests.unit.generation.kinds._support import SEED, cfg, generate, one, table, values


def _csv(tmp_path: Path, text: str, name: str = "list.csv") -> str:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def _col(**extra: Any) -> dict[str, Any]:
    return {"name": "x", "type": "lookup", **extra}


def _code(config: dict[str, Any]) -> str:
    with pytest.raises(PlanCompileError) as exc:
        compile_plan(config, empty_profile(), decoy_engine_version="test")
    return exc.value.code


def _ctx(col: dict[str, Any], digests: Any = None) -> GenDeriveContext:
    return GenDeriveContext.for_column(
        derive_key=None,
        column_config=with_table(col, "t"),
        fallback_seed=SEED,
        content_digests=digests,
    )


def test_values_come_from_the_file_and_repeat_for_one_seed(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v\nred\ngreen\nblue\n")
    first = values(one(_col(file=path), rows=60))
    assert set(first) <= {"red", "green", "blue"}
    assert len(set(first)) == 3
    assert first == values(one(_col(file=path), rows=60))


def test_a_weight_column_steers_the_draw(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v,w\na,1\nb,0\nc,0\n")
    assert set(values(one(_col(file=path, weight_column="w"), rows=40))) == {"a"}


def test_the_value_column_can_be_named(tmp_path: Path) -> None:
    path = _csv(tmp_path, "id,name\n1,ann\n2,bo\n")
    out = values(one(_col(file=path, value_column="name"), rows=30))
    assert set(out) <= {"ann", "bo"}


def test_csv_cells_keep_their_exact_text(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v\nNA\nnull\n007\n")
    assert set(values(one(_col(file=path), rows=80))) == {"NA", "null", "007"}


def test_a_blank_value_cell_is_an_empty_string_entry(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v,w\n,1\nx,0\n")
    assert set(values(one(_col(file=path, weight_column="w"), rows=20))) == {""}


def test_a_parquet_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / "list.parquet"
    pq.write_table(pa.table({"v": [10, 20, 30], "w": [1.0, 1.0, 0.0]}), path)
    out = values(one(_col(file=str(path), weight_column="w"), rows=50))
    assert set(out) == {10, 20}


def test_a_shipped_code_set_is_a_list_with_no_file() -> None:
    out = values(one(_col(codeset="hcpcs"), rows=30))
    assert len(out) == 30
    assert all(isinstance(v, str) and v for v in out)
    assert out == values(one(_col(codeset="hcpcs"), rows=30))


def test_null_probability_applies_after_the_draw(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v\na\n")
    out = values(one(_col(file=path, null_probability=1.0), rows=10))
    assert out == [None] * 10


def test_output_type_casts_the_drawn_values(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v\n1\n2\n")
    result = generate(one(_col(file=path, output_type="int"), rows=10))
    assert pa.types.is_integer(result["t"].schema.field("x").type)


# AT-16 ---------------------------------------------------------------------------------


def test_the_sampler_gives_the_categorical_draws_for_one_seed() -> None:
    cats, weights = ["a", "b", "c"], [0.5, 0.3, 0.2]
    ctx = _ctx(_col(file="x.csv"), {("lookup", "file"): "sha256:x"})
    lookup = kinds.LookupList(tuple(cats), tuple(weights), "sha256:x")
    drawn = kinds.lookup_values({}, 25, ctx, lookup)
    assert drawn == random.Random(ctx.base_int("py")).choices(cats, weights=weights, k=25)


def test_moving_the_file_keeps_the_output_and_one_weight_changes_it(tmp_path: Path) -> None:
    body = "v,w\na,5\nb,3\nc,2\n"
    first = _csv(tmp_path, body, "one.csv")
    second = _csv(tmp_path, body, "two.csv")
    edited = _csv(tmp_path, "v,w\na,5\nb,3\nc,9\n", "three.csv")
    base = values(one(_col(file=first, weight_column="w"), rows=200))
    assert base == values(one(_col(file=second, weight_column="w"), rows=200))
    assert base != values(one(_col(file=edited, weight_column="w"), rows=200))


def test_the_seed_holds_the_content_digest_not_the_path() -> None:
    a = strategy_config_fingerprint(
        _col(file="a.csv"), content_digests={("lookup", "file"): "sha256:1"}
    )
    b = strategy_config_fingerprint(
        _col(file="b.csv"), content_digests={("lookup", "file"): "sha256:1"}
    )
    c = strategy_config_fingerprint(
        _col(file="a.csv"), content_digests={("lookup", "file"): "sha256:2"}
    )
    assert a == b
    assert a != c


# Config and compile ---------------------------------------------------------------------


def test_an_unknown_codeset_is_refused() -> None:
    assert _code(one(_col(codeset="no_such_set"))) == str(
        reasons.config.GENERATE_OPTION_VALUE_UNKNOWN
    )


@pytest.mark.parametrize(
    ("text", "extra", "code"),
    [
        ("", {}, reasons.plan.LOOKUP_FILE_EMPTY),
        ("v\n", {}, reasons.plan.LOOKUP_FILE_EMPTY),
        ("v\na\n", {"value_column": "nope"}, reasons.plan.LOOKUP_COLUMN_MISSING),
        ("v,w\na,oops\n", {"weight_column": "w"}, reasons.plan.LOOKUP_WEIGHT_INVALID),
        ("v,w\na,-1\n", {"weight_column": "w"}, reasons.plan.LOOKUP_WEIGHT_INVALID),
        ("v,w\na,0\n", {"weight_column": "w"}, reasons.plan.LOOKUP_WEIGHT_INVALID),
        ("v,w\na,nan\n", {"weight_column": "w"}, reasons.plan.LOOKUP_WEIGHT_INVALID),
        ("v,w\na,\n", {"weight_column": "w"}, reasons.plan.LOOKUP_WEIGHT_INVALID),
        ("v,w\na,-1\nb,5\n", {"weight_column": "w"}, reasons.plan.LOOKUP_WEIGHT_INVALID),
        ("v,w\na,\nb,1\n", {"weight_column": "w"}, reasons.plan.LOOKUP_WEIGHT_INVALID),
        ("v\na\n", {"weight_column": "w"}, reasons.plan.LOOKUP_COLUMN_MISSING),
    ],
)
def test_a_bad_file_is_refused_with_its_own_code(
    tmp_path: Path, text: str, extra: dict[str, Any], code: str
) -> None:
    assert _code(one(_col(file=_csv(tmp_path, text), **extra))) == str(code)


def test_an_unknown_extension_and_a_missing_file_are_refused(tmp_path: Path) -> None:
    other = _csv(tmp_path, "v\na\n", "list.txt")
    assert _code(one(_col(file=other))) == str(reasons.plan.LOOKUP_FILE_FORMAT_UNSUPPORTED)
    gone = str(tmp_path / "gone.csv")
    assert _code(one(_col(file=gone))) == str(reasons.plan.LOOKUP_FILE_UNREADABLE)


def test_more_entries_than_the_cap_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_lookup, "MAX_LOOKUP_ENTRIES", 2)
    path = _csv(tmp_path, "v\na\nb\nc\n")
    assert _code(one(_col(file=path))) == str(reasons.plan.LOOKUP_TOO_MANY_ENTRIES)


def test_a_lookup_column_is_not_streamed(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v\na\n")
    config = one(_col(file=path))
    declines = classify_plan(config).declines
    assert [(d.column, d.kind) for d in declines] == [("x", "lookup")]
    assert declines[0].reason == "family_declined:lookup"


def test_two_tables_may_share_one_file(tmp_path: Path) -> None:
    path = _csv(tmp_path, "v\na\nb\n")
    config = cfg([table("t", 5, [_col(file=path)]), table("u", 5, [_col(file=path)])])
    out = generate(config)
    assert set(out["t"].column("x").to_pylist()) <= {"a", "b"}
    assert set(out["u"].column("x").to_pylist()) <= {"a", "b"}
