"""F06 B3 (real routes): the same declared contract gives the same rows, schema, counts and
dispositions on every route that admits it, and the harness proves WHICH route ran.

A parity test that silently runs one route twice proves nothing, so every case asserts the
route identity the engine reports before comparing results.
"""

from __future__ import annotations

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
    write_csv,
    zero_pad,
)

DATE_OUT = {"id": "date/iso8601/v1", "width": 10, "width_unit": "code_points"}
IDS = [str(i) for i in range(1, 9)]
DATES = ["2026-02-01", "TBD", "bad", "2026-03-05", None, "2026-12-31", "2024-02-29", "2026-02-01"]
AGES = ["042", "7", "x", "1000", "5", None, "12", "0"]
SENTINEL = {"id": "sentinel/exact/v1", "values": ["TBD"]}


def _cfg(tmp_path: Path, policy: str) -> dict[str, Any]:
    src = write_csv(tmp_path / "g.csv", ["id", "start", "age"], [["1", "2026-02-01", "1"]])
    cols = [
        column("id"),
        column(
            "start",
            logical_type="date",
            parse_format=ISO_DATE,
            sentinel_to_null=SENTINEL,
            error_policy=policy,
            output_format=DATE_OUT,
        ),
        column(
            "age",
            logical_type="integer",
            parse_format=INT64,
            error_policy=policy,
            output_format=zero_pad(3),
        ),
    ]
    top = {"protected_quarantine": PROTECTED_QUARANTINE} if policy == "quarantine" else None
    return config(cols, source_path=src, top=top)


def _source() -> pa.Table:
    return text_table(id=IDS, start=DATES, age=AGES)


def _runtime(root: Path) -> Any:
    from decoy_engine.declared_casting import (
        LocalProtectedQuarantineSink,
        ProtectedQuarantineRuntime,
    )
    from decoy_engine.keyprovider import SecretKeyProvider

    sink = LocalProtectedQuarantineSink(root, key_provider=SecretKeyProvider(secret=b"q" * 32))
    return ProtectedQuarantineRuntime(sink=sink, owner="tenant-1", run_id="run-A")


def _kwargs(policy: str, tmp_path: Path, tag: str) -> dict[str, Any]:
    return {"quarantine_runtime": _runtime(tmp_path / f"q-{tag}")} if policy == "quarantine" else {}


def _opened(result: Any, tmp_path: Path, tag: str) -> list[tuple[str, int, tuple[str, ...], Any]]:
    """The protected records of a run, order-normalized, with their F29 identities."""
    from decoy_engine.declared_casting import QuarantineAccessContext

    sink = _runtime(tmp_path / f"q-{tag}").sink
    records = sink.open(
        result.quarantine_access, QuarantineAccessContext(owner="tenant-1", run_id="run-A")
    )
    return sorted(
        (r.table, r.row_position, tuple(x.code for x in r.reasons), tuple(sorted(r.row.items())))
        for r in records
    )


def _facts(result: Any) -> dict[str, Any]:
    counts = dict(result.quality_metrics["declared_casting"])
    counts.pop("quarantine", None)
    return {"table": result.outputs["people"], "counts": counts}


def _full_frame(tmp_path: Path, policy: str) -> Any:
    return run(
        _cfg(tmp_path, policy),
        {"people": _source()},
        execution_mode="full_frame",
        auto_chunk=False,
        **_kwargs(policy, tmp_path, "full"),
    )


