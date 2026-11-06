"""The nine national identifier specs and their valid-by-construction builders.

Every builder maps 32 bytes to a compact, upper-case value that passes
``checksums.validate`` for its scheme, including the scheme's non-checksum
format rules, without a rejection loop: generation cannot run out of tries.
Sources per scheme are in each spec's ``source`` and in ``checksums``.
"""

from __future__ import annotations

import string

from decoy_engine import checksums
from decoy_engine.providers_v2.identifiers._national import NationalIdSpec


def _num(b: bytes, start: int, width: int) -> str:
    """``width`` decimal digits from eight bytes at ``start``."""
    return f"{int.from_bytes(b[start : start + 8], 'big') % 10**width:0{width}d}"


def _with_check(scheme: str, body: str) -> str:
    return body + checksums.calc_check_digit(scheme, body)


def _mod11_repaired(scheme: str, body: str) -> str:
    """Append the check digit, stepping the last body digit if none exists.

    NHS weights the last body digit 2 and TFN 9; both are units modulo 11, so
    the ten values of that digit give ten distinct remainders and at most one
    lacks a check digit: one step always suffices.
    """
    try:
        return _with_check(scheme, body)
    except ValueError:
        stepped = body[:-1] + str((int(body[-1]) + 1) % 10)
        return _with_check(scheme, stepped)


def _gb_nhs(b: bytes) -> str:
    # 999 is the NHS test range: valid check digits, never issued to a patient.
    return _mod11_repaired("gb_nhs", "999" + _num(b, 0, 6))


def _au_tfn(b: bytes) -> str:
    return _mod11_repaired("au_tfn", _num(b, 0, 8))


def _ca_sin(b: bytes) -> str:
    # 0 and 8 lead no valid SIN.
    return _with_check("ca_sin", "12345679"[b[0] % 8] + _num(b, 1, 7))


def _in_aadhaar(b: bytes) -> str:
    body = "23456789"[b[0] % 8] + _num(b, 1, 10)
    value = _with_check("in_aadhaar", body)
    if value == value[::-1]:
        # Verhoeff catches every single-digit change, so the new check digit
        # cannot restore the palindrome.
        body = body[:5] + str((int(body[5]) + 1) % 10) + body[6:]
        value = _with_check("in_aadhaar", body)
    return value


def _de_idnr(b: bytes) -> str:
    # Exactly one digit appears twice in the first ten, as the IdNr requires.
    digits = list(string.digits)
    for i in range(9, 0, -1):
        j = b[i] % (i + 1)
        digits[i], digits[j] = digits[j], digits[i]
    chosen = digits[:9]
    chosen.insert(b[10] % 10, chosen[b[11] % 9])
    # Rotation keeps the digit counts; at most two zeros, so a non-zero lead exists.
    start = next(i for i, d in enumerate(chosen) if d != "0")
    body = "".join([*chosen[start:], *chosen[:start]])
    return _with_check("de_idnr", body)


_NIR_DEPARTMENTS = (*(f"{d:02d}" for d in range(1, 96) if d != 20), "2A", "2B")


def _fr_nir(b: bytes) -> str:
    body = (
        "12"[b[0] % 2]
        + f"{b[1] % 100:02d}"
        + f"{b[2] % 12 + 1:02d}"
        + _NIR_DEPARTMENTS[int.from_bytes(b[3:5], "big") % len(_NIR_DEPARTMENTS)]
        + _num(b, 5, 6)
    )
    return _with_check("fr_nir", body)


def _es_dni(b: bytes) -> str:
    return _with_check("es_dni", _num(b, 0, 8))


def _es_nie(b: bytes) -> str:
    return _with_check("es_nie", "XYZ"[b[0] % 3] + _num(b, 1, 7))


def _it_codice_fiscale(b: bytes) -> str:
    letters = string.ascii_uppercase
    # Days 1 to 28 exist in every month; +40 is the female form.
    day = b[8] % 28 + 1 + (40 if b[9] % 2 else 0)
    body = (
        "".join(letters[x % 26] for x in b[0:6])
        + f"{b[6] % 100:02d}"
        + "ABCDEHLMPRST"[b[7] % 12]
        + f"{day:02d}"
        + letters[b[10] % 26]
        + f"{int.from_bytes(b[11:13], 'big') % 1000:03d}"
    )
    return _with_check("it_codice_fiscale", body)


NATIONAL_ID_SPECS: tuple[NationalIdSpec, ...] = (
    NationalIdSpec(
        "synthetic_gb_nhs", "gb_nhs", ("en_GB",), r"^999[0-9]{7}$", _gb_nhs, "NHS Digital"
    ),
    NationalIdSpec(
        "synthetic_ca_sin",
        "ca_sin",
        ("en_CA", "fr_CA"),
        r"^[1-79][0-9]{8}$",
        _ca_sin,
        "Employment and Social Development Canada",
    ),
    NationalIdSpec(
        "synthetic_in_aadhaar",
        "in_aadhaar",
        ("en_IN", "hi_IN"),
        r"^[2-9][0-9]{11}$",
        _in_aadhaar,
        "UIDAI",
    ),
    NationalIdSpec(
        "synthetic_au_tfn",
        "au_tfn",
        ("en_AU",),
        r"^[0-9]{9}$",
        _au_tfn,
        "Australian Taxation Office",
    ),
    NationalIdSpec(
        "synthetic_de_idnr", "de_idnr", ("de_DE",), r"^[1-9][0-9]{10}$", _de_idnr, "BZSt"
    ),
    NationalIdSpec(
        "synthetic_fr_nir",
        "fr_nir",
        ("fr_FR",),
        r"^[12][0-9]{4}(?:[0-9]{2}|2[AB])[0-9]{8}$",
        _fr_nir,
        "INSEE",
    ),
    NationalIdSpec(
        "synthetic_es_dni",
        "es_dni",
        ("es_ES",),
        r"^[0-9]{8}[A-Z]$",
        _es_dni,
        "Ministerio del Interior",
    ),
    NationalIdSpec(
        "synthetic_es_nie",
        "es_nie",
        ("es_ES",),
        r"^[XYZ][0-9]{7}[A-Z]$",
        _es_nie,
        "Ministerio del Interior",
    ),
    NationalIdSpec(
        "synthetic_it_codice_fiscale",
        "it_codice_fiscale",
        ("it_IT",),
        r"^[A-Z]{6}[0-9]{2}[ABCDEHLMPRST][0-9]{2}[A-Z][0-9]{3}[A-Z]$",
        _it_codice_fiscale,
        "Agenzia delle Entrate",
    ),
)
