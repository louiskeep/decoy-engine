# DE-01 Option C: audited NIST FF1 + vault-token fallback — design & analysis

**Date:** 2026-07-14
**Status:** decision-ready design brief. NO source under `src/` changed by this document.
**For:** Cam (decision), with a build guide for whoever implements the fast-follow.
**Baseline:** `origin/main` @ `06bacb9` (DE-11 + DE-03 + DE-01 fail-closed + DE-02 KeyProvider all merged).
**Companions:**
- `docs/discussions/2026-07-13-de01-fpe-options.md` (Options A/B/C/D; this is C)
- `docs/discussions/2026-07-13-crypto-ga-blockers.md` (DE-01/02/03 verification)
- `docs/discussions/2026-07-14-de01-vault-token-for-fpe.md` (the vault-token sub-design; extended here)
- `docs/security/de-02-keyprovider-design.md` (the `mask_key` seam this rides)

---

## TL;DR

- **The premise partly fails, and that is the headline finding.** There is **no audited,
  maintained, license-compatible, arbitrary-alphabet FF1 library in pure Python.** The one
  audited/maintained Python option (`mysto/ff3`) implements **FF3-1 only** — and NIST is
  **withdrawing FF3/FF3-1** (2nd public draft of SP 800-38G Rev.1, 2026-02-03; Beyne 2021
  attack). So FF3-1 is the wrong horse: it does not make the "NIST FF1" claim true and it is
  being removed from the standard.
- **FF1 is the surviving NIST method** but has **no clean pure-Python implementation** (patent
  history + `mysto`/PyCryptodome explicitly declining FF1). Real FF1 in Python today means one of:
  **(a)** bind the Rust `fpe` crate (str4d) — genuine third-party FF1, RustCrypto AES, dual
  MIT/Apache-2.0, maintained; cost = a compiled extension in the wheel; or **(b)** vendor a
  **NIST-KAT-locked FF1** over the AES already in our `cryptography` dep — zero new runtime dep,
  license-clean, but it is "our code" (mitigated by locking to NIST's published known-answer
  test vectors).
- **Recommendation:** ship **FF1 (not FF3-1)**, sourced from **the Rust `fpe` crate** if a native
  build is acceptable, else **vendored-KAT-locked FF1 over `cryptography`'s AES**. Below the FF1
  minimum domain (`radix^len < 1,000,000`), **vault-token** the value into the DE-02
  secret-keyed vault. **Replace** the home-rolled Feistel entirely (do not run it alongside), under
  a **`SEED_PROTOCOL_VERSION` 6 → 7 bump**, with a **full golden re-baseline of all five test-flight
  jobs** (every one uses `fpe`).

---

## 0. Where the code stands after DE-01/DE-02 (the seam FF1 plugs into)

Two merged pieces define the integration surface; FF1 must not re-open either.

**DE-01 (fail-closed FPE, `#69`).** The home-rolled 8-round HMAC-Feistel is unchanged as a
primitive, but every silent-failure path now **fails closed**:
- all-out-of-charset value → `FpeUnencryptableError` (was a non-invertible covering hash);
- `preserve_separators=false` + any out-of-charset char → `FpeUnencryptableError` (was a silent
  cleartext no-op);
- too-short checksum value → `FpeChecksumError` (was silent passthrough).
Two residual axes are surfaced as structured `QualityWarning`s, not closed:
`fpe_sub_minimum_domain` and `fpe_partial_plaintext_disclosure`
(`execution/_strategies/_fpe.py::_residual_risk_warnings`). The handler already computes
`_FF1_MIN_DOMAIN = 1_000_000` and `_min_domain_length(radix)` — the exact boundary Option C acts on.

**DE-02 (KeyProvider, `#70`) — the key seam.** It shipped as a **one-slot IKM substitution**, NOT
the per-purpose `derive_purpose_key(provider, purpose, ns)` sketched in the older design doc. In
the shipped code:
- `StrategyContext.mask_key` (bytes) replaces `job_seed` as the keyed-mask IKM.
  `mask_key == job_seed` (8 bytes) when no secret; `HKDF-SHA256(secret)` (32 bytes) with a secret.
