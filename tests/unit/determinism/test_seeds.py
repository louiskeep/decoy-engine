"""The seed helpers: one width check, one resolve rule, one fresh-seed draw."""

from __future__ import annotations

import pytest

from decoy_engine.determinism import fresh_seed, resolve_seed, seed_bytes_to_int


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
