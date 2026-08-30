//! Typed canonical source-byte encoding, mirroring
//! `decoy_engine.kernel._canonicalize.canonicalize_derive_source` (itself
//! `generation.pool._canonicalize._canonicalize_source`) for the Arrow types the native hash
//! kernel admits: utf8, large_utf8, signed and unsigned integer widths, bool, and
//! timestamp-with-timezone.
//!
//! Per-type rules, pinned to the shipped Python behavior (not re-derived here):
//!
//! - utf8 / large_utf8: Unicode NFC normalization, then UTF-8 bytes. No length prefix.
//! - int (any admitted width, signed or unsigned): a 4-byte big-endian length prefix around
//!   the minimal-width two's-complement big-endian body, following the ASN.1 DER INTEGER
//!   convention (X.690 SS8.3) the Python side cites. The prefix is INSIDE the canonical source
//!   bytes (the source-length prefix the HMAC frame applies in `derive.rs` is a second, outer
//!   layer).
//! - bool: exactly one byte, `0x01` for true or `0x00` for false. No length prefix.
//! - timestamp-with-tz: convert to the UTC instant, format as ISO 8601 with a `+00:00` offset,
//!   UTF-8 bytes. No length prefix. The declared timezone is metadata only: Arrow stores a
//!   timestamp value as ticks since the Unix epoch UTC regardless of the tz field, so two
//!   columns declaring different timezones over the same absolute instant canonicalize
//!   identically (verified against the live Python reference while building the KAT fixture).
//!
//! Any other Arrow type (floating point, a naive/tz-less timestamp, or anything else) is out
//! of the admitted set: the caller (`arrow_ffi`) rejects it with `mixed_object_not_native`
//! before this module ever sees it, matching the Python canonicalizer's hard-error policy for
//! float and naive-datetime sources.

use arrow_array::temporal_conversions::{
    timestamp_ms_to_datetime, timestamp_ns_to_datetime, timestamp_s_to_datetime,
    timestamp_us_to_datetime,
};
use arrow_array::{
    Array, ArrayAccessor, BooleanArray, GenericStringArray, OffsetSizeTrait, PrimitiveArray,
};
use arrow_schema::{ArrowError, DataType, TimeUnit};
use chrono::{Datelike, NaiveDateTime, Timelike};
use unicode_normalization::UnicodeNormalization;

/// A row-level canonicalization failure. Never carries the source value itself (the
/// redacted-error contract): `detail` names the *type*, not the offending data.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CanonError {
    pub code: &'static str,
    pub detail: String,
}

impl CanonError {
    pub fn new(code: &'static str, detail: impl Into<String>) -> Self {
        Self {
            code,
            detail: detail.into(),
        }
    }

    /// The one rejection code the admitted-type boundary ever raises (float, naive timestamp,
    /// mixed-object, or any other unsupported shape), per `CRYPTO_EXT_ABI`.
    pub fn unsupported(detail: impl Into<String>) -> Self {
        Self::new("mixed_object_not_native", detail.into())
    }
}

impl std::fmt::Display for CanonError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

impl std::error::Error for CanonError {}

/// Encode a Python-equivalent arbitrary-magnitude integer as
/// `_canonicalize.py::_encode_int` does: a 4-byte big-endian length prefix around the
/// minimal-width two's-complement big-endian body.
///
/// `n` is an `i128` container for any admitted Arrow int width (up to 64-bit signed or
/// unsigned); the widest admitted value (`u64::MAX`, `i64::MIN`) needs at most 9 body bytes,
/// far under the `i128` range, so the arithmetic below never approaches `i128`'s own bounds.
pub fn encode_int(n: i128) -> Vec<u8> {
    // Python's `int.bit_length()`: 0 for zero, else floor(log2(|n|)) + 1. Mirrored here via
    // `u128::leading_zeros` on the magnitude so the byte count matches the reference exactly,
    // including its documented non-minimal cases at negative powers of two (e.g. -128 takes 2
    // body bytes, not 1): this is intentionally the reference's own formula, not an
    // independently "optimal" two's-complement width.
    let magnitude = n.unsigned_abs();
    let bit_length: u32 = if magnitude == 0 {
        0
    } else {
        128 - magnitude.leading_zeros()
    };
    let nbytes = ((bit_length + 8) / 8) as usize;

    let body = twos_complement_be(n, nbytes);
    let mut out = Vec::with_capacity(4 + nbytes);
    out.extend_from_slice(&(nbytes as u32).to_be_bytes());
    out.extend_from_slice(&body);
    out
}