- The FPE handler derives its cipher key as **`key = derive(ctx.mask_key, namespace, FPE_KEY_LABEL)`**
  (`_strategies/_fpe.py:135`), where `derive(seed, namespace, source) -> 32 bytes`
  (`determinism/_derive.py:216`, mixes `SEED_PROTOCOL_VERSION`).
- The gate (`keyprovider.require_mask_key` / `plan_has_keyed_strategy`) already lists `"fpe"` in
  `_ALWAYS_KEYED_STRATEGIES`, and a `vault: true` column is **always** keyed. So **as long as FF1
  stays the `fpe` strategy, it inherits DE-02's fail-closed gate and `reversed_unverified` unmask
  caveat for free** — no gate change.
- The vault already keys off `mask_key` too: `vault._fernet(mask_key)` →
  `derive(mask_key, VAULT_NAMESPACE, VAULT_KEY_LABEL)`, with `assert_vault_writer_keyed` fail-closed
  (`vault.py:119,134,207`). The vault-token tail rides this unchanged.

**Consequence for FF1:** the FF1 **AES key** = `derive(ctx.mask_key, namespace, FF1_KEY_LABEL)`
(32 bytes → AES-256), and the FF1 **tweak** = the existing per-column tweak bytes
(`join_group or column`). This preserves every DE-02 property (secret-keyed, per-namespace
separation, per-column tweak, join-group linkage, seed fallback pre-GA) with a one-line change to
what `derive`'s output feeds.

---

## 1. Library survey + recommendation

### 1.1 The NIST status change that decides FF1-vs-FF3-1

NIST's **2nd public draft of SP 800-38G Rev.1 (2026-02-03)** **removes FF3 and FF3-1** — the
Beyne (2021) attack broke both — and specifies **FF1 as the sole method**, with an **increased
minimum domain size**. This is dispositive:

- **Building the "NIST FPE" claim on FF3-1 makes it false-on-arrival.** The algorithm is being
  struck from the standard. Any product/compliance copy citing NIST must target **FF1**.
- The engine's own residual-risk copy already says "await the structured-FPE/**FF1** fast-follow" —
  consistent with FF1, not FF3-1.

### 1.2 The candidates

| Library | Algo | AES backend | License | Radix / alphabet | Tweak | Min-domain | Maint. / audit | Verdict |
|---|---|---|---|---|---|---|---|---|
| **`mysto/ff3`** (PyPI `ff3`) | **FF3-1 only** | PyCryptodome (AES-ECB round fn) | Apache-2.0 | custom alphabet ≤256, radix 10/36/62… | 7-byte (FF3-1) | `radix^minlen ≥ 1e6` (`DOMAIN_MIN`) | v1.0.2 (2024-03), passes NIST + ACVP KATs, ~103★ | **Reject for the claim**: FF3-1 is being withdrawn; not FF1. Viable only if Cam accepts "FF3-1" framing + withdrawal risk. |
| **`pyffx`** (emulbreh) | **FFX** (Bellare-Rogaway FFX[radix], *not* a NIST SP 800-38G method) | pure-Python | BSD-ish | radix via alphabet | tweak-ish | none | **Inactive** (no release >12mo), no audit | **Disqualified**: not NIST FF1, unaudited/inactive — repeats the exact home-rolled-cipher liability DE-01 removes. |
| **`PaddyKe/FFX`** | claims FF1 + FF3-1 | varies | unclear | draft | draft | draft | hobby/draft repo, not a maintained PyPI package | **Disqualified**: unaudited hobby impl; "audited" is the whole point. |
| **`pyFPE`** | FF3 (pre-3-1) | — | — | — | — | — | stale | **Disqualified**: FF3 (withdrawn), stale. |
| **PyCryptodome FF3-1** | FF3-1 | its own AES | BSD-2 | — | — | — | **PR #601 unmerged** | **Unavailable**: not in a release. And still FF3-1. |
| **Rust `fpe` crate** (str4d) | **FF1** (+ FF3-1) | RustCrypto `aes` | **MIT / Apache-2.0** | arbitrary radix (numeral string), custom alphabet | yes (byte tweak) | enforces FF1 domain | maintained (str4d = Zcash core dev); RustCrypto ecosystem | **Recommended primitive source** (via PyO3/maturin binding). Genuine third-party FF1. |
| **Vendored KAT-locked FF1** (ours, over `cryptography` AES) | **FF1** | **`cryptography` (already our `[vault]` dep, >=42)** | Apache-2.0/BSD (our code) | arbitrary radix (we already base-r encode) | yes | we set `radix^len ≥ threshold` | our code, but **locked to NIST KAT vectors** | **Recommended fallback**: zero new runtime dep, license-clean; caveat = "our code." |

