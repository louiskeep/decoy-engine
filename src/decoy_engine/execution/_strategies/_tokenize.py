"""tokenize strategy (MG-5): reversible tokenization via a platform vault.

The engine emits a deterministic surrogate (the token) and records the
``(plaintext, token)`` pair through a platform-injected ``TokenVaultWriter``;
the platform owns the vault, its access control, and key custody (reversible
tokenization is an auth + persistence problem, not a data-behavior one). The
engine holds no vault -- library code does not know its callers -- so with no
vault the strategy fails closed.

Token shape: ``tok_`` + 32 hex chars of ``derive(K_tok, namespace,
nfc_normalize(str(value)).encode())`` where ``K_tok`` is the vault-scoped subkey
(the run's mask key, the column namespace, the ``tokenize-key/v1`` label and the vault's own
``derivation_scope``). The same
value therefore tokenizes differently in a different namespace or a different
vault, and never equals a ``hash`` output. A writer without a scope is refused
before any row is read. The token is deterministic, but the VAULT is the identity
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
keyed derivation every re-identification-protecting strategy draws from) under a
purpose-separated subkey (RFC 5869 §3.2), not a hand-rolled hash, AND the same Unicode NFC string normalization ``mask.hash``
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

Pattern: HKDF purpose separation (RFC 5869 §3.2 `info`; NIST SP 800-108r1 §4 Label and Context)
through the engine's existing `derive` envelope, the same shape as `vault-key/v1` (`vault.py`) and
`ff1-key/v1` (`determinism/_derive.py`).

    K_tok = derive(mask_key, namespace, b"tokenize-key/v1" || len(scope) || scope)
    token = "tok_" + hex(derive(K_tok, namespace, NFC(plaintext))[:16])

`scope` is the vault's own `derivation_scope`: a non-secret, vault-unique byte string the vault's
owner mints once. Binding it makes two vaults under one mask key and namespace tokenize the same
plaintext apart, so tokens cannot be correlated across vaults. The label makes the token HMAC key
an HKDF output under a different IKM from `hash`'s (`derive(mask_key, namespace, ...)`), so a
`hash` output is never a token payload. A label change is a new hard cut.

A writer with no scope predates this derivation; its tokens were minted under the old one, so it
is refused (`token_vault_derivation_unsupported`) rather than minted into under a second
derivation (pre-release hard cut, no migration).
"""

from __future__ import annotations

import unicodedata
from typing import Any

import pandas as pd

from decoy_engine import reasons
from decoy_engine.determinism import derive
from decoy_engine.determinism._derive import DeriveContext
from decoy_engine.execution._adapter import StrategyContext, StrategyOption
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

TOKENIZE_KEY_LABEL = b"tokenize-key/v1"
SCOPE_MIN_BYTES = 16
SCOPE_MAX_BYTES = 64
_TOKEN_PREFIX = "tok_"  # noqa: S105 - a surrogate-token prefix, not a secret
_TOKEN_HEX_LEN = 32


def _summary(code: str) -> str:
    # The refusal text is the registered summary, verbatim: no column, value or scope in it.
    return reasons.get("runtime", code).summary


def require_derivation_scope(writer: Any) -> bytes:
    """The writer's `derivation_scope`, or the coded refusal before any row is processed."""
    unreadable = False
    try:
        scope = getattr(writer, "derivation_scope", None)
    except Exception:
        unreadable = True
    if scope is None and not unreadable:
        raise StrategyError(
            code=reasons.runtime.TOKEN_VAULT_DERIVATION_UNSUPPORTED,
            strategy="tokenize",
            message=_summary(reasons.runtime.TOKEN_VAULT_DERIVATION_UNSUPPORTED),
        )
    if (
        unreadable
        or type(scope) is not bytes
        or not SCOPE_MIN_BYTES <= len(scope) <= SCOPE_MAX_BYTES
    ):
        raise StrategyError(
            code=reasons.runtime.TOKENIZE_VAULT_SCOPE_REQUIRED,
            strategy="tokenize",
            message=_summary(reasons.runtime.TOKENIZE_VAULT_SCOPE_REQUIRED),
        )
    return scope


def tokenize_context(ctx: StrategyContext, namespace: str, scope: bytes) -> DeriveContext:
    """The per-column token HMAC context under the vault-scoped subkey `K_tok`. The mask key is
    read inside the derive call, so no name in this frame holds its bytes."""
    label = TOKENIZE_KEY_LABEL + len(scope).to_bytes(4, "big") + scope
    return DeriveContext.for_column(derive(ctx.mask_key, namespace, label), namespace)


def token_from(keyed: DeriveContext, namespace: str, plaintext: str) -> str:
    """The proposed token for an NFC-normalized plaintext."""
    digest = keyed.derive_source(namespace, plaintext.encode("utf-8"))
    return _TOKEN_PREFIX + digest.hex()[:_TOKEN_HEX_LEN]


class TokenizeStrategyHandler:
    """Reversible tokenization: deterministic surrogate + vault-recorded mapping."""

    name: str = "tokenize"
    namespace_required: bool = True
    join_safe: bool = False
    wizard_options: tuple[StrategyOption, ...] = ()
    offer_in_wizard: bool = False

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
                code=reasons.runtime.TOKENIZE_REQUIRES_VAULT,
                strategy="tokenize",
                message=(
                    f"column {column!r} uses the tokenize strategy but no vault was "
                    "provided by the platform; tokenize requires a vault-backed runner."
                ),
            )
        scope = require_derivation_scope(vault)
        if plan.namespace is None:
            raise StrategyError(
                code=reasons.runtime.TOKENIZE_REQUIRES_NAMESPACE,
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
                code=reasons.runtime.TOKENIZE_REQUIRES_STRING_DTYPE,
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
                code=reasons.runtime.TOKENIZE_VAULT_ERROR,
                strategy="tokenize",
                message=(
                    f"column {column!r}: the token vault raised during {op}; the run "
                    "is aborted so no partial or unrecorded output is written. The "
                    "underlying vault error is suppressed to avoid leaking the source "
                    "value or token."
                ),
            )

        keyed = tokenize_context(ctx, namespace, scope)
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
            # The coded error is raised after the handler has left, so neither `__cause__` nor
            # `__context__` can hold the vault's own (value-bearing) exception.
            existing = None
            failed = False
            try:
                existing = vault.lookup_existing(namespace, plaintext)
            except Exception:
                failed = True
            if failed:
                raise _vault_error("lookup")
            if existing is not None:
                out.append(existing)  # the vault is authoritative for identity
                continue
            token = token_from(keyed, namespace, plaintext)
            stored = None
            try:
                stored = vault.put(namespace, plaintext, token)
            except Exception:
                failed = True
            if failed:
                raise _vault_error("write")
            # The vault's committed token wins a race; a writer that returns
            # nothing (the pre-0.16 Protocol) must not publish a null/unmapped token.
            if not isinstance(stored, str) or not stored:
                raise _vault_error("write")
            out.append(stored)
        df[column] = out
        return df, []
