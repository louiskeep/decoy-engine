//! Keyed derivation: HKDF-SHA256 then HMAC-SHA256, reproducing the engine's shipped
//! `reference_keyed_derivation` byte for byte (`decoy_engine.determinism._derive.derive`).
//!
//! References:
//! - RFC 5869 (HKDF): <https://www.rfc-editor.org/rfc/rfc5869.html>
//! - RFC 2104 (HMAC): <https://www.rfc-editor.org/rfc/rfc2104.html>
//! - RFC 4231 (HMAC-SHA-256 test vectors): <https://www.rfc-editor.org/rfc/rfc4231.html>
//!
//! Both primitives come from the reviewed RustCrypto crates (`hkdf`, `hmac`, `sha2`); this
//! module rolls no hash or MAC construction of its own, only the envelope that binds a
//! `(mask_key, namespace, canonical_source)` triple to the shipped byte frame:
//!
//! ```text
//! HMAC_key   = HKDF-SHA256(IKM=mask_key, salt=b"decoy-engine/determinism/v1",
//!                          info=namespace.encode("utf-8"), length=32)
//! HMAC_input = SEED_PROTOCOL_VERSION byte
//!            | 4-byte BE namespace length | namespace UTF-8
//!            | 4-byte BE source length    | canonical source bytes
//! output     = HMAC-SHA256(HMAC_key, HMAC_input)
//! ```
//!
//! The version byte and salt are pinned to the shipped protocol
//! (`decoy_engine.determinism._derive`, `SEED_PROTOCOL_VERSION = 6`); a native re-implementation
//! never changes them, since Phase 2 changes no logical value.

use hkdf::Hkdf;
use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

/// The salt bound into every HKDF call, pinned to the shipped protocol.
const SALT: &[u8] = b"decoy-engine/determinism/v1";

/// The version byte mixed into every HMAC frame, pinned to the shipped protocol.
/// Bumping it is a determinism-family decision made in the Python envelope, not here.
const SEED_PROTOCOL_VERSION: u8 = 6;

/// The two seed lengths the shipped reference accepts
/// (`decoy_engine.determinism._derive._SEED_LENGTHS`): 8 bytes for the no-secret `job_seed`
/// path, 32 bytes for a real `KeyProvider`-derived `mask_key`. Checked HERE, inside `derive()`,
/// not by the batch caller before its row loop: the reference validates this (and the
/// namespace) only when a non-null value actually reaches `derive()`, so an empty or
/// all-null batch with an otherwise-invalid key or namespace returns cleanly with no error,
/// exactly like the reference. A batch with at least one non-null row still fails closed the
/// moment that row is derived.
const SEED_LENGTHS: [usize; 2] = [8, 32];

/// A derivation input or arithmetic bound was violated. Never carries key or source bytes
/// (the redacted-error contract): callers format `code` and a value-free `detail` only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeriveError {
    pub code: &'static str,
    pub detail: String,
}

impl DeriveError {
    fn new(code: &'static str, detail: impl Into<String>) -> Self {
        Self {
            code,
            detail: detail.into(),
        }
    }
}

impl std::fmt::Display for DeriveError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

impl std::error::Error for DeriveError {}

/// RFC 5869 HKDF-SHA256 extract-then-expand, deriving the 32-byte per-namespace HMAC key.
///
/// A namespace produces the same key on every call (pure function of
/// `(mask_key, namespace)`); callers processing a whole column may cache it, but this
/// module itself carries no cache, matching the Python side's scalar `derive()`.
fn hkdf_key(mask_key: &[u8], namespace: &str) -> [u8; 32] {
    let hk = Hkdf::<Sha256>::new(Some(SALT), mask_key);
    let mut okm = [0u8; 32];
    // RFC 5869 length bound is 255 * HashLen; 32 bytes is always in range, so this
    // never fails. Mirrors hkdf_sha256()'s one-shot extract+expand in `_hkdf.py`.
    hk.expand(namespace.as_bytes(), &mut okm)
        .expect("32-byte HKDF-SHA256 output is always within the RFC 5869 length bound");
    okm
}

