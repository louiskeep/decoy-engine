"""Vectorized standard normal CDF and inverse CDF, without scipy.

Pattern: Cody ANORM and Wichura AS241 normal functions (Cody, Math. Comp. 23, 1969, as ACM TOMS
Algorithm 715; Wichura, Appl. Statist. 37, 1988). See: R nmath/pnorm.c and qnorm.c, CPython
statistics.NormalDist.inv_cdf.

`ndtr` is Cody's rational Chebyshev approximation in three regions, with R's split of
`exp(-z^2/2)` into `exp(-zr^2/2) * exp(-(z - zr)(z + zr)/2)`, `zr = trunc(16 z) / 16`, which keeps
the far tail accurate to a few ulps. Coefficients are R's (about 18 digits in exact arithmetic).

`ndtri` is AS241 PPND16 (about 16 digits), the algorithm of R's `qnorm` and CPython's
`NormalDist.inv_cdf`; coefficients are CPython's. It refuses probabilities outside the open
interval (0, 1): its callers only ever pass interior points, so an endpoint is a programmer error.

Both are elementwise numpy, so a value depends only on its own input, never on the array length.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

_A = (
    2.2352520354606839287,
    161.02823106855587881,
    1067.6894854603709582,
    18154.981253343561249,
    0.065682337918207449113,
)
_B = (47.20258190468824187, 976.09855173777669322, 10260.932208618978205, 45507.789335026729956)
_C = (
    0.39894151208813466764,
    8.8831497943883759412,
    93.506656132177855979,
    597.27027639480026226,
    2494.5375852903726711,
    6848.1904505362823326,
    11602.651437647350124,
    9842.7148383839780218,
    1.0765576773720192317e-8,
)
_D = (
    22.266688044328115691,
    235.38790178262499861,
    1519.377599407554805,
    6485.558298266760755,
    18615.571640885098091,
    34900.952721145977266,
    38912.003286093271411,
    19685.429676859990727,
)
_P = (
    0.21589853405795699,
    0.1274011611602473639,
    0.022235277870649807,
    0.001421619193227893466,
    2.9112874951168792e-5,
    0.02307344176494017303,
)
_Q = (
    1.28426009614491121,
    0.468238212480865118,
    0.0659881378689285515,
    0.00378239633202758244,
    7.29751555083966205e-5,
)

_SQRT_32 = 5.656854249492380195206754896838
_ONE_OVER_SQRT_2PI = 0.398942280401432677939946059934
_HALF_EPS = float(np.finfo(np.float64).eps) * 0.5
# Beyond these Cody's tail formula underflows (lower) or rounds to 1 (upper).
_LOWER_LIMIT = -38.4674
_UPPER_LIMIT = 8.2924


def _gaussian_tail(x: FloatArray, temp: FloatArray) -> FloatArray:
    """`exp(-x^2/2) * temp` with the exponent split so the rounding of `x^2` does not leak in."""
    xsq = np.trunc(x * 16.0) / 16.0
    delta = (x - xsq) * (x + xsq)
    return np.exp(-xsq * xsq * 0.5) * np.exp(-delta * 0.5) * temp


def ndtr(z: npt.ArrayLike) -> FloatArray:
    """Standard normal CDF, elementwise. NaN propagates; -inf -> 0, inf -> 1."""
    x = np.asarray(z, dtype=np.float64)
    out = np.empty_like(x)
    y = np.abs(x)

    central = y <= 0.67448975
    if central.any():
        xc = x[central]
        xsq = xc * xc
        tiny = np.abs(xc) <= _HALF_EPS
        xnum = _A[4] * xsq
        xden = xsq.copy()
        for i in range(3):
            xnum = (xnum + _A[i]) * xsq
            xden = (xden + _B[i]) * xsq
        xnum = np.where(tiny, 0.0, xnum)
        xden = np.where(tiny, 0.0, xden)
        out[central] = 0.5 + xc * (xnum + _A[3]) / (xden + _B[3])

    middle = (~central) & (y <= _SQRT_32)
    if middle.any():
        ym = y[middle]
        xnum = _C[8] * ym
        xden = ym.copy()
        for i in range(7):
            xnum = (xnum + _C[i]) * ym
            xden = (xden + _D[i]) * ym
        small = _gaussian_tail(ym, (xnum + _C[7]) / (xden + _D[7]))
        out[middle] = np.where(x[middle] > 0, 1.0 - small, small)

    tail = (~central) & (~middle) & (x > _LOWER_LIMIT) & (x < _UPPER_LIMIT)
    if tail.any():
        xt = x[tail]
        yt = y[tail]
        xsq = 1.0 / (xt * xt)
        xnum = _P[5] * xsq
        xden = xsq.copy()
        for i in range(4):
            xnum = (xnum + _P[i]) * xsq
            xden = (xden + _Q[i]) * xsq
        temp = xsq * (xnum + _P[4]) / (xden + _Q[4])
        small = _gaussian_tail(xt, (_ONE_OVER_SQRT_2PI - temp) / yt)
        out[tail] = np.where(xt > 0, 1.0 - small, small)

    rest = ~(central | middle | tail)
    if rest.any():
        xr = x[rest]
        out[rest] = np.where(np.isnan(xr), np.nan, np.where(xr > 0, 1.0, 0.0))
    return out


def ndtri(p: npt.ArrayLike) -> FloatArray:
    """Inverse standard normal CDF, elementwise, for `p` strictly inside (0, 1)."""
    prob = np.asarray(p, dtype=np.float64)
    if not bool(np.all((prob > 0.0) & (prob < 1.0))):
        raise ValueError("ndtri: every probability must lie strictly between 0 and 1.")
    q = prob - 0.5
    out = np.empty_like(prob)

    central = np.abs(q) <= 0.425
    if central.any():
        qc = q[central]
        r = 0.180625 - qc * qc
        num = (
            (
                (
                    (
                        (
                            (
                                (2.5090809287301226727e3 * r + 3.3430575583588128105e4) * r
                                + 6.7265770927008700853e4
                            )
                            * r
                            + 4.5921953931549871457e4
                        )
                        * r
                        + 1.3731693765509461125e4
                    )
                    * r
                    + 1.9715909503065514427e3
                )
                * r
                + 1.3314166789178437745e2
            )
            * r
            + 3.3871328727963666080e0
        ) * qc
        den = (
            (
                (
                    (
                        (
                            (5.2264952788528545610e3 * r + 2.8729085735721942674e4) * r
                            + 3.9307895800092710610e4
                        )
                        * r
                        + 2.1213794301586595867e4
                    )
                    * r
                    + 5.3941960214247511077e3
                )
                * r
                + 6.8718700749205790830e2
            )
            * r
            + 4.2313330701600911252e1
        ) * r + 1.0
        out[central] = num / den

    outer = ~central
    if outer.any():
        qo = q[outer]
        po = prob[outer]
        r = np.sqrt(-np.log(np.where(qo <= 0.0, po, 1.0 - po)))
        near = r <= 5.0
        x = np.empty_like(r)
        if near.any():
            s = r[near] - 1.6
            num = (
                (
                    (
                        (
                            (
                                (7.74545014278341407640e-4 * s + 2.27238449892691845833e-2) * s
                                + 2.41780725177450611770e-1
                            )
                            * s
                            + 1.27045825245236838258e0
                        )
                        * s
                        + 3.64784832476320460504e0
                    )
                    * s
                    + 5.76949722146069140550e0
                )
                * s
                + 4.63033784615654529590e0
            ) * s + 1.42343711074968357734e0
            den = (
                (
                    (
                        (
                            (
                                (1.05075007164441684324e-9 * s + 5.47593808499534494600e-4) * s
                                + 1.51986665636164571966e-2
                            )
                            * s
                            + 1.48103976427480074590e-1
                        )
                        * s
                        + 6.89767334985100004550e-1
                    )
                    * s
                    + 1.67638483018380384940e0
                )
                * s
                + 2.05319162663775882187e0
            ) * s + 1.0
            x[near] = num / den
        far = ~near
        if far.any():
            s = r[far] - 5.0
            num = (
                (
                    (
                        (
                            (
                                (2.01033439929228813265e-7 * s + 2.71155556874348757815e-5) * s
                                + 1.24266094738807843860e-3
                            )
                            * s
                            + 2.65321895265761230930e-2
                        )
                        * s
                        + 2.96560571828504891230e-1
                    )
                    * s
                    + 1.78482653991729133580e0
                )
                * s
                + 5.46378491116411436990e0
            ) * s + 6.65790464350110377720e0
            den = (
                (
                    (
                        (
                            (
                                (2.04426310338993978564e-15 * s + 1.42151175831644588870e-7) * s
                                + 1.84631831751005468180e-5
                            )
                            * s
                            + 7.86869131145613259100e-4
                        )
                        * s
                        + 1.48753612908506148525e-2
                    )
                    * s
                    + 1.36929880922735805310e-1
                )
                * s
                + 5.99832206555887937690e-1
            ) * s + 1.0
            x[far] = num / den
        out[outer] = np.where(qo < 0.0, -x, x)
    return out
