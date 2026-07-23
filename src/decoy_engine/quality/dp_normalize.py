"""Recordwise normalization for the DP fit (DPS Scope B).

Extracted from `quality/dp.py` to keep that module under the 600-LOC
orchestration cap, following the `dp_schedule.py`/`dp_ledger.py`
precedent. This is a cohesive unit rather than an arbitrary slice: it is
the whole boundary between caller-supplied row content and the values
OpenDP ever sees.

Two invariants, and the second is the one that keeps getting missed.

TOTALITY. Each input row contributes at most one element to the
normalized vector, and no row's content can make normalization raise,
warn, or otherwise become an observable. Fit success is itself an
observable, so a value that made one frame raise where its one-row
neighbour succeeded would break (epsilon, delta) for any delta < 1
before a single released number was considered. Three separate review
rounds found defects of exactly that shape here, so prefer widening a
guard over narrowing one.

BOXING INVARIANCE. The label is a function of the value MODULO EVERY
BOXING PANDAS CAN APPLY. Totality is necessary and is not sufficient,
and rounds 6, 7 and 8 each shipped a guard that was total and not
boxing-invariant: `str()` moved under the int64->float64 upcast, the
integral-real fast path moved above 2**53, and the type gate itself
evaluated `isinstance` on the BOX, so `numpy.bool_` failed a `bool`
check and a whole column dropped. A guard that decides anything from a
value's Python TYPE is suspect, because pandas chooses that type from
the whole column's contents: bool<->float, `numpy.bool_`<->`bool`, and
real<->complex all cross this module's gate on ordinary neighbours.
Canonicalize the scalar first (`_unbox`), then decide.

The corollary that cost a round: "an allowlist can only over-drop" is
FALSE. Over-dropping is safe only when it is uniform across every boxing
of the same value, which a type-keyed allowlist is not.

THE DOMAIN, stated because totality cannot be absolute. The guards below
catch `BaseException`, not `Exception`, and drop the row: Codex round 10
showed a cell whose `__float__` raised a direct `BaseException` subclass
escaping an `Exception` guard and aborting the fit, which is the same
probability-0-versus-1 channel by another route. Two exceptions are
re-raised rather than dropped, `KeyboardInterrupt` and `SystemExit`, so
that an operator can still interrupt a fit and a caller can still exit
the process. A cell that raises either of those FROM ITS OWN dunders is
therefore outside the domain and is the one residual case.

For completeness, the `BaseException` enumeration is: `KeyboardInterrupt`
and `SystemExit` are re-raised everywhere. `GeneratorExit` is NOT re-raised
in `_cells`'s fetch guard -- one raised by an array's `__getitem__` is
dropped there like any other fetch failure; conformance comes instead from
keeping `_cells`'s `yield` OUTSIDE every guard, so a finalization
`GeneratorExit` propagates from the yield point and the generator stays
conforming (swallowing it there would make CPython raise `RuntimeError:
generator ignored GeneratorExit`). `asyncio.CancelledError` is currently
swallowed -- inert on this synchronous call path, and listed so the next
reader does not have to rediscover the set.

That residual is a caller precondition, not a defect we can close, and
it is narrow: reaching it requires a live Python object carrying
executable behaviour in a frame cell, OR a `Series` subclass whose own
`array`/`__len__` runs caller code that depends on the rows (the frame's
container is then as live as a cell would be). No file-based ingestion
path produces either -- Parquet, CSV, JSON and Arrow all yield values in
a plain `Series`, and the compiler reads plans with `yaml.safe_load` and
has no pickle path -- so a caller who can place one is already executing
code in this process and has strictly greater capability than the
channel provides. This is the same shape as the parse-time dtype
precondition in `docs/what-we-cannot-prove.md`: what happens before a
frame exists is the caller's, and everything reachable from a plain
frame built out of real data is ours. For such an out-of-domain frame we
make no claim at all: a container whose setup runs content-dependent code
makes the fit RAISE (fail loud), which is the honest disposition, because
silently reading no rows would emit an artifact claiming the column is
almost entirely null. The guarantee does not extend to a frame that is
not data, and no plain frame from a file reaches this.
"""

