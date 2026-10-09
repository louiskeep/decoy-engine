//! Span-detection kernel for `text_redact` (C6c-ii), with per-cell ASCII-domain routing.
//!
//! The eight lookaround-free detectors (`email`, `us_phone`, `pan`, `iban`, `ipv4`, `icd10`,
//! `npi`, `url`) run here as linear-time `regex` crate scans with explicit ASCII character
//! classes (`(?-u)` / `(?i-u)`), plus the five ported validators. They run ONLY on cells in the
//! ASCII-safe domain (every code point < 0x80 AND none in 0x1c-0x1f), where Python's
//! `\d`/`\s`/`\w`/ASCII-case-fold classes and the `regex` crate's `(?-u)` classes coincide
//! code-point-for-code-point (plan 2.1/2.2; backed by the class-level enumeration in 7.4). On
//! such a cell the Rust candidate list equals Python's `finditer`+validator list span-for-span,
//! so the full output stays byte-identical.
//!
//! Per cell the kernel returns either the candidate list `[(detector_index, start, end), ...]`
//! in `finditer` order with code-point (scalar-value) offsets, OR `None` meaning "ineligible,
//! Python must handle this cell" (non-ASCII, a 0x1c-0x1f separator, a lone surrogate, or a
//! non-string object). `None` is distinct from `[]` (eligible, no match). The three lookaround
//! detectors (`ssn`, `us_zip`, `street_address`) stay in Python and are not known here.
//!
//! Established methodology: the `regex` crate's per-pattern `find_iter` (non-overlapping,
//! leftmost-first, matching Python's `re.finditer` on these lookaround-free patterns). Validators
//! port the executable behavior of `storm/_validators.py` (the reference), not an idealized
//! standard: Luhn (ISO/IEC 7812-1) with the module's normalization, NPI (CMS Luhn over the 80840
//! prefix), ICD-10 chapter-range table, IBAN (ISO 13616 incremental mod-97), IPv4 octet range.

use regex::Regex;
use std::sync::OnceLock;

#[cfg(feature = "extension-module")]
use pyo3::exceptions::PyValueError;
#[cfg(feature = "extension-module")]
use pyo3::prelude::*;
#[cfg(feature = "extension-module")]
use pyo3::types::{PyList, PyString, PyTuple};

/// The detector-catalog contract id. Bumps when ANY detector's id, order, label, pattern,
/// validator, OR supported flag changes (plan 4.6). The Python mirror
/// (`_text_redact_kernel.CATALOG_VERSION`) must match, or the loader falls back to full Python.
pub const CATALOG_VERSION: i64 = 1;

/// The eight lookaround-free detector ids, in catalog order. The returned `detector_index` is a
/// position in this array; the Python merge maps it back to the id (and its `[REDACTED:<id>]`
/// label). Keep in sync with `_text_redact_kernel.SUPPORTED_IDS`.
pub const SUPPORTED_IDS: [&str; 8] = [
    "email", "us_phone", "pan", "iban", "ipv4", "icd10", "npi", "url",
];

/// The validator each detector applies to a candidate match (None = regex match is sufficient).
#[derive(Clone, Copy, PartialEq, Eq)]
enum Validator {
    None,
    Luhn,
    Iban,
    Ipv4,
    Icd10,
    Npi,
}

fn pattern_for(index: usize) -> &'static str {
    // ASCII-restricted translations of the Python `_*_RE` patterns. `(?-u)` turns `\d`/`\s` into
    // their ASCII classes; `(?i-u)` is ASCII case folding (icd10 only). email/url use only literal
    // character classes, so the Unicode flag is irrelevant and the pattern is copied verbatim.
    match index {
        0 => r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", // email
        1 => r"(?-u)(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?[2-9]\d{2}[\s.-]?\d{4}", // us_phone
        2 => r"(?-u)\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{1,7}",  // pan
        3 => r"(?-u)[A-Z]{2}\d{2}[\sA-Z0-9]{11,34}",            // iban
        4 => r"(?-u)(?:\d{1,3}\.){3}\d{1,3}",                   // ipv4
        5 => r"(?i-u)[A-Z]\d{2}(?:\.?[A-Z0-9]{1,4})?",          // icd10
        6 => r"(?-u)\d{10}",                                    // npi
        7 => r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{4,}", // url
        _ => unreachable!("detector index out of range"),
    }
}

