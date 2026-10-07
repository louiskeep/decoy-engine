"""C8-iii-a acceptance: `when:` on text_redact, bucket_perturb and date_shift, chunked route.

Plan: `docs/plans/2026-10-07-c8-iii-a-when-operators.md` rev 3, section 4. The native leg runs
with the oracle fallback poisoned, so a silent reroute fails. The lane-off leg is the same
columns beside a forced-oracle column, which keeps the chunked oracle's own output schema rule.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _chunked_oracle
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.plan._errors import PlanCompileError
from tests.native._b8_support import FORCE, identical, with_force
from tests.native._c6c_i_support import ADMITTED_CONFIGS
from tests.native._c8_iii_a_support import (
    BAD_DATE,
    CHUNKINGS,
    KINDS,
    MIXED_OFFSET_VALUES,
    SPECIAL_FORMAT_JOBS,
    SPECIAL_FORMATS,
    N,
    chunk_by_sizes,
    columns,
    declines,
    make_table,
    predicates,
    value,
)
from tests.native._chunked_bucket_perturb_support import bp_col, date_value
from tests.native._chunked_date_shift_support import (
    FORMAT_ERROR_REASON,
    Outcome,
    run_outcome,
)
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    TABLE,
    force_oracle,
    forced_reason,
    make_config,
    passthrough,
)

GOLDENS = json.loads((Path(__file__).parent / "_c8_iii_a_main_goldens.json").read_text())


@contextmanager
def no_oracle() -> Iterator[None]:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the chunked oracle ran on a table that must stay native")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_chunked_oracle, "_oracle_masked", _boom)
        yield


def legs(cols: list[dict[str, Any]], chunks: list[pa.Table], **kw: Any) -> tuple[Outcome, Outcome]:
    """(native leg with the oracle poisoned, forced-oracle leg) of the same columns and chunks."""
    with no_oracle():
        native = run_outcome(make_config(cols), chunks, **kw)
    forced = run_outcome(
        make_config([*cols, force_oracle(FORCE)]), [with_force(c) for c in chunks], **kw
    )
    assert len(native.ev) == 1 and native.ev[0].native_admitted is True, native.ev
    assert native.ev[0].reroute_reason is None
    assert len(forced.ev) == 1 and forced.ev[0].native_admitted is False, forced.ev
    assert forced_reason(FORCE) in (forced.ev[0].reroute_reason or "")
    assert not isinstance(native.error, AssertionError), native.error
    return native, forced


def _records(outcome: Outcome) -> tuple[RowErrorRecord, ...]:
    assert isinstance(outcome.error, RowErrorsFailedError), outcome.error
    return tuple(outcome.error.records)


def _metrics(result: Any) -> dict[str, Any]:
    return {k: v for k, v in result.quality_metrics.items() if k != "chunked_route"}


def assert_same(native: Outcome, forced: Outcome) -> None:
    """Tables, schema and metadata, warnings, row errors and metrics are equal chunk by chunk."""
    assert type(native.error) is type(forced.error)
    if native.error is not None:
        if isinstance(native.error, RowErrorsFailedError):
            assert _records(native) == _records(forced)
        else:
            assert str(native.error) == str(forced.error)
    assert len(native.out) == len(forced.out)
    for i, (got, want) in enumerate(zip(native.out, forced.out, strict=True)):
        assert identical(got, want.drop_columns([FORCE])), i
    assert len(native.sink) == len(forced.sink)
    for got, want in zip(native.sink, forced.sink, strict=True):
        assert tuple(got.warnings) == tuple(want.warnings)
        assert tuple(got.row_errors) == tuple(want.row_errors)
        assert _metrics(got) == _metrics(want)
    assert native.chunks_pulled == forced.chunks_pulled


# ---------------------------------------------------------------------------
# 1. Matrix: operator x selectivity x reference x chunking.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunking", sorted(CHUNKINGS))
@pytest.mark.parametrize("predicate", sorted(predicates("text_redact")))
@pytest.mark.parametrize("kind", KINDS)
def test_1_matrix(kind: str, predicate: str, chunking: str) -> None:
    expr = predicates(kind)[predicate]
    chunks = chunk_by_sizes(make_table(kind), CHUNKINGS[chunking])
    native, forced = legs(columns(kind, expr), chunks)
    assert native.error is None
    assert_same(native, forced)
    if predicate == "zero":
        # No selected row: the column is unchanged by the operator.
        got = [v for t in native.out for v in t.column("v").to_pylist()]
        assert got == [v for c in chunks for v in c.column("v").to_pylist()]


@pytest.mark.parametrize("kind", KINDS)
def test_1_empty_table(kind: str) -> None:
    native, forced = legs(columns(kind, "k == 'x'"), [make_table(kind, 0)])
    assert native.error is None
    assert_same(native, forced)


@pytest.mark.parametrize("kind", KINDS)
def test_1_selected_rows_change_and_unselected_rows_do_not(kind: str) -> None:
    table = make_table(kind)
    native, _ = legs(columns(kind, "k == 'x'"), chunk_by_sizes(table, CHUNKINGS["even"]))
    sel = table.column("k").to_pylist()
    src = table.column("v").to_pylist()
    out = [v for t in native.out for v in t.column("v").to_pylist()]
    changed = [i for i in range(N) if out[i] != src[i]]
    assert changed, "some selected row must change, or the case proves nothing"
    assert all(sel[i] == "x" for i in changed)
    assert all(out[i] == src[i] for i in range(N) if sel[i] != "x")


# ---------------------------------------------------------------------------
# 2. date_shift row errors: chunk-local on the chunked route.
# ---------------------------------------------------------------------------


def _ds(when: str | None) -> list[dict[str, Any]]:
    return columns("date_shift", when)


@NEEDS_COMPANION
def test_2_unparseable_selected_rows_give_chunk_local_records() -> None:
    chunks = [make_table("date_shift", 9, 0, bad=(0,)), make_table("date_shift", 9, 9, bad=(4, 6))]
    k = [("x" if i in (0, 6) else "y") for i in range(9)]
    chunks = [c.set_column(1, "k", pa.array(k, pa.string())) for c in chunks]
    native, forced = legs(_ds("k == 'x'"), chunks)
    assert [r.row_index for r in _records(native)] == [0]
    assert_same(native, forced)
    # Second chunk only: positions are local to it (6, not 15).
    later = [make_table("date_shift", 9, 0), chunks[1]]
    native, forced = legs(_ds("k == 'x'"), later)
    assert [r.row_index for r in _records(native)] == [6]
    assert _records(native) == _records(forced)
    assert all(r.reason == FORMAT_ERROR_REASON for r in _records(native))


@NEEDS_COMPANION
def test_2_unparseable_unselected_rows_give_no_errors_and_keep_the_value() -> None:
    table = make_table("date_shift", 12, bad=(1, 5, 7))
    k = ["x" if i not in (1, 5, 7) else "y" for i in range(12)]
    table = table.set_column(1, "k", pa.array(k, pa.string()))
    native, forced = legs(_ds("k == 'x'"), chunk_by_sizes(table, [5, 7]))
    assert native.error is None
    assert_same(native, forced)
    out = [v for t in native.out for v in t.column("v").to_pylist()]
    assert [out[i] for i in (1, 5, 7)] == [BAD_DATE] * 3


@NEEDS_COMPANION
def test_2_a_later_failing_chunk_with_a_base_offset_fails_before_yield() -> None:
    good = make_table("date_shift", 8, 0)
    bad_k = ["y", "y", "x", "y", "y", "y", "y", "y"]
    bad = make_table("date_shift", 8, 8, bad=(2, 3)).set_column(
        1, "k", pa.array(bad_k, pa.string())
    )
    never = make_table("date_shift", 8, 16)
    native, forced = legs(_ds("k == 'x'"), [good, bad, never], base_row_offset=1000)
    assert [r.row_index for r in _records(native)] == [2], "chunk-local, never offset"
    assert_same(native, forced)
    assert len(native.out) == len(forced.out) == 1, "the failing chunk is not yielded"
    assert native.chunks_pulled == forced.chunks_pulled == 2


# ---------------------------------------------------------------------------
# 3. text_redact: detectors, spans, tokens, selected and unselected rows.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config", sorted(ADMITTED_CONFIGS))
def test_3_text_redact_configs(config: str) -> None:
    cols = columns("text_redact", "k == 'x'")
    cols[0]["provider_config"] = dict(ADMITTED_CONFIGS[config])
    table = make_table("text_redact", 32)
    native, forced = legs(cols, chunk_by_sizes(table, [11, 0, 13, 8]))
    assert_same(native, forced)
    sel = table.column("k").to_pylist()
    out = [v for t in native.out for v in t.column("v").to_pylist()]
    src = table.column("v").to_pylist()
    assert all(out[i] == src[i] for i in range(32) if sel[i] != "x")


def test_3_text_redact_default_config_redacts_selected_spans() -> None:
    table = make_table("text_redact", 32)
    native, _ = legs(columns("text_redact", "k == 'x'"), [table])
    sel = table.column("k").to_pylist()
    src = table.column("v").to_pylist()
    out = native.out[0].column("v").to_pylist()
    redacted = [i for i in range(32) if sel[i] == "x" and out[i] != src[i]]
    assert redacted and all("[REDACTED" in out[i] for i in redacted)


# ---------------------------------------------------------------------------
# 4. bucket_perturb: explicit formats, special formats.
# ---------------------------------------------------------------------------

_STRFTIME = {
    "%Y-%m-%d": lambda i: date_value(i),
    "%d/%m/%Y": lambda i: f"{date_value(i)[8:]}/{date_value(i)[5:7]}/{date_value(i)[:4]}",
    "%Y%m%d": lambda i: date_value(i).replace("-", ""),
}


@NEEDS_COMPANION
@pytest.mark.parametrize("bucket", ["week", "month", "quarter"])
@pytest.mark.parametrize("fmt", sorted(_STRFTIME))
def test_4_explicit_formats_are_admitted_and_only_selected_rows_change(
    fmt: str, bucket: str
) -> None:
    vals: list[str | None] = [None if i % 5 == 2 else _STRFTIME[fmt](i) for i in range(23)]
    table = make_table("bucket_perturb", v=vals)
    cols = columns("bucket_perturb", "k == 'x'", bucket=bucket, date_format=fmt)
    native, forced = legs(cols, chunk_by_sizes(table, CHUNKINGS["ragged"]))
    assert_same(native, forced)
    sel = table.column("k").to_pylist()
    out = [v for t in native.out for v in t.column("v").to_pylist()]
    assert all(out[i] == vals[i] for i in range(23) if sel[i] != "x")
    assert any(out[i] != vals[i] for i in range(23) if sel[i] == "x")


@NEEDS_COMPANION
@pytest.mark.parametrize("fmt", SPECIAL_FORMATS)
@pytest.mark.parametrize("split", ["one_chunk", "split_chunks"])
def test_4_special_formats_unmasked_decline_with_the_new_code(fmt: str, split: str) -> None:
    """Mixed offsets: the oracle parses them, native raised on main. Now both take the oracle."""
    vals: list[str | None] = [*MIXED_OFFSET_VALUES, None, "junk"]
    table = make_table("bucket_perturb", v=vals, k=["x"] * 4)
    chunks = [table] if split == "one_chunk" else chunk_by_sizes(table, [1, 1, 2])
    cols = [bp_col("v", date_format=fmt), passthrough("k"), passthrough("p")]
    native = run_outcome(make_config(cols), chunks)
    forced = run_outcome(make_config([*cols, force_oracle(FORCE)]), [with_force(c) for c in chunks])
    assert native.ev[0].native_admitted is False
    assert "bucket_perturb_special_date_format:v" in (native.ev[0].reroute_reason or "")
    assert native.error is None and forced.error is None
    for got, want in zip(native.out, forced.out, strict=True):
        assert identical(got, want.drop_columns([FORCE]))


def _job_table(chunk: list[str | None]) -> pa.Table:
    return pa.table(
        {"d": pa.array(chunk, pa.string()), "p": pa.array(list(range(len(chunk))), pa.int64())}
    )


@NEEDS_COMPANION
@pytest.mark.parametrize("job", sorted(SPECIAL_FORMAT_JOBS))
@pytest.mark.parametrize("fmt", SPECIAL_FORMATS)
def test_4_special_format_jobs_that_succeeded_natively_keep_their_output(
    fmt: str, job: str
) -> None:
    """Main ran these natively. The route changes to the oracle; the output does not."""
    golden = GOLDENS[f"{fmt}/{job}"]["chunked"]
    assert golden["native"] is True, "recorded on main"
    cols = [bp_col("d", date_format=fmt), passthrough("p")]
    outcome = run_outcome(make_config(cols), [_job_table(c) for c in SPECIAL_FORMAT_JOBS[job]])
    assert outcome.error is None
    assert outcome.ev[0].native_admitted is False
    assert "bucket_perturb_special_date_format:d" in (outcome.ev[0].reroute_reason or "")
    got = [
        {"schema": str(t.schema.field("d").type), "d": t.column("d").to_pylist()}
        for t in outcome.out
    ]
    assert got == golden["chunks"]


@NEEDS_COMPANION
@pytest.mark.parametrize("fmt", SPECIAL_FORMATS)
@pytest.mark.parametrize("predicate", ["k == 'zzz'", "k == 'x'", "k != 'zzz'"])
def test_4_special_formats_masked_on_the_explicit_chunked_route_raise_the_gate(
    fmt: str, predicate: str
) -> None:
    """Both legs reach the same oracle gate, before the predicate is evaluated."""
    table = make_table("bucket_perturb", 6, v=[date_value(i) for i in range(6)])
    cols = [{**bp_col("v", date_format=fmt), "when": predicate}, passthrough("k"), passthrough("p")]
    native = run_outcome(make_config(cols), [table])
    forced = run_outcome(make_config([*cols, force_oracle(FORCE)]), [with_force(table)])
    for outcome in (native, forced):
        assert isinstance(outcome.error, PlanCompileError), outcome.error
        assert outcome.error.code == "chunked_bucket_perturb_when_not_supported"
    assert not native.out and not forced.out


# ---------------------------------------------------------------------------
# 5. Degenerate outputs: the EMITTED schema after normalization.
# ---------------------------------------------------------------------------


def _degenerate_cases(kind: str) -> dict[str, list[pa.Table]]:
    nulls = [None] * 4
    return {
        "all_null_source": [make_table(kind, v=nulls, k=["x"] * 4)],
        "empty_table": [make_table(kind, 0)],
        "selected_rows_all_null": [
            make_table(
                kind, v=[None, None, value(kind, 1), value(kind, 3)], k=["x", "x", "y", "y"]
            ),
            make_table(kind, v=[None, None], k=["x", "x"]),
        ],
        "selected_nulls_beside_unselected_values": [
            make_table(kind, v=[None, value(kind, 1), None, value(kind, 3)], k=["x", "y", "x", "y"])
        ],
        "null_then_valued_chunks": [
            make_table(kind, v=nulls, k=["x"] * 4),
            make_table(kind, v=[value(kind, 5), None, value(kind, 6)], k=["x", "y", "x"]),
        ],
    }


@pytest.mark.parametrize("case", sorted(_degenerate_cases("text_redact")))
@pytest.mark.parametrize("kind", KINDS)
def test_5_degenerate_outputs_are_string_pinned_on_both_legs(kind: str, case: str) -> None:
    chunks = _degenerate_cases(kind)[case]
    native, forced = legs(columns(kind, "k == 'x'"), chunks)
    assert native.error is None
    assert_same(native, forced)
    for outcome in (native, forced):
        assert [t.schema.field("v").type for t in outcome.out] == [pa.string()] * len(chunks)


# ---------------------------------------------------------------------------
# 6. Declines unchanged: the entry's outcome is the public chunked oracle's.
# ---------------------------------------------------------------------------


def _entry_vs_oracle(cols: list[dict[str, Any]], table: pa.Table) -> list[Any]:
    from decoy_engine import run_mask_pipeline_chunked
    from tests.native._chunked_entry_support import ENGINE_VERSION, key_provider

    config = make_config(cols)
    chunks = chunk_by_sizes(table, [8, 8, 7])
    evidence: list[Any] = []

    def call(entry: Any, **kw: Any) -> tuple[str, Any]:
        try:
            out = entry(
                config,
                list(chunks),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                **kw,
            )
            return ("ok", [t.to_pydict() for t in out])
        except Exception as exc:
            return ("error", (type(exc).__name__, getattr(exc, "code", None)))

    assert call(run_mask_chunked, route_evidence_sink=evidence) == call(run_mask_pipeline_chunked)
    return evidence


@NEEDS_COMPANION
@pytest.mark.parametrize("name", sorted(declines()))
def test_6_declines_are_unchanged(name: str) -> None:
    cols, kind = declines()[name]
    cols = [{**cols[0], "when": "k == 'x'"}, *cols[1:]]
    evidence = _entry_vs_oracle(cols, make_table(kind))
    if evidence:
        assert evidence[0].native_admitted is False, name