from __future__ import annotations

import datetime
import decimal
import math
import numbers
import warnings
from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd


def _unbox(raw: Any) -> Any:
    """Strip the numpy scalar box, leaving the value it holds.

    dennis round 8 (BLOCKER-2): the type gate was evaluated on the BOX
    rather than on the value, and pandas chooses the box. `numpy.bool_`
    is neither `bool` nor `numbers.Real` (both `isinstance` checks are
    False), so every row of a nullable `boolean` column failed the gate
    and dropped, releasing an artifact that asserted a fully populated
    column was 100% null. No adversarial neighbour was needed: the same
    parquet file read with `dtype_backend="numpy_nullable"` released
    nothing while the default backend released a full distribution, and
    any caller running `convert_dtypes()` upstream hit it.

    `.item()` maps every numpy scalar to its Python equivalent
    (`bool_`->`bool`, `str_`->`str`, `float32`->`float`, `int64`->`int`,
    `complex128`->`complex`), so the gate sees the value in whatever box
    pandas happened to choose.

    `.item()` is TYPE-ERASING, not gate-preserving, and callers must
    reject datetimelike values BEFORE calling it (round 9, both
    reviewers). An earlier version of this docstring claimed
    `datetime64`/`timedelta64` "unbox to objects that still fail the
    gate". That is false at nanosecond resolution, which is pandas'
    native one:

        unit  dt64.item() ->                  td64.item() ->
        s     datetime.datetime(2020, 1, 1)   datetime.timedelta(seconds=5)
        us    datetime.datetime(2020, 1, 1)   datetime.timedelta(microseconds=5)
        ns    int 1577836800000000000         int 5

    An `int` passes the real-number gate, so a date column was labelled
    with its raw epoch-nanosecond integers -- releasing the timestamps
    themselves -- while the same values boxed as `pandas.Timestamp` were
    correctly dropped. One added row moved ~801 labels.

    This is why `.item()` was the wrong generalization of the two
    special cases it replaced: it erases "this is a time quantity" into
    `int` for exactly the resolution pandas uses, and the gate cannot
    see what it can no longer distinguish.
    """
    if isinstance(raw, np.generic):
        return raw.item()
    return raw


def _is_datetimelike(raw: Any) -> bool:
    """A time quantity has no categorical label and no numeric value.

    Checked BEFORE any unboxing, so the answer cannot depend on the
    resolution pandas chose (see `_unbox`). `pandas.Timestamp` and
    `pandas.Timedelta` subclass the `datetime` types; `numpy.datetime64`
    and `numpy.timedelta64` are caught by their dtype kind, as
    `_is_complex` catches complex widths. Callers run this inside their
    conversion guard, so a value whose `dtype` access misbehaves drops
    the row rather than escaping.
    """
    if isinstance(raw, (datetime.date, datetime.time, datetime.timedelta)):
        return True
    return getattr(getattr(raw, "dtype", None), "kind", None) in ("M", "m")


def _is_complex(raw: Any) -> bool:
    """A complex value is unconvertible, whatever its width.

    `isinstance(raw, complex)` alone is not that test. `numpy.complex128`
    subclasses Python's `complex` and so is caught by it, but
    `numpy.complex64` does not, and `float()` on one silently returns the
    real part instead of failing. That is the "silently keep a real part"
    half of C-B2, still open for the narrower width. The dtype-kind check
    covers every numpy complex width, including any this build has not
    seen. Callers run this inside their conversion guard, so a value
    whose `dtype` access misbehaves drops the row rather than escaping.
    """
    if isinstance(raw, complex):
        return True
    return getattr(getattr(raw, "dtype", None), "kind", None) == "c"