@pytest.mark.parametrize("policy", ["null", "quarantine"])
@pytest.mark.parametrize("chunk", [1, 2, 3, 100])
def test_chunked_route_matches_full_frame_and_is_really_chunked(
    tmp_path: Path, policy: str, chunk: int
) -> None:
    base = _full_frame(tmp_path, policy)
    assert base.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    got = run(
        _cfg(tmp_path, policy),
        {"people": _source()},
        auto_chunk=True,
        chunk_size_rows=chunk,
        auto_chunk_threshold_rows=1,
        **_kwargs(policy, tmp_path, f"chunk{chunk}"),
    )
    block = got.quality_metrics["auto_chunk"]
    assert block["mode"] == "chunked", block["reason"]
    assert block["chunk_count"] == -(-8 // chunk)
    a, b = _facts(base), _facts(got)
    assert b["table"].equals(a["table"]) and b["table"].schema.equals(a["table"].schema)
    assert b["counts"] == a["counts"]
    if policy == "quarantine":
        assert _opened(got, tmp_path, f"chunk{chunk}") == _opened(base, tmp_path, "full")


# -- relational corpus: sequential and out-of-core are FK routes ---------------------------------

PARENT_IDS = ["042", "7", "0100", "bad", None]
CHILD_PIDS = ["42", "0007", "100", "42", "9"]


def _fk_cfg(tmp_path: Path, *, endpoints: bool, policy: str = "null") -> dict[str, Any]:
    par = write_csv(tmp_path / "par.csv", ["id", "label"], [["1", "a"]])
    chi = write_csv(tmp_path / "chi.csv", ["cid", "pid", "start"], [["1", "1", "2026-02-01"]])

    def key(name: str, **extra: Any) -> dict[str, Any]:
        return column(
            name,
            "hash",
            namespace="ns",
            deterministic=True,
            logical_type="integer",
            parse_format=INT64,
            error_policy=policy,
            **extra,
        )

    plain = column("id", "hash", namespace="ns", deterministic=True)
    plain_fk = column("pid", "hash", namespace="ns", deterministic=True)
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "parent": {"type": "file", "format": "csv", "path": par},
            "child": {"type": "file", "format": "csv", "path": chi},
        },
        "tables": [
            {"name": "parent", "columns": [key("id") if endpoints else plain, column("label")]},
            {
                "name": "child",
                "columns": [
                    column("cid"),
                    key("pid") if endpoints else plain_fk,
                    column(
                        "start",
                        logical_type="date",
                        parse_format=ISO_DATE,
                        error_policy=policy,
                        output_format=DATE_OUT,
                    ),
                ],
            },
        ],
        "targets": {
            "parent": {"type": "file", "format": "csv", "path": par + ".o"},
            "child": {"type": "file", "format": "csv", "path": chi + ".o"},
        },
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "remap",
                "namespace": "ns",
            }
        ],
        "namespaces": {},
    }
    if policy == "quarantine":
        cfg["protected_quarantine"] = PROTECTED_QUARANTINE
    return cfg


def _fk_sources() -> dict[str, pa.Table]:
    return {
        "parent": text_table(id=PARENT_IDS, label=list("abcde")),
        "child": text_table(
            cid=list("vwxyz"),
            pid=CHILD_PIDS,
            start=["2026-02-01", "nope", "2026-03-05", None, "2024-02-29"],
        ),
    }


def _fk_facts(result: Any) -> dict[str, Any]:
    counts = dict(result.quality_metrics["declared_casting"])
    counts.pop("quarantine", None)
    return {"outputs": result.outputs, "counts": counts}


def _fk_run(tmp_path: Path, mode: str, *, endpoints: bool, policy: str = "null") -> Any:
    return run(
        _fk_cfg(tmp_path, endpoints=endpoints, policy=policy),
        _fk_sources(),
        execution_mode=mode,
        **_kwargs(policy, tmp_path, f"fk-{mode}"),
    )


@pytest.mark.parametrize("policy", ["null", "quarantine"])
def test_sequential_route_matches_full_frame_for_declared_relationship_keys(
    tmp_path: Path, policy: str
) -> None:
    base = _fk_run(tmp_path, "full_frame", endpoints=True, policy=policy)
    assert base.quality_metrics["execution"]["execution_mode"] == "full_frame"
    got = _fk_run(tmp_path, "sequential", endpoints=True, policy=policy)
    assert got.quality_metrics["execution"]["execution_mode"] == "sequential"
    a, b = _fk_facts(base), _fk_facts(got)
    for name in ("parent", "child"):
        assert b["outputs"][name].equals(a["outputs"][name]), name
        assert b["outputs"][name].schema.equals(a["outputs"][name].schema), name
    assert b["counts"] == a["counts"]
    if policy == "quarantine":
        assert _opened(got, tmp_path, "fk-sequential") == _opened(base, tmp_path, "fk-full_frame")


