# DE-02: KeyProvider design (Sprint 4) — decision-ready

**Date:** 2026-07-14
**For:** Cam (design + analysis; NOT a build. No production code changed by this document.)
**Branch point read:** `origin/main @ e0dc846` (DE-01 + DE-03 + DE-11 landed).
**Scope:** the last crypto GA blocker — demote `job_seed` to generation-only, introduce a
`KeyProvider` (opaque ≥32-byte secret + HKDF-SHA256 purpose/version derivation), rekey the
**full** keyed surface, and add a fail-closed gate keyed on `is_pre_ga()`.

This is the highest-blast-radius crypto change in the GA set: it changes the IKM of every
re-identification-protecting derivation in the engine. The plan (`2026-07-13-crypto-de11-delivery-plan.md`
§Sprint 4) assumes the keyed surface is "`derive()`-call-site-complete" and names ~8 call-sites.
**That assumption is wrong in two load-bearing ways** (see §1). This doc gives the complete,
verified surface map, a minimal-substitution design that keeps golden fingerprints byte-identical
pre-GA, and the decisions Cam needs to make before anyone builds.

---

## TL;DR

- **The keyed surface is ~2.5× larger than the plan's list** and one keyed strategy (`text_mask`)
  **does not call `derive()` at all** — it keys a raw `HMAC-SHA256(job_seed, matched_text)`. So
  "path-complete = `derive()`-call-site-complete" would ship a column still keyed off `job_seed`
  at GA. Complete map in §1: **23 keyed call-sites across ~20 files**, plus the raw-HMAC site.
- **The mask/generation boundary is a runtime condition, not a directory.** A "generation"
  provider (faker / categorical / composite) is *keyed re-identification surface* exactly when
  `deterministic=True AND a real source column flows in` — then a real value maps deterministically
  to a synthetic one and the mapping is reversible if the seed is known. The same code path with
  `source=None` is pure generation. This is the single distinction that decides what to rekey.
- **Recommended design is a one-slot IKM substitution.** Everywhere the keyed surface currently
  feeds `job_seed` as the seed/IKM, feed a `mask_key` instead. `mask_key = job_seed` when no secret
  is present (byte-identical to today → golden gate stays 53/53 / 5/5), and `mask_key =
  HKDF-SHA256(secret, version)` when a secret is present. All existing per-namespace / per-purpose /
  per-column-tweak domain separation downstream of the IKM is untouched. The only primitive change
  is relaxing `derive()`'s 8-byte seed guard to accept 8 **or** 32 bytes (the 8-byte path is
  unchanged).
- **Determinism is preserved** because the golden gate runs with no secret, which routes to the
  identical 8-byte `job_seed` IKM. No backward-incompatible case found.
- **`KeyProvider` is injected at run time and never serialized into the plan** (the secret must not
  land in a persisted/logged plan). `job_seed` stays in `SeedEnvelope` as the generation seed.

---

## 1. Complete keyed-surface map (the load-bearing part)

### 1.1 Two corrections to the plan's assumption

**Correction A — `text_mask` is keyed but bypasses `derive()`.** `transforms/text_mask.py:158-170`
`_span_key(job_seed, matched_text)` returns `HMAC-SHA256(job_seed, matched_text)` **directly**, not
through `derive()`. It then keys FPE (`_mask_fpe`), Faker seeding (`_mask_faker`, first 4 bytes of
the span key), and date-shift (`_mask_date_shift`, first 8 bytes) off that span key. A rekey scoped
to `derive()` call-sites would leave `text_mask` (and its out-of-core twin, §1.4) keyed off
`job_seed` at GA. The "32-byte" docstring at `text_mask.py:353` is also a lie — `job_seed` is 8
bytes (`plan/_seed.py:108`, `.to_bytes(8, "big")`); it must be corrected in this sprint.

