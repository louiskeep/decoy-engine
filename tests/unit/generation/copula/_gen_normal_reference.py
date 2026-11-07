"""Regenerate `_normal_reference.json`: high-precision normal CDF and inverse reference values.

Run by hand (`python tests/unit/generation/copula/_gen_normal_reference.py`); it needs `mpmath`,
which is not an engine dependency, so the tests read only the committed JSON. Values are computed
at 50 significant digits and stored as 30-digit decimal strings.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import NormalDist

import mpmath  # type: ignore[import-untyped]
import numpy as np

OUT = Path(__file__).with_name("_normal_reference.json")


def main() -> None:
    mpmath.mp.dps = 50
    zs = sorted(
        {float(v) for v in np.linspace(-37.5, 9.0, 451)}
        | {float(v) for v in np.linspace(-1.0, 1.0, 101)}
        | {-5.6568, -5.6569, -0.6744, -0.6745, 0.6744, 0.6745, 5.6568, 5.6569, 8.29, 8.3}
    )
    cdf = [[repr(z), mpmath.nstr(mpmath.ncdf(mpmath.mpf(z)), 30)] for z in zs]
    ps = sorted(
        {float(v) for v in np.logspace(-300, -1, 300)}
        | {float(v) for v in np.linspace(0.01, 0.99, 197)}
        | {float(1.0 - v) for v in np.logspace(-16, -1, 100)}
        | {0.075, 0.074999, 0.075001, 0.925, 0.5}
    )
    inv = []
    for p in ps:
        target = mpmath.mpf(p)
        start = NormalDist().inv_cdf(p)
        # Root of the exact CDF; erfinv(2p - 1) would lose the far tail to cancellation.
        z = mpmath.findroot(lambda t, target=target: mpmath.ncdf(t) - target, start)
        inv.append([repr(p), mpmath.nstr(z, 30)])
    OUT.write_text(json.dumps({"cdf": cdf, "inv": inv}, indent=0) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
