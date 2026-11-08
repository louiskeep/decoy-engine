"""Restricted date kernels: ``%Y %m %d`` plus separator literals, no locale, no inference.

Python's ``strptime`` accepts a one-digit ``%Y`` and ``strftime('%Y')`` is not zero-padded on every
platform, and both consult locale-dependent tables for names. This grammar is purely numeric, so
the lexical shape is checked with a compiled pattern and only the calendar check is delegated to
``datetime.date`` (documented: https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior).
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from typing import Any

from decoy_engine import reasons
from decoy_engine.contracts.declared_casting import DateIso, date_pattern
from decoy_engine.declared_casting._types import CastOutcome, fail, succeed

_R = reasons.runtime
ISO_FORMAT = "%Y-%m-%d"
_GROUP = {"%Y": "(?P<Y>[0-9]{4})", "%m": "(?P<m>[0-9]{2})", "%d": "(?P<d>[0-9]{2})"}


@lru_cache(maxsize=64)
def _compiled(fmt: str) -> re.Pattern[str]:
    return re.compile("".join(_GROUP.get(p) or re.escape(p) for p in date_pattern(fmt)), re.ASCII)


def _format_of(spec: Any) -> str:
    return ISO_FORMAT if isinstance(spec, DateIso) else spec.format


def parse_date(spec: Any, raw: Any) -> CastOutcome:
    if not isinstance(raw, str) or len(raw) > 32:
        return fail(_R.DECLARED_DATE_INVALID)
    m = _compiled(_format_of(spec)).fullmatch(raw)
    if m is None:  # guard:date_shape
        return fail(_R.DECLARED_DATE_INVALID)
    try:
        return succeed(date(int(m.group("Y")), int(m.group("m")), int(m.group("d"))))
    except ValueError:
        return fail(_R.DECLARED_DATE_INVALID)


def format_date(spec: Any, value: Any) -> CastOutcome:
    # datetime is a date subclass, but a timestamp carries semantics V1 does not declare.
    if type(value) is not date:
        return fail(_R.DECLARED_DATE_INVALID)
    fields = {"%Y": f"{value.year:04d}", "%m": f"{value.month:02d}", "%d": f"{value.day:02d}"}
    return succeed("".join(fields.get(p, p) for p in date_pattern(_format_of(spec))))
