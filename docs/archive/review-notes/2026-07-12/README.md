# Adversarial Review Evidence Directory - 2026-07-12

This directory preserves the scoped evidence used by
`docs/adversarial-architecture-review-2026-07-12.md`. Existing reviews and plans were treated as
leads; the findings here were checked against source or a focused executable counterexample.

## Notes

| File | Owner | Scope |
|---|---|---|
| `execution-and-integrity.md` | delegated execution reviewer | Pipeline flow, route behavior, FK integrity, profiling, memory, transactionality, and generation semantics |
| `contracts-tests-operations.md` | delegated contracts reviewer | Public API, packaging, version evidence, CI, performance gates, extras, typing, and release mechanics |
| `lead-security-and-config-contracts.md` | lead reviewer | Cryptography, FPE, model-pack loading, schema closure, source binding, configuration ownership, and focused reproductions |
| `report-adversarial-check.md` | independent adversarial reviewer | Claim-by-claim severity, threat-precondition, citation, remediation-feasibility, and clean-artifact challenge of the assembled report |

The configuration-trace delegate exhausted its execution allowance before it could persist a
standalone note. The lead reviewer independently repeated that trace and recorded it in
`lead-security-and-config-contracts.md`. The execution and contracts delegates wrote their notes
before their final response allowance expired; their files are retained as the primary record of
those slices.

The independent check confirmed the three critical engine findings, downgraded model-pack loading
to HIGH until an untrusted platform selection boundary is demonstrated, independently rebuilt a
clean exported revision, and corrected several over-broad verification/remediation claims. The main
report incorporates those corrections; the original challenge note is retained for auditability.

## Revision Boundary

- Behavioral review baseline: `1e80016bdbf1aa100f7f21215c919df4c69f41b1`.
- Frozen reviewed revision: `c1c4f2c2b33af39e1de4874788a7df78a352970c` on `main`.
- The only intervening source changes were typing annotations in the isolated-run modules; the
  other changes pinned pandas below 3 and widened mypy strict-module coverage. None changed a
  finding's behavioral evidence.
- Full frozen-revision regression result: `6281 passed, 38 skipped, 13 deselected`.
- Final-revision static gates: ruff check passed, ruff format check passed, mypy passed for 375
  source files.

After those gates completed, the shared checkout moved to a Track B implementation branch that
changes out-of-core/isolation behavior. The changes were initially uncommitted during review and
were later committed/pushed as `9244847`. They are post-baseline remediation, not evidence used to
close a finding; see the main report's concurrent-work notice.

## Evidence Handling

All counterexamples used synthetic strings and temporary directories. No production connector,
cloud account, external database, or real personal data was accessed. The review changed only
documentation under `docs/`.
