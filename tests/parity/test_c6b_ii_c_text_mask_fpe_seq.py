"""C6b-ii slice c: native FF1 spans on the SEQ route and the chunked route.

Both routes call `native_text_mask`, so these cases pin that the compiled FF1 really ran there
(not just that output matched) and that output, warnings and fail-closed behavior equal the
full-frame oracle.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.execution.native import _text_mask_native_fpe as nfpe
from tests.native._c6b_i_support import CORPUS, SUB_FLOOR_TEXTS
from tests.native._c6c_ii_support import NEEDS_TR_KERNEL
from tests.parity._text_redact_seq_data import assert_trio_strict, run_trio
from tests.parity.test_c6b_ii_text_mask_seq import _assert_native, _shape

pytestmark = NEEDS_TR_KERNEL

PHONES = ["call 415-555-0132 now", "ssn 123-45-6789", "zip 94105-1234", "clean", None]


def _spy_kernel(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    nfpe.load_fpe_batch.cache_clear()
    real = nfpe.load_fpe_batch()
    assert real is not None
    rows: list[int] = []

    def spy(values: Any, **kw: Any) -> Any:
        rows.append(len(values))
        return real(values, **kw)

    monkeypatch.setattr(nfpe, "load_fpe_batch", lambda: spy)
    return rows


def test_seq_route_runs_native_ff1_and_matches_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _spy_kernel(monkeypatch)
    trio = run_trio(_shape(PHONES), monkeypatch)
    assert_trio_strict(trio)
    _assert_native(trio.native)
    assert sum(rows) > 0  # the SEQ op reached the compiled FF1


@pytest.mark.parametrize("policy", ["redact", "synthetic"])
def test_seq_route_sub_floor_hybrid_matches_oracle_warnings(
    policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _spy_kernel(monkeypatch)
    trio = run_trio(
        _shape([*PHONES, *SUB_FLOOR_TEXTS, *CORPUS], sub_floor_span=policy), monkeypatch
    )
    assert_trio_strict(trio)  # warnings (declined ZIP5 counts) equal the oracle field for field
    assert any(w.code == "text_mask_sub_floor_span_handled" for w in trio.oracle.warnings)
    assert sum(rows) > 0


@pytest.mark.parametrize("size", [2, 50_000])
def test_chunked_route_runs_native_ff1_and_matches_the_oracle(
    size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow as pa

    from tests.native._b8_support import assert_same_as_oracle, run_pair
    from tests.native._c6b_i_support import tm_col
    from tests.native._chunked_entry_support import passthrough, split

    rows = _spy_kernel(monkeypatch)
    values = [*PHONES, *SUB_FLOOR_TEXTS]
    table = pa.table(
        {"s": pa.array(values, pa.string()), "p": pa.array(range(len(values)), pa.int64())}
    )
    native, forced = run_pair(
        [tm_col(sub_floor_span="synthetic"), passthrough("p")], split(table, size)
    )
    assert_same_as_oracle(native, forced)
    assert sum(rows) > 0
