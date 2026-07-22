# Crypto / security GA release blockers: DE-01, DE-02, DE-03

**Date:** 2026-07-13
**For:** Cam (discussion, not a decision record)
**Status:** analysis + options. No production code changed by this document.
**Scope:** the three CRITICAL crypto/security findings that the 2026-07-12 adversarial
review flagged as GA blockers. This doc verifies each claim against the actual engine
code (with a REPL where a live behavior settles it), then lays out fix options and a lean.

Every claim below was re-derived from source. Where a prior design brief overstated or
mis-located a claim, this doc says so plainly. Line numbers are against the repo state at
branch point `origin/main` (2edbc7b).

---

## How to read the verification verdicts

- **Confirmed** = the code does what the finding says, and I reproduced the consequence.
- **Confirmed (narrowed)** = the underlying defect is real and reproduced, but the finding's
  strongest wording describes a variant that the code no longer has. The narrower live defect
  still blocks GA.
- **Refuted** = the code does not do what the finding says.

Summary table:

| Blocker | One-line claim | Verdict |
|---|---|---|
| DE-01 | Hand-rolled FPE, not FF1, small-domain-unsafe, non-invertible path | Confirmed (narrowed) |
| DE-02 | Live mask key derives from the 8-byte reproducibility seed, not a 32-byte master key | Confirmed |
| DE-03 | Undeclared source columns pass through as raw PII, silently | Confirmed |

---

# DE-01: Format-preserving encryption is a hand-rolled cipher

## 1. What it is

Decoy sells `fpe` as reversible, format-preserving encryption and uses it for the
highest-sensitivity identifiers (SSN, MRN, NPI, account numbers, PANs, VINs) across the
disguise packs. The shipped primitive is not a NIST-approved construction. It is a custom
8-round type-II Feistel network whose round function is HMAC-SHA256. The concern has three
independent parts: it is a home-grown cipher (against the repo's own "do not roll our own"
rule), it is unsafe on small domains, and it has value classes that do not round-trip even
though the product presents FPE as algebraically reversible.

## 2. Evidence

**It is a hand-rolled cipher, and the code says so.** `transforms/fpe.py:9-25` (module
docstring) and `:55-82` define `_ROUNDS = 8`, the HMAC-SHA256 round function `_prf`, and the
Feistel permutation `_feistel`. The docstring is explicit:

```
Design note: this is not NIST FF1 (which requires AES-CBC and therefore
the `cryptography` package). ... Defer a hard AES dep until a customer asks
for NIST SP 800-38G compliance by name.
```

Meanwhile the reversal docstring and the `derive`-keying comments describe the scheme as
"the NIST SP 800-38G FF1 key model" (`unmask.py:14`, `execution/_strategies/_fpe.py:6-12`).
So the product-facing framing invokes NIST while the primitive docstring disclaims it. Both
are true statements about different layers; together they are the honesty problem the finding
names. **Verdict on "not FF1": confirmed.** It is not FF1, FF3-1, or any SP 800-38G method.

**Small-domain weakness.** The Feistel splits an `n`-character string over charset size `r`
into halves and runs 8 rounds. Two structural facts make small inputs weak:

- The single-character case (`transforms/fpe.py:186-189`) is not a Feistel at all. It is a
  keyed alphabet rotation: `charset[(idx + shift) % len(charset)]` where `shift` depends only
  on `(key, tweak)`, not on the input. That is a Caesar cipher over one column. One known
  plaintext/ciphertext pair recovers the shift and decrypts every other single-char value
  under the same `(key, tweak)`.
- Two-character digit strings live in a domain of 100 values; 8 Feistel rounds over a domain
  that small have known statistical distinguishers, and NIST SP 800-38G Rev.1 raises the
  minimum admissible domain (`radix^minlen` on the order of a million) precisely because small
  domains are unsafe under *any* FPE cipher, FF1 included.

`_ROUNDS = 8` is also fewer than FF1's 10 rounds. **Verdict on "small-domain-unsafe":
confirmed by construction.**

