"""Identity of a file two repositories keep byte-identical: fetch the other repository's copy at a
recorded commit and compare it with the local bytes and the local ledger hash.

Repository-neutral. A 404 or 410 means the other repository has no such file at that commit, which
is a mismatch. A 408, 429 or 5xx, or a dropped connection, is retried with a short backoff; if it
still fails the result is "could not be fetched", never "differs". Callers skip that outcome only
when the network is down and `CI` is unset (`should_skip`), so a hosted run cannot pass without
the comparison.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MISSING_STATUS = {404, 410}
RETRY_STATUS = {408, 429}
TRIES = 3


@dataclass(frozen=True)
class Fetched:
    status: str  # "ok", "missing" or "unreachable"
    data: bytes = b""


def raw_url(repo: str, commit: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"


def fetch_raw(
    repo: str,
    commit: str,
    path: str,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    tries: int = TRIES,
) -> Fetched:
    """The file at `commit` in `repo`, retrying transient failures up to `tries` times."""
    open_url = opener or urllib.request.build_opener().open
    for attempt in range(tries):
        try:
            with open_url(raw_url(repo, commit, path), timeout=20) as response:
                return Fetched("ok", bytes(response.read()))
        except urllib.error.HTTPError as err:
            if err.code in MISSING_STATUS:
                return Fetched("missing")
            if err.code not in RETRY_STATUS and err.code < 500:
                return Fetched("unreachable")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        if attempt + 1 < tries:
            sleep(0.5 * 2**attempt)
    return Fetched("unreachable")


def identity_findings(local: bytes, ledger_sha256: str, fetched: Fetched) -> list[str]:
    """Guarded twice: against the local ledger hash, and against the other repository's bytes.
    Editing the file and recomputing the ledger hash still fails the second."""
    found = []
    if hashlib.sha256(local).hexdigest() != ledger_sha256:
        found.append("local copy differs from the sha256 in the ledger")
    if fetched.status == "missing":
        found.append("the other repository has no such file at the recorded commit")
    elif fetched.status == "unreachable":
        found.append("the other repository's copy could not be fetched")
    elif fetched.data != local:
        found.append("local copy differs from the other repository's copy at the recorded commit")
    return found


def should_skip(fetched: Fetched, env: Mapping[str, str]) -> bool:
    """Skip only when the network is down and this is not a CI run."""
    return fetched.status == "unreachable" and not env.get("CI")
