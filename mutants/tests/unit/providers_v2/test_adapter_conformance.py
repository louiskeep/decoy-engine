"""Shared adapter-conformance gate (engine-v2 S7).

Runs FakerAdapter, the 5 S6 DecoyNative identifier adapters, and (when
installed) MimesisAdapter through the same harness. Closes
MEDIUM-ADAPTER-CONFORMANCE-1: the conformance surface is now one place, so a
future adapter that skips the gate is caught here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._faker_adapter import FakerAdapter
from decoy_engine.providers_v2.identifiers import (
    EinAdapter,
    EinValidator,
    MrnAdapter,
    MrnValidator,
    NdcAdapter,
    NdcValidator,
    NpiAdapter,
    NpiValidator,
    SsnAdapter,
    SsnValidator,
)

from ._adapter_conformance import (
    ConformanceCase,
    assert_format_correct,
    assert_no_blocklist_leak,
    assert_seed_stable,
)

_EMAIL = re.compile(r"[^@]+@[^@]+\.[^@]+")
_COUNT = 64


def _faker_batch(provider: str) -> Callable[[bytes], Sequence[Any]]:
    def make(seed: bytes) -> Sequence[Any]:
        spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=seed)
        return FakerAdapter(locale="en_US").generate_batch(provider, spec=spec, count=_COUNT)

    return make


def _decoy_det(adapter: Any, provider: str) -> Callable[[bytes], Sequence[Any]]:
    def make(seed: bytes) -> Sequence[Any]:
        return [
            adapter.generate(
                provider,
                spec=ProviderSpec(
                    locale="en_US", deterministic=True, namespace="conf_ns", seed=seed
                ),
                source_value=f"conformance-src-{i}",
            )
            for i in range(_COUNT)
        ]

    return make


CASES: list[ConformanceCase] = [
    ConformanceCase("faker:person_first_name", _faker_batch("person_first_name")),
    ConformanceCase(
        "faker:person_email",
        _faker_batch("person_email"),
        validate=lambda v: bool(_EMAIL.fullmatch(str(v))),
    ),
    ConformanceCase(
        "decoy:synthetic_ssn",
        _decoy_det(SsnAdapter(), "synthetic_ssn"),
        validate=SsnValidator.is_valid,
        has_blocklist=True,
    ),
    ConformanceCase(
        "decoy:synthetic_ein",
        _decoy_det(EinAdapter(), "synthetic_ein"),
        validate=EinValidator.is_valid,
        has_blocklist=True,
    ),
    ConformanceCase(
        "decoy:synthetic_npi",
        _decoy_det(NpiAdapter(), "synthetic_npi"),
        validate=NpiValidator.is_valid,
        has_blocklist=True,
    ),
    ConformanceCase(
        "decoy:synthetic_ndc",
        _decoy_det(NdcAdapter(), "synthetic_ndc"),
        validate=NdcValidator.is_valid,
    ),
    ConformanceCase(
        "decoy:synthetic_mrn",
        _decoy_det(MrnAdapter(), "synthetic_mrn"),
        validate=MrnValidator.is_valid,
    ),
]

# MimesisAdapter joins the harness when the optional dep is installed. The
# candidate set runs through the gate even at zero adoption, so a future
# adoption cannot bypass conformance.
try:
    from decoy_engine.providers_v2.mimesis import MimesisAdapter

    def _mimesis_batch(provider: str) -> Callable[[bytes], Sequence[Any]]:
        def make(seed: bytes) -> Sequence[Any]:
            spec = ProviderSpec(locale="en_US", deterministic=False, namespace=None, seed=seed)
            return MimesisAdapter(locale="en_US").generate_batch(provider, spec=spec, count=_COUNT)

        return make

    CASES.extend(
        [
            ConformanceCase("mimesis:person_first_name", _mimesis_batch("person_first_name")),
            ConformanceCase(
                "mimesis:person_email",
                _mimesis_batch("person_email"),
                validate=lambda v: bool(_EMAIL.fullmatch(str(v))),
            ),
        ]
    )
except ImportError:
    pass


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.label)
def test_seed_stable(case: ConformanceCase) -> None:
    assert_seed_stable(case)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.label)
def test_format_correct(case: ConformanceCase) -> None:
    assert_format_correct(case)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.label)
def test_no_blocklist_leak(case: ConformanceCase) -> None:
    assert_no_blocklist_leak(case)
