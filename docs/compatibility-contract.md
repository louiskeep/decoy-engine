# Decoy Compatibility Contract — The Frozen Surface

> **Status:** governance document. **Audience:** every engineer touching
> `decoy-engine`, `decoy` (CLI), or `decoy-platform`. **Post-launch this is a
> required pre-read before any feature branch.** **Owner:** PO.

## Why this document exists

We want to add features as **minor** releases that existing users can adopt
without re-masking their data, re-generating their reports, or losing access to
their vaults. The alternative — cutting a new major version every time we add a
capability — punishes our most committed users and stalls adoption.

That is only possible if we agree, in writing and in advance, on what is
**frozen** (cannot change within a major version) versus what is **fluid** (free
to change). This document is that agreement. If you are about to change anything
in the "frozen surface" below, stop and follow the decision procedure in §5.

The one-sentence rule of thumb, which you can hand to any contributor:

> **A new version adds capability and reads everything older versions wrote;
> output for an unchanged config never changes within a major; the vault is
> forever.**

## 1. The pre-GA → GA flip (read this first)

Today we are **pre-GA**, and engineering-best-practices **§8.1 ("pre-GA = hard
delete")** is in force: breaking changes need no shims, no migration adapters,
no deprecation horizons, because **nobody depends on anything yet.** That rule
is correct right now.

**The day we ship to a real user, §8.1 inverts for everything in §3 below.**
"Defensive code for users who don't exist" becomes "the product feature that
keeps paying users working." There is no gradual transition. The single most
important cultural event at launch is recognizing that the frozen surface is now
frozen, and that the team's reflex of "just delete it and refresh the fixtures"
is, for that surface, now a breaking change to a customer.

Until GA: use this document as the design target so we don't paint ourselves
into a corner. After GA: this document is binding.

## 2. The compatibility surface is wider than the API

The instinct is "don't break the public API." For Decoy the API is the
*smallest* part of the surface. We are a tool that produces **persisted
artifacts users keep** and that promises **stable output across runs.** Those
two facts, not the function signatures, are where breakage actually happens —
silently, with no error message, in a user's pipeline weeks later. Internalize
the four categories in §3.

## 3. The frozen surface

Within a major version, these MUST NOT change in a way that breaks an existing
user. Each entry names the concrete thing in the codebase.

### 3.1 Persisted artifact formats (the biggest surface)

The engine already version-tags every persisted artifact with a `name/vN` tag.
As of this writing the tags include:

```
distribution-snapshot/v1   decoy-vault/v2        vault-key/v1
fpe-key/v1                  quality-report/v1     synth-report/v1
quality-diagnostic/v1      quality-fidelity/v1   quality-policy/v1
quality-shape-fidelity/v1  storm-post-mask/v1    name-hints/v1
ssn/v1  npi/v1  pan/v1  iban/v1  ein/v1  mrn/v1  ndc/v1  icd10/v1
cusip/v1  address/v1  locality/v1  person/v1  provider/v1
composite/v1  custom/v1   ... (every disguise spec)
```

A user runs `decoy fit`, keeps the `distribution-snapshot/v1` artifact, and
feeds it to a later engine version. That artifact is a **contract**.

**Rules:**

- **Never mutate the shape under an existing tag.** If you change what
  `distribution-snapshot/v1` contains, you have broken every holder of one.
  Changing the shape means minting `distribution-snapshot/v2` and **keeping a v1
  reader.**
- **New code reads all historical versions.** The current engine must load every
  `/vN` it has ever written. Readers are append-only; you add `v2` support, you
  never remove `v1` support within a major.
- **New fields are additive and optional.** Adding an optional field to a `/v1`
  artifact that old readers safely ignore is allowed and preferred over a version
  bump. Removing or repurposing a field is not.

### 3.2 The vault (the catastrophic one)

`decoy-vault/v2` + `vault-key/v1` back re-identification. If a new engine version
cannot read an old vault, **users can never unmask the data they already
masked.** That is unrecoverable, not inconvenient.

**Rule:** the vault format is **forever-readable.** Treat it as the most
conservative format we own. A vault change is a new version *plus* a permanent
reader for every prior version, reviewed by the PO. There is no "pre-GA hard
delete" exception for the vault once a real vault exists in the wild.

**Pre-GA hard cutover to v2 (F13, 2026-06-26):** `decoy-vault/v1` was replaced
by `decoy-vault/v2` without a v1 reader. This is legal only pre-GA because no
vaults exist in the wild. The forever-readable rule begins at the first
in-the-wild `decoy-vault/v2` vault. From that point forward, a v2 reader is
permanent and any future format bump must add a v3 alongside a kept v2 reader.

### 3.3 The determinism guarantee (the silent one)

Backed by `docs/determinism.md`, the `determinism/` module, the
`validation/post/_checks/_determinism_sample.py` post-check, and the golden
suites (`tests/snapshots/golden/mask_faker_seeded`,
`tests/integration/golden/test_determinism_invariants.py`).

The guarantee: **same input + same seed → byte-identical output, forever, within
a major version.** A user who masked with one version and later masks *new rows*
with an update — expecting the new rows to join to the old output (same person →
same pseudonym across runs) — is relying on the derivation (HKDF-SHA256 `derive`,
FPE keying, Faker seeding) being unchanged. Change any of it and you break that
user with **zero error message.**

**Rules:**

- The derivation path (`determinism/_derive.py` and everything it feeds) is
  frozen within a major. A change to it is a major bump, even if the public API
  is untouched.
- **A changed golden baseline in CI is the alarm.** If a committed snapshot
  changes, that is the signal that you altered a user-visible guarantee. Golden
  baselines are updated only as a conscious, reviewed, version-gated act — never
  as a "tests went red so I refreshed them" reflex.

**Current state (v6, 2026-06-26, pre-GA):** `SEED_PROTOCOL_VERSION` was bumped
from 5 to 6 by the F2/F3 generation-determinism rewrite (see `CHANGELOG.md`
and `docs/determinism.md`). Both masked output and synthetic-generation output
shifted. This is a pre-GA hard cutover; no vaults exist in the wild. The
`SEED_PROTOCOL_VERSION` byte is now mixed into the generation-path HMAC as well
as the mask-path envelope, so it is the single compatibility knob across both
roots. CONSEQUENCE for future maintainers: any future `SEED_PROTOCOL_VERSION`
bump re-keys synthetic-generation output too, even a bump made for a mask-only
reason. There is no longer a "mask-only" envelope change; budget for the
generation shift (and a corpus re-baseline) whenever you bump the version.
A v5 vault over a synthetic column cannot be unmasked under v6.

The vault format is now `decoy-vault/v2` (F13, 2026-06-26). The v2 file stamps
`SEED_PROTOCOL_VERSION` in an unencrypted header; `load_vault` reads that header
before any decryption attempt and raises a typed
`VaultError(code="vault_protocol_version_mismatch")` on a mismatch, distinct
from the wrong-seed `vault_key_mismatch`. Cross-version unmask is not supported
and was not supported before F13; F13 makes the failure diagnosable rather than
opaque.

### 3.4 The public API + CLI contract

- **Python:** the symbols re-exported from `decoy_engine/__init__.py` and
  `decoy_engine/sdk.py`, with their signatures and output-affecting defaults.
- **CLI:** verb names, flag names, and the exit-code contract (0 ok, 1
  validation/usage, 2 deprecated-shim, 3 runtime).
- **Config:** the `pipeline.yaml` schema. An old config must keep validating and
  running, or be deprecated through §4.4.

### 3.5 Disguises

Disguises are dated/versioned specs (`disguises/`, `disguises/loader.py`,
`disguises/schema.py`), with a drift-guard test. A config pins a disguise
version so its output stays stable.

**Rule:** **never edit a released disguise version.** Add a new dated version.
The drift-guard test enforces this; do not "fix" a shipped disguise in place.

## 4. The rules for adding a feature without breaking anyone

### 4.1 Additive-only public surface

New parameters are keyword-only with defaults that preserve existing behavior.
Never remove or rename a public symbol, and never change a default that changes
output, without the deprecation path in §4.4. The **engine-stays-narrow** rule
(best-practices §3.3) is your ally: the smaller the public surface, the less you
*can* break. Convenience layers (`decoy.mask`) live in the CLI package, not the
engine.

### 4.2 Format versioning, never mutation

See §3.1. Bump the tag, keep the old reader. Prefer an additive optional field
over a version bump when old readers can safely ignore it.

### 4.3 Determinism is sacred within a major

See §3.3. If a feature *needs* to change derivation (e.g. a better KDF), it is a
major-version project with a migration story, not a feature PR.

### 4.4 Deprecation mechanics (when you must change CLI/API)

The `storm scan` → `storm analyze` rename is the template. To retire or change a
public surface:

1. Keep the old surface working as a shim.
2. Emit a `DeprecationWarning` to stderr (CLI: exit code 2 lane).
3. Hold it for **at least one minor release.**
4. Document the removal target version.
5. Add a `CHANGELOG.md` entry (Keep-a-Changelog).

Exit codes stay stable throughout.

### 4.5 The vault is forever

See §3.2. No exceptions.

### 4.6 Platform specifics (when the platform unfreezes)

- Alembic migrations are reversible (up **and** down) and there is a **single
  head** — the gap-closure work already enforces this.
- Ship features dark behind a flag (the `DECOY_ENABLE_ENVIRONMENTS` pattern) so
  code can land before it activates.

## 5. The decision procedure

Before you change something, find it in this table.

| You want to change… | Frozen? | What to do |
|---|---|---|
| Add a new strategy / detector / report metric | No (additive) | Ship it. Register through the public path. New optional config only. |
| Add an optional field to an existing artifact | No (additive) | Add it; ensure old readers ignore it; no version bump. |
| Change the **shape** of an existing `name/vN` artifact | **Yes** | Mint `name/v(N+1)`, keep the `vN` reader. PO review. |
| Change hashing / FPE / Faker seeding / `derive` | **Yes** | Major-version project + migration story. Not a feature PR. |
| Change/remove a public Python symbol or default that affects output | **Yes** | Deprecation path §4.4, or major bump. |
| Rename/remove a CLI verb or flag | **Yes** | Deprecation shim §4.4 (one-minor window). |
| Edit a released disguise version | **Yes** | Forbidden. Add a new dated version. |
| Any change to vault read/write | **Yes (forever)** | New version + permanent prior-version reader + PO review. |
| Change a `pipeline.yaml` schema field | **Yes** | Keep old configs valid, or deprecate §4.4. |
| Internal refactor with no surface change | No | Golden + compatibility tests must stay green; if a golden baseline moves, you changed a guarantee — stop. |

If your change is **Yes (frozen)** and you cannot justify a major bump, the
answer is almost always: **make it additive instead.** Most "I need to change X"
turns into "I can add X alongside the old X" with five more minutes of thought.

## 6. How the freeze is enforced (the machinery)

- **Golden / determinism snapshots** (`tests/snapshots/golden`,
  `tests/integration/golden/test_determinism_invariants.py`) catch **output
  drift**. A baseline change is a red flag, not a refresh chore.
- **The cross-version compatibility corpus** (`tests/integration/compat_corpus/`).
  Golden tests regenerate artifacts with *current* code, so they do **not** catch
  *format* drift. The corpus freezes synthetic artifacts produced at a known
  engine version and verifies the *current* engine can read and round-trip every
  one. It currently covers two read-back artifact kinds:

  - `decoy-vault/v2`: full `load_vault` round-trip plus a schema-tamper bite-test
    (verifies the guard actually fires on a corrupted artifact).
  - `distribution-snapshot/v1`: full `load_spec` round-trip for each reader branch
    (numeric, categorical, conditioned-joint) plus a `schema_version` tamper
    bite-test.

  Intentionally **not** in corpus scope for now: masked CSV/Parquet output (the
  engine has no owned reader for its own masked output; freezing it would only
  retest pandas/pyarrow), and plan YAML / profile JSON (real cross-version readers
  exist, but these are in-process artifacts today; add them once the platform
  persists plans/profiles to disk for cross-version reuse).

  The corpus is synthetic pre-GA: artifacts carry `synthetic: true` and
  `produced_by_engine_version: "0.1.0"`. At GA, replace or supplement with a real
  (`synthetic: false`) artifact of each read-back kind.
- **`CHANGELOG.md`** records every user-visible change.
- **The regression-gate** runs the above on every PR.

## 7. Versioning policy and the 0.x wrinkle

Per-package independent semver (`decoy-engine` and `decoy` cut at `0.2.0`;
`decoy-platform` stays `0.1.x`). The CLI pins the engine it ships with; the
engine publishes before the CLI.

**The wrinkle:** under semver, a `0.x` *minor* bump is technically allowed to
break. Users do not read our semver philosophy; they just get broken.
**Decision: freeze the data contracts (§3.1 artifacts, §3.2 vault, §3.3
determinism) at 1.0-grade the day we have a real user, even while the Python API
remains 0.x-fluid.** The API can churn behind deprecation shims; the things users
*hold* cannot.

## 8. When you genuinely must break something

Major version bump + a real migration tool (read old format, write new) + a
deprecation window + a loud `CHANGELOG.md` entry. **Never silent.** A break the
user discovers by getting wrong output is a defect in this process, not an
acceptable cost.

## 9. Pre-flight checklist (paste into the PR description)

- [ ] I read this document.
- [ ] My change is additive, OR it follows the §5 decision for a frozen item.
- [ ] No `name/vN` artifact shape changed under its existing tag (or: I minted a
      new version and kept the old reader).
- [ ] No determinism golden baseline changed (or: I made a reviewed,
      version-gated decision and said so in the PR).
- [ ] Vault read/write is untouched (or: PO-reviewed new-version + permanent old
      reader).
- [ ] No released disguise version was edited in place.
- [ ] Any CLI/API removal goes through a deprecation shim with a `CHANGELOG`
      entry.
- [ ] The cross-version compatibility corpus still passes.

---

## Pre-GA corpus action item

The cross-version compatibility corpus (§6) exists and runs in CI, covering
`decoy-vault/v2` and `distribution-snapshot/v1`. Before GA: capture a real
(`synthetic: false`) artifact of each read-back kind at the `0.2.0` cut and
add it alongside the existing synthetic fixtures. The corpus cannot be
retrofitted after users hold artifacts we no longer have fixtures for.

## Cross-references

- Engineering best practices §8 (pre-GA/post-GA), §3.3 (library stays narrow):
  `decoy-platform/docs/guides/engineering-best-practices.md`
- Determinism contract: `decoy-engine/docs/determinism.md`
- Roadmap versioning lock: `decoy-platform/docs/ROADMAP.md` ("Versioning")
- This document should be linked from each repo's `CONTRIBUTING.md` and named as
  a required pre-read in the PR template.
