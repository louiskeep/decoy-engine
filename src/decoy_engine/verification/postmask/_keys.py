"""Declared relationship key checks (stub)."""

from __future__ import annotations

from typing import Any

__all__ = ["KeyCounts", "KeyResult", "check_keys", "keys_report"]


class KeyCounts: ...


class KeyResult: ...


def check_keys(*args: Any, **kwargs: Any) -> tuple[KeyResult, ...]:
    return ()


def keys_report(results: Any) -> list[dict[str, Any]]:
    return []
