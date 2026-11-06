"""Check-digit registry for structured identifier schemes (SP-04 / P5.INFRA.1).

Pattern: Check-digit validation (python-stdnum + NPPES + ISO 3779).
python-stdnum (LGPL) backs luhn/iban/ean/isbn/gtin; the CMS NPPES NPI Check
Digit Procedure backs npi; ISO 3779 / NHTSA 49 CFR 565 Appendix B backs vin.

Provides two public functions with a uniform signature:

    validate(scheme, value) -> bool
    calc_check_digit(scheme, body) -> str

Supported schemes
-----------------
luhn    Credit-card PAN and any Luhn-protected digit string.
        Uses python-stdnum 2.2 stdnum.luhn (Luhn 1954, US Patent 2,950,048).

npi     US National Provider Identifier (10 digits, CMS NPPES).
        python-stdnum has no NPI module; hand-rolled per the CMS check-digit
        specification:
        https://www.cms.gov/Regulations-and-Guidance/Administrative-Simplification/
        NationalProvIdentStand/Downloads/NPIcheckdigit.pdf
        Algorithm: Luhn applied to the string '80840' + 9-digit NPI body.

iban    International Bank Account Number (ISO 13616).
        Uses python-stdnum 2.2 stdnum.iban (ISO 7064 mod-97).
        Body = CC (2-char country code) + BBAN; returns 2-char check string.

vin     Vehicle Identification Number (ISO 3779 / NHTSA 49 CFR Part 565).
        python-stdnum has no VIN module; hand-rolled per the NHTSA standard.
        Body = the 16 non-check characters (positions 0-7 and 9-16 of the
        full 17-character VIN).  Check = single char: '0'-'9' or 'X' (=10).

isbn13  ISBN-13 (ISO 2108, 13-digit form).
        Validation via python-stdnum 2.2 stdnum.isbn.is_valid.
        Check-digit via stdnum.ean.calc_check_digit (ISBN-13 reuses the GS1
        algorithm; body = 12 digits).

ean13   EAN-13 / UPC (GS1 General Specifications).
        Uses python-stdnum 2.2 stdnum.ean (body = 12 digits; check = 1 digit).

gtin    GTIN (Global Trade Item Number, GS1; 8/12/13/14-digit variants).
        Uses python-stdnum 2.2 stdnum.ean which handles all four lengths.
        Body = all digits except the trailing check digit.

National identifier schemes (validate via the named python-stdnum module):

gb_nhs             UK NHS number. stdnum.gb.nhs. Body 9 digits; check by
                   modulus 11 (weights 10..2), hand-rolled because stdnum
                   only validates. A body whose remainder gives 10 has no
                   valid check digit and raises ValueError.
ca_sin             Canadian SIN. stdnum.ca.sin. Body 8 digits; Luhn.
in_aadhaar         Indian Aadhaar. stdnum.in_.aadhaar. Body 11 digits; Verhoeff.
au_tfn             Australian TFN, 9-digit form. stdnum.au.tfn. Body 8 digits;
                   weights 1,4,3,7,5,8,6,9,10 with the sum modulo 11 equal to 0,
                   hand-rolled; a body with no valid digit raises ValueError.
de_idnr            German Steuer-IdNr. stdnum.de.idnr. Body 10 digits;
                   ISO 7064 mod 11,10.
fr_nir             French NIR. stdnum.fr.nir. Body 13 characters (Corsica 2A or
                   2B allowed); returns 2 digits.
es_dni             Spanish DNI. stdnum.es.dni. Body 8 digits; returns a letter.
es_nie             Spanish NIE. stdnum.es.nie. Body X, Y or Z plus 7 digits;
                   returns a letter.
it_codice_fiscale  Italian codice fiscale, 16-character personal form.
                   stdnum.it.codicefiscale. Body 15 characters; returns a letter.

For the national schemes, ``calc_check_digit`` checks the body shape first
(stdnum's calculators assume a well-formed body), and a computed check digit
does not promise ``validate`` passes when the scheme has other format rules
(Aadhaar's leading digit and palindrome rule, IdNr's repeated-digit rule,
SIN's leading digit, the codice fiscale birth date), as with ``npi``.

FPE checksum mode admits only ``_FPE_CHECKSUM_SCHEMES``: the national schemes
include bodies with no valid check digit and letter check characters, which
the FF1 path does not handle.

Dependencies
------------
python-stdnum >= 2.2  (core dependency, declared in pyproject.toml).

Internal notes
--------------
python-stdnum validate() functions return the normalised value on success and
raise stdnum.exceptions.ValidationError on failure; use is_valid() to get
a plain bool.  calc_check_digit functions return a string (1 or 2 chars).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from types import ModuleType

import stdnum.au.tfn as _au_tfn
import stdnum.ca.sin as _ca_sin
import stdnum.de.idnr as _de_idnr
import stdnum.ean as _ean
import stdnum.es.dni as _es_dni
import stdnum.es.nie as _es_nie
import stdnum.fr.nir as _fr_nir
import stdnum.gb.nhs as _gb_nhs
import stdnum.iban as _iban
import stdnum.in_.aadhaar as _in_aadhaar
import stdnum.isbn as _isbn
import stdnum.it.codicefiscale as _it_cf
import stdnum.luhn as _luhn
import stdnum.verhoeff as _verhoeff
from stdnum.iso7064 import mod_11_10 as _mod_11_10

# ---------------------------------------------------------------------------
# Luhn
# ---------------------------------------------------------------------------

# Delegates entirely to python-stdnum (Luhn 1954, US Patent 2,950,048).


def _luhn_validate(value: str) -> bool:
    return _luhn.is_valid(value)


def _luhn_calc_check_digit(body: str) -> str:
    return _luhn.calc_check_digit(body)


# ---------------------------------------------------------------------------
# NPI  (hand-rolled: not in python-stdnum)
# ---------------------------------------------------------------------------

_NPI_PREFIX = "80840"
_NPI_RE = re.compile(r"^\d{10}$")


def _luhn_cd(digits: str) -> str:
    """Luhn check digit for an arbitrary digit string (used by NPI).

    Matches the CMS NPPES spec: process the digits right-to-left, doubling
    every second digit (starting from rightmost = position 0).
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def _npi_calc_check_digit(body: str) -> str:
    """NPI check digit: Luhn applied to '80840' + 9-digit body.

    CMS NPPES NPI Check Digit Procedure (2008):
    prefix '80840' is prepended to the 9-digit NPI body before computing
    the Luhn check digit.  The result is the 10th digit of the full NPI.

    Verified: body '123456789' -> prefix '80840123456789' -> check '3'.
    """
    return _luhn_cd(_NPI_PREFIX + body)


