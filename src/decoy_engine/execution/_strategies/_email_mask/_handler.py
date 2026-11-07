"""email_mask strategy: replace the local part with a keyed token, keep the domain or its class.

The token is the first 20 hex characters of ``derive(mask_key, namespace, NFC(value))``, the
HMAC-SHA256 draw `hash` and `tokenize` use, keyed on the whole address so two addresses that share
a local part never share a token. A value with no usable domain gets the token at
`unknown.invalid`, so nothing of it survives. Requires an admitted mask secret: a token keyed on
the public job seed next to a kept domain would let anyone with the config confirm a guessed
address.
"""

from __future__ import annotations

import unicodedata

import pandas as pd

from decoy_engine import reasons
from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import (
    StrategyContext,
    StrategyOption,
    option,
    provider_config_to_dict,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._email_mask._address import (
    KEEP_CHOICES,
    output_domain,
    split_domain,
)
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.keyprovider import (
    KEY_REQUIRED_GUIDANCE,
    MIN_SECRET_BYTES,
    KeyedStrategyRequiresSecret,
)
from decoy_engine.plan._types import ColumnSeed

TOKEN_HEX_LEN = 20


def _canonical(value: object) -> bytes:
    # NFC like `hash`'s string branch; surrogatepass so an unpaired surrogate keys deterministically.
    return unicodedata.normalize("NFC", str(value)).encode("utf-8", "surrogatepass")


def _config_error(column: str, detail: str) -> StrategyError:
    return StrategyError(
        code=reasons.runtime.EMAIL_MASK_CONFIG_INVALID,
        strategy="email_mask",
        message=f"column {column!r}: {detail}",
    )


def _resolve(column: str, plan: ColumnSeed) -> tuple[str, str]:
    """(keep, namespace), or the backstop refusal when compile-time checks were bypassed."""
    cfg = provider_config_to_dict(plan.provider_config)
    keep = cfg.get("keep")
    if set(cfg) != {"keep"} or not isinstance(keep, str) or keep not in KEEP_CHOICES:
        raise _config_error(column, "provider_config must be exactly {keep: domain|domain_class}.")
    if plan.namespace is None:
        raise _config_error(column, "a namespace is required; the token is keyed on it.")
    return keep, plan.namespace


class EmailMaskStrategyHandler:
    """Keyed local-part token plus the source domain or its class."""

    name: str = "email_mask"
    namespace_required: bool = True
    join_safe: bool = False
    wizard_options: tuple[StrategyOption, ...] = (
        option("keep", "str", "domain_class", required=True, choices=KEEP_CHOICES),
    )
    offer_in_wizard: bool = False

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        keep, namespace = _resolve(column, plan)
        # A hand-built context defaults mask_key to the 8-byte job seed; the run path admits a
        # secret first, so this only fires when the handler is driven directly.
        if len(ctx.mask_key) < MIN_SECRET_BYTES:
            raise KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)
        col = df[column]
        nulls = col.isna().tolist()
        if all(nulls):
            return df, []
        if not (pd.api.types.is_object_dtype(col.dtype) or isinstance(col.dtype, pd.StringDtype)):
            raise StrategyError(
                code=reasons.runtime.EMAIL_MASK_REQUIRES_STRING_DTYPE,
                strategy="email_mask",
                message=f"column {column!r} has source dtype {col.dtype!s}; email_mask needs text.",
            )
        out: list[str | None] = []
        malformed = 0
        for value, is_null in zip(col.tolist(), nulls, strict=True):
            if is_null:
                out.append(None)
                continue
            domain = split_domain(value)
            malformed += domain is None
            token = derive(ctx.mask_key, namespace, _canonical(value)).hex()[:TOKEN_HEX_LEN]
            out.append(f"{token}@{output_domain(domain, keep)}")
        df[column] = out
        if not malformed:
            return df, []
        warning = QualityWarning(
            code=reasons.warning.EMAIL_MASK_MALFORMED_VALUES,
            provider="email_mask",
            column=column,
            detail={"malformed_count": malformed},
        )
        return df, [warning]
