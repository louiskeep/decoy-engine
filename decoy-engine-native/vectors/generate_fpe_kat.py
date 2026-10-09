"""Generate the FF1 crate-parity KAT corpus from the live Python oracle.

The Rust `tests/kat_fpe.rs` test reads `fpe_ff1_kat.json` and asserts the `fpe`
crate's FF1 (instantiated over AES-256) reproduces, byte-for-byte, the ciphertext
`decoy_engine.transforms._ff1.encrypt`/`decrypt` produces across the admitted
deployment domain: the preset charsets, EVERY radix 2..=64, lengths
`min_domain_length(radix)..256`, leading-zero numerals, and the engine's tweak
framing (plus the published NIST SP 800-38G FF1 AES-256 samples run verbatim).

This is the C6a plan's §3a blocking go/no-go gate. The corpus comes from RUNNING
the shipped primitive, never a hand-derived guess, so it is correct by
construction against the Python oracle the native kernel must match. Re-run only
when `_ff1.py` itself changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from decoy_engine.transforms import _ff1
from decoy_engine.transforms.fpe import (
    FF1_TWEAK_SCOPE_COLUMN,
    FF1_TWEAK_SCOPE_JOIN_GROUP,
    build_ff1_tweak,
)

# AES-256 (32-byte) key, explicit per plan §3a. A fixed, non-trivial key.
_KEY = bytes((i * 7 + 3) & 0xFF for i in range(32))


def _numerals(radix: int, length: int, *, leading_zeros: int) -> list[int]:
    out = [0] * min(leading_zeros, length)
    for i in range(len(out), length):
        out.append((i * 31 + 7) % radix)
    return out


def _lengths_for(radix: int) -> list[int]:
    min_len = _ff1.min_domain_length(radix)
    candidates = {
        min_len,
        min_len + 1,
        min_len + 2,
        min_len + 3,
        min_len + 7,
        min_len + 16,
        2 * min_len,
        32,
        64,
        100,
        128,
        200,
        255,
        256,
    }
    return sorted(n for n in candidates if min_len <= n <= _ff1.FF1_MAX_LEN and n >= 2)


def _vector(radix: int, numerals: list[int], tweak: bytes, note: str) -> dict[str, Any]:
    ct = _ff1.encrypt(_KEY, tweak, radix, numerals)
    # Round-trip self-check so the corpus can never pin a non-invertible vector.
    back = _ff1.decrypt(_KEY, tweak, radix, ct)
    if back != numerals:
        raise AssertionError(f"oracle round-trip failed: radix={radix} len={len(numerals)} {note}")
    return {
        "note": note,
        "radix": radix,
        "key_hex": _KEY.hex(),
        "tweak_hex": tweak.hex(),
        "plaintext": numerals,
        "ciphertext": ct,
    }


def build_vectors() -> list[dict[str, Any]]:
    column_tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, "ssn")
    group_tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_JOIN_GROUP, "people_group")
    # Maximum-size identity that still fits the deployed FF1_MAX_TWEAK_LEN (256 bytes).
    # tweak = 1(version)+1(scope)+2(len)+identity; identity max = 256 - 4 = 252 bytes.
    max_tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, "x" * 252)
    preset_radices = {10, 26, 36, 62}

    vectors: list[dict[str, Any]] = []
    for radix in range(2, 65):
        bulk_tweaks = [(b"", "empty_tweak"), (column_tweak, "column_tweak")]
        if radix in preset_radices:
            bulk_tweaks += [
                (group_tweak, "join_group_tweak"),
                (max_tweak, "max_tweak"),
            ]
        for length in _lengths_for(radix):
            plain = _numerals(radix, length, leading_zeros=0)
            lz = _numerals(radix, length, leading_zeros=min(5, length - 1))
            for tweak, tname in bulk_tweaks:
                vectors.append(_vector(radix, plain, tweak, f"r{radix}_l{length}_{tname}"))
                vectors.append(
                    _vector(radix, lz, tweak, f"r{radix}_l{length}_{tname}_leadingzeros")
                )
    return vectors


_AES256_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3CEF4359D8D580AA4F7F036D6F04FC6A94")
_DECIMAL = "0123456789"
_ALPHANUM36 = "0123456789abcdefghijklmnopqrstuvwxyz"

# The three published NIST SP 800-38G FF1 AES-256 samples (Samples #7/#8/#9),
# constants taken verbatim from the engine's own NIST-locked test suite
# (tests/unit/transforms/test_ff1_primitive.py) to remove transcription risk.
_NIST_AES256_SAMPLES = [
    ("nist_sample7_aes256_radix10", 10, _DECIMAL, "", "0123456789", "6657667009"),
    ("nist_sample8_aes256_radix10", 10, _DECIMAL, "39383736353433323130", "0123456789", "1001623463"),
    (
        "nist_sample9_aes256_radix36",
        36,
        _ALPHANUM36,
        "3737373770717273373737",
        "0123456789abcdefghi",
        "xs8a0azh2avyalyzuwd",
    ),
]


def _to_numerals(text: str, alphabet: str) -> list[int]:
    index = {ch: i for i, ch in enumerate(alphabet)}
    return [index[ch] for ch in text]


def nist_vectors() -> list[dict[str, Any]]:
    """Pin the three published NIST FF1 AES-256 samples through the oracle.

    `kat_fpe.rs` runs the same key/tweak/plaintext through the `fpe` crate and
    asserts the crate reproduces the published ciphertext. The oracle re-derives
    the ciphertext here and the generator asserts it against the published value,
    so a wrong plaintext or tweak fails the build rather than pinning a bad vector.
    """
    out: list[dict[str, Any]] = []
    for note, radix, alphabet, tweak_hex, pt, ct in _NIST_AES256_SAMPLES:
        tweak = bytes.fromhex(tweak_hex)
        pt_numerals = _to_numerals(pt, alphabet)
        ct_numerals = _to_numerals(ct, alphabet)
        oracle_ct = _ff1.encrypt(_AES256_KEY, tweak, radix, pt_numerals)
        if oracle_ct != ct_numerals:
            raise AssertionError(f"NIST {note}: oracle ct {oracle_ct} != published {ct_numerals}")
        if _ff1.decrypt(_AES256_KEY, tweak, radix, ct_numerals) != pt_numerals:
            raise AssertionError(f"NIST {note} round-trip failed")
        out.append(
            {
                "note": note,
                "radix": radix,
                "key_hex": _AES256_KEY.hex(),
                "tweak_hex": tweak_hex,
                "plaintext": pt_numerals,
                "ciphertext": ct_numerals,
            }
        )
    return out


def main() -> None:
    vectors = nist_vectors() + build_vectors()
    fixture = {"format_version": 1, "vectors": vectors}
    out_path = Path(__file__).parent / "fpe_ff1_kat.json"
    out_path.write_text(json.dumps(fixture, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {len(vectors)} vectors to {out_path}")


if __name__ == "__main__":
    main()