def _npi_validate(value: str) -> bool:
    """NPI check: 10 digits, first digit 1 or 2 (NPPES), last digit is CMS check.

    NPPES allocates 1- and 2-prefixed NPIs only (CMS, 2008).  A 10-digit
    string with a valid Luhn check but a leading 0 or 3-9 is structurally
    invalid and is rejected here to agree with the engine's own NpiValidator
    (providers_v2/identifiers/_npi.py ``_is_valid_npi``).
    """
    if not _NPI_RE.match(value):
        return False
    if value[0] not in ("1", "2"):
        return False
    return value[9] == _npi_calc_check_digit(value[:9])


# ---------------------------------------------------------------------------
# IBAN  (python-stdnum; ISO 13616 / ISO 7064 mod-97)
# ---------------------------------------------------------------------------


def _iban_calc_check_digit(body: str) -> str:
    """Compute the 2-character IBAN check digits for a CC + BBAN body.

    Args:
        body: 2-char ISO 3166 country code followed by the BBAN (no check
              digits, no spaces).  E.g. 'GBWEST12345698765432'.

    Returns:
        2-digit check string.  The full IBAN is body[:2] + check + body[2:].
    """
    cc = body[:2]
    bban = body[2:]
    return _iban.calc_check_digits(cc + "xx" + bban)


def _iban_validate(value: str) -> bool:
    return _iban.is_valid(value)


# ---------------------------------------------------------------------------
# VIN  (hand-rolled: not in python-stdnum; ISO 3779 / NHTSA 49 CFR 565)
# ---------------------------------------------------------------------------

# Transliteration values per NHTSA 49 CFR Part 565 Appendix B.
# Letters I, O, Q are not used in VIN; omitted from this map.
_VIN_TRANS: dict[str, int] = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "J": 1,
    "K": 2,
    "L": 3,
    "M": 4,
    "N": 5,
    "P": 7,
    "R": 9,
    "S": 2,
    "T": 3,
    "U": 4,
    "V": 5,
    "W": 6,
    "X": 7,
    "Y": 8,
    "Z": 9,
    **{str(d): d for d in range(10)},
}

