### Changed (pre-scan key type families, POSTSCAN-STREAM PS-2b0, 2026-11-12)

Pre-scan now measures relationship keys that differ only in integer width (`int32` against `int64`,
signed against unsigned) or string encoding (`string`, `large_string`, dictionary of strings), and
reports exact counts instead of `key_type_mismatch`. Floats, decimals, dates, timestamps and integer
against string stay a mismatch. `decoy_engine.profiling.keys_comparable` answers the same question
from the schemas alone.