**The non-invertible path (this is where the finding's wording needs narrowing).** The task
brief and the DE-01 design brief phrase this as "the last-position keyed one-way PRF is
non-invertible so `unmask` cannot round-trip." That exact shape (permute all `n` chars, then
overwrite the last with a check digit, discarding one encrypted character) was the pre-WS1
bug. It has already been fixed: the Luhn path now permutes the body `s[:-1]` and *appends* the
check digit (`transforms/fpe.py:360-362`), and the fix is recorded as the
`SEED_PROTOCOL_VERSION 4 -> 5` bump (`determinism/_derive.py:82-89`). So the literal
"last-position overwrite" defect is **not** in the current code.

What *is* still non-invertible on the live path is the covering-hash fallback. When a value
has zero in-charset characters, `_fpe_value` (`transforms/fpe.py:388-389`) routes to
`_covering_hash_to_charset` (`:166-177`), a per-position one-way HMAC PRF, not a Feistel
permutation. `_feistel_inverse` cannot undo it. I reproduced this directly:

```
$ .venv/bin/python  (fpe_encrypt_value then fpe_decrypt_value, charset "digits", key=0x00*32)
 plain digits '123456789' -> enc '620799045' -> dec '123456789'   round_trip=True
 mixed        '123-45-6789'-> enc '620-79-9045'-> dec '123-45-6789' round_trip=True   (dashes kept in clear)
 covering     '---'        -> enc '092'        -> dec '858'         round_trip=False
 covering     'N/A'        -> enc '418'        -> dec '652'         round_trip=False
 luhn source  '1234567890123456' (not Luhn-valid) -> ... -> '...3452' round_trip=False
```

So: the main in-charset path round-trips exactly. The covering-hash path (all-out-of-charset
values) does not, and neither does a `validate_luhn` column whose source was not already
Luhn-valid (documented caveat, but still a silent non-round-trip for the operator who does not
read it). **Verdict on "non-invertible so unmask cannot round-trip": confirmed but narrowed.**
It is not the whole cipher and not the last-position overwrite; it is the covering-hash
fallback plus the checksum/Luhn caveat. The consequence the finding cares about (a column sold
as reversible that silently does not reverse for some values) is real and reproduced.

**Two adjacent disclosure paths, both code-true.** These are part of the same DE-01 surface:

- Out-of-charset characters are retained in the clear (`_fpe_value:386-402`,
  `preserve_separators=True`): only in-charset positions are permuted, the rest are copied
  verbatim. Under `charset: digits`, `STATUS-1` masks to `STATUS-6`. I reproduced the leak of
  the `STATUS-` prefix above.
- Whole-value no-op under `preserve_separators=False` (`_fpe_value:403-404`): if any single
  character is out of charset, the entire value is returned unchanged. The live V2 handler
  (`_strategies/_fpe.py:110-116`) calls `fpe_encrypt_value` directly and hits this branch with
  no warning. The legacy `FPEStrategy._encrypt` (`transforms/fpe.py:559-564`) at least logs a
  warning; the executed path does not.

## 3. Why it is a GA blocker

FPE is the masking strategy for regulated identifiers across essentially every disguise pack
(HIPAA, PCI, GLBA, SOX, GDPR, CCPA, CPNI, FERPA). Three concrete consequences:

- **Reversibility is a product promise that some values silently break.** A masked output
  represented as "reversible with the config" contains values (covering-hash, non-Luhn) that
  will never recover. An operator who round-trips a sample and sees it work has not proven the
  tail is safe.
- **Partial-plaintext disclosure.** Any punctuation, letters, or data-bearing characters
  outside the charset survive in the clear. A short digit tail plus surviving context can be
  enough to re-identify.
- **Custom cipher risk.** A hand-rolled small-domain cipher is exactly what standards bodies
  and auditors tell you not to ship. For a privacy product whose entire value proposition is
  "the masked output is safe," a self-authored cipher is a due-diligence liability even before
  a concrete break.

