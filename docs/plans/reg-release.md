# REG-RELEASE: stamp the reason registry and the CHANGELOG at release time

Status: plan

Rules consulted: 00-universal, risk-and-exceptions, development-loop ("Proportion"),
local-operations, workspace-and-vcs, reuse-first, exploration, scope-discipline, verification,
code-review, testing, architecture, cross-cutting-concerns, api-and-compatibility,
release-and-rollback, documentation, catalogs/recurring-defects

Base: engine main `79d5ded7`. Engine only. Light plan by owner rule (2026-11-10): the change is to
release tooling and test guards, with no product behavior and no `src/` logic beyond one constant.

**Guarantee.** A PR adds reason codes or declares one of the two allowed pending edits, adds one
changelog fragment, and does not claim a release version. Release prep applies the one registry
transition selected below, writes a minor fixture when that transition adds identities, regenerates
the consumer contract and compiles the fragments. Before and after stamping, the complete pin and
minor-fixture gates pass while still rejecting an undeclared released-code change and a new code
with no fragment.
**Non-goals:** changing what a registry minor means to consumers; the CLI's own CHANGELOG; the
package version (`pyproject.toml`), which the release prep already sets.

## Why

Today every PR that adds a code bumps `REGISTRY_VERSION` (`src/decoy_engine/reasons/_registry.py:24`),
adds a `reason-manifest-v1.N-<slug>.json` fixture, regenerates `tests/fixtures/f29/consumer-contract.json`,
edits the `registry_version` sentence in `docs/reasons.md:21`, and writes into `CHANGELOG.md`
`[Unreleased]`. Two PRs in flight claim the same minor, so the second to merge runs
`scripts/dev/registry_renumber.py`, and every merge gives the next PR a CHANGELOG conflict. Each fix
means a new head and a full gate rerun. Eleven minors (1.7 to 1.17) landed between the 0.19.0
release (registry 1.6.0, `tests/fixtures/f29/reason-registry-release.json`) and today, one per PR.

Established pattern: news fragments compiled at release (towncrier, scriv), and a release-time
stamp of "unreleased" markers (Django's `versionadded` filled at release, Rust's
`#[stable(since = "CURRENT_RUSTC_VERSION")]` replaced when a release branches).

## Behavior

### Registry

- New constant `UNRELEASED = "unreleased"` in `reasons/_types.py`. A new code is declared with
  `since=UNRELEASED`. A PR does not touch `REGISTRY_VERSION`, the minor fixtures, the consumer
  contract or the `docs/reasons.md` version sentence.
- `manifest()` reports `"since": "unreleased"` for such codes; `registry_version` stays the last
  released value. An older consumer that meets one of these codes in a dev build degrades to
  `decoy.unknown_reason_from_future`, as it does today for any newer code.
- **Ceiling.** The per-PR minors already on main (up to 1.17.0 at `79d5ded7`) stay as they are. A
  fixed constant `LAST_PER_PR_MINOR` in `scripts/release/reason_manifest_policy.py` records the
  value on the build head. The allowed registry version is `max(pin version, LAST_PER_PR_MINOR)`,
  and `REGISTRY_VERSION` must equal it. A PR that lands a new numeric minor (1.18.0) before E2
  merges turns E2's own check red, so E2 is rebuilt on that main with the new ceiling.
- **Changes to a released code.** Two edits stay legal after release, each declared as pending and
  stamped at release:
  - Deprecation: `deprecated_in=UNRELEASED` with its `replaced_by` link (the SUB-STOP code
    `config.subset_run_unsupported` retires this way when SUB-RUN lands, `docs/reasons.md:23`).
  - Editorial summary fix: the identity is listed in `PENDING_SUMMARY_EDITS` in
    `reasons/_registry.py`.

  The stamp sets `deprecated_in` to the selected release registry version and clears
  `PENDING_SUMMARY_EDITS`. Owner question R1 remains open; this plan uses the recommended **yes**.
