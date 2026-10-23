"""tokenize strategy (MG-5): reversible tokenization via a platform vault.

The engine emits a deterministic surrogate (the token) and records the
``(plaintext, token)`` pair through a platform-injected ``TokenVaultWriter``;
the platform owns the vault, its access control, and key custody (reversible
tokenization is an auth + persistence problem, not a data-behavior one). The
engine holds no vault -- library code does not know its callers -- so with no
vault the strategy fails closed.

Token shape: ``tok_`` + 32 hex chars of ``derive(mask_key, namespace,
plaintext)``, keyed on the run's mask key AND the column namespace, so the same
value tokenizes differently in a different namespace/vault. The token is
deterministic, but the VAULT is the identity boundary: if a token is already
stored for a plaintext it is REUSED, so a value's token is stable across jobs
and across key rotations.

Established methodology: reuses the engine's HKDF-SHA256 ``derive`` (the same
keyed derivation every re-identification-protecting strategy draws from), not a
hand-rolled hash. Format-preserving tokens (same shape as the plaintext, via FPE
under a vault key -- shape only, NOT crypto reversal) are a tracked follow-up;
this slice ships the opaque default.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

_TOKEN_PREFIX = "tok_"  # noqa: S105 - a surrogate-token prefix, not a secret
_TOKEN_HEX_LEN = 32


class TokenizeStrategyHandler:
    """Reversible tokenization: deterministic surrogate + vault-recorded mapping."""

    name: str = "tokenize"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        # getattr: handlers are also driven with hand-built contexts that predate
        # the field; a missing vault is the fail-closed case either way.
        vault = getattr(ctx, "vault_writer", None)
        if vault is None:
            raise StrategyError(
                code="tokenize_requires_vault",
                strategy="tokenize",
                message=(
                    f"column {column!r} uses the tokenize strategy but no vault was "
                    "provided by the platform; tokenize requires a vault-backed runner."
                ),
            )
        if plan.namespace is None:
            raise StrategyError(
                code="tokenize_requires_namespace",
                strategy="tokenize",
                message=(
                    f"column {column!r} uses the tokenize strategy but has no namespace; "
                    "the token is keyed on it (so different namespaces tokenize apart)."
                ),
            )

        out: list[str | None] = []
        for value in df[column].tolist():
            if pd.isna(value):
                out.append(None)  # null preserved; nothing to tokenize
                continue
            plaintext = str(value)
            existing = vault.lookup_existing(plaintext)
            if existing is not None:
                out.append(existing)  # the vault is authoritative for identity
                continue
            token = (
                _TOKEN_PREFIX
                + derive(ctx.mask_key, plan.namespace, plaintext.encode("utf-8")).hex()[
                    :_TOKEN_HEX_LEN
                ]
            )
            vault.put(plaintext, token)
            out.append(token)
        df[column] = out
        return df, []
