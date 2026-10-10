//! NIST SP 800-38G FF1 format-preserving encryption, native kernel (C6a).
//!
//! The raw FF1 Feistel comes from the `fpe` crate (v0.7.0), instantiated over AES-256 from the
//! audited `aes` crate: no cipher is hand-rolled here. The `fpe` crate's FF1 is proven
//! byte-identical to the Python oracle `decoy_engine.transforms._ff1` across the admitted
//! deployment domain (preset charsets, every radix 2..=64, lengths `min_domain_length..256`,
//! leading-zero numerals, the engine tweak framing, and the published NIST SP 800-38G FF1
//! AES-256 samples) by the blocking parity gate `tests/kat_fpe.rs`; the suspected float
//! `calculate_b` divergence point agrees over the whole radix 2..=64 x v 1..=128 grid.
//!
//! This module ports the deployable-profile WRAPPER from `decoy_engine.transforms.fpe`
//! (`_fpe_value` / `_fpe_pure_value` / `_permute` / `_luhn_check_digit`) and the per-row
//! orchestration from `decoy_engine.execution.native._crypto_reference._ReferenceFpe._run`
//! (the non-checksum path only: checksum modes decline to the Python oracle, C6a plan §3g).
//! Config-level fail-closed cases (duplicate / degenerate charset, missing namespace, tweak
//! construction) are raised in Python against the shipped handler before this kernel runs; this
//! kernel reproduces only the three per-row UNENCRYPTABLE status codes, in the handler's pinned
//! validation order, so the SAME value yields the SAME code (plan §3d).
//!
//! The FF1 AES-256 key is derived here, reusing the shipped envelope
//! `derive(mask_key, namespace, b"ff1-key/v1")` (see `derive.rs`), so key material is passed in
//! per call and never cached or logged. The tweak bytes are built in Python by
//! `transforms.fpe.build_ff1_tweak` and passed in verbatim.

use std::collections::HashMap;

use aes::Aes256;
use arrow_array::{Array, StringArray};
use fpe::ff1::{FlexibleNumeralString, FF1};

use crate::derive::{derive, DeriveError};
use crate::threads::shared_native_pool;

/// The FF1 key-derivation label, pinned to `transforms.fpe.FF1_KEY_LABEL` (`b"ff1-key/v1"`).
const FF1_KEY_LABEL: &[u8] = b"ff1-key/v1";

/// Deployable-profile constants, pinned to `transforms/_ff1.py`.
const FF1_MIN_DOMAIN: u128 = 1_000_000;
const FF1_MIN_RADIX: u32 = 2;
const FF1_MAX_RADIX: u32 = 64;
const FF1_MAX_LEN: usize = 256;
const FF1_MAX_TWEAK_LEN: usize = 256;
const PRINTABLE_ASCII_MIN: u32 = 0x21;
const PRINTABLE_ASCII_MAX: u32 = 0x7E;

/// The three per-row status codes this kernel can emit, matching what
/// `_fpe.strategy_code_for_unencryptable` returns for the reference's own raises (plan §3d).
pub const CODE_UNENCRYPTABLE_VALUE: &str = "fpe_unencryptable_value";
pub const CODE_UNENCRYPTABLE_DOMAIN: &str = "fpe_unencryptable_domain";
pub const CODE_UNENCRYPTABLE_LENGTH: &str = "fpe_unencryptable_length";

/// A redacted per-row FPE failure: the row index and a fixed status code, never the cell value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FpeRowError {
    pub row_index: usize,
    pub code: &'static str,
}

/// The batch output plus the ordered per-row errors (in source row order).
#[derive(Debug)]
pub struct FpeArrayResult {
    pub values: StringArray,
    pub errors: Vec<FpeRowError>,
}