/// Validate that a byte length fits the frame's 4-byte length prefix, failing closed with
/// `code` rather than truncating or wrapping when it does not.
///
/// Split out from `build_frame` as a plain `usize -> Result<u32, _>` function so a test can
/// drive the overflow branch directly with a synthetic length: `derive()` accepts arbitrary
/// slices and strings, so a namespace or canonical source over 4 GiB is a real, reachable input
/// the reference must fail closed on, not a branch that can be waved off as unreachable just
/// because no test actually allocates 4 GiB to reach it honestly.
fn checked_frame_length(len: usize, code: &'static str, what: &str) -> Result<u32, DeriveError> {
    u32::try_from(len).map_err(|_| DeriveError::new(code, format!("{what} exceeds u32 bytes")))
}

/// Build the length-prefixed HMAC frame: version byte, namespace, canonical source.
///
/// Length-prefixing (not a delimiter) is what makes `namespace || source` injective: without
/// it, `("ab", "c")` and `("a", "bc")` would concatenate to the same bytes despite being
/// logically distinct derivation inputs (the framing-ambiguity KAT vectors exercise exactly
/// this). Lengths are checked, never truncated: a length that cannot fit a `u32` fails closed
/// rather than silently wrapping and colliding with a shorter input.
fn build_frame(namespace: &str, canonical_source: &[u8]) -> Result<Vec<u8>, DeriveError> {
    let namespace_bytes = namespace.as_bytes();
    let ns_len = checked_frame_length(
        namespace_bytes.len(),
        "namespace_length_overflow",
        "namespace",
    )?;
    let src_len = checked_frame_length(
        canonical_source.len(),
        "source_length_overflow",
        "canonical source",
    )?;

    let mut frame = Vec::with_capacity(1 + 4 + namespace_bytes.len() + 4 + canonical_source.len());
    frame.push(SEED_PROTOCOL_VERSION);
    frame.extend_from_slice(&ns_len.to_be_bytes());
    frame.extend_from_slice(namespace_bytes);
    frame.extend_from_slice(&src_len.to_be_bytes());
    frame.extend_from_slice(canonical_source);
    Ok(frame)
}

/// Return the 32-byte HMAC-SHA256 output for `(mask_key, namespace, canonical_source)`.
///
/// Pure function; byte-identical to `decoy_engine.determinism.derive(mask_key, namespace,
/// canonicalize_derive_source(value))` for the same inputs, INCLUDING its validation order:
/// seed length is checked first, then namespace emptiness, matching
/// `decoy_engine.determinism._derive.derive` exactly so a batch with both a wrong-length key
/// and an empty namespace fails with the same code the reference would report.
///
/// `mask_key` non-emptiness (`MaskKeyRequiredError` / `BatchError::MaskKeyRequired`) is a
/// SEPARATE, coarser guard callers enforce before touching a single row (mirroring
/// `_require_mask_key`, which the reference calls once before its loop); this function only
/// re-validates the LENGTH, since a caller could pass a non-empty key of the wrong length.
pub fn derive(
    mask_key: &[u8],
    namespace: &str,
    canonical_source: &[u8],
) -> Result<[u8; 32], DeriveError> {
    if !SEED_LENGTHS.contains(&mask_key.len()) {
        return Err(DeriveError::new(
            "seed_wrong_length",
            format!(
                "mask_key must be 8 (job_seed) or 32 (mask_key) bytes; got {}",
                mask_key.len()
            ),
        ));
    }
    if namespace.is_empty() {
        return Err(DeriveError::new(
            "namespace_empty",
            "namespace must be non-empty",
        ));
    }
    let key = hkdf_key(mask_key, namespace);
    let frame = build_frame(namespace, canonical_source)?;
    let mut mac = <Hmac<Sha256> as KeyInit>::new_from_slice(&key)
        .expect("HMAC-SHA256 accepts a key of any length, including this fixed 32-byte one");
    mac.update(&frame);
    Ok(mac.finalize().into_bytes().into())
}

/// The number of hex characters a 32-byte digest produces, unrounded (`HEX_LEN` bytes of
/// scratch always suffice for `hex_token_into`, whatever `truncate` is).
pub const HEX_LEN: usize = 64;

