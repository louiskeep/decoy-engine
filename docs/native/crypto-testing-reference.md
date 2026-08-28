Status: reference
Purpose: Testing playbook for Decoy's native keyed derivation and format-preserving encryption kernels.
Last reviewed: 2026-08-27

# Native Cryptography Testing Reference

## 1. Scope and safety position

This document defines the evidence required before native cryptography can replace a Decoy Python path.
It covers keyed derivation, deterministic pseudonymization, and an FF1-style format-preserving encryption path.
It also covers the format layers around the core permutation.

Decoy is a self-hosted, single-organization batch-masking engine.
It has no network service or multi-tenant trust boundary of its own.
Its main risks are incorrect cryptography, compatibility drift, accidental cleartext, and native memory defects.
Testing does not make a novel construction safe by itself.
Testing can show conformance, preserve compatibility, expose known bug classes, and bound operational risk.

Use established cryptographic primitives and reviewed implementations wherever possible.
Limit custom code to protocol binding, canonical encoding, batching, and required format behavior.
Treat each line of custom permutation or key-schedule code as security-sensitive.

### 1.1 Two different claims require two different gates

Decoy currently has pure-Python reference kernels named `reference_keyed_derivation` and `reference_fpe`.
They reproduce the shipped Python behavior and are the compatibility oracles for the first native kernel.
The current `reference_fpe` path uses Decoy's existing HMAC-SHA256 Feistel construction.
It is not NIST FF1.

Therefore, these claims are separate:

1. **Compatibility claim:** the native code reproduces the current Decoy output byte for byte.
2. **FF1 conformance claim:** an implementation matches the applicable NIST FF1 algorithm and vectors.

A kernel cannot satisfy both claims merely by matching the current `reference_fpe` output.
Round trips and parity with a non-FF1 oracle do not establish FF1 conformance.
If Decoy moves to actual FF1, use a new seed protocol version and a new reviewed Python oracle.
Expect ciphertext to change at that version boundary.
Keep the old vectors to support explicit compatibility and migration tests.

### 1.2 Test evidence and what it cannot prove

| Evidence | What it proves | What it does not prove |
| --- | --- | --- |
| Known-answer tests | Exact agreement on specified inputs | Safety on untested inputs, memory safety, or side-channel resistance |
| Python parity | Compatibility with shipped Decoy behavior | Standards conformance or sound cryptographic design |
| Independent implementation parity | Agreement with a separately implemented algorithm | That both implementations are free from the same specification error |
| Property tests | Broad invariant coverage with minimized counterexamples | Exhaustive correctness over a large domain |
| Exhaustive domain test | Permutation and round-trip behavior for one complete domain and configuration | Correctness for every radix, length, key, and tweak |
| Fuzzing | Robustness against generated malformed and edge inputs | Absence of all crashes or logic defects |
| Timing tests | Evidence of detectable leakage under the measured setup | Constant-time behavior on every compiler, CPU, and deployment |
| Avalanche checks | Gross mixing failures are absent in a sample | Cryptographic security |

## 2. Risk register and detecting tests

Every listed failure needs a targeted test.
Generic round-trip tests are not enough.

| Failure mode | Concrete consequence | Test that detects it |
| --- | --- | --- |
| Secret-dependent branch, comparison, or table lookup | Key or plaintext information can affect runtime or cache access | Review release assembly, use constant-time types, run dudect and ctgrind when the threat model requires them |
| Incorrect FPE domain size calculation | Security bounds fail, or valid inputs are rejected | Boundary tests at `radix^length = 1,000,000`, one below it, and one above it. Use checked integer arithmetic |
| Tweak ignored, truncated, or accidentally constant | Columns or join groups use the same permutation | KATs with nonempty tweaks, tweak-separation statistics, and a mutation test that removes the tweak binding |
| Tweak encoded differently in Rust and Python | Cross-language output drift | Shared vectors containing both logical text and expected UTF-8 bytes, including non-ASCII cases |
| HKDF salt or `info` bound incorrectly | Protocol separation fails and namespaces can collide | RFC 5869 KATs plus Decoy protocol vectors that change one field at a time |
| Namespace omitted or ambiguously framed | Two logical namespaces derive the same seed | Length-prefix ambiguity vectors such as `("ab", "c")` versus `("a", "bc")` |
| Wrong HMAC key normalization | Long or short keys produce wrong results | RFC 4231 cases below and above the hash block size, plus a Decoy boundary vector with an exact 64-byte key |
| Endianness drift | Rust and Python frame lengths or integers differently | Golden framing vectors with byte dumps, including `0`, `255`, `256`, and multi-byte lengths |
| Unicode normalization drift | Canonically equivalent strings produce different bytes | NFC composed and decomposed pairs, combining marks, and supplementary-plane characters |
| Byte, Unicode scalar, or grapheme confusion | Radix positions and lengths differ across languages | Explicit alphabet-index vectors with multi-byte UTF-8 characters. Reject unsupported semantics |
| Draw-order or global-state dependency | Output changes with row order or prior work | Shuffle, prefix, retry, and isolated-row tests with identical row identity |
| Batch-size dependency | Chunk boundaries change output | Compare one batch with partitions of size 1, prime sizes, uneven sizes, and empty batches |
| Off-by-one radix handling | First or last alphabet character encrypts incorrectly | Exercise indices `0`, `1`, `radix - 2`, and `radix - 1` in both halves of the Feistel input |
| Duplicate or reordered alphabet mishandling | Domain is smaller than reported, or mappings silently change | Reject duplicates, pin alphabet order, and test reordered alphabets as different configurations |
| Leading zero loss | Length or formatting changes | KATs with one and many leading zero digits, for encryption and decryption |
| Null treated as an empty value | Nulls become tokens or empty strings become nulls | Mixed null and empty arrays with row-by-row expected output and validity bits |
| Empty value silently passes cleartext | A declared encrypted field is not encrypted | Future FF1 path rejects empty domains unless the product contract explicitly versions a no-op rule |
| Non-ASCII input is silently replaced | Data changes before cryptography and parity breaks | Strict UTF-8 tests, invalid scalar tests, and vectors that pin any deliberate replacement policy |
| Invalid formatted value passes through | PII remains cleartext | Assert a typed, redacted row error and null placeholder at the kernel boundary. Then assert that the execution boundary discards the batch and fails the operation |
| Partial batch returned after an error | Some rows leak or callers process incomplete results | Inject failures at first, middle, and last rows. Assert atomic failure or the documented error contract |
| Error includes key or source value | Logs disclose secrets or PII | Snapshot errors and logs with sentinel values, then assert the sentinels are absent |
| Integer overflow in domain arithmetic | Panic, wraparound, or wrong security check | Fuzz extreme radix and length fields under debug, release, sanitizers, and checked arithmetic |
| Encrypt and decrypt share the same inverse bug | Round trip passes despite nonconformance | External KATs and independent one-way comparisons for both directions |