/// Everything that fails a whole FPE batch before (or independent of) per-row processing.
#[derive(Debug)]
pub enum FpeKernelError {
    /// Missing or empty mask key (fail-before-output; no row has been touched). Mirrors
    /// `_require_mask_key`, which the reference calls once before deriving the key.
    MaskKeyRequired,
    /// Seed length / namespace validation from the shared `derive` envelope (same codes and
    /// order as the reference's own `derive(mask_key, namespace, FF1_KEY_LABEL)`).
    Derive(DeriveError),
    /// The input array was not an Arrow `Utf8` (`pa.string()`) column. Admission routes only the
    /// exact string type this kernel reproduces to the native path; any other type is a wiring
    /// error here (declined to the oracle upstream).
    InputNotString,
    /// The resolved FF1 key was not 32 bytes (AES-256). `derive` always returns 32 bytes, so this
    /// is an unreachable defensive guard rather than a reachable input condition.
    KeyNotAes256,
    /// A thread-pool construction failure surfaced from the shared native pool.
    Pool(DeriveError),
}

impl From<DeriveError> for FpeKernelError {
    fn from(e: DeriveError) -> Self {
        FpeKernelError::Derive(e)
    }
}

impl FpeKernelError {
    pub fn code(&self) -> &str {
        match self {
            FpeKernelError::MaskKeyRequired => "mask_key_required",
            FpeKernelError::Derive(e) => e.code,
            FpeKernelError::InputNotString => "fpe_input_not_string",
            FpeKernelError::KeyNotAes256 => "fpe_key_not_aes256",
            FpeKernelError::Pool(e) => e.code,
        }
    }

    pub fn detail(&self) -> String {
        match self {
            FpeKernelError::MaskKeyRequired => {
                "mask_key is required and must be non-empty; refusing to emit unkeyed output"
                    .to_string()
            }
            FpeKernelError::Derive(e) => e.detail.clone(),
            FpeKernelError::InputNotString => {
                "fpe native kernel accepts only a pa.string() array; got another Arrow type"
                    .to_string()
            }
            FpeKernelError::KeyNotAes256 => {
                "derived FF1 key was not 32 bytes (AES-256)".to_string()
            }
            FpeKernelError::Pool(e) => e.detail.clone(),
        }
    }
}

/// Smallest numeral-string length L with `radix**L >= FF1_MIN_DOMAIN`, by exact integer search
/// (no float), reproducing `transforms/_ff1.min_domain_length`.
fn min_domain_length(radix: u32) -> usize {
    let radix = radix as u128;
    let mut length = 1usize;
    let mut value = radix;
    while value < FF1_MIN_DOMAIN {
        length += 1;
        value *= radix;
    }
    length
}

/// Luhn check digit for a digit string, reproducing `transforms.fpe._luhn_check_digit`. `body`
/// is composed entirely of ASCII decimal digits (luhn mode is gated to an all-digit charset).
fn luhn_check_digit(body: &str) -> char {
    let mut total: u32 = 0;
    for (i, ch) in body.chars().rev().enumerate() {
        let mut n = ch.to_digit(10).expect("luhn body is all decimal digits");
        if i % 2 == 0 {
            n *= 2;
            if n > 9 {
                n -= 9;
            }
        }
        total += n;
    }
    let digit = (10 - total % 10) % 10;
    char::from_digit(digit, 10).expect("0..=9 is a valid decimal digit")
}

/// The resolved, per-batch FPE configuration shared by every row.
struct ResolvedConfig<'a> {
    /// Charset as code points (index -> symbol), the FF1 numeral alphabet.
    charset: &'a [char],
    /// Symbol -> numeral index.
    char_to_idx: HashMap<char, u16>,
    radix: u32,
    preserve_separators: bool,
    validate_luhn: bool,
    /// Whether every charset symbol is printable ASCII (0x21..=0x7E). Checked once; the per-row
    /// path reports `fpe_unencryptable_length` when it is false, matching `_permute`.
    charset_printable_ascii: bool,
    /// Whether the charset has a duplicate symbol (len(set) != len). Python pre-rejects this as a
    /// StrategyError, so it is defensive here; reported as `fpe_unencryptable_length` to match
    /// `_permute`'s own duplicate check ordering if it is ever reached.
    charset_has_duplicate: bool,
    tweak_len: usize,
}