- **One transition table.** `registry_transition(live, pin, pending_summaries)` in
  `scripts/release/reason_manifest_policy.py` is the only decision point used by the stamp, checks
  and tests. It returns the current version or the next release version, never a second caller-owned
  rule:

  | Pending state | Registry transition | Minor fixture |
  |---|---|---|
  | one or more new identities (with or without pending edits) | next minor | write one |
  | deprecation only (or deprecation plus summaries) | next patch | none |
  | summary only | next patch | none |
  | nothing pending | unchanged; stamp is a no-op for the registry | none |

  A later identity release after a patch advances to the next minor (for example, `1.17.1` to
  `1.18.0`).
- **Policy changes** (`scripts/release/reason_manifest_policy.py`, the one place the rules live).
  Every function that parses `since` with `Version()` learns `UNRELEASED`, because
  `Version("unreleased")` is not a valid PEP 440 version:
  - one shared pending-edit descriptor names the only conditionally mutable fields: `summary` for
    identities in `PENDING_SUMMARY_EDITS`, and `deprecated_in` plus `replaced_by` for a released
    identity whose `deprecated_in` is `UNRELEASED` **and whose pinned `deprecated_in` and
    `replaced_by` field hashes are the canonical hashes of null**. Thus the marker can create a
    tombstone but cannot retarget one. Both the pin writer and checker consume this descriptor;
    callers cannot maintain parallel allowlists. `_registry.py` exposes the declared identities
    through a read-only `pending_summary_edits()` accessor re-exported from the owning
    `decoy_engine.reasons` public surface, so release tooling does not import a private module;
  - migrate `reason-registry-release.json` to a strictly validated, versioned pin format. Version 2
    keeps registry/count metadata plus a per-identity, per-field SHA-256 baseline. That baseline is
    the minimum old-state information needed to ignore only the declared pending fields while
    comparing every other field; the current single aggregate digest cannot do that. The one-time
    E2 migration first runs the legacy v1 aggregate-pin and full-entry minor-fixture gates on the
    same tree, and refuses to capture if either is red. It then preserves the pin's last
    package-release `registry_version` (`1.6.0` on this base)
    but baselines every numeric live identity through `LAST_PER_PR_MINOR` (`1.17.0` here). This is
    required before historical fixtures stop comparing the three release-mutable fields; otherwise
    entries added in 1.7--1.17 would lose their only summary/tombstone baseline. Later release
    `--write` operations set the pin version to the stamped version and baseline every numeric
    identity. The reader rejects a missing/unknown format version, extra/missing required keys and
    wrong primitive types. `release_pin()` writes this format, and `release_pin_violations()` checks
    it;
  - `reason_release_pin.py --check` calls
    `release_pin_violations(checked_in_pin, live, pending_summary_edits())` rather than comparing the
    checked-in pin with a freshly generated pin. It ignores new `UNRELEASED` identities and only the
    descriptor-authorized fields of released identities. `--write` is used after stamping to capture
    all now-numeric identities and the newly stamped edits;
  - `since_floor_violations` (`:119`, run on the live catalog by
    `tests/unit/test_reason_release_pin.py:128`) skips `UNRELEASED`;
  - `append_only_violations` (`:45`) no longer requires a minor bump for an `UNRELEASED` addition;
  - new `pending_codes`, `allowed_registry_version` and `registry_transition` hold the pending,
    allowed-version and transition-table rules. `stamp_release.py`, the fragment sentry and
    `release_checks.sh` call them; no shell or script copy exists
    (`tests/sentry/test_reason_policy_single_source.py` keeps it that way).
  - one `fixture_entry_view()` removes only the fields that are release-mutable after an identity's
    introduction (`summary`, `deprecated_in`, `replaced_by`). `_fixture_violations()` compares this
    view for both the historical fixture and live entry, and ignores live `UNRELEASED` identities.
    The release pin, not an identity-introduction fixture, freezes the current values of those three
    fields. The guard test proves no other field can drift;
  - New `unreleased_violations(live, pin)`: a numeric `since` above the allowed version, or a
    `REGISTRY_VERSION` other than the allowed one, fails. This is what stops a PR from bumping.
  - `since_window_violations` (`:130`) treats `UNRELEASED` as inside the window.
  - `summary_version_violations` (`:150`) is unchanged.
