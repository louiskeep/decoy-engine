//! Loads the shared `keyed_derivation_kat.json` fixture (generated from the live Python
//! `reference_keyed_derivation()` and `canonicalize_derive_source`, see
//! `vectors/generate_kat.py`) and asserts the Rust canonicalizer and derivation envelope
//! reproduce both the canonical source bytes and the final derived token, per row, exactly.
//!
//! The RFC 5869 (HKDF-SHA256) and RFC 4231 (HMAC-SHA256) primitive vectors are pinned as unit
//! tests next to the primitive they exercise (`src/derive.rs`), not duplicated here; this file
//! owns the one artifact genuinely shared across languages, the compatibility corpus.

use std::sync::Arc;

use _kernel::canonicalize::canonicalize_row;
use _kernel::derive::{derive, hex_token};
use arrow_array::{Array, ArrayRef, BooleanArray, GenericStringArray, PrimitiveArray};
use arrow_schema::{DataType, TimeUnit};
use chrono::DateTime;
use serde::Deserialize;

#[derive(Deserialize)]
struct Fixture {
    #[allow(dead_code)]
    format_version: u32,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    arrow_type: ArrowTypeSpec,
    logical_values: Vec<serde_json::Value>,
    mask_key_hex: String,
    namespace: String,
    truncate: Option<isize>,
    expected_canonical_source_hex: Vec<Option<String>>,
    expected_output: Vec<Option<String>>,
}

#[derive(Deserialize)]
struct ArrowTypeSpec {
    kind: String,
    bits: Option<u32>,
    signed: Option<bool>,
    unit: Option<String>,
    tz: Option<String>,
}

fn load_fixture() -> Fixture {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/vectors/keyed_derivation_kat.json"
    );
    let text = std::fs::read_to_string(path).expect("shared KAT fixture must be present");
    serde_json::from_str(&text).expect("shared KAT fixture must parse")
}

fn time_unit(unit: &str) -> TimeUnit {
    match unit {
        "s" => TimeUnit::Second,
        "ms" => TimeUnit::Millisecond,
        "us" => TimeUnit::Microsecond,
        "ns" => TimeUnit::Nanosecond,
        other => panic!("unknown time unit in fixture: {other}"),
    }
}

/// Parse an ISO-8601-with-offset timestamp string into raw epoch ticks at `unit`, matching how
/// `generate_kat.py` fed the same strings to `pd.Timestamp` when building the fixture.
fn parse_timestamp_ticks(s: &str, unit: TimeUnit) -> i64 {
    let dt = DateTime::parse_from_rfc3339(s)
        .unwrap_or_else(|e| panic!("fixture timestamp {s:?} must parse as RFC 3339: {e}"));
    match unit {
        TimeUnit::Second => dt.timestamp(),
        TimeUnit::Millisecond => dt.timestamp_millis(),
        TimeUnit::Microsecond => dt.timestamp_micros(),
        TimeUnit::Nanosecond => dt
            .timestamp_nanos_opt()
            .unwrap_or_else(|| panic!("fixture timestamp {s:?} out of nanosecond range")),
    }
}

/// Build the arrow-rs array a fixture case describes, mirroring `generate_kat.py::_arrow_type`
/// and `_logical_to_native`.
fn build_array(spec: &ArrowTypeSpec, values: &[serde_json::Value]) -> ArrayRef {
    match spec.kind.as_str() {
        "utf8" => {
            let v: Vec<Option<String>> = values
                .iter()
                .map(|j| j.as_str().map(str::to_string))
                .collect();
            Arc::new(GenericStringArray::<i32>::from(v))
        }
        "large_utf8" => {
            let v: Vec<Option<String>> = values
                .iter()
                .map(|j| j.as_str().map(str::to_string))
                .collect();
            Arc::new(GenericStringArray::<i64>::from(v))
        }
        "bool" => {
            let v: Vec<Option<bool>> = values.iter().map(|j| j.as_bool()).collect();
            Arc::new(BooleanArray::from(v))
        }
        "int" => build_int_array(spec, values),
        "timestamp" => build_timestamp_array(spec, values),
        other => panic!("unhandled arrow_type.kind in fixture: {other}"),
    }
}

fn parse_decimal_i128(j: &serde_json::Value) -> i128 {
    j.as_str()
        .unwrap_or_else(|| panic!("int logical_value must be a decimal string, got {j:?}"))
        .parse::<i128>()
        .unwrap_or_else(|e| panic!("bad decimal int literal {j:?}: {e}"))
}

fn build_int_array(spec: &ArrowTypeSpec, values: &[serde_json::Value]) -> ArrayRef {
    use arrow_array::types::*;
    let bits = spec.bits.expect("int arrow_type needs bits");
    let signed = spec.signed.expect("int arrow_type needs signed");
    macro_rules! prim {
        ($ty:ty, $native:ty) => {{
            let v: Vec<Option<$native>> = values
                .iter()
                .map(|j| {
                    if j.is_null() {
                        None
                    } else {
                        Some(parse_decimal_i128(j) as $native)
                    }
                })
                .collect();
            Arc::new(PrimitiveArray::<$ty>::from(v)) as ArrayRef
        }};
    }
    match (bits, signed) {
        (8, true) => prim!(Int8Type, i8),
        (8, false) => prim!(UInt8Type, u8),
        (16, true) => prim!(Int16Type, i16),
        (16, false) => prim!(UInt16Type, u16),
        (32, true) => prim!(Int32Type, i32),
        (32, false) => prim!(UInt32Type, u32),
        (64, true) => prim!(Int64Type, i64),
        (64, false) => prim!(UInt64Type, u64),
        other => panic!("unsupported int (bits, signed) in fixture: {other:?}"),
    }
}