/// FF1-permute (or invert) one in-charset string, reproducing `transforms.fpe._permute`'s pinned
/// validation order 1-6, returning the output string or the row status code of the first violation.
fn permute(
    cfg: &ResolvedConfig<'_>,
    ff1: &FF1<Aes256>,
    tweak: &[u8],
    s: &[char],
    forward: bool,
) -> Result<String, &'static str> {
    let n = s.len();
    if n == 0 {
        // `_permute` returns the input unchanged for the empty string. Unreachable here (callers
        // filter empties), kept for faithful parity.
        return Ok(String::new());
    }
    // Order mirrors `_permute`: (1) key size is guaranteed 32 by the caller; (2) radix bound;
    // (3) charset uniqueness; (4) printable-ASCII; (5) domain floor; (6) max length; then the
    // tweak-length and body-membership backstops, then the FF1 call.
    if !(FF1_MIN_RADIX..=FF1_MAX_RADIX).contains(&cfg.radix) {
        return Err(CODE_UNENCRYPTABLE_LENGTH);
    }
    if cfg.charset_has_duplicate {
        return Err(CODE_UNENCRYPTABLE_LENGTH);
    }
    if !cfg.charset_printable_ascii {
        return Err(CODE_UNENCRYPTABLE_LENGTH);
    }
    if n < min_domain_length(cfg.radix) {
        return Err(CODE_UNENCRYPTABLE_DOMAIN);
    }
    if n > FF1_MAX_LEN {
        return Err(CODE_UNENCRYPTABLE_LENGTH);
    }
    if cfg.tweak_len > FF1_MAX_TWEAK_LEN {
        return Err(CODE_UNENCRYPTABLE_LENGTH);
    }
    let mut numerals: Vec<u16> = Vec::with_capacity(n);
    for ch in s {
        match cfg.char_to_idx.get(ch) {
            Some(&idx) => numerals.push(idx),
            None => return Err(CODE_UNENCRYPTABLE_LENGTH),
        }
    }
    let input = FlexibleNumeralString::from(numerals);
    let result = if forward {
        ff1.encrypt(tweak, &input)
    } else {
        ff1.decrypt(tweak, &input)
    };
    match result {
        Ok(out) => {
            let out_numerals: Vec<u16> = out.into();
            Ok(out_numerals
                .into_iter()
                .map(|i| cfg.charset[i as usize])
                .collect())
        }
        // The FF1 crate rejected the input. All reachable rejections map to the oracle's
        // `fpe.unencryptable_length` (the `Ff1Error` -> FpeUnencryptableError wrapping in
        // `_permute`); the validation above already excludes the domain/length cases that would
        // otherwise reach it, so this is the final backstop.
        Err(_) => Err(CODE_UNENCRYPTABLE_LENGTH),
    }
}

/// FF1-permute (or invert) a pure in-charset value, reproducing `_fpe_pure_value` (non-checksum):
/// luhn mode permutes the body and appends the recomputed check digit; otherwise permutes whole.
fn fpe_pure_value(
    cfg: &ResolvedConfig<'_>,
    ff1: &FF1<Aes256>,
    tweak: &[u8],
    s: &[char],
    forward: bool,
) -> Result<String, &'static str> {
    if s.is_empty() {
        return Ok(String::new());
    }
    if cfg.validate_luhn && s.len() >= 2 {
        let body = permute(cfg, ff1, tweak, &s[..s.len() - 1], forward)?;
        let check = luhn_check_digit(&body);
        let mut out = body;
        out.push(check);
        return Ok(out);
    }
    permute(cfg, ff1, tweak, s, forward)
}

