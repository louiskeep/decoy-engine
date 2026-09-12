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
4. ``TestAcvpCorpus`` (round-2 BLOCKER-2a): the real ACVP AES-FF1 vector
   set the plan originally asked for (``tests/vectors/
   ff1_acvp_aes256_kat.json``, full provenance in ``tests/vectors/
   README.md``), fetched from the public ``usnistgov/ACVP-Server`` gen-val
   JSON files -- the round-1 "no reachable session-authenticated ACVP
   server" blocker did not apply to this static mirror. 250 AES-256 cases,
   radix 2/4/16/32/64, msgSize 10-512.
5. ``TestWycheproofInvalidCorpus`` (round-2 BLOCKER-2c): 44 genuine
   Wycheproof ``result: "invalid"`` cases (``InvalidKeySize`` /
   ``InvalidMessageSize``) the curated corpus above deliberately excluded --
   pulled back in here as committed malformed-input KATs instead of only
   hand-authored ones.

Plus ``test_exhaustive_six_digit_permutation``: one full run over every
6-digit decimal value (radix 10, length 6, exactly at the domain floor)
proves the encryption is an honest bijection over its whole domain, not
just correct on the sampled points above.

And ``TestMalformedInputsAndBoundaries`` (round-2 BLOCKER-1 mutation-kill
pass): hand-authored primitive-level cases no corpus above happens to cover
-- an out-of-range numeral (``digit == radix``, the mutmut survivor that
turned ``<`` into ``<=``), a 64- and 256-byte tweak (the survivor that
turned the ``2**32`` uint32 ceiling into ``64``), and the LOW-2 standards-
level radix ceiling.
"""

from __future__ import annotations

import json
import signal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.transforms import _ff1
from tests.unit.transforms import _ff1_independent_oracle as oracle

_VECTORS_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "vectors" / "ff1_wycheproof_kat.json"
)
_ACVP_VECTORS_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "vectors" / "ff1_acvp_aes256_kat.json"
)
_INVALID_VECTORS_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "vectors" / "ff1_wycheproof_invalid_kat.json"
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
    """`HealthCheck.differing_executors` is suppressed on every test here: the
    mutation-grading harness (round-2 BLOCKER-1, scripts/tq_mutate.py) runs
    this SAME test twice per mutant -- once from the real source tree, once
    from mutmut's `mutants/` copy -- and Hypothesis's on-disk example
    database sees the same qualified test name reappear from a different
    execution context. That is real for THIS tooling, not a sign of test
    flakiness; the check exists to catch a test invoked inconsistently
    within one run, which is not what is happening here."""

    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.differing_executors],
    )
    @given(_ff1_inputs())
    def test_encrypt_matches_oracle(self, inputs):
        key, tweak, radix, digits = inputs
        production = _ff1.encrypt(key, tweak, radix, digits)
        reference = oracle.oracle_encrypt(key, tweak, radix, digits)
        assert production == reference

    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.differing_executors],
    )
    @given(_ff1_inputs())
    def test_decrypt_matches_oracle(self, inputs):
        key, tweak, radix, digits = inputs
        production = _ff1.decrypt(key, tweak, radix, digits)
        reference = oracle.oracle_decrypt(key, tweak, radix, digits)
        assert production == reference

    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.differing_executors],
    )
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


# ---------------------------------------------------------------------------
# 4. ACVP AES-256 corpus (round-2 BLOCKER-2a: the vectors the plan actually
# asked for, not just the Wycheproof stand-in).
# ---------------------------------------------------------------------------


def _load_acvp_vectors() -> list[dict]:
    return json.loads(_ACVP_VECTORS_PATH.read_text(encoding="utf-8"))


_ACVP_VECTORS = _load_acvp_vectors()


def _acvp_vector_id(v: dict) -> str:
    return f"acvp-tg{v['tgId']}-tc{v['tcId']}-{v['direction']}-n{v['msgSize']}"


class TestAcvpCorpus:
    """The official NIST ACVP AES-FF1 vector set (``tests/vectors/
    ff1_acvp_aes256_kat.json``; provenance in ``tests/vectors/README.md``).

    Every case is checked BOTH directions regardless of its own recorded
    ``direction`` (an ACVP encrypt-group case's plaintext must decrypt back
    from its expected ciphertext just as much as an encrypt from it must
    match): a stronger claim than the source protocol itself makes per case,
    and the one a KAT lock actually needs.
    """

    @pytest.mark.parametrize(
        "vector", _ACVP_VECTORS, ids=[_acvp_vector_id(v) for v in _ACVP_VECTORS]
    )
    def test_encrypt_matches_vector(self, vector):
        key = bytes.fromhex(vector["key"])
        tweak = bytes.fromhex(vector["tweak"])
        radix = vector["radix"]
        alphabet = vector["alphabet"]
        msg = _string_to_numerals(vector["msg"], alphabet)
        expected = _string_to_numerals(vector["ct"], alphabet)
        assert _ff1.encrypt(key, tweak, radix, msg) == expected

    @pytest.mark.parametrize(
        "vector", _ACVP_VECTORS, ids=[_acvp_vector_id(v) for v in _ACVP_VECTORS]
    )
    def test_decrypt_matches_vector(self, vector):
        key = bytes.fromhex(vector["key"])
        tweak = bytes.fromhex(vector["tweak"])
        radix = vector["radix"]
        alphabet = vector["alphabet"]
        ct = _string_to_numerals(vector["ct"], alphabet)
        expected = _string_to_numerals(vector["msg"], alphabet)
        assert _ff1.decrypt(key, tweak, radix, ct) == expected


# ---------------------------------------------------------------------------
# 5. Wycheproof INVALID cases (round-2 BLOCKER-2c: malformed-input boundary
# coverage backed by the same external corpus, not just hand-authored cases).
# ---------------------------------------------------------------------------


def _load_invalid_vectors() -> list[dict]:
    return json.loads(_INVALID_VECTORS_PATH.read_text(encoding="utf-8"))


_INVALID_VECTORS = _load_invalid_vectors()


def _invalid_vector_id(v: dict) -> str:
    return f"{v['src']}-tc{v['tcId']}-{'+'.join(v['flags'])}"


class TestWycheproofInvalidCorpus:
    """Every Wycheproof ``InvalidKeySize`` / ``InvalidMessageSize`` case
    (``tests/vectors/ff1_wycheproof_invalid_kat.json``; provenance in
    ``tests/vectors/README.md``) must raise ``Ff1Error`` from the raw
    primitive -- an unrecognized key size or an under-length numeral string
    is a caller bug the primitive must refuse, never silently "encrypt"."""

    @pytest.mark.parametrize(
        "vector", _INVALID_VECTORS, ids=[_invalid_vector_id(v) for v in _INVALID_VECTORS]
    )
    def test_invalid_input_is_rejected(self, vector):
        key = bytes.fromhex(vector["key"])
        tweak = bytes.fromhex(vector["tweak"])
        radix = vector["radix"]
        msg = (
            vector["msg"]
            if vector["kind"] == "list"
            else _string_to_numerals(vector["msg"], vector["alphabet"])
        )
        with pytest.raises(_ff1.Ff1Error):
            _ff1.encrypt(key, tweak, radix, msg)


# ---------------------------------------------------------------------------
# 6. Round-2 BLOCKER-1 mutation-kill pass + LOW-2: hand-authored primitive-
# level boundaries no corpus above happens to exercise.
# ---------------------------------------------------------------------------


class TestMalformedInputsAndBoundaries:
    _KEY = bytes(range(32))
    _TWEAK = b"boundary-check"

    def test_numeral_equal_to_radix_is_rejected(self):
        """BLOCKER-1a: kills the mutmut survivor that turned
        ``0 <= digit < radix`` into ``0 <= digit <= radix`` in
        ``_validate_common``. A numeral equal to ``radix`` (e.g. ``10`` at
        radix 10) is not a valid base-``radix`` digit -- there is no such
        digit -- and admitting it would let ``_num_radix``/``_str_m_radix``
        silently operate on a value outside the declared domain."""
        radix = 10
        numerals = [1, 2, radix, 4, 5, 6]  # radix itself is one past the top digit
        with pytest.raises(_ff1.Ff1Error, match=r"in \[0, 10\)"):
            _ff1.encrypt(self._KEY, self._TWEAK, radix, numerals)
        with pytest.raises(_ff1.Ff1Error, match=r"in \[0, 10\)"):
            _ff1.decrypt(self._KEY, self._TWEAK, radix, numerals)

    def test_numeral_negative_is_rejected(self):
        """Same guard's other edge: a negative numeral is equally not a
        valid base-``radix`` digit."""
        radix = 10
        numerals = [1, 2, -1, 4, 5, 6]
        with pytest.raises(_ff1.Ff1Error, match=r"in \[0, 10\)"):
            _ff1.encrypt(self._KEY, self._TWEAK, radix, numerals)

    @pytest.mark.parametrize("tweak_len", [64, 256])
    def test_large_tweak_is_accepted_and_round_trips(self, tweak_len):
        """BLOCKER-1b: kills the mutmut survivor that turned the primitive's
        ``len(tweak) >= 2**32`` uint32 ceiling into ``>= 64``, which would
        wrongly reject every tweak from 64 bytes up -- exactly the deployed
        profile's ``FF1_MAX_TWEAK_LEN == 256`` ceiling this test reaches.
        Doubles as the BLOCKER-2c 256-byte-maximum-tweak boundary case: no
        external corpus above carries a tweak anywhere near this long (ACVP
        tops out at 16 bytes, Wycheproof at 32), so round-trip + independent-
        oracle agreement is the evidence, not an external KAT."""
        assert tweak_len <= _ff1.FF1_MAX_TWEAK_LEN
        tweak = bytes((i * 7 + 3) % 256 for i in range(tweak_len))
        radix = 10
        numerals = [3, 1, 4, 1, 5, 9]
        ct = _ff1.encrypt(self._KEY, tweak, radix, numerals)
        assert ct != numerals  # a genuine permutation, not a silent no-op
        assert _ff1.decrypt(self._KEY, tweak, radix, ct) == numerals
        # Independent second implementation agrees too (not just self-consistent).
        oracle_ct = oracle.oracle_encrypt(self._KEY, tweak, radix, numerals)
        assert oracle_ct == ct
        assert oracle.oracle_decrypt(self._KEY, tweak, radix, oracle_ct) == numerals

    def test_radix_above_standard_ceiling_is_rejected(self):
        """LOW-2: the primitive enforces NIST SP 800-38G Rev.1 2PD's own
        ``radix <= 2**16`` domain bound directly, independent of the
        deployed profile's tighter ``FF1_MAX_RADIX == 64`` cap every caller
        already applies one layer up."""
        radix = _ff1.FF1_STANDARD_MAX_RADIX + 1
        numerals = [0, 1]
        with pytest.raises(_ff1.Ff1Error, match="radix must be"):
            _ff1.encrypt(self._KEY, self._TWEAK, radix, numerals)

    def test_radix_at_standard_ceiling_is_accepted(self):
        """The ceiling itself (``radix == 2**16``) is INCLUSIVE -- LOW-2's
        own wording is ``radix <= 2**16`` -- so it must NOT raise. Kills the
        mutmut survivor that turned ``radix > FF1_STANDARD_MAX_RADIX`` into
        ``>=``, which would wrongly reject exactly this boundary value while
        every OTHER radix (below and above it) still behaves identically
        either way. Calls ``_validate_common`` directly: a real ``encrypt``
        at this radix would need a numeral string long enough to clear the
        FF1 domain floor too, which is a separate (and much larger) concern
        this test isn't about."""
        key = bytes(32)
        assert _ff1._validate_common(key, b"", _ff1.FF1_STANDARD_MAX_RADIX, [0, 1]) == (2, 0)

    def test_key_size_zero_is_rejected(self):
        with pytest.raises(_ff1.Ff1Error, match="16, 24, or 32"):
            _ff1.encrypt(b"", self._TWEAK, 10, [1, 2, 3, 4, 5, 6])

    def test_numeral_string_shorter_than_two_is_rejected(self):
        """NIST FF1's own precondition (n >= 2): a single-numeral string has
        no non-trivial Feistel split (u=0 or v=0)."""
        with pytest.raises(_ff1.Ff1Error, match="length >= 2"):
            _ff1.encrypt(self._KEY, self._TWEAK, 10, [5])
        with pytest.raises(_ff1.Ff1Error, match="length >= 2"):
            _ff1.encrypt(self._KEY, self._TWEAK, 10, [])

    def test_radix_below_two_is_rejected_with_a_specific_message(self):
        """Kills the mutmut survivor that nulled this raise's message
        (``_validate_common``'s ``radix < 2`` branch, distinct from
        ``min_domain_length``'s own separate copy of the same check below)."""
        with pytest.raises(_ff1.Ff1Error, match="radix must be >= 2; got 1"):
            _ff1.encrypt(self._KEY, self._TWEAK, 1, [0, 0])

    def test_out_of_range_message_reports_the_exact_offending_count(self):
        """Kills five distinct mutmut survivors in the out-of-range counting
        expression (`sum(1 for digit in numerals if not (0 <= digit < radix))`):
        the count -> None, the summand 1 -> 2, the condition inverted or
        negated, and both boundary shifts (`0 <` / `<= radix`). A mixed
        numeral list -- one below-range, one at-range (== radix), several
        valid including the 0 and radix-1 edges -- pins the count each of
        those mutations would silently change."""
        radix = 10
        numerals = [0, 5, 9, 10, -1, 3]  # exactly 2 out of range: 10 and -1
        with pytest.raises(_ff1.Ff1Error, match=r"2 of 6 numeral\(s\) are out of range"):
            _ff1.encrypt(self._KEY, self._TWEAK, radix, numerals)


