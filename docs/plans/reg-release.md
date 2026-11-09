# REG-RELEASE: stamp the reason registry and the CHANGELOG at release time

Status: plan

Rules consulted: 00-universal, development-loop ("Proportion"), testing (escalation triggers),
api-and-compatibility, workspace-and-vcs, catalogs/recurring-defects

Base: engine main `79d5ded7`. Engine only. Light plan by owner rule (2026-11-10): the change is to
release tooling and test guards, with no product behavior and no `src/` logic beyond one constant.

**Guarantee.** A PR adds reason codes and one changelog fragment, and nothing else that names a
version. The release prep stamps the registry version, writes the minor fixture, regenerates the
consumer contract and compiles the fragments. The guards still catch a released code that changes
and a new code with no fragment.
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

  The pin digest leaves out exactly those fields of exactly those entries; every other field of a
  released entry stays frozen. The stamp sets `deprecated_in` to the release minor, clears
  `PENDING_SUMMARY_EDITS`, and takes at least a patch bump when only summaries changed.
- **Policy changes** (`scripts/release/reason_manifest_policy.py`, the one place the rules live).
  Every function that parses `since` with `Version()` learns `UNRELEASED`, because
  `Version("unreleased")` is not a valid PEP 440 version:
  - one shared `released_view(manifest)` drops `UNRELEASED` entries and the pending fields above;
    `release_pin_violations` (`:95`) and `release_pin()` (`scripts/release/reason_release_pin.py:45`)
    both digest and count through it, so the pin and its check cannot disagree;
  - `since_floor_violations` (`:119`, run on the live catalog by
    `tests/unit/test_reason_release_pin.py:128`) skips `UNRELEASED`;
  - `append_only_violations` (`:45`) no longer requires a minor bump for an `UNRELEASED` addition;
  - new `pending_codes`, `allowed_registry_version` and `next_registry_version` hold the "any code
    unreleased", allowed-version and next-minor rules. `stamp_release.py`, the fragment sentry and
    `release_checks.sh` call them; no shell or script copy exists
    (`tests/sentry/test_reason_policy_single_source.py` keeps it that way).
  - `minor_fixture_violations` (`:183`) ignores `UNRELEASED` entries.
  - New `unreleased_violations(live, pin)`: a numeric `since` above the allowed version, or a
    `REGISTRY_VERSION` other than the allowed one, fails. This is what stops a PR from bumping.
  - `since_window_violations` (`:130`) treats `UNRELEASED` as inside the window.
  - `summary_version_violations` (`:150`) is unchanged.
- **Release stamp.** New `scripts/release/stamp_release.py --registry-minor auto|X.Y.0`, run on the
  release prep branch:
  1. If any code is `UNRELEASED`, the new registry version is the next minor above the allowed
     version; otherwise the registry does not move.
  2. Rewrites `since=UNRELEASED` and `deprecated_in=UNRELEASED` to `"X.Y.0"` in
     `src/decoy_engine/reasons/**` and sets `REGISTRY_VERSION`, with the source token rewrite the
     renumber script already has (`token()` and `plan_rewrites`, `scripts/dev/registry_renumber.py:82`
     and `:140`), applied to a temporary copy first; `restamp` (`reason_manifest_policy.py:263`)
     stamps the JSON fixtures.
  3. Writes one fixture `reason-manifest-vX.Y-release-<package version>.json`, regenerates
     `consumer-contract.json`, updates the `docs/reasons.md` version sentence (`:21`) and appends
     one release paragraph naming the fixture and its codes to the per-minor list (`:25-37`), and
     re-captures the pin with `reason_release_pin.py --write`.
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
  new `ci.yml` job on `pull_request` (checkout with `fetch-depth: 0`) runs
  `scripts/dev/check_fragment.py --base origin/<base ref>`. It fails a PR that changes `src/` and
  adds no fragment, unless its non-merge commits are all `test`, `docs`, `ci`, `chore` or
  `refactor` type. No workflow runs `registry_renumber.py` today, so this is a new job, not a
  moved one.

## Migration of in-flight PRs

- E1 merges first and changes nothing about the registry, so in-flight PRs only move their
  `[Unreleased]` text into a fragment when they next merge main.
- When E2 merges, a PR that has bumped to a new minor converts with
  `registry_renumber.py --to-unreleased` (new mode, about 40 lines): it reverts `REGISTRY_VERSION`,
  the consumer contract and the docs sentence to main's, rewrites the claimed `since` to
  `UNRELEASED`, and deletes the branch's minor fixture. `--check` then becomes the
  `unreleased_violations` check.
- After the first release under the rule, `registry_renumber.py` keeps only `--to-unreleased` and
  `--check`.

## Pre-mortem (failure modes)

| Risk | Guard |
|---|---|
| A released code changes meaning or summary after release | Release pin digest over released entries (AT-2) |
| A PR still bumps the version or adds a minor fixture | `unreleased_violations` (AT-3) |
| A release ships an `UNRELEASED` code | `cut-release.sh` refusal (AT-4) |
| A new code ships with no CHANGELOG text | Fragment sentry (AT-5) and the merge-base CI step (AT-6) |
| The compile drops, reorders or duplicates a fragment, or loses a Breaking line | AT-7 round trip |
| The stamp rewrites a version token it should not | Restamp on a temporary copy, then the policy checks (AT-8) |
| A stale-phrase test stops seeing unreleased text | The one helper reads fragments too (AT-9) |

## Acceptance tests