/// FF1-transform one non-null, non-empty value, reproducing `transforms.fpe._fpe_value`
/// (non-checksum): the preserve_separators branch reinserts out-of-charset characters verbatim;
/// the non-preserve branch fails closed on any out-of-charset character.
fn fpe_one(
    cfg: &ResolvedConfig<'_>,
    ff1: &FF1<Aes256>,
    tweak: &[u8],
    value: &str,
    forward: bool,
) -> Result<String, &'static str> {
    let chars: Vec<char> = value.chars().collect();
    if cfg.preserve_separators {
        let positions: Vec<usize> = chars
            .iter()
            .enumerate()
            .filter(|(_, ch)| cfg.char_to_idx.contains_key(ch))
            .map(|(i, _)| i)
            .collect();
        if positions.is_empty() {
            // No in-charset character: nothing to format-preserving-encrypt. `_fpe_value` raises
            // FpeUnencryptableError with no explicit code -> `fpe_unencryptable_value`.
            return Err(CODE_UNENCRYPTABLE_VALUE);
        }
        let body_in: Vec<char> = positions.iter().map(|&i| chars[i]).collect();
        let body_out = fpe_pure_value(cfg, ff1, tweak, &body_in, forward)?;
        let body_out_chars: Vec<char> = body_out.chars().collect();
        if body_out_chars.len() != positions.len() {
            // Length invariant (`_fpe_value`): unreachable for the non-checksum path, kept as the
            // fail-closed backstop against a mismatched reinsertion.
            return Err(CODE_UNENCRYPTABLE_VALUE);
        }
        let mut result = chars;
        for (pos, ch) in positions.into_iter().zip(body_out_chars) {
            result[pos] = ch;
        }
        return Ok(result.into_iter().collect());
    }
    // preserve_separators = false: any out-of-charset character fails closed.
    if chars.iter().any(|ch| !cfg.char_to_idx.contains_key(ch)) {
        return Err(CODE_UNENCRYPTABLE_VALUE);
    }
    fpe_pure_value(cfg, ff1, tweak, &chars, forward)
}

/// Deterministic contiguous `(row_lo, row_hi)` ranges, `min(threads, len)` of them, balanced by
/// row count. FPE's per-row cost is dominated by the (independent, deterministic) FF1 work, so an
/// even row-count split suffices; the output is byte-identical at every thread count.
fn row_ranges(len: usize, threads: usize) -> Vec<(usize, usize)> {
    if len == 0 {
        return vec![];
    }
    let nranges = threads.clamp(1, len);
    let per = len.div_ceil(nranges);
    let mut ranges = Vec::with_capacity(nranges);
    let mut lo = 0usize;
    while lo < len {
        let hi = (lo + per).min(len);
        ranges.push((lo, hi));
        lo = hi;
    }
    ranges
}

/// Process rows `lo..hi`: each row is null (-> None), empty (-> Some("")), a successful transform
/// (-> Some(output)), or a per-row failure (-> None plus a recorded error), in row order.
fn fill_range(
    cfg: &ResolvedConfig<'_>,
    ff1: &FF1<Aes256>,
    tweak: &[u8],
    array: &StringArray,
    forward: bool,
    lo: usize,
    hi: usize,
) -> (Vec<Option<String>>, Vec<FpeRowError>) {
    let mut out: Vec<Option<String>> = Vec::with_capacity(hi - lo);
    let mut errors: Vec<FpeRowError> = Vec::new();
    for i in lo..hi {
        if array.is_null(i) {
            out.push(None);
            continue;
        }
        let value = array.value(i);
        if value.is_empty() {
            // Missing-data policy shared with the strategy and unmask: no cipher, no warning.
            out.push(Some(String::new()));
            continue;
        }
        match fpe_one(cfg, ff1, tweak, value, forward) {
            Ok(s) => out.push(Some(s)),
            Err(code) => {
                out.push(None);
                errors.push(FpeRowError { row_index: i, code });
            }
        }
    }
    (out, errors)
}