# Positional weights for VIN positions 1-17 (1-indexed).
# Position 9 (index 8) has weight 0 -- the check digit slot.
_VIN_WEIGHTS: tuple[int, ...] = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

# Valid VIN characters (no I, O, Q per ISO 3779).
_VIN_CHARSET: frozenset[str] = frozenset(_VIN_TRANS.keys())


def _vin_check_from_full(vin17: str) -> str:
    """Compute the VIN check character from a 17-character VIN string.

    The string at position 8 (0-indexed) is the check position; its weight
    is 0 so its current value does not affect the sum.  Returns '0'-'9'
    or 'X' (= 10).

    Standard: ISO 3779 / NHTSA 49 CFR Part 565, Appendix B.
    """
    total = sum(_VIN_TRANS[c] * _VIN_WEIGHTS[i] for i, c in enumerate(vin17))
    r = total % 11
    return "X" if r == 10 else str(r)


def _vin_calc_check_digit(body: str) -> str:
    """Compute the VIN check character from the 16 non-check characters.

    Args:
        body: 16-character string = positions 0-7 (before check) + positions
              9-16 (after check) of the full 17-character VIN.

    Returns:
        Single character: '0'-'9' or 'X'.

    Because the check position (index 8) has weight 0 in the sum, inserting
    any valid character there produces the same result.  We insert '0' as a
    neutral placeholder (ISO 3779 Appendix B computation property).
    """
    full = body[:8] + "0" + body[8:]  # neutral placeholder at weight-0 position
    return _vin_check_from_full(full)


def _vin_validate(value: str) -> bool:
    """VIN check: exactly 17 valid characters, check digit at position 8.

    Standard: ISO 3779 / NHTSA 49 CFR Part 565.
    """
    v = value.strip().upper()
    if len(v) != 17:
        return False
    if not all(c in _VIN_CHARSET for c in v):
        return False
    return _vin_check_from_full(v) == v[8]


# ---------------------------------------------------------------------------
# ISBN-13  (python-stdnum; ISO 2108 + GS1 check digit)
# ---------------------------------------------------------------------------


def _isbn13_calc_check_digit(body: str) -> str:
    """ISBN-13 check digit: same GS1 alternating-weight algorithm as EAN-13.

    Body = 12 digits (including the 978/979 bookland prefix).
    Delegates to stdnum.ean.calc_check_digit (ISBN-13 = EAN-13 compatible).
    """
    return _ean.calc_check_digit(body)


def _isbn13_validate(value: str) -> bool:
    return _isbn.is_valid(value)


# ---------------------------------------------------------------------------
# EAN-13  (python-stdnum; GS1 General Specifications)
# ---------------------------------------------------------------------------


def _ean13_calc_check_digit(body: str) -> str:
    """EAN-13 check digit: GS1 alternating weights (1, 3), mod 10.

    Body = N-1 digits (where N is 8, 12, 13, or 14 for the four EAN/GTIN
    variants); returns 1 digit.  Delegates to stdnum.ean.calc_check_digit.
    """
    return _ean.calc_check_digit(body)


def _ean13_validate(value: str) -> bool:
    return _ean.is_valid(value)


# ---------------------------------------------------------------------------
# GTIN  (python-stdnum; GS1 GTIN spec -- all lengths via stdnum.ean)
# ---------------------------------------------------------------------------


def _gtin_calc_check_digit(body: str) -> str:
    """GTIN check digit: same GS1 algorithm as EAN-13, applicable to all lengths.

    Delegates to stdnum.ean.calc_check_digit which handles GTIN-8 (7-digit
    body), GTIN-12 (11-digit body), GTIN-13 (12-digit body), and GTIN-14
    (13-digit body).
    """
    return _ean.calc_check_digit(body)


def _gtin_validate(value: str) -> bool:
    return _ean.is_valid(value)


# ---------------------------------------------------------------------------
# National identifiers  (python-stdnum validation; see module docstring)
# ---------------------------------------------------------------------------

_CF_ODD = "[0-9LMNPQRSTUV]"  # codice fiscale digit positions (omocodia letters allowed)

