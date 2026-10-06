"""C8-i acceptance test 6: every admission rule has an exact decline, and a decline is today's run.

A declined `when:` column takes the existing downgrade to the chunked oracle leg with its
code, never a hard error, and produces what the oracle produces today. The write-set rule
is checked end to end with ordinary scalar writers and, for composite and unknown writers
(which are rejected earlier end to end), directly on the classifier.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._planner import _whole_column_state_rejections
from decoy_engine.execution.native._when_admission import (
    admitted_when_columns,
    first_when_rejection,
    when_native_rejection,
)
from decoy_engine.providers_v2 import get_default_registry
from tests.native._c8_i_support import (
    chunk_by_sizes,
    run_native,
    run_public_oracle,
    source_table,
)
from tests.native._chunked_entry_support import (
    TABLE,
    make_config,
    passthrough,
    redact,
    truncate,
)

REG = get_default_registry()
_SCHEMA = pa.schema([("s", pa.string()), ("p", pa.string()), ("u", pa.string()), ("n", pa.int64())])


def _when(col: dict[str, Any], expr: str) -> dict[str, Any]:
    return {**col, "when": expr}


def _outcome(call: Any) -> Any:
    try:
        return ("ok", [t.to_pydict() for t in call()])
    except Exception as exc:
        return ("error", type(exc).__name__, getattr(exc, "code", None))


def _declined_equals_today(config: dict[str, Any], code: str) -> None:
    chunks = chunk_by_sizes(source_table(), [4, 4, 3])
    got: list[pa.Table] = []
    evidence: Any = None

    def run_entry() -> list[pa.Table]:
        nonlocal evidence
        out, evidence = run_native(config, chunks)
        got.extend(out)
        return out

    entry = _outcome(run_entry)
    assert evidence is not None and evidence.native_admitted is False
    assert evidence.reroute_reason is not None and evidence.reroute_reason.startswith(code), (
        evidence.reroute_reason
    )
    today = _outcome(lambda: run_public_oracle(config, chunks))
    assert entry[0] == today[0]
    if entry[0] == "ok":
        # The entry pins output types the public oracle infers; compare the values.
        for g, w in zip(got, run_public_oracle(config, chunks), strict=True):
            for name in g.schema.names:
                assert g.column(name).to_pylist() == w.column(name).to_pylist()
    else:
        assert entry == today


# ---------------------------------------------------------------------------
# Rule 1: the strategy and its own config.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        passthrough("s"),
        {"name": "s", "strategy": "text_redact"},
        redact("s", redact_with=7),
        {"name": "s", "strategy": "truncate", "provider_config": {"length": 0}},
        {
            "name": "s",
            "strategy": "categorical",
            "namespace": "ns",
            "deterministic": True,
            "provider_config": {"categories": [1, 2]},
        },
    ],
    ids=[
        "passthrough",
        "text_redact",
        "redact_non_string",
        "truncate_bad_length",
        "categorical_numeric",
    ],
)
def test_a_non_admitted_strategy_or_config_declines_with_the_legacy_code(
    column: dict[str, Any],
) -> None:
    config = make_config([_when(column, "p == 'x'"), passthrough("p")])
    entries = config["tables"][0]["columns"]
    assert when_native_rejection("s", entries, REG, table=TABLE, schema=_SCHEMA) == (
        "when_predicate_not_native:s"
    )
    if column["strategy"] in ("passthrough", "text_redact"):
        _declined_equals_today(config, "when_predicate_not_native:s")


# ---------------------------------------------------------------------------
# Rule 2: the target source type is string.
# ---------------------------------------------------------------------------


def test_a_non_string_target_declines_with_the_legacy_code() -> None:
    config = make_config([_when(truncate("n"), "p == 'x'"), passthrough("p")])
    entries = config["tables"][0]["columns"]
    assert when_native_rejection("n", entries, REG, table=TABLE, schema=_SCHEMA) == (
        "when_predicate_not_native:n"
    )
    # The same column over a string source is admitted: the type is what declines.
    assert (
        when_native_rejection(
            "n",
            entries,
            REG,
            table=TABLE,
            schema=pa.schema([("n", pa.string()), ("p", pa.string())]),
        )
        is None
    )
    # An unknown schema declines too.
    assert when_native_rejection("n", entries, REG, table=TABLE, schema=None) == (
        "when_predicate_not_native:n"
    )
    outcome = _outcome(lambda: run_native(config, chunk_by_sizes(source_table(), [11]))[0])
    assert outcome == _outcome(
        lambda: run_public_oracle(config, chunk_by_sizes(source_table(), [11]))
    )


def test_a_large_string_target_declines() -> None:
    config = make_config([_when(redact("s"), "p == 'x'"), passthrough("p")])
    entries = config["tables"][0]["columns"]
    schema = pa.schema([("s", pa.large_string()), ("p", pa.string())])
    assert when_native_rejection("s", entries, REG, table=TABLE, schema=schema) == (
        "when_predicate_not_native:s"
    )


# ---------------------------------------------------------------------------
# Rule 3: the predicate parses under the closed grammar.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expr", ["p.notnull()", "p == u", "len(p) == 1", "p + 'a' == 'xa'"])
def test_a_raw_predicate_outside_the_grammar_declines_and_equals_today(expr: str) -> None:
    config = make_config([_when(redact("s"), expr), passthrough("p")])
    entries = config["tables"][0]["columns"]
    assert when_native_rejection("s", entries, REG, table=TABLE, schema=_SCHEMA) == (
        "when_predicate_outside_native_subset:s"
    )
    _declined_equals_today(config, "when_predicate_outside_native_subset:s")


# ---------------------------------------------------------------------------
# Rule 4: a reference to a column an earlier node writes.
# ---------------------------------------------------------------------------


def test_a_reference_to_a_column_an_earlier_node_masks_declines_and_equals_today() -> None:
    """Codex round 2: `a` redacts to 'REDACTED' before `z`, whose predicate reads `a`, and the
    config order is the reverse of the execution order. The oracle sees the masked `a`."""
    config = make_config([_when(redact("z"), "a == 'REDACTED'"), redact("a"), passthrough("p")])
    entries = config["tables"][0]["columns"]
    assert [e["name"] for e in entries][:2] == ["z", "a"]
    assert when_native_rejection("z", entries, REG, table=TABLE, schema=_schema_az()) == (
        "when_predicate_reads_masked_column:z:a"
    )
    chunks = [
        pa.table({"a": ["k1", "k2", None], "z": ["v1", "v2", "v3"], "p": ["x", "y", "z"]}),
        pa.table({"a": ["k3", None], "z": ["v4", "v5"], "p": ["x", "y"]}),
    ]
    out, evidence = run_native(config, chunks)
    assert evidence.native_admitted is False
    assert (evidence.reroute_reason or "").startswith("when_predicate_reads_masked_column:z:a")
    # The oracle's predicate saw the masked values, so z is redacted on every row with an `a`
    # that was masked (a null `a` stays null, which does not equal 'REDACTED').
    assert out[0].column("z").to_pylist() == ["REDACTED", "REDACTED", "v3"]
    assert [t.to_pydict() for t in out] == [
        {k: v for k, v in t.to_pydict().items()} for t in run_public_oracle(config, chunks)
    ]


def test_a_reference_to_a_column_a_later_node_masks_is_admitted() -> None:
    """The same shape with the order the other way: `a` runs first and reads `z`, which the
    oracle has not masked yet, so the native mask over the source chunk is exact."""
    config = make_config([_when(redact("a"), "z == 'v1'"), redact("z"), passthrough("p")])
    entries = config["tables"][0]["columns"]
    assert when_native_rejection("a", entries, REG, table=TABLE, schema=_schema_az()) is None
    chunks = [pa.table({"a": ["k1", "k2"], "z": ["v1", "v2"], "p": ["x", "y"]})]
    out, evidence = run_native(config, chunks)
    assert evidence.native_admitted is True, evidence.reroute_reason
    assert out[0].column("a").to_pylist() == ["REDACTED", "k2"]


def test_a_reference_to_a_passthrough_or_unconfigured_column_is_admitted() -> None:
    config = make_config([_when(redact("s"), "p == 'x' and u == 'q'"), passthrough("p")])
    entries = config["tables"][0]["columns"]
    assert when_native_rejection("s", entries, REG, table=TABLE, schema=_SCHEMA) is None


def test_a_passthrough_node_is_not_a_write() -> None:
    config = make_config([passthrough("a"), _when(redact("z"), "a == 'k1'")])
    entries = config["tables"][0]["columns"]
    assert when_native_rejection("z", entries, REG, table=TABLE, schema=_schema_az()) is None


def _schema_az() -> pa.Schema:
    return pa.schema([("a", pa.string()), ("z", pa.string()), ("p", pa.string())])


def test_a_composite_writers_extra_column_counts_as_written() -> None:
    """Composites are rejected end to end by the chunked compatibility check, so the write-set
    classifier is exercised directly: a composite earlier in the order writes its bundle."""
    from decoy_engine.generation.composite._validate import _COMPOSITE_OUTPUT_COLUMNS

    outputs = _COMPOSITE_OUTPUT_COLUMNS["composite_name_email"]
    first = outputs[0]
    entries = [
        {
            "name": "a_head",
            "strategy": "redact",
            "provider": "composite_name_email",
            "coherent_with": list(outputs[1:]),
        },
        _when(redact("z_target"), f"{outputs[-1]} == 'x'"),
    ]
    schema = pa.schema([(c, pa.string()) for c in ("a_head", "z_target", *outputs)])
    assert (
        first
        and when_native_rejection("z_target", entries, REG, table=TABLE, schema=schema)
        == f"when_predicate_reads_masked_column:z_target:{outputs[-1]}"
    )


def test_a_node_with_unknown_writes_declines_any_when_after_it() -> None:
    entries = [
        {"name": "a", "strategy": "no_such_strategy_with_unknown_writes"},
        _when(redact("z"), "p == 'x'"),
    ]
    schema = pa.schema([("a", pa.string()), ("z", pa.string()), ("p", pa.string())])
    assert when_native_rejection("z", entries, REG, table=TABLE, schema=schema) == (
        "when_predicate_reads_masked_column:z:p"
    )
    # An unknown writer that runs AFTER the target does not matter.
    after = [_when(redact("a"), "p == 'x'"), {"name": "z", "strategy": "no_such_strategy"}]
    assert when_native_rejection("a", after, REG, table=TABLE, schema=schema) is None


# ---------------------------------------------------------------------------
# Admission of numeric references on the explicit chunked route.
# ---------------------------------------------------------------------------


def test_a_numeric_reference_is_admitted_and_equals_the_chunked_oracle() -> None:
    config = make_config([_when(redact("s"), "n > 3"), passthrough("p")])
    chunks = chunk_by_sizes(source_table(), [4, 4, 3])
    out, evidence = run_native(config, chunks)
    assert evidence.native_admitted is True, evidence.reroute_reason
    want = run_public_oracle(config, chunks)
    assert [t.column("s").to_pylist() for t in out] == [t.column("s").to_pylist() for t in want]


# ---------------------------------------------------------------------------
# Helpers over a whole table.
# ---------------------------------------------------------------------------


def test_the_table_level_helpers_report_the_first_decline_and_the_admitted_set() -> None:
    config = make_config(
        [
            _when(redact("s"), "p == 'x'"),
            _when(truncate("u"), "p.notnull()"),
            _when(passthrough("p"), "s == 'a'"),
        ]
    )
    assert first_when_rejection(config, REG, table=TABLE, schema=_SCHEMA) == (
        "when_predicate_outside_native_subset:u"
    )
    assert admitted_when_columns(config, REG, table=TABLE, schema=_SCHEMA) == frozenset({"s"})
    clean = make_config([_when(redact("s"), "p == 'x'")])
    assert first_when_rejection(clean, REG, table=TABLE, schema=_SCHEMA) is None
    assert (
        first_when_rejection(make_config([redact("s")]), REG, table=TABLE, schema=_SCHEMA) is None
    )


def test_the_planner_gate_still_names_every_declined_when_column() -> None:
    config = make_config([_when(passthrough("s"), "p == 'x'"), passthrough("p")])
    joined = "; ".join(_whole_column_state_rejections(config, table=TABLE))
    assert "when_predicate_not_chunk_stable" in joined
