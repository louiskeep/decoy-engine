"""Identifier validators, adapters, and domains as a focused sub-import.

These identifier families live in ``decoy_engine.providers_v2.identifiers``.
They are re-exported here so callers can reach them via a small, stable
namespace (``decoy_engine.identifiers.EinValidator``) instead of widening the
top-level ``decoy_engine`` public surface. The top-level package keeps its
module bindings for backward compatibility, but these symbols are no longer
part of ``decoy_engine.__all__``.
"""

from __future__ import annotations

from decoy_engine.providers_v2.identifiers import (
    EinAdapter,
    EinDomain,
    EinValidator,
    IdentifierError,
    IdentifierFormatError,
    MrnAdapter,
    MrnDomain,
    MrnValidator,
    NdcAdapter,
    NdcDomain,
    NdcValidator,
    NpiAdapter,
    NpiDomain,
    NpiValidator,
    SsnAdapter,
    SsnDomain,
    SsnValidator,
)

__all__ = [
    "EinAdapter",
    "EinDomain",
    "EinValidator",
    "IdentifierError",
    "IdentifierFormatError",
    "MrnAdapter",
    "MrnDomain",
    "MrnValidator",
    "NdcAdapter",
    "NdcDomain",
    "NdcValidator",
    "NpiAdapter",
    "NpiDomain",
    "NpiValidator",
    "SsnAdapter",
    "SsnDomain",
    "SsnValidator",
]