def test_sequential_child_keys_join_the_parent_after_both_are_typed(tmp_path: Path) -> None:
    out = _fk_run(tmp_path, "sequential", endpoints=True).outputs
    parent_ids = out["parent"].column("id").to_pylist()
    child_pids = out["child"].column("pid").to_pylist()
    assert child_pids[0] == parent_ids[0]  # "042" and "42" are the same integer
    assert child_pids[1] == parent_ids[1]  # "7" and "0007"
    assert child_pids[2] == parent_ids[2]  # "0100" and "100"


def test_out_of_core_matches_full_frame_when_no_declared_column_is_a_key(tmp_path: Path) -> None:
    base = _fk_run(tmp_path, "full_frame", endpoints=False)
    got = _fk_run(tmp_path, "out_of_core", endpoints=False)
    assert got.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    a, b = _fk_facts(base), _fk_facts(got)
    for name in ("parent", "child"):
        assert b["outputs"][name].equals(a["outputs"][name]), name
    assert b["counts"] == a["counts"]


@pytest.mark.parametrize(
    ("endpoints", "policy"),
    [
        (True, "null"),  # a declared relationship key needs typed keys on both sides of a join
        (False, "quarantine"),  # removing rows would publish rows the key relations were built from
    ],
)
def test_incapable_route_refuses_before_any_source_is_read(
    tmp_path: Path, endpoints: bool, policy: str
) -> None:
    mode, code = "out_of_core", "declared_cast_route_unsupported"
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.profile._readers import LazySource

    poisoned = {
        "parent": LazySource(path=tmp_path / "missing-parent.parquet"),
        "child": LazySource(path=tmp_path / "missing-child.parquet"),
    }

    def boom(_name: str) -> pa.Table:
        raise AssertionError("a source was read before route admission")

    with pytest.raises(ExecutionError) as exc:
        run(
            _fk_cfg(tmp_path, endpoints=endpoints, policy=policy),
            poisoned,
            execution_mode=mode,
            source_loader=boom,
            **_kwargs(policy, tmp_path, "refuse"),
        )
    assert exc.value.code == code


def test_auto_routing_never_picks_a_route_that_cannot_honour_the_contract(
    tmp_path: Path,
) -> None:
    """Under `auto` an out-of-core-sized relational job with declared keys takes the capable
    bounded route instead of the incapable one."""
    got = run(
        _fk_cfg(tmp_path, endpoints=True),
        _fk_sources(),
        out_of_core_threshold_rows=1,
    )
    assert got.quality_metrics["execution"]["execution_mode"] in {"sequential", "full_frame"}
    assert got.quality_metrics["declared_casting"]["total_failures"] > 0


