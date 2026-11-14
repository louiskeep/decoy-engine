"""One reader for boolean environment flags."""

from __future__ import annotations

__all__ = ["env_flag"]


def env_flag(name: str, *, default: bool) -> bool:
    return default
