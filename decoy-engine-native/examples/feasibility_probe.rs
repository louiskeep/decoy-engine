//! Phase 1 Task 1.1 feasibility probe: measures the two numbers that decide whether
//! the 600s target is reachable on the reference host BEFORE the cached-key + Rayon
//! kernel is built (Tasks 1.2-1.5).
//!
//! 1. `cached_hmac_ns_per_row`: cost of one keyed-hash row AFTER the Task 1.2 cache
//!    (HKDF computed once per batch, then one HMAC-SHA256 per row). This is the
//!    post-optimization per-row cost.
//! 2. effective multi-core scaling of that work across 1/2/4/8 threads on this host
//!    (n2-standard-8 is 4 physical cores + HT, so 8-thread scaling is the open question).
//! 3. `current_ns_per_row`: the current cost (HKDF recomputed every row) so the ratio
//!    shows the cache gain the Task 1.2 optimization captures.
//!
//! Projection: hash_wall = cached_hmac_ns_per_row * 300M rows (3 hash cols x 100M) /
//! effective_scaling_at_8. Feasible iff hash_wall + the measured ~292s non-hash serial
//! floor <= 600s WITH margin. Emits JSON on stdout for the harness to parse.
//!
//! Uses only the package's hkdf/hmac/sha2 deps + std (no crate internals, no rayon):
//! a std::thread fan-out over independent rows is a fair proxy for Rayon's data-parallel
//! scaling, and the HMAC/HKDF primitives are the exact 0.13/0.11 versions the kernel uses.

use std::thread;
use std::time::Instant;

use hkdf::Hkdf;
use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

const SALT: &[u8] = b"decoy-engine/keyed-derivation/v1"; // representative 32-byte salt (timing only)
const NAMESPACE: &[u8] = b"h_email";
const NON_HASH_SERIAL_FLOOR_S: f64 = 292.0; // measured redact+truncate+passthrough+IO (Task 0.2)
const TARGET_S: f64 = 600.0;
const HASH_OPS_100M: u64 = 300_000_000; // 3 hash cols x 100M rows
// Task 0.2 authoritative baseline: hash single-thread = 1,280.11s over 300M ops.
// This is the FULL per-row hash cost (HKDF-per-row + HMAC + canonicalization + hex +
// Arrow), not just the HMAC. The projection subtracts only the MEASURED per-row HKDF
// savings from it (caching keeps everything else per-row), so it does not understate
// the cached hash cost the way an HMAC-only microbench would.
const BASELINE_HASH_1T_S: f64 = 1280.11;

/// One representative canonical source (a ~16-byte utf8 email-ish value).
fn sample_frame(row: u64) -> Vec<u8> {
    // version byte + length-prefixed namespace + length-prefixed canonical source,
    // mirroring the kernel's build_frame shape closely enough for timing.
    let src = format!("user{row:012}@example.com");
    let src = src.as_bytes();
    let mut f = Vec::with_capacity(1 + 4 + NAMESPACE.len() + 4 + src.len());
    f.push(1u8);
    f.extend_from_slice(&(NAMESPACE.len() as u32).to_be_bytes());
    f.extend_from_slice(NAMESPACE);
    f.extend_from_slice(&(src.len() as u32).to_be_bytes());
    f.extend_from_slice(src);
    f
}

fn hkdf_key(mask_key: &[u8]) -> [u8; 32] {
    let hk = Hkdf::<Sha256>::new(Some(SALT), mask_key);
    let mut okm = [0u8; 32];
    hk.expand(NAMESPACE, &mut okm).expect("32-byte OKM is in range");
    okm
}

/// Cached-key path: HKDF once, then one HMAC per row (the post-Task-1.2 cost).
#[inline]
fn cached_hmac_row(key: &[u8; 32], frame: &[u8]) -> [u8; 32] {
    let mut mac = <Hmac<Sha256> as KeyInit>::new_from_slice(key).expect("any-length key");
    mac.update(frame);
    mac.finalize().into_bytes().into()
}

/// Current path: recompute HKDF every row, then HMAC (what the kernel does today).
#[inline]
fn current_row(mask_key: &[u8], frame: &[u8]) -> [u8; 32] {
    let key = hkdf_key(mask_key);
    cached_hmac_row(&key, frame)
}

/// Run `rows` cached-HMAC ops single-threaded; return ns/row. `black_box` via a checksum
/// fold so the optimizer cannot elide the work.
fn bench_cached(rows: u64, key: &[u8; 32]) -> (f64, u64) {
    let t = Instant::now();
    let mut acc = 0u64;
    for r in 0..rows {
        let frame = sample_frame(r);
        let d = cached_hmac_row(key, &frame);
        acc = acc.wrapping_add(d[0] as u64);
    }
    let ns = t.elapsed().as_nanos() as f64 / rows as f64;
    (ns, acc)
}