class TestValidateCommonTweakLengthBoundary:
    """The tweak-length ceiling (``len(tweak) >= 2**32``, a uint32 boundary)
    cannot be exercised with a REAL 4-GiB ``bytes`` object. `_validate_common`
    only ever calls ``len(tweak)`` before this check runs (it never iterates
    or concatenates the tweak itself), so a duck-typed ``__len__`` override
    exercises the real boundary check without allocating real memory."""

    class _FakeLen:
        def __init__(self, n: int) -> None:
            self._n = n

        def __len__(self) -> int:
            return self._n

    def test_tweak_length_exactly_at_the_uint32_ceiling_is_rejected(self):
        """Kills three survivors that each widen the ceiling
        (``>`` instead of ``>=``, ``3**32`` instead of ``2**32``,
        ``2**33`` instead of ``2**32``): all three would wrongly ACCEPT a
        tweak of exactly ``2**32`` bytes."""
        key = bytes(32)
        with pytest.raises(_ff1.Ff1Error, match="tweak length must fit in a uint32"):
            _ff1._validate_common(key, self._FakeLen(2**32), 10, [1, 2])

    def test_tweak_length_one_below_the_ceiling_is_accepted(self):
        key = bytes(32)
        assert _ff1._validate_common(key, self._FakeLen(2**32 - 1), 10, [1, 2]) == (
            2,
            2**32 - 1,
        )