### 1.3 Recommendation

**Ship FF1.** Source the primitive, in priority order:

1. **Rust `fpe` crate (str4d), bound via PyO3/maturin.** Best honors the engine's
   "we do not roll our own" rule: a real, maintained, third-party FF1 in the RustCrypto ecosystem,
   permissively licensed. **Cost:** the engine becomes not-pure-Python — a compiled extension + Rust
   toolchain in the wheel/CI matrix, plus a per-platform build. Supply-chain vetting extends to the
   crate and its RustCrypto transitive deps.
2. **Vendored NIST-KAT-locked FF1 over `cryptography`'s AES** (fallback if a native extension is
   unacceptable). FF1 is a fully specified algorithm with **published NIST known-answer test
   vectors**; a faithful implementation that (a) uses AES from the already-vetted `cryptography`
   package (>=42, currently the optional `[vault]` extra) and (b) is **locked in CI to the NIST
   KATs** is materially different from an ad-hoc Feistel and satisfies the rule's spirit ("survey
   how the standard approaches it, cite the source pattern"). **Zero new runtime dependency** if
   `cryptography` is promoted from `[vault]` to a base/`[fpe]` dep. Caveat to state honestly: it is
   still first-party code; the KAT lock is the audit anchor, not a third-party audit.

**Reject `mysto/ff3` / FF3-1** for the compliance claim (withdrawn algorithm, and not FF1). Reject
`pyffx`/`PaddyKe`/`pyFPE` outright (unaudited and/or not NIST FF1 — choosing them would re-create
the DE-01 liability).

**IP flag (Cam-gated, not resolved here):** FF1 (Bellare-Rogaway-Spies / Voltage, now OpenText)
has patent history; Voltage filed a Letter of Assurance with NIST. Using a permissively-licensed
FF1 crate/impl for a standardized method is the industry norm, but confirm there is no
encumbrance that conflicts with Decoy's Apache-2.0 posture before shipping. This is a legal check,
not an engineering one.

---

## 2. Integration design (how FF1 slots into the existing strategy)

The `fpe` strategy keeps its name, config surface, and unmask contract. Only the **inner
permutation** changes: `transforms/fpe.py::_permute` (and the `_fpe_pure_value` dispatch) swaps the
Feistel for an FF1 call. Everything wrapping it is preserved.

**Key + tweak (both from the DE-02 seam):**
```
ff1_key   = derive(ctx.mask_key, namespace, FF1_KEY_LABEL)   # 32 bytes -> AES-256
ff1_tweak = (fpe_join_group or column).encode()              # unchanged; public, need not be secret
ciphertext = FF1(radix=len(charset)).encrypt(ff1_key, ff1_tweak, numeral_string(in_charset_chars))
```
- `FF1_KEY_LABEL` is a **new** constant (e.g. `b"ff1-key/v1"`), distinct from `FPE_KEY_LABEL`, so
  FF1 keys are domain-separated from any legacy-Feistel key and the change is captured by the
  protocol-version bump (§4). Per-namespace separation and per-column tweak are exactly today's.
- FF1's tweak is a NIST first-class input (public, arbitrary-length in FF1, unlike FF3-1's fixed
  7 bytes). The existing `join_group or column` tweak maps directly — **and FF1's flexible tweak
  length removes FF3-1's 7-byte cap**, another reason FF1 fits better.