fn bench_current(rows: u64, mask_key: &[u8]) -> (f64, u64) {
    let t = Instant::now();
    let mut acc = 0u64;
    for r in 0..rows {
        let frame = sample_frame(r);
        let d = current_row(mask_key, &frame);
        acc = acc.wrapping_add(d[0] as u64);
    }
    let ns = t.elapsed().as_nanos() as f64 / rows as f64;
    (ns, acc)
}

/// Wall time for `rows` cached-HMAC ops spread over `threads` threads (each does its share).
fn wall_threaded(rows: u64, threads: u64, key: [u8; 32]) -> f64 {
    let per = rows / threads;
    let t = Instant::now();
    let handles: Vec<_> = (0..threads)
        .map(|tid| {
            thread::spawn(move || {
                let mut acc = 0u64;
                let start = tid * per;
                for r in start..start + per {
                    let frame = sample_frame(r);
                    let d = cached_hmac_row(&key, &frame);
                    acc = acc.wrapping_add(d[0] as u64);
                }
                acc
            })
        })
        .collect();
    let mut acc = 0u64;
    for h in handles {
        acc = acc.wrapping_add(h.join().unwrap());
    }
    std::hint::black_box(acc);
    t.elapsed().as_secs_f64()
}

fn main() {
    let mask_key = [0x11u8; 32];
    let key = hkdf_key(&mask_key);

    // Warm up (prime caches / branch predictors).
    let _ = bench_cached(200_000, &key);

    let single_rows = 3_000_000u64;
    let (cached_ns, _a) = bench_cached(single_rows, &key);
    let (current_ns, _b) = bench_current(single_rows, &mask_key);
    let cache_gain = current_ns / cached_ns;

    // Scaling: same total work over 1/2/4/8 threads.
    let scale_rows = 8_000_000u64; // divisible by 1,2,4,8
    let mut scaling = Vec::new();
    let base = wall_threaded(scale_rows, 1, key);
    for &threads in &[1u64, 2, 4, 8] {
        let w = wall_threaded(scale_rows, threads, key);
        let speedup = base / w;
        scaling.push((threads, w, speedup));
    }
    let eff8 = scaling.last().unwrap().2;

    // Honest projection: start from the baseline's FULL per-row hash cost (1,280s / 300M)
    // and subtract ONLY the measured per-row HKDF savings caching removes; everything else
    // (HMAC, canonicalization, hex, Arrow) stays per-row and parallelizes under Rayon.
    let hkdf_savings_ns = current_ns - cached_ns; // the redundant per-row HKDF caching removes
    let hkdf_savings_s = hkdf_savings_ns * HASH_OPS_100M as f64 / 1e9;
    let cached_hash_1t = (BASELINE_HASH_1T_S - hkdf_savings_s).max(0.0);
    let hash_wall_8t = cached_hash_1t / eff8;
    let projected_total = hash_wall_8t + NON_HASH_SERIAL_FLOOR_S;
    let feasible = projected_total <= TARGET_S;
    // Also expose the raw microbench single-thread hash wall for reference.
    let hash_wall_1t = cached_hash_1t;

    let ncpu = thread::available_parallelism().map(|n| n.get()).unwrap_or(0);
    print!("FEASIBILITY_JSON {{");
    print!("\"ncpu\":{ncpu},");
    print!("\"cached_hmac_ns_per_row\":{cached_ns:.2},");
    print!("\"current_ns_per_row\":{current_ns:.2},");
    print!("\"cache_gain_x\":{cache_gain:.2},");
    print!("\"scaling\":[");
    for (i, (t, w, s)) in scaling.iter().enumerate() {
        if i > 0 {
            print!(",");
        }
        print!("{{\"threads\":{t},\"wall_s\":{w:.4},\"speedup\":{s:.3}}}");
    }
    print!("],");
    print!("\"effective_scaling_8t\":{eff8:.3},");
    print!("\"baseline_hash_1t_s\":{BASELINE_HASH_1T_S:.1},");
    print!("\"hkdf_savings_s\":{hkdf_savings_s:.1},");
    print!("\"cached_hash_1t_s\":{cached_hash_1t:.1},");
    print!("\"hash_wall_1t_s\":{hash_wall_1t:.1},");
    print!("\"hash_wall_8t_s\":{hash_wall_8t:.1},");
    print!("\"non_hash_floor_s\":{NON_HASH_SERIAL_FLOOR_S:.1},");
    print!("\"projected_total_s\":{projected_total:.1},");
    print!("\"target_s\":{TARGET_S:.1},");
    print!("\"feasible\":{feasible}");
    println!("}}");
}