- **Release stamp.** New `scripts/release/stamp_release.py --registry-minor auto|X.Y.0`, run on the
  release prep branch:
  1. Calls `registry_transition()` once. `--registry-minor X.Y.0` is accepted only when it equals
     the automatic minor transition; patch-only and no-op states use `auto` so the CLI cannot
     contradict the table.
  2. Rewrites `since=UNRELEASED` and `deprecated_in=UNRELEASED` to the selected numeric registry
     version in
     `src/decoy_engine/reasons/**` and sets `REGISTRY_VERSION`, with the source token rewrite the
     renumber script already has (`token()` and `plan_rewrites`, `scripts/dev/registry_renumber.py:82`
     and `:140`), applied to a temporary copy first; `restamp` (`reason_manifest_policy.py:263`)
     stamps the JSON fixtures.
  3. On a minor transition, `release_fixture_slug()` in the central policy lowercases the package
     version and replaces each run outside `[a-z0-9]` with one hyphen, then requires the name to
     round-trip through `fixture_name()` and `parse_fixture_name()`. It writes, for example, package
     `0.20.0` as `reason-manifest-v1.18-release-0-20-0.json`. On any transition it
     regenerates `consumer-contract.json`, updates the `docs/reasons.md` version sentence (`:21`),
     records the release paragraph, and re-captures the v2 pin with
     `reason_release_pin.py --write`.
- `cut-release.sh` refuses to tag while any code or deprecation is `UNRELEASED`,
  `PENDING_SUMMARY_EDITS` is not empty, or `changelog.d/` holds any file besides its README (new
  checks in `release_checks.sh`). This catches a fragment that merges between the release prep PR
  and the tag.
- Tests for a new code assert that it is registered and who owns it, not its `since`; the minor
  fixture is the pin.

### CHANGELOG

- A PR that changes behavior adds one file `changelog.d/<slice-slug>.md`. It holds exactly the block
  it would have put under `[Unreleased]` today, starting with a `### <Type> (<title>, <slice>,
  <date>)` heading. An optional first line `Breaking: <one line>` feeds the release's "Breaking for
  consumers" list.
- `parse_fragment()` in `stamp_release.py` is the one strict fragment grammar used by both the
  compiler and `check_fragment.py`. It uses whole-line matching (`fullmatch`, never `$`) for the
  optional `Breaking:` line and required heading, rejects a missing/duplicate heading or malformed
  first line, and returns the parsed block. The PR workflow therefore fails malformed fragments
  before release rather than letting the compiler reinterpret them.
- `CHANGELOG.md` keeps an `## [Unreleased]` heading (`tests/unit/test_version_agreement.py:76`
  requires it first) with no new text under it.
- **Release compile.** `stamp_release.py` also compiles the fragments, newest first by the commit
  that added each file (release prep runs on a full clone; the script refuses a shallow one), under
  the new `## [X.Y.Z] - YYYY-MM-DD` heading. The existing `[Unreleased]` body (about 420 lines at
  `CHANGELOG.md:10-430`, with its own `### Breaking for consumers` at `:67`) is folded in below
  the fragments. The `Breaking:` lines and the existing Breaking list merge into one
  `### Breaking for consumers` list. The compile then deletes the fragments and adds the previous
  release's row to `tests/fixtures/changelog_released_sections.json`.
- Tests that check unreleased CHANGELOG text read it through one helper,
  `tests/_changelog.unreleased_text()`, which returns `[Unreleased]` plus every fragment
  (`tests/native/test_c1b_i_metadata_inventory.py:171` is the current reader).
- **Missing fragment.** A sentry requires every `UNRELEASED` code to be named in some fragment. A
  dedicated lightweight PR workflow, `.github/workflows/changelog-fragment.yml`, follows
  `compat-preflight.yml`: `pull_request`, read-only contents, `ubuntu-latest`, a five-minute timeout,
  full checkout, Python setup, explicit base fetch, then
  `scripts/dev/check_fragment.py --base origin/<base ref>`. It is added to
  `TRIGGERED_ON_PURPOSE` in `tests/sentry/test_ci_config.py`; manual-only paid `ci.yml` remains
  unchanged. The check fails a PR that changes `src/` and adds no fragment unless its non-merge
  commits are all `test`, `docs`, `ci`, `chore` or `refactor` type.

