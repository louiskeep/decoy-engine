# DE-01 — FPE ("format-preserving encryption"): what it is, what we built, and the options

Decision brief for Cam. All code claims below were independently verified against source (Fable review, 2026-07-13). Companion to `2026-07-13-crypto-ga-blockers.md`.

---

## 1. What FPE is supposed to be (plain terms)

**Format-preserving encryption** turns a value into another value *of the same shape* — encrypta a 9-digit SSN and you get a different 9-digit number, encrypt  VIN and you get a valid-looking VIN. That's what makes it useful for masking: downstream systems that expect "a 9-digit number" keep working, but the real value is hidden. Crucially it is **reversible** with the key (unmask), and *without* needing a side lookup table.

The recognized standard for this is **NIST SP 800-38G**, which defines two algorithms: **FF1** and **FF3-1**. They are built on AES, use a specified number of rounds (FF1 = 10), a specified tweak schedule, and — importantly — a **minimum domain size** (~1,000,000 possible values) below which the construction is not considered secure.

## 2. What we actually built

Our `transforms/fpe.py` is **not** FF1 or FF3-1. It's a home-rolled **8-round Feistel network keyed with HMAC-SHA256**. The module's own docstring is honest about this ("this is not NIST FF1"). The problem is that the **product-facing layer sells it as NIST**: `unmask.py` and the FPE strategy both describe a "NIST SP 800-38G FF1 key model." So we have two individually-true statements at different layers that are, together, an **overclaim**: the box says NIST FF1; the thing inside is a custom cipher.

This also violates our own engine rule (`CLAUDE.md`: "we do not roll our own" for crypto primitives).

## 3. Why it matters — three separate problems bundled under "DE-01"

It helps to split DE-01, because the three clusters have very different urgency and the options only really disagree about cluster (C).

**(A) Honesty of the claim.** We tell users (and, implicitly, their compliance/auditors) that regulated identifiers are protected with NIST FF1. They aren't. For a data-privacy product this is the kind of claim that has to be true at GA or removed.

**(B) Cryptographic strength.** The home-rolled cipher is weaker than FF1 in concrete, demonstrable ways:
- **Single-character values degrade to a keyed Caesar shift** — the shift depends only on (key, tweak), so **one known original→masked pair breaks the entire column**.
- **8 rounds** vs FF1's 10 (fewer rounds = weaker mixing).
- **Small domains are unsafe** — below SP 800-38G's ~1M-value minimum, any format-preserving cipher leaks; ours has no floor and will happily "encrypt" a domain of 10.

**(C) Silent-failure bugs — the dangerous ones.** These are not "weak crypto," they are **data-integrity and silent-disclosure defects**, and they are must-fix regardless of what we decide about strength:
- **Non-round-trip corruption (undetectable).** A value made entirely of out-of-charset characters routes to a one-way per-position HMAC whose output is all *in-charset*; unmask then runs the inverse cipher on it and silently returns a **wrong value** — and can't even tell it's wrong. Verified repro: `'---'` → `'092'` → `'858'`.
- **Silent raw passthrough (PII leak, no warning).** Several paths emit the **original value in the clear** with empty warnings:
  - out-of-charset characters retained under `preserve_separators=True`,
  - whole-value no-op under `preserve_separators=False` (the V2 path, unlike legacy V1, does not even warn),
  - **checksum-scheme short values**: NPI < 10 chars, ISBN-13 < 13, VIN < 17 each `return` the input **unchanged, silently**.

Cluster (C) is the one that can put unmasked SSNs into "masked" output. It gets fixed in every option below.

## 4. Two sub-decisions that every "real fix" has to answer

These recur inside the options, so pull them out:

1. **What do we do with a value we *can't* strongly encrypt?** (sub-minimum domain, too short, wrong charset.) Three choices: **(i) fail closed** — reject the column/job with a clear error; **(ii) vault-token it** — replace with a random token stored in the encrypted sidecar (we already have the vault), which is reversible *only with the sidecar* (a semantics change for those values); **(iii) leave it weak/raw** — this is the current bug, not an option.
2. **Separators.** Keeping the dashes in `123-45-6789` is *intended* product behavior (`preserve_separators`). Keeping arbitrary out-of-charset data in the clear is a *leak*. The fix is a **declared-separator allowlist**: fail closed on any out-of-charset character that isn't a declared separator, so we can be strict without breaking dashed-SSN.

## 5. Options

| | What ships | Strength | Honest? | Effort | New dep? |
|---|---|---|---|---|---|
| **A. Real FF1, in place** | Swap the Feistel for an audited FF1; keep in-place reversibility | Strong (for admissible domains) | Yes | High | Yes |
| **B. Keep home-rolled, harden + relabel** | Bump rounds ≥10, fix single-char, fail closed everywhere; rename away from "NIST FF1" | Better, still home-rolled | Yes (relabeled) | Medium | No |
| **C. Real FF1 + vault fallback** | FF1 where the domain qualifies; vault-token sub-minimum/short values; fail closed on undeclared out-of-charset | Strong + safe everywhere | Yes | High | Yes |
| **D. Contain + relabel for GA (defer real FF1)** | Fix all cluster-(C) bugs → fail closed; correct the "NIST FF1" language; real FF1 becomes fast-follow | Unchanged (but honest + safe) | Yes (relabeled) | Low | No |

Notes:
- **Every option fixes cluster (C)** (the silent-failure bugs). They differ on strength and timing.
- **A and C both still need sub-decision #1** — FF1 has a hard minimum domain, so short identifiers can't be FF1'd; you still choose fail-closed vs vault for those. C just makes that choice explicit (vault) instead of leaving a gap.
- **B and D keep a custom cipher** but stop calling it NIST. D is A/C's cluster-(C) fixes *without* the FF1 swap — i.e. D is the safe subset you'd ship first anyway.

## 6. Recommendation

**Phased: ship D now, land C as the fast-follow — with the cluster-(C) fixes and the relabel being non-negotiable for GA.**

1. **Now (GA-blocking, do regardless):** fix every cluster-(C) path to **fail closed** with a clear error (no silent raw passthrough, no non-round-trip), add the **declared-separator allowlist**, and **correct the product language** so we stop claiming NIST FF1 until it's true. This alone removes the "unmasked PII in masked output" and the false-claim risks — the two things that actually block GA.
2. **Fast-follow (post-GA-blocker, still pre-GA if time allows):** adopt an **audited FF1** for admissible domains and **vault-token** the sub-minimum/short values (Option C). This makes the "NIST FF1" claim true and gives real strength, with the vault covering the values FF1 structurally can't.

Why not A/B/C *right now*: the FF1 swap is real work (dependency vetting, determinism + round-trip re-validation against the golden gate, and the vault-semantics change for short identifiers), and none of it is what makes the current state *dangerous* — the danger is cluster (C), which D fixes immediately. Sequencing D→C ships safe fast and honest, then upgrades strength without a rushed crypto swap.

**The decision I need from you:** are you good with **D-now / C-fast-follow**, or do you want the **full FF1 swap (C) as a single GA-blocking piece of work** before we cut the release? And for the values FF1 can't cover (short/sub-minimum identifiers): **fail closed** (reject and make the user pick a different strategy) or **vault-token** (reversible only via the encrypted sidecar)?
