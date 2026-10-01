"""Column shapes for the B1 rev9 profile-equivalence test (acceptance test 20).

The 28 shapes of the plan author's `rev91_probe_arrowprofile.py` (long and short
text with and without nulls, `large_string`, dictionaries, all-null columns,
every integer width, `uint64` above 2^53, floats with NaN and signed zero,
booleans, dates, timestamps with and without a zone and with `-2^63`,
durations, `time64`, decimal and binary), then the Arrow types of acceptance
tests 3 and 15 that pandas can convert.
"""

from __future__ import annotations

import datetime as dt
import decimal
import random
import string

import pyarrow as pa

from tests.native._rev9_support import I64MIN, SHAPES

N = 60


def _cases() -> dict[str, pa.Array]:
    rnd = random.Random(7)

    def word(n: int) -> str:
        return "".join(rnd.choice(string.ascii_letters + " éß名") for _ in range(n))

    payload = [word(rnd.randint(50, 120)) if i % 7 else None for i in range(N)]
    filled = [p or "filler " * 9 + str(i) for i, p in enumerate(payload)]
    cases: dict[str, pa.Array] = {
        "payload_long": pa.array(payload),
        "payload_large": pa.array(payload, pa.large_string()),
        "payload_nonull": pa.array(filled),
        "dict_long_nonull": pa.array(filled).dictionary_encode(),
        "dict_allnull": pa.array([None] * N, pa.string()).dictionary_encode(),
        "dict_int": pa.DictionaryArray.from_arrays(
            pa.array([i % 2 if i % 3 else None for i in range(N)], pa.int8()), pa.array([10, 20])
        ),
        "empty_str": pa.array(["" for _ in range(N)]),
        "short_str": pa.array([rnd.choice(["a", "bb", None]) for _ in range(N)]),
        "all_null_str": pa.array([None] * N, pa.string()),
        "int64_nonull": pa.array(list(range(N))),
        "int64_null": pa.array([i if i % 5 else None for i in range(N)]),
        "uint64_big": pa.array(
            [2**64 - 1 - (i % 3) if i % 4 else None for i in range(N)], pa.uint64()
        ),
        "int8": pa.array([i % 100 for i in range(N)], pa.int8()),
        "float64": pa.array(
            [
                float("nan")
                if i % 9 == 0
                else (-0.0 if i % 9 == 1 else (0.0 if i % 9 == 2 else i / 3))
                if i % 11
                else None
                for i in range(N)
            ]
        ),
        "float32": pa.array([i / 7 if i % 3 else None for i in range(N)], pa.float32()),
        "bool_null": pa.array([None if i % 4 == 0 else bool(i % 2) for i in range(N)]),
        "bool": pa.array([bool(i % 2) for i in range(N)]),
        "date32": pa.array(
            [dt.date(2000, 1, 1) + dt.timedelta(days=i % 13) if i % 6 else None for i in range(N)]
        ),
        "ts_us_tz": pa.array(
            [i * 10**6 if i % 6 else None for i in range(N)],
            pa.timestamp("us", "America/New_York"),
        ),
        "ts_s": pa.array([i if i % 6 else None for i in range(N)], pa.timestamp("s")),
        "ts_ns_min": pa.array([I64MIN if i % 10 == 0 else i for i in range(N)], pa.timestamp("ns")),
        "dur_ms_min": pa.array(
            [I64MIN if i % 10 == 0 else (i % 7) for i in range(N)], pa.duration("ms")
        ),
        "dur_us": pa.array([i if i % 6 else None for i in range(N)], pa.duration("us")),
        "time64us": pa.array([i if i % 6 else None for i in range(N)], pa.time64("us")),
        "decimal": pa.array(
            [decimal.Decimal(i % 9) / 4 if i % 6 else None for i in range(N)], pa.decimal128(9, 2)
        ),
        "binary": pa.array([bytes([i % 5]) * 3 if i % 6 else None for i in range(N)], pa.binary()),
        "dict_str": pa.array(
            [rnd.choice(["x", "y", "zz", None]) for _ in range(N)]
        ).dictionary_encode(),
        "dict_long": pa.array(payload).dictionary_encode(),
    }
    # Acceptance test 3 types beyond the ones above.
    for width in ("int16", "int32", "uint8", "uint16", "uint32"):
        cases[width] = pa.array([i % 50 if i % 8 else None for i in range(N)], getattr(pa, width)())
        cases[f"{width}_nonull"] = pa.array([i % 50 for i in range(N)], getattr(pa, width)())
    cases["dict_int32_idx"] = (
        pa.array(["u", "v", None, "w"] * 15)
        .dictionary_encode()
        .cast(pa.dictionary(pa.int32(), pa.string()))
    )
    cases["decimal_38_5"] = pa.array(
        [decimal.Decimal(i) / 100 if i % 6 else None for i in range(N)], pa.decimal128(38, 5)
    )
    cases["date64"] = pa.array([i * 86400000 if i % 6 else None for i in range(N)], pa.date64())
    cases["ts_ms_tz"] = pa.array(
        [i * 1000 if i % 6 else None for i in range(N)], pa.timestamp("ms", "+05:30")
    )
    cases["duration_ns"] = pa.array([i if i % 6 else None for i in range(N)], pa.duration("ns"))
    return cases


CASES: dict[str, pa.Array] = _cases()

# The test 15 shapes pandas converts and profiles (`public` is "exact" or "altered").
for _shape in SHAPES:
    if _shape.public in ("exact", "altered"):
        CASES[f"shape_{_shape.name}"] = pa.concat_arrays([_shape.bad] * (N // 3))


def case_ids() -> list[str]:
    return list(CASES)


def long_text_cases() -> set[str]:
    """Cases whose real profile raises an HC-7 free-text advisory (see the control test)."""
    return {"payload_nonull", "dict_long_nonull"}