**Correction B — "generation" providers are keyed surface in deterministic mode.** The plan lists
"deterministic faker/categorical `derive_index`" but the actual index-selection lives in the shared
`PoolSampler` (`generation/pool/_sampler.py:183,313`) and the composite generators
(`generation/composite/*`), not only in the `execution/_strategies/*` handlers. The seam is:
`deterministic=True AND source is not None` → a real value is pseudonymized → **keyed**.
`source=None` → fresh generation → **not keyed** (see `_faker.py:97`, `_composite.py:117-122`,
`_categorical.py:187`). Missing the sampler/composite means missing where the derive actually runs.

### 1.2 MASK PATH — keyed re-identification surface (MUST rekey to the secret)

Pandas execution strategies (`execution/_strategies/`), all threading `ctx.job_seed`:

| # | Call-site (file:line) | Keyed primitive | In plan's list? |
|---|---|---|---|
| 1 | `_fpe.py:135` | `derive(job_seed, ns, FPE_KEY_LABEL)` → Feistel key | yes |
| 2 | `_hash.py:54` → `kernel/_scalar.py:75` | `derive(seed, ns, canonical(value)).hex()` → hash token | yes |
| 3 | `_date_shift.py:73` | `derive(job_seed, ns, value)` → day offset | yes |
| 4 | `_shuffle.py:50` | `derive(job_seed, ns, column)[:8]` → permutation RNG seed | **NO** |
| 5 | `_bucket_perturb.py:74` → `transforms/bucket_perturb.py:118` | `derive(job_seed, ns, value)` → numeric offset | **NO** |
| 6 | `_categorical.py:188,202` | `derive_index(job_seed, ns, value, pool_size)` → category select | yes (named) |
| 7 | `_faker.py:97` → `pool/_sampler.py:183` | `derive_index(seed, ns, canonical(value), pool_size)` → pool member select | partial (sampler unnamed) |
| 8 | `_text_mask.py:83` → `transforms/text_mask.py:170` | **raw** `HMAC-SHA256(job_seed, matched_text)` → span key | yes (but not a `derive()` site) |
| 9 | `_code_set.py:99` → `transforms/code_set.py:521` | `derive_index(job_seed, ns, ...)` → code select | **NO** |
| 10 | `_grouped_series.py:63` → `transforms/grouped_series.py:271` | `derive(seed, ns, group_label)[:8]` → monotone_walk RNG | **NO** |
| 11 | `_windowed_date.py:59` → `transforms/windowed_date.py:209` | `derive(seed, ns, row_index)[:8]` → windowed date RNG | **NO** |
| 12 | `_group_key.py:67` → `transforms/group_key.py:171` | `derive(seed, ns, source)` → group key token | **NO** |
| 13 | `_joint_mask.py:51` | `apply_joint_mask(..., job_seed=ctx.job_seed)` | **NO** |
| 14 | `_composite.py:122` | `ProviderSpec(seed=job_seed)` deterministic → coherent pseudonym bundle | **NO** |

Polars execution strategies (`execution/polars/_strategies/`) — separate call-sites:

| # | Call-site | Keyed primitive | In plan? |
|---|---|---|---|
| 15 | `_shuffle.py:51` | `derive(job_seed, ns, column)[:8]` | **NO** |
| 16 | `_categorical.py:114,129` | `derive_index(job_seed, ...)` | **NO** |
| 17 | `_hash.py:53` | `derive(job_seed, ns, canonical(value)).hex()` | **NO** |

(Polars `fpe` / `text_redact` reuse the pandas handlers/transforms, so they inherit the rekey.)

Out-of-core path (`execution/out_of_core/`):

| # | Call-site | Keyed primitive | In plan? |
|---|---|---|---|
| 18 | `_mask_group_b.py:132` | `derive(job_seed, ns, FPE_KEY_LABEL)` → FPE | yes |
| 19 | `_mask_group_b.py:336,346` | `derive_index(job_seed, ...)` → categorical | yes (via group_b) |
| 20 | `_mask_group_c.py:101,147` | `text_mask` (raw HMAC), `code_set`, `bucket_perturb` | **NO (group_c unnamed)** |
| 21 | `_mask.py:222,257`, `_relation.py:101`, `_runner.py:356,452` | `job_seed` threading into the above | n/a (plumbing) |