| # | Test | Red on |
|---|---|---|
| AT-1 | A synthetic registry with one `UNRELEASED` code passes every policy check, `since_floor_violations` and `append_only_violations` included, with `REGISTRY_VERSION` unchanged; `release_pin()` and `release_pin_violations` give the same digest | current build ("added without a registry minor version bump"; `InvalidVersion` in `since_floor_violations`) |
| AT-1b | `deprecated_in=UNRELEASED` on a released code, and a summary edit listed in `PENDING_SUMMARY_EDITS`, pass the pin; the same edits without the markers fail; a pending deprecation that also changes `kind` fails | a mutant that leaves every field out of the digest for pending entries |
| AT-2 | Editing a released entry's summary or rules fails `release_pin_violations`; adding an `UNRELEASED` entry does not | a mutant that digests all entries; a mutant that skips released entries |
| AT-3 | A numeric `since` above the allowed version fails, and so does a bumped `REGISTRY_VERSION` | a mutant without `unreleased_violations` |
| AT-4 | `release_checks.sh` fails with one `UNRELEASED` code, with a pending summary edit, and with one fragment left in `changelog.d/`; it passes after the stamp | a mutant without each new check |
| AT-5 | An `UNRELEASED` code named in no fragment fails the sentry | a mutant without the sentry |
| AT-6 | `check_fragment.py` fails a `feat` commit touching `src/` with no fragment, passes a `test` commit and a `feat` with one | a mutant that ignores commit types |
| AT-7 | In a temporary repo whose commits add three fragments out of name order, one with `Breaking:`, compile into a copy of the real `CHANGELOG.md`: one dated section, fragments newest first then the old `[Unreleased]` body, one merged Breaking list, fragments deleted, released-sections pin row added, `test_version_agreement` and the breaking ratchet green; a shallow clone is refused | a mutant that sorts by name; a mutant that drops `Breaking:` lines or the old body |
| AT-8 | Stamp a synthetic tree with two `UNRELEASED` codes: both get `X.Y.0`, one fixture written, contract and pin current, a `"unreleased"` string in a docstring untouched | a mutant that rewrites every match |
| AT-9 | `unreleased_text()` returns fragment text; the metadata-inventory stale-phrase test fails on a stale phrase placed only in a fragment | current build (fragments unread) |

Mutations to run on each final head: digest all entries in the pin; drop the cut-release check;
ignore `Breaking:` lines; let `check_fragment.py` pass every commit type; drop the non-empty
`changelog.d/` refusal; digest pending fields. Each must turn a test red.

## Reuse inventory, frozen contracts

- **Reuse:** `restamp` (JSON stamping), `canonical_digest` and `fixture_name` from
  `scripts/release/reason_manifest_policy.py`; `token()` and `plan_rewrites` (source rewrite) and
  the temporary-copy check from `scripts/dev/registry_renumber.py`;
  `reason_release_pin.py --write`; the consumer-contract builder in
  `tests/unit/progress/test_consumer_contract.py`; the released-sections pin.
- **Frozen contracts:** the reason manifest shape gains one `since` value, `"unreleased"`, seen only
  in dev builds; released manifests never carry it (AT-4). `registry_version` semantics for
  consumers do not change. The CHANGELOG format of released sections does not change.

## Placement

| Change | File | Sits beside |
|---|---|---|
| `UNRELEASED` | `src/decoy_engine/reasons/_types.py` | `ReasonSpec.since` |
| Policy changes, `unreleased_violations`, `LAST_PER_PR_MINOR` | `scripts/release/reason_manifest_policy.py` | `release_pin_violations` |
| Stamp and compile | new `scripts/release/stamp_release.py` | `reason_release_pin.py` |
| Release refusal | `scripts/release/release_checks.sh` | the reason pin check |
| `--to-unreleased` | `scripts/dev/registry_renumber.py` | `--check` |
| Merge-base fragment check | new `scripts/dev/check_fragment.py` | `registry_renumber.py` |
| Fragment sentry | `tests/sentry/test_reason_minor_fixtures.py` | the one-fixture-per-minor test |
| Unreleased-text helper | new `tests/_changelog.py` | `tests/sentry/_baseline_sources.py` |
| Fragment folder and README | new `changelog.d/README.md` | `CHANGELOG.md` |
| Rule text | `docs/reasons.md:41`, `CONTRIBUTING.md` | the renumber paragraph |

## Slices

| Slice | Branch | Size | Content | Gate |
|---|---|---|---|---|
| E1 | `feat/reg-release-e1` | about 260 lines | Fragments, compile half of `stamp_release.py`, `check_fragment.py`, `unreleased_text()`, AT-6, AT-7, AT-9, the `ci.yml` job and the non-empty `changelog.d/` release refusal; the compile folds the existing `[Unreleased]` body | Full suite: test tooling and shared fixtures are escalation triggers in `testing.md` |
| E2 | `feat/reg-release-e2` | about 380 lines | `UNRELEASED`, policy changes, stamp half of `stamp_release.py`, release refusal, `--to-unreleased`, fragment sentry, docs, AT-1, AT-1b, AT-2 to AT-5, AT-8 | Full suite (frozen contract: the reason manifest) |

## Engineering calls made

- `registry_version` stays at the last release in dev builds; consumers already degrade on unknown
  codes, and a dev version would need a post-release bump commit.
- An in-repo compiler keeps today's `### Type (title, slice, date)` blocks and uses towncrier's
  fragment-folder convention, so there is no new dependency and no `uv.lock` change.
- A release with new codes takes exactly one registry minor, however many slices added codes.

## Owner questions

1. **Changing a released code.** Deprecations and summary fixes after release are declared as
   pending (`deprecated_in=UNRELEASED`, `PENDING_SUMMARY_EDITS`) and stamped at release, with every
   other field frozen. Accept, or require any change to a released code to wait for a release
   branch? Recommended: accept; SUB-RUN needs the deprecation path before the next release.