/// FF1-encrypt (or decrypt) one `pa.string()` column, returning the output array and the ordered
/// per-row errors. The FF1 AES-256 key is derived once from `(mask_key, namespace)`; a
/// missing/empty key or an invalid seed/namespace fails closed before any row is processed.
///
/// `charset` is the resolved numeral alphabet as code points (Python resolves and config-validates
/// it before the call); `tweak` is the engine tweak framing built in Python. Output is
/// byte-identical at every thread count: each row's transform depends only on its own value and
/// the shared key/tweak/config.
#[allow(clippy::too_many_arguments)]
pub fn fpe_transform_array(
    array: &dyn Array,
    mask_key: Option<&[u8]>,
    namespace: &str,
    tweak: &[u8],
    charset: &[char],
    preserve_separators: bool,
    validate_luhn: bool,
    forward: bool,
    threads: usize,
) -> Result<FpeArrayResult, FpeKernelError> {
    let mask_key = match mask_key {
        Some(k) if !k.is_empty() => k,
        _ => return Err(FpeKernelError::MaskKeyRequired),
    };
    let string_array = array
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or(FpeKernelError::InputNotString)?;

    // Derive the FF1 key once, up front, unconditionally (matching `_ReferenceFpe._run`, which
    // derives before its row loop even for an all-null column, so a bad seed/namespace fails here
    // rather than silently on an empty batch).
    let key = derive(mask_key, namespace, FF1_KEY_LABEL)?;
    if key.len() != 32 {
        return Err(FpeKernelError::KeyNotAes256);
    }

    let radix = charset.len() as u32;
    let char_to_idx: HashMap<char, u16> = charset
        .iter()
        .enumerate()
        .map(|(i, &ch)| (ch, i as u16))
        .collect();
    let cfg = ResolvedConfig {
        charset,
        charset_has_duplicate: char_to_idx.len() != charset.len(),
        charset_printable_ascii: charset
            .iter()
            .all(|ch| (PRINTABLE_ASCII_MIN..=PRINTABLE_ASCII_MAX).contains(&(*ch as u32))),
        char_to_idx,
        radix,
        preserve_separators,
        validate_luhn,
        tweak_len: tweak.len(),
    };

    // The FF1 instance is reused across every row. `FF1::new` only fails for a radix outside
    // [2, 2^16]; the deployed profile caps at 64 and Python rejects a degenerate (<2) charset, so
    // a radix in range always constructs. A radix above 64 is caught per-row as
    // `fpe_unencryptable_length` (matching `_permute`), so build the cipher only when in range.
    let ff1 = if (FF1_MIN_RADIX..=FF1_MAX_RADIX).contains(&radix) {
        FF1::<Aes256>::new(&key, radix).ok()
    } else {
        None
    };

    let len = string_array.len();
    let ranges = row_ranges(len, threads);

    let per_range: Vec<(Vec<Option<String>>, Vec<FpeRowError>)> = if ranges.len() <= 1 {
        ranges
            .into_iter()
            .map(|(lo, hi)| run_one_range(&cfg, ff1.as_ref(), tweak, string_array, forward, lo, hi))
            .collect()
    } else {
        use rayon::prelude::*;
        let pool = shared_native_pool().map_err(FpeKernelError::Pool)?;
        pool.install(|| {
            ranges
                .into_par_iter()
                .map(|(lo, hi)| {
                    run_one_range(&cfg, ff1.as_ref(), tweak, string_array, forward, lo, hi)
                })
                .collect()
        })
    };

    let mut values: Vec<Option<String>> = Vec::with_capacity(len);
    let mut errors: Vec<FpeRowError> = Vec::new();
    for (range_values, range_errors) in per_range {
        values.extend(range_values);
        errors.extend(range_errors);
    }
    Ok(FpeArrayResult {
        values: StringArray::from(values),
        errors,
    })
}