/// `n`'s two's-complement representation in exactly `nbytes` big-endian bytes.
///
/// `nbytes` is at most 9 in this crate's admitted domain (bounded by `encode_int`'s own
/// bit-length computation over a 64-bit-wide value), so `nbytes * 8 <= 72` and every
/// intermediate value below fits comfortably inside `u128`.
fn twos_complement_be(n: i128, nbytes: usize) -> Vec<u8> {
    let bits = (nbytes as u32) * 8;
    let modulus: u128 = 1u128 << bits;
    let unsigned_val: u128 = if n >= 0 {
        n as u128
    } else {
        // Two's complement of a negative value: modulus - |n|. `n` never reaches `i128::MIN`
        // in this crate's domain (the widest admitted magnitude is `u64::MAX`), so `-n` never
        // overflows `i128`.
        modulus - n.unsigned_abs()
    };
    unsigned_val.to_be_bytes()[16 - nbytes..].to_vec()
}

/// Canonicalize a UTF-8 string source: NFC normalization, then UTF-8 bytes. No length prefix
/// (the HMAC frame in `derive.rs` supplies the outer one).
pub fn encode_utf8(s: &str) -> Vec<u8> {
    s.nfc().collect::<String>().into_bytes()
}

/// Canonicalize a boolean source: one byte, matching `_canonicalize_source`'s `b"\x01"` /
/// `b"\x00"` (checked before the integer branch there, since `bool` is a Python `int`
/// subtype; the Arrow type system keeps these distinct so no such ordering trick is needed
/// here).
pub fn encode_bool(b: bool) -> Vec<u8> {
    vec![u8::from(b)]
}

/// Canonicalize a timestamp tick value (already UTC-epoch ticks per Arrow's C Data Interface
/// convention) to the same ISO 8601 string the Python side produces via
/// `value.astimezone(timezone.utc).isoformat()`.
///
/// Fractional-second digits follow the exact rule pandas' `Timestamp.isoformat()` applies (the
/// Python reference sees a `pandas.Timestamp` for nanosecond-unit columns and a plain
/// `datetime.datetime` otherwise, and both converge on this rule): no fraction when the
/// sub-second component is exactly zero, six digits when it is a whole number of
/// microseconds, nine digits otherwise. Verified empirically against the live Python
/// reference while building the KAT fixture (`decoy-engine-native/vectors/generate_kat.py`).
pub fn encode_timestamp(unit: TimeUnit, ticks: i64) -> Result<Vec<u8>, CanonError> {
    let naive: NaiveDateTime = match unit {
        TimeUnit::Second => timestamp_s_to_datetime(ticks),
        TimeUnit::Millisecond => timestamp_ms_to_datetime(ticks),
        TimeUnit::Microsecond => timestamp_us_to_datetime(ticks),
        TimeUnit::Nanosecond => timestamp_ns_to_datetime(ticks),
    }
    .ok_or_else(|| {
        CanonError::unsupported("timestamp tick value out of the representable range")
    })?;

    let ns = naive.nanosecond();
    let frac = if ns == 0 {
        String::new()
    } else if ns.is_multiple_of(1000) {
        format!(".{:06}", ns / 1000)
    } else {
        format!(".{ns:09}")
    };

    Ok(format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}{}+00:00",
        naive.year(),
        naive.month(),
        naive.day(),
        naive.hour(),
        naive.minute(),
        naive.second(),
        frac,
    )
    .into_bytes())
}

