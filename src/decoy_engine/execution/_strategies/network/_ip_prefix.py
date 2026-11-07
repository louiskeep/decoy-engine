"""ip_prefix strategy: keep an IP address's first N bits, pseudonymise the rest (Crypto-PAn).

Thin handler over ``transforms/network/ip_prefix.py``. One Crypto-PAn key per
``(mask_key, namespace)``, ``derive(mask_key, namespace, CRYPTOPAN_KEY_LABEL)``, like ``hash``:
columns sharing a namespace share the mapping, so they join (the compile check makes their keep
bits agree). Per-cell and source-keyed, so chunking and row order cannot change the output.

A non-null cell that is not an address (see the transform module) is left in the frame and
recorded as a ``format_error`` row error, so it is quarantined or fails the run; it is never
emitted unflagged and its value is never put in a message.
"""

from __future__ import annotations

import functools

import pandas as pd

from decoy_engine import reasons
from decoy_engine._opaque import OpaqueBox
from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import (
    StrategyContext,
    StrategyOption,
    option,
    provider_config_to_dict,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.network._cryptopan import CRYPTOPAN_KEY_LABEL, CryptoPan
from decoy_engine.transforms.network.ip_prefix import (
    KEEP_BITS_V4,
    KEEP_BITS_V6,
    STRATEGY,
    IpPrefixConfigFault,
    IpPrefixMasker,
    parse_ip_prefix_config,
)

# Bounds the per-column memo of distinct values; a column with more distinct addresses just
# recomputes the evicted ones (the mapping is deterministic).
MEMO_SIZE = 65_536


class IpPrefixStrategyHandler:
    """Prefix-keeping keyed pseudonymisation of IPv4 and IPv6 addresses."""

    name: str = STRATEGY
    namespace_required: bool = True
    # Injective per address, but join_safe also makes a strategy a native FK key on the
    # sequential route (`_sequential_native._KEY_STRATEGIES`), whose REMAP minting needs a
    # compiled kernel this strategy does not have. Shared-namespace columns still join.
    join_safe: bool = False
    wizard_options: tuple[StrategyOption, ...] = (
        option(KEEP_BITS_V4, "int", 16),
        option(KEEP_BITS_V6, "int", 64),
    )
    # Opt-in only for now: the CLI wizard does not offer it.
    offer_in_wizard: bool = False

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code=reasons.runtime.IP_PREFIX_REQUIRES_NAMESPACE,
                strategy=STRATEGY,
                message=f"column {column!r} uses the ip_prefix strategy but has no namespace.",
            )
        parsed = parse_ip_prefix_config(provider_config_to_dict(plan.provider_config))
        if isinstance(parsed, IpPrefixConfigFault):
            raise StrategyError(
                code=reasons.runtime.IP_PREFIX_CONFIG_INVALID,
                strategy=STRATEGY,
                message=f"column {column!r}: {parsed.describe()}.",
            )
        key_box = OpaqueBox(derive(ctx.mask_key, plan.namespace, CRYPTOPAN_KEY_LABEL))
        masker = IpPrefixMasker(CryptoPan(key_box), parsed)
        mask_one = functools.lru_cache(maxsize=MEMO_SIZE)(masker.mask)

        values = df[column].tolist()
        nulls = df[column].isna().tolist()
        out: list[object] = []
        for i, (value, is_null) in enumerate(zip(values, nulls, strict=True)):
            masked = None if is_null or not isinstance(value, str) else mask_one(value)
            if is_null:
                out.append(value)
            elif masked is None:
                # Kept in the frame only so the quarantine gate can route the row; the
                # row error guarantees it never reaches the main output unflagged.
                out.append(value)
                ctx.row_errors.append(
                    RowError(
                        column=column,
                        row_index=i,
                        trigger="format_error",
                        reason="value is not an IP address under ip_prefix",
                    )
                )
            else:
                out.append(masked)
        df[column] = pd.Series(out, index=df.index, dtype=object)
        return df, []