/// Hex-encode a derived 32-byte digest directly into `buf` (no heap allocation), applying
/// Python's `token[:truncate]` slice semantics, and return the resulting slice.
///
/// The reference (`_ReferenceKeyedDerivation.derive_batch`) does `token[:truncate]` on the
/// 64-character hex string with `truncate: int | None`, and Python slicing is total: `None`
/// keeps everything, a non-negative stop clamps to the string length (never an error, even
/// past the end), and a NEGATIVE stop counts back from the end, clamping at zero rather than
/// raising (`"abcd"[:-10] == ""`). `truncate` is therefore a signed count here, not `usize`,
/// so the native kernel can reproduce this exactly instead of rejecting a value the reference
/// accepts. hex is ASCII, so a character-index slice and a byte-index slice are identical.
///
/// Writing into a caller-owned stack buffer, rather than building an owned `String` per row,
/// is what keeps a whole-column derive pass from allocating one heap string per row on top of
/// the batch's own output buffer (the transient-scratch allocation bound).
pub fn hex_token_into<'a>(
    digest: &[u8; 32],
    truncate: Option<isize>,
    buf: &'a mut [u8; HEX_LEN],
) -> &'a str {
    const HEX_DIGITS: &[u8; 16] = b"0123456789abcdef";
    for (i, byte) in digest.iter().enumerate() {
        buf[i * 2] = HEX_DIGITS[(byte >> 4) as usize];
        buf[i * 2 + 1] = HEX_DIGITS[(byte & 0x0f) as usize];
    }
    let len = python_slice_stop(HEX_LEN, truncate);
    // SAFETY: every byte written above is one of the 16 ASCII hex-digit characters, so the
    // slice is valid UTF-8 by construction; no other byte pattern is ever placed in `buf`.
    unsafe { std::str::from_utf8_unchecked(&buf[..len]) }
}

/// Python's `s[:stop]` stop index for a string of length `total_len`, total over every `stop`
/// (no panic, no error, ever -- exactly like Python slicing):
/// - `None`: the whole string.
/// - `stop >= 0`: `min(stop, total_len)`.
/// - `stop < 0`: `max(0, total_len + stop)` (counts back from the end; an index that lands
///   before the start clamps to 0 rather than wrapping or erroring).
fn python_slice_stop(total_len: usize, stop: Option<isize>) -> usize {
    match stop {
        None => total_len,
        Some(t) if t >= 0 => (t as usize).min(total_len),
        // `t` is negative here; `total_len as isize` is always tiny (HEX_LEN == 64) relative to
        // isize's range, so this addition never overflows regardless of how negative `t` is.
        Some(t) => (total_len as isize + t).max(0) as usize,
    }
}

