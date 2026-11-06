"""International check-digit schemes in decoy_engine.checksums.

Vectors are python-stdnum 2.2's documented module examples (the doctests in
each module's docstring); stdnum's validators are the oracle for the property
tests. The guard tests walk the real registry, so a scheme registered without
vectors or without both functions fails here.
"""

from __future__ import annotations

import random
import string

import pytest

import decoy_engine.checksums as checksums
from decoy_engine import reasons
from decoy_engine.errors import FpeChecksumError
from decoy_engine.plan._checks import check_fpe_checksum_scheme
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms._fpe_checksum import _fpe_checksum_permute

NATIONAL = (
    "gb_nhs",
    "ca_sin",
    "in_aadhaar",
    "au_tfn",
    "de_idnr",
    "fr_nir",
    "es_dni",
    "es_nie",
    "it_codice_fiscale",
)

# scheme -> (valid values, invalid values, (body, check) pairs)
VECTORS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]] = {
    "luhn": (("4532015112830366",), ("4532015112830360",), (("453201511283036", "6"),)),
    "npi": (("1234567893",), ("1234567890",), (("123456789", "3"),)),
    "iban": (("GB82WEST12345698765432",), ("GB82WEST12345698765433",), ()),
    "vin": (("1M8GDM9AXKP042788",), ("1M8GDM9A1KP042788",), ()),
    "isbn13": (("9780306406157",), ("9780306406158",), (("978030640615", "7"),)),
    "ean13": (("4006381333931",), ("4006381333932",), (("400638133393", "1"),)),
    "gtin": (("4006381333931",), ("4006381333932",), (("400638133393", "1"),)),
    "gb_nhs": (("943-476-5870", "9434765870"), ("9434765871",), (("943476587", "0"),)),
    "ca_sin": (
        ("123-456-782",),
        ("999-999-999", "12345678Z", "823456785"),
        (("12345678", "2"),),
    ),
    "in_aadhaar": (
        ("234123412346",),
        ("234123412347", "123412341234", "643343121", "222222222222"),
        (("23412341234", "6"),),
    ),
    "au_tfn": (("123 456 782",), ("999 999 999", "12345677"), (("12345678", "2"),)),
    "de_idnr": (
        ("36 574 261 809",),
        ("36574261890", "36554266806"),
        (("3657426180", "9"),),
    ),
    "fr_nir": (
        ("2 95 10 99 126 111 93", "253072B07300470", "253072A07300443"),
        ("295109912611199", "253072C07300443", "6546546546546703"),
        (("2951099126111", "93"), ("253072B073004", "70"), ("253072A073004", "43")),
    ),
    "es_dni": (("54362315-K",), ("54362315Z", "54362315"), (("54362315", "K"),)),
    "es_nie": (("x-2482300w",), ("x-2482300a", "X2482300"), (("X2482300", "W"),)),
    "it_codice_fiscale": (
        ("RCCMNL83S18D969H",),
        ("RCCMNL83S18D969", "00743110157"),
        (("RCCMNL83S18D969", "H"),),
    ),
}


@pytest.mark.parametrize("scheme", sorted(checksums._KNOWN_SCHEMES))
def test_every_scheme_has_vectors_and_both_functions(scheme: str) -> None:
    assert scheme in VECTORS, f"{scheme} is registered without test vectors"
    assert scheme in checksums._VALIDATORS
    assert scheme in checksums._CHECK_DIGIT_FUNCS
    valid, invalid, _ = VECTORS[scheme]
    assert valid and invalid


def test_validator_and_calculator_tables_agree() -> None:
    assert set(checksums._VALIDATORS) == set(checksums._CHECK_DIGIT_FUNCS)


@pytest.mark.parametrize("scheme", sorted(checksums._KNOWN_SCHEMES))
def test_every_non_fpe_scheme_has_a_body_shape(scheme: str) -> None:
    if scheme not in checksums._FPE_CHECKSUM_SCHEMES:
        assert scheme in checksums._NATIONAL_BODY


@pytest.mark.parametrize("scheme", sorted(VECTORS))
def test_documented_examples(scheme: str) -> None:
    valid, invalid, calcs = VECTORS[scheme]
    for value in valid:
        assert checksums.validate(scheme, value) is True, value
    for value in invalid:
        assert checksums.validate(scheme, value) is False, value
    for body, check in calcs:
        assert checksums.calc_check_digit(scheme, body) == check


