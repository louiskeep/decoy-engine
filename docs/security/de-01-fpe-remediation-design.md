# DE-01 FPE Remediation Design: Custom Format-Preserving Encryption

**Finding:** DE-01 (CRITICAL) in
[adversarial-architecture-review-2026-07-12.md](../adversarial-architecture-review-2026-07-12.md).
**Status:** design brief. No source under `src/` changed by this document.
**Author scope:** security/crypto tech-lead decision brief to unblock the product owner.

This document separates the work into two clearly labelled tiers:

- **ENGINE-BUILDABLE-NOW**: strict containment of the current primitive. Reject out-of-domain
  values fail-closed, ban `preserve_separators=False` on sensitive/disguise fields, typed formats,
  and stop describing the primitive as NIST-modeled. Reversible, flag-gated, no product-fork
  decision required.
- **CAM-GATED**: which primitive to adopt long-term (FF1 vs vault tokenization vs contain) and the
  rekey / version-migration plan.

---

## 1. The exact failure modes

Decoy sells FPE as reversible format-preserving encryption and uses it for the highest-sensitivity
identifiers. The shipped primitive is a custom construction with three disclosure paths and one
reversibility contradiction. All are code-true against `src/decoy_engine/transforms/fpe.py`.

**1a. Custom 8-round HMAC/Feistel, explicitly not FF1.**
`transforms/fpe.py:9-25` (module docstring) and `:55-82` (`_ROUNDS = 8`, `_prf`, `_feistel`)
describe an 8-round type-II Feistel over `Z_(r^u) x Z_(r^v)` with an HMAC-SHA256 round function.
The docstring states plainly: "this is not NIST FF1 (which requires AES-CBC ...)" and "Defer a
hard AES dep until a customer asks for NIST SP 800-38G compliance by name." It is a hand-rolled
cipher, contrary to the repo's own established-methodology rule and to
[OWASP Cryptographic Storage](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
guidance against custom algorithms.

**1b. Out-of-charset characters retained in the clear (mixed value).**
`transforms/fpe.py:366-402` (`_fpe_value`, `preserve_separators=True` branch): only positions whose
character is in the charset are permuted (`positions = [i ... if ch in charset_set]`, line 387);
`result = list(val)` (line 399) then overwrites only those positions. Every out-of-charset
character survives verbatim. Under a `digits` charset, `STATUS-1` discloses `STATUS-` unchanged.
The repo discloses this itself at `docs/what-we-cannot-prove.md:147-163`.

**1c. Whole-value no-op under `preserve_separators=False`.**
`transforms/fpe.py:403-404`: `if not all(ch in charset_set for ch in val): return val`. If any single
character is outside the charset, the entire value is returned unchanged. The live V2 handler
(`_strategies/_fpe.py:110-116`) calls `fpe_encrypt_value` directly and hits this branch with no
warning at all. (The legacy `FPEStrategy._encrypt`, `transforms/fpe.py:559-564`, at least logs a
warning before the same passthrough; the executed path does not.)

**1d. Non-invertible covering-hash fallback, despite FPE being sold as reversible.**
`transforms/fpe.py:166-177` (`_covering_hash_to_charset`) fires when a value has zero in-charset
characters (`_fpe_value` line 388-389). It is a per-position keyed one-way PRF, not a permutation
over the encoded integer, so `_feistel_inverse` (`:85-101`) cannot undo it. Yet `decoy_engine.unmask`
and `vault.py:1-9` present FPE as algebraically reversible ("`unmask` reverses fpe columns
algebraically (a keyed bijection inverts)"). A value that took the covering-hash branch cannot
round-trip. The review's probe captured this: `--- -> 297 -> 456` (encrypt then decrypt does not
return the source).

**Evidence anchor.** `transforms/fpe.py:9-25,55-82,166-177,366-452`;
`docs/what-we-cannot-prove.md:147-169`; and the passing leak demonstrations the review cites in
`tests/unit/disguises/test_pack_charset_no_leak.py`.

### Blast radius

FPE is the masking strategy for regulated identifiers across the disguise packs in
`src/decoy_engine/disguises/`: SSN, MRN, NPI, account numbers, device IDs, and VINs, keyed by
`charset: digits` or `charset: ALPHANUM` in `hipaa.yaml`, `sox.yaml`, `cpni.yaml`, `gdpr.yaml`,
`glba.yaml`, `ccpa.yaml`, `pci.yaml`, `pc.yaml`, and `ferpa.yaml`. `hipaa.yaml:245-259` already shows
a per-pack MRN workaround (widening `alphanum` to `ALPHANUM` so uppercase letters fall inside the
charset instead of leaking). That is a hand-patch of failure mode 1b on one field, not a systemic
fix, which is exactly why containment must be primitive-level.

---

## 2. Options

| Option | What it is | Pros | Cons |
|---|---|---|---|
| 1. Reviewed FF1 | Replace Feistel/HMAC with a reviewed FF1 (pyca AES) meeting SP 800-38G Rev.1 domain-size guidance | Approved construction; keeps format-preserving + reversible + keyed-deterministic API; no vault sidecar | Adds `cryptography` (AES) dependency; small domains fail Rev.1 minimum and still need a fallback |
| 2. Vault tokenization | Emit a random format-valid token, record source->token in the existing vault | Reversible by record, not by cipher; safe for any domain size; reuses `vault.py` | Requires vault file handling everywhere; equality/frequency still leak unless token is per-row |
| 3. Strict containment | Keep the current primitive but reject every disclosure path | Ships immediately, no new dependency, no primitive change | Still a custom cipher; not an approved construction; only safe once every out-of-domain path fails closed |

### Recommendation

**Ship Option 3 (containment) now as Phase-0, and adopt Option 1 (FF1) as the primary replacement,
with Option 2 (vault tokenization) as the mandatory fallback for domains below FF1's minimum size.**

Rationale: containment is reversible and flag-gated and closes the disclosure paths without a fork
decision, so it can land immediately. For the long-term primitive, FF1 preserves the exact
user-visible contract the product already sells (format-preserving, reversible without a sidecar,
keyed-deterministic), and it is a NIST-approved construction. But
[SP 800-38G Rev.1 second public draft](https://csrc.nist.gov/pubs/sp/800/38/g/r1/2pd) raises the
minimum domain size (`radix^minlen` must clear roughly one million), and several Decoy fields are
small domains (short digit tails, low-cardinality codes). FPE over a domain that small is unsafe
under any cipher, so those fields must route to vault tokenization instead. This is not
either/or: FF1 for admissible domains, vault for sub-minimum domains, containment guarding both
today.

---

## 3. Work split

### 3.1 ENGINE-BUILDABLE-NOW (Phase 0 containment, reversible, flag-gated)

| Item | What ships | Closes |
|---|---|---|
| C1. Fail-closed out-of-domain | Any value with a character outside the resolved charset raises a typed error before output, instead of retaining it (1b) or passing the whole value through (1c) | 1b, 1c |
| C2. Ban `preserve_separators=False` on sensitive fields | Reject the flag at compile time for any disguise/sensitive column; the whole-value no-op path becomes unreachable on those fields | 1c |
| C3. Retire the covering hash on the reversible path | On any column that participates in `unmask`/vault, a zero-in-charset value fails closed rather than taking the non-invertible covering hash (1d); document any retained whole-value fixed-point policy explicitly | 1d |
| C4. Typed formats | Define the input domain per field as a typed format (digits, VIN alphabet, etc.) so punctuation and data-bearing characters are declared, not inferred from charset membership | 1b |
| C5. Honest description | Remove "NIST", "NIST SP 800-38G", and FF1-modeled framing from the FPE docstring and product/evidence copy; describe it as a custom keyed permutation until a reviewed FF1 lands | 1a |

C1 through C5 are additive and reversible. C2 and C3 are gated so a caller can still opt into the
old behavior on an explicitly non-sensitive field during migration, but the disguise packs default
to fail-closed. None of these require choosing the long-term primitive.

### 3.2 CAM-GATED FORK

1. **Which primitive.** FF1 (adds the `cryptography` AES dependency) vs vault tokenization (adds
   vault-handling everywhere) vs indefinite containment of the custom Feistel. Recommendation in
   section 2. This is a dependency-footprint and product-shape decision for Cam.
2. **Small-domain policy.** For fields below FF1's Rev.1 minimum domain size, confirm the fallback
   is vault tokenization (recommended) versus accepting a documented residual risk. Per-field
   classification of which identifiers are sub-minimum is part of this decision.
3. **Rekey / version migration.** FPE keying is versioned by `SEED_PROTOCOL_VERSION` and
   `FPE_KEY_LABEL` (`_strategies/_fpe.py:49-51`). Replacing the primitive is a protocol-version
   bump: existing FPE outputs and vault-recorded mappings were produced by the old cipher and will
   not round-trip under the new one. Cam decides the migration shape (re-run against retained
   source, dual-version unmask window, or pre-GA hard delete and regenerate), coordinated with the
   DE-02 KeyProvider migration since both are `SEED_PROTOCOL_VERSION`-bound.

---

## 4. Verification

| # | Requirement | Assertion |
|---|---|---|
| V1 | Cross-implementation KATs | Chosen primitive matches published known-answer vectors (FF1 test vectors if Option 1; deterministic token vectors if Option 2) |
| V2 | Every admitted value round-trips exactly | For all admitted inputs, decrypt(encrypt(x)) == x byte-for-byte; no covering-hash branch on a reversible column |
| V3 | No out-of-domain admission | Any value with a character outside the typed domain fails before output; assert across full-frame, Polars, sequential, chunked, out-of-core routes |
| V4 | Every char participates | Instrumented/structural test proves every admitted character enters the permutation, not the separator-copy branch; per-position equality is not accepted as proof of passthrough |
| V5 | Fixed-point policy | Any whole-value fixed point is explicitly documented and asserted, not incidental |
| V6 | `preserve_separators=False` banned on sensitive fields | Compile-time rejection asserted for every disguise pack column |
| V7 | Per-disguise-rule coverage | Every disguise rule (SSN, MRN, NPI, account, device, VIN) tested through its actual pack config, not a synthetic charset |
| V8 | No NIST claim | Docstring/product/evidence copy contains no NIST/FF1-compliance claim until a reviewed FF1 ships |

V2, V3, V4 are the review's DE-01 verification line made concrete. V4 specifically counters the
review's note that per-position equality is not proof of passthrough.

---

## 5. Standards references

- [NIST SP 800-38G (FPE)](https://csrc.nist.gov/pubs/sp/800/38/g/upd1/final): the approved FPE
  methods (FF1, FF3-1). The current primitive is none of these.
- [NIST SP 800-38G Rev.1 second public draft](https://csrc.nist.gov/pubs/sp/800/38/g/r1/2pd):
  raises FF1 minimum domain-size requirements; motivates the vault-tokenization fallback for small
  domains.
- [OWASP Cryptographic Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html):
  do not roll your own cryptography; use approved constructions; fail closed.
</content>
</invoke>
