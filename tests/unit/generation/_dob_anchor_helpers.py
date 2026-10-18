"""Shared helpers for the dob-anchor tests (masking golden + clock simulation)."""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from decoy_engine.generation.composite._person import CompositePerson
from decoy_engine.providers_v2._adapter import ProviderSpec

GOLDEN_PATH = (
    Path(__file__).resolve().parents[2] / "fixtures" / "dob_anchor" / "composite_person_masking.json"
)

_SEED = (0x0123456789).to_bytes(8, "big")


def masking_spec() -> ProviderSpec:
    return ProviderSpec(locale="en_US", deterministic=True, namespace="dobgold", seed=_SEED, extra={})


def run_composite_person_masking() -> dict[str, list[str | None]]:
    """The composite_person masking strategy on a fixed dob-bearing config (fresh pools)."""
    gen = CompositePerson(coherent_namespace="dobgold", pool_size=300)
    source = pd.Series(["alice", "bob", None, "carol", "alice", "dave", "erin", "frank"])
    out = gen.generate_bundle(masking_spec(), len(source), source=source, deterministic=True)
    return {
        k: [None if v is pd.NA else str(v) for v in out[k].tolist()] for k in sorted(out)
    }


@contextmanager
def simulated_system_date(day: _dt.date) -> Iterator[None]:
    """Make Faker's date_time provider read `day` as 'now' (what a different calendar day does)."""
    from faker.providers import date_time

    class _Dt(_dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            base = _dt.datetime(day.year, day.month, day.day, 12, 0, 0)
            return base.replace(tzinfo=tz) if tz is not None else base

        @classmethod
        def today(cls) -> Any:
            return _dt.datetime(day.year, day.month, day.day, 12, 0, 0)

    class _D(_dt.date):
        @classmethod
        def today(cls) -> Any:
            return day

    saved = {n: getattr(date_time, n) for n in ("datetime", "dtdate")}
    date_time.datetime = _Dt  # type: ignore[attr-defined]
    date_time.dtdate = _D  # type: ignore[attr-defined]
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(date_time, n, v)