def _is_container(raw: Any) -> bool:
    """A cell holding a SEQUENCE is not a scalar, whatever its length.

    Codex round 12 (BLOCKER): an Arrow `list<int64>` column arrives cell by
    cell as a Python `list`, which `float()` rejects, so the row drops. Add
    one null and pandas widens the column to `object`, reboxing every
    existing cell as a length-1 `ndarray` -- and `float(np.array([1]))`
    SUCCEEDS, returning the sole element. The same logical value therefore
    dropped in one boxing and converted in the other, distance N on an
    N-row column, while OpenDP is certified with `map(1)`. The categorical
    path already dropped both boxings at its type gate; the numeric path
    only calls `float()`, so it needed this.

    `numpy.ndim` is the boxing-invariant test: 0 for every scalar this
    module accepts (Python and numpy numbers, `str`, `bytes`, `Decimal`,
    `Fraction`, `complex`), and >= 1 for a `list`, `tuple` or multi-element
    `ndarray` at any length. It does NOT catch every non-scalar: a 0-d
    `ndarray` is `ndim == 0` and passes here -- correctly, since it is a
    scalar `float()` converts (Codex round 13), and stably under every
    boxing. Non-array containers (`set`, `dict`, `frozenset`) are also
    `ndim == 0` and pass here, then drop one step later at `float()`,
    uniformly across boxings. So this test reaches nothing pandas can turn a
    scalar INTO, and no real value is dropped by it; the sequence boxings
    that motivated it (`list`/`tuple`/`ndarray`) are exactly the ones it
    catches. Callers run this inside their conversion guard, so a value
    whose length access misbehaves drops the row rather than escaping.
    """
    return np.ndim(raw) != 0


def _normalize_numeric(series: pd.Series, *, lower: float, upper: float) -> list[float]:
    """Total, recordwise projection (guide section 4.2/7.1): every row
    contributes at most one element. Conversion failures and NaN become
    null (excluded); +-inf clamp to the declared bound; finite
    out-of-domain values clamp into [lower, upper]. Content can never
    raise -- there is no kind-selection branch here, only clamping.

    C-B2 (Codex round-3 blocker): this used to call the vectorized
    `pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)`. Codex
    demonstrated two content-dependent side channels in that path: (1) a
    column containing a Python/numpy `complex` value deterministically
    emits `ComplexWarning` on the cast to real, while an otherwise-
    identical neighbour without one emits no warning at all -- an
    observable with probability 0 on one neighbour and 1 on the other,
    which alone violates any (epsilon, delta) guarantee with delta < 1,
    independent of anything the fit releases; (2) `pd.to_numeric`'s
    object/complex coercion can silently produce garbage floats (an
    uninitialized buffer reinterpretation, not a `NaN`) instead of
    treating the value as a null conversion failure. Both close the same
    way: convert element-by-element with a blanket warning suppression
    around the whole pass, so NO warning is ever emitted regardless of
    content (not a per-type special case -- any future warning-emitting
    input closes the same way), and explicitly treat any `complex` value
    (covers the builtin type and `numpy.complex128`, which subclasses it)
    as an unconvertible failure before calling `float()`, so it becomes
    null like any other unconvertible value rather than silently keeping
    a real part.

    C-B4 (Codex round-4 blocker): the `except` here used to name
    `(TypeError, ValueError)`, which made the "content can never raise"
    claim above false. `float(10**10000)` raises `OverflowError`, so a
    one-row neighbour carrying a large Python int made the whole fit
    raise instead of emitting an artifact. Fit success/failure is itself
    an observable, and one with probability 0 on one neighbour and 1 on
    the other breaks (epsilon, delta) for any delta < 1 before a single
    released number is considered. Totality is the invariant, so the
    handler is now the totality itself rather than a list of the
    conversion errors we happened to think of: ANY failure converting a
    row's value drops that row, exactly as an unconvertible value
    already did. Each row still contributes at most one element, so
    add-or-remove-one-row stability is unchanged."""
    out: list[float] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for raw in _cells(series):
            try:
                # No `_unbox` here, deliberately. This path has no type
                # gate -- it only calls `float()`, which is total over
                # numpy scalars, and `_is_complex` reads the numpy dtype
                # directly. Unboxing is inert here: the original evidence
                # was run under a fetch that pre-unboxed numpy scalars and
                # so no longer supports the claim, but dennis round 10
                # re-established it under the current fetch (1222 frames x
                # 3 domains, zero differences). Inert code that no test can
                # falsify is a liability, so it stays out.
                # Anything that adds a type gate here must add `_unbox`
                # with it, or it reintroduces the nullable-boolean drop.
                if _is_container(raw):
                    # A list/ndarray cell is not a scalar; drop it whatever
                    # its boxing (Codex round 12). Must precede `float()`,
                    # which accepts a length-1 ndarray.
                    continue
                if _is_datetimelike(raw):
                    # float(np.datetime64(...,'ns')) SUCCEEDS and returns
                    # the epoch integer, so this path needs its own
                    # rejection -- `_unbox` would not have helped.
                    continue
                if _is_complex(raw):
                    # dennis round 8 (BLOCKER-4): dropping every complex
                    # was recordwise but not coercion-invariant. ONE
                    # complex row re-types the whole column to
                    # complex128, so every previously-real value arrived
                    # boxed as complex and dropped too -- the released
                    # bin_counts went from [.., 400, .., 400, ..] to all
                    # zeros on a one-row neighbour. A real that was
                    # re-typed keeps its value in the real part, so it
                    # must still convert; only a nonzero imaginary part
                    # is genuinely unconvertible.
                    if raw.imag != 0:
                        continue
                    raw = raw.real
                v = float(raw)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:  # broad by design: totality, see THE DOMAIN above
                continue
            if math.isnan(v):
                continue
            if math.isinf(v):
                v = upper if v > 0 else lower
            else:
                v = min(max(v, lower), upper)
            out.append(v)
    return out