/// Canonicalize row `i` of `array`, matching `canonicalize_derive_source` for the admitted
/// Arrow types. Returns `Ok(None)` for a null slot (the caller must not call this for a null
/// row in the first place; kept as a defensive `None` rather than a panic).
pub fn canonicalize_row(array: &dyn Array, i: usize) -> Result<Option<Vec<u8>>, CanonError> {
    if array.is_null(i) {
        return Ok(None);
    }
    match array.data_type() {
        DataType::Utf8 => Ok(Some(encode_utf8(typed_string::<i32>(array)?.value(i)))),
        DataType::LargeUtf8 => Ok(Some(encode_utf8(typed_string::<i64>(array)?.value(i)))),
        DataType::Boolean => {
            let a = downcast::<BooleanArray>(array)?;
            Ok(Some(encode_bool(a.value(i))))
        }
        DataType::Int8 => Ok(Some(encode_int(
            int_value::<arrow_array::types::Int8Type>(array, i)? as i128,
        ))),
        DataType::Int16 => Ok(Some(encode_int(
            int_value::<arrow_array::types::Int16Type>(array, i)? as i128,
        ))),
        DataType::Int32 => Ok(Some(encode_int(
            int_value::<arrow_array::types::Int32Type>(array, i)? as i128,
        ))),
        DataType::Int64 => Ok(Some(encode_int(
            int_value::<arrow_array::types::Int64Type>(array, i)? as i128,
        ))),
        DataType::UInt8 => Ok(Some(encode_int(
            int_value::<arrow_array::types::UInt8Type>(array, i)? as i128,
        ))),
        DataType::UInt16 => Ok(Some(encode_int(
            int_value::<arrow_array::types::UInt16Type>(array, i)? as i128,
        ))),
        DataType::UInt32 => Ok(Some(encode_int(
            int_value::<arrow_array::types::UInt32Type>(array, i)? as i128,
        ))),
        DataType::UInt64 => Ok(Some(encode_int(
            int_value::<arrow_array::types::UInt64Type>(array, i)? as i128,
        ))),
        DataType::Timestamp(unit, Some(_tz)) => {
            let ticks = timestamp_value(array, *unit, i)?;
            encode_timestamp(*unit, ticks).map(Some)
        }
        DataType::Timestamp(_, None) => Err(CanonError::unsupported(
            "timestamp column has no timezone (naive datetime)",
        )),
        other => Err(CanonError::unsupported(format!(
            "Arrow type {other:?} is not native-admitted"
        ))),
    }
}

fn downcast<T: 'static>(array: &dyn Array) -> Result<&T, CanonError> {
    array.as_any().downcast_ref::<T>().ok_or_else(|| {
        CanonError::unsupported("Arrow array physical type did not match its declared logical type")
    })
}

fn typed_string<O: OffsetSizeTrait>(
    array: &dyn Array,
) -> Result<&GenericStringArray<O>, CanonError> {
    downcast::<GenericStringArray<O>>(array)
}

fn int_value<T>(array: &dyn Array, i: usize) -> Result<T::Native, CanonError>
where
    T: arrow_array::types::ArrowPrimitiveType,
{
    let a = downcast::<PrimitiveArray<T>>(array)?;
    Ok(ArrayAccessor::value(&a, i))
}

fn timestamp_value(array: &dyn Array, unit: TimeUnit, i: usize) -> Result<i64, CanonError> {
    use arrow_array::types::{
        TimestampMicrosecondType, TimestampMillisecondType, TimestampNanosecondType,
        TimestampSecondType,
    };
    match unit {
        TimeUnit::Second => int_value::<TimestampSecondType>(array, i),
        TimeUnit::Millisecond => int_value::<TimestampMillisecondType>(array, i),
        TimeUnit::Microsecond => int_value::<TimestampMicrosecondType>(array, i),
        TimeUnit::Nanosecond => int_value::<TimestampNanosecondType>(array, i),
    }
}

