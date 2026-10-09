//! FF1 crate-parity gate (C6a plan §3a, acceptance test 1): the `fpe` crate's FF1, over
//! AES-256, must be byte-identical to the Python oracle `decoy_engine.transforms._ff1`
//! across the admitted deployment domain. This is the blocking go/no-go for the whole slice:
//! if the crate diverges anywhere in that domain, the native FPE kernel cannot be built on it.
//!
//! Two independent checks:
//!
//! 1. `crate_matches_oracle_corpus`: every vector in `vectors/fpe_ff1_kat.json` (generated
//!    from the live oracle by `vectors/generate_fpe_kat.py`: preset charsets, EVERY radix
//!    2..=64, lengths `min_domain_length(radix)..256`, leading-zero numerals, the engine
//!    tweak framing, and the three published NIST SP 800-38G FF1 AES-256 samples) encrypts
//!    to the oracle's ciphertext and decrypts back to the plaintext, byte-for-byte.
//!
//! 2. `calculate_b_matches_exact_integer_over_full_grid`: the single most likely divergence
//!    point (plan §3a). The crate computes `b = ceil(v * log2(radix) / 8)` in libm floating
//!    point for non-power-of-two radices, while the oracle uses exact integer arithmetic
//!    (`_byte_len_for_radix_power`: `ceil((radix**v - 1).bit_length() / 8)`). This asserts the
//!    two agree for every `(radix, v)` in `2..=64 x 1..=128`, so no float rounding at a ceil
//!    boundary can hand the crate a different byte count than the oracle anywhere in the domain.

use std::path::PathBuf;

use aes::Aes256;
use fpe::ff1::{FlexibleNumeralString, FF1};
use num_bigint::BigUint;
use serde::Deserialize;

#[derive(Deserialize)]
struct Fixture {
    #[allow(dead_code)]
    format_version: u32,
    vectors: Vec<Vector>,
}

#[derive(Deserialize)]
struct Vector {
    note: String,
    radix: u32,
    key_hex: String,
    tweak_hex: String,
    plaintext: Vec<u16>,
    ciphertext: Vec<u16>,
}

fn hex_to_bytes(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("vectors")
        .join("fpe_ff1_kat.json")
}

#[test]
fn crate_matches_oracle_corpus() {
    let raw = std::fs::read_to_string(fixture_path()).expect("read fpe_ff1_kat.json");
    let fixture: Fixture = serde_json::from_str(&raw).expect("parse fpe_ff1_kat.json");

    // A truncated or empty corpus must not vacuously pass this blocking gate.
    assert!(
        fixture.vectors.len() >= 3000,
        "corpus has only {} vectors; expected the full radix 2..=64 sweep (>=3000)",
        fixture.vectors.len()
    );

    let mut radices_seen = std::collections::BTreeSet::new();
    for v in &fixture.vectors {
        radices_seen.insert(v.radix);
        let key = hex_to_bytes(&v.key_hex);
        assert_eq!(key.len(), 32, "{}: AES-256 requires a 32-byte key", v.note);
        let tweak = hex_to_bytes(&v.tweak_hex);

        // Instantiate AES-256 explicitly (the crate is cipher-generic); plan §3a.
        let ff1 = FF1::<Aes256>::new(&key, v.radix)
            .unwrap_or_else(|e| panic!("{}: FF1::new failed: {e:?}", v.note));

        let ct: Vec<u16> = ff1
            .encrypt(&tweak, &FlexibleNumeralString::from(v.plaintext.clone()))
            .unwrap_or_else(|e| panic!("{}: encrypt failed: {e:?}", v.note))
            .into();
        assert_eq!(
            ct, v.ciphertext,
            "{}: ciphertext diverges from oracle",
            v.note
        );

        let pt: Vec<u16> = ff1
            .decrypt(&tweak, &FlexibleNumeralString::from(v.ciphertext.clone()))
            .unwrap_or_else(|e| panic!("{}: decrypt failed: {e:?}", v.note))
            .into();
        assert_eq!(
            pt, v.plaintext,
            "{}: decrypt does not recover the plaintext",
            v.note
        );
    }

    // Every radix in the deployed profile's [2, 64] range must be represented.
    for radix in 2..=64u32 {
        assert!(
            radices_seen.contains(&radix),
            "corpus is missing radix {radix}; the gate must cover every radix 2..=64"
        );
    }
}

/// The crate's own `calculate_b` logic (ff1.rs::Radix::calculate_b), reproduced here so the grid
/// can compare it against the exact-integer oracle value without the crate exposing it.
fn crate_calculate_b(radix: u32, v: usize) -> usize {
    if radix.count_ones() == 1 {
        let log_radix = (31 - radix.leading_zeros()) as usize;
        (v * log_radix).div_ceil(8)
    } else {
        libm::ceil(v as f64 * libm::log2(f64::from(radix)) / 8f64) as usize
    }
}

/// The oracle's exact-integer `b` (`transforms/_ff1._byte_len_for_radix_power`): the number of
/// bytes to hold any value in `[0, radix**v)`, with no float anywhere.
fn exact_calculate_b(radix: u32, v: usize) -> usize {
    let max_value = BigUint::from(radix).pow(v as u32) - 1u32; // radix>=2, v>=1 => >= 1
    let bits = max_value.bits() as usize; // bit_length of a positive integer
    bits.div_ceil(8)
}

#[test]
fn calculate_b_matches_exact_integer_over_full_grid() {
    for radix in 2..=64u32 {
        for v in 1..=128usize {
            let got = crate_calculate_b(radix, v);
            let want = exact_calculate_b(radix, v);
            assert_eq!(
                got, want,
                "calculate_b divergence at radix={radix} v={v}: crate(float)={got} oracle(exact)={want}"
            );
        }
    }
}