/// Convenience owned-`String` form of `hex_token_into`, for callers that are not iterating a
/// whole column (tests, the proptest harness) where one heap allocation per call is fine.
pub fn hex_token(digest: &[u8; 32], truncate: Option<isize>) -> String {
    let mut buf = [0u8; HEX_LEN];
    hex_token_into(digest, truncate, &mut buf).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn checked_frame_length_accepts_a_realistic_length() {
        assert_eq!(
            checked_frame_length(100, "namespace_length_overflow", "namespace").unwrap(),
            100
        );
    }

    #[test]
    fn checked_frame_length_accepts_the_u32_max_boundary_exactly() {
        assert_eq!(
            checked_frame_length(
                u32::MAX as usize,
                "source_length_overflow",
                "canonical source"
            )
            .unwrap(),
            u32::MAX
        );
    }

    /// `derive()` accepts arbitrary slices and strings, so a length one byte past `u32::MAX`
    /// is a real input, not a hypothetical one; it must fail closed with the caller's code
    /// rather than truncate or wrap. `usize` on this build's target is 64 bits, so this
    /// synthetic length is always representable without needing to allocate it.
    #[test]
    fn checked_frame_length_rejects_a_length_that_does_not_fit_u32() {
        let too_big = (u32::MAX as usize) + 1;
        let err =
            checked_frame_length(too_big, "namespace_length_overflow", "namespace").unwrap_err();
        assert_eq!(err.code, "namespace_length_overflow");
        assert!(err.detail.contains("namespace"));
    }

    #[test]
    fn derive_error_display_reports_code_and_detail() {
        let err = DeriveError::new("some_code", "some detail");
        assert_eq!(err.to_string(), "some_code: some detail");
    }

    proptest! {
        /// `python_slice_stop` must stay within `[0, total_len]` for every `isize` stop, not
        /// just the boundary values pinned as examples below: this sweeps the whole domain the
        /// PyO3 boundary's own huge-magnitude clamp (`extract_truncate` in arrow_ffi.rs) can
        /// hand it, so an arithmetic regression (an off-by-one, a wraparound) can't hide behind
        /// a gap between the pinned examples.
        #[test]
        fn python_slice_stop_stays_in_bounds_for_any_isize(stop in proptest::option::of(any::<isize>())) {
            let result = python_slice_stop(HEX_LEN, stop);
            prop_assert!(result <= HEX_LEN);
            if let Some(t) = stop {
                if t >= 0 {
                    prop_assert_eq!(result, (t as usize).min(HEX_LEN));
                }
            } else {
                prop_assert_eq!(result, HEX_LEN);
            }
        }

        /// Monotonicity: a stop that is no smaller than another must never keep FEWER
        /// characters. A sign-handling regression (e.g. the negative branch losing its `max(0,
        /// ...)` clamp) would violate this even where the boundary examples below don't happen
        /// to catch it.
        #[test]
        fn python_slice_stop_is_monotonic_in_stop(a in any::<isize>(), b in any::<isize>()) {
            let (lo, hi) = if a <= b { (a, b) } else { (b, a) };
            prop_assert!(
                python_slice_stop(HEX_LEN, Some(lo)) <= python_slice_stop(HEX_LEN, Some(hi))
            );
        }
    }

    #[test]
    fn derive_rejects_empty_namespace() {
        let err = derive(&[7u8; 32], "", b"anything").unwrap_err();
        assert_eq!(err.code, "namespace_empty");
    }

    #[test]
    fn derive_accepts_the_two_reference_seed_lengths() {
        for len in [8usize, 32] {
            let key = vec![0u8; len];
            assert!(
                derive(&key, "ns", b"x").is_ok(),
                "length {len} must be accepted"
            );
        }
    }

    #[test]
    fn derive_rejects_every_other_seed_length() {
        for len in [1usize, 7, 9, 16, 20, 31, 33, 64] {
            let key = vec![0u8; len];
            let err =
                derive(&key, "ns", b"x").expect_err(&format!("length {len} must be rejected"));
            assert_eq!(err.code, "seed_wrong_length");
        }
    }

    /// The reference checks seed length BEFORE namespace emptiness, so a call with both wrong
    /// must report `seed_wrong_length`, never `namespace_empty`.
    #[test]
    fn derive_checks_seed_length_before_namespace() {
        let err = derive(&[0u8; 16], "", b"x").unwrap_err();
        assert_eq!(err.code, "seed_wrong_length");
    }

    #[test]
    fn hex_token_truncate_beyond_length_is_a_no_op() {
        let digest = [0xabu8; 32];
        let full = hex_token(&digest, None);
        assert_eq!(hex_token(&digest, Some(200)), full);
    }

    /// Python's `token[:truncate]` for a negative `truncate`: counts back from the end,
    /// clamping at zero rather than erroring or wrapping. Table matches the reference exactly
    /// (`"x" * 64`, indices below computed the same way CPython computes a negative slice
    /// stop): -1 keeps 63 chars, -63 keeps 1, -64 and anything more negative keep 0.
    #[test]
    fn hex_token_negative_truncate_matches_python_slice_semantics() {
        let digest = [0xabu8; 32];
        let full = hex_token(&digest, None);
        assert_eq!(full.len(), 64);
        assert_eq!(hex_token(&digest, Some(-1)), full[..63]);
        assert_eq!(hex_token(&digest, Some(-63)), full[..1]);
        assert_eq!(hex_token(&digest, Some(-64)), "");
        assert_eq!(hex_token(&digest, Some(-65)), "");
        assert_eq!(hex_token(&digest, Some(isize::MIN)), "");
    }

    // RFC 4231 SS4.2-4.8 HMAC-SHA256 test cases 1-4, 6, 7 (5 needs truncation semantics this
    // primitive layer doesn't apply, so it's out of scope here). Each (key_hex, data, digest_hex)
    // triple is transcribed from the RFC text and cross-checked against Python's stdlib `hmac`
    // (an independent implementation) before being pinned here, so a transcription slip in
    // either language would show up as a mismatch between the two, not a shared blind spot.
    const RFC4231_CASES: &[(&str, &[u8], &str)] = &[
        (
            "0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b",
            b"Hi There",
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7",
        ),
        (
            "4a656665",
            b"what do ya want for nothing?",
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
        ),
        (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            &[0xdd; 50],
            "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe",
        ),
        (
            "0102030405060708090a0b0c0d0e0f10111213141516171819",
            &[0xcd; 50],
            "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b",
        ),
        (
            // 131-byte key: longer than the SHA-256 block size (64 bytes), so HMAC hashes the
            // key down first (RFC 2104 SS2) before using it -- the case this vector exists to pin.
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            b"Test Using Larger Than Block-Size Key - Hash Key First",
            "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54",
        ),
        (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            b"This is a test using a larger than block-size key and a larger than block-size data. The key needs to be hashed before being used by the HMAC algorithm.",
            "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2",
        ),
    ];

    #[test]
    fn rfc4231_hmac_sha256_vectors() {
        for (key_hex, data, expected_hex) in RFC4231_CASES {
            let key = hex_literal(key_hex);
            let mut mac = <Hmac<Sha256> as KeyInit>::new_from_slice(&key).unwrap();
            mac.update(data);
            let expected = hex_literal(expected_hex);
            assert_eq!(
                mac.finalize().into_bytes().as_slice(),
                expected.as_slice(),
                "RFC 4231 vector mismatch for key {key_hex}"
            );
        }
    }

    fn hex_literal(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }

    // RFC 5869 Appendix A test cases 1-3 (SHA-256), the same constants pinned in the shipped
    // Python `_hkdf.py` test suite (`tests/unit/determinism/test_hkdf.py`). Exercised here at
    // the raw primitive layer (`hkdf::Hkdf`), independent of this module's own frame-building,
    // matching how the Python side pins `hkdf_extract`/`hkdf_expand` against the same vectors.
    #[test]
    fn rfc5869_test_case_1_hkdf_sha256() {
        let ikm = hex_literal(&"0b".repeat(22));
        let salt = hex_literal("000102030405060708090a0b0c");
        let info = hex_literal("f0f1f2f3f4f5f6f7f8f9");
        let expected = hex_literal(
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865",
        );
        let hk = Hkdf::<Sha256>::new(Some(&salt), &ikm);
        let mut okm = vec![0u8; 42];
        hk.expand(&info, &mut okm).unwrap();
        assert_eq!(okm, expected);
    }

    #[test]
    fn rfc5869_test_case_2_hkdf_sha256_long_inputs() {
        let ikm = hex_literal(concat!(
            "000102030405060708090a0b0c0d0e0f",
            "101112131415161718191a1b1c1d1e1f",
            "202122232425262728292a2b2c2d2e2f",
            "303132333435363738393a3b3c3d3e3f",
            "404142434445464748494a4b4c4d4e4f",
        ));
        let salt = hex_literal(concat!(
            "606162636465666768696a6b6c6d6e6f",
            "707172737475767778797a7b7c7d7e7f",
            "808182838485868788898a8b8c8d8e8f",
            "909192939495969798999a9b9c9d9e9f",
            "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf",
        ));
        let info = hex_literal(concat!(
            "b0b1b2b3b4b5b6b7b8b9babbbcbdbebf",
            "c0c1c2c3c4c5c6c7c8c9cacbcccdcecf",
            "d0d1d2d3d4d5d6d7d8d9dadbdcdddedf",
            "e0e1e2e3e4e5e6e7e8e9eaebecedeeef",
            "f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff",
        ));
        let expected = hex_literal(concat!(
            "b11e398dc80327a1c8e7f78c596a4934",
            "4f012eda2d4efad8a050cc4c19afa97c",
            "59045a99cac7827271cb41c65e590e09",
            "da3275600c2f09b8367793a9aca3db71",
            "cc30c58179ec3e87c14c01d5c1f3434f",
            "1d87",
        ));
        let hk = Hkdf::<Sha256>::new(Some(&salt), &ikm);
        let mut okm = vec![0u8; 82];
        hk.expand(&info, &mut okm).unwrap();
        assert_eq!(okm, expected);
    }

    #[test]
    fn rfc5869_test_case_3_hkdf_sha256_zero_length_salt_and_info() {
        let ikm = hex_literal(&"0b".repeat(22));
        let info: &[u8] = b"";
        let expected = hex_literal(
            "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8",
        );
        // RFC 5869 SS2.2: a missing salt is treated as HashLen zero bytes. The `hkdf` crate
        // applies that default when `None` is passed, matching the RFC exactly (the Python side
        // instead requires callers to pass the equivalent explicit zero-byte salt -- a
        // Decoy-specific defensive policy in `_hkdf.py`, not part of the RFC vector itself).
        let hk = Hkdf::<Sha256>::new(None, &ikm);
        let mut okm = vec![0u8; 42];
        hk.expand(info, &mut okm).unwrap();
        assert_eq!(okm, expected);
    }
}
