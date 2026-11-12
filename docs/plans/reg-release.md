# REG-RELEASE: stamp the reason registry and the CHANGELOG at release time

Status: E1 built in engine #450 (`5c24a75e`); E2 ready to build

Rules consulted: 00-universal, risk-and-exceptions, development-loop (including Proportion),
local-operations, workspace-and-vcs, reuse-first, exploration, scope-discipline, verification,
code-review, testing, architecture, cross-cutting-concerns, api-and-compatibility,
data-and-migrations, release-and-rollback, documentation, catalogs/recurring-defects

Planning base: engine main `79d5ded7`. E2 was re-derived from `origin/main` at `c96e2855` on
2026-11-11. Before building, fetch and rebase on the then-current `origin/main`, read every cited
path with `git show origin/main:<path>`, and update the ceiling if another numeric registry minor
has landed. Risk: R2 because E2 changes a frozen compatibility contract, its release pin, and
release tooling.

**Guarantee.** A normal PR declares every new reason with `since=UNRELEASED`, may declare only the
two accepted pending edits to a numeric reason, adds one changelog fragment, and does not claim a
release version. Release prep selects the one registry transition below, stamps all pending facts,
writes a minor fixture only when identities move from `UNRELEASED`, rebuilds the consumer contract,
recaptures the release pin, and uses E1 to compile changelog fragments. Before and after stamping,
the pin and fixture gates reject undeclared changes to numeric reasons and a new reason with no
fragment.

**Non-goals:** changing the reason-manifest shape except for the development-only `"unreleased"`
value; changing what a registry minor means to consumers; changing the CLI CHANGELOG; changing the
package-version source in `pyproject.toml`; rebuilding E1.

## Current-main facts and built dependency

- `REGISTRY_VERSION` is `1.23.0` at `src/decoy_engine/reasons/_registry.py:24`; `ReasonSpec.since`
  and `deprecated_in` are plain strings at `src/decoy_engine/reasons/_types.py:81-94`; `manifest()`
  emits them at `_registry.py:152-184`.
- The last shipped pin, `tests/fixtures/f29/reason-registry-release.json`, is the v1 aggregate pin
  for `1.22.0`. It contains numeric buckets through `1.22.0`. Main also has the three GEN-TYPES
  identities and fixture at `1.23.0`, documented at `docs/reasons.md:21` and `:50`.
- The policy choke point is `scripts/release/reason_manifest_policy.py`. Current relevant functions
  are `append_only_violations` at `:45`, `release_pin_violations` at `:95`,
  `since_floor_violations` at `:119`, `since_window_violations` at `:130`, fixture comparison at
  `:159-222`, `restamp` at `:263`, fixture naming at `:286-300`, and renumber policy at `:303-389`.
- The current pin writer and checker are aggregate based at
  `scripts/release/reason_release_pin.py:44-84`. The minor-fixture gate has a fixed historical floor
  of `1.5.0` at `tests/sentry/test_reason_minor_fixtures.py:21-33`.
- Current renumbering is numeric-to-numeric. Its rewrite surface and anchors are at
  `scripts/dev/registry_renumber.py:51-79`, token and planned rewrites at `:82-171`, candidate-copy
  verification at `:270-318`, and CLI at `:329-348`. The isolated extractor that preserves
  branch-owned consumer-contract facts is `scripts/dev/_registry_renumber_git.py:22-91`.
- E1 is built and is not in E2 scope. Engine #450 added the strict shared fragment grammar and
  changelog compiler in `scripts/release/stamp_release.py:34-107` and `:250-310`, the merge-base
  fragment check in `scripts/dev/check_fragment.py:30-104`, the uncompiled-fragment refusal in
  `scripts/release/release_checks.sh:36-39`, and the CODEMAP rows at `CODEMAP.md:256-258`. E1's
  acceptance tests AT-6, AT-7, and AT-9 are already built. E2 extends these choke points and does
  not replace their parser, ordering, transaction, workflow, or helper.

## Established solutions and reuse