fn validator_for(index: usize) -> Validator {
    match index {
        0 => Validator::None,  // email
        1 => Validator::None,  // us_phone
        2 => Validator::Luhn,  // pan
        3 => Validator::Iban,  // iban
        4 => Validator::Ipv4,  // ipv4
        5 => Validator::Icd10, // icd10
        6 => Validator::Npi,   // npi
        7 => Validator::None,  // url
        _ => unreachable!("detector index out of range"),
    }
}

/// The eight compiled detectors, built once. A compile failure is a build-time bug in a pattern
/// above, surfaced as a panic (caught at the PyO3 boundary) rather than a silent wrong result.
fn compiled() -> &'static [Regex; 8] {
    static REGEXES: OnceLock<[Regex; 8]> = OnceLock::new();
    REGEXES.get_or_init(|| {
        std::array::from_fn(|i| {
            Regex::new(pattern_for(i)).expect("text_redact pattern must compile")
        })
    })
}

/// A cell is eligible iff every code point is ASCII (`< 0x80`) AND none is in 0x1c-0x1f (plan
/// 2.1). A lone surrogate never reaches here: it cannot be decoded to a Rust `&str`, so the
/// caller already routes it to Python as ineligible.
pub fn is_eligible(text: &str) -> bool {
    text.is_ascii() && !text.bytes().any(|b| (0x1c..=0x1f).contains(&b))
}

/// The candidate list for one eligible cell over `indices` (catalog indices, in the oracle's
/// requested order among the eight). Each detector contributes its `find_iter` matches, in order,
/// that pass its validator; finditer advancement is independent of the validator (a rejected
/// match still advances past its own span), exactly as the Python loop does. Offsets are
/// code-point counts; on an ASCII cell byte offsets equal code-point offsets, so the match's byte
/// span is returned directly.
pub fn candidates_for_cell(text: &str, indices: &[usize]) -> Vec<(u32, u32, u32)> {
    let regexes = compiled();
    let mut out: Vec<(u32, u32, u32)> = Vec::new();
    for &idx in indices {
        let validator = validator_for(idx);
        for m in regexes[idx].find_iter(text) {
            if validator != Validator::None && !run_validator(validator, m.as_str()) {
                continue;
            }
            out.push((idx as u32, m.start() as u32, m.end() as u32));
        }
    }
    out
}

fn run_validator(validator: Validator, matched: &str) -> bool {
    match validator {
        Validator::None => true,
        Validator::Luhn => luhn_valid(matched),
        Validator::Iban => iban_valid(matched),
        Validator::Ipv4 => ipv4_valid(matched),
        Validator::Icd10 => icd10_valid(matched),
        Validator::Npi => npi_valid(matched),
    }
}

// ── validators (ports of storm/_validators.py; ASCII-input behavior) ────────────────────────

/// The whitespace set Python's `\s` strips on an eligible cell: `[ \t\n\x0b\x0c\r]`. 0x1c-0x1f
/// (which Python `\s` also matches) never appear in an eligible cell, so they are not listed.
#[inline]
fn is_strip_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// Luhn / mod-10 (`_luhn_valid`): strip `[\s-]`, require >= 13 pure digits, mod-10 checksum.
fn luhn_valid(value: &str) -> bool {
    let digits: Vec<u8> = value
        .bytes()
        .filter(|b| !(is_strip_ws(*b) || *b == b'-'))
        .collect();
    if digits.len() < 13 || !digits.iter().all(u8::is_ascii_digit) {
        return false;
    }
    let mut total: u32 = 0;
    for (i, b) in digits.iter().rev().enumerate() {
        let mut d = (b - b'0') as u32;
        if i % 2 == 1 {
            d *= 2;
            if d > 9 {
                d -= 9;
            }
        }
        total += d;
    }
    total.is_multiple_of(10)
}