## 3. Oracle hierarchy and test data

Use test oracles in this order:

1. Published standard vectors for the standard primitive.
2. Reviewed Decoy compatibility vectors for the seed protocol and current outputs.
3. The pure-Python Decoy reference kernel.
4. A trustworthy, independently implemented third library.
5. Algebraic and product properties when no exact expected output exists.

Never generate a golden value from the implementation under test during the same test run.
Never update goldens automatically after a failure.
A golden update is a protocol change and requires review.

### 3.1 Shared vector schema

Store shared vectors in versioned JSON or JSON Lines files.
Use hex for raw bytes.
Keep logical values next to their canonical byte encoding.
A useful record contains these fields:

```json
{
  "case_id": "derive-nfc-001",
  "primitive": "derive",
  "seed_protocol_version": 6,
  "mask_key_hex": "...",
  "namespace_text": "customer.email",
  "namespace_utf8_hex": "637573746f6d65722e656d61696c",
  "source_type": "string",
  "source_text": "...",
  "source_canonical_hex": "...",
  "alphabet": null,
  "radix": null,
  "tweak_utf8_hex": null,
  "expected_hex": "...",
  "expected_error": null,
  "source": "decoy compatibility corpus"
}
```

FPE records also need direction, ordered alphabet, normalized digit indices, key size, tweak bytes,
separator policy, checksum policy, expected formatted output, and expected core-permutation output.
Keep the two outputs separate so wrapper defects do not look like core cipher defects.

Each imported corpus must record:

* the upstream URL and file path
* the upstream tag or commit
* the file SHA-256 digest
* the license
* any transformation made during import
* every skipped case and the reason

### 3.2 Current Decoy compatibility framing

The current source defines seed protocol version 6.
The derivation path first obtains a 32-byte key with HKDF-SHA256.
It uses the literal salt `decoy-engine/determinism/v1` and the UTF-8 namespace as HKDF `info`.
It then computes HMAC-SHA256 over this byte frame:

```text
one protocol-version byte
four-byte big-endian namespace length
namespace UTF-8 bytes
four-byte big-endian source length
canonical source bytes
```

Compatibility vectors must expose this complete frame in hexadecimal.
They must cover lengths 0, 1, 255, 256, and a multi-byte UTF-8 namespace.
They must also prove that moving bytes between namespace and source changes the result.

Current canonical source encoding has type-specific rules.
Strings use NFC followed by UTF-8.
Integers use a length-prefixed minimal two's-complement big-endian representation.
Booleans use one byte.
Aware datetimes use a normalized UTC ISO 8601 form.
Naive datetimes and unsupported floating-point sources fail.
Null is handled before derivation and remains null.
Pin every supported type in a golden vector rather than reimplementing these rules in the test harness.

The current FPE compatibility path derives its key using the source label `fpe-key/v1`.
Its effective tweak is the join-group name when present, otherwise the column name.
The Python path encodes that tweak as UTF-8 with replacement behavior for encoding errors.
Compatibility tests must reproduce that behavior for the existing protocol.
A future FF1 protocol must either specify the same behavior or adopt strict UTF-8 under a new version.

These framing rules describe current Decoy compatibility.
They are not a substitute for the FF1 algorithm specification.

## 4. Known-answer tests

Known-answer tests, or KATs, compare an exact result with an authoritative expected result.
Run encrypt and decrypt directions independently.
Do not derive the decrypt expectation by encrypting in the same test.

### 4.1 HKDF-SHA256

