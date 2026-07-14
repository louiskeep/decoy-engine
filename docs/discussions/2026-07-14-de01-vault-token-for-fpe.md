# DE-01 fast-follow: vault_token for FPE + the partial-prefix limitation

**Date:** 2026-07-14
**Status:** deferred design record (NOT built this sprint). Captures the
`on_unencryptable: vault_token` design worked out during DE-01 cluster-C, and
the partial-plaintext (format-prefix) limitation that DE-01 deliberately left
open, so both are on record for their own sprint.
**Companion to:** `docs/discussions/2026-07-13-de01-fpe-options.md`,
`docs/plans/2026-07-13-crypto-de11-delivery-plan.md` (Sprint 3).

---

## What shipped in DE-01 cluster-C (for context)

Every un-encryptable value now **fails closed** (`error`): an all-out-of-charset
value, a `preserve_separators=false` out-of-charset value, and a too-short
checksum value all raise instead of leaking cleartext or emitting a
non-round-trip covering hash. Behavior is always-fail-closed; there is **no**
`on_unencryptable` config field yet (a single-value `error`-only enum on a
frozen-surface config is vacuous and would drag in the compatibility-contract
`§9` process for no benefit). The field lands additively **when** `vault_token`
does.

Two residual axes were surfaced as structured `QualityWarning`s rather than
closed: `fpe_sub_minimum_domain` (no fix pre-FF1) and
`fpe_partial_plaintext_disclosure` (the format-prefix leak below).

---

## Deferred item 1: `on_unencryptable: error | vault_token`

**Goal.** Give operators a fail-closed alternative to `error` for values FPE
structurally cannot cover: instead of aborting the job, replace the
un-encryptable value with a token and record `token -> source` in the encrypted
vault, so the value is recoverable **only** via the sidecar.

