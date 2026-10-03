"""Direct unit tests for B6b helper functions whose branches the acceptance suite
only exercised indirectly. Each test pins one invariant that the 2026-10-03
hand-mutation substitute found unprotected (a surviving mutant). These are
strengthening tests, not acceptance tests: they close coverage gaps, not behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.execution import _chunked_output_sink as sink
from decoy_engine.execution._chunked_input import (
    InputChunks,
    SourceFacts,
    facts_match,
    lazy_stream_candidates,
)

_SCHEMA = pa.schema([("x", pa.int64())])


def test_a_zero_row_lazy_table_is_not_a_stream_candidate() -> None:
    # The lane cannot stream an empty source; a zero-row mask table must resolve
    # resident. Pins the `num_rows > 0` filter against a `>= 0` mutant.
    facts = {
        "empty": SourceFacts(num_rows=0, schema=_SCHEMA),
        "full": SourceFacts(num_rows=5, schema=_SCHEMA),
    }
    kept = lazy_stream_candidates(
        facts,
        table_kinds={"empty": "mask", "full": "mask"},
        route_chunked=True,
        bearing=frozenset(),
    )
    assert "empty" not in kept
    assert "full" in kept


def test_facts_match_is_sensitive_to_row_group_layout() -> None:
    # The routing snapshot includes the row-group count; a source whose layout
    # changed between capture and open is not the one routing decided on, even if
    # its row count and max row-group size are unchanged. Pins the row_groups term.
    a = SourceFacts(num_rows=10, schema=_SCHEMA, row_groups=1, max_row_group_rows=10)
    b = SourceFacts(num_rows=10, schema=_SCHEMA, row_groups=2, max_row_group_rows=10)
    assert facts_match(a, a)
    assert not facts_match(a, b)


def test_input_chunks_close_releases_the_owner_without_iterating() -> None:
    # Closing a routed input that was never iterated must still release the source
    # handle; `_guarded` only closes it on exhaustion, error or generator close.
    # Pins the `self._owner.close()` call against a `pass` mutant.
    class _Owner:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    owner = _Owner()
    chunks = InputChunks(pa.table({"x": [1]}), iter(()), {}, owner)  # type: ignore[arg-type]
    chunks.close()
    assert owner.closed is True


def test_fold_timings_keeps_the_peak_memory_max_not_the_last() -> None:
    # The memory evidence that backs B6b's bounded-memory claim is the peak across
    # chunks, so a later smaller chunk must not lower the recorded peak. Pins the
    # `max(...)` fold against a last-value mutant by folding decreasing deltas.
    def _result(peak_kb: int) -> SimpleNamespace:
        rec = SimpleNamespace(
            strategy_type="hash", column="c", elapsed_ms=1.0, peak_memory_delta_kb=peak_kb
        )
        return SimpleNamespace(timings=[rec])

    elapsed: dict[tuple[str, str], float] = {}
    peak: dict[tuple[str, str], int] = {}
    sink.fold_timings(elapsed, peak, _result(100))
    sink.fold_timings(elapsed, peak, _result(40))
    assert peak[("hash", "c")] == 100


def test_fold_corpora_is_first_wins_per_table_column() -> None:
    # `masked_any` corpora are deterministic: the first record per (table, column)
    # is kept regardless of later chunks, so output does not depend on chunk order.
    # Pins the `setdefault` (first-wins) against a last-wins mutant.
    def _result(tag: str) -> SimpleNamespace:
        return SimpleNamespace(
            quality_metrics={"code_set_corpora": [{"table": "t", "column": "c", "tag": tag}]}
        )

    seen: dict[tuple[object, object], dict[str, object]] = {}
    sink.fold_corpora(seen, _result("first"))
    sink.fold_corpora(seen, _result("second"))
    assert seen[("t", "c")]["tag"] == "first"
