"""Synthetic fail-control worker (plan_faker_determinism_harness_v2.md, C4).

`pool_determinism_worker.py` never depends on Python's string-hash
randomization (`PYTHONHASHSEED`), so every real candidate's digests agree
across the K subprocesses `check_determinism.py` spawns -- which is exactly
the property the whole harness exists to prove, and also exactly the
property that would let a broken driver silently report false positives
forever. This worker exists to make that failure mode observable: it builds
its "pool" by putting a fixed set of strings into a `set` and reading them
back with `list(...)`, an operation whose ORDER genuinely depends on
`PYTHONHASHSEED` for `str` elements. Two runs under different hash seeds
produce two different orderings and therefore two different digests.

Prints the SAME JSON shape `pool_determinism_worker.py` prints (same
`POOL_DETERMINISM_JSON` marker, same field names) so `check_determinism.py`
can point at either worker through one code path -- the driver's own tests
assert it reports this worker's candidates as DIVERGENT, proving the
determinism check can fail, not just pass.

Usage: python hash_order_probe_worker.py <faker_type> <locale|-> [kwargs_json]
(faker_type/locale/kwargs are accepted and echoed into meta for interface
parity with the real worker; the digested content ignores them.)
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from digest_codec import CODEC_VERSION, _pool_digest

_PROBE_STRINGS = (
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
)


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit("usage: hash_order_probe_worker.py <faker_type> <locale|-> [kwargs_json]")
    faker_type = sys.argv[1]
    requested_locale = None if sys.argv[2] == "-" else sys.argv[2]
    kwargs_raw = sys.argv[3] if len(sys.argv) == 4 else "{}"
    kwargs = json.loads(kwargs_raw)

    # The hash-seed-dependent step: set iteration order over str members is
    # a function of PYTHONHASHSEED. list(...) freezes that order into the
    # sequence _pool_digest then encodes positionally.
    hash_ordered = list(set(_PROBE_STRINGS))

    digest_hex = hashlib.sha256(_pool_digest(hash_ordered)).hexdigest()

    record: dict[str, Any] = {
        "pool_digest": digest_hex,
        "output_digest": digest_hex,
        "exact_name_available": True,
        "custom_override_present": False,
        "meta": {
            "worker": "hash_order_probe_worker",
            "requested_locale": requested_locale,
            "effective_locale": requested_locale,
            "resolved_locale": requested_locale,
            "faker_type": faker_type,
            "kwargs": kwargs,
            "codec_version": CODEC_VERSION,
        },
    }
    print("POOL_DETERMINISM_JSON " + json.dumps(record))


if __name__ == "__main__":
    main()
