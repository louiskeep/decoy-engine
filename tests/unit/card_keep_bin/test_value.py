"""keep_bin at the value layer: the kept prefix, the tweak binding, the fail-closed shapes,
the backstop for every path that cannot honour it, and the dark guarantee."""

from __future__ import annotations

import hashlib
import random
import re

import pytest

from decoy_engine import checksums
from decoy_engine.errors import FpeChecksumError, FpeUnencryptableError
from decoy_engine.transforms.fpe import (
    FF1_TWEAK_SCOPE_COLUMN,
    FF1_TWEAK_SCOPE_JOIN_GROUP,
    _permute,
    build_ff1_tweak,
    fpe_decrypt_value,
    fpe_encrypt_value,
)

from ._pans import DIGITS, KEY, TWEAK, digits_of, luhn, pan


def enc(value: str, keep_bin: int, *, tweak: bytes = TWEAK, sep: bool = True) -> str:
    return fpe_encrypt_value(value, KEY, DIGITS, tweak, sep, False, "luhn", keep_bin=keep_bin)


def dec(value: str, keep_bin: int, *, tweak: bytes = TWEAK) -> str:
    return fpe_decrypt_value(value, KEY, DIGITS, tweak, True, False, "luhn", keep_bin=keep_bin)


# Every length FF1 can take: 13 to 19 digits with 6 kept, 15 to 19 with 8.
_SHAPES = [(k, n) for k in (6, 8) for n in range(k + 7, 20)]


@pytest.mark.parametrize(("keep_bin", "length"), _SHAPES)
def test_prefix_length_and_luhn_hold_and_decrypt_inverts(keep_bin: int, length: int) -> None:
    rng = random.Random(length * 10 + keep_bin)
    for _ in range(40):
        source = pan(rng, length)
        out = enc(source, keep_bin)
        assert len(out) == length
        assert out[:keep_bin] == source[:keep_bin]
        assert checksums.validate("luhn", out)
        assert dec(out, keep_bin) == source


def test_separators_stay_in_place_and_do_not_count_toward_the_prefix() -> None:
    source = "4539 5787 6362 1486"
    out = enc(source, 6)
    assert [i for i, ch in enumerate(out) if ch == " "] == [4, 9, 14]
    assert digits_of(out)[:6] == "453957"
    assert out[:7] == "4539 57"
    assert checksums.validate("luhn", digits_of(out))
    assert dec(out, 6) == source
    assert digits_of(out) == enc(digits_of(source), 6)


def test_distinct_inputs_give_distinct_outputs() -> None:
    rng = random.Random(7)
    one_bin = {pan(rng, 16, "453957") for _ in range(5000)}
    assert len({enc(p, 6) for p in one_bin}) == len(one_bin)
    spread = {pan(rng, 16) for _ in range(5000)}
    assert len({enc(p, 6) for p in spread}) == len(spread)


def test_the_kept_prefix_is_bound_into_the_tweak() -> None:
    rng = random.Random(11)
    same = 0
    for _ in range(200):
        middle = "".join(str(rng.randint(0, 9)) for _ in range(9))
        a = luhn("453957" + middle)
        b = luhn("222100" + middle)
        same += enc(a, 6)[6:15] == enc(b, 6)[6:15]
        # An independent recomputation of the construction.
        expected = _permute(middle, KEY, DIGITS, TWEAK + b"453957", forward=True)
        assert enc(a, 6) == luhn("453957" + expected)
    assert same <= 1


def test_deterministic_and_column_or_group_scoped() -> None:
    source = "4539578763621486"
    assert enc(source, 6) == enc(source, 6)
    other_column = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, "card2")
    group = build_ff1_tweak(FF1_TWEAK_SCOPE_JOIN_GROUP, "card")
    assert enc(source, 6, tweak=other_column) != enc(source, 6)
    assert enc(source, 6, tweak=group) != enc(source, 6)
    assert enc(source, 8) != enc(source, 6)


# Pinned known answers: any change to the construction (split, tweak, Luhn) shows here.
_VECTORS = [
    (6, "4539578763621486", "4539573085550042"),
    (6, "4539 5787 6362 1486", "4539 5730 8555 0042"),
    (6, "378282246310005", "378282454829803"),
    (6, "4539570000000004", "4539579178662438"),
    (8, "4539578763621486", "4539578736588911"),
    (8, "6011000990139424", "6011000969302763"),
    (8, "353011133330001", "353011133819961"),
    (6, "6221260000000000019", "6221262057435026033"),
    (6, "4222222222222", "4222223315793"),
]


@pytest.mark.parametrize(("keep_bin", "source", "expected"), _VECTORS)
def test_pinned_vectors(keep_bin: int, source: str, expected: str) -> None:
    assert enc(source, keep_bin) == expected
    assert dec(expected, keep_bin) == source


