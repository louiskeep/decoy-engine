//! Span-detection kernel for `text_redact` (C6c-ii + lookaround port), with per-cell ASCII-domain
//! routing.
//!
//! The eight lookaround-free detectors (`email`, `us_phone`, `pan`, `iban`, `ipv4`, `icd10`,
//! `npi`, `url`) run here as linear-time `regex` crate scans with explicit ASCII character
//! classes (`(?-u)` / `(?i-u)`), plus the five ported validators. The three lookaround/
//! backtracking detectors (`ssn`, `us_zip`, `street_address`) use Python lookahead/lookbehind and
//! ordered-alternation backtracking that the linear-time `regex` crate cannot express, so they run
//! as hand-written byte scanners (plan 2, "moving them to Rust via an explicit advancement search
//! is a possible later slice" -- this is that slice). `fancy-regex` was tried first and rejected:
//! its backtracking engine is 1.6-2.6x SLOWER than Python's C `re` on these patterns, which erases
//! the native win and fails the plan's latency floor on the lookaround-only workload (7.7); the
//! scanners instead beat Python. Every scanner reproduces its Python pattern's exact finditer
//! semantics (leftmost, non-overlapping, greedy with the same ordered-alternation precedence),
//! verified span-for-span against the oracle by a large differential corpus
//! (`tests/native/test_c6c_ii_text_redact_rust.py`, the byte-parity gate).
//!
//! All eleven run ONLY on cells in the ASCII-safe domain (every code point < 0x80 AND none in
//! 0x1c-0x1f). There Python's `\d`/`\s`/`\w`/`\b`/ASCII-case-fold constructs coincide with the
//! scanners' ASCII byte predicates: `\d` is `[0-9]`, `\w` is `[0-9A-Za-z_]`, `\b` is the
//! transition between them, ASCII case-fold is `A-Z`<->`a-z`, and `\s` is `[ \t\n\x0b\x0c\r]` --
//! Python `\s` would also include 0x1c-0x1f, the exact code points the predicate excludes (plan
//! 2.1/2.2; backed by the class-level enumeration in 7.4). So the Rust candidate list equals
//! Python's `finditer`+validator list span-for-span, and the full output stays byte-identical.
//!
//! Per cell the kernel returns either the candidate list `[(detector_index, start, end), ...]`
//! in `finditer` order with code-point (scalar-value) offsets, OR `None` meaning "ineligible,
//! Python must handle this cell" (non-ASCII, a 0x1c-0x1f separator, a lone surrogate, or a
//! non-string object). `None` is distinct from `[]` (eligible, no match).
//!
//! Established methodology: the `regex` crate's per-pattern `find_iter` (non-overlapping,
//! leftmost-first, matching Python's `re.finditer` on the lookaround-free patterns) and, for the
//! three lookaround patterns, hand scanners that replay Python `re`'s leftmost/greedy/ordered-
//! alternation advancement directly over ASCII bytes. Validators port the executable behavior of
//! `storm/_validators.py` (the reference), not an idealized standard: Luhn (ISO/IEC 7812-1) with
//! the module's normalization, NPI (CMS Luhn over the 80840 prefix), ICD-10 chapter-range table,
//! IBAN (ISO 13616 incremental mod-97), IPv4 octet range.

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
/// v2 adds `ssn`/`us_zip`/`street_address` to the supported set (the lookaround port).
pub const CATALOG_VERSION: i64 = 2;

/// The eleven supported detector ids, in catalog order. The returned `detector_index` is a
/// position in this array; the Python merge maps it back to the id (and its `[REDACTED:<id>]`
/// label). The first eight are lookaround-free (`regex` crate); the last three are the
/// lookaround detectors (hand-written scanners), appended so the existing eight keep their
/// indices. Keep in sync with `_text_redact_kernel.SUPPORTED_IDS`.
pub const SUPPORTED_IDS: [&str; 11] = [
    "email",
    "us_phone",
    "pan",
    "iban",
    "ipv4",
    "icd10",
    "npi",
    "url",
    "ssn",
    "us_zip",
    "street_address",
];

/// The number of catalog entries, for fixed-size per-detector arrays (e.g. `text_mask`'s
/// per-index `verbatim` map). A const expression so those arrays track the catalog automatically.
pub const CATALOG_LEN: usize = SUPPORTED_IDS.len();

/// How many leading catalog entries are the lookaround-free `regex`-crate detectors; the rest are
/// the hand-written lookaround scanners. The split is an index boundary, not a reordering.
const LOOKAROUND_FREE_COUNT: usize = 8;

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

