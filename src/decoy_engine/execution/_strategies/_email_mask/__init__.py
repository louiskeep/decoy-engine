"""The `email_mask` strategy: keyed local part, domain or domain class kept."""

from __future__ import annotations

from decoy_engine.execution._strategies._email_mask._handler import EmailMaskStrategyHandler

__all__ = ["EmailMaskStrategyHandler"]
