"""Phase 1 admitted-set diagnostics-free guarantee + base_row_offset plumbing.

The engine-native efficiency program's Phase 1 streams a table only when
every column's strategy is in the zero-error/zero-warning admitted set
{hash, redact, truncate, passthrough}; the platform-side eligibility
predicate (a separate task) enforces admission. This module proves the
engine-side half of that contract: a job built only from the admitted set
never emits a warning or a row error on any chunk, so Phase 1 needs no
warning globalizer and no quarantine machinery. It also proves
`base_row_offset` is a pure foundation counter for a later phase (it
changes no output and does not touch the fail-closed row-error path).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _chunked, run_pipeline
from tests.unit.execution.test_chunked import _chunks, _config

_ENGINE_VERSION = "phase1-admitted-set-test"

# Exactly the Phase 1 admitted set; no other strategy belongs in this file.
_ADMITTED_COLUMNS = [
    {"name": "ssn", "strategy": "hash", "namespace": "ssn_ns"},
    {"name": "secret", "strategy": "redact"},
    {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
    {"name": "region", "strategy": "passthrough"},
]


def _admitted_frame(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ssn": [f"{i:09d}" for i in range(n)],
            "secret": [f"secret-{i}" for i in range(n)],
            "zip": [f"{10000 + i:05d}" for i in range(n)],
            "region": [f"region-{i % 5}" for i in range(n)],
        }
    )


class TestAdmittedSetDiagnosticsFree:
    def test_zero_warnings_and_zero_row_errors_across_chunks(self, tmp_path) -> None:
        """The admitted set is exactly the strategies with empty
        warning_codes and empty row_error_modes, so a job built only from
        it must clear every chunk with no diagnostics, by construction."""
        df = _admitted_frame(9)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _ADMITTED_COLUMNS)
        sink: list[Any] = []

        out = list(
            run_mask_pipeline_chunked(
                cfg,
                _chunks(df, 4),
                table="accounts",
                engine_version=_ENGINE_VERSION,
                chunk_result_sink=sink,
            )
        )

        assert len(out) == 3  # chunk sizes 4, 4, 1
        assert len(sink) == 3
        for result in sink:
            assert result.warnings == ()
            assert result.row_errors == ()


class TestBaseRowOffsetCounter:
    def test_offset_advances_by_chunk_num_rows(self, tmp_path, monkeypatch) -> None:
        """Instruments the counter step `_masked()` uses internally: the
        offset is never exposed through the yielded output (that is the
        inertness contract), so this spies on the pure advance function to
        prove base_row_offset + cumulative rows is what actually
        accumulates chunk by chunk."""
        df = _admitted_frame(5)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _ADMITTED_COLUMNS)
        chunks = _chunks(df, 2)  # row counts: 2, 2, 1

        seen_offsets: list[int] = []
        real_advance = _chunked._advance_row_offset

        def _spy(offset: int, chunk: pa.Table) -> int:
            result = real_advance(offset, chunk)
            seen_offsets.append(result)
            return result

        monkeypatch.setattr(_chunked, "_advance_row_offset", _spy)

        out = list(
            run_mask_pipeline_chunked(
                cfg,
                chunks,
                table="accounts",
                engine_version=_ENGINE_VERSION,
                base_row_offset=100,
            )
        )

        assert len(out) == 3
        assert seen_offsets == [102, 104, 105]


class TestBaseRowOffsetInert:
    def test_admitted_set_parity_with_and_without_offset(self, tmp_path) -> None:
        """base_row_offset changes no output: chunked runs at offset 0 and
        at a large nonzero offset both match the full-frame run exactly."""
        df = _admitted_frame(20)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _ADMITTED_COLUMNS)

        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]

        zero_offset = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, 6),
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                    base_row_offset=0,
                )
            )
        )
        nonzero_offset = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    _chunks(df, 6),
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                    base_row_offset=1_000_000,
                )
            )
        )

        assert zero_offset.to_pylist() == full.to_pylist()
        assert nonzero_offset.to_pylist() == full.to_pylist()


class TestFailClosedUnchangedWithOffset:
    def test_row_error_still_raises_with_base_row_offset_present(
        self, tmp_path, monkeypatch
    ) -> None:
        """Reuses the shared config builder from the parity suite rather
        than duplicating a fail-closed fixture; base_row_offset must not
        touch the row-error path (defense in depth against a future
        globalizer swallowing it). Spies on the advance step to pin the
        raise-before-advance ordering: a rejected chunk must raise before
        the counter moves, so a later globalizer never attributes positions
        to a chunk that failed closed."""
        cfg = _config(
            tmp_path,
            [{"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}}],
        )
        chunk = pa.table({"age": pa.array(["23", "not-a-number"], type=pa.string())})

        advance_calls: list[int] = []
        real_advance = _chunked._advance_row_offset

        def _spy(offset: int, chunk: pa.Table) -> int:
            advance_calls.append(offset)
            return real_advance(offset, chunk)

        monkeypatch.setattr(_chunked, "_advance_row_offset", _spy)

        with pytest.raises(RowErrorsFailedError) as exc_info:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    [chunk],
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                    base_row_offset=50,
                )
            )
        assert exc_info.value.records[0].trigger == "format_error"
        # The rejected chunk never advances the counter: the row-error gate
        # raises before _advance_row_offset runs. If that order ever flips,
        # advance_calls becomes [50] and this test fails.
        assert advance_calls == []
