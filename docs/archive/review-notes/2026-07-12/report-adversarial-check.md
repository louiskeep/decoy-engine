# Adversarial Check of the Architecture Review

**Reviewed report:** `docs/adversarial-architecture-review-2026-07-12.md`
**Report snapshot:** 647 lines, SHA-256 `4a55102c98f20e09c2cd742bbb3478f1a26346dcef03fa6189747e179bf6b23c`
**Code baseline:** `c1c4f2c2b33af39e1de4874788a7df78a352970c`
**Disposition:** revise before treating the report as the release decision record

## Findings

### HIGH - DE-04 does not establish the threat precondition needed for CRITICAL

The deserialization mechanism is proven: `classify_fields(..., pack_dir=...)` accepts a path,
unsigned packs are accepted by default, and `joblib.load` can execute Python. The report does not,
however, show that a less-trusted actor can control `pack_dir`, `manifest.json`, or `model.joblib`
across a privilege boundary. The reviewed engine is an in-process Python library; its direct caller
already executes Python with the same privileges. The loader also explicitly defines the artifact as
locally generated and trusted, and supports fail-closed signature enforcement. The platform path
that could turn this into remote or cross-tenant code execution was outside the review.

**Required report edit:** replace the unconditional CRITICAL statement with one of these:

- Demonstrate the platform/CLI dataflow by which an untrusted tenant, job author, archive, or
  writable lower-privilege directory controls the selected pack. Retain CRITICAL if that boundary
  exists.
- Otherwise rate this HIGH: unsafe-by-default artifact provenance with latent code execution if an
  untrusted pack crosses the documented trust boundary.

Keep the joblib warning and synthetic execution probe. Add the missing preconditions: attacker can
write both adjacent files, influence pack selection, and cannot already execute arbitrary code as
the engine caller. Do not describe the adjacent hash as authentication. Also acknowledge that the
existing per-instance HMAC is meaningful for the repository's stated single-host threat model even
though it is not publisher identity for a distributed wheel.

### HIGH - DE-07 mixes dirty-checkout evidence into the frozen-baseline record

The report places `uv build` under "Frozen revision checks" but the 49 MB/3,116-entry sdist includes
post-baseline untracked review files, caches, and local worktrees. That artifact characterizes the
dirty shared checkout, not commit `c1c4f2c` alone.

I exported `c1c4f2c` to a clean temporary tree and ran:

```text
uv build --out-dir /tmp/decoy-clean-dist
sdist: 25 MB, 1,003 entries
wheel: 1.1 MB, 377 entries
```

The clean sdist contained no `.claude/worktrees`, Hypothesis cache, adversarial review, or review-note
paths. The non-hermeticity finding remains valid because Hatch's default sdist selection includes
all files not ignored by VCS, but the report must label the 49 MB result as a dirty-workspace probe,
not a frozen-revision artifact. Split DE-07 into:

1. dirty-workspace-dependent sdist membership, remediated by an allowlist and clean release CI; and
2. installed-wheel contract failure, which is independently confirmed: the clean wheel still omits
   `py.typed`, `model.joblib`, and the model manifest.

### HIGH - DE-12 is labelled as sampled, and HIGH impact is not demonstrated

"Unlabelled head sample" is factually too broad. Baseline source explicitly documents first-N reads,
sets `ColumnProfile.sampled`, preserves the total row count, and carries `row_count_exact`; it also
stores `Profile.profile_seed`. What is missing is the **sampling method and coverage provenance**, not
all sampling labels. In addition, the concrete uniqueness-saturation use cited at
`_pipeline_routing_signals.py:418-434` sits behind `use_probe_routing=False` by default.

**Exact suggested wording:** "Bounded file/cloud profiling marks statistics as sampled but does not
record that the sample is a deterministic head sample; the stamped seed does not affect those
first-N reads."

Downgrade to MEDIUM unless a default-on route, admission check, or publication decision is shown to
produce one of the report's HIGH outcomes. Keep the representative-sampling remediation.

### HIGH - DE-15's immutable-plan remedy conflicts with a runtime governor

"Execute it without re-deciding" and `explain == selected == executed` are not feasible invariants
for a governor whose purpose is to react to measured RSS, source failures, or route OOM and move down
a fallback ladder. They also conflict with the post-baseline Track B work described in the report.

**Exact suggested edit:** require one authoritative **execution decision record/state machine** with
an immutable initial plan plus append-only runtime transitions. Verify `explained initial route ==
first attempted route`, and record `attempted routes`, `transition reason`, `measured budget/RSS`, and
`final executed route`. Do not require the initial and final route to be identical.

### MEDIUM - DE-01's "no unchanged data-bearing position" gate is invalid

The fail-open mechanism is real: out-of-charset characters are copied deliberately, false separator
mode can return the whole input, and the covering fallback is not reversible. But a sound
format-preserving permutation may coincidentally emit the same character at one or more positions.
Position equality alone is not evidence of passthrough, and standard FF1 does not promise that every
character changes.

**Exact suggested verification edit:** replace "no unchanged data-bearing position" with:

> No out-of-domain value is admitted; every admitted value round-trips exactly; mixed-alphabet
> inputs either match a complete typed domain or fail before output; instrumented/structural tests
> prove that every admitted character participates in the permutation rather than being copied by
> the separator branch. Whole-value fixed points are handled according to an explicitly documented
> policy, not inferred from per-position equality.

Known-answer and cross-implementation vectors remain appropriate. Cite the current SP 800-38G Rev. 1
draft/domain-size guidance in addition to the 2016 final publication.

### MEDIUM - DE-02 assigns deployment key ownership to the library