[RFC 5869](https://www.rfc-editor.org/rfc/rfc5869.html) Appendix A contains HKDF vectors.
The text format names the hash, input keying material, salt, `info`, output length, PRK, and OKM.
Parse wrapped hexadecimal lines after removing spaces and line breaks.

For SHA-256, import at least:

* Test Case 1, a basic extract-and-expand case.
* Test Case 2, long inputs and long output.
* Test Case 3, empty salt and empty `info`.

RFC 5869 defines absent salt as a zero string of `HashLen` bytes.
If Decoy policy rejects an empty salt, keep two separate tests.
The raw HKDF helper must pass the RFC vector.
The Decoy protocol wrapper must reject a missing or empty configured salt if that is its contract.

Decoy's current derivation protocol uses HKDF-SHA256 to derive a key and HMAC-SHA256 to bind framed fields.
Its compatibility vectors must pin the exact salt, namespace encoding, frame order, and length byte order.
They must also pin the protocol version byte.

### 4.2 HMAC-SHA256

[RFC 4231](https://www.rfc-editor.org/rfc/rfc4231.html) supplies HMAC-SHA test cases.
The cases contain a key, data, and expected digest in hexadecimal form.
They cover normal keys, long data, keys longer than the hash block size, and truncation.
Use the full SHA-256 result where Decoy requires a full HMAC.
Test deliberate truncation only at the layer that specifies it.

[RFC 2104](https://www.rfc-editor.org/rfc/rfc2104.html) defines HMAC.
It is the primary construction reference.
RFC 4231 is the practical SHA-2 vector source.

### 4.3 NIST FF1 and FF3-1 vectors

The current final [NIST SP 800-38G](https://csrc.nist.gov/pubs/sp/800/38/g/final) was published in March 2016.
Its [FF1 sample PDF](https://csrc.nist.gov/CSRC/media/Projects/Cryptographic-Standards-and-Guidelines/documents/examples/FF1samples.pdf)
shows inputs, outputs, and intermediate values.
The intermediate values are useful when the final ciphertext differs.
They identify the first round or conversion that diverges.

The NIST [Automated Cryptographic Validation Protocol](https://pages.nist.gov/ACVP/)
defines machine-readable validation exchanges.
The [symmetric algorithm specification](https://pages.nist.gov/ACVP/draft-celi-acvp-symmetric.html)
includes AES-FF1 and AES-FF3-1 schemas.
An ACVP vector set is JSON with this shape:

* Top level: `vsId`, `algorithm`, `revision`, `isSample`, and `testGroups`.
* Group: `tgId`, `testType`, `direction`, `keyLen`, `alphabet`, `radix`, and `tests`.
* Test: `tcId`, hexadecimal `key`, hexadecimal `tweak`, `tweakLen`, and `pt` or `ct`.
* Response: matching `tgId` and `tcId`, with the computed `ct` or `pt`.

Keep `tgId` and `tcId` in test names.
This makes a CI failure traceable to the source vector.
Write a small checked loader rather than transcribing vectors by hand.
Reject missing fields, duplicate identifiers, malformed hex, and inconsistent declared lengths.

The [CAVP program page](https://csrc.nist.gov/Projects/Cryptographic-Algorithm-Validation-Program)
links algorithm requirements and validation resources.
For capability-matched FF1 JSON, register a test capability with the NIST Demo ACVTS,
or run a pinned release of the [`usnistgov/ACVP-Server`](https://github.com/usnistgov/ACVP-Server)
generator locally.
As of 2026-08-27, NIST lists AES-FF1 in production and marks AES-FF3-1 as demo-only.
The JSON examples in the ACVP specification are loader fixtures, not a complete validation corpus.
Do not imply that generic legacy CAVP `.req` or `.rsp` archives contain FPE vectors unless a specific archive is pinned.
Passing public or generated vectors does not grant CAVP or ACVP validation.

FF3-1 vectors remain useful for historical regression and for testing deliberate rejection.
They are not a reason to select FF3-1 for new Decoy code.
The February 2025 [second public draft of SP 800-38G Revision 1](https://csrc.nist.gov/pubs/sp/800/38/g/r1/2pd)
removes FF3 and FF3-1 from the document.
As of this review date, that revision remains a draft.

### 4.4 Google Wycheproof

[Google Wycheproof](https://github.com/C2SP/wycheproof) is now maintained in the C2SP organization.
Its `testvectors_v1` directory includes FF1, HKDF-SHA256, and HMAC-SHA256 JSON vectors.
Relevant filenames include `aes_ff1_base*_test.json`, `aes_ff1_radix*_test.json`,
`hkdf_sha256_test.json`, and `hmac_sha256_test.json`.

The general [Wycheproof vector format](https://github.com/C2SP/wycheproof/blob/main/doc/formats.md)
uses metadata, `testGroups`, and cases with `tcId`, `comment`, `flags`, and `result`.
Results are `valid`, `invalid`, or `acceptable`.

Adopt this policy:

* `valid`: the implementation must accept and match the expected output.
* `invalid`: the implementation must reject the case.
* `acceptable`: make an explicit Decoy decision, document it, and test that decision.

Do not treat Wycheproof as a certification or a security proof.
It is a high-value collection of known edge cases and attack-derived tests.
The current repository notes that some FF1 files do not have a formal JSON schema.
Pin the upstream commit and validate all fields used by the loader.

### 4.5 Loader pattern

Keep parsing separate from execution.
The loader returns strongly typed cases and fails before any test executes.

```python
def load_acvp_ff1(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    require(doc["algorithm"] == "ACVP-AES-FF1")
    seen = set()
    for group in doc["testGroups"]:
        radix = checked_radix(group["radix"])
        alphabet = checked_alphabet(group["alphabet"], radix)
        for raw in group["tests"]:
            identity = (group["tgId"], raw["tcId"])
            require(identity not in seen)
            seen.add(identity)
            yield FF1Case.from_acvp(group, raw, alphabet)
```

The test adapter converts digit strings into the implementation's numeral array.
It must not normalize, reorder, deduplicate, or case-fold the published alphabet.

## 5. Differential and parity testing

Differential testing runs the same case through two implementations and compares the results.
For Decoy compatibility, the Python reference is the required oracle.
For actual FF1, add an independent implementation that passes the same NIST vectors.
An implementation is not independent when the production kernel links it or derives code from it.
If Rust uses the `str4d/fpe` crate, use Bouncy Castle or another separately authored FF1 implementation as the third oracle.

### 5.1 Required comparisons

For every valid case, compare:

* output bytes or exact Unicode scalar sequence
* null bitmap
* output data type
* formatted separators
* checksum or Luhn digit
* warnings and typed status values

For every invalid case, compare:

* success or failure
* stable error category
* row index, if the API reports one
* absence of raw key, input, and derived material in the error
* whether any partial output was returned

Do not require exact human-readable error text across languages.
Require a stable machine-readable code and redaction contract.

### 5.2 Golden corpus layers

Use three corpus layers:

1. External standard vectors, unchanged except for checked format conversion.
2. Decoy compatibility goldens, including every existing `HASH_KAT` and `FPE_KAT` case.
3. Seeded generated cases, committed with their seed and generator version.

The generated corpus must cover valid and invalid records.
It must include all supported logical types, alphabets, lengths, tweaks, separators, and checksum modes.
Keep difficult minimized failures as permanent named cases.

### 5.3 Cross-language parity harness

The parity harness must launch Python and Rust through public kernel entry points.
It must not duplicate protocol framing inside the harness.

```text
for case in shared_corpus:
    python_result = python_reference(case)
    rust_result = native_kernel(case)
    assert serialize(rust_result) == serialize(python_result)

    if case.algorithm == "FF1" and independent_ff1_is_available:
        third_result = independent_ff1(case)
        assert core_digits(rust_result) == core_digits(third_result)
```

The serializer must have a canonical representation for bytes, Unicode, nulls, and errors.
Run the reference and native kernel in different processes at least once in CI.
This detects accidental shared state and import-time configuration.

### 5.4 Mutation tests for the harness

A parity suite can be green while omitting a critical field.
Prove the harness can fail by applying controlled mutations in a test-only build:

* remove the namespace from the derivation frame
* change one length prefix from big-endian to little-endian
* replace the tweak with empty bytes
* use the alphabet's sorted order
* skip the last encryption round
* pass through an invalid row

Each mutation must produce at least one failing test.

## 6. Property-based testing

Use [Hypothesis](https://hypothesis.readthedocs.io/) in Python.
Use [proptest](https://proptest-rs.github.io/proptest/) or QuickCheck in Rust.
Prefer proptest when minimized regression files and custom strategies are important.

Generate structured inputs, not only arbitrary bytes.
Build valid configurations first, then derive invalid cases with one controlled mutation.
Record every seed needed to reproduce a failure.

### 6.1 Round trip

For every accepted FPE value:

```python
@given(valid_fpe_case())
def test_round_trip(case):
    ciphertext = encrypt(case.plaintext, case.key, case.tweak, case.format)
    plaintext = decrypt(ciphertext, case.key, case.tweak, case.format)
    assert plaintext == case.plaintext
```

Round trip proves that the selected encrypt and decrypt paths are inverses for the generated case.
It does not prove standard conformance.

### 6.2 Determinism and batch invariance

For derivation and encryption, assert exact repeatability:

```python
@given(valid_batch_case())
def test_batch_invariance(case):
    whole = native(case.values, case.config)
    for partition in partitions(case.values):
        pieces = [native(chunk, case.config) for chunk in partition]
        assert concatenate(pieces) == whole
```

The partition generator must include:

* one batch
* one row per batch
* uneven and prime-sized batches
* empty batches between nonempty batches
* different Arrow chunk layouts

Also test separate processes.
Use different `PYTHONHASHSEED` values, thread counts, process start methods, and row orders.
Compare results after restoring the original row order.
The same logical row must not depend on prior rows or draw order.

### 6.3 Format preservation

For each accepted FPE value, assert:

* output length equals input length under the defined character-count model
* every encrypted symbol belongs to the same ordered alphabet
* fixed separators remain at the same positions
* leading zero symbols remain representable and length is unchanged
* the output passes the configured checksum validator
* null remains null and does not invoke cryptography

Do not mix core-permutation assertions with wrapper assertions.
Test the normalized digit sequence before format reconstruction.

### 6.4 Tweak separation

A different tweak selects a different permutation.
For a sufficiently large domain, sample many values and compare the two ciphertext sets.
Use a fixed statistical threshold with a false-alarm analysis.

Do not assert that different tweaks always produce different ciphertext for one plaintext.
Any two independent permutations can map one value to the same result with probability `1 / N`.
Instead, reject suspicious aggregate behavior, such as identical results for almost every sample.
Include an exact KAT where the expected outputs for two tweaks are already known.

### 6.5 Bijection and collision checks

FPE encryption must be a permutation on its declared message space.
For routine property tests, sample distinct plaintexts and reject duplicate ciphertexts within the sample.
Account for the expected birthday collision behavior only if the sample is not a true subset of one permutation.

For a slow exhaustive gate, enumerate radix 10 with length 6.
This is exactly 1,000,000 values, which meets the draft NIST domain floor.
For one fixed key and tweak:

1. Encrypt every value from `000000` through `999999`.
2. Assert that every ciphertext has six decimal digits.
3. Assert that the ciphertext cardinality is 1,000,000.
4. Decrypt every ciphertext and recover its original plaintext.

Use a bitset or external sort if memory usage matters.
Never weaken the public small-domain guard merely to make this test faster.
An internal round-function test can use small domains when it is not a public encryption path.

### 6.6 Key sensitivity

Change one key bit while holding all public inputs fixed.
Over a large domain and sample, outputs must behave like independently keyed permutations.
Use aggregate thresholds, not a per-case inequality assertion.
A fixed point or cross-key collision can occur by chance.

For derivation, a one-bit key change must alter the full HMAC-SHA256 output in the KAT cases.
Use a distribution check only as a smoke test.

### 6.7 Null, empty, and edge values

Generate these cases deliberately:

* a null Arrow slot
* empty string and empty byte string
* one-symbol values
* odd and even lengths
* values at the exact domain threshold
* values one unit below the threshold
* every alphabet boundary symbol
* composed and decomposed Unicode
* combining marks and supplementary-plane characters
* lone surrogate input at a Python boundary
* the longest accepted value and one longer value

Null and empty are different.
For keyed derivation, null normally remains null while empty is a real source value.
For future FF1, an empty value has no acceptable one-value domain.
Reject it unless a versioned product rule explicitly defines another behavior.

The current compatibility reference can preserve its existing empty-input behavior.
That compatibility rule must not silently become the security rule for a new FF1 version.

### 6.8 Rust proptest sketch

```rust
proptest! {
    #[test]
    fn ff1_round_trip(case in valid_ff1_cases()) {
        let ct = encrypt(&case.key, &case.tweak, &case.digits, &case.cfg)?;
        prop_assert_eq!(ct.len(), case.digits.len());
        prop_assert!(ct.iter().all(|d| *d < case.cfg.radix));
        let pt = decrypt(&case.key, &case.tweak, &ct, &case.cfg)?;
        prop_assert_eq!(pt, case.digits);
    }
}
```

Save minimized failures under version control.
Run them before new randomized cases.

## 7. Fuzzing the native kernel

Use [cargo-fuzz and libFuzzer](https://rust-fuzz.github.io/book/).
Build small deterministic targets with no file, clock, network, or global-random dependencies.

### 7.1 Required fuzz targets

1. **Protocol decoder:** arbitrary bytes into vector and batch decoders.
2. **Canonical encoder:** all supported logical values and length boundaries.
3. **Alphabet mapper:** arbitrary alphabet, radix, text, and Unicode input.
4. **FF1 core:** structured valid key, tweak, radix, and numeral arrays.
5. **Format wrapper:** separator extraction and reconstruction.
6. **Luhn and checksum wrapper:** valid and invalid lengths, digits, and checksum positions.
7. **FFI or Arrow boundary:** null bitmaps, offsets, slices, and malformed metadata.

### 7.2 Fuzz oracles

Use more than crash detection.
Each structured valid input can assert:

* decrypt of encrypt equals the original numeral array
* output digits are below the radix
* output length is unchanged
* native output equals the Python reference for the compatibility protocol
* native output equals a trusted FF1 implementation for actual FF1
* invalid input returns an error without panic
* error text does not contain sentinel key or source bytes

For parity fuzzing, cap input size and batch length.
This keeps the Python oracle fast enough for useful throughput.
Run a faster Rust-only target for deep native coverage.

### 7.3 Corpus and interpretation

Seed each target with:

* all applicable KAT inputs
* all Decoy golden cases
* exact domain boundaries
* minimized property-test regressions
* prior production incidents with synthetic values

A crash, panic, timeout, sanitizer report, or out-of-bounds access is a release blocker.
A round-trip failure indicates a permutation or wrapper defect.
A parity failure indicates a compatibility, encoding, framing, or algorithm defect.
An unexpected acceptance indicates a fail-closed defect.
Keep every minimized finding in the regression corpus.

Run AddressSanitizer and UndefinedBehaviorSanitizer where the build supports them.
Use Miri on small unsafe or FFI-adjacent units when practical.
Rust's type safety does not protect unchecked FFI, raw pointers, or foreign buffer metadata.

## 8. Constant-time and side-channel testing

### 8.1 Threat-model priority

Decoy normally runs offline for one organization.
There is no built-in remote timing oracle and no cross-tenant service boundary.
In that deployment, correctness, fail-closed behavior, compatibility, and memory safety have higher priority.

Timing leakage moves into scope when any of these conditions apply:

* an untrusted local process shares the CPU or cache hierarchy
* an attacker can submit chosen values and measure job duration
* the native kernel is placed behind a service boundary
* process-level isolation is weak and the masking key protects against local users

Document the deployment decision.
Do not claim that timing is irrelevant merely because the initial use is batch processing.

### 8.2 Code construction

Use reviewed AES, SHA-256, HMAC, and HKDF implementations.
Avoid secret-indexed tables and early-exit comparisons.
Use the Rust [`subtle`](https://docs.rs/subtle/latest/subtle/) crate for constant-time equality and selection.
Its types help express intent, but they do not prove compiler output is constant time.
Test optimized release builds because debug assertions can add branches.

Branches on public radix, public length, format, and tweak length are usually acceptable.
Branches or memory indexes based on keys, derived material, or secret plaintext digits require review.

### 8.3 dudect

[dudect](https://github.com/oreparaz/dudect) compares timing distributions for two input classes.
Create fixed-versus-random classes while holding public metadata constant.
Test key-dependent and plaintext-dependent paths separately.
Pin CPU affinity, reduce frequency scaling noise, and use the shipping optimization level.

For an in-scope release gate, collect at least 10 million measurements per target.
Run the experiment twice on each supported CPU family.
Treat a stable absolute Welch t statistic of 4.5 or greater as an investigation and release failure.
This threshold is a leakage alarm, not proof of constant-time execution.

### 8.4 ctgrind and assembly inspection

[ctgrind](https://github.com/agl/ctgrind) uses Valgrind to find branches and memory accesses influenced by marked secrets.
It can expose secret-dependent control flow that noisy wall-clock tests miss.
Its maintenance and platform support are limited, so record the exact working toolchain.

Inspect release assembly for the smallest secret-handling functions.
Repeat inspection after compiler, target-feature, or cryptographic backend changes.
No single timing tool covers compiler transformations, microarchitecture, and all call paths.

## 9. Diffusion and avalanche sanity checks

Avalanche testing is a smoke test for gross mixing errors.
It is not a proof of pseudorandomness or cryptographic security.

For a 256-bit derivation result:

1. Choose a fixed corpus of keys and framed inputs.
2. Flip one input bit, then one key bit.
3. Measure Hamming distance between the original and changed outputs.
4. Inspect the mean and distribution across thousands of samples.

A sound 256-bit pseudorandom output has an expected mean near 128 changed bits.
Do not require exactly half the bits in one case.
Use a predeclared confidence band and retain the raw summary.

For FPE, map the numeral string to a fixed-width integer before measuring bits.
Radix encoding and small domains bias the metric.
Use this result only to catch failures such as an ignored half, ignored tweak, or unchanged output block.

## 10. Inputs, outputs, and pass criteria

### 10.1 Primitive contracts

| Primitive | Input space | Expected output | Concrete pass criteria | Concrete fail criteria |
| --- | --- | --- | --- | --- |
| Keyed `derive` | Usable mask key, seed protocol version, namespace bytes, canonical source bytes | Fixed-length pseudorandom bytes, or specified hexadecimal token at the wrapper | All RFC primitive KATs pass. All Decoy framing KATs match byte for byte. Namespace and source changes are bound. Repeat calls and processes match | Missing key, bad key length, unsupported version, ambiguous encoding, or unsupported source type returns a typed error. No cleartext or partial token |
| Keyed hash wrapper | Supported logical value or null, namespace, output encoding and optional length | Null for null input. Deterministic non-null token for non-null input | Python parity for value, type, null bitmap, length, and casing. Empty differs from null | Unsupported type, unsafe truncation, unusable key, or invalid length fails closed |
| FF1 encrypt core | AES key of supported size, allowed radix, ordered alphabet or numeral array, tweak, message in domain | Same number of numerals, every numeral less than radix | NIST and ACVP KAT ciphertext matches. Domain floor passes. Independent implementation parity and deterministic round trip pass | Invalid digit, small domain, unsupported radix or key size, overflow, or bad tweak fails without output |
| FF1 decrypt core | Same configuration plus valid ciphertext numeral array | Original plaintext numeral array | NIST decrypt KAT matches independently. Encrypt and decrypt configurations are identical. Full-domain test has no collision | Wrong configuration, invalid digit, small domain, or malformed input fails closed |
| Formatted FPE encrypt | Core digits plus separators, alphabet, tweak, and optional checksum rule | Same external format, valid checksum, encrypted core | Core result matches oracle. Separator positions match. Checksum validates. Exact output matches versioned golden | Invalid source format, unsupported separator layout, or impossible checksum fails with no cleartext pass-through |
| Formatted FPE decrypt | Formatted ciphertext and identical versioned configuration | Original formatted plaintext | Exact golden and round trip match. Checksum policy behaves as specified | Invalid ciphertext format, wrong version, or invalid checksum metadata fails closed |

### 10.2 Required edge matrix

| Dimension | Cases |
| --- | --- |
| Key | missing, empty, minimum accepted, each supported AES size, one bit changed, malformed encoding |
| Namespace | empty if allowed, ASCII, NFC Unicode, delimiter-like bytes, embedded NUL, very long |
| Source | null, empty, ASCII, NFC pair, integer boundaries, decimal, aware time, naive time rejection, unsupported float if policy rejects it |
| Tweak | empty if allowed, one byte, typical column name, join-group name, maximum length, one byte changed, non-ASCII UTF-8 |
| Radix | 2, 10, 36, 62 if supported, maximum supported, zero, one, and one above maximum |
| Alphabet | first and last symbol, duplicate symbol, reordered symbols, multi-byte symbols, symbol outside alphabet |
| Length | zero, one, odd, even, exact domain floor, one below floor, maximum, one above maximum |
| Format | no separators, leading and trailing separators, repeated separators, mixed separator types, separator-only input |
| Checksum | none, Luhn valid, Luhn invalid, checksum-only input, changed payload digit, unsupported algorithm |
| Batch | empty, one row, all null, mixed null, failure at first, middle, and last row, multiple Arrow chunks |

## 11. FPE-specific pitfalls

### 11.1 Minimum domain size

The February 2025 second public draft of SP 800-38G Revision 1 requires an FF1 domain of at least 1,000,000.
For a fixed radix and numeral length, enforce:

```text
radix^length >= 1,000,000
```

The draft also requires `2 <= radix <= 2^16` and `2 <= minlen <= maxlen < 2^32`.
Decoy can support a narrower range.
Tests must reject values outside both the standard range and the declared Decoy range.

The 2016 final publication had a lower normative floor and recommended at least 1,000,000.
Decoy must use the stronger draft floor for new work.
Use checked exponentiation or compare with early saturation.
Do not use floating-point logarithms for this security decision.

Test the exact threshold and the closest values on each side.
For decimal input, length 6 passes and length 5 fails.
For a rejected small domain, choose one documented action:

* reject the value and fail the operation
* route the field to an approved vault or tokenization strategy
* use a different masking strategy with an explicit migration contract

Never fall back to cleartext.
Never repeat encryption to enlarge the apparent domain.
Never silently switch to an unreviewed cipher.

### 11.2 FF3, FF3-1, and FF1

The original SP 800-38G specified FF1 and FF3.
Durak and Vaudenay published
[Breaking the FF3 Format-Preserving Encryption Standard over Small Domains](https://eprint.iacr.org/2017/521).
NIST responded with a proposed FF3-1 revision that changed the tweak construction and domain constraints.
NIST summarized that response in its
[2017 FF3 cryptanalysis notice](https://csrc.nist.gov/news/2017/recent-cryptanalysis-of-ff3).

Later analysis by Beyne found another weakness affecting FF3 and FF3-1.
The current SP 800-38G Revision 1 second public draft removes both.
It retains FF1 with a stronger minimum domain requirement.

Use FF1 as the default for new standards-based Decoy work.
Do not add FF3 or FF3-1 merely because a convenient library implements it.
Keep FF3-1 vectors only for historical comparison, migrations, and rejection tests.

### 11.3 Tweak length and handling

Treat tweak bytes as protocol input, not display text.
Pin all of these rules:

* source of the tweak, such as column name or join-group name
* Unicode normalization policy
* UTF-8 encoding policy
* empty-tweak policy
* maximum tweak length
* length framing and byte order
* behavior after a column rename

Different join-group columns require the same tweak when cross-column joinability is intended.
Unrelated columns require separated tweaks.
Test both cases with exact vectors.

Do not hash or truncate a tweak unless the seed protocol specifies the operation.
If a library limits tweak length, reject excess input or define a reviewed preprocessing protocol.
Record that protocol in the vector file.
FF1 names the implementation's maximum tweak byte length `maxTlen`.
An accepted tweak length `t` must satisfy `0 <= t <= maxTlen`.
Test zero, `maxTlen`, and `maxTlen + 1` bytes.

### 11.4 Radix and alphabet

The ordered alphabet defines numeral indexes.
Changing order changes the permutation even when the character set is unchanged.
Reject duplicates rather than silently deduplicating them in a new protocol.

Define whether a character means a byte, Unicode scalar value, or grapheme cluster.
Rust `char` and Python iteration both operate on Unicode scalar-like code points for valid text,
but lone surrogate behavior differs.
Grapheme clusters require a separate segmentation protocol and are not implicit.

Test these details:

* smallest and largest supported radix
* index zero and index `radix - 1`
* leading zero numeral
* alphabets with UTF-8 multi-byte symbols, if supported
* composed and decomposed forms
* duplicate and reordered alphabets
* odd and even message lengths
* conversion values near integer limb boundaries

If the actual FF1 library accepts numeral arrays, perform text-to-numeral mapping outside the core.
Test that mapper independently.

### 11.5 Separators and checksums are separate layers

Core FF1 permutes a numeral string.
It does not know about dashes, spaces, parentheses, Luhn, or application checksums.
Build and test the formatted path as a composition:

```text
parse format
  -> extract encrypted symbols and fixed positions
  -> validate alphabet and domain
  -> apply core permutation
  -> recompute or preserve checksum by the versioned rule
  -> restore separators
  -> validate final format
```

Each arrow needs unit tests and negative tests.
Keep a vector for the core digits before checksum reconstruction.
This isolates an FF1 failure from a Luhn or separator failure.

For PAN-like values, define whether the check digit participates in the permutation.
A common design encrypts the payload and recomputes the Luhn digit.
That design is not a permutation over the complete original PAN string unless the protocol defines the inverse mapping.
Prove decryptability and collision behavior for the complete wrapper, not only the core.

## 12. Public examples from libraries and enterprises

These projects demonstrate useful techniques.
Their presence in this list is not an endorsement of every algorithm or release.
Pin versions and inspect current code before using any project as an oracle.

| Project | Public evidence | Technique demonstrated | Decoy use |
| --- | --- | --- | --- |
| Rust `fpe` crate | [`str4d/fpe`](https://github.com/str4d/fpe), including `src/ff1.rs` and `proptest-regressions/ff1` | NIST vector modules, property tests, persistent minimized regressions, and domain/radix validation | Candidate FF1 comparator only when production does not link or derive from it. Do not infer an audit |
| Bouncy Castle Java | [`SP80038GTest.java`](https://github.com/bcgit/bc-java/blob/main/core/src/test/java/org/bouncycastle/crypto/test/SP80038GTest.java) | AES-128, AES-192, and AES-256 FF1 KATs, round trips, wide radix, invalid input, buffer errors, and regression cases | Strong third-language test pattern and possible independent FF1 comparator |
| mysto `python-fpe` | [`mysto/python-fpe`](https://github.com/mysto/python-fpe) | Official and ACVP-style FF3 or FF3-1 vector loading | Historical loader example only. It does not provide an FF1 oracle and its algorithm is removed from the latest NIST draft |
| pyFPE | [`pyFPE` package page](https://pypi.org/project/pyFPE/) | Small-domain override and FF3-oriented API behavior | Cautionary example. Verify package identity and version, and never use a small-domain override in production Decoy code |
| Google Wycheproof | [`C2SP/wycheproof`](https://github.com/C2SP/wycheproof) | Shared adversarial JSON vectors with valid, invalid, and acceptable classifications | Import applicable FF1, HKDF, and HMAC cases with pinned provenance |
| Google Tink | [`tink-crypto/tink`](https://github.com/tink-crypto/tink) | Narrow primitive APIs, cross-language implementations, and compatibility-focused testing | Model API discipline and cross-language fixtures. Tink is not an FF1 oracle |
| HashiCorp Vault Transform | [Transform secrets engine](https://developer.hashicorp.com/vault/docs/secrets/transform) and [FF3 tweak details](https://developer.hashicorp.com/vault/docs/secrets/transform/ff3-tweak-details) | Separate roles, templates, alphabets, tweaks, formats, and checksum behavior | Model wrapper-layer tests. Do not copy its FF3-1 algorithm choice for new work |
| AWS Database Encryption SDK for DynamoDB | [Repository](https://github.com/aws/aws-database-encryption-sdk-dynamodb) and [TestVectors directory](https://github.com/aws/aws-database-encryption-sdk-dynamodb/tree/main/TestVectors) | Versioned encrypt/decrypt JSON fixtures read across languages and releases | Model cross-version and cross-language compatibility artifacts. It is not an FPE validation source |
| AWS Crypto Tools Test Vector Framework | [`awslabs/aws-crypto-tools-test-vector-framework`](https://github.com/awslabs/aws-crypto-tools-test-vector-framework) | Manifest-driven interoperability tests across implementations | Model a shared manifest and runner boundary |

HashiCorp's documentation also makes a useful product point.
FPE does not automatically preserve a valid check digit.
Template matching, alphabet mapping, tweak policy, and checksum behavior need their own tests.

AWS's test-vector repositories demonstrate another important practice.
New language implementations read records written by old versions.
Decoy needs the same backward-compatibility direction for every supported seed protocol version.

## 13. Recommended Decoy test plan

Build the suites in this order.
A later suite does not excuse a failure in an earlier suite.

### Gate 0: protocol specification and vector schema

Before native implementation work:

* freeze the seed protocol version and exact byte framing
* define canonical encoding for every accepted logical type
* define null, empty, Unicode, tweak, alphabet, separator, and checksum behavior
* state whether the target is legacy Decoy FPE or actual FF1
* add provenance and schema checks for every vector file

Acceptance: every input byte is explainable from the specification.
No implementation-specific behavior is the only specification.

### Gate 1: primitive KAT suite

Run:

* all applicable RFC 5869 SHA-256 cases
* all applicable RFC 4231 HMAC-SHA256 cases
* NIST FF1 sample vectors for an actual FF1 path
* current applicable ACVP AES-FF1 cases
* applicable Wycheproof FF1, HKDF-SHA256, and HMAC-SHA256 cases
* all existing Decoy keyed-derivation and FPE KATs for their protocol versions

Acceptance:

* 100 percent of applicable vectors pass in both required directions.
* No case is skipped without a named incompatibility and review record.
* Invalid vectors fail with the expected category.
* Legacy `reference_fpe` is not credited with passing NIST FF1 vectors.

### Gate 2: parity with the Python reference

Run the full committed golden corpus through `reference_keyed_derivation`, `reference_fpe`, and Rust.
Include at least 10,000 seeded valid and invalid records per primitive and supported configuration family.
Compare values, bytes, nulls, types, wrappers, status codes, and redaction.

Acceptance:

* 100 percent byte-for-byte parity for valid compatibility cases.
* 100 percent agreement on typed success or failure for invalid cases.
* Zero cleartext pass-through cases.
* Every controlled harness mutation causes a failure.

For a new FF1 protocol version, repeat this gate against its new Python FF1 reference.
Do not compare actual FF1 ciphertext with the legacy custom Feistel output.

### Gate 3: property suite

Run round-trip, determinism, format, tweak, key, null, edge, and failure properties.
Use both Hypothesis and proptest where the same layer exists in both languages.

Acceptance:

* 10,000 generated examples per high-value property and configuration family in release CI.
* 100,000 examples per property in the nightly job.
* Zero falsifying examples.
* Every minimized failure is saved as a permanent regression before the fix is accepted.

### Gate 4: process and batch determinism

Run the entire compatibility corpus in fresh processes.
Use multiple hash seeds, thread counts, row orders, and batch partitions.
Run the shipping build on Linux x86_64 and Linux aarch64 when both are supported deployment targets.

Acceptance:

* Every serialized result is identical after row order is restored.
* Concatenated chunk results equal the one-batch result.
* Warnings and global row indexes remain stable.
* Retry after a failed batch does not change later output.

### Gate 5: exhaustive permutation check

Enumerate the complete six-digit decimal domain for at least one actual FF1 key and tweak.
Repeat for a second key or tweak in the scheduled release job when runtime permits.

Acceptance:

* 1,000,000 distinct inputs produce 1,000,000 distinct outputs.
* Every output is six decimal digits.
* Every ciphertext decrypts to its original plaintext.
* The test produces no panic, overflow, or nondeterministic result.

### Gate 6: native fuzzing and dynamic analysis

Run each required cargo-fuzz target for 10 minutes in ordinary CI.
Before the first production release, accumulate at least 24 CPU-hours per target.
Repeat the 24 CPU-hour campaign after changes to framing, unsafe code, alphabet conversion, or core rounds.

Acceptance:

* Zero crashes, panics, timeouts, sanitizer findings, parity failures, or round-trip failures.
* Zero unexpected acceptance of invalid formatted values.
* All earlier findings remain in the corpus and stay fixed.

### Gate 7: conditional side-channel suite

Apply this gate when the deployment threat model includes local co-tenancy, chosen input timing,
or a service boundary.

Acceptance:

* No secret-dependent branch or memory index found by review or ctgrind in covered paths.
* Two dudect runs per CPU family complete with no stable absolute t statistic at or above 4.5.
* Results come from the optimized shipping build.
* Any exception has a documented threat analysis and owner approval.

### Gate 8: independent review and rollout evidence

Require a crypto-aware reviewer who did not write the kernel.
Review protocol framing, standard conformance, vector provenance, unsafe boundaries, and error redaction.

Before full replacement, run the native kernel in shadow mode on synthetic or authorized test data.
Compare it with the Python reference without logging PII or keys.
Keep a tested rollback to the Python path for the same seed protocol version.

Acceptance:

* No open blocker or high-severity finding.
* All gates apply to the exact release artifact and source revision.
* The rollback produces identical outputs for the compatibility protocol.
* Metrics expose mismatch counts and error categories without exposing values.

## 14. Release checklist

### Protocol and inputs

* [ ] The algorithm name and seed protocol version are explicit.
* [ ] Legacy custom FPE and actual FF1 are not described as the same algorithm.
* [ ] Key, salt, `info`, namespace, source, and tweak binding are byte-specified.
* [ ] Integer lengths and values have a pinned byte order.
* [ ] Unicode normalization and UTF-8 error behavior are pinned.
* [ ] Null and empty behavior are distinct and tested.
* [ ] Alphabet order is semantic and duplicate handling is explicit.
* [ ] Domain arithmetic uses checked integers and enforces at least 1,000,000.
* [ ] Unsupported formats fail closed.

### Exact correctness

* [ ] Applicable RFC, NIST, ACVP, and Wycheproof vectors pass 100 percent.
* [ ] Every current Decoy KAT remains pinned to its protocol version.
* [ ] Rust matches the applicable Python oracle byte for byte.
* [ ] Actual FF1 also matches an independent standards-conformant implementation.
* [ ] Encrypt and decrypt directions have independent expected values.
* [ ] Harness mutation tests prove that key fields affect comparisons.

### Invariants and robustness

* [ ] Round trip, determinism, format, tweak, key, and edge properties pass.
* [ ] Cross-process and cross-batch matrices pass.
* [ ] The six-digit exhaustive permutation test has zero collisions.
* [ ] Required fuzz targets meet their time budget with zero findings.
* [ ] Unsafe, FFI, and Arrow boundaries pass sanitizer or equivalent checks.
* [ ] Errors contain no raw key, source, tweak-derived secret, or intermediate state.

### Operational decision

* [ ] Timing leakage is either tested or explicitly out of scope for the deployment.
* [ ] A crypto-aware independent review has no open blocker or high finding.
* [ ] Vector sources, digests, licenses, and transformations are recorded.
* [ ] The exact release build and revision produced the evidence.
* [ ] Shadow comparison has zero unexplained mismatches.
* [ ] Rollback to the compatible Python path is tested.

## 15. Confidence threshold

The Rust kernel is confident enough to replace the Python path only when every applicable gate passes.
The evidence must bind to the exact release artifact.
There must be zero unexplained parity mismatches, zero cleartext fallbacks, and no open high-severity finding.

For the compatibility protocol, confidence means exact reproduction of the Python reference.
For actual FF1, confidence also requires NIST conformance vectors and an independent FF1 comparison.
Neither claim substitutes for the other.

This threshold does not mean the implementation is proven secure.
It means Decoy has standards evidence, compatibility evidence, broad invariant coverage,
native robustness evidence, a reviewed threat decision, and a tested rollback.

## 16. References

### Standards and primary cryptographic material

1. NIST, [SP 800-38G: Recommendation for Block Cipher Modes of Operation, Methods for Format-Preserving Encryption](https://doi.org/10.6028/NIST.SP.800-38G), March 2016.
2. NIST, [SP 800-38G Revision 1, Second Public Draft](https://csrc.nist.gov/pubs/sp/800/38/g/r1/2pd), February 2025. As of 2026-08-27, this remains a draft.
3. NIST, [FF1 sample values](https://csrc.nist.gov/CSRC/media/Projects/Cryptographic-Standards-and-Guidelines/documents/examples/FF1samples.pdf).
4. NIST, [Automated Cryptographic Validation Protocol](https://pages.nist.gov/ACVP/) and [symmetric algorithm specification](https://pages.nist.gov/ACVP/draft-celi-acvp-symmetric.html).
5. NIST, [Cryptographic Algorithm Validation Program](https://csrc.nist.gov/Projects/Cryptographic-Algorithm-Validation-Program).
6. NIST, [SP 800-108 Revision 1 Update 1: Recommendation for Key Derivation Using Pseudorandom Functions](https://csrc.nist.gov/pubs/sp/800/108/r1/upd1/final), February 2024. This is related KDF guidance, not Decoy's HKDF specification.
7. Krawczyk and Eronen, [RFC 5869: HMAC-based Extract-and-Expand Key Derivation Function](https://www.rfc-editor.org/rfc/rfc5869.html), May 2010.
8. Krawczyk, Bellare, and Canetti, [RFC 2104: HMAC, Keyed-Hashing for Message Authentication](https://www.rfc-editor.org/rfc/rfc2104.html), February 1997.
9. Nystrom, [RFC 4231: Identifiers and Test Vectors for HMAC-SHA-224, HMAC-SHA-256, HMAC-SHA-384, and HMAC-SHA-512](https://www.rfc-editor.org/rfc/rfc4231.html), December 2005.
10. Bellare, Rogaway, and Spies, [The FFX Mode of Operation for Format-Preserving Encryption](https://csrc.nist.gov/csrc/media/projects/block-cipher-techniques/documents/bcm/proposed-modes/ffx/ffx-spec.pdf), submission to NIST.
11. Durak and Vaudenay, [Breaking the FF3 Format-Preserving Encryption Standard over Small Domains](https://eprint.iacr.org/2017/521), 2017.
12. NIST, [Recent Cryptanalysis of FF3](https://csrc.nist.gov/news/2017/recent-cryptanalysis-of-ff3), 2017.
13. Beyne, [Cryptanalysis of FF3-1 and FEA](https://doi.org/10.1007/978-3-030-84242-0_3), CRYPTO 2021.

### Test vectors, tools, and implementations

14. C2SP, [Google Wycheproof](https://github.com/C2SP/wycheproof) and [vector format documentation](https://github.com/C2SP/wycheproof/blob/main/doc/formats.md).
15. str4d, [Rust `fpe` crate repository](https://github.com/str4d/fpe).
16. Bouncy Castle, [Java cryptography repository](https://github.com/bcgit/bc-java) and [SP 800-38G tests](https://github.com/bcgit/bc-java/blob/main/core/src/test/java/org/bouncycastle/crypto/test/SP80038GTest.java).
17. mysto, [Python FPE repository](https://github.com/mysto/python-fpe).
18. pyFPE maintainers, [pyFPE package](https://pypi.org/project/pyFPE/). Verify the exact package and release before use.
19. Google, [Tink cryptographic library](https://github.com/tink-crypto/tink).
20. HashiCorp, [Vault Transform secrets engine](https://developer.hashicorp.com/vault/docs/secrets/transform) and [FF3 tweak details](https://developer.hashicorp.com/vault/docs/secrets/transform/ff3-tweak-details).
21. AWS, [Database Encryption SDK for DynamoDB](https://github.com/aws/aws-database-encryption-sdk-dynamodb) and its [TestVectors directory](https://github.com/aws/aws-database-encryption-sdk-dynamodb/tree/main/TestVectors).
22. AWS Labs, [Crypto Tools Test Vector Framework](https://github.com/awslabs/aws-crypto-tools-test-vector-framework).
23. Rust Fuzz Project, [Rust Fuzz Book](https://rust-fuzz.github.io/book/).
24. Reparaz, [dudect](https://github.com/oreparaz/dudect).
25. Langley, [ctgrind](https://github.com/agl/ctgrind).
26. Dalek Cryptography, [`subtle` crate](https://docs.rs/subtle/latest/subtle/).
27. HypothesisWorks, [Hypothesis documentation](https://hypothesis.readthedocs.io/).
28. proptest contributors, [proptest documentation](https://proptest-rs.github.io/proptest/).