class TestByteLenForRadixPower:
    """Direct coverage for `_byte_len_for_radix_power`'s degenerate
    ``length == 0`` case: no real `encrypt`/`decrypt` call ever reaches it
    (the ``n >= 2`` precondition in `_validate_common` guarantees ``v >= 1``
    for every real Feistel split), but the function's own contract -- zero
    numerals need zero bytes -- is worth pinning directly."""

    def test_zero_length_needs_zero_bytes(self):
        assert _ff1._byte_len_for_radix_power(10, 0) == 0


class TestMinDomainLength:
    """Direct unit coverage for `min_domain_length` (round-2 mutation-kill
    pass): every OTHER test in this file exercises it only indirectly, or
    not at all -- `test_exhaustive_six_digit_permutation` hardcodes
    ``length=6`` rather than calling the function -- so mutmut reported
    every mutant here as a trivial "no tests" gap."""

    def test_radix_below_two_is_rejected(self):
        with pytest.raises(ValueError, match="radix must be >= 2; got 1"):
            _ff1.min_domain_length(1)
        with pytest.raises(ValueError, match="radix must be >= 2; got 0"):
            _ff1.min_domain_length(0)

    @pytest.mark.parametrize(
        "radix,expected",
        [(2, 20), (3, 13), (10, 6), (16, 5), (36, 4), (62, 4), (64, 4)],
    )
    def test_known_answer_values(self, radix, expected):
        """radix=2 (the documented ``FF1_MIN_RADIX`` floor) kills the
        mutants that reject it (``radix < 2`` -> ``<= 2`` / ``< 3``).
        radix=10 -> 6 is the EXACT domain-floor boundary
        (``10**6 == 1,000,000 == FF1_MIN_DOMAIN`` exactly): kills the
        ``value < FF1_MIN_DOMAIN`` -> ``<=`` off-by-one mutant, which would
        return 7 instead of 6 at this radix. The full parametrized spread
        also kills every ``length`` accumulator mutant (``+= 1`` -> ``= 1``
        / ``-= 1`` / ``+= 2``): each produces a wrong value at more than one
        of these radices."""
        assert _ff1.min_domain_length(radix) == expected
        assert radix**expected >= _ff1.FF1_MIN_DOMAIN
        assert radix ** (expected - 1) < _ff1.FF1_MIN_DOMAIN

    @pytest.mark.parametrize("radix", [1_000_000, 2_000_000])
    def test_radix_already_at_or_above_the_floor_needs_length_one(self, radix):
        """A radix whose single numeral already covers the domain floor
        returns 1 without the accumulation loop ever running -- kills the
        ``length = 1`` -> ``None``/``2`` mutants, which only diverge from
        the correct value on this exact no-loop-iteration path."""
        assert _ff1.min_domain_length(radix) == 1

    def test_terminates_within_a_bounded_time(self):
        """Liveness guard, GUARDED by SIGALRM (same idiom as
        ``test_ooc_external_sort_generic.py``'s
        ``test_timestamp_ns_key_multipass_does_not_hang``): two round-2
        mutmut survivors (``value = radix`` and ``value /= radix`` in place
        of ``value *= radix``) turn the accumulation loop into an INFINITE
        loop for any realistic radix -- once ``value`` stops growing it
        never crosses ``FF1_MIN_DOMAIN`` again. A plain call would hang the
        whole grading run rather than fail; the alarm turns that hang into
        a clean, fast test failure instead."""

        class _HangError(Exception):
            pass

        def _on_alarm(signum, frame):
            raise _HangError

        has_alarm = hasattr(signal, "SIGALRM")
        if has_alarm:
            old = signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(5)
        try:
            assert _ff1.min_domain_length(10) == 6
        except _HangError:
            pytest.fail("min_domain_length(10) did not terminate within 5s")
        finally:
            if has_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