# Body shape per scheme, checked before any calculator runs: stdnum's
# calculators raise IndexError or KeyError, or return a digit, on a bad body.
_NATIONAL_BODY: dict[str, tuple[re.Pattern[str], str]] = {
    "gb_nhs": (re.compile(r"[0-9]{9}"), "9 digits"),
    "ca_sin": (re.compile(r"[0-9]{8}"), "8 digits"),
    "in_aadhaar": (re.compile(r"[0-9]{11}"), "11 digits"),
    "au_tfn": (re.compile(r"[0-9]{8}"), "8 digits"),
    "de_idnr": (re.compile(r"[0-9]{10}"), "10 digits"),
    "fr_nir": (
        re.compile(r"[0-9]{5}(?:[0-9]{2}|2[AB])[0-9]{6}"),
        "5 digits, a department (2 digits, 2A or 2B), then 6 digits",
    ),
    "es_dni": (re.compile(r"[0-9]{8}"), "8 digits"),
    "es_nie": (re.compile(r"[XYZ][0-9]{7}"), "X, Y or Z then 7 digits"),
    "it_codice_fiscale": (
        re.compile(rf"[A-Z]{{6}}{_CF_ODD}{{2}}[ABCDEHLMPRST]{_CF_ODD}{{2}}[A-Z]{_CF_ODD}{{3}}"),
        "the 15-character codice fiscale pattern",
    ),
}


def _nhs_digit(body: str) -> str | None:
    """NHS modulus 11: weights 10..2; 11 maps to 0 and 10 has no valid digit."""
    check = 11 - sum(w * int(d) for w, d in zip(range(10, 1, -1), body, strict=True)) % 11
    if check == 10:
        return None
    return "0" if check == 11 else str(check)


def _tfn_digit(body: str) -> str | None:
    """TFN: the last weight is 10, which is -1 modulo 11, so the digit equals the body sum."""
    check = sum(w * int(d) for w, d in zip((1, 4, 3, 7, 5, 8, 6, 9), body, strict=True)) % 11
    if check == 10:
        return None
    return str(check)


def _national_calc(scheme: str, calc: Callable[[str], str | None]) -> Callable[[str], str]:
    pattern, shape = _NATIONAL_BODY[scheme]

    def _calc(body: str) -> str:
        # Messages name the scheme and shape only: a body is part of an identifier.
        message = f"{scheme} check-digit body must be {shape}"
        if not isinstance(body, str) or not pattern.fullmatch(body):
            raise ValueError(message) from None
        try:
            check = calc(body)
        except (KeyError, IndexError, ValueError):
            # Unreachable after the shape check, so this is a calculator fault, not bad
            # input; stdnum messages carry the body, so it is not chained.
            raise ValueError(f"{scheme} check-digit calculation failed") from None
        if check is None:
            raise ValueError(f"{scheme} body has no valid check digit")
        return check

    return _calc


def _national_validate(module: ModuleType, length: int | None = None) -> Callable[[str], bool]:
    is_valid: Callable[[str], bool] = module.is_valid
    compact: Callable[[str], str] = module.compact

    def _validate(value: str) -> bool:
        if not is_valid(value):
            return False
        return length is None or len(compact(value)) == length

    return _validate


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

_VALIDATORS: dict[str, Callable[[str], bool]] = {
    "luhn": _luhn_validate,
    "npi": _npi_validate,
    "iban": _iban_validate,
    "vin": _vin_validate,
    "isbn13": _isbn13_validate,
    "ean13": _ean13_validate,
    "gtin": _gtin_validate,
    "gb_nhs": _national_validate(_gb_nhs),
    "ca_sin": _national_validate(_ca_sin),
    "in_aadhaar": _national_validate(_in_aadhaar),
    "au_tfn": _national_validate(_au_tfn, length=9),
    "de_idnr": _national_validate(_de_idnr),
    "fr_nir": _national_validate(_fr_nir),
    "es_dni": _national_validate(_es_dni),
    "es_nie": _national_validate(_es_nie),
    "it_codice_fiscale": _national_validate(_it_cf, length=16),
}