def _cells(series: pd.Series) -> Iterator[Any]:
    """One cell at a time, with the FETCH itself inside the guard.

    `for raw in series` puts the per-cell fetch in the loop header, where
    no `try` can reach it, and the fetch is not free of content. A
    `pd.ArrowDtype` column's `__iter__` calls `as_py()` per element, which
    raises `OverflowError` whenever the stored integer leaves the
    corresponding Python `datetime`/`date`/`time`/`timedelta` range -- so
    ONE out-of-range row made the whole fit raise, which is a fit-success
    observable with probability 0 on one neighbour and 1 on the other
    (dennis round 10, BLOCKER-1). Affects `timestamp[s|ms|us]`,
    `duration[s|ms]`, `date32`, `date64` and `time64[us]`, both signs;
    `[ns]` is the one safe resolution, because int64 nanoseconds cannot
    leave the Python range. Note that this is the exact inverse of round
    9, where `ns` was the only UNSAFE resolution -- a resolution axis
    established on the numpy side does not transfer to the arrow side.

    Positional access, not one `try` around the whole loop: a single
    guard would drop the tail of the column after the first bad cell,
    which is not recordwise, and an `__iter__` that has raised cannot be
    resumed anyway. A cell whose fetch fails is dropped, the same
    disposition an unconvertible value already gets, and the same one the
    identical logical value receives under a numpy or object boxing where
    it arrives as a `pd.Timestamp` and is dropped by `_is_datetimelike`.

    THE BOX CHANGED, and callers of this helper must know it. For a
    numpy-backed Series, `Series.__iter__` is `map(self._values.item,
    range(size))` and returns PYTHON NATIVES; `series.array[i]` returns
    NUMPY SCALARS. dennis round 10 measured the difference on 11 of 31
    representations: `int64`, `float64`, `float16`, `float32`,
    `complex128`, `complex64`, `uint64`, `int8`, `bytes_S`, `categorical`
    and `bool` -- and that last one is `np.True_`, which is exactly the
    round-8 BLOCKER-2 value that is neither `bool` nor `numbers.Real`.
    Behaviourally this is inert (2040 frames across both normalizers, zero
    disposition differences) because `_canonical_label` unboxes and the
    numeric path only calls `float()`. It makes the warning below STRONGER,
    not weaker: before this change a naive type gate would have appeared to
    work on numpy-backed columns and failed only on extension dtypes, and
    now it fails on numpy-backed columns too.

    THE COST, measured so it is a known number rather than a surprise on
    the next benchmark: end-to-end normalizer throughput is 1.3x-3.0x
    slower than the old fetch (fetch alone is 4.8x-12.1x; the loop body
    absorbs most of it), landing at roughly 100k-520k rows per second
    depending on representation and path. Multi-chunk arrow does not
    degrade superlinearly -- 1 to 1000 chunks all sit in the same band --
    so there is no quadratic landmine here. The cheaper shape, guarding
    only extension arrays and keeping `map(ndarray.item, ...)` for
    numpy-backed columns, is deliberately NOT taken: it reintroduces a
    backend-dependent code path, which is the defect class that produced
    six of the last ten review rounds.
    """
    # Setup is deliberately NOT guarded. `series.array` and `len(series)`
    # never raise for any real dtype -- verified across every arrow width
    # and temporal resolution, huge decimals, list/struct arrows, sparse,
    # interval, period, datetimetz, categorical and empty -- so no in-domain
    # frame reaches an exception here, and there is no fit-success channel to
    # guard against. A `Series` subclass whose `array`/`__len__` runs
    # content-dependent caller code CAN raise, but such a frame is out of
    # domain (its container is as live as an object in a cell, see THE DOMAIN
    # above). The honest disposition there is to FAIL LOUD: an earlier round
    # guarded this and returned [], which emits an artifact asserting the
    # column is ~100% null -- a lying release, and an unbounded multiset
    # difference from its one-row neighbour dressed up as success (dennis and
    # Codex both, round 12). Propagating is also consistent with how a
    # cell-dunder interrupt is handled one screen down: loud, not swallowed.
    values = series.array
    count = len(series)

    for i in range(count):
        try:
            cell = values[i]
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # broad by design: totality, see THE DOMAIN above
            continue
        # The `yield` is deliberately OUTSIDE the guard. Codex round 11:
        # with both inside one `try`, a `GeneratorExit` arriving from the
        # FETCH was indistinguishable from generator finalization, and the
        # re-raise added for the latter aborted the fit for the former --
        # so an ExtensionArray raising `GeneratorExit` from `__getitem__`
        # took the whole fit down while the docstring promised one dropped
        # row. Splitting them lets a fetch-raised `GeneratorExit` drop the
        # row like any other fetch failure, while a finalization
        # `GeneratorExit` still propagates from the `yield` and keeps this
        # generator conforming (swallowing it there makes CPython raise
        # `RuntimeError: generator ignored GeneratorExit`, which is what
        # dennis round 11 caught when the guard was widened).
        yield cell


