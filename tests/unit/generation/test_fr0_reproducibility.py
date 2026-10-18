"""Phase F FR0: reproducibility eligibility is a third axis, closed over dependencies.

A column is excluded when its output is not a deterministic function of the pinned envelope, and
so is anything that reads an excluded column. The exclusion holds on the streamed core and on the
legacy path alike, and an excluded column never carries a reproducibility assertion.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.generation import _reproducibility as rep
from decoy_engine.generation._admission import classify_plan
from decoy_engine.internal.faker_setup import register_faker_provider, unregister_faker_provider
from tests.parity import _f0_fixtures as fx
from tests.parity._fr0_support import prepared_config, run_resident


def _excl(cfg: dict[str, Any], **kw: Any) -> dict[tuple[str, str], str]:
    return rep.reproducibility_exclusions(prepared_config(cfg), **kw)


def _run(cfg: dict[str, Any]) -> dict[str, pa.Table]:
    return run_resident(cfg)


def test_fresh_column_and_its_dependents_are_excluded() -> None:
    cfg = fx.config(
        [
            fx.table(
                "t",
                6,
                [
                    {"name": "k", "type": "sequence", "start": 1},
                    {"name": "f", "type": "sequence", "start": 1, "determinism": "fresh"},
                    {
                        "name": "dep",
                        "type": "formula",
                        "formula": "f'{f}-x'",
                        "references": ["f"],
                    },
                    {"name": "d2", "type": "derived", "expression": "dep + 1"},
                    {"name": "agg", "type": "derived_aggregate", "op": "count", "column": "d2"},
                    {"name": "ok", "type": "categorical", "categories": ["a", "b"]},
                ],
            )
        ]
    )
    assert _excl(cfg) == {
        ("t", "f"): rep.REASON_FRESH,
        ("t", "dep"): rep.REASON_DEPENDS,
        ("t", "d2"): rep.REASON_DEPENDS,
        ("t", "agg"): rep.REASON_DEPENDS,
    }


def test_cross_table_reference_to_an_excluded_column_is_excluded() -> None:
    cfg = fx.config(
        [
            fx.table(
                "c",
                5,
                [
                    {
                        "name": "r",
                        "type": "reference",
                        "reference_table": "p",
                        "reference_column": "k",
                    },
                    {"name": "n", "type": "sequence"},
                ],
            ),
            fx.table("p", 5, [{"name": "k", "type": "sequence", "determinism": "fresh"}]),
        ]
    )
    assert _excl(cfg) == {("p", "k"): rep.REASON_FRESH, ("c", "r"): rep.REASON_DEPENDS}


@pytest.fixture
def stateful_provider() -> Iterator[str]:
    counter = itertools.count()
    register_faker_provider("fr0_stateful", lambda fake: f"v{next(counter)}")
    yield "fr0_stateful"
    unregister_faker_provider("fr0_stateful")


def test_stateful_custom_provider_is_excluded_and_really_is_not_reproducible(
    stateful_provider: str,
) -> None:
    cfg = fx.config(
        [
            fx.table(
                "t",
                8,
                [
                    {"name": "p", "type": "faker", "faker_type": stateful_provider},
                    {"name": "seq", "type": "sequence"},
                ],
            )
        ]
    )
    assert _excl(cfg) == {("t", "p"): rep.REASON_CUSTOM_PROVIDER}
    assert not classify_plan(prepared_config(cfg)).eligible  # legacy path
    first, second = _run(cfg), _run(cfg)
    assert first["t"].column("p").to_pylist() != second["t"].column("p").to_pylist()
    assert first["t"].column("seq").equals(second["t"].column("seq"))


def test_fresh_column_is_not_reproducible_but_the_rest_is_on_the_legacy_path() -> None:
    cfg = fx.config(
        [
            fx.table(
                "t",
                20,
                [
                    {
                        "name": "f",
                        "type": "categorical",
                        "categories": list("abcdefgh"),
                        "determinism": "fresh",
                    },
                    {"name": "dep", "type": "formula", "formula": "f'{f}!'", "references": ["f"]},
                    {"name": "keep", "type": "categorical", "categories": list("abcdefgh")},
                ],
            )
        ]
    )
    excluded = _excl(cfg)
    assert set(excluded) == {("t", "f"), ("t", "dep")}
    first, second = _run(cfg), _run(cfg)
    for name in ("f", "dep"):
        assert first["t"].column(name).to_pylist() != second["t"].column(name).to_pylist()
    assert first["t"].column("keep").equals(second["t"].column("keep"))


def test_clock_dependent_provider_is_excluded_on_the_streamed_core_without_a_clock() -> None:
    """`date_between` is semantically eligible (a built-in string provider) so it streams, but it
    reads the wall clock: excluded unless the envelope records a controlled clock."""
    cfg = fx.config(
        [
            fx.table("p", 4, [{"name": "d", "type": "faker", "faker_type": "date_between"}]),
            fx.table(
                "c",
                4,
                [
                    {
                        "name": "r",
                        "type": "reference",
                        "reference_table": "p",
                        "reference_column": "d",
                    },
                    {"name": "n", "type": "sequence"},
                ],
            ),
        ]
    )
    assert classify_plan(prepared_config(cfg)).eligible  # the streamed core, not legacy
    assert _excl(cfg) == {("p", "d"): rep.REASON_CLOCK, ("c", "r"): rep.REASON_DEPENDS}
    assert _excl(cfg, controlled_clock=True) == {}


def test_clock_helper_in_a_formula_is_excluded_without_a_clock() -> None:
    cfg = fx.config(
        [fx.table("t", 3, [{"name": "a", "type": "formula", "formula": "days_from_now(3)"}])]
    )
    assert _excl(cfg) == {("t", "a"): rep.REASON_CLOCK}
    assert _excl(cfg, controlled_clock=True) == {}


def test_date_of_birth_is_anchored_not_clock_dependent() -> None:
    assert not rep.is_clock_dependent_faker_type("date_of_birth")
    for t in ("date_between", "date_time", "past_date", "future_datetime", "date_this_year"):
        assert rep.is_clock_dependent_faker_type(t), t


def test_deterministic_columns_are_never_excluded() -> None:
    cfg = fx.cfg_live_faker()
    assert _excl(cfg, controlled_clock=True) == {}


def test_resolved_locale_and_clock_are_part_of_the_envelope() -> None:
    cfg = fx.config([fx.table("t", 3, [{"name": "c", "type": "faker", "faker_type": "city"}])])
    config = prepared_config(cfg)
    default = rep.build_envelope(config)
    fr = rep.build_envelope(config, instance_default_locale="fr_FR")
    assert default.column_locales == (("t", "c", "default"),)
    assert fr.column_locales == (("t", "c", "fr_FR"),)
    assert default.digest() != fr.digest()
    pinned = rep.build_envelope(config, controlled_clock="2026-10-16T12:00:00")
    assert pinned.controlled_clock and pinned.digest() != default.digest()
    # A column locale wins over the instance default, so the instance default is not an input there.
    own = fx.config(
        [
            fx.table(
                "t", 3, [{"name": "c", "type": "faker", "faker_type": "city", "locale": "de_DE"}]
            )
        ]
    )
    a = rep.build_envelope(prepared_config(own), instance_default_locale="fr_FR")
    assert a.column_locales == (("t", "c", "de_DE"),)


def test_envelope_names_the_engine_minor_version() -> None:
    cfg = prepared_config(fx.cfg_sequence())
    assert rep.build_envelope(cfg).engine_minor == rep.engine_minor_version()