**Why it is not a trivial "reuse the vault".** The engine **explicitly forbids**
`vault: true` on `strategy: fpe` today (`plan/_checks.py::check_vault_columns`:
"fpe is already algebraically reversible ... a vault there stores a second copy
of the source values for zero capability, pure disclosure liability"). And
`vault.collect_vault_entries` only records `vault: true` columns and pairs the
**whole** column, not just the un-encryptable tail. So `vault_token` for FPE
cannot ride the existing `vault: true` machinery as-is.

**Design (to build in its own sprint).**

1. **Config field.** Add `on_unencryptable: "error" | "vault_token"` to the
   per-column config (default `error`). This is the additive frozen-surface
   change that makes the `§9` process worthwhile (a real second value).
2. **Scoped carve-out to the fpe+vault prohibition.** Allow a vault write for an
   FPE column **only** for the un-encryptable tail (the exact values that would
   otherwise fail closed), not the whole column. The prohibition's rationale
   (don't store a redundant second copy of already-reversible values) still
   holds for the FPE'd majority; the carve-out covers only values FPE genuinely
   cannot reverse. `check_vault_columns` gains a narrow exception keyed on
   `on_unencryptable == "vault_token"`.
3. **Per-value token-record path via `StrategyContext`.** `StrategyContext` does
   not currently carry a vault writer, so the FPE handler cannot record a
   mapping. Thread an optional vault sink (or vault path + namespace) into the
   context. When the handler hits an un-encryptable value under `vault_token`:
   (a) generate a deterministic in-charset token (the removed
   `_covering_hash_to_charset` is the natural, reproducibility-preserving
   generator to reinstate here -- keyed, unpredictable, byte-stable across runs,
   which the engine's determinism contract requires; a truly random token would
   break byte-reproducibility), and (b) record `(namespace, token) -> source`
   into the vault. **Fail closed if a vault path/namespace is absent** (a token
   with nowhere to record it is unrecoverable -- a silent data loss, which is the
   class DE-01 exists to prevent).
4. **Matching `unmask` recovery.** `unmask` currently always FPE-inverts an fpe
   column and never consults the vault for it. Add: for a `vault_token` fpe
   column with a supplied vault, recover the tokened values via
   `(namespace, masked) -> source` lookup and FPE-invert the rest. The two are
   distinguishable because the tokens are exactly the vault keys.
5. **Determinism + golden gate.** The token generator must be keyed-deterministic
   so re-runs are byte-identical; add a route-parametrized round-trip test
   (mask -> unmask) proving vault-token values recover, and confirm the golden
   gate stays green.

**Effort.** Medium-high: a config field (+ `§9`), a compile-check carve-out, a
`StrategyContext` change touching the execution edge, a new vault-write path, and
an `unmask` change. Sequence it with (or after) the audited-FF1 swap (Option C),
since FF1 changes which values are un-encryptable in the first place.

---

## Deferred item 2: the partial-plaintext (format-prefix) leak

**What it is.** Under `preserve_separators=true`, a value with **some** in-charset
content and an out-of-charset, data-bearing (alphanumeric) prefix keeps that
prefix **in the clear**: `M000001 -> M<permuted-digits>`, `PRV000001 ->
PRV<permuted>`, `EMP-00001 -> EMP-<permuted>`. The in-charset body is encrypted;
the prefix survives verbatim. This is a real partial-plaintext disclosure -- a
short encrypted tail plus a surviving format prefix can aid re-identification.

**Why DE-01 did not close it (Cam decision, 2026-07-14).** Closing it means
failing closed on any out-of-charset alphanumeric character, which would break
the pervasive letter-prefixed-ID pattern across the entire golden test-flight
corpus (member_id `M######`, customer_id `CU######`, provider_id `PRV######`,
etc. -- all five jobs). The engine cannot distinguish a uniform format-prefix
letter from data-bearing content per value, so the only principled fixes are
(a) fail closed (breaks the corpus; requires re-baselining every job) or
(b) widen those columns to a charset that **covers** the prefix so it gets
encrypted. Cam chose to ship the dangerous all-out-of-charset fix now and defer
the prefix case.

**Interim behavior (shipped).** The leak is unchanged (prefix preserved) but is
now surfaced via the `fpe_partial_plaintext_disclosure` `QualityWarning` and
documented as a known limitation (docstrings + CHANGELOG).

**Full fix (fast-follow).** Two options, not mutually exclusive:
- **Covering charset.** Recommend/allow a charset that covers the prefix (e.g.
  `ALPHANUM` for `M000001`) so the whole value is format-preserving-encrypted,
  no surviving prefix. This is a per-column config choice; the leak disappears
  and the value round-trips. (Applying this to the golden corpus is itself a
  reviewed re-baseline, deferred with this item.)
- **Structured FPE / FF1 with typed sub-fields.** A structured-identifier FPE
  that encrypts the prefix within its own alphabet (or tokenizes it via
  `vault_token`) removes the disclosure without an operator charset change.

Both depend on the audited-FF1 fast-follow and/or `vault_token` above, so they
share a sprint.

---

## Deferred item 3: pre-fix covering-hash artifacts are not reliably reversible

**What it is (Codex cross-model review, 2026-07-14).** DE-01 removed the
covering-hash GENERATION path, so no new non-invertible artifacts are produced.
But any masked output written **before** the fix, for an all-out-of-charset
value, contains an in-charset covering-hash token that is indistinguishable from
a normal FPE ciphertext. `unmask` has no provenance/version check, so it will
run the inverse cipher on such a token and report a fabricated plaintext as
`status="reversed"` -- silent wrong-plaintext, exactly the class DE-01 closes on
the forward path.

**Why it is not fixed now.** A covering-hash token and a real FPE ciphertext are
both just in-charset strings; distinguishing them requires either a per-value
authentication tag (a real crypto/MAC build) or a versioned/authenticated
artifact envelope. There is no cheap, reliable detection to add. And it only
affects PRE-FIX artifacts: the engine is pre-GA (`is_pre_ga()` is `True`), there
are no manifests/masked outputs in the wild under a compatibility guarantee
(the `SEED_PROTOCOL_VERSION` history repeatedly notes "pre-GA, hard cutover; no
manifests in the wild"), and covering-hash generation is gone going forward.

**Decision: document, do not build.** Pre-fix covering-hash outputs are declared
**not reliably reversible** (pre-GA, no compatibility guarantee); regenerate any
such output under the fixed engine. Reliable detection/authentication of
un-invertible values is folded into the structured-FPE / FF1 fast-follow scope
(an authenticated artifact envelope naturally subsumes it). No versioning build
lands in DE-01.
