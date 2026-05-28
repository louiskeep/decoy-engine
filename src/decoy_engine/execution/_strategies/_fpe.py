"""fpe strategy (engine-v2 S9): format-preserving encryption, re-keyed + chunked.

The Feistel+HMAC permutation is REUSED from V1 `transforms/fpe.FPEStrategy`
(stdlib hmac, no PyCA -- per the module's design comment and Session 18 B1).
The only S9 change is the keying: instead of the legacy column_key/seed:int, the
per-value Feistel key is `derive(job_seed, namespace, _canonicalize_source(value))`
(S9 spec §5.2 + §8 path #1). Same value -> same key -> same ciphertext within a
namespace (joinability + format preserved), byte-stable across runs.

Per-row parallelism (S9 spec §5.2): rows are split into `chunk_count` chunks
processed in worker threads, then concatenated. Each value's encryption is
independent + deterministic (its key derives from the value), so chunked and
serial output are byte-identical by construction -- the non-negotiable parity
gate. The lift is wall-clock, not output.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.fpe import _CHARSETS, FPEStrategy

_V1_FPE = FPEStrategy()


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

        def encrypt_one(value: str) -> str:
            key = derive(ctx.job_seed, namespace, _canonicalize_source(value))
            # White-box reuse of the V1 Feistel orchestration with the derived key.
            return _V1_FPE._encrypt(value, key, charset, tweak, preserve_sep, validate_luhn, column)

        source = df[column]
        na_mask = source.isna().to_numpy()
        non_na_positions = np.where(~na_mask)[0]
        non_na_values = [str(source.iloc[int(i)]) for i in non_na_positions]
        encrypted = self._encrypt_values(non_na_values, encrypt_one)

        out: list[object] = [None] * len(source)
        for offset, position in enumerate(non_na_positions):
            out[int(position)] = encrypted[offset]
        df[column] = out
        return df, []

    def _encrypt_values(self, values: list[str], encrypt_one: Callable[[str], str]) -> list[str]:
        if self._chunk_count <= 1 or len(values) < self._chunk_count:
            return [encrypt_one(v) for v in values]
        chunks = [
            list(chunk)
            for chunk in np.array_split(np.array(values, dtype=object), self._chunk_count)
        ]
        with ThreadPoolExecutor(max_workers=self._chunk_count) as executor:
            chunk_results = list(
                executor.map(lambda chunk: [encrypt_one(v) for v in chunk], chunks)
            )
        return [value for chunk_result in chunk_results for value in chunk_result]
