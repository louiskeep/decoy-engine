"""Stub."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

Fetch = Callable[[str], Any]
Git = Callable[[Sequence[str]], str]


class CheckError(Exception):
    pass


class DerivationError(CheckError):
    pass


def workflow_events(doc: dict[Any, Any]) -> set[str]:
    return set()


def job_check_names(key: str, job: dict[str, Any]) -> list[str]:
    return []


def main(
    argv: list[str] | None = None,
    fetch: Fetch | None = None,
    git: Git | None = None,
    now: Any = None,
) -> int:
    return 99