/// Whether an Arrow `DataType` is one the native hash kernel admits (used by the eligibility
/// gate this crate exposes, and by tests constructing the "wrong type" FFI-boundary case).
pub fn is_admitted_type(dt: &DataType) -> bool {
    matches!(
        dt,
        DataType::Utf8
            | DataType::LargeUtf8
            | DataType::Boolean
            | DataType::Int8
            | DataType::Int16
            | DataType::Int32
            | DataType::Int64
            | DataType::UInt8
            | DataType::UInt16
            | DataType::UInt32
            | DataType::UInt64
    ) || matches!(dt, DataType::Timestamp(_, Some(_)))
}

/// Map an arrow-rs schema/import error to our redacted error type (never embeds row content;
/// arrow-rs's own `ArrowError` messages for these paths carry only type/shape descriptions).
pub fn map_arrow_error(err: ArrowError) -> CanonError {
    CanonError::unsupported(format!("Arrow C Data Interface rejected the input: {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_int_zero_is_one_byte() {
        assert_eq!(encode_int(0), vec![0, 0, 0, 1, 0x00]);
    }

    #[test]
    fn encode_int_matches_python_boundaries() {
        // Cross-checked against `decoy_engine.kernel._canonicalize.canonicalize_derive_source`
        // while building the shared KAT fixture (see keyed_derivation_kat.json,
        // "int8_signed_boundaries" / "int64_unsigned_boundaries").
        assert_eq!(encode_int(-128), hex("00000002ff80"));
        assert_eq!(encode_int(-1), hex("00000001ff"));
        assert_eq!(encode_int(1), hex("0000000101"));
        assert_eq!(encode_int(127), hex("000000017f"));
        assert_eq!(
            encode_int(u64::MAX as i128),
            hex("0000000900ffffffffffffffff")
        );
        assert_eq!(
            encode_int(i64::MIN as i128),
            hex("00000009ff8000000000000000")
        );
    }

    #[test]
    fn encode_bool_matches_python() {
        assert_eq!(encode_bool(true), vec![0x01]);
        assert_eq!(encode_bool(false), vec![0x00]);
    }

    #[test]
    fn canon_error_display_reports_code_and_detail() {
        let err = CanonError::new("some_code", "some detail");
        assert_eq!(err.to_string(), "some_code: some detail");
    }

    /// `TimeUnit::Second` admits any `i64` tick value, and a tick this large is far past
    /// chrono's representable proleptic-Gregorian range: unlike the length-overflow branches in
    /// `derive.rs`, this one needs no synthetic helper to reach, since the real `i64` domain
    /// already covers it directly.
    #[test]
    fn encode_timestamp_rejects_a_tick_outside_the_representable_calendar_range() {
        let err = encode_timestamp(TimeUnit::Second, i64::MAX).unwrap_err();
        assert_eq!(err.code, "mixed_object_not_native");
    }

    #[test]
    fn encode_utf8_nfc_normalizes() {
        // "cafe" + combining acute (NFD) must canonicalize the same as precomposed "café" (NFC).
        let nfc = "caf\u{e9}";
        let nfd = "cafe\u{301}";
        assert_eq!(encode_utf8(nfc), encode_utf8(nfd));
        assert_eq!(encode_utf8(nfc), hex("636166c3a9"));
    }

    #[test]
    fn encode_timestamp_matches_python_fraction_rules() {
        // Cross-checked against the live Python reference (keyed_derivation_kat.json,
        // "timestamp_ns_utc_fraction_precision").
        assert_eq!(
            encode_timestamp(TimeUnit::Nanosecond, 1_577_881_845_123_456_789).unwrap(),
            b"2020-01-01T12:30:45.123456789+00:00".to_vec()
        );
        assert_eq!(
            encode_timestamp(TimeUnit::Nanosecond, 1_577_881_845_123_456_000).unwrap(),
            b"2020-01-01T12:30:45.123456+00:00".to_vec()
        );
        assert_eq!(
            encode_timestamp(TimeUnit::Nanosecond, 1_577_881_845_000_000_000).unwrap(),
            b"2020-01-01T12:30:45+00:00".to_vec()
        );
    }

    fn hex(s: &str) -> Vec<u8> {
        (0..s.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
            .collect()
    }
}
