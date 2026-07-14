# DE-02 KeyProvider Design: Separating Public Seed from Masking Key Material

**Finding:** DE-02 (CRITICAL) in
[adversarial-architecture-review-2026-07-12.md](../adversarial-architecture-review-2026-07-12.md).
**Status:** design brief. No source under `src/` changed by this document.
**Author scope:** security/crypto tech-lead decision brief to unblock the product owner.

This document separates the work into two clearly labelled tiers:

- **ENGINE-BUILDABLE-NOW**: the `KeyProvider` boundary, purpose-key derivation, and the
  fail-closed missing-secret gate. Reversible, flag-gated, no product-fork decision required.
- **CAM-GATED**: where the 256-bit secret comes from, vault migration/rekey, and whether a
  seed-only "secure" mode is hard-rejected by default and when.

---

## 1. The exact current defect

Today one integer, `global_settings.seed`, is the sole secret behind reproducibility, HMAC
masking, FPE, unmask, and vault encryption. Four code facts establish the defect.

**1a. The seed is a required plain integer, capped at 64 bits.**
`config/_global_settings.py:33` declares `seed: int` on a Pydantic model. The normalizer
`plan/_seed.py:20-103` (`_normalize_job_seed_int`) requires only `0 <= seed_int < (1 << 64)`
(line 97-101, code `seed_overflow`). `_normalize_job_seed` (line 106-108) then does
`.to_bytes(8, "big")`: the entire downstream key chain is fed at most **8 bytes = 64 bits** of
secret material, and in practice far less because operators pick small human values (the review
recovered the example seed `42` from a vault and a known-plaintext hash in 43 attempts).

**1b. The seed defaults to 0 in the lower paths.**
`plan/_seed.py:52-57`: when `global_settings.seed` is absent or explicitly `None`, `seed_int = 0`.
A job with no seed configured is keyed on the all-zero 8-byte value. There is no fail-closed step
that notices a keyed masking strategy is running under a null or trivially-guessable secret.

**1c. The same seed is reused across every keyed purpose.**
- Hash: `execution/_strategies/_hash.py:52-58` keys `hash_array` on `ctx.job_seed` + namespace.
- FPE: `execution/_strategies/_fpe.py:110-111` derives the Feistel key as
  `derive(ctx.job_seed, namespace, FPE_KEY_LABEL)`.
- Vault: `vault.py:115-127` (`_fernet`) derives the Fernet key as
  `derive(job_seed, VAULT_NAMESPACE, VAULT_KEY_LABEL)` and hands it straight to `Fernet`.
- Unmask keys on the same `job_seed` value by construction (it must, to invert FPE).

All four call the same `derive(job_seed, ...)` with the same 8-byte `job_seed`. HKDF-style
domain separation by label (`FPE_KEY_LABEL`, `VAULT_KEY_LABEL`) separates *purposes* but not
*strength*: every purpose inherits the same <=64-bit input entropy.

**1d. The documented 32-byte master key is not on the live mask path.**
`docs/security/key-derivation.md` describes a `DECOY_MASTER_KEY` (32 bytes) resolver
(`make_key_resolver`) that the V1 `transforms/fpe.py` `FPEStrategy._column_key` (line 581-609)
consumes via `self.derive_key("mask")`. But the live V2 execution handlers
(`_strategies/_hash.py`, `_strategies/_fpe.py`) do **not** use that resolver. They use
`derive(ctx.job_seed, ...)`. So the strong-key design already documented is dark code on the
executed path. This is the review's point: "The mask path does not use the 32-byte master-key
resolver described by `docs/security/key-derivation.md`."