/// CMS NPI check digit (`_npi_valid`): strip `[\s-]`, require exactly 10 digits, prepend "80840"
/// to the 9-digit body, modified Luhn (even 0-indexed positions from the right doubled).
fn npi_valid(value: &str) -> bool {
    let digits: Vec<u8> = value
        .bytes()
        .filter(|b| !(is_strip_ws(*b) || *b == b'-'))
        .collect();
    if digits.len() != 10 || !digits.iter().all(u8::is_ascii_digit) {
        return false;
    }
    let mut seq: Vec<u8> = Vec::with_capacity(14);
    seq.extend_from_slice(b"80840");
    seq.extend_from_slice(&digits[..9]);
    let mut total: u32 = 0;
    for (i, b) in seq.iter().rev().enumerate() {
        let mut d = (b - b'0') as u32;
        if i % 2 == 0 {
            d *= 2;
            if d > 9 {
                d -= 9;
            }
        }
        total += d;
    }
    (10 - total % 10) % 10 == (digits[9] - b'0') as u32
}

/// IPv4 dotted-quad (`_ipv4_valid`): exactly four `.`-separated parts, each 1-3 digits in 0-255.
fn ipv4_valid(value: &str) -> bool {
    let parts: Vec<&str> = value.split('.').collect();
    if parts.len() != 4 {
        return false;
    }
    for p in parts {
        let pb = p.as_bytes();
        if pb.is_empty() || pb.len() > 3 || !pb.iter().all(u8::is_ascii_digit) {
            return false;
        }
        let n: u32 = p.parse().expect("1-3 ascii digits parse into u32");
        if n > 255 {
            return false;
        }
    }
    true
}

/// IBAN (`_iban_valid`): strip whitespace (not the hyphen) and uppercase, require length 15-34
/// with a country code and 2-digit check, require country-set membership, then verify ISO 13616
/// mod-97 computed incrementally (equal to `int(rearranged_as_digits) % 97`) equals 1.
fn iban_valid(value: &str) -> bool {
    let cleaned: Vec<u8> = value
        .bytes()
        .filter(|b| !is_strip_ws(*b))
        .map(|b| b.to_ascii_uppercase())
        .collect();
    let n = cleaned.len();
    if !(15..=34).contains(&n) {
        return false;
    }
    if !(cleaned[0].is_ascii_alphabetic()
        && cleaned[1].is_ascii_alphabetic()
        && cleaned[2].is_ascii_digit()
        && cleaned[3].is_ascii_digit())
    {
        return false;
    }
    if !iban_country(cleaned[0], cleaned[1]) {
        return false;
    }
    let mut rem: u32 = 0;
    for &c in cleaned[4..].iter().chain(cleaned[..4].iter()) {
        if c.is_ascii_digit() {
            rem = (rem * 10 + (c - b'0') as u32) % 97;
        } else if c.is_ascii_uppercase() {
            // Letter -> ord(c) - 55 (A=10 .. Z=35), fed as its two decimal digits, matching
            // Python's `str(ord(c) - 55)` string join before the single big-integer mod.
            let v = (c - b'A') as u32 + 10;
            rem = (rem * 10 + v / 10) % 97;
            rem = (rem * 10 + v % 10) % 97;
        } else {
            return false;
        }
    }
    rem == 1
}

/// ICD-10-CM structural + chapter-range validity (`_icd10_valid`): strip surrounding whitespace,
/// drop dots, uppercase; require 3-7 chars with a chapter letter + 2-digit category in the
/// chapter's inclusive range.
fn icd10_valid(value: &str) -> bool {
    let v: Vec<u8> = value
        .trim_matches(|c: char| is_strip_ws(c as u8) && c.is_ascii())
        .bytes()
        .filter(|b| *b != b'.')
        .map(|b| b.to_ascii_uppercase())
        .collect();
    if !(3..=7).contains(&v.len())
        || !v[0].is_ascii_alphabetic()
        || !v[1].is_ascii_digit()
        || !v[2].is_ascii_digit()
    {
        return false;
    }
    let cat = (v[1] - b'0') as u32 * 10 + (v[2] - b'0') as u32;
    match icd10_chapter_range(v[0]) {
        Some((lo, hi)) => cat >= lo && cat <= hi,
        None => false,
    }
}

