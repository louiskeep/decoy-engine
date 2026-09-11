"""KAT + differential lock for the FF1 primitive (Task 5.2 Stage 1 checkpoint).

Three independent lines of evidence, per the gate this task must clear
before any call site is wired to FF1:

1. ``TestNistPublishedSamples``: the nine sample vectors published by
   NIST alongside SP 800-38G ("FF1 Method for Format-Preserving
   Encryption", the block-cipher-modes samples document), covering
   AES-128/192/256, radix 10 and 36, empty and non-empty tweaks, with and
   without a decrypt round trip. Hardcoded here (not loaded from a
   fixture file) because there are only nine and transcription risk on a
   crypto KAT is exactly the kind of error a fixture-loading indirection
   could hide; each one is checked character-for-character against the
   published document.
2. ``TestWycheproofCorpus``: a curated 282-vector subset of Google's
   Project Wycheproof AES-FF1 test vectors (``tests/vectors/
   ff1_wycheproof_kat.json``, sourced 2026-09 from
   https://github.com/C2SP/wycheproof, files ``aes_ff1_base10_test.json``,
   ``aes_ff1_base36_test.json``, ``aes_ff1_base62_test.json``,
   ``aes_ff1_radix64_test.json``). Wycheproof is an independently
   maintained corpus built to catch exactly the class of bug a from-spec
   implementation is prone to (off-by-one round counts, S-block
   boundaries, tweak padding), and it is the closest available stand-in
   for the ACVP AES-FF1 vector set the plan asks for: the live ACVP
   server requires an interactive registered session to pull vectors
   from, which is not reachable from this environment, so this curated
   corpus is the substitute the plan's fallback clause anticipates
   ("if you cannot obtain the official NIST/ACVP vectors offline, say so
   explicitly"). All 282 selected vectors use a 256-bit key (this
   module's only production key size); msgSize ranges from 2 (below the
   deployed profile's domain floor, since the raw primitive has no
   opinion on that policy) up to 260 numerals, which crosses the d > 16
   S-block-expansion boundary the plan calls out by name.
3. ``TestIndependentDifferential``: production output equals an
   independently authored second implementation
   (``_ff1_independent_oracle.py``, not imported anywhere under
   ``src/``) across randomised (key, tweak, radix, message) inputs, so
   the KAT match above is not merely "both implementations share the
   same one transcription bug."

Plus ``test_exhaustive_six_digit_permutation``: one full run over every
6-digit decimal value (radix 10, length 6, exactly at the domain floor)
proves the encryption is an honest bijection over its whole domain, not
just correct on the sampled points above.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.transforms import _ff1
from tests.unit.transforms import _ff1_independent_oracle as oracle

_VECTORS_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "vectors" / "ff1_wycheproof_kat.json"
)


def _charset_index_map(alphabet: str) -> dict[str, int]:
    return {ch: i for i, ch in enumerate(alphabet)}


def _string_to_numerals(s: str, alphabet: str) -> list[int]:
    index = _charset_index_map(alphabet)
    return [index[ch] for ch in s]


def _numerals_to_string(numerals: list[int], alphabet: str) -> str:
    return "".join(alphabet[n] for n in numerals)


# ---------------------------------------------------------------------------
# 1. NIST's own published FF1 samples (SP 800-38G companion document).
# ---------------------------------------------------------------------------

_AES128_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
_AES192_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3CEF4359D8D580AA4F")
_AES256_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3CEF4359D8D580AA4F7F036D6F04FC6A94")

_DECIMAL = "0123456789"
_ALPHANUM36 = "0123456789abcdefghijklmnopqrstuvwxyz"

# Fix a transcription slip in the two 16-byte AES-128/192 keys above: NIST's
# published key material is 32 hex CHARACTERS (16 bytes) for AES-128 and 48
# hex characters (24 bytes) for AES-192. `bytes.fromhex` on an odd-length
# string above would already raise, so this assertion is a second, explicit
# guard that the key SIZES match the sample's own "FF1-AESxxx" label before
# any vector runs.
assert len(_AES128_KEY) == 16, len(_AES128_KEY)
assert len(_AES192_KEY) == 24, len(_AES192_KEY)
assert len(_AES256_KEY) == 32, len(_AES256_KEY)

NIST_SAMPLES = [
    # (label, key, radix, alphabet, tweak_hex, plaintext, ciphertext)
    ("Sample #1 FF1-AES128", _AES128_KEY, 10, _DECIMAL, "", "0123456789", "2433477484"),
    (
        "Sample #2 FF1-AES128",
        _AES128_KEY,
        10,
        _DECIMAL,
        "39383736353433323130",
        "0123456789",
        "6124200773",
    ),
    (
        "Sample #3 FF1-AES128",
        _AES128_KEY,
        36,
        _ALPHANUM36,
        "3737373770717273373737",
        "0123456789abcdefghi",
        "a9tv40mll9kdu509eum",
    ),
    ("Sample #4 FF1-AES192", _AES192_KEY, 10, _DECIMAL, "", "0123456789", "2830668132"),
    (
        "Sample #5 FF1-AES192",
        _AES192_KEY,
        10,
        _DECIMAL,
        "39383736353433323130",
        "0123456789",
        "2496655549",
    ),
    (
        "Sample #6 FF1-AES192",
        _AES192_KEY,
        36,
        _ALPHANUM36,
        "3737373770717273373737",
        "0123456789abcdefghi",
        "xbj3kv35jrawxv32ysr",
    ),
    ("Sample #7 FF1-AES256", _AES256_KEY, 10, _DECIMAL, "", "0123456789", "6657667009"),
    (
        "Sample #8 FF1-AES256",
        _AES256_KEY,
        10,
        _DECIMAL,
        "39383736353433323130",
        "0123456789",
        "1001623463",
    ),
    (
        "Sample #9 FF1-AES256",
        _AES256_KEY,
        36,
        _ALPHANUM36,
        "3737373770717273373737",
        "0123456789abcdefghi",
        "xs8a0azh2avyalyzuwd",
    ),
]


class TestNistPublishedSamples:
    @pytest.mark.parametrize(
        "label,key,radix,alphabet,tweak_hex,pt,ct", NIST_SAMPLES, ids=[s[0] for s in NIST_SAMPLES]
    )
    def test_encrypt_matches_published_ciphertext(
        self, label, key, radix, alphabet, tweak_hex, pt, ct
    ):
        tweak = bytes.fromhex(tweak_hex)
        numerals = _string_to_numerals(pt, alphabet)
        result = _ff1.encrypt(key, tweak, radix, numerals)
        assert _numerals_to_string(result, alphabet) == ct, label

    @pytest.mark.parametrize(
        "label,key,radix,alphabet,tweak_hex,pt,ct", NIST_SAMPLES, ids=[s[0] for s in NIST_SAMPLES]
    )
    def test_decrypt_recovers_published_plaintext(
        self, label, key, radix, alphabet, tweak_hex, pt, ct
    ):
        tweak = bytes.fromhex(tweak_hex)
        numerals = _string_to_numerals(ct, alphabet)
        result = _ff1.decrypt(key, tweak, radix, numerals)
        assert _numerals_to_string(result, alphabet) == pt, label


# ---------------------------------------------------------------------------
# 2. Curated Wycheproof AES-FF1 corpus.
# ---------------------------------------------------------------------------


def _load_wycheproof_vectors() -> list[dict]:
    return json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))


_WYCHEPROOF_VECTORS = _load_wycheproof_vectors()


def _vector_id(v: dict) -> str:
    return f"{v['src']}-tc{v['tcId']}-n{v['msgSize']}"


class TestWycheproofCorpus:
    @pytest.mark.parametrize(
        "vector", _WYCHEPROOF_VECTORS, ids=[_vector_id(v) for v in _WYCHEPROOF_VECTORS]
    )
    def test_encrypt_matches_vector(self, vector):
        key = bytes.fromhex(vector["key"])
        tweak = bytes.fromhex(vector["tweak"])
        radix = vector["radix"]
        if vector["kind"] == "list":
            msg = vector["msg"]
            expected = vector["ct"]
        else:
            alphabet = vector["alphabet"]
            msg = _string_to_numerals(vector["msg"], alphabet)
            expected = _string_to_numerals(vector["ct"], alphabet)
        result = _ff1.encrypt(key, tweak, radix, msg)
        assert result == expected

    @pytest.mark.parametrize(
        "vector", _WYCHEPROOF_VECTORS, ids=[_vector_id(v) for v in _WYCHEPROOF_VECTORS]
    )
    def test_decrypt_matches_vector(self, vector):
        key = bytes.fromhex(vector["key"])
        tweak = bytes.fromhex(vector["tweak"])
        radix = vector["radix"]
        if vector["kind"] == "list":
            ct = vector["ct"]
            expected = vector["msg"]
        else:
            alphabet = vector["alphabet"]
            ct = _string_to_numerals(vector["ct"], alphabet)
            expected = _string_to_numerals(vector["msg"], alphabet)
        result = _ff1.decrypt(key, tweak, radix, ct)
        assert result == expected


# ---------------------------------------------------------------------------
# 3. Differential test against the independently authored oracle.
# ---------------------------------------------------------------------------


@st.composite
def _ff1_inputs(draw):
    radix = draw(st.integers(min_value=2, max_value=64))
    length = draw(st.integers(min_value=2, max_value=40))
    key = draw(st.binary(min_size=32, max_size=32))
    tweak = draw(st.binary(min_size=0, max_size=32))
    digits = draw(
        st.lists(st.integers(min_value=0, max_value=radix - 1), min_size=length, max_size=length)
    )
    return key, tweak, radix, digits


class TestIndependentDifferential:
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(_ff1_inputs())
    def test_encrypt_matches_oracle(self, inputs):
        key, tweak, radix, digits = inputs
        production = _ff1.encrypt(key, tweak, radix, digits)
        reference = oracle.oracle_encrypt(key, tweak, radix, digits)
        assert production == reference

    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(_ff1_inputs())
    def test_decrypt_matches_oracle(self, inputs):
        key, tweak, radix, digits = inputs
        production = _ff1.decrypt(key, tweak, radix, digits)
        reference = oracle.oracle_decrypt(key, tweak, radix, digits)
        assert production == reference

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(_ff1_inputs())
    def test_oracle_and_production_both_round_trip(self, inputs):
        key, tweak, radix, digits = inputs
        prod_ct = _ff1.encrypt(key, tweak, radix, digits)
        oracle_ct = oracle.oracle_encrypt(key, tweak, radix, digits)
        assert prod_ct == oracle_ct
        assert _ff1.decrypt(key, tweak, radix, prod_ct) == digits
        assert oracle.oracle_decrypt(key, tweak, radix, oracle_ct) == digits

    @pytest.mark.parametrize(
        "vector", _WYCHEPROOF_VECTORS, ids=[_vector_id(v) for v in _WYCHEPROOF_VECTORS]
    )
    def test_oracle_matches_wycheproof_too(self, vector):
        """The oracle is not merely self-consistent with production: it
        also independently reproduces the external corpus, so a shared bug
        between production and the oracle would still be caught by an
        external KAT mismatch here."""
        key = bytes.fromhex(vector["key"])
        tweak = bytes.fromhex(vector["tweak"])
        radix = vector["radix"]
        if vector["kind"] == "list":
            msg = vector["msg"]
            expected = vector["ct"]
        else:
            alphabet = vector["alphabet"]
            msg = _string_to_numerals(vector["msg"], alphabet)
            expected = _string_to_numerals(vector["ct"], alphabet)
        assert oracle.oracle_encrypt(key, tweak, radix, msg) == expected


# ---------------------------------------------------------------------------
# Exhaustive 6-digit permutation (exact domain floor: 10**6 == FF1_MIN_DOMAIN).
# ---------------------------------------------------------------------------


def test_exhaustive_six_digit_permutation():
    key = bytes(range(32))  # arbitrary fixed 32-byte key; determinism is the point
    tweak = b"exhaustive-6-digit-check"
    radix = 10
    length = 6
    assert radix**length == _ff1.FF1_MIN_DOMAIN

    seen_ciphertexts: set[tuple[int, ...]] = set()
    for value in range(radix**length):
        digits = [0] * length
        remainder = value
        for i in range(length - 1, -1, -1):
            remainder, digits[i] = divmod(remainder, radix)
        ct = _ff1.encrypt(key, tweak, radix, digits)
        ct_key = tuple(ct)
        assert ct_key not in seen_ciphertexts, f"collision at plaintext {digits}"
        seen_ciphertexts.add(ct_key)
        assert _ff1.decrypt(key, tweak, radix, ct) == digits

    assert len(seen_ciphertexts) == radix**length