@pytest.mark.parametrize("reorder_rows", [None, 1])
@pytest.mark.parametrize("policy", ["null"])
def test_out_of_core_streams_lazy_sources_into_a_sink_with_the_same_result(
    tmp_path: Path, policy: str, reorder_rows: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parquet sources stream through the real streaming drivers (batch join, and the reorder
    driver when forced) into a transactional sink; the published tables equal full-frame."""
    import pyarrow.parquet as pq

    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
    from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
    from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
    from decoy_engine.profile._readers import LazySource

    saw = {"batch": 0, "reorder": 0}
    for key, cls in (("batch", ChildFkBatchJoiner), ("reorder", StreamFkJoiner)):
        original = cls.__init__

        def spy(self: Any, *a: Any, _o: Any = original, _k: str = key, **kw: Any) -> None:
            saw[_k] += 1
            _o(self, *a, **kw)

        monkeypatch.setattr(cls, "__init__", spy)
    base = _fk_run(tmp_path, "full_frame", endpoints=False, policy=policy)
    lazy: dict[str, Any] = {}
    for name, table in _fk_sources().items():
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path)
        lazy[name] = LazySource(path=path)
    target = tmp_path / "published"
    got = run(
        _fk_cfg(tmp_path, endpoints=False, policy=policy),
        lazy,
        execution_mode="out_of_core",
        sink=ParquetTransactionalSink(target),
        out_of_core_reorder_threshold_rows=reorder_rows,
        **_kwargs(policy, tmp_path, "fk-stream"),
    )
    assert got.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert saw["reorder" if reorder_rows else "batch"] > 0, saw  # the intended driver ran
    for name in ("parent", "child"):
        published = pq.read_table(target / f"{name}.parquet")
        assert published.to_pydict() == base.outputs[name].to_pydict(), name
    counts = dict(got.quality_metrics["declared_casting"])
    counts.pop("quarantine", None)
    assert counts == _fk_facts(base)["counts"]
    if policy == "quarantine":
        assert _opened(got, tmp_path, "fk-stream") == _opened(base, tmp_path, "fk-full_frame")


# -- mixed (generate + mask) ----------------------------------------------------------------------


def _mixed(tmp_path: Path, policy: str, *, coupled: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    from tests.parity import _f2_mixed_support as mx

    ages = ["042", "7", "bad", "1000", None, "12", "5", "0", "99", "8", "x", "300"]
    n = len(ages)
    child = pa.table(
        {
            mx.FK: pa.array([f"P{i % 5}" for i in range(n)], pa.string()),
            "email": pa.array([f"u{i}@example.com" for i in range(n)], pa.string()),
            "note": pa.array([f"note-{i}" for i in range(n)], pa.string()),
            "age": pa.array(ages, pa.string()),
        }
    )
    columns = [
        *mx.payload_columns(),
        column(
            "age",
            logical_type="integer",
            parse_format=INT64,
            error_policy=policy,
            output_format=zero_pad(3),
        ),
    ]
    if policy == "quarantine":
        # The fixture validates before the protected block exists, so inject both afterwards,
        # in the same dumped shape a validated config has.
        cfg = mx.mixed_config(tmp_path, child, child_columns=mx.payload_columns(), coupled=coupled)
        table = next(t for t in cfg["tables"] if t["name"] == mx.CHILD)
        table["columns"].append(columns[-1])
        cfg["protected_quarantine"] = PROTECTED_QUARANTINE
        return cfg, {mx.CHILD: child}
    cfg = mx.mixed_config(tmp_path, child, child_columns=columns, coupled=coupled)
    return cfg, {mx.CHILD: child}


@pytest.mark.parametrize("coupled", [False, True])
def test_mixed_route_matches_the_full_frame_oracle(tmp_path: Path, coupled: bool) -> None:
    from tests.parity import _f2_mixed_support as mx

    cfg, sources = _mixed(tmp_path, "null", coupled=coupled)
    want = mx.oracle(cfg, sources)
    got = mx.run(cfg, sources)
    assert "mixed_route" in got.quality_metrics, "the production mixed route did not run"
    assert "mixed_route" not in want.quality_metrics
    mx.assert_outputs_identical(got.outputs, want.outputs)
    a = dict(want.quality_metrics["declared_casting"])
    b = dict(got.quality_metrics["declared_casting"])
    assert a == b
    assert a["total_failures"] > 0
    assert mx.chunk_count(got) > 1  # really chunked, not one slab


def test_mixed_route_declines_a_contract_it_cannot_honour_and_still_matches(
    tmp_path: Path,
) -> None:
    """Row quarantine would desync the pre-resolved key column, so the router keeps this job
    off the mixed route; the result is still the oracle's."""
    from tests.parity import _f2_mixed_support as mx

    cfg, sources = _mixed(tmp_path, "quarantine", coupled=True)
    kwargs = {"quarantine_runtime": _runtime(tmp_path / "q-mixed")}
    want = mx.oracle(cfg, sources, **kwargs)
    got = mx.run(cfg, sources, **{"quarantine_runtime": _runtime(tmp_path / "q-mixed2")})
    assert "mixed_route" not in got.quality_metrics
    mx.assert_outputs_identical(got.outputs, want.outputs)


# -- unified slice ----------------------------------------------------------------------------


def _int_only(tmp_path: Path, policy: str) -> dict[str, Any]:
    """The slice admits the scalar strategies over string/integer columns, not date32."""
    cfg = _cfg(tmp_path, policy)
    table = cfg["tables"][0]
    table["columns"] = [c for c in table["columns"] if c["name"] != "start"]
    return cfg


@pytest.mark.parametrize("policy", ["null", "quarantine"])
def test_unified_slice_route_matches_the_full_frame_oracle(tmp_path: Path, policy: str) -> None:
    key = "unified_slice_activation"
    source = _source().drop_columns(["start"])
    want = run(
        _int_only(tmp_path, policy),
        {"people": source},
        unified_slice_enabled=False,
        auto_chunk=False,
        **_kwargs(policy, tmp_path, "uni-oracle"),
    )
    got = run(
        _int_only(tmp_path, policy),
        {"people": source},
        auto_chunk=False,
        **_kwargs(policy, tmp_path, "uni"),
    )
    assert key not in want.quality_metrics
    assert key in got.quality_metrics, "the unified slice did not take the job"
    a, b = _facts(want), _facts(got)
    assert b["table"].equals(a["table"]) and b["table"].schema.equals(a["table"].schema)
    assert b["counts"] == a["counts"]
    if policy == "quarantine":
        assert _opened(got, tmp_path, "uni") == _opened(want, tmp_path, "uni-oracle")


# -- binary cells ---------------------------------------------------------------------------------


def _comp3(value: str) -> bytes:
    from decimal import Decimal

    from decoy_engine.declared_casting import format_value

    spec = {"id": "cobol/comp-3/v1", "precision": 5, "scale": 2, "signed": True}
    outcome = format_value(spec, Decimal(value))
    assert outcome.ok
    return bytes(outcome.value)


def test_binary_comp3_cells_keep_their_bytes_on_the_chunked_route(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    comp3 = {"id": "cobol/comp-3/v1", "precision": 5, "scale": 2, "signed": True}
    cells = [_comp3("12.34"), _comp3("-0.05"), b"\xff\xff\xff", None, _comp3("999.99")]
    path = tmp_path / "amt.parquet"
    pq.write_table(
        pa.table({"id": ["1", "2", "3", "4", "5"], "amt": pa.array(cells, pa.binary())}), path
    )
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    column("id"),
                    column(
                        "amt",
                        logical_type="decimal",
                        parse_format=comp3,
                        error_policy="null",
                        output_format=comp3,
                    ),
                ],
            }
        ],
        "targets": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "o.parquet")}
        },
        "relationships": [],
        "namespaces": {},
    }
    table = pq.read_table(path)
    base = run(cfg, {"t": table}, execution_mode="full_frame", auto_chunk=False)
    got = run(cfg, {"t": table}, auto_chunk=True, chunk_size_rows=2, auto_chunk_threshold_rows=1)
    assert got.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert got.outputs["t"].equals(base.outputs["t"])
    out = got.outputs["t"].column("amt").to_pylist()
    assert out[0] == cells[0] and out[1] == cells[1] and out[4] == cells[4]  # canonical bytes
    assert out[2] is None and out[3] is None  # invalid bytes -> null; physical null stays null
    counts = got.quality_metrics["declared_casting"]["by_table"]["t"]["by_column"]["amt"]
    assert counts == base.quality_metrics["declared_casting"]["by_table"]["t"]["by_column"]["amt"]