_CHECK_DIGIT_FUNCS: dict[str, Callable[[str], str]] = {
    "luhn": _luhn_calc_check_digit,
    "npi": _npi_calc_check_digit,
    "iban": _iban_calc_check_digit,
    "vin": _vin_calc_check_digit,
    "isbn13": _isbn13_calc_check_digit,
    "ean13": _ean13_calc_check_digit,
    "gtin": _gtin_calc_check_digit,
    "gb_nhs": _national_calc("gb_nhs", _nhs_digit),
    "ca_sin": _national_calc("ca_sin", _luhn.calc_check_digit),
    "in_aadhaar": _national_calc("in_aadhaar", _verhoeff.calc_check_digit),
    "au_tfn": _national_calc("au_tfn", _tfn_digit),
    "de_idnr": _national_calc("de_idnr", _mod_11_10.calc_check_digit),
    "fr_nir": _national_calc("fr_nir", _fr_nir.calc_check_digits),
    "es_dni": _national_calc("es_dni", _es_dni.calc_check_digit),
    "es_nie": _national_calc("es_nie", _es_nie.calc_check_digit),
    "it_codice_fiscale": _national_calc("it_codice_fiscale", _it_cf.calc_check_digit),
}

_KNOWN_SCHEMES: frozenset[str] = frozenset(_VALIDATORS.keys())

# Kept apart from _KNOWN_SCHEMES so registering a scheme never admits it to FPE.
# iban stays in the set so FPE refuses it with its own code, not "unknown".
_FPE_CHECKSUM_SCHEMES: frozenset[str] = frozenset(
    {"luhn", "npi", "iban", "vin", "isbn13", "ean13", "gtin"}
)

# Per-scheme minimum required charset for FPE mode.  A configured FPE charset
# that does not contain all characters in this set causes values to pass
# through unmasked at runtime: with preserve_separators=True, missing characters
# are treated as separators, fragmenting the value into short runs that fall
# below the per-scheme L1 min-length guard and returning the input verbatim.
# check_fpe_checksum_scheme in plan/_checks.py rejects the misconfiguration
# at plan-compile rather than letting it silently no-op at run time.
#
# iban is absent: FPE mode is unconditionally unsupported for iban regardless
# of charset (caught by the preceding fpe_checksum_iban_unsupported check).
_SCHEME_REQUIRED_CHARSET: dict[str, frozenset[str]] = {
    "luhn": frozenset("0123456789"),
    "npi": frozenset("0123456789"),
    "ean13": frozenset("0123456789"),
    "isbn13": frozenset("0123456789"),
    "gtin": frozenset("0123456789"),
    # VIN: 0-9 plus A-Z excluding I, O, Q (NHTSA 49 CFR Part 565 Appendix B).
    # Reuses the module-level _VIN_CHARSET so the two stay in sync.
    "vin": _VIN_CHARSET,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate(scheme: str, value: str) -> bool:
    """Return True iff ``value`` has a valid check digit for ``scheme``.

    Args:
        scheme: A registered scheme (see the module docstring).
        value:  The full value including its check digit(s).

    Returns:
        True if the check digit is valid; False otherwise.

    Raises:
        ValueError: If ``scheme`` is not a recognised checksum scheme.
    """
    if scheme not in _KNOWN_SCHEMES:
        raise ValueError(
            f"unknown checksum scheme {scheme!r}. Known schemes: {sorted(_KNOWN_SCHEMES)}"
        )
    return _VALIDATORS[scheme](value)


def calc_check_digit(scheme: str, body: str) -> str:
    """Compute the check digit(s) for ``body`` under ``scheme``.

    The ``body`` semantics are scheme-specific:

    * ``luhn``, ``npi``, ``ean13``, ``isbn13``, ``gtin``: body is the full
      value minus the trailing check digit.
    * ``iban``: body is the 2-char country code followed directly by the
      BBAN (no check digits, no spaces).  Returns a 2-character string.
    * ``vin``: body is the 16 non-check characters (positions 0-7 and 9-16
      of the 17-character VIN).  Returns a single character ('0'-'9' or 'X').
    * national schemes (``gb_nhs`` and the rest): body shape per the module
      docstring; a malformed body raises ``ValueError``.

    Args:
        scheme: A registered scheme (see the module docstring).
        body:   The non-check portion of the value (see above).

    Returns:
        The check digit string (1 character for most schemes; 2 for IBAN).

    Raises:
        ValueError: If ``scheme`` is not a recognised checksum scheme, or a
            national scheme's body is malformed or has no valid check digit.
    """
    if scheme not in _KNOWN_SCHEMES:
        raise ValueError(
            f"unknown checksum scheme {scheme!r}. Known schemes: {sorted(_KNOWN_SCHEMES)}"
        )
    return _CHECK_DIGIT_FUNCS[scheme](body)