**Charset / radix mapping:** `_CHARSETS` already yields a charset string; `radix = len(charset)`
(digits=10, alpha/ALPHA=26, alphanum=36, ALPHANUM=62, or a custom string). The engine already
base-r encodes/decodes (`_encode`/`_decode`); FF1 libraries take exactly a numeral string over a
radix (Rust `fpe`: arbitrary radix; vendored: we own the encode). Per-column custom charsets are a
direct radix + alphabet pass-through. No `_CHARSETS` change.

**`preserve_separators`:** unchanged. The orchestration in `_fpe_value` already **extracts the
in-charset positions, encrypts them as one string, and reinserts** at their positions; FF1 replaces
only that middle encrypt. The DE-01 fail-closed guards (all-out-of-charset → error; length
invariant `len(body) == len(positions)` with `strict=True` zip) stay verbatim.

**Checksum schemes (Luhn / NPI / ISBN-13 / VIN / EAN-13 / GTIN):** compose unchanged. Each scheme
permutes a **body** then recomputes the check digit (`_fpe_checksum.py`); FF1 replaces the body
permutation, and the check-digit recompute on decrypt still holds (it is not stored). Note the
min-domain constraint (§3) now applies to the **body** radix^length, not the whole value — e.g. an
8-digit NPI body (10^8) and a 15-digit Luhn/PAN body (10^15) clear the floor; a short body would
route to vault-token or fail closed.

**Unmask:** symmetric. `unmask._decrypt_column` derives the same
`derive(mask_key, ns, FF1_KEY_LABEL)` + tweak and calls **FF1.decrypt**. FF1 is a bijection under
(key, tweak, radix), so round-trip is exact for admissible-domain values; the DE-02
`reversed` / `reversed_unverified` status and Luhn/checksum caveats are unchanged.

---

## 3. The minimum-domain split (the core of Option C)

FF1 is **undefined/insecure below the NIST minimum domain**: `radix^len ≥ 1,000,000` today
(and **increasing** in Rev.1 — keep this a named constant pinned to the spec revision the chosen
library implements, not a literal). A value whose in-charset domain is below the floor **cannot be
FF1'd** and must be **vault-tokened**.

**Per-value routing (deterministic, no config-time guess):**
```
for each value:
    L = count of in-charset chars ;  r = radix
    if r**L >= FF1_MIN_DOMAIN:  ff1_encrypt(...)                      # admissible
    else:                        vault_token(...)                      # sub-minimum tail
```
Both outputs are in-charset, same-length, and indistinguishable in the masked output; at unmask
they are distinguished because **the tokens are exactly the vault keys** (vault membership test),
per the mechanism already described in `2026-07-14-de01-vault-token-for-fpe.md`.

**Vault-token integration (extends the vault-token-for-fpe design):**
1. **Config field** `on_unencryptable: "error" | "vault_token"` (default `error`). Additive
   frozen-surface change → triggers the compatibility-contract §9 process (now non-vacuous: a real
   second value). Under `error`, sub-minimum values **fail closed** (today's behavior, promoted
   from warning to error for the FF1 path). Under `vault_token`, they route to the vault.
2. **Scoped carve-out to the fpe+vault prohibition.** `plan/_checks.py::check_vault_columns` today
   forbids `vault: true` on `strategy: fpe` (an FPE column is already reversible; a full-column
   vault is pure disclosure liability). The carve-out permits a vault write for an fpe column
   **only for the sub-minimum tail** (the exact values FF1 cannot cover), keyed on
   `on_unencryptable == "vault_token"`. The FF1'd majority is **not** vaulted.
3. **Per-value token via `StrategyContext`.** Thread an optional vault sink (path + namespace) into
   `StrategyContext` (it carries none today). On a sub-minimum value: **(a)** generate a
   **keyed-deterministic in-charset token** — reinstate the removed `_covering_hash_to_charset` as
   the generator (keyed on `derive(mask_key, ns, …)`, byte-stable across runs, which the
   determinism contract requires; a random token would break reproducibility), and **(b)** record
   `(namespace, token) -> source` into the vault. **Fail closed if no vault path/namespace** (a
   token with nowhere to record it is unrecoverable — the exact silent-data-loss class DE-01
   exists to prevent).