Research refreshed 2026-11-11: Towncrier keeps per-change fragments, accepts the release version
at build time, previews without writing, then compiles and removes fragments at release; Scriv
commits `changelog.d` fragments with code and later collects them into a versioned, dated entry.
Rust represents an upcoming stable version as `Current` and carries a dedicated version placeholder
until release. Sources: [Towncrier tutorial](https://towncrier.readthedocs.io/en/stable/tutorial.html),
[Scriv concepts](https://scriv.readthedocs.io/en/latest/concepts.html), and
[Rust `StableSince`](https://doc.rust-lang.org/stable/nightly-rustc/rustc_attr_ir/stability/enum.StableSince.html).
E2 follows those established fragment and pending-version patterns. Custom code remains necessary
because Decoy must preserve its existing reason-manifest fixtures, per-field freeze rules, exact
CHANGELOG grammar, and branch conversion without adding a dependency. Reuse E1's parser, compiler,
clean-tree guard, planned output, replace and rollback functions; reuse the existing policy, fixture
naming, extractor, contract builder and token-boundary machinery named above.

## Behavior and frozen contracts

### Development declarations

- Add `UNRELEASED = "unreleased"` to `src/decoy_engine/reasons/_types.py` and export it from
  `decoy_engine.reasons`. New catalog entries use the constant, not a second literal. Catalog files
  may import it from the sibling `_types` module, consistent with existing same-package imports.
- `manifest()` reports `"since": "unreleased"` for a pending identity. `registry_version` stays at
  the allowed numeric version. An older consumer that sees an unknown identity in a development
  build continues to degrade to `decoy.unknown_reason_from_future`; no released manifest contains
  `"unreleased"`.
- A code-adding PR does not edit `REGISTRY_VERSION`, a historical minor fixture,
  `consumer-contract.json`, or the version sentence in `docs/reasons.md`. It adds a valid E1
  fragment that names every new identity.
- The one identity spelling in fragments is a Markdown code span containing
  `namespace/code`, with the live manifest's namespace and code bytes on each side of one ASCII
  slash. For example, identity `("config", "generate_output_type_unsupported")` is named only by
  `` `config/generate_output_type_unsupported` ``. Matching is case-sensitive and compares the
  whole code-span payload to the exact pending-identity set. Plain prose, a dotted spelling, a
  prefix such as `` `config/generate_output_type_unsupported_extra` ``, a suffix, whitespace
  inside the span, or a span with more than one slash does not name that identity. A pending
  identity may be named in more than one fragment, but every pending identity must have at least
  one exact match. The extraction of these claims lives beside the pending-identity policy in
  `reason_manifest_policy.py`; E1's `parse_fragment()` remains the sole fragment grammar and the
  checker passes only each already-parsed `Fragment.block` to that extractor.

### Numeric ceiling and fixtures

- Add `LAST_PER_PR_MINOR = "1.23.0"` to `reason_manifest_policy.py`. It is the last minor created by
  the old per-PR process, not a moving release version. `allowed_registry_version(pin)` is
  `max(pin.registry_version, LAST_PER_PR_MINOR)`. On the E2 build base that is
  `max(1.22.0, 1.23.0) == 1.23.0`, and live `REGISTRY_VERSION` must equal it.
- E2 adds no `reason-manifest-v1.24-*.json` fixture because E2 adds no reason identity. Existing
  fixtures through `reason-manifest-v1.23-gen-types.json` remain historical facts.
- If a numeric `1.24.0` or later minor lands before E2 merges, do not raise a test cap merely to
  pass. Rebase E2, rerun the legacy pin and fixture gates, set `LAST_PER_PR_MINOR` to that new live
  ceiling, update the migration assertions, and rerun all E2 tests and mutations on the new head.
- `minor_fixture_violations()` ignores live `UNRELEASED` identities. For numeric identities it keeps
  the fixed `1.5.0` floor and one-fixture-per-minor rule. `fixture_entry_view()` removes only
  `summary`, `deprecated_in`, and `replaced_by` from both the historical fixture entry and live
  entry. The v2 release pin freezes those three fields. Every other fixture field remains equal.
- A release with new identities advances from the allowed version to its next minor and writes one
  fixture containing exactly the identities stamped in that transition. The name is
  `reason-manifest-v<registry-major>.<registry-minor>-release-<package-version-slug>.json`.
  `release_fixture_slug()` lowercases the `stamp_release.py --version` value, replaces each run
  outside `[a-z0-9]` with one hyphen, strips edge hyphens, and requires the result to round-trip
  through the existing `fixture_name()` and `parse_fixture_name()` functions. For example, a
  package release `0.23.0` that stamps pending identities from this base writes
  `reason-manifest-v1.24-release-0-23-0.json`.
- A patch-only or no-op registry transition writes no minor fixture. Release prep still recaptures
  the v2 release pin at the numeric registry version the package will ship. This matters on current
  main: a release with no E2-era pending facts must move the pin's shipped version from `1.22.0` to
  live `1.23.0` even though `REGISTRY_VERSION` itself does not change.

### Accepted edits to numeric reasons

Owner decision 2026-11-11: accepted and frozen as contract. A numeric reason may change only as
one of these declared pending edits, stamped at release, with every other field frozen:

1. A first deprecation sets `deprecated_in=UNRELEASED` together with `replaced_by`. It is legal only
   when the v2 pin proves both fields were null. An existing numeric tombstone cannot be retargeted.
2. An editorial summary correction lists the identity in `PENDING_SUMMARY_EDITS` in
   `src/decoy_engine/reasons/_registry.py`. Expose a read-only `pending_summary_edits()` accessor
   through `decoy_engine.reasons`; release tooling must not import the private registry module.

One pending-edit descriptor in `reason_manifest_policy.py` owns the conditional field allowances.
The pin writer, pin checker, stamp, release checks, and tests consume it. The stamp converts a
pending deprecation to the selected numeric version, keeps the edited summary, clears
`PENDING_SUMMARY_EDITS`, and then recaptures the pin. No caller owns a parallel field allowlist.

### Versioned release pin

- Migrate `reason-registry-release.json` to strict format version 2. Its root is an object with
  exactly five keys: `format_version`, `registry_version`, `baseline_ceiling`, `identities`, and
  `since_counts`. `format_version` is the strict integer `2`; the two versions are strict strings;
  `identities` is a JSON array; and `since_counts` is an object from a numeric version string to a
  strict non-negative integer. It counts the numeric `since` values of exactly the captured
  identity records, so its values sum to the array length. Each identity array item is an object
  with exactly `namespace`, `code`, and `fields`. `namespace` and `code` are separate non-empty
  strict strings and encode the identity without a joined or escaped form. The policy checker
  requires each tuple to equal one numeric live-manifest identity; the loader itself does not add
  a second identity regex beside the registry.
  `fields` is an object with exactly the eleven manifest-entry field names shown below. Its values
  are lowercase 64-character SHA-256 hex strings. Identity items are unique and sorted by the
  `(namespace, code)` string tuple. There is no count in place of the identities array and no
  parallel baseline container.
- This is the normative one-entry shape and exact writer output after removing the two spaces of
  Markdown list indentation. `test_reason_release_pin.py` holds the resulting bytes plus the final
  newline as a golden, and the migrated checked-in pin is the full-catalog golden fixture.

  ```json
  {
    "format_version": 2,
    "registry_version": "1.22.0",
    "baseline_ceiling": "1.23.0",
    "identities": [
      {
        "namespace": "config",
        "code": "example",
        "fields": {
          "code": "2e23250164957d519115f2dd253384e487806e87cb3557eb1372b937dff93959",
          "context": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
          "deprecated_in": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
          "kind": "3045b5c998d76c75f480b2e91388b6adef07205004129c0b8dffee745b4aa77e",
          "namespace": "48882ab48bfdf3bef2fe65e3304e91ccfd8f74e357c183367ad6fe903fe0e697",
          "owner": "8f92c9a8acd15b4719b1d1e3ace2a7d07618948904ae2ebc4c99f4f18fe02281",
          "replaced_by": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
          "rules": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
          "since": "dd89545194911b5b0e3ddbc7285adf096b7196163c2fbf42e2a382cab8fc6c23",
          "summary": "cc6db7b8b4b774cc4f1743881776cb076e906271caa9dd3958db760598b9c304",
          "symbol": "81d3380871cf9d1c4db281c5f5f051a708922e920e0e8d0b80d40fc9fa3b0b2d"
        }
      }
    ],
    "since_counts": {
      "1.23.0": 1
    }
  }
  ```

  `canonical-json(value)` is exactly
  `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")`.
  A field digest is `hashlib.sha256(canonical-json(entry[field])).hexdigest()`. Object keys are
  sorted recursively and array order is preserved, so nested `rules`, `context`, and
  `replaced_by` have one digest on every supported interpreter. The writer constructs root keys in
  the order in the example, sorts identity records by `(namespace, code)`, sorts every `fields`
  object's keys lexically, sorts `since_counts` by `packaging.version.Version`, and emits exactly
  `json.dumps(pin, indent=2, ensure_ascii=True) + "\n"`. The loader treats JSON object order as
  insignificant, but the checked-in golden pins the writer's bytes.
- The loader rejects missing or extra roots, unknown or missing format versions, duplicate
  identities or fields, empty identity parts, inconsistent `since_counts`, booleans where integers
  are required, numeric strings, free-form field names, malformed digests, and wrong container
  types. Messages name identities and fields only, never catalog values.
- `registry_version` means the registry version in the last package release. The one-time E2
  migration preserves `1.22.0`. `baseline_ceiling` means the greatest numeric `since` captured by
  the field baseline, not the live registry version. The E2 migration sets it to `1.23.0` and
  captures every numeric live identity through that ceiling. Every later writer computes it as
  `max(Version(entry["since"]))` over the numeric identities actually written. Therefore a
  summary-only or deprecation-only release at live `1.23.1` keeps `baseline_ceiling` at `1.23.0`;
  a release that stamps new identities at `1.24.0` advances it to `1.24.0`. An empty numeric
  baseline is invalid.
- The field baseline covers every manifest-entry field. Identity membership is checked separately.
  The pending descriptor can waive only the one summary hash or the two initially-null deprecation
  hashes described above. New `UNRELEASED` identities are absent from the pin and allowed until
  stamping. Numeric identities absent from the baseline, deleted identities, or drift in any other
  field fail.
- Before replacing v1, the migration runs the existing v1 aggregate check and full historical
  minor-fixture check against the same `c96e2855` tree. It refuses without writing if either is red.
  Only then may it write v2 and switch fixtures to `fixture_entry_view()`. This prevents the format
  migration from laundering an existing mismatch.
- `reason_release_pin.py --check` calls
  `release_pin_violations(pin, live, pending_summary_edits())`; it does not compare the checked-in
  pin with a freshly generated pin. `--write` is release-only and captures only numeric entries.
  `since_floor_violations`, `since_window_violations`, and every other `Version(since)` caller skip
  or explicitly classify `UNRELEASED` before invoking `Version`.

### One transition table and release stamp

`registry_transition(live, pin, pending_summaries)` in `reason_manifest_policy.py` is the only
decision point for the stamp, release checks, migration check, and tests:

| Pending state | Registry result from current allowed `1.23.0` | Minor fixture |
|---|---|---|
| One or more new identities, with or without pending edits | `1.24.0` | Write one |
| Deprecation only, with or without summaries | `1.23.1` | None |
| Summary only | `1.23.1` | None |
| Nothing pending | Unchanged `1.23.0` | None |

A later identity release after a patch advances to the next minor, such as `1.23.1` to `1.24.0`.
Explicit `--registry-minor X.Y.0` is accepted only when it equals the automatic identity transition.
Patch-only and no-op states require `auto`.

Extend E1's `stamp_release.py --version X.Y.Z` with `--registry-minor auto|X.Y.0`, default `auto`:

1. Require a clean union of E1's owned changelog paths and every registry path the stamp can write.
   Compute the changelog plan and registry plan without writing the real tree.
2. Materialize one candidate tree, apply source-token rewrites only to actual `since=UNRELEASED`
   and `deprecated_in=UNRELEASED` arguments, set `REGISTRY_VERSION`, clear pending summary markers,
   remove now-unused `UNRELEASED` imports, create the minor fixture only for a minor transition,
   rebuild `consumer-contract.json` through `build_contract()` at
   `tests/unit/progress/test_consumer_contract.py:132-145`, update `docs/reasons.md`, recapture v2,
   and apply E1's changelog compile plan.
3. Run the complete policy, pin, fixture, contract, docs-anchor, and fragment postconditions against
   the candidate. A string `"unreleased"` in a docstring, summary, or unrelated source expression
   is untouched. Only after all checks pass, apply the same planned writes and deletions to the real
   clean tree through E1's one replace/rollback mechanism. A failure restores tracked owned paths
   and removes a newly created fixture. There is no resume path.

Add `reason_release_pin.py --release-ready`, which loads the live manifest plus the public pending
summary accessor and returns the central policy's pending-state violations. `release_checks.sh`
invokes it in the lock-built venv after the ordinary pin check and fails when pending codes, a
pending deprecation, or `PENDING_SUMMARY_EDITS` remain. It retains E1's pre-`uv` uncompiled-fragment
refusal. `cut-release.sh` already calls this script at `scripts/release/cut-release.sh:99-105`, so
it gains the registry refusal without a second shell implementation.

## In-flight conversion

After E2 merges, an in-flight branch first merges current main, then runs:

```text
python scripts/dev/registry_renumber.py --to-unreleased --main origin/main
python scripts/dev/registry_renumber.py --check --main origin/main
```

`--to-unreleased` compares the branch to main and accepts exactly one branch-claimed numeric minor.
It sets the branch's `REGISTRY_VERSION` and `docs/reasons.md` anchor back to main's allowed version,
rewrites branch-added reason declarations to `since=UNRELEASED`, inserts the sibling `_types`
import when needed, rewrites branch-added numeric carriers, and deletes the branch-added minor
fixture. The exact replacement depends on the surface:

| Branch-added surface | Required replacement bytes |
|---|---|
| Catalog declaration argument | Unquoted identifier `since=UNRELEASED`; `deprecated_in` is outside this conversion's supported workflow |
| Python expected manifest data or other Python string carrier | Quoted lowercase string `"unreleased"`, preserving the file's existing quote style |
| JSON carrier other than a `registry_version` anchor, including a branch-owned consumer-contract fact | JSON string `"unreleased"` |
| Markdown docs, records, and `changelog.d/*.md` | Lowercase text `unreleased` in the original delimiters, so `` `1.24.0` `` becomes `` `unreleased` `` and a quoted version remains quoted |
| `REGISTRY_VERSION` and the `docs/reasons.md` registry anchor | Main's numeric allowed version, never either unreleased spelling |
| Branch-added numeric minor fixture | Delete the file; do not rewrite its contents or name |

No emitted carrier contains the uppercase string value `"UNRELEASED"`, and no catalog declaration
uses the quoted literal `since="unreleased"`. `UNRELEASED` is a Python source constant only;
`"unreleased"` is its stored and displayed value. The tool does not touch historical fixtures, the
v2 release pin, or main-owned lines.

`--to-unreleased` supports identity-addition branches only. It refuses before candidate creation
if the branch edits any identity present on main, including a first deprecation, a marked summary
correction, or either edit combined with a new identity. Those shapes retain the existing
`edited-existing <identity>` refusal; an edit-only numeric bump also retains
`version-without-identity`. Supporting those workflows would require distinguishing and rewriting
existing-identity fields, so E2 does not add that second conversion mode.

To migrate an in-flight edit-only or mixed branch safely, create a clean branch from E2-enabled
`origin/main`; do not run `--to-unreleased` and do not cherry-pick a commit that carries the old
numeric bump. Reapply only the intended allowed source edits: use
`deprecated_in=UNRELEASED` plus `replaced_by` for a first deprecation, and add a
`PENDING_SUMMARY_EDITS` marker for a summary correction. Reapply new identities with
`since=UNRELEASED` if the old branch was mixed, and add or update a valid fragment with the exact
identity code spans specified above. Do not copy the old branch's `REGISTRY_VERSION`,
`docs/reasons.md` version anchor, minor fixture, consumer contract, or release pin. Compare the new
branch's intended catalog diff with the old branch, then run ordinary pin, fixture, policy,
fragment, and `registry_renumber.py --check --main origin/main` checks. Before replacing the old
branch, compare the `REGISTRY_VERSION` assignment and `docs/reasons.md` version anchor directly
with `git show origin/main:<path>`; require no diff for the release pin or consumer contract; and
require `git diff --name-status origin/main...HEAD -- 'tests/fixtures/f29/reason-manifest-v*.json'`
to print nothing. This selective reapplication is the prescribed migration; release prep later
stamps it through the normal E2 transaction.

The conversion uses the existing hunk walk and token boundaries, applies to a temporary archive
first, then rebuilds `consumer-contract.json` from the converted branch with
`_registry_renumber_git.extract(..., contract=True)`. It never copies main's contract, so a
branch-owned contract fact survives. It runs `unreleased_violations`, identity-set equality,
contract equality, anchor checks, and import/lint checks before applying the same plan to the real
clean tree. A failed postcondition leaves the real worktree unchanged. `--check` becomes the
non-writing pending-era gate and rejects a branch numeric bump or branch-added minor fixture.

After the first release under E2, remove the old numeric-to-numeric mode in a separate cleanup;
E2 does not need to preserve it as an advertised workflow.

## Central choke points and design

- Registry state, pending-field allowances, ceiling calculation, fixture view, release-fixture slug,
  and the four transitions live only in `reason_manifest_policy.py`. Extend
  `tests/sentry/test_reason_policy_single_source.py`, whose AST walk and exact exemptions start at
  `:1-63`, so a local validator or transition branch in the stamp, pin CLI, shell check, or
  renumber tool fails. Named guard mutation: add a deprecation-only branch to `stamp_release.py`.
- `pending_summary_edits()` is the one registry-owned declaration surface. No script reads the
  private constant directly.
- Exact fragment identity claims are extracted once in `reason_manifest_policy.py` from E1-parsed
  fragment bodies. The fixture sentry and release postconditions consume that result; neither owns
  another identity spelling or Markdown parser. Guard mutation: make the fixture sentry accept a
  plain-text or prefix-collision claim.
- E1 remains the sole fragment grammar, compiler, file-replace, and rollback mechanism. E2 adds no
  second parser, writer, or transaction helper.
- No new Python module or package is needed. `tests/unit/test_registry_renumber.py` is 771 lines on
  `c96e2855`; all other currently scoped files are below 700 lines. The 771-line test file is a
  V2 cleanup item under the owner size rule, not a reason to move this acceptance coverage or raise
  an allowance. E2 must not grow a hand-written source file past the repository cap and must not
  raise a size or fan-out allowance.
- Recurring-defect checks: use `fullmatch` for every new token, slug, version, identity, and digest
  parser; strictly validate v2 stored primitives; do not surface raw exception text; do not import
  another package's private module; keep CI and shell failures blocking; run every mutation on the
  final head; do not raise pins or allowlists.

## Placement and E2 file scope

This table is against current `CODEMAP.md`. E1 already added the changelog and workflow rows. E2
adds no file outside an existing mapped location and no module to a flat package.

| E2 concern | Files | Current CODEMAP placement |
|---|---|---|
| Constant, pending declarations, public access | `src/decoy_engine/reasons/_types.py`, `_registry.py`, `__init__.py` | Reason codes, `CODEMAP.md:248` |
| Central policy and v2 pin | `scripts/release/reason_manifest_policy.py`, `scripts/release/reason_release_pin.py`, `tests/fixtures/f29/reason-registry-release.json` | Dev tooling under `scripts/release/`, `CODEMAP.md:254`; existing F29 fixture beside the pin |
| Registry stamp and tag refusal | `scripts/release/stamp_release.py`, `scripts/release/release_checks.sh` | Dev tooling under `scripts/release/`, `CODEMAP.md:254` |
| In-flight conversion | `scripts/dev/registry_renumber.py` | Dev tooling under `scripts/dev/`, `CODEMAP.md:254` |
| Policy and fixture guards | `tests/sentry/test_reason_policy_single_source.py`, `tests/sentry/test_reason_minor_fixtures.py` | Sentry test, `CODEMAP.md:252` |
| Unit and end-to-end acceptance | `tests/unit/test_reason_manifest_policy.py`, `tests/unit/test_reason_release_pin.py`, `tests/unit/test_registry_renumber.py`, `tests/unit/scripts/test_stamp_release_compile.py`, `tests/unit/scripts/test_release_checks_fragments.py` | Existing tests beside each owning tool |
| Durable instructions | `docs/reasons.md`, `CONTRIBUTING.md` | Existing reason contract and contributor guide |
| Required behavior fragment | `changelog.d/reg-release-e2.md` | Changelog fragments, `CODEMAP.md:256` |

Do not modify E1's workflow, `tests/_changelog.py`, fragment README, changelog compiler tests except
where the combined stamp transaction needs coverage, or `CHANGELOG.md` directly.

## Slice

E2 builds on engine #450 only, on branch `feat/reg-release-e2`. Its exact production, tooling,
fixture, test, docs, and fragment files are the Placement table above. There is no E1 build work in
this slice. The gate is the full-suite trigger below because E2 changes the frozen manifest contract
and migrates the release-pin format.

## Red-first E2 acceptance

Write each row red against current `c96e2855` behavior before implementing its source change.
AT-6, AT-7, and AT-9 belong to built E1 and are not repeated.

| ID | Acceptance test and fail-before evidence | Named mutation that must turn it red on the final head |
|---|---|---|
| AT-1 | In `test_reason_manifest_policy.py` and `test_reason_release_pin.py`, one `UNRELEASED` identity passes append-only, floor, window, fixture, and pin checks while live registry stays `1.23.0`. The strict v2 reader accepts only the five-root schema and exact identity and field records specified above, and rejects each missing or extra root or record key, unknown format, bool-as-int, numeric string, wrong container, malformed digest, empty identity part, duplicate or unsorted identity, unknown or missing field, and `since_counts` with a missing bucket, extra bucket, wrong count, or sum unequal to the identities length. A one-entry golden equals the normative JSON bytes above; the migrated checked-in pin round-trips byte for byte. Tests independently recompute each field hash from canonical JSON, including reversed object-key order, preserved list order, `null`, and a non-ASCII string. Migration preserves shipped `1.22.0`, sets ceiling `1.23.0`, baselines numeric identities through that ceiling, and refuses a seeded v1 or full-fixture mismatch before writing. Current code fails on `Version("unreleased")`, demands a minor bump, and has no v2 reader. | Remove the v2 format check; replace the identities array with a count; accept an empty code; ignore a wrong `since_counts` total; baseline only identities at or below `1.22.0`; hash pretty JSON instead of the specified canonical bytes; flip one nibble of the golden summary digest; skip the legacy preflight. |
| AT-1b | Start from v2 plus a historical fixture. A first `deprecated_in=UNRELEASED` with replacement and a marked summary edit pass the ordinary pin CLI `--check`, fixture gate, append-only checks, and pending-edit policy before stamping. They must fail `reason_release_pin.py --release-ready` and the real `release_checks.sh` path before stamping. The same edits without markers fail ordinary validation; changing `kind`, `rules`, `owner`, `context`, `since`, or `symbol` under either marker fails; retargeting a numeric tombstone fails. Stamp deprecation-only and summary-only cases to live `1.23.1`, clear markers, recapture with `registry_version=1.23.1` and `baseline_ceiling=1.23.0`, then rerun both ordinary and release-ready gates green. Current aggregate pin and full-entry fixture comparison reject the legal cases. | Exempt every field of a pending identity; allow a non-null tombstone baseline; route ordinary `--check` through release-ready refusal; make release-ready pass before stamping; set `baseline_ceiling` to live `1.23.1`; leave the summary marker after stamping. |
| AT-2 | An unmarked summary change or rules change to a numeric identity fails per-field pin verification; adding an `UNRELEASED` identity does not. Changing a numeric field value while retaining its old digest, and changing one digest nibble while retaining the live value, both fail the actual pin CLI. Current pin has only one aggregate digest. | Skip numeric entries in the checker; digest all entries including `UNRELEASED`; compare only field names without checking their digests. |
| AT-3 | Table-test `allowed_registry_version` with shipped pin `1.22.0` and ceiling `1.23.0`. A numeric `since` above `1.23.0`, a live `REGISTRY_VERSION` other than `1.23.0`, or a branch-added numeric fixture fails; an `UNRELEASED` addition passes. Current code accepts the old numeric workflow. | Remove `unreleased_violations`; compute allowed version from the pin alone. |
| AT-4 | In `test_reason_release_pin.py`, `--release-ready` fails for one pending identity, one pending deprecation, and one pending summary marker. In `test_release_checks_fragments.py`, the real shell path invokes that mode and retains E1's pre-`uv` fragment refusal. After each stamp both checks pass. | Drop the `--release-ready` invocation; make that mode ignore each pending class in turn; bypass the central policy with shell parsing. |
| AT-5 | In `test_reason_minor_fixtures.py`, every `UNRELEASED` identity must have an exact, case-sensitive `` `namespace/code` `` code-span claim in at least one valid E1 fragment; numeric historical identities need no fragment. The parser accepts the fragment grammar but the identity check rejects plain text, dotted or wrong-case spellings, whitespace or an extra slash, and both shorter and longer prefix collisions. | Remove the pending-code fragment check; search only `CHANGELOG.md`; use substring matching so `` `config/code_extra` `` satisfies `config/code`; accept a dotted spelling. |
| AT-8 | In `test_stamp_release_compile.py`, table-test the four transitions, including the current-base no-op. A candidate with two pending identities stamps both to `1.24.0`, writes exactly `reason-manifest-v1.24-release-0-23-0.json` for package `0.23.0`, round-trips its name, rebuilds contract and docs, captures v2 with both `registry_version` and `baseline_ceiling` at `1.24.0`, compiles fragments, and passes all postconditions. Deprecation-only and summary-only transitions set `registry_version=1.23.1` but keep `baseline_ceiling=1.23.0`; no-op keeps both at `1.23.0`. Patch and no-op cases write no fixture, and the no-op still recaptures the pin. A docstring containing `"unreleased"` is unchanged. Forced verification and mid-apply failures restore all owned paths. | Make deprecation-only unchanged; set a patch transition's ceiling to live `1.23.1`; leave the ceiling at `1.23.0` after stamping new identities; use the dotted package version as the fixture slug; rewrite every matching string; omit no-op pin capture; disable rollback for the created fixture. |
| AT-10 | In `test_registry_renumber.py`, a temporary identity-addition branch owns a reason, Python expected-data carrier, JSON carrier, Markdown doc and record, consumer-contract fact, minor fixture, and fragment naming `1.24.0`. `--to-unreleased` adds the constant import; emits unquoted `since=UNRELEASED` only in the catalog; emits lowercase `"unreleased"` in Python and JSON data; emits lowercase `unreleased` inside the existing Markdown delimiters; resets only version anchors to main's numeric version; removes only the branch fixture; preserves the branch contract fact in the rebuilt contract; and makes `--check` pass. Separate edit-only first-deprecation and marked-summary branches, plus a mixed new-identity and existing-edit branch, refuse before candidate creation with `edited-existing` and, where applicable, `version-without-identity`; every real-tree byte remains unchanged. A forced candidate verification failure also leaves the real worktree byte-identical. | Emit JSON value `"UNRELEASED"`; emit quoted catalog `since="unreleased"`; emit bare uppercase `UNRELEASED` in Markdown; omit `changelog.d/*.md`; allow a marked existing-identity edit through conversion; copy main's contract; skip constant-import insertion; apply before candidate verification. |

Run every named mutation after the final source change and record the exact head. A test-only fix
after a mutation requires rerunning the affected mutation; any source fix requires rerunning all E2
mutations.

## Verification and gate

T0 runs the touched test module after each edit. T1 runs every named test file in the Placement
table plus the repository guard set, Ruff check, Ruff format check, mypy, import layers, module-size
and fan-out sentries, and `python scripts/dev/registry_renumber.py --check --main origin/main` on an
appropriate scratch branch. Exercise `reason_release_pin.py --check`, `--release-ready`, and
`release_checks.sh` through their real CLI paths, not only direct function calls.

Run the full default suite once on the exact merge head. Trigger: E2 changes the frozen reason
manifest contract, migrates a stored pin format, changes shared release fixtures, and changes
release tooling. Compare failing IDs and collected IDs with the stored current-main baseline. Do
not run a second full suite on the same head. Gate only after all red-first evidence, final-head
mutations, T1, full-suite baseline diff, independent review, and exact-artifact final gate are
green.

## Downstream

GEN-REL (branch `plan/gen-rel`) builds on E2 and declares every new reason code
`since=UNRELEASED`.