def _canonical_label(raw: Any) -> str:
    """The label for a categorical cell, as a function of the VALUE and
    not of the column's storage width.

    Codex round 6, the one defect in this program that broke the DP
    guarantee itself rather than a test or an error contract. `str(raw)`
    alone is NOT stable under add-or-remove-one-row adjacency, because
    pandas upcasts an integer column to float64 the moment a null enters
    it, and every cell's `str()` changes with it:

        D  = ints 0..7            -> ["0" ... "7"]
        D' = ints 0..7 and a null -> ["0.0" ... "7.0"]

    Adding ONE row replaced EVERY label. The two normalized multisets
    differ by 16 elements while the grouped-count measurement certifies
    `map(1)`, so the composed `(epsilon, delta)` understated the true
    sensitivity by a factor of the column's cardinality. Normalization
    was recordwise GIVEN A FRAME, but a frame's storage dtype is a
    function of ALL its rows, so the per-row map was not really per-row.
    Five review rounds missed it because every recordwise test used
    string fixtures, which never upcast.

    Rendering an integral real as its integer string was the round-6
    answer, and it was not enough. It canonicalizes the value AS THE
    FRAME PRESENTS IT, but the upcast has already destroyed the value
    for any magnitude above 2**53: the `Integral` path rendered the
    exact int on one side while the `Real` path rendered the rounded
    float64 image on the other, so neighbours disagreed on every such
    cell. A 1200-row frame of three 19-digit IDs released three labels
    at 400 each; adding one null row released one label at 1200, a
    multiset distance of 2400 against a `map(1)` certificate. Bigint
    account and snowflake IDs declared categorical are exactly the case.

    The label is therefore a function of the value's FLOAT64 IMAGE, not
    of its storage width, which is what makes it survive the upcast: both
    sides agree because both sides ask the same question of the same
    lossy image. Distinct large ints merging into one label is a
    coarsening, so it can only weaken a release, matching the disposition
    already accepted for `7`/`7.0`. Integers too large for float64 at all
    cannot live in an `int64` column, so pandas holds them in `object`
    dtype, which never upcasts; rendering those from the exact int is
    stable for the same reason.

    Only `str`, `bool`, and reals are labelled. Every other type is
    REJECTED to the caller's guard, which drops the row -- deliberately
    not an error. Dropping is both total and coercion-invariant, and it
    is the only disposition that is: a timedelta drops identically
    whether pandas boxes it as `pandas.Timedelta` in a `timedelta64`
    column or as `datetime.timedelta` after one incompatible row forces
    `object`, whereas LABELLING it changes "1 days 00:00:00" to
    "1 day, 0:00:00" across that same coercion and moves every existing
    label. Raising instead would reopen the fit-success channel this
    module exists to close: an all-string frame would succeed where its
    one-row neighbour carrying a timedelta raised, which is a
    probability-0-vs-1 observable and breaks (epsilon, delta) for any
    delta < 1 before a single released number is considered.

    `bool` gets NO special case, and the round-7 rationale for giving it
    one was exactly backwards (dennis round 8, BLOCKER-1). That rationale
    said bool must render "True"/"False" because it is an `int` subclass
    that "would otherwise render 1/0". Rendering "1"/"0" is the
    coercion-invariant answer and "True"/"False" is the unstable one: a
    bool column's float64 image IS 1.0/0.0, and pandas re-types bool to
    int64, float64, or object-of-int on the most ordinary neighbours
    there are -- concatenating a partition whose flag column is all-null,
    or one holding ints. Measured distance was 1600 on an 800-row column,
    from one added row. Bools now render from their image like every
    other real, and the resulting `True`/`1` label collision is a
    coarsening, which can only weaken a release.

    Non-finite floats render from the image too, so `inf` stays "inf".
    """
    # No container check here, deliberately. A list or ndarray cell already
    # fails the `numbers.Real`/`str` gate below and drops under every boxing
    # (Codex round 12 verified both a Python `list` and a length-1 `ndarray`
    # drop), so an explicit check would be inert -- and this module does not
    # keep inert code no test can falsify. The numeric path is different: it
    # calls `float()`, which ACCEPTS a length-1 ndarray, so its container
    # check is load-bearing and has one.
    if _is_datetimelike(raw):
        raise TypeError("datetimelike categorical value")
    raw = _unbox(raw)
    if isinstance(raw, str):
        return raw
    if _is_complex(raw):
        # A real that one complex row re-typed to complex128 keeps its
        # value in the real part; a genuinely complex value has no label.
        if raw.imag != 0:
            raise TypeError("complex categorical value with a nonzero imaginary part")
        raw = raw.real
    if not isinstance(raw, (numbers.Real, decimal.Decimal)):
        raise TypeError(f"unsupported categorical value type: {type(raw).__name__}")
    try:
        as_float = float(raw)
    except OverflowError:
        return str(raw)
    if math.isfinite(as_float) and as_float == int(as_float):
        return str(int(as_float))
    return str(as_float)


