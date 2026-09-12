# DE-01 resolution: NIST SP 800-38G FF1 adoption

**Status:** current. Records what Task 5.2 shipped: the durable behavior, the
conformance claim, and the accepted leakage. For the finding this resolves
and the options weighed before choosing FF1, see
`docs/security/de-01-fpe-remediation-design.md` (in-tree, excluded from the
rendered docs).

## What changed

The `fpe` strategy's cipher is NIST SP 800-38G FF1 (Algorithms 5 and 6),
AES-256 via the `cryptography` package. It replaced the engine's earlier
home-rolled 8-round HMAC-SHA256 Feistel construction entirely: pre-GA hard
cutover, no dual-primitive period, gated by `SEED_PROTOCOL_VERSION` 6 -> 7.
Every `fpe` column and the `fpe`-branch text-mask spans (`text_mask`'s ZIP,
SSN, phone, PAN detectors) now use FF1; every other engine derivation that
mixes in the version byte also changed output, since the byte is shared
infrastructure, not an FPE-specific flag.

The primitive lives in `src/decoy_engine/transforms/_ff1.py`: a from-the-standard
implementation of Algorithms 5/6 over numeral strings, exact-arithmetic
throughout (no float), AES used forward-only through `cryptography`'s
audited AES implementation (never hand-rolled). `src/decoy_engine/transforms/fpe.py`
is the deployable-profile wrapper: it resolves a column's charset into FF1's
numeral alphabet, enforces the profile below, and calls the primitive.

## Conformance claim

State this precisely, in docs and product copy alike:

> AES-FF1 conformant to NIST SP 800-38G, enforcing the SP 800-38G Rev. 1
> Second Public Draft (2025-02-03) restrictions: FF1 only (FF3/FF3-1 are
> withdrawn following the Beyne 2021 attack) and the raised minimum domain.

Do not claim "NIST-certified" or "FIPS 140 validated." Neither is true: this
is a from-the-standard implementation locked against NIST's published
known-answer vectors plus an independent Wycheproof-derived corpus and a
structurally separate differential oracle, not a CAVP/CMVP-validated module.
Rev. 1 is still a draft; if the final revision changes a pinned parameter
(the minimum domain is the likely candidate), this implementation and claim
need re-review and, if behavior changes, a new algorithm tag.

The DE-01 finding this resolves flagged the inverse claim: the engine used
to say "not NIST FF1" while shipping a Feistel construction that predated a
real one. That framing is now retired everywhere in docstrings and product
copy; it should not reappear in new code without immediately being wrong
again.

## Key and tweak model

One AES-256 key per `(job_seed or secret-derived mask_key, namespace)`:
`derive(mask_key, namespace, FF1_KEY_LABEL)` where
`FF1_KEY_LABEL = b"ff1-key/v1"`, domain-separated from the retired Feistel
label (`b"fpe-key/v1"`) so no old key material is ever reused under the new
cipher. See [key-derivation.md](key-derivation.md) for the shared `derive()`
envelope and `mask_key` sourcing.

The tweak is a pinned wire format built by `transforms.fpe.build_ff1_tweak`:

```
tweak = VERSION(1 byte, 0x01) || SCOPE(1 byte) || LEN(uint16 BE) || identity_utf8
```

`SCOPE` is `0x01` (column), `0x02` (join_group), or `0x03` (text-span);
`identity_utf8` is the column name, `fpe_join_group` value, or span detector
id, encoded strict UTF-8 with no normalization (two normalization-different
identities are intentionally distinct tweaks). This makes the tweak framing
itself part of the pinned contract: two conforming implementations of this
wrapper behave identically given the same inputs.

## Deployable profile (enforced before every FF1 call)

`transforms.fpe._permute` enforces, in order, fail-closed at the first
violation:

1. Key is exactly 32 bytes (AES-256 only).
2. Radix in `[2, 64]`.
3. Alphabet is ordered and duplicate-free (a repeated symbol would make two
   distinct numerals decode to the same character, silently losing
   information).
4. Custom alphabets are restricted to printable ASCII (`0x21`-`0x7E`); this
   sidesteps Unicode grapheme segmentation entirely, since one code point is
   always exactly one symbol in that range.
5. Body length is within `[min_domain_length(radix), 256]`, where
   `min_domain_length` is the smallest length with
   `radix ** length >= FF1_MIN_DOMAIN` (`1,000,000`, pinned to the Rev. 1 2PD
   floor, not a bare literal, so a spec revision is a one-line reviewed
   diff). Below the floor, FF1 is undefined/insecure for any implementation,
   not just this one.
6. Tweak length `<= 256` bytes.
7. Every body character is a member of the resolved alphabet.

A value that fails any of these raises `FpeUnencryptableError` with a code
that distinguishes "domain genuinely too small"
(`fpe.unencryptable_domain`) from "config or wiring problem"
(`fpe.unencryptable_length`). There is no silent fallback: the engine never
emits a value that looks masked but isn't safely reversible.

