"""The seed helpers: one width check, one resolve rule, one fresh-seed draw."""

from __future__ import annotations

import pytest

from decoy_engine import reasons
from decoy_engine.determinism import (
    COMPOSITE_SEED_WIDTHS,
    fresh_seed,
    resolve_seed,
    seed_bytes_to_int,
)


@pytest.mark.parametrize("width", [0, 7, 9, 32])
def test_seed_bytes_to_int_refuses_every_width_but_eight(width: int) -> None:
    raw = bytes(range(1, width + 1))
    with pytest.raises(ValueError) as exc:
        seed_bytes_to_int(raw)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert not raw or (str(raw) not in str(exc.value) and raw.hex() not in str(exc.value))


def test_seed_bytes_to_int_refuses_a_non_bytes_value() -> None:
    with pytest.raises(ValueError):
        seed_bytes_to_int(None)  # type: ignore[arg-type]


def test_seed_bytes_to_int_is_big_endian_on_eight_bytes() -> None:
    assert seed_bytes_to_int(bytes.fromhex("0000000000000102")) == 0x0102
    assert seed_bytes_to_int(bytes.fromhex("ff" * 8)) == 2**64 - 1


def test_resolve_seed_passes_eight_and_thirty_two_bytes_through_unchanged() -> None:
    eight, thirty_two = b"\x01" * 8, bytes(range(32))
    assert resolve_seed(eight) is eight
    assert resolve_seed(thirty_two) is thirty_two
    assert resolve_seed(resolve_seed(eight)) == eight


@pytest.mark.parametrize("width", [0, 4, 9, 16, 31, 33])
def test_resolve_seed_refuses_every_other_width(width: int) -> None:
    with pytest.raises(ValueError):
        resolve_seed(b"\x07" * width)


def test_resolve_seed_draws_one_fresh_eight_byte_seed_for_none() -> None:
    first, second = resolve_seed(None), resolve_seed(None)
    assert len(first) == len(second) == 8
    assert first != second
    assert first != b"\x00" * 8


def test_fresh_seed_is_eight_bytes_of_entropy() -> None:
    assert len(fresh_seed()) == 8
    assert fresh_seed() != fresh_seed()


@pytest.mark.parametrize("width", [8, 32])
def test_seed_bytes_to_int_composite_widths_equals_the_old_raw_expression(width: int) -> None:
    for raw in (bytes(range(1, width + 1)), b"\xff" * width, b"\x00" * (width - 1) + b"\x05"):
        assert seed_bytes_to_int(raw, widths=COMPOSITE_SEED_WIDTHS) == int.from_bytes(raw, "big")


@pytest.mark.parametrize("width", [0, 7, 9, 16, 31, 33])
def test_seed_bytes_to_int_composite_widths_refuses_every_other_width(width: int) -> None:
    raw = bytes(range(1, width + 1))
    with pytest.raises(ValueError) as exc:
        seed_bytes_to_int(raw, widths=COMPOSITE_SEED_WIDTHS)
    assert not raw or raw.hex() not in str(exc.value)


def test_seed_bytes_to_int_composite_widths_refuses_a_non_bytes_value() -> None:
    with pytest.raises(ValueError):
        seed_bytes_to_int(None, widths=COMPOSITE_SEED_WIDTHS)  # type: ignore[arg-type]


def test_seed_width_errors_carry_the_coded_reason() -> None:
    with pytest.raises(ValueError) as exc:
        seed_bytes_to_int(b"\x01" * 5)
    assert getattr(exc.value, "code", None) == reasons.runtime.INVALID_SEED_LENGTH


@pytest.mark.parametrize("width", [8, 32])
def test_native_composite_mirror_accepts_the_python_build_widths(width: int) -> None:
    import numpy as np

    from decoy_engine.execution.native._determinism_protocol import provider_for

    raw = bytes(range(1, width + 1))
    mirror = provider_for("gen.composite_build_pool").generator(raw)
    old = np.random.default_rng(int.from_bytes(raw, "big"))
    assert mirror.integers(0, 10**9, size=16).tolist() == old.integers(0, 10**9, size=16).tolist()


@pytest.mark.parametrize("width", [7, 16])
def test_native_composite_mirror_refuses_other_widths(width: int) -> None:
    from decoy_engine.execution.native._determinism_protocol import provider_for

    with pytest.raises(ValueError):
        provider_for("gen.composite_build_pool").generator(b"\x01" * width)


@pytest.mark.parametrize("site", ["gen.pool_nondeterministic", "gen.faker_pool_selection"])
def test_the_other_seeded_providers_stay_eight_bytes(site: str) -> None:
    from decoy_engine.execution.native._determinism_protocol import provider_for

    provider = provider_for(site)
    assert provider.seed_widths == (8,)
    with pytest.raises(ValueError):
        provider.generator(b"\x01" * 32)