Reversal + key-store (standalone functions, take `job_seed` directly — outside `StrategyContext`):

| # | Call-site | Keyed primitive | In plan? |
|---|---|---|---|
| 22 | `vault.py:126` (`_fernet`) | `derive(job_seed, VAULT_NAMESPACE, VAULT_KEY_LABEL)` → Fernet key | yes |
| 23 | `unmask.py:308` | `derive(job_seed, ns, FPE_KEY_LABEL)` → reverse FPE key | yes |

**Count: 23 keyed call-sites across ~20 files, of which one (`text_mask`, sites 8 + 20) does not
use `derive()`.** The plan named 8. Call-sites the plan **missed**: shuffle (pandas+polars),
bucket_perturb, code_set, grouped_series/monotone_walk, windowed_date, group_key, joint_mask,
deterministic composite, the `PoolSampler` where faker's `derive_index` actually runs, the polars
`_hash`/`_categorical`/`_shuffle` trio, and out-of-core `_mask_group_c`.

### 1.3 GENERATION PATH — reproducibility only (job_seed STAYS; do NOT rekey)

These consume `job_seed` (as `spec.seed` / build seed) purely to make **fresh synthetic data**
reproducible. No real value is protected, so there is nothing to key. They must keep deriving from
`job_seed` unchanged:

- `generation/composite/*` in **non-deterministic** mode (`spec.seed`, `source=None`): `_person`,
  `_custom`, `_provider`, `_name_email`, `_address`, `_city_state_zip`.
- `generation/pool/_builder.py` (pool *build* — the bag of fake values; sites `:57`).
- `generation/pool/_sampler.py` / `_pool_adapter.py` in **generate** mode (`source=None`).
- `providers_v2/identifiers/*` (`derive_value(...)` — generating fresh SSN/EIN/NPI/… from row
  position), `providers_v2/_adapter.py`, `_faker_adapter.py`.
- `generation/synthesize.py`, `generators/columns.py`, `generators/derivation.py`
  (`GenDeriveContext`), `generation/_grouped_windowed_generators.py`.

### 1.4 The entanglement to flag plainly

The plan treats mask and generation as separable file sets. They are not: **the same primitives**
(`PoolSampler.sample(seed=, source=, deterministic=)`, `ProviderSpec.seed`, the transform-library
functions `apply_grouped_series` / `apply_windowed_date` / `apply_group_key` / `bucket_perturb`)
**serve both roles, and the `seed` argument's security meaning depends on the call mode.** The pool
BUILD stays on `job_seed` (fine — synthetic values); the pool-member SELECTION from a real source
value must move to the mask key (`_sampler.py:183` — the `seed=` argument). Because the seam is a
runtime condition, the rekey cannot be done purely by "swap `job_seed` for `mask_key` in these
files." It must be done at the **caller**, which knows whether a real source flows in:

- All `execution/_strategies/*` scalar mask handlers: a real source column always flows in →
  always mask_key.
- Composite / faker / categorical: mask_key **iff** `deterministic AND source is not None`; else
  `job_seed`. This is a per-call decision (`_composite.py:117` computes exactly this `deterministic`
  + `source` pair already), so the handler already has the signal — it just needs to pass mask_key
  vs job_seed accordingly.

`subset/_seed.py:62` (`DeriveContext.for_column(job_seed, ns)`) selects *which rows* survive a
subset. It is deterministic row selection, not value re-identification — treat as generation/repro
(stays on `job_seed`), but call it out for Cam in case subset membership is considered sensitive.

Non-keyed hashing that must NOT be touched (schema fingerprints, model provenance, cache keys):
`profile/_hash.py`, `storm/model_pack/*`, `execution/_mem_telemetry.py`,
`execution/out_of_core/_relation.py` (cache keys), `reference_tables/_types.py`.

