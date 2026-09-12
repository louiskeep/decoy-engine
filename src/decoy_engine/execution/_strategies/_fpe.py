"""fpe strategy (engine-v2 S9; Task 5.2 re-keyed onto NIST SP 800-38G FF1).

Keying (WS1 detokenization, 2026-06-12, SEED_PROTOCOL_VERSION 4 -> 5, then
Task 5.2's 6 -> 7 for the FF1 primitive swap): ONE AES-256 key per
(job_seed, namespace), `derive(job_seed, namespace, FF1_KEY_LABEL)`, with
the column name (or `fpe_join_group`) framed into the per-column tweak per
`transforms.fpe.build_ff1_tweak`. This is a single-key / varying-tweak key
model. The underlying primitive is NIST SP 800-38G FF1 (`transforms/_ff1.py`,
KAT-locked; wired in via `transforms/fpe.py`'s deployable-profile wrapper),
replacing the engine's earlier home-rolled 8-round HMAC-SHA256 Feistel
entirely. The key model keeps the S9 contracts (same value -> same
ciphertext within a namespace, byte-stable across runs, cross-column linkage
broken by the tweak) AND makes ciphertext decryptable by any holder of
(job_seed, namespace, column, charset) via `decoy_engine.unmask`.

Per-row parallelism (S9 spec §5.2): rows are split into `chunk_count` chunks
processed in worker threads, then concatenated. Each value's encryption is
independent and deterministic under the shared key, so chunked and serial
output are byte-identical by construction, which is the non-negotiable
parity gate. The lift is wall-clock, not output.

Sprint 2 honesty pack (2026-07-04, S6, GATE-1 Q4, discovery 0.1): a
degenerate charset (fewer than 2 distinct characters after dedup) used to
`return df, []`, a silent whole-column passthrough (V1 behavior) and the
same fail-open shape #13 closed for truncate/bucketize/categorical.
`check_fpe_charset_config` (plan/_checks_fpe.py) rejects the same shape at
compile time; `run` now raises `StrategyError` instead of passing through,
as the defense-in-depth backstop if the compile check is ever bypassed
(e.g. a raw-dict caller that skips `compile_plan`'s checks entirely).

Sub-minimum domain (Task 5.2, plan v3 P2/round-2 HIGH-3): FF1 is
undefined/insecure below `radix ** length < FF1_MIN_DOMAIN` (~1,000,000).
Pre-FF1 this axis surfaced only as a residual-risk `QualityWarning`; under
FF1 it is enforced fail-closed inside `transforms.fpe._permute` itself
(`FpeUnencryptableError`, code `fpe.unencryptable_domain`), so a
sub-minimum value now kills the job rather than masking under a domain too
small to be safe. `_residual_risk_warnings` below keeps only the (still
non-fatal) partial-plaintext-prefix warning.
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
from decoy_engine.transforms import _ff1
from decoy_engine.transforms.fpe import FF1_KEY_LABEL as FF1_KEY_LABEL  # re-exported, see below
from decoy_engine.transforms.fpe import (
    FF1_TWEAK_SCOPE_COLUMN,
    FF1_TWEAK_SCOPE_JOIN_GROUP,
    build_ff1_tweak,
    check_charset_unique,
    fpe_encrypt_value,
    resolve_fpe_charset,
)

# `FF1_KEY_LABEL` above is re-exported for existing importers of this module
# (unmask.py, out_of_core/_mask_group_b.py, native/_crypto_reference.py); the
# canonical definition lives in `transforms.fpe` so `transforms/` never
# depends on `execution/` for its own key-label constant. Changing the value
# is a SEED_PROTOCOL_VERSION bump either way.

_FF1_MIN_DOMAIN = _ff1.FF1_MIN_DOMAIN

# Maps the low-level exception's `.code` (dotted) onto the handler-surface
# `StrategyError` code (underscored) the runner / operator sees. Codes not in
# this map keep their existing default (`fpe_unencryptable_value` for any
# `FpeUnencryptableError` whose `.code` isn't listed, matching pre-Task-5.2
# behavior for the DE-01 guards verbatim).
_UNENCRYPTABLE_STRATEGY_CODE: dict[str, str] = {
    "fpe.unencryptable_domain": "fpe_unencryptable_domain",
    "fpe.unencryptable_length": "fpe_unencryptable_length",
}
_CHECKSUM_STRATEGY_CODE: dict[str, str] = {
    "fpe.checksum_invalid_source": "fpe_checksum_invalid_source",
}


def strategy_code_for_unencryptable(exc: FpeUnencryptableError) -> str:
    """Map an `FpeUnencryptableError` onto its `StrategyError`/row-error code."""
    return _UNENCRYPTABLE_STRATEGY_CODE.get(exc.code, "fpe_unencryptable_value")


def strategy_code_for_checksum(exc: FpeChecksumError) -> str:
    """Map an `FpeChecksumError` onto its `StrategyError`/row-error code."""
    return _CHECKSUM_STRATEGY_CODE.get(exc.code, "fpe_checksum_unsupported")


class FpeStrategyHandler:
    """Format-preserving encryption via NIST SP 800-38G FF1, keyed onto derive."""

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
        charset = resolve_fpe_charset(charset_spec)
        try:
            check_charset_unique(charset_spec, charset)
        except FpeUnencryptableError as exc:
            raise StrategyError(
                code="fpe_charset_duplicate_symbols",
                strategy="fpe",
                message=f"column {column!r}: {exc}",
            ) from exc
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
        # Default (no group) is `column`. Key derivation is unchanged either way;
        # the tweak is not in derive()'s envelope, only in the FF1 call itself.
        join_group: str | None = cfg.get("fpe_join_group") or None
        if join_group:
            tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_JOIN_GROUP, join_group)
        else:
            tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, column)
        namespace = plan.namespace

        # One key per (mask_key, namespace), derived once, not per cell.
        key = derive(ctx.mask_key, namespace, FF1_KEY_LABEL)

        def encrypt_one(value: str) -> str:
            return fpe_encrypt_value(
                value, key, charset, tweak, preserve_sep, validate_luhn, checksum
            )

        source = df[column]
        na_mask = source.isna().to_numpy()
        # Vectorized non-null materialization: numpy boolean-select (C-level) then
        # str() each, NOT a per-row pandas `.iloc[int(i)]` scalar-access loop (that
        # paid O(n) pandas-indexing overhead V1's C-level astype never did; Dennis
        # S13 FPE-port finding). str() semantics + order are preserved exactly.
        raw_non_na = [str(v) for v in source.to_numpy(dtype=object)[~na_mask]]
        non_na_positions = np.where(~na_mask)[0]
        # Empty string is not null (`na_mask` misses it), but `fpe_encrypt_value`
        # now fails closed on it (plan P2: an empty value's in-charset domain,
        # radix**0 == 1, is below the FF1 floor like any other sub-floor value).
        # Treat it like null at THIS per-cell missing-data boundary -- preserved
        # as `""`, never sent to the cipher -- so a column with legitimate empty
        # cells (the same nullable-data shape a real source table has) does not
        # fail the whole run. This is a strategy-level missing-data policy, not
        # a crypto-layer carve-out: the value function's own contract stays
        # honestly fail-closed for any other caller that reaches it with "".
        empty_local_mask = np.array([v == "" for v in raw_non_na])
        encrypt_positions = non_na_positions[~empty_local_mask]
        empty_positions = non_na_positions[empty_local_mask]
        non_na_values = [
            v for v, is_empty in zip(raw_non_na, empty_local_mask, strict=True) if not is_empty
        ]
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
                code=strategy_code_for_unencryptable(exc),
                strategy="fpe",
                message=(
                    f"column {column!r}: {exc}. The engine fails closed rather than "
                    "emit unmaskable or non-round-trip output."
                ),
            ) from exc
        except FpeChecksumError as exc:
            raise StrategyError(
                code=strategy_code_for_checksum(exc),
                strategy="fpe",
                message=f"column {column!r}: {exc}",
            ) from exc

        out: list[object] = [None] * len(source)
        for position in empty_positions:
            out[int(position)] = ""
        for offset, position in enumerate(encrypt_positions):
            out[int(position)] = encrypted[offset]
        df[column] = out

        run_warnings: list[QualityWarning] = []
        run_warnings.extend(
            self._residual_risk_warnings(
                non_na_values,
                charset_set=set(charset),
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
        preserve_sep: bool,
        column: str,
    ) -> list[QualityWarning]:
        """Structured residual-risk notes for the remaining documented DE-01 limit.

        Rides `ExecutionResult.warnings`, NOT the masked output, so it never
        changes a determinism fingerprint.

        `fpe_partial_plaintext_disclosure`: values that keep an out-of-charset,
        data-bearing (alphanumeric) format prefix in the clear under
        preserve_separators=true (e.g. "M" in "M000001"). This is a known
        limitation independent of the FF1 swap; full coverage needs a
        structured/typed-subfield FPE follow-on.

        The sibling `fpe_sub_minimum_domain` warning this method used to emit
        is gone: under FF1 that condition is no longer survivable output with
        a caveat, it is a fail-closed `FpeUnencryptableError` raised inside
        `transforms.fpe._permute` before any value in the column is encrypted
        (see `run`'s `_encrypt_values` call), so this method never sees a
        sub-minimum value to warn about.
        """
        partial_prefix = 0
        for value in values:
            in_charset = sum(1 for ch in value if ch in charset_set)
            if preserve_sep and in_charset > 0:
                if any(ch not in charset_set and ch.isalnum() for ch in value):
                    partial_prefix += 1
        warnings: list[QualityWarning] = []
        total = len(values)
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
                            "plaintext disclosure is a known FPE limitation "
                            "independent of the cipher; use a charset that covers "
                            "the prefix, or await a structured/typed-subfield FPE "
                            "follow-on (with vault_token)."
                        ),
                    },
                )
            )
        return warnings

    def _encrypt_values(self, values: list[str], encrypt_one: Callable[[str], str]) -> list[str]:
        # Cap workers at the actual CPU count: FF1's per-value work is GIL-bound
        # pure Python calling into `cryptography`'s C-accelerated AES for each
        # round (the AES calls release the GIL; the Python-side round loop does
        # not), so spawning more threads than cores adds contention and overhead
        # without parallelism (net-negative on a 2-vCPU CI runner). Output is
        # identical for any worker count (each value's encryption is independent
        # and deterministic), so this is wall-clock only; the byte-identical
        # parity gate is unaffected.
        workers = min(self._chunk_count, os.cpu_count() or 1)
        if workers <= 1 or len(values) < workers:
            return [encrypt_one(v) for v in values]
        chunks = [list(chunk) for chunk in np.array_split(np.array(values, dtype=object), workers)]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            chunk_results = list(
                executor.map(lambda chunk: [encrypt_one(v) for v in chunk], chunks)
            )
        return [value for chunk_result in chunk_results for value in chunk_result]
