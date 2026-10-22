"""Child process for the pinned-generation determinism tests.

argv: config_json_path reference_date seed days_offset. Prints one JSON line:
{"fingerprint": ..., "digest": ..., "tables": [...]}. A non-zero days_offset shifts the wall
clock the process sees (time.time and datetime.now), so a clock read would change the output.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sys
import time

config_path, reference_date, seed, days = (
    sys.argv[1],
    sys.argv[2],
    int(sys.argv[3]),
    int(sys.argv[4]),
)
shift = days * 86400

import numpy  # noqa: E402,F401
import pandas  # noqa: E402,F401
import pyarrow  # noqa: E402,F401

from decoy_engine.generation.pinned import (  # noqa: E402
    compile_pinned_plan,
    execute_pinned,
    fingerprint,
)

# Patched after the C extensions have imported: they bind datetime at import time.
if shift:
    _real_time, _real_ns = time.time, time.time_ns
    time.time = lambda: _real_time() + shift  # type: ignore[assignment]
    time.time_ns = lambda: _real_ns() + shift * 10**9  # type: ignore[assignment]

    class _Shifted(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return super().now(tz) + _dt.timedelta(seconds=shift)

        @classmethod
        def utcnow(cls):  # type: ignore[no-untyped-def]
            return super().utcnow() + _dt.timedelta(seconds=shift)

        @classmethod
        def today(cls):  # type: ignore[no-untyped-def]
            return super().today() + _dt.timedelta(seconds=shift)

    _dt.datetime = _Shifted  # type: ignore[misc]


with open(config_path, encoding="utf-8") as fh:
    config = json.load(fh)
plan = compile_pinned_plan(config, reference_date)
out = execute_pinned(plan, seed)
h = hashlib.sha256()
for name, table in out.items():
    h.update(name.encode())
    for col in table.column_names:
        h.update(col.encode())
        h.update(repr(table.column(col).to_pylist()).encode())
print(json.dumps({"fingerprint": fingerprint(plan), "digest": h.hexdigest(), "tables": list(out)}))