fn build_timestamp_array(spec: &ArrowTypeSpec, values: &[serde_json::Value]) -> ArrayRef {
    use arrow_array::types::*;
    let unit = time_unit(
        spec.unit
            .as_deref()
            .expect("timestamp arrow_type needs unit"),
    );
    let tz: Option<Arc<str>> = spec.tz.as_deref().map(Arc::from);
    let ticks: Vec<Option<i64>> = values
        .iter()
        .map(|j| j.as_str().map(|s| parse_timestamp_ticks(s, unit)))
        .collect();
    match unit {
        TimeUnit::Second => {
            Arc::new(PrimitiveArray::<TimestampSecondType>::from(ticks).with_timezone_opt(tz))
        }
        TimeUnit::Millisecond => {
            Arc::new(PrimitiveArray::<TimestampMillisecondType>::from(ticks).with_timezone_opt(tz))
        }
        TimeUnit::Microsecond => {
            Arc::new(PrimitiveArray::<TimestampMicrosecondType>::from(ticks).with_timezone_opt(tz))
        }
        TimeUnit::Nanosecond => {
            Arc::new(PrimitiveArray::<TimestampNanosecondType>::from(ticks).with_timezone_opt(tz))
        }
    }
}

fn hex_of(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn shared_kat_fixture_canonical_source_and_output_match() {
    let fixture = load_fixture();
    assert!(
        !fixture.cases.is_empty(),
        "the shared fixture must not be empty"
    );

    let mut checked_types = std::collections::HashSet::new();

    for case in &fixture.cases {
        let array = build_array(&case.arrow_type, &case.logical_values);
        assert_eq!(
            array.len(),
            case.logical_values.len(),
            "case {}: built array length mismatch",
            case.name
        );
        checked_types.insert(case.arrow_type.kind.clone());

        let mask_key = hex_bytes(&case.mask_key_hex);

        for i in 0..array.len() {
            let canonical = canonicalize_row(array.as_ref(), i).unwrap_or_else(|e| {
                panic!("case {} row {i}: canonicalize_row failed: {e}", case.name)
            });
            let expected_canon = case.expected_canonical_source_hex[i].as_deref();
            match (&canonical, expected_canon) {
                (None, None) => {}
                (Some(bytes), Some(hex)) => assert_eq!(
                    hex_of(bytes),
                    hex,
                    "case {} row {i}: canonical source mismatch",
                    case.name
                ),
                (got, want) => panic!(
                    "case {} row {i}: null mismatch, got {got:?} want {want:?}",
                    case.name
                ),
            }

            let expected_out = case.expected_output[i].as_deref();
            match (&canonical, expected_out) {
                (None, None) => {}
                (Some(bytes), Some(expected_token)) => {
                    let digest = derive(&mask_key, &case.namespace, bytes).unwrap_or_else(|e| {
                        panic!("case {} row {i}: derive failed: {e}", case.name)
                    });
                    let token = hex_token(&digest, case.truncate);
                    assert_eq!(
                        &token, expected_token,
                        "case {} row {i}: output mismatch",
                        case.name
                    );
                }
                (got, want) => panic!(
                    "case {} row {i}: output null-mismatch, got {:?} want {want:?}",
                    case.name,
                    got.is_some()
                ),
            }
        }
    }

    // Sanity check on the fixture itself: every admitted Arrow type family from the plan's
    // required coverage list is actually represented, so a silently-emptied fixture can't pass.
    for expected_kind in ["utf8", "large_utf8", "bool", "int", "timestamp"] {
        assert!(
            checked_types.contains(expected_kind),
            "fixture is missing coverage for arrow_type.kind = {expected_kind:?}"
        );
    }
}

/// A float or naive-timestamp array is rejected with `mixed_object_not_native`; no partial
/// output. The eligibility gate upstream already excludes these types from reaching the
/// compiled kernel, but the kernel refuses them too, so a gate bug never turns into silent
/// coercion here.
#[test]
fn unsupported_types_are_rejected_not_coerced() {
    let floats: ArrayRef = Arc::new(arrow_array::Float64Array::from(vec![1.0, 2.0]));
    let err = canonicalize_row(floats.as_ref(), 0).unwrap_err();
    assert_eq!(err.code, "mixed_object_not_native");

    let naive_ts: ArrayRef = Arc::new(
        PrimitiveArray::<arrow_array::types::TimestampMicrosecondType>::from(vec![Some(0i64)]),
    );
    assert_eq!(
        naive_ts.data_type(),
        &DataType::Timestamp(TimeUnit::Microsecond, None)
    );
    let err = canonicalize_row(naive_ts.as_ref(), 0).unwrap_err();
    assert_eq!(err.code, "mixed_object_not_native");
}

fn hex_bytes(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