4. **Unmask recovery.** For a `vault_token` fpe column with a supplied vault: vault-lookup the
   tokened values via `(namespace, masked) -> source` and FF1-invert the rest.
5. **Composes with DE-02 fail-closed.** A vault-bearing plan is **always keyed**
   (`_col_seed_is_keyed` returns `True` on `vault: true`), so at GA it already requires a
   ≥32-byte secret; the vault Fernet keys off the same `mask_key`
   (`derive(mask_key, VAULT_NAMESPACE, VAULT_KEY_LABEL)`) with `assert_vault_writer_keyed`
   fail-closed. No new gate; the tail vault inherits it.

**Boundary caveat (checksum bodies):** apply the threshold to the **body** radix^len for
checksum schemes, and to the **in-charset** radix^len otherwise (matching the existing
`_residual_risk_warnings` `sub_minimum` computation). Single-character and 2–5 digit tails are the
common sub-minimum cases; they route to vault-token (or `error`).

---

## 4. Determinism / golden blast radius (the biggest decision)

**FF1 output ≠ home-rolled Feistel output for every value.** This is **not** a backward-compatible
determinism change — it is an intended, total re-baseline of the masked fingerprint of **every
`fpe` column**.

**All five test-flight golden jobs use `fpe` heavily** (verified against each `manifest.yaml`):

| Job | fpe columns (representative) | charsets / schemes exercised |
|---|---|---|
| **a_healthcare_claims** | member_id, ssn, npi, mrn, claim_id, line_id | digits, ALPHANUM; checksum `luhn`, `npi` |
| **b_retail_m2m** | customer_id, pan, product_id, order_id, cat_code, risk_flag | digits, ALPHANUM; checksum `luhn`; 2-char sub-minimum codes |
| **c_hr_selfref** | employee_id, manager_id (self-FK, same namespace) | alphanum, `preserve_separators` |
| **d_longitudinal_visits** | provider_id, patient_id | digits, `preserve_separators` (letter prefixes) |
| **e_hostile_edge_cases** | person_id, kana_name, account_id, singleton | digits, ALPHANUM; fail-closed edge shapes |

Every one re-records. Job **b**'s 2-char `cat_code`/`risk_flag` (radix 62, 62^2 ≈ 3,844 < 1e6) and
the letter-prefixed IDs in **b/c/d** are precisely the **sub-minimum / partial-prefix** values that
route to vault-token (or must be widened to a covering charset), so the re-baseline is coupled to
the §3 decision — you cannot re-bless the golden until the sub-minimum policy is fixed.

**No-secret path (answer to "does FF1 run under `mask_key == job_seed`?"):** **Yes — FF1 runs on
the no-secret pre-GA path too, and it must.** FF1's AES-256 key is `derive(ctx.mask_key, …)`, which
returns 32 bytes regardless of whether `mask_key` is the 8-byte seed or a 32-byte secret root. So
FF1 is **not** gated to secret-present; it replaces the primitive unconditionally, and the golden
gate (which runs under the `SeedKeyProvider` fallback) exercises real FF1. A secret only strengthens
the key material; it does not change *whether* FF1 runs. The DE-02 `reversed_unverified` unmask
caveat continues to apply under the seed fallback.

**Version bump + migration:**
- **`SEED_PROTOCOL_VERSION` 6 → 7** (same shape as the WS1 `4 → 5` Feistel-keying bump). `derive`
  mixes the version into all key material, so v6 vaults become undecryptable under v7 — the vault
  header stamps the version and load fails with `vault_protocol_version_mismatch` (clean, not
  opaque).
- **Pre-GA = hard delete.** `is_pre_ga()` is `True`; there are no manifests/vaults/outputs in the
  wild under a compatibility guarantee (the `SEED_PROTOCOL_VERSION` history repeatedly notes this).
  So: **regenerate**, don't dual-version. Re-bless the golden via `test_flight.py --bless`
  (`write_golden`).
