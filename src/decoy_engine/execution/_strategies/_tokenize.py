"""tokenize strategy (MG-5): reversible tokenization via a platform vault.

The engine emits a deterministic surrogate (the token) and records the
``(plaintext, token)`` pair through a platform-injected ``TokenVaultWriter``;
the platform owns the vault, its access control, and key custody (reversible
tokenization is an auth + persistence problem, not a data-behavior one). The
engine holds no vault -- library code does not know its callers -- so with no
vault the strategy fails closed.

Token shape: ``tok_`` + 32 hex chars of ``derive(mask_key, namespace,
nfc_normalize(str(value)).encode())``, keyed on the run's mask key AND the
column namespace, so the same value tokenizes differently in a different
namespace/vault. The token is deterministic, but the VAULT is the identity
boundary: if a token is already stored for a ``(namespace, plaintext)`` pair it
is REUSED, so a value's token is stable across jobs and across key rotations.
On a miss the strategy only PROPOSES the derived token; ``put`` returns the
authoritative stored one (a concurrent winner's, under a race) and that is what
is emitted.
The vault lookup and write are both scoped by namespace, so the same plaintext
under two namespaces is two distinct entries with two distinct tokens, never
one (namespaces cannot be linked through a shared token, and execution order
does not decide the token).

Established methodology: reuses the engine's HKDF-SHA256 ``derive`` (the same
keyed derivation every re-identification-protecting strategy draws from), not a
hand-rolled hash, AND the same Unicode NFC string normalization ``mask.hash``
routes its derive input through (via ``_canonicalize_source``'s string branch)
rather than a bare ``str(value)`` -- tokenize is a REVERSIBLE primitive used as
an FK key, so two values that mean the same thing must canonicalize (and
therefore tokenize) identically. V0 is string-only: a fail-closed, coded
preflight restricts the source column to string/object dtype, which sidesteps
``mask.hash``'s other canonicalization branches entirely (no int-null-widening,
float, or tz-naive-datetime hazard ever reaches this handler, since none of
those dtypes passes the preflight). Wiring the full per-value
``_canonicalize_source`` dispatch (int/float/datetime branches, hard-errors
included) to lift this restriction is a tracked V1 follow-up. Format-preserving
tokens (same shape as the plaintext, via FPE under a vault key -- shape only,
NOT crypto reversal) are a separate tracked follow-up; this slice ships the
opaque default.
"""

from __future__ import annotations

import unicodedata

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
        col = df[column]
        # HIGH-1 (V0 canonicalization guard): tokenize is reversible and used as an
        # FK key, so a null-widened int column tokenizing as "123.0" (wrong on
        # reversal), or an int parent vs. a float-widened child FK tokenizing apart
        # (broken join), is not acceptable. Restricting the source to string/object
        # dtype sidesteps that widening hazard entirely (and the float / tz-naive-
        # datetime non-determinism mask.hash's canonicalizer hard-errors on): an
        # int/float/datetime/bool column fails closed here rather than silently
        # canonicalizing wrong.
        if not (pd.api.types.is_object_dtype(col.dtype) or isinstance(col.dtype, pd.StringDtype)):
            raise StrategyError(
                code="tokenize_requires_string_dtype",
                strategy="tokenize",
                message=(
                    f"column {column!r} uses the tokenize strategy but its source dtype "
                    f"is {col.dtype!s}; V0 tokenize supports only string/object source "
                    "columns."
                ),
            )

        namespace = plan.namespace

        def _vault_error(op: str) -> StrategyError:
            # A vault callback error must become a fixed, coded, run-aborting error
            # that names neither the source value nor the token: a raw callback
            # exception (e.g. a database error echoing the plaintext) would otherwise
            # leak the value it is meant to protect through run_pipeline's exception.
            return StrategyError(
                code="tokenize_vault_error",
                strategy="tokenize",
                message=(
                    f"column {column!r}: the token vault raised during {op}; the run "
                    "is aborted so no partial or unrecorded output is written. The "
                    "underlying vault error is suppressed to avoid leaking the source "
                    "value or token."
                ),
            )

        out: list[str | None] = []
        for value in col.tolist():
            if pd.isna(value):
                out.append(None)  # null preserved; nothing to tokenize
                continue
            # NFC-normalize like mask.hash's string branch (_canonicalize_source), so
            # two Unicode-equivalent-but-byte-different values tokenize identically.
            # The vault's plaintext key and the derive input are the SAME string, so
            # lookup/reversal and the token stay consistent with each other.
            plaintext = unicodedata.normalize("NFC", str(value))
            try:
                existing = vault.lookup_existing(namespace, plaintext)
            except Exception:
                raise _vault_error("lookup") from None
            if existing is not None:
                out.append(existing)  # the vault is authoritative for identity
                continue
            token = (
                _TOKEN_PREFIX
                + derive(ctx.mask_key, namespace, plaintext.encode("utf-8")).hex()[:_TOKEN_HEX_LEN]
            )
            try:
                stored = vault.put(namespace, plaintext, token)
            except Exception:
                raise _vault_error("write") from None
            # The vault's committed token wins a race; a writer that returns
            # nothing (the pre-0.16 Protocol) must not publish a null/unmapped token.
            if not isinstance(stored, str) or not stored:
                raise _vault_error("write")
            out.append(stored)
        df[column] = out
        return df, []