**Why HKDF cannot repair this.** Per [RFC 5869](https://www.rfc-editor.org/rfc/rfc5869.html)
sections 3.1 and 4, HKDF is extract-then-expand: it concentrates and diffuses existing input
keying material but does not create entropy. A 64-bit (or 0-bit) input yields at most 64 bits of
unpredictability no matter how many expansion rounds run. Deriving `decoy/fpe/...` from a 42 does
not make a 42 secret.

---

## 2. Proposed `KeyProvider` boundary (ENGINE-BUILDABLE-NOW)

Introduce an explicit key boundary at the engine execution edge. The engine accepts **opaque key
bytes plus a stable, non-secret key-id and key-version**, derives **versioned purpose keys** via
HKDF, and **fails closed** before any output if a keyed strategy has no secret. No network
dependency is added: KMS/HSM storage, tenant authorization, and rotation orchestration remain the
host platform's responsibility, exactly as the review's "Target Crypto Architecture" section
requires.

### 2.1 Boundary rules

1. **Public seed stays, for generation only.** `global_settings.seed` continues to drive sampling
   and synthetic generation (the reproducibility PRNG). It is documented as non-secret and is
   never used for keyed masking, FPE, unmask, or vault after this change.
2. **Keyed masking requires a `KeyProvider`.** Hash, FPE, unmask, and vault draw key material only
   from an injected `KeyProvider`, never from the seed.
3. **Opaque material in, versioned purpose keys out.** The engine treats the provider's secret as
   an opaque byte string of at least 32 bytes. It derives one key per (purpose, namespace,
   key-version) via HKDF-SHA256 with a structured `info` string.
4. **Domain-separated `info`.** `info = "decoy/<purpose>/v2/<namespace>/<key-version>"`. This binds
   every derived key to its purpose, its namespace, and the caller-declared key version, so
   rotating the version deterministically re-keys without changing the derivation code.
5. **Fail closed before output.** If any keyed strategy is scheduled and no provider (or an
   empty/short secret) is supplied, compilation or the pre-execution gate raises a typed error
   before any table, quarantine, vault, or manifest is written.
6. **No secret in logs or plans.** The provider exposes only `key_id` and `key_version` for audit;
   `secret()` is never logged, never serialized into the frozen plan, and never placed in the
   evidence manifest. Only `key_id`/`key_version` may appear as provenance.

### 2.2 Derivation

```
KeyProvider.secret()  (opaque, >= 32 bytes, high-entropy, host-managed)
        |
        v  HKDF-SHA256(ikm=secret, salt=<fixed or key_id>,
        |             info="decoy/<purpose>/v2/<namespace>/<key-version>", L=32)
   purpose_key (32 bytes)   ->  hash / fpe / vault / unmask
```

Per [NIST SP 800-57 Part 1 Rev.5](https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final) section 5.6,
a 256-bit symmetric key is the appropriate strength target for long-term confidentiality of the
re-identification surface (masked outputs and vaults are long-lived). The 32-byte minimum on
`secret()` enforces that at the boundary.

---

## 3. Interface sketch (ENGINE-BUILDABLE-NOW)

```python
from typing import Protocol, Literal

Purpose = Literal["hash", "fpe", "vault", "unmask"]


class KeyProvider(Protocol):
    """Opaque source of masking key material, injected at the execution boundary.

    The engine never fetches this over a network. A host platform (KMS/HSM,
    env-var loader, test fixture) implements it and hands a fully-resolved
    instance to run_pipeline. key_id and key_version are non-secret and are the
    only fields allowed into logs, plans, and the evidence manifest.
    """

    @property
    def key_id(self) -> str: ...        # stable non-secret identifier (audit only)

    @property
    def key_version(self) -> str: ...   # stable non-secret version tag (rotation)

    def secret(self) -> bytes: ...      # >= 32 bytes; raises MissingMaskSecret if none


def derive_purpose_key(
    provider: KeyProvider,
    purpose: Purpose,
    namespace: str,
    *,
    length: int = 32,
) -> bytes:
    """HKDF-SHA256 purpose key. info binds purpose + namespace + key_version."""
    material = provider.secret()               # raises MissingMaskSecret if absent
    if len(material) < 32:
        raise WeakMaskSecret(key_id=provider.key_id, got=len(material))
    info = f"decoy/{purpose}/v2/{namespace}/{provider.key_version}".encode()
    return hkdf_sha256(ikm=material, info=info, length=length)


def require_mask_secret(plan: "Plan", provider: KeyProvider | None) -> None:
    """Fail-closed gate. Runs before any durable write.

    If the compiled plan contains any keyed strategy (hash, fpe, vault, unmask)
    and no usable provider is present, raise before output creation.
    """
    if plan.has_keyed_strategy() and provider is None:
        raise KeyedStrategyRequiresSecret(strategies=plan.keyed_strategies())
    # Probing secret() here surfaces MissingMaskSecret/WeakMaskSecret pre-output.
    if provider is not None:
        _ = provider.secret()
```

New typed errors (siblings of the existing `MaskKeyDerivationError` in
`transforms/fpe.py:604`): `KeyedStrategyRequiresSecret`, `MissingMaskSecret`, `WeakMaskSecret`.
All three fail the run before any table, quarantine, vault, or manifest is published, matching the
review's required verification ("missing secret fails").

`ctx.job_seed` on the keyed strategies is replaced by a resolved purpose key: e.g. FPE becomes
`key = derive_purpose_key(provider, "fpe", namespace)` instead of
`derive(ctx.job_seed, namespace, FPE_KEY_LABEL)` at `_strategies/_fpe.py:110-111`. (That edit is
part of the concurrent refactor's surface and is out of scope for this doc; the design fixes the
target shape.)

---

## 4. Work split

### 4.1 ENGINE-BUILDABLE-NOW (Phase 0, reversible, flag-gated)

| Item | What ships |
|---|---|
| `KeyProvider` protocol | The interface above, plus a `StaticKeyProvider(secret, key_id, key_version)` test/host adapter |
| `derive_purpose_key` | HKDF-SHA256, `info="decoy/<purpose>/v2/<ns>/<key-version>"`, 32-byte minimum enforced |
| Fail-closed gate | `require_mask_secret` run before any durable write; three typed errors |
| Seed demotion | `global_settings.seed` documented + wired as generation-only reproducibility; keyed paths stop reading it |
| Provenance | `key_id`/`key_version` recorded in the evidence manifest; secret never logged/serialized |
| Flag | New keyed behavior gated behind an explicit `secure_keys` mode so existing seed-only jobs keep running until Cam sets the default (see 4.2) |

None of these require a product-architecture decision. They are additive: the provider is
injected, the gate is on when a provider is present, and the flag preserves the current path until
Cam flips the default.

### 4.2 CAM-GATED PRODUCT FORKS

1. **Where the 256-bit secret comes from.** The engine only accepts bytes. The host decides:
   platform KMS/HSM envelope, per-tenant derived key, env-var/secret-file for self-hosted, or a
   generated-and-stored key. This is a platform architecture and tenancy decision, not an engine
   one.
2. **Migration / rekey of existing vaults and outputs.** Every vault written to date is keyed on
   `derive(job_seed, "vault", VAULT_KEY_LABEL)` (`vault.py:126`). Moving to a `KeyProvider` changes
   that key, so old vaults become undecryptable under the new derivation. Options for Cam: (a)
   dual-read window that tries the new purpose key then the legacy seed-derived key during a
   migration period; (b) an explicit `rekey-vault` tool that decrypts under the seed key and
   re-encrypts under the new purpose key, stamping the new `key_id`/`key_version` in the vault
   header; (c) declare pre-GA vaults disposable (pre-GA = hard delete per repo policy) and
   regenerate. Hash/FPE masked *outputs* cannot be rekeyed in place without re-running the job
   against source; that is acceptable if source is retained, and is itself a decision.
3. **Hard-reject seed-only "secure" mode by default, and when.** Phase 0 ships the flag OFF-safe
   (seed-only still runs, loudly deprecated). Cam decides the date the default flips to reject any
   keyed strategy that lacks a `KeyProvider`, and whether GA hard-blocks seed-only keyed masking
   entirely. Recommendation: flip the default to fail-closed before the GA corpus freeze so no GA
   artifact is ever produced under a seed-only key.

---

## 5. Test matrix

| # | Property | Assertion |
|---|---|---|
| T1 | Secret dominates seed | Same `seed`, different `KeyProvider.secret()` -> hash/FPE/vault outputs differ |
| T2 | Determinism under key | Same secret + same `key_version` -> byte-identical masked output across runs/processes |
| T3 | Version rotation | Same secret, bumped `key_version` -> different purpose key, different output (deterministic re-key) |
| T4 | Missing secret fails closed | Keyed strategy present, provider `None` -> `KeyedStrategyRequiresSecret` before any table/quarantine/vault/manifest write |
| T5 | Weak secret fails closed | `secret()` returns < 32 bytes -> `WeakMaskSecret` before output |
| T6 | Purpose separation | `hash` and `fpe` under the same namespace/secret derive independent keys (info differs by purpose) |
| T7 | Namespace separation | Same purpose/secret, different namespace -> independent keys |
| T8 | No key in logs | Log capture across a full keyed run contains no `secret()` bytes; only `key_id`/`key_version` appear |
| T9 | No key in plan/manifest | Frozen plan and evidence manifest contain `key_id`/`key_version` only, never secret material |
| T10 | Seed is generation-only | Changing `seed` changes synthetic-generation output but does not change keyed-mask output |
| T11 | Offline-search resistance | Config + masked output together do not enable a small offline key search (no low-entropy secret is derivable from published artifacts) |

T1, T2, T4, T8, T9 directly satisfy the review's DE-02 verification line.

---

## 6. Standards references

- [RFC 5869 (HKDF)](https://www.rfc-editor.org/rfc/rfc5869.html): extract-then-expand; HKDF does
  not amplify entropy. Justifies the 32-byte minimum and the seed/secret split.
- [NIST SP 800-57 Part 1 Rev.5](https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final): key-strength
  and key-lifetime guidance; 256-bit target for long-term re-identification-surface protection;
  key separation by purpose.
- [OWASP Key Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html):
  secrets injected from a managed store, never derived from low-entropy config; rotation via
  versioned key ids; no secret in logs.
</content>
</invoke>