# Bodies stdnum alone would mishandle: it raises IndexError or KeyError with the
# body in the message, or returns a check character, for several of these.
_POSITIONAL_MALFORMED = {
    "fr_nir": ("253072C073004", "A95109912611"),
    "es_nie": ("A2482300",),
    "it_codice_fiscale": ("RCCMNL83Z18D969", "RCCMNLA3S18D969", "RCCMNL83S18D96W"),
}


def _malformed_bodies(scheme: str) -> list[str]:
    good = VECTORS[scheme][2][0][0]
    arabic_indic = good.translate({ord(d): 0x0660 + int(d) for d in string.digits})
    full_width = good.translate({ord(d): 0xFF10 + int(d) for d in string.digits})
    bodies = ["", good[:-1], good + "0", good.lower() + "x", good[:-1] + "!", "abc"]
    bodies += [arabic_indic, full_width, *_POSITIONAL_MALFORMED.get(scheme, ())]
    return [b for b in bodies if b != good]


@pytest.mark.parametrize("scheme", NATIONAL)
def test_malformed_body_raises_value_error_without_the_value(scheme: str) -> None:
    for body in _malformed_bodies(scheme):
        with pytest.raises(ValueError, match=scheme) as excinfo:
            checksums.calc_check_digit(scheme, body)
        if body:
            assert body not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__


def _random_body(scheme: str, rng: random.Random) -> str:
    digits = string.digits
    if scheme == "fr_nir":
        dept = rng.choice(["2A", "2B", f"{rng.randrange(1, 96):02d}"])
        return (
            rng.choice("12")
            + f"{rng.randrange(100):02d}"
            + f"{rng.randrange(1, 13):02d}"
            + dept
            + "".join(rng.choices(digits, k=6))
        )
    if scheme == "de_idnr":
        # Exactly one digit repeats (twice), as the IdNr format requires.
        while True:
            chars = rng.sample(digits, 9)
            chars.append(rng.choice(chars))
            rng.shuffle(chars)
            if chars[0] != "0":
                return "".join(chars)
    if scheme == "es_nie":
        return rng.choice("XYZ") + "".join(rng.choices(digits, k=7))
    if scheme == "it_codice_fiscale":
        letters = string.ascii_uppercase
        day = rng.randrange(1, 29) + rng.choice([0, 40])
        return (
            "".join(rng.choices(letters, k=6))
            + f"{rng.randrange(100):02d}"
            + rng.choice("ABCDEHLMPRST")
            + f"{day:02d}"
            + rng.choice(letters)
            + "".join(rng.choices(digits, k=3))
        )
    length = int(checksums._NATIONAL_BODY[scheme][1].split()[0])
    return "".join(rng.choices(digits, k=length))


def _format_rules_hold(scheme: str, value: str) -> bool:
    """Non-checksum rules stdnum also enforces, so the property test can skip them."""
    if scheme == "in_aadhaar":
        return value[0] not in "01" and value != value[::-1]
    if scheme == "ca_sin":
        return value[0] not in "08"
    if scheme == "de_idnr":
        repeated = [value[:10].count(d) for d in set(value[:10]) if value[:10].count(d) > 1]
        return value[0] != "0" and len(repeated) == 1 and repeated[0] in (2, 3)
    return True


@pytest.mark.parametrize("scheme", NATIONAL)
def test_calc_then_validate_round_trips(scheme: str) -> None:
    rng = random.Random(f"round-trip-{scheme}")
    checked = 0
    for _ in range(400):
        body = _random_body(scheme, rng)
        try:
            check = checksums.calc_check_digit(scheme, body)
        except ValueError:
            assert scheme in ("gb_nhs", "au_tfn"), body
            continue
        value = body + check
        if not _format_rules_hold(scheme, value):
            continue
        assert checksums.validate(scheme, value), value
        checked += 1
    assert checked >= 50


# Every national algorithm catches a single-character substitution. Mutations use
# canonical upper-case letters and digits: validate normalises case, so a case
# change is not an error.
_CANONICAL = string.ascii_uppercase + string.digits


