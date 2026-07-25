"""fpe strategy (engine-v2 S9, re-keyed WS1): format-preserving encryption.

The Feistel+HMAC permutation is REUSED from V1 `transforms/fpe.FPEStrategy`
(stdlib hmac, no PyCA -- per the module's design comment and Session 18 B1).

Keying (WS1 detokenization, 2026-06-12, SEED_PROTOCOL_VERSION 4 -> 5): ONE
Feistel key per (job_seed, namespace), `derive(job_seed, namespace,
FPE_KEY_LABEL)`, with the column name as the per-column tweak. This is a
single-key / varying-tweak key model; the underlying primitive is the engine's
home-rolled 8-round HMAC-SHA256 Feistel (`transforms/fpe.py`), which is NOT
NIST SP 800-38G FF1 (no AES, 8 rounds vs FF1's 10, no minimum-domain floor).
An audited FF1 is a documented fast-follow; do not describe this construction
as NIST FF1 in product-facing copy. The key model keeps the S9 contracts (same
value -> same ciphertext within a namespace, byte-stable across runs,
cross-column linkage broken by the tweak) AND makes ciphertext decryptable by
any holder of (job_seed, namespace, column, charset) via `decoy_engine.unmask`.
The pre-WS1 keying derived a key from the PLAINTEXT (`derive(seed, ns,
_canonicalize_source(value))`), which made ciphertext-only reversal impossible
and incidentally paid one HKDF per cell.

Per-row parallelism (S9 spec §5.2): rows are split into `chunk_count` chunks
processed in worker threads, then concatenated. Each value's encryption is
independent + deterministic under the shared key, so chunked and serial
output are byte-identical by construction -- the non-negotiable parity gate.
The lift is wall-clock, not output.

Sprint 2 honesty pack (2026-07-04, S6, GATE-1 Q4, discovery 0.1): a
degenerate charset (fewer than 2 distinct characters after dedup) used to
`return df, []` -- a silent whole-column passthrough (V1 behavior), the same
fail-open shape #13 closed for truncate/bucketize/categorical.
`check_fpe_charset_config` (plan/_checks_fpe.py) rejects the same shape at
compile time; `run` now raises `StrategyError` instead of passing through,
as the defense-in-depth backstop if the compile check is ever bypassed
(e.g. a raw-dict caller that skips `compile_plan`'s checks entirely).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.errors import FpeChecksumError, FpeUnencryptableError
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

# The constant derive() source for the per-(job_seed, namespace) Feistel key.
# Shared with decoy_engine.unmask; changing it is a SEED_PROTOCOL_VERSION bump.
FPE_KEY_LABEL: bytes = b"fpe-key/v1"

# NIST SP 800-38G Rev.1 sets the minimum admissible FPE domain at ~1,000,000
# possible values (radix ** length): below it, ANY format-preserving cipher --
# FF1 included -- leaks. The home-rolled Feistel has no floor, so DE-01
# cluster-C surfaces sub-minimum columns as a documented residual-risk
# QualityWarning (structured channel, not stdout) instead of silently masking
# them under a domain too small to be safe. This axis does NOT leak cleartext;
# it records that the masked output is weaker than an admissible-domain cipher.
_FF1_MIN_DOMAIN = 1_000_000


def _min_domain_length(radix: int, min_domain: int = _FF1_MIN_DOMAIN) -> int:
    """Smallest value length whose domain (radix ** length) reaches min_domain."""
    length = 1
    size = radix
    while size < min_domain:
        size *= radix
        length += 1
    return length


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
            # Sprint 2 honesty pack (S6, GATE-1 Q4): fail closed instead of
            # the V1 passthrough. `check_fpe_charset_config` rejects this at
            # compile time; this is the execution-time backstop.
            raise StrategyError(
                code="fpe_charset_degenerate",
                strategy="fpe",
                message=(
                    f"column {column!r} uses fpe but its resolved charset "
                    f"{charset_spec!r} -> {charset!r} has fewer than 2 distinct "
                    "characters. A degenerate charset has nothing to permute over "
                    "and would leave the column unmasked."
                ),
            )
        preserve_sep = bool(cfg.get("preserve_separators", True))
        # checksum takes priority over validate_luhn when both are configured.
        checksum: str | None = cfg.get("checksum") or None
        validate_luhn = (
            checksum is None
            and bool(cfg.get("validate_luhn", False))
            and all(c in "0123456789" for c in charset)
        )
        # SP-46: opt-in fpe_join_group shares the tweak across member columns.
        # When set, the group name replaces the column name as the tweak so two
        # columns with identical values encrypt identically (joinable ciphertext).
        # Default (no group) is `column` -- byte-identical to pre-SP46 behaviour
        # (`join_group or column` evaluates to column when join_group is falsy).
        # Key derivation is UNCHANGED; the tweak is NOT in derive()'s envelope.
        join_group: str | None = cfg.get("fpe_join_group") or None
        tweak = (join_group or column).encode("utf-8", errors="replace")
        namespace = plan.namespace

        # One key per (mask_key, namespace) -- derived once, not per cell.
        key = derive(ctx.mask_key, namespace, FPE_KEY_LABEL)

        def encrypt_one(value: str) -> str:
            return fpe_encrypt_value(
                value, key, charset, tweak, preserve_sep, validate_luhn, checksum
            )

        source = df[column]
        na_mask = source.isna().to_numpy()
        non_na_positions = np.where(~na_mask)[0]
        # Vectorized non-null materialization: numpy boolean-select (C-level) then
        # str() each, NOT a per-row pandas `.iloc[int(i)]` scalar-access loop (that
        # paid O(n) pandas-indexing overhead V1's C-level astype never did; Dennis
        # S13 FPE-port finding). str() semantics + order are preserved exactly.
        non_na_values = [str(v) for v in source.to_numpy(dtype=object)[~na_mask]]
        # DE-01 cluster-C (2026-07-14): value-level fail-closed raises
        # (`FpeUnencryptableError` for an all-out-of-charset value or a
        # preserve_separators=false out-of-charset value; `FpeChecksumError` for a
        # too-short checksum value) are re-raised at the execution boundary as
        # `StrategyError`, matching the `fpe_charset_degenerate` / truncate /
        # bucketize fail-closed precedent so the runner attributes the failure to
        # this strategy and kills the job before any unsafe output is written.
        try:
            encrypted = self._encrypt_values(non_na_values, encrypt_one)
        except FpeUnencryptableError as exc:
            raise StrategyError(
                code="fpe_unencryptable_value",
                strategy="fpe",
                message=(
                    f"column {column!r}: {exc}. The engine fails closed rather than "
                    "emit unmaskable or non-round-trip output."
                ),
            ) from exc
        except FpeChecksumError as exc:
            raise StrategyError(
                code="fpe_checksum_unsupported",
                strategy="fpe",
                message=f"column {column!r}: {exc}",
            ) from exc

        out: list[object] = [None] * len(source)
        for offset, position in enumerate(non_na_positions):
            out[int(position)] = encrypted[offset]
        df[column] = out

        run_warnings: list[QualityWarning] = []
        run_warnings.extend(
            self._residual_risk_warnings(
                non_na_values,
                charset_set=set(charset),
                radix=len(charset),
                preserve_sep=preserve_sep,
                column=column,
            )
        )
        if join_group:
            run_warnings.append(
                QualityWarning(
                    code="fpe_join_group_active",
                    provider="fpe",
                    column=column,
                    detail={
                        "join_group": join_group,
                        "security_note": ("cross-column domain separation intentionally waived"),
                    },
                )
            )
        return df, run_warnings

    def _residual_risk_warnings(
        self,
        values: list[str],
        *,
        charset_set: set[str],
        radix: int,
        preserve_sep: bool,
        column: str,
    ) -> list[QualityWarning]:
        """Structured residual-risk notes for the two documented DE-01 limits.

        Both ride `ExecutionResult.warnings`, NOT the masked output, so they never
        change a determinism fingerprint:

        - `fpe_sub_minimum_domain`: values whose in-charset domain
          (radix ** in_charset_length) is below the ~1M FF1 minimum. No fix is
          available pre-FF1; this axis does not leak cleartext, it records weaker
          strength for small-domain values.
        - `fpe_partial_plaintext_disclosure`: values that keep an out-of-charset,
          data-bearing (alphanumeric) format prefix in the clear under
          preserve_separators=true (e.g. "M" in "M000001"). This partial-plaintext
          disclosure is a KNOWN limitation of the home-rolled FPE; full coverage
          needs the structured-FPE/FF1 fast-follow with vault_token.
        """
        min_len = _min_domain_length(radix)
        sub_minimum = 0
        partial_prefix = 0
        for value in values:
            in_charset = sum(1 for ch in value if ch in charset_set)
            if 0 < in_charset < min_len:
                sub_minimum += 1
            if preserve_sep and in_charset > 0:
                if any(ch not in charset_set and ch.isalnum() for ch in value):
                    partial_prefix += 1
        warnings: list[QualityWarning] = []
        total = len(values)
        if sub_minimum:
            warnings.append(
                QualityWarning(
                    code="fpe_sub_minimum_domain",
                    provider="fpe",
                    column=column,
                    detail={
                        "sub_minimum_values": sub_minimum,
                        "total_values": total,
                        "radix": radix,
                        "min_domain": _FF1_MIN_DOMAIN,
                        "min_length": min_len,
                        "note": (
                            "values shorter than the FF1 minimum admissible domain "
                            "(radix ** length < ~1,000,000) are format-preserving-"
                            "encrypted under a home-rolled cipher with weaker "
                            "small-domain guarantees; no fix is available pre-FF1. "
                            "This does not leak cleartext."
                        ),
                    },
                )
            )
        if partial_prefix:
            warnings.append(
                QualityWarning(
                    code="fpe_partial_plaintext_disclosure",
                    provider="fpe",
                    column=column,
                    detail={
                        "affected_values": partial_prefix,
                        "total_values": total,
                        "note": (
                            "values retain an out-of-charset, data-bearing format "
                            "prefix in the clear (e.g. 'M' in 'M000001') under "
                            "preserve_separators=true. This residual partial-"
                            "plaintext disclosure is a known limitation of the home-"
                            "rolled FPE; use a charset that covers the prefix, or await "
                            "the structured-FPE/FF1 fast-follow (with vault_token)."
                        ),
                    },
                )
            )
        return warnings

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