## 4. Options to fix

- **Contain now (Phase 0, no fork).** Keep the current primitive but close every disclosure
  path: fail closed on any out-of-charset character on sensitive columns (kills the clear-text
  retention and the whole-value no-op), retire the covering hash on any column that
  participates in `unmask`/vault (fail closed instead of producing a non-invertible value),
  and strip the "NIST"/"FF1 model" framing from docstrings and evidence copy until a real FF1
  ships. Reversible, additive, no dependency, no product decision. Downside: still a custom
  cipher; only makes the current one honest and non-leaking, not approved.
- **FF1 primary (real NIST).** Replace the Feistel/HMAC core with a reviewed FF1 (or FF3-1)
  over AES per NIST SP 800-38G, adding the `cryptography` dependency. Keeps the exact
  user-visible contract (format-preserving, reversible without a sidecar, keyed-deterministic)
  and is an approved construction with published known-answer test vectors. Downside: adds an
  AES dependency, and SP 800-38G Rev.1's raised minimum domain size means small-domain fields
  are still inadmissible and need a fallback.
- **Tokenization / vault fallback.** For domains below FF1's minimum size (short digit tails,
  low-cardinality codes), emit a random format-valid token and record `source -> token` in the
  existing encrypted vault (`vault.py`). Reversible by record, safe at any domain size. This is
  not either/or with FF1: FF1 for admissible domains, vault tokens for sub-minimum domains,
  containment guarding both today.

## 5. Recommendation

Ship containment now, adopt FF1 as the primary primitive, with vault tokenization as the
mandatory fallback for sub-minimum domains. Containment is the only piece that can land without
a fork decision and it removes the live disclosure and non-round-trip paths immediately. FF1 is
the right long-term primitive because it preserves the contract the product already sells and it
satisfies the engine's own core rule (CLAUDE.md: "For crypto ... survey how established tools or
standards approach the problem ... we do not roll our own"). A hand-authored small-domain cipher
is the textbook violation of that rule; FF1 + vault is the standards-aligned answer.

## 6. Open questions for Cam

- **Primitive choice:** FF1 (accept the `cryptography`/AES dependency) vs indefinite
  containment of the custom Feistel vs vault-tokenization-everywhere? Lean is FF1 + vault.
- **Small-domain policy:** for fields below FF1's Rev.1 minimum, confirm vault tokenization as
  the fallback vs accepting a documented residual risk. This needs a per-field classification of
  which identifiers are sub-minimum.
- **Fixed-point / covering-hash policy:** on a reversible column, should an all-out-of-charset
  value fail closed (recommended) or keep a documented, asserted fixed-point behavior?
- **Migration:** replacing the primitive is a `SEED_PROTOCOL_VERSION` bump. Pre-GA hard-delete
  and regenerate is the cheapest path (no manifests exist in the wild). Confirm that vs a
  dual-version unmask window.

---

# DE-02: The live mask key is derived from the reproducibility seed, not a master key

## 1. What it is

The value that keys FPE, hash, unmask, and vault encryption on the executed path is the same
integer `global_settings.seed` that exists for run reproducibility. It is capped at 64 bits,
defaults to 0 when absent, and in practice operators pick small human values. A documented
32-byte `DECOY_MASTER_KEY` resolver exists, but it is not wired into the live V2 mask path. So
the re-identification surface (masked outputs and vaults) is protected by at most 64 bits of
operator-chosen entropy, not by a real key.

## 2. Evidence

**The live FPE key is `derive(job_seed, ...)`.** `execution/_strategies/_fpe.py:111`:

```python
key = derive(ctx.job_seed, namespace, FPE_KEY_LABEL)
```

**`ctx.job_seed` is 8 bytes and is the sole entropy input.** `StrategyContext` documents it
directly (`execution/_adapter.py:86-89`): "`job_seed` (8 bytes) is the sole entropy input
deterministic strategies feed into `derive` / `derive_index` / `PoolSampler.sample`."