@pytest.mark.parametrize("scheme", NATIONAL)
def test_single_character_substitution_is_caught(scheme: str) -> None:
    rng = random.Random(f"substitution-{scheme}")
    tried = 0
    while tried < 60:
        body = _random_body(scheme, rng)
        try:
            value = body + checksums.calc_check_digit(scheme, body)
        except ValueError:
            continue
        if not _format_rules_hold(scheme, value) or not checksums.validate(scheme, value):
            continue
        pos = rng.randrange(len(value))
        new = rng.choice([c for c in _CANONICAL if c != value[pos]])
        assert not checksums.validate(scheme, value[:pos] + new + value[pos + 1 :]), (pos, new)
        tried += 1


def test_nhs_and_tfn_raise_exactly_when_no_digit_validates() -> None:
    for scheme, length in (("gb_nhs", 9), ("au_tfn", 8)):
        rng = random.Random(f"no-digit-{scheme}")
        for _ in range(300):
            body = "".join(rng.choices(string.digits, k=length))
            valid_digits = [d for d in string.digits if checksums.validate(scheme, body + d)]
            if valid_digits:
                assert checksums.calc_check_digit(scheme, body) == valid_digits[0]
            else:
                with pytest.raises(ValueError, match="no valid check digit"):
                    checksums.calc_check_digit(scheme, body)


def test_out_of_scope_forms_are_rejected() -> None:
    from stdnum.au import tfn
    from stdnum.it import codicefiscale

    # stdnum accepts these, the schemes do not: legacy 8-digit TFNs and the
    # 11-digit codice fiscale company form are not personal identifiers here.
    eight_digit = next(
        f"{n:08d}" for n in range(10_000_000, 10_100_000) if tfn.is_valid(f"{n:08d}")
    )
    assert checksums.validate("au_tfn", eight_digit) is False
    assert codicefiscale.is_valid("00743110157")
    assert checksums.validate("it_codice_fiscale", "00743110157") is False


def test_fpe_set_is_unchanged_and_keeps_its_charset_entries() -> None:
    assert {
        "luhn",
        "npi",
        "iban",
        "vin",
        "isbn13",
        "ean13",
        "gtin",
    } == checksums._FPE_CHECKSUM_SCHEMES
    assert set(checksums._SCHEME_REQUIRED_CHARSET) == checksums._FPE_CHECKSUM_SCHEMES - {"iban"}


def _fpe_config(scheme: str) -> dict[str, object]:
    return {
        "tables": [
            {
                "name": "people",
                "columns": [
                    {
                        "name": "national_id",
                        "strategy": "fpe",
                        "provider_config": {"checksum": scheme, "charset": "ALPHANUM"},
                    }
                ],
            }
        ]
    }


@pytest.mark.parametrize(
    "scheme", sorted(checksums._KNOWN_SCHEMES - checksums._FPE_CHECKSUM_SCHEMES)
)
def test_non_fpe_schemes_are_refused_at_plan_compile(scheme: str) -> None:
    with pytest.raises(PlanCompileError) as excinfo:
        check_fpe_checksum_scheme(_fpe_config(scheme))
    assert excinfo.value.code == reasons.plan.FPE_CHECKSUM_UNKNOWN_SCHEME


@pytest.mark.parametrize(
    "scheme", sorted(checksums._KNOWN_SCHEMES - checksums._FPE_CHECKSUM_SCHEMES)
)
def test_non_fpe_schemes_are_refused_by_the_fpe_runtime(scheme: str) -> None:
    with pytest.raises(FpeChecksumError, match="unknown or unsupported"):
        _fpe_checksum_permute(
            "9434765870",
            b"k" * 32,
            "0123456789",
            b"",
            scheme,
            forward=True,
        )


@pytest.mark.parametrize(
    ("scheme", "body"),
    [(scheme, body) for scheme, bodies in _POSITIONAL_MALFORMED.items() for body in bodies],
)
def test_shape_check_runs_before_the_calculator(scheme: str, body: str) -> None:
    calls: list[str] = []

    def spy(value: str) -> str:
        calls.append(value)
        return "0"

    with pytest.raises(ValueError, match=scheme):
        checksums._national_calc(scheme, spy)(body)
    assert calls == []


@pytest.mark.parametrize("error", [KeyError, IndexError, ValueError])
def test_calculator_errors_are_replaced_without_the_body(error: type[Exception]) -> None:
    body = "943476587"

    def leaky(value: str) -> str:
        raise error(f"bad input {value}")

    with pytest.raises(ValueError, match="gb_nhs check-digit calculation failed") as excinfo:
        checksums._national_calc("gb_nhs", leaky)(body)
    assert body not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__
