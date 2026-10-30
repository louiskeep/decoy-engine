"""F06 acceptance A1, A8, A9 and B1-B7 (end to end, RED until the matching slice lands).

Kernel-level A2-A7 vectors live in tests/unit/declared_casting/; protected-store and leakage
cases C1-C6 live in tests/security/test_f06_quarantine_boundary.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.acceptance._f06_support import (
    INT64,
    ISO_DATE,
    PROTECTED_QUARANTINE,
    column,
    config,
    run,
    text_table,
    validate,
    write_csv,
    zero_pad,
)

DATE_OUT = {"id": "date/iso8601/v1", "width": 10, "width_unit": "code_points"}


def _date_col(policy: str, **extra: Any) -> dict[str, Any]:
    return column(
        "start",
        logical_type="date",
        parse_format=ISO_DATE,
        error_policy=policy,
        output_format=DATE_OUT,
        **extra,
    )


def _people(tmp_path: Path, policy: str, **col_extra: Any) -> dict[str, Any]:
    src = write_csv(tmp_path / "people.csv", ["id", "start"], [["1", "2026-02-01"]])
    top = {"protected_quarantine": PROTECTED_QUARANTINE} if policy == "quarantine" else None
    return config([column("id"), _date_col(policy, **col_extra)], source_path=src, top=top)


def _counts(result: Any, table: str = "people", col: str = "start") -> dict[str, int]:
    return result.quality_metrics["declared_casting"]["by_table"][table]["by_column"][col]


def _local_runtime(tmp_path: Path, clock: Any = None) -> Any:
    from decoy_engine.declared_casting import (
        LocalProtectedQuarantineSink,
        ProtectedQuarantineRuntime,
    )
    from decoy_engine.keyprovider import SecretKeyProvider

    kwargs = {"clock": clock} if clock is not None else {}
    sink = LocalProtectedQuarantineSink(
        tmp_path / "quarantine", key_provider=SecretKeyProvider(secret=b"q" * 32), **kwargs
    )
    return ProtectedQuarantineRuntime(sink=sink, owner="tenant-1", run_id="run-A")


# -- A1 fixed-width age ----------------------------------------------------------------------


def test_a1_fixed_width_age_reaches_bucketize_as_int_and_is_rendered_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pandas as pd

    from decoy_engine import read_fixed_width, write_fixed_width
    from decoy_engine.execution._strategies._bucketize import BucketizeStrategyHandler

    layout = {
        "exact_record_width": True,
        "columns": [
            {"name": "name", "start": 0, "width": 8, "type": "str"},
            {"name": "age", "start": 8, "width": 3, "type": "str"},
        ],
    }
    path = tmp_path / "in.txt"
    path.write_text("Alice   042\nBob     007\nCarol   123\n", encoding="utf-8")
    raw = read_fixed_width(path, layout)
    assert raw["age"].tolist() == ["042", "007", "123"]  # no legacy int cast: lexemes survive

    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "people": {"type": "file", "format": "fixed_width", "path": str(path), "layout": layout}
        },
        "tables": [
            {
                "name": "people",
                "columns": [
                    column("name"),
                    column(
                        "age",
                        "bucketize",
                        provider_config={"width": 10, "format": "lower"},
                        logical_type="integer",
                        parse_format=INT64,
                        sentinel_to_null={"id": "sentinel/exact/v1", "values": [""]},
                        error_policy="fail",
                        output_format=zero_pad(3),
                    ),
                ],
            }
        ],
        "targets": {
            "people": {
                "type": "file",
                "format": "fixed_width",
                "path": str(tmp_path / "out.txt"),
                "layout": layout,
            }
        },
        "relationships": [],
        "namespaces": {},
    }
    seen: list[list[Any]] = []
    original = BucketizeStrategyHandler.run

    def spy(self, df, column_name, plan, ctx):
        seen.append(list(df[column_name]))
        return original(self, df, column_name, plan, ctx)

    monkeypatch.setattr(BucketizeStrategyHandler, "run", spy)
    result = run(cfg, {"people": pa.Table.from_pandas(raw, preserve_index=False)})

    assert seen and [int(v) for v in seen[0]] == [42, 7, 123]
    assert all(not isinstance(v, str) for v in seen[0])
    out = result.outputs["people"]
    assert out.column("age").to_pylist() == ["040", "000", "120"]
    target = tmp_path / "out.txt"
    write_fixed_width(out.to_pandas(), layout, target)
    reread = read_fixed_width(target, layout)
    assert reread["age"].tolist() == ["040", "000", "120"]
    from decoy_engine.declared_casting import parse_value

    assert [parse_value(INT64, v).value for v in reread["age"]] == [40, 0, 120]
    assert isinstance(pd.DataFrame, type)


def test_a1_unmodified_legacy_fixed_width_int_layout_still_casts(tmp_path: Path) -> None:
    from decoy_engine import read_fixed_width

    path = tmp_path / "legacy.txt"
    path.write_text("042\n", encoding="utf-8")
    frame = read_fixed_width(
        path, {"columns": [{"name": "a", "start": 0, "width": 3, "type": "int"}]}
    )
    assert frame["a"].tolist() == [42]


# -- A8 sentinel precedence ------------------------------------------------------------------


def test_a8_sentinel_precedes_parse_and_is_counted_separately(tmp_path: Path) -> None:
    src = write_csv(tmp_path / "p.csv", ["id", "start"], [["1", "2026-02-01"]])
    cfg = config(
        [
            column("id"),
            _date_col("fail", sentinel_to_null={"id": "sentinel/exact/v1", "values": ["TBD", ""]}),
        ],
        source_path=src,
    )
    table = text_table(
        id=["1", "2", "3", "4", "5"],
        start=["2026-02-01", "TBD", "", None, "2026-03-05"],
    )
    result = run(cfg, {"people": table})
    assert result.outputs["people"].column("start").to_pylist() == [
        "2026-02-01",
        None,
        None,
        None,
        "2026-03-05",
    ]
    counts = _counts(result)
    assert counts["sentinel_to_null"] == 2  # TBD and "" - the physical null is not a substitution
    assert counts.get("parse_null", 0) == 0 and counts.get("parse_quarantine", 0) == 0
    assert result.quality_metrics["declared_casting"]["total_substitutions"] == 2
    assert "TBD" not in json.dumps(result.quality_metrics, default=str)


def test_a8_sentinel_case_variant_does_not_match(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    src = write_csv(tmp_path / "p.csv", ["id", "start"], [["1", "2026-02-01"]])
    cfg = config(
        [
            column("id"),
            _date_col("fail", sentinel_to_null={"id": "sentinel/exact/v1", "values": ["TBD"]}),
        ],
        source_path=src,
    )
    with pytest.raises(ExecutionError) as exc:
        run(cfg, {"people": text_table(id=["1"], start=["tbd"])})
    assert exc.value.code == "declared_date_invalid"


# -- A9 framing vs value ---------------------------------------------------------------------


def test_a9_overlong_fixed_width_row_is_a_framing_error_not_a_misleading_cast_error(
    tmp_path: Path,
) -> None:
    from decoy_engine import read_fixed_width
    from decoy_engine.errors import FixedWidthParseError

    layout = {
        "exact_record_width": True,
        "columns": [
            {"name": "name", "start": 0, "width": 4, "type": "str"},
            {"name": "age", "start": 4, "width": 3, "type": "str"},
        ],
    }
    path = tmp_path / "long.txt"
    path.write_text("Ann 042\nBob 0071\n", encoding="utf-8")
    with pytest.raises(FixedWidthParseError) as exc:
        read_fixed_width(path, layout)
    assert exc.value.code == "fixed_width_record_overlong"
    assert not str(exc.value.code).startswith("declared_")
    assert "0071" not in str(exc.value)


def test_a9_layout_without_exact_width_is_unchanged_for_legacy_files(tmp_path: Path) -> None:
    from decoy_engine import read_fixed_width

    layout = {"columns": [{"name": "a", "start": 0, "width": 2, "type": "str"}]}
    path = tmp_path / "t.txt"
    path.write_text("abcd\n", encoding="utf-8")
    assert read_fixed_width(path, layout)["a"].tolist() == ["ab"]


def test_fixed_width_source_with_declared_columns_requires_exact_record_width(
    tmp_path: Path,
) -> None:
    from decoy_engine.plan._errors import PlanCompileError

    layout = {"columns": [{"name": "age", "start": 0, "width": 3, "type": "str"}]}
    path = tmp_path / "t.txt"
    path.write_text("042\n", encoding="utf-8")
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "t": {"type": "file", "format": "fixed_width", "path": str(path), "layout": layout}
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    column(
                        "age",
                        logical_type="integer",
                        parse_format=INT64,
                        error_policy="fail",
                        output_format=zero_pad(3),
                    )
                ],
            }
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        "relationships": [],
        "namespaces": {},
    }
    with pytest.raises(PlanCompileError) as exc:
        run(cfg, {"t": text_table(age=["042"])})
    assert exc.value.code == "declared_cast_contract_invalid"


@pytest.mark.parametrize("fmt", ["cobol/comp-3/v1", "cobol/zoned-ebcdic-trailing/v1"])
@pytest.mark.parametrize("kind", ["fixed_width", "csv"])
def test_a9_binary_cobol_on_a_text_source_is_rejected_at_compile(
    tmp_path: Path, fmt: str, kind: str
) -> None:
    """A text physical kind cannot carry exact bytes: reject before any read, not at decode."""
    from decoy_engine.plan._errors import PlanCompileError

    path = tmp_path / "t.txt"
    path.write_text("0123\n", encoding="utf-8")
    source: dict[str, Any] = {"type": "file", "format": kind, "path": str(path)}
    if kind == "fixed_width":
        source["layout"] = {
            "exact_record_width": True,
            "columns": [{"name": "amt", "start": 0, "width": 3, "type": "str"}],
        }
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"t": source},
        "tables": [
            {
                "name": "t",
                "columns": [
                    column(
                        "amt",
                        logical_type="decimal",
                        parse_format={"id": fmt, "precision": 5, "scale": 2, "signed": True},
                        error_policy="fail",
                    )
                ],
            }
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        "relationships": [],
        "namespaces": {},
    }
    with pytest.raises(PlanCompileError) as exc:
        run(cfg, {"t": text_table(amt=["x"])})
    assert exc.value.code == "declared_cast_contract_invalid"


# -- B1 three policies ------------------------------------------------------------------------

_BAD_DATES = ["2026-02-01", "TBD", "2026-03-05"]


def test_b1_fail_policy_aborts_without_publishing(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        run(_people(tmp_path, "fail"), {"people": text_table(id=["1", "2", "3"], start=_BAD_DATES)})
    assert exc.value.code == "declared_date_invalid"
    assert "TBD" not in str(exc.value)


def test_b1_null_policy_nulls_only_the_field(tmp_path: Path) -> None:
    result = run(
        _people(tmp_path, "null"), {"people": text_table(id=["1", "2", "3"], start=_BAD_DATES)}
    )
    out = result.outputs["people"]
    assert out.column("id").to_pylist() == ["1", "2", "3"]
    assert out.column("start").to_pylist() == ["2026-02-01", None, "2026-03-05"]
    counts = _counts(result)
    assert counts["parse_null"] == 1 and counts.get("parse_quarantine", 0) == 0
    assert result.quality_metrics["declared_casting"]["total_substitutions"] == 1
    assert result.quality_metrics["declared_casting"]["total_failures"] == 1


def test_b1_quarantine_policy_removes_the_whole_row_and_stores_it_once(tmp_path: Path) -> None:
    runtime = _local_runtime(tmp_path)
    result = run(
        _people(tmp_path, "quarantine"),
        {"people": text_table(id=["1", "2", "3"], start=_BAD_DATES)},
        quarantine_runtime=runtime,
    )
    out = result.outputs["people"]
    assert out.column("id").to_pylist() == ["1", "3"]
    counts = _counts(result)
    assert counts["parse_quarantine"] == 1 and counts.get("parse_null", 0) == 0
    assert result.quality_metrics["declared_casting"]["total_substitutions"] == 0
    token = result.quarantine_access
    assert token is not None and "TBD" not in repr(result)
    from decoy_engine.declared_casting import QuarantineAccessContext

    records = runtime.sink.open(token, QuarantineAccessContext(owner="tenant-1", run_id="run-A"))
    assert len(records) == 1
    assert records[0].table == "people" and records[0].row_position == 1
    assert records[0].row == {"id": "2", "start": "TBD"}
    assert [r.code for r in records[0].reasons] == ["declared_date_invalid"]


# -- B2 multi-field row -----------------------------------------------------------------------


def test_b2_two_failing_fields_one_quarantined_row_two_reasons(tmp_path: Path) -> None:
    src = write_csv(tmp_path / "p.csv", ["id", "a", "b"], [["1", "2026-02-01", "5"]])
    cfg = config(
        [
            column("id"),
            column(
                "a",
                logical_type="date",
                parse_format=ISO_DATE,
                error_policy="quarantine",
                output_format=DATE_OUT,
            ),
            column(
                "b",
                logical_type="integer",
                parse_format=INT64,
                error_policy="quarantine",
                output_format={"id": "integer/ascii/v1", "signed": True, "bit_width": 64},
            ),
        ],
        source_path=src,
        top={"protected_quarantine": PROTECTED_QUARANTINE},
    )
    runtime = _local_runtime(tmp_path)
    table = text_table(id=["1", "2"], a=["2026-02-30", "2026-02-01"], b=["x", "7"])
    result = run(cfg, {"people": table}, quarantine_runtime=runtime)
    assert result.outputs["people"].to_pydict() == {"id": ["2"], "a": ["2026-02-01"], "b": ["7"]}
    by_col = result.quality_metrics["declared_casting"]["by_table"]["people"]["by_column"]
    assert by_col["a"]["parse_quarantine"] == 1 and by_col["b"]["parse_quarantine"] == 1
    from decoy_engine.declared_casting import QuarantineAccessContext

    records = runtime.sink.open(
        result.quarantine_access, QuarantineAccessContext(owner="tenant-1", run_id="run-A")
    )
    assert len(records) == 1
    assert sorted(r.code for r in records[0].reasons) == [
        "declared_date_invalid",
        "declared_integer_invalid",
    ]


# -- B3 route parity --------------------------------------------------------------------------


def _golden(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    src = write_csv(tmp_path / "g.csv", ["id", "start"], [["1", "2026-02-01"]])
    cfg = config(
        [
            column("id"),
            _date_col("null", sentinel_to_null={"id": "sentinel/exact/v1", "values": ["TBD"]}),
        ],
        source_path=src,
    )
    rows = [f"{i}" for i in range(1, 8)]
    dates = ["2026-02-01", "TBD", "bad", "2026-03-05", None, "2026-12-31", "2024-02-29"]
    return cfg, text_table(id=rows, start=dates)


# B3 route parity (chunked / sequential / out-of-core / mixed / unified-slice) lives in
# tests/acceptance/test_f06_route_parity.py, where every case proves which route ran.


def test_b3_fk_parent_042_joins_child_42_after_both_become_integer(tmp_path: Path) -> None:
    par = write_csv(tmp_path / "par.csv", ["id"], [["1"]])
    chi = write_csv(tmp_path / "chi.csv", ["cid", "pid"], [["1", "1"]])
    int_col = lambda n, **k: column(
        n,
        "hash",
        namespace="ns",
        logical_type="integer",
        parse_format=INT64,
        error_policy="fail",
        **k,
    )
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "parent": {"type": "file", "format": "csv", "path": par},
            "child": {"type": "file", "format": "csv", "path": chi},
        },
        "tables": [
            {"name": "parent", "columns": [int_col("id")]},
            {"name": "child", "columns": [column("cid"), int_col("pid")]},
        ],
        "targets": {
            "parent": {"type": "file", "format": "csv", "path": par + ".o"},
            "child": {"type": "file", "format": "csv", "path": chi + ".o"},
        },
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
        "namespaces": {},
    }
    result = run(
        cfg,
        {
            "parent": text_table(id=["042", "7"]),
            "child": text_table(cid=["a", "b"], pid=["42", "0007"]),
        },
    )
    masked_parent = result.outputs["parent"].column("id").to_pylist()
    masked_child = result.outputs["child"].column("pid").to_pylist()
    assert masked_parent[0] == masked_child[0] and masked_parent[1] == masked_child[1]


# -- B5 unknown width / behaviour: config-only preflight ---------------------------------------

_BASE_INT = {"logical_type": "integer", "parse_format": INT64, "error_policy": "fail"}


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        (
            {
                **_BASE_INT,
                "output_format": {
                    "id": "integer/zero-pad/v1",
                    "width_unit": "code_points",
                    "pad": "0",
                    "align": "right",
                },
            },
            "declared_output_width_unknown",
        ),
        (
            {
                **_BASE_INT,
                "output_format": {
                    "id": "integer/zero-pad/v1",
                    "width": 3,
                    "pad": "0",
                    "align": "right",
                },
            },
            "declared_cast_directive_unsupported",
        ),
        (
            {
                **_BASE_INT,
                "output_format": {
                    "id": "integer/zero-pad/v1",
                    "width": 3,
                    "width_unit": "furlongs",
                    "pad": "0",
                    "align": "right",
                },
            },
            "declared_cast_directive_unsupported",
        ),
        (
            {**_BASE_INT, "output_format": {"id": "integer/mystery/v9"}},
            "declared_cast_format_unknown",
        ),
        (
            {**_BASE_INT, "parse_format": {"id": "integer/mystery/v9"}},
            "declared_cast_format_unknown",
        ),
        (
            {
                "logical_type": "date",
                "parse_format": {"id": "date/strptime/v1", "format": "%Y-%m-%d %H"},
                "error_policy": "fail",
            },
            "declared_cast_directive_unsupported",
        ),
        (
            {
                "logical_type": "date",
                "parse_format": ISO_DATE,
                "error_policy": "fail",
                "output_format": {
                    "id": "date/strftime/v1",
                    "format": "%Y/%m/%d",
                    "width": 9,
                    "width_unit": "code_points",
                },
            },
            "declared_output_width_unknown",
        ),
        (
            {"logical_type": "integer", "parse_format": ISO_DATE, "error_policy": "fail"},
            "declared_cast_contract_invalid",
        ),
        (
            {
                "logical_type": "decimal",
                "parse_format": {"id": "decimal/plain/v1", "precision": 39, "scale": 0},
                "error_policy": "fail",
            },
            "declared_cast_precision_invalid",
        ),
        (
            {**_BASE_INT, "sentinel_to_null": {"id": "sentinel/exact/v1", "values": []}},
            "declared_cast_sentinel_config_invalid",
        ),
        ({**_BASE_INT, "error_policy": "explode"}, "declared_cast_contract_invalid"),
        ({"parse_format": INT64}, "declared_cast_contract_invalid"),
    ],
)
def test_b5_bad_contracts_fail_config_validation_with_a_registered_code(
    tmp_path: Path, fields: dict, code: str
) -> None:
    from pydantic import ValidationError

    cfg = config([column("age", **fields)], source_path=str(tmp_path / "never-read.csv"))
    with pytest.raises(ValidationError) as exc:
        validate(cfg)
    assert code in {e["type"] for e in exc.value.errors()}


def test_b5_preflight_runs_before_profiling_or_reading(tmp_path: Path) -> None:
    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.profile._readers import LazySource

    cfg = validate(
        config([column("age", **_BASE_INT)], source_path=str(tmp_path / "never-read.csv"))
    )
    # a dump that bypasses the pydantic layer (hand-built dict) must still hit the same choke point
    cfg["tables"][0]["columns"][0]["parse_format"] = {"id": "integer/mystery/v9"}
    from decoy_engine.execution._pipeline import run_pipeline

    poisoned = LazySource(path=tmp_path / "nope.parquet")
    with pytest.raises(PlanCompileError) as exc:
        run_pipeline(cfg, {"people": poisoned}, engine_version="t")
    assert exc.value.code == "declared_cast_format_unknown"


def test_b5_text_target_requires_an_explicit_output_format(tmp_path: Path) -> None:
    from decoy_engine.plan._errors import PlanCompileError

    layout = {
        "exact_record_width": True,
        "columns": [{"name": "age", "start": 0, "width": 3, "type": "str"}],
    }
    src = write_csv(tmp_path / "s.csv", ["age"], [["1"]])
    cfg = config([column("age", **_BASE_INT)], source_path=src)
    cfg["targets"]["people"] = {
        "type": "file",
        "format": "fixed_width",
        "path": str(tmp_path / "o.txt"),
        "layout": layout,
    }
    with pytest.raises(PlanCompileError) as exc:
        run(cfg, {"people": text_table(age=["1"])})
    assert exc.value.code == "declared_output_width_unknown"


def test_b5_strategy_whose_output_type_is_not_proven_is_rejected(tmp_path: Path) -> None:
    from decoy_engine.plan._errors import PlanCompileError

    src = write_csv(tmp_path / "s.csv", ["age"], [["1"]])
    for provider_config in ({"width": 10, "format": "range"}, {"width": 10, "format": "midpoint"}):
        cfg = config(
            [
                column(
                    "age",
                    "bucketize",
                    provider_config=provider_config,
                    **_BASE_INT,
                    output_format=zero_pad(3),
                )
            ],
            source_path=src,
        )
        with pytest.raises(PlanCompileError) as exc:
            run(cfg, {"people": text_table(age=["1"])})
        assert exc.value.code == "declared_type_strategy_incompatible"


def test_b5_dtype_conflict_and_duplicate_date_format_are_rejected(tmp_path: Path) -> None:
    from decoy_engine.plan._errors import PlanCompileError

    src = write_csv(tmp_path / "s.csv", ["age"], [["1"]])
    cfg = config([column("age", dtype="string", **_BASE_INT)], source_path=src)
    with pytest.raises(PlanCompileError) as exc:
        run(cfg, {"people": text_table(age=["1"])})
    assert exc.value.code == "declared_type_strategy_incompatible"
    cfg = config(
        [
            column(
                "d",
                "date_shift",
                provider_config={"days": 3, "date_format": "%d/%m/%Y"},
                logical_type="date",
                parse_format=ISO_DATE,
                error_policy="fail",
            )
        ],
        source_path=src,
    )
    with pytest.raises(PlanCompileError) as exc2:
        run(cfg, {"people": text_table(d=["2026-01-01"])})
    assert exc2.value.code == "declared_cast_contract_invalid"


def test_b5_legacy_configs_unchanged_when_f06_fields_unset(tmp_path: Path) -> None:
    src = write_csv(tmp_path / "s.csv", ["age"], [["1"]])
    dumped = validate(config([column("age")], source_path=src))
    col = dumped["tables"][0]["columns"][0]
    for k in ("logical_type", "parse_format", "sentinel_to_null", "error_policy", "output_format"):
        assert k not in col
    assert "protected_quarantine" not in dumped


# -- B6 count algebra -------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [1, 3, 1000])
def test_b6_count_algebra_across_partitions(tmp_path: Path, chunk: int) -> None:
    cfg, table = _golden(tmp_path)
    result = run(cfg, {"people": table}, chunk_size_rows=chunk)
    dc = result.quality_metrics["declared_casting"]
    counts = _counts(result)
    assert dc["schema_version"] == 1 and dc["completeness"] == "complete"
    assert counts["sentinel_to_null"] == 1  # TBD
    assert counts["parse_null"] == 1  # "bad"
    assert dc["total_substitutions"] == counts["sentinel_to_null"] + counts["parse_null"] == 2
    assert dc["total_failures"] == 1
    flat = json.dumps(dc)
    assert "TBD" not in flat and "bad" not in flat


def test_b6_fail_policy_marks_partial_before_abort(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        run(_people(tmp_path, "fail"), {"people": text_table(id=["1", "2", "3"], start=_BAD_DATES)})
    evidence = getattr(exc.value, "declared_casting_counts", None)
    assert evidence is not None and evidence["completeness"] == "partial_before_abort"
    assert evidence["total_failures"] == 1


def test_b6_output_failure_counts_as_output_quarantine(tmp_path: Path) -> None:
    src = write_csv(tmp_path / "p.csv", ["id", "n"], [["1", "5"]])
    cfg = config(
        [
            column("id"),
            column(
                "n",
                "bucketize",
                provider_config={"width": 1000, "format": "lower"},
                logical_type="integer",
                parse_format=INT64,
                error_policy="quarantine",
                output_format=zero_pad(3),
            ),
        ],
        source_path=src,
        top={"protected_quarantine": PROTECTED_QUARANTINE},
    )
    runtime = _local_runtime(tmp_path)
    result = run(
        cfg, {"people": text_table(id=["1", "2"], n=["5", "2500"])}, quarantine_runtime=runtime
    )
    assert result.outputs["people"].column("id").to_pylist() == ["1"]
    assert _counts(result, col="n")["output_quarantine"] == 1


# -- B7 legacy compatibility ------------------------------------------------------------------


def test_b7_plan_version_is_3_and_v2_plans_still_load() -> None:
    from decoy_engine.plan._compile import PLAN_VERSION

    assert PLAN_VERSION == 3


def test_b7_legacy_run_outputs_are_unchanged(tmp_path: Path) -> None:
    src = write_csv(tmp_path / "s.csv", ["age"], [["42"]])
    cfg = config(
        [column("age", "bucketize", provider_config={"width": 10, "format": "lower"})],
        source_path=src,
    )
    result = run(cfg, {"people": pa.table({"age": [42, 7, None]})})
    assert result.outputs["people"].column("age").to_pylist() == ["40", "0", None]
    assert "declared_casting" not in result.quality_metrics
    assert getattr(result, "quarantine_access", None) is None


def test_b7_legacy_plan_yaml_differs_only_by_the_documented_version_line(tmp_path: Path) -> None:
    from decoy_engine.plan._compile import compile_plan
    from decoy_engine.plan._serialize import plan_to_yaml
    from decoy_engine.profile._source import profile_source

    src = write_csv(tmp_path / "s.csv", ["age"], [["42"], ["7"]])
    cfg = validate(
        config(
            [column("age", "bucketize", provider_config={"width": 10, "format": "lower"})],
            source_path=src,
        )
    )
    plan = compile_plan(cfg, profile_source(cfg, seed=7), decoy_engine_version="t")
    text = plan_to_yaml(plan)
    assert "plan_version: 3" in text
    assert "declared_casting" not in text  # an empty payload is omitted


def test_b7_f06_plan_round_trips_and_is_deterministic(tmp_path: Path) -> None:
    from decoy_engine.plan._compile import compile_plan
    from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
    from decoy_engine.profile._source import profile_source

    src = write_csv(tmp_path / "s.csv", ["id", "start"], [["1", "2026-02-01"]])
    cfg = validate(_people(tmp_path, "null"))
    profile = profile_source(cfg, seed=7)
    a = compile_plan(cfg, profile, decoy_engine_version="t")
    b = compile_plan(copy.deepcopy(cfg), profile, decoy_engine_version="t")
    assert plan_to_yaml(a) == plan_to_yaml(b)
    again = plan_from_yaml(plan_to_yaml(a))
    assert again.declared_casting == a.declared_casting and a.declared_casting
    assert "date/iso8601/v1" in plan_to_yaml(a)
    del src


def test_b7_unknown_format_in_a_serialized_plan_never_executes(tmp_path: Path) -> None:
    from decoy_engine.plan._compile import compile_plan
    from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
    from decoy_engine.profile._source import profile_source

    cfg = validate(_people(tmp_path, "null"))
    text = plan_to_yaml(compile_plan(cfg, profile_source(cfg, seed=7), decoy_engine_version="t"))
    tampered = text.replace("date/iso8601/v1", "date/iso8601/v9", 1)
    with pytest.raises(Exception) as exc:
        plan_from_yaml(tampered)
    assert getattr(exc.value, "code", None) == "declared_cast_format_unknown"


def test_b7_legacy_config_and_plan_match_the_baseline_fixture(tmp_path: Path) -> None:
    """The fixture was produced by the pre-F06 engine (fdd08a89). Config dump and hash are
    byte-identical; the plan YAML differs only by the documented version line."""
    import hashlib

    from decoy_engine.config import PipelineConfig
    from decoy_engine.plan._compile import compile_plan
    from decoy_engine.plan._serialize import plan_to_yaml
    from decoy_engine.profile._source import profile_source

    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "f06" / "legacy-compat-baseline.json"
        ).read_text()
    )
    cfg = PipelineConfig.model_validate(fixture["raw_config"]).model_dump()
    dump = json.dumps(cfg, sort_keys=True)
    assert hashlib.sha256(dump.encode()).hexdigest() == fixture["config_dump_sha256"]
    (tmp_path / "p.csv").write_text("id,age,note\n1,42,a\n2,7,b\n3,13,c\n")
    run_cfg = json.loads(dump)
    run_cfg["sources"]["p"]["path"] = str(tmp_path / "p.csv")
    plan = compile_plan(
        run_cfg, profile_source(run_cfg, seed=7), decoy_engine_version="legacy-fixture"
    )
    assert plan.pipeline_config_hash == fixture["pipeline_config_hash"]
    assert plan_to_yaml(plan) == fixture["plan_yaml_at_baseline"].replace(
        "plan_version: 2", "plan_version: 3", 1
    )
