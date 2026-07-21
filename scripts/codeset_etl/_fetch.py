"""HTTPS download primitive for the codeset ETL pipeline (HC-1 slice 2).

One function, ``fetch_url``, used by every parser. Kept tiny and isolated so
``update.update_corpus`` can inject a fake in tests (``fetch_fn=...``) and
the unit suite never makes a real network call -- the slice-2 spec requires
the update flow's tests to mock the network, and this is the one seam that
needs mocking.

Standard-library only (``urllib.request``): no new runtime dependency for a
single-purpose GET. Scheme is restricted to ``https`` before opening --
avoids ruff S310 (arbitrary-scheme url open) by construction, not by
suppressing the check, and rules out an accidental ``file://`` / ``ftp://``
read from a caller-supplied URL.
"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

from ._errors import CodesetFetchError

#: Generous but bounded: catches an accidental infinite-hang against a stalled
#: host without needing per-source tuning (the NDC zip, at ~11MB over a normal
#: connection, finishes in low single-digit seconds).
_DEFAULT_TIMEOUT_S = 120.0

_USER_AGENT = "decoy-engine-codeset-etl/1 (+https://github.com/louiskeep/decoy-engine)"


def fetch_url(url: str, *, timeout: float = _DEFAULT_TIMEOUT_S) -> bytes:
    """Download *url* and return its raw bytes.

    Args:
        url: Source URL. Must be ``https://`` -- fail-closed on any other
            scheme (including plain ``http://``) rather than silently
            downgrading transport security for a public-data pull.
        timeout: Socket timeout in seconds, passed to ``urlopen``.

    Raises:
        CodesetFetchError: Non-``https`` scheme, network/timeout failure, or
            a non-2xx HTTP status. Never returns a partial/truncated
            response silently -- ``urlopen`` reading the full body via
            ``.read()`` either succeeds completely or raises.
    """
    scheme = urlparse(url).scheme
    if scheme != "https":
        raise CodesetFetchError(f"refusing to fetch non-https URL (scheme {scheme!r}): {url}")

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()
    except OSError as exc:
        # Covers urllib.error.URLError/HTTPError (both OSError subclasses)
        # and raw socket timeouts -- one typed, fail-closed error for every
        # network-layer failure mode instead of leaking urllib internals.
        raise CodesetFetchError(f"failed to fetch {url}: {exc}") from exc