/// ICD-10-CM chapter category ranges, mirroring `_ICD10_CHAPTERS`.
fn icd10_chapter_range(chapter: u8) -> Option<(u32, u32)> {
    let hi = match chapter {
        b'A' | b'B' | b'C' => 99,
        b'D' => 89,
        b'E' => 89,
        b'F' => return Some((1, 99)),
        b'G' => 99,
        b'H' => 95,
        b'I' | b'J' => 99,
        b'K' => 95,
        b'L' | b'M' | b'N' | b'O' => 99,
        b'P' => 96,
        b'Q' | b'R' | b'S' => 99,
        b'T' => 88,
        b'U' => 85,
        b'V' | b'W' | b'X' | b'Y' | b'Z' => 99,
        _ => return None,
    };
    Some((0, hi))
}

/// ISO 3166 alpha-2 codes of IBAN-issuing countries, mirroring `_IBAN_COUNTRIES` (SWIFT 2024).
fn iban_country(c0: u8, c1: u8) -> bool {
    const COUNTRIES: [&[u8; 2]; 78] = [
        b"AD", b"AE", b"AL", b"AT", b"AZ", b"BA", b"BE", b"BG", b"BH", b"BR", b"BY", b"CH", b"CR",
        b"CY", b"CZ", b"DE", b"DK", b"DO", b"EE", b"EG", b"ES", b"FI", b"FO", b"FR", b"GB", b"GE",
        b"GI", b"GL", b"GR", b"GT", b"HR", b"HU", b"IE", b"IL", b"IQ", b"IS", b"IT", b"JO", b"KW",
        b"KZ", b"LB", b"LC", b"LI", b"LT", b"LU", b"LV", b"MC", b"MD", b"ME", b"MK", b"MR", b"MT",
        b"MU", b"NL", b"NO", b"PK", b"PL", b"PS", b"PT", b"QA", b"RO", b"RS", b"RU", b"SA", b"SC",
        b"SE", b"SI", b"SK", b"SM", b"ST", b"SV", b"TL", b"TN", b"TR", b"UA", b"VA", b"VG", b"XK",
    ];
    COUNTRIES.iter().any(|cc| cc[0] == c0 && cc[1] == c1)
}

// ── test / class-equivalence helpers ────────────────────────────────────────────────────────

/// Members of an ASCII class, for the class-equivalence proof (plan 7.4). Returns the ASCII code
/// points (0-127) the named class matches under the exact flags the kernel uses. `"icd_ci"` is
/// the `(?i-u)[A-Z]` class the icd10 detector relies on.
#[cfg(feature = "extension-module")]
fn class_members(name: &str) -> Option<Vec<u32>> {
    let pat = match name {
        "d" => r"(?-u)^\d$",
        "s" => r"(?-u)^\s$",
        "w" => r"(?-u)^\w$",
        "icd_ci" => r"(?i-u)^[A-Z]$",
        _ => return None,
    };
    let re = Regex::new(pat).expect("class probe pattern must compile");
    let mut out = Vec::new();
    for cp in 0u32..128 {
        let ch = char::from_u32(cp).expect("0-127 is valid");
        let buf = ch.to_string();
        if re.is_match(&buf) {
            out.push(cp);
        }
    }
    Some(out)
}

// ── PyO3 boundary ───────────────────────────────────────────────────────────────────────────