## Migration of in-flight PRs

- E1 merges first and changes nothing about registry pending-edit policy or stamping. It is useful
  even if owner question R1 changes: in-flight PRs only move their `[Unreleased]` text into a
  fragment when they next merge main.
- When E2 merges, a PR that has bumped to a new minor converts with
  `registry_renumber.py --to-unreleased`: it reverts `REGISTRY_VERSION` and the docs sentence to
  main's, rewrites the claimed `since` and every branch-added numeric version carrier—including
  `changelog.d/*.md`—to `UNRELEASED`, and deletes the branch's minor fixture. It applies the
  conversion to a temporary copy, rebuilds `consumer-contract.json` from that converted branch
  with the existing `rg.extract(..., contract=True)` path, verifies it, then writes that rebuilt
  contract to the branch. It never copies main's contract, so branch-owned contract changes remain.
  `--check` then becomes the `unreleased_violations` check.
- After the first release under the rule, `registry_renumber.py` keeps only `--to-unreleased` and
  `--check`.

## Pre-mortem (failure modes)

| Risk | Guard |
|---|---|
| A released code changes without its narrow declaration | Per-field release pin over released entries (AT-2) |
| A declared pending edit either fails the old gates or weakens another field | Versioned per-field release pin plus immutable fixture view (AT-1b) |
| A PR still bumps the version or adds a minor fixture | `unreleased_violations` (AT-3) |
| A release ships an `UNRELEASED` code | `cut-release.sh` refusal (AT-4) |
| A new code ships with no CHANGELOG text | Fragment sentry (AT-5) and the merge-base CI step (AT-6) |
| The compile drops, reorders or duplicates a fragment, or loses a Breaking line | AT-7 round trip |
| The stamp rewrites a version token it should not | Restamp on a temporary copy, then the policy checks (AT-8) |
| `--to-unreleased` leaves a fragment stale or loses a branch contract change | Converted-branch contract rebuild and end-to-end migration (AT-10) |
| A stale-phrase test stops seeing unreleased text | The one helper reads fragments too (AT-9) |

## Acceptance tests