# -- decimals: stable only when rendered --------------------------------------------------------

AMOUNTS = ["12.34", "0.5", "999.99", "1.234", None, "-3.10", "bad", "7"]
DEC = {"id": "decimal/plain/v1", "precision": 5, "scale": 2}
DEC_OUT = {
    "id": "decimal/plain/v1",
    "precision": 5,
    "scale": 2,
    "width": 8,
    "width_unit": "code_points",
}


def _dec_cfg(tmp_path: Path, *, rendered: bool) -> dict[str, Any]:
    src = write_csv(tmp_path / "d.csv", ["id", "amt"], [["1", "1.00"]])
    extra = {"output_format": DEC_OUT} if rendered else {}
    cols = [
        column("id"),
        column("amt", logical_type="decimal", parse_format=DEC, error_policy="null", **extra),
    ]
    return config(cols, source_path=src)


@pytest.mark.parametrize("chunk", [1, 3])
def test_rendered_decimal_is_chunk_stable_and_matches_full_frame(
    tmp_path: Path, chunk: int
) -> None:
    source = text_table(id=IDS, amt=AMOUNTS)
    base = run(
        _dec_cfg(tmp_path, rendered=True),
        {"people": source},
        execution_mode="full_frame",
        auto_chunk=False,
    )
    got = run(
        _dec_cfg(tmp_path, rendered=True),
        {"people": source},
        auto_chunk=True,
        chunk_size_rows=chunk,
        auto_chunk_threshold_rows=1,
    )
    assert got.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert got.outputs["people"].equals(base.outputs["people"])
    assert got.quality_metrics["declared_casting"] == base.quality_metrics["declared_casting"]