#[cfg(feature = "extension-module")]
fn requested_indices(detector_ids: &Bound<'_, PyAny>) -> PyResult<Vec<usize>> {
    let mut indices = Vec::new();
    for item in detector_ids.try_iter()? {
        let id: String = item?.extract()?;
        match SUPPORTED_IDS.iter().position(|s| *s == id) {
            Some(i) => indices.push(i),
            None => {
                return Err(PyValueError::new_err(format!(
                    "unknown_detector_id: {id:?} is not a Rust-supported text_redact detector"
                )))
            }
        }
    }
    Ok(indices)
}

/// `text_redact_candidates(values, detector_ids, catalog_version) -> list[list[(int,int,int)] | None]`
///
/// Per cell: the eligible candidate list `[(detector_index, start, end), ...]` in finditer order,
/// or `None` (ineligible: non-ASCII, a 0x1c-0x1f separator, a lone surrogate, or a non-string).
/// `detector_ids` are the requested Rust-supported ids in the oracle's requested order; a mismatched
/// `catalog_version` raises `catalog_version_mismatch` so the loader can fall back to full Python.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_candidates(
    py: Python<'_>,
    values: &Bound<'_, PyAny>,
    detector_ids: &Bound<'_, PyAny>,
    catalog_version: i64,
) -> PyResult<Py<PyAny>> {
    if catalog_version != CATALOG_VERSION {
        return Err(PyValueError::new_err(format!(
            "catalog_version_mismatch: caller sent {catalog_version}, kernel is {CATALOG_VERSION}"
        )));
    }
    let indices = requested_indices(detector_ids)?;
    let out = PyList::empty(py);
    for item in values.try_iter()? {
        let item = item?;
        if item.is_none() {
            out.append(py.None())?;
            continue;
        }
        // A non-string, or a str carrying a lone surrogate (no UTF-8 view), is ineligible.
        let Ok(py_str) = item.cast::<PyString>() else {
            out.append(py.None())?;
            continue;
        };
        let Ok(text) = py_str.to_str() else {
            out.append(py.None())?;
            continue;
        };
        if !is_eligible(text) {
            out.append(py.None())?;
            continue;
        }
        let cands = candidates_for_cell(text, &indices);
        let cell = PyList::empty(py);
        for (idx, start, end) in cands {
            cell.append(PyTuple::new(py, [idx, start, end])?)?;
        }
        out.append(cell)?;
    }
    Ok(out.into_any().unbind())
}

/// The kernel's catalog version, for the loader's agreement check.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_catalog_version() -> i64 {
    CATALOG_VERSION
}

/// The kernel's Rust-supported detector ids, in catalog (index) order, for the agreement check.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_supported_ids() -> Vec<String> {
    SUPPORTED_IDS.iter().map(|s| (*s).to_string()).collect()
}

/// Run one ported validator by name, for the validator KATs (plan 7.3).
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_validate(name: &str, value: &str) -> PyResult<bool> {
    let validator = match name {
        "luhn" => Validator::Luhn,
        "iban" => Validator::Iban,
        "ipv4" => Validator::Ipv4,
        "icd10" => Validator::Icd10,
        "npi" => Validator::Npi,
        _ => return Err(PyValueError::new_err(format!("unknown validator {name:?}"))),
    };
    Ok(run_validator(validator, value))
}

/// Whether a value is in the ASCII-safe domain (plan 2.1), for the routing-predicate test (7.1).
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_is_eligible(value: &Bound<'_, PyAny>) -> bool {
    let Ok(py_str) = value.cast::<PyString>() else {
        return false;
    };
    match py_str.to_str() {
        Ok(text) => is_eligible(text),
        Err(_) => false,
    }
}

/// The ASCII code points a named class matches under the kernel's flags, for the class-level
/// equivalence proof (plan 7.4). Names: `"d"`, `"s"`, `"w"`, `"icd_ci"`.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_class_members(name: &str) -> PyResult<Vec<u32>> {
    class_members(name).ok_or_else(|| PyValueError::new_err(format!("unknown class {name:?}")))
}

#[cfg(feature = "extension-module")]
pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(text_redact_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_catalog_version, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_supported_ids, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_validate, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_is_eligible, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_class_members, m)?)?;
    Ok(())
}