/// A range's rows, with a radix-out-of-range column short-circuiting every non-empty row to the
/// `fpe_unencryptable_length` code (the cipher could not be built; `_permute`'s radix guard).
fn run_one_range(
    cfg: &ResolvedConfig<'_>,
    ff1: Option<&FF1<Aes256>>,
    tweak: &[u8],
    array: &StringArray,
    forward: bool,
    lo: usize,
    hi: usize,
) -> (Vec<Option<String>>, Vec<FpeRowError>) {
    match ff1 {
        Some(ff1) => fill_range(cfg, ff1, tweak, array, forward, lo, hi),
        None => {
            let mut out = Vec::with_capacity(hi - lo);
            let mut errors = Vec::new();
            for i in lo..hi {
                if array.is_null(i) {
                    out.push(None);
                    continue;
                }
                if array.value(i).is_empty() {
                    out.push(Some(String::new()));
                    continue;
                }
                out.push(None);
                errors.push(FpeRowError {
                    row_index: i,
                    code: CODE_UNENCRYPTABLE_LENGTH,
                });
            }
            (out, errors)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn digits() -> Vec<char> {
        "0123456789".chars().collect()
    }

    fn key32() -> Vec<u8> {
        (0..32u8).collect()
    }

    #[test]
    fn empty_mask_key_fails_closed() {
        let array = StringArray::from(vec![Some("123456789")]);
        let ds = digits();
        let err = fpe_transform_array(&array, Some(&[]), "ns", b"", &ds, true, false, true, 1)
            .unwrap_err();
        assert!(matches!(err, FpeKernelError::MaskKeyRequired));
    }

    #[test]
    fn null_and_empty_pass_through() {
        let array = StringArray::from(vec![None, Some(""), Some("123456789")]);
        let ds = digits();
        let key = key32();
        let r =
            fpe_transform_array(&array, Some(&key), "ns", b"", &ds, true, false, true, 1).unwrap();
        assert!(r.values.is_null(0));
        assert_eq!(r.values.value(1), "");
        assert_ne!(r.values.value(2), "123456789");
        assert_eq!(r.errors.len(), 0);
    }

    #[test]
    fn round_trips() {
        let array = StringArray::from(vec![Some("123-45-6789"), Some("000111222")]);
        let ds = digits();
        let key = key32();
        let tweak = b"\x01\x01\x00\x03ssn";
        let enc = fpe_transform_array(&array, Some(&key), "ns", tweak, &ds, true, false, true, 1)
            .unwrap();
        let ct = StringArray::from(vec![
            Some(enc.values.value(0).to_string()),
            Some(enc.values.value(1).to_string()),
        ]);
        let dec =
            fpe_transform_array(&ct, Some(&key), "ns", tweak, &ds, true, false, false, 1).unwrap();
        assert_eq!(dec.values.value(0), "123-45-6789");
        assert_eq!(dec.values.value(1), "000111222");
    }

    #[test]
    fn preserve_separators_keeps_structure() {
        let array = StringArray::from(vec![Some("123-45-6789")]);
        let ds = digits();
        let key = key32();
        let out =
            fpe_transform_array(&array, Some(&key), "ns", b"", &ds, true, false, true, 1).unwrap();
        let v = out.values.value(0);
        assert_eq!(v.len(), "123-45-6789".len());
        assert_eq!(&v[3..4], "-");
        assert_eq!(&v[6..7], "-");
    }

    #[test]
    fn no_in_charset_is_unencryptable_value() {
        let array = StringArray::from(vec![Some("----------")]);
        let ds = digits();
        let key = key32();
        let out =
            fpe_transform_array(&array, Some(&key), "ns", b"", &ds, true, false, true, 1).unwrap();
        assert!(out.values.is_null(0));
        assert_eq!(out.errors[0].code, CODE_UNENCRYPTABLE_VALUE);
    }

    #[test]
    fn below_domain_is_unencryptable_domain() {
        // digits min_domain_length == 6; a 5-digit value is below the floor.
        let array = StringArray::from(vec![Some("12345")]);
        let ds = digits();
        let key = key32();
        let out =
            fpe_transform_array(&array, Some(&key), "ns", b"", &ds, true, false, true, 1).unwrap();
        assert_eq!(out.errors[0].code, CODE_UNENCRYPTABLE_DOMAIN);
    }

    #[test]
    fn non_preserve_out_of_charset_fails_closed() {
        let array = StringArray::from(vec![Some("123-45-6789")]);
        let ds = digits();
        let key = key32();
        let out =
            fpe_transform_array(&array, Some(&key), "ns", b"", &ds, false, false, true, 1).unwrap();
        assert!(out.values.is_null(0));
        assert_eq!(out.errors[0].code, CODE_UNENCRYPTABLE_VALUE);
    }

    #[test]
    fn thread_count_never_changes_output() {
        let values: Vec<Option<String>> = (0..500)
            .map(|i| match i % 7 {
                0 => None,
                1 => Some(String::new()),
                _ => Some(format!("{:09}", i * 12345)),
            })
            .collect();
        let array = StringArray::from(values);
        let ds = digits();
        let key = key32();
        let tweak = b"\x01\x01\x00\x03ssn";
        let baseline =
            fpe_transform_array(&array, Some(&key), "ns", tweak, &ds, true, false, true, 1)
                .unwrap();
        for threads in [2usize, 3, 4, 8] {
            let out = fpe_transform_array(
                &array,
                Some(&key),
                "ns",
                tweak,
                &ds,
                true,
                false,
                true,
                threads,
            )
            .unwrap();
            assert_eq!(out.values, baseline.values, "threads={threads}");
            assert_eq!(out.errors, baseline.errors, "threads={threads}");
        }
    }

    #[test]
    fn luhn_appends_valid_check_digit() {
        let array = StringArray::from(vec![Some("4111111111111111")]);
        let ds = digits();
        let key = key32();
        let out =
            fpe_transform_array(&array, Some(&key), "ns", b"", &ds, true, true, true, 1).unwrap();
        let v = out.values.value(0);
        assert_eq!(v.len(), 16);
        // The appended check digit must be the Luhn digit of the first 15 characters.
        let body = &v[..15];
        assert_eq!(v.chars().last().unwrap(), luhn_check_digit(body));
    }

    /// Locks the whole native pipeline (FF1 key derivation + tweak + FF1 + charset) to the
    /// Python-pinned `FPE_KAT` vectors in `decoy_engine.execution.native._crypto_ext`, so a drift
    /// anywhere in the kernel (not just the raw FF1 the crate-parity gate covers) is caught here.
    /// mask_key = bytes(range(32)); namespace = "people.ssn"; tweak = build_ff1_tweak(COLUMN, "ssn").
    #[test]
    fn matches_python_fpe_kat() {
        let key: Vec<u8> = (0..32u8).collect();
        let ns = "people.ssn";
        let tweak_ssn: &[u8] = &[1, 1, 0, 3, b's', b's', b'n'];
        let ds = digits();

        let check = |pt: &str, expected: &str, luhn: bool| {
            let array = StringArray::from(vec![Some(pt)]);
            let out =
                fpe_transform_array(&array, Some(&key), ns, tweak_ssn, &ds, true, luhn, true, 1)
                    .unwrap();
            assert_eq!(out.values.value(0), expected, "FPE_KAT pt={pt}");
            assert!(out.errors.is_empty());
        };
        check("123456789", "566184649", false);
        check("123-45-6789", "566-18-4649", false);
        check("4111111111111111", "3663799520739755", true);
    }

    #[test]
    fn min_domain_length_matches_reference_values() {
        assert_eq!(min_domain_length(2), 20);
        assert_eq!(min_domain_length(10), 6);
        assert_eq!(min_domain_length(16), 5);
        assert_eq!(min_domain_length(36), 4);
        assert_eq!(min_domain_length(62), 4);
        assert_eq!(min_domain_length(64), 4);
    }
}