### Checksum-scheme columns (Luhn, NPI, ISBN-13, VIN, EAN-13, GTIN)

FF1 permutes the non-check-digit body; the check digit is recomputed after
permutation, never stored or carried through. On encrypt, the complete
source identifier (including its check digit) must already satisfy
`checksums.validate()` before any permutation runs: an already-invalid
source (wrong check digit) is not a valid instance of the scheme, and
permuting it would produce output that looks checksum-valid but has no
honest source, so it fails closed (`fpe.checksum_invalid_source`). Decrypt
does not re-validate: the recomputed check digit has nothing external to
check against.

### Sub-floor text-mask spans

Auto-detected text-mask spans (ZIP, SSN, phone, PAN) can't be pre-configured
per-field the way a column can, so a 5-digit ZIP match (domain `10^5 <
1,000,000`) legitimately falls below the FF1 floor even though a 9-digit
ZIP+4 match on the same detector clears it. This is a per-match decision,
not a per-column one. The operator sets `sub_floor_span: "redact" |
"synthetic"` explicitly (no default: leaving it unset raises a fail-closed
error the first time a sub-floor span is actually matched at runtime, since
silently choosing a fallback is exactly the kind of silent behavior this
task closes). `redact` uses the existing redaction token; `synthetic`
produces a deterministic, valid-format synthetic value of the span's type
(e.g. a well-formed but fake ZIP). Both are non-reversible: that is the
honest consequence of a domain too small for FF1, not a limitation of this
implementation. Handling a sub-floor span under either policy emits a
structured `QualityWarning` (`text_mask_sub_floor_span_handled`), so the
substitution is visible in the run's evidence rather than silent.

## Known and accepted leakage (documented, not "fixed")

FF1 is a keyed permutation, not an authenticated encryption scheme. These
properties are inherent to any correct FF1 implementation, not defects in
this one:

- **Deterministic equality leakage.** The same value under the same
  `(key, tweak)` always produces the same ciphertext (this is the point:
  it's what makes joins and cross-cell consistency work). An observer who
  sees the ciphertexts learns which cells share a source value, exactly as
  before, under the Feistel construction.
- **Fixed points are legal.** A permutation can map a value to itself. A
  single ciphertext equal to its plaintext is not a bug; only a systematic
  failure to vary output with the key or tweak would be (see the KAT and
  differential-oracle tests, and the aggregate, not per-sample, invariants
  in `tests/property/test_fpe_invariants.py`).
- **Unauthenticated: a wrong key yields a plausible wrong plaintext.** FF1
  has no integrity tag. Decrypting with the wrong key or tweak produces
  another in-domain value, not a decryption failure. There is no way to
  detect this from the ciphertext alone. Every FF1 reversal through
  `decoy_engine.unmask` therefore reports `reversed_unverified`, including
  when a secret is supplied (an authenticated artifact envelope that could
  upgrade this to a verified status is a separate, larger, out-of-scope
  build).
- **Partial-plaintext prefix disclosure is a separate, already-deferred
  axis.** A pinned non-charset prefix (e.g. the `M` in `M000001` under a
  digits charset) still passes through under `preserve_separators=True`.
  FF1 does not close this; it was already a documented, Cam-deferred
  residual risk before this task and remains one.

## Side-channel posture

AES runs through the reviewed `cryptography`/OpenSSL backend. The numeral
conversion and big-integer arithmetic surrounding it (base conversion,
S-block expansion for domains needing more than one AES block) may be
variable-time; this is accepted for the self-hosted, batch-processing
threat model Decoy targets, and is not advertised as constant-time. If
chosen-input timing attacks or co-tenant adversaries ever enter scope, that
needs dedicated evidence (e.g. dudect or ctgrind) before any different
claim.

## Gate assumption

"Zero deployed ciphertext" (no `fpe`-masked output produced under the
retired Feistel construction is sitting in a downstream system that this
change needs to migrate) is a release-owner assertion, not something this
repository can prove. Pre-GA hard-delete assumes it holds. If it does not
(a masked dataset from before this change needs to stay decryptable), that
is a migration decision for whoever owns that dataset, not an engine
compatibility guarantee.

## Verification evidence

- KAT-locked against NIST's published Algorithm 5/6 sample vectors plus a
  282-vector curated corpus derived from Google's Wycheproof project
  (`tests/vectors/ff1_wycheproof_kat.json`).
- Differentially tested against a structurally independent second FF1
  implementation (`tests/unit/transforms/_ff1_independent_oracle.py`), so
  the production primitive is not its own oracle.
- Exhaustively tested over the full 6-digit decimal domain: 1,000,000
  unique ciphertexts and a complete decrypt round trip.
- Cross-route parity (full-frame, out-of-core, text-mask, native-reference
  KATs) and a full `SEED_PROTOCOL_VERSION` 7 regression across generation,
  masking, vaults, and the five test-flight jobs.
