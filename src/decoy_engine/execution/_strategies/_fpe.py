"""fpe strategy (engine-v2 S9, re-keyed WS1): format-preserving encryption.

The Feistel+HMAC permutation is REUSED from V1 `transforms/fpe.FPEStrategy`
(stdlib hmac, no PyCA -- per the module's design comment and Session 18 B1).

Keying (WS1 detokenization, 2026-06-12, SEED_PROTOCOL_VERSION 4 -> 5): ONE
Feistel key per (job_seed, namespace), `derive(job_seed, namespace,
FPE_KEY_LABEL)`, with the column name as the per-column tweak. This is the
NIST SP 800-38G FF1 key model (single key, varying tweak); it keeps the
S9 contracts (same value -> same ciphertext within a namespace, byte-stable
across runs, cross-column linkage broken by the tweak) AND makes ciphertext
decryptable by any holder of (job_seed, namespace, column, charset) via
`decoy_engine.unmask`. The pre-WS1 keying derived a key from the PLAINTEXT
(`derive(seed, ns, _canonicalize_source(value))`), which made ciphertext-only
reversal impossible and incidentally paid one HKDF per cell.

Per-row parallelism (S9 spec §5.2): rows are split into `chunk_count` chunks
processed in worker threads, then concatenated. Each value's encryption is
independent + deterministic under the shared key, so chunked and serial
output are byte-identical by construction -- the non-negotiable parity gate.
The lift is wall-clock, not output.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

# The constant derive() source for the per-(job_seed, namespace) Feistel key.
# Shared with decoy_engine.unmask; changing it is a SEED_PROTOCOL_VERSION bump.
FPE_KEY_LABEL: bytes = b"fpe-key/v1"


class FpeStrategyHandler:
    """Format-preserving encryption via the V1 Feistel cipher, re-keyed onto derive."""

    name: str = "fpe"

    def __init__(self, *, chunk_count: int = 4) -> None:
        self._chunk_count = chunk_count

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code="fpe_requires_namespace",
                strategy="fpe",
                message=f"column {column!r} uses fpe but has no namespace.",
            )
        cfg = provider_config_to_dict(plan.provider_config)
        charset_spec = cfg.get("charset", "digits")
        charset = "".join(dict.fromkeys(_CHARSETS.get(charset_spec, charset_spec)))
        if len(charset) < 2:
            return df, []  # degenerate charset -> passthrough (V1 behavior)
        preserve_sep = bool(cfg.get("preserve_separators", True))
        validate_luhn = bool(cfg.get("validate_luhn", False)) and all(
            c in "0123456789" for c in charset
        )
        tweak = column.encode("utf-8", errors="replace")
        namespace = plan.namespace

        # One key per (job_seed, namespace) -- derived once, not per cell.
        key = derive(ctx.job_seed, namespace, FPE_KEY_LABEL)

        def encrypt_one(value: str) -> str:
            return fpe_encrypt_value(value, key, charset, tweak, preserve_sep, validate_luhn)

        source = df[column]
        na_mask = source.isna().to_numpy()
        non_na_positions = np.where(~na_mask)[0]
        # Vectorized non-null materialization: numpy boolean-select (C-level) then
        # str() each, NOT a per-row pandas `.iloc[int(i)]` scalar-access loop (that
        # paid O(n) pandas-indexing overhead V1's C-level astype never did; Dennis
        # S13 FPE-port finding). str() semantics + order are preserved exactly.
        non_na_values = [str(v) for v in source.to_numpy(dtype=object)[~na_mask]]
        encrypted = self._encrypt_values(non_na_values, encrypt_one)

        out: list[object] = [None] * len(source)
        for offset, position in enumerate(non_na_positions):
            out[int(position)] = encrypted[offset]
        df[column] = out
        return df, []

    def _encrypt_values(self, values: list[str], encrypt_one: Callable[[str], str]) -> list[str]:
        # Cap workers at the actual CPU count: the Feistel orchestration is
        # GIL-bound pure Python (only the stdlib-HMAC digest releases the GIL), so
        # spawning more threads than cores adds contention + overhead without
        # parallelism (net-negative on a 2-vCPU CI runner). Output is identical for
        # any worker count (each value's encryption is independent + deterministic),
        # so this is wall-clock only -- the byte-identical parity gate is unaffected.
        workers = min(self._chunk_count, os.cpu_count() or 1)
        if workers <= 1 or len(values) < workers:
            return [encrypt_one(v) for v in values]
        chunks = [list(chunk) for chunk in np.array_split(np.array(values, dtype=object), workers)]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            chunk_results = list(
                executor.map(lambda chunk: [encrypt_one(v) for v in chunk], chunks)
            )
        return [value for chunk_result in chunk_results for value in chunk_result]
