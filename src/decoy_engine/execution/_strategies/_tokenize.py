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
from decoy_engine.execution._adapter import StrategyContext, TokenVaultWriter
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

        values = df[column].tolist()
        # Distinct non-null plaintexts, order-stable. The column is resolved with
        # one vault lookup + one write over these distinct values, not a per-cell
        # round-trip: at the million-row counts the product advertises, per-cell
        # I/O is 1M+ synchronous DB calls inside the job transaction. Dedup also
        # means a repeated value is looked up and written exactly once.
        distinct: list[str] = []
        seen: set[str] = set()
        for value in values:
            if pd.isna(value):
                continue
            plaintext = str(value)
            if plaintext not in seen:
                seen.add(plaintext)
                distinct.append(plaintext)

        token_for = self._resolve_tokens(vault, distinct, ctx.mask_key, plan.namespace)

        # Nulls preserved; every non-null maps through the resolved tokens.
        df[column] = [None if pd.isna(value) else token_for[str(value)] for value in values]
        return df, []

    def _resolve_tokens(
        self,
        vault: TokenVaultWriter,
        distinct: list[str],
        mask_key: bytes,
        namespace: str,
    ) -> dict[str, str]:
        """``{plaintext: token}`` for every distinct plaintext, reusing any token
        the vault already holds (the vault is authoritative) and minting the rest.

        Uses the optional ``BatchTokenVaultWriter`` seam when the injected writer
        provides it (one lookup + one bulk write); falls back to the per-row
        ``lookup_existing`` / ``put`` otherwise, so a writer that predates the
        batch seam still works. Either way the row-level result is identical: a
        hit reuses the stored token, a miss gets the deterministic surrogate."""
        token_for: dict[str, str] = {}
        if not distinct:
            return token_for

        lookup_many = getattr(vault, "lookup_existing_many", None)
        if callable(lookup_many):
            token_for.update(lookup_many(distinct))
        else:
            for plaintext in distinct:
                existing = vault.lookup_existing(plaintext)
                if existing is not None:
                    token_for[plaintext] = existing

        minted = {
            plaintext: _TOKEN_PREFIX
            + derive(mask_key, namespace, plaintext.encode("utf-8")).hex()[:_TOKEN_HEX_LEN]
            for plaintext in distinct
            if plaintext not in token_for
        }
        if minted:
            put_many = getattr(vault, "put_many", None)
            if callable(put_many):
                put_many(minted)
            else:
                for plaintext, token in minted.items():
                    vault.put(plaintext, token)
            token_for.update(minted)
        return token_for
