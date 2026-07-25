"""Cross-version pandas compatibility helpers.

CONTRACT (audit M5, 2026-06-12): dtype labels emitted into persisted
payloads (ColumnProfile, distribution snapshots) feed user-held snapshot
digests, so they must not drift when the pandas version changes. pandas
3 made ``str`` the default inferred string dtype where pandas 1.5-2.x
inferred ``object``; reporting the new label verbatim silently broke
every pre-existing baseline digest.
"""

from __future__ import annotations

import re
from typing import Any

# pandas-3 default datetime inference resolution is 'us' where 1.5-2.x
# always produced 'ns'. Resolution is uniform within one pandas version,
# so cross-frame drift comparison loses nothing by pinning the label.
_DATETIME_RES = re.compile(r"^datetime64\[(s|ms|us|ns)(, .+)?\]$")


def canonical_dtype_label(dtype: Any) -> str:
    """Stable dtype label across pandas major versions.

    Two pandas-3 inference changes are normalized to their historical
    labels so digests stay byte-stable across the 2.x -> 3.x boundary:
    the DEFAULT inferred string dtype (``str`` -> ``object``) and the
    default datetime resolution (``datetime64[us]`` ->
    ``datetime64[ns]``, timezone suffix preserved). Explicitly-requested
    extension dtypes (``string``, ``string[pyarrow]``, ...) pass through
    unchanged, as do all other dtypes.
    """
    label = str(dtype)
    if label == "str":
        return "object"
    m = _DATETIME_RES.match(label)
    if m:
        return f"datetime64[ns{m.group(2) or ''}]"
    return label
