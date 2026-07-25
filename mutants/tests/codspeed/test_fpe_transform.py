"""CodSpeed: format-preserving encryption, the per-cell hot path of the
``fpe`` masking strategy (``decoy_engine.execution._strategies._fpe``).

``fpe_encrypt_value`` (``decoy_engine.transforms.fpe``) is the primitive the
strategy calls once per cell -- an 8-round HMAC-SHA256 Feistel permutation
(see that module's docstring for the "why not FF1" rationale). It is CPU-bound
and allocation-heavy per call (one HMAC per round), so it is the clearest
representative hot path for a masking transform: real column-shaped input,
no I/O, no pandas overhead diluting the measurement.

2,000 values keeps a `--codspeed` run in the sub-second range while still
exercising enough iterations for the instrumented backend to get a stable
call count.
"""

from __future__ import annotations

import pytest

from decoy_engine.determinism import derive
from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

pytestmark = pytest.mark.codspeed

_ROW_COUNT = 2_000
# derive() requires an 8-byte (job_seed) or 32-byte (mask_key) IKM.
_KEY = derive(b"cdsp-bch", "fpe_codspeed_bench", b"fpe-key/v1")
_TWEAK = b"acct"
_CHARSET = _CHARSETS["digits"]
_VALUES = [f"{100_000_000 + i:09d}" for i in range(_ROW_COUNT)]


def test_fpe_encrypt_value_batch(benchmark) -> None:
    def _run() -> list[str]:
        return [
            fpe_encrypt_value(val, _KEY, _CHARSET, _TWEAK, preserve_separators=True)
            for val in _VALUES
        ]

    result = benchmark(_run)

    assert len(result) == _ROW_COUNT
    # Format-preserving: same digit-only shape and length as the source.
    assert all(
        len(out) == len(src) and out.isdigit() for out, src in zip(result, _VALUES, strict=True)
    )
    # Permutation, not passthrough.
    assert result != _VALUES