**Those 8 bytes are the reproducibility seed.** `plan/_seed.py:106-108`,
`_normalize_job_seed`, is literally `_normalize_job_seed_int(config).to_bytes(8, "big")`, and
`_normalize_job_seed_int` reads `global_settings.seed`, defaulting to `0` when absent
(`:52-57`) and requiring only `0 <= seed_int < 2**64` (`:97-101`). No high-entropy secret
enters anywhere on this path.

**Every keyed purpose reuses the same 8-byte seed.** FPE: `_strategies/_fpe.py:111`. Vault:
`vault.py:126`, `derive(job_seed, VAULT_NAMESPACE, VAULT_KEY_LABEL)` handed straight to
`Fernet`. Unmask keys on the same `job_seed` by construction (`unmask.py:295`). HKDF's per-label
domain separation (`FPE_KEY_LABEL`, `VAULT_KEY_LABEL`) separates *purposes* but not *strength*:
every purpose inherits the same <=64-bit input.

**The documented 32-byte master key is dark code on the executed path.** The
`DECOY_MASTER_KEY` / `make_key_resolver` design (`docs/security/key-derivation.md`) is consumed
only by the legacy V1 `FPEStrategy._column_key` (`transforms/fpe.py:581-609`) via
`self.derive_key("mask")`. The V2 execution handlers do not call that resolver. `context.py`
carries a `derive_key` closure built from a master key via HKDF (`context.py:375-391`), but the
live strategy handlers key on `ctx.job_seed`, not on that resolver.

**Verdict: confirmed.** The live mask/unmask/vault key material is derived from the 8-byte
reproducibility seed. The 32-byte master key described in the docs is not on the executed path.

**Why HKDF does not save it.** Per RFC 5869 sections 3.1 and 4, HKDF is extract-then-expand: it
concentrates and diffuses existing keying material, it does not manufacture entropy. A 64-bit
(or 0-bit, when the seed defaults) input yields at most 64 bits of unpredictability regardless
of expansion. Deriving `decoy/fpe/...` from `42` does not make `42` secret.

## 3. Why it is a GA blocker

- **The masked output is offline-attackable.** The config (which carries the seed) plus any
  masked output, or a single known source->masked mapping, lets an attacker brute-force a small
  seed and then decrypt every FPE column and open every vault. A 64-bit ceiling is already weak
  for long-lived confidentiality; a human-chosen seed like `42` is trivially searchable, and the
  absent-seed default of `0` is worse.
- **No fail-closed step.** A keyed masking job runs happily under a null or trivially guessable
  secret. Nothing notices.
- **Purpose reuse.** FPE, vault, and unmask all inherit the same weak input, so one recovered
  seed unlocks the entire re-identification surface at once.

## 4. Options to fix

- **`KeyProvider` boundary (recommended, engine-buildable now).** Introduce an explicit key
  boundary at the execution edge. The engine accepts opaque key bytes (>= 32 bytes) plus a
  non-secret `key_id` / `key_version`, and derives versioned purpose keys via HKDF-SHA256 with
  a structured `info = "decoy/<purpose>/v2/<namespace>/<key-version>"` (RFC 5869 domain
  separation). The public `seed` stays, demoted to generation-only reproducibility and
  documented as non-secret. Keyed strategies (hash, FPE, unmask, vault) draw only from the
  provider. Tradeoff: it is a real boundary change touching every keyed handler, gated behind a
  `secure_keys` mode so existing seed-only jobs keep running until the default flips.
- **Fail-closed gate (ships with the above, or standalone).** A pre-execution check: if the
  compiled plan contains any keyed strategy and no usable provider (or a secret shorter than 32
  bytes) is present, raise a typed error before any table, quarantine, vault, or manifest is
  written. New errors: `KeyedStrategyRequiresSecret`, `MissingMaskSecret`, `WeakMaskSecret`.
  This is the single highest-value, lowest-cost piece.
- **Versioned purpose keys.** Bake `key_version` into the `info` string so rotating the version
  deterministically re-keys without touching derivation code, and record `key_id` / `key_version`
  (never the secret) in the evidence manifest for audit.