---

## 2. KeyProvider interface proposal

### 2.1 Existing material to build on

The engine already ships a 32-byte master-key resolver — it is just wired to the **legacy V1 graph
path**, not V2 execution:

- `context.py:371` `make_key_resolver(master: bytes, pipeline_label)` — validates `len(master) == 32`,
  does `HKDF-SHA256(master, "pipeline:<label>")` then `HKDF-SHA256(pipeline_key, info)`
  (`_hkdf_sha256` at `context.py:347`, routed through the canonical `determinism/_hkdf.hkdf_sha256`).
- `internal/crypto.py` `hmac_hex` / `hmac_seed` — the "Path B" keyed primitives, and
  `deterministic_hash` (the deprecated `SHA256(value+seed)` that DE-02 is fundamentally about
  retiring as a key model).
- `determinism/_derive.py` `derive()` — `HKDF-SHA256(IKM=seed, salt="decoy-engine/determinism/v1",
  info=namespace)` then `HMAC-SHA256(key, versioned_input)`. **This is the shape we keep**; DE-02
  only changes what `seed`/IKM is.

The DE-02 design is therefore not new crypto — it is promoting the existing 32-byte resolver idea
onto the V2 execution edge, with `derive()` as the unchanged downstream envelope. This satisfies
`CLAUDE.md` ("we use HKDF-SHA256 … we do not roll our own") and cites RFC 5869 (HKDF) + RFC 2104
(HMAC), NIST SP 800-57 Pt.1 Rev.5 (256-bit strength target), OWASP Key Management ("never derive
secrets from low-entropy config").

### 2.2 The abstraction

```python
class KeyProvider(Protocol):
    """Opaque source of keyed mask material. The engine only ever takes bytes."""
    key_version: str            # non-secret, e.g. "v1"; recorded in the evidence manifest
    def mask_key(self) -> bytes  # the 32-byte mask-root IKM (see below)

@dataclass(frozen=True)
class SecretKeyProvider:           # GA path
    _secret: bytes                 # opaque, >= 32 bytes, injected from a managed store
    key_version: str = "v1"
    def mask_key(self) -> bytes:
        return hkdf_sha256(ikm=self._secret,
                           salt=b"decoy-engine/keyprovider/v1",
                           info=f"decoy/mask/{self.key_version}".encode(),
                           length=32)

@dataclass(frozen=True)
class SeedKeyProvider:              # pre-GA / no-secret path (backward-compatible)
    _job_seed: bytes               # the 8-byte generation seed
    key_version: str = "seed"      # marks a non-secret-keyed artifact in the manifest
    def mask_key(self) -> bytes:
        return self._job_seed       # identical IKM to today -> golden unchanged
```

Two-level derivation, versioned:

```
secret ──HKDF(salt=keyprovider/v1, info="decoy/mask/<version>")──▶ mask_root (32 bytes)
mask_root ──derive(mask_root, namespace, purpose_label)──▶ per-(column,purpose) key   (UNCHANGED envelope)
```

Because `derive()` already domain-separates by `namespace` (per-column) and by the `source`/label
(`FPE_KEY_LABEL`, `VAULT_KEY_LABEL`, column-name tweak), **a single `mask_root` inherits every
existing separation for free**. Rotating `key_version` deterministically re-keys the whole surface
without touching any derivation code. `key_version` (never the secret) is stamped into the evidence
manifest for audit.

### 2.3 How it threads to each call-site

- **`StrategyContext` gains one field: `mask_key: bytes`** (`execution/_adapter.py:83-103`), set to
  `key_provider.mask_key()`. Every `execution/_strategies/*` handler swaps `ctx.job_seed` →
  `ctx.mask_key` at its keyed call — **except** the faker/categorical/composite deterministic seam,
  which passes `ctx.mask_key` when `deterministic and source is not None`, else `ctx.job_seed` (§1.4).
  `ctx.job_seed` remains on the context for the generation branches.
- **`derive()` relaxes its length guard** (`determinism/_derive.py`, `_SEED_LENGTH = 8`) to accept
  8 **or** 32 bytes. The 8-byte path is byte-for-byte unchanged; 32-byte is the new secret path.
  (`DeriveContext.for_column` and `derive_index` inherit this via the same guard.)
- **`text_mask`** (sites 8, 20): `_span_key(mask_key, matched_text)` instead of `job_seed`; fix the
  `:353` "32-byte" docstring. No `derive()` involvement, so no guard interaction.
- **`KeyProvider` is injected at run time**, not compiled into the plan:
  `ExecutionAdapter.run(..., key_provider: KeyProvider)` alongside `registry` / `pool_cache`
  (`_adapter.py:134`), and each adapter (`_pandas_adapter.py:195`, `_sequential.py:288`,
  `_chunked.py:480`, `polars/_polars_adapter.py:214`, `out_of_core/_runner.py:356`) resolves it into
  `StrategyContext.mask_key`. **The secret is never serialized** — `SeedEnvelope.job_seed` stays the
  only key-ish thing in the persisted plan, and it is now (correctly) non-secret.
- **`vault.py` / `unmask.py`** take a `mask_key: bytes` parameter (they are standalone, outside
  `StrategyContext`): `vault_writer(config, mask_key)`, `_fernet(mask_key)`, `unmask_pipeline(...,
  mask_key)`. Vault migration is a Cam decision (§7 / open questions) — a vault written under
  `job_seed` cannot be opened under a secret key without a dual-read window or a rekey tool.

---

## 3. The determinism constraint (analyzed)

**Requirement:** switching the IKM changes every keyed output, so pre-GA / no-secret behavior must
derive keys from `job_seed` *exactly as today*, and derive from the secret *only* when one is
present. The golden gate (53/53 masking fingerprints, 5/5 unmask round-trips) must stay green.

**This is achievable, and the design makes it near-automatic:**

1. The golden gate runs with **no secret** → `SeedKeyProvider.mask_key()` returns the 8-byte
   `job_seed` → every keyed call feeds the identical IKM it feeds today → `derive()` takes the
   unchanged 8-byte branch → **byte-identical output**. `text_mask` likewise keys the identical
   `HMAC(job_seed, text)`.
2. The `derive()` guard relax (8 **or** 32) does not alter the 8-byte code path — same HKDF, same
   HMAC, same `SEED_PROTOCOL_VERSION` byte. No fingerprint moves.
3. The generation path is untouched (still `job_seed`), so synthetic-column fingerprints and the
   deterministic-generate goldens are unaffected.

**No backward-incompatible case was found.** Two things to watch, neither a blocker:

- **`SEED_PROTOCOL_VERSION` bump.** Introducing the 32-byte secret path is an output-shifting change
  *for secret-keyed artifacts only*, but the codebase convention (`_derive.py` v1→v6 history) is to
  bump the version byte on any envelope-shape change. Recommendation: **do not bump for the no-secret
  path** (it must stay identical), and if a bump is wanted to stamp "secret-capable era," gate it so
  the byte only changes when a secret is present — otherwise the golden gate moves for the wrong
  reason. Cam decision D-3 below. DE-01 already bumped to v6 for FPE; if DE-01's FF1 fast-follow and
  DE-02 both want a bump, coordinate them into one (the discussion's "one coordinated pre-GA bump").
- **The deterministic faker/categorical/composite seam** is the one place where a *mask* output will
  change when a secret is introduced (correct — that is the rekey). With no secret it is identical.
  The only risk is a handler that passes `job_seed` where it should pass `mask_key` (or vice-versa);
  that is a per-call correctness point, so the regression suite must assert, for a masked
  deterministic-faker column, that output *changes* under a secret and is *identical* without one.

---

## 4. Secret-source options (for Cam)

The engine only ever accepts bytes; where they come from is a host decision. Concrete options:

| Option | Dev story | GA story | Trade-offs |
|---|---|---|---|
| **A. Env var** `DECOY_MASK_SECRET` (hex/base64, ≥32 bytes) | export in shell / `.env` | ops injects from a secret store into the process env | Simplest; works for CLI + self-host. Env vars leak into `/proc`, crash dumps, CI logs. Fine for self-host, weak for multi-tenant. |
| **B. Config field** `global_settings.mask_secret_ref` (a *reference*, not the secret) | points at a file path / env name | points at the platform secret_ref / KMS handle | Keeps the secret out of the YAML; the engine resolves the ref to bytes at run edge. Needs a small resolver. Best fit with the platform's existing `secret_ref` design. |
| **C. Platform secret manager / KMS envelope** (per-tenant key) | n/a (platform-only) | platform decrypts an envelope key from KMS/HSM, hands 32 bytes to `run(key_provider=…)` | Strongest; per-tenant isolation; audit via KMS. Doesn't help CLI/self-host alone. |
| **D. `KeyProvider` passed programmatically** (the actual boundary) | test/CLI constructs `SecretKeyProvider(os.urandom(32))` | platform constructs it over C | This is the real seam; A/B/C are just *ways to obtain the bytes* that feed D. |

**Recommendation: D is the boundary (always), fed by B for CLI/self-host and C for the platform.**
The engine ships `SecretKeyProvider` + a tiny ref-resolver for `mask_secret_ref` (Option B: file or
env-name), and `SeedKeyProvider` as the pre-GA default. The platform passes a KMS-derived
per-tenant key straight into `run(key_provider=…)` (Option C) and never touches the ref path. This
matches the discussion's "engine takes bytes, source stays a platform/tenancy decision," so DE-02
lands without forcing a platform-architecture choice.

---

## 5. Fail-closed gate design

A pre-execution check (before any table / quarantine / vault / manifest is written):

```
if compiled_plan has any KEYED strategy:                # any of the 23 sites in §1.2
    provider = resolved KeyProvider (or None)
    if is_pre_ga():
        if provider is None -> SeedKeyProvider(job_seed) # backward-compatible default
        if provider is a real secret but < 32 bytes -> raise WeakMaskSecret   # always enforced
    else:  # GA
        if provider is None or secret < 32 bytes -> raise KeyedStrategyRequiresSecret  # FAIL CLOSED
```

New typed errors (per the discussion): `KeyedStrategyRequiresSecret`, `MissingMaskSecret`,
`WeakMaskSecret`. The gate keys on `is_pre_ga()` (`release.py`), which is already the codebase's
single pre-GA/GA switch. **Pre-GA:** absent secret → silently falls back to `SeedKeyProvider`
(today's behavior, gate is a no-op except the weak-secret check when a secret *is* supplied).
**At GA (`is_pre_ga()` → False):** absent-or-weak secret on a keyed plan hard-errors before any
output. `release.py`'s "pre-flip remediation" note already documents this pattern for DE-03; DE-02
adds a bullet: fixtures that mask keyed columns must supply a ≥32-byte secret before the flip.

**Dev / quickstart story at GA (so the gate is not watered down):** a developer running locally at
GA must supply a secret, but it need not be *managed* — `DECOY_MASK_SECRET=$(openssl rand -hex 32)`
(Option A) or a throwaway file ref (Option B) satisfies the ≥32-byte gate. The gate enforces
*presence + length*, not provenance; provenance is the host's job. Ship a one-liner in the quickstart
and a clear error message that prints the exact command. This keeps "fail closed at GA" real (no
silent null-key run) without making local dev require KMS.

---

## 6. job_seed demotion

`job_seed` stays exactly what it is (`plan/_seed.py`: 8-byte big-endian of
`global_settings.seed`, default 0) and keeps driving **generation reproducibility**:

- All §1.3 generation call-sites read it unchanged → synthetic values are byte-stable across runs
  for the same seed, exactly as today.
- It is **removed from the *key* role**: the keyed surface (§1.2) no longer treats `job_seed` as the
  confidentiality secret; that role moves to `KeyProvider.mask_key()`. When no secret is present,
  `mask_key == job_seed` numerically, but its *documented contract* changes: `SeedEnvelope.job_seed`
  is labeled "non-secret generation seed," and the docs stop claiming it protects masked output.
- Generation determinism cannot break, because no generation call-site changes what it feeds
  `derive`/`derive_index`/`GenDeriveContext`. The only outputs that move (under a secret) are the
  keyed ones — by design.

Docstring debt to fix in this sprint: `execution/_adapter.py:87-89` ("`job_seed` … is the sole
entropy input") must be corrected to "generation seed; keyed strategies draw from `mask_key`," and
`text_mask.py:353` "32-byte HMAC key material" → the mask_key contract.

---

## 7. Decisions Cam needs to make

1. **Secret source (§4).** Confirm: `KeyProvider` is the boundary (D); CLI/self-host feeds it via a
   `mask_secret_ref` file/env resolver (B), platform feeds it a KMS-derived per-tenant key (C). Or
   pick a different split (e.g. env-var-only A for v1). *Lean: D fed by B + C.*
2. **Gate dev-mode behavior at GA (§5).** Confirm that at GA a *presence+length* check is enough for
   local dev (a `openssl rand` throwaway secret passes), i.e. the gate enforces ≥32 bytes but not
   provenance. Or require managed provenance even in dev (harder quickstart). *Lean: presence+length.*
3. **`SEED_PROTOCOL_VERSION` bump policy (§3).** Do we bump the version byte for the secret-capable
   era, and if so gate the bump so it only shifts output when a secret is present (keeping the
   no-secret golden identical)? And do we coordinate it with DE-01's FF1 fast-follow bump into one?
   *Lean: no bump on the no-secret path; coordinate any secret-era bump with the DE-01 FF1 bump.*
4. **Vault + already-masked-output migration (§2.3, unavoidable determinism trade-off).** Every
   vault and every masked output produced to date is keyed on `job_seed`. Under a real secret they
   do not open / are not reproducible. Options: (a) dual-read window in `unmask`/`load_vault` (try
   mask_key, then legacy job_seed); (b) an explicit `rekey-vault` tool; (c) pre-GA = hard-delete and
   regenerate (no manifests in the wild — the `_derive.py` history says so). *Lean: (c) for pre-GA
   artifacts; masked outputs cannot be rekeyed in place regardless.* This is the one trade-off that
   cannot be made backward-compatible.
5. **Subset membership (§1.4).** Is deterministic row-subset selection (`subset/_seed.py`)
   sensitive enough to move to the mask key, or does it stay on `job_seed` as reproducibility?
   *Lean: stays on job_seed (it selects rows, not values).*
6. **When does GA hard-block seed-only keyed masking (§5)?** Confirm the flip lands before the GA
   corpus freeze so no GA artifact is ever produced under a seed-only key, and confirm the date.

---

## 8. Scope reality check

The keyed surface is **larger and more entangled than the plan assumes** (§1: 23 sites vs 8, plus a
non-`derive()` site, plus a shared mask/generate seam that is a runtime condition rather than a file
boundary). The *good* news: the design collapses that breadth into **one substitution at the IKM
slot** plus one `StrategyContext` field, one `derive()` guard relax, one `text_mask` key swap, and a
`mask_key` param on vault/unmask — because all downstream domain separation is already in `derive()`.
The build is mechanical but **wide and unforgiving**: a single missed call-site (e.g. `_shuffle`,
`bucket_perturb`, `text_mask`, or the deterministic-faker seam) ships a column still keyed off
`job_seed` at GA, which is precisely the DE-02 defect. The acceptance test must therefore assert, per
keyed strategy, that output *changes* under a secret and is *byte-identical* without one — that is the
only way to prove path-completeness, and it must cover all 23 sites, not the 8 in the plan.