| # | Test | Red on |
|---|---|---|
| AT-1 | A synthetic registry with one `UNRELEASED` code passes every policy check, `since_floor_violations` and `append_only_violations` included, with `REGISTRY_VERSION` unchanged; the v2 pin writer/checker agree on numeric entries and strictly reject missing/unknown format versions and wrong stored primitive types. The migration fixture keeps package-release version `1.6.0` while field-baselining synthetic numeric identities through ceiling `1.17.0`; an unmarked summary edit to a `1.17.0` identity fails after fixture comparison switches to `fixture_entry_view()`. A legacy full-fixture mismatch makes migration refuse before it writes the v2 pin | current build ("added without a registry minor version bump"; `InvalidVersion` in `since_floor_violations`); a lax or unversioned pin reader; a migration that baselines only identities at or below the package-release pin or launders a legacy mismatch |
| AT-1b | Starting from a released v2 pin and historical minor fixture: (a) first-time `deprecated_in=UNRELEASED` plus its `replaced_by`, and (b) a summary edit listed in `PENDING_SUMMARY_EDITS`, pass the actual `reason_release_pin.py --check`/`release_checks.sh` pin path and `minor_fixture_violations()` before stamping; the same edits without markers fail, changing `kind` while either marker is present fails, and attempting to retarget an already numeric tombstone fails. Stamp each pending-only case, assert deprecation-only and summary-only select the next patch, recapture the pin, and rerun the complete CLI pin, minor-fixture and release-check gates after stamping | current aggregate-only pin; current complete-entry fixture comparison; a mutant that exempts every field of a pending entry or permits tombstone retargeting; a checker that differs from the writer |
| AT-2 | Editing a released entry's summary without its marker or editing its rules fails `release_pin_violations`; adding an `UNRELEASED` entry does not | a mutant that ignores the per-field baseline; a mutant that skips released entries |
| AT-3 | A numeric `since` above the allowed version fails, and so does a bumped `REGISTRY_VERSION` | a mutant without `unreleased_violations` |
| AT-4 | `release_checks.sh` fails with one `UNRELEASED` code, with a pending summary edit, and with one fragment left in `changelog.d/`; it passes after the stamp | a mutant without each new check |
| AT-5 | An `UNRELEASED` code named in no fragment fails the sentry | a mutant without the sentry |
| AT-6 | `check_fragment.py` fails a `feat` commit touching `src/` with no fragment, passes a `test` commit and a `feat` with one, and fails a fragment whose required heading or optional first line does not whole-line match the shared grammar | a mutant that ignores commit types; a checker that tests only file presence or uses a second parser |
| AT-7 | In a temporary repo whose commits add three fragments out of name order, one with `Breaking:`, compile into a copy of the real `CHANGELOG.md`: one dated section, fragments newest first then the old `[Unreleased]` body, one merged Breaking list, fragments deleted, released-sections pin row added, `test_version_agreement` and the breaking ratchet green; malformed fragments and a shallow clone are refused | a mutant that sorts by name; a mutant that drops `Breaking:` lines or the old body; a compiler that bypasses `parse_fragment()` |
| AT-8 | Table-test the central transition: new identity -> next minor; deprecation-only -> next patch; summary-only -> next patch; nothing pending -> unchanged. Then stamp a synthetic tree with two `UNRELEASED` codes: both get `X.Y.0`, one fixture named with the real package-version slug is written, `parse_fixture_name(actual_name)` returns that minor/slug, `minor_fixture_violations({actual_name: fixture}, live, floor)` passes, contract and v2 pin are current, and a `"unreleased"` string in a docstring is untouched | the contradictory code-only transition; a dotted, parser-invalid fixture slug; a mutant that rewrites every match |
| AT-9 | `unreleased_text()` returns fragment text; the metadata-inventory stale-phrase test fails on a stale phrase placed only in a fragment | current build (fragments unread) |
| AT-10 | In a temporary git repo, an in-flight branch owns a consumer-contract change, a minor fixture and a fragment whose text names its claimed numeric registry version. `--to-unreleased` converts the source and fragment, removes the fixture, preserves the branch-owned contract fact in the contract rebuilt from the converted tree, and passes `--check`; a forced post-conversion verification failure leaves the real worktree untouched | a rewrite surface that omits `changelog.d/*.md`; copying main's contract wholesale |

Mutations to run on each final head: exempt all fields of a pending identity; omit the v2 pin format
check; compare full historical fixture entries; change the deprecation-only transition to unchanged;
drop the cut-release check; ignore `Breaking:` lines; let `check_fragment.py` pass every commit type;
drop the non-empty `changelog.d/` refusal; remove `changelog.d/*.md` from the migration rewrite
surface; replace the converted-branch contract with main's. Each must turn a named test red.

## Reuse inventory, frozen contracts

- **Reuse:** `restamp` (JSON stamping), `canonical_digest` and `fixture_name` from
  `scripts/release/reason_manifest_policy.py`; `token()` and `plan_rewrites` (source rewrite) and
  the temporary-copy check from `scripts/dev/registry_renumber.py`;
  `reason_release_pin.py --write`; the consumer-contract builder in
  `tests/unit/progress/test_consumer_contract.py`; the released-sections pin.
- **Choke points:** pending-field allowances and the four registry transitions live only in
  `reason_manifest_policy.py`; `test_reason_policy_single_source.py` fails if the stamp, pin CLI,
  release shell or migration reimplements them. The realistic red mutation is a local transition
  branch or pending-field list added to `stamp_release.py`. Fragment enforcement lives in
  `check_fragment.py`, and the dedicated workflow only invokes it. Fragment syntax lives only in
  `stamp_release.parse_fragment()`; the checker and compiler both consume that parsed result. A
  guard test fails if either script grows its own heading/`Breaking:` regex.