def _normalize_categorical(series: pd.Series) -> list[str]:
    """Total, recordwise projection: nulls excluded per row (uniform
    across None/NaN/NaT/pd.NA), every remaining scalar mapped through
    `_canonical_label`, which labels `str`/`bool`/reals and rejects every
    other type to the conversion guard below, dropping that row. Both
    departures from the original `Series.dropna()`-plus-`str()` shape are
    load-bearing and are explained at their guards below. Wrapped in
    the same blanket warning suppression as `_normalize_numeric` (C-B2):
    `str()` on an exotic scalar type is not known to warn today, but
    suppression here costs nothing and keeps both normalizers under the
    same "no warning ever, regardless of content" invariant.

    C-B4 (Codex round-4 blocker): `str()` was assumed total and called
    bare. It is not. CPython caps integer-to-string conversion at 4300
    digits, so `str(10**10000)` raises `ValueError`, and a one-row
    neighbour carrying such a value made the entire fit raise rather
    than emit an artifact -- the same probability-0-vs-1 fit-success
    channel closed in `_normalize_numeric`. Any failure rendering a
    row's value now drops that row, which is what an unrepresentable
    label should have meant all along; each row still contributes at
    most one element.

    Totality is defined against what the release boundary can consume,
    not merely against `str()` returning. A lone surrogate such as
    `"\\ud800"` is a perfectly good Python `str` that cannot be encoded
    as UTF-8, and it raised `UnicodeEncodeError` further downstream when
    the label crossed into OpenDP -- the same fit-success channel, just
    relocated. Labels are therefore required to be UTF-8 encodable here.
    A label containing NUL is dropped for the neighbouring reason: it
    does not raise at the boundary, it TRUNCATES there, silently merging
    distinct source values into one released label.
    `str.isascii()` is a cached flag check, so the overwhelmingly common
    all-ASCII label pays no encoding cost.

    Null exclusion is per row rather than a vectorized `Series.dropna()`
    for the same reason. Deciding nullness runs the value's own dunders,
    so it too can raise on content: `pd.isna(Decimal("sNaN"))` raises
    `InvalidOperation`, which took the whole fit down from outside the
    conversion guard. A row whose null check cannot be evaluated is
    treated as present and left to the conversion step, which is total.
    For an array-valued cell `pd.isna` returns an ARRAY of per-element
    verdicts rather than a verdict about the cell, so only a genuine
    scalar result (`ndim == 0`) is allowed to exclude a row. Testing
    `bool()` alone is not that check: it raises for a multi-element array
    but for a SINGLETON container it silently returns that one element's
    verdict, so container cells were being handled differently BY LENGTH
    (Codex round 5).

    That guard is now BELT-AND-BRACES rather than load-bearing, and is
    kept deliberately rather than by oversight (dennis round 8, LOW-2).
    Since round 7 a container drops at the type gate whether or not the
    null check excluded it first, so removing the `ndim` check produces
    zero behavioural difference across 17 container and scalar cell
    shapes. It stays because it is the only thing keeping the null step
    itself honest if the type gate ever widens.

    No equivalence to `dropna()` is claimed. Round 5 asserted one and it
    was false, and round 7 widened the gap deliberately. Dropping is
    recordwise and can only coarsen a release, so the DP claim is
    unaffected either way; what matters is that the disposition is
    uniform and does not depend on how pandas is storing the column."""
    out: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for raw in _cells(series):
            try:
                null = pd.isna(raw)
                if getattr(null, "ndim", 0) == 0 and bool(null):
                    continue
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:  # broad by design: totality, see THE DOMAIN above
                # Reachable, and pinned (dennis + Codex round 13). An earlier
                # note here claimed this guard was unreachable because
                # `pd.isna` classifies by identity and NaN-membership without
                # invoking a cell's dunders. That was false: `pd.isna` calls a
                # cell's `__array__`, so a cell whose `__array__` raises routes
                # a `BaseException` -- including a `KeyboardInterrupt` -- through
                # this line, and `pd.isna(Decimal("sNaN"))` raising
                # `InvalidOperation` reaches it too. The re-raise above is
                # therefore load-bearing (an operator's Ctrl-C during a hostile
                # `__array__` must still propagate, not be swallowed into a
                # dropped row), on a par with the label guard one screen down.
                # `test_an_interrupt_from_the_null_check_still_propagates` pins
                # it; a mutant narrowing the breadth or dropping the re-raise
                # now fails.
                pass
            try:
                label = _canonical_label(raw)
                if "\x00" in label:
                    # dennis round 10 (MEDIUM-1): the label is truncated
                    # at the first NUL when it crosses into OpenDP, so
                    # "a\x00b", "a" and "a\x00c" all released as "a" --
                    # three distinct source values reported as one. Not a
                    # DP break (a many-to-one label map only coarsens,
                    # and it is recordwise and boxing-invariant), but a
                    # silent truncation at the release boundary that made
                    # the artifact assert a `top_values` entry that is
                    # not a value in the source. Same class as the
                    # surrogate case below, one step short of it.
                    continue
                if not label.isascii():
                    label.encode("utf-8")
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:  # broad by design: totality, see THE DOMAIN above
                continue
            out.append(label)
    return out