- **Coordinate with DE-02's rekey under one bump** if DE-02 has not already consumed the 6 → 7
  step (both are protocol-version-bound; do the primitive swap and any remaining key changes in one
  coordinated pre-GA cutover, per the crypto-ga-blockers guidance).
- **Also folds in** deferred item 3 of the vault-token doc (pre-fix covering-hash artifacts are not
  reliably reversible) — a full regenerate under v7 moots it.

---

## 5. Decisions for Cam

1. **(Library)** Adopt **FF1 via the Rust `fpe` crate (str4d)** as the primitive source, OR the
   **vendored NIST-KAT-locked FF1 over `cryptography`'s AES** if a compiled extension is
   unacceptable. **Do not** use `mysto/ff3` (FF3-1), `pyffx`, `PaddyKe`, or `pyFPE`. *Critical
   finding: no audited maintained pure-Python FF1 exists — this is a real constraint, and the
   choice is genuinely between "add a native dep" and "vendor a KAT-locked impl," not "pip install
   an audited FF1."*
2. **(FF1 vs FF3-1)** **FF1**, decisively. NIST is **withdrawing FF3/FF3-1** (SP 800-38G Rev.1 2nd
   draft, 2026-02; Beyne 2021 attack); FF3-1 would make the "NIST" claim false and rest on a
   removed algorithm. FF1 also gives arbitrary-length tweaks (vs FF3-1's fixed 7 bytes), which fits
   the existing per-column tweak cleanly.
3. **(Domain threshold)** Route per value: **`radix^len ≥ FF1_MIN_DOMAIN` → FF1, else → vault-token**
   (applied to the checksum *body* for checksum schemes). Keep `FF1_MIN_DOMAIN` a **named constant
   pinned to the spec revision** the chosen library implements (currently 1,000,000; **rising** in
   Rev.1 — re-pin when the library tracks it).
4. **(Replace vs alongside)** **Replace the home-rolled Feistel entirely.** Keeping it alongside
   keeps the exact liability DE-01/Option-C exist to remove and doubles the crypto surface. FF1
   (admissible) + vault-token (sub-minimum) is full coverage. Gate the swap behind the protocol
   bump, **not** a runtime flag.
5. **(Golden / version bump)** Full re-baseline of **all five** test-flight jobs;
   **`SEED_PROTOCOL_VERSION` 6 → 7**; **pre-GA hard-delete + regenerate** vaults/manifests;
   coordinate with the DE-02 rekey under one bump; FF1 runs under the no-secret seed fallback so the
   golden gate exercises it. The re-bless is **blocked on the §3 sub-minimum policy** (jobs b/c/d
   carry sub-minimum and letter-prefixed values).
6. **(Supply-chain / IP)** Two items to clear before merge: **(a)** FF1 **patent** history
   (Voltage/OpenText LoA to NIST) — confirm no encumbrance vs Decoy's Apache-2.0; **(b)** the
   **dependency**: the Rust crate adds a compiled extension + RustCrypto transitive deps to vet and
   a per-platform wheel build, whereas the vendored route adds **zero new runtime dependency** by
   reusing `cryptography` (already the `[vault]` dep — promote it to base or a new `[fpe]` extra).
   The vendored route is the lower-supply-chain-risk option; the crate is the stronger
   "don't-roll-our-own" option. This trade is the crux of decision 1.

---

## 6. Residual items NOT closed by Option C (state, don't hide)

- **Partial-plaintext prefix leak** (`M000001` keeps `M` in the clear under
  `preserve_separators=true`) is only fully closed by a **covering charset** (per-column config) or
  a **structured/typed-subfield FPE** that encrypts the prefix in its own alphabet (or vault-tokens
  it). Option C enables the vault-token half; the structured-subfield half is a further follow-up.
  Cam already chose (2026-07-14) to ship the dangerous all-out-of-charset fix and defer this.
- **FF1 does not authenticate.** A wrong key yields plausible-but-wrong plaintext; the DE-02
  `reversed_unverified` caveat under the seed fallback is the honest signal. An authenticated
  artifact envelope (subsuming the pre-fix covering-hash reversibility gap, item 3 of the
  vault-token doc) remains a separate, larger build — out of Option C scope.