def test_unrendered_decimal_keeps_the_job_off_the_chunked_route(tmp_path: Path) -> None:
    """Its Arrow precision is rebuilt per chunk, so the planner refuses chunking with a reason
    and the job runs full-frame with the same result."""
    source = text_table(id=IDS, amt=AMOUNTS)
    got = run(
        _dec_cfg(tmp_path, rendered=False),
        {"people": source},
        auto_chunk=True,
        chunk_size_rows=2,
        auto_chunk_threshold_rows=1,
    )
    block = got.quality_metrics["auto_chunk"]
    assert block["mode"] == "full_frame" and "declared, no output_format" in block["reason"]


# -- a failed publish leaves no sealed quarantine on any route that can quarantine -----------------


def _sealed(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.dqa")] if root.exists() else []


def test_sequential_sink_commit_failure_discards_the_sealed_quarantine(tmp_path: Path) -> None:
    from tests.parity._f2_mixed_support import RecordingSink

    sink = RecordingSink(fail_on="commit")
    with pytest.raises(RuntimeError, match="injected sink failure"):
        run(
            _fk_cfg(tmp_path, endpoints=True, policy="quarantine"),
            _fk_sources(),
            execution_mode="sequential",
            sink=sink,
            **_kwargs("quarantine", tmp_path, "commit-fail"),
        )
    assert sink.aborted == 1
    assert _sealed(tmp_path / "q-commit-fail") == []


def test_chunked_post_seal_failure_leaves_no_quarantine_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure fires in the publish step, after the chunked route sealed its quarantine; the
    injection itself asserts the sealed artifact exists, so the discard is what removes it."""
    from decoy_engine.execution._chunked_output_sink import OutputPublish

    root = tmp_path / "q-late"
    seen: list[int] = []

    def boom(self: Any) -> None:
        seen.append(len(_sealed(root)))
        raise RuntimeError("late failure after the seal")

    monkeypatch.setattr(OutputPublish, "commit", boom)
    with pytest.raises(RuntimeError, match="late failure"):
        run(
            _cfg(tmp_path, "quarantine"),
            {"people": _source()},
            auto_chunk=True,
            chunk_size_rows=2,
            auto_chunk_threshold_rows=1,
            **_kwargs("quarantine", tmp_path, "late"),
        )
    assert seen == [1], "the chunked route had not sealed its quarantine when the failure fired"
    assert _sealed(root) == []


def test_chunked_route_keeps_its_schema_when_every_row_is_quarantined(tmp_path: Path) -> None:
    source = text_table(id=["1", "2", "3"], start=["bad", "bad", "bad"], age=["x", "y", "z"])
    base = run(
        _cfg(tmp_path, "quarantine"),
        {"people": source},
        execution_mode="full_frame",
        auto_chunk=False,
        **_kwargs("quarantine", tmp_path, "empty-full"),
    )
    got = run(
        _cfg(tmp_path, "quarantine"),
        {"people": source},
        auto_chunk=True,
        chunk_size_rows=2,
        auto_chunk_threshold_rows=1,
        **_kwargs("quarantine", tmp_path, "empty-chunk"),
    )
    assert got.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert got.outputs["people"].num_rows == 0
    assert got.outputs["people"].schema.equals(base.outputs["people"].schema)
    assert got.quality_metrics["declared_casting"]["quarantined_rows"] == 3
    assert _opened(got, tmp_path, "empty-chunk") == _opened(base, tmp_path, "empty-full")
