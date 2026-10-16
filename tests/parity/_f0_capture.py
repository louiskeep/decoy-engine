"""Re-capture helper for `_f0_expected.py` (manual use only; no test imports this).

    ~/.cache/decoy-d1a-abi3-venv/bin/python -m tests.parity._f0_capture > /tmp/new_expected_body.py

Prints replacement ``EXPECTED`` and ``POOL_SEAM`` literals. Review the diff against
`_f0_expected.py` as a generation-behavior change before adopting any of it.
"""

from __future__ import annotations

import pprint

from tests.parity._f0_canonical import table_fingerprint
from tests.parity._f0_fixtures import FIXTURES_BY_ID, cfg_pooled_faker, run_generate_tables
from tests.parity._f0_seams import capture_pool_seam


def main() -> None:
    from tests.parity._f0_fixtures import FIXTURES

    captured = {
        f.fid: {name: table_fingerprint(tbl) for name, tbl in f.run().items()} for f in FIXTURES
    }
    assert FIXTURES_BY_ID
    log = capture_pool_seam(lambda: run_generate_tables(cfg_pooled_faker()))
    seam = [c.signature() for c in log.calls]
    print(
        "EXPECTED: dict[str, dict[str, dict[str, Any]]] = "
        + pprint.pformat(captured, width=98, sort_dicts=False)
    )
    print()
    print("POOL_SEAM: list[dict[str, Any]] = " + pprint.pformat(seam, width=98, sort_dicts=False))


if __name__ == "__main__":
    main()
