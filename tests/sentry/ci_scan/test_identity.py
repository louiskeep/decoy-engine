"""The cross-repository identity helper: what it retries, what it calls a mismatch, and why a
one-sided edit with a recomputed ledger hash still fails. Shared byte-for-byte by both repos."""

from __future__ import annotations

import hashlib
import io
import urllib.error
from collections.abc import Callable
from typing import Any

from tests.sentry.ci_scan._identity import (
    Fetched,
    fetch_raw,
    identity_findings,
    should_skip,
)

REPO, COMMIT, PATH = "o/r", "a" * 40, "tests/x.py"


class _Reply:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _script(*steps: Any) -> tuple[Callable[..., _Reply], list[str]]:
    """An opener that plays `steps` in order: an int raises that HTTP status, bytes succeed."""
    calls: list[str] = []
    queue = list(steps)

    def opener(url: str, timeout: float) -> _Reply:
        calls.append(url)
        step = queue.pop(0)
        if isinstance(step, bytes):
            return _Reply(step)
        if isinstance(step, int):
            raise urllib.error.HTTPError(url, step, "x", {}, io.BytesIO(b""))
        raise step

    return opener, calls


def _fetch(*steps: Any) -> tuple[Fetched, int, list[float]]:
    opener, calls = _script(*steps)
    sleeps: list[float] = []
    return fetch_raw(REPO, COMMIT, PATH, opener=opener, sleep=sleeps.append), len(calls), sleeps


def test_a_transient_status_is_retried_then_succeeds() -> None:
    for status in (503, 500, 429, 408):
        got, calls, sleeps = _fetch(status, b"data")
        assert (got, calls, len(sleeps)) == (Fetched("ok", b"data"), 2, 1), status


def test_a_persistent_transient_failure_is_unreachable_after_bounded_retries() -> None:
    got, calls, sleeps = _fetch(503, 503, 503)
    assert got.status == "unreachable" and calls == 3 and len(sleeps) == 2
    assert max(sleeps) <= 2, "the backoff is short"
    dropped = _fetch(urllib.error.URLError("down"), TimeoutError(), ConnectionError())
    assert dropped[0].status == "unreachable" and dropped[1] == 3


def test_a_missing_file_is_a_mismatch_and_is_not_retried() -> None:
    for status in (404, 410):
        got, calls, _ = _fetch(status)
        assert (got.status, calls) == ("missing", 1), status


def test_other_failures_are_unreachable_without_retry() -> None:
    got, calls, _ = _fetch(403)
    assert (got.status, calls) == ("unreachable", 1)


def test_findings_name_each_outcome_and_skip_only_when_offline_outside_ci() -> None:
    local = b"core\n"
    sha = hashlib.sha256(local).hexdigest()
    assert identity_findings(local, sha, Fetched("ok", local)) == []
    assert identity_findings(local, sha, Fetched("ok", b"other\n")) == [
        "local copy differs from the other repository's copy at the recorded commit"
    ]
    assert "no such file" in identity_findings(local, sha, Fetched("missing"))[0]
    assert "could not be fetched" in identity_findings(local, sha, Fetched("unreachable"))[0]
    assert identity_findings(local, "0" * 64, Fetched("ok", local)) == [
        "local copy differs from the sha256 in the ledger"
    ]
    assert should_skip(Fetched("unreachable"), {}) and not should_skip(
        Fetched("unreachable"), {"CI": "true"}
    )
    assert not should_skip(Fetched("missing"), {}) and not should_skip(Fetched("ok", b""), {})


def test_a_one_sided_edit_with_a_recomputed_hash_still_fails() -> None:
    local = b"core\n"
    edited = local + b"# edit\n"
    rehashed = hashlib.sha256(edited).hexdigest()
    assert identity_findings(edited, rehashed, Fetched("ok", local)) == [
        "local copy differs from the other repository's copy at the recorded commit"
    ]
