"""Stored pandas index fields a config references are not source columns: the declaration gate
refuses a configured stored-index name before any chunk, and a plain-to-indexed change between
chunks fails as a missing declared column. Moved out of test_composite_admission.py, which is over
the test-file cap, with the expectations the declared-column refusal changed."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from tests._declared_schema import chunked_entry_kwargs
from tests.native._b8_support import (
    FORCE,
    run_mask_chunked_below_gate,
    run_mask_pipeline_chunked_below_gate,
    with_force,
)
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    force_oracle,
    key_provider,
    make_config,
    redact,
    truncate,
)
from tests.native.test_composite_admission import (
    _WARN,
    _indexed,
    _outcome,
    _stored_index_configs,
)

MISSING = "source_file.declared_column_missing"
DRIFT = {"index_then_plain": "native_chunk_schema_drift", "plain_then_index": MISSING}


@pytest.mark.parametrize("route", ["native_capable", "forced_oracle", "public_oracle"])
@pytest.mark.parametrize("case", sorted(_stored_index_configs()))
def test_a_configured_stored_index_column_is_refused_before_any_chunk(
    case: str, route: str
) -> None:
    columns = _stored_index_configs()[case]
    chunks = _indexed()
    if case == "group_by_reference":
        # `k` must exist as a source field for the plan to compile.
        chunks = [c.append_column("k", pa.array(["g"] * c.num_rows)) for c in chunks]
    if route == "forced_oracle":
        columns = [*columns, force_oracle(FORCE)]
        chunks = [with_force(c) for c in chunks]
    config = make_config(columns, global_settings=_WARN)
    entry_point = (
        run_mask_pipeline_chunked_below_gate
        if route == "public_oracle"
        else run_mask_chunked_below_gate
    )
    sink: list[Any] = []
    stored = case != "when_reference"  # a name that is only the stored index is not a column
    with pytest.raises(Exception) as info:
        list(
            entry_point(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
                chunk_result_sink=sink,
                **chunked_entry_kwargs(entry_point, config, TABLE),
            )
        )
    assert info.value.code == (MISSING if stored else "config_references_stored_index")
    assert not stored or "column id" in str(info.value)
    assert sink == []


@pytest.mark.parametrize("direction", ["index_then_plain", "plain_then_index"])
def test_a_stored_index_change_between_chunks_is_schema_drift_on_both_routes(
    direction: str,
) -> None:
    indexed = _indexed()
    plain = [c.replace_schema_metadata(None) for c in indexed]
    chunks = [indexed[0], plain[1]] if direction == "index_then_plain" else [plain[0], indexed[1]]
    config = make_config([redact("s"), truncate("t")], global_settings=_WARN)
    forced = make_config([redact("s"), truncate("t"), force_oracle(FORCE)], global_settings=_WARN)
    native = _outcome(config, chunks)
    oracle = _outcome(forced, [with_force(c) for c in chunks])
    assert native == oracle == (DRIFT[direction], 1, 1)


@pytest.mark.parametrize("direction", ["index_then_plain", "plain_then_index"])
@pytest.mark.parametrize("entry_name", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_stored_index_drift_is_raised_by_both_public_entries(
    entry_name: str, direction: str
) -> None:
    entry_point = (
        run_mask_chunked_below_gate
        if entry_name == "run_mask_chunked"
        else run_mask_pipeline_chunked_below_gate
    )
    indexed = _indexed()
    plain = [c.replace_schema_metadata(None) for c in indexed]
    chunks = [indexed[0], plain[1]] if direction == "index_then_plain" else [plain[0], indexed[1]]
    sink: list[Any] = []
    config = make_config([redact("s"), truncate("t")], global_settings=_WARN)
    gen = entry_point(
        config,
        iter(chunks),
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        chunk_result_sink=sink,
        **chunked_entry_kwargs(entry_point, config, TABLE),
    )
    next(gen)
    with pytest.raises(Exception) as info:
        next(gen)
    assert getattr(info.value, "code", None) == DRIFT[direction]
    assert len(sink) == 1