/// The eight lookaround-free compiled detectors, built once. A compile failure is a build-time bug
/// in a pattern above, surfaced as a panic (caught at the PyO3 boundary) rather than a silent wrong
/// result.
fn compiled() -> &'static [Regex; LOOKAROUND_FREE_COUNT] {
    static REGEXES: OnceLock<[Regex; LOOKAROUND_FREE_COUNT]> = OnceLock::new();
    REGEXES.get_or_init(|| {
        std::array::from_fn(|i| {
            Regex::new(pattern_for(i)).expect("text_redact pattern must compile")
        })
    })
}

// ── lookaround detectors: hand-written ASCII byte scanners (plan 2) ───────────────────────────
//
// Each scanner runs ONLY on an eligible (all-ASCII, no 0x1c-0x1f) cell, so byte offsets equal
// code-point offsets and the ASCII predicates below coincide with Python's classes (module doc).
// They reproduce `re.finditer`: scan start positions left to right; at a position find the
// leftmost greedy/ordered match; on a match emit (start, end) and resume from end; else advance
// one. These patterns never match zero-width (ssn/zip need >=5 digits, street needs a house
// number), so advancement is always forward.

/// `\w` on ASCII: `[0-9A-Za-z_]`.
#[inline]
fn is_ascii_word(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// Python `\s` restricted to the ASCII-safe domain: `[ \t\n\x0b\x0c\r]`. 0x1c-0x1f (which Python
/// `\s` also matches) never appear in an eligible cell, so they are not listed.
#[inline]
fn is_ascii_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// Case-insensitive ASCII equality of a byte slice against a lowercase literal (suffix / unit).
#[inline]
fn ascii_ci_eq(hay: &[u8], needle: &str) -> bool {
    let nb = needle.as_bytes();
    hay.len() == nb.len()
        && hay
            .iter()
            .zip(nb)
            .all(|(h, m)| h.to_ascii_lowercase() == *m)
}

/// Collect the lookaround detector `idx`'s spans into `out`, in finditer order.
fn scan_lookaround(idx: usize, text: &str, out: &mut Vec<(u32, u32, u32)>) {
    let b = text.as_bytes();
    match idx {
        8 => scan_ssn(b, idx as u32, out),
        9 => scan_us_zip(b, idx as u32, out),
        10 => scan_street(b, idx as u32, out),
        _ => unreachable!("lookaround detector index out of range"),
    }
}

/// ssn: `(?!000|666|9\d{2})\d{3}-?(?!00)\d{2}-?(?!0000)\d{4}` (_SSN_RE). Fixed-shape with two
/// greedy optional hyphens; no backtracking changes the result (a hyphen, when present, must be
/// consumed because the following field is digits), so each start position is a direct test.
fn scan_ssn(b: &[u8], idx: u32, out: &mut Vec<(u32, u32, u32)>) {
    let n = b.len();
    let mut p = 0;
    while p < n {
        if let Some(end) = ssn_match_at(b, n, p) {
            out.push((idx, p as u32, end as u32));
            p = end;
        } else {
            p += 1;
        }
    }
}

fn ssn_match_at(b: &[u8], n: usize, p: usize) -> Option<usize> {
    // area: three digits, not 000 / 666 / 9xx (the `9\d{2}` lookahead == a leading '9').
    if p + 3 > n
        || !b[p].is_ascii_digit()
        || !b[p + 1].is_ascii_digit()
        || !b[p + 2].is_ascii_digit()
    {
        return None;
    }
    let area = &b[p..p + 3];
    if area == b"000" || area == b"666" || area[0] == b'9' {
        return None;
    }
    let mut q = p + 3;
    if q < n && b[q] == b'-' {
        q += 1;
    }
    // group: two digits, not 00.
    if q + 2 > n || !b[q].is_ascii_digit() || !b[q + 1].is_ascii_digit() {
        return None;
    }
    if b[q] == b'0' && b[q + 1] == b'0' {
        return None;
    }
    q += 2;
    if q < n && b[q] == b'-' {
        q += 1;
    }
    // serial: four digits, not 0000.
    if q + 4 > n || !b[q..q + 4].iter().all(u8::is_ascii_digit) {
        return None;
    }
    if &b[q..q + 4] == b"0000" {
        return None;
    }
    Some(q + 4)
}

/// us_zip: `(?<!\w)\d{5}(?:-\d{4})?(?!\w)(?!\.\d)` (_US_ZIP_RE). The optional `-\d{4}` is greedy,
/// so the extension is tried first and the plain five-digit form is the backtrack fallback.
fn scan_us_zip(b: &[u8], idx: u32, out: &mut Vec<(u32, u32, u32)>) {
    let n = b.len();
    let mut p = 0;
    while p < n {
        if let Some(end) = zip_match_at(b, n, p) {
            out.push((idx, p as u32, end as u32));
            p = end;
        } else {
            p += 1;
        }
    }
}

fn zip_match_at(b: &[u8], n: usize, p: usize) -> Option<usize> {
    // (?<!\w): start of string, or a non-word char precedes.
    if p > 0 && is_ascii_word(b[p - 1]) {
        return None;
    }
    if p + 5 > n || !b[p..p + 5].iter().all(u8::is_ascii_digit) {
        return None;
    }
    let base = p + 5;
    // Greedy `(?:-\d{4})?`: try the four-digit extension first.
    if base < n
        && b[base] == b'-'
        && base + 5 <= n
        && b[base + 1..base + 5].iter().all(u8::is_ascii_digit)
    {
        let ext = base + 5;
        if zip_trailing_ok(b, n, ext) {
            return Some(ext);
        }
    }
    if zip_trailing_ok(b, n, base) {
        return Some(base);
    }
    None
}

/// `(?!\w)(?!\.\d)` at `end`: not followed by a word char, and not by a decimal point + digit.
fn zip_trailing_ok(b: &[u8], n: usize, end: usize) -> bool {
    if end < n && is_ascii_word(b[end]) {
        return false;
    }
    if end < n && b[end] == b'.' && end + 1 < n && b[end + 1].is_ascii_digit() {
        return false;
    }
    true
}

/// The USPS Pub 28 C1 street-suffix vocabulary, in `_USPS_STREET_SUFFIXES` order (ordered
/// alternation: the first that lets the whole match complete wins, as Python's backtracking picks).
const STREET_SUFFIXES: [&str; 47] = [
    "street",
    "st",
    "avenue",
    "ave",
    "boulevard",
    "blvd",
    "road",
    "rd",
    "drive",
    "dr",
    "lane",
    "ln",
    "court",
    "ct",
    "circle",
    "cir",
    "place",
    "pl",
    "terrace",
    "ter",
    "trail",
    "trl",
    "parkway",
    "pkwy",
    "highway",
    "hwy",
    "way",
    "plaza",
    "plz",
    "square",
    "sq",
    "expressway",
    "expy",
    "freeway",
    "fwy",
    "crossing",
    "xing",
    "loop",
    "alley",
    "aly",
    "bend",
    "cove",
    "cv",
    "point",
    "pt",
    "ridge",
    "rdg",
];

/// The Pub 28 C2 secondary-unit designators (`#` is a literal), in pattern order.
const STREET_UNITS: [&str; 6] = ["apt", "apartment", "suite", "ste", "unit", "#"];

/// street_address: `\b\d{1,6}\s+(?:[A-Za-z0-9][A-Za-z0-9'.\-]*\s+){1,4}(?:SUFFIX)\.?`
/// `(?:\s+(?:UNIT)\.?\s*[\w\-]+)?(?=[\s,.;:!?)"']|$)` (_STREET_ADDRESS_RE, IGNORECASE). The greedy
/// `{1,4}` street-name repetition backtracks over the iteration count (most first), then the
/// suffix/dot/unit alternatives, exactly in Python's order.
fn scan_street(b: &[u8], idx: u32, out: &mut Vec<(u32, u32, u32)>) {
    let n = b.len();
    let mut p = 0;
    while p < n {
        if let Some(end) = street_match_at(b, n, p) {
            out.push((idx, p as u32, end as u32));
            p = end;
        } else {
            p += 1;
        }
    }
}

fn street_match_at(b: &[u8], n: usize, p: usize) -> Option<usize> {
    // \b before the house number: start, or a non-word char precedes (the next char is a digit).
    if p > 0 && is_ascii_word(b[p - 1]) {
        return None;
    }
    // house number: `\d{1,6}\s+`. A digit run longer than 6 cannot be cut before whitespace (the
    // next char is still a digit), so it has no match; a 1-6 run must be followed by whitespace.
    let mut dr = 0;
    while p + dr < n && b[p + dr].is_ascii_digit() {
        dr += 1;
    }
    if dr == 0 || dr > 6 || p + dr >= n || !is_ascii_space(b[p + dr]) {
        return None;
    }
    let mut q = p + dr;
    while q < n && is_ascii_space(b[q]) {
        q += 1; // greedy \s+
    }
    // Greedy `(?:WORD \s+){1,4}`: record the end after each successful iteration (up to 4).
    let mut iter_ends: Vec<usize> = Vec::with_capacity(4);
    let mut pos = q;
    for _ in 0..4 {
        match street_word_iter(b, n, pos) {
            Some(next) => {
                iter_ends.push(next);
                pos = next;
            }
            None => break,
        }
    }
    // Most iterations first (greedy), require >= 1, take the first that completes the suffix tail.
    for &rest_start in iter_ends.iter().rev() {
        if let Some(end) = street_rest(b, n, rest_start) {
            return Some(end);
        }
    }
    None
}

/// One `[A-Za-z0-9][A-Za-z0-9'.\-]*\s+` iteration from `s`; returns the position after its `\s+`.
fn street_word_iter(b: &[u8], n: usize, s: usize) -> Option<usize> {
    if s >= n || !b[s].is_ascii_alphanumeric() {
        return None;
    }
    let mut w = s + 1;
    while w < n && (b[w].is_ascii_alphanumeric() || matches!(b[w], b'\'' | b'.' | b'-')) {
        w += 1; // greedy [A-Za-z0-9'.\-]*
    }
    if w >= n || !is_ascii_space(b[w]) {
        return None; // the trailing \s+ is required
    }
    let mut e = w;
    while e < n && is_ascii_space(b[e]) {
        e += 1;
    }
    Some(e)
}

/// `(?:SUFFIX)\.?(?:\s+(?:UNIT)\.?\s*[\w\-]+)?(?=[\s,.;:!?)"']|$)` from `q`. Suffix alternation,
/// then dot present-before-absent, then unit present-before-absent, mirroring Python's backtrack
/// order; returns the match end (the zero-width lookahead's position) of the first that completes.
fn street_rest(b: &[u8], n: usize, q: usize) -> Option<usize> {
    for suf in STREET_SUFFIXES {
        let slen = suf.len();
        if q + slen > n || !ascii_ci_eq(&b[q..q + slen], suf) {
            continue;
        }
        let after_suf = q + slen;
        for with_dot in [true, false] {
            let after_dot = if with_dot {
                if after_suf < n && b[after_suf] == b'.' {
                    after_suf + 1
                } else {
                    continue; // no dot to consume; the dot-absent branch below handles it
                }
            } else {
                after_suf
            };
            // Unit present first.
            if let Some(after_unit) = street_unit(b, n, after_dot) {
                if street_lookahead(b, n, after_unit) {
                    return Some(after_unit);
                }
            }
            // Unit absent.
            if street_lookahead(b, n, after_dot) {
                return Some(after_dot);
            }
        }
    }
    None
}

/// `\s+(?:UNIT)\.?\s*[\w\-]+` from `s`; returns the position after the `[\w\-]+` unit id, or None.
fn street_unit(b: &[u8], n: usize, s: usize) -> Option<usize> {
    if s >= n || !is_ascii_space(b[s]) {
        return None; // \s+
    }
    let mut u = s;
    while u < n && is_ascii_space(b[u]) {
        u += 1;
    }
    for un in STREET_UNITS {
        let ul = un.len();
        if u + ul > n || !ascii_ci_eq(&b[u..u + ul], un) {
            continue;
        }
        let after_un = u + ul;
        for with_dot in [true, false] {
            let after_dot = if with_dot {
                if after_un < n && b[after_un] == b'.' {
                    after_un + 1
                } else {
                    continue;
                }
            } else {
                after_un
            };
            let mut w = after_dot;
            while w < n && is_ascii_space(b[w]) {
                w += 1; // \s*
            }
            let mut e = w;
            while e < n && (is_ascii_word(b[e]) || b[e] == b'-') {
                e += 1; // [\w\-]+
            }
            if e > w {
                return Some(e);
            }
        }
    }
    None
}

/// `(?=[\s,.;:!?)"']|$)` at `pos`: end of string, or a boundary punctuation / whitespace char.
fn street_lookahead(b: &[u8], n: usize, pos: usize) -> bool {
    if pos >= n {
        return true; // $
    }
    let c = b[pos];
    is_ascii_space(c)
        || matches!(
            c,
            b',' | b'.' | b';' | b':' | b'!' | b'?' | b')' | b'"' | b'\''
        )
}

/// A cell is eligible iff every code point is ASCII (`< 0x80`) AND none is in 0x1c-0x1f (plan
/// 2.1). A lone surrogate never reaches here: it cannot be decoded to a Rust `&str`, so the
/// caller already routes it to Python as ineligible.
pub fn is_eligible(text: &str) -> bool {
    text.is_ascii() && !text.bytes().any(|b| (0x1c..=0x1f).contains(&b))
}

/// The candidate list for one eligible cell over `indices` (catalog indices, in the oracle's
/// requested order among the eleven). Each detector contributes its `find_iter` matches, in order,
/// that pass its validator; finditer advancement is independent of the validator (a rejected
/// match still advances past its own span), exactly as the Python loop does. The lookaround-free
/// eight run on the `regex` crate; the three lookaround detectors run as hand-written scanners.
/// Offsets are code-point counts; on an ASCII cell byte offsets equal code-point offsets, so the
/// match's byte span is returned directly.
///
/// The lookaround-free eight run on the `regex` crate; the three lookaround detectors run as the
/// hand-written byte scanners above (no validator). Both are infallible on an eligible cell, so
/// this always returns a (possibly empty) candidate list.
pub fn candidates_for_cell(text: &str, indices: &[usize]) -> Vec<(u32, u32, u32)> {
    let regexes = compiled();
    let mut out: Vec<(u32, u32, u32)> = Vec::new();
    for &idx in indices {
        if idx < LOOKAROUND_FREE_COUNT {
            let validator = validator_for(idx);
            for m in regexes[idx].find_iter(text) {
                if validator != Validator::None && !run_validator(validator, m.as_str()) {
                    continue;
                }
                out.push((idx as u32, m.start() as u32, m.end() as u32));
            }
        } else {
            scan_lookaround(idx, text, &mut out);
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

/// The scanner's hand-copied USPS street-suffix vocabulary, in ordered-alternation order, for the
/// vocabulary-agreement test (plan 7). Exposed so the test can assert ordered list-equality against
/// the Python oracle's `_USPS_STREET_SUFFIXES`; a one-sided edit (add "pike"/"run") then fails CI
/// instead of silently diverging on the native path.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_street_suffixes() -> Vec<String> {
    STREET_SUFFIXES.iter().map(|s| (*s).to_string()).collect()
}

/// The scanner's hand-copied secondary-unit designators (Pub 28 C2; `#` is a literal), in pattern
/// order, for the vocabulary-agreement test (plan 7). The oracle embeds these directly inside
/// `_STREET_ADDRESS_RE`, so the test ties this list back to that pattern body rather than to a
/// separately copied Python list.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_redact_street_units() -> Vec<String> {
    STREET_UNITS.iter().map(|s| (*s).to_string()).collect()
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
    m.add_function(wrap_pyfunction!(text_redact_street_suffixes, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_street_units, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_validate, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_is_eligible, m)?)?;
    m.add_function(wrap_pyfunction!(text_redact_class_members, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn index_of(id: &str) -> usize {
        SUPPORTED_IDS.iter().position(|s| *s == id).unwrap()
    }

    #[test]
    fn eligibility_marks_the_boundary() {
        assert!(is_eligible("plain 123-45-6789 a@b.com"));
        assert!(is_eligible(""));
        assert!(!is_eligible("café"));
        for sep in ['\u{1c}', '\u{1d}', '\u{1e}', '\u{1f}'] {
            assert!(!is_eligible(&format!("a{sep}b")));
        }
        // The code points bracketing 0x1c-0x1f stay eligible.
        assert!(is_eligible("a\u{1b}b"));
        assert!(is_eligible("a b"));
    }

    #[test]
    fn validators_match_the_reference_vectors() {
        assert!(luhn_valid("0000000000000"));
        assert!(luhn_valid("4111 1111 1111 1111"));
        assert!(!luhn_valid("000000000000")); // 12 digits < 13
        assert!(!luhn_valid("4111111111111112"));
        assert!(npi_valid("1234567893"));
        assert!(npi_valid("123-456-7893"));
        assert!(!npi_valid("1234567890"));
        assert!(ipv4_valid("001.002.003.004"));
        assert!(!ipv4_valid("256.1.1.1"));
        assert!(!ipv4_valid("1.2.3"));
        assert!(iban_valid("GB82WEST12345698765432"));
        assert!(iban_valid("GB82 WEST 1234 5698 7654 32"));
        assert!(!iban_valid("ZZ00INVALID1234567890"));
        assert!(!iban_valid("GB00WEST12345698765432"));
        assert!(!icd10_valid("F00"));
        assert!(icd10_valid("F01"));
        assert!(icd10_valid("D89"));
        assert!(!icd10_valid("D90"));
        assert!(icd10_valid("A00!!!!"));
    }

    #[test]
    fn candidates_find_each_detector_in_order_with_scalar_offsets() {
        let text = "mail a@b.com ip 10.0.0.1 ok";
        let all: Vec<usize> = (0..SUPPORTED_IDS.len()).collect();
        let got = candidates_for_cell(text, &all);
        assert!(got.contains(&(index_of("email") as u32, 5, 12)));
        assert!(got.contains(&(index_of("ipv4") as u32, 16, 24)));
    }

    #[test]
    fn a_rejected_validator_match_does_not_halt_finditer() {
        // Two ipv4-shaped tokens; the first fails the octet range, the second passes. finditer
        // advances past the rejected one, so the valid second match is still found.
        let text = "999.1.1.1 then 10.0.0.1";
        let got = candidates_for_cell(text, &[index_of("ipv4")]);
        assert_eq!(got, vec![(index_of("ipv4") as u32, 15, 23)]);
    }

    #[test]
    fn requested_subset_runs_only_those_detectors() {
        let text = "a@b.com 10.0.0.1";
        let got = candidates_for_cell(text, &[index_of("ipv4")]);
        assert_eq!(got, vec![(index_of("ipv4") as u32, 8, 16)]);
    }

    // ── lookaround detectors (hand-written scanners) ────────────────────────────────────────

    fn spans_of(text: &str, id: &str) -> Vec<(u32, u32)> {
        candidates_for_cell(text, &[index_of(id)])
            .into_iter()
            .map(|(_, s, e)| (s, e))
            .collect()
    }

    #[test]
    fn ssn_honours_the_ssa_reserved_block_lookaheads() {
        // Hyphenated and bare nine-digit shapes both match; reserved area/group/serial blocks do
        // not, exactly as _SSN_RE's negative lookaheads require.
        assert_eq!(spans_of("x 123-45-6789 y", "ssn"), vec![(2, 13)]);
        assert_eq!(spans_of("123456789", "ssn"), vec![(0, 9)]);
        assert!(spans_of("000-45-6789", "ssn").is_empty()); // area 000
        assert!(spans_of("666-45-6789", "ssn").is_empty()); // area 666
        assert!(spans_of("900-45-6789", "ssn").is_empty()); // 9xx area
        assert!(spans_of("123-00-6789", "ssn").is_empty()); // group 00
        assert!(spans_of("123-45-0000", "ssn").is_empty()); // serial 0000
    }

    #[test]
    fn us_zip_rejects_runs_embedded_in_word_or_number_tokens() {
        assert_eq!(spans_of("zip 12345 ok", "us_zip"), vec![(4, 9)]);
        assert_eq!(spans_of("90210-1234 here", "us_zip"), vec![(0, 10)]);
        assert!(spans_of("12345mg", "us_zip").is_empty()); // trailing word char
        assert!(spans_of("x12345", "us_zip").is_empty()); // leading word char
        assert!(spans_of("12345.6", "us_zip").is_empty()); // decimal number
        assert!(spans_of("123456", "us_zip").is_empty()); // six-digit run
    }

    fn one_match_text<'a>(text: &'a str, id: &str) -> &'a str {
        let spans = spans_of(text, id);
        assert_eq!(spans.len(), 1, "{text:?} should have exactly one {id} span");
        &text[spans[0].0 as usize..spans[0].1 as usize]
    }

    #[test]
    fn street_address_anchors_on_a_house_number_and_usps_suffix() {
        // House number + 1-4 street-name words + a USPS Pub 28 suffix; the trailing lookahead
        // stops the span at the first boundary punctuation (asserted via the matched substring so
        // the exact offsets are checked without hand-counting).
        assert_eq!(
            one_match_text("at 12 Main Street, Springfield", "street_address"),
            "12 Main Street"
        );
        // The optional `\.?` after the suffix consumes a trailing period.
        assert_eq!(
            one_match_text("742 Evergreen Terrace.", "street_address"),
            "742 Evergreen Terrace."
        );
        // Case-insensitive suffix match on the abbreviated form.
        assert_eq!(
            one_match_text("100 oak AVE here", "street_address"),
            "100 oak AVE"
        );
        assert!(spans_of("just some prose here", "street_address").is_empty());
    }
}