The 64-bit seed/key conflation is confirmed and remains release-blocking. The proposed requirement
for a `KMS/HSM mask_key_ref` resolved "inside the worker" is too specific for this repository's
documented boundary: the engine is an in-process library with no network/auth surface, while the
platform and CLI own secret retrieval.

**Exact suggested edit:** the engine must require a separate 256-bit masking key or opaque
`KeyProvider`/resolver for keyed strategies, accept only key bytes plus stable key/version IDs at the
execution boundary, domain-separate purpose keys, and fail before writes when unavailable. KMS/HSM
storage, tenant authorization, rotation orchestration, and low-privilege worker isolation are caller
or platform requirements. Do not make an engine network dependency part of the minimum fix.

Apply the same ownership correction to DE-04's "constrained low-privilege process": the engine can
provide a safe loader/verifier interface, but process isolation belongs to its host unless a
subprocess becomes an explicit engine feature.

### MEDIUM - DE-03 contains a broken evidence citation

`tests/perf_fixtures/fk_relational.py:241-245` does not contain the claimed intentional passthrough
statement at `c1c4f2c`; those lines define `FkFixture.graph` and begin `build_fk_relational`.

**Exact suggested replacement:** cite the fixture's wide unplanned payload construction at
`tests/perf_fixtures/fk_relational.py:53-56,88-99,143-176`, together with the adapter output-copy
sites and a retained executable probe. Better still, add a focused regression test dedicated to an
undeclared sensitive column rather than using a performance fixture as policy evidence.

The raw-column behavior itself is confirmed. NIST SP 800-188 and OWASP allowlist guidance support the
general risk posture, but neither source specifically mandates Decoy's proposed unknown-column API;
present closed-schema defaulting as the review's architectural conclusion, not as a quoted standard
requirement.

### MEDIUM - DE-08 promises atomicity without defining a transaction domain

The quarantine-before-sink-commit sequence is confirmed. A single atomic transaction across tables,
quarantine, vault, evidence, and manifests is not generally implementable when artifacts may target
different filesystems, object stores, or external sinks. Atomic rename only works inside a compatible
storage boundary.

Revise the remedy to define a run-scoped transaction domain and protocol: stage under one root where
possible, publish artifacts first, publish a signed success manifest/commit marker last, and treat
artifacts without that marker as uncommitted and subject to cleanup. For heterogeneous sinks, require
idempotency and compensation rather than claiming physical all-or-none atomicity. Also decide
explicitly whether failed-run quarantine is a protected diagnostic artifact or must be destroyed.

### MEDIUM - DE-06 combines distinct ownership questions into one HIGH finding

`sink` being silently ignored on full-frame/auto-chunk paths and the unreachable post-validation path
are concrete runtime contract defects. `subset`, `targets`, and `run_storm` are different: repository
guidance says this package is library code and callers own CLI/platform orchestration; `run_subset`
is deliberately a separate public operation, and target descriptors can be caller metadata.
Moreover, the assertion that STORM is post-publication is a platform-runtime claim despite the
report's platform coverage limit.

Split the finding into (a) runtime arguments/fields that claim execution ownership but are ignored,
and (b) externally owned orchestration fields whose ownership is undocumented or misplaced. Retain
HIGH only for a demonstrated false-success, publication, or memory consequence.

### MEDIUM - DE-14 understates the advertised Python surface and misstates collection

`pyproject.toml` has `requires-python = ">=3.10"` with no upper bound. Testing only 3.10-3.12 would
still leave advertised 3.13+ installations unowned. Either cap metadata (for example `<3.13`) or add
every supported interpreter to the installed-wheel matrix.

The phrase "its one collected measurement always passes" is also inaccurate.
`tests/benchmark/test_arrow_to_pandas_conversion.py` defines a four-case parametrized measurement
plus a summary test. The valid criticism is that the measurements assert shape/column equality but
have no performance-regression threshold. State that directly.

### LOW - The Graphify conclusion is not reproducible from the evidence directory

The architecture directory says a cached Graphify graph found no import cycles, but no graph,
`GRAPH_REPORT.md`, query output, command/version, or commit-bound cache is retained under the evidence
directory. Remove the claim or preserve enough material to reproduce it. Also qualify it as cycles
detectable by that extractor; it is not proof that Python has no dynamic/import-time cycles.

### LOW - DE-18's typed-union remedy must preserve provider extensibility

Forbid unknown fields on built-in generator variants, but do not replace an extensible provider
surface with a completely closed discriminated union. Use typed built-in variants plus an explicit
namespaced extension/provider-config object. The independent `generation_id` proposal is sound; its
default and migration behavior must say whether identical configs intentionally share streams.

## Confirmed Core

The corrections above do not invalidate the report's main release concern. Direct baseline reading
confirmed the substance of DE-01, DE-02, DE-03, DE-05, DE-08, DE-09, DE-10, DE-11, DE-13, DE-16,
DE-17, DE-18, DE-19, and DE-20. In particular:

- masking/vault key material is derived from the eight-byte reproducibility seed;
- undeclared input columns are emitted unchanged;
- planned tables missing from the execution mapping are silently skipped;
- public baseline out-of-core loader resolution eagerly retains all loaded tables;
- pandas FK assignment can widen and round integers above `2**53`;
- top-level `pool_size` is checked at compile time but omitted from `ColumnSeed.provider_config`;
- the installed wheel omits claimed typing/model resources; and
- the uncommitted Track B branch is correctly treated as post-baseline only. Its direct public
  `source_loader` route remains eager.

## Review Limits

This check traced the disputed claims against an exported `c1c4f2c` tree and independently rebuilt
clean wheel/sdist artifacts. It did not repeat the report's 6,281-test run, every synthetic probe,
cloud integration, large RSS benchmark, or platform/CLI runtime trace. Those remain residual evidence
limits, especially for DE-04 severity and end-to-end atomicity.