# --- fail closed --------------------------------------------------------------------------


def _assert_value_free(exc: BaseException, source: str) -> None:
    text = str(exc) + repr(exc.args)
    for run in re.findall(r"\d{4,}", source):
        for i in range(len(run) - 3):
            assert run[i : i + 4] not in text
    assert exc.__cause__ is None
    assert exc.__context__ is None


@pytest.mark.parametrize(("keep_bin", "length"), [(6, 12), (8, 14), (8, 13)])
def test_too_few_encrypted_digits_fail_closed_on_the_domain_floor(
    keep_bin: int, length: int
) -> None:
    source = pan(random.Random(length), length)
    with pytest.raises(FpeUnencryptableError) as info:
        enc(source, keep_bin)
    assert info.value.code == "fpe.unencryptable_domain"
    assert "keep_bin" in str(info.value)
    _assert_value_free(info.value, source)


def test_more_than_nineteen_digits_fail_closed() -> None:
    source = pan(random.Random(20), 20)
    with pytest.raises(FpeChecksumError) as info:
        enc(source, 6)
    assert info.value.code == "fpe.checksum_unsupported"
    assert "19" in str(info.value)
    _assert_value_free(info.value, source)


def test_luhn_invalid_source_fails_closed() -> None:
    source = "4539578763621487"
    with pytest.raises(FpeChecksumError) as info:
        enc(source, 6)
    assert info.value.code == "fpe.checksum_invalid_source"


def test_separators_without_preserve_fail_closed() -> None:
    with pytest.raises(FpeUnencryptableError):
        enc("4539 5787 6362 1486", 6, sep=False)


# --- value-layer backstop -----------------------------------------------------------------

_PAN = "4539578763621486"


@pytest.mark.parametrize("forward", [True, False], ids=["encrypt", "decrypt"])
@pytest.mark.parametrize(
    ("keep_bin", "validate_luhn", "checksum"),
    [
        (6, False, None),
        (6, True, None),
        (6, False, "npi"),
        (8, False, "ean13"),
        (7, False, "luhn"),
        (True, False, "luhn"),
        (-6, False, "luhn"),
    ],
    ids=["plain", "validate_luhn", "npi", "ean13", "seven", "bool", "negative"],
)
def test_keep_bin_is_never_silently_ignored(
    keep_bin: int, validate_luhn: bool, checksum: str | None, forward: bool
) -> None:
    value = _PAN if checksum != "npi" else "1234567893"
    call = fpe_encrypt_value if forward else fpe_decrypt_value
    with pytest.raises(FpeChecksumError) as info:
        call(value, KEY, DIGITS, TWEAK, True, validate_luhn, checksum, keep_bin=keep_bin)
    _assert_value_free(info.value, value)


def test_zero_is_off_at_the_value_layer() -> None:
    plain = fpe_encrypt_value(_PAN, KEY, DIGITS, TWEAK, True, False, "luhn")
    assert fpe_encrypt_value(_PAN, KEY, DIGITS, TWEAK, True, False, "luhn", keep_bin=0) == plain


# --- dark: configs without keep_bin are byte-identical to the base tree -------------------

# Captured on engine main dd31ea91, before this change.
_DARK_DIGEST = "df9bc1eb770590cee5327975929701f36cb86c1d0381d131d71650467e2daaee"


def test_existing_modes_are_byte_identical_to_main() -> None:
    rng = random.Random(20261107)

    def legacy_pan(n: int) -> str:
        body = str(rng.randint(1, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(n - 2))
        return body + checksums.calc_check_digit("luhn", body)

    pans = [legacy_pan(rng.choice([13, 15, 16, 19])) for _ in range(200)]
    npis: list[str] = []
    while len(npis) < 50:
        b = "1" + "".join(str(rng.randint(0, 9)) for _ in range(8))
        npis.append(b + checksums.calc_check_digit("npi", b))
    plain = [f"{rng.randint(0, 10**9 - 1):09d}-{rng.randint(0, 9999):04d}" for _ in range(100)]
    out = [fpe_encrypt_value(p, KEY, DIGITS, TWEAK, True, False, "luhn") for p in pans]
    out += [
        fpe_encrypt_value(p[:4] + " " + p[4:], KEY, DIGITS, TWEAK, True, False, "luhn")
        for p in pans[:50]
    ]
    out += [fpe_encrypt_value(p, KEY, DIGITS, TWEAK, True, False, "npi") for p in npis]
    out += [fpe_encrypt_value(p, KEY, DIGITS, TWEAK, True, True, None) for p in pans[:50]]
    out += [fpe_encrypt_value(p, KEY, DIGITS, TWEAK, True, False, None) for p in plain]
    assert hashlib.sha256("\n".join(out).encode()).hexdigest() == _DARK_DIGEST