- **Frozen contracts:** the reason manifest shape gains one `since` value, `"unreleased"`, seen only
  in dev builds; released manifests never carry it (AT-4). `registry_version` semantics for
  consumers do not change. The CHANGELOG format of released sections does not change.

## Placement

Before E1 adds files, update CODEMAP's "Where things go" table with three rows: changelog fragments
and their README in `changelog.d/`; cross-suite test helpers in `tests/_<concern>.py`; and lightweight
automatic PR checks in `.github/workflows/<concern>.yml` with their sentry allowlist. These are map
changes for the two previously unmapped artifact kinds and the new workflow, not new packages.

| Change | File | CODEMAP row | Sits beside |
|---|---|---|---|
| `UNRELEASED` | `src/decoy_engine/reasons/_types.py` | Reason codes | `ReasonSpec.since` |
| Policy changes, fixture slug, pin v2, transitions | `scripts/release/reason_manifest_policy.py`, `reason_release_pin.py` | Dev tooling (`scripts/release/`) | existing release policy and pin |
| Stamp and compile | new `scripts/release/stamp_release.py` | Dev tooling (`scripts/release/`) | `reason_release_pin.py` |
| Release refusal | `scripts/release/release_checks.sh` | Dev tooling (`scripts/release/`) | the reason pin check |
| `--to-unreleased` | `scripts/dev/registry_renumber.py` | Dev tooling (`scripts/dev/`) | `--check` |
| Merge-base fragment check | new `scripts/dev/check_fragment.py` | Dev tooling (`scripts/dev/`) | `registry_renumber.py` |
| Dedicated PR check | new `.github/workflows/changelog-fragment.yml` | new Lightweight automatic PR checks row | `compat-preflight.yml` |
| Fragment sentry | `tests/sentry/test_reason_minor_fixtures.py` | Sentry test | the one-fixture-per-minor test |
| Unreleased-text helper | new `tests/_changelog.py` | new Cross-suite test helpers row | `tests/sentry/_baseline_sources.py` |
| Fragment folder and README | new `changelog.d/README.md` | new Changelog fragments row | `CHANGELOG.md` |

## Slices

| Slice | Branch | Size | Content | Gate |
|---|---|---|---|---|
| E1 | `feat/reg-release-e1` | about 280 lines | CODEMAP rows; fragments; compile half of `stamp_release.py`; `check_fragment.py`; `unreleased_text()`; AT-6, AT-7 and AT-9; dedicated `changelog-fragment.yml` plus its existing sentry allowlist entry; non-empty `changelog.d/` release refusal. It has no dependency on R1 or registry pending edits and folds the existing `[Unreleased]` body | Full suite: test tooling and shared fixtures are escalation triggers in `testing.md` |
| E2 | `feat/reg-release-e2` | about 430 lines | `UNRELEASED`; v2 per-field pin; immutable fixture view; central transition table; stamp half of `stamp_release.py`; registry release refusal; converted-branch `--to-unreleased`; fragment sentry; docs; AT-1, AT-1b, AT-2 to AT-5, AT-8 and AT-10 | Full suite (frozen contract and pin-format migration) |

## Engineering calls made

- `registry_version` stays at the last release in dev builds; consumers already degrade on unknown
  codes, and a dev version would need a post-release bump commit.
- An in-repo compiler keeps today's `### Type (title, slice, date)` blocks and uses towncrier's
  fragment-folder convention, so there is no new dependency and no `uv.lock` change.
- A release with new codes takes exactly one registry minor, however many slices added codes.
- A release with only declared deprecations and/or summaries takes one patch; with nothing pending,
  the registry does not move.
- Recovery is clean-tree plus rollback from git, not resume, after three review rounds found
  recovery-provenance holes.

## Owner questions

1. **Changing a released code.** Deprecations and summary fixes after release are declared as
   pending (`deprecated_in=UNRELEASED`, `PENDING_SUMMARY_EDITS`) and stamped at release, with every
   other field frozen. Accept, or require any change to a released code to wait for a release
   branch? Recommended: accept; SUB-RUN needs the deprecation path before the next release.