## 5. Recommendation

Adopt the `KeyProvider` boundary with a fail-closed gate and versioned purpose keys, and demote
`global_settings.seed` to generation-only. This is the standards-aligned shape: opaque >= 32-byte
secret injected from a managed store (RFC 5869 for derivation, NIST SP 800-57 Part 1 Rev.5 for
the 256-bit strength target, OWASP Key Management for "never derive secrets from low-entropy
config"). It is squarely inside the engine's core rule to use HKDF and established key management
rather than improvising. The engine only ever accepts bytes; where those bytes come from
(platform KMS/HSM, per-tenant derived key, env-var/secret-file for self-hosted) stays a host
decision, so this does not force a platform-architecture choice to land the boundary.

## 6. Open questions for Cam

- **Secret source:** platform KMS/HSM envelope vs per-tenant derived key vs env-var/secret-file
  for self-hosted vs generated-and-stored. Engine only takes bytes; this is a platform/tenancy
  decision, not an engine one.
- **Vault migration:** every vault written to date is keyed on the seed-derived key. Options:
  (a) dual-read window (try new purpose key, then legacy seed key) during migration; (b) an
  explicit `rekey-vault` tool; (c) declare pre-GA vaults disposable and regenerate (pre-GA =
  hard delete). Masked *outputs* cannot be rekeyed in place without re-running against retained
  source, which is its own call.
- **When does seed-only become a hard reject?** Recommendation: flip the default to fail-closed
  before the GA corpus freeze, so no GA artifact is ever produced under a seed-only key. Confirm
  the date and whether GA hard-blocks seed-only keyed masking entirely.
- **Sequencing with DE-01:** both DE-01 and DE-02 are `SEED_PROTOCOL_VERSION`-bound. Do the
  rekey and the primitive swap in one coordinated pre-GA bump.

---

# DE-03: Undeclared source columns pass through as raw PII, silently

## 1. What it is

If a source table has a column that the pipeline config does not declare (no strategy), that
column is copied to the output verbatim. If it holds PII, the output holds raw PII. There is no
warning. The safe default for a privacy tool would be the opposite: a column the operator did
not account for should not silently leave the building in the clear.

## 2. Evidence

The work list is built only from columns that have a strategy. `_build_seed_envelope`
(`plan/_seed_envelope.py:99`) skips any column with no strategy: `if not col_name or not
strategy: continue`. `build_work_list` (`execution/_runner.py:55-` and its docstring `:5-9`)
enumerates maskable units from `plan.seed_envelope.per_table` only. An undeclared column
therefore never becomes a work node, and the adapter carries the original source column through
to the output unchanged.

I reproduced this end-to-end with a validated config (one declared `fpe` column `acct`, one
undeclared column `ssn`):

```
        acct          ssn
0  608514531  111-22-3333
1  966987072  444-55-6666
2  016965783  777-88-9999
---
acct masked: True
ssn present: True | ssn raw-equal (output == source): True
run warnings: ()
```

`acct` was masked (harness is valid); `ssn` came out byte-identical to the source, present in
the output schema, with an **empty** `res.warnings`. The unmask path documents the same
convention on its own side ("(no strategy) -> untouched ... passed through unchanged",
`unmask.py:21` and `:314-323`), which confirms passthrough is the intended model, not an
accident of one adapter.

**Verdict: confirmed.** An undeclared source column is emitted unchanged, and no warning is
raised.

## 3. Why it is a GA blocker

- **Raw identifier leak by omission.** The failure mode is a human forgetting to declare a
  column, or a source schema gaining a column between runs. Either way the output ships raw PII,
  and the operator gets no signal. For a masking product, "the column you forgot leaves in the
  clear, silently" is the worst possible default.
- **It defeats the product's core guarantee.** Customers reasonably assume the output of a
  masking run is safe to hand out. Passthrough-by-default means "safe" is contingent on the
  operator having enumerated every PII column perfectly, with no backstop.

## 4. Options to fix

- **Compile an exact output schema; default unknown column -> error (recommended).** At compile
  time, compute the set of output columns from the declared columns plus generated columns. Any
  source column not in that set is a hard error (`undeclared_column`) unless the operator
  explicitly opts it into a passthrough allowlist. Because the engine is pre-GA
  (`is_pre_ga()` is `True`), this is a hard-delete change: no compatibility window needed, just
  flip the default to safe.
- **Warn-and-passthrough (weaker).** Keep passthrough but emit a loud `QualityWarning` /
  manifest entry for every undeclared column that reaches output. Cheaper, but a warning on a
  privacy leak is not fail-closed, and warnings get ignored.
- **Explicit passthrough opt-in.** Require columns intended to pass through unchanged to be
  declared with an explicit `strategy: passthrough` (which already exists as a scalar
  transform). Anything not declared at all becomes an error. This makes "I meant to keep this
  column raw" an on-the-record decision.

## 5. Recommendation

Default unknown-column -> error, with an explicit `passthrough` opt-in for the columns an
operator genuinely wants carried in the clear. Pre-GA is the cheapest possible moment to make
this switch: no manifests in the wild, no compatibility contract binding yet, so it is a clean
hard-delete of the unsafe default (per CLAUDE.md "Pre-GA = hard delete"). A privacy engine
should fail closed on data it was not told how to handle; a warning is not enough for a raw-PII
leak.

## 6. Open questions for Cam

- **Error vs warn:** hard error on undeclared columns (recommended) vs loud warning + passthrough?
- **Opt-in shape:** require explicit `strategy: passthrough`, or a table-level `allow_passthrough`
  list, or both?
- **Scope:** does the hard-fail apply to all tables, or only tables/columns matched by a disguise
  pack (where PII expectation is highest)? Recommendation: all tables, since the whole point is to
  catch the column nobody classified.

---

# Cross-cutting: these are GA blockers, and pre-GA is the cheapest time to fix

All three are release blockers for the same reason: they undermine the guarantee the product
exists to make (the masked output is safe and, where promised, reversible). And all three are
cheapest to fix right now:

- `decoy_engine.release.RELEASE_PHASE` is `"pre-ga"` and `is_pre_ga()` returns `True`
  (verified). The compatibility contract is not binding until launch flips this to `"ga"`
  (CLAUDE.md, `release.py`).
- There are no manifests or vaults in the wild (the `SEED_PROTOCOL_VERSION` history in
  `determinism/_derive.py` repeatedly notes "pre-GA, hard cutover; no manifests in the wild").
- DE-01 and DE-02 are both `SEED_PROTOCOL_VERSION`-bound, so the primitive swap and the key
  rekey want to ride one coordinated pre-GA bump rather than two migrations later.

**Fix order.** The single highest-value, lowest-cost item across all three is the DE-02
fail-closed gate (a keyed strategy with no real secret should refuse to run). DE-03's
default-to-error is the next cheapest and closes a raw-leak-by-omission with a compile-time
check. DE-01 containment (fail-closed on out-of-charset, retire the covering hash on reversible
columns, drop the NIST framing) can land in parallel; the FF1 + vault primitive swap is the
larger, Cam-gated piece and should be sequenced with the DE-02 rekey under one protocol bump.

**Sequencing vs the TB-6 / 50M spend.** The 2026-07-12 adversarial review recommended holding
the TB-6 GCP benchmark spend behind DE-01/02/03. That coupling is not strictly necessary. The
50M run validates the memory model and the out-of-core routing path: peak-RSS behavior, chunking,
and the full-frame vs out-of-core decision. None of that touches the crypto primitive, the key
source, or undeclared-column handling. So Cam's decision to run 50M now is compatible with
holding GA behind the crypto fixes: the benchmark answers a memory/routing question, and the
crypto blockers answer a privacy-correctness question. They are independent workstreams. The
only real coupling is calendar and attention, not a technical dependency.
